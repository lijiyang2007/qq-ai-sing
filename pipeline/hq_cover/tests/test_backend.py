"""回归测试：后端选择必须支持 CUDA / ROCm / DirectML，且低显存有保守档。

背景：原来 rvc_stage.py 和 uvr_stage.py 都写死
`if not torch.cuda.is_available(): raise`，于是 AMD 卡（Windows 上应该走
DirectML）被直接拒之门外 —— 尽管上游 RVC / UVR 本身支持。

这几个用例锁定：
  · 三条后端分支各自选对设备与精度
  · ≤4 GB 显存走保守分块档
  · 全都没有时抛 BackendError（带可操作建议），不静默退 CPU
  · 两个 stage 脚本里不再有写死的 CUDA 硬检查
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
PIPELINE = REPO / "pipeline"
sys.path.insert(0, str(PIPELINE))

from hq_cover import backend  # noqa: E402

try:
    import torch  # noqa: F401
    HAS_TORCH = True
except Exception:
    HAS_TORCH = False


@unittest.skipUnless(HAS_TORCH, "需要 torch")
class BackendSelectionTests(unittest.TestCase):
    def test_cuda_branch(self):
        props = types.SimpleNamespace(total_memory=8 * 1024 ** 3)
        with mock.patch.object(torch.cuda, "get_device_properties", return_value=props), \
             mock.patch.object(torch.cuda, "get_device_name", return_value="Fake 4060"):
            chosen = backend.select()
        self.assertEqual(chosen["kind"], "cuda")
        self.assertEqual(chosen["device"], "cuda:0")
        self.assertTrue(chosen["is_half"])
        self.assertEqual(chosen["gpu_mem"], 8)

    def test_low_vram_uses_conservative_dims(self):
        """≤4 GB 必须走上游的保守档，否则会 OOM。"""
        for gib in (2, 3, 4):
            props = types.SimpleNamespace(total_memory=gib * 1024 ** 3)
            with mock.patch.object(torch.cuda, "get_device_properties", return_value=props), \
                 mock.patch.object(torch.cuda, "get_device_name", return_value="GTX 1650"):
                chosen = backend.select()
            self.assertEqual(chosen["dims"], (1, 5, 30, 32),
                             f"{gib} GiB 应该用保守分块档")

    def test_absurdly_small_vram_rejected(self):
        """1 GiB 以下拒绝 —— 那不是能跑的量级。"""
        props = types.SimpleNamespace(total_memory=1 * 1024 ** 3)
        with mock.patch.object(torch.cuda, "get_device_properties", return_value=props), \
             mock.patch.object(torch.cuda, "get_device_name", return_value="ancient"), \
             mock.patch.object(backend, "_directml_usable", return_value=(False, None)):
            with self.assertRaises(backend.BackendError):
                backend.select()

    def test_directml_branch(self):
        fake = mock.MagicMock()
        fake.device.return_value = "privateuseone:0"
        with mock.patch.object(backend, "_cuda_usable", return_value=False), \
             mock.patch.object(backend, "_directml_usable", return_value=(True, fake)):
            chosen = backend.select()
        self.assertEqual(chosen["kind"], "dml")
        self.assertEqual(chosen["device"], "privateuseone:0")
        # DirectML 半精度不可靠，必须 fp32
        self.assertFalse(chosen["is_half"], "DirectML 默认不能用 fp16")
        self.assertEqual(chosen["dims"], (1, 5, 30, 32))

    def test_no_backend_fails_loudly(self):
        with mock.patch.object(backend, "_cuda_usable", return_value=False), \
             mock.patch.object(backend, "_directml_usable", return_value=(False, None)):
            with self.assertRaises(backend.BackendError) as ctx:
                backend.select()
        message = str(ctx.exception)
        # 报错必须能直接指导下一步，而不是只说"不可用"
        self.assertIn("NVIDIA", message)
        self.assertIn("DirectML", message)
        self.assertIn("ROCm", message)

    def test_forced_backend_via_env(self):
        fake = mock.MagicMock()
        fake.device.return_value = "privateuseone:0"
        with mock.patch.dict("os.environ", {"SING_BACKEND": "dml"}), \
             mock.patch.object(backend, "_cuda_usable", return_value=True), \
             mock.patch.object(backend, "_directml_usable", return_value=(True, fake)):
            chosen = backend.select()
        # prefer=dml 时不应被 CUDA 抢走
        self.assertEqual(chosen["kind"], "dml")


class StageNoLongerHardcodesCudaTests(unittest.TestCase):
    """两个 stage 不能再写死 CUDA。"""

    def test_rvc_stage_uses_backend_select(self):
        text = (PIPELINE / "hq_cover" / "rvc_stage.py").read_text(encoding="utf-8")
        self.assertIn("backend.select()", text)
        self.assertNotIn(
            'raise RuntimeError("This verified FP16 RVC stage requires CUDA")', text,
            "rvc_stage 还在写死 CUDA")
        self.assertNotIn(
            'raise RuntimeError("RVC FP16 stage requires CUDA; no mutating CPU fallback")', text,
            "rvc_stage 的 device_config 还在写死 CUDA")

    def test_uvr_stage_uses_backend_select(self):
        text = (PIPELINE / "hq_cover" / "uvr_stage.py").read_text(encoding="utf-8")
        self.assertIn("backend.select()", text)
        self.assertNotIn(
            'raise RuntimeError("This verified UVR stage requires CUDA; no CPU fallback")', text,
            "uvr_stage 还在写死 CUDA")

    def test_uvr_stage_passes_directml_flag(self):
        """UVR 的 worker 必须收到 is_using_directml 标志。"""
        text = (PIPELINE / "hq_cover" / "uvr_stage.py").read_text(encoding="utf-8")
        self.assertIn("is_using_directml=is_dml", text)

    def test_cuda_only_backend_attrs_are_guarded(self):
        """torch.backends.cuda / cudnn 在 DirectML 环境可能不存在，必须 hasattr 保护。"""
        for name in ("rvc_stage.py", "uvr_stage.py"):
            text = (PIPELINE / "hq_cover" / name).read_text(encoding="utf-8")
            self.assertIn('hasattr(torch.backends, "cuda")', text, f"{name} 未保护 torch.backends.cuda")
            self.assertIn('hasattr(torch.backends, "cudnn")', text, f"{name} 未保护 torch.backends.cudnn")


class DocsClaimTests(unittest.TestCase):
    """文档不能再说「必须 NVIDIA / 8 GB 起」。"""

    def test_install_no_longer_requires_nvidia(self):
        text = (REPO / "docs" / "INSTALL.md").read_text(encoding="utf-8")
        self.assertNotIn("全程假设 Windows + NVIDIA", text)
        self.assertNotIn("NVIDIA 8 GB 显存", text)
        # 应该明确提到 AMD 和 DirectML
        self.assertIn("DirectML", text)
        self.assertIn("ROCm", text)

    def test_readme_mentions_4gb(self):
        text = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn("4 GB", text, "README 应该说明 4 GB 显存也能跑")
        self.assertNotIn("**8 GB 显存**是下限", text)

    def test_troubleshooting_documents_directml_limits(self):
        text = (REPO / "docs" / "TROUBLESHOOTING.md").read_text(encoding="utf-8")
        self.assertIn("privateuseone", text, "应记录 DirectML 的算子报错")
        self.assertIn("torch-directml", text)


if __name__ == "__main__":
    unittest.main()
