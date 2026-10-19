#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""End-to-end lifecycle checks for the C++ policy executor."""
import os
from pathlib import Path
import sys
import time

if not os.environ.get('NVUS_USE_INSTALLED'):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))

from nvus import _scanner as cpp


def create(rollout_length, groups=12, duration_ms=5000, capacity=2,
           continuous=False, continuous_duration_ms=0):
    angles = [-7.75 + .5*index for index in range(32)]
    hardware = cpp.FakeHardware()
    limits = cpp.PolicyLimits()
    limits.groups = 0 if continuous else groups
    limits.max_duration_ms = continuous_duration_ms if continuous else duration_ms
    limits.queue_capacity = capacity
    limits.continuous = continuous
    policy = cpp.Pw4cmPolicy(angles)
    return hardware, cpp.PolicyExecutor(
        hardware, policy, rollout_length, limits)


def drain(scanner):
    groups = []
    while True:
        group = scanner.next(100)
        if group is not None:
            groups.append(group)
        elif scanner.wait(0):
            return groups


def happy_path(rollout_length):
    hardware, scanner = create(rollout_length)
    try:
        scanner.start()
        groups = drain(scanner)
        status = scanner.status()
        calls = 12 * 32 // rollout_length
        assert len(groups) == 12 and status.received_events == 768
        assert all(len(group.transmits) == 32 for group in groups)
        assert hardware.program_calls == hardware.verify_calls == 1
        assert hardware.transmit_sequence_updates == calls - 1
        assert hardware.stop_calls == 1
    finally:
        scanner.stop()
        scanner.wait()
    print(f'PASS: rollout {rollout_length}, {calls} policy calls, 12 groups')


def runs_without_python():
    hardware, scanner = create(1)
    previous = sys.getswitchinterval()
    try:
        sys.setswitchinterval(10.)
        scanner.start()
        until = time.perf_counter() + .5
        while time.perf_counter() < until:
            pass
        assert hardware.acquired >= 128
        assert len(drain(scanner)) == 12
    finally:
        sys.setswitchinterval(previous)
        scanner.stop()
        scanner.wait()
    print('PASS: C++ policy and executor advance while Python holds the GIL')


def continuous_stop():
    hardware, scanner = create(32, continuous=True)
    try:
        scanner.start()
        groups = [scanner.next(1000) for _ in range(3)]
        assert all(group is not None for group in groups)
        scanner.stop()
        assert scanner.wait(2000)
        status = scanner.status()
        assert status.reason == cpp.Completion.Stopped
        assert status.completed_groups >= 3 and hardware.stop_calls == 1
    finally:
        scanner.stop()
        scanner.wait()
    print('PASS: continuous policy stops cleanly on request')


def continuous_time_limit():
    hardware, scanner = create(
        32, continuous=True, continuous_duration_ms=100)
    try:
        scanner.start()
        groups = drain(scanner)
        status = scanner.status()
        assert groups and status.state == cpp.State.Idle
        assert status.reason == cpp.Completion.TimeLimit and not status.error
        assert status.completed_groups == len(groups)
        assert hardware.stop_calls == 1
    finally:
        scanner.stop()
        scanner.wait()
    print('PASS: continuous policy reaches its time limit cleanly')


def injected_failure(kind):
    hardware, scanner = create(32, groups=3)
    if kind == 'verify':
        hardware.fail_verify = True
    elif kind == 'receive':
        hardware.fail_event = 1
    else:
        hardware.fail_stop = True
    try:
        scanner.start()
        drain(scanner)
        status = scanner.status()
        assert status.state == cpp.State.Fault and hardware.stop_calls == 1
        assert status.error or status.cleanup_error
    finally:
        scanner.stop()
        scanner.wait()
    print(f'PASS: {kind} failure remains visible')


def absent_consumer_is_bounded():
    hardware, scanner = create(32, groups=12, duration_ms=100, capacity=1)
    try:
        scanner.start()
        assert scanner.wait(2000)
        status = scanner.status()
        assert status.queued_observations <= 1
        assert status.state == cpp.State.Fault and hardware.stop_calls == 1
    finally:
        scanner.stop()
        scanner.wait()
    print('PASS: absent consumer stops at the bounded run deadline')


def main():
    happy_path(1)
    happy_path(32)
    runs_without_python()
    continuous_stop()
    continuous_time_limit()
    for kind in ('verify', 'receive', 'cleanup'):
        injected_failure(kind)
    absent_consumer_is_bounded()


if __name__ == '__main__':
    main()
