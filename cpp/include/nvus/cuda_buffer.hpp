#pragma once
#include "nvus/runtime.hpp"
namespace nvus {
// Primary CUDA context only. The copy's source must remain valid through
// wait_ready(); consumers register their stream before releasing the lease.
Buffer copy_cuda_buffer(uintptr_t source, size_t bytes, int device,
                        uintptr_t stream);
Buffer allocate_cuda_buffer(size_t bytes, int device);
} // namespace nvus
