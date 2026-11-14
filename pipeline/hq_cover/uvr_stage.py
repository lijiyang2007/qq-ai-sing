r"""Synchronous, standalone MelBandRoFormer separation stage (no QQ/subprocess).

CLI (run in the UVR environment only)::
    <UVR>/venv/Scripts/python.exe uvr_stage.py --input song.flac --out NEW_DIR
    ... --segment 1101 --overlap 4

Outputs: NEW_DIR/vocals.wav, instrumental.wav (44100 Hz stereo IEEE FLOAT),
stage.json. Existing output directories are NEVER reused. Exit 0 + status=success
is emitted only after both outputs have been decoded and checked. Failure exits
nonzero; an owned partial directory is retained with status=failed when possible.

Model paths come from hq_cover/config.py; the weights themselves are NOT shipped
here (see docs/INSTALL.md). Uses the official UVR-beta SeperateMDXC.demix.
If models.lock.json records SHA256 for the model and YAML they are verified before
loading; otherwise verification is skipped with a warning. weights_only,
CPU threads=1 and TF32 disabled. FLAC/WAV PCM or float only; mono is duplicated,
>2 channels rejected. Non-44100 input is resampled exactly once with soxr VHQ;
the original and processing rates/frames are reported separately. No peak
normalization, denoise, lossy encoding, source provenance claim or mixing.

--help imports only stdlib. Production requires CUDA; do not run concurrent GPU
jobs. Caller owns asynchronous process scheduling. AI separation is not lossless
in the perceptual sense; reconstruction checks do not establish zero leakage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback

from hq_cover import config
ROOT = config.UVR_HOME
APP = (ROOT / "UVR-beta") if ROOT else None
WEIGHT = (ROOT / "models" / config.UVR_WEIGHT_NAME) if ROOT else None
CONFIG = (ROOT / "models" / config.UVR_CONFIG_NAME) if ROOT else None
EXPECTED_WEIGHT = config.expected_hash(WEIGHT) or ""
EXPECTED_CONFIG = config.expected_hash(CONFIG) or ""
VERIFIED_SOURCE = "qq-ai-sing/pipeline/hq_cover/uvr_stage.py"
SAMPLE_RATE = 44100


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require_hash(path, expected):
    actual = sha256(path)
    if actual != expected:
        raise RuntimeError("SHA256 mismatch for %s: expected %s, got %s" % (path, expected, actual))
    return actual


def new_output_dir(path):
    # lexists also rejects dangling links. mkdir is the race-safe final guard.
    raw = Path(path).expanduser().absolute()
    if os.path.lexists(str(raw)):
        raise FileExistsError("Refusing existing output path: %s" % raw)
    out = raw.resolve()
    out.mkdir(parents=True, exist_ok=False)
    return out


def read_input(path):
    import numpy as np
    import soundfile as sf

    info = sf.info(str(path))
    allowed = {"PCM_U8", "PCM_S8", "PCM_16", "PCM_24", "PCM_32", "FLOAT", "DOUBLE"}
    if info.format not in {"FLAC", "WAV", "WAVEX", "RF64"} or info.subtype not in allowed:
        raise ValueError("Only uncompressed PCM/float WAV or FLAC input is accepted")
    if info.channels not in (1, 2) or info.frames < 1 or info.samplerate < 1:
        raise ValueError("Input must be nonempty mono/stereo audio")
    y, sr = sf.read(str(path), dtype="float32", always_2d=True)
    if y.shape != (info.frames, info.channels) or not np.isfinite(y).all():
        raise ValueError("Input decoding produced invalid/nonfinite samples")
    metadata = {"path": str(path), "sha256": sha256(path), "format": info.format,
                "subtype": info.subtype, "sample_rate": sr, "frames": len(y),
                "channels": info.channels, "duration_seconds": len(y) / sr}
    if info.channels == 1:
        y = np.repeat(y, 2, axis=1)
    resampling = {"performed": sr != SAMPLE_RATE, "input_rate": sr,
                  "output_rate": SAMPLE_RATE, "passes": int(sr != SAMPLE_RATE),
                  "method": "soxr VHQ" if sr != SAMPLE_RATE else "none"}
    if sr != SAMPLE_RATE:
        import soxr
        y = soxr.resample(y, sr, SAMPLE_RATE, quality="VHQ")
    y = np.ascontiguousarray(y, dtype=np.float32)
    if len(y) < 1 or not np.isfinite(y).all():
        raise ValueError("Resampling produced empty/nonfinite audio")
    resampling["output_frames"] = len(y)
    resampling["duration_delta_seconds"] = len(y) / SAMPLE_RATE - metadata["duration_seconds"]
    return y, metadata, resampling


def verify_stems(voice, instrumental, mix):
    import numpy as np
    if voice.shape != mix.shape or instrumental.shape != mix.shape:
        raise RuntimeError("Stem dimensions differ from processing input")
    if not np.isfinite(voice).all() or not np.isfinite(instrumental).all():
        raise RuntimeError("Demixer produced NaN/Inf")
    error = voice.astype(np.float64) + instrumental.astype(np.float64) - mix
    maximum = float(np.max(np.abs(error)))
    rms = float(np.sqrt(np.mean(error ** 2)))
    if maximum >= 1e-5:
        raise RuntimeError("Stems fail waveform reconstruction consistency: %g" % maximum)
    return {"max_error": maximum, "rms_error": rms}


def write_float(path, audio, sample_rate):
    import numpy as np
    import soundfile as sf
    with Path(path).open("xb") as stream:
        sf.write(stream, audio, sample_rate, format="WAV", subtype="FLOAT")
    decoded, sr = sf.read(str(path), dtype="float32", always_2d=True)
    info = sf.info(str(path))
    if (sr != sample_rate or info.subtype != "FLOAT" or decoded.shape != audio.shape
            or not np.isfinite(decoded).all() or not np.array_equal(decoded, audio)):
        raise RuntimeError("Float WAV readback failed: %s" % path)
    return {"path": str(path), "sha256": sha256(path), "sample_rate": sr,
            "channels": info.channels, "frames": info.frames, "subtype": info.subtype,
            "duration_seconds": info.frames / sr, "peak": float(np.max(np.abs(decoded))),
            "nonfinite_samples": 0}


def demix(mix, segment, overlap):
    # All environment changes are process-local; no installed module is edited.
    sys.dont_write_bytecode = True
    os.environ.update(OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1",
                      NUMBA_NUM_THREADS="1")
    import numpy as np
    import torch
    import yaml
    from ml_collections import ConfigDict

    torch.set_num_threads(1)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    torch.backends.cudnn.benchmark = False
    if not torch.cuda.is_available():
        raise RuntimeError("This verified UVR stage requires CUDA; no CPU fallback")
    # Do not parse unverified YAML (the pinned upstream YAML uses python/tuple).
    require_hash(WEIGHT, EXPECTED_WEIGHT)
    require_hash(CONFIG, EXPECTED_CONFIG)
    with CONFIG.open(encoding="utf-8") as stream:
        cfg = ConfigDict(yaml.load(stream, Loader=yaml.FullLoader))
    if cfg.audio.sample_rate != SAMPLE_RATE or cfg.audio.num_channels != 2:
        raise RuntimeError("Unexpected UVR model audio configuration")
    old_cwd = Path.cwd()
    sys.path.insert(0, str(APP))
    try:
        os.chdir(APP)
        from separate import SeperateMDXC
        worker = object.__new__(SeperateMDXC)
        settings = dict(device="cuda", is_pitch_change=False, is_roformer=True,
                        roformer_config=cfg, mdx_c_configs=cfg, model_path=str(WEIGHT),
                        is_mdx_c_seg_def=False, mdx_segment_size=segment,
                        overlap_mdx23=overlap, is_using_directml=False, is_other_gpu=False,
                        is_vocal_main_target=True, is_denoise_model=False)
        for key, value in settings.items():
            setattr(worker, key, value)
        count = [0]
        def progress(total):
            count[0] += 1
            if count[0] == 1 or count[0] % 10 == 0:
                print("UVR windows=%d/%d" % (count[0], total), flush=True)
        worker.running_inference_progress_bar = progress
        original_load = torch.load
        def safe_load(*args, **kwargs):
            kwargs["weights_only"] = True
            return original_load(*args, **kwargs)
        torch.load = safe_load
        try:
            stems = worker.demix(np.ascontiguousarray(mix.T))
        finally:
            torch.load = original_load
        return (np.asarray(stems["Vocals"], dtype=np.float32).T,
                np.asarray(stems["Instrumental"], dtype=np.float32).T)
    finally:
        os.chdir(old_cwd)


def require_assets():
    if ROOT is None or APP is None:
        raise RuntimeError("SING_UVR_HOME 未设置：请指向 UVR 安装目录（见 docs/INSTALL.md）")
    missing = [p for p in (WEIGHT, CONFIG) if p is None or not Path(p).is_file()]
    if missing:
        raise RuntimeError(
            "缺少 UVR 分离权重：\n  " + "\n  ".join(str(p) for p in missing) +
            "\n下载 MelBandRoformer.ckpt 和配套 yaml 放进 <UVR>/models/，"
            "文件名在 config.local.json 的 uvrWeight / uvrConfig 里改。")
    if not EXPECTED_WEIGHT or not EXPECTED_CONFIG:
        print("WARN: models.lock.json 里没有这两个权重的 sha256，本次跳过完整性校验；"
              "建议跑一次 scripts/hash-models.py 生成。", flush=True)


def run(args):
    sys.dont_write_bytecode = True
    require_assets()
    os.environ.update(OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1",
                      NUMBA_NUM_THREADS="1")
    started = time.time()
    if args.segment < 2 or args.overlap < 1 or args.overlap > 441 * (args.segment - 1):
        raise ValueError("segment must be >=2; overlap must yield a positive hop")
    source = args.input.expanduser().resolve(strict=True)
    out = new_output_dir(args.out)
    report = {"schema_version": 1, "stage": "uvr", "status": "running",
              "verified_source": VERIFIED_SOURCE, "input": {"path": str(source)}}
    try:
        if EXPECTED_WEIGHT:
            require_hash(WEIGHT, EXPECTED_WEIGHT)
        if EXPECTED_CONFIG:
            require_hash(CONFIG, EXPECTED_CONFIG)
        mix, source_info, resampling = read_input(source)
        report.update(input=source_info, source_sample_rate=source_info["sample_rate"],
                      sample_rate=SAMPLE_RATE, resampling=resampling,
                      channel_mapping="mono duplicated to stereo" if source_info["channels"] == 1 else "stereo unchanged")
        voice, instrumental = demix(mix, args.segment, args.overlap)
        reconstruction = verify_stems(voice, instrumental, mix)
        outputs = {"vocals": write_float(out / "vocals.wav", voice, SAMPLE_RATE),
                   "instrumental": write_float(out / "instrumental.wav", instrumental, SAMPLE_RATE)}
        report.update(status="success", outputs=outputs, reconstruction=reconstruction,
                      model={"path": str(WEIGHT), "sha256": EXPECTED_WEIGHT,
                             "config": str(CONFIG), "config_sha256": EXPECTED_CONFIG},
                      engine="official SeperateMDXC.demix",
                      settings={"segment": args.segment, "overlap": args.overlap, "batch_size": 1,
                                "cpu_threads": 1, "tf32": False, "weights_only": True,
                                "dtype": "float32", "normalization": False, "post_denoise": False},
                      elapsed_seconds=time.time() - started, technical_checks_passed=True,
                      subjective_listening_verified=False,
                      quality_note="AI separation can leak stems; FLOAT/reconstruction is not perceptual losslessness")
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, required=True, help="PCM/float FLAC or WAV")
    parser.add_argument("--out", type=Path, required=True, help="New, nonexistent output directory")
    parser.add_argument("--segment", type=int, default=1101)
    parser.add_argument("--overlap", type=int, default=4)
    args = parser.parse_args(argv)
    try:
        report = run(args)
    except Exception:
        traceback.print_exc()
        return 1
    print("UVR_READY=" + str(Path(report["outputs"]["vocals"]["path"]).parent), flush=True)
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
