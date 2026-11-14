# 接进 QQ 桥接

这一份说明怎么让「在群里说一句"唱首 XX"」真的跑起来。

前置：`docs/INSTALL.md` 已经跑通，`python scripts/doctor.py` 全绿。

---

## 零、先说清楚一件事

**上游 [qq-bridge](https://github.com/Derpyu520/qq-bridge) 没有任何唱歌/代唱代码。**
它是个成熟的 QQ↔AI 消息桥（发消息、发表情、语音、空间动态都有），但它不知道
"点歌"是什么，也没有 `/api/socialV2/sing`。

所以「照仓库做能不能唱歌」的答案是：

| 部分 | 要不要自己动手 |
|---|---|
| 唱歌流水线（取源 → 分离 → 换声 → 混音 → 发送） | **不用**。本仓库全给了，`pipeline/` 可以直接跑 |
| 模型训练 | 必须（仓库不带权重，见 TRAIN_VOICE.md） |
| 桥接侧的接线 | **要动手，但已经封装成 4 步**（见第三节） |

第三节就是全部工作量。做完之后：

- 群里说「唱首 XX」→ AI 用 `qq_sing` 提交 → 做好发回该群 ✅
- 管理员私聊说「唱首 XX 发给某人」→ AI 用 `qq_relay_send` → 做好发到对方那个会话 ✅

---

## 一、这些文件是干什么的

`bridge/src/` 里的模块，分三层：

### 1.1 核心模块（上游没有，本仓库提供）

| 文件 | 职责 |
|---|---|
| `cover-job.js` | **提交任务**。校验歌名/配额/去重，spawn `cover_job.py submit`，把结果投影成模型能看的字段 |
| `sing-wiring.js` | **装配器**。把下面所有东西一次性接好，你只调它一个 |
| `sing-endpoints.js` | `/api/socialV2/sing` + `sing-status` 两条路由 |
| `relay-endpoints.js` | `/api/socialV2/relay-targets` + `relay-send` 两条路由（代发/代唱） |
| `relay-song-scope.js` | 代唱授权范围（HMAC 授权、单任务绑定、24h 过期） |
| `song-intent.js` | 从聊天文本判断「这是不是一次点歌」，含等歌手/补充/更换/取消 |
| `pro-song.js` | `pro唱XX` 这类显式版本许可 |
| `bilibili-song.js` | B 站链接点歌（有损来源，会明确标注） |
| `song-job-notifier.js` | 跑完主动通知原会话 |
| `voice-delivery-receipts.js` | 发送回执/journal，防重复发 |
| `mcp-sing-tools.js` | 注册 `qq_sing` / `qq_sing_status` / `qq_relay_targets` / `qq_relay_send` 四个 MCP 工具 |
| `selftest.mjs` | 接线自检（29 项） |
| `selftest-mcp.mjs` | MCP 工具自检（16 项） |

### 1.2 设计前提

QQ 会话的工具面被锁死了（没有 `pwsh` / `read` / 文件系统）。
所以这些模块只做一件事——**提交一首歌**。参数收窄到只有歌名和模式，
目标会话由桥接按会话令牌强制绑定，模型无法指定别的会话。

---

## 二、复制文件

```powershell
$Repo   = "C:\path\to\qq-ai-sing"
$Bridge = "C:\path\to\qq-bridge"

Copy-Item "$Repo\bridge\src\*.js"   "$Bridge\src\" -Force
Copy-Item "$Repo\bridge\src\*.mjs"  "$Bridge\src\" -Force
```

**流水线不用复制。** 桥接是 spawn 一个 Python 脚本，直接指向仓库里的那个就行：

```jsonc
// qq-bridge/config.json → socialV2.sing
"script": "C:/path/to/qq-ai-sing/pipeline/cover_job.py"
```

> 早期版本这里写的是「把 cover_job.py 复制到 sing.root」。**不要那样做** ——
> 它旁边没有 `hq_cover/` 包，会直接起不来。要么指向仓库里的路径（推荐），
> 要么把整个 `pipeline/` 目录复制过去。`cover_job.py` 现在会主动探这两个位置，
> 找不到就给出明确提示，不会只丢一个 `ModuleNotFoundError`。

---

## 三、接线（一条命令）

```powershell
node scripts/wire-all.mjs C:\path\to\qq-bridge --dry-run   # 先看会改什么
node scripts/wire-all.mjs C:\path\to\qq-bridge             # 真改
```

它做四件事，每步都失败即停：

| 步骤 | 做什么 |
|---|---|
| 1. 复制模块 | 11 个 `.js` 进 `qq-bridge/src/` |
| 2. 接线 bridge.js | import + 装配 + 挂路由，位置用括号配对找 |
| 3. 接线 MCP 工具 | `TOOL_CONFIG_FLAGS` 合并 + `registerSingTools` |
| 4. 验证 | deps 检查 + **真实 MCP 握手**列出工具清单 |

跑完你会看到：

```
✓ deps 检查：真正缺失 0 项
✓ initialize 成功：snowluma-safe
✓ tools/list 返回 39 个工具
  OK    qq_sing 已注册
  OK    qq_sing_status 已注册
  OK    qq_relay_targets 已注册
  OK    qq_relay_send 已注册
```

> **第 4 步是真验证，不是自检**：它真的 spawn 一个 MCP 进程、走
> `initialize` → `tools/list`，看工具清单里到底有没有这四个。上游原版是 38 个，
> 接完是 39+（取决于配置开关）。少一个就说明没接上。

改坏了要还原：

```powershell
node scripts/wire-all.mjs C:\path\to\qq-bridge --revert
```

### 单独跑某一步

```powershell
node scripts/wire-bridge.mjs C:\path\to\qq-bridge    # 只接 bridge.js
node scripts/wire-mcp.mjs    C:\path\to\qq-bridge    # 只接 MCP 工具
node scripts/check-deps.mjs  C:\path\to\qq-bridge\src\bridge.js
node scripts/verify-mcp-tools.mjs C:\path\to\qq-bridge
```

每个都支持 `--dry-run` 和 `--revert`。

---

## 三之二、手工接线（脚本失败时看这里）

> `bridge.js` 每个版本都不一样，所以这里不给整份文件，也不给行号。
> 找**锚点**（下面每步都写了），插在锚点旁边。
>
> ⚠️ **位置很关键**：3.2 和 3.3 必须放在 `http.createServer(async (req, res) => { ... })`
> 的**回调内部**，而且要在 `sendJson` / `readBody` 定义**之后**。
> 这几个 deps 是回调作用域的闭包，放到外层（比如 `v2SessionAllowed` 那一层）会
> 直接 `ReferenceError: sendJson is not defined`，启动就挂。
> 这不是推测——是在纯净上游 clone 上实测出来的。

### 3.1 `src/bridge.js` — 引入装配器

在文件顶部的 import 区（找 `const __dirname = ...` 那行，插在它前面）：

```js
import { createSingWiring } from './sing-wiring.js';
```

### 3.2 + 3.3 `src/bridge.js` — 装配并挂路由

找到 `http.createServer(async (req, res) => {`，进到回调内部，再找
`const readBody = () => new Promise(...)`。**在这个 `readBody` 定义结束之后**插入整块：

```js
// ── 点歌 / 代唱接线 ──────────────────────────────────────────────────
// 必须放在 createServer 回调内部、sendJson/readBody 之后。
const sing = createSingWiring({
  // 现成的（这些名字 bridge.js 里本来就有，直接给）
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

  // 需要包一层的（bridge.js 里叫别的名字）
  getCurrentMode: () => currentMode,
  isPaused: () => socialV2.paused === true,
  fetchAction: onebotHttpAction,

  // 可选：只有「管理员代发本地图片」才需要。
  // 上游没有这四个时，imagePath 会返回 501；文字/表情/转发图片/代唱都照常。
  // 你的 bridge.js 里有就带上，没有就整行删掉。
  ...(typeof imageSendRoots !== 'undefined' ? {
    sendImageBufferToKey, imageSendRoots, isInsideAllowedImageDir, imageSendMaxPerHour,
  } : {}),
});

// 挂路由：拦下 4 条 /api/socialV2/* 路径，其余原样放行
if (await sing.handle(req, res, url)) return;
// ────────────────────────────────────────────────────────────────────
```

> `coverJobs` / `relaySongScope` / `submitTrackedSong` 这三个**不用传**——
> `createSingWiring` 内部自己创建并导出，漏了反而会重复实例化。

它会拦下这四条路径：

```
POST /api/socialV2/sing
POST /api/socialV2/sing-status
GET  /api/socialV2/relay-targets
POST /api/socialV2/relay-send
```

> `url` 是回调里 `try { url = new URL(...) }` 解析出来的那个变量，所以插入位置
> 必须在那之后。上面选的 `readBody` 之后天然满足。

跑一次这个，看有没有报错：

```powershell
node "$Bridge\scripts\check-deps.mjs" "$Bridge\src\bridge.js"
```

它会告诉你缺哪一项、哪些只是名字不同。**0 项真正缺失**才继续。

### 3.3b 关闭时清理（可选）

如果你的 bridge.js 里有 `server.on('close', ...)`，加一行：

```js
sing.close();   // 清掉两个 setInterval，避免测试里进程不退
```

上游某些版本没有这个钩子，跳过即可——进程退出时定时器自然会没。

### 3.4 `src/mcp-snowluma-safe.js` — 注册工具

> `wire-all.mjs` / `wire-mcp.mjs` 会自动做这一步。下面是手工版，供脚本失败时对照。

```js
import { registerSingTools, SING_TOOL_FLAGS } from './mcp-sing-tools.js';
```

**两个位置，都要对：**

**(a) 并进工具开关表** —— 接在 `const TOOL_CONFIG_FLAGS = { ... };` 这个块之后：

```js
Object.assign(TOOL_CONFIG_FLAGS, SING_TOOL_FLAGS);
```

> 这一步不能漏。漏了的话，控制台显示「工具已关闭」，但模型每次请求仍然收到
> 完整 schema 和描述，调用后再吃 403 —— 白烧 token，还诱导模型误调。

**(b) 注册工具** —— 接在 `server.tool` 被包装成 `registerTool` 的那个块之后
（找 `const registerTool = server.registerTool.bind(server);`，它外面是一个单独的 `{`）：

```js
registerSingTools(server, { z, agentApi, serializeModelData, cfg });
```

> **顺序很重要**：必须放在包装**之后**。放在之前的话，这四个工具会绕过开关过滤
> 和 `selfKeyGuard` 会话绑定闸门 —— 等于开了个后门，模型能把歌提交到别人的会话。

**验证工具真的注册上了**（不是看代码，是看真实进程）：

```powershell
node scripts/verify-mcp-tools.mjs C:\path\to\qq-bridge
```

它会 spawn 一个 MCP 进程、走 `initialize` → `tools/list`，然后告诉你四个工具在不在。

---

## 四、配置

在 `qq-bridge/config.json` 的 `socialV2` 下加：

```json
{
  "socialV2": {
    "sing": {
      "enabled": true,
      "root": "D:/qq-ai-sing-data",
      "python": "C:/path/to/venv/Scripts/python.exe",
      "script": "C:/path/to/qq-ai-sing/pipeline/cover_job.py",
      "jobsDir": "D:/qq-ai-sing-data/jobs",
      "maxPerHourPerSession": 3,
      "maxPerDayPerSession": 8,
      "maxPerHourGlobal": 6,
      "maxQueuedTotal": 5,
      "etaChorusSec": 300,
      "etaFullSec": 1500,
      "dailyUnlimitedTargets": []
    }
  }
}
```

| 字段 | 说明 |
|---|---|
| `root` | 数据根目录（任务/产物/缓存）。**别指向仓库目录**，会被 .gitignore 掉 |
| `python` | 跑流水线的解释器（有 numpy/soundfile/scipy/av 的那个） |
| `script` | 指向仓库里的 `pipeline/cover_job.py` |
| `jobsDir` | 任务 JSON 目录，桥接靠扫它做限频和去重 |
| `maxQueuedTotal` | 并发上限，**别超过 5**；8 GB 显存一次只能跑一个 |
| `dailyUnlimitedTargets` | 豁免每日限额的会话，格式 `private:QQ号` / `group:群号` |

还要有 `ownerQQ`（代发/代唱要用）：

```json
{ "ownerQQ": 你的QQ号 }
```

---

## 五、凭据是怎么传的（别自己改）

发送环节**不接受任何写死的 token**：

```
桥接 spawn cover_job.py 时注入
  QQ_CONSOLE_TOKEN = config.consoleToken
  QQ_CONSOLE_PORT  = config.consolePort
        ↓
jobs.py 用这个 token 给任务算 HMAC（submission_binding）
        ↓
delivery.py 用同一个 token 调 /api/voice/send
```

也就是说：

- 模型**看不到** token，也改不了
- 换 token 只需要改 `config.json`，脚本不用动
- `pipeline.py` 在跑 GPU 阶段前会**主动清掉**这两个环境变量，避免泄漏给子进程

如果你看到 `Missing inherited bridge credentials`，说明不是从桥接发起的——
本地调试请加 `--no-send`。

---

## 六、验证（按顺序，别跳）

### 6.1 接线自检（不需要 QQ，不需要显卡）

```powershell
node bridge/src/selftest.mjs        # 期望：29 项通过 / 0 项失败
node bridge/src/selftest-mcp.mjs    # 期望：16 项通过 / 0 项失败
```

这两步验证的是**闸门关得住**：群友会话调代发会被拒、非白名单目标不能夹带文字、
代唱请求误用 `qq_sing` 会被指回 `qq_relay_send`。这些错了就是提权，必须先绿。

### 6.2 不带 QQ，只验证流水线

```powershell
cd pipeline
python -m hq_cover.jobs render --song "我怀念的 孙燕姿" --mode chorus --out D:/test-1
```

成功会打印 `COVER_PATH=...`。

### 6.3 只验证提交链路（不发 QQ）

```powershell
python pipeline/cover_job.py submit --song "我怀念的 孙燕姿" --mode chorus --no-send
```

应该立刻返回 JSON，里面有 `job`。然后：

```powershell
python pipeline/cover_job.py status --job <那个 id>
```

看 `stage` 一步步往前走。

### 6.4 群里点歌

在群里发「唱首 XX」，看桥接日志里有没有：

```
[sing] 提交 group:xxxx 《XX》 mode=full → job=xxxx-xxxx
```

跑完会主动发回原会话。

### 6.5 代唱

在**管理员私聊**里说「唱首 XX 发给某人」。日志应该有：

```
[relay] private:管理员 → private:某人: 成功 0/1（点歌 job=xxxx）
```

成品直接落到对方会话，失败提示只回给管理员。

---

## 七、模型会看到什么

`cover-job.js` 的 `publicCoverStatus()` 会把内部状态投影成一组安全字段：

| 字段 | 含义 |
|---|---|
| `sent` | 只有真的回读确认过才是 `true` |
| `qualityHold` | 质量检查暂停，需要人工复核 |
| `deliveryUncertain` | 发送结果不确定，**禁止重发** |
| `retryProhibited` | 同上，给模型看的硬约束 |
| `statusHuman` | 中文状态说明 |

**绝不暴露**：本地路径、传输凭据、原始 traceback、签名下载 URL。

这也是为什么 `qq_sing` 的描述里反复强调"只有 sent=true 才能说已送达"——
超时很可能只是回读没拿到，消息其实已经发出去了。自动重发会造成重复消息。

---

## 八、代发/代唱到底能发给谁

这是最容易被误解的地方，明确写在这里：

| 内容 | 白名单好友/群 | 非白名单的真实好友/群 |
|---|---|---|
| 唱歌（`song`） | ✅ | ✅ |
| 文字（`messages`） | ✅ | ❌ 403 |
| 收藏表情（`stickerId`） | ✅ | ❌ 403 |
| 转发图片 | ✅ | ❌ 403 |
| 引用回复 | ✅ | ❌ 403 |

也就是说：**唱歌可以发给任何真实好友或已加入的群**，但「替机器人发消息」
仍然只限聊天白名单。而且两者都只能从 `private:<ownerQQ>` 这个会话发起。

群友在任何会话里都满足不了「发起方必须是 owner 私聊」这一条，
所以群聊里的 prompt injection 拿不到这个口子。

### 8.1 上游桥接没有的那部分

`imagePath`（代发生图目录里的本地图片）需要 bridge.js 里有
`sendImageBufferToKey` / `imageSendRoots` / `isInsideAllowedImageDir` 这套东西。
上游没有 → 这条功能会返回 **501**，其余全部照常。
`check-deps.mjs` 会把它列成「可选缺失」，不阻止启动。
