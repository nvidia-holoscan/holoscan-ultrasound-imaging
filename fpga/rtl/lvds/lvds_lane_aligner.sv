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

module lvds_lane_aligner #(
    parameter int SERDES_W       = 6,
    parameter int SAMPLE_W       = 12,
    parameter int HIST_W         = 4*SERDES_W,
    parameter int N_WINDOWS      = HIST_W-SAMPLE_W+1,
    parameter int WINDOW_INDEX_W = (N_WINDOWS <= 1) ? 1 : $clog2(N_WINDOWS),
    parameter logic [SAMPLE_W-1:0] SYNC_WORD = 12'h277,
    parameter int MIN_ZERO_WORDS = 4
) (
    input  logic                            clk,
    input  logic                            reset,

    input  logic                            cfg_enable,
    input  logic                            cfg_start,
    input  logic                            cfg_clear,
    input  logic [SAMPLE_W-1:0]             cfg_pattern_a,
    input  logic [SAMPLE_W-1:0]             cfg_pattern_b,

    input  logic                            rx_dpa_locked,
    input  logic [SERDES_W-1:0]             rx_data,
    input  logic                            rx_bf_sync_req,

    output logic [SAMPLE_W-1:0]             aligned_data,
    output logic                            aligned_valid,
    output logic                            lane_locked,
    output logic                            busy,
    output logic                            done,
    output logic                            timeout,
    output logic                            sample_phase,
    output logic [31:0]                     debug_status
);

localparam int MIN_ZERO_WORDS_SAFE = (MIN_ZERO_WORDS < 1) ? 1 : MIN_ZERO_WORDS;
localparam int ZERO_COUNT_W = (MIN_ZERO_WORDS_SAFE <= 1) ? 1 : $clog2(MIN_ZERO_WORDS_SAFE + 1);

typedef enum logic [3:0] {
    ST_IDLE,
    ST_SCAN,
    ST_WAIT_SYNC_REQ,
    ST_SYNC1,
    ST_SYNC2,
    ST_ZERO_RUN,
    ST_SYNC3,
    ST_SYNC4,
    ST_ALIGN_PATTERN,
    ST_ALIGNED
} align_state_e;

align_state_e                  state_q;
logic [HIST_W-1:0]             hist_q;
logic [HIST_W-1:0]             hist_next;
logic [N_WINDOWS-1:0]          window_match_c;
logic [N_WINDOWS-1:0]          window_match_q;
logic                          first_match_valid;
logic [WINDOW_INDEX_W-1:0]     first_match_index;
logic [WINDOW_INDEX_W-1:0]     match_index_q;
logic [SAMPLE_W-1:0]           locked_window;
logic                          sync_word_match;
logic                          zero_word_match;
logic                          pattern_a_word_match;
logic                          pattern_b_word_match;
logic [ZERO_COUNT_W-1:0]       zero_count_q;
logic                          sample_phase_q;
logic [SAMPLE_W-1:0]           aligned_data_q;
logic [SAMPLE_W-1:0]           pattern_a_q;
logic [SAMPLE_W-1:0]           pattern_b_q;
logic                          aligned_valid_q;
logic                          window_locked_q;
logic                          lane_locked_q;
logic                          rx_bf_sync_req_pending_q;
logic                          align_enable;

assign align_enable    = cfg_enable & rx_dpa_locked;
assign hist_next       = {hist_q[HIST_W-SERDES_W-1:0], rx_data};
assign aligned_data    = aligned_data_q;
assign aligned_valid   = aligned_valid_q;
assign lane_locked     = lane_locked_q;
assign busy            = align_enable & cfg_start & !lane_locked_q;
assign done            = lane_locked_q;
assign timeout         = 1'b0;
assign sample_phase    = aligned_valid_q;
assign sync_word_match = (locked_window == SYNC_WORD);
assign zero_word_match = (locked_window == '0);
assign pattern_a_word_match = (locked_window == pattern_a_q);
assign pattern_b_word_match = (locked_window == pattern_b_q);

function automatic logic [SAMPLE_W-1:0] select_window(
    input logic [HIST_W-1:0]             hist,
    input logic [WINDOW_INDEX_W-1:0]     index
);
    select_window = '0;
    for (int bit_i = 0; bit_i < SAMPLE_W; bit_i++) begin
        select_window[bit_i] = hist[index + bit_i];
    end
endfunction

// Keep rx_data on the shift-register input path; all window logic uses registered history.
assign locked_window = select_window(hist_q, match_index_q);

genvar win;
generate
    for (win = 0; win < N_WINDOWS; win++) begin : gen_window_match
        assign window_match_c[win] =
            (hist_q[win +: SAMPLE_W] == pattern_a_q) ||
            (hist_q[win +: SAMPLE_W] == pattern_b_q);
    end
endgenerate

always_comb begin
    first_match_valid = 1'b0;
    first_match_index = '0;

    for (int win_i = 0; win_i < N_WINDOWS; win_i++) begin
        if (!first_match_valid && window_match_q[win_i]) begin
            first_match_valid = 1'b1;
            first_match_index = WINDOW_INDEX_W'(win_i);
        end
    end
end

always_ff @(posedge clk) begin
    if (reset) begin
        state_q         <= ST_IDLE;
        hist_q          <= '0;
        window_match_q  <= '0;
        match_index_q   <= '0;
        zero_count_q    <= '0;
        sample_phase_q  <= 1'b0;
        aligned_data_q  <= '0;
        pattern_a_q     <= '0;
        pattern_b_q     <= '0;
        aligned_valid_q          <= 1'b0;
        window_locked_q          <= 1'b0;
        lane_locked_q            <= 1'b0;
        rx_bf_sync_req_pending_q <= 1'b0;
    end else begin
        hist_q          <= hist_next;
        window_match_q  <= window_match_c;
        aligned_valid_q <= 1'b0;

        if (!align_enable || cfg_clear) begin
            state_q         <= ST_IDLE;
            hist_q          <= '0;
            window_match_q  <= '0;
            match_index_q   <= '0;
            zero_count_q    <= '0;
            sample_phase_q  <= 1'b0;
            aligned_data_q  <= '0;
            pattern_a_q     <= '0;
            pattern_b_q     <= '0;
            window_locked_q          <= 1'b0;
            lane_locked_q            <= 1'b0;
            rx_bf_sync_req_pending_q <= 1'b0;
        end else if (!cfg_start) begin
            state_q         <= ST_IDLE;
            window_match_q  <= '0;
            zero_count_q    <= '0;
            sample_phase_q           <= 1'b0;
            pattern_a_q              <= cfg_pattern_a;
            pattern_b_q              <= cfg_pattern_b;
            window_locked_q          <= 1'b0;
            lane_locked_q            <= 1'b0;
            rx_bf_sync_req_pending_q <= 1'b0;
        end else begin
            if (rx_bf_sync_req && window_locked_q && !lane_locked_q) begin
                rx_bf_sync_req_pending_q <= 1'b1;
            end

            if (window_locked_q) begin
                aligned_data_q <= locked_window;
            end

            case (state_q)
                ST_IDLE: begin
                    pattern_a_q     <= cfg_pattern_a;
                    pattern_b_q     <= cfg_pattern_b;
                    window_match_q  <= '0;
                    window_locked_q <= 1'b0;
                    lane_locked_q   <= 1'b0;
                    state_q         <= ST_SCAN;
                end

                ST_SCAN: begin
                    if (first_match_valid) begin
                        match_index_q   <= first_match_index;
                        aligned_data_q  <= select_window(hist_q, first_match_index);
                        window_locked_q <= 1'b1;
                        state_q         <= ST_WAIT_SYNC_REQ;
                    end
                end

                ST_WAIT_SYNC_REQ: begin
                    if (rx_bf_sync_req_pending_q) begin
                        rx_bf_sync_req_pending_q <= 1'b0;
                        zero_count_q             <= '0;
                        sample_phase_q           <= 1'b0;
                        state_q                  <= ST_SYNC1;
                    end
                end

                ST_SYNC1: begin
                    if (sync_word_match) begin
                        state_q <= ST_SYNC2;
                    end
                end

                ST_SYNC2: begin
                    if (sync_word_match) begin
                        zero_count_q <= '0;
                        state_q      <= ST_ZERO_RUN;
                    end
                end

                ST_ZERO_RUN: begin
                    if (zero_word_match) begin
                        if (zero_count_q >= ZERO_COUNT_W'(MIN_ZERO_WORDS_SAFE-1)) begin
                            state_q <= ST_SYNC3;
                        end else begin
                            zero_count_q <= zero_count_q + ZERO_COUNT_W'(1);
                        end
                    end else begin
                        zero_count_q <= '0;
                    end
                end

                ST_SYNC3: begin
                    if (sync_word_match) begin
                        state_q <= ST_SYNC4;
                    end
                end

                ST_SYNC4: begin
                    if (sync_word_match) begin
                        sample_phase_q <= 1'b0;
                        state_q        <= ST_ALIGN_PATTERN;
                    end
                end

                ST_ALIGN_PATTERN: begin
                    sample_phase_q <= ~sample_phase_q;
                    if (sample_phase_q && pattern_a_word_match) begin
                        aligned_valid_q <= 1'b1;
                        lane_locked_q   <= 1'b1;
                        state_q         <= ST_ALIGNED;
                    end
                end

                ST_ALIGNED: begin
                    aligned_valid_q <= sample_phase_q;
                    sample_phase_q  <= ~sample_phase_q;
                    if (sample_phase_q) begin
                        lane_locked_q <= 1'b1;
                    end
                end

                default: begin
                    state_q         <= ST_IDLE;
                    zero_count_q    <= '0;
                    sample_phase_q  <= 1'b0;
                    window_locked_q <= 1'b0;
                    lane_locked_q            <= 1'b0;
                    rx_bf_sync_req_pending_q <= 1'b0;
                end
            endcase
        end
    end
end

always_comb begin
    debug_status        = 32'h0;
    debug_status[0]     = cfg_enable;
    debug_status[1]     = cfg_start;
    debug_status[2]     = cfg_clear;
    debug_status[3]     = rx_dpa_locked;
    debug_status[4]     = first_match_valid;
    debug_status[5]     = window_locked_q;
    debug_status[6]     = aligned_valid_q;
    debug_status[7]     = lane_locked_q;
    debug_status[11:8]  = state_q;
    debug_status[12]    = rx_bf_sync_req;
    debug_status[13]    = rx_bf_sync_req_pending_q;
    debug_status[14]    = pattern_a_word_match;
    debug_status[15]    = pattern_b_word_match;
    debug_status[23:16] = {{(8-WINDOW_INDEX_W){1'b0}}, match_index_q};
    debug_status[31:24] = window_match_q[7:0];
end

endmodule
