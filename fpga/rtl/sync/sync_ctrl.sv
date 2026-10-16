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

module sync_ctrl
  import apb_pkg::*;
(
    input  logic   apb_clk,
    input  logic   apb_rst,
    input  apb_m2s i_apb_m2s,
    output apb_s2m o_apb_s2m,

    input  logic   sync_clk,
    input  logic   sync_rst,
    input  logic   sync_phase_valid_i,
    input  logic   sync_phase_i,
    input  logic   capture_busy_i,

    output logic   tx_wakeup_o,
    output logic   tx1_bf_sync_o,
    output logic   tx2_bf_sync_o,
    output logic   capture_start_o,

    output logic   rx_bf_sync_int_o,
    output logic   rx_bf_sync_ext_o,

    input  logic        tx_update_ready_i,
    output logic        tx_update_expected_sequence_valid_o,
    output logic [15:0] tx_update_expected_sequence_o,
    output logic        tx_update_event_retired_o,
    output logic        tx_update_release_o,
    output logic        tx_update_capture_error_o
);

localparam int SYNC_N_CTRL = 13;
localparam int SYNC_N_STAT = 4;
localparam int SYNC_W_OFST = 8;

localparam int CTRL_START               = 0;  // 0x00
localparam int CTRL_CFG                 = 1;  // 0x04
localparam int CTRL_WAKEUP_CYCLES       = 2;  // 0x08
localparam int CTRL_TX_SYNC_DELAY       = 3;  // 0x0c
localparam int CTRL_TX_SYNC_PULSE       = 4;  // 0x10
localparam int CTRL_RX_SYNC_DELAY       = 5;  // 0x14
localparam int CTRL_RX_SYNC_PULSE       = 6;  // 0x18
localparam int CTRL_CAPTURE_START_DELAY = 7;  // 0x1c
localparam int CTRL_EVENT_MASK          = 8;  // 0x20
localparam int CTRL_SEQUENCE_COUNT      = 9;  // 0x24
localparam int CTRL_SEQUENCE_INTERVAL   = 10; // 0x28
localparam int CTRL_TX_UPDATE_GUARD     = 11; // 0x2c
localparam int CTRL_TX_UPDATE_RELEASE   = 12; // 0x30

localparam int STAT_STATUS              = 0;  // 0x80
localparam int STAT_RX_SYNC_COUNT       = 1;  // 0x84
localparam int STAT_EVENT_COUNT         = 2;  // 0x88
localparam int STAT_CAPTURE_START_COUNT = 3;  // 0x8c

// START bits:
// [0] sequence start/run
//
// SW programs CFG, EVENT_MASK, timing, SEQUENCE_COUNT, and
// SEQUENCE_INTERVAL before asserting START[0].
localparam int START_SEQUENCE = 0;

// CFG bits:
// [0] RX sync internal output enable
// [1] RX sync external output enable
// [2] wakeup timer enable
// [3] TX1 sync timer enable
// [4] TX2 sync timer enable
// [5] RX sync timer enable
// [6] capture_start timer enable
// [7] TX update safety enable
// [8] TX update sequence/accounting enable
// [9] sync phase alignment enable
// [10] TX update automatic sequence start enable
// [11] TX update release timer enable
localparam logic [31:0] ONE_CYCLE   = 32'd1;
localparam logic [31:0] ZERO_CYCLES = 32'd0;
localparam logic [31:0] TX_UPDATE_MIN_GUARD_CYCLES = 32'd20;

// EVENT_MASK bits:
// [0] RX sync
// [1] wakeup
// [2] TX1 sync
// [3] TX2 sync
// [4] capture_start
// [5] TX update release
localparam int EVENT_MASK_WIDTH     = 6;
localparam int EVENT_RX_SYNC        = 0;
localparam int EVENT_WAKEUP         = 1;
localparam int EVENT_TX1_SYNC       = 2;
localparam int EVENT_TX2_SYNC       = 3;
localparam int EVENT_CAPTURE_START  = 4;
localparam int EVENT_TX_UPDATE_RELEASE = 5;

typedef enum logic [1:0] {
    SEQ_IDLE,
    SEQ_GAP,
    SEQ_WAIT,
    SEQ_PHASE
} sequence_state_t;

logic [31:0] ctrl_reg [SYNC_N_CTRL];
logic [31:0] stat_reg [SYNC_N_STAT];

logic        rx_sync_int_enable;
logic        rx_sync_ext_enable;
logic        wakeup_enable;
logic        tx1_sync_enable;
logic        tx2_sync_enable;
logic        rx_sync_enable;
logic        capture_start_enable;
logic        tx_update_safety_enable;
logic        tx_update_sequence_enable;
logic        sync_phase_align_enable;
logic        tx_update_auto_start_enable;
logic        tx_update_release_enable;
logic        tx_update_start_required;
logic        tx_update_sequence_required;
logic        tx_update_start_suppressed;
logic        tx_update_sequence_suppressed;
logic        sync_phase_ok;

logic        sequence_start_level;
logic        sequence_start_pulse;
logic        sequence_start_request_pulse;
logic        sequence_start_q;

logic        tx_update_ready_q;
logic        auto_start_pending_q;
logic [31:0] auto_start_guard_count_q;
logic [31:0] auto_start_interval_count_q;
logic        auto_sequence_start_pulse;

logic [EVENT_MASK_WIDTH-1:0] event_mask_cfg;
logic [EVENT_MASK_WIDTH-1:0] start_event_mask;
logic [EVENT_MASK_WIDTH-1:0] sequence_event_mask;
logic [EVENT_MASK_WIDTH-1:0] event_start_pulse_q;
logic [EVENT_MASK_WIDTH-1:0] sequence_mask_q;
logic [EVENT_MASK_WIDTH-1:0] pending_event_mask_q;
logic                        pending_capture_error_q;
logic                        pending_last_event_q;

logic        any_busy;
logic        event_timer_busy;

logic        wakeup_event;
logic        tx1_sync_event;
logic        tx2_sync_event;
logic        rx_sync_event;
logic        capture_start_event;
logic        tx_update_release_event;

logic        wakeup_busy;
logic        tx1_sync_busy;
logic        tx2_sync_busy;
logic        rx_sync_busy;
logic        capture_start_busy;
logic        tx_update_release_busy;
logic        rx_sync_done;

sequence_state_t sequence_state_q;
logic        sequence_busy_q;
logic        sequence_done_q;
logic        sequence_overrun_q;
logic        sequence_infinite_q;
logic [31:0] sequence_interval_q;
logic [31:0] sequence_interval_count_q;
logic [31:0] sequence_remaining_q;
logic [31:0] sequence_event_count_q;
logic [15:0] tx_update_expected_sequence_q;
logic        tx_update_event_retired_q;
logic        tx_update_capture_error_q;

logic [31:0] rx_sync_count_q;
logic [31:0] capture_start_count_q;
logic [31:0] status_sync;

s_apb_reg #(
    .N_CTRL   (SYNC_N_CTRL),
    .N_STAT   (SYNC_N_STAT),
    .W_OFST   (SYNC_W_OFST),
    .SYNC_CLK (0),
    .SAME_CLK (0)
) u_apb_reg (
    .i_aclk    (apb_clk),
    .i_arst    (apb_rst),
    .i_apb_m2s (i_apb_m2s),
    .o_apb_s2m (o_apb_s2m),
    .i_pclk    (sync_clk),
    .i_prst    (sync_rst),
    .o_ctrl    (ctrl_reg),
    .i_stat    (stat_reg)
);

assign rx_sync_int_enable    = ctrl_reg[CTRL_CFG][0];
assign rx_sync_ext_enable    = ctrl_reg[CTRL_CFG][1];
assign wakeup_enable         = ctrl_reg[CTRL_CFG][2];
assign tx1_sync_enable       = ctrl_reg[CTRL_CFG][3];
assign tx2_sync_enable       = ctrl_reg[CTRL_CFG][4];
assign rx_sync_enable        = ctrl_reg[CTRL_CFG][5];
assign capture_start_enable  = ctrl_reg[CTRL_CFG][6];
assign tx_update_safety_enable   = ctrl_reg[CTRL_CFG][7];
assign tx_update_sequence_enable = ctrl_reg[CTRL_CFG][8];
assign sync_phase_align_enable     = ctrl_reg[CTRL_CFG][9];
assign tx_update_auto_start_enable = ctrl_reg[CTRL_CFG][10];
assign tx_update_release_enable    = ctrl_reg[CTRL_CFG][11];

assign sequence_start_level  = ctrl_reg[CTRL_START][START_SEQUENCE];
assign sequence_start_pulse  = sequence_start_level & ~sequence_start_q;
assign sequence_start_request_pulse = sequence_start_pulse |
                                      auto_sequence_start_pulse;
assign event_mask_cfg        = ctrl_reg[CTRL_EVENT_MASK][EVENT_MASK_WIDTH-1:0];
assign tx_update_start_required    = event_mask_cfg[EVENT_TX1_SYNC] |
                                     event_mask_cfg[EVENT_TX2_SYNC];
assign tx_update_sequence_required = sequence_mask_q[EVENT_TX1_SYNC] |
                                     sequence_mask_q[EVENT_TX2_SYNC];
assign tx_update_start_suppressed    = tx_update_safety_enable &&
                                       !tx_update_ready_i &&
                                       tx_update_start_required;
assign tx_update_sequence_suppressed = tx_update_safety_enable &&
                                       !tx_update_ready_i &&
                                       tx_update_sequence_required;
assign sync_phase_ok = !sync_phase_align_enable ||
                       (sync_phase_valid_i && !sync_phase_i);

always_comb begin
    start_event_mask    = event_mask_cfg;
    sequence_event_mask = sequence_mask_q;

    if (tx_update_safety_enable && !tx_update_ready_i) begin
        start_event_mask[EVENT_TX1_SYNC]    = 1'b0;
        start_event_mask[EVENT_TX2_SYNC]    = 1'b0;
        sequence_event_mask[EVENT_TX1_SYNC] = 1'b0;
        sequence_event_mask[EVENT_TX2_SYNC] = 1'b0;
    end
end

assign event_timer_busy      = wakeup_busy | tx1_sync_busy | tx2_sync_busy | rx_sync_busy |
                               capture_start_busy | tx_update_release_busy;
assign any_busy              = event_timer_busy | sequence_busy_q;

assign tx_wakeup_o           = wakeup_event;
assign tx1_bf_sync_o         = tx1_sync_event;
assign tx2_bf_sync_o         = tx2_sync_event;
assign capture_start_o       = capture_start_event;
assign rx_bf_sync_int_o      = rx_sync_event & rx_sync_int_enable;
assign rx_bf_sync_ext_o      = rx_sync_event & rx_sync_ext_enable;
assign tx_update_expected_sequence_valid_o = tx_update_sequence_enable;
assign tx_update_expected_sequence_o       = tx_update_expected_sequence_q;
assign tx_update_event_retired_o           = tx_update_event_retired_q;
assign tx_update_release_o = tx_update_release_enable ?
                             tx_update_release_event : tx_update_event_retired_q;
assign tx_update_capture_error_o           = tx_update_capture_error_q;

assign status_sync[0]        = rx_sync_event;
assign status_sync[1]        = any_busy;
assign status_sync[2]        = tx_wakeup_o;
assign status_sync[3]        = tx1_bf_sync_o;
assign status_sync[4]        = tx2_bf_sync_o;
assign status_sync[5]        = rx_bf_sync_int_o;
assign status_sync[6]        = rx_bf_sync_ext_o;
assign status_sync[7]        = capture_start_o;
assign status_sync[15:8]     = ctrl_reg[CTRL_CFG][7:0];
assign status_sync[16]       = sequence_busy_q;
assign status_sync[17]       = sequence_done_q;
assign status_sync[18]       = sequence_overrun_q;
assign status_sync[19]       = (ctrl_reg[CTRL_SEQUENCE_COUNT] == 32'd0);
assign status_sync[24:20]    = event_mask_cfg[4:0];
assign status_sync[25]       = tx_update_auto_start_enable;
assign status_sync[26]       = auto_start_pending_q;
assign status_sync[27]       = (auto_start_guard_count_q == 32'd0);
assign status_sync[28]       = (auto_start_interval_count_q == 32'd0);
assign status_sync[29]       = auto_sequence_start_pulse;
assign status_sync[30]       = tx_update_ready_i;
assign status_sync[31]       = 1'b0;

assign stat_reg[STAT_STATUS]              = status_sync;
assign stat_reg[STAT_RX_SYNC_COUNT]       = rx_sync_count_q;
assign stat_reg[STAT_EVENT_COUNT]         = sequence_event_count_q;
assign stat_reg[STAT_CAPTURE_START_COUNT] = capture_start_count_q;

sync_event_timer u_wakeup_timer (
    .clk               (sync_clk),
    .rst               (sync_rst),
    .enable_i          (wakeup_enable),
    .start_i           (event_start_pulse_q[EVENT_WAKEUP]),
    .delay_cycles_i    (ZERO_CYCLES),
    .duration_cycles_i (ctrl_reg[CTRL_WAKEUP_CYCLES]),
    .event_o           (wakeup_event),
    .busy_o            (wakeup_busy),
    .done_o            ()
);

sync_event_timer u_tx1_sync_timer (
    .clk               (sync_clk),
    .rst               (sync_rst),
    .enable_i          (tx1_sync_enable),
    .start_i           (event_start_pulse_q[EVENT_TX1_SYNC]),
    .delay_cycles_i    (ctrl_reg[CTRL_TX_SYNC_DELAY]),
    .duration_cycles_i (ctrl_reg[CTRL_TX_SYNC_PULSE]),
    .event_o           (tx1_sync_event),
    .busy_o            (tx1_sync_busy),
    .done_o            ()
);

sync_event_timer u_tx2_sync_timer (
    .clk               (sync_clk),
    .rst               (sync_rst),
    .enable_i          (tx2_sync_enable),
    .start_i           (event_start_pulse_q[EVENT_TX2_SYNC]),
    .delay_cycles_i    (ctrl_reg[CTRL_TX_SYNC_DELAY]),
    .duration_cycles_i (ctrl_reg[CTRL_TX_SYNC_PULSE]),
    .event_o           (tx2_sync_event),
    .busy_o            (tx2_sync_busy),
    .done_o            ()
);

sync_event_timer u_rx_sync_timer (
    .clk               (sync_clk),
    .rst               (sync_rst),
    .enable_i          (rx_sync_enable),
    .start_i           (event_start_pulse_q[EVENT_RX_SYNC]),
    .delay_cycles_i    (ctrl_reg[CTRL_RX_SYNC_DELAY]),
    .duration_cycles_i (ctrl_reg[CTRL_RX_SYNC_PULSE]),
    .event_o           (rx_sync_event),
    .busy_o            (rx_sync_busy),
    .done_o            (rx_sync_done)
);

sync_event_timer u_capture_start_timer (
    .clk               (sync_clk),
    .rst               (sync_rst),
    .enable_i          (capture_start_enable),
    .start_i           (event_start_pulse_q[EVENT_CAPTURE_START]),
    .delay_cycles_i    (ctrl_reg[CTRL_CAPTURE_START_DELAY]),
    .duration_cycles_i (ONE_CYCLE),
    .event_o           (capture_start_event),
    .busy_o            (capture_start_busy),
    .done_o            ()
);

sync_event_timer u_tx_update_release_timer (
    .clk               (sync_clk),
    .rst               (sync_rst),
    .enable_i          (tx_update_release_enable),
    .start_i           (event_start_pulse_q[EVENT_TX_UPDATE_RELEASE]),
    .delay_cycles_i    (ctrl_reg[CTRL_TX_UPDATE_RELEASE]),
    .duration_cycles_i (ONE_CYCLE),
    .event_o           (tx_update_release_event),
    .busy_o            (tx_update_release_busy),
    .done_o            ()
);

// In packet-live automatic mode, a completed TX update replaces the software
// sequence-start write. Keep this timing policy separate from the sequence FSM;
// the FSM consumes only the resulting one-cycle start request.
always_ff @(posedge sync_clk) begin
    if (sync_rst) begin
        tx_update_ready_q           <= 1'b0;
        auto_start_pending_q        <= 1'b0;
        auto_start_guard_count_q    <= 32'd0;
        auto_start_interval_count_q <= 32'd0;
        auto_sequence_start_pulse   <= 1'b0;
    end else begin
        tx_update_ready_q         <= tx_update_ready_i;
        auto_sequence_start_pulse <= 1'b0;

        if (tx_update_event_retired_q) begin
            auto_start_interval_count_q <= ctrl_reg[CTRL_SEQUENCE_INTERVAL];
        end else if (auto_start_interval_count_q != 32'd0) begin
            auto_start_interval_count_q <= auto_start_interval_count_q - 1'b1;
        end

        if (!tx_update_auto_start_enable || !tx_update_sequence_enable) begin
            auto_start_pending_q     <= 1'b0;
            auto_start_guard_count_q <= 32'd0;
        end else if (sequence_start_pulse) begin
            // A manual start wins and cancels a pending automatic request.
            auto_start_pending_q     <= 1'b0;
            auto_start_guard_count_q <= 32'd0;
        end else begin
            if (tx_update_ready_i && !tx_update_ready_q) begin
                auto_start_pending_q     <= 1'b1;
                auto_start_guard_count_q <=
                    (ctrl_reg[CTRL_TX_UPDATE_GUARD] < TX_UPDATE_MIN_GUARD_CYCLES) ?
                    TX_UPDATE_MIN_GUARD_CYCLES : ctrl_reg[CTRL_TX_UPDATE_GUARD];
            end else if (auto_start_pending_q &&
                         (auto_start_guard_count_q != 32'd0)) begin
                auto_start_guard_count_q <= auto_start_guard_count_q - 1'b1;
            end

            if (auto_start_pending_q &&
                (auto_start_guard_count_q == 32'd0) &&
                (auto_start_interval_count_q == 32'd0) &&
                !any_busy &&
                !capture_busy_i &&
                (ctrl_reg[CTRL_SEQUENCE_COUNT] == 32'd1)) begin
                auto_sequence_start_pulse <= 1'b1;
                auto_start_pending_q      <= 1'b0;
            end
        end
    end
end

always_ff @(posedge sync_clk) begin
    if (sync_rst) begin
        sequence_state_q          <= SEQ_IDLE;
        sequence_start_q          <= 1'b0;
        event_start_pulse_q       <= '0;
        sequence_mask_q           <= '0;
        pending_event_mask_q      <= '0;
        pending_capture_error_q   <= 1'b0;
        pending_last_event_q      <= 1'b0;
        sequence_busy_q           <= 1'b0;
        sequence_done_q           <= 1'b0;
        sequence_overrun_q        <= 1'b0;
        sequence_infinite_q       <= 1'b0;
        sequence_interval_q       <= 32'd0;
        sequence_interval_count_q <= 32'd0;
        sequence_remaining_q      <= 32'd0;
        sequence_event_count_q    <= 32'd0;
        tx_update_expected_sequence_q <= 16'd0;
        tx_update_event_retired_q     <= 1'b0;
        tx_update_capture_error_q     <= 1'b0;
    end else begin
        sequence_start_q          <= sequence_start_level;
        event_start_pulse_q       <= '0;
        tx_update_event_retired_q <= 1'b0;

        if (sequence_start_request_pulse) begin
            sequence_state_q          <= SEQ_IDLE;
            sequence_mask_q           <= event_mask_cfg;
            pending_event_mask_q      <= '0;
            pending_capture_error_q   <= 1'b0;
            pending_last_event_q      <= 1'b0;
            sequence_busy_q           <= 1'b0;
            sequence_done_q           <= 1'b0;
            sequence_overrun_q        <= 1'b0;
            sequence_infinite_q       <= (ctrl_reg[CTRL_SEQUENCE_COUNT] == 32'd0);
            sequence_interval_q       <= ctrl_reg[CTRL_SEQUENCE_INTERVAL];
            sequence_interval_count_q <= ctrl_reg[CTRL_SEQUENCE_INTERVAL];
            sequence_remaining_q      <= (ctrl_reg[CTRL_SEQUENCE_COUNT] == 32'd0) ?
                                         32'd0 : ctrl_reg[CTRL_SEQUENCE_COUNT];
            sequence_event_count_q    <= 32'd0;

            if (event_mask_cfg == '0) begin
                sequence_done_q <= 1'b1;
                tx_update_capture_error_q <= 1'b0;
            end else if (event_timer_busy) begin
                sequence_overrun_q <= 1'b1;
                tx_update_capture_error_q <= 1'b0;
            end else if (sync_phase_align_enable) begin
                pending_event_mask_q    <= start_event_mask;
                pending_capture_error_q <= tx_update_start_suppressed;
                pending_last_event_q    <= (ctrl_reg[CTRL_SEQUENCE_COUNT] == 32'd1);
                sequence_state_q        <= SEQ_PHASE;
                sequence_busy_q         <= 1'b1;
            end else begin
                event_start_pulse_q    <= start_event_mask;
                sequence_event_count_q <= 32'd1;
                tx_update_capture_error_q <= tx_update_start_suppressed;

                if (tx_update_sequence_enable) begin
                    tx_update_event_retired_q <= 1'b1;
                    tx_update_expected_sequence_q <= tx_update_expected_sequence_q + 16'd1;
                end

                if (ctrl_reg[CTRL_SEQUENCE_COUNT] == 32'd1) begin
                    sequence_done_q <= 1'b1;
                end else begin
                    sequence_state_q     <= SEQ_GAP;
                    sequence_busy_q      <= 1'b1;
                    sequence_remaining_q <= (ctrl_reg[CTRL_SEQUENCE_COUNT] == 32'd0) ?
                                            32'd0 : (ctrl_reg[CTRL_SEQUENCE_COUNT] - 32'd1);
                end
            end
        end else begin
            case (sequence_state_q)
                SEQ_IDLE: begin
                    sequence_busy_q <= 1'b0;
                end

                SEQ_GAP: begin
                    if (sequence_infinite_q && !sequence_start_level) begin
                        sequence_state_q <= SEQ_IDLE;
                        sequence_busy_q  <= 1'b0;
                    end else begin
                        sequence_state_q          <= SEQ_WAIT;
                        sequence_interval_count_q <= sequence_interval_q;
                    end
                end

                SEQ_WAIT: begin
                    if (sequence_infinite_q && !sequence_start_level) begin
                        sequence_state_q <= SEQ_IDLE;
                        sequence_busy_q  <= 1'b0;
                    end else if (sequence_interval_count_q == 32'd0) begin
                        if (event_timer_busy) begin
                            sequence_state_q    <= SEQ_IDLE;
                            sequence_busy_q     <= 1'b0;
                            sequence_overrun_q  <= 1'b1;
                        end else if (sync_phase_align_enable) begin
                            pending_event_mask_q    <= sequence_event_mask;
                            pending_capture_error_q <= tx_update_sequence_suppressed;
                            pending_last_event_q    <= !sequence_infinite_q && (sequence_remaining_q <= 32'd1);
                            sequence_state_q        <= SEQ_PHASE;
                            sequence_busy_q         <= 1'b1;
                        end else begin
                            event_start_pulse_q    <= sequence_event_mask;
                            sequence_event_count_q <= sequence_event_count_q + 32'd1;
                            tx_update_capture_error_q <= tx_update_sequence_suppressed;

                            if (tx_update_sequence_enable) begin
                                tx_update_event_retired_q <= 1'b1;
                                tx_update_expected_sequence_q <= tx_update_expected_sequence_q + 16'd1;
                            end

                            if (!sequence_infinite_q && (sequence_remaining_q <= 32'd1)) begin
                                sequence_state_q <= SEQ_IDLE;
                                sequence_busy_q  <= 1'b0;
                                sequence_done_q  <= 1'b1;
                            end else begin
                                sequence_state_q          <= SEQ_GAP;
                                sequence_busy_q           <= 1'b1;
                                sequence_interval_count_q <= sequence_interval_q;
                                if (!sequence_infinite_q) begin
                                    sequence_remaining_q <= sequence_remaining_q - 32'd1;
                                end
                            end
                        end
                    end else begin
                        sequence_interval_count_q <= sequence_interval_count_q - 32'd1;
                    end
                end

                SEQ_PHASE: begin
                    if (sequence_infinite_q && !sequence_start_level) begin
                        sequence_state_q <= SEQ_IDLE;
                        sequence_busy_q  <= 1'b0;
                    end else if (sync_phase_ok) begin
                        if (event_timer_busy) begin
                            sequence_state_q   <= SEQ_IDLE;
                            sequence_busy_q    <= 1'b0;
                            sequence_overrun_q <= 1'b1;
                        end else begin
                            event_start_pulse_q <= pending_event_mask_q;
                            pending_event_mask_q <= '0;
                            tx_update_capture_error_q <= pending_capture_error_q;
                            sequence_event_count_q <= sequence_event_count_q + 32'd1;

                            if (tx_update_sequence_enable) begin
                                tx_update_event_retired_q <= 1'b1;
                                tx_update_expected_sequence_q <= tx_update_expected_sequence_q + 16'd1;
                            end

                            if (!sequence_infinite_q && (sequence_remaining_q != 32'd0)) begin
                                sequence_remaining_q <= sequence_remaining_q - 32'd1;
                            end

                            if (pending_last_event_q) begin
                                sequence_state_q <= SEQ_IDLE;
                                sequence_busy_q  <= 1'b0;
                                sequence_done_q  <= 1'b1;
                            end else begin
                                sequence_state_q <= SEQ_GAP;
                                sequence_busy_q  <= 1'b1;
                            end
                        end
                    end
                end

                default: begin
                    sequence_state_q <= SEQ_IDLE;
                    sequence_busy_q  <= 1'b0;
                end
            endcase
        end
    end
end

always_ff @(posedge sync_clk) begin
    if (sync_rst) begin
        rx_sync_count_q       <= 32'd0;
        capture_start_count_q <= 32'd0;
    end else begin
        if (rx_sync_done) begin
            rx_sync_count_q <= rx_sync_count_q + 1'b1;
        end

        if (capture_start_o) begin
            capture_start_count_q <= capture_start_count_q + 1'b1;
        end
    end
end

endmodule
