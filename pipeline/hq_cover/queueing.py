"""Small durable FIFO. Locks coordinate processes; never infer stale running is safe to replay."""
import contextlib,json,time
from pathlib import Path

from .policy import QUEUE_VERSION as VERSION
CAPACITY=5  # running plus waiting, including local no-send tasks
WAIT_SECONDS=6*3600
POLL_SECONDS=5

class QueueBusy(RuntimeError):pass
class QueueFull(RuntimeError):pass

@contextlib.contextmanager
def file_lock(path):
    import msvcrt
    f=Path(path).open('a+b');f.seek(0,2)
    if not f.tell():f.write(b'0');f.flush()
    f.seek(0)
    try:msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:
        f.close();raise QueueBusy('Resource is owned by another process') from None
    try:yield
    finally:
        f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1);f.close()

@contextlib.contextmanager
def mutex(root,seconds=3):
    end=time.monotonic()+seconds
    while True:
        cm=file_lock(Path(root)/'queue.lock')
        try:cm.__enter__();break
        except QueueBusy:
            if time.monotonic()>=end:raise QueueBusy('Queue metadata lock busy; no task accepted') from None
            time.sleep(.05)
    try:yield
    finally:cm.__exit__(None,None,None)

def rows(root):
    result=[]
    for p in Path(root).glob('*.json'):
        try:
            j=json.loads(p.read_text(encoding='utf-8'))
            if not isinstance(j,dict):raise ValueError()
        except (OSError,ValueError):
            raise QueueBusy('Unreadable task state; administrator review required') from None
        result.append(j)
    return result

def active(items):return [j for j in items if j.get('status') in ('queued','running')]
def order(j):return (0 if j.get('queue_version')!=VERSION else 1,int(j.get('queue_seq',0)),float(j.get('created',0)),str(j.get('id','')))
def waiting(items):return sorted((j for j in items if j.get('status')=='queued'),key=order)
def untouched(j):
    return j.get('queue_version')==VERSION and j.get('status')=='queued' and not any(j.get(k) for k in ('dispatch_nonce','started','supervised_worker_pid','send_ok','sent','verified','send_journal','retry_prohibited','delivery_uncertain'))
def replay_safe(j,hq):
    if not untouched(j):return False
    work=Path(hq)/'runs'/str(j.get('id',''))
    # A pre-existing work directory can hide a journal/partial execution. Never recover it.
    return not work.exists()
def can_start(items,jid):
    pending=waiting(items)
    return bool(pending and pending[0].get('id')==jid and not any(j.get('status')=='running' or j.get('queue_blocked') for j in items))
