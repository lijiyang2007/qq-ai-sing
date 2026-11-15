# 安装

分三块：**流水线**（能单独跑）→ **UVR 分离** → **RVC 换声**。
最后一块是可选的 QQ 桥接接入。

支持 **NVIDIA（CUDA）** 和 **AMD / Intel（DirectML，Windows）**，
Linux 上 AMD 走 ROCm。下面的示例用 PowerShell，Linux 换成对应命令即可。

---

## 0. 硬件与前置

| 项 | 最低 | 推荐 | 说明 |
|---|---|---|---|
| 显卡 | **4 GB 显存** | 8 GB+ | NVIDIA 走 CUDA、AMD/Intel 走 DirectML（Windows）、Linux AMD 走 ROCm |
| 内存 | 16 GB | 32 GB | CPU 兜底时需要更多 |
| 磁盘 | 20 GB 空闲 | | 权重约 1.2 GB，产物更大 |
| Python | 3.10 | | 3.11/3.12 在 torch 和 RVC 上兼容性差 |

**关于显存**：RVC 上游对 ≤4 GB 的卡有专门的保守分块档
（`x_pad=1, x_query=5, x_center=30, x_max=32`），本项目会按显存自动选。
4 GB 能跑，只是慢一些，而且整首渲染时间会更长。

**关于显卡厂商**：

| 平台 | 后端 | 怎么装 |
|---|---|---|
| NVIDIA (Windows/Linux) | CUDA | 装 CUDA 版 torch |
| AMD / Intel (Windows) | **DirectML** | `pip install torch-directml` |
| AMD (Linux) | ROCm | 装 ROCm 版 torch，`torch.cuda.is_available()` 会返回 True |
| 无独显 | 不推荐 | 上游可退 CPU，但整首渲染要几小时，实际不可用 |

`python scripts/doctor.py` 会打印实际选中的后端。也可以用环境变量
`SING_BACKEND=cuda|dml` 强制指定。

> **DirectML 的两个已知限制**（实测 + 上游 issue #2569）：
> 半精度支持不一致（本项目在 DML 上强制 fp32），
> 且个别算子未实现（例如某些 rmvpe 路径报
> `Could not run ... with arguments from the 'privateuseone' backend`）。
> 后者没有通用补丁，只能换 NVIDIA 卡或等上游补齐。

**三个 Python 环境，互相不能合并**：

| 用途 | 依赖 | 为什么不能合并 |
|---|---|---|
| 主流水线 | numpy / soundfile / scipy / requests / av | 轻量，做编排和混音 |
| UVR 分离 | torch 2.4 + UVR-beta 源码 | 需要 UVR 自己的 `separate` 模块 |
| RVC 换声 | torch 2.0 + faiss + RVC 源码 | 和 UVR 的 torch 版本冲突 |

> 这是第一次安装最容易踩的坑：把三者装进一个环境，必然有一个跑不起来。
> `pipeline/config.py` 里 `UVR_PY` / `RVC_PY` 就是用来分开指定解释器的。

---

## 1. 主流水线

```bash
git clone https://github.com/<你的用户名>/qq-ai-sing.git
cd qq-ai-sing
```

装依赖（用你系统里的 Python 3.10）：

```bash
pip install numpy soundfile scipy requests av
```

可选，跑测试用：

```bash
pip install unittest   # 标准库，不用装
```

确认能 import：

```bash
cd pipeline
python -c "import hq_cover.pipeline; print('ok')"
```

---

## 2. UVR（人声分离）

用的是 **UVR v5.6.1 + beta 补丁** 里的 MelBandRoformer 模型。

### 2.1 拿源码

```bash
git clone https://github.com/Anjok07/ultimatevocalremovergui.git C:\UVR\UVR-beta
cd C:\UVR\UVR-beta
git checkout 9ffd8f2ec66ed5af80ba46556aca58106672f3f6
```

> 这个 commit 是验证过的那一版。新版 UVR 的接口可能变了，`uvr_stage.py` 会跑不通。

### 2.2 建环境

```powershell
python -m venv C:\UVR\venv
C:\UVR\venv\Scripts\python.exe -m pip install -U pip
C:\UVR\venv\Scripts\python.exe -m pip install -r C:\UVR\UVR-beta\requirements.txt
```

再补两个分离阶段要用的：

```powershell
C:\UVR\venv\Scripts\python.exe -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
C:\UVR\venv\Scripts\python.exe -m pip install soxr soundfile pyyaml einops
```

### 2.3 下载分离权重

需要两个文件，放进 `C:\UVR\models\`：

| 文件 | 大小 | 说明 |
|---|---|---|
| `MelBandRoformer.ckpt` | ~870 MB | Kim MelBandRoFormer 权重 |
| `config_vocals_mel_band_roformer_kim.yaml` | ~2 KB | 配套配置，**必须成对** |

来源：Kim MelBandRoformer 的官方发布页 / HuggingFace 镜像。文件名可以在
`config.local.json` 里用 `uvrWeight` / `uvrConfig` 改成你实际的文件名。

### 2.4 验证

```powershell
C:\UVR\venv\Scripts\python.exe C:\UVR\UVR-beta\separate.py --help
```

能出帮助就行。真正的分离验证等跑通整条链路时一起做。

---

## 3. RVC（换声）

推荐直接用整合包，别自己从源码装。

### 3.1 装

搜 "RVC 整合包" / "RVC20240604Nvidia"，解压到 `D:\RVC`。确认有：

```
D:\RVC\
  runtime\python.exe
  assets\hubert\hubert_base.pt
  assets\rmvpe\rmvpe.pt
  assets\pretrained_v2\f0G48k.pth, f0D48k.pth
  infer\modules\vc\
  configs\config.py
```

### 3.2 检查环境

先看 RVC 目录里的 torch 能不能认出你的卡：

```powershell
D:\RVC\runtime\python.exe -c "import torch,faiss,librosa;print('torch',torch.__version__,'cuda',torch.cuda.is_available())"
```

- NVIDIA / Linux-ROCm：`cuda True` 就对了
- AMD / Intel（Windows）：`cuda False` 是**正常**的，接着确认 DirectML：

```powershell
D:\RVC\runtime\python.exe -c "import torch_directml;print('dml ok', torch_directml.device())"
```

两条里至少有一条通。都报错的话，按上一节那张表装对应的 torch。

**最终以这个为准**（它会告诉你实际会走哪个后端、用哪档分块）：

```powershell
python scripts/doctor.py
# [OK] 加速后端   dml / privateuseone:0（DirectML adapter，显存 未知，分块档 (1, 5, 30, 32)）
# [OK] 加速后端   cuda / cuda:0（NVIDIA GeForce RTX 4060 Laptop GPU，显存 8 GiB，分块档 (3, 10, 60, 65)）
```

### 3.3 训练你自己的音色

**这一步不能跳过**，仓库不带任何模型。完整流程见 **[TRAIN_VOICE.md](TRAIN_VOICE.md)**。

训完之后至少要有：

```
D:\RVC\assets\weights\<你的模型>.pth
D:\RVC\logs\<你的索引目录>\*.index
```

---

## 4. 配置

```bash
cp pipeline/config.example.json pipeline/config.local.json
```

编辑 `pipeline/config.local.json`，把四个路径指对：

```json
{
  "dataRoot": "D:/qq-ai-sing-data",
  "voiceId": "myvoice",
  "rvcHome": "D:/RVC",
  "rvcModel": "myvoice48k_e90.pth",
  "rvcIndex": "myvoice48k/added_IVF2230_Flat_nprobe_1_myvoice48k_v2.index",
  "uvrHome": "C:/UVR",
  "rvcPy": "D:/RVC/runtime/python.exe",
  "uvrPy": "C:/UVR/venv/Scripts/python.exe"
}
```

`dataRoot` 是放任务、产物、缓存的地方，别放在仓库目录里（会被 .gitignore，但容易乱）。

### 4.1 生成模型指纹

```bash
python scripts/hash-models.py
```

会写出 `pipeline/models.lock.json`。之后每次渲染都会校验权重没被换掉。
这个文件不进仓库（它记录的是你这台机器的模型）。

### 4.2 自检

```bash
python scripts/doctor.py
```

看到 `0 项失败` 就可以往下走了。如果有 FAIL，按提示改 `config.local.json`。

---

## 5. 第一次跑通（不发 QQ）

```bash
cd pipeline
python -m hq_cover.jobs render --song "我怀念的 孙燕姿" --mode chorus --out D:/sing-data/runs/test-1
```

- `chorus` 是 36 秒短试听，用来验证链路，几分钟出结果
- `full` 是整首，5～15 分钟
- `--out` 必须是**不存在**的目录，而且**必须在 `<dataRoot>/runs/` 下面**（防止覆盖、
  也保证缓存与清理边界清晰）。给错位置会立刻报错并给出可照抄的正确路径，
  不会等你跑完 GPU 才失败。

成功的话最后会打印 `COVER_PATH=...`，那个 wav 就是成品。

### 需要网易云 Cookie

无损音源走的是网易云官方接口，**必须登录**。导出方式：

1. 浏览器登录 music.163.com
2. F12 → Application → Cookies → 复制 `MUSIC_U` 等字段
3. 存成 JSON 放进 `pipeline/secrets/netease_cookies.json`：

```json
{
  "MUSIC_U": "你的值",
  "__csrf": "你的值",
  "NMTID": "你的值"
}
```

**这个文件绝不能提交。** `.gitignore` 已经排除了 `secrets/`。

没有 Cookie 也能跑，但只能拿到 128k 试听，质量检查会拒绝，属于预期行为。

---

## 6. 接 QQ（可选）

需要额外装两样：**SnowLuma**（QQ 协议端）和 **qq-bridge**（消息桥）。

1. 装 SnowLuma：<https://github.com/SnowLuma/SnowLuma/releases>
   下 `SnowLuma-v<版本>-win-x64.zip`，解压后运行，扫码登录 QQ。
   注意 **SnowLuma 的可执行文件必须叫 `node.exe`**，改名会启动崩溃。

2. 装 qq-bridge：<https://github.com/Derpyu520/qq-bridge>
   按它的 README 配好 DSH 和 preset。

3. 把本仓库 `bridge/src/` 下的 7 个模块复制进 `qq-bridge/src/`，
   并按 **[BRIDGE_WIRING.md](BRIDGE_WIRING.md)** 接线。

4. 在 `qq-bridge/config.json` 里加 `socialV2.sing` 段（见 BRIDGE_WIRING.md）。

---

## 下一步

- 训练音色 → [TRAIN_VOICE.md](TRAIN_VOICE.md)
- 接 QQ 桥接 → [BRIDGE_WIRING.md](BRIDGE_WIRING.md)
- 出问题了 → [TROUBLESHOOTING.md](TROUBLESHOOTING.md)
