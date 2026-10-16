# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Converters: depth/time -> samples -> records -> frame bytes."""

import math

from .raw import FRAME_ALIGNMENT_BYTES, RECORD_BYTES


def samples_to_depth(samples, c=1480.0, fs=50e6):
    return 0.5 * c * samples / fs


def as_samples(depth=None, time=None, samples=None, c=1480.0, fs=50e6):
    if sum(value is not None for value in (depth, time, samples)) != 1:
        raise ValueError("give exactly one of depth, time, samples")
    if samples is not None:
        return int(samples)
    if time is None:
        time = 2.0 * depth / c
    return int(math.ceil(time * fs))


def samples_to_records(samples):
    return 2 * samples


def records_to_frame_bytes(records):
    raw_bytes = records * RECORD_BYTES
    blocks = -(-raw_bytes // FRAME_ALIGNMENT_BYTES)
    return blocks * FRAME_ALIGNMENT_BYTES


def seconds_to_cycles(seconds, clock_hz):
    return int(round(seconds * clock_hz))
