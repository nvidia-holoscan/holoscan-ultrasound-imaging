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

module lvds_group_assembler #(
    parameter int AFE_LANES          = 16,
    parameter int SAMPLE_W           = 12,
    parameter int AFE1_GROUP_LANES   = 15,
    parameter int AFE2_GROUP_LANES   = 11,
    parameter int MIXED_GROUP_LANES  = 6,
    parameter int SEQ_W              = 16,
    parameter int FIFO_DEPTH         = 32,
    parameter int FIFO_ALMOST_FULL   = FIFO_DEPTH-4,
    parameter int FIFO_ALMOST_EMPTY  = 2,
    parameter     FIFO_MEM_STYLE     = "BLOCK",
    localparam int FIFO_COUNT_W      = $clog2(FIFO_DEPTH)+1,
    localparam int AFE1_GROUP_DATA_W = AFE1_GROUP_LANES*SAMPLE_W,
    localparam int AFE2_GROUP_DATA_W = AFE2_GROUP_LANES*SAMPLE_W,
    localparam int MIXED_GROUP_DATA_W = MIXED_GROUP_LANES*SAMPLE_W,
    localparam int AFE_DATA_W        = AFE_LANES*SAMPLE_W,
    localparam int AFE1_FIFO_W       = AFE1_GROUP_DATA_W+SEQ_W+1,
    localparam int AFE2_FIFO_W       = AFE2_GROUP_DATA_W+SEQ_W+1,
    localparam int MIXED_FIFO_W      = MIXED_GROUP_DATA_W+SEQ_W+1
) (
    input  logic                          afe1_clk,
    input  logic                          afe1_rst,
    input  logic [AFE1_GROUP_DATA_W-1:0]  afe1_group_data,
    input  logic                          afe1_group_valid,

    input  logic                          afe2_clk,
    input  logic                          afe2_rst,
    input  logic [AFE2_GROUP_DATA_W-1:0]  afe2_group_data,
    input  logic                          afe2_group_valid,

    input  logic                          mixed_clk,
    input  logic                          mixed_rst,
    input  logic [MIXED_GROUP_DATA_W-1:0] mixed_group_data,
    input  logic                          mixed_group_valid,

    input  logic                          out_clk,
    input  logic                          out_rst,
    input  logic                          cfg_enable,
    input  logic                          cfg_clear,

    output logic [AFE_DATA_W-1:0]         afe1_data,
    output logic                          afe1_valid,
    output logic [AFE_DATA_W-1:0]         afe2_data,
    output logic                          afe2_valid,
    output logic                          sample_phase,
    output logic [SEQ_W-1:0]              sample_seq,

    output logic [31:0]                   status,
    output logic [31:0]                   error_status
);

localparam int AFE1_SEQ_LSB  = AFE1_GROUP_DATA_W;
localparam int AFE1_PHASE_MSB = AFE1_FIFO_W-1;
localparam int AFE2_SEQ_LSB  = AFE2_GROUP_DATA_W;
localparam int AFE2_PHASE_MSB = AFE2_FIFO_W-1;
localparam int MIXED_SEQ_LSB = MIXED_GROUP_DATA_W;
localparam int MIXED_PHASE_MSB = MIXED_FIFO_W-1;

logic [1:0] cfg_afe1_sync;
logic [1:0] cfg_afe2_sync;
logic [1:0] cfg_mixed_sync;
logic [1:0] cfg_out_sync;
logic       afe1_enable;
logic       afe2_enable;
logic       mixed_enable;
logic       out_enable;
logic       afe1_clear;
logic       afe2_clear;
logic       mixed_clear;
logic       out_clear;

logic                       afe1_wr_phase_q;
logic                       afe2_wr_phase_q;
logic                       mixed_wr_phase_q;
logic [SEQ_W-1:0]           afe1_wr_seq_q;
logic [SEQ_W-1:0]           afe2_wr_seq_q;
logic [SEQ_W-1:0]           mixed_wr_seq_q;
logic [AFE1_FIFO_W-1:0]     afe1_fifo_din;
logic [AFE2_FIFO_W-1:0]     afe2_fifo_din;
logic [MIXED_FIFO_W-1:0]    mixed_fifo_din;
logic [AFE1_FIFO_W-1:0]     afe1_fifo_dout;
logic [AFE2_FIFO_W-1:0]     afe2_fifo_dout;
logic [MIXED_FIFO_W-1:0]    mixed_fifo_dout;
logic                       afe1_fifo_wren;
logic                       afe2_fifo_wren;
logic                       mixed_fifo_wren;
logic                       afe1_fifo_full;
logic                       afe2_fifo_full;
logic                       mixed_fifo_full;
logic                       afe1_fifo_afull;
logic                       afe2_fifo_afull;
logic                       mixed_fifo_afull;
logic                       afe1_fifo_empty;
logic                       afe2_fifo_empty;
logic                       mixed_fifo_empty;
logic                       afe1_fifo_aempty;
logic                       afe2_fifo_aempty;
logic                       mixed_fifo_aempty;
logic                       afe1_fifo_over;
logic                       afe2_fifo_over;
logic                       mixed_fifo_over;
logic                       afe1_over_sticky_q;
logic                       afe2_over_sticky_q;
logic                       mixed_over_sticky_q;
logic [2:0]                 fifo_over_sticky_sync;
logic                       afe1_fifo_under;
logic                       afe2_fifo_under;
logic                       mixed_fifo_under;
logic                       afe1_fifo_rdval;
logic                       afe2_fifo_rdval;
logic                       mixed_fifo_rdval;
logic                       fifo_rden;
logic [FIFO_COUNT_W-1:0]    afe1_fifo_wrcount;
logic [FIFO_COUNT_W-1:0]    afe2_fifo_wrcount;
logic [FIFO_COUNT_W-1:0]    mixed_fifo_wrcount;
logic [FIFO_COUNT_W-1:0]    afe1_fifo_rdcount;
logic [FIFO_COUNT_W-1:0]    afe2_fifo_rdcount;
logic [FIFO_COUNT_W-1:0]    mixed_fifo_rdcount;

logic [AFE1_GROUP_DATA_W-1:0]  afe1_fifo_data;
logic [AFE2_GROUP_DATA_W-1:0]  afe2_fifo_data;
logic [MIXED_GROUP_DATA_W-1:0] mixed_fifo_data;
logic [SEQ_W-1:0]              afe1_fifo_seq;
logic [SEQ_W-1:0]              afe2_fifo_seq;
logic [SEQ_W-1:0]              mixed_fifo_seq;
logic                          afe1_fifo_phase;
logic                          afe2_fifo_phase;
logic                          mixed_fifo_phase;
logic                          fifo_rdval_all;
logic                          phase_match;
logic                          seq_match;
logic [AFE_DATA_W-1:0]         afe1_data_next;
logic [AFE_DATA_W-1:0]         afe2_data_next;
logic [31:0]                   error_status_q;

data_sync #(
    .DATA_WIDTH  (2),
    .RESET_VALUE (1'b0),
    .SYNC_DEPTH  (2),
    .RST_SYNC    ("FALSE")
) u_cfg_afe1_sync (
    .clk      (afe1_clk),
`ifdef HSB_2P7_REL
    .rst_n    (~afe1_rst),
`else
    .rst      (afe1_rst),
`endif
    .sync_in  ({cfg_clear, cfg_enable}),
    .sync_out (cfg_afe1_sync)
);

data_sync #(
    .DATA_WIDTH  (2),
    .RESET_VALUE (1'b0),
    .SYNC_DEPTH  (2),
    .RST_SYNC    ("FALSE")
) u_cfg_afe2_sync (
    .clk      (afe2_clk),
`ifdef HSB_2P7_REL
    .rst_n    (~afe2_rst),
`else
    .rst      (afe2_rst),
`endif
    .sync_in  ({cfg_clear, cfg_enable}),
    .sync_out (cfg_afe2_sync)
);

data_sync #(
    .DATA_WIDTH  (2),
    .RESET_VALUE (1'b0),
    .SYNC_DEPTH  (2),
    .RST_SYNC    ("FALSE")
) u_cfg_mixed_sync (
    .clk      (mixed_clk),
`ifdef HSB_2P7_REL
    .rst_n    (~mixed_rst),
`else
    .rst      (mixed_rst),
`endif
    .sync_in  ({cfg_clear, cfg_enable}),
    .sync_out (cfg_mixed_sync)
);

data_sync #(
    .DATA_WIDTH  (2),
    .RESET_VALUE (1'b0),
    .SYNC_DEPTH  (2),
    .RST_SYNC    ("FALSE")
) u_cfg_out_sync (
    .clk      (out_clk),
`ifdef HSB_2P7_REL
    .rst_n    (~out_rst),
`else
    .rst      (out_rst),
`endif
    .sync_in  ({cfg_clear, cfg_enable}),
    .sync_out (cfg_out_sync)
);

assign afe1_enable  = cfg_afe1_sync[0];
assign afe1_clear   = cfg_afe1_sync[1];
assign afe2_enable  = cfg_afe2_sync[0];
assign afe2_clear   = cfg_afe2_sync[1];
assign mixed_enable = cfg_mixed_sync[0];
assign mixed_clear  = cfg_mixed_sync[1];
assign out_enable   = cfg_out_sync[0];
assign out_clear    = cfg_out_sync[1];

assign afe1_fifo_din   = {afe1_wr_phase_q, afe1_wr_seq_q, afe1_group_data};
assign afe2_fifo_din   = {afe2_wr_phase_q, afe2_wr_seq_q, afe2_group_data};
assign mixed_fifo_din  = {mixed_wr_phase_q, mixed_wr_seq_q, mixed_group_data};
assign afe1_fifo_wren  = afe1_enable & afe1_group_valid & !afe1_fifo_full;
assign afe2_fifo_wren  = afe2_enable & afe2_group_valid & !afe2_fifo_full;
assign mixed_fifo_wren = mixed_enable & mixed_group_valid & !mixed_fifo_full;

always_ff @(posedge afe1_clk) begin
    if (afe1_rst || afe1_clear || !afe1_enable) begin
        afe1_wr_phase_q    <= 1'b0;
        afe1_wr_seq_q      <= '0;
        afe1_over_sticky_q <= 1'b0;
    end else begin
        if (afe1_group_valid && afe1_fifo_full) begin
            afe1_over_sticky_q <= 1'b1;
        end
        if (afe1_fifo_wren) begin
            afe1_wr_phase_q <= ~afe1_wr_phase_q;
            if (afe1_wr_phase_q) begin
                afe1_wr_seq_q <= afe1_wr_seq_q + {{(SEQ_W-1){1'b0}}, 1'b1};
            end
        end
    end
end

always_ff @(posedge afe2_clk) begin
    if (afe2_rst || afe2_clear || !afe2_enable) begin
        afe2_wr_phase_q    <= 1'b0;
        afe2_wr_seq_q      <= '0;
        afe2_over_sticky_q <= 1'b0;
    end else begin
        if (afe2_group_valid && afe2_fifo_full) begin
            afe2_over_sticky_q <= 1'b1;
        end
        if (afe2_fifo_wren) begin
            afe2_wr_phase_q <= ~afe2_wr_phase_q;
            if (afe2_wr_phase_q) begin
                afe2_wr_seq_q <= afe2_wr_seq_q + {{(SEQ_W-1){1'b0}}, 1'b1};
            end
        end
    end
end

always_ff @(posedge mixed_clk) begin
    if (mixed_rst || mixed_clear || !mixed_enable) begin
        mixed_wr_phase_q    <= 1'b0;
        mixed_wr_seq_q      <= '0;
        mixed_over_sticky_q <= 1'b0;
    end else begin
        if (mixed_group_valid && mixed_fifo_full) begin
            mixed_over_sticky_q <= 1'b1;
        end
        if (mixed_fifo_wren) begin
            mixed_wr_phase_q <= ~mixed_wr_phase_q;
            if (mixed_wr_phase_q) begin
                mixed_wr_seq_q <= mixed_wr_seq_q + {{(SEQ_W-1){1'b0}}, 1'b1};
            end
        end
    end
end

data_sync #(
    .DATA_WIDTH  (3),
    .RESET_VALUE (1'b0),
    .SYNC_DEPTH  (2),
    .RST_SYNC    ("FALSE")
) u_fifo_over_sticky_sync (
    .clk      (out_clk),
`ifdef HSB_2P7_REL
    .rst_n    (~out_rst),
`else
    .rst      (out_rst),
`endif
    .sync_in  ({mixed_over_sticky_q, afe2_over_sticky_q, afe1_over_sticky_q}),
    .sync_out (fifo_over_sticky_sync)
);

dc_fifo_stub #(
    .DATA_WIDTH   (AFE1_FIFO_W),
    .FIFO_DEPTH   (FIFO_DEPTH),
    .ALMOST_FULL  (FIFO_ALMOST_FULL),
    .ALMOST_EMPTY (FIFO_ALMOST_EMPTY),
    .MEM_STYLE    (FIFO_MEM_STYLE)
) u_afe1_fifo (
    .wrclk   (afe1_clk),
    .wrrst   (afe1_rst | afe1_clear | !afe1_enable),
    .wren    (afe1_fifo_wren),
    .wrdin   (afe1_fifo_din),
    .wrcount (afe1_fifo_wrcount),
    .full    (afe1_fifo_full),
    .afull   (afe1_fifo_afull),
    .over    (afe1_fifo_over),
    .rdclk   (out_clk),
    .rdrst   (out_rst | out_clear | !out_enable),
    .rden    (fifo_rden),
    .rddout  (afe1_fifo_dout),
    .rdval   (afe1_fifo_rdval),
    .rdcount (afe1_fifo_rdcount),
    .empty   (afe1_fifo_empty),
    .aempty  (afe1_fifo_aempty),
    .under   (afe1_fifo_under)
);

dc_fifo_stub #(
    .DATA_WIDTH   (AFE2_FIFO_W),
    .FIFO_DEPTH   (FIFO_DEPTH),
    .ALMOST_FULL  (FIFO_ALMOST_FULL),
    .ALMOST_EMPTY (FIFO_ALMOST_EMPTY),
    .MEM_STYLE    (FIFO_MEM_STYLE)
) u_afe2_fifo (
    .wrclk   (afe2_clk),
    .wrrst   (afe2_rst | afe2_clear | !afe2_enable),
    .wren    (afe2_fifo_wren),
    .wrdin   (afe2_fifo_din),
    .wrcount (afe2_fifo_wrcount),
    .full    (afe2_fifo_full),
    .afull   (afe2_fifo_afull),
    .over    (afe2_fifo_over),
    .rdclk   (out_clk),
    .rdrst   (out_rst | out_clear | !out_enable),
    .rden    (fifo_rden),
    .rddout  (afe2_fifo_dout),
    .rdval   (afe2_fifo_rdval),
    .rdcount (afe2_fifo_rdcount),
    .empty   (afe2_fifo_empty),
    .aempty  (afe2_fifo_aempty),
    .under   (afe2_fifo_under)
);

dc_fifo_stub #(
    .DATA_WIDTH   (MIXED_FIFO_W),
    .FIFO_DEPTH   (FIFO_DEPTH),
    .ALMOST_FULL  (FIFO_ALMOST_FULL),
    .ALMOST_EMPTY (FIFO_ALMOST_EMPTY),
    .MEM_STYLE    (FIFO_MEM_STYLE)
) u_mixed_fifo (
    .wrclk   (mixed_clk),
    .wrrst   (mixed_rst | mixed_clear | !mixed_enable),
    .wren    (mixed_fifo_wren),
    .wrdin   (mixed_fifo_din),
    .wrcount (mixed_fifo_wrcount),
    .full    (mixed_fifo_full),
    .afull   (mixed_fifo_afull),
    .over    (mixed_fifo_over),
    .rdclk   (out_clk),
    .rdrst   (out_rst | out_clear | !out_enable),
    .rden    (fifo_rden),
    .rddout  (mixed_fifo_dout),
    .rdval   (mixed_fifo_rdval),
    .rdcount (mixed_fifo_rdcount),
    .empty   (mixed_fifo_empty),
    .aempty  (mixed_fifo_aempty),
    .under   (mixed_fifo_under)
);

assign fifo_rden = out_enable & !out_clear & !afe1_fifo_empty & !afe2_fifo_empty & !mixed_fifo_empty;

assign afe1_fifo_data  = afe1_fifo_dout[0 +: AFE1_GROUP_DATA_W];
assign afe1_fifo_seq   = afe1_fifo_dout[AFE1_SEQ_LSB +: SEQ_W];
assign afe1_fifo_phase = afe1_fifo_dout[AFE1_PHASE_MSB];
assign afe2_fifo_data  = afe2_fifo_dout[0 +: AFE2_GROUP_DATA_W];
assign afe2_fifo_seq   = afe2_fifo_dout[AFE2_SEQ_LSB +: SEQ_W];
assign afe2_fifo_phase = afe2_fifo_dout[AFE2_PHASE_MSB];
assign mixed_fifo_data = mixed_fifo_dout[0 +: MIXED_GROUP_DATA_W];
assign mixed_fifo_seq  = mixed_fifo_dout[MIXED_SEQ_LSB +: SEQ_W];
assign mixed_fifo_phase = mixed_fifo_dout[MIXED_PHASE_MSB];
assign fifo_rdval_all  = afe1_fifo_rdval & afe2_fifo_rdval & mixed_fifo_rdval;
assign phase_match     = (afe1_fifo_phase == mixed_fifo_phase) && (afe2_fifo_phase == mixed_fifo_phase);
assign seq_match       = (afe1_fifo_seq == mixed_fifo_seq) && (afe2_fifo_seq == mixed_fifo_seq);

always_comb begin
    afe1_data_next = '0;
    afe2_data_next = '0;

    for (int lane_i = 0; lane_i < 6; lane_i++) begin
        afe1_data_next[lane_i*SAMPLE_W +: SAMPLE_W] = afe1_fifo_data[lane_i*SAMPLE_W +: SAMPLE_W];
        afe2_data_next[lane_i*SAMPLE_W +: SAMPLE_W] = afe2_fifo_data[lane_i*SAMPLE_W +: SAMPLE_W];
    end

    afe1_data_next[6*SAMPLE_W +: SAMPLE_W] = mixed_fifo_data[0*SAMPLE_W +: SAMPLE_W];
    for (int lane_i = 7; lane_i < AFE_LANES; lane_i++) begin
        afe1_data_next[lane_i*SAMPLE_W +: SAMPLE_W] = afe1_fifo_data[(lane_i-1)*SAMPLE_W +: SAMPLE_W];
    end

    afe2_data_next[ 6*SAMPLE_W +: SAMPLE_W] = mixed_fifo_data[1*SAMPLE_W +: SAMPLE_W];
    afe2_data_next[ 7*SAMPLE_W +: SAMPLE_W] = afe2_fifo_data[6*SAMPLE_W +: SAMPLE_W];
    afe2_data_next[ 8*SAMPLE_W +: SAMPLE_W] = mixed_fifo_data[2*SAMPLE_W +: SAMPLE_W];
    afe2_data_next[ 9*SAMPLE_W +: SAMPLE_W] = mixed_fifo_data[3*SAMPLE_W +: SAMPLE_W];
    afe2_data_next[10*SAMPLE_W +: SAMPLE_W] = mixed_fifo_data[4*SAMPLE_W +: SAMPLE_W];
    afe2_data_next[11*SAMPLE_W +: SAMPLE_W] = mixed_fifo_data[5*SAMPLE_W +: SAMPLE_W];
    for (int lane_i = 12; lane_i < AFE_LANES; lane_i++) begin
        afe2_data_next[lane_i*SAMPLE_W +: SAMPLE_W] = afe2_fifo_data[(lane_i-5)*SAMPLE_W +: SAMPLE_W];
    end
end

always_ff @(posedge out_clk) begin
    if (out_rst) begin
        afe1_data      <= '0;
        afe2_data      <= '0;
        afe1_valid     <= 1'b0;
        afe2_valid     <= 1'b0;
        sample_phase   <= 1'b0;
        sample_seq     <= '0;
        error_status_q <= '0;
    end else begin
        afe1_valid <= 1'b0;
        afe2_valid <= 1'b0;

        if (out_clear || !out_enable) begin
            afe1_data      <= '0;
            afe2_data      <= '0;
            sample_phase   <= 1'b0;
            sample_seq     <= '0;
            error_status_q <= '0;
        end else begin
            if (fifo_over_sticky_sync[0]) begin
                error_status_q[0] <= 1'b1;
            end
            if (fifo_over_sticky_sync[1]) begin
                error_status_q[1] <= 1'b1;
            end
            if (fifo_over_sticky_sync[2]) begin
                error_status_q[2] <= 1'b1;
            end
            if (afe1_fifo_under) begin
                error_status_q[3] <= 1'b1;
            end
            if (afe2_fifo_under) begin
                error_status_q[4] <= 1'b1;
            end
            if (mixed_fifo_under) begin
                error_status_q[5] <= 1'b1;
            end

            if (fifo_rdval_all) begin
                if (!phase_match) begin
                    error_status_q[8] <= 1'b1;
                end
                if (!seq_match) begin
                    error_status_q[9] <= 1'b1;
                end

                if (phase_match && seq_match) begin
                    afe1_data    <= afe1_data_next;
                    afe2_data    <= afe2_data_next;
                    afe1_valid   <= 1'b1;
                    afe2_valid   <= 1'b1;
                    sample_phase <= mixed_fifo_phase;
                    sample_seq   <= mixed_fifo_seq;
                end
            end
        end
    end
end

always_comb begin
    status        = 32'h0;
    status[0]     = out_enable;
    status[1]     = out_clear;
    status[2]     = fifo_rden;
    status[3]     = fifo_rdval_all;
    status[4]     = phase_match;
    status[5]     = seq_match;
    status[6]     = |fifo_over_sticky_sync;
    status[7]     = sample_phase;
    status[8]     = afe1_fifo_empty;
    status[9]     = afe2_fifo_empty;
    status[10]    = mixed_fifo_empty;
    status[11]    = afe1_fifo_aempty;
    status[12]    = afe2_fifo_aempty;
    status[13]    = mixed_fifo_aempty;
    status[17:14] = afe1_fifo_rdcount[3:0];
    status[21:18] = afe2_fifo_rdcount[3:0];
    status[25:22] = mixed_fifo_rdcount[3:0];
end

assign error_status = error_status_q;

endmodule
