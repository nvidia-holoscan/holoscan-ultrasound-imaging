#!/usr/bin/env python3
"""Offline GPU inference, model parity and asset reuse; never opens the scanner."""
import argparse
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import cupy as cp
import numpy as np
from onnx.reference import ReferenceEvaluator

from nvus.mimicknet import DISPLAY_GAIN_DB, MimickNet
from nvus import mimicknet_setup as setup


def parity(engine):
    operation = MimickNet(engine)
    reference = ReferenceEvaluator(str(engine.with_suffix('.onnx')))
    for shape, constant in (((32, 48), False), ((31, 33), False), ((32, 48), True),
                            ((528, 519), False), ((527, 519), False)):
        image = np.random.default_rng(42).uniform(-90., 0., shape).astype(np.float32)
        if constant:
            image.fill(-40.)
        with cp.cuda.Stream(non_blocking=True):
            source = cp.asarray(image)
            actual = cp.asnumpy(operation.validate(source))
            np.testing.assert_array_equal(cp.asnumpy(source), image)
        assert actual.shape == shape and np.all(np.isfinite(actual))
        assert actual.min() >= -50 and actual.max() <= 0
        if shape[0] > 32:
            continue
        normalized = np.maximum(image - image.max(), -80.)
        normalized = (normalized - normalized.min()) / max(np.ptp(normalized), 1e-12)
        extra = [(-size) % 16 for size in shape]
        # Upstream make_shape pads both sides, then prepends any odd remainder.
        padded = np.pad(normalized, [(n // 2, n // 2) for n in extra], mode='symmetric')
        padded = np.pad(padded, [(n % 2, 0) for n in extra], mode='symmetric')[None, None]
        expected = reference.run(None, {'image': padded})[0][0, 0]
        z, x = [(n + 1) // 2 for n in extra]
        expected = np.clip(expected[z:z+shape[0], x:x+shape[1]] * 50 - 50 + DISPLAY_GAIN_DB, -50, 0)
        np.testing.assert_allclose(actual, expected, rtol=0, atol=.001)
    print('PASS: GPU/reference parity, odd/constant inputs, full PW4cm grids and source preservation')


def assets(engine):
    paths = [engine.with_suffix(suffix) for suffix in ('.h5', '.onnx', '.engine', '.json')]
    before = [(path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()) for path in paths]
    download = setup.urlopen
    setup.urlopen = lambda *a, **k: (_ for _ in ()).throw(AssertionError('Reuse attempted a download'))
    try:
        setup.prepare(engine)
    finally:
        setup.urlopen = download
    assert before == [(p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest()) for p in paths]
    with TemporaryDirectory() as directory:
        target = Path(directory) / 'mimicknet.engine'
        target.symlink_to(engine.resolve())
        original = json.loads(engine.with_suffix('.json').read_text())
        for key in ('gpu', 'tensorrt'):
            changed = dict(original, build=dict(original['build'], **{key: 'incompatible'}))
            sidecar = target.with_suffix('.json')
            sidecar.write_text(json.dumps(changed))
            try:
                setup.prepare(target)
            except RuntimeError as error:
                assert 'Existing assets preserved' in str(error) and 'incompatible' in str(error)
            else:
                raise AssertionError(f'Accepted incompatible {key}')
            assert json.loads(sidecar.read_text()) == changed
        weights = Path(directory) / 'bad.h5'
        weights.write_bytes(b'not the pinned model')
        try:
            setup.export_onnx(weights, weights.with_suffix('.onnx'))
        except ValueError as error:
            assert 'checksum mismatch' in str(error)
        else:
            raise AssertionError('Accepted corrupt weights')
    print('PASS: offline reuse, GPU/TensorRT mismatch preservation and corrupt-weight rejection')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('engine', type=Path)
    args = parser.parse_args()
    parity(args.engine)
    assets(args.engine)
