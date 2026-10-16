# Set up BUILD_REV parameter
# fpga_ver is YEAR/MONTH.
# timestamp is DAYOFYEAR/HOUR/MINUTE, where DAYOFYEAR is 1 to 366.

set fpga_ver [clock format [clock seconds] -format {%y%m}]
set vendor_id 0000
set timestamp [clock format [clock seconds] -format {%j%H%M}]
set timestamp_int [scan $timestamp "%d"]
set timehex [format "%08X" $timestamp_int]
set build_rev $timehex$vendor_id
set prefix "48'h"

project_open ti_ultrasound
set_parameter -name BUILD_REV $prefix$build_rev
