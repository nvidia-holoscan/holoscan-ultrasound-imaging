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

module led #(
    parameter int LED_N = 1,
    parameter int STEP_COUNT_WIDTH = 25,
    parameter logic [31:0] LFSR_RESET = 32'h1ACE_B00C
) (
    input  logic               clk,
    input  logic               rst,
    output logic [LED_N-1:0]   led_o
);

logic [31:0]                 lfsr_q;
logic [31:0]                 lfsr_mixed;
logic [STEP_COUNT_WIDTH-1:0] step_count_q;
logic [LED_N-1:0]            led_q;
logic                        lfsr_feedback;
logic                        led_step;

assign lfsr_feedback = lfsr_q[31] ^ lfsr_q[21] ^ lfsr_q[1] ^ lfsr_q[0];
assign led_step      = &step_count_q;
assign lfsr_mixed    = lfsr_q ^ {lfsr_q[15:0], lfsr_q[31:16]} ^ {lfsr_q[6:0], lfsr_q[31:7]};
assign led_o         = led_q;

always_ff @(posedge clk) begin
    if (rst) begin
        step_count_q <= '0;
        lfsr_q       <= LFSR_RESET;
        led_q        <= LFSR_RESET[31 -: LED_N];
    end else begin
        step_count_q <= step_count_q + 1'b1;
        lfsr_q       <= {lfsr_q[30:0], lfsr_feedback};

        if (led_step) begin
            led_q <= lfsr_mixed[31 -: LED_N];
        end
    end
end

endmodule
