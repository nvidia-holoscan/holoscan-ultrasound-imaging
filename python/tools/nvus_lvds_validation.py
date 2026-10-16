# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""NVUS LVDS bring-up and received-pattern validation, driver-backed.

Presets pick the capture scenario (DC/ramp pattern validation, eval-board
loopback, probe capture, plane-wave sweep). --dry-run drives every register
and SPI sequence against a Trace backend and prints the recorded events.
"""

import argparse
import ast
import logging
import math
import json
import os
import sys

import cupy as cp
import holoscan

import hololink as hololink_module

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import nvus  # noqa: E402
from nvus import host, raw, tx  # noqa: E402

META_KEYS = ("received_frame_number", "frame_number", "dropped", "rx_write_requests",
             "imm_data", "page_number", "bytes_written")
SYNC_CONFIG_CLK_HZ = 100e6


class PatternValidatorOp(holoscan.core.Operator):
    PARAMS = ("record_bytes", "afe_sample_bytes", "lanes", "log_every",
              "max_errors", "stream_name", "pattern_mode", "pattern_a", "pattern_b",
              "strict_start", "dump_capture_dir", "dump_capture_frames",
              "dump_prefix", "pw_sidecar")

    def __init__(self, fragment, **kwargs):
        params = {k: kwargs.pop(k) for k in self.PARAMS}
        super().__init__(fragment, **kwargs)
        vars(self).update({f"_{k}": v for k, v in params.items()})
        self._pattern_a &= raw.SAMPLE_MASK
        self._pattern_b &= raw.SAMPLE_MASK
        self._dump_prefix = self._dump_prefix or ""
        self._frames = self._samples = self._errors = 0
        self._phase_expected = [None, None]
        self._phase_steps = [1, 2]
        self._sample_offset = None
        self._last_received_frame_number = self._last_frame_number = self._last_dropped = None
        if self._dump_capture_dir is not None:
            os.makedirs(self._dump_capture_dir, exist_ok=True)
        if self._pw_sidecar is not None:
            self._pw_sidecar.write()

    def setup(self, spec):
        spec.input("input")

    def _metadata_value(self, metadata, key):
        value = metadata.get(key, None)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return value

    def _metadata_snapshot(self):
        metadata = self.metadata
        return {key: self._metadata_value(metadata, key) for key in META_KEYS}

    def _format_metadata(self, meta):
        parts = []
        for key in META_KEYS:
            value = meta.get(key)
            if value is None:
                continue
            if key == "imm_data" and isinstance(value, int):
                parts.append(f"{key}=0x{value:x}")
            else:
                parts.append(f"{key}={value}")
        return " ".join(parts) if parts else "metadata=unavailable"

    def _check_metadata(self, meta):
        dropped = meta.get("dropped")
        if isinstance(dropped, int):
            if self._last_dropped is not None and dropped > self._last_dropped:
                logging.warning(
                    "%s receiver dropped count changed: %d -> %d (%s)",
                    self._stream_name,
                    self._last_dropped,
                    dropped,
                    self._format_metadata(meta),
                )
            elif self._last_dropped is None and dropped:
                logging.warning(
                    "%s receiver dropped count already nonzero: %d (%s)",
                    self._stream_name,
                    dropped,
                    self._format_metadata(meta),
                )
            self._last_dropped = dropped

        for key, attr in (
            ("received_frame_number", "_last_received_frame_number"),
            ("frame_number", "_last_frame_number"),
        ):
            value = meta.get(key)
            last = getattr(self, attr)
            if isinstance(value, int) and isinstance(last, int) and value != last + 1:
                logging.warning(
                    "%s %s jump: %d -> %d (%s)",
                    self._stream_name,
                    key,
                    last,
                    value,
                    self._format_metadata(meta),
                )
            if isinstance(value, int):
                setattr(self, attr, value)

    def _dump_frame(self, data, meta):
        if self._dump_capture_dir is None:
            return
        if self._dump_capture_frames and self._frames >= self._dump_capture_frames:
            return

        frame_index = self._frames
        angle_entry = None
        if self._pw_sidecar is not None:
            frame_index = self._pw_sidecar.frame_index_for_metadata(meta, self._frames)
            angle_entry = self._pw_sidecar.angle_entry(frame_index)

        meta_out = {
            "frame_index": frame_index,
            "dump_index": self._frames,
            "bytes": len(data),
            "record_bytes": self._record_bytes,
            "afe_sample_bytes": self._afe_sample_bytes,
            "lanes": self._lanes,
            "pattern_mode": self._pattern_mode,
            "metadata": meta,
        }
        if angle_entry is not None:
            meta_out["plane_wave"] = angle_entry
        bin_path = raw.dump_frame(
            self._dump_capture_dir,
            frame_index,
            data,
            meta_out,
            prefix=f"{self._dump_prefix}frame",
        )

        if self._pw_sidecar is not None:
            self._pw_sidecar.add_dumped_frame(
                frame_index,
                os.path.basename(bin_path),
                meta,
                self._frames,
            )

    def _unpack_record(self, data, offset):
        return tuple(
            raw.unpack_afe_sample(
                data[offset + afe * self._afe_sample_bytes : offset + (afe + 1) * self._afe_sample_bytes]
            )
            for afe in range(raw.AFES_PER_RECORD)
        )

    def _equal_record_value(self, data, offset):
        samples = self._unpack_record(data, offset)
        values = []
        for sample in samples:
            if not all(value == sample[0] for value in sample):
                return None
            values.append(sample[0])
        if all(value == values[0] for value in values):
            return values[0]
        return None

    def _score_offset(self, data, offset, phase_spec):
        values = []
        usable = min(len(data), offset + self._record_bytes * 64)
        for record_offset in range(offset, usable - self._record_bytes + 1, self._record_bytes):
            values.append(self._equal_record_value(data, record_offset))

        if len(values) < 4:
            return -10_000

        score = 0
        equal_count = 0
        for value in values:
            if value is None:
                score -= 8
            else:
                equal_count += 1
                score += 2

        if self._pattern_mode == "dc":
            for i, value in enumerate(values):
                if value is None:
                    continue
                expected = phase_spec[i & 1]
                score += 8 if value == expected else -8
        else:
            for i in range(2, len(values)):
                if values[i] is None or values[i - 2] is None:
                    continue
                expected = (values[i - 2] + phase_spec[i & 1]) & raw.SAMPLE_MASK
                score += 6 if values[i] == expected else -4

        if equal_count < 4:
            score -= 1000
        return score

    def _lock_sample_offset(self, data):
        best = None
        if self._pattern_mode == "dc":
            phase_specs = (
                (self._pattern_a, self._pattern_b),
                (self._pattern_b, self._pattern_a),
            )
        else:
            phase_specs = ((1, 2), (2, 1))

        for offset in range(self._record_bytes):
            for phase_spec in phase_specs:
                score = self._score_offset(data, offset, phase_spec)
                if best is None or score > best[0]:
                    best = (score, offset, list(phase_spec))

        score, offset, phase_spec = best
        self._sample_offset = offset
        if self._pattern_mode == "dc":
            self._phase_expected = phase_spec
            self._phase_steps = [0, 0]
            logging.info(
                "%s locked software record offset=%d dc_phase_values=%s score=%d",
                self._stream_name,
                offset,
                [f"0x{value:03x}" for value in phase_spec],
                score,
            )
        else:
            self._phase_steps = phase_spec
            logging.info(
                "%s locked software record offset=%d phase_steps=%s score=%d",
                self._stream_name,
                offset,
                phase_spec,
                score,
            )

    def _advance_expected_for_skipped_sample(self):
        if self._pattern_mode != "dc":
            phase = self._samples & 1
            if self._phase_expected[phase] is not None:
                self._phase_expected[phase] = (
                    self._phase_expected[phase] + self._phase_steps[phase]
                ) & raw.SAMPLE_MASK
        self._samples += 1

    def _log_sample_error(self, message, *args):
        self._errors += 1
        if self._errors <= self._max_errors:
            logging.error(message, *args)

    def _check_record(self, samples):
        phase = self._samples & 1
        values = []
        record_ok = True
        for afe, sample in enumerate(samples):
            values_equal = all(value == sample[0] for value in sample)
            if not values_equal:
                self._log_sample_error(
                    "%s sample_pair %d %s lane mismatch: %s",
                    self._stream_name,
                    self._samples,
                    raw.AFE_NAMES[afe],
                    " ".join(f"{v:03x}" for v in sample),
                )
                record_ok = False
            else:
                values.append(sample[0])

        if not record_ok:
            return

        if not all(value == values[0] for value in values):
            self._log_sample_error(
                "%s sample_pair %d AFE mismatch: %s",
                self._stream_name,
                self._samples,
                " ".join(
                    f"{raw.AFE_NAMES[afe]}=0x{value:03x}" for afe, value in enumerate(values)
                ),
            )
            return

        value = values[0]
        if self._phase_expected[phase] is None:
            if self._strict_start and self._pattern_mode == "ramp":
                self._phase_expected[phase] = 1 if self._phase_steps[phase] == 1 else 2
            else:
                self._phase_expected[phase] = value

        expected = self._phase_expected[phase] & raw.SAMPLE_MASK
        if value != expected:
            self._log_sample_error(
                "%s sample_pair %d phase=%d value=%#x expected=%#x",
                self._stream_name,
                self._samples,
                phase,
                value,
                expected,
            )

        if self._pattern_mode != "dc":
            self._phase_expected[phase] = (expected + self._phase_steps[phase]) & raw.SAMPLE_MASK

    def compute(self, op_input, op_output, context):
        in_message = op_input.receive("input")
        meta = self._metadata_snapshot()
        self._check_metadata(meta)
        tensor = in_message.get("")
        data = cp.asarray(tensor, dtype=cp.uint8).get()
        self._dump_frame(data, meta)
        if self._pattern_mode == "loopback":
            self._samples += len(data) // self._record_bytes
            self._frames += 1
            if self._frames % self._log_every == 0:
                logging.info(
                    "%s frames=%d sample_pairs=%d loopback_capture %s",
                    self._stream_name,
                    self._frames,
                    self._samples,
                    self._format_metadata(meta),
                )
            return
        if self._sample_offset is None:
            self._lock_sample_offset(data)

        start = self._sample_offset
        usable = start + ((len(data) - start) // self._record_bytes) * self._record_bytes
        if start or usable != len(data):
            logging.debug(
                "%s frame %d length=%d record_offset=%d trailing=%d",
                self._stream_name,
                self._frames,
                len(data),
                start,
                len(data) - usable,
            )
        for offset in range(start, usable, self._record_bytes):
            self._check_record(self._unpack_record(data, offset))
            self._samples += 1
        if start:
            self._advance_expected_for_skipped_sample()
        self._frames += 1
        if self._frames % self._log_every == 0:
            logging.info(
                "%s frames=%d sample_pairs=%d errors=%d %s",
                self._stream_name,
                self._frames,
                self._samples,
                self._errors,
                self._format_metadata(meta),
            )
        if self._errors > self._max_errors:
            logging.error(
                "%s failing metadata: %s",
                self._stream_name,
                self._format_metadata(meta),
            )
            raise RuntimeError(
                f"{self._stream_name}: too many validation errors ({self._errors})"
            )


class PlaneWaveSidecar:
    def __init__(self, path, args, rx_select):
        self._path = path
        self._angles = args.pw_planned_angles
        self._data = {
            "format": "nvus_plane_wave_sweep_v2",
            "scene": args.pw_scene,
            "rx_select": rx_select,
            "bf_clock_hz": args.tx_delay_cal_bf_clock,
            "bf_clock_period_ns": 1e9 / args.tx_delay_cal_bf_clock,
            "sound_speed_mps": args.pw_sound_speed,
            "pitch_m": tx.TX_PW_PITCH_M,
            "element_count": tx.TX_PW_ELEMENTS,
            "pattern_profile": args.tx_pattern_profile,
            "angle_count": args.pw_angles,
            "angle_max_degrees": args.pw_angle_max,
            "delay_start_word": args.pw_delay_start_word,
            "delay_last_start_word": args.pw_profile_plan[-1]["delay_start_word"],
            "auto_delay_profile_end_word": args.pw_auto_increment_end_word,
            "element_map": tx.TX_PW_ELEMENT_MAP,
            "planned_angles": self._angles,
            "dumped_frames": [],
        }

    def angle_entry(self, frame_index):
        if not 0 <= frame_index < len(self._angles):
            return None
        return self._angles[frame_index]

    def frame_index_for_metadata(self, meta, dump_index):
        frame_number = meta.get("frame_number")
        if isinstance(frame_number, int) and 0 <= frame_number < len(self._angles):
            return frame_number
        return dump_index

    def add_dumped_frame(self, frame_index, filename, meta, dump_index):
        entry = {
            "frame_index": frame_index,
            "dump_index": dump_index,
            "filename": filename,
            "metadata": meta,
        }
        angle = self.angle_entry(frame_index)
        if angle is not None:
            entry.update(angle)
        self._data["dumped_frames"].append(entry)
        self.write()

    def write(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, sort_keys=True)


class NVUSLvdsValidationApp(holoscan.core.Application):
    def __init__(self, **kw):
        super().__init__()
        vars(self).update({f"_{k}": v for k, v in kw.items()})

    def compose(self):
        if self._frame_limit:
            condition = holoscan.conditions.CountCondition(
                self, name="frame_count", count=self._frame_limit
            )
        else:
            condition = holoscan.conditions.BooleanCondition(
                self, name="run", enable_tick=True
            )

        receiver = hololink_module.operators.RoceReceiverOp(
            self,
            condition,
            name="roce_receiver",
            frame_size=self._frame_size,
            frame_context=self._cuda_context,
            ibv_name=self._ibv_name,
            ibv_port=self._ibv_port,
            hololink_channel=self._hololink_channel,
            device=self._rx_device,
            trim=True,
            pages=self._rx_pages,
            queue_size=self._rx_queue_size,
        )
        validator = PatternValidatorOp(
            self,
            name="ramp_validator",
            record_bytes=raw.RECORD_BYTES,
            afe_sample_bytes=raw.AFE_SAMPLE_BYTES,
            lanes=raw.LANES_PER_AFE,
            log_every=self._log_every,
            max_errors=self._max_errors,
            stream_name=self._stream_name,
            pattern_mode=self._pattern_mode,
            pattern_a=self._pattern_a,
            pattern_b=self._pattern_b,
            strict_start=self._strict_start,
            dump_capture_dir=self._dump_capture_dir,
            dump_capture_frames=self._dump_capture_frames,
            dump_prefix=self._dump_prefix,
            pw_sidecar=self._pw_sidecar,
        )
        self.add_flow(receiver, validator, {("output", "input")})


def parse_delay_start_word(text):
    value = int(text, 0)
    if not 2 <= value <= 510 or value % 2 != 0:
        raise argparse.ArgumentTypeError(
            f"{text} is not a valid TX delay profile start word; expected even value 2..510"
        )
    return value


def ranged_int(what, low, high):
    def parse(text):
        value = int(text, 0)
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(
                f"{text} is not a valid {what}; expected {low}..{high:#x}"
            )
        return value
    return parse


def safe_filename_token(value):
    token = []
    for char in value:
        if char.isalnum() or char in ("-", "_", "."):
            token.append(char)
        else:
            token.append("_")
    result = "".join(token).strip("._-")
    return result or "capture"


# --- plane-wave sweep ---


def build_plane_wave_plan(args):
    start_word = args.pw_delay_start_word
    last_start_word = start_word + (args.pw_angles - 1) * tx.TX_DELAY_PROFILE_WORDS
    if last_start_word + tx.TX_DELAY_PROFILE_WORDS - 1 > 511:
        raise ValueError(
            "plane-wave delay profiles exceed TX memory: "
            f"start={start_word} angles={args.pw_angles} last_start={last_start_word}"
        )
    if start_word <= max(tx.TX_SAMPLE_PATTERN_START_WORDS.values()):
        raise ValueError(
            f"pw-delay-start-word={start_word} collides with loaded TX sample patterns; "
            f"use >= {max(tx.TX_SAMPLE_PATTERN_START_WORDS.values()) + 1}"
        )

    plan = []
    for angle_index, theta_degrees in enumerate(
        tx.plane_wave_angles(args.pw_angles, args.pw_angle_max)
    ):
        half_cycles = tx.plane_wave_delays(
            math.radians(theta_degrees),
            c=args.pw_sound_speed,
            bf_clock_hz=args.tx_delay_cal_bf_clock,
        )
        cycles = [value // 2 for value in half_cycles]
        plan.append(
            {
                "angle_index": angle_index,
                "theta_degrees": theta_degrees,
                "delay_start_word": start_word + angle_index * tx.TX_DELAY_PROFILE_WORDS,
                "half_cycles": half_cycles,
                "delay_min_cycles": min(cycles),
                "delay_max_cycles": max(cycles),
                "delay_span_cycles": max(cycles) - min(cycles),
            }
        )

    args.pw_profile_plan = plan
    args.pw_auto_increment_end_word = last_start_word + 1
    keys = ("angle_index", "theta_degrees", "delay_start_word",
            "delay_min_cycles", "delay_max_cycles", "delay_span_cycles")
    args.pw_planned_angles = [{key: item[key] for key in keys} for item in plan]


def load_plane_wave_profiles(evm, args):
    plan = args.pw_profile_plan
    logging.info(
        "Programming %d plane-wave TX delay profiles: angles=%.3f..%.3f deg "
        "start_word=%d last_start_word=%d auto_end=%d BF_CLK=%.0f Hz",
        len(plan),
        plan[0]["theta_degrees"],
        plan[-1]["theta_degrees"],
        args.pw_delay_start_word,
        plan[-1]["delay_start_word"],
        args.pw_auto_increment_end_word,
        args.tx_delay_cal_bf_clock,
    )
    evm.tx.load_delay_profiles(
        [item["half_cycles"] for item in plan], args.pw_delay_start_word
    )
    evm.tx.set_auto_increment(True, args.pw_auto_increment_end_word)
    logging.info(
        "Plane-wave TX delay profiles loaded; %s; pattern=%s",
        tx.TX_PW_ELEMENT_MAP,
        args.tx_pattern_profile,
    )


def arm_plane_wave_sequence(evm, args):
    logging.info(
        "Arming plane-wave TX profile sequence at start_word=%d auto_end=%d",
        args.pw_delay_start_word,
        args.pw_auto_increment_end_word,
    )
    evm.tx.select_profile(args.pw_delay_start_word)
    if args.repeat_shots > 1:
        # Known issue under auto-increment: each profile's second memory word
        # (elements 64-127) fires one profile late; multi-angle sweeps carry that lag.
        evm.tx.set_auto_increment(True, args.pw_auto_increment_end_word)
    else:
        # Static select is lag-free: the TX skew baseline covered all 128
        # elements at 4.3 ns std with auto-increment off.
        evm.tx.set_auto_increment(False)


# --- presets and CLI ---

# Each preset is a plain settings dict applied over the parser defaults;
# explicit flags that disagree with the preset raise instead of being mutated.
PRESETS = {
    "validation-dc": dict(eval_board_mode="none", capture_pattern="dc"),
    "validation-ramp": dict(eval_board_mode="none", capture_pattern="ramp"),
    "loopback": dict(eval_board_mode="loopback", capture_pattern="loopback"),
    "probe-capture": dict(eval_board_mode="probe", capture_pattern="loopback"),
    "pw-sweep": dict(
        eval_board_mode="probe",
        capture_pattern="loopback",
        tx_pattern_profile="3Level_5MHz",
        frame_limit=1,
    ),
}


def apply_preset(args, parser):
    for key, value in PRESETS[args.preset].items():
        current = getattr(args, key, None)
        if current != parser.get_default(key) and current != value:
            raise ValueError(
                f"--{key.replace('_', '-')}={current} conflicts with "
                f"preset {args.preset} (needs {value})"
            )
        setattr(args, key, value)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Bring up NVUS LVDS and validate received AFE pattern data.",
    )
    parser.add_argument(
        "--preset",
        choices=tuple(PRESETS),
        default="validation-dc",
        help="Capture scenario: validation-dc/validation-ramp validate AFE test "
        "patterns; loopback and probe-capture configure the eval-board TX path; "
        "pw-sweep programs and captures a steered plane-wave TX sweep.",
    )
    parser.add_argument("--hololink", default="192.168.0.2")
    parser.add_argument("--ibv-name", default=host.default_ibv_name())
    parser.add_argument("--ibv-port", type=int, default=1)
    parser.add_argument("--frame-size", type=int, default=raw.RECORD_BYTES * 4096)
    parser.add_argument("--frame-limit", type=int, default=64)
    parser.add_argument("--rx-pages", type=int, default=64)
    parser.add_argument("--rx-queue-size", type=int, default=64)
    parser.add_argument("--log-level", type=int, default=logging.INFO)
    parser.add_argument("--log-every", type=int, default=8)
    parser.add_argument(
        "--tx-pattern-profile",
        choices=tuple(tx.TX_SAMPLE_PATTERN_START_WORDS),
        default="3Level_10MHz",
        help="Sample TX profile selected via MEM_START_WORD after loading pattern memory.",
    )
    parser.add_argument(
        "--tx-delay-cal-json",
        default=None,
        help="Calibration JSON from nvus_probe_calibrate.py to load into TX delay memory.",
    )
    parser.add_argument(
        "--tx-delay-cal-csv",
        default=None,
        help="Calibration CSV from nvus_probe_calibrate.py to load into TX delay memory.",
    )
    parser.add_argument(
        "--tx-delay-cal-column",
        default="tx_bf_half_cycles_rounded",
        help="Calibration column to load. Default is the calibrator's rounded TX half-cycle field.",
    )
    parser.add_argument(
        "--tx-delay-cal-units",
        choices=("half-cycles", "ns"),
        default="half-cycles",
        help="Units for --tx-delay-cal-column; half-cycles are 2.5 ns at the default 200 MHz BF clock.",
    )
    parser.add_argument(
        "--tx-delay-cal-bf-clock",
        type=float,
        default=tx.BF_CLOCK_HZ,
        help="BF clock used for plane-wave TX delay generation.",
    )
    parser.add_argument("--pw-angles", type=int, default=75)
    parser.add_argument("--pw-angle-max", type=float, default=15.0)
    parser.add_argument(
        "--pw-delay-start-word",
        type=parse_delay_start_word,
        default=tx.TX_PW_DELAY_START_WORD_DEFAULT,
        help="First even TX delay profile memory word for plane-wave steering profiles.",
    )
    parser.add_argument(
        "--pw-sound-speed",
        type=float,
        default=tx.TX_PW_SOUND_SPEED_MPS,
        help="Medium sound speed for plane-wave TX delay generation, in m/s.",
    )
    parser.add_argument(
        "--pw-scene",
        default="capture",
        help="Scene name used as the dumped frame filename prefix and angle sidecar name.",
    )
    parser.add_argument(
        "--tx-spi-lanes",
        type=int,
        choices=(1, 2),
        default=1,
        help="TX SPI data lanes for the pattern-memory load. Default 1 "
        "preserves the legacy SDATA_0-only flow.",
    )
    parser.add_argument(
        "--tx-spi-prescaler",
        type=ranged_int("TX SPI prescaler", 0, 0x0F),
        default=nvus.bus.TX_SPI_DEFAULT_PRESCALER,
        help="HSB TX SPI prescaler. SCLK is 100 MHz / (2 * (prescaler + 1)); "
        "default 15 gives 3.125 MHz, 0 gives 50 MHz.",
    )
    parser.add_argument(
        "--tx-spi-path",
        choices=nvus.bus.TX_SPI_PATHS,
        default=nvus.bus.TX_SPI_PATH_HSB,
        help="TX register transport. hsb uses the original HSB SPI block; tx-hs "
        "uses the new NVUS TX HS SPI APB slow path. Default hsb.",
    )
    parser.add_argument(
        "--tx-spi-page-mask0",
        type=ranged_int("TX SPI page mask", 0, 0xFFFF),
        default=tx.TX_DUAL_SPI_SDATA0_PAGE_MASK,
        help="TX memory page-enable mask for SDATA_0 during --tx-spi-lanes 2 "
        "pattern loads. Default 0x5555.",
    )
    parser.add_argument(
        "--tx-spi-page-mask1",
        type=ranged_int("TX SPI page mask", 0, 0xFFFF),
        default=tx.TX_DUAL_SPI_SDATA1_PAGE_MASK,
        help="TX memory page-enable mask for SDATA_1 during --tx-spi-lanes 2 "
        "pattern loads. Default 0xaaaa.",
    )
    parser.add_argument(
        "--tx-spi-sdata-term",
        choices=tuple(tx.TX_SPI_SDATA_TERM_VALUES),
        default=None,
        help="TX73L64 SPI SDATA input termination override in ohms. Default is "
        "200 for --tx-spi-lanes 2 and leaves the reset value for single-lane.",
    )
    parser.add_argument(
        "--tx-loopback-atten-code",
        type=int,
        choices=tx.TX_LOOPBACK_ATTEN_CHOICES,
        default=47,
        help="TX73L64 fixed attenuation code used by eval-board loopback mode.",
    )
    parser.add_argument(
        "--tx-loopback-waveform",
        choices=("cw", "pattern"),
        default="cw",
        help="TX waveform source for the loopback preset. cw preserves the TI "
        "loopback setup; pattern uses the selected TX sample pattern profile.",
    )
    parser.add_argument(
        "--tx-probe-rx-select",
        choices=tx.TX_PROBE_RX_SELECT_CHOICES,
        default="odd",
        help="In probe mode, select which side of each TX73L64 odd/even T/R pair "
        "is connected to RX. both runs odd then even as separate capture runs.",
    )
    parser.add_argument("--rx-arm-delay-s", type=float, default=1.0)
    parser.add_argument(
        "--capture-records",
        type=int,
        default=None,
        help="Total 48-byte AFE-pair records to release through capture_ctrl. "
        "Defaults to frame-limit * frame-size / 48.",
    )
    parser.add_argument("--capture-delay-records", type=int, default=0)
    parser.add_argument("--sync-wakeup-cycles", type=int, default=20000)
    parser.add_argument("--sync-tx-delay-cycles", type=int, default=20010)
    parser.add_argument("--sync-tx-pulse-cycles", type=int, default=40)
    parser.add_argument(
        "--sync-capture-delay-cycles",
        type=int,
        default=None,
        help="sync_ctrl capture_start delay. Defaults to tx delay + tx pulse.",
    )
    parser.add_argument(
        "--sync-sequence-interval-cycles",
        type=int,
        default=None,
        help="sync_ctrl delay between repeated sequence starts. Defaults to "
        "max(capture_delay + capture_records + margin, 1,000,000 cycles == 10 ms) "
        "when repeat-shots > 1 - below ~1 ms the FPGA delivers only frame 0"
        "; otherwise 0.",
    )
    parser.add_argument(
        "--sync-sequence-interval-margin-cycles",
        type=int,
        default=10000,
        help="Extra cycles added to the computed repeated sequence interval.",
    )
    parser.add_argument("--sync-capture-wait-s", type=float, default=0.001)
    parser.add_argument("--strict-start", action="store_true")
    parser.add_argument("--max-errors", type=int, default=10)
    parser.add_argument(
        "--dump-capture-dir",
        default=None,
        help="Optional directory for raw received frame_NNNNNN.bin dumps plus JSON metadata.",
    )
    parser.add_argument(
        "--dump-capture-frames",
        type=int,
        default=4,
        help="Number of received frames to dump when --dump-capture-dir is set. "
        "Use 0 for all frames.",
    )
    parser.add_argument(
        "--repeat-shots",
        type=int,
        default=1,
        help="Use the FPGA sync sequencer to run multiple capture shots in one "
        "receiver run. Supported for loopback/probe pattern captures.",
    )
    parser.add_argument("--skip-bringup", action="store_true")
    parser.add_argument(
        "--skip-hololink-reset",
        action="store_true",
        help="Skip the FPGA soft reset; with --skip-bringup this preserves a "
        "warm board state for debug re-runs.",
    )
    parser.add_argument(
        "--evm",
        action="append",
        metavar="KEY=VALUE",
        help="Override any EvmConfig field, e.g. --evm rx_arm_delay_s=0.05 "
        "--evm spi_delay_s=0.002. Repeatable.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the full bring-up and capture-arm sequences against a Trace "
        "backend (no hardware) and print the recorded register/SPI events.",
    )
    return parser


def validate_pw_sweep(args):
    if args.dump_capture_dir is None:
        raise ValueError("pw-sweep requires --dump-capture-dir for angle sidecars")
    if args.tx_delay_cal_json is not None or args.tx_delay_cal_csv is not None:
        raise ValueError("pw-sweep owns TX delay profiles; do not also pass TX delay calibration")
    if args.pw_angles < 1:
        raise ValueError("pw-angles must be >= 1")
    if args.pw_sound_speed <= 0:
        raise ValueError("pw-sound-speed must be positive")
    if args.repeat_shots not in (1, args.pw_angles):
        raise ValueError(
            f"pw-sweep needs one shot per angle; --repeat-shots={args.repeat_shots} "
            f"conflicts with --pw-angles={args.pw_angles}"
        )
    if args.repeat_shots != args.pw_angles:
        logging.info(
            "Plane-wave sweep needs one shot per angle; overriding repeat-shots from %d to %d",
            args.repeat_shots,
            args.pw_angles,
        )
        args.repeat_shots = args.pw_angles
    if args.dump_capture_frames and args.dump_capture_frames < args.pw_angles:
        logging.info(
            "Plane-wave sweep sidecar requires all frames; overriding dump-capture-frames "
            "from %d to %d",
            args.dump_capture_frames,
            args.pw_angles,
        )
        args.dump_capture_frames = args.pw_angles
    args.pw_scene = safe_filename_token(args.pw_scene)
    build_plane_wave_plan(args)


def validate_args(args):
    if args.eval_board_mode in ("loopback", "probe") and args.skip_bringup:
        # lab probing relies on firing with hand-written TX state; an
        # unprogrammed board just times out on capture
        logging.warning("--skip-bringup: %s preset fires with TX/RX devices as-is",
                        args.preset)
    if args.tx_delay_cal_json is not None and args.tx_delay_cal_csv is not None:
        raise ValueError("use only one of --tx-delay-cal-json or --tx-delay-cal-csv")
    if args.tx_delay_cal_bf_clock <= 0:
        raise ValueError("tx-delay-cal-bf-clock must be positive")
    if args.tx_spi_path == nvus.bus.TX_SPI_PATH_TX_HS and args.tx_spi_lanes != 1:
        raise ValueError("--tx-spi-path tx-hs currently supports only --tx-spi-lanes 1")

    if args.preset == "pw-sweep":
        validate_pw_sweep(args)

    if args.repeat_shots < 1:
        raise ValueError("repeat-shots must be >= 1")
    if args.repeat_shots > 1 and not (
        (args.eval_board_mode == "loopback" and args.tx_loopback_waveform == "pattern")
        or args.eval_board_mode == "probe"
    ):
        raise ValueError(
            "repeat-shots > 1 is currently supported only for loopback/probe pattern captures"
        )
    if args.sync_sequence_interval_cycles is not None and args.sync_sequence_interval_cycles < 0:
        raise ValueError("sync-sequence-interval-cycles must be >= 0")
    if args.sync_sequence_interval_margin_cycles < 0:
        raise ValueError("sync-sequence-interval-margin-cycles must be >= 0")

    if args.frame_size % raw.FRAME_ALIGNMENT_BYTES:
        raise ValueError(
            f"frame-size must be a multiple of {raw.FRAME_ALIGNMENT_BYTES} "
            f"bytes (48-byte AFE pair and 64-byte SIF beat alignment)"
        )
    if args.rx_queue_size > args.rx_pages:
        raise ValueError("rx-queue-size must be <= rx-pages")
    if args.capture_delay_records < 0:
        raise ValueError("capture-delay-records must be >= 0")
    records_per_frame = args.frame_size // raw.RECORD_BYTES
    if args.capture_records is None:
        if args.frame_limit <= 0:
            raise ValueError("frame-limit must be > 0 unless capture-records is set")
        args.capture_records = records_per_frame * args.frame_limit
    if args.capture_records <= 0:
        raise ValueError("capture-records must be > 0")
    if args.dump_capture_frames < 0:
        raise ValueError("dump-capture-frames must be >= 0")
    logging.info(
        "capture_ctrl plan: records_per_frame=%d records_per_shot=%d bytes_per_shot=%d",
        records_per_frame,
        args.capture_records,
        args.capture_records * raw.RECORD_BYTES,
    )


# --- driver wiring ---


def sync_cycles_to_s(cycles):
    return None if cycles is None else cycles / SYNC_CONFIG_CLK_HZ


def parse_evm_overrides(args):
    overrides = {}
    for item in args.evm or ():
        key, sep, text = item.partition("=")
        if not sep or not key:
            raise ValueError(f"--evm expects KEY=VALUE, got {item!r}")
        try:
            overrides[key] = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            overrides[key] = text
    return overrides


def make_config(args):
    # --evm overrides merge last so they can override any explicit field;
    # EvmConfig rejects unknown keys.
    return nvus.EvmConfig(**{
        **dict(
            sync_capture_wait_s=args.sync_capture_wait_s,
            eval_board_mode=args.eval_board_mode,
            capture_pattern=args.capture_pattern,
            wakeup_s=sync_cycles_to_s(args.sync_wakeup_cycles),
            tx_delay_s=sync_cycles_to_s(args.sync_tx_delay_cycles),
            tx_pulse_s=sync_cycles_to_s(args.sync_tx_pulse_cycles),
            capture_delay_s=sync_cycles_to_s(args.sync_capture_delay_cycles),
            interval_s=sync_cycles_to_s(args.sync_sequence_interval_cycles),
            interval_margin_s=sync_cycles_to_s(args.sync_sequence_interval_margin_cycles),
            capture_records=args.capture_records,
            delay_records=args.capture_delay_records,
            rx_arm_delay_s=args.rx_arm_delay_s,
            loopback_waveform=args.tx_loopback_waveform,
            loopback_atten=args.tx_loopback_atten_code,
            tx_spi_lanes=args.tx_spi_lanes,
            tx_spi_prescaler=args.tx_spi_prescaler,
            tx_spi_path=args.tx_spi_path,
            tx_spi_page_mask0=args.tx_spi_page_mask0,
            tx_spi_page_mask1=args.tx_spi_page_mask1,
            tx_spi_sdata_term=args.tx_spi_sdata_term,
        ),
        **parse_evm_overrides(args),
    })


def calibration_delays_from_args(args):
    path = args.tx_delay_cal_json or args.tx_delay_cal_csv
    if path is None:
        return None
    return tx.calibration_delays(
        path,
        column=args.tx_delay_cal_column,
        units=args.tx_delay_cal_units,
        bf_clock_hz=args.tx_delay_cal_bf_clock,
    )


def first_probe_select(args):
    if args.eval_board_mode != "probe":
        return None
    return "odd" if args.tx_probe_rx_select == "both" else args.tx_probe_rx_select


def pick_device_mode(args):
    # "arm" releases capture_ctrl only; "release" also clears the assembler
    # and fires RX_BF_SYNC to complete a receiver-gated bring-up.
    if args.eval_board_mode in ("loopback", "probe") or args.skip_bringup:
        return "arm"
    return "release"


def init_evm(evm, args, delays):
    evm.init(
        pulse=args.tx_pattern_profile,
        delays=delays,
        samples=args.frame_size // (2 * raw.RECORD_BYTES),
        rx_select=first_probe_select(args) or "odd",
        repeat_shots=args.repeat_shots,
    )


def bring_up(evm, args):
    if args.skip_bringup:
        return
    delays = calibration_delays_from_args(args)
    init_evm(evm, args, delays)
    if args.preset == "pw-sweep":
        load_plane_wave_profiles(evm, args)


# --- capture runs ---


def probe_dump_dir(args, rx_select):
    if args.preset == "pw-sweep":
        return args.dump_capture_dir
    if args.tx_probe_rx_select == "both" and args.dump_capture_dir is not None:
        return os.path.join(args.dump_capture_dir, rx_select)
    return args.dump_capture_dir


def probe_dump_prefix(args, rx_select):
    if args.preset == "pw-sweep":
        return f"{args.pw_scene}_{rx_select}_"
    return None


def make_sidecar(args, rx_select, dump_dir):
    if args.preset != "pw-sweep":
        return None
    path = os.path.join(dump_dir, f"{args.pw_scene}_{rx_select}_angles.json")
    return PlaneWaveSidecar(path, args, rx_select)


def capture_runs_for(args):
    if args.eval_board_mode != "probe":
        return ((None, args.dump_capture_dir, None),)
    if args.tx_probe_rx_select == "both":
        logging.info(
            "Probe both mode: running odd then even as separate capture runs; base dump_dir=%s",
            args.dump_capture_dir,
        )
        selects = ("odd", "even")
    else:
        selects = (args.tx_probe_rx_select,)
    return tuple(
        (select, probe_dump_dir(args, select), probe_dump_prefix(args, select))
        for select in selects
    )


def for_each_capture_run(args, evm, run_one):
    current_select = first_probe_select(args)
    for rx_select, dump_dir, dump_prefix in capture_runs_for(args):
        if args.eval_board_mode == "probe" and rx_select != current_select:
            evm.tx.configure_probe(rx_select, 1, False)
            current_select = rx_select
        if args.preset == "pw-sweep":
            arm_plane_wave_sequence(evm, args)
        run_one(rx_select, dump_dir, dump_prefix)


def run_capture_runs(args, config, evm, hololink_channel, cuda_context):
    device_mode = pick_device_mode(args)
    effective_frame_limit = args.frame_limit * args.repeat_shots
    if args.repeat_shots > 1:
        logging.info(
            "FPGA sync repeat mode: %d shots, %d frame(s)/shot, %d total frame(s)",
            args.repeat_shots,
            args.frame_limit,
            effective_frame_limit,
        )

    def run_one(rx_select, dump_dir, dump_prefix):
        logging.info(
            "Starting capture run: rx_select=%s frame_limit=%d dump_dir=%s",
            rx_select if rx_select is not None else "n/a",
            effective_frame_limit,
            dump_dir,
        )
        app = NVUSLvdsValidationApp(
            cuda_context=cuda_context,
            hololink_channel=hololink_channel,
            frame_size=args.frame_size,
            frame_limit=effective_frame_limit,
            ibv_name=args.ibv_name,
            ibv_port=args.ibv_port,
            stream_name="afe_pair",
            log_every=args.log_every,
            max_errors=args.max_errors,
            pattern_mode=args.capture_pattern,
            pattern_a=config.pattern_a,
            pattern_b=config.pattern_b,
            strict_start=args.strict_start,
            rx_pages=args.rx_pages,
            rx_queue_size=args.rx_queue_size,
            rx_device=evm.rx_device(device_mode),
            dump_capture_dir=dump_dir,
            dump_capture_frames=args.dump_capture_frames,
            dump_prefix=dump_prefix,
            pw_sidecar=make_sidecar(args, rx_select, dump_dir),
        )
        app.run()

    for_each_capture_run(args, evm, run_one)


# --- entry points ---


def run_dry_run(args):
    logging.info("Dry run: driving all sequences against a Trace backend")
    trace = nvus.Trace(reads=nvus.fpga.ready_reads())
    evm = nvus.Evm(trace, make_config(args))
    bring_up(evm, args)
    device_mode = pick_device_mode(args)

    def run_one(rx_select, dump_dir, dump_prefix):
        logging.info(
            "Dry-run capture arm: rx_select=%s dump_dir=%s",
            rx_select if rx_select is not None else "n/a",
            dump_dir,
        )
        evm.rx_device(device_mode, arm_delay_s=0.0)._run()

    for_each_capture_run(args, evm, run_one)
    print(f"dry run: {len(trace.events)} trace events")
    for event in trace.events:
        print(nvus.bus.decode_event(event))


def run_hardware(args):
    nvus.enumeration_strategy()
    cu_context, cu_device = host.retain_cuda_context()

    channel_metadata = hololink_module.Enumerator.find_channel(channel_ip=args.hololink)
    logging.info("channel_metadata=%s", channel_metadata)
    hololink_module.DataChannel.use_sensor(channel_metadata, 0)
    logging.info("channel_metadata after use_sensor(0)=%s", channel_metadata)
    hololink_channel = hololink_module.DataChannel(channel_metadata)
    hololink = hololink_channel.hololink()

    hololink.start()
    try:
        if not args.skip_hololink_reset:
            hololink.reset()
        config = make_config(args)
        evm = nvus.Evm(hololink, config)
        bring_up(evm, args)
        run_capture_runs(args, config, evm, hololink_channel, cu_context)
    finally:
        hololink.stop()
        host.release_cuda_context(cu_device)


def main():
    parser = build_parser()
    args = parser.parse_args()
    apply_preset(args, parser)
    hololink_module.logging_level(args.log_level)
    logging.info("Initializing NVUS LVDS validation")
    validate_args(args)
    if args.dry_run:
        run_dry_run(args)
    else:
        run_hardware(args)


if __name__ == "__main__":
    main()
