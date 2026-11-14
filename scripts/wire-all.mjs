#!/usr/bin/env node
// 一条命令接完线：装配 + 路由 + MCP 工具注册 + 全量验证。
//
// 用法：
//   node scripts/wire-all.mjs <path/to/qq-bridge>
//   node scripts/wire-all.mjs <path/to/qq-bridge> --dry-run
//   node scripts/wire-all.mjs <path/to/qq-bridge> --revert
//   node scripts/wire-all.mjs <path/to/qq-bridge> --skip-copy   # 已经复制过模块

import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '..');
const SRC = path.join(REPO, 'bridge', 'src');

const MODULES = [
  'cover-job.js', 'song-intent.js', 'pro-song.js', 'bilibili-song.js',
  'relay-song-scope.js', 'song-job-notifier.js', 'voice-delivery-receipts.js',
  'sing-wiring.js', 'sing-endpoints.js', 'relay-endpoints.js', 'mcp-sing-tools.js',
];

function run(script, args) {
  const r = spawnSync(process.execPath, [path.join(HERE, script), ...args], {
    encoding: 'utf8',
    stdio: 'pipe',
  });
  return { ok: r.status === 0, out: (r.stdout || '') + (r.stderr || ''), status: r.status };
}

function main() {
  const argv = process.argv.slice(2);
  const target = argv.find((a) => !a.startsWith('--'));
  const dryRun = argv.includes('--dry-run');
  const revert = argv.includes('--revert');
  const skipCopy = argv.includes('--skip-copy');

  if (!target) {
    console.error('用法：node scripts/wire-all.mjs <path/to/qq-bridge> [--dry-run|--revert|--skip-copy]');
    process.exit(1);
  }
  const bridgeDir = path.resolve(target);
  if (!fs.existsSync(path.join(bridgeDir, 'src', 'bridge.js'))) {
    console.error(`\n✗ ${bridgeDir} 看起来不是 qq-bridge 根目录（缺 src/bridge.js）\n`);
    process.exit(1);
  }

  // ── revert ──────────────────────────────────────────────────
  if (revert) {
    console.log('── 还原 ──');
    for (const s of ['wire-bridge.mjs', 'wire-mcp.mjs']) {
      const r = run(s, [bridgeDir, '--revert']);
      console.log((r.ok ? '✓ ' : '✗ ') + s.replace('.mjs', '') + '：' + r.out.trim().split('\n')[0]);
    }
    return;
  }

  // ── 1. 复制模块 ──────────────────────────────────────────────
  if (!skipCopy) {
    console.log('── [1/4] 复制模块 ──');
    let copied = 0;
    for (const name of MODULES) {
      const from = path.join(SRC, name);
      if (!fs.existsSync(from)) {
        console.error(`✗ 本仓库缺 ${name}，无法继续`);
        process.exit(1);
      }
      const to = path.join(bridgeDir, 'src', name);
      if (dryRun) {
        console.log(`  would copy ${name}`);
      } else {
        fs.copyFileSync(from, to);
        copied += 1;
      }
    }
    if (!dryRun) console.log(`✓ 复制 ${copied} 个模块到 ${path.join(bridgeDir, 'src')}`);
  } else {
    console.log('── [1/4] 跳过复制（--skip-copy）──');
  }

  // ── 2. 接线 bridge.js ───────────────────────────────────────
  console.log('\n── [2/4] 接线 bridge.js ──');
  const wb = run('wire-bridge.mjs', dryRun ? [bridgeDir, '--dry-run'] : [bridgeDir]);
  console.log(wb.out.trim());
  if (!wb.ok && !dryRun) {
    console.error('\n✗ bridge.js 接线失败，中止');
    process.exit(1);
  }

  // ── 3. 接线 MCP 工具 ────────────────────────────────────────
  console.log('\n── [3/4] 接线 MCP 工具 ──');
  const wm = run('wire-mcp.mjs', dryRun ? [bridgeDir, '--dry-run'] : [bridgeDir]);
  console.log(wm.out.trim());
  if (!wm.ok && !dryRun) {
    console.error('\n✗ mcp-snowluma-safe.js 接线失败，中止');
    process.exit(1);
  }

  if (dryRun) {
    console.log('\n（dry-run 结束，没有改任何文件。）');
    return;
  }

  // ── 4. 验证 ─────────────────────────────────────────────────
  console.log('\n── [4/4] 验证 ──');

  const cd = run('check-deps.mjs', [path.join(bridgeDir, 'src', 'bridge.js')]);
  const missing = /真正缺失（会阻止启动）：(\d+) 项/.exec(cd.out);
  if (missing) {
    const n = Number(missing[1]);
    console.log(`${n === 0 ? '✓' : '✗'} deps 检查：真正缺失 ${n} 项`);
    if (n > 0) console.log(cd.out);
  } else {
    console.log('· deps 检查跳过（输出不符合预期）');
  }

  const vm = run('verify-mcp-tools.mjs', [bridgeDir]);
  console.log(vm.out.trim());

  const s1 = run(path.join('..', 'bridge', 'src', 'selftest.mjs'), []);
  const s2 = run(path.join('..', 'bridge', 'src', 'selftest-mcp.mjs'), []);

  console.log(`
────────────────────────────────────────────
接线完成。最后三步（需要你手动做）：

  1. 装依赖（如果还没装）：
       cd ${bridgeDir} && npm install

  2. 在 config.json 的 socialV2 下加 sing 段（见 docs/BRIDGE_WIRING.md 第四节）

  3. 重启 DSH（工具清单在启动时读取），然后在 QQ 里发：
       唱首 我怀念的 孙燕姿

  出问题先看 docs/TROUBLESHOOTING.md，再看 bridge 的 state/bridge.log
`);
}

main();
