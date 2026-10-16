# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""NVUS LVDS wire format: constants, CPU decode, element mapping, frame dump.

One 48-byte record carries one 24-byte word per AFE: 16 lanes x 12 bit,
LSB-first. Physical channel c rides lane c >> 1 with record parity c & 1.
"""

import json
import os

import numpy as np

RECORD_BYTES = 48
AFE_SAMPLE_BYTES = 24
AFES_PER_RECORD = 2
LANES_PER_AFE = 16
CHANNELS_PER_AFE = 32
SAMPLE_MASK = 0xFFF
FRAME_ALIGNMENT_BYTES = 192
AFE_NAMES = ("afe1", "afe2")
FS = 50e6
FC = 5e6
N_ELEMENTS = 128
PITCH_M = 0.3e-3


def sign_extend_12(value):
    value &= SAMPLE_MASK
    return value - 0x1000 if value & 0x800 else value


def unpack_afe_sample(data):
    word = int.from_bytes(data[:AFE_SAMPLE_BYTES], "little")
    return [(word >> (12 * lane)) & SAMPLE_MASK for lane in range(LANES_PER_AFE)]


def element_index(afe, physical_channel, rx_select):
    """Probe element observed by one capture column under one TR_SW_SEL half.

    The RX mux halves are wired as contiguous element blocks: "odd" routes
    elements 0-63 and "even" routes 64-127, each in capture-column order
    (afe1 ch0-31, then afe2 ch0-31).
    """
    if rx_select not in ("odd", "even"):
        raise ValueError(f'rx_select must be "odd" or "even", not {rx_select!r}')
    half_offset = 0 if rx_select == "odd" else N_ELEMENTS // 2
    return half_offset + afe * CHANNELS_PER_AFE + physical_channel


def element_indices(rx_select):
    """Element for every capture column of one TR_SW_SEL half, in column order."""
    return [element_index(afe, channel, rx_select)
            for afe in range(AFES_PER_RECORD)
            for channel in range(CHANNELS_PER_AFE)]


def _decode_lane_words(body):
    triples = body.reshape(-1, 3).astype(np.int32)
    low = triples[:, 0] | ((triples[:, 1] & 0x0F) << 8)
    high = (triples[:, 1] >> 4) | (triples[:, 2] << 4)
    words = np.empty(low.size * 2, dtype=np.int32)
    words[0::2] = low
    words[1::2] = high
    return np.where(words >= 2048, words - 4096, words)


def unpack_records(data, record_offset=0, start_record=0, max_records=None, merge_phases=True):
    raw = np.frombuffer(data, dtype=np.uint8)
    if not 0 <= record_offset < RECORD_BYTES:
        raise ValueError(f"record_offset must be 0..{RECORD_BYTES - 1}")
    if start_record < 0:
        raise ValueError("start_record must be >= 0")
    start = record_offset + start_record * RECORD_BYTES
    if start > raw.size:
        raise ValueError("start_record is beyond the input")

    records = (raw.size - start) // RECORD_BYTES
    if max_records:
        records = min(records, max_records)
    lanes = _decode_lane_words(raw[start : start + records * RECORD_BYTES])
    lanes = lanes.reshape(records, AFES_PER_RECORD, LANES_PER_AFE)
    trailing_bytes = raw.size - (start + records * RECORD_BYTES)

    if not merge_phases:
        # Raw LVDS lane view: one sample per lane per record, no parity
        # de-interleave; lane order is the 12-bit field order on the wire.
        info = {
            "records": records,
            "samples": records,
            "trailing_bytes": trailing_bytes,
        }
        return lanes.reshape(records, -1), info

    rows = records // 2
    base = start_record & 1  # rows whose absolute record index is even
    by_phase = (lanes[base::2][:rows], lanes[base ^ 1 :: 2][:rows])
    samples = np.stack(by_phase, axis=-1)  # (rows, afe, lane, phase)
    samples = samples.reshape(rows, -1)

    info = {
        "records": records,
        "samples": rows,
        "trailing_bytes": trailing_bytes,
    }
    return samples, info


def dump_frame(directory, index, data, metadata, prefix="frame"):
    os.makedirs(directory, exist_ok=True)
    stem = os.path.join(directory, f"{prefix}_{index:06d}")
    raw_bytes = data.tobytes() if hasattr(data, "tobytes") else bytes(data)
    with open(f"{stem}.bin", "wb") as f:
        f.write(raw_bytes)
    with open(f"{stem}.json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, sort_keys=True)
    return f"{stem}.bin"
