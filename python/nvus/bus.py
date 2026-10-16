# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""NVUS board-control transport, the tx_write_packet SPI encoder, and the Trace dry-run backend."""

import logging
import time

BOARD_CTRL_BASE = 0x6000_0000
BOARD_CTRL_REG = BOARD_CTRL_BASE + 0x00
BOARD_STATUS = BOARD_CTRL_BASE + 0x80
BOARD_LVDS_STATUS = BOARD_CTRL_BASE + 0x84

BOARD_RX_MUX_LSB = 0
BOARD_TX_MUX_LSB = 2
BOARD_PDN_STANDBY = 4
BOARD_CRC_EN = 5
BOARD_RESET = 6
BOARD_RX_SPI_LVDS_EN = 8
BOARD_TX_SPI_DUAL_LANE_EN = 9

BOARD_CRC_MATCH = 9
BOARD_TX_TSHUT = 10
BOARD_TX_RXZ = 11
BOARD_AFE1_VALID = 12
BOARD_AFE2_VALID = 13
BOARD_LVDS_ALIVE = 14
BOARD_LVDS_ACTIVE = 15

RX_MUX_VALUES = {"broadcast": 0, "rx1": 1, "rx2": 2}
TX_MUX_VALUES = {"broadcast": 0, "tx1": 1, "tx2": 2}

RX_SPI_BUS = 0
TX_SPI_BUS = 1
TX_READ_ENABLE_REG = 0x000
TX_READ_ENABLE_WRITE = 0x0000_0000
TX_READ_ENABLE_READ = 0x0000_0002
# SCLK is 100 MHz / (2 * (prescaler + 1)); 0x0F gives the bring-up-proven 3.125 MHz.
TX_SPI_DEFAULT_PRESCALER = 0x0F

TX_SPI_PATH_HSB = "hsb"
TX_SPI_PATH_TX_HS = "tx-hs"
TX_SPI_PATHS = (TX_SPI_PATH_HSB, TX_SPI_PATH_TX_HS)

TX_HS_SPI_BASE = 0x7000_0000
TX_HS_SPI_CTRL = TX_HS_SPI_BASE + 0x00
TX_HS_SPI_CFG = TX_HS_SPI_BASE + 0x04
TX_HS_SPI_TX1_ADDR = TX_HS_SPI_BASE + 0x08
TX_HS_SPI_TX1_DATA = TX_HS_SPI_BASE + 0x0C
TX_HS_SPI_TX2_ADDR = TX_HS_SPI_BASE + 0x10
TX_HS_SPI_TX2_DATA = TX_HS_SPI_BASE + 0x14
TX_HS_SPI_STATUS = TX_HS_SPI_BASE + 0x80
TX_HS_SPI_READ_DATA = TX_HS_SPI_BASE + 0x84

TX_HS_SPI_CTRL_START = 0x1
TX_HS_SPI_CTRL_OP_WRITE = 0x0
TX_HS_SPI_CTRL_OP_READ = 0x1
TX_HS_SPI_CTRL_WRITE_MASK_TX1 = 0x1
TX_HS_SPI_CTRL_WRITE_MASK_TX2 = 0x2
TX_HS_SPI_CTRL_TARGET_TX1 = 0x1
TX_HS_SPI_CTRL_TARGET_TX2 = 0x2
TX_HS_SPI_STATUS_BUSY = 1 << 0
TX_HS_SPI_STATUS_ERROR = 1 << 1
TX_HS_SPI_STATUS_DONE = 1 << 4
TX_HS_SPI_TIMEOUT_S = 0.25

_MISSING = object()


def tx_write_packet(reg, value):
    """Encode one TX73L64 register write as its 6-byte SPI packet."""
    reg &= 0x3FF
    value &= 0xFFFF_FFFF
    return [
        (reg >> 4) & 0x3F,
        ((reg & 0xF) << 4) | ((value >> 28) & 0xF),
        (value >> 20) & 0xFF,
        (value >> 12) & 0xFF,
        (value >> 4) & 0xFF,
        (value & 0xF) << 4,
    ]


def tx_dual_write_packet(reg, value):
    """Encode one register write mirrored onto both lanes of a width-2 SPI packet.

    Each output byte carries 4 bit-times, the same bit on SDATA_1 (bits
    7/5/3/1) and SDATA_0 (bits 6/4/2/0), MSB-first. Per-lane-distinct data
    (the delay-profile speedup) has no caller yet; generalize when it does.
    """
    return [
        sum(0b11 << (6 - 2 * i) for i in range(4) if nibble & (8 >> i))
        for byte in tx_write_packet(reg, value)
        for nibble in (byte >> 4, byte & 0xF)
    ]


def tx_dual_packet_lane0(packet):
    """Recover the 6-byte SDATA_0 lane packet from a width-2 SPI packet."""
    bits = [(byte >> (6 - 2 * i)) & 1 for byte in packet for i in range(4)]
    return bytes(sum(bits[index + bit] << (7 - bit) for bit in range(8))
                 for index in range(0, len(bits), 8))


class Trace:
    """A fake hololink handle: records ordered events and answers reads; dry run = Bus(Trace()).

    Dry-run Evm: Trace(reads=fpga.ready_reads())."""

    def __init__(self, reads=None):
        # reads: {address: value-or-list} for read_uint32, {("spi", bus, reg): value-or-list}
        # for SPI register reads; lists are consumed in order, then reads fall back to the
        # stateful map of values written so far, else 0. TX HS SPI dry-run reads may also
        # be scripted as ("tx-hs", "tx1"/"tx2", reg).
        self.events = []
        self._reads = dict(reads or {})
        self._regs = {}
        self._spis = {}
        self._chip_values = {}  # per-bus register file, shared across handle widths
        self._txhs_values = {"tx1": {}, "tx2": {}}

    def write_uint32(self, address, value):
        self._regs[address] = value
        self._handle_tx_hs_write(address, value)
        self.events.append(("wr32", address, value))

    def read_uint32(self, address):
        value = self._scripted(address)
        if value is _MISSING:
            value = self._regs.get(address, 0)
        self.events.append(("rd32", address, value))
        return value

    def _handle_tx_hs_write(self, address, value):
        if address == TX_HS_SPI_CTRL:
            if value & TX_HS_SPI_CTRL_START:
                self._run_tx_hs_command(value)
            elif value == 0 and (self._regs.get(TX_HS_SPI_STATUS, 0) & TX_HS_SPI_STATUS_DONE):
                self._regs[TX_HS_SPI_STATUS] = 0

    def _run_tx_hs_command(self, command):
        op = (command >> 1) & 0x3
        mask = (command >> 4) & 0x3
        target = (command >> 8) & 0x3
        status = TX_HS_SPI_STATUS_DONE | (op << 16) | (mask << 18) | (target << 20)
        if op == TX_HS_SPI_CTRL_OP_WRITE:
            if mask & TX_HS_SPI_CTRL_WRITE_MASK_TX1:
                self._txhs_values["tx1"][self._regs.get(TX_HS_SPI_TX1_ADDR, 0) & 0x3FF] = (
                    self._regs.get(TX_HS_SPI_TX1_DATA, 0) & 0xFFFF_FFFF
                )
            if mask & TX_HS_SPI_CTRL_WRITE_MASK_TX2:
                self._txhs_values["tx2"][self._regs.get(TX_HS_SPI_TX2_ADDR, 0) & 0x3FF] = (
                    self._regs.get(TX_HS_SPI_TX2_DATA, 0) & 0xFFFF_FFFF
                )
        elif op == TX_HS_SPI_CTRL_OP_READ and target in (
            TX_HS_SPI_CTRL_TARGET_TX1, TX_HS_SPI_CTRL_TARGET_TX2
        ):
            name = "tx1" if target == TX_HS_SPI_CTRL_TARGET_TX1 else "tx2"
            addr_reg = TX_HS_SPI_TX1_ADDR if name == "tx1" else TX_HS_SPI_TX2_ADDR
            reg = self._regs.get(addr_reg, 0) & 0x3FF
            value = self._scripted(("tx-hs", name, reg))
            if value is _MISSING:
                value = self._txhs_values[name].get(reg, 0)
            self._regs[TX_HS_SPI_READ_DATA] = value & 0xFFFF_FFFF
        else:
            status |= TX_HS_SPI_STATUS_ERROR
        self._regs[TX_HS_SPI_STATUS] = status

    def get_spi(self, bus, chip_select, prescaler=None, width=1):
        return self._spis.setdefault((bus, chip_select, width), _TraceSpi(self, bus))

    def _scripted(self, key):
        entry = self._reads.get(key, _MISSING)
        if isinstance(entry, list):
            return entry.pop(0) if entry else _MISSING
        return entry


class _TraceSpi:
    """Recording SPI: logs transactions on the shared Trace and answers reads statefully."""

    def __init__(self, trace, bus):
        self._trace = trace
        self._bus = bus
        self._values = trace._chip_values.setdefault(bus, {})

    def spi_transaction(self, cmd, data, read_count):
        cmd, data = bytes(cmd), bytes(data)
        self._trace.events.append(("spi", self._bus, cmd, data, read_count))
        self._remember_write(cmd, data)
        response = list(cmd) + list(data)
        if read_count:
            value = self._read_value(cmd) & 0xFFFF_FFFF
            response += list(value.to_bytes(4, "big")[-read_count:])
        return response

    def _remember_write(self, cmd, data):
        if self._bus == TX_SPI_BUS and len(data) == 6:
            reg, value = _tx_packet_reg_value(data)
            self._values[reg] = value
        elif self._bus == RX_SPI_BUS and len(data) == 2:
            self._values[cmd[0]] = (data[0] << 8) | data[1]

    def _read_value(self, cmd):
        if self._bus == TX_SPI_BUS and len(cmd) == 7:
            reg = ((cmd[5] & 0x03) << 8) | cmd[6]
        else:
            reg = cmd[0]
        value = self._trace._scripted(("spi", self._bus, reg))
        return self._values.get(reg, 0) if value is _MISSING else value


class Bus:
    """Board-control register, mux, and AFE/TX SPI transport over a hololink handle or a Trace."""

    def __init__(self, hololink, spi_delay_s=0.0, tx_spi_prescaler=TX_SPI_DEFAULT_PRESCALER,
                 tx_spi_path=TX_SPI_PATH_HSB):
        if tx_spi_path not in TX_SPI_PATHS:
            raise ValueError(f"unknown TX SPI path {tx_spi_path!r}; expected one of {TX_SPI_PATHS}")
        self._hololink = hololink
        self._spi_delay_s = spi_delay_s
        self._tx_spi_path = tx_spi_path
        self._tx_hs_half_period_cycles = (tx_spi_prescaler & 0xFFFF) + 1
        self._tx_hs_cfg_written = False
        self._spi = hololink.get_spi(RX_SPI_BUS, 0)
        self._tx_spi = hololink.get_spi(TX_SPI_BUS, 0, prescaler=tx_spi_prescaler)
        self._tx_spi_dual = hololink.get_spi(TX_SPI_BUS, 0, prescaler=tx_spi_prescaler, width=2)

    def write32(self, address, value):
        logging.debug("write %#010x <= %#010x", address, value)
        self._hololink.write_uint32(address, value)

    def read32(self, address):
        return self._hololink.read_uint32(address)

    def gpio_init(self):
        self.write32(BOARD_CTRL_REG, 0)

    def set_control(self, pdn_standby=None, crc_en=None, reset=None, rx_spi_lvds_en=None,
                    tx_spi_dual_lane=None, rx=None, tx=None):
        # One read-modify-write of the board-control register.
        value = self.read32(BOARD_CTRL_REG)
        if rx is not None:
            value &= ~(0x3 << BOARD_RX_MUX_LSB)
            value |= (RX_MUX_VALUES[rx] & 0x3) << BOARD_RX_MUX_LSB
        if tx is not None:
            value &= ~(0x3 << BOARD_TX_MUX_LSB)
            value |= (TX_MUX_VALUES[tx] & 0x3) << BOARD_TX_MUX_LSB
        for control_value, control_bit in (
            (pdn_standby, BOARD_PDN_STANDBY),
            (crc_en, BOARD_CRC_EN),
            (reset, BOARD_RESET),
            (rx_spi_lvds_en, BOARD_RX_SPI_LVDS_EN),
            (tx_spi_dual_lane, BOARD_TX_SPI_DUAL_LANE_EN),
        ):
            if control_value is None:
                continue
            if control_value:
                value |= 1 << control_bit
            else:
                value &= ~(1 << control_bit)
        self.write32(BOARD_CTRL_REG, value)

    def show_gpio(self):
        board_ctrl = self.read32(BOARD_CTRL_REG)
        board_status = self.read32(BOARD_STATUS)
        board_lvds_status = self.read32(BOARD_LVDS_STATUS)
        rx_mux = (board_ctrl >> BOARD_RX_MUX_LSB) & 0x3
        tx_mux = (board_ctrl >> BOARD_TX_MUX_LSB) & 0x3
        print(f"board_ctrl        = 0x{board_ctrl:08x}")
        print(f"board_status      = 0x{board_status:08x}")
        print(f"board_lvds_status = 0x{board_lvds_status:08x}")
        print(f"rx_spi_mux  = {_mux_name(RX_MUX_VALUES, rx_mux)} ({rx_mux})")
        print(f"tx_spi_mux  = {_mux_name(TX_MUX_VALUES, tx_mux)} ({tx_mux})")
        for name, word, bit in (
            ("pdn_standby", board_ctrl, BOARD_PDN_STANDBY),
            ("crc_en", board_ctrl, BOARD_CRC_EN),
            ("reset", board_ctrl, BOARD_RESET),
            ("rx_lvds_en", board_ctrl, BOARD_RX_SPI_LVDS_EN),
            ("crc_match", board_status, BOARD_CRC_MATCH),
            ("tx_tshut", board_status, BOARD_TX_TSHUT),
            ("tx_rxz", board_status, BOARD_TX_RXZ),
            ("afe1_valid", board_status, BOARD_AFE1_VALID),
            ("afe2_valid", board_status, BOARD_AFE2_VALID),
            ("lvds_alive", board_status, BOARD_LVDS_ALIVE),
            ("lvds_active", board_status, BOARD_LVDS_ACTIVE),
        ):
            print(f"{name:<11} = {(word >> bit) & 1}")

    def afe_write(self, target, reg, value):
        logging.debug("afe-write %s reg=%#x value=%#x", target, reg, value)
        self.set_control(rx=target)
        if reg != 0:
            self._afe_write_raw(0x00, 0x0000)  # clear the AFE5932 address pointer
        self._afe_write_raw(reg, value)

    def afe_read(self, target, reg):
        # readout-enable (reg 0 = 0x0002) persists until the next afe_write's pointer clear erases it
        logging.debug("afe-read %s reg=%#x", target, reg)
        self.set_control(rx=target)
        self._afe_write_raw(0x00, 0x0002)  # set the AFE5932 register-readout enable
        return self._afe_read_raw(reg)

    def tx_write(self, target, reg, value):
        logging.debug("tx-write %s reg=%#x value=%#x", target, reg, value)
        if self._tx_spi_path == TX_SPI_PATH_TX_HS:
            self._tx_hs_write(target, TX_READ_ENABLE_REG, TX_READ_ENABLE_WRITE)
            self._tx_hs_write(target, reg, value)
            return
        # dual_lane=False folds the old app's defensive per-write clear of the
        # dual-lane bit into the mux-select RMW instead of a second write.
        self.set_control(tx=target, tx_spi_dual_lane=False)
        self._tx_write_raw(TX_READ_ENABLE_REG, TX_READ_ENABLE_WRITE)
        self._tx_write_raw(reg, value)

    def tx_write_dual(self, target, reg, value):
        """One register write mirrored on both SPI SDATA lanes (width-2 packet)."""
        logging.debug("tx-write-dual %s reg=%#x value=%#x", target, reg, value)
        if self._tx_spi_path == TX_SPI_PATH_TX_HS:
            raise RuntimeError("TX HS SPI slow path is single-lane only; use --tx-spi-lanes 1")
        # The read-enable write must go out single-lane before enabling dual mode.
        self.set_control(tx=target, tx_spi_dual_lane=False)
        self._tx_write_raw(TX_READ_ENABLE_REG, TX_READ_ENABLE_WRITE)
        self.set_control(tx_spi_dual_lane=True)
        packet = tx_dual_write_packet(reg, value)
        self._tx_spi_dual.spi_transaction(bytearray(), bytearray(packet), 0)
        self._pause()
        self.set_control(tx_spi_dual_lane=False)

    def tx_read(self, target, reg):
        if target == "broadcast":
            raise RuntimeError("Refusing TX read with broadcast target")
        logging.debug("tx-read %s reg=%#x", target, reg)
        if self._tx_spi_path == TX_SPI_PATH_TX_HS:
            self._tx_hs_write(target, TX_READ_ENABLE_REG, TX_READ_ENABLE_READ)
            value = self._tx_hs_read(target, reg)
            self._tx_hs_write(target, TX_READ_ENABLE_REG, TX_READ_ENABLE_WRITE)
            return value
        self.set_control(tx=target)
        self._tx_write_raw(TX_READ_ENABLE_REG, TX_READ_ENABLE_READ)
        value = self._tx_read_raw(reg)
        self._tx_write_raw(TX_READ_ENABLE_REG, TX_READ_ENABLE_WRITE)
        return value

    def tx_update(self, target, reg, mask, value):
        old = self.tx_read(target, reg)
        self.tx_write(target, reg, (old & ~mask) | (value & mask))

    def _afe_write_raw(self, reg, value):
        self._spi.spi_transaction([reg & 0xFF], [(value >> 8) & 0xFF, value & 0xFF], 0)
        self._pause()

    def _afe_read_raw(self, reg):
        response = self._spi.spi_transaction([reg & 0xFF], [], 2)
        logging.debug("spi response: %s", bytes(response).hex())
        if len(response) < 3:
            raise RuntimeError(f"short SPI read response: {bytes(response).hex()}")
        self._pause()
        return (response[1] << 8) | response[2]

    def _tx_write_raw(self, reg, value):
        # Payload bytes, not command bytes: command bytes only ever clock out
        # on SDATA_0, so this keeps single- and dual-lane writes symmetric.
        self._tx_spi.spi_transaction(bytearray(), bytearray(tx_write_packet(reg, value)), 0)
        self._pause()

    def _tx_read_raw(self, reg):
        reg &= 0x3FF
        command = [(reg >> 4) & 0x3F, (reg & 0xF) << 4, 0x00, 0x00, 0x00,
                   (reg >> 8) & 0x03, reg & 0xFF]
        response = self._tx_spi.spi_transaction(bytearray(command), bytearray(), 4)
        logging.debug("spi response: %s", bytes(response).hex())
        payload = response[len(command):]
        if len(payload) != 4:
            raise RuntimeError(f"TX read returned {len(payload)} data bytes, expected 4: "
                               f"{bytes(response).hex()}")
        self._pause()
        return (payload[0] << 24) | (payload[1] << 16) | (payload[2] << 8) | payload[3]

    def _tx_hs_write(self, target, reg, value):
        mask = self._tx_hs_write_mask(target)
        reg &= 0x3FF
        value &= 0xFFFF_FFFF
        self._tx_hs_configure()
        if mask & TX_HS_SPI_CTRL_WRITE_MASK_TX1:
            self.write32(TX_HS_SPI_TX1_ADDR, reg)
            self.write32(TX_HS_SPI_TX1_DATA, value)
        if mask & TX_HS_SPI_CTRL_WRITE_MASK_TX2:
            self.write32(TX_HS_SPI_TX2_ADDR, reg)
            self.write32(TX_HS_SPI_TX2_DATA, value)
        command = (TX_HS_SPI_CTRL_START |
                   (TX_HS_SPI_CTRL_OP_WRITE << 1) |
                   (mask << 4))
        self.write32(TX_HS_SPI_CTRL, command)
        self._tx_hs_wait_done()
        self.write32(TX_HS_SPI_CTRL, 0)
        self._pause()

    def _tx_hs_read(self, target, reg):
        read_target = self._tx_hs_read_target(target)
        reg &= 0x3FF
        self._tx_hs_configure()
        if read_target == TX_HS_SPI_CTRL_TARGET_TX1:
            self.write32(TX_HS_SPI_TX1_ADDR, reg)
        else:
            self.write32(TX_HS_SPI_TX2_ADDR, reg)
        command = (TX_HS_SPI_CTRL_START |
                   (TX_HS_SPI_CTRL_OP_READ << 1) |
                   (read_target << 8))
        self.write32(TX_HS_SPI_CTRL, command)
        self._tx_hs_wait_done()
        value = self.read32(TX_HS_SPI_READ_DATA)
        self.write32(TX_HS_SPI_CTRL, 0)
        self._pause()
        return value

    def _tx_hs_configure(self):
        if self._tx_hs_cfg_written:
            return
        logging.debug("tx-hs-spi cfg half_period_cycles=%d", self._tx_hs_half_period_cycles)
        self.write32(TX_HS_SPI_CFG, self._tx_hs_half_period_cycles)
        self._tx_hs_cfg_written = True

    def _tx_hs_wait_done(self):
        deadline = time.monotonic() + TX_HS_SPI_TIMEOUT_S
        while True:
            status = self.read32(TX_HS_SPI_STATUS)
            if status & TX_HS_SPI_STATUS_ERROR:
                raise RuntimeError(f"TX HS SPI command fault: status={status:#010x}")
            if (status & TX_HS_SPI_STATUS_DONE) and not (status & TX_HS_SPI_STATUS_BUSY):
                return status
            if time.monotonic() >= deadline:
                raise TimeoutError(f"TX HS SPI command timed out: status={status:#010x}")
            time.sleep(0.0001)

    def _tx_hs_write_mask(self, target):
        if target == "broadcast":
            return TX_HS_SPI_CTRL_WRITE_MASK_TX1 | TX_HS_SPI_CTRL_WRITE_MASK_TX2
        if target == "tx1":
            return TX_HS_SPI_CTRL_WRITE_MASK_TX1
        if target == "tx2":
            return TX_HS_SPI_CTRL_WRITE_MASK_TX2
        raise ValueError(f"unknown TX target: {target!r}")

    def _tx_hs_read_target(self, target):
        if target == "tx1":
            return TX_HS_SPI_CTRL_TARGET_TX1
        if target == "tx2":
            return TX_HS_SPI_CTRL_TARGET_TX2
        raise RuntimeError(f"Refusing TX HS SPI read with target {target!r}")

    def _pause(self):
        if self._spi_delay_s:
            time.sleep(self._spi_delay_s)


def _tx_packet_reg_value(packet):
    reg = ((packet[0] & 0x3F) << 4) | (packet[1] >> 4)
    value = (((packet[1] & 0xF) << 28) | (packet[2] << 20) | (packet[3] << 12)
             | (packet[4] << 4) | (packet[5] >> 4))
    return reg, value


def _mux_name(values, code):
    return next((name for name, value in values.items() if value == code), f"unknown({code})")


def _decode_tx_hs_event(kind, address, value):
    if not (TX_HS_SPI_BASE <= address <= TX_HS_SPI_READ_DATA):
        return None
    if address == TX_HS_SPI_CTRL:
        if kind == "wr32" and (value & TX_HS_SPI_CTRL_START):
            op = (value >> 1) & 0x3
            mask = (value >> 4) & 0x3
            target = (value >> 8) & 0x3
            if op == TX_HS_SPI_CTRL_OP_WRITE:
                return f"tx-hs-start write mask={mask:#x}"
            if op == TX_HS_SPI_CTRL_OP_READ:
                return f"tx-hs-start read target={target:#x}"
        if kind == "wr32" and value == 0:
            return "tx-hs-clear-start"
    names = {
        TX_HS_SPI_CFG: "cfg",
        TX_HS_SPI_TX1_ADDR: "tx1_addr",
        TX_HS_SPI_TX1_DATA: "tx1_data",
        TX_HS_SPI_TX2_ADDR: "tx2_addr",
        TX_HS_SPI_TX2_DATA: "tx2_data",
        TX_HS_SPI_STATUS: "status",
        TX_HS_SPI_READ_DATA: "read_data",
    }
    name = names.get(address, f"reg{address - TX_HS_SPI_BASE:#x}")
    arrow = "<=" if kind == "wr32" else "=>"
    return f"tx-hs-{kind} {name} {arrow} {value:#010x}"


def decode_event(event):
    """Render one Trace event as readable text (SPI packets decoded to reg/value)."""
    kind = event[0]
    if kind in ("wr32", "rd32"):
        _, address, value = event
        arrow = "<=" if kind == "wr32" else "=>"
        txhs = _decode_tx_hs_event(kind, address, value)
        if txhs is not None:
            return txhs
        return f"{kind} {address:#010x} {arrow} {value:#010x}"
    _, bus, cmd, data, read_count = event
    if bus == TX_SPI_BUS and len(data) == 6:
        reg, value = _tx_packet_reg_value(data)
        return f"tx-write reg {reg:#05x} <= {value:#010x}"
    if bus == TX_SPI_BUS and len(data) == 12:
        reg, value = _tx_packet_reg_value(tx_dual_packet_lane0(data))
        return f"tx-write-dual reg {reg:#05x} <= {value:#010x}"
    if bus == TX_SPI_BUS and len(cmd) == 7:
        reg = ((cmd[5] & 0x03) << 8) | cmd[6]
        return f"tx-read reg {reg:#05x}"
    if bus == RX_SPI_BUS and len(data) == 2:
        return f"afe-write reg {cmd[0]:#04x} <= {(data[0] << 8) | data[1]:#06x}"
    if bus == RX_SPI_BUS and read_count:
        return f"afe-read reg {cmd[0]:#04x}"
    return f"spi{bus} cmd={bytes(cmd).hex()} data={bytes(data).hex()}"
