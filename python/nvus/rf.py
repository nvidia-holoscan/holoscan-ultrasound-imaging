# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Saved receive-pattern captures; CPU inspection uses the existing LVDS decoder."""
import json
from pathlib import Path

import numpy as np

from . import raw

SCHEMA = "nvus.rf.v1"
PATTERNS = {"dc": (0x5a3, 0x0c6), "ramp": (1, 2)}


def plot_capture(directory, frame, mode, phase):
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    samples, _ = raw.unpack_records((directory / frame).read_bytes())
    samples &= raw.SAMPLE_MASK
    # Phase one starts on the second physical channel of each LVDS lane.
    samples = samples[:, np.arange(samples.shape[1]) ^ phase]
    figure = Figure(figsize=(12, 7), layout="constrained")
    FigureCanvasAgg(figure)
    traces, channels = figure.subplots(2, 1)
    for index in (0, 1, 32, 33):
        traces.plot(samples[:, index], label=f"AFE{index // 32 + 1}:ch{index % 32}")
    traces.set(title=f"{mode.upper()} — {frame} (first frame)", ylabel="Unsigned 12-bit code")
    traces.legend(ncol=4)
    pixels = channels.imshow(samples.T, aspect="auto", interpolation="nearest",
                             origin="lower", vmin=0, vmax=4095)
    channels.set(xlabel="Sample index", ylabel="Channel (AFE1: 0–31, AFE2: 32–63)")
    figure.colorbar(pixels, ax=channels, label="Unsigned 12-bit code")
    path = directory / "pattern.png"
    with path.open("xb") as stream:
        figure.savefig(stream, format="png", dpi=150)
    return str(path.resolve())


def finish_capture(path, manifest=None):
    report = inspect_capture(path, manifest)
    if report["frames"]:
        try:
            frame = report["frames"][0]
            report["plot"] = plot_capture(Path(path), frame["file"], report["pattern"], frame["phase"])
        except Exception as error:
            report["ok"] = False
            report["errors"].append(f"PNG generation failed; RF data retained: {error}")
    return report


def check_pattern(data, mode):
    if len(data) < 4 * raw.RECORD_BYTES or len(data) % raw.FRAME_ALIGNMENT_BYTES:
        raise ValueError("RF length must contain complete 192-byte groups")
    samples, info = raw.unpack_records(data, merge_phases=False)
    words = samples & raw.SAMPLE_MASK
    errors = []
    for phase in (0, 1):
        values = np.asarray(PATTERNS[mode])[(np.arange(len(words)) + phase) % 2]
        if mode == "dc":
            mismatch = np.count_nonzero(words != values[:, None])
        else:
            mismatch = np.count_nonzero(words != words[:, :1])
            mismatch += np.count_nonzero(((words[2:] - words[:-2]) & raw.SAMPLE_MASK)
                                         != values[2:, None])
        errors.append(int(mismatch))
    return dict(records=info["records"], samples=info["records"] // 2,
                pattern_errors=min(errors), phase=int(np.argmin(errors)))


def inspect_capture(path, manifest=None):
    directory = Path(path).resolve()
    if manifest is None:
        manifest = json.loads((directory / "metadata.json").read_text())
    if manifest.get("schema") != SCHEMA or manifest.get("record_bytes") != raw.RECORD_BYTES:
        raise ValueError("Unsupported capture schema or record format")
    if manifest.get("pattern") not in PATTERNS or not 1 <= manifest["requested_count"] <= 1024:
        raise ValueError("Invalid pattern or frame count")
    size = manifest["frame_bytes"]
    if not 192 <= size <= 65536 * raw.RECORD_BYTES or size % 192:
        raise ValueError("Invalid frame size")
    frames = []
    names = set()
    errors = list(manifest.get("errors", []))
    for entry in manifest["frames"]:
        name = entry["file"]
        if Path(name).name != name or not name.endswith(".bin") or name in names:
            raise ValueError("Capture frame must be a local .bin filename")
        names.add(name)
        file = directory / name
        try:
            if file.is_symlink() or file.stat().st_size != size:
                raise ValueError(f"RF size mismatch: {name}")
            data = file.read_bytes()
            frames.append(dict(file=name, bytes=len(data), **check_pattern(data, manifest["pattern"])))
        except (OSError, ValueError) as error:
            errors.append(str(error))
        if entry["dropped"]:
            errors.append(f"Receiver drops: {entry['file']}")
        if entry["bytes_written"] != size:
            errors.append(f"Packet byte count mismatch: {entry['file']}")
    for before, after in zip(manifest["frames"], manifest["frames"][1:]):
        if after["received_frame_number"] != before["received_frame_number"] + 1:
            errors.append(f"Receive count gap: {after['file']}")
        if after["frame_number"] != (before["frame_number"] + 1) & 0xffffffff:
            errors.append(f"HSB frame gap: {after['file']}")
    ok = (manifest.get("complete") is True and len(frames) == manifest["requested_count"]
          and not any(frame["pattern_errors"] for frame in frames)
          and not errors)
    return dict(ok=ok, capture=str(directory), pattern=manifest["pattern"], frames=frames,
                requested_count=manifest["requested_count"], errors=errors)
