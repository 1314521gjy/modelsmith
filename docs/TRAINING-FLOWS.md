# 四类训练的真实流程（上游源码核对版）

> 唯一依据：本机第三方源码克隆 + 本机训练台实践。**禁止编造**；查不到写「未验」。
> 出处简写（完整绝对路径，后文 `L/`、`A/`、`F/`、`K/` 都是这四个前缀的缩写）：
> `L/` = `E:\视频\modelsmith\third_party\laya\`；`A/` = `E:\视频\modelsmith\third_party\ai-toolkit\`；
> `F/` = `E:\视频\modelsmith\third_party\LlamaFactory\`；`K/` = `E:\视频\_kb_research\htlb\train\`。
> 证据等级：**A(源码)** = 读上游源码/官方文档原文；**A** = 本机实测读数（引 `K/results`、`K/README`）。
> **未验** = 本机源码未提供证据，不许补脑。

**先给结论（四类根本不是一条流水线）**：
决策模型是**单文件 JSONL/CSV → 一轮训练 → 末尾校准+评测**（无采样图，进度靠 loss 行）；
生图/视频是**文件夹图片+同名 caption → 按固定步数训 → 每 N 步边训边采样出图**；
LLM 是**注册数据集名 → 按 epoch 训 → 每 N 步存 checkpoint**。四者的「成果」定义不同（见 §5 差异表）。

---

## 1 决策模型（Laya）· P0

### 1.1 数据格式与目录约定

| 形态 | 约定 | 出处 |
|---|---|---|
| CSV | 一行一决策；文本列 + 标签列（默认 `text`/`label`）；标签列的**去重值按首次出现顺序**变成 choice 选项；题目 id 默认 `label`，指令来自 `--instructions` | `L/docs/finetune.md:27-48`；`L/laya/train_cli.py:53-66` |
| JSONL | 一行一 case：`{state, questions, expected}` 或 `{state, questions, gold}`，两者可混且可同时含多题；题类型只三种 `choice/score/noul` | `L/docs/finetune.md:52-69` |
| `expected` | choice=标签串，noul=true/false，score=档位（`1.5` 这类小数把权重拆到 1/2 档） | `L/laya/train.py:196-284`；`finetune.md:63-66` |
| `gold` | 教师概率；noul 键 `"false"/"true"`，score 键为档位索引字符串 | `train.py:167-193`；`finetune.md:67-69` |
| 目录 | **没有固定数据目录约定**：`--data` 接受单文件；产物目录由 `--out` 给；训练/校准/可选评测是**三份独立来源** | `train_cli.py:24-41,153-160`；`finetune.md:74,170` |

- **必做的「体检」= `--dry-run`**：只加载 tokenizer 与 config，不加载权重、不训练，打印读入行数 / 生成 item 数 / 跳过计数与原因 / 优化器更新预算（`finetune.md:71-88`；`train.py:1282-1330`）。
  跳过原因枚举 5 种：`empty_text`、`empty_label`、`invalid_question`、`invalid_target`、`options_collapsed`、`options_beyond_max_len`（`finetune.md:82-88`）。
- 本题域的实际约定（本机 A 级）：训练/留出**按章节切**（节 1–27 训练、28–34 留出），同一节的问法与候选只出现在一边（`K/README-训练台.md:39-40`）。

### 1.2 必填与默认参数

| 参数 | 必填 | 默认 | 校验 | 出处 |
|---|---|---|---|---|
| `--data` | **是** | — | argparse `required=True` | `train_cli.py:24-27` |
| `--out` | 是（`--dry-run` 时免） | — | `--out is required unless --dry-run` | `train_cli.py:176` |
| `--base` | 否 | `convaiinnovations/laya` | 目录 / 别名(`english`/`multilingual`/`typed-decisions`) / Hub id | `train_cli.py:44-49`；`finetune.md:162` |
| `--loss` | 否 | `rlcd` | ∈`{rlcd,soft-ce}` | `train.py:63,99,127-128`；`finetune.md:163` |
| `--epochs/--micro-batch/--grad-accum` | 否 | `4 / 8 / 8`（等效批 64） | 正整数 | `train.py:91-93,135-138`；`finetune.md:167` |
| `--encoder-lr/--head-lr` | 否 | `2.5e-5 / 1e-4` | — | `train.py:94-95`；`finetune.md:168` |
| `--max-len/--head-max-len` | 否 | **取 base ckpt 自己的 config**（缺省 512/192；notebook 用 1024/256） | 写回产物 config | `train.py:112-113,1005-1006,1294-1295`；`finetune.md:169` |
| `--shuffle-options` | 否 | 关 | 名字必须是已知题类型 | `train.py:105,129-131`；`finetune.md:164` |
| `--label-smoothing` | 否 | `0.0` | `[0,1)` | `train.py:117,141-142` |
| `--target-error` | 否 | `0.10` | `[0,1]` | `train.py:123,143-144` |
| `--min-abstain-n` | 否 | `10` | 正整数 | `train.py:124,145-146` |
| `--eval/--eval-data` | 否 | 无 | — | `finetune.md:170` |
| `--device/--seed` | 否 | `auto / 0` | — | `train.py:111,528-537` |
| 校准切片（Python 才有） | 否 | `calib_max=400`、`calib_frac=0.1`、`calib_seed=20260922` | `calib_frac∈[0,1)` | `train.py:108-110,139-140` |

### 1.3 训练循环里实际发生什么

1. 编码 item：state token（`max_len`）+ 题目/选项 head（`head_max_len`）（`train.py:293-316,446-461`）。
2. 每 epoch 用 `seed+epoch` 洗牌 item；每个 micro-batch 若开 `shuffle_options` 就抽一个随机 `option_order` 并把 target 同步置换（`train.py:464-471,688-697`）。
3. 前向 → 取 loss（`rlcd` 或 `soft-ce`）→ **除以窗口大小后 backward**；在 `grad_accum` 边界**或本 epoch 最后一个不满窗口**做一次 `clip_grad_norm_(1.0)` + `step` + `scheduler.step()`（`train.py:699-725`）。
4. 优化器 AdamW（两组 lr）+ **CosineAnnealingLR，T_max = 全程 optimizer updates**（`train.py:671-681`）。
5. CUDA 上默认 fp16 autocast + gradient checkpointing（`amp` 未显式给时跟随设备）（`train.py:657-658`；`finetune.md:175-176`）。
6. `rlcd` 的具体形态：4 个带噪 logit 投影（σ 0.4→0.1 按 epoch 线性退火），proper reward（spherical 0.75 + ranked-prob 1.0）→ 归一化 advantage → 高斯 log 密度项 + **全权重 soft-CE**（`train.py:499-523`）。
7. 每 epoch 末回调存 `checkpoint_latest/`（`train.py:1125-1130`）。
8. 全部 epoch 结束后才做：拟合温度 → 拟合拒答阈值 → （可选）before/after 评测 → 存终版（`train.py:1132-1237`）。
9. **没有按步评测、没有早停、没有 resume**：`train.py` 全文无 `resume`/`patience`/`early` 命中（已 grep，0 命中）。

### 1.4 运行中该显示什么（真字符串）

| 时机 | 输出 | 出处 |
|---|---|---|
| 起跑 | `train items N, calibration items N, eval items N, skipped {...}, device cuda` | `train.py:1112-1114` |
| 起跑 | `optimizer budget: 1100 train item(s), effective batch up to 64 (micro_batch 8 x grad_accum 8), 72 optimizer updates over 4 epoch(s)` | `train.py:622-632`；`finetune.md:104` |
| **每 100 个 micro-batch 步** | `epoch 1/4 step 100 loss 1.2345`（`log_every` 默认 100） | `train.py:116,727-728` |
| 每 epoch | `epoch 1/4 mean loss 1.2345` | `train.py:731` |
| 校准告警 | `laya.train: choice calibration: not fitted: 2 calibration items, fewer than 10, so the temperature stays 1.0` | `train.py:1140-1142`；`finetune.md:127-129` |
| 末段 | `=== Evaluation (before vs after fine-tuning on N held-out items …) ===` + `Metric/Before/After/Delta` 表 | `train.py:1170-1191` |

**坑（UI 编进度条必踩）**：loss 行的「step」是 **micro-batch 步**，budget 行的「updates」是 **optimizer 步**；两者差 `grad_accum` 倍（默认 8）。拿 loss 行当进度分母会显示到 8 倍处（`train.py:707-728` 对照 `607-619`）。

### 1.5 每多少步/轮出一次成果

- **决策模型不出采样图**（全流程无图像生成）。
- 产物节奏：`checkpoint_latest/` **每 epoch 重写一次**（崩溃最多损失一轮）；终版 checkpoint 在**校准之后**一次性落盘（`finetune.md:117-120,344-345`；`train.py:1125-1130,1229-1237`）。
- 可看的「成果」= 每 100 micro-step 的 loss 行 + 每 epoch 均损 + 末尾 before/after 表。
- 本机实测（A 级）：1310 步 = 2 epoch × 655 步，**7.7 分钟**、峰值 **16.3 GiB**、0.34 s/步（`K/README-训练台.md:123`）。

### 1.6 怎么停

- 无内建 stop 接口；只能中断进程（Ctrl+C / 杀进程）。因为没有 resume，停下就只能从头再训（未发现 resume 路径）。
- 止损设计就是 `checkpoint_latest/`：**至少拿回上一轮**（`finetune.md:344-345`）。

### 1.7 产物清单

| 路径 | 内容 | 出处 |
|---|---|---|
| `<out>/model.safetensors` | 决策头+编码器权重 | `finetune.md:118`；`train.py:590-602` |
| `<out>/encoder/`、`<out>/tokenizer/` | 编码器与分词器 | 同上 |
| `<out>/rl_agent_config.json` | `max_len/head_max_len`、`fine_tuned`、`temperature`、`temperature_by_options`、`training.laya_train`、`training.laya_train_calibration`、`training.train_report`、`abstention_thresholds` | `train.py:1219-1228` |
| `<out>/questions.json` | 题 schema（CSV 路线必带，推理要复用同一题） | `train.py:1239-1256`；`finetune.md:36-37` |
| `<out>/train_report.json` | eval_source/mode/is_held_out/note、before/after/comparison、calibration、abstention_thresholds、training | `train.py:1205-1217,1258-1264` |
| `<out>/checkpoint_latest/` | 每 epoch 重写；末尾会同步最终校准后的 config | `train.py:1125-1137,1231-1237` |

---

## 2 生图 LoRA（ai-toolkit）· P0

### 2.1 数据格式与目录约定

| 项 | 约定 | 出处 |
|---|---|---|
| 数据 | **一个文件夹**（`datasets[].folder_path`）；图片 + **同名 .txt caption**（`image2.jpg` ↔ `image2.txt`） | `A/config/examples/train_lora_flux_24gb.yaml:28-36` |
| 扩展名 | 只支持 jpg / jpeg / png | 同上 `:30` |
| caption 文件后缀 | `caption_ext`（默认 `.txt`，自动补点） | `A/toolkit/config_modules.py:950-953` |
| 分辨率 | 自动缩放并 **bucketing**；FLUX/Qwen-Image 吃多分辨率 `[512,768,1024]`，SD3.5 示例 `[1024]` | `train_lora_flux_24gb.yaml:39`；`train_lora_qwen_image_24gb.yaml:38`；`train_lora_sd35_large_24gb.yaml:40` |
| 视频同源 | `num_frames>1` 时数据加载器**改找视频文件**；`num_frames=1` 找图片 | `config_modules.py:1071-1074` |
| 加速缓存 | `cache_latents_to_disk: true`（默认建议保持） | `train_lora_flux_24gb.yaml:38` |

### 2.2 必填与默认参数

**必填最小集**：`config.name`、`process[0].training_folder`、至少一个 `datasets[].folder_path`、`train.steps`、`model.name_or_path`。
**强烈建议但非必填**：`sample.prompts`（不给 prompt 就不出采样图——`sample()` 只是按 prompts 列表循环，空列表=0 张）。

| 参数 | 默认 | 示例值 | 出处 |
|---|---|---|---|
| `train.steps` | **1000** | 2000（示例都写 2000，注释「500–4000 是好区间」） | `config_modules.py:383`；`train_lora_flux_24gb.yaml:42` |
| `train.batch_size` | **1** | 1 | `config_modules.py:396` |
| `train.lr` | —（必给，示例 1e-4） | 1e-4 | `train_lora_flux_24gb.yaml:49` |
| `train.gradient_accumulation(_steps)` | **1** | 1 | `config_modules.py:458-465` |
| `network.linear / linear_alpha` | —（示例 16/16、32/32） | 16/16 | `train_lora_flux_24gb.yaml:18-19`；`train_lora_wan22_14b_24gb.yaml:19-20` |
| `save.save_every` | **1000** | 250 | `config_modules.py:25`；示例 `:22` |
| `save.max_step_saves_to_keep` | **5** | 4 | `config_modules.py:27`；示例 `:23` |
| `sample.sample_every` | **100** | 250 | `config_modules.py:84`；示例 `:72` |
| `sample.sample_start_step` | **0** | 0 | `config_modules.py:85` |
| `sample.sample_steps`（采样去噪步） | **20** | 20 / 25 | `config_modules.py:92`；示例 `:93` |
| `sample.guidance_scale` | **7** | 4 / 3.5 / 3 | `config_modules.py:91`；示例 `flux:92` |
| `sample.width/height` | **512/512** | 1024/1024 | `config_modules.py:86-87` |
| `sample.num_frames / fps` | **1 / 16** | 1 / 16（视频加大 `num_frames`） | `config_modules.py:100-105` |
| `log_every` | **100** | — | `config_modules.py:37` |
| `trigger_word` | **None**（全注释掉） | `"p3r5on"` | `config_modules.py:936`；示例 `:15`（flux）/`:16`（wan22） |
| `sample.walk_seed` | **false** | true | `config_modules.py:90` |

**校验/互斥规则**（UI 必校）：`gradient_accumulation` 与 `gradient_accumulation_steps` **互斥**（同时 >1 直接 `ValueError`，`config_modules.py:463-465`）；`sample.sampler` 必须与 `train.noise_scheduler` 匹配（示例注释「must match」，`train_lora_flux_24gb.yaml:71`）；`save_format` ∈`{safetensors,diffusers}`（`config_modules.py:29-30`）。

### 2.3 训练循环里实际发生什么

1. 预采样：`step_num <= 1` 且没开 `skip_first_sample`/`disable_sampling` → 打印 `Generating baseline samples before training` 并出**首图**（`A/jobs/process/BaseSDTrainProcess.py:2487-2491`）。
2. 主循环 `for step in range(start, train.steps)`：取 batch → `hook_train_loop` → loss；OOM 单步跳过，**连续 3 次 OOM 才中止**（`A/jobs/process/BaseSDTrainProcess.py:2543-2657`）。
3. 每次进循环先算两个布尔：`is_save_step = save_every and step % save_every == 0`；`is_sample_step = sample_every and step >= sample_start_step and step % sample_every == 0`（同文件 `:2564-2571`）。
4. `step == start_step`（第 0 步）时**不重复采样**（`if self.step_num != self.start_step`，同文件 `:2716`）——所以「首图」来自预采样那一步，不是循环里的 0 步。
5. 命中 save → `Saving at step N` + 存 safetensors；命中 sample → 逐个 prompt 出图（同文件 `:2720-2738`）。
6. 循环结束后无条件 `self.save()` 存**终版**（无步号后缀）（同文件 `:2829`）。

### 2.4 sample_prompts 怎么写 / trigger word

- 字段是 `sample.prompts`（列表，每项一个字符串；也支持 `samples:` 逐项对象，见 `config_modules.py:107-122`）。
- `[trigger]` 占位：训练 caption 里写 `[trigger]` 会被替换成 `trigger_word`；若 caption 里**没有** trigger，则**自动加在开头**（`A/toolkit/prompt_utils.py:715-748`；`train_lora_flux_24gb.yaml:13-14`）。
- 采样 prompt 同样替换 `[trigger]`，但 `add_if_not_present=False`（**不会**自动补到前面，必须你自己写进去）（`BaseSDTrainProcess.py:326-329`；`prompt_utils.py:740`）。
- **冲突**：缓存文本嵌入（`cache_text_embeddings: true`）时 trigger word 不生效——Qwen-Image 示例明写「Trigger words will not work when caching text embeddings」，Wan2.2 示例明写「if caching text embeddings, do not use a trigger word」（`train_lora_qwen_image_24gb.yaml:15`；`train_lora_wan22_14b_24gb.yaml:15`）。

### 2.5 每多少步出几张 / 首末步是否出

- **每 `sample_every` 步出一次，一次出 `len(prompts)` 张**（按 prompt 循环，`walk_seed` 时 seed 逐张 +1）（`BaseSDTrainProcess.py:300-309`）。
- 真实默认：`sample_every=100`（schema 默认），但**所有官方示例都手写 250**——UI 若显示默认值，必须说清是「配置默认 100」还是「示例 250」。
- **首步出**（预采样，`step<=1`）；**末步只在 `train.steps % sample_every == 0` 时出**，否则最后一组图停在最后一个整数倍处，终版 LoRA 没有对应采样图（源码无「末尾强制采样」分支：`A/jobs/process/BaseSDTrainProcess.py:2565-2569` 只有取模条件）。
- 例：`steps=2000, sample_every=250` → 首图 1 组 + 250/500/…/2000 共 8 组 = **9 组 × 10 张 = 90 张**；`steps=2200` 则末组在 2000，2200 不出图。

### 2.6 运行中该显示什么

| 项 | 内容 | 出处 |
|---|---|---|
| 进度条 | `total = train.steps`，postfix 为 `lr: 1.0e-04 loss: 1.234e-01 …`（loss 字典逐键拼接） | `BaseSDTrainProcess.py:2494-2501,2703-2708` |
| 存档 | `Saving at step 250` | `BaseSDTrainProcess.py:2725` |
| 采样 | 图落到 `save_root/samples/[time]_[step]_[count].<ext>` | `BaseSDTrainProcess.py:286-309` |
| 采样扩展名 | 视频（`num_frames>1`）自动改 animated **webp** | `config_modules.py:103-105` |
| 可选性能日志 | `performance_log_every: 1000`（示例里被注释掉） | `train_lora_flux_24gb.yaml:11` |

### 2.7 怎么停 / 产物

- 停：SIGINT（Ctrl+C 或 UI 的 stop 按钮）= **正常停止而非报错**；Windows 下由 2 秒轮询 stop flag 的守护线程 `signal.raise_signal(SIGINT)` 实现（`A/extensions_built_in/sd_trainer/DiffusionTrainer.py:42-85,320-321`）。
- 产物：`<training_folder>/<name>/<name>.safetensors`（终版，`save()` 无步号）；中途 `…_000000250.safetensors`（9 位零填充步号）；采样图在 `…/samples/`；`max_step_saves_to_keep` 控中途档数（`BaseSDTrainProcess.py:506-525,517-521`；`config_modules.py:27`）。
- 进度恢复：源码未见 resume 逻辑（未验）。

### 2.8 验收怎么看（loss / 过拟合 / 采样对比 / 产物）

- **看采样图对比，不是看 loss 下降**：一组 prompt 固定、seed 固定（`seed:42` + `walk_seed:true` 逐张偏移），不同 step 的同一 prompt 图并排，才看得出「像不像数据集」；样本里应能肉眼看到 trigger 概念在 ~250–1000 步间逐渐成型（此为阅读示例+日志机制的推断，**未做本机实测，标未验**）。
- 官方唯一内建「验证」是 `validation` 段：`validate_every_n_steps` 默认 **10**，`validation_sigmas` 默认 `[1.0,0.75,0.5,0.25]`（`config_modules.py:349-357`）；本文读的四份示例（flux/qwen_image/sd35/wan22）**都没开**——即默认验收口径就是「人眼看采样图」。
- 过拟合信号（经验项，非上游断言，标未验）：训练图在后期同 seed 高步数采样里开始复现原图构图/水印；此时用早一档 `save_every` 的中间档而不是终版。
- 产物体检：safetensors 能否被推理端加载、`max_step_saves_to_keep` 是否把想要的档删掉（默认只留 5 档，`config_modules.py:27`）。

---

## 3 LLM 微调（LlamaFactory）· P1（简写）

### 3.1 数据格式与目录约定

| 项 | 约定 | 出处 |
|---|---|---|
| 目录 | `dataset_dir` 默认 **`data`**；数据集必须先在 `data/dataset_info.json` **注册一个名字**，`dataset:` 只写名字（逗号分隔多个） | `F/src/llamafactory/hparams/data_args.py:30-41` |
| 注册项 | `{file_name, formatting, columns:{...}, tags:{...}}`；不写 `formatting` 默认 alpaca | `F/data/dataset_info.json:1-45` |
| alpaca 行 | `{"instruction","input","output"}`（`input` 可为空串） | `F/data/identity.json:1-6` |
| sharegpt 行 | `{"conversations":[...], "tools":[...]}`，靠 `columns` 映射 | `F/data/dataset_info.json:11-18` |
| 模板 | `template` 默认 **None**（必须给，示例 `qwen3_nothink`） | `data_args.py:26-29`；`F/examples/train_lora/qwen3_lora_sft.yaml:14` |

### 3.2 必填与默认

| 参数 | 默认 | 示例值 | 出处 |
|---|---|---|---|
| `model_name_or_path` | 无（必填） | `Qwen/Qwen3-4B-Instruct-2507` | `qwen3_lora_sft.yaml:2` |
| `stage` | **`sft`** | sft | `F/src/llamafactory/hparams/finetuning_args.py:460-462` |
| `finetuning_type` | **`lora`** | lora | `finetuning_args.py:464-466` |
| `lora_rank` | **8** | 8 | `finetuning_args.py:77-80` |
| `lora_alpha` | **`rank*2`=16** | 未写→16 | `finetuning_args.py:69-72,568` |
| `lora_target / lora_dropout` | `all / 0.0` | all | `finetuning_args.py:73-82` |
| `cutoff_len` | **2048** | 2048 | `data_args.py:46-49` |
| `val_size` | **0.0** | 0.0（注释掉） | `data_args.py:100-103`；`qwen3_lora_sft.yaml:42` |
| `per_device_train_batch_size` | **8** | 1 | `transformers 4.57.6 training_args.py:861-863`（本机 `E:\ComfyUI\python_embeded\Lib\site-packages\`） |
| `gradient_accumulation_steps` | **1** | 8 | 同上 `:887`；`qwen3_lora_sft.yaml:31` |
| `learning_rate` | **5e-5** | 1e-4 | 同上 `:915`；`:32` |
| `num_train_epochs` | **3.0** | 3.0 | 同上 `:922`；`:33` |
| `save_steps` | **500** | 500 | 同上 `:991-998`；`:23` |
| `logging_steps` | **500** | **10** | 同上 `:977-985`；`:22` |
| `save_total_limit` | **None（不删）** | 未写 | 同上 `:1000-1013` |
| `report_to` | 未写=无 | `none` | `qwen3_lora_sft.yaml:27` |

### 3.3–3.7 循环 / 显示 / 节奏 / 停 / 产物

- 循环：tokenize(`cutoff_len`) → 只对 answer token 计 loss（`train_on_prompt=False`、`mask_history` 可选）→ AdamW + cosine + `warmup_ratio` → 每 `gradient_accumulation_steps` 一次 optimizer step（`data_args.py:50-57`；`qwen3_lora_sft.yaml:34-36`）。
- 显示：transformers 的 step 进度条；**每 `logging_steps` 一条 loss**（示例里被改成 10，默认 500 → UI 若用默认，前 500 步看不到任何 loss）；`Resuming training from <ckpt>` 才是真 resume（`F/src/llamafactory/hparams/parser.py:498-499`）。
- 成果节奏：**每 `save_steps` 存一个 `checkpoint-<global_step>/`**（含 `model/adapter_model.safetensors` 与 optimizer state），**不出图**；全文无采样（`F/src/llamafactory/v1/core/utils/checkpoint.py:195-205,238-251`）。
- 停：Ctrl+C；可 `resume_from_checkpoint`（`parser.py:477-508`；`qwen3_lora_sft.yaml:38`）。
- 产物：`output_dir/` 终版 adapter（`trainer.save_model()`）+ `train_results.json`（`save_metrics`）+ `training_loss.png`（`plot_loss: true`）+ `checkpoint-N/`（`F/src/llamafactory/train/sft/workflow.py:120-140`）。
- 评测：只有给了 `eval_dataset` 或 `val_size>0` 才评；两者同时给直接报错（`data_args.py:159-163`）。

---

## 4 视频 LoRA（ai-toolkit · Wan2.2 14B）· P1（简写）

- 数据：仍是**一个文件夹**；`num_frames>1` 时加载器**找视频文件**，`shrink_video_to_frames` 默认 true → 均匀抽 N 帧（100 帧取 10 帧：0,10,…,90）；`fps` 默认 24，仅在 `shrink_video_to_frames=false` 时用（`A/toolkit/config_modules.py:1071-1082`）。
- 与生图不同的必填/默认：`model.arch: 'wan22_14b'`、`qtype` 指 Accuracy Recovery Adapter（4bit 训练）、`cache_text_embeddings: true`（24GB 必需；与 `unload_text_encoder` **二选一**）、`train_high_noise`/`train_low_noise` 同时 true、**`switch_boundary_every: 10`**（Wan2.2 MOE 每 10 步切高/低噪专家）（`A/config/examples/train_lora_wan22_14b_24gb.yaml:55-79`）。
- 采样：`sample.num_frames` 示例仍写 **1**（按图训时的值）；出视频要把 `num_frames>1`，此时采样扩展名自动改 **animated webp**，`fps: 16`；示例注释「samples take a long time. so use them sparingly」（`A/config/examples/train_lora_wan22_14b_24gb.yaml:86-90`；`config_modules.py:103-105`）。
- 节奏：完全沿用 `sample_every`（schema 默认 100 / 示例 250）与 `save_every`（默认 1000 / 示例 250）语义；每采样事件出 `len(prompts)` 个 webp 片段而不是单图（`BaseSDTrainProcess.py:300-309`）。
- 停：与生图同一套 SIGINT/stop-flag（`DiffusionTrainer.py:42-85`）。产物：同名 `.safetensors` + `samples/*.webp`。
- 未验：Wan2.2 双专家切换对显存/步时的影响、`qtype` ARA 的实际收益——本机无该模型权重，未跑。

---

## 5 UI 差异表（四类在 UI 上**必须不同**的点）

> 用法：UI 不是「一张表单套四种皮肤」，而是四套字段/校验/预估/进度单位。每条给上游依据。

| 维度 | 决策（Laya） | 生图（ai-toolkit） | 视频（wan22） | LLM（LlamaFactory） | 上游依据 |
|---|---|---|---|---|---|
| **数据形态** | **单文件** JSONL 或 CSV（+列名） | **文件夹**，图片+同名 `.txt`，仅 jpg/jpeg/png | **文件夹**，视频+同名 caption | **已注册的数据集名**（先写 `dataset_info.json`） | `finetune.md:27-69`；`train_lora_flux_24gb.yaml:28-36`；`config_modules.py:950-953,1071-1074`；`data_args.py:30-41` |
| **底模字段** | `--base` 有默认 `convaiinnovations/laya`，可留空 | `model.name_or_path` **必填** + `arch`（flux/qwen_image/sd35/…） | `arch: wan22_14b` **必填** + `qtype` ARA | `model_name_or_path` **必填** + `template` **必填** | `finetune.md:162`；`train_lora_qwen_image_24gb.yaml:57-60`；`train_lora_wan22_14b_24gb.yaml:66-73`；`finetuning_args.py:460-466`；`data_args.py:26-29` |
| **只有本类有的字段** | `loss`(rlcd/soft-ce)、`shuffle_options`、`label_smoothing`、校准切片、`target_error`、`min_abstain_n`、`eval_data` | `sample.prompts`、`trigger_word`、`sample_every`、`sample_start_step`、`save_every`、`guidance_scale`、`sample_steps`、多分辨率列表 | 生图全部 + `num_frames`/`fps`/`switch_boundary_every`/`train_high_noise` | `stage`(sft/dpo/kto/rm/pt/ppo)、`finetuning_type`(lora/oft/freeze/full)、`lora_target`、`cutoff_len`、`val_size` | 同 §1.2 / §2.2 / §3.2 表 |
| **可省略的步骤** | `--eval` 可省（省了「after」来自**校准切片**=in-sample，报告 `eval_mode` 会写明）；`--shuffle-options` 可省 | `sample.prompts` 可省（=不出采样图）；`skip_first_sample`/`disable_sampling` 可跳首图或全关 | 同生图 | `eval_dataset` 可省；不给则无 eval 指标 | `finetune.md:170`；`train.py:1095-1107`；`BaseSDTrainProcess.py:2487-2491,2565-2571`；`data_args.py:159-163` |
| **校验规则** | `loss` 枚举、`epochs/micro_batch/grad_accum` 正整数、`shuffle_options` 必须是题类型名 | `gradient_accumulation` 与 `gradient_accumulation_steps` **互斥**；`sampler` 必须等于 `noise_scheduler`；`save_format` 枚举 | 同生图 + `cache_text_embeddings` 与 `unload_text_encoder` **二选一** | `val_size` 与 `eval_dataset` **互斥**；`template` 空=失败；数据集名必须已注册 | `train.py:126-148`；`config_modules.py:29-30,463-465`；`train_lora_wan22_14b_24gb.yaml:58-64`；`data_args.py:159-163` |
| **步数/预估口径** | **不按步填**，由 `epochs × ceil(ceil(items/micro_batch)/grad_accum)` 算出 optimizer updates（例：5600 项 → **352**） | **直接填 `train.steps`**（默认 1000/示例 2000），**无 epoch 概念**；batch 默认 1 | 同生图，但每步含 `num_frames` 帧，成本按帧数放大 | `epochs` + batch + 累积 + 卡数；进度单位是 `global_step` | `finetune.md:96-114`；`train.py:607-632`；`config_modules.py:383,396`；`training_args.py:922` |
| **进度单位（最容易错）** | loss 行按 **micro-batch 步**打印（每 100 步），预算按 **optimizer updates** 报——**两个分母** | 进度条 `total = train.steps`，postfix 是 lr+loss | 同生图 | `global_step`（=优化器步） | `train.py:707-728` vs `607-619`；`BaseSDTrainProcess.py:2494-2501` |
| **「成果」是什么** | 无图；= loss 曲线 + 每轮 `checkpoint_latest` + 末尾 before/after 表 | **采样图**每 `sample_every` 步一组（每组 `len(prompts)` 张） | 采样**webp 片段**（每事件 `len(prompts)` 个） | 无图；= `checkpoint-N/` 每 `save_steps` + 末尾 `training_loss.png` | `finetune.md:117-120`；`BaseSDTrainProcess.py:300-309,2720-2738`；`workflow.py:131-140` |
| **预估读数从哪来** | 本机实践：**先跑 20 步 smoke** 实测 s/步与峰值显存，再传回 `--秒每步/--预计显存GB` 打印计划 | 无内建 ETA；只有可选 `performance_log_every` | 同生图 | 无内建 ETA；看进度条剩余 | `K/train_sel.py:185-188,209-219`；`train_lora_flux_24gb.yaml:11` |
| **危险开关** | `--freeze-encoder`（省显存但收益小） | `disable_sampling`（关掉唯一验收信号）、`low_vram` | `low_vram` + 双专家 | `save_total_limit` 默认 **None=不删档**（磁盘会一直涨） | `finetune.md:166`；`training_args.py:1000-1013` |

---

## 6 v1 原型错在哪（逐条点名）

> 对象：`E:\视频\modelsmith\design\prototype\archive\index-unreviewed-20261022.html`（只读复核，未改）
> 行号即该归档件行号（`FLOWS` 定义在 415–461，阶段条在 463）。

### 6.1 编造项（上游不存在，v1 自造）

| # | v1 写了什么 | 真实是什么 | 依据 |
|---|---|---|---|
| E1 | 决策底座 `Laya-Policy-S / -M / -Value-XL`（419） | 默认 `convaiinnovations/laya`；可选别名 `english/multilingual/typed-decisions` 或本地目录/Hub id | `finetune.md:162`；`train.py:540-570` |
| E2 | 决策产物 `policy.pt`（419） | `model.safetensors` + `encoder/` + `tokenizer/` + `rl_agent_config.json` + `questions.json` + `train_report.json` | `finetune.md:117-120`；`train.py:1239-1264` |
| E3 | 决策默认 `epochs=8, lr=3e-4, batch=64, max_len=512`（423） | `epochs=4`、`encoder_lr=2.5e-5`/`head_lr=1e-4`、`micro_batch=8 × grad_accum=8`（等效 64）、`max_len` **取 base config** | `train.py:91-95,112-113` |
| E4 | 生图架构含 **Z-Image**、底模 `sd_xl_base_1.0.safetensors`（442-443） | `config/examples/` 共 22 个示例文件（含 generate/extract/slider 等非 LoRA 训练件）；训练架构覆盖 flux/flux_schnell/flux_kontext/chroma/flex/flex2/hidream/lumina/omnigen2/qwen_image/qwen_image_edit/sd35_large/wan21_1b/wan21_14b/wan22_14b；**无 z-image、无 sdxl 训练示例** | `A/config/examples/` 目录清单 |
| E5 | 视频架构 `H3`、`LTX`、底模 `h3-13b`/`ltx-video-2b`（453-454） | 视频只有 `wan21_1b`/`wan21_14b`/`wan22_14b` 三类示例 | 同上 |
| E6 | 视频参数 `frames=33`、`resolution=720p`（457） | 字段是 `num_frames`（帧数）+ `resolution`（整数/列表，自动 buckets），**没有 720p 这种值** | `config_modules.py:956,1071-1074` |
| E7 | 统一阶段条 `PHASES=准备/加载权重/前向计算/反向传播/保存产物`（463） | 三类后端都不报这种阶段：Laya 报 `epoch e/E step n loss x`；ai-toolkit 报每步 postfix+`Saving at step N`；LLM 报 global_step 进度条 | `train.py:728,731`；`BaseSDTrainProcess.py:2703-2725` |
| E8 | LLM 默认 `rank=16, alpha=32`（435） | `lora_rank` 默认 **8**，`lora_alpha` 默认 **rank×2=16** | `finetune_args.py:77-80,69-72,568` |
| E9 | LLM 默认 `lr=2e-4`（435） | transformers 默认 **5e-5**（示例取 1e-4） | `training_args.py:915`；`qwen3_lora_sft.yaml:32` |

### 6.2 缺失项（真实契约里有、v1 完全没有）

| # | 缺什么 | 为什么致命 | 依据 |
|---|---|---|---|
| M1 | 决策的 `loss`(rlcd/soft-ce)、`shuffle_options`、`label_smoothing` | 这三个直接决定训练目标本身；UI 不暴露 = 断了一条上游主旋钮 | `finetune.md:163-166` |
| M2 | 决策的**校准 + 拒答阈值**（`target_error/min_abstain_n`，温度与 `abstention_thresholds` 产物） | 「校准是最容易被抄丢的一步，一旦有人用置信度设门就变成承重墙」 | `finetune.md:275-299`；`train.py:1138-1151` |
| M3 | 决策的**留出集**（`--eval`）与「after 来自校准切片=in-sample」的告知 | 不给 eval 时 v1 会让用户把 in-sample 数字当泛化 | `finetune.md:170`；`train.py:1095-1107,1199-1201` |
| M4 | 生图/视频的 `trigger_word` + `[trigger]` 写法、以及「缓存文本嵌入时 trigger 不生效」 | 用户最常见的失败就是「训完触发词没反应」 | `A/toolkit/prompt_utils.py:715-748`；`A/config/examples/train_lora_qwen_image_24gb.yaml:15`；`A/config/examples/train_lora_wan22_14b_24gb.yaml:15` |
| M5 | `sample.prompts` / `sample_every` / `sample_start_step` / `save_every` / `max_step_saves_to_keep` | 这是生图唯一的**过程验收信号**与产物保留策略；v1 全无 | `config_modules.py:25-27,84-85` |
| M6 | 生图的 caption 契约（同名 `.txt`、仅 jpg/jpeg/png、`caption_ext`） | 数据不合格时后端直接空跑/丢样本 | `train_lora_flux_24gb.yaml:29-35`；`config_modules.py:950-953` |
| M7 | LLM 的 `dataset_info.json` **注册**步骤 + `template` + `cutoff_len` | 不注册 dataset 名，训练启动即失败；template 缺省=None 也直接失败 | `data_args.py:26-41`；`dataset_info.json:1-18` |
| M8 | 三类共有的「数据体检」真实内容（决策 `--dry-run` 的 5 类跳过原因；生图 caption/扩展名；LLM 注册名解析） | v1 的「体检」只是一个步骤壳，没有判据 | `finetune.md:71-88` |

### 6.3 顺序错（步骤次序与真实契约不符）

| # | v1 次序 | 真实次序 | 依据 |
|---|---|---|---|
| S1 | 四类都套「架构→底模→数据→参数→确认开跑」 | 决策是**数据→体检→参数→训练→（自动）校准→（可选）评测**；生图/视频是**数据→参数（含采样计划）→训中边训边采样**；LLM 是**注册数据→模板→训练→每 500 步存**。三者的「末段」根本不同 | §1–§4 |
| S2 | 「数据体检」作为第 3 步但无产物 | 体检 = 起跑前**同一份 config** 的 dry-run，产出「items/跳过原因/更新预算」，可直接变成 UI 的预估数字 | `finetune.md:71-88`；`train.py:1282-1330` |
| S3 | 参数步骤恒定 4 个高级项 | 生图/视频的「参数」必须包含**采样计划**（否则训完没有中间图）；决策的「参数」必须包含 **loss 与 epochs**（决定更新预算），且 `epochs` 不能孤立填——步数由公式推出 | `config_modules.py:84`；`finetune.md:96-114` |
| S4 | 「确认并开始训练」是终点 | 真实流程在「开跑」**之后**还有一段 UI 必须承载：决策的校准/评测报告、生图/视频的采样图墙、LLM 的 checkpoint 列表 | `train.py:1170-1191`；`BaseSDTrainProcess.py:286-309`；`checkpoint.py:238-251` |

---

## 7 未验清单（不许当结论用）

- **未验**：Wan2.2 实际训练耗时/显存/双专家切换行为（本机无权重）。
- **未验**：ai-toolkit 的 loss 数值在何种量级算「正常」——源码只打印，不给阈值；本机未跑过生图训练。
- **未验**：「过拟合看采样图哪一档」是经验判断（§2.8），上游未给判据。
- **未验**：Laya `--loss rlcd` 在**本机数据**上的收益（上游只在 typed-decisions/MASSIVE 报数，`finetune.md:183-204`）。
- **未验**：Laya 是否能 resume（`train.py` 无 resume 命中；未做端到端实验）。
- **未验**：本机实践的温度校准只有单标量网格（`K/README:155-156`），与上游 LBFGS 分类型拟合**不是同一口径**。
- 已查无命中：`third_party/LlamaFactory` 内无 `hparams.py` 单文件（是 `hparams/` 包）；`laya/train.py` 无 `resume/patience/early`；`ai-toolkit` 无 resume 逻辑。
