#!/usr/bin/env python3
"""Offline end-to-end RF inspection and receive-only register trace; no devices."""
import ast
from contextlib import ExitStack, contextmanager, redirect_stdout, redirect_stderr
import io
from itertools import product
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import numpy as np
from PIL import Image

from nvus import fpga, rf
from nvus.bus import Bus, Trace, BOARD_CTRL_REG, RX_SPI_BUS
from nvus.capture import pattern
from nvus.cli import print_capture


def packed_pattern(mode, records=16, phase=0):
    result = bytearray()
    for index in range(phase, records + phase):
        value = rf.PATTERNS[mode][index % 2]
        if mode == "ramp":
            value = (4090 + index // 2 * value) & 0xfff
        word = sum(value << (lane * 12) for lane in range(16))
        result.extend(word.to_bytes(24, "little") * 2)
    return result


def cli(*arguments, expected=0):
    result = subprocess.run([sys.executable, "-m", "nvus.cli", *arguments, "--format", "json"],
                            capture_output=True, text=True)
    assert result.returncode == expected, (arguments, result.stdout, result.stderr)
    return json.loads(result.stdout)


def inspect_examples(directory):
    for mode, phase in product(rf.PATTERNS, (0, 1)):
        data = packed_pattern(mode, phase=phase)
        manifest = dict(schema=rf.SCHEMA, record_bytes=48, frame_bytes=len(data),
                        pattern=mode, requested_count=2, complete=True, errors=[], frames=[])
        for index in range(2):
            name = f"frame_{index}.bin"
            (directory / name).write_bytes(data)
            manifest["frames"].append(dict(file=name, received_frame_number=index,
                                          frame_number=index, dropped=0, bytes_written=len(data)))
        metadata = directory / "metadata.json"
        metadata.write_text(json.dumps(manifest))
        report = checked_plot(directory, mode)
        assert report["frames"][0]["phase"] == phase
        assert report["ok"] and Path(report["plot"]).is_absolute()
        with Image.open(report["plot"]) as image:
            assert image.format == "PNG" and image.width > 0 and image.height > 0
            image.verify()
        output = io.StringIO()
        with redirect_stdout(output):
            print_capture(report)
        assert f"Plot: {report['plot']}" in output.getvalue()
        assert json.loads(json.dumps(report))["plot"] == report["plot"]
        assert not rf.finish_capture(directory)["ok"]  # Never overwrite an existing plot.
        Path(report["plot"]).unlink()
        (directory / "frame_0.bin").write_bytes(bytes([data[0] ^ 1]) + data[1:])
        assert not rf.inspect_capture(directory)["ok"]
        (directory / "frame_0.bin").write_bytes(data[:-1])
        assert "size mismatch" in str(rf.inspect_capture(directory)["errors"])
        (directory / "frame_0.bin").write_bytes(data)
        manifest["frames"][1]["frame_number"] = 3
        metadata.write_text(json.dumps(manifest))
        assert "HSB frame gap" in str(rf.inspect_capture(directory)["errors"])
        manifest["frames"][1]["frame_number"] = 1
        manifest["frames"][0]["dropped"] = 1
        metadata.write_text(json.dumps(manifest))
        assert not rf.inspect_capture(directory)["ok"]


def checked_plot(directory, mode):
    from matplotlib.figure import Figure
    save = Figure.savefig

    def verify(figure, *args, **kwargs):
        for index, line in enumerate(figure.axes[0].lines):
            values = line.get_ydata()
            if mode == "dc":
                assert np.all(values == rf.PATTERNS[mode][index % 2])
            else:
                assert np.all((np.diff(values) & 0xfff) == rf.PATTERNS[mode][index % 2])
        return save(figure, *args, **kwargs)

    Figure.savefig = verify
    try:
        return rf.finish_capture(directory)
    finally:
        Figure.savefig = save


def failed_capture(directory):
    from nvus import capture, cli as module, evm
    control = Trace()
    control.start = control.stop = lambda: None
    channel = lambda metadata: SimpleNamespace(hololink=lambda: control)
    channel.use_sensor = lambda metadata, sensor: None

    @contextmanager
    def failed_restore(bus, mode):
        try:
            yield None
        finally:
            raise RuntimeError("Restoration failed: injected")

    def failed_receive(args, channel, frontend, context, manifest):
        assert manifest["pages"] == 4096 and manifest["queue_size"] == 4
        data = packed_pattern("dc")
        (Path(args.output) / "frame.bin").write_bytes(data)
        manifest["frames"].append(dict(file="frame.bin", frame_number=0,
                                      received_frame_number=1, dropped=0, bytes_written=len(data)))
        raise RuntimeError("Receive failed: injected")

    with ExitStack() as restore:
        modules = {
            "hololink": SimpleNamespace(Enumerator=SimpleNamespace(find_channel=lambda **kwargs: {}),
                                        DataChannel=channel),
            "nvus.host": SimpleNamespace(retain_cuda_context=lambda: (None, None),
                                         release_cuda_context=lambda device: None),
        }
        for name, fake in modules.items():
            if name in sys.modules:
                restore.callback(sys.modules.__setitem__, name, sys.modules[name])
            else:
                restore.callback(sys.modules.pop, name, None)
            sys.modules[name] = fake
        for obj, name, value in (
            (evm, "enumeration_strategy", lambda: None),
            (capture, "pattern", failed_restore), (capture, "receive", failed_receive)):
            restore.callback(setattr, obj, name, getattr(obj, name))
            setattr(obj, name, value)
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            code = module.main(["capture", "test-pattern", "dc", "--count", "2", "--records", "16",
                                "--ibv-name", "unused", "--output", str(directory), "--format", "json"])
    report = json.loads(output.getvalue())
    assert code == 1 and not report["ok"] and report["capture"] == str(directory.resolve())
    assert set(report["errors"]) == {"Receive failed: injected", "Restoration failed: injected"}
    assert len(report["frames"]) == 1 and Path(report["plot"]).is_file()
    saved = json.loads((directory / "metadata.json").read_text())
    assert not saved["complete"] and saved["errors"] == report["errors"]


def trace_restoration():
    for unsafe in (0x4, 0x8, 0x10, 0x40, 0x80, 0x100, 0x400):
        trace = Trace({fpga.SYNC_CTRL + fpga.SYNC_CFG: unsafe})
        try:
            with pattern(Bus(trace), "dc"):
                raise AssertionError("Unsafe sync configuration accepted")
        except RuntimeError as error:
            assert "Idle check blocked" in str(error)
        assert all(event[0] == "rd32" for event in trace.events)
    reads = {}
    for _, base, mask in fpga.GROUPS:
        reads.update({base + 0x84: (1 << 17) | (1 << 18) | (1 << 20),
                      base + 0x88: mask})
    for fail in (False, True):
        trace = Trace(reads)
        trace._regs[fpga.SYNC_CTRL + fpga.SYNC_CFG] = 0x23
        bus = Bus(trace)
        try:
            with pattern(bus, "dc") as frontend:
                frontend.program_capture(16, 0, 0)
                frontend.start_capture()
                frontend.pulse_sync_start()
                if fail:
                    raise RuntimeError("injected capture failure")
        except RuntimeError as error:
            assert str(error) == "injected capture failure"
        assert bus.read32(fpga.SYNC_CTRL + fpga.SYNC_CFG) == 0x23
        assert bus.read32(BOARD_CTRL_REG) == 0
        assert all(value == 0 for value in trace._chip_values[RX_SPI_BUS].values())
        for event in trace.events:
            if event[0] == "spi":
                assert event[1] == RX_SPI_BUS
            if event[0] == "wr32":
                assert event[1] < 0x70000000 or event[1] == fpga.TOP or any(
                    base <= event[1] <= base + 12 for _, base, _ in fpga.GROUPS)
                if event[1] == fpga.SYNC_CTRL + fpga.SYNC_CFG:
                    assert event[2] & ~0x223 == 0
                if event[1] == fpga.SYNC_CTRL + fpga.SYNC_EVENT_MASK:
                    assert event[2] in (0, fpga.SYNC_EVENT_RX_SYNC)


def diagnostic_reads():
    from nvus import hardware, cli as module
    from nvus.bus import TX_HS_SPI_STATUS
    trace = Trace({fpga.SYNC_CTRL + fpga.SYNC_CFG: 0x23,
                   fpga.TX_UPDATE_CTRL: 1, TX_HS_SPI_STATUS: 1})

    @contextmanager
    def connection(ip):
        yield Bus(trace)

    original = hardware.connect
    hardware.connect = connection
    try:
        for device, register, expected in (("fpga", "0x40000004", 0), ("rx1", "0x92", 1),
                                            ("tx1", "0x1f", 1), ("fpga", "0x40000000", 1)):
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                code = module.main(["hw", "read", device, register, "--format", "json"])
            report = json.loads(output.getvalue())
            assert code == expected
            if not code:
                assert report["value"] == 0x23
            elif device != "fpga":
                assert all(text in report["error"] for text in
                           ("SYNC_CFG(0x40000004)=0x23", "TX_UPDATE_CTRL(0x01200000)=0x1",
                            "TX_SPI_STATUS(0x70000080)=0x1"))
        assert all(event[0] == "rd32" for event in trace.events)
        with hardware.ownership(), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            assert module.main(["hw", "read", "fpga", "0x40000004"]) == 1
        previous = Path.cwd()
        with TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    assert module.main(["hw", "write", "fpga", "0x50000008", "1",
                                        "--expect", "0"]) == 1
                assert Path(".runtime/hw-writes.jsonl").read_text() == ""
                assert all(event[0] == "rd32" for event in trace.events)
            finally:
                os.chdir(previous)
    finally:
        hardware.connect = original


def main():
    for arguments in (("capture",), ("capture", "test-pattern"),
                      ("capture", "test-pattern", "dc", "--help")):
        assert cli(*arguments)["command"] == "help"
    for count in ("0", "5", "1024"):
        assert "count must" in cli("capture", "test-pattern", "dc", "--count", count, expected=1)["error"]
    with TemporaryDirectory() as directory:
        inspect_examples(Path(directory))
        failed_capture(Path(directory) / "failed")
    trace_restoration()
    diagnostic_reads()
    from nvus import cli as module
    for node in ast.walk(ast.parse(Path(module.__file__).read_text())):
        if isinstance(node, ast.FunctionDef):
            assert node.end_lineno - node.lineno + 1 <= 20, node.name
    print("PASS: DC/ramp RF inspection, invalid captures, help, RX-only trace and restoration; no hardware")


if __name__ == "__main__":
    main()
