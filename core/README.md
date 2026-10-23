# ModelSmith 核心服务 MVP（`core/`）

版本 `0.1.0` · Python 3.10+ · **只用标准库，零 pip 依赖** · 只绑定 `127.0.0.1`

这是 PLAN §3.1 的第一里程碑：给后续 UI 接线和三个训练适配器提供**唯一真相源**。
当前执行 `kind="fake"`、`kind="laya"`、`kind="llm"`、`kind="aitk"`、`kind="diffsynth"` 与 `kind="lf"`（Laya/LLM/ai-toolkit/DiffSynth/LlamaFactory 路由已接；Laya 真训练尚未跑；LLM 真 GPU smoke 在适配器层已单独报过；ai-toolkit 已在 ComfyUI Python + `.aitk-pylibs` 下跑通 SD1.5 1 步 smoke；DiffSynth 的 Qwen-Image 2.1 recipe 已跑通 4 步 smoke；LlamaFactory 适配器层已跑通 2 步 smoke（§10.7）。核心这三条新路由本次仍只用桩验证）。

---

## 1. 快速启动

```powershell
# 默认：127.0.0.1:8765，状态目录 %LOCALAPPDATA%\ModelSmith
python E:\视频\modelsmith\core\modelsmith_core.py

# 端口被占用 / 要并行跑多个实例：随机端口，自动写 core.json
python E:\视频\modelsmith\core\modelsmith_core.py --port 0

# 临时状态目录（自测、隔离实验）
python E:\视频\modelsmith\core\modelsmith_core.py --port 0 --data-dir C:\temp\ms-core

# 指定项目根（默认取本文件上一级，即 E:\视频\modelsmith）
python E:\视频\modelsmith\core\modelsmith_core.py --project-root E:\视频\modelsmith

# Laya 子进程环境（默认值如下；工程移动时才需要显式指定）
python E:\视频\modelsmith\core\modelsmith_core.py `
  --laya-python 'E:\ComfyUI\python_embeded\python.exe' `
  --laya-src 'E:\视频\modelsmith\third_party\laya'

# LLM 子进程 Python（默认 ComfyUI embedded python；脚本固定 adapters\llm_lora.py）
python E:\视频\modelsmith\core\modelsmith_core.py `
  --llm-python 'E:\ComfyUI\python_embeded\python.exe'

# ai-toolkit 子进程环境（ComfyUI python + .aitk-pylibs；pinned diffusers 在 libs 里）
python E:\视频\modelsmith\core\modelsmith_core.py `
  --aitk-python 'E:\ComfyUI\python_embeded\python.exe' `
  --aitk-libs 'E:\视频\modelsmith\.aitk-pylibs' `
  --aitk-src 'E:\视频\modelsmith\third_party\ai-toolkit'

# DiffSynth-Studio（Qwen-Image 2.1）子进程环境
python E:\视频\modelsmith\core\modelsmith_core.py `
  --diffsynth-python 'E:\ComfyUI\python_embeded\python.exe' `
  --diffsynth-src 'E:\视频\modelsmith\third_party\DiffSynth-Studio'
```

启动后：

- UI：<http://127.0.0.1:8765/>（由本服务直接托管 `design\prototype\index.html`，并在内存响应里注入 token 引导；磁盘文件保持原字节，避免 `file://` CORS）
- 健康检查：<http://127.0.0.1:8765/health>（**免 token**）
- 随机端口时，读取 `%LOCALAPPDATA%\ModelSmith\core.json` 里的 `port` / `base_url`
- 按 `Ctrl+C` 或 `POST /admin/shutdown` 优雅退出；会停掉正在跑的 worker 并标记 `interrupted`

> **不会**绑定 `0.0.0.0`，没有 `--host` 参数；端口错误会直接报错退出，不会静默换端口。

---

## 2. 数据目录

默认 `%LOCALAPPDATA%\ModelSmith`（本机即 `C:\Users\Administrator\AppData\Local\ModelSmith`）。
`--data-dir` 可整体改到别处。

| 路径 | 内容 |
|---|---|
| `core.db`（+ `-wal` / `-shm`） | SQLite 作业表，**WAL 模式** |
| `token` | 首次启动随机生成的鉴权 token（43 字符 URL-safe） |
| `core.json` | 当前绑定地址/端口/pid/token 路径/UI 路径/Laya/LLM/aitk/diffsynth 环境；干净退出时删除 |
| `logs\<job_id>.log` | 每个作业的 UTF-8 日志，按字节 offset 尾随 |
| `locks\gpu-0.lock` | GPU 文件锁；状态行含 `pid=` / `job=` / `acquired_at=` |
| `jobs\<job_id>\metrics.json` | 假任务每步原子写入的 metrics 快照 |
| `jobs\<job_id>\stop` | 取消时先写的温和停止标志（worker 轮询后自行退出） |

---

## 3. Token 鉴权

- `/health` **免鉴权**。
- 其余 `/api/*` 以及 `/env/doctor` 要求请求头 `X-ModelSmith-Token`（也接受 `Authorization: Bearer <token>`）。
- 缺失或错误 → `401`，响应体含 token 文件路径提示。
- **`/env/doctor` 不在 `/api/` 下但仍要求 token**：这是按“除 `/health` 外都保护”的安全默认值做的；任务单只显式豁免了 `/health`。

### 3.1 UI 的 token 引导（内存注入，磁盘只读）

`GET /`（以及 `/index.html`）返回 UI 时，服务在 `</head>` 前插入一行：

```html
<script>window.__MS_TOKEN__="<token>";window.__MS_API__="/api";</script>
```

- `design\prototype\index.html` **一个字节都不改**（冻结基线）；注入只发生在 HTTP 响应内存里，响应体会比磁盘文件大（当前 token 43 字符时 +108 B）。
- 页面 JS 直接读 `window.__MS_TOKEN__` 与 `window.__MS_API__` 调核心 API，不需要再读 token 文件。
- 这意味着**能 GET 到 `/` 的本机进程就能读到 token**；服务只绑 loopback，不要把它暴露到局域网/公网。

PowerShell 读取 token（中文路径按 UTF-8，禁用 `Get-Content` 的默认编码）：

```powershell
$token = [IO.File]::ReadAllText("$env:LOCALAPPDATA\ModelSmith\token", [Text.Encoding]::UTF8).Trim()
$headers = @{ "X-ModelSmith-Token" = $token }
Invoke-RestMethod -Headers $headers http://127.0.0.1:8765/api/jobs
```

---

## 4. HTTP API

所有 JSON 响应都带 `ok` 字段；错误响应为 `{ "ok": false, "error": "..." }`。
请求体上限 1 MiB；日志单次最多返回 1000 行 / 256 KiB。

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/health` | 否 | `{ok,version,pid,uptime_s,host,port,db,schema,jobs}`；`jobs` 是各状态计数 |
| GET | `/env/doctor` | 是 | Python / torch / CUDA+驱动 / 磁盘 / 端口 / GPU 锁 / 路径；30s 缓存，`?refresh=1` 强刷 |
| POST | `/api/jobs` | 是 | body `{kind, params, idempotency_key?}`，立即返回 `202`（重放返回 `200`） |
| GET | `/api/jobs` | 是 | 列表；`?state=queued|running|...&limit=100`，返回 `jobs` + `counts` |
| GET | `/api/jobs/{id}` | 是 | 单作业；含 `params`、`metrics`、`queue_position`、`log_size` |
| GET | `/api/jobs/{id}/logs` | 是 | `?offset=0&max_lines=200`；返回 `offset,next_offset,size,eof,lines` |
| POST | `/api/jobs/{id}/cancel` | 是 | 排队中直接 `cancelled`；运行中先温和后强杀；已结束返回 `409` |
| POST | `/admin/shutdown` | 是 | 优雅退出（停 worker、关 DB、删 `core.json`） |
| GET | `/`、`/index.html` | 否 | 返回冻结 UI 基线 + 内存注入的 token 引导（`text/html; charset=utf-8`；磁盘文件不变） |
| GET | `/assets/*` | 否 | 预留静态资源路由；依次查 `design\assets\`、`core\assets\`、`design\prototype\`，未命中 404 |

### 4.1 提交假任务

```powershell
$body = @{
  kind = "fake"
  params = @{ steps = 20; interval_s = 1.0 }
  idempotency_key = "demo-1"
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Headers $headers `
  -ContentType "application/json; charset=utf-8" `
  -Body ([Text.Encoding]::UTF8.GetBytes($body)) `
  http://127.0.0.1:8765/api/jobs
```

响应：

```json
{
  "ok": true,
  "job_id": "job-20261022-051031-9bd44262",
  "state": "queued",
  "queue_position": 1,
  "idempotent_replay": false,
  "job": { "...": "完整作业对象" }
}
```

- `kind` 只允许 `^[a-z][a-z0-9_-]{0,31}$`；当前只有 `fake` 有 worker。
- `params.steps` 默认 `20`（范围 1–10000），`params.interval_s` 默认 `1.0`（范围 0.01–60）。
- `params.ignore_soft_stop` 默认 `false`；仅作为**负控测试钩子**：设为 `true` 时 fake worker 故意忽略 stop 文件，用来确定性地验证 15 秒后的 `taskkill` 硬杀路径。
- 同一 `idempotency_key` 重复提交 → 返回原 `job_id`、HTTP `200`、`idempotent_replay: true`，SQLite 里只有一行。
- 其他 `kind` 会正常入队，但立即 `failed`，`error` 明写 `no adapter for kind ... in MVP (only 'fake')`——**不假装成功**。

### 4.2 日志尾随

```powershell
Invoke-RestMethod -Headers $headers `
  "http://127.0.0.1:8765/api/jobs/<job_id>/logs?offset=0&max_lines=200"
```

```json
{
  "ok": true, "job_id": "...", "offset": 0, "next_offset": 780,
  "size": 780, "eof": true, "lines": ["[...] fake worker start ...", "..."],
  "max_lines": 200
}
```

`offset` 是**字节**偏移；下一次把 `next_offset` 原样传回即可增量尾随。中断/重启后直接用 `GET /api/jobs/{id}` 校正状态，日志文件仍在。

### 4.3 取消

```powershell
Invoke-RestMethod -Method Post -Headers $headers `
  http://127.0.0.1:8765/api/jobs/<job_id>/cancel
```

- `queued`：立即 `cancelled`（`error=cancelled before start`），**不会 spawn**，`pid` 保持 `NULL`。
- `starting/running`：先写 `jobs\<id>\stop`，之后**轮询 worker 是否退出，上限 15 秒**；15 秒仍未退才 `taskkill /T /F /PID <pid>`（随后 3 秒未收尸再 `proc.kill()` 兜底）。
- 已结束：`409 {ok:false,error:"job already finished",state:"..."}`。
- 取消**不可恢复**：MVP 没有 resume，重新提交就是新 job。

---

## 5. 作业状态机

```
queued → starting → running → succeeded
   │         │         ├──→ failed
   │         │         ├──→ cancelled
   │         │         └──→ interrupted（核心重启/关机时）
   └─────────┴──────────→ cancelled（启动前取消）
```

- **串行调度**：一个核心进程同一时刻最多 1 个 `starting/running`；其余排队。
- **GPU 锁**：调度器在 spawn 前拿 `locks\gpu-0.lock` 的 OS 字节锁（Windows `msvcrt.locking`，POSIX `fcntl.flock`），拿到才把作业置 `running`；进程崩溃时 OS 自动释放锁。锁文件同时记录 `pid/job/acquired_at`，便于人查。
- **崩溃恢复**：启动时把遗留的 `starting/running` 行标 `interrupted`。若旧 worker 的 pid 还活着，也标 `interrupted` 并在 `error` 里写“orphan worker not adopted”——**MVP 不接管孤儿进程**，需要人工清理（见 §10）。

---

## 6. SQLite 表

```sql
CREATE TABLE jobs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  state TEXT NOT NULL,
  pid INTEGER,
  log_path TEXT,
  metrics_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  idempotency_key TEXT,
  params_json TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  exit_code INTEGER,
  error TEXT
);
CREATE UNIQUE INDEX idx_jobs_idempotency ON jobs(idempotency_key)
  WHERE idempotency_key IS NOT NULL;
```

验证 WAL（服务运行中也可读）：

```powershell
python -c "import sqlite3,os; p=os.path.expandvars(r'%LOCALAPPDATA%\ModelSmith\core.db'); c=sqlite3.connect(p); print(c.execute('PRAGMA journal_mode').fetchone()[0]); print(c.execute('SELECT state,COUNT(*) FROM jobs GROUP BY state').fetchall())"
```

---

## 7. `GET /env/doctor` 读数

| 字段 | 来源 | 失败时 |
|---|---|---|
| `python.version` / `executable` / `bits` | 当前核心进程 `sys` | — |
| `torch.found` / `version` | 探测 `E:\ComfyUI\python_embeded\python.exe`，用 `importlib.util.find_spec` + `metadata.version`（不 import torch，秒回） | `found:false` + `error` |
| `cuda.status` / `driver_version` / `gpus` | `nvidia-smi --query-gpu=...` | `status:"unknown"` + `error` |
| `disk.free_gb` / `total_gb` / `used_gb` | `shutil.disk_usage(data_dir)` | — |
| `ports` | 对 `8765` 和当前端口做 TCP connect | `listening:false` |
| `gpu_lock` | 锁文件路径/字节数/内容 | `exists:false` |
| `laya` | `--laya-python` / `--laya-src` / adapter 路径与存在性 | `*_exists:false` |
| `llm` | `--llm-python` / `adapters\llm_lora.py` 路径与存在性 | `*_exists:false` |
| `aitk` | `--aitk-python` / `--aitk-libs` / `--aitk-src` / `run.py` / adapter 路径与存在性 | `*_exists:false` |
| `diffsynth` | `--diffsynth-python` / `--diffsynth-src` / `train.py` / adapter 路径与存在性 | `*_exists:false` |
| `paths` | 数据目录、日志、DB、token、UI 路径与 UI 字节数 | — |

可用环境变量 `MODELSMITH_TORCH_PYTHON` 改探测用的解释器。

---

## 8. 自测

```powershell
python E:\视频\modelsmith\core\selftest_core.py
```

- 实际起一个**随机端口**的核心服务（临时数据目录，且数据目录名是中文 `中文 状态`，验证 Windows 中文路径）。
- 覆盖：`/health` 200、`GET /` **内存注入 token 引导**且磁盘冻结 UI 字节不变、无 token/错 token 401、`/env/doctor` 字段、netstat 只监听 `127.0.0.1` 且无 `0.0.0.0`、`127.0.0.2` 负控不可达、WAL、idempotency 只一行、成功假任务、日志 offset 至少推进 2 次、两作业并发时 DB `running=1`、GPU 锁跨进程互斥、排队取消不 spawn、运行中取消后 worker pid 已死、软停日志、默认 20 步/1 秒、`ignore_soft_stop` 负控下轮询 15 秒后 `taskkill` 硬杀、**Laya 默认 `-c` runner**、**Laya 路由（桩）成功/取消/缺 data+out 失败**、**LLM 路由（桩）成功/取消/缺 model 失败**、**aitk 新 runner 形状/默认值**、**aitk 路由（桩）13 组/1250 警告/取消/缺 model/环境缺失报错**、**diffsynth runner 形状/环境**、**diffsynth 路由（桩）4 步/artifact/取消/缺 model**、未知 kind 诚实失败、重启后 `running→interrupted`。
- 输出末行 `RESULT: PASS n / FAIL m`；非 0 退出码代表失败。
- 失败时保留临时数据目录并打印路径；成功默认删除。加 `--keep` 可保留成功现场。

> 本机基线：`PASS 186 / FAIL 0`（Python 3.12.8，2026-10-22；连续 3 次复跑见任务回执）。

---

## 9. 运行约束

- **仅 Windows 优先**：GPU 锁用 `msvcrt`，强杀用 `taskkill`；POSIX 有降级实现但未在本项目验收。
- **端口严格独占**：Windows 下关闭了 `SO_REUSEADDR`，第二个实例绑定同一端口会以 `WinError 10048` 退出（不会静默抢占）；固定端口被占时用 `--port 0`。
- **单实例单数据目录**：不同 `--data-dir` 可并行；同一数据目录请只跑一个核心（SQLite 能承受多读，但调度语义未设计多写者）。
- **无 shell**：spawn 与 `taskkill` 都是参数数组，`shell=False`。
- **静态资源路径解析**：`/assets/*` 解析后必须位于允许根内，防目录穿越。
- **无 log rotation**：日志会一直长；MVP 由调用方按 `size` 自行截断/归档（后续加上限）。
- **无 SSE**：PLAN 里 SSE 只做变更通知，本 MVP 先给 `GET /api/jobs/{id}` + 字节 offset 轮询；断线以详情为准。

---

## 10. 已知限制 / 未做（诚实清单）

1. **核心路由 fake + laya + llm + aitk + diffsynth + lf**：六条路由都已接入核心调度。Laya 只用桩 CLI 验证；LLM 适配器层已有真实 20 步 GPU smoke，但核心 `kind="llm"` 仍只用桩验证；ai-toolkit 已在 ComfyUI Python + `.aitk-pylibs` 下跑通 SD1.5 1 步 smoke，核心 `kind="aitk"` 仍只用桩验证；DiffSynth 的 Qwen-Image 2.1 recipe 已跑通 4 步 smoke，核心 `kind="diffsynth"` 仍只用桩验证；LlamaFactory（`kind="lf"`）适配器层已跑通 2 步 smoke，核心也已用当前代码真跑 2 步（step=2/total=2、loss_series step [1,2]），另有 3 次连续 `PASS 186 / FAIL 0`（含真 import 探针 `llamafactory 0.9.5` + 去 `\src` 负控）。
2. **UI 已接 fetch**：`GET /` 把 token/API 前缀注入 `window.__MS_TOKEN__` / `window.__MS_API__`；UI 已接 fetch（`kind` 见 §13–§17），`file://` 下仍走模拟。
3. **没有 SSE**；没有 WebSocket；没有 `GET /metrics`（Prometheus）。
4. **没有多 GPU / gpu_set 调度**：锁固定 `gpu-0.lock`，单卡。
5. **孤儿 worker 守护（已实现）**：启动时扫 `jobs\<id>\pid|worker.pid` 与 DB pid；命令行含 `modelsmith_core.py`/适配器标记才写 stop、等 5 s 再 `taskkill /T /F`，job error 记 `orphan_reaped`；无关 pid 不动。`--no-orphan-reap` 可关。
6. **无鉴权静态资源 / 无 CORS / 无 TLS**：只服务 loopback；不要暴露到局域网。
7. **无速率限制 / 无审计日志**；`/env/doctor` 会暴露本机硬件与路径（已要求 token）。
8. **无 Windows 服务 / 开机自启 / 托盘**；当前是前台进程。
9. **无 remote / MCP 适配器**：PLAN §3.3 的 16 个 MCP 工具尚未实现。
10. **无 DB 迁移框架**：`PRAGMA user_version=1`，未来改表要手写迁移。
11. **取消硬杀 15 秒阈值是常量**，没有按后端类型区分；真实训练接入时需按后端调整。
12. **显存准入（已实现）**：提交前 `nvidia-smi` 查 used/total；kind 最小需求 laya 2 / lf 2 / diffsynth 12 / aitk 6 GiB（`params.est_vram_gib` 可覆盖）+ `--vram-margin-mib` 默认 1500 → 不够 409 点名；`--no-vram-check` 应急；探测失败放行并记 `vram_probe_failed`。job 收尾补 `peak_vram_mib`（已有 `peak_vram` 不覆盖）。
13. **`/env/doctor` 的 torch 只证明“该解释器能发现 torch”**，不证明 CUDA 可用；CUDA 状态来自 `nvidia-smi`，两者独立。

---

## 11. 文件

| 文件 | 作用 |
|---|---|
| `E:\视频\modelsmith\core\modelsmith_core.py` | 服务本体（HTTP + SQLite + 调度 + 假 worker + 静态托管） |
| `E:\视频\modelsmith\core\selftest_core.py` | 可重复黑盒自测（`PASS/FAIL` 计数，非 0 退出码） |
| `E:\视频\modelsmith\core\README.md` | 本文件 |

---

## 12. Laya 适配器（P3 准备，**本次不真跑**）

### 12.1 文件与边界

| 文件 | 作用 |
|---|---|
| `E:\视频\modelsmith\adapters\laya_adapter.py` | 产品适配器：`build_argv` / `parse_line` / `run`（stdlib only） |
| `E:\视频\modelsmith\adapters\selftest_laya_adapter.py` | 无 GPU 自测（假 CLI 桩，PASS/FAIL 计数） |
| `E:\视频\modelsmith\adapters\_tmp\laya_stub.py` | **仅测试**的假 `laya-train`；打印 20 条真实格式 stdout + 1 条校准告警 stderr |

本次**不安装 Laya、不下载模型、不跑 GPU**；真跑 20 步由 维护者 报数后单独起。核心 HTTP 调度器**已接** `kind="laya"` 路由（见 §13），但本次只用桩 CLI 验证；真模型训练尚未跑。

### 12.2 `build_argv(params)` 与真实参数出处

只发 `laya/train_cli.py` 里真实存在的 flag；`items` / `calib_frac` / `calib_max` 是 UI/Python-only，**不会**变成 CLI 参数。参数顺序固定，方便对拍。

| 作业参数 | CLI flag | `third_party\laya\laya\train_cli.py` |
|---|---|---|
| `data`（必填） | `--data` | 24–27 |
| `out`（必填；dry-run 才可省） | `--out` | 36–42；必填校验 176 |
| `base` | `--base` | 44–49 |
| `loss` | `--loss`（`rlcd` / `soft-ce`） | 105–109 |
| `epochs` | `--epochs` | 75–79 |
| `micro_batch` | `--micro-batch` | 81–85 |
| `grad_accum` | `--grad-accum` | 87–91 |
| `encoder_lr` | `--encoder-lr` | 93–97 |
| `head_lr` | `--head-lr` | 99–103 |
| `max_len` | `--max-len` | 150–154 |
| `head_max_len` | `--head-max-len` | 156–161 |
| `shuffle_options` | `--shuffle-options` | 117–120 |
| `label_smoothing` | `--label-smoothing` | 111–115 |
| `target_error` | `--target-error` | 127–131 |
| `min_abstain_n` | `--min-abstain-n` | 133–137 |
| `eval_data` | `--eval-data`（`--eval` 别名） | 29–34 |

- `build_argv(params, train_command=None)`：默认命令 `["laya-train"]`；`train_command` 仅供桩/已安装 CLI/注入 runner 覆盖。
- 数值/枚举在适配器侧先校验（`loss`、`label_smoothing∈[0,1)`、`target_error∈[0,1]`、正整数），失败抛 `ValueError`，不静默塞给上游。
- 自测里有一条静态检查：**每个发出去的 flag 都必须出现在 `train_cli.py` 源文本里**（当前 16 个，0 missing）。

### 12.3 `parse_line(line)`：两个分母必须分开

| 行型 | 示例 | 返回字段 |
|---|---|---|
| 预算 | `optimizer budget: 5600 train item(s), effective batch up to 64 (micro_batch 8 x grad_accum 8), 352 optimizer updates over 4 epoch(s)` | `type=budget`、`updates_total=352`、`epochs_total=4`、`train_items=5600`、`effective_batch/micro_batch/grad_accum`；**不含 `micro_step`** |
| loss | `epoch 1/4 step 100 loss 1.2345` | `type=loss`、`micro_step=100`、`loss=1.2345`、`step_kind=micro_batch`；**不含 `updates_total`** |
| epoch 均损 | `epoch 2/4 mean loss 0.4321` | `type=epoch_mean`、`epoch_mean_loss=0.4321` |
| checkpoint | `checkpoint_latest: <out>\checkpoint_latest` | `type=checkpoint`、`checkpoint_latest=true`、`path` |
| 评测 | `=== Evaluation (before vs after fine-tuning on 500 held-out items from ...) ===` | `type=evaluation`、`mode=held_out/overlapping_eval/in_sample_calibration` |
| 校准告警 | `laya.train: choice calibration: not fitted: 2 calibration items, fewer than 10, so the temperature stays 1.0` | `type=calibration_warning`、`qtype=choice`、`message` |
| 数据集 | `train items 5600, calibration items 560, eval items 500 ...` | `type=dataset`、三个计数 |

- `updates = epochs × ceil(ceil(items / micro_batch) / grad_accum)`（`train.py:607-619`）；5600/4/8×8 = **352**。
- `loss` 的 `step` 是 **micro-batch 步**，`budget` 的 updates 是 **optimizer 步**；两者默认差 8 倍。运行页/预估不得混用。
- `micro_step` 是**当前 epoch 内**的 micro-batch 步（上游 `n_steps` 每 epoch 重置，`train.py:692,714`），不是全局步；`updates_total` 才是全程 optimizer 更新总数。
- **诚实备注**：上游当前只在 `train.py:1125-1130` **保存** `checkpoint_latest/`，没有打印一行 `checkpoint_latest`。适配器仍识别任何含该字符串的行（便于未来/包装日志）；桩为了格式覆盖会打印它，不代表上游现在一定打印。

### 12.4 `run(job, log_path, ...)`

```python
run(job, log_path, *, train_command=None, stop_file=None, metrics_path=None,
    cancel_event=None, poll_interval=0.1, grace_s=5.0, cwd=None, env=None) -> dict
```

- `job` 可以是 `{"id": ..., "params": {...}}`、core 行里的 `{"id": ..., "params_json": "..."}`，或直接是 params dict；`run` 三种都接受。
- 启动子进程（`shell=False`），stdout/stderr 合并逐行读、写 UTF-8 日志、`parse_line` 后立即更新 metrics。`metrics_path` 省略时默认写 `<log_path>.metrics.json`（例如 `job.log` → `job.metrics.json`）。
- `metrics_json` 同时保留 `updates_total`（预算）与 `micro_step`（loss），另有 `epoch`、`loss`、`epoch_mean_loss`、`checkpoint_latest`、`evaluation`、`warnings`、`events`、`elapsed_s`、`returncode`、`state`。
- 软停：`stop_file` 出现或 `cancel_event` 置位 → 发 `CTRL_BREAK_EVENT`（Windows）/`SIGINT`（POSIX）；`grace_s` 内不退出则 terminate/kill。返回 `state=cancelled`、`cancelled=true`，`returncode` 保留真实值（桩为 130）。
- **无 resume**：取消后只能从头再训；`checkpoint_latest/` 只是“上一轮可拿回”的止损点（`TRAINING-FLOWS.md` §1.6）。
- `stop_file` 若在启动前已存在 → 直接 `cancelled`，不 spawn。适配器不删 stop 文件（由调用方/核心管理）。

### 12.5 无 GPU 自测

```powershell
python E:\视频\modelsmith\adapters\selftest_laya_adapter.py
```

- 用 `_tmp\laya_stub.py` 假 CLI；不 import torch、不加载模型、不占 GPU。
- 覆盖：16 个参数一一映射、源文件 grep 静态检查、**直接调用上游 `laya.train_cli.build_parser()` 解析生成 argv**、`5600/4/8×8=352`、budget 与 loss 两个分母不串、checkpoint/eval/warning/dataset 解析、完整生命周期、日志落盘、stop 文件取消并返回 `cancelled`。
- 本机基线：`PASS 29 / FAIL 0`（系统 Python 3.12.8，2026-10-22）。

### 12.6 本机环境探测（只读，A 级）

| 解释器 | 读数 | 结论 |
|---|---|---|
| `C:\Program Files\Python312\python.exe` 3.12.8 | `torch` 无、`transformers` 无；`import laya` 可（纯 Python 部分），`import laya.train` → `ModuleNotFoundError: torch` | **不能训练**；本次不推荐为它装 GPU 栈 |
| `E:\ComfyUI\python_embeded\python.exe` 3.12.12 | torch **2.12.0+cu130**、transformers **4.57.6**、safetensors 0.8.0、huggingface_hub 0.36.2、numpy 2.5.4、pip 25.3、setuptools 84.0.0、wheel 0.45.1 | 依赖齐；`sys.path.insert` 后可 `import laya.train`，上游 `optimizer_updates(5600,8,8,4)=352` |

- ComfyUI embedded Python 是 **isolated=1**（`python312._pth`），**`PYTHONPATH` 不生效**；无安装 runner 必须用 `-c` 里 `sys.path.insert(...)`，不能靠环境变量。
- `laya-train` 当前不在 PATH、ComfyUI `Scripts\` 下也没有；`laya` 未安装。

### 12.7 安装方案（本次**不执行**）

**首选（零安装）**：用 ComfyUI Python + 注入 runner，见 §12.8；优点是不改任何环境、不写 `third_party`。

`E:\ComfyUI\python_embeded\python.exe -m pip install -e E:\视频\modelsmith\third_party\laya --no-deps` **技术上大概率可行，但本次判定不建议/未批准**：

- 前提基本满足：pip/setuptools/wheel 在，torch/transformers/safetensors/hf_hub/numpy 已齐；`--no-deps` 不会重装 torch。setuptools 84 的 `editable_wheel` 源码不依赖外部 `editables`（未装也能 import；只出现 `EditableStrategy` 协议名），所以这一点不是阻塞；离线时可加 `--no-build-isolation`。
- 两个真实阻塞：① editable 构建通常会在 **`third_party\laya` 写 `laya.egg-info` / build 产物**，违反“third_party 只读”；② 会改共享 ComfyUI 环境并新增 `Scripts\laya-train.exe`，需要 维护者 批准。是否写 `third_party` 本次**未实测**（不许装），但按 pip/setuptools 常规路径这是高风险默认行为。
- 若以后要 console script，正确做法是先把 Laya 源码复制到可写目录 / 专用 venv，再 `pip install -e` 那份副本；**不要**直接从 `third_party` 装。
- 系统 Python 缺 torch/transformers，不要往它装。

### 12.8 真跑命令形态（维护者 报数后再起）

零安装 runner（已用 `--help` 验证可启动，未训练）：

```powershell
$py = 'E:\ComfyUI\python_embeded\python.exe'
$laya = 'E:\视频\modelsmith\third_party\laya'
$runner = "import sys; sys.path.insert(0, r'$laya'); from laya.train_cli import main; sys.exit(main())"
& $py -c $runner --data 'E:\data\train.jsonl' --out 'E:\runs\laya-001' `
  --base convaiinnovations/laya --loss rlcd --epochs 4 --micro-batch 8 --grad-accum 8 `
  --encoder-lr 2.5e-5 --head-lr 1e-4 --max-len 512 --head-max-len 192
# 可选：--shuffle-options --eval-data 'E:\data\eval.jsonl'
```

适配器调用形态：

```python
from adapters import laya_adapter
result = laya_adapter.run(
    {"id": "laya-001", "params": params},
    r"E:\runs\laya-001.log",
    train_command=[r"E:\ComfyUI\python_embeded\python.exe", "-c", runner],
    metrics_path=r"E:\runs\laya-001.metrics.json",
    stop_file=r"E:\runs\laya-001.stop",
)
```

- 真跑 20 步时只需在 params 里把 `epochs` 调小/加停点（本次不跑），不要改上游。
- 预计耗时/显存**读取方式**（不跑）：适配器 metrics 的 `elapsed_s` 给实际时长；显存用 `nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits -l 1` 并行采样取峰值。
- 量级参考（A 级，`E:\视频\_kb_research\htlb\train\README-训练台.md:123`）：1310 micro-step = 7.7 min、峰值 16.3 GiB、0.34 s/step。按 5600×4/8×8 = 2800 micro-step 外推 ≈ **15.9 min / ~16.3 GiB**；这是**估算，未验**，真跑必须报实测。

---

## 13. 在 UI / API 里提交 Laya 任务（`kind="laya"`）

### 13.1 启动环境

```powershell
python E:\视频\modelsmith\core\modelsmith_core.py
# 或显式指定：
python E:\视频\modelsmith\core\modelsmith_core.py `
  --laya-python 'E:\ComfyUI\python_embeded\python.exe' `
  --laya-src 'E:\视频\modelsmith\third_party\laya'
```

- `--laya-python` 默认 `E:\ComfyUI\python_embeded\python.exe`；`--laya-src` 默认 `E:\视频\modelsmith\third_party\laya`。
- 子进程命令形态是 `[laya-python, "-c", runner]`，runner 内部 `sys.path.insert(0, laya-src)` 再 `laya.train_cli.main()`。**ComfyUI embedded Python 是 isolated，`PYTHONPATH` 会被忽略**，所以不能靠环境变量。
- `core.json` 与 `/env/doctor` 都会回报 `laya.python / laya.src / laya.adapter`；缺文件会在 job preflight 阶段失败并指出路径。
- **不需要安装 Laya**；本次也没有装。系统 Python 无 torch，不能用它跑 Laya；ComfyUI Python 有 torch 2.12.0+cu130 等依赖（§12.6）。

### 13.2 请求

```powershell
$token = [IO.File]::ReadAllText("$env:LOCALAPPDATA\ModelSmith\token", [Text.Encoding]::UTF8).Trim()
$body = @{
  kind = "laya"
  params = @{
    data = "E:\data\train.jsonl"
    out  = "E:\runs\laya-001"
    base = "convaiinnovations/laya"
    loss = "rlcd"
    epochs = 4
    micro_batch = 8
    grad_accum = 8
    encoder_lr = 2.5e-5
    head_lr = 1e-4
    max_len = 512
    head_max_len = 192
    shuffle_options = $true
    label_smoothing = 0.0
    target_error = 0.10
    min_abstain_n = 10
    eval_data = "E:\data\eval.jsonl"
  }
} | ConvertTo-Json -Depth 6
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8765/api/jobs' `
  -Headers @{ "X-ModelSmith-Token" = $token } `
  -ContentType 'application/json; charset=utf-8' `
  -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

`data` / `out` 必填；缺失时 job 进入 `failed`，`error` 形如 `missing required param(s): data, out`（点名缺哪个）。其余参数不传就交给上游默认。

### 13.3 params 字段表

| 字段 | 类型 | 必填 | 上游默认 | 说明 |
|---|---|---|---|---|
| `data` | str | **是** | — | 训练数据 `.jsonl` / `.csv` |
| `out` | str | **是** | — | 输出目录（`checkpoint_latest/`、`train_report.json` 等） |
| `base` | str | 否 | `convaiinnovations/laya` | base checkpoint 路径 / alias / Hub id |
| `loss` | str | 否 | `rlcd` | `rlcd` 或 `soft-ce` |
| `epochs` | int | 否 | `4` | 正整数 |
| `micro_batch` | int | 否 | `8` | 每个 forward 的 micro-batch |
| `grad_accum` | int | 否 | `8` | 梯度累积窗口 |
| `encoder_lr` | float | 否 | `2.5e-5` | encoder 学习率 |
| `head_lr` | float | 否 | `1e-4` | head 学习率 |
| `max_len` | int/null | 否 | 取 base ckpt config | state token 上限 |
| `head_max_len` | int/null | 否 | 取 base ckpt config | 题目 head token 上限 |
| `shuffle_options` | bool | 否 | `false` | 选项顺序增广 |
| `label_smoothing` | float | 否 | `0.0` | `[0,1)` |
| `target_error` | float | 否 | `0.10` | 拒答阈值目标错误率 |
| `min_abstain_n` | int | 否 | `10` | 拟合拒答阈值的最小桶大小 |
| `eval_data` | str/null | 否 | 无 | before/after 评测集 |
| `items` / `calib_frac` / `calib_max` | — | 否 | — | **UI/Python-only，适配器忽略，不变成 CLI 参数** |
| `train_command` | str[] | 否 | 核心 runner | **仅测试钩子**：覆盖子进程命令（自测用 `python + laya_stub.py`）；UI 不应发送 |

### 13.4 状态与 metrics

- 状态：`queued → starting → running → succeeded | failed | cancelled | interrupted`（核心调度器保证同一时刻最多 1 个 running，GPU 锁仍生效）。
- `metrics_json` 由适配器事件驱动更新：
  - `updates_total` = 预算行（5600/4/8×8 = **352**），**不是** loss 的 step；
  - `micro_step` = loss 行（当前 epoch 内，每 epoch 重置；不和 updates 混用）；
  - `epoch`、`loss`、`epoch_mean_loss`、`checkpoint_latest`、`evaluation`、`warnings`、`events`、`elapsed_s`、`returncode`、`phase`；
  - `phase` 取值 `starting / training / checkpoint / evaluation / finished`。
- 取消：`POST /api/jobs/{id}/cancel` 把 `cancel_requested=1`；核心 monitor 写 `jobs\<id>\stop`，适配器发 `CTRL_BREAK_EVENT` / `SIGINT`，终态 `cancelled`。**没有 resume**：停下只能从头再训，`checkpoint_latest/` 只保上一轮。

### 13.5 未接真训练前的限制

- 本次只验证了 **桩 CLI**：`params.train_command` 指向 `adapters\_tmp\laya_stub.py`；真实模型、Hub 下载、显存实测都未跑。
- 没有显存准入；GPU 锁只保证串行，不保证 32GB 装得下。真跑前要按 base/数据量核算。
- `--base` 未命中本地缓存时，上游可能访问 Hub（本项目未做下载管理）；首次真跑前要确认模型已缓存或允许网络。
- `train_command` 是本地测试钩子，会执行任意命令；服务只绑 loopback + token，但不能暴露到不可信网络。
- 目前 fake/laya/llm/aitk/diffsynth/lf 都有核心路由（`kind="lf"` = LlamaFactory，见 §17）；`image / video` 通过 `aitk` 统一入口，ai-toolkit 环境已跑通 1 步 smoke，但核心尚未真拉起。
- UI 已接 fetch（`kind` 见 §13–§17），`file://` 下仍走模拟；§13.2 的 curl/PowerShell 形态保留为 API 示例。

---

## 14. LLM 任务（`kind="llm"`）

### 14.1 启动环境

```powershell
python E:\视频\modelsmith\core\modelsmith_core.py `
  --llm-python 'E:\ComfyUI\python_embeded\python.exe'
```

- `--llm-python` 默认 `E:\ComfyUI\python_embeded\python.exe`；核心固定调用
  `E:\视频\modelsmith\adapters\llm_lora.py --mode train ...`。
- 依赖已在本机 ComfyUI Python 里（torch/transformers/peft/accelerate/safetensors）；本次**未装包、未下载、未跑 GPU**。
- 默认底模：`E:\视频\modelsmith\resources\03-LLM-小模型\Qwen2.5-0.5B-Instruct`；真实模型加载已在适配器层单独做过 20 步 GPU smoke（7 条验收，见 维护者 回执），核心本次只用桩验证。

### 14.2 params 字段（严格按这套字段名，不要自创别名）

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `model` | str | **是** | — | base 模型目录（Qwen2.5-0.5B 路径） |
| `data` | str | **是** | — | dolly/alpaca 形状 jsonl（文件或目录） |
| `out` | str | **是** | — | adapter / report 输出目录 |
| `max_steps` | int | 否 | `0`（完整 epochs） | 映射 `--max-steps` |
| `epochs` | int | 否 | `1` | `--epochs` |
| `rank` | int | 否 | `8` | `--rank` |
| `alpha` | int | 否 | `16` | `--alpha` |
| `lr` | float | 否 | `5e-4` | `--lr` |
| `max_len` | int | 否 | `512` | `--max-len`（>=8） |
| `batch` | int | 否 | `1` | `--batch` |
| `device` | str | 否 | `auto` | `--device`（auto/cpu/cuda/cuda:0） |
| `template` | str | 否 | `qwen` | `--template`（qwen/plain） |
| `assert_updated` | bool | 否 | `false` | true → `--assert-updated`（无参数更新则 exit 5） |
| `train_command` | str[] | 否 | 核心命令 | **仅测试钩子**：覆盖 `[python, llm_lora.py]` 前缀；自测用 `python adapters\_tmp\llm_stub.py` |

请求示例：

```powershell
$body = @{
  kind = "llm"
  params = @{
    model = "E:\视频\modelsmith\resources\03-LLM-小模型\Qwen2.5-0.5B-Instruct"
    data  = "E:\视频\modelsmith\resources\02-LLM-语料\dolly-15k\dolly-15k.jsonl"
    out   = "E:\视频\modelsmith\_runs\llm-001"
    max_steps = 20
    rank = 8
    alpha = 16
    lr = 5e-4
    batch = 1
    device = "cuda"
    template = "qwen"
    assert_updated = $true
  }
} | ConvertTo-Json -Depth 6
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8765/api/jobs' `
  -Headers @{ "X-ModelSmith-Token" = $token } `
  -ContentType 'application/json; charset=utf-8' `
  -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

- 缺 `model` / `data` / `out` → preflight `failed`，`error` 形如 `missing required param(s): model`（点名缺哪个）。
- 可选参数不传就不出现在命令行，由 `llm_lora.py` 自己取默认；核心不另造默认。
- `template` 只接受 `qwen` / `plain`；`assert_updated` 必须 bool；`max_len>=8`，`rank/alpha/batch/epochs>=1`。

### 14.3 事件 / metrics

- 逐行解析 stdout `step N loss X lr Y` → `step/loss/lr`，同时写日志与 DB `metrics_json`。
- 进程退出后读 `<out>\train_report.json`，把以下字段并入 job metrics：
  - `loss_series`（`report.loss.series`，逐点 step/loss/lr）
  - `train_elapsed_s`、`total_elapsed_s`、`steps_done`、`planned_steps`
  - `peak_vram_alloc_mib`（`report.memory.peak_vram_alloc_mib`）
  - `report_status`、`report_loaded`、`report_path`
- `report.status != "ok"` 或出现 `error:` / `SystemExit:` 行 → `failed`；取消 → `cancelled`。
- 取消：核心写 `jobs\<id>\stop` 并发 `CTRL_BREAK_EVENT`（POSIX `SIGINT`），5 秒未退再 `taskkill /T /F`；终态 `cancelled`。**没有 resume**。

### 14.4 限制

- 本次核心 `kind="llm"` 只验证 **桩 CLI**（`adapters\_tmp\llm_stub.py` 打印 20 行 + 最小 report）；核心层没有重跑真 GPU 训练。
- 没有显存准入；GPU 锁只保证串行。真跑前确认 Qwen 路径、数据 jsonl、显存。
- `train_command` 是测试钩子，可执行任意命令；服务只绑 loopback + token，不能暴露到不可信网络。
- `image / video` 走 §15 的 `aitk` 路由；ai-toolkit 环境已用 ComfyUI Python + `.aitk-pylibs` 跑通 1 步 smoke，核心尚未真拉起。UI 已接 fetch（`kind` 见 §13–§17），`file://` 下仍走模拟；§14.2 的 API 形态保留为接口示例。

---

## 15. 生图 / 视频任务（`kind="aitk"`）

### 15.1 启动环境

```powershell
python E:\视频\modelsmith\core\modelsmith_core.py `
  --aitk-python 'E:\ComfyUI\python_embeded\python.exe' `
  --aitk-libs 'E:\视频\modelsmith\.aitk-pylibs' `
  --aitk-src 'E:\视频\modelsmith\third_party\ai-toolkit'
```

- `--aitk-python` 默认 `E:\ComfyUI\python_embeded\python.exe`；`--aitk-libs` 默认 `E:\视频\modelsmith\.aitk-pylibs`；`--aitk-src` 默认 `third_party\ai-toolkit`。
- 真实命令形状：`[aitk-python, "-c", runner, <job>\aitk.yaml]`；runner 依次
  `sys.path.insert(0, .aitk-pylibs)` → `sys.path.insert(0, ai-toolkit)` →
  `sys.argv = ["run.py", cfg]` → `runpy.run_path("<ai-toolkit>\run.py", run_name="__main__")`；cwd 设为 ai-toolkit 目录。
- 环境要求：ComfyUI Python（torch/transformers 等已有）+ `.aitk-pylibs`（含 pinned diffusers 等 ai-toolkit 依赖）+ `third_party\ai-toolkit\run.py`。
  实际环境已跑通 SD1.5 1 步 smoke（`E:\视频\modelsmith\_runs\aitk-smoke-sd15-v2\`，exit 0），但核心本次只用桩验证。
- 预检：`aitk-python` / `.aitk-pylibs` / `run.py` 任一缺失 → `failed`，`error` 明写
  `ai-toolkit environment missing (未安装或不完整): <具体缺失路径>`。
  核心只按给定路径检查，**不做隐式探测/回退**。
- 自测用 `params.train_command` 覆盖为 `python adapters\_tmp\aitk_stub.py`，不 import torch、不占 GPU。

### 15.2 params 字段（严格按这套字段名，不要自创别名）

| 字段 | 类型 | 必填 | 默认/映射 |
|---|---|---|---|
| `model` | str | **是** | 底模路径 / Hub id → `model.name_or_path` |
| `dataset` | str | **是** | 图片或视频文件夹 → `datasets[0].folder_path` |
| `out` | str | **是** | 输出根目录 → `process[0].training_folder` |
| `arch` | str | **是** | 适配器 arch 字符串（如 `flux` / `wan22_14b`）→ `model.arch` |
| `steps` | int | 否 | `1000` → `train.steps` |
| `batch` | int | 否 | `1` → `train.batch_size` |
| `lr` | float | 否 | 上游默认（不传则不写）→ `train.lr` |
| `rank` | int | 否 | 上游默认 → `network.linear` |
| `alpha` | float | 否 | 上游默认 → `network.linear_alpha` |
| `resolution` | int/list | 否 | 上游默认 → `datasets[0].resolution` |
| `grad_accum` | int | 否 | `1` → `train.gradient_accumulation` |
| `save_every` | int | 否 | `1000` → `save.save_every` |
| `keep` | int | 否 | `5` → `save.max_step_saves_to_keep` |
| `sample_every` | int | 否 | `100` → `sample.sample_every` |
| `sample_start` | int | 否 | `0` → `sample.sample_start_step` |
| `sample_steps` | int | 否 | `20` → `sample.sample_steps` |
| `guidance` | float | 否 | `7` → `sample.guidance_scale` |
| `sample_width` | int | 否 | `512` → `sample.width` |
| `sample_height` | int | 否 | `512` → `sample.height` |
| `num_frames` | int | 否 | `1` → `sample.num_frames` / `datasets[0].num_frames` |
| `trigger_word` | str/null | 否 | `None` → dataset + process trigger |
| `caption_ext` | str | 否 | `.txt` → `datasets[0].caption_ext` |
| `sample_prompts` | str[] | 否 | `[]` → `sample.prompts`；空会警告 |
| `sampler` | str | 否 | `ddpm`；必须与 `noise_scheduler` 一致 |
| `noise_scheduler` | str | 否 | `ddpm`；必须与 `sampler` 一致 |
| `save_format` | str | 否 | `safetensors`；枚举 `safetensors/diffusers` |
| `train_command` | str[] | 否 | **仅测试钩子**：覆盖 `[aitk-python, -c, runner]` 前缀；自测用 `python adapters\_tmp\aitk_stub.py` |

请求示例：

```powershell
$body = @{
  kind = "aitk"
  params = @{
    model = "black-forest-labs/FLUX.1-dev"
    dataset = "E:\data\images"
    out = "E:\runs\aitk-001"
    arch = "flux"
    steps = 1200
    batch = 1
    sample_every = 100
    sample_prompts = @("a photo of sks person", "a portrait")
    sampler = "flowmatch"
    noise_scheduler = "flowmatch"
  }
} | ConvertTo-Json -Depth 6
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8765/api/jobs' `
  -Headers @{ "X-ModelSmith-Token" = $token } `
  -ContentType 'application/json; charset=utf-8' `
  -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

- 缺 `model` / `dataset` / `out` / `arch` → preflight `failed`，`error` 形如 `missing required param(s): model`（点名缺哪个）。
- 可选字段不传就不写进 YAML，由适配器/上游取默认；核心不另造默认。
- 校验由适配器做：`gradient_accumulation` 与 `_steps` 互斥（核心只发 `gradient_accumulation`）、`sampler` 与 `noise_scheduler` 必须一致、`save_format` 枚举、`sample_prompts` 空警告。

> 参数可达性：core 只转发 §15.2 字段；适配器另有 5 个参数（`cache_text_embeddings` / `cache_latents_to_disk` / `skip_first_sample` / `disable_sampling` / `device`）经 core 不可达。完整参数/flag 见 `adapters\README.md` 的 ai-toolkit 段与 `adapters\aitk_adapter.py`（不复制正文）。

### 15.3 事件 / metrics

- 适配器逐行解析 ai-toolkit 输出：`Generating baseline samples`、`Sampling at step N`（也兼容 `Sample at step N`）、`Saving at step N`、`lr/loss` postfix；核心把 adapter metrics 写入 job `metrics_json`。
- 终态 metrics 至少含：`step`、`total`、`sample_groups`、`last_sample_step`、`warnings`、`phase`、`expected_sample_groups`、`plan`。
- `steps=1200, sample_every=100` → 首图 1 + 12 = **13 组**；`steps=1250` → 13 组但 `last_sample_step=1200`，`warnings` 含 **「末步不整除」**；`sample_prompts=[]` → `warnings` 含空 prompts 警告。
- 取消：核心写 `jobs\<id>\stop` 并发 `CTRL_BREAK_EVENT`（POSIX `SIGINT`），5 秒未退再 `taskkill /T /F`；终态 `cancelled`。没有 resume。

### 15.4 限制

- **环境已具备但核心未真拉起**：ai-toolkit 已在 ComfyUI Python + `.aitk-pylibs` 下跑通 SD1.5 1 步 smoke（`_runs\aitk-smoke-sd15-v2\`）；核心 `kind="aitk"` 本次只用桩验证，真实生图/视频训练未由核心跑。
- 没有显存准入；真实 arch/底模兼容性、视频 fps/format、采样图产物都没有核心层验证。
- `.aitk-pylibs` 含 pinned diffusers；升级/换依赖不在本任务范围。核心不做隐式 fallback（缺 python/libs/run.py 一律 preflight failed 指名）。
- `train_command` 是测试钩子，可执行任意命令；服务只绑 loopback + token，不能暴露到不可信网络。
- `image / video` 共用 `aitk` 路由；UI 已接 fetch（`kind` 见 §13–§17；aitk 发布态仍为 API），`file://` 下仍走模拟；§15.2 的 API 形态保留为接口示例。

---

## 16. Qwen-Image 2.1 LoRA（`kind="diffsynth"`）

### 16.1 启动环境

```powershell
python E:\视频\modelsmith\core\modelsmith_core.py `
  --diffsynth-python 'E:\ComfyUI\python_embeded\python.exe' `
  --diffsynth-src 'E:\视频\modelsmith\third_party\DiffSynth-Studio'
```

- `--diffsynth-python` 默认 `E:\ComfyUI\python_embeded\python.exe`；`--diffsynth-src` 默认 `third_party\DiffSynth-Studio`；`--diffsynth-libs` 默认空，空时自动找 `<project-root>\runtime\pylibs-5` → `.aitk-pylibs` 作为默认 `params.python_paths`（显式 params 优先）。`arch=moss_image` 且两者都没有 → job metrics.warnings 记 `moss_needs_transformers_5`。
- 环境 = **ComfyUI Python + DiffSynth repo**，零新装；预检要求 python / repo / `examples\qwen_image_21\model_training\train.py` / `adapters\diffsynth_adapter.py` 都存在，缺任一 → `failed` 并点名具体路径。
- 命令形状：核心不再自拼 launcher；直接调用 `adapters.diffsynth_adapter.run()`，由适配器构造带 `ModelLogger.on_step_end` 采样钩子、`cfg_path` 与 device 快照的 RUNNER；采样参数写 `.ms_sample.json`。
- 已验证基线（适配器层，A 级）：Qwen-Image 2.1 本地 diffusers，1 epoch × 4 图 512px、rank 8 → **4/4 步、3.88 s/it、epoch-0.safetensors 40.1 MB**（`_tmp\run_diffsynth_smoke.py`）。核心 `kind="diffsynth"` 本次只用桩验证，未真拉起。

### 16.2 params 字段（严格按这套字段名，不要自创别名）

| 字段 | 类型 | 必填 | 默认/说明 |
|---|---|---|---|
| `model` | str | **是** | 本地 diffusers 底模目录（分片结构） |
| `dataset` | str | **是** | 图片文件夹 |
| `out` | str | **是** | 输出目录（`epoch-*.safetensors` 落这里） |
| `metadata` | str | 否 | 默认 `<dataset>\metadata.csv`（列 `image,prompt`） |
| `processor` | str | 否 | 默认 `<model>\processor` |
| `rank` | int | 否 | `8` → `--lora_rank` |
| `epochs` | int | 否 | `1` → `--num_epochs` |
| `lr` | float | 否 | `1e-4` → `--learning_rate` |
| `height` | int | 否 | `512` |
| `width` | int | 否 | `512` |
| `dataset_repeat` | int | 否 | `1` |
| `dataset_image_exts` | str/list | 否 | 空=适配器默认 jpg/jpeg/png；可传 `"png,jpg"` 或列表，用于扫描数据集图片 |
| `dataset_default_caption` | str | 否 | 空=适配器用 `a photo of <stem>`；支持 `{image}` 占位，建议写触发词 |
| `train_command` | str[] | 否 | **仅测试钩子**：覆盖 `[diffsynth-python, -c, runner, train.py]` 前缀；自测用 `python adapters\_tmp\diffsynth_stub.py` |

> 适配器还接受 `save_steps / gradient_accumulation_steps / max_pixels / data_file_keys` 等可选 argv，但本节只列核心路由合同字段。
> 参数可达性：core 只校验必填 3 项并透传 §16.2 字段；适配器另有 9 个 flag（`model_paths` / `lora_base_model` / `lora_target_modules` / `task` / `remove_prefix_in_ckpt` / `use_gradient_checkpointing` / `initialize_model_on_cpu` / `enable_model_cpu_offload` / `resolution`）未入本文。完整 flag 见 `adapters\README.md` 的 DiffSynth 段与 `adapters\diffsynth_adapter.py::build_argv()`（不复制正文）。

请求示例：

```powershell
$body = @{
  kind = "diffsynth"
  params = @{
    model = "E:\临时文件夹\Qwen-Image-2.1"
    dataset = "E:\data\qwen-images"
    out = "E:\runs\qwen21-001"
    rank = 8
    epochs = 1
    lr = 0.0001
    height = 512
    width = 512
  }
} | ConvertTo-Json -Depth 6
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8765/api/jobs' `
  -Headers @{ "X-ModelSmith-Token" = $token } `
  -ContentType 'application/json; charset=utf-8' `
  -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

### 16.3 事件 / metrics

- 适配器解析 tqdm：`N/4 [..]` → `step/total`；`3.88s/it` → `sec_per_it`；`Saving`/`epoch-*.safetensors` + 输出目录扫描 → `artifact`。
- 终态 metrics 至少含：`step`、`total`、`sec_per_it`、`artifact`、`artifacts`、`phase`、`output_dir`。
- 取消：核心写 `jobs\<id>\stop` 并发 `CTRL_BREAK_EVENT`（POSIX `SIGINT`），5 秒未退再 `taskkill /T /F`；终态 `cancelled`。DiffSynth 无 resume。
- 缺 `model` / `dataset` / `out` → preflight `failed`，error 形如 `missing required param(s): model`（点名缺哪个）。

### 16.4 限制

- 核心 `kind="diffsynth"` 本次只验证 **桩 CLI**（`adapters\_tmp\diffsynth_stub.py` 打 4 行 tqdm + 写 `epoch-0.safetensors` 占位）；核心未真拉起 Qwen 训练。
- 没有显存准入；Qwen-Image 2.1 真训练显存/耗时未由核心层验证。
- 分片展开、processor/metadata 默认、空 target 自动探测都依赖 `adapters\diffsynth_adapter.py`；核心只透传参数字段。
- `train_command` 是测试钩子，可执行任意命令；服务只绑 loopback + token。
- UI 已接 fetch（`kind` 见 §13–§17），`file://` 下仍走模拟；§16.2 的 API 形态保留为接口示例。

---

## 17. LLM 微调 · LlamaFactory 主线（`kind="lf"`）

### 17.1 启动环境

```powershell
python E:\视频\modelsmith\core\modelsmith_core.py `
  --lf-python 'E:\ComfyUI\python_embeded\python.exe' `
  --lf-libs   'E:\视频\modelsmith\.pylibs-lf' `
  --lf-src    'E:\视频\modelsmith\third_party\LlamaFactory'
```

- 默认值：`--lf-python` = `E:\ComfyUI\python_embeded\python.exe`；`--lf-libs` = `.pylibs-lf`（相对项目根）；`--lf-src` = `third_party\LlamaFactory`。
- 环境 = **ComfyUI Python + 隔离依赖目录 `.pylibs-lf`（10.4 MB：datasets 4.0.0 / trl 0.24.0 / multiprocess 0.70.16 / dill 0.3.8 / fsspec 2025.3.0）+ LlamaFactory v0.9.5 克隆**；依赖没有装进 ComfyUI 的 site-packages（torch 2.12.0+cu130 仍来自 ComfyUI，`shadowed_from_libs=[]`）。
- 预检（`core.lf_preflight()`，缺任一 → `failed` 并点名）：`lf-python` 是文件、`lf-libs` 是目录、`<lf-src>\src\llamafactory\cli.py` 是文件、`adapters\lf_adapter.py` 是文件。
- 命令形状（3 段）：`[lf-python, "-c", runner]`，YAML 路径烘进 runner：
  `sys.path.insert(0, <lf-libs>)` → `sys.path.insert(0, <lf-src>)` → **`sys.path.insert(0, <lf-src>\src)`（`src` 最终位于第一位）** → `import dill` → `sys.argv = ['llamafactory-cli', 'train', <job>\lf.yaml]` → `from llamafactory.cli import main` → `sys.exit(main())`。
  `--lf-src` 是仓库根（预检查 `<lf-src>\src\llamafactory\cli.py`）；**`\src` 缺了就会 `ModuleNotFoundError: No module named 'llamafactory'`**（task-39 的 P0 根因）。
  **顺序不是随意的**：torch 会先 import dill（ComfyUI 是 0.4.0）并写进 `sys.modules`，而 datasets 4.0 硬约束 `dill<0.3.9` ⇒ 必须先 seed libs 里的 dill 0.3.8。
- 子进程环境：`PYTHONUTF8=1`（模型卡按 cp936 写/UTF-8 读的坑）、`RECORD_VRAM=1`（`trainer_log.jsonl` 记显存）、`PYTHONDONTWRITEBYTECODE=1`、`MODELSMITH_LF_OUT` / `MODELSMITH_LF_JOB_DIR` / `MODELSMITH_LF_STOP_FILE`；cwd = 项目根。
- 数据：`data` 是 dolly 形状 JSONL，核心用 `adapters\lf_adapter.py::prepare_alpaca_data` 转成 alpaca 写到 `<data_dir>\jobs\<id>\lf_data\`，并生成 `dataset_info.json` + `lf.yaml`。
- 已验证（A 级）：`kind="lf"` **3 次连续 `PASS 131 / FAIL 0`**（含真 import 探针 `<lf-src>\src` → `llamafactory 0.9.5`、去 `\src` 负控、LF bar 分类正/负控）；**核心已用当前代码真跑 2 步**：`step/total=2/2`、`loss_series` step `[1,2]`、loss 3.8127→2.6485、`train_runtime 0.8788 s`、峰值 1.22 GiB、`adapter_model.safetensors` 在场。适配器层早期 2 步读数见 `resources\02-LLM-语料\README-LLM跑通.md` §10.7。

### 17.2 params 字段（严格按这套字段名，不要自创别名）

| 字段 | 类型 | 必填 | 默认 / 说明 |
|---|---|---|---|
| `model` | str | **是** | 本地 HF 模型目录 |
| `data` | str | **是** | JSONL 绝对路径（dolly 形状 `instruction/context/response`） |
| `out` | str | **是** | 输出目录（`adapter_model.safetensors` / `adapter_config.json` / `trainer_log.jsonl` 落这里） |
| `template` | str | **是** | LlamaFactory 模板名（Qwen2.5 用 `qwen`；Qwen3 系可用 `qwen3` 等） |
| `rank` | int | 否 | `8` → `lora_rank` |
| `alpha` | int | 否 | `16` → `lora_alpha` |
| `lr` | float | 否 | `2e-4` → `learning_rate` |
| `epochs` | float | 否 | `3` → `num_train_epochs` |
| `max_steps` | int | 否 | `0` = 按 epochs（内部转成 LF 的 `-1`）；`>0` 覆盖 |
| `batch` | int | 否 | `1` → `per_device_train_batch_size` |
| `grad_accum` | int | 否 | `1` → `gradient_accumulation_steps` |
| `cutoff_len` | int | 否 | `512` |
| `logging_steps` | int | 否 | `1` |
| `save_steps` | int | 否 | `0` = 不设（用 LF 默认 500）；`>0` 透传 |
| `train_command` | str[] | 否 | **仅测试钩子**：覆盖 `[lf-python, -c, runner]`；自测用 `python adapters\_tmp\lf_stub.py` |

请求示例：

```powershell
$body = @{
  kind = "lf"
  params = @{
    model    = "E:\视频\modelsmith\resources\03-LLM-小模型\Qwen2.5-0.5B-Instruct"
    data     = "E:\视频\modelsmith\resources\02-LLM-语料\dolly-15k\databricks-dolly-15k.jsonl"
    out      = "E:\runs\llm-smoke-001"
    template = "qwen"
    rank = 8; alpha = 16; lr = 0.0002
    epochs = 3; max_steps = 2; batch = 1; grad_accum = 1
    cutoff_len = 512; logging_steps = 1; save_steps = 0
  }
} | ConvertTo-Json -Depth 6
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8765/api/jobs' `
  -Headers @{ "X-ModelSmith-Token" = $token } `
  -ContentType 'application/json; charset=utf-8' `
  -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

### 17.3 事件 / metrics

- 解析器复用 `adapters\lf_adapter.py::parse_line`：`{'loss': X, 'learning_rate': Y, 'epoch': Z}` → `loss/lr/epoch`；tqdm `N/M` → `step/total`；`***** Running training *****` / `Num examples` / `Total optimization steps` → `total`；`Saving model checkpoint to` → `checkpoints`；`{'train_runtime': ...}` → `train_runtime/train_loss`。
- **bar 分类（task-42 事故修复）**：只有含 `it/s` 或 `s/it` 的 bar 才算训练条；含 `ex/s` / `examples/s` / `samples/s` 的是数据预处理/缓存条，**不写 step/total**。候选训练条取最后出现且 `total <= 期望步数*1.5` 的一条（期望 = `max_steps>0 ? max_steps : 基于 epochs/num_examples/batch 估算`）。`metrics.bars` 仍保留所有条（如 `{"15011":15011,"2":2}`）作诊断。
- **loss↔step**：loss 行不带 step，按出现次数映射为 `step=1..N`，写 `loss_series=[{"step":k,"loss":X,"lr":Y}]`；不得再用 bar 的 total 当 step。
- 事故：曾把 dolly 的预处理进度条 `15011/15011 [.. 5000.00ex/s]` 当训练条，UI 显示「step 15,011 / 2」、两条 loss 都记在 step 15011；已由分类规则 + loss 计数修复，并加了“只给预处理条 → step 不得为 15011”的负控与「预处理+训练+2 loss」正控桩。
- 终态 metrics 至少含：`step`、`total`、`loss`、`lr`、`train_runtime`、`peak_vram`（`{allocated_gib, reserved_gib}`，取自 `trainer_log.jsonl` 的 `RECORD_VRAM` 记录）、`jsonl_path`（`trainer_log.jsonl` 绝对路径）、`artifact` / `artifacts`（`adapter_model.safetensors`）、`phase`、`elapsed_s`、`returncode`。
- 取消：核心写 `jobs\<id>\stop` 并发 `CTRL_BREAK_EVENT`（POSIX `SIGINT`），10 秒未退再 `taskkill /T /F`；终态 `cancelled`（桩会收到 stop 文件或信号后退出 0）。
- 缺 `model` / `data` / `out` / `template` → preflight `failed`，error 形如 `missing required param(s): model, data`（**逐项点名**，4 个字段各有一条自测）。
- 缺环境 → `failed`，error 形如 `lf preflight failed: LlamaFactory environment missing (未安装或不完整): lf-python=...`。

### 17.4 限制

- 核心 `kind="lf"` 已用 **当前代码真跑 2 步**验证（Qwen2.5-0.5B + dolly，job `job-20261022-080231-b275b572`：`succeeded`、`step=2/total=2`、`loss_series` step `[1,2]`、loss 3.8127→2.6485、`train_runtime 0.8788 s`、峰值 1.22 GiB、`adapter_model.safetensors` 在场）；另有桩 CLI 回归（`adapters\_tmp\lf_stub.py` 与 task-42 的 bars 桩）。
- `data` 必须是 **dolly 形状** JSONL（`instruction/context/response`）；核心转 alpaca。已是 alpaca（`instruction/input/output`）的文件会被当成 `response` 为空而**跳过全部行** —— alpaca 直传**未支持**（下一轮）。
- 没有显存准入；`rank/alpha/lr/epochs/...` 只做类型与下界校验（`rank>=1`、`cutoff_len>=16` 等），不做调优。
- 未接 `resume_from_checkpoint`（LF 支持，但核心合同本次不含）。
- `train_command` 是测试钩子，可执行任意命令；服务只绑 loopback + token。
- `kind="llm"`（自研 `adapters\llm_lora.py`）保留为备用/回归基线，行为未改。

---

## 18. 托管 v3 与接线注入

- **磁盘文件只读**：`design\prototype\v3\*` 与 `design\prototype\index.html` 一个字节都不改；token/wire 注入只发生在 HTTP 响应内存里。
- 路由：
  - `GET /`、`GET /v3/` → `design\prototype\v3\index.html`。
  - `GET /v3/<name>` → v3 目录内文件；非 HTML 原样字节，HTML 同样注入。
  - **根路径等价**：未命中其它路由时，若 `design\prototype\v3\<path>` 存在就直接返回（忽略 `?query`）；这是给 v3 页面里的相对链接 `llm.html`、`image-data.html?model=...`、`model-index.js` 用的。
  - `GET /legacy` → 旧 `design\prototype\index.html`，保持旧的 token 注入方式，便于对照。
- HTML 响应在 `</body>` 前注入两段：
  `<script>window.__MS_TOKEN__="...";window.__MS_API__="/api";window.__MS_UI="v3";</script>`
  `<script src="/ms-wire.js"></script>`
  token 来自核心内存，不写磁盘；旧 `/legacy` 只在 `</head>` 前注入 `__MS_TOKEN__`/`__MS_API__`。
- `GET /ms-wire.js` 读 `--assets-dir`（默认 `<project-root>\core\assets`）下的 `ms-wire.js`；文件不存在 → 404 + stderr 一行提示，服务不崩。R2 接线席落盘后无需重启（每次请求重新读）。
- 路径穿越：任何 `..`、绝对路径、编码绕过均 403/404；不允许离开 v3 目录。
- 自测：`GET /` 注入后字节数、v3 index SHA 前后不变、`/v3/model-index.js` 字节等盘、`/llm.html` 与 `/model-index.js` 根路径等价、`/legacy` 200、穿越 404、`/ms-wire.js` 空临时目录 404→放文件 200。
- 完整映射与页面互跳说明见 `docs\V3-WIRE-MAP.md`（唯一出处，不在本文件复制）。

## 19. 模型仓与扫描

- **唯一根**：`<project-root>\models`（默认 `E:\视频\modelsmith\models`；`--models-dir` 可改，非绝对路径按项目根解析）。核心**只扫这里**，不扫 ComfyUI、系统目录或其它位置。
- 存放指引的**唯一出处**是 `models\README.md`；本节只写扫描/API 口径，不复制正文。
- 内容判定（不看目录名）：
  - `model_index.json` → `diffusion`；
  - `rl_agent_config.json` 或 `model.safetensors` + `encoder\` + `tokenizer\` → `laya`；
  - `config.json` + 顶层 `*.safetensors` → `llm`；
  - 只有顶层 `*.safetensors` → `diffusion_single`（`arch_hint` 取文件名前缀，如 qwen/flux/sd）；
  - 其它 → `unknown`（列出但**不可选**）。
- 扫描深度 ≤3；目录名即 `id`，重名自动加 `-2`；单目录 >200 GiB 或 >5000 文件标 `skipped_too_large` 并跳过分类；符号链接/junction 不跟随。
- `GET /api/models`（需 token，与 `/api/jobs` 同级）：
  ```json
  {"ok": true, "root": "E:\\视频\\modelsmith\\models", "scanned_at": "...",
   "models": [{"id":"...", "name":"...", "path":"...", "kind":"llm",
               "size_bytes":123, "files":4, "arch_hint":null}]}
  ```
  支持 `?kind=diffusion|diffusion_single|llm|laya|unknown`；**complete 在前**，同组按 kind/name 排序；60 s TTL，目录 mtime 变化则重扫；根不存在返回 200 + 空列表 + `root_warning`。
- 每条模型带 `complete:bool` / `missing:[组件名]`：解析 `model_index.json` 的非 `_` 键，逐项检查对应子目录/文件（数组取第一项）；残缺 diffusers 目录 `complete=false`，UI 不可选。`kind="diffsynth"` 提交时若 `model` 指向残缺目录 → **400** 并点名 missing，不会等子进程报错。
- 相对 id 解析（只在 `models\` 内）：`kind="laya"` 的 `base`、`kind="lf"` 的 `model`、`kind="diffsynth"` 的 `model` 传 `id`/目录名时，核心在交给适配器前替换为绝对路径；**绝对路径原样直通**，不破坏已有 e2e。`unknown` id 不解析。
- 自测：4 个假目录（diffusion/llm/laya/unknown）内容判定、`?kind=llm` 过滤、无 token 401、相对 id 命中绝对路径、unknown/绝对路径负控。

---

## 20. 下载 / 打开目录 / 星标 / Doctor（F07/F18/F19/F34）

### 20.1 `POST /api/models/{id}/download`（token）

- body：`{"sources":["http://..."],"resume":true}`；`sources` 必填，按序尝试；`resume=true` 用 Range 续传。
- 只允许已扫描到的 `models\` 条目 id；目标只能是 `models\` 内：
  - 目录条目 → `models\<id>\<源文件名>`；单文件条目 → 原文件路径；
  - 临时文件 `<target>.part`，完成后重命名；失败保留 `.part` 供续传。
- 202 返回 `{ok,id,state,status_url,...}`；同 id 已有状态时直接返回既有状态，不重复起线程。
- 事件（`GET /api/models/{id}/download/status`，token）：
  - `{"type":"attempt","source","i","n"}`
  - `{"type":"progress","bytes","total","pct","speed_bps","eta_s"}`
  - `{"type":"switch","from","to","reason"}`（某源失败自动切下一源）
  - `{"type":"done","path"}` 或 `{"type":"failed","errors":[...]}`
- 全部源失败 → `state=failed`，`errors` 非空；`.part` 不自动删。仅使用 stdlib/urllib，不依赖 aria2c。
- `POST /api/models/{id}/download/cancel`（token）：置位 cancel event、删除 `.part`、状态 `cancelled`；线程下一块退出，不继续增长。
- 错误：id 不存在 404、sources 空/非法 400、目标逃出 `models\` 400。

### 20.2 `POST /api/open?dry=1`（token）

- body：`{"path":"E:\\视频\\modelsmith\\_runs\\..."}`；允许 `models\`、`_runs\`、项目根子树（realpath 前缀校验）；拒绝 `..`、绝对路径逃逸、符号链接逃逸 → 403 并点名。
- `dry=1` 只校验不弹窗；正常调用 `subprocess.Popen(["explorer", path], shell=False)`。
- 成功 `{ok:true,dry:bool,path}`；不存在 404；越界 403。

### 20.3 `POST /api/jobs/{id}/star`（token）

- body：`{"artifact":"step-1200.safetensors","starred":true}`；artifact 可为文件名或绝对路径。
- 写 job `metrics.starred_artifacts`，合并去重；`starred:false` 取消；同一 artifact 重复写幂等。
- 返回 `{ok, job_id, starred_artifacts:[...]}`；job 不存在 404。

### 20.4 `/env/doctor`（token）

- UI 抽屉只读现有字段；核心补齐 `core_version`、`paths.ui_bytes`、`paths.models_dir`、`paths.v3_*`、`paths.wire_js*`，不另造第二套。
- 不返回 token；不把下载源/星标等敏感状态塞进 doctor。

### 20.5 自测

下载用本地临时 HTTP（146 B 文件）：正控 attempt/progress/done、`.part` 不存在、146 B；404 源切第二源成功；全 404 → failed/errors。`/api/open?dry=1` 允许 `_runs`、拒绝 `C:\Windows` 与 `..\..\x`。星标重复写幂等、两个 artifact 合并为 2、缺 job 404。

---

## 21. 历史回放 / 语料体检 / 早停（F22/F17/F15）

### 21.1 F22 `GET /api/jobs/{id}/metrics_history?since=<seq>`（token）

- 每个 job 在 `<data_dir>\jobs\<id>\metrics.jsonl` 追加一行 `{ts, seq, ...metrics}`；每次 metrics 变化写一行，`seq` 从 1 单调递增。
- 响应：`{ok:true, job_id, rows:[...], next_seq}`；`rows` 只含 `seq > since`；`since=最后一条` → `rows=[]`，`next_seq` 不变。
- 用于完成页/曲线断线重连；日志仍走 `/logs?offset=`，两者不混用。

### 21.2 F17 `GET /api/data/hint?path=<file>`（token）

- 只读前 200 行 / 前 256 KiB；路径只允许 `models\`、`_runs\`、项目根子树（realpath 校验，拒绝越界/符号链接）。
- 响应：
  ```json
  {"ok":true,"path":"...","rows_guess":3,"lang_hint":"zh|en|mixed",
   "schema":{"has_state":true,"has_questions":true,"has_gold":true,"has_expected":false},
   "format":"laya|csv|unknown","issues":[]}
  ```
- 语言：只统计 JSON 字符串值/CSV 数据行中的 CJK 与拉丁字母，CJK 占比 >0.3 → zh、<0.02 → en，否则 mixed。
- schema：Laya 复合格式（`state`+`questions`+`gold|expected`）→ `format="laya"`；`text,label` CSV → `format="csv"`；其它 `unknown` + `issues`。
- 文件不存在/不是文件 → `ok:false` + `error` 点名；不读盘符根或允许根之外。

### 21.3 F15 `POST /api/jobs/{id}/early_stop`（token）

- body：`{enabled:true, patience?:2, min_delta?:0.01}`；默认 **disabled**，只有显式调用才启用。
- 核心在 Laya metrics 更新时检查 `epoch_mean_loss`：连续 `patience` 个 epoch 改善 < `min_delta` → 写 `jobs\<id>\stop`、`cancel_requested=1`，终态 `cancelled`，`metrics.early_stop={triggered:true,reason,...}`。
- `enabled:false` 可关闭；纯状态机 `early_stop_step()` 自测覆盖触发/不触发两条。
- **红线：早停不是 resume。** Laya 中断后只能从头再训；不要承诺从 checkpoint 续。

### 21.4 自测

fake 完成后 `metrics_history` 行数 ≥3、seq 单调、`since=last` 空；`skills\laya-corpus` 样例 → laya/zh，临时 `text,label` CSV → csv/en，缺文件 → ok:false；早停 stale 2 触发、真实改善不触发。

---

## 22. 原生选择框与采样图托管（F18/F14）

### 22.1 `POST /api/pick`（token）

- body：`{"mode":"file|dir","title":"...","filter":["jsonl","csv"]}`；返回 `{ok:true,path}`。
- Windows 原生 `System.Windows.Forms`（PowerShell `-STA`）弹 `OpenFileDialog`/`FolderBrowserDialog`；**不依赖 tkinter / 不装包**。
- 取消 → `{ok:false,cancelled:true}`；超时（180 s）→ `{ok:false,reason:"dialog_timeout"}`；无桌面会话/失败 → `{ok:false,reason:"no_desktop"}`。
- 对话框在请求线程内等待，但 `ThreadingHTTPServer` 其它路由（`/health` 等）继续响应；selftest 用 `--pick-stub "path|__cancel__|__no_desktop__"` 证明。
- 只返回路径，不落盘、不写日志、不回显 token；`--pick-stub` 仅测试用。

### 22.2 `GET /api/jobs/{id}/samples/<rel>`

- 从 job `metrics.output_dir`（或 params.out）的 `samples\` 下读文件；样例按 png/jpeg/webp 设置 `Content-Type`。
- 同源可免 token（`<img>` 可直接加载）；路径只在该 `samples\` 子树，`..`/绝对路径/编码绕过 → 403；无 samples 或文件不存在 → 404 并点名。
- 目录逃逸、符号链接逃逸一律拒绝；返回 `Cache-Control: no-store`。

### 22.3 `GET /api/jobs/{id}/artifacts`（token）

- 扫 output_dir 顶层 `*.safetensors`/`*.sft` 与 `samples\**` 文件：
  `{ok, job_id, artifacts:[{name,path,bytes,sha256}]}`；`name` 是相对 output_dir 的 `/` 路径。
- 无 output_dir/无产物 → 200 + 空列表；job 不存在 404。

### 22.4 自测

`/api/pick` 正/取消/no_desktop/非法 mode 四态，且 stub 延迟 1 s 期间 `/health` 仍 200；samples 用真实 1×1 PNG（200/image/png/68 B）、`../core.db` 403、缺失 404；artifacts 同时列出 `epoch-0.safetensors` 与 `samples/step-2/a.png`。

---

## 23. 评测与 AI 报告（F23/F24/F25/F26）

### 23.1 `POST /api/jobs/{id}/eval`（token）

- body：`{data?: "<heldout.jsonl|空>", limit?:96}`；要求 job 已到终态，否则 **409**；job 无 `output_dir`/产物 → `{ok:false,error}`。
- 数据源：`data` 非空 → `data_source="heldout"`；空 → `calibration`（用 `train_report.json` 的校准口径，UI 必须显示 in-sample 黄条）。
- body 增加 `mode:"auto|replay|cli"`（默认 auto）：`auto` 有 data 且配置了 CLI stub/可起 Laya CLI → cli，否则 replay；`cli` 用 ComfyUI python + `sys.path.insert(third_party\laya)` 起 `laya.evals_cli run <data> --model <out> --device cuda --json <tmp.json>`（测试可用 `--eval-cli-stub` 桩，超时 `--eval-timeout` 默认 300s）；逐条预测只来自 CLI 输出的 JSON，不编造。CLI 超时/非 0/JSON 失败 → 回退 replay，响应 `warn:"eval_cli_failed"`。
- `replay` 从 `<out>\train_report.json` 回放 metrics/confusion/errors，`per_question=[]`，带 `note:"本次训练未落逐条预测"`。
- 响应：`{ok, job_id, data_source, n, mode, metrics:{...}, per_question, confusion, errors, note, output_dir_source:"metrics|params"}`；结果缓存到 `<job>\eval_<ts>.json` 供 /report 复用。
- **`output_dir` 编码口径（P0 hotfix）**：读适配器 `*.metrics.json` 一律 `encoding="utf-8", errors="strict"`；出现 U+FFFD 立即抛错并点名。`_job_output_dir_info()` 先试 metrics.output_dir（必须无 U+FFFD 且在允许根内），失败则回退 `params_json.out`，响应带 `output_dir_source:"params"` 和 warnings；两者都坏 → `ok:false` 点名。老 job 的坏 metrics 不改写；新 Laya job 收尾时用 `params.out` 修复 output_dir。真实历史目录 `_runs\ui-laya-20261022-190622`（model.safetensors 842,609,220 B）复测 eval → `ok:true`、accuracy 0.8542、source=params。

### 23.2 `POST /api/jobs/{id}/report`（token）

- body：`{language:"zh", model?, template:"laya-eval-v1"}`；读最新 `eval_*.json`；没有 → `{ok:false,error:"run /eval first"}`。
- 引擎：默认 **模板** `engine:"template"`（确定性四段：总体→错例→归因→建议，结尾固定 `in-sample ≠ 泛化` 提醒）。显式 `--report-llm <HF目录>` 且当前无 running job、能拿 GPU 锁时，子进程跑 `core\report_llm.py`（ComfyUI python + transformers，greedy，超时 `--report-timeout` 默认 60s）→ `engine:"llm"`；`--report-llm` 为空/GPU 忙/超时/非 0/缺四段/接地失败 → 模板兜底 + `warn:"llm_failed"`（接地失败用 `warn:"ungrounded_numbers"`）。当前默认仍 template-only，LLM 只在显式开启时使用。
- 数字接地：`grounded_numbers(markdown, metrics)` 抽取所有 `0.xx`/百分比，逐个与 metrics ±0.01 比对；不在 metrics 的数字会被判 ungrounded，响应带 `warn:"ungrounded_numbers"`（模板只用 metrics 值，正常无 warn）。
- 返回 `{ok, engine, model, elapsed_s, markdown, json:{job_id,data_source,n,metrics,confusion,errors,summary,engine,generated_at}}`；导出文件由 UI/wire 层落盘。

### 23.3 红线

- **不许编造逐条答案**：b 路径 `per_question` 必须为空，并明写 note；只有真实跑评测才能填。
- **数字接地**：报告只能引用输入 metrics 里的数字；新增数字即回退模板并告警。
- 评测是只读操作，不改原 job；LLM 报告若启用需先拿 GPU 锁，拿不到直接模板。

### 23.4 自测

`train_report.json` fixture → calibration n=12/per_question=[]/note；heldout 数据也只回放 metrics 且 per_question=[]；`/report` 四段标题 + in-sample 提醒 + 无 warn；`grounded_numbers("0.1234", metrics)` 返回 `["0.1234"]`；非终态 409；无 output_dir → ok:false。

### 23.5 运行稳定性补充

- `/health` 增 `code_sha256`（核心文件前 16 位）、`code_mtime`（ISO）、`started_at`；读自身文件失败时 `code_sha256:null`，不崩。启动日志第一行固定 `data_dir=<实际路径>`（显式 `--data-dir` 时不再尝试 `%LOCALAPPDATA%`）。
- diffsynth 数据集 caption 告警：`dataset_metadata.from_txt == 0` → `metrics.warnings` 含 `no_captions: 全部图片没有同名 .txt...`，日志 `# WARNING: no captions...`；`0 < from_txt < n` → `partial_captions: <缺>/<n> 张缺 caption`；全部有 txt 不告警。

### 23.6 V5 启动清扫残留 llama-server

- 启动时 `select_stale(procs)` 纯函数筛选：映像名必须是 `llama-server.exe`，命令行必须含 `--mmproj` 且带本机模型目录特征，且父 PID 不是当前 core；空/不匹配列表一律返回空。
- **默认 dry-run**：未加 `--sweep-stale-servers`（或 `MODELSMITH_SWEEP_STALE=1`）只打印 `sweep dry-run: would kill n`，不杀进程；显式开启才逐条 `swept pid=… cmd=…`。


