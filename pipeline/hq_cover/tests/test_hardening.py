"""CPU/mocked regression tests: no QQ/network/GPU side effects."""
import copy,hashlib,json,os,tempfile,unittest
from pathlib import Path
from unittest import mock
import numpy as np
import requests
import soundfile as sf
from hq_cover import jobs,config
from hq_cover.delivery import send_verified,DeliveryHeld,DeliveryUncertain
from hq_cover.pipeline import verify_stage

class Response:
    ok=True
    def __init__(self,data):self.data=data
    def json(self):return self.data

class TestHardening(unittest.TestCase):
    def cover(self,root):
        p=Path(root)/'cover.wav';sf.write(p,np.zeros((4800,2)),48000,subtype='PCM_24')
        return p,{'quality_passed':True,'pipeline':'hq-lossless-uvr-rvc-v2-full-track','singing_boundary_policy':'disabled_full_track_conversion','original_source_bypass':False,'files':[{'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}]}
    def test_job_lock_rejects_duplicate_worker(self):
        with tempfile.TemporaryDirectory() as t,mock.patch.object(jobs,'JOBS',Path(t)):
            with jobs.job_claim('test_1234'):
                with self.assertRaises(RuntimeError):
                    with jobs.job_claim('test_1234'):self.fail('duplicate claim accepted')
            with jobs.job_claim('test_1234'):pass
    def test_atomic_job_save_has_no_shared_temp(self):
        with tempfile.TemporaryDirectory() as t,mock.patch.object(jobs,'JOBS',Path(t)):
            j={'id':'test_1234','status':'queued'};jobs.save(j);j['status']='running';jobs.save(j)
            self.assertEqual(jobs.load('test_1234')['status'],'running');self.assertEqual(list(Path(t).glob('*.tmp')),[])
    def test_submission_binding_covers_target_track_mode(self):
        j={'id':'test_1234','song':'song artist','target':'group:12345678','track_id':123,'no_send':False,'mode':'full','pipeline':jobs.PIPELINE,'voice_id':config.VOICE_ID,'created':1}
        with mock.patch.dict(os.environ,{'QQ_CONSOLE_TOKEN':'test-only-not-real'}):
            h=jobs.submission_binding(j)
            for key,value in [('target','group:87654321'),('track_id',456),('mode','chorus'),('no_send',True),('song','another artist')]:
                altered=dict(j);altered[key]=value;self.assertNotEqual(h,jobs.submission_binding(altered),key)
    def test_missing_worker_credentials_marks_failed_without_processing(self):
        with tempfile.TemporaryDirectory() as t,mock.patch.object(jobs,'JOBS',Path(t)),mock.patch.dict(os.environ,{},clear=True),mock.patch('hq_cover.pipeline.process') as process:
            jobs.save({'id':'test_1234','pipeline':jobs.PIPELINE,'status':'queued','no_send':False,'submission_hmac':'bad'})
            jobs.run('test_1234');self.assertEqual(jobs.load('test_1234')['stage'],'binding_failed');process.assert_not_called()
    def test_snapshot_prevents_mutation_during_preflight(self):
        with tempfile.TemporaryDirectory() as t,mock.patch.dict(os.environ,{'QQ_CONSOLE_TOKEN':'test-only'}),mock.patch('hq_cover.delivery.requests.Session') as Session:
            cover,q=self.cover(t);calls=[]
            def post(url,**kw):
                calls.append(kw)
                self.assertFalse(kw['allow_redirects'])
                if len(calls)==2:
                    p=Path(kw['json']['file']);p.write_bytes(p.read_bytes()+b'tamper')
                return Response({'ok':True,'limits':{'maxBytes':20000000,'maxSeconds':300}})
            Session.return_value.post.side_effect=post
            with self.assertRaises(DeliveryHeld):send_verified(cover,'group:12345678',q,t)
            self.assertEqual(len(calls),2);self.assertTrue(all(c['json']['dryRun'] for c in calls))
    def test_transport_timeout_is_uncertain_and_never_retried(self):
        with tempfile.TemporaryDirectory() as t,mock.patch.dict(os.environ,{'QQ_CONSOLE_TOKEN':'test-only'}),mock.patch('hq_cover.delivery.requests.Session') as Session:
            p,q=self.cover(t);s=Session.return_value;s.post.side_effect=[Response({'ok':True}),Response({'ok':True}),requests.Timeout('network')]
            with self.assertRaises(DeliveryUncertain):send_verified(p,'group:12345678',q,t)
            self.assertEqual(sum(c.args[0].endswith('/voice/send') for c in s.post.call_args_list),3)
            self.assertEqual(sum(c.args[0].endswith('/delivery-status') for c in s.post.call_args_list),3)
            count=s.post.call_count
            with self.assertRaises(DeliveryHeld):send_verified(p,'group:12345678',q,t)
            self.assertEqual(s.post.call_count,count)
    def test_probe_does_not_consume_formal_delivery(self):
        with tempfile.TemporaryDirectory() as t,mock.patch.dict(os.environ,{'QQ_CONSOLE_TOKEN':'test-only'}),mock.patch('hq_cover.delivery.requests.Session') as Session:
            p,q=self.cover(t);Session.return_value.post.return_value=Response({'ok':True,'messageId':456,'verified':True})
            first=send_verified(p,'group:12345678',q,t,probe_only=True)
            self.assertTrue(first['dry_run_only']);self.assertFalse((Path(t)/'delivery-attempt.json').exists())
            second=send_verified(p,'group:12345678',q,t);self.assertTrue(second['verified'])
    def test_old_quality_policy_rejected_before_network(self):
        with mock.patch('hq_cover.delivery.requests.Session') as net:
            with self.assertRaises(DeliveryHeld):send_verified('missing','group:12345678',{'quality_passed':True,'gate':{'intro':{},'outro':{}}},'.')
            net.assert_not_called()
    def test_stage_manifest_binds_source_and_output(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);source=root/'source.flac';source.write_bytes(b'original');sha=hashlib.sha256(source.read_bytes()).hexdigest()
            stage=root/'uvr';stage.mkdir();out=stage/'vocals.wav';inst=stage/'instrumental.wav';out.write_bytes(b'v');inst.write_bytes(b'i')
            r={'status':'success','stage':'uvr','technical_checks_passed':True,'input':{'path':str(source),'sha256':sha},'outputs':{}}
            for name,p in [('vocals',out),('instrumental',inst)]:r['outputs'][name]={'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
            (stage/'stage.json').write_text(json.dumps(r));verify_stage(stage,'uvr',source,sha)
            source.write_bytes(b'replaced')
            with self.assertRaises(RuntimeError):verify_stage(stage,'uvr',source,sha)
            source.write_bytes(b'original');out.write_bytes(b'other')
            with self.assertRaises(RuntimeError):verify_stage(stage,'uvr',source,sha)

if __name__=='__main__':unittest.main()
