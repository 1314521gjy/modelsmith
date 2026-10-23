# UI 流程契约 v2（按上游真实契约 · 维护者 裁决 2026-10-22）

- 唯一依据：`E:\视频\modelsmith\docs\TRAINING-FLOWS.md`（202 处 `文件:行号` 出处）。**本文件不新增事实，只做 UI 裁决与落点。**
- 上位优先级：`docs\CONTRACT-UI-v1.md`（技术接口）> 本文件（流程内容）> `docs\UI-BRIEF-MINI.md` > `design\DESIGN-SPEC.md`。
- 核心裁决：**四类训练不是一条流水线**。共用外壳（左步骤条 / 中一屏一问 / 右固定预估 / 底部单主按钮），但**步骤、字段、校验、进度单位、成果定义四套不同**。
- 批准：**维护者 于 2026-10-22 批准执行**（用户已全权授权，不再逐条等确认）。实施分 3 阶段：① 决策（task-6）→ ② 生图+视频（task-7）→ ③ LLM（task-8）；每阶段独立验收、独立快照。
- 对象：`E:\视频\modelsmith\design\prototype\index.html` 的 `FLOWS` 配置（只改配置与渲染分支，不重写框架）。

---

## 0 四条硬规矩（先钉死）

1. **进度分母必须说清是哪个分母**：
   - 决策 = **optimizer updates**（预算行）；loss 行里的 `step` 是 **micro-batch 步**，两者差 `grad_accum` 倍（默认 8）——UI 拿 loss 行当分母会画到 8 倍处。
   - 生图/视频 = `train.steps`；LLM = `global_step`。
2. **「成果」四类不同**：决策无图（loss + 每轮 checkpoint + 末尾 before/after 表）；生图/视频有采样图/片段；LLM 无图（checkpoint + 训练结束的 loss 图）。
3. **预估不许写成承诺**：决策优先「20 步 smoke 实测 s/步 + 峰值显存回填」；生图/视频/LLM 无内建 ETA，只能给公式估算并标「估算」。
4. **末段必须有地方承载**：开跑不是终点。决策要有校准/评测报告位；生图/视频要有采样图墙；LLM 要有 checkpoint 列表。

---

## 1 决策模型（Laya）· 4 步

| 步 | 标题 | 必填/关键字段 | 默认值（上游） | 校验（能变红） |
|---|---|---|---|---|
| 1 | 数据 | `data`（**单文件** JSONL/CSV）；CSV 列名 `text`/`label`；JSONL `{state,questions,expected|gold}` | — | 文件不存在 / 后缀不是 .jsonl/.csv / CSV 缺列名 → 拒 |
| 2 | 数据体检 | 调 `--dry-run`：读入行数、生成 item 数、跳过计数与原因（`empty_text/empty_label/invalid_question/invalid_target/options_collapsed/options_beyond_max_len`）、**更新预算** | — | 跳过率 > 0 必须逐类展示；items=0 → 阻止下一步 |
| 3 | 训练设置 | `base`、`loss`、`epochs`、`micro_batch`、`grad_accum`、`encoder_lr`、`head_lr`、`max_len/head_max_len`、`shuffle_options`、`label_smoothing`、`calib_frac/calib_max`、`target_error`、`min_abstain_n`、`eval_data`（可选） | base=`convaiinnovations/laya`；loss=`rlcd`；4 / 8 / 8；2.5e-5 / 1e-4；max_len **取 base ckpt 自身 config**（缺省 512/192）；calib 0.1/400；target_error 0.10；min_abstain_n 10 | loss 枚举；三个正整数；target_error∈[0,1]；label_smoothing∈[0,1)；`shuffle_options` 必须是已知题类型名 |
| 4 | 确认并开始 | 只读汇总：**updates = epochs × ceil(ceil(items/micro_batch)/grad_accum)**、每秒步、预计时长、峰值显存、产物清单 | — | updates=0 → 阻止；未跑体检 → 提示先体检 |

**运行页（与 v1 完全不同）**
- 主进度：`更新 u / U`（optimizer updates）+ `epoch e / E`。**不许**用 micro-batch 步当分母。
- loss 曲线：x 轴标「micro-batch 步（每 100 条一个点）」，标题旁写明「≠ 更新步，差 `grad_accum` 倍」。
- 事件：`train items N, calibration items N` → 每 epoch `mean loss` → 每轮重写 `checkpoint_latest/` → 末尾 `Evaluation (before vs after)` 表。
- **不出现采样图**（上游本就不生成）；不出现统一阶段条「准备/加载/前向/反向/保存」（三类后端都不报）。
- 取消：先提示**无 resume**——中断=从头再训；`checkpoint_latest/` 只能拿回上一轮。

**完成页**
- before/after 表（Metric / Before / After / Delta）+ `eval_source` 明示：用了 `--eval` 才是 held-out；没给 eval 时 after 来自**校准切片 = in-sample**，必须用黄条写「不是泛化数」。
- 校准温度（每类型；<10 项未拟合=保持 1.0 要标出）+ 拒答阈值 `abstention_thresholds`。
- 产物：`model.safetensors`、`encoder/`、`tokenizer/`、`rl_agent_config.json`、`questions.json`、`train_report.json`、`checkpoint_latest/`。

**本机参考读数（A 级，可作默认文案）**：1310 步 = 2 epoch × 655 步，7.7 分钟，峰值 16.3 GiB，0.34 s/步。

---

## 2 生图 LoRA（ai-toolkit）· 5 步

| 步 | 标题 | 必填/关键字段 | 默认值 | 校验 |
|---|---|---|---|---|
| 1 | 架构与底模 | `arch`、`model.name_or_path` | arch 枚举：`flux / flux_schnell / flux_kontext / chroma / flex / flex2 / hidream / lumina / omnigen2 / qwen_image / qwen_image_edit / sd35_large`（**无 Z-Image / 无 SDXL 训练示例**）；底模**必填** | 底模路径不存在 → 拒；arch 不匹配 → 红字阻止 |
| 2 | 数据与触发词 | `folder_path`；`caption_ext`；`trigger_word`；caption 写法（同名 `.txt`，可用 `[trigger]`） | caption_ext=`.txt`；trigger_word=`None`；只收 jpg/jpeg/png | 目录无图 / 有图缺 caption / 扩展名不支持 → 列清单；`cache_text_embeddings=true` 且设了 trigger → **警告「缓存文本嵌入时 trigger 不生效」** |

> **2026-10-22 增补（维护者 裁决，同 LLM 的 `data_path`）**：数据步新增 **`data_path`**（本地图片文件夹绝对路径，**优先于**缩略图模拟值）；提交时 `params.dataset = data_path || picks.data`。同时架构枚举补 **`sd1`**（ai-toolkit 原生支持，用于最快 smoke）。
| 3 | 数据体检 | 图片数、缺 caption 数、扩展名分布、尺寸分布、重复图 | — | 缺 caption>0 逐张列出；可用「一键生成空 caption」修 |
| 4 | 训练参数 | `steps`、`batch_size`、`lr`、`rank/alpha`、`resolution`（多分辨率 bucketing）、`grad_accum`、`save_every`、`max_step_saves_to_keep`、`sample_every`、`sample_start_step`、`sample.prompts`、`sample_steps`、`guidance_scale`、采样宽高 | steps=1000（官方示例都写 2000）；batch=1；grad_accum=1；save_every=1000（示例 250）；keep=5（示例 4）；sample_every=100（示例 250）；guidance=7（示例 4/3.5/3）；采样 512×512（示例 1024） | `gradient_accumulation` 与 `gradient_accumulation_steps` **互斥**；`sampler` 必须与 `noise_scheduler` 匹配；`save_format` 枚举；steps 必须 >0 |
| 5 | 确认并开始 | **采样计划预览**：首图 1 组 + `floor(steps/sample_every)` 组，每组 `len(prompts)` 张 | — | `sample.prompts` 为空 → 黄条「关掉采样 = 关掉唯一验收信号」；`steps % sample_every != 0` → 黄条「末步不出图，最终 LoRA 可能没有对应采样图」 |

**运行页**
- 主进度：`step / train.steps`；postfix 显示 `lr`、`loss`。
- **每 `sample_every` 步出现一组采样图**（首图在训练前）；图落 `samples/…`，视频为 animated webp。
- 事件：`Saving at step N`（save_every）与采样事件分开显示。
- 取消：SIGINT 正常停止（非报错）；源码未见 resume（未验）。

**完成页**
- 采样图墙：按 step 分组，同一 prompt 跨 step 并排对比（固定 seed）；这是**唯一验收信号**，不许用 loss 下降代替。
- 产物：终版 `<training_folder>/<name>/<name>.safetensors`、中途档 `…_000000250.safetensors`（受 `max_step_saves_to_keep` 限制）、`samples/`、训练配置、数据清单哈希。
- 黄条：末步未整除时，标注「最后采样停在 step X」。

---

## 3 LLM 微调（LlamaFactory）· 5 步

| 步 | 标题 | 必填/关键字段 | 默认值 | 校验 |
|---|---|---|---|---|
| 1 | 模型与模板 | `model_name_or_path`、`template` | template 默认 **None（必填）**；示例 `qwen3_nothink` | template 空 → 直接红（上游会失败） |
| 2 | 数据注册 | 数据集名（必须在 `data/dataset_info.json` 注册）、`alpaca`/`sharegpt` 列映射、`cutoff_len`、`val_size`（或 `eval_dataset`） | dataset_dir=`data`；formatting 缺省 alpaca；cutoff_len=2048；val_size=0.0 | 名字未注册 → 红；`val_size>0` 与 `eval_dataset` **互斥** |

> **2026-10-22 增补（维护者 裁决）**：本项目 LLM 后端是**文件式薄 runner**（`adapters\llm_lora.py`），不是 LlamaFactory 的注册表模式，所以数据步必须多一个 **`data_path`** 字段（本地 JSONL 绝对路径，**优先于** `dataset_name`）；`dataset_name` 保留作逻辑名/展示用。提交给核心时 `params.data = data_path || dataset_name`，不得再用 `eval_dataset` 冒充训练数据。
| 3 | 数据体检 | 解析注册名、列映射、token 长度分布、截断率 | — | 截断率 >0 展示；空回复/角色格式错列出 |
| 4 | 参数 | `stage`、`finetuning_type`、`rank/alpha`、`lr`、`epochs`、`batch`、`grad_accum`、`save_steps`、`logging_steps`、`save_total_limit` | stage=sft；lora；rank=8 / alpha=16；lr=5e-5（示例 1e-4）；epochs=3.0；batch=8；grad_accum=1；save_steps=500；logging_steps=500（示例 10） | rank/alpha 正整数；`save_total_limit=None` 要提示「默认不删档，磁盘会一直涨」 |
| 5 | 确认并开始 | 预计总 update 数、预计时长、产物路径 | — | 未注册数据集 → 阻止 |

**运行页**：进度 = `global_step`；loss 每 `logging_steps` 一条（默认 500，前 500 步看不到 loss 要提前说明）；每 `save_steps` 出现 `checkpoint-N/`；**无采样图**。
**停/续**：支持 `resume_from_checkpoint`（与决策相反，UI 要显示「可续训」）。
**完成页**：终版 adapter、`train_results.json`、`training_loss.png`、`checkpoint-N/` 列表；有 `eval_dataset` 才显示验证指标。

---

## 4 视频 LoRA（Wan2.2）· 5 步

> **2026-10-22 裁决：视频功能砍掉，只保留入口且暂不可选。** 本节保留为**将来开放时的契约**，但当前**不实现、不可达**：首屏视频卡留位并标「暂未开放」（`aria-disabled="true"`、`tabindex="-1"`），点击不进入流程。交付物里不出现 Wan 训练。

与生图共用外壳，差异如下：

| 步 | 差异 |
|---|---|
| 1 架构与底模 | arch 只有 `wan21_1b / wan21_14b / wan22_14b`（**无 H3 / LTX**）；`qtype`=ARA（4bit 训练）；`cache_text_embeddings` 与 `unload_text_encoder` **二选一** |
| 2 数据 | 文件夹 + 视频 + 同名 caption；`num_frames`（>1 时加载器改找视频）、`shrink_video_to_frames`（默认 true，均匀抽帧）、`fps`（默认 24，仅 shrink=false 时用） |
| 3 体检 | 片段数、总时长、分辨率、帧率、缺 caption、损坏 |
| 4 参数 | 生图全部 + `num_frames`、`switch_boundary_every`（默认 10，Wan2.2 MOE 高/低噪专家切换）、`train_high_noise/train_low_noise` 同开 |
| 5 确认 | 显存建议（降分辨率/减帧数）；采样产物是 **animated webp 片段**（`sample.num_frames>1` 时自动改扩展名） |

**节奏**：完全沿用 `sample_every`/`save_every`；每采样事件出 `len(prompts)` 个 webp 片段，不是单图。**未验**：Wan2.2 显存/耗时/双专家切换（本机无权重）。

---

## 5 四类差异速查（UI 必须落地的 10 行）

| 维度 | 决策 | 生图 | 视频 | LLM |
|---|---|---|---|---|
| 数据形态 | 单文件 JSONL/CSV | 文件夹（图+同名 txt） | 文件夹（视频+caption） | 已注册数据集名 |
| 必填底模 | 可留空（有默认） | 必填 + arch | 必填 + arch=wan22_14b | 必填 + template |
| 进度单位 | optimizer updates（loss 行是 micro-step） | train.steps | train.steps | global_step |
| 成果节奏 | 每 epoch checkpoint；末尾 before/after | 首图 + 每 sample_every 一组 | 同生图（webp） | 每 save_steps checkpoint；末尾 loss 图 |
| 中途图 | 无 | 有 | 有（webp） | 无 |
| 预估来源 | 20 步 smoke 实测回填 | 公式估算 | 公式估算（按帧放大） | 公式估算 |
| 可续训 | 无（中断=从头） | 未验（源码未见） | 未验 | 有 resume |
| 危险开关 | freeze_encoder 收益小 | disable_sampling=关验收 | low_vram+双专家 | save_total_limit=None 不删档 |
| 完成页核心 | 校准温度+拒答阈值+before/after | 采样图对比 | 采样片段对比 | adapter+loss 图+checkpoints |
| 必须出现的警告 | in-sample ≠ 泛化 | trigger 缓存失效 / 末步不出图 / 空 prompts | 同生图 | 前 500 步无 loss / 不删档 |

---

## 6 落到 `FLOWS` 配置的形状（给框架席）

```js
FLOWS = {
  decision: { title, accent, steps: [
    {k:'数据', type:'file', field:'data', accept:['.jsonl','.csv'], columns:{text:'text',label:'label'}},
    {k:'体检', type:'dryrun', field:'check', runtime:'laya --dry-run'},
    {k:'设置', type:'params', groups:['optim','calibration','eval'], fields:[...]},
    {k:'开始', type:'review', estimate:'updates'}
  ], run:{unit:'updates', extra:'epoch', showSamples:false, cancelWarn:'无 resume，中断=从头'},
     done:{panels:['before_after','calibration','abstention','artifacts']} },

  image: { ..., steps:[arch, data, check, params, review],
     run:{unit:'steps', showSamples:true, sampleEvery:true, cancelWarn:'正常停止'},
     done:{panels:['samples','artifact','config','lastSampleWarn']} },

  video: { ... 同 image，arch 枚举不同，采样 ext='webp' },
  llm:   { ..., steps:[model, data, check, params, review],
     run:{unit:'global_step', showSamples:false, resume:true},
     done:{panels:['adapter','loss_png','checkpoints','eval_if_any']} }
}
```

- 只允许**改配置字段与新增渲染分支**；骨架（步骤条/中心/预估/底部按钮）、类名、状态机（`flowState`）不动。
- 新增控件必须复用 `.field/.seg/.dropzone/.est/details.adv` 这套类，不新造视觉语言。
- 现有 5 步 vs 新 4/5 步的差异由配置数组长度自然处理；**不许**再出现「四类共用一套阶段条」。

---

## 7 验收挂钩

- B1/B2/B3/B4 的步骤断言改为本文件 §1–§4 的步骤名与顺序。
- 新增：
  - **B10** 决策运行页**不出现采样图**，且进度分母标注为「updates」。
  - **B11** 生图/视频运行页在 `sample_every` 边界出现采样组；完成页有图墙。
  - **B12** 生图 `steps % sample_every != 0` 时出现末步警告。
  - **B13** LLM 完成页有 checkpoint 列表与 loss 图；`save_total_limit=None` 有提示。
  - **B14** 决策完成页在无 `--eval` 时必须出现「in-sample，不是泛化」黄条。
- P0 口径不变：这些新判据里，**B10/B11/B14 为 P0**（否则等于把流程做回 v1 的编造版）。
