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

module tx_hs_spi_scheduler
  import tx_hs_spi_pkg::*;
#(
    parameter int MAX_OPS_PER_BANK = TX_HS_SPI_DEFAULT_MAX_OPS_PER_BANK,
    parameter int OP_INDEX_W       = (MAX_OPS_PER_BANK <= 1) ? 1 : $clog2(MAX_OPS_PER_BANK)
)(
    input  logic                  clk,
    input  logic                  rst,

    input  logic                  start_i,
    input  logic                  bank_select_i,
    input  logic [1:0]            bank_ready_i,
    input  logic [15:0]           bank0_op_count_i,
    input  logic [15:0]           bank1_op_count_i,
    output logic                  bank_consume_o,
    output logic                  bank_consume_select_o,
    output logic                  bank_rd_select_o,
    output logic [OP_INDEX_W-1:0] bank_rd_index_o,
    input  tx_hs_spi_op_t         bank_rd_op_i,

    output logic                  op_valid_o,
    input  logic                  op_ready_i,
    output tx_hs_spi_op_t         op_o,
    input  logic                  op_done_i,
    input  logic                  op_fault_i,

    output logic                  busy_o,
    output logic [15:0]           active_op_count_o,
    output logic [15:0]           issued_op_count_o,
    output logic                  done_pulse_o,
    output logic                  fault_pulse_o,
    output logic [7:0]            last_error_code_o,
    output logic [15:0]           fault_op_index_o,
    output logic [31:0]           fault_count_o
);

typedef enum logic [2:0] {
    ST_IDLE,
    ST_READ_WAIT,
    ST_LATCH_OP,
    ST_ISSUE_OP,
    ST_WAIT_OP_DONE,
    ST_CONSUME_BANK,
    ST_WAIT_BANK_CLEAR
} state_e;

state_e          state_q             /* synthesis preserve noprune */;
logic [15:0]     active_op_count_q   /* synthesis preserve noprune */;
logic [15:0]     op_index_q          /* synthesis preserve noprune */;
logic [15:0]     issued_op_count_q   /* synthesis preserve noprune */;
logic            bank_consume_q      /* synthesis preserve noprune */;
logic            bank_rd_select_q    /* synthesis preserve noprune */;
logic [OP_INDEX_W-1:0] bank_rd_index_q /* synthesis preserve noprune */;
logic            op_valid_q          /* synthesis preserve noprune */;
tx_hs_spi_op_t   op_q                /* synthesis preserve noprune */;
logic            done_pulse_q        /* synthesis preserve noprune */;
logic            fault_pulse_q       /* synthesis preserve noprune */;
logic [7:0]      last_error_code_q   /* synthesis preserve noprune */;
logic [15:0]     fault_op_index_q    /* synthesis preserve noprune */;
logic [31:0]     fault_count_q       /* synthesis preserve noprune */;

assign bank_consume_o        = bank_consume_q;
assign bank_consume_select_o = bank_rd_select_q;
assign bank_rd_select_o      = bank_rd_select_q;
assign bank_rd_index_o       = bank_rd_index_q;
assign op_valid_o            = op_valid_q;
assign op_o                  = op_q;
assign busy_o                = (state_q != ST_IDLE);
assign active_op_count_o     = active_op_count_q;
assign issued_op_count_o     = issued_op_count_q;
assign done_pulse_o          = done_pulse_q;
assign fault_pulse_o         = fault_pulse_q;
assign last_error_code_o     = last_error_code_q;
assign fault_op_index_o      = fault_op_index_q;
assign fault_count_o         = fault_count_q;

always_ff @(posedge clk) begin
    if (rst) begin
        state_q           <= ST_IDLE;
        active_op_count_q <= 16'd0;
        op_index_q        <= 16'd0;
        issued_op_count_q <= 16'd0;
        bank_consume_q    <= 1'b0;
        bank_rd_select_q  <= 1'b0;
        bank_rd_index_q   <= '0;
        op_valid_q        <= 1'b0;
        op_q              <= '0;
        done_pulse_q      <= 1'b0;
        fault_pulse_q     <= 1'b0;
        last_error_code_q <= TX_HS_SPI_SCHED_ERR_NONE;
        fault_op_index_q  <= 16'd0;
        fault_count_q     <= 32'd0;
    end else begin
        bank_consume_q <= 1'b0;
        done_pulse_q   <= 1'b0;
        fault_pulse_q  <= 1'b0;

        case (state_q)
            ST_IDLE: begin
                op_valid_q <= 1'b0;
                if (start_i && bank_ready_i[bank_select_i] &&
                    ((bank_select_i ? bank1_op_count_i : bank0_op_count_i) != 16'd0)) begin
                    active_op_count_q <= bank_select_i ? bank1_op_count_i : bank0_op_count_i;
                    op_index_q        <= 16'd0;
                    issued_op_count_q <= 16'd0;
                    bank_rd_select_q  <= bank_select_i;
                    bank_rd_index_q   <= '0;
                    state_q           <= ST_READ_WAIT;
                end
            end

            ST_READ_WAIT: begin
                state_q <= ST_LATCH_OP;
            end

            ST_LATCH_OP: begin
                op_q       <= bank_rd_op_i;
                op_valid_q <= 1'b1;
                state_q    <= ST_ISSUE_OP;
            end

            ST_ISSUE_OP: begin
                if (op_ready_i) begin
                    op_valid_q        <= 1'b0;
                    issued_op_count_q <= issued_op_count_q + 16'd1;
                    state_q           <= ST_WAIT_OP_DONE;
                end
            end

            ST_WAIT_OP_DONE: begin
                if (op_fault_i) begin
                    fault_pulse_q     <= 1'b1;
                    last_error_code_q <= TX_HS_SPI_SCHED_ERR_PHY_FAULT;
                    fault_op_index_q  <= op_index_q;
                    fault_count_q     <= fault_count_q + 1'b1;
                    state_q           <= ST_CONSUME_BANK;
                end else if (op_done_i) begin
                    if ((op_index_q + 16'd1) >= active_op_count_q) begin
                        done_pulse_q <= 1'b1;
                        state_q      <= ST_CONSUME_BANK;
                    end else begin
                        op_index_q      <= op_index_q + 16'd1;
                        bank_rd_index_q <= bank_rd_index_q + 1'b1;
                        state_q         <= ST_READ_WAIT;
                    end
                end
            end

            ST_CONSUME_BANK: begin
                bank_consume_q <= 1'b1;
                state_q        <= ST_WAIT_BANK_CLEAR;
            end

            ST_WAIT_BANK_CLEAR: begin
                if (!bank_ready_i[bank_rd_select_q]) begin
                    state_q <= ST_IDLE;
                end
            end

            default: begin
                state_q <= ST_IDLE;
            end
        endcase
    end
end

endmodule
