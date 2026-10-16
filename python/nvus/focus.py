# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Focused linear scan geometry; no GPU or hardware access."""

import math

from . import tx


def focus_profile(x, z, c=1480.0):
    """Return full-aperture delays and nominal focal arrival time from launch.

    Quantize to whole BF clocks, as the proven plane-wave path does. Values
    returned to the TX driver are half-cycle units. Never clamp overflow.
    """
    if not all(math.isfinite(v) for v in (x, z, c)) or z <= 0 or c <= 0:
        raise ValueError("focus coordinates must be finite; depth and sound speed positive")
    edge = (tx.TX_PW_ELEMENTS - 1) * tx.TX_PW_PITCH_M / 2
    if abs(x) > edge:
        raise ValueError("linear scan focus must lie within the probe aperture")
    distances = [math.hypot((i - (tx.TX_PW_ELEMENTS - 1) / 2)
                            * tx.TX_PW_PITCH_M - x, z)
                 for i in range(tx.TX_PW_ELEMENTS)]
    arrival = max(distances) / c
    delays = [2 * round((arrival - r / c) * tx.BF_CLOCK_HZ) for r in distances]
    for value in delays:
        tx.delay_word(value)
    return delays, arrival


def linear_foci(lines, width, depth, c=1480.0):
    """Uniform vertical scanlines centered on the probe, distances in metres."""
    if not isinstance(lines, int) or lines < 2:
        raise ValueError("focused scan requires at least two lines")
    if not math.isfinite(width) or width <= 0:
        raise ValueError("focused scan width must be finite and positive")
    foci = tuple((-width / 2 + i * width / (lines - 1), depth) for i in range(lines))
    for x, z in foci:
        focus_profile(x, z, c)
    return foci
