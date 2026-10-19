# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Unchanged live GPU reconstruction, independent of acquisition and Hololink."""
import math
import os
import cupy as cp
import numpy as np
from . import recon

# --- GPU kernels (CUDA C source strings) ---------------------------------------------
# The app launches exactly TWO kernels per frame, the Kernel Factory
# winners compiled from nvus/kernels/ (see the block after the sources):
#   decode_fir31_frame_kernel  whole frame: raw pages -> baseband channel data
#   das_kernel + reduce_kernel all angles -> one compound image
# The sources below are the slower reference forms of the same math
# (decode_baseband, fir31_complex, decode_fir31_shot, das_pw_offset). They
# stay in this file because python/tools/nvus_kernel_bench.py compiles
# them as the parity references that prove the live kernels correct.
# Derived kernels are built by TEXTUAL PATCH of their parent source so the
# pair can never drift: every anchor must match exactly once or we refuse
# to start (a silently skipped patch would be a subtle image regression).


def _derive_kernel(src_text, replacements, parent):
    for old, new in replacements:
        if src_text.count(old) != 1:
            raise RuntimeError(f"{parent} drifted; kernel anchor lost: {old!r}")
        src_text = src_text.replace(old, new)
    return src_text


# das_pw_offset: recon's single-angle DAS plus a per-element delay offset
# (tau_e, seconds) - the odd-column half-sample correction applied to BOTH
# the sample index and the carrier phase - and element-major channel-data
# addressing.
DAS_OFFSET_KERNEL = _derive_kernel(recon.DAS_KERNEL, (
    ("das_pw0", "das_pw_offset"),
    ("const float sin_t, const float cos_t,",
     "const float* __restrict__ el_ds,\n    const float sin_t, const float cos_t,"),
    ("const float s = (tau + t_offset) * fs;",
     "const float tau_e = tau + el_ds[e];\n"
     "        const float s = (tau_e + t_offset) * fs;"),
    ("const float ph = two_pi_fd * (tau + t_peak);",
     "const float ph = two_pi_fd * (tau_e + t_peak);"),
    # element-major channel-data layout (n_el, n_ax): the a/b lerp pair becomes two
    # ADJACENT float2s, matching the live compound kernel's gather addressing
    ("// (n_ax, n_el) baseband samples", "// (n_el, n_ax) baseband samples"),
    ("iq[(size_t)i0 * n_el + e]", "iq[(size_t)e * n_ax + i0]"),
    ("iq[(size_t)(i0 + 1) * n_el + e]", "iq[(size_t)e * n_ax + i0 + 1]"),
), "recon.DAS_KERNEL")

# fir31_complex is the RETAINED REFERENCE for decode_fir31_shot below: the
# harness proves the fused kernel bit-exact against decode_baseband ->
# fir31_complex, so keep both un-drifted. Not launched on the live path.
FIR31_KERNEL = r"""
extern "C" __global__ void fir31_complex(
    const float2* __restrict__ x,   // (n, nch) samples, one shot
    const float* __restrict__ taps, // 31 filter taps, centered (zero group delay)
    float2* __restrict__ y,         // (out_nch, n) element-major destination; this
    const int n, const int nch,     // shot's channels land at whole CHANNEL ROWS
    const int col_off)              // col_off..col_off+nch (row offset, not column)
{
    // sample index i is the fast x-dimension so stores stay fully coalesced
    // along n_ax in the element-major channel data; the input reads are
    // 512B-strided per warp, but threads i and i+1 share 30 of 31 tap
    // addresses, so L1 serves them.
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    const int c = blockIdx.y * blockDim.y + threadIdx.y;
    if (c >= nch || i >= n) return;
    float re = 0.f, im = 0.f;
    for (int k = 0; k < 31; ++k) {
        const int m = i + 15 - k;
        if (m < 0 || m >= n) continue;
        const float t = taps[k];
        const float2 v = x[(size_t)m * nch + c];
        re += t * v.x;
        im += t * v.y;
    }
    y[(size_t)(col_off + c) * n + i] = make_float2(re, im);
}
"""

# Fused per-shot decode: packed 12-bit raw u8 -> complex64 baseband in ONE
# launch, replacing the chained unpack passes (u32 intermediates cost ~65 MB
# of DRAM traffic per shot; this reads the 0.66 MB raw once and writes the
# 3.4 MB IQ once). The gather below inverts recon.unpack's
# reshape(-1,2,2,2,16) + transpose(0,1,3,4,2) exactly; edge rules replicate
# _baseband's original decode->spike-zero->DC->trim->pad->demod order.
DECODE_BB_KERNEL = r"""
extern "C" __global__ void decode_baseband(
    const unsigned char* __restrict__ raw,  // packed frame bytes (2 samples / 3 bytes)
    const float* __restrict__ dc,           // (64) cached per-channel DC
    const float2* __restrict__ demod,       // (n_keep) carrier phasor, contiguous
    float2* __restrict__ iq,                // (n_keep, 64) baseband out
    const int n_keep, const int onset, const int rows_avail)
{
    const int c = threadIdx.x;              // channel: float2 stores coalesce
    const int t = blockIdx.y * blockDim.y + threadIdx.y;
    if (t >= n_keep) return;
    const int g = onset + t;                // source row in the decoded record
    if (g >= rows_avail) {                  // caller pads AFTER the DC subtract,
        iq[(size_t)t * 64 + c] = make_float2(0.f, 0.f);  // so pads are exactly 0
        return;
    }
    float v;
    if (g < 8) {                            // start-of-record capture spike:
        v = 0.f - dc[c];                    // unpack zeroes it BEFORE DC subtract
    } else {
        // out[g,c] with g=2r+i1, c=32*i3+2*i4+i2 reads lane word
        // w = 128r + 64*i1 + 32*i2 + 16*i3 + i4  (inverse of unpack's transpose)
        const int w = (g >> 1) * 128 + (g & 1) * 64 + (c & 1) * 32
                      + (c >> 5) * 16 + ((c & 31) >> 1);
        const int base = 3 * (w >> 1);      // even/odd sample of a 3-byte group
        const int v12 = ((w & 1) == 0)
            ? (raw[base] | ((raw[base + 1] & 0xF) << 8))
            : ((raw[base + 1] >> 4) | (raw[base + 2] << 4));
        v = (float)(v12 >= 2048 ? v12 - 4096 : v12) - dc[c];
    }
    const float2 d = demod[t];              // same f32 op order as (rf-dc)*demod
    iq[(size_t)t * 64 + c] = make_float2(v * d.x, v * d.y);
}
"""

# Fully fused per-shot prep: packed 12-bit raw -> filtered baseband, written
# straight into the element-major channel_data buffer in ONE launch
# (decode + cached-DC + demod + 31-tap FIR). Replaces the
# decode_baseband -> fir31_complex pair on the live path: the pair's 3.4 MB
# IQ intermediate cost a DRAM round trip per shot (~438 MB/frame) and left
# the FIR's input reads 512B-strided (7.58 ms/frame, 4.7x off its roofline).
# Each block owns CBLK channels x TILE output samples. Phase A decodes the
# tile plus a 15-sample halo into shared memory with decode_baseband's EXACT
# per-sample ops (same gather w, same spike/pad edge rules, demod indexed by
# ABSOLUTE sample t so every block sees the same phasor); halo slots with
# t outside [0, n) are left unwritten and predicated out in phase B, exactly
# what fir31's 'continue' produces - never a spurious 0.0f add. Phase B is
# fir31_complex's 31-tap loop verbatim (same k order, same skip semantics,
# same per-term float ops) sourced from smem, stores coalesced along n_ax.
# The 0.66 MB raw is L2-resident, so cross-block halo re-reads are L2
# traffic, not DRAM. TILE/CBLK are compile-time (smem = (TILE+30)*CBLK*8 B).
DECODE_FIR31_KERNEL = r"""
#define TILE FUSED_TILE_PLACEHOLDER
#define CBLK FUSED_CBLK_PLACEHOLDER

extern "C" __global__ void decode_fir31_shot(
    const unsigned char* __restrict__ raw,  // packed frame bytes (2 samples / 3 bytes)
    const float* __restrict__ dc,           // (64) cached per-channel DC
    const float2* __restrict__ demod,       // (n) carrier phasor, contiguous
    const float* __restrict__ taps,         // 31 filter taps, centered
    float2* __restrict__ y,                 // (out_nch, n) element-major channel data
    const int n, const int onset, const int rows_avail,
    const int col_off)                      // whole channel-ROW offset (odd 0, even 64)
{
    __shared__ float2 s[CBLK * (TILE + 30)];
    __shared__ float st[31];
    const int tid = threadIdx.x;
    const int tile0 = blockIdx.x * TILE;
    const int c0 = blockIdx.y * CBLK;
    if (tid < 31) st[tid] = taps[tid];
    // phase A: cooperative decode of this block's (channel, sample) tile+halo
    for (int idx = tid; idx < CBLK * (TILE + 30); idx += blockDim.x) {
        const int cl = idx / (TILE + 30);
        const int t = tile0 - 15 + (idx - cl * (TILE + 30));
        if (t < 0 || t >= n) continue;      // out-of-record: predicated in phase B
        const int c = c0 + cl;
        const int g = onset + t;            // source row in the decoded record
        float2 val;
        if (g >= rows_avail) {              // zero-pad rows ARE summed (as +0
            val = make_float2(0.f, 0.f);    // terms), matching the reference pair
        } else {
            float v;
            if (g < 8) {                    // start-of-record capture spike:
                v = 0.f - dc[c];            // unpack zeroes it BEFORE DC subtract
            } else {
                const int w = (g >> 1) * 128 + (g & 1) * 64 + (c & 1) * 32
                              + (c >> 5) * 16 + ((c & 31) >> 1);
                const int base = 3 * (w >> 1);
                const int v12 = ((w & 1) == 0)
                    ? (raw[base] | ((raw[base + 1] & 0xF) << 8))
                    : ((raw[base + 1] >> 4) | (raw[base + 2] << 4));
                v = (float)(v12 >= 2048 ? v12 - 4096 : v12) - dc[c];
            }
            const float2 d = demod[t];
            val = make_float2(v * d.x, v * d.y);
        }
        s[idx] = val;
    }
    __syncthreads();
    // phase B: fir31_complex's tap loop verbatim, sourced from smem
    for (int idx = tid; idx < CBLK * TILE; idx += blockDim.x) {
        const int cl = idx / TILE;
        const int i = tile0 + (idx - cl * TILE);
        if (i >= n) continue;
        const float2* sc = s + cl * (TILE + 30);  // this channel's halo'd row
        float re = 0.f, im = 0.f;
        for (int k = 0; k < 31; ++k) {
            const int m = i + 15 - k;
            if (m < 0 || m >= n) continue;
            const float t = st[k];
            const float2 v = sc[m - tile0 + 15];
            re += t * v.x;
            im += t * v.y;
        }
        y[(size_t)(col_off + c0 + cl) * n + i] = make_float2(re, im);
    }
}
"""

FUSED_TILE = 128  # output samples per block (A/B candidates: 128, 256)
FUSED_CBLK = 8  # channels per block (A/B candidates: 8, 16)
FUSED_BLOCK = 256  # threads per block, grid-striding over the tile slots

# --- the live reconstruction kernels (Kernel Factory campaign winners) ---------------
# The two kernels the app launches, committed verbatim under kernels/
# and compiled from there at startup: decode 5cf3b266 (fused
# decode+demod+FIR31 frame kernel) and das 1b6eecf4 (chunk-per-block
# phase-factorized compound DAS). Compile flags are the campaign's eval
# flags; nvrtc has no -O3 (device optimization is default-on) but takes
# the rest unchanged. python/tools/nvus_kernel_bench.py times these
# kernels and parity-checks them against the reference forms above.
KF_DIR = os.path.join(os.path.dirname(__file__), "kernels")
KF_DECODE_FLAGS = ("-DXF2", "-DSKIP_ZERO_TAIL", "--use_fast_math")
KF_DAS_FLAGS = ("--use_fast_math", "--maxrregcount=48")
KF_MAX_ANG = 64  # das constant-memory angle-table capacity (its MAX_ANG)
KF_ANGLE_CHUNK = 8  # das angles per grid.z slice (its CHUNK define)
KF_TILE, KF_BLOCK = 64, 512  # decode candidate defaults (TILE, NTHREADS)


def _cut_once(src, marker):
    """Split at a marker that must appear exactly once (candidate-drift
    guard, same contract as _derive_kernel anchors)."""
    if src.count(marker) != 1:
        raise RuntimeError(f"kf candidate source drifted; marker lost: {marker!r}")
    return src.split(marker)


def load_kf_decode_source():
    """kernels/decode.cu as NVRTC device code.

    Drops the unused STORE_X variant (its anchor lines shadow the shipped
    kernel's) and the host launcher, then restores the production ring-page
    pointer table in place of the SOLBench contiguous raw array
    The source is the selected Kernel Factory implementation."""
    with open(os.path.join(KF_DIR, "decode.cu")) as f:
        src = f.read()
    head, rest = _cut_once(src, "#ifdef STORE_X")
    _variant, rest = _cut_once(rest, "#else  // !STORE_X")
    body, _host = _cut_once(rest, "#endif  // STORE_X")
    _, head = _cut_once(head, '#include "kernel.h"\n')
    return _derive_kernel(head + body, (
        ("    const unsigned char* __restrict__ raw,  // (n_shots, page_bytes)",
         "    const unsigned char* const* __restrict__ pages,  "
         "// collected ring-page pointers"),
        ("    const int n_angles, const long long page_bytes)",
         "    const int n_angles)"),
        ("    const unsigned char* __restrict__ page = "
         "raw + (long long)shot * page_bytes;",
         "    const unsigned char* __restrict__ page = pages[shot];"),
    ), "kernels/decode.cu")


def load_kf_das_source(nx, nz, n_el):
    """kernels/das.cu as NVRTC device code: the includes
    and everything host-side go (prep_kernel runs once per plan, so its two
    lines of math move to the host; the launcher's constant-memory uploads
    and its n_ax==3466 -> active_nz=678 shortcut are replaced outright -
    the full NZ is always computed, see META.txt). The candidate's baked
    image geometry is re-rendered for the caller's plan, so any make_plan
    geometry compiles; its block/chunk tuning was searched at the live
    519x1271x128 config, so other geometries are correct but not
    necessarily peak speed."""
    with open(os.path.join(KF_DIR, "das.cu")) as f:
        src = f.read()
    _, src = _cut_once(src, '#include "kernel.h"\n')
    _, src = _cut_once(src, '#include <cuda_runtime.h>\n')
    src, _host = _cut_once(src, "__global__ void prep_kernel")
    return _derive_kernel(src, (
        ("#define NX 519", f"#define NX {int(nx)}"),
        ("#define NZ 1271", f"#define NZ {int(nz)}"),
        ("#define N_EL 128", f"#define N_EL {int(n_el)}"),
    ), "kernels/das.cu")


class Beamformer:
    """The GPU reconstruction pipeline. It has a single path: two kernels,
    each launched once per frame.

    baseband_frame() runs the frame decode kernel, which converts the
    frame's collected pages of raw channel data into baseband channel data.
    compound_image() runs the compound DAS kernel plus its reduce, which
    sum every angle into the image. set_pages() refreshes the page-pointer
    table both of them read.

    Every buffer is sized and allocated once, because a CUDA graph replays
    fixed device addresses. The slower per-shot and per-angle reference
    forms of the same math are compiled by
    python/tools/nvus_kernel_bench.py as parity references.

    Signal representation matches recon.to_iq's FFT chain (image corr 0.995
    on real captures) with no FFTs: a 31-tap centered FIR replaces the
    127-tap FFT-domain filter (zero group delay, so beamforming references
    t_peak instead of plan.t_offset) and the odd-column half-sample ramp is
    the DAS kernel's per-element delay offset.

    Both launches are the Kernel Factory campaign winners compiled from
    nvus/kernels/ for this plan's geometry. The decode candidate skips the
    all-zero tail past rows_avail, so the channel-data buffer must be
    ZERO-INITIALIZED once and persist (cp.zeros, not cp.empty)."""

    def __init__(self, plan):
        from scipy.signal import firwin
        self._plan = plan
        self._ptrs_host = None  # collected page-pointer table (rebuilt per frame)
        self._ptrs_dev = None
        self._rows_avail = None  # frame size is session-fixed; derived once
        self._taps = cp.asarray(
            firwin(31, recon.RX_LPF_CUTOFF_HZ, fs=recon.FS).astype(np.float32))
        n, nch = plan.n_keep, int(plan.el_x.size)
        ds = cp.zeros(nch, cp.float32)
        odd = plan.odd_cols
        ds[odd] = 0.5 / recon.FS  # kernel offset is in seconds (no in-loop divide)
        if nch > 64:
            ds[odd + 64] = 0.5 / recon.FS
        self._el_ds = ds
        self._dc = None  # per-channel DC, measured once per session (like onset)
        self._n = np.int32(n)
        # demod flattened to a contiguous (n,) float2 view: plan.demod is
        # (n_keep, 1) and a strided float2 load would read garbage
        self._demod1d = cp.ascontiguousarray(plan.demod[:n, 0])
        # reference launch geometry + das_pw_offset argument pack below: not
        # used live; borrowed by the bench's ReferenceKernels
        self._fused_grid = ((n + FUSED_TILE - 1) // FUSED_TILE,
                            recon.N_CH // FUSED_CBLK)
        sample_offset, phase_offset = recon.timing_offsets(plan, centered_fir=True)
        self._das_consts = (
            np.int32(n), np.int32(nch), np.int32(plan.nx), np.int32(plan.nz),
            np.float32(recon.FS), np.float32(plan.fd), np.float32(1.0 / plan.c),
            sample_offset,
            phase_offset,
            np.float32(1.0 / (2 * plan.f_number)))
        self._init_kf()

    def _init_kf(self):
        """Compile the live kernels and fill the das candidate's static
        constant memory (element geometry, prep_kernel's per-element
        offsets). The das source is rendered for THIS plan's NX/NZ/N_EL, so
        any make_plan geometry works. The per-sequence angle tables are
        filled lazily on the first compound_image call - a D2D copy, so it
        must happen during the warmup frames, before graph capture (it
        does)."""
        plan = self._plan
        decode_mod = cp.RawModule(code=load_kf_decode_source(),
                                  options=KF_DECODE_FLAGS)
        self._kf_decode = decode_mod.get_function("decode_fir31_frame_kernel")
        das48, das16 = "das_kernel<48, 2>", "das_kernel<16, 8>"
        self._kf_das_mod = cp.RawModule(
            code=load_kf_das_source(plan.nx, plan.nz, int(plan.el_x.size)),
            options=KF_DAS_FLAGS,
            name_expressions=(das48, das16, "reduce_kernel"))
        # the candidate's shape rule minus the active_nz hack: deep workloads
        # (the live config, n_keep 6675) take the 48x2 instantiation
        name, self._kf_das_block = ((das16, (16, 8)) if plan.n_keep <= 3466
                                    else (das48, (48, 2)))
        self._kf_das = self._kf_das_mod.get_function(name)
        self._kf_reduce = self._kf_das_mod.get_function("reduce_kernel")
        sample_offset, phase_offset = recon.timing_offsets(plan, centered_fir=True)
        fs, fd = np.float32(recon.FS), np.float32(plan.fd)
        self._kf_set_const("c_el_x", plan.el_x)
        # prep_kernel's per-element offsets, in fp32 as the kernel computed them
        self._kf_set_const("c_seoff", (self._el_ds + sample_offset) * fs)
        self._kf_set_const("c_uboff", fd * (self._el_ds + phase_offset))
        self._kf_das_scalar_tail = (
            fs, fd, np.float32(1.0 / plan.c),
            sample_offset, phase_offset,
            np.float32(1.0 / (2 * plan.f_number)))
        self._kf_angles_key = None  # sequence whose tables sit in constant memory
        self._kf_partials = None
        self._kf_image = cp.empty((plan.nz, plan.nx), cp.complex64)

    def _kf_set_const(self, name, values):
        src = cp.asarray(values)
        dst = cp.ndarray(src.shape, src.dtype,
                         memptr=self._kf_das_mod.get_global(name))
        dst[...] = src

    def _prep_raw(self, raw):
        """Device u8 view of the frame + rows available after unpack's two
        floors (whole 3-byte groups, then whole 128-word record pages, 64
        channels per row). First call also measures the session's per-channel
        DC via the slow unpack path - one measurement replaces a per-shot
        GPU sort."""
        if self._dc is None:
            rf = recon.unpack(raw, subtract_dc=False)
            self._dc = cp.ascontiguousarray(cp.median(rf[rf.shape[0] // 2:], axis=0))
        b = (cp.asarray(np.frombuffer(raw, dtype=np.uint8))
             if isinstance(raw, (bytes, bytearray)) else cp.asarray(raw, dtype=cp.uint8))
        words = (b.size - b.size % 3) // 3 * 2
        words -= words % (4 * recon.RECORD_WORDS)
        return b, np.int32(words // recon.N_CH)

    def set_pages(self, raw_pages):
        """Refresh the collected page-pointer table. Kept OUT of baseband_frame so a
        CUDA graph can capture the launches: a pageable host->device copy is
        not capturable, and the table's device address never changes."""
        if self._dc is None or self._rows_avail is None:
            _, self._rows_avail = self._prep_raw(raw_pages[0])
        n_shots = len(raw_pages)
        if self._ptrs_host is None or self._ptrs_host.size != n_shots:
            self._ptrs_host = np.empty(n_shots, np.uint64)
            self._ptrs_dev = cp.empty(n_shots, cp.uint64)
        for i, page in enumerate(raw_pages):
            self._ptrs_host[i] = page.data.ptr
        self._ptrs_dev.set(self._ptrs_host)

    def baseband_frame(self, n_shots, onset, channel_data, n_angles):
        """The whole frame's baseband conversion in ONE launch: grid.z walks
        the collected ring pages via the pointer table (left sub-aperture's
        sequence, then right, in angle order). One launch instead of n_shots
        launches: every extra launch against a busy GPU blocks the host.

        The kernel never writes output rows past rows_avail-onset+15 (they
        are exactly zero): channel_data must be zero-initialized once and
        persist, and the grid skips the all-dead tail blocks."""
        n_ax = int(self._n)
        n_eff = max(1, min(n_ax, int(self._rows_avail) - onset + 15))
        self._kf_decode(((n_eff + KF_TILE - 1) // KF_TILE, n_shots, 1),
                        (KF_BLOCK,),
                        (self._ptrs_dev, self._dc, self._demod1d, self._taps,
                         channel_data, np.int32(onset), self._n,
                         self._rows_avail, np.int32(n_angles)))

    def compound_image(self, channel_data, angles):
        """The compound of all angles over the whole (n_angles, n_el, n_keep)
        element-major channel data: one das launch (each grid.z slice
        compounds its angle chunk into a partial image) plus its reduce over
        the full image. Matches the bench's per-angle reference loop to
        ~4e-3 max rel err (corr ~1.0): the candidate reorders the fp32 sum
        and rotates the carrier with fast-math __sincosf. The per-angle
        host math feeding the constant memory is np.float32-identical to
        the reference's."""
        plan = self._plan
        n_angles = len(angles)
        if tuple(angles) != self._kf_angles_key:
            if n_angles > KF_MAX_ANG:
                raise RuntimeError(
                    f"kf das candidate holds at most {KF_MAX_ANG} angles; "
                    f"sequence has {n_angles}")
            self._kf_set_const("c_sin", np.array(
                [np.float32(math.sin(a)) for a in angles], np.float32))
            self._kf_set_const("c_cos", np.array(
                [np.float32(math.cos(a)) for a in angles], np.float32))
            self._kf_set_const("c_t0", np.array(
                [np.float32(recon._t0_ref(plan.tx_el_x_np, a, plan.c))
                 for a in angles], np.float32))
            self._kf_angles_key = tuple(angles)
        n_chunks = (n_angles + KF_ANGLE_CHUNK - 1) // KF_ANGLE_CHUNK
        if self._kf_partials is None or self._kf_partials.shape[0] != n_chunks:
            # persistent workspace: fixed addresses make this capture-safe
            self._kf_partials = cp.empty((n_chunks, plan.nz, plan.nx), cp.complex64)
        bx, by = self._kf_das_block
        self._kf_das(((plan.nx + bx - 1) // bx, (plan.nz + by - 1) // by, n_chunks),
                     self._kf_das_block,
                     (channel_data, plan.px_x, plan.px_z, self._kf_partials,
                      np.int32(n_angles), self._n) + self._kf_das_scalar_tail)
        nred = plan.nz * plan.nx
        self._kf_reduce(((nred + 255) // 256,), (256,),
                        (self._kf_partials, self._kf_image,
                         np.int32(n_chunks), np.int32(nred)))
        return self._kf_image
