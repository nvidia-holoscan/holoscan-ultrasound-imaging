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

module tx_hs_spi_slow_ctrl
  import apb_pkg::*;
  import tx_hs_spi_pkg::*;
(
    input  logic          apb_clk,
    input  logic          apb_rst,
    input  apb_m2s        i_apb_m2s,
    output apb_s2m        o_apb_s2m,

    input  logic          tx_spi_clk,
    input  logic          tx_spi_rst,

    output logic          op_valid_o,
    input  logic          op_ready_i,
    output tx_hs_spi_op_t op_o,
    output logic [15:0]   half_period_cycles_o,

    input  logic          op_done_i,
    input  logic          op_fault_i,
    input  logic [7:0]    op_rx_data_i,
    input  logic          op_rx_valid_i
);

localparam int TX_HS_SPI_N_CTRL = 6;
localparam int TX_HS_SPI_N_STAT = 2;
localparam int TX_HS_SPI_W_OFST = 8;

localparam int CTRL_CMD      = 0; // 0x00
localparam int CTRL_CFG      = 1; // 0x04
localparam int CTRL_TX1_ADDR = 2; // 0x08
localparam int CTRL_TX1_DATA = 3; // 0x0c
localparam int CTRL_TX2_ADDR = 4; // 0x10
localparam int CTRL_TX2_DATA = 5; // 0x14

localparam int STAT_STATUS    = 0; // 0x80
localparam int STAT_READ_DATA = 1; // 0x84

typedef enum logic [3:0] {
    ST_IDLE,
    ST_ISSUE_WRITE,
    ST_WAIT_WRITE,
    ST_ISSUE_READ_PREFIX,
    ST_WAIT_READ_PREFIX,
    ST_ISSUE_READ_ADDR,
    ST_WAIT_READ_ADDR,
    ST_ISSUE_READ_BYTE,
    ST_WAIT_READ_BYTE,
    ST_DONE
} state_e;

logic [31:0] ctrl_reg [TX_HS_SPI_N_CTRL];
logic [31:0] stat_reg [TX_HS_SPI_N_STAT];

state_e        state_q;
tx_hs_spi_op_t op_q;
logic [31:0]   status_word;
logic          cmd_start;
logic [1:0]    cmd_op;
logic [1:0]    write_mask;
logic [1:0]    read_target;
logic [15:0]   half_period_cycles;
logic [9:0]    tx1_addr;
logic [31:0]   tx1_data;
logic [9:0]    tx2_addr;
logic [31:0]   tx2_data;
logic          cmd_err_q;
logic [1:0]    active_read_target_q;
logic [9:0]    active_read_addr_q;
logic [1:0]    read_byte_index_q;
logic [31:0]   read_shift_q;
logic [31:0]   read_data_q;

assign cmd_start          = ctrl_reg[CTRL_CMD][TX_HS_SPI_CTRL_START_LSB];
assign cmd_op             = ctrl_reg[CTRL_CMD][TX_HS_SPI_CTRL_OP_LSB+:2];
assign write_mask         = ctrl_reg[CTRL_CMD][TX_HS_SPI_CTRL_WRITE_MASK_LSB+:2];
assign read_target        = ctrl_reg[CTRL_CMD][TX_HS_SPI_CTRL_READ_TARGET_LSB+:2];
assign half_period_cycles = ctrl_reg[CTRL_CFG][15:0];
assign tx1_addr           = ctrl_reg[CTRL_TX1_ADDR][9:0];
assign tx1_data           = ctrl_reg[CTRL_TX1_DATA];
assign tx2_addr           = ctrl_reg[CTRL_TX2_ADDR][9:0];
assign tx2_data           = ctrl_reg[CTRL_TX2_DATA];

assign half_period_cycles_o = half_period_cycles;
assign op_o                 = op_q;

assign status_word[0]     = (state_q != ST_IDLE) && (state_q != ST_DONE);
assign status_word[1]     = cmd_err_q;
assign status_word[3:2]   = 2'd0;
assign status_word[4]     = (state_q == ST_DONE);
assign status_word[7:5]   = 3'd0;
assign status_word[15:8]  = state_q;
assign status_word[17:16] = cmd_op;
assign status_word[19:18] = write_mask;
assign status_word[21:20] = read_target;
assign status_word[23:22] = read_byte_index_q;
assign status_word[31:24] = 8'd0;
assign stat_reg[STAT_STATUS]    = status_word;
assign stat_reg[STAT_READ_DATA] = read_data_q;

// APB and TX SPI clocks may be independent; the APB register bank crosses
// into the TX SPI execution domain through s_apb_reg CDC.
s_apb_reg #(
    .N_CTRL   (TX_HS_SPI_N_CTRL),
    .N_STAT   (TX_HS_SPI_N_STAT),
    .W_OFST   (TX_HS_SPI_W_OFST),
    .SYNC_CLK (0),
    .SAME_CLK (0)
) u_apb_reg (
    .i_aclk    (apb_clk),
    .i_arst    (apb_rst),
    .i_apb_m2s (i_apb_m2s),
    .o_apb_s2m (o_apb_s2m),
    .i_pclk    (tx_spi_clk),
    .i_prst    (tx_spi_rst),
    .o_ctrl    (ctrl_reg),
    .i_stat    (stat_reg)
);

function automatic logic target_tx1(input logic [1:0] target);
    return (target == TX_HS_SPI_TARGET_TX1);
endfunction

function automatic logic target_tx2(input logic [1:0] target);
    return (target == TX_HS_SPI_TARGET_TX2);
endfunction

function automatic tx_hs_spi_op_t make_write_op(
    input logic [1:0]  mask,
    input logic [9:0]  addr1,
    input logic [31:0] data1,
    input logic [9:0]  addr2,
    input logic [31:0] data2
);
    tx_hs_spi_op_t op;
    begin
        op = '0;
        op.clock_count = TX_HS_SPI_NORMAL_WRITE_BITS[7:0];
        if (mask[0]) begin
            op.tx1_sdata0[TX_HS_SPI_NORMAL_WRITE_BITS-1:0] = {2'b00, addr1, data1};
        end
        if (mask[1]) begin
            op.tx2_sdata0[TX_HS_SPI_NORMAL_WRITE_BITS-1:0] = {2'b00, addr2, data2};
        end
        return op;
    end
endfunction

function automatic tx_hs_spi_op_t make_read_prefix_op(
    input logic [1:0] target,
    input logic [9:0] addr
);
    tx_hs_spi_op_t op;
    begin
        op = '0;
        op.clock_count = TX_HS_SPI_NORMAL_WRITE_BITS[7:0];
        op.flags[TX_HS_SPI_OP_KEEP_SEN_LOW] = 1'b1;
        if (target_tx1(target)) begin
            op.tx1_sdata0[TX_HS_SPI_NORMAL_WRITE_BITS-1:0] = {2'b00, addr, 32'd0};
        end
        if (target_tx2(target)) begin
            op.tx2_sdata0[TX_HS_SPI_NORMAL_WRITE_BITS-1:0] = {2'b00, addr, 32'd0};
        end
        return op;
    end
endfunction

function automatic tx_hs_spi_op_t make_read_addr_op(
    input logic [1:0] target,
    input logic [9:0] addr
);
    tx_hs_spi_op_t op;
    begin
        op = '0;
        op.clock_count = 8'd12;
        op.flags[TX_HS_SPI_OP_KEEP_SEN_LOW] = 1'b1;
        if (target_tx1(target)) begin
            op.tx1_sdata0[11:0] = {2'b00, addr};
        end
        if (target_tx2(target)) begin
            op.tx2_sdata0[11:0] = {2'b00, addr};
        end
        return op;
    end
endfunction

function automatic tx_hs_spi_op_t make_read_byte_op(input logic keep_sen_low);
    tx_hs_spi_op_t op;
    begin
        op = '0;
        op.clock_count = 8'd8;
        op.flags[TX_HS_SPI_OP_CAPTURE_SDOUT] = 1'b1;
        op.flags[TX_HS_SPI_OP_DISABLE_SDATA] = 1'b1;
        op.flags[TX_HS_SPI_OP_KEEP_SEN_LOW] = keep_sen_low;
        return op;
    end
endfunction

function automatic logic [9:0] selected_read_addr(
    input logic [1:0] target,
    input logic [9:0] addr1,
    input logic [9:0] addr2
);
    return target_tx1(target) ? addr1 : addr2;
endfunction

function automatic logic command_is_valid(
    input logic [1:0] op,
    input logic [1:0] mask,
    input logic [1:0] target
);
    if (op == TX_HS_SPI_CTRL_OP_WRITE) begin
        return (mask != 2'd0);
    end else if (op == TX_HS_SPI_CTRL_OP_READ) begin
        return target_tx1(target) || target_tx2(target);
    end else begin
        return 1'b0;
    end
endfunction

always_ff @(posedge tx_spi_clk) begin
    if (tx_spi_rst) begin
        state_q              <= ST_IDLE;
        op_valid_o           <= 1'b0;
        op_q                 <= '0;
        cmd_err_q            <= 1'b0;
        active_read_target_q <= TX_HS_SPI_TARGET_TX1;
        active_read_addr_q   <= 10'd0;
        read_byte_index_q    <= 2'd0;
        read_shift_q         <= 32'd0;
        read_data_q          <= 32'd0;
    end else begin
        case (state_q)
            ST_IDLE: begin
                op_valid_o        <= 1'b0;
                cmd_err_q         <= 1'b0;
                read_byte_index_q <= 2'd0;
                read_shift_q      <= 32'd0;
                if (cmd_start) begin
                    if (!command_is_valid(cmd_op, write_mask, read_target)) begin
                        cmd_err_q <= 1'b1;
                        state_q   <= ST_DONE;
                    end else if (cmd_op == TX_HS_SPI_CTRL_OP_WRITE) begin
                        op_q       <= make_write_op(write_mask, tx1_addr, tx1_data, tx2_addr, tx2_data);
                        op_valid_o <= 1'b1;
                        state_q    <= ST_ISSUE_WRITE;
                    end else begin
                        active_read_target_q <= read_target;
                        active_read_addr_q   <= selected_read_addr(read_target, tx1_addr, tx2_addr);
                        op_q                 <= make_read_prefix_op(read_target, selected_read_addr(read_target, tx1_addr, tx2_addr));
                        op_valid_o           <= 1'b1;
                        state_q              <= ST_ISSUE_READ_PREFIX;
                    end
                end
            end

            ST_ISSUE_WRITE: begin
                if (op_ready_i) begin
                    op_valid_o <= 1'b0;
                    state_q    <= ST_WAIT_WRITE;
                end
            end

            ST_WAIT_WRITE: begin
                if (op_fault_i) begin
                    cmd_err_q <= 1'b1;
                    state_q   <= ST_DONE;
                end else if (op_done_i) begin
                    state_q <= ST_DONE;
                end
            end

            ST_ISSUE_READ_PREFIX: begin
                if (op_ready_i) begin
                    op_valid_o <= 1'b0;
                    state_q    <= ST_WAIT_READ_PREFIX;
                end
            end

            ST_WAIT_READ_PREFIX: begin
                if (op_fault_i) begin
                    cmd_err_q <= 1'b1;
                    state_q   <= ST_DONE;
                end else if (op_done_i) begin
                    op_q       <= make_read_addr_op(active_read_target_q, active_read_addr_q);
                    op_valid_o <= 1'b1;
                    state_q    <= ST_ISSUE_READ_ADDR;
                end
            end

            ST_ISSUE_READ_ADDR: begin
                if (op_ready_i) begin
                    op_valid_o <= 1'b0;
                    state_q    <= ST_WAIT_READ_ADDR;
                end
            end

            ST_WAIT_READ_ADDR: begin
                if (op_fault_i) begin
                    cmd_err_q <= 1'b1;
                    state_q   <= ST_DONE;
                end else if (op_done_i) begin
                    op_q       <= make_read_byte_op(1'b1);
                    op_valid_o <= 1'b1;
                    state_q    <= ST_ISSUE_READ_BYTE;
                end
            end

            ST_ISSUE_READ_BYTE: begin
                if (op_ready_i) begin
                    op_valid_o <= 1'b0;
                    state_q    <= ST_WAIT_READ_BYTE;
                end
            end

            ST_WAIT_READ_BYTE: begin
                if (op_fault_i) begin
                    cmd_err_q <= 1'b1;
                    state_q   <= ST_DONE;
                end else if (op_done_i) begin
                    if (op_rx_valid_i) begin
                        read_shift_q <= {read_shift_q[23:0], op_rx_data_i};
                        if (read_byte_index_q == 2'd3) begin
                            read_data_q <= {read_shift_q[23:0], op_rx_data_i};
                            state_q     <= ST_DONE;
                        end else begin
                            read_byte_index_q <= read_byte_index_q + 2'd1;
                            op_q              <= make_read_byte_op(read_byte_index_q != 2'd2);
                            op_valid_o        <= 1'b1;
                            state_q           <= ST_ISSUE_READ_BYTE;
                        end
                    end else begin
                        cmd_err_q <= 1'b1;
                        state_q   <= ST_DONE;
                    end
                end
            end

            ST_DONE: begin
                op_valid_o <= 1'b0;
                if (!cmd_start) begin
                    state_q <= ST_IDLE;
                end
            end

            default: begin
                op_valid_o <= 1'b0;
                state_q    <= ST_IDLE;
            end
        endcase
    end
end

endmodule
