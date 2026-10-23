# UI 基线 v1（维护者 裁决 · 2026-10-22）

## 1 裁决

**接受 `E:\视频\modelsmith\design\prototype\index.html` 为 ModelSmith UI 基线 v1，并冻结（只读）。**
依据：`review\FINAL-AUDIT-v2.md`（15/15 红项全清、P0 3/3 清零、P1 0、P2 0、无新 P0）+ 本席独立复核（四套测试 231 PASS / 0 FAIL、FLOWS 未变、静态四条、`node --check`=0）。

## 2 冻结件

- 路径：`E:\视频\modelsmith\design\prototype\index.html`
- 读数：113,604 B / 1,744 行 / SHA256 `5982D874D86A1CA952D1988A334086B3D8CA68A9305AA6A390167B9073BBA46C`
- 属性：只读（`IsReadOnly=True`）
- 快照：`design\prototype\_recovery\fix1-5982D8.html`
- 流程契约：`docs\UI-FLOWS-v2.md`（四类各自 4/5/5/5 步、进度单位、成果节奏、B10–B14）
- 事实来源：`docs\TRAINING-FLOWS.md`

## 3 既有残余（不阻塞基线，进 P2 backlog）

| # | 残余 | 级别 | 处理 |
|---|---|---|---|
| R1 | 文档声称 `--r-sub/--r-sub-ctrl` token，CSS 实际仍是字面量 20/10 | P2 | 已改 `ART-NOTES.md` 说明；不建 token（等真实组件复用再抽） |
| R2 | 抽屉 `.xbtn` 36×36、`.switch` 36×20 < 40 触控目标 | P2 | 下轮 UI 小修 |
| R3 | K4 返回首屏后「训练继续」只写隐藏日志，首页无可见状态 | P2 | 下轮 UI 小修 |
| R4 | 生图 arch 默认未进显存公式（25.2 vs 25.6，0.4GB 文案差） | P2 | 下轮 UI 小修 |

## 4 变更纪律

- 基线冻结后，**任何改动必须**：① 先快照；② 解锁；③ 小块 edit；④ `node --check` + headless 四流程回归；⑤ 报 SHA；⑥ 再锁。
- 美术/框架两席的写权保持不变（美术只交素材）。

## 5 下一步

P2 核心服务（`core/modelsmith_core.py`，stdlib-only）：`127.0.0.1:8765`、SQLite WAL 作业表、GPU 锁、假任务、日志尾随、`/health`、静态托管本基线 UI。验收：双击起服务 → `/health` 200 → 假任务排队/取消 → 日志可尾随 → 只绑 127.0.0.1。

## 6 P2 接线版（UI 双模式 · 2026-10-22）

- 新基线：121,140 B / 1,876 行 / SHA256 `9DB9C35B21E3D93B49B820B97959499FDFAC318D7CEDA958F06F6B0859DA63EC`
- 双模式：`window.__MS_TOKEN__` 存在（核心 `GET /` 内存注入）→ 真 HTTP；`file://` → 原模拟一行未改。
- 维护者 亲自 e2e（核心随机端口 64987 + headless CDP）：**19 项断言全 PASS**——token 注入、建 job（`job-20261022-052509-...`）、日志增长（1→4 行）、进度 `step 2 / 20`、VRAM「未检测」、取消（服务端 `cancelled cancel_requested=true`）、成功路径 21.2s 到完成页、服务端 `succeeded`、token 不在 URL/日志；测后端口监听 0。
- 施工席回归：真模式 20/0、`file://` 11/0、旧模拟 50/0。
- 快照：`design\prototype\_recovery\wire-9DB9C3.html`
- 残余（P2 backlog）：800ms 轮询（无 SSE）；`failed` 路径未专门测；未用 8765 默认端口跑；核心仅 `fake` 适配器。

## 7 冻结链补登（接线后每一版都只读锁定 + 有快照）

| 版本 | SHA256（前 16） | 字节 | 变化 | 快照 |
|---|---|---|---:|---|
| fix1 | `5982D874D86A1CA9` | 113,604 | 审计 15 红项清零 | `_recovery\fix1-5982D8.html` |
| wire | `9DB9C35B21E3D93B` | 121,140 | UI→核心 fake 接线 | `_recovery\wire-9DB9C3.html` |
| llm | `1A861EBFF3A1A642` | 125,882 | `data_path` + LLM 真 e2e 19/0 | `_recovery\pre-img-ui-1A861E.html` |
| diffsynth | `6381286579E9A10C` | 129,873 | 生图发 `kind="diffsynth"`、arch `qwen_image_2_1`（20/0） | `_recovery\pre-diffsynth-ui-1A861E.html` |
| lf | `992AA4392DDEDEF2` | 130,136 | LLM 发 `kind="lf"`、`max_steps=2`（LFREAL 20/0） | `_recovery\pre-lf-ui-638128.html` |
| task40 | `5A467DE663375F7A` | 130,631 | LLM `data_path` 必填（删注册名兜底）+ smoke 诚实文案 | `_recovery\pre-task40-ui-992AA439.html` |
| video-disabled | `7B64C45957A62970` | 130,828 | 视频入口保留「暂未开放」（`aria-disabled`）；P0 修复内容保留 | `_recovery\pre-laya-ui-7B64C4.html` |
| live-laya | `6897E9B90EBA90B5` | 139,257 | 决策线真 payload：`liveLayaPayload()`（第 1388 行，`kind:"laya"`）+ `data_path` 必填校验（LIVE 模式）；**未提交**（git `M`） | `_recovery\live-laya-6897E9.html` |

- 每一版都满足：`node --check`=0、`https?://`=0、`<link|@import`=0、`<script src=`=0、`·`=0、CSS/FLOWS 与上一版逐字节相同（只改 JS）。
- 变更纪律同上（§4）；**当前交付版 = `6897E9B90EBA90B5…`（139,257 B，只读，mtime 10-22 16:19，含决策线 `liveLayaPayload`；未提交）**。
  - 历史指针（不删）：上两版为 `7B64C459…`（130,828 B，视频禁用）与 `5A467DE663375F7A…`（130,631 B，task-40）；「130,631 B / 1,991 行」这个读数只属于 task-40 版，不再代表盘上当前文件。
