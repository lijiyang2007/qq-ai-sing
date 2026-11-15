# 自己训练音色（本仓库不附带任何模型）

> **重要**：这个仓库**只提供流程和代码，不带任何音色权重、训练语料或成品音频**。
> 你需要用自己的声音、或你有权使用的语音素材，训一个 RVC 模型出来。
> 不要拿别人的声音去训——那是侵权，而且很多平台的条款也明确禁止。

整条链路里，音色由 **RVC（Retrieval-based Voice Conversion）** 决定。
UVR 只负责把歌里的人声和伴奏分开，不参与"变成谁的声音"。

---

## 一、你需要准备什么

| 东西 | 要求 | 说明 |
|---|---|---|
| 干净人声素材 | **10～30 分钟**，越长越好 | 没有伴奏、没有混响、没有背景噪音 |
| 采样率 | 44.1k 或 48k | 44.1k 也能用，但 48k 模型质量更好 |
| 格式 | wav / flac | 单声道或立体声都行 |
| 内容 | 自然说话 + 有起伏 | 语速、情绪要有变化，别全程一个调 |

**素材从哪来（按推荐度）**：

1. **自己录**：手机或麦克风，安静房间，离麦 15～20 cm，避免喷麦。最好照着稿子念，覆盖不同语气。
2. **云 TTS 合成**：如果你有某个 TTS 服务的授权音色，可以用它批量合成语料。
   仓库里的 `bridge/gen_breeze_corpus.mjs` 就是这种思路的示例（需要你自己的 API Key）。
   **注意**：拿商业 TTS 的声音去训模型再公开分发，通常违反它的服务条款——自己用可以，别传播。
3. **公开授权的语音数据集**：注意看许可证是否允许训练和再分发。

> 素材质量比数量重要。20 分钟干净素材 > 2 小时带噪素材。

---

## 二、训练流程（RVC WebUI）

### 1. 装 RVC

推荐用整合包（自带 Python 和依赖，省掉 90% 的环境问题）：

- 搜 "RVC 整合包" 或 "RVC20240604Nvidia" 这类带 `runtime/` 目录的发行版
- 解压到比如 `D:\RVC`，确认里面有：
  ```
  D:\RVC\
    runtime\python.exe          ← 自带解释器
    assets\hubert\hubert_base.pt
    assets\rmvpe\rmvpe.pt
    assets\pretrained_v2\*.pth  ← 预训练底模
    infer-web.py
    ```

启动：`runtime\python.exe infer-web.py`，浏览器打开 `http://localhost:7865`。

### 2. 放素材

把音频放进 `D:\RVC\datasets\<你的实验名>\`（比如 `datasets\myvoice\`）。

### 3. 训练（WebUI 里点，或者用命令行）

WebUI 的「训练」页按顺序点：

| 步骤 | 参数 | 建议值 |
|---|---|---|
| 1. 处理数据 | 采样率 | **48000** |
| | 音高提取算法 | **rmvpe**（比 harvest/pm 准很多） |
| | 声道数 | 1（单声道） |
| 2. 提取特征 | — | 默认即可 |
| 3. 训练模型 | batch size | 8 GB → **4～6**；4 GB → **1～2**（并开 fp16） |
| | 总 epoch | **100～200**（20 分钟素材） |
| | 保存频率 | 每 10 epoch 存一个 |
| | 是否仅保存最新 | 否（要留多个 epoch 做对比） |
| 4. 训练索引 | — | 必做，`index_rate` 靠它 |

**命令行等价流程**（`D:\RVC\run_rvc.ps1` 里有完整示例）：

```powershell
$Py = "D:\RVC\runtime\python.exe"
$Data = "D:\RVC\datasets\myvoice"
$Exp  = "D:\RVC\logs\myvoice"

# 1) 切片 + 重采样到 48k
& $Py infer\modules\train\preprocess.py $Data 48000 1 $Exp True

# 2) 提取 f0（rmvpe）
& $Py infer\modules\train\extract\extract_f0_rmvpe.py 1 0 0 $Exp

# 3) 提取 HuBERT 特征
& $Py infer\modules\train\extract\extract_feature_print.py cuda:0 1 0 0 $Exp v2

# 4) 训练（v2 / 48k / 带 f0）
& $Py infer\modules\train\train.py -e myvoice -sr 48k -f0 1 -bs 6 -g 0 -te 200 -se 10 -pg assets\pretrained_v2\f0G48k.pth -pd assets\pretrained_v2\f0D48k.pth -l 0 -c 0 -sw 1 -v v2

# 5) 建索引
& $Py infer\modules\train\index.py "D:\RVC\logs\myvoice\2a_f0" "D:\RVC\logs\myvoice\3_feature768"
```

### 4. 挑 epoch

训练会产出 `G_10.pth`、`G_20.pth`……`G_200.pth`。

**不要只看 loss 选**。用同一段测试音频把每个候选 epoch 都转一遍，听 + 看指标：

- **听**：有没有金属音、沙哑、跑调
- **指标**：高频能量别过冲（`（`scripts/` 里可以自己写一个类似的对比脚本）` 就是这个思路）

经验值：**e90～e120** 通常在"像"和"稳"之间最平衡。epoch 太高会开始学素材里的瑕疵。

### 5. 放到 RVC 能认的位置

```
D:\RVC\assets\weights\myvoice48k_e90.pth          ← 从 logs\myvoice\G_90.pth 改名复制
D:\RVC\logs\myvoice\added_IVF2230_Flat_nprobe_1_myvoice48k_v2.index
```

索引文件是 `index.py` 跑完自动生成的，名字里带 IVF 参数，别改。

---

## 三、接到本仓库

编辑 `pipeline/config.local.json`（从 `config.example.json` 复制）：

```json
{
  "voiceId": "myvoice",
  "voiceLabel": "我自己的音色",
  "rvcHome": "D:/RVC",
  "rvcModel": "myvoice48k_e90.pth",
  "rvcIndex": "myvoice48k/added_IVF2230_Flat_nprobe_1_myvoice48k_v2.index",
  "uvrHome": "C:/UVR",
  "rvcPy": "D:/RVC/runtime/python.exe",
  "uvrPy": "C:/UVR/venv/Scripts/python.exe"
}
```

然后生成模型指纹并自检：

```bash
python scripts/hash-models.py
python scripts/doctor.py
```

`doctor.py` 里所有 RVC / UVR 项都显示 OK 就可以点歌了。

> `voiceId` 是个自由字符串，但它会进入缓存键。**换音色必须换 voiceId**，
> 否则会命中上一个音色的缓存，拿到错的成品。

---

## 四、参数怎么调（已经验证过的一组）

仓库里的默认值是实测下来最稳的一组，先别动：

| 参数 | 值 | 为什么 |
|---|---|---|
| `rms_mix` | 0 | 改成 1 会让响度跟着原唱走，反而更脏 |
| `index_rate` | 0.75 | 低于 0.5 音色会飘，高于 0.9 会有杂音 |
| `protect` | 0.33 | 保护清音/气声，太低会有电子味 |
| `pitch` | 0 | 男女声互转才需要 ±12 |
| f0 方法 | rmvpe | harvest 会出电音 |
| 分块 | 18 秒 / 0.3 秒重叠 | 8 GB 显存下的稳定值；4 GB 卡项目会自动降到更小的分块档 |

如果结果听起来"后半段失真"，先检查是不是显存不够导致分块被截断，而不是去改这些参数。

---

## 五、常见问题

**Q：训出来完全不像。**
A：先查素材——有没有伴奏残留、混响、或者本身采样率太低。再查 `index_rate` 是不是被调低了。

**Q：训出来有金属音/电流声。**
A：降 epoch，或者降 `index_rate` 到 0.6 试试。素材里如果有削波也会这样。

**Q：显存不够，训练直接 OOM。**
A：`batch_size` 降到 1～2 并开 fp16。8 GB 训 48k v2 是够的；4 GB 很吃紧，可能要 CPU 兜底。

**Q：能不能直接用别人的模型？**
A：技术上可以（把 pth 和 index 放进对应目录即可）。但**公开发布别人的音色模型需要授权**。
这个仓库刻意不带任何模型，就是为了避开这个问题。

**Q：能不能用 so-vits-svc / Seed-VC？**
A：能，但那两条链路的效果和稳定性都不如 RVC 48k v2，而且维护成本高。
本仓库的流水线只走 RVC。
