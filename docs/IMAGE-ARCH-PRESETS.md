# 生图 LoRA 架构预设（UI / MCP / 核心共用）

> 唯一事实来源：`adapters\diffsynth_adapter.py` 的 `ARCHES` 与三次 A 级真跑（2026-10-22）。
> 本表给 `kind="diffsynth"` 的 `params.arch` 用；所有 arch 共用 `adapters\diffsynth_adapter.py`。
> GPU：RTX 4080 SUPER 32 GB（32,760 MiB）。**不再猜**：下面 flag 均来自上游 `train.py`/`lora\*.sh` 与实测。

## 1. 三 arch 对照表

| 项 | `qwen_image_21`（默认） | `moss_image` | `z_image` |
|---|---|---|---|
| DiffSynth `train.py` | `examples\qwen_image_21\model_training\train.py` | `examples\moss_image\model_training\train.py` | `examples\z_image\model_training\train.py` |
| `--model_paths` 组数 | 3（transformer 分片 list / TE 分片 list / vae 单文件串） | 3（同左） | 3（同左） |
| `--lora_base_model` | `dit` | `dit` | `dit` |
| `--lora_target_modules` | `""`（自动探测） | `""`（自动探测） | `to_q,to_k,to_v,to_out.0,w1,w2,w3`（固定 7 个） |
| `--remove_prefix_in_ckpt` | `pipe.dit.` | `pipe.dit.` | `pipe.dit.` |
| tokenizer 传法 | **不需要** `--tokenizer_path` | `--tokenizer_path <model>\tokenizer`（必需，本地） | 同左（必需，本地） |
| `--processor_path` | **必需**（`<model>\processor`） | 不支持（传了 argparse 报错） | 不支持（同左） |
| 分辨率倍数 | 未强制对齐（默认 512；Qwen VAE 为 f8） | **16**（`moss train.py:104-105`）±对齐 | **16**（`z train.py:126-127`） |
| 尺寸参数 | `--height/--width` | `--max_pixels = h*w` | `--max_pixels = h*w` |
| 默认 rank | 8（可覆盖） | 32 | 32 |
| 额外环境 | — | `python_paths=[.aitk-pylibs]`（transformers 5.x） | — |
| 上游出采样图 | 否 | 否 | 否 |
| 适配器采样钩子 | ✅ 已实测（step-2/4） | 通用钩子未实测 | ✅ 已实测（step-2/4） |

> `arch` 未知时适配器 `ValueError` 点名（`known archs: moss_image, qwen_image_21, z_image`），不会猜路径。

## 2. 32 GB 卡「安全 smoke 参数」（三次实测同款）

| arch | max_pixels | rank | epochs | dataset_repeat | grad ckpt | CPU offload | python_paths | 实测读数 |
|---|---|---|---|---|---|---|---|---|
| `qwen_image_21` | 262144（512²） | 8 | 1 | 1 | ✅ | **开（true）** | `[]` | **succeeded / 63.1 s / 峰值 31,945 MiB**；step-2 103,862 B、step-4 104,188 B（PNG 256×256 RGBA） |
| `moss_image` | 262144（512²） | 16 | 1 | 1 | ✅ | **关（false，必须）** | `[.aitk-pylibs]` | **succeeded / 43.0 s / 峰值 26,869 MiB**；`epoch-0.safetensors` 77,719,832 B |
| `z_image` | 262144（512²） | 32 | 1 | 1 | ✅ | **关（false，必须）** | `[]` | **succeeded / 31.6 s / 峰值 21,927 MiB**；`epoch-0.safetensors` 158,818,752 B；step-2 73,166 B、step-4 72,582 B（256×256 RGB） |

采样建议：`sample_every=200`、`sample_prompts` 1–4 条、`sample_steps=4–20`、`sample_width/height=256`（smoke 用 2/4 步即可）。
`sample_every>0` 但 `sample_prompts` 为空 ⇒ 适配器**警告并自动关采样**（不静默）。

## 3. 坑表（每条带「怎么判红」）

| # | 坑 | 怎么判红 |
|---|---|---|
| 1 | **MOSS 开 `--enable_model_cpu_offload`**：`OffloadTrainingManager` 清空 VAE `bn` 统计量 | `RuntimeError: The size of tensor a (128) must match the size of tensor b (0)` @ `diffsynth\models\flux2_vae.py:326` |
| 2 | **Z-Image 开 `--enable_model_cpu_offload`**：顶层 pad token 被清空 | `RuntimeError: shape mismatch: value tensor of shape [0] cannot be broadcast to indexing result of shape [0, 3840]` @ `diffsynth\pipelines\z_image.py:606` |
| 3 | **MOSS 需要 transformers ≥5.x**（`Qwen3_5TextConfig`） | `ImportError: cannot import name 'Qwen3_5TextConfig'`（ComfyUI 自带 4.57.6）；修法 `python_paths=[E:\视频\modelsmith\.aitk-pylibs]` |
| 4 | **Z-Image transformer 是 F32 24.6 GB**：加载期 CPU 峰值 ~24.6 GB（上卡会 cast bf16 ~12.3 GB） | 加载阶段系统内存吃满/换页、进程被 kill；`train.py:33 torch_dtype=bfloat16` + `file.py:31-32` 读入即 cast |
| 5 | `--initialize_model_on_cpu` 本版 **不存在**（moss/z_image 都会 argparse 报错） | `error: unrecognized arguments: --initialize_model_on_cpu` |
| 6 | `--processor_path` **只适用 qwen** | moss/z_image：`error: unrecognized arguments: --processor_path` |
| 7 | 给了 `--save_steps` 就**不出 `epoch-*.safetensors`** | 产物目录只有 `step-*.safetensors`（`runner.py:113-114`） |
| 8 | `resume_from_checkpoint` 只恢复权重（无步号/optimizer/数据位置） | 续训从 epoch 0 重跑、`step-*.safetensors` 重名覆盖（`training_module.py:466-475`） |
| 9 | tokenizer 未本地传 ⇒ 上游偷偷下载 base tokenizer | 日志卡在 `snapshot_download` / 出现 download tips；适配器已直接 ValueError（不回落） |
| 10 | 采样后不恢复状态 ⇒ 下一步训练崩 | 改造钩子时：`scheduler.training` 未恢复 ⇒ `KeyError: 'use_kv_cache'`；device 未复原 ⇒ `Expected all tensors to be on the same device` |
| 11 | **调用方传错 `train.py`**（例如核心曾固定传 qwen 路径） | 适配器已忽略并 WARNING；日志里 `# diffsynth adapter train_py:` 不是本 arch 的路径 ⇒ 回归（桩 `test_launch_command` + `train_py_matches_arch` 负控，判红句：`argv[4]` 必须含 `/moss_image/`、`/z_image/`、`/qwen_image_21/`） |

## 4. 新增一种底模（FLUX / HiDream / …）要改哪几处

1. **`adapters\diffsynth_adapter.py`**
   - `ARCHES` 加一项：`train_py`、`default_rank`、`offload_default`、`uses_max_pixels`、`requires_tokenizer`、`align16`、`fixed.{lora_base_model,lora_target_modules,remove_prefix_in_ckpt}`；
   - 若上游 flag 与三类现有分支不同：在 `build_argv` 加分支（**不许给未知 arch 猜 train.py**）；
   - 若 pipeline 的 `__call__` 签名不支持采样参数：采样钩子要单独适配（当前钩子传 `prompt/negative_prompt/cfg_scale/height/width/seed/num_inference_steps`）。
2. **核心路由**（`core\modelsmith_core.py` 的 `kind="diffsynth"`）：arch 透传 + 参数白名单要含新字段（`python_paths`/`sample_*`/`max_pixels`/`tokenizer_path`），否则 UI 传了会被丢。
3. **`/api/models`**（task-46 的模型列表）：加该 arch 的 `class_name`/显示名/默认值，否则 UI 选不到。
4. **`recipes\<model>_lora.md`**：权重布局、必要 flag、安全 smoke 参数、坑（抄本文件 §2/§3）。
5. **`adapters\selftest_diffsynth_adapter.py`**：加 arch fixture + argv 逐字段断言 + 缺 tokenizer/未知 arch 反例；**旧 arch 的 flag 序列回归必须保持**。
6. 跑一次 ≤4 分钟 GPU smoke（4 图、512²、1 epoch）并记录：state/耗时/峰值显存/产物字节；没跑的明写「未验」。
