# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

######################################### Internal False Paths ###############################

#ECB RD/WR Controller
# Header is sent with valid signal. Valid is synchronized into aclk domain so header should be stable when the synchronized valid asserts
set_false_path -from [get_keepers -no_duplicates {u_hololink_top|u_rx_ls_parser|ecb_rdwr_ctrl_inst|ecb_cmd*}]
set_false_path -from [get_keepers -no_duplicates {u_hololink_top|u_rx_ls_parser|ecb_rdwr_ctrl_inst|ecb_flags*}]
set_false_path -from [get_keepers -no_duplicates {u_hololink_top|u_rx_ls_parser|ecb_rdwr_ctrl_inst|ecb_seq*}]
set_false_path -from [get_keepers -no_duplicates {u_hololink_top|u_rx_ls_parser|ecb_rdwr_ctrl_inst|cmd_addr*}]
set_false_path -from [get_keepers -no_duplicates {u_hololink_top|u_rx_ls_parser|ecb_rdwr_ctrl_inst|cmd_dout*}]


#SW reset to reset sync modules
set_false_path -from [get_keepers -no_duplicates {u_hololink_top|u_rst_gen|u_apb_rst|srst*}] -to [get_keepers -no_duplicates {u_hololink_top|u_rst_gen|u_hif_rst|*}]
set_false_path -from [get_keepers -no_duplicates {u_hololink_top|u_rst_gen|u_apb_rst|srst*}] -to [get_keepers -no_duplicates {u_hololink_top|u_rst_gen|*u_sif_rx_rst|*}]
set_false_path -from [get_keepers -no_duplicates {u_hololink_top|u_rst_gen|u_apb_rst|srst*}] -to [get_keepers -no_duplicates {u_hololink_top|u_rst_gen|*u_sif_tx_rst|*}]
set_false_path -from [get_keepers -no_duplicates {u_hololink_top|u_rst_gen|u_apb_rst|srst*}] -to [get_keepers -no_duplicates {*u_dc_fifo*}]
set_false_path -from [get_keepers -no_duplicates {u_hololink_top|u_rst_gen|u_apb_rst|srst*}] -to [get_keepers -no_duplicates {u_hololink_top|u_rst_gen|u_ptp_rst|*}]


# Reset from register
set_false_path -from [get_keepers -no_duplicates {u_hololink_top|u_glb_ctrl|u_reg_map|*.u_ctrl_cdc|a2b_reg_sync*}] -to [get_keepers -no_duplicates {u_hololink_top|u_rst_gen|u_apb_rst|aasr_rst}]

#EEPROM Info is static after fetching
set_false_path -from [get_keepers -no_duplicates {u_hololink_top|u_eeprom_info|*}] -to [get_keepers -no_duplicates {u_hololink_top|gen_rx_parser*}]
set_false_path -from [get_keepers -no_duplicates {u_hololink_top|u_eeprom_info|*}] -to [get_keepers -no_duplicates {u_hololink_top|gen_eth_pkt*}]
set_false_path -from [get_keepers -no_duplicates {u_hololink_top|dev_mac_addr*}] -to [get_keepers -no_duplicates {*}]
set_false_path -from [get_keepers -no_duplicates {u_hololink_top|u_eeprom_info|*}] -to [get_keepers -no_duplicates {u_hololink_top|u_rx_ls_parser|u_bootp|*}]

#s_apb_ram
# Write data to RAM is latched in APB clock domain. The RAM wren is synchronized from the APB clock domain to the HIF clock domain. So the write data is stable when the wren asserts.
set_false_path -from [get_keepers -no_duplicates {u_hololink_top|u_dp_pkt_top|u_config_ram|reg_wdata*}] -to [get_keepers -no_duplicates {u_hololink_top|u_dp_pkt_top|u_config_ram|blk_mem*}]
# Write addr. Same as above.
set_false_path -from [get_keepers -no_duplicates {u_hololink_top|u_dp_pkt_top|u_config_ram|reg_addr*}] -to [get_keepers -no_duplicates {u_hololink_top|u_dp_pkt_top|u_config_ram|blk_mem*}]
# Read data is latched into the hif_clk domain from logic in the APB domain. The data should be stable before being consumed on the APB interface using rden that is synchronized from the hif_clk to the apb_clk.
set_false_path -from [get_keepers -no_duplicates {u_hololink_top|u_dp_pkt_top|u_config_ram|reg_addr*}] -to [get_keepers -no_duplicates {u_hololink_top|u_dp_pkt_top|u_config_ram|reg_rdata_q*}]
# The read data latched in the hif_clk also has dependency on the reg_address, which has dependency on reg_wren and reg_rden. The apb_clk signals should be stable so latching in the hif_clk should be ok. 
set_false_path -from [get_keepers -no_duplicates {u_hololink_top|u_dp_pkt_top|u_config_ram|reg_wren}] -to [get_keepers -no_duplicates {u_hololink_top|u_dp_pkt_top|u_config_ram|reg_rdata_q*}]
set_false_path -from [get_keepers -no_duplicates {u_hololink_top|u_dp_pkt_top|u_config_ram|reg_rden}] -to [get_keepers -no_duplicates {u_hololink_top|u_dp_pkt_top|u_config_ram|reg_rdata_q*}]

# Read data going back to the apb_clk domain. Will be stable prior to the signal indicating the APB command is done.
set_false_path -from [get_keepers -no_duplicates {u_hololink_top|u_dp_pkt_top|u_config_ram|reg_rdata_q*}] -to [get_keepers -no_duplicates {u_hololink_top|u_apb_intc|u_apb_ff_lvl2|apb_s2m_q.prdata*}]


##Packetizer

##APB CDC - updated from original as that had only at hololink and below.
set_false_path -from [get_keepers -no_duplicates {*|*u_ctrl_cdc*|a_reg_qq*}] -to [get_keepers -no_duplicates {*|*u_ctrl_cdc*|a2b_reg_sync*}]
##APB CDC false path from the reg_cdc module valid latched in slow to fast
set_false_path -from [get_keepers -no_duplicates {*|*u_ctrl_cdc*|a_val_qq*}] -to [get_keepers -no_duplicates {*|*u_ctrl_cdc*|a2b_val_sync*}]
##APB CDC b2b sync
set_false_path -from [get_keepers -no_duplicates {*|*u_ctrl_cdc*|a2b_val_sync*}] -to [get_keepers -no_duplicates {*|*u_ctrl_cdc*|b2a_ack_sync*}]

#Async reset paths
set_false_path -from [get_keepers -no_duplicates {hl_sw_rst_sync}] -to [get_keepers -no_duplicates {u_hololink_top|u_rst_gen|*}]
set_false_path -from [get_keepers -no_duplicates {hl_sw_rst_sync}] -to [get_keepers -no_duplicates {tx_spi_rst_sync|*}]
set_false_path -from [get_keepers -no_duplicates {rst_pb_debounce|debounce_sync[2]}] -to [get_keepers -no_duplicates {u_hololink_top|u_rst_gen|*}]
set_false_path -from [get_keepers -no_duplicates {rst_pb_debounce|debounce_sync[2]}] -to [get_keepers -no_duplicates {tx_spi_rst_sync|*}]
set_false_path -from [get_keepers -no_duplicates {rst_and_lock_sync|filter_pipe*}] -to [get_keepers -no_duplicates {u_hololink_top|u_rst_gen|*}]
set_false_path -from [get_keepers -no_duplicates {rst_and_lock_sync|filter_pipe*}] -to [get_keepers -no_duplicates {tx_spi_rst_sync|*}]
# PLL Locks
set_false_path -from [get_keepers -no_duplicates {fpga_clk_pll|iopll_0|stratix10_altera_iopll_i|s10_iopll.fourteennm_pll~pll_e_reg__nff}] -to [get_keepers -no_duplicates {u_hololink_top|u_rst_gen|*|aasr_rst*}]
set_false_path -from [get_keepers -no_duplicates {hif_pll|iopll_0|stratix10_altera_iopll_i|s10_iopll.fourteennm_pll~pll_e_reg__nff}] -to [get_keepers -no_duplicates {u_hololink_top|u_rst_gen|*|aasr_rst*}]
set_false_path -from [get_keepers -no_duplicates {hif_pll|iopll_0|stratix10_altera_iopll_i|s10_iopll.fourteennm_pll~pll_e_reg__nff}] -to [get_keepers -no_duplicates {hif_pll_locked_sync}]
set_false_path -from [get_keepers -no_duplicates {hif_pll_locked_sync}] -to [get_keepers -no_duplicates {u_hololink_top|u_rst_gen|*}]
set_false_path -from [get_keepers -no_duplicates {fpga_clk_pll_lock_sync}] -to [get_keepers -no_duplicates {u_hololink_top|u_rst_gen|*}]

#PB Reset
set_false_path -from [get_ports {FPGA_PB_RESET_L}] -to [get_registers {*}]

#Tx Stream Buffer and Pause
set_false_path -from [get_keepers -no_duplicates {u_hololink_top|gen_stx_buf[*].u_tx_stream_buffer|u_sensor_tx_data_regs|[0].u_ctrl_cdc|a2b_reg_sync[*]}] -to [get_keepers -no_duplicates {u_hololink_top|gen_stx_buf[*].u_tx_stream_buffer|dbg_ena_output_sync}]
set_false_path -from [get_keepers -no_duplicates {u_hololink_top|u_rst_gen|u_hif_rst|srst[1]*}] -to [get_keepers -no_duplicates {u_hololink_top|gen_stx_buf[*].u_tx_stream_buffer|u_axis_tx_buffer|axis_buffer_cdc|u_dc_fifo|auto_generated|rdaclr|dffe*a[*]}]


proc apply_sdc_cdc {hier_path} {
   set from_node_list [get_keepers -no_duplicates $hier_path|filter_pipe[*][0]]
   set to_node_list [get_keepers -no_duplicates $hier_path|filter_pipe[*][1]]
   
   # False path on input pin CDC
   set_false_path -to $from_node_list 
}

set inst_list [get_entity_instances data_sync]
foreach each_inst $inst_list {
   apply_sdc_cdc ${each_inst} 
}
