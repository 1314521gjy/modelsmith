# 线上资源清单：LLM 语料 / 可训练小模型 / 生图与视频 LoRA 图集（带许可证）

> 检索日期：**2026-10-22**
> 证据等级：**A**=本机实测（grep/文件读数）｜**C**=外部源站（HF 数据集/模型卡片元数据、GitHub LICENSE/README，经官方 API/raw 读取）｜**D**=推断
> 纪律：**本次未下载任何文件、未装依赖、未跑 GPU**。体积 = HuggingFace `tree` API 的文件字节求和（含 README/.gitattributes，误差 <1 MB）；许可证只认**源站卡片字段 `cardData.license` / `license:*` 标签 / 仓库 LICENSE 文本**，不抄博客。
> 方法：本机 `Invoke-WebRequest` 直连 `huggingface.co/api/...` 只读元数据（`web_fetch` 对该 API 返回 404，故未走 fetch 预算；`web_search` 本次 0 条）。

## 0. 结论（先看这四行）

| 类别 | 首选 | 许可证 | 体积 | 备选 | 许可证 | 体积 |
|---|---|---|---|---|---|---|
| LLM SFT 语料 | `databricks/databricks-dolly-15k` | CC-BY-SA-3.0 | 12.5 MB | `OpenAssistant/oasst1` | Apache-2.0 | 220.8 MB |
| 可训练小 LLM | `Qwen/Qwen2.5-0.5B-Instruct` | Apache-2.0 | 0.93 GB | `HuggingFaceTB/SmolLM2-360M-Instruct` | Apache-2.0 | 690.2 MB（仅 safetensors） |
| 生图 LoRA 图集 | `nbeerbower/iceland-photos` | CC-BY-SA-4.0 | 505.2 MB | `nbeerbower/japanese-photos-captioned` | CC0-1.0 | 452.8 MB（1/8 分片） |
| 视频 LoRA 图集 | **0 命中**（干净许可 + 视频+caption + LoRA 用途） | — | — | 仅评估：`jbilcke-hf/video-lora-training-fail-simulator` | **无许可** | 279.7 MB |

- **首选合计 ≈ 1,469.7 MB（1.44 GiB）**：语料 12.5 MB + 模型 0.93 GB + 图集 505.2 MB + 视频 0。
- **首选+备选合计 ≈ 2,833.5 MB（2.77 GiB）**：+ 220.8 MB + 690.2 MB + 452.8 MB。
- **需 维护者 批准的 >1 GiB 单件**：`Qwen2.5-1.5B-Instruct` 2.89 GB（推荐升级件，可选）、`ultrachat_200k` 1.55 GB（可选大语料）、`tulu-3-sft-mixture` 1.35 GB（**不采纳**，仅登记）。三类首选+备选**没有**单件 >1 GiB。
- 本机现状（A）：`third_party\LlamaFactory\data\` 只有 demo（`alpaca_en_demo.json`、`wiki_demo.txt`、`c4_demo.jsonl`、`dpo_en_demo.json`），**无正式训练语料**；无 HF 格式小模型权重；无带 caption 的图集/视频集。⇒ 线上补充成立。

## 1. LLM SFT 语料

| 类别 | 名称 | 来源 URL | 许可证（SPDX） | 体积 | 格式 | 需登录 | 适配理由 | 风险 / 裁决 |
|---|---|---|---|---|---|---|---|---|
| 语料·首选 | databricks-dolly-15k | https://huggingface.co/datasets/databricks/databricks-dolly-15k | **CC-BY-SA-3.0** | 12.5 MB | jsonl | 否（gated=False） | 15k 条人工指令，7 类任务，单文件可直接 SFT；体积最小 | ShareAlike：训练产物是否算衍生作品**未验**（法律问题）；**采纳** |
| 语料·备选 | OpenAssistant oasst1 | https://huggingface.co/datasets/OpenAssistant/oasst1 | **Apache-2.0** | 220.8 MB | parquet/gz | 否 | 多语言人工对话树，可做 chat 模板 SFT | 含用户生成内容，PII/毒性未清洗（**未验**）；**采纳** |
| 语料·可选 | UltraChat 200k | https://huggingface.co/datasets/HuggingFaceH4/ultrachat_200k | **MIT** | 1,548.8 MB | parquet | 否 | 合成多轮对话，许可最干净的大件 | >1 GiB 需批；合成数据风格偏「助手腔」；**可选（需批）** |
| 语料·可选 | orca-math-word-problems-200k | https://huggingface.co/datasets/microsoft/orca-math-word-problems-200k | **MIT** | 80.4 MB | parquet | 否 | 小学算术，答案由 GPT-4 生成，适合小模型单任务 SFT | 答案合成、非人工核；只覆盖数学；**可选** |
| 语料·仅评估 | SmolTalk | https://huggingface.co/datasets/HuggingFaceTB/smoltalk | **混杂**：卡片无 SPDX；README 称新增子集 Apache-2.0，其余「遵循原始数据集」 | 3,960.3 MB | parquet | 否 | 与 SmolLM2 配套的 SFT 混合集 | 整包许可不可判（子集来源多）；**仅评估，不作首选** |
| 语料·仅评估 | Tulu-3 SFT Mixture | https://huggingface.co/datasets/allenai/tulu-3-sft-mixture | **ODC-BY-1.0**（卡片明示「子集另有许可」） | 1,347.5 MB | parquet | 否 | 高质量混合 SFT，含 CoT | 子集许可混杂 + >1 GiB；**仅评估** |
| 语料·陷阱 | alpaca-cleaned | https://huggingface.co/datasets/yahma/alpaca-cleaned | 卡片写 **CC-BY-4.0**，但上游 Stanford Alpaca **DATA_LICENSE = CC-BY-NC-4.0** | 42.3 MB | json | 否 | — | 许可证冲突，按更严的上游判：**不采纳**（仅研究） |
| 语料·陷阱 | OpenHermes-2.5 | https://huggingface.co/datasets/teknium/OpenHermes-2.5 | **无许可**（卡片无 license 字段/标签） | 1,846.6 MB | json | 否 | — | ShareGPT 系派生、无许可；**不采纳** |

> Alpaca 冲突证据（C）：HF 卡片 `cardData.license=cc-by-4.0`；Stanford 官方 README 徽章与正文 = `Data License CC By NC 4.0 … research use only`（`https://raw.githubusercontent.com/tatsu-lab/stanford_alpaca/main/README.md`，访问 2026-10-22）。

## 2. 可训练的小 LLM（HF safetensors，非 GGUF）

| 类别 | 名称 | 来源 URL | 许可证（SPDX） | 体积（bf16 权重） | 格式 | 需登录 | 32GB 单卡 LoRA 可行性 | 风险 / 裁决 |
|---|---|---|---|---|---|---|---|---|
| 模型·首选 | Qwen2.5-0.5B-Instruct | https://huggingface.co/models/Qwen/Qwen2.5-0.5B-Instruct | **Apache-2.0** | 0.93 GB | safetensors | 否 | **可行**，峰值估 3–4 GB（D，按 bf16 权重+激活估，未实测） | 能力弱，只适合格式/流程验证；**采纳** |
| 模型·备选 | SmolLM2-360M-Instruct | https://huggingface.co/models/HuggingFaceTB/SmolLM2-360M-Instruct | **Apache-2.0** | **690.2 MB**（仓库全量 4.79 GB 含 ONNX/bin，**只取 safetensors**） | safetensors | 否 | **可行**，峰值估 3–4 GB（D） | 仓库混放多格式，按文件粒度下载；**采纳** |
| 模型·升级 | Qwen2.5-1.5B-Instruct | https://huggingface.co/models/Qwen/Qwen2.5-1.5B-Instruct | **Apache-2.0** | 2.89 GB | safetensors | 否 | **可行**，峰值估 8–10 GB（D） | >1 GiB 需批；中文/指令跟随明显好于 0.5B；**可选（需批）** |
| 模型·可选 | SmolLM2-135M-Instruct | https://huggingface.co/models/HuggingFaceTB/SmolLM2-135M-Instruct | **Apache-2.0** | 256.6 MB（仅 safetensors；全量 1.79 GB） | safetensors | 否 | **可行**，峰值估 2–3 GB（D） | 只够冒烟测试；**可选** |
| 模型·可选 | TinyLlama-1.1B-Chat-v1.0 | https://huggingface.co/models/TinyLlama/TinyLlama-1.1B-Chat-v1.0 | **Apache-2.0** | 2.05 GB | safetensors | 否 | **可行**，峰值估 7–9 GB（D） | >1 GiB；英文为主；**可选（需批）** |
| 模型·可选 | Phi-3.5-mini-instruct（3.8B） | https://huggingface.co/models/microsoft/Phi-3.5-mini-instruct | **MIT** | 7.12 GB | safetensors | 否 | **可行**，峰值估 16–18 GB（D） | >1 GiB；32GB 上余量小，grad checkpoint 必开；**可选（需批）** |
| 模型·不采纳 | gemma-3-270m-it | https://huggingface.co/models/google/gemma-3-270m-it | **Gemma Terms**（非 OSI） | 0.54 GB | safetensors | **是**（gated=manual） | 技术上可行 | 许可限制再分发/用途 + 需登录同意；**不采纳** |

> 可行性口径（D）：LoRA SFT 峰值 ≈ 权重（bf16）+ 激活（seq 2048、batch 1–8）+ LoRA 参数/优化器；32 GB 卡上 7 GB 权重以内都留有余量，但**未实测**，不进承诺。

## 3. 生图 LoRA 图集（图片 + caption 对）

| 类别 | 名称 | 来源 URL | 许可证（SPDX） | 体积 | 格式 | 需登录 | 适配理由 | 风险 / 裁决 |
|---|---|---|---|---|---|---|---|---|
| 图集·首选 | nbeerbower/iceland-photos | https://huggingface.co/datasets/nbeerbower/iceland-photos | **CC-BY-SA-4.0** | 505.2 MB | parquet（image+text） | 否 | 实拍冰岛照片 + caption，<1K 张，正好训风格/场景 LoRA；≤2GB | ShareAlike + 上传者声明的原始出处未逐张核（**未验**）；**采纳** |
| 图集·备选 | nbeerbower/japanese-photos-captioned | https://huggingface.co/datasets/nbeerbower/japanese-photos-captioned | **CC0-1.0** | 3,394.2 MB（整仓）/ **452.8 MB（1/8 分片）** | parquet（image+text） | 否 | 1K–10K 张日本街景 + caption，CC0 最干净 | 整仓 >2GB：**只取 1 个分片**（`train-00000`），不再分发其余；**采纳（分片）** |
| 图集·仅评估 | PokeFA-pokemon-fanart-captioned | https://huggingface.co/datasets/Kev0208/PokeFA-pokemon-fanart-captioned | **CC0-1.0**（声明） | 6.0 MB | parquet（image+text） | 否 | 10K–100K 标签、极小 | 宝可梦二创，底层 IP 归任天堂，CC0 声明不能覆盖 IP；**仅评估** |
| 图集·不采纳 | pokemon-blip-captions | https://huggingface.co/datasets/lambda/pokemon-blip-captions | **CC-BY-NC-SA-4.0** | 95.1 MB | parquet（image+text） | 是（gated=manual） | diffusers 官方示例，常用 | 非商用 + 需登录 + IP；**不采纳** |
| 图集·不采纳 | naruto-blip-captions | https://huggingface.co/datasets/lambda/naruto-blip-captions | **无许可**（卡片无 license） | 668.3 MB | parquet（image+text） | 否 | — | 无许可 + 动漫 IP；**不采纳** |
| 图集·不采纳 | diffusers/dog-example | https://huggingface.co/datasets/diffusers/dog-example | **无许可**（卡片无 license） | 5.3 MB | jpeg（**无 caption 文件**） | 否 | diffusers LoRA 文档示例 | 无许可且只有图、无同名 caption；**不采纳** |

## 4. 视频 LoRA 图集

**结论：0 命中**（干净许可证 + 视频/短片段 + caption + 可直接做 LoRA 训练，三个条件同时满足）。

- 检索式原文（HF 官方 API，2026-10-22）：
  - `https://huggingface.co/api/datasets?search=video%20lora&limit=15`
  - `https://huggingface.co/api/datasets?search=wan&limit=15`
  - `https://huggingface.co/api/datasets?filter=license:cc-by-4.0&search=video&limit=12`
  - `https://huggingface.co/api/datasets?search=video%20caption&limit=15`
- 命中但**不可用**：
  - `jbilcke-hf/video-lora-training-fail-simulator`（279.7 MB，text-to-video/image-to-video，**无许可**）→ 仅评估。
  - `jbilcke-hf/video-lora-training-rickroll`（108.4 MB，webdataset，**无许可**）→ 仅评估。
  - `DAMO-NLP-SG/Multi-Source-Video-Captioning`（MIT）、`wchai/Video-Detailed-Caption`（Apache-2.0）：是**评测/描述**数据，非「视频+caption 的训练图集」，原始视频再分发权未验 → 不用。
  - `nvidia/video_to_data_challenge`（CC-BY-4.0）、`HumynLabs/Street-videos`（CC-BY-4.0）：研究/挑战数据，非 LoRA 训练小样本，体积/结构未验 → 不用。

## 5. 下载总量算术

| 方案 | 组成 | 体积 |
|---|---|---|
| 三类首选 | 12.5 + 952.3（0.93 GB）+ 505.2 + 0 | **≈ 1,469.7 MB（1.44 GiB）** |
| 首选 + 备选 | 上 + 220.8 + 690.2 + 452.8 | **≈ 2,833.5 MB（2.77 GiB）** |
| 加推荐升级件（需批） | 上 + 2,891.5（Qwen2.5-1.5B） | ≈ 5,725.0 MB（5.59 GiB） |
| 再加可选大语料（需批） | 上 + 1,548.8（UltraChat 200k） | ≈ 7,273.8 MB（7.10 GiB） |

> 口径：GB 按 API 十进制值 ×1024 折算为 MB；**未计** HF 缓存/解包开销（通常 +5–10%）。

## 6. 未验与风险

- **未验**：所有体积为 HF `tree` API 元数据求和，**未实际下载核对**；`gated` 字段是 API 读数，未走登录流程验证。
- **未验**：图集的「上传者声明」许可是否覆盖每张图的原始版权（尤其 iceland / japanese 实拍）；商用前需人工抽检。
- **未验**：OASST1 的 PII/毒性；Dolly 的 ShareAlike 对模型权重的传染范围（法律判断，非技术）。
- **未验**：32GB 单卡 LoRA 峰值显存为 **D 级推算**；实际以 `torch.cuda.max_memory_allocated` 为准（本次不跑 GPU）。
- 视频类 `0 命中` 是「未找到同时满足三条件者」，不是「不存在」——如需，可放宽到「无许可但仅本地研究」后再评估。
- 许可证判读规则：卡片无 `license` 字段且标签无 `license:*` ⇒ 记「无许可」，按不可再分发处理（不假设默认许可）。
