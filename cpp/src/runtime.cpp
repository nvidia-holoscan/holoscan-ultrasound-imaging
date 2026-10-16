#include "nvus/runtime.hpp"
#include "nvus/timing.hpp"
#include "nvus/thread_affinity.hpp"
#include <algorithm>
#include <limits>
#include <set>
#include <stdexcept>
#include <utility>

namespace nvus {
namespace {
std::atomic<uint64_t> run_ids{0}, observation_ids{0};
Clock::time_point after_ms(uint64_t ms) {
  auto now = Clock::now();
  auto room = std::chrono::duration_cast<std::chrono::milliseconds>(
                  Clock::time_point::max() - now)
                  .count();
  return ms >= static_cast<uint64_t>(room)
             ? Clock::time_point::max()
             : now + std::chrono::milliseconds(ms);
}
std::string current_error() {
  try {
    throw;
  } catch (const std::exception &e) {
    return e.what();
  } catch (...) {
    return "unknown native error";
  }
}
} // namespace
Buffer Buffer::host(std::vector<uint8_t> bytes) {
  auto owned = std::make_shared<std::vector<uint8_t>>(std::move(bytes));
  Buffer b;
  b.owner = owned;
  b.data = reinterpret_cast<uintptr_t>(owned->data());
  b.size = owned->size();
  return b;
}
std::vector<uint8_t> Buffer::copy_host() const {
  if (kind == MemoryKind::Cuda) {
    if (!copy_to_host)
      throw std::runtime_error(
          "CUDA host copy requires an implemented lease adapter");
    if (wait_ready)
      wait_ready();
    auto bytes = copy_to_host();
    if (bytes.size() != size)
      throw std::runtime_error("CUDA host copy returned incorrect byte count");
    return bytes;
  }
  if (!owner || (size && !data))
    throw std::runtime_error("buffer has no owned storage");
  if (wait_ready)
    wait_ready();
  if (!size)
    return {};
  auto begin = reinterpret_cast<const uint8_t *>(data);
  return {begin, begin + size};
}
void Run::validate(const PreparedSequence &s) {
  if (s.id.empty() || s.config_id.empty() || s.events.empty())
    throw std::invalid_argument("sequence IDs and events are required");
  std::set<uint64_t> ids;
  for (const auto &e : s.events) {
    if (!ids.insert(e.event_id).second)
      throw std::invalid_argument("duplicate event ID");
    if (e.rx_side > 1 || !e.samples || e.samples > 65536 || e.samples % 2 ||
        e.packet.empty() || !e.interval_ns || e.interval_ns > INT64_MAX)
      throw std::invalid_argument(
          "invalid event receive selection, samples, packet or interval");
  }
}
Run::Run(std::shared_ptr<Hardware> h, PreparedSequence s, RunOptions o,
         NativePolicy policy)
    : hardware_(std::move(h)), sequence_(std::move(s)), options_(o),
      native_policy_(std::move(policy)) {
  validate(sequence_);
  if (!hardware_ || !o.queue_capacity)
    throw std::invalid_argument(
        "hardware and positive queue capacity required");
  if (o.adaptive && !o.max_events && !o.max_duration_ms)
    throw std::invalid_argument(
        "adaptive acquisition requires an independent event or time limit");
  if (o.adaptive && !o.decision_timeout_ms)
    throw std::invalid_argument("adaptive decision timeout must be positive");
  if (native_policy_ && !o.adaptive)
    throw std::invalid_argument("native rollout policy requires adaptive acquisition");
  status_.run_id = ++run_ids;
}
Run::~Run() {
  request_stop();
  wait();
}
void Run::start() {
  std::lock_guard<std::mutex> lock(mutex_);
  if (started_ || finished_)
    throw std::logic_error("run can only start once");
  started_ = true;
  status_.state = State::Running;
  try {
    worker_ = std::thread(&Run::execute, this);
  } catch (...) {
    finished_ = true;
    status_.state = State::Fault;
    status_.completion = Completion::Error;
    throw;
  }
}
void Run::request_stop() noexcept {
  bool cancel = false;
  {
    std::lock_guard<std::mutex> lock(mutex_);
    if (finished_)
      return;
    stopped_ = true;
    if (!started_) {
      finished_ = true;
      status_.state = State::Idle;
      status_.completion = Completion::Stopped;
    } else {
      status_.state = State::Stopping;
      cancel = true;
    }
  }
  if (cancel)
    hardware_->cancel();
  cv_.notify_all();
}
bool Run::wait(int64_t timeout_ms) {
  std::unique_lock<std::mutex> lock(mutex_);
  if (!started_)
    return finished_;
  bool done;
  if (timeout_ms < 0) {
    cv_.wait(lock, [&] { return finished_; });
    done = true;
  } else
    done = cv_.wait_for(lock, std::chrono::milliseconds(timeout_ms),
                        [&] { return finished_; });
  lock.unlock();
  if (done) {
    std::lock_guard<std::mutex> join_lock(join_mutex_);
    if (worker_.joinable())
      worker_.join();
  }
  return done;
}
Status Run::status() const {
  std::lock_guard<std::mutex> lock(mutex_);
  auto s = status_;
  s.queued_observations = observations_.size();
  s.pending_update = pending_.has_value();
  return s;
}
std::optional<Observation> Run::next(int64_t timeout_ms) {
  std::unique_lock<std::mutex> lock(mutex_);
  auto ready = [&] { return !observations_.empty() || finished_; };
  if (timeout_ms < 0)
    cv_.wait(lock, ready);
  else if (!cv_.wait_for(lock, std::chrono::milliseconds(timeout_ms), ready))
    return {};
  if (observations_.empty())
    return {};
  auto observation = std::move(observations_.front());
  observations_.pop_front();
  cv_.notify_all();
  return observation;
}
void Run::submit(PreparedSequence s, uint64_t source, uint64_t boundary) {
  validate(s);
  std::lock_guard<std::mutex> lock(mutex_);
  if (native_policy_)
    throw std::logic_error("native rollout policy does not accept external decisions");
  if (!options_.adaptive || status_.state != State::Waiting || stopped_ ||
      finished_)
    throw std::logic_error("run is not waiting for an adaptive decision");
  if (Clock::now() >= decision_deadline_)
    throw StaleDecision("adaptive decision deadline expired");
  if (source != source_observation_ || boundary != status_.boundary)
    throw StaleDecision("stale observation or target boundary");
  if (s.config_id != sequence_.config_id)
    throw std::invalid_argument("board configuration is locked for a run");
  if (pending_)
    throw std::logic_error("one sequence update is already pending");
  pending_ = std::move(s);
  cv_.notify_all();
}
bool Run::pause_until(Clock::time_point deadline) {
  std::unique_lock<std::mutex> lock(mutex_);
  cv_.wait_until(lock, deadline, [&] { return stopped_.load(); });
  return !stopped_;
}
void Run::execute() noexcept {
  StageTiming<6> timing{{"host_pacing", "observer_queue_wait", "policy_wait",
                         "initial_program", "initial_verify",
                         "transmit_sequence_update"}};
  auto deadline = options_.max_duration_ms ? after_ms(options_.max_duration_ms)
                                           : Clock::time_point::max();
  Completion reason = Completion::None;
  std::string failure, cleanup;
  Observation observation;
  bool group_started = false;
  try {
    pin_thread_from_environment("NVUS_ACQUIRE_CPU");
    if (!stopped_) {
      auto stage = timing.now();
      hardware_->program(sequence_);
      timing.add(3, stage);
      stage = timing.now();
      hardware_->verify(sequence_);
      timing.add(4, stage);
    }
    if (!stopped_)
      hardware_->start_receiver();
    while (!stopped_) {
      uint64_t groups, attempted;
      {
        std::lock_guard<std::mutex> lock(mutex_);
        groups = status_.completed_groups;
        attempted = status_.attempted_events;
      }
      if (options_.repetitions && groups >= options_.repetitions) {
        reason = Completion::Repetitions;
        break;
      }
      if (options_.max_events &&
          (attempted >= options_.max_events ||
           sequence_.events.size() > options_.max_events - attempted)) {
        reason = Completion::EventLimit;
        break;
      }
      if (Clock::now() >= deadline) {
        reason = Completion::TimeLimit;
        break;
      }
      auto queue_wait = timing.now();
      {
        std::unique_lock<std::mutex> lock(mutex_);
        cv_.wait_until(lock, deadline, [&] {
          return stopped_ || observations_.size() < options_.queue_capacity;
        });
      }
      timing.add(1, queue_wait);
      if (stopped_)
        break;
      if (Clock::now() >= deadline) {
        reason = Completion::TimeLimit;
        break;
      }
      observation = {};
      observation.run_id = status_.run_id;
      observation.observation_id = ++observation_ids;
      observation.boundary = groups + 1;
      observation.sequence_id = sequence_.id;
      observation.config_id = sequence_.config_id;
      group_started = true;
      for (const auto &event : sequence_.events) {
        if (stopped_ || Clock::now() >= deadline)
          throw std::runtime_error("acquisition cancelled or expired");
        auto event_start = Clock::now();
        {
          std::lock_guard<std::mutex> lock(mutex_);
          ++status_.attempted_events;
        }
        auto buffer = hardware_->acquire(event, stopped_, deadline);
        if (!buffer.owner || (buffer.size && !buffer.data))
          throw std::runtime_error("hardware returned unowned receive buffer");
        if (buffer.kind == MemoryKind::Cuda &&
            (!buffer.wait_ready || !buffer.record_consumer))
          throw std::runtime_error(
              "CUDA buffer requires producer and consumer fence hooks");
        observation.events.push_back(
            {event.event_id,
             static_cast<uint64_t>(
                 std::chrono::duration_cast<std::chrono::nanoseconds>(
                     event_start.time_since_epoch())
                     .count()),
             event.rx_side, event.samples});
        observation.buffers.push_back(std::move(buffer));
        {
          std::lock_guard<std::mutex> lock(mutex_);
          ++status_.received_events;
        }
        auto room = std::chrono::duration_cast<std::chrono::nanoseconds>(
                        Clock::time_point::max() - event_start)
                        .count();
        auto next_start =
            event.interval_ns >= static_cast<uint64_t>(room)
                ? Clock::time_point::max()
                : event_start + std::chrono::nanoseconds(event.interval_ns);
        auto pacing_started = timing.now();
        if (options_.host_pacing && event.interval_ns &&
            Clock::now() < next_start)
          pause_until(std::min(next_start, deadline));
        timing.add(0, pacing_started);
      }
      observation.complete = true;
      group_started = false;
      auto decision_deadline =
          std::min(deadline, after_ms(options_.decision_timeout_ms));
      uint64_t completed_events;
      {
        std::lock_guard<std::mutex> lock(mutex_);
        completed_events = status_.attempted_events;
      }
      const bool more =
          (!options_.repetitions || groups + 1 < options_.repetitions) &&
          (!options_.max_events || completed_events < options_.max_events);
      if (native_policy_ && more) {
        auto stage = timing.now();
        auto update = native_policy_(observation);
        timing.add(2, stage);
        validate(update);
        if (update.config_id != sequence_.config_id)
          throw std::invalid_argument("board configuration is locked for a run");
        stage = timing.now();
        hardware_->set_transmit_sequence(update);
        timing.add(5, stage);
        sequence_ = std::move(update);
      }
      {
        std::lock_guard<std::mutex> lock(mutex_);
        ++status_.completed_groups;
        status_.boundary = observation.boundary;
        source_observation_ = observation.observation_id;
        decision_deadline_ = options_.deadline_policy == DeadlinePolicy::Wait
                                 ? deadline
                                 : decision_deadline;
        status_.state =
            options_.adaptive && more && !native_policy_ ? State::Waiting : State::Running;
        observations_.push_back(std::move(observation));
        cv_.notify_all();
      }
      if (native_policy_)
        continue;
      if (!options_.adaptive ||
          (options_.repetitions && groups + 1 >= options_.repetitions))
        continue;
      {
        std::lock_guard<std::mutex> lock(mutex_);
        if (options_.max_events &&
            status_.attempted_events >= options_.max_events) {
          reason = Completion::EventLimit;
          break;
        }
      }
      std::optional<PreparedSequence> update;
      auto policy_wait = timing.now();
      {
        std::unique_lock<std::mutex> lock(mutex_);
        cv_.wait_until(lock, decision_deadline_,
                       [&] { return stopped_ || pending_.has_value(); });
        update = std::exchange(pending_, std::nullopt);
        if (!update && !stopped_ && Clock::now() < deadline &&
            options_.deadline_policy == DeadlinePolicy::Stop)
          reason = Completion::PolicyTimeout;
        if (!stopped_)
          status_.state = State::Running;
      }
      timing.add(2, policy_wait);
      if (stopped_ || reason == Completion::PolicyTimeout)
        break;
      if (Clock::now() >= deadline) {
        reason = Completion::TimeLimit;
        break;
      }
      if (update) {
        auto stage = timing.now();
        hardware_->set_transmit_sequence(*update);
        timing.add(5, stage);
        std::lock_guard<std::mutex> lock(mutex_);
        sequence_ = std::move(*update);
      }
    }
    if (stopped_)
      reason = Completion::Stopped;
  } catch (...) {
    failure = current_error();
    const bool expired = !stopped_ && Clock::now() >= deadline;
    reason = stopped_ ? Completion::Stopped
                      : (expired ? Completion::TimeLimit : Completion::Error);
    if (expired) {
      failure.clear();
    } else if (group_started) {
      observation.error = failure;
      std::lock_guard<std::mutex> lock(mutex_);
      observations_.push_back(std::move(observation));
    }
  }
  try {
    hardware_->stop();
  } catch (...) {
    cleanup = current_error();
    reason = Completion::Error;
  }
  {
    std::lock_guard<std::mutex> lock(mutex_);
    status_.completion = reason;
    status_.error = failure;
    status_.cleanup_error = cleanup;
    status_.state = reason == Completion::Error ? State::Fault : State::Idle;
    pending_.reset();
    finished_ = true;
    cv_.notify_all();
  }
}
void FakeHardware::program(const PreparedSequence &) {
  ++program_calls;
  std::lock_guard<std::mutex> lock(mutex_);
  cancelled_ = false;
}
void FakeHardware::verify(const PreparedSequence &) {
  ++verify_calls;
  if (fail_verify)
    throw std::runtime_error("injected verify failure");
}
void FakeHardware::set_transmit_sequence(const PreparedSequence &) {
  ++transmit_sequence_updates;
}
void FakeHardware::start_receiver() { ++receiver_starts; }
Buffer FakeHardware::acquire(const Event &e, const std::atomic_bool &stopped,
                             Clock::time_point deadline) {
  if (stopped)
    throw std::runtime_error("cancelled before transmit");
  ++acquired;
  if (e.event_id == fail_event)
    throw std::runtime_error("injected acquisition failure");
  std::unique_lock<std::mutex> lock(mutex_);
  auto until = std::min(deadline, after_ms(delay_ms));
  cv_.wait_until(lock, until, [&] { return stopped || cancelled_; });
  if (stopped || cancelled_)
    throw std::runtime_error("receive cancelled");
  if (Clock::now() >= deadline)
    throw std::runtime_error("receive deadline expired");
  return Buffer::host(std::vector<uint8_t>(size_t(e.samples) * 96,
                                           static_cast<uint8_t>(e.event_id)));
}
void FakeHardware::cancel() noexcept {
  std::lock_guard<std::mutex> lock(mutex_);
  cancelled_ = true;
  cv_.notify_all();
}
void FakeHardware::stop() {
  ++stop_calls;
  if (fail_stop)
    throw std::runtime_error("injected cleanup failure");
}
std::shared_ptr<Run> Scanner::run(PreparedSequence s, RunOptions o) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (auto previous = active_) {
    auto state = previous->status().state;
    if (state != State::Idle && state != State::Fault)
      throw std::logic_error("scanner already has an active run");
    if (state == State::Fault)
      throw std::logic_error(
          "scanner fault requires explicit hardware recovery");
  }
  auto result = std::make_shared<Run>(hardware_, std::move(s), o);
  active_ = result;
  result->start();
  return result;
}
State Scanner::state() const {
  std::lock_guard<std::mutex> lock(mutex_);
  if (auto run = active_)
    return run->status().state;
  return State::Idle;
}
} // namespace nvus
