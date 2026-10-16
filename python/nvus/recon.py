# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Functional CUDA reconstruction for NVUS LVDS frames (cupy only).

Build one frozen plan with make_plan(); every per-frame function is
f(data, plan, scalars) -> arrays. Session scalars (onset, norm_ref) are
returned to the caller and passed back in - nothing is stored per frame.
"""

from types import SimpleNamespace

import cupy as cp
import numpy as np

from .raw import (AFES_PER_RECORD, CHANNELS_PER_AFE, FC, FS, LANES_PER_AFE, N_ELEMENTS,
                  PITCH_M, element_indices)
from .units import as_samples, samples_to_depth

N_CH = AFES_PER_RECORD * CHANNELS_PER_AFE  # channels per capture (one RX mux state)
RECORD_WORDS = AFES_PER_RECORD * LANES_PER_AFE  # lane words per 48-byte record
RX_LPF_CUTOFF_HZ = 3.25e6 / 2  # RX passband edge; every demod low-pass must share it

DAS_KERNEL = r"""
extern "C" __global__ void das_pw0(
    const float2* __restrict__ iq,   // (n_ax, n_el) baseband samples
    const float* __restrict__ el_x,  // (n_el) element x positions [m]
    const float* __restrict__ px_x,  // (nx) pixel x [m]
    const float* __restrict__ px_z,  // (nz) pixel z [m]
    float2* __restrict__ out,        // (nz, nx) complex sum
    const int n_ax, const int n_el, const int nx, const int nz,
    const float fs, const float fd, const float inv_c,
    const float t_offset,            // t_peak - initial_time  [s]
    const float t_peak,              // transmit burst center [s]
    const float inv_2fnum,           // 1 / (2 * f_number)
    const float sin_t, const float cos_t,
    const float t0_ref)              // firing reference: -min(el_x*sin_t)/c
{
    const int px = blockIdx.x * blockDim.x + threadIdx.x;
    const int pz = blockIdx.y * blockDim.y + threadIdx.y;
    if (px >= nx || pz >= nz) return;

    const float x = px_x[px];
    const float z = px_z[pz];
    const float tau_tx = (z * cos_t + x * sin_t) * inv_c + t0_ref;  // steered plane wave
    const float half_ap = z * inv_2fnum;     // dynamic receive aperture
    const float two_pi_fd = 6.283185307179586f * fd;

    float re = 0.f, im = 0.f;
    for (int e = 0; e < n_el; ++e) {
        const float dx = x - el_x[e];
        if (fabsf(dx) > half_ap) continue;
        const float tau = tau_tx + sqrtf(dx * dx + z * z) * inv_c;
        // sample index relative to the record (t_offset folds in initial_time
        // and the burst-center t_peak correction)
        const float s = (tau + t_offset) * fs;
        const int i0 = (int)floorf(s);
        if (i0 < 0 || i0 + 1 >= n_ax) continue;
        const float w = s - (float)i0;
        const float2 a = iq[(size_t)i0 * n_el + e];
        const float2 b = iq[(size_t)(i0 + 1) * n_el + e];
        const float sr = a.x + w * (b.x - a.x);
        const float si = a.y + w * (b.y - a.y);
        // undo the demodulation carrier phase at the echo time
        const float ph = two_pi_fd * (tau + t_peak);
        float cp_, sp_;
        sincosf(ph, &sp_, &cp_);
        re += sr * cp_ - si * sp_;
        im += sr * sp_ + si * cp_;
    }
    out[(size_t)pz * nx + px] = make_float2(re, im);
}
"""


def make_plan(depth=None, time=None, samples=None, c=1480.0, demod_freq=FC,
              f_number=1.0, rx_half="odd", grid_frequency_hz=FC):
    """Precompute everything per-frame recon needs; pure function of its args.

    rx_half names the TR_SW_SEL state the frames were captured under: "odd"
    (elements 0-63), "even" (64-127), or "both" for joined 128-element frames
    (bmode_pair / full-aperture sweep). Element positions come from
    raw.element_indices. Note "both" joins two shots' captures: live
    full-aperture imaging transmits twice per displayed frame (BModeOp warns
    about the acoustic-output implication).

    The pixel grid uses grid_frequency_hz (historically 5 MHz) for matched
    frequency comparisons. Acoustic wavelength/demodulation use demod_freq.
    """
    from scipy.signal import firwin

    if not np.isfinite([demod_freq, grid_frequency_hz]).all() or not (
            0 < demod_freq < FS/2 and grid_frequency_hz > 0):
        raise ValueError("positive finite grid frequency and sub-Nyquist demodulation required")
    if rx_half not in ("odd", "even", "both"):
        raise ValueError(f'rx_half must be "odd", "even", or "both", not {rx_half!r}')
    halves = ("odd", "even") if rx_half == "both" else (rx_half,)
    columns = [e for half in halves for e in element_indices(half)]
    n_elements = len(columns)

    n_keep = as_samples(depth=depth, time=time, samples=samples, c=c, fs=FS) + 256
    if depth is None:
        depth = samples_to_depth(n_keep - 256, c=c, fs=FS)

    # element 0 at -x
    el_x_np = ((np.arange(N_ELEMENTS) - (N_ELEMENTS - 1) / 2) * PITCH_M).astype(np.float32)[columns]
    d = c / grid_frequency_hz / 4
    half_ap = N_ELEMENTS * PITCH_M / 2  # image the full probe aperture
    px_x = cp.arange(-half_ap, half_ap, d, dtype=cp.float32)
    px_z = cp.arange(1e-3, depth, d, dtype=cp.float32)
    nx, nz = int(px_x.size), int(px_z.size)

    # demodulation phasor and low-pass taps for the kept window
    t = cp.arange(n_keep, dtype=cp.float32) / FS
    taps = firwin(127, RX_LPF_CUTOFF_HZ, fs=FS).astype(np.float32)  # RX passband
    # complex spectra: IQ data needs the full FFT, not rfft
    f_cyc = cp.fft.fftfreq(n_keep)

    t_peak = 0.5 * 5.0 / demod_freq  # historical five-cycle default
    group_delay = 63.0 / FS  # linear-phase FIR (127 taps)
    block = (16, 16)
    # pre-warm the cufft plan cache so the first frame doesn't hitch
    cp.fft.fft(cp.zeros((n_keep, n_elements), cp.complex64), axis=0)
    return SimpleNamespace(
        c=c, fd=demod_freq, center_freq=demod_freq, grid_frequency_hz=grid_frequency_hz,
        f_number=f_number, n_keep=n_keep,
        el_x=cp.asarray(el_x_np), el_x_np=el_x_np,
        # the TRANSMIT is always the full 128-element aperture; steered t0
        # must reference its launch edge even when receiving one mux half
        tx_el_x_np=((np.arange(N_ELEMENTS) - (N_ELEMENTS - 1) / 2) * PITCH_M).astype(np.float32),
        px_x=px_x, px_z=px_z, nx=nx, nz=nz,
        demod=cp.exp(-2j * np.pi * demod_freq * t).astype(cp.complex64)[:, None],
        lpf_f=cp.fft.fft(cp.asarray(taps), n=n_keep)[:, None].astype(cp.complex64),
        # half-sample alignment for odd-phase channels, applied in the same FFT pass
        half_shift=cp.exp(-2j * np.pi * f_cyc * 0.5)[:, None].astype(cp.complex64),
        odd_cols=cp.asarray(
            [a * 32 + ch for a in range(2) for ch in range(32) if ch & 1], dtype=cp.int32
        ),
        t_peak=t_peak, system_delay_s=0.0,
        t_offset=np.float32(t_peak + group_delay),
        t0_ref=_t0_ref(el_x_np, 0.0, c),
        kernel=cp.RawKernel(DAS_KERNEL, "das_pw0"),
        block=block,
        grid=((nx + block[0] - 1) // block[0], (nz + block[1] - 1) // block[1]),
        extent=(float(px_x[0]) * 1e3, float(px_x[-1]) * 1e3,
                float(px_z[-1]) * 1e3, float(px_z[0]) * 1e3),
    )



def set_plan_frequency(plan, frequency_hz, *, burst_cycles=5):
    """Change the carrier/pulse center while preserving grid and LPF bandwidth.

    Call before constructing beamformers or capturing CUDA graphs. IQ samples
    retain the same time origin; common residual system delay is unchanged.
    """
    if not np.isfinite([frequency_hz, burst_cycles]).all() or not (
            0 < frequency_hz < FS/2 and burst_cycles > 0):
        raise ValueError("positive burst cycles and sub-Nyquist carrier required")
    plan.fd = plan.center_freq = float(frequency_hz)
    t = cp.arange(plan.n_keep, dtype=cp.float32)/FS
    plan.demod = cp.exp(-2j*np.pi*frequency_hz*t).astype(cp.complex64)[:, None]
    plan.t_peak = .5*burst_cycles/frequency_hz
    plan.t_offset = np.float32(plan.t_peak + 63/FS)
    return plan

def unpack(data, subtract_dc=True):
    """Raw frame bytes/GPU u8 -> (n_samples, 64) float32 on GPU, capture spike zeroed.

    subtract_dc removes the per-channel DC median (one GPU sort per frame);
    callers that maintain their own cached DC estimate pass False."""
    b = (cp.asarray(np.frombuffer(data, dtype=np.uint8))
         if isinstance(data, (bytes, bytearray)) else cp.asarray(data, dtype=cp.uint8))
    b = b[: b.size - b.size % 3].reshape(-1, 3).astype(cp.uint32)
    s1 = b[:, 0] | ((b[:, 1] & 0x0F) << 8)
    s2 = (b[:, 1] >> 4) | (b[:, 2] << 4)
    w = cp.empty(s1.size * 2, dtype=cp.int32)
    w[0::2] = s1
    w[1::2] = s2
    w = cp.where(w >= 2048, w - 4096, w).astype(cp.float32)
    w = w[: w.size - w.size % (4 * RECORD_WORDS)].reshape(-1, 2, 2, 2, 16)
    out = cp.ascontiguousarray(w.transpose(0, 1, 3, 4, 2)).reshape(-1, N_CH)
    if subtract_dc:
        out -= cp.median(out[out.shape[0] // 2:], axis=0)
    out[:8] = 0  # start-of-record capture spike
    return out


def detect_onset(rf):
    """TX drive-leakage leading edge, relative to its own peak in the record head."""
    env = cp.abs(rf).mean(axis=1)
    k = cp.ones(51, dtype=cp.float32) / 51
    env = cp.convolve(env, k, mode="same")
    # The drive leakage sits in the record head (~2.6 us after start).
    # Detect its leading edge relative to its own peak: a whole-record
    # median floor moves with scene content and slid the threshold
    # crossing by ~100 samples shot-to-shot on identical transmits.
    head = env[:512]
    peak = float(head.max())
    floor = float(cp.median(env))
    if peak > 8 * floor:
        idx = cp.where(head > 0.5 * peak)[0]
        if idx.size:
            return int(idx[0])
    idx = cp.where(env > 4 * floor)[0]
    return int(idx[0]) if idx.size else 0


def to_iq(rf, onset, plan):
    """Trim at onset, demodulate, FFT low-pass, half-sample align odd channels."""
    seg = rf[onset: onset + plan.n_keep]
    if seg.shape[0] < plan.n_keep:
        seg = cp.pad(seg, ((0, plan.n_keep - seg.shape[0]), (0, 0)))
    iq = (seg * plan.demod).astype(cp.complex64, copy=False)
    spec = cp.fft.fft(iq, axis=0)
    spec *= plan.lpf_f
    spec[:, plan.odd_cols] *= plan.half_shift
    return cp.fft.ifft(spec, axis=0).astype(cp.complex64, copy=False)


def _t0_ref(el_x_np, angle_rad, c):
    """Firing reference: -min(el_x*sin)/c, host-side."""
    return float(-(el_x_np * np.sin(angle_rad)).min() / c)


def timing_offsets(plan, *, centered_fir=False):
    """Sample and demodulation-phase offsets including residual system delay.

    Positive delay samples later at the same physical coordinate. Keep burst
    center separate: focused resolution width still depends on pulse duration.
    """
    delay = getattr(plan, "system_delay_s", 0.0)
    if not np.isfinite(delay):
        raise ValueError("system delay must be finite")
    sample = plan.t_peak if centered_fir else plan.t_offset
    return np.float32(sample + delay), np.float32(plan.t_peak + delay)


def beamform(iq, plan, angle_rad=0.0):
    """Plane-wave DAS -> (nz, nx) complex64."""
    t0_ref = plan.t0_ref if angle_rad == 0.0 else _t0_ref(plan.tx_el_x_np, angle_rad, plan.c)
    iq = cp.ascontiguousarray(iq, dtype=cp.complex64)
    if iq.shape[1] != plan.el_x.size:
        raise ValueError(
            f"iq has {iq.shape[1]} channels but plan has {plan.el_x.size}; "
            f'build the plan with rx_half="both" for joined frames or a half name for one mux state'
        )
    out = cp.empty((plan.nz, plan.nx), dtype=cp.complex64)
    sample_offset, phase_offset = timing_offsets(plan)
    plan.kernel(
        plan.grid,
        plan.block,
        (
            iq,
            plan.el_x,
            plan.px_x,
            plan.px_z,
            out,
            np.int32(plan.n_keep),
            np.int32(plan.el_x.size),
            np.int32(plan.nx),
            np.int32(plan.nz),
            np.float32(FS),
            np.float32(plan.fd),
            np.float32(1.0 / plan.c),
            sample_offset,
            phase_offset,
            np.float32(1.0 / (2 * plan.f_number)),
            np.float32(np.sin(angle_rad)),
            np.float32(np.cos(angle_rad)),
            np.float32(t0_ref),
        ),
    )
    return out


def norm_reference(image, percentile=99.5):
    """0-dB reference scalar; caller memoizes it for the session."""
    return cp.percentile(image, percentile)


def to_db(image, norm_ref):
    """Envelope -> dB re norm_ref, clipped at 0."""
    return cp.minimum(20 * cp.log10(image / norm_ref + 1e-12), 0.0)


def to_gray(img_db):
    """[-50, 0] dB -> contiguous (nz, nx, 1) float32 in [0, 1] for Holoviz."""
    gray = cp.clip(img_db / 50.0 + 1.0, 0.0, 1.0).astype(cp.float32, copy=False)
    return cp.ascontiguousarray(gray[..., None])


def extent_mm(plan):
    """(x0, x1, z1, z0) display extent [mm] for imshow."""
    return plan.extent


def bmode(data, plan, onset=None, norm_ref=None):
    """One 64-channel frame end-to-end -> (img_db, onset, norm_ref)."""
    rf = unpack(data)
    onset = detect_onset(rf) if onset is None else onset
    iq = to_iq(rf, onset, plan)
    env = cp.abs(beamform(iq, plan))
    norm_ref = norm_reference(env) if norm_ref is None else norm_ref
    return to_db(env, norm_ref), onset, norm_ref


def bmode_pair(data_even, data_odd, plan, onsets=None, norm_ref=None):
    """Interleaved halves -> 128-element (img_db, (onset_even, onset_odd), norm_ref).

    args are (even, odd); concat order is [odd | even] -> elements 0-63 | 64-127.
    """
    onset_even, onset_odd = onsets if onsets is not None else (None, None)
    parts = []
    rf = unpack(data_odd)
    onset_odd = detect_onset(rf) if onset_odd is None else onset_odd
    parts.append(to_iq(rf, onset_odd, plan))
    rf = unpack(data_even)
    onset_even = detect_onset(rf) if onset_even is None else onset_even
    parts.append(to_iq(rf, onset_even, plan))
    iq = cp.concatenate(parts, axis=1)  # [odd: elements 0-63 | even: 64-127]
    env = cp.abs(beamform(iq, plan))
    norm_ref = norm_reference(env) if norm_ref is None else norm_ref
    return to_db(env, norm_ref), (onset_even, onset_odd), norm_ref
