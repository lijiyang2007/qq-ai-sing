"""Pro is opt-in: ordered official search, no badge/hearts, all source checks intact.
Every network/session operation is mocked; no GPU, downloads, credentials or QQ.
"""
import copy,io,json,unittest
from unittest import mock
from hq_cover import song_resolver as r,source

def track(i=1,title='Heartbeat',artists=('Childish Gambino',),album='Camp',origin=None):
    t={'id':i,'title':title,'artists':[{'id':n+1,'name':a} for n,a in enumerate(artists)],'album':{'id':3,'name':album},'duration_ms':269840}
    if origin is not None:t['originCoverType']=origin
    return t

def stream(i=1,**extra):
    return {'id':i,'code':200,'level':'lossless','type':'flac','size':12345,'md5':'a'*32,'url':'https://m1.music.126.net/signed-secret.flac',**extra}

class ProResolverTests(unittest.TestCase):
    def run_query(self,tracks,query='Heartbeat',streams=None,details=None,count=1000):
        session=mock.Mock();calls=[]
        stream_map=streams if streams is not None else {t['id']:stream(t['id']) for t in tracks if isinstance(t,dict) and type(t.get('id')) is int}
        def api(s,endpoint,params):
            self.assertIs(s,session);calls.append((endpoint,params))
            if endpoint=='/api/cloudsearch/pc':
                self.assertEqual(params,{'s':query.strip(),'type':1,'limit':100,'offset':0})
                return {'result':{'songs':tracks,'songCount':count}}
            if endpoint=='/api/song/detail':
                selected=next(t for t in tracks if isinstance(t,dict) and t.get('id')==params['id'])
                return {'songs':(details or {}).get(params['id'],[selected])}
            if endpoint=='/api/song/enhance/player/url/v1':
                self.assertEqual(params['level'],'lossless');self.assertEqual(params['encodeType'],'flac')
                return {'data':[stream_map[json.loads(params['ids'])[0]]]}
            raise AssertionError('Unexpected endpoint '+endpoint)
        with mock.patch.object(source,'_official_get',side_effect=api),mock.patch.object(r,'_favorite_count',side_effect=AssertionError('pro must not query hearts')) as hearts,mock.patch.object(r,'catalog_candidates',side_effect=AssertionError('pro must not prefer catalog')) as catalog:
            answer=r.resolve_pro(query,lambda:session)
            hearts.assert_not_called();catalog.assert_not_called()
        session.close.assert_called_once()
        return answer,calls

    def test_first_exact_result_wins_without_badge(self):
        answer,calls=self.run_query([track(7,origin=2),track(2,origin=1)])
        self.assertTrue(answer['ok']);self.assertEqual(answer['track_id'],7)
        self.assertEqual(answer['selection_policy'],'pro_any_version_exact_artist_lossless_search_order')
        self.assertEqual(answer['source'],'official');self.assertTrue(answer['availability_probed'])
        self.assertNotIn('favorite_count',answer)
        self.assertEqual([p['id'] for e,p in calls if e.endswith('/detail')],[7])

    def test_unavailable_original_can_fall_through_only_pro(self):
        answer,calls=self.run_query([track(1,origin=1),track(2,origin=0)],streams={1:stream(1,url=None),2:stream(2)})
        self.assertTrue(answer['ok']);self.assertEqual(answer['track_id'],2)

    def test_artist_is_exact_and_not_partial(self):
        rows=[track(1,artists=('Else',)),track(2,artists=('Childish Gambino','Guest')),track(3)]
        answer,calls=self.run_query(rows,'Heartbeat Childish Gambino')
        self.assertEqual(answer['track_id'],3)
        self.assertEqual([p['id'] for e,p in calls if e.endswith('/detail')],[3])

    def test_title_and_all_duet_artists_match(self):
        answer,_=self.run_query([track(1,artists=('Childish Gambino','Guest'))],'Heartbeat Childish Gambino / Guest')
        self.assertTrue(answer['ok'])

    def test_exact_title_any_album_version_allowed(self):
        rows=[track(1,title='Heartbeats'),track(2,title='Heartbeat Live'),track(3,album='Camp Remix'),track(4,album='Camp karaoke'),track(6)]
        answer,calls=self.run_query(rows)
        self.assertEqual(answer['track_id'],3)
        self.assertEqual([p['id'] for e,p in calls if e.endswith('/detail')],[3])

    def test_live_remix_cover_and_instrumental_versions_allowed(self):
        for suffix in ['Live','Remix','Cover','共创版','伴奏']:
            t=track(1,title='Heartbeat ('+suffix+')')
            for query in ['Heartbeat',t['title']]:
                answer,calls=self.run_query([t],query)
                self.assertTrue(answer['ok']);self.assertTrue(answer['allow_variant'])

    def test_real_artist_possessive_forms(self):
        rows=[track(1,title='Let Me Go',artists=('别人',)),track(2,title='Let Me Go(共创版)',artists=('罐装毕加索',))]
        for query in ['罐装毕加索的Let Me Go','罐装毕加索的Let Me Go(共创版)']:
            answer,_=self.run_query(rows,query);self.assertEqual(answer['track_id'],2)
        self.assertFalse(self.run_query(rows,'罐装毕加索的Let Me Go(Remix)')[0]['ok'])

    def test_possessive_artist_must_match_and_title_de_is_not_deleted(self):
        self.assertFalse(self.run_query([track(1,title='我的歌',artists=('甲',))],'乙的我的歌')[0]['ok'])
        self.assertTrue(self.run_query([track(1,title='我的歌',artists=('甲',))],'甲的我的歌')[0]['ok'])

    def test_bad_and_duplicate_candidates_skipped(self):
        rows=[None,{},'bad',track(1),track(1),track(2)]
        answer,calls=self.run_query(rows,streams={1:stream(1,freeTrialInfo={}),2:stream(2)})
        self.assertEqual(answer['track_id'],2)
        self.assertEqual([p['id'] for e,p in calls if e.endswith('/detail')],[1,2])

    def test_invalid_first_duplicate_does_not_block_valid(self):
        bad={'id':1,'title':'Heartbeat'}
        answer,calls=self.run_query([bad,track(1),track(2)],details={1:[track(1)]})
        self.assertEqual(answer['track_id'],1)

    def test_detail_identity_change_skips_candidate(self):
        answer,calls=self.run_query([track(1),track(2)],details={1:[track(1,artists=('Someone Else',))]})
        self.assertEqual(answer['track_id'],2)
        self.assertEqual([json.loads(p['ids'])[0] for e,p in calls if e.endswith('/url/v1')],[2])

    def test_detail_missing_or_duplicate_rejected(self):
        for values in [[],[track(1),track(1)]]:
            with self.subTest(values=values):
                answer,calls=self.run_query([track(1)],details={1:values})
                self.assertFalse(answer['ok']);self.assertFalse(any(e.endswith('/url/v1') for e,p in calls))

    def test_all_original_lossless_and_trial_guards_preserved(self):
        bad_rows=[{'url':None},{'url':'https://evil.test/signed-secret.flac'}, {'level':'standard'},{'type':'mp3'},{'size':None},{'md5':None},{'freeTrialInfo':{}},{'freeTrialPrivilege':{'resConsumable':True}},{'code':403}]
        for bad in bad_rows:
            with self.subTest(bad=bad):
                answer,_=self.run_query([track(1)],streams={1:stream(1,**bad)})
                self.assertFalse(answer['ok']);self.assertNotIn('signed-secret',json.dumps(answer))

    def test_search_first_hundred_only(self):
        rows=[track(i+1,title='Not Exact') for i in range(100)]+[track(101)]
        answer,calls=self.run_query(rows)
        self.assertFalse(answer['ok']);self.assertEqual(len(calls),1)

    def test_empty_or_nonlist_search_safe(self):
        for raw in [[],{},'bad']:
            with self.subTest(raw=raw):
                answer,_=self.run_query(raw);self.assertFalse(answer['ok'])

    def test_bad_input_has_no_side_effect(self):
        factory=mock.Mock()
        for query in [None,'','  ','a\nsecret','a'*321,123]:
            self.assertEqual(r.resolve_pro(query,factory)['code'],'invalid_query')
        factory.assert_not_called()

    def test_pro_prefix_not_stripped(self):
        answer,calls=self.run_query([track(1)],'pro Heartbeat')
        self.assertFalse(answer['ok']);self.assertEqual(len(calls),1)

    def test_no_sensitive_exception_output(self):
        result=r.resolve_pro('Heartbeat',mock.Mock(side_effect=RuntimeError('Cookie SECRET https://signed')))
        self.assertEqual(result['code'],'resolver_unavailable');self.assertNotIn('SECRET',json.dumps(result))

    def test_cli_one_safe_json_and_no_other_policy(self):
        output=io.StringIO()
        def pro(query):
            print('SECRET DEBUG');return {'ok':True,'track_id':123,'selection_policy':'pro_any_version_exact_artist_lossless_search_order'}
        with mock.patch('sys.stdout',output),mock.patch.object(r,'resolve_pro',side_effect=pro) as resolver,mock.patch.object(r,'resolve_auto_original') as normal:
            self.assertEqual(r.main(['resolve','--pro-query','Heartbeat']),0)
        resolver.assert_called_once_with('Heartbeat');normal.assert_not_called()
        self.assertEqual(len(output.getvalue().splitlines()),1);self.assertNotIn('SECRET',output.getvalue());self.assertEqual(json.loads(output.getvalue())['track_id'],123)

    def test_cli_conflicting_selectors_rejected(self):
        for selector in ['--title','--query','--auto-original-query']:
            with self.subTest(selector=selector),mock.patch('sys.stderr',io.StringIO()),self.assertRaises(SystemExit):
                r.main(['resolve','--pro-query','Heartbeat',selector,'Heartbeat'])

    def test_cli_artist_and_auto_select_rejected_without_resolve(self):
        for flags in [['--artist','Singer'],['--auto-select']]:
            with self.subTest(flags=flags),mock.patch('sys.stdout',new_callable=io.StringIO) as out,mock.patch.object(r,'resolve_pro') as pro:
                r.main(['resolve','--pro-query','Heartbeat',*flags]);self.assertEqual(json.loads(out.getvalue())['code'],'invalid_options');pro.assert_not_called()

if __name__=='__main__':unittest.main()
