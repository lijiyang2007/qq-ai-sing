"""Explicit pro variant propagation; isolated files/mock peers only, no QQ/GPU."""
import contextlib,hashlib,hmac,io,json,os,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from hq_cover import source,jobs,pipeline
from hq_cover.tests.test_source import FixtureCase,make_track

class IdentityTests(unittest.TestCase):
 def test_normal_live_album_rejected_pro_exact_allowed(self):
  for title in ['Test Song (Live)','Test Song Remix','Test Song Cover','Test Song(共创版)','Test Song伴奏']:
   t=make_track(title=title,album='Concert Live 2020');query=title+' Test Artist'
   with self.assertRaises(source.NeedsReview):source.select_candidate(query,[t],123)
   self.assertEqual(source.select_candidate(query,[t],123,allow_variant=True),t)
 def test_pro_requires_full_identity_and_positive_id(self):
  t=make_track(title='Test Song (Live)',album='Concert Live 2020')
  for track_id in [None,0,-1,True]:
   with self.assertRaises(source.SourceError):source.select_candidate('Test Song (Live) Test Artist',[t],track_id,allow_variant=True)
  for query,track_id in [('Test Song (Live)',123),('Test Song (Live) Other Artist',123),('Test Song Test Artist',123),('Test Song (Live) Test Artist',456)]:
   with self.assertRaises(source.NeedsReview):source.select_candidate(query,[t],track_id,allow_variant=True)
  with self.assertRaises(source.SourceError):source.select_candidate('Test Song Test Artist',[make_track()],123,allow_variant='yes')

class CacheTests(FixtureCase):
 def variant_cache(self):
  manifest,record,path=self.make_cache();record['track']['title']='Test Song (Live)';record['track']['album']['name']='Live 2020';manifest.write_text(json.dumps(record),encoding='utf-8');return manifest,record,path
 def test_cache_path_permits_only_explicit_exact_variant(self):
  manifest,record,path=self.variant_cache()
  with mock.patch.object(source,'CACHE_ROOTS',(self.root,)):
   with self.assertRaises(source.NeedsReview):source.acquire_lossless('Test Song (Live) Test Artist',self.root/'normal',123)
   r=source.acquire_lossless('Test Song (Live) Test Artist',self.root/'pro',123,allow_variant=True)
  self.assertTrue(r['allow_variant']);self.assertTrue(r['cache_reused']);self.assertEqual(r['source_sha256'],record['download']['sha256'])
 def test_lossless_validation_not_bypassed(self):
  manifest,record,path=self.variant_cache();path.write_bytes(b'not flac')
  with mock.patch.object(source,'CACHE_ROOTS',(self.root,)):
   with self.assertRaises(source.SourceError):source.acquire_lossless('Test Song (Live) Test Artist',self.root/'bad',123,allow_variant=True)
 def test_official_detail_variant_keeps_full_lossless_validation(self):
  path,track,quality=self.make_flac();track['title']='Test Song (Live)';track['album']['name']='Live 2020'
  session=mock.Mock();payloads=[{'songs':[track]},{'data':[dict(quality,url='https://m1.music.126.net/test.flac')]}]
  def download(url,target,q):target.write_bytes(path.read_bytes())
  with mock.patch.object(source,'_authorized_session',return_value=session),mock.patch.object(source,'_official_get',side_effect=payloads),mock.patch.object(source,'_download',side_effect=download),mock.patch.object(source,'_lyrics',return_value={'available':False,'entries':[]}):
   r=source.acquire_lossless('Test Song (Live) Test Artist',self.root/'network-pro',123,allow_variant=True)
  self.assertTrue(r['allow_variant']);self.assertFalse(r['cache_reused']);self.assertTrue(r['verification']['flac_streaminfo_pcm_md5_match'])
 def test_no_id_rejected_before_workdir(self):
  out=self.root/'bad'
  with self.assertRaises(source.SourceError):source.acquire_lossless('Test Song Live Test Artist',out,allow_variant=True)
  self.assertFalse(out.exists())

class JobTests(unittest.TestCase):
 def test_hmac_variant_tamper_and_legacy_compatibility(self):
  j={'id':'test_job','song':'Song Artist','target':'private:12345678','mode':'full','track_id':123,'no_send':False,'pipeline':jobs.PIPELINE,'voice_id':'voice','created':1}
  with mock.patch.dict(os.environ,{'QQ_CONSOLE_TOKEN':'test-only'}):
   old=jobs.submission_binding(j)
   fields={k:j.get(k) for k in ('id','song','target','mode','track_id','no_send','pipeline','voice_id','created')}
   expected=hmac.new(b'test-only',json.dumps(fields,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode(),hashlib.sha256).hexdigest()
   self.assertEqual(old,expected);false=jobs.submission_binding(dict(j,allow_variant=False));true=jobs.submission_binding(dict(j,allow_variant=True));self.assertNotEqual(false,true);self.assertNotEqual(old,true)
 def test_submit_requires_exact_id(self):
  args=SimpleNamespace(song='Song Artist',target=None,no_send=True,mode='full',track_id=None,allow_variant=True)
  with self.assertRaisesRegex(ValueError,'ID'):jobs.submit(args)
 def test_cli_parses_flag(self):
  seen=[]
  with mock.patch.object(jobs,'submit',side_effect=lambda args:seen.append(args)):
   jobs.main(['submit','--song','Song Artist','--track-id','123','--allow-variant','--no-send'])
  self.assertTrue(seen[0].allow_variant);self.assertEqual(seen[0].track_id,123)
 def test_outer_cli_forwards_flag_unchanged(self):
  import importlib.util
  spec=importlib.util.spec_from_file_location('outer_cover_job',Path(jobs.__file__).parent.parent/'cover_job.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  args=['submit','--song','Song Artist','--track-id','123','--allow-variant','--no-send']
  with mock.patch.object(sys,'argv',['cover_job.py',*args]),mock.patch.object(jobs,'main') as cli:module.main();cli.assert_called_once_with(args)
 def test_worker_forwards_only_true(self):
  for flag in [None,False,True]:
   with tempfile.TemporaryDirectory() as tmp,mock.patch.object(jobs,'JOBS',Path(tmp)),mock.patch.object(jobs,'HQ',Path(tmp)):
    j={'id':'test_variant','song':'Song Artist','target':'local','mode':'full','track_id':123,'no_send':True,'pipeline':jobs.PIPELINE,'status':'queued','queue_version':2}
    # Use current dispatch identity with mocked containment; no process/GPU work.
    j['dispatch_nonce']='test';j['supervised_worker_pid']=1
    if flag is not None:j['allow_variant']=flag
    jobs.save(j)
    with mock.patch.dict(os.environ,{'HQ_QUEUE_DISPATCH':'test'}),mock.patch('hq_cover.processes.in_contained_tree',return_value=True),mock.patch('hq_cover.pipeline.process',return_value={'cover':'c','vocal':'v','quality':{}}) as proc:
     jobs.run(j['id'])
    self.assertEqual(proc.call_args.kwargs,{'allow_variant':True} if flag else {})
 def test_pipeline_source_flag_only_true(self):
  for flag in [False,True]:
   with tempfile.TemporaryDirectory() as tmp,mock.patch.object(pipeline,'acquire_lossless',side_effect=RuntimeError('stop before audio')) as acquire:
    with self.assertRaisesRegex(RuntimeError,'stop before audio'):pipeline.process('Song Artist',Path(tmp)/'run',track_id=123,allow_variant=flag)
    self.assertEqual(acquire.call_args.kwargs,{'track_id':123,**({'allow_variant':True} if flag else {})})

if __name__=='__main__':unittest.main()
