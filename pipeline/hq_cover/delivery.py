"""QQ operator delivery via inherited console credentials, verified record readback.
No hard-coded token, guessed target, master modification, or silent song truncation.
Explicit 320kbps MP3 is used only for QQ transport; the PCM24 master is retained.
Only called after quality checks by jobs submitted from qq_sing's authenticated bridge.
"""
import contextlib,hashlib,json,math,os,time,uuid
from pathlib import Path
import numpy as np
import requests
import soundfile as sf
from .policy import PIPELINE,SINGING_BOUNDARY_POLICY,ORIGINAL_SOURCE_BYPASS

class DeliveryHeld(RuntimeError):pass

class DeliveryUncertain(DeliveryHeld):
    """Request may remain queued in the bridge even if the HTTP client timed out."""
    pass


def file_hash(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(8*1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def snapshot_verified(cover,expected,workdir):
    """Exclusive-send snapshot; bind each later request to this exact audio."""
    p=Path(workdir)/'qq_send_snapshot.wav'
    try:
        with Path(cover).open('rb') as inp,p.open('xb') as out:
            for block in iter(lambda:inp.read(8*1024*1024),b''):out.write(block)
            out.flush();os.fsync(out.fileno())
    except FileExistsError:raise DeliveryHeld('发送快照已存在；停止避免重复发送') from None
    if file_hash(p)!=expected:raise DeliveryHeld('发送快照与质量报告不一致')
    return p


def require_quality(quality):
    if quality.get('quality_passed') is not True:raise DeliveryHeld('质量未通过，拒绝发送')
    if quality.get('pipeline')!=PIPELINE or quality.get('singing_boundary_policy')!=SINGING_BOUNDARY_POLICY or quality.get('original_source_bypass') is not ORIGINAL_SOURCE_BYPASS:
        raise DeliveryHeld('旧边界策略、未知流水线或来源旁路成品禁止发送；必须使用当前全曲无边界流水线重新制作')


def prepare_wav_parts(cover,outdir,max_bytes=19*1024*1024,max_seconds=295):
    info=sf.info(cover)
    if info.format!='WAV' or info.subtype!='PCM_24':raise DeliveryHeld('只允许已验证24-bit WAV交付')
    if max_bytes<1024*1024 or max_seconds<5:raise DeliveryHeld('桥接音频限制过低，保留母带等待处理')
    # Keep bit depth/rate/stereo; partition instead of lowering quality or dropping the ending.
    by_size=(max_bytes-4096)//(info.channels*3)
    frames_per=min(by_size,int(max_seconds*info.samplerate))
    if info.frames<=frames_per:return [Path(cover)]
    outdir=Path(outdir);outdir.mkdir(parents=True,exist_ok=False)
    parts=[];frame_counts=[]
    with sf.SoundFile(cover) as inp:
        k=0
        while inp.tell()<info.frames:
            data=inp.read(frames_per,dtype='int32',always_2d=True)
            p=outdir/f'part_{k+1:02d}.wav';sf.write(p,data,info.samplerate,subtype='PCM_24')
            if p.stat().st_size>max_bytes:raise DeliveryHeld('WAV分段仍超限')
            with sf.SoundFile(p) as verify:
                decoded=verify.read(dtype='int32',always_2d=True)
                if not np.array_equal(decoded,data):raise DeliveryHeld('WAV分段样本不一致')
            parts.append(p);frame_counts.append(len(data));k+=1
    if sum(frame_counts)!=info.frames:raise DeliveryHeld('WAV分段丢失样本，拒绝发送')
    return parts


@contextlib.contextmanager
def delivery_lock(root):
    """Serialize HQ delivery only; other operator clients still share bridge quota."""
    import msvcrt
    p=Path(root)/'delivery.lock';f=p.open('a+b');f.seek(0,2)
    if not f.tell():f.write(b'0');f.flush()
    f.seek(0)
    try:msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:
        f.close();raise DeliveryHeld('另一个高质量任务正在交付，暂不发起本任务发送') from None
    try:yield
    finally:
        f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1);f.close()


def send_verified(cover,target,quality,workdir,checkpoint=None,probe_only=False,relay_grant=None):
    require_quality(quality)
    workdir=Path(workdir)
    if probe_only:
        # A dry-run must not consume the one-shot claim for a later real delivery.
        workdir=workdir/('probe-'+uuid.uuid4().hex)
        workdir.mkdir(parents=True,exist_ok=False)
    with delivery_lock(Path(__file__).resolve().parent):
        if not probe_only:
            try:
                with (workdir/'delivery-attempt.json').open('x',encoding='utf-8') as f:
                    json.dump({'target':target,'attempted_at':time.time(),'replay_allowed':False},f)
                    f.flush();os.fsync(f.fileno())
            except FileExistsError:raise DeliveryHeld('本任务已进入发送流程，禁止自动再次发送') from None
        relay_kwargs={'relay_grant':relay_grant} if relay_grant is not None else {}
        return _send_verified(cover,target,quality,workdir,checkpoint,probe_only,**relay_kwargs)


def _send_verified(cover,target,quality,workdir,checkpoint=None,probe_only=False,relay_grant=None):
    require_quality(quality)
    expected=next((x.get('sha256') for x in quality.get('files',[]) if Path(x.get('path',''))==Path(cover)),None)
    if not expected or file_hash(cover)!=expected:
        raise DeliveryHeld('待发送文件与质量报告哈希不一致，拒绝发送')
    import re
    if not re.fullmatch(r'(group|private):[1-9][0-9]{4,19}',str(target)):raise DeliveryHeld('目标格式无效，不能猜测会话')
    token=os.environ.get('QQ_CONSOLE_TOKEN','')
    if not token:raise DeliveryHeld('缺少桥接继承的控制台凭据；不从配置或旧脚本提取令牌')
    try:port=int(os.environ.get('QQ_CONSOLE_PORT','3100'))
    except ValueError:raise DeliveryHeld('控制台端口无效')
    if not 1<=port<=65535:raise DeliveryHeld('控制台端口无效')
    session=requests.Session();session.trust_env=False
    url=f'http://127.0.0.1:{port}/api/voice/send'
    expected_files={};delivery_ids={}
    def receipt_query(binding):
        # Read-only recovery. A missing receipt does NOT prove a timed-out POST was unsent.
        payload=dict(binding)
        if relay_grant is not None:payload['relayGrant']=relay_grant
        for attempt in range(3):
            if attempt:time.sleep((1,3)[attempt-1])
            try:
                r=session.post(url.rsplit('/',1)[0]+'/delivery-status',headers={'X-Console-Token':token},json=payload,timeout=(5,15),allow_redirects=False)
                body=r.json();record=body.get('receipt') if isinstance(body,dict) else None
                if r.ok and body.get('ok') is True and isinstance(record,dict):
                    if record.get('target')!=target or record.get('sha256')!=binding['sha256'] or record.get('deliveryId')!=binding['deliveryId']:
                        raise DeliveryUncertain('交付回执身份不匹配，停止并禁止重发')
                    if record.get('status')=='verified' and record.get('messageId'):
                        return {'ok':True,'verified':True,'messageId':record['messageId'],'recovered_by_receipt':True}
            except DeliveryUncertain:raise
            except Exception:pass
        raise DeliveryUncertain('QQ发送结果不确定：已查询本次交付回执但未确认，禁止自动重发。')
    def request(path,dry):
        want=expected_files.get(str(path))
        if not want or file_hash(path)!=want:raise DeliveryHeld('预检后音频发生变化，停止发送')
        binding={'deliveryId':delivery_ids.setdefault(str(path),'hq-'+uuid.uuid4().hex),'target':target,'sha256':want}
        payload={'target':target,'file':str(path),'dryRun':dry,'verify':True,'readback':False,'normalize':False,'volume':1}
        if relay_grant is not None:payload['relayGrant']=relay_grant
        if not dry:payload.update(binding)
        for attempt in range(3):
            if attempt:time.sleep((1,3)[attempt-1])
            if file_hash(path)!=want:raise DeliveryHeld('重试前文件哈希改变，停止发送')
            try:
                r=session.post(url,headers={'X-Console-Token':token},json=payload,timeout=(10,180),allow_redirects=False)
                body=r.json()
            except requests.exceptions.ConnectTimeout:
                # requests ConnectTimeout establishes the connection never completed.
                if attempt<2:continue
                if dry:raise DeliveryHeld('QQ预检连接失败，已有限重试，未发送') from None
                raise DeliveryHeld('QQ发送连接未建立，有限重试后仍失败；未发送') from None
            except Exception:
                if dry:
                    if attempt<2:continue
                    raise DeliveryHeld('QQ预检请求失败；已有限重试，没有发起语音发送') from None
                return receipt_query(binding)
            if r.ok and isinstance(body,dict) and body.get('ok') is True:
                if dry or (body.get('verified') is True and body.get('messageId')):return body
                return receipt_query(binding)
            if dry:
                if getattr(r,'status_code',0) in (408,502,503,504) and attempt<2:continue
                raise DeliveryHeld('QQ预检未通过，未发起语音发送')
            # A typed bridge rejection before any send is definite; permission/limits
            # still stop, never retry around them. Everything else is readback-only.
            if isinstance(body,dict) and body.get('deliveryState')=='not_started':
                raise DeliveryHeld('桥接在发送前拒绝本次交付，请检查权限、配额或文件；未发起发送')
            return receipt_query(binding)
    cover=snapshot_verified(cover,expected,workdir)
    # Small lossless probe validates target/authorization and returns live limits without sending.
    p=Path(workdir)/'qq_preflight.wav'
    if p.exists():raise DeliveryHeld('发送工作目录重复，停止避免误重发')
    with sf.SoundFile(cover) as a:data=a.read(min(a.frames,a.samplerate),dtype='int32',always_2d=True);sr=a.samplerate
    with p.open('xb') as f:sf.write(f,data,sr,format='WAV',subtype='PCM_24')
    expected_files[str(p)]=file_hash(p)
    probe=request(p,True);limits=probe.get('limits') or {}
    max_bytes=min(int(limits.get('maxBytes',20*1024*1024))-4096,19*1024*1024)
    max_sec=min(float(limits.get('maxSeconds',300))-1,295)
    from .transport import prepare_transport_parts,TransportError
    try:
        parts,transport=prepare_transport_parts(cover,Path(workdir)/'qq_mp3_transport',max_bytes,max_sec)
    except TransportError as e:
        raise DeliveryHeld(str(e)) from None
    expected_files.update({str(part):file_hash(part) for part in parts})
    for part in parts:delivery_ids.setdefault(str(part),'hq-'+uuid.uuid4().hex)
    manifest={'source_sha256':expected,'target':target,'transport':transport,'parts':[{'path':str(p),'sha256':expected_files[str(p)],'delivery_id':delivery_ids[str(p)]} for p in parts]}
    with (Path(workdir)/'qq-send-manifest.json').open('x',encoding='utf-8') as f:json.dump(manifest,f,ensure_ascii=False,indent=2)
    for part in parts:request(part,True)
    report={'target':target,'parts':[str(p) for p in parts],'limits':limits,'dry_run_only':probe_only,'sent':[],'verified':False,'transport':transport,'master_sha256':expected,'master_preserved':True,'platform_note':transport['notice']}
    if probe_only:return report
    # Durable send-attempt marker before each side effect. Never auto-resend uncertain sends.
    per_minute=int(limits.get('maxPerMinute',2));per_hour=int(limits.get('maxPerHour',10))
    gap=max(1.,61/max(1,per_minute)) if per_minute>0 else 1.
    sent_times=[]
    for idx,part in enumerate(parts):
        if idx:
            sleep=max(0,gap-(time.monotonic()-sent_times[-1]))
            if per_hour>0 and idx>=per_hour:sleep=max(sleep,3601-(time.monotonic()-sent_times[-per_hour]))
            if sleep:time.sleep(sleep)
        event={'phase':'sending','part':idx+1,'total':len(parts),'attempted_at':time.time(),'target':target,'transport_sha256':expected_files[str(part)],'transport_format':'MP3 320kbps','master_sha256':expected,'delivery_id':delivery_ids[str(part)]}
        with (Path(workdir)/'transport-send-journal.jsonl').open('a',encoding='utf-8') as f:
            f.write(json.dumps(event,ensure_ascii=False)+'\n');f.flush();os.fsync(f.fileno())
        if checkpoint:checkpoint(event)
        response=request(part,False);sent_times.append(time.monotonic())
        item={'part':idx+1,'message_id':response.get('messageId'),'verified':response.get('verified') is True}
        report['sent'].append(item)
        if checkpoint:checkpoint({'phase':'part_sent','part':idx+1,'result':item})
        if not item['message_id'] or not item['verified']:raise DeliveryUncertain('QQ已受理但未回读确认语音；停止后续发送，不自动重发')
    report['verified']=len(report['sent'])==len(parts) and all(x['verified'] for x in report['sent'])
    return report
