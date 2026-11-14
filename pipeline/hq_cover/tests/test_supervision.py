"""Mocked supervisor state transitions, no network/GPU or real task spawn."""
import tempfile,unittest,time
from pathlib import Path
from unittest import mock
from hq_cover import jobs,queueing as q

class TestSupervisor(unittest.TestCase):
    def seed(self):
        j={'id':'test_1234','status':'queued','pipeline':jobs.PIPELINE,'timeout':10,'send_ok':False,'no_send':True,'queue_version':q.VERSION,'queue_seq':1,'queue_deadline':time.time()+60}
        jobs.save(j);return j
    def test_crashed_worker_marks_failed_only_after_tree_empty(self):
        with tempfile.TemporaryDirectory() as t,mock.patch.object(jobs,'JOBS',Path(t)),mock.patch.object(jobs,'HQ',Path(t)):
            self.seed()
            def child(*args,**kw):
                kw['on_started'](777)
                j=jobs.load('test_1234');j['status']='running';jobs.save(j)
                return {'pid':777,'returncode':1,'timed_out':False,'tree_empty':True}
            with mock.patch('hq_cover.processes.run_contained',side_effect=child):jobs.supervise('test_1234')
            j=jobs.load('test_1234');self.assertEqual(j['status'],'failed');self.assertEqual(j['stage'],'worker_terminated');self.assertFalse(j['delivery_uncertain'])
    def test_attempted_send_never_becomes_certainly_unsent(self):
        with tempfile.TemporaryDirectory() as t,mock.patch.object(jobs,'JOBS',Path(t)),mock.patch.object(jobs,'HQ',Path(t)):
            self.seed()
            def child(*args,**kw):
                kw['on_started'](777)
                j=jobs.load('test_1234');j.update(status='running',send_journal=[{'phase':'sending','part':1}]);jobs.save(j)
                return {'pid':777,'returncode':124,'timed_out':True,'tree_empty':True}
            with mock.patch('hq_cover.processes.run_contained',side_effect=child):jobs.supervise('test_1234')
            j=jobs.load('test_1234');self.assertTrue(j['delivery_uncertain']);self.assertTrue(j['retry_prohibited']);self.assertFalse(j['send_ok'])
    def test_successful_worker_state_preserved(self):
        with tempfile.TemporaryDirectory() as t,mock.patch.object(jobs,'JOBS',Path(t)),mock.patch.object(jobs,'HQ',Path(t)):
            self.seed()
            def child(*args,**kw):
                kw['on_started'](777)
                j=jobs.load('test_1234');j.update(status='done',send_ok=True);jobs.save(j)
                return {'pid':777,'returncode':0,'timed_out':False,'tree_empty':True}
            with mock.patch('hq_cover.processes.run_contained',side_effect=child):jobs.supervise('test_1234')
            j=jobs.load('test_1234');self.assertEqual(j['status'],'done');self.assertTrue(j['send_ok'])

if __name__=='__main__':unittest.main()
