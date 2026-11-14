"""Real harmless Windows process tests; no network, QQ or GPU."""
import os,sys,tempfile,unittest
from pathlib import Path
from hq_cover.processes import run_contained

@unittest.skipUnless(os.name=='nt','Windows Job Object tests')
class TestContainedProcess(unittest.TestCase):
    def test_normal_exit_and_log(self):
        with tempfile.TemporaryDirectory() as t:
            log=Path(t)/'log.txt'
            r=run_contained([sys.executable,'-c','print("contained-ok")'],t,dict(os.environ),log,20)
            self.assertEqual(r['returncode'],0);self.assertTrue(r['tree_empty']);self.assertFalse(r['timed_out'])
            self.assertIn('contained-ok',log.read_text())
    def test_timeout_terminates_tree(self):
        with tempfile.TemporaryDirectory() as t:
            code='import subprocess,sys,time;subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]);time.sleep(60)'
            r=run_contained([sys.executable,'-c',code],t,dict(os.environ),Path(t)/'log.txt',.8)
            self.assertTrue(r['timed_out']);self.assertTrue(r['tree_empty']);self.assertNotEqual(r['returncode'],0)
    def test_no_orphan_when_parent_exits(self):
        with tempfile.TemporaryDirectory() as t:
            code='import subprocess,sys;subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"])'
            r=run_contained([sys.executable,'-c',code],t,dict(os.environ),Path(t)/'log.txt',20)
            self.assertTrue(r['tree_empty']);self.assertFalse(r['timed_out'])
    def test_start_callback_error_fails_before_resume(self):
        with tempfile.TemporaryDirectory() as t:
            marker=Path(t)/'must-not-exist'
            code='from pathlib import Path;Path('+repr(str(marker))+').write_text("bad")'
            def fail(pid):raise RuntimeError('intentional callback failure')
            with self.assertRaises(RuntimeError):run_contained([sys.executable,'-c',code],t,dict(os.environ),Path(t)/'log.txt',20,on_started=fail)
            self.assertFalse(marker.exists())

if __name__=='__main__':unittest.main()
