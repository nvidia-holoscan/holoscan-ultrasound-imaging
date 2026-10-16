"""Observation adapters for the existing reconstruction algorithms.

Processing is outside acquisition. Live centered FIR31 and offline causal FIR127
are explicit profiles; they are not interchangeable numerical references.
"""
from contextlib import nullcontext
from dataclasses import asdict, dataclass
import math
from types import SimpleNamespace

import cupy as cp
import numpy as np

from . import raw, recon, tx
from .focus import focus_profile
from .focused_recon import FocusedCompound, configure_plan


@dataclass(frozen=True)
class Reconstruction:
    kind: str = 'plane_wave'
    angles_deg: tuple = (0.,)
    positions_mm: tuple = ()
    focus_mm: float | None = None
    depth_mm: float = 40.
    c: float = 1480.
    system_delay_us: float = 0.
    onset_sample: int = 183
    f_number: float = 1.
    grid_frequency_hz: float = 5e6
    iq_profile: str = 'live_fir31'
    db_ref: float | None = None
    db_percentile: float = 99.7

    def __post_init__(self):
        object.__setattr__(self, 'angles_deg', tuple(self.angles_deg))
        object.__setattr__(self, 'positions_mm', tuple(self.positions_mm))
        if self.kind not in ('plane_wave', 'focused'):
            raise ValueError('kind must be plane_wave or focused')
        if self.iq_profile not in ('live_fir31', 'causal_fir127'):
            raise ValueError('unknown IQ profile')
        values = (self.depth_mm, self.c, self.system_delay_us, self.f_number,
                  self.grid_frequency_hz, self.db_percentile)
        if not np.isfinite(values).all() or min(self.depth_mm, self.c, self.f_number,
                                              self.grid_frequency_hz) <= 0:
            raise ValueError('invalid reconstruction dimensions or timing')
        if not isinstance(self.onset_sample, int) or self.onset_sample < 0:
            raise ValueError('onset_sample must be a nonnegative integer')
        if not 0 < self.db_percentile <= 100:
            raise ValueError('percentile must be in (0,100]')
        if self.db_ref is not None and (not math.isfinite(self.db_ref) or self.db_ref <= 0):
            raise ValueError('display reference must be positive and finite')
        geometry = self.angles_deg if self.kind == 'plane_wave' else self.positions_mm
        if not geometry or not np.isfinite(geometry).all():
            raise ValueError('finite nonempty reconstruction geometry required')
        if self.kind == 'focused' and (self.focus_mm is None or not math.isfinite(self.focus_mm)
                                      or not 1 < self.focus_mm < self.depth_mm):
            raise ValueError('focus must lie inside the reconstructed depth window')
        if self.kind == 'focused' and (len(geometry) < 2 or any(b <= a for a,b in zip(geometry,geometry[1:]))):
            raise ValueError('focused positions must be increasing, at least two')


@dataclass(frozen=True)
class Image:
    complex: object
    envelope: object
    db: object
    metadata: dict


class Processor:
    """Reuse a prepared GPU plan; return owned outputs and explicit provenance.

    IQ input maps actual event IDs to sample-major complex64 arrays (N,64).
    Causal FIR127 IQ is half-sample aligned; live FIR31 IQ is unaligned and the
    existing beamformer applies channel offsets. Pair sums use measured signed
    values: normal + inverted, without negating the inverted acquisition again.
    """
    def __init__(self, settings):
        self.settings = settings
        self._key = self._plan = self._decoder = self._focused = None
        self._reference = settings.db_ref

    def _validate(self, observation):
        events = tuple(observation.prepared.events)
        records = tuple(observation.records)
        if not getattr(observation, 'complete', True) or not events or len(records) != len(events):
            raise ValueError('incomplete observation')
        ids = [e.event_id for e in events]
        if len(set(ids)) != len(ids):
            raise ValueError('duplicate event ID')
        for event, record in zip(events, records):
            if not getattr(record, 'valid', True) or record.event_id != event.event_id:
                raise ValueError('invalid or reordered event record')
            if hasattr(record, 'samples') and record.samples != event.samples:
                raise ValueError('record sample count differs from prepared event')
            if tuple(event.element_indices) != tuple(raw.element_indices(event.rx_side)):
                raise ValueError('physical element mapping differs from reconstruction')
            if record.rx_side != {'odd':0, 'even':1}.get(event.rx_side):
                raise ValueError('receive mux does not match prepared event')
        geometries = (self.settings.angles_deg if self.settings.kind == 'plane_wave'
                      else self.settings.positions_mm)
        profiles = [tuple(tx.plane_wave_delays(math.radians(value), c=self.settings.c))
                    if self.settings.kind == 'plane_wave' else
                    tuple(focus_profile(value*1e-3,self.settings.focus_mm*1e-3,self.settings.c)[0])
                    for value in geometries]
        groups = {}
        signatures = []
        pairs = {}
        for index, event in enumerate(events):
            request = event.request
            geometry = request.geometry_id
            if not isinstance(geometry,int) or not 0 <= geometry < len(profiles):
                raise ValueError('unknown geometry ID')
            if tuple(event.delays_half_cycles) != profiles[geometry]:
                raise ValueError('achieved transmit delays differ from reconstruction geometry')
            if len(request.transmit.aperture) != 128 or not all(request.transmit.aperture):
                raise ValueError('this reconstruction requires the full transmit aperture')
            waveform = request.transmit.waveform
            signature = (event.actual_frequency_hz, waveform.cycles, waveform.quiet,
                         event.samples, event.start_us, event.interval_ns)
            signatures.append(signature)
            groups.setdefault((geometry,event.rx_side),[]).append(event)
            if request.pair_id is not None:
                pairs.setdefault(request.pair_id,[]).append(index)
        if any(e.samples <= self.settings.onset_sample for e in events):
            raise ValueError('onset must lie within acquired RF')
        if len(set(signatures)) != 1:
            raise ValueError('waveform/timing/window mismatch; separate acquisition groups required')
        for indices in pairs.values():
            if len(indices)!=2 or indices[1]!=indices[0]+1:
                raise ValueError('pulse inversion requires complete adjacent pairs')
            a,b=(events[i] for i in indices)
            if ((a.request.geometry_id,a.rx_side)!=(b.request.geometry_id,b.rx_side)
                    or a.request.transmit.invert or not b.request.transmit.invert):
                raise ValueError('pulse inversion pair geometry/mux/polarity mismatch')
        paired=bool(pairs)
        for geometry in range(len(profiles)):
            for side in ('odd','even'):
                group=groups.get((geometry,side),[])
                if len(group)!=(2 if paired else 1):
                    raise ValueError('each geometry requires both receive halves exactly once')
                if paired and (group[0].request.pair_id is None or
                               group[0].request.pair_id != group[1].request.pair_id):
                    raise ValueError('mismatched or missing pulse inversion partner')
        return events, groups, signatures[0], paired

    def plan_for(self, observation):
        events, groups, signature, paired = self._validate(observation)
        key = (observation.prepared.id, observation.prepared.config_id, signature)
        if key != self._key:
            frequency, cycles = signature[:2]
            s = self.settings
            plan = recon.make_plan(depth=s.depth_mm*1e-3,c=s.c,demod_freq=frequency,
                                   f_number=s.f_number,rx_half='both',grid_frequency_hz=s.grid_frequency_hz)
            recon.set_plan_frequency(plan,frequency,burst_cycles=cycles)
            plan.system_delay_s=s.system_delay_us*1e-6
            self._focused=None
            sequence=None
            if s.kind=='focused':
                sequence=SimpleNamespace(foci=tuple((x*1e-3,s.focus_mm*1e-3) for x in s.positions_mm),sound_speed=s.c)
                configure_plan(plan,sequence)
            self._decoder=None
            if s.iq_profile=='live_fir31':
                from .live_recon import Beamformer
                self._decoder=Beamformer(plan)
            if sequence is not None:
                self._focused=FocusedCompound(plan,sequence,
                    centered_fir=s.iq_profile=='live_fir31',
                    element_delays=self._decoder._el_ds if self._decoder else None)
            self._plan,self._key=plan,key
            self._reference=s.db_ref
        return self._plan

    def process_iq(self, observation, iq_by_event):
        observation.wait_ready()
        return self._process_iq_ready(observation, iq_by_event)

    def _process_iq_ready(self, observation, iq_by_event):
        events,groups,_,paired=self._validate(observation)
        plan=self.plan_for(observation)
        if set(iq_by_event)!={e.event_id for e in events}:
            raise ValueError('IQ event IDs do not match observation')
        data={}
        for eid,value in iq_by_event.items():
            value=cp.asarray(value)
            if value.shape!=(plan.n_keep,64) or value.dtype!=cp.complex64:
                raise ValueError('expected complex64 IQ shaped (retained samples,64)')
            data[eid]=value
        geometry=self.settings.angles_deg if self.settings.kind=='plane_wave' else self.settings.positions_mm
        channels=[]
        for i in range(len(geometry)):
            halves=[]
            for side in ('odd','even'):
                group=groups[i,side]
                value=data[group[0].event_id]
                if paired:
                    value=value+data[group[1].event_id]
                halves.append(value)
            channels.append(cp.ascontiguousarray(cp.concatenate(halves,axis=1).T))
        stacked=cp.stack(channels)
        if self._focused is not None:
            compound=self._focused(stacked).copy()
        elif self._decoder is not None:
            compound=self._decoder.compound_image(stacked,tuple(map(math.radians,geometry))).copy()
        else:
            compound=cp.zeros((plan.nz,plan.nx),cp.complex64)
            for channel,angle in zip(channels,geometry):
                compound+=recon.beamform(channel.T,plan,math.radians(angle))
            compound/=len(geometry)
        envelope=cp.abs(compound)
        if self._reference is None:
            self._reference=cp.maximum(recon.norm_reference(envelope,self.settings.db_percentile),
                                       cp.float32(1e-12))
        db=recon.to_db(envelope,self._reference)
        metadata=dict(prepared_id=observation.prepared.id,event_ids=[e.event_id for e in events],
                      reconstruction=asdict(self.settings),iq_profile=self.settings.iq_profile,
                      pulse_inversion='sum_before_envelope' if paired else None,
                      display_reference=float(self._reference),display_range_db=50.,
                      focused=self._focused.settings.copy() if self._focused else None)
        return Image(compound,envelope,db,metadata)

    def process(self, observation):
        if hasattr(observation, 'gpu_rf'):
            lease = observation.gpu_rf()
        else:
            observation.wait_ready()
            lease = nullcontext(observation.rf)
        with lease as pages:
            return self._process_rf_ready(observation, pages)

    def _process_rf_ready(self, observation, raw_pages):
        events,groups,_,paired=self._validate(observation)
        plan=self.plan_for(observation)
        if len(raw_pages)!=len(events):
            raise ValueError('missing RF buffers')
        pages={}
        for event,raw in zip(events,raw_pages):
            host=np.frombuffer(raw,dtype=np.uint8) if isinstance(raw,(bytes,bytearray,memoryview)) else raw
            page=cp.asarray(host)
            if page.dtype != cp.uint8:
                raise ValueError('RF buffers must contain unsigned wire bytes')
            if page.ndim!=1 or page.size != event.samples*96:
                raise ValueError('RF byte length differs from prepared sample count')
            pages[event.event_id]=page
        if self._decoder is None:
            iq={e.event_id:recon.to_iq(recon.unpack(pages[e.event_id]),self.settings.onset_sample,plan)
                for e in events}
        else:
            ordered=sorted(events,key=lambda e:({'odd':0,'even':1}[e.rx_side],e.request.geometry_id,e.request.transmit.invert))
            n_tx=len(ordered)//2
            channel_data=cp.zeros((n_tx,128,plan.n_keep),cp.complex64)
            self._decoder.set_pages([pages[e.event_id] for e in ordered])
            self._decoder.baseband_frame(len(ordered),self.settings.onset_sample,channel_data,n_tx)
            iq={e.event_id:channel_data[i%n_tx,0 if i<n_tx else 64:64 if i<n_tx else 128].T
                for i,e in enumerate(ordered)}
        return self._process_iq_ready(observation,iq)
