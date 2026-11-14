// 验证 sing-wiring.js 的 deps 清单是否与**真实 bridge.js** 完全对齐。
//
// 为什么需要这个：文档里列的 deps 名字如果和 bridge.js 实际有的闭包对不上，
// 别人照抄会得到一个 undefined，然后在运行时才炸。这里直接从 bridge.js 源码里
// 抓出它实际定义了哪些名字，和我们的清单做差集。
//
// 用法：node scripts/check-deps.mjs [path/to/bridge.js]
//   不给路径时默认找 ../qq-bridge/src/bridge.js（同级目录的常见布局）

import fs from 'node:fs';
import path from 'node:path';

const guess = process.argv[2] || path.resolve('..', 'qq-bridge', 'src', 'bridge.js');
if (!fs.existsSync(guess)) {
  console.log(`跳过：找不到 bridge.js（${guess}）`);
  console.log('这是可选检查，只在你想验证接线兼容性时才有用。');
  process.exit(0);
}

const src = fs.readFileSync(guess, 'utf8');
console.log(`对照文件：${guess}`);
console.log(`行数：${src.split('\n').length}\n`);

// 我们要求的 deps（与 relay-endpoints.js / sing-wiring.js 保持一致）
const required = [
  'getConfig', 'sendJson', 'readBody', 'agentTokenOk', 'v2ToolEnabled', 'v2SessionAllowed',
  'modeAllowed', 'canonicalV2Key', 'isSessionAllowedInCurrentMode', 'shouldBlockSilentReply',
  'getCurrentMode', 'isPaused', 'coverJobs', 'relaySongScope', 'submitTrackedSong',
  'imageSendRoots', 'isInsideAllowedImageDir', 'imageSendMaxPerHour', 'sendImageBufferToKey',
  'sendMessagesV2', 'sendStickerV2', 'computeGapsV2', 'findMessageMedia',
  'resolveReplyTargetV2', 'getSocialV2State', 'saveSocialV2State', 'recordSentMessagesV2',
  'scheduleReplyCheckV2', 'safeFetchBuffer', 'sendToQQ', 'lastSendSucceeded',
  'verifyVoiceMessageSent', 'MAX_MEDIA_BYTES', 'SENSITIVE_RE', 'KNOWN_AGENT_TOKENS',
  'appendActivity', 'fetchAction', 'path', 'fs', 'stateDir', 'log'
];

// 判断一个名字在 bridge.js 里是否有定义（函数声明 / const / let / import）
function defined(name) {
  const esc = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const patterns = [
    new RegExp(`\\bfunction\\s+${esc}\\s*\\(`),
    new RegExp(`\\basync\\s+function\\s+${esc}\\s*\\(`),
    new RegExp(`\\b(?:const|let|var)\\s+${esc}\\s*[=,]`),
    new RegExp(`\\{[^}]*\\b${esc}\\b[^}]*\\}\\s*=\\s*`),   // 解构赋值
    new RegExp(`^import\\s+${esc}\\s+from`, 'm'),           // import x from '...'
    new RegExp(`^import\\s+\\{[^}]*\\b${esc}\\b[^}]*\\}`, 'm'), // import { x } from '...'
    new RegExp(`\\b${esc}\\s*\\(`)                          // 形如 onebotHttpAction(...) 的本地函数
  ];
  return patterns.some((re) => re.test(src));
}

/**
 * 有些 deps 在 bridge.js 里**不是这个名字**，但一行适配表达式就能给出来。
 * 这不是缺陷 —— deps 是我们自定义的接口，本来就是「用你手上的变量包一下」。
 */
const ADAPTERS = {
  getConfig: '() => cfg',
  getCurrentMode: '() => currentMode',
  isPaused: '() => socialV2.paused === true',
  fetchAction: 'onebotHttpAction',
  stateDir: "path.join(ROOT, 'state')"
};

/**
 * 只影响某一种内容形态的 deps。缺了不阻止启动，那条路会被明确关掉并返回 501，
 * 其余功能照常 —— 上游 qq-bridge 就没有生图目录白名单那套东西。
 */
const OPTIONAL = new Set([
  'imageSendRoots', 'isInsideAllowedImageDir', 'imageSendMaxPerHour', 'sendImageBufferToKey'
]);

/**
 * 这三个**不由 bridge.js 提供**，而是 createSingWiring() 自己造出来再传给路由的。
 * 所以 bridge.js 里没有它们是正常的 —— 接线时你只要调 createSingWiring，
 * 拿它返回的 submitSong / statusSong 用就行。
 */
const PROVIDED_BY_WIRING = new Set(['coverJobs', 'relaySongScope', 'submitTrackedSong']);

const missingAll = required.filter((n) => !defined(n) && !(n in ADAPTERS));
const missing = missingAll.filter((n) => !OPTIONAL.has(n) && !PROVIDED_BY_WIRING.has(n));
const missingOptional = missingAll.filter((n) => OPTIONAL.has(n));
const fromWiring = missingAll.filter((n) => PROVIDED_BY_WIRING.has(n));
const needsAdapter = required.filter((n) => !defined(n) && n in ADAPTERS);
const present = required.filter((n) => defined(n));

console.log(`deps 清单：${required.length} 项`);
console.log(`  bridge.js 里现成的：${present.length} 项`);
console.log(`  需要一行适配的：${needsAdapter.length} 项`);
console.log(`  createSingWiring 替你造的：${fromWiring.length} 项`);
console.log(`  可选（缺了只关掉代发图片）：${missingOptional.length} 项`);
console.log(`  真正缺失（会阻止启动）：${missing.length} 项`);

if (needsAdapter.length) {
  console.log('\n  需要适配（照抄右边的表达式即可，不是缺陷）：');
  for (const n of needsAdapter) console.log(`    ${n.padEnd(22)} ${ADAPTERS[n]}`);
}
if (fromWiring.length) {
  console.log(`\n  createSingWiring 替你造：${fromWiring.join(', ')}`);
  console.log('  → 不用在 bridge.js 里自己写，调 createSingWiring 就拿到了。');
}
if (missingOptional.length) {
  console.log(`\n  可选缺失：${missingOptional.join(', ')}`);
  console.log('  → 代发本地图片（imagePath）会返回 501；文字/表情/转发图片/代唱都照常。');
}
if (missing.length) {
  console.log(`\n  真正缺失：${missing.join(', ')}`);
  console.log('  这些要么在 bridge.js 里补，要么从 deps 里去掉对应功能。');
}

// 顺带确认：我们的模块不会覆盖 bridge.js 里已有的同名函数
console.log('\n反向检查：我们**新增**的东西（bridge.js 上游没有的）');
const ours = ['createSingWiring', 'createRelayRoutes', 'createCoverJobService',
  'createRelaySongScope', 'createSongIntentService', 'createSongJobNotifier',
  'createVoiceDeliveryReceipts'];
for (const n of ours) {
  console.log(`  ${defined(n) ? '已存在' : '新增  '}  ${n}`);
}

process.exitCode = missing.length ? 1 : 0;
