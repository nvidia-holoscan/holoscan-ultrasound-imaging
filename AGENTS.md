# AGENTS.md — how to work in Holoscan Ultrasound Imaging

Real-time ultrasound imaging on the HSB (Holoscan Sensor Bridge) platform:
TI AFE5932 receive AFEs + TX73L64 transmitters on the AFE5932EVM, the Sonosray L12-5 D
probe, and a GPU host. `hololink` is an install-time dependency (enumeration,
RoCE receive/transmit); it is not part of this repository.

## Safety rules for live runs (do not bend these)

1. This will start the ultrasound hardware. Start transmit runs only when the
   operator asks; never leave one running after a task.
2. For agent-run checks, use `./nvus run pw4cm --frames 300` or `--duration 30s`.
   The CLI defaults to continuous imaging; use it only with an operator present
   and ensure it stops before the task ends. `--headless` still transmits.
3. After an interrupted or failed launch, list the python processes on the rig
   and stop only confirmed leftover processes from this task by PID. Do not
   terminate unrelated users' jobs. Check restoration before another run.
4. On the shared rig, run only from this repository's checkout, never from a
   teammate's directory.

## Current documentation

- [README.md](README.md): project introduction and quick run commands.
- [docs/cli.md](docs/cli.md): CLI options and hardware debugging.
- [docs/setup.md](docs/setup.md): host setup, assembly, FPGA programming and first imaging.
- [docs/hardware.md](docs/hardware.md): parts and electrical limits.

## Setup and CLI

Keep `./setup` separate from the thin `./nvus` wrapper; no CLI setup/install
commands and no duplicated setup logic. Run from the checkout mounted in container
`nvus`. The wrapper uses the installed package in `/opt/nvus-env` and sets
`NVUS_USE_INSTALLED=1`; do not substitute a host environment or source extension.

```bash
./setup --dry-run
./setup
./nvus doctor
./nvus help
./nvus run pw4cm --dry-run
```

Setup never transmits. Doctor checks software only. HSB patches are applied
during image build; keep `.runtime/hsb` clean. After source or patch changes,
stop container jobs and use `./setup --rebuild`; add `--mimicknet` when needed.
Rebuild replaces container-local state; preserve captures in the project mount.
Documentation-only edits need no rebuild. Validate fresh setup in isolation
before claiming new-machine installation works.

Use `./nvus capture test-pattern dc|ramp --count 4` for receive-only checks on an
idle, exclusively available board. Capture produces metadata and a PNG under
`captures/`. Cleanup failure remains a failure even if all frames arrived.
Use `./nvus hw read/write` for supported register access; writes require `--expect`.

PW4cm uses one native C++ `PolicyExecutor`; Python handles initialization,
restoration, reconstruction and display. The scripts in `examples/` are the
underlying implementation, not the primary user workflow. Other policies/modes
and hardware fault behavior require separate commissioning.

## Hardware constraints

- TX SPI reaches the chips through TX-HS only. Idle CLI readback uses prescaler
  15; native acquisition manages its fast write path. Do not unify these settings.
- The FPGA sync clock is 200 MHz. Keep phase alignment enabled: without it,
  +/-10 ns launch jitter creates image flicker. Check the FPGA version when
  commissioning; older images do not implement the alignment bit.
- AFE SPI has its own clock, independent of TX-HS. Read `rx1` and `rx2`
  separately; their shared SDOUT prevents broadcast reads.
- Preserve documented channel maps, delay quantization and profile quirks.
  Low-level writes require an idle board with exclusive ownership.
- Commissioning utilities live under `python/tools/`; they are not supported
  API examples and require the same exclusive-ownership rules.

## Checks

Use only runnable end-to-end examples as tests; do not add unit-test suites.
GPU replay requires a real CUDA device. Simulated acquisition and
saved-RF replay do not qualify live hardware.

## Conventions

- Python 3, 4-space indentation; comments state constraints the code cannot
  show, not narration.
- Keep reconstruction and model inference off the per-shot receive path. Native
  CUDA buffer copies and readiness fences are part of acquisition ownership.
- Hardware quirks are documented where they are handled (wrap rule,
  profile-select lag, element map); do not "simplify" them away.
- Keep changes small. Preserve unrelated local edits, captures and build artifacts.
- The package version lives in `pyproject.toml`; setup reads it for the image tag.

## Firmware

RPD binaries are GitHub release assets; the manifest is checked in. Follow
[the programming instructions](docs/setup.md#5-program-the-fpga) and use the
asset named in the manifest.
