#!/usr/bin/env python3
"""End-to-end check of an installed wheel's CLI, native extension and CUDA resources."""
from importlib import metadata, resources
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

import nvus
from nvus import _scanner as cpp


def main():
    package = Path(metadata.distribution("nvus").locate_file("nvus")).resolve()
    assert Path(nvus.__file__).resolve().parent == package
    assert Path(cpp.__file__).resolve().parent == package
    assert cpp.native_source_sha256
    for name in ("das.cu", "decode.cu"):
        assert resources.files("nvus").joinpath("kernels", name).is_file()
    cli = str(Path(sys.executable).parent / "nvus")
    with TemporaryDirectory() as directory:
        result = subprocess.run([sys.executable, "-m", "nvus.cli", "version", "--format", "json"],
                                cwd=directory, check=True, text=True, stdout=subprocess.PIPE)
        report = json.loads(result.stdout)
        assert report["command"] == "version" and report["version"] == metadata.version("nvus")
        for arguments in (("version",), ("help",), ("--version",), ("--help",), ("doctor", "--help"),
                          ("doctor",)):
            subprocess.run([cli, *arguments], cwd=directory, check=True)
        for arguments, command in ((["version"], "version"), (["help"], "help"),
                                   (["--version"], "version"),
                                   (["--help"], "help"),
                                   (["doctor", "--help"], "help"),
                                   (["doctor"], "doctor")):
            result = subprocess.run([cli, *arguments, "--format", "json"],
                                    cwd=directory, check=True, text=True,
                                    stdout=subprocess.PIPE)
            report = json.loads(result.stdout)
            assert report["schema"] == "nvus.cli.v1"
            assert report["version"] == metadata.version("nvus")
            assert report["command"] == command and report["effect"] == "offline"
            if command == "doctor":
                assert report["ok"] and report["sdk_compiled"] == cpp.sdk_available
            if command == "help":
                is_doctor = arguments[0] == "doctor"
                assert report["program"] == ("nvus doctor" if is_doctor else "nvus")
                assert report["commands"] == ({} if is_doctor else
                    dict(help="offline", version="offline", doctor="offline", hw="hardware",
                         capture="receive-only", run="transmit"))
                options = ["--help", "--format text|json"]
                assert report["options"] == (options if is_doctor else ["--version", *options])
        for arguments in (("unknown",), ("rf",), ("acquire",), ("firmware",),
                          ("doctor", "--version"), ("doctor", "doctor")):
            result = subprocess.run([cli, *arguments], cwd=directory,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert result.returncode == 2 and not result.stdout and result.stderr
        for arguments in (("hw",), ("hw", "read", "--help"), ("hw", "write", "--help"),
                          ("run",), ("run", "pw4cm", "--help")):
            result = subprocess.run([cli, *arguments, "--format", "json"], cwd=directory,
                                    check=True, text=True, stdout=subprocess.PIPE)
            report = json.loads(result.stdout)
            assert report["command"] == "help" and report["effect"] == "offline"
        for arguments in (("hw", "read"), ("hw", "write", "rx1", "0x92", "1")):
            result = subprocess.run([cli, *arguments], cwd=directory, capture_output=True)
            assert result.returncode == 2
        result = subprocess.run([cli, "hw", "write", "fpga", "0x40000000", "1",
                                 "--expect", "0", "--format", "json"], cwd=directory,
                                capture_output=True, text=True)
        assert result.returncode == 1 and "Unsupported register" in json.loads(result.stdout)["error"]
    print("PASS: installed CLI, native extension and packaged CUDA sources; no hardware opened")


if __name__ == "__main__":
    main()
