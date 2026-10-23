# UI 架构与功能接线设计 v4（2026-10-22 · 讨论汇总，先记录后实装）

> 这份文件是**设计与接线台账**，不是实装记录。当前实装进度见 §6「阶段」列。
> 上位/相关：`docs\UI-FLOWS-v2.md`（四类训练的内容契约，事实来自 `TRAINING-FLOWS.md`）、`docs\UI-BASELINE-v1.md`（旧单文件原型的冻结链）、`design\prototype\v3\index.html`（**新交互原型，PPT 式**）。
> 一句话：**v2 定义"每类训练要问什么"，本文件定义"怎么一页一页问、怎么接线、哪些交给 AI"。**

---

## 1 原则（用户裁决，优先级最高）

1. **PPT 式线性引导**：一页只放一件事；**当前页看不到下一页的内容**，不摊开大纲；可回退，不许跳步；页面横向推进转场（220–320ms）。
2. **默认参数即可用**：界面只给默认值（epochs/批次/学习率等），不把细参数摊给普通用户。
3. **细参数交给 AI/MCP**：同一个训练任务，界面提交的是"默认包"，Agent 通过 **MCP** 提交"细参数包"（同一份 core API）。
4. **模型只在包内找**：模型仓 = 安装目录下 `models\`，只扫这里，不扫 ComfyUI/系统盘；扫到的自动出现在"选模型"页。
5. **缺什么就让用户补**：缺模型 → 行内"下载"（多源依次尝试，显示进度/速度/剩余时间，可取消）；缺格式知识 → 技能包 + 校验器。
6. **视觉**：白色/深色双档，磨砂玻璃半透明质感；悬停"挤占"（被悬停卡占一整列，其余三张让位）；`prefers-reduced-motion` 全关。
7. **不撒谎**：进度分母写清是 updates 还是 micro-step；in-sample 必须标"不是泛化"；无 resume 必须提前说。
8. **视频线已砍**（用户语："视频砍掉""保留入口，暂不可选"）：入口留位、禁用、不可达。
9. **同一时刻只允许一个 GPU 作业**：界面与 MCP 都必须先查 `running` 再提交。

---

## 2 交互范式（v3）

```
首屏（四卡） ──点卡──► ① 选模型 ──► ② 选数据 ──► ③ 训练（含完成态）
                          ▲   │          ▲   │        │
                          └───┘          └───┘        └─ 返回=回到数据页（训练继续）
```

- 每页结构固定：`左上返回 · 流程名 + 第 N 步/共 M 步 · 右上 环境/设置/Agent 接入 | 一屏一问 | 底部 上一步 / 下一步`。
- 决策线步数：**3 步**（模型 → 数据 → 训练）。用户明确要"先跑起来"，数据体检与参数页**暂不独立建页**（默认参数 + 运行页承担解释）；如需可后续插页。
- LLM / 生图线沿用同一范式（页数按各自契约），视频线禁用。
- 全部页面共用：深/浅主题切换、玻璃材质、入场错峰、焦点环、键盘可达。

---

## 3 逐页规格（v3 现状）

### 3.1 入口页（已实现）
- 标题「今天想训练什么？」居中大字；2×2 四卡等权、占满视口、无滚动条。
- 四卡：决策模型 / LLM 微调 / 生图 LoRA / 视频 LoRA（**禁用**：`aria-disabled` + 「暂未开放」）。
- 悬停挤占：被悬停卡占 62% 宽满高，其余三张在另一列纵排；移走 320ms 复原。
- 右上：`深色/浅色`（记忆在 `localStorage.ms-theme`，默认跟随系统）、环境、Agent 接入。

### 3.2 决策 ① 选择 Laya 模型（已实现，静态数据）
- 四个选项 + 每行「适合」一句人话：Laya 标准版（已装好 842 MB）/ Laya 题型版（已装好 842 MB）/ Laya 多语言版（**需下载 1.1 GB**，带「推荐」标）/ 用我自己的模型文件夹。
- 选中才放行「下一步」。
- **缺模型的下载**（已实现）：点「下载」→ 行内确认面板（3 个源按序尝试 + 体积 + 落盘位置 `models\<id>` + 可续传）→ 确定后进度条 + 百分比 + 已下/总量 + 实时速度 + 剩余时间；第一个源失败自动切下一个；可取消；完成变「已装好」并可选中。
- **待接线**：列表改为核心 `GET /api/models` 的真实结果（`models\` 扫描），下载走 `POST /api/models/{id}/download` 事件流。

### 3.3 决策 ② 选择训练数据（已实现，静态提示）
- 拖放区（`.jsonl`/`.csv`，单文件）+「选择文件」+「用示例数据试跑」。
- 选中后文件卡（名/路径/大小/条数）+「换一份」。
- **语言提示**：语料是中文 → 黄条「底座建议换『Laya 多语言版』」+「回上一步换底座」。
- 折叠「格式怎么写？」：JSONL = `{state, questions, expected}`；CSV = `text/label` 两列；只读单文件。
- **待接线**：真实路径 + 行数/坏行统计（核心 dry-run/体检）；示例数据换成 `resources\01-决策模型-业务语料\` 里的中文客服样例。

### 3.4 决策 ③ 训练运行页（已实现，模拟数据）
- 顶部摘要：模型 / 数据 / 当前 loss / epoch。
- **指标卡（4 个）**：步数（micro-step，`x / 704`）、更新（optimizer，`x / 88`）、显存占用（`16.x GiB / 32 GiB`）、用时/预计/剩余（`0:42 / 1:36 · 剩 0:54`）。
- **进度条**（按 updates 百分比）。
- **loss 拟合曲线**（SVG，横轴 micro-step、每 100 条一个点；纵轴 loss；epoch 分隔刻度）。
- 日志尾随区（update/epoch/loss/lr 行）。
- 完成态：before/after 表（Accuracy 0.5938→0.7917、Loss 1.0355→0.6288、ECE 0.2211→0.1145、Brier 0.3985→0.1749、Mean Conf 0.8031→0.7241）+ 校准温度 1.00（未拟合说明）+ 拒答阈值 0.42 + 产物 `model.safetensors`。
- 底部：`取消训练`（二次确认文案：**无 resume，中断=从头**）/ `查看产物`。
- **待接线**：接核心 job 事件（`updates/updates_total/epoch/loss/epoch_mean_loss/evaluation`），曲线点来自日志 offset 增量；显存来自 `nvidia-smi` 采样（核心目前不做，见 §5 未决）。

### 3.5 LLM / 生图线（待做）
- LLM：模型 → 数据（`data_path` 必填）→ 训练（`global_step` 分母、loss、checkpoint 列表；`max_steps=2` 标注"smoke"）。
- 生图：架构 → 底座（扫描）→ 数据 → 参数 → 训练（`step/total`、`s/it`、每 N 步采样图墙）。
- 两线共用 v3 外壳；视频线保持禁用。

---

## 4 功能接线台账

| # | 功能 | 现状 | 归属层 | 契约 / 判据 | 阶段 |
|---|---|---|---|---|---|
| F01 | PPT 式线性引导 + 不可跳步 | ✅ v3 | UI | 每页只渲染当前步；返回保持状态 | 已实装 |
| F02 | 深/浅主题（记忆 + 跟随系统） | ✅ v3 | UI | `data-theme` + `localStorage.ms-theme`；不闪白 | 已实装 |
| F03 | 磨砂玻璃材质（白/深两档） | ✅ v3 | UI | 玻璃后面必须有内容（网格 mesh + 颗粒），否则无质感 | 已实装 |
| F04 | 悬停挤占（其余卡让位） | ✅ v3 | UI | 逐卡绝对定位；禁用卡不参与；320ms | 已实装 |
| F05 | 视频线禁用入口 | ✅ v3 | UI | `aria-disabled` + `tabindex=-1` + 点击/键盘/hash 全不可达 | 已实装 |
| F06 | 决策三页（模型/数据/训练） | ✅ v3 静态 | UI | 三页流转、状态保留、示例数据可跑 | 已实装（静态） |
| F07 | 缺模型下载（多源+进度/速度/取消） | ✅ v3 模拟 | UI→core | `POST /api/models/{id}/download`，事件 `attempt/progress/switch/done/failed` | 设计完成，待后端 |
| F08 | 模型仓扫描 | ✗ | core | `GET /api/models`（内容判定 kind）；**只扫 `models\`** | 设计完成 |
| F09 | 语料格式技能 + 校验器 | ✅ | skills/tools | `skills\laya-corpus\SKILL.md` + `tools\validate_laya_corpus.py`（PASS/FAIL） | 已实装 |
| F10 | 语料语言→底座建议 | ✅ v3 | UI | 选完数据出黄条 + 回上一步 | 已实装（规则式） |
| F11 | 运行页四指标（步数/更新/显存/时间） | ✅ v3 模拟 | UI←core | 分母必须标清 updates vs micro-step；时间=估算 | 已实装（模拟） |
| F12 | loss 拟合曲线 | ✅ v3 模拟 | UI←core | 横轴 micro-step、每 100 条一点；epoch 刻度 | 已实装（模拟） |
| F13 | 取消（无 resume 提示） | ✅ v3 | UI→core | `POST /api/jobs/{id}/cancel`；终态 `cancelled` | 已实装（模拟） |
| F14 | 完成态 before/after + 校准 + 拒答 | ✅ v3 静态 | UI←core | in-sample 必须标"不是泛化" | 已实装（模拟） |
| F15 | 早停（可选，默认关） | ✗ | core | 连续 N epoch `epoch_mean_loss` 改善 <1% 或 held-out 不升 → stop 文件 → `cancelled_by_early_stop` | 设计完成（用户未拍板是否要） |
| F16 | 细参数给 AI/MCP | ✗ | MCP | 9 个工具（health/env/…/wait）；**拒收 `train_command`** | 设计完成，实装延后 |
| F17 | 语言提示的规则升级 | ✗ | core | 读语料前 N 行判语言/字段，返回 `lang_hint` | 设计完成 |
| F18 | 产物打开 / 再训一次 | ✗ | UI→core | 打开目录（核心端点或 shell）；再训=带上次参数回填 | 待设计 |
| F19 | 环境体检/设置/Agent 接入抽屉 | 半成品 | UI→core | `/env/doctor`；MCP 状态显示"未连接"直到配置 | 待接线 |
| F20 | 训练中"返回不中断" | ✗ | UI | 返回=训练继续，图标/状态在首屏可见 | 待设计 |
| F21 | 多 GPU 作业并发保护 | 部分 | core+UI+MCP | 提交前查 `running`；核心队列串行 + GPU 锁 | core 已有锁，UI/MCP 待加提示 |
| F22 | 训练曲线与日志落盘归档 | ✗ | core | 每 job 保留 `metrics.jsonl` + `log`，供完成页回放 | 待设计 |
| F23 | 测试页（测试参数 + 问答效果表） | ✅ v3 演示 | UI→core | 手动跑：指标 4 项 + 逐条 题目/答案/标注/一致/置信度 | 已实装（演示数据） |
| F24 | AI 测试并出完整报告 | ✅ v3 演示 | UI→core（内置 LLM） | 5 步过程可见 → 指标 + 错例 + 归因 + 建议；报告必须引用真实读数 | 已实装（演示） |
| F25 | 内置 LLM 生成报告 + 模板兜底 | ✗ | core | `--report-llm` 默认 `resources\03-LLM-小模型\Qwen2.5-0.5B-Instruct`；LLM 不可用/超时 → 确定性模板，功能不降级 | 设计完成 |
| F26 | 报告导出（Markdown + JSON） | ✅ v3（md） | UI / core | `laya-eval-report.md` + `.json`（同一份数据两个视图） | 已实装（md） |
| F27 | 加训页（第二段训练） | ✅ v3 | UI→core | 起点二选一（上一版产物 / 原始底座）；`base` 指 `_runs\ui-laya-*` 时核心走"第二段训练"；**不是 resume**，校准/阈值重拟合；默认 epochs 2、旧数据回放 20% | 已实装（演示） |
| F28 | LLM 线页面（简约版） | ✅ v3 第 1 步 | UI→core | 三步：选模型 → 选数据 → 训练；模型行显示规模/上下文/显存级；缺模型可下载；模板/rank 等**只读展示默认值**，细参数走 MCP | 已实装（第 1 步；数据页待做） |

---

## 5 接口契约（接线目标）

### 5.1 现有（核心已实现，可直接用）
| 端点 | 说明 |
|---|---|
| `GET /health` | 免鉴权健康检查 |
| `GET /env/doctor` | Python/torch/CUDA/磁盘/端口体检 |
| `POST /api/jobs` | `{kind, params}` → `{job_id,state,queue_position}` |
| `GET /api/jobs`、`GET /api/jobs/{id}` | 作业列表/详情（含 `metrics`） |
| `GET /api/jobs/{id}/logs?offset=&max_lines=` | 日志增量（offset 字节） |
| `POST /api/jobs/{id}/cancel` | 取消（软停→taskkill） |
| `GET /` | 托管 UI，内存注入 `window.__MS_TOKEN__` |

四种 kind 的 params 见 `core\README.md`：§13.3（laya）、§15.2（aitk）、§16.2（diffsynth）、§17（lf）。

### 5.2 需要新增（按本台账）
```
GET  /api/models?kind=diffusion|llm|laya
     → {ok,root,scanned_at,models:[{id,name,path,kind,size_bytes,files,arch_hint}]}
POST /api/models/{id}/download
     body {sources:[...], resume:true}
     → 事件流（或轮询 /api/models/{id}/download/status）：
       {type:"attempt", source, i, n}
       {type:"progress", bytes, total, pct, speed_bps, eta_s}
       {type:"switch", from, to, reason}
       {type:"done", path} | {type:"failed", errors:[...]}
GET  /api/jobs/{id}/events?since=<seq>     # 结构化事件（比日志解析稳）
     事件：{seq, ts, type:"updates|micro_step|epoch|evaluation|sample|artifact|warning", data}
POST /api/jobs/{id}/early_stop             # 可选早停开关（F15）
```

**事件语义（必须写清分母）**
- laya：`updates`/`updates_total`（optimizer），`micro_step`（loss 行），`epoch`，`epoch_mean_loss`，`evaluation{in_sample|held_out}`
- lf：`step`/`total`（HF global_step）、`loss`、`lr`、`peak_vram`
- diffsynth：`step`/`total`（iteration）、`sec_per_it`、`artifact`、`sample_events[{step,files}]`
- 通用：`phase`（starting/training/checkpoint/evaluation/finished）

---

## 6 前端状态模型（v3 现状 → 目标）

```
state = {
  flow: "decision" | "llm" | "image" | null,
  step: 0..N-1,
  params: { base:"", data:"", out:"", ... 默认包 },
  ui: { theme:"light|dark", reducedMotion:bool },
  job: { id:null, state:null, metrics:{}, logOffset:0, pollTimer:null }
}
```
- 页面切换只改 `deck` 的 class（`is-dec1/…`），**不重载**。
- 提交后：轮询 `GET /api/jobs/{id}`（800ms）+ `logs?offset=` 增量；终态停轮询。
- 默认参数包（界面用）：laya `{epochs:4, micro_batch:8, grad_accum:8}`；lf `{max_steps:2, rank:8, alpha:16, cutoff_len:512}`；diffsynth `{rank:8, epochs:1, lr:1e-4, size:512}`。

---

## 7 分期

| 阶段 | 内容 | 出口判据 |
|---|---|---|
| **P1（现在）** | 本文件记录 + v3 三页视觉/交互定稿 | 用户逐页确认；静态四条 + 无滚动条 + 键盘可达 |
| P2 | 接核心：真实提交/轮询/日志/取消（decision 优先） | UI→core→laya 真跑 1 epoch，指标卡/曲线/完成态读数与 `metrics` 一致 |
| P3 | 接核心：`/api/models` 扫描 + 下载事件流 | `models\` 放模型 → 列表出现 → 选中可提交；缺模型下载走真下载器 |
| P4 | LLM / 生图两线套用同壳 | LF 2 步真 e2e；Qwen 2.1 采样图墙出现 |
| P5 | MCP 工具面（F16） | Agent 通过 MCP 完成一次 smoke + 取消，`selftest_mcp` 绿 |

---

## 8 未决问题（需用户/后续拍板）

1. **早停是否要**（F15）：默认关、开关放界面还是只给 MCP？
2. **设置/环境抽屉**里要不要暴露"GPU 显存上限/并发数"？
3. 决策线的**数据体检页**是否独立成页（现在压进运行页与提示里）？
4. **生成式业务话术**：Laya 只做决策，"给客户的解释"是走标签模板还是接 LLM 线？
5. 训练中返回首屏后的**作业卡片**放哪（首屏最近任务？顶部状态条？）。
6. 模型仓允许用户**改路径**（如放 D 盘）吗？默认只读 `models\`。
7. 内置报告 LLM 用哪个：默认 `Qwen2.5-0.5B-Instruct`（本机已有，弱但够用模板化总结）；要不要允许换成更大的本地模型（GGUF 需另装 runner）？

---

## 9 追加：测试页与「内置 LLM 出报告」（2026-10-22）

用户裁决：「下一页是测试页面，展示测试参数、问答效果，加入 AI 测试选项，除了手动外，可以交由 AI 测试给出完整报告」；随后确认「**内置 LLM 吧，都可以**」。

### 9.1 页面（v3 已实装，演示数据）
决策线第 4 步 `/共 4 步 · 测试`：

1. **测试参数**（4 格）：测试数据（held-out 路径 或 校准切片，标 `in-sample`）、样本数、指标集合（Accuracy·Loss·ECE·Brier）、拒答阈值（`target_error`）。
2. **两个入口**：
   - `运行测试`（手动）：立即出指标卡 + 问答效果表；
   - `交给 AI 测试并出报告`：过程可见（读测试集 → 生成预测并对比标注 → 算校准 → 归因错例 → 撰写），最后出 **指标 + 逐条效果表 + 完整报告**，并解锁 `导出报告（Markdown）`。
3. **问答效果表**：`题目（截断） | 模型答案 | 标注 | 一致 ✓/✗ | 置信度`，错例标红；超过 6 条可滚动。
4. **报告结构**（固定四段）：`总体 → 错例 → 归因 → 建议`；结尾必须带一句"in-sample ≠ 泛化"的提醒（有 eval_data 时改为 held-out 口径）。
5. 底部：`上一步`（回训练页）/ `完成`（回首屏）。

### 9.2 评测契约（待核心实装）
```
POST /api/jobs/{id}/eval
  body { data: "<held-out.jsonl|空=用校准切片>", limit?: 96, with_predictions: true }
  → { data_source: "heldout|calibration", n: 96,
      metrics: { accuracy, loss, ece, brier_multi, brier_top1, mean_conf, abstain_rate },
      per_question: [ { q, answer, expected, match, confidence, state_excerpt } ],
      confusion: [ {label, support, correct} ],
      errors: [ {q, answer, expected, confidence, note} ] }
```
- `per_question` 只回传**截断后的题目**（≤80 字），完整 state 留在产物目录，避免把语料塞进 HTTP 响应。
- 空 `data` ⇒ 用训练时的校准切片，响应里 `data_source:"calibration"`，UI 必须显示 in-sample 黄条。

### 9.3 内置 LLM 出报告（F25）
```
POST /api/jobs/{id}/report
  body { language:"zh", model?: "<本地 HF 目录|models\\<id>>", template: "laya-eval-v1" }
  → { ok, engine:"llm|template", model, elapsed_s, markdown, json: {...} }
```
- **引擎选择**：优先**内置 LLM**（`--report-llm` 默认 `resources\03-LLM-小模型\Qwen2.5-0.5B-Instruct`，用 ComfyUI python + transformers 推理，零新装）；不可用/超时（默认 60 s）→ **确定性模板**（同一份 `json` 事实，套固定四段），`engine:"template"`，**功能不降级**。
- **接地约束（硬）**：LLM 只能引用请求里给它的数字与错例；prompt 里禁止它"估算/推测"新数字；生成后再做一次**数字校验**——报告里出现的每个 `0.xx`/百分比必须能在输入 metrics 里找到（容差 ±0.01），找不到就回退模板并记 `warn:"ungrounded_numbers"`。
- 报告 JSON schema（导出用）：
  ```json
  {"job_id":"…","data_source":"calibration","n":96,
   "metrics":{...},"confusion":[...],"errors":[...],
   "summary":{"verdict":"…","top_cause":"…","actions":["…"]},
   "engine":"llm|template","generated_at":"…"}
  ```
- 导出：`laya-eval-report.md`（人读）+ `laya-eval-report.json`（机读、可回灌 MCP/CI）。

### 9.4 与 MCP 的关系
- 同一能力通过 MCP 暴露：`modelsmith_eval_job`、`modelsmith_report_job`（参数同上），Agent 可以直接跑评测+取报告，不需要开 UI。
- MCP 侧仍然禁止 `train_command`；评测是**只读**操作，不占 GPU 锁（除非被测模型需要加载显存，按 GPU 作业排队）。

### 9.5 未定
- 报告 LLM 的默认模型（0.5B 够不够）：先按 0.5B 落地，报告质量不够再换大模型（接口不变）。
- 报告的"建议"是否允许**给出参数改动**（如 epochs 4→8）：允许，但必须标注"建议，需人工确认"，且不允许直接触发训练。

---

## 10 追加：加训页（第二段训练，F27，2026-10-22）

用户问「加训还是回头重训」后的结论：**Laya 没有 resume**，所以只有两条路，做成一个页面二选一。

**页面（v3 已实装）**：`加训 · 第二段训练`
1. 顶部黄条硬提醒：续训 ≠ 恢复训练（不恢复步号/优化器、旧分布被冲淡、校准与拒答阈值重新拟合、两次指标不可直接对比）。
2. 起点二选一：
   - **以本次训练结果为起点**（默认）→ `base = _runs\ui-laya-<上次>`，`epochs=2`，新增数据 + 回放 20%
   - **从原始底座重新开始** → `base = 原始 Laya 底座`，`epochs=4`，全量数据（用于公正判断"补数据有没有用"）
3. 参数四格只读展示：新增数据条数 / 旧数据回放比例 / epochs / batch·accum。
4. 底部 `上一步`（回测试页）/ `开始加训`。

**接线契约**
```
POST /api/jobs  {kind:"laya", params:{
  base:"<上次 out 目录 或 原始底座 绝对路径>",
  data:"<合并后的训练集>",            // 新增 + 回放（回放由核心/适配器按比例抽样合并）
  out:"<新目录>", epochs:2|4, micro_batch:8, grad_accum:8,
  eval_data:"<held-out>",             // 必填才可能判断泛化
  train_mode:"continued"|"scratch"    // continued = 二段训练（语义标注，不改变上游行为）
}}
```
- 核心需在 metrics 里带 `base_source:"previous_run|original"` 与 `replay_ratio`，完成页/报告据此标注"这是第 N 段训练"。
- 评测页报告底部出现两个入口：`加训（续训）` → 本页且默认选「以本次结果为起点」；`补数据后从头重训` → 本页且默认选「从原始底座」。
- 红线：**任何入口都不许把它说成 resume**；UI 文案固定为"第二段训练"。

**已验（headless 全流程）**：`dec1=true ; dec2=true ; dec3=true/正在训练 ; dec4=true ; reportLen=570 ; actsHidden=false ; dec5=true/ep=2 ; run=正在加训|48 / 352`（续训 2 epoch × 22 updates × 8 micro = 352 micro-step）。

---

## 11 追加：独立页面架构 + LLM/生图线（2026-10-22 晚，本文件即"功能接线清单"）

> 用户问「之前让你登记的清单，文件名我忘了」——**就是本文件** `E:\视频\modelsmith\docs\UI-ARCH-v4.md`（§4 台账 F01–F28 + 本节 F29–F38）。

### 11.1 架构变更（重要）

**决策线留在 deck（幻灯片容器）里；LLM / 生图线改为独立 HTML 页面**，点卡片直接 `location.href` 跳转。

- 原因：deck 版首页出现"整页纯白"（见 F36）；独立页面不依赖 deck 的尺寸测量与位移计算，直接渲染，**已验证稳定**。
- 代价：两条线之间没有横向滑页动画（换来的是"一定能打开"）。
- 决策线仍是 PPT 式滑页（dec1…dec5）。

### 11.2 新增台账（F29–F38）

| # | 功能 | 现状 | 归属层 | 判据 / 契约 | 阶段 |
|---|---|---|---|---|---|
| F29 | 模型扫描器 `tools\scan_models.py` → `design\prototype\v3\model-index.js` | ✅ | 工具 | 扫 `models\` + resources\03/07 + laya + Qwen-Image 目录；按内容判定（`rl_agent_config.json`=laya、`model_index.json`=diffusion、`config.json`+safetensors=llm）；**diffusers 根不再下钻**（否则 text_encoder/transformer 会被误判成 LLM） | 已实装 |
| F30 | 模型下拉（按流程过滤）+「打开文件夹…」+ 粘贴/去引号 | ✅ | UI | LLM 只列 `llm`、生图只列 `diffusion/diffusion_single`；`webkitdirectory` 打开系统文件夹对话框；**浏览器不给绝对路径**，名称能对上扫描结果就自动补全，否则提示粘贴 | 已实装 |
| F31 | LLM 三页：`llm.html` → `llm-data.html` → `llm-train.html` | ✅ | UI | 选模型（下拉/文件夹/粘贴）→ 数据（JSONL 绝对路径 + 打开文件 + 示例 dolly）→ 训练（`global_step` 进度、loss、用时、adapter 真实路径 + 复制） | 已实装（演示数据） |
| F32 | 生图四页：`image.html` → `image-data.html` → `image-params.html` → `image-train.html` | ✅ | UI | 选底模（架构/大小/形态自动识别）→ 图片文件夹（图片数/metadata/分辨率）→ 参数+提示词+采样计划 → 训练（采样图墙） | 已实装（演示数据） |
| F33 | 生图训练：**每 200 步一组，独立横排卡片** | ✅ | UI←core | 每组带「第 N 组 · step X」+ LoRA 文件名 + 该组提示词图格；**只显示最近 5 组**（卡片区滚动，标题/进度/日志常驻） | 已实装 |
| F34 | 候选星标 + 最近 5 份 LoRA + 选一份 | ✅ | UI←core | 每轮 `☆ 候选` 可标星；`这轮达标，停训保留` 立即停训并跳过后续采样；收尾列表 = 最近 5 份 ∪ 星标项（★ 标记）+ `epoch-0.safetensors`；可复制/选定；实测星标在窗口外仍保留（rows=7、starRows=1） | 已实装 |
| F35 | 生图默认参数更新 | ✅ | UI | `steps=3000`、`分辨率 1020`、`每 200 步出图`（batch/lr/rank 从界面撤掉，走默认+MCP）；采样计划自动算：首图 1 组 + 15 次 = 16 组 × 提示词数 | 已实装 |
| F36 | deck 首页白屏修复 | ✅ | UI | 根因：四卡是 JS 绝对定位，窗口未量出尺寸时算出 0×0；修法：尺寸守卫（<40px 重试）+ `load/resize/visibilitychange/ResizeObserver` 四触发 + 3 秒兜底普通 2×2；另**默认关闭 `backdrop-filter`**（`?glass=1` 才开） | 已实装 |
| F37 | 训练页文案诚实化 | ✅ | UI | LLM 训练页写明「smoke 2 步；正式训练按 epochs×ceil(样本/batch×accum)，dolly 15k 默认约 4.5 万步」；生图页写「每 200 步出图并存 LoRA，只留最近 5 份」 | 已实装 |
| F38 | 产物路径展示（真实路径 + 复制） | ✅ | UI | 训练/测试完成卡显示 `_runs\...\model.safetensors`、`adapter_model.safetensors`、`epoch-0.safetensors` 与输出目录，`navigator.clipboard` 复制 + `execCommand` 兜底 | 已实装 |

### 11.3 接线契约（对这部分的增量）

```
POST /api/jobs  {kind:"diffsynth", params:{
  model:"<底模目录>", dataset:"<图片文件夹>", metadata:"<csv，可空>",
  steps:3000,                      // 替代 epochs 口径：固定步数
  height:1020, width:1020,
  save_steps:200, max_step_saves_to_keep:5,
  sample_every:200, sample_prompts:["...","..."],
  sample_width:1020, sample_height:1020
}}
```
- 核心/适配器需回传：`step/total`、`sec_per_it`、`sample_events[{step, files[], lora_path}]`、`artifacts[{step, path, bytes, starred?}]`
- 「这轮达标，停训保留」= 取消任务但**保留已落盘的 step LoRA**（`state=cancelled`，产物不清）
- 「选一份」在 UI 侧只是本地状态；真正"导出/部署"需要核心给 `POST /api/jobs/{id}/export {artifact}`（待设计）

### 11.4 未决

1. 分辨率写的是 **1020**；若实际要 **1024**（常见方形），改一个数字即可（UI + 核心参数）。
2. 采样图现在是**占位色块**：接核心后换成 `<img src=...>` 真图（核心需把 `samples\step-N\*.png` 暴露成静态资源）。
3. 星标目前只在本页内存里（刷新丢失）；接核心后应写进 job metrics，便于 MCP 侧读取。

### 11.5 已决（2026-10-22 夜，补记）

- 上列 1–3 已有结论：分辨率仍为 1020（未改）；采样图**接口已接**（`/api/jobs/{id}/samples/<rel>`）但训练侧尚无 `sample_events`（task-47）；星标已写进 `metrics.starred_artifacts`。证据见 `review\V3-WIRING-AUDIT.md`。

---

## 12 接线落地状态（2026-10-22 夜 · 本次结果）

> **证据唯一出处**：`E:\视频\modelsmith\review\V3-WIRING-AUDIT.md`（F01–F38 逐条表 + 证据 1–5 + 三次真跑 + 两口径截图 + §9/§10 复测）。本节只记架构与状态计数，**不复制读数**。

### 12.1 架构（最终形态）
- **UI 磁盘文件零改动**：`design\prototype\v3\` 九文件 + 旧 `design\prototype\index.html` 的 SHA256 与开工前逐项一致（0 偏差，跑后复扫仍 0）。
- **核心托管 v3**：`GET /` 与 `GET /v3/*` 与根路径同名文件等价；HTML 响应在 `</body>` 前**内存注入** `window.__MS_TOKEN__ / __MS_API__ / __MS_UI` 与 `<script src="/ms-wire.js">`（磁盘不变）。
- **接线层**：`E:\视频\modelsmith\core\assets\ms-wire.js`（61,274 B，ES5 零依赖）——`?demo=1` 关闭；5 s `/health` 心跳 + 掉线可见；参数从既有节点读取。
- **模型仓**：`models\` 唯一根，`GET /api/models`（内容判定 kind）；宿主持久化 `localStorage`（`ms.last.laya.out` / `ms.llm.model` / `ms.image.model` / `ms.dec.base`）。

### 12.2 状态计数（审计表为准）
- **F01–F38：✅28 / ⚠6 / ❌1 / ⬜3**；**P0 = 0**。
- 唯一 ❌ = **F33**（采样墙真图：接口已就绪，训练侧 `sample_events` 属 task-47，未产出）。
- ⚠6 与 ⬜3 的逐条理由在审计表「状态/证据」列；主要三类：无 UI 控件（F15/F32/F07「已装好」行）、仅 md 无 JSON（F26）、恢复期 2 条 `/health` 网络日志（浏览器网络错误，非未捕获异常）。

### 12.3 真跑证据（A 级，三次 GPU 串行）
- 决策：`updates_total=22`（1 epoch）与 `56`（4 epoch）两次 submitted-by-UI；`model.safetensors` **842,609,220 B**。
- LLM：`step/total=2/2`、`loss_series` step `[1,2]`、`adapter_model.safetensors` **17,640,136 B**。
- 生图：`4/4`、`sec_per_it 3.74`、`epoch-0.safetensors` **41,999,944 B**。
- 取消：`cancelled` 4.782 s；加训：`base` = 上一段 `out`、`train_mode=continued`。
- 评测/报告：`/eval` b 路径（指标回放 `n=96`）；`/report` **LLM 引擎真跑**（0.5B，16.3 s，2,305 B，无未接地数字），失败回退模板 `warn="llm_failed"`。

### 12.4 未做（明确留到下一轮）
- F33 真采样图（依赖 task-47 适配器采样钩子）。
- F32 `image-params` 无 steps/height 控件（UI 侧不加控件，按默认 + MCP 细参数口径处理）。
- `/eval` 逐条预测（现为 b 路径指标回放；`laya.evals_cli` a 路径未接）。

---

## 13 生图多底模 + 分发运行时（2026-10-22 深夜）

> 本节补 §12 之后的两件事：生图线从"单一 Qwen-Image 2.1"扩到**三 arch**，以及"点击即用"的运行时方案。证据：`adapters\README.md` arch 表、`docs\IMAGE-ARCH-PRESETS.md`、`docs\MOSS-WIRING.md`、`dist\RUNTIME-PACK.md`、`review\V3-WIRING-AUDIT.md` §11。

### 13.1 三个 arch（同一 `adapters\diffsynth_adapter.py`，`ARCHES` 分支）
| arch | train.py | 关键固定 flag | tokenizer/processor | 16 倍数 | 真跑读数（A 级） |
|---|---|---|---|---|---|
| `qwen_image_21`（默认） | `examples\qwen_image_21\model_training\train.py` | — | 走 `--processor_path <model>\processor` | 未强制 | 4/4 步、3.87 s/it、63.1 s、峰值 **31,945 MiB**、`epoch-0` 41,999,944 B、采样图 103,862/104,188 B |
| `moss_image` | `examples\moss_image\model_training\train.py` | `--lora_base_model dit`、`--lora_target_modules ""`、`--remove_prefix_in_ckpt "pipe.dit."`；**禁** `--initialize_model_on_cpu`、**offload 必须关** | `--tokenizer_path <model>\tokenizer`（缺则报错，不回落 hub）；无 processor | 16（`train.py:104-105`） | 4/4 步、1.49 s/it、43.0 s、峰值 **26,869 MiB**、`epoch-0` 77,719,832 B（524 张量）；**本版不出采样图** |
| `z_image` | `examples\z_image\model_training\train.py` | `--lora_target_modules "to_q,to_k,to_v,to_out.0,w1,w2,w3"`、`--lora_base_model dit`；**offload 必须关** | `--tokenizer_path`；无 processor（仓库无 `processor/`） | 16（`train.py:126-127`） | 4/4 步、1.06 s/it、31.6 s、峰值 **21,927 MiB**、`epoch-0` 158,818,752 B（476 张量）、采样图 73,166/72,582 B |

- **采样**：上游无原生回调 ⇒ runner 内 monkeypatch `ModelLogger.on_step_end`，用训练进程内 `model.pipe` 出图（零重载）；产出 `metrics.sample_events=[{step,files,lora_path}]` + `[sample] step=N file=...` 日志行；采样后必须恢复 `scheduler.training` 与组件 device 快照。
- **权重仓**：`models\moss-image-sft\`（25.36 GB）、`models\z-image-turbo\`（30.64 GB）、`models\z-image\`（Base transformer 子集 12.31 GB）；三者 `model_index.json` 的 `_class_name` 分别为 `MOSSImagePipeline` / `ZImagePipeline`，由 `/api/models` 回传 `class_name`，UI 据此自动判 `arch`（**不改 UI 文件**）。

### 13.2 两个实测"开关杀"
- MOSS：`--enable_model_cpu_offload` 会清空 VAE `bn` 统计量 → `RuntimeError: size of tensor a (128) must match b (0)`（`flux2_vae.py:326`）⇒ **默认关**。
- Z-Image：offload 会清空顶层 `x_pad_token` → `shape mismatch: [0] vs [0,3840]`（`z_image.py:606`）⇒ **默认关**。
- Z-Image Turbo 权重存量是 **F32（24.6 GB）**，但上游 `train.py:33` + `file.py:31-32` 读入即 cast bf16（上卡 ~12.3 GB）；**CPU 侧读入峰值仍 24.6 GB**。

### 13.3 分发运行时（"点击即用"）
- `dist\pack_runtime.py`（默认 dry-run，`--apply` 才复制）+ `dist\RUNTIME-PACK.md` + `dist\start-core.cmd`（ASCII/CRLF，设 `PYTHONHOME/PYTHONPATH/PYTHONUTF8/PYTHONNOUSERSITE`）。
- 最小可跑集 **3,188,169,011 B = 2.97 GiB / 16,988 文件**（源全量 12.33 GiB；CUDA DLL 占 ~87%）；import 探针 40/40（含 `diffsynth`/`llamafactory.cli`/`laya`）。
- 许可：Python PSF / torch BSD-3 / HF 系 Apache-2.0 / numpy BSD-3 … 共 52 个 `licenses/<pkg>/`；**CUDA/cuDNN 二进制随包**（NVIDIA CUDA EULA Attachment A + cuDNN SLA，分发方承担，用户已确认"Cuda 我们备好"）；`tokenizers`/`sentencepiece`/`antlr4` 的 LICENSE 原文待补（task-81）。
- 运行时产物 `dist\runtime\` 与 `dist\runtime-manifest.json` 已 gitignore（可重建，~3 GiB 不进仓库）。
---

## 14 台账补漏（2026-10-22 深夜 · 用户实测后核对）

> 用户实测发现两处"台账里没有登记"的接线缺口。本节把 F01–F38 之外的缺口全部登记，**以后以本节为准补**。审计表（`review\V3-WIRING-AUDIT.md`）的 38 行只覆盖台账原有条目，**不等于产品无缺口**。

| # | 缺口 | 现象/证据 | 归属 | 状态 |
|---|---|---|---|---|
| G01 | 生图数据页**没有原生文件夹选择** | wire 只在决策线接 `/api/pick`（`mode:"file"`），`image-data` 的"上传文件夹"走浏览器 `webkitdirectory`，拿不到绝对路径 ⇒ `dataset` 填不进 | UI 接线 | 修中（2026-10-22 夜） |
| G02 | 模型仓**不校验 diffusers 目录完整性** | `models\z-image` 只有 `transformer/`，`/api/models` 照列；提交后子进程报 `no *.safetensors shards under ...\text_encoder`（job `job-20261022-132436-79a25081`） | core | 修中（`complete`/`missing` + 400 点名） |
| G03 | 终态后**演示动画不停** | 用户截图"训练失败"但界面仍像演示 tick 在推进 | UI 接线 | 修中 |
| G04 | LLM 数据页**只有输入框、无原生选择** | `initLlmData` 只监听 `input`，无 `/api/pick` | UI 接线 | 待修（同 G01 方案） |
| G05 | `/eval` 只有 b 路径（指标回放） | `per_question: []`，测试页"逐条效果表"为空；a 路径（`laya.evals_cli`）未接 | core | 待修（P1） |
| G06 | 下载缺模型入口只在决策线 | 生图/LLM 模型页无"下载"按钮，缺模型只能手放 | UI 接线 | 待设计 |
| G07 | F26 只导出 md | 无 JSON 按钮位 | UI | 已知 |
| G08 | F19 无抽屉 | 只有环境 chip 的 text/title/aria | UI | 已知 |
| G09 | F07 无取消按钮 | 核心有 `download/cancel`，UI 无入口 | UI | 待设计 |

**判据口径**：G01/G02/G03 修完后，生图线必须能"选文件夹 → 提交 → 真训练"一条链走通（`dataset` 为绝对路径、job 不再因缺组件失败、终态静止）。
