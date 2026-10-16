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

module tx_hs_spi_package_rx
  import tx_hs_spi_pkg::*;
#(
    parameter int MAX_OPS_PER_BANK = TX_HS_SPI_DEFAULT_MAX_OPS_PER_BANK,
    parameter int OP_INDEX_W       = (MAX_OPS_PER_BANK <= 1) ? 1 : $clog2(MAX_OPS_PER_BANK)
)(
    input  logic                         clk,
    input  logic                         rst,

    input  logic                         s_axis_tvalid_i,
    output logic                         s_axis_tready_o,
    input  logic [511:0]                 s_axis_tdata_i,
    input  logic [63:0]                  s_axis_tkeep_i,
    input  logic                         s_axis_tlast_i,

    input  logic                         expected_sequence_valid_i,
    input  logic [15:0]                  expected_sequence_i,

    // Bank interface to the future scheduler.
    input  logic                         bank_consume_i,             // Free selected bank after scheduler use.
    input  logic                         bank_consume_select_i,      // Bank index to free: 0=bank0, 1=bank1.
    input  logic                         bank_rd_select_i,           // Bank index selected for operation readback.
    input  logic [OP_INDEX_W-1:0]        bank_rd_index_i,            // Operation index inside selected bank.
    output tx_hs_spi_op_t                bank_rd_op_o,               // Operation record read from selected bank.
    output logic [1:0]                   bank_ready_o,               // One bit per bank; high when package is ready.
    output logic [15:0]                  bank0_sequence_o,           // Event sequence stored in bank 0.
    output logic [15:0]                  bank1_sequence_o,           // Event sequence stored in bank 1.
    output logic [15:0]                  bank0_op_count_o,           // Valid operation count in bank 0.
    output logic [15:0]                  bank1_op_count_o,           // Valid operation count in bank 1.

    output logic                         package_ready_pulse_o,
    output logic                         package_ready_bank_o,
    output logic [15:0]                  package_ready_sequence_o,
    output logic [15:0]                  package_ready_op_count_o,

    output logic                         error_pulse_o,
    output logic [7:0]                   last_error_code_o,
    output logic [31:0]                  accepted_package_count_o,
    output logic [31:0]                  accepted_fragment_count_o,
    output logic [31:0]                  error_count_o
);

typedef enum logic [2:0] {
    ST_WAIT_HEADER,
    ST_PARSE_HEADER,
    ST_CHECK_HEADER,
    ST_WRITE_HEADER_OP,
    ST_WAIT_DATA,
    ST_WRITE_SLOT0,
    ST_WRITE_SLOT1
} state_e;

state_e          state_q;
logic            axis_valid;
logic [511:0]    beat_data_q;
logic [63:0]     beat_keep_q;
logic            beat_last_q;
logic            active_bank_q                       /* synthesis preserve noprune */;
logic [15:0]     next_op_index_q                    /* synthesis preserve noprune */;
logic            header_parsed_pulse_q             /* synthesis preserve noprune */;
logic            header_valid_q                    /* synthesis preserve noprune */;
tx_hs_spi_fragment_header_t header_q               /* synthesis preserve noprune */;
logic            ram_wr_en_q                       /* synthesis preserve noprune */;
logic            ram_wr_bank_q                     /* synthesis preserve noprune */;
logic [OP_INDEX_W-1:0] ram_wr_addr_q               /* synthesis preserve noprune */;
tx_hs_spi_op_t   ram_wr_op_q                       /* synthesis preserve noprune */;
logic            ram_wr_final_q                    /* synthesis preserve noprune */;
tx_hs_spi_op_t   bank0_rd_op;
tx_hs_spi_op_t   bank1_rd_op;
logic [1:0]      bank_ready_q                      /* synthesis preserve noprune */;
logic [15:0]     bank0_sequence_q                  /* synthesis preserve noprune */;
logic [15:0]     bank1_sequence_q                  /* synthesis preserve noprune */;
logic [15:0]     bank0_op_count_q                  /* synthesis preserve noprune */;
logic [15:0]     bank1_op_count_q                  /* synthesis preserve noprune */;
logic            package_ready_pulse_q             /* synthesis preserve noprune */;
logic            package_ready_bank_q              /* synthesis preserve noprune */;
logic [15:0]     package_ready_sequence_q          /* synthesis preserve noprune */;
logic [15:0]     package_ready_op_count_q          /* synthesis preserve noprune */;
logic            error_pulse_q                     /* synthesis preserve noprune */;
logic [7:0]      last_error_code_q                 /* synthesis preserve noprune */;
logic [31:0]     accepted_package_count_q          /* synthesis preserve noprune */;
logic [31:0]     accepted_fragment_count_q         /* synthesis preserve noprune */;
logic [31:0]     error_count_q                     /* synthesis preserve noprune */;
logic [31:0]     header_payload_bytes;
logic            header_check_valid;
logic [7:0]      header_error_code;
logic            package_accept_pulse;
assign axis_valid = s_axis_tvalid_i && s_axis_tready_o;
assign s_axis_tready_o = ((state_q == ST_WAIT_HEADER) &&
                          (bank_ready_q != 2'b11) && !package_accept_pulse) ||
                         (state_q == ST_WAIT_DATA);

tx_hs_spi_bank_ram #(
    .DEPTH  (MAX_OPS_PER_BANK),
    .ADDR_W (OP_INDEX_W)
) u_bank0_ram (
    .clk       (clk),
    .wr_en_i   (ram_wr_en_q && !ram_wr_bank_q),
    .wr_addr_i (ram_wr_addr_q),
    .wr_op_i   (ram_wr_op_q),
    .rd_addr_i (bank_rd_index_i),
    .rd_op_o   (bank0_rd_op)
);

tx_hs_spi_bank_ram #(
    .DEPTH  (MAX_OPS_PER_BANK),
    .ADDR_W (OP_INDEX_W)
) u_bank1_ram (
    .clk       (clk),
    .wr_en_i   (ram_wr_en_q && ram_wr_bank_q),
    .wr_addr_i (ram_wr_addr_q),
    .wr_op_i   (ram_wr_op_q),
    .rd_addr_i (bank_rd_index_i),
    .rd_op_o   (bank1_rd_op)
);

assign bank_rd_op_o              = bank_rd_select_i ? bank1_rd_op : bank0_rd_op;
assign bank_ready_o              = bank_ready_q;
assign bank0_sequence_o          = bank0_sequence_q;
assign bank1_sequence_o          = bank1_sequence_q;
assign bank0_op_count_o          = bank0_op_count_q;
assign bank1_op_count_o          = bank1_op_count_q;
assign package_ready_pulse_o     = package_ready_pulse_q;
assign package_ready_bank_o      = package_ready_bank_q;
assign package_ready_sequence_o  = package_ready_sequence_q;
assign package_ready_op_count_o  = package_ready_op_count_q;
assign error_pulse_o             = error_pulse_q;
assign last_error_code_o         = last_error_code_q;
assign accepted_package_count_o  = accepted_package_count_q;
assign accepted_fragment_count_o = accepted_fragment_count_q;
assign error_count_o             = error_count_q;
assign package_accept_pulse      = ram_wr_en_q && ram_wr_final_q;
assign header_payload_bytes      = {16'd0, header_q.total_operation_count} << 5;

always_comb begin
    header_check_valid = 1'b0;
    header_error_code  = TX_HS_SPI_PKG_ERR_NONE;

    if ((header_q.magic != TX_HS_SPI_MAGIC) ||
        (header_q.version != TX_HS_SPI_PROTOCOL_VERSION) ||
        (header_q.header_bytes != TX_HS_SPI_FRAGMENT_HDR_BYTES) ||
        (header_q.record_bytes != TX_HS_SPI_OP_BYTES) ||
        (header_q.flags != 16'd0) ||
        (header_q.reserved0 != 16'd0) ||
        (header_q.reserved1 != 32'd0)) begin
        header_error_code = TX_HS_SPI_PKG_ERR_BAD_HEADER;
    end else if ((header_q.fragment_index != 16'd0) ||
                 (header_q.fragment_count != 16'd1)) begin
        header_error_code = TX_HS_SPI_PKG_ERR_UNEXPECTED_FRAGMENT;
    end else if (header_q.total_operation_count == 16'd0) begin
        header_error_code = TX_HS_SPI_PKG_ERR_BAD_PAYLOAD;
    end else if (header_q.total_operation_count > MAX_OPS_PER_BANK) begin
        header_error_code = TX_HS_SPI_PKG_ERR_TOO_MANY_OPS;
    end else if ((header_q.fragment_operation_count != header_q.total_operation_count) ||
                 (header_q.fragment_payload_bytes != header_payload_bytes[15:0]) ||
                 (header_q.total_payload_bytes != header_payload_bytes)) begin
        header_error_code = TX_HS_SPI_PKG_ERR_BAD_PAYLOAD;
    end else if (expected_sequence_valid_i &&
                 (header_q.event_sequence != expected_sequence_i)) begin
        header_error_code = TX_HS_SPI_PKG_ERR_BAD_SEQUENCE;
    end else begin
        header_check_valid = 1'b1;
    end
end

always_ff @(posedge clk) begin
    if (rst) begin
        state_q                             <= ST_WAIT_HEADER;
        beat_data_q                         <= 512'd0;
        beat_keep_q                         <= 64'd0;
        beat_last_q                         <= 1'b0;
        active_bank_q                       <= 1'b0;
        next_op_index_q                     <= 16'd0;
        header_parsed_pulse_q               <= 1'b0;
        header_valid_q                      <= 1'b0;
        ram_wr_en_q                         <= 1'b0;
        ram_wr_bank_q                       <= 1'b0;
        ram_wr_addr_q                       <= '0;
        ram_wr_op_q                         <= '0;
        ram_wr_final_q                      <= 1'b0;
        bank_ready_q                        <= 2'd0;
        bank0_sequence_q                    <= 16'd0;
        bank1_sequence_q                    <= 16'd0;
        bank0_op_count_q                    <= 16'd0;
        bank1_op_count_q                    <= 16'd0;
        package_ready_pulse_q               <= 1'b0;
        package_ready_bank_q                <= 1'b0;
        package_ready_sequence_q            <= 16'd0;
        package_ready_op_count_q            <= 16'd0;
        header_q                            <= '0;
    end else begin
        header_parsed_pulse_q <= 1'b0;
        ram_wr_en_q           <= 1'b0;
        ram_wr_final_q        <= 1'b0;
        package_ready_pulse_q <= 1'b0;

        if (bank_consume_i) begin
            bank_ready_q[bank_consume_select_i] <= 1'b0;
        end

        if (ram_wr_en_q && ram_wr_final_q) begin
            package_ready_pulse_q    <= 1'b1;
            package_ready_bank_q     <= ram_wr_bank_q;
            package_ready_sequence_q <= header_q.event_sequence;
            package_ready_op_count_q <= header_q.total_operation_count;

            if (ram_wr_bank_q) begin
                bank_ready_q[1]   <= 1'b1;
                bank1_sequence_q  <= header_q.event_sequence;
                bank1_op_count_q  <= header_q.total_operation_count;
            end else begin
                bank_ready_q[0]   <= 1'b1;
                bank0_sequence_q  <= header_q.event_sequence;
                bank0_op_count_q  <= header_q.total_operation_count;
            end
        end

        case (state_q)
            ST_WAIT_HEADER: begin
                if (axis_valid) begin
                    beat_data_q     <= s_axis_tdata_i;
                    beat_keep_q     <= s_axis_tkeep_i;
                    beat_last_q     <= s_axis_tlast_i;
                    active_bank_q   <= bank_ready_q[0];
                    next_op_index_q <= 16'd0;
                    state_q         <= ST_PARSE_HEADER;
                end
            end

            ST_PARSE_HEADER: begin
                header_q.magic                    <= beat_data_q[0 +: 32];
                header_q.version                  <= beat_data_q[32 +: 8];
                header_q.header_bytes             <= beat_data_q[40 +: 8];
                header_q.flags                    <= beat_data_q[48 +: 16];
                header_q.event_sequence           <= beat_data_q[64 +: 16];
                header_q.fragment_index           <= beat_data_q[80 +: 16];
                header_q.fragment_count           <= beat_data_q[96 +: 16];
                header_q.record_bytes             <= beat_data_q[112 +: 16];
                header_q.fragment_operation_count <= beat_data_q[128 +: 16];
                header_q.total_operation_count    <= beat_data_q[144 +: 16];
                header_q.fragment_payload_bytes   <= beat_data_q[160 +: 16];
                header_q.reserved0                <= beat_data_q[176 +: 16];
                header_q.total_payload_bytes      <= beat_data_q[192 +: 32];
                header_q.reserved1                <= beat_data_q[224 +: 32];
                header_parsed_pulse_q             <= 1'b1;
                state_q                           <= ST_CHECK_HEADER;
            end

            ST_CHECK_HEADER: begin
                header_valid_q <= header_check_valid;
                if (header_check_valid) begin
                    state_q <= ST_WRITE_HEADER_OP;
                end else begin
                    state_q <= ST_WAIT_HEADER;
                end
            end

            ST_WRITE_HEADER_OP: begin
                ram_wr_en_q             <= 1'b1;
                ram_wr_bank_q           <= active_bank_q;
                ram_wr_addr_q           <= '0;
                ram_wr_op_q.tx1_sdata0  <= beat_data_q[256 +: 48];
                ram_wr_op_q.tx1_sdata1  <= beat_data_q[304 +: 48];
                ram_wr_op_q.tx2_sdata0  <= beat_data_q[352 +: 48];
                ram_wr_op_q.tx2_sdata1  <= beat_data_q[400 +: 48];
                ram_wr_op_q.clock_count <= beat_data_q[448 +: 8];
                ram_wr_op_q.flags       <= beat_data_q[456 +: 8];
                ram_wr_op_q.reserved0   <= beat_data_q[464 +: 16];
                ram_wr_op_q.reserved1   <= beat_data_q[480 +: 32];
                ram_wr_final_q          <= (header_q.total_operation_count <= 16'd1);
                if (header_q.total_operation_count <= 16'd1) begin
                    state_q <= ST_WAIT_HEADER;
                end else begin
                    next_op_index_q <= 16'd1;
                    state_q         <= ST_WAIT_DATA;
                end
            end

            ST_WAIT_DATA: begin
                if (axis_valid) begin
                    beat_data_q <= s_axis_tdata_i;
                    beat_keep_q <= s_axis_tkeep_i;
                    beat_last_q <= s_axis_tlast_i;
                    state_q     <= ST_WRITE_SLOT0;
                end
            end

            ST_WRITE_SLOT0: begin
                ram_wr_en_q             <= (next_op_index_q < header_q.total_operation_count);
                ram_wr_bank_q           <= active_bank_q;
                ram_wr_addr_q           <= next_op_index_q[OP_INDEX_W-1:0];
                ram_wr_op_q.tx1_sdata0  <= beat_data_q[0 +: 48];
                ram_wr_op_q.tx1_sdata1  <= beat_data_q[48 +: 48];
                ram_wr_op_q.tx2_sdata0  <= beat_data_q[96 +: 48];
                ram_wr_op_q.tx2_sdata1  <= beat_data_q[144 +: 48];
                ram_wr_op_q.clock_count <= beat_data_q[192 +: 8];
                ram_wr_op_q.flags       <= beat_data_q[200 +: 8];
                ram_wr_op_q.reserved0   <= beat_data_q[208 +: 16];
                ram_wr_op_q.reserved1   <= beat_data_q[224 +: 32];
                ram_wr_final_q          <= ((next_op_index_q + 16'd1) >= header_q.total_operation_count);
                if ((next_op_index_q + 16'd1) >= header_q.total_operation_count) begin
                    next_op_index_q <= next_op_index_q + 16'd1;
                    state_q         <= ST_WAIT_HEADER;
                end else begin
                    next_op_index_q <= next_op_index_q + 16'd1;
                    state_q         <= ST_WRITE_SLOT1;
                end
            end

            ST_WRITE_SLOT1: begin
                ram_wr_en_q             <= (next_op_index_q < header_q.total_operation_count);
                ram_wr_bank_q           <= active_bank_q;
                ram_wr_addr_q           <= next_op_index_q[OP_INDEX_W-1:0];
                ram_wr_op_q.tx1_sdata0  <= beat_data_q[256 +: 48];
                ram_wr_op_q.tx1_sdata1  <= beat_data_q[304 +: 48];
                ram_wr_op_q.tx2_sdata0  <= beat_data_q[352 +: 48];
                ram_wr_op_q.tx2_sdata1  <= beat_data_q[400 +: 48];
                ram_wr_op_q.clock_count <= beat_data_q[448 +: 8];
                ram_wr_op_q.flags       <= beat_data_q[456 +: 8];
                ram_wr_op_q.reserved0   <= beat_data_q[464 +: 16];
                ram_wr_op_q.reserved1   <= beat_data_q[480 +: 32];
                ram_wr_final_q          <= ((next_op_index_q + 16'd1) >= header_q.total_operation_count);
                if ((next_op_index_q + 16'd1) >= header_q.total_operation_count) begin
                    next_op_index_q <= next_op_index_q + 16'd1;
                    state_q         <= ST_WAIT_HEADER;
                end else begin
                    next_op_index_q <= next_op_index_q + 16'd1;
                    state_q         <= ST_WAIT_DATA;
                end
            end

            default: begin
                state_q <= ST_WAIT_HEADER;
            end
        endcase
    end
end

always_ff @(posedge clk) begin
    if (rst) begin
        error_pulse_q             <= 1'b0;
        last_error_code_q         <= TX_HS_SPI_PKG_ERR_NONE;
        accepted_package_count_q  <= 32'd0;
        accepted_fragment_count_q <= 32'd0;
        error_count_q             <= 32'd0;
    end else begin
        error_pulse_q <= 1'b0;

        if ((state_q == ST_CHECK_HEADER) && !header_check_valid) begin
            error_pulse_q     <= 1'b1;
            last_error_code_q <= header_error_code;
            error_count_q     <= error_count_q + 1'b1;
        end

        if (package_accept_pulse) begin
            accepted_package_count_q  <= accepted_package_count_q + 1'b1;
            accepted_fragment_count_q <= accepted_fragment_count_q + 1'b1;
        end
    end
end

endmodule
