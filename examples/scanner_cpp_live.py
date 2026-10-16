#!/usr/bin/env python3
"""Live PW4cm with a C++ policy/compiler/control loop and Python setup/viewer.

Inspection and simulation never open hardware. With --commissioning:
This will start the ultrasound hardware. No Python per-frame policy or packet compiler.
"""
import argparse
from contextlib import closing, contextmanager, nullcontext
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

if not __package__ and not os.environ.get('NVUS_USE_INSTALLED'):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import nvus
from nvus.hardware import ownership
from nvus import _scanner as cpp
from nvus.native_observation import observation_view, prepared_view
from nvus.scanner import ObservationRecorder
from nvus.scanner_sequence import BoardConfig
from nvus.scanner_timing import Cadence, inspect_timing


REPOSITORY = (Path.cwd() if __package__ == 'nvus'
              else Path(__file__).resolve().parents[1])


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def command_output(*command):
    result = subprocess.run(command, cwd=REPOSITORY, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError(f'{" ".join(command)} failed: {result.stderr.strip()}')
    return result.stdout.strip()


def verify_installed_python():
    """Refuse an installed environment whose Python package differs from this tree."""
    if not os.environ.get('NVUS_USE_INSTALLED'):
        extension = Path(cpp.__file__).resolve()
        return {'mode': 'source-tree', 'verified': False,
                'extension': str(extension),
                'extension_sha256': file_sha256(extension),
                'sdk_available': cpp.sdk_available}
    source = REPOSITORY / 'python' / 'nvus'
    if not source.is_dir():
        raise RuntimeError('Run PW4cm from the project folder')
    installed = Path(nvus.__file__).resolve().parent
    extension = Path(cpp.__file__).resolve()
    if installed == source.resolve() or extension.parent != installed:
        raise RuntimeError('Expected the installed nvus package and native extension; unset PYTHONPATH')
    mismatches = []
    hashes = {}
    files = {str(path.relative_to(source)): path for path in [*source.glob('*.py'), *source.glob('kernels/*.cu')]}
    files['_pw4cm.py'] = REPOSITORY / 'examples' / 'scanner_cpp_live.py'
    for name, path in sorted(files.items()):
        target = installed / name
        source_hash = file_sha256(path)
        installed_hash = file_sha256(target) if target.is_file() else None
        hashes[name] = source_hash
        if installed_hash != source_hash:
            mismatches.append(name)
    if mismatches:
        raise RuntimeError('Installed nvus Python differs from this checkout: '
                           + ', '.join(mismatches))
    inputs = [REPOSITORY / 'CMakeLists.txt', *sorted((REPOSITORY / 'cpp').rglob('*.cpp')),
              *sorted((REPOSITORY / 'cpp').rglob('*.hpp'))]
    identity = ''.join(
        f'{path.relative_to(REPOSITORY)}:{file_sha256(path)}\n'
        for path in sorted(inputs))
    native_hash = hashlib.sha256(identity.encode()).hexdigest()
    if getattr(cpp, 'native_source_sha256', None) != native_hash:
        raise RuntimeError('Installed native extension differs from this checkout')
    return {'mode': 'installed', 'verified': True, 'package': str(installed),
            'build_revision': getattr(cpp, 'repository_revision', None),
            'source_sha256': hashes, 'extension': str(extension),
            'extension_sha256': file_sha256(extension),
            'sdk_available': cpp.sdk_available}


def repository_metadata():
    git = ('git', '-c', f'safe.directory={REPOSITORY}')
    status = command_output(*git, 'status', '--porcelain', '--untracked-files=no')
    return dict(path=str(REPOSITORY),
                branch=command_output(*git, 'branch', '--show-current'),
                revision=command_output(*git, 'rev-parse', 'HEAD'), dirty=bool(status))


class RfRecording:
    """Bounded caller-side persistence for completed, owned observations."""
    RUN_METADATA_RESERVE_BYTES = 1024 * 1024
    FRAME_METADATA_RESERVE_BYTES = 2 * 1024 * 1024

    def __init__(self, args, prepared, environment):
        self.directory = args.record_rf_dir.resolve()
        self.skip = args.record_rf_skip
        self.frames = args.record_rf_frames
        self.paths = []
        self.connection = {}
        raw_per_frame = sum(event.samples * 96 for event in prepared.events)
        self.raw_bytes = raw_per_frame * self.frames
        self.estimated_bytes = (self.raw_bytes + self.RUN_METADATA_RESERVE_BYTES
                                + self.frames * self.FRAME_METADATA_RESERVE_BYTES)
        self.repository = repository_metadata()
        parent = self.directory.parent
        if not parent.is_dir():
            raise ValueError('record-rf-dir parent must already exist')
        available = shutil.disk_usage(parent).free
        if available < self.estimated_bytes:
            raise RuntimeError(
                f'Insufficient recording space: need {self.estimated_bytes} bytes, '
                f'only {available} bytes available')
        self.available_bytes_before = available
        self.directory.mkdir()  # Never merge with or overwrite prior results.
        self.recorder = ObservationRecorder(self.directory / 'observations')
        self.environment = environment
        print(f'RF recording estimate: {self.raw_bytes} raw bytes; '
              f'{self.estimated_bytes} bytes including metadata reserve; '
              f'{available} bytes available')

    def consume(self, frame_index, observation):
        if frame_index < self.skip or len(self.paths) >= self.frames:
            return
        path = self.recorder.write(observation)
        self.paths.append(str(path.relative_to(self.directory)))

    def manifest(self, args, initial, summary, error):
        result = summary.get('result', {})
        connection = result.get('connection', self.connection)
        return dict(
            schema='nvus.rx-attenuation-run.v1',
            status='complete' if error is None else 'failed',
            error=None if error is None else f'{type(error).__name__}: {error}',
            command=' '.join(shlex.quote(value) for value in sys.argv),
            repository=self.repository, environment=self.environment,
            requested=dict(frame_limit=args.frame_limit,
                continuous=args.continuous, max_seconds=args.max_seconds,
                record_rf_skip=self.skip, record_rf_frames=self.frames,
                rollout_length=args.rollout_length,
                queue_capacity=args.queue_capacity,
                receive_attenuation_code=initial.board.receive_attenuation_code,
                receive_source_ohms=initial.board.receive_source_ohms,
                receive_dtgc_start_db=initial.board.receive_dtgc_start_db,
                receive_dtgc_start_us=initial.board.receive_dtgc_start_us,
                receive_dtgc_ramp_us=initial.board.receive_dtgc_ramp_us,
                fpga_auto_sync_start=args.fpga_auto_sync_start,
                fpga_auto_sync_guard_us=args.fpga_auto_sync_guard_us,
                fpga_auto_sync_pri_us=args.fpga_auto_sync_pri_us,
                rx_arm_delay_s=args.rx_arm_delay_s,
                tx_dual_bank=args.tx_dual_bank,
                fpga_tx_release_lead_us=args.fpga_tx_release_lead_us,
                no_display=args.no_display, pretty=args.pretty),
            storage=dict(raw_bytes_estimate=self.raw_bytes,
                total_bytes_estimate=self.estimated_bytes,
                available_bytes_before=self.available_bytes_before),
            recorded=dict(count=len(self.paths), observations=self.paths),
            acquisition=result, connection=connection,
            acceptance=dict(
                complete=(error is None
                          and ((args.continuous
                                and result.get('completion') in ('Stopped', 'TimeLimit'))
                               or (not args.continuous
                                   and result.get('frames') == args.frame_limit
                                   and result.get('received_events')
                                       == 64 * args.frame_limit
                                   and result.get('completion') == 'Repetitions'))
                          and len(self.paths) == self.frames
                          and connection.get('restoration', {}).get('restored') is True)))

    def finalize(self, args, initial, summary, error=None):
        serialized = json.dumps(self.manifest(args, initial, summary, error),
                                indent=2, allow_nan=False) + '\n'
        path = self.directory / 'metadata.json'
        with path.open('x') as output:
            output.write(serialized)


def prefer_performance_cores():
    """Pin acquisition and receive to distinct fastest available CPU cores."""
    frequencies = {}
    for cpu in os.sched_getaffinity(0):
        try:
            path = Path(f'/sys/devices/system/cpu/cpu{cpu}/cpufreq/cpuinfo_max_freq')
            frequencies[cpu] = int(path.read_text())
        except (OSError, ValueError):
            return
    if len(set(frequencies.values())) < 2:
        return
    fastest = sorted(cpu for cpu, hz in frequencies.items()
                     if hz == max(frequencies.values()))
    if len(fastest) >= 2:
        os.environ.setdefault('NVUS_ACQUIRE_CPU', str(fastest[0]))
        os.environ.setdefault('NVUS_RECEIVE_CPU', str(fastest[1]))


class Images:
    """Reconstruct the one geometry declared by this PW4cm policy."""
    def __init__(self):
        self.processor = None
        self.angles = None

    def process(self, observation, action):
        from nvus.scanner_processing import Processor, Reconstruction
        angles = tuple(action.angles_deg)
        if self.processor is None:
            self.angles = angles
            self.processor = Processor(Reconstruction(angles_deg=angles,
                depth_mm=action.config.depth_mm, c=action.config.sound_speed_m_s,
                onset_sample=183, iq_profile='live_fir31', db_percentile=99.7))
        if angles != self.angles:
            raise RuntimeError('PW4cm observation geometry changed unexpectedly')
        return self.processor.process(observation).db

    @property
    def report(self):
        return dict(presentation='single PW4cm image', angles_deg=self.angles)


class RunResources:
    """Resources and results owned by one C++ policy run."""
    def __init__(self):
        self.executor = None
        self.session = None
        self.restoration = {'restored': True, 'scope': 'simulation; no device access'}

    @property
    def readback(self):
        return self.session.readback if self.session else {}


def make_hardware(args, initial, resources):
    if not args.commissioning:
        return cpp.FakeHardware()
    from nvus.scanner_hardware import _HsbSession
    prefer_performance_cores()
    resources.session = _HsbSession(
        args.hololink, initial.board, args.ibv_name, 0, 1024, True)
    config = resources.session.open(initial)
    config.rx_arm_delay_s = args.rx_arm_delay_s
    execution = cpp.HsbExecutionConfig()
    execution.fpga_auto_start = args.fpga_auto_sync_start
    execution.fpga_auto_guard_ticks = round(
        args.fpga_auto_sync_guard_us * initial.board.sync_clock_hz * 1e-6)
    execution.fpga_auto_interval_ticks = round(
        args.fpga_auto_sync_pri_us * initial.board.sync_clock_hz * 1e-6)
    execution.tx_dual_bank = args.tx_dual_bank
    execution.allow_sequence_wrap = args.continuous
    if args.tx_dual_bank:
        event = initial.events[0]
        capture_end_us = (initial.board.tx_delay_us + event.start_us +
                          event.samples / initial.board.sample_rate_hz * 1e6)
        release_us = capture_end_us - args.fpga_tx_release_lead_us
        capture_start_us = initial.board.tx_delay_us + event.start_us
        if release_us <= capture_start_us:
            raise ValueError('FPGA TX release lead leaves no in-capture update window')
        execution.fpga_tx_release_ticks = round(
            release_us * initial.board.sync_clock_hz * 1e-6)
    return cpp.make_hololink_hardware(config, execution)


def make_executor(args, hardware):
    limits = cpp.PolicyLimits()
    limits.groups = args.frame_limit
    limits.max_duration_ms = math.ceil(args.max_seconds * 1000)
    limits.queue_capacity = args.queue_capacity
    limits.host_pacing = not args.fpga_auto_sync_start
    limits.continuous = args.continuous
    angles = [-7.75 + .5*index for index in range(32)]
    policy = cpp.Pw4cmPolicy(angles)
    return cpp.PolicyExecutor(hardware, policy, args.rollout_length, limits)


def stop_executor(executor):
    if executor is None:
        return True, []
    try:
        executor.stop()
        quiescent = executor.wait()
        status = executor.status()
        if not quiescent or status.cleanup_error:
            raise RuntimeError(f'C++ executor shutdown unverified: {status.cleanup_error}')
        return True, []
    except BaseException as error:
        return False, [error]


def restore_hardware(resources, quiescent):
    if resources.session is None:
        return []
    if not quiescent:
        return [RuntimeError(
            'Analog restoration skipped: C++ shutdown is unverified')]
    try:
        resources.restoration = resources.session.close()
        return []
    except BaseException as error:
        return [error]


def finish_run(resources, primary):
    quiescent, errors = stop_executor(resources.executor)
    errors.extend(restore_hardware(resources, quiescent))
    if not errors:
        return
    failure = RuntimeError('; '.join(
        f'{type(error).__name__}: {error}' for error in errors))
    if primary is not None:
        raise failure from primary
    raise failure


@contextmanager
def cpp_policy_run(args, initial, resources=None):
    """Start one supervised C++ run, then stop and restore it in safety order."""
    resources = RunResources() if resources is None else resources
    try:
        hardware = make_hardware(args, initial, resources)
        resources.executor = make_executor(args, hardware)
        resources.executor.start()
        yield resources
    finally:
        finish_run(resources, sys.exc_info()[1])


def validate_frame(frame, index, board):
    observation = observation_view(frame, board)
    if not observation.complete or len(observation.records) != 64:
        raise RuntimeError(f'Incomplete C++ frame: {observation.error}')
    if observation.boundary != index + 1:
        raise RuntimeError('Missing or reordered C++ frame')
    expected = tuple(-7.75 + .5*i for i in range(32))
    if tuple(frame.action.angles_deg) != expected:
        raise RuntimeError('RF action differs from the PW4cm policy')
    return observation


def render_frame(args, images, display, observation, action):
    if not (args.commissioning or args.process):
        return True
    import cupy as cp
    db = images.process(observation, action)
    if db.shape != (528, 519) or not bool(cp.isfinite(db).all()):
        raise RuntimeError('Invalid reconstructed image')
    scanner = None
    if args.commissioning:
        event = observation.prepared.events[0]
        board = observation.prepared.board
        scanner = dict(
            probe_name='L7-3', probe_type='Linear 128', mode='B-Mode · PW Compound',
            depth_mm=float(action.config.depth_mm),
            tx_frequency_mhz=float(event.actual_frequency_hz / 1e6),
            pulse_cycles=event.request.transmit.waveform.cycles,
            angles=len(action.angles_deg), dynamic_range_db=50,
            receive_attenuation_db=board.receive_attenuation_code * .25,
            receive_source_ohms=board.receive_source_ohms,
            dtgc_start_db=board.receive_dtgc_start_db,
            dtgc_ramp_us=board.receive_dtgc_ramp_us,
            analog_lpf_mhz=board.analog_lpf_mhz)
    return display.show(db, scanner)


def consume_frames(args, executor, images, display, cadence, board, recording=None):
    count = 0
    try:
        while True:
            frame = executor.next(100)  # Periodically return for signals/UI.
            if frame is None:
                if executor.wait(0):
                    return count, False, False
                continue
            observation = validate_frame(frame, count, board)
            if recording is not None:
                recording.consume(count, observation)
            shown = render_frame(args, images, display, observation, frame.action)
            count += 1
            cadence.record(observation, time.monotonic_ns())
            if not shown:
                return count, True, False
    except KeyboardInterrupt:
        return count, False, True


def validate_completion(args, status, count, window_closed, operator_stopped,
                        recording=None):
    failed = (status.state == cpp.State.Fault or status.cleanup_error or
              (status.error and status.reason != cpp.Completion.Stopped))
    if failed:
        raise RuntimeError(
            f'C++ acquisition failed: {status.error}; {status.cleanup_error}')
    if args.continuous:
        expected = {cpp.Completion.Stopped}
        if args.max_seconds:
            expected.add(cpp.Completion.TimeLimit)
        if (status.reason not in expected
                or (status.reason == cpp.Completion.Stopped
                    and not (window_closed or operator_stopped))):
            raise RuntimeError(f'Unexpected continuous-run completion: {status.reason}')
    else:
        incomplete = (count != args.frame_limit or
                      status.received_events != 64 * args.frame_limit or
                      status.reason != cpp.Completion.Repetitions)
        if not (window_closed or operator_stopped) and incomplete:
            raise RuntimeError(
                f'Incomplete run: {count}/{args.frame_limit}; {status.reason}')
    if recording is not None and len(recording.paths) != recording.frames:
        raise RuntimeError(
            f'Incomplete RF recording: {len(recording.paths)}/{recording.frames}')


def make_enhancement(args):
    if not args.mimicknet:
        return None
    engine = Path(os.environ.get(
        'MIMICKNET_ENGINE_PATH',
        Path.cwd() / '.runtime/mimicknet.engine'))
    import cupy as cp
    from nvus.mimicknet import MimickNet
    operation = MimickNet(engine)
    image = cp.linspace(-80., 0., 528 * 519, dtype=cp.float32).reshape(528, 519)
    operation.validate(image)
    return operation


def acquire(args, initial, recording=None):
    from nvus.scanner_display import Display
    cadence, images = Cadence(), Images()
    enhancement = make_enhancement(args)
    display = Display(scale=1., enabled=args.commissioning and not args.no_display,
                      enhancement=enhancement, enhancement_enabled=args.mimicknet,
                      pretty=args.pretty)
    started = time.monotonic()
    resources = RunResources()
    try:
        with closing(display), cpp_policy_run(args, initial, resources) as resources:
            executor = resources.executor
            count, window_closed, operator_stopped = consume_frames(
                args, executor, images, display, cadence, initial.board, recording)
    finally:
        if recording is not None:
            recording.connection = dict(readback=resources.readback,
                                        restoration=resources.restoration)
    status = executor.status()
    validate_completion(args, status, count, window_closed, operator_stopped,
                        recording)
    return dict(simulated=not args.commissioning, frames=count,
        completed_logical_transmits=32 * status.completed_groups,
        completed_groups=status.completed_groups, received_events=status.received_events,
        completion=status.reason.name, window_closed=window_closed,
        operator_stopped=operator_stopped,
        elapsed_s=time.monotonic()-started, cadence=cadence.report(), display=display.report,
        connection=dict(readback=resources.readback, restoration=resources.restoration),
        observer=images.report,
        enhancement=enhancement.metadata if enhancement else None,
        control=f'C++ PolicyExecutor rollout_length={args.rollout_length}; '
                f'queue_capacity={args.queue_capacity}; dual_bank={args.tx_dual_bank}; '
                'no Python callbacks or transmit cache')


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--simulate', action='store_true')
    mode.add_argument('--commissioning', action='store_true')
    stopping = parser.add_mutually_exclusive_group(required=True)
    stopping.add_argument('--frame-limit', type=int)
    stopping.add_argument('--continuous', action='store_true',
                          help='Run until Ctrl+C or the display window is closed')
    parser.add_argument('--max-seconds', type=float,
                        help='Optional run limit; defaults to 120 for finite runs')
    parser.add_argument('--rollout-length', type=int, choices=(1, 32), default=32,
                        help='Plane waves produced per C++ policy decision')
    parser.add_argument('--queue-capacity', type=int, choices=range(1, 9), default=1,
                        help='Maximum queued acquisition groups (default: 1 for low latency)')
    parser.add_argument('--fpga-auto-sync-start', action='store_true',
                        help='Launch each event in FPGA after its TX update commits')
    parser.add_argument('--fpga-auto-sync-guard-us', type=float, default=0.,
                        help='FPGA guard after TX update completion (default: 0)')
    parser.add_argument('--fpga-auto-sync-pri-us', type=float, default=0.,
                        help='FPGA minimum event interval in auto-sync mode (default: 0 us)')
    parser.add_argument('--rx-arm-delay-s', type=float, default=0.,
                        help='Wait after receiver setup before the first TX update (default: 0 s)')
    parser.add_argument('--tx-dual-bank', action='store_true',
                        help='Keep one future TX update queued during capture')
    parser.add_argument('--fpga-tx-release-lead-us', type=float, default=35.,
                        help='Start the queued TX update this long before capture end (default: 35 us)')
    parser.add_argument('--rx-atten-code', type=int, choices=range(49),
                        help='TX fixed receive attenuation code (default: 23; nominal 0.25 dB/step)')
    parser.add_argument('--rx-source-ohms', type=int, choices=(100, 200, 400, 800),
                        help='TX receive attenuator source-impedance model (default: unchanged)')
    parser.add_argument('--tx-dtgc-enable', action='store_true',
                        help='Use a one-segment dynamic TX receive attenuation ramp')
    parser.add_argument('--tx-dtgc-start-db', type=float, default=12.,
                        help='Dynamic receive attenuation at ramp start (default: 12 dB)')
    parser.add_argument('--tx-dtgc-start-us', type=float, default=10.,
                        help='Dynamic ramp start after TR_BF_SYNC (default: 10 us)')
    parser.add_argument('--tx-dtgc-ramp-us', type=float, default=40.,
                        help='Requested duration of the attenuation ramp (default: 40 us)')
    parser.add_argument('--record-rf-dir', type=Path,
                        help='New directory for bounded completed-observation RF recording')
    parser.add_argument('--record-rf-frames', type=int,
                        help='Number of completed RF frames to save')
    parser.add_argument('--record-rf-skip', type=int,
                        help='Number of completed warm-up frames to skip before saving')
    parser.add_argument('--no-display', action='store_true', help='Live GPU reconstruction without a window')
    parser.add_argument('--pretty', action='store_true',
                        help='Use the full-screen clinical viewer')
    parser.add_argument('--process', action='store_true', help='Also reconstruct simulated RF; never display it')
    parser.add_argument('--mimicknet', action='store_true',
                        help='Apply local TensorRT MimickNet after reconstruction for display')
    parser.add_argument('--hololink', default='192.168.0.2')
    parser.add_argument('--ibv-name')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    if (not hasattr(cpp, 'PolicyExecutor')
            or not hasattr(cpp.PolicyLimits(), 'continuous')):
        parser.error('Rebuild the C++ extension and use NVUS_USE_INSTALLED=1 with that environment')
    if (args.commissioning
            and (not hasattr(cpp, 'HsbExecutionConfig')
                 or not hasattr(cpp.HsbExecutionConfig(), 'allow_sequence_wrap')
                 or not hasattr(cpp, 'HololinkTransportConfig')
                 or not hasattr(cpp.HololinkTransportConfig(), 'rx_arm_delay_s'))):
        parser.error('Rebuild the SDK-enabled C++ extension and use NVUS_USE_INSTALLED=1')
    if args.continuous:
        args.frame_limit = 0
        args.max_seconds = 0. if args.max_seconds is None else args.max_seconds
        if (not math.isfinite(args.max_seconds)
                or not 0 <= args.max_seconds <= 120):
            parser.error('Continuous max-seconds must be finite and in [0, 120]')
    else:
        args.max_seconds = 120. if args.max_seconds is None else args.max_seconds
        if (not 1 <= args.frame_limit <= 1024
                or not math.isfinite(args.max_seconds)
                or not 0 < args.max_seconds <= 120):
            parser.error('Require 1..1024 frames and 0 < max-seconds <= 120')
    if (not math.isfinite(args.fpga_auto_sync_guard_us)
            or not 0 <= args.fpga_auto_sync_guard_us <= 1_000_000):
        parser.error('FPGA auto-sync guard must be finite and in [0, 1000000] us')
    if (not math.isfinite(args.fpga_auto_sync_pri_us)
            or not 0 <= args.fpga_auto_sync_pri_us <= 1_000_000):
        parser.error('FPGA auto-sync PRI must be finite and in [0, 1000000] us')
    if (not math.isfinite(args.rx_arm_delay_s) or not 0 <= args.rx_arm_delay_s <= 30):
        parser.error('RX arm delay must be finite and in [0, 30] s')
    if (not math.isfinite(args.fpga_tx_release_lead_us)
            or not 0 <= args.fpga_tx_release_lead_us <= 1_000_000):
        parser.error('FPGA TX release lead must be finite and in [0, 1000000] us')
    if args.tx_dual_bank and not args.fpga_auto_sync_start:
        parser.error('--tx-dual-bank requires --fpga-auto-sync-start')
    if args.tx_dtgc_enable and args.rx_atten_code is not None:
        parser.error('--tx-dtgc-enable and --rx-atten-code are mutually exclusive')
    if args.process and not args.simulate:
        parser.error('--process requires --simulate')
    if args.mimicknet and not (args.commissioning or (args.simulate and args.process)):
        parser.error('--mimicknet requires --commissioning or --simulate --process')
    recording_values = (args.record_rf_dir, args.record_rf_frames, args.record_rf_skip)
    if any(value is not None for value in recording_values):
        if any(value is None for value in recording_values):
            parser.error('record-rf-dir, record-rf-frames, and record-rf-skip are required together')
        if args.record_rf_frames < 1 or args.record_rf_skip < 0:
            parser.error('record-rf-frames must be positive and record-rf-skip nonnegative')
        if (not args.continuous
                and args.record_rf_skip + args.record_rf_frames > args.frame_limit):
            parser.error('recorded frames plus skipped frames exceed frame-limit')
        if args.record_rf_dir.exists():
            parser.error('record-rf-dir must not already exist')
        if not (args.simulate or args.commissioning):
            parser.error('RF recording requires --simulate or --commissioning')
    if args.report and (args.report.exists() or not args.report.parent.is_dir()):
        parser.error('report must be a new file in an existing directory')
    if args.commissioning and not cpp.sdk_available:
        parser.error('SDK-enabled C++ build required')
    return args


def print_result(result):
    def rate(value):
        return f'{value:.1f}' if value is not None else 'n/a'
    cadence = result['cadence']
    shown = result['display']
    if result.get('enhancement'):
        state = 'ON' if shown.get('mimicknet_enabled') else 'OFF'
        print(f"MimickNet engine ready | final state {state} | "
              f"viewer toggles {shown.get('mimicknet_toggles', 0)}")
    print(f"Complete: {result['frames']} consumed frames / "
          f"{result['completed_logical_transmits']} logical transmits / "
          f"{result['received_events']} physical acquisitions; {result['completion']}; clean shutdown")
    print(f"Pipeline {rate(cadence['pipeline_fps'])} fps | "
          f"acquisition {rate(cadence['acquisition_fps'])} fps (host frame-start cadence)")
    if shown['enabled']:
        print(f"Fresh images {rate(shown.get('new_image_fps'))} fps | "
              f"display {rate(shown.get('redraw_hz'))} Hz | "
              f"screen {shown.get('screen_hz')} Hz")


def write_report(path, summary):
    if path is None:
        return
    with path.open('x') as output:
        json.dump(summary, output, indent=2, allow_nan=False)
        output.write('\n')


def main(argv=None):
    args = parse_args(argv)
    environment = verify_installed_python()
    angles = [-7.75 + .5*index for index in range(32)]
    board = BoardConfig(
        receive_attenuation_code=(BoardConfig().receive_attenuation_code
                                  if args.rx_atten_code is None else args.rx_atten_code),
        receive_source_ohms=args.rx_source_ohms,
        receive_dtgc_start_db=(args.tx_dtgc_start_db if args.tx_dtgc_enable else None),
        receive_dtgc_start_us=args.tx_dtgc_start_us,
        receive_dtgc_ramp_us=args.tx_dtgc_ramp_us)
    initial = prepared_view(cpp.compile_plane_waves(angles), board)
    summary = dict(policy=f'C++ PW4cm rollout_length={args.rollout_length}',
                   timing=inspect_timing(initial), live_validated=False)
    recording = None
    error = None
    try:
        if args.record_rf_dir is not None:
            recording = RfRecording(args, initial, environment)
        if args.simulate or args.commissioning:
            with ownership() if args.commissioning else nullcontext():
                result = summary['result'] = acquire(args, initial, recording)
            summary['live_validated'] = args.commissioning
            print_result(result)
        else:
            print(json.dumps(summary, indent=2, allow_nan=False))
            print('Offline inspection only. --simulate exercises the C++ loop without hardware.')
    except BaseException as exc:
        error = exc
        raise
    finally:
        if recording is not None:
            recording.finalize(args, initial, summary, error)
        write_report(args.report, summary)
    return summary


if __name__ == '__main__':
    main()
