r"""Synchronous full-track RVC stage; no QQ/download/subprocess.

CLI (use the existing RVC runtime)::
    <RVC>/runtime/python.exe rvc_stage.py --input vocals.wav --out NEW_DIR

Singing-boundary inference and intro/outro gating are intentionally unsupported.
The entire separated vocal stem is converted. No timeline trim/offset is applied.

Outputs: NEW_DIR/vocals.wav (IEEE FLOAT, 48000 Hz mono), stage.json and diagnostic
FLOAT chunks. Never reuse an existing directory. Exit 0/status=success only after
full coverage and final WAV readback. Failures exit nonzero and retain partial
outputs with a failure manifest where possible; partial audio is NOT a result.

Uses a PRIVATE pipeline copy, never edits the installed pipeline or environment.
Voice model/index come from hq_cover/config.py (yours, not shipped here).
Fixed settings: rms=0, index=.75, protect=.33, pitch=0, rmvpe with f0 smoothing;
18s chunks/.3s linear crossfades; no time-domain gating, whole-chunk passthrough or
silence restoration. Float input/stitch/output, FP16 network.
Soundfile + scipy resample_poly Kaiser(8.6) replaces ffmpeg audio loading. Engine
input is resampled to 16k per chunk; native neural output is 48k, NOT original sr.
A <=1 global input gain to .90 is recorded, not undone. Final mastering is caller
responsibility. CPU threads=1, TF32 disabled. Config() argv is cleared; its
filesystem-mutating setup is replaced IN THIS PROCESS with read-only CUDA setup.
--help only imports stdlib. Caller schedules the subprocess/GPU asynchronously.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import time
import traceback

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:sys.path.insert(0,str(HERE.parent))
from hq_cover.policy import STAGE_SCHEMA,SINGING_BOUNDARY_POLICY,ORIGINAL_SOURCE_BYPASS,TIME_DOMAIN_GATING
from hq_cover import config
RVC = config.RVC_HOME
MODEL = (RVC / "assets/weights" / config.RVC_MODEL_NAME) if RVC else None
INDEX = (RVC / "logs" / config.RVC_INDEX_NAME) if RVC else None
HUBERT = (RVC / config.RVC_HUBERT) if RVC else None
RMVPE = (RVC / config.RVC_RMVPE) if RVC else None
PIPELINE = HERE / "rvc_pipeline_float.py"
# 官方预训练件（HuBERT / RMVPE）的哈希是固定的，属于 RVC 发行版本身。
# 音色模型和索引是你自己训的，哈希由 scripts/hash-models.py 写进 models.lock.json；
# 没写就跳过这两项校验（但会打印警告），写了就必须一致。
OFFICIAL_HASHES = {
    HUBERT: "f54b40fd2802423a5643779c4861af1e9ee9c1564dc9d32f54f20b5ffba7db96",
    RMVPE: "6d62215f4306e3ca278246188607209f09af3dc77ed4232efdd069798c4ec193",
}
ASSET_HASHES = {p: h for p, h in OFFICIAL_HASHES.items() if p is not None}
for _p in (MODEL, INDEX):
    if _p is not None:
        _expected = config.expected_hash(_p)
        if _expected:
            ASSET_HASHES[_p] = _expected
VERIFIED_SOURCE = "qq-ai-sing/pipeline/hq_cover/rvc_stage.py"
OUTPUT_RATE = 48000
CHUNK_SECONDS = 18.0
OVERLAP_SECONDS = 0.3


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def new_output_dir(path):
    raw = Path(path).expanduser().absolute()
    if os.path.lexists(str(raw)):
        raise FileExistsError("Refusing existing output path: %s" % raw)
    out = raw.resolve()
    out.mkdir(parents=True, exist_ok=False)
    return out


def load_float(path, sr):
    """VC.load_audio replacement: no ffmpeg and no integer codec intermediate."""
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly
    y, rate = sf.read(str(path), dtype="float32", always_2d=True)
    if len(y) == 0 or not np.isfinite(y).all():
        raise ValueError("Empty/nonfinite RVC chunk")
    y = y.mean(axis=1, dtype=np.float64).astype(np.float32)
    if rate != sr:
        divisor = math.gcd(rate, sr)
        y = resample_poly(y, sr // divisor, rate // divisor, window=("kaiser", 8.6))
    if not np.isfinite(y).all():
        raise ValueError("RVC input resampling produced NaN/Inf")
    return np.ascontiguousarray(y, dtype=np.float32)


def chunk_plan(frames, sr):
    """Integer input-sample boundaries: final fractions of a second are not lost."""
    chunk = round(CHUNK_SECONDS * sr)
    step = round((CHUNK_SECONDS - OVERLAP_SECONDS) * sr)
    if frames < 1 or sr < 1 or step < 1:
        raise ValueError("Invalid audio length/sample rate")
    start = 0
    while start < frames:
        end = min(start + chunk, frames)
        yield start, end
        if end == frames:
            break
        start += step


def overlap_window(length, fade_in, fade_out):
    import numpy as np
    window = np.ones(length, dtype=np.float32)
    if fade_in:
        window[:fade_in] *= np.linspace(0, 1, fade_in, dtype=np.float32)
    if fade_out:
        window[-fade_out:] *= np.linspace(1, 0, fade_out, dtype=np.float32)
    return window


def read_only_config(Config, config_files, torch):
    """Patch only this process: upstream Config otherwise writes/renames old files."""
    cls = next((cell.cell_contents for cell in (Config.__closure__ or ())
                if isinstance(cell.cell_contents, type) and cell.cell_contents.__name__ == "Config"), None)
    if cls is None:
        raise RuntimeError("Unexpected Config singleton implementation; refusing unsafe setup")
    def read_json():
        data = {}
        for name in config_files:
            path = RVC / "configs/inuse" / name
            if not path.is_file():
                path = RVC / "configs" / name
            with path.open(encoding="utf-8") as stream:
                data[name] = json.load(stream)
        return data
    def device_config(self):
        # 后端选择交给 backend.select()：CUDA / ROCm / DirectML 都支持。
        # 以前这里是 `if not torch.cuda.is_available(): raise`，AMD 卡直接被拒。
        from hq_cover import backend
        chosen = backend.select()
        self.device = chosen["device"]
        self.is_half = chosen["is_half"]
        self.n_cpu = 1
        self.gpu_name = chosen["gpu_name"]
        self.gpu_mem = chosen["gpu_mem"]
        self.use_jit = False
        # 上游 Pipeline 用 self.dml 判断是否走 DirectML 的兼容路径
        self.dml = chosen["kind"] == "dml"
        return chosen["dims"]
    old_json = cls.__dict__["load_config_json"]
    old_device = cls.__dict__["device_config"]
    old_argv = sys.argv
    try:
        cls.load_config_json = staticmethod(read_json)
        cls.device_config = device_config
        sys.argv = [old_argv[0]]  # Config's argparse must not see this stage's CLI.
        return Config()
    finally:
        sys.argv = old_argv
        cls.load_config_json = old_json
        cls.device_config = old_device


def load_engine():
    sys.dont_write_bytecode = True
    os.environ.update(weight_root=str(RVC / "assets/weights"), index_root=str(RVC / "logs"),
                      rmvpe_root=str(RVC / "assets/rmvpe"), RVC_F0_SMOOTH="1",
                      RVC_F0_MEDIAN="5", RVC_F0_MAXGAP="100", OMP_NUM_THREADS="1",
                      MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", NUMBA_NUM_THREADS="1")
    import torch
    torch.set_num_threads(1)
    # 这三个开关只在 CUDA 后端存在；DirectML(AMD) 上访问会 AttributeError。
    if hasattr(torch.backends, "cuda") and torch.backends.cuda is not None:
        torch.backends.cuda.matmul.allow_tf32 = False
    if hasattr(torch.backends, "cudnn") and torch.backends.cudnn is not None:
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cudnn.benchmark = False
    # 后端可用性由 backend.select() 判定（CUDA / ROCm / DirectML），这里不再写死 CUDA。
    from hq_cover import backend
    backend.select()
    sys.path.insert(0, str(RVC))
    spec = importlib.util.spec_from_file_location("infer.modules.vc.pipeline", PIPELINE)
    if spec is None or spec.loader is None:
        raise RuntimeError("Private float pipeline cannot be loaded")
    pipeline = importlib.util.module_from_spec(spec)
    sys.modules["infer.modules.vc.pipeline"] = pipeline
    spec.loader.exec_module(pipeline)
    old_argv = sys.argv
    try:
        sys.argv = [old_argv[0]]
        from configs.config import Config, version_config_list
        cfg = read_only_config(Config, version_config_list, torch)
    finally:
        sys.argv = old_argv
    from infer.modules.vc import modules as vc_modules
    vc_modules.load_audio = load_float
    vc = vc_modules.VC(cfg)
    vc.get_vc(MODEL.name)
    if vc.tgt_sr != OUTPUT_RATE or vc.if_f0 != 1 or vc.version != "v2":
        raise RuntimeError(
            "音色模型架构不符：需要 RVC v2 / 带 f0 / 48k 的模型（当前 tgt_sr=%s if_f0=%s version=%s）"
            % (vc.tgt_sr, vc.if_f0, vc.version))
    return vc


def write_float(path, audio, sample_rate):
    import soundfile as sf
    with Path(path).open("xb") as stream:
        sf.write(stream, audio, sample_rate, format="WAV", subtype="FLOAT")


def convert_chunks(y, sr, out, vc):
    import numpy as np
    target_frames = round(len(y) * OUTPUT_RATE / sr)
    if target_frames < 1:
        raise ValueError("Input shorter than one output sample")
    result = np.zeros(target_frames, dtype=np.float32)
    weights = np.zeros_like(result)
    plans = list(chunk_plan(len(y), sr))
    mapped = [(round(a * OUTPUT_RATE / sr), round(b * OUTPUT_RATE / sr)) for a, b in plans]
    chunks = out / "chunks"
    chunks.mkdir(exist_ok=False)
    reports = []
    for i, (a, b) in enumerate(plans):
        audio = y[a:b]
        begin, end = mapped[i]
        n = end - begin
        path = chunks / ("input_%04d.wav" % i)
        write_float(path, audio, sr)
        rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
        message, got = vc.vc_single(0, str(path), 0, None, "rmvpe", str(INDEX), None,
                                    0.75, 3, 0, 0.0, 0.33)
        if not isinstance(message, str) or not message.startswith("Success."):
            raise RuntimeError("RVC chunk %d failed: %s" % (i, message))
        if got is None or len(got) != 2 or got[1] is None or "Index not used" in message:
            raise RuntimeError("RVC chunk %d has no audio/index" % i)
        rate, converted = got
        raw = np.asarray(converted)
        if rate != OUTPUT_RATE or raw.ndim != 1 or raw.dtype.kind != "f" or raw.size == 0:
            raise RuntimeError("RVC must return mono floating-point 48k audio, not PCM integers")
        converted = raw.astype(np.float32)
        if not np.isfinite(converted).all():
            raise RuntimeError("RVC chunk contains NaN/Inf")
        returned_frames = len(converted)
        if abs(returned_frames - n) >= OUTPUT_RATE * 0.1:
            raise RuntimeError("RVC chunk duration mismatch: %d vs %d" % (returned_frames, n))
        converted = np.pad(converted, (0, max(0, n - returned_frames)))[:n]
        write_float(chunks / ("converted_%04d.wav" % i), converted, OUTPUT_RATE)
        fade_in = min(n, max(0, mapped[i - 1][1] - begin)) if i else 0
        fade_out = min(n, max(0, end - mapped[i + 1][0])) if i + 1 < len(plans) else 0
        window = overlap_window(n, fade_in, fade_out)
        result[begin:end] += converted * window
        weights[begin:end] += window
        reports.append({"chunk": i, "source_start_frame": a, "source_end_frame": b,
                        "start_seconds": a / sr, "end_seconds": b / sr, "input_rms": rms,
                        "output_start_frame": begin, "output_end_frame": end,
                        "engine_returned_frames": returned_frames,
                        "padding_or_trimming_frames": n - returned_frames, "message": message})
        print("RVC chunk %d/%d %.3f-%.3fs" % (i + 1, len(plans), a / sr, b / sr), flush=True)
    if not np.all(weights > 0) or not np.isfinite(result).all():
        raise RuntimeError("Full-song coverage/nonfinite check failed")
    result /= weights
    return result, reports


def require_assets():
    """权重缺失时给出能照着做的报错，而不是一堆 FileNotFoundError。"""
    if RVC is None:
        raise RuntimeError("SING_RVC_HOME 未设置：请指向 RVC 安装目录（见 docs/INSTALL.md）")
    missing = [p for p in (MODEL, INDEX, HUBERT, RMVPE) if p is None or not Path(p).is_file()]
    if missing:
        raise RuntimeError(
            "缺少 RVC 资源：\n  " + "\n  ".join(str(p) for p in missing) +
            "\n音色模型和索引要自己训练（docs/TRAIN_VOICE.md），"
            "然后把 .pth 放到 <RVC>/assets/weights/、.index 放到 <RVC>/logs/，"
            "并在 config.local.json 里写 rvcModel / rvcIndex。")


def run(args):
    sys.dont_write_bytecode = True
    require_assets()
    os.environ.update(OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1",
                      NUMBA_NUM_THREADS="1")
    import numpy as np
    import soundfile as sf
    started = time.time()
    source = args.input.expanduser().resolve(strict=True)
    out = new_output_dir(args.out)
    report = {"schema_version": STAGE_SCHEMA, "stage": "rvc", "status": "running",
              "verified_source": VERIFIED_SOURCE, "input": {"path": str(source)}}
    old_cwd = Path.cwd()
    try:
        info = sf.info(str(source))
        if (info.format not in {"WAV", "WAVEX", "RF64"} or info.subtype not in {"FLOAT", "DOUBLE"}
                or info.channels not in (1, 2) or info.frames < 1 or info.samplerate < 1):
            raise ValueError("RVC input must be nonempty mono/stereo FLOAT/DOUBLE WAV")
        audio, sr = sf.read(str(source), dtype="float32", always_2d=True)
        if audio.shape != (info.frames, info.channels) or not np.isfinite(audio).all():
            raise ValueError("Invalid decoded source vocals")
        y = audio.mean(axis=1, dtype=np.float64).astype(np.float32)
        duration = len(y) / sr
        report.update(input={"path": str(source), "sha256": sha256(source), "sample_rate": sr,
                             "channels": info.channels, "frames": info.frames, "subtype": info.subtype,
                             "duration_seconds": duration}, source_sample_rate=sr,
                      stage_schema=STAGE_SCHEMA,singing_boundary_policy=SINGING_BOUNDARY_POLICY,
                      original_source_bypass=ORIGINAL_SOURCE_BYPASS,time_domain_gating=TIME_DOMAIN_GATING)
        gain = min(1.0, 0.90 / max(float(np.max(np.abs(y))), 1e-12))
        y *= gain
        assets = {}
        for path, expected in ASSET_HASHES.items():
            actual = sha256(path)
            if actual != expected:
                raise RuntimeError("SHA256 mismatch: %s" % path)
            assets[path.name] = {"path": str(path), "sha256": actual}
        if not PIPELINE.is_file():
            raise FileNotFoundError("Private float pipeline is missing")
        os.chdir(RVC)
        vc = load_engine()
        result, chunks = convert_chunks(y, sr, out, vc)
        if not np.isfinite(result).all():
            raise RuntimeError("Final audio contains NaN/Inf")
        output = out / "vocals.wav"
        write_float(output, result, OUTPUT_RATE)
        decoded, rate = sf.read(str(output), dtype="float32")
        output_info = sf.info(str(output))
        if (rate != OUTPUT_RATE or output_info.channels != 1 or output_info.subtype != "FLOAT"
                or not np.array_equal(decoded, result) or len(decoded) != round(duration * OUTPUT_RATE)):
            raise RuntimeError("Final FLOAT mono 48k readback failed")
        report.update(status="success", voice_id=config.VOICE_ID, label=config.VOICE_LABEL,
                      assets=assets, private_pipeline_sha256=sha256(PIPELINE),
                      output=str(output), outputs={"vocals": {"path": str(output), "sha256": sha256(output),
                      "sample_rate": rate, "channels": 1, "frames": len(result), "subtype": "FLOAT",
                      "duration_seconds": len(result) / rate, "peak": float(np.max(np.abs(result))),
                      "nonfinite_samples": 0}}, sample_rate=rate, channels=1,
                      input_global_gain=gain, chunk_reports=chunks,
                      resampling={"source_rate": sr, "engine_input_rate": 16000,
                                  "method": "scipy.signal.resample_poly Kaiser beta=8.6",
                                  "scope": "once per chunk if source rate differs from 16000",
                                  "native_neural_output_rate": OUTPUT_RATE, "final_resample": False},
                      settings={"rms_mix": 0, "index_rate": 0.75, "protect": 0.33,
                                "pitch_shift_semitones": 0, "f0_method": "rmvpe", "f0_smooth": True,
                                "f0_median_frames": 5, "f0_max_gap_ms": 100, "filter_radius": 3,
                                "chunk_seconds": CHUNK_SECONDS, "overlap_seconds": OVERLAP_SECONDS,
                                "time_domain_gating": False, "whole_chunk_passthrough": False,
                                "boundary_detection": False,
                                "neural_precision": "fp16", "output_precision": "float32",
                                "integer_audio_intermediate": False, "original_vocal_blend": 0,
                                "cpu_threads": 1, "tf32": False},
                      elapsed_seconds=time.time() - started, technical_checks_passed=True,
                      subjective_listening_verified=False)
        with (out / "stage.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        return report
    except BaseException as exc:
        report.update(status="failed", error_type=type(exc).__name__, error=str(exc),
                      elapsed_seconds=time.time() - started, technical_checks_passed=False)
        if not (out / "stage.json").exists():
            try:
                with (out / "stage.json").open("x", encoding="utf-8") as stream:
                    json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            except Exception:
                pass
        raise
    finally:
        os.chdir(old_cwd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, required=True, help="Source FLOAT vocals WAV")
    parser.add_argument("--out", type=Path, required=True, help="New, nonexistent output directory")
    args = parser.parse_args(argv)
    try:
        report = run(args)
    except Exception:
        traceback.print_exc()
        return 1
    print("RVC_READY=" + report["output"], flush=True)
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
