/*
 * SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: Apache-2.0
 */
#include "kernel.h"

#ifndef TILE
#define TILE 64
#endif
#ifndef CBLK
#define CBLK 64
#endif
#ifndef NTHREADS
#define NTHREADS 512
#endif
#ifndef RPT
#define RPT 2
#endif
#define NCH 64
#define HALO 15
#define STILE (TILE + 30)

// All-channel fused decode + centered 31-tap FIR.
// grid = (ceil(n_eff/TILE), n_shots, NCH/CBLK)
// Shared: real decoded scalar v per (channel, sample+halo) as float, plus the
// per-sample demod phasor (shared across channels) as float2.
#ifdef STORE_X
// Variant: stage demodulated complex x = v*demod in shared (float2).
// Phase B then does one 8B shared load + 2 FMA per tap.
extern "C" __global__ void __launch_bounds__(NTHREADS)
decode_fir31_frame_kernel(
    const unsigned char* __restrict__ raw,
    const float* __restrict__ dc,
    const float2* __restrict__ demod,
    const float* __restrict__ taps,
    float2* __restrict__ y,
    const int onset,
    const int n_ax, const int rows_avail,
    const int n_angles, const long long page_bytes)
{
    __shared__ float2 sx[CBLK * STILE];
    __shared__ float  st[31];

    const int tid  = threadIdx.x;
    const int shot = blockIdx.y;
    const int c0   = blockIdx.z * CBLK;
    const unsigned char* __restrict__ page = raw + (long long)shot * page_bytes;
    const int tile0 = blockIdx.x * TILE;

    const int angle = shot % n_angles;
    const int half  = shot / n_angles;
    const long long yrow0 = (long long)angle * 128 + (long long)half * NCH + c0;

    if (tid < 31) st[tid] = taps[tid];

    for (int idx = tid; idx < CBLK * STILE; idx += blockDim.x) {
        const int cl   = idx / STILE;
        const int srel = idx - cl * STILE;
        const int c    = c0 + cl;
        const int t    = tile0 - HALO + srel;
        float2 x = make_float2(0.f, 0.f);
        if (t >= 0 && t < n_ax) {
            const int g = onset + t;
            if (g < rows_avail) {
                float v;
                if (g < 8) {
                    v = 0.f - dc[c];
                } else {
                    const int w = (g >> 1) * 128 + (g & 1) * 64 + (c & 1) * 32
                                  + (c >> 5) * 16 + ((c & 31) >> 1);
                    const int base = 3 * (w >> 1);
                    int v12;
                    if ((w & 1) == 0)
                        v12 = page[base] | ((page[base + 1] & 0xF) << 8);
                    else
                        v12 = (page[base + 1] >> 4) | (page[base + 2] << 4);
                    v = (float)(v12 >= 2048 ? v12 - 4096 : v12) - dc[c];
                }
                const float2 d = __ldg(&demod[t]);
                x = make_float2(v * d.x, v * d.y);
            }
        }
        sx[idx] = x;
    }
    __syncthreads();

    const int ngrp = TILE / RPT;
    for (int idx = tid; idx < CBLK * ngrp; idx += blockDim.x) {
        const int cl = idx / ngrp;
        const int gj = idx - cl * ngrp;
        const int o  = gj * RPT;
        const int i0 = tile0 + o;
        if (i0 >= n_ax) continue;
#ifdef SKIP_ZERO_TAIL
        const int iz = rows_avail - onset + HALO;
        if (i0 >= iz) continue;
#endif
        const float2* sxc = sx + cl * STILE + o;
        float re[RPT], im[RPT];
        #pragma unroll
        for (int r = 0; r < RPT; ++r) { re[r] = 0.f; im[r] = 0.f; }
        #pragma unroll
        for (int k = 0; k < 31; ++k) {
            const float tk = st[k];
            #pragma unroll
            for (int r = 0; r < RPT; ++r) {
                const float2 xv = sxc[r + 30 - k];
                re[r] += tk * xv.x;
                im[r] += tk * xv.y;
            }
        }
        const long long base = (yrow0 + cl) * (long long)n_ax + i0;
        const bool aligned = ((base & 1LL) == 0);
        if (aligned && (i0 + RPT <= n_ax)) {
            #pragma unroll
            for (int r = 0; r < RPT; r += 2)
                *reinterpret_cast<float4*>(&y[base + r]) =
                    make_float4(re[r], im[r], re[r + 1], im[r + 1]);
        } else {
            #pragma unroll
            for (int r = 0; r < RPT; ++r)
                if (i0 + r < n_ax) y[base + r] = make_float2(re[r], im[r]);
        }
    }
}

#else  // !STORE_X

#define SPITCH (STILE + 1)   // pad to keep smem banks conflict-free

extern "C" __global__ void __launch_bounds__(NTHREADS)
decode_fir31_frame_kernel(
    const unsigned char* __restrict__ raw,  // (n_shots, page_bytes)
    const float* __restrict__ dc,           // (64)
    const float2* __restrict__ demod,       // (n_ax)
    const float* __restrict__ taps,         // (31)
    float2* __restrict__ y,                 // (n_angles*128, n_ax)
    const int onset,
    const int n_ax, const int rows_avail,
    const int n_angles, const long long page_bytes)
{
#ifdef XF2
    __shared__ float2 sx[NCH * SPITCH];   // demodulated x = v*demod
#elif defined(XPLANES)
    __shared__ float  sxr[NCH * SPITCH];  // demodulated x, re plane
    __shared__ float  sxi[NCH * SPITCH];  // demodulated x, im plane
#else
    __shared__ float  sv[NCH * SPITCH];   // decoded real value, padded pitch
    __shared__ float2 sd[STILE];          // demod phasor per sample offset
#endif
    __shared__ float  st[31];
    __shared__ float  sdc[NCH];

    const int tid  = threadIdx.x;
    const int shot = blockIdx.y;
    const unsigned char* __restrict__ page = raw + (long long)shot * page_bytes;
    const int tile0 = blockIdx.x * TILE;

    const int angle = shot % n_angles;
    const int half  = shot / n_angles;
    const long long yrow0 = (long long)angle * 128 + (long long)half * NCH;

    if (tid < 31) st[tid] = taps[tid];
    if (tid < NCH) sdc[tid] = dc[tid];
#if !defined(XPLANES) && !defined(XF2)
    for (int srel = tid; srel < STILE; srel += blockDim.x) {
        const int t = tile0 - HALO + srel;
        sd[srel] = (t >= 0 && t < n_ax) ? demod[t] : make_float2(0.f, 0.f);
    }
#endif
    __syncthreads();

    // ---- Phase A: row-based coalesced decode ----
    // Sample row g occupies the contiguous 96 bytes [96g, 96g+96) of the page
    // (64 twelve-bit words, one per channel). Each task decodes one 12-byte
    // segment (8 samples) with three 32-bit loads; segment seg of row g holds
    // channels cbase + 2j, j=0..7.
    for (int task = tid; task < STILE * 8; task += blockDim.x) {
        const int srel = task >> 3;
        const int seg  = task & 7;
        const int t    = tile0 - HALO + srel;
        const int g    = onset + t;
        const int cbase = ((seg >> 2) & 1) | ((seg & 1) << 4) | (((seg >> 1) & 1) << 5);
#ifdef XF2
        float2* svx = sx + (long long)cbase * SPITCH + srel;
        float2 dm = make_float2(0.f, 0.f);
        if (t >= 0 && t < n_ax) dm = __ldg(&demod[t]);
#elif defined(XPLANES)
        float* svr = sxr + (long long)cbase * SPITCH + srel;
        float* svi = sxi + (long long)cbase * SPITCH + srel;
        float2 dm = make_float2(0.f, 0.f);
        if (t >= 0 && t < n_ax) dm = __ldg(&demod[t]);
#else
        float* svp = sv + (long long)cbase * SPITCH + srel;
#endif
        float v[8];
        bool zero = false;
        if (t < 0 || t >= n_ax || g >= rows_avail) {
            zero = true;
            #pragma unroll
            for (int j = 0; j < 8; ++j) v[j] = 0.f;
        } else if (g < 8) {
            #pragma unroll
            for (int j = 0; j < 8; ++j) v[j] = 0.f - sdc[cbase + 2 * j];
        } else {
            const unsigned int* p =
                reinterpret_cast<const unsigned int*>(page + 96ll * g + 12 * seg);
            const unsigned int u0 = p[0], u1 = p[1], u2 = p[2];
            int s[8];
            s[0] = u0 & 0xFFF;
            s[1] = (u0 >> 12) & 0xFFF;
            s[2] = (u0 >> 24) | ((u1 & 0xF) << 8);
            s[3] = (u1 >> 4) & 0xFFF;
            s[4] = (u1 >> 16) & 0xFFF;
            s[5] = (u1 >> 28) | ((u2 & 0xFF) << 4);
            s[6] = (u2 >> 8) & 0xFFF;
            s[7] = (u2 >> 20) & 0xFFF;
            #pragma unroll
            for (int j = 0; j < 8; ++j) {
                const int v12 = (s[j] << 20) >> 20;   // sign-extend 12 bits
                v[j] = (float)v12 - sdc[cbase + 2 * j];
            }
        }
#ifdef XF2
        (void)zero;
        #pragma unroll
        for (int j = 0; j < 8; ++j)
            svx[2 * j * SPITCH] = make_float2(v[j] * dm.x, v[j] * dm.y);
#elif defined(XPLANES)
        (void)zero;
        #pragma unroll
        for (int j = 0; j < 8; ++j) {
            svr[2 * j * SPITCH] = v[j] * dm.x;
            svi[2 * j * SPITCH] = v[j] * dm.y;
        }
#else
        (void)zero;
        #pragma unroll
        for (int j = 0; j < 8; ++j) svp[2 * j * SPITCH] = v[j];
#endif
    }
    __syncthreads();

    // ---- Phase B: FIR from shared ----
    const int ngrp = TILE / RPT;
    for (int idx = tid; idx < NCH * ngrp; idx += blockDim.x) {
        const int cl = idx / ngrp;
        const int gj = idx - cl * ngrp;
        const int o  = gj * RPT;               // tile-relative offset
        const int i0 = tile0 + o;
        if (i0 >= n_ax) continue;
#ifdef SKIP_ZERO_TAIL
        const int iz = rows_avail - onset + HALO;
        if (i0 >= iz) continue;
#endif
        float re[RPT], im[RPT];
        #pragma unroll
        for (int r = 0; r < RPT; ++r) { re[r] = 0.f; im[r] = 0.f; }
#ifdef XF2
        const float2* sxc = sx + cl * SPITCH + o;
        #pragma unroll
        for (int k = 0; k < 31; ++k) {
            const float tk = st[k];
            #pragma unroll
            for (int r = 0; r < RPT; ++r) {
                const float2 xv = sxc[r + 30 - k];
                re[r] += tk * xv.x;
                im[r] += tk * xv.y;
            }
        }
#elif defined(XPLANES)
        const float* sxrc = sxr + cl * SPITCH + o;
        const float* sxic = sxi + cl * SPITCH + o;
        #pragma unroll
        for (int k = 0; k < 31; ++k) {
            const float tk = st[k];
            #pragma unroll
            for (int r = 0; r < RPT; ++r) {
                const int dd = r + 30 - k;
                re[r] += tk * sxrc[dd];
                im[r] += tk * sxic[dd];
            }
        }
#else
        const float*  svc = sv + cl * SPITCH + o;  // svc[dd]=v[m], m=i0-15+dd
        const float2* sdb = sd + o;
        #pragma unroll
        for (int k = 0; k < 31; ++k) {
            const float tk = st[k];
            #pragma unroll
            for (int r = 0; r < RPT; ++r) {
                const int dd = r + 30 - k;
                const float  vr = svc[dd];
                const float2 dr = sdb[dd];
                const float  xr = tk * vr;
                re[r] += xr * dr.x;
                im[r] += xr * dr.y;
            }
        }
#endif
        const long long base = (yrow0 + cl) * (long long)n_ax + i0;
        const bool aligned = ((base & 1LL) == 0);
        if (aligned && (i0 + RPT <= n_ax)) {
            #pragma unroll
            for (int r = 0; r < RPT; r += 2)
                *reinterpret_cast<float4*>(&y[base + r]) =
                    make_float4(re[r], im[r], re[r + 1], im[r + 1]);
        } else {
            #pragma unroll
            for (int r = 0; r < RPT; ++r)
                if (i0 + r < n_ax) y[base + r] = make_float2(re[r], im[r]);
        }
    }
}

#endif  // STORE_X

void decode_fir31_frame_launcher(
    const unsigned char* raw, const float* dc, const float2* demod,
    const float* taps, float2* y, int onset,
    int n_ax, int rows_avail, int n_angles, int n_shots,
    long long page_bytes, cudaStream_t stream)
{
    int n_eff = n_ax;
#ifdef SKIP_ZERO_TAIL
    // outputs past iz are exactly zero and the DPS output is zero-initialized;
    // don't even launch the all-dead tail blocks.
    const int iz = rows_avail - onset + HALO;
    if (iz < n_eff) n_eff = iz;
    if (n_eff < 1) n_eff = 1;
#endif
    dim3 grid((n_eff + TILE - 1) / TILE, n_shots, NCH / CBLK);
    decode_fir31_frame_kernel<<<grid, NTHREADS, 0, stream>>>(
        raw, dc, demod, taps, y, onset, n_ax, rows_avail, n_angles, page_bytes);
}
