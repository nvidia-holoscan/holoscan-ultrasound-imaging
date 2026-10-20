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
