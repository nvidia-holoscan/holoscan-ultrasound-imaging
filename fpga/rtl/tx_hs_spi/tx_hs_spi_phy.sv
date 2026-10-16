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

module tx_hs_spi_phy
  import tx_hs_spi_pkg::*;
(
    input  logic          clk,
    input  logic          rst,

    input  logic          start_i,
    input  tx_hs_spi_op_t op_i,
    input  logic [15:0]   half_period_cycles_i,
    input  logic          tx_sdout_i,

    output logic          busy_o,
    output logic          done_o,
    output logic          fault_o,
    output logic [7:0]    rx_data_o,
    output logic          rx_valid_o,

    output logic          tx_sclk_o,
    output logic          tx_sen_o,
    output logic          tx1_sdata0_o,
    output logic          tx1_sdata1_o,
    output logic          tx2_sdata0_o,
    output logic          tx2_sdata1_o
);

typedef enum logic [1:0] {
    ST_IDLE,
    ST_ACTIVE,
    ST_FINISH
} state_e;

state_e         state_q;
tx_hs_spi_op_t op_q;
logic [7:0]    clocks_remaining_q;
logic [7:0]    bit_index_q;
logic [15:0]   half_count_q;
logic [15:0]   half_period_q;
logic          keep_sen_low_q;
logic          tx1_sdata1_enable_q;
logic          tx2_sdata1_enable_q;
logic          capture_sdout_q;
logic          disable_sdata_q;
logic [7:0]    rx_shift_q;
logic          start_fault;
localparam logic [7:0] MAX_CLOCK_COUNT_U8 = TX_HS_SPI_MAX_CLOCK_COUNT;
logic          launch;
logic          sclk_rising_edge;
logic          sclk_falling_edge;

assign start_fault = (op_i.clock_count == 8'd0) ||
                     (op_i.clock_count > MAX_CLOCK_COUNT_U8) ||
                     (op_i.flags[7:5] != 3'd0);

assign launch            = start_i && !busy_o && !start_fault;
assign sclk_rising_edge  = (state_q == ST_ACTIVE) && (half_count_q == 16'd0) && (tx_sclk_o == 1'b0);
assign sclk_falling_edge = (state_q == ST_ACTIVE) && (half_count_q == 16'd0) && (tx_sclk_o == 1'b1);

assign busy_o = (state_q != ST_IDLE);

always_comb begin
    if (disable_sdata_q) begin
        tx1_sdata0_o = 1'b0;
        tx1_sdata1_o = 1'b0;
        tx2_sdata0_o = 1'b0;
        tx2_sdata1_o = 1'b0;
    end else begin
        tx1_sdata0_o = op_q.tx1_sdata0[bit_index_q];
        tx1_sdata1_o = tx1_sdata1_enable_q ? op_q.tx1_sdata1[bit_index_q] : 1'b0;
        tx2_sdata0_o = op_q.tx2_sdata0[bit_index_q];
        tx2_sdata1_o = tx2_sdata1_enable_q ? op_q.tx2_sdata1[bit_index_q] : 1'b0;
    end
end

always_ff @(posedge clk) begin
    if (rst) begin
        state_q             <= ST_IDLE;
        op_q                <= '0;
        clocks_remaining_q  <= 8'd0;
        bit_index_q         <= 8'd0;
        half_count_q        <= 16'd0;
        half_period_q       <= 16'd1;
        keep_sen_low_q      <= 1'b0;
        tx1_sdata1_enable_q <= 1'b0;
        tx2_sdata1_enable_q <= 1'b0;
        capture_sdout_q     <= 1'b0;
        disable_sdata_q     <= 1'b0;
        rx_shift_q          <= 8'd0;
        rx_data_o           <= 8'd0;
        rx_valid_o          <= 1'b0;
        tx_sclk_o           <= 1'b0;
        tx_sen_o            <= 1'b1;
        done_o              <= 1'b0;
        fault_o             <= 1'b0;
    end else begin
        done_o     <= 1'b0;
        fault_o    <= start_i && !busy_o && start_fault;
        rx_valid_o <= 1'b0;

        case (state_q)
            ST_IDLE: begin
                tx_sclk_o <= 1'b0;
                if (!keep_sen_low_q) begin
                    tx_sen_o <= 1'b1;
                end
                if (launch) begin
                    op_q                <= op_i;
                    clocks_remaining_q  <= op_i.clock_count;
                    bit_index_q         <= op_i.clock_count - 8'd1;
                    half_period_q       <= (half_period_cycles_i == 16'd0) ? 16'd1 : half_period_cycles_i;
                    half_count_q        <= (half_period_cycles_i == 16'd0) ? 16'd0 : (half_period_cycles_i - 16'd1);
                    keep_sen_low_q      <= op_i.flags[TX_HS_SPI_OP_KEEP_SEN_LOW];
                    tx1_sdata1_enable_q <= op_i.flags[TX_HS_SPI_OP_TX1_SDATA1_ENABLE];
                    tx2_sdata1_enable_q <= op_i.flags[TX_HS_SPI_OP_TX2_SDATA1_ENABLE];
                    capture_sdout_q     <= op_i.flags[TX_HS_SPI_OP_CAPTURE_SDOUT];
                    disable_sdata_q     <= op_i.flags[TX_HS_SPI_OP_DISABLE_SDATA];
                    rx_shift_q          <= 8'd0;
                    tx_sen_o            <= 1'b0;
                    state_q             <= ST_ACTIVE;
                end
            end

            ST_ACTIVE: begin
                if (half_count_q != 16'd0) begin
                    half_count_q <= half_count_q - 16'd1;
                end else begin
                    half_count_q <= half_period_q - 16'd1;
                    tx_sclk_o    <= ~tx_sclk_o;

                    if (sclk_rising_edge) begin
                        clocks_remaining_q <= clocks_remaining_q - 8'd1;
                        if (capture_sdout_q) begin
                            rx_shift_q <= {rx_shift_q[6:0], tx_sdout_i};
                        end
                    end

                    if (sclk_falling_edge) begin
                        if (clocks_remaining_q == 8'd0) begin
                            state_q <= ST_FINISH;
                        end else if (bit_index_q != 8'd0) begin
                            bit_index_q <= bit_index_q - 8'd1;
                        end
                    end
                end
            end

            ST_FINISH: begin
                tx_sclk_o <= 1'b0;
                if (!keep_sen_low_q) begin
                    tx_sen_o <= 1'b1;
                end
                rx_data_o  <= rx_shift_q;
                rx_valid_o <= capture_sdout_q;
                done_o     <= 1'b1;
                state_q    <= ST_IDLE;
            end

            default: begin
                state_q <= ST_IDLE;
            end
        endcase
    end
end

endmodule
