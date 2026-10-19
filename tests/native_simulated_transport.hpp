// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "nvus/hsb_backend.hpp"

// Offline stand-in for the SDK only. The test still exercises HsbBackend's
// packet validation, send/flush/guard/trigger/receive and shutdown contracts.
// These counters are not evidence of register readback or RF on a real rig.
class SimulatedTransport final : public nvus::HsbTransport {
public:
  uint64_t programs = 0, verifications = 0, sends = 0, triggers = 0, stops = 0;
  uint64_t fail_verify_frame = 0, fail_receive_event = UINT64_MAX;
  bool fail_stop = false;

  void prepare(const nvus::PreparedSequence &, size_t bytes,
               uint16_t next_sequence, bool first) override {
    if (first != (programs == 0) || next_sequence != sends)
      throw std::runtime_error("Incorrect update epoch at frame boundary");
    bytes_ = bytes;
    ++programs;
  }
  bool verify(const nvus::PreparedSequence &) override {
    return ++verifications != fail_verify_frame;
  }
  void start_receiver() override { receiving_ = true; }
  void send_update(const std::vector<uint8_t> &packet) override {
    const auto sequence = uint32_t(packet.at(8)) | (uint32_t(packet.at(9)) << 8);
    if (sequence != sends || sends != triggers || !receiving_ || cancelled_)
      throw std::runtime_error("Invalid send ordering or wire sequence");
    ++sends;
  }
  bool flush_update(nvus::Clock::time_point deadline) override {
    return !cancelled_ && nvus::Clock::now() < deadline;
  }
  void trigger() override {
    if (cancelled_ || sends != triggers + 1)
      throw std::runtime_error("Trigger without exactly one update");
    ++triggers;
  }
  nvus::HsbFrame receive(nvus::Clock::time_point deadline) override {
    if (cancelled_ || nvus::Clock::now() >= deadline ||
        triggers != received_ + 1 || received_ == fail_receive_event)
      throw std::runtime_error("Simulated receive failure");
    nvus::HsbFrame frame;
    frame.frame_number = received_++;
    frame.metadata_frame_number = uint16_t(frame.frame_number);
    frame.bytes_written = uint32_t(bytes_);
    frame.buffer = nvus::Buffer::host(std::vector<uint8_t>(bytes_, 0));
    return frame;
  }
  void cancel_receive() noexcept override { cancelled_ = true; }
  void stop() override {
    ++stops;
    cancelled_ = true;
    receiving_ = false;
    if (fail_stop) throw std::runtime_error("Simulated shutdown failure");
  }

private:
  size_t bytes_ = 0;
  uint64_t received_ = 0;
  bool receiving_ = false;
  std::atomic_bool cancelled_{false};
};
