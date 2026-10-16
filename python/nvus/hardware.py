# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Idle register diagnostics; no reset, arm, or transmit operations."""

from contextlib import contextmanager, ExitStack
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import time

from . import fpga
from .afe import LPF_REG
from .bus import (Bus, BOARD_CTRL_REG, BOARD_STATUS, BOARD_LVDS_STATUS,
                  TX_HS_SPI_CFG, TX_HS_SPI_STATUS, TX_HS_SPI_STATUS_BUSY,
                  TX_SPI_DEFAULT_PRESCALER)
from .tx import TX_LOOPBACK_FIXED_TGC_REG

# Explicit access list: unknown/clear-on-read/trigger registers stay inaccessible.
REGISTERS = {
    "fpga": {fpga.SYNC_CTRL + fpga.SYNC_CFG: False,
             fpga.CAPTURE_CTRL + fpga.CAPTURE_DELAY_RECORDS: True,
             fpga.CAPTURE_CTRL + fpga.CAPTURE_LENGTH_RECORDS: False,
             fpga.CAPTURE_CTRL + fpga.CAPTURE_STATUS: False,
             BOARD_CTRL_REG: False, BOARD_STATUS: False, BOARD_LVDS_STATUS: False},
    "rx1": {LPF_REG: True}, "rx2": {LPF_REG: True},
    "tx1": {TX_LOOPBACK_FIXED_TGC_REG: True}, "tx2": {TX_LOOPBACK_FIXED_TGC_REG: True},
}


@contextmanager
def ownership():
    # Shared by this container's CLI and PW4cm. Other hosts/tools need operator exclusion.
    with open("/tmp/nvus-hardware.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Hardware is owned by another nvus operation") from None
        yield


def validate(args):
    args.ip = str(ipaddress.IPv4Address(args.ip))
    registers = REGISTERS[args.device]
    if args.register not in registers:
        raise ValueError("Unsupported register; allowed: " + ", ".join(map(hex, registers)))
    if args.operation == "write":
        if not registers[args.register]:
            raise ValueError("Register is read-only in this CLI")
        limit = 0xffff if args.device.startswith("rx") else 0xffffffff
        if any(not 0 <= value <= limit for value in (args.value, args.expect)):
            raise ValueError(f"Value and --expect must be in 0..{limit:#x}")


@contextmanager
def connect(ip):
    import hololink
    from .evm import enumeration_strategy
    enumeration_strategy()
    metadata = hololink.Enumerator.find_channel(channel_ip=ip)
    control = hololink.DataChannel(metadata).hololink()
    try:
        control.start()
        # Idle diagnostics need reliable readback, not the acquisition write clock.
        yield Bus(control, tx_spi_path="tx-hs", tx_spi_prescaler=TX_SPI_DEFAULT_PRESCALER)
    finally:
        control.stop()


def require_idle(bus, *, allow_rx_sync=False):
    sync = bus.read32(fpga.SYNC_CTRL + fpga.SYNC_CFG)
    ingress = bus.read32(fpga.TX_UPDATE_CTRL)
    spi = bus.read32(TX_HS_SPI_STATUS)
    rx_mask = (fpga.SYNC_CFG_RX_SYNC_INT_ENABLE | fpga.SYNC_CFG_RX_SYNC_EXT_ENABLE
               | fpga.SYNC_CFG_RX_SYNC_ENABLE | fpga.SYNC_CFG_SYNC_PHASE_ALIGN_ENABLE)
    if sync & ~(rx_mask if allow_rx_sync else 0) or ingress or spi & TX_HS_SPI_STATUS_BUSY:
        raise RuntimeError(f"Idle check blocked: SYNC_CFG(0x40000004)={sync:#x}, "
                           f"TX_UPDATE_CTRL(0x01200000)={ingress:#x}, "
                           f"TX_SPI_STATUS(0x70000080)={spi:#x}. "
                           "Nonzero sync configuration does not prove imaging is running; "
                           "inspect configuration before changing hardware.")


@contextmanager
def access(bus, device):
    with ExitStack() as cleanup:
        if device.startswith("rx"):
            cleanup.callback(restore_word, bus, BOARD_CTRL_REG, bus.read32(BOARD_CTRL_REG))
            cleanup.callback(bus.afe_write, device, 0, 0)
            yield (lambda reg: bus.afe_read(device, reg),
                   lambda reg, value: bus.afe_write(device, reg, value))
        elif device.startswith("tx"):
            cleanup.callback(restore_word, bus, TX_HS_SPI_CFG, bus.read32(TX_HS_SPI_CFG))
            cleanup.callback(bus.tx_write, device, 0, 0)
            yield (lambda reg: bus.tx_read(device, reg),
                   lambda reg, value: bus.tx_write(device, reg, value))
        else:
            yield bus.read32, bus.write32


def restore_word(bus, register, value):
    bus.write32(register, value)
    if bus.read32(register) != value:
        raise RuntimeError(f"Restoration failed at {register:#x}; inspect board before imaging")


def exchange(args, read, write, audit):
    before = read(args.register)
    if args.operation == "read":
        return dict(value=before)
    if before != args.expect:
        raise RuntimeError(f"Expected {args.expect:#x}, found {before:#x}; nothing written")
    if args.device.startswith("tx") and ((before ^ args.value) & ~0x3f
            or any(value & 0x3f > 48 for value in (before, args.value))):
        raise ValueError("TX writes require old/new attenuation codes 0..48; only bits [5:0] may change")
    if args.device.startswith("rx") and ((before ^ args.value) & ~3
            or any(value & 3 > 1 for value in (before, args.value))):
        raise ValueError("RX writes require old/new LPF codes 0 or 1; only bits [1:0] may change")
    record = dict(time_ns=time.time_ns(), ip=args.ip, device=args.device,
                  register=args.register, before=before, requested=args.value)
    audit.write(json.dumps(dict(record, status="intent")) + "\n")
    audit.flush()
    os.fsync(audit.fileno())
    write(args.register, args.value)
    actual = read(args.register)
    audit.write(json.dumps(dict(record, status="readback", actual=actual)) + "\n")
    audit.flush()
    if actual != args.value:
        raise RuntimeError(f"Readback mismatch: {actual:#x}; inspect and restore register before imaging")
    return dict(before=before, value=actual, verified=True)


def execute(args):
    validate(args)
    with ownership(), ExitStack() as resources:
        audit = None
        if args.operation == "write":
            path = Path(".runtime/hw-writes.jsonl")
            path.parent.mkdir(exist_ok=True)
            audit = resources.enter_context(path.open("a"))
        bus = resources.enter_context(connect(args.ip))
        # Allowlisted FPGA reads have no read-mode writes or clear-on-read effects.
        if args.device != "fpga" or args.operation != "read":
            require_idle(bus)
        read, write = resources.enter_context(access(bus, args.device))
        return dict(ip=args.ip, device=args.device, register=args.register,
                    **exchange(args, read, write, audit))
