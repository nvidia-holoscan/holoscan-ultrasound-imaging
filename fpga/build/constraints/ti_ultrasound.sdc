# (C) 2001-2022 Intel Corporation. All rights reserved.
# Your use of Intel Corporation's design tools, logic functions and other 
# software and tools, and its AMPP partner logic functions, and any output 
# files from any of the foregoing (including device programming or simulation 
# files), and any associated documentation or information are expressly subject 
# to the terms and conditions of the Intel Program License Subscription 
# Agreement, Intel FPGA IP License Agreement, or other applicable 
# license agreement, including, without limitation, that your use is for the 
# sole purpose of programming logic devices manufactured by Intel and sold by 
# Intel or its authorized distributors.  Please refer to the applicable 
# agreement for further details.



create_clock -name {ref_clk}            -period 6.400  -waveform { 0.000 3.200 } [get_ports {FPGA_ETH_REFCLK}]
create_clock -name {altera_reserved_tck} -period 40                               [get_ports {altera_reserved_tck}]

set OSC_CLK         [get_clocks ALTERA_INSERTED_INTOSC_FOR_TRS|divided_osc_clk]
set CLK100          [get_clocks fpga_clk_pll|iopll_0_fpga_clk_100]
set REF_CLK         [get_clocks ref_clk]

# LVDS parallel clocks are generated inside the AFE LVDS IPs. Treat the three
# receive groups as independent clock domains; intentional crossings use
# APB CDC synchronizers or the LVDS group assembler async FIFOs.
#set LVDS_AFE1_CORE  [get_clocks -nowarn {u_lvds_top|u_afe1_15ch_if|u_lvds_ip|lvds_0|core|arch_inst|pll_inst|internal_pll_outclk4}]
#set LVDS_AFE2_CORE  [get_clocks -nowarn {u_lvds_top|u_afe2_11ch_if|u_lvds_ip|lvds_0|core|arch_inst|pll_inst|internal_pll_outclk4}]
#set LVDS_MIXED_CORE [get_clocks -nowarn {u_lvds_top|u_mixed_6ch_if|u_lvds_ip|lvds_0|core|arch_inst|pll_inst|internal_pll_outclk4}]

#if {[llength $LVDS_AFE1_CORE] && [llength $LVDS_AFE2_CORE] && [llength $LVDS_MIXED_CORE]} {
#    set_clock_groups -asynchronous         -group $LVDS_AFE1_CORE         -group $LVDS_AFE2_CORE         -group $LVDS_MIXED_CORE         -group $CLK100         -group $REF_CLK         -group $OSC_CLK
#}

# RX_BF_SYNC request is generated in sync_ctrl and sampled by explicit
# two-flop synchronizers in each LVDS RX clock domain. Cut only the async
# arrival at the first sync flop; keep the rest of the LVDS logic timed.
set_false_path \
    -to [get_keepers -no_duplicates {u_lvds_top|u_afe1_15ch_if|u_rx_group|rx_bf_sync_req_sync_q[0]}]
set_false_path \
    -to [get_keepers -no_duplicates {u_lvds_top|u_afe2_11ch_if|u_rx_group|rx_bf_sync_req_sync_q[0]}]
set_false_path \
    -to [get_keepers -no_duplicates {u_lvds_top|u_mixed_6ch_if|u_rx_group|rx_bf_sync_req_sync_q[0]}]

# rst_sys is generated in the 100 MHz system domain and sampled by reset_sync
# instances in the LVDS RX clock domains. Cut only the async arrival at the
# first reset_sync flop; keep the synchronized reset outputs timed.
set_false_path \
    -to [get_keepers -no_duplicates {u_lvds_top|u_afe1_15ch_if|u_rx_reset_sync|aasr_rst}]
set_false_path \
    -to [get_keepers -no_duplicates {u_lvds_top|u_afe2_11ch_if|u_rx_reset_sync|aasr_rst}]
set_false_path \
    -to [get_keepers -no_duplicates {u_lvds_top|u_mixed_6ch_if|u_rx_reset_sync|aasr_rst}]


for {set x 0} {$x < 2} {incr x} {
        set CORECLK         [get_clocks ethernet_100gb[$x].eth_top_inst|qsfp_inst|eth_100g_inst|alt_ehipc3_0|alt_ehipc3_hard_inst|E100GX4_FEC.altera_xcvr_native_inst|xcvr_native_s10_etile_0_example_design_4ln_ptp|tx_clkout|ch0]
        
        set CORECLK_PTP     [get_clocks ethernet_100gb[$x].eth_top_inst|qsfp_inst|eth_100g_inst|alt_ehipc3_0|alt_ehipc3_hard_inst|E100GX4_FEC*.altera_xcvr_native_inst|xcvr_native_s10_etile_0_example_design_4ln_ptp*|tx_clkout|ch0]
        set CORECLK_PTP3    [get_clocks ethernet_100gb[$x].eth_top_inst|qsfp_inst|eth_100g_inst|alt_ehipc3_0|alt_ehipc3_hard_inst|E100GX4_FEC*.altera_xcvr_native_inst|xcvr_native_s10_etile_0_example_design_4ln_ptp*|tx_transfer_clk|ch3]
        
        
        set RX_CLK0         [get_clocks ethernet_100gb[$x].eth_top_inst|qsfp_inst|eth_100g_inst|alt_ehipc3_0|alt_ehipc3_hard_inst|E100GX4_FEC*.altera_xcvr_native_inst|xcvr_native_s10_etile_0_example_design_4ln_ptp*|rx_transfer_clk|ch0]
        set RX_CLK1         [get_clocks ethernet_100gb[$x].eth_top_inst|qsfp_inst|eth_100g_inst|alt_ehipc3_0|alt_ehipc3_hard_inst|E100GX4_FEC*.altera_xcvr_native_inst|xcvr_native_s10_etile_0_example_design_4ln_ptp*|rx_transfer_clk|ch1]
        set RX_CLK2         [get_clocks ethernet_100gb[$x].eth_top_inst|qsfp_inst|eth_100g_inst|alt_ehipc3_0|alt_ehipc3_hard_inst|E100GX4_FEC*.altera_xcvr_native_inst|xcvr_native_s10_etile_0_example_design_4ln_ptp*|rx_transfer_clk|ch2]
        set RX_CLK3         [get_clocks ethernet_100gb[$x].eth_top_inst|qsfp_inst|eth_100g_inst|alt_ehipc3_0|alt_ehipc3_hard_inst|E100GX4_FEC*.altera_xcvr_native_inst|xcvr_native_s10_etile_0_example_design_4ln_ptp*|rx_transfer_clk|ch3]
        
        set TX_CLK0         [get_clocks ethernet_100gb[$x].eth_top_inst|qsfp_inst|eth_100g_inst|alt_ehipc3_0|alt_ehipc3_hard_inst|E100GX4_FEC*.altera_xcvr_native_inst|xcvr_native_s10_etile_0_example_design_4ln_ptp*|tx_transfer_clk|ch0]
        set TX_CLK1         [get_clocks ethernet_100gb[$x].eth_top_inst|qsfp_inst|eth_100g_inst|alt_ehipc3_0|alt_ehipc3_hard_inst|E100GX4_FEC*.altera_xcvr_native_inst|xcvr_native_s10_etile_0_example_design_4ln_ptp*|tx_transfer_clk|ch1]
        set TX_CLK2         [get_clocks ethernet_100gb[$x].eth_top_inst|qsfp_inst|eth_100g_inst|alt_ehipc3_0|alt_ehipc3_hard_inst|E100GX4_FEC*.altera_xcvr_native_inst|xcvr_native_s10_etile_0_example_design_4ln_ptp*|tx_transfer_clk|ch2]
        set TX_CLK3         [get_clocks ethernet_100gb[$x].eth_top_inst|qsfp_inst|eth_100g_inst|alt_ehipc3_0|alt_ehipc3_hard_inst|E100GX4_FEC*.altera_xcvr_native_inst|xcvr_native_s10_etile_0_example_design_4ln_ptp*|tx_transfer_clk|ch3]
        
        set RX_CLK0_PTP     [get_clocks ethernet_100gb[$x].eth_top_inst|qsfp_inst|eth_100g_inst|alt_ehipc3_0|alt_ehipc3_hard_inst|E100GX4_FEC*.altera_xcvr_native_inst|xcvr_native_s10_etile_0_example_design_4ln_ptp*|rx_transfer_clk|ch0]
        set RX_CLK1_PTP     [get_clocks ethernet_100gb[$x].eth_top_inst|qsfp_inst|eth_100g_inst|alt_ehipc3_0|alt_ehipc3_hard_inst|E100GX4_FEC*.altera_xcvr_native_inst|xcvr_native_s10_etile_0_example_design_4ln_ptp*|rx_transfer_clk|ch1]
        set RX_CLK2_PTP     [get_clocks ethernet_100gb[$x].eth_top_inst|qsfp_inst|eth_100g_inst|alt_ehipc3_0|alt_ehipc3_hard_inst|E100GX4_FEC*.altera_xcvr_native_inst|xcvr_native_s10_etile_0_example_design_4ln_ptp*|rx_transfer_clk|ch2]
        set RX_CLK3_PTP     [get_clocks ethernet_100gb[$x].eth_top_inst|qsfp_inst|eth_100g_inst|alt_ehipc3_0|alt_ehipc3_hard_inst|E100GX4_FEC*.altera_xcvr_native_inst|xcvr_native_s10_etile_0_example_design_4ln_ptp*|rx_transfer_clk|ch3]
        
        set TX_CLK0_PTP     [get_clocks ethernet_100gb[$x].eth_top_inst|qsfp_inst|eth_100g_inst|alt_ehipc3_0|alt_ehipc3_hard_inst|E100GX4_FEC*.altera_xcvr_native_inst|xcvr_native_s10_etile_0_example_design_4ln_ptp*|tx_transfer_clk|ch0]
        set TX_CLK1_PTP     [get_clocks ethernet_100gb[$x].eth_top_inst|qsfp_inst|eth_100g_inst|alt_ehipc3_0|alt_ehipc3_hard_inst|E100GX4_FEC*.altera_xcvr_native_inst|xcvr_native_s10_etile_0_example_design_4ln_ptp*|tx_transfer_clk|ch1]
        set TX_CLK2_PTP     [get_clocks ethernet_100gb[$x].eth_top_inst|qsfp_inst|eth_100g_inst|alt_ehipc3_0|alt_ehipc3_hard_inst|E100GX4_FEC*.altera_xcvr_native_inst|xcvr_native_s10_etile_0_example_design_4ln_ptp*|tx_transfer_clk|ch2]
        set TX_CLK3_PTP     [get_clocks ethernet_100gb[$x].eth_top_inst|qsfp_inst|eth_100g_inst|alt_ehipc3_0|alt_ehipc3_hard_inst|E100GX4_FEC*.altera_xcvr_native_inst|xcvr_native_s10_etile_0_example_design_4ln_ptp*|tx_transfer_clk|ch3]



    if {! [is_post_route]} {
       # fitter; 15% at 402.83 MHz
       set_clock_uncertainty -setup -add -from $CORECLK 0.372
       set_clock_uncertainty -setup -add -from $CORECLK_PTP 0.372
    } else {
       # everywhere else
    }
    
    set_clock_groups -asynchronous -group  $CORECLK       -group $OSC_CLK  -group $REF_CLK ;#-group $CLK100 ;#-group $IOPLL_REF
    set_clock_groups -asynchronous -group  $CORECLK_PTP   -group $OSC_CLK  -group $REF_CLK ;#-group $CLK100 ;#-group $IOPLL_REF
    set_clock_groups -asynchronous -group  $CORECLK_PTP3  -group $OSC_CLK  -group $REF_CLK ;#-group $CLK100 ;#-group $IOPLL_REF

    set_clock_groups -asynchronous -group  $RX_CLK0       -group $OSC_CLK  -group $REF_CLK -group $CLK100 ;#-group $IOPLL_REF
    set_clock_groups -asynchronous -group  $RX_CLK1       -group $OSC_CLK  -group $REF_CLK -group $CLK100 ;#-group $IOPLL_REF
    set_clock_groups -asynchronous -group  $TX_CLK0       -group $OSC_CLK  -group $REF_CLK -group $CLK100 ;#-group $IOPLL_REF
    set_clock_groups -asynchronous -group  $TX_CLK1       -group $OSC_CLK  -group $REF_CLK -group $CLK100 ;#-group $IOPLL_REF 

    set_clock_groups -asynchronous -group  $RX_CLK2       -group $OSC_CLK  -group $REF_CLK -group $CLK100 ;#-group $IOPLL_REF
    set_clock_groups -asynchronous -group  $RX_CLK3       -group $OSC_CLK  -group $REF_CLK -group $CLK100 ;#-group $IOPLL_REF
    set_clock_groups -asynchronous -group  $TX_CLK2       -group $OSC_CLK  -group $REF_CLK -group $CLK100 ;#-group $IOPLL_REF
    set_clock_groups -asynchronous -group  $TX_CLK3       -group $OSC_CLK  -group $REF_CLK -group $CLK100 ;#-group $IOPLL_REF

    set_clock_groups -asynchronous -group  $RX_CLK0_PTP   -group $OSC_CLK  -group $REF_CLK -group $CLK100 ;#-group $IOPLL_REF
    set_clock_groups -asynchronous -group  $RX_CLK1_PTP   -group $OSC_CLK  -group $REF_CLK -group $CLK100 ;#-group $IOPLL_REF
    set_clock_groups -asynchronous -group  $TX_CLK0_PTP   -group $OSC_CLK  -group $REF_CLK -group $CLK100 ;#-group $IOPLL_REF
    set_clock_groups -asynchronous -group  $TX_CLK1_PTP   -group $OSC_CLK  -group $REF_CLK -group $CLK100 ;#-group $IOPLL_REF 

    set_clock_groups -asynchronous -group  $RX_CLK2_PTP   -group $OSC_CLK  -group $REF_CLK -group $CLK100 ;#-group $IOPLL_REF
    set_clock_groups -asynchronous -group  $RX_CLK3_PTP   -group $OSC_CLK  -group $REF_CLK -group $CLK100 ;#-group $IOPLL_REF
    set_clock_groups -asynchronous -group  $TX_CLK2_PTP   -group $OSC_CLK  -group $REF_CLK -group $CLK100 ;#-group $IOPLL_REF
    set_clock_groups -asynchronous -group  $TX_CLK3_PTP   -group $OSC_CLK  -group $REF_CLK -group $CLK100 ;#-group $IOPLL_REF

}

set_false_path  -to *qsfp*_inst|eth_100g_inst|alt_ehipc3_0|RST_CTRL_100G.alt_ehipc3_reset_controller_inst|status_rx_reset_synchronizers|resync_chains[0].synchronizer_nocut|din_s1
set_false_path  -to *qsfp*_inst|*.synchronizer_nocut|din_s1*
#set_false_path  -to *qsfp*_inst|eth_100g_inst|alt_ehipc3_0|rx_adapt_dropped_frame_count_sync_inst|data_out*
#set_false_path  -to *qsfp*_inst|*synchronizer_nocut_inst|din_s1*
#set_false_path  -to *qsfp*_inst|*|rx_adapt_clr_sync_inst|din_s1
#set_false_path  -to *qsfp*_inst|*|ack_sync_0|din_s1
#set_false_path  -to *qsfp*_inst|*valid_sync_0|din_s1
#set_false_path  -from *|alt_ehipc3_0|undersized_frame_csr_rst

# RX DC FIFO Arst
set_false_path -from [get_keepers -no_duplicates {ethernet_100gb[*].eth_top_inst|qsfp_inst|eth_rst_count_n[0]}] -to [get_keepers -no_duplicates {ethernet_100gb[*].eth_top_inst|qsfp_inst|u_eth_rx_dc_fifo|auto_generated|rdaclr|dffe*a[0]}]
# Eth ready to sys_rst
set_false_path -from [get_keepers -no_duplicates {ethernet_100gb[*].eth_top_inst|qsfp_inst|eth_rst_count_n[0]}] -to [get_keepers -no_duplicates {ethernet_100gb[*].eth_top_inst|qsfp_inst|*|aasr_rst*}]
# TX DC FIFO Arst
set_false_path -from [get_keepers -no_duplicates {ethernet_100gb[*].eth_top_inst|qsfp_inst|u_hif_rst_sync|srst[1]}] -to [get_keepers -no_duplicates {ethernet_100gb[*].eth_top_inst|qsfp_inst|u_eth_tx_dc_fifo|auto_generated|rdaclr|dffe*a[0]}]

# From Timequest cookbook
set_clock_groups -asynchronous -group [get_clocks altera_reserved_tck]

# From "AV SoC Golden Hardware Reference Design"
set_input_delay  -clock altera_reserved_tck -clock_fall 3 [get_ports altera_reserved_tdi]
set_input_delay  -clock altera_reserved_tck -clock_fall 3 [get_ports altera_reserved_tms]
set_output_delay -clock altera_reserved_tck 3 [get_ports altera_reserved_tdo]


#**************************************************************
# Output
#**************************************************************

set_false_path -to [get_ports *STX_DBG_LED*]
set_false_path -to [get_ports *FPGA_HB_LED*]

set_false_path -from [get_ports FPGA_EEPROM_SCL]
set_false_path -from [get_ports FPGA_EEPROM_SDA]

## Hololink IP Constraints
source "[file dirname [info script]]/hololink.sdc"

## DCFIFO Constraints
source "[file dirname [info script]]/dcfifo.sdc"
