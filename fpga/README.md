# ti_ultrasound FPGA

## Repo layout

- `rtl/` -- design RTL, plus `rtl/nv_hsb_ip/` for the vendored Holoscan
  Sensor Bridge (HSB) core (see `rtl/nv_hsb_ip/README.md`).
- `build/` -- Quartus project (`ti_ultrasound.qpf` / `ti_ultrasound.qsf`),
  constraints (`build/constraints/`), vendor IP (`build/ip/`), and the
  build script (`build/build_script/`).

## Prerequisites

Quartus Prime Pro 25.1.1 is the intended tool version. It must be installed
and licensed for Stratix 10 device support before building.

## Building

1. Populate `rtl/nv_hsb_ip/` by following the instructions in
   `rtl/nv_hsb_ip/README.md`.
2. Edit `build/build_script/build_ti_ultrasound.sh` and replace
   `<path to quartus installation>` with your local Quartus install path.
3. From `build/build_script`, run:
   ```
   ./build_ti_ultrasound.sh
   ```
   This runs a full Quartus compile (`execute_flow -compile`) and prints a
   timing pass/fail summary at the end.
4. On success, the bitstream is written to
   `build/output_files/ti_ultrasound.sof`, and the flash image (ASX4 mode) to
   `build/output_files/ti_ultrasound.rpd`. Timestamped copies of both are placed
   in `build/build_script/bitfile/`.
