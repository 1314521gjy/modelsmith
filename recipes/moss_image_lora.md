# MOSS-Image LoRA 配方（DiffSynth-Studio）

> 入口：`third_party\DiffSynth-Studio\examples\moss_image\model_training\train.py`（145 行，Apache-2.0，只读）
> 适配器：`adapters\diffsynth_adapter.py` 的 `arch="moss_image"` 分支；本机**尚无完整权重**（2026-10-22 下载中）。
> 上游 LoRA 参数来源：`examples\moss_image\model_training\lora\MOSS-Image-SFT.sh`（不猜，逐行核对）。

## 0. 权重（两种形态）

### A. 本地 diffusers 目录（适配器用这种）

```
%MODEL_DIR% = E:\视频\modelsmith\models\moss-image-sft
  transformer\diffusion_pytorch_model-*.safetensors   (分片)
  text_encoder\model-*.safetensors                    (分片)
  vae\diffusion_pytorch_model.safetensors
  tokenizer\                                          (必需，本地)
```

- 权重规模：**25.36 GB / 37 文件**（维护者 派专席下载到上述目录）；本机 E 盘余量 554.7 GB。
- **tokenizer 必须本地**：MOSS `train.py:30` 在 `--tokenizer_path` 为空时会去 hub 拉
  `openmoss/MOSS-Image-Base/tokenizer/`；适配器**拒绝**这个回落（缺 tokenizer 直接 ValueError），避免意外下载。

### B. 官方 hub 形态（手工命令行，仅参考）

```bash
accelerate launch examples/moss_image/model_training/train.py \
  --dataset_base_path <data> --dataset_metadata_path <data>/metadata.csv \
  --max_pixels 1048576 --dataset_repeat 50 \
  --model_id_with_origin_paths "openmoss/MOSS-Image-SFT:transformer/diffusion_pytorch_model*.safetensors,openmoss/MOSS-Image-SFT:text_encoder/model*.safetensors,openmoss/MOSS-Image-SFT:vae/diffusion_pytorch_model.safetensors" \
  --learning_rate 1e-4 --num_epochs 5 \
  --remove_prefix_in_ckpt "pipe.dit." --output_path "./models/train/MOSS-Image-SFT_lora" \
  --lora_base_model "dit" --lora_target_modules "" --lora_rank 32 \
  --use_gradient_checkpointing --dataset_num_workers 8 --find_unused_parameters
```

> 这是上游原样命令（`MOSS-Image-SFT.sh:3-18`）；适配器走 A 形态时把 `--model_id_with_origin_paths`
> 换成 `--model_paths` + `--tokenizer_path`，其余字段等价。

## 1. 适配器命令（可直接复制）

```powershell
$py = 'E:\ComfyUI\python_embeded\python.exe'
& $py -c "import sys; sys.path.insert(0,r'E:\视频\modelsmith\adapters'); import diffsynth_adapter as A; p={'arch':'moss_image','model':r'%MODEL_DIR%','dataset':r'%DATA%','out':r'%OUT%','lora_rank':32,'num_epochs':5,'learning_rate':1e-4,'dataset_repeat':50,'height':1024,'width':1024,'use_gradient_checkpointing':True,'enable_model_cpu_offload':True}; r=A.run({'id':'moss-lora','params':p}, r'%OUT%\run.log'); print(r['state'], r['metrics']['arch'], r['metrics']['size_aligned_from'], r['metrics']['artifacts'])"
```

## 1.5 步数换算（适配器）

DiffSynth 没有"总步数"参数：总步数 = 图数 × `dataset_repeat` × `num_epochs`。UI 发 `steps` 时适配器换算成 **`num_epochs=1` + `dataset_repeat=ceil(steps/图数)`**；日志 `# steps: requested=300 -> epochs=1 repeat=50 effective=300`，metrics 有 `requested_steps/effective_steps/steps_from_repeat`，不整除时 `steps_overshoot` 警告。不传 `steps` 时 epochs/repeat 各自生效。

## 2. 参数表（适配器 → 上游 flag）

| 适配器 param | 默认 | 上游 flag | 说明 |
|---|---|---|---|
| `arch` | `qwen_image_21` | — | **必须写 `moss_image`**；未知 arch 直接 ValueError |
| `model` | 必需 | `--model_paths`（JSON） | 本地上面的目录，自动展开三组 |
| `dataset` | 必需 | `--dataset_base_path` | 含 `metadata.csv`（`image,prompt`）；**没有会自动生成**到 `<out>\dataset_metadata.csv`（同名 `.txt` 优先，否则 `a photo of <stem>`） |
| `out` | 必需 | `--output_path` | 产物目录 |
| `height` / `width` | 512 | `--max_pixels` = 对齐后 h×w | **向上对齐到 16 的倍数**；变化记 `size_aligned_from` |
| `lora_rank` | **32**（MOSS 默认） | `--lora_rank` | 官方配方 32 |
| `num_epochs` | 1 | `--num_epochs` | 官方示例 5 |
| `learning_rate` | 1e-4 | `--learning_rate` | |
| `dataset_repeat` | 1 | `--dataset_repeat` | 官方示例 50 |
| `tokenizer_path` | `<model>\tokenizer` | `--tokenizer_path` | 缺 → ValueError（不回落 hub） |
| `use_gradient_checkpointing` | true | `--use_gradient_checkpointing` | |
| `enable_model_cpu_offload` | true | `--enable_model_cpu_offload` | 同时让上游把模型先建在 CPU（`train.py:128`） |
| `dataset_num_workers` | 不传 | `--dataset_num_workers` | |
| `find_unused_parameters` | false | `--find_unused_parameters` | |
| `save_steps` | 不传 | `--save_steps` | **`0` = 不按 step 存档（只出 epoch 档）**；`>0` 才传 flag，传了就**不出 `epoch-*.safetensors`**（`runner.py:113-114`） |
| `sample_every` / `sample_prompts` | 200 / [] | —（进程内钩子） | 同 Qwen 配方 §9，图落 `<out>\samples\step-<N>` |

固定 LoRA 接线（适配器强制，不读 params）：`--lora_base_model dit`、`--lora_target_modules ""`、`--remove_prefix_in_ckpt "pipe.dit."`。

## 3. 16 倍数（已自验，不是外部口径）

`examples\moss_image\model_training\train.py:104-105`：

```python
height_division_factor=16,
width_division_factor=16,
```

⇒ MOSS-Image 的图/潜空间按 **16** 对齐。适配器对 `height/width` **向上取整到 16 的倍数**（`1020→1024`、`1000→1008`），
并把原始值写进 `metrics.size_aligned_from = {"width":1000,"height":1020}`（未变更时为 `null`）。

## 4. 产物与建议

- 无 `save_steps`：`%OUT%\epoch-0.safetensors`（终版 LoRA）
- 有 `save_steps=N`：`%OUT%\step-N.safetensors`（**没有** epoch 档，见坑 1）
- 带 `sample_prompts`：`%OUT%\samples\step-<N>\*.png` + `metrics.sample_events`

建议（沿用上游配方）：rank 32、lr 1e-4、5 epochs、repeat 50、`use_gradient_checkpointing`；先用 1 epoch×4 图做 smoke。

## 5. 坑

1. `--save_steps` 会抑制 `epoch-*.safetensors`（上游 `runner.py:113-114` 只在 `save_steps is None` 时调 `on_epoch_end`）。
2. `resume_from_checkpoint` 只恢复权重，不恢复步号/optimizer/数据位置（`training_module.py:466-475`）⇒ 没有"续训到第 N 步"。
3. MOSS 的 parser **没有** `--initialize_model_on_cpu` / `--processor_path`（与 Qwen 的 train.py 不同）；适配器已按 arch 分支处理，手工命令行时别照抄 Qwen。
4. tokenizer 本地缺失会被适配器拒绝；只补 `tokenizer\` 子目录即可，不必重下全量权重。
5. 显存：**未验**（权重已到 37 文件 / 25.36 GB，但真跑尚未通过 VAE 环节）。MOSS-Image 是 5B DiT + 大 TE，32 GB 卡请开 CPU offload/梯度检查点并关闭其它 GPU 程序；第一次真跑要记录峰值。

## 6. 环境与已知阻断（2026-10-22 实测，真跑两臂）

- **transformers 必须 ≥5.x**：`diffsynth\models\moss_image_text_encoder.py:3` 要 `Qwen3_5TextConfig / Qwen3_5TextModel`，ComfyUI 自带 4.57.6 **没有**
  （`ImportError: cannot import name 'Qwen3_5TextConfig'`）。修法：适配器 `python_paths=[r"E:\视频\modelsmith\.aitk-pylibs"]`
  （transformers 5.5.3，**不带 torch**，torch 仍回落 ComfyUI 2.12.0+cu130）。runner 会把这些路径插到 `sys.path` 最前。
- **MOSS `train.py` 没有 `--initialize_model_on_cpu`**：它的 parser 只有 `add_general_config` + `add_image_size_config` + `--tokenizer_path`；
  手写命令行时**不要**照抄 Qwen 的 flag（会 argparse 直接退出）。CPU 初始化由 `--enable_model_cpu_offload` 隐含（`train.py:128`）。
- **VAE encode 曾阻断、已定位并绕过（A 级）**：带 `--enable_model_cpu_offload` 时崩在
  `diffsynth\models\flux2_vae.py:326`（`RuntimeError: The size of tensor a (128) must match the size of tensor b (0)`，VAE `bn` 统计量为空）。
  **去掉 `--enable_model_cpu_offload` 后同一组参数直接跑通**（4/4 步、峰值 26,869 MiB）。⇒ **MOSS 不要开 CPU offload**；
  27 GB 权重全驻在 32 GB 卡上可行（见 §7）。

## 7. 本机真跑读数（A 级，1 臂，2026-10-22）

| 项 | 读数 |
|---|---|
| 参数 | `arch=moss_image`、`max_pixels=262144`(512²)、`num_epochs=1`、`dataset_repeat=1`、`lora_rank=16`、`use_gradient_checkpointing=True`、**`enable_model_cpu_offload=False`**、`python_paths=[.aitk-pylibs]` |
| 结果 | **succeeded / rc=0**，4/4 步（1.49 s/it） |
| 耗时 | **43.0 s**（加载+训练+存档） |
| 峰值显存 | **26,869 MiB / 32,760** |
| 产物 | `epoch-0.safetensors`，**77,719,832 B（77.72 MB）** |
| LoRA 目标 | 自动探测：`text_embedder.blocks.{0,1}.*` + `layers.0-41.proj.{q,k,v,o,ffn_up_gate,ffn_down}` |
| 采样 | 本版 MOSS 训练不出采样图（与 Qwen 不同）；验收走 `validate_lora\MOSS-Image-SFT.py` |


