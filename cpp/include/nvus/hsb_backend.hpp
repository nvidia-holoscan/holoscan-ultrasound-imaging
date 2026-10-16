#pragma once
#include "nvus/runtime.hpp"
#include "nvus/timing.hpp"

namespace nvus {
struct HsbFrame {
  Buffer buffer;
  uint64_t frame_number = 0;
  uint16_t metadata_frame_number = 0;
  uint32_t bytes_written = 0, completion_psn = 0, metadata_psn = 0, dropped = 0;
};
// All implementations are native. This interface is not a Python callback hook.
// A real adapter must own the SDK control/RoCE resources and return detached
// storage (or an enforceable device lease), never a borrowed cyclic DMA page.
class HsbTransport {
public:
  virtual ~HsbTransport() = default;
  // Startup-only session configuration, including capture window, one-shot
  // sync, TX update safety, phase alignment and waveform resources. first=true
  // establishes the FPGA update-sequence epoch. Runtime transmit sequences do
  // not call this method.
  virtual void prepare(const PreparedSequence &, size_t frame_bytes,
                       uint16_t next_sequence, bool first) = 0;
  // Must verify both chips' waveform/delay resources and board configuration.
  virtual bool verify(const PreparedSequence &) = 0;
  virtual void start_receiver() = 0; // Must never trigger TX.
  virtual void send_update(const std::vector<uint8_t> &) = 0;
  virtual bool
  flush_update(Clock::time_point deadline) = 0; // NIC completion, not FPGA ACK.
  virtual void trigger() = 0;                   // Exactly one physical event.
  virtual HsbFrame receive(Clock::time_point deadline) = 0;
  virtual void
  cancel_receive() noexcept = 0; // Wake receive/flush; thread-safe.
  virtual void stop() = 0;       // Quiesce before releasing SDK/DMA resources.
};
struct HsbExecutionConfig {
  uint64_t commit_guard_ns = 50000;
  uint16_t sequence_start = 0;
  uint32_t fpga_auto_guard_ticks = 0;
  uint32_t fpga_auto_interval_ticks = 0;
  uint32_t fpga_tx_release_ticks = 0;
  bool fpga_auto_start = false;
  bool tx_dual_bank = false;
  bool allow_sequence_wrap = false;
};
class HsbBackend final : public Hardware {
public:
  HsbBackend(std::shared_ptr<HsbTransport>, HsbExecutionConfig = {});
  ~HsbBackend() override;
  void program(const PreparedSequence &) override;
  void verify(const PreparedSequence &) override;
  void set_transmit_sequence(const PreparedSequence &) override;
  void start_receiver() override;
  Buffer acquire(const Event &, const std::atomic_bool &,
                 Clock::time_point) override;
  void cancel() noexcept override;
  void stop() override;

private:
  StageTiming<6> timing_{{"packet", "send", "flush", "guard", "trigger", "receive_owned"}};
  std::shared_ptr<HsbTransport> transport_;
  HsbExecutionConfig config_;
  std::mutex mutex_;
  std::condition_variable cv_;
  std::atomic_bool cancelled_{false};
  PreparedSequence prepared_;
  size_t frame_bytes_ = 0;
  uint32_t wire_sequence_;
  bool programmed_ = false, verified_ = false, receiver_started_ = false,
       stopped_ = false;
  std::optional<uint64_t> last_frame_;
  std::deque<Event> submitted_updates_;
};
} // namespace nvus
