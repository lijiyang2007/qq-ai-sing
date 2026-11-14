"""RVC full-track policy tests; fake engine, no GPU."""
import tempfile,unittest
from pathlib import Path
from unittest import mock
import numpy as np
from hq_cover import rvc_stage

class FakeVC:
 def __init__(self):self.calls=[]
 def vc_single(self,*args):
  self.calls.append(args)
  import soundfile as sf
  audio,sr=sf.read(args[1],dtype='float32')
  frames=round(len(audio)*rvc_stage.OUTPUT_RATE/sr)
  return 'Success. Index search used.',(rvc_stage.OUTPUT_RATE,np.full(frames,1e-7,dtype=np.float32))

class FullTrackRvcTests(unittest.TestCase):
 def test_extremely_quiet_whole_chunk_still_calls_rvc(self):
  with tempfile.TemporaryDirectory() as tmp:
   out=Path(tmp)/'out';out.mkdir();y=np.full(8000,1e-8,dtype=np.float32);vc=FakeVC()
   result,reports=rvc_stage.convert_chunks(y,8000,out,vc)
   self.assertEqual(len(vc.calls),1);self.assertEqual(len(reports),1);self.assertTrue(np.all(result!=0))
   self.assertNotIn('passthrough',reports[0]['message'].casefold())
 def test_no_silence_restoration_or_gate_api_exists(self):
  self.assertFalse(hasattr(rvc_stage,'silence_gaps'));self.assertFalse(hasattr(rvc_stage,'restore_silence'));self.assertFalse(hasattr(rvc_stage,'read_gate'));self.assertFalse(hasattr(rvc_stage,'apply_gate'))
 def test_cli_rejects_retired_gate_argument(self):
  with self.assertRaises(SystemExit) as cm,mock.patch('sys.stderr'):
   rvc_stage.main(['--input','x.wav','--out','new','--gate','old.json'])
  self.assertEqual(cm.exception.code,2)
if __name__=='__main__':unittest.main()
