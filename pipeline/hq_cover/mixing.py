"""Float-domain full-track mixing with no singing-boundary decisions or source bypass."""
import hashlib
import json
import math
from pathlib import Path
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly
from .policy import PIPELINE,SINGING_BOUNDARY_POLICY,ORIGINAL_SOURCE_BYPASS
from . import config

SR=48000

def load48(path):
    a,sr=sf.read(str(path),dtype='float32',always_2d=True)
    if sr!=SR:
        g=math.gcd(sr,SR);a=resample_poly(a,SR//g,sr//g,axis=0,window=('kaiser',10.0))
    if not np.isfinite(a).all():raise ValueError('Non-finite input audio')
    return a


def true_peak(y):
    peak=0.
    for start in range(0,len(y),SR*10):
        a=max(0,start-256);b=min(len(y),start+SR*10+256)
        peak=max(peak,float(np.max(np.abs(resample_poly(y[a:b],4,1,axis=0)))))
    return peak


def mix_instrumental_and_converted(inst,converted,vocal_gain):
    return inst+converted[:,None]*vocal_gain


def mix_cover(source_path,vocal_path,inst_path,converted_path,outdir,mode='full'):
    outdir=Path(outdir);outdir.mkdir(parents=True,exist_ok=False)
    source=load48(source_path);ov=load48(vocal_path).mean(axis=1)
    inst=load48(inst_path);cv=load48(converted_path).mean(axis=1)
    n=min(len(source),len(inst),len(ov),len(cv))
    if max(len(source),len(inst),len(ov),len(cv))-n>SR*.02:raise ValueError('Stem duration mismatch')
    source,inst,ov,cv=source[:n],inst[:n],ov[:n],cv[:n]
    if source.shape[1]!=2 or inst.shape[1]!=2:raise ValueError('Stereo source/instrumental required')
    # Full-track numeric gain only: no VAD, lyric, RMS-window or singing-frame classification.
    original_energy=float(np.mean(ov.astype(np.float64)**2));converted_energy=float(np.mean(cv.astype(np.float64)**2))
    if not math.isfinite(original_energy) or not math.isfinite(converted_energy) or converted_energy<=1e-15:raise ValueError('Converted vocal energy is invalid')
    vg=float(np.sqrt(max(original_energy,1e-15)/converted_energy))
    if not math.isfinite(vg) or vg<=0:raise ValueError('Converted vocal gain is invalid')
    mixed=mix_instrumental_and_converted(inst,cv,vg)
    dry=cv*vg
    start=0
    if mode=='chorus':
        # Choose a complete energetic 36s vocal region; full song remains in upstream artifacts.
        seconds=36;block=SR;blocks=n//block
        energies=np.array([np.mean((dry[i*block:(i+1)*block]).astype(np.float64)**2) for i in range(blocks)])
        if len(energies)<seconds:raise ValueError('Song too short for 36-second preview')
        rolling=np.convolve(energies,np.ones(seconds),mode='valid')
        start=int(np.argmax(rolling));stop=min(n,(start+seconds)*SR)
        mixed=mixed[start*SR:stop].copy();dry=dry[start*SR:stop].copy()
        fade=min(round(.04*SR),len(dry)//4)
        mixed[:fade]*=np.linspace(0,1,fade)[:,None];mixed[-fade:]*=np.linspace(1,0,fade)[:,None]
        dry[:fade]*=np.linspace(0,1,fade);dry[-fade:]*=np.linspace(1,0,fade)
    elif mode!='full':raise ValueError('mode must be full or chorus')
    unscaled=outdir/'cover_master_32float.wav';sf.write(unscaled,mixed,SR,subtype='FLOAT')
    gain=min(1.,10**(-1.2/20)/max(true_peak(mixed),true_peak(dry[:,None]),1e-12))
    cover=outdir/('cover_%s_24bit.wav'%config.VOICE_ID);vocal=outdir/('vocal_%s_24bit.wav'%config.VOICE_ID)
    sf.write(cover,mixed*gain,SR,subtype='PCM_24');sf.write(vocal,dry*gain,SR,subtype='PCM_24')
    checks=[]
    for path in [cover,vocal]:
        a,sr=sf.read(path,dtype='float32',always_2d=True);peak=true_peak(a)
        item={'path':str(path),'duration_seconds':len(a)/sr,'sample_rate':sr,'channels':a.shape[1],'subtype':sf.info(path).subtype,'true_peak_4x_dbfs':float(20*np.log10(peak+1e-15)),'nonfinite':int(np.sum(~np.isfinite(a))),'clipped_samples':int(np.sum(np.abs(a)>=1)),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        if item['nonfinite'] or item['clipped_samples'] or peak>=1:raise ValueError('Output quality check failed')
        checks.append(item)
    report={'quality_passed':True,'pipeline':PIPELINE,'mode':mode,'source_start_seconds':start,'voice_id':config.VOICE_ID,'vocal_gain_db':20*math.log10(vg),'common_gain_db':20*math.log10(gain),'original_vocal_blend':0,'singing_boundary_policy':SINGING_BOUNDARY_POLICY,'original_source_bypass':ORIGINAL_SOURCE_BYPASS,'files':checks,'cover':str(cover),'vocal':str(vocal),'float_master':str(unscaled),'subjective_listening_verified':False,'limitations':'No singing-boundary inference or original-source intro/outro bypass is applied; instruments leaking into the separated vocal stem may be transformed by RVC. Does not certify zero source-vocal leakage or subjective singing quality. QQ delivery may be transcoded.'}
    (outdir/'quality.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report
