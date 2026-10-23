# Z-Image / Z-Image-Turbo LoRA 配方（DiffSynth-Studio）

> 入口：`third_party\DiffSynth-Studio\examples\z_image\model_training\train.py`（8,812 B，Apache-2.0，只读）
> 适配器：`adapters\diffsynth_adapter.py` 的 `arch="z_image"` 分支；上游 LoRA 配方 `...\model_training\lora\Z-Image-Turbo.sh`。
> 本机权重（2026-10-22 已就位，33/33 SHA256 与上游一致）：`models\z-image-turbo\` **30.64 GB / 33 文件**。

## 0. 权重与 dtype（A 级，源码+header）

```
%MODEL_DIR% = E:\视频\modelsmith\models\z-image-turbo
  transformer\diffusion_pytorch_model-00001..3.safetensors   # 3 片，**F32 存储 24.6 GB**
  text_encoder\model-00001..3.safetensors                    # 3 片，BF16（约 7.7 GB）
  vae\diffusion_pytorch_model.safetensors                    # 1 文件，BF16（0.16 GB）
  tokenizer\   scheduler\   model_index.json
```

- **F32 → bf16 的 cast 在上游完成，不需要另做 bf16 副本**：
  - `examples\z_image\model_training\train.py:33`：`ZImagePipeline.from_pretrained(torch_dtype=torch.bfloat16, ...)`；
  - `diffsynth\core\loader\file.py:31-32`：`if torch_dtype is not None: state_dict[k] = state_dict[k].to(torch_dtype)` ⇒ 权重**读进来就转 bf16**，上卡是 ~12.3 GB，不是 24.6 GB。
  - 注意：**加载期 CPU 侧仍有 ~24.6 GB 的 F32 读入峰值**（逐片），32 GB 内存的机器要留意。
- Base 变体另外下在 `models\z-image\`（**只有 transformer，BF16 12.31 GB**，5 文件）；按 `Z-Image.sh` 配方它复用 Turbo 的 text_encoder/vae。

## 1. 适配器命令（Turbo，可直接复制）

```powershell
$py = 'E:\ComfyUI\python_embeded\python.exe'
& $py -c "import sys; sys.path.insert(0,r'E:\视频\modelsmith\adapters'); import diffsynth_adapter as A; p={'arch':'z_image','model':r'%MODEL_DIR%','dataset':r'%DATA%','out':r'%OUT%','lora_rank':32,'num_epochs':5,'learning_rate':1e-4,'dataset_repeat':50,'height':1024,'width':1024,'use_gradient_checkpointing':True,'enable_model_cpu_offload':True,'sample_every':200,'sample_prompts':['a red apple on a wooden table, studio light']}; r=A.run({'id':'zimage-lora','params':p}, r'%OUT%\run.log'); print(r['state'], r['metrics']['arch'], r['metrics']['artifacts'], r['metrics']['sample_events'])"
```

## 1.5 步数换算（适配器）

DiffSynth 没有"总步数"参数：总步数 = 图数 × `dataset_repeat` × `num_epochs`。UI 发 `steps` 时适配器换算成 **`num_epochs=1` + `dataset_repeat=ceil(steps/图数)`**；日志 `# steps: requested=300 -> epochs=1 repeat=50 effective=300`，metrics 有 `requested_steps/effective_steps/steps_from_repeat`，不整除时 `steps_overshoot` 警告。不传 `steps` 时 epochs/repeat 各自生效。

## 2. 混合目录（Base transformer + Turbo TE/VAE）

`--model_paths` 是 **3 个独立元素**（`parsers.py:26`、`training_module.py:242-249`），每段各自解析，所以可以跨目录：

```json
[
  ["<Base>\transformer\diffusion_pytorch_model-00001-of-00005.safetensors", "...", "..."],
  ["<Turbo>\text_encoder\model-00001-of-00003.safetensors", "...", "..."],
  "<Turbo>\vae\diffusion_pytorch_model.safetensors"
]
```

适配器里直接传 `model_paths`（并同时给 `tokenizer_path`，仍必须本地）即可；`model` 目录只是“三段同目录”的快捷方式。

## 3. 参数表

| 适配器 param | 默认 | 上游 flag | 说明 |
|---|---|---|---|
| `arch` | `qwen_image_21` | — | **必须写 `z_image`** |
| `model` / `model_paths` | 必需 | `--model_paths`（JSON） | 三段：transformer 分片 list / TE 分片 list / VAE 单文件串 |
| `dataset` | 必需 | `--dataset_base_path` | `metadata.csv` 列 `image,prompt`；**没有会自动生成**到 `<out>\dataset_metadata.csv`（同名 `.txt` 优先，否则 `a photo of <stem>`） |
| `out` | 必需 | `--output_path` | |
| `height` / `width` | 512 | `--max_pixels` = 对齐后 h×w | 16 的倍数（`train.py:126-127`），适配器向上取整并记 `size_aligned_from` |
| `lora_base_model` | `dit` | `--lora_base_model` | 固定 |
| `lora_target_modules` | `to_q,to_k,to_v,to_out.0,w1,w2,w3` | `--lora_target_modules` | **固定**（官方 `Z-Image-Turbo.sh:14`）；Z-Image DiT 的注意力 4 个 + SwiGLU MLP 3 个（w1/w2/w3），不要写成空串（空串=自动探测，会挂更多层） |
| `lora_rank` | **32** | `--lora_rank` | 官方 32 |
| `num_epochs` | 1 | `--num_epochs` | 官方示例 5 |
| `learning_rate` | 1e-4 | `--learning_rate` | |
| `dataset_repeat` | 1 | `--dataset_repeat` | 官方示例 50 |
| `tokenizer_path` | `<model>\tokenizer` | `--tokenizer_path` | 缺 → ValueError（不回落 hub 下载） |
| `use_gradient_checkpointing` | true | `--use_gradient_checkpointing` | 官方必开 |
| `enable_model_cpu_offload` | **false（默认关）** | `--enable_model_cpu_offload` | 实测**开了会崩**：`OffloadTrainingManager` 把顶层 `x_pad_token` 清成空张量 → `z_image.py:606` `shape mismatch [0] vs [0,3840]`。12B 级 DiT 在 32 GB 卡上不开 offload 也能跑（bf16 上卡 ~12.3 GB + TE 7.7 GB）；OOM 时先降 `max_pixels` |
| `save_steps` | 不传 | `--save_steps` | **`0` = 不按 step 存档（只出 epoch 档）**；`>0` 才传 flag，传了**不出** `epoch-*.safetensors`（`runner.py:113-114`） |
| `sample_every` / `sample_prompts` | 200 / [] | —（进程内钩子） | 图落 `<out>\samples\step-<N>\*.png`，见 Qwen 配方 §9 |

## 4. 16 倍数

`examples\z_image\model_training\train.py:126-127`：`height_division_factor=16, width_division_factor=16`。
适配器把 `height/width` **向上取整到 16 的倍数**（`1020→1024`、`1000→1008`），原始值记 `metrics.size_aligned_from`。

## 5. 产物与验收

- 无 `save_steps`：`%OUT%\epoch-{id}.safetensors`
- 有 `save_steps=N`：`%OUT%\step-{n}.safetensors`（**没有 epoch 档**）
- 采样：`%OUT%\samples\step-<N>\*.png` + `metrics.sample_events`（本任务新增，Z-Image 的 pipeline `__call__` 支持 `prompt/cfg_scale/height/width/seed/num_inference_steps/sigma_shift`）
- ComfyUI 导出注意：`--remove_prefix_in_ckpt "pipe.dit."` 只剥 key 前缀，保证 LoRA 键是 DiT 的 `...proj...` 布局；转 ComfyUI 时按 ComfyUI 的 z_image LoRA 命名核对（不要直接拿训练档当 ComfyUI 单文件底模用，PITFALLS C2）。

## 6. 建议与坑

- rank 32 / lr 1e-4 / 5 epochs / repeat 50（官方 `Z-Image-Turbo.sh`）；smoke 用 1 epoch × 4 图。
- `models\z-image-turbo` 的 transformer 是 **F32**（24.6 GB），加载期 CPU 会短暂吃满 ~24.6 GB；别在内存紧张时跑。
- Z-Image 本版训练**不出采样图**（`diffsynth\diffusion\logger.py` 无生成逻辑）——采样图由适配器的进程内钩子产出（task-47），不是上游原生。
- `--initialize_model_on_cpu` 本版**不存在**（parser 只有 `--tokenizer_path` / `--enable_npu_patch`），别传。
- 未验：混合目录（Base transformer + Turbo TE/VAE）的真跑；`--enable_model_cpu_offload` 下采样钩子的稳定性（已知不开都崩，见 §3）。

## 7. 本机真跑读数（A 级，1 臂，2026-10-22）

| 项 | 读数 |
|---|---|
| 参数 | `arch=z_image`、`max_pixels=262144`(512²)、`num_epochs=1`、`dataset_repeat=1`、`lora_rank=32`、`use_gradient_checkpointing=True`、**`enable_model_cpu_offload=False`**、`sample_every=2`、1 prompt、采样 4 步 @256² |
| 结果 | **succeeded / rc=0**，4/4 步（1.06 s/it） |
| 耗时 / 峰值显存 | **31.6 s** / **21,927 MiB / 32,760** |
| 产物 | `epoch-0.safetensors` **158,818,752 B（151.5 MB，476 张量）**；`samples\step-2\sample_00.png` **73,166 B / 256×256 RGB**；`samples\step-4\sample_00.png` **72,582 B / 256×256 RGB** |
| F32→bf16 | 上游 `train.py:33` + `file.py:31-32` 已 cast，上卡 ~12.3 GB；无需 bf16 副本 |
| python_paths | `[]`（ComfyUI transformers 4.57.6 已有 `Qwen3Model`，不需要 .aitk-pylibs） |

