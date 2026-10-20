# Copyright 2024-2025 Altera Corporation
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the “Software”), to deal in the Software without restriction, except as set forth below, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software;
#
# If a file contained in this Software includes separate license text or a header file with license terms, those terms will supersede this agreement for purposes of that file only, all files without a separate agreement are subject to this agreement;
#
# The Software must be used solely for design and implementation on an Altera product;
#
# You must not use the Software or devices you configure using this Software to violate any internationally recognized human right; and
#
# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#
# Governing Law and Jurisdiction. If you are in the Americas, U.S. and Delaware law governs all disputes arising out of or relating to this agreement without regard to conflict-of-laws principles. The state and federal courts in Wilmington, Delaware will have exclusive jurisdiction over any dispute arising out of or relating to this agreement. If you are in Europe or Africa, the laws of England and Wales govern all matters arising out of or relating to this agreement without regard to conflict-of-laws principles. The courts in England will have exclusive jurisdiction over any dispute arising out of or relating to this agreement. If you are in Asia or Australia, Singapore law governs all disputes arising out of or relating to this agreement without regard to conflict-of-laws principles. The courts in Singapore will have exclusive jurisdiction over any dispute arising out of or relating to this agreement. You and Intel consent to personal jurisdiction and venue in the courts designated for your location. If you are in China, Hong Kong law governs all disputes arising out of or relating to this agreement without regard to conflict-of-laws principles. Any dispute arising out of or relating to this agreement will be subject to arbitration by the Hong Kong International Arbitration Centre, this arbitration agreement will be governed by Hong Kong law, and the seat and location of proceedings will be Hong Kong. The current rules of the HKIAC will apply, except that the arbitration will be referred to a sole arbitrator and the proceedings will be conducted in English. The Arbitral Tribunal may only award monetary damages and may not award injunctive relief or any remedy that requires a party to license any intellectual property rights. Regardless of the above or your location, claims for misappropriation of trade secrets and breach of confidentiality obligations may also be brought in any court that has jurisdiction over the parties if the relief sought is limited to injunctive or other nonmonetary relief. The parties exclude the application of the United Nations Convention on Contracts for the International Sale of Goods (1980).



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
