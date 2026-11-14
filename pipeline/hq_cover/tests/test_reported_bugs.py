"""回归测试：用户实际踩过的四个坑。

每个用例对应一个真实反馈的 bug，防止以后改回去：

  ① stage 脚本被当独立脚本启动时报 ModuleNotFoundError: hq_cover
  ② --out 不在 <dataRoot>/runs/ 下 → GPU 跑完才炸
  ③ doctor 与 source 各查各的 Cookie 路径
  ④ 热门歌搜索被截断 → 直接 NeedsReview，必须手带 --track-id
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
PIPELINE = REPO / "pipeline"
sys.path.insert(0, str(PIPELINE))

from hq_cover import artifact_cache, config, source  # noqa: E402


class StageStandaloneTests(unittest.TestCase):
    """① stage 脚本必须能作为独立脚本启动，不依赖外部 PYTHONPATH。"""

    def _run_help(self, script):
        """用干净的 sys.path（只留标准库位置）跑 --help。"""
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, str(PIPELINE / "hq_cover" / script), "--help"],
            capture_output=True, text=True, env=env, cwd=str(PIPELINE), timeout=120,
        )

    def test_uvr_stage_runs_standalone(self):
        r = self._run_help("uvr_stage.py")
        self.assertIn("--input", r.stdout + r.stderr, "uvr_stage.py --help 没有正常输出")
        self.assertNotIn("ModuleNotFoundError", r.stderr)

    def test_rvc_stage_runs_standalone(self):
        r = self._run_help("rvc_stage.py")
        self.assertIn("--input", r.stdout + r.stderr, "rvc_stage.py --help 没有正常输出")
        self.assertNotIn("ModuleNotFoundError", r.stderr)

    def test_stage_scripts_bootstrap_sys_path(self):
        """源码层面必须有 sys.path 自举，否则换个启动方式又会坏。"""
        for name in ("uvr_stage.py", "rvc_stage.py"):
            text = (PIPELINE / "hq_cover" / name).read_text(encoding="utf-8")
            self.assertIn("sys.path.insert", text,
                          f"{name} 缺少 sys.path 自举；独立运行时 import hq_cover 会失败")

    def test_run_stage_injects_pythonpath(self):
        """pipeline.run_stage 必须给子进程注入 PYTHONPATH。"""
        text = (PIPELINE / "hq_cover" / "pipeline.py").read_text(encoding="utf-8")
        self.assertIn("PYTHONPATH", text, "run_stage 没有注入 PYTHONPATH")


class RunDirTests(unittest.TestCase):
    """② --out 必须在 <dataRoot>/runs/ 下，而且要在开工前就校验。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.runs = self.tmp / "runs"
        self.runs.mkdir()
        self._patch = mock.patch.object(artifact_cache, "RUNS", self.runs)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_accepts_path_inside_runs(self):
        target = self.runs / "my-run"
        self.assertEqual(artifact_cache.ensure_run_dir(target), target.resolve())

    def test_rejects_path_outside_runs_with_actionable_message(self):
        bad = self.tmp / "test-1"
        with self.assertRaises(ValueError) as ctx:
            artifact_cache.ensure_run_dir(bad)
        message = str(ctx.exception)
        self.assertIn("runs", message)
        # 报错必须给出可以直接照抄的正确路径
        self.assertIn(str(self.runs / "test-1"), message)

    def test_rejects_existing_directory(self):
        existing = self.runs / "already-there"
        existing.mkdir()
        with self.assertRaises(ValueError) as ctx:
            artifact_cache.ensure_run_dir(existing)
        self.assertIn("已存在", str(ctx.exception))

    def test_validated_before_any_gpu_work(self):
        """pipeline.process 必须在校验 run 目录之后才碰 GPU。"""
        text = (PIPELINE / "hq_cover" / "pipeline.py").read_text(encoding="utf-8")
        body = text[text.index("def process("):]
        ensure_pos = body.find("ensure_run_dir")
        stage_pos = body.find("stage('uvr_melband_separation')")
        self.assertGreater(ensure_pos, -1, "process() 没有调用 ensure_run_dir")
        self.assertGreater(stage_pos, -1)
        self.assertLess(ensure_pos, stage_pos, "ensure_run_dir 必须在 GPU 阶段之前")

    def test_suggested_path_is_usable(self):
        """报错里建议的路径，照着建应该能用。"""
        bad = self.tmp / "test-2"
        with self.assertRaises(ValueError) as ctx:
            artifact_cache.ensure_run_dir(bad)
        suggested = self.runs / bad.name
        self.assertEqual(artifact_cache.ensure_run_dir(suggested), suggested.resolve())


class CookiePathTests(unittest.TestCase):
    """③ Cookie 路径只能有一处真相。"""

    def test_config_exposes_single_source(self):
        self.assertTrue(hasattr(config, "NETEASE_COOKIE"))
        self.assertTrue(hasattr(config, "netease_cookie_candidates"))
        self.assertTrue(hasattr(config, "netease_cookie_path"))

    def test_candidates_are_ordered_and_complete(self):
        candidates = config.netease_cookie_candidates()
        self.assertGreaterEqual(len(candidates), 1)
        # 默认部署时 dataRoot 就是 pipeline/，两个候选去重后只剩一个；
        # 但只要有 dataRoot 分离，就必须多出一个候选。
        self.assertIn("secrets", str(candidates[0]))
        with mock.patch.object(config, "DATA_ROOT", Path("X:/other-data")):
            separated = config.netease_cookie_candidates()
        self.assertGreaterEqual(len(separated), 2,
                                "dataRoot 与 pipeline 分离时必须给出两个候选位置")
        self.assertTrue(any("X:/other-data" in str(p).replace("\\", "/") for p in separated),
                        "dataRoot 分离时应该包含 <dataRoot>/secrets 候选")

    def test_env_var_wins(self):
        with mock.patch.dict(os.environ, {"SING_NETEASE_COOKIES": "X:/custom/cookie.json"}):
            candidates = config.netease_cookie_candidates()
        self.assertEqual(str(candidates[0]).replace("\\", "/"), "X:/custom/cookie.json")

    def test_song_source_agrees_with_config(self):
        """tools/song_source.py 解析出的路径必须和 config 一致。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location("_ss_test", PIPELINE / "tools" / "song_source.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(
            Path(module.COOKIE_FILE).resolve(),
            Path(config.NETEASE_COOKIE).resolve(),
            "song_source 与 config 解析到了不同的 Cookie 路径（这正是那个 bug）",
        )

    def test_doctor_reports_same_path(self):
        """doctor.py 必须问 config，不能自己拼路径。"""
        text = (REPO / "scripts" / "doctor.py").read_text(encoding="utf-8")
        self.assertIn("config.NETEASE_COOKIE", text)
        self.assertNotIn('Path(config.DATA_ROOT) / "secrets"', text,
                         "doctor.py 还在自己拼 dataRoot/secrets 路径")


class TruncatedSearchTests(unittest.TestCase):
    """④ 搜索被截断时不能直接放弃，要走自动选版。"""

    def test_source_uses_resolver_on_truncation(self):
        text = (PIPELINE / "hq_cover" / "source.py").read_text(encoding="utf-8")
        self.assertIn("resolve_auto_original", text,
                      "截断分支没有接自动选版，热门歌会一直卡住")
        # 旧的「直接抛」写法必须已经去掉
        self.assertNotIn(
            'raise NeedsReview("Search results are incomplete; review an exact official track ID.")',
            text,
            "旧的硬闸门还在：截断时直接抛 NeedsReview 而不是自动选版",
        )

    def test_still_fails_closed_when_resolver_cannot_decide(self):
        """自动选版选不出来时，仍然要抛 NeedsReview —— 标准不能放宽。"""
        text = (PIPELINE / "hq_cover" / "source.py").read_text(encoding="utf-8")
        self.assertIn("无法自动确定唯一原版", text)

    def test_resolver_takes_only_track_id(self):
        """只用 resolver 的 track_id，避免两处 track 结构不一致。"""
        text = (PIPELINE / "hq_cover" / "source.py").read_text(encoding="utf-8")
        self.assertIn('track_id = int(resolved["track_id"])', text)


if __name__ == "__main__":
    unittest.main()
