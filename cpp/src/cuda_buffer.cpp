// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0
#include "nvus/cuda_buffer.hpp"
#include <cuda.h>
#include <stdexcept>

namespace nvus {
namespace {
void check(CUresult result, const char *operation) {
  if (result == CUDA_SUCCESS)
    return;
  const char *message = nullptr;
  cuGetErrorString(result, &message);
  throw std::runtime_error(std::string(operation) + ": " +
                           (message ? message : "CUDA error"));
}
struct Current {
  explicit Current(CUcontext context) {
    check(cuCtxPushCurrent(context), "cuCtxPushCurrent");
  }
  ~Current() {
    CUcontext previous;
    cuCtxPopCurrent(&previous);
  }
};
struct Allocation {
  CUdevice device = 0;
  CUcontext context = nullptr;
  CUdeviceptr data = 0;
  CUevent produced = nullptr;
  size_t size = 0;
  bool quarantined = false;
  std::mutex mutex;
  std::vector<CUevent> consumers;
  ~Allocation() {
    // A failed CUDA context cannot prove readers stopped. Preserve the
    // allocation/context reference until process teardown instead of reuse.
    if (!context || quarantined)
      return;
    if (cuCtxPushCurrent(context) == CUDA_SUCCESS) {
      if (produced)
        cuEventSynchronize(produced);
      for (auto event : consumers) {
        cuEventSynchronize(event);
        cuEventDestroy(event);
      }
      if (produced)
        cuEventDestroy(produced);
      if (data)
        cuMemFree(data);
      CUcontext previous;
      cuCtxPopCurrent(&previous);
    }
    cuDevicePrimaryCtxRelease(device);
  }
  void ready() {
    Current current(context);
    check(cuEventSynchronize(produced), "RF producer fence");
  }
  void consumed(uintptr_t stream) {
    Current current(context);
    std::lock_guard<std::mutex> lock(mutex);
    // Finished consumers need no longer occupy a fence slot.
    for (auto it = consumers.begin(); it != consumers.end();) {
      auto result = cuEventQuery(*it);
      if (result == CUDA_SUCCESS) {
        cuEventDestroy(*it);
        it = consumers.erase(it);
      } else {
        if (result != CUDA_ERROR_NOT_READY)
          check(result, "consumer fence query");
        ++it;
      }
    }
    CUevent event = nullptr;
    try {
      check(cuEventCreate(&event, CU_EVENT_DISABLE_TIMING), "consumer event");
      check(cuEventRecord(event, reinterpret_cast<CUstream>(stream)),
            "consumer event record");
      consumers.push_back(event);
    } catch (...) {
      // The consumer may already have queued reads. A failed fence must
      // not allow its source allocation to be freed underneath that work.
      if (cuStreamSynchronize(reinterpret_cast<CUstream>(stream)) !=
              CUDA_SUCCESS &&
          cuCtxSynchronize() != CUDA_SUCCESS) {
        quarantined = true;
        throw std::runtime_error(
            "CUDA consumer fence and synchronization failed; RF allocation "
            "quarantined until process exit");
      }
      if (event)
        cuEventDestroy(event);
      throw;
    }
  }
};
Buffer make_buffer(size_t bytes, int index, uintptr_t source,
                   uintptr_t stream) {
  if (!bytes || index < 0)
    throw std::invalid_argument(
        "CUDA buffer requires positive bytes and a device");
  check(cuInit(0), "cuInit");
  auto owner = std::make_shared<Allocation>();
  check(cuDeviceGet(&owner->device, index), "cuDeviceGet");
  check(cuDevicePrimaryCtxRetain(&owner->context, owner->device),
        "retain CUDA context");
  owner->size = bytes;
  {
    Current current(owner->context);
    check(cuMemAlloc(&owner->data, bytes), "allocate RF snapshot");
    check(cuEventCreate(&owner->produced, CU_EVENT_DISABLE_TIMING),
          "producer event");
    if (source)
      check(cuMemcpyDtoDAsync(owner->data, source, bytes,
                              reinterpret_cast<CUstream>(stream)),
            "snapshot RF");
    else
      check(cuMemsetD8Async(owner->data, 0, bytes,
                            reinterpret_cast<CUstream>(stream)),
            "initialize RF");
    check(cuEventRecord(owner->produced, reinterpret_cast<CUstream>(stream)),
          "record producer fence");
  }
  Buffer result;
  result.owner = owner;
  result.data = owner->data;
  result.size = bytes;
  result.kind = MemoryKind::Cuda;
  result.device = index;
  result.wait_ready = [owner] { owner->ready(); };
  result.record_consumer = [owner](uintptr_t stream) {
    owner->consumed(stream);
  };
  result.copy_to_host = [owner] {
    owner->ready();
    Current current(owner->context);
    std::vector<uint8_t> data(owner->size);
    check(cuMemcpyDtoH(data.data(), owner->data, owner->size),
          "explicit RF host export");
    return data;
  };
  return result;
}
} // namespace
Buffer copy_cuda_buffer(uintptr_t source, size_t bytes, int device,
                        uintptr_t stream) {
  if (!source)
    throw std::invalid_argument("source CUDA pointer is required");
  return make_buffer(bytes, device, source, stream);
}
Buffer allocate_cuda_buffer(size_t bytes, int device) {
  return make_buffer(bytes, device, 0, 0);
}
} // namespace nvus
