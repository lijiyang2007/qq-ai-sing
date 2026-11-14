# 排错

按「症状 → 原因 → 怎么办」组织。这些都是实际踩过的。

---

## 安装阶段

### `ModuleNotFoundError: No module named 'faiss'`

**原因**：跑错解释器了。faiss 只装在 RVC 的环境里，主流水线环境没有也不需要。

**怎么办**：`rvc_stage.py` 必须用 RVC 的解释器跑：

```powershell
D:\RVC\runtime\python.exe hq_cover\rvc_stage.py --input vocals.wav --out newdir
```

检查 `config.local.json` 里的 `rvcPy` 是不是指向 `D:\RVC\runtime\python.exe`。

### `缺少 SING_UVR_PY 指向的 Python 解释器`

**原因**：`uvrPy` / `rvcPy` 没配，或者路径写错。

**怎么办**：

```bash
python scripts/doctor.py
```

它会明确告诉你哪一项找不到。

### `torch.cuda.is_available()` 返回 False

**原因**：装了 CPU 版 torch，或者 CUDA 版本和驱动不匹配。

**怎么办**：

```powershell
D:\RVC\runtime\python.exe -c "import torch;print(torch.__version__, torch.version.cuda)"
```

如果是 `+cpu`，重装带 cu 的版本。检查驱动：

```powershell
nvidia-smi
```

---

## 分离阶段（UVR）

### `缺少 UVR 分离权重`

**原因**：`MelBandRoformer.ckpt` 或 yaml 没放对位置。

**怎么办**：两个文件都要放进 `<uvrHome>/models/`。文件名和 `config.local.json` 的
`uvrWeight` / `uvrConfig` 必须一致。

### `SHA256 mismatch for ...`

**原因**：`models.lock.json` 里记录的哈希和当前文件不符——文件被换过或下载不完整。

**怎么办**：

- 如果你**故意换了模型**：删掉 `pipeline/models.lock.json`，重跑 `scripts/hash-models.py`
- 如果没换：重新下载，文件可能损坏了

### `Refusing existing output path`

**原因**：输出目录已经存在。这是**故意的**——防止覆盖已完成的成品。

**怎么办**：换个新的输出目录名。

---

## 换声阶段（RVC）

### `音色模型架构不符：需要 RVC v2 / 带 f0 / 48k`

**原因**：训的是 v1 模型，或者没带 f0，或者采样率不是 48k。

**怎么办**：重新训。训练时 `-v v2`、`-f0 1`、`-sr 48k` 三个都要对。

### 后半段突然失真 / 金属音

**原因**（按可能性排序）：

1. **显存不足**，分块推理被截断
2. 素材里有削波
3. epoch 训太高，学到了素材瑕疵

**怎么办**：

- 先看 `stage.json` 里的 `chunk_reports`，检查每块的 `engine_returned_frames`
  和 `output_start_frame` 有没有异常
- 换低一点的 epoch（e60~e90）试试
- 别急着改 `rms_mix` / `index_rate`——默认那组是验证过的

### `RVC chunk N failed: ...`

**原因**：单块推理失败，通常是 f0 提取在极端段落（纯气声、长静音）出错。

**怎么办**：看 `<输出目录>/rvc/*.log` 里的具体报错。如果是 f0 问题，
确认 `rmvpe.pt` 存在且哈希对。

---

## 音源阶段（网易云）

### `未取得通过校验的无损音源，已暂停`

**原因**（按可能性排序）：

1. 没有 Cookie，只能拿到 128k 试听
2. Cookie 过期
3. 这首歌本身没有无损版本
4. 会员到期

**怎么办**：

- 检查 `pipeline/secrets/netease_cookies.json` 里的 `MUSIC_U` 是否有效
- 在网页上确认这首歌标着"无损"
- **不要**想着绕过——流水线会拒绝降级到有损来源，这是设计

### `NeedsReview: 歌名、歌手或发行版本需要确认`

**原因**：搜到多个同名版本，无法确定是哪一个。

**怎么办**：加上歌手名重试，或者用 `--track-id` 指定准确的歌曲 ID。

### `标题黑名单` 命中

**原因**：歌名里带了"伴奏/纯音乐/翻唱/remix"这类词。

**怎么办**：确认你要的是原唱版本，去掉这些词。

---

## 交付阶段（QQ）

### `No decodable audio stream: Decoder not found`

**原因**：SnowLuma 某些版本**不支持 24-bit WAV 作为语音消息**。

**怎么办**：

- 走 MP3 通道（`cover_job.py` 的默认路径就是转 MP3）
- 或者用文件附件接口发完整 WAV，而不是语音条
- **不要**降位深糊弄过去

### `QQ发送结果不确定：已查询本次交付回执但未确认，禁止自动重发`

**原因**：POST 超时了，但消息**可能已经发出去**了。

**怎么办**：

1. **先去会话里看**，别急着重发
2. 确实没发出去，才由管理员手工重试
3. 流水线已经把任务标成 `delivery_uncertain` + `retry_prohibited`，就是为了防这个

自动重发的后果是群里收到两条一样的语音。

### 语音时长被截断

**原因**：桥接有 300 秒上限，超了会被拒或截断。

**怎么办**：`cover_job.py` 会在 295 秒处主动截断并淡出。如果不想截，
改成发文件附件而不是语音条。

### QQ 里的音质和本地 WAV 不一样

**原因**：QQ 语音消息会被转成 SILK，这是协议层的压缩，**不可避免**。

**怎么办**：本地保留 WAV 母带。要发无损就发文件，别发语音条。

---

## 桥接阶段

### `点歌功能已关闭（socialV2.sing.enabled=false）`

**原因**：配置里关了。

**怎么办**：`qq-bridge/config.json` → `socialV2.sing.enabled = true`。

### `翻唱运行时不可用，请管理员检查安装配置`

**原因**：`socialV2.sing.python` 或 `script` 指向的文件不存在。

**怎么办**：路径要写**绝对路径**，且用正斜杠或双反斜杠。

### `这首歌正在制作了（任务 xxx）`

**原因**：去重生效了——同一首歌在排队/制作中。

**怎么办**：等它做完。这是设计，不是 bug（GPU 只有一块）。

### `Missing inherited bridge credentials`

**原因**：不是从桥接发起的，所以没有 `QQ_CONSOLE_TOKEN`。

**怎么办**：本地调试加 `--no-send`。

---

## 跑测试

```bash
cd pipeline
python -B -m unittest discover -s hq_cover/tests -t . -p "test_*.py"
```

应该有 `OK (skipped=1)`。

如果报 `缺少 SING_UVR_PY 指向的 Python 解释器`，说明测试环境没有真实 UVR/RVC。
设两个假路径就能跑纯逻辑测试：

```powershell
$env:SING_UVR_PY = "C:\Windows\System32\cmd.exe"
$env:SING_RVC_PY = "C:\Windows\System32\cmd.exe"
```

（这些测试不会真的调用解释器，只是过一下存在性检查。）

---

## 还是不行

收集这些再提问：

1. `python scripts/doctor.py` 的完整输出
2. `pipeline/jobs/<任务id>.json` 里的 `stage` 和 `error`
3. 对应阶段的 `*.log`（UVR / RVC 各一份）
4. `nvidia-smi` 的输出

**别贴 Cookie、token、或带签名的下载链接。**
