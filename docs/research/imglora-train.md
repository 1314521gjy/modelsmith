# 生图/视频 LoRA（ai-toolkit）：训练 / 采样 / 验收协议（研究笔记）

> 出处 `文件:行号`，路径相对 `third_party/ai-toolkit/`。
> 例证配置：`config/examples/train_lora_flux_24gb.yaml`（下称 F）、`train_lora_wan22_14b_24gb.yaml`（下称 W）。
> 注：任务单指向 `toolkit/stable_diffusion_model.py` 找采样触发，但全仓 grep 证实该文件无
> generate/sample_every 触发代码；触发逻辑在 `jobs/process/BaseSDTrainProcess.py`。

## 1) 训练协议

### 数据集与 caption 约定
- 数据集 = 图片文件夹；caption 为同名 txt（image2.jpg ↔ image2.txt）；支持 jpg/jpeg/png（F:29-35；W:26-30）。
- `caption_dropout_rate: 0.05` —— 5% 概率丢 caption（无条件训练）（F:36；W:31）。
- `shuffle_tokens: false` 可打乱逗号分段的 caption 词序（仅 F 示例列出，F:37）。
- trigger word：`train.trigger_word` 设定后自动追加进不含它的 caption；caption/样本 prompt 里写 `[trigger]` 占位符会被替换（F:13-15,77-78；注入代码 BaseSDTrainProcess.py:326-329）。
- Wan2.2 特别约束：trigger_word 与 `unload_text_encoder`（用 trigger 编码替全部 caption，captions 被忽略）/`cache_text_embeddings`（缓存全部 caption）的用法互斥，二选一（W:15-16,58-64）。

### 分辨率 / bucket
- `resolution: [512, 768, 1024]` 多档位列表，图片自动缩放并 bucket 进最近档位（F:31,39；W:35）。
- 视频：`num_frames` 均匀抽帧，=1 即当图片训（W:32-34；config_modules.py:1072-1074）。

### steps / rank / lr / batch
- `network: {type: lora, linear, linear_alpha}`：F 16/16，W 32/32；代码默认 rank=4，alpha 默认回退到 rank（F:16-19；W:17-20；config_modules.py:174-188,200）。
- `train.steps`：默认 1000；示例 2000，注释建议 500–4000（config_modules.py:383；F:42；W:38）。
- `train.lr`：默认 1e-6，示例 1e-4；`lr_scheduler` 默认 `constant`（config_modules.py:384,392；F:49；W:46）。
- `batch_size` 默认 1（config_modules.py:396；F:41；W:37）；`gradient_accumulation_steps` 默认 1（config_modules.py:458；F:43）。
- 优化器示例 `adamw8bit`，W 另设 `optimizer_params.weight_decay: 1e-4`；`noise_scheduler: flowmatch`（仅训练侧）；`gradient_checkpointing: true` 视为 24GB 卡必需；`dtype: bf16`（F:46-49,63；W:43-46,53）。
- F 独有：`ema_config.use_ema: true, ema_decay: 0.99`（F:58-60）；`train_text_encoder: false`（F:45；W:41）。
- W 独有：MOE 高/低噪两阶段 `switch_boundary_every: 10`（每 10 步切换训练哪个 stage）（W:55-56）；`model_kwargs.train_high_noise/train_low_noise` 可控只训一噪（W:77-79）。

### save_every 与产物
- `save.save_every`：默认 1000，示例 250；`max_step_saves_to_keep` 默认 5、示例 4（中间 ckpt 只留最近 N 个）；`dtype: float16`；`save_format` 默认 safetensors（config_modules.py:25-28；F:20-23；W:21-24）。
- `training_folder: "output"` 为 sessions/samples/weights 根目录，`name` 即文件夹与文件名（F:5-9,8-9）。
- 可 `push_to_hub`（F:24-27）。

### 显存档位（24GB 系列的省显存手段）
- Flux：`quantize: true`（8bit 混精）；`low_vram: true` 备选（显示器占用场景更省但更慢）（F:68-69）。
- Wan 14B：`quantize + qtype uint4`（带 accuracy-recovery adapter）、TE `qfloat8`、`low_vram: true`、`cache_text_embeddings: true`（W:64-75）。

## 2) 采样协议

### 默认值（SampleConfig，config_modules.py:81-117）
- `sample_every` 默认 **100**（示例都显式改 250）；`sample_start_step` 默认 0；width/height 默认 512（示例 1024）；seed 默认 0；`walk_seed` 默认 false（示例 true）；`guidance_scale` 默认 7（F 用 4，W 用 3.5）；`sample_steps` 默认 20（F:93；W:108 用 25）；`sampler` 须与 train.noise_scheduler 一致（F:71）。
- `prompts` 为字符串列表；另有新式 `samples:` 每项可覆写 width/seed/guidance 等（SampleItem，config_modules.py:44-79,107-115）。
- num_frames>1 时输出强制转 animated webp（config_modules.py:103-105；W:89-90）。

### 触发时机（BaseSDTrainProcess.py；stable_diffusion_model.py 无此逻辑）
- **起训前**：step≤1 生成一次"训前基线"样本，`skip_first_sample` 或 `disable_sampling` 可跳（:2486-2491；F:50-53；W:49-52）。另有 `has_first_sample_requested` 时 step 0 用 first_sample_config 再出一次（:2482-2484）。
- **每 N 步**：循环内 `step_num >= sample_start_step 且 step_num % sample_every == 0` 触发（:2565-2569,2735-2742）；首个 step 不触发（:2715-2717）。
- **末步**：训练结束后先 `save()` 再对最终 step 出一次样本，除非 `disable_sampling`（:2828-2831）。→ 首、末各出一次成立（默认配置下）。
- **保存时不额外采样**：is_save_step 与 is_sample_step 独立判定，仅当二者同步时各自执行（:2564-2569）。

### 出图张数与命名
- 每次采样 = 每个 prompt 出 **1 张**，共 `len(prompts)` 张（示例 11 条 prompt → 11 张）（:300-349；F:76-88）。
- `walk_seed: true` → 第 i 条 prompt 用 seed+i，跨步可纵向对比（:301-302；F:91）。
- 落 `save_root/samples/`，文件名 `[time]_{step补零9位}_[count].{ext}`（:286,304-311）。
- prompt 支持 `[trigger]` 占位与 embedding/adapter 注入（:316-329）。

## 3) 验收协议

### loss 曲线怎么读
- 训练标量 loss 按 `log_every` 默认 100 步记录（config_modules.py:37）；flowmatch 下 loss 是噪声预测 MSE，绝对值不可跨架构比，只看趋势与平台。**repo 未给任何 loss 阈值判据——未验**。

### 过拟合信号
- 工具内无自动过拟合检测。实践判据靠采样对比：同 seed 逐步对比，出现风格糊化/构图僵死/触发词外泄（不加 trigger 也长同一张脸）即过拟合 —— **repo 内未文档化，未验**；`caption_dropout_rate` 与 EMA 是仅有的两个抗过拟合旋钮（F:36,58-60）。
- 每 epoch 重看样本：`sample_every 250 × steps 2000` = 全程约 8 个观察点 + 首末各一（由 :2565-2569,2828-2831 推得）。

### 采样对比怎么看
- 固定 `seed: 42` + `walk_seed: true`：同 prompt 跨 step 可比（F:90-91）。
- 训前基线样本（step 0/1）是与训后对照的天然参照（BaseSDTrainProcess.py:2489-2491）。
- 采样本身耗时，Wan 视频尤甚，示例注释建议少用（W:89-90）。

### 产物清单
- 权重：`output/<name>/`（save_root）下 safetensors（fp16），每 save_every 步一个中间 ckpt，仅留最近 `max_step_saves_to_keep` 个；训练完成后全量 ckpt（config_modules.py:25-28；F:20-23）。
- 样本：`<save_root>/samples/*_step_[count].jpg|webp`（BaseSDTrainProcess.py:286,309）。
- 配置：整份 YAML 随训练 session 存档于 training_folder（"training_folder 保存 sessions/samples/weights"，F:8-9；sessions 具体落盘格式未验）。
