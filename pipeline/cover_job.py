#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
翻唱异步任务系统
================
为什么需要它：
  1. 转换要几分钟到二十几分钟，AI 不能干等（会阻塞会话、超时）
  2. Seed-VC 每次启动都要花约 200 秒加载模型——固定开销比副歌本身还长

所以：提交任务 -> 后台跑 -> 跑完自动转 MP3 并发到 QQ -> 写状态文件。
AI 只需说一句"在做了"，然后用 schedule 定时回来查状态。

用法：
    python cover_job.py submit --song "Love Story Taylor Swift" --target group:<你的群号> --mode chorus
    python cover_job.py status --job <id>
    python cover_job.py list
    python cover_job.py run --job <id>          # worker 内部用
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

# ---------------------------------------------------------------- 代码位置
# 这个脚本有两种被调用的方式，都要能找到 hq_cover：
#   ① 留在仓库里直接跑（python <repo>/pipeline/cover_job.py ...）—— hq_cover 就在旁边；
#   ② 按 BRIDGE_WIRING.md 复制到 dataRoot（桥接 spawn 它）—— hq_cover 可能在旁边，
#      也可能不在（只复制了一个文件）。所以两种情况都探一遍，都没有就明确报错。
_SELF_DIR = Path(__file__).resolve().parent
for _cand in (_SELF_DIR, _SELF_DIR / "pipeline", _SELF_DIR.parent / "pipeline"):
    if (_cand / "hq_cover" / "__init__.py").is_file():
        if str(_cand) not in sys.path:
            sys.path.insert(0, str(_cand))
        break
else:
    raise SystemExit(
        "找不到 hq_cover 包。这个脚本必须和 pipeline/hq_cover/ 在一起：\n"
        "  · 要么留在仓库里跑：python <仓库>/pipeline/cover_job.py ...\n"
        "  · 要么把整个 pipeline/ 目录（不只这一个文件）放到 sing.root，"
        "再用 SING_DATA_ROOT 指定数据目录\n"
        "详见 docs/BRIDGE_WIRING.md。"
    )
del _cand

# 数据目录（任务 JSON / 产物 / 缓存）。代码目录和它分开，方便只更新代码不丢任务。
ROOT = Path(os.environ.get("SING_DATA_ROOT") or _SELF_DIR)
JOBS = ROOT / "jobs"
VENV_PY = Path(sys.executable)
AUDIO_DIR = Path(os.environ.get("SING_AUDIO_DIR") or (ROOT / "audio"))
CONSOLE_URL = "http://127.0.0.1:%s/api/voice/send" % (os.environ.get("QQ_CONSOLE_PORT") or "3100")
CONSOLE_TOKEN = os.environ.get("QQ_CONSOLE_TOKEN", "")  # 由桥接注入，绝不写死

DEFAULT_REF = ""  # 旧 Seed-VC 引擎用；RVC 流程不需要参考音频

JOBS.mkdir(parents=True, exist_ok=True)


def effective_audio_dir():
    """桥接实际读取的音频目录。

    桥接 src/bridge.js 的 resolveVoiceDir() 逻辑：
      若 audio/<voiceId>/ 子目录存在，语音库根目录就切到那个子目录。
    所以存在 voc_xxx 子目录时，文件必须写进去才能被找到。
    """
    cfg_p = Path(os.environ.get("QQ_BRIDGE_CONFIG")
                 or (ROOT / "qq-bridge" / "config.json"))
    try:
        cfg = json.loads(cfg_p.read_text(encoding="utf-8"))
    except Exception:
        return AUDIO_DIR
    v = (cfg.get("socialV2") or {}).get("voice") or {}
    if v.get("libraryDir"):
        cand = AUDIO_DIR / str(v["libraryDir"]).strip()
        if cand.is_dir():
            return cand
    voice_id = (((v.get("tts") or {}).get("breeze") or {}).get("voiceId") or "").strip()
    if re.fullmatch(r"voc_[A-Za-z0-9]+", voice_id):
        cand = AUDIO_DIR / voice_id
        if cand.is_dir():
            return cand
    return AUDIO_DIR


def slugify(text, limit=48):
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "_", text, flags=re.UNICODE).strip("_")
    return s[:limit] or "job"


def job_path(jid):
    return JOBS / f"{jid}.json"


def load(jid):
    p = job_path(jid)
    if not p.exists():
        raise SystemExit(f"任务不存在: {jid}")
    return json.loads(p.read_text(encoding="utf-8"))


def save(job):
    job_path(job["id"]).write_text(
        json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- MP3 导出
def export_mp3(wav_path, mp3_path, bitrate=160000):
    """PyAV 编码，绕开被安全软件拦截的系统 ffmpeg"""
    import av
    import numpy as np
    import soundfile as sf

    y, sr = sf.read(str(wav_path), always_2d=True)
    c = av.open(str(mp3_path), mode="w", format="mp3")
    st = c.add_stream("libmp3lame", rate=sr)
    st.bit_rate = bitrate
    st.layout = "stereo"
    data = np.ascontiguousarray(y.T.astype(np.float32))
    frame = av.AudioFrame.from_ndarray(data, format="fltp", layout="stereo")
    frame.sample_rate = sr
    for pkt in st.encode(frame):
        c.mux(pkt)
    for pkt in st.encode(None):
        c.mux(pkt)
    c.close()
    return mp3_path, os.path.getsize(mp3_path)


# ---------------------------------------------------------------- 发送
def send_qq(mp3_name, target, timeout=180):
    import requests
    r = requests.post(
        CONSOLE_URL,
        headers={"X-Console-Token": CONSOLE_TOKEN},
        json={"target": target, "file": mp3_name},
        timeout=timeout,
    )
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:300]}


# ---------------------------------------------------------------- worker
def cmd_run(jid):
    import make_cover  # noqa: F401  仅为确保同目录可导入

    job = load(jid)
    job.update(status="running", started=time.time())
    save(job)

    log_path = JOBS / f"{jid}.log"

    def log(msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    cover = None
    try:
        args = [str(VENV_PY), str(ROOT / "make_cover.py"),
                "--ref", job["ref"],
                "--steps", str(job["steps"]),
                "--cfg", str(job["cfg"]),
                "--blend", str(job["blend"]),
                "--vocal-above-inst", str(job["vocal_above_inst"]),
                "--engine", job.get("engine", "svc"),
                "--mode", job["mode"]]
        if job.get("song_file"):
            args += ["--song-file", job["song_file"]]
        else:
            args += ["--song", job["song"]]

        log(f"启动流水线: mode={job['mode']}")
        r = subprocess.run(args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           cwd=str(ROOT), timeout=job.get("timeout", 3600))
        tail = (r.stdout or "")[-2000:]
        log(tail)

        m = re.search(r"COVER_PATH=(.+)$", r.stdout or "", re.M)
        if not m:
            raise RuntimeError(f"未找到产物。stderr tail: {(r.stderr or '')[-500:]}")
        cover = Path(m.group(1).strip())
        if not cover.exists():
            raise RuntimeError(f"产物不存在: {cover}")

        # 转 MP3 并发到 QQ
        # 注意：桥接的 resolveVoiceDir() 在发现 audio/<voiceId>/ 子目录存在时，
        # 会把语音库根目录整个切到那个子目录。所以覆盖文件必须写进
        # 「桥接实际读取的目录」，否则 API 会返回 400 找不到音频。
        out_dir = effective_audio_dir()
        mp3 = out_dir / f"cover_{jid}.mp3"
        out_dir.mkdir(parents=True, exist_ok=True)
        export_mp3(cover, mp3)

        # 时长兜底：桥接上限 300 秒。本机 ffprobe 缺失时时长校验会被跳过，
        # 但一旦装上就会被拒，所以这里主动截到 295 秒并做淡出。
        try:
            import soundfile as _sf
            _i = _sf.info(str(cover))
            if _i.duration > 295.0:
                import numpy as _np
                _y, _sr = _sf.read(str(cover), always_2d=True)
                _n = int(295.0 * _sr)
                _y = _y[:_n].copy()
                _fade = min(int(0.5 * _sr), _n // 4)
                if _fade > 1:
                    _y[-_fade:] *= _np.linspace(1, 0, _fade)[:, None]
                _sf.write(str(mp3.with_suffix(".trim.wav")), _y, _sr, subtype="PCM_16")
                export_mp3(mp3.with_suffix(".trim.wav"), mp3)
                mp3.with_suffix(".trim.wav").unlink(missing_ok=True)
                log(f"时长超 295s（{_i.duration:.1f}s），已截断并淡出")
        except Exception as e:
            log(f"时长检查跳过: {e}")

        size_mb = mp3.stat().st_size / 1e6
        log(f"MP3 导出: {mp3.name}  {size_mb:.2f} MB  (目录 {out_dir.name})")

        if size_mb >= 19.5:
            mp3b = out_dir / f"cover_{jid}_96.mp3"
            export_mp3(cover, mp3b, bitrate=96000)
            mp3.unlink(missing_ok=True)
            mp3 = mp3b
            log(f"体积过大，降码率重编: {mp3.stat().st_size/1e6:.2f} MB")

        code, resp = send_qq(mp3.name, job["target"])
        log(f"发送结果: HTTP {code} {json.dumps(resp, ensure_ascii=False)[:200]}")

        job.update(status="done", finished=time.time(), cover=str(cover),
                   mp3=str(mp3), mp3_name=mp3.name,
                   send_ok=bool(resp.get("ok")), send_resp=resp,
                   message_id=resp.get("messageId"))

        # 清理旧翻唱产物：语音库里堆太多 cover_*.mp3 会污染语音列表。
        # 保留最近 3 个（便于排查），其余删除。已发送的音频已在 QQ 服务端。
        if resp.get("ok"):
            try:
                olds = sorted(out_dir.glob("cover_*.mp3"),
                              key=lambda p: p.stat().st_mtime, reverse=True)[3:]
                for p in olds:
                    p.unlink(missing_ok=True)
                if olds:
                    log(f"清理旧翻唱产物 {len(olds)} 个")
            except Exception as e:
                log(f"清理跳过: {e}")
    except Exception as e:
        log(f"失败: {type(e).__name__}: {e}")
        job.update(status="failed", finished=time.time(), error=f"{type(e).__name__}: {e}",
                   cover=str(cover) if cover else None)
    save(job)


# ---------------------------------------------------------------- 提交
def cmd_submit(args):
    jid = time.strftime("%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    job = {
        "id": jid,
        "song": args.song,
        "song_file": args.song_file,
        "target": args.target,
        "mode": args.mode,
        "ref": args.ref,
        "steps": args.steps,
        "cfg": args.cfg,
        "blend": args.blend if args.blend is not None else (0.0 if args.engine in ("rvc", "svc") else 0.20),
        "engine": args.engine,
        "vocal_above_inst": args.vocal_above_inst,
        "status": "queued",
        "created": time.time(),
        "timeout": 3600 if args.mode == "full" else 1200,
    }
    save(job)

    log_path = JOBS / f"{jid}.log"
    flags = 0x00000008 | 0x00000200        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    with open(log_path, "w", encoding="utf-8") as lf:
        subprocess.Popen(
            [str(VENV_PY), str(Path(__file__)), "run", "--job", jid],
            stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
            creationflags=flags, close_fds=True, cwd=str(ROOT),
        )
    job["status"] = "queued"
    save(job)
    print(json.dumps({"ok": True, "job": jid, "mode": job["mode"],
                      "target": job["target"], "eta_sec": (150 if args.mode == "chorus" else 1300)},
                     ensure_ascii=False, indent=2))


def cmd_status(args):
    job = load(args.job)
    print(json.dumps(job, ensure_ascii=False, indent=2))


def cmd_list(args):
    rows = []
    for p in sorted(JOBS.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:15]:
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
            rows.append({"job": j["id"], "song": j.get("song") or j.get("song_file"),
                         "mode": j.get("mode"), "status": j.get("status"),
                         "target": j.get("target")})
        except Exception:
            pass
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def main():
    # Quality-first default for the bridge's existing qq_sing CLI.
    # Credentials/target are inherited from the authenticated bridge submission.
    # Legacy non-default engines are never silently selected by this route.
    if len(sys.argv) > 1 and sys.argv[1] == "submit":
        if "--song-file" in sys.argv or "--engine" in sys.argv or "--blend" in sys.argv:
            raise SystemExit("质量优先入口不接受旧管线覆盖参数；请使用已验证无损音源流程。")
        from hq_cover.jobs import main as hq_main
        return hq_main(sys.argv[1:])
    if len(sys.argv) > 1 and sys.argv[1] == "run" and "--job" in sys.argv:
        _i = sys.argv.index("--job")
        if _i + 1 < len(sys.argv):
            _job = load(sys.argv[_i + 1])
            if _job.get("pipeline"):
                from hq_cover.policy import PIPELINE
                if _job.get("pipeline") != PIPELINE:
                    raise SystemExit("LEGACY_HQ_PIPELINE_REPLAY_REJECTED: 旧版或未知高质量流水线任务禁止重放；保留历史记录，必须重新点歌。")
                from hq_cover.jobs import main as hq_main
                return hq_main(sys.argv[1:])
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("submit")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--song")
    g.add_argument("--song-file")
    s.add_argument("--target", required=True, help="group:群号 或 private:QQ号")
    s.add_argument("--mode", choices=["chorus", "full"], default="chorus")
    s.add_argument("--ref", default=DEFAULT_REF)
    s.add_argument("--steps", type=int, default=50)
    s.add_argument("--cfg", type=float, default=0.5)
    s.add_argument("--blend", type=float, default=None,
                   help="混入原唱比例（默认：rvc/svc 引擎 0，seedvc 引擎 0.20）")
    s.add_argument("--engine", choices=["rvc", "svc", "seedvc"], default="rvc",
                   help="rvc=RVC 48k v2（默认，质量最好）；svc=so-vits-svc；"
                        "seedvc=Seed-VC 零样本")
    s.add_argument("--vocal-above-inst", type=float, default=4.0)
    s.set_defaults(func=cmd_submit)

    st = sub.add_parser("status")
    st.add_argument("--job", required=True)
    st.set_defaults(func=cmd_status)

    ls = sub.add_parser("list")
    ls.set_defaults(func=cmd_list)

    rn = sub.add_parser("run")
    rn.add_argument("--job", required=True)
    rn.set_defaults(func=lambda a: cmd_run(a.job))

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
