// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
// http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

module lvds_mixed_6ch_if
  import apb_pkg::*;
#(
    parameter int SERDES_W = 6,
    parameter int SAMPLE_W = 12
) (
    input  logic                         reset,

    input  logic                         apb_clk,
    input  logic                         apb_rst,
    input  apb_m2s                       i_apb_m2s,
    output apb_s2m                       o_apb_s2m,

    input  logic                         inclock,
    input  logic                         rx_bf_sync_req,
    input  logic                [5:0] rx_in,

    output logic                         rx_coreclock,
    output logic                         rx_reset,
    output logic                         pll_locked,
    output logic                [5:0] rx_dpa_locked,
    output logic     [6*SERDES_W-1:0] rx_out,
    output logic     [6*SAMPLE_W-1:0] aligned_data,
    output logic                [5:0] aligned_valid,
    output logic     [6*SAMPLE_W-1:0] group_aligned_data,
    output logic                         group_aligned_valid,
    output logic                [5:0] lane_locked,
    output logic                [5:0] lane_timeout,
    output logic                [5:0] lane_phase,
    output logic [31:0]                  debug_status,
    output logic                         group_locked,
    output logic                         group_busy,
    output logic                         group_done,
    output logic                         group_timeout
);

localparam logic RESET_INACTIVE = 1'b0;

lvds_MIXED_6CH u_lvds_ip (
    .rx_in            (rx_in),
    .rx_out           (rx_out),
    .rx_bitslip_reset (6'b0),
    .rx_bitslip_ctrl  (6'b0),
    .rx_bitslip_max   (),
    .pll_areset       (reset),
    .rx_dpa_locked    (rx_dpa_locked),
    .rx_dpa_hold      (6'b0),
    .rx_dpa_reset     ({6{RESET_INACTIVE}}),
    .rx_fifo_reset    ({6{RESET_INACTIVE}}),
    .rx_coreclock     (rx_coreclock),
    .inclock          (inclock),
    .pll_locked       (pll_locked)
);

reset_sync u_rx_reset_sync (
    .i_clk    (rx_coreclock),
    .i_arst_n (1'b1),
    .i_srst   (reset),
    .i_locked (pll_locked),
    .o_arst   (),
    .o_arst_n (),
    .o_srst   (rx_reset),
    .o_srst_n ()
);

lvds_rx_group #(
    .N_LANES  (6),
    .SERDES_W (SERDES_W),
    .SAMPLE_W (SAMPLE_W),
    .GROUP_ID (32'h4d583036) // "MX06"
) u_rx_group (
    .reset              (rx_reset),
    .apb_clk            (apb_clk),
    .apb_rst            (apb_rst),
    .i_apb_m2s          (i_apb_m2s),
    .o_apb_s2m          (o_apb_s2m),
    .rx_clk             (rx_coreclock),
    .pll_locked         (pll_locked),
    .rx_dpa_locked      (rx_dpa_locked),
    .rx_data            (rx_out),
    .rx_bf_sync_req     (rx_bf_sync_req),
    .aligned_data       (aligned_data),
    .aligned_valid      (aligned_valid),
    .group_aligned_data (group_aligned_data),
    .group_aligned_valid (group_aligned_valid),
    .lane_locked        (lane_locked),
    .lane_timeout       (lane_timeout),
    .lane_phase         (lane_phase),
    .debug_status       (debug_status),
    .group_locked       (group_locked),
    .group_busy         (group_busy),
    .group_done         (group_done),
    .group_timeout      (group_timeout)
);

endmodule
