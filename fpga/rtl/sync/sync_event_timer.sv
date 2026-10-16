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

module sync_event_timer #(
    parameter int COUNTER_WIDTH = 32
) (
    input  logic                     clk,
    input  logic                     rst,

    input  logic                     enable_i,
    input  logic                     start_i,
    input  logic [COUNTER_WIDTH-1:0] delay_cycles_i,
    input  logic [COUNTER_WIDTH-1:0] duration_cycles_i,

    output logic                     event_o,
    output logic                     busy_o,
    output logic                     done_o
);

typedef enum logic [1:0] {
    ST_IDLE,
    ST_DELAY,
    ST_ACTIVE
} state_t;

state_t state_q;
logic start_q;
logic start_event;
logic [COUNTER_WIDTH-1:0] delay_count_q;
logic [COUNTER_WIDTH-1:0] duration_count_q;

localparam logic [COUNTER_WIDTH-1:0] COUNT_ONE = {{(COUNTER_WIDTH-1){1'b0}}, 1'b1};

assign start_event = start_i & ~start_q;

always_ff @(posedge clk) begin
    if (rst) begin
        state_q          <= ST_IDLE;
        start_q          <= 1'b0;
        delay_count_q    <= '0;
        duration_count_q <= '0;
        event_o          <= 1'b0;
        busy_o           <= 1'b0;
        done_o           <= 1'b0;
    end else begin
        start_q <= start_i;
        done_o  <= 1'b0;

        case (state_q)
            ST_IDLE: begin
                event_o <= 1'b0;
                busy_o  <= 1'b0;

                if (enable_i && start_event) begin
                    if (duration_cycles_i == '0) begin
                        done_o <= 1'b1;
                    end else if (delay_cycles_i == '0) begin
                        event_o <= 1'b1;
                        busy_o <= 1'b1;
                        if (duration_cycles_i == COUNT_ONE) begin
                            done_o <= 1'b1;
                        end else begin
                            state_q          <= ST_ACTIVE;
                            duration_count_q <= duration_cycles_i - COUNT_ONE;
                        end
                    end else begin
                        state_q          <= ST_DELAY;
                        busy_o           <= 1'b1;
                        delay_count_q    <= delay_cycles_i;
                        duration_count_q <= duration_cycles_i;
                    end
                end
            end

            ST_DELAY: begin
                event_o <= 1'b0;
                busy_o  <= 1'b1;

                if (!enable_i) begin
                    state_q <= ST_IDLE;
                    busy_o  <= 1'b0;
                end else if (delay_count_q <= COUNT_ONE) begin
                    event_o <= 1'b1;
                    if (duration_count_q == COUNT_ONE) begin
                        state_q <= ST_IDLE;
                        busy_o  <= 1'b1;
                        done_o  <= 1'b1;
                    end else begin
                        state_q          <= ST_ACTIVE;
                        duration_count_q <= duration_count_q - COUNT_ONE;
                    end
                end else begin
                    delay_count_q <= delay_count_q - COUNT_ONE;
                end
            end

            ST_ACTIVE: begin
                event_o <= 1'b1;
                busy_o  <= 1'b1;

                if (!enable_i) begin
                    state_q <= ST_IDLE;
                    event_o <= 1'b0;
                    busy_o  <= 1'b0;
                end else if (duration_count_q <= COUNT_ONE) begin
                    state_q <= ST_IDLE;
                    busy_o  <= 1'b1;
                    done_o  <= 1'b1;
                end else begin
                    duration_count_q <= duration_count_q - COUNT_ONE;
                end
            end

            default: begin
                state_q <= ST_IDLE;
                event_o <= 1'b0;
                busy_o  <= 1'b0;
                done_o  <= 1'b0;
            end
        endcase
    end
end

endmodule
