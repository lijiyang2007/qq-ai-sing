"""Explicit 320kbps QQ transport; preserve every PCM24 master sample.
Try exact-length encoding first. Only an evidenced encoder length mismatch may use
<1152 appended zero samples. No fade, tail trimming or padding-silence assumption.
"""
import hashlib,io,math
from pathlib import Path
import av
import numpy as np
import soundfile as sf

BIT_RATE=320000
ENCODER_GRANULE=1152
class TransportError(RuntimeError):pass
class TransportLengthError(TransportError):pass

def sha256(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(8*1024*1024),b''):h.update(b)
    return h.hexdigest()

def parse_mp3_gapless(path):
    """Parse our MPEG-1 Layer III file, count physical frames, check Xing/Info.
    Xing count excludes its own metadata frame; byte count includes that frame.
    Lavf uses the LAME-compatible 12-bit delay/padding layout, not guessed offsets.
    """
    raw=Path(path).read_bytes();pos=0
    if raw[:3]==b'ID3':
        if len(raw)<10 or any(x&128 for x in raw[6:10]):raise TransportError('Invalid ID3 size')
        pos=10+sum(raw[6+i]<<(7*(3-i)) for i in range(4))
        if raw[5]&16:pos+=10
    start=pos;frames=0;first_end=None;rate=None;xing=None
    while pos<len(raw):
        if pos+4>len(raw):raise TransportError('Truncated MPEG frame header')
        h=int.from_bytes(raw[pos:pos+4],'big')
        if h>>21!=2047 or (h>>19)&3!=3 or (h>>17)&3!=1:raise TransportError('Not MPEG-1 Layer III')
        bi=(h>>12)&15;ri=(h>>10)&3
        if bi!=14 or ri==3:raise TransportError('Transport bitrate/sample rate changed')
        sr=(44100,48000,32000)[ri]
        if rate is not None and sr!=rate:raise TransportError('MPEG sample rate changed')
        rate=sr
        if (h>>6)&3==3:raise TransportError('Transport unexpectedly mono')
        size=144*BIT_RATE//sr+((h>>9)&1)
        if pos+size>len(raw):raise TransportError('Truncated MPEG audio frame')
        if frames==0:
            first_end=pos+size;xing=pos+4+(0 if (h>>16)&1 else 2)+32
        frames+=1;pos+=size
    if frames<2 or xing is None or raw[xing:xing+4] not in (b'Xing',b'Info'):raise TransportError('Missing Xing/Info frame')
    flags=int.from_bytes(raw[xing+4:xing+8],'big');cursor=xing+8
    if flags&~15 or flags&3!=3:raise TransportError('Missing Xing frame/byte counts')
    count=int.from_bytes(raw[cursor:cursor+4],'big');cursor+=4
    byte_count=int.from_bytes(raw[cursor:cursor+4],'big');cursor+=4
    if flags&4:cursor+=100
    if flags&8:cursor+=4
    if cursor+24>first_end or raw[cursor:cursor+4] not in (b'LAME',b'Lavf',b'Lavc'):raise TransportError('Missing encoder gapless metadata')
    dp=raw[cursor+21:cursor+24];delay=(dp[0]<<4)|(dp[1]>>4);padding=((dp[1]&15)<<8)|dp[2]
    if count+1!=frames or byte_count!=len(raw)-start:raise TransportError('Xing counts disagree with physical MPEG stream')
    declared=count*ENCODER_GRANULE-delay-padding
    if declared<=0:raise TransportError('Invalid encoder delay/padding')
    return {'mpeg_frames':count,'physical_mpeg_frames':frames,'sample_rate':rate,'encoder_delay':delay,'encoder_padding':padding,'gapless_declared_frames':declared,'_delay_padding_offset':cursor+21,'_xing_frames_offset':xing+8}

def _encode(cover,output,start,count,sr,pad):
    """Same deterministic encoding recipe for initial output and independent re-read proof."""
    digest=hashlib.sha256();fed=0
    with sf.SoundFile(cover) as source,av.open(output,'w',format='mp3') as out:
        source.seek(start)
        stream=out.add_stream('libmp3lame',rate=sr);stream.bit_rate=BIT_RATE;stream.layout='stereo'
        while fed<count:
            pcm=source.read(min(11520,count-fed),dtype='int32',always_2d=True)
            if not len(pcm) or pcm.shape[1]!=2:raise TransportError('母带意外截断或声道改变')
            digest.update(pcm.tobytes())
            audio=(pcm.astype(np.float64)/2147483648.).astype(np.float32)
            if not np.isfinite(audio).all():raise TransportError('非有限母带音频')
            frame=av.AudioFrame.from_ndarray(np.ascontiguousarray(audio.T),format='fltp',layout='stereo');frame.sample_rate=sr
            for packet in stream.encode(frame):out.mux(packet)
            fed+=len(pcm)
        if pad:
            frame=av.AudioFrame.from_ndarray(np.zeros((2,pad),dtype=np.float32),format='fltp',layout='stereo');frame.sample_rate=sr
            for packet in stream.encode(frame):out.mux(packet)
        for packet in stream.encode(None):out.mux(packet)
    if fed!=count:raise TransportError('输入PCM覆盖不完整')
    return digest.hexdigest()

def validate_encoded(path,expected_frames,sample_rate,channels,pad_frames=0,source_path=None,source_start_frame=0):
    if type(expected_frames) is not int or expected_frames<=0 or type(pad_frames) is not int or not 0<=pad_frames<ENCODER_GRANULE:raise TransportError('Invalid content/padding frame count')
    if pad_frames and pad_frames!=(-expected_frames)%ENCODER_GRANULE:raise TransportError('Noncanonical encoder alignment padding')
    if sample_rate not in (32000,44100,48000) or channels!=2:raise TransportError('Invalid transport format')
    before=sha256(path);gapless=parse_mp3_gapless(path);frames=0;peak=0.
    with av.open(str(path)) as inp:
        stream=inp.streams.audio[0]
        if not stream.codec_context.name.startswith('mp3') or stream.codec_context.bit_rate!=BIT_RATE:raise TransportError('QQ运输格式/码率不是320kbps MP3')
        for frame in inp.decode(audio=0):
            if frame.sample_rate!=sample_rate or len(frame.layout.channels)!=channels:raise TransportError('运输解码采样率或声道改变')
            a=frame.to_ndarray()
            if not np.isfinite(a).all():raise TransportError('运输解码含NaN/Inf')
            peak=max(peak,float(np.max(np.abs(a))));frames+=frame.samples
    if gapless['sample_rate']!=sample_rate or frames!=gapless['gapless_declared_frames']:raise TransportError('实际解码与Xing/LAME声明不一致')
    if frames!=expected_frames+pad_frames:raise TransportLengthError('运输解码样本数与原内容及声明补齐不一致')
    if peak>=1:raise TransportError('运输解码发生削波；保留母带，需复核运输余量')
    pcm_hash=None
    if source_path is not None:
        info=sf.info(source_path)
        if info.format!='WAV' or info.subtype!='PCM_24' or info.samplerate!=sample_rate or info.channels!=channels or type(source_start_frame) is not int or source_start_frame<0 or source_start_frame+expected_frames>info.frames:raise TransportError('验证母带区间或格式不匹配')
        # A deterministic complete re-encode from a fresh read proves all content,
        # including quiet/hard-cut tails and padding. It needs no acoustic threshold.
        # No reference bytes go to QQ. Current codec must reproduce exact stream bytes.
        reference=io.BytesIO();pcm_hash=_encode(source_path,reference,source_start_frame,expected_frames,sample_rate,pad_frames)
        if hashlib.sha256(reference.getvalue()).hexdigest()!=before:raise TransportError('运输内容与母带确定性参考编码不一致（包括首尾），拒绝发送')
    if sha256(path)!=before:raise TransportError('运输文件在校验期间改变')
    return {'decoded_frames':frames,'content_frames':expected_frames,'encoded_input_frames':expected_frames+pad_frames,'pad_frames':pad_frames,
            'content_duration_seconds':expected_frames/sample_rate,'duration_seconds':frames/sample_rate,'padding_seconds':pad_frames/sample_rate,'decoded_peak':peak,
            'sample_rate':sample_rate,'channels':channels,'bit_rate':BIT_RATE,'sha256':before,'bytes':Path(path).stat().st_size,
            'gapless_declared_frames':gapless['gapless_declared_frames'],'mpeg_frames':gapless['mpeg_frames'],'encoder_delay':gapless['encoder_delay'],'encoder_padding':gapless['encoder_padding'],
            'source_pcm_sha256':pcm_hash,'deterministic_content_verified':source_path is not None}

def prepare_transport_parts(cover,outdir,max_bytes=19*1024*1024,max_seconds=295):
    info=sf.info(cover)
    if info.format!='WAV' or info.subtype!='PCM_24' or info.channels!=2:raise TransportError('运输只接受已验证的PCM24立体声WAV母带')
    if info.samplerate not in (32000,44100,48000):raise TransportError('不支持母带采样率；禁止自动降采样')
    if not math.isfinite(float(max_seconds)) or max_seconds<5 or max_bytes<1024*1024:raise TransportError('桥接运输限制过低')
    # Maximal regular segments align without padding, reserving no artificial silence
    # between them. Only the final residue may require a controlled aligned retry.
    limit=int(min(float(max_seconds),(int(max_bytes)-65536)/(BIT_RATE/8))*info.samplerate)
    aligned=limit//ENCODER_GRANULE*ENCODER_GRANULE
    if aligned<=0 or info.frames<=0:raise TransportError('空运输音频')
    root=Path(outdir);root.mkdir(parents=True,exist_ok=False)
    before=sha256(cover);parts=[];rows=[];start=0
    while start<info.frames:
        remaining=info.frames-start;count=remaining if remaining<=aligned else aligned
        path=root/f'part_{len(parts)+1:02d}_320kbps.mp3'
        if path.exists():raise TransportError('运输输出已存在')
        input_hash=_encode(cover,str(path),start,count,info.samplerate,0);pad=0;retried=False
        try:checks=validate_encoded(path,count,info.samplerate,2,0,cover,start)
        except TransportLengthError:
            pad=(-count)%ENCODER_GRANULE
            if not pad or count+pad>limit:raise TransportError('编码长度不能安全对齐且满足限制') from None
            # Keep rejected bytes as evidence; no overwrite or destructive cleanup.
            path=root/f'part_{len(parts)+1:02d}_320kbps_aligned.mp3'
            if path.exists():raise TransportError('重编码输出已存在')
            second_hash=_encode(cover,str(path),start,count,info.samplerate,pad)
            if second_hash!=input_hash:raise TransportError('重编码读取的母带内容改变')
            checks=validate_encoded(path,count,info.samplerate,2,pad,cover,start);retried=True
        if checks['source_pcm_sha256']!=input_hash:raise TransportError('独立重读PCM覆盖hash不一致')
        if checks['bytes']>max_bytes or checks['duration_seconds']>max_seconds:raise TransportError('320kbps运输超限；拒绝降码率或截尾')
        rows.append({'path':str(path),'source_start_frame':start,'source_frames':count,'alignment_retry':retried,**checks});parts.append(path);start+=count
    if start!=info.frames or sum(x['content_frames'] for x in rows)!=info.frames or sha256(cover)!=before:raise TransportError('母带变化或运输覆盖不完整')
    return parts,{'format':'MP3','lossy':True,'bit_rate':BIT_RATE,'source_sha256':before,'source_frames':info.frames,'sample_rate':info.samplerate,'channels':info.channels,
                  'complete_sample_coverage':True,'content_duration_seconds':info.frames/info.samplerate,'total_pad_frames':sum(x['pad_frames'] for x in rows),'parts':rows,
                  'verification':'Physical MPEG frames + Xing/LAME + full decode + deterministic source-reference stream equality',
                  'notice':'仅QQ运输为320kbps MP3；无损PCM24母带不变。仅长度记账不匹配时重编码并附加不足1152样本的零填充，逐段记录，不裁切/淡出原音频。QQ可能再转SILK，接收端不保证无损或立体声。'}
