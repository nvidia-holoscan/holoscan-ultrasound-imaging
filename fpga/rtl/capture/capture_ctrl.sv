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

module capture_ctrl
  import apb_pkg::*;
#(
    parameter int IN_DWIDTH          = 384,
    parameter int OUT_DWIDTH         = 512,
    parameter int BUF_DEPTH          = 1024,
    parameter int W_USER             = 1,
    parameter int ALMOST_FULL_DEPTH  = BUF_DEPTH * 7 / 8,
    parameter int ALMOST_EMPTY_DEPTH = BUF_DEPTH / 8,
    parameter int OUT_W_USER         = W_USER
) (
    input  logic                         apb_clk,
    input  logic                         apb_rst,
    input  apb_m2s                       i_apb_m2s,
    output apb_s2m                       o_apb_s2m,

    input  logic                         stream_clk,
    input  logic                         stream_rst,

    input  logic                         capture_start_i,
    input  logic                         capture_error_i,

    output logic                         capture_busy_o,

    input  logic                         i_axis_rx_tvalid,
    input  logic [IN_DWIDTH-1:0]         i_axis_rx_tdata,
    input  logic                         i_axis_rx_phase,

    output logic                         o_axis_tx_tvalid,
    output logic [OUT_DWIDTH-1:0]        o_axis_tx_tdata,
    output logic                         o_axis_tx_tlast,
    output logic [OUT_W_USER-1:0]        o_axis_tx_tuser,
    output logic [(OUT_DWIDTH/8)-1:0]    o_axis_tx_tkeep,
    input  logic                         i_axis_tx_tready
);

localparam int CAPTURE_N_CTRL = 4;
localparam int CAPTURE_N_STAT = 4;
localparam int CAPTURE_W_OFST = 8;

localparam int CTRL_CMD           = 0; // 0x00, command bits sampled on rising edge
localparam int CTRL_CFG           = 1; // 0x04
localparam int CTRL_DELAY_RECORDS = 2; // 0x08
localparam int CTRL_LENGTH_RECORDS = 3; // 0x0c

localparam int STAT_STATUS          = 0; // 0x80
localparam int STAT_ACCEPTED        = 1; // 0x84
localparam int STAT_CAPTURE_COUNT   = 2; // 0x88
localparam int STAT_LAST_CAPTURE    = 3; // 0x8c

typedef enum logic [1:0] {
    ST_IDLE       = 2'd0,
    ST_DELAY      = 2'd1,
    ST_WAIT_PHASE = 2'd2,
    ST_CAPTURE    = 2'd3
} capture_state_e;

logic [31:0] ctrl_reg [CAPTURE_N_CTRL];
logic [31:0] stat_reg [CAPTURE_N_STAT];

logic [1:0]  cfg_stream;
logic [31:0] delay_records_stream;
logic [31:0] length_records_stream;

logic [2:0]  cmd_q;
logic        start_cmd_event_q;
logic        abort_cmd_event_q;
logic        clear_cmd_event_q;
logic        start_event;
logic        abort_event;
logic        clear_event;
logic        capture_error_stream;
logic        capture_error_seen_q;
logic [31:0] requested_length_records;

capture_state_e state_q;
logic [31:0]     delay_count_q;
logic [31:0]     capture_length_q;
logic            capture_tlast_enable_q;
logic [31:0]     accepted_records_q;
logic [31:0]     last_capture_records_q;
logic [31:0]     capture_count_q;
logic            done_sticky_q;
logic            aborted_sticky_q;
logic            afull_seen_sticky_q;
logic            full_seen_sticky_q;
logic            overflow_sticky_q;

logic            phase_start_now;
logic            buffer_in_valid;
logic            buffer_in_tlast;
logic            buffer_in_ready;
logic            fifo_aempty;
logic            fifo_afull;
logic            fifo_empty;
logic            fifo_full;
logic            final_record_count;

logic [31:0] status_stream;

s_apb_reg #(
    .N_CTRL   (CAPTURE_N_CTRL),
    .N_STAT   (CAPTURE_N_STAT),
    .W_OFST   (CAPTURE_W_OFST),
    .SYNC_CLK (0),
    .SAME_CLK (0)
) u_apb_reg (
    .i_aclk    (apb_clk),
    .i_arst    (apb_rst),
    .i_apb_m2s (i_apb_m2s),
    .o_apb_s2m (o_apb_s2m),
    .i_pclk    (stream_clk),
    .i_prst    (stream_rst),
    .o_ctrl    (ctrl_reg),
    .i_stat    (stat_reg)
);

data_sync #(
    .DATA_WIDTH  (1),
    .RESET_VALUE (1'b0),
    .SYNC_DEPTH  (2)
) u_capture_error_sync (
    .clk      (stream_clk),
`ifdef HSB_2P7_REL
    .rst_n    (~stream_rst),
`else
    .rst      (stream_rst),
`endif
    .sync_in  (capture_error_i),
    .sync_out (capture_error_stream)
);

assign cfg_stream            = ctrl_reg[CTRL_CFG][1:0];
assign delay_records_stream  = ctrl_reg[CTRL_DELAY_RECORDS];
assign length_records_stream = ctrl_reg[CTRL_LENGTH_RECORDS];
assign requested_length_records = capture_error_stream ? 32'd1 : length_records_stream;
assign capture_busy_o = (state_q != ST_IDLE);

assign stat_reg[STAT_STATUS]          = status_stream;
assign stat_reg[STAT_ACCEPTED]        = accepted_records_q;
assign stat_reg[STAT_CAPTURE_COUNT]   = capture_count_q;
assign stat_reg[STAT_LAST_CAPTURE]    = last_capture_records_q;

assign start_event = start_cmd_event_q | capture_start_i;
assign abort_event = abort_cmd_event_q;
assign clear_event = clear_cmd_event_q;

assign final_record_count = (accepted_records_q + 32'd1 >= capture_length_q);
assign phase_start_now   = (state_q == ST_WAIT_PHASE) && i_axis_rx_tvalid && (i_axis_rx_phase == 1'b0);

assign buffer_in_valid = ((state_q == ST_CAPTURE) && i_axis_rx_tvalid) || phase_start_now;
assign buffer_in_tlast = buffer_in_valid && capture_tlast_enable_q && final_record_count;

always_comb begin
    status_stream = 32'd0;
    status_stream[1:0]  = state_q;
    status_stream[2]    = cfg_stream[0];
    status_stream[3]    = cfg_stream[1];
    status_stream[4]    = done_sticky_q;
    status_stream[5]    = aborted_sticky_q;
    status_stream[6]    = afull_seen_sticky_q;
    status_stream[7]    = full_seen_sticky_q;
    status_stream[8]    = overflow_sticky_q;
    status_stream[9]    = buffer_in_ready;
    status_stream[10]   = fifo_aempty;
    status_stream[11]   = fifo_afull;
    status_stream[12]   = fifo_empty;
    status_stream[13]   = fifo_full;
    status_stream[14]   = buffer_in_valid;
    status_stream[15]   = buffer_in_tlast;
    status_stream[16]   = (state_q == ST_DELAY);
    status_stream[17]   = (state_q == ST_CAPTURE);
    status_stream[18]   = (state_q == ST_WAIT_PHASE);
    status_stream[19]   = i_axis_rx_phase;
    status_stream[20]   = phase_start_now;
    status_stream[21]   = capture_error_stream;
    status_stream[22]   = capture_error_seen_q;
end

always_ff @(posedge stream_clk) begin
    if (stream_rst) begin
        cmd_q                   <= 3'd0;
        start_cmd_event_q       <= 1'b0;
        abort_cmd_event_q       <= 1'b0;
        clear_cmd_event_q       <= 1'b0;
        capture_error_seen_q    <= 1'b0;
        state_q                 <= ST_IDLE;
        delay_count_q           <= 32'd0;
        capture_length_q        <= 32'd0;
        capture_tlast_enable_q  <= 1'b0;
        accepted_records_q      <= 32'd0;
        last_capture_records_q  <= 32'd0;
        capture_count_q         <= 32'd0;
        done_sticky_q           <= 1'b0;
        aborted_sticky_q        <= 1'b0;
        afull_seen_sticky_q     <= 1'b0;
        full_seen_sticky_q      <= 1'b0;
        overflow_sticky_q       <= 1'b0;
    end else begin
        cmd_q             <= ctrl_reg[CTRL_CMD][2:0];
        start_cmd_event_q <= ctrl_reg[CTRL_CMD][0] & ~cmd_q[0];
        abort_cmd_event_q <= ctrl_reg[CTRL_CMD][1] & ~cmd_q[1];
        clear_cmd_event_q <= ctrl_reg[CTRL_CMD][2] & ~cmd_q[2];

        if (clear_event) begin
            state_q                <= ST_IDLE;
            delay_count_q          <= 32'd0;
            capture_length_q       <= 32'd0;
            capture_tlast_enable_q <= 1'b0;
            accepted_records_q     <= 32'd0;
            last_capture_records_q <= 32'd0;
            capture_count_q        <= 32'd0;
            done_sticky_q          <= 1'b0;
            aborted_sticky_q       <= 1'b0;
            capture_error_seen_q   <= 1'b0;
            afull_seen_sticky_q    <= 1'b0;
            full_seen_sticky_q     <= 1'b0;
            overflow_sticky_q      <= 1'b0;
        end else if (abort_event) begin
            state_q          <= ST_IDLE;
            delay_count_q    <= 32'd0;
            aborted_sticky_q <= 1'b1;
        end else if (start_event && cfg_stream[0]) begin
            accepted_records_q     <= 32'd0;
            capture_length_q       <= requested_length_records;
            capture_tlast_enable_q <= cfg_stream[1] | capture_error_stream;
            capture_error_seen_q   <= capture_error_stream;
            delay_count_q          <= delay_records_stream;
            done_sticky_q          <= 1'b0;
            aborted_sticky_q       <= 1'b0;
            afull_seen_sticky_q    <= 1'b0;
            full_seen_sticky_q     <= 1'b0;
            overflow_sticky_q      <= 1'b0;

            if (requested_length_records == 32'd0) begin
                state_q                <= ST_IDLE;
                last_capture_records_q <= 32'd0;
                done_sticky_q          <= 1'b1;
                capture_count_q        <= capture_count_q + 1'b1;
            end else if (delay_records_stream == 32'd0) begin
                state_q         <= ST_WAIT_PHASE;
                capture_count_q <= capture_count_q + 1'b1;
            end else begin
                state_q         <= ST_DELAY;
                capture_count_q <= capture_count_q + 1'b1;
            end
        end else begin
            if (fifo_afull) begin
                afull_seen_sticky_q <= 1'b1;
            end
            if (fifo_full) begin
                full_seen_sticky_q <= 1'b1;
            end
            if (buffer_in_valid && fifo_full) begin
                overflow_sticky_q <= 1'b1;
            end

            unique case (state_q)
                ST_IDLE: begin
                    delay_count_q <= 32'd0;
                end

                ST_DELAY: begin
                    if (i_axis_rx_tvalid) begin
                        if (delay_count_q <= 32'd1) begin
                            delay_count_q <= 32'd0;
                            state_q       <= ST_WAIT_PHASE;
                        end else begin
                            delay_count_q <= delay_count_q - 1'b1;
                        end
                    end
                end

                ST_WAIT_PHASE: begin
                    if (phase_start_now) begin
                        accepted_records_q <= accepted_records_q + 1'b1;
                        if (final_record_count) begin
                            state_q                <= ST_IDLE;
                            last_capture_records_q <= accepted_records_q + 1'b1;
                            done_sticky_q          <= 1'b1;
                        end else begin
                            state_q <= ST_CAPTURE;
                        end
                    end
                end

                ST_CAPTURE: begin
                    if (buffer_in_valid) begin
                        accepted_records_q <= accepted_records_q + 1'b1;
                        if (final_record_count) begin
                            state_q                <= ST_IDLE;
                            last_capture_records_q <= accepted_records_q + 1'b1;
                            done_sticky_q          <= 1'b1;
                        end
                    end
                end

                default: begin
                    state_q <= ST_IDLE;
                end
            endcase
        end
    end
end

axis_buffer #(
    .IN_DWIDTH          (IN_DWIDTH),
    .OUT_DWIDTH         (OUT_DWIDTH),
    .BUF_DEPTH          (BUF_DEPTH),
    .WAIT2SEND          (0),
    .DUAL_CLOCK         (0),
    .W_USER             (W_USER),
    .ALMOST_FULL_DEPTH  (ALMOST_FULL_DEPTH),
    .ALMOST_EMPTY_DEPTH (ALMOST_EMPTY_DEPTH),
    .NO_BACKPRESSURE    (1),
    .OUT_W_USER         (OUT_W_USER)
) u_axis_buffer (
    .in_clk           (stream_clk),
    .in_rst           (stream_rst),
    .out_clk          (stream_clk),
    .out_rst          (stream_rst),
    .i_axis_rx_tvalid (buffer_in_valid),
    .i_axis_rx_tdata  (i_axis_rx_tdata),
    .i_axis_rx_tlast  (buffer_in_tlast),
    .i_axis_rx_tuser  ('0),
    .i_axis_rx_tkeep  ({(IN_DWIDTH/8){1'b1}}),
    .o_axis_rx_tready (buffer_in_ready),
    .o_fifo_aempty    (fifo_aempty),
    .o_fifo_afull     (fifo_afull),
    .o_fifo_empty     (fifo_empty),
    .o_fifo_full      (fifo_full),
    .o_axis_tx_tvalid (o_axis_tx_tvalid),
    .o_axis_tx_tdata  (o_axis_tx_tdata),
    .o_axis_tx_tlast  (o_axis_tx_tlast),
    .o_axis_tx_tuser  (o_axis_tx_tuser),
    .o_axis_tx_tkeep  (o_axis_tx_tkeep),
    .i_axis_tx_tready (i_axis_tx_tready)
);

endmodule
