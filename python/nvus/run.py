# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""CLI adapter for the commissioned PW4cm demo; no second acquisition path."""
import argparse
from contextlib import redirect_stdout
import math
import sys


def duration(value):
    try:
        seconds = float(value[:-1]) * {'s': 1, 'm': 60}[value[-1]]
        if math.isfinite(seconds) and 0 < seconds <= 120:
            return seconds
    except (ValueError, KeyError, IndexError):
        pass
    raise argparse.ArgumentTypeError('Use a positive duration up to 120s, e.g. 30s or 2m')


def parser(commands, common):
    run = common(commands.add_parser('run', add_help=False, help='This will start the ultrasound hardware'))
    pw = common(run.add_subparsers(dest='recipe').add_parser('pw4cm', add_help=False,
                description='Continuous PW4cm imaging. This will start the ultrasound hardware.'))
    stopping = pw.add_mutually_exclusive_group()
    stopping.add_argument('--frames', type=int, choices=range(1, 1025), metavar='N')
    stopping.add_argument('--duration', type=duration, metavar='TIME')
    stopping.add_argument('--continuous', action='store_true', help='Run until stopped (default)')
    pw.add_argument('--headless', action='store_true', help='Reconstruct without a viewer; still transmits')
    pw.add_argument('--dry-run', action='store_true', help='Print settings without opening hardware')
    pw.add_argument('--mimicknet', action='store_true', help='Enable enhancement prepared by ./setup --mimicknet')
    pw.add_argument('--rollout-length', type=int, choices=(1, 32), default=32)
    pw.add_argument('--ibv-name', help='Board-facing RDMA NIC')
    pw.add_argument('--output', help='New directory for recorded RF')
    pw.add_argument('--record-frames', type=int, help='Frames to save with --output (default: 1)')
    pw.add_argument('--record-skip', type=int, help='Warm-up frames to skip (default: 0)')


def arguments(args):
    result = ['--commissioning', '--fpga-auto-sync-start', '--fpga-auto-sync-guard-us=0',
              '--fpga-auto-sync-pri-us=100', '--tx-dual-bank', '--fpga-tx-release-lead-us=35',
              '--rx-source-ohms=800', '--tx-dtgc-enable', '--tx-dtgc-start-db=12',
              '--tx-dtgc-start-us=2', '--tx-dtgc-ramp-us=10',
              f'--rollout-length={args.rollout_length}', '--no-display' if args.headless else '--pretty']
    result += ([f'--frame-limit={args.frames}'] if args.frames else ['--continuous'])
    if args.duration:
        result.append(f'--max-seconds={args.duration}')
    if args.ibv_name:
        result.append(f'--ibv-name={args.ibv_name}')
    if args.mimicknet:
        result.append('--mimicknet')
    if args.output:
        result += [f'--record-rf-dir={args.output}',
                   f'--record-rf-frames={1 if args.record_frames is None else args.record_frames}',
                   f'--record-rf-skip={0 if args.record_skip is None else args.record_skip}']
    return result


def execute(args):
    from .cli import make_report
    report = make_report('run pw4cm')
    report.update(ok=False, effect='offline' if args.dry_run else 'transmit', arguments=arguments(args))
    try:
        if not args.output and (args.record_frames is not None or args.record_skip is not None):
            raise ValueError('--record-frames and --record-skip require --output')
        if not args.dry_run and not sys.stdin.isatty():
            raise ValueError('Live imaging requires a terminal for Ctrl+C; do not pipe or redirect ./nvus')
        from . import _pw4cm
        if args.dry_run:
            _pw4cm.parse_args(['--simulate', '--process', *report['arguments'][1:]])
        else:
            with redirect_stdout(sys.stderr):
                report.update(_pw4cm.main(report['arguments']))
        report['ok'] = True
    except Exception as error:
        report['error'] = str(error)
    return report


def print_run(report):
    if report['ok']:
        print('PW4cm: ' + (' '.join(report['arguments']) if report['effect'] == 'offline' else 'complete'))
    if 'error' in report:
        print(report['error'], file=sys.stderr)
