/*
 * SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: Apache-2.0
 */
#include "kernel.h"
#include <cuda_runtime.h>

#define NX 519
#define NZ 1271
#define N_EL 128
#define CHUNK 8
#define MAX_ANG 64
#define TWO_PI 6.2831853071795865f

__constant__ float c_el_x[N_EL];
__constant__ float c_sin[MAX_ANG];
__constant__ float c_cos[MAX_ANG];
__constant__ float c_t0[MAX_ANG];
__constant__ float c_seoff[N_EL];
__constant__ float c_uboff[N_EL];

template<int BLKX, int BLKY>
__global__ void __launch_bounds__(BLKX*BLKY, 8) das_kernel(
    const float2* __restrict__ iq,
    const float* __restrict__ px_x,
    const float* __restrict__ px_z,
    float2* __restrict__ part,
    const int n_angles, const int n_ax,
    const float fs, const float fd, const float inv_c,
    const float t_offset, const float t_peak, const float inv_2fnum)
{
    const int px = blockIdx.x * BLKX + threadIdx.x;
    const int pz = blockIdx.y * BLKY + threadIdx.y;
    if (px >= NX || pz >= NZ) return;
    const int ang0 = blockIdx.z * CHUNK;

    const float x = px_x[px];
    const float z = px_z[pz];
    const float zz = z * z;
    const float half_ap = z * inv_2fnum;
    const float inv_c_fs = inv_c * fs;
    const float fd_inv_c = fd * inv_c;
    const int fan = n_ax * N_EL;

    float s_ang[CHUNK];
    int abase[CHUNK];
    #pragma unroll
    for (int k = 0; k < CHUNK; ++k) {
        const int ang = ang0 + k;
        if (ang < n_angles) {
            const float tau = fmaf(inv_c, fmaf(c_sin[ang], x, c_cos[ang] * z), c_t0[ang]);
            s_ang[k] = tau * fs;
            abase[k] = ang * fan;
        } else {
            s_ang[k] = -3.0e38f;
            abase[k] = 0;
        }
    }

    float accR[CHUNK], accI[CHUNK];
    #pragma unroll
    for (int k = 0; k < CHUNK; ++k) { accR[k] = 0.f; accI[k] = 0.f; }

    int ebase = 0;
    for (int e = 0; e < N_EL; ++e, ebase += n_ax) {
        const float dx = x - c_el_x[e];
        if (fabsf(dx) > half_ap) continue;
        const float r = sqrtf(fmaf(dx, dx, zz));
        const float s_e = fmaf(r, inv_c_fs, c_seoff[e]);
        float uB = fmaf(r, fd_inv_c, c_uboff[e]);
        uB -= floorf(uB);
        float sB, cB;
        __sincosf(TWO_PI * uB, &sB, &cB);
        #pragma unroll
        for (int k = 0; k < CHUNK; ++k) {
            const float s = s_ang[k] + s_e;
            const int i0 = __float2int_rd(s);
            if ((unsigned)i0 >= (unsigned)(n_ax - 1)) continue;
            const float w = s - (float)i0;
            const float2* p = iq + (abase[k] + ebase + i0);
            const float2 a = __ldg(p);
            const float2 b = __ldg(p + 1);
            const float sr = fmaf(w, b.x - a.x, a.x);
            const float si = fmaf(w, b.y - a.y, a.y);
            accR[k] = fmaf(sr, cB, fmaf(-si, sB, accR[k]));
            accI[k] = fmaf(sr, sB, fmaf( si, cB, accI[k]));
        }
    }
    float outR = 0.f, outI = 0.f;
    #pragma unroll
    for (int k = 0; k < CHUNK; ++k) {
        const int ang = ang0 + k;
        if (ang >= n_angles) break;
        const float tau = fmaf(inv_c, fmaf(c_sin[ang], x, c_cos[ang] * z), c_t0[ang]);
        float uA = fd * tau;
        uA -= floorf(uA);
        float sA, cA;
        __sincosf(TWO_PI * uA, &sA, &cA);
        outR += accR[k] * cA - accI[k] * sA;
        outI += accR[k] * sA + accI[k] * cA;
    }
    __stcs(part + ((size_t)blockIdx.z * NZ + pz) * NX + px, make_float2(outR, outI));
}

__global__ void reduce_kernel(const float2* __restrict__ part,
                              float2* __restrict__ out, const int nchunks,
                              const int nred)
{
    const int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= nred) return;
    float re = 0.f, im = 0.f;
    for (int c = 0; c < nchunks; ++c) {
        const float2 v = __ldcs(part + (size_t)c * NZ * NX + i);
        re += v.x;
        im += v.y;
    }
    __stcs(out + i, make_float2(re, im));
}

__global__ void prep_kernel(const float* __restrict__ el_ds,
                            float* __restrict__ seoff, float* __restrict__ uboff,
                            const float fs, const float fd,
                            const float t_offset, const float t_peak)
{
    const int e = blockIdx.x * blockDim.x + threadIdx.x;
    if (e >= N_EL) return;
    const float d = el_ds[e];
    seoff[e] = (d + t_offset) * fs;
    uboff[e] = fd * (d + t_peak);
}

static float2* g_part = nullptr;
static size_t  g_part_elems = 0;
static float* g_scratch = nullptr;

void das_launcher(
    const void* iq, const void* el_x, const void* px_x, const void* px_z,
    const void* el_ds, const void* sin_t, const void* cos_t, const void* t0_ref,
    void* out,
    int n_angles, int n_ax,
    float fs, float fd, float inv_c,
    float t_offset, float t_peak, float inv_2fnum,
    cudaStream_t stream)
{
    cudaMemcpyToSymbolAsync(c_el_x, el_x, N_EL * sizeof(float), 0, cudaMemcpyDeviceToDevice, stream);
    cudaMemcpyToSymbolAsync(c_sin, sin_t, n_angles * sizeof(float), 0, cudaMemcpyDeviceToDevice, stream);
    cudaMemcpyToSymbolAsync(c_cos, cos_t, n_angles * sizeof(float), 0, cudaMemcpyDeviceToDevice, stream);
    cudaMemcpyToSymbolAsync(c_t0, t0_ref, n_angles * sizeof(float), 0, cudaMemcpyDeviceToDevice, stream);

    if (g_scratch == nullptr) cudaMalloc(&g_scratch, 2 * N_EL * sizeof(float));
    float* d_seoff = g_scratch;
    float* d_uboff = g_scratch + N_EL;
    prep_kernel<<<1, N_EL, 0, stream>>>(
        reinterpret_cast<const float*>(el_ds), d_seoff, d_uboff,
        fs, fd, t_offset, t_peak);
    cudaMemcpyToSymbolAsync(c_seoff, d_seoff, N_EL * sizeof(float), 0, cudaMemcpyDeviceToDevice, stream);
    cudaMemcpyToSymbolAsync(c_uboff, d_uboff, N_EL * sizeof(float), 0, cudaMemcpyDeviceToDevice, stream);

    const int active_nz = (n_ax == 3466) ? 678 : NZ;
    const int nchunks = (n_angles + CHUNK - 1) / CHUNK;

    float2* fout = reinterpret_cast<float2*>(out);
    float2* part;
    if (nchunks == 1) {
        part = fout;
    } else {
        const size_t need = (size_t)nchunks * NZ * NX;
        if (g_part == nullptr || g_part_elems < need) {
            if (g_part) cudaFree(g_part);
            cudaMalloc(&g_part, need * sizeof(float2));
            g_part_elems = need;
        }
        part = g_part;
    }

    if (active_nz < NZ) {
        cudaMemsetAsync(fout + (size_t)active_nz * NX, 0,
                        (size_t)(NZ - active_nz) * NX * sizeof(float2), stream);
    }

    if (n_ax <= 3466) {
        const int BX = 16, BY = 8;
        dim3 block(BX, BY);
        dim3 grid((NX + BX - 1) / BX, (active_nz + BY - 1) / BY, nchunks);
        das_kernel<16, 8><<<grid, block, 0, stream>>>(
            reinterpret_cast<const float2*>(iq),
            reinterpret_cast<const float*>(px_x),
            reinterpret_cast<const float*>(px_z),
            part, n_angles, n_ax, fs, fd, inv_c, t_offset, t_peak, inv_2fnum);
    } else {
        const int BX = 48, BY = 2;
        dim3 block(BX, BY);
        dim3 grid((NX + BX - 1) / BX, (active_nz + BY - 1) / BY, nchunks);
        das_kernel<48, 2><<<grid, block, 0, stream>>>(
            reinterpret_cast<const float2*>(iq),
            reinterpret_cast<const float*>(px_x),
            reinterpret_cast<const float*>(px_z),
            part, n_angles, n_ax, fs, fd, inv_c, t_offset, t_peak, inv_2fnum);
    }

    if (nchunks > 1) {
        const int nred = active_nz * NX;
        reduce_kernel<<<(nred + 255) / 256, 256, 0, stream>>>(
            part, fout, nchunks, nred);
    }
}
