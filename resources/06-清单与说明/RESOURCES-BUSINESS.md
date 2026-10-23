# 业务端语料与 JEV 侦察（客服/意图）

> 日期：2026-10-22 ｜ 只找+核许可，**未下载任何数据**、未装依赖、未跑 GPU。
> 证据等级：**A**=本机自测/本机文件原文 ｜ **A(源码)**=上游仓库原文 ｜ **B**=两处以上二手一致 ｜
> **C**=第三方网页单一来源 ｜ **D**=按行数推算的估值 ｜ **未核**=本轮没取回源站原文。
> 预算：`web_search` 2 次（8 条 query）、`web_fetch` 8 次（4 成 4 败，失败见 §F）。

---

## A. JEV 是什么、有没有公开语料（先答这个）

**一句话**：Jev 是 **TypeSafe 的托管决策 API**（产品名 System One），**不开源、不发权重、不发语料**；
我们唯一能用的是它**公开发布的分数**，不是它的数据。

| 问题 | 答案 | 出处 |
|---|---|---|
| 托管还是开源 | **托管 HTTP API**，要 API key（`TYPESAFE_API_KEY`），客户端可改 `JEV_ENDPOINT` | hs-jev README（A 源码，web）；`L/README.md:735-743` |
| API 形状 | `POST /v1/systemone`：state 进，`choice`/`score`/`noul` 三类题出，回包带 `{input_tokens, output_tokens}` usage；批量走 `/v1/systemone/batch`（≤64 states） | `L/README.md:737-743,758-771` |
| `jev-1` | 是**模型别名**；在 Laya 里等价于「按脚本/语言自动选路」 | `L/README.md:789` |
| 公开权重 | **无**（无 checkpoint 可下） | `L/README.md:735-743`；hs-jev 仓库无权重目录 |
| 公开训练/评测语料 | **未发布**（0 命中） | 见下 |
| 谁在用它的名字做题 | 第三方 `jev-benchmarks`(AbdelStark)、`decision-model-benchmark`(nibzard)、`browser-use/jev-ultrafast` —— 都是**用公开数据集跑 Jev**，不是 Jev 放数据 | `L/README.md:1491-1494,1785-1786` |

**「未发布 + 检索式」记录（2026-10-22）**
- 检索 `TypeSafe Jev dataset corpus training data` → 0 条指官方数据集页；返回的是 Jev 托管 API 指南（第三方 [jevmodel.org](https://jevmodel.org/jevbench/jevbench/)，称 **托管 API 自 2026-09-21 起公开、$0.042/百万输入 token、输出免费** —— 单一第三方源，**C 级**，官方定价页未核）。
- 抓取 [github.com/getmissionctrl/hs-jev](https://github.com/getmissionctrl/hs-jev) 全页：只有 `src/`（wire types/Ask/Client/Patterns）、`example/`（support-triage、spam-check、resume-screen 三个**文档示例**）、`test/`；**无 data/ 目录、无语料、无权重**（A 源码）。
- 本机 `E:\` 顶层 `jev` 目录 0 命中（Lead 已验）。

**结论**：拿不到 Jev 的数据；能做的是「**同一批公开数据集 + 对拍它公布的分数**」。
可用于对拍的 Jev 已公布数字（均出自 Laya 的对照表，转引，B 级）：
typed-decisions 0.727 / Banking77 0.870 / AG News 0.910 / DAIR Emotion 0.480（`L/BENCHMARKS.md:43-49,165-168`）。

---

## B. Laya 官方基准用的数据集（最稳的一手）

| # | 数据集 | 用于 | 源站 URL | 许可 | 规模 | 语言 | 客服意图贴合度 |
|---|---|---|---|---|---|---|---|
| B1 | **`LocalLLaMA/typed-decisions`** | Laya 官方**微调主集 + 主榜**（`laya-typed-decisions`） | https://huggingface.co/datasets/LocalLLaMA/typed-decisions （config `all`） | **未核**（HF 源站，商用前必核；HF raw 端点本机 404） | train 1,200 cases / **6,000 decisions**；test 400 cases / **2,000 decisions** | en | ★★★★★ 四个 workflow 全是业务决策：Agent Trace Observability、**Customer Service**、**Invoice Processing**、**Security Incidents**（`L/docs/finetune.md:303-305`；`L/research/scripts/step4_benchmark/README.md:35-37`） |
| B2 | **Amazon MASSIVE** | 意图榜（20 选项）、51 语言扫描、option-order 鲁棒性 | https://github.com/alexa/massive ；S3：`amazon-massive-nlu-dataset.s3.amazonaws.com/amazon-massive-dataset-1.0.tar.gz` | **CC BY 4.0**（B 级：HF 卡 + SEACrowd issue + 本机派生物头注三处一致；source LICENSE.txt 未取回） | 1.0=**51 语言**（1.1 加 Catalan=52）；单语言 11,514/2,033/2,974=**16,521 句**（HF 卡读数）；全量 **>1M 句**；60 intents / 55 slots；**JSONL**（`utt` 字段） | 51 语言（含 zh-CN） | ★★★ 虚拟助手语料，意图粒度是「alarm_set」这类，不是客服工单 |
| B3 | **Banking77** | 77 选项压力测试（暴露 head 预算上限） | https://github.com/PolyAI-LDN/task-specific-datasets | **CC BY 4.0**（A 源码：仓库 LICENSE 提交即 “Update licence to CC-BY-4.0”） | 10,003 train / 3,080 test = **13,083**；77 intents；CSV | en | ★★★★ 银行客服，最贴「业务端分诊」 |
| B4 | **CLINC150**（`clinc/oos-eval`） | 150 意图 + **OOS 拒答** | https://github.com/clinc/oos-eval | **未核**（仓库有 LICENSE，本轮两次 raw 抓取均 404） | 150×(100/20/30) + OOS 100/100/1000 ≈ **16,200**；`data/data_full.json` | en | ★★★★ 含 out-of-scope，正好对应「拒答」这一档 |
| B5 | XNLI | 15 语言 NLI、校准曲线 | （Laya 表内引用） | 见源站（未核） | 15 语言 | 15 语言 | ★ 不是意图任务，**不进业务语料** |
| B6 | AG News / DAIR Emotion | 应用工作流榜（4/6 类） | （Laya 表内引用） | 见源站（未核） | 各 400–1,000 样本 | en | ★ 内容分类，不是业务路由 |
| B7 | **`zh-decision-bench`（本机已带！）** | 中文业务场景评测，**laya-evals 格式现成** | 本机 `third_party/laya/research/evals/zh_decision_bench.jsonl`（146,706 B）＋ 上游 https://github.com/CodyQin/zh-decision-bench | **CC BY 4.0**（本机文件头 1-3 行自述：MASSIVE zh-CN 179 条 + 合成 40 条，均 CC BY 4.0；`L/BENCHMARKS.md:509`） | **219 items / 284 questions**；含 voice routing 179 / CS routing 25 / urgency 25 / scam 15 / escalate 40 | zh-CN | ★★★★★ 中文客服路由 + 紧急度 + 诈骗 + 升级，**零下载** |
| B8 | `feishu_zh`（本机） | 飞书式中文字职场决策（合成 + 归档 Laya/Jev 双跑） | `third_party/laya/research/benchmarks/feishu_zh/` | **MIT**（`README.zh-CN.md:54`） | `data/cases.jsonl` 22,994 B | zh-CN | ★★★★ 企业内决策，量小（评测级） |
| B9 | `zh_short_commands`（本机） | 18 条中文短指令的 7 档消融 | 同上目录 | 见其 README（未核 SPDX） | `data/cases.jsonl` 1,858 B | zh-CN | ★★ 评测专用 |

**注**：B1 是唯一「自带教师概率（`gold`）」的公开集，这决定 RLCD 能否拿到分布信号；其余集只有硬标签（转写见 §D）。

---

## C. 业务/客服语料候选（含中文）

| 语料 | 语言 | 源站 URL | 许可（只认源站） | 规模 | 格式 | 可商用/可分发 | 转 Laya 难度 |
|---|---|---|---|---|---|---|---|
| **MASSIVE** | 51 语言（含 zh-CN） | https://github.com/alexa/massive | **CC BY 4.0**（B 级） | 16,521 句/语言 × 51 | JSONL | ✅ 可（署名） | **S**（字段直搬） |
| **CLINC150** | en | https://github.com/clinc/oos-eval | **未核**（有 LICENSE 文件，未取回正文） | ~16,200 句 | 单 JSON | ⚠️ 核完再用 | **S**（+OOS 处理） |
| **Banking77** | en | https://github.com/PolyAI-LDN/task-specific-datasets | **CC BY 4.0**（A 源码） | 13,083 句 | CSV | ✅ 可（署名） | **M**（77 选项超 head 预算） |
| **HWU64** | en | https://github.com/xliuhw/NLU-Evaluation-Data | **CC BY 4.0**（A 源码 README 明写；LICENSE 由 CC BY-SA 3.0 改为 CC BY 4.0） | 25,716 行标注（home 域，64 intents） | CSV（**分号分隔**） | ✅ 可（署名） | **S**（两层：scenario+intent） |
| **Snips NLU 2017** | en | 原 repo [snipsco/nlu-benchmark](https://github.com/snipsco/nlu-benchmark)（现以存档为准） | **CC0 1.0**（C 级：Software Heritage 存档 LICENSE 全文） | >16,000 句，7 intents | JSON | ✅ 可（CC0 无约束） | **S** |
| Rasa NLU demo | en | https://github.com/RasaHQ/rasa-demo | **未核**（本轮 fetch 预算用尽） | 未核 | YAML/MD | ⚠️ | S |
| **SMP2017/2018-ECDT** | zh | https://github.com/HITlilingzhi/SMP2017ECDT-DATA ；往年数据另见 https://github.com/ygwpz/SMP2019-ECDT-NLU（含 `往年数据.zip`） | **未标明（0 命中）** —— 竞赛数据，无 SPDX | SMP2017 训练集约 2,299 句起（片段）；18 年同源 | 官方格式（非标准） | ❌ **不可分发**，仅内部研究 | **M**（要先核许可 + 自定义解析） |
| SMP2019-ECDT-NLU | zh | https://github.com/ygwpz/SMP2019-ECDT-NLU | 未标明（同上） | 未核 | 竞赛格式 | ❌ | M |
| **CLUE** | zh | https://www.cluebenchmarks.com/ ／ HF `clue/clue` | 各子集不一（未核） | — | — | — | **不适用**：CLUE **没有意图任务**；最接近的是 IFLYTEK（应用描述 119 类）与 TNEWS（15 类），是**主题分类**不是意图 |
| 中文客服对话（通用） | zh | — | — | — | — | — | **0 命中**：检索 `中文 客服 对话 数据集 意图 许可`（2026-10-22）未找到带明确许可证的开放客服对话集；公开的多为竞赛/爬取，**不可分发** |

---

## D. 转 Laya JSONL 的字段映射草图

目标行（A 源码：`L/research/evals/dataset.template.jsonl:1-7`；训练同 schema，`L/docs/finetune.md:52-69`）：

```json
{"state":"我被重复扣费了，请退款","questions":{"intent":{"type":"choice","instructions":"用户想要什么？","criteria":{"billing":"退款或扣费","technical":"故障或报错"}}},"expected":{"intent":"billing"},"tags":["billing"],"language":"zh"}
```

| 来源 | `state` ← | 题 | `expected` ← | 备注 |
|---|---|---|---|---|
| MASSIVE | `utt` | 单个 `choice`，60 选项（`criteria` 可用 intent 描述） | `intent` | `tags`←`[scenario, locale]`；60 选项在默认 `head_max_len=256` 下每项仅 ~3–4 token（`L/README.md:1627`），要么抬 `head_max_len` 要么先分 scenario |
| CLINC150 | `text` | `choice`(150) **或** `noul`(是否 in-scope) | `intent` / `true|false` | 建议 OOS 走 `noul`，拒答阈值才可校准（`L/docs/finetune.md:171`） |
| Banking77 | `text` | `choice`(77) | `label` | 77 选项实测掉点（0.425 vs Jev 0.870），用 `predict_tournament` 或先粗分桶可回到 ~0.610（`L/README.md:1621-1631`） |
| HWU64 | `answer` | **两题**：`domain` choice + `intent` choice | `scenario` / `intent` | 分号 CSV，注意 `;` 分隔 |
| SMP/ECDT | 原句 | `domain` + `intent` 两题 | 原标注 | 中文语料建议用中文 `instructions`（对精度的影响**未验**）；注意 confidence 口径随选项数变（`L/README.md:806`） |
| typed-decisions | `state` | 已是 Laya 题 | `gold`（教师分布） | **唯一带 gold 的集**，可直接训 `rlcd` |
| 其余（硬标签） | 原句 | 单/双 choice | one-hot | 只有硬标签时 `target_from_expected` 生成目标；`rlcd` 的「教师分布」信号缺失，建议先 `--loss soft-ce` |

---

## E. 首选与备选（含下载总量）

| 方案 | 组成 | 下载量 | 依据 |
|---|---|---|---|
| **首选（业务落地，最贴客服）** | ① `LocalLLaMA/typed-decisions`（训练+评测主集）② 本机 `zh_decision_bench.jsonl`（中文，**0 下载**） | ① **未核**（1,600 cases 级，估 <50 MB，D 级）② 0 | B1/B7；typed-decisions 四 workflow 含 Customer Service / Invoice / Security |
| **备选（规模化 + 多语言 + OOS）** | MASSIVE（只取 zh-CN + en-US）+ CLINC150 + Banking77 | 16,521 + ~16,200 + 13,083 ≈ **4.6 万句**；估 **30–80 MB**（D 级，字节未核） | B2/B3/B4 |
| 中文备选 | SMP2017/ECDT（仅内部研究，不可分发） | 未核 | §C |
| **建议本轮下载上限** | 首选 1 个 HF 集 + 备选 3 个文本集 | **≤ 100 MB 量级**（D 级） | 均为文本，无音频/图像 |

**许可红线**：本表把「**未核**」单列，不按二手当已核。Alpaca 的教训（文档自称开放、实为 CC BY-NC）在这里的对应动作是：**只用源站 LICENSE 文本判可商用**，B1/B4/SMP 三个未核项在核完前不得进分发物。

---

## F. 未验 / 0 命中清单（2026-10-22）

1. **CLINC150 许可证正文未取回**：`raw.githubusercontent.com/clinc/oos-eval/master/LICENSE` 与 `huggingface.co/datasets/clinc/clinc_oos/raw/main/README.md` 均 **HTTP 404**（后者经本机 fetch 端点返回 404，疑端点不支持 HF raw）。检索式：`CLINC150 dataset license clinc oos-eval github`。
2. **MASSIVE 源站 LICENSE.txt 未取回**（只取到仓库 README 的「See LICENSE.txt」）；CC BY 4.0 为 **B 级**（HF 卡 + SEACrowd issue + 本机派生物头注）。
3. **`LocalLLaMA/typed-decisions` 的许可、体积、是否含真实客户 PII 未核**（HF 页未抓）。
4. **中文 SMP2017/2018-ECDT 无明确许可证（0 命中）**：检索 `SMP2017 SMP2018 ECDT 客服 意图 数据集 下载`，只得到竞赛仓库与「往年数据.zip」入口，无 SPDX。
5. **中文通用客服对话语料 0 命中**（带可商用许可证的开放集）。
6. **Rasa NLU demo 许可未核**（fetch 预算 8/8 用尽）。
7. **Jev 官方定价/文档页未核**：只有第三方 jevmodel.org 称「2026-09-21 公开、$0.042/M input、output free」（C 级）。
8. **HF raw 端点在本机 fetch 通路不可用**（2 次 404）——下一轮优先改用 `github.com` 页面或 `hf.co/datasets/<id>` HTML 页。
9. 未下载任何数据；以上所有「体积」除本机文件（按字节给出）外均为 **D 级估值**，不是读数。
