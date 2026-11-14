"""Verified lossless -> UVR MelBand -> full-track RVC -> WAV QA.

User policy intentionally disables singing-boundary inference, vocal profiles, intro/outro
source bypass and automatic gate review. Every sample in the separated vocal stem is sent
through RVC; this can transform instruments that leaked into that stem.
"""
import hashlib,json,os,re
from pathlib import Path
from .source import acquire_lossless
from .mixing import mix_cover
from .policy import PIPELINE,SINGING_BOUNDARY_POLICY,ORIGINAL_SOURCE_BYPASS
from . import config
from . import artifact_cache

ROOT=Path(__file__).resolve().parent
# 两个阶段的解释器来自 config.py（环境变量 SING_UVR_PY / SING_RVC_PY 或 config.local.json）
UVR_PY=config.UVR_PY
RVC_PY=config.RVC_PY


def _require_interpreters():
    for name,p in (('SING_UVR_PY',UVR_PY),('SING_RVC_PY',RVC_PY)):
        if p is None or not Path(p).is_file():
            raise RuntimeError(
                f'缺少 {name} 指向的 Python 解释器（当前 {p}）。'
                '请先按 docs/INSTALL.md 装好 UVR 与 RVC，'
                '或在 pipeline/config.local.json 里写 uvrPy / rvcPy。')


def run_stage(python,script,args,workdir):
    env=dict(os.environ,PYTHONIOENCODING='utf-8',PYTHONUNBUFFERED='1',OMP_NUM_THREADS='1',MKL_NUM_THREADS='1',RVC_F0_SMOOTH='1')
    env.pop('QQ_CONSOLE_TOKEN',None);env.pop('QQ_CONSOLE_PORT',None)
    # 阶段脚本里有 `from hq_cover import config`，而它们是被当成**独立脚本**启动的
    # （不是 -m），此时 sys.path[0] 是 hq_cover/ 自己，找不到 hq_cover 包本身。
    # 不注入 PYTHONPATH 的话：uvr_stage / rvc_stage 一启动就
    # ModuleNotFoundError: No module named 'hq_cover'。
    # 异步路径下这个错误尤其隐蔽——任务停在 status=queued，只有 worker 日志里一行 traceback。
    pkg_parent=str(ROOT.parent)
    existing=env.get('PYTHONPATH','')
    parts=[pkg_parent]+[p for p in existing.split(os.pathsep) if p]
    env['PYTHONPATH']=os.pathsep.join(dict.fromkeys(parts))
    log=Path(workdir)/(Path(script).stem+'.log')
    from .processes import run_contained
    r=run_contained([str(python),str(script),*map(str,args)],ROOT.parent,env,log,6*3600)
    (Path(workdir)/(Path(script).stem+'-process.json')).write_text(json.dumps(r,indent=2),encoding='utf-8')
    if r['returncode'] or r['timed_out'] or not r['tree_empty']:
        raise RuntimeError(f'{Path(script).stem}失败或超时；阶段进程已清理，未发送。')


def require_audio_hash(path,expected):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(8*1024*1024),b''):h.update(block)
    if not expected or h.hexdigest()!=expected:raise RuntimeError('音频来源或阶段文件被改动，质量链路校验失败')
    return expected


def verify_stage(directory,kind,input_path,input_sha):
    directory=Path(directory)
    report=json.loads((directory/'stage.json').read_text(encoding='utf-8'))
    if report.get('stage')!=kind or report.get('status')!='success' or report.get('technical_checks_passed') is not True:
        raise RuntimeError('阶段退出成功但质量报告未通过')
    item=report.get('input',{})
    if Path(item.get('path','')).resolve()!=Path(input_path).resolve() or item.get('sha256')!=input_sha:
        raise RuntimeError('阶段输入与已验证音源不一致')
    require_audio_hash(input_path,input_sha)
    expected_names=('vocals','instrumental') if kind=='uvr' else ('vocals',)
    for name in expected_names:
        output=report.get('outputs',{}).get(name,{})
        p=directory/(name+'.wav')
        if Path(output.get('path','')).resolve()!=p.resolve():raise RuntimeError('阶段输出路径不符')
        require_audio_hash(p,output.get('sha256'))
    return report


def process(query,workdir,mode='full',track_id=None,stage_callback=None,allow_variant=False,video_url=None):
    if type(allow_variant) is not bool:raise ValueError('Variant policy must be boolean')
    if video_url is not None:
        if track_id is not None or allow_variant:raise ValueError('Video source conflicts with track/variant selection')
        from .bilibili_source import normalize_video_url,acquire_bilibili,verify_bilibili_source
        video_url=normalize_video_url(video_url)
    elif re.search(r'(?i)(?:https?://|www\.|b23\.tv/|bilibili\.com/)',str(query)):
        raise ValueError('URLs require explicit video_url source selection')
    # 在动手之前就校验 run 目录：否则 GPU 跑完三分钟才在 register() 抛出
    # "Path escapes its permitted root"，白等一场。
    workdir=artifact_cache.ensure_run_dir(workdir)
    workdir.mkdir(parents=True,exist_ok=False)
    def stage(s):
        print(f'HQ_STAGE={s}',flush=True)
        if stage_callback:stage_callback(s)
    if video_url is not None:
        stage('verify_bilibili_audio')
        source=acquire_bilibili(video_url,workdir/'source')
        if (source.get('source_kind')!='bilibili' or source.get('original_lossless') is not False
                or source.get('user_selected_source') is not True):raise RuntimeError('Video provenance is invalid')
        verify_bilibili_source(source,source['source_path'])
    else:
        stage('verify_lossless_source')
        variant_kwargs={'allow_variant':True} if allow_variant is True else {}
        source=acquire_lossless(query,workdir/'source',track_id=track_id,**variant_kwargs)
    source_kind='bilibili' if video_url is not None else 'netease'
    require_audio_hash(source['source_path'],source['source_sha256'])
    from .artifact_cache import lookup,register,fingerprint
    stage('checking_finished_cover_cache')
    cached=lookup(source,mode,workdir)
    if cached is not None:
        (workdir/'result.json').write_text(json.dumps(cached,ensure_ascii=False,indent=2),encoding='utf-8')
        stage('cached_cover_ready')
        return cached
    render_fingerprint=fingerprint()
    _require_interpreters()
    stage('uvr_melband_separation')
    uvr=workdir/'uvr'
    run_stage(UVR_PY,ROOT/'uvr_stage.py',['--input',source['source_path'],'--out',uvr,'--segment',1101,'--overlap',4],workdir)
    uvr_report=verify_stage(uvr,'uvr',source['source_path'],source['source_sha256'])
    stage('rvc_breeze_conversion')
    rvc=workdir/'rvc'
    # Full separated vocal stem is converted, including intro/outro leakage.
    run_stage(RVC_PY,ROOT/'rvc_stage.py',['--input',uvr/'vocals.wav','--out',rvc],workdir)
    rvc_report=verify_stage(rvc,'rvc',uvr/'vocals.wav',uvr_report['outputs']['vocals']['sha256'])
    if (rvc_report.get('voice_id')!=config.VOICE_ID
            or rvc_report.get('singing_boundary_policy')!=SINGING_BOUNDARY_POLICY
            or rvc_report.get('original_source_bypass') is not ORIGINAL_SOURCE_BYPASS
            or rvc_report.get('time_domain_gating') is not False):
        raise RuntimeError('换声音色或全曲无边界策略与任务不一致')
    stage('float_mix_and_wav_quality_checks')
    verify_stage(uvr,'uvr',source['source_path'],source['source_sha256'])
    quality=mix_cover(source['source_path'],uvr/'vocals.wav',uvr/'instrumental.wav',rvc/'vocals.wav',workdir/'mix',mode)
    quality.update(source_kind=source_kind,original_lossless=source_kind=='netease')
    # Explicitly mark lossy-origin video audio; a PCM/WAV master does not upgrade its source.
    if source_kind=='bilibili':quality['source_notice']='User-selected Bilibili audio; original may be lossy. WAV master is not proof of lossless original.'
    quality_path=workdir/'mix/quality.json'
    if quality_path.exists():quality_path.write_text(json.dumps(quality,ensure_ascii=False,indent=2),encoding='utf-8')
    if (quality.get('quality_passed') is not True or quality.get('singing_boundary_policy')!=SINGING_BOUNDARY_POLICY
            or quality.get('original_source_bypass') is not ORIGINAL_SOURCE_BYPASS):raise RuntimeError('质量检查或无边界策略未通过，拒绝发送')
    verify_stage(uvr,'uvr',source['source_path'],source['source_sha256'])
    verify_stage(rvc,'rvc',uvr/'vocals.wav',uvr_report['outputs']['vocals']['sha256'])
    result={'source':source,'source_kind':source_kind,'original_lossless':source_kind=='netease','singing_boundary_policy':SINGING_BOUNDARY_POLICY,'quality':quality,'cover':quality['cover'],'vocal':quality['vocal'],'mode':mode,'voice_id':config.VOICE_ID,'pipeline':PIPELINE,'cache_hit':False,'render_fingerprint':render_fingerprint}
    if fingerprint()!=render_fingerprint:raise RuntimeError('制作期间音色或处理配置改变，拒绝交付旧参数产物')
    stage('registering_finished_cover')
    register(result,workdir)
    (workdir/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    stage('ready_for_delivery')
    return result
