"""CPU-only cache tests for boundary-free schema 2; no GPU, QQ or network."""
import copy,hashlib,json,tempfile,unittest
from pathlib import Path
from unittest import mock
import numpy as np
import soundfile as sf
from hq_cover import artifact_cache as c

class CacheTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.runs=self.root/'runs';self.runs.mkdir();self.cache=self.root/'cache'
  self.fp={'schema':2,'pipeline':c.PIPELINE,'singing_boundary_policy':c.BOUNDARY_POLICY,'digest':'policy2','code_hashes':{'rvc_pipeline_float.py':'pipe'},'assets':{}}
  for name,value in [('RUNS',self.runs),('CACHE',self.cache)]:
   p=mock.patch.object(c,name,value);p.start();self.addCleanup(p.stop)
  p=mock.patch.object(c,'fingerprint',return_value=self.fp);p.start();self.addCleanup(p.stop)
  self.run=self.runs/'original';self.run.mkdir()
  for d in ['source','uvr','rvc','mix']:(self.run/d).mkdir()
  sf.write(self.run/'source/source.flac',np.zeros((48000,2)),48000,subtype='PCM_16')
  sha=c._hash(self.run/'source/source.flac');raw=(self.run/'source/source.flac').read_bytes()
  source={'source_path':str(self.run/'source/source.flac'),'source_sha256':sha,'status':'verified_official_lossless','transcoded':False,'trial_restricted':False,'track':{'id':123,'title':'Song','artists':[{'id':1,'name':'Artist'}],'album':{'id':2,'name':'Album'},'duration_ms':1000},'quality_response':{'id':123,'code':200,'level':'lossless','type':'flac','size':len(raw),'md5':hashlib.md5(raw).hexdigest(),'sr':48000}}
  for p,ch in [(self.run/'uvr/vocals.wav',2),(self.run/'uvr/instrumental.wav',2),(self.run/'rvc/vocals.wav',1)]:sf.write(p,np.zeros((48000,ch)),48000,subtype='FLOAT')
  u={'stage':'uvr','status':'success','technical_checks_passed':True,'input':{'path':source['source_path'],'sha256':sha},'outputs':{n:{'path':str(self.run/'uvr'/(n+'.wav')),'sha256':c._hash(self.run/'uvr'/(n+'.wav'))} for n in ['vocals','instrumental']},'model':{'sha256':c.uvr_stage.EXPECTED_WEIGHT,'config_sha256':c.uvr_stage.EXPECTED_CONFIG},'settings':{'segment':1101,'overlap':4,'batch_size':1,'cpu_threads':1,'tf32':False,'dtype':'float32','normalization':False,'post_denoise':False}}
  r={'schema_version':2,'stage':'rvc','status':'success','technical_checks_passed':True,'input':u['outputs']['vocals'],'outputs':{'vocals':{'path':str(self.run/'rvc/vocals.wav'),'sha256':c._hash(self.run/'rvc/vocals.wav')}},'voice_id':c.VOICE,'singing_boundary_policy':c.BOUNDARY_POLICY,'original_source_bypass':False,'time_domain_gating':False,'private_pipeline_sha256':'pipe','assets':{p.name:{'sha256':h} for p,h in c.rvc_stage.ASSET_HASHES.items()},'settings':{'rms_mix':0,'index_rate':.75,'protect':.33,'pitch_shift_semitones':0,'f0_method':'rmvpe','f0_smooth':True,'chunk_seconds':18.,'overlap_seconds':.3,'time_domain_gating':False,'whole_chunk_passthrough':False,'boundary_detection':False,'integer_audio_intermediate':False,'original_vocal_blend':0,'cpu_threads':1,'tf32':False}}
  for kind,data in [('uvr',u),('rvc',r)]:self.dump(self.run/kind/'stage.json',data)
  files=[]
  for name,ch in [('cover',2),('vocal',1)]:
   p=self.run/'mix'/(name+'.wav');sf.write(p,np.zeros((48000,ch)),48000,subtype='PCM_24');files.append({'path':str(p),'sha256':c._hash(p),'duration_seconds':1.,'channels':ch})
  q={'quality_passed':True,'pipeline':c.PIPELINE,'voice_id':c.VOICE,'mode':'full','singing_boundary_policy':c.BOUNDARY_POLICY,'original_vocal_blend':0,'original_source_bypass':False,'files':files,'cover':files[0]['path'],'vocal':files[1]['path']}
  self.result={'source':source,'voice_id':c.VOICE,'mode':'full','pipeline':c.PIPELINE,'singing_boundary_policy':c.BOUNDARY_POLICY,'quality':q,'cover':q['cover'],'vocal':q['vocal'],'render_fingerprint':self.fp,'target':'SECRET','send_journal':['SECRET'],'token':'SECRET'}
  self.dump(self.run/'mix/quality.json',q);self.dump(self.run/'result.json',self.result)
 def dump(self,p,v):p.write_text(json.dumps(v),encoding='utf-8')
 def newrun(self,name='hit'):p=self.runs/name;p.mkdir();return p
 def test_register_lookup_no_send_state(self):
  m=c.register(self.result,self.run);self.assertEqual(m['schema'],2);self.assertNotIn('SECRET',json.dumps(m));hit=c.lookup(self.result['source'],'full',self.newrun());self.assertTrue(hit['cache_hit']);self.assertEqual(hit['singing_boundary_policy'],c.BOUNDARY_POLICY);self.assertNotIn('SECRET',json.dumps(hit));self.assertEqual(c._hash(hit['cover']),c._hash(self.result['cover']))
 def test_old_gate_schema_is_miss(self):
  m=c.register(self.result,self.run);p=self.cache/m['cache_entry']/'metadata.json';data=c._read(p);data['schema']=1;data['gate']={'intro':{},'outro':{}};self.dump(p,data);self.assertIsNone(c.lookup(self.result['source'],'full',self.newrun()))
 def test_malformed_manifest_is_miss(self):
  m=c.register(self.result,self.run);p=self.cache/m['cache_entry']/'metadata.json';data=c._read(p);data['files']=[];self.dump(p,data);self.assertIsNone(c.lookup(self.result['source'],'full',self.newrun()))
 def test_metrics_cannot_override_trusted_fields(self):
  m=c.register(self.result,self.run);p=self.cache/m['cache_entry']/'metadata.json';data=c._read(p);data['quality_metrics'].update(quality_passed=False,mode='chorus',files=['evil'],target='SECRET');self.dump(p,data);hit=c.lookup(self.result['source'],'full',self.newrun());self.assertTrue(hit['quality']['quality_passed']);self.assertEqual(hit['quality']['mode'],'full');self.assertNotIn('SECRET',json.dumps(hit))
 def test_stat_hash_cache_invalidates_after_mutation(self):
  p=self.root/'asset';p.write_bytes(b'first');a=c._hash(p,True);self.assertEqual(a,c._hash(p,True));p.write_bytes(b'changed-longer');self.assertNotEqual(a,c._hash(p,True))
 def test_source_changed_before_lookup_rejected(self):
  c.register(self.result,self.run);Path(self.result['source']['source_path']).write_bytes(b'bad')
  with self.assertRaises(c.CacheRejected):c.lookup(self.result['source'],'full',self.newrun())
 def test_missing_fingerprint_rejected(self):
  r=copy.deepcopy(self.result);r.pop('render_fingerprint')
  with self.assertRaises(c.CacheRejected):c.register(r,self.run)
 def test_mode_source_voice_policy_misses(self):
  c.register(self.result,self.run);self.assertIsNone(c.lookup(self.result['source'],'chorus',self.newrun('mode')))
  source=copy.deepcopy(self.result['source']);source['track']['id']=456;self.assertIsNone(c.lookup(source,'full',self.newrun('track')))
  with mock.patch.object(c,'fingerprint',return_value={'digest':'new'}):self.assertIsNone(c.lookup(self.result['source'],'full',self.newrun('policy')))
 def test_corrupt_audio_is_miss(self):
  m=c.register(self.result,self.run);(self.cache/m['cache_entry']/'cover.wav').write_bytes(b'corrupt');self.assertIsNone(c.lookup(self.result['source'],'full',self.newrun()))
 def test_original_quality_boolean_insufficient(self):
  Path(self.result['cover']).write_bytes(b'bad')
  with self.assertRaises(c.CacheRejected):c.register(self.result,self.run)
 def test_stage_tamper_or_gate_rejected(self):
  p=self.run/'rvc/stage.json';r=c._read(p);r['time_domain_gating']=True;self.dump(p,r)
  with self.assertRaises(c.CacheRejected):c.register(self.result,self.run)
 def test_path_escape_rejected(self):
  r=copy.deepcopy(self.result);r['cover']=str(self.root/'outside.wav')
  with self.assertRaises((c.CacheRejected,FileNotFoundError)):c.register(r,self.run)
 def test_no_overwrite(self):
  c.register(self.result,self.run);out=self.newrun();(out/'mix').mkdir()
  with self.assertRaises(c.CacheRejected):c.lookup(self.result['source'],'full',out)
 def test_import_rejects_historical_gate_run(self):
  r=copy.deepcopy(self.result);r['pipeline']='hq-lossless-uvr-rvc-v1';r['gate']={'intro':{},'outro':{}};self.dump(self.run/'result.json',r)
  with self.assertRaises(c.CacheRejected):c.import_run(self.run)
if __name__=='__main__':unittest.main()
