"""Mocked integration contracts: cache hits never invoke UVR/RVC/mix or old sends."""
import json,tempfile,unittest
from pathlib import Path
from unittest import mock
from hq_cover import pipeline,jobs,config

class TestArtifactPipeline(unittest.TestCase):
 def test_hit_skips_all_gpu_and_mixing(self):
  with tempfile.TemporaryDirectory() as tmp:
   out=Path(tmp)/'new_request'
   source={'source_path':'source.flac','source_sha256':'a'*64}
   result={'cache_hit':True,'mode':'full','cover':'new-cover','vocal':'new-vocal','singing_boundary_policy':'disabled_full_track_conversion','pipeline':jobs.PIPELINE,'quality':{'quality_passed':True,'pipeline':jobs.PIPELINE,'singing_boundary_policy':'disabled_full_track_conversion','original_source_bypass':False}}
   stages=[]
   with mock.patch.object(pipeline,'acquire_lossless',return_value=source),mock.patch.object(pipeline,'require_audio_hash'),mock.patch('hq_cover.artifact_cache.lookup',return_value=result) as lookup,mock.patch('hq_cover.artifact_cache.register') as register,mock.patch.object(pipeline,'run_stage') as gpu,mock.patch.object(pipeline,'mix_cover') as mix:
    actual=pipeline.process('song artist',out,stage_callback=stages.append)
    self.assertTrue(actual['cache_hit']);gpu.assert_not_called();mix.assert_not_called();register.assert_not_called();lookup.assert_called_once_with(source,'full',out)
    self.assertIn('cached_cover_ready',stages);self.assertTrue(json.loads((out/'result.json').read_text())['cache_hit'])
 def test_new_job_uses_current_target_and_fresh_delivery_directory(self):
  with tempfile.TemporaryDirectory() as tmp,mock.patch.object(jobs,'JOBS',Path(tmp)),mock.patch.object(jobs,'ROOT',Path(tmp)),mock.patch.dict('os.environ',{'QQ_CONSOLE_TOKEN':'unit-test-only'}):
   for index,target in enumerate(['private:12345678','group:87654321']):
    jid='cache_request_'+str(index)
    job={'id':jid,'status':'queued','pipeline':jobs.PIPELINE,'song':'song artist','mode':'full','target':target,'no_send':False,'created':index,'voice_id':config.VOICE_ID}
    job['submission_hmac']=jobs.submission_binding(job);jobs.save(job)
    result={'cache_hit':True,'cover':'validated-cover','vocal':'validated-vocal','singing_boundary_policy':'disabled_full_track_conversion','pipeline':jobs.PIPELINE,'quality':{'quality_passed':True,'pipeline':jobs.PIPELINE,'singing_boundary_policy':'disabled_full_track_conversion','original_source_bypass':False}}
    with mock.patch('hq_cover.pipeline.process',return_value=result),mock.patch('hq_cover.delivery.send_verified',return_value={'verified':True,'sent':[{'message_id':100+index}]}) as send:
     work=Path(tmp)/'runs'/jid;work.mkdir(parents=True)
     jobs.run(jid)
     call=send.call_args;self.assertEqual(call.args[1],target);self.assertEqual(call.args[3],work)
     final=jobs.load(jid);self.assertTrue(final['cache_hit']);self.assertTrue(final['send_ok']);self.assertEqual(final['message_ids'],[100+index])

if __name__=='__main__':unittest.main()
