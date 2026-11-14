#!/usr/bin/env python
"""环境自检：装完先跑这个，比直接点歌省时间。

用法（仓库根目录）：
    python scripts/doctor.py
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

OK, WARN, BAD = "  OK  ", " WARN ", " FAIL "

results = []


def check(level, name, detail=""):
    results.append((level, name, detail))


def main() -> int:
    print("== qq-ai-sing 自检 ==\n")
    try:
        from hq_cover import config
    except Exception as exc:
        print("[FAIL] 无法导入 hq_cover.config：%s" % exc)
        return 2

    info = config.as_dict()
    for key in ("dataRoot", "voiceId", "rvcHome", "uvrHome", "mainPy"):
        print("  %-10s %s" % (key, info.get(key)))
    print()

    # 配置文件本身
    local_cfg = info.get("localConfig")
    if local_cfg:
        check(OK, "config.local.json", "已加载：%s" % local_cfg)
    else:
        check(WARN, "config.local.json",
              "没有（全部走默认值/环境变量）。要自定义就 cp config.example.json config.local.json")
    for problem in info.get("problems") or []:
        check(WARN, "配置问题", problem)

    # Python
    check(OK, "Python", sys.version.split()[0])
    try:
        import numpy, soundfile, scipy  # noqa: F401
        check(OK, "numpy/soundfile/scipy", "已安装")
    except Exception as exc:
        check(BAD, "numpy/soundfile/scipy", str(exc))
    try:
        import requests  # noqa: F401
        check(OK, "requests", "已安装")
    except Exception as exc:
        check(BAD, "requests", str(exc))

    # 三个解释器
    for label, path in (("UVR 解释器", config.UVR_PY),
                        ("RVC 解释器", config.RVC_PY)):
        if path and Path(path).is_file():
            check(OK, label, str(path))
        else:
            check(BAD, label, "找不到：%s（改 config.local.json 的 uvrPy / rvcPy）" % path)

    # RVC 资源
    rvc = config.RVC_HOME
    if not rvc or not rvc.is_dir():
        check(BAD, "RVC 目录", "找不到 %s" % rvc)
    else:
        check(OK, "RVC 目录", str(rvc))
        for label, p in (
            ("音色模型", rvc / "assets" / "weights" / config.RVC_MODEL_NAME),
            ("检索索引", rvc / "logs" / config.RVC_INDEX_NAME),
            ("HuBERT", rvc / config.RVC_HUBERT),
            ("RMVPE", rvc / config.RVC_RMVPE),
        ):
            check(OK if Path(p).is_file() else BAD, label,
                  str(p) if Path(p).is_file() else "缺失 %s（见 docs/TRAIN_VOICE.md）" % p)

    # UVR 资源
    uvr = config.UVR_HOME
    if not uvr or not uvr.is_dir():
        check(BAD, "UVR 目录", "找不到 %s" % uvr)
    else:
        check(OK, "UVR 目录", str(uvr))
        for label, p in (("分离权重", uvr / "models" / config.UVR_WEIGHT_NAME),
                         ("分离配置", uvr / "models" / config.UVR_CONFIG_NAME)):
            check(OK if Path(p).is_file() else BAD, label,
                  str(p) if Path(p).is_file() else "缺失 %s（见 docs/INSTALL.md）" % p)

    # 网易云 Cookie
    cookie = Path(config.DATA_ROOT) / "secrets" / "netease_cookies.json"
    if cookie.is_file():
        try:
            data = json.loads(cookie.read_text(encoding="utf-8"))
            check(OK if data else WARN, "网易云 Cookie",
                  "%d 个字段" % len(data) if data else "文件是空的")
        except Exception as exc:
            check(BAD, "网易云 Cookie", "解析失败：%s" % exc)
    else:
        check(WARN, "网易云 Cookie",
              "没有 %s —— 只能取到 128k 试听，无损流程会暂停" % cookie)

    # 模型指纹
    lock = Path(config.ROOT) / "models.lock.json"
    check(OK if lock.is_file() else WARN, "models.lock.json",
          "已生成" if lock.is_file() else "未生成，跑 python scripts/hash-models.py")

    # 桥接接线模块（只有要用 QQ 时才需要）
    bridge_src = Path(config.ROOT).parents[0] / "bridge" / "src"
    if not bridge_src.is_dir():
        bridge_src = Path(__file__).resolve().parents[1] / "bridge" / "src"
    if bridge_src.is_dir():
        required = ["cover-job.js", "sing-wiring.js", "sing-endpoints.js",
                    "relay-endpoints.js", "mcp-sing-tools.js",
                    "selftest.mjs", "selftest-mcp.mjs"]
        missing = [n for n in required if not (bridge_src / n).is_file()]
        check(OK if not missing else WARN, "桥接模块",
              "齐全（%d 个）" % len(required) if not missing else "缺 %s" % ", ".join(missing))
    else:
        check(WARN, "桥接模块", "找不到 bridge/src（只有要接 QQ 时才需要）")

    # 交付通道
    if shutil.which("node"):
        check(OK, "node", shutil.which("node"))
    else:
        check(WARN, "node", "没装；只有要用 QQ 桥接时才需要")

    print()
    width = max(len(n) for _, n, _ in results) + 2
    for level, name, detail in results:
        print("[%s] %-*s %s" % (level.strip(), width, name, detail))

    bad = sum(1 for lv, _, _ in results if lv == BAD)
    warn = sum(1 for lv, _, _ in results if lv == WARN)
    print("\n%d 项失败 / %d 项提醒" % (bad, warn))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
