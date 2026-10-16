# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""NVUS TX high-speed SPI update package builder.

This module is intentionally transport-free: it only converts TX register
updates into the wire-format records consumed by the FPGA tx_hs_spi package
receiver. Sender ownership stays with the caller.
"""

import struct

from . import tx as nvus_tx

TX_HS_SPI_MAGIC = 0x44585455
TX_HS_SPI_PROTOCOL_VERSION = 1
TX_HS_SPI_FRAGMENT_HDR_BYTES = 32
TX_HS_SPI_OP_BYTES = 32
TX_HS_SPI_DEFAULT_MAX_OPS_PER_BANK = 128
TX_HS_SPI_MAX_PACKET_BYTES = 4096
TX_HS_SPI_NORMAL_WRITE_BITS = 44
TX_READ_ENABLE_REG = 0x000
TX_READ_ENABLE_WRITE = 0x0000_0000
TRSW_SELECT_NONE = "none"

HEADER_STRUCT = struct.Struct("<IBBHHHHHHHHHII")


def pack_u48_le(value):
    if not 0 <= value < (1 << 48):
        raise ValueError(f"0x{value:x} is outside 48-bit unsigned range")
    return int(value).to_bytes(6, byteorder="little")


def normal_write_word(address, data):
    return ((address & 0x3FF) << 32) | (data & 0xFFFF_FFFF)


def pack_operation(tx1_address, tx1_data, tx2_address, tx2_data,
                   clock_count=TX_HS_SPI_NORMAL_WRITE_BITS, flags=0):
    """Pack one normal-write operation for both TX chips."""
    record = bytearray()
    record += pack_u48_le(normal_write_word(tx1_address, tx1_data))
    record += pack_u48_le(0)
    record += pack_u48_le(normal_write_word(tx2_address, tx2_data))
    record += pack_u48_le(0)
    record += struct.pack("<BBHI", clock_count & 0xFF, flags & 0xFF, 0, 0)
    if len(record) != TX_HS_SPI_OP_BYTES:
        raise AssertionError(f"operation record is {len(record)} bytes")
    return bytes(record)


def build_header(sequence, op_count):
    payload_bytes = op_count * TX_HS_SPI_OP_BYTES
    return HEADER_STRUCT.pack(
        TX_HS_SPI_MAGIC,
        TX_HS_SPI_PROTOCOL_VERSION,
        TX_HS_SPI_FRAGMENT_HDR_BYTES,
        0,
        sequence & 0xFFFF,
        0,
        1,
        TX_HS_SPI_OP_BYTES,
        op_count,
        op_count,
        payload_bytes,
        0,
        payload_bytes,
        0,
    )


def build_packet(sequence, operations):
    op_count = len(operations)
    if op_count > TX_HS_SPI_DEFAULT_MAX_OPS_PER_BANK:
        raise ValueError(
            f"payload has {op_count} ops, max is {TX_HS_SPI_DEFAULT_MAX_OPS_PER_BANK}"
        )
    packet = bytearray(build_header(sequence, op_count))
    for operation in operations:
        if len(operation) != TX_HS_SPI_OP_BYTES:
            raise ValueError(f"operation is {len(operation)} bytes")
        packet += operation
    if len(packet) > TX_HS_SPI_MAX_PACKET_BYTES:
        raise ValueError(f"packet is {len(packet)} bytes, max is {TX_HS_SPI_MAX_PACKET_BYTES}")
    return bytes(packet)


def delay_profile_operations(half_cycles128, *, delay_start_word, trsw_select=TRSW_SELECT_NONE,
                             clock_count=TX_HS_SPI_NORMAL_WRITE_BITS, flags=0):
    """Operations to load one 128-element delay profile and optional TRSW select."""
    chip_words = {
        target: nvus_tx.profile_words(nvus_tx.chip_delays(half_cycles128)[target])
        for target in nvus_tx.TX_TARGETS
    }
    reg0 = nvus_tx.TX_DELAY_PROFILE_REG_BASE + delay_start_word
    reg1 = reg0 + 1
    operations = [
        pack_operation(
            TX_READ_ENABLE_REG, TX_READ_ENABLE_WRITE,
            TX_READ_ENABLE_REG, TX_READ_ENABLE_WRITE,
            clock_count, flags,
        )
    ]
    if trsw_select != TRSW_SELECT_NONE:
        value = nvus_tx.TX_PROBE_RX_SELECT_VALUES[trsw_select]
        operations.append(pack_operation(
            nvus_tx.TX_TRSW_SEL_REG, value,
            nvus_tx.TX_TRSW_SEL_REG, value,
            clock_count, flags,
        ))
    for block in range(16):
        page_mask = 1 << block
        tx1_word0, tx1_word1 = chip_words["tx1"][block]
        tx2_word0, tx2_word1 = chip_words["tx2"][block]
        operations.append(pack_operation(
            nvus_tx.TX_MEM_PAGE_A_REG, page_mask,
            nvus_tx.TX_MEM_PAGE_A_REG, page_mask,
            clock_count, flags,
        ))
        operations.append(pack_operation(
            nvus_tx.TX_MEM_PAGE_B_REG, page_mask,
            nvus_tx.TX_MEM_PAGE_B_REG, page_mask,
            clock_count, flags,
        ))
        operations.append(pack_operation(
            reg0, tx1_word0,
            reg0, tx2_word0,
            clock_count, flags,
        ))
        operations.append(pack_operation(
            reg1, tx1_word1,
            reg1, tx2_word1,
            clock_count, flags,
        ))
    return operations


def plane_wave_operations(angle_rad, *, c=nvus_tx.TX_PW_SOUND_SPEED_MPS,
                          delay_start_word=nvus_tx.TX_PW_DELAY_START_WORD_DEFAULT,
                          trsw_select=TRSW_SELECT_NONE,
                          clock_count=TX_HS_SPI_NORMAL_WRITE_BITS, flags=0):
    delays = nvus_tx.plane_wave_delays(angle_rad, c=c)
    return delay_profile_operations(
        delays,
        delay_start_word=delay_start_word,
        trsw_select=trsw_select,
        clock_count=clock_count,
        flags=flags,
    )


def plane_wave_packet(sequence, angle_rad, *, c=nvus_tx.TX_PW_SOUND_SPEED_MPS,
                      delay_start_word=nvus_tx.TX_PW_DELAY_START_WORD_DEFAULT,
                      trsw_select=TRSW_SELECT_NONE,
                      clock_count=TX_HS_SPI_NORMAL_WRITE_BITS, flags=0):
    operations = plane_wave_operations(
        angle_rad,
        c=c,
        delay_start_word=delay_start_word,
        trsw_select=trsw_select,
        clock_count=clock_count,
        flags=flags,
    )
    return build_packet(sequence, operations)


def packet_op_count(packet):
    return (len(packet) - TX_HS_SPI_FRAGMENT_HDR_BYTES) // TX_HS_SPI_OP_BYTES
