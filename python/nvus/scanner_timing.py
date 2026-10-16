# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Offline timing inspection and bounded, consumer-side host cadence reporting.

Neither calculated windows nor host timestamps establish hardware launch timing.
"""
from collections import deque
import math
import statistics

from . import tx


def inspect_timing(prepared):
    """Explain a compiled batch; perform no hardware, native-runtime or GPU I/O.

    The window floor reproduces the current compiler's constraints. It is not
    a qualification of geometric coverage, recovery time or transport capacity.
    """
    board = prepared.board
    tx_start_us = round(board.tx_delay_us * board.sync_clock_hz / 1e6) / board.sync_clock_hz * 1e6
    events = []
    for event in prepared.events:
        capture_start_us = tx_start_us + event.start_us
        capture_duration_us = event.samples / board.sample_rate_hz * 1e6
        last_pulse_end_us = (tx_start_us + max(event.delays_half_cycles) / (2 * tx.BF_CLOCK_HZ) * 1e6
                             + event.request.transmit.waveform.cycles / event.actual_frequency_hz * 1e6)
        capture_end_us = capture_start_us + capture_duration_us
        # Keep the requested board offset distinct from the rounded launch time.
        compiler_floor_us = (max(capture_end_us, last_pulse_end_us) - tx_start_us
                             + board.tx_delay_us + board.capture_margin_us)
        events.append(dict(
            event_id=event.event_id, geometry_id=event.request.geometry_id, rx_half=event.rx_side,
            samples=event.samples, rf_bytes=event.samples * 96,
            requested_interval_us=event.request.interval_us,
            resolved_interval_us=event.interval_ns / 1000,
            capture_start_us=capture_start_us, capture_duration_us=capture_duration_us,
            capture_end_us=capture_end_us, last_pulse_end_us=last_pulse_end_us,
            margin_us=board.capture_margin_us,
            compiler_window_floor_us=compiler_floor_us,
        ))
    nominal_period_s = sum(e.interval_ns for e in prepared.events) / 1e9
    return dict(
        schema=1, prepared_id=prepared.id, physical_events=len(events),
        offsets_reference='FPGA sync sequence start; not a measured acoustic timestamp',
        pacing_reference='host_monotonic_before_acquire',
        nominal_batch_period_ms=nominal_period_s * 1000,
        nominal_batches_per_second=1 / nominal_period_s,
        hardware_launch_timing_qualified=False,
        interpretation='Nominal cadence assumes the requested spacing is achieved. '
                       'Host transport, RF retention, consumers and scheduling may slow execution. '
                       'The compiler window floor is not a qualified minimum PRI.',
        events=events,
    )

def _distribution(values):
    values = sorted(values)
    if not values:
        return dict(count=0, mean_ms=None, median_ms=None, p95_ms=None, p99_ms=None, max_ms=None)
    mean = statistics.fmean(values)
    return dict(count=len(values), mean_ms=mean,
                median_ms=statistics.median(values),
                p95_ms=values[math.ceil(.95 * len(values)) - 1],
                p99_ms=values[math.ceil(.99 * len(values)) - 1], max_ms=values[-1])


class Cadence:
    """Measure completed observations outside RX; never read/copy RF for timing.

    Keep the last 65,536 intervals. Event gaps include batch-boundary stalls;
    frame completion gaps include the consumer's processing and presentation.
    """
    def __init__(self):
        self._event_gaps = deque(maxlen=65536)
        self._excess = deque(maxlen=65536)
        self._frame_gaps = deque(maxlen=65536)
        self._acquisition_gaps = deque(maxlen=65536)
        self._previous_acquisition = None
        self._previous_event = self._previous_completion = self._run_id = None
        self.events = self.frames = 0
        self.exceeded_requested_spacing = 0

    def record(self, observation, completed_ns):
        if not observation.complete:
            raise ValueError('cadence requires a complete observation')
        if observation.timestamp_origin != 'host_monotonic_before_acquire':
            raise ValueError('unsupported timestamp origin')
        if self._run_id is not None and observation.run_id != self._run_id:
            raise ValueError('cadence cannot mix runs')
        self._run_id = observation.run_id
        # Reconstruction retains canonical event order. A short rollout may
        # execute both RX halves of one logical wave together, so chronology is
        # derived from the preserved host timestamps instead of list position.
        ordered = sorted(zip(observation.records, observation.prepared.events,
                             strict=True), key=lambda pair: pair[0].timestamp_ns)
        acquisition = ordered[0][0].timestamp_ns
        if self._previous_acquisition is not None:
            self._acquisition_gaps.append((acquisition-self._previous_acquisition)/1e6)
        self._previous_acquisition = acquisition
        for record, event in ordered:
            if self._previous_event is not None:
                previous_ns, previous_interval_ns = self._previous_event
                gap_ns = record.timestamp_ns - previous_ns
                if gap_ns < 0:
                    raise ValueError('host timestamps must be monotonic')
                self._event_gaps.append(gap_ns / 1e6)
                excess_ns = max(0, gap_ns - previous_interval_ns)
                self._excess.append(excess_ns / 1e6)
                self.exceeded_requested_spacing += int(excess_ns > 0)
            self._previous_event = record.timestamp_ns, event.interval_ns
            self.events += 1
        if self._previous_completion is not None:
            if completed_ns < self._previous_completion:
                raise ValueError('completion timestamps must be monotonic')
            self._frame_gaps.append((completed_ns - self._previous_completion) / 1e6)
        self._previous_completion = completed_ns
        self.frames += 1

    def report(self):
        completion = _distribution(self._frame_gaps)
        median = completion['median_ms']
        acquisition = _distribution(self._acquisition_gaps)
        return dict(
            timestamp_origin='host_monotonic_before_acquire', hardware_launch_timestamps=False,
            events=self.events, frames=self.frames,
            statistics='All completions, including warmup; last 65536 intervals; '
                       'median and nearest-rank p95/p99. No setup/cleanup interval.',
            event_start_intervals=_distribution(self._event_gaps),
            acquisition_frame_start_intervals=acquisition,
            acquisition_fps=1000/acquisition['mean_ms'] if acquisition['mean_ms'] else None,
            excess_over_requested_spacing=_distribution(self._excess),
            intervals_exceeding_requested_spacing=self.exceeded_requested_spacing,
            consumer_completion_intervals=completion,
            pipeline_fps=1000/completion['mean_ms'] if completion['mean_ms'] else None,
            fps_from_median_completion_interval=1000 / median if median else None,
            interpretation='Excess includes host scheduling and batch/consumer stalls; '
                           'it is not a hardware deadline-miss measurement.',
        )
