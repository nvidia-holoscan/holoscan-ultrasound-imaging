# PGX SDK image

Run `./setup` from the project folder on the host. It fetches HSB into
`.runtime/hsb`, builds `nvus:0.1.0` if missing, prepares container `nvus`,
and installs the package in its `/opt/nvus-env`.

HSB is an external dependency, pinned to public
[2.7.0](https://github.com/nvidia-holoscan/holoscan-sensor-bridge/releases/tag/2.7.0),
commit `1df133d4c78a6921249be4b147d65554aa8efa68`.
All six [patches](patches/) are required, in filename order:

- `00-roce-transmitter-api.patch`: native C++ transmitter API.
- `10-roce-receive-failure.patch`: detect fatal receive errors and wake consumers.
- `20-roce-receive-diagnostics.patch`: completion, metadata and async-event logging.
- `30-roce-receive-queue.patch`: queue depth and early burst retention.
- `40-roce-tx-memory-ordering.patch`: GPU TX buffer visibility.
- `50-ultrasound-fpga-uuid.patch`: additional TI FPGA UUID; default unchanged.

The Dockerfile applies them in filename order during image build only;
already-applied patches are accepted. Incompatible patches fail the build.
The local SDK checkout stays clean; startup never patches it.
After patch or package changes, run `./setup --rebuild` with all `nvus` jobs stopped.
It builds first, removes the idle old container, and installs a replacement.
Container-local files are lost; keep persistent files in the mounted project.
Plain `./setup` reuses existing images and containers.
An image with a different HSB revision is refused until `--rebuild` is requested.
Setup updates only clean SDK checkouts and never discards local SDK edits.

The image retains the patches in `/opt/nvus/hsb-patches` and the SDK revision
in its Docker labels. Existing `demo` containers and host environments are not used
or changed. Repeat setup preserves the installation and starts `nvus` if stopped.

Prerequisites: access to the SDK repository, NVIDIA drivers, Docker with BuildKit,
NVIDIA Container Toolkit, NIC/RDMA drivers, and access to NVIDIA's container
and public package repositories. If missing, setup builds `nvus-hsb:COMMIT`
from the pinned HSB `docker/Dockerfile`, selecting PGX's `cuda13` stage and
Holoscan 4.4.0. It does not build or launch the HSB demo.
The build targets PGX/GB10, not IGX. Setup never transmits; software checks
do not qualify live hardware or display.
