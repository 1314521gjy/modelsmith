# Adapters — 环境方案（P4 准备，**未执行安装**）

本文件只记录各训练后端的环境结论与安装方案；**本次没有安装任何包、没有下载模型、没有跑 GPU**。
具体适配器实现与桩测试在各 `*_adapter.py` / `selftest_*.py`。

> **两个 LLM 自测必须用 `E:\ComfyUI\python_embeded\python.exe` 跑**（系统 Python312 无 torch/transformers：`selftest_llm.py` 会 0/1、`selftest_lf_adapter.py` 会 23/1 假红，非产品红）。

## ai-toolkit（生图 / 视频 LoRA）

### 事实（只读核对）

- 上游要求：`E:\视频\modelsmith\third_party\ai-toolkit\requirements_base.txt`
  - `transformers==5.5.3`（第 5 行）
  - `diffusers` 钉在 git commit `c943837899b16cbae2f619b8dd4f7bb6f07dd81a`（第 3 行，不是 PyPI 版本）
  - `torchao==0.10.0`、`huggingface_hub==1.23.0`、`peft==0.18.1`、`gradio`、`opencv-python` 等（同文件）
  - `requirements.txt` 另加 `scipy==1.12.0`
- 本机 ComfyUI Python：`E:\ComfyUI\python_embeded\python.exe`，已装 `torch 2.12.0+cu130`、`transformers 4.57.6`、`numpy 2.5.4` 等（只读探测；详见 `core\README.md` §12.6）。
- **冲突点**：ai-toolkit 要 `transformers==5.5.3`，ComfyUI 是 `4.57.6`。把 ai-toolkit 的 requirements 装进 ComfyUI 环境会**替换/降级 ComfyUI 的 transformers**，可能破坏现有 ComfyUI 节点与工作流。⇒ **必须独立 venv，不要装进 ComfyUI 环境。**

### 安装方案（本次不执行，需 维护者 批准）

1. **独立 venv（推荐）**
   - 用 Python 3.12 建独立 venv（不要用 ComfyUI embedded 的 `._pth` 隔离环境直接装）。
   - 先装与显卡驱动匹配的 torch（本机驱动 616.56 / CUDA 13.0；ComfyUI 现用 torch 2.12.0+cu130），再：
     `python -m pip install -r E:\视频\modelsmith\third_party\ai-toolkit\requirements.txt`
   - `diffusers` 是 git 直装（第 3 行），需要 git 与网络；离线环境要预先准备 wheel/镜像。
2. **复用 ComfyUI 的 torch（备选，未验）**
   - 用 ComfyUI Python 建 `venv --system-site-packages`，让 venv 复用 torch，再在 venv 内装 ai-toolkit 的其余依赖（含 transformers 5.5.3）。这样比全套重装省数 GiB，但 embedded Python 的 `._pth` 隔离是否能正常建 venv、以及 `--system-site-packages` 是否被识别，**本次未验**。
3. **禁止**：`pip install -r ...` 直接进 ComfyUI；用系统 `C:\Program Files\Python312\python.exe` 直接装（它没有 torch，且会把整个 GPU 栈装进系统 Python）。

### 体积 / 耗时估算（**未验**）

- 若复用现有 torch：新增依赖约 **2–4 GiB**，安装约 **10–30 分钟**（取决于网络/diffusers git 克隆）。
- 若 venv 内另装 CUDA torch：总占用约 **6–10 GiB**，安装约 **20–60 分钟**。
- 以上为量级估算，**未实测**；以真实 `pip` 输出与磁盘读数为准。

### 运行时入口

- 真实训练由独立 venv 的 Python 执行：
  `python E:\视频\modelsmith\third_party\ai-toolkit\run.py <config.yaml>`
  （`run.py` 用 `os.getcwd()` 做 `sys.path`，cwd 应为 ai-toolkit 根目录；适配器会写好 YAML。）
- 适配器默认用 `sys.executable`；可设 `MODELSMITH_AITK_PYTHON` 指向独立 venv 的 Python，或给 `run(..., train_command=[venv_python, run_py])`。
- 本次自测只启动 `adapters\_tmp\aitk_stub.py`，不 import torch/ai-toolkit，不占 GPU。

## LlamaFactory v0.9.5（LLM 微调主线）

### 事实（只读核对，A 级）

- 版本：`third_party\LlamaFactory\src\llamafactory\extras\env.py:27` → `VERSION = "0.9.5"`；许可证 Apache-2.0（仓库根 `LICENSE`）。
- 入口：`src\llamafactory\cli.py:16` `main()` → `launcher.launch()`；`launcher.py:154-157` 的 `train` 分支 `from .train.tuner import run_exp`。
- YAML 解析：`hparams\parser.py:85-99` `read_args()` —— `sys.argv[1]` 以 `.yaml/.yml` 结尾就 `OmegaConf.load(...)`，其余走 CLI 参数。
- 字段出处（逐条 grep，行号见 `resources\02-LLM-语料\README-LLM跑通.md`「成熟线」表）：`model_args.py:38/201`、`finetuning_args.py:69/73/77/81/460/464/551`、`data_args.py:26/30/34/38/46/84/88/100`、`training_args.py:143/146`；`do_train`/`output_dir`/`logging_steps` 等 17 个键**不在 LF 里**，继承 `transformers.Seq2SeqTrainingArguments`（`training_args.py:19,36,143`）。
- 适配器：`adapters\lf_adapter.py`（薄，不改 third_party）；自测 `adapters\selftest_lf_adapter.py`（零 GPU，22/22 PASS）。

### 依赖探测结论（本机 ComfyUI Python 3.12.12，**未安装任何包**）

- **OK**：`llamafactory 0.9.5`、`torch 2.12.0+cu130`、`transformers 4.57.6`、`accelerate 1.12.0`、`peft 0.18.0`、`yaml`、`pyarrow 26.0.0`、`fsspec`、`dill 0.4.0`、`aiohttp`、`zstandard`、`huggingface_hub 0.36.2`、`pandas`、`numpy`、`sentencepiece`、`tiktoken`、`omegaconf`、`fire`、`av`、`gradio`、`matplotlib`、`pydantic`、`fastapi`、`uvicorn`。
- **缺（阻断 SFT）**：`datasets`（**不是没装这么简单**：`import datasets` 会命中 `E:\ComfyUI\ComfyUI\custom_nodes\ComfyUI-FishSpeech\fish_speech\datasets` 这个同名目录，得到 `__file__=None` 的命名空间包，`from datasets import DatasetDict` 直接 `ImportError: unknown location` —— 探针把它标成 `BROKEN_NAMESPACE`，不许算 OK）、`trl`、`multiprocess`（datasets 的依赖）。
- **缺（不影响 SFT）**：`torchdata`（`src\` 里 0 处 import）、`tyro`（0 处）、`sse_starlette`（只有 `api\app.py`）、`hf_transfer`（可选加速）。
- **版本口径**：`extras\misc.py:check_dependencies()` 只校验 5 个包 —— `transformers`/`datasets`/`accelerate>=1.3.0,<=1.15.0`/`peft>=0.18.0,<=0.20.0`/`trl>=0.18.0,<=0.24.0`。本机 `accelerate 1.12.0`、`peft 0.18.0`、`transformers 4.57.6` 都在范围内（`pyproject.toml` 里写的 `accelerate<=1.11.0` 是打包声明，运行时校验是 ≤1.15.0）。
- 决定性的导入探针（3 条全红）：`from llamafactory.hparams.parser import get_train_args` → `ImportError: cannot import name 'DownloadMode' from 'datasets'`；`from llamafactory.model import load_model` → `ModuleNotFoundError: No module named 'trl'`（`model\loader.py:28` 是模块级 import）；`from llamafactory.train.tuner import run_exp` → `ImportError: DatasetDict`。

### 最小安装清单（**待 维护者 批准；本席未安装**）

| 包 | 约束（上游） | 体积估计（未验，D 级） |
|---|---|---|
| `multiprocess` | datasets 的依赖 | 约 0.05 MB |
| `datasets` | `>=2.16.0,<=4.0.0` | wheel 约 0.5 MB；已装的 pyarrow 26.0.0 不再拉 |
| `trl` | `>=0.18.0,<=0.24.0` | wheel 约 0.6 MB；transformers/accelerate/datasets 已有 |

合计下载约 **1.1–1.3 MB**，装后磁盘约 **15–25 MB**（估）。**不要**为了 SFT 装 `torchdata`/`tyro`/`sse_starlette`/`hf_transfer`（SFT 路径不 import）。
风险：ComfyUI 的 `sys.path` 里**用户站点包**（`C:\Users\Administrator\AppData\Roaming\Python\Python312\site-packages`）排在 ComfyUI 站点包**前面**，装到哪里要先决定；建议先 `pip install --dry-run` 看它会动哪些已有包（尤其 `numpy`/`fsspec`/`huggingface_hub`），并加 `--no-deps` 先试（三个包的全部传递依赖本机已具备）。

### 运行时入口

```powershell
# 只写 YAML + 打印 argv（不启动训练）
& 'E:\ComfyUI\python_embeded\python.exe' 'E:\视频\modelsmith\adapters\lf_adapter.py' `
  --model  'E:\视频\modelsmith\resources\03-LLM-小模型\Qwen2.5-0.5B-Instruct' `
  --dataset ms_dolly_200 --dataset-dir 'E:\视频\modelsmith\adapters\_tmp\lf_data' `
  --template qwen --out 'E:\视频\modelsmith\adapters\_tmp\lf_jobs\smoke2' `
  --max-steps 2 --cutoff-len 512 --logging-steps 1 --save-steps 2 --bf16 --dry-run

# 依赖装齐后真跑（同上去掉 --dry-run）
```

子进程形式：`<python> -c "sys.argv=['llamafactory-cli','train',<yaml>]; sys.path.insert(0,<LF src>); from llamafactory.cli import main; sys.exit(main())"`，
cwd 固定为项目根、`PYTHONDONTWRITEBYTECODE=1`（避免往 `third_party` 里写 `__pycache__`）。

### 本次状态

- 适配器 + 自测：**已交付**，`selftest_lf_adapter.py` **PASS 22 / FAIL 0**（零 GPU；含 stub 子进程成功/取消/spawn 失败三态、11 种日志行、YAML 27 键全部命中上游、第三方树未变）。
- **2 步 smoke 未跑**：依赖不齐（上面 3 个包缺失），按任务纪律「依赖齐才跑」。
- 数据集已备好可直跑：`adapters\_tmp\lf_data\ms_dolly_200.json`（200 行，57 行带 input）+ `dataset_info.json`。
- 详细读数与阻断 traceback：`resources\02-LLM-语料\README-LLM跑通.md` §10「成熟线：LlamaFactory」。

### 更新（维护者 授权后）：安装、probe、2 步 smoke 结果

> 上面的「依赖探测 / 最小安装清单 / 本次状态」写在授权之前，**保留为历史**；本节是执行后的读数与修正。

**安装（隔离目录，ComfyUI 的 site-packages 未动）**：`E:\视频\modelsmith\.pylibs-lf`

- 命令（维护者 给的原样）：`pip install -i <清华镜像> --target .pylibs-lf "datasets>=2.16,<=4.0" "trl>=0.18,<=0.24" multiprocess`，rc=0，207.9 s。
- **坑 1：`pip --target` 会装整条依赖树**（忽略基础环境已有的包）⇒ 实际 **92 个包 / 973.3 MB**，含 **torch 2.14.1+cpu（501.6 MB，`cuda_available=False`）**、transformers 5.20.2、huggingface_hub 2.2.0。
  若按原计划把该目录插到 `sys.path[0]`：子进程会加载 **CPU torch**，且 LF `check_dependencies()` 会拒收 `transformers 5.20.2`（要求 `<=5.6.0`）。
- **处置**：只保留 5 个真正需要的包 —— `datasets 4.0.0`、`trl 0.24.0`、`multiprocess 0.70.16`、`dill 0.3.8`、`fsspec 2025.3.0`（**合计 10.4 MB**）；其余 121 项（**962.9 MB**）**移动**到 `.pylibs-lf\_shadowed_not_on_path\`（不在 `sys.path` 上，**未删除**；去留待 维护者 定）。
- 为什么必须这 5 个：`datasets 4.0.0` 硬约束 `dill<0.3.9`、`fsspec<=2025.3.0`（ComfyUI 是 dill **0.4.0** / fsspec **2026.7.0**，必须由 libs 覆盖）；而 `transformers 4.57.6` 硬约束 `huggingface-hub<1.0`、`tokenizers<=0.23.0`（hub/tokenizers 必须用 ComfyUI 的，不能被 libs 覆盖）。

**坑 2：`torch` 会先 import `dill`（0.4.0）并写进 `sys.modules`** ⇒ 只靠 path 顺序不够；runner 必须在 torch 之前 `import dill`，把 0.3.8 预置进去。已在 `RUNNER_TEMPLATE` 固化。

**runner 最终形态（342 字符）**：
`sys.path.insert(0,<.pylibs-lf>)` → `sys.path.insert(0,<LF src>)` → `import dill` → `sys.argv=['llamafactory-cli','train',<yaml>]` → `from llamafactory.cli import main` → `sys.exit(main())`。
子进程环境：`PYTHONUTF8=1`、`RECORD_VRAM=1`、`PYTHONDONTWRITEBYTECODE=1`、cwd=项目根。

**probe（`--probe-deps`，exit 0）**：`LF_READY_FOR_SFT=True`；`datasets/trl/dill/fsspec/multiprocess` 全部来自 `.pylibs-lf`；
`torch 2.12.0+cu130`（`cuda_available=True`、CUDA 13.0）、`transformers 4.57.6`、`hub 0.36.2` **全部来自 ComfyUI**，`shadowed_from_libs=[]`。

**2 步 smoke（GPU，一臂；两次读数都报）**：

- **第一臂**：训练本身成功（loss 1.7745→2.1596、`train_runtime 0.9743 s`、adapter + `checkpoint-2/` 全落盘），但**进程 exit 1** —— 死在下游收尾：
  PEFT/`huggingface_hub` 的模型卡 `README.md` 被按 **cp936** 写、再按 UTF-8 读（中文项目路径触发）⇒ `UnicodeDecodeError`（**坑 3**）。
- **修复**：① 子进程加 `PYTHONUTF8=1`；② 进度解析改为按 `total` 区分「数据预处理 200/200 条」与「训练 2/2 步」（此前 `step` 被预处理进度条误置成 200）。
- **第二臂（同参数、新目录 `smoke3`）**：**`state=succeeded rc=0`，wall 12.1 s**；`step=2/2`；首/末 loss **1.7745 → 2.1596**、`train_loss 1.9670`、`train_runtime 0.9546 s`；
  **峰值显存 1.36 GiB allocated / 1.57 GiB reserved**（LF `RECORD_VRAM` → `trainer_log.jsonl`）；`trainer_log_lines=3`。
- **可加载**：`PeftModel.from_pretrained`（独立 CPU 进程）**PASS**：336 个 LoRA 张量、r=8/α=16、7/7 target、`logits_diff_max=0.392379`、exit 0。**两臂产物都能重载**。
- 产物：`E:\视频\modelsmith\adapters\_tmp\lf_jobs\smoke2\`（第一臂）、`E:\视频\modelsmith\adapters\_tmp\lf_jobs\smoke3\`（第二臂，推荐引用；
  SHA256 `adapter_model.safetensors 709FEA9D10B4F382…`、`adapter_config.json 3B2D26369FB5CE19…`）。
- 自检：`selftest_lf_adapter.py` **PASS 24 / FAIL 0**（新增回归项「预处理进度条不得抢 step」）。
- 残留：跑后 GPU 回 1307 MiB / 1%、`python.exe` 0（同机残留的 1 个 python 是 core-builder 的 pip dry-run，不是本席）；`third_party\LlamaFactory` `pyc=0`、`git status` 空。

## DiffSynth-Studio（Qwen-Image 2.1 LoRA，**已跑通**）

### 事实（A 级，本机实跑）

- 仓库：`E:\视频\modelsmith\third_party\DiffSynth-Studio`（Apache-2.0，**只读**）；入口 `examples\qwen_image_21\model_training\train.py`（153 行）。
- 解释器：`E:\ComfyUI\python_embeded\python.exe`（torch 2.12.0+cu130）——**零新装**；`sys.path.insert(0, repo)` 后 `runpy.run_path(train.py)`，`cwd=repo`。
- `--model_paths` 是 **JSON 字符串**，解出来 3 项：transformer **2** 分片 / text_encoder **4** 分片 / vae **1**（单文件写成字符串）；`--processor_path` 必须指本地 `%MODEL_DIR%\processor`。
- 已验证 smoke：1 epoch × 4 图 @512、rank 8、`--lora_target_modules ""`（空 = 自动探测）→ **4/4 步、3.88 s/it、`epoch-0.safetensors` 41,999,944 B（≈40.05 MiB）**。
- `build_argv()` 实际 22 个 flag，此处只列高频 3 个（`--model_paths` / `--processor_path` / `--lora_target_modules`）；全表见 `adapters\diffsynth_adapter.py::build_argv()`。
- 该 `train.py` 这版**不出采样图**；验收 = safetensors 落盘 + 事后用 LoRA 跑推理图。

### 适配器与自测

- `adapters\diffsynth_adapter.py`：`expand_model_paths()` / `build_argv()` / `parse_line()` / `run()`；软停 = `CTRL_BREAK_EVENT` → terminate → kill；结束后扫 `epoch-*.safetensors` 入 `metrics["artifact_files"]`。
- `adapters\selftest_diffsynth_adapter.py`：零 GPU，**PASS 70 / FAIL 0**（分片 2/4/1、argv 全字段、空 target、JSON round-trip、tqdm/loss/artifact 解析、成功 + 取消、缺参异常）。
- 配方（可直接复制）：`E:\视频\modelsmith\recipes\qwen_image_21_lora.md`。

### arch 分支（2026-10-22）

| arch | train.py | 必需本地件 | 固定 LoRA 接线 | 备注 |
|---|---|---|---|---|
| `qwen_image_21`（默认） | `examples\qwen_image_21\model_training\train.py` | `processor\` | rank 8（可覆盖）、`dit`、`""`、`pipe.dit.` | argv 逐字段不变（桩回归锁 flag 序列） |
| `moss_image` | `examples\moss_image\model_training\train.py` | `tokenizer\`（**缺了直接报错，不回落 hub**） | rank 32、`dit`、`""`、`pipe.dit.` | h/w **向上对齐 16**、`--max_pixels=h*w`；上游 parser **没有** `--processor_path` / `--initialize_model_on_cpu` |
| `z_image` | `examples\z_image\model_training\train.py` | `tokenizer\`（同上） | rank 32、`dit`、`to_q,to_k,to_v,to_out.0,w1,w2,w3`、`pipe.dit.` | h/w 对齐 16；**offload 默认关**（开了会把顶层 pad token 清成空张量，`x_pad_token` shape [0] 崩）；transformer 是 F32 24.6 GB，上游 `train.py:33` `torch_dtype=bfloat16` + `file.py:31-32` 读入即 cast ⇒ 上卡 ~12.3 GB（CPU 读入峰值仍 24.6 GB） |

- 未知 arch → `ValueError` 点名（`known archs: moss_image, qwen_image_21, z_image`），不猜 train.py。
- **dataset 没有 `metadata.csv` 不会阻塞**：适配器扫描 `<dataset>` 下 jpg/jpeg/png（`dataset_image_exts` 可覆盖），caption 优先取同名 `.txt`、空则用 `a photo of <stem>`（`dataset_default_caption` 可覆盖），生成到 **`<out>\dataset_metadata.csv`**（绝对路径、`/` 分隔），日志打 `# generated dataset metadata: ...`；**不写用户目录**。已有 metadata 缺 `image,prompt` 列会明确报错点名。
- **train.py 由 arch 决定**：`run()` / `build_launch_command()` 只认 `ARCHES[arch]["train_py"]`；调用方传错（例如核心曾固定传 qwen 的路径）会被**忽略并写 WARNING**，不会跑错 parser。回归：`test_launch_command`（三 arch 的 `argv[4]` 断言 + “moss 收到 qwen train_py 仍用 moss”负控）+ `train_py_matches_arch`。
- 16 倍数出处：`examples\moss_image\model_training\train.py:104-105`（`height_division_factor=16` / `width_division_factor=16`）；变更写 `metrics.size_aligned_from`。
- 配方：`recipes\moss_image_lora.md`。
- **Z-Image 实测（A 级，2026-10-22）**：Turbo 本地权重 / 4 图 / 1 epoch / 512² / rank 32 / `use_gradient_checkpointing` / **offload 关** → **succeeded、31.6 s、峰值 21,927 MiB、`epoch-0.safetensors` 158,818,752 B（476 张量）**，`samples\step-2` 73,166 B / 256×256 RGB、`step-4` 72,582 B。transformer 是 F32 24.6 GB，但 `train.py:33` 的 `torch_dtype=bfloat16` + `file.py:31-32` 读入即 cast，上卡 ~12.3 GB。
- **MOSS-Image 实测（A 级，2026-10-22）**：本地 37 文件权重 / 4 图 / 1 epoch / 512²(`max_pixels 262144`) / rank 16 / `use_gradient_checkpointing` → **succeeded、43.0 s、峰值 26,869 MiB、`epoch-0.safetensors` 77,719,832 B**（4/4 步，1.49 s/it）。**不要开 `--enable_model_cpu_offload`**：开了会在 VAE encode 崩（`bn` 统计量为空，`128 vs 0`）；MOSS 需要 `python_paths=[.aitk-pylibs]`（transformers 5.x 才有 `Qwen3_5TextConfig`）。

### 三个坑

1. `--model_paths` 必须是**分片列表**（不是目录；vae 单文件是字符串），顺序按文件名。
2. `--lora_target_modules` 用**空串**自动探测，别抄别的 arch 的 `q,k,v,o,ffn.0,ffn.2`。
3. `--processor_path` 不传会去 HF 拉 `Qwen/Qwen-Image-2.1` 的 processor（会下载）。

### 与 ai-toolkit 的关系

ai-toolkit 的 `arch: qwen_image_2` 在本机走本地 diffusers 目录时稳定崩在 `Cannot copy out of meta tensor`（`quantize.py:402` / `_mixin.py:451`），未在 30 分钟内解决；DiffSynth 是本机**已验证可用**的 Qwen-Image 2.1 通路。适配器接口按核心的 `kind` 路由设计（见 `core\README.md` §15 的 aitk 段，换 `kind="diffsynth"` 即可）。

### 每 N 步采样预览（2026-10-22，A 级）

- 上游 `train.py` **无原生采样回调**，适配器在 `-c` runner 里 monkeypatch `ModelLogger.on_step_end`，用训练进程已加载的 `model.pipe` 就地出图（零重载、可提前中止）。
- 新参数：`sample_every`（默认 200，0=关）、`sample_prompts`、`sample_steps`(20)、`sample_guidance`(1.0)、`sample_seed`(42)、`sample_width/height`(512)；`sample_every>0` 但 prompts 为空 → **警告并自动关闭**。
- 产物：`<out>\samples\step-<N>\*.png`；每张打印 `[sample] step=N file=...`；`metrics.sample_events=[{step, files:[{file,bytes,path}], lora_path}]`，另有 `steps_estimate`。
- 实测：4 图 / `sample_every=2` / 1 prompt / 256×256 / 4 采样步 → **succeeded、63.1 s、峰值 31,945 MiB**、`samples\step-2\sample_00.png` 103,862 B（PNG 256×256 RGBA）、`step-4` 104,188 B。
- 坑：`save_steps` 会抑制 `epoch-*.safetensors`（`runner.py:113-114`）；**`save_steps=0`（或不传）= 只出 epoch 档，`>0` 才传 `--save_steps`**；`resume_from_checkpoint` 只恢复权重、不恢复步号/优化器/数据位置（`training_module.py:466-475`）⇒ 补中间步的图用离线 `validate_lora` 脚本，不要写"续训到 N"。
- **采样后状态复原（必做）**：① `pipe.scheduler.set_timesteps(1000, training=True)` 恢复 `scheduler.training`，否则下一步训练直接在 `qwen_image_21.py:393` 抛 `KeyError: 'use_kv_cache'`；② 采样前记录各 component 的 device 快照、采样后原位 `.to(device)` 复原——整管 `pipe.to("cpu")` 会造成训练 TE 的 CPU/CUDA 混用崩（`Expected all tensors to be on the same device`）。
- **显存**：实测峰值 **31,945 MiB / 32,760 MiB**（Qwen-Image 2.1，256×256、4 采样步）。峰值由 bf16 权重主导（text_encoder 16.2 GB + DiT 13.25 GB），**32 GB 卡上没有余量做 batch>1**；跑采样/训练前必须关掉其它 GPU 程序。

