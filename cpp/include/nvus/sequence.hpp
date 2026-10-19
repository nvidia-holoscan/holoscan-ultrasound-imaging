// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "nvus/runtime.hpp"

namespace nvus {
struct PlaneWaveConfig {
  double depth_mm = 40., sound_speed_m_s = 1480., interval_us = 400.;
};
struct PlaneWaveAction {
  PreparedSequence sequence;
  PlaneWaveConfig config;
  std::vector<double> angles_deg;
  // Achieved delays, retained with RF for reconstruction; one profile/angle.
  std::vector<std::vector<uint16_t>> delays_half_cycles;
};

// L7-3 reference recipe: 5 MHz / two cycles, full TX aperture, 50 MSPS,
// 200 MHz sync, TX offset 10.1 us, RX offset 0.4 us, 20 us capture margin,
// receive attenuation 23 and analog LPF 15 MHz. Both RX halves, mux-major.
// No I/O, Python, global rounding-mode changes or hidden policy state.
PreparedSequence prepare_plane_waves(const std::vector<double> &angles_deg,
                                    PlaneWaveConfig = {});
PlaneWaveAction compile_plane_waves(const std::vector<double> &angles_deg,
                                   PlaneWaveConfig = {});
} // namespace nvus
