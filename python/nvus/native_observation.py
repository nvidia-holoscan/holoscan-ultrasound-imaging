# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Native acquisition metadata and offline PW4cm RF replay."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from . import raw
from .scanner import Observation
from .scanner_sequence import (BoardConfig, Event, Receive, Transmit, Waveform,
                               PreparedEvent, PreparedSequence, PreparedWaveform)


def prepared_view(action, board=None):
    """Expose the C++ action's achieved delays/bytes to existing GPU consumers.

    This is downstream provenance, not the Python compiler or control API. The
    native loop already owns/submits its next action without calling this code.
    """
    native = action.sequence
    profiles = tuple(tuple(values) for values in action.delays_half_cycles)
    count = len(action.angles_deg)
    events = tuple(native.events)
    if not count or len(profiles) != count or len(events) != 2 * count:
        raise ValueError('Incomplete native PW action metadata')
    board = BoardConfig() if board is None else board
    waveform = Waveform()
    transmits = tuple(Transmit(waveform, tuple(d / 400e6 for d in profile))
                      for profile in profiles)
    start_us = native.rx_start_ticks / board.sync_clock_hz * 1e6
    compiled = []
    for index, event in enumerate(events):
        geometry = index % count
        side = 'odd' if index < count else 'even'
        if event.event_id != index or event.rx_side != int(side == 'even'):
            raise ValueError('Native PW event ordering differs from reconstruction')
        request = Event(transmits[geometry], Receive(side, start_us, event.samples),
                        event.interval_ns / 1000., event.event_id, geometry)
        compiled.append(PreparedEvent(request, event.event_id, profiles[geometry], 5e6,
            event.samples, start_us, side, tuple(raw.element_indices(side)),
            event.interval_ns, 9, event.packet_bytes))
    words = tuple((word.address, word.value) for word in native.resources)
    if words != ((0x89, 0x20001), (0x8a, 0x9d99)):
        raise ValueError('Native PW waveform differs from the reference recipe')
    resource = PreparedWaveform(9, words, 5e6, 2, False, False)
    return PreparedSequence(native.id, native.config_id, tuple(compiled), (resource,),
                            board, 'streamed', sum(e.interval_ns for e in events) / 1e9)


def observation_view(frame, board=None):
    return Observation(frame.observation, prepared_view(frame.action, board))


def read_native_observation(path):
    """Read a C++-compiled PW observation with full manifest/checksum verification."""
    from . import _scanner as cpp

    path = Path(path)
    data = json.loads((path / 'observation.json').read_text())
    if data.get('schema') != 1:
        raise ValueError('unknown observation schema')
    if (data.get('timestamp_origin') != 'host_monotonic_before_acquire'
            or data.get('identity_source') != 'host_inferred'):
        raise ValueError('unsupported observation provenance')
    manifest = data['prepared']
    prepared = prepared_view(
        cpp.compile_plane_waves([-7.75 + .5 * index for index in range(32)]),
        BoardConfig(**manifest['board']))
    if (prepared.id != manifest['id']
            or prepared.config_id != manifest['config_id']
            or json.dumps(prepared.manifest(), sort_keys=True)
            != json.dumps(manifest, sort_keys=True)):
        raise ValueError('recorded native sequence does not match the current C++ compiler')
    records, rf = [], []
    for index, record in enumerate(data['records']):
        if record['file'] != f'event_{index:06d}.bin':
            raise ValueError('invalid recorded event path')
        payload = (path / record['file']).read_bytes()
        if hashlib.sha256(payload).hexdigest() != record['sha256']:
            raise ValueError('recorded RF checksum mismatch')
        records.append(SimpleNamespace(**record))
        rf.append(payload)
    native = SimpleNamespace(
        id=data['id'], run_id=data['run_id'], boundary=data['boundary'],
        complete=data['complete'], error=data['error'],
        sequence_id=prepared.id, config_id=prepared.config_id,
        records=tuple(records))
    return Observation(native, prepared, tuple(rf))
