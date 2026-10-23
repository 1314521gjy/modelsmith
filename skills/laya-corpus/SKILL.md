---
name: laya-corpus
description: 把混合语料（客服对话、工单、多轮聊天、留言）加工成 Laya 决策模型能训练的复合 JSONL：一行 = 一个 state + 多个 question + 软标签 gold。当任务涉及"做训练语料 / 造数据 / 标 Laya 数据 / 把聊天记录变成训练集"时使用。
when_to_use: 用户要求生成、清洗、扩充、校验 Laya（决策模型）训练数据，或要求把客服/业务语料转成训练格式时。
---

# Laya 训练语料制造（复合格式）

Laya 是**非自回归决策模型**：它不写自由文本，只对「给定上下文 + 若干问题」输出**选一个 / 打分 / 要不要**。所以语料必须做成「一行 = 一个上下文 + 一组问题 + 每个问题的软标签概率」。

配套校验器：`python tools\validate_laya_corpus.py <file.jsonl>`（不通过会逐行报错）。

## 1 一行长什么样（严格结构）

```json
{"state": {"message": "你们重复扣了我两笔钱，我要退款"},
 "questions": {
   "意图":   {"type": "choice", "instructions": "客户想解决什么？",
              "criteria": {"refund": "要退钱/撤销重复扣款", "technical": "故障、宕机、集成问题", "sales": "咨询价格、套餐、演示"}},
   "情绪":   {"type": "score", "instructions": "客户情绪有多急？",
              "criteria": ["平静咨询", "有不满但可控", "愤怒/要投诉"]},
   "要升级": {"type": "noul", "instructions": "是否需要立即转人工？"}
 },
 "gold": {
   "意图":   {"probabilities": {"refund": 0.9, "technical": 0.05, "sales": 0.05}},
   "情绪":   {"probabilities": {"0": 0.1, "1": 0.3, "2": 0.6}},
   "要升级": {"probabilities": {"true": 0.7, "false": 0.3}}
 }}
```

硬约束（违反就训练报错或效果差）：

| 项 | 规则 |
|---|---|
| `state` | 字典；字段名自定（`message` / `history` / `order` / `ticket`…）；**整行 state 受底座长度限制**：标准版 512 tokens、题型版 1024；中文按 ~1 字≈1.5 token 估 |
| `questions` | 字典，可一次多个；键名即问题名（同一数据集内保持一致） |
| `type` | 只能是 `choice` / `score` / `noul` |
| `choice.criteria` | `{标签: 一句人话描述}`；**单题 ≤20 个标签**；标签名不要含空格/全角标点（用 `snake_case`） |
| `score.criteria` | **有序**档位数组（从低到高）；档位数 2–5 最佳 |
| `noul.criteria` | 不写 |
| `gold` | 与 `questions` **同键**；每项 `{"probabilities": {...}}`，**概率和必须 = 1**（容差 1e-3） |
| `gold` 的键 | choice→标签名；score→档位下标字符串（`"0"`,`"1"`…）；noul→`"true"`/`"false"` |
| 文件 | **单个** `.jsonl`，每行一个 JSON；不要目录、不要数组包起来 |

## 2 从混合语料造数据的流程

1. **清洗**：去掉空行、纯表情、系统提示（"客服已接入"）、PII（手机号/地址/身份证 → 用 `[PHONE]` 之类占位符替换，不要真值）。
2. **切 state**：一段对话/一个工单 = 一行。
   - 单轮：`{"message": "客户最后那句话"}`。
   - 多轮：`{"history": "客户: …\n客服: …\n客户: …", "order": "已付 199 元，重复扣款"}`；**把最关键的事实写进结构化字段**，不要一股脑塞进一个长字符串。
3. **设计问题**（见 §3 的客服模板）；每个问题都要能从 state 里**明确判断**，不许出"看图猜"的题。
4. **打软标签**：
   - 有真实处置结果（工单结案动作、是否退货、是否发红包）→ 用它做 0.85–0.95 主标签，其余按先验分摊；
   - 多人标注 → 用投票比例当概率；
   - 纯人工判断 → 主标签 1.0 也可以（但下游校准会把它当"确定"，慎用）。
5. **校验**：`python tools\validate_laya_corpus.py 你的文件.jsonl`，必须 `PASS`；有 `WARN` 要逐条看。
6. **抽样复核**：随机抽 20 条人工读，确认 `instructions` 指代的字段存在、标签能对上。

## 3 客服场景推荐问题集（可直接抄）

| 问题名 | 类型 | instructions | criteria 示例 |
|---|---|---|---|
| `意图` | choice | 客户这次想解决什么？ | `refund` 退款/撤销扣款；`negotiate` 协商补偿；`redpacket` 要红包/优惠；`technical` 故障/集成；`sales` 咨询价格套餐 |
| `情绪` | score | 客户情绪有多急？ | `["平静咨询","有不满但可控","愤怒/要投诉"]` |
| `动作` | choice | 客服最合适的下一步动作？ | `approve_refund` 直接退款；`counter_offer` 协商折中；`send_redpacket` 发红包安抚；`escalate` 转人工；`explain_only` 只解释 |
| `要升级` | noul | 是否需要立即转人工？ | — |
| `解释类型` | choice | 给客户的解释应该是哪一种？ | `dup_charge` 重复扣款 3–5 工作日原路退回；`policy` 说明规则；`need_order` 请提供订单号 |

> ⚠️ **Laya 不会写回复话术。** 「业务解释」只能落成**解释类型标签**，实际措辞由你的模板库按键渲染；要自由生成就接 LLM 线（`kind="lf"`），把 Laya 的输出当约束。

## 4 反例（别做）

- ❌ 一题一个文件、每行只有 `{"text": "...", "label": "..."}`（那是另一个模型的格式；Laya 要 `state/questions/gold`）。
- ❌ `gold` 写单个字符串或 `0/1`（必须是 `{"probabilities": {...}}`）。
- ❌ 概率和 = 0.9 / 1.1；score 下标从 1 开始；choice 标签大小写混用。
- ❌ 一个 choice 塞 50 个标签（>20 明显掉点，先做短名单或两级决策）。
- ❌ `instructions` 里说"看情况判断"，没指明看 `state` 的哪个字段。
- ❌ 把没处置结果的猜测标成 1.0，还混进训练集。

## 5 提交训练

- 训练入口：核心 `POST /api/jobs {"kind":"laya","params":{"data":"<该 jsonl 绝对路径>","base":"<底座目录>","out":"<输出目录>","epochs":1,"micro_batch":8,"grad_accum":5}}`。
- 底座要和语料语言匹配：中文/混合 → `Laya 多语言版`；英文 → 标准版；题型规整（固定候选集）→ 题型版。
- 先跑 1 epoch 看 `train_items / updates / 校准 before-after`，再决定加轮次（Laya **无 resume**，中断=从头）。
