# CLI reference

Complete the [setup guide](setup.md) before using hardware commands.
Run commands from the project folder on the host. `./nvus` uses the installed
package inside container `nvus`; no shell activation is needed.


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

## Hardware debugging

For FPGA programming, follow the [hardware setup guide](setup.md#5-program-the-fpga).

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

```bash
# Help only; no hardware access.
./nvus hw
```

Writes require `--expect`, verify readback, and log to `.runtime/hw-writes.jsonl`.
Successful writes persist until explicitly restored. A failed write may have
changed hardware: inspect the result and restore before imaging.
