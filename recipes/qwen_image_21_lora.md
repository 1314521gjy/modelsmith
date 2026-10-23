# Qwen-Image 2.1 LoRA —— 1 epoch × 4 图 smoke 配方

> **状态：A 级（本机已跑通）**。训练入口 `E:\视频\modelsmith\third_party\DiffSynth-Studio\examples\qwen_image_21\model_training\train.py`（Apache-2.0，**只读**）。
> 适配器 `E:\视频\modelsmith\adapters\diffsynth_adapter.py`；零 GPU 自测 `E:\视频\modelsmith\adapters\selftest_diffsynth_adapter.py`（**PASS 70 / FAIL 0**）。
> 本配方**不需要安装任何新包**：ComfyUI Python 现成的 torch 2.12.0+cu130 就够。

## 0. 环境与占位符

| 占位符 | 本机实际值 | 说明 |
|---|---|---|
| `%PY%` | `E:\ComfyUI\python_embeded\python.exe` | torch 2.12.0+cu130 / cuda=True |
| `%REPO%` | `E:\视频\modelsmith\third_party\DiffSynth-Studio` | Apache-2.0，只读 |
| `%MODEL_DIR%` | `E:\临时文件夹\Qwen-Image-2.1` | diffusers 目录（31.6 GB，分片） |
| `%DATA%` | `E:\视频\modelsmith\resources\04-生图LoRA-图集\smoke-synthetic` | 4 图 + `metadata.csv` |
| `%OUT%` | `E:\视频\modelsmith\_runs\diffsynth-qwen21-smoke` | 产物目录（不存在会自动建） |

## 1. 数据集（DiffSynth 只认 metadata.csv）

```
%DATA%\metadata.csv      # 两列：image,prompt（image 相对 %DATA%）
%DATA%\smoke_00.png ...  # 4 张 512×512
```

- 同名 `.txt` caption **不是上游读的东西**——`UnifiedDataset` 只读 `--dataset_metadata_path`；但**没有 `metadata.csv` 不会阻塞**：适配器会扫描 `<dataset>` 下 jpg/jpeg/png，按「同名 `.txt` → 空则 `a photo of <stem>`」生成 `<out>\dataset_metadata.csv`（绝对路径 `/` 分隔），日志打 `# generated dataset metadata: <path> (n=…, captions_from=txt=…,default=…)`，**不写用户目录**。
- 每行：`smoke_00.png,a simple synthetic test image ...`。

- **训练步数换算（适配器）**：DiffSynth 没有"总步数"参数，总步数 = 图数 × `dataset_repeat` × `num_epochs`。UI 发来的 `steps` 会被换算成 **`num_epochs=1` + `dataset_repeat=ceil(steps/图数)`**；日志打 `# steps: requested=300 -> epochs=1 repeat=50 effective=300`，metrics 有 `requested_steps/effective_steps/steps_from_repeat`；不整除时 warnings 写 `steps_overshoot: requested=… effective=…`（不静默）。不传 `steps` 时按 epochs/repeat 各自生效。

## 2. 模型路径必须是「分片列表」，不是目录

`--model_paths` 是 **JSON 字符串**，解出来是 3 项：

```json
[
  ["%MODEL_DIR%/transformer/diffusion_pytorch_model-00001-of-00002.safetensors",
   "%MODEL_DIR%/transformer/diffusion_pytorch_model-00002-of-00002.safetensors"],
  ["%MODEL_DIR%/text_encoder/model-00001-of-00004.safetensors",
   "... 00002 ... 00003 ... 00004 ..."],
  "%MODEL_DIR%/vae/diffusion_pytorch_model.safetensors"
]
```

- 实测分片数：transformers **2**、text_encoder **4**、vae **1**（vae 单文件用**字符串**，不是 `[str]`）。
- 顺序按文件名排序（`diffusion_pytorch_model-00001-of-00002` < `-00002-`）。
- 用适配器自动展开：`diffsynth_adapter.expand_model_paths(r"%MODEL_DIR%")`。
- `--processor_path` 必须指本地 `%MODEL_DIR%\processor`；不传会让 DiffSynth 去拉 `Qwen/Qwen-Image-2.1`（会下载）。

## 3. 命令 A：走适配器（推荐，argv 与基线逐字一致）

```powershell
$py = 'E:\ComfyUI\python_embeded\python.exe'

# 3.1 先看会生成的 argv（零 GPU，不建进程）
& $py -c "import sys; sys.path.insert(0,r'E:\视频\modelsmith\adapters'); import diffsynth_adapter as A, json; p={'model':r'%MODEL_DIR%','dataset':r'%DATA%','out':r'%OUT%'}; print(json.dumps(A.build_argv(p, r'%OUT%'), ensure_ascii=False, indent=1))"

# 3.2 真跑（GPU 一臂；约 15–25 s）
& $py -c "import sys; sys.path.insert(0,r'E:\视频\modelsmith\adapters'); import diffsynth_adapter as A; p={'model':r'%MODEL_DIR%','dataset':r'%DATA%','out':r'%OUT%'}; r=A.run({'id':'qwen21-smoke','params':p}, r'%OUT%\run.log'); print(r['state'], r['metrics']['step'], '/', r['metrics']['total'], r['metrics']['artifacts'])"
```

> 把 `%MODEL_DIR% / %DATA% / %OUT%` 换成绝对路径时**保留 `r'...'` 引号**（中文路径 + 反斜杠）。

## 4. 命令 B：原生 argv（调试/排障用）

适配器实际执行的就是这条（`cwd=%REPO%`）：

```
%PY% -c "import runpy,sys; repo,train_py=sys.argv[1],sys.argv[2]; sys.path.insert(0,repo); sys.argv=[train_py]+sys.argv[3:]; runpy.run_path(train_py, run_name='__main__')" ^
  "%REPO%" "%REPO%\examples\qwen_image_21\model_training\train.py" ^
  --dataset_base_path "%DATA%" ^
  --dataset_metadata_path "%DATA%\metadata.csv" ^
  --model_paths "<上面那段 JSON>" ^
  --processor_path "%MODEL_DIR%\processor" ^
  --output_path "%OUT%" ^
  --lora_base_model dit ^
  --lora_target_modules "" ^
  --lora_rank 8 ^
  --num_epochs 1 ^
  --learning_rate 1e-4 ^
  --dataset_repeat 1 ^
  --use_gradient_checkpointing ^
  --initialize_model_on_cpu ^
  --enable_model_cpu_offload ^
  --height 512 --width 512 ^
  --task sft ^
  --remove_prefix_in_ckpt pipe.dit.
```

## 5. 参数表（适配器默认值 → 上游 flag）

| 适配器 param | 默认 | train.py flag | 备注 |
|---|---|---|---|
| `model` / `model_paths` | 必需 | `--model_paths`（JSON） | 目录自动展开；显式传则原样用 |
| `dataset` | 必需 | `--dataset_base_path` | 目录 |
| `dataset_metadata_path` | `<dataset>/metadata.csv` | `--dataset_metadata_path` | 必须存在 |
| `out` / `output_path` | 必需 | `--output_path` | 产物落在这里 |
| `processor_path` | `<model>/processor` | `--processor_path` | 必须本地 |
| `lora_base_model` | `dit` | `--lora_base_model` | |
| `lora_target_modules` | `""` | `--lora_target_modules` | **空 = 自动探测**（本配方验证值） |
| `lora_rank` | `8` | `--lora_rank` | |
| `num_epochs` | `1` | `--num_epochs` | 4 图 → 每 epoch 4 step |
| `learning_rate` | `1e-4` | `--learning_rate` | |
| `dataset_repeat` | `1` | `--dataset_repeat` | |
| `use_gradient_checkpointing` | `true` | `--use_gradient_checkpointing` | |
| `initialize_model_on_cpu` | `true` | `--initialize_model_on_cpu` | 防加载期显存尖峰 |
| `enable_model_cpu_offload` | `true` | `--enable_model_cpu_offload` | 逐层上卡 |
| `height` / `width` | `512` | `--height/--width` | 也可给 `resolution` |
| `task` | `sft` | `--task` | |
| `remove_prefix_in_ckpt` | `pipe.dit.` | `--remove_prefix_in_ckpt` | LoRA 前缀剥离 |
| `save_steps` | 不传 | `--save_steps` | 不传 = 每 epoch 存一次；**`0` = 不按 step 存档（只出 epoch 档）**；`>0` 才传 flag（传了就不出 epoch 档） |
| `gradient_accumulation_steps` | 不传(1) | `--gradient_accumulation_steps` | |

## 6. 预期读数（A 级基线）

| 指标 | 读数 | 怎么判红 |
|---|---|---|
| 进度 | **4/4**（1 epoch × 4 图） | 不到 4/4 或 total≠4 |
| 速度 | **3.88 s/it**（tqdm） | 无 `s/it` 或数量级异常 |
| 产物 | `%OUT%\epoch-0.safetensors`，**41,999,944 B（≈40.05 MiB）** | 文件缺失 / 小于 ~10 MB |
| 进程 | exit 0 | 非 0 |
| 峰值显存 | **本席未复测**（维护者 跑通时未记录） | 下一臂用 `nvidia-smi` 采 |

> 采样图：DiffSynth 的 `model_training/train.py` 这版**不出采样图**，验收靠 `epoch-*.safetensors` + 事后用 LoRA 跑一张推理图（另配）。

## 7. 三个坑（按踩到顺序）

1. **把目录当 `--model_paths`**：转成 JSON 时必须是分片路径列表；vae 单文件是字符串。传目录/单文件都会在 `ModelConfig(path=...)` 处炸。→ 用 `expand_model_paths()`。
2. **`--lora_target_modules` 别填 `q,k,v,o,ffn.0,ffn.2`**：那是别的 arch 的默认值；Qwen-Image 2.1 用**空字符串**让 DiffSynth 自动探测（`auto_detect_lora_target_modules`），否则可能挂到不存在的模块上。
3. **`--processor_path` 不传 = 去 HuggingFace 拉**：本配方必须显式指 `%MODEL_DIR%\processor`，否则会偷偷下载 `Qwen/Qwen-Image-2.1` 的 processor。

> 另：数据必须是 `metadata.csv`（不是同名 `.txt`）；`--initialize_model_on_cpu --enable_model_cpu_offload` 两个都要开，否则 32 GB 卡在加载 transformer+text_encoder 时容易顶满。

## 8. 取消与产物

- 取消：适配器 `run(..., stop_file=...)` → Windows 先发 `CTRL_BREAK_EVENT`（新建进程组），5 s 宽限后 terminate/kill；终态 `cancelled`。
- 产物扫描：结束后自动扫 `%OUT%\epoch-*.safetensors` / `step-*.safetensors`，写进 `metrics["artifact_files"]`（含 `bytes`）。
- 日志：`run(job, log_path)` 把子进程 stdout+stderr 逐行落盘；`# diffsynth adapter finished: state=...` 是收尾行。

## 9. 每 N 步出图看效果 + 提前中止（2026-10-22 新增，A 级实测）

上游 `train.py` **没有**原生采样/验证回调（153 行全文无 sample/validation/callback），所以适配器把采样做成
**训练进程内钩子**：runner 在 `runpy` 之前 monkeypatch `ModelLogger.on_step_end`，每到 `sample_every` 步就用
**训练进程已加载的 `model.pipe`** 直接出图 —— 零重载、可随时中止（不是分段重启）。

### 9.1 新参数

| param | 默认 | 说明 |
|---|---|---|
| `sample_every` | `200` | 每多少个优化步出一次图；`0` = 关 |
| `sample_prompts` | `[]` | 预设提示词数组；**空 + every>0 ⇒ 警告并自动关闭** |
| `sample_steps` | `20` | 采样去噪步数（不是训练步） |
| `sample_guidance` | `1.0` | CFG（Qwen-Image 2.1 无 guidance，保持 1.0） |
| `sample_seed` | `42` | 基础种子，第 i 张用 `seed+i` |
| `sample_width` / `sample_height` | `512` / `512` | 采样分辨率 |
| `sample_negative` | `""` | 负向提示词 |

### 9.2 命令

```powershell
$py = 'E:\ComfyUI\python_embeded\python.exe'
& $py -c "import sys; sys.path.insert(0,r'E:\视频\modelsmith\adapters'); import diffsynth_adapter as A; p={'model':r'%MODEL_DIR%','dataset':r'%DATA%','out':r'%OUT%','sample_every':200,'sample_prompts':['a red apple on a wooden table, studio light'],'sample_steps':20,'sample_width':512,'sample_height':512,'sample_seed':42}; r=A.run({'id':'qwen21-sample','params':p}, r'%OUT%\run.log'); print(r['state'], r['metrics']['sample_events'])"
```

### 9.3 产物与事件

- 图：`%OUT%\samples\step-200\sample_00.png`（每步一个目录、每 prompt 一张）
- stdout：每张图一行 **`[sample] step=200 file=...`**（核心/UI 靠它驱动缩略图墙）
- `metrics.sample_events = [{"step": 200, "files": [{"file","bytes","path"}], "lora_path": ...}]`
- `metrics.steps_estimate`：`num_epochs × dataset_rows × dataset_repeat`（数据集行数从 `metadata.csv` 数出来）

### 9.4 提前中止

看到 step-200 的图不满意 → 直接取消（stop 文件 / Ctrl-C / 关进程），不用等训完。
最后一张采样图对应 **step=N 的权重**；但该步的 LoRA 档只有在你同时传了 `save_steps=N` 时才会落成
`%OUT%\step-N.safetensors`（`lora_path` 字段会自动填；没传则为 `null`）。

### 9.5 三个坑（本机实测）

1. **`--save_steps` 会抑制 `epoch-*.safetensors`**（`diffsynth\diffusion\runner.py:113-114`：只有 `save_steps is None` 才调 `on_epoch_end`）
   ⇒ 要终版档就二选一：不传 `save_steps`（只出 epoch 档），或传了之后自己从 `step-*.safetensors` 里拿最后一个。
2. **`resume_from_checkpoint` 只恢复权重**（`diffsynth\diffusion\training_module.py:466-475`：只有 `load_state_dict`，无步号/optimizer/数据位置）
   ⇒ 不能"续训到 400"。要补中间步的图，用**离线补图**：`--save_steps N` 训完后跑
   `third_party\DiffSynth-Studio\examples\qwen_image_21\model_training\validate_lora\Qwen-Image-2.1.py`
   （把 `pipe.load_lora(pipe.dit, "<out>/step-N.safetensors")` 路径换成实际档）。
3. **采样显存**：实测峰值 **31,945 MiB**（卡 32,760 MiB；256×256、4 采样步、4 训练图）——显存由 bf16 权重主导
   （text_encoder 16.2GB + DiT 13.25GB），不是分辨率。**32 GB 卡上没有余量做 batch>1**；跑采样/训练前关掉其它 GPU 程序。钩子已做
   "采样后恢复 scheduler.training + 各组件原位 device + empty_cache"，否则下一步训练会崩
   （`KeyError: 'use_kv_cache'` / CPU-CUDA 混合）。

