# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""TX73L64 pair driver: patterns, delay profiles, TRSW, loopback, and the pure delay math."""

import csv
import json
import logging
import math
import time

TX_TARGETS = ("tx1", "tx2")

# Transmitter initialization register values.
TX_GUI_INIT_WRITES = (
    ("broadcast", 0x6C, 0xB0110008),
    ("broadcast", 0x02, 0x80000000),
    ("broadcast", 0x94, 0x00000005),
    ("broadcast", 0x02, 0x00000000),
    ("broadcast", 0x55, 0x01000000),
    ("broadcast", 0x1B, 0x00000010),
    ("broadcast", 0x1F, 0x00000000),
    ("broadcast", 0x1F, 0x000000EF),
    ("broadcast", 0x0B, 0x00000002),
)

TX_MEM_PAGE_A_REG = 0x02
TX_MEM_PAGE_B_REG = 0x03
TX_MEM_START_WORD_REG = 0x33
TX_GUI_MEMORY_PAGE_ENABLE_WRITES = (
    ("broadcast", TX_MEM_PAGE_A_REG, 0x0000FFFF),
    ("broadcast", TX_MEM_PAGE_B_REG, 0x0000FFFF),
)
TX_GUI_MEMORY_CLEAR_FIRST = 0x80
TX_GUI_MEMORY_CLEAR_LAST = 0x27F

# Dual-lane TX SPI (SDATA_0 + SDATA_1): the page-enable regs gate which memory
# pages each lane's writes land in, so splitting them halves memory-load time.
TX_SDATA1_POWER_REG = 0x6C
TX_PDN_SDATA1_MASK = 0x0000_0008
TX_SPI_TERM_REG = 0x6F
# reg 0x6F termination codes, pre-shifted to bits 15:14
TX_SPI_SDATA_TERM_VALUES = {"400": 0x0000, "high-z": 0x4000, "100": 0x8000, "200": 0xC000}
TX_DUAL_SPI_SDATA0_PAGE_MASK = 0x5555
TX_DUAL_SPI_SDATA1_PAGE_MASK = 0xAAAA

# Sample pattern memory; the active pattern is selected via MEM_START_WORD.
TX_GUI_PATTERN_WORD_WRITES = (
    ("broadcast", 0x83, 0x00020005),
    ("broadcast", 0x84, 0x00004D49),
    ("broadcast", 0x85, 0x00040005),
    ("broadcast", 0x86, 0x99999D9D),
    ("broadcast", 0x87, 0x00040005),
    ("broadcast", 0x88, 0x9D9D9999),
    ("broadcast", 0x89, 0x00020005),
    ("broadcast", 0x8A, 0x00009D99),
    ("broadcast", 0x8B, 0x00020005),
    ("broadcast", 0x8C, 0x0000999D),
)
TX_GUI_SAMPLE_PATTERN_WRITES = (*TX_GUI_MEMORY_PAGE_ENABLE_WRITES, *TX_GUI_PATTERN_WORD_WRITES)

TX_SAMPLE_PATTERN_START_WORDS = {
    "3Level_10MHz": 3,
    "3Level_2.5MHz_inverted": 5,
    "3Level_2.5MHz": 7,
    "3Level_5MHz": 9,
    "3Level_5MHz_inverted": 11,
}
# Historical 5 MHz preset; L12-5D nominal center is 8 MHz.
# Frequency experiments explicitly program and verify a matched waveform.
TX_DEFAULT_PATTERN_PROFILE = "3Level_5MHz"

def _sample_pattern_words(profile):
    """The shipped header and payload for one pattern-memory slot."""
    source = {reg: value for _, reg, value in TX_GUI_PATTERN_WORD_WRITES}
    address = 0x80 + TX_SAMPLE_PATTERN_START_WORDS[profile]
    length = (source[address] >> 16) & 0x3FFF
    return {reg: source[reg] for reg in range(address, address + 1 + (length + 3) // 4)}


TX_LOOPBACK_ATTEN_CHOICES = (20, 24, 29, 34, 40, 47)
TX_LOOPBACK_FIXED_TGC_ENABLE = 0x000000C0
TX_LOOPBACK_FIXED_TGC_REG = 0x1F
TX_LOOPBACK_MODE_REG = 0x54
TX_DTGC_REGS = (0x05, 0x06, 0x07)
TX_FIXED_ATTEN_ENABLE_MASK = 0x40
TX_DTGC_START_TGC_MAX = 0x3FF
TX_DTGC_STEP_PERIOD_MAX = 0xFF
TX_LOOPBACK_MODE_RSOURCE_MASK = 0x60000000
TX_LOOPBACK_MODE_RSOURCE_100_OHM = 0x60000000
TX_MODE_RSOURCE_VALUES = {
    800: 0x00000000,
    400: 0x20000000,
    200: 0x40000000,
    100: 0x60000000,
}


def receive_dtgc_profile(start_db, start_us, ramp_us, bf_clock_hz=200e6):
    """Encode a one-segment receive DTGC ramp."""
    for value, name in ((start_db, "start attenuation"),
                        (start_us, "start time"), (ramp_us, "ramp time")):
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"DTGC {name} must be finite")
    if not 0.25 <= start_db <= 12.0 or start_db * 4 != round(start_db * 4):
        raise ValueError("DTGC start attenuation must be a 0.25 dB step in [0.25, 12]")
    if start_us < 0 or ramp_us <= 0:
        raise ValueError("DTGC start time must be nonnegative and ramp time must be positive")
    if not isinstance(bf_clock_hz, (int, float)) or not math.isfinite(bf_clock_hz) or bf_clock_hz <= 0:
        raise ValueError("DTGC BF clock must be finite and positive")

    # START_TGC and STEP_PER use two BF clocks per step.
    tick_us = 2e6 / bf_clock_hz
    start_atten = round(start_db * 4)
    start_tgc = round(start_us / tick_us)
    step_count = start_atten
    step_period = round(ramp_us / (tick_us * step_count))
    if start_tgc > TX_DTGC_START_TGC_MAX:
        raise ValueError(f"DTGC start time exceeds {TX_DTGC_START_TGC_MAX * tick_us:g} us")
    if not 0 <= step_period <= TX_DTGC_STEP_PERIOD_MAX:
        raise ValueError("DTGC ramp time cannot be represented by one segment")

    registers = {
        0x05: (start_tgc << 6) | start_atten,
        0x06: (step_count << 8) | step_period,
        0x07: 0,
    }
    return {
        "registers": registers,
        "start_attenuation_db": start_atten * 0.25,
        "start_tgc": start_tgc,
        "step_count": step_count,
        "step_period": step_period,
        "tick_us": tick_us,
        "quantized_start_us": start_tgc * tick_us,
        "quantized_ramp_us": step_period * step_count * tick_us,
    }


TX_LOOPBACK_CW_WRITES = (
    # CW mode: enable CW while preserving TR_BF_SYNC enable in reg 0x0b.
    # Reg 0x0c encodes levels 1/5/1/5 and periods 31/31/31/31.
    ("broadcast", 0x0B, 0x00000003),
    ("broadcast", 0x0C, 0xFDF9FDF9),
)
TX_LOOPBACK_PATTERN_WRITES = (
    # Pattern mode keeps TR_BF_SYNC enabled but leaves CW disabled.
    ("broadcast", 0x0B, 0x00000002),
)
TX_LOOPBACK_ROUTE_WRITES = (
    # T/R switch settings for loopback mode.
    ("broadcast", 0x19, 0xFFFFFFFF),
    ("broadcast", 0x18, 0x00000000),
    ("broadcast", 0x1B, 0x00000010),
    ("broadcast", 0x2E, 0x00000000),
    ("broadcast", 0x2D, 0xFFFFFFFF),
    ("broadcast", 0x17, 0x00000000),
    ("broadcast", 0x16, 0xFFFFFFFF),
)

TX_TRSW_EN_EVEN_REG = 0x16
TX_TRSW_EN_ODD_REG = 0x17
TX_TRSW_DIS_EVEN_REG = 0x18
TX_TRSW_DIS_ODD_REG = 0x19
TX_TRSW_SEL_REG = 0x1A
TX_TRSW_CTRL_REG = 0x1B
# Memory regs; these land only in pages enabled via MEM_PAGE_A/B (0x02/0x03).
# configure_probe re-enables all pages itself before writing them.
TX_TRSW_DELAY_WORD0_REG = 0x80
TX_TRSW_DELAY_WORD1_REG = 0x81
TX_PDN_PUL_EVEN_REG = 0x2D
TX_PDN_PUL_ODD_REG = 0x2E

TX_TRSW_EN_AUTO_DIS_RX = 0x00001000
TX_TRSW_GBL_MODE = 0x00000001

TX_PROBE_RX_SELECT_VALUES = {"odd": 0x00000000, "even": 0xFFFFFFFF}
TX_PROBE_RX_SELECT_CHOICES = tuple(TX_PROBE_RX_SELECT_VALUES) + ("both",)
# Settle after a per-shot TRSW flip (interleave); the switch itself is
# us-scale, this only guards the SPI transaction.
MUX_SETTLE_S = 0.002

TX_DELAY_PROFILE_START_WORD_DEFAULT = 510  # Calibration slot
TX_DELAY_PROFILE_REG_BASE = 0x80
TX_DELAY_PROFILE_WORDS = 2
TX_DELAY_BF_PROF_SEL_FIRST_REG = 0x0D
TX_DELAY_BF_PROF_SEL_REGS = 8
TX_DELAY_BF_PROF_SEL_MASK = 0x01FF01FF
TX_DELAY_BF_PROF_SEL_FIELDS = 16
TX_DELAY_FRAC_BIT = 14
TX_DELAY_INT_MASK = (1 << TX_DELAY_FRAC_BIT) - 1
TX_CHANNELS_PER_DEVICE = 64  # probe elements map 1:1 onto channels per chip
TX_AUTO_DELAY_PROFILE_REG = 0x15
TX_AUTO_DELAY_PROFILE_ENABLE = 1 << 12
TX_AUTO_DELAY_PROFILE_END_MASK = 0x01FF
BF_CLOCK_HZ = 200_000_000.0


def burst_pattern(frequency_hz, cycles=2, *, quiet=False, invert=False):
    """Encode a symmetric burst at the 200 MHz TX clock.

    TI §7.3.11.3.1.2: each byte is PERIOD[7:3], LEVEL[2:0];
    state duration is PERIOD+1 clocks. Keep the existing two-state slot,
    so delay memory and all pattern pointers remain unchanged.
    """
    if not math.isfinite(frequency_hz) or frequency_hz <= 0:
        raise ValueError("frequency must be finite and positive")
    ticks = min((20, 16, 12), key=lambda n: abs(BF_CLOCK_HZ/(2*n)-frequency_hz))
    actual = BF_CLOCK_HZ/(2*ticks)
    if abs(actual-frequency_hz)/actual > .001:
        raise ValueError("supported MHz: 5, 6.25, 8.333333 (8.33 accepted); exact 8 is not a symmetric preset")
    if isinstance(cycles, bool) or cycles not in (1, 2, 6):
        raise ValueError("burst cycles must be 1, 2 or 6")
    period = ticks-1
    if not isinstance(invert, bool):
        raise ValueError("invert must be a boolean")
    positive, negative = (4,4) if quiet else ((5,1) if invert else (1,5))
    payload = (period << 3 | positive) | ((period << 3 | negative) << 8)
    return dict(words={0x89: 0x20000 | (int(cycles)-1), 0x8A: payload},
                requested_frequency_hz=frequency_hz, frequency_hz=actual,
                digital_clock_hz=BF_CLOCK_HZ, half_period_ticks=ticks,
                burst_cycles=int(cycles), duration_s=cycles/actual,
                burst_center_s=cycles/(2*actual), quiet=bool(quiet), invert=invert)

TX_PW_SOUND_SPEED_MPS = 1480.0
TX_PW_PITCH_M = 0.3e-3
TX_PW_ELEMENTS = 128
TX_PW_DELAY_START_WORD_DEFAULT = 14
TX_PW_ELEMENT_MAP = (
    "EVM userguide SLVUDI1 sec 3.7.8: element e -> chip tx1 if (e % 64) < 32 else tx2, "
    "0-based channel 2*(e % 32) + e//64; inverse: chip t (0=tx1), 0-based "
    "channel c drives element 64*(c % 2) + 32*t + c//2"
)


def delay_word(half_cycles):
    """Encode a TX delay in BF half-cycles as its 15-bit memory word.

    Raises on integer-field overflow: a wrong-units calibration file must
    abort before firing the probe with garbage delays. plane_wave_delays
    clamps (with a warning) before encoding, so that path cannot raise.
    """
    half_cycles = int(round(half_cycles))
    if half_cycles < 0:
        raise ValueError(f"TX delay cannot be negative: {half_cycles}")
    integer = half_cycles >> 1
    if integer > TX_DELAY_INT_MASK:
        raise ValueError(f"TX delay overflows the 14-bit field: {half_cycles} half-cycles")
    return integer | ((half_cycles & 1) << TX_DELAY_FRAC_BIT)


def profile_words(half_cycles64):
    """Pack one chip's 64 delays into 16 blocks of two 32-bit memory words."""
    # block b (page 1<<b): word0 = ch(2b) | ch(2b+32)<<16, word1 = ch(2b+1) | ch(2b+33)<<16
    words = []
    for block in range(16):
        low = delay_word(half_cycles64[2 * block]) | (delay_word(half_cycles64[2 * block + 32]) << 16)
        high = delay_word(half_cycles64[2 * block + 1]) | (delay_word(half_cycles64[2 * block + 33]) << 16)
        words.append((low, high))
    return words


def chip_delays(half_cycles128):
    """Route 128 per-element delays onto per-chip 64-channel lists.

    Chip t channel c drives probe element 64*(c % 2) + 32*t + c//2 — the
    measured map (TX_PW_ELEMENT_MAP), verified against steered-feedthrough
    onsets at sub-sample residuals.
    """
    if len(half_cycles128) != TX_PW_ELEMENTS:
        raise ValueError(f"expected {TX_PW_ELEMENTS} delays, got {len(half_cycles128)}")
    return {
        target: [half_cycles128[64 * (c % 2) + 32 * t + c // 2]
                 for c in range(TX_CHANNELS_PER_DEVICE)]
        for t, target in enumerate(TX_TARGETS)
    }


def calibration_delays(path, column="tx_bf_half_cycles_rounded", units="half-cycles",
                       bf_clock_hz=BF_CLOCK_HZ):
    """Load a probe-calibrate JSON/CSV file into 128 per-element TX half-cycle delays."""
    delays = [None] * TX_PW_ELEMENTS
    for row in _calibration_rows(path):
        try:
            element, value = int(row["element"]), float(row[column])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"bad calibration row (need integer element and numeric {column!r}): {row!r}"
            ) from exc
        if not 1 <= element <= TX_PW_ELEMENTS:
            raise ValueError(f"calibration element {element} is outside 1..{TX_PW_ELEMENTS}")
        if units == "ns":
            half_period_ns = 1e9 / (2.0 * bf_clock_hz)
            value = value / half_period_ns
        delays[element - 1] = int(round(value))
    missing = [str(index + 1) for index, value in enumerate(delays) if value is None]
    if missing:
        raise ValueError("TX delay calibration is missing elements: "
                         + ", ".join(missing[:8]) + ("..." if len(missing) > 8 else ""))
    return delays


def _calibration_rows(path):
    if str(path).endswith(".json"):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        rows = data.get("results") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise ValueError(f"{path}: expected a list or an object with a results list")
        return rows
    with open(path, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def plane_wave_delays(angle_rad, c=TX_PW_SOUND_SPEED_MPS, bf_clock_hz=BF_CLOCK_HZ):
    """Steering delay ramp for one plane wave, as per-element BF half-cycles."""
    center = (TX_PW_ELEMENTS - 1) / 2.0
    taus = [(index - center) * TX_PW_PITCH_M * math.sin(angle_rad) / c
            for index in range(TX_PW_ELEMENTS)]
    min_tau = min(taus)
    cycles = [int(round((tau - min_tau) * bf_clock_hz)) for tau in taus]
    clamped = sum(1 for value in cycles if value > TX_DELAY_INT_MASK)
    if clamped:
        logging.warning("Plane-wave delay calculation clamped %d delay values to 14 bits", clamped)
    return [2 * min(value, TX_DELAY_INT_MASK) for value in cycles]


def plane_wave_angles(count, max_deg):
    """Symmetric sweep angles in degrees, always including the extremes."""
    if count < 1:
        raise ValueError("plane-wave angle count must be >= 1")
    if count == 1:
        return [0.0]
    step = (2.0 * max_deg) / (count - 1)
    return [-max_deg + index * step for index in range(count)]


class Tx:
    """Driver for the TX73L64 pair behind a Bus (or a Bus over a Trace).

    spi_lanes=2 loads pattern memory over both SPI SDATA lanes with split
    page-enable masks; bring-up powers SDATA_1 and sets its input termination.
    """

    def __init__(self, bus, config_wait_s=0.02, spi_lanes=1,
                 page_mask0=TX_DUAL_SPI_SDATA0_PAGE_MASK,
                 page_mask1=TX_DUAL_SPI_SDATA1_PAGE_MASK, sdata_term=None):
        self._bus = bus
        self._config_wait_s = config_wait_s
        self._spi_lanes = spi_lanes
        self._page_mask0 = page_mask0
        self._page_mask1 = page_mask1
        # 200 ohm is the only termination that makes TX2's SDATA_1 lane work.
        self._sdata_term = sdata_term or ("200" if spi_lanes == 2 else None)

    def init(self):
        logging.info("Programming TX initialization registers")
        self._replay(TX_GUI_INIT_WRITES)
        if self._spi_lanes == 2:
            base = next(v for _, reg, v in TX_GUI_INIT_WRITES if reg == TX_SDATA1_POWER_REG)
            value = base & ~TX_PDN_SDATA1_MASK
            logging.info("Enabling TX SDATA_1 input buffer for dual-lane SPI: "
                         "reg %#04x <= %#010x", TX_SDATA1_POWER_REG, value)
            self._write_wait("broadcast", TX_SDATA1_POWER_REG, value)
        if self._sdata_term is not None:
            value = TX_SPI_SDATA_TERM_VALUES[self._sdata_term]
            logging.info("Setting TX SPI SDATA termination to %s ohm: reg %#04x <= %#010x",
                         self._sdata_term, TX_SPI_TERM_REG, value)
            self._write_wait("broadcast", TX_SPI_TERM_REG, value)

    def clear_memory(self):
        # Always clear single-lane with all pages enabled: in a dual-lane run
        # this keeps stale memory from hiding a bad SDATA_1 path during the
        # later split-page pattern load.
        logging.info("Clearing TX pattern/delay memory %#x..%#x",
                     TX_GUI_MEMORY_CLEAR_FIRST, TX_GUI_MEMORY_CLEAR_LAST)
        self._replay(TX_GUI_MEMORY_PAGE_ENABLE_WRITES)
        for reg in range(TX_GUI_MEMORY_CLEAR_FIRST, TX_GUI_MEMORY_CLEAR_LAST + 1):
            self._write_wait("broadcast", reg, 0x00000000)

    def load_pattern(self, start_word=None):
        """Clear memory, program waveform pattern words, and select the start word."""
        self.clear_memory()
        if self._spi_lanes == 2:
            logging.info("Programming TX sample pattern memory over dual-lane SPI: "
                         "page masks SDATA0=%#06x SDATA1=%#06x",
                         self._page_mask0, self._page_mask1)
            self._write_wait("broadcast", TX_MEM_PAGE_A_REG, self._page_mask0)
            self._write_wait("broadcast", TX_MEM_PAGE_B_REG, self._page_mask1)
            for target, reg, value in TX_GUI_PATTERN_WORD_WRITES:
                self._bus.tx_write_dual(target, reg, value)
                self._wait()
            self._replay(TX_GUI_MEMORY_PAGE_ENABLE_WRITES)
        else:
            logging.info("Programming TX sample pattern memory")
            self._replay(TX_GUI_SAMPLE_PATTERN_WRITES)
        if start_word is None:
            start_word = TX_SAMPLE_PATTERN_START_WORDS[TX_DEFAULT_PATTERN_PROFILE]
        self.select_pattern(start_word)

    def select_pattern(self, start_word):
        """Select the TX waveform pattern memory start word."""
        logging.info("Selecting TX sample pattern at MEM_START_WORD=%d", start_word)
        for target in TX_TARGETS:
            self._write_wait(target, TX_MEM_START_WORD_REG, start_word)

    def set_fixed_receive_attenuation(self, code):
        """Set only 0x1F[5:0]; caller must use a reliable SPI read clock."""
        if not 0 <= code <= 48:
            raise ValueError("fixed receive attenuation code must be 0..48")
        before = {t: self._bus.tx_read(t, 0x1F) for t in TX_TARGETS}
        if any(not v & 0x40 for v in before.values()):
            raise RuntimeError("fixed receive attenuation is not enabled on both chips")
        after = {t: (v & ~0x3F) | code for t, v in before.items()}
        try:
            self.restore_fixed_receive_attenuation(after)
        except Exception:
            self.restore_fixed_receive_attenuation(before)
            raise
        return {"before": before, "after": after, "code": code,
                "nominal_attenuation_db": code * 0.25}

    def configure_receive_attenuation(self, code=None, source_ohms=None):
        """Configure fixed attenuation and its source-impedance model."""
        if code is None and source_ohms is None:
            raise ValueError("attenuation code or source impedance is required")
        if code is not None and not 0 <= code <= 48:
            raise ValueError("fixed receive attenuation code must be 0..48")
        if source_ohms is not None and source_ohms not in TX_MODE_RSOURCE_VALUES:
            raise ValueError("receive source impedance must be 100, 200, 400, or 800 ohms")

        atten_before = {target: self._bus.tx_read(target, TX_LOOPBACK_FIXED_TGC_REG)
                        for target in TX_TARGETS}
        mode_before = {target: self._bus.tx_read(target, TX_LOOPBACK_MODE_REG)
                       for target in TX_TARGETS}
        if any(not value & 0x40 for value in atten_before.values()):
            raise RuntimeError("fixed receive attenuation is not enabled on both chips")

        source_value = (None if source_ohms is None
                        else TX_MODE_RSOURCE_VALUES[source_ohms])
        atten_after = {
            target: value if code is None else (value & ~0x3F) | code
            for target, value in atten_before.items()
        }
        mode_after = {
            target: value if source_value is None
            else (value & ~TX_LOOPBACK_MODE_RSOURCE_MASK) | source_value
            for target, value in mode_before.items()
        }
        before = {"attenuation": atten_before, "mode": mode_before}
        after = {"attenuation": atten_after, "mode": mode_after}
        logging.info("Configuring TX receive path: attenuation_code=%s source_ohms=%s "
                     "before=%s requested=%s", code, source_ohms, before, after)
        try:
            for target in TX_TARGETS:
                if code is not None:
                    self._write_wait(target, TX_LOOPBACK_FIXED_TGC_REG,
                                     atten_after[target])
                if source_ohms is not None:
                    self._write_wait(target, TX_LOOPBACK_MODE_REG,
                                     mode_after[target])
            readback = {
                "attenuation": {
                    target: self._bus.tx_read(target, TX_LOOPBACK_FIXED_TGC_REG)
                    for target in TX_TARGETS
                },
                "mode": {
                    target: self._bus.tx_read(target, TX_LOOPBACK_MODE_REG)
                    for target in TX_TARGETS
                },
            }
            if readback != after:
                raise RuntimeError(
                    f"receive attenuation readback mismatch: {readback} != {after}")
        except Exception:
            self.restore_receive_attenuation(before)
            raise

        actual_code = next(iter(readback["attenuation"].values())) & 0x3F
        encoded_source = (next(iter(readback["mode"].values()))
                          & TX_LOOPBACK_MODE_RSOURCE_MASK)
        actual_source = next(ohms for ohms, value in TX_MODE_RSOURCE_VALUES.items()
                             if value == encoded_source)
        logging.info("TX receive path verified: readback=%s attenuation_code=%d "
                     "source_ohms=%d", readback, actual_code, actual_source)
        return {"before": before, "after": after, "readback": readback,
                "registers": {"0x1f": readback["attenuation"],
                              "0x54": readback["mode"]},
                "code": actual_code, "source_ohms": actual_source,
                "nominal_attenuation_db": actual_code * 0.25}

    def configure_receive_dtgc(self, start_db, start_us, ramp_us,
                               source_ohms=None, bf_clock_hz=200e6):
        """Configure and verify a one-segment dynamic receive attenuation ramp."""
        if source_ohms is not None and source_ohms not in TX_MODE_RSOURCE_VALUES:
            raise ValueError("receive source impedance must be 100, 200, 400, or 800 ohms")
        profile = receive_dtgc_profile(start_db, start_us, ramp_us, bf_clock_hz)
        before = {
            "attenuation": {
                target: self._bus.tx_read(target, TX_LOOPBACK_FIXED_TGC_REG)
                for target in TX_TARGETS
            },
            "dtgc": {
                target: {reg: self._bus.tx_read(target, reg) for reg in TX_DTGC_REGS}
                for target in TX_TARGETS
            },
            "mode": {
                target: self._bus.tx_read(target, TX_LOOPBACK_MODE_REG)
                for target in TX_TARGETS
            },
        }
        source_value = (None if source_ohms is None
                        else TX_MODE_RSOURCE_VALUES[source_ohms])
        after = {
            "attenuation": {
                target: value & ~TX_FIXED_ATTEN_ENABLE_MASK
                for target, value in before["attenuation"].items()
            },
            "dtgc": {
                target: dict(profile["registers"])
                for target in TX_TARGETS
            },
            "mode": {
                target: value if source_value is None
                else (value & ~TX_LOOPBACK_MODE_RSOURCE_MASK) | source_value
                for target, value in before["mode"].items()
            },
        }
        logging.info("Configuring TX receive DTGC: start_db=%g start_us=%g ramp_us=%g "
                     "profile=%s before=%s requested=%s",
                     start_db, start_us, ramp_us, profile, before, after)
        try:
            for target in TX_TARGETS:
                for reg in TX_DTGC_REGS:
                    self._write_wait(target, reg, after["dtgc"][target][reg])
                if source_ohms is not None:
                    self._write_wait(target, TX_LOOPBACK_MODE_REG,
                                     after["mode"][target])
                # Enable dynamic mode only after the complete profile is present.
                self._write_wait(target, TX_LOOPBACK_FIXED_TGC_REG,
                                 after["attenuation"][target])
            readback = self._read_receive_attenuation_state(include_dtgc=True)
            if readback != after:
                raise RuntimeError(f"receive DTGC readback mismatch: {readback} != {after}")
        except Exception:
            self.restore_receive_attenuation(before)
            raise
        return {"before": before, "after": after, "readback": readback,
                "source_ohms": source_ohms, **profile}

    def _read_receive_attenuation_state(self, include_dtgc=False):
        state = {
            "attenuation": {
                target: self._bus.tx_read(target, TX_LOOPBACK_FIXED_TGC_REG)
                for target in TX_TARGETS
            },
            "mode": {
                target: self._bus.tx_read(target, TX_LOOPBACK_MODE_REG)
                for target in TX_TARGETS
            },
        }
        if include_dtgc:
            state["dtgc"] = {
                target: {reg: self._bus.tx_read(target, reg) for reg in TX_DTGC_REGS}
                for target in TX_TARGETS
            }
        return state

    def restore_receive_attenuation(self, values):
        """Restore and verify receive attenuation, DTGC, and source-mode registers."""
        for target in TX_TARGETS:
            self._write_wait(target, TX_LOOPBACK_FIXED_TGC_REG,
                             values["attenuation"][target])
            for reg, value in values.get("dtgc", {}).get(target, {}).items():
                self._write_wait(target, reg, value)
            self._write_wait(target, TX_LOOPBACK_MODE_REG, values["mode"][target])
        actual = self._read_receive_attenuation_state("dtgc" in values)
        if actual != values:
            raise RuntimeError(f"receive attenuation readback mismatch: {actual} != {values}")
        return actual

    def restore_fixed_receive_attenuation(self, values):
        for target, value in values.items():
            self._write_wait(target, 0x1F, value)
        actual = {t: self._bus.tx_read(t, 0x1F) for t in values}
        if actual != values:
            raise RuntimeError(f"receive attenuation readback mismatch: {actual} != {values}")

    def _verify_sample_words(self, profile, words):
        start = TX_SAMPLE_PATTERN_START_WORDS[profile]
        try:
            for target in TX_TARGETS:
                selected = self._bus.tx_read(target, TX_MEM_START_WORD_REG)
                if selected != start:
                    raise RuntimeError(f"{target}: pattern pointer {selected:#x}, expected {start:#x}")
                for page in range(16):
                    self._write_wait(target, TX_MEM_PAGE_A_REG, 1 << page)
                    self._write_wait(target, TX_MEM_PAGE_B_REG, 1 << page)
                    for reg, value in words.items():
                        actual = self._bus.tx_read(target, reg)
                        if actual != value:
                            raise RuntimeError(
                                f"{target} page {page + 1} reg {reg:#x}: "
                                f"read {actual:#010x}, expected {value:#010x}")
        finally:
            self._replay(TX_GUI_MEMORY_PAGE_ENABLE_WRITES)

    def _write_sample_words(self, profile, words):
        self._replay(TX_GUI_MEMORY_PAGE_ENABLE_WRITES)
        for reg, value in words.items():
            self._write_wait("broadcast", reg, value)
        self._verify_sample_words(profile, words)

    def restore_sample_pattern(self, profile=TX_DEFAULT_PATTERN_PROFILE):
        self._write_sample_words(profile, _sample_pattern_words(profile))
        logging.info("TX sample waveform restored and verified on all pages")

    def verify_sample_pattern(self, profile=TX_DEFAULT_PATTERN_PROFILE, *, quiet=False,
                              burst_cycles=None, frequency_hz=None, invert=False):
        """Verify the shipped pattern, apply bounded changes, verify every page.

        Must run before capture at a reliable read clock. Frequency changes
        reuse the two-state slot; G repeats means G+1 cycles.
        """
        if invert and frequency_hz is None:
            raise ValueError("inversion requires explicit frequency and burst cycles")
        programmed = None
        clocks = None
        if quiet or burst_cycles is not None or frequency_hz is not None:
            if profile != TX_DEFAULT_PATTERN_PROFILE:
                raise ValueError("burst programming requires the default pattern slot")
            if frequency_hz is not None and burst_cycles is None:
                raise ValueError("frequency programming requires explicit cycles")
            programmed = burst_pattern(frequency_hz if frequency_hz is not None else 5e6,
                                       6 if burst_cycles is None else burst_cycles,
                                       quiet=quiet, invert=invert)
        if frequency_hz is not None:
            clocks = {t: {hex(r): self._bus.tx_read(t, r) for r in (0x0B, 0x6E)}
                      for t in TX_TARGETS}
            if any(v['0xb'] & 0xF001 or v['0x6e'] & 0x8 for v in clocks.values()):
                raise RuntimeError("frequency programming requires pattern mode and undivided TX clocks")
        start = TX_SAMPLE_PATTERN_START_WORDS[profile]
        original = _sample_pattern_words(profile)
        expected = programmed["words"] if programmed is not None else original
        self._verify_sample_words(profile, original)
        if expected != original:
            try:
                self._write_sample_words(profile, expected)
            except Exception:
                self._write_sample_words(profile, original)
                raise
        result = dict(profile=profile, start_word=start, quiet=quiet,
                      burst_cycles=burst_cycles, verified_pages_per_chip=16,
                      words={hex(k): hex(v) for k, v in expected.items()})
        if programmed is not None:
            result.update({k: v for k, v in programmed.items() if k != "words"})
            result["clock_readback"] = clocks
        logging.info("TX waveform readback verified: %s", result)
        return result

    def load_delay_profiles(self, profiles, first_word):
        """Bulk-load consecutive 128-element delay profiles; selects first_word."""
        last_reg = TX_DELAY_PROFILE_REG_BASE + first_word + len(profiles) * TX_DELAY_PROFILE_WORDS - 1
        if last_reg > TX_GUI_MEMORY_CLEAR_LAST:
            raise ValueError(f"{len(profiles)} TX delay profiles at word {first_word} exceed memory range")
        logging.info("Programming %d TX delay profiles at start_word=%d", len(profiles), first_word)
        # MEM_PAGE_A/B must select the block's page before any 0x80+ write -- writes
        # land only in enabled pages.
        for target in TX_TARGETS:
            chip_words = [profile_words(chip_delays(profile)[target]) for profile in profiles]
            for block in range(16):
                page_mask = 1 << block
                self._write_wait(target, TX_MEM_PAGE_A_REG, page_mask)
                self._write_wait(target, TX_MEM_PAGE_B_REG, page_mask)
                for index, words in enumerate(chip_words):
                    reg = TX_DELAY_PROFILE_REG_BASE + first_word + index * TX_DELAY_PROFILE_WORDS
                    word0, word1 = words[block]
                    self._write_wait(target, reg, word0)
                    self._write_wait(target, reg + 1, word1)
            self._select_chip_profile(target, first_word)

    def verify_delay_profiles(self, profiles, first_word):
        """Read every programmed word on both chips; use a reliable SPI clock."""
        last_reg = TX_DELAY_PROFILE_REG_BASE + first_word + len(profiles) * 2 - 1
        if not profiles or first_word < 0 or last_reg > TX_GUI_MEMORY_CLEAR_LAST:
            raise ValueError("delay profiles exceed TX memory")
        expected = {target: [profile_words(chip_delays(p)[target]) for p in profiles]
                    for target in TX_TARGETS}
        try:
            for target in TX_TARGETS:
                for page in range(16):
                    self._write_wait(target, TX_MEM_PAGE_A_REG, 1 << page)
                    self._write_wait(target, TX_MEM_PAGE_B_REG, 1 << page)
                    for index, words in enumerate(expected[target]):
                        for offset, value in enumerate(words[page]):
                            reg = TX_DELAY_PROFILE_REG_BASE + first_word + 2 * index + offset
                            actual = self._bus.tx_read(target, reg)
                            if actual != value:
                                raise RuntimeError(
                                    f"{target} delay profile {index} page {page + 1} "
                                    f"reg {reg:#x}: read {actual:#010x}, expected {value:#010x}")
        finally:
            self._replay(TX_GUI_MEMORY_PAGE_ENABLE_WRITES)
        result = {"profiles_per_chip": len(profiles), "pages_per_chip": 16,
                  "words_checked": 2 * 16 * len(profiles) * 2,
                  "first_word": first_word}
        logging.info("TX delay profiles verified: %s", result)
        return result

    def select_profile(self, word):
        """Point BF_PROF_SEL_1..16 on both chips at a delay profile memory word."""
        for target in TX_TARGETS:
            self._select_chip_profile(target, word)

    def _select_chip_profile(self, target, word):
        value = word | (word << TX_DELAY_BF_PROF_SEL_FIELDS)
        first = TX_DELAY_BF_PROF_SEL_FIRST_REG
        for reg in range(first, first + TX_DELAY_BF_PROF_SEL_REGS):
            self._bus.tx_update(target, reg, TX_DELAY_BF_PROF_SEL_MASK, value)
            self._wait()

    def set_auto_increment(self, enable, end_word=0):
        """Enable/disable hardware delay-profile auto increment up to end_word."""
        value = TX_AUTO_DELAY_PROFILE_ENABLE | (end_word & TX_AUTO_DELAY_PROFILE_END_MASK) if enable else 0
        mask = TX_AUTO_DELAY_PROFILE_ENABLE | TX_AUTO_DELAY_PROFILE_END_MASK
        logging.info("%s TX auto delay profile increment: end_word=%d reg0x15=%#010x",
                     "Enabling" if enable else "Disabling", end_word, value)
        for target in TX_TARGETS:
            self._bus.tx_update(target, TX_AUTO_DELAY_PROFILE_REG, mask, value)
            self._wait()

    def set_trsw_select(self, rx_select):
        """Per-shot odd/even T/R flip: reg 0x1A only; caller settles MUX_SETTLE_S."""
        self._bus.tx_write("broadcast", TX_TRSW_SEL_REG, TX_PROBE_RX_SELECT_VALUES[rx_select])

    def configure_probe(self, rx_select, trsw_delay_cycles, global_mode):
        """Full probe-mode T/R switch setup with dynamic TRSW and normal capture."""
        trsw_ctrl = TX_TRSW_EN_AUTO_DIS_RX | (TX_TRSW_GBL_MODE if global_mode else 0)
        delay = trsw_delay_cycles
        trsw_delay = delay | (delay << 8) | (delay << 16) | (delay << 24)
        logging.info("Programming TX probe mode: rx_select=%s trsw_delay_word=%#010x trsw_ctrl=%#010x",
                     rx_select, trsw_delay, trsw_ctrl)
        self._replay((
            TX_LOOPBACK_PATTERN_WRITES[0],
            ("broadcast", TX_TRSW_EN_EVEN_REG, 0x00000000),
            ("broadcast", TX_TRSW_EN_ODD_REG, 0x00000000),
            ("broadcast", TX_TRSW_DIS_EVEN_REG, 0x00000000),
            ("broadcast", TX_TRSW_DIS_ODD_REG, 0x00000000),
            ("broadcast", TX_TRSW_SEL_REG, TX_PROBE_RX_SELECT_VALUES[rx_select]),
            ("broadcast", TX_TRSW_CTRL_REG, trsw_ctrl),
            ("broadcast", TX_PDN_PUL_EVEN_REG, 0x00000000),
            ("broadcast", TX_PDN_PUL_ODD_REG, 0x00000000),
            # TRSW delay words are memory regs (0x80+): enable every page so
            # they land regardless of what load_delay_profiles left selected.
            *TX_GUI_MEMORY_PAGE_ENABLE_WRITES,
            ("broadcast", TX_TRSW_DELAY_WORD0_REG, trsw_delay),
            ("broadcast", TX_TRSW_DELAY_WORD1_REG, trsw_delay),
        ))

    def configure_loopback(self, waveform, atten_code):
        """Eval-board loopback route with a cw or pattern waveform source."""
        logging.info("Programming TX loopback mode: waveform=%s atten_code=%d", waveform, atten_code)
        waveform_writes = TX_LOOPBACK_CW_WRITES if waveform == "cw" else TX_LOOPBACK_PATTERN_WRITES
        self._replay((*waveform_writes, *TX_LOOPBACK_ROUTE_WRITES))
        self._write_wait("broadcast", TX_LOOPBACK_FIXED_TGC_REG,
                         TX_LOOPBACK_FIXED_TGC_ENABLE | atten_code)
        for target in TX_TARGETS:
            self._bus.tx_update(target, TX_LOOPBACK_MODE_REG,
                                TX_LOOPBACK_MODE_RSOURCE_MASK, TX_LOOPBACK_MODE_RSOURCE_100_OHM)
            self._wait()

    def _replay(self, writes):
        for target, reg, value in writes:
            self._write_wait(target, reg, value)

    def _write_wait(self, target, reg, value):
        self._bus.tx_write(target, reg, value)
        self._wait()

    def _wait(self):
        time.sleep(self._config_wait_s)

def setup_plane_wave_sweep(tx_device, angle_count, angle_max_deg, c=TX_PW_SOUND_SPEED_MPS):
    """Load a steered plane-wave fan and arm auto-increment; returns angles [rad].

    Wrap rule: the pointer fires one word PAST
    the wrap threshold before returning to the select, so end at the
    second-to-last profile - the overshoot shot then fires the last angle and
    the cycle is exactly angle_count shots.
    """
    degs = plane_wave_angles(angle_count, angle_max_deg)
    tx_device.load_delay_profiles(
        [plane_wave_delays(math.radians(a), c=c) for a in degs],
        TX_PW_DELAY_START_WORD_DEFAULT)
    tx_device.set_auto_increment(
        True, TX_PW_DELAY_START_WORD_DEFAULT + 2 * len(degs) - 3)
    return [math.radians(a) for a in degs]
