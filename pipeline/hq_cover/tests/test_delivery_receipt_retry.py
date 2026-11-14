"""No network: distinguish connect-before-send from uncertain POST and read-only recovery."""
import json,os,tempfile,unittest
from pathlib import Path
from unittest import mock
import requests,numpy as np,soundfile as sf
from hq_cover.delivery import send_verified,DeliveryUncertain,DeliveryHeld,file_hash
from hq_cover.policy import PIPELINE,SINGING_BOUNDARY_POLICY
class R:
 def __init__(self,data,status=200):self.data=data;self.status_code=status;self.ok=status<400
 def json(self):return self.data
class Tests(unittest.TestCase):
 def run_case(self,kind):
  with tempfile.TemporaryDirectory() as t,mock.patch.dict(os.environ,{'QQ_CONSOLE_TOKEN':'fixture-only'}),mock.patch('hq_cover.delivery.requests.Session') as S,mock.patch('hq_cover.delivery.time.sleep'):
   p=Path(t)/'master.wav';sf.write(p,np.zeros((48000,2)),48000,subtype='PCM_24')
   q={'quality_passed':True,'pipeline':PIPELINE,'singing_boundary_policy':SINGING_BOUNDARY_POLICY,'original_source_bypass':False,'files':[{'path':str(p),'sha256':file_hash(p)}]}
   actual=[];queries=[];dry=[]
   def post(url,**kw):
    b=kw['json']
    if url.endswith('/delivery-status'):
     queries.append(b)
     if kind in ('recover','unverified'):
      return R({'ok':True,'receipt':{**b,'status':'verified','messageId':'-456'}})
     return R({'ok':True,'found':False,'receipt':None})
    if b['dryRun']:
     dry.append(b)
     if kind=='preflight' and len(dry)==1:raise requests.Timeout('temporary preflight')
     return R({'ok':True,'limits':{'maxSeconds':300,'maxBytes':20000000}})
    actual.append(b)
    if kind=='connect' and len(actual)==1:raise requests.ConnectTimeout('connection not established')
    if kind in ('recover','unknown'):raise requests.ReadTimeout('may be sent')
    if kind=='unverified':return R({'ok':True,'messageId':-456,'verified':False})
    if kind=='reject':return R({'ok':False,'deliveryState':'not_started'},403)
    return R({'ok':True,'messageId':-456,'verified':True})
   S.return_value.post.side_effect=post
   if kind in ('unknown','reject'):
    with self.assertRaises(DeliveryHeld):send_verified(p,'group:12345678',q,t)
   else:
    result=send_verified(p,'group:12345678',q,t);self.assertTrue(result['verified'])
   expected=2 if kind=='connect' else 1;self.assertEqual(len(actual),expected)
   if kind=='connect':self.assertEqual(actual[0]['deliveryId'],actual[1]['deliveryId'])
   if kind in ('recover','unverified','unknown'):
    self.assertTrue(queries);self.assertEqual(queries[0]['deliveryId'],actual[0]['deliveryId']);self.assertEqual(queries[0]['sha256'],actual[0]['sha256']);self.assertNotIn('file',queries[0])
   if kind=='unknown':self.assertEqual(len(queries),3)
   if kind=='reject':self.assertEqual(queries,[])
   manifest=json.loads((Path(t)/'qq-send-manifest.json').read_text(encoding='utf-8'));self.assertEqual(manifest['parts'][0]['delivery_id'],actual[0]['deliveryId'])
 def test_connect_failure_bounded_same_id(self):self.run_case('connect')
 def test_read_timeout_queries_never_resends(self):self.run_case('recover')
 def test_missing_readback_queries(self):self.run_case('unverified')
 def test_missing_receipt_does_not_authorize_resend(self):self.run_case('unknown')
 def test_permission_rejection_not_retried(self):self.run_case('reject')
 def test_dry_preflight_can_retry(self):self.run_case('preflight')
if __name__=='__main__':unittest.main()
