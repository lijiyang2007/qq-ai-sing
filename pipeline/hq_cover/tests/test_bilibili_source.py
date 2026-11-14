"""CPU-only isolated fixtures; every HTTP operation mocked, no QQ/GPU/cookies."""
import copy,hashlib,io,json,socket,tempfile,unittest
from pathlib import Path
from unittest import mock
import numpy as np
import soundfile as sf
from hq_cover import bilibili_source as b

BV='BV1xx411c7mu';URL=f'https://www.bilibili.com/video/{BV}/'
class Response:
 def __init__(self,data=b'x',status=200,headers=None):self.status=status;self.headers=headers or {'Content-Length':str(len(data))};self.data=io.BytesIO(data);self.closed=False
 def read(self,n):return self.data.read(n)
 def close(self):self.closed=True

def view():return {'bvid':BV,'aid':170001,'title':'测试视频','rights':{},'pages':[{'cid':11,'page':1,'duration':2,'part':'第一P'},{'cid':22,'page':2,'duration':2,'part':'第二P'}]}
def play():return {'timelength':2000,'dash':{'duration':2,'audio':[{'bandwidth':100,'baseUrl':'https://a.bilivideo.com/lower?secret=signed','codecs':'flac'},{'bandwidth':200,'baseUrl':'https://b.bilivideo.cn/higher?secret=signed','codecs':'flac'}]}}

class URLTests(unittest.TestCase):
 def test_default_and_pages(self):
  self.assertEqual(b.normalize_video_url(URL),URL+'?p=1');self.assertEqual(b.normalize_video_url(URL+'?p=2'),URL+'?p=2')
  self.assertEqual(b.normalize_video_url('https://m.bilibili.com/video/av170001?p=2'),'https://www.bilibili.com/video/av170001/?p=2')
  self.assertEqual(b.normalize_video_url('https://b23.tv/abcdEF'),'https://b23.tv/abcdEF')
 def test_user_share_tracking_stripped_but_page_preserved(self):
  u='https://www.bilibili.com/video/BV1qZ4v6eEA9?vd_source=5da3d886e2de59e017599ea76340e758&p=2'
  self.assertEqual(b.normalize_video_url(u),'https://www.bilibili.com/video/BV1qZ4v6eEA9/?p=2')
 def test_real_b23_tracking_keys_removed_without_changing_page(self):
  keys=['buvid','from_spmid','is_story_h5','mid','plat_id','share_from','share_medium','share_plat','share_session_id','share_source','share_tag','spmid','timestamp','unique_k','up_id']
  u='https://www.bilibili.com/video/BV1mThC6LEbA/?p=2&'+'&'.join(k+'=test' for k in keys)
  self.assertEqual(b.normalize_video_url(u),'https://www.bilibili.com/video/BV1mThC6LEbA/?p=2')
  for suffix in ['&redirect=https://evil.test','&p=3','&url=https://127.0.0.1']:
   with self.assertRaises(b.BilibiliSourceError):b.normalize_video_url(u+suffix)
 def test_ssrf_credentials_port_schemes_and_not_video(self):
  for url in ['http://www.bilibili.com/video/'+BV,'https://127.0.0.1/video/'+BV,'https://localhost/video/'+BV,'https://www.bilibili.com.evil/video/'+BV,'https://x@www.bilibili.com/video/'+BV,'https://www.bilibili.com:443/video/'+BV,URL+'?p=0',URL+'?p=1&p=2',URL+'?url=https://evil',URL+'#private','https://live.bilibili.com/123','https://www.bilibili.com/bangumi/play/ep1','https://b23.tv/abc/../../x','https://b23.tv/abcde?token=abc',URL+'\n']:
   with self.subTest(url=url),self.assertRaises(b.BilibiliSourceError):b.normalize_video_url(url)
 def test_dns_reject_all_nonpublic_and_mixed_answers(self):
  for addr in ['127.0.0.1','10.0.0.1','169.254.169.254','192.168.1.1','::1','fc00::1','100.64.0.1','224.0.0.1']:
   rows=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443)),(socket.AF_INET,socket.SOCK_STREAM,6,'',(addr,443))]
   with self.subTest(addr=addr),mock.patch.object(socket,'getaddrinfo',return_value=rows),self.assertRaises(b.BilibiliSourceError):b._public_ips('api.bilibili.com')
 def test_actual_connection_pins_public_ip_and_hostname(self):
  with mock.patch.object(b,'_public_ips',return_value=['8.8.8.8']),mock.patch.object(b.urllib3,'HTTPSConnectionPool') as pool:
   pool.return_value.urlopen.return_value=Response()
   response=b._open('https://api.bilibili.com/x/web-interface/view?bvid='+BV,'api');response.close()
   self.assertEqual(pool.call_args.args,('8.8.8.8',443));self.assertEqual(pool.call_args.kwargs['server_hostname'],'api.bilibili.com');self.assertEqual(pool.call_args.kwargs['assert_hostname'],'api.bilibili.com')
   call=pool.return_value.urlopen.call_args;self.assertFalse(call.kwargs['redirect']);self.assertNotIn('Cookie',call.kwargs['headers']);self.assertNotIn('Authorization',call.kwargs['headers'])
 def test_redirect_external_private_and_credentials_refused(self):
  for location in ['https://127.0.0.1/x','https://evil.com/x','http://www.bilibili.com/video/'+BV,'https://user@www.bilibili.com/video/'+BV]:
   response=Response(status=302,headers={'Location':location})
   with self.subTest(location=location),mock.patch.object(b,'_open',return_value=response) as net,self.assertRaises(b.BilibiliSourceError):b._request('https://b23.tv/abcde','page')
   self.assertEqual(net.call_count,1);self.assertTrue(response.closed)
 def test_cdn_allowlist_not_external_lookalike(self):
  for host in ['a.bilivideo.com','b.bilivideo.cn']:self.assertTrue(b._host_allowed(host,'cdn'))
  for host in ['evilbilivideo.com','bilivideo.com.evil','x.akamaized.net','localhost']:self.assertFalse(b._host_allowed(host,'cdn'))

class ResolveTests(unittest.TestCase):
 def resolve(self,v=None,p=None,url=URL):
  calls=[]
  def api(path,params):calls.append((path,params));return copy.deepcopy(view() if v is None else v) if path.endswith('/view') else copy.deepcopy(play() if p is None else p)
  def request(link,kind,headers=None):return Response(status=206,headers={'Content-Length':'1','Content-Range':'bytes 0-0/1000'}),link
  with mock.patch.object(b,'_api',side_effect=api),mock.patch.object(b,'_request',side_effect=request) as req:
   result=b.resolve_video(url)
  return result,calls,req.call_args
 def test_page_identity_highest_bandwidth_safe_metadata(self):
  result,calls,request=self.resolve(url=URL+'?p=2');self.assertEqual(result['cid'],22);self.assertEqual(result['page'],2);self.assertEqual(calls[-1][1]['cid'],22);self.assertEqual(result['audio']['bandwidth'],200)
  self.assertNotIn('signed',json.dumps(result));self.assertNotIn('baseUrl',json.dumps(result));self.assertFalse(result['original_lossless']);self.assertEqual(request.args[1],'cdn')
 def test_invalid_page_and_identity(self):
  for kwargs in [{'url':URL+'?p=3'},{'v':dict(view(),bvid='BV1yy411c7mu')},{'v':dict(view(),pages=[{'cid':0,'page':1,'duration':2}])}]:
   with self.subTest(kwargs=kwargs),self.assertRaises(b.BilibiliSourceError):self.resolve(**kwargs)
 def test_paid_preview_and_drm_rejected(self):
  bad_views=[dict(view(),rights={k:1}) for k in ['pay','arc_pay','ugc_pay','ugc_pay_preview','is_stein_gate']]+[dict(view(),is_upower_exclusive=True)]
  for v in bad_views:
   with self.subTest(v=v),self.assertRaises(b.BilibiliSourceError):self.resolve(v=v)
  for p in [dict(play(),is_preview=True),dict(play(),is_drm=True),dict(play(),need_login=True),dict(play(),play_video_type='preview')]:
   with self.subTest(p=p),self.assertRaises(b.BilibiliSourceError):self.resolve(p=p)
 def test_incomplete_duration_and_too_long(self):
  for p in [dict(play(),timelength=10000),dict(play(),dash={'duration':10,'audio':play()['dash']['audio']})]:
   with self.assertRaises(b.BilibiliSourceError):self.resolve(p=p)
  v=view();v['pages'][0]['duration']=1201
  with self.assertRaises(b.BilibiliSourceError):self.resolve(v=v)
 def test_bad_probe_size_closed(self):
  response=Response(status=206,headers={'Content-Range':'bytes 0-0/'+str(b.MAX_BYTES+1)})
  with mock.patch.object(b,'_api',side_effect=[view(),play()]),mock.patch.object(b,'_request',return_value=(response,'https://a.bilivideo.com/x')),self.assertRaises(b.BilibiliSourceError):b.resolve_video(URL)
  self.assertTrue(response.closed)
 def test_shortlink_resolves_only_official_video(self):
  def req(url,kind,headers=None):
   if kind=='page':return Response(),URL+'?p=2'
   return Response(status=206,headers={'Content-Range':'bytes 0-0/1000'}),url
  with mock.patch.object(b,'_api',side_effect=[view(),play()]),mock.patch.object(b,'_request',side_effect=req):self.assertEqual(b.resolve_video('https://b23.tv/abcde')['page'],2)

class AcquisitionTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
 def fixture(self,mono=False):
  file=self.root/'original.flac';sr=32000;t=np.arange(sr*2)/sr;x=.1*np.sin(2*np.pi*440*t);sf.write(file,x if mono else np.column_stack((x,x*.5)),sr,subtype='PCM_16');return file
 def acquire(self,original):
  safe={'canonical_url':URL+'?p=1','bvid':BV,'cid':11,'page':1,'title':'测试','duration_seconds':2.,'audio':{'bytes':original.stat().st_size,'cdn_host':'a.bilivideo.com'}}
  def dl(url,path,canonical,size):Path(path).write_bytes(original.read_bytes())
  with mock.patch.object(b,'_resolve',return_value=(safe,'https://a.bilivideo.com/x?secret=signed')),mock.patch.object(b,'_download',side_effect=dl):return b.acquire_bilibili(URL,self.root/'run/source')
 def test_full_decode_float_pcm_and_mono_duplication(self):
  m=self.acquire(self.fixture(True));v=b.verify_bilibili_source(m,m['source_path']);self.assertEqual(v['frames'],64000);self.assertEqual(v['sample_rate'],32000);self.assertEqual(v['channels'],2);self.assertTrue(v['mono_duplicated_to_stereo']);self.assertFalse(m['lossless_original']);self.assertTrue(m['user_selected_source']);self.assertNotIn('signed',json.dumps(m));self.assertEqual(sf.info(m['source_path']).subtype,'FLOAT')
 def test_stereo_not_resampled(self):
  m=self.acquire(self.fixture());self.assertFalse(m['decoded']['mono_duplicated_to_stereo']);self.assertFalse(m['resampled'])
 def test_corrupted_download_rejected(self):
  p=self.root/'bad';p.write_bytes(b'not audio')
  with self.assertRaises(b.BilibiliSourceError):self.acquire(p)
  failure=json.loads((self.root/'run/source/source_failure.json').read_text(encoding='utf-8'));self.assertEqual(failure['status'],'failed');self.assertNotIn('signed',json.dumps(failure))
 def test_original_or_wav_tampering_rejected(self):
  m=self.acquire(self.fixture());Path(m['input_audio']['path']).write_bytes(b'tampered')
  with self.assertRaises(b.BilibiliSourceError):b.verify_bilibili_source(m,m['source_path'])
 def test_path_escape_and_provenance_tamper_rejected(self):
  m=self.acquire(self.fixture());m['input_audio']['path']=str(self.root/'outside');(self.root/'run/source/source_metadata.json').write_text(json.dumps(m))
  with self.assertRaises(b.BilibiliSourceError):b.verify_bilibili_source(m,m['source_path'])
 def test_wav_replacement_rejected_even_if_updated_file_hash(self):
  m=self.acquire(self.fixture());p=Path(m['source_path']);sf.write(p,np.zeros((64000,2)),32000,subtype='FLOAT');m['source_sha256']=b.sha256(p);(p.parent/'source_metadata.json').write_text(json.dumps(m),encoding='utf-8')
  with self.assertRaises(b.BilibiliSourceError):b.verify_bilibili_source(m,p)
 def test_source_wrong_duration_rejected(self):
  m=self.acquire(self.fixture());m['expected_duration_seconds']=10.;p=Path(m['source_path']);(p.parent/'source_metadata.json').write_text(json.dumps(m),encoding='utf-8')
  with self.assertRaises(b.BilibiliSourceError):b.verify_bilibili_source(m,p)
 def test_playlist_cannot_trigger_decoder_network(self):
  p=self.root/'playlist.bin';p.write_text('#EXTM3U\nhttps://127.0.0.1/private',encoding='utf-8')
  with mock.patch.object(b.av,'open') as decoder,self.assertRaises(b.BilibiliSourceError):b._decode(p)
  decoder.assert_not_called()
 def test_decoded_nan_rejected(self):
  p=self.fixture();frame=mock.Mock();frame.sample_rate=32000;frame.layout.channels=[1,2];frame.samples=10;frame.format.is_planar=True;frame.to_ndarray.return_value=np.full((2,10),np.nan,dtype=np.float32)
  container=mock.MagicMock();container.__enter__.return_value=container;stream=mock.Mock();stream.codec_context.name='flac';container.streams.audio=[stream];container.streams.video=[];container.decode.return_value=[frame]
  with mock.patch.object(b.av,'open',return_value=container),self.assertRaises(b.BilibiliSourceError):b._decode(p)
 def test_existing_output_never_overwritten(self):
  m=self.acquire(self.fixture())
  with self.assertRaises(FileExistsError):b.acquire_bilibili(URL,self.root/'run/source')
 def test_download_size_and_redirect_protection(self):
  for response in [Response(b'abc',headers={'Content-Length':'2'}),Response(b'abc',headers={'Content-Length':'1000'}),Response(b'abc',headers={'Content-Length':'3','Content-Encoding':'gzip'})]:
   with mock.patch.object(b,'_request',return_value=(response,'https://a.bilivideo.com/x')),self.assertRaises(b.BilibiliSourceError):b._download('https://a.bilivideo.com/x',self.root/('download'+str(id(response))),URL,3)
   self.assertTrue(response.closed)
 def test_json_hard_limit(self):
  with self.assertRaises(b.BilibiliSourceError):b._read_bytes(Response(b'abcdef'),3)

if __name__=='__main__':unittest.main()
