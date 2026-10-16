# Security Policy: Holoscan Ultrasound Imaging

NVIDIA is dedicated to the security and trust of our software products and services,
including this source repository.

**nvus 0.1.0** is an early reference architecture for streaming ultrasound channel
data from a TI AFE5932EVM through an FPGA and RDMA over Converged Ethernet (RoCE)
into GPU memory, then reconstructing a live B-mode image. It is an evaluation and
learning codebase. It is not a production medical device and it is not clinically
validated.

If you believe you have found a security issue, report it privately.
**Do not open a public issue, pull request, or discussion.**

## Reporting a Vulnerability

* **Web (preferred):** [NVIDIA Vulnerability Disclosure Program](https://www.nvidia.com/en-us/security/)
* **E-mail:** [psirt@nvidia.com](mailto:psirt@nvidia.com)
  - Encrypt the report with the [NVIDIA public PGP key](https://www.nvidia.com/en-us/security/pgp-key)

### What to include

- Product name (`nvus` / Holoscan Ultrasound Imaging), version (`0.1.0`), and commit
- Vulnerability type
- Step-by-step reproduction, including whether hardware, the `nvus` container, or
  `--dry-run` / simulation was involved
- Proof of concept, if available
- Impact, including effects on transmit, register state, RDMA/GPU memory, or saved RF

Detailed reports help NVIDIA evaluate and address issues faster.

NVIDIA's Product Security Incident Response Team (PSIRT) will acknowledge receipt,
validate severity, develop a fix, and publish a security bulletin as appropriate.

## Security Architecture & Context

nvus is a **host application, CLI, and FPGA reference** (`python/nvus`, `cpp/`,
`fpga/`, `firmware/`). Its primary security responsibility is to keep ultrasound
transmit and board configuration inside operator-commissioned bounds, and to keep
sensor samples and GPU receive buffers on the intended board-facing path.

The supported runtime is a Lenovo PGX host. `./setup` builds container `nvus`
(`docker/Dockerfile.pgx`) and `./nvus` runs `python -m nvus.cli` inside it.
The container uses host networking, all GPUs, `/dev/infiniband`, and a read-write
bind mount of the repository. `hololink` and Holoscan are install-time dependencies
fetched at the pinned HSB revision in `setup`; they are not vendored here.

Trust boundaries:

| Boundary | What crosses it | Control in this repository |
| --- | --- | --- |
| Operator CLI | `nvus hw`, `nvus capture`, `nvus run pw4cm` | Local process only. `nvus hw` allowlists registers in `python/nvus/hardware.py`. Live runs require `commissioning=True` in `HololinkTransport`. |
| HSB control plane | `hololink.Enumerator.find_channel`, default `--ip 192.168.0.2` | IPv4 syntax check. No authentication or TLS in nvus. |
| RoCE data plane | Board-facing NIC (`--ibv-name`), GPU receive ring (`rx_pages`) | Native adapter in `cpp/src/hololink_transport.cpp`. Container is passed `/dev/infiniband`. |
| FPGA image | UUID `ac192bb6-24be-c23f-d07e-2e130d9d2f0f` | `enumeration_strategy()` in `python/nvus/evm.py` selects the image. `firmware/manifest-ti-ultrasound-126d629c25eb.yaml` records an MD5 of the `.rpd`. |
| Saved sensor data | `nvus capture --output`, `nvus run --output` | New directory on the bind-mounted tree; SHA-256 of payloads. |
| Model download | `python/nvus/mimicknet_setup.py` `urlopen` of pinned MimickNet weights | SHA-256 check before ONNX export. Optional; PW4cm does not require it. |

`./nvus doctor` checks installed Python modules only. It does not open the GPU,
NIC, or board.

### Supported versions

| Version | Status |
| --- | --- |
| 0.1.0 (current `main` reference) | Accepts security reports |
| Older checkouts | Unsupported; APIs and register maps can change during 0.x |

### Deployment assumptions

- One operator, one repository checkout, one `nvus` container, exclusive use of the rig.
- The board-facing link to `192.168.0.2` is an isolated sensor-bridge segment.
  `docs/setup.md` states that `ping` is not an identity check.
- Electrical limits, probe coupling, and supply wiring are operator procedures in
  `docs/setup.md` and `docs/hardware.md`.

### Out of scope

- Clinical safety, diagnostic claims, and acoustic-output qualification.
- Vulnerabilities that exist only in the upstream Holoscan Sensor Bridge tree,
  Holoscan, CUDA, or the NVIDIA driver, except where an nvus patch under
  `docker/patches/` introduces them.
- Use of in-tree commissioning utilities under `python/tools/` outside their
  documented exclusive-ownership procedure.
- A report whose only impact is that an operator who already runs `./nvus` on
  the host can pass CLI flags. That process is the trusted operator.

## Threat Model

The following scenarios are the primary concerns for this repository, including
setup and diagnostic paths. No separate threat-and-vulnerability assessment was
available when this document was written.

1. **Transmit and register actuation on a shared or reachable board:**
   `nvus run pw4cm` opts into commissioning and drives TX-HS SPI and sync/capture
   blocks (`cpp/src/hololink_transport.cpp`, `python/nvus/tx_update.py`).
   `nvus hw write` can change capture delay (`0x50000008`), AFE LPF (`0x92`), and
   TX attenuation (`0x1f`) after an idle check. The ownership lock is
   `fcntl.flock` on `/tmp/nvus-hardware.lock` inside one container. Another host,
   an older tool, or a second checkout can still reach the same FPGA.

2. **Unauthenticated HSB control plane:** `hardware.connect` and `capture.execute`
   call `hololink.Enumerator.find_channel(channel_ip=...)` and `control.start()`
   against a default address of `192.168.0.2`. The `nvus` container is started
   with `--network host`. nvus has no credential, token, or TLS configuration for
   that endpoint. A peer that can send on the board-facing network can speak the
   same control protocol the CLI uses.

3. **RoCE receive into GPU memory:** Live and capture paths program a board-facing
   RDMA NIC (`--ibv-name`, default first device or `mlx5_0` from
   `python/nvus/host.py`) and a receive ring (`rx_pages`, 2..1024). The container
   receives `/dev/infiniband` and unlimited locked memory. Unexpected RoCE traffic
   on that NIC is handled by the Hololink receiver, including the local patches in
   `docker/patches/` (`10-roce-receive-failure`, `30-roce-receive-queue`,
   `40-roce-tx-memory-ordering`). A fault on that path can corrupt or stall GPU
   buffers owned by the acquisition process.

4. **Sensor data written to the repository mount:** `nvus capture` and
   `nvus run --output` store RF frames, checksums, hardware readbacks, and plots
   under a caller-chosen directory on the bind-mounted project tree
   (`python/nvus/capture.py`, `python/nvus/scanner.py`). Those files are channel
   data from a medical imaging reference. Protection is the host filesystem
   permissions of that directory. SHA-256 detects later modification; it does not
   restrict who can read the capture.

5. **Container and setup blast radius:** `./setup` clones
   `HSB_REPOSITORY` (default the public Holoscan Sensor Bridge repository) at a
   full commit SHA, builds with `--network=host`, and applies `docker/patches/`.
   The running container bind-mounts the repo read-write, passes every GPU, and
   `./setup` performs the venv install with `docker exec --user root`.
   `./nvus` also mounts the operator's X authority into the container. Compromise
   of that container is compromise of host networking, the RDMA device, and the
   working tree.

6. **Unsigned FPGA image and optional model fetch:** Enumeration trusts the FPGA
   UUID string in `python/nvus/fpga.py`. The firmware manifest identifies
   `ti_ultrasound_126d629c25eb.rpd` by MD5 and size, not by a signature.
   `python/nvus/mimicknet_setup.py` downloads weights with `urlopen` from a pinned
   GitHub URL and rejects a SHA-256 mismatch before export. A substituted image
   that still advertises the expected UUID, or a download that passes the hash
   check because the expected hash was changed with it, is outside what nvus
   detects at runtime.

7. **Commissioning tools beside the CLI allowlist:** `python/nvus/hardware.py`
   rejects unknown addresses, trigger registers, and broadcast access for
   `nvus hw`. Utilities such as `python/tools/nvus_lvds_lab.py` accept a wider
   register surface (`--hololink` default `192.168.0.2`). They are documented as
   commissioning tools with the same exclusive-ownership expectation, and they
   are still shipped in the tree a container can execute.

### Critical Security Assumptions

- The board-facing Ethernet and RoCE segment is physically or administratively
  isolated to the commissioned host and FPGA. nvus does not authenticate the peer
  at `192.168.0.2`.
- The operator has exclusive physical ownership of the rig before `nvus hw`,
  `nvus capture`, or `nvus run`. The flock in `/tmp/nvus-hardware.lock` only
  serializes nvus operations inside that container.
- The programmed FPGA is the commissioned ultrasound image for UUID
  `ac192bb6-24be-c23f-d07e-2e130d9d2f0f`. The UUID selects an enumeration strategy;
  it is not a signature check. Manifest integrity is an MD5.
- The host OS, Docker, the NVIDIA driver, and the RDMA stack enforce the
  container's device and user mapping. nvus adds no further authorization.
- Acoustic output, supply polarity, and probe connection stay inside the limits
  in `docs/hardware.md`. Software idle checks do not measure acoustic energy.
- `HSB_REPOSITORY` points at the intended Sensor Bridge source, and the pinned
  revision in `./setup` is the revision operators mean to build. nvus verifies
  that the checkout is clean and matches that SHA; it does not re-verify upstream
  signatures at container start.
- Callers of the native `HololinkTransport` pass a fresh `commissioning` opt-in
  and the register expectations checked in `cpp/src/hololink_transport.cpp`.
  Those expectations compare readback values; they do not identify the human
  operator.
