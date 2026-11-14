"""Actual venv launcher containment test, no GPU or QQ."""
import os,json,tempfile,unittest
from pathlib import Path
from hq_cover import jobs
from hq_cover.processes import run_contained,in_contained_tree

class VenvContainment(unittest.TestCase):
    def test_launcher_and_interpreter_are_same_job(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);script=root/'child.py';pidfile=root/'launcher.txt'
            script.write_text('''import os,json,sys
from pathlib import Path
from hq_cover.processes import in_contained_tree
root=int(Path(sys.argv[1]).read_text())
print(json.dumps(dict(launcher=root,interpreter=os.getpid(),ppid=os.getppid(),verified=in_contained_tree(root),wrong=in_contained_tree(99999999))),flush=True)
assert in_contained_tree(root)
assert not in_contained_tree(99999999)
''',encoding='utf-8')
            env=dict(os.environ,PYTHONPATH=str(Path(jobs.__file__).resolve().parent.parent))
            report=run_contained([str(jobs.PY),str(script),str(pidfile)],root,env,root/'out.log',10,on_started=lambda p:pidfile.write_text(str(p)))
            self.assertEqual(report['returncode'],0);self.assertTrue(report['tree_empty'])
            data=json.loads((root/'out.log').read_text().strip());self.assertTrue(data['verified']);self.assertFalse(data['wrong'])
            print('VENV_CONTAINMENT',json.dumps(data),flush=True)
    def test_uncontained_process_rejected(self):self.assertFalse(in_contained_tree(os.getpid()))

if __name__=='__main__':unittest.main()
