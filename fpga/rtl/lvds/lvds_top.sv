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

module lvds_top
  import apb_pkg::*;
#(
    parameter int AFE_LANES = 16,
    parameter int SAMPLE_W  = 12,
    parameter int DEBUG_W   = 32
) (
    input  logic                          reset,

    input  logic                          apb_clk,
    input  logic                          apb_rst,
    input  apb_m2s                        i_apb_m2s,
    output apb_s2m                        o_apb_s2m,

    input  logic                          afe1_dclk_p,
    input  logic                          afe1_fclk_p, // Monitor only; not used for alignment
    input  logic [AFE_LANES-1:0]          afe1_dout_p,

    input  logic                          afe2_dclk_p,
    input  logic                          afe2_fclk_p, // Monitor only; not used for alignment
    input  logic [AFE_LANES-1:0]          afe2_dout_p,

    // Single output clock: the group assembler resyncs both AFEs' aligned
    // data into this one domain before it leaves lvds_top, so afe1_aligned_data
    // and afe2_aligned_data are both valid here.
    output logic                          lvds_out_clk,
    output logic [AFE_LANES*SAMPLE_W-1:0] afe1_aligned_data,
    output logic                          afe1_aligned_valid,
    output logic [AFE_LANES*SAMPLE_W-1:0] afe2_aligned_data,
    output logic                          afe2_aligned_valid,
    output logic                          afe_sample_phase,
    output logic                          mixed_stream_clk,
    input  logic                          rx_bf_sync_req,
    output logic [DEBUG_W-1:0]            debug_status
);

localparam int SERDES_W            = 6;
localparam int LVDS_TOP_N_CTRL     = 2;
localparam int LVDS_TOP_N_STAT     = 8;
localparam int LVDS_APB_N_SPORT    = 4;
localparam int LVDS_APB_W_OFST     = 8;
localparam int LVDS_APB_W_SW       = 4;

localparam logic [LVDS_TOP_N_CTRL*32-1:0] LVDS_TOP_CTRL_RST_VAL = {
    32'h00000000, // ctrl[1] reserved
    32'h00000000  // ctrl[0]: bit1 assembler clear, bit2 reserved
};
localparam logic [15:0] AFE_LANES_STAT       = AFE_LANES;
localparam logic [15:0] SERDES_W_STAT        = SERDES_W;
localparam logic [15:0] LVDS_TOP_N_CTRL_STAT = LVDS_TOP_N_CTRL;
localparam logic [15:0] LVDS_TOP_N_STAT_STAT = LVDS_TOP_N_STAT;

apb_m2s lvds_apb_m2s_in  [1];
apb_s2m lvds_apb_s2m_out [1];
apb_m2s lvds_apb_m2s     [LVDS_APB_N_SPORT];
apb_s2m lvds_apb_s2m     [LVDS_APB_N_SPORT];

logic [31:0] lvds_top_ctrl [LVDS_TOP_N_CTRL];
logic [31:0] lvds_top_stat [LVDS_TOP_N_STAT];
logic [14:0] afe1_rx_in_15ch;
logic [10:0] afe2_rx_in_11ch;
logic [5:0]  mixed_rx_in_6ch;

(* keep = "true" *) logic [89:0] afe1_rx_out_15ch;
(* keep = "true" *) logic [65:0] afe2_rx_out_11ch;
(* keep = "true" *) logic [35:0] mixed_rx_out_6ch;

(* keep = "true" *) logic [14:0] afe1_rx_dpa_locked_15ch;
(* keep = "true" *) logic [10:0] afe2_rx_dpa_locked_11ch;
(* keep = "true" *) logic [5:0]  mixed_rx_dpa_locked_6ch;

(* keep = "true" *) logic        afe1_rx_coreclock_15ch;
(* keep = "true" *) logic        afe2_rx_coreclock_11ch;
(* keep = "true" *) logic        mixed_rx_coreclock_6ch;
logic        afe1_rx_reset_15ch;
logic        afe2_rx_reset_11ch;
logic        mixed_rx_reset_6ch;

(* keep = "true" *) logic        afe1_pll_locked_15ch;
(* keep = "true" *) logic        afe2_pll_locked_11ch;
(* keep = "true" *) logic        mixed_pll_locked_6ch;

logic [179:0] afe1_align_data_15ch;
logic [131:0] afe2_align_data_11ch;
logic [71:0]  mixed_align_data_6ch;
logic [14:0]  afe1_align_valid_15ch;
logic [10:0]  afe2_align_valid_11ch;
logic [5:0]   mixed_align_valid_6ch;
(* keep = "true" *) logic [179:0] afe1_group_align_data_15ch;
(* keep = "true" *) logic [131:0] afe2_group_align_data_11ch;
(* keep = "true" *) logic [71:0]  mixed_group_align_data_6ch;
(* keep = "true" *) logic         afe1_group_align_valid;
(* keep = "true" *) logic         afe2_group_align_valid;
(* keep = "true" *) logic         mixed_group_align_valid;
logic [14:0]  afe1_lane_locked_15ch;
logic [10:0]  afe2_lane_locked_11ch;
logic [5:0]   mixed_lane_locked_6ch;
logic [14:0]  afe1_lane_timeout_15ch;
logic [10:0]  afe2_lane_timeout_11ch;
logic [5:0]   mixed_lane_timeout_6ch;
logic [14:0]  afe1_lane_phase_15ch;
logic [10:0]  afe2_lane_phase_11ch;
logic [5:0]   mixed_lane_phase_6ch;
logic         afe1_group_locked;
logic         afe2_group_locked;
logic         mixed_group_locked;
logic         afe1_group_busy;
logic         afe2_group_busy;
logic         mixed_group_busy;
logic         afe1_group_done;
logic         afe2_group_done;
logic         mixed_group_done;
logic         afe1_group_timeout;
logic         afe2_group_timeout;
logic         mixed_group_timeout;
logic [31:0]  afe1_group_debug;
logic [31:0]  afe2_group_debug;
logic [31:0]  mixed_group_debug;

(* preserve, noprune *) logic [89:0] afe1_rx_out_15ch_preserve;
(* preserve, noprune *) logic [65:0] afe2_rx_out_11ch_preserve;
(* preserve, noprune *) logic [35:0] mixed_rx_out_6ch_preserve;

(* preserve, noprune *) logic [14:0] afe1_rx_dpa_locked_15ch_preserve;
(* preserve, noprune *) logic [10:0] afe2_rx_dpa_locked_11ch_preserve;
(* preserve, noprune *) logic [5:0]  mixed_rx_dpa_locked_6ch_preserve;

(* preserve, noprune *) logic        afe1_pll_locked_15ch_preserve;
(* preserve, noprune *) logic        afe2_pll_locked_11ch_preserve;
(* preserve, noprune *) logic        mixed_pll_locked_6ch_preserve;

logic [2:0] afe1_fclk_sync_15ch;
logic [2:0] afe2_fclk_sync_11ch;
logic [2:0] mixed_fclk_sync_6ch;
logic       afe1_fclk_rise_15ch;
logic       afe2_fclk_rise_11ch;
logic       mixed_fclk_rise_6ch;
logic [15:0] lvds_status_async;
logic [15:0] lvds_status_apb;
logic [31:0] assembler_status;
logic [31:0] assembler_error_status;
logic [80:0] assembler_status_apb;
logic        assembler_sample_phase;
logic [15:0] assembler_sample_seq;
logic        afe1_locked;
logic        afe2_locked;
logic        mixed_locked;

assign lvds_out_clk     = afe1_rx_coreclock_15ch;
assign afe_sample_phase = assembler_sample_phase;
assign mixed_stream_clk = mixed_rx_coreclock_6ch;

assign lvds_apb_m2s_in[0] = i_apb_m2s;
assign o_apb_s2m          = lvds_apb_s2m_out[0];

// APB map inside the LVDS user drop:
//   0x000-0x0ff: lvds_top status/control
//   0x100-0x1ff: AFE1 15-lane LVDS IP group
//   0x200-0x2ff: AFE2 11-lane LVDS IP group
//   0x300-0x3ff: mixed 6-lane LVDS IP group
apb_switch #(
    .N_MPORT             (1),
    .N_SPORT             (LVDS_APB_N_SPORT),
    .W_OFSET             (LVDS_APB_W_OFST),
    .W_SW                (LVDS_APB_W_SW),
    .MERGE_COMPLETER_SIG (0)
) u_lvds_apb_switch (
    .i_apb_clk     (apb_clk),
    .i_apb_reset   (apb_rst),
    .i_apb_m2s     (lvds_apb_m2s_in),
    .o_apb_s2m     (lvds_apb_s2m_out),
    .i_apb_s2m     (lvds_apb_s2m),
    .o_apb_m2s     (lvds_apb_m2s),
    .i_apb_timeout (1'b0)
);

s_apb_reg #(
    .N_CTRL   (LVDS_TOP_N_CTRL),
    .N_STAT   (LVDS_TOP_N_STAT),
    .W_OFST   (LVDS_APB_W_OFST),
    .SYNC_CLK (0),
    .SAME_CLK (1),
    .RST_VAL  (LVDS_TOP_CTRL_RST_VAL)
) u_lvds_top_apb_reg (
    .i_aclk    (apb_clk),
    .i_arst    (apb_rst),
    .i_apb_m2s (lvds_apb_m2s[0]),
    .o_apb_s2m (lvds_apb_s2m[0]),
    .i_pclk    (apb_clk),
    .i_prst    (apb_rst),
    .o_ctrl    (lvds_top_ctrl),
    .i_stat    (lvds_top_stat)
);

data_sync #(
    .DATA_WIDTH (16)
) u_lvds_status_sync (
    .clk      (apb_clk),
`ifdef HSB_2P7_REL
    .rst_n    (~apb_rst),
`else
    .rst      (apb_rst),
`endif
    .sync_in  (lvds_status_async),
    .sync_out (lvds_status_apb)
);

data_sync #(
    .DATA_WIDTH (81)
) u_assembler_status_sync (
    .clk      (apb_clk),
`ifdef HSB_2P7_REL
    .rst_n    (~apb_rst),
`else
    .rst      (apb_rst),
`endif
    .sync_in  ({assembler_sample_phase, assembler_sample_seq, assembler_error_status, assembler_status}),
    .sync_out (assembler_status_apb)
);

assign afe1_rx_in_15ch[ 0] = afe1_dout_p[ 0]; // AFE1 DOUT1
assign afe1_rx_in_15ch[ 1] = afe1_dout_p[ 1]; // AFE1 DOUT2
assign afe1_rx_in_15ch[ 2] = afe1_dout_p[ 2]; // AFE1 DOUT3
assign afe1_rx_in_15ch[ 3] = afe1_dout_p[ 3]; // AFE1 DOUT4
assign afe1_rx_in_15ch[ 4] = afe1_dout_p[ 4]; // AFE1 DOUT5
assign afe1_rx_in_15ch[ 5] = afe1_dout_p[ 5]; // AFE1 DOUT6
assign afe1_rx_in_15ch[ 6] = afe1_dout_p[ 7]; // AFE1 DOUT8
assign afe1_rx_in_15ch[ 7] = afe1_dout_p[ 8]; // AFE1 DOUT9
assign afe1_rx_in_15ch[ 8] = afe1_dout_p[ 9]; // AFE1 DOUT10
assign afe1_rx_in_15ch[ 9] = afe1_dout_p[10]; // AFE1 DOUT11
assign afe1_rx_in_15ch[10] = afe1_dout_p[11]; // AFE1 DOUT12
assign afe1_rx_in_15ch[11] = afe1_dout_p[12]; // AFE1 DOUT13
assign afe1_rx_in_15ch[12] = afe1_dout_p[13]; // AFE1 DOUT14
assign afe1_rx_in_15ch[13] = afe1_dout_p[14]; // AFE1 DOUT15
assign afe1_rx_in_15ch[14] = afe1_dout_p[15]; // AFE1 DOUT16

assign afe2_rx_in_11ch[ 0] = afe2_dout_p[ 0]; // AFE2 DOUT1
assign afe2_rx_in_11ch[ 1] = afe2_dout_p[ 1]; // AFE2 DOUT2
assign afe2_rx_in_11ch[ 2] = afe2_dout_p[ 2]; // AFE2 DOUT3
assign afe2_rx_in_11ch[ 3] = afe2_dout_p[ 3]; // AFE2 DOUT4
assign afe2_rx_in_11ch[ 4] = afe2_dout_p[ 4]; // AFE2 DOUT5
assign afe2_rx_in_11ch[ 5] = afe2_dout_p[ 5]; // AFE2 DOUT6
assign afe2_rx_in_11ch[ 6] = afe2_dout_p[ 7]; // AFE2 DOUT8
assign afe2_rx_in_11ch[ 7] = afe2_dout_p[12]; // AFE2 DOUT13
assign afe2_rx_in_11ch[ 8] = afe2_dout_p[13]; // AFE2 DOUT14
assign afe2_rx_in_11ch[ 9] = afe2_dout_p[14]; // AFE2 DOUT15
assign afe2_rx_in_11ch[10] = afe2_dout_p[15]; // AFE2 DOUT16

assign mixed_rx_in_6ch[0] = afe1_dout_p[ 6]; // AFE1 DOUT7, sampled with AFE2 DCLK for this lab path
assign mixed_rx_in_6ch[1] = afe2_dout_p[ 6]; // AFE2 DOUT7
assign mixed_rx_in_6ch[2] = afe2_dout_p[ 8]; // AFE2 DOUT9
assign mixed_rx_in_6ch[3] = afe2_dout_p[ 9]; // AFE2 DOUT10
assign mixed_rx_in_6ch[4] = afe2_dout_p[10]; // AFE2 DOUT11
assign mixed_rx_in_6ch[5] = afe2_dout_p[11]; // AFE2 DOUT12

lvds_afe1_15ch_if #(
    .SERDES_W      (SERDES_W),
    .SAMPLE_W      (SAMPLE_W)
) u_afe1_15ch_if (
    .reset             (reset),
    .apb_clk           (apb_clk),
    .apb_rst           (apb_rst),
    .i_apb_m2s         (lvds_apb_m2s[1]),
    .o_apb_s2m         (lvds_apb_s2m[1]),
    .inclock           (afe1_dclk_p),
    .rx_bf_sync_req    (rx_bf_sync_req),
    .rx_in             (afe1_rx_in_15ch),
    .rx_coreclock      (afe1_rx_coreclock_15ch),
    .rx_reset          (afe1_rx_reset_15ch),
    .pll_locked        (afe1_pll_locked_15ch),
    .rx_dpa_locked     (afe1_rx_dpa_locked_15ch),
    .rx_out            (afe1_rx_out_15ch),
    .aligned_data      (afe1_align_data_15ch),
    .aligned_valid     (afe1_align_valid_15ch),
    .group_aligned_data (afe1_group_align_data_15ch),
    .group_aligned_valid (afe1_group_align_valid),
    .lane_locked       (afe1_lane_locked_15ch),
    .lane_timeout      (afe1_lane_timeout_15ch),
    .lane_phase        (afe1_lane_phase_15ch),
    .debug_status      (afe1_group_debug),
    .group_locked      (afe1_group_locked),
    .group_busy        (afe1_group_busy),
    .group_done        (afe1_group_done),
    .group_timeout     (afe1_group_timeout)
);

lvds_afe2_11ch_if #(
    .SERDES_W      (SERDES_W),
    .SAMPLE_W      (SAMPLE_W)
) u_afe2_11ch_if (
    .reset             (reset),
    .apb_clk           (apb_clk),
    .apb_rst           (apb_rst),
    .i_apb_m2s         (lvds_apb_m2s[2]),
    .o_apb_s2m         (lvds_apb_s2m[2]),
    .inclock           (afe2_dclk_p),
    .rx_bf_sync_req    (rx_bf_sync_req),
    .rx_in             (afe2_rx_in_11ch),
    .rx_coreclock      (afe2_rx_coreclock_11ch),
    .rx_reset          (afe2_rx_reset_11ch),
    .pll_locked        (afe2_pll_locked_11ch),
    .rx_dpa_locked     (afe2_rx_dpa_locked_11ch),
    .rx_out            (afe2_rx_out_11ch),
    .aligned_data      (afe2_align_data_11ch),
    .aligned_valid     (afe2_align_valid_11ch),
    .group_aligned_data (afe2_group_align_data_11ch),
    .group_aligned_valid (afe2_group_align_valid),
    .lane_locked       (afe2_lane_locked_11ch),
    .lane_timeout      (afe2_lane_timeout_11ch),
    .lane_phase        (afe2_lane_phase_11ch),
    .debug_status      (afe2_group_debug),
    .group_locked      (afe2_group_locked),
    .group_busy        (afe2_group_busy),
    .group_done        (afe2_group_done),
    .group_timeout     (afe2_group_timeout)
);

lvds_mixed_6ch_if #(
    .SERDES_W      (SERDES_W),
    .SAMPLE_W      (SAMPLE_W)
) u_mixed_6ch_if (
    .reset             (reset),
    .apb_clk           (apb_clk),
    .apb_rst           (apb_rst),
    .i_apb_m2s         (lvds_apb_m2s[3]),
    .o_apb_s2m         (lvds_apb_s2m[3]),
    .inclock           (afe2_dclk_p),
    .rx_bf_sync_req    (rx_bf_sync_req),
    .rx_in             (mixed_rx_in_6ch),
    .rx_coreclock      (mixed_rx_coreclock_6ch),
    .rx_reset          (mixed_rx_reset_6ch),
    .pll_locked        (mixed_pll_locked_6ch),
    .rx_dpa_locked     (mixed_rx_dpa_locked_6ch),
    .rx_out            (mixed_rx_out_6ch),
    .aligned_data      (mixed_align_data_6ch),
    .aligned_valid     (mixed_align_valid_6ch),
    .group_aligned_data (mixed_group_align_data_6ch),
    .group_aligned_valid (mixed_group_align_valid),
    .lane_locked       (mixed_lane_locked_6ch),
    .lane_timeout      (mixed_lane_timeout_6ch),
    .lane_phase        (mixed_lane_phase_6ch),
    .debug_status      (mixed_group_debug),
    .group_locked      (mixed_group_locked),
    .group_busy        (mixed_group_busy),
    .group_done        (mixed_group_done),
    .group_timeout     (mixed_group_timeout)
);

assign afe1_locked  = afe1_pll_locked_15ch & (&afe1_rx_dpa_locked_15ch);
assign afe2_locked  = afe2_pll_locked_11ch & (&afe2_rx_dpa_locked_11ch);
assign mixed_locked = mixed_pll_locked_6ch & (&mixed_rx_dpa_locked_6ch);

lvds_group_assembler #(
    .AFE_LANES         (AFE_LANES),
    .SAMPLE_W          (SAMPLE_W),
    .AFE1_GROUP_LANES  (15),
    .AFE2_GROUP_LANES  (11),
    .MIXED_GROUP_LANES (6),
    .SEQ_W             (16),
    .FIFO_DEPTH        (32),
    .FIFO_MEM_STYLE    ("BLOCK")
) u_group_assembler (
    .afe1_clk          (afe1_rx_coreclock_15ch),
    .afe1_rst          (afe1_rx_reset_15ch),
    .afe1_group_data   (afe1_group_align_data_15ch),
    .afe1_group_valid  (afe1_group_align_valid),
    .afe2_clk          (afe2_rx_coreclock_11ch),
    .afe2_rst          (afe2_rx_reset_11ch),
    .afe2_group_data   (afe2_group_align_data_11ch),
    .afe2_group_valid  (afe2_group_align_valid),
    .mixed_clk         (mixed_rx_coreclock_6ch),
    .mixed_rst         (mixed_rx_reset_6ch),
    .mixed_group_data  (mixed_group_align_data_6ch),
    .mixed_group_valid (mixed_group_align_valid),
    .out_clk           (afe1_rx_coreclock_15ch),
    .out_rst           (afe1_rx_reset_15ch),
    .cfg_enable        (1'b1),
    .cfg_clear         (lvds_top_ctrl[0][1]),
    .afe1_data         (afe1_aligned_data),
    .afe1_valid        (afe1_aligned_valid),
    .afe2_data         (afe2_aligned_data),
    .afe2_valid        (afe2_aligned_valid),
    .sample_phase      (assembler_sample_phase),
    .sample_seq        (assembler_sample_seq),
    .status            (assembler_status),
    .error_status      (assembler_error_status)
);

assign afe1_fclk_rise_15ch = (afe1_fclk_sync_15ch[2:1] == 2'b01);
assign afe2_fclk_rise_11ch = (afe2_fclk_sync_11ch[2:1] == 2'b01);
assign mixed_fclk_rise_6ch = (mixed_fclk_sync_6ch[2:1] == 2'b01);

always_ff @(posedge afe1_rx_coreclock_15ch) begin
    if (afe1_rx_reset_15ch) begin
        afe1_fclk_sync_15ch              <= 3'b000;
        afe1_rx_out_15ch_preserve        <= '0;
        afe1_rx_dpa_locked_15ch_preserve <= '0;
        afe1_pll_locked_15ch_preserve    <= 1'b0;
    end else begin
        afe1_fclk_sync_15ch              <= {afe1_fclk_sync_15ch[1:0], afe1_fclk_p};
        afe1_rx_out_15ch_preserve        <= afe1_rx_out_15ch;
        afe1_rx_dpa_locked_15ch_preserve <= afe1_rx_dpa_locked_15ch;
        afe1_pll_locked_15ch_preserve    <= afe1_pll_locked_15ch;
    end
end

always_ff @(posedge afe2_rx_coreclock_11ch) begin
    if (afe2_rx_reset_11ch) begin
        afe2_fclk_sync_11ch              <= 3'b000;
        afe2_rx_out_11ch_preserve        <= '0;
        afe2_rx_dpa_locked_11ch_preserve <= '0;
        afe2_pll_locked_11ch_preserve    <= 1'b0;
    end else begin
        afe2_fclk_sync_11ch              <= {afe2_fclk_sync_11ch[1:0], afe2_fclk_p};
        afe2_rx_out_11ch_preserve        <= afe2_rx_out_11ch;
        afe2_rx_dpa_locked_11ch_preserve <= afe2_rx_dpa_locked_11ch;
        afe2_pll_locked_11ch_preserve    <= afe2_pll_locked_11ch;
    end
end

always_ff @(posedge mixed_rx_coreclock_6ch) begin
    if (mixed_rx_reset_6ch) begin
        mixed_fclk_sync_6ch              <= 3'b000;
        mixed_rx_out_6ch_preserve        <= '0;
        mixed_rx_dpa_locked_6ch_preserve <= '0;
        mixed_pll_locked_6ch_preserve    <= 1'b0;
    end else begin
        mixed_fclk_sync_6ch              <= {mixed_fclk_sync_6ch[1:0], afe2_fclk_p};
        mixed_rx_out_6ch_preserve        <= mixed_rx_out_6ch;
        mixed_rx_dpa_locked_6ch_preserve <= mixed_rx_dpa_locked_6ch;
        mixed_pll_locked_6ch_preserve    <= mixed_pll_locked_6ch;
    end
end

always_comb begin
    lvds_status_async  = {
        afe2_aligned_valid,
        afe1_aligned_valid,
        mixed_group_timeout,
        afe2_group_timeout,
        afe1_group_timeout,
        mixed_group_busy,
        afe2_group_busy,
        afe1_group_busy,
        mixed_locked,
        afe2_locked,
        afe1_locked,
        mixed_pll_locked_6ch,
        afe2_pll_locked_11ch,
        afe1_pll_locked_15ch,
        mixed_group_locked,
        ~reset
    };

    for (int stat_i = 0; stat_i < LVDS_TOP_N_STAT; stat_i++) begin
        lvds_top_stat[stat_i] = 32'h0;
    end

    lvds_top_stat[0] = 32'h4c564453; // "LVDS"
    lvds_top_stat[1] = {16'h0, lvds_status_apb};
    lvds_top_stat[2] = {AFE_LANES_STAT, SERDES_W_STAT};
    lvds_top_stat[3] = {LVDS_TOP_N_CTRL_STAT, LVDS_TOP_N_STAT_STAT};
    lvds_top_stat[4] = {28'h0, lvds_top_ctrl[0][2:0], assembler_status_apb[80]};
    lvds_top_stat[5] = assembler_status_apb[31:0];
    lvds_top_stat[6] = assembler_status_apb[63:32];
    lvds_top_stat[7] = {16'h0, assembler_status_apb[79:64]};

    debug_status        = '0;
    debug_status[15:0]  = lvds_status_apb;
    debug_status[16]    = assembler_status_apb[80];
    debug_status[17]    = |assembler_status_apb[63:32];
    debug_status[18]    = assembler_status_apb[0];
    debug_status[19]    = assembler_status_apb[2];
    debug_status[20]    = assembler_status_apb[3];
    debug_status[21]    = assembler_status_apb[4];
    debug_status[22]    = assembler_status_apb[5];
    debug_status[31:23] = assembler_status_apb[40:32];
end

endmodule
