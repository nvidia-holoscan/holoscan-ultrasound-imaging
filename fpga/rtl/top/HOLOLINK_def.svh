// SPDX-FileCopyrightText: Copyright (c) 2023-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
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

`ifndef HOLOLINK_def
`define HOLOLINK_def

package HOLOLINK_pkg;

//-----------------------------------------------------
// Holoscan IP Host Clock Frequency
//
// Used for internal timer calculation
//-----------------------------------------------------

  `define HIF_CLK_FREQ  201416016

//-----------------------------------------------------
// Holoscan IP APB Clock Frequency
//
// Used for I2C clock divider setting
//-----------------------------------------------------

  `define APB_CLK_FREQ  100000000

//-----------------------------------------------------
// Holoscan IP PTP Clock Frequency
//
// Used for internal timer calculation
//-----------------------------------------------------

  `define PTP_CLK_FREQ  100707500

//-----------------------------------------------------
// Board Info Enumeration
//-----------------------------------------------------

  //UUID is used to uniquely identify the board. The UUID is sent over BOOTP.
  `define UUID                   128'hAC19_2BB6_24BE_C23F_D07E_2E13_0D9D_2F0F

  // Define ENUM_EEPROM if board info is stored in an external EEPROM.
  // Otherwise, soft MAC address and Board Serial Number can be used
  `define ENUM_EEPROM

  `ifdef ENUM_EEPROM
    `define EEPROM_REG_ADDR_BITS 8                //EEPROM Register Address Bits. Valid values: 8, 16
  `endif

//-----------------------------------------------------
// Sensor Interface
//-----------------------------------------------------

  `define DATAPATH_WIDTH  512                 // Sensor interface data width. This should be set to MAX width between SIF RX and TX widths
                                             // Valid values: 8, 16, 32, 64, 128, 256, 512, 1024
  `define DATAKEEP_WIDTH  `DATAPATH_WIDTH/8  // Sensor interface data keep width
  `define DATAUSER_WIDTH  1                  // Sensor interface data user width

//-----------------------------------------------------
// Sensor RX IF
//-----------------------------------------------------

  `define SENSOR_RX_IF_INST  1               // Number of Sensor RX Interface. Valid values: undefined, 1 - 32
  //----------------------------------------------------------------------------------
  //If no Sensor RX Interfaces are used, then comment out "`define SENSOR_RX_IF_INST"
  //This will remove Sensor RX IF I/Os from HOLOLINK_top module.
  //The same applies for "SENSOR_TX_IF_INST", "SPI_INST", and "I2C_INST" definitions.
  //----------------------------------------------------------------------------------

  `ifdef SENSOR_RX_IF_INST
    //`define SIF_RX_DATA_GEN             // If defined, Sensor RX Data Generator is instantiated. This can be used for bring-up.

    localparam integer  SIF_RX_WIDTH           [`SENSOR_RX_IF_INST-1:0] = {512}; // Define width for each interface.
    //--------------------------------------------------------------------------------
    // Sensor RX Packetizer Parameters
    // If RX_PACKETIZER_EN is set to 0, then Packetizer is disabled for that Sensor RX interface.
    //--------------------------------------------------------------------------------
    localparam integer  SIF_RX_PACKETIZER_EN   [`SENSOR_RX_IF_INST-1:0] = '{default:0};
    localparam integer  SIF_RX_VP_COUNT        [`SENSOR_RX_IF_INST-1:0] = {2};
    localparam integer  SIF_RX_SORT_RESOLUTION [`SENSOR_RX_IF_INST-1:0] = {2};
    localparam integer  SIF_RX_VP_SIZE         [`SENSOR_RX_IF_INST-1:0] = {64};
    localparam integer  SIF_RX_NUM_CYCLES      [`SENSOR_RX_IF_INST-1:0] = {3};
  `endif

//-----------------------------------------------------
// Sensor TX IF
//-----------------------------------------------------

  `define SENSOR_TX_IF_INST  1                // Number of Sensor TX Interface. Valid values: undefined, 1 - 32

  `ifdef SENSOR_TX_IF_INST
    localparam integer  SIF_TX_WIDTH           [`SENSOR_TX_IF_INST-1:0] = {512};
    localparam integer  SIF_TX_BUF_SIZE        [`SENSOR_TX_IF_INST-1:0] = '{default:32};
  `endif

//-----------------------------------------------------
// Host IF
//-----------------------------------------------------

  `define HOST_WIDTH      512                // Host interface data width.                     Valid values: 8, 16, 32, 64, 128, 256, 512
  `define HOSTKEEP_WIDTH  `HOST_WIDTH/8     // Host interface data keep width
  `define HOSTUSER_WIDTH  1                 // Host interface data user width
  `define HOST_IF_INST    2                 // Host interface instantiation number.           Valid values: 1 - 32
  `define HOST_MTU        4096              // Maximum Transmission Unit for Ethernet packet. Valid values: 1500, 4096

//------------------------------------------------------------------------------
// Peripheral Control
//------------------------------------------------------------------------------

  `define SPI_INST  2   // SPI interface instantiation number. Valid values: undefined, 1 - 8
  `define I2C_INST  1   // I2C interface instantiation number. Valid values: undefined, 1 - 8
  //`define UART_INST 1   // UART interface instantiation number. Valid values: undefined, 1
  `define GPIO_INST 16  // INOUT GPIO instantiation number.    Valid values: 1 - 255

  localparam [`GPIO_INST-1:0] GPIO_RESET_VALUE = 16'h0000;

  `define DISABLE_COE

//------------------------------------------------------------------------------
// Register IF
//
// Creates <REG_INST> number of APB register interfaces for user logic access
//------------------------------------------------------------------------------

  `define REG_INST 8

endpackage: HOLOLINK_pkg
`endif
