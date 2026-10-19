// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0
#include "nvus/sequence.hpp"
#include <algorithm>
#include <array>
#include <cmath>
#include <iomanip>
#include <locale>
#include <sstream>

namespace nvus {
namespace {
using DelayProfile = std::array<uint16_t, 128>;
void require(bool ok, const char *message) {
  if (!ok) throw std::invalid_argument(message);
}
void put(std::vector<uint8_t> &packet, size_t offset, uint64_t value, size_t bytes) {
  for (size_t i = 0; i < bytes; ++i) packet[offset + i] = uint8_t(value >> (8 * i));
}
void operation(std::vector<uint8_t> &packet, size_t index, uint32_t address,
               uint32_t tx1, uint32_t tx2) {
  const size_t offset = 32 + 32 * index;
  put(packet, offset, (uint64_t(address) << 32) | tx1, 6);
  put(packet, offset + 12, (uint64_t(address) << 32) | tx2, 6);
  packet[offset + 24] = 44;
}
std::vector<uint8_t> packet_header() {
  std::vector<uint8_t> p(2176, 0);
  put(p, 0, 0x44585455, 4);
  p[4] = 1;
  p[5] = 32;
  put(p, 12, 1, 2);
  put(p, 14, 32, 2);
  put(p, 16, 67, 2);
  put(p, 18, 67, 2);
  put(p, 20, 2144, 2);
  put(p, 24, 2144, 4);
  operation(p, 0, 0, 0, 0); // Disable read before every event's writes.
  for (size_t page = 0; page < 16; ++page) {
    operation(p, 2 + 4 * page, 2, 1u << page, 1u << page);
    operation(p, 3 + 4 * page, 3, 1u << page, 1u << page);
  }
  return p;
}
// Python's reference uses ties-to-even, not std::round's ties-away-from-zero.
uint64_t round_even(double value) {
  const auto lower = uint64_t(std::floor(value));
  const auto fraction = value - double(lower);
  return lower + (fraction > .5 || (fraction == .5 && (lower & 1)));
}
uint32_t delay_word(uint16_t half_cycles) {
  return (half_cycles >> 1) | ((half_cycles & 1u) << 14);
}
uint32_t chip_word(const DelayProfile &delays, size_t chip, size_t channel) {
  // Measured element map; page b packs ch(2b) with ch(2b+32).
  auto element = [chip](size_t c) { return 64 * (c % 2) + 32 * chip + c / 2; };
  return delay_word(delays[element(channel)]) |
         (delay_word(delays[element(channel + 32)]) << 16);
}
DelayProfile plane_wave_delays(double angle, double sound_speed) {
  require(std::isfinite(angle) && std::abs(angle) < 90.,
          "Plane-wave angle must be finite and strictly between -90 and 90 degrees");
  std::array<double, 128> seconds{};
  const double sine = std::sin(angle * (3.14159265358979323846 / 180.));
  for (size_t i = 0; i < seconds.size(); ++i)
    seconds[i] = (double(i) - 63.5) * .0003 * sine / sound_speed;
  const double minimum = *std::min_element(seconds.begin(), seconds.end());
  DelayProfile delays{};
  for (size_t i = 0; i < delays.size(); ++i) {
    const double cycles = (seconds[i] - minimum) * 200000000.;
    require(std::isfinite(cycles) && cycles < 16383.5,
            "Plane-wave delay exceeds the hardware field");
    delays[i] = uint16_t(2 * round_even(cycles));
  }
  return delays;
}
std::vector<uint8_t> compile_event_packet(const DelayProfile &delays, uint32_t side,
                                        uint16_t sequence) {
  constexpr uint32_t waveform = 9, delay_start = 32;
  require(side <= 1, "Receive side must be 0 (odd) or 1 (even)");
  require(delay_start >= 4 && delay_start <= 510 && waveform >= 2 &&
              waveform <= delay_start - 2,
          "Waveform and delay allocations overlap or exceed TX memory");
  for (auto delay : delays) require(delay <= 32767, "Delay exceeds 15-bit half-cycle range");
  auto packet = packet_header();
  put(packet, 8, sequence, 2);
  operation(packet, 1, 0x1a, side ? 0xffffffffu : 0, side ? 0xffffffffu : 0);
  for (size_t page = 0; page < 16; ++page)
    for (size_t part = 0; part < 2; ++part)
      operation(packet, 4 + 4 * page + part, 0x80 + delay_start + part,
                chip_word(delays, 0, 2 * page + part),
                chip_word(delays, 1, 2 * page + part));
  operation(packet, 66, 0x33, waveform, waveform);
  return packet;
}
} // namespace

PlaneWaveAction compile_plane_waves(const std::vector<double> &angles,
                                   PlaneWaveConfig config) {
  require(!angles.empty() && angles.size() <= 2048, "Require 1..2048 angles");
  require(std::isfinite(config.depth_mm) && config.depth_mm > 0 &&
              std::isfinite(config.sound_speed_m_s) && config.sound_speed_m_s > 0 &&
              std::isfinite(config.interval_us) && config.interval_us > 0 &&
              config.interval_us * 1000. < double(INT64_MAX),
          "Depth, sound speed and interval must be finite, positive and within range");
  const double requested_samples =
      std::ceil(2. * (config.depth_mm * .001 * 1.1) / config.sound_speed_m_s * 50000000.);
  require(std::isfinite(requested_samples) && requested_samples >= 1 && requested_samples <= 65536,
          "Depth requires an unsupported receive window");
  const auto samples = (uint32_t(requested_samples) + 1) & ~1u;
  require(config.interval_us >= 10.1 + .4 + samples / 50. + 20.,
          "Interval cannot contain the acquisition window");

  PlaneWaveAction action;
  action.config = config;
  action.angles_deg = angles;
  action.delays_half_cycles.reserve(angles.size());
  auto &result = action.sequence;
  result.config_id = "native-pw-v1:fs=50000000:sync=200000000:tx=2020:margin=4000:atten=23:lpf=15";
  result.rx_start_ticks = 80;
  result.resources = {{0x89, 0x20001}, {0x8a, 0x9d99}};
  result.programming_manifest =
      R"({"schema":"nvus.native-pw.v1","tx_delay_ticks":2020,"rx_start_ticks":80,"sample_rate_hz":50000000,"sync_clock_hz":200000000,"receive_attenuation_code":23,"analog_lpf_mhz":15,"waveform_words":[[137,131073],[138,40345]],"verification":"not performed by compilation"})";
  // Exact request identity, not a truncated hash or a cache key. The packet
  // builder is versioned so a future encoding change cannot alias this recipe.
  std::ostringstream identity;
  identity.imbue(std::locale::classic());
  identity << "native-pw-v1:" << std::hexfloat << config.depth_mm << ':'
           << config.sound_speed_m_s << ':' << config.interval_us;
  result.events.resize(2 * angles.size());
  for (size_t i = 0; i < angles.size(); ++i) {
    const auto delays = plane_wave_delays(angles[i], config.sound_speed_m_s);
    action.delays_half_cycles.emplace_back(delays.begin(), delays.end());
    require(config.interval_us >= 10.1 + *std::max_element(delays.begin(), delays.end()) / 400. + .4 + 20.,
            "Interval cannot contain the final transmitter pulse");
    identity << ':' << angles[i];
    auto packet = compile_event_packet(delays, 0, uint16_t(i));
    result.events[i] = {i, 0, samples, round_even(config.interval_us * 1000.), packet};
    // Each logical angle uses the same delay profile for the other RX half.
    put(packet, 8, i + angles.size(), 2);
    operation(packet, 1, 0x1a, 0xffffffffu, 0xffffffffu);
    result.events[i + angles.size()] = {i + angles.size(), 1, samples,
                                      round_even(config.interval_us * 1000.), std::move(packet)};
  }
  result.id = identity.str();
  return action;
}
PreparedSequence prepare_plane_waves(const std::vector<double> &angles,
                                    PlaneWaveConfig config) {
  return compile_plane_waves(angles, config).sequence;
}
} // namespace nvus
