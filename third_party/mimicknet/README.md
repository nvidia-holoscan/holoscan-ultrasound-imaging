# Optional MimickNet model (not bundled)

The integration supports Ouwen/MimickNet's luminance-adjusted 52,993-parameter
model from upstream commit `a3cd4e972aa7a33c1fa2948f884ad9af5154611e`:
https://github.com/Ouwen/MimickNet/tree/a3cd4e972aa7a33c1fa2948f884ad9af5154611e

The source weights are `examples/models/mimicknet_1568473738-210304.h5`.
Expected SHA-256:
`5291c75b0fcac0637c662b8723c8ca3678b75d46b5d6115ac9defe10beed8d80`.
Weights, exported ONNX and machine-specific TensorRT engines are not included.
The upstream model is identified as CC BY-NC 4.0; upstream software is
Apache-2.0 (preserved in SOFTWARE_LICENSE). The software license does not
replace the model license.

## Prepare on PGX

From the project folder on the host:

```bash
# Prepare software and the optional model; never transmits.
./setup --mimicknet
```

Setup downloads and checks the pinned weights, exports ONNX, builds a TensorRT
engine and checks GPU inference. It saves `.runtime/mimicknet.{h5,onnx,engine,json}`
on the project mount. The JSON records the source commit, model/ONNX/engine
checksums, TensorRT version, GPU and compute capability. No TensorFlow or PyTorch
installation is needed. Compatible assets are reused without downloading or
rebuilding them. After a code update, use `./setup --rebuild --mimicknet`.

This will start the ultrasound hardware:

```bash
./nvus run pw4cm --mimicknet
```

MimickNet is opt-in; plain `./nvus run pw4cm` does not load it. The viewer can
toggle enhancement on/off. Inference consumes completed images, outside the
per-shot receive path. Enhanced grayscale is for display, not acoustic measurement.

Missing or incompatible engines fail before hardware initialization. Setup
preserves existing assets on failure. After changing GPU or TensorRT, select a
new engine path in the mounted project and use it for both setup and imaging:

```bash
export MIMICKNET_ENGINE_PATH="$PWD/.runtime/mimicknet-new.engine"
./setup --mimicknet
# This will start the ultrasound hardware.
./nvus run pw4cm --mimicknet
```

## Offline validation

From the project folder on the host; no hardware is opened:

```bash
docker exec -w "$PWD" nvus /opt/nvus-env/bin/python \
  tests/mimicknet.py .runtime/mimicknet.engine

for rollout in 1 32; do
  docker exec -w "$PWD" -e NVUS_USE_INSTALLED=1 nvus \
    /opt/nvus-env/bin/python -m nvus._pw4cm --simulate --process --mimicknet \
    --frame-limit 12 --rollout-length "$rollout" --no-display
done
```

The first check compares GPU output against an ONNX reference and checks asset
reuse and mismatch rejection. Substitute your engine path if overridden above.
Each run must finish with 12 frames, 768 physical acquisitions and clean shutdown.
This checks GPU reconstruction and enhancement, not live hardware performance.

Attribution: Ouwen Huang et al., *MimickNet, Mimicking Clinical Image
Post-Processing Under Black-Box Constraints*, IEEE Transactions on Medical
Imaging 39(6), 2277–2286 (2020), DOI: 10.1109/TMI.2020.2970867.
