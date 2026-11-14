// 接线自检：不用真的起 QQ，就能验证 7 个模块 + 两条代发路由能不能装配起来、
// 以及最关键的闸门有没有生效。
//
// 用法（仓库根目录）：
//     node bridge/src/selftest.mjs
//
// 它做的事：
//   ① 逐个 import 模块，确认没有循环依赖/语法错误
//   ② 用假的 deps 装配 createSingWiring，确认定时器能创建、能清掉
//   ③ 打两条 relay 路由，确认「非 owner 会话」被拒、「缺令牌」被拒、
//      「非白名单目标混发文字」被拒 —— 这三条是安全边界，错了就是提权
//
// 注意：这不是单元测试，是**接线自检**。它证明的是「代码接得上、闸门关得住」，
// 不证明「真的能唱歌」—— 后者要真跑一次完整流水线。

import assert from 'node:assert';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const here = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'));
const load = (name) => import(new URL(`./${name}`, import.meta.url).href);

let passed = 0;
let failed = 0;
function check(name, fn) {
  try { fn(); console.log(`  OK    ${name}`); passed += 1; }
  catch (e) { console.log(`  FAIL  ${name}\n        ${e?.message ?? e}`); failed += 1; }
}
async function checkAsync(name, fn) {
  try { await fn(); console.log(`  OK    ${name}`); passed += 1; }
  catch (e) { console.log(`  FAIL  ${name}\n        ${e?.message ?? e}`); failed += 1; }
}

console.log('== qq-ai-sing 桥接接线自检 ==\n');

// ── ① 模块能否 import ──────────────────────────────────────────────────
console.log('[1] 模块加载');
const mods = {};
for (const name of [
  'bilibili-song.js', 'pro-song.js', 'song-intent.js', 'cover-job.js',
  'relay-song-scope.js', 'song-job-notifier.js', 'voice-delivery-receipts.js',
  'relay-endpoints.js', 'sing-wiring.js'
]) {
  await checkAsync(name, async () => { mods[name] = await load(name); });
}

const { createSingWiring } = mods['sing-wiring.js'] ?? {};
const { createRelayRoutes } = mods['relay-endpoints.js'] ?? {};

// ── ② 装配 ─────────────────────────────────────────────────────────────
console.log('\n[2] 装配 createSingWiring');
const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), 'sing-selftest-'));

function makeDeps(overrides = {}) {
  const cfg = {
    ownerQQ: 10001,
    allow: { private: ['10001', '20002'], groups: ['30003'] },
    deny: {},
    socialV2: { sing: { enabled: true, root: stateDir, jobsDir: path.join(stateDir, 'jobs') }, tools: {} },
    ...overrides.cfg
  };
  const sent = [];
  const scope = overrides.deps?.relaySongScope ?? {
    list: async () => ({ private: [], groups: [] }),
    assertMember: async () => true,
    issue: async () => 'fake.grant',
    bind: () => {},
    authorize: async () => true
  };
  const deps = {
    getConfig: () => cfg,
    stateDir,
    path, fs,
    log: () => {},
    appendActivity: () => {},
    // 用假的 sendJson/readBody 捕获响应，不真的走 HTTP
    sendJson: (obj, code) => { deps.__res = { obj, code }; },
    readBody: async () => deps.__body ?? {},
    // 二代会话令牌：每个会话各有自己的令牌，测试里统一用 'good-token'
    agentTokenOk: (key, token) => token === 'good-token' && /^(group|private):\d+$/.test(String(key ?? '')),
    v2ToolEnabled: () => true,
    v2SessionAllowed: () => true,
    modeAllowed: (target) => ['private:10001', 'private:20002', 'group:30003'].includes(target),
    canonicalV2Key: (k) => (/^(group|private):[1-9]\d+$/.test(String(k ?? '')) ? String(k) : null),
    isSessionAllowedInCurrentMode: () => true,
    shouldBlockSilentReply: () => false,
    getCurrentMode: () => 'reserved2',
    isPaused: () => false,
    // 下面三个是「点歌」侧的依赖，代发纯文字时用不到，但装配期必须齐全
    coverJobs: { resolveSong: async () => null, submit: async () => ({}), status: () => ({}) },
    relaySongScope: scope,
    submitTrackedSong: async (args) => ({ ok: true, job: 'fake-job-1', song: args.song, mode: args.mode }),
    sendToQQ: async (key, msg) => { sent.push({ key, msg }); },
    lastSendSucceeded: () => true,
    recordSentMessagesV2: () => {},
    verifyVoiceMessageSent: async () => ({ confirmed: false }),
    sendMessagesV2: async (target, messages) => messages.map((m) => m),
    sendStickerV2: async () => {},
    computeGapsV2: () => [],
    findMessageMedia: () => [],
    resolveReplyTargetV2: async () => null,
    getSocialV2State: () => ({ sendTimes: [], wakeConfig: {} }),
    saveSocialV2State: () => {},
    scheduleReplyCheckV2: () => {},
    sendImageBufferToKey: async () => {},
    imageSendRoots: () => [stateDir],
    isInsideAllowedImageDir: () => false,
    imageSendMaxPerHour: () => 10,
    safeFetchBuffer: async () => ({ buffer: Buffer.alloc(0) }),
    MAX_MEDIA_BYTES: 8 * 1024 * 1024,
    SENSITIVE_RE: /token|password/i,
    KNOWN_AGENT_TOKENS: new Set(['good-token']),
    fetchAction: async () => ({ status: 'ok', retcode: 0, data: [] }),
    ...overrides.deps
  };
  deps.__sent = sent;
  return deps;
}

let wiring = null;
await checkAsync('createSingWiring 不抛', async () => {
  const deps = makeDeps();
  wiring = createSingWiring(deps);
  assert.ok(wiring, 'no wiring');
  assert.equal(typeof wiring.handle, 'function');
  assert.equal(typeof wiring.submitSong, 'function');
  assert.equal(typeof wiring.statusSong, 'function');
  assert.equal(typeof wiring.relayGuidance, 'function');
  wiring.close();
});

// ── ③ 代发路由闸门 ─────────────────────────────────────────────────────
console.log('\n[3] 代发路由闸门（安全边界）');

function callSend(deps, body, headers = { 'x-agent-token': 'good-token' }) {
  deps.__body = body;
  const req = { method: 'POST', headers };
  const res = {};
  const url = { pathname: '/api/socialV2/relay-send', searchParams: new URLSearchParams() };
  return { req, res, url };
}

await checkAsync('群友会话（非 owner）→ 403', async () => {
  const deps = makeDeps();
  const routes = createRelayRoutes(deps);
  const { req, res, url } = callSend(deps, {
    key: 'private:20002', token: 'good-token', target: 'private:20002', messages: 'hi'
  });
  await routes.handle(req, res, url);
  assert.equal(deps.__res.code, 403, `期望 403，实际 ${deps.__res.code}`);
  assert.match(deps.__res.obj.error, /管理员私聊/, deps.__res.obj.error);
});

await checkAsync('owner 但令牌错 → 403', async () => {
  const deps = makeDeps();
  const routes = createRelayRoutes(deps);
  const { req, res, url } = callSend(deps, {
    key: 'private:10001', token: 'wrong', target: 'private:20002', messages: 'hi'
  });
  await routes.handle(req, res, url);
  assert.equal(deps.__res.code, 403);
  assert.match(deps.__res.obj.error, /token/, deps.__res.obj.error);
});

await checkAsync('非白名单目标混发文字 → 403（只允许纯代唱）', async () => {
  const deps = makeDeps();
  const routes = createRelayRoutes(deps);
  const { req, res, url } = callSend(deps, {
    key: 'private:10001', token: 'good-token', target: 'private:99999', messages: 'hi', song: '某歌'
  });
  await routes.handle(req, res, url);
  assert.equal(deps.__res.code, 403, `期望 403，实际 ${deps.__res.code}`);
  assert.match(deps.__res.obj.error, /非聊天白名单/, deps.__res.obj.error);
});

await checkAsync('目标格式非法 → 400', async () => {
  const deps = makeDeps();
  const routes = createRelayRoutes(deps);
  const { req, res, url } = callSend(deps, {
    key: 'private:10001', token: 'good-token', target: 'nonsense', messages: 'hi'
  });
  await routes.handle(req, res, url);
  assert.equal(deps.__res.code, 400);
});

await checkAsync('代发给自己 → 400', async () => {
  const deps = makeDeps();
  const routes = createRelayRoutes(deps);
  const { req, res, url } = callSend(deps, {
    key: 'private:10001', token: 'good-token', target: 'private:10001', messages: 'hi'
  });
  await routes.handle(req, res, url);
  assert.equal(deps.__res.code, 400);
  assert.match(deps.__res.obj.error, /自己/, deps.__res.obj.error);
});

await checkAsync('什么内容都没给 → 400', async () => {
  const deps = makeDeps();
  const routes = createRelayRoutes(deps);
  const { req, res, url } = callSend(deps, {
    key: 'private:10001', token: 'good-token', target: 'private:20002'
  });
  await routes.handle(req, res, url);
  assert.equal(deps.__res.code, 400);
});

await checkAsync('白名单目标发文字 → 真的发出去了', async () => {
  const deps = makeDeps();
  const routes = createRelayRoutes(deps);
  const { req, res, url } = callSend(deps, {
    key: 'private:10001', token: 'good-token', target: 'private:20002', messages: ['在吗']
  });
  await routes.handle(req, res, url);
  assert.equal(deps.__res.code, undefined, `不该有错误码：${JSON.stringify(deps.__res.obj)}`);
  assert.equal(deps.__res.obj.ok, true, JSON.stringify(deps.__res.obj));
  assert.equal(deps.__res.obj.sent, 1);
});

// ── ④ 目标清单路由 ─────────────────────────────────────────────────────
console.log('\n[4] 代发目标清单闸门');

// relaySongScope.list() 正常要问 OneBot 拿好友/群名单。这里给一个假的，
// 顺便验证「清单返回后 canRelaySong/canRelayMessages 是按白名单算的」。
const fakeScope = {
  list: async () => ({
    private: [
      { target: 'private:20002', qq: '20002', name: '好友甲', nickname: '甲', remark: '' },
      { target: 'private:99999', qq: '99999', name: '好友乙', nickname: '乙', remark: '' }
    ],
    groups: [{ target: 'group:30003', group: '30003', name: '测试群' }]
  }),
  assertMember: async () => true,
  issue: async () => 'fake.grant',
  bind: () => {},
  authorize: async () => true
};

function callTargets(deps, key, token) {
  const req = { method: 'GET', headers: token ? { 'x-agent-token': token } : {} };
  const url = { pathname: '/api/socialV2/relay-targets', searchParams: new URLSearchParams({ key }) };
  return { req, res: {}, url };
}

await checkAsync('群友会话查清单 → 403', async () => {
  const deps = makeDeps({ deps: { relaySongScope: fakeScope } });
  const routes = createRelayRoutes(deps);
  const { req, res, url } = callTargets(deps, 'private:20002', 'good-token');
  await routes.handle(req, res, url);
  assert.equal(deps.__res.code, 403);
});

await checkAsync('owner 查清单 → 200，且 canRelayMessages 按白名单区分', async () => {
  const deps = makeDeps({ deps: { relaySongScope: fakeScope } });
  const routes = createRelayRoutes(deps);
  const { req, res, url } = callTargets(deps, 'private:10001', 'good-token');
  await routes.handle(req, res, url);
  assert.equal(deps.__res.obj.ok, true, JSON.stringify(deps.__res.obj));
  const byTarget = Object.fromEntries(
    [...deps.__res.obj.private, ...deps.__res.obj.groups].map((x) => [x.target, x]));
  assert.equal(byTarget['private:20002'].canRelaySong, true);
  assert.equal(byTarget['private:20002'].canRelayMessages, true, '白名单好友应可代发文字');
  assert.equal(byTarget['private:99999'].canRelaySong, true, '任何真实好友都能代唱');
  assert.equal(byTarget['private:99999'].canRelayMessages, false, '非白名单只允许代唱');
  assert.equal(byTarget['group:30003'].canRelayMessages, true);
});

await checkAsync('缺 deps 时装配期就点名报错', async () => {
  const deps = makeDeps();
  delete deps.sendMessagesV2;
  assert.throws(() => createRelayRoutes(deps), /sendMessagesV2/);
});

// ── ⑤ 未注册路径应放行（返回 false）──────────────────────────────────────
await checkAsync('其它路径返回 false（不抢别人的路由）', async () => {
  const deps = makeDeps();
  const routes = createRelayRoutes(deps);
  const url = { pathname: '/api/socialV2/send-message', searchParams: new URLSearchParams() };
  const handled = await routes.handle({ method: 'POST', headers: {} }, {}, url);
  assert.equal(handled, false);
});

// ── ⑥ 点歌路由 ─────────────────────────────────────────────────────────
console.log('\n[5] 点歌路由（/api/socialV2/sing）');

const { createSingRoutes } = await load('sing-endpoints.js');

function callSing(deps, body, headers = { 'x-agent-token': 'good-token' }, pathname = '/api/socialV2/sing') {
  deps.__body = body;
  return {
    req: { method: 'POST', headers }, res: {},
    url: { pathname, searchParams: new URLSearchParams() }
  };
}

function makeSingDeps(overrides = {}) {
  const deps = makeDeps(overrides);
  deps.songIntents = {
    get: () => null, autoSelect: async () => ({ status: 'submitted', job: 'j1' }),
    provideArtist: async () => ({ status: 'submitted', job: 'j2' }),
    replace: async () => ({ status: 'submitted', job: 'j3' }),
    cancel: () => ({ status: 'cancelled' }),
    ...overrides.songIntents
  };
  deps.relayGuidance = overrides.relayGuidance ?? (() => []);
  deps.parseBilibiliSongRequest = () => null;
  deps.parseProSongRequest = () => null;
  return deps;
}

await checkAsync('非 reserved2 模式 → 403', async () => {
  const deps = makeSingDeps({ deps: { getCurrentMode: () => 'chat' } });
  const routes = createSingRoutes(deps);
  const { req, res, url } = callSing(deps, { key: 'private:20002', token: 'good-token', song: 'x' });
  await routes.handle(req, res, url);
  assert.equal(deps.__res.code, 403);
});

await checkAsync('缺令牌 → 403', async () => {
  const deps = makeSingDeps();
  const routes = createSingRoutes(deps);
  const { req, res, url } = callSing(deps, { key: 'private:20002', song: 'x' }, {});
  await routes.handle(req, res, url);
  assert.equal(deps.__res.code, 403);
});

await checkAsync('令牌不匹配 → 403', async () => {
  const deps = makeSingDeps();
  const routes = createSingRoutes(deps);
  const { req, res, url } = callSing(deps, { key: 'private:20002', token: 'bad', song: 'x' });
  await routes.handle(req, res, url);
  assert.equal(deps.__res.code, 403);
});

await checkAsync('不在白名单的会话 → 403', async () => {
  const deps = makeSingDeps();
  deps.modeAllowed = () => false;
  const routes = createSingRoutes(deps);
  const { req, res, url } = callSing(deps, { key: 'private:99999', token: 'good-token', song: 'x' });
  await routes.handle(req, res, url);
  assert.equal(deps.__res.code, 403);
});

await checkAsync('正常提交 → 走到 submitTrackedSong', async () => {
  let got = null;
  const deps = makeSingDeps({
    deps: { submitTrackedSong: async (a) => { got = a; return { ok: true, job: 'j-real', song: a.song }; } }
  });
  const routes = createSingRoutes(deps);
  const { req, res, url } = callSing(deps, { key: 'private:20002', token: 'good-token', song: '某歌', mode: 'chorus' });
  await routes.handle(req, res, url);
  assert.ok(got, 'submitTrackedSong 没被调用');
  assert.equal(got.target, 'private:20002');
  assert.equal(got.mode, 'chorus');
  assert.equal(deps.__res.obj.ok, true, JSON.stringify(deps.__res.obj));
});

await checkAsync('代唱请求误用 qq_sing → 409 + 指引用 qq_relay_send', async () => {
  const deps = makeSingDeps({ relayGuidance: () => [{ instruction: '这是管理员要求唱给别人' }] });
  const routes = createSingRoutes(deps);
  const { req, res, url } = callSing(deps, { key: 'private:20002', token: 'good-token', song: '某歌' });
  await routes.handle(req, res, url);
  assert.equal(deps.__res.code, 409, `期望 409，实际 ${deps.__res.code}`);
  assert.equal(deps.__res.obj.code, 'USE_RELAY_FOR_RECIPIENTS');
  assert.match(deps.__res.obj.error, /qq_relay_send/);
});

await checkAsync('查状态：别的会话的任务 → 透传 403', async () => {
  const deps = makeSingDeps({
    deps: {
      coverJobs: {
        resolveSong: async () => null,
        submit: async () => ({}),
        status: () => { throw Object.assign(new Error('这个任务不是这个会话点的，看不了'), { statusCode: 403 }); }
      }
    }
  });
  const routes = createSingRoutes(deps);
  const { req, res, url } = callSing(deps, { key: 'private:20002', token: 'good-token', job: 'j1' },
    { 'x-agent-token': 'good-token' }, '/api/socialV2/sing-status');
  await routes.handle(req, res, url);
  assert.equal(deps.__res.code, 403);
  assert.match(deps.__res.obj.error, /不是这个会话/);
});

await checkAsync('查状态：正常 → ok', async () => {
  const deps = makeSingDeps({
    deps: {
      coverJobs: {
        resolveSong: async () => null, submit: async () => ({}),
        status: ({ jobId, target }) => ({ job: jobId, target, sent: false, status: 'running' })
      }
    }
  });
  const routes = createSingRoutes(deps);
  const { req, res, url } = callSing(deps, { key: 'private:20002', token: 'good-token', job: 'j1' },
    { 'x-agent-token': 'good-token' }, '/api/socialV2/sing-status');
  await routes.handle(req, res, url);
  assert.equal(deps.__res.obj.ok, true, JSON.stringify(deps.__res.obj));
  assert.equal(deps.__res.obj.job, 'j1');
});

fs.rmSync(stateDir, { recursive: true, force: true });

console.log(`\n== ${passed} 项通过 / ${failed} 项失败 ==`);
if (failed) {
  console.log('\n失败项说明接线有问题，别急着跑真任务。');
  process.exitCode = 1;
} else {
  console.log('接线自检通过。下一步：按 docs/BRIDGE_WIRING.md 把 deps 接到你的 bridge.js。');
}
