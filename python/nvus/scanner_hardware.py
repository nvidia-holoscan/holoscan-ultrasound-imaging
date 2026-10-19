# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Idle-to-native hardware handoff."""
import logging
import math
from collections.abc import Mapping
from numbers import Integral

from . import fpga, tx
from .afe import AFE_READBACK_MASKS, LPF_REG


def _integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, Integral) or not low <= value <= high:
        raise ValueError(f'{name} must be an integer in [{low}, {high}]')
    return int(value)


def native_hsb_config(prepared, metadata, afe_snapshot, tx_snapshot, *, ibv_name,
                         commissioning=False, gpu_ordinal=0, rx_pages=1024):
    """Create a native SDK owner after idle bring-up; this function never fires.

    Snapshots are expected values, not proof of current state. The native adapter
    rereads both chips before executing. Metadata must already describe sensor0.
    A fresh idle initialization must establish update sequence zero. This backend
    is single-run: after stop, create a new owner following fresh idle bring-up.
    """
    from . import _scanner as native
    if not getattr(native, 'sdk_available', False):
        raise RuntimeError('Native Hololink SDK transport is not compiled')
    if commissioning is not True:
        raise ValueError('Explicit commissioning=True is required; native rig equivalence is unverified')
    config = native.HololinkTransportConfig()
    integers, strings, byte_vectors = {}, {}, {}
    for key, value in dict(metadata).items():
        if isinstance(value, bool):
            raise ValueError('Enumeration metadata booleans are not supported')
        if isinstance(value, Integral):
            integers[str(key)] = int(value)
        elif isinstance(value, str):
            strings[str(key)] = value
        elif isinstance(value, (bytes, bytearray)):
            byte_vectors[str(key)] = list(value)
        else:
            # pybind's VectorUInt8 implements the sequence protocol, but is not
            # bytes and need not register with collections.abc.Sequence.
            if isinstance(value, Mapping) or not (hasattr(value, '__len__') and hasattr(value, '__getitem__')):
                raise ValueError(f'Unsupported enumeration metadata value for {key}')
            copied = []
            for index in range(len(value)):
                byte = value[index]
                if isinstance(byte, bool) or not isinstance(byte, Integral) or not 0 <= byte <= 255:
                    raise ValueError(f'Enumeration metadata {key} must contain integer bytes in [0, 255]')
                copied.append(int(byte))
            byte_vectors[str(key)] = copied
    config.metadata_integers, config.metadata_strings, config.metadata_bytes = integers, strings, byte_vectors
    config.ibv_name, config.config_id = ibv_name, prepared.config_id
    config.gpu_ordinal, config.rx_pages = gpu_ordinal, rx_pages
    config.tx_delay_ticks = round(prepared.board.tx_delay_us * prepared.board.sync_clock_hz * 1e-6)
    config.wakeup_ticks = max(0, config.tx_delay_ticks - 20)
    config.delay_start_word = int.from_bytes(prepared.events[0].packet[164:166], 'little') - 0x80
    config.commissioning = True

    def expectations(snapshot, prefix, required, bits):
        output = []
        for chip in (1, 2):
            values = {int(k, 0) if isinstance(k, str) else int(k): int(v)
                      for k, v in snapshot[f'{prefix}{chip}'].items()}
            if not set(required) <= values.keys():
                raise ValueError(f'{prefix}{chip} snapshot lacks required register expectations')
            if prefix == 'rx' and values[LPF_REG] & 3 != {15: 0, 10: 1}[prepared.board.analog_lpf_mhz]:
                raise ValueError('AFE filter snapshot differs from BoardConfig')
            if prefix == 'tx':
                if prepared.board.receive_dtgc_start_db is None:
                    if (values[0x1f] & 0x3f != prepared.board.receive_attenuation_code
                            or not values[0x1f] & tx.TX_FIXED_ATTEN_ENABLE_MASK):
                        raise ValueError('Receive attenuation snapshot differs from BoardConfig')
                else:
                    profile = tx.receive_dtgc_profile(
                        prepared.board.receive_dtgc_start_db,
                        prepared.board.receive_dtgc_start_us,
                        prepared.board.receive_dtgc_ramp_us,
                        prepared.board.sync_clock_hz)
                    if values[0x1f] & tx.TX_FIXED_ATTEN_ENABLE_MASK:
                        raise ValueError('Receive DTGC is not enabled')
                    if any(values[reg] != value
                           for reg, value in profile['registers'].items()):
                        raise ValueError('Receive DTGC snapshot differs from BoardConfig')
                if (prepared.board.receive_source_ohms is not None
                        and values[0x54] & tx.TX_LOOPBACK_MODE_RSOURCE_MASK
                        != tx.TX_MODE_RSOURCE_VALUES[prepared.board.receive_source_ohms]):
                    raise ValueError('Receive source impedance snapshot differs from BoardConfig')
                if values[0xb] & 0xf001 or values[0x6e] & 8:
                    raise ValueError('Waveforms require pattern mode and undivided TX clocks')
            for address, value in values.items():
                expectation = native.RegisterExpectation()
                expectation.chip, expectation.address, expectation.value = chip, address, value
                expectation.mask = (AFE_READBACK_MASKS.get(address, (1 << bits) - 1)
                                    if prefix == 'rx' else (1 << bits) - 1)
                output.append(expectation)
        return output

    config.afe_expected = expectations(afe_snapshot, 'rx', (LPF_REG, 0x95), 16)
    tx_required = (0x1f, 0x54, 0xb, 0x6e)
    if prepared.board.receive_dtgc_start_db is not None:
        tx_required += tx.TX_DTGC_REGS
    config.tx_expected = expectations(tx_snapshot, 'tx', tx_required, 32)
    return config


def configure_native_hsb(evm, channel_metadata, ibv_name, prepared, **options):
    """Read idle board expectations for a native owner; never arm or fire.

    First initialize/configure ``evm`` with the existing idle bring-up API and
    the prepared board's LPF/attenuation. No imaging process may own the rig.
    This helper reads both chips, clears read-enable and restores SPI/control
    state even on error. Close the Python Hololink connection after it returns,
    then hand the config to the native ``make_hololink_hardware`` factory.
    Native program/verify repeats readback before allowing a transmit.
    """
    from .bus import Bus, BOARD_CTRL_REG, TX_HS_SPI_CFG
    if evm.bus._tx_spi_path != 'tx-hs':
        raise ValueError('Native handoff requires the verified tx-hs idle bring-up path')
    bus = evm.bus
    board_control, spi_config = bus.read32(BOARD_CTRL_REG), bus.read32(TX_HS_SPI_CFG)
    # Separate slow readback bus avoids changing the caller's cached SPI setup.
    read_bus = Bus(bus._hololink, tx_spi_path='tx-hs', tx_spi_prescaler=15)
    afe, tx_values = {}, {}
    try:
        for chip in ('rx1', 'rx2'):
            try:
                afe[chip] = {reg: read_bus.afe_read(chip, reg) for reg in (LPF_REG, 0x95)}
            finally:
                read_bus.afe_write(chip, 0, 0)
        for chip in ('tx1', 'tx2'):
            try:
                registers = (0x1f, 0x54, 0xb, 0x6e)
                if prepared.board.receive_dtgc_start_db is not None:
                    registers += tx.TX_DTGC_REGS
                tx_values[chip] = {reg: read_bus.tx_read(chip, reg)
                                   for reg in registers}
            finally:
                read_bus.tx_write(chip, 0, 0)
    finally:
        try:
            bus.write32(TX_HS_SPI_CFG, spi_config)
        finally:
            bus.write32(BOARD_CTRL_REG, board_control)
    return native_hsb_config(prepared, channel_metadata, afe, tx_values,
                             ibv_name=ibv_name, **options)


def _finish_idle_operation(action):
    """Keep the primary failure when disconnect/read-enable cleanup also fails."""
    import sys
    original = sys.exc_info()[1]
    try:
        action()
    except BaseException as cleanup:
        if original is None:
            raise
        message = f'Idle hardware cleanup failed: {cleanup!r}'
        if callable(getattr(original, 'add_note', None)):
            original.add_note(message)
        else:  # Python 3.10 preserves inspectable notes on the same exception.
            original.__notes__ = [*getattr(original, '__notes__', ()), message]


def _afe_filter(afe, chip, value=None):
    """Read or write/verify LPF register, always leaving read mode disabled."""
    try:
        if value is not None:
            afe.write(chip, LPF_REG, value)
        actual = afe.read(chip, LPF_REG)
        if value is not None and actual != value:
            raise RuntimeError(f'{chip} LPF readback failed')
        return actual
    finally:
        _finish_idle_operation(lambda: afe.write(chip, 0, 0))


class _HsbSession:
    """Idle bring-up and post-init analog rollback; the caller owns native Run.

    Call close only after the native run has stopped and wait has completed.
    Reset/init changes are deliberately not presented as a restored prior rig
    state. No method arms or fires a transmitter.
    """
    def __init__(self, address, board, ibv_name=None, gpu_ordinal=0,
                 rx_pages=1024, commissioning=False):
        if not isinstance(address, str) or not address.strip():
            raise ValueError('address must be a nonempty string')
        if ibv_name is not None and (not isinstance(ibv_name, str) or not ibv_name):
            raise ValueError('ibv_name must be a nonempty string')
        _integer(gpu_ordinal, 'GPU ordinal', 0, 2**31-1)
        _integer(rx_pages, 'receive pages', 2, 1024)
        if commissioning is not True:
            raise ValueError('live session requires commissioning=True')
        if (board.sample_rate_hz != 50e6 or board.sync_clock_hz != 200e6
                or board.tx_delay_us != 10.1):
            raise ValueError('idle handoff supports 50 MSPS, 200 MHz sync and 10.1 us TX delay')
        if board.analog_lpf_mhz not in (10, 15):
            raise ValueError('analog LPF must be 10 or 15 MHz')
        _integer(board.receive_attenuation_code, 'receive attenuation', 0, 48)
        if board.receive_source_ohms not in (None, 100, 200, 400, 800):
            raise ValueError('receive source impedance must be 100, 200, 400, or 800 ohms')
        if board.receive_dtgc_start_db is not None:
            tx.receive_dtgc_profile(board.receive_dtgc_start_db,
                                    board.receive_dtgc_start_us,
                                    board.receive_dtgc_ramp_us,
                                    board.sync_clock_hz)
        if not math.isfinite(board.capture_margin_us) or board.capture_margin_us < 0:
            raise ValueError('capture margin must be finite and nonnegative')
        self.address, self.board, self.ibv_name = address, board, ibv_name
        self.gpu_ordinal, self.rx_pages = gpu_ordinal, rx_pages
        self.readback = {}
        self.restoration = {'restored': False, 'scope': 'post_init_analog_overrides'}
        self._control = self._evm = None
        self._filters, self._attenuation = {}, {}
        self._mutated = self._opened = self._closed = False

    def open(self, prepared):
        if self._opened or self._closed:
            raise RuntimeError('hardware session is single-use')
        if prepared.board != self.board or not prepared.events:
            raise ValueError('prepared sequence differs from session board')
        self._opened = True
        import hololink
        import nvus
        from .bus import TX_HS_SPI_CFG
        try:
            nvus.enumeration_strategy()
            metadata = hololink.Enumerator.find_channel(channel_ip=self.address)
            hololink.DataChannel.use_sensor(metadata, 0)
            control = self._control = hololink.DataChannel(metadata).hololink()
            try:
                control.start()
                fpga_identity = dict(
                    hsb_ip_version=int(control.get_hsb_ip_version()),
                    datecode=int(control.get_fpga_date()))
                control.reset()
                event = prepared.events[0]
                evm = self._evm = nvus.Evm(control, nvus.EvmConfig(
                    eval_board_mode='probe', fs=self.board.sample_rate_hz,
                    sync_clk_hz=self.board.sync_clock_hz, wakeup_s=10e-6,
                    tx_delay_s=self.board.tx_delay_us*1e-6, tx_pulse_s=.4e-6,
                    capture_delay_s=(self.board.tx_delay_us+event.start_us)*1e-6,
                    interval_margin_s=self.board.capture_margin_us*1e-6,
                    tx_spi_path='tx-hs', tx_spi_prescaler=0,
                    rx_bf_sync_with_tx=True, sync_phase_align_enable=True,
                    spi_delay_s=0., trsw_settle_s=.0002, sync_capture_wait_s=.0002))
                evm.init(samples=event.samples, pri=event.interval_ns*1e-9,
                         rx_select='odd', repeat_shots=1)
                spi = evm.bus.read32(TX_HS_SPI_CFG)
                try:
                    evm.bus.write32(TX_HS_SPI_CFG, 16)
                    # Snapshot BOTH chips in BOTH chains before any override.
                    self._attenuation = {
                        'attenuation': {
                            chip: evm.tx._bus.tx_read(chip, tx.TX_LOOPBACK_FIXED_TGC_REG)
                            for chip in tx.TX_TARGETS
                        },
                        'mode': {
                            chip: evm.tx._bus.tx_read(chip, tx.TX_LOOPBACK_MODE_REG)
                            for chip in tx.TX_TARGETS
                        },
                        'dtgc': {
                            chip: {reg: evm.tx._bus.tx_read(chip, reg)
                                   for reg in tx.TX_DTGC_REGS}
                            for chip in tx.TX_TARGETS
                        },
                    }
                    if any(not value & tx.TX_FIXED_ATTEN_ENABLE_MASK
                           for value in self._attenuation['attenuation'].values()):
                        raise RuntimeError('fixed receive attenuation is not enabled')
                    self._filters = {chip: _afe_filter(evm.afe, chip)
                                     for chip in ('rx1', 'rx2')}
                    self._mutated = True
                    if self.board.receive_dtgc_start_db is None:
                        attenuation = evm.tx.configure_receive_attenuation(
                            self.board.receive_attenuation_code,
                            self.board.receive_source_ohms)
                    else:
                        attenuation = evm.tx.configure_receive_dtgc(
                            self.board.receive_dtgc_start_db,
                            self.board.receive_dtgc_start_us,
                            self.board.receive_dtgc_ramp_us,
                            self.board.receive_source_ohms,
                            self.board.sync_clock_hz)
                    filters = {}
                    for chip, before in self._filters.items():
                        value = (before & ~3) | {15: 0, 10: 1}[self.board.analog_lpf_mhz]
                        filters[chip] = {'before': before, 'after': _afe_filter(evm.afe, chip, value)}
                finally:
                    _finish_idle_operation(lambda: evm.bus.write32(TX_HS_SPI_CFG, spi))
                if self.ibv_name is None:
                    from . import host
                    self.ibv_name = host.default_ibv_name()
                config = configure_native_hsb(evm, metadata, self.ibv_name, prepared,
                    gpu_ordinal=self.gpu_ordinal, rx_pages=self.rx_pages, commissioning=True)
                expected = lambda entries: [dict(chip=e.chip, address=e.address, value=e.value, mask=e.mask) for e in entries]
                self.readback = dict(ibv_name=self.ibv_name, fpga=fpga_identity,
                    attenuation=attenuation,
                    filters=filters, afe_expected=expected(config.afe_expected),
                    tx_expected=expected(config.tx_expected))
                return config
            finally:
                _finish_idle_operation(control.stop)
        except BaseException:
            _finish_idle_operation(self.close)
            raise

    def close(self):
        if self._closed:
            return self.restoration
        self._closed = True
        errors = []
        restored_tx = {}
        def attempt(action):
            try:
                action()
            except BaseException as exc:
                errors.append(repr(exc))
        if self._mutated:
            from .bus import TX_HS_SPI_CFG
            control, evm = self._control, self._evm
            spi = None
            try:
                control.start()
                control.write_uint32(0x40000004, 0)
                control.write_uint32(fpga.TX_UPDATE_CTRL, 0)
                if (control.read_uint32(fpga.SYNC_CTRL + fpga.SYNC_CFG)
                        or control.read_uint32(fpga.TX_UPDATE_CTRL)
                        or control.read_uint32(0x70000080) & 1):
                    raise RuntimeError('TX/update ingress is not confirmed quiescent; analog restoration skipped')
                spi = evm.bus.read32(TX_HS_SPI_CFG)
                evm.bus.write32(TX_HS_SPI_CFG, 16)
                logging.info('Restoring TX receive path registers: %s', self._attenuation)
                def restore_tx():
                    actual = evm.tx.restore_receive_attenuation(self._attenuation)
                    restored_tx.update({'0x1f': actual['attenuation'],
                                        '0x54': actual['mode']})
                    if 'dtgc' in actual:
                        restored_tx['dtgc'] = actual['dtgc']
                attempt(restore_tx)
                for chip, value in self._filters.items():
                    attempt(lambda: _afe_filter(evm.afe, chip, value))
            except BaseException as exc:
                errors.append(repr(exc))
            finally:
                if spi is not None:
                    attempt(lambda: evm.bus.write32(TX_HS_SPI_CFG, spi))
                attempt(control.stop)
        self.restoration = dict(restored=not errors, scope='post_init_analog_overrides',
                                changed=self._mutated, errors=errors,
                                tx_registers=restored_tx)
        if errors:
            raise RuntimeError('; '.join(errors))
        return self.restoration
