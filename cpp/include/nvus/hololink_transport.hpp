#pragma once
#include "nvus/hsb_backend.hpp"
#include <map>

namespace nvus {
struct RegisterExpectation {
  uint32_t chip = 0, address = 0, value = 0, mask = 0xffffffffu;
};
struct HololinkTransportConfig {
  // Enumeration metadata is copied, never a Python-owned SDK object.
  std::map<std::string, int64_t> metadata_integers;
  std::map<std::string, std::string> metadata_strings;
  std::map<std::string, std::vector<uint8_t>> metadata_bytes;
  std::string ibv_name, config_id;
  uint32_t ibv_port = 1, tx_qp = 2, gpu_ordinal = 0, rx_pages = 1024;
  uint32_t tx_delay_ticks = 2020, wakeup_ticks = 2000, tx_pulse_ticks = 80;
  uint32_t delay_start_word = 32;
  double rx_arm_delay_s = 0.;
  bool rx_bf_sync_with_tx = true;
  // Idle Python bring-up is allowed; all supplied expectations are freshly
  // reread natively before execution. Voltage remains reported context only.
  std::vector<RegisterExpectation> afe_expected, tx_expected;
  // SDK compilation is not on-rig qualification. Caller must deliberately
  // opt into commissioning and establish a fresh verified update epoch.
  bool commissioning = false;
};
// Creates owners only; no enumeration/register access/TX until Run starts.
std::shared_ptr<Hardware> make_hololink_hardware(HololinkTransportConfig,
                                                 HsbExecutionConfig = {});
} // namespace nvus
