# ModelSmith × AutoDL 社区镜像（Linux / 4090 24 GB）落地手册

> 目标：在 AutoDL 实例里照着敲，把 ModelSmith 做成**可共享的社区镜像**。
> 证据等级：**A(源码)**=本机代码 `文件:行`；**A(实测)**=本机实测；**C**=AutoDL 官方文档（维护者 转述，未本机复核）；**D**=估算。
> 代码快照 `HEAD=341290b`（`core-builder` 正在做平台抽象层，`core\modelsmith_core.py` 行号会漂；本文给锚点串，漂了用 `Select-String '<锚点>'` 重定位）。**全文未在 AutoDL 上跑过**——见 §6。

## 0. AutoDL 硬约束（C，维护者 转述官方文档）

| 约束 | 值 | 对镜像设计的影响 |
|---|---|---|
| 导入自定义镜像 | **不支持** | 只能「实例配好 → 关机 → 保存镜像」（保存**整个系统盘**） |
| 系统盘 | **30 GB** | 代码+依赖挤进 30 GB，且保存前留 ≥5 GB 余量 |
| 免费镜像空间 | 30 GB（按容量计费） | 镜像**免费发布、不产生收入**（§5） |
| 数据盘 | 50 GB 免费，**换镜像清空系统盘、数据盘不随镜像走** | 模型/状态一律放数据盘；别人起机数据盘是空的 ⇒ 首次启动自己拉模型 |
| 主流卡 | RTX 4090 **24 GB** / 内存 120 GB / ¥2.08 时 | 只有 24 GB 显存 ⇒ §2 只放能跑的 arch |

仓库已有 `start-modelsmith.sh`（POSIX 启动器，但它要求**自带 runtime 包** `runtime/python/bin/python3`，`start-modelsmith.sh:10-21`，且不传 `--models-dir`/`--diffsynth-src`）；AutoDL 镜像走**系统 python + 数据盘模型**，故 §3.1 给的是镜像专用版本（显式 `--models-dir`/`--diffsynth-src`，不依赖 runtime 包）。

## 1. 镜像里放什么 / 不放什么（体积预算）

| 内容 | 放法 | 预算 | 依据 |
|---|---|---:|---|
| Python 运行时 | **用 AutoDL 自带 python** | —（基础镜像已含） | C；本机 Windows 运行时不可复用（`dist\PACK-README.md:26`） |
| torch+nvidia-*+transformers/diffusers/accelerate/safetensors/Pillow/numpy | `pip install` 到系统 site-packages | **≤6.0 GiB（D）** | A 对照：Windows 运行时 `torch` 单件 2,925,651,321 B（`dist\RUNTIME-PACK.md:29`）；Linux 另含 `nvidia-*`，按 6 GiB 预算 |
| DiffSynth-Studio 源码（1,695 文件） | 方案包 `third_party/DiffSynth-Studio/` | **≈10 MiB** | A：方案包 12.76 MB 整体（`dist\PACK-README.md:24,35`） |
| ModelSmith 代码/UI/文档 | 方案包解压 | **≈13 MiB** | 同上 |
| `start-modelsmith.sh` + `download-models.sh` | 根目录 | <20 KiB | §3/§4 |
| pip cache / `~/.cache` / `__pycache__` | **保存前删除** | 0 | 判据 `du -sh ~/.cache` = 0 |
| **合计新增** | | **≈6.1 GiB** | 远低于 25 GB 上限 |

**不放（硬规则）**：任何 `*.safetensors`/`*.gguf`/模型目录、`_runs/`、token/state、ComfyUI、数据集 zip。
> 依据：本机 `models\` 三个模型合计 **67.46 GiB**（z-image-turbo 30.64 + moss-image-sft 25.36 + z-image 11.46 GiB，A 实测）——**放一个就爆 30 GB 系统盘**。
> 数据盘约定：`/root/autodl-tmp/models/`（50 GB 免费盘）→ `--models-dir`；`--data-dir /root/autodl-tmp/state`。

## 2. 模型体积 vs 24 GB 显存：**默认只留 z_image**

| arch | 512² 峰值显存（A 实测，32 GB 卡） | 24 GB 能跑？ | 依据 |
|---|---:|---|---|
| `qwen_image_21` | **31,945 MiB（31.2 GiB）** | ❌ | `docs\IMAGE-ARCH-PRESETS.md:31`（succeeded/63.1 s/峰值 31,945 MiB） |
| `moss_image` | **26,869 MiB（26.2 GiB）** | ❌ | `docs\IMAGE-ARCH-PRESETS.md:32`（succeeded/43.0 s/峰值 26,869 MiB） |
| **`z_image`** | **21,927 MiB（21.4 GiB）** | ✅ 余量 ≈2.1 GiB | `docs\IMAGE-ARCH-PRESETS.md:33`（succeeded/31.6 s/峰值 21,927 MiB） |

**镜像默认参数（写死进脚本与镜像说明）**：`arch=z_image`；`max_pixels=262144`（512²，16 对齐）；`lora_rank=32`；`use_gradient_checkpointing=开`；`enable_model_cpu_offload=**必须关**`；`--initialize_model_on_cpu`/`--processor_path` **不许传**；`save_steps=0`；tokenizer 用本地 `--tokenizer_path`。
> 依据：rank 32 与固定 7 个 target 模块（`adapters\diffsynth_adapter.py:111,120`）；512²/32/关 offload 的 smoke 读数（`IMAGE-ARCH-PRESETS.md:18,33`）；**开 offload 必崩** `shape mismatch [0] vs [0,3840]` @ `z_image.py:606`（`:43`）；两个 flag 本版不存在（`:46-47`）；`save_steps>0` 就不出 `epoch-*`（`:48`）；内存 120 GB 够（transformer 加载期 CPU 峰值 ~24.6 GB，上卡 cast bf16 ~12.3 GB，`:45`）。

**权重从哪来**（数据盘，不进镜像）：transformer 用 `Tongyi-MAI/Z-Image`，text_encoder/VAE 用 `Tongyi-MAI/Z-Image-Turbo`（官方配方 `third_party\DiffSynth-Studio\examples\z_image\model_training\lora\Z-Image.sh:8`）。本机同构目录实测 `models\z-image` **11.46 GiB**、`models\z-image-turbo` **30.64 GiB**（后者含 F32 transformer，只取 TE/VAE）⇒ 组合预计 **≈17.5 GiB（D）**。

## 3. 建镜像：在 4090 实例里照着敲（每步带判据）

```bash
# 0) 选 AutoDL 官方 PyTorch 2.x + CUDA 12.x 基础镜像，开机先确认
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader   # 判据：RTX 4090, 24564 MiB ±
python -V && df -h /                                             # 判据：python ≥3.10；/ 30G 且可用 ≥25G
# 1) 放代码（包在数据盘，解压到系统盘）
cd /root && unzip -q /root/autodl-tmp/ModelSmith-方案-20261022.zip -d /root/ModelSmith
ls /root/ModelSmith/core/modelsmith_core.py                      # 判据：存在（非空）
# 2) 装依赖（cu12x 轮子按实例驱动选，D/未验）
pip install -U pip && pip install torch --index-url https://download.pytorch.org/whl/cu124
pip install "transformers==4.57.6" "diffusers==0.39.0" "accelerate==1.12.0" safetensors pillow numpy
python -c "import torch,transformers,diffusers as d;print(torch.__version__,torch.cuda.is_available(),transformers.__version__,d.__version__)"  # 判据：True + 三版本
# 3) 落盘启动脚本（§3.1 全文）并语法检查
chmod +x /root/ModelSmith/start-modelsmith.sh && bash -n /root/ModelSmith/start-modelsmith.sh  # 判据：exit 0
# 4) 起核心并验证（127.0.0.1:8765；token 在 <data-dir>/token）
bash /root/ModelSmith/start-modelsmith.sh > /root/ms.log 2>&1 & sleep 6
curl -s http://127.0.0.1:8765/health                             # 判据：200 且含 "ok": true
curl -s -H "X-ModelSmith-Token: $(cat /root/autodl-tmp/state/token)" http://127.0.0.1:8765/api/models | head -c 200  # 判据：{"ok": true, ...}
stat -c '%a %n' /root/autodl-tmp/state/token                     # 判据：600（仅属主可读）
# 5) 6006 → 8765（AutoDL「自定义服务」用 6006；核心只监听 127.0.0.1）
apt-get update -qq && apt-get install -y -qq socat
nohup socat TCP-LISTEN:6006,fork,reuseaddr TCP:127.0.0.1:8765 >/root/socat.log 2>&1 &
curl -s http://127.0.0.1:6006/health                             # 判据：同 8765 的 200/ok
# 6) 清理 + 保存前自检
pip cache purge; rm -rf /root/.cache/* ; find /root/ModelSmith -name __pycache__ -prune -exec rm -rf {} +
du -sh /root/ModelSmith ~/.cache 2>/dev/null                     # 判据：代码树 ≈25 MiB、cache 0
df -h /                                                          # 判据：可用 ≥5G（硬判据）
pkill -f modelsmith_core.py; pkill -f 'socat TCP-LISTEN:6006'    # 判据：0 个残留进程
```
> 最后 AutoDL 控制台 **关机 → 保存镜像**（保存整个系统盘，C）；**镜像大小以控制台读数为准（未验）**。

### 3.1 `start-modelsmith.sh`（全文；镜像内落盘）

```bash
#!/usr/bin/env bash
set -euo pipefail
MS_ROOT="$(cd "$(dirname "$0")" && pwd)"
MS_DATA="${MS_DATA:-/root/autodl-tmp/state}"        # 状态/日志/token 放数据盘
MS_MODELS="${MS_MODELS:-/root/autodl-tmp/models}"   # 模型放数据盘（不随镜像）
PY="${MS_PYTHON:-python3}"
mkdir -p "$MS_DATA"
echo "ModelSmith · 本地训练工作台（镜像免费，不含模型权重）"
echo "  代码: $MS_ROOT   状态: $MS_DATA   模型: $MS_MODELS"
echo "  启动后: http://127.0.0.1:8765/  (AutoDL 自定义服务 6006 → 8765)"
exec "$PY" "$MS_ROOT/core/modelsmith_core.py" \
  --port 8765 --data-dir "$MS_DATA" --models-dir "$MS_MODELS" --project-root "$MS_ROOT" \
  --laya-python "$PY" --llm-python "$PY" --aitk-python "$PY" \
  --diffsynth-python "$PY" --lf-python "$PY" \
  --diffsynth-src "$MS_ROOT/third_party/DiffSynth-Studio"
```
> flag 依据：`--port` 默认 8765（`core\modelsmith_core.py:170,7094-7098`）；`--data-dir`（`:7101`）；`--models-dir`（`:7173`）；五个 `--*-python`（`:7111-7170`）；token 落 `<data-dir>/token`、0600（`:4370,4497,326`）；核心**只绑 127.0.0.1**（`:7,169,7349`）⇒ 6006 必须靠 socat 转发。

## 4. 在 AutoDL 上用（别人起你的镜像）

1. **自定义服务**：实例详情 → 自定义服务 → 端口填 **6006** → 打开控制台给的 URL（域名形状未验）。浏览器不用手填 token（HTML 注入）；`curl` 要 `X-ModelSmith-Token: $(cat /root/autodl-tmp/state/token)`。
2. **首次启动拉模型**（数据盘空，必须做一次）：
```bash
export HF_ENDPOINT=https://hf-mirror.com; pip install -U huggingface_hub
M=/root/autodl-tmp/models/z-image-train; mkdir -p "$M"
hf download Tongyi-MAI/Z-Image       --include "transformer/*"                 --local-dir "$M"
hf download Tongyi-MAI/Z-Image-Turbo --include "text_encoder/*" "vae/*"        --local-dir "$M"
du -sh "$M"                                    # 判据：≈17.5 GiB（D；本机同构 11.46+6.0）
ls "$M/transformer" "$M/text_encoder" "$M/vae" # 判据：三个目录都有 *.safetensors
```
3. **起服务**：`bash /root/ModelSmith/start-modelsmith.sh` → 开 6006 的 URL → `/api/models` 里应出现 `z-image-train`（`kind=diffusion`）。
4. **跑一次**：生图线 → 底模 `z-image-train` → 数据 `resources/04-生图LoRA-图集/smoke-synthetic` → 参数用 §2 默认 → 判据：job `succeeded`、产出 `epoch-0.safetensors`、`/api/jobs/<id>` 的 `peak_vram_mib` < 24,000。
> **数据盘不随镜像走**：换镜像/新用户起机后数据盘为空 ⇒ 每次都要重跑第 2 步（AutoDL 机制，不是缺陷）。

## 5. 共享与获客（明确：**镜像免费、不产生收入**）

- **共享方式**：AutoDL 镜像页 → **共享镜像** → 填对方**用户 ID**（C：不是公开售卖、不是公开市场）。
- **社区镜像入口**：AutoDL「社区镜像」区发布（C；入口位置与审核规则**未验**）。描述里必须写清 3 条：① 镜像**免费**、**不含任何模型权重**、不产生收入；② 首次启动要跑 §4 第 2 步下载 ≈17.5 GiB；③ 默认 `arch=z_image`，qwen/moss 在 24 GB 上跑不了（附 §2 读数）。
- **引流入口**（镜像内自带，不依赖网络）：① `README.md` 首屏；② `start-modelsmith.sh` 启动 banner（§3.1 已印「镜像免费，不含权重」）；③ `docs\QUICKSTART-用户测试.md` + 本文件随方案包分发。
- **许可红线**：只发训练方案/代码，不发权重（与 `dist\PACK-README.md:60-64` 同口径）；Qwen-Image 2.1 是 Research License，**不进镜像**。

## 6. 未验清单（我没法上 AutoDL，全部如实标）

1. **端口转发**：核心只绑 127.0.0.1（A 源码）；AutoDL 自定义服务能否直连 127.0.0.1、还是必须 socat 到 0.0.0.0:6006 —— **未验**（本文按 socat 写，最保守）。
2. **镜像体积/保存结果**：30 GB 盘上 6.1 GiB 新增是否留够余量、保存耗时、共享后的镜像大小 —— **未验**。
3. **真 24 GB 上 z_image 能否跑完 6 步**：21,927 MiB 是 **32 GB 卡**实测，24 GB 卡余量仅 2.1 GiB —— **未验**。
4. **Linux 依赖**：cu12x 轮子在该实例驱动上是否可用、`diffusers 0.39.0` 是否需 git 钉版（本机 Windows 用 commit，见 `docs\PITFALLS.md` C1）—— **未验**。
5. **Linux 路径/权限**：核心 `default_data_dir()` 仍带 `LOCALAPPDATA` 语义（A 源码 `:312`），本文全部显式 `--data-dir` 规避；`core-builder` 平台层落地后需复跑 §3 判据 —— **未验**。
6. **AutoDL 域名形状 / 社区镜像审核 / 共享是否需对方实名** —— C 级转述，未逐条核官方页。
7. **模型下载可达性**：`hf-mirror` 在容器内能否拿到 `Tongyi-MAI/*`（是否需同意条款）—— **未验**。
8. **其它训练线**（决策/LLM/字幕）在 24 GB 卡上的显存：本文只保生图 `z_image`，其余线可用但**未做 24 GB 实测**。
