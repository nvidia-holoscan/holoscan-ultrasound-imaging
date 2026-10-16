# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""NVUS FPGA blocks: LVDS aligner groups + assembler, capture_ctrl, sync_ctrl."""

import logging
import time

# Mailbox-enabled FPGA images place the LVDS APB block at 0x80000000.
TOP = 0x8000_0000
AFE1_GRP = TOP + 0x0100
AFE2_GRP = TOP + 0x0200
MIXED_GRP = TOP + 0x0300
SYNC_CTRL = 0x4000_0000
CAPTURE_CTRL = 0x5000_0000
TX_UPDATE_CTRL = 0x0120_0000

SYNC_START_SEQUENCE = 0x1
SYNC_CFG = 0x04
SYNC_WAKEUP_CYCLES = 0x08
SYNC_TX_SYNC_DELAY = 0x0C
SYNC_TX_SYNC_PULSE = 0x10
SYNC_RX_SYNC_DELAY = 0x14
SYNC_RX_SYNC_PULSE = 0x18
SYNC_CAPTURE_START_DELAY = 0x1C
SYNC_EVENT_MASK = 0x20
SYNC_SEQUENCE_COUNT = 0x24
SYNC_SEQUENCE_INTERVAL = 0x28

SYNC_EVENT_RX_SYNC = 0x01
SYNC_EVENT_WAKEUP = 0x02
SYNC_EVENT_TX1_SYNC = 0x04
SYNC_EVENT_TX2_SYNC = 0x08
SYNC_EVENT_CAPTURE = 0x10

SYNC_CFG_RX_SYNC_INT_ENABLE = 0x01
SYNC_CFG_RX_SYNC_EXT_ENABLE = 0x02
SYNC_CFG_WAKEUP_ENABLE = 0x04
SYNC_CFG_TX1_SYNC_ENABLE = 0x08
SYNC_CFG_TX2_SYNC_ENABLE = 0x10
SYNC_CFG_RX_SYNC_ENABLE = 0x20
SYNC_CFG_CAPTURE_START_ENABLE = 0x40
SYNC_CFG_TX_UPDATE_SAFETY_ENABLE = 0x80
SYNC_CFG_TX_UPDATE_SEQUENCE_ENABLE = 0x100
# Aligns the sequence launch (TX fire + capture start) to the 50 MHz AFE sample
# phase. Without it the launch lands in either of two 100 MHz record slots per
# shot: a random +/-10 ns (half-sample, 18 deg at 5 MHz) time-origin error that
# re-randomizes the compound's sidelobes every frame.
SYNC_CFG_SYNC_PHASE_ALIGN_ENABLE = 0x200

CAPTURE_CMD = 0x00
CAPTURE_CFG = 0x04
CAPTURE_DELAY_RECORDS = 0x08
CAPTURE_LENGTH_RECORDS = 0x0C
CAPTURE_STATUS = 0x80
CAPTURE_ACCEPTED = 0x84
CAPTURE_COUNT = 0x88
CAPTURE_LAST_CAPTURE = 0x8C

CAPTURE_CMD_START = 0x1
CAPTURE_CMD_ABORT = 0x2
CAPTURE_CMD_CLEAR = 0x4
CAPTURE_CFG_ENABLE = 0x1
CAPTURE_CFG_TLAST_ENABLE = 0x2

# UUID advertised by the ultrasound FPGA image.
NVUS_FPGA_UUID = "ac192bb6-24be-c23f-d07e-2e130d9d2f0f"

GROUPS = (
    ("afe1", AFE1_GRP, 0x7FFF),
    ("afe2", AFE2_GRP, 0x07FF),
    ("mixed", MIXED_GRP, 0x003F),
)

# Aligner control bits; names inferred; confirm against RTL.
TOP_RESET = 0x2
GRP_ENABLE = 0x1
GRP_START = 0x2
GRP_RESET = 0x4


def ready_reads():
    """Scripted Trace reads that make every group look ready and locked (dry-run Evm)."""
    reads = {}
    for _, base, mask in GROUPS:
        reads[base + 0x84] = 0x16_0000  # reset=0, pll+dpa locked, busy
        reads[base + 0x88] = mask
        reads[base + 0x8C] = 0
    return reads


class Fpga:
    """LVDS aligners/assembler, capture programmer, and sync sequencer over a Bus."""

    def __init__(self, bus):
        self._bus = bus

    # --- aligners / assembler ---

    def clear_groups(self):
        self._bus.write32(TOP, TOP_RESET)
        for _, base, _ in GROUPS:
            self._bus.write32(base, GRP_RESET)
        time.sleep(0.05)  # hold aligner reset 50 ms for DPA re-lock
        self._bus.write32(TOP, 0x0000_0000)
        for _, base, _ in GROUPS:
            self._bus.write32(base, 0x0000_0000)

    def clear_assembler(self):
        self._bus.write32(TOP, TOP_RESET)
        time.sleep(0.02)  # hold assembler reset 20 ms to flush in-flight records
        self._bus.write32(TOP, 0x0000_0000)
        time.sleep(0.02)  # let the assembler settle after reset release

    def configure_groups(self, pattern_a, pattern_b):
        for _, base, mask in GROUPS:
            self._bus.write32(base + 0x04, mask)
            self._bus.write32(base + 0x08, pattern_a)
            self._bus.write32(base + 0x0C, pattern_b)

    def start_aligners(self, wait_s):
        for _, base, _ in GROUPS:
            self._bus.write32(base, GRP_RESET | GRP_START | GRP_ENABLE)
        time.sleep(0.02)  # hold group reset 20 ms before releasing into start
        for _, base, _ in GROUPS:
            self._bus.write32(base, GRP_START | GRP_ENABLE)
        time.sleep(wait_s)

    def frontend_ready(self):
        ok = True
        for name, base, _ in GROUPS:
            summary = self._bus.read32(base + 0x84)
            reset, pll_locked, dpa_locked, busy = (
                bool(summary & (1 << b)) for b in (16, 17, 18, 20))
            group_ok = (not reset) and pll_locked and dpa_locked and busy
            ok &= group_ok
            logging.info(
                "%s pre-sync: summary=%#x reset=%d pll=%d dpa=%d busy=%d %s",
                name, summary, int(reset), int(pll_locked), int(dpa_locked),
                int(busy), "OK" if group_ok else "BAD",
            )
        return ok

    def aligners_locked(self):
        ok = True
        for name, base, lock_mask in GROUPS:
            summary = self._bus.read32(base + 0x84)
            locked = self._bus.read32(base + 0x88)
            timeout = self._bus.read32(base + 0x8C)
            valid = self._bus.read32(base + 0x90)
            hold = self._bus.read32(base + 0x94)
            group_ok = (locked == lock_mask) and (timeout == 0)
            ok &= group_ok
            logging.info(
                "%s: summary=%#x locked=%#x expected=%#x timeout=%#x valid=%#x hold=%#x %s",
                name, summary, locked, lock_mask, timeout, valid, hold,
                "OK" if group_ok else "BAD",
            )
        status = [self._bus.read32(TOP + offset) for offset in (0x90, 0x94, 0x98, 0x9C)]
        logging.info("assembler: status0=%#x status1=%#x status2=%#x status3=%#x", *status)
        return ok

    # --- capture ctrl ---

    def program_capture(self, length_records, delay_records, wait_s):
        if length_records > 0xFFFF_FFFF:
            raise ValueError(f"length_records exceeds 32-bit hardware limit: {length_records}")
        logging.info(
            "capture_ctrl: delay_records=%d length_records=%d", delay_records, length_records
        )
        self._bus.write32(CAPTURE_CTRL + CAPTURE_CMD, CAPTURE_CMD_ABORT)
        self._bus.write32(CAPTURE_CTRL + CAPTURE_CMD, CAPTURE_CMD_CLEAR)
        self._bus.write32(
            CAPTURE_CTRL + CAPTURE_CFG, CAPTURE_CFG_ENABLE | CAPTURE_CFG_TLAST_ENABLE
        )
        self._bus.write32(CAPTURE_CTRL + CAPTURE_DELAY_RECORDS, delay_records)
        self._bus.write32(CAPTURE_CTRL + CAPTURE_LENGTH_RECORDS, length_records)
        time.sleep(wait_s)

    def start_capture(self):
        self._bus.write32(CAPTURE_CTRL + CAPTURE_CMD, CAPTURE_CMD_START)

    def read_capture_status(self):
        """Read-only capture_ctrl status: distinguishes "capture never accepted"
        from "receiver missed it"."""
        status = self._bus.read32(CAPTURE_CTRL + CAPTURE_STATUS)
        accepted = self._bus.read32(CAPTURE_CTRL + CAPTURE_ACCEPTED)
        capture_count = self._bus.read32(CAPTURE_CTRL + CAPTURE_COUNT)
        last_capture = self._bus.read32(CAPTURE_CTRL + CAPTURE_LAST_CAPTURE)
        logging.info(
            "capture_ctrl: status=%#x accepted=%d capture_count=%d last_capture=%d",
            status, accepted, capture_count, last_capture,
        )
        return {"status": status, "accepted": accepted,
                "capture_count": capture_count, "last_capture": last_capture}

    # --- sync ctrl ---
    # program_sync_capture/program_sync_alignment deliberately leave the other mode's
    # delay/pulse regs stale; they are gated off by SYNC_CFG/EVENT_MASK.

    def program_sync_alignment(self):
        self._bus.write32(SYNC_CTRL, 0)
        self._bus.write32(
            SYNC_CTRL + SYNC_CFG,
            SYNC_CFG_RX_SYNC_INT_ENABLE
            | SYNC_CFG_RX_SYNC_EXT_ENABLE
            | SYNC_CFG_RX_SYNC_ENABLE,
        )
        self._bus.write32(SYNC_CTRL + SYNC_RX_SYNC_DELAY, 0)
        self._bus.write32(SYNC_CTRL + SYNC_RX_SYNC_PULSE, 20)
        self._bus.write32(SYNC_CTRL + SYNC_EVENT_MASK, SYNC_EVENT_RX_SYNC)
        self._bus.write32(SYNC_CTRL + SYNC_SEQUENCE_COUNT, 1)
        self._bus.write32(SYNC_CTRL + SYNC_SEQUENCE_INTERVAL, 0)

    def set_tx_update_sequence_enable(self, enable=True):
        cfg = self._bus.read32(SYNC_CTRL + SYNC_CFG)
        if enable:
            cfg |= SYNC_CFG_TX_UPDATE_SEQUENCE_ENABLE
        else:
            cfg &= ~SYNC_CFG_TX_UPDATE_SEQUENCE_ENABLE
        self._bus.write32(SYNC_CTRL + SYNC_CFG, cfg)

    def program_sync_capture(self, wakeup, tx_delay, tx_pulse, capture_delay, count, interval,
                             quiet=False, tx_sync_enable=True, tx_update_safety_enable=False,
                             tx_update_sequence_enable=False, sync_phase_align_enable=True,
                             rx_bf_sync_with_tx=False):
        # quiet=True (per-shot re-fires) demotes the log to DEBUG so live runs don't spam.
        logging.log(
            logging.DEBUG if quiet else logging.INFO,
            "sync_ctrl capture: wakeup_cycles=%d tx_delay=%d tx_pulse=%d "
            "capture_delay=%d sequence_count=%d sequence_interval=%d "
            "tx_sync_enable=%d tx_update_safety_enable=%d "
            "tx_update_sequence_enable=%d sync_phase_align_enable=%d rx_bf_sync_with_tx=%d",
            wakeup, tx_delay, tx_pulse, capture_delay, count, interval,
            int(tx_sync_enable), int(tx_update_safety_enable),
            int(tx_update_sequence_enable), int(sync_phase_align_enable),
            int(rx_bf_sync_with_tx),
        )
        cfg = SYNC_CFG_WAKEUP_ENABLE | SYNC_CFG_CAPTURE_START_ENABLE
        event_mask = SYNC_EVENT_WAKEUP | SYNC_EVENT_CAPTURE
        if tx_sync_enable:
            cfg |= SYNC_CFG_TX1_SYNC_ENABLE | SYNC_CFG_TX2_SYNC_ENABLE
            event_mask |= SYNC_EVENT_TX1_SYNC | SYNC_EVENT_TX2_SYNC
        if rx_bf_sync_with_tx:
            cfg |= SYNC_CFG_RX_SYNC_EXT_ENABLE | SYNC_CFG_RX_SYNC_ENABLE
            event_mask |= SYNC_EVENT_RX_SYNC
        if tx_update_safety_enable:
            cfg |= SYNC_CFG_TX_UPDATE_SAFETY_ENABLE
        if tx_update_sequence_enable:
            cfg |= SYNC_CFG_TX_UPDATE_SEQUENCE_ENABLE
        if sync_phase_align_enable:
            cfg |= SYNC_CFG_SYNC_PHASE_ALIGN_ENABLE
        self._bus.write32(SYNC_CTRL, 0)
        self._bus.write32(SYNC_CTRL + SYNC_CFG, cfg)
        self._bus.write32(SYNC_CTRL + SYNC_WAKEUP_CYCLES, wakeup)
        self._bus.write32(SYNC_CTRL + SYNC_TX_SYNC_DELAY, tx_delay)
        self._bus.write32(SYNC_CTRL + SYNC_TX_SYNC_PULSE, tx_pulse)
        # Match the external AFE sync pulse used by the rig's Hololink example.
        self._bus.write32(SYNC_CTRL + SYNC_RX_SYNC_DELAY, tx_delay)
        self._bus.write32(SYNC_CTRL + SYNC_RX_SYNC_PULSE, 20 if rx_bf_sync_with_tx else 0)
        self._bus.write32(SYNC_CTRL + SYNC_CAPTURE_START_DELAY, capture_delay)
        self._bus.write32(SYNC_CTRL + SYNC_EVENT_MASK, event_mask)
        self._bus.write32(SYNC_CTRL + SYNC_SEQUENCE_COUNT, count)
        self._bus.write32(SYNC_CTRL + SYNC_SEQUENCE_INTERVAL, interval)

    def pulse_sync_start(self):
        self._bus.write32(SYNC_CTRL, SYNC_START_SEQUENCE)
        self._bus.write32(SYNC_CTRL, 0)
