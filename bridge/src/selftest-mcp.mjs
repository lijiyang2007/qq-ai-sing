// 验证 mcp-sing-tools.js：四个工具的注册条件、schema 形状、以及请求转发。
//
// 用法：node bridge/src/selftest-mcp.mjs

import assert from 'node:assert';
import { registerSingTools, SING_TOOL_FLAGS } from './mcp-sing-tools.js';

let passed = 0, failed = 0;
function check(name, fn) {
  try { fn(); console.log(`  OK    ${name}`); passed++; }
  catch (e) { console.log(`  FAIL  ${name}\n        ${e?.message ?? e}`); failed++; }
}
async function checkAsync(name, fn) {
  try { await fn(); console.log(`  OK    ${name}`); passed++; }
  catch (e) { console.log(`  FAIL  ${name}\n        ${e?.message ?? e}`); failed++; }
}

// 极简的 zod 替身：只要能被构造、能链式 .optional()/.describe() 就行。
// 这样自检不依赖真的装 zod。
function makeZ() {
  const mk = (type) => {
    const node = { __type: type, __optional: false };
    node.describe = (d) => { node.__desc = d; return node; };
    node.optional = () => { node.__optional = true; return node; };
    node.min = (n) => { node.__min = n; return node; };
    return node;
  };
  return {
    string: () => mk('string'),
    number: () => mk('number'),
    enum: (vals) => { const n = mk('enum'); n.__vals = vals; return n; },
    array: (inner) => { const n = mk('array'); n.__inner = inner; return n; },
    union: (opts) => { const n = mk('union'); n.__opts = opts; return n; }
  };
}

function makeServer() {
  const tools = {};
  return {
    tools,
    tool(name, description, schema, handler) { tools[name] = { description, schema, handler }; }
  };
}

console.log('== MCP 点歌/代发工具自检 ==\n');

console.log('[1] 开关映射');
check('四个工具都登记了 flag', () => {
  assert.deepEqual(SING_TOOL_FLAGS, {
    qq_sing: 'sing', qq_sing_status: 'sing',
    qq_relay_targets: 'relayTargets', qq_relay_send: 'relaySend'
  });
});

console.log('\n[2] 全部开启时注册四个工具');
const z = makeZ();
let calls = [];
const agentApi = async (path, opts) => { calls.push({ path, opts }); return { ok: true, job: 'j1' }; };
const serializeModelData = (d) => JSON.stringify(d);

const s1 = makeServer();
registerSingTools(s1, { z, agentApi, serializeModelData, cfg: { socialV2: {} } });
check('注册了 qq_sing', () => assert.ok(s1.tools.qq_sing));
check('注册了 qq_sing_status', () => assert.ok(s1.tools.qq_sing_status));
check('注册了 qq_relay_targets', () => assert.ok(s1.tools.qq_relay_targets));
check('注册了 qq_relay_send', () => assert.ok(s1.tools.qq_relay_send));
check('每个工具都有非空描述', () => {
  for (const [n, t] of Object.entries(s1.tools)) {
    assert.ok(t.description.length > 50, `${n} 描述太短`);
  }
});
check('qq_sing 的 mode 是 chorus/full 枚举', () => {
  const m = s1.tools.qq_sing.schema.mode;
  assert.deepEqual(m.__vals, ['chorus', 'full']);
  assert.equal(m.__optional, true);
});
check('qq_relay_send 有 target 且必填', () => {
  const t = s1.tools.qq_relay_send.schema.target;
  assert.ok(t, 'no target');
  assert.equal(t.__optional, false);
});

console.log('\n[3] 开关关闭时不注册');
const s2 = makeServer();
registerSingTools(s2, {
  z, agentApi, serializeModelData,
  cfg: { socialV2: { tools: { relayTargets: false, relaySend: false } } }
});
check('relay 两个工具被关掉', () => {
  assert.equal(s2.tools.qq_relay_targets, undefined);
  assert.equal(s2.tools.qq_relay_send, undefined);
});
check('sing 两个工具还在', () => {
  assert.ok(s2.tools.qq_sing);
  assert.ok(s2.tools.qq_sing_status);
});

const s3 = makeServer();
registerSingTools(s3, {
  z, agentApi, serializeModelData,
  cfg: { socialV2: { sing: { enabled: false } } }
});
check('sing.enabled=false 时点歌工具不注册', () => {
  assert.equal(s3.tools.qq_sing, undefined);
  assert.equal(s3.tools.qq_sing_status, undefined);
});

console.log('\n[4] 请求转发');
await checkAsync('qq_sing 打到 /api/socialV2/sing，默认 mode=full', async () => {
  calls = [];
  const r = await s1.tools.qq_sing.handler({ key: 'group:1', token: 't', song: '某歌' });
  assert.equal(calls[0].path, '/api/socialV2/sing');
  const body = JSON.parse(calls[0].opts.body);
  assert.equal(body.mode, 'full');
  assert.equal(body.song, '某歌');
  assert.equal(calls[0].opts.headers['x-agent-token'], 't');
  assert.ok(r.content[0].text.includes('ok'));
});

await checkAsync('qq_sing_status 打到 /api/socialV2/sing-status', async () => {
  calls = [];
  await s1.tools.qq_sing_status.handler({ key: 'group:1', token: 't', job: 'j1' });
  assert.equal(calls[0].path, '/api/socialV2/sing-status');
});

await checkAsync('qq_relay_targets 用 GET 查询参数', async () => {
  calls = [];
  await s1.tools.qq_relay_targets.handler({ key: 'private:1', token: 't' });
  assert.ok(calls[0].path.startsWith('/api/socialV2/relay-targets?'));
  assert.ok(calls[0].path.includes('key=private%3A1') || calls[0].path.includes('key=private:1'));
});

await checkAsync('qq_relay_send 只把非空字段放进 payload', async () => {
  calls = [];
  await s1.tools.qq_relay_send.handler({
    key: 'private:1', token: 't', target: 'private:2',
    song: '某歌', mode: 'chorus',
    messages: undefined, stickerId: '', forwardFromMessageId: null
  });
  const body = JSON.parse(calls[0].opts.body);
  assert.equal(body.song, '某歌');
  assert.equal(body.mode, 'chorus');
  assert.equal(body.target, 'private:2');
  assert.ok(!('messages' in body), 'messages 不该出现');
  assert.ok(!('stickerId' in body), 'stickerId 不该出现');
  assert.ok(!('forwardFromMessageId' in body), 'forwardFromMessageId 不该出现');
});

await checkAsync('出错时返回 isError', async () => {
  const s4 = makeServer();
  registerSingTools(s4, {
    z, serializeModelData, cfg: { socialV2: {} },
    agentApi: async () => { throw new Error('boom'); }
  });
  const r = await s4.tools.qq_sing.handler({ key: 'g:1', token: 't', song: 'x' });
  assert.equal(r.isError, true);
  assert.ok(r.content[0].text.includes('boom'));
});

console.log(`\n== ${passed} 项通过 / ${failed} 项失败 ==`);
if (failed) process.exitCode = 1;
