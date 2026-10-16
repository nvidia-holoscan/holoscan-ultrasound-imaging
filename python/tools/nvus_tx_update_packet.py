# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Send one NVUS TX high-speed SPI update package through Sensor TX RoCE.

This is a bring-up/debug app for the FPGA tx_hs_spi_package_rx path. It builds
a single-fragment update package in host memory and sends it through the direct
RoceTransmitter class by default. The older RoceTransmitterOp route is retained
as a fallback/comparison path.
"""

import argparse
import logging
import os
import struct
import sys
import time


NVUS_PYTHON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
# Append instead of prepend so non-dry-run imports still find the installed hololink
# extension package before the source-tree hololink package.
if NVUS_PYTHON_DIR not in sys.path:
    sys.path.append(NVUS_PYTHON_DIR)
from nvus import tx as nvus_tx  # noqa: E402
from nvus.bus import (  # noqa: E402
    Bus,
    TX_READ_ENABLE_REG,
    TX_READ_ENABLE_WRITE,
    TX_SPI_DEFAULT_PRESCALER,
    TX_SPI_PATH_TX_HS,
)


TX_HS_SPI_MAGIC = 0x44585455
TX_HS_SPI_PROTOCOL_VERSION = 1
TX_HS_SPI_FRAGMENT_HDR_BYTES = 32
TX_HS_SPI_OP_BYTES = 32
TX_HS_SPI_DEFAULT_MAX_OPS_PER_BANK = 128
TX_HS_SPI_MAX_PACKET_BYTES = 4096
TX_HS_SPI_NORMAL_WRITE_BITS = 44
TX_HS_SPI_SENSOR_TX_BASE = 0x01200000
PAYLOAD_MODE_SIMPLE = "simple"
PAYLOAD_MODE_DELAY_PROFILE = "delay-profile"
TX_SEND_PATH_DIRECT = "direct"
TX_SEND_PATH_OPERATOR = "operator"
TRSW_SELECT_NONE = "none"
TX_HS_SPI_BASE = 0x70000000
TX_HS_SPI_CFG = TX_HS_SPI_BASE + 0x04
TX_HS_SPI_DEFAULT_HALF_PERIOD_CYCLES = 0
SENSOR_TX_CTRL_ENABLE = 0x00000003
SENSOR_TX_CTRL_LATCH_THRESHOLDS = 0x00000004
SENSOR_TX_CTRL_IDLE = 0x00000000

HEADER_STRUCT = struct.Struct("<IBBHHHHHHHHHII")


def parse_int(text):
    return int(text, 0)


def parse_u10(text):
    value = parse_int(text)
    if not 0 <= value <= 0x3FF:
        raise argparse.ArgumentTypeError(f"{text} is outside TX register range 0..0x3ff")
    return value


def parse_u32(text):
    value = parse_int(text)
    if not 0 <= value <= 0xFFFF_FFFF:
        raise argparse.ArgumentTypeError(f"{text} is outside 32-bit unsigned range")
    return value


def parse_u8(text):
    value = parse_int(text)
    if not 0 <= value <= 0xFF:
        raise argparse.ArgumentTypeError(f"{text} is outside 8-bit unsigned range")
    return value


def pack_u48_le(value):
    if not 0 <= value < (1 << 48):
        raise ValueError(f"0x{value:x} is outside 48-bit unsigned range")
    return int(value).to_bytes(6, byteorder="little")


def normal_write_word(address, data):
    address &= 0x3FF
    data &= 0xFFFF_FFFF
    return (address << 32) | data


def pack_operation(tx1_address, tx1_data, tx2_address, tx2_data, clock_count, flags):
    record = bytearray()
    record += pack_u48_le(normal_write_word(tx1_address, tx1_data))
    record += pack_u48_le(0)
    record += pack_u48_le(normal_write_word(tx2_address, tx2_data))
    record += pack_u48_le(0)
    record += struct.pack("<BBHI", clock_count, flags, 0, 0)
    if len(record) != TX_HS_SPI_OP_BYTES:
        raise AssertionError(f"operation record is {len(record)} bytes")
    return bytes(record)


def build_header(sequence, op_count, futz_header):
    magic = TX_HS_SPI_MAGIC
    version = TX_HS_SPI_PROTOCOL_VERSION
    header_bytes = TX_HS_SPI_FRAGMENT_HDR_BYTES
    flags = 0
    fragment_index = 0
    fragment_count = 1
    record_bytes = TX_HS_SPI_OP_BYTES
    fragment_operation_count = op_count
    total_operation_count = op_count
    fragment_payload_bytes = op_count * TX_HS_SPI_OP_BYTES
    reserved0 = 0
    total_payload_bytes = fragment_payload_bytes
    reserved1 = 0

    if futz_header == "magic":
        magic ^= 1
    elif futz_header == "version":
        version ^= 1
    elif futz_header == "header-bytes":
        header_bytes = TX_HS_SPI_FRAGMENT_HDR_BYTES - 1
    elif futz_header == "record-bytes":
        record_bytes = TX_HS_SPI_OP_BYTES - 1
    elif futz_header == "op-count-zero":
        fragment_operation_count = 0
        total_operation_count = 0
    elif futz_header == "op-count-too-big":
        fragment_operation_count = TX_HS_SPI_DEFAULT_MAX_OPS_PER_BANK + 1
        total_operation_count = TX_HS_SPI_DEFAULT_MAX_OPS_PER_BANK + 1

    return HEADER_STRUCT.pack(
        magic,
        version,
        header_bytes,
        flags,
        sequence & 0xFFFF,
        fragment_index,
        fragment_count,
        record_bytes,
        fragment_operation_count,
        total_operation_count,
        fragment_payload_bytes,
        reserved0,
        total_payload_bytes,
        reserved1,
    )


def simple_operations(args):
    operations = []
    for op_index in range(args.ops):
        tx1_addr = (args.tx1_addr + op_index * args.addr_stride) & 0x3FF
        tx2_addr = (args.tx2_addr + op_index * args.addr_stride) & 0x3FF
        tx1_data = (args.tx1_data + op_index * args.data_stride) & 0xFFFF_FFFF
        tx2_data = (args.tx2_data + op_index * args.data_stride) & 0xFFFF_FFFF
        operations.append(pack_operation(
            tx1_addr,
            tx1_data,
            tx2_addr,
            tx2_data,
            args.clock_count,
            args.op_flags,
        ))
    return operations


def delay_profile_half_cycles(args):
    return [
        args.delay_base_half_cycles + index * args.delay_step_half_cycles
        for index in range(nvus_tx.TX_PW_ELEMENTS)
    ]


def delay_profile_words(args):
    delays = delay_profile_half_cycles(args)
    return {
        target: nvus_tx.profile_words(nvus_tx.chip_delays(delays)[target])
        for target in nvus_tx.TX_TARGETS
    }


def delay_profile_operations(args):
    chip_words = delay_profile_words(args)
    reg0 = nvus_tx.TX_DELAY_PROFILE_REG_BASE + args.delay_start_word
    reg1 = reg0 + 1
    operations = [
        pack_operation(
            TX_READ_ENABLE_REG, TX_READ_ENABLE_WRITE,
            TX_READ_ENABLE_REG, TX_READ_ENABLE_WRITE,
            args.clock_count, args.op_flags,
        )
    ]
    if args.trsw_select != TRSW_SELECT_NONE:
        value = nvus_tx.TX_PROBE_RX_SELECT_VALUES[args.trsw_select]
        operations.append(pack_operation(
            nvus_tx.TX_TRSW_SEL_REG, value,
            nvus_tx.TX_TRSW_SEL_REG, value,
            args.clock_count, args.op_flags,
        ))
    for block in range(16):
        page_mask = 1 << block
        tx1_word0, tx1_word1 = chip_words["tx1"][block]
        tx2_word0, tx2_word1 = chip_words["tx2"][block]
        operations.append(pack_operation(
            nvus_tx.TX_MEM_PAGE_A_REG, page_mask,
            nvus_tx.TX_MEM_PAGE_A_REG, page_mask,
            args.clock_count, args.op_flags,
        ))
        operations.append(pack_operation(
            nvus_tx.TX_MEM_PAGE_B_REG, page_mask,
            nvus_tx.TX_MEM_PAGE_B_REG, page_mask,
            args.clock_count, args.op_flags,
        ))
        operations.append(pack_operation(
            reg0, tx1_word0,
            reg0, tx2_word0,
            args.clock_count, args.op_flags,
        ))
        operations.append(pack_operation(
            reg1, tx1_word1,
            reg1, tx2_word1,
            args.clock_count, args.op_flags,
        ))
    return operations


def packet_operations(args):
    if args.payload_mode == PAYLOAD_MODE_DELAY_PROFILE:
        return delay_profile_operations(args)
    return simple_operations(args)


def build_packet(args, packet_index):
    sequence = (args.sequence + packet_index * args.sequence_step) & 0xFFFF
    operations = packet_operations(args)
    packet = bytearray(build_header(sequence, len(operations), args.futz_header))

    for operation in operations:
        packet += operation

    if len(packet) > TX_HS_SPI_MAX_PACKET_BYTES:
        raise ValueError(f"packet is {len(packet)} bytes, max is {TX_HS_SPI_MAX_PACKET_BYTES}")
    return bytes(packet)


def packet_op_count(packet):
    return (len(packet) - TX_HS_SPI_FRAGMENT_HDR_BYTES) // TX_HS_SPI_OP_BYTES


def configure_tx_hs_spi(hololink, args):
    if args.skip_tx_hs_spi_setup:
        logging.info("Skipping TX-HS SPI setup")
        return

    logging.info(
        "Configuring TX-HS SPI: cfg half_period_cycles=%d",
        args.tx_hs_spi_half_period_cycles,
    )
    hololink.write_uint32(TX_HS_SPI_CFG, args.tx_hs_spi_half_period_cycles)


def configure_sensor_tx(hololink, args):
    if args.skip_sensor_tx_setup:
        logging.info("Skipping Sensor TX setup")
        return

    base = args.sensor_tx_base
    logging.info(
        "Configuring Sensor TX: base=0x%08x almost_full=%d almost_empty=%d",
        base,
        args.sensor_tx_almost_full,
        args.sensor_tx_almost_empty,
    )
    hololink.write_uint32(base + 0x04, args.sensor_tx_almost_full)
    hololink.write_uint32(base + 0x08, args.sensor_tx_almost_empty)
    hololink.write_uint32(base + 0x00, SENSOR_TX_CTRL_LATCH_THRESHOLDS)
    hololink.write_uint32(base + 0x00, SENSOR_TX_CTRL_IDLE)
    hololink.write_uint32(base + 0x00, SENSOR_TX_CTRL_ENABLE)
    time.sleep(args.sensor_tx_setup_sleep_s)


def get_hololink_module():
    import hololink as hololink_module
    return hololink_module


def send_packets_direct(hololink_module, args):
    transmitter = hololink_module.operators.RoceTransmitter(
        args.ibv_name,
        args.ibv_port,
        TX_HS_SPI_MAX_PACKET_BYTES,
        args.queue_size,
        args.hololink,
        args.ibv_qp,
    )
    transmitter.start()
    try:
        for packet_index in range(args.repeat):
            packet = build_packet(args, packet_index)
            transmitter.send(packet)
            logging.info(
                "sent package index=%d sequence=0x%04x ops=%d bytes=%d futz_header=%s send_path=%s",
                packet_index,
                (args.sequence + packet_index * args.sequence_step) & 0xFFFF,
                packet_op_count(packet),
                len(packet),
                args.futz_header,
                TX_SEND_PATH_DIRECT,
            )
        if not transmitter.flush(args.tx_flush_timeout_ms):
            raise RuntimeError(
                f"RoCE TX direct flush timed out after {args.tx_flush_timeout_ms} ms"
            )
    finally:
        transmitter.stop()


def make_tx_update_package_app(holoscan, cp, np, hololink_module):
    class TxUpdatePackageSourceOp(holoscan.core.Operator):
        def __init__(self, fragment, *args, cli_args, **kwargs):
            self._args = cli_args
            self._sent_packets = 0
            self._device_buffers = []
            super().__init__(fragment, *args, **kwargs)

        def setup(self, spec):
            spec.output("output")

        def compute(self, op_input, op_output, context):
            packet = build_packet(self._args, self._sent_packets)
            host_buffer = np.frombuffer(packet, dtype=np.uint8).copy()
            device_buffer = cp.asarray(host_buffer)
            self._device_buffers.append(device_buffer)
            op_output.emit({"": device_buffer}, "output")

            logging.info(
                "sent package index=%d sequence=0x%04x ops=%d bytes=%d futz_header=%s",
                self._sent_packets,
                (self._args.sequence + self._sent_packets * self._args.sequence_step) & 0xFFFF,
                packet_op_count(packet),
                len(packet),
                self._args.futz_header,
            )
            self._sent_packets += 1

    class TxUpdatePackageApp(holoscan.core.Application):
        def __init__(self, args):
            super().__init__()
            self._args = args

        def compose(self):
            source_condition = holoscan.conditions.CountCondition(self, name="source_count", count=self._args.repeat)
            source = TxUpdatePackageSourceOp(
                self,
                source_condition,
                cli_args=self._args,
                name="tx_update_package_source",
            )
            transmitter_condition = holoscan.conditions.CountCondition(
                self,
                name="transmitter_count",
                count=self._args.repeat,
            )

            def transmitter_on_stop(info):
                if self._args.tx_stop_drain_s > 0.0:
                    logging.info(
                        "waiting %.3fs for RoCE TX drain before stop",
                        self._args.tx_stop_drain_s,
                    )
                    time.sleep(self._args.tx_stop_drain_s)

            transmitter = hololink_module.operators.RoceTransmitterOp(
                self,
                transmitter_condition,
                name="roce_transmitter",
                ibv_name=self._args.ibv_name,
                ibv_port=self._args.ibv_port,
                hololink_ip=self._args.hololink,
                ibv_qp=self._args.ibv_qp,
                buffer_size=TX_HS_SPI_MAX_PACKET_BYTES,
                queue_size=self._args.queue_size,
                on_stop=transmitter_on_stop,
            )
            self.add_flow(source, transmitter, {("output", "input")})

    return TxUpdatePackageApp


def default_ibv_name():
    try:
        hololink_module = get_hololink_module()
        devices = hololink_module.infiniband_devices()
        if devices:
            return devices[0]
    except (FileNotFoundError, ModuleNotFoundError):
        pass
    return "roceP2p1s0f0"


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hololink", default="192.168.0.2", help="Target Hololink board IP")
    parser.add_argument("--ibv-name", default=default_ibv_name(), help="IBV device for RoCE TX")
    parser.add_argument("--ibv-port", type=int, default=1, help="IBV port for RoCE TX")
    parser.add_argument("--ibv-qp", type=int, default=2, help="Remote QP number for Sensor TX")
    parser.add_argument("--queue-size", type=int, default=2, help="RoCE TX queue size")
    parser.add_argument("--tx-send-path", choices=(TX_SEND_PATH_DIRECT, TX_SEND_PATH_OPERATOR),
                        default=TX_SEND_PATH_DIRECT,
                        help="RoCE TX implementation to use: direct reusable class or legacy Holoscan operator wrapper")
    parser.add_argument("--tx-flush-timeout-ms", type=int, default=1000,
                        help="Direct RoCE TX flush timeout after queueing all packets")
    parser.add_argument("--tx-stop-drain-s", type=float, default=0.1, help="Operator-path delay in RoCE TX on_stop so pending sends can drain")
    parser.add_argument("--repeat", type=int, default=1, help="Number of packages to send")
    parser.add_argument("--payload-mode", choices=(PAYLOAD_MODE_SIMPLE, PAYLOAD_MODE_DELAY_PROFILE), default=PAYLOAD_MODE_SIMPLE)
    parser.add_argument("--sequence", type=parse_int, default=0x1234, help="Starting package sequence")
    parser.add_argument("--sequence-step", type=parse_int, default=1, help="Sequence increment per package")
    parser.add_argument("--ops", type=int, default=4, help="Operation records per package")
    parser.add_argument("--tx1-addr", type=parse_u10, default=0x001, help="TX1 starting register address")
    parser.add_argument("--tx2-addr", type=parse_u10, default=0x101, help="TX2 starting register address")
    parser.add_argument("--addr-stride", type=parse_int, default=1, help="Address increment per operation")
    parser.add_argument("--tx1-data", type=parse_u32, default=0x11110000, help="TX1 starting data")
    parser.add_argument("--tx2-data", type=parse_u32, default=0x22220000, help="TX2 starting data")
    parser.add_argument("--data-stride", type=parse_int, default=1, help="Data increment per operation")
    parser.add_argument("--clock-count", type=parse_u8, default=TX_HS_SPI_NORMAL_WRITE_BITS, help="SPI clocks per op")
    parser.add_argument("--op-flags", type=parse_u8, default=0, help="Operation flags byte")
    parser.add_argument("--delay-start-word", type=parse_int, default=nvus_tx.TX_PW_DELAY_START_WORD_DEFAULT)
    parser.add_argument("--delay-base-half-cycles", type=parse_int, default=0)
    parser.add_argument("--delay-step-half-cycles", type=parse_int, default=1)
    parser.add_argument("--verify-delay-profile", action="store_true", help="After transmit, read back the generated delay profile through the TX-HS slow path")
    parser.add_argument("--trsw-select", choices=(TRSW_SELECT_NONE, *nvus_tx.TX_PROBE_RX_SELECT_VALUES), default=TRSW_SELECT_NONE)
    parser.add_argument(
        "--futz-header",
        choices=("none", "magic", "version", "header-bytes", "record-bytes", "op-count-zero", "op-count-too-big"),
        default="none",
        help="Deliberately corrupt one checked header field",
    )
    parser.add_argument("--dump-packet", default=None, help="Optional path to write the first packet bytes")
    parser.add_argument("--dry-run", action="store_true", help="Build and optionally dump packet, but do not transmit")
    parser.add_argument("--skip-sensor-tx-setup", action="store_true", help="Do not program Sensor TX FIFO/pause registers")
    parser.add_argument("--skip-tx-hs-spi-setup", action="store_true", help="Do not program TX-HS SPI config registers")
    parser.add_argument(
        "--tx-hs-spi-half-period-cycles",
        type=parse_int,
        default=TX_HS_SPI_DEFAULT_HALF_PERIOD_CYCLES,
        help="TX-HS SPI half-period cycles config; default 0 lets RTL select the fastest current mode",
    )
    parser.add_argument("--sensor-tx-base", type=parse_int, default=TX_HS_SPI_SENSOR_TX_BASE)
    parser.add_argument("--sensor-tx-almost-full", type=parse_int, default=1)
    parser.add_argument("--sensor-tx-almost-empty", type=parse_int, default=0)
    parser.add_argument("--sensor-tx-setup-sleep-s", type=float, default=0.01)
    parser.add_argument("--log-level", type=int, default=20, help="Logging level")
    return parser


def validate_args(args):
    if args.repeat < 1:
        raise SystemExit("--repeat must be >= 1")
    if args.ops < 1:
        raise SystemExit("--ops must be >= 1")
    if args.delay_start_word < 0:
        raise SystemExit("--delay-start-word must be >= 0")

    op_count = len(packet_operations(args))
    if op_count > TX_HS_SPI_DEFAULT_MAX_OPS_PER_BANK and args.futz_header != "op-count-too-big":
        raise SystemExit(
            f"payload has {op_count} ops, max is {TX_HS_SPI_DEFAULT_MAX_OPS_PER_BANK} for the current RTL"
        )
    packet_bytes = TX_HS_SPI_FRAGMENT_HDR_BYTES + op_count * TX_HS_SPI_OP_BYTES
    if packet_bytes > TX_HS_SPI_MAX_PACKET_BYTES:
        raise SystemExit(f"packet would be {packet_bytes} bytes; max is {TX_HS_SPI_MAX_PACKET_BYTES}")


def verify_delay_profile(hololink, args):
    if not args.verify_delay_profile:
        return
    if args.payload_mode != PAYLOAD_MODE_DELAY_PROFILE:
        raise SystemExit("--verify-delay-profile requires --payload-mode delay-profile")

    bus = Bus(hololink, tx_spi_prescaler=TX_SPI_DEFAULT_PRESCALER, tx_spi_path=TX_SPI_PATH_TX_HS)
    chip_words = delay_profile_words(args)
    reg0 = nvus_tx.TX_DELAY_PROFILE_REG_BASE + args.delay_start_word
    reg1 = reg0 + 1
    failures = 0

    for block in range(16):
        page_mask = 1 << block
        bus.tx_write("broadcast", nvus_tx.TX_MEM_PAGE_A_REG, page_mask)
        bus.tx_write("broadcast", nvus_tx.TX_MEM_PAGE_B_REG, page_mask)
        for target in nvus_tx.TX_TARGETS:
            expected0, expected1 = chip_words[target][block]
            actual0 = bus.tx_read(target, reg0)
            actual1 = bus.tx_read(target, reg1)
            ok0 = actual0 == expected0
            ok1 = actual1 == expected1
            print(f"{'PASS' if ok0 else 'FAIL'} {target} page {block:02d} reg 0x{reg0:03x} => 0x{actual0:08x}; expected 0x{expected0:08x}")
            print(f"{'PASS' if ok1 else 'FAIL'} {target} page {block:02d} reg 0x{reg1:03x} => 0x{actual1:08x}; expected 0x{expected1:08x}")
            failures += 0 if ok0 else 1
            failures += 0 if ok1 else 1

    if args.trsw_select != TRSW_SELECT_NONE:
        expected = nvus_tx.TX_PROBE_RX_SELECT_VALUES[args.trsw_select]
        for target in nvus_tx.TX_TARGETS:
            actual = bus.tx_read(target, nvus_tx.TX_TRSW_SEL_REG)
            ok = actual == expected
            print(f"{'PASS' if ok else 'FAIL'} {target} reg 0x{nvus_tx.TX_TRSW_SEL_REG:03x} => 0x{actual:08x}; expected 0x{expected:08x}")
            failures += 0 if ok else 1

    if failures:
        raise SystemExit(failures)
    logging.info("delay profile readback verified")


def main():
    parser = build_parser()
    args = parser.parse_args()
    validate_args(args)

    logging.basicConfig(level=args.log_level, format="%(levelname)s %(message)s")
    first_packet = build_packet(args, 0)
    logging.info("first packet bytes=%d ops=%d sequence=0x%04x", len(first_packet), packet_op_count(first_packet), args.sequence & 0xFFFF)
    if args.dump_packet:
        with open(args.dump_packet, "wb") as f:
            f.write(first_packet)
        logging.info("wrote %s", args.dump_packet)

    if args.dry_run:
        preview = first_packet[: min(128, len(first_packet))]
        print(" ".join(f"{byte:02x}" for byte in preview))
        return 0

    hololink_module = get_hololink_module()
    hololink_module.logging_level(args.log_level)

    channel_metadata = hololink_module.Enumerator.find_channel(channel_ip=args.hololink)
    hololink_channel = hololink_module.DataChannel(channel_metadata)
    hololink = hololink_channel.hololink()
    hololink.start()
    configure_tx_hs_spi(hololink, args)
    configure_sensor_tx(hololink, args)

    if args.tx_send_path == TX_SEND_PATH_DIRECT:
        send_packets_direct(hololink_module, args)
    else:
        import cupy as cp
        import holoscan
        import numpy as np

        TxUpdatePackageApp = make_tx_update_package_app(holoscan, cp, np, hololink_module)
        app = TxUpdatePackageApp(args)
        app.run()

    verify_delay_profile(hololink, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
