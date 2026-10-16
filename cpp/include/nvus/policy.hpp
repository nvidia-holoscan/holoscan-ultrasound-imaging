#pragma once
#include "nvus/sequence.hpp"
#include <map>

namespace nvus {
// One logical transmit. Hardware may need multiple physical acquisitions to
// observe its full receive aperture.
struct TransmitEvent {
  double angle_deg = 0.;
  uint64_t group_id = 0;
  size_t group_index = 0, group_size = 0;
  std::string group_label;
};
struct TransmitObservation {
  TransmitEvent transmit;
  uint64_t run_id = 0;
  std::vector<Event> physical_events;
  std::vector<EventRecord> acquisitions;
  std::vector<Buffer> buffers;
};
// Feedback from the preceding policy call. next_event makes policies pure:
// they need no hidden cursor to produce the next part of an infinite stream.
struct PolicyObservation {
  uint64_t next_event = 0;
  std::vector<TransmitObservation> completed;
};
class Policy {
public:
  virtual ~Policy() = default;
  virtual std::vector<TransmitEvent>
  next(size_t count, const PolicyObservation &) const = 0;
};
// A stable 4 cm plane-wave stream. Composition is separate from this policy.
class Pw4cmPolicy final : public Policy {
public:
  explicit Pw4cmPolicy(std::vector<double>, std::string label = "pw4cm");
  std::vector<TransmitEvent>
  next(size_t, const PolicyObservation &) const override;
  const std::vector<double> &angles() const { return angles_; }
private:
  std::vector<double> angles_;
  std::string label_;
};
enum class Composition { Interleave, Stack };
class CompositePolicy final : public Policy {
public:
  CompositePolicy(std::shared_ptr<Pw4cmPolicy>,
                  std::shared_ptr<Pw4cmPolicy>, Composition);
  std::vector<TransmitEvent>
  next(size_t, const PolicyObservation &) const override;
private:
  std::shared_ptr<Pw4cmPolicy> first_, second_;
  Composition composition_;
};
std::shared_ptr<Policy> interleave(std::shared_ptr<Pw4cmPolicy>,
                                   std::shared_ptr<Pw4cmPolicy>);
std::shared_ptr<Policy> stack(std::shared_ptr<Pw4cmPolicy>,
                              std::shared_ptr<Pw4cmPolicy>);
// Reconstruction-ready observations, ordered by explicit group_index rather
// than acquisition arrival order.
struct ObservationGroup {
  Observation observation;
  PlaneWaveAction action;
  std::vector<TransmitObservation> transmits;
  std::string label;
};
struct PolicyLimits {
  uint64_t groups = 12, max_duration_ms = 120000;
  size_t queue_capacity = 2;
  bool host_pacing = true, continuous = false;
};
// The only policy execution path. rollout_length changes feedback granularity,
// never the executor or physical acquisition semantics.
class PolicyExecutor final {
public:
  PolicyExecutor(std::shared_ptr<Hardware>, std::shared_ptr<Policy>,
                 size_t rollout_length, PolicyLimits = {});
  ~PolicyExecutor();
  void start();
  void stop() noexcept;
  bool wait(int64_t timeout_ms = -1);
  std::optional<ObservationGroup> next(int64_t timeout_ms = -1);
  Status status() const;
private:
  struct Batch;
  struct Group;
  std::shared_ptr<Batch> compile_next(const PolicyObservation &);
  std::vector<TransmitObservation> observe(const Observation &, const Batch &) const;
  void accept(TransmitObservation);
  void publish(Group &&);
  void execute() noexcept;
  std::shared_ptr<Policy> policy_;
  size_t rollout_length_;
  PolicyLimits limits_;
  uint64_t next_event_ = 0;
  std::atomic<uint64_t> published_{0};
  std::shared_ptr<Run> run_;
  std::shared_ptr<Batch> policy_batch_;
  std::deque<std::shared_ptr<Batch>> batches_;
  std::map<uint64_t, Group> groups_;
  std::thread worker_;
  mutable std::mutex mutex_;
  std::mutex batch_mutex_, join_mutex_;
  std::condition_variable cv_;
  std::deque<ObservationGroup> ready_;
  Clock::time_point deadline_ = Clock::time_point::max();
  std::atomic_bool stopping_{false};
  bool started_ = false, finished_ = false;
  std::string error_;
};
} // namespace nvus
