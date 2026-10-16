# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Host-side glue shared by the NVUS examples: IB device pick, CUDA primary context."""

import cuda.bindings.driver as cuda

import hololink as hololink_module


def default_ibv_name():
    try:
        return hololink_module.infiniband_devices()[0]
    except Exception:
        return "mlx5_0"


def retain_cuda_context():
    (cu_result,) = cuda.cuInit(0)
    assert cu_result == cuda.CUresult.CUDA_SUCCESS
    cu_result, cu_device = cuda.cuDeviceGet(0)
    assert cu_result == cuda.CUresult.CUDA_SUCCESS
    cu_result, cu_context = cuda.cuDevicePrimaryCtxRetain(cu_device)
    assert cu_result == cuda.CUresult.CUDA_SUCCESS
    return cu_context, cu_device


def release_cuda_context(cu_device):
    (cu_result,) = cuda.cuDevicePrimaryCtxRelease(cu_device)
    assert cu_result == cuda.CUresult.CUDA_SUCCESS
