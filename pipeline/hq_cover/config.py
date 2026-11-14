"""所有「跟这台机器有关」的值都从这里取。

这个仓库本身不含任何音色权重、语料、Cookie 或令牌。你需要自己准备：

  · 一个 RVC 音色模型（.pth）和配套索引（.index）——见 docs/TRAIN_VOICE.md
  · UVR 的分离权重（MelBandRoformer.ckpt + 同名 yaml）
  · 自己的网易云 Cookie（只在本机，绝不提交）

配置来源优先级（后面的覆盖前面的）：
  1. 本文件里的默认值
  2. 环境变量（SING_* 前缀）
  3. pipeline/config.local.json（已在 .gitignore 里）

config.local.json 示例见 config.example.json。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# pipeline/ 目录（hq_cover 的上一级）
ROOT = Path(__file__).resolve().parents[1]

_LOCAL_FILE = ROOT / "config.local.json"

# 配置解析问题会记在这里，doctor.py 会单独列一项。
# 语法错误是致命的（直接 SystemExit）；其余问题只警告。
problems: list[str] = []


def _load_local():
    """读 config.local.json。

    文件不存在 → 正常回落默认值（用户可能全用环境变量，这是合法的）。
    文件存在但读不了 / 不是合法 JSON / 顶层不是对象 → **直接报错退出**。

    以前这里是 try/except 吞掉的，结果是「照文档复制了配置却全走默认值」，
    跑完一整首歌才发现音色不对。现在宁可启动就炸。
    """
    if not _LOCAL_FILE.is_file():
        return {}

    try:
        raw = _LOCAL_FILE.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise SystemExit(
            f"读不了配置文件 {_LOCAL_FILE}\n"
            f"  {type(exc).__name__}: {exc}\n"
            "检查文件权限，或先把它改名/删掉。"
        ) from None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        # 把出错那一行原文打出来，比只给行列号有用得多。
        lines = raw.splitlines()
        context = ""
        if 1 <= exc.lineno <= len(lines):
            context = f"\n  第 {exc.lineno} 行： {lines[exc.lineno - 1].strip()[:160]}"
        raise SystemExit(
            f"配置文件不是合法 JSON：{_LOCAL_FILE}\n"
            f"  {exc.msg}（第 {exc.lineno} 行，第 {exc.colno} 列）{context}\n"
            "\n"
            "常见原因：\n"
            "  · 写了 // 或 # 注释（JSON 不支持注释）\n"
            "  · 最后一项后面多了一个逗号\n"
            "  · 用了中文引号 “ ” 或全角逗号 ，\n"
            "  · Windows 路径里的反斜杠没转义（要么用 D:/x/y，要么写 D:\\\\x\\\\y）\n"
            "\n"
            "对照 pipeline/config.example.json 改一遍。"
        ) from None

    if not isinstance(data, dict):
        raise SystemExit(
            f"配置文件顶层必须是一个对象（{{...}}）：{_LOCAL_FILE}\n"
            f"  实际读到的是 {type(data).__name__}。"
        )
    return data


_LOCAL = _load_local()


def _get(key, env, default=None):
    """先看环境变量，再看 config.local.json，最后用默认值。"""
    value = os.environ.get(env)
    if value not in (None, ""):
        return value
    value = _LOCAL.get(key)
    if value not in (None, ""):
        return value
    return default


def _path(value):
    return Path(str(value)).expanduser().resolve() if value else None


# ── 数据目录 ────────────────────────────────────────────────────────────────
# 任务 JSON、运行产物、缓存、无损音源缓存都放这里。默认就是 pipeline/ 本身，
# 想放别的盘就设 SING_DATA_ROOT。
DATA_ROOT = _path(_get("dataRoot", "SING_DATA_ROOT", str(ROOT))) or ROOT

# ── 音色 ────────────────────────────────────────────────────────────────────
# 任意字符串都行，只用来做报告里的身份标记和缓存键，换音色必须换这个值。
VOICE_ID = str(_get("voiceId", "SING_VOICE_ID", "my-voice"))
VOICE_LABEL = str(_get("voiceLabel", "SING_VOICE_LABEL", "自定义音色"))

# ── RVC（换声引擎）──────────────────────────────────────────────────────────
RVC_HOME = _path(_get("rvcHome", "SING_RVC_HOME", r"D:\RVC"))
RVC_MODEL_NAME = str(_get("rvcModel", "SING_RVC_MODEL", "model.pth"))
RVC_INDEX_NAME = str(_get("rvcIndex", "SING_RVC_INDEX", "model.index"))
RVC_HUBERT = "assets/hubert/hubert_base.pt"
RVC_RMVPE = "assets/rmvpe/rmvpe.pt"

# ── UVR（人声分离）──────────────────────────────────────────────────────────
UVR_HOME = _path(_get("uvrHome", "SING_UVR_HOME", r"C:\UVR"))
UVR_WEIGHT_NAME = str(_get("uvrWeight", "SING_UVR_WEIGHT", "MelBandRoformer.ckpt"))
UVR_CONFIG_NAME = str(_get(
    "uvrConfig", "SING_UVR_CONFIG", "config_vocals_mel_band_roformer_kim.yaml"))


def _venv_python(home, local_key, env_key):
    """venv 里的解释器：Windows 是 Scripts/python.exe，其它平台是 bin/python。

    先看显式配置（config.local.json 的 local_key / 环境变量 env_key），
    再按惯例从 home 猜。猜的时候两个平台都探一遍。
    """
    override = _get(local_key, env_key, None)
    if override:
        return _path(override)
    if home is None:
        return None
    for candidate in (home / "venv" / "Scripts" / "python.exe",
                      home / "venv" / "bin" / "python",
                      home / "venv" / "Scripts" / "python",
                      home / "venv" / "bin" / "python.exe"):
        if candidate.is_file():
            return candidate
    return home / "venv" / "Scripts" / "python.exe"


# 分离阶段必须用 UVR 自己的环境；换声阶段必须用 RVC 自己的环境。
# 两者依赖互相冲突，不能合并（见 docs/TROUBLESHOOTING.md）。
UVR_PY = _venv_python(UVR_HOME, "uvrPy", "SING_UVR_PY")
RVC_PY = _path(_get("rvcPy", "SING_RVC_PY",
                    str(RVC_HOME / "runtime" / "python.exe") if RVC_HOME else None))

# 主流水线解释器：默认就是当前正在跑的这个 python。
MAIN_PY = Path(os.sys.executable)


# ── 模型完整性校验（可选但强烈建议）─────────────────────────────────────────
# 仓库不带权重，所以哈希也没法预置。用 scripts/hash-models.py 生成 models.lock.json，
# 之后每次跑都会校验；没有这个文件就跳过校验并打印提示。
_LOCK_FILE = _LOCAL.get("modelsLock") or str(ROOT / "models.lock.json")


def load_model_lock():
    """models.lock.json 读不到就当没有（它是可选的），但读到了格式不对要提醒。"""
    path = Path(_LOCK_FILE)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        problems.append(
            f"models.lock.json 不是合法 JSON（第 {exc.lineno} 行）：{path} —— 本次跳过模型校验，"
            "建议删掉它重跑 scripts/hash-models.py")
        return {}
    if not isinstance(data, dict):
        problems.append(f"models.lock.json 顶层不是对象：{path} —— 本次跳过模型校验")
        return {}
    return data


_MODEL_LOCK = load_model_lock()


def expected_hash(path):
    """返回 models.lock.json 里记录的 sha256；没记录就返回 None（跳过校验）。"""
    if not path:
        return None
    for key in (Path(path).name, str(path)):
        value = _MODEL_LOCK.get(key)
        if isinstance(value, str) and len(value) == 64:
            return value.lower()
    return None


def as_dict():
    """打印诊断用；不含任何凭据。"""
    return {
        "dataRoot": str(DATA_ROOT),
        "voiceId": VOICE_ID,
        "rvcHome": str(RVC_HOME) if RVC_HOME else None,
        "rvcModel": RVC_MODEL_NAME,
        "rvcIndex": RVC_INDEX_NAME,
        "uvrHome": str(UVR_HOME) if UVR_HOME else None,
        "uvrWeight": UVR_WEIGHT_NAME,
        "uvrPy": str(UVR_PY) if UVR_PY else None,
        "rvcPy": str(RVC_PY) if RVC_PY else None,
        "mainPy": str(MAIN_PY),
        "modelsLock": _LOCK_FILE if _MODEL_LOCK else None,
        "localConfig": str(_LOCAL_FILE) if _LOCAL else None,
        "problems": list(problems),
    }
