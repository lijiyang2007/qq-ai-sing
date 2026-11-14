"""仓库自身的健康检查：配置文件合法性、接线模块完整性。

这些用例防的是「代码能跑，但照文档操作的人会被坑」这一类问题：
  · config.example.json 不合法 → 用户复制过去后静默回落默认值
  · bridge/src 少文件 → 文档说复制 7 个/13 个，实际对不上
  · 文档里引用的文件不存在 → 读者点进去 404

它们不碰网络、GPU、QQ，纯静态检查。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PIPELINE = REPO / "pipeline"
BRIDGE_SRC = REPO / "bridge" / "src"


class ExampleConfigTests(unittest.TestCase):
    """config.example.json 必须能被 json.load 直接吃下去。"""

    def test_example_is_valid_json(self):
        path = PIPELINE / "config.example.json"
        self.assertTrue(path.is_file(), f"缺少 {path}")
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        self.assertIsInstance(data, dict)

    def test_example_keys_are_known(self):
        """示例里的键必须都是 config.py 认识的，否则用户填了也没用。"""
        data = json.loads((PIPELINE / "config.example.json").read_text(encoding="utf-8-sig"))
        known = {
            "_说明",
            "dataRoot", "voiceId", "voiceLabel",
            "rvcHome", "rvcModel", "rvcIndex",
            "uvrHome", "uvrWeight", "uvrConfig",
            "rvcPy", "uvrPy", "modelsLock",
        }
        unknown = set(data) - known
        self.assertFalse(unknown, f"config.example.json 里有 config.py 不认识的键：{sorted(unknown)}")

    def test_example_has_no_comments(self):
        """防回归：有人手贱把 # 或 // 注释加回去。"""
        text = (PIPELINE / "config.example.json").read_text(encoding="utf-8-sig")
        stripped = text.lstrip()
        self.assertTrue(stripped.startswith("{"), "config.example.json 必须以 { 开头")
        for lineno, line in enumerate(text.splitlines(), 1):
            body = line.strip()
            if not body:
                continue
            self.assertFalse(
                body.startswith("#") or body.startswith("//"),
                f"config.example.json 第 {lineno} 行是注释，JSON 不支持：{body[:60]}",
            )


class BridgeModuleTests(unittest.TestCase):
    """bridge/src 里文档承诺的模块必须都在。"""

    EXPECTED = {
        # 核心模块（7 个）
        "cover-job.js", "song-intent.js", "pro-song.js", "bilibili-song.js",
        "relay-song-scope.js", "song-job-notifier.js", "voice-delivery-receipts.js",
        # 接线模块
        "sing-wiring.js", "sing-endpoints.js", "relay-endpoints.js", "mcp-sing-tools.js",
        # 自检脚本
        "selftest.mjs", "selftest-mcp.mjs",
    }

    def test_all_modules_present(self):
        missing = sorted(n for n in self.EXPECTED if not (BRIDGE_SRC / n).is_file())
        self.assertFalse(missing, f"bridge/src 缺少模块：{missing}")

    def test_modules_are_non_trivial(self):
        """空文件也算缺失——防止有人用 touch 糊弄。"""
        for name in self.EXPECTED:
            path = BRIDGE_SRC / name
            if not path.is_file():
                continue
            self.assertGreater(path.stat().st_size, 500, f"{name} 太小了，可能是空壳")

    def test_wiring_exports_assembler(self):
        src = (BRIDGE_SRC / "sing-wiring.js").read_text(encoding="utf-8")
        self.assertIn("export function createSingWiring", src)

    def test_mcp_tools_export_factory(self):
        src = (BRIDGE_SRC / "mcp-sing-tools.js").read_text(encoding="utf-8")
        self.assertRegex(src, r"export (function|const) \w*[Ss]ing\w*[Tt]ool")


class DocsTests(unittest.TestCase):
    """文档里引用的仓库内文件必须存在。"""

    DOCS = [
        REPO / "README.md",
        REPO / "docs" / "INSTALL.md",
        REPO / "docs" / "TRAIN_VOICE.md",
        REPO / "docs" / "BRIDGE_WIRING.md",
        REPO / "docs" / "TROUBLESHOOTING.md",
        REPO / "docs" / "PRIVACY.md",
    ]

    def test_docs_exist(self):
        missing = [str(p.relative_to(REPO)) for p in self.DOCS if not p.is_file()]
        self.assertFalse(missing, f"缺少文档：{missing}")

    def test_relative_links_resolve(self):
        """Markdown 里的相对链接指向的文件要真实存在（外链跳过）。"""
        pattern = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
        broken = []
        for doc in self.DOCS:
            if not doc.is_file():
                continue
            for target in pattern.findall(doc.read_text(encoding="utf-8")):
                target = target.split("#")[0].strip()
                if not target or "://" in target or target.startswith("mailto:"):
                    continue
                if not (doc.parent / target).exists():
                    broken.append(f"{doc.name} -> {target}")
        self.assertFalse(broken, "文档里有指向不存在文件的链接：\n  " + "\n  ".join(broken))

    def test_readme_has_no_stale_test_count(self):
        """README 里不要再写死会过时的测试用例总数。

        允许写「23 个测试文件」（文件数变化慢，且不是"跑了多少个"），
        不允许写「280 个测试」这种会随用例增删漂移的数字。
        """
        text = (REPO / "README.md").read_text(encoding="utf-8")
        # 只抓「N 个测试」后面不跟「文件」的写法
        pattern = re.compile(r"(\d{2,4})\s*个测试(?!文件)")
        for line in text.splitlines():
            hit = pattern.search(line)
            self.assertIsNone(
                hit,
                f"README 里写死了测试用例数量 {hit.group(1) if hit else ''}，它会过时；"
                f"改成「N 个测试文件」或直接写命令。\n  行：{line.strip()}",
            )


class WiringScriptTests(unittest.TestCase):
    """接线脚本必须存在且语法正确——它们是「clone 下来能不能真跑起来」的关键。"""

    SCRIPTS = [
        "wire-all.mjs",
        "wire-bridge.mjs",
        "wire-mcp.mjs",
        "check-deps.mjs",
        "verify-mcp-tools.mjs",
    ]

    def test_scripts_exist(self):
        missing = [n for n in self.SCRIPTS if not (REPO / "scripts" / n).is_file()]
        self.assertFalse(missing, f"scripts/ 缺少：{missing}")

    @unittest.skipUnless(shutil.which("node"), "需要 node")
    def test_scripts_pass_node_check(self):
        bad = []
        for name in self.SCRIPTS:
            path = REPO / "scripts" / name
            if not path.is_file():
                continue
            result = subprocess.run(
                [shutil.which("node"), "--check", str(path)],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                bad.append(f"{name}: {result.stderr.strip().splitlines()[0] if result.stderr else '?'}")
        self.assertFalse(bad, "接线脚本语法错误：\n  " + "\n  ".join(bad))

    @unittest.skipUnless(shutil.which("node"), "需要 node")
    def test_wire_all_has_dry_run(self):
        """wire-all 必须支持 --dry-run，否则用户不敢跑。"""
        src = (REPO / "scripts" / "wire-all.mjs").read_text(encoding="utf-8")
        self.assertIn("--dry-run", src)
        self.assertIn("--revert", src)

    def test_wire_all_reverts_on_failure(self):
        """接线脚本必须在语法检查失败时自动还原。"""
        for name in ("wire-bridge.mjs", "wire-mcp.mjs"):
            src = (REPO / "scripts" / name).read_text(encoding="utf-8")
            self.assertIn("--check", src, f"{name} 没有 node --check 自检")
            self.assertIn("bak", src, f"{name} 没有备份机制")


class NoMachinePathsTests(unittest.TestCase):
    """仓库里不能出现任何开发机特有的绝对路径。

    为什么单独一条：这类东西不会让测试失败，只会让别人的部署静默走错默认值
    —— 具体踩到的就是 cover-job.js 里 `s.root ?? 'D:/AI-Song'`，
    以及 relay-song-scope.js 里两处同样的默认值。
    """

    # 开发机/作者本机的特征串
    FORBIDDEN = [
        "C:\\Users\\",
        "C:/Users/",
        "Administrator\\Documents",
        "dsh-runtimes",
        "default-workspace",
        "AI-Song",
        "AI-UVR-2026",
        "AI-RVC",
    ]
    ALLOWED_FILES = {
        # 隐私文档里教人怎么扫这些串，必然出现
        "PRIVACY.md",
        # 本文件自己就写着这些特征串，否则没法做检查
        "test_repo_health.py",
    }

    def _scan(self, path):
        hits = []
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            for needle in self.FORBIDDEN:
                if needle in line:
                    hits.append(f"{path.name}:{lineno}: {line.strip()[:110]}")
                    break
        return hits

    def test_no_machine_specific_paths(self):
        bad = []
        for path in REPO.rglob("*"):
            if not path.is_file():
                continue
            if ".git" in path.parts or "__pycache__" in path.parts:
                continue
            if path.suffix not in {".py", ".js", ".mjs", ".json", ".md"}:
                continue
            if path.name in self.ALLOWED_FILES:
                continue
            bad.extend(self._scan(path))
        self.assertFalse(
            bad,
            "仓库里出现了开发机特有路径（别人部署会静默走错默认值）：\n  "
            + "\n  ".join(bad[:20]),
        )

    def test_sing_root_has_no_default(self):
        """cover-job.js 的 sing.root 不能有写死的默认值。"""
        src = (REPO / "bridge" / "src" / "cover-job.js").read_text(encoding="utf-8")
        self.assertNotRegex(
            src,
            r"s\.root\s*\?\?\s*'[A-Za-z]:",
            "sing.root 又被写死了默认路径；应为空值，由配置决定",
        )

    def test_relay_scope_has_no_default_root(self):
        src = (REPO / "bridge" / "src" / "relay-song-scope.js").read_text(encoding="utf-8")
        self.assertNotRegex(
            src,
            r"\|\|\s*'[A-Za-z]:[\\\\/]",
            "relay-song-scope.js 里又有写死的盘符路径",
        )


class PackageJsonTests(unittest.TestCase):
    def test_valid_json(self):
        data = json.loads((REPO / "package.json").read_text(encoding="utf-8-sig"))
        self.assertIsInstance(data, dict)

    def test_has_test_script(self):
        data = json.loads((REPO / "package.json").read_text(encoding="utf-8-sig"))
        self.assertIn("test", data.get("scripts", {}), "package.json 缺少 test 脚本")


if __name__ == "__main__":
    unittest.main()
