# LoRA 自测指南（32 GB 单卡 · 用户版）

> 工具：`E:\视频\modelsmith\tools\make_image_dataset.py`、`E:\视频\modelsmith\tools\preview_lora.py`
> 训练适配器：`E:\视频\modelsmith\adapters\diffsynth_adapter.py`；配方见 `recipes\`；架构对照见 `docs\IMAGE-ARCH-PRESETS.md`。

## 0. 路线

准备图片 → 生成数据集 → 训练（带 `sample_every`）→ 看 `samples\step-N\*.png` → 训完 `preview_lora.py` 出终图。

## 1. 数据集（零依赖脚本）

```powershell
python tools\make_image_dataset.py --images D:\my_photos --trigger "mystyle" --out D:\datasets\mystyle --link
```

- 产出：图片（硬链接，失败自动复制）+ 每张同名 `.txt`（源目录有 `.txt` 就沿用，否则 `a photo of <trigger>`）+ `metadata.csv`（`image,prompt`，相对路径 `/`）。
- 校验：只收 jpg/jpeg/png；报告张数、尺寸范围、是否 16 倍数（不是会被 DiffSynth 向上对齐）；空目录/坏路径 **exit 2**。
- 训练规模换算：**步数 = 图片数 × dataset_repeat × num_epochs**（适配器按 DataLoader 步数算，无 `steps` 参数）。

## 2. 32 GB 卡第一次测试推荐（按 speed 排）

| 底模 | arch | 推荐配置 | 预计时长 |
|---|---|---|---|
| **Z-Image-Turbo**（最快，首选） | `z_image` | 512×512（`max_pixels 262144`）、rank **32**（16–32）、300–600 步、`sample_every=50` | **7–15 分钟** |
| MOSS-Image-SFT | `moss_image` | 512²、rank 16–32、300–600 步、`sample_every=50`、**offload 关**、`python_paths=[.aitk-pylibs]` | 约 10–20 分钟 |
| Qwen-Image 2.1 | `qwen_image_21` | 512²、rank 8–16、200–400 步、`sample_every=50`、`--processor_path` 必需 | 20–40 分钟（最慢） |

> 步数示例：100 张图 × `dataset_repeat=1` × `num_epochs=4` = 400 步（`sample_every=50` → 8 组采样图）。

## 3. 训练（Z-Image 示例）

```powershell
$py='E:\ComfyUI\python_embeded\python.exe'
& $py -c "import sys; sys.path.insert(0,r'E:\视频\modelsmith\adapters'); import diffsynth_adapter as A; p={'arch':'z_image','model':r'E:\视频\modelsmith\models\z-image-turbo','dataset':r'D:\datasets\mystyle','out':r'D:\runs\mystyle-z','lora_rank':32,'num_epochs':4,'dataset_repeat':1,'learning_rate':1e-4,'height':512,'width':512,'use_gradient_checkpointing':True,'sample_every':50,'sample_prompts':['mystyle, a red apple on a wooden table, studio light'],'sample_steps':8,'sample_width':512,'sample_height':512}; r=A.run({'id':'mystyle-z','params':p}, r'D:\runs\mystyle-z\run.log'); print(r['state'], r['metrics']['sample_events'])"
```

## 4. 看效果 / 提前中止

- 训练中每 `sample_every` 步出图：`D:\runs\mystyle-z\samples\step-50\sample_00.png`（`metrics.sample_events` 有 step/files/bytes）。
- 不满意可直接停（关进程 / stop 文件），不用等训完；最后一张采样图对应当时的 LoRA。
- 训完产物：`D:\runs\mystyle-z\epoch-3.safetensors`（每个 epoch 末一个，`epoch-0` 起）。

## 5. 训完出终图（preview）

```powershell
python tools\preview_lora.py --arch z_image --model E:\视频\modelsmith\models\z-image-turbo --lora D:\runs\mystyle-z\epoch-3.safetensors --prompt "mystyle, a red apple on a wooden table, studio light" --out D:\runs\mystyle-z\preview.png --steps 8 --guidance 1.0 --seed 42 --height 512 --width 512
```

- 输出 PNG + 一行 JSON `{ok,path,bytes,elapsed_s}`；`--dry-run` 可先看计划（不占 GPU）。
- 本机实测（Z-Image + 4 步 smoke LoRA）：**272,448 B / 512×512 / 46 s / 峰值 22,176 MiB**。
- 三个 arch 的 validate_lora 参数由脚本从上游 `examples\<arch>\model_training\validate_lora\*.py` 读取（seed/steps/guidance 默认值随之）。

## 6. OOM / 太慢 怎么退（按顺序）

1. 关掉其它 GPU 程序（32 GB 卡**没有 batch>1 余量**）；
2. `max_pixels` 262144 → 147456（384²）→ 65536（256²），保持 16 倍数；
3. `lora_rank` 32 → 16 → 8；
4. 减 `dataset_repeat`/`num_epochs`（先看 50 步的图再决定）；
5. `--use_gradient_checkpointing` 必须开；MOSS/Z-Image **不要开** `--enable_model_cpu_offload`（会崩，见坑表）；Qwen 可以开。

## 7. 坑（详见 `docs\IMAGE-ARCH-PRESETS.md` §3）

- MOSS：offload 会清空 VAE `bn` 统计量（`128 vs 0`）；且需要 transformers 5.x → `python_paths=[E:\视频\modelsmith\.aitk-pylibs]`。
- Z-Image：offload 会清空顶层 `x_pad_token`（`[0] vs [0,3840]`）；transformer 是 F32 24.6 GB，加载期 CPU 峰值 ~24.6 GB。
- Qwen：只有它用 `--processor_path`；MOSS/Z 用 `--tokenizer_path`（本地 `tokenizer\`，缺失直接报错，不回落 hub）。
- `--save_steps` 会抑制 `epoch-*.safetensors`；`resume_from_checkpoint` 只恢复权重（不能“续训到第 N 步”）。
