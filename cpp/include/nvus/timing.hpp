#pragma once
#include <array>
#include <chrono>
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <cstring>

namespace nvus {
// Opt-in diagnostics: no per-event output or allocation, report after shutdown.
template <size_t N> class StageTiming {
  using Timer = std::chrono::steady_clock;
  std::array<const char *, N> names_;
  std::array<uint64_t, N> ns_{}, counts_{};
  bool enabled_ = false;
public:
  explicit StageTiming(std::array<const char *, N> names) : names_(names) {
    const auto *value = std::getenv("NVUS_TIMING");
    enabled_ = value && std::strcmp(value, "1") == 0;
  }
  auto now() const { return enabled_ ? Timer::now() : Timer::time_point{}; }
  void add(size_t stage, Timer::time_point start) {
    if (!enabled_) return;
    ns_[stage] += std::chrono::duration_cast<std::chrono::nanoseconds>(Timer::now() - start).count();
    ++counts_[stage];
  }
  ~StageTiming() {
    for (size_t i = 0; i < N; ++i)
      if (counts_[i])
        std::fprintf(stderr, "native_timing stage=%s count=%llu mean_us=%.3f total_ms=%.3f\n",
                     names_[i], static_cast<unsigned long long>(counts_[i]),
                     double(ns_[i]) / counts_[i] / 1000., double(ns_[i]) / 1e6);
  }
};
} // namespace nvus
