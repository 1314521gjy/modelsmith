# Qwen-Image 2.1 LoRA 训练：成品路线侦查（2026-10-22）

> 只查不装：未下载、未装包、未跑 GPU、未改产品文件。
> 等级：**A**=本机文件原文/本机实测 ｜ **A(源码)**=上游仓库页原文 ｜ **C**=第三方网页 ｜ **未验**=本次没取回原文。
> 预算：`web_search` 1 次（4 query）、`web_fetch` 6 次（5 成 1 败）。

## 0. 先读这一节（结论比工具更重要）

1. **工具层面有现成成品，而且两套都能照搬**：DiffSynth-Studio（**Apache-2.0**）官方表格里 Qwen-Image-2.1 的 **LoRA Training 一栏有 code**；ai-toolkit（**MIT**，本机已装且 SD1.5 已跑通）里已有 `qwen_image_2` 加载器。
2. **但权重许可证是硬闸**：`E:\临时文件夹\Qwen-Image-2.1\LICENSE` 是 **Qwen RESEARCH LICENSE AGREEMENT**（Release Date: September 20, 2026，7,831 B / 55 行），原文：
   - 「FOR **NON-COMMERCIAL** PURPOSES ONLY」；商业用途须另行申请（`model-business@notice.qwencloud.com`）；
   - 再分发须附协议副本 + `Notice` 归属声明 + 若用它训练/改进模型还须标「**Built with Qwen**」。
   - ⇒ 用户要求「许可证干净（Apache/MIT/BSD）」**只有工具满足，权重不满足**；Qwen-Image **2.0** 才是 Apache-2.0（C 级，modelscope README 片段）。
3. **因此首选取决于一个问题**：内部研究/自用 → 走 DiffSynth 训 2.1；要商用/对外分发 → **换 2.0（Apache-2.0）或 Z-Image**，工具路线不变。
4. 本机 2.1 崩 `Cannot copy out of meta tensor` 的原因已经定位到**权重形态**，不是显卡也不是装错：本地是 **diffusers 分片目录**，ai-toolkit 的 2.1 加载器默认去拿 **Comfy-Org 单文件 repack**。

## 1. 候选逐个核（按照搬成本从低到高）

| # | 方案 | 能否照搬 | 许可证（源站） | Windows | 显存 | 依赖体量 | 落地时间 | 依据 |
|---|---|---|---|---|---|---|---|---|
| 1 | **DiffSynth-Studio** | ✅ 官方表 Qwen-Image-2.1 行：Inference/LowVRAM/Full/**LoRA**/Validate 全有 code；例程 `examples/qwen_image_21/model_training/train.py` | **Apache-2.0**（LICENSE 全文已取，Copyright 2023 Zhongjie Duan） | 纯 Python，无平台专用件（未验 Windows 实测） | 未验（官方有低显存/CPU offload；`--enable_model_cpu_offload`） | 新环境约 **3–4 GB**（torch+依赖，D 级估值）；若复用 `E:\ComfyUI\python_embeded` 可能仅 +~50 MB（未验） | **30–60 分钟**（装环境+1 步 smoke） | `github.com/modelscope/DiffSynth-Studio` README「All Supported Models」表 + `examples/qwen_image_21/` 目录（A 源码）；LICENSE 页（A 源码） |
| 2 | **ai-toolkit（本机已装）** | ⚠️ 能，但要改一处：把权重形态对齐 | **MIT**（`third_party\LICENSES\ai-toolkit-LICENSE` 头 = MIT, Ostris LLC） | ✅ 已在本机跑通 SD1.5 | 未验（`quantize: true` + 32GB 单卡应可） | **0**（已装） | 改配置 **10 分钟**；若换 Comfy 权重需下 ~15 GB（>1 GiB，须 维护者 批） | 本机代码（A） |
| 3 | HF diffusers `examples/qwen_image` | 未验（本次未取回该目录） | diffusers 库 Apache-2.0；例程另计（未核） | 未验 | 未验 | 已有 torch 则可复用 | 未验 | — |
| 4 | OneTrainer | 未核（未取源站） | 未核 | 未验 | 未验 | 未验 | 未验 | — |
| 5 | Diffusion-Pipe | 未核 | 未核 | 未验 | 未验 | 未验 | 未验 | — |
| 6 | **SimpleTuner** | ❌ **直接排除** | **AGPL-3.0**（C 级：AI-TLDR 工具页） | — | — | — | — | 用户约束：AGPL 不进分发 |
| 7 | kohya **sd-scripts（本机 v0.12.2）** | ❌ **无入口** | Apache-2.0（本机 `LICENSES\sd-scripts-LICENSE`） | — | — | — | — | 本机 grep：只有 `library/qwen_image_autoencoder_kl*.py`（VAE），**没有 qwen 训练脚本**（顶层无 `qwen*_train*.py`） |
| 8 | ComfyUI 训练节点 | ❌ **0 命中** | — | — | — | — | — | 本机 `E:\ComfyUI\custom_nodes` 为**空目录** |

## 2. 首选：DiffSynth-Studio（Apache-2.0）

**为什么是它**：唯一一个「许可证干净 + 官方明写支持 2.1 LoRA 训练 + 有专门例程目录」的组合；不碰 AGPL，也不用赌 ai-toolkit 的量化分支。

**1 步 smoke（可直接复制；参数名见「未验」说明）**

```powershell
# 1) 拿代码（只读克隆，不装）
git clone --depth 1 https://github.com/modelscope/DiffSynth-Studio E:\视频\modelsmith\third_party\DiffSynth-Studio
cd E:\视频\modelsmith\third_party\DiffSynth-Studio

# 2) 先看入口脚本的真实参数（本次未取回 argparse，必须先跑这一步）
python examples/qwen_image_21/model_training/train.py --help
```

```powershell
# 3) 1 步 smoke（DS LoRA 常用参数据 README 惯例；以 --help 为准）
python examples/qwen_image_21/model_training/train.py `
  --dataset_base_path   "E:\视频\modelsmith\datasets\smoke_imgs" `
  --dataset_metadata_path "E:\视频\modelsmith\datasets\smoke_imgs\metadata.csv" `
  --model_paths '["E:\临时文件夹\Qwen-Image-2.1"]' `
  --output_path "E:\视频\modelsmith\output\qwen21_lora_smoke" `
  --lora_rank 32 --num_epochs 1 --learning_rate 1e-4 --trainable_models "dit"
```

**数据形状**：一个图片文件夹 + 一份 `metadata.csv`（DiffSynth 惯例是 `image,prompt` 两列；**未验**该版本列名）。
**为什么用本地目录**：DiffSynth 直接吃 diffusers 目录（`model_index.json` 在），不需要 Comfy repack——这正是它比 ai-toolkit 少一步的地方。
**显存**：32 GB 单卡跑 2.1（~14 GB DiT + Qwen3-VL-8B 文本编码器）**未验**；若 OOM，加 CPU offload（`--enable_model_cpu_offload`）或先 `--lora_rank 16`。

## 3. 次选：ai-toolkit 改一个参数（MIT，本机已装）

**崩溃根因（A 级，本机代码）**：
- `extensions_built_in\diffusion_models\qwen_image_2\qwen_image_2.py:182-196`：`base_model_path` 逻辑分三支——单文件 → 去 HF 取 `Qwen/Qwen-Image-2.1` 的 configs；**本地目录且含 `text_encoder/`** → 用本地当 config 源（我们的目录符合）。
- `toolkit\models\v2\_mixin.py:470-481` 原文：`name_or_path` 可以是「local diffusers directory / hub repo / 单文件」；「Local dirs and unregistered repos load as-is; **`use_comfy_weights=False` opts out**」；而 `use_comfy_weights` 默认 **True**，且 2.1 注册了 `_COMFY_FILES`（`src/transformer.py:919-926`，指向 `diffusion_models/qwen_image_2.1_{int8_convrot,bf16}.safetensors`）。
- ⇒ 默认路径会尝试用 **Comfy 单文件**去装 diffusers 分片目录，量化阶段拿到未物化的空权重 → `Cannot copy out of meta tensor`。

**最小改法（配置层，未验）**：

```yaml
# config/examples/train_lora_qwen_image_24gb.yaml 的副本里改这三处
model:
  name_or_path: "E:\\临时文件夹\\Qwen-Image-2.1"   # 本地 diffusers 目录
  arch: qwen_image_2                                # 别用 qwen_image（那是 2.0）
  quantize: true
  model_kwargs:
    use_comfy_weights: false                        # ← 关键：让本地目录按原样加载
sample:
  guidance_scale: 1.0                               # 2.1 设计为无 CFG 采样
  sample_every: 250
  prompts: ["a red apple on a wooden table, studio light"]
```

**另一条等价路线**：不改代码，改权重——下 `Comfy-Org/Qwen-Image-2.1` 的 `diffusion_models/qwen_image_2.1_bf16.safetensors` + `vae/qwen_image_2.1_vae_bf16.safetensors`（~15 GB，**>1 GiB 须 维护者 批**），把 `name_or_path` 指向该单文件。这条**最稳**（等于走上游默认路径），但要多下 15 GB。

## 4. 排除了什么（省得再试）

- **SimpleTuner**：AGPL-3.0，按用户约束不进分发 ⇒ 不做候选（哪怕它支持 40+ 架构）。
- **sd-scripts**：本机 v0.12.2 **没有 Qwen-Image 训练入口**（只有 VAE 库与其它架构的 `*_train*.py`）；要用得等上游加，别自己拼。
- **ComfyUI 训练节点**：本机 `custom_nodes` 空 ⇒ 0 命中（检索式：`ComfyUI Qwen-Image LoRA training node`，本次未跑该检索，属**未验**）。
- **OneTrainer / Diffusion-Pipe / HF diffusers examples**：本次时间盒内未核（0 命中，非「不支持」）。

## 5. 落地判断（给 维护者 的三句话）

1. **能照搬，首选 DiffSynth-Studio（Apache-2.0）**：`examples/qwen_image_21/model_training/train.py` 是官方 2.1 LoRA 入口，落地 30–60 分钟，新环境约 3–4 GB（D 级估值）；若复用 ComfyUI 的 Python 可能 0 新装（未验）。
2. **ai-toolkit 不用重装**，`model_kwargs.use_comfy_weights: false` 是根因级修复（本机代码原文支持），10 分钟可试；失败就换 Comfy 单文件权重（15 GB 需批）。
3. **真正的拦路虎是权重许可**：2.1 是 **Qwen RESEARCH LICENSE（非商用）**。要商用分发，工具不变、**换 Qwen-Image 2.0（Apache-2.0）**；这一步建议先问用户，而不是先跑训练。

## 6. 未验清单

- DiffSynth `train.py` 的**确切参数名**（本次只取到脚本路径与目录结构，未取回 argparse）⇒ 跑前必须 `--help`。
- DiffSynth 在 **Windows + 4080S 32GB** 上的实测显存/步时；`--enable_model_cpu_offload` 是否够用。
- ai-toolkit `use_comfy_weights: false` 是否真能让本地 diffusers 分片目录通过量化（**推论来自源码注释，未实测**）；`_mixin.py:451` 的 `self.to(device)` 是否就是报错点（维护者 的 traceback 指向这里，未复现）。
- OneTrainer / Diffusion-Pipe / HF diffusers examples 对 2.1 的支持与许可证（0 命中，非否定）。
- 本机无 `DiffSynth-Studio` 克隆（`E:\视频\modelsmith\third_party` 只有 ai-toolkit / laya / LlamaFactory / sd-scripts / LICENSES）。
- 网页来源：DiffSynth README、`examples/`、`examples/qwen_image_21/`、`model_training/`、LICENSE 五页（A 源码）；Qwen-Image-2.1 许可证**以本机 LICENSE 文件为准**（A），未依赖网页。
