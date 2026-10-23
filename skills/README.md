# skills/ —— 给 Agent 用的操作知识

这些不是给人读的长文档，而是**Agent 完成任务时该走的流程**：每个目录一份 `SKILL.md`（带 frontmatter：`name` / `description` / `when_to_use`），按需加载。

| 目录 | 什么时候用 | 交付物 |
|---|---|---|
| `laya-corpus/` | 要做/清洗/校验 **Laya 决策模型训练语料**（把客服对话、工单、多轮聊天变成 `state + questions + gold` 复合 JSONL） | 合规 `.jsonl` + 校验通过 |
| `modelsmith-mcp/` | 要用 **MCP 接口驱动 ModelSmith 训练**（列模型/提交细参数/轮询/取消/读产物，含欠拟合与过拟合判断） | 训练 job + 产物 + 读数报告 |
| `frontend-design/`（在 `design/skills/`） | 做 UI/视觉 | 设计资产 |

配套工具：`tools\validate_laya_corpus.py`（Laya 语料校验器，零依赖）。

调用约定（给 Agent）：
1. 先读 `SKILL.md` 的 frontmatter 判断是否命中；
2. 按 `## 流程` 执行，产物落盘后**必须跑校验器**；
3. 校验 `PASS` 才算完成；`WARN` 要逐条在回复里点名。
