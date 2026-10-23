# Laya 决策模型：训练 / 测验协议（研究笔记）

> 出处格式 `文件:行号`；laya 代码路径相对 `third_party/laya/`。未确证处标「未验」。

## 1) 训练协议

### 数据格式（JSONL）
- 一行一 case：`{state, questions, gold}` 或 `{state, questions, expected}`，可混用（docs/finetune.md:52-69; laya/train.py:993）。
- `questions`：每问 `{type: choice|score|noul, instructions, criteria}`；noul 可不带 criteria（docs/finetune.md:56-60）。
- `expected`：choice=标签串、noul=true/false、score=档位（小数如 1.5 按权重拆到 1/2 档）（train.py:196-284）。
- `gold`：教师概率 `{qid:{probabilities:{label:p}}}`；noul 键 `"false"/"true"`，score 键为档位索引字符串；和为 0 则退化为均匀（train.py:167-193）。
- CSV 也可：`--text-column/--label-column`，标签首现顺序成 choice 选项，存 `questions.json`（train.py:365-423; docs/finetune.md:27-45）。
- 无效行按原因计数跳过：empty_text/invalid_question/options_collapsed/options_beyond_max_len 等（train.py:317-362; docs/finetune.md:82-88）。

### loss 选择
- `loss="rlcd"`（默认）= GRPO 式策略梯度（每项 4 采样、噪声 σ 0.4→0.1 线性退火、proper reward：spherical 0.75 + ranked-prob 1.0）+ 全权重 soft-CE（train.py:7-10,499-523,600 行附近 sigma_at 521-523; docs/finetune.md:249-255）。
- `loss="soft-ce"` = 只软交叉熵（train.py:493-496）。#741 测得 typed-decisions 上额外项无增益；#887 实测 soft-ce 0.790 vs rlcd 0.7715（docs/finetune.md:163,186-190; train.py:7-10）。

### 关键超参默认（TrainConfig，train.py:91-124）
- epochs 4 / micro_batch 8 / grad_accum 8（等效批 64）；encoder_lr 2.5e-5、head_lr 1e-4、min_lr 1e-6、wd 0.01、grad_clip 1.0；AdamW + 余弦调度（train.py:91-98,676-681）。
- calib_max 400、calib_frac 0.1、calib_seed 20260922、seed 0；label_smoothing 0.0；target_error 0.10、min_abstain_n 10；max_len/head_max_len 默认取 base cfg（缺省 512/192，train.py:1005-1006）。
- amp：仅 CUDA 默认开（fp16 autocast）；gradient_checkpointing 跟随 amp（train.py:85-86,657-658）。fp16 慢卡（GTX 16xx）设 amp=False（docs/finetune.md:175-176）。
- shuffle_options：默认关；每 epoch 随机 option_order 重编码并置换 target（train.py:12-16,464-471,694-697）。20 选项时 +0.55pt acc、序翻率降约 1/4，多 ~14% 时间；4 选项无收益（docs/finetune.md:192-204）。

### 步数计算
- `updates = epochs × ceil(ceil(train_items/micro_batch)/grad_accum)`，每 epoch 末尾不满窗也 step 一次（train.py:607-619）。
- 例：6,000 项 − 400 校准 = 5,600 → 352 updates；`--dry-run` 与开跑都打印预算（docs/finetune.md:96-114; train.py:622-632）。
- 小数据集默认 4 epoch 预算不够 → 头塌缩到类先验；判据：均值行内 logit range < 0.05 或校准 CE 距 ln K ≤2% 触发警告（train.py:66-73,926-986）。

### 评测/保存节奏
- 无按步评测；`checkpoint_latest/` 每 epoch 末重写一次，崩只损失一轮；最终 ckpt 在全训完+校准后落盘（train.py:1125-1130,1229-1237; docs/finetune.md:117-120）。无早停机制（未验：未发现 early-stopping 代码路径）。
- base ckpt 在 eval 数据上训前打分、微调后复评，出 before/after/delta 表（train.py:1117-1191）。

### 温度校准
- 校准切片在训练前切出：min(400, 10% items)，固定 seed，各 rank 一致（train.py:474-488）。用训过的项拟合会退化（docs/finetune.md:279-284）。
- 末 epoch 后 rank0/主进程调 `laya.calibrate.fit_temperature_map`：LBFGS 优化 log T、NLL 目标，按 qtype 一个标量 + 按 temp_bucket(qtype,k) 桶值；clamp [TEMP_MIN,TEMP_MAX]（文档记 [0.1,10]，docs/finetune.md:287-290; calibrate.py:136-199,306-330）。
- 下限：类型级 MIN_TYPE_N=10 项以下不拟合保持 T=1.0（calibrate.py:43; train.py:128 警告样例）；<50 项报告「证据少」（train.py:66,794-796）。
- 产物写入 `rl_agent_config.json`：`temperature`、`temperature_by_options`，且删除继承的旧桶值以免掩盖新拟合（train.py:1219-1223; docs/finetune.md:289-290,358-359）。温度不改 argmax，只改置信度（docs/finetune.md:292）。
- 另拟合拒答阈值 `fit_abstention_thresholds`（按桶、target_error=0.10、桶 <10 项不设限）（train.py:1144-1151; calibrate.py:442+）。

### 产物清单
`--out`：`model.safetensors`（fp16）、`encoder/`、`tokenizer/`、`rl_agent_config.json`、`questions.json`、`train_report.json`、`checkpoint_latest/`（train.py:590-602,1239-1264; docs/finetune.md:117-120）。

## 2) 测验协议（laya-evals）

### 数据与指标
- eval JSONL 行：必填 `state/questions/expected`（与训练同 schema，一文件可既训又评），可选 `tags/language/model`（docs/evals.md:276-283; docs/finetune.md:63-65）。
- 默认指标：choice_accuracy、noul_accuracy(p≥0.5)、score_mae、mean_confidence；每行必算：ece(15 桶)、brier、aurc、selective_accuracy@50/@80、latency_p50/95（docs/evals.md:295-310,386-392）。
- score 答无 correct 值 → 不进 ece/brier/aurc/selective（纯 score 集不出 ece，--max-ece 报缺失）（docs/evals.md:312-316）。
- 置信度列读 `answer_confidence`，缺失按 confidence→max(p,1-p)→max(prob) 兜底——不同来源不可比（docs/evals.md:318-341）。

### 留出集怎么切
- 训练侧：`--eval/--eval-data` 第二数据集训前训后各答一遍；报告标 `eval_mode/is_held_out/note`；state+question 与训练或校准输入重合 → `overlapping_eval`，不算独立泛化（#967）（train.py:1026-1107; docs/finetune.md:170）。
- 不给 --eval 时"after"数字来自校准切片 = in-sample，不是留出（train.py:1095-1101）。
- 校准片 ≤400 项按设计只够 3 个标量，不能当验证集用，评测数据要另行留出（docs/finetune.md:351-353）。

### 跑命令与判据
```
laya-evals validate fixture.jsonl            # 只查格式，无权重
laya-evals run data.jsonl --model <ckpt> --device cpu \
  --min-accuracy 0.8 --max-ece 0.05 --score-within 0.25 --slice language \
  --json report.json --baseline baseline.json --tolerance choice_accuracy=0.02
laya-evals compare report.json --baseline baseline.json --tolerance ...
```
（docs/evals.md:12-23）。退出码 0 通过 / 1 阈值或基线漂移失败 / 2 用法错（docs/evals.md:27-30）。切片级门槛用 `--gate-policy gates.json`（min_count + max_drop 等）（docs/evals.md:464-499）。可比性硬门：`compare` 校验 schema/dataset_sha256/questions_sha256，不一致直接 FAIL（docs/evals.md:575-584）。

### 常见坑
- 模板留出集 ≠ 泛化：本机 htlb 实践——模板留出 top-1 1.0000 饱和，真人问法集仅 0.80（A 级证据）；只有真人风格集是「对用户能说的数」（train/README-训练台.md:43-47,126-128）。
- 泄漏：htlb v1 训练候选池含留出条目 6.8%，v2 起 `leak_pairs` 必须为 0 作负控（README:173-174）。
- 覆盖/并列：置信度相同的组不会被阈值切开，selective_accuracy 实际覆盖率可高于名义值、行序可致 ±0.5 波动；跨 `coverage_metric_definition` 版本的基线被拒比（docs/evals.md:343-383）。
- 第 2 epoch loss→0.0009 是记忆（同批仅 shuffle），不能当收敛质量（README:132,142）。
- 温度未按目标分布重校：sel-v1 在真人分布外 P(相关) 塌 0，0.5 阈值拒 93.3%——阈值须按部署分布重定（README:153-156）。
- base Laya 未训口径下真人集不如随机（top-1 0.10），不可作否定微调的基线（README:163-164）。
- 塌缩是种子相关事件（#963：15 跑中 9 起 6 不起），单跑不定结论，换种子重复（docs/finetune.md:151-156）。
- Windows：`rl_agent_config.json` 必须纯 ASCII（Agent 读取不带 encoding）；中文路径需 TORCHDYNAMO_DISABLE=1（README:158-160; train_sel.py:35,169-172）。

### 本机实践差异（train_sel.py，纯监督路线）
- 不走 laya.train：自写 CE 循环，noul 5 选 1（1 正+4 硬负），bf16、lr 2e-5、batch 16、seq≤512、clip 1.0、余弦（train_sel.py:11-17,251-252）。
- 增广 = 训练时 50% 概率交换 noul 两选项位（train_sel.py:13,71-74）。
- 校准 = 按 qid 抽 8%（问题不跨集合），单标量 T 网格 0.5–5.0 步长 0.05 最小化 NLL，写 `temperature[noul]` 与 `temperature_by_options["noul:2"]`（train_sel.py:16,120-149,330-336）。未做 ECE/分桶校准（README:155-156）。
- 保存节奏：每 epoch 末存 checkpoint_latest，训毕存终版 + 训练元数据入 cfg（train_sel.py:311-316,363）。
