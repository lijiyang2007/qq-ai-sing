"""Compatible QQ cover_job submit/run protocol with fail-closed quality and delivery."""
import argparse
import contextlib
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
from . import config

ROOT=config.DATA_ROOT
HQ=Path(__file__).resolve().parent
JOBS=ROOT/'jobs'
PY=config.MAIN_PY
from .policy import PIPELINE


def safe_id(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]{4,64}',str(value)):raise ValueError('Invalid job id')
    return value


def save(job):
    p=JOBS/(safe_id(job['id'])+'.json')
    temp=p.with_name(p.name+'.'+uuid.uuid4().hex+'.tmp')
    with temp.open('x',encoding='utf-8') as stream:
        json.dump(job,stream,ensure_ascii=False,indent=2,allow_nan=False)
        stream.flush();os.fsync(stream.fileno())
    os.replace(temp,p)


def load(jid):return json.loads((JOBS/(safe_id(jid)+'.json')).read_text(encoding='utf-8'))


@contextlib.contextmanager
def gpu_lock():
    import msvcrt
    lock=ROOT/'gpu.lock';f=lock.open('a+b');f.seek(0,2)
    if f.tell()==0:f.write(b'0');f.flush()
    f.seek(0)
    try:msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:
        f.close();raise RuntimeError('已有高质量翻唱任务正在运行；拒绝并发占用GPU')
    try:yield
    finally:
        f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1);f.close()


def submission_binding(job):
    """Bind request identity to bridge-inherited credentials; never persist the secret."""
    token=os.environ.get('QQ_CONSOLE_TOKEN','')
    if not token:raise ValueError('Missing inherited bridge credentials')
    names=('id','song','target','mode','track_id','no_send','pipeline','voice_id','created')
    if job.get('queue_version') is not None:names+=('queue_version','queue_seq','queue_deadline','timeout')
    if 'allow_variant' in job:names+=('allow_variant',)
    if 'relay_grant' in job:names+=('relay_grant',)
    if 'video_url' in job or 'source_kind' in job:names+=('video_url','source_kind')
    fields={k:job.get(k) for k in names}
    payload=json.dumps(fields,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
    return hmac.new(token.encode('utf-8'),payload,hashlib.sha256).hexdigest()


def submit(args):
    query=' '.join(args.song.split())
    if not query or len(query)>160:raise ValueError('需要具体歌名与歌手')
    allow_variant=getattr(args,'allow_variant',False)
    if type(allow_variant) is not bool:raise ValueError('版本许可必须是布尔值')
    if allow_variant and (type(args.track_id) is not int or args.track_id<=0):raise ValueError('pro版本许可必须绑定准确歌曲ID')
    video_url=getattr(args,'video_url',None)
    if video_url is not None:
        if args.track_id is not None or allow_variant:raise ValueError('B站来源不可与歌曲ID或pro版本许可组合')
        from .bilibili_source import normalize_video_url
        video_url=normalize_video_url(video_url)
        if not isinstance(video_url,str) or not video_url:raise ValueError('B站视频地址无效')
    elif re.search(r'(?i)(?:https?://|www\.|b23\.tv/|bilibili\.com/)',query):
        raise ValueError('视频来源必须使用显式video-url入口，不能将URL塞入歌名')
    relay_grant=getattr(args,'relay_grant',None)
    if relay_grant is not None:
        if not isinstance(relay_grant,str) or len(relay_grant)>4096:raise ValueError('Invalid relay grant')
        if args.no_send or not args.target:raise ValueError('Relay grant requires a send target without --no-send')
    if not args.no_send:
        if not re.fullmatch(r'(group|private):[1-9][0-9]{4,19}',args.target or ''):raise ValueError('缺少当前授权会话目标')
        if not os.environ.get('QQ_CONSOLE_TOKEN'):raise ValueError('仅接受桥接继承凭据的发送任务；本地测试请用 --no-send')
    from . import queueing as q
    JOBS.mkdir(exist_ok=True);jid=time.strftime('%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]
    with q.mutex(JOBS):
        items=q.rows(JOBS)
        # Resume only never-started current-version queued jobs. Their exclusive supervisor lock
        # prevents duplicates; signed targets must still validate with inherited credentials.
        for old in q.waiting(items):
            if q.replay_safe(old,HQ) and (old.get('no_send') or valid_binding(old)):
                try:
                    with q.file_lock(JOBS/(safe_id(old['id'])+'.supervisor.lock')):pass
                except q.QueueBusy:continue
                spawn_supervisor(old['id'])
        if len(q.active(items))>=q.CAPACITY:raise q.QueueFull('点歌队列已满，请稍后再点；未创建新任务')
        now=time.time();seq=max((int(x.get('queue_seq',0)) for x in items),default=0)+1
        job={'id':jid,'song':query,'song_file':None,'target':args.target or 'local','mode':args.mode,'engine':'rvc','pipeline':PIPELINE,'voice_id':config.VOICE_ID,'status':'queued','created':now,'send_ok':False,'no_send':args.no_send,'track_id':args.track_id,'stage':'queued','timeout':12*3600,'queue_version':q.VERSION,'queue_seq':seq,'queue_deadline':now+q.WAIT_SECONDS}
        job['allow_variant']=allow_variant
        if relay_grant:job['relay_grant']=relay_grant
        if video_url is not None:job.update(video_url=video_url,source_kind='bilibili')
        if not args.no_send:job['submission_hmac']=submission_binding(job)
        save(job)
        try:child=spawn_supervisor(jid)
        except Exception:
            job.update(status='failed',error='排队监督进程无法启动；未发送。',stage='launch_failed',finished=time.time());save(job)
            raise RuntimeError('排队监督进程无法启动；未发送。') from None
        position=len(q.waiting(items))+1
    print(json.dumps({'ok':True,'job':jid,'mode':job['mode'],'target':job['target'],'queue_position':position,'queue_capacity':q.CAPACITY,'worker_pid':child.pid,'source_kind':job.get('source_kind','netease'),'quality_policy':'user-selected Bilibili audio may be lossy; full-track RVC with original-source provenance retained' if video_url is not None else 'lossless full-track RVC; singing-boundary inference disabled'},ensure_ascii=False))


def valid_binding(job):
    try:return hmac.compare_digest(str(job.get('submission_hmac','')),submission_binding(job))
    except ValueError:return False


def spawn_supervisor(jid):
    # Every attempt gets a distinct log; an already-live supervisor exits on its job lock.
    with (JOBS/(safe_id(jid)+'.supervisor.'+uuid.uuid4().hex[:8]+'.log')).open('x',encoding='utf-8') as log:
        return subprocess.Popen([str(PY),'-u','-m','hq_cover.jobs','supervise','--job',jid],cwd=ROOT,env=dict(os.environ,PYTHONIOENCODING='utf-8',PYTHONUNBUFFERED='1'),stdout=log,stderr=log,stdin=subprocess.DEVNULL,creationflags=0x00000008|0x00000200,close_fds=True)


@contextlib.contextmanager
def job_claim(jid,role='worker'):
    """One live worker owns a job through delivery, not just GPU processing."""
    import msvcrt
    if role not in ('worker','supervisor'):raise ValueError('Invalid job role')
    p=JOBS/(safe_id(jid)+'.'+role+'.lock')
    f=p.open('a+b');f.seek(0,2)
    if not f.tell():f.write(b'0');f.flush()
    f.seek(0)
    try:msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:
        f.close();raise RuntimeError('This job already has an active worker') from None
    try:yield
    finally:
        f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1);f.close()


def supervise(jid):
    """Wait without consuming worker deadline; hold the old GPU lock across the tree."""
    from . import queueing as q
    with job_claim(jid,'supervisor'):
        initial=load(jid)
        if initial.get('queue_version')!=q.VERSION:
            raise RuntimeError('Legacy task requires manual review; no automatic replay')
        if not q.replay_safe(initial,HQ):raise RuntimeError('Task was already attempted; no replay')
        if not initial.get('no_send') and not valid_binding(initial):
            initial.update(status='failed',stage='binding_failed',error='排队任务签名校验失败；未发送。');save(initial);return
        deadline=float(initial['queue_deadline'])
        monotonic_end=time.monotonic()+max(0,min(q.WAIT_SECONDS,deadline-time.time()))
        while True:
            if time.time()>=deadline or time.monotonic()>=monotonic_end:
                with q.mutex(JOBS):
                    current=load(jid)
                    if q.untouched(current):
                        current.update(status='failed',stage='queue_expired',finished=time.time(),error='排队等待已到上限；未启动制作或发送。');save(current)
                return
            try:
                with q.file_lock(JOBS/'dispatch.lock'):
                    with q.mutex(JOBS):
                        items=q.rows(JOBS)
                        if not q.can_start(items,jid):raise q.QueueBusy('Earlier or unknown running task')
                    with q.file_lock(ROOT/'gpu.lock'):
                        with q.mutex(JOBS):
                            current=load(jid)
                            if not q.replay_safe(current,HQ):raise RuntimeError('Task changed; no replay')
                            if not current.get('no_send') and not valid_binding(current):
                                current.update(status='failed',stage='binding_failed',error='排队期间任务签名改变；未启动。');save(current);return
                            nonce=uuid.uuid4().hex
                            current.update(dispatch_nonce=nonce,stage='dispatching',supervisor_pid=os.getpid());save(current)
                        try:return _supervise_worker(jid,nonce)
                        except Exception:
                            # Containment setup/cleanup did not return verified tree_empty.
                            # Fail this task explicitly and block later dispatch until an
                            # operator verifies its tree; never guess that it is safe.
                            current=load(jid)
                            current.update(status='failed',stage='supervision_uncertain',retry_prohibited=True,queue_blocked=True,finished=time.time(),error='监督进程未返回完整清理确认；请管理员核实，队列暂停且禁止重放。');save(current)
                            raise
            except q.QueueBusy:
                # Bounded sleeping, not an unbounded CPU busy loop. Old running is
                # deliberately never rewritten based on PID guesses or age alone.
                time.sleep(min(q.POLL_SECONDS,max(0,monotonic_end-time.monotonic())))


def _supervise_worker(jid,nonce):
    from .processes import run_contained
    if True:
        job=load(jid)
        if job.get('status')!='queued':raise RuntimeError('Only a new queued job may be supervised')
        timeout=float(job.get('timeout',12*3600))
        if not 0<timeout<=24*3600:raise ValueError('Invalid worker deadline')
        def started(pid):
            current=load(jid);current.update(supervisor_pid=os.getpid(),supervised_worker_pid=pid,deadline=time.time()+timeout);save(current)
        result=run_contained([str(PY),'-u','-m','hq_cover.jobs','run','--job',jid],ROOT,dict(os.environ,PYTHONIOENCODING='utf-8',PYTHONUNBUFFERED='1',HQ_QUEUE_DISPATCH=nonce),JOBS/(jid+'.worker.log'),timeout,on_started=started)
        with job_claim(jid):
            current=load(jid);current['process_supervision']=result
            if current.get('status') not in ('done','failed'):
                uncertain=bool(current.get('send_journal'))
                current.update(status='failed',send_ok=False,finished=time.time(),stage='delivery_uncertain' if uncertain else 'worker_terminated',delivery_uncertain=uncertain,retry_prohibited=uncertain,error='worker异常退出或超时；进程树已清理。'+('曾尝试QQ发送，结果需人工核实，禁止重发。' if uncertain else '未发起QQ发送。'))
            save(current)


def run(jid):
    with job_claim(jid):
        return _run_claimed(jid)


def _run_claimed(jid):
    from . import queueing as q
    job=load(jid)
    if job.get('pipeline')!=PIPELINE:raise RuntimeError('This worker does not accept legacy jobs')
    if job.get('status')!='queued' or job.get('send_journal'):
        raise RuntimeError('Job already attempted; no automatic resend/replay')
    if not job.get('no_send'):
        try:valid=hmac.compare_digest(str(job.get('submission_hmac','')),submission_binding(job))
        except ValueError:valid=False
        if not valid:
            job.update(status='failed',send_ok=False,error='提交身份绑定或继承凭据校验失败；拒绝发送。',stage='binding_failed',finished=time.time());save(job)
            return
    reserved=job.get('queue_version')==q.VERSION
    if reserved:
        nonce=os.environ.get('HQ_QUEUE_DISPATCH','')
        from .processes import in_contained_tree
        if not nonce or not hmac.compare_digest(nonce,str(job.get('dispatch_nonce',''))) or not in_contained_tree(job.get('supervised_worker_pid')):
            raise RuntimeError('Worker requires its contained queue dispatch identity')
    job.update(status='running',started=time.time(),worker_pid=os.getpid(),stage='starting');save(job)
    work=ROOT/'runs'/jid
    try:
        from .pipeline import process
        from .delivery import send_verified
        with (contextlib.nullcontext() if reserved else gpu_lock()):
            def stage(s):job.update(stage=s,updated=time.time());save(job)
            variant_kwargs={'allow_variant':True} if job.get('allow_variant') is True else {}
            if job.get('video_url') is not None:
                if job.get('source_kind')!='bilibili' or job.get('track_id') is not None or job.get('allow_variant'):
                    raise ValueError('Invalid video-source job binding')
                variant_kwargs={'video_url':job['video_url']}
            elif job.get('source_kind') not in (None,'netease'):
                raise ValueError('Unknown job source kind')
            result=process(job['song'],work,job['mode'],job.get('track_id'),stage,**variant_kwargs)
        job.update(cover=result['cover'],vocal=result['vocal'],quality_report=str(work/'mix/quality.json'),cache_hit=result.get('cache_hit') is True,stage='cached_cover_ready' if result.get('cache_hit') else 'quality_passed')
        if job.get('source_kind')=='bilibili':job['original_lossless']=False
        save(job)
        if job.get('no_send'):
            job.update(status='done',send_ok=False,delivery='not_requested',finished=time.time());save(job);return
        def checkpoint(event):
            job.setdefault('send_journal',[]).append(event);job.update(stage='qq_delivery',updated=time.time());save(job)
        # Exact target was bound by bridge to the submitting session; never change/fallback.
        relay_kwargs={'relay_grant':job['relay_grant']} if 'relay_grant' in job else {}
        delivery=send_verified(result['cover'],job['target'],result['quality'],work,checkpoint=checkpoint,**relay_kwargs)
        (work/'delivery.json').write_text(json.dumps(delivery,ensure_ascii=False,indent=2),encoding='utf-8')
        job.update(status='done',send_ok=delivery['verified'] is True,verified=delivery['verified'],message_ids=[x['message_id'] for x in delivery['sent']],finished=time.time(),stage='delivered')
    except Exception as e:
        # Only local controlled messages for known policy errors; other exceptions may contain signed URLs.
        # Error handling must work even when dependency imports themselves failed.
        known=type(e).__module__ in ('hq_cover.source','hq_cover.delivery','hq_cover.bilibili_source') and type(e).__name__ in ('NeedsReview','SourceError','DeliveryHeld','DeliveryUncertain','BilibiliSourceError')
        message=str(e) if known else f'{type(e).__name__}：高质量处理未完成，保留本地诊断，未继续发送。'
        job.update(status='failed',send_ok=False,error=message[:500],failure_type=type(e).__name__,finished=time.time(),stage='held_for_review' if known else 'failed')
        if known:job['quality_hold']=True
        if type(e).__name__=='DeliveryUncertain':
            job.update(delivery_uncertain=True,stage='delivery_uncertain',retry_prohibited=True)
        print('HQ_JOB_FAILED '+message,flush=True)
    save(job)


@contextlib.contextmanager
def render_slot():
    """Operator render yields to queued jobs, without stealing or killing GPU owners."""
    from . import queueing as q
    end=time.monotonic()+q.WAIT_SECONDS
    JOBS.mkdir(exist_ok=True)
    while True:
        stack=contextlib.ExitStack()
        try:
            stack.enter_context(q.file_lock(JOBS/'dispatch.lock'))
            with q.mutex(JOBS):
                items=q.rows(JOBS)
                if q.active(items) or any(j.get('queue_blocked') for j in items):raise q.QueueBusy('Queued or uncertain tasks have priority')
            stack.enter_context(q.file_lock(ROOT/'gpu.lock'))
        except q.QueueBusy:
            stack.close()
            if time.monotonic()>=end:raise RuntimeError('Render waiting deadline reached; no GPU task started') from None
            time.sleep(min(q.POLL_SECONDS,max(0,end-time.monotonic())));continue
        except BaseException:
            stack.close();raise
        try:yield
        finally:stack.close()
        return


def render(args):
    from .pipeline import process
    video=getattr(args,'video_url',None)
    if video is not None and args.track_id is not None:raise ValueError('B站来源不可与歌曲ID组合')
    with render_slot():result=process(args.song,Path(args.out),args.mode,args.track_id,**({'video_url':video} if video is not None else {}))
    print('COVER_PATH='+result['cover'],flush=True)


def main(argv=None):
    ap=argparse.ArgumentParser(description='Lossless high-quality breeze covers; no silent quality fallback')
    sub=ap.add_subparsers(dest='cmd',required=True)
    s=sub.add_parser('submit');s.add_argument('--song',required=True);s.add_argument('--target');s.add_argument('--mode',choices=['full','chorus'],default='full');s.add_argument('--track-id',type=int);s.add_argument('--allow-variant',action='store_true');s.add_argument('--video-url');s.add_argument('--relay-grant');s.add_argument('--no-send',action='store_true');s.set_defaults(func=submit)
    r=sub.add_parser('run');r.add_argument('--job',required=True);r.set_defaults(func=lambda a:run(a.job))
    sp=sub.add_parser('supervise');sp.add_argument('--job',required=True);sp.set_defaults(func=lambda a:supervise(a.job))
    st=sub.add_parser('status');st.add_argument('--job',required=True);st.set_defaults(func=lambda a:print(json.dumps(load(a.job),ensure_ascii=False,indent=2)))
    rr=sub.add_parser('render');rr.add_argument('--song',required=True);rr.add_argument('--out',required=True);rr.add_argument('--mode',choices=['full','chorus'],default='full');rr.add_argument('--track-id',type=int);rr.add_argument('--video-url');rr.set_defaults(func=render)
    a=ap.parse_args(argv);a.func(a)

if __name__=='__main__':main()
