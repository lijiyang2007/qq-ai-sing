"""CPU-only transport integrity tests. No fade injected into hard/residual cases."""
import hashlib,os,tempfile,unittest
from pathlib import Path
import numpy as np
import soundfile as sf
from hq_cover.transport import prepare_transport_parts,validate_encoded,parse_mp3_gapless,TransportError,ENCODER_GRANULE

class GaplessTests(unittest.TestCase):
 def make(self,root,n=1152*50+21,sr=48000,tail='hard',name='master.wav'):
  t=np.arange(n)/sr;y=np.column_stack((.5*np.sin(2*np.pi*997*t),.4*np.sin(2*np.pi*659*t)))
  k=min(4096,n)
  if tail=='residual':y[-k:]=np.random.default_rng(4).normal(0,.001,(k,2))
  if tail=='fade':y[-k:]*=np.linspace(1,0,k)[:,None]
  p=Path(root)/name;sf.write(p,y,sr,subtype='PCM_24');return p
 def test_hard_residual_fade_preserved_without_silence_assumption(self):
  for tail in ['hard','residual','fade']:
   with self.subTest(tail=tail),tempfile.TemporaryDirectory() as tmp:
    p=self.make(tmp,tail=tail);old=p.read_bytes();parts,r=prepare_transport_parts(p,Path(tmp)/'out');row=r['parts'][0]
    self.assertEqual(p.read_bytes(),old);self.assertTrue(row['deterministic_content_verified'])
    self.assertEqual(row['content_frames'],sf.info(p).frames);self.assertEqual(row['decoded_frames'],row['content_frames']+row['pad_frames']);self.assertNotIn('pad_silence_peak',row)
    self.assertLess(row['pad_frames'],1152)
    self.assertEqual(parse_mp3_gapless(parts[0])['gapless_declared_frames'],row['decoded_frames'])
 def test_original_length_first_no_unneeded_padding(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=self.make(tmp,n=96000);parts,r=prepare_transport_parts(p,Path(tmp)/'out');row=r['parts'][0]
   self.assertFalse(row['alignment_retry']);self.assertEqual(row['pad_frames'],0);self.assertEqual(row['decoded_frames'],96000)
 def test_three_rates_and_residues(self):
  for sr in [32000,44100,48000]:
   for rem in [0,1,21,95,96,137,1151]:
    with self.subTest(sr=sr,rem=rem),tempfile.TemporaryDirectory() as tmp:
     n=1152*8+rem;p=self.make(tmp,n=n,sr=sr);parts,r=prepare_transport_parts(p,Path(tmp)/'out');row=r['parts'][0]
     self.assertEqual(row['content_frames'],n);self.assertLess(row['pad_frames'],1152);self.assertEqual(row['sample_rate'],sr);self.assertTrue(row['deterministic_content_verified'])
 def test_multipart_content_independently_matches_master(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=self.make(tmp,n=48000*11+21);old=p.read_bytes();parts,r=prepare_transport_parts(p,Path(tmp)/'out',max_seconds=5);cursor=0
   self.assertEqual(len(parts),3)
   with sf.SoundFile(p) as source:
    for row in r['parts']:
     self.assertEqual(cursor,row['source_start_frame']);source.seek(cursor);pcm=source.read(row['source_frames'],dtype='int32',always_2d=True)
     self.assertEqual(hashlib.sha256(pcm.tobytes()).hexdigest(),row['source_pcm_sha256']);cursor+=row['source_frames'];self.assertLessEqual(row['duration_seconds'],5)
   self.assertEqual(cursor,sf.info(p).frames);self.assertEqual(p.read_bytes(),old)
   self.assertTrue(all(x['pad_frames']==0 for x in r['parts'][:-1]))
 def test_gapless_metadata_tamper_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=self.make(tmp);parts,r=prepare_transport_parts(p,Path(tmp)/'out');mp=parts[0];row=r['parts'][0];g=parse_mp3_gapless(mp);raw=bytearray(mp.read_bytes());raw[g['_delay_padding_offset']+2]^=1;bad=Path(tmp)/'tampered.mp3';bad.write_bytes(raw)
   with self.assertRaises(TransportError):validate_encoded(bad,row['content_frames'],48000,2,row['pad_frames'],p)
 def test_physical_truncated_stream_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=self.make(tmp);parts,r=prepare_transport_parts(p,Path(tmp)/'out');bad=Path(tmp)/'truncated.mp3';bad.write_bytes(parts[0].read_bytes()[:-960]);row=r['parts'][0]
   with self.assertRaises(TransportError):validate_encoded(bad,row['content_frames'],48000,2,row['pad_frames'],p)
 def test_same_length_tail_replacement_rejected(self):
  for change in ['silence','new_audio','front']:
   with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
    original=self.make(tmp,n=1152*60+21);y,sr=sf.read(original,dtype='float32',always_2d=True)
    if change=='silence':y[-16384:]=0
    elif change=='new_audio':y[-4096:]=.05
    else:y[:4096]=0
    changed=Path(tmp)/'changed.wav';sf.write(changed,y,sr,subtype='PCM_24');parts,r=prepare_transport_parts(changed,Path(tmp)/'out');row=r['parts'][0]
    with self.assertRaises(TransportError):validate_encoded(parts[0],row['content_frames'],sr,2,row['pad_frames'],original)
 def test_invalid_padding_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=self.make(tmp);parts,r=prepare_transport_parts(p,Path(tmp)/'out')
   for pad in [-1,1152,True,1.1]:
    with self.assertRaises(TransportError):validate_encoded(parts[0],sf.info(p).frames,48000,2,pad,p)
 def test_real_previous_failure(self):
  # 这条用例原本绑定作者本机一份历史母带（仓库里没有）。改成读环境变量：
  # 把你自己的母带路径填进 HQ_TRANSPORT_REAL_WAV，并设 HQ_TRANSPORT_REAL_TEST=1。
  p=Path(os.environ.get('HQ_TRANSPORT_REAL_WAV',''))
  if os.environ.get('HQ_TRANSPORT_REAL_TEST')!='1' or not p.is_file():
   self.skipTest('opt-in real master test (set HQ_TRANSPORT_REAL_WAV)')
  before=hashlib.sha256(p.read_bytes()).hexdigest()
  with tempfile.TemporaryDirectory() as tmp:
   parts,r=prepare_transport_parts(p,Path(tmp)/'out');row=r['parts'][0]
   self.assertTrue(row['deterministic_content_verified'])
   self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),before)


if __name__=='__main__':unittest.main()
