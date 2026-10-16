# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Installed CLI: software diagnostics and explicit idle register access."""

import argparse
import importlib
from importlib import metadata, util
import json
from pathlib import Path
import sys
from .run import parser as run_parser, execute as run_report, print_run


NOTICE = "Early reference; APIs and data formats may change during 0.x."
SDK_MODULES = ("hololink", "holoscan", "cuda.bindings")
OPTIONAL_MODULES = ("cupy", "scipy", "pygame", "PIL", "tensorrt")


def check_import(name, installed):
    try:
        module = importlib.import_module(name)
        if name == "nvus._scanner" and Path(module.__file__).resolve().parent != installed:
            raise ImportError("native extension is not from the installed package")
        return dict(name=name, status="ok")
    except Exception as error:
        return dict(name=name, status="error", detail=str(error),
                    fix="Unset PYTHONPATH and reinstall nvus and its dependencies.")


def check_presence(name, required):
    # Presence is not runtime compatibility; do not initialize SDK/GPU/display libraries.
    try:
        present = util.find_spec(name) is not None
        if name == "tensorrt" and not present:
            present = util.find_spec("tensorrt_bindings") is not None
    except ModuleNotFoundError:
        present = False
    status = "present" if present else "error" if required else "optional-missing"
    check = dict(name=name, status=status)
    if status == "error":
        check["fix"] = "Use the SDK container and rebuild nvus against its installed SDKs."
    return check


def doctor():
    """Check software only; never open devices."""
    installed = Path(metadata.distribution("nvus").locate_file("nvus")).resolve()
    checks = [dict(name="installed-package",
                   status="ok" if Path(__file__).resolve().parent == installed else "error",
                   fix="Unset PYTHONPATH and run the installed nvus executable.")]
    checks.extend(check_import(name, installed) for name in ("numpy", "nvus._scanner"))
    native = sys.modules.get("nvus._scanner")
    sdk = bool(native and native.sdk_available)
    checks.extend(check_presence(name, sdk) for name in SDK_MODULES)
    checks.extend(check_presence(name, False) for name in OPTIONAL_MODULES)
    return dict(ok=all(check["status"] != "error" for check in checks),
                sdk_compiled=sdk, checks=checks,
                native_source_sha256=getattr(native, "native_source_sha256", None),
                build_revision=getattr(native, "repository_revision", None),
                scope="Software imports/presence only; GPU, NIC and board readiness are not tested.")


def common_options(parser):
    parser.set_defaults(selected_parser=parser)
    parser.add_argument("-h", "--help", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--format", choices=("text", "json"), default=argparse.SUPPRESS)
    return parser


def hardware_parser(commands, help_requested):
    parser = common_options(commands.add_parser("hw", add_help=False, help="Idle register access"))
    operations = parser.add_subparsers(dest="operation")
    nargs = "?" if help_requested else None
    for name in ("read", "write"):
        item = common_options(operations.add_parser(name, add_help=False))
        item.add_argument("device", choices=("fpga", "rx1", "rx2", "tx1", "tx2"), nargs=nargs)
        item.add_argument("register", type=lambda value: int(value, 0), nargs=nargs)
        item.add_argument("--ip", default="192.168.0.2", help="HSB control endpoint; rig must be idle")
        if name == "write":
            item.add_argument("value", type=lambda value: int(value, 0), nargs=nargs)
            item.add_argument("--expect", required=not help_requested, type=lambda value: int(value, 0),
                              help="Required old value; refuse stale writes")


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="nvus", add_help=False,
                                     description=f"Holoscan Ultrasound Imaging. {NOTICE}")
    parser.add_argument("--version", action="store_true", help="Show installed version")
    commands = parser.add_subparsers(dest="command")
    common_options(parser)
    for name, description in dict(help="Show usage", version="Show installed version",
                                 doctor="Check software dependencies without opening hardware").items():
        common_options(commands.add_parser(name, add_help=False, help=description))
    argv = sys.argv[1:] if argv is None else argv
    help_requested = any(flag in argv for flag in ("-h", "--help"))
    hardware_parser(commands, help_requested)
    capture_parsers(commands)
    run_parser(commands, common_options)
    args = parser.parse_args(argv)
    if args.version and args.command:
        parser.error("--version cannot be combined with a command")
    return args, parser if args.command == "help" else args.selected_parser


def capture_parsers(commands):
    capture = common_options(commands.add_parser("capture", add_help=False, help="Receive-only AFE patterns"))
    modes = capture.add_subparsers(dest="mode")
    pattern = common_options(modes.add_parser("test-pattern", add_help=False))
    patterns = pattern.add_subparsers(dest="pattern")
    for mode in ("dc", "ramp"):
        item = common_options(patterns.add_parser(mode, add_help=False))
        capture_options(item)


def capture_options(parser):
    parser.add_argument("--ip", default="192.168.0.2", help="Idle HSB endpoint; exclusive board access required")
    parser.add_argument("--count", type=int, default=1, help="1–4 frames (default: 1; receive queue limit)")
    parser.add_argument("--records", type=int, default=4096, help="48-byte records per frame")
    parser.add_argument("--timeout", type=float, default=10, help="Receive timeout in seconds")
    parser.add_argument("--output", help="New capture directory (default: captures/PATTERN-TIMESTAMP)")
    parser.add_argument("--ibv-name", help="Receive NIC; default: first RDMA device")


def capture_report(args):
    from .capture import execute
    report = make_report(f"capture test-pattern {args.pattern}")
    report.update(ok=False, effect="receive-only")
    try:
        report.update(execute(args))
    except Exception as error:
        report["error"] = str(error)
    return report


def make_report(command, subject=None):
    report = dict(schema="nvus.cli.v1", version=metadata.version("nvus"),
                  notice=NOTICE, effect="offline", command=command)
    if command == "help":
        report.update(program=f"nvus {subject}" if subject else "nvus",
                      commands={} if subject else dict(help="offline", version="offline",
                                                       doctor="offline", hw="hardware",
                                                       capture="receive-only", run="transmit"),
                      options=(["--version"] if not subject else []) +
                              ["--help", "--format text|json"],
                      exit_codes={"0": "success", "1": "operation failure", "2": "usage error"})
    elif command == "doctor":
        report.update(doctor())
    return report


def hardware_report(args):
    from .hardware import execute
    report = make_report(f"hw {args.operation}")
    report.update(effect="idle-read" if args.operation == "read" else "hardware-write",
                  ip=args.ip, device=args.device, register=args.register, ok=False)
    try:
        report.update(execute(args), ok=True)
    except Exception as error:
        report["error"] = str(error)
    return report


def print_hardware(report):
    if report["ok"]:
        print(f"{report['device']} {report['register']:#x} = {report['value']:#x}")
        if report.get("verified"):
            print(f"Previous: {report['before']:#x}; readback verified; log: .runtime/hw-writes.jsonl")
    else:
        print(report["error"], file=sys.stderr)


def print_report(report, output_format):
    printers = {"capture": print_capture, "hw": print_hardware, "run": print_run}
    command = report["command"].split()[0]
    if output_format == "text" and command in printers:
        return printers[command](report)
    if output_format == "json":
        print(json.dumps(report))
        if "error" in report:
            print(report["error"], file=sys.stderr)
    else:
        print(f"nvus {report['version']} — {NOTICE}")
        if report["command"] == "doctor":
            print(f"Native HSB SDK support: {'yes' if report['sdk_compiled'] else 'no (CPU build)'}")
            for check in report["checks"]:
                print(f"{check['status']}: {check['name']}")
            print(report["scope"])
    for check in report.get("checks", []):
        if check["status"] == "error":
            print(f"{check['name']}: {check.get('detail', 'missing')}. {check['fix']}", file=sys.stderr)


def print_capture(report):
    if "error" in report:
        print(report["error"], file=sys.stderr)
        return
    errors = sum(frame["pattern_errors"] for frame in report["frames"])
    print(f"{'PASS' if report['ok'] else 'FAIL'}: {report['pattern']}; "
          f"{len(report['frames'])}/{report['requested_count']} frames; {errors} pattern errors")
    print(f"Capture: {report['capture']}")
    if report.get("plot"):
        print(f"Plot: {report['plot']}")
    for error in report["errors"]:
        print(error, file=sys.stderr)


def main(argv=None):
    args, parser = parse_args(argv)
    output_format = getattr(args, "format", "text")
    help_requested = args.command == "help" or getattr(args, "help", False) or not (args.command or args.version)
    help_requested |= args.command == "hw" and not args.operation
    help_requested |= args.command == "capture" and not getattr(args, "pattern", None)
    help_requested |= args.command == "run" and not args.recipe
    if help_requested and output_format == "text":
        parser.print_help()
        return 0
    command = "help" if help_requested else args.command or "version"
    report = (run_report(args) if command == "run" else capture_report(args) if command == "capture" else
              hardware_report(args) if command == "hw" else
              make_report(command, None if args.command == "help" else args.command))
    if command == "help" and args.command in ("hw", "capture", "run"):
        report.update(program=parser.prog, commands={}, usage=parser.format_help())
    print_report(report, output_format)
    return 0 if report.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
