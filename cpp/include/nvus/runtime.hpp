#pragma once
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace nvus {
using Clock = std::chrono::steady_clock;
struct Event {
  uint64_t event_id = 0;
  uint32_t rx_side = 0, samples = 0;
  uint64_t interval_ns = 0;
  std::vector<uint8_t> packet;
};
struct ProgramWord {
  uint32_t address = 0, value = 0;
};
struct StaleDecision : std::runtime_error {
  using std::runtime_error::runtime_error;
};
struct PreparedSequence {
  std::string id, config_id;
  std::vector<Event> events;
  std::string programming_manifest; // Canonical resources/config; not readback
                                    // evidence.
  std::vector<ProgramWord> resources;
  uint32_t rx_start_ticks = 0;
};
enum class MemoryKind { Host, Cuda };
struct Buffer {
  std::shared_ptr<void> owner;
  uintptr_t data = 0;
  size_t size = 0;
  MemoryKind kind = MemoryKind::Host;
  int device = -1;
  std::function<void()> wait_ready;
  std::function<void(uintptr_t)> record_consumer;
  std::function<std::vector<uint8_t>()> copy_to_host;
  static Buffer host(std::vector<uint8_t> bytes);
  std::vector<uint8_t> copy_host() const;
};
class Hardware {
public:
  virtual ~Hardware() = default;
  virtual void program(const PreparedSequence &) = 0;
  virtual void verify(const PreparedSequence &) = 0;
  // Install the next streamed transmit events in host memory only. Session
  // configuration/resources stay locked; implementations must not perform
  // device programming or readback here.
  virtual void set_transmit_sequence(const PreparedSequence &) = 0;
  virtual void start_receiver() = 0; // Must not transmit.
  // Owns trigger + receive. Honor cancellation/deadline; returned DMA storage
  // must not be reused until ownership AND consumer fences allow reuse.
  virtual Buffer acquire(const Event &, const std::atomic_bool &,
                         Clock::time_point) = 0;
  virtual void cancel() noexcept = 0; // Concurrent, idempotent, wakes receive.
  virtual void
  stop() = 0; // Quiesce and release resources; throw if unverified.
};
enum class State { Ready, Running, Waiting, Stopping, Idle, Fault };
enum class DeadlinePolicy { Wait, Stop, Repeat };
enum class Completion {
  None,
  Repetitions,
  EventLimit,
  TimeLimit,
  Stopped,
  PolicyTimeout,
  Error
};
struct RunOptions {
  uint64_t repetitions = 1; // Zero explicitly means continuous.
  uint64_t max_events = 0, max_duration_ms = 0;
  size_t queue_capacity = 2;
  bool adaptive = false, host_pacing = true;
  uint64_t decision_timeout_ms = 1000;
  DeadlinePolicy deadline_policy = DeadlinePolicy::Stop;
};
struct EventRecord {
  // Host scheduling evidence, not FPGA or acoustic acquisition timestamps.
  uint64_t event_id = 0, timestamp_ns = 0;
  uint32_t rx_side = 0, samples = 0;
  std::string timestamp_origin = "host_monotonic_before_acquire";
  std::string association = "host_inferred";
};
struct Observation {
  uint64_t run_id = 0, observation_id = 0, boundary = 0;
  std::string sequence_id, config_id;
  bool complete = false;
  std::string error;
  std::vector<EventRecord> events;
  std::vector<Buffer> buffers;
};
struct Status {
  State state = State::Ready;
  Completion completion = Completion::None;
  uint64_t run_id = 0, attempted_events = 0, received_events = 0,
           completed_groups = 0, boundary = 0;
  size_t queued_observations = 0;
  bool pending_update = false;
  std::string error, cleanup_error;
};
class Run {
public:
  using NativePolicy = std::function<PreparedSequence(const Observation &)>;
  Run(std::shared_ptr<Hardware>, PreparedSequence, RunOptions = {},
      NativePolicy = {});
  ~Run();
  Run(const Run &) = delete;
  Run &operator=(const Run &) = delete;
  void start();
  void request_stop() noexcept;
  bool wait(int64_t timeout_ms = -1);
  std::optional<Observation> next(int64_t timeout_ms = -1);
  Status status() const;
  void submit(PreparedSequence, uint64_t source_observation_id,
              uint64_t expected_boundary);

private:
  void execute() noexcept;
  static void validate(const PreparedSequence &);
  bool pause_until(Clock::time_point);
  std::shared_ptr<Hardware> hardware_;
  PreparedSequence sequence_;
  RunOptions options_;
  NativePolicy native_policy_;
  mutable std::mutex mutex_;
  std::condition_variable cv_;
  std::thread worker_;
  std::mutex join_mutex_;
  uint64_t source_observation_ = 0;
  Clock::time_point decision_deadline_ = Clock::time_point::max();
  std::atomic_bool stopped_{false};
  bool started_ = false, finished_ = false;
  Status status_;
  std::deque<Observation> observations_;
  std::optional<PreparedSequence> pending_;
};
class Scanner {
public:
  explicit Scanner(std::shared_ptr<Hardware> hardware)
      : hardware_(std::move(hardware)) {}
  std::shared_ptr<Run> run(PreparedSequence, RunOptions = {});
  State state() const;

private:
  std::shared_ptr<Hardware> hardware_;
  std::shared_ptr<Run> active_;
  mutable std::mutex mutex_;
};
class FakeHardware final : public Hardware {
public:
  uint64_t fail_event = UINT64_MAX, delay_ms = 0;
  bool fail_verify = false, fail_stop = false;
  std::atomic<uint64_t> acquired{0}, receiver_starts{0}, stop_calls{0},
      program_calls{0}, verify_calls{0}, transmit_sequence_updates{0};
  void program(const PreparedSequence &) override;
  void verify(const PreparedSequence &) override;
  void set_transmit_sequence(const PreparedSequence &) override;
  void start_receiver() override;
  Buffer acquire(const Event &, const std::atomic_bool &,
                 Clock::time_point) override;
  void cancel() noexcept override;
  void stop() override;

private:
  std::mutex mutex_;
  std::condition_variable cv_;
  bool cancelled_ = false;
};
} // namespace nvus
