"""Isolated FIFO tests: fake workers, no pipeline imports, no GPU/QQ traffic."""
import contextlib,io,json,os,subprocess,sys,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from hq_cover import jobs,queueing as q

class QueueTests(unittest.TestCase):
    def test_queue_policy_version_is_two(self):self.assertEqual(q.VERSION,2)
    def seed(self,root,jid='test_a',seq=1,**extra):
        j=dict(id=jid,status='queued',pipeline=jobs.PIPELINE,timeout=10,send_ok=False,no_send=True,created=seq,queue_version=q.VERSION,queue_seq=seq,queue_deadline=time.time()+60)
        j.update(extra);(Path(root)/(jid+'.json')).write_text(json.dumps(j),encoding='utf-8');return j
    def test_fifo_unknown_running_and_replay(self):
        a=self.seed_dict('a',1);b=self.seed_dict('b',2)
        self.assertTrue(q.can_start([b,a],'a'));self.assertFalse(q.can_start([b,a],'b'))
        self.assertFalse(q.can_start([a,dict(id='old',status='running')],'a'))
        for mark in ['send_ok','send_journal','dispatch_nonce','supervised_worker_pid','delivery_uncertain']:
            self.assertFalse(q.untouched(dict(a,**{mark:True})))
    def seed_dict(self,jid,seq):return dict(id=jid,status='queued',created=seq,queue_version=q.VERSION,queue_seq=seq)
    def test_submission_capacity_and_fast_return(self):
        with tempfile.TemporaryDirectory() as t,mock.patch.object(jobs,'JOBS',Path(t)),mock.patch.object(jobs,'ROOT',Path(t)),mock.patch.object(jobs,'spawn_supervisor',return_value=SimpleNamespace(pid=77)):
            args=SimpleNamespace(song='test artist',no_send=True,target=None,mode='full',track_id=None)
            start=time.monotonic()
            for _ in range(q.CAPACITY):
                with contextlib.redirect_stdout(io.StringIO()):jobs.submit(args)
            self.assertLess(time.monotonic()-start,2)
            with self.assertRaises(q.QueueFull):jobs.submit(args)
            self.assertEqual(len(q.active(q.rows(t))),q.CAPACITY)
    def test_expired_wait_never_runs(self):
        with tempfile.TemporaryDirectory() as t,mock.patch.object(jobs,'JOBS',Path(t)),mock.patch.object(jobs,'ROOT',Path(t)),mock.patch('hq_cover.processes.run_contained') as worker:
            self.seed(t,queue_deadline=time.time()-1);jobs.supervise('test_a');worker.assert_not_called()
            self.assertEqual(jobs.load('test_a')['stage'],'queue_expired')
    def test_legacy_never_replayed(self):
        with tempfile.TemporaryDirectory() as t,mock.patch.object(jobs,'JOBS',Path(t)),mock.patch.object(jobs,'ROOT',Path(t)),mock.patch('hq_cover.processes.run_contained') as worker:
            self.seed(t,queue_version=q.VERSION-1)
            with self.assertRaisesRegex(RuntimeError,'Legacy'):jobs.supervise('test_a')
            worker.assert_not_called()
    def test_signed_queue_fields_tamper(self):
        with mock.patch.dict(os.environ,{'QQ_CONSOLE_TOKEN':'unit-test-only'}):
            j=dict(self.seed_dict('test_a',1),queue_deadline=123,timeout=10)
            signature=jobs.submission_binding(j)
            for field in ['queue_deadline','timeout','queue_seq']:
                changed=dict(j);changed[field]+=1;self.assertNotEqual(signature,jobs.submission_binding(changed))
    def test_cross_process_fifo_waits_for_legacy_gpu_lock(self):
        with tempfile.TemporaryDirectory() as t:
            self.seed(t,'test_a',1);self.seed(t,'test_b',2)
            script=Path(t)/'fake.py'
            script.write_text('''import sys,time,json,os
from pathlib import Path
from unittest import mock
from hq_cover import jobs,queueing as q
root=Path(sys.argv[1]);jobs.JOBS=root;jobs.ROOT=root;q.POLL_SECONDS=.05

def fake(*a,**kw):
    jid=sys.argv[2];kw['on_started'](os.getpid())
    with (root/'events').open('a') as f:f.write('start '+jid+'\\n')
    # Supervisor owns the exact legacy gpu lock throughout this child interval.
    try:
        with q.file_lock(root/'gpu.lock'):raise AssertionError('GPU lock released early')
    except q.QueueBusy:pass
    time.sleep(.1)
    j=jobs.load(jid);j.update(status='done',send_ok=False);jobs.save(j)
    return dict(pid=os.getpid(),returncode=0,timed_out=False,tree_empty=True)
with mock.patch('hq_cover.processes.run_contained',side_effect=fake):jobs.supervise(sys.argv[2])
''',encoding='utf-8')
            env=dict(os.environ,PYTHONPATH=str(Path(jobs.__file__).resolve().parent.parent))
            children=[]
            with (Path(t)/'output.log').open('w') as out:
                try:
                    with q.file_lock(Path(t)/'gpu.lock'):
                        # Start second first: persisted FIFO, not process scheduling, wins.
                        for jid in ['test_b','test_a']:children.append(subprocess.Popen([sys.executable,str(script),t,jid],env=env,stdout=out,stderr=out))
                        time.sleep(.3);self.assertFalse((Path(t)/'events').exists())
                    for child in children:self.assertEqual(child.wait(timeout=10),0)
                    self.assertEqual((Path(t)/'events').read_text().splitlines(),['start test_a','start test_b'])
                finally:
                    for child in children:
                        if child.poll() is None:child.kill();child.wait()
    def test_uncertain_supervision_blocks_queue_without_replay(self):
        with tempfile.TemporaryDirectory() as t,mock.patch.object(jobs,'JOBS',Path(t)),mock.patch.object(jobs,'ROOT',Path(t)),mock.patch('hq_cover.processes.run_contained',side_effect=RuntimeError('setup failed')):
            self.seed(t)
            with self.assertRaises(RuntimeError):jobs.supervise('test_a')
            failed=jobs.load('test_a');self.assertTrue(failed['queue_blocked']);self.assertTrue(failed['retry_prohibited'])
            other=self.seed(t,'test_b',2);self.assertFalse(q.can_start([failed,other],'test_b'))
    def test_live_supervisor_not_respawned_on_submit(self):
        with tempfile.TemporaryDirectory() as t,mock.patch.object(jobs,'JOBS',Path(t)),mock.patch.object(jobs,'ROOT',Path(t)),mock.patch.object(jobs,'spawn_supervisor',return_value=SimpleNamespace(pid=77)) as spawn:
            self.seed(t)
            with q.file_lock(Path(t)/'test_a.supervisor.lock'),contextlib.redirect_stdout(io.StringIO()):
                jobs.submit(SimpleNamespace(song='song artist',no_send=True,target=None,mode='full',track_id=None))
            self.assertEqual(spawn.call_count,1)
    def test_existing_work_artifact_forbids_recovery(self):
        with tempfile.TemporaryDirectory() as t:
            j=self.seed(t);(Path(t)/'runs'/'test_a').mkdir(parents=True)
            self.assertFalse(q.replay_safe(j,t))

if __name__=='__main__':unittest.main()
