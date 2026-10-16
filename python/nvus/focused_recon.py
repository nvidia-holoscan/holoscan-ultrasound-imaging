# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Focused synthetic-aperture DAS with dynamic receive focusing.

Zea arrivals with Gaussian transmit weighting and panel-four noise normalization.
Geometry and gain tables are built once before imaging; no image-space blur.
"""
import cupy as cp
import numpy as np
from . import recon, tx
from .focus import focus_profile

KERNEL = r"""
extern "C" __global__ void focused_compound(
 const float2* iq, const float* ex, const float* xs, const float* zs,
 const float* tx_times, const float* weights, const float* gain, const float* ds, float2* out,
 int n, int ne, int nx, int nz, int nt, float fs, float fd, float invc,
 float peak, float offset, float inv2f)
{
 int ix=blockIdx.x*blockDim.x+threadIdx.x;
 int iz=blockIdx.y*blockDim.y+threadIdx.y;
 if(ix>=nx || iz>=nz) return;
 int pixel=iz*nx+ix;
 float x=xs[ix],z=zs[iz],re=0.f,im=0.f,total_weight=0.f;
 for(int t=0;t<nt;++t){
  float weight=weights[(size_t)t*nx*nz+pixel];
  if(weight==0.f) continue;
  float tx=tx_times[(size_t)t*nx*nz+pixel],tr=0.f,ti=0.f;
  for(int e=0;e<ne;++e){
   float dx=x-ex[e];
   if(fabsf(dx)>z*inv2f) continue;
   float tau=tx+sqrtf(dx*dx+z*z)*invc+ds[e];
   float s=(tau+offset)*fs;
   if(s<0.f || s>n-1) continue;
   int j=(int)floorf(s);float w=s-j;
   size_t base=((size_t)t*ne+e)*n;
   float2 a=iq[base+j],b=iq[base+min(j+1,n-1)];
   float r=a.x+w*(b.x-a.x),i=a.y+w*(b.y-a.y),sn,cs;
   sincosf(6.283185307179586f*fd*(tau+peak),&sn,&cs);
   tr+=r*cs-i*sn;ti+=r*sn+i*cs;
  }
  re+=weight*tr;im+=weight*ti;total_weight+=weight;
 }
 out[pixel]=total_weight>0.f?make_float2(re/total_weight*gain[pixel],im/total_weight*gain[pixel]):make_float2(0.f,0.f);
}
"""


def transmit_tables(xs, zs, foci, c, aperture_width, *,
                    focal_region_length=0.000296, minimum_aperture=None, frequency_hz=recon.FC):
    """Zea first/last arrival delays and Gaussian geometric TX weights.

    Reference: zea.beamform.beamformer.transmit_delays at
    9da00744dad4727ab8469cbda760115bca2923cc. Arrival extrema use the
    actual quantized element fire times. The linear blend spans a full
    ``focal_region_length`` along each beam, centered on its focus.

    Gaussian width is a geometric approximation, not a pressure estimate.
    Its full minimum width is max(lambda * TX f-number, grid spacing).
    """
    if not np.isfinite([c, aperture_width, focal_region_length, frequency_hz]).all() or (
            c <= 0 or aperture_width <= 0 or focal_region_length < 0 or frequency_hz <= 0):
        raise ValueError("invalid focused compound geometry")
    xs, zs = np.asarray(xs, dtype=float), np.asarray(zs, dtype=float)
    if xs.ndim != 1 or zs.ndim != 1 or not xs.size or not zs.size or not (
            np.isfinite(xs).all() and np.isfinite(zs).all()):
        raise ValueError("finite nonempty coordinate vectors required")
    spacing = np.max(np.diff(np.sort(np.unique(xs)))) if len(np.unique(xs)) > 1 else 0.
    if minimum_aperture is not None and (not np.isfinite(minimum_aperture) or minimum_aperture <= 0):
        raise ValueError("minimum aperture must be finite and positive")
    xx, zz = np.meshgrid(xs, zs)
    ex = (np.arange(tx.TX_PW_ELEMENTS) - (tx.TX_PW_ELEMENTS - 1) / 2) * tx.TX_PW_PITCH_M
    travel = np.hypot(xx[..., None] - ex, zz[..., None]) / c
    times, weights = [], []
    for fx, fz in foci:
        profile, _ = focus_profile(fx, fz, c)
        fire = np.asarray(profile, dtype=float) / (2 * tx.BF_CLOCK_HZ)
        arrivals = travel + fire
        first, last = arrivals.min(axis=-1), arrivals.max(axis=-1)
        length = np.hypot(fx, fz)
        dx, dz = xx - fx, zz - fz
        axial = (dx * fx + dz * fz) / length
        lateral = (dx * fz - dz * fx) / length
        if focal_region_length > 0:
            blend = np.clip(axial / focal_region_length + .5, 0., 1.)
            arrival = first * (1. - blend) + last * blend
        else:
            arrival = np.where(axial < 0., first, last)
        fnum = length / aperture_width
        floor = max(c / frequency_hz * fnum, spacing) if minimum_aperture is None else minimum_aperture
        times.append(arrival)
        width = np.hypot(axial / fnum, floor)
        weight = np.exp(-4 * np.log(2) * (lateral / width)**2)
        weight[weight < 1e-6] = 0
        weights.append(weight)
    return np.asarray(times, np.float32), np.asarray(weights, np.float32)


def configure_plan(plan, sequence):
    """Use one image column per focus; preserve depth/filter/sample geometry."""
    if sequence.foci is None:
        raise ValueError("focused reconstruction requires focus coordinates")
    depths = [z for _, z in sequence.foci]
    if min(depths) < float(plan.px_z[0]) or max(depths) > float(plan.px_z[-1]):
        raise ValueError("focus must lie inside the reconstructed depth window")
    plan.px_x = cp.asarray([x for x, _ in sequence.foci], dtype=cp.float32)
    plan.nx = len(sequence.foci)
    plan.extent = (float(plan.px_x[0])*1e3, float(plan.px_x[-1])*1e3,
                   float(plan.px_z[-1])*1e3, float(plan.px_z[0])*1e3)


class FocusedCompound:
    """Frame-end GPU DAS for aligned FFT IQ or centered-FIR live IQ."""
    def __init__(self, plan, sequence, *, focal_region_length=None,
                 minimum_aperture=None, element_delays=None, centered_fir=False):
        if sequence.foci is None:
            raise ValueError("focused transmissions required")
        self.plan = plan
        self.nt = len(sequence.foci)
        self.offset, self.phase_offset = recon.timing_offsets(plan, centered_fir=centered_fir)
        self.ds = cp.zeros(plan.el_x.size, cp.float32) if element_delays is None else element_delays
        ex = plan.tx_el_x_np
        xs, zs = cp.asnumpy(plan.px_x), cp.asnumpy(plan.px_z)
        # One pulse-echo axial resolution cell, full length (not half length).
        if focal_region_length is None:
            focal_region_length = max(float(np.max(np.diff(zs))), float(plan.t_peak) * plan.c)
        aperture_width = float(ex.max() - ex.min())
        lateral_spacing = float(np.max(np.diff(xs))) if len(xs) > 1 else 0.
        frequency_hz = float(getattr(plan, "center_freq", plan.fd))
        floors = [max(plan.c / frequency_hz * np.hypot(x,z) / aperture_width, lateral_spacing)
                  if minimum_aperture is None else minimum_aperture for x,z in sequence.foci]
        self.settings = dict(
            system_delay_s=float(getattr(plan, "system_delay_s", 0.0)),
            focal_model="zea_first_last", transmit_window="gaussian", normalization="noise",
            center_frequency_hz=frequency_hz,
            zea_reference="9da00744dad4727ab8469cbda760115bca2923cc",
            focal_region_length_m=focal_region_length,
            minimum_transmit_width_m=min(floors),
            minimum_transmit_width_max_m=max(floors),
            transmit_f_number_min=min(np.hypot(x,z)/aperture_width for x,z in sequence.foci),
            quantized_transmit_delays=True)
        times, weights = transmit_tables(xs, zs, sequence.foci, plan.c, aperture_width,
            focal_region_length=focal_region_length, minimum_aperture=minimum_aperture,
            frequency_hz=frequency_hz)
        self.times=cp.asarray(times);self.weights=cp.asarray(weights)
        self.weight_sum=weights.sum(axis=0)
        squared=(weights**2).sum(axis=0)
        self.contributors=np.divide(self.weight_sum**2, squared,
            out=np.zeros_like(squared), where=squared>0)
        rx=(np.abs(xs[None,:,None]-cp.asnumpy(plan.el_x)[None,None,:])
            <=zs[:,None,None]/(2*plan.f_number)).sum(axis=-1)
        gain=np.sqrt(np.divide(self.contributors,rx,
            out=np.zeros_like(self.contributors),where=rx>0))
        anchor=(zs[:,None]>=.017)&(zs[:,None]<.020)&(np.abs(xs[None,:])<.010)&(gain>0)
        selected=gain[anchor] if anchor.any() else gain[gain>0]
        scale=float(np.median(selected)) if selected.size else 1.
        gain/=scale
        self.settings["noise_gain_anchor"]=scale
        self.gain=cp.asarray(gain, dtype=cp.float32)
        self.kernel=cp.RawKernel(KERNEL,'focused_compound')
        self.out=cp.empty((plan.nz,plan.nx),cp.complex64)

    def __call__(self,channel_data):
        p=self.plan
        if channel_data.shape!=(self.nt,int(p.el_x.size),p.n_keep):
            raise ValueError('expected (transmit, element, sample) aligned IQ')
        self.kernel(((p.nx+15)//16,(p.nz+15)//16),(16,16),
          (channel_data,p.el_x,p.px_x,p.px_z,self.times,self.weights,self.gain,self.ds,self.out,
           np.int32(p.n_keep),np.int32(p.el_x.size),np.int32(p.nx),np.int32(p.nz),np.int32(self.nt),
           np.float32(recon.FS),np.float32(p.fd),np.float32(1/p.c),np.float32(self.phase_offset),
           np.float32(self.offset),np.float32(1/(2*p.f_number))))
        return self.out


class FocusedBeamformer:
    """Adapt focused SA to the live decoder without work on the shot path."""
    def __init__(self, decoder, sequence, *, centered_fir=True):
        self.decoder = decoder
        self.plan = decoder._plan
        self.compound = FocusedCompound(self.plan, sequence,
            element_delays=decoder._el_ds, centered_fir=centered_fir)
        self.out = self.compound.out

    def set_pages(self, pages):
        self.decoder.set_pages(pages)

    def baseband_frame(self, *args):
        self.decoder.baseband_frame(*args)

    def compound_image(self, channel_data, angles):
        return self.compound(channel_data)
