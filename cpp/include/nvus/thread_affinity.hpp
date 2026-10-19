// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <cerrno>
#include <cstdlib>
#include <stdexcept>
#include <string>
#ifdef __linux__
#include <pthread.h>
#include <sched.h>
#include <unistd.h>
#endif

namespace nvus {
// Optional process-local commissioning control. Never changes IRQs or policy.
inline void pin_thread_from_environment(const char *name) {
  const char *value = std::getenv(name);
  if (!value) return;
#ifdef __linux__
  char *end = nullptr;
  errno = 0;
  const long cpu = std::strtol(value, &end, 10);
  if (errno || end == value || *end || cpu < 0 || cpu >= CPU_SETSIZE)
    throw std::invalid_argument(std::string("Invalid CPU in ") + name);
  cpu_set_t allowed, selected;
  // A monitor spawned by a pinned acquisition worker inherits its one-core
  // mask. Validate against the application's main-thread allowance instead.
  if (sched_getaffinity(getpid(), sizeof(allowed), &allowed) || !CPU_ISSET(cpu, &allowed))
    throw std::invalid_argument(std::string("CPU outside allowed affinity in ") + name);
  CPU_ZERO(&selected);
  CPU_SET(cpu, &selected);
  if (pthread_setaffinity_np(pthread_self(), sizeof(selected), &selected))
    throw std::runtime_error(std::string("Cannot apply ") + name);
#else
  throw std::runtime_error(std::string(name) + " requires Linux");
#endif
}
} // namespace nvus
