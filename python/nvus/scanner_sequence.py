"""Immutable acquisition metadata for reconstruction and recording."""
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Waveform:
    frequency_hz: float = 5e6
    cycles: int = 2
    quiet: bool = False


@dataclass(frozen=True)
class Transmit:
    waveform: Waveform
    delays_s: tuple
    aperture: tuple = (True,) * 128
    invert: bool = False

    def __post_init__(self):
        object.__setattr__(self, 'delays_s', tuple(self.delays_s))
        object.__setattr__(self, 'aperture', tuple(self.aperture))


@dataclass(frozen=True)
class Receive:
    aperture: str = 'odd'
    start_us: float = .4
    samples: int = 2704


@dataclass(frozen=True)
class Event:
    transmit: Transmit
    receive: Receive
    interval_us: float = 400.
    event_id: int | None = None
    geometry_id: int = 0
    pair_id: int | None = None


@dataclass(frozen=True)
class BoardConfig:
    sample_rate_hz: float = 50e6
    sync_clock_hz: float = 200e6
    tx_delay_us: float = 10.1
    capture_margin_us: float = 20.
    receive_attenuation_code: int = 23
    receive_source_ohms: int | None = None
    receive_dtgc_start_db: float | None = None
    receive_dtgc_start_us: float = 10.
    receive_dtgc_ramp_us: float = 40.
    analog_lpf_mhz: int = 15
    reported_voltage_v: float | None = None
    reported_voltage_at: str | None = None


@dataclass(frozen=True)
class PreparedWaveform:
    start_word: int
    words: tuple
    frequency_hz: float
    cycles: int
    quiet: bool
    invert: bool


@dataclass(frozen=True)
class PreparedEvent:
    request: Event
    event_id: int
    delays_half_cycles: tuple
    actual_frequency_hz: float
    samples: int
    start_us: float
    rx_side: str
    element_indices: tuple
    interval_ns: int
    waveform_start_word: int
    packet: bytes = field(repr=False)


@dataclass(frozen=True)
class PreparedSequence:
    id: str
    config_id: str
    events: tuple
    waveforms: tuple
    board: BoardConfig
    strategy: str
    duration_s: float

    def manifest(self):
        data = asdict(self)
        for event in data['events']:
            event['packet'] = event['packet'].hex()
        return data
