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


# constraints for DCFIFO sdc
#
# top-level sdc
# convention for module sdc apply_sdc_<module_name>
#
proc apply_sdc_dcfifo {hier_path} {
# gray_rdptr
apply_sdc_dcfifo_rdptr $hier_path
# gray_wrptr
apply_sdc_dcfifo_wrptr $hier_path
}
#
# common constraint setting proc
#
proc apply_sdc_dcfifo_for_ptrs {from_node_list to_node_list} {
# control skew for bits
set_max_skew -from $from_node_list -to $to_node_list -get_skew_value_from_clock_period src_clock_period -skew_value_multiplier 0.8
# path delay (exception for net delay)
if { ![string equal "quartus_syn" $::TimeQuestInfo(nameofexecutable)] } {

#original multiplier 0.8
set_net_delay -from $from_node_list -to $to_node_list -max -get_value_from_clock_period dst_clock_period -value_multiplier 0.8
}
#relax setup and hold calculation
set_max_delay -from $from_node_list -to $to_node_list 100
set_min_delay -from $from_node_list -to $to_node_list -100
}
#
# mstable propgation delay
#
proc apply_sdc_dcfifo_mstable_delay {from_node_list to_node_list} {
# mstable delay
if { ![string equal "quartus_syn" $::TimeQuestInfo(nameofexecutable)] } {
#original multiplier 0.8
set_net_delay -from $from_node_list -to $to_node_list -max -get_value_from_clock_period dst_clock_period -value_multiplier 0.8
}
}
#
# rdptr constraints
#
proc apply_sdc_dcfifo_rdptr {hier_path} {
# get from and to list
set from_node_list [get_keepers $hier_path|auto_generated|*rdptr_g*]
set to_node_list [get_keepers $hier_path|auto_generated|ws_dgrp|dffpipe*|dffe*]
apply_sdc_dcfifo_for_ptrs $from_node_list $to_node_list
# mstable
set from_node_mstable_list [get_keepers $hier_path|auto_generated|ws_dgrp|dffpipe*|dffe*]
set to_node_mstable_list [get_keepers $hier_path|auto_generated|ws_dgrp|dffpipe*|dffe*]
apply_sdc_dcfifo_mstable_delay $from_node_mstable_list $to_node_mstable_list
}
#
# wrptr constraints
#
proc apply_sdc_dcfifo_wrptr {hier_path} {
# control skew for bits
set from_node_list [get_keepers $hier_path|auto_generated|delayed_wrptr_g*]
set to_node_list [get_keepers $hier_path|auto_generated|rs_dgwp|dffpipe*|dffe*]
apply_sdc_dcfifo_for_ptrs $from_node_list $to_node_list
# mstable
set from_node_mstable_list [get_keepers $hier_path|auto_generated|rs_dgwp|dffpipe*|dffe*]
set to_node_mstable_list [get_keepers $hier_path|auto_generated|rs_dgwp|dffpipe*|dffe*]
apply_sdc_dcfifo_mstable_delay $from_node_mstable_list $to_node_mstable_list
}

proc apply_sdc_pre_dcfifo {entity_name} {

set inst_list [get_entity_instances $entity_name]

foreach each_inst $inst_list {

        apply_sdc_dcfifo ${each_inst} 

    }
}
apply_sdc_pre_dcfifo dcfifo
