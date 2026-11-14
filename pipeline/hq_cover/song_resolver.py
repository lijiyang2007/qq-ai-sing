"""Resolve a QQ song request without downloading audio or producing/sending a cover.
The caller owns the 120-second artist clarification timer. auto_select is an explicit
post-timeout policy input, never enabled merely because a title is present.
"""
import argparse,contextlib,json,math,re,sys,unicodedata
from datetime import datetime,timezone
from pathlib import Path
if __package__ in (None,''):
    sys.path.insert(0,str(Path(__file__).resolve().parent.parent));__package__='hq_cover'
from . import source


def _safe(value):
    return ''.join(c for c in str(value) if ord(c)>=32)[:160]


def _public(track):
    return {'track_id':track['id'],'title':_safe(track['title']),
            'artists':[_safe(a['name']) for a in track['artists']],
            'album':_safe(track['album']['name'])}


def _matches(track,title,artist):
    if source._normalize(track['title'])!=source._normalize(title):return False
    # Never silently choose Live, remix, karaoke, cover or sped-up versions.
    if source._VARIANT.search(track['title']) or source._variant_unrequested(title,track):return False
    return not artist or source._identity_match(title+' '+artist,track)


def _verified_release_years(manifest,track):
    """Only local official album-detail evidence tied to this exact track and album.
    No inferred year, arbitrary file path, album-name substring or online permission expansion.
    """
    evidence=Path(manifest).parent/'identity_review.json'
    try:
        # Refuse symlink/junction escape and oversized/corrupted evidence.
        if evidence.resolve().parent!=Path(manifest).parent.resolve() or evidence.stat().st_size>1024*1024:return []
        report=json.loads(evidence.read_text(encoding='utf-8'))
        if report.get('status')!='official_studio_catalog_identity_verified':return []
        years=set()
        for row in report.get('official_details',[]):
            if not isinstance(row,dict):continue
            recorded=source._track(row.get('track',{}));album=row.get('album_metadata',{})
            if recorded!=track or not isinstance(album,dict):continue
            if album.get('id')!=track['album']['id'] or source._normalize(album.get('name',''))!=source._normalize(track['album']['name']):continue
            stamp=album.get('publishTime')
            if type(stamp) not in (int,float) or not math.isfinite(stamp):continue
            dt=datetime.fromtimestamp(stamp/1000,timezone.utc)
            if 1900<=dt.year<=datetime.now(timezone.utc).year+1:years.add(dt.year)
        return sorted(years)
    except (OSError,ValueError,TypeError,KeyError,AttributeError,OverflowError):return []


def _version_query(query):
    """Parse only an entire terminal '(album, YYYY)' qualifier, never strip arbitrary brackets.
    A title's own parentheses are preserved in base; all studio-only checks still apply.
    """
    value=unicodedata.normalize('NFKC',query).strip()
    match=re.fullmatch(r'(?P<base>.+?)\s+\((?P<album>[^()]+?),\s*(?P<year>\d{4})\)',value)
    if not match:return None
    base=match['base'].strip();album=match['album'].strip();year=int(match['year'])
    if not album or not 1900<=year<=datetime.now(timezone.utc).year+1:return None
    return {'base':base,'album':album,'year':year}


def catalog_candidates(title=None,artist=None,*,query=None):
    """Read verified full-cover catalog; no copying or trusting metadata success flags alone."""
    from . import artifact_cache as cache
    if not cache.CACHE.is_dir():return []
    fp=cache.fingerprint();records=source._cache_records();found=[]
    for candidate in sorted(cache.CACHE.glob('*/metadata.json')):
        try:
            mp=cache._inside(candidate,cache.CACHE);meta=cache._read(mp);identity=meta['identity']
            if meta.get('schema')!=cache.SCHEMA or meta.get('complete') is not True or meta.get('fingerprint')!=fp:continue
            if identity.get('voice_id')!=cache.VOICE or identity.get('mode')!='full' or identity.get('pipeline')!=cache.PIPELINE or identity.get('singing_boundary_policy')!=cache.BOUNDARY_POLICY:continue
            created=meta.get('created')
            if type(created) not in (int,float) or not math.isfinite(created):continue
            for manifest,record,track in records:
                sha=record.get('source_sha256') or (record.get('download') or {}).get('sha256')
                if track['id']!=identity.get('track_id') or sha!=identity.get('source_sha256'):continue
                if query is not None:
                    if not source._identity_match(query,track) or source._VARIANT.search(track['title']) or source._variant_unrequested(query,track):continue
                elif not _matches(track,title,artist):continue
                if record.get('transcoded') is not False or record.get('trial_restricted') is not False:continue
                # Ensure the saved source still verifies against official lossless checks.
                raw=Path(record.get('source_path') or record.get('download_path',''))
                sp=cache._inside(raw if raw.is_absolute() else manifest.parent/raw,manifest.parent)
                quality=source._quality(record.get('quality_response',{}),track['id'])
                source.validate_lossless(sp,track,quality,sha)
                normalized=dict(record,source_sha256=sha,track=track,lyrics=source._cached_lyrics(record.get('lyrics'),track['id']))
                duration=cache._verified_source_duration(normalized,sp)
                if abs(float(meta.get('source_duration_seconds',-1))-duration)>.02:raise cache.CacheRejected('Catalog source duration mismatch')
                for name,ch in [('cover',2),('vocal',1)]:
                    path=cache._inside(mp.parent/(name+'.wav'),mp.parent)
                    actual=cache._audio(path,ch,meta['files'][name]['sha256'],duration)
                    if actual!=meta['files'][name]:raise cache.CacheRejected('Catalog audio metadata mismatch')
                if cache.fingerprint()!=fp:raise cache.CacheRejected('Catalog policy changed')
                found.append({'track':track,'created':float(created),'release_years':_verified_release_years(manifest,track)})
                break
        except (ValueError,TypeError,KeyError,AttributeError,IndexError,OverflowError,OSError,RuntimeError):continue
    # Newest confirmed rendition first, then lower official ID, independent of filesystem order.
    found.sort(key=lambda x:(-x['created'],x['track']['id']))
    unique=[];seen=set()
    for item in found:
        if item['track']['id'] not in seen:unique.append(item);seen.add(item['track']['id'])
    return unique


def _success(track,origin,auto):
    public=_public(track)
    return {'ok':True,'song':public['title']+' '+' '.join(public['artists']),**public,
            'source':origin,'auto_selected':bool(auto),
            'selection_policy':'newest_verified_catalog_then_lowest_track_id' if origin=='catalog' else 'first_exact_studio_lossless_in_official_order',
            'availability_probed':origin=='official'}


def _failure(code,message,candidates=None,**extra):
    return {'ok':False,'code':code,'message':message,**extra,
            'candidates':[_public(x) for x in (candidates or [])[:10]]}


def _favorite_count(session,track):
    """Exact global red-heart/favorite count; never substitute search popularity."""
    try:
        payload=source._official_get(session,'/api/song/red/count',{'songId':track['id']})
        data=payload.get('data') or {};count=data.get('count')
        if payload.get('code')!=200 or type(count) is not int or count<0:return None
        return count
    except (source.SourceError,ValueError,TypeError,AttributeError,KeyError,OverflowError):return None


def _probe(session,track):
    detail=source._official_get(session,'/api/song/detail',{'id':track['id'],'ids':json.dumps([track['id']])})
    matches=[source._track(x) for x in detail.get('songs',[]) if isinstance(x,dict) and x.get('id')==track['id']]
    if len(matches)!=1 or matches[0]!=track:return False
    payload=source._official_get(session,'/api/song/enhance/player/url/v1',{'ids':json.dumps([track['id']]),'level':'lossless','encodeType':'flac'})
    rows=payload.get('data') or []
    if not isinstance(rows,list):return False
    for row in rows:
        if not isinstance(row,dict) or row.get('id')!=track['id']:continue
        try:
            source._quality(row,track['id'])
            if not isinstance(row.get('url'),str) or not source._valid_cdn(row['url']):continue
            return True
        except source.SourceError:continue
    return False


def resolve(title,artist=None,auto_select=False,*,catalog_provider=None,session_factory=None):
    """Pure injectable orchestration; returns safe JSON-compatible dictionary.
    Artist must include the complete credited artist list, as source._identity_match requires.
    No sleep: without artist and auto_select=False returns awaiting_artist/120 seconds.
    """
    if not isinstance(title,str) or not title.strip() or len(title)>160 or any(ord(c)<32 for c in title):return _failure('invalid_title','请提供有效歌名。')
    title=title.strip()
    if artist is not None and (not isinstance(artist,str) or len(artist)>160 or any(ord(c)<32 for c in artist)):return _failure('invalid_artist','请提供有效歌手名。')
    artist=(artist or '').strip() or None
    if not artist and not auto_select:return _failure('awaiting_artist','请补充歌手；等待120秒后可按已授权规则自动选择。',wait_seconds=120)
    provider=catalog_provider or catalog_candidates
    factory=session_factory or source._authorized_session
    try:
        candidates=provider(title,artist)
        tracks=[];seen=set()
        for row in sorted(candidates,key=lambda x:(-float(x['created']),x['track']['id'])):
            track=source._track(row['track'])
            if track['id'] not in seen and _matches(track,title,artist):tracks.append(track);seen.add(track['id'])
        if tracks:
            if len(tracks)>1 and not auto_select:return _failure('ambiguous','已有多个准确匹配版本，请确认版本。',tracks)
            return _success(tracks[0],'catalog',auto_select)
        session=factory()
        try:
            payload=source._official_get(session,'/api/search/get',{'s':title+(' '+artist if artist else ''),'type':1,'limit':100,'offset':0})
            result=payload.get('result') or {};raw=result.get('songs') or []
            if not isinstance(raw,list):return _failure('official_unavailable','官方搜索未返回有效候选。')
            tracks=[];seen=set()
            for item in raw[:100]:
                try:track=source._track(item)
                except (source.SourceError,AttributeError):continue
                if track['id'] not in seen and _matches(track,title,artist):tracks.append(track);seen.add(track['id'])
            if not auto_select:
                if result.get('songCount',len(raw))>len(raw):return _failure('search_incomplete','搜索结果未完整覆盖，请确认准确版本。',tracks)
                if len(tracks)>1:return _failure('ambiguous','有多个准确匹配版本，请确认版本。',tracks)
            for track in tracks:
                try:available=_probe(session,track)
                except (source.SourceError,ValueError,TypeError,AttributeError):available=False
                if available:return _success(track,'official',auto_select)
            return _failure('no_available_match','没有找到准确匹配且可用的完整无损录音室版本。',tracks)
        finally:
            with contextlib.suppress(Exception):session.close()
    except Exception:
        # Do not include exception strings: HTTP libraries may embed cookies or signed URLs.
        return _failure('resolver_unavailable','歌曲解析暂不可用，请稍后重试或确认准确版本。')


def resolve_auto_original(query,*,catalog_provider=None,session_factory=None):
    """Choose without asking: explicit original badge first; if none exists among
    exact non-variant candidates, use the exact global red-heart favorite count.
    A title-only query may compare artists; an artist-bearing query must match the full identity.
    """
    if not isinstance(query,str) or not query.strip() or len(query)>320 or any(ord(c)<32 for c in query):return _failure('invalid_query','请提供有效歌名。')
    query=query.strip();factory=session_factory or source._authorized_session
    try:
        # Do not let a single local rendition define "original". Always compare the
        # official release metadata first; the chosen track ID may reuse cache later.
        session=factory()
        try:
            payload=source._official_get(session,'/api/cloudsearch/pc',{'s':query,'type':1,'limit':100,'offset':0})
            result=payload.get('result') or {};raw=result.get('songs') or []
            if not isinstance(raw,list):return _failure('official_unavailable','官方搜索未返回有效候选。')
            candidates=[];seen=set()
            for position,item in enumerate(raw[:100]):
                try:track=source._track(item)
                except (source.SourceError,AttributeError):continue
                if track['id'] in seen or not source._identity_match(query,track,True) or not source._normalize(track['album']['name']) or source._VARIANT.search(track['title']) or source._variant_unrequested(query,track):continue
                candidates.append({'track':track,'origin':item.get('originCoverType'),'position':position});seen.add(track['id'])
            originals=[row for row in candidates if row['origin']==1]
            if originals:
                for row in originals:
                    track=row['track']
                    try:available=_probe(session,track)
                    except (source.SourceError,ValueError,TypeError,AttributeError):available=False
                    if available:
                        result=_success(track,'official',True)
                        result['selection_policy']='explicit_origin_cover_type_1_then_exact_studio_lossless_search_order'
                        return result
                return _failure('no_available_match','明确标注原版的准确版本没有可用的完整无损音源。',[row['track'] for row in originals])
            ranked=[]
            for row in candidates:
                count=_favorite_count(session,row['track'])
                if count is not None:ranked.append((count,row['position'],row['track']))
            if not ranked:return _failure('favorite_count_unavailable','没有原版标签，且无法取得准确候选的红心收藏数。',[row['track'] for row in candidates])
            count,_,track=sorted(ranked,key=lambda row:(-row[0],row[1],row[2]['id']))[0]
            try:available=_probe(session,track)
            except (source.SourceError,ValueError,TypeError,AttributeError):available=False
            if not available:return _failure('favorite_leader_unavailable','没有原版标签；收藏数最高的准确版本没有可用的完整无损音源。',[track])
            result=_success(track,'official',True)
            result['selection_policy']='no_original_badge_then_highest_exact_red_heart_count'
            result['favorite_count']=count
            return result
        finally:
            with contextlib.suppress(Exception):session.close()
    except Exception:
        return _failure('resolver_unavailable','歌曲解析暂不可用，请稍后重试。')


def _pro_identity_match(query,track):
    """Candidate-grounded natural artist phrasing; suffixes only denote a version.
    Never delete every 的 from input or ignore a user-specified artist/version.
    """
    normalized=unicodedata.normalize('NFKC',query).strip()
    title=unicodedata.normalize('NFKC',track['title']).strip()
    artists=[a['name'] for a in track['artists']]
    bases=[title]
    # Restrict relaxation to terminal brackets and explicit version suffix words.
    version_pattern=r'\s*(?:\([^()]+\)|[-—–]\s*(?:Live|Remix|Cover|伴奏|翻唱|共创版|现场版).*)\s*$'
    base=re.sub(version_pattern,'',title,flags=re.I).strip()
    if base and base!=title:bases.append(base)
    nq=source._normalize(normalized)
    for candidate in bases:
        nt=source._normalize(candidate)
        if nq==nt:return True
        for artist in artists:
            if nq == source._normalize(artist+'的'+candidate):
                return True
        if source._identity_match(normalized,dict(track,title=candidate),True):return True
    return False


def resolve_pro(query,session_factory=None):
    """Explicit pro selection: official search order, exact studio identity and lossless.
    No original badge/favorite ranking or catalog preselection. No pro-prefix parsing:
    the bridge must strip its command prefix before calling this isolated policy.
    """
    if not isinstance(query,str) or not query.strip() or len(query)>320 or any(ord(c)<32 for c in query):
        return _failure('invalid_query','请提供有效歌名。')
    query=query.strip();factory=session_factory or source._authorized_session
    try:
        session=factory()
        try:
            payload=source._official_get(session,'/api/cloudsearch/pc',{'s':query,'type':1,'limit':100,'offset':0})
            result=payload.get('result') or {};raw=result.get('songs') or []
            if not isinstance(raw,list):return _failure('official_unavailable','官方搜索未返回有效候选。')
            candidates=[];seen=set()
            for item in raw[:100]:
                try:track=source._track(item)
                except (source.SourceError,AttributeError,TypeError,ValueError):continue
                if track['id'] in seen:continue
                if not _pro_identity_match(query,track):continue
                seen.add(track['id']);candidates.append(track)
                try:available=_probe(session,track)
                except (source.SourceError,ValueError,TypeError,AttributeError,KeyError,OverflowError):available=False
                if available:
                    answer=_success(track,'official',True)
                    answer['selection_policy']='pro_any_version_exact_artist_lossless_search_order'
                    answer['allow_variant']=True
                    answer['selected_version_title']=track['title']
                    return answer
            return _failure('no_available_match','没有找到准确匹配且可用的完整无损录音室版本。',candidates)
        finally:
            with contextlib.suppress(Exception):session.close()
    except Exception:
        return _failure('resolver_unavailable','歌曲解析暂不可用，请稍后重试。')


def resolve_query(query,*,catalog_provider=None):
    """Exact catalog identity; optional terminal album/year must match official evidence.
    Unknown version qualifiers fail closed, not catalog_miss/automatic search fallback.
    """
    if not isinstance(query,str) or not query.strip() or len(query)>320 or any(ord(c)<32 for c in query):return _failure('invalid_query','请提供有效歌名和歌手。')
    query=query.strip();version=_version_query(query)
    annotated=any(c in query for c in '()（）')
    invalid=lambda:_failure('invalid_version','附加专辑或发行年份未能与已验证的官方版本一致，请确认专辑与年份；未提交制作。')
    if source._VARIANT.search(query):return invalid()
    if version and source._VARIANT.search(version['album']):return invalid()
    try:
        provider=catalog_provider or catalog_candidates
        # Exact names containing parentheses are never blindly stripped.
        base=version['base'] if version else query
        rows=provider(query=base)
        tracks=[];seen=set()
        for row in rows:
            track=source._track(row['track'])
            if not source._identity_match(base,track) or source._VARIANT.search(track['title']) or source._variant_unrequested(base,track):continue
            if version:
                if source._normalize(track['album']['name'])!=source._normalize(version['album']):continue
                years=row.get('release_years',[])
                # Conflicting release-year evidence also requires review.
                if not isinstance(years,list) or len(years)!=1 or type(years[0]) is not int or years[0]!=version['year']:continue
            if track['id'] not in seen:tracks.append(track);seen.add(track['id'])
        if len(tracks)==1:
            result=_success(tracks[0],'catalog',False)
            if version:result['version_confirmation']={'album':tracks[0]['album']['name'],'release_year':version['year'],'evidence':'local_official_album_detail','verified':True}
            return result
        if len(tracks)>1:return _failure('ambiguous','成品库有多个准确匹配版本，请确认版本。',tracks)
        if annotated:return invalid()
        return _failure('catalog_miss','未命中唯一已确认成品，交回正常音源解析。')
    except Exception:
        return invalid() if annotated else _failure('catalog_miss','成品库暂不可核验，交回正常音源解析。')


def main(argv=None):
    parser=argparse.ArgumentParser(description='Resolve song identity; never downloads or sends audio')
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('resolve');selector=p.add_mutually_exclusive_group(required=True);selector.add_argument('--title');selector.add_argument('--query');selector.add_argument('--auto-original-query');selector.add_argument('--pro-query');p.add_argument('--artist');p.add_argument('--auto-select',action='store_true')
    a=parser.parse_args(argv)
    # Suppress helper output entirely: library debug output could contain credentials.
    # Only our safe result is printed, no raw diagnostic strings.
    import io
    with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
        if a.query is not None:
            result=_failure('invalid_options','--query不能与--artist或--auto-select组合。') if a.artist or a.auto_select else resolve_query(a.query)
        elif a.auto_original_query is not None:
            result=_failure('invalid_options','--auto-original-query不能与--artist或--auto-select组合。') if a.artist or a.auto_select else resolve_auto_original(a.auto_original_query)
        elif a.pro_query is not None:
            result=_failure('invalid_options','--pro-query不能与--artist或--auto-select组合。') if a.artist or a.auto_select else resolve_pro(a.pro_query)
        else:result=resolve(a.title,a.artist,a.auto_select)
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))
    return 0

if __name__=='__main__':raise SystemExit(main())
