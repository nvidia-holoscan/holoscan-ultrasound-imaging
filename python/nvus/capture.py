# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Finite AFE pattern capture with RX bring-up; never configure TX."""
from contextlib import contextmanager, ExitStack
import ipaddress
import json
from pathlib import Path
import time
from threading import Timer

from . import fpga, raw, rf
from .afe import (Afe, AFE_TARGETS, AFE_LEGACY_INIT_WRITES,
                  AFE_READBACK_MASKS, TEST_PAT_BASE, TEST_PAT_PAIRS)
from .bus import BOARD_CTRL_REG
from .hardware import access, ownership, require_idle, restore_word

# HSB retains its 12-bit page index across runs, even when the ring shrinks.
# Cover every page so a pattern capture can follow imaging without a board reset.
RECEIVE_PAGES = 4096


@contextmanager
def pattern(bus, mode):
    require_idle(bus, allow_rx_sync=True)
    frontend = fpga.Fpga(bus)
    with ExitStack() as restore:
        registers = [fpga.SYNC_CTRL + offset for offset in
                     (fpga.SYNC_CFG, fpga.SYNC_RX_SYNC_DELAY, fpga.SYNC_RX_SYNC_PULSE,
                      fpga.SYNC_EVENT_MASK, fpga.SYNC_SEQUENCE_COUNT, fpga.SYNC_SEQUENCE_INTERVAL)]
        registers += [fpga.CAPTURE_CTRL + offset for offset in
                      (fpga.CAPTURE_CFG, fpga.CAPTURE_DELAY_RECORDS, fpga.CAPTURE_LENGTH_RECORDS)]
        registers += [BOARD_CTRL_REG, fpga.TOP]
        registers += [base + offset for _, base, _ in fpga.GROUPS for offset in (0, 4, 8, 12)]
        for register in registers:
            restore.callback(restore_word, bus, register, bus.read32(register))
        for target in AFE_TARGETS:
            read, write = restore.enter_context(access(bus, target))
            afe_registers = [reg for reg, _ in AFE_LEGACY_INIT_WRITES]
            afe_registers += list(range(TEST_PAT_BASE, TEST_PAT_BASE + 2 * TEST_PAT_PAIRS))
            for register in afe_registers:
                restore.callback(restore_afe, target, read, write, register, read(register))
        # Stop capture and gate sync before restoring any saved configuration.
        restore.callback(bus.write32, fpga.SYNC_CTRL + fpga.SYNC_CFG, 0)
        restore.callback(bus.write32, fpga.CAPTURE_CTRL, fpga.CAPTURE_CMD_ABORT)
        bus.write32(fpga.SYNC_CTRL + fpga.SYNC_CFG, 0)
        bus.write32(fpga.CAPTURE_CTRL, fpga.CAPTURE_CMD_ABORT)
        # Unlike legacy bring-up, do not reset the board or touch TX configuration.
        bus.set_control(rx_spi_lvds_en=0, pdn_standby=0)
        frontend.clear_groups()
        frontend.program_sync_alignment()
        # Legacy RX-only bootstrap precedes sample-phase alignment; no TX events.
        # Imaging retains its separate phase-aligned launch configuration.
        cfg = (fpga.SYNC_CFG_RX_SYNC_INT_ENABLE | fpga.SYNC_CFG_RX_SYNC_EXT_ENABLE
               | fpga.SYNC_CFG_RX_SYNC_ENABLE)
        restore_word(bus, fpga.SYNC_CTRL + fpga.SYNC_CFG, cfg)
        restore_word(bus, fpga.SYNC_CTRL + fpga.SYNC_EVENT_MASK, fpga.SYNC_EVENT_RX_SYNC)
        afe = Afe(bus)
        for target in AFE_TARGETS:
            afe.init(target, mode="legacy")
        afe.set_pattern("dc", *rf.PATTERNS["dc"])
        frontend.configure_groups(*rf.PATTERNS["dc"])
        time.sleep(0.02)
        frontend.start_aligners(0.5)
        if not frontend.frontend_ready():
            raise RuntimeError("Receive frontend failed PLL/DPA readiness after initialization")
        if mode == "ramp":
            afe.set_pattern(mode, *rf.PATTERNS[mode])
        time.sleep(0.05)
        yield frontend


def restore_afe(target, read, write, register, value):
    write(register, value)
    actual, mask = read(register), AFE_READBACK_MASKS.get(register, 0xFFFF)
    if (actual ^ value) & mask:
        raise RuntimeError(
            f"{target} restoration failed at {register:#x}: expected {value:#06x}, "
            f"read {actual:#06x}, mask {mask:#06x}; inspect board before imaging")


def receive(args, channel, frontend, context, manifest):
    import cupy as cp
    import hololink
    import holoscan as hs

    class Device:
        def start(self):
            deadline.start()
            frontend.program_capture(args.records * args.count, 0, 0.02)
            frontend.clear_assembler()
            # Arm capture before RX sync releases the aligned stream.
            frontend.start_capture()
            frontend.pulse_sync_start()
            time.sleep(0.5)
            if not frontend.aligners_locked():
                raise RuntimeError("Receive lanes failed alignment after RX sync")

        def stop(self):
            pass  # The outer context restores hardware even if receiver teardown fails.

    class Sink(hs.core.Operator):
        def setup(self, spec):
            spec.input("input")

        def compute(self, op_input, op_output, context):
            data = cp.asarray(op_input.receive("input")[""]).get().tobytes()
            index = len(manifest["frames"])
            name = f"frame_{index:06d}.bin"
            with (Path(args.output) / name).open("xb") as stream:
                stream.write(data)
            keys = ("frame_number", "received_frame_number", "dropped", "bytes_written", "page_number")
            entry = dict(file=name, **{key: int(self.metadata[key]) for key in keys})
            manifest["frames"].append(entry)
            if len(data) != manifest["frame_bytes"] or rf.check_pattern(data, args.pattern)["pattern_errors"]:
                raise RuntimeError(f"Pattern or size mismatch: {name}")
            if entry["dropped"]:
                raise RuntimeError(f"Receiver reported dropped frames: {entry['dropped']}")
            if index + 1 == args.count:
                app.stop_execution()

    class Application(hs.core.Application):
        def compose(self):
            receiver = hololink.operators.RoceReceiverOp(
                self, hs.conditions.CountCondition(self, count=args.count), name="receiver",
                frame_size=manifest["frame_bytes"], frame_context=context,
                ibv_name=args.ibv_name, ibv_port=1, hololink_channel=channel,
                device=Device(), trim=True, pages=manifest["pages"], queue_size=manifest["queue_size"])
            self.add_flow(receiver, Sink(self, name="save"), {("output", "input")})

    app = Application()
    app.scheduler(hs.schedulers.GreedyScheduler(
        app, max_duration_ms=int(args.timeout * 1000), check_recession_period_ms=1,
        stop_on_deadlock=True, stop_on_deadlock_timeout=int(args.timeout * 1000)))
    # The scheduler deadline alone does not wake an idle asynchronous receiver.
    deadline = Timer(args.timeout, app.executor.interrupt)
    try:
        app.run()
    finally:
        deadline.cancel()
        if deadline.ident is not None:
            deadline.join()


def execute(args):
    import math
    args.ip = str(ipaddress.IPv4Address(args.ip))
    if not 1 <= args.count <= 4 or not 4 <= args.records <= 65536 or args.records % 4:
        raise ValueError("count must be 1..4 (receive queue limit); records must be 4..65536 and divisible by 4")
    if not math.isfinite(args.timeout) or not 1 <= args.timeout <= 60:
        raise ValueError("timeout must be 1..60 seconds")
    import hololink
    from . import host
    from .bus import Bus
    from .evm import enumeration_strategy
    args.output = args.output or f"captures/{args.pattern}-{time.time_ns()}"
    directory = Path(args.output)
    manifest = dict(schema=rf.SCHEMA, pattern=args.pattern, record_bytes=raw.RECORD_BYTES,
                    frame_bytes=args.records * raw.RECORD_BYTES, requested_count=args.count,
                    ip=args.ip, pages=RECEIVE_PAGES, queue_size=4, frames=[], errors=[], complete=False)
    with ownership():
        directory.mkdir(parents=True, exist_ok=False)
        try:
            with ExitStack() as resources:
                enumeration_strategy()
                metadata = hololink.Enumerator.find_channel(channel_ip=args.ip)
                hololink.DataChannel.use_sensor(metadata, 0)
                channel = hololink.DataChannel(metadata)
                control = channel.hololink()
                resources.callback(control.stop)
                control.start()
                context, device = host.retain_cuda_context()
                resources.callback(host.release_cuda_context, device)
                args.ibv_name = args.ibv_name or host.default_ibv_name()
                manifest["ibv_name"] = args.ibv_name
                frontend = resources.enter_context(pattern(Bus(control), args.pattern))
                receive(args, channel, frontend, context, manifest)
            manifest["complete"] = len(manifest["frames"]) == args.count
            if not manifest["complete"]:
                raise RuntimeError("Incomplete capture: receive timeout or pipeline failure")
        except (Exception, KeyboardInterrupt) as error:
            while error is not None:
                manifest["errors"].append(str(error) or type(error).__name__)
                error = error.__cause__ or error.__context__
        finally:
            try:
                (directory / "metadata.json").write_text(json.dumps(manifest, indent=2) + "\n")
            except OSError as error:
                manifest["errors"].append(f"Metadata save failed: {error}")
    return rf.finish_capture(directory, manifest)
