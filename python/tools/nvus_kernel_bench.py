#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Standalone GPU kernel benchmark + parity reference for the direct B-mode path.

Times the reusable Beamformer stages in nvus.live_recon on
seeded synthetic raw shots. No hololink hardware, no EVM, no transmit.
Runs identically on a local dev GPU and inside the spark container: the
hardware packages are stubbed via sys.modules.setdefault, so a real
hololink/holoscan install always wins over the stubs.

Stages (median device ms over --reps cp.cuda.Event-timed repetitions,
after warmup):
  decode           recon.unpack(raw, subtract_dc=False)   12-bit decode
  baseband         ReferenceKernels.baseband (reference)  decode + DC + demod
  shot_into        ReferenceKernels.shot_into (reference) baseband + 31-tap FIR
  beamform_1angle  ReferenceKernels.beamform (reference), one das_pw_offset launch
  beamform_all     the live compound: one das-candidate launch + reduce
                   (Kernel Factory 1b6eecf4, all 32 angles)
  frame            one full synthetic frame: 64x shot_into (odd sub-aperture
                   into columns 0-63, then even into 64-127, in live sequence
                   order) + one beamform_all + cp.abs + recon.to_db
  baseband_frame   the live whole-frame decode: one decode-candidate launch
                   (Kernel Factory 5cf3b266)
fps = 1000 / frame_ms.

beamform_all and baseband_frame time the live kernels themselves. Each
run also compares them against the reference chain on the same inputs
(reported as "kf_parity": the frame decode vs the per-shot reference
fill, expected bit-exact; the compound vs the in-order per-angle
reference sum, corr-gated - the candidate's fast-math __sincosf and fp32
reorder put it at ~4e-3 max rel err, corr ~1.0). kf_parity never affects
the --check-ref exit status.

Parity: --save-ref writes the frame's baseband channel data and the final
dB image as .npz; --check-ref recomputes them and prints max rel err +
pearson corr vs the saved reference (exit 1 if any corr < 0.9999). The
.npz keys are "joined" and "image"; existing references stay valid:
"joined" is still produced by the reference decode chain (bit-stable),
and "image" now goes through the live das candidate, which correlates
~0.99999998 with references minted by the retired full-precision
compound - far inside the gate. Everything is seeded and in-order
accumulated.

Usage:
  python3 python/tools/nvus_kernel_bench.py --json --save-ref /path/ref.npz
  python3 python/tools/nvus_kernel_bench.py --check-ref /path/ref.npz
"""

import argparse
import json
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

ONSET = 183  # mirrors the live rig's detected transmit onset
N_ANGLES = 32  # live default: 32-angle full-aperture compound
ANGLE_MAX_DEG = 7.75  # live default steering half-angle
ROW_MARGIN = 64  # spare rows past onset + n_keep, as live captures have
SEED = 20260717
CORR_GATE = 0.9999


def rel_and_corr(cur, base):
    """max |cur-base| / max |base| plus pearson corr, complex split to re/im
    (the same comparison --check-ref applies to the saved references)."""
    a, b = cur.ravel(), base.ravel()
    if np.iscomplexobj(a):
        a = np.concatenate([a.real, a.imag])
        b = np.concatenate([b.real, b.imag])
    a, b = a.astype(np.float64), b.astype(np.float64)
    rel = float(np.max(np.abs(a - b)) / max(float(np.max(np.abs(b))), 1e-30))
    corr = float(np.corrcoef(a, b)[0, 1])
    return {"max_rel_err": rel, "pearson_corr": corr}


class ReferenceKernels:
    """The slower forms the live kernels were fused from, compiled from the
    live_recon kernel sources. They give every fused kernel an executable
    reference, bit-exact except for the compound, which is corr-gated. The
    app itself ships only the fast path."""

    def __init__(self, app, beamformer):
        import cupy as cp
        self._cp = cp
        self._app = app
        self._beamformer = beamformer  # borrow plan, taps, el_ds, demod, DC bootstrap
        plan = beamformer._plan
        n = plan.n_keep
        self._decode = cp.RawKernel(self._app.DECODE_BB_KERNEL, "decode_baseband")
        self._decode.compile()
        self._fir = cp.RawKernel(self._app.FIR31_KERNEL, "fir31_complex")
        self._fir.compile()
        self._shot = cp.RawKernel(
            self._app.DECODE_FIR31_KERNEL
            .replace("FUSED_TILE_PLACEHOLDER", str(self._app.FUSED_TILE))
            .replace("FUSED_CBLK_PLACEHOLDER", str(self._app.FUSED_CBLK)),
            "decode_fir31_shot")
        self._shot.compile()
        self._das = cp.RawKernel(self._app.DAS_OFFSET_KERNEL, "das_pw_offset")
        self._das.compile()
        self._iq = cp.empty((n, self._app.recon.N_CH), cp.complex64)
        self._decode_grid = (1, (n + 15) // 16)
        self._fir_grid = ((n + 15) // 16, (self._app.recon.N_CH + 15) // 16)
        self._n, self._nch = np.int32(n), np.int32(self._app.recon.N_CH)
        self._angle_args = {}

    def baseband(self, raw, onset):
        """decode_baseband: fused decode + DC + demod into the reused IQ buffer."""
        p = self._beamformer
        b, rows_avail = p._prep_raw(raw)
        self._decode(self._decode_grid, (self._app.recon.N_CH, 16),
                     (b, p._dc, p._demod1d, self._iq, self._n,
                      np.int32(onset), rows_avail))
        return self._iq

    def fir31(self, iq, channel_slab, col_off):
        """fir31_complex alone: the bit-exact FIR half of the parity chain."""
        self._fir(self._fir_grid, (16, 16),
                  (iq, self._beamformer._taps, channel_slab, self._n, self._nch,
                   np.int32(col_off)))

    def shot_into(self, raw, onset, channel_slab, col_off):
        """decode_fir31_shot: the per-shot form the live frame kernel is
        derived from. Bit-identical output, one launch per shot."""
        p = self._beamformer
        b, rows_avail = p._prep_raw(raw)
        self._shot(p._fused_grid, (self._app.FUSED_BLOCK,),
                   (b, p._dc, p._demod1d, p._taps, channel_slab,
                    self._n, np.int32(onset), rows_avail, np.int32(col_off)))

    def beamform(self, iq, angle):
        """One-angle DAS (das_pw_offset): the corr-gated reference for the
        live compound kernel."""
        cp = self._cp
        p, plan = self._beamformer, self._beamformer._plan
        args = self._angle_args.get(angle)
        if args is None:
            t0 = self._app.recon._t0_ref(plan.tx_el_x_np, angle, plan.c)
            args = (np.float32(self._app.math.sin(angle)), np.float32(self._app.math.cos(angle)),
                    np.float32(t0))
            self._angle_args[angle] = args
        out = cp.empty((plan.nz, plan.nx), dtype=cp.complex64)
        self._das(plan.grid, plan.block,
                  (iq, plan.el_x, plan.px_x, plan.px_z, out)
                  + p._das_consts + (p._el_ds,) + args)
        return out


def main():
    parser = argparse.ArgumentParser(
        description="Beamformer kernel benchmark + parity reference (synthetic data only).")
    parser.add_argument("--reps", type=int, default=30,
                        help="Timed repetitions per stage after warmup (default 30).")
    parser.add_argument("--json", action="store_true",
                        help="Print only a machine-readable JSON result dict.")
    parser.add_argument("--save-ref", metavar="PATH",
                        help="Save the frame's baseband channel data + final image as .npz "
                             "(keep outside the repo).")
    parser.add_argument("--check-ref", metavar="PATH",
                        help="Compare against a saved reference; exit 1 if corr < 0.9999.")
    parser.add_argument("--profile-loop", type=int, metavar="N", default=0,
                        help="After warmup, run N full synthetic frames back-to-back "
                             "with NVTX ranges and exit, for external profilers such "
                             "as nsys. Skips stage timings and parity.")
    args = parser.parse_args()

    from nvus import live_recon as app, tx
    import cupy as cp
    recon = app.recon

    plan = recon.make_plan(depth=0.095, rx_half="both")
    beamformer = app.Beamformer(plan)
    ref = ReferenceKernels(app, beamformer)
    angles = [math.radians(a)
              for a in tx.plane_wave_angles(N_ANGLES, ANGLE_MAX_DEG)]

    # seeded synthetic raw shots, uploaded once (live raws are already GPU ring
    # pages, so H2D copies must not pollute the timings)
    rows = ONSET + plan.n_keep + ROW_MARGIN
    rng = np.random.default_rng(SEED)
    raw_shots = [cp.asarray(rng.integers(0, 256, size=rows * 96, dtype=np.uint8))
                 for _ in range(2 * N_ANGLES)]

    # element-major baseband channel data, as run_sequence allocates it.
    # Saved references keep the earlier (n_angles, n_keep, n_el) layout under
    # the .npz key "joined", so save/compare go through channel_data_ref_layout()
    # and existing references stay valid bit-for-bit.
    # zeros, as run_sequence allocates it: the live decode kernel never
    # writes the all-zero tail past rows_avail-onset+15
    channel_data = cp.zeros((N_ANGLES, int(plan.el_x.size), plan.n_keep), cp.complex64)
    # the live frame decode's parity output, kept separate so the reference
    # fill in channel_data survives for the same-run comparison
    live_channel_data = cp.zeros_like(channel_data)

    def channel_data_ref_layout():
        return np.ascontiguousarray(cp.asnumpy(channel_data).transpose(0, 2, 1))

    def fill_channel_data():
        si = 0
        for col_off in (0, 64):  # odd sub-aperture then even, as run_sequence fires them
            for k in range(N_ANGLES):
                ref.shot_into(raw_shots[si], ONSET, channel_data[k], col_off)
                si += 1

    def compound_env():
        # the live compound; matches the per-angle beamform loop to ~4e-3
        # max rel err (corr ~1.0), which the corr gate checks
        return cp.abs(beamformer.compound_image(channel_data, angles))

    # warmup: compile kernels, cache DC / angle args / launch consts, and fix
    # the session norm_ref (live memoizes it after the first frame too)
    fill_channel_data()
    norm_ref = recon.norm_reference(compound_env())
    cp.cuda.runtime.deviceSynchronize()

    def frame():
        fill_channel_data()
        return recon.to_db(compound_env(), norm_ref)

    if args.profile_loop:
        try:
            from cupy.cuda import nvtx
        except ImportError:
            nvtx = None
        start, end = cp.cuda.Event(), cp.cuda.Event()
        start.record()
        for i in range(args.profile_loop):
            if nvtx is not None:
                nvtx.RangePush(f"frame_{i}")
            frame()
            if nvtx is not None:
                nvtx.RangePop()
        end.record()
        end.synchronize()
        total_ms = cp.cuda.get_elapsed_time(start, end)
        print(json.dumps({"profile_loop": args.profile_loop,
                          "total_ms": round(float(total_ms), 4),
                          "frame_ms": round(float(total_ms) / args.profile_loop, 4)}))
        sys.exit(0)

    def timed(fn, reps):
        fn()  # per-stage warmup rep
        cp.cuda.runtime.deviceSynchronize()
        start, end = cp.cuda.Event(), cp.cuda.Event()
        samples = []
        for _ in range(reps):
            start.record()
            fn()
            end.record()
            end.synchronize()
            samples.append(cp.cuda.get_elapsed_time(start, end))
        return float(np.median(samples))

    # the two live-composition stages: one batched baseband launch, and the
    # whole frame replayed as one reconstruction graph (mirrors
    # run_sequence's steady state)
    def frame_batched():
        beamformer.set_pages(raw_shots)
        beamformer.baseband_frame(len(raw_shots), ONSET, channel_data, N_ANGLES)
        return recon.to_db(compound_env(), norm_ref)

    frame_batched()  # warm pools/refs so graph capture allocates nothing new
    cp.cuda.runtime.deviceSynchronize()
    graph_stream = cp.cuda.Stream(non_blocking=True)
    with graph_stream:
        beamformer.set_pages(raw_shots)
        graph_stream.begin_capture()
        beamformer.baseband_frame(len(raw_shots), ONSET, channel_data, N_ANGLES)
        _g_env = cp.abs(beamformer.compound_image(channel_data, angles))
        _g_img = recon.to_db(_g_env, norm_ref)  # refs held: graph replays into them
        reconstruction_graph = graph_stream.end_capture()

    def reconstruction_graph_replay():
        with graph_stream:
            beamformer.set_pages(raw_shots)
            reconstruction_graph.launch(graph_stream)
        graph_stream.synchronize()

    stage_ms = {
        "decode": timed(lambda: recon.unpack(raw_shots[0], subtract_dc=False), args.reps),
        "baseband": timed(lambda: ref.baseband(raw_shots[0], ONSET), args.reps),
        "shot_into": timed(lambda: ref.shot_into(raw_shots[0], ONSET, channel_data[0], 0),
                           args.reps),
        "beamform_1angle": timed(lambda: ref.beamform(channel_data[0], angles[0]), args.reps),
        "beamform_all": timed(lambda: beamformer.compound_image(channel_data, angles),
                              args.reps),
        "baseband_frame": timed(lambda: beamformer.baseband_frame(
            len(raw_shots), ONSET, channel_data, N_ANGLES), args.reps),
        "frame_batched": timed(frame_batched, args.reps),
        "reconstruction_graph": timed(reconstruction_graph_replay, args.reps),
    }
    frame_ms = timed(frame, args.reps)

    # Live-kernel parity vs the reference chain on the same run's inputs:
    # the frame decode against the per-shot reference fill (expected
    # bit-exact), and the compound against the in-order per-angle reference
    # sum. channel_data is refilled by frame() below, so this block cannot
    # leak into the saved-reference parity state.
    fill_channel_data()  # reference decode chain
    reference_cd = cp.asnumpy(channel_data)
    reference_img = None
    for k, angle in enumerate(angles):  # in-order angle sum, deterministic
        one = ref.beamform(channel_data[k], angle)
        reference_img = one if reference_img is None else reference_img + one
    reference_img = cp.asnumpy(reference_img)
    beamformer.set_pages(raw_shots)
    beamformer.baseband_frame(len(raw_shots), ONSET, live_channel_data, N_ANGLES)
    live_cd = cp.asnumpy(live_channel_data)
    live_img = cp.asnumpy(beamformer.compound_image(channel_data, angles))
    kf_parity = {}
    for name, cur, base in (("baseband_frame", live_cd, reference_cd),
                            ("beamform_all", live_img, reference_img)):
        p = rel_and_corr(cur, base)
        p["meets_bench_gate"] = bool(p["pearson_corr"] >= CORR_GATE)
        kf_parity[name] = p

    # deterministic parity state: fixed raws + fixed norm_ref, freshly recomputed
    img_db = frame()
    cp.cuda.runtime.deviceSynchronize()

    result = {
        "device": cp.cuda.runtime.getDeviceProperties(0)["name"].decode(),
        "plan": {"n_keep": int(plan.n_keep), "nx": int(plan.nx), "nz": int(plan.nz),
                 "n_el": int(plan.el_x.size), "n_angles": N_ANGLES,
                 "onset": ONSET, "seed": SEED},
        "reps": args.reps,
        "stage_ms": {k: round(v, 4) for k, v in stage_ms.items()},
        "frame_ms": round(frame_ms, 4),
        "fps": round(1000.0 / frame_ms, 2),
        "kf_parity": kf_parity,
    }

    status = 0
    if args.save_ref:
        np.savez(args.save_ref, joined=channel_data_ref_layout(), image=cp.asnumpy(img_db))
        result["saved_ref"] = os.path.abspath(args.save_ref)
    if args.check_ref:
        ref = np.load(args.check_ref)
        parity = {}
        for name, cur in (("joined", channel_data_ref_layout()),
                          ("image", cp.asnumpy(img_db))):
            parity[name] = rel_and_corr(cur, ref[name])
            if parity[name]["pearson_corr"] < CORR_GATE:
                status = 1
        result["parity"] = parity

    if args.json:
        print(json.dumps(result))
    else:
        print(f"device: {result['device']}")
        print(f"plan: n_keep={plan.n_keep} nx={plan.nx} nz={plan.nz} "
              f"n_el={int(plan.el_x.size)} angles={N_ANGLES} reps={args.reps}")
        for k, v in result["stage_ms"].items():
            print(f"{k:>20}: {v:8.4f} ms")
        print(f"{'frame':>20}: {result['frame_ms']:8.4f} ms  ({result['fps']:.2f} fps)")
        for name, p in result["kf_parity"].items():
            gate = ("meets bench gate" if p["meets_bench_gate"]
                    else f"BELOW bench gate ({CORR_GATE})")
            print(f"kf parity {name}: max_rel_err={p['max_rel_err']:.3e} "
                  f"pearson_corr={p['pearson_corr']:.7f}  {gate}")
        if args.check_ref:
            for name, p in result["parity"].items():
                print(f"parity {name}: max_rel_err={p['max_rel_err']:.3e} "
                      f"pearson_corr={p['pearson_corr']:.7f}")
            print("parity: " + ("PASS" if status == 0 else
                                f"FAIL (corr < {CORR_GATE})"))
        if args.save_ref:
            print(f"reference saved: {result['saved_ref']}")
    sys.exit(status)


if __name__ == "__main__":
    main()
