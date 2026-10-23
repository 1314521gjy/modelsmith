# 客服决策数据集调研（情绪 / 意图 / 动作）

> 目的：为 Laya 客服决策模型补**真实、相关、许可清楚**的训练数据。现状 held-out 合计 **0.6497**（情绪 **51.72%** / 动作 **67.24%**），低于 Laya 官方基准（AG 0.953 / typed 0.766）。
> 证据等级：**A**=本机实测；**B**=官方卡片/仓库；**C**=第三方；**D**=推断/估算。**本次只查元数据，未下载任何数据集**。
> 调研时间 2026-10-23 16:2x。

## §1 本机已有语料盘点（A）

| 目录/文件 | 规模 | 字段/标签 | 许可（据本机 MANIFEST/卡片） | 覆盖信号 |
|---|---|---|---|---|
| `resources\01-决策模型-业务语料\typed-decisions\`（`all` 1,200/400；`customer_service` 300/100） | 2.1 MB | `state/questions(choice/score/noul)/gold 概率` | **Apache-2.0**（HF 卡） | 意图+动作（教师模型 gold，非真人） |
| `resources\01\massive-zh-CN\`（train 11,514 / val 2,033 / test 2,974） | 2.77 MB | `text` + `label_text_ch`（60 intents / 18 scenarios） | 本地卡片**未声明**；上游 Amazon MASSIVE **CC BY 4.0**（B，LICENSE 未取回） | **意图（中文）** |
| `resources\01\massive-en-US\` | 1.60 MB | 同上（英文） | 同上 | 意图（英文） |
| `resources\01\banking77\`（train 10,003 / test 3,076） | 2.17 MB | `text` + 77 类 intent | mteb 卡 **MIT** vs 上游 **CC BY 4.0** 冲突（取更严，需核） | 意图（英文银行） |
| `resources\01\zh-decision-bench\`（224 行） | 143 KB | 决策题（客服路由/紧急度/诈骗/升级） | **CC BY 4.0**（GitHub `CodyQin/zh-decision-bench`） | 意图+动作（中文小样本） |
| `datasets\cs-decision\`（train 649 / val 116） | 1.0 MB / 0.18 MB | `instruction/output{emotion,emotion_level,intent,action,reply}`（alpaca） | **不明**（自造/规则推 gold；`build_dataset.py`） | 情绪+意图+动作（**规则标签**，本次问题根源） |
| `datasets\laya-cs\`（train 671 / eval 118；含 24 补充行） | 2.0 MB / 0.35 MB | 三题：情绪(6)/目的(10)/动作(8)，`{"probabilities"}` gold=0.9 | 衍生自 `cs-decision`（同上不明） | 情绪+意图+动作（偏斜：动作「转人工」379/671=**56.5%**；目的「其他」214/671=**31.9%**；情绪「不满+焦虑」467/671=**69.6%**） |
| `E:\视频\拉亚决策模型\数据\样例-闸门.train.jsonl`（320 行） | 280 KB | `questions{意图(choice),要升级(noul),严重度(score)}` | **不明**（拉亚 0.3.5 离线包内） | 意图（英文样例） |

> 结论：本机**意图**信号相对多（MASSIVE/banking77/typed），**情绪**只有规则 gold，**动作**几乎全靠规则；缺的是**真人标注的动作/情绪**。

## §2 候选数据集表（只查元数据）

| 名称 | URL | 规模 | 字段/标签 | 语言 | 许可 | 覆盖 | 需申请 |
|---|---|---|---|---|---|---|---|
| **ABCD**（Action-Based Conversations Dataset） | https://github.com/asappresearch/abcd | 约 10k 对话（D；未下载核） | 对话 + **agent action**（API 调用序列） | 英文 | **MIT**（B，仓库 LICENSE） | **动作**（最稀缺） | 否 |
| **GoEmotions** | https://huggingface.co/datasets/google-research-datasets/go_emotions | 58k 评论 | 27 类情绪 + Neutral | 英文 | **Apache-2.0**（B，卡 README 明写） | **情绪**（真实 Reddit） | 否 |
| **MASSIVE zh-CN**（已在本机） | https://huggingface.co/datasets/SetFit/amazon_massive_intent_zh-CN | 11,514 train | 60 intents / 18 scenarios | 中文 | 卡未声明；上游 **CC BY 4.0**（B） | **意图** | 否 |
| **LocalLLaMA/typed-decisions**（已在本机） | https://huggingface.co/datasets/LocalLLaMA/typed-decisions | 1,200/400（customer_service 300/100） | 四 workflow + 教师 gold 概率 | 英文 | **Apache-2.0**（B） | 意图+动作（**非真人标签**） | 否 |
| **typesafeAI/evalsafe-customer-service** | https://huggingface.co/datasets/typesafeAI/evalsafe-customer-service | 204 cases / 3,287 question instances | cases+questions+runs；`actions/status/primary_action` | 英文 | **不明**（HF 无 license 标签） | **动作**（评测级） | 建议问作者 |
| **JDDC（京东客服对话）** | http://jddc.jd.com/auth_environment | 「百万级」对话（B/D，未核） | 原始客服对话（无决策标签） | 中文 | **不明/申请制**（B：仅参赛队可拿） | 原始中文客服（可做弱标注动作） | **是** |
| **SMP2019 ECDT** | https://conference.cipsc.org.cn/smp2019/evaluation.html | 训练/开发集（规模未公开） | 领域/意图/槽位 | 中文 | **不明**（仅参赛队） | 意图 | **是** |
| **THU-StarLab/CustomerService** | https://huggingface.co/datasets/THU-StarLab/CustomerService | 电信 QA 87,366 + 行业 QA 5,218 + 通用指令 18,123 | 问答对（**无**情绪/动作标签） | 中文 | **不明**（HF `license:unknown`，gated auto） | 领域语料，不是决策标签 | 是（gated） |
| **ChnSentiCorp**（镜像） | https://huggingface.co/datasets/seamew/ChnSentiCorp | 酒店评论（约 7k） | 正/负情感 | 中文 | **不明**（HF 无 license；`XiangPan` 镜像标 `other`） | 情绪（二分类代理） | 建议问作者 |
| **zh-decision-bench**（已在本机） | https://github.com/CodyQin/zh-decision-bench | 224 行 | 决策题 | 中文 | **CC BY 4.0**（B） | 意图+动作（小） | 否 |
| **banking77**（已在本机） | https://huggingface.co/datasets/PolyAI/banking77 | 10,003 train | 77 类 intent | 英文 | **CC BY 4.0**（B；mteb 卡写 MIT，冲突） | 意图 | 否 |

**已查无命中**：HF `search=jddc` → **0 条**；HF `search=customer service` 结果里除上表外多为**无 license/合成对话**（`FunDialogues/*` 等），未列入推荐；SMP2017/2018 客服意图本地 MANIFEST 记「**无许可证（0 命中）⇒ 不可再分发**」（`resources\01\MANIFEST.md:21`）。

## §3 推荐（3–5 个，按"能补什么"）

1. **ABCD（MIT，英文，动作）** — 唯一能直接补「动作」标签的公开集（agent action 序列）；需人工把它的动作映射到我们的 8 类（退款/补发/换货/补偿/转人工/拒绝…），**映射规则要单测**。体积：仓库数据压缩包**数十 MB 级（D，未下载核）**；下载方式 `git clone https://github.com/asappresearch/abcd`（只取 data，不取模型）。
2. **GoEmotions（Apache-2.0，英文，情绪）** — 真实 Reddit 评论，27→6 映射（愤怒/不满/失望/焦虑/平静/威胁投诉可近似合并），用于**情绪先验与校准**（不是中文客服语料，需注明）。体积约 **2–5 MB（D）**。
3. **MASSIVE zh-CN（CC BY 4.0，中文，意图）** — 已在本机（2.77 MB），直接扩「目的」标签分布；60 intents 需按我们的 10 类重新归并（映射表逐条审查）。
4. **typed-decisions / customer_service（Apache-2.0，英文，意图+动作）** — 已在本机（2.1 MB），格式与 Laya 一致（带教师 gold），可用于**校准与格式对齐**；但标签是教师模型生成，**不能当"真人动作"**。
5. **JDDC（需申请，中文，原始客服对话）** — 唯一可能拿到**真实中文客服对话**的源；无决策标签，可用「客服回复→动作」弱标注补动作；**先申请并核许可**，未获批**不进入训练集**。

> 不建议直接进训练：`evalsafe-customer-service`（许可不明，只做评测）、`THU-StarLab/CustomerService`（许可不明+gated）、`ChnSentiCorp`（许可不明）、SMP2019（参赛制）、`cs-decision`/`laya-cs`（规则 gold，即当前偏斜来源）。

## §4 许可与风险

- **可商用/可再分发（明确）**：ABCD **MIT**、GoEmotions **Apache-2.0**、typed-decisions **Apache-2.0**、MASSIVE 上游 **CC BY 4.0**、banking77 上游 **CC BY 4.0**、zh-decision-bench **CC BY 4.0**（均需保留署名/许可文本；CC BY 允许商用）。
- **许可不明（不直接进训练）**：evalsafe、THU-StarLab/CustomerService、ChnSentiCorp 镜像、`cs-decision`/`laya-cs`（自造，来源链未写许可）、`样例-闸门`。
- **申请制/参赛制（不可再分发）**：JDDC（仅参赛队，B）、SMP2019（参赛队）、SMP2017/2018（无许可证）。
- **风险**：① 英文集（ABCD/GoEmotions）迁到中文有**领域/语言错配**，只能做辅助/先验；② 动作映射（ABCD action→我们 8 类）若用规则，会重演"规则 gold"；③ CCD BY 4.0 要求**署名**（MASSIVE/banking77/zh-decision-bench），打包时需 NOTICE；④ 任何"许可不明"的数据进入训练前必须**书面确认**。

## §5 未验 / 需申请 / 查不到

- **JDDC**：只查到「仅参赛队可获取」（B），**未申请、未核许可**；规模「百万级」为第三方描述（C），本机未核。
- **SMP2019 ECDT**：只查到评测页（B），数据规模/许可**未公开**；未申请。
- **ABCD**：README/LICENSE 经第三方引用确认 **MIT**（B/C），**未下载数据**核字段与规模（D）。
- **GoEmotions**：许可 Apache-2.0 来自 HF 卡 README（B）；**未下载**，字段/体积未核（D）。
- **ChnSentiCorp**：多个镜像**均未声明许可**（`XiangPan` 标 `other`）；**不建议**直接进训练。
- **waimai_10k**：HF 搜索**超时未取回**（未验），未列入候选表。
- **typesafeAI/evalsafe**：动作/状态字段已见卡片描述（B），但许可**不明**；是否可训练**未核**（建议问作者）。
- 本机 `cs-decision` / `laya-cs` 的**数据来源链与许可**未在仓内文档写明（`datasets\laya-cs\README.md` 只写转换口径，未写来源许可）⇒ **按不明处理**。
