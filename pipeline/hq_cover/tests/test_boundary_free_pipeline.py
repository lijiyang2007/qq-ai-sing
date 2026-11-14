"""Boundary-free pipeline contract; no GPU/network/QQ."""
import tempfile,unittest
from pathlib import Path
from unittest import mock
from hq_cover import pipeline,config

class BoundaryFreePipelineTests(unittest.TestCase):
 def test_pipeline_never_runs_gate_stage_or_passes_gate(self):
  with tempfile.TemporaryDirectory() as tmp:
   work=Path(tmp)/'run';source={'source_path':str(Path(tmp)/'source.flac'),'source_sha256':'a'*64};Path(source['source_path']).write_bytes(b'x');stages=[];rvc_args=[]
   def run_stage(py,script,args,workdir):
    stages.append(Path(script).stem)
    if Path(script).stem=='rvc_stage':rvc_args.extend(map(str,args))
   uvr={'outputs':{'vocals':{'sha256':'b'*64}}};rvc={'voice_id':config.VOICE_ID,'singing_boundary_policy':'disabled_full_track_conversion','original_source_bypass':False,'time_domain_gating':False}
   def verify(directory,kind,*args):return uvr if kind=='uvr' else rvc
   quality={'quality_passed':True,'pipeline':'hq-lossless-uvr-rvc-v2-full-track','singing_boundary_policy':'disabled_full_track_conversion','original_source_bypass':False,'cover':'cover','vocal':'vocal'};fp={'digest':'v2'}
   with mock.patch.object(pipeline,'acquire_lossless',return_value=source),mock.patch.object(pipeline,'require_audio_hash'),mock.patch.object(pipeline,'run_stage',side_effect=run_stage),mock.patch.object(pipeline,'verify_stage',side_effect=verify),mock.patch.object(pipeline,'mix_cover',return_value=quality) as mix,mock.patch('hq_cover.artifact_cache.lookup',return_value=None),mock.patch('hq_cover.artifact_cache.register') as register,mock.patch('hq_cover.artifact_cache.fingerprint',return_value=fp):
    result=pipeline.process('song',work)
   self.assertEqual(stages,['uvr_stage','rvc_stage']);self.assertNotIn('--gate',rvc_args);self.assertEqual(result['singing_boundary_policy'],'disabled_full_track_conversion');mix.assert_called_once();register.assert_called_once()
 def test_rvc_manifest_with_gate_is_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   work=Path(tmp)/'run';source={'source_path':str(Path(tmp)/'source.flac'),'source_sha256':'a'*64};Path(source['source_path']).write_bytes(b'x')
   uvr={'outputs':{'vocals':{'sha256':'b'*64}}};rvc={'voice_id':config.VOICE_ID,'singing_boundary_policy':'disabled_full_track_conversion','original_source_bypass':False,'time_domain_gating':True}
   def verify(directory,kind,*args):return uvr if kind=='uvr' else rvc
   with mock.patch.object(pipeline,'acquire_lossless',return_value=source),mock.patch.object(pipeline,'require_audio_hash'),mock.patch.object(pipeline,'run_stage'),mock.patch.object(pipeline,'verify_stage',side_effect=verify),mock.patch('hq_cover.artifact_cache.lookup',return_value=None),mock.patch('hq_cover.artifact_cache.fingerprint',return_value={'digest':'v2'}):
    with self.assertRaisesRegex(RuntimeError,'无边界策略'):pipeline.process('song',work)
if __name__=='__main__':unittest.main()
