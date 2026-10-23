# core\assets\ms-wire.js — v3 接线层（R2a/R2b）

单文件、零依赖、ES5。由核心在托管 v3 时**内存注入**，磁盘 UI 文件零改动。

## 注入机制

核心对 `design\prototype\v3\*.html` 的响应在 `</body>` 前拼两段（task-52）：

```html
<script>window.__MS_TOKEN__="…";window.__MS_API__="/api";window.__MS_UI="v3";</script>
<script src="/ms-wire.js"></script>
```

`GET /ms-wire.js` 从 `--assets-dir`（默认 `core\assets`）读本文件。

## 开关 / 调试

- `?demo=1`：强制关闭接线（页面保留原演示），用于 file:// vs served 截图对拍。
- 无 `window.__MS_TOKEN__`（如直接 file:// 打开）时脚本直接 return，零副作用。
- 控制台：`window.MSWire` 暴露 `page/tokenPresent/demo/features/api/engine`；日志前缀 `[ms-wire]`。
- token 只放 `X-ModelSmith-Token` header，不进 URL/日志。

## 轮询与“演示空转”

v3 的演示 timer 全在各页 IIFE 闭包里，外部 `clearInterval` 不到。本层按 维护者 裁决处理：

- 真实 job 活跃时每 **300ms** 用 `GET /api/jobs/{id}`（+ `logs?offset=`）覆盖同一批既有节点；
- **任务终态后停轮询并把最终值写死一次**（完成态冻结，演示 tick 不会再改回去）；
- README 口径：**演示 tick 仍在空转，显示值由 wire 每 300ms 覆盖**。

## 每页接线（F 编号）

| 页面 | F | 行为 |
|---|---|---|
| `/`（decision deck） | F06/F11/F12/F13/F14/F19/F20/F21/F23/F24/F25/F26/F27 | dec2 走 `POST /api/pick` 取绝对路径（404 回退浏览器 File 并诚实提示）；dec3 POST laya + 300ms 轮询（updates 分母/loss/日志/曲线），cancel，完成态真路径 + in-sample 提示；dec4 `/eval`、`/report`（404 显示“接口未就绪”），markdown 导出；dec5 加训 `train_mode=continued`；F19 只写 chip title/aria |
| `/v3/llm.html` | F28/F30 | `GET /api/models?kind=llm` 填既有下拉（option 克隆，过滤 llm）；双击下拉刷新；手输保留 |
| `/v3/llm-data.html` | F17 | `GET /api/data/hint?path=` 防抖提示（404 只写 title） |
| `/v3/llm-train.html` | F31/F38 | POST lf（§17.2 字段）+ 300ms 轮询进度/loss/log，终态冻结；adapter/out 用真实路径 |
| `/v3/image.html` | F30/F32 | `GET /api/models?kind=diffusion` 填下拉（option 克隆，过滤 diffusion） |
| `/v3/image-data.html` | F17 | 同 data_hint |
| `/v3/image-params.html` | F32/F35 | 默认 steps=3000 / 1024 / 每 200 步（UI 静态值） |
| `/v3/image-train.html` | F32/F33/F34/F38 | POST diffsynth（3000/1024/200）+ 300ms 轮询；`sample_events` → 既有 `.shot__img` 的 `backgroundImage` 指向 `/api/jobs/{id}/samples/...`，无图保持占位并写明；完成 .lorow 真路径；☆ 走 `/star`（404 本地降级） |

## 预接但等核心端点的接口（全部 404 降级，不崩）

- `POST /api/pick`（task-60）
- `GET /api/jobs/{id}/samples/<rel>`、`GET /api/jobs/{id}/artifacts`（task-60/53）
- `POST /api/jobs/{id}/star`（task-53）
- `GET /api/jobs/{id}/metrics_history?since=0`（task-54）
- `GET /api/data/hint?path=`（task-54）
- `POST /api/models/{id}/download` + `GET .../download/status`（task-53）
- `POST /api/jobs/{id}/eval`、`/report`（task-55）

## 已知限制

- **F19 = 部分**：v3 无抽屉节点，只有 chip 摘要（写 `title`/`aria-label`，不改可视文本以保截图 diff）。
- **F14**：核心未回传 before/after 数值时完成页显示 `—`，in-sample 提示仍在；等 `/eval` + train_report.json。
- **F18/F15**：v3 无“打开目录”/早停开关节点；F18 已用路径节点双击（`#dec3Out`/`#adapterPath`/`#outPath`/`.lorow code`）接 `/api/open`，F15 记 API/MCP-only。
- **F26**：仅 markdown（页面没有第二个导出按钮位）。
- **F30**：无独立刷新按钮，刷新=双击模型下拉（title 提示）。
- 决策自选文件的绝对路径依赖 `/api/pick`；核心 404 时只能样例数据，UI 会诚实提示。
- 真采样图依赖核心静态托管 samples；未就绪时既有 `.shot__img` 保持占位。

## 自测读数（2026-10-22，本机 A 级）

- served CDP（核心随机端口 + `--data-dir` 临时）：**31 PASS / 0 FAIL**
  - 三线 POST body：`kind=laya`（data=样例-闸门路径/base=laya/无 train_command）、`kind=lf`（model/data/template/max_steps=2）、`kind=diffsynth`（model/dataset/steps=3000/height=1024/save_steps=200/sample_every=200）
  - 进度/日志增长、决策取消→`cancelled`、完成态真实路径（`_runs\ui-laya-*` / `ui-lf-*` / `ui-diffsynth-*`，无 `180000`）、完成态 3 次采样冻结
  - `metrics_history?since=0` 行数≥4、`data_hint` 返回 lang_hint、`/api/open?dry=1` 允许 200 / 拒绝 403
  - console error=0
- 静态截图对拍：**8 个 v3 HTML + `/legacy` 共 9 页，file:// vs served `?demo=1`，reduced-motion，1280×900，diff 全 0 px**
- `design\prototype\` SHA：9 个 v3 文件全部未变（index.html `27592D9E…`，llm `ff8a8f30…` 等）；旧 `index.html` `6897E9B9…` 未变
- 真实 core 未跑 GPU：三线全部 `params.train_command` 指向 `adapters\_tmp\*_stub.py`

## R2b 自测（task-57）

- **R2b：18 PASS / 0 FAIL**（核心 `--models-dir _tmp\wire\fake-models`，本地 146 B HTTP 下载桩）
  - `/api/models` 4 条扫描、kind 过滤（llm/diffusion 各 1）；UI 下拉过滤真实（生图不出现 llm/laya，LLM 不出现 sd15/Qwen-Image）
  - 下载：POST → status 到 `state=succeeded/pct=100`，落盘且 `.part` 消失
  - `/api/open?dry=1` 允许 200 / 拒绝 403；`data_hint` 返回 lang_hint；`metrics_history` rows≥4；`/star` artifact 幂等 200；`/eval`+`/report` 404 降级
- 静态截图复测：9 页 diff 仍全 0 px；九文件 SHA 未变
- F15 = ⬜（v3 无早停开关节点；仅 API/MCP 可用）
- F26 = ◐（仅 markdown；无第二个 JSON 按钮位）
- F07 取消 = 未做（核心未提供 download/cancel 路由；面板无取消按钮）
- F33 真采样图 = ◐：`/api/jobs/{id}/samples/<rel>` 端点已就绪、wire 已把 `.shot__img` 的 `backgroundImage` 指向它；但**训练侧 DiffSynth 适配器尚未产出 `sample_events`**（task-47），因此本次无真图可显示，**不造假**。
- F34 = ✅（无 GPU）：diffsynth 桩写假 `epoch-0.safetensors` → `/artifacts` 填完成态行 → 点 ☆ → `/star` → `GET /api/jobs/{id}` 的 `metrics.starred_artifacts` 含 `step-0200.safetensors`。
- F14/F23/F24/F25/F26 = ✅（无 GPU）：桩 job + 手写 `train_report.json`（accuracy/loss/ece/brier/temperature/abstain 字段）→ `/eval` → `/report` 四段（总体/错例/归因/建议 + in-sample ≠ 泛化）→ UI 表格/报告/导出按钮可见。**报告 LLM 引擎未验**（本次走 template）。
- F07 = 部分（取消待核心路由，README 记明）；F15 = ⬜（无节点）；F06 = ✅（`--pick-stub` 断言绝对路径写入）。
- 无 GPU 报表/星标自测：**13 PASS / 0 FAIL**（`_tmp\wire\test_report_star.mjs`）。

## task-62 热修（P0/P1）

- **P0 错误不再显示成成功**：`/eval`、`/report`、`/pick`、`/download(+status)`、`/star`、`/open` 一律先判 `ok`；`ok:false` 时既有节点显示「评测失败：<error>」「报告生成失败：<error>」「下载失败：…」「标星失败：…」「打开目录失败：…」/「选择文件失败：…」，并隐藏结果/导出；错误原文写 `title`；网络异常 →「核心未连接」。只有 `ok:true` 才走成功渲染。
- **P1 终态回填**：laya 收到 `succeeded` 时强制 `updates=updates_total`（有 `updates` 优先）、`micro_step`、`epoch/epochs_total`、`loss`、`elapsed`，bar 100%，并纳入完成态冻结（500ms 守卫，3 次采样一致）。
- **附带**：`/eval` 的 `output_dir_source=params` 只 console；`per_question:[]` 且 `note` → 逐条表显示「本次训练未落逐条预测（note 原文）」。
- 新增断言（无 GPU，`test_report_star.mjs`）：**18 PASS / 0 FAIL**，含 `p0_eval_fail_text`（含“评测失败”、不含“完成”）、`p0_report_fail_text`（含“报告生成失败”、不含“已生成”）、`p0_report_hidden`、`p1_final_updates`（22/22）、`p1_frozen`（3 次一致）。

## task-65 核心掉线可见（heartbeat）

- 每 **5 s** `GET /health`（免 token）；**连续 2 次失败** → `markOffline()`：环境 chip `textContent=「环境 · 核心未连接」`、训练页日志追加「核心未连接」、停轮询、禁开始按钮（`#dec2Next`/`#dec5Run`）。
- **一次成功** → `markOnline()`：恢复 chip「环境」、移除自己追加的日志行、恢复按钮。
- 普通 API fetch reject/5xx 也走 `markOffline`；`/eval` 等 **HTTP 200 + ok:false 业务失败不算离线**。
- 自测 `_tmp\wire\test_health.mjs`：**9 PASS / 0 FAIL**（在线负控无文案 → 停核心后可见+按钮 disabled → 同端口重启恢复；console error=0）。
- F33 仍 **⚠ 未完成**：训练侧 `sample_events` 属 task-47，`/samples` 端点与 wire backgroundImage 已就绪但无真图产出，不造假。

## task-73 F10/F20 实测（无 GPU）

- **F10**：`/api/pick` 选中文语料 `skills\laya-corpus\examples\customer-service-3rows.jsonl` → `/api/data/hint` `lang_hint=zh` → `#dec2LangNote` 显示「检测到中文语料：建议换多语言底座（lang_hint=zh）」；选英文 dolly → 黄条 `hidden=true`（负控）。
- **F20**：fake job running 时返回首屏，决策卡 `.state` =「训练中」；job 变 succeeded 后 =「已完成 · ui-laya-fake」；再进 dec3，`#dec3Title=训练完成`、`#dec3Done` 可见。
- 自测 `_tmp\wire\test_f10_f20.mjs`：**6 PASS / 0 FAIL**。

## task-73 F33/F34 代码（fixture 自测未通过）

- F33：`sample_events[].files[]` 支持对象 `{file,bytes,path}`；`.shot__img` 的 `backgroundImage` = `/api/jobs/{id}/samples/step-N/<file>`（不新增 DOM）；按 step 克隆既有 `.round` 行；`#shots` = `sum(len(files))`；无 events 保持“本次运行未出采样图”。
- F34：☆ 用该行 `lora_path`（null 时 `step-N.safetensors`）→ `/star`；保留「停训保留」按钮 → 取消 job、已落盘 LoRA 不清。
- **fixture 自测未通过**：核心 diffsynth 桩 job 状态 `failed`、`sample_events=[]`（`test_f33_f34.mjs`），故 F33/F34 仍标 **未验**，不谎报；无 GPU。

## task-73 文本掩膜口径

- `_tmp\wire\test_wireon_mask.mjs`：CDP 取文本节点/输入框 bbox（外扩 4px）+ **截图进程冻结 ≥500ms 定时器** + reduced-motion + 双 rAF → Python PIL 掩膜逐像素 diff。
- wire 关（`?demo=1`）9 页：**全 0 px**。
- wire 开：**9/9 页 0 px**（正控同页两次 0、负控不同页 34.37% 变红）。

## task-73 arch 判定 / MOSS 诚实文案

- `/api/models` 的 `class_name` 映射：`QwenImage*`→`qwen_image_21`、`MOSSImagePipeline`→`moss_image`、`ZImagePipeline`→`z_image`；未知/缺失→`qwen_image_21`（console 记一条）。
- 生图提交 body 带 `arch`（image.html 选择时写入 `ms.image.arch` sessionStorage，image-train 读取）。
- `arch=moss_image` 时既有 `#rounds` 文本 =「该底模当前训练不产出采样图（可训练后离线验证）」，`#shots=0`，不渲染假采样行。
- 自测 `_tmp\wire\test_arch_moss.mjs`：**6 PASS / 0 FAIL**（MOSS body arch、Qwen 默认、MOSS 文案/计数、console=0）。
- MOSS 背景（截图/产物）：真跑 4/4 步、43 s、峰值 26.9 GB、`epoch-0.safetensors` 77.7 MB；`--enable_model_cpu_offload` 在 MOSS 上会崩（VAE `bn` 空）默认必须关；文本编码器需 transformers 5.x（走 `.aitk-pylibs`）。

## task-73 F33/F34 真 job 验收（无 GPU）

- 真 job `job-20261022-124331-d5047408`（`succeeded`，out `_runs\core-sampling-qwen-e2e-20261022-204328`，sample_events 2 组）：
  - `#shots` = **2 组**；`.shot__img` `backgroundImage` = `url("/api/jobs/job-20261022-124331-d5047408/samples/step-2/sample_00.png")`；该 URL 200 `image/png` **103,531 B**
  - ☆ 两次 → `metrics.starred_artifacts` = `["step-2.safetensors"]`（幂等）
  - 负控（fake job 无 sample_events）→ `#shots=0` + 日志「本次运行未出采样图（核心未回传 sample_events）」
  - 「停训保留」用 fake running job：取消调用发生、UI 显示已停训、完成态列出 step-2/step-4 产物；**samples/ 目录实际保留由核心侧保证，本次未单独验**。
- 脚本：`_tmp\wire\test_f33_real.mjs`（真 job）、`_tmp\wire\test_f34_fake.mjs`（stop/negative）。
- **演示行清理（P1 修复）**：收到 `sample_events` 后给真实行加 `data-ms-real=1`，安装 MutationObserver 删除 demo `.round`（console.warn 一次）；`#shots = sum(len(files))`；终态再 `imgEnforce` 一次。真 job 现在 **#shots=2**（修复前 3，多出 demo step400 假组），`.round` 数=2；负控无 events → #shots=0 且 0 个 `.round`；`?demo=1` 仍保留演示行（wire 不激活）。

## F20 首屏 running 回填 / F19 三值（hotfix）

- `initHome` 启动时 `GET /api/jobs?state=running`：有 running → 决策卡既有 `.state` =「训练中 · <kind>」，禁用开始按钮；每 2 s 轮询该 job，终态改「已完成 · <kind>」/「已取消」/「失败」并恢复按钮；无 running 保持「可开始」。
- 自测 `test_f10_f20.mjs`：**10 PASS / 0 FAIL**（含 seedRun running → 训练中/disabled → 已完成；无 running → 可开始）。
- F19 三值实测（`test_health.mjs` 9/0）：在线 `text=环境, title=核心环境：正常, aria=环境：正常`；停核心 `text=环境 · 核心未连接, title=核心环境：核心未连接, aria=环境：核心未连接`；恢复回正常三值。

## 用户实测两卡点 + 失败态

- **生图数据页目录选择**：`image-data` 的「打开文件夹…」/dropzone 先 `POST /api/pick {mode:"dir"}`，绝对路径写入既有 `#dataPath` 并 dispatch input；`#browseBtn` 原处理器被 capture 拦截；404/失败回退 `#folderPick` 并提示「浏览器拿不到绝对路径，需核心在线」。提交 body `dataset` = 该绝对路径（`test_abc.mjs` 验）。
- **模型下拉完整性**：`complete=false` 的 option `disabled=true`、文本追加「不完整（缺 …）」、`data-complete=false`；已选残项会清空并提示；`z-image-turbo`（complete=true）可选。
- **失败态停演示**：job 终态时 `stopDemoTimers()`（覆写页面 `setTimeout/setInterval`，冻结计时）→ 仅静态显示终态；`test_abc.mjs` 连续 3×500ms 文本完全一致。
- 自测 `_tmp\wire\test_abc.mjs`：**8 PASS / 0 FAIL**；两口径 9/9 0 px。

## 开训前失败 / 首图占位

- 终态 `failed` 且 `metrics.step` 缺失/None：进度强制 `0 / total`（total 取 metrics/params/3000）、速度 `—`、用时取 `elapsed_s` 或 `—`、bar 0、`#shots=0`；armFreeze 500ms 守卫，3×500ms 采样稳定。
- live 模式无 `sample_events`：`imgActivateGuard` + `imgEnforce` 清除 demo `.round`（含“首图（训练前）”和假 caption/背景），只留既有日志「本次运行未出采样图」；`?demo=1` 演示态不受影响。
- 自测 `test_abc.mjs`：**13 PASS / 0 FAIL**（c_step_zero=0 / 3000、c_speed_dash=—、c_shots_zero、c_no_demo_rows、c_no_fake_caption、c_failed_frozen 3 次一致）。

## image-params 真提交 / image-train 按 job 轮询

- `image-params` 的「开始训练」capture 拦截：从既有节点/query/sessionStorage 读 model/dataset/steps/height/width 等，`POST /api/jobs {kind:"diffsynth", arch: sessionStorage ms.image.arch, params:{...}}`；成功后写 `sessionStorage["ms.image.job"]` 再跳 `image-train.html`；失败（4xx/5xx/网络）**不跳转**，在既有 `#plan` 显示失败原文。
- `image-train` 启动时若 `ms.image.job` 存在：`stopDemoTimers()` 冻结页面自带演示 tick，改用 `pollJob` 轮询该 job（不提交新 job）；不存在则保持原演示（演示模式）。
- 自测 `_tmp\wire\test_params.mjs`：**7 PASS / 0 FAIL**
  - 点开始 → `/api/jobs` 新增 `kind=diffsynth` job（`job-20261022-140113-1392775d`，state failed 因 model 不存在）
  - image-train 打开 3 s 后标题=`训练失败`（不是「训练完成」），日志无 `ui-diffsynth-20261022-180000`
  - 负控：POST 网络失败 → 仍停留 image-params，`#plan`=「提交失败：核心未连接」

## A/B/C 用户硬失败修复（2026-10-22）

- **A epoch 存档**：`image-params` 提交 `save_steps=0`（不再等于 sample_every），DiffSynth 默认产 `epoch-0.safetensors`；采样只由 `sample_every` 控制。自测 job params `save_steps=0`。
- **B 真实采样行**：清 demo 前先捕获模板（优先 `[data-act]`，否则普通 `.round`），再清行重建；每行写 step 文本、`.shot__img` 的 `backgroundImage=url(/api/jobs/<id>/samples/step-N/<file>)`；真 job `#shots=2`、第 2/4 组 bg 分别 step-2/step-4；无「未设置提示词/帅哥」。
- **C live 禁演示**：live 页面 `image-train/llm-train` 无 job 时 `stopDemoTimers()` + 标题「当前没有训练任务」+ 进度 0/速度 —/用时 —/`#shots=0`，`armFreeze` 守卫；`?demo=1`/file:// 保留演示。
- 自测：`test_params.mjs` 8/0、`test_abc.mjs` 17/0、真 job `test_f33_real.mjs` 8/0；两口径 9/9 0 px。

## image-train 首帧零进度守卫

- 无 job：`stopDemoTimers()` + `#step="0 / 3000"`、sp/time=`—`、`#shots=0`，并安装 `MutationObserver` 即时归零（防 pending demo tick 先写 25）。
- 有 `ms.image.job`：首帧先 `0 / 3000` 零值，`guardZeroProgress` 在第一次真实 poll 前持续归零，随后停 observer 渲染真值。
- 读数（核心托管 `/v3/image-train.html` 无 job，CDP 100 ms ×5）：`["0 / 3000|—|—|0" ×5]`，无一次 25；两口径 9/9 0 px。
- 有 job 首帧的 5×100ms 自动采样脚本挂起未完成，代码已加同一 observer 守卫，标**未验**。

## image-train 顶部步数/采样组数

- 顶部 `.meta` 的「共 N 步 · 每 M 步出图」改为来自本次 job 的 `metrics.total`/`metrics.sample_every`，缺则 params，仍缺显示「共 — 步」；不再写死 3000/200。
- `#shots` = `sample_events.length`（采样组数，不是图片张数）；空 events 显示 0。
- 合成 job 验证：`total=6,sample_events=[]` → 顶部「共 6 步」、`#shots=0`；`total=4,sample_events=2组` → 「共 4 步」、`#shots=2`（`_tmp\wire\test_meta.mjs` 4/0）。
- 真实 `job-20261022-142737` / `142554` 的 data-dir 不在本机可查范围（DB/目录已不在），故未直接复跑，标 **未验**；逻辑用同参数合成 job 验过。

## task-68 F27 加训起点

- 成功 laya 完成时写 `localStorage["ms.last.laya.out"] = metrics.output_dir || params.out`（同时 `ms.last.laya.job`）。
- dec5「以本次训练结果为起点」（默认）→ `base=该 out`；**空值直接拦截**，显示「还没有可续训的上一段训练」，不发 POST、不静默回退底座。
- dec5「从原始底座」→ `base=/api/models?kind=laya` 第一项路径（wire 已把首个扫描模型记入 `DEC.layaModelPath`），无扫描则回原默认 `E:\视频\拉亚决策模型\models\laya`。
- 判据自测（无 GPU，拦截 `POST /api/jobs` 只抓 body、不真跑）：**PASS**
  - seed `E:\视频\modelsmith\_runs\ui-laya-20261022-193300`（`model.safetensors` 842,609,220 B）→ dec5 默认续训 body `base`=该目录、`epochs=2`、`train_mode=continued`
  - 切「从原始底座」→ body `base`=`E:\视频\拉亚决策模型\models\laya-闸门-样例\checkpoint_latest`（`/api/models?kind=laya` 第一项）
  - 清 `ms.last.laya.out` + 续训 → **0 新增 POST**，`#dec5FileInfo` 显示「还没有可续训的上一段训练」
  - console error=0；测试 `_tmp\wire\test_f27_final.mjs`
