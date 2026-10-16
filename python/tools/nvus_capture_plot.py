#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import argparse
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from nvus import raw  # noqa: E402
from nvus.raw import AFES_PER_RECORD, RECORD_BYTES as AFE_PAIR_BYTES, CHANNELS_PER_AFE, LANES_PER_AFE, AFE_NAMES  # noqa: E402


def channel_limit(channel_mode):
    return CHANNELS_PER_AFE if channel_mode == "physical" else LANES_PER_AFE


def channel_kind(channel_mode):
    return "physical channel" if channel_mode == "physical" else "raw lane"


def parse_channel(text, channel_mode):
    try:
        afe_name, index_text = text.split(":", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not CHANNEL format; expected afe1:0 or afe2:31"
        ) from exc
    if afe_name not in AFE_NAMES:
        raise argparse.ArgumentTypeError(f"{afe_name!r} must be afe1 or afe2")

    index = int(index_text, 0)
    limit = channel_limit(channel_mode)
    if not 0 <= index < limit:
        raise argparse.ArgumentTypeError(f"{channel_kind(channel_mode)} {index} must be 0..{limit - 1}")
    return AFE_NAMES.index(afe_name), index


def expand_channel_spec(text, channel_mode):
    limit = channel_limit(channel_mode)
    if text == "all":
        return [(afe, index) for afe in range(AFES_PER_RECORD) for index in range(limit)]

    try:
        afe_name, index_text = text.split(":", 1)
    except ValueError:
        return [parse_channel(text, channel_mode)]
    if afe_name not in AFE_NAMES:
        return [parse_channel(text, channel_mode)]

    afe = AFE_NAMES.index(afe_name)
    if index_text == "all":
        return [(afe, index) for index in range(limit)]
    if "-" in index_text:
        try:
            start_text, end_text = index_text.split("-", 1)
            start = int(start_text, 0)
            end = int(end_text, 0)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid channel range {text!r}") from exc
        if start > end or start < 0 or end >= limit:
            raise argparse.ArgumentTypeError(f"{channel_kind(channel_mode)} range {text!r} must stay within 0..{limit - 1}")
        return [(afe, index) for index in range(start, end + 1)]
    return [parse_channel(text, channel_mode)]


def parse_channels(text, channel_mode):
    channels = []
    seen = set()
    for part in (part.strip() for part in text.split(",")):
        if not part:
            continue
        for channel in expand_channel_spec(part, channel_mode):
            if channel not in seen:
                seen.add(channel)
                channels.append(channel)
    return channels


def channel_label(channel, channel_mode="physical"):
    afe, index = channel
    if channel_mode == "physical":
        return f"{AFE_NAMES[afe]}:ch{index}"
    return f"{AFE_NAMES[afe]}:lane{index}"


def all_channels(channel_mode):
    return [channel for afe in range(AFES_PER_RECORD) for channel in afe_channels(afe, channel_mode)]


def afe_channels(afe, channel_mode):
    return [(afe, index) for index in range(channel_limit(channel_mode))]


def decode_capture(raw_bytes, *, signed, channel_mode="physical", record_offset=0, start_record=0, max_records=8192):
    samples, info = raw.unpack_records(
        raw_bytes,
        record_offset=record_offset,
        start_record=start_record,
        max_records=max_records,
        merge_phases=channel_mode == "physical",
    )
    if not signed:
        samples = samples & raw.SAMPLE_MASK
    return {
        "samples": samples,
        "records": info["records"],
        "trailing_bytes": info["trailing_bytes"],
    }


def extract_traces(decoded, channels, channel_mode="physical"):
    stride = channel_limit(channel_mode)
    return {
        channel: decoded["samples"][:, channel[0] * stride + channel[1]].tolist()
        for channel in channels
    }


def remove_dc_offsets(channel_data):
    for values in channel_data.values():
        if not values:
            continue
        mean = sum(values) / len(values)
        for index, value in enumerate(values):
            values[index] = value - mean

def summarize_trace(label, values, sample_rate):
    if not values:
        return f"{label}: no samples"
    count = len(values)
    vmin = min(values)
    vmax = max(values)
    mean = sum(values) / count
    summary = f"{label}: n={count} min={vmin:.3f} max={vmax:.3f} pp={(vmax - vmin):.3f} mean={mean:.3f}"

    if count >= 8:
        try:
            import numpy as np
        except ImportError:
            return summary + " fft=unavailable(numpy missing)"
        arr = np.asarray(values, dtype=float)
        arr -= np.mean(arr)
        window = np.hanning(arr.size)
        spectrum = np.fft.rfft(arr * window)
        mags = np.abs(spectrum)
        if mags.size > 1:
            mags[0] = 0.0
            peak_idx = int(np.argmax(mags))
            freq = np.fft.rfftfreq(arr.size, d=1.0 / sample_rate)[peak_idx]
            summary += f" peak={freq:.3f} Hz"
    return summary


def write_csv(path, data, records, channel_mode):
    labels = [channel_label(channel, channel_mode) for channel in data]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["record", *labels])
        for idx in range(records):
            writer.writerow([idx, *[data[channel][idx] for channel in data]])


def transform_image_matrix(matrix, *, signed):
    import numpy as np

    ref = 2047.0 if signed else 4095.0
    floor_db = -80.0
    floor_mag = ref * (10.0 ** (floor_db / 20.0))
    mag = abs(matrix)
    db = 20.0 * np.log10(np.maximum(mag, floor_mag) / ref)
    db = np.maximum(db, floor_db)

    signed_db = np.sign(matrix) * (db - floor_db)
    vabs = max(abs(floor_db), float(abs(signed_db).max()), 1.0)
    label = f"signed magnitude (dB above {floor_db:g} dBFS floor)"
    return signed_db, "coolwarm", -vabs, vabs, label


def write_channel_step_image(
    path,
    heatmap,
    records,
    sample_rate,
    title,
    channel_mode,
    image_channels,
    *,
    signed,
    use_agg=True,
    show=False,
):
    try:
        import matplotlib
        if use_agg:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("image plotting requires matplotlib and numpy") from exc

    if records <= 0:
        raise RuntimeError("no records to image")

    ordered = image_channels
    if not ordered:
        raise RuntimeError("no image channels selected")
    matrix = np.asarray([heatmap[channel] for channel in ordered], dtype=float).T
    image, cmap, vmin, vmax, colorbar_label = transform_image_matrix(matrix, signed=signed)

    fig, ax = plt.subplots(1, 1, figsize=(15, 8.5), constrained_layout=True)
    im = ax.imshow(
        image,
        aspect="auto",
        interpolation="nearest",
        extent=[-0.5, len(ordered) - 0.5, records - 0.5, -0.5],
        cmap=cmap,
        vmin=vmin,
        vmax=vmax,
    )
    ax.set_title(f"{title} channel-step image")
    ax.set_xlabel("channel")
    ax.set_ylabel("step")
    ax.set_xticks(range(len(ordered)))
    ax.set_xticklabels([channel_label(channel, channel_mode) for channel in ordered], rotation=90, fontsize=7)
    ax.set_yticks(sorted(set([0, records // 4, records // 2, (3 * records) // 4, records - 1])))
    for index in range(1, len(ordered)):
        if ordered[index - 1][0] != ordered[index][0]:
            ax.axvline(index - 0.5, color="black", linewidth=0.8, alpha=0.45)
    duration_us = (records - 1) / sample_rate * 1e6 if records > 1 else 0.0
    ax.text(
        0.995,
        0.01,
        f"{records} steps, {duration_us:.3f} us",
        transform=ax.transAxes,
        ha="right",
        va="bottom",
        fontsize=9,
        bbox={"boxstyle": "round,pad=0.25", "facecolor": "white", "alpha": 0.75, "edgecolor": "none"},
    )
    fig.colorbar(im, ax=ax, label=colorbar_label)
    if path is not None:
        fig.savefig(path, dpi=150)
    if show:
        plt.show()
    else:
        plt.close(fig)

def plot_png(path, data, heatmap, records, sample_rate, title, channel_mode, show_heatmap, show=False):
    try:
        import matplotlib
        if not show:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("plotting requires matplotlib and numpy") from exc

    if records <= 0:
        raise RuntimeError("no records to plot")

    x = np.arange(records) / sample_rate * 1e6
    xlabel = "time (us)"

    saved_paths = []
    trace_fig, ax_trace = plt.subplots(1, 1, figsize=(13, 5), constrained_layout=True)
    for channel, values in data.items():
        ax_trace.plot(x, values, label=channel_label(channel, channel_mode), linewidth=1.0)
    ax_trace.set_title(title)
    ax_trace.set_xlabel(xlabel)
    ax_trace.set_ylabel("sample value")
    ax_trace.grid(True, alpha=0.3)
    show_legend = len(data) <= 12
    if show_legend:
        ax_trace.legend(loc="best", fontsize=8)
    else:
        ax_trace.text(
            0.01,
            0.99,
            f"{len(data)} channels; legend omitted",
            transform=ax_trace.transAxes,
            va="top",
            fontsize=9,
            bbox={"boxstyle": "round,pad=0.25", "facecolor": "white", "alpha": 0.75, "edgecolor": "none"},
        )

    if path is not None:
        trace_fig.savefig(path, dpi=150)
        saved_paths.append(("trace", path))

    if show_heatmap:
        images = []
        for afe in range(AFES_PER_RECORD):
            ordered = afe_channels(afe, channel_mode)
            images.append(np.asarray([heatmap[channel] for channel in ordered], dtype=float))

        all_heat_values = np.concatenate([image.ravel() for image in images])
        vmin = float(np.min(all_heat_values))
        vmax = float(np.max(all_heat_values))
        if vmin < 0.0 < vmax:
            vabs = max(abs(vmin), abs(vmax))
            vmin = -vabs
            vmax = vabs
            cmap = "coolwarm"
        else:
            cmap = "viridis"
        if vmin == vmax:
            vmin -= 1.0
            vmax += 1.0

        heat_fig, heat_axes = plt.subplots(
            AFES_PER_RECORD,
            1,
            figsize=(13, 8.5),
            constrained_layout=True,
            sharex=True,
        )
        heat_kind = channel_kind(channel_mode)
        heat_extent_x1 = x[-1] if records > 1 else x[0]
        heat_ims = []
        for afe, (ax_heat, image) in enumerate(zip(heat_axes, images)):
            ordered = afe_channels(afe, channel_mode)
            extent = [x[0], heat_extent_x1, len(ordered) - 0.5, -0.5]
            im = ax_heat.imshow(
                image,
                aspect="auto",
                interpolation="nearest",
                extent=extent,
                vmin=vmin,
                vmax=vmax,
                cmap=cmap,
            )
            heat_ims.append(im)
            ax_heat.set_title(f"{AFE_NAMES[afe]} {heat_kind} heatmap")
            ax_heat.set_ylabel(heat_kind)
            ax_heat.set_yticks(range(len(ordered)))
            ax_heat.set_yticklabels([str(channel[1]) for channel in ordered], fontsize=7)
            ax_heat.set_yticks([index + 0.5 for index in range(len(ordered) - 1)], minor=True)
            ax_heat.grid(which="minor", axis="y", color="white", linewidth=0.35, alpha=0.35)
        heat_axes[-1].set_xlabel(xlabel)
        heat_fig.colorbar(heat_ims[-1], ax=list(heat_axes), label="sample value")

        if path is not None:
            heatmap_path = path.with_name(f"{path.stem}_heatmaps{path.suffix}")
            heat_fig.savefig(heatmap_path, dpi=150)
            saved_paths.append(("heatmaps", heatmap_path))

    if show:
        plt.show()
    return saved_paths

def parse_args():
    parser = argparse.ArgumentParser(
        description="Parse and visualize raw NVUS 1-SIF LVDS capture dumps.",
    )
    parser.add_argument("capture", type=Path, help="Raw frame_NNNNNN.bin capture dump")
    parser.add_argument(
        "--channels",
        default="afe1:0,afe1:1,afe2:0,afe2:1",
        help="Comma-separated channels, for example afe1:0,afe1:0-7,afe2:all, or all. In physical mode these are physical channels 0..31; in lane mode these are raw LVDS lanes 0..15.",
    )
    parser.add_argument("--channel-mode", choices=("physical", "lane"), default="physical", help="Interpret --channels as deinterleaved physical channels or raw LVDS lanes.")
    parser.add_argument("--signed", action="store_true", help="Interpret 12-bit samples as signed two's-complement values.")
    parser.add_argument("--remove-dc", action="store_true", help="Subtract each channel mean before stats, CSV, and plots.")
    parser.add_argument("--record-offset", type=int, default=0, help="Byte offset before the first 48-byte AFE-pair record.")
    parser.add_argument("--start-record", type=int, default=0)
    parser.add_argument("--max-records", type=int, default=8192, help="Records to parse; use 0 for the whole file.")
    parser.add_argument("--out", type=Path, default=None, help="Optional PNG or SVG output path.")
    parser.add_argument("--image-out", type=Path, default=None, help="Optional channel-by-step image output path.")
    parser.add_argument("--image-show", action="store_true", help="Open the channel-by-step image in an interactive matplotlib window.")
    parser.add_argument("--image-channels", default="all", help="Comma-separated channels for --image-out/--image-show, for example afe1:0-15,afe2:all, or all. Uses --channel-mode numbering.")
    parser.add_argument("--csv-out", type=Path, default=None, help="Optional CSV output path for selected channels.")
    parser.add_argument("--show", action="store_true", help="Open an interactive matplotlib window.")
    parser.add_argument("--no-heatmap", action="store_true", help="Omit all-channel heatmap from the PNG.")
    return parser.parse_args()


def main():
    args = parse_args()
    raw_bytes = args.capture.read_bytes()
    channels = parse_channels(args.channels, args.channel_mode)
    image_channels = parse_channels(args.image_channels, args.channel_mode)
    decoded = decode_capture(
        raw_bytes,
        signed=args.signed,
        channel_mode=args.channel_mode,
        record_offset=args.record_offset,
        start_record=args.start_record,
        max_records=args.max_records,
    )
    data = extract_traces(decoded, channels, args.channel_mode)
    raw_records = decoded["records"]
    trailing = decoded["trailing_bytes"]
    records = decoded["samples"].shape[0] if data else 0
    need_heatmap = (
        args.image_out is not None
        or args.image_show
        or ((args.out is not None or args.show) and not args.no_heatmap)
    )
    heatmap = extract_traces(decoded, all_channels(args.channel_mode), args.channel_mode) if need_heatmap else {}

    if args.remove_dc:
        remove_dc_offsets(data)
        remove_dc_offsets(heatmap)

    print(f"capture={args.capture}")
    print(f"bytes={len(raw_bytes)} record_bytes={AFE_PAIR_BYTES} raw_records={raw_records} parsed_samples={records} trailing_bytes={trailing}")
    print(f"sample_rate={raw.FS:g} signed={args.signed} remove_dc={args.remove_dc}")
    for channel, values in data.items():
        print(summarize_trace(channel_label(channel, args.channel_mode), values, raw.FS))

    draw = lambda out, heat, show: plot_png(  # noqa: E731
        out, data, heatmap, records, raw.FS, args.capture.name, args.channel_mode, heat, show=show
    )

    if args.csv_out is not None:
        write_csv(args.csv_out, data, records, args.channel_mode)
        print(f"wrote_csv={args.csv_out}")

    if args.image_out is not None or args.image_show:
        write_channel_step_image(
            args.image_out,
            heatmap,
            records,
            raw.FS,
            args.capture.name,
            args.channel_mode,
            image_channels,
            signed=args.signed,
            use_agg=not args.image_show,
            show=args.image_show,
        )
        if args.image_out is not None:
            print(f"wrote_image={args.image_out}")

    if args.out is not None:
        saved_paths = draw(args.out, not args.no_heatmap, args.show)
        out_kind = "svg" if args.out.suffix.lower() == ".svg" else "png"
        for plot_kind, plot_path in saved_paths:
            print(f"wrote_{plot_kind}_{out_kind}={plot_path}")
    elif args.show:
        draw(None, not args.no_heatmap, True)


if __name__ == "__main__":
    main()
