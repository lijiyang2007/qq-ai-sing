"""CPU/mock only: no downloads, credentials, GPU or real QQ."""
import copy,io,json,unittest
from unittest import mock
from hq_cover import song_resolver as r
from hq_cover import source

def track(i=1,title='Heartbeat',artists=('Childish Gambino',),album='Camp'):
 return {'id':i,'title':title,'artists':[{'id':n+1,'name':name} for n,name in enumerate(artists)],'album':{'id':3,'name':album},'duration_ms':269840}

def stream(i=1,**extra):
 return {'id':i,'code':200,'level':'lossless','type':'flac','size':12345,'md5':'a'*32,'url':'https://m1.music.126.net/signed.flac',**extra}

class ResolverTests(unittest.TestCase):
 def call(self,tracks,streams=None,artist=None,auto=True,count=None):
  calls=[];session=mock.Mock();streams=streams or {t['id']:stream(t['id']) for t in tracks}
  def api(s,endpoint,args):
   calls.append((endpoint,args))
   if endpoint.endswith('/search/get'):return {'code':200,'result':{'songs':tracks,'songCount':len(tracks) if count is None else count}}
   if endpoint.endswith('/song/detail'):return {'songs':[t for t in tracks if t['id']==args['id']]}
   if endpoint.endswith('/url/v1'):return {'data':[streams[json.loads(args['ids'])[0]]]}
   raise AssertionError(endpoint)
  with mock.patch.object(source,'_official_get',side_effect=api):
   result=r.resolve('Heartbeat',artist,auto,catalog_provider=lambda *a:[],session_factory=lambda:session)
  return result,calls,session
 def test_wait_before_title_only_auto(self):
  provider=mock.Mock();factory=mock.Mock();result=r.resolve('Heartbeat',catalog_provider=provider,session_factory=factory)
  self.assertEqual(result['code'],'awaiting_artist');self.assertEqual(result['wait_seconds'],120);provider.assert_not_called();factory.assert_not_called()
 def test_catalog_priority_avoids_incomplete_search(self):
  factory=mock.Mock(side_effect=AssertionError('network forbidden'))
  result=r.resolve('Heartbeat','Childish Gambino',catalog_provider=lambda *a:[{'track':track(),'created':1}],session_factory=factory)
  self.assertTrue(result['ok']);self.assertEqual(result['source'],'catalog');self.assertEqual(result['track_id'],1);factory.assert_not_called()
 def test_catalog_auto_stable_order(self):
  records=[{'track':track(4),'created':1},{'track':track(3),'created':2},{'track':track(2),'created':2}]
  self.assertEqual(r.resolve('Heartbeat',auto_select=True,catalog_provider=lambda *a:records)['track_id'],2)
 def test_catalog_nonauto_multiple_ambiguous(self):
  result=r.resolve('Heartbeat','Childish Gambino',catalog_provider=lambda *a:[{'track':track(1),'created':1},{'track':track(2),'created':2}])
  self.assertEqual(result['code'],'ambiguous')
 def test_first_available_official_even_truncated_auto(self):
  result,calls,s=self.call([track(3),track(2)],{3:stream(3,freeTrialInfo={}),2:stream(2)},count=500)
  self.assertTrue(result['ok']);self.assertEqual(result['track_id'],2);s.close.assert_called_once();self.assertNotIn('signed',json.dumps(result))
 def test_nonauto_incomplete_holds(self):
  result,calls,s=self.call([track()],artist='Childish Gambino',auto=False,count=500)
  self.assertEqual(result['code'],'search_incomplete');self.assertFalse(any(x[0].endswith('/url/v1') for x in calls))
 def test_excludes_variants_and_inexact_title(self):
  result,calls,s=self.call([track(1,title='Heartbeat Live'),track(2,album='Camp Live'),track(3,title='Heartbeats'),track(4)])
  self.assertEqual(result['track_id'],4)
 def test_wrong_artist_never_chosen(self):
  result,_,_=self.call([track(1,artists=('Someone Else',)),track(2)],artist='Childish Gambino')
  self.assertEqual(result['track_id'],2)
 def test_partial_duet_not_silent_match(self):
  result,_,_=self.call([track(1,artists=('Childish Gambino','Guest'))],artist='Childish Gambino')
  self.assertFalse(result['ok'])
 def test_full_duet_allowed(self):
  result,_,_=self.call([track(1,artists=('Childish Gambino','Guest'))],artist='Childish Gambino Guest')
  self.assertTrue(result['ok'])
 def test_stream_rejections(self):
  for bad in [{'url':None},{'url':'https://evil.test/x'},{'level':'standard'},{'type':'mp3'},{'size':None},{'md5':None},{'freeTrialPrivilege':{'resConsumable':True}},{'freeTrialInfo':{}}]:
   with self.subTest(bad=bad):self.assertFalse(self.call([track()],{1:stream(1,**bad)})[0]['ok'])
 def test_safe_error(self):
  result=r.resolve('Heartbeat',auto_select=True,catalog_provider=lambda *a:[],session_factory=mock.Mock(side_effect=RuntimeError('cookie SECRET signedURL')))
  self.assertEqual(result['code'],'resolver_unavailable');self.assertNotIn('SECRET',json.dumps(result))
 def test_cli_single_json(self):
  out=io.StringIO()
  with mock.patch('sys.stdout',out):self.assertEqual(r.main(['resolve','--title','Heartbeat']),0)
  result=json.loads(out.getvalue());self.assertEqual(result['code'],'awaiting_artist');self.assertEqual(len(out.getvalue().splitlines()),1)
 def test_query_catalog_only_unique(self):
  provider=mock.Mock(return_value=[{'track':track(),'created':1}])
  with mock.patch.object(source,'_official_get',side_effect=AssertionError('no network')):
   result=r.resolve_query('Heartbeat Childish Gambino',catalog_provider=provider)
  self.assertTrue(result['ok']);provider.assert_called_once_with(query='Heartbeat Childish Gambino')
 def test_query_no_title_only_guess_or_fallback(self):
  for query in ['Heartbeat','Heartbeat Someone Else']:
   result=r.resolve_query(query,catalog_provider=lambda **kw:[{'track':track(),'created':1}])
   self.assertEqual(result['code'],'catalog_miss')
 def test_query_multiple_hold(self):
  result=r.resolve_query('Heartbeat Childish Gambino',catalog_provider=lambda **kw:[{'track':track(1)},{'track':track(2)}])
  self.assertEqual(result['code'],'ambiguous')
 def test_query_cli_json(self):
  out=io.StringIO()
  with mock.patch('sys.stdout',out),mock.patch.object(r,'resolve_query',return_value={'ok':False,'code':'catalog_miss'}):r.main(['resolve','--query','Heartbeat Childish Gambino'])
  self.assertEqual(json.loads(out.getvalue())['code'],'catalog_miss')
 def test_bad_input(self):
  for title in ['',None,'a\nsecret']:
   self.assertEqual(r.resolve(title)['code'],'invalid_title')

class VersionQueryTests(unittest.TestCase):
 def provider(self,years=(2011,),album='Camp'):
  return mock.Mock(return_value=[{'track':track(album=album),'created':1,'release_years':list(years)}])
 def test_album_year_canonical_query(self):
  p=self.provider();result=r.resolve_query('Heartbeat - Childish Gambino (Camp, 2011)',catalog_provider=p)
  self.assertTrue(result['ok']);self.assertEqual(result['song'],'Heartbeat Childish Gambino');self.assertEqual(result['track_id'],1)
  self.assertTrue(result['version_confirmation']['verified']);self.assertEqual(result['version_confirmation']['release_year'],2011)
  p.assert_called_once_with(query='Heartbeat - Childish Gambino')
 def test_chinese_parentheses_and_comma(self):
  result=r.resolve_query('Heartbeat - Childish Gambino （Camp，2011）',catalog_provider=self.provider())
  self.assertTrue(result['ok'])
 def test_wrong_unknown_and_missing_year_never_fallback(self):
  for q in ['Heartbeat - Childish Gambino (Camp, 2012)','Heartbeat - Childish Gambino (Unknown, 2011)','Heartbeat - Childish Gambino (Camp)','Heartbeat - Childish Gambino (Camp, xx)','Heartbeat - Childish Gambino (2011)','Heartbeat - Childish Gambino (Camp, 2011) extra']:
   with self.subTest(q=q):self.assertEqual(r.resolve_query(q,catalog_provider=self.provider())['code'],'invalid_version')
 def test_absent_or_conflicting_year_evidence_rejected(self):
  for years in [(),(2012,),(2011,2012),('2011',),(True,)]:
   with self.subTest(years=years):self.assertEqual(r.resolve_query('Heartbeat - Childish Gambino (Camp, 2011)',catalog_provider=self.provider(years))['code'],'invalid_version')
 def test_live_remix_karaoke_annotations_not_stripped(self):
  for note in ['Live','Camp Live, 2011','Camp Remix, 2011','Karaoke, 2011','伴奏, 2011','Camp, 2011, Live']:
   self.assertEqual(r.resolve_query('Heartbeat - Childish Gambino ('+note+')',catalog_provider=self.provider())['code'],'invalid_version')
 def test_wrong_performer_rejected(self):
  self.assertEqual(r.resolve_query('Heartbeat - Someone Else (Camp, 2011)',catalog_provider=self.provider())['code'],'invalid_version')
 def test_parenthesized_exact_song_name_not_stripped(self):
  p=mock.Mock(return_value=[{'track':track(title='Heartbeat (Again)'),'created':1}])
  self.assertTrue(r.resolve_query('Heartbeat (Again) Childish Gambino',catalog_provider=p)['ok'])
  p.assert_called_once_with(query='Heartbeat (Again) Childish Gambino')
 def test_annotated_empty_catalog_is_invalid_version(self):
  self.assertEqual(r.resolve_query('Heartbeat - Childish Gambino (Camp, 2011)',catalog_provider=lambda **kw:[])['code'],'invalid_version')
 def test_annotated_exception_never_fallback_or_leaks(self):
  result=r.resolve_query('Heartbeat - Childish Gambino (Camp, 2011)',catalog_provider=mock.Mock(side_effect=RuntimeError('SECRET signed url')))
  self.assertEqual(result['code'],'invalid_version');self.assertNotIn('SECRET',json.dumps(result))

class CatalogValidationTests(unittest.TestCase):
 def setUp(self):
  import tempfile,hashlib
  import numpy as np
  import soundfile as sf
  from pathlib import Path
  from hq_cover import artifact_cache as c
  self.c=c;self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name);self.catalog=root/'catalog';self.entry=self.catalog/'entry';self.entry.mkdir(parents=True);self.source_dir=root/'source';self.source_dir.mkdir()
  p=self.source_dir/'source.flac';sf.write(p,np.zeros((48000,2)),48000,subtype='PCM_16');raw=p.read_bytes();self.sha=hashlib.sha256(raw).hexdigest();t=track();t['duration_ms']=1000
  self.record={'source':'netease','status':'verified_official_lossless','source_path':str(p),'source_sha256':self.sha,'track':t,'transcoded':False,'trial_restricted':False,'quality_response':{'id':1,'code':200,'level':'lossless','type':'flac','size':len(raw),'md5':hashlib.md5(raw).hexdigest(),'sr':48000}}
  self.fp={'digest':'policy'}
  files={}
  for name,ch in [('cover',2),('vocal',1)]:
   a=self.entry/(name+'.wav');sf.write(a,np.zeros((48000,ch)),48000,subtype='PCM_24');files[name]=c._audio(a,ch)
  self.meta={'schema':c.SCHEMA,'complete':True,'fingerprint':self.fp,'identity':{'track_id':1,'source_sha256':self.sha,'voice_id':c.VOICE,'mode':'full','pipeline':c.PIPELINE,'singing_boundary_policy':c.BOUNDARY_POLICY},'created':1,'singing_boundary_policy':c.BOUNDARY_POLICY,'source_duration_seconds':1.,'files':files}
  self.save()
  for obj,key,value in [(c,'CACHE',self.catalog),(c,'fingerprint',lambda:self.fp),(source,'_cache_records',lambda:[(self.source_dir/'source_metadata.json',self.record,t)])]:
   patch=mock.patch.object(obj,key,value);patch.start();self.addCleanup(patch.stop)
 def save(self): (self.entry/'metadata.json').write_text(json.dumps(self.meta),encoding='utf-8')
 def test_release_year_evidence_exact_binding(self):
  review={'status':'official_studio_catalog_identity_verified','official_details':[{'track':self.record['track'],'album_metadata':{'id':3,'name':'Camp','publishTime':1321286400000}}]}
  p=self.source_dir/'identity_review.json';p.write_text(json.dumps(review),encoding='utf-8')
  result=r.resolve_query('Heartbeat - Childish Gambino (Camp, 2011)')
  self.assertTrue(result['ok']);self.assertEqual(result['version_confirmation']['release_year'],2011)
  review['official_details'][0]['album_metadata']['id']=999;p.write_text(json.dumps(review),encoding='utf-8')
  self.assertEqual(r.resolve_query('Heartbeat - Childish Gambino (Camp, 2011)')['code'],'invalid_version')
 def test_release_year_wrong_track_or_status_rejected(self):
  review={'status':'official_studio_catalog_identity_verified','official_details':[{'track':track(999),'album_metadata':{'id':3,'name':'Camp','publishTime':1321286400000}}]}
  p=self.source_dir/'identity_review.json';p.write_text(json.dumps(review),encoding='utf-8')
  self.assertEqual(r.catalog_candidates(query='Heartbeat Childish Gambino')[0]['release_years'],[])
  review['official_details'][0]['track']=self.record['track'];review['status']='unverified';p.write_text(json.dumps(review),encoding='utf-8')
  self.assertEqual(r.catalog_candidates(query='Heartbeat Childish Gambino')[0]['release_years'],[])
 def test_valid_catalog_no_copy(self):
  self.assertEqual(r.catalog_candidates('Heartbeat')[0]['track']['id'],1)
  self.assertEqual(r.catalog_candidates(query='Heartbeat Childish Gambino')[0]['track']['id'],1)
 def test_audio_tamper_rejected(self):
  (self.entry/'cover.wav').write_bytes(b'corrupt');self.assertEqual(r.catalog_candidates('Heartbeat'),[])
 def test_policy_change_rejected(self):
  self.meta['fingerprint']={'digest':'old'};self.save();self.assertEqual(r.catalog_candidates('Heartbeat'),[])
 def test_source_tamper_rejected(self):
  from pathlib import Path
  Path(self.record['source_path']).write_bytes(b'corrupt');self.assertEqual(r.catalog_candidates('Heartbeat'),[])
 def test_metadata_success_flag_insufficient(self):
  self.meta['files']['vocal']['sha256']='0'*64;self.save();self.assertEqual(r.catalog_candidates('Heartbeat'),[])

if __name__=='__main__':unittest.main()
