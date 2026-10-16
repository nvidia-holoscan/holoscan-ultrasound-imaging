# Holoscan Ultrasound Imaging

Holoscan Ultrasound Imaging is a reference architecture for streaming ultrasound
channel data from sensor hardware to GPU memory over RDMA over Converged Ethernet
(RoCE). The goal is to enable agentic ultrasound by treating the scanner as a
robot that senses echoes, reasons about the world, and dynamically chooses every
transmit to close the sense–reason–act loop.

The implementation uses NVIDIA Holoscan Sensor Bridge (HSB), native C++
acquisition, CUDA reconstruction, and a live B-mode viewer.
The reference setup uses the TI AFE5932EVM, its AFE5932 receivers and TX73L64
transmitters, and a Sonosray L12-5 D probe.

*This project is a starting point for a community effort in open ultrasound.
We hope it helps you learn, tinker, and build—and invite you to share your ideas,
improvements, and discoveries so we can learn from one another.*

*This is an early **v0.1** reference for evaluation, learning, and extension.
APIs and data formats may change. It is not a production medical device or a
clinically validated system.*

## Setup

Start with the [hardware shopping list](docs/hardware.md) and
[hardware setup guide](docs/setup.md) for assembly, FPGA programming and first imaging.

The supported setup is Lenovo PGX with a board-facing RDMA NIC, compatible FPGA
firmware, and the connected ultrasound hardware. Complete
[HSB host setup](https://docs.nvidia.com/holoscan/sensor-bridge/2-7-latest/getting-started/host-setup)
on the host first, including the platform-specific driver and network steps.
`./setup` prepares the project container; it does not configure the host.

### One-time setup

From the project folder on the host:

```bash
./setup
./nvus doctor
./nvus help
```

`./setup`:

- Fetches the pinned public HSB 2.7.0 release into `.runtime/hsb`.
- Builds missing SDK and `nvus` images, applying patches during the build.
- Creates or starts container `nvus` with the project mounted.
- Installs the package in `/opt/nvus-env` if needed.
- Checks software dependencies.

Setup never transmits and leaves the HSB checkout clean.

`./nvus` runs the installed CLI inside that container. No container shell,
host Python environment or activation is needed.

```bash
# Preview setup.
./setup --dry-run

# Reuse the installation and start a stopped container.
./setup
```

### Optional MimickNet

The [MimickNet model](third_party/mimicknet/README.md) uses CC BY-NC 4.0 terms.
It is not included or required for PW4cm.

```bash
# Download pinned weights and build an engine for this GPU; no transmission.
./setup --mimicknet
```

## CLI

```bash
./nvus version
./nvus doctor
./nvus doctor --format json
./nvus help --format json
```

Version, help and doctor do not open hardware. Doctor checks software, not GPU,
NIC or board readiness. Exit codes:
`0` success, `1` operation failure, `2` usage error; errors go to stderr.

### Live imaging

PW4cm combines echoes from 32 plane-wave angles (−7.75° to +7.75°) into each
4 cm B-mode image. Each angle uses a 5 MHz two-cycle pulse and two receive-mux
halves: 64 physical acquisitions per image. Reconstruction uses 1,480 m/s sound
speed and 2,974 samples at 50 MSPS.

`--rollout-length 32` processes all angles per policy call; `1` processes one
angle per call. Both produce the same image geometry. This is host-side grouping,
not an autonomous sequence uploaded to the FPGA.

Run from the project folder after `./setup`. Preview without hardware:

```bash
./nvus run pw4cm --dry-run
```

This will start the ultrasound hardware:

```bash
# Continuous, full-screen; terminal required. Ctrl+C or viewer exit stops.
./nvus run pw4cm

# No viewer; still transmits. Frames: 1–1024, with a 120s limit.
./nvus run pw4cm --frames 12 --headless

# Timed run (max 2m); final frame may be partial. Don't combine with --frames.
./nvus run pw4cm --duration 30s

# One angle per policy call (default: 32).
./nvus run pw4cm --rollout-length 1

# Select the RDMA NIC; replace DEVICE.
./nvus run pw4cm --ibv-name DEVICE

# Enable the prepared MimickNet model.
./nvus run pw4cm --mimicknet
```

Save four completed RF frames after four warm-up frames:

```bash
./nvus run pw4cm --frames 12 --output captures/pw4cm \
  --record-frames 4 --record-skip 4 --format json
```

The directory must be new. Recording saves checksums, hardware readbacks and
shutdown results. JSON contains final counts and restoration details; progress
goes to stderr.

## Update the installation

After code or SDK patch changes, stop all jobs in `nvus`, then run:

```bash
./setup --rebuild --mimicknet
```

Omit `--mimicknet` if you do not use enhancement.

This replaces the `nvus` container and its installed environment. Keep persistent
files in the mounted project folder; container-local files are removed. A failed
image build leaves the old container unchanged. Documentation-only changes need
no rebuild.

An HSB revision change requires `--rebuild`; setup updates the clean SDK checkout
and builds the matching image.

## Hardware debugging

For FPGA programming, follow the [hardware setup guide](docs/setup.md#5-program-the-fpga).

Use receive-only patterns to check the data path and register access to inspect
or adjust the FPGA, receive AFEs (`rx1`, `rx2`) and transmitters (`tx1`, `tx2`).

### Working with the Hardware

On an idle board with exclusive access:

```bash
# RX-only, no TX. Save 1–4 frames, metadata and PNG to a new directory.
# Restores configuration, not in-flight data or aligner state.
./nvus capture test-pattern dc --count 4 --output captures/dc-test

# Validate all frames; plot the first. Open the printed PNG path.
./nvus capture test-pattern ramp --count 4 --output captures/ramp-test

# JSON results; default output: captures/PATTERN-TIMESTAMP/.
./nvus capture test-pattern dc --format json

# Select NIC and timeout (defaults: first RDMA device, 10s).
./nvus capture test-pattern ramp --ibv-name DEVICE --timeout 5
```

### Idle register access

Stop imaging and take exclusive ownership of the rig before register access;
even SPI reads temporarily enable readout.
Allowlisted FPGA reads can inspect a configured board without passing the idle
check; they still take the ownership lock. Writes and AFE/TX reads require the
idle check. A blocked operation reports all three guard register values.

```bash
# Help only; no hardware access.
./nvus hw

# Read capture delay; default endpoint is 192.168.0.2.
./nvus hw read fpga 0x50000008 --ip 192.168.0.2

# Change 0 to 1, verify, then restore. Use the actual original value.
./nvus hw write fpga 0x50000008 1 --expect 0
./nvus hw read fpga 0x50000008 --format json
./nvus hw write fpga 0x50000008 0 --expect 1

# Read each chip separately.
./nvus hw read rx1 0x92
./nvus hw read rx2 0x92
./nvus hw read tx1 0x1f
./nvus hw read tx2 0x1f
```

This changes capture delay 0 → 1 → 0 without capturing or transmitting.
Use the actual original value if not zero.
Writes require `--expect`, verify readback, and log to `.runtime/hw-writes.jsonl`.
Successful writes persist until explicitly restored. A failed write may have
changed hardware: inspect the result and restore before imaging.

| Device | Registers | Writes |
| --- | --- | --- |
| `fpga` | Sync config `0x40000004`; capture delay/length/status `0x50000008`, `0x5000000c`, `0x50000080`; board control/status/LVDS `0x60000000`, `0x60000080`, `0x60000084` | Capture delay only |
| `rx1`, `rx2` | LPF `0x92` | Low two bits: 0 = 15 MHz, 1 = 10 MHz |
| `tx1`, `tx2` | Receive attenuation `0x1f` | Low six bits: codes 0–48 |

Other addresses, trigger/reset registers, and broadcast access are rejected.
RX/TX writes require both old and new field values to be in the listed range.
RX accesses restore the board mux and leave readout disabled. TX accesses use
TX-HS/prescaler 15 for reliable idle readback, restore its clock configuration,
and leave write mode enabled. Acquisition's fast TX writes are unchanged.
Active sync, TX ingress, or TX SPI blocks access. The CLI/PW4cm lock is local
to one container; exclusive ownership must also exclude older tools and other hosts.

## Development

- [SDK build](docker/README.md): dependency pins and patches.
- [MimickNet](third_party/mimicknet/README.md): model preparation and offline GPU checks.
- [Contributing](CONTRIBUTING.md): contribution guidelines.

The Python package and C++ namespace are `nvus`; build options use `NVUS_*`.

```bash
# Check the installed package inside the prepared container; no hardware.
docker exec -w "$PWD" -e NVUS_USE_INSTALLED=1 nvus \
  /opt/nvus-env/bin/python tests/installation.py
```

The supplied image targets PGX. Qualify other hosts separately; simulation does
not qualify hardware.

```bash
# List GPUs, then substitute the UUID and NIC below. Preview only.
nvidia-smi -L
NVUS_CUDA_VISIBLE_DEVICES=GPU-UUID ./nvus run pw4cm --ibv-name DEVICE --dry-run
```

## License

The source is Apache-2.0. See [LICENSE](LICENSE) and
[THIRD-PARTY-NOTICES](THIRD-PARTY-NOTICES) for dependency and model terms.
