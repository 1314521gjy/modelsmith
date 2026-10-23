# Laya 版本演进与 checkpoint 调查（2026-10-23）

> 问题：用户记得「后面出了很多变体/再训练版，速度和准确率提升很大，我们下的可能是早期版本」。
> 证据等级：**A**=本机实测/源码；**B**=官方文档（GitHub/PyPI/HF 模型卡）；**C**=第三方；**D**=推断。
> 方法：只读本机 + 只查上游元数据/模型卡，**未下载任何权重、未跑 GPU**。查询时间 2026-10-23 15:3x。

## §1 本机现状

| 项 | 读数 | 依据 |
|---|---|---|
| 版本 | **0.4.13** | `third_party\laya\pyproject.toml:6-7`（`name="laya"` / `version="0.4.13"`）A |
| git | `6f0496f release: 0.4.13`（2026-10-21）；tag **仅 `v0.4.13`**；**detached HEAD**（无本地分支）；remote `https://github.com/NandhaKishorM/laya.git` | `git -C third_party\laya log -1 / tag -l / branch -a / remote -v` A |
| 权重（**仅 ModelSmith 仓**） | **没有**：`E:\视频\modelsmith\models\` 下 0 个 laya 目录；`third_party\laya` 1,601 文件里 `*.safetensors` **0**。**但另一块盘有完整离线部署，见 §1.1** | A（目录扫） |
| adapter | 只接**训练**：`LAYA_TRAIN_DEFAULT="laya-train"`（`adapters\laya_adapter.py:52`）、`build_argv`（`:157`、`:171`）、**无 resume**（`:23` 自述）；core 侧 `_run_laya`（`core\modelsmith_core.py:2673`）、`build_laya_train_command`（`:1488`） | A |
| 推理入口 | **没有**：core 里 `Router`/`system_one` **0 命中**（grep 全文件），只有训练路由 | A |
| 训练默认底模 | `laya-train --base` 默认 **`convaiinnovations/laya`** ⇒ 首次训练会从 HF 下 ~803 MiB（除非传本地 `--base`） | `third_party\laya\laya\train_cli.py:48` A |
| 既有研究 | 只有训练/测验协议：`docs\research\laya-train.md`（无版本记录） | A |

### §1.1 另一块盘的完整离线部署（laya **0.3.5**，与 0.4.13 **同名不同物**）

| 项 | 读数 | 依据 |
|---|---|---|
| 路径 | `E:\视频\拉亚决策模型\`（存在，含 `models\`/`vendor\`/`scripts\`/`数据\`/`logs\`） | A |
| 版本 | **laya 0.3.5**：`vendor\laya-0.3.5.dist-info\METADATA` `Version: 0.3.5`；`说明书.md:3`「版本：laya 0.3.5（Convai Innovations，Apache 2.0）· 部署日期：**2026-09-22** · 部署形态：本机离线包」 | A |
| 权重 | `models\laya\` **15 文件 / 2,370,592,926 B（2260.8 MB）**；`说明书.md:4` 称「权重合计 2260.8 MB（15 个文件，sha256 全部校验通过）」 | A |
| 三权重 sha256 前 16（**本机重算**） | `model.safetensors` **`891102d372688fc2`**（842,609,210 B）；`multilingual/model.safetensors` **`9d628fd971b70038`**（643,835,514 B）；`typed-decisions/model.safetensors` **`4fa56de72383a9d3`**（842,609,220 B）——**与当前 HF hub 三个 LFS oid 逐字节一致**（见 §2） | A |
| 中文封装 | `vendor\拉亚本地包.py`：`加载路由模型:70` / `决策:81` / `问题模板:86` / `置信度分流:115` / `简述:131` | A |
| 脚本/数据 | `scripts\smoke.py`、`scripts\自检.py`、`启动体验台.cmd`、`数据\` | A |
| 自训练记录（**真跑过**） | `logs\自训练3.log`：基础模型 `.\models\laya`（max_len=512 head_max_len=192）；`数据\样例-闸门.train.jsonl` **320 条 → 960 序列（768 训 / 192 校准）**；**计划 2 轮 × 24 步**（micro=4 accum=8 group=4）；**轮1 完成：均损 -6.7325 / 训练内准确率 0.462 / 46s**（该日志只跑完轮1）；早前 `自训练.log`/`自训练2.log` 同为 2 轮计划、micro=8 accum=4 | A |
| 是否 0.4.13 | **否**：部署全文 grep `0.4.13` **0 命中**；vendor 只有 0.3.5 | A |

> **同名不同物（必须写死）**：`laya 0.3.5` = **vendored 包版本**（`E:\视频\拉亚决策模型\vendor\laya`）；`0.4.13` = **GitHub 仓库版本**（`E:\视频\modelsmith\third_party\laya`）。同名 `laya`、不是同一个东西：**本机同时存在两份代码，只有 0.3.5 部署了权重并真跑过自训练**；`third_party\laya` 那份只是源码（无权重）。

## §2 上游版本与 checkpoint 清单

**版本（B）**：GitHub 最新 release = **v0.4.13**（2026-10-21T13:42Z）；tags 从 v0.3.14 到 v0.4.13（v0.4.13 为最新，**晚于它的 release/tag 0 条**）。PyPI `laya` 版本 0.1.0 → **0.4.13**（最新）。repo created 2026-09-18，pushed_at 2026-10-21，stars 34,020 / forks 3,062 / open issues 93，license **Apache-2.0**。
- URL：`https://api.github.com/repos/NandhaKishorM/laya/releases?per_page=20`、`https://api.github.com/repos/NandhaKishorM/laya/tags?per_page=30`、`https://api.github.com/repos/NandhaKishorM/laya`、`https://pypi.org/simple/laya/`

**checkpoint（B）**：HF 上 `convaiinnovations` **只有 3 个 laya repo**（`laya`、`laya-multilingual`、`laya-typed-decisions`）；**`-small`/`-xl`/`distill`/`v1.x` 等变体 0 命中**。三个权重**都在同一个 hub repo** 里（root + `multilingual/` + `typed-decisions/` 子目录），也各有独立 repo。

| checkpoint | 位置 | 参数 | 上下文 | 权重文件 | 首次发布 | 最后权重变更 | 许可证 | 官方声称 |
|---|---|---|---:|---|---:|---|---|---|---|
| `laya`（英文） | hub root `model.safetensors`；`https://huggingface.co/convaiinnovations/laya` | **421.3M**（F16） | 512 | **842,609,210 B（803.6 MiB）** | 2026-09-18 | **2026-09-19** | Apache-2.0 | T4 单问 **39.5 ms**；typed-decisions 0.362 |
| `laya-multilingual` | hub 子目录 `multilingual/model.safetensors`；独立 repo 同名 | **321.9M** | 1024（可 8192） | **643,835,514 B（614 MiB）** + tokenizer 34.4 MB | 2026-09-19 | **2026-09-19** | Apache-2.0 | T4 **32.8 ms**（~2.2× 快）；51 语言 macro **0.4008**、usable **48/51** |
| `laya-typed-decisions` | hub 子目录 `typed-decisions/model.safetensors`；独立 repo 同名 | **421.3M** | 1024 | **842,609,220 B（803.6 MiB）** | 2026-09-18（repo）/ 09-19 打包 | **2026-09-19** | Apache-2.0 | typed-decisions **0.766**（微调后）；base 0.362 |

- hub 全仓 `usedStorage` = **2,366,100,536 B（2.20 GiB）**；三个 checkpoint 合计 ≈ **2.17 GiB**（可单下）。
- 权重**自 2026-09-19 起未再变**：HF commits 里最后一次权重相关提交是 09-19 06:48「bundle the multilingual/typed-decisions checkpoint」；之后只有 README/config 改动（最新 2026-10-03「chore: root config.json so the Hub tracks downloads」）。模型卡 0.3.20 段也明写「**The checkpoints themselves are unchanged**」。
- URL：`https://huggingface.co/convaiinnovations/laya`、`.../laya-multilingual`、`.../laya-typed-decisions`、`https://huggingface.co/spaces/convaiinnovations/laya-demo`；元数据经 `https://hf-mirror.com/api/models/convaiinnovations/laya{,/tree/main?recursive=true,/commits/main}` 取回。

## §3 与我们 v0.4.13 的差异

| 维度 | 结论 | 依据 |
|---|---|---|
| 代码版本 | **我们就是最新**（0.4.13 = 最新 release/PyPI），没有更新的包可选 | §2 B |
| checkpoint | **没有「很多变体」也没有重训**：3 个，自 09-19 冻结；0.4.x 只在 runtime/工具上升级 | §2 B + 模型卡 |
| 速度 | checkpoint 未变 ⇒ 官方延迟数字不变（T4 39.5/32.8 ms；批量 10 问 158.6/72.3 ms）；0.4.13 仅把 `answer_confidence` 帮手提速 3.8×（对端到端影响小） | `BENCHMARKS.md:198-206`、README 0.4.13 段 B |
| 准确率 | 英语 0.953 AG News / 0.600 DAIR / ECE 0.081（拟合后）；多语言 macro 0.4008；typed 0.766 仅限四工作流 | `BENCHMARKS.md:39-47,55-61,175-181` B |
| 多语言边界 | 英文 checkpoint 在非拉丁文字**崩溃**（Khmer 0.000 @ 0.952 置信度）⇒ 非英文必须用 multilingual | `BENCHMARKS.md:139` B |
| 接口 | `laya-train` CLI / `Router` API 稳定；0.4.13 无会打断我们 adapter 的改动 | README 0.4.13 段 + adapter 只依赖 train CLI A/B |

## §4 建议与代价（含「不升级」的理由）

**裁决：代码不用升（我们已是最新 0.4.13）；要不要下权重取决于「是否现在就用决策模型」。**

- **推荐 checkpoint**（按用途）：
  1. 英文决策/护栏/triage ⇒ `laya`（root，421M/512，39.5 ms）；
  2. 中文/多语言或更省算力 ⇒ **`laya-multilingual`**（322M/1024，32.8 ms，2.2× 快；51 语言 48 可用）——中文单语言准确率**未在表中找到=未验**；
  3. 就用那四个 typed 工作流（invoice/security/客服/agent trace）⇒ `laya-typed-decisions`（0.766，但必须知道它是在该 benchmark 训练集上微调的，base zero-shot 只 0.362）。
- **显存/延迟预期（D，基于官方 T4 数）**：权重 F16 0.64–0.84 GB；`Router` 默认驻 2 个 ⇒ **~1.5 GB**，`preload=True` 三个 ⇒ **~2.3 GB**；CPU 也能跑（README 给 193–464 ms/问）。我们的 4080S/4090 **显存远够**，但**本机实测=0（未验）**。
- **要改 `adapters/laya_adapter.py` 吗？** **训练线：不用**（它只拼 `laya-train` argv，不假设版本/权重路径，`:52/:157/:171`）。**推理线：要做就得新接**——ModelSmith 现在没有 Laya 推理路由（`Router`/`system_one` 在 core 0 命中），需要新 adapter/路由 + 依赖核对（`transformers>=4.48`、`torch>=2.0`；运行时有 4.57.6，torch 版本未记录=未验）。
- **下载代价**：整 hub 2.20 GiB；单 checkpoint 0.61–0.80 GiB。**但三个权重本机已有**（§1.1，sha256 与 HF 当前 oid 一致）⇒ **没必要重下**；缺的只是 0.4.13 代码（runtime 修复）。训练首跑默认自动下 root（`train_cli.py:48`），可 `--base E:\视频\拉亚决策模型\models\laya` 指本地。
- **许可证**：repo 与 HF 卡均 **Apache-2.0**，HF tags 含 **`commercial-use`** ⇒ **可商用**（保留 LICENSE/NOTICE；本地 `third_party\laya\LICENSE` 10,349 B 在位）。B
- **不升级/不下载的理由**：① 没有更新版本可升（0.4.13 已最新）；② 权重自 09-19 冻结，不存在"新重训版"；③ 权重本机已有（§1.1），下载不是门槛；真正的缺口是 **ModelSmith 没有 0.4.13 的推理入口**；④ 训练线要跑时用 `--base E:\视频\拉亚决策模型\models\laya` 即可，不必联网。

## §5 未验项

- **本机有 0.3.5 离线部署与自训练记录**（§1.1，A）：vendored 0.3.5 已真跑过 320 条/960 序列自训练（2 轮计划，轮1 均损 -6.7325 / acc 0.462 / 46s）；**0.4.13 未部署、未在本机跑** ⇒「本机没跑过 Laya」应精确为「**0.4.13 没跑过**」。4080S 上 `Router` 推理的**延迟/显存**仍**未验**（自训练日志不含延迟指标）。
- 运行时 `torch` / `huggingface_hub` 版本未记录；`transformers 4.57.6 ≥ 4.48` 满足 Laya 的 floor（`pyproject.toml:27-31`），但**未实测 import**。
- 中文单语言准确率**未在 BENCHMARKS 表中找到**（表中未见 `zh` 行，未验）。
- 独立 repo `convaiinnovations/laya-typed-decisions` 的 API 本次**超时未取到**（以 hub 内 bundled 文件的 842,609,220 B 与 421M 参数为据）。
- 0.4.0–0.4.10 的逐版发布日期未逐条抓取（只核 0.4.11/0.4.12/0.4.13 与 0.3.26–0.3.28）。
- 训练首跑自动下载 root checkpoint 的行为**未实测**（代码 `train_cli.py:48` 推断）；本机已有离线权重（§1.1），可 `--base` 本地路径规避。
- 离线 0.3.5 的 `加载路由模型/决策/置信度分流` 在 4080S 上的实测速度**未验**（只核了权重与自训练日志，未跑推理）。
