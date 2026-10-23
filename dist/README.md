# 分发打包方案（2026-10-22 起生效）

> 用户要求：**所有内容最终都要分发**。语料/训练集**单独打包**；其余环境与组件**打进同一个包**。
> 因此从现在起：任何产物必须落在固定目录、带来源与许可，**不许只躺在 `_tmp` 里**；打包前用 `dist\collect.py` 生成清单核对，防止丢件。

## 1 两个包（不是两个以上）

| 包 | 内容 | 打包形态 | 现状 |
|---|---|---|---|
| **A. 数据包** | `resources\01-决策模型-业务语料`、`02-LLM-语料`、`03-LLM-小模型`、`04-生图LoRA-图集`、`05-视频LoRA-图集` | 每类一个 zip（可分开发布）；每个包内必须有 `LICENSE/` 与 `MANIFEST.md`（来源、许可、行数、SHA256） | 01 已就绪 8.25 MB；02–05 待补 |
| **B. 主包** | UI（`design\prototype\index.html`）、核心（`core\`）、适配器（`adapters\`）、启动器、`docs\`、`LICENSES\`、**运行时环境**（Python embed + 所需 wheels） | 一个 zip（解压即用） | UI+core+适配器已有；运行时打包待做 |

## 2 目录与归属（唯一出处）

```
E:\视频\modelsmith\
├─ design\prototype\index.html      → 主包 web\index.html（冻结基线，SHA 记录在 docs\UI-BASELINE-v1.md）
├─ core\                            → 主包 core\（modelsmith_core.py / README / selftest）
├─ adapters\                        → 主包 adapters\（laya / aitk / llm + 各自 selftest；_tmp 不进包）
├─ resources\                       → **数据包**（01–05 分类；README 与 MANIFEST 随包）
├─ docs\                            → 主包 docs\（PLAN、UI-FLOWS、TRAINING-FLOWS、资源清单、裁决记录）
├─ review\                          → 主包 docs\review\（QA 清单与审计）
└─ third_party\                     → **默认不进任何包**（只把 LICENSES 清单与 SHA 带进主包）
```

## 3 明确不进包（许可/体积原因）

- `third_party\` 四棵源码树（Apache/MIT 可分发，但体积 169.5 MB；如要带只带 LICENSE 与 MANIFEST）。
- ComfyUI 本体（GPL-3.0）、`E:\ComfyUI\ComfyUI\models` 底模（各自许可不同）。
- 本机 107 个 GGUF（推理格式，且许可未逐个核）。
- Laya 底座权重（许可未核，且 842 MB）。
- 无许可证数据：SMP2017/2018（ECDT）、CLINC150（许可未取回）——**只在本地研究，不进数据包**。

## 4 每个产物的记录方式

`dist\collect.py` 扫描上述目录，生成 `dist\manifest.json`（rel 路径、字节、SHA256、归属包），并打印汇总。
**口径（易误读）**：`manifest.json` 是**全机盘点**（含 `resources\03-LLM-小模型`、`resources\07-底模` 等权重），**不代表进包**；进包集合只由 `dist\pack.py` 的 selection + 硬排除决定。
**许可**：manifest 条目**没有 license 字段**；许可逐项见 `third_party\LICENSES\` 与各 `resources\*\MANIFEST.md`（包内另见 `PACK-MANIFEST.json` 的 `group`）。
**打包前必须**：重跑一次 + 核对体积 + 核对许可清单；缺一项不放行。

## 5 当前清单（2026-10-22，首次生成前）

| 组 | 文件 | 大小 | 许可 |
|---|---|---|---|
| UI 基线 | `design\prototype\index.html` | 121,140 B | 自有 |
| 核心 | `core\modelsmith_core.py`、`selftest_core.py`、`README.md` | ~160 KB | 自有（依赖 stdlib） |
| 适配器 | `adapters\laya_adapter.py`、`selftest_laya_adapter.py`、`llm_lora.py`（在建）、`aitk_adapter.py`（在建） | ~60 KB+ | 自有 |
| 数据包 01 | `resources\01-决策模型-业务语料\` | 8.25 MB | Apache-2.0 / CC BY 4.0 / MIT（逐条见 MANIFEST） |
| 文档 | `docs\`、`review\` | ~150 KB | 自有 |

## 6 运行时（主包内）待办

- Python embed 运行时：当前训练依赖 `E:\ComfyUI\python_embeded\python.exe`（torch 2.12+cu130）；分发不能依赖用户的 ComfyUI ⇒ 需要把「Python embed + torch/transformers/peft」打进主包（体积大，待估）。
- 训练适配器依赖：Laya 走源码目录（可打进主包 `third_party\laya` 或 pip 包）；ai-toolkit 需要独立 venv（transformers 5.5.3 冲突）。
- 打包前给体积预算与实测（未验）。
