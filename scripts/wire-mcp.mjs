#!/usr/bin/env node
// 自动接线第 4 步：把 qq_sing / qq_sing_status / qq_relay_targets / qq_relay_send
// 注册进 mcp-snowluma-safe.js。
//
// 为什么这步能自动化：上游的 `server.tool` 已经被改写成统一包装
// （开关过滤 + 会话绑定闸门 selfKeyGuard），所以只要在包装**之后**调用
// registerSingTools，这四个工具就自动继承全部防护，不需要逐个改。
//
// 用法：
//   node scripts/wire-mcp.mjs <path/to/qq-bridge>
//   node scripts/wire-mcp.mjs <path/to/qq-bridge> --dry-run
//   node scripts/wire-mcp.mjs <path/to/qq-bridge> --revert

import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

const MARK = '点歌 / 代唱工具（qq-ai-sing）';
const IMPORT_LINE = "import { registerSingTools, SING_TOOL_FLAGS } from './mcp-sing-tools.js';";

function die(msg) {
  console.error(`\n✗ ${msg}\n`);
  process.exit(1);
}

function braceEnd(src, openIndex) {
  let depth = 0;
  let i = openIndex;
  let state = 'code';
  while (i < src.length) {
    const c = src[i];
    const n = src[i + 1];
    if (state === 'code') {
      if (c === '/' && n === '/') { state = 'line'; i += 2; continue; }
      if (c === '/' && n === '*') { state = 'block'; i += 2; continue; }
      if (c === '"' || c === "'" || c === '`') { state = c; i += 1; continue; }
      if (c === '{') depth += 1;
      else if (c === '}') { depth -= 1; if (depth === 0) return i; }
    } else if (state === 'line') {
      if (c === '\n') state = 'code';
    } else if (state === 'block') {
      if (c === '*' && n === '/') { state = 'code'; i += 2; continue; }
    } else {
      if (c === '\\') { i += 2; continue; }
      if (c === state) state = 'code';
    }
    i += 1;
  }
  return -1;
}

const FLAGS_BLOCK = `
// ── 点歌 / 代唱工具名 → 开关（qq-ai-sing）──────────────────────────────
// 必须并进 TOOL_CONFIG_FLAGS：漏了的话控制台显示「已关闭」，但模型每次请求
// 仍收到完整 schema，调用后再吃 403 —— 白烧 token 还诱导误调。
Object.assign(TOOL_CONFIG_FLAGS, SING_TOOL_FLAGS);
// ──────────────────────────────────────────────────────────────────────
`;

const REGISTER_BLOCK = `
// ── 点歌 / 代唱工具（qq-ai-sing）───────────────────────────────────────
// 必须在 server.tool 被包装成 registerTool 之后调用：这样四个工具自动继承
// 开关过滤与 selfKeyGuard 会话绑定闸门，不需要逐个改。
registerSingTools(server, { z, agentApi, serializeModelData, cfg });
// ──────────────────────────────────────────────────────────────────────
`;

function main() {
  const args = process.argv.slice(2);
  const target = args.find((a) => !a.startsWith('--'));
  const dryRun = args.includes('--dry-run');
  const revert = args.includes('--revert');

  if (!target) die('用法：node scripts/wire-mcp.mjs <path/to/qq-bridge> [--dry-run|--revert]');

  const bridgeDir = path.resolve(target);
  const file = path.join(bridgeDir, 'src', 'mcp-snowluma-safe.js');
  if (!fs.existsSync(file)) {
    die(`找不到 ${file}\n参数应该是 qq-bridge 的根目录（含 src/mcp-snowluma-safe.js）。`);
  }

  const src = fs.readFileSync(file, 'utf8');

  if (revert) {
    const dir = path.dirname(file);
    const baks = fs.readdirSync(dir)
      .filter((n) => n.startsWith('mcp-snowluma-safe.js.bak-wire-'))
      .sort();
    if (!baks.length) die('没有找到 wire 备份，无法还原。');
    const latest = baks[baks.length - 1];
    fs.copyFileSync(path.join(dir, latest), file);
    console.log(`✓ 已还原自 ${latest}`);
    return;
  }

  if (src.includes(MARK)) {
    console.log('✓ 已经接过线，跳过（要重做先 --revert）');
    return;
  }

  // ── 锚点 1：import 区（接在 sensitive.js 那行之后）──────────────
  const importAnchor = "import { serializeModelData } from './qq-model-view.js';";
  if (!src.includes(importAnchor)) {
    die(`找不到 import 锚点：\n  ${importAnchor}\n请按 docs/BRIDGE_WIRING.md 3.4 手工接线。`);
  }

  // ── 锚点 2：TOOL_CONFIG_FLAGS 定义结束 ─────────────────────────
  const flagsAnchor = 'const TOOL_CONFIG_FLAGS = {';
  const flagsIdx = src.indexOf(flagsAnchor);
  if (flagsIdx < 0) {
    die(`找不到 TOOL_CONFIG_FLAGS：\n  ${flagsAnchor}\n请手工接线。`);
  }
  const flagsOpen = src.indexOf('{', flagsIdx);
  const flagsClose = braceEnd(src, flagsOpen);
  if (flagsClose < 0) die('TOOL_CONFIG_FLAGS 括号配对失败。');
  const flagsEnd = src.indexOf('\n', flagsClose) + 1;

  // ── 锚点 3：server.tool 包装块结束 ─────────────────────────────
  // 找 "{ const registerTool = server.registerTool.bind(server);" 那个块
  const wrapAnchor = 'const registerTool = server.registerTool.bind(server);';
  const wrapIdx = src.indexOf(wrapAnchor);
  if (wrapIdx < 0) {
    die(`找不到 server.tool 包装块：\n  ${wrapAnchor}\n请手工接线。`);
  }
  // 该块的开头是一个独立的 "{"（单独占一行），在 wrapAnchor 之前往上找第一行只有 "{"
  const lines = src.split('\n');
  const wrapLine = src.slice(0, wrapIdx).split('\n').length - 1; // 0-based
  let openLine = -1;
  for (let i = wrapLine; i >= 0; i -= 1) {
    if (lines[i].trim() === '{') { openLine = i; break; }
    // 遇到上一个语句的收尾就停，避免跨到别的块里去
    if (/^}\s*;?\s*$/.test(lines[i].trim()) && i < wrapLine) break;
  }
  if (openLine < 0) die('找不到包装块起始大括号（应该是一行单独的 "{"）。');
  const blockOpen = lines.slice(0, openLine).reduce((n, l) => n + l.length + 1, 0);
  const blockClose = braceEnd(src, blockOpen);
  if (blockClose < 0) die('包装块括号配对失败。');
  const blockEnd = src.indexOf('\n', blockClose) + 1;

  if (dryRun) {
    const lineOf = (idx) => src.slice(0, idx).split('\n').length;
    console.log('── dry-run：会做这些改动 ──');
    console.log(`1. import 区插入：${IMPORT_LINE}`);
    console.log(`2. 第 ${lineOf(flagsEnd)} 行（TOOL_CONFIG_FLAGS 之后）合并 SING_TOOL_FLAGS`);
    console.log(`3. 第 ${lineOf(blockEnd)} 行（server.tool 包装块之后）注册四个工具`);
    console.log('\n（没有写文件。去掉 --dry-run 才会真改。）');
    return;
  }

  // ── 组装（从后往前插，避免下标失效）────────────────────────────
  let out = src;
  out = out.slice(0, blockEnd) + REGISTER_BLOCK + out.slice(blockEnd);
  out = out.slice(0, flagsEnd) + FLAGS_BLOCK + out.slice(flagsEnd);
  out = out.replace(importAnchor, `${importAnchor}\n${IMPORT_LINE}`);

  // ── 备份 + 写入 + 语法检查 ─────────────────────────────────────
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const bak = `${file}.bak-wire-${stamp}`;
  fs.copyFileSync(file, bak);
  fs.writeFileSync(file, out, 'utf8');
  console.log(`✓ 已备份 ${path.basename(bak)}`);
  console.log(`✓ 已接线 ${file}`);

  const chk = spawnSync(process.execPath, ['--check', file], { encoding: 'utf8' });
  if (chk.status !== 0) {
    console.error('\n✗ 语法检查失败，已还原：');
    console.error(chk.stderr.split('\n').slice(0, 12).join('\n'));
    fs.copyFileSync(bak, file);
    process.exit(1);
  }
  console.log('✓ node --check 通过');

  console.log(`
下一步：
  1. 确认 bridge/src 里有 mcp-sing-tools.js
  2. node bridge/src/selftest-mcp.mjs       期望 16 项通过
  3. 重启 DSH（工具清单在启动时读取）
  4. 在 QQ 里说「唱首 XX」
`);
}

main();
