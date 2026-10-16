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

module tx_hs_spi_top
  import apb_pkg::*;
  import tx_hs_spi_pkg::*;
#(
    parameter int MAX_OPS_PER_BANK = TX_HS_SPI_DEFAULT_MAX_OPS_PER_BANK,
    parameter int OP_INDEX_W       = (MAX_OPS_PER_BANK <= 1) ? 1 : $clog2(MAX_OPS_PER_BANK)
)(
    input  logic   apb_clk,
    input  logic   apb_rst,
    input  apb_m2s                 i_apb_m2s,
    output apb_s2m                 o_apb_s2m,

    input  logic                   tx_spi_clk,
    input  logic                   tx_spi_rst,

    input  logic                   s_axis_tvalid_i,
    output logic                   s_axis_tready_o,
    input  logic [511:0]           s_axis_tdata_i,
    input  logic [63:0]            s_axis_tkeep_i,
    input  logic                   s_axis_tlast_i,

    output logic [1:0]             package_bank_ready_o,
    output logic [15:0]            package_bank0_sequence_o,
    output logic [15:0]            package_bank1_sequence_o,
    output logic [15:0]            package_bank0_op_count_o,
    output logic [15:0]            package_bank1_op_count_o,
    output logic                   package_ready_pulse_o,
    output logic                   package_ready_bank_o,
    output logic [15:0]            package_ready_sequence_o,
    output logic [15:0]            package_ready_op_count_o,
    output logic                   package_error_pulse_o,
    output logic [7:0]             package_last_error_code_o,
    output logic [31:0]            package_accepted_package_count_o,
    output logic [31:0]            package_accepted_fragment_count_o,
    output logic [31:0]            package_error_count_o,

    input  logic                   tx_update_expected_sequence_valid_i,
    input  logic [15:0]            tx_update_expected_sequence_i,
    input  logic                   tx_update_event_retired_i,
    input  logic                   tx_update_release_i,
    output logic                   tx_update_ready_o,

    input  logic                   tx_sdout_i,
    output logic   tx_sclk_o,
    output logic   tx_sen_o,
    output logic   tx1_sdata0_o,
    output logic   tx1_sdata1_o,
    output logic   tx2_sdata0_o,
    output logic   tx2_sdata1_o
);

logic          slow_op_valid;
logic          slow_op_ready;
tx_hs_spi_op_t slow_op;
logic [15:0]   half_period_cycles;
logic          phy_busy;
logic          phy_done;
logic          phy_fault;
logic [7:0]    phy_rx_data;
logic          phy_rx_valid;
logic          sched_op_valid;
logic          sched_op_ready;
tx_hs_spi_op_t sched_op;
logic          sched_busy;
logic [15:0]   sched_active_op_count;
logic [15:0]   sched_issued_op_count;
logic          sched_done_pulse;
logic          sched_fault_pulse;
logic [7:0]    sched_last_error_code;
logic [15:0]   sched_fault_op_index;
logic [31:0]   sched_fault_count;
logic          package_bank_consume;
logic          package_bank_consume_select;
logic          package_bank_rd_select;
logic [OP_INDEX_W-1:0] package_bank_rd_index;
tx_hs_spi_op_t package_bank_rd_op;
logic [1:0]    package_bank_ready;
logic [15:0]   package_bank0_sequence;
logic [15:0]   package_bank1_sequence;
logic [15:0]   package_bank0_op_count;
logic [15:0]   package_bank1_op_count;
logic          sched_start_q;
logic          sched_bank_select_q;
logic          matching_bank_valid;
logic          matching_bank_select;
logic [15:0]   matching_bank_sequence;
logic [15:0]   package_expected_sequence_q;
logic          execute_credit_q;

typedef enum logic [1:0] {
    TX_UPDATE_IDLE,
    TX_UPDATE_EXECUTING,
    TX_UPDATE_MISSED,
    TX_UPDATE_READY
} tx_update_state_e;

tx_update_state_e tx_update_state_q;
logic [15:0]      active_update_sequence_q;
logic [15:0]      tx_update_ready_sequence_q;
logic             tx_update_late_pulse_q /* synthesis preserve noprune */;
logic [31:0]      tx_update_late_count_q /* synthesis preserve noprune */;

assign package_bank_ready_o     = package_bank_ready;
assign package_bank0_sequence_o = package_bank0_sequence;
assign package_bank1_sequence_o = package_bank1_sequence;
assign package_bank0_op_count_o = package_bank0_op_count;
assign package_bank1_op_count_o = package_bank1_op_count;
assign tx_update_ready_o        = (tx_update_state_q == TX_UPDATE_READY);
always_comb begin
    matching_bank_valid    = 1'b0;
    matching_bank_select   = 1'b0;
    matching_bank_sequence = 16'd0;

    if (package_bank_ready[0] &&
        (!tx_update_expected_sequence_valid_i ||
         (package_bank0_sequence == tx_update_expected_sequence_i))) begin
        matching_bank_valid    = 1'b1;
        matching_bank_select   = 1'b0;
        matching_bank_sequence = package_bank0_sequence;
    end else if (package_bank_ready[1] &&
                 (!tx_update_expected_sequence_valid_i ||
                  (package_bank1_sequence == tx_update_expected_sequence_i))) begin
        matching_bank_valid    = 1'b1;
        matching_bank_select   = 1'b1;
        matching_bank_sequence = package_bank1_sequence;
    end
end

// Slow APB traffic keeps priority so register readback remains deterministic.
// NOTE: phy_start/phy_op are combinational (not registered, despite the
// project's usual "_q" = flopped convention) so tx_hs_spi_phy.launch can
// fire the same cycle either source asserts valid.
logic          phy_start;
tx_hs_spi_op_t phy_op;

assign slow_op_ready  = !phy_busy;
assign sched_op_ready = !phy_busy && !slow_op_valid;
assign phy_start      = (slow_op_valid && slow_op_ready) ||
                        (sched_op_valid && sched_op_ready);

always_comb begin
    phy_op = sched_op;
    if (slow_op_valid) begin
        phy_op = slow_op;
    end
end

always_ff @(posedge tx_spi_clk) begin
    if (tx_spi_rst) begin
        tx_update_state_q          <= TX_UPDATE_IDLE;
        active_update_sequence_q   <= 16'd0;
        tx_update_ready_sequence_q <= 16'd0;
        tx_update_late_pulse_q     <= 1'b0;
        tx_update_late_count_q     <= 32'd0;
        package_expected_sequence_q <= 16'd0;
        sched_start_q              <= 1'b0;
        sched_bank_select_q        <= 1'b0;
        execute_credit_q           <= 1'b1;
    end else begin
        tx_update_late_pulse_q <= 1'b0;
        sched_start_q          <= 1'b0;

        if (package_ready_pulse_o) begin
            package_expected_sequence_q <= package_expected_sequence_q + 16'd1;
        end

        if (tx_update_release_i) begin
            execute_credit_q <= 1'b1;
        end

        case (tx_update_state_q)
            TX_UPDATE_IDLE: begin
                if (execute_credit_q && matching_bank_valid && !sched_busy) begin
                    sched_start_q            <= 1'b1;
                    sched_bank_select_q      <= matching_bank_select;
                    active_update_sequence_q <= matching_bank_sequence;
                    execute_credit_q         <= 1'b0;
                    tx_update_state_q        <= TX_UPDATE_EXECUTING;
                end
            end

            TX_UPDATE_EXECUTING: begin
                if (sched_fault_pulse) begin
                    execute_credit_q    <= 1'b1;
                    tx_update_state_q   <= TX_UPDATE_IDLE;
                end else if (tx_update_event_retired_i) begin
                    tx_update_late_pulse_q <= 1'b1;
                    tx_update_late_count_q <= tx_update_late_count_q + 1'b1;
                    tx_update_state_q      <= TX_UPDATE_MISSED;
                end else if (sched_done_pulse) begin
                    if (!tx_update_expected_sequence_valid_i ||
                        (active_update_sequence_q == tx_update_expected_sequence_i)) begin
                        tx_update_ready_sequence_q <= active_update_sequence_q;
                        tx_update_state_q          <= TX_UPDATE_READY;
                    end else begin
                        execute_credit_q  <= 1'b1;
                        tx_update_state_q <= TX_UPDATE_IDLE;
                    end
                end
            end

            TX_UPDATE_MISSED: begin
                if (sched_fault_pulse || sched_done_pulse) begin
                    tx_update_state_q <= TX_UPDATE_IDLE;
                end
            end

            TX_UPDATE_READY: begin
                if (tx_update_event_retired_i) begin
                    tx_update_state_q <= TX_UPDATE_IDLE;
                end
            end

            default: begin
                tx_update_state_q <= TX_UPDATE_IDLE;
            end
        endcase
    end
end

tx_hs_spi_package_rx #(
    .MAX_OPS_PER_BANK (MAX_OPS_PER_BANK),
    .OP_INDEX_W       (OP_INDEX_W)
) u_package_rx (
    .clk                         (tx_spi_clk),
    .rst                         (tx_spi_rst),
    .s_axis_tvalid_i             (s_axis_tvalid_i),
    .s_axis_tready_o             (s_axis_tready_o),
    .s_axis_tdata_i              (s_axis_tdata_i),
    .s_axis_tkeep_i              (s_axis_tkeep_i),
    .s_axis_tlast_i              (s_axis_tlast_i),
    .expected_sequence_valid_i   (tx_update_expected_sequence_valid_i),
    .expected_sequence_i         (package_expected_sequence_q),
    .bank_consume_i              (package_bank_consume),
    .bank_consume_select_i       (package_bank_consume_select),
    .bank_rd_select_i            (package_bank_rd_select),
    .bank_rd_index_i             (package_bank_rd_index),
    .bank_rd_op_o                (package_bank_rd_op),
    .bank_ready_o                (package_bank_ready),
    .bank0_sequence_o            (package_bank0_sequence),
    .bank1_sequence_o            (package_bank1_sequence),
    .bank0_op_count_o            (package_bank0_op_count),
    .bank1_op_count_o            (package_bank1_op_count),
    .package_ready_pulse_o       (package_ready_pulse_o),
    .package_ready_bank_o        (package_ready_bank_o),
    .package_ready_sequence_o    (package_ready_sequence_o),
    .package_ready_op_count_o    (package_ready_op_count_o),
    .error_pulse_o               (package_error_pulse_o),
    .last_error_code_o           (package_last_error_code_o),
    .accepted_package_count_o    (package_accepted_package_count_o),
    .accepted_fragment_count_o   (package_accepted_fragment_count_o),
    .error_count_o               (package_error_count_o)
);

tx_hs_spi_slow_ctrl u_slow_ctrl (
    .apb_clk              (apb_clk),
    .apb_rst              (apb_rst),
    .i_apb_m2s            (i_apb_m2s),
    .o_apb_s2m            (o_apb_s2m),
    .tx_spi_clk           (tx_spi_clk),
    .tx_spi_rst           (tx_spi_rst),
    .op_valid_o           (slow_op_valid),
    .op_ready_i           (slow_op_ready),
    .op_o                 (slow_op),
    .half_period_cycles_o (half_period_cycles),
    .op_done_i            (phy_done),
    .op_fault_i           (phy_fault),
    .op_rx_data_i         (phy_rx_data),
    .op_rx_valid_i        (phy_rx_valid)
);

tx_hs_spi_scheduler #(
    .MAX_OPS_PER_BANK (MAX_OPS_PER_BANK),
    .OP_INDEX_W       (OP_INDEX_W)
) u_scheduler (
    .clk                  (tx_spi_clk),
    .rst                  (tx_spi_rst),
    .start_i              (sched_start_q),
    .bank_select_i        (sched_bank_select_q),
    .bank_ready_i         (package_bank_ready),
    .bank0_op_count_i     (package_bank0_op_count),
    .bank1_op_count_i     (package_bank1_op_count),
    .bank_consume_o       (package_bank_consume),
    .bank_consume_select_o(package_bank_consume_select),
    .bank_rd_select_o     (package_bank_rd_select),
    .bank_rd_index_o      (package_bank_rd_index),
    .bank_rd_op_i         (package_bank_rd_op),
    .op_valid_o           (sched_op_valid),
    .op_ready_i           (sched_op_ready),
    .op_o                 (sched_op),
    .op_done_i            (phy_done),
    .op_fault_i           (phy_fault),
    .busy_o               (sched_busy),
    .active_op_count_o    (sched_active_op_count),
    .issued_op_count_o    (sched_issued_op_count),
    .done_pulse_o         (sched_done_pulse),
    .fault_pulse_o        (sched_fault_pulse),
    .last_error_code_o    (sched_last_error_code),
    .fault_op_index_o     (sched_fault_op_index),
    .fault_count_o        (sched_fault_count)
);

tx_hs_spi_phy u_phy (
    .clk                  (tx_spi_clk),
    .rst                  (tx_spi_rst),
    .start_i              (phy_start),
    .op_i                 (phy_op),
    .half_period_cycles_i (half_period_cycles),
    .tx_sdout_i           (tx_sdout_i),
    .busy_o               (phy_busy),
    .done_o               (phy_done),
    .fault_o              (phy_fault),
    .rx_data_o            (phy_rx_data),
    .rx_valid_o           (phy_rx_valid),
    .tx_sclk_o            (tx_sclk_o),
    .tx_sen_o             (tx_sen_o),
    .tx1_sdata0_o         (tx1_sdata0_o),
    .tx1_sdata1_o         (tx1_sdata1_o),
    .tx2_sdata0_o         (tx2_sdata0_o),
    .tx2_sdata1_o         (tx2_sdata1_o)
);

endmodule
