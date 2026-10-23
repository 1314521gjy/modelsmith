# 训练经验手册（2026-10-23）

> 本文件是今天两条训练线（4B LLM LoRA / Laya 全参决策）的**实测经验台账**。
> 唯一出处：`E:\视频\modelsmith\docs\TRAINING-PLAYBOOK-20261023.md`；别处只许指向、不许抄。
> 下次先读 §7 检查单，再动手。

---

## 0. 一句话结论

**决策任务用 Laya（421M 全参，35 ms/条、2.3 GB 显存、1–2 分钟训完）；对话/话术任务才用 4B（Qwen3-VL + LoRA，3 秒/条、8.9 GB）。两条都通过 ModelSmith MCP 提交，实测跑通。**

---

## 1. 环境事实（踩过才知道）

| 项 | 事实 | 证据 |
|---|---|---|
| `C:\ModelSmith\runtime\python\python.exe` | torch 2.12.0+cu130；**无 torchaudio、无 pip** | 实测 `import torchaudio` → ModuleNotFoundError |
| `E:\ComfyUI\python_embeded\python.exe` | torch 2.12.0+cu130 + **torchaudio 2.11.0+cu130** | 实测 import 通过 |
| LLM 训练依赖 | `E:\视频\modelsmith\.pylibs-lf`（transformers 4.57.6 / peft 0.18.0 / accelerate） + `third_party\LlamaFactory` v0.9.5 | `adapters\lf_adapter.py:83-85` |
| Laya 代码（训练） | `third_party\laya` = **0.4.13**，有 `laya\train_cli.py`；入口 `laya-train = laya.train_cli:main` | `pyproject.toml:103` |
| Laya 离线部署 | `E:\视频\拉亚决策模型\`：laya **0.3.5**（vendored）+ 权重 `models\laya\`（803.6 MiB）+ `vendor\拉亚本地包.py` + `scripts\自训练*.py` | `说明书.md:3-4` |
| ⚠️ 同名不同物 | 0.3.5 = 本机 vendored 包；0.4.13 = GitHub 仓库。**权重与 HF LFS oid 逐字节一致（不用重下）**，缺的只是 0.4.13 代码 | `docs\research\LAYA-VERSIONS-20261023.md` |
| core 启动参数 | `--lf-python` 与 `--laya-python` **都必须指向 ComfyUI python** | 见 §6 坑 2 |

## 2. 两个 MCP 契约（提交训练用）

用 stdio JSON-RPC 调 `E:\视频\modelsmith\mcp_server\modelsmith_mcp.py --api http://127.0.0.1:8765 --token-file C:\ModelSmith\state\token`。

**kind=lf（4B LoRA）** 必填 `model / data / out / template`：
- `data` = **jsonl 路径**（不是数据集名）；core 会转 alpaca + 生成 dataset_info 到 job 私有目录（`core\modelsmith_core.py:3956-3996`）
- 数值键：`rank / alpha / lr / epochs / max_steps / batch / grad_accum / cutoff_len / logging_steps / save_steps`
- template：Qwen3-VL 用 **`qwen3_vl`**（`LlamaFactory/data/template.py:2091`）

**kind=laya（全参决策）** 必填 `data / out`，`base` 可选：
- 键：`base / loss(rlcd|soft-ce) / epochs / micro_batch / grad_accum / encoder_lr / head_lr / max_len / label_smoothing / target_error / eval_data`
- **`train_command` 被 MCP 硬拒**（测试钩子），别想绕

## 3. 数据格式

**LLM（alpaca）**：`{"instruction","input","output"}`；output 必须是严格 JSON 字符串。
- LlamaFactory 的 alpaca 列默认就是 `instruction/input/output`（`data/parser.py:51-53`）

**Laya（typed decision）**：每行
```json
{"state":{"message":"客户原话"},
 "questions":{"情绪":{"type":"choice","instructions":"...","criteria":{...}},
              "目的":{"type":"choice",...},"动作":{"type":"choice",...}},
 "gold":{"情绪":{"probabilities":{"愤怒":0.9,...}},"目的":{...},"动作":{...}}}
```
- gold 的 `probabilities` 和必须为 1，命中项给 0.9（其余按剩余分配）
- 只做判定、**不写话术**：数据里不要出现 reply/话术文本
- **先 `laya-train --dry-run`** 验 schema 与预算：它会打印 `valid items / optimizer budget / updates`

## 4. 实测读数（A 级，本机）

### 4.1 4B LoRA（`job-20261023-074144-7e4aa8c9`）
| 项 | 读数 |
|---|---|
| 训练 | 325 步（1 epoch × 649 行 / batch 2），train_loss **0.2581**，**2 分 46 秒**，1.96 it/s |
| 显存 | 峰值 **10.5 GiB** |
| 产物 | `adapter_model.safetensors` **126 MiB**（`_runs\cs-decision-lf-epoch1\`） |
| 合并 | `merge_and_unload` 7.5 s + save 4.7 s ⇒ `_runs\cs-decision-merged\` **8.89 GB**；重载 0.1 s；与 base+LoRA **token 逐字一致** |
| 评测（116 行 val） | 动作 **66.38%**（77/116）、情绪 **52.59%**（61/116）、macro-F1 0.473、JSON 合法率（剥 think 后）**100%**、白名单违规 0、负控 10/10、生成 386.5 s、峰值 8,724 MiB |

### 4.2 Laya（样例数据 `job-20261023-075455-1bdc033c`）
| 项 | 读数 |
|---|---|
| 训练 | 2 epochs / **54 次更新** / **79.14 秒** / loss 0.5088 |
| 评测 | `overlapping_eval`、**is_held_out=False**、231/300 重叠 ⇒ 0.72→0.8467 **不可外推** |
| 推理 | 加载 18.8 s；23 条 **0.8 s（≈35 ms/条）**；峰值 **2,330 MiB** |

### 4.3 Laya（我们自己的 765 行，`job-20261023-080343-f292fc26`）★
| 项 | 训练前 | 训练后 |
|---|---|---|
| 准确率（348 题 = 116 行×3 题，**held_out=True**） | 0.2874 | **0.6379** |
| loss | 5.7443 | 1.2119 |
| **ECE** | 0.2418 | **0.0850** |
| Brier | 0.7378 | 0.3608 |
| 训练 | — | 4 epochs / 112 次更新 / loss 1.37 / 约 1–2 分钟 |

## 5. 置信度这件事（必须懂）

- Laya 的 `confidence`（choice/score）= **1 − 归一化熵**（`third_party\laya\README.md:806`），不是温度概率。
- 权重里带**已拟合温度**：`rl_agent_config.json` 的 `temperature` / `temperature_by_options`；`说明书.md:323` 明确「改它即改置信度」。
- ⚠️ **`choice:11+` 桶未校准**：`说明书.md:291` 记录该档温度 0.1006 超界被 clamp ⇒ **选项数 ≥11 的问题，置信度不可信**。目的/动作要控制在 **≤10 类**。
- **分类器不能套 LLM 的 0.6 阈值**：6/8/10 类的均匀基线是 0.167/0.125/0.1；旧 checkpoint 在我们的新标签上只给 0.17–0.26。阈值必须**每题可配、默认关**，等分题概率分布出来再定。
- 显示口径：界面标 `confidence_source: "raw" | "calibrated"`；要校准就在**自己的留出集**上拟合温度（温度缩放，不改权重）。

## 6. 坑表（每条都真踩过）

| # | 坑 | 判据（怎么变红） | 修法 |
|---|---|---|---|
| 1 | `lf` 报 `NO_ROWS` | 日志 `lf preflight failed: NO_ROWS` | `adapters\lf_adapter.py` 原来只认 dolly 的 `response/context`；已改成兼容 alpaca `output/input`（selftest 25/0） |
| 2 | `ModuleNotFoundError: torchaudio` | 日志 traceback | core 的 `--lf-python` 指到了 runtime python（无 torchaudio）⇒ 换 ComfyUI python 重启 |
| 3 | MCP 提交报 `missing required param(s): data` | 提交回执 | lf 契约用 `data` 不是 `dataset`；系数键是 `rank/lr/epochs/batch/grad_accum` |
| 4 | 4B 输出带空 `<think>` 块 | 直接 json.loads 全失败（legal 0/116） | 解析前剥 `^\s*<think>.*?</think>\s*`；评测必须报“剥前/剥后”两套数 |
| 5 | `reply` 里“金额 0 元” | 模型输出原文 | prompt 没给金额 ⇒ 兜底 0；**不许服务端篡改 reply**，模板/业务侧补 |
| 6 | 样例数据 train/test 重叠 | 按 `sha256(state.message)` 去重 | 样例：train 320 / test 100 / **重叠 77 ⇒ clean 23**；任何 “84.67%” 这类重叠数不可外推 |
| 7 | 进度分母错（已修） | UI 显示 `2/2` 而日志 `325/325` | `core\modelsmith_core.py:4091-4104 / :4419-4434`，先取 tqdm 分母；selftest 237/0 带负控 |
| 8 | 重启核心中断训练 | `/api/jobs?state=running` 非空 | 先断言 running=0，**kill 与 start 写同一条命令**；adapter 改动只有重启后生效 |
| 9 | GPU 并发 | 两个进程同时占卡 | 同一时刻只一个 GPU 作业；跑前报“多少显存/多久”，跑完贴 `nvidia-smi` 证明显存回落 |

## 7. 下次训练的检查单（照抄执行）

1. `modelsmith_list_jobs` → 断言 `running=0`
2. 确认数据：`datasets\laya-cs\train.jsonl` / `eval.jsonl`（三题固定 `情绪 / 目的 / 动作`，十类目的，train/eval 零重叠）
3. `laya-train --dry-run` 过一遍，把 `valid items / optimizer budget / updates` 记下来
4. MCP 提交 `kind=laya`：
   ```json
   {"data":"...\\datasets\\laya-cs\\train.jsonl",
    "eval_data":"...\\datasets\\laya-cs\\eval.jsonl",
    "base":"E:\\视频\\拉亚决策模型\\models\\laya",
    "out":"...\\_runs\\laya-cs-<ts>",
    "epochs":4,"micro_batch":8,"grad_accum":8,"loss":"rlcd","max_len":512}
   ```
5. 轮询到终态，读 `<out>\train_report.json`：**必须 `is_held_out=true`**，否则数不算
6. 出**分题**（情绪/目的/动作）准确率 + ECE + 概率分布，再决定是否做温度校准/是否启用阈值
7. 只有第 6 步的数合格，才切展示台的后端 checkpoint

## 8. 未验 / 待办

- 目标 10 类目的的新数据集与重训（数据重建中）
- 分题准确率/ECE/概率分布（评测席进行中）
- `choice:11+` 桶的校准（Laya 自带温度在该桶被 clamp）
- 4B 路线：`think` 剥离已做、话术金额兜底未做；已封存不再投入
- Laya 在本机 4080S 上的 Router/多 checkpoint 常驻延迟（官方数是 T4）未测
