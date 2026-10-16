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

module tx_hs_spi_bank_ram
  import tx_hs_spi_pkg::*;
#(
    parameter int    DEPTH    = TX_HS_SPI_DEFAULT_MAX_OPS_PER_BANK,
    parameter int    ADDR_W   = (DEPTH <= 1) ? 1 : $clog2(DEPTH),
    parameter string RAMSTYLE = "M20K"
)(
    input  logic                  clk,

    input  logic                  wr_en_i,
    input  logic [ADDR_W-1:0]     wr_addr_i,
    input  var tx_hs_spi_op_t     wr_op_i,

    input  logic [ADDR_W-1:0]     rd_addr_i,
    output tx_hs_spi_op_t         rd_op_o
);

logic [TX_HS_SPI_OP_BITS-1:0] wr_data;
logic [TX_HS_SPI_OP_BITS-1:0] rd_data_q;

(* ramstyle = RAMSTYLE *) logic [TX_HS_SPI_OP_BITS-1:0] op_mem [0:DEPTH-1];

assign wr_data = wr_op_i;
assign rd_op_o = rd_data_q;

always_ff @(posedge clk) begin
    if (wr_en_i) begin
        op_mem[wr_addr_i] <= wr_data;
    end

    rd_data_q <= op_mem[rd_addr_i];
end

endmodule
