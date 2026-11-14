"""Immutable verified cache for the boundary-free full-track RVC policy.

Schema 2 deliberately rejects every historical gate-bound rendition.  No GPU, network,
target, credential or send state is stored.  Public API: fingerprint(), register(),
lookup(), import_run().
"""
import hashlib,json,os,re,shutil,time,uuid
from pathlib import Path
import numpy as np
import soundfile as sf
from . import rvc_stage,uvr_stage
from .policy import PIPELINE,SINGING_BOUNDARY_POLICY as BOUNDARY_POLICY,CACHE_SCHEMA as SCHEMA
from . import config

HQ=Path(__file__).resolve().parent
RUNS=config.DATA_ROOT/'runs'
CACHE=config.DATA_ROOT/'artifact_cache'
VOICE=config.VOICE_ID
CODE_NAMES=('policy.py','pipeline.py','rvc_stage.py','rvc_pipeline_float.py','uvr_stage.py','mixing.py')
_HASH_MEMORY={}

class CacheRejected(ValueError):pass

def _require(ok,message):
    if not ok:raise CacheRejected(message)

def _hash(path,stat_cache=False):
    p=Path(path).resolve(strict=True);s=p.stat();key=(str(p),s.st_size,s.st_mtime_ns,s.st_ctime_ns)
    if stat_cache and key in _HASH_MEMORY:return _HASH_MEMORY[key]
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(8*1024*1024),b''):h.update(b)
    after=p.stat();_require((s.st_size,s.st_mtime_ns,s.st_ctime_ns)==(after.st_size,after.st_mtime_ns,after.st_ctime_ns),'File changed during hashing')
    value=h.hexdigest()
    if stat_cache:_HASH_MEMORY[key]=value
    return value

def _digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def _inside(path,root,exists=True):
    p=Path(path).resolve(strict=exists);r=Path(root).resolve(strict=True)
    _require(p!=r and p.is_relative_to(r),'Path escapes its permitted root')
    return p


def ensure_run_dir(path):
    """把用户给的 --out 变成合法的 run 目录，并在**开工前**校验。

    为什么要这个：register() 要求 run 目录必须在 <dataRoot>/runs/ 下面，否则最后一步
    抛 CacheRejected: Path escapes its permitted root —— 那时 GPU 已经跑了三分钟。
    典型触发场景是 README 里写的 `--out D:/test-1`（不在 runs/ 下）。

    规则（按顺序，第一个命中就返回）：
      · 在 RUNS 下         → 原样用（但目录必须不存在，防止覆盖）
      · 在 RUNS 下但已存在 → 报错，不覆盖
      · 不在 RUNS 下       → 抛 ValueError，并在信息里直接给出正确的写法
    """
    raw = Path(path).expanduser()
    target = raw if raw.is_absolute() else (Path.cwd() / raw)
    resolved = target.resolve()
    # 运行时读模块级 RUNS（而不是闭包捕获），这样测试里 mock RUNS 能生效。
    runs_root = RUNS.resolve()

    if resolved != runs_root and resolved.is_relative_to(runs_root):
        if resolved.exists():
            raise ValueError(
                f'输出目录已存在，拒绝覆盖：{resolved}\n'
                '换一个新的目录名，例如在末尾加 -2。')
        return resolved

    # 不在 runs/ 下：给出可照抄的建议路径
    suggested = runs_root / resolved.name
    raise ValueError(
        f'输出目录必须在 {runs_root} 下面，当前给的是：{resolved}\n'
        f'改用：{suggested}\n'
        '（artifact_cache 只接受 runs/ 下的目录，这是为了缓存与清理边界清晰。'
        '你可以设 SING_DATA_ROOT 把 dataRoot 换到别处。）')

def _read(path):
    _require(Path(path).stat().st_size<=16*1024*1024,'Metadata too large')
    value=json.loads(Path(path).read_text(encoding='utf-8'))
    _require(isinstance(value,dict),'Metadata must be an object')
    return value

def fingerprint():
    """Hash the exact boundary-free render policy and installed neural assets."""
    assets={}
    pairs=list(rvc_stage.ASSET_HASHES.items())
    for p in (uvr_stage.WEIGHT,uvr_stage.CONFIG):
        if p is not None:
            pairs.append((p,config.expected_hash(p)))
    for path,expected in pairs:
        actual=_hash(path,True)
        # 没有 models.lock.json 时 expected 为空：只记录实际哈希，不做比对。
        if expected:
            _require(actual==expected,'Installed model/config differs from verified asset')
        assets[Path(path).name]=actual
    body={'schema':SCHEMA,'pipeline':PIPELINE,'voice_id':VOICE,'singing_boundary_policy':BOUNDARY_POLICY,
          'assets':assets,'code_hashes':{name:_hash(HQ/name) for name in CODE_NAMES},
          'settings':{'uvr_segment':1101,'uvr_overlap':4,'rvc_chunk_seconds':rvc_stage.CHUNK_SECONDS,
                      'rvc_overlap_seconds':rvc_stage.OVERLAP_SECONDS,'output_rate':48000,
                      'output_subtype':'PCM_24','original_source_bypass':False,'full_vocal_stem_rvc':True}}
    return dict(body,digest=_digest(body))

def _identity(source,mode):
    _require(mode in ('full','chorus'),'Unknown rendition mode')
    sha=source.get('source_sha256','')
    _require(isinstance(sha,str) and len(sha)==64 and all(c in '0123456789abcdef' for c in sha),'Invalid source SHA')
    kind=source.get('source_kind','netease')
    base={'source_sha256':sha,'source_kind':kind,'voice_id':VOICE,'mode':mode,'pipeline':PIPELINE,'singing_boundary_policy':BOUNDARY_POLICY}
    if kind=='bilibili':
        video=source.get('video') or {}
        _require(isinstance(video,dict) and isinstance(video.get('bvid'),str) and re.fullmatch(r'BV[0-9A-Za-z]{10}',video['bvid']) is not None,'Invalid Bilibili identity')
        _require(type(video.get('cid')) is int and video['cid']>0 and type(video.get('page')) is int and video['page']>0,'Invalid video CID/page')
        _require(source.get('original_lossless') is False and source.get('user_selected_source') is True,'Missing explicit video source provenance')
        return dict(base,bvid=video['bvid'],cid=video['cid'],page=video['page'])
    _require(kind=='netease','Unsupported source kind')
    track=(source.get('track') or {}).get('id')
    _require(type(track) is int and track>0,'Missing official track id')
    return dict(base,track_id=track)

def _audio(path,channels,expected=None,duration=None):
    before=_hash(path)
    if expected is not None:_require(before==expected,'Audio hash mismatch')
    info=sf.info(path)
    _require(info.format=='WAV' and info.subtype=='PCM_24' and info.channels==channels and info.samplerate==48000 and info.frames>0,'Invalid cached PCM24 format')
    if duration is not None:_require(abs(info.duration-duration)<=.02,'Audio duration mismatch')
    count=0;peak=0.
    with sf.SoundFile(path) as f:
        while True:
            a=f.read(48000*10,dtype='float64',always_2d=True)
            if not len(a):break
            _require(np.isfinite(a).all(),'Nonfinite cached audio');peak=max(peak,float(np.max(np.abs(a))));count+=len(a)
    _require(count==info.frames and peak<1.,'Audio decode incomplete or clipped')
    _require(_hash(path)==before,'Audio changed while decoding')
    return {'sha256':before,'frames':count,'duration_seconds':info.duration,'sample_rate':info.samplerate,'channels':channels,'subtype':'PCM_24','peak':peak,'nonfinite':0,'clipped_samples':0}

def _verified_source_duration(source,path=None):
    p=Path(path or source.get('source_path','')).resolve(strict=True)
    _require(_hash(p)==source.get('source_sha256'),'Verified source hash mismatch')
    if source.get('source_kind')=='bilibili':
        from .bilibili_source import verify_bilibili_source
        verified=verify_bilibili_source(source,p)
        duration=verified.get('duration_seconds') if isinstance(verified,dict) else None
        _require(type(duration) in (int,float) and np.isfinite(duration) and duration>0,'Invalid verified video duration')
        return float(duration)
    _require(source.get('source_kind','netease')=='netease','Unsupported source kind')
    with sf.SoundFile(p) as f:
        _require(f.format=='FLAC' and f.frames>0 and f.samplerate>0,'Source is not a complete FLAC')
        duration=len(f)/f.samplerate
        while True:
            block=f.read(480000,dtype='float32',always_2d=True)
            if not len(block):break
            _require(np.isfinite(block).all(),'Invalid source decode')
    return duration

def _stage_chain(run,source,fp):
    reports={}
    for kind in ('uvr','rvc'):
        d=run/kind;r=_read(_inside(d/'stage.json',run))
        _require(r.get('stage')==kind and r.get('status')=='success' and r.get('technical_checks_passed') is True,'Incomplete stage')
        inp=r.get('input',{});p=_inside(inp.get('path',''),run)
        _require(_hash(p)==inp.get('sha256'),'Stage input hash mismatch')
        names=('vocals','instrumental') if kind=='uvr' else ('vocals',)
        for name in names:
            item=r.get('outputs',{}).get(name,{})
            p=_inside(item.get('path',''),run)
            _require(p==(d/(name+'.wav')).resolve() and _hash(p)==item.get('sha256'),'Stage output hash mismatch')
        reports[kind]=r
    u,r=reports['uvr'],reports['rvc']
    _require(u['input']['sha256']==source['source_sha256'] and Path(u['input']['path']).resolve()==Path(source['source_path']).resolve(),'Source/UVR identity mismatch')
    _require(r['input']['sha256']==u['outputs']['vocals']['sha256'] and Path(r['input']['path']).resolve()==(run/'uvr/vocals.wav').resolve(),'UVR/RVC link mismatch')
    _require(r.get('voice_id')==VOICE and r.get('singing_boundary_policy')==BOUNDARY_POLICY and r.get('original_source_bypass') is False and r.get('time_domain_gating') is False,'RVC is not boundary-free')
    _require(r.get('private_pipeline_sha256')==fp['code_hashes']['rvc_pipeline_float.py'],'RVC pipeline fingerprint differs')
    for p,expected in rvc_stage.ASSET_HASHES.items():
        if expected:_require(r.get('assets',{}).get(p.name,{}).get('sha256')==expected,'RVC historical asset mismatch')
    # UVR 权重哈希只有在 models.lock.json 记录了才校验；否则只要求与当前装的一致。
    _uvr_w=fp['assets'].get(Path(uvr_stage.WEIGHT).name) if uvr_stage.WEIGHT else None
    _uvr_c=fp['assets'].get(Path(uvr_stage.CONFIG).name) if uvr_stage.CONFIG else None
    if _uvr_w or _uvr_c:
        _require(u.get('model',{}).get('sha256')==_uvr_w
                 and u.get('model',{}).get('config_sha256')==_uvr_c,'UVR historical model mismatch')
    for k,v in {'segment':1101,'overlap':4,'batch_size':1,'cpu_threads':1,'tf32':False,'dtype':'float32','normalization':False,'post_denoise':False}.items():_require(u.get('settings',{}).get(k)==v,'UVR historical settings differ')
    _require(r.get('schema_version')==2,'RVC stage schema is not boundary-free v2')
    for k,v in {'rms_mix':0,'index_rate':.75,'protect':.33,'pitch_shift_semitones':0,'f0_method':'rmvpe','f0_smooth':True,'chunk_seconds':18.,'overlap_seconds':.3,'time_domain_gating':False,'whole_chunk_passthrough':False,'boundary_detection':False,'integer_audio_intermediate':False,'original_vocal_blend':0,'cpu_threads':1,'tf32':False}.items():_require(r.get('settings',{}).get(k)==v,'RVC historical settings differ')
    return reports

def register(result,run_dir):
    run=_inside(run_dir,RUNS);_require(run.is_dir(),'Run is not a directory')
    fp=fingerprint();_require(result.get('render_fingerprint')==fp,'Missing or changed pre-render fingerprint')
    source=result.get('source',{});identity=_identity(source,result.get('mode'))
    _require(result.get('voice_id')==VOICE and result.get('pipeline')==PIPELINE and result.get('singing_boundary_policy')==BOUNDARY_POLICY,'Wrong result policy')
    q=result.get('quality',{})
    _require(q.get('quality_passed') is True and q.get('pipeline')==PIPELINE and q.get('voice_id')==VOICE and q.get('mode')==identity['mode'] and q.get('singing_boundary_policy')==BOUNDARY_POLICY and q.get('original_source_bypass') is False,'Quality policy mismatch')
    diskq=_read(_inside(run/'mix/quality.json',run));_require(diskq==q,'Result quality differs from persisted report')
    sp=_inside(source.get('source_path',''),run)
    if identity['source_kind']=='bilibili':
        _require(source.get('status')=='verified_bilibili_audio' and source.get('original_lossless') is False and source.get('user_selected_source') is True,'Unverified Bilibili provenance')
        _require(result.get('source_kind')=='bilibili' and result.get('original_lossless') is False and q.get('source_kind')=='bilibili' and q.get('original_lossless') is False,'Video original must never be labeled lossless')
        from .bilibili_source import verify_bilibili_source
        verify_bilibili_source(source,sp)
    else:
        _require(source.get('status')=='verified_official_lossless' and source.get('trial_restricted') is not True and source.get('transcoded') is not True,'Unverified source provenance')
        from .source import validate_lossless
        official=source.get('quality_response',{});_require(isinstance(official,dict) and official.get('level')=='lossless','Missing official lossless descriptor')
        validate_lossless(sp,source['track'],official,identity['source_sha256'])
    duration=_verified_source_duration(source,sp)
    _stage_chain(run,source,fp)
    files={};paths={}
    for name,ch in (('cover',2),('vocal',1)):
        p=_inside(result.get(name,''),run);_require(p==Path(q.get(name,'')).resolve(),'Quality result path mismatch')
        row=next((x for x in q.get('files',[]) if Path(x.get('path','')).resolve()==p),None);_require(isinstance(row,dict),'Missing quality file row')
        files[name]=_audio(p,ch,row.get('sha256'),duration if identity['mode']=='full' else 36.)
        _require(row.get('channels')==ch and abs(float(row.get('duration_seconds',-1))-files[name]['duration_seconds'])<.001,'Quality audio metadata differs')
        paths[name]=p
    _require(files['cover']['frames']==files['vocal']['frames'],'Cover/vocal duration mismatch')
    _require(fingerprint()==fp,'Rendering policy changed during registration')
    CACHE.mkdir(parents=True,exist_ok=True)
    entry=CACHE/(_digest(dict(identity,fingerprint=fp['digest']))+'-'+uuid.uuid4().hex);entry.mkdir(exist_ok=False)
    for name,ch in (('cover',2),('vocal',1)):
        target=entry/(name+'.wav')
        with paths[name].open('rb') as a,target.open('xb') as b:shutil.copyfileobj(a,b,8*1024*1024)
        _audio(target,ch,files[name]['sha256'],files[name]['duration_seconds'])
    keys=('source_start_seconds','vocal_gain_db','common_gain_db','original_vocal_blend','singing_boundary_policy','subjective_listening_verified','limitations')
    metadata={'schema':SCHEMA,'provenance':'boundary_free_pre_render_fingerprint','identity':identity,'fingerprint':fp,
              'singing_boundary_policy':BOUNDARY_POLICY,'source_kind':identity['source_kind'],'original_lossless':identity['source_kind']=='netease','source_duration_seconds':duration,'files':files,
              'created':time.time(),'quality_metrics':{k:q.get(k) for k in keys},'complete':True}
    with (entry/'metadata.json').open('x',encoding='utf-8') as f:json.dump(metadata,f,ensure_ascii=False,indent=2,allow_nan=False)
    return dict(metadata,cache_entry=entry.name)

def lookup(source,mode,workdir):
    identity=_identity(source,mode)
    if not CACHE.exists():return None
    source_path=_inside(source.get('source_path',''),RUNS);_require(_hash(source_path)==identity['source_sha256'],'Verified source changed before cache lookup')
    duration=_verified_source_duration(source,source_path);fp=fingerprint()
    work=_inside(workdir,RUNS);_require(work.is_dir(),'Current run must already exist');_require(not (work/'mix').exists(),'Refusing to overwrite current mix')
    for candidate in sorted(CACHE.glob('*/metadata.json')):
        try:
            mp=_inside(candidate,CACHE);entry=mp.parent;m=_read(mp)
            _require(m.get('schema')==SCHEMA and m.get('complete') is True and m.get('identity')==identity and m.get('fingerprint')==fp,'Cache identity mismatch')
            _require(m.get('singing_boundary_policy')==BOUNDARY_POLICY and abs(float(m.get('source_duration_seconds',-1))-duration)<=.02,'Cache boundary/source policy mismatch')
            _require(m.get('source_kind')==identity['source_kind'] and m.get('original_lossless') is (identity['source_kind']=='netease'),'Cache source provenance mismatch')
            _require(isinstance(m.get('files'),dict) and isinstance(m.get('quality_metrics'),dict),'Malformed cache sections')
            validated={}
            for name,ch in (('cover',2),('vocal',1)):
                p=_inside(entry/(name+'.wav'),entry);validated[name]=_audio(p,ch,m['files'][name]['sha256'],duration if mode=='full' else 36.)
                _require(validated[name]==m['files'][name],'Cache manifest/audio mismatch')
        except (CacheRejected,ValueError,TypeError,KeyError,AttributeError,IndexError,OverflowError,OSError,RuntimeError):continue
        _require(_hash(source_path)==identity['source_sha256'] and fingerprint()==fp,'Source or rendering policy changed during lookup')
        mix=work/'mix';mix.mkdir(exist_ok=False)
        keys=('source_start_seconds','vocal_gain_db','common_gain_db','original_vocal_blend','singing_boundary_policy','subjective_listening_verified','limitations')
        q={**{k:m['quality_metrics'].get(k) for k in keys},'quality_passed':True,'pipeline':PIPELINE,'mode':mode,'voice_id':VOICE,'original_source_bypass':False,'files':[],'cache_hit':True,'source_kind':identity['source_kind'],'original_lossless':identity['source_kind']=='netease'}
        if identity['source_kind']=='bilibili':q['source_notice']='User-selected Bilibili audio; original may be lossy. WAV master is not proof of lossless original.'
        _require(q['singing_boundary_policy']==BOUNDARY_POLICY,'Cached quality boundary policy mismatch')
        for name,ch in (('cover',2),('vocal',1)):
            target=mix/(name+'_cached_24bit.wav')
            with (entry/(name+'.wav')).open('rb') as a,target.open('xb') as b:shutil.copyfileobj(a,b,8*1024*1024)
            info=_audio(target,ch,m['files'][name]['sha256'],m['files'][name]['duration_seconds']);q[name]=str(target);q['files'].append(dict(info,path=str(target)))
        _require(_hash(source_path)==identity['source_sha256'] and fingerprint()==fp,'Source or rendering policy changed during cache copy')
        with (mix/'quality.json').open('x',encoding='utf-8') as f:json.dump(q,f,ensure_ascii=False,indent=2,allow_nan=False)
        return {'source':source,'source_kind':identity['source_kind'],'original_lossless':identity['source_kind']=='netease','mode':mode,'voice_id':VOICE,'pipeline':PIPELINE,'singing_boundary_policy':BOUNDARY_POLICY,
                'quality':q,'cover':q['cover'],'vocal':q['vocal'],'cache_hit':True,'cache_entry':entry.name,'render_fingerprint':fp}
    return None

def import_run(run_dir):
    """Only a current schema-2 run may be imported; gate-bound historical runs are rejected."""
    run=_inside(run_dir,RUNS);result=_read(_inside(run/'result.json',run))
    _require(result.get('pipeline')==PIPELINE and result.get('singing_boundary_policy')==BOUNDARY_POLICY,'Historical gate-bound runs cannot enter the boundary-free cache')
    return register(result,run)
