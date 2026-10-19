// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0
#include "nvus/hololink_transport.hpp"
#include "nvus/cuda_buffer.hpp"
#include "nvus/thread_affinity.hpp"
#include <algorithm>
#include <array>
#include <cmath>
#include <cuda.h>
#include <cstdio>
#include <exception>
#include <hololink/core/data_channel.hpp>
#include <hololink/core/metadata.hpp>
#include <hololink/core/networking.hpp>
#include <hololink/operators/roce_receiver/roce_receiver.hpp>
#include <hololink/operators/roce_transmitter/roce_transmitter.hpp>
#include <limits>
#include <map>
#include <set>
#include <sys/socket.h>
#include <tuple>
#include <unistd.h>

namespace nvus {
namespace {
// Compare only defined configuration fields; other bits need not read back.
constexpr uint32_t afe_95_config_mask = 0x1073u;
void check(CUresult result) {
  if (result != CUDA_SUCCESS)
    throw std::runtime_error("CUDA driver failure: " + std::to_string(result));
}
void need(bool ok, const char *message) {
  if (!ok)
    throw std::runtime_error(message);
}
struct Context {
  CUdevice device;
  CUcontext context;
  explicit Context(unsigned ordinal) {
    check(cuInit(0));
    check(cuDeviceGet(&device, ordinal));
    check(cuDevicePrimaryCtxRetain(&context, device));
  }
  ~Context() { cuDevicePrimaryCtxRelease(device); }
  void current() { check(cuCtxSetCurrent(context)); }
};
// The deployed SDK does not release RDMA resources when blocking_monitor
// throws. A failed receiver therefore retains its registered allocation until
// process exit.
std::atomic_bool receiver_process_faulted{false};
struct QuarantinedReceiver {
  std::unique_ptr<hololink::operators::RoceReceiver> receiver;
  std::unique_ptr<hololink::ReceiverMemoryDescriptor> ring;
  std::shared_ptr<Context> context;
};
class HololinkTransport final : public HsbTransport {
  StageTiming<2> timing_{{"receive_wait", "rf_snapshot"}};
  HololinkTransportConfig config_;
  HsbExecutionConfig execution_;
  std::shared_ptr<hololink::DataChannel> channel_;
  std::shared_ptr<hololink::Hololink> control_;
  std::shared_ptr<Context> context_;
  std::unique_ptr<hololink::ReceiverMemoryDescriptor> ring_;
  std::unique_ptr<hololink::operators::RoceReceiver> receiver_;
  std::unique_ptr<hololink::operators::RoceTransmitter> sender_;
  std::thread monitor_;
  std::atomic_bool cancelled_{false};
  std::atomic_bool monitor_clean_{false};
  CUstream copy_stream_ = nullptr;
  bool flush_gpudirect_ = false;
  int socket_ = -1;
  size_t frame_bytes_ = 0;
  std::exception_ptr monitor_error_;
  std::mutex monitor_mutex_;
  std::vector<ProgramWord> expected_words_;
  std::map<std::tuple<uint32_t, uint32_t, uint32_t>, uint32_t> undo_words_;
  std::map<std::pair<uint32_t, uint32_t>, uint32_t> undo_registers_;
  bool idle_saved_ = false;
  uint32_t idle_spi_ = 1, idle_board_ = 0;
  void snapshot_tx(const PreparedSequence &sequence) {
    if (!idle_saved_) {
      idle_spi_ = control_->read_uint32(txbase + 4);
      idle_board_ = control_->read_uint32(0x60000000);
      idle_saved_ = true;
    }
    write(txbase + 4, 16);
    std::set<uint32_t> addresses{0x80 + config_.delay_start_word,
                                 0x81 + config_.delay_start_word};
    for (auto &word : sequence.resources)
      addresses.insert(word.address);
    for (uint32_t chip = 1; chip <= 2; ++chip) {
      for (auto reg : {2u, 3u, 0xdu, 0xeu, 0xfu, 0x10u, 0x11u, 0x12u, 0x13u,
                       0x14u, 0x15u, 0x1au, 0x33u}) {
        auto key = std::make_pair(chip, reg);
        if (!undo_registers_.count(key))
          undo_registers_.emplace(key, tx_read(chip, reg));
      }
      for (uint32_t page = 0; page < 16; ++page) {
        tx_write(chip, 2, 1u << page);
        tx_write(chip, 3, 1u << page);
        for (auto address : addresses) {
          auto key = std::make_tuple(chip, page, address);
          if (!undo_words_.count(key))
            undo_words_.emplace(key, tx_read(chip, address));
        }
      }
    }
  }
  void restore_tx() {
    if (!idle_saved_)
      return;
    std::exception_ptr error;
    auto attempt = [&](auto action) {
      try {
        action();
      } catch (...) {
        if (!error)
          error = std::current_exception();
      }
    };
    attempt([&] { write(txbase + 4, 16); });
    for (auto &item : undo_words_)
      attempt([&] {
        auto [chip, page, address] = item.first;
        tx_write(chip, 2, 1u << page);
        tx_write(chip, 3, 1u << page);
        tx_write(chip, address, item.second);
        need(tx_read(chip, address) == item.second,
             "TX memory restoration readback failed");
      });
    for (auto &item : undo_registers_)
      if (item.first.second > 3)
        attempt([&] {
          auto [chip, address] = item.first;
          tx_write(chip, address, item.second);
          need(tx_read(chip, address) == item.second,
               "TX register restoration readback failed");
        });
    for (auto &item : undo_registers_)
      if (item.first.second <= 3)
        attempt([&] {
          auto [chip, address] = item.first;
          tx_write(chip, address, item.second);
          need(tx_read(chip, address) == item.second,
               "TX page restoration readback failed");
        });
    attempt([&] { write(0x60000000, idle_board_); });
    attempt([&] { write(txbase + 4, idle_spi_); });
    if (error)
      std::rethrow_exception(error);
    idle_saved_ = false;
    undo_words_.clear();
    undo_registers_.clear();
  }
  static constexpr uint32_t txbase = 0x70000000, sync = 0x40000000,
                            capture = 0x50000000;
  void write(uint32_t address, uint32_t value) {
    need(control_->write_uint32(address, value), "HSB register write failed");
  }
  void wait_spi() {
    auto end = Clock::now() + std::chrono::milliseconds(250);
    for (;;) {
      auto status = control_->read_uint32(txbase + 0x80);
      need(!(status & 2), "TX-HS SPI error");
      if ((status & 16) && !(status & 1))
        return;
      need(Clock::now() < end, "TX-HS SPI timeout");
      std::this_thread::sleep_for(std::chrono::microseconds(100));
    }
  }
  void tx_write_raw(uint32_t chip, uint32_t address, uint32_t value) {
    write(txbase + (chip == 1 ? 8 : 16), address);
    write(txbase + (chip == 1 ? 12 : 20), value);
    write(txbase, 1 | (chip << 4));
    wait_spi();
    write(txbase, 0);
  }
  void tx_write(uint32_t chip, uint32_t address, uint32_t value) {
    tx_write_raw(chip, 0, 0);
    tx_write_raw(chip, address, value);
  }
  uint32_t tx_read(uint32_t chip, uint32_t address) {
    tx_write_raw(chip, 0, 2);
    try {
      write(txbase + (chip == 1 ? 8 : 16), address);
      write(txbase, 3 | (chip << 8));
      wait_spi();
      auto value = control_->read_uint32(txbase + 0x84);
      write(txbase, 0);
      tx_write_raw(chip, 0, 0);
      return value;
    } catch (...) {
      try {
        tx_write_raw(chip, 0, 0);
      } catch (...) {
      }
      throw;
    }
  }
  uint32_t afe_read(uint32_t chip, uint32_t address) {
    auto board = control_->read_uint32(0x60000000);
    write(0x60000000, (board & ~3u) | chip);
    auto spi = control_->get_spi(0, 0, 15, 1, 1, 1);
    try {
      spi->spi_transaction({0}, {0, 2}, 0);
      auto bytes = spi->spi_transaction({uint8_t(address)}, {}, 2);
      spi->spi_transaction({0}, {0, 0}, 0);
      write(0x60000000, board);
      need(bytes.size() == 3, "AFE SPI read length mismatch");
      return (uint32_t(bytes[1]) << 8) | bytes[2];
    } catch (...) {
      try {
        spi->spi_transaction({0}, {0, 0}, 0);
      } catch (...) {
      }
      try {
        write(0x60000000, board);
      } catch (...) {
      }
      throw;
    }
  }
  void restore_pages() {
    for (unsigned chip = 1; chip <= 2; ++chip) {
      tx_write(chip, 2, 0xffff);
      tx_write(chip, 3, 0xffff);
    }
  }
  void connect() {
    if (control_)
      return;
    hololink::Metadata metadata;
    for (auto &item : config_.metadata_integers)
      metadata[item.first] = item.second;
    for (auto &item : config_.metadata_strings)
      metadata[item.first] = item.second;
    for (auto &item : config_.metadata_bytes)
      metadata[item.first] = item.second;
    channel_ = std::make_shared<hololink::DataChannel>(metadata);
    control_ = channel_->hololink();
    control_->start();
  }

public:
  HololinkTransport(HololinkTransportConfig config,
                    HsbExecutionConfig execution)
      : config_(std::move(config)), execution_(execution) {
    need(!receiver_process_faulted,
         "A prior receiver failure requires process restart");
    need(config_.commissioning, "Native SDK adapter requires explicit "
                                "commissioning opt-in; not rig-qualified");
    need(!config_.ibv_name.empty() && !config_.config_id.empty(),
         "NIC and board configuration identity required");
    need(config_.rx_pages >= 2 && config_.rx_pages <= 1024,
         "RX ring must contain 2..1024 pages");
    need(std::isfinite(config_.rx_arm_delay_s) && config_.rx_arm_delay_s >= 0. &&
             config_.rx_arm_delay_s <= 30.,
         "RX arm delay must be finite and inside 0..30 seconds");
    for (unsigned chip = 1; chip <= 2; ++chip) {
      for (auto reg : {0x92u, 0x95u})
        need(std::any_of(config_.afe_expected.begin(),
                         config_.afe_expected.end(),
                         [&](auto &e) {
                           if (e.chip != chip || e.address != reg)
                             return false;
                           return reg == 0x95 ? e.mask == afe_95_config_mask
                                              : (e.mask & 3u) == 3u;
                         }),
             "Both AFEs require LPF/PGA expectations");
      for (auto reg : {0x1fu, 0x54u, 0x0bu, 0x6eu})
        need(std::any_of(config_.tx_expected.begin(), config_.tx_expected.end(),
                         [&](auto &e) {
                           uint32_t required = reg == 0x1f   ? 0x7fu
                                               : reg == 0xb  ? 0xf001u
                                               : reg == 0x6e ? 8u
                                                             : 0xffffffffu;
                           return e.chip == chip && e.address == reg &&
                                  (e.mask & required) == required;
                         }),
             "Both TX chips require attenuation/source/clock expectations");
    }
    for (auto &e : config_.afe_expected)
      need(e.chip >= 1 && e.chip <= 2 && e.address <= 255,
           "Invalid AFE readback target");
    for (auto &e : config_.tx_expected)
      need(e.chip >= 1 && e.chip <= 2 && e.address <= 1023,
           "Invalid TX readback target");
  }
  ~HololinkTransport() override {
    try {
      stop();
    } catch (...) {
    }
  }
  void prepare(const PreparedSequence &sequence, size_t bytes,
               uint16_t next_sequence, bool first) override {
    need(!receiver_process_faulted,
         "A prior receiver failure requires process restart");
    need(sequence.config_id == config_.config_id,
         "Idle board configuration differs from prepared sequence");
    need(!sequence.programming_manifest.empty() && !sequence.resources.empty(),
         "Explicit programming manifest and waveform words required");
    need(!first || next_sequence == 0,
         "Fresh idle bring-up must establish wire sequence zero");
    if (first)
      cancelled_ = false;
    need(!cancelled_, "Transport cancelled");
    connect();
    write(sync, 0);
    snapshot_tx(sequence);
    for (const auto &event : sequence.events)
      need((uint32_t(event.packet.at(164)) |
            (uint32_t(event.packet.at(165)) << 8)) ==
               0x80 + config_.delay_start_word,
           "Packet delay allocation differs from native profile selection");
    expected_words_ = sequence.resources;
    try {
      for (unsigned chip = 1; chip <= 2; ++chip) {
        tx_write(chip, 2, 0xffff);
        tx_write(chip, 3, 0xffff);
        for (auto &w : expected_words_) {
          need(w.address >= 0x82 && w.address < 0x80 + config_.delay_start_word,
               "Waveform word overlaps delay memory");
          tx_write(chip, w.address, w.value);
        }
        tx_write(chip, 0x15, tx_read(chip, 0x15) & ~0x1000u);
        for (unsigned reg = 0x0d; reg < 0x15; ++reg)
          tx_write(chip, reg,
                   config_.delay_start_word | (config_.delay_start_word << 16));
      }
      restore_pages();
    } catch (...) {
      try {
        restore_pages();
      } catch (...) {
      }
      throw;
    }
    frame_bytes_ = bytes;
    write(capture, 2);
    write(capture, 4);
    write(capture + 4, 3);
    write(capture + 8, 0);
    write(capture + 12, uint32_t(bytes / 48));
    // Program one physical event per update, with update safety.
    uint32_t cfg = 0x4 | 0x8 | 0x10 | 0x40 | 0x80 | 0x100 | 0x200;
    if (config_.rx_bf_sync_with_tx)
      cfg |= 0x2 | 0x20;
    if (execution_.fpga_auto_start)
      cfg |= 0x400;
    if (execution_.tx_dual_bank)
      cfg |= 0x800;
    write(sync + 4, cfg);
    write(sync + 8, config_.wakeup_ticks);
    write(sync + 12, config_.tx_delay_ticks);
    write(sync + 16, config_.tx_pulse_ticks);
    write(sync + 20, config_.tx_delay_ticks);
    write(sync + 24, config_.rx_bf_sync_with_tx ? 20 : 0);
    need(uint64_t(config_.tx_delay_ticks) + sequence.rx_start_ticks <=
             UINT32_MAX,
         "Capture timing overflow");
    if (execution_.tx_dual_bank) {
      const uint64_t capture_start =
          uint64_t(config_.tx_delay_ticks) + sequence.rx_start_ticks;
      const uint64_t capture_ticks = uint64_t(bytes / 96) * 4;
      need(execution_.fpga_auto_start,
           "Dual-bank TX requires FPGA automatic sync start");
      need(execution_.fpga_tx_release_ticks > capture_start,
           "TX update release must occur after capture starts");
      need(execution_.fpga_tx_release_ticks < capture_start + capture_ticks,
           "TX update release must occur before capture ends");
    }
    write(sync + 28, config_.tx_delay_ticks + sequence.rx_start_ticks);
    write(sync + 32, (config_.rx_bf_sync_with_tx ? 0x1f : 0x1e) |
                         (execution_.tx_dual_bank ? 0x20 : 0));
    write(sync + 36, 1);
    write(sync + 40, execution_.fpga_auto_interval_ticks);
    if (execution_.fpga_auto_start)
      write(sync + 44, execution_.fpga_auto_guard_ticks);
    if (execution_.tx_dual_bank)
      write(sync + 48, execution_.fpga_tx_release_ticks);
    if (first) {
      write(0x01200004, 1);
      write(0x01200008, 0);
      write(0x01200000, 4);
      write(0x01200000, 0);
      write(0x01200000, 3);
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
  }
  bool verify(const PreparedSequence &sequence) override {
    bool matched = true;
    write(txbase + 4, 16);
    try {
      for (auto &e : config_.afe_expected)
        matched &= (afe_read(e.chip, e.address) & e.mask) == (e.value & e.mask);
      for (auto &e : config_.tx_expected)
        matched &= (tx_read(e.chip, e.address) & e.mask) == (e.value & e.mask);
      for (unsigned chip = 1; chip <= 2; ++chip) {
        matched &= (tx_read(chip, 0x15) & 0x1000) == 0;
        for (unsigned reg = 0x0d; reg < 0x15; ++reg)
          matched &= tx_read(chip, reg) == (config_.delay_start_word |
                                            (config_.delay_start_word << 16));
      }
      matched &= control_->read_uint32(capture + 12) == frame_bytes_ / 48;
      matched &= control_->read_uint32(capture + 4) == 3;
      matched &= control_->read_uint32(capture + 8) == 0;
      uint32_t cfg = 0x4 | 0x8 | 0x10 | 0x40 | 0x80 | 0x100 | 0x200;
      if (config_.rx_bf_sync_with_tx)
        cfg |= 0x2 | 0x20;
      if (execution_.fpga_auto_start)
        cfg |= 0x400;
      if (execution_.tx_dual_bank)
        cfg |= 0x800;
      for (auto item : std::vector<std::pair<uint32_t, uint32_t>>{
               {4, cfg},
               {8, config_.wakeup_ticks},
               {12, config_.tx_delay_ticks},
               {16, config_.tx_pulse_ticks},
               {20, config_.tx_delay_ticks},
               {24, config_.rx_bf_sync_with_tx ? 20u : 0u},
               {28, config_.tx_delay_ticks + sequence.rx_start_ticks},
               {32, (config_.rx_bf_sync_with_tx ? 0x1fu : 0x1eu) |
                    (execution_.tx_dual_bank ? 0x20u : 0u)},
               {36, 1},
               {40, execution_.fpga_auto_interval_ticks}})
        matched &= control_->read_uint32(sync + item.first) == item.second;
      if (execution_.fpga_auto_start)
        matched &= control_->read_uint32(sync + 44) ==
                   execution_.fpga_auto_guard_ticks;
      if (execution_.tx_dual_bank)
        matched &= control_->read_uint32(sync + 48) ==
                   execution_.fpga_tx_release_ticks;
      for (unsigned chip = 1; chip <= 2; ++chip)
        for (unsigned page = 0; page < 16; ++page) {
          tx_write(chip, 2, 1u << page);
          tx_write(chip, 3, 1u << page);
          for (auto &w : expected_words_)
            matched &= tx_read(chip, w.address) == w.value;
        }
      restore_pages();
      write(txbase + 4, 1);
    } catch (...) {
      try {
        restore_pages();
        write(txbase + 4, 1);
      } catch (...) {
      }
      throw;
    }
    return matched;
  }
  void start_receiver() override {
    need(!receiver_process_faulted,
         "A prior receiver failure requires process restart");
    if (receiver_)
      return;
    context_ = std::make_shared<Context>(config_.gpu_ordinal);
    context_->current();
    check(cuStreamCreate(&copy_stream_, CU_STREAM_NON_BLOCKING));
    auto metadata_offset = (frame_bytes_ + 127) / 128 * 128;
    auto page_size = metadata_offset + 128;
    auto allocation = (page_size * config_.rx_pages + 4095) / 4096 * 4096;
    ring_ = std::make_unique<hololink::ReceiverMemoryDescriptor>(
        context_->context, allocation);
    if (!ring_->get_host_ptr()) {
      int ordering, options;
      check(cuDeviceGetAttribute(
          &ordering, CU_DEVICE_ATTRIBUTE_GPU_DIRECT_RDMA_WRITES_ORDERING,
          context_->device));
      if (ordering < CU_GPU_DIRECT_RDMA_WRITES_ORDERING_OWNER) {
        check(cuDeviceGetAttribute(
            &options, CU_DEVICE_ATTRIBUTE_GPU_DIRECT_RDMA_FLUSH_WRITES_OPTIONS,
            context_->device));
        need(options & CU_FLUSH_GPU_DIRECT_RDMA_WRITES_OPTION_HOST,
             "GPU RDMA writes require an unsupported visibility fence");
        flush_gpudirect_ = true;
      }
    }
    socket_ = ::socket(AF_INET, SOCK_DGRAM, 0);
    need(socket_ >= 0, "RX socket creation failed");
    channel_->configure_socket(socket_);
    receiver_ = std::make_unique<hololink::operators::RoceReceiver>(
        config_.ibv_name.c_str(), config_.ibv_port, ring_->get(), allocation,
        frame_bytes_, page_size, config_.rx_pages, metadata_offset,
        channel_->peer_ip().c_str(), config_.rx_pages, ring_->get_host_ptr());
    need(receiver_->start(), "Native RoCE receiver start failed");
    monitor_ = std::thread([this] {
      try {
        pin_thread_from_environment("NVUS_RECEIVE_CPU");
        context_->current();
        receiver_->blocking_monitor();
        monitor_clean_ = true;
      } catch (...) {
        std::lock_guard<std::mutex> lock(monitor_mutex_);
        receiver_process_faulted = true;
        monitor_error_ = std::current_exception();
      }
    });
    channel_->authenticate(receiver_->get_qp_number(), receiver_->get_rkey());
    channel_->configure_roce(receiver_->external_frame_memory(), frame_bytes_,
                             page_size, config_.rx_pages, 4791);
    sender_ = std::make_unique<hololink::operators::RoceTransmitter>(
        config_.ibv_name, config_.ibv_port, 4096, 2, channel_->peer_ip(),
        config_.tx_qp);
    sender_->start(); // No initial event update or trigger here.
    if (config_.rx_arm_delay_s > 0.) {
      std::fprintf(stderr, "receiver_arm_delay_s=%.6f\n",
                   config_.rx_arm_delay_s);
      auto until = Clock::now() + std::chrono::duration<double>(config_.rx_arm_delay_s);
      while (!cancelled_ && Clock::now() < until)
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
      need(!cancelled_, "Receiver arm delay cancelled");
    }
  }
  void send_update(const std::vector<uint8_t> &packet) override {
    need(!receiver_process_faulted,
         "A prior receiver failure requires process restart");
    need(!cancelled_, "Transport cancelled");
    context_->current();
    sender_->queue_host_buffer(packet.data(), packet.size());
  }
  bool flush_update(Clock::time_point deadline) override {
    while (!cancelled_ && Clock::now() < deadline)
      if (sender_->flush(std::chrono::milliseconds(1)))
        return true;
    return false;
  }
  void trigger() override {
    need(!receiver_process_faulted,
         "A prior receiver failure requires process restart");
    need(!cancelled_, "Transport cancelled");
    write(sync, 1);
    write(sync, 0);
  }
  HsbFrame receive(Clock::time_point deadline) override {
    auto stage = timing_.now();
    context_->current();
    hololink::operators::RoceReceiverMetadata meta;
    bool received = false;
    while (!cancelled_ && Clock::now() < deadline) {
      {
        std::lock_guard<std::mutex> lock(monitor_mutex_);
        if (monitor_error_)
          std::rethrow_exception(monitor_error_);
      }
      if (receiver_->get_next_frame(5, meta)) {
        received = true;
        break;
      }
    }
    need(received, "Native receive cancelled or deadline exceeded");
    if (flush_gpudirect_) {
      check(cuFlushGPUDirectRDMAWrites(
          CU_FLUSH_GPU_DIRECT_RDMA_WRITES_TARGET_CURRENT_CTX,
          CU_FLUSH_GPU_DIRECT_RDMA_WRITES_TO_OWNER));
      std::array<uint8_t, hololink::METADATA_SIZE> metadata;
      check(cuMemcpyDtoH(metadata.data(), meta.metadata_memory, metadata.size()));
      meta.frame_metadata = hololink::Hololink::deserialize_metadata(
          metadata.data(), metadata.size());
    }
    timing_.add(0, stage);
    stage = timing_.now();
    HsbFrame result;
    result.buffer = copy_cuda_buffer(
        meta.frame_memory, frame_bytes_, config_.gpu_ordinal,
        reinterpret_cast<uintptr_t>(copy_stream_));
    result.frame_number = meta.frame_number;
    result.metadata_frame_number = meta.frame_metadata.frame_number;
    result.bytes_written = uint32_t(meta.frame_metadata.bytes_written);
    result.metadata_psn = meta.frame_metadata.psn;
    result.completion_psn = meta.imm_data >> 12;
    result.dropped = meta.dropped;
    // The borrowed DMA page is safe only until another event can reuse it.
    result.buffer.wait_ready();
    timing_.add(1, stage);
    return result;
  }
  void cancel_receive() noexcept override { cancelled_ = true; }
  void stop() override {
    cancelled_ = true;
    std::exception_ptr error;
    auto attempt = [&](auto action) {
      try {
        action();
      } catch (...) {
        if (!error)
          error = std::current_exception();
      }
    };
    bool disabled = false;
    if (control_) {
      attempt([&] { write(sync + 4, 0); });
      attempt([&] { write(capture, 2); });
      attempt([&] { write(0x01200000, 0); });
      attempt([&] {
        disabled = control_->read_uint32(sync + 4) == 0 &&
                   control_->read_uint32(0x01200000) == 0;
        need(disabled,
             "Cannot verify disabled TX/update ingress; restoration skipped");
      });
      if (channel_)
        attempt([&] { channel_->unconfigure(); });
    }
    if (sender_)
      attempt([&] { sender_->stop(); });
    sender_.reset();
    if (control_ && disabled)
      attempt([&] {
        auto deadline = Clock::now() + std::chrono::milliseconds(250);
        while (control_->read_uint32(txbase + 0x80) & 1) {
          need(Clock::now() < deadline,
               "TX update engine did not quiesce before restoration");
          std::this_thread::sleep_for(std::chrono::microseconds(100));
        }
        restore_tx();
      });
    if (receiver_)
      attempt([&] { receiver_->close(); });
    if (monitor_.joinable())
      monitor_.join();
    if (receiver_ && !monitor_clean_) {
      receiver_process_faulted = true;
      // Intentional process-lifetime quarantine: freeing a still-registered DMA
      // allocation would be unsafe. The SDK exposes no exception cleanup API.
      (void)new QuarantinedReceiver{std::move(receiver_), std::move(ring_),
                                    context_};
      if (!error)
        error = std::make_exception_ptr(
            std::runtime_error("Receiver cleanup unproven; RDMA allocation "
                               "quarantined; restart process"));
    }
    receiver_.reset();
    if (socket_ >= 0) {
      ::close(socket_);
      socket_ = -1;
    }
    if (context_) {
      attempt([&] { context_->current(); });
      if (copy_stream_) {
        attempt([&] { check(cuStreamSynchronize(copy_stream_)); });
        attempt([&] { check(cuStreamDestroy(copy_stream_)); });
        copy_stream_ = nullptr;
      }
      ring_.reset();
    }
    if (control_)
      attempt([&] { control_->stop(); });
    control_.reset();
    channel_.reset();
    context_.reset();
    if (error)
      std::rethrow_exception(error);
  }
};
} // namespace
std::shared_ptr<Hardware> make_hololink_hardware(HololinkTransportConfig config,
                                                 HsbExecutionConfig execution) {
  return std::make_shared<HsbBackend>(
      std::make_shared<HololinkTransport>(std::move(config), execution),
      execution);
}
} // namespace nvus
