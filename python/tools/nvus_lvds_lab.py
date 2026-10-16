# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Bench CLI for poking the NVUS board: GPIO controls, AFE/TX SPI, test patterns."""

import argparse
import logging
import pathlib
import sys

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
# python/tools/hololink.py must not shadow the real hololink package.
sys.path = [p for p in sys.path if pathlib.Path(p or ".").resolve() != SCRIPT_DIR]

import hololink as hololink_module  # noqa: E402

sys.path.insert(0, str(SCRIPT_DIR.parent))

from nvus.afe import TEST_PAT_MODE, TEST_PAT_PAIRS, Afe  # noqa: E402
from nvus.bus import (  # noqa: E402
    RX_MUX_VALUES, TX_MUX_VALUES, TX_SPI_DEFAULT_PRESCALER, TX_SPI_PATH_HSB,
    TX_SPI_PATHS, Bus,
)
from nvus.fpga import Fpga  # noqa: E402


def parse_int(text):
    return int(text, 0)


def parse_tx_register(text):
    value = parse_int(text)
    if not 0 <= value <= 0x3FF:
        raise argparse.ArgumentTypeError(f"{text} is outside TX register range 0..1023")
    return value


def parse_u32(text):
    value = parse_int(text)
    if not 0 <= value <= 0xFFFF_FFFF:
        raise argparse.ArgumentTypeError(f"{text} is outside 32-bit unsigned range")
    return value


def parse_tx_expectation(text):
    try:
        reg_text, expected_text = text.split("=", 1)
        if "/" in expected_text:
            expected_text, mask_text = expected_text.split("/", 1)
            mask = parse_u32(mask_text)
        else:
            mask = 0xFFFF_FFFF
        return parse_tx_register(reg_text), parse_u32(expected_text), mask
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not REG=VALUE or REG=VALUE/MASK"
        ) from exc


def expand_tx_targets(target):
    if target == "both":
        return ("tx1", "tx2")
    return (target,)


class TxWakeupTrap(argparse.Action):
    # tx_wakeup lives in sync_ctrl (0x4000_0000), not the board-control register.
    def __call__(self, parser, namespace, values, option_string=None):
        parser.error("tx_wakeup is controlled by sync_ctrl, not board control")


def run_gpio_init(bus, args):
    bus.gpio_init()
    bus.show_gpio()


def run_control(bus, args):
    bus.set_control(rx=args.rx_mux, tx=args.tx_mux, pdn_standby=args.pdn_standby,
                    crc_en=args.crc_en, reset=args.reset, rx_spi_lvds_en=args.rx_spi_lvds_en)
    bus.show_gpio()


def run_afe_write(bus, args):
    Afe(bus).write(args.target, args.register, args.value)
    print(f"{args.target} afe reg 0x{args.register:02x} <= 0x{args.value:04x}")


def run_afe_read(bus, args):
    value = Afe(bus).read(args.target, args.register)
    print(f"{args.target} afe reg 0x{args.register:02x} => 0x{value:04x}")


def run_afe_pattern(bus, args):
    Afe(bus).program_pattern(args.target, args.mode, args.value, args.even_value, args.pairs)
    print(f"{args.target} afe pattern {args.mode}: {args.pairs} register pairs written")


def run_tx_write(bus, args):
    bus.tx_write(args.target, args.register, args.value)
    print(f"{args.target} tx reg 0x{args.register:03x} <= 0x{args.value:08x}")


def run_tx_read(bus, args):
    value = bus.tx_read(args.target, args.register)
    print(f"{args.target} tx reg 0x{args.register:03x} => 0x{value:08x}")


def run_tx_read_many(bus, args):
    for target in expand_tx_targets(args.target):
        for register in args.registers:
            value = bus.tx_read(target, register)
            print(f"{target} tx reg 0x{register:03x} => 0x{value:08x}")


def run_tx_verify(bus, args):
    failures = 0
    for target in expand_tx_targets(args.target):
        for register, expected, mask in args.expectations:
            value = bus.tx_read(target, register)
            ok = (value & mask) == (expected & mask)
            result = "PASS" if ok else "FAIL"
            print(f"{result} {target} tx reg 0x{register:03x} => 0x{value:08x}; "
                  f"expected 0x{expected:08x} mask 0x{mask:08x}")
            failures += 0 if ok else 1
    if failures:
        raise SystemExit(failures)


def run_tx_write_verify(bus, args):
    failures = 0
    for target in expand_tx_targets(args.target):
        for register, expected, mask in args.expectations:
            old = bus.tx_read(target, register)
            bus.tx_write(target, register, expected)
            value = bus.tx_read(target, register)
            ok = (value & mask) == (expected & mask)
            result = "PASS" if ok else "FAIL"
            print(f"{result} {target} tx reg 0x{register:03x} <= 0x{expected:08x}, "
                  f"read 0x{value:08x}, mask 0x{mask:08x}; old 0x{old:08x}")
            failures += 0 if ok else 1
            if not args.no_restore:
                bus.tx_write(target, register, old)
                restored = bus.tx_read(target, register)
                restore_ok = restored == old
                restore_result = "PASS" if restore_ok else "FAIL"
                print(f"{restore_result} {target} tx reg 0x{register:03x} restored 0x{restored:08x}")
                failures += 0 if restore_ok else 1
    if failures:
        raise SystemExit(failures)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hololink", default="192.168.0.2")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--spi-delay-s", type=float, default=0.0,
                        help="Delay after AFE/TX SPI operations, in seconds.")
    parser.add_argument("--tx-spi-path", choices=TX_SPI_PATHS, default=TX_SPI_PATH_HSB,
                        help="TX register transport: hsb original HSB SPI, tx-hs new TX HS SPI slow path.")
    parser.add_argument("--tx-spi-prescaler", type=parse_int, default=TX_SPI_DEFAULT_PRESCALER,
                        help="TX SPI prescaler-compatible setting; tx-hs programs half_period_cycles=prescaler+1.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sp = subparsers.add_parser("gpio-init")
    sp.set_defaults(handler=run_gpio_init)

    sp = subparsers.add_parser("gpio-show")
    sp.set_defaults(handler=lambda bus, args: bus.show_gpio())

    sp = subparsers.add_parser("capture-status")
    sp.set_defaults(handler=lambda bus, args: Fpga(bus).read_capture_status())

    sp = subparsers.add_parser("control")
    sp.add_argument("--rx-mux", choices=tuple(RX_MUX_VALUES), default=None)
    sp.add_argument("--tx-mux", choices=tuple(TX_MUX_VALUES), default=None)
    sp.add_argument("--pdn-standby", type=parse_int, choices=(0, 1), default=None)
    sp.add_argument("--crc-en", type=parse_int, choices=(0, 1), default=None)
    sp.add_argument("--reset", type=parse_int, choices=(0, 1), default=None)
    sp.add_argument("--tx-wakeup", nargs="?", action=TxWakeupTrap,
                    help="rejected: tx_wakeup is controlled by sync_ctrl, not board control")
    sp.add_argument("--rx-spi-lvds-en", type=parse_int, choices=(0, 1), default=None)
    sp.set_defaults(handler=run_control)

    sp = subparsers.add_parser("afe-write")
    sp.add_argument("target", choices=("rx1", "rx2", "broadcast"))
    sp.add_argument("register", type=parse_int)
    sp.add_argument("value", type=parse_int)
    sp.set_defaults(handler=run_afe_write)

    sp = subparsers.add_parser("afe-read")
    sp.add_argument("target", choices=("rx1", "rx2", "broadcast"))
    sp.add_argument("register", type=parse_int)
    sp.set_defaults(handler=run_afe_read)

    sp = subparsers.add_parser("afe-pattern")
    sp.add_argument("target", choices=("rx1", "rx2", "broadcast"))
    sp.add_argument("mode", choices=tuple(TEST_PAT_MODE))
    sp.add_argument("--value", type=parse_int, default=0)
    sp.add_argument("--even-value", type=parse_int, default=0)
    sp.add_argument("--pairs", type=int, default=TEST_PAT_PAIRS)
    sp.set_defaults(handler=run_afe_pattern)

    sp = subparsers.add_parser("tx-write")
    sp.add_argument("target", choices=("tx1", "tx2", "broadcast"))
    sp.add_argument("register", type=parse_tx_register)
    sp.add_argument("value", type=parse_u32)
    sp.set_defaults(handler=run_tx_write)

    sp = subparsers.add_parser("tx-read")
    sp.add_argument("target", choices=("tx1", "tx2"))
    sp.add_argument("register", type=parse_tx_register)
    sp.set_defaults(handler=run_tx_read)

    sp = subparsers.add_parser("tx-read-many")
    sp.add_argument("target", choices=("tx1", "tx2", "both"))
    sp.add_argument("registers", nargs="+", type=parse_tx_register)
    sp.set_defaults(handler=run_tx_read_many)

    sp = subparsers.add_parser("tx-verify")
    sp.add_argument("target", choices=("tx1", "tx2", "both"))
    sp.add_argument("expectations", nargs="+", type=parse_tx_expectation,
                    help="Expected reads as REG=VALUE or REG=VALUE/MASK.")
    sp.set_defaults(handler=run_tx_verify)

    sp = subparsers.add_parser("tx-write-verify")
    sp.add_argument("target", choices=("tx1", "tx2", "both"))
    sp.add_argument("expectations", nargs="+", type=parse_tx_expectation,
                    help="Write/read checks as REG=VALUE or REG=VALUE/MASK. Values are restored by default.")
    sp.add_argument("--no-restore", action="store_true",
                    help="Leave written values in place instead of restoring original register values.")
    sp.set_defaults(handler=run_tx_write_verify)

    return parser


def main():
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)
    channel_metadata = hololink_module.Enumerator.find_channel(channel_ip=args.hololink)
    hololink = hololink_module.DataChannel(channel_metadata).hololink()
    hololink.start()
    try:
        args.handler(Bus(hololink, spi_delay_s=args.spi_delay_s,
                         tx_spi_prescaler=args.tx_spi_prescaler,
                         tx_spi_path=args.tx_spi_path), args)
    finally:
        hololink.stop()


if __name__ == "__main__":
    main()
