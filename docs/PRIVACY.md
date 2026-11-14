# 隐私边界

这个仓库是公开的。下面这些**永远不该出现在提交里**，`.gitignore` 已经做了防护，
但你还是要知道自己在防什么。

---

## 一、绝对不能提交的

### 凭据

| 文件 | 里面是什么 |
|---|---|
| `pipeline/secrets/netease_cookies.json` | 网易云 `MUSIC_U` 等登录态 |
| `pipeline/config.local.json` | 你本机的绝对路径（可能含用户名） |
| `pipeline/models.lock.json` | 你模型的指纹（本身不算秘密，但只对你有意义） |
| `qq-bridge/config.json` | `consoleToken`、`dsh.authToken`、`snowluma.accessToken`、`ownerQQ`、白名单 |
| `qq-bridge/state/secrets/*.json` | 各种 API Key（TTS、图像等） |
| `qq-bridge/state/console-token` | 控制台令牌 |

**判断标准**：如果这个文件能让别人以你的身份操作任何东西，就不能提交。

### 模型与语料

| 类型 | 为什么不能提交 |
|---|---|
| `*.pth` / `*.index` | 音色模型。**这是别人的声音**，公开等于帮别人侵权 |
| 训练语料 `*.wav` | 原始录音，含说话人身份信息 |
| 成品翻唱 | 涉及原歌曲著作权 + 你的音色 |

> 这是本仓库刻意不带任何模型的原因。要音色，自己训。

### 会话与记忆

| 类型 | 例子 |
|---|---|
| 聊天记录 | `state/agents/`、任何 `*.jsonl` 会话存档 |
| 记忆文件 | 桥接的轻量记忆、DSH 的 fish-memory |
| 群友印象 | `qq_memory_append` 写下的 memberImpression |
| 黑话库 | `state/slang-agent/`（含具体群聊语境） |
| 角色卡里的真实关系 | `roles/*.md` 里如果写了真实 QQ 号或称呼 |

**这一条最容易被忽略**——它们看起来像"配置"，实际是别人的隐私。

### 标识符

- QQ 号（`private:12345678`）
- 群号（`group:87654321`）
- 白名单 / 黑名单
- owner 的 QQ 号

代码里出现这些一律改成占位符（`group:<你的群号>`）或读配置。

---

## 二、`.gitignore` 已经挡住的

```gitignore
config.local.json          # 本机路径
pipeline/secrets/          # Cookie
models.lock.json           # 模型指纹
*.pth  *.index  *.ckpt     # 权重
*.wav  *.flac  *.mp3       # 音频
jobs/  runs/               # 运行产物
artifact_cache/            # 缓存（可能含历史成品）
audio/                     # 语音库
state/                     # 桥接状态
```

**注意**：`.gitignore` 只影响**之后的提交**。如果某个文件曾经被提交过，
它会留在 Git 历史里，`git rm` 也删不掉。

---

## 三、提交前自查

### 扫一遍可疑字符串

```bash
# QQ 号 / 群号
grep -rnE '\b[1-9][0-9]{7,10}\b' --include='*.py' --include='*.js' --include='*.json' .

# 个人路径
grep -rn 'C:\\Users\\' --include='*.py' --include='*.js' .

# 常见凭据关键字
grep -rniE 'token|secret|apikey|cookie|password' --include='*.py' --include='*.js' .
```

（最后一条会有很多正常命中，重点看**被赋了具体值**的那些。）

### 看看到底会提交什么

```bash
git add -A
git status
git diff --cached --stat
```

**逐条看文件列表**。出现任何 `config.json`、`*.pth`、`*.wav`、`state/` 就停下来。

### 如果已经误提交了

1. **立刻轮换那个凭据**——历史里的东西已经泄露了
2. 用 `git filter-repo` 或 BFG 重写历史
3. 强制推送，并通知所有 fork 的人

删掉文件再提交一次**是不够的**。

---

## 四、运行时也别泄露

流水线本身做了这些防护：

| 防护 | 位置 |
|---|---|
| GPU 阶段前清掉 `QQ_CONSOLE_TOKEN` / `QQ_CONSOLE_PORT` | `pipeline.py` |
| 错误信息只回传受控的本地消息，不透传可能含签名 URL 的异常 | `jobs.py` |
| 模型看到的字段经过投影，不含路径/凭据/traceback | `cover-job.js` |
| 下载 URL 带签名，不打印、不落盘 | `source.py` |

所以你在排错时贴日志也要注意：**别贴完整日志**，只贴出错那几行。

---

## 五、合规提醒

这套东西能做的事，在有些场景是不合适的：

| 场景 | 风险 |
|---|---|
| 用明星/主播的声音训模型 | 侵犯声音权益（中国民法典第 1023 条参照肖像权保护声音） |
| 用商业 TTS 的音色训模型 | 违反服务条款 |
| 公开发布翻唱成品 | 涉及原歌曲的著作权（表演权/信息网络传播权） |
| 用生成的音频冒充真人 | 可能构成欺诈 |

**个人自己玩、自己听**，一般没问题。
**要公开发布**，请自己确认授权。

这个仓库不提供任何绕过付费、会员、版权限制的功能，
也不提供任何现成的音色模型。
