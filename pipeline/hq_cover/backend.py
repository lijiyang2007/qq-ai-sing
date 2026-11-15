"""统一的后端选择：CUDA / DirectML(AMD, Intel) / ROCm(Linux AMD)。

为什么单独一个模块：
  rvc_stage.py 和 uvr_stage.py 都要做同一件事 —— 挑一个能用的 torch 设备，
  然后给出对应的精度和分块参数。以前这段逻辑在两个文件里各写一遍，而且都
  只认 CUDA（`if not torch.cuda.is_available(): raise`），于是 AMD 卡直接
  被拒之门外，尽管上游 RVC / UVR 本身是支持 DirectML 的。

判定顺序（和 RVC 上游 config.py 的意图一致）：
  1. CUDA 可用且显存 >= 4 GiB  → cuda:0
  2. torch_directml 可用        → privateuseone:0（Windows 上的 AMD / Intel）
  3. 其它（Linux AMD 装 ROCm 时 torch 会把 cuda.is_available() 报成 True）→ 已在第 1 步覆盖
  4. 都没有                      → 明确报错，绝不静默退回 CPU

显存分档：
  <= 4 GiB 用上游的保守档 (x_pad=1, x_query=5, x_center=30, x_max=32)
  否则用标准档 (3, 10, 60, 65)

DirectML 的两个实测限制（写在这里免得以后又踩）：
  · 半精度支持不一致，默认用 fp32；
  · 部分算子没实现（例如某些 rmvpe / 自定义 kernel），失败时会抛
    "Could not run ... with arguments from the 'privateuseone' backend"，
    这种情况只能换 CUDA 卡或回退 CPU，没有通用补丁。
"""
from __future__ import annotations

import os


class BackendError(RuntimeError):
    """没有任何可用加速后端。信息里带上排查建议，不要只抛一句 CUDA 不可用。"""


def _cuda_usable(torch):
    try:
        if not torch.cuda.is_available():
            return False
        # 4 GiB 以下的卡上游会判定为不可用；这里保持同一门槛，避免跑到一半 OOM。
        total = torch.cuda.get_device_properties(0).total_memory
        # 门槛用 2 GiB：上游对 ≤4 GiB 的卡有专门的保守分块档，说明低显存是能跑的。
        # 设太高会把确实可用的小卡（例如 2/3 GB 的老卡）误判成不可用。
        return total >= 2 * 1024 ** 3
    except Exception:
        return False


def _directml_usable():
    """真的分配一个张量做一次加法 —— 只 import 成功不代表适配器可用。

    返回 (ok, module)。module 从 sys.modules 取，保证和 import 进来的是同一个
    对象（也方便测试里 mock）。
    """
    try:
        import torch_directml
    except Exception:
        return False, None
    try:
        import torch
        dml = torch_directml.device()
        probe = torch.tensor([1.0], dtype=torch.float32, device=dml) + 1.0
        probe.cpu()
        return True, torch_directml
    except Exception:
        return False, None


def select(allow_directml=True, prefer=None):
    """返回一个 dict，字段：kind / device / is_half / gpu_mem / gpu_name / detail / dims。

    prefer: None（自动）/ 'cuda' / 'dml'，可用环境变量 SING_BACKEND 覆盖。
    """
    import torch

    prefer = (prefer or os.environ.get("SING_BACKEND") or "").strip().lower() or None

    want_cuda = prefer in (None, "cuda", "rocm")
    want_dml = prefer in (None, "dml", "directml") and allow_directml

    if want_cuda and _cuda_usable(torch):
        total = torch.cuda.get_device_properties(0).total_memory
        gpu_mem = int(total / 1024 ** 3 + 0.4)
        # ROCm 在 Linux 上也是走 torch.cuda 这层 API，用 torch.version.hip 区分。
        is_rocm = bool(getattr(torch.version, "hip", None))
        return {
            "kind": "rocm" if is_rocm else "cuda",
            "device": "cuda:0",
            "is_half": True,
            "gpu_mem": gpu_mem,
            "gpu_name": torch.cuda.get_device_name(0),
            "detail": "torch.version.hip=%s" % getattr(torch.version, "hip", None) if is_rocm else "",
            "dims": (1, 5, 30, 32) if gpu_mem <= 4 else (3, 10, 60, 65),
        }

    if want_dml:
        ok, torch_directml = _directml_usable()
        if ok:
            dml = torch_directml.device()
            return {
                "kind": "dml",
                "device": str(dml),
                # DirectML 的半精度支持不一致，默认 fp32；上游 RVC 的 dml 分支也用 fp32。
                "is_half": False,
                # torch_directml 没有可靠的显存查询，按保守档处理。
                "gpu_mem": 0,
                "gpu_name": "DirectML adapter",
                "detail": "torch_directml",
                # DirectML 更慢且算子覆盖有限，用最小分块换稳定性。
                "dims": (1, 5, 30, 32),
            }

    # 走到这里说明没有可用加速设备。信息里要能直接指导下一步。
    hints = [
        "NVIDIA: 装 CUDA 版 torch（pip install torch --index-url https://download.pytorch.org/whl/cu121）",
        "AMD/Intel (Windows): 装 DirectML（pip install torch-directml），并保留 torch_directml 可导入",
        "AMD (Linux): 用 ROCm 版 torch，torch.cuda.is_available() 会返回 True",
        "显存 <4 GiB 的卡会被上游判定为不可用；可换更小的分块或换卡",
    ]
    raise BackendError(
        "没有可用的 GPU 加速后端。\n  "
        + "\n  ".join(hints)
        + "\n  当前 prefer=%r，torch=%s，cuda.is_available()=%s"
        % (prefer, torch.__version__, torch.cuda.is_available())
    )
