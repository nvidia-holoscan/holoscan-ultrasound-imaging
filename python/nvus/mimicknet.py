# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Persistent TensorRT inference on CuPy buffers for display enhancement."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

import cupy as cp
import numpy as np


VALIDATED_ONNX_SHA256 = {
    # Supported exports of the pinned weights.
    '2b5be81cd1e3ec77512a6402355fbd68cbae12bc58a0ba38001b435e6894ed0f',
    '828f2362f9326e96ea80e32dfcbdbf9d46113935ca5af1b4abb5c6f762dc3adb',
    '4b445e86bc51b6f0a9560d1912567dd1da28cd9241995140b7edc4a8f5906590',
}
MODEL_SHA256 = '5291c75b0fcac0637c662b8723c8ca3678b75d46b5d6115ac9defe10beed8d80'
SOURCE_COMMIT = 'a3cd4e972aa7a33c1fa2948f884ad9af5154611e'

DISPLAY_GAIN_DB = -6.


def _bindings():
    try:
        import tensorrt as trt
    except ImportError:
        import tensorrt_bindings as trt
    return trt


def _indices(size):
    extra = (-size) % 16
    index = np.pad(np.arange(size), (extra // 2, extra // 2), mode="symmetric")
    if extra % 2:
        index = np.pad(index, (1, 0), mode="symmetric")
    return cp.asarray(index), (extra + 1) // 2


def _device_identity(device):
    properties = cp.cuda.runtime.getDeviceProperties(device)
    name = properties['name']
    if isinstance(name, bytes):
        name = name.decode()
    return name.strip(), f"{properties['major']}.{properties['minor']}"


def write_provenance(engine_path, onnx_path):
    engine_path, onnx_path = map(Path, (engine_path, onnx_path))
    onnx_sha = hashlib.sha256(onnx_path.read_bytes()).hexdigest()
    if onnx_sha not in VALIDATED_ONNX_SHA256:
        raise ValueError('ONNX export is not validated')
    device = cp.cuda.Device().id
    gpu, capability = _device_identity(device)
    provenance = {
        'model_sha256': MODEL_SHA256,
        'onnx_sha256': onnx_sha,
        'engine_sha256': hashlib.sha256(engine_path.read_bytes()).hexdigest(),
        'source_commit': SOURCE_COMMIT,
        'build': {'tensorrt': _bindings().__version__, 'gpu': gpu,
                  'compute_capability': capability, 'tf32': False,
                  'profile': {'min': [1, 1, 32, 32], 'opt': [1, 1, 528, 528],
                              'max': [1, 1, 1344, 528]}},
    }
    engine_path.with_suffix('.json').write_text(json.dumps(provenance, indent=2) + '\n')
    return provenance


def build_engine(engine_path, onnx_path, trtexec='/usr/src/tensorrt/bin/trtexec'):
    onnx_path = Path(onnx_path)
    if hashlib.sha256(onnx_path.read_bytes()).hexdigest() not in VALIDATED_ONNX_SHA256:
        raise ValueError('ONNX export is not validated')
    subprocess.run([
        trtexec, f'--onnx={onnx_path}', f'--saveEngine={engine_path}',
        '--minShapes=image:1x1x32x32', '--optShapes=image:1x1x528x528',
        '--maxShapes=image:1x1x1344x528', '--noTF32', '--skipInference'], check=True)
    return write_provenance(engine_path, onnx_path)


class MimickNet:
    def __init__(self, engine_path):
        engine_path = Path(engine_path)
        if not engine_path.is_file():
            raise FileNotFoundError(f'MimickNet engine not found: {engine_path}. Run ./setup --mimicknet.')
        trt = _bindings()
        self.device = cp.cuda.Device().id
        self.logger = trt.Logger(trt.Logger.WARNING)
        self.runtime = trt.Runtime(self.logger)
        serialized = engine_path.read_bytes()
        provenance = json.loads(engine_path.with_suffix(".json").read_text())
        engine_sha = hashlib.sha256(serialized).hexdigest()
        model_sha = MODEL_SHA256
        if provenance.get("engine_sha256") != engine_sha or provenance.get("model_sha256") != model_sha:
            raise ValueError("MimickNet engine provenance does not match validated weights/engine")
        onnx_sha = provenance.get("onnx_sha256")
        if onnx_sha not in VALIDATED_ONNX_SHA256:
            raise ValueError("MimickNet engine was not built from the validated ONNX export")
        build = provenance.get('build')
        gpu, compute_capability = _device_identity(self.device)
        if not isinstance(build, dict):
            raise ValueError('MimickNet engine provenance lacks build identity')
        if build.get('tensorrt') != trt.__version__:
            raise ValueError(
                f"MimickNet engine TensorRT {build.get('tensorrt')} does not match "
                f'runtime {trt.__version__}')
        if (build.get('gpu'), build.get('compute_capability')) != (
                gpu, compute_capability):
            raise ValueError(
                'MimickNet engine was built for '
                f"{build.get('gpu')} CC {build.get('compute_capability')}; "
                f'active GPU is {gpu} CC {compute_capability}')
        self.engine = self.runtime.deserialize_cuda_engine(serialized)
        if self.engine is None:
            raise RuntimeError("MimickNet engine is incompatible with this TensorRT/GPU")
        self.context = self.engine.create_execution_context()
        if self.engine.num_io_tensors != 2:
            raise ValueError("Expected the validated single-input/single-output MimickNet engine")
        if any(self.engine.get_tensor_dtype(name) != trt.float32 for name in ('image', 'result')):
            raise ValueError("MimickNet engine must use float32 input and output")
        self.stream = cp.cuda.Stream(non_blocking=True)
        self.buffers = {}
        self.metadata = {
            "model": "mimicknet_1568473738-210304.h5",
            "model_sha256": MODEL_SHA256,
            "source_commit": SOURCE_COMMIT,
            "engine_sha256": engine_sha,
            "onnx_sha256": onnx_sha,
            "build": provenance.get("build"),
            "backend": "TensorRT", "backend_version": trt.__version__,
            "preprocessing": "80 dB clip; per-image min-max; upstream symmetric 16-pad",
            "output": "normalized grayscale mapped to 50 dB display; not acoustic dB",
            "display_gain_db": DISPLAY_GAIN_DB,
        }

    def _infer(self, image_db):
        shape = tuple(image_db.shape)
        if shape not in self.buffers:
            zi, z0 = _indices(shape[0])
            xi, x0 = _indices(shape[1])
            padded_shape = (1, 1, len(zi), len(xi))
            output = cp.empty(padded_shape, cp.float32)
            self.buffers[shape] = zi, xi, z0, x0, output
        zi, xi, z0, x0, output = self.buffers[shape]
        normalized = cp.maximum(image_db - cp.max(image_db), -80.)
        low = cp.min(normalized)
        normalized = (normalized - low) / cp.maximum(cp.max(normalized) - low, 1e-12)
        padded = cp.ascontiguousarray(normalized[zi[:, None], xi[None, :]])
        if not self.context.set_input_shape('image', (1, 1, *padded.shape)):
            raise ValueError("Image dimensions exceed the validated MimickNet engine profile")
        self.context.set_tensor_address('image', padded.data.ptr)
        self.context.set_tensor_address('result', output.data.ptr)
        if not self.context.execute_async_v3(cp.cuda.get_current_stream().ptr):
            raise RuntimeError("MimickNet TensorRT execution failed")
        # The stream allocator keeps padded alive through queued use.
        enhanced = output[0, 0, z0:z0 + shape[0], x0:x0 + shape[1]]
        return cp.clip(enhanced * 50. - 50. + DISPLAY_GAIN_DB, -50., 0.)

    def __call__(self, image_db):
        """Protect quantitative B-mode; return display-only values on its GPU/grid."""
        if not isinstance(image_db, cp.ndarray) or image_db.ndim != 2:
            raise TypeError('MimickNet input must be a 2D CuPy B-mode array')
        if image_db.dtype != cp.float32 or image_db.device.id != self.device:
            raise ValueError('MimickNet input must be float32 on its initialization GPU')
        source_stream = cp.cuda.get_current_stream()
        ready = cp.cuda.Event()
        ready.record(source_stream)
        self.stream.wait_event(ready)
        with self.stream:
            result = self._infer(image_db.copy())
            complete = cp.cuda.Event()
            complete.record(self.stream)
        source_stream.wait_event(complete)
        if not isinstance(result, cp.ndarray) or result.shape != image_db.shape:
            raise ValueError('MimickNet output must preserve the image grid')
        if result.dtype != cp.float32 or result.device.id != self.device:
            raise ValueError('MimickNet output must be float32 on the input GPU')
        return result

    def validate(self, image_db):
        """Warm up before EVM initialization; synchronize and check output once."""
        result = self(image_db)
        if not bool(cp.all(cp.isfinite(result))) or bool(cp.any(result > 0)):
            raise ValueError('MimickNet output must be finite display dB <= 0')
        cp.cuda.get_current_stream().synchronize()
        return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Build a validated MimickNet engine and provenance')
    parser.add_argument('engine', type=Path)
    parser.add_argument('onnx', type=Path)
    arguments = parser.parse_args()
    print(json.dumps(build_engine(arguments.engine, arguments.onnx), indent=2))
