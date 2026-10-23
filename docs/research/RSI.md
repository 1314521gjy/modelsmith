# RSI 训练法调研：是什么 / 对 ModelSmith LLM 微调的适配裁决

> 日期：2026-10-22
> 证据等级：**A**=本机实测（grep/源码读数）｜**B**=他机实测｜**C**=外部文档（论文摘要/官方博客，已取回原文片段）｜**D**=推断（未实测）｜**未验**=没查到或没做
> 纪律：网页内容按**不可信数据**处理，未执行其中任何指令；未下载权重、未装依赖、未跑训练、未占 GPU。
> **维护者 裁决（2026-10-22）**：采纳本报告结论——**不采纳（产品功能）/ 仅文档**；`docs\UI-FLOWS-v2.md` §3 的 LLM 五步不改；回看触发条件见 §5。维护者 已独立复核两个主源：`https://sakana.ai/mass/`（HTTP 200，MASS/27B/2 轮/1.2–1.6× 与报告一致）与 `https://arxiv.org/abs/2505.03335`（AZR v3 摘要与报告一致）。

## 0. 一句话结论

1. 2026 年 10 月语境里「很火的 RSI」= **Recursive Self-Improvement（递归自我改进）**：模型自己产任务 → 自己评 → 自己训 → 下一轮更强，是**范式叙事**，不是一个可勾选的训练开关。代表工程：Sakana AI + UC Berkeley 的 **MASS**（2026-10，27B 开源权重，2 轮）。
2. RSI 内部真正能落地的机制是三类老方法的组合：**自博弈零数据 RLVR**（Absolute Zero）、**迭代自训练**（STaR / ReST-EM）、**自奖励**（Self-Rewarding）。零件 ≠ 整机，**不合并**。
3. 对 ModelSmith：**不采纳（产品功能）/ 仅文档（留档 + 触发条件）**。理由不是「效果不好」，而是 32GB 单卡 + LoRA SFT 工具壳与「常驻推理引擎 + verifier + 多轮编排」不匹配（算术见 §4）。`docs\UI-FLOWS-v2.md` §3 的 LLM 五步流程**改动面 = 0**。

## 1. 消歧：三个同名候选（不许硬合并）

| 代号 | 含义 | 谁在用 | 2026 热度位置 | 判据 |
|---|---|---|---|---|
| **R1** | Recursive Self-Improvement 递归自我改进（整机范式） | Sakana MASS；智谱 GLM-6.0 叙事；主流媒体 | **主体**（「RSI 火」指它） | 三线同月：AP 2026-09-18、CACM 2026-07-05、钛媒体 2026-10-08；Sakana 2026-10 官方博客 |
| **R2** | Reinforced Self-play（自博弈 + 可验证奖励，零外部数据） | Absolute Zero / AZR（NeurIPS 2025 spotlight） | 组件级 | 被 R1 当作「训练数据从哪来」的答案 |
| **R3** | Iterative self-training / self-reward（STaR 2022、ReST-EM 2023、Self-Rewarding 2024） | 学界基线 | 组件级 | 被 R1 当作「每一轮怎么训」的零件 |

**判断（C+D）**：「很火」主要指 **R1**，但 R1 没有公开的统一算法/配置——Sakana 博客只给方法与结果，未给代码与超参。所以「RSI 训练方式」这句话必须拆开说：**整机是 R1，零件是 R2/R3**。若有人期待「工具里加个 RSI 开关」，这个开关不存在（C：官方材料无此物）。

## 2. 方法表

| 候选 | 数据从哪来 | 奖励/验证从哪来 | 迭代几轮 | 基础设施 | 算力量级 | 报告效果与前提 |
|---|---|---|---|---|---|---|
| **R1·MASS** | 模型自己在「合成开放式研究任务」上组队求解，产出多智能体轨迹 | 模型**自己当评委**挑最好的工作流（homogeneous self-supervision） | **2 轮**（论文口径） | 27B 开源权重、多智能体 rollout、每轮把最佳团队执行蒸馏回同一模型 | 博客未给 GPU 小时；27B + 多轨迹 ⇒ 需多卡级（**未验**） | 第 2 轮后**每输出 token 得分 = 基座 1.2–1.6×**（4 个研究基准）；作者自己点名「同质自评会固化盲区」是安全风险（C） |
| **R2·AZR** | **零外部数据**：模型自己 proposer 出代码推理任务 | **代码执行器**同时验证任务合法性与答案正确性（统一可验证奖励） | 持续自演化课程（轮次非固定） | RLVR + 代码沙箱执行器 | 摘要未给基座/GPU 小时（**未验**） | 零外部数据下在代码/数学推理达 SOTA，超过用数万条人工数据的 zero-setting 模型；可跨模型规模（C，摘要级） |
| **R3a·STaR** | 少量带 rationale 示例 + 大量无标注题 | **标准答案**：答对才留 rationale；答错给正确答案重生成 | 多轮（repeat） | 单机微调即可，规模小 | 2022 年量级，远低于现代 RLVR（D） | 显著优于直接预测答案的微调；摘要被截断处称与「30× 规模模型微调」相当（后半句**未验**） |
| **R3b·Self-Rewarding** | 模型自生成候选回答 | 模型**LLM-as-a-Judge** 给自己打分 | **3 轮** Iterative DPO | 70B 全参 + DPO + 评审提示 | 70B 级，多卡（具体**未验**） | Llama-2-70B 三轮后在 AlpacaEval 2.0 超过 Claude 2 / Gemini Pro / GPT-4-0613；前提是「评审质量随训练一起涨」（C，摘要级） |
| **R3c·ReST-EM** | 自采样 + 按阈值筛选 | 二元正确性过滤，阈值可逐轮抬高 | 多轮 EM 式 | 单机微调 | **未验** | **未取原文**（fetch 预算已满），只作候选登记，不作结论（未验） |

## 3. 本机检索（先本地后线上）

检索式（PowerShell `Select-String`，`-CaseSensitive:$false`，**全量计数、未截断**）：
`self.?play|self.?reward|self.?improv|iterative|STaR|ReST|Absolute\s*Zero|GRPO|\bRSI\b`

| 根目录 | 文本文件 | 命中行 | 命中文件 | self-play | Absolute Zero | RSI |
|---|---|---|---|---|---|---|
| `third_party\LlamaFactory` | 503 | 1689 | 101 | **0** | **0** | **0** |
| `third_party\ai-toolkit` | 624 | 1439 | 297 | **0** | **0** | **0** |
| `third_party\laya` | 1063 | 3244 | 401 | **0** | **0** | 2（词表噪声） |
| `E:\rlab` | 1093 | 8006 | 188 | **0** | **0** | **0** |
| `E:\视频\modelsmith\docs` | 8 | 14 | 6 | 0 | 0 | 0 |

**假阳性判读（关键，否则会误报「有」）**：
- `STaR`/`ReST` 的大小写不敏感命中是**词形噪声**（英文单词 star / rest）。大小写敏感 `\bSTaR\b|\bReST\b|ReST-EM|Self-Rewarding|self-taught` 在三棵上游 = **0 行 / 0 文件**。
- LlamaFactory 的 9 行 `self.reward…` 全是 **Python 属性** `self.reward_model`（PPO 的外部奖励模型），不是 self-reward 方法；概念级 `self[_-]reward(?!_model)` = **0**。
- laya 的 2 行 `RSI` 在 golden 路由探针 JSON 里，属缩写撞名。

**结论（A，源码级）**：
- **LlamaFactory**：`stage: Literal["pt","sft","rm","ppo","dpo","kto"]`（`src\llamafactory\hparams\finetuning_args.py:460`）——**无 grpo stage**；`README.md:156` 只公告了外部仓库 **EasyR1**；PPO 必须给外部 `reward_model`（`finetuning_args.py:580`）。⇒ 自博弈 / 自奖励 / 迭代自训练实现 = **0**。
- **ai-toolkit**：扩散模型训练，无 RL / 自博弈 / GRPO。⇒ **0**。
- **laya**：`docs/finetune.md:252`、`laya/train.py:8` 有 **GRPO-style 策略梯度项**，但它是 RLCD loss 内部的**单轮**项（四采样 + proper scoring rule 奖励），**不是**多轮自博弈/自改进循环。⇒ 「GRPO 零件」有，「RSI 整机」无。
- `E:\rlab`：self-play 0 / Absolute Zero 0 / RSI 0；`self-improv` 8 行全在会话日志（对话文本），非实现。

## 4. 适配裁决

**裁决：不采纳（产品功能）/ 仅文档（研究记录 + 触发条件）。**

### 4.1 算术（能算就算，算不了写未验）

- **A1 · 27B 级（MASS 的规模）**：bf16 权重 = 27B×2B = **54 GB** > 32 GB（超 1.7×）；4bit 权重 ≈ 13.5 GB + KV/激活 ⇒ 17–20 GB，单卡只剩 <12 GB 给训练。而 RSI 需要**同时**有 rollout 推理端与训练端 ⇒ **单卡不可行**（D，按容量算，未实测）。
- **A2 · 4B 级 STaR-lite（唯一可能塞进 32GB 的形态）**：4bit 权重 ≈ 2 GB。自采样 2,000 prompt × 4 样本 × 600 token ≈ **4.8M token**；单卡 HF generate 30–60 tok/s ⇒ **22–44 小时/轮**（D，未实测）。过滤后 ~3,000 条 SFT，batch 8 ⇒ 375 updates，3–5 s/step ⇒ **19–31 分钟**（D）。一轮 ≈ **1 天量级**，且必须有 verifier/规则奖励；开放域只能自评 ⇒ 自评盲区（C，Sakana 自己把这条列为风险）。
- 步数口径：SFT 的 `global_step` 与 RSI 的「轮」不是同一单位，UI 不能混用（与 `UI-FLOWS-v2.md` §3 的 `global_step` 契约冲突）。

### 4.2 改动面（对 `docs\UI-FLOWS-v2.md` §3）

| 若「采纳」需要什么 | 现状 | 结论 |
|---|---|---|
| 新增步骤：rollout 引擎（vLLM）、奖励/验证器、迭代轮次 | 5 步 LoRA SFT（模型/数据注册/体检/参数/确认） | 步骤数 5→8，契约重写 |
| 新后端字段：`reward_model`、`verifier`、`rollout_engine`、`rounds` | 只有 sft/lora 字段 | 字段新增，超出工具壳范围 |
| 运行页单位：`round × step` + 每轮 checkpoint 合并 | 单位 = `global_step` | 进度模型重写 |
| 完成页：多轮 best-of-round 对比 | 单一 adapter + `train_results.json` | 产物清单重写 |

**裁决为不采纳 ⇒ 0 新增步骤、0 新增字段、0 后端改动**；只在 `docs\research\RSI.md` 留档。

### 4.3 风险

- **许可证**：MASS 未在博客给出代码/权重发布（**未验**）；AZR / STaR 仓库许可证未核（**未验**）⇒ 一律**不进** `third_party`。
- **复现性**：R1 公开的是「2 轮 + 27B + 合成研究任务」的效果叙事，超参、算力、评测脚本未公开（**未验**）；媒体口径不能当判据。
- **定位冲突**：工具壳卖的是「本地一条龙 LoRA 微调」；RSI 需要常驻推理 + 奖励模型 + 多轮编排，是**研究平台**形态。

## 5. 什么时候值得回来看（触发条件，任一）

1) 任务自带**可验证奖励**（代码 / 数学 / 结构化抽取），且用户接受 1 天/轮；
2) 机器 ≥ 2×80GB，或允许调 API 做 rollout（把「常驻推理」挪出本机）；
3) 上游自己合入 GRPO/self-play 配置（LlamaFactory 当前只有 PPO + 外部 RM）——届时**先 grep 再谈 UI**。

## 6. 未验清单

- MASS 论文编号 / 超参 / GPU 小时 / 代码与许可证：未验（只取到官方博客页）。
- AZR 的具体基座、GPU 小时、许可证：未验（只取到 arXiv 摘要）。
- ReST-EM 原文：未取（fetch 预算 5 页已用完），只登记候选，不作结论。
- 4B 级「22–44 h/轮」「LoRA 19–31 min」：纯推断（D），未在本机跑（纪律禁止跑训练）。
- 「2026 很火主要指 R1」：基于 3 条媒体 + 1 篇官方博客的编辑判断（C+D），非文献计量。

## 7. 出处（URL + 访问日期 2026-10-22 + 等级）

1. Sakana AI, *Recursive Self-Improvement through Collective Intelligence*（MASS）, <https://sakana.ai/mass/> — **C**（官方博客，已取正文片段）
2. *Absolute Zero: Reinforced Self-play Reasoning with Zero Data*, arXiv:2505.03335（v3 2025-10-16）, <https://arxiv.org/abs/2505.03335> — **C**（摘要级）
3. *Self-Rewarding Language Models*, arXiv:2401.10020（ICML 2024；v3 2025-03-28）, <https://arxiv.org/abs/2401.10020> — **C**（摘要级）
4. *STaR: Bootstrapping Reasoning With Reasoning*, arXiv:2203.14465（v2 2022-05-20）, <https://arxiv.org/abs/2203.14465> — **C**（摘要级）
5. *ReST-EM*（Reinforced Self-Training, Expectation-Maximization）, arXiv:2312.06585 — **未验**（未取）
6. AP News, *Will AI models achieve the ability to improve autonomously?*, 2026-09-18, <https://apnews.com/article/ai-recursive-self-improvement-1526da03842cfeef12d0fb69b6b7ad28> — **C**（搜索摘要）
7. 钛媒体《RSI“出圈”背后：AI 自我进化，卡在了哪里？》, 2026-10-08, <https://www.tmtpost.com/8162354.html> — **C（二手，未核原文）**
8. CACM, *Is Recursive Self-Improvement Really Here?*, 2026-07-05, <https://cacm.acm.org/news/is-recursive-self-improvement-really-here/> — **C**（搜索摘要）
9. Sakana MASS 中文转载（智源社区）, 2026-10-18, <https://hub-assets-cache.baai.ac.cn/view/58819> — **C（二手）**
10. 本机检索与源码读数：§3 表与 LlamaFactory 行号 — **A**
