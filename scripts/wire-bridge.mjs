#!/usr/bin/env node
// 自动接线：把 qq-ai-sing 的点歌/代唱接到你的 qq-bridge 上。
//
// 为什么要有这个脚本：手工改 bridge.js 有 3 个位置，其中「装配」和「挂路由」
// 必须放在 http.createServer 回调内部、readBody 之后 —— 放错位置会
// `ReferenceError: sendJson is not defined` 或 `Unexpected reserved word`，
// 而且报错信息完全指不到真正原因。这个脚本按括号配对找位置，改完还会自检。
//
// 用法：
//   node scripts/wire-bridge.mjs <path/to/qq-bridge>
//   node scripts/wire-bridge.mjs <path/to/qq-bridge> --dry-run   只看会改什么
//   node scripts/wire-bridge.mjs <path/to/qq-bridge> --revert    还原
//
// 幂等：已经接过线会直接跳过。改动前会备份成 bridge.js.bak-wire-<时间戳>。

import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

const IMPORT_LINE = "import { createSingWiring } from './sing-wiring.js';";

const WIRING_BLOCK = `
      // ── 点歌 / 代唱接线（qq-ai-sing）─────────────────────────────
      // 必须放在 createServer 回调内部、sendJson/readBody 之后：
      // 这些 deps 是回调作用域的闭包，放到外层会 ReferenceError。
      const sing = createSingWiring({
        getConfig: () => cfg,
        stateDir: path.join(ROOT, 'state'),
        log, appendActivity, path, fs,
        sendJson, readBody,
        agentTokenOk, v2ToolEnabled, v2SessionAllowed, modeAllowed, canonicalV2Key,
        isSessionAllowedInCurrentMode, shouldBlockSilentReply,
        sendToQQ, lastSendSucceeded, recordSentMessagesV2, verifyVoiceMessageSent,
        sendMessagesV2, sendStickerV2, computeGapsV2, findMessageMedia, resolveReplyTargetV2,
        getSocialV2State, saveSocialV2State, scheduleReplyCheckV2,
        safeFetchBuffer, MAX_MEDIA_BYTES, SENSITIVE_RE, KNOWN_AGENT_TOKENS,
        getCurrentMode: () => currentMode,
        isPaused: () => socialV2.paused === true,
        fetchAction: onebotHttpAction,
        ...(typeof imageSendRoots !== 'undefined' ? {
          sendImageBufferToKey, imageSendRoots, isInsideAllowedImageDir, imageSendMaxPerHour,
        } : {}),
      });
      if (await sing.handle(req, res, url)) return;
      // ────────────────────────────────────────────────────────────
`;

const MARK = '点歌 / 代唱接线（qq-ai-sing）';

function die(msg) {
  console.error(`\n✗ ${msg}\n`);
  process.exit(1);
}

function braceEnd(src, openIndex) {
  // 从 openIndex 处的 "{" 开始做括号配对，返回匹配的 "}" 下标。
  // 跳过字符串与注释，避免被 "}" 误导。
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
      // 字符串内
      if (c === '\\') { i += 2; continue; }
      if (c === state) state = 'code';
    }
    i += 1;
  }
  return -1;
}

function main() {
  const args = process.argv.slice(2);
  const target = args.find((a) => !a.startsWith('--'));
  const dryRun = args.includes('--dry-run');
  const revert = args.includes('--revert');

  if (!target) {
    die('用法：node scripts/wire-bridge.mjs <path/to/qq-bridge> [--dry-run|--revert]');
  }
  const bridgeDir = path.resolve(target);
  const bridgeFile = path.join(bridgeDir, 'src', 'bridge.js');
  if (!fs.existsSync(bridgeFile)) die(`找不到 ${bridgeFile}\n参数应该是 qq-bridge 的根目录（含 src/bridge.js）。`);

  const src = fs.readFileSync(bridgeFile, 'utf8');

  // ── revert ────────────────────────────────────────────────────
  if (revert) {
    const baks = fs.readdirSync(path.dirname(bridgeFile))
      .filter((n) => n.startsWith('bridge.js.bak-wire-'))
      .sort();
    if (!baks.length) die('没有找到 wire 备份，无法还原。');
    const latest = baks[baks.length - 1];
    fs.copyFileSync(path.join(path.dirname(bridgeFile), latest), bridgeFile);
    console.log(`✓ 已还原自 ${latest}`);
    return;
  }

  if (src.includes(MARK)) {
    console.log('✓ 已经接过线，跳过（要重做先 --revert）');
    return;
  }

  // ── 找锚点 ────────────────────────────────────────────────────
  const importAnchor = 'const __dirname = path.dirname(fileURLToPath(import.meta.url));';
  if (!src.includes(importAnchor)) {
    die(`找不到 import 锚点：\n  ${importAnchor}\n你的 bridge.js 可能版本差异较大，请按 docs/BRIDGE_WIRING.md 手工接线。`);
  }

  const srvAnchor = 'const server = http.createServer(async (req, res) => {';
  const srvIdx = src.indexOf(srvAnchor);
  if (srvIdx < 0) {
    die(`找不到 createServer 锚点：\n  ${srvAnchor}\n请按 docs/BRIDGE_WIRING.md 手工接线。`);
  }

  const readBodyAnchor = 'const readBody = () => new Promise((resolve, reject) => {';
  const rbIdx = src.indexOf(readBodyAnchor, srvIdx);
  if (rbIdx < 0) {
    die(`找不到 readBody 锚点（在 createServer 之后）：\n  ${readBodyAnchor}\n请按 docs/BRIDGE_WIRING.md 手工接线。`);
  }

  // readBody 的箭头函数体从第一个 "{" 开始配对
  const bodyOpen = src.indexOf('{', rbIdx);
  const bodyClose = braceEnd(src, bodyOpen);
  if (bodyClose < 0) die('readBody 函数体括号配对失败，bridge.js 可能被改坏了。');

  // 插在 readBody 的 "};" 之后那一行末尾
  const semi = src.indexOf(';', bodyClose);
  if (semi < 0) die('readBody 结尾找不到分号。');
  const insertAt = src.indexOf('\n', semi) + 1;
  if (insertAt <= 0) die('定位插入点失败。');

  // ── 组装 ──────────────────────────────────────────────────────
  let out = src;
  out = out.replace(importAnchor, `${IMPORT_LINE}\n\n${importAnchor}`);
  // 重新定位（因为前面插入了内容，下标变了）
  const rbIdx2 = out.indexOf(readBodyAnchor, out.indexOf(srvAnchor));
  const bodyOpen2 = out.indexOf('{', rbIdx2);
  const bodyClose2 = braceEnd(out, bodyOpen2);
  const semi2 = out.indexOf(';', bodyClose2);
  const insertAt2 = out.indexOf('\n', semi2) + 1;
  out = out.slice(0, insertAt2) + WIRING_BLOCK + out.slice(insertAt2);

  if (dryRun) {
    console.log('── dry-run：会做这些改动 ──');
    console.log(`1. 在 import 区插入：${IMPORT_LINE}`);
    console.log(`2. 在 readBody 之后（第 ${out.slice(0, insertAt2).split('\n').length} 行）插入装配块 + 路由`);
    console.log('\n（没有写文件。去掉 --dry-run 才会真改。）');
    return;
  }

  // ── 备份 + 写入 ───────────────────────────────────────────────
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const bak = `${bridgeFile}.bak-wire-${stamp}`;
  fs.copyFileSync(bridgeFile, bak);
  fs.writeFileSync(bridgeFile, out, 'utf8');
  console.log(`✓ 已备份 ${path.basename(bak)}`);
  console.log(`✓ 已接线 ${bridgeFile}`);

  // ── 语法检查 ──────────────────────────────────────────────────
  const chk = spawnSync(process.execPath, ['--check', bridgeFile], { encoding: 'utf8' });
  if (chk.status !== 0) {
    console.error('\n✗ 语法检查失败，已还原：');
    console.error(chk.stderr.split('\n').slice(0, 12).join('\n'));
    fs.copyFileSync(bak, bridgeFile);
    process.exit(1);
  }
  console.log('✓ node --check 通过');

  console.log(`
下一步：
  1. 确认 bridge/src 里已经有 sing-wiring.js / sing-endpoints.js /
     relay-endpoints.js / mcp-sing-tools.js（本仓库 bridge/src 下那几个）
  2. node scripts/check-deps.mjs ${path.join(bridgeDir, 'src', 'bridge.js')}
     期望：真正缺失（会阻止启动）：0 项
  3. 按 docs/BRIDGE_WIRING.md 3.4 把 MCP 工具注册进 mcp-snowluma-safe.js
  4. 启动 bridge，看日志里有没有 [sing] 相关报错
  5. 在群里发「唱首 XX」试试
`);
}

main();
