# MOSS-Image 本地目录 → DiffSynth 训练接线（施工照抄版）

> 依据：`third_party\DiffSynth-Studio\`（commit `baac56f2…`，v2.1.12，2026-10-22）+ 本地权重 `E:\视频\modelsmith\models\moss-image-sft\`。
> 全部为源码 `文件:行号`（A 级）；**未加载模型、未跑 GPU**。行号以当前工作树为准。

## 0. 本地权重结构（实测，字节来自 MANIFEST）

`transformer/` 3 片（4,991,978,016 + 4,983,137,016 + 189,549,184 = **10.17 GB**）＋ index.json；
`text_encoder/` 4 片（**16.64 GB**）＋ index.json；`vae/diffusion_pytorch_model.safetensors` **0.25 GB**；
`tokenizer/`（`tokenizer.json` / `tokenizer_config.json` / `chat_template.jinja`）；`scheduler/`；`model_index.json`；根目录 `pipeline_moss_image.py`。

**⇐ 关键**：这三段（dit / te / vae）合计 **~27 GB bf16**，32 GB 卡全驻后几乎没空间放激活 ⇒ 见 §4。

## 1. `--model_paths` 的确切形状（最重要）

- 类型是 **JSON 字符串**：`parsers.py:26`（“In JSON format”）；`training_module.py:242` 直接 `json.loads(model_paths)`。
- 解析后**每个元素 = 一个 ModelConfig**（`training_module.py:243-249`）；`ModelConfig.path` 允许 **`str` 或 `list[str]`**（`core\loader\config.py:45`）。
- **分片必须打包进同一个元素（list），不能拆成 3 个元素**：
  - 模型类型靠 **state-dict key 的 md5** 识别：`core\loader\file.py:147-151`（`hash_model_file`），list 会先合并 key（`file.py:96-100`）；
  - 注册表里 MOSS 的 dit 只有一个 hash `b2e1de16…`、te `a7140be4…`、vae `dc89b5ba…`（`configs\model_configs.py:608-631`）；
  - 单个分片各自 hash 对不上 ⇒ 启动即 `Cannot detect the model type. File: …`（`models\model_loader.py:100-101`）。
- **不能传目录**：目录名不以 `.safetensors` 结尾 ⇒ 走 `load_keys_dict_from_bin(dir)`（`file.py:101-104`）⇒ `torch.load(目录)` 必崩。
- 元素顺序不影响识别（按 hash 自动匹配），但官方顺序是 dit → te → vae（`MOSS-Image-SFT.sh:8`）。
- `*.index.json` **不用给**（loader 直接读分片，`file.py:6-9`）。

**给 `expand_model_paths()` 的可抄 JSON（把三段各自包成 list）**：

```json
[
  ["E:\\视频\\modelsmith\\models\\moss-image-sft\\transformer\\diffusion_pytorch_model-00001-of-00003.safetensors",
   "E:\\视频\\modelsmith\\models\\moss-image-sft\\transformer\\diffusion_pytorch_model-00002-of-00003.safetensors",
   "E:\\视频\\modelsmith\\models\\moss-image-sft\\transformer\\diffusion_pytorch_model-00003-of-00003.safetensors"],
  ["E:\\视频\\modelsmith\\models\\moss-image-sft\\text_encoder\\model-00001-of-00004.safetensors",
   "E:\\视频\\modelsmith\\models\\moss-image-sft\\text_encoder\\model-00002-of-00004.safetensors",
   "E:\\视频\\modelsmith\\models\\moss-image-sft\\text_encoder\\model-00003-of-00004.safetensors",
   "E:\\视频\\modelsmith\\models\\moss-image-sft\\text_encoder\\model-00004-of-00004.safetensors"],
  "E:\\视频\\modelsmith\\models\\moss-image-sft\\vae\\diffusion_pytorch_model.safetensors"
]
```

> 现有适配器“transformer 分片 + text_encoder 分片 + vae 单文件”三段展开**结构正确**，唯一要求：**段内必须是 list，段间必须是三个独立元素**。少一片 → hash 变 → `Cannot detect`（判红句同上）。

## 2. tokenizer 怎么传

- `train.py:30`：`tokenizer_path is None` 时用 `ModelConfig(model_id="openmoss/MOSS-Image-Base", origin_file_pattern="tokenizer/")`（**会联网下载 Base 的 tokenizer**）；给了就用 `ModelConfig(tokenizer_path)`。
- 本地走 **`--tokenizer_path "E:\视频\modelsmith\models\moss-image-sft\tokenizer"`**（**目录**）：`moss_image.py:53-55` 随后 `AutoTokenizer.from_pretrained(tokenizer_config.path)`，吃目录。
- **不要塞进 `--model_paths`**：tokenizer 不在 `MODEL_CONFIGS` 注册表里，会被 §1 的 hash 检测拒掉。
- `path` 非空时不会触发下载（`config.py:120-121,157-159`）。

## 3. flag 逐个核（本版 2.1.12）

| flag | 定义/使用 | MOSS 上是否支持 | 结论 |
|---|---|---|---|
| `--lora_base_model "dit"` | `parsers.py:55`；`training_module.py:392-402`（`getattr(pipe, 名字)`） | ✅ MOSS 管线有 `self.dit`（`moss_image.py:24`） | **必须给**，官方值 `dit`（`MOSS-Image-SFT.sh:13`） |
| `--lora_target_modules ""` | `parsers.py:56`；`training_module.py:348-350`：空串 ⇒ `auto_detect_lora_target_modules` | ✅ 通用 | 照抄官方**空串**（`SFT.sh:14`），别写死 `q,k,v` |
| `--remove_prefix_in_ckpt "pipe.dit."` | `parsers.py:50`（默认值就是它）；`logger.py:129` 导出时 `remove_prefix` | ✅ | 保持默认 `pipe.dit.` |
| `--fp8_models` | `parsers.py:29` → `training_module.py:237,245-246` → `parse_vram_config(fp8=True)` = **权重按 `float8_e4m3fn` 存/载**（`training_module.py:153-164`） | ✅ 省显存（TE 16.6→8.3 GB） | 32 GB 上**建议对 text_encoder 开** |
| `--offload_models` | `parsers.py:30` → `parse_vram_config(offload=True)` = **磁盘 offload + clear_parameters**（`training_module.py:165-176`） | ⚠️ 注释写明「Only used in splited training」 | 不用；要省显存走 cpu_offload |
| `--initialize_model_on_cpu` | — | ❌ **本版 0 命中**（全仓库 grep） | **禁止传**（argparse 直接报错） |
| `--enable_model_cpu_offload` | `parsers.py:75`；`train.py:128`（`device="cpu"`）→ 层卸载训练 | ✅ 通用（慢） | 仅在 OOM 时用 |
| `--use_gradient_checkpointing` | `parsers.py:64`（`store_true`，**默认关**）；`train.py:119` | ✅ | **必须开**（官方 `SFT.sh:16`） |
| `--use_gradient_checkpointing_offload` | `parsers.py:65` | ✅ | 可选，显存再紧时 |
| `--find_unused_parameters` | `parsers.py:42`；`train.py:92` | ✅ | 官方配方开（`SFT.sh:18`） |

## 4. 显存与分辨率约束

- **16 的倍数**：`train.py:104-105`（`height_division_factor=16, width_division_factor=16`）+ 管线自身 `moss_image.py:20-21`。给 `--height/--width` 也要 16 的倍数。
- `--height/--width` 默认 `None` ⇒ **动态分辨率**（`parsers.py:13-14`）。
- `--max_pixels` = **每帧像素上限**，默认 `1024*1024`（`parsers.py:15`）；实际作用在 `ImageCropAndResize(..., max_pixels, 16, 16)`（`core\data\unified_dataset.py:36`）。
- 显存账：dit 10.17 + te 16.64 + vae 0.25 = **27.06 GB**（bf16 全驻），32 GB 卡只剩 ~4 GB 给激活/优化器 ⇒ 全量必 OOM。
- **32 GB 安全 smoke**（1 张卡、2 张图、1 epoch、2 步）：
  `--max_pixels 262144`（512×512）、`--lora_rank 16`、`--lora_target_modules ""`、`--use_gradient_checkpointing`、`--fp8_models "<te 那段的 JSON list>"`；仍 OOM 再加 `--enable_model_cpu_offload`。

## 5. 产物命名与采样

- **epoch 末**：`{output_path}\epoch-{epoch_id}.safetensors`（`logger.py:116-117`）。
- 给了 `--save_steps`：`{output_path}\step-{n}.safetensors`，并在训练结束时补最后一步（`logger.py:113-114,119-121`）。
- `--remove_prefix_in_ckpt` **只影响权重 key 前缀**（`logger.py:129`），**不影响文件名**。
- **本版不产采样图**：`logger.py` 全文只有 logger 与 `save_model`（`logger.py:104-133`，无任何 generate/sample）；`train.py:145` 行内也没有 `--sample*` 参数。要图/要验收，走单独的 `examples\moss_image\model_training\validate_lora\MOSS-Image-SFT.py`。

## 6. 风险清单（每条带判红）

| # | 风险 | 怎么判红 |
|---|---|---|
| R1 | `--model_paths` 把分片拆成 3 个元素，或传了目录 | 启动即 `ValueError: Cannot detect the model type. File: …`（`model_loader.py:100-101`） |
| R2 | 忘给 `--tokenizer_path` ⇒ 去下 `openmoss/MOSS-Image-Base:tokenizer/` | 日志出现 download tips / 卡在 `snapshot_download`，迟迟没有 `Loading models from:`；离线直接超时 |
| R3 | transformers 版本不认 `Qwen2Tokenizer`（本地 `model_index.json` 的 tokenizer 类） | `AutoTokenizer.from_pretrained` 抛 `ValueError: … is not a valid … class`（`moss_image.py:55`） |
| R4 | 32 GB 全驻 27 GB → OOM | `torch.OutOfMemoryError`，栈在 `MOSSImageUnit_PromptEmbedder`/`model_fn_moss_image`；先加 `--fp8_models`，再 `--enable_model_cpu_offload` |
| R5 | `--num_epochs > 1` 中途中断 ⇒ 一个文件都没有（只有 epoch 末存） | `output_path` 为空目录 / 只有 loggers 目录；smoke 用 `--num_epochs 1` + 极小数据集 |
| R6 | 把 `--task` 写成 `sft:train`（两段式）却没先跑 `sft:data_process` | `split_pipeline_units` 走 `:train` 分支（`training_module.py:443-444`）后报 units/参数缺失；**默认 `sft`（`parsers.py:44`）即可** |

## 7. 照抄的 1 步 smoke 命令

```powershell
E:\ComfyUI\python_embeded\python.exe examples\moss_image\model_training\train.py `
  --dataset_base_path   "E:\视频\modelsmith\datasets\moss_smoke" `
  --dataset_metadata_path "E:\视频\modelsmith\datasets\moss_smoke\metadata.csv" `
  --max_pixels 262144 --num_epochs 1 --dataset_repeat 1 --dataset_num_workers 0 `
  --model_paths '[["<dit-00001>","<dit-00002>","<dit-00003>"],["<te-00001>","<te-00002>","<te-00003>","<te-00004>"],"<vae>"]' `
  --tokenizer_path "E:\视频\modelsmith\models\moss-image-sft\tokenizer" `
  --lora_base_model dit --lora_target_modules "" --lora_rank 16 `
  --remove_prefix_in_ckpt "pipe.dit." --learning_rate 1e-4 `
  --use_gradient_checkpointing --output_path "E:\视频\modelsmith\_runs\moss-lora-smoke"
```

- 数据：`metadata.csv` 列名 **`image,prompt`**（`train.py:55,58` 读 `data["prompt"]` / `data["image"]`；`data_file_keys` 默认 `image,video`，`parsers.py:9`），`image` 写**相对 `dataset_base_path` 的路径**（`unified_dataset.py:36` 的 `ToAbsolutePath(base_path)`）。
- 产物：`_runs\moss-lora-smoke\epoch-0.safetensors`（`num_epochs 1`）。
- 本命令**未执行**（只读侦察）；`--fp8_models` 可按 §4 追加。
- **未验（实测才算数）**：fp8 对 TE 训练是否真省显存且不炸（代码路径在 `training_module.py:153-164`，本机未跑）；`max_pixels 262144` 下 MOSS 的实际步时；`Qwen2Tokenizer` 在本机 transformers 版本是否可直接 `from_pretrained`（本机 `E:\ComfyUI\python_embeded` 的 transformers 版本未查）。
