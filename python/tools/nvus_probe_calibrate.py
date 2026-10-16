#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import argparse
import csv
import json
import math
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from nvus import raw  # noqa: E402

# TX beamformer clock; TX delays quantize to half cycles of this clock.
TX_BF_CLOCK = 200e6
MAX_SHIFT_US = 10.0


def unpack_physical_capture(path, *, rx_select):
    data = path.read_bytes()
    samples, info = raw.unpack_records(data)
    samples = samples & raw.SAMPLE_MASK

    traces = {}
    channels = {}
    for afe in range(raw.AFES_PER_RECORD):
        for physical_channel in range(raw.CHANNELS_PER_AFE):
            # element_index is 0-based; calibration files speak 1-based elements
            element = raw.element_index(afe, physical_channel, rx_select) + 1
            traces[element] = samples[:, afe * raw.CHANNELS_PER_AFE + physical_channel].tolist()
            channels[element] = {
                "rx_select": rx_select,
                "afe": raw.AFE_NAMES[afe],
                "afe_index": afe,
                "physical_channel": physical_channel,
            }

    return {
        "path": str(path),
        "bytes": len(data),
        "raw_records": info["records"],
        "parsed_samples_per_element": info["samples"],
        "trailing_bytes": info["trailing_bytes"],
        "traces": traces,
        "channels": channels,
    }


def mean(values):
    return sum(values) / len(values) if values else 0.0


def rms(values):
    if not values:
        return 0.0
    return math.sqrt(sum(value * value for value in values) / len(values))


def parse_bandpass(text):
    if text is None:
        return None
    try:
        low_text, high_text = text.split(",", 1)
        low = float(low_text)
        high = float(high_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("bandpass must be LOW,HIGH in Hz") from exc
    if low < 0 or high <= low:
        raise argparse.ArgumentTypeError("bandpass must satisfy 0 <= LOW < HIGH")
    return low, high


def bandpass_fft(values, sample_rate, bandpass):
    if bandpass is None:
        return values
    arr = np.asarray(values, dtype=float)
    spectrum = np.fft.rfft(arr)
    freqs = np.fft.rfftfreq(arr.size, d=1.0 / sample_rate)
    low, high = bandpass
    spectrum[(freqs < low) | (freqs > high)] = 0.0
    return np.fft.irfft(spectrum, n=arr.size).tolist()


def envelope(values):
    arr = np.asarray(values, dtype=float)
    n = arr.size
    if n == 0:
        return arr
    spectrum = np.fft.fft(arr)
    weights = np.zeros(n)
    if n % 2 == 0:
        weights[0] = 1.0
        weights[n // 2] = 1.0
        weights[1 : n // 2] = 2.0
    else:
        weights[0] = 1.0
        weights[1 : (n + 1) // 2] = 2.0
    return np.abs(np.fft.ifft(spectrum * weights))


def parabolic_peak_delta(y0, y1, y2):
    denom = y0 - 2.0 * y1 + y2
    if denom == 0.0:
        return 0.0
    delta = 0.5 * (y0 - y2) / denom
    if not -1.0 <= delta <= 1.0:
        return 0.0
    return delta


def xcorr_delay_samples(values, reference, max_lag_samples):
    arr = np.asarray(values, dtype=float)
    ref = np.asarray(reference, dtype=float)
    arr = arr - float(np.mean(arr))
    ref = ref - float(np.mean(ref))
    denom = float(np.linalg.norm(arr) * float(np.linalg.norm(ref)))
    if denom == 0.0:
        return 0.0, 0.0

    corr = np.correlate(arr, ref, mode="full")
    lags = np.arange(-(ref.size - 1), arr.size)
    mag = np.abs(corr)
    keep = np.abs(lags) <= max_lag_samples
    lags = lags[keep]
    mag = mag[keep]

    peak = int(np.argmax(mag))
    lag = float(lags[peak])
    if 0 < peak < mag.size - 1:
        lag += parabolic_peak_delta(float(mag[peak - 1]), float(mag[peak]), float(mag[peak + 1]))
    confidence = float(mag[peak] / denom)
    return lag, confidence


def peak_index(values):
    if not values:
        return 0.0, 0.0
    best = max(range(len(values)), key=lambda idx: abs(values[idx]))
    return float(best), float(abs(values[best]))


def analyze(traces, channels, args):
    min_len = min((len(values) for values in traces.values()), default=0)
    start_sample = max(0, int(round(args.start_us * 1e-6 * raw.FS)))
    if args.end_us is None:
        end_sample = min_len
    else:
        end_sample = int(round(args.end_us * 1e-6 * raw.FS))
    end_sample = min(min_len, max(start_sample, end_sample))
    if end_sample <= start_sample:
        raise ValueError("selected calibration window is empty")

    processed = {}
    for element, values in traces.items():
        window = [float(value) for value in values[start_sample:end_sample]]
        raw_mean = mean(window)
        window = [value - raw_mean for value in window]
        if args.bandpass is not None:
            window = bandpass_fft(window, raw.FS, args.bandpass)
        sig = envelope(window).tolist()
        peak_pos, peak_abs = peak_index(sig)
        processed[element] = {
            "window": window,
            "signal": sig,
            "raw_mean": raw_mean,
            "rms": rms(window),
            "peak_index": peak_pos,
            "peak_abs": peak_abs,
        }

    reference_element = max(processed, key=lambda element: processed[element]["peak_abs"])
    reference = processed[reference_element]

    max_lag_samples = max(1, int(round(MAX_SHIFT_US * 1e-6 * raw.FS)))

    ref_signal = reference["signal"]
    for element, item in processed.items():
        lag, confidence = xcorr_delay_samples(item["signal"], ref_signal, max_lag_samples)
        item["delay_samples"] = lag
        item["confidence"] = confidence

    results = []
    for element in sorted(processed):
        item = processed[element]
        delay_samples = float(item["delay_samples"])
        delay_ns = delay_samples / raw.FS * 1e9
        peak_time_us = (start_sample + item["peak_index"]) / raw.FS * 1e6
        results.append(
            {
                "element": element,
                **channels[element],
                "delay_samples_vs_reference": delay_samples,
                "delay_ns_vs_reference": delay_ns,
                "peak_sample_in_window": item["peak_index"],
                "peak_time_us": peak_time_us,
                "peak_abs": float(item["peak_abs"]),
                "rms": float(item["rms"]),
                "mean_before_dc": float(item["raw_mean"]),
                "confidence": float(item["confidence"]),
            }
        )

    if results:
        max_delay_ns = max(row["delay_ns_vs_reference"] for row in results)
        half_bf_period_ns = 1e9 / (2.0 * TX_BF_CLOCK)
        for row in results:
            comp_delay_ns = max_delay_ns - row["delay_ns_vs_reference"]
            half_cycles = int(round(comp_delay_ns / half_bf_period_ns))
            row["nonnegative_comp_delay_ns_to_latest"] = comp_delay_ns
            row["tx_bf_half_cycles_rounded"] = half_cycles
            row["tx_bf_delay_ns_rounded"] = half_cycles * half_bf_period_ns
            row["postprocess_shift_samples_to_reference"] = -row["delay_samples_vs_reference"]

    return {
        "reference_element": reference_element,
        "start_sample": start_sample,
        "end_sample": end_sample,
        "samples_per_element": min_len,
        "window_samples": end_sample - start_sample,
        "results": results,
    }


def merge_captures(captures):
    traces = {}
    channels = {}
    inputs = []
    for capture in captures:
        inputs.append({key: capture[key] for key in ("path", "bytes", "raw_records", "parsed_samples_per_element", "trailing_bytes")})
        overlap = set(traces).intersection(capture["traces"])
        if overlap:
            raise ValueError(f"duplicate element(s) in captures: {sorted(overlap)[:8]}")
        traces.update(capture["traces"])
        channels.update(capture["channels"])
    return traces, channels, inputs


def write_csv(path, results):
    if not results:
        return
    fieldnames = list(results[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)


def write_plot(path, calibration, title, show=False):
    try:
        import matplotlib
        if not show:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError("plotting requires matplotlib") from exc

    results = calibration["results"]
    elements = [row["element"] for row in results]
    delays = [row["delay_ns_vs_reference"] for row in results]
    comp = [row["nonnegative_comp_delay_ns_to_latest"] for row in results]
    peaks = [row["peak_abs"] for row in results]
    confidence = [row["confidence"] for row in results]

    panels = (
        (delays, "delay vs ref (ns)", None),
        (comp, "delay to latest (ns)", "tab:orange"),
        (peaks, "peak abs", "tab:green"),
        (confidence, "confidence", "tab:red"),
    )
    fig, axes = plt.subplots(4, 1, figsize=(14, 10), sharex=True, constrained_layout=True)
    for ax, (values, ylabel, color) in zip(axes, panels):
        ax.plot(elements, values, marker=".", linewidth=1.0, color=color)
        ax.set_ylabel(ylabel)
        ax.grid(True, alpha=0.3)
        ax.axvline(64.5, color="black", linewidth=0.8, alpha=0.35)
    axes[0].axhline(0.0, color="black", linewidth=0.8, alpha=0.4)
    axes[0].set_title(title)
    axes[3].set_xlabel("probe element")
    axes[3].set_ylim(-0.05, 1.05)
    if path is not None:
        fig.savefig(path, dpi=150)
    if show:
        plt.show()
    else:
        plt.close(fig)


def print_summary(calibration, sample_rate):
    results = calibration["results"]
    if not results:
        print("no calibration results")
        return
    delays = [row["delay_ns_vs_reference"] for row in results]
    comp = [row["nonnegative_comp_delay_ns_to_latest"] for row in results]
    confidences = [row["confidence"] for row in results]
    peaks = [row["peak_abs"] for row in results]
    print(f"elements={len(results)} reference_element={calibration['reference_element']}")
    print(
        "window="
        f"samples {calibration['start_sample']}..{calibration['end_sample'] - 1} "
        f"({calibration['window_samples'] / sample_rate * 1e6:.3f} us)"
    )
    print(
        "delay_ns="
        f"min {min(delays):.3f} max {max(delays):.3f} pp {max(delays) - min(delays):.3f} "
        f"comp_max {max(comp):.3f}"
    )
    print(f"peak_abs=min {min(peaks):.3f} max {max(peaks):.3f}")
    print(f"confidence=min {min(confidences):.3f} median {sorted(confidences)[len(confidences)//2]:.3f}")
    weak = sorted(results, key=lambda row: (row["confidence"], row["peak_abs"]))[:8]
    print("lowest_confidence=" + ", ".join(f"e{row['element']}:{row['confidence']:.3f}" for row in weak))


def parse_args():
    parser = argparse.ArgumentParser(
        description="Estimate per-element timing offsets from NVUS probe odd/even raw captures.",
    )
    parser.add_argument("--odd", type=Path, default=None, help="Raw frame captured with --tx-probe-rx-select odd")
    parser.add_argument("--even", type=Path, default=None, help="Raw frame captured with --tx-probe-rx-select even")
    parser.add_argument("--start-us", type=float, default=0.0, help="Calibration window start time in physical-channel samples")
    parser.add_argument("--end-us", type=float, default=None, help="Calibration window end time in physical-channel samples")
    parser.add_argument("--bandpass", type=parse_bandpass, default=None, help="Optional FFT bandpass LOW,HIGH in Hz")
    parser.add_argument("--out-json", type=Path, default=None)
    parser.add_argument("--out-csv", type=Path, default=None)
    parser.add_argument("--out-png", type=Path, default=None)
    parser.add_argument("--show", action="store_true", help="Show the matplotlib summary plot interactively")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.odd is None and args.even is None:
        raise SystemExit("at least one of --odd or --even is required")

    captures = [
        unpack_physical_capture(path, rx_select=rx_select)
        for rx_select, path in (("odd", args.odd), ("even", args.even))
        if path is not None
    ]

    traces, channels, inputs = merge_captures(captures)
    calibration = analyze(traces, channels, args)
    output = {
        "inputs": inputs,
        "sample_rate": raw.FS,
        "tx_bf_clock": TX_BF_CLOCK,
        "tx_bf_half_period_ns": 1e9 / (2.0 * TX_BF_CLOCK),
        "method": "envelope-xcorr",
        "reference_request": "strongest",
        "bandpass_hz": args.bandpass,
        "max_shift_us": MAX_SHIFT_US,
        "remove_dc": True,
        # Fixed by raw.unpack_records defaults: even physical channels ride even records.
        "first_physical_channel": "even",
        "record_offset": 0,
        "start_record": 0,
        **calibration,
    }

    print_summary(output, raw.FS)
    if args.out_json is not None:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote_json={args.out_json}")
    if args.out_csv is not None:
        args.out_csv.parent.mkdir(parents=True, exist_ok=True)
        write_csv(args.out_csv, output["results"])
        print(f"wrote_csv={args.out_csv}")
    if args.out_png is not None or args.show:
        if args.out_png is not None:
            args.out_png.parent.mkdir(parents=True, exist_ok=True)
        try:
            write_plot(args.out_png, output, "NVUS probe timing calibration", show=args.show)
        except RuntimeError as exc:
            if args.show:
                raise
            print(f"warning_png={exc}")
        else:
            if args.out_png is not None:
                print(f"wrote_png={args.out_png}")


if __name__ == "__main__":
    main()
