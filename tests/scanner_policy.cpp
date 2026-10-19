// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0
#include "nvus/policy.hpp"
#include "native_simulated_transport.hpp"
#include <iostream>

using namespace nvus;

struct Options {
  uint64_t frames = 0;
  size_t rollout = 0;
  std::string composition = "single";
};

Options parse(int argc, char **argv) {
  if (argc != 5 && argc != 7) throw std::invalid_argument(
      "Usage: scanner_pw4cm --frame-limit N --rollout-length 1|32 "
      "[--composition single|interleave|stack]");
  Options result;
  for (int i = 1; i < argc; i += 2) {
    const std::string name = argv[i];
    const std::string value = argv[i + 1];
    if (name == "--frame-limit") result.frames = std::stoull(value);
    else if (name == "--rollout-length") result.rollout = std::stoull(value);
    else if (name == "--composition") result.composition = value;
    else throw std::invalid_argument("Unknown option: " + name);
  }
  if (!result.frames || result.frames > 1024 ||
      (result.rollout != 1 && result.rollout != 32) ||
      (result.composition != "single" && result.composition != "interleave" &&
       result.composition != "stack") ||
      (result.composition != "single" && result.frames % 2))
    throw std::invalid_argument("Require 1..1024 frames and rollout 1 or 32");
  return result;
}

std::shared_ptr<Policy> make_policy(const Options &options) {
  std::vector<double> angles(32);
  for (size_t i = 0; i < angles.size(); ++i) angles[i] = -7.75 + .5 * i;
  auto first = std::make_shared<Pw4cmPolicy>(angles, "A");
  if (options.composition == "single") return first;
  for (auto &angle : angles) angle += .125;
  auto second = std::make_shared<Pw4cmPolicy>(angles, "B");
  return options.composition == "interleave" ? interleave(first, second)
                                               : stack(first, second);
}

void validate(const ObservationGroup &group) {
  if (!group.observation.complete || group.transmits.size() != 32 ||
      group.observation.events.size() != 64 ||
      group.observation.buffers.size() != 64 ||
      group.action.angles_deg.size() != 32)
    throw std::runtime_error("Incomplete PW4cm observation group");
  for (size_t index = 0; index < 64; ++index)
    if (group.observation.events[index].event_id != index)
      throw std::runtime_error("RF buffer differs from reconstruction geometry");
  for (size_t index = 0; index < 32; ++index)
    if (group.transmits[index].transmit.angle_deg != group.action.angles_deg[index])
      throw std::runtime_error("Transmit observation angle mismatch");
}

int run(const Options &options) {
  auto policy = make_policy(options);
  auto transport = std::make_shared<SimulatedTransport>();
  PolicyLimits limits;
  limits.groups = options.frames;
  PolicyExecutor scanner(std::make_shared<HsbBackend>(transport), policy,
                         options.rollout, limits);
  scanner.start();
  uint64_t groups = 0;
  while (auto group = scanner.next()) {
    validate(*group);
    ++groups;
  }
  scanner.wait();
  const auto status = scanner.status();
  if (groups != options.frames || status.received_events != 64 * groups ||
      status.state != State::Idle || status.completion != Completion::Repetitions)
    throw std::runtime_error("PW4cm policy execution failed: " + status.error);
  std::cout << "offline_simulation groups=" << groups
            << " physical_acquisitions=" << status.received_events
            << " rollout_length=" << options.rollout
            << " composition=" << options.composition << '\n';
  return 0;
}

int main(int argc, char **argv) {
  try { return run(parse(argc, argv)); }
  catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
