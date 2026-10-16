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

`ifndef TX_HS_SPI_PKG_SV
`define TX_HS_SPI_PKG_SV

package tx_hs_spi_pkg;

    localparam logic [31:0] TX_HS_SPI_MAGIC              = 32'h44585455; // "DXTU", final value TBD.
    localparam logic [7:0]  TX_HS_SPI_PROTOCOL_VERSION   = 8'd1;
    localparam int TX_HS_SPI_FRAGMENT_HDR_BYTES = 32;
    localparam int TX_HS_SPI_OP_BYTES           = 32;
    localparam int TX_HS_SPI_OP_BITS            = TX_HS_SPI_OP_BYTES * 8;
    localparam int TX_HS_SPI_LANE_PAYLOAD_BITS  = 48;
    localparam int TX_HS_SPI_MAX_CLOCK_COUNT    = 48;

    localparam int TX_HS_SPI_AXIS_DATA_BITS = 512;
    localparam int TX_HS_SPI_AXIS_KEEP_BITS = TX_HS_SPI_AXIS_DATA_BITS / 8;

    localparam int TX_HS_SPI_NORMAL_WRITE_BITS  = 44;
    localparam int TX_HS_SPI_BURST_DATA_BITS    = 32;

    localparam int TX_HS_SPI_OP_KEEP_SEN_LOW      = 0;
    localparam int TX_HS_SPI_OP_TX1_SDATA1_ENABLE = 1;
    localparam int TX_HS_SPI_OP_TX2_SDATA1_ENABLE = 2;
    localparam int TX_HS_SPI_OP_CAPTURE_SDOUT     = 3;
    localparam int TX_HS_SPI_OP_DISABLE_SDATA     = 4;

    localparam int TX_HS_SPI_BANK_COUNT                   = 2;
    localparam int TX_HS_SPI_DEFAULT_MAX_OPS_PER_BANK     = 128;
    localparam int TX_HS_SPI_DEFAULT_MAX_FRAGMENT_PAYLOAD  = 4096;

    localparam logic [7:0] TX_HS_SPI_PKG_ERR_NONE                = 8'h00;
    localparam logic [7:0] TX_HS_SPI_PKG_ERR_BAD_TKEEP           = 8'h01;
    localparam logic [7:0] TX_HS_SPI_PKG_ERR_BAD_HEADER          = 8'h02;
    localparam logic [7:0] TX_HS_SPI_PKG_ERR_UNEXPECTED_FRAGMENT = 8'h03;
    localparam logic [7:0] TX_HS_SPI_PKG_ERR_NO_FREE_BANK        = 8'h04;
    localparam logic [7:0] TX_HS_SPI_PKG_ERR_TOO_MANY_OPS        = 8'h05;
    localparam logic [7:0] TX_HS_SPI_PKG_ERR_BAD_PAYLOAD         = 8'h06;
    localparam logic [7:0] TX_HS_SPI_PKG_ERR_BAD_OPERATION       = 8'h07;
    localparam logic [7:0] TX_HS_SPI_PKG_ERR_TLAST_MISMATCH      = 8'h08;
    localparam logic [7:0] TX_HS_SPI_PKG_ERR_BAD_SEQUENCE        = 8'h09;

    localparam logic [7:0] TX_HS_SPI_SCHED_ERR_NONE              = 8'h00;
    localparam logic [7:0] TX_HS_SPI_SCHED_ERR_PHY_FAULT         = 8'h01;

    // APB maintenance register map for the TI TX73L64-specific slow path.
    // Fast package execution will consume tx_hs_spi_op_t records directly.
    localparam logic [11:0] TX_HS_SPI_REG_CTRL      = 12'h000;
    localparam logic [11:0] TX_HS_SPI_REG_CFG       = 12'h004;
    localparam logic [11:0] TX_HS_SPI_REG_TX1_ADDR  = 12'h008;
    localparam logic [11:0] TX_HS_SPI_REG_TX1_DATA  = 12'h00c;
    localparam logic [11:0] TX_HS_SPI_REG_TX2_ADDR  = 12'h010;
    localparam logic [11:0] TX_HS_SPI_REG_TX2_DATA  = 12'h014;
    localparam logic [11:0] TX_HS_SPI_REG_STATUS    = 12'h080;
    localparam logic [11:0] TX_HS_SPI_REG_READ_DATA = 12'h084;

    localparam logic [1:0] TX_HS_SPI_TARGET_BOTH = 2'd0;
    localparam logic [1:0] TX_HS_SPI_TARGET_TX1  = 2'd1;
    localparam logic [1:0] TX_HS_SPI_TARGET_TX2  = 2'd2;

    localparam logic [1:0] TX_HS_SPI_CTRL_OP_WRITE = 2'd0;
    localparam logic [1:0] TX_HS_SPI_CTRL_OP_READ  = 2'd1;

    localparam int TX_HS_SPI_CTRL_START_LSB       = 0;
    localparam int TX_HS_SPI_CTRL_OP_LSB          = 1;
    localparam int TX_HS_SPI_CTRL_WRITE_MASK_LSB  = 4;
    localparam int TX_HS_SPI_CTRL_READ_TARGET_LSB = 8;

    typedef enum logic [1:0] {
        TX_HS_SPI_LANES_1 = 2'd0,
        TX_HS_SPI_LANES_2 = 2'd1
    } tx_hs_spi_lane_mode_e;

    typedef enum logic [1:0] {
        TX_HS_SPI_SUPPRESS_TX_ONLY    = 2'd0,
        TX_HS_SPI_SUPPRESS_WHOLE_SHOT = 2'd1
    } tx_hs_spi_suppress_policy_e;

    typedef struct packed {
        logic [47:0] tx1_sdata0;
        logic [47:0] tx1_sdata1;
        logic [47:0] tx2_sdata0;
        logic [47:0] tx2_sdata1;
        logic [7:0]  clock_count;
        logic [7:0]  flags;
        logic [15:0] reserved0;
        logic [31:0] reserved1;
    } tx_hs_spi_op_t;

    typedef struct packed {
        logic [31:0] magic;
        logic [7:0]  version;
        logic [7:0]  header_bytes;
        logic [15:0] flags;
        logic [15:0] event_sequence;
        logic [15:0] fragment_index;
        logic [15:0] fragment_count;
        logic [15:0] record_bytes;
        logic [15:0] fragment_operation_count;
        logic [15:0] total_operation_count;
        logic [15:0] fragment_payload_bytes;
        logic [15:0] reserved0;
        logic [31:0] total_payload_bytes;
        logic [31:0] reserved1;
    } tx_hs_spi_fragment_header_t;

endpackage

`endif
