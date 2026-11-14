"""Explicit user-selected Bilibili lossy-source exception. No GPU/QQ/cookies/subprocess.
Only public free BV/av video pages, default P1. Decoded float WAV is not original lossless.
HTTP uses validated public IPs pinned in TLS pools, with original hostname verification.
"""
from __future__ import annotations
import argparse,hashlib,ipaddress,json,math,os,re,socket,time
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit,parse_qsl,urljoin,urlencode
import numpy as np
import soundfile as sf
import av
import urllib3

MAX_SECONDS=1200
MAX_BYTES=512*1024*1024
MAX_JSON=2*1024*1024
MAX_DOWNLOAD_SECONDS=1800
VIDEO_HOSTS=frozenset({'www.bilibili.com','bilibili.com','m.bilibili.com'})
SHORT_HOST='b23.tv'
API_HOST='api.bilibili.com'
# Only Bilibili-operated video domains; external Akamai is deliberately NOT enabled.
CDN_SUFFIXES=('bilivideo.com','bilivideo.cn')
BV_RE=re.compile(r'^BV[0-9A-Za-z]{10}$')
UA='Mozilla/5.0 HQCoverBilibili/1.0'

class BilibiliSourceError(RuntimeError):
    def __init__(self,code,message):self.code=code;super().__init__(message)

def require(ok,code,message):
    if not ok:raise BilibiliSourceError(code,message)

def sha256(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(4*1024*1024),b''):h.update(b)
    return h.hexdigest()

def _split(url):
    require(isinstance(url,str) and 0<len(url)<=8192 and not re.search(r'[\x00-\x20\x7f\\]',url),'invalid_url','视频链接格式无效。')
    try:
        p=urlsplit(url);port=p.port
    except ValueError:raise BilibiliSourceError('invalid_url','视频链接格式无效。') from None
    require(p.scheme=='https' and p.hostname and p.username is None and p.password is None and port is None,'invalid_url','仅允许无凭据、无端口的HTTPS官方链接。')
    require(p.netloc==p.hostname,'invalid_url','链接主机格式无效。')
    return p

def normalize_video_url(url):
    p=_split(url)
    require(not p.fragment,'invalid_url','视频链接不能包含片段或隐藏参数。')
    if p.hostname==SHORT_HOST:
        require(re.fullmatch(r'/[A-Za-z0-9]{4,32}/?',p.path) and not p.query,'invalid_short_url','短链接格式无效。')
        return 'https://b23.tv/'+p.path.strip('/')
    require(p.hostname in VIDEO_HOSTS,'invalid_host','仅支持哔哩哔哩官方BV/av视频链接。')
    match=re.fullmatch(r'/video/(BV[0-9A-Za-z]{10}|av[1-9][0-9]{0,19})/?',p.path)
    require(match is not None,'unsupported_video','只支持普通BV/av视频，不支持直播、番剧或付费课程。')
    pairs=parse_qsl(p.query,keep_blank_values=True,strict_parsing=False)
    tracking={'vd_source','spm_id_from','from_spmid','share_source','share_medium','share_plat','share_session_id','timestamp','is_story_h5','buvid','mid','plat_id','share_from','share_tag','spmid','unique_k','up_id'}
    require(all(k=='p' or k in tracking for k,v in pairs),'invalid_query','视频链接包含不支持的参数。')
    pairs=[(k,v) for k,v in pairs if k=='p']
    require(len(pairs)<=1,'invalid_query','视频链接存在重复分P参数。')
    page=1
    if pairs:
        require(re.fullmatch(r'[1-9][0-9]{0,3}',pairs[0][1]) is not None,'invalid_page','分P参数无效。');page=int(pairs[0][1])
    return 'https://www.bilibili.com/video/'+match[1]+'/?p='+str(page)

def _host_allowed(host,kind):
    if kind=='page':return host in VIDEO_HOSTS or host==SHORT_HOST
    if kind=='api':return host==API_HOST
    return any(host==suffix or host.endswith('.'+suffix) for suffix in CDN_SUFFIXES)

def _public_ips(host):
    try:records=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
    except OSError:raise BilibiliSourceError('network','官方资源域名暂不可解析。') from None
    values=[]
    for row in records:
        raw=row[4][0]
        try:address=ipaddress.ip_address(raw)
        except ValueError:raise BilibiliSourceError('ssrf','域名地址无效。') from None
        require(address.is_global and not address.is_multicast and not address.is_unspecified,'ssrf','拒绝访问本机、内网或保留地址。')
        if raw not in values:values.append(raw)
    require(bool(values),'network','官方资源域名未返回地址。')
    return values

class _Response:
    def __init__(self,raw,pool):self.raw=raw;self.pool=pool;self.status=raw.status;self.headers=raw.headers
    def read(self,n):return self.raw.read(n,decode_content=False)
    def close(self):
        try:self.raw.close()
        finally:self.pool.close()

def _open(url,kind,headers=None):
    p=_split(url)
    require(not p.fragment and _host_allowed(p.hostname,kind),'ssrf','资源或跳转离开允许的官方域名。')
    ips=_public_ips(p.hostname)
    # The actual TCP target is a vetted literal IP. No second hostname lookup.
    pool=urllib3.HTTPSConnectionPool(ips[0],443,server_hostname=p.hostname,assert_hostname=p.hostname,cert_reqs='CERT_REQUIRED',timeout=urllib3.Timeout(connect=15,read=35),retries=False)
    request_headers={'Host':p.hostname,'User-Agent':UA,'Accept-Encoding':'identity'}
    for k,v in (headers or {}).items():
        require(k in ('Referer','Range'),'headers','不允许传递凭据或任意请求头。');request_headers[k]=v
    try:
        raw=pool.urlopen('GET',urlunsplit(('','',p.path or '/',p.query,'')),headers=request_headers,redirect=False,preload_content=False,retries=False)
        return _Response(raw,pool)
    except Exception:
        pool.close();raise BilibiliSourceError('network','官方资源连接失败。') from None

def _request(url,kind,headers=None,max_redirects=4):
    for attempt in range(max_redirects+1):
        response=_open(url,kind,headers)
        if response.status in (301,302,303,307,308):
            location=response.headers.get('Location');response.close()
            require(isinstance(location,str) and attempt<max_redirects,'redirect','官方资源跳转无效或过多。')
            url=urljoin(url,location)
            p=_split(url);require(not p.fragment and _host_allowed(p.hostname,kind),'ssrf','资源跳转离开允许的官方域名。')
            continue
        return response,url
    raise BilibiliSourceError('redirect','资源跳转过多。')

def _read_bytes(response,limit):
    raw_length=response.headers.get('Content-Length')
    if raw_length is not None:
        try:length=int(raw_length)
        except ValueError:raise BilibiliSourceError('size','资源大小字段无效。') from None
        require(0<=length<=limit,'size','资源超过允许大小。')
    require(response.headers.get('Content-Encoding','identity').lower() in ('','identity'),'encoding','不支持压缩或混淆的资源响应。')
    blocks=[];size=0
    while True:
        b=response.read(min(65536,limit+1-size))
        if not b:break
        size+=len(b);require(size<=limit,'size','资源超过允许大小。');blocks.append(b)
    return b''.join(blocks)

def _api(path,params):
    require(path in ('/x/web-interface/view','/x/player/playurl'),'api','不允许的接口。')
    response,_=_request('https://'+API_HOST+path+'?'+urlencode(params),'api')
    try:
        require(response.status==200,'api_unavailable','官方视频接口不可用。')
        try:body=json.loads(_read_bytes(response,MAX_JSON))
        except (ValueError,UnicodeError):raise BilibiliSourceError('api_data','官方接口数据无效。') from None
    finally:response.close()
    require(isinstance(body,dict) and body.get('code')==0 and isinstance(body.get('data'),dict),'api_denied','视频不可公开访问或接口拒绝。')
    return body['data']

def _unrestricted(view,play=None):
    rights=view.get('rights') or {}
    require(isinstance(rights,dict),'restricted','视频权限信息无效。')
    for k in ('pay','arc_pay','ugc_pay','ugc_pay_preview','is_stein_gate'):
        require(not rights.get(k),'restricted','不支持付费、试看或互动视频。')
    for k in ('is_upower_exclusive','is_upower_pay','is_chargeable_season','is_live_playback','is_paid'):
        require(not view.get(k),'restricted','不支持充电专属、付费或直播内容。')
    require(view.get('state',0)==0,'restricted','视频未公开或状态异常。')
    require(not view.get('redirect_url'),'restricted','不支持跳转番剧或其他付费内容。')
    if play is not None:
        require(not play.get('drm') and not play.get('drm_info'),'restricted','不支持DRM音轨。')
        require(not any('试看' in str(x) or 'preview' in str(x).lower() for x in (play.get('accept_description') or [])),'restricted','不支持试看描述的音轨。')
        for k in ('is_preview','isPreview','is_drm','drm_tech_type','need_login','need_pay','is_pay','is_upower_exclusive'):
            require(not play.get(k),'restricted','不支持试看、付费、登录受限或DRM音轨。')
        require(play.get('play_video_type') not in ('preview','PLAY_PREVIEW','none'),'restricted','不支持预览音轨。')

def _resolve(url):
    canonical=normalize_video_url(url)
    if urlsplit(canonical).hostname==SHORT_HOST:
        response,final=_request(canonical,'page')
        try:require(response.status==200,'shortlink','短链接无法解析。')
        finally:response.close()
        require(urlsplit(final).hostname in VIDEO_HOSTS,'shortlink','短链接没有指向普通视频。')
        canonical=normalize_video_url(final)
    p=urlsplit(canonical);ident=p.path.split('/')[2];page=int(dict(parse_qsl(p.query)).get('p','1'))
    view=_api('/x/web-interface/view',{'bvid':ident} if ident.startswith('BV') else {'aid':ident[2:]})
    _unrestricted(view)
    bvid=view.get('bvid');require(isinstance(bvid,str) and BV_RE.fullmatch(bvid),'identity','官方视频标识无效。')
    if ident.startswith('BV'):require(bvid==ident,'identity','视频身份发生改变。')
    else:require(str(view.get('aid'))==ident[2:],'identity','视频身份发生改变。')
    pages=view.get('pages');require(isinstance(pages,list) and 1<=page<=len(pages),'page','指定分P不存在。')
    part=pages[page-1];require(isinstance(part,dict) and part.get('page')==page and type(part.get('cid')) is int and part['cid']>0,'page','分P身份无效。')
    duration=part.get('duration');require(type(duration) in (int,float) and math.isfinite(duration) and 0<duration<=MAX_SECONDS,'duration','只接受20分钟以内的完整分P音轨。')
    cid=part['cid'];canonical=f'https://www.bilibili.com/video/{bvid}/?p={page}'
    play=_api('/x/player/playurl',{'bvid':bvid,'cid':cid,'fnval':16,'fnver':0,'fourk':0})
    _unrestricted(view,play)
    require(type(play.get('timelength')) in (int,float) and math.isfinite(play['timelength']) and 0<play['timelength']/1000<=MAX_SECONDS and abs(play['timelength']/1000-duration)<=2.0,'duration','播放接口不是该分P完整长度。')
    precise_duration=play['timelength']/1000
    dash=play.get('dash');require(isinstance(dash,dict) and type(dash.get('duration')) in (int,float) and abs(dash['duration']-duration)<=2.0,'duration','完整DASH音轨不可用。')
    candidates=[]
    for audio in dash.get('audio') or []:
        if not isinstance(audio,dict):continue
        bandwidth=audio.get('bandwidth');link=audio.get('baseUrl') or audio.get('base_url')
        if type(bandwidth) is not int or bandwidth<=0 or not isinstance(link,str):continue
        try:pp=_split(link)
        except BilibiliSourceError:continue
        if pp.fragment or not _host_allowed(pp.hostname,'cdn'):continue
        if audio.get('drm') or audio.get('ContentProtection') or audio.get('content_protection'):continue
        candidates.append((bandwidth,audio,link))
    require(bool(candidates),'audio','没有可公开取得的完整DASH音轨。')
    bandwidth,audio,link=max(candidates,key=lambda x:x[0])
    response,final=_request(link,'cdn',{'Referer':canonical,'Range':'bytes=0-0'})
    try:
        require(response.status in (200,206),'audio_denied','音轨访问被拒绝。')
        total=response.headers.get('Content-Length')
        if response.status==206:
            cr=response.headers.get('Content-Range','');match=re.fullmatch(r'bytes 0-0/(\d+)',cr)
            require(match is not None,'audio_probe','音轨探测范围无效。');total=match[1]
        require(total is not None and str(total).isdigit() and 0<int(total)<=MAX_BYTES,'size','音轨大小未知或超过512MB。')
        probe=response.read(1);require(len(probe)==1,'audio_probe','音轨为空。')
    finally:response.close()
    title=str(part.get('part') or view.get('title') or bvid)
    title=''.join(c for c in title if ord(c)>=32)[:200]
    codec_hint=str(audio.get('codecs',''))
    if not re.fullmatch(r'[A-Za-z0-9.,_-]{0,80}',codec_hint):codec_hint=''
    safe={'canonical_url':canonical,'bvid':bvid,'cid':cid,'page':page,'title':title,'duration':float(precise_duration),'duration_seconds':float(precise_duration),'audio':{'bandwidth':bandwidth,'codec_hint':codec_hint,'bytes':int(total),'cdn_host':urlsplit(final).hostname},'source':'bilibili','original_lossless':False,'lossless_original':False,'user_selected_source':True}
    return safe,final

def resolve_video(url):
    try:return _resolve(url)[0]
    except BilibiliSourceError:raise
    except Exception:raise BilibiliSourceError('resolve_failed','视频解析失败，未继续获取音轨。') from None

def _download(url,path,canonical,expected_size):
    response,_=_request(url,'cdn',{'Referer':canonical});start=time.monotonic();size=0
    try:
        require(response.status==200,'download','音轨下载未返回完整文件。')
        length=response.headers.get('Content-Length');require(length is not None and str(length).isdigit() and int(length)==expected_size<=MAX_BYTES,'size','音轨下载大小与预检不一致。')
        require(response.headers.get('Content-Encoding','identity').lower() in ('','identity'),'encoding','不接受压缩传输。')
        with Path(path).open('xb') as f:
            while True:
                require(time.monotonic()-start<=MAX_DOWNLOAD_SECONDS,'timeout','音轨下载超时。')
                b=response.read(65536)
                if not b:break
                size+=len(b);require(size<=expected_size and size<=MAX_BYTES,'size','音轨下载超限。');f.write(b)
            f.flush();os.fsync(f.fileno())
        require(size==expected_size,'truncated','音轨下载不完整。')
    finally:response.close()

def _container_format(path):
    with Path(path).open('rb') as f:head=f.read(64)
    if len(head)>=12 and head[4:8]==b'ftyp':return 'mov'
    if head.startswith(b'fLaC'):return 'flac'
    if head.startswith(b'OggS'):return 'ogg'
    if head.startswith(b'ID3') or (len(head)>2 and head[0]==255 and head[1]&0xe0==0xe0):return 'mp3'
    raise BilibiliSourceError('codec','只接受自包含音频容器，不接受播放列表或外部引用。')


def _decode(path,wav=None):
    count=0;sr=None;channels=None;codec=None;out=None;pcm=hashlib.sha256()
    try:
        fmt=_container_format(path)
        # Force a self-contained demuxer: never let FFmpeg interpret a malicious
        # playlist as a second network/file downloader. MOV external drefs stay off.
        with av.open(str(path),format=fmt,options={'protocol_whitelist':'file','enable_drefs':'0','use_absolute_path':'0'}) as container:
            require(len(container.streams.audio)==1 and len(container.streams.video)==0,'codec','只接受单音轨音频文件，不接受视频或多音轨。')
            stream=container.streams.audio[0];codec=stream.codec_context.name
            require(codec in ('aac','mp3','mp3float','opus','vorbis','flac','alac'),'codec','音轨编码不支持或受保护。')
            for frame in container.decode(audio=0):
                rate=frame.sample_rate;ch=len(frame.layout.channels)
                require(ch in (1,2) and type(rate) is int and 8000<=rate<=192000,'audio_format','音轨声道或采样率不支持。')
                if sr is None:
                    sr=rate;channels=ch
                    if wav is not None:out=sf.SoundFile(str(wav),mode='x',samplerate=sr,channels=2,format='WAV',subtype='FLOAT')
                require(sr==rate and channels==ch,'audio_format','音轨中途格式变化。')
                array=frame.to_ndarray();planar=frame.format.is_planar
                array=array.T if planar else array.reshape(-1,ch)
                if np.issubdtype(array.dtype,np.integer):
                    info=np.iinfo(array.dtype)
                    array=(array.astype(np.float64)-(128 if array.dtype==np.uint8 else 0))/(128 if array.dtype==np.uint8 else float(max(abs(info.min),info.max)))
                array=np.ascontiguousarray(array,dtype=np.float32)
                require(array.shape==(frame.samples,ch) and np.isfinite(array).all(),'decode','音轨解码无效或含非有限数。')
                if ch==1:array=np.repeat(array,2,axis=1)
                count+=len(array);require(count<=sr*(MAX_SECONDS+2),'duration','音轨超过20分钟。')
                require(count*2*4+4096<=MAX_BYTES,'size','解码WAV超过512MB。')
                pcm.update(array.tobytes())
                if out is not None:out.write(array)
        require(count>0 and sr is not None,'decode','音轨为空或无法解码。')
    except BilibiliSourceError:raise
    except Exception:raise BilibiliSourceError('decode','音轨损坏或无法完整解码。') from None
    finally:
        if out is not None:out.close()
    return {'frames':count,'sample_rate':sr,'channels':2,'input_channels':channels,'mono_duplicated_to_stereo':channels==1,'duration_seconds':count/sr,'pcm_sha256':pcm.hexdigest(),'codec':codec}

def acquire_bilibili(url,newdir):
    out=Path(newdir);out.mkdir(parents=True,exist_ok=False)
    try:
        resolved,link=_resolve(url);input_path=out/'input_audio.bin';wav=out/'source.wav'
        _download(link,input_path,resolved['canonical_url'],resolved['audio']['bytes'])
        decoded=_decode(input_path,wav)
        require(abs(decoded['duration_seconds']-resolved['duration_seconds'])<=max(.25,resolved['duration_seconds']*.0001),'duration','解码音轨长度与完整分P不一致，拒绝预览或截断。')
        metadata={'schema_version':1,'status':'verified_bilibili_audio','source_kind':'bilibili','source':'bilibili','original_lossless':False,'lossless_original':False,'user_selected_source':True,'title':resolved['title'],'source_path':str(wav.resolve()),'source_sha256':sha256(wav),'source_metadata_path':str((out/'source_metadata.json').resolve()),'video':{k:resolved[k] for k in ('bvid','cid','page','canonical_url')},'expected_duration_seconds':resolved['duration_seconds'],'decoded':decoded,'input_audio':{'path':str(input_path.resolve()),'sha256':sha256(input_path),'bytes':input_path.stat().st_size,'codec':decoded['codec'],'cdn_host':resolved['audio']['cdn_host']},'provenance':'public_official_view_and_playurl_no_cookie_no_preview','resampled':False,'transcoded_to_float_wav':True}
        with (out/'source_metadata.json').open('x',encoding='utf-8') as f:json.dump(metadata,f,ensure_ascii=False,indent=2,allow_nan=False)
        verify_bilibili_source(metadata,wav)
        return metadata
    except Exception as exc:
        code=exc.code if isinstance(exc,BilibiliSourceError) else 'acquire_failed'
        with (out/'source_failure.json').open('x',encoding='utf-8') as f:json.dump({'status':'failed','code':code,'message':'视频音轨未通过获取或完整性校验，未交付。'},f,ensure_ascii=False)
        if isinstance(exc,BilibiliSourceError):raise
        raise BilibiliSourceError('acquire_failed','视频音轨获取失败，未继续交付。') from None

def verify_bilibili_source(source,path):
    """Offline provenance+hash+full input decode+WAV PCM equivalence, within run/source."""
    require(isinstance(source,dict),'provenance','视频源元数据无效。')
    p=Path(path).resolve(strict=True);root=p.parent
    require(p.name=='source.wav' and root.name=='source' and Path(path).absolute()==p,'path','视频源必须位于任务source目录内且不是路径跳转。')
    meta=root/'source_metadata.json';require(meta.resolve().parent==root and meta.stat().st_size<=MAX_JSON,'path','视频元数据路径越界。')
    stored=json.loads(meta.read_text(encoding='utf-8'))
    require(stored==source,'provenance','视频源元数据与持久记录不同。')
    require(source.get('status')=='verified_bilibili_audio' and source.get('source')=='bilibili' and source.get('source_kind')=='bilibili' and source.get('original_lossless') is False and source.get('lossless_original') is False and source.get('user_selected_source') is True and source.get('resampled') is False and source.get('provenance')=='public_official_view_and_playurl_no_cookie_no_preview','provenance','视频有损来源声明不一致。')
    require(Path(source.get('source_path','')).resolve()==p and Path(source.get('source_metadata_path','')).resolve()==meta,'path','视频源路径不一致。')
    video=source.get('video',{});url=normalize_video_url(video.get('canonical_url',''))
    require(url==f'https://www.bilibili.com/video/{video.get("bvid")}/?p={video.get("page")}' and type(video.get('cid')) is int and video['cid']>0 and BV_RE.fullmatch(str(video.get('bvid',''))),'provenance','视频身份无效。')
    inp=root/'input_audio.bin';require(inp.resolve().parent==root and inp.resolve()==Path(source.get('input_audio',{}).get('path','')).resolve(),'path','原音轨路径越界。')
    item=source.get('input_audio',{});require(isinstance(item.get('cdn_host'),str) and _host_allowed(item['cdn_host'],'cdn'),'provenance','音轨来源域名记录无效。');require(0<inp.stat().st_size<=MAX_BYTES and inp.stat().st_size==item.get('bytes') and sha256(inp)==item.get('sha256'),'hash','原音轨大小或哈希改变。')
    require(sha256(p)==source.get('source_sha256'),'hash','解码WAV哈希改变。')
    decoded=_decode(inp);require(decoded==source.get('decoded') and decoded['codec']==item.get('codec'),'decode','原音轨解码结果改变。')
    expected=source.get('expected_duration_seconds');require(type(expected) in (int,float) and math.isfinite(expected) and 0<expected<=MAX_SECONDS and abs(decoded['duration_seconds']-expected)<=max(.25,expected*.0001),'duration','视频源长度无效。')
    digest=hashlib.sha256();frames=0
    with sf.SoundFile(p) as wav:
        require(wav.format=='WAV' and wav.subtype=='FLOAT' and wav.samplerate==decoded['sample_rate'] and wav.channels==2 and wav.frames==decoded['frames'],'decode','视频源WAV格式或样本数不一致。')
        while True:
            block=wav.read(480000,dtype='float32',always_2d=True)
            if not len(block):break
            require(np.isfinite(block).all(),'decode','WAV含非有限数。');frames+=len(block);digest.update(np.ascontiguousarray(block).tobytes())
    require(frames==decoded['frames'] and digest.hexdigest()==decoded['pcm_sha256'],'decode','WAV与原音轨解码PCM不一致。')
    require(sha256(p)==source['source_sha256'] and sha256(inp)==item['sha256'],'hash','验证过程中视频文件改变。')
    return decoded

def main(argv=None):
    parser=argparse.ArgumentParser();sub=parser.add_subparsers(dest='cmd',required=True)
    p=sub.add_parser('resolve');p.add_argument('--url',required=True)
    a=parser.parse_args(argv)
    try:result={'ok':True,**resolve_video(a.url)}
    except Exception as e:result={'ok':False,'code':e.code if isinstance(e,BilibiliSourceError) else 'failed','message':'视频不可解析或不符合公开完整音轨要求。'}
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))
if __name__=='__main__':main()
