#!/usr/bin/env python
"""生成 models.lock.json：把你自己那套权重的 sha256 记下来。

仓库不附带任何模型，所以哈希也没法预置。跑一次这个脚本，之后每次渲染都会
校验权重没被换过（缓存命中判断也依赖它）。

用法（在仓库根目录）：
    python scripts/hash-models.py
    python scripts/hash-models.py --out pipeline/models.lock.json

注意：models.lock.json 在 .gitignore 里——它记录的是你这台机器的模型指纹，
不该跟着仓库走。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

try:
    from hq_cover import config
except Exception as exc:  # pragma: no cover
    raise SystemExit("无法导入 hq_cover.config：%s" % exc)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "pipeline" / "models.lock.json"))
    ap.add_argument("--force", action="store_true", help="覆盖已存在的 lock 文件")
    args = ap.parse_args()

    out = Path(args.out)
    if out.exists() and not args.force:
        raise SystemExit("%s 已存在；确认要重写请加 --force" % out)

    targets = []
    rvc = config.RVC_HOME
    uvr = config.UVR_HOME
    if rvc:
        targets += [
            rvc / "assets" / "weights" / config.RVC_MODEL_NAME,
            rvc / "logs" / config.RVC_INDEX_NAME,
            rvc / config.RVC_HUBERT,
            rvc / config.RVC_RMVPE,
        ]
    if uvr:
        targets += [uvr / "models" / config.UVR_WEIGHT_NAME,
                    uvr / "models" / config.UVR_CONFIG_NAME]

    lock = {}
    missing = []
    for path in targets:
        if path is None or not Path(path).is_file():
            missing.append(str(path))
            continue
        digest = sha256(path)
        lock[Path(path).name] = digest
        print("%-64s %s" % (Path(path).name, digest), flush=True)

    if missing:
        print("\n以下文件不存在，已跳过：", flush=True)
        for item in missing:
            print("  " + item, flush=True)

    if not lock:
        raise SystemExit("\n一个文件都没找到；先按 docs/INSTALL.md 把 RVC / UVR 装好，"
                         "并在 pipeline/config.local.json 里指对路径。")

    out.write_text(json.dumps(lock, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    print("\n已写入 %s（%d 项）" % (out, len(lock)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
