import json,os,tempfile,unittest,hashlib
from pathlib import Path
from unittest import mock
import numpy as np
import soundfile as sf
from hq_cover.delivery import prepare_wav_parts,send_verified,DeliveryHeld
from hq_cover.jobs import safe_id

class Response:
    ok=True
    def __init__(self,data):self.data=data
    def json(self):return self.data

class TestDelivery(unittest.TestCase):
    def make_cover(self,root,seconds=2):
        p=Path(root)/'cover.wav';sf.write(p,np.zeros((48000*seconds,2)),48000,subtype='PCM_24')
        q={'quality_passed':True,'pipeline':'hq-lossless-uvr-rvc-v2-full-track','singing_boundary_policy':'disabled_full_track_conversion','original_source_bypass':False,'files':[{'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}]}
        return p,q
    def test_parts_lossless_no_truncation(self):
        with tempfile.TemporaryDirectory() as t:
            p,q=self.make_cover(t,20);parts=prepare_wav_parts(p,Path(t)/'parts',max_bytes=2*1024*1024,max_seconds=30)
            self.assertGreater(len(parts),1);self.assertEqual(sum(sf.info(x).frames for x in parts),48000*20)
            self.assertTrue(all(sf.info(x).subtype=='PCM_24' for x in parts))
    def test_unknown_target_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            p,q=self.make_cover(t)
            with self.assertRaises(DeliveryHeld):send_verified(p,'other',q,t)
    def test_missing_credentials_no_network(self):
        with tempfile.TemporaryDirectory() as t,mock.patch.dict(os.environ,{},clear=True),mock.patch('hq_cover.delivery.requests.Session') as net:
            p,q=self.make_cover(t)
            with self.assertRaises(DeliveryHeld):send_verified(p,'group:12345678',q,t)
            net.assert_not_called()
    def test_failed_quality_no_network(self):
        with mock.patch('hq_cover.delivery.requests.Session') as net:
            with self.assertRaises(DeliveryHeld):send_verified('missing','group:12345678',{'quality_passed':False},'.')
            net.assert_not_called()
    def test_tampered_file_no_send(self):
        with tempfile.TemporaryDirectory() as t,mock.patch('hq_cover.delivery.requests.Session') as net:
            p,q=self.make_cover(t);q['files'][0]['sha256']='0'*64
            with self.assertRaises(DeliveryHeld):send_verified(p,'group:12345678',q,t)
            net.assert_not_called()
    def test_verified_required(self):
        with tempfile.TemporaryDirectory() as t,mock.patch.dict(os.environ,{'QQ_CONSOLE_TOKEN':'fake-test-only','QQ_CONSOLE_PORT':'3100'}),mock.patch('hq_cover.delivery.requests.Session') as Session:
            p,q=self.make_cover(t);s=Session.return_value
            s.post.side_effect=[Response({'ok':True,'limits':{'maxSeconds':300,'maxBytes':20*1024*1024}}),Response({'ok':True}),Response({'ok':True,'messageId':123,'verified':False})]
            with self.assertRaises(DeliveryHeld):send_verified(p,'group:12345678',q,t)
            sends=[c for c in s.post.call_args_list if c.args[0].endswith('/voice/send')]
            queries=[c for c in s.post.call_args_list if c.args[0].endswith('/delivery-status')]
            self.assertEqual(len(sends),3);self.assertEqual(len(queries),3)
            self.assertEqual(sum(c.kwargs['json'].get('dryRun') is False for c in sends),1)
    def test_dry_run_sends_nothing(self):
        with tempfile.TemporaryDirectory() as t,mock.patch.dict(os.environ,{'QQ_CONSOLE_TOKEN':'fake-test-only'}),mock.patch('hq_cover.delivery.requests.Session') as Session:
            p,q=self.make_cover(t);s=Session.return_value;s.post.return_value=Response({'ok':True,'limits':{'maxSeconds':300,'maxBytes':20*1024*1024}})
            r=send_verified(p,'group:12345678',q,t,probe_only=True)
            self.assertTrue(r['dry_run_only']);self.assertEqual(r['sent'],[])
            for call in s.post.call_args_list:self.assertTrue(call.kwargs['json']['dryRun'])
    def test_job_path_traversal_rejected(self):
        with self.assertRaises(ValueError):safe_id('../../secret')

if __name__=='__main__':unittest.main()
