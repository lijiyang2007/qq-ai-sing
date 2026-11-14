# 接进 QQ 桥接

这一份说明怎么让「在群里说一句"唱首 XX"」真的跑起来。

前置：`docs/INSTALL.md` 已经跑通，`python scripts/doctor.py` 全绿。

---

## 一、这些文件是干什么的

`bridge/src/` 里的 7 个模块，是桥接侧的"点歌入口"：

| 文件 | 职责 |
|---|---|
| `cover-job.js` | **核心**。校验歌名/配额/去重，spawn `cover_job.py submit`，把结果投影成模型能看的字段 |
| `song-intent.js` | 从聊天文本里判断"这是不是一次点歌" |
| `pro-song.js` | `pro唱XX` 这类显式版本许可 |
| `bilibili-song.js` | B 站链接点歌（有损来源，会明确标注） |
| `relay-song-scope.js` | 代唱（A 让机器人唱给 B 听）的授权范围 |
| `song-job-notifier.js` | 跑完主动通知原会话 |
| `voice-delivery-receipts.js` | 发送回执/journal，防重复发 |

**设计前提**：QQ 会话的工具面被锁死了，模型拿不到 `pwsh` / `read` / 文件系统。
所以这些模块只做一件事——**提交一首歌**。参数收窄到只有歌名和模式，
目标会话由桥接按会话令牌强制绑定，模型无法指定别的会话。

---

## 二、复制文件

```powershell
$Repo   = "C:\path\to\qq-ai-sing"
$Bridge = "C:\path\to\qq-bridge"

Copy-Item "$Repo\bridge\src\*.js" "$Bridge\src\" -Force
Copy-Item "$Repo\pipeline\cover_job.py" "D:\sing-data\cover_job.py"   # 或你自己的 dataRoot
```

`cover_job.py` 要放在**桥接 `sing.root` 指向的目录**，因为它就是被 spawn 的入口。

---

## 三、改桥接代码（4 处）

本仓库只给模块，**接线要你自己打**——因为 `bridge.js` 每个版本都不一样。
以下是需要加的位置（行号仅供参考，按你手上的版本找）。

### 3.1 `src/bridge.js` — 引入模块

```js
import { createCoverJobService } from './cover-job.js';
import { parseProSongRequest, parseProSongMessage } from './pro-song.js';
import { parseBilibiliSongRequest, parseBilibiliSongMessage, parseRelayBilibiliSong,
         bilibiliRelayGuidance } from './bilibili-song.js';
import { createSongIntentService, parseTitleOnlySongRequest } from './song-intent.js';
```

### 3.2 `src/bridge.js` — 创建服务实例

在其它服务初始化附近（找 `createXxxService({` 那一片）：

```js
const coverJobs = createCoverJobService({
  getConfig: () => cfg,
  stateDir: path.join(ROOT, 'state'),
  log
});
```

### 3.3 `src/bridge.js` — 挂到 `qq_sing` / `qq_sing_status`

找到 `qq_sing` 的处理分支，把它接到：

```js
const result = await coverJobs.submit(args);
// 或解析歌名：
return pro ? coverJobs.resolveSong({ proQuery: pro.query })
  : autoSelect && !artist ? coverJobs.resolveSong({ autoOriginalQuery: title })
  : coverJobs.resolveSong({ title, artist, autoSelect });
```

### 3.4 `src/mcp-snowluma-safe.js` — 注册工具

在工具表里加 `qq_sing` 和 `qq_sing_status` 两个条目（schema 见该文件里其它
`qq_*` 工具的写法）。工具描述建议直接抄本仓库 README 的「模型会看到什么」一节，
把"不确定就不说已发送"这类约束写进去。

> 完整版 `bridge.js` / `mcp-snowluma-safe.js` 体积很大且与本仓库解耦，
> 这里不给整份文件——避免你覆盖掉自己版本里的其它改动。

---

## 四、配置

在 `qq-bridge/config.json` 的 `socialV2` 下加：

```json
{
  "socialV2": {
    "sing": {
      "enabled": true,
      "root": "D:/AI-Song",
      "python": "D:/AI-Song/runtime/venv-seedvc/Scripts/python.exe",
      "script": "D:/AI-Song/cover_job.py",
      "jobsDir": "D:/AI-Song/jobs",
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
| `root` | 数据根目录，`cover_job.py` 也从这里算 |
| `python` | 跑流水线的解释器（有 numpy/soundfile/scipy 的那个） |
| `jobsDir` | 任务 JSON 目录，桥接靠扫它做限频和去重 |
| `maxQueuedTotal` | 并发上限，**别超过 5**；8 GB 显存一次只能跑一个 |
| `dailyUnlimitedTargets` | 豁免每日限额的会话，格式 `private:QQ号` / `group:群号` |

`config.example.json` 里也加了这一段，可以直接对照。

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

## 六、验证

### 6.1 不带 QQ，只验证提交链路

```powershell
D:\sing-data\runtime\venv-seedvc\Scripts\python.exe D:\sing-data\cover_job.py submit `
  --song "我怀念的 孙燕姿" --mode chorus --no-send
```

应该立刻返回一个 JSON，里面有 `job` 字段。然后用：

```powershell
... cover_job.py status --job <那个 id>
```

看 `stage` 一步步往前走。

### 6.2 群里点歌

在群里发「唱首 XX」，看桥接日志里有没有：

```
[sing] 提交 group:xxxx 《XX》 mode=full → job=xxxx-xxxx
```

跑完会主动发回原会话。

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
