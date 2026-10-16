#include "nvus/hsb_backend.hpp"
#include <algorithm>
#include <limits>
#include <map>
#include <stdexcept>

namespace nvus {
namespace {
uint64_t get(const std::vector<uint8_t> &b, size_t at, size_t n) {
  uint64_t value = 0;
  for (size_t i = 0; i < n; ++i)
    value |= uint64_t(b.at(at + i)) << (8 * i);
  return value;
}
void require(bool ok, const char *message) {
  if (!ok)
    throw std::invalid_argument(message);
}
void validate_packet(const Event &event) {
  const auto &p = event.packet;
  require(p.size() == 2176,
          "HSB requires a complete 67-operation event packet");
  require(get(p, 0, 4) == 0x44585455 && p[4] == 1 && p[5] == 32,
          "Unsupported HSB packet protocol");
  require(get(p, 6, 2) == 0 && get(p, 10, 2) == 0 && get(p, 12, 2) == 1 &&
              get(p, 14, 2) == 32 && get(p, 16, 2) == 67 &&
              get(p, 18, 2) == 67 && get(p, 20, 2) == 2144 &&
              get(p, 22, 2) == 0 && get(p, 24, 4) == 2144 && get(p, 28, 4) == 0,
          "Unsupported HSB packet header");
  uint32_t delay_address = uint32_t(get(p, 32 + 4 * 32, 6) >> 32);
  require(delay_address >= 0x82 && delay_address <= 0x27e,
          "Delay memory outside supported range");
  for (size_t i = 0; i < 67; ++i) {
    size_t pos = 32 + i * 32;
    require(get(p, pos + 6, 6) == 0 && get(p, pos + 18, 6) == 0 &&
                p[pos + 24] == 44 && get(p, pos + 25, 7) == 0,
            "Unsupported SPI operation flags or clock count");
    for (size_t chip = 0; chip < 2; ++chip) {
      auto word = get(p, pos + chip * 12, 6);
      auto address = uint32_t(word >> 32), value = uint32_t(word);
      if (i == 0)
        require(address == 0 && value == 0,
                "Missing TX read-disable operation");
      else if (i == 1)
        require(address == 0x1a && value == (event.rx_side ? 0xffffffffu : 0u),
                "RX mux mismatch");
      else if (i == 66)
        require(address == 0x33 && value >= 2 &&
                    value + 1 < delay_address - 0x80,
                "Waveform selection overlaps reserved/delay memory");
      else {
        auto page = (i - 2) / 4, part = (i - 2) % 4;
        if (part < 2)
          require(address == part + 2 && value == (1u << page),
                  "Incorrect TX page selection");
        else
          require(address == delay_address + part - 2 && !(value & 0x80008000u),
                  "Invalid delay word");
      }
    }
  }
  require(get(p, 32 + 66 * 32, 6) == get(p, 44 + 66 * 32, 6),
          "TX chips must select the same waveform");
}
uint32_t validate_sequence(const PreparedSequence &sequence) {
  require(!sequence.events.empty(), "Empty HSB sequence");
  require(!sequence.programming_manifest.empty(),
          "HSB requires an explicit programming manifest");
  const auto samples = sequence.events.front().samples;
  require(samples > 0 && samples % 2 == 0 &&
              samples <= std::numeric_limits<uint32_t>::max() / 96,
          "HSB capture requires positive even sample count within byte-counter "
          "range");
  std::map<uint32_t, uint32_t> words;
  for (const auto &word : sequence.resources)
    require(words.emplace(word.address, word.value).second,
            "Duplicate waveform resource address");
  for (const auto &event : sequence.events) {
    require(event.rx_side <= 1, "HSB receives one of two mux halves per event");
    require(event.samples == samples,
            "Per-event capture geometry is unsupported");
    validate_packet(event);
    const auto selected =
        uint32_t(get(event.packet, 32 + 66 * 32, 4)) + 0x80;
    require(words.count(selected) && words.count(selected + 1),
            "Selected waveform words missing from programming resources");
  }
  return samples;
}
bool same_resources(const std::vector<ProgramWord> &a,
                    const std::vector<ProgramWord> &b) {
  if (a.size() != b.size())
    return false;
  for (size_t i = 0; i < a.size(); ++i)
    if (a[i].address != b[i].address || a[i].value != b[i].value)
      return false;
  return true;
}
bool same_event(const Event &a, const Event &b) {
  return a.event_id == b.event_id && a.packet == b.packet &&
         a.samples == b.samples && a.rx_side == b.rx_side &&
         a.interval_ns == b.interval_ns;
}
} // namespace
HsbBackend::HsbBackend(std::shared_ptr<HsbTransport> transport,
                       HsbExecutionConfig config)
    : transport_(std::move(transport)), config_(config),
      wire_sequence_(config.sequence_start) {
  require(bool(transport_), "Native HSB transport is required");
  require(config.commit_guard_ns <= 1000000000ull,
          "Commit guard must be at most one second");
  require(!config.tx_dual_bank || config.fpga_auto_start,
          "Dual-bank TX requires FPGA automatic sync start");
  require(!config.tx_dual_bank || config.fpga_tx_release_ticks != 0,
          "Dual-bank TX requires a nonzero FPGA release time");
}
HsbBackend::~HsbBackend() {
  try {
    stop();
  } catch (...) {
  }
}
void HsbBackend::program(const PreparedSequence &sequence) {
  const auto samples = validate_sequence(sequence);
  std::lock_guard<std::mutex> lock(mutex_);
  require(!cancelled_ && !stopped_, "Stopped HSB backend cannot be reused");
  require(!programmed_ && !receiver_started_,
          "HSB session configuration can only be programmed once");
  require(wire_sequence_ <= 65535,
          "HSB sequence epoch exhausted; verified reset required");
  verified_ = false;
  transport_->prepare(sequence, size_t(samples) * 96, uint16_t(wire_sequence_),
                      true);
  prepared_ = sequence;
  frame_bytes_ = size_t(samples) * 96;
  programmed_ = true;
}
void HsbBackend::verify(const PreparedSequence &sequence) {
  std::lock_guard<std::mutex> lock(mutex_);
  require(programmed_ && sequence.id == prepared_.id &&
              sequence.config_id == prepared_.config_id,
          "Verification identity differs from programmed sequence");
  verified_ = transport_->verify(prepared_);
  if (!verified_)
    throw std::runtime_error("HSB resource/configuration readback failed");
}
void HsbBackend::set_transmit_sequence(const PreparedSequence &sequence) {
  const auto samples = validate_sequence(sequence);
  std::lock_guard<std::mutex> lock(mutex_);
  require(programmed_ && verified_ && receiver_started_ && !cancelled_ &&
              !stopped_,
          "Streamed transmit update requires an active verified session");
  require(sequence.config_id == prepared_.config_id &&
              sequence.rx_start_ticks == prepared_.rx_start_ticks &&
              samples == prepared_.events.front().samples &&
              same_resources(sequence.resources, prepared_.resources),
          "Streamed transmit update cannot change session configuration, "
          "capture geometry or startup waveform resources");
  require(!config_.tx_dual_bank || submitted_updates_.empty(),
          "Adaptive sequence changed with a TX update still queued");
  // A completed policy batch is the only safe point to roll both host and
  // FPGA packet sequence counters modulo 16 bits.
  if (config_.allow_sequence_wrap && wire_sequence_ == 65536) {
    require(submitted_updates_.empty(),
            "HSB sequence wrap requires an empty TX update queue");
    wire_sequence_ = 0;
  }
  require(wire_sequence_ <= 65535,
          "HSB sequence epoch exhausted; verified reset required");
  // Host-only handoff. Every event is still sent over Ethernet by acquire().
  // There is deliberately no transport prepare/readback or fixed-plan cache.
  prepared_ = sequence;
}
void HsbBackend::start_receiver() {
  std::lock_guard<std::mutex> lock(mutex_);
  require(verified_ && !cancelled_ && !stopped_,
          "HSB receiver requires verified programming");
  if (!receiver_started_) {
    transport_->start_receiver();
    receiver_started_ = true;
  }
}
Buffer HsbBackend::acquire(const Event &event, const std::atomic_bool &stop,
                           Clock::time_point deadline) {
  size_t event_index = 0;
  {
    std::lock_guard<std::mutex> lock(mutex_);
    require(verified_ && receiver_started_ && !stopped_,
            "HSB is not ready to acquire");
    auto match = std::find_if(prepared_.events.begin(), prepared_.events.end(),
                              [&](const Event &candidate) {
                                return same_event(candidate, event);
                              });
    require(match != prepared_.events.end(),
            "Event was not part of verified sequence");
    event_index = size_t(std::distance(prepared_.events.begin(), match));
    require(!config_.tx_dual_bank || submitted_updates_.empty() ||
                same_event(submitted_updates_.front(), event),
            "Acquisition order differs from queued TX updates");
  }

  auto check = [&] {
    if (cancelled_ || stop.load())
      throw std::runtime_error("HSB acquisition cancelled");
    if (Clock::now() >= deadline)
      throw std::runtime_error("HSB acquisition deadline expired");
  };
  auto submit = [&](const Event &update) {
    auto stage = timing_.now();
    check();
    require(wire_sequence_ <= 65535,
            "HSB sequence epoch exhausted; verified reset required");
    auto packet = update.packet;
    packet[8] = uint8_t(wire_sequence_);
    packet[9] = uint8_t(wire_sequence_ >> 8);
    timing_.add(0, stage);

    stage = timing_.now();
    transport_->send_update(packet);
    timing_.add(1, stage);

    stage = timing_.now();
    if (!transport_->flush_update(deadline))
      throw std::runtime_error("HSB update transport flush timed out");
    timing_.add(2, stage);

    stage = timing_.now();
    {
      std::unique_lock<std::mutex> lock(mutex_);
      check();
      if (!config_.fpga_auto_start) {
        auto guard_end =
            Clock::now() + std::chrono::nanoseconds(config_.commit_guard_ns);
        if (guard_end >= deadline)
          throw std::runtime_error("HSB commit guard exceeds event deadline");
        const auto spin_window = std::chrono::microseconds(50);
        if (guard_end - Clock::now() > spin_window)
          cv_.wait_until(lock, guard_end - spin_window,
                         [&] { return cancelled_.load() || stop.load(); });
        while (Clock::now() < guard_end && !cancelled_.load() &&
               !stop.load())
          std::atomic_signal_fence(std::memory_order_seq_cst);
        check();
      }
      timing_.add(3, stage);
      stage = timing_.now();
      if (!config_.fpga_auto_start)
        transport_->trigger();
      timing_.add(4, stage);
      ++wire_sequence_;
    }
  };

  if (config_.tx_dual_bank) {
    if (submitted_updates_.empty()) {
      submit(event);
      submitted_updates_.push_back(event);
    }
    if (submitted_updates_.size() < 2 &&
        event_index + 1 < prepared_.events.size()) {
      const auto &next = prepared_.events[event_index + 1];
      submit(next);
      submitted_updates_.push_back(next);
    }
  } else {
    submit(event);
  }

  auto stage = timing_.now();
  auto frame = transport_->receive(deadline);
  timing_.add(5, stage);
  check();
  if (!frame.buffer.owner || !frame.buffer.data ||
      frame.buffer.size != frame_bytes_ || frame.bytes_written != frame_bytes_)
    throw std::runtime_error(
        "HSB incomplete/unowned capture: owner=" +
        std::to_string(bool(frame.buffer.owner)) +
        " data=" + std::to_string(bool(frame.buffer.data)) +
        " buffer_bytes=" + std::to_string(frame.buffer.size) +
        " bytes_written=" + std::to_string(frame.bytes_written) +
        " expected_bytes=" + std::to_string(frame_bytes_));
  if (frame.dropped ||
      frame.metadata_frame_number != uint16_t(frame.frame_number) ||
      (frame.metadata_psn & 0xfffffu) != (frame.completion_psn & 0xfffffu) ||
      (last_frame_ && frame.frame_number != *last_frame_ + 1))
    throw std::runtime_error(
        "HSB dropped/stale/out-of-order capture metadata: dropped=" +
        std::to_string(frame.dropped) +
        " frame=" + std::to_string(frame.frame_number) +
        " metadata_frame=" + std::to_string(frame.metadata_frame_number) +
        " completion_psn=" + std::to_string(frame.completion_psn) +
        " metadata_psn=" + std::to_string(frame.metadata_psn) +
        " last_frame=" +
        (last_frame_ ? std::to_string(*last_frame_) : std::string("none")));
  if (frame.buffer.kind == MemoryKind::Cuda &&
      (!frame.buffer.wait_ready || !frame.buffer.record_consumer))
    throw std::runtime_error("HSB CUDA buffer lacks readiness/consumer fences");
  if (config_.tx_dual_bank) {
    require(!submitted_updates_.empty() &&
                same_event(submitted_updates_.front(), event),
            "Completed capture differs from queued TX update");
    submitted_updates_.pop_front();
  }
  last_frame_ = frame.frame_number;
  return std::move(frame.buffer);
}
void HsbBackend::cancel() noexcept {
  {
    std::lock_guard<std::mutex> lock(mutex_);
    cancelled_ = true;
  }
  cv_.notify_all();
  transport_->cancel_receive();
}
void HsbBackend::stop() {
  cancel();
  std::lock_guard<std::mutex> lock(mutex_);
  if (stopped_)
    return;
  transport_->stop();
  stopped_ = true;
  receiver_started_ = false;
  verified_ = false;
}
} // namespace nvus
