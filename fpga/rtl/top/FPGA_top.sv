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

`include "HOLOLINK_def.svh"

module FPGA_top #(
    parameter logic [47:0] BUILD_REV = 48'h0
) (
    // Board control
    input  logic FPGA_CLK_IN,
    input  logic FPGA_PB_RESET_L,
    input  logic FPGA_ETH_REFCLK,

    // LEDs
    output logic FPGA_HB_LED,
    output logic [15:0] STX_DBG_LED,

    // QSFP SERDES
    input  logic [1:0][3:0] QSFP_RX_P,
    output logic [1:0][3:0] QSFP_TX_P,

    // QSFP0
    output logic QSFP0_LPMODE,
    output logic QSFP0_RESET_L,
    inout  tri   QSFP0_SCL,
    inout  tri   QSFP0_SDA,
    output logic QSFP0_MODSEL_L,
    input  logic QSFP0_MODPRS_L,
    output logic QSFP0_PWR_EN,

    // QSFP1
    output logic QSFP1_LPMODE,
    output logic QSFP1_RESET_L,
    inout  tri   QSFP1_SCL,
    inout  tri   QSFP1_SDA,
    output logic QSFP1_MODSEL_L,
    input  logic QSFP1_MODPRS_L,
    output logic QSFP1_PWR_EN,

    // FPGA EEPROM
    inout  tri   FPGA_EEPROM_SCL,
    inout  tri   FPGA_EEPROM_SDA,
    output logic FPGA_EEPROM_I2C_LVL_EN,

    // AFE1 clocks
    input  logic FMC_HB00_CC_TERM_P, // AFE1 DCLKP1
    input  logic FMC_HB06_CC_P,      // AFE1 FCLKP1

    // AFE2 clocks
    input  logic FMC_LA00_CC_TERM_P, // AFE2 DCLKP3
    input  logic FMC_HA17_CC_TERM_P, // AFE2 FCLKP2

    // Device source clocks
    output logic FMC_HA01_CC_P,      // RX AFE shared SYS_CLK / CLKP_2
    output logic FMC_HA02_P,         // TX1 BF_CLK / CLKP_1
    output logic FMC_HA03_P,         // TX2 BF_CLK / CLKP_3

    // AFE RX sync
    output logic FMC_HA13_P,         // RX AFE shared RX_BF_SYNC / SYNCP_2

    // TX73L64 sync
    output logic FMC_HA12_P,         // TX1 TR_BF_SYNC / SYNCP_1
    output logic FMC_HA14_P,         // TX2 TR_BF_SYNC / SYNCP_3

    // AFE1 DOUT
    input  logic FMC_HB05_P,         // AFE1 DOUTP1
    input  logic FMC_HB12_P,         // AFE1 DOUTP2
    input  logic FMC_HB04_P,         // AFE1 DOUTP3
    input  logic FMC_HB07_P,         // AFE1 DOUTP4
    input  logic FMC_HB11_P,         // AFE1 DOUTP5
    input  logic FMC_HB09_P,         // AFE1 DOUTP6
    input  logic FMC_HA04_P,         // AFE1 DOUTP7
    input  logic FMC_HB16_P,         // AFE1 DOUTP8
    input  logic FMC_HB13_P,         // AFE1 DOUTP9
    input  logic FMC_HB02_P,         // AFE1 DOUTP10
    input  logic FMC_HB03_P,         // AFE1 DOUTP11
    input  logic FMC_HB14_P,         // AFE1 DOUTP12
    input  logic FMC_HB01_P,         // AFE1 DOUTP13
    input  logic FMC_HB10_P,         // AFE1 DOUTP14
    input  logic FMC_HB08_P,         // AFE1 DOUTP15
    input  logic FMC_HB15_P,         // AFE1 DOUTP16

    // AFE2 DOUT
    input  logic FMC_LA10_P,         // AFE2 DOUTP1
    input  logic FMC_LA09_P,         // AFE2 DOUTP2
    input  logic FMC_LA08_P,         // AFE2 DOUTP3
    input  logic FMC_LA06_P,         // AFE2 DOUTP4
    input  logic FMC_LA02_P,         // AFE2 DOUTP5
    input  logic FMC_LA05_P,         // AFE2 DOUTP6
    input  logic FMC_HA19_P,         // AFE2 DOUTP7
    input  logic FMC_HA23_P,         // AFE2 DOUTP8
    input  logic FMC_HA18_P,         // AFE2 DOUTP9
    input  logic FMC_HA21_P,         // AFE2 DOUTP10
    input  logic FMC_HA22_P,         // AFE2 DOUTP11
    input  logic FMC_HA20_P,         // AFE2 DOUTP12
    input  logic FMC_LA03_P,         // AFE2 DOUTP13
    input  logic FMC_LA15_P,         // AFE2 DOUTP14
    input  logic FMC_LA16_P,         // AFE2 DOUTP15
    input  logic FMC_LA11_P,         // AFE2 DOUTP16

    // AFE RX SPI
    output logic FMC_LA29_P,         // AFE RX SC_SCLK_1
    output logic FMC_LA29_N,         // AFE RX SC_SEN_1
    output logic FMC_LA32_P,         // AFE1 SC_DATA_1
    output logic FMC_LA20_N,         // AFE2 SC_DATA_2
    input  logic FMC_LA20_P,         // AFE RX SC_SDOUT_1

    // TX73L64 SPI
    output logic FMC_HA11_P,         // TX SPI S_CLKP_1
    output logic FMC_HA10_P,         // TX SPI S_SENP_1
    output logic FMC_HA09_P,         // TX1 SDATA_0 / S_DATAP_1
    output logic FMC_HB21_P,         // TX1 SDATA_1 / S_DATAP_2
    output logic FMC_HA16_P,         // TX2 SDATA_0 / S_DATAP_3
    output logic FMC_CLK1_M2C_TERM_P, // TX2 SDATA_1 / S_DATAP_4

    // Common TX/RX GPIO control/status
    output logic FMC_LA28_N,         // PDN_STDBY_FPGA / GPIO_TRX5
    input  logic FMC_LA30_P,         // CRC_MATCH / GPIO_TRX8
    output logic FMC_LA18_CC_P,      // CRC_EN / GPIO_TRX2
    output logic FMC_LA33_P,         // RST / GPIO_TRX6
    input  logic FMC_LA17_CC_TERM_N, // TX_TSHUT / GPIO_TRX1
    input  logic FMC_LA28_P,         // TX_RXZ / GPIO_TRX4
    output logic FMC_LA33_N,         // TX_WAKEUP / GPIO_TRX7
    output logic FMC_HA08_N,         // RX_SPI_LVDSEN / GPIO_RX6

    // FMC control
    output logic FMC_PWR_EN,         // 602 FMC power enable
    output logic FPGA_PG_C2M,        // Carrier-to-mezzanine power good
    input  logic FMC_PG_C2M_1V8      // FMC power-good sense
);

localparam int HALF_SECOND        = 26'h2FAF07F;
localparam int MAILBOX_APB_IDX       = 2;
localparam int SYNC_CTRL_APB_IDX    = 3;
localparam int CAPTURE_CTRL_APB_IDX = 4;
localparam int BOARD_CTRL_APB_IDX   = 5;
localparam int TX_HS_SPI_APB_IDX    = 6;
localparam int LVDS_APB_IDX         = 7;
localparam int BOARD_N_CTRL         = 1;
localparam int BOARD_N_STAT         = 2;
localparam int BOARD_W_OFST         = 8;
localparam int BOARD_CTRL_REG       = 0;
localparam int BOARD_STAT_REG       = 0;
localparam int BOARD_LVDS_STAT_REG  = 1;
localparam int AFE_SAMPLE_DWIDTH             = 16*12;
localparam int AFE_COMBO_DWIDTH              = 2*AFE_SAMPLE_DWIDTH;
localparam int TX_HS_SPI_PACKAGE_OP_INDEX_W =
    (tx_hs_spi_pkg::TX_HS_SPI_DEFAULT_MAX_OPS_PER_BANK <= 1) ? 1 :
    $clog2(tx_hs_spi_pkg::TX_HS_SPI_DEFAULT_MAX_OPS_PER_BANK);

logic                          ninit_done;
logic                          fpga_clk_pll_rst;
logic                          fpga_clk_100;
logic                          fpga_clk_200;
logic                          fpga_clk_pll_lock;
logic                          fpga_clk_pll_lock_sync;
logic                          rst_sys;
logic                          pb_rst_debounced;
logic                          hl_sw_rst_sync;
logic [`HOST_IF_INST-1:0]      eth_rdy;
logic [`HOST_IF_INST-1:0]      eth_rst_n;
logic [`HOST_IF_INST-1:0]      eth_clk_div64;
logic                          hif_pll_locked;
logic                          hif_pll_locked_sync;
logic [25:0]                   second_cnt;
logic                          heartbeat;
logic                          sys_rst;
logic                          apb_clk;
logic                          apb_rst;
logic                          hif_clk;
logic                          hif_rst;
logic                          ptp_clk;
logic                          ptp_rst;
logic [`SENSOR_RX_IF_INST-1:0] sif_rx_clk;
logic [`SENSOR_RX_IF_INST-1:0] sif_rx_rst;
logic [`SENSOR_TX_IF_INST-1:0] sif_tx_clk;
logic [`SENSOR_TX_IF_INST-1:0] sif_tx_rst;
logic                          hololink_init_done;
logic                          hl_sw_rst;
logic [31:0]                   hl_sw_sen_rst;
logic                          lvds_reset;
logic                          tx_spi_rst;
logic                          afe1_dclk_p;
logic                          afe1_fclk_p;
logic                          afe2_dclk_p;
logic                          afe2_fclk_p;
logic                          tx_spi_clk;
logic                          tx1_bf_clk;
logic                          tx2_bf_clk;
logic                          afe_rx_bf_sync;
logic                          tx1_bf_sync;
logic                          tx2_bf_sync;
logic                          lvds_rx_bf_sync_req;
logic                          capture_start;
logic                          capture_start_sif;
logic                          capture_busy;
logic                          tx_update_capture_error;
logic                          trx_pdn_standby;
logic                          trx_crc_en;
logic                          trx_reset;
logic                          trx_reset_req;
logic                          tx_wakeup;
logic                          rx_spi_lvds_en;
(* keep = "true" *)
logic                          trx_crc_match_sync;
(* keep = "true" *)
logic                          tx_tshut_sync;
(* keep = "true" *)
logic                          tx_rxz_sync;
logic [15:0]                   afe1_dout_p;
logic [15:0]                   afe2_dout_p;

logic [`REG_INST-1:0]          apb_psel;
logic                          apb_penable;
logic [31:0]                   apb_paddr;
logic [31:0]                   apb_pwdata;
logic                          apb_pwrite;
logic [`REG_INST-1:0]          apb_pready;
logic [31:0]                   apb_prdata [`REG_INST-1:0];
logic [`REG_INST-1:0]          apb_pserr;
apb_pkg::apb_m2s               lvds_apb_m2s;
apb_pkg::apb_s2m               lvds_apb_s2m;
apb_pkg::apb_m2s               sync_ctrl_apb_m2s;
apb_pkg::apb_s2m               sync_ctrl_apb_s2m;
apb_pkg::apb_m2s               capture_ctrl_apb_m2s;
apb_pkg::apb_s2m               capture_ctrl_apb_s2m;
apb_pkg::apb_m2s               board_ctrl_apb_m2s;
apb_pkg::apb_s2m               board_ctrl_apb_s2m;
apb_pkg::apb_m2s               tx_hs_spi_apb_m2s;
apb_pkg::apb_s2m               tx_hs_spi_apb_s2m;
logic [31:0]                   board_ctrl_reg [BOARD_N_CTRL];
logic [31:0]                   board_stat_reg [BOARD_N_STAT];
logic [16:0]                   led_lfsr;

logic [`SENSOR_RX_IF_INST-1:0] sif_rx_axis_tvalid;
logic [`SENSOR_RX_IF_INST-1:0] sif_rx_axis_tlast;
logic [`DATAPATH_WIDTH-1:0]    sif_rx_axis_tdata [`SENSOR_RX_IF_INST-1:0];
logic [`DATAKEEP_WIDTH-1:0]    sif_rx_axis_tkeep [`SENSOR_RX_IF_INST-1:0];
logic [`DATAUSER_WIDTH-1:0]    sif_rx_axis_tuser [`SENSOR_RX_IF_INST-1:0];
logic [`SENSOR_RX_IF_INST-1:0] sif_rx_axis_tready;
logic [`SENSOR_TX_IF_INST-1:0] sif_tx_axis_tvalid;
logic [`SENSOR_TX_IF_INST-1:0] sif_tx_axis_tlast;
logic [`DATAPATH_WIDTH-1:0]    sif_tx_axis_tdata [`SENSOR_TX_IF_INST-1:0];
logic [`DATAKEEP_WIDTH-1:0]    sif_tx_axis_tkeep [`SENSOR_TX_IF_INST-1:0];
logic [`DATAUSER_WIDTH-1:0]    sif_tx_axis_tuser [`SENSOR_TX_IF_INST-1:0];
logic [`SENSOR_TX_IF_INST-1:0] sif_tx_axis_tready;
logic [15:0]                   sif_event;
logic [AFE_COMBO_DWIDTH-1:0]    afe_combo_axis_data;
logic                          afe_combo_axis_valid;

logic [`HOST_IF_INST-1:0]      hif_rx_axis_tvalid;
logic [`HOST_IF_INST-1:0]      hif_rx_axis_tlast;
logic [`HOST_WIDTH-1:0]        hif_rx_axis_tdata [`HOST_IF_INST-1:0];
logic [`HOSTKEEP_WIDTH-1:0]    hif_rx_axis_tkeep [`HOST_IF_INST-1:0];
logic [`HOSTUSER_WIDTH-1:0]    hif_rx_axis_tuser [`HOST_IF_INST-1:0];
logic [`HOST_IF_INST-1:0]      hif_rx_axis_tready;
logic [`HOST_IF_INST-1:0]      hif_tx_axis_tvalid;
logic [`HOST_IF_INST-1:0]      hif_tx_axis_tlast;
logic [`HOST_WIDTH-1:0]        hif_tx_axis_tdata [`HOST_IF_INST-1:0];
logic [`HOSTKEEP_WIDTH-1:0]    hif_tx_axis_tkeep [`HOST_IF_INST-1:0];
logic [`HOSTUSER_WIDTH-1:0]    hif_tx_axis_tuser [`HOST_IF_INST-1:0];
logic [`HOST_IF_INST-1:0]      hif_tx_axis_tready;

logic                          tx_spi_rst_n;
logic [`SPI_INST-1:0]          spi_csn;
logic [`SPI_INST-1:0]          spi_sck;
`ifdef HSB_2P7_REL
logic [`SPI_INST-1:0]          spi_oen;
`else
logic [3:0]                    spi_oen [`SPI_INST-1:0];
`endif
(* keep = "true" *)
logic [3:0]                    spi_sdio_i [`SPI_INST-1:0];
logic [3:0]                    spi_sdio_o [`SPI_INST-1:0];
(* keep = "true" *)
logic                          tx_spi_sdout_sync;
(* keep = "true" *)
logic                          afe_spi_sdout_sync;
logic [1:0]                    afe_rx_spi_sdata_mode;
logic                          afe_rx1_spi_sdata;
logic                          afe_rx2_spi_sdata;
logic                          tx1_spi_sdata0;
logic                          tx1_spi_sdata1;
logic                          tx2_spi_sdata0;
logic                          tx2_spi_sdata1;
logic                          tx_spi_sclk;
logic                          tx_spi_sen;
logic [1:0]                    tx_hs_pkg_bank_ready              /* synthesis preserve noprune */;
logic [15:0]                   tx_hs_pkg_bank0_sequence          /* synthesis preserve noprune */;
logic [15:0]                   tx_hs_pkg_bank1_sequence          /* synthesis preserve noprune */;
logic [15:0]                   tx_hs_pkg_bank0_op_count          /* synthesis preserve noprune */;
logic [15:0]                   tx_hs_pkg_bank1_op_count          /* synthesis preserve noprune */;
logic                          tx_hs_pkg_ready_pulse             /* synthesis preserve noprune */;
logic                          tx_hs_pkg_ready_bank              /* synthesis preserve noprune */;
logic [15:0]                   tx_hs_pkg_ready_sequence          /* synthesis preserve noprune */;
logic [15:0]                   tx_hs_pkg_ready_op_count          /* synthesis preserve noprune */;
logic                          tx_hs_pkg_error_pulse             /* synthesis preserve noprune */;
logic [7:0]                    tx_hs_pkg_last_error_code         /* synthesis preserve noprune */;
logic [31:0]                   tx_hs_pkg_accepted_package_count  /* synthesis preserve noprune */;
logic [31:0]                   tx_hs_pkg_accepted_fragment_count /* synthesis preserve noprune */;
logic [31:0]                   tx_hs_pkg_error_count             /* synthesis preserve noprune */;
logic                          tx_update_expected_sequence_valid /* synthesis preserve noprune */;
logic [15:0]                   tx_update_expected_sequence       /* synthesis preserve noprune */;
logic                          tx_update_release                 /* synthesis preserve noprune */;
logic                          tx_update_event_retired           /* synthesis preserve noprune */;
logic                          tx_update_ready                   /* synthesis preserve noprune */;

logic [`I2C_INST-1:0]          i2c_scl;
logic [`I2C_INST-1:0]          i2c_sda;
logic [`I2C_INST-1:0]          i2c_scl_en;
logic [`I2C_INST-1:0]          i2c_sda_en;
logic                          fpga_eeprom_i2c_scl_sync;
logic                          fpga_eeprom_i2c_sda_sync;

logic [`GPIO_INST-1:0]         hl_gpio_i;
logic [`GPIO_INST-1:0]         hl_gpio;
logic [`GPIO_INST-1:0]         hl_gpio_dir;

(* keep = "true" *)
logic [AFE_SAMPLE_DWIDTH-1:0]  afe1_aligned_data;
(* keep = "true" *)
logic                          afe1_aligned_valid;
(* keep = "true" *)
logic                          lvds_out_clk;
(* keep = "true" *)
logic [AFE_SAMPLE_DWIDTH-1:0]  afe2_aligned_data;
(* keep = "true" *)
logic                          afe2_aligned_valid;
(* keep = "true" *)
logic                          afe_sample_phase;
(* keep = "true" *)
logic                          mixed_stream_clk;
(* keep = "true" *)
logic [31:0]                   lvds_debug_status;
logic [47:0]                   ptp_sec;
logic [31:0]                   ptp_nanosec;
logic                          pps;

reset_ip intel_reset_ip (
    .ninit_done (ninit_done)
);

assign fpga_clk_pll_rst = ninit_done;

fpga_clk_pll_mfg fpga_clk_pll (
    .rst      (fpga_clk_pll_rst),
    .refclk   (FPGA_CLK_IN),
    .locked   (fpga_clk_pll_lock),
    .outclk_0 (fpga_clk_100),
    .outclk_1 (fpga_clk_200)
);

data_sync #(
    .DATA_WIDTH (3)
) rst_and_lock_sync (
    .clk      (fpga_clk_100),
`ifdef HSB_2P7_REL
    .rst_n    (1'b1),
`else
    .rst      (1'b0),
`endif
    .sync_in  ({hl_sw_rst, fpga_clk_pll_lock, hif_pll_locked}),
    .sync_out ({hl_sw_rst_sync, fpga_clk_pll_lock_sync, hif_pll_locked_sync})
);

rst_pb_debounce rst_pb_debounce (
    .clk        (fpga_clk_100),
    .pb_rst_in  (FPGA_PB_RESET_L),
    .pb_rst_out (pb_rst_debounced)
);

assign rst_sys        = !fpga_clk_pll_lock_sync || hl_sw_rst_sync || pb_rst_debounced;
assign sys_rst        = rst_sys || !hif_pll_locked_sync;
assign lvds_reset     = rst_sys || trx_reset_req;
assign apb_clk        = fpga_clk_100;
assign sif_rx_clk[0]  = lvds_out_clk;

// Clock TX-SPI/event timing from the AFE1 LVDS stream clock so TX sync and
// capture share a sample-aligned domain.
assign tx_spi_clk     = lvds_out_clk;
assign tx_spi_rst_n   = ~tx_spi_rst;
assign sif_tx_clk[0]  = tx_spi_clk;

hif_pll hif_pll (
    .rst      (!eth_rst_n[0]),
    .refclk   (eth_clk_div64[0]),
    .locked   (hif_pll_locked),
    .outclk_0 (),
    .outclk_1 (hif_clk),
    .outclk_2 (ptp_clk)
);

always_ff @(posedge apb_clk) begin
    if (rst_sys) begin
        second_cnt <= 26'b0;
        heartbeat  <= 1'b0;
    end else if (second_cnt == HALF_SECOND) begin
        second_cnt <= 26'b0;
        heartbeat  <= ~heartbeat;
    end else begin
        second_cnt <= second_cnt + 1'b1;
    end
end

led #(
    .LED_N (17)
) u_led (
    .clk   (fpga_clk_100),
    .rst   (rst_sys),
    .led_o (led_lfsr)
);

assign FPGA_HB_LED  = led_lfsr[16];
assign STX_DBG_LED  = led_lfsr[15:0];

assign QSFP0_LPMODE   = 1'b0;
assign QSFP0_RESET_L  = 1'b1;
assign QSFP0_MODSEL_L = 1'b0;
assign QSFP0_PWR_EN   = 1'b1;
assign QSFP1_LPMODE   = 1'b0;
assign QSFP1_RESET_L  = 1'b1;
assign QSFP1_MODSEL_L = 1'b0;
assign QSFP1_PWR_EN   = 1'b1;
assign FMC_PWR_EN     = 1'b1;
assign FPGA_PG_C2M    = 1'b1;

assign afe1_dclk_p = FMC_HB00_CC_TERM_P;
assign afe1_fclk_p = FMC_HB06_CC_P;
assign afe2_dclk_p = FMC_LA00_CC_TERM_P;
assign afe2_fclk_p = FMC_HA17_CC_TERM_P;

assign tx1_bf_clk     = tx_spi_clk;
assign tx2_bf_clk     = tx_spi_clk;

reset_sync tx_spi_rst_sync (
    .i_clk    (tx_spi_clk),
    .i_arst_n (FPGA_PB_RESET_L),
    .i_srst   (rst_sys),
    .i_locked (1'b1),
    .o_arst   (),
    .o_arst_n (),
    .o_srst   (tx_spi_rst),
    .o_srst_n ()
);

sync_ctrl u_sync_ctrl (
    .apb_clk          (apb_clk),
    .apb_rst          (apb_rst),
    .i_apb_m2s        (sync_ctrl_apb_m2s),
    .o_apb_s2m        (sync_ctrl_apb_s2m),
    .sync_clk         (tx_spi_clk),
    .sync_rst         (tx_spi_rst),
    .sync_phase_valid_i (afe_combo_axis_valid),
    .sync_phase_i       (afe_sample_phase),
    .capture_busy_i     (capture_busy),
    .tx_wakeup_o      (tx_wakeup),
    .tx1_bf_sync_o    (tx1_bf_sync),
    .tx2_bf_sync_o    (tx2_bf_sync),
    .capture_start_o  (capture_start),
    .rx_bf_sync_int_o (lvds_rx_bf_sync_req),
    .rx_bf_sync_ext_o (afe_rx_bf_sync),
    .tx_update_ready_i                    (tx_update_ready),
    .tx_update_expected_sequence_valid_o  (tx_update_expected_sequence_valid),
    .tx_update_expected_sequence_o        (tx_update_expected_sequence),
    .tx_update_event_retired_o            (tx_update_event_retired),
    .tx_update_release_o                  (tx_update_release),
    .tx_update_capture_error_o            (tx_update_capture_error)
);

// tx_spi_clk and lvds_out_clk are the same net (see the shimmer-bug
// reclocking above), so capture_start no longer crosses a real clock
// domain. A single reset-qualified register replaces the CDC pulse
// synchronizer that used to be required here.
always_ff @(posedge lvds_out_clk) begin
    if (tx_spi_rst || sif_rx_rst[0]) begin
        capture_start_sif <= 1'b0;
    end else begin
        capture_start_sif <= capture_start;
    end
end

assign FMC_HA01_CC_P  = fpga_clk_100;
assign FMC_HA02_P     = tx1_bf_clk;
assign FMC_HA03_P     = tx2_bf_clk;
assign FMC_HA13_P     = afe_rx_bf_sync;
assign FMC_HA12_P     = tx1_bf_sync;
assign FMC_HA14_P     = tx2_bf_sync;

// Board control is owned by the top-level APB register drop. Keep external
// reset coincident with the LVDS reset so alignment starts after the devices release.
assign afe_rx_spi_sdata_mode = board_ctrl_reg[BOARD_CTRL_REG][1:0];
assign trx_pdn_standby       = board_ctrl_reg[BOARD_CTRL_REG][4];
assign trx_crc_en            = board_ctrl_reg[BOARD_CTRL_REG][5];
assign trx_reset_req         = board_ctrl_reg[BOARD_CTRL_REG][6];
assign rx_spi_lvds_en        = board_ctrl_reg[BOARD_CTRL_REG][8];
assign trx_reset             = lvds_reset;

assign FMC_LA28_N      = trx_pdn_standby;
assign FMC_LA18_CC_P   = trx_crc_en;
assign FMC_LA33_P      = trx_reset;
assign FMC_LA33_N      = tx_wakeup;
assign FMC_HA08_N      = rx_spi_lvds_en;

data_sync #(
    .DATA_WIDTH (3)
) trx_status_filter (
    .clk      (apb_clk),
`ifdef HSB_2P7_REL
    .rst_n    (~apb_rst),
`else
    .rst      (apb_rst),
`endif
    .sync_in  ({FMC_LA30_P, FMC_LA17_CC_TERM_N, FMC_LA28_P}),
    .sync_out ({trx_crc_match_sync, tx_tshut_sync, tx_rxz_sync})
);

s_apb_reg #(
    .N_CTRL   (BOARD_N_CTRL),
    .N_STAT   (BOARD_N_STAT),
    .W_OFST   (BOARD_W_OFST),
    .SYNC_CLK (0),
    .SAME_CLK (1)
) u_board_ctrl_apb_reg (
    .i_aclk    (apb_clk),
    .i_arst    (apb_rst),
    .i_apb_m2s (board_ctrl_apb_m2s),
    .o_apb_s2m (board_ctrl_apb_s2m),
    .i_pclk    (apb_clk),
    .i_prst    (apb_rst),
    .o_ctrl    (board_ctrl_reg),
    .i_stat    (board_stat_reg)
);

always_comb begin
    board_stat_reg[BOARD_STAT_REG]       = 32'b0;
    board_stat_reg[BOARD_STAT_REG][ 9]   = trx_crc_match_sync;
    board_stat_reg[BOARD_STAT_REG][10]   = tx_tshut_sync;
    board_stat_reg[BOARD_STAT_REG][11]   = tx_rxz_sync;
    board_stat_reg[BOARD_STAT_REG][12]   = lvds_debug_status[14];
    board_stat_reg[BOARD_STAT_REG][13]   = lvds_debug_status[15];
    board_stat_reg[BOARD_STAT_REG][14]   = lvds_debug_status[0];
    board_stat_reg[BOARD_STAT_REG][15]   = lvds_debug_status[1];
    board_stat_reg[BOARD_LVDS_STAT_REG]  = lvds_debug_status;
end

assign hl_gpio_i = '0;

assign lvds_apb_m2s.psel       = apb_psel[LVDS_APB_IDX];
assign lvds_apb_m2s.penable    = apb_penable;
assign lvds_apb_m2s.paddr      = apb_paddr;
assign lvds_apb_m2s.pwdata     = apb_pwdata;
assign lvds_apb_m2s.pwrite     = apb_pwrite;
assign apb_pready[LVDS_APB_IDX] = lvds_apb_s2m.pready;
assign apb_prdata[LVDS_APB_IDX] = lvds_apb_s2m.prdata;
assign apb_pserr[LVDS_APB_IDX]  = lvds_apb_s2m.pserr;

assign sync_ctrl_apb_m2s.psel       = apb_psel[SYNC_CTRL_APB_IDX];
assign sync_ctrl_apb_m2s.penable    = apb_penable;
assign sync_ctrl_apb_m2s.paddr      = apb_paddr;
assign sync_ctrl_apb_m2s.pwdata     = apb_pwdata;
assign sync_ctrl_apb_m2s.pwrite     = apb_pwrite;
assign apb_pready[SYNC_CTRL_APB_IDX] = sync_ctrl_apb_s2m.pready;
assign apb_prdata[SYNC_CTRL_APB_IDX] = sync_ctrl_apb_s2m.prdata;
assign apb_pserr[SYNC_CTRL_APB_IDX]  = sync_ctrl_apb_s2m.pserr;

assign capture_ctrl_apb_m2s.psel       = apb_psel[CAPTURE_CTRL_APB_IDX];
assign capture_ctrl_apb_m2s.penable    = apb_penable;
assign capture_ctrl_apb_m2s.paddr      = apb_paddr;
assign capture_ctrl_apb_m2s.pwdata     = apb_pwdata;
assign capture_ctrl_apb_m2s.pwrite     = apb_pwrite;
assign apb_pready[CAPTURE_CTRL_APB_IDX] = capture_ctrl_apb_s2m.pready;
assign apb_prdata[CAPTURE_CTRL_APB_IDX] = capture_ctrl_apb_s2m.prdata;
assign apb_pserr[CAPTURE_CTRL_APB_IDX]  = capture_ctrl_apb_s2m.pserr;

assign board_ctrl_apb_m2s.psel       = apb_psel[BOARD_CTRL_APB_IDX];
assign board_ctrl_apb_m2s.penable    = apb_penable;
assign board_ctrl_apb_m2s.paddr      = apb_paddr;
assign board_ctrl_apb_m2s.pwdata     = apb_pwdata;
assign board_ctrl_apb_m2s.pwrite     = apb_pwrite;
assign apb_pready[BOARD_CTRL_APB_IDX] = board_ctrl_apb_s2m.pready;
assign apb_prdata[BOARD_CTRL_APB_IDX] = board_ctrl_apb_s2m.prdata;
assign apb_pserr[BOARD_CTRL_APB_IDX]  = board_ctrl_apb_s2m.pserr;

assign tx_hs_spi_apb_m2s.psel       = apb_psel[TX_HS_SPI_APB_IDX];
assign tx_hs_spi_apb_m2s.penable    = apb_penable;
assign tx_hs_spi_apb_m2s.paddr      = apb_paddr;
assign tx_hs_spi_apb_m2s.pwdata     = apb_pwdata;
assign tx_hs_spi_apb_m2s.pwrite     = apb_pwrite;
assign apb_pready[TX_HS_SPI_APB_IDX] = tx_hs_spi_apb_s2m.pready;
assign apb_prdata[TX_HS_SPI_APB_IDX] = tx_hs_spi_apb_s2m.prdata;
assign apb_pserr[TX_HS_SPI_APB_IDX]  = tx_hs_spi_apb_s2m.pserr;

// Intel Mailbox IP for QSPI configuration/remote system update
mailbox_top u_mailbox_top (
    .apb_clk                        (apb_clk),
    .apb_rst                        (apb_rst),
    .i_mailbox_apb_psel             (apb_psel[MAILBOX_APB_IDX]),
    .i_mailbox_apb_penable          (apb_penable),
    .i_mailbox_apb_paddr            (apb_paddr),
    .i_mailbox_apb_pwdata           (apb_pwdata),
    .i_mailbox_apb_pwrite           (apb_pwrite),
    .o_mailbox_apb_pready           (apb_pready[MAILBOX_APB_IDX]),
    .o_mailbox_apb_prdata           (apb_prdata[MAILBOX_APB_IDX]),
    .o_mailbox_apb_pserr            (apb_pserr[MAILBOX_APB_IDX])
);

// APB drops 0 and 1 are owned by the two Ethernet instances. Drop 2 is the
// Intel Mailbox IP (QSPI/RSU). Drop 3 is sync_ctrl. Drop 4 is capture_ctrl.
// Drop 5 is board_ctrl. Drop 6 is the TX high-speed SPI block.
// Drop 7 is the LVDS lab/debug block. All user slots are in use.

generate
    for (genvar eth_i = 0; eth_i < `HOST_IF_INST; eth_i++) begin : ethernet_100gb
        eth_top eth_top_inst (
            .hif_clk               (hif_clk),
            .fpga_clk_100          (fpga_clk_100),
            .eth_reset_in          (rst_sys),
            .eth_rdy               (eth_rdy[eth_i]),
            .heartbeat             (heartbeat),
            .eth_clk               (eth_clk_div64[eth_i]),
            .eth_rst_n             (eth_rst_n[eth_i]),
            .i_clk_ref             (FPGA_ETH_REFCLK),
            .i_rx_serial           (QSFP_RX_P[eth_i]),
            .o_tx_serial           (QSFP_TX_P[eth_i]),
            .o_eth_axis_rx_tvalid  (hif_rx_axis_tvalid[eth_i]),
            .o_eth_axis_rx_tdata   (hif_rx_axis_tdata[eth_i]),
            .o_eth_axis_rx_tlast   (hif_rx_axis_tlast[eth_i]),
            .o_eth_axis_rx_tuser   (hif_rx_axis_tuser[eth_i]),
            .o_eth_axis_rx_tkeep   (hif_rx_axis_tkeep[eth_i]),
            .i_eth_axis_tx_tvalid  (hif_tx_axis_tvalid[eth_i]),
            .i_eth_axis_tx_tdata   (hif_tx_axis_tdata[eth_i]),
            .i_eth_axis_tx_tlast   (hif_tx_axis_tlast[eth_i]),
            .i_eth_axis_tx_tuser   (hif_tx_axis_tuser[eth_i]),
            .i_eth_axis_tx_tkeep   (hif_tx_axis_tkeep[eth_i]),
            .o_eth_axis_tx_tready  (hif_tx_axis_tready[eth_i]),
            .i_eth_apb_psel        (apb_psel[eth_i]),
            .i_eth_apb_penable     (apb_penable),
            .i_eth_apb_paddr       (apb_paddr),
            .i_eth_apb_pwdata      (apb_pwdata),
            .i_eth_apb_pwrite      (apb_pwrite),
            .o_eth_apb_pready      (apb_pready[eth_i]),
            .o_eth_apb_prdata      (apb_prdata[eth_i]),
            .o_eth_apb_pserr       (apb_pserr[eth_i])
        );
    end
endgenerate

// The LVDS assembler exports one 192-bit sample beat per AFE in the AFE1
// stream clock domain. Join AFE1 and AFE2 into a single 384-bit sample-pair
// stream. capture_ctrl gates capture windows and owns the axis_buffer gearbox
// that packs the stream into one 512-bit SIF.
assign afe_combo_axis_valid = afe1_aligned_valid & afe2_aligned_valid;
assign afe_combo_axis_data  = {afe2_aligned_data, afe1_aligned_data};

capture_ctrl #(
    .IN_DWIDTH  (AFE_COMBO_DWIDTH),
    .OUT_DWIDTH (`DATAPATH_WIDTH),
    .BUF_DEPTH  (1024),
    .W_USER     (`DATAUSER_WIDTH),
    .OUT_W_USER (`DATAUSER_WIDTH)
) u_capture_ctrl (
    .apb_clk          (apb_clk),
    .apb_rst          (apb_rst),
    .i_apb_m2s        (capture_ctrl_apb_m2s),
    .o_apb_s2m        (capture_ctrl_apb_s2m),
    .stream_clk       (lvds_out_clk),
    .stream_rst       (sif_rx_rst[0]),
    .capture_start_i  (capture_start_sif),
    .capture_error_i  (tx_update_capture_error),
    .capture_busy_o   (capture_busy),
    .i_axis_rx_tvalid (afe_combo_axis_valid),
    .i_axis_rx_tdata  (afe_combo_axis_data),
    .i_axis_rx_phase  (afe_sample_phase),
    .o_axis_tx_tvalid (sif_rx_axis_tvalid[0]),
    .o_axis_tx_tdata  (sif_rx_axis_tdata[0]),
    .o_axis_tx_tlast  (sif_rx_axis_tlast[0]),
    .o_axis_tx_tuser  (sif_rx_axis_tuser[0]),
    .o_axis_tx_tkeep  (sif_rx_axis_tkeep[0]),
    .i_axis_tx_tready (sif_rx_axis_tready[0])
);

assign sif_event              = '0;

glitch_filter #(
    .DATA_WIDTH   (2),
    .RESET_VALUE  (1'b1),
    .FILTER_DEPTH (8)
) fpga_eeprom_i2c_glitch_filter (
    .clk          (apb_clk),
`ifdef HSB_2P7_REL
    .rst_n        (~apb_rst),
`else
    .rst          (apb_rst),
`endif
    .sync_in      ({FPGA_EEPROM_SDA, FPGA_EEPROM_SCL}),
    .sync_out     ({fpga_eeprom_i2c_sda_sync, fpga_eeprom_i2c_scl_sync})
);

assign i2c_scl[0]              = i2c_scl_en[0] ? fpga_eeprom_i2c_scl_sync : 1'b0;
assign i2c_sda[0]              = i2c_sda_en[0] ? fpga_eeprom_i2c_sda_sync : 1'b0;
assign FPGA_EEPROM_SCL         = i2c_scl_en[0] ? 1'bz : 1'b0;
assign FPGA_EEPROM_SDA         = i2c_sda_en[0] ? 1'bz : 1'b0;
assign FPGA_EEPROM_I2C_LVL_EN  = 1'b1;

assign afe1_dout_p[ 0] = FMC_HB05_P;
assign afe1_dout_p[ 1] = FMC_HB12_P;
assign afe1_dout_p[ 2] = FMC_HB04_P;
assign afe1_dout_p[ 3] = FMC_HB07_P;
assign afe1_dout_p[ 4] = FMC_HB11_P;
assign afe1_dout_p[ 5] = FMC_HB09_P;
assign afe1_dout_p[ 6] = FMC_HA04_P;
assign afe1_dout_p[ 7] = FMC_HB16_P;
assign afe1_dout_p[ 8] = FMC_HB13_P;
assign afe1_dout_p[ 9] = FMC_HB02_P;
assign afe1_dout_p[10] = FMC_HB03_P;
assign afe1_dout_p[11] = FMC_HB14_P;
assign afe1_dout_p[12] = FMC_HB01_P;
assign afe1_dout_p[13] = FMC_HB10_P;
assign afe1_dout_p[14] = FMC_HB08_P;
assign afe1_dout_p[15] = FMC_HB15_P;

assign afe2_dout_p[ 0] = FMC_LA10_P;
assign afe2_dout_p[ 1] = FMC_LA09_P;
assign afe2_dout_p[ 2] = FMC_LA08_P;
assign afe2_dout_p[ 3] = FMC_LA06_P;
assign afe2_dout_p[ 4] = FMC_LA02_P;
assign afe2_dout_p[ 5] = FMC_LA05_P;
assign afe2_dout_p[ 6] = FMC_HA19_P;
assign afe2_dout_p[ 7] = FMC_HA23_P;
assign afe2_dout_p[ 8] = FMC_HA18_P;
assign afe2_dout_p[ 9] = FMC_HA21_P;
assign afe2_dout_p[10] = FMC_HA22_P;
assign afe2_dout_p[11] = FMC_HA20_P;
assign afe2_dout_p[12] = FMC_LA03_P;
assign afe2_dout_p[13] = FMC_LA15_P;
assign afe2_dout_p[14] = FMC_LA16_P;
assign afe2_dout_p[15] = FMC_LA11_P;

// The AFE RX devices share SCLK/SEN; HSB SPI instance 0 is fanned out with a
// simple SDATA mux.
// AFE RX mode 0: broadcast SDIO[0] to both RX SDATA pins.
// AFE RX mode 1: drive RX1 SDATA from SDIO[0], hold RX2 SDATA low.
// AFE RX mode 2: drive RX2 SDATA from SDIO[0], hold RX1 SDATA low.
//
// TX73L64 SPI is owned by tx_hs_spi_top. HSB SPI instance 1 remains inside
// HOLOLINK_top for now, but no longer drives the physical TX SPI pins.
//
// FMC_LA20_P (SDOUT) is a single physical bus shared by all four AFE/TX73L64
// devices, so it needs its own independent synchronizer per destination
// clock domain rather than being chained through one another's synchronized
// copy. tx_spi_sdout_sync (tx_spi_clk domain) feeds tx_hs_spi_top for
// TX73L64 readback; afe_spi_sdout_sync (apb_clk domain) feeds HSB SPI
// instance 0 for AFE1/AFE2 readback.

data_sync #(
    .DATA_WIDTH (1)
) tx_spi_sdout_filter (
    .clk      (tx_spi_clk),
`ifdef HSB_2P7_REL
    .rst_n    (~tx_spi_rst),
`else
    .rst      (tx_spi_rst),
`endif
    .sync_in  (FMC_LA20_P),
    .sync_out (tx_spi_sdout_sync)
);

data_sync #(
    .DATA_WIDTH (1)
) afe_spi_sdout_filter (
    .clk      (apb_clk),
`ifdef HSB_2P7_REL
    .rst_n    (~apb_rst),
`else
    .rst      (apb_rst),
`endif
    .sync_in  (FMC_LA20_P),
    .sync_out (afe_spi_sdout_sync)
);

tx_hs_spi_top #(.OP_INDEX_W(TX_HS_SPI_PACKAGE_OP_INDEX_W)) u_tx_hs_spi_top (
    .apb_clk                          (apb_clk),
    .apb_rst                          (apb_rst),
    .i_apb_m2s                        (tx_hs_spi_apb_m2s),
    .o_apb_s2m                        (tx_hs_spi_apb_s2m),
    .tx_spi_clk                       (tx_spi_clk),
    .tx_spi_rst                       (tx_spi_rst),
    .s_axis_tvalid_i                  (sif_tx_axis_tvalid[0]),
    .s_axis_tready_o                  (sif_tx_axis_tready[0]),
    .s_axis_tdata_i                   (sif_tx_axis_tdata[0]),
    .s_axis_tkeep_i                   (sif_tx_axis_tkeep[0]),
    .s_axis_tlast_i                   (sif_tx_axis_tlast[0]),
    .package_bank_ready_o             (tx_hs_pkg_bank_ready),
    .package_bank0_sequence_o         (tx_hs_pkg_bank0_sequence),
    .package_bank1_sequence_o         (tx_hs_pkg_bank1_sequence),
    .package_bank0_op_count_o         (tx_hs_pkg_bank0_op_count),
    .package_bank1_op_count_o         (tx_hs_pkg_bank1_op_count),
    .package_ready_pulse_o            (tx_hs_pkg_ready_pulse),
    .package_ready_bank_o             (tx_hs_pkg_ready_bank),
    .package_ready_sequence_o         (tx_hs_pkg_ready_sequence),
    .package_ready_op_count_o         (tx_hs_pkg_ready_op_count),
    .package_error_pulse_o            (tx_hs_pkg_error_pulse),
    .package_last_error_code_o        (tx_hs_pkg_last_error_code),
    .package_accepted_package_count_o (tx_hs_pkg_accepted_package_count),
    .package_accepted_fragment_count_o(tx_hs_pkg_accepted_fragment_count),
    .package_error_count_o            (tx_hs_pkg_error_count),
    .tx_update_expected_sequence_valid_i (tx_update_expected_sequence_valid),
    .tx_update_expected_sequence_i       (tx_update_expected_sequence),
    .tx_update_event_retired_i           (tx_update_event_retired),
    .tx_update_release_i                 (tx_update_release),
    .tx_update_ready_o                   (tx_update_ready),
    .tx_sdout_i                       (tx_spi_sdout_sync),
    .tx_sclk_o                        (tx_spi_sclk),
    .tx_sen_o                         (tx_spi_sen),
    .tx1_sdata0_o                     (tx1_spi_sdata0),
    .tx1_sdata1_o                     (tx1_spi_sdata1),
    .tx2_sdata0_o                     (tx2_spi_sdata0),
    .tx2_sdata1_o                     (tx2_spi_sdata1)
);

// HSB SPI core samples SDIO_in[1] in single-lane mode.
assign spi_sdio_i[0] = {1'b0, 1'b0, afe_spi_sdout_sync, 1'b0};
// HSB SPI instance 1 is unused (TX73L64 SPI is owned by tx_hs_spi_top).
assign spi_sdio_i[1] = 4'b0;

always_comb begin
    case (afe_rx_spi_sdata_mode)
        2'd1: begin
            afe_rx1_spi_sdata = spi_sdio_o[0][0];
            afe_rx2_spi_sdata = 1'b0;
        end
        2'd2: begin
            afe_rx1_spi_sdata = 1'b0;
            afe_rx2_spi_sdata = spi_sdio_o[0][0];
        end
        default: begin
            afe_rx1_spi_sdata = spi_sdio_o[0][0];
            afe_rx2_spi_sdata = spi_sdio_o[0][0];
        end
    endcase
end

always_ff @(posedge apb_clk) begin
    if (apb_rst) begin
        FMC_LA29_P           <= 1'b0;
        FMC_LA29_N           <= 1'b1;
        FMC_LA32_P           <= 1'b0;
        FMC_LA20_N           <= 1'b0;
    end else begin
        FMC_LA29_P           <= spi_sck[0];
        FMC_LA29_N           <= spi_csn[0];
        FMC_LA32_P           <= afe_rx1_spi_sdata;
        FMC_LA20_N           <= afe_rx2_spi_sdata;
    end
end

always_ff @(posedge tx_spi_clk) begin
    if (!tx_spi_rst_n) begin
        FMC_HA11_P           <= 1'b0;
        FMC_HA10_P           <= 1'b1;
        FMC_HA09_P           <= 1'b0;
        FMC_HB21_P           <= 1'b0;
        FMC_HA16_P           <= 1'b0;
        FMC_CLK1_M2C_TERM_P  <= 1'b0;
    end else begin
        FMC_HA11_P           <= tx_spi_sclk;
        FMC_HA10_P           <= tx_spi_sen;
        FMC_HA09_P           <= tx1_spi_sdata0;
        FMC_HB21_P           <= tx1_spi_sdata1;
        FMC_HA16_P           <= tx2_spi_sdata0;
        FMC_CLK1_M2C_TERM_P  <= tx2_spi_sdata1;
    end
end

HOLOLINK_top #(
    .BUILD_REV             (BUILD_REV)
) u_hololink_top (
    .i_sys_rst             (sys_rst),

    // Control plane
    .i_apb_clk             (apb_clk),
    .o_apb_rst             (apb_rst),
    .o_apb_psel            (apb_psel),
    .o_apb_penable         (apb_penable),
    .o_apb_paddr           (apb_paddr),
    .o_apb_pwdata          (apb_pwdata),
    .o_apb_pwrite          (apb_pwrite),
    .i_apb_pready          (apb_pready),
    .i_apb_prdata          (apb_prdata),
    .i_apb_pserr           (apb_pserr),

    // Board enumeration comes from the external EEPROM.
    .o_init_done           (hololink_init_done),

    // Sensor RX
    .i_sif_rx_clk          (sif_rx_clk),
    .o_sif_rx_rst          (sif_rx_rst),
    .i_sif_axis_tvalid     (sif_rx_axis_tvalid),
    .i_sif_axis_tlast      (sif_rx_axis_tlast),
    .i_sif_axis_tdata      (sif_rx_axis_tdata),
    .i_sif_axis_tkeep      (sif_rx_axis_tkeep),
    .i_sif_axis_tuser      (sif_rx_axis_tuser),
    .o_sif_axis_tready     (sif_rx_axis_tready),
    .i_sif_event           (sif_event),

    // Sensor TX
    .i_sif_tx_clk          (sif_tx_clk),
    .o_sif_tx_rst          (sif_tx_rst),
    .o_sif_axis_tvalid     (sif_tx_axis_tvalid),
    .o_sif_axis_tlast      (sif_tx_axis_tlast),
    .o_sif_axis_tdata      (sif_tx_axis_tdata),
    .o_sif_axis_tkeep      (sif_tx_axis_tkeep),
    .o_sif_axis_tuser      (sif_tx_axis_tuser),
    .i_sif_axis_tready     (sif_tx_axis_tready),

    // Host AXIS path
    .i_hif_clk             (hif_clk),
    .o_hif_rst             (hif_rst),
    .i_hif_axis_tvalid     (hif_rx_axis_tvalid),
    .i_hif_axis_tlast      (hif_rx_axis_tlast),
    .i_hif_axis_tdata      (hif_rx_axis_tdata),
    .i_hif_axis_tkeep      (hif_rx_axis_tkeep),
    .i_hif_axis_tuser      (hif_rx_axis_tuser),
    .o_hif_axis_tready     (hif_rx_axis_tready),
    .o_hif_axis_tvalid     (hif_tx_axis_tvalid),
    .o_hif_axis_tlast      (hif_tx_axis_tlast),
    .o_hif_axis_tdata      (hif_tx_axis_tdata),
    .o_hif_axis_tkeep      (hif_tx_axis_tkeep),
    .o_hif_axis_tuser      (hif_tx_axis_tuser),
    .i_hif_axis_tready     (hif_tx_axis_tready),

    // Peripheral control
    .o_spi_csn             (spi_csn),
    .o_spi_sck             (spi_sck),
    .i_spi_sdio            (spi_sdio_i),
    .o_spi_sdio            (spi_sdio_o),
    .o_spi_oen             (spi_oen),
    .i_i2c_scl             (i2c_scl),
    .i_i2c_sda             (i2c_sda),
    .o_i2c_scl_en          (i2c_scl_en),
    .o_i2c_sda_en          (i2c_sda_en),
    .i_gpio                (hl_gpio_i),
    .o_gpio                (hl_gpio),
    .o_gpio_dir            (hl_gpio_dir),

    // Software reset and PTP
    .o_sw_sys_rst          (hl_sw_rst),
    .o_sw_sen_rst          (hl_sw_sen_rst),
    .i_ptp_clk             (ptp_clk),
    .o_ptp_rst             (ptp_rst),
    .o_ptp_sec             (ptp_sec),
    .o_ptp_nanosec         (ptp_nanosec),
    .o_pps                 (pps)
);

lvds_top u_lvds_top (
    .reset              (lvds_reset),
    .apb_clk            (apb_clk),
    .apb_rst            (apb_rst),
    .i_apb_m2s          (lvds_apb_m2s),
    .o_apb_s2m          (lvds_apb_s2m),
    .afe1_dclk_p        (afe1_dclk_p),
    .afe1_fclk_p        (afe1_fclk_p),
    .afe1_dout_p        (afe1_dout_p),
    .afe2_dclk_p        (afe2_dclk_p),
    .afe2_fclk_p        (afe2_fclk_p),
    .afe2_dout_p        (afe2_dout_p),
    .lvds_out_clk       (lvds_out_clk),
    .afe1_aligned_data  (afe1_aligned_data),
    .afe1_aligned_valid (afe1_aligned_valid),
    .afe2_aligned_data  (afe2_aligned_data),
    .afe2_aligned_valid (afe2_aligned_valid),
    .afe_sample_phase   (afe_sample_phase),
    .mixed_stream_clk   (mixed_stream_clk),
    .rx_bf_sync_req     (lvds_rx_bf_sync_req),
    .debug_status       (lvds_debug_status)
);

endmodule
