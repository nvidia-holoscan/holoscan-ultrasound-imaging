#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Installed CLI check: change an idle register, verify, and restore. No TX."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from nvus.afe import LPF_REG
from nvus.fpga import CAPTURE_CTRL, CAPTURE_DELAY_RECORDS
from nvus.tx import TX_LOOPBACK_FIXED_TGC_REG

CASES = {"fpga": (CAPTURE_CTRL + CAPTURE_DELAY_RECORDS, 0, 1),
         "rx": (LPF_REG, 0, 1), "tx": (TX_LOOPBACK_FIXED_TGC_REG, 0xef, 0xee)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ip", default="192.168.0.2")
    parser.add_argument("--device", choices=("fpga", "rx1", "rx2", "tx1", "tx2"), default="fpga")
    parser.add_argument("--idle-confirmed", required=True, action="store_true",
                        help="Operator confirms the board is idle and exclusively available")
    args = parser.parse_args()
    cli = str(Path(sys.executable).parent / "nvus")
    register, before, after = CASES[args.device.rstrip("12")]

    def command(operation, *values, check=True):
        result = subprocess.run([cli, "hw", operation, args.device, hex(register),
                                 *map(str, values), "--ip", args.ip, "--format", "json"],
                                check=check, capture_output=True, text=True)
        print(result.stdout, end="")
        return result.returncode, json.loads(result.stdout)

    _, original = command("read")
    if original["value"] != before:
        raise RuntimeError(f"Example requires initial value {before:#x}; nothing written")
    try:
        code, rejected = command("write", after, "--expect", after, check=False)
        assert code == 1 and not rejected["ok"]
        _, unchanged = command("read")
        assert unchanged["value"] == before
        command("write", after, "--expect", before)
        _, changed = command("read")
        assert changed["value"] == after
    finally:
        _, current = command("read")
        command("write", before, "--expect", current["value"])
    _, restored = command("read")
    assert restored["value"] == before
    print(f"PASS: {args.device} stale write refused; register changed, verified, and restored; no transmit")


if __name__ == "__main__":
    main()
