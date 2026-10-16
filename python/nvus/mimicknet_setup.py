# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Prepare the pinned MimickNet model offline; no scanner or hardware imports."""
import argparse
import fcntl
import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlopen

import h5py
import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper

from .mimicknet import MODEL_SHA256, SOURCE_COMMIT, MimickNet, build_engine

MODEL_URL = (f'https://raw.githubusercontent.com/Ouwen/MimickNet/{SOURCE_COMMIT}/'
             'examples/models/mimicknet_1568473738-210304.h5')


def export_onnx(weights, output):
    if hashlib.sha256(Path(weights).read_bytes()).hexdigest() != MODEL_SHA256:
        raise ValueError('MimickNet weights checksum mismatch')
    nodes, parameters, skips = [], [], []

    def node(kind, inputs, name, **attributes):
        nodes.append(helper.make_node(kind, inputs, [name], name=name, **attributes))
        return name

    with h5py.File(weights, 'r') as model:
        def convolution(image, index, transpose=False):
            name = ('conv2d_transpose' if transpose else 'conv2d') + (f'_{index}' if index else '')
            group = model['model_weights'][name][name]
            kernel = np.asarray(group['kernel:0']).transpose(3, 2, 0, 1)
            bias = np.asarray(group['bias:0'])
            parameters.extend([numpy_helper.from_array(kernel, name + '.weight'),
                               numpy_helper.from_array(bias, name + '.bias')])
            padding = 0 if transpose else kernel.shape[2] // 2
            return node('ConvTranspose' if transpose else 'Conv',
                        [image, name + '.weight', name + '.bias'], name,
                        kernel_shape=list(kernel.shape[2:]), pads=[padding] * 4,
                        strides=[2 if transpose else 1] * 2)

        image = 'image'
        for level in range(5):
            for index in (2 * level, 2 * level + 1):
                image = node('LeakyRelu', [convolution(image, index)], f'encoder{index}', alpha=.2)
            if level < 4:
                skips.append(image)
                image = node('MaxPool', [image], f'pool{level}', kernel_shape=[2, 2], strides=[2, 2])
        for level in range(4):
            image = node('Concat', [convolution(image, level, True), skips[3 - level]],
                         f'skip{level}', axis=1)
            for index in (10 + 2 * level, 11 + 2 * level):
                image = node('Relu', [convolution(image, index)], f'decoder{index}')
        node('Identity', [convolution(image, 18)], 'result')
    if sum(np.prod(parameter.dims) for parameter in parameters) != 52993:
        raise ValueError('Expected the 52,993-parameter MimickNet model')
    tensors = [helper.make_tensor_value_info(name, TensorProto.FLOAT, [1, 1, 'height', 'width'])
               for name in ('image', 'result')]
    graph = helper.make_graph(nodes, 'MimickNet', tensors[:1], tensors[1:], parameters)
    model = helper.make_model(graph, producer_name='nvus', ir_version=8,
                              opset_imports=[helper.make_opsetid('', 17)])
    onnx.checker.check_model(model)
    onnx.save(model, output)


def warmup(engine):
    import cupy as cp
    image = cp.linspace(-80., 0., 528 * 519, dtype=cp.float32).reshape(528, 519)
    MimickNet(engine).validate(image)


def prepare(engine):
    engine = Path(engine).absolute()
    if engine.suffix != '.engine':
        raise ValueError('MIMICKNET_ENGINE_PATH must end in .engine')
    engine.parent.mkdir(parents=True, exist_ok=True)
    suffixes = ('.h5', '.onnx', '.engine', '.json')
    with engine.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if any(engine.with_suffix(suffix).exists() for suffix in suffixes):
            try:
                warmup(engine)
            except Exception as error:
                raise RuntimeError(f'{error}. Existing assets preserved; select a new '
                                   'MIMICKNET_ENGINE_PATH and rerun ./setup --mimicknet.') from error
            print(f'Reusing MimickNet: {engine}')
            return
        with TemporaryDirectory(prefix='mimicknet-', dir=engine.parent) as directory:
            staged = Path(directory) / engine.name
            print(f'Downloading MimickNet weights (CC BY-NC 4.0): {MODEL_URL}', flush=True)
            with urlopen(MODEL_URL, timeout=30) as response:
                staged.with_suffix('.h5').write_bytes(response.read())
            export_onnx(staged.with_suffix('.h5'), staged.with_suffix('.onnx'))
            build_engine(staged, staged.with_suffix('.onnx'))
            warmup(staged)
            for suffix in suffixes:
                os.link(staged.with_suffix(suffix), engine.with_suffix(suffix))
        print(f'MimickNet ready: {engine}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('engine', type=Path)
    prepare(parser.parse_args().engine)
