# Holoscan Ultrasound Imaging

<div align="center">
  <img src="https://github.com/user-attachments/assets/bf0d5494-48c5-40f5-9d2c-d11ac9cf4431" width="720" height="405" alt="Holoscan Ultrasound Imaging system overview">
  <br>
  <a href="https://github.com/user-attachments/assets/66609cde-cbe5-4f73-a209-4e360799954f">Watch video</a>
</div>

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

## Get started

Follow the [setup guide](docs/setup.md) for host preparation, assembly,
installation, FPGA programming and first imaging. Parts and electrical limits
are in the [hardware shopping list](docs/hardware.md).

## Run

After setup, run these from the project folder on the host:

```bash
# Check software dependencies; no hardware access.
./nvus doctor

# Receive-only patterns; board must be idle and exclusively available.
./nvus capture test-pattern dc --count 4
./nvus capture test-pattern ramp --count 4
```

> [!WARNING]
> ⚠️ This will start the ultrasound hardware.

```bash
# Continuous, full-screen; terminal required. Ctrl+C or viewer exit stops.
./nvus run pw4cm

# Enable the prepared MimickNet model.
./nvus run pw4cm --mimicknet
```

PW4cm combines 32 plane-wave angles into a B-mode image at 4 cm depth.
See the [CLI reference](docs/cli.md) for recording, run limits, JSON output
and register access, or run `./nvus help`.

For updates, follow [Update the software](docs/setup.md#update-the-software).

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

The NVIDIA-authored source is licensed under Apache-2.0. See [LICENSE](LICENSE).
Third-party and external components retain their separate terms:

- The [Altera FPGA IP directory](fpga/build/ip) is governed by its
  [Altera license](fpga/build/ip/LICENSE), including its Altera-product-only use
  restrictions. The project Apache-2.0 license does not replace those terms.
- The MimickNet software component is governed by its
  [Apache-2.0 software license](third_party/mimicknet/SOFTWARE_LICENSE).
- Optional MimickNet model weights are not distributed in this repository and
  remain separately governed by the upstream CC BY-NC 4.0 terms described in
  the [MimickNet documentation](third_party/mimicknet/README.md). The software
  license does not replace the model-weight terms.

See [THIRD-PARTY-NOTICES](THIRD-PARTY-NOTICES) for the complete component and
dependency inventory.

### NOTICE AND DISCLAIMER

NOTICE AND DISCLAIMER: This software automatically retrieves, accesses or interacts with external materials. Those retrieved materials are not distributed with this software and are governed solely by separate terms, conditions and licenses.
