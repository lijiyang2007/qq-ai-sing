"""CPU-only transport regressions. All QQ HTTP requests are mocked."""
import hashlib,json,os,tempfile,unittest
from pathlib import Path
from unittest import mock
import numpy as np
import soundfile as sf
from hq_cover.transport import prepare_transport_parts,validate_encoded,TransportError,BIT_RATE
from hq_cover.delivery import send_verified,DeliveryHeld

class Response:
    ok=True
    def json(self):return {'ok':True,'messageId':123,'verified':True,'limits':{'maxBytes':20971520,'maxSeconds':300,'maxPerMinute':2,'maxPerHour':10}}

class TestTransport(unittest.TestCase):
    def make_cover(self,root,frames=48000):
        p=Path(root)/'master.wav';t=np.arange(frames)/48000
        y=np.column_stack((.05*np.sin(2*np.pi*440*t),.04*np.sin(2*np.pi*660*t)))
        sf.write(p,y,48000,subtype='PCM_24')
        sha=hashlib.sha256(p.read_bytes()).hexdigest()
        return p,{'quality_passed':True,'pipeline':'hq-lossless-uvr-rvc-v2-full-track','singing_boundary_policy':'disabled_full_track_conversion','original_source_bypass':False,'files':[{'path':str(p),'sha256':sha}]}
    def test_lossy_transport_retains_master_and_full_sample_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            p,q=self.make_cover(tmp,48000*11+137);original=p.read_bytes()
            paths,r=prepare_transport_parts(p,Path(tmp)/'transport',max_seconds=5)
            self.assertEqual(len(paths),3);self.assertEqual(p.read_bytes(),original)
            self.assertTrue(r['lossy']);self.assertEqual(r['bit_rate'],320000)
            self.assertEqual(sum(x['content_frames'] for x in r['parts']),48000*11+137)
            self.assertEqual(sum(x['decoded_frames'] for x in r['parts']),48000*11+137+r['total_pad_frames'])
            cursor=0
            for row in r['parts']:
                self.assertEqual(row['source_start_frame'],cursor)
                with sf.SoundFile(p) as original:
                    original.seek(cursor)
                    pcm=original.read(row['source_frames'],dtype='int32',always_2d=True)
                self.assertEqual(hashlib.sha256(pcm.tobytes()).hexdigest(),row['source_pcm_sha256'])
                self.assertTrue(row['deterministic_content_verified'])
                cursor+=row['source_frames']
                self.assertEqual(row['sample_rate'],48000);self.assertEqual(row['channels'],2)
                self.assertLess(row['decoded_peak'],1);self.assertEqual(row['bit_rate'],BIT_RATE)
            self.assertEqual(sf.info(p).subtype,'PCM_24')
    def test_wrong_master_format_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'bad.wav';sf.write(p,np.zeros((4800,2)),48000,subtype='PCM_16')
            with self.assertRaises(TransportError):prepare_transport_parts(p,Path(tmp)/'out')
    def test_decode_frame_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p,q=self.make_cover(tmp);parts,r=prepare_transport_parts(p,Path(tmp)/'out')
            with self.assertRaises(TransportError):validate_encoded(parts[0],48001,48000,2)
    def test_private_and_group_payload_bound_and_all_preflight_first(self):
        for target in ['private:12345678','group:87654321']:
            with self.subTest(target=target),tempfile.TemporaryDirectory() as tmp,mock.patch.dict(os.environ,{'QQ_CONSOLE_TOKEN':'test-only'}),mock.patch('hq_cover.delivery.requests.Session') as Session:
                p,q=self.make_cover(tmp);original=p.read_bytes();s=Session.return_value;s.post.return_value=Response();journal=[]
                result=send_verified(p,target,q,tmp,checkpoint=journal.append)
                calls=[c.kwargs['json'] for c in s.post.call_args_list]
                self.assertTrue(all(c['target']==target for c in calls))
                self.assertEqual([c['dryRun'] for c in calls],[True,True,False])
                self.assertTrue(all(Path(c['file']).suffix=='.mp3' for c in calls[1:]))
                self.assertTrue(all(c['normalize'] is False and c['volume']==1 and c['verify'] is True for c in calls))
                self.assertTrue(result['verified']);self.assertTrue(result['transport']['lossy']);self.assertEqual(p.read_bytes(),original)
                self.assertEqual(journal[0]['target'],target)
                self.assertEqual(journal[0]['transport_format'],'MP3 320kbps')
                manifest=json.loads((Path(tmp)/'qq-send-manifest.json').read_text(encoding='utf-8'))
                self.assertEqual(manifest['target'],target);self.assertTrue(manifest['transport']['complete_sample_coverage'])
    def test_transport_failure_never_sends(self):
        with tempfile.TemporaryDirectory() as tmp,mock.patch.dict(os.environ,{'QQ_CONSOLE_TOKEN':'test-only'}),mock.patch('hq_cover.delivery.requests.Session') as Session,mock.patch('hq_cover.transport.prepare_transport_parts',side_effect=TransportError('held')):
            p,q=self.make_cover(tmp);Session.return_value.post.return_value=Response()
            with self.assertRaises(DeliveryHeld):send_verified(p,'private:12345678',q,tmp)
            self.assertTrue(all(c.kwargs['json']['dryRun'] for c in Session.return_value.post.call_args_list))

if __name__=='__main__':unittest.main()
