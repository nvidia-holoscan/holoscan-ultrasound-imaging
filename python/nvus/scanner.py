# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Owned RF observations and recording outside the per-shot receive path."""
import hashlib
import json
from pathlib import Path


class Observation:
    def __init__(self, native, prepared, rf=None):
        if native.sequence_id != prepared.id or native.config_id != prepared.config_id:
            raise ValueError('observation provenance does not match its prepared sequence')
        self._native, self.prepared, self._rf = native, prepared, rf
        self.id, self.run_id, self.boundary = native.id, native.run_id, native.boundary
        self.complete, self.error = native.complete, native.error
        self.records = tuple(native.records)
        self.timestamp_origin = 'host_monotonic_before_acquire'
        self.identity_source = 'host_inferred'  # No FPGA event-ID echo is claimed.
        if len(self.records) > len(prepared.events) or (self.complete and len(self.records) != len(prepared.events)):
            raise ValueError('observation event count does not match its completion state')
        for record, event in zip(self.records, prepared.events):
            expected = (event.event_id, 0 if event.rx_side == 'odd' else 1, event.samples)
            if (record.event_id, record.rx_side, record.samples) != expected:
                raise ValueError('observation event identity/receive geometry mismatch')
            if (getattr(record, 'timestamp_origin', self.timestamp_origin) != self.timestamp_origin
                    or getattr(record, 'association', self.identity_source) != self.identity_source):
                raise ValueError('unsupported event timing or association provenance')
        if rf is not None:
            self._validate_rf(rf)

    def _validate_rf(self, rf):
        if len(rf) != len(self.records):
            raise ValueError('observation RF count differs from event records')
        if any(len(data) != record.samples*96 for data, record in zip(rf, self.records)):
            raise ValueError('observation RF byte count differs from receive geometry')

    @property
    def rf(self):
        if self._rf is None:
            rf = tuple(buffer.to_bytes() for buffer in self._native.buffers)
            self._validate_rf(rf)
            self._rf = rf
        return self._rf

    def gpu_rf(self):
        """Lease RF on the current CUDA stream, fencing consumers on exit.

        CUDA views retain their native owner. Do not enqueue work using these
        views after leaving the context, or switch streams inside the context.
        Host observations yield their ordinary byte buffers.
        """
        from contextlib import contextmanager

        @contextmanager
        def lease():
            buffers = tuple(getattr(self._native, 'buffers', ()))
            cuda = [b for b in buffers if b.kind.name == 'Cuda']
            if not cuda:
                yield self.rf
                return
            import cupy as cp
            stream = cp.cuda.get_current_stream()
            device = cp.cuda.runtime.getDevice()
            if any(b.device != device for b in cuda):
                raise ValueError('RF CUDA buffers must belong to the current CUDA device')
            leased = []
            try:
                pages = []
                for buffer in buffers:
                    if buffer.kind.name != 'Cuda':
                        pages.append(buffer.to_bytes())
                        continue
                    buffer.wait_ready()
                    memory = cp.cuda.UnownedMemory(buffer.device_pointer, buffer.size,
                                                  buffer, device_id=buffer.device)
                    pages.append(cp.ndarray((buffer.size,), dtype=cp.uint8,
                                            memptr=cp.cuda.MemoryPointer(memory, 0)))
                    leased.append(buffer)
                self._validate_rf(pages)
                yield tuple(pages)
            finally:
                for buffer in leased:
                    try:
                        buffer.record_consumer(stream.ptr)
                    except Exception:
                        # A failed event insertion must not permit early free.
                        stream.synchronize()
                        raise
        return lease()

    def wait_ready(self):
        if self._rf is None:
            for buffer in self._native.buffers:
                buffer.wait_ready()


class ObservationRecorder:
    """Explicit caller-side recording of owned observations; never runs in RX."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def write(self, observation):
        path = self.directory/f'observation_{observation.run_id}_{observation.id}'
        path.mkdir()  # Never overwrite an earlier capture.
        records = []
        for i, (record, data) in enumerate(zip(observation.records, observation.rf, strict=True)):
            name = f'event_{i:06d}.bin'
            (path/name).write_bytes(data)
            records.append({'event_id':record.event_id,'rx_side':record.rx_side,
                            'samples':record.samples,'timestamp_ns':record.timestamp_ns,
                            'timestamp_origin':observation.timestamp_origin,
                            'association':observation.identity_source,
                            'file':name,'sha256':hashlib.sha256(data).hexdigest()})
        metadata = dict(schema=1,id=observation.id,run_id=observation.run_id,
                        boundary=observation.boundary,complete=observation.complete,error=observation.error,
                        timestamp_origin=observation.timestamp_origin,identity_source=observation.identity_source,
                        prepared=observation.prepared.manifest(), records=records)
        (path/'observation.json').write_text(json.dumps(metadata,indent=2,allow_nan=False)+'\n')
        return path
