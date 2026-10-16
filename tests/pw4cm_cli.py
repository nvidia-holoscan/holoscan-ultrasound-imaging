#!/usr/bin/env python3
"""Installed PW4cm CLI and native simulation; never opens hardware."""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from nvus import _pw4cm
from nvus.native_observation import read_native_observation


def cli(*options, expected=0):
    result = subprocess.run([sys.executable, '-m', 'nvus.cli', 'run', 'pw4cm',
                             *options, '--format', 'json'], capture_output=True, text=True)
    assert result.returncode == expected, result.stderr
    return json.loads(result.stdout) if result.stdout else None


def main():
    report = cli('--dry-run')
    assert report['effect'] == 'offline' and report['ok']
    assert 'requires a terminal' in cli(expected=1)['error']
    assert '--pretty' in report['arguments'] and '--continuous' in report['arguments']
    assert '--mimicknet' not in report['arguments']
    assert '--mimicknet' in cli('--dry-run', '--mimicknet')['arguments']
    for options in (('--frames', '0'), ('--duration', 'nan'), ('--duration', '0s'),
                    ('--duration', '3m'), ('--continuous', '--duration', '2m')):
        cli(*options, expected=2)
    assert '--max-seconds=120.0' in cli('--dry-run', '--duration', '2m')['arguments']
    assert '--continuous' in cli('--dry-run', '--continuous')['arguments']
    assert 'require --output' in cli('--dry-run', '--record-frames', '2', expected=1)['error']
    with TemporaryDirectory() as directory:
        for rollout in ('1', '32'):
            recording = str(Path(directory) / rollout)
            report = cli('--dry-run', '--frames', '2', '--headless', '--rollout-length', rollout,
                         '--output', recording)
            assert '--no-display' in report['arguments']
            result = _pw4cm.main(['--simulate', *report['arguments'][1:]])['result']
            assert result['simulated'] and result['frames'] == 2 and result['received_events'] == 128
            assert result['completed_logical_transmits'] == 64
            assert result['completion'] == 'Repetitions' and Path(recording).is_dir()
            observations = list((Path(recording) / 'observations').iterdir())
            assert len(observations) == 1
            for path in observations:
                observation = read_native_observation(path)
                assert observation.complete and len(observation.records) == len(observation.rf) == 64
    report = cli('--dry-run', '--duration', '1s', '--headless')
    result = _pw4cm.main(['--simulate', *report['arguments'][1:]])['result']
    assert result['simulated'] and result['completion'] == 'TimeLimit'
    print('PASS: PW4cm CLI, stopping options, recording and native rollouts 1/32; no hardware')


if __name__ == '__main__':
    main()
