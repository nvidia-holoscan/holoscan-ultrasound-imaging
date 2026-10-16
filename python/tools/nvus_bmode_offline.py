#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Offline B-mode: dumped NVUS frame .bin file(s) -> PNG via nvus.recon.

Give one frame (its even/odd partner is auto-detected for the full aperture)
or an explicit even/odd pair.
"""

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import cupy as cp  # noqa: E402

from nvus import recon  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("inputs", nargs="+", help="one or two frame .bin files")
    p.add_argument("--sound-speed", type=float, default=1480.0)
    p.add_argument("--depth", type=float, default=0.095)
    p.add_argument("--timing", action="store_true", help="report per-frame processing time")
    p.add_argument("--out", type=Path, help="output PNG (default alongside input)")
    return p.parse_args()


def mux_of(path):
    return "even" if "even_" in path.name else "odd"


def resolve_inputs(inputs):
    paths = [Path(text) for text in inputs]
    if len(paths) == 1:
        mux = mux_of(paths[0])
        other = "odd" if mux == "even" else "even"
        partner = paths[0].with_name(paths[0].name.replace(f"{mux}_", f"{other}_", 1))
        if partner != paths[0] and partner.exists():
            paths.append(partner)
    return paths


def save_png(out, img, plan, label):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(6, 7))
    im = ax.imshow(cp.asnumpy(img), cmap="gray", vmin=-50, vmax=0,
                   extent=recon.extent_mm(plan), origin="upper")
    ax.set_xlabel("lateral [mm]")
    ax.set_ylabel("depth [mm]")
    ax.set_title(f"NVUS CUDA DAS — {label}")
    fig.colorbar(im, ax=ax, label="dB")
    fig.tight_layout()
    fig.savefig(out, dpi=140)
    print(f"saved {out}")


def main():
    args = parse_args()
    paths = resolve_inputs(args.inputs)
    plan_kw = dict(depth=args.depth, c=args.sound_speed)
    if len(paths) == 2:
        by_mux = {mux_of(path): path.read_bytes() for path in paths}
        if len(by_mux) != 2:
            raise SystemExit("pair inputs must be one even_ and one odd_ frame")
        plan = recon.make_plan(rx_half="both", **plan_kw)
        run = lambda: recon.bmode_pair(by_mux["even"], by_mux["odd"], plan)[0]  # noqa: E731
        label = "full 128-element pair"
    else:
        mux = mux_of(paths[0])
        data = paths[0].read_bytes()
        plan = recon.make_plan(rx_half=mux, **plan_kw)
        run = lambda: recon.bmode(data, plan)[0]  # noqa: E731
        label = f"{mux} half"

    img = run()  # warmup + result
    if args.timing:
        cp.cuda.Stream.null.synchronize()
        t0 = time.perf_counter()
        n = 20
        for _ in range(n):
            run()
        cp.cuda.Stream.null.synchronize()
        dt = (time.perf_counter() - t0) / n
        print(f"per-frame processing: {dt * 1e3:.1f} ms ({label}, {img.shape[1]}x{img.shape[0]} px)")

    stem = paths[0].stem.replace("even_", "").replace("odd_", "")
    out = args.out or paths[0].with_name(stem + "_cuda_bmode.png")
    save_png(out, img, plan, label)


if __name__ == "__main__":
    main()
