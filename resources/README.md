# ModelSmith 资源库（`E:\视频\modelsmith\resources\`）

> 用途：把「训练要用的语料 / 模型 / 图集」集中到一处，按训练类型分类，拿来即用。
> 原则：**下载来的放这里；本机已有的大件（底座权重、ComfyUI 模型）只放指针，不复制**（避免多份 800MB 级副本）。
> 建立日期：2026-10-22 ｜ 总量：31 文件 / 8.27 MB（不含指针指向的大件）

## 目录

| 目录 | 内容 | 状态 |
|---|---|---|
| `01-决策模型-业务语料\` | Laya 官方基准集 + 客服/意图语料（中英） | ✅ 已就绪（5 套，8.27 MB） |
| `02-LLM-语料\` | 指令/对话 SFT 语料 | ⬜ 待下载（首选 dolly-15k 12.5 MB、备选 OASST1 220.8 MB） |
| `03-LLM-小模型\` | HF 格式可训练小模型 | ⬜ 待下载（首选 Qwen2.5-0.5B-Instruct 0.93 GB、备选 SmolLM2-360M 690 MB） |
| `04-生图LoRA-图集\` | 图片 + caption 图集 | ⬜ 待下载（首选 iceland-photos 505 MB、备选 japanese 分片 453 MB） |
| `05-视频LoRA-图集\` | 视频 + caption | ⛔ 线上 0 干净命中（详见 `06-清单与说明\RESOURCES-ONLINE.md` §4） |
| `06-清单与说明\` | 两份线上资源清单副本 | ✅ |
| `00-底座与权重-指针.md` | 本机已有大件的路径指针 | ✅ |

## 01 决策模型-业务语料（已就绪）

| 目录 | 来源 | 许可证 | 行数 | 说明 |
|---|---|---|---|---|
| `typed-decisions\` | HF `LocalLLaMA/typed-decisions` | **Apache-2.0** | 训练 1,200 / 测试 400；客服 300/100、发票 300/100、安全 300/100、agent-trace 300/100 | **Laya 官方基准集**，唯一带教师 `gold`；四 workflow 里三个是业务 |
| `massive-zh-CN\` | HF `SetFit/amazon_massive_intent_zh-CN` | 上游 **CC BY 4.0**（rehost 未声明） | 11,514 / 2,033 / 2,974 | 中文客服意图 60 类 |
| `massive-en-US\` | HF `SetFit/amazon_massive_intent_en-US` | 同上 | 同 zh-CN（平行翻译） | 英文客服意图 |
| `banking77\` | HF `mteb/banking77` | mteb 卡 **MIT** / 上游 **CC BY 4.0**（取更严，注明） | 9,993 / 3,076 | 77 类银行客服，长标签压力测试 |
| `zh-decision-bench\` | 本机 `third_party\laya\research\evals\zh_decision_bench.jsonl`（源 GitHub `CodyQin/zh-decision-bench`） | **CC BY 4.0** | 224（含 1 行注释头） | 中文客服路由/紧急度/诈骗/升级，零下载 |

- 详细清单（体积/字段/未下载项）：`01-决策模型-业务语料\MANIFEST.md`
- **两条坑**：① MASSIVE/Banking77 是硬标签、无 `gold` ⇒ 只能 `target_from_expected`，建议先 `--loss soft-ce`；`typed-decisions` 有 `gold`，可走 `rlcd`。② Banking77 有 77 选项，`head_max_len` 共享预算，需抬预算或 `predict_tournament`（官方实测 0.430→0.610）。

## 02–05 待下载（按需，不要一次全拉）

| 类别 | 首选 | 许可 | 体积 | 备选 |
|---|---|---|---|---|
| LLM 语料 | `databricks/databricks-dolly-15k` | CC-BY-SA-3.0 | 12.5 MB | OASST1（Apache-2.0，220.8 MB） |
| LLM 小模型 | `Qwen/Qwen2.5-0.5B-Instruct` | Apache-2.0 | 0.93 GB | SmolLM2-360M（Apache-2.0，690 MB） |
| 生图图集 | `nbeerbower/iceland-photos` | CC-BY-SA-4.0 | 505 MB | japanese-photos 分片（CC0，453 MB） |
| 视频图集 | — | — | — | 0 干净命中 |

> 下载 >1 GiB 需 维护者 批准；命令统一走 `huggingface_hub.snapshot_download`（ComfyUI Python）或 `aria2c -x16 -s16`。

## 06 清单与说明

- `RESOURCES-ONLINE.md`：LLM/生图/视频候选与许可证（原 `docs\` 副本）
- `RESOURCES-BUSINESS.md`：决策/客服语料 + JEV 调研 + Laya 基准反推（原 `docs\` 副本）

## 用法速查

```powershell
# 决策模型（Laya）真跑，业务语料示例
& 'E:\ComfyUI\python_embeded\python.exe' -c "import sys; sys.path.insert(0, r'E:\视频\modelsmith\third_party\laya'); from laya.train_cli import main; sys.exit(main())" `
  --data 'E:\视频\modelsmith\resources\01-决策模型-业务语料\zh-decision-bench\zh_decision_bench.jsonl' `
  --out  'E:\视频\modelsmith\_runs\laya-zh-001' --base 'E:\视频\拉亚决策模型\models\laya' `
  --loss soft-ce --epochs 1 --micro-batch 8 --grad-accum 5
```

> 注意：`zh_decision_bench.jsonl` 首行是 `#` 注释，Laya 直接读会失败；用前先去掉注释行（转换脚本待写）。
