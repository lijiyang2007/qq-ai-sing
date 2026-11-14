"""Bilibili dispatch/provenance/cache integration; no network, GPU or QQ side effects."""
import contextlib,copy,hashlib,hmac,io,json,os,sys,tempfile,types,unittest
from pathlib import Path
from unittest import mock
from hq_cover import jobs,pipeline,artifact_cache as cache,config
# 注意：这里必须 import 模块而不是 `from ... import CacheTests`。
# unittest 的 discover 会扫描模块命名空间里所有 TestCase 子类，直接 import 类会让
# CacheTests 的 14 个用例被重复收集一遍（同一个 id 跑两次）。
from hq_cover.tests import test_artifact_cache as _cache

URL='https://www.bilibili.com/video/BV1xx411c7mD?p=2'

def fake_module():
 m=types.ModuleType('hq_cover.bilibili_source')
 m.normalize_video_url=mock.Mock(side_effect=lambda u:u if isinstance(u,str) and u.startswith('https://www.bilibili.com/video/BV') else (_ for _ in ()).throw(ValueError('invalid video URL')))
 m.acquire_bilibili=mock.Mock()
 m.verify_bilibili_source=mock.Mock(return_value={'duration_seconds':1.})
 return m

class VideoJobTests(unittest.TestCase):
 def test_cli_accepts_video_url(self):
  with mock.patch.object(jobs,'submit') as submit:
   jobs.main(['submit','--song','B站指定视频','--video-url',URL,'--no-send'])
  self.assertEqual(submit.call_args.args[0].video_url,URL)
 def test_submit_normalizes_and_persists_bound_source(self):
  m=fake_module()
  with tempfile.TemporaryDirectory() as t,mock.patch.dict(sys.modules,{'hq_cover.bilibili_source':m}),mock.patch.object(jobs,'JOBS',Path(t)),mock.patch.object(jobs,'HQ',Path(t)),mock.patch.object(jobs,'spawn_supervisor',return_value=types.SimpleNamespace(pid=42)),contextlib.redirect_stdout(io.StringIO()):
   jobs.submit(types.SimpleNamespace(song='B站视频',video_url=URL,track_id=None,allow_variant=False,no_send=True,target=None,mode='full'))
   rows=list(Path(t).glob('*.json'));self.assertEqual(len(rows),1);j=json.loads(rows[0].read_text(encoding='utf-8'))
   self.assertEqual(j['source_kind'],'bilibili');self.assertEqual(j['video_url'],URL);m.normalize_video_url.assert_called_once_with(URL)
 def test_mutual_exclusion_rejected_before_submit(self):
  for track,allow in [(123,False),(123,True),(None,True)]:
   with self.subTest(track=track,allow=allow),mock.patch.object(jobs,'spawn_supervisor') as spawn:
    with self.assertRaises(ValueError):jobs.submit(types.SimpleNamespace(song='B站视频',video_url=URL,track_id=track,allow_variant=allow,no_send=True))
    spawn.assert_not_called()
 def test_ordinary_song_url_cannot_choose_video_source(self):
  with self.assertRaises(ValueError):jobs.submit(types.SimpleNamespace(song=URL,track_id=None,allow_variant=False,no_send=True))
 def test_hmac_binds_video_and_kind_with_legacy_compatibility(self):
  j={'id':'video_test','song':'B站视频','mode':'full','target':'private:12345678','no_send':False,'pipeline':jobs.PIPELINE,'voice_id':'voice','created':1}
  with mock.patch.dict(os.environ,{'QQ_CONSOLE_TOKEN':'unit-only'}):
   fields={k:j.get(k) for k in ('id','song','target','mode','track_id','no_send','pipeline','voice_id','created')}
   expected=hmac.new(b'unit-only',json.dumps(fields,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode(),hashlib.sha256).hexdigest()
   self.assertEqual(jobs.submission_binding(j),expected)
   video=dict(j,source_kind='bilibili',video_url=URL);sig=jobs.submission_binding(video)
   for changed in [dict(video,video_url=URL.replace('p=2','p=3')),dict(video,source_kind='netease')]:self.assertNotEqual(sig,jobs.submission_binding(changed))
 def test_worker_passes_only_explicit_video_kwarg(self):
  with tempfile.TemporaryDirectory() as t,mock.patch.object(jobs,'JOBS',Path(t)),mock.patch.object(jobs,'HQ',Path(t)),mock.patch.dict(os.environ,{'HQ_QUEUE_DISPATCH':'test'}),mock.patch('hq_cover.processes.in_contained_tree',return_value=True),mock.patch.object(pipeline,'process',return_value={'cover':'c','vocal':'v','quality':{}}) as process:
   j={'id':'video_test','song':'B站视频','mode':'full','target':'local','no_send':True,'pipeline':jobs.PIPELINE,'status':'queued','queue_version':2,'dispatch_nonce':'test','supervised_worker_pid':1,'source_kind':'bilibili','video_url':URL,'allow_variant':False,'track_id':None}
   jobs.save(j);jobs.run(j['id']);self.assertEqual(process.call_args.kwargs,{'video_url':URL});self.assertFalse(jobs.load(j['id'])['original_lossless'])

class RealVideoContractTests(unittest.TestCase):
 def test_real_acquirer_result_passes_cache_verification_contract(self):
  from hq_cover import bilibili_source as b
  import numpy as np
  import soundfile as sf
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);original=root/'input.flac';sf.write(original,np.zeros((64000,2)),32000,subtype='PCM_16')
   canonical='https://www.bilibili.com/video/BV1xx411c7mD/?p=2'
   safe={'canonical_url':canonical,'bvid':'BV1xx411c7mD','cid':123,'page':2,'title':'测试','duration_seconds':2.,'audio':{'bytes':original.stat().st_size,'cdn_host':'a.bilivideo.com'}}
   def download(url,path,url2,size):Path(path).write_bytes(original.read_bytes())
   with mock.patch.object(b,'_resolve',return_value=(safe,'https://a.bilivideo.com/fixture')),mock.patch.object(b,'_download',side_effect=download):
    source=b.acquire_bilibili(canonical,root/'run/source')
   identity=cache._identity(source,'full');self.assertEqual(identity['cid'],123);self.assertEqual(identity['page'],2)
   self.assertEqual(cache._verified_source_duration(source),2.);self.assertFalse(source['original_lossless'])

class VideoPipelineTests(unittest.TestCase):
 def source(self):return {'source_kind':'bilibili','source':'bilibili','status':'verified_bilibili_audio','original_lossless':False,'user_selected_source':True,'source_path':'source.wav','source_sha256':'a'*64,'video':{'bvid':'BV1xx411c7mD','cid':123,'page':2}}
 def test_explicit_video_uses_acquirer_not_netease_and_preserves_full_chain(self):
  m=fake_module();m.acquire_bilibili.return_value=self.source();stages=[]
  def verify(d,kind,*a):return {'outputs':{'vocals':{'sha256':'b'*64}}} if kind=='uvr' else {'voice_id':config.VOICE_ID,'singing_boundary_policy':pipeline.SINGING_BOUNDARY_POLICY,'original_source_bypass':False,'time_domain_gating':False}
  with tempfile.TemporaryDirectory() as t,mock.patch.dict(sys.modules,{'hq_cover.bilibili_source':m}),mock.patch.object(pipeline,'acquire_lossless') as netease,mock.patch.object(pipeline,'require_audio_hash'),mock.patch.object(cache,'RUNS',Path(t)),mock.patch.object(cache,'lookup',return_value=None),mock.patch.object(cache,'fingerprint',return_value={'test':True}),mock.patch.object(cache,'register') as register,mock.patch.object(pipeline,'run_stage') as run,mock.patch.object(pipeline,'verify_stage',side_effect=verify),mock.patch.object(pipeline,'mix_cover',return_value={'quality_passed':True,'singing_boundary_policy':pipeline.SINGING_BOUNDARY_POLICY,'original_source_bypass':False,'cover':'c','vocal':'v'}):
   result=pipeline.process('B站视频',Path(t)/'run',video_url=URL,stage_callback=stages.append)
   netease.assert_not_called();m.acquire_bilibili.assert_called_once();self.assertEqual(run.call_count,2);self.assertEqual(stages[0],'verify_bilibili_audio');self.assertFalse(result['original_lossless']);self.assertFalse(result['quality']['original_lossless']);self.assertEqual(result['quality']['source_kind'],'bilibili');register.assert_called_once()
 def test_video_bad_flags_fail_before_workdir(self):
  with tempfile.TemporaryDirectory() as t:
   for kwargs in [{'video_url':URL,'track_id':1},{'video_url':URL,'allow_variant':True}]:
    p=Path(t)/'run'
    with self.assertRaises(ValueError):pipeline.process('B站视频',p,**kwargs)
    self.assertFalse(p.exists())
 def test_query_url_never_implicitly_acquires_video(self):
  with tempfile.TemporaryDirectory() as t,mock.patch.object(pipeline,'acquire_lossless') as n:
   with self.assertRaises(ValueError):pipeline.process(URL,Path(t)/'run')
   n.assert_not_called()

class VideoCacheTests(_cache.CacheTests):
 # Reuse established realistic stage/PCM fixture, override only video provenance.
 def setUp(self):
  super().setUp();self.original_source=copy.deepcopy(self.result['source'])
  self.video=fake_module();p=mock.patch.dict(sys.modules,{'hq_cover.bilibili_source':self.video});p.start();self.addCleanup(p.stop)
 def as_video(self):
  source=self.result['source'];source.update(source_kind='bilibili',source='bilibili',status='verified_bilibili_audio',original_lossless=False,lossless_original=False,user_selected_source=True,video={'bvid':'BV1xx411c7mD','cid':123,'page':2,'canonical_url':URL})
  self.result.update(source_kind='bilibili',original_lossless=False);self.result['quality'].update(source_kind='bilibili',original_lossless=False)
  self.dump(self.run/'mix/quality.json',self.result['quality']);return source
 def test_video_identity_is_disjoint_and_page_bound(self):
  original=cache._identity(self.original_source,'full');source=self.as_video();video=cache._identity(source,'full')
  self.assertNotEqual(original,video);self.assertNotIn('track_id',video)
  changed=copy.deepcopy(source);changed['video']['page']=3;self.assertNotEqual(video,cache._identity(changed,'full'))
 def test_video_register_lookup_uses_video_verifier_and_keeps_lossy_label(self):
  source=self.as_video()
  with mock.patch('hq_cover.source.validate_lossless',side_effect=AssertionError('Video must not use FLAC assertion')):
   m=cache.register(self.result,self.run);hit=cache.lookup(source,'full',self.newrun())
  self.assertGreaterEqual(self.video.verify_bilibili_source.call_count,2);self.assertFalse(m['original_lossless']);self.assertFalse(hit['quality']['original_lossless']);self.assertEqual(hit['source_kind'],'bilibili')
 def test_video_mislabeled_lossless_rejected(self):
  source=self.as_video();self.result['quality']['original_lossless']=True;self.dump(self.run/'mix/quality.json',self.result['quality'])
  with self.assertRaises(cache.CacheRejected):cache.register(self.result,self.run)
 def test_video_cache_cannot_hit_netease_same_hash(self):
  source=self.as_video();cache.register(self.result,self.run)
  self.assertIsNone(cache.lookup(self.original_source,'full',self.newrun()))

if __name__=='__main__':unittest.main()
