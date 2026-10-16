# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Evm: the north-star NVUS driver -- init/arm/fire/rx_device over Bus/Afe/Tx/Fpga."""

import ast
import logging
import threading
import time

from . import units
from .afe import Afe
from .bus import TX_SPI_DEFAULT_PRESCALER, Bus
from .fpga import NVUS_FPGA_UUID, Fpga
from .tx import (MUX_SETTLE_S, TX_DELAY_PROFILE_START_WORD_DEFAULT,
                 TX_DUAL_SPI_SDATA0_PAGE_MASK, TX_DUAL_SPI_SDATA1_PAGE_MASK,
                 TX_SAMPLE_PATTERN_START_WORDS, Tx)

CONFIG_DEFAULTS = dict(
    # waits
    reset_hold_s=0.1, post_reset_wait_s=0.5, align_wait_s=0.5, sync_wait_s=0.5,
    tx_config_wait_s=0.02, afe_config_wait_s=0.02, afe_pattern_wait_s=0.1,
    group_wait_s=0.02, capture_config_wait_s=0.02, rx_arm_delay_s=1.0,
    sync_capture_wait_s=0.001, trsw_settle_s=MUX_SETTLE_S, spi_delay_s=0.001,
    # bring-up
    align_retries=1, device_init_mode="legacy", eval_board_mode="none",
    capture_pattern="dc", pattern_a=0x5A3, pattern_b=0x0C6,
    ramp_odd_step=0x001, ramp_even_step=0x002,
    # sync timing
    sync_clk_hz=100e6,
    wakeup_s=200e-6, tx_delay_s=200.1e-6, tx_pulse_s=0.4e-6,
    capture_delay_s=None, interval_s=None, interval_margin_s=100e-6,
    tx_sync_enable=True, tx_update_safety_enable=False,
    tx_update_sequence_enable=False, sync_phase_align_enable=True,
    rx_bf_sync_with_tx=False,
    # capture
    capture_records=None, delay_records=0, start_source="sw", fast_rearm=False,
    # tx
    pattern_start_word=None, delay_profile_word=TX_DELAY_PROFILE_START_WORD_DEFAULT,
    trsw_delay_cycles=1, trsw_global_mode=False, loopback_waveform="cw", loopback_atten=47,
    tx_spi_lanes=1, tx_spi_prescaler=TX_SPI_DEFAULT_PRESCALER, tx_spi_path="hsb",
    tx_spi_page_mask0=TX_DUAL_SPI_SDATA0_PAGE_MASK,
    tx_spi_page_mask1=TX_DUAL_SPI_SDATA1_PAGE_MASK, tx_spi_sdata_term=None,
    # physics
    c=1480.0, fs=50e6,
)

PATTERN_SETTLE_S = 0.05  # let the DC pattern propagate through the deserializers before RX_BF_SYNC


class EvmConfig:
    """Keyword-overridable attribute bag; defaults match today's validation CLI."""

    def __init__(self, **overrides):
        unknown = sorted(set(overrides) - set(CONFIG_DEFAULTS))
        if unknown:
            raise ValueError(f"unknown EvmConfig fields: {', '.join(unknown)}")
        vars(self).update(CONFIG_DEFAULTS)
        vars(self).update(overrides)


def parse_config_overrides(items):
    """--evm KEY=VALUE strings -> EvmConfig kwargs (literal if it parses, else str)."""
    overrides = {}
    for item in items or ():
        key, sep, text = item.partition("=")
        if not sep or not key:
            raise ValueError(f"expected KEY=VALUE, got {item!r}")
        try:
            overrides[key] = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            overrides[key] = text
    return overrides


class Evm:
    """One TI EVM behind a hololink handle or a Trace; the user-sketch driver."""

    def __init__(self, hololink, config=None):
        self._config = _apply_mode_overrides(config if config is not None else EvmConfig())
        self.bus = Bus(hololink, spi_delay_s=self._config.spi_delay_s,
                       tx_spi_prescaler=self._config.tx_spi_prescaler,
                       tx_spi_path=self._config.tx_spi_path)
        self.afe = Afe(self.bus)
        self.tx = Tx(self.bus, config_wait_s=self._config.tx_config_wait_s,
                     spi_lanes=self._config.tx_spi_lanes,
                     page_mask0=self._config.tx_spi_page_mask0,
                     page_mask1=self._config.tx_spi_page_mask1,
                     sdata_term=self._config.tx_spi_sdata_term)
        self.fpga = Fpga(self.bus)
        self.samples = self.records = self.frame_bytes = None
        self._pri = None
        self._shots = 1
        self._rx_select = "odd"
        self._fast_rearm_ready = False

    def init(self, pulse=None, delays=None, *, depth=None, time=None, samples=None,
             pri=None, rx_select="odd", repeat_shots=1, retries=None):
        """Resolve frame geometry, then run the full LVDS bring-up with retries."""
        config = self._config
        # capture 10% past the nominal round trip: sound-speed mismatch, the
        # transmit onset offset, and echo tails from targets at full depth
        if depth is not None:
            depth = depth * 1.1
        self.samples = units.as_samples(depth=depth, time=time, samples=samples,
                                        c=config.c, fs=config.fs)
        # capture length must cover whole 192-byte-aligned frames, or the FPGA
        # streams fewer bytes than the receiver and operator were sized for
        self.frame_bytes = units.records_to_frame_bytes(units.samples_to_records(self.samples))
        self.records = self.frame_bytes // units.RECORD_BYTES
        self._pri = pri
        self._shots = repeat_shots
        # Interleave starts on the odd half; the operator flips per shot.
        self._rx_select = "odd" if rx_select == "interleave" else rx_select
        retries = config.align_retries if retries is None else retries
        for attempt in range(1, retries + 2):
            logging.info("LVDS pre-receiver bring-up attempt %d", attempt)
            if self._prepare_once(pulse, delays):
                logging.info("LVDS pre-receiver bring-up passed")
                return
            logging.warning("LVDS pre-receiver bring-up failed")
        raise RuntimeError("LVDS pre-receiver bring-up failed after retries")

    def _prepare_once(self, pulse, delays):
        # Steps map 1:1 onto the old prepare_once; the order is hardware-critical.
        self._fast_rearm_ready = False
        config = self._config
        self._reset_afes()
        self._init_tx(pulse, delays)
        self.fpga.clear_groups()
        self.fpga.program_sync_alignment()
        self._init_afes()
        self._dc_align()
        self.fpga.configure_groups(config.pattern_a, config.pattern_b)
        time.sleep(config.group_wait_s)
        self.fpga.start_aligners(config.align_wait_s)
        if not self.fpga.frontend_ready():
            return False
        mode = config.eval_board_mode
        if mode not in ("loopback", "probe"):
            return self._set_capture_pattern()
        logging.info("%s mode: aligning LVDS with AFE DC pattern before raw capture", mode)
        time.sleep(PATTERN_SETTLE_S)
        self.fire_rx_bf_sync()
        if not self.fpga.aligners_locked():
            return False
        self._enter_probe_or_loopback()
        self._normal_pattern()
        return True

    # --- bring-up steps, order preserved verbatim ---

    def _reset_afes(self):
        self.bus.gpio_init()
        self.bus.set_control(rx="rx1", rx_spi_lvds_en=0, pdn_standby=0, reset=1)
        time.sleep(self._config.reset_hold_s)
        self.bus.set_control(rx="rx1", rx_spi_lvds_en=0, pdn_standby=0, reset=0)
        time.sleep(self._config.post_reset_wait_s)

    def _init_tx(self, pulse, delays):
        config = self._config
        if config.device_init_mode != "gui":
            if pulse is not None or delays is not None:
                logging.warning("device_init_mode=%r ignores pulse/delays; use "
                                "EvmConfig(eval_board_mode='probe') or device_init_mode='gui'",
                                config.device_init_mode)
            return
        self.tx.init()
        if config.eval_board_mode == "loopback" and config.loopback_waveform == "cw":
            # CW uses the register waveform generator, not pattern memory.
            self.tx.clear_memory()
            self.tx.set_auto_increment(False)
            return
        self.tx.load_pattern(self._pattern_start_word(pulse))
        if delays is not None:
            self.tx.load_delay_profiles([delays], config.delay_profile_word)
        self.tx.set_auto_increment(False)

    def _pattern_start_word(self, pulse):
        if pulse is None:
            return self._config.pattern_start_word
        return TX_SAMPLE_PATTERN_START_WORDS[pulse]

    def _init_afes(self):
        for target in ("rx1", "rx2"):
            self.afe.init(target, self._config.device_init_mode, self._config.afe_config_wait_s)

    def _dc_align(self):
        # DC pattern must be programmed before the aligners start.
        self.afe.set_pattern("dc", self._config.pattern_a, self._config.pattern_b,
                             self._config.afe_pattern_wait_s)

    def _enter_probe_or_loopback(self):
        config = self._config
        if config.eval_board_mode == "loopback":
            self.tx.configure_loopback(config.loopback_waveform, config.loopback_atten)
        else:
            self.tx.configure_probe(self._rx_select, config.trsw_delay_cycles,
                                    config.trsw_global_mode)

    def _normal_pattern(self):
        # Normal/off pattern LAST, after probe/loopback config.
        logging.info("Setting AFE test mode to normal/off")
        self.afe.set_pattern("off", wait_s=self._config.afe_pattern_wait_s)

    def _set_capture_pattern(self):
        config = self._config
        if config.capture_pattern == "ramp":
            self.afe.set_pattern("ramp", config.ramp_odd_step, config.ramp_even_step,
                                 config.afe_pattern_wait_s)
        else:
            logging.info("Leaving AFE pattern in DC mode through RX_BF_SYNC")
        time.sleep(PATTERN_SETTLE_S)
        return True

    # --- shots ---

    def arm(self, shots=1):
        """Program capture_ctrl and the sync sequencer for one capture window."""
        self._program_capture()
        self._program_sync(shots)
        self._shots = shots

    def fire(self, rx_select=None):
        """Fire one shot; optional TRSW flip (with settle) first."""
        config = self._config
        if rx_select is not None:
            self.tx.set_trsw_select(rx_select)
            time.sleep(config.trsw_settle_s)
        # Fast-rearm asymmetry: the first shot programs sync, later shots just pulse.
        if not (config.fast_rearm and self._fast_rearm_ready):
            self._program_sync(self._shots, quiet=True)
            self._fast_rearm_ready = True
        self.fpga.pulse_sync_start()
        time.sleep(config.sync_capture_wait_s)

    def fire_rx_bf_sync(self):
        """Alignment-mode release: RX_BF_SYNC pulse plus its settle wait."""
        self.fpga.pulse_sync_start()
        time.sleep(self._config.sync_wait_s)

    # --- receiver integration (sensor-bridge push model) ---

    def rx_device(self, mode, arm_delay_s=None):
        """Receiver `device=` shim; arms ("arm") or releases ("release") after a delay."""
        callback = {"arm": self._arm_after_receiver,
                    "release": self._release_after_receiver}[mode]
        delay = self._config.rx_arm_delay_s if arm_delay_s is None else arm_delay_s
        return _DelayedStart(callback, delay)

    def _arm_after_receiver(self):
        logging.info("capture_ctrl release")
        self._program_capture()
        self._start_window()

    def _release_after_receiver(self):
        logging.info("LVDS post-receiver sync/assembler release")
        self._program_capture()
        self.fpga.clear_assembler()
        self._start_window()
        self._fast_rearm_ready = False
        self.fpga.program_sync_alignment()
        self.fpga.pulse_sync_start()
        logging.info("LVDS stream released")

    def _start_window(self):
        # Ordering invariant: capture armed BEFORE the sync release.
        if self._config.start_source == "sync":
            self._program_sync(self._shots)
            self.fpga.pulse_sync_start()
            time.sleep(self._config.sync_capture_wait_s)
        else:
            self.fpga.start_capture()

    def _program_capture(self):
        self.fpga.program_capture(self._capture_records(), self._config.delay_records,
                                  self._config.capture_config_wait_s)

    def _program_sync(self, shots, quiet=False):
        config = self._config
        wakeup = self._sync_seconds(config.wakeup_s)
        tx_delay = self._sync_seconds(config.tx_delay_s)
        tx_pulse = self._sync_seconds(config.tx_pulse_s)
        if config.capture_delay_s is None:
            capture_delay = tx_delay + tx_pulse
        else:
            capture_delay = self._sync_seconds(config.capture_delay_s)
        self.fpga.program_sync_capture(
            wakeup, tx_delay, tx_pulse, capture_delay, shots,
            self._interval_cycles(shots, capture_delay),
            quiet=quiet,
            tx_sync_enable=config.tx_sync_enable,
            tx_update_safety_enable=config.tx_update_safety_enable,
            tx_update_sequence_enable=config.tx_update_sequence_enable,
            sync_phase_align_enable=config.sync_phase_align_enable,
            rx_bf_sync_with_tx=config.rx_bf_sync_with_tx,
        )

    def _interval_cycles(self, shots, capture_delay):
        config = self._config
        if config.interval_s is not None:
            return self._sync_seconds(config.interval_s)
        window = (capture_delay + self._capture_records_sync_cycles() +
                  self._sync_seconds(config.interval_margin_s))
        if self._pri is not None:
            return max(self._sync_seconds(self._pri), window)
        if shots <= 1:
            return 0
        # Delivery collapses to a single frame below the capture-window floor
        # (~150-190 us regardless of frame size); 10 ms
        # is the proven-delivering default. An explicit pri= or interval_s=
        # overrides this floor.
        return max(window, self._sync_seconds(10e-3))

    def _sync_seconds(self, seconds):
        return units.seconds_to_cycles(seconds, self._config.sync_clk_hz)

    def _capture_records_sync_cycles(self):
        config = self._config
        capture_s = self._capture_records() / (2.0 * config.fs)
        return self._sync_seconds(capture_s)

    def _capture_records(self):
        if self._config.capture_records is not None:
            return self._config.capture_records
        if self.records is None:
            raise RuntimeError("frame geometry unknown: call init() or set capture_records")
        return self.records


class _DelayedStart:
    """Receiver device shim: run the callback on a daemon thread after a delay."""

    def __init__(self, callback, delay_s):
        self._callback = callback
        self._delay_s = delay_s
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._run, name="lvds-rx-bf-sync", daemon=True).start()

    def _run(self):
        logging.info("Waiting %.3fs after RoCE receiver config before RX_BF_SYNC",
                     self._delay_s)
        self._stop.wait(self._delay_s)
        if self._stop.is_set():
            return
        self._callback()

    def stop(self):
        self._stop.set()


def _apply_mode_overrides(config):
    # Old CLI implications that are hardware-critical, kept in the driver.
    if config.eval_board_mode in ("loopback", "probe"):
        if config.device_init_mode != "gui":
            logging.info("%s mode requires the standard initialization sequence",
                         config.eval_board_mode)
            config.device_init_mode = "gui"
        if config.start_source != "sync":
            logging.info("%s mode needs sync_ctrl sequencing; overriding start_source to sync",
                         config.eval_board_mode)
            config.start_source = "sync"
    return config


def enumeration_strategy():
    """Install the NVUS UUID enumeration strategy; call before find_channel.

    NVUS reuses the Hololink-Lite UUID with one SIF per sensor."""
    import hololink as hololink_module

    strategy = hololink_module.BasicEnumerationStrategy(
        total_sensors=2, total_dataplanes=2, sifs_per_sensor=1)
    hololink_module.Enumerator.set_uuid_strategy(NVUS_FPGA_UUID, strategy)
    logging.info("Installed NVUS enum strategy for UUID %s", NVUS_FPGA_UUID)
    return strategy
