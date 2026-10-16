#ifdef NVUS_WITH_CUDA
#include "nvus/cuda_buffer.hpp"
#endif
#include "nvus/runtime.hpp"
#include "nvus/policy.hpp"
#include <nanobind/nanobind.h>
#include <nanobind/stl/map.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/shared_ptr.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/vector.h>
#ifdef NVUS_WITH_HOLOLINK
#include "nvus/hololink_transport.hpp"
#endif
namespace nb = nanobind;
using namespace nb::literals;
using namespace nvus;
NB_MODULE(_scanner, m) {
  m.attr("native_source_sha256") = NVUS_NATIVE_SOURCE_SHA256;
  m.attr("repository_revision") = NVUS_REPOSITORY_REVISION;
  m.attr("hardware_available") =
      false; // SDK compilation is not rig qualification.
  m.attr("sdk_available") = false;
#ifdef NVUS_WITH_HOLOLINK
  m.attr("sdk_available") = true;
#endif
  nb::exception<StaleDecision>(m, "StaleDecisionError");
  nb::class_<ProgramWord>(m, "ProgramWord")
      .def(nb::init<>())
      .def_rw("address", &ProgramWord::address)
      .def_rw("value", &ProgramWord::value);
  nb::enum_<MemoryKind>(m, "MemoryKind")
      .value("Host", MemoryKind::Host)
      .value("Cuda", MemoryKind::Cuda);
  nb::enum_<State>(m, "State")
      .value("Ready", State::Ready)
      .value("Running", State::Running)
      .value("Waiting", State::Waiting)
      .value("Stopping", State::Stopping)
      .value("Idle", State::Idle)
      .value("Fault", State::Fault);
  nb::enum_<Completion>(m, "Completion")
      .value("None_", Completion::None)
      .value("Repetitions", Completion::Repetitions)
      .value("EventLimit", Completion::EventLimit)
      .value("TimeLimit", Completion::TimeLimit)
      .value("Stopped", Completion::Stopped)
      .value("PolicyTimeout", Completion::PolicyTimeout)
      .value("Error", Completion::Error);
  nb::enum_<DeadlinePolicy>(m, "DeadlineAction")
      .value("Wait", DeadlinePolicy::Wait)
      .value("Stop", DeadlinePolicy::Stop)
      .value("Repeat", DeadlinePolicy::Repeat);
  nb::class_<Event>(m, "Event")
      .def(nb::init<>())
      .def_rw("event_id", &Event::event_id)
      .def_rw("rx_side", &Event::rx_side)
      .def_rw("samples", &Event::samples)
      .def_rw("interval_ns", &Event::interval_ns)
      .def_rw("packet", &Event::packet)
      .def_prop_ro("packet_bytes", [](const Event &e) {
        return nb::bytes(e.packet.data(), e.packet.size());
      });
  nb::class_<PreparedSequence>(m, "PreparedSequence")
      .def(nb::init<>())
      .def_rw("id", &PreparedSequence::id)
      .def_rw("config_id", &PreparedSequence::config_id)
      .def_rw("events", &PreparedSequence::events)
      .def_rw("programming_manifest", &PreparedSequence::programming_manifest)
      .def_rw("resources", &PreparedSequence::resources)
      .def_rw("rx_start_ticks", &PreparedSequence::rx_start_ticks);
  nb::class_<RunOptions>(m, "RunOptions")
      .def(nb::init<>())
      .def_rw("repetitions", &RunOptions::repetitions)
      .def_rw("max_events", &RunOptions::max_events)
      .def_rw("max_duration_ms", &RunOptions::max_duration_ms)
      .def_rw("queue_capacity", &RunOptions::queue_capacity)
      .def_rw("adaptive", &RunOptions::adaptive)
      .def_rw("host_pacing", &RunOptions::host_pacing)
      .def_rw("decision_timeout_ms", &RunOptions::decision_timeout_ms)
      .def_rw("deadline_action", &RunOptions::deadline_policy);
  nb::class_<Buffer>(m, "Buffer")
      .def_ro("size", &Buffer::size)
      .def_ro("kind", &Buffer::kind)
      .def_ro("device", &Buffer::device)
      .def_ro("device_pointer", &Buffer::data)
      .def(
          "wait_ready",
          [](const Buffer &b) {
            if (b.wait_ready)
              b.wait_ready();
          },
          nb::call_guard<nb::gil_scoped_release>())
      .def(
          "record_consumer",
          [](const Buffer &b, uintptr_t stream) {
            if (!b.record_consumer)
              throw std::runtime_error(
                  "buffer has no consumer-stream lease support");
            b.record_consumer(stream);
          },
          "stream"_a, nb::call_guard<nb::gil_scoped_release>())
      .def("to_bytes", [](const Buffer &b) {
        std::vector<uint8_t> data;
        {
          nb::gil_scoped_release release;
          data = b.copy_host();
        }
        return nb::bytes(data.data(), data.size());
      });
#ifdef NVUS_WITH_CUDA
  m.def("copy_cuda_buffer", &copy_cuda_buffer, "source"_a, "bytes"_a,
        "device"_a, "stream"_a, nb::call_guard<nb::gil_scoped_release>());
#endif
  nb::class_<EventRecord>(m, "EventRecord")
      .def_ro("event_id", &EventRecord::event_id)
      .def_ro("timestamp_ns", &EventRecord::timestamp_ns)
      .def_ro("rx_side", &EventRecord::rx_side)
      .def_ro("samples", &EventRecord::samples)
      .def_ro("timestamp_origin", &EventRecord::timestamp_origin)
      .def_ro("association", &EventRecord::association);
  nb::class_<Observation>(m, "Observation")
      .def_ro("id", &Observation::observation_id)
      .def_ro("run_id", &Observation::run_id)
      .def_ro("boundary", &Observation::boundary)
      .def_ro("sequence_id", &Observation::sequence_id)
      .def_ro("config_id", &Observation::config_id)
      .def_ro("complete", &Observation::complete)
      .def_ro("error", &Observation::error)
      .def_ro("records", &Observation::events)
      .def_ro("buffers", &Observation::buffers);
  nb::class_<Status>(m, "Status")
      .def_ro("state", &Status::state)
      .def_ro("reason", &Status::completion)
      .def_ro("run_id", &Status::run_id)
      .def_ro("attempted_events", &Status::attempted_events)
      .def_ro("received_events", &Status::received_events)
      .def_ro("completed_groups", &Status::completed_groups)
      .def_ro("boundary", &Status::boundary)
      .def_ro("queued_observations", &Status::queued_observations)
      .def_ro("pending_update", &Status::pending_update)
      .def_ro("error", &Status::error)
      .def_ro("cleanup_error", &Status::cleanup_error);
  nb::class_<Hardware>(m, "Hardware");
  nb::class_<FakeHardware, Hardware>(m, "FakeHardware")
      .def(nb::init<>())
      .def_rw("fail_event", &FakeHardware::fail_event)
      .def_rw("delay_ms", &FakeHardware::delay_ms)
      .def_rw("fail_verify", &FakeHardware::fail_verify)
      .def_rw("fail_stop", &FakeHardware::fail_stop)
      .def_prop_ro("acquired",
                   [](const FakeHardware &h) { return h.acquired.load(); })
      .def_prop_ro("stop_calls",
                   [](const FakeHardware &h) { return h.stop_calls.load(); })
      .def_prop_ro("receiver_starts", [](const FakeHardware &h) {
        return h.receiver_starts.load();
      })
      .def_prop_ro("program_calls",
                   [](const FakeHardware &h) { return h.program_calls.load(); })
      .def_prop_ro("verify_calls",
                   [](const FakeHardware &h) { return h.verify_calls.load(); })
      .def_prop_ro("transmit_sequence_updates", [](const FakeHardware &h) {
        return h.transmit_sequence_updates.load();
      });
  nb::class_<Run>(m, "Run")
      .def("start", &Run::start)
      .def("stop", &Run::request_stop, nb::call_guard<nb::gil_scoped_release>())
      .def("wait", &Run::wait, "timeout_ms"_a = -1,
           nb::call_guard<nb::gil_scoped_release>())
      .def("next_observation", &Run::next, "timeout_ms"_a = -1,
           nb::call_guard<nb::gil_scoped_release>())
      .def("status", &Run::status)
      .def("submit_next", &Run::submit, "prepared"_a, "source_observation_id"_a,
           "expected_boundary"_a);
  nb::class_<Scanner>(m, "Scanner")
      .def(nb::init<std::shared_ptr<Hardware>>())
      .def("run", &Scanner::run, "prepared"_a, "options"_a = RunOptions{})
      .def("state", &Scanner::state);
  nb::class_<PlaneWaveConfig>(m, "PlaneWaveConfig")
      .def(nb::init<>())
      .def_rw("depth_mm", &PlaneWaveConfig::depth_mm)
      .def_rw("sound_speed_m_s", &PlaneWaveConfig::sound_speed_m_s)
      .def_rw("interval_us", &PlaneWaveConfig::interval_us);
  nb::class_<PlaneWaveAction>(m, "PlaneWaveAction")
      .def_ro("sequence", &PlaneWaveAction::sequence)
      .def_ro("config", &PlaneWaveAction::config)
      .def_ro("angles_deg", &PlaneWaveAction::angles_deg)
      .def_ro("delays_half_cycles", &PlaneWaveAction::delays_half_cycles);
  m.def("compile_plane_waves", &compile_plane_waves, "angles_deg"_a,
        "config"_a = PlaneWaveConfig{}, nb::call_guard<nb::gil_scoped_release>());
  nb::class_<PolicyLimits>(m, "PolicyLimits")
      .def(nb::init<>())
      .def_rw("groups", &PolicyLimits::groups)
      .def_rw("max_duration_ms", &PolicyLimits::max_duration_ms)
      .def_rw("queue_capacity", &PolicyLimits::queue_capacity)
      .def_rw("host_pacing", &PolicyLimits::host_pacing)
      .def_rw("continuous", &PolicyLimits::continuous);
  nb::class_<TransmitEvent>(m, "TransmitEvent")
      .def_ro("angle_deg", &TransmitEvent::angle_deg)
      .def_ro("group_id", &TransmitEvent::group_id)
      .def_ro("group_index", &TransmitEvent::group_index)
      .def_ro("group_size", &TransmitEvent::group_size)
      .def_ro("group_label", &TransmitEvent::group_label);
  nb::class_<TransmitObservation>(m, "TransmitObservation")
      .def_ro("transmit", &TransmitObservation::transmit)
      .def_ro("run_id", &TransmitObservation::run_id)
      .def_ro("physical_events", &TransmitObservation::physical_events)
      .def_ro("acquisitions", &TransmitObservation::acquisitions)
      .def_ro("buffers", &TransmitObservation::buffers);
  nb::class_<Policy>(m, "Policy");
  nb::class_<Pw4cmPolicy, Policy>(m, "Pw4cmPolicy")
      .def(nb::init<std::vector<double>, std::string>(), "angles_deg"_a,
           "label"_a = "pw4cm")
      .def_prop_ro("angles_deg", &Pw4cmPolicy::angles);
  m.def("interleave", &interleave, "first"_a, "second"_a);
  m.def("stack", &stack, "first"_a, "second"_a);
  nb::class_<ObservationGroup>(m, "ObservationGroup")
      .def_ro("observation", &ObservationGroup::observation)
      .def_ro("action", &ObservationGroup::action)
      .def_ro("transmits", &ObservationGroup::transmits)
      .def_ro("label", &ObservationGroup::label);
  nb::class_<PolicyExecutor>(m, "PolicyExecutor")
      .def(nb::init<std::shared_ptr<Hardware>, std::shared_ptr<Policy>, size_t,
                    PolicyLimits>(), "hardware"_a, "policy"_a,
           "rollout_length"_a, "limits"_a = PolicyLimits{})
      .def("start", &PolicyExecutor::start, nb::call_guard<nb::gil_scoped_release>())
      .def("stop", &PolicyExecutor::stop, nb::call_guard<nb::gil_scoped_release>())
      .def("wait", &PolicyExecutor::wait, "timeout_ms"_a = -1,
           nb::call_guard<nb::gil_scoped_release>())
      .def("next", &PolicyExecutor::next, "timeout_ms"_a = -1,
           nb::call_guard<nb::gil_scoped_release>())
      .def("status", &PolicyExecutor::status);
#ifdef NVUS_WITH_HOLOLINK
  nb::class_<RegisterExpectation>(m, "RegisterExpectation")
      .def(nb::init<>())
      .def_rw("chip", &RegisterExpectation::chip)
      .def_rw("address", &RegisterExpectation::address)
      .def_rw("value", &RegisterExpectation::value)
      .def_rw("mask", &RegisterExpectation::mask);
  nb::class_<HsbExecutionConfig>(m, "HsbExecutionConfig")
      .def(nb::init<>())
      .def_rw("commit_guard_ns", &HsbExecutionConfig::commit_guard_ns)
      .def_rw("sequence_start", &HsbExecutionConfig::sequence_start)
      .def_rw("fpga_auto_guard_ticks",
              &HsbExecutionConfig::fpga_auto_guard_ticks)
      .def_rw("fpga_auto_interval_ticks",
              &HsbExecutionConfig::fpga_auto_interval_ticks)
      .def_rw("fpga_auto_start", &HsbExecutionConfig::fpga_auto_start)
      .def_rw("fpga_tx_release_ticks",
              &HsbExecutionConfig::fpga_tx_release_ticks)
      .def_rw("tx_dual_bank", &HsbExecutionConfig::tx_dual_bank)
      .def_rw("allow_sequence_wrap",
              &HsbExecutionConfig::allow_sequence_wrap);
  nb::class_<HololinkTransportConfig>(m, "HololinkTransportConfig")
      .def(nb::init<>())
      .def_rw("metadata_integers", &HololinkTransportConfig::metadata_integers)
      .def_rw("metadata_strings", &HololinkTransportConfig::metadata_strings)
      .def_rw("metadata_bytes", &HololinkTransportConfig::metadata_bytes)
      .def_rw("ibv_name", &HololinkTransportConfig::ibv_name)
      .def_rw("config_id", &HololinkTransportConfig::config_id)
      .def_rw("ibv_port", &HololinkTransportConfig::ibv_port)
      .def_rw("tx_qp", &HololinkTransportConfig::tx_qp)
      .def_rw("gpu_ordinal", &HololinkTransportConfig::gpu_ordinal)
      .def_rw("rx_pages", &HololinkTransportConfig::rx_pages)
      .def_rw("tx_delay_ticks", &HololinkTransportConfig::tx_delay_ticks)
      .def_rw("wakeup_ticks", &HololinkTransportConfig::wakeup_ticks)
      .def_rw("tx_pulse_ticks", &HololinkTransportConfig::tx_pulse_ticks)
      .def_rw("delay_start_word", &HololinkTransportConfig::delay_start_word)
      .def_rw("rx_arm_delay_s", &HololinkTransportConfig::rx_arm_delay_s)
      .def_rw("rx_bf_sync_with_tx",
              &HololinkTransportConfig::rx_bf_sync_with_tx)
      .def_rw("afe_expected", &HololinkTransportConfig::afe_expected)
      .def_rw("tx_expected", &HololinkTransportConfig::tx_expected)
      .def_rw("commissioning", &HololinkTransportConfig::commissioning);
  m.def("make_hololink_hardware", &make_hololink_hardware, "config"_a,
        "execution"_a = HsbExecutionConfig{});
#endif
}
