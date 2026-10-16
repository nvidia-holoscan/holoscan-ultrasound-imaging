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

file mkdir bitfile

# Open the project
project_open "../ti_ultrasound.qpf"

load_package flow

# Run compilation
execute_flow -compile

# Print a timing pass/fail summary. On Pass, just the overall Timing Closure
# line; on Fail, the Timing Closure sub-rows (Setup/Hold/Recovery/Removal/
# Skew/etc.) so it's clear which one(s) failed. The Design Closure Summary
# row itself (which also reflects Design Assistant/Unconstrained Paths) is
# intentionally not included here.
set sta_rpt [open "output_files/ti_ultrasound.sta.rpt" r]
set timing_line ""
set sub_lines {}
set in_timing_block 0
set timing_passed 0
while {[gets $sta_rpt line] >= 0} {
    if {!$in_timing_block} {
        if {[string match "*Timing Closure*" $line]} {
            set timing_line $line
            set in_timing_block 1
            if {[string match "*Pass*" $line]} {
                set timing_passed 1
            }
        }
        continue
    }
    if {[string match "*Design Assistant Summary*" $line]} {
        break
    }
    lappend sub_lines $line
}
close $sta_rpt

puts ""
puts "===== TIMING SUMMARY ====="
if {$timing_passed} {
    puts $timing_line
} else {
    puts $timing_line
    foreach sub_line $sub_lines {
        puts $sub_line
    }
}
puts "==========================="

## Close the project

project_close

set dateTime [clock format [clock seconds] -format "%Y%m%d%H%M%S"]
set dateTime [format %x $dateTime]

file copy output_files/ti_ultrasound.sof build_script/bitfile/ti_ultrasound_$dateTime.sof
file copy output_files/ti_ultrasound.rpd build_script/bitfile/ti_ultrasound_$dateTime.rpd
