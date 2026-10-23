# MCP 工具参考（`mcp_server\modelsmith_mcp.py`）

> 唯一实现：`E:\视频\modelsmith\mcp_server\modelsmith_mcp.py`（stdlib、零依赖，**19 个工具 = 9 基础 + 6 端点 v2 + 4 端点 v3**）。
> 唯一服务端：`E:\视频\modelsmith\core\modelsmith_core.py`（本地核心）。
> 本文件只写**怎么用**；每个 kind 的完整字段以 `core\README.md` 对应小节为准（本文逐字段抄了字段名与默认值，**没有自创别名**）。

---

## 0. 前置与红线

1. **先起核心**（MCP 不负责启动它）：
   ```powershell
   python E:\视频\modelsmith\core\modelsmith_core.py      # 默认 127.0.0.1:8765
   # 或 --port 0：真实端口写进 %LOCALAPPDATA%\ModelSmith\core.json 的 port 字段
   ```
2. **token**：默认读 `%LOCALAPPDATA%\ModelSmith\token`；可用 `--token` / `--token-file` 覆盖。token 不会出现在 MCP 的 stdout/stderr。
3. **同一时刻只允许一个 GPU 作业**。提交前先 `modelsmith_list_jobs`（看 `state="running"` 是否为空）；核心有 GPU 文件锁会串行化，但排队不是并行许可 —— **排队也会等**，别把两个长训一起塞进去。
4. **不许传 `train_command`**：那是核心自测钩子（可执行任意子进程）。MCP 在 `modelsmith_submit_job` 里硬拒，返回
   ```json
   {"ok": false, "error": "train_command is a test hook, not allowed via MCP", "tool": "modelsmith_submit_job"}
   ```
5. 只监听 loopback；不要把核心端口暴露到不可信网络。

## 1. 传输与握手

- **stdio**，换行分隔的 JSON-RPC 2.0（**不是** LSP 的 `Content-Length` 帧）。
- 握手：`initialize` → `{protocolVersion:"2025-06-18", capabilities:{tools:{}}, serverInfo:{name:"modelsmith", version:"0.1.0"}}`；
  `notifications/initialized`（无响应）、`ping` → `{}`、`tools/list`、`tools/call`。
- 未知工具 → JSON-RPC 错误 `-32602`；未知方法 → `-32601`；坏 JSON → `-32700`。
- 每个工具的返回都是 `{"content":[{"type":"text","text":"<JSON>"}],"structuredContent":{...},"isError":bool}`。
- HTTP 非 2xx → 工具返回 `{ok:false, status, body}` 且 `isError:true`；**进程不崩**。
- stdin EOF → 退出码 0。

启动（客户端配置见 `mcp_server\mcp.json.example`）：
```powershell
python E:\视频\modelsmith\mcp_server\modelsmith_mcp.py --api http://127.0.0.1:8765
# 可选：--token / --token-file / --timeout 30
```

---

## 2. 九个工具

### 2.1 `modelsmith_health` — 核心存活（免 token）

入参：无。

```json
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"modelsmith_health","arguments":{}}}
```
返回（`structuredContent`，节选）：
```json
{"ok": true, "version": "0.1.0", "pid": 8892, "uptime_s": 12.3,
 "host": "127.0.0.1", "port": 8765, "db": "wal", "schema": 1,
 "jobs": {"queued": 0, "starting": 0, "running": 0, "succeeded": 3, "failed": 0, "cancelled": 1, "interrupted": 0}}
```

### 2.2 `modelsmith_env_doctor` — 环境体检

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `refresh` | bool | `false` | 绕过核心 30 s 缓存，重新探测 torch/CUDA/磁盘/端口 |

返回含 `python`、`torch`、`cuda`、`disk`、`ports`、`gpu_lock`、`laya`、`llm`、`aitk`、`diffsynth` 等块。**第一次真跑前先调它**，确认 `cuda.status="ok"`、`torch.version`、磁盘余量。

### 2.3 `modelsmith_list_models` — 本地模型清单

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `kind` | `"diffusion" \| "llm" \| "laya"` | 不过滤 | 只看某一类底座 |
| `refresh` | bool | `false` | `true` = 强制重扫模型目录（核心平时缓存 60 s，且目录签名变了会自动重扫） |

返回：`{ok:true, models:[{id,kind,name,...}], ...}`（核心 `GET /api/models`）。

**降级口径**：若核心尚未提供该路由（404），返回 **不抛**：
```json
{"ok": false, "note": "core 未提供 /api/models", "status": 404, "body": {"ok": false, "error": "not found"}}
```
且 `isError=false` —— Agent 看到 `ok:false` + `note` 就该改用 `resources\` 里已知的本地路径，而不是重试。

### 2.4 `modelsmith_list_jobs` — 任务列表

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `state` | `queued/starting/running/succeeded/failed/cancelled/interrupted` | 全部 | 过滤 |
| `limit` | int 1–500 | `100` | 最多返回多少条（新→旧） |

返回：`{ok:true, jobs:[...], counts:{各状态计数}}`。

### 2.5 `modelsmith_submit_job` — 提交任务

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `kind` | string | **是** | `fake` / `laya` / `lf` / `diffsynth` / `aitk`（`llm` 为备用基线） |
| `params` | object | **是** | 按 §3 的 per-kind 表；**原样透传**给核心 |
| `idempotency_key` | string ≤128 | 否 | 重放安全：同 key 再提交返回同一 job（200 + `idempotent_replay:true`） |

返回（202）：
```json
{"ok": true, "job_id": "job-20261022-103617-9244d4ee", "state": "queued",
 "queue_position": 1, "idempotent_replay": false, "job": {...}}
```
被拒（`train_command`）→ `isError:true`，`{"ok":false,"error":"train_command is a test hook, not allowed via MCP"}`。

### 2.6 `modelsmith_get_job` — 单个任务

| 参数 | 类型 | 必填 |
|---|---|---|
| `id` | string | **是** |

返回 `{ok:true, job:{id,kind,state,pid,params,metrics,log_path,log_size,exit_code,error,created_at,updated_at,...}}`。
`metrics` 是核心按适配器事件写的对象（字段随 kind 不同，见 §4）。

### 2.7 `modelsmith_job_logs` — 按字节 offset 尾随日志

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `id` | string | — | **必填** |
| `offset` | int ≥0 | `0` | 上一次返回的 `next_offset` |
| `max_lines` | int 1–1000 | `200` | 单次最多行数 |

返回：
```json
{"ok": true, "job_id": "job-...", "offset": 130, "next_offset": 273,
 "size": 273, "eof": true, "max_lines": 200, "lines": ["...", "..."]}
```
**循环尾随**：把 `next_offset` 当下一次的 `offset`；`eof=true` 表示已追平当前文件尾（任务可能还在跑）。

### 2.8 `modelsmith_cancel_job` — 取消

| 参数 | 类型 | 必填 |
|---|---|---|
| `id` | string | **是** |

返回 `{ok:true, job_id, state, cancel_requested:true, mode:"soft stop file, poll up to 15s, then taskkill /T /F"}`。
任务已终态 → HTTP 409 → 工具 `isError:true` + `{ok:false,status:409,body:{error:"job already finished"}}`。
**没有 resume**：停下只能从头再训（Laya/LF/DiffSynth/ai-toolkit 上游都没有真正的续训契约；见 §4 第三条范式）。

### 2.9 `modelsmith_wait_job` — 等到终态

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `id` | string | — | **必填** |
| `timeout_s` | number 1–7200 | `600` | 超过就返回 `timed_out:true`（**不会**取消任务） |
| `poll_s` | number 0.05–10 | `1.0` | 轮询间隔 |

返回：
```json
{"ok": true, "job_id": "job-...", "state": "succeeded",
 "metrics": {"step": 20, "loss": 0.09, "...": "..."},
 "waited_s": 21.4, "timed_out": false, "job": {...}}
```
`timed_out:true` 时 `state` 仍是当前态（`running`/`queued`），任务**继续跑**——要么接着等，要么 `cancel_job`。

---

### 2.10 `modelsmith_job_metrics_history` — 增量指标时间线

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `id` | string | — | **必填** |
| `since` | int ≥0 | `0` | 上次返回的 `next_seq`；只要比它新的行 |

返回：
```json
{"ok": true, "job_id": "job-...",
 "rows": [{"seq": 1, "step": 1, "loss": 1.77, "...": "..."}],
 "next_seq": 6}
```
尾随循环：`since = next_seq`。这是长训曲线/断点续看的首选（比反复拉整份 `metrics` 省）。

### 2.11 `modelsmith_eval_job` — 评测（透传，不代跑训练）

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `id` | string | — | **必填**，必须是**终态** job（`succeeded/failed/cancelled/interrupted`） |
| `data` | string | 无 | 留出集 JSONL；不传 = 校准切片（**in-sample**） |
| `limit` | int 1–100000 | 无 | 行数上限；**本次核心不消费该字段**（透传） |

返回（本次 b 路径）：
```json
{"ok": true, "job_id": "job-...", "data_source": "calibration", "n": 3,
 "metrics": {"accuracy": 0.9, "loss": 0.31, "ece": 0.04, "brier_top1": 0.11,
             "mean_conf": 0.88, "abstain_rate": 0.05},
 "per_question": [], "confusion": [], "errors": [],
 "note": "本次训练未落逐条预测"}
```
- 非终态 → HTTP 409 → `isError:true`。
- 该 job 没有 `train_report.json` → HTTP 200 + `{ok:false,error:"train_report.json not found: ..."}`（`isError:false`，是状态答案不是崩溃）。

### 2.12 `modelsmith_report_job` — 评测报告（Markdown）

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `id` | string | — | **必填** |
| `language` | string | `zh` | 报告语言提示 |
| `model` | string | 无 | 报告里回显的模型标签 |
| `template` | string | 无 | 模板提示（本次固定 template 引擎） |

返回：
```json
{"ok": true, "engine": "template", "model": "selftest-model", "elapsed_s": 0.01,
 "markdown": "# ModelSmith 评测报告\n...\n> in-sample ≠ 泛化：...",
 "json": {"job_id": "...", "data_source": "calibration", "n": 3, "metrics": {...},
          "confusion": [], "errors": [], "summary": {...}, "engine": "template", "generated_at": "..."}}
```
- **必须先 `/eval`**，否则 `{ok:false,error:"run /eval first"}`。
- 本次只返回 `engine:"template"`（模板四段：总体 / 错例 / 归因 / 建议 + in-sample 提醒）；不要断言 LLM 引擎。
- 若模板数字与 metrics 对不上，会带 `warn:"ungrounded_numbers"` + `ungrounded:[...]`（`grounded_numbers` 校验）。

### 2.13 `modelsmith_star_artifact` — 标记候选产物

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `id` | string | — | **必填** |
| `artifact` | string | — | **必填**，产物名/路径（如 `adapter_model.safetensors`） |
| `starred` | bool | `true` | `true` 加入、`false` 移除 |

返回 `{ok:true, job_id:"job-...", starred_artifacts:["adapter_model.safetensors"]}`。标记存在 job 的 `metrics.starred_artifacts` 里（`get_job` 可见）。

### 2.14 `modelsmith_open_path` — 校验 / 打开产物目录

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `path` | string | — | **必填**，白名单根：`models_dir`、`<project>\_runs`、`<project>` |
| `dry` | bool | **`true`** | `true` 只校验（**不弹窗**）；`false` 才真的打开资源管理器 |

返回 `{ok:true, dry:true, path:"E:\\视频\\modelsmith"}`。
- 越界（如 `C:\Windows`）→ HTTP 403 → `isError:true`、`{ok:false,status:403,body:{"error":"path is outside allowed roots: ..."}}`。
- `dry=false` 且路径不存在 → 404。**Agent 默认只传 `path`，永远 dry**；要真的打开必须由用户明说。

### 2.15 `modelsmith_download_model` — 下载模型（发起 + 轮询）

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `model_id` | string | — | **必填**，`list_models` 返回的 `id`（目录型模型会下到该目录里） |
| `sources` | string[] | 无 | URL 列表；核心**按顺序回退**（第一源失败换下一个）。不传 → 核心 400 |
| `timeout_s` | number 1–7200 | `600` | 轮询总超时 |
| `poll_s` | number 0.05–5 | `0.25` | 轮询间隔 |

行为：`POST /api/models/{id}/download` → 轮询 `GET .../download/status` 到终态（`succeeded`/`failed`）或超时；返回：
```json
{"ok": true, "model_id": "dummy-llm", "state": "succeeded", "timed_out": false,
 "waited_s": 0.2,
 "status": {"id": "dummy-llm", "bytes": 146, "total": 146, "pct": 100.0,
            "path": "...\\models\\dummy-llm\\f.bin", "errors": [], "...": "..."}}
```
- 坏源 / 失败 / 超时 → `ok:false` **且 `isError:true`**，且**不会**无限等（`waited_s` 有界）。
- 超时只停止轮询，**不取消**下载（`note` 写明）；要看后续状态再调一次本工具（核心会返回同一份 download 状态）。

**取消下载**：同一工具传 `cancel:true` → `POST /api/models/{id}/download/cancel`（**核心 R9 已实现**）。
- **有进行中的下载**：200 `{ok:true, id, state:"cancelled", bytes, ...}`，`.part` 被清掉；**幂等**——再调一次仍返回 `cancelled`（0 秒级）。
- **该 model 从未开始下载**：核心 404 `{"ok":false,"error":"download not found"}` → 工具返回
  ```json
  {"ok": false, "status": 404, "note": "该 model 当前没有下载任务（核心 404: download not found）",
   "body": {"ok": false, "error": "download not found"}}
  ```
  （`isError:false`，是状态答案不是崩溃。）
- **更老的核心（无该路由）**：404 `not found` → `{"ok": false, "note": "core 未提供 /api/models/{id}/download/cancel", ...}`（同样 `isError:false`）。

### 2.16 `modelsmith_job_artifacts` — 产物清单

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | **是** | job id（`/eval` 一样要求有 `out` 目录） |

返回：
```json
{"ok": true, "job_id": "job-...",
 "artifacts": [
   {"name": "model.safetensors", "path": "E:\\...\\v3-out\\model.safetensors", "bytes": 32, "sha256": "..."},
   {"name": "samples/fig.png", "path": "E:\\...\\samples\\fig.png", "bytes": 64, "sha256": "..."}
 ]}
```
**什么时候用**：训练成功后想确认"到底落了哪些文件、多大、哈希对不对"；等价于手动翻 `out` 目录但带哈希。

### 2.17 `modelsmith_job_sample` — 取采样图/样本文件

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `id` | string | — | **必填** |
| `path` | string | — | **必填**，相对 `<out>\samples` 的路径（如 `step-100/0.png`） |
| `include_b64` | bool | `false` | `true` 才回 `bytes_b64`；**> 2 MiB 直接拒（413）** |

默认返回（**不传二进制**）：
```json
{"ok": true, "job_id": "job-...", "path": "fig.png", "bytes": 64, "content_type": "image/png"}
```
`include_b64:true` 且 ≤2 MiB：
```json
{"ok": true, "job_id": "job-...", "path": "fig.png", "bytes": 64,
 "content_type": "image/png", "bytes_b64": "iVBORw0KGgo..."}
```
> 大图不要 base64：先拿元数据，再让用户/本地进程按 `path` 读文件。**>2 MiB 时工具返回 `{ok:false,error:"sample too large for b64 (> 2 MiB)",bytes:N}`（`isError:true`）**。

**什么时候用**：aitk/diffsynth 每次 `sample_every` 的验收图；`job_artifacts` 已经列出 `samples/*`，用它逐个取。

### 2.18 `modelsmith_pick_path` — 文件/目录选择

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `mode` | `"file"` \| `"dir"` | `file` | 选文件还是选目录 |
| `title` | string | 无 | 对话框标题 |
| `filter` | string 或 string[] | 无 | 扩展名过滤（如 `["jsonl","csv"]`） |
| `dry` | bool | **`true`** | `true` 只返回 dry 说明（**不弹窗**）；`false` 才真正调 `POST /api/pick` |

- `dry=true`（默认）返回：`{ok:false, dry:true, mode:"file", note:"dry mode: no dialog opened. Pass dry=false ..."}` —— **Agent 默认不要弹窗**。
- `dry=false`：核心若以 `--pick-stub` 启动，直接返回桩路径（测试用）；否则会弹**原生对话框**，需要本机桌面会话，最长等 180 s，取消返回 `{ok:false,cancelled:true}`，无桌面返回 `{ok:false,reason:"no_desktop"}`。
- **什么时候用**：用户说"让我选个文件"时；Agent 自己不要主动弹。

### 2.19 `modelsmith_early_stop` — 早停

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `id` | string | — | **必填** |
| `enabled` | bool | **`false`** | 开/关（默认关闭 = 现有行为不变） |
| `patience` | int 1–1000 | `2` | 连续多少个 epoch 没改善就停 |
| `min_delta` | number ≥0 | `0.01` | 改善幅度小于它就记为"没改善" |

返回：
```json
{"ok": true, "job_id": "job-...",
 "early_stop": {"enabled": true, "patience": 2, "min_delta": 0.01}}
```
**什么时候用**：长训（epochs 8+）时防止过拟合多烧时间；触发后 job 会 `stop`（记 `metrics.early_stop`），**无 resume**。
非数字/`patience<1` → 400 → `isError:true`。

> **降级口径（所有端点工具一致）**：核心没升级时对应路由 404 → 工具返回
> `{"ok": false, "note": "core 未提供 <route>", "status": 404, "body": {...}}`，**`isError:false`、不抛**。
> Agent 看到 `ok:false` + `note` 就该降级（用 `get_job.metrics` / 现有路径），不要重试同一工具。

---

## 3. 四种 kind 的完整 params

> 字段名与默认值逐条对照 `core\README.md`：Laya §13.3、LlamaFactory §17.2、DiffSynth §16.2、ai-toolkit §15.2。
> **不要自创别名**；`train_command` 在 MCP 层已被禁。

### 3.1 `kind="laya"`（决策模型）

| 字段 | 类型 | 必填 | 默认 | 什么时候改它 |
|---|---|---|---|---|
| `data` | str | **是** | — | 训练 `.jsonl/.csv` 换成新数据时 |
| `out` | str | **是** | — | 每次实验换新目录（无 resume，旧目录只留 `checkpoint_latest/`） |
| `base` | str | 否 | `convaiinnovations/laya` | 换底座（本地路径 / alias / Hub id） |
| `loss` | str | 否 | `rlcd` | 数据标签噪声大时可试 `soft-ce` |
| `epochs` | int | 否 | `4` | 欠拟合加、过拟合减（见 `skills\modelsmith-mcp\SKILL.md`） |
| `micro_batch` | int | 否 | `8` | 显存不够时减小（8→4→2） |
| `grad_accum` | int | 否 | `8` | 显存小但要保持有效 batch 时加大（与 micro_batch 反向调） |
| `encoder_lr` | float | 否 | `2.5e-5` | 底座不动时默认；要更大改动时上调到 5e-5 |
| `head_lr` | float | 否 | `1e-4` | 决策头学得慢就上调（注意过拟合） |
| `max_len` | int/null | 否 | 取 base config | 题目/状态超长被截断时调大 |
| `head_max_len` | int/null | 否 | 取 base config | 选项很多（题目 head 变长）时 |
| `shuffle_options` | bool | 否 | `false` | 选项顺序有偏（模型记位置）时开 `true` |
| `label_smoothing` | float [0,1) | 否 | `0.0` | 标签硬且噪声大时开 0.05–0.1 |
| `target_error` | float | 否 | `0.10` | 拒答太激进/太保守时调（目标错误率） |
| `min_abstain_n` | int | 否 | `10` | 小类目太少导致拒答阈值不稳时 |
| `eval_data` | str/null | 否 | 无 | **有留出集一定要填**，否则 before/after 是 in-sample |

### 3.2 `kind="lf"`（LLM / LlamaFactory 主线）

| 字段 | 类型 | 必填 | 默认 | 什么时候改它 |
|---|---|---|---|---|
| `model` | str | **是** | — | 换 HF 模型目录（Qwen2.5-0.5B 起步） |
| `data` | str | **是** | — | dolly 形状 JSONL（`instruction/context/response`） |
| `out` | str | **是** | — | 每次实验新目录 |
| `template` | str | **是** | — | 换模型族必须换模板（Qwen2.5 用 `qwen`，Qwen3 用 `qwen3` 等） |
| `rank` | int | 否 | `8` | 欠拟合且显存有富余时 8→16 |
| `alpha` | int | 否 | `16` | 与 rank 同比例（rank 的 2 倍） |
| `lr` | float | 否 | `2e-4` | loss 不降→上调；震荡/发散→下调 |
| `epochs` | float | 否 | `3` | 欠拟合加、过拟合减 |
| `max_steps` | int | 否 | `0`（按 epochs） | 只做 smoke 时设 `2`；`>0` 会覆盖 epochs |
| `batch` | int | 否 | `1` | 显存够时 2–4 提升吞吐 |
| `grad_accum` | int | 否 | `1` | 显存小但想要大有效 batch 时 |
| `cutoff_len` | int | 否 | `512` | 样本被截断（长文摘要）时调大 |
| `logging_steps` | int | 否 | `1` | 长训时调大减少日志/指标噪声 |
| `save_steps` | int | 否 | `0`（用 LF 默认 500） | 想常留 checkpoint 时设小（如 100） |

### 3.3 `kind="diffsynth"`（Qwen-Image 2.1 LoRA）

| 字段 | 类型 | 必填 | 默认 | 什么时候改它 |
|---|---|---|---|---|
| `model` | str | **是** | — | 换 diffusers 底模目录时 |
| `dataset` | str | **是** | — | 换图片文件夹时 |
| `out` | str | **是** | — | 每次实验新目录 |
| `metadata` | str | 否 | `<dataset>\metadata.csv` | caption 表不在默认位置时 |
| `processor` | str | 否 | `<model>\processor` | 换处理器时 |
| `rank` | int | 否 | `8` | 欠拟合 8→16 |
| `epochs` | int | 否 | `1` | 小图集一遍不够时加 |
| `lr` | float | 否 | `1e-4` | 收敛慢上调、过拟合下调 |
| `height` / `width` | int | 否 | `512` | 显存不够降、细节不够升（需 512 的倍数） |
| `dataset_repeat` | int | 否 | `1` | 图太少（<20 张）时重复采样 |

### 3.4 `kind="aitk"`（生图 / 视频 LoRA）

| 字段 | 类型 | 必填 | 默认 | 什么时候改它 |
|---|---|---|---|---|
| `model` | str | **是** | — | 换底模 |
| `dataset` | str | **是** | — | 换图/视频文件夹（同名 caption `.txt`） |
| `out` | str | **是** | — | 每次实验新目录 |
| `arch` | str | **是** | — | 换架构（`flux` / `wan22_14b` …；**上游枚举**，见 core §15.2） |
| `steps` | int | 否 | `1000` | 采样/收敛不够时加 |
| `batch` | int | 否 | `1` | 显存够时加大 |
| `lr` | float | 否 | 上游默认 | 不写就不改上游默认 |
| `rank` / `alpha` | int/float | 否 | 上游默认 | 欠拟合时加 rank |
| `resolution` | int/list | 否 | 上游默认 | 显存/细节权衡 |
| `grad_accum` | int | 否 | `1` | 有效 batch 不够时 |
| `save_every` / `keep` | int | 否 | `1000` / `5` | 想多留中间权重时 |
| `sample_every` | int | 否 | `100` | **验收信号**：采样太密拖慢、太稀看不到趋势 |
| `sample_start` / `sample_steps` | int | 否 | `0` / `20` | 正式采样前先热身 / 采样质量（步数越多越慢） |
| `guidance` | float | 否 | `7` | 采样贴合度 |
| `sample_width` / `sample_height` | int | 否 | `512` | 采样图尺寸 |
| `num_frames` | int | 否 | `1` | 视频 LoRA 帧数（>1） |
| `trigger_word` | str/null | 否 | `null` | 需要触发词时（注意 `cache_text_embeddings=true` 时 trigger 不生效） |
| `caption_ext` | str | 否 | `.txt` | caption 后缀不同时 |
| `sample_prompts` | str[] | 否 | `[]` | **必填式建议**：空 = 关掉唯一验收信号 |
| `sampler` / `noise_scheduler` | str | 否 | `ddpm` | 必须成对一致（flowmatch 模型用 `flowmatch`） |
| `save_format` | str | 否 | `safetensors` | 枚举 `safetensors/diffusers` |

---

## 4. 三条使用范式

### ① 一次 smoke（先证明管线通，1–3 分钟）

```json
{"kind":"laya","params":{"data":"…\\train.jsonl","out":"…\\runs\\laya-smoke1","epochs":1}}
{"kind":"lf","params":{"model":"…\\Qwen2.5-0.5B-Instruct","data":"…\\dolly-15k\\databricks-dolly-15k.jsonl",
                       "out":"…\\runs\\lf-smoke1","template":"qwen","max_steps":2,"logging_steps":1}}
{"kind":"diffsynth","params":{"model":"…\\Qwen-Image-2.1","dataset":"…\\imgs","out":"…\\runs\\ds-smoke1",
                              "rank":8,"epochs":1,"height":512,"width":512,"dataset_repeat":1}}
```
判据：`wait_job` 到 `succeeded`；`job.metrics.step>0` 且 `loss` 有限；`out` 下出现产物（laya `train_report.json` / lf `adapter_model.safetensors` / diffsynth `epoch-0.safetensors`）；再挑一条 `job_logs` 看有没有 `Traceback`。**smoke 只看管线，不看效果。**
smoke 跑完接一句：`metrics_history`（看曲线）→ `eval_job`（没留出集就是校准切片）→ `report_job`（拿 Markdown）→ `star_artifact`（标记这次产物，可选）。

### ② 长训（能用，但要会看曲线）

```json
{"kind":"laya","params":{"data":"…","out":"…\\runs\\laya-long1","epochs":8,"micro_batch":8,"grad_accum":8,
                         "eval_data":"…\\eval.jsonl","target_error":0.1}}
```
- 进度看 `metrics.updates_total`（预算）与 `metrics.epoch`；**`micro_step` ≠ 更新步**，别拿它当分母。
- 收敛看 `epoch_mean_loss`（每 epoch 一条）；`checkpoint_latest` 每轮更新；有 `eval_data` 才有 `evaluation` 的 before/after。
- lf 长训看 `loss` 序列与 `train_runtime`/`peak_vram`，`save_steps>0` 才会留 `checkpoint-N/`。
- aitk 长训看 `sample_groups`（每 `sample_every` 一组采样图）与 `warnings`（末步不整除、空 prompts）。
- 长训期间用 **`metrics_history`** 增量拉曲线（`since=next_seq`，比反复 `get_job` 省）；跑完 `eval_job` → `report_job`（Markdown）→ `star_artifact`（标记候选）→ `open_path`（**dry** 确认产物目录）。

### ③ 取消与「无 resume」

```json
{"name":"modelsmith_cancel_job","arguments":{"id":"job-..."}}
{"name":"modelsmith_wait_job","arguments":{"id":"job-...","timeout_s":60}}
```
- 取消是**软停优先**（写 `jobs\<id>\stop` + `CTRL_BREAK_EVENT`/`SIGINT`，Laya/aitk 5 s、LF 10 s 后 `taskkill /T /F`）；终态 `cancelled`。
- **没有 resume**：重训只能新开 job + 新 `out`；`checkpoint_latest/` 只能拿回上一轮产物，不能接着训。取消前先想清楚要不要留。
- 任务在跑时**不要**再提交第二个 GPU 作业（见 §0.3）。
- 取消后仍可对已落盘产物调 `eval_job` / `report_job`（只要 job 是终态），但**取消的 job 不能当验收结果**。

---

## 5. 错误与降级速查

| 情形 | MCP 表现 |
|---|---|
| 核心没起 / 端口错 | `isError:true`，`{ok:false,error:"core unreachable: ConnectionRefusedError: ..."}`，进程继续活着 |
| token 错/缺 | `isError:true`，`{ok:false,status:401,body:{ok:false,error:"unauthorized"}}` |
| 未知工具 | JSON-RPC `-32602`（不是工具结果） |
| 参数类型错（如 `id` 不是字符串） | `isError:true`，`{ok:false,error:"id must be a non-empty string"}` |
| `/api/models` 404 | `isError:false`，`{ok:false,note:"core 未提供 /api/models"}` |
| 提交含 `train_command` | `isError:true`，`{ok:false,error:"train_command is a test hook, not allowed via MCP"}` |
| 取消已终态任务 | `isError:true`，`{ok:false,status:409,body:{error:"job already finished"}}` |

## 6. 端点覆盖情况（R4 v2 + R11 v3）

已暴露为工具（v2 新增 6 个 + v3 新增 4 个）：

| 核心路由 | MCP 工具 |
|---|---|
| `GET /api/models?kind=&refresh=` | `modelsmith_list_models`（`refresh` 已透传） |
| `POST /api/models/{id}/download` + `GET .../download/status` | `modelsmith_download_model`（合并：发起+轮询） |
| `POST /api/models/{id}/download/cancel` | `modelsmith_download_model {cancel:true}`（**核心 R9 已实现**：真取消 + 幂等；无下载任务/旧核心走 404 降级） |
| `GET /api/jobs/{id}/metrics_history?since=` | `modelsmith_job_metrics_history` |
| `POST /api/jobs/{id}/eval` | `modelsmith_eval_job` |
| `POST /api/jobs/{id}/report` | `modelsmith_report_job` |
| `POST /api/jobs/{id}/star` | `modelsmith_star_artifact` |
| `POST /api/open` | `modelsmith_open_path`（默认 dry） |
| `GET /api/jobs/{id}/artifacts` | `modelsmith_job_artifacts` |
| `GET /api/jobs/{id}/samples/<rel>` | `modelsmith_job_sample`（默认元数据，b64 ≤2 MiB） |
| `POST /api/pick` | `modelsmith_pick_path`（默认 dry，不弹窗） |
| `POST /api/jobs/{id}/early_stop` | `modelsmith_early_stop`（默认 disabled） |

**仍未暴露**（核心有、MCP 没有）：`POST /admin/shutdown`（只给本地运维，不给 Agent）。
需要时按下一次任务卡补工具；**不要在 MCP 里用 HTTP 直连绕过**。
