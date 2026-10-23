---
name: modelsmith-mcp
description: 用 ModelSmith 的 MCP 接口（19 个 modelsmith_* 工具）驱动本地训练：列模型/选底座/提交（细参数）/轮询与尾随日志/取消/评测（eval）→ 报告（report）→ 取产物与采样图（artifacts/sample）→ 标记候选（star）→ 早停（early_stop）/打开产物（open dry）/下载模型；含欠拟合与过拟合的判断与加码建议。
when_to_use: 当需要为 ModelSmith 提交、监控、取消训练任务（Laya 决策模型 / LlamaFactory LLM / DiffSynth 生图 / ai-toolkit 生图视频），或要判断「该不该加 epochs/lr/grad_accum、要不要补 eval_data」，或训练完要做评测/报告/取采样图/标记候选/早停时。
---

# ModelSmith MCP 训练流程

> 工具清单与逐字段参数表在 `E:\视频\modelsmith\docs\MCP-TOOLS.md`（本文只给**动作顺序**与**判断口径**，不复制那张大表）。
> 服务端：`E:\视频\modelsmith\mcp_server\modelsmith_mcp.py`；核心：`E:\视频\modelsmith\core\modelsmith_core.py`。

## 0 两条红线（违反即失败，没有例外）

1. **同一时刻只允许一个 GPU 作业。** 提交前必须 `modelsmith_list_jobs` 看 `state="running"` 是否为空；
   有 running 就 `modelsmith_wait_job` 等它，或者明确告知用户"排队"，**不许直接塞第二个**（核心有 GPU 锁，但排队不是并行许可）。
2. **不许传 `train_command`。** 它是核心自测钩子（能执行任意子进程），MCP 会硬拒并返回
   `train_command is a test hook, not allowed via MCP`。被拒后**不要换别的字段绕**，改掉参数重提。

## 1 流程（每步都有判据）

1. **起核心**（一次）：默认 `http://127.0.0.1:8765`；用 `modelsmith_health` 确认 `ok:true`。
   连不上 → 让用户先跑 `python E:\视频\modelsmith\core\modelsmith_core.py`（MCP 不负责起服务）。
2. **体检**：`modelsmith_env_doctor`。判据：`cuda.status="ok"`、`torch.version` 有值、`disk.free_gb` 足够。
3. **列模型 / 选底座**：`modelsmith_list_models`（可带 `kind`）。
   - `ok:true` → 用返回的 `id` 当 `model`/`base`。
   - `ok:false` + `note:"core 未提供 /api/models"` → 降级：用 `resources\` 下已知本地路径（`resources\03-LLM-小模型\Qwen2.5-0.5B-Instruct` 等），**不要重试同一个工具**。
4. **确认没有在跑的作业**（红线 1）：`modelsmith_list_jobs`。
5. **提交**：`modelsmith_submit_job {kind, params}`（字段照 `docs\MCP-TOOLS.md` §3）。
   判据：`ok:true` + 拿到 `job_id`；`state` 通常是 `queued`。
6. **跟踪**：
   - 粗跟踪：`modelsmith_get_job {id}` 看 `state`/`metrics`（长任务每 10–30 s 一次就够）。
   - 细跟踪：`modelsmith_job_logs {id, offset=上次的 next_offset}` 循环尾随；**只追加，不重拉整段**。
   - 等终态：`modelsmith_wait_job {id, timeout_s}`；`timed_out:true` 只说明等超时，**任务还在跑**。
7. **取消**（可选）：`modelsmith_cancel_job {id}` → `wait_job` 到 `cancelled`。
   **没有 resume**：停下就是从头再训；`checkpoint_latest/` 只是上一轮产物，不能接着训。取消前先问用户。
8. **读产物**：`get_job` 的 `metrics.artifact` / `artifacts`，或到 `params.out` 下找：
   - laya：`model.safetensors` / `encoder/` / `tokenizer/` / `rl_agent_config.json` / `train_report.json` / `checkpoint_latest/`
   - lf：`adapter_model.safetensors` + `adapter_config.json` + `trainer_log.jsonl`
   - diffsynth：`epoch-0.safetensors`
   - aitk：`<out>\<name>.safetensors` + 采样图
   判据：文件存在且 `metrics.state="succeeded"`；**再抽 1 条日志确认没有 `Traceback`**。

## 2 细参数怎么填（只给决策点，完整表见 docs §3）

| 想做的事 | 改哪个 | 怎么改 |
|---|---|---|
| 先证明管线通（smoke） | laya `epochs=1`；lf `max_steps=2`；diffsynth `epochs=1` | 其余全默认；**只看管线不看效果** |
| 显存不够 | laya `micro_batch` 8→4→2；lf `batch=1`；diffsynth 降 `height/width` | `grad_accum` 反向补有效 batch |
| 数据超长被截断 | lf `cutoff_len` 512→1024/2048 | 同时注意显存 |
| 要验收采样图 | aitk `sample_prompts` 非空 + `sample_every` | 空 prompts = 关掉唯一验收信号 |
| 要泛化数（不是 in-sample） | laya `eval_data` 指向留出集 | 不填就只能报"校准切片"的 in-sample 数 |

## 3 欠拟合 / 过拟合：怎么判断、怎么加码

**先看证据，再动参数。** 每次只改一项，改完重跑 smoke 或小 epoch 验证。

### 3.1 欠拟合（模型还没学会）

判据（任一）：
- laya：`epoch_mean_loss` 到最后一个 epoch **仍在明显下降**；有 `eval_data` 时 `evaluation` 的 after 准确率仍低。
- lf：`metrics.loss` 末 20% 均值仍显著高于前 20% 之后的平台期（还在降）；`grad_norm` 长期很大。
- diffsynth/aitk：`loss` 不降或采样图与目标完全不沾边。

加码顺序（从便宜到贵）：
1. **`epochs` +2**（最直接；先确认 loss 曲线还有下降空间，否则白烧）。
2. **`lr` 上调 1.5–2×**（loss 平得早、梯度小）；lf 2e-4→4e-4、laya encoder 2.5e-5→5e-5。
3. **有效 batch**：`grad_accum` 8→16（laya）或 lf `grad_accum` 1→2；噪声大时有效 batch 比 lr 更稳。
4. **容量**：lf `rank` 8→16（`alpha` 同比例）；diffsynth/aitk `rank` 同上。
5. **数据**：样本 <几百条时欠拟合常常是数据不够 —— 先补数据，不要一直加 epochs。

### 3.2 过拟合（学会了训练集、丢了泛化）

判据：
- laya：`epoch_mean_loss` 继续降，但 `evaluation` 的 after 指标**变差**或 `target_error` 达不到；校准温度偏离。
- lf：train loss 降而验证 loss（有 `eval_dataset` 时）升；或 loss 掉到很低但采样输出开始背模板。
- diffsynth/aitk：采样图越来越像训练图原样、prompt 变化不响应。

减码顺序：
1. **`epochs` −1~−2**（最有效）。
2. **`lr` 下调 0.5×**；laya 可加 `label_smoothing=0.05`、开 `shuffle_options=true`。
3. **正则**：lf `lora_dropout` 0→0.05；rank 别急着降（先降 epochs/lr）。
4. **验收口径**：用留出集 `eval_data`（laya）/ `eval_dataset`（lf）看数，别只看 train loss。
5. **数据**：去重、清洗模板化样本。

### 3.3 拒答/校准（只有 Laya）

- `target_error`（默认 0.10）：**错误率目标**。要更保守（少答、少错）→ 调小；要覆盖更多 → 调大。
- `min_abstain_n`（默认 10）：小类目桶太小导致阈值抖 → 调大。
- 校准温度：**样本 <10 项时不拟合，温度固定 1.0** —— 看到 1.0 要知道这是"没拟合"，不是"拟合好了"。
- 不填 `eval_data` 时 completion 页的 before/after 来自**校准切片（in-sample）**，不是泛化数 —— 必须向用户说明。

## 4 评测 → 报告 → 标记候选 → 打开产物

训练到 `succeeded` 之后（**只有终态 job 能评测**）：

1. **曲线**：`modelsmith_job_metrics_history {id, since}` 增量拉（`since` = 上次的 `next_seq`）。长训用它看趋势，别反复 `get_job` 拉整份。
2. **评测**：`modelsmith_eval_job {id}`（可选 `data` 指向留出集）。本次核心走 **b 路径**：
   - 返回 `metrics`（accuracy/loss/ece/brier_top1/mean_conf/abstain_rate）+ **`per_question: []`** + `note:"本次训练未落逐条预测"` —— 别把它当"逐条可解释"。
   - **不传 `data` ⇒ `data_source="calibration"`（in-sample，不是泛化数）**；要泛化数必须传留出集。
   - 非终态 job → 409（`isError`）；job 没有 `train_report.json` → `ok:false`（状态答案，不是崩溃）。
3. **报告**：`modelsmith_report_job {id, model?}` → `engine:"template"`（总体 / 错例 / 归因 / 建议 四段 + in-sample 提醒）。
   **必须先 eval**，否则 `{ok:false,error:"run /eval first"}`。若返回 `warn:"ungrounded_numbers"`，说明模板里的数字和 metrics 对不上 —— 别照抄。
4. **标记候选**：`modelsmith_star_artifact {id, artifact:"adapter_model.safetensors"}` → 进 job 的 `starred_artifacts`；换候选时先 `starred:false` 撤掉旧的。
5. **确认产物目录**：`modelsmith_open_path {path}` —— **默认 `dry=true` 只校验**（白名单：`models_dir`、`_runs`、项目根；`C:\Windows` 之类 403）。**不要传 `dry=false`**；真要弹窗必须用户明说。
6. **写结论**：必须同时写清三件事 —— `data_source`（calibration / heldout）、`engine`（template / llm）、`warn`。缺任一项不许写"指标达标"。

## 5 取产物 / 采样图 → 标候选 → 早停

1. **产物清单**：`modelsmith_job_artifacts {id}` → `artifacts:[{name,path,bytes,sha256}]`
   （含 `*.safetensors` 与 `samples/**`）。判据：要交的 adapter/采样图都在列表里，`bytes>0`。
2. **采样图**：`modelsmith_job_sample {id, path}` —— 默认**只回元数据**（`bytes`/`content_type`，不传二进制）；
   需要像素才 `include_b64:true`（**≤2 MiB**，更大的会被 413 拒，改成本地按 `path` 读）。
   aitk/diffsynth 每 `sample_every` 一组，按 step 逐组抽看。
3. **标候选**：`modelsmith_star_artifact {id, artifact:"adapter_model.safetensors"}`；换候选先 `starred:false` 撤旧的。
   `get_job.metrics.starred_artifacts` 里能看到当前标记。
4. **早停**（只在长训时）：`modelsmith_early_stop {id, enabled:true, patience:2, min_delta:0.01}`。
   - 默认 **`enabled:false`**（现有行为不变）；开了之后连续 `patience` 个 epoch 的 `epoch_mean_loss` 改善 < `min_delta` 就写 stop 文件。
   - 触发后 job 会往 `cancelled`/停止走，`metrics.early_stop` 里有记录；**无 resume**，要接着训只能新开 job。
   - 判据：开早停前先确认 `metrics_history` 里 loss 已进入平台期，否则会被过早停掉。
5. **打开目录**（只在用户明说时）：`modelsmith_open_path {path}` 默认 `dry:true` 只校验；
   `modelsmith_pick_path` 默认也是 dry（不弹窗），要真实选择必须 `dry:false` 且本机有桌面会话。

## 6 完成前自查

- [ ] `modelsmith_wait_job` 到 `succeeded`（不是 `timed_out`）。
- [ ] `metrics` 里有 step/loss，且 loss 是有限值（`null`/`NaN` 要报）。
- [ ] `out` 目录有产物（按 kind 对表），且**没有** `Traceback` 落在日志里。
- [ ] 没传 `train_command`；同一时刻只有一个 GPU 作业。
- [ ] 汇报时给：job_id、state、loss 首末/曲线、耗时、产物绝对路径、任何 `warnings`（末步不整除、空 prompts、小样本温度=1.0）。
