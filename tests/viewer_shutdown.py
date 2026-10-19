#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Exercise Ctrl+C on the real viewer and its parent; no acquisition or hardware."""
import os
import signal

from nvus.scanner_display import Display


def main():
    display = Display(scale=1)
    try:
        display._start((64, 64), {})
        viewer = display._process
        os.kill(viewer.pid, signal.SIGINT)
        try:
            os.kill(os.getpid(), signal.SIGINT)
        except KeyboardInterrupt:
            display.close()
        assert not viewer.is_alive() and viewer.exitcode == 0
        assert 'error' not in display.report, display.report
    finally:
        display.close()
    print('PASS: Ctrl+C handled by parent; viewer stopped cleanly; no hardware')


if __name__ == '__main__':
    main()
