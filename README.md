# qq-ai-sing

让 QQ 里的 AI 真的会唱歌 —— 从「在群里说一句"唱首 XX"」到一条语音消息，
中间是：**取无损音源 → 分离人声 → 换成你训的音色 → 混音验收 → 发回 QQ**。

> **这个仓库不含任何音色模型、训练语料或成品音频。**
> 音色必须你自己训（[docs/TRAIN_VOICE.md](docs/TRAIN_VOICE.md)）。
> 这样既避免侵权，也让仓库保持干净。

```
QQ 消息「唱首 我怀念的」
    │
    ▼
qq-bridge ──► cover_job.py submit ──► 任务 JSON
                                          │
                                          ▼
                          ① 网易云官方接口取无损 FLAC（校验 MD5 + 完整解码）
                          ② UVR MelBandRoFormer 分离人声 / 伴奏
                          ③ RVC 换声（你的音色模型，全曲逐块）
                          ④ float 混音 + 真峰值/削波/帧数验收
                          ⑤ 转 MP3 发回原会话（回读确认）
```

---

## 它解决什么问题

直接让 AI"唱首歌"是做不到的——TTS 只会念，不会唱。

这套东西的思路是：**把原唱的人声换成另一个音色**。
伴奏保持不变，所以节奏、旋律、编曲都是对的，只有嗓音变了。

三个关键决定：

| 决定 | 为什么 |
|---|---|
| 用 **RVC** 而不是 TTS 或 Seed-VC | TTS 不会唱；Seed-VC 零样本效果不稳。RVC 训一个自己的模型，质量高一个数量级 |
| 用 **UVR MelBandRoFormer** 分离 | 比 demucs 干净，乐器残留少 |
| **不做演唱边界推断** | 整条人声轨全过 RVC，不猜哪里开始/结束。宁可让漏进来的乐器被音色化，也不做静音门控把真唱切掉 |

---

## 快速开始

```bash
git clone https://github.com/<你的用户名>/qq-ai-sing.git
cd qq-ai-sing
```

1. **装环境** → [docs/INSTALL.md](docs/INSTALL.md)
2. **训音色** → [docs/TRAIN_VOICE.md](docs/TRAIN_VOICE.md)（必做，仓库不带模型）
3. **自检**：
   ```bash
   python scripts/hash-models.py
   python scripts/doctor.py
   ```
4. **跑一次**（不发 QQ）：
   ```bash
   cd pipeline
   python -m hq_cover.jobs render --song "我怀念的 孙燕姿" --mode chorus --out D:/test-1
   ```
5. **接 QQ** → [docs/BRIDGE_WIRING.md](docs/BRIDGE_WIRING.md)

---

## 目录

```
qq-ai-sing/
├── pipeline/                    # 唱歌流水线（可以完全独立使用）
│   ├── cover_job.py             # 入口：submit / status / list
│   ├── config.example.json      # 复制成 config.local.json 再改
│   └── hq_cover/
│       ├── config.py            # 所有路径和音色都从这里取
│       ├── policy.py            # 流水线身份（版本/策略常量）
│       ├── source.py            # ① 无损音源获取与校验
│       ├── song_resolver.py     #    选版本（原唱优先，排除 Live/Remix）
│       ├── bilibili_source.py   #    B 站来源（明确标注有损）
│       ├── uvr_stage.py         # ② UVR 分离阶段
│       ├── rvc_stage.py         # ③ RVC 换声阶段
│       ├── rvc_pipeline_float.py#    float 精度推理管线
│       ├── mixing.py            # ④ 混音与质量验收
│       ├── transport.py         #    MP3 编码（无间隙）
│       ├── delivery.py          # ⑤ QQ 交付 + 回执
│       ├── artifact_cache.py    #    成品缓存（同源同音色直接复用）
│       ├── jobs.py              #    任务调度 / GPU 锁 / 进程约束
│       ├── processes.py         #    Windows Job Object 进程树约束
│       ├── queueing.py          #    FIFO 队列
│       └── tests/               # 280 个测试，全部可离线跑
├── bridge/src/                  # 桥接侧的 7 个点歌模块（要接到 qq-bridge）
├── scripts/
│   ├── doctor.py                # 环境自检
│   └── hash-models.py           # 生成模型指纹
└── docs/
    ├── INSTALL.md               # 安装
    ├── TRAIN_VOICE.md           # 训练你自己的音色
    ├── BRIDGE_WIRING.md         # 接进 QQ 桥接
    ├── TROUBLESHOOTING.md       # 排错
    └── PRIVACY.md               # 隐私边界（哪些东西永远不该提交）
```

---

## 质量是怎么保证的

这条流水线是**失败即停**的，不会悄悄降级。每个阶段都写 `stage.json`，
下一个阶段校验上一个阶段的哈希。

| 检查 | 位置 |
|---|---|
| 音源必须是官方 lossless，MD5 + 完整解码 + FLAC STREAMINFO 三重校验 | `source.py` |
| 分离结果重建误差检查 | `uvr_stage.py` |
| 模型/索引/HuBERT/RMVPE 哈希 | `rvc_stage.py` |
| 全曲帧覆盖、NaN/Inf、削波 | `rvc_stage.py` / `mixing.py` |
| 4x 过采样真峰值 ≤ -1.2 dBFS | `mixing.py` |
| 发送前预检（dryRun）+ 回执确认 | `delivery.py` |

**故意不做的事**：

- 不把 MP3/AAC 转成 WAV 冒充无损
- 不在无损失败时偷偷换 B 站或低码率
- 不截掉长歌尾奏
- 不在发送结果不确定时自动重发

---

## 已知限制

- **QQ 语音消息会被转 SILK**，接收端不保证无损。本地 WAV 母带才是原始质量。
- **不做演唱边界推断**：漏进人声轨的乐器可能被 RVC 变形成伪人声。这是刻意的取舍。
- **8 GB 显存**是下限，整首需要 5～15 分钟。
- **必须有自己的网易云账号**（无损音源要登录）。不提供任何绕过。
- 技术验收 ≠ 听觉验收。流水线能证明"没坏"，不能证明"好听"。

---

## 隐私与合规

这个仓库**不包含、也不应该包含**：

- 任何音色权重（`.pth` / `.index`）
- 任何训练语料
- Cookie、API Key、console token
- QQ 号、群号、白名单
- 聊天记录、记忆、历史成品

详见 [docs/PRIVACY.md](docs/PRIVACY.md)。

**使用时请注意**：

- 只训你有权使用的声音。拿别人的声音训模型并传播，可能侵权。
- 用商业 TTS 的音色训模型，通常违反其服务条款。
- 生成的翻唱涉及原歌曲的著作权。个人使用一般没问题，公开发布请自行确认。

---

## 致谢

- [RVC](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI) — 换声引擎
- [Ultimate Vocal Remover](https://github.com/Anjok07/ultimatevocalremovergui) — 分离框架
- Kim MelBandRoFormer — 分离权重
- [qq-bridge](https://github.com/Derpyu520/qq-bridge) — QQ ↔ AI 消息桥
- [SnowLuma](https://github.com/SnowLuma/SnowLuma) — QQ 协议端

## 许可

MIT。见 [LICENSE](LICENSE)。

注意：MIT 只覆盖**本仓库的代码**。上面这些依赖和模型权重各有各的许可证，
商用前请逐个确认。
