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

module lvds_rx_group
  import apb_pkg::*;
#(
    parameter int         N_LANES  = 1,
    parameter int         SERDES_W = 6,
    parameter int         SAMPLE_W = 12,
    parameter logic [31:0] GROUP_ID = 32'h4c564753 // "LVGS"
) (
    input  logic                          reset,

    input  logic                          apb_clk,
    input  logic                          apb_rst,
    input  apb_m2s                        i_apb_m2s,
    output apb_s2m                        o_apb_s2m,

    input  logic                          rx_clk,
    input  logic                          pll_locked,
    input  logic [N_LANES-1:0]            rx_dpa_locked,
    input  logic [N_LANES*SERDES_W-1:0]   rx_data,
    input  logic                          rx_bf_sync_req,

    output logic [N_LANES*SAMPLE_W-1:0]   aligned_data,
    output logic [N_LANES-1:0]            aligned_valid,
    output logic [N_LANES*SAMPLE_W-1:0]   group_aligned_data,
    output logic                          group_aligned_valid,
    output logic [N_LANES-1:0]            lane_locked,
    output logic [N_LANES-1:0]            lane_timeout,
    output logic [N_LANES-1:0]            lane_phase,
    output logic [31:0]                   debug_status,
    output logic                          group_locked,
    output logic                          group_busy,
    output logic                          group_done,
    output logic                          group_timeout
);

localparam int LVDS_GROUP_N_CTRL = 8;
localparam int LVDS_GROUP_N_STAT = 8;
localparam int LVDS_GROUP_W_OFST = 8;
localparam int LANE_SEL_W        = (N_LANES <= 1) ? 1 : $clog2(N_LANES);
localparam logic [7:0] N_LANES_STAT  = N_LANES;
localparam logic [7:0] SERDES_W_STAT = SERDES_W;

localparam logic [31:0] LANE_MASK_RST = {{(32-N_LANES){1'b0}}, {N_LANES{1'b1}}};
localparam logic [LVDS_GROUP_N_CTRL*32-1:0] GROUP_CTRL_RST_VAL = {
    32'h00000000, // ctrl[7] reserved
    32'h00000000, // ctrl[6] reserved
    32'h00000000, // ctrl[5] reserved
    32'h00000000, // ctrl[4] debug lane select
    32'h00000000, // ctrl[3] pattern B
    32'h00000000, // ctrl[2] pattern A
    LANE_MASK_RST, // ctrl[1] lane mask
    32'h00000000  // ctrl[0]: bit0 enable, bit1 start, bit2 clear
};

logic [31:0]                      group_ctrl [LVDS_GROUP_N_CTRL];
logic [31:0]                      group_stat [LVDS_GROUP_N_STAT];
logic [N_LANES-1:0]               lane_busy;
logic [N_LANES-1:0]               lane_done;
logic [N_LANES*32-1:0]            lane_debug_status;
logic [N_LANES-1:0]               lane_mask;
logic [LANE_SEL_W-1:0]            debug_lane_sel;
logic [4:0]                       debug_lane_sel_stat;
logic                             debug_lane_sel_valid;
logic [31:0]                      selected_lane_debug_status;
logic [N_LANES*SAMPLE_W-1:0]      group_aligned_data_q;
logic                             group_aligned_valid_q;
logic                             group_sample_ready_next;
logic [N_LANES*SAMPLE_W-1:0]      lane_hold_data_q;
logic [N_LANES*SAMPLE_W-1:0]      lane_hold_data_next;
logic [N_LANES-1:0]               lane_hold_valid_q;
logic [N_LANES-1:0]               lane_hold_valid_next;
logic [SAMPLE_W-1:0]              cfg_pattern_a;
logic [SAMPLE_W-1:0]              cfg_pattern_b;
logic [1:0]                       rx_bf_sync_req_sync_q;
logic                             rx_bf_sync_req_sync_d;
logic                             rx_bf_sync_req_pulse;

assign lane_mask           = group_ctrl[1][N_LANES-1:0];
assign cfg_pattern_a       = group_ctrl[2][SAMPLE_W-1:0];
assign cfg_pattern_b       = group_ctrl[3][SAMPLE_W-1:0];
assign debug_lane_sel       = group_ctrl[4][LANE_SEL_W-1:0];
assign debug_lane_sel_stat  = 5'(debug_lane_sel);
assign debug_lane_sel_valid = (group_ctrl[4] < 32'(N_LANES));
assign group_aligned_data   = group_aligned_data_q;
assign group_aligned_valid  = group_aligned_valid_q;
assign rx_bf_sync_req_pulse = rx_bf_sync_req_sync_q[1] & ~rx_bf_sync_req_sync_d;

always_comb begin
    selected_lane_debug_status = 32'h0;
    if (debug_lane_sel_valid) begin
        selected_lane_debug_status = lane_debug_status[debug_lane_sel*32 +: 32];
    end
end

s_apb_reg #(
    .N_CTRL   (LVDS_GROUP_N_CTRL),
    .N_STAT   (LVDS_GROUP_N_STAT),
    .W_OFST   (LVDS_GROUP_W_OFST),
    .SYNC_CLK (0),
    .SAME_CLK (0),
    .RST_VAL  (GROUP_CTRL_RST_VAL)
) u_apb_reg (
    .i_aclk    (apb_clk),
    .i_arst    (apb_rst),
    .i_apb_m2s (i_apb_m2s),
    .o_apb_s2m (o_apb_s2m),
    .i_pclk    (rx_clk),
    .i_prst    (reset),
    .o_ctrl    (group_ctrl),
    .i_stat    (group_stat)
);

always_ff @(posedge rx_clk) begin
    if (reset) begin
        rx_bf_sync_req_sync_q <= '0;
        rx_bf_sync_req_sync_d <= 1'b0;
    end else begin
        rx_bf_sync_req_sync_q <= {rx_bf_sync_req_sync_q[0], rx_bf_sync_req};
        rx_bf_sync_req_sync_d <= rx_bf_sync_req_sync_q[1];
    end
end

genvar lane;
generate
    for (lane = 0; lane < N_LANES; lane++) begin : gen_lane_aligner
        lvds_lane_aligner #(
            .SERDES_W (SERDES_W),
            .SAMPLE_W (SAMPLE_W)
        ) u_lane_aligner (
            .clk           (rx_clk),
            .reset         (reset),
            .cfg_enable    (group_ctrl[0][0] & lane_mask[lane]),
            .cfg_start     (group_ctrl[0][1]),
            .cfg_clear     (group_ctrl[0][2]),
            .cfg_pattern_a (cfg_pattern_a),
            .cfg_pattern_b (cfg_pattern_b),
            .rx_dpa_locked  (rx_dpa_locked[lane]),
            .rx_data        (rx_data[lane*SERDES_W +: SERDES_W]),
            .rx_bf_sync_req (rx_bf_sync_req_pulse),
            .aligned_data  (aligned_data[lane*SAMPLE_W +: SAMPLE_W]),
            .aligned_valid (aligned_valid[lane]),
            .lane_locked   (lane_locked[lane]),
            .busy          (lane_busy[lane]),
            .done          (lane_done[lane]),
            .timeout       (lane_timeout[lane]),
            .sample_phase  (lane_phase[lane]),
            .debug_status  (lane_debug_status[lane*32 +: 32])
        );
    end
endgenerate

assign group_locked  = pll_locked & (&(lane_locked | ~lane_mask));
assign group_busy    = |lane_busy;
assign group_done    = &(lane_done | ~lane_mask);
assign group_timeout = |lane_timeout;

always_comb begin
    lane_hold_data_next  = lane_hold_data_q;
    lane_hold_valid_next = lane_hold_valid_q;

    for (int lane_i = 0; lane_i < N_LANES; lane_i++) begin
        if (!lane_mask[lane_i]) begin
            lane_hold_data_next[lane_i*SAMPLE_W +: SAMPLE_W] = '0;
            lane_hold_valid_next[lane_i] = 1'b0;
        end else if (aligned_valid[lane_i]) begin
            lane_hold_data_next[lane_i*SAMPLE_W +: SAMPLE_W] = aligned_data[lane_i*SAMPLE_W +: SAMPLE_W];
            lane_hold_valid_next[lane_i] = 1'b1;
        end
    end

    group_sample_ready_next = group_ctrl[0][0] && !group_ctrl[0][2] && (|lane_mask) && (&(lane_hold_valid_next | ~lane_mask));
end

always_ff @(posedge rx_clk) begin
    if (reset) begin
        group_aligned_data_q  <= '0;
        group_aligned_valid_q <= 1'b0;
        lane_hold_data_q      <= '0;
        lane_hold_valid_q     <= '0;
    end else begin
        group_aligned_valid_q <= 1'b0;

        if (!group_ctrl[0][0] || group_ctrl[0][2]) begin
            group_aligned_data_q <= '0;
            lane_hold_data_q     <= '0;
            lane_hold_valid_q    <= '0;
        end else if (group_sample_ready_next) begin
            group_aligned_data_q  <= lane_hold_data_next;
            group_aligned_valid_q <= 1'b1;
            lane_hold_data_q      <= '0;
            lane_hold_valid_q     <= '0;
        end else begin
            lane_hold_data_q  <= lane_hold_data_next;
            lane_hold_valid_q <= lane_hold_valid_next;
        end
    end
end

always_comb begin
    for (int stat_i = 0; stat_i < LVDS_GROUP_N_STAT; stat_i++) begin
        group_stat[stat_i] = 32'h0;
    end

    group_stat[0] = GROUP_ID;
    group_stat[1] = {
        7'h0,
        group_aligned_valid_q,
        group_sample_ready_next,
        group_timeout,
        group_done,
        group_busy,
        group_locked,
        &rx_dpa_locked,
        pll_locked,
        reset,
        N_LANES_STAT,
        SERDES_W_STAT
    };
    group_stat[2] = {{(32-N_LANES){1'b0}}, lane_locked};
    group_stat[3] = {{(32-N_LANES){1'b0}}, lane_timeout};
    group_stat[4] = {{(32-N_LANES){1'b0}}, aligned_valid};
    group_stat[5] = {{(32-N_LANES){1'b0}}, lane_hold_valid_q};
    group_stat[6] = selected_lane_debug_status;
    group_stat[7] = {26'h0, debug_lane_sel_valid, debug_lane_sel_stat};
end

always_comb begin
    debug_status        = 32'h0;
    debug_status[0]     = ~reset;
    debug_status[1]     = pll_locked;
    debug_status[2]     = &rx_dpa_locked;
    debug_status[3]     = group_locked;
    debug_status[4]     = group_busy;
    debug_status[5]     = group_done;
    debug_status[6]     = group_timeout;
    debug_status[7]     = group_aligned_valid_q;
    debug_status[15:8]  = N_LANES_STAT;
    debug_status[23:16] = SERDES_W_STAT;
    debug_status[24]    = rx_bf_sync_req_sync_q[1];
    debug_status[25]    = rx_bf_sync_req_pulse;
end

endmodule
