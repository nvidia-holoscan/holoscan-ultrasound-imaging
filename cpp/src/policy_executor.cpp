// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0
#include "nvus/policy.hpp"
#include <algorithm>
#include <limits>

namespace nvus {
namespace {
bool same_physical_event(Event expected, Event actual) {
  if (expected.packet.size() > 9 && actual.packet.size() > 9) {
    expected.packet[8] = expected.packet[9] = 0;
    actual.packet[8] = actual.packet[9] = 0;
  }
  return expected.rx_side == actual.rx_side &&
         expected.samples == actual.samples &&
         expected.interval_ns == actual.interval_ns &&
         expected.packet == actual.packet;
}
} // namespace

struct PolicyExecutor::Batch {
  std::vector<TransmitEvent> events;
  PlaneWaveAction action;
};
struct PolicyExecutor::Group {
  std::string label;
  uint64_t run_id = 0;
  std::vector<std::optional<TransmitObservation>> slots;
};

std::shared_ptr<PolicyExecutor::Batch>
PolicyExecutor::compile_next(const PolicyObservation &previous) {
  auto batch = std::make_shared<Batch>();
  batch->events = policy_->next(rollout_length_, previous);
  if (batch->events.size() != rollout_length_)
    throw std::invalid_argument("Policy returned the wrong event count");
  std::vector<double> angles;
  for (const auto &event : batch->events) {
    if (!event.group_size || event.group_index >= event.group_size)
      throw std::invalid_argument("Policy returned invalid grouping");
    angles.push_back(event.angle_deg);
  }
  batch->action = compile_plane_waves(angles);
  next_event_ += rollout_length_;
  return batch;
}

PolicyExecutor::PolicyExecutor(std::shared_ptr<Hardware> hardware,
                               std::shared_ptr<Policy> policy,
                               size_t rollout, PolicyLimits limits)
    : policy_(std::move(policy)), rollout_length_(rollout), limits_(limits) {
  const bool invalid_groups = limits.continuous ? limits.groups != 0
                                                 : !limits.groups || limits.groups > 1024;
  const bool invalid_duration = limits.continuous
                                    ? limits.max_duration_ms > 120000
                                    : !limits.max_duration_ms ||
                                          limits.max_duration_ms > 120000;
  if (!hardware || !policy_ || !rollout || rollout > 32 || invalid_groups ||
      invalid_duration || !limits.queue_capacity)
    throw std::invalid_argument("Invalid policy executor configuration");
  policy_batch_ = compile_next({});
  batches_.push_back(policy_batch_);
  RunOptions options;
  options.adaptive = true;
  options.host_pacing = limits.host_pacing;
  options.repetitions = limits.continuous
                            ? 0
                            : (limits.groups * 32 + rollout - 1) / rollout;
  options.max_events = limits.continuous
                           ? (limits.max_duration_ms
                                  ? 0
                                  : std::numeric_limits<uint64_t>::max())
                           : options.repetitions * rollout * 2;
  options.max_duration_ms = limits.max_duration_ms;
  // Keep acquisition backpressure aligned with the reconstruction-facing
  // queue. A larger hidden queue turns a slow display consumer into seconds
  // of stale ultrasound frames even though PolicyLimits advertises a small
  // bounded queue.
  options.queue_capacity = limits.queue_capacity;
  run_ = std::make_shared<Run>(std::move(hardware), policy_batch_->action.sequence,
      options, [this](const Observation &raw) {
        PolicyObservation previous{next_event_, observe(raw, *policy_batch_)};
        auto batch = compile_next(previous);
        std::lock_guard<std::mutex> lock(batch_mutex_);
        batches_.push_back(batch);
        policy_batch_ = batch;
        return batch->action.sequence;
      });
}

PolicyExecutor::~PolicyExecutor() { stop(); wait(); }

std::vector<TransmitObservation>
PolicyExecutor::observe(const Observation &raw, const Batch &batch) const {
  const auto count = batch.events.size();
  if (!raw.complete || raw.events.size() != 2 * count ||
      raw.buffers.size() != 2 * count || raw.sequence_id != batch.action.sequence.id)
    throw std::runtime_error("Incomplete or misassociated transmit observation");
  std::vector<TransmitObservation> result(count);
  for (size_t i = 0; i < count; ++i) {
    result[i].transmit = batch.events[i];
    result[i].run_id = raw.run_id;
    result[i].physical_events = {batch.action.sequence.events[i],
                                 batch.action.sequence.events[i + count]};
    result[i].acquisitions = {raw.events[i], raw.events[i + count]};
    result[i].buffers = {raw.buffers[i], raw.buffers[i + count]};
  }
  return result;
}

void PolicyExecutor::start() {
  std::lock_guard<std::mutex> lock(mutex_);
  if (started_ || finished_ || stopping_)
    throw std::logic_error("Policy executor is single-use");
  started_ = true;
  deadline_ = limits_.max_duration_ms
                  ? Clock::now() +
                        std::chrono::milliseconds(limits_.max_duration_ms)
                  : Clock::time_point::max();
  try { worker_ = std::thread(&PolicyExecutor::execute, this); }
  catch (...) { started_ = false; throw; }
}

void PolicyExecutor::stop() noexcept {
  stopping_ = true;
  run_->request_stop();
  {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!started_) finished_ = true;
  }
  cv_.notify_all();
}

bool PolicyExecutor::wait(int64_t timeout_ms) {
  std::unique_lock<std::mutex> lock(mutex_);
  if (!started_) return finished_;
  bool done = timeout_ms < 0
      ? (cv_.wait(lock, [&] { return finished_; }), true)
      : cv_.wait_for(lock, std::chrono::milliseconds(timeout_ms),
                     [&] { return finished_; });
  lock.unlock();
  if (done) {
    std::lock_guard<std::mutex> join(join_mutex_);
    if (worker_.joinable()) worker_.join();
  }
  return done;
}

std::optional<ObservationGroup> PolicyExecutor::next(int64_t timeout_ms) {
  std::unique_lock<std::mutex> lock(mutex_);
  auto available = [&] { return !ready_.empty() || finished_; };
  if (timeout_ms < 0) cv_.wait(lock, available);
  else if (!cv_.wait_for(lock, std::chrono::milliseconds(timeout_ms), available)) return {};
  if (ready_.empty()) return {};
  auto result = std::move(ready_.front());
  ready_.pop_front();
  cv_.notify_all();
  return result;
}

void PolicyExecutor::accept(TransmitObservation observation) {
  const auto event = observation.transmit;
  auto [position, inserted] = groups_.try_emplace(event.group_id);
  auto &group = position->second;
  if (inserted) {
    group.label = event.group_label;
    group.run_id = observation.run_id;
    group.slots.resize(event.group_size);
  }
  if (group.slots.size() != event.group_size || group.label != event.group_label ||
      group.run_id != observation.run_id || group.slots[event.group_index])
    throw std::runtime_error("Inconsistent observation group");
  group.slots[event.group_index] = std::move(observation);
  const bool complete = std::all_of(group.slots.begin(), group.slots.end(),
                                    [](const auto &slot) { return bool(slot); });
  if (complete) {
    auto finished = std::move(group);
    groups_.erase(position);
    publish(std::move(finished));
  }
}

void PolicyExecutor::publish(Group &&group) {
  std::vector<double> angles;
  for (const auto &slot : group.slots) angles.push_back(slot->transmit.angle_deg);
  ObservationGroup output;
  output.action = compile_plane_waves(angles);
  output.label = std::move(group.label);
  output.observation.run_id = group.run_id;
  output.observation.sequence_id = output.action.sequence.id;
  output.observation.config_id = output.action.sequence.config_id;
  output.observation.complete = true;
  for (auto &slot : group.slots) output.transmits.push_back(std::move(*slot));
  for (size_t side = 0; side < 2; ++side)
    for (size_t index = 0; index < output.transmits.size(); ++index) {
      auto &transmit = output.transmits[index];
      const auto &expected = output.action.sequence.events[
          index + side * output.transmits.size()];
      if (!same_physical_event(expected, transmit.physical_events[side]))
        throw std::runtime_error("RF geometry differs from executed transmit packet");
      auto record = transmit.acquisitions[side];
      record.event_id = index + side * output.transmits.size();
      output.observation.events.push_back(std::move(record));
      output.observation.buffers.push_back(transmit.buffers[side]);
    }
  std::unique_lock<std::mutex> lock(mutex_);
  const bool writable = cv_.wait_until(lock, deadline_, [&] {
    return stopping_ || ready_.size() < limits_.queue_capacity;
  });
  // Expiry is a normal stop for a duration-bounded continuous demo. A finite
  // validation with no consumer still violates its bounded-consumer contract.
  if (!writable && !limits_.continuous)
    throw std::runtime_error("Observation consumer exceeded the run deadline");
  if (!writable || stopping_)
    return;
  output.observation.observation_id = ++published_;
  output.observation.boundary = published_;
  ready_.push_back(std::move(output));
  cv_.notify_all();
}

Status PolicyExecutor::status() const {
  auto result = run_->status();
  std::lock_guard<std::mutex> lock(mutex_);
  result.completed_groups = published_;
  result.boundary = published_;
  result.queued_observations = ready_.size();
  if (!error_.empty()) {
    result.state = State::Fault;
    result.completion = Completion::Error;
    result.error = error_ + (result.error.empty() ? "" : "; " + result.error);
  }
  return result;
}

void PolicyExecutor::execute() noexcept {
  std::string failure;
  try {
    if (!stopping_) run_->start();
    while (auto raw = run_->next()) {
      std::shared_ptr<Batch> batch;
      {
        std::lock_guard<std::mutex> lock(batch_mutex_);
        if (batches_.empty()) throw std::logic_error("Missing batch metadata");
        batch = std::move(batches_.front());
        batches_.pop_front();
      }
      for (auto &observation : observe(*raw, *batch)) accept(std::move(observation));
    }
  } catch (const std::exception &error) {
    if (!stopping_) failure = error.what();
    run_->request_stop();
  } catch (...) {
    if (!stopping_) failure = "Unknown C++ policy failure";
    run_->request_stop();
  }
  run_->wait();
  {
    std::lock_guard<std::mutex> lock(mutex_);
    error_ = std::move(failure);
    finished_ = true;
  }
  cv_.notify_all();
}
} // namespace nvus
