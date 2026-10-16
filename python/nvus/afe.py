# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""AFE5932 RX pair: init write lists and test-pattern programming over a Bus."""

import logging
import time

TEST_PAT_BASE = 0x6F
TEST_PAT_PAIRS = 16
TEST_PAT_MODE = {"off": 0, "dc": 1, "toggle": 2, "ramp": 3}
LPF_REG = 0x92
# Compare only defined configuration fields; other bits need not read back.
AFE_READBACK_MASKS = {0x95: 0x1073}

# Alternate AFE setup retained for A/B testing.
AFE_LEGACY_INIT_WRITES = (
    (0x11, 0x0002),
    (0x28, 0x0001),
    (0x95, 0x0041),
    (0x99, 0x0008),
)

# Raw-mode initialization register values.
AFE_GUI_INIT_WRITES = (
    (0x11, 0x0002),
    (0x28, 0x0001),
    (0x95, 0x0241),
    (0x99, 0x0028),
    (0xE0, 0x0000),
)

AFE_TARGETS = ("rx1", "rx2")


class Afe:
    """The AFE5932 receive pair; SPI pointer clears live in Bus.afe_write."""

    def __init__(self, bus):
        self._bus = bus

    def init(self, target, mode="gui", wait_s=0.02):
        writes = AFE_GUI_INIT_WRITES if mode == "gui" else AFE_LEGACY_INIT_WRITES
        logging.info("Programming %s AFE init for %s", mode, target)
        for reg, value in writes:
            self.write(target, reg, value)
        time.sleep(wait_s)

    def write(self, target, reg, value):
        self._bus.afe_write(target, reg, value)

    def read(self, target, reg):
        return self._bus.afe_read(target, reg)

    def set_pattern(self, mode, value=0, even_value=0, wait_s=0.1):
        for target in AFE_TARGETS:
            self.program_pattern(target, mode, value, even_value)
            time.sleep(wait_s)

    def program_pattern(self, target, mode, value=0, even_value=0, pairs=TEST_PAT_PAIRS):
        odd_word = ((value & 0xFFF) << 4) | TEST_PAT_MODE[mode]
        even_word = (even_value & 0xFFF) << 4
        logging.debug("afe-pattern %s %s value=%#x even=%#x", target, mode, value, even_value)
        for pair in range(pairs):
            odd_reg = TEST_PAT_BASE + pair * 2
            self.write(target, odd_reg, odd_word)
            self.write(target, odd_reg + 1, even_word)
