// MCP 握手测试：真正 spawn 一个 MCP server，走 initialize → tools/list，
// 检查 qq_sing 等四个工具是不是真的注册上了。
//
// 这是「接完线到底能不能用」的最终判据 —— selftest-mcp.mjs 只能验注册逻辑，
// 这个验的是真实进程里的工具清单。

import { spawn } from 'node:child_process';
import path from 'node:path';

const BRIDGE = path.resolve(process.argv[2] || '.');
const SERVER = path.join(BRIDGE, 'src', 'mcp-snowluma-safe.js');

const child = spawn(process.execPath, [SERVER], {
  cwd: BRIDGE,
  stdio: ['pipe', 'pipe', 'pipe'],
  env: { ...process.env, QQ_BRIDGE_SELF_KEY: '' },
});

let buf = '';
const pending = new Map();
let nextId = 1;

child.stdout.on('data', (d) => {
  buf += d.toString('utf8');
  let idx;
  while ((idx = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, idx).trim();
    buf = buf.slice(idx + 1);
    if (!line) continue;
    let msg;
    try { msg = JSON.parse(line); } catch { continue; }
    const resolver = pending.get(msg.id);
    if (resolver) { pending.delete(msg.id); resolver(msg); }
  }
});

child.stderr.on('data', (d) => {
  process.stderr.write('[mcp stderr] ' + d.toString('utf8'));
});

function send(method, params) {
  const id = nextId++;
  const payload = JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n';
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`timeout: ${method}`)), 15000);
    pending.set(id, (msg) => { clearTimeout(timer); resolve(msg); });
    child.stdin.write(payload);
  });
}

function notify(method, params) {
  child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method, params }) + '\n');
}

const WANTED = ['qq_sing', 'qq_sing_status', 'qq_relay_targets', 'qq_relay_send'];

(async () => {
  try {
    const init = await send('initialize', {
      protocolVersion: '2024-11-05',
      capabilities: {},
      clientInfo: { name: 'qq-ai-sing-verify', version: '0.1.0' },
    });
    if (init.error) throw new Error('initialize 失败：' + JSON.stringify(init.error));
    console.log('✓ initialize 成功：' + (init.result?.serverInfo?.name || '?'));
    notify('notifications/initialized', {});

    const tools = await send('tools/list', {});
    if (tools.error) throw new Error('tools/list 失败：' + JSON.stringify(tools.error));
    const names = (tools.result?.tools || []).map((t) => t.name);
    console.log(`✓ tools/list 返回 ${names.length} 个工具`);

    let bad = 0;
    for (const w of WANTED) {
      if (names.includes(w)) {
        console.log(`  OK    ${w} 已注册`);
      } else {
        console.log(`  FAIL  ${w} 缺失`);
        bad += 1;
      }
    }

    // 顺便确认别的工具没被搞坏
    for (const must of ['qq_send_message', 'qq_get_unread_messages', 'qq_mark_read']) {
      if (!names.includes(must)) { console.log(`  FAIL  ${must} 意外丢失`); bad += 1; }
    }

    console.log(bad === 0 ? '\n== 工具清单验证通过 ==' : `\n== ${bad} 项失败 ==`);
    child.kill();
    process.exit(bad === 0 ? 0 : 1);
  } catch (error) {
    console.error('✗ ' + error.message);
    child.kill();
    process.exit(2);
  }
})();
