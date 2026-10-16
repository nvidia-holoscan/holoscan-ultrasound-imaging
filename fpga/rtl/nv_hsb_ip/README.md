# nv_hsb_ip

This folder is intentionally empty in the repo. It holds the Holoscan Sensor
Bridge (HSB) core RTL, which is sourced directly from the public Holoscan
Sensor Bridge repo rather than being vendored here.

To populate it:

1. Clone the Holoscan Sensor Bridge repo at release 2.7.0:
   https://github.com/nvidia-holoscan/holoscan-sensor-bridge/tree/release-2.7.0
2. Copy all source from that repo's `fpga/nv_hsb_ip/` folder into this
   `nv_hsb_ip/` folder.
