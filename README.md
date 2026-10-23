# ModelSmith · 模匠

**Windows 本地优先、免安装的训练工作台**：一个本地 Web UI + 一个常驻核心服务，把「决策模型 / LLM 微调 / 生图 LoRA」三条线的真实训练后端串起来。长任务走作业表，带 GPU 串行锁、日志尾随、取消与崩溃恢复。

> 本仓库只发**训练方案**：代码、适配器、配方、界面、文档，以及两份可再分发的客服决策语料。
> **不含模型权重**——权重自备，许可自担。

## 1 已验证的训练路径

| 线 | 后端（许可证） | 实测读数 |
|---|---|---|
| 决策模型 | Laya v0.4.13（Apache-2.0） | 22 updates、exit 0、产物 842.6 MB |
| LLM 微调 | LlamaFactory v0.9.5（Apache-2.0） | loss 3.8127→2.6485、峰值 1.22 GiB、adapter 17,640,136 B、wall 22 s |
| 生图 LoRA · SD1.5 | ai-toolkit（MIT） | 1 步 exit 0，11.87 s，LoRA 6.7 MB |
| 生图 LoRA · Qwen-Image 2.1 | DiffSynth-Studio（Apache-2.0） | 4/4 步、3.87 s/it、产物 `epoch-0.safetensors` 41,999,944 B（448 tensors） |
| 图 → 字幕（打标） | `llama-server` + 视觉 GGUF（自备） | 6 图 batch 写出 6 个 `.txt`，无 OOM |

链路一律是：**浏览器 → HTTP 核心服务 → 子进程训练后端 → 产物落盘**，页面只读作业状态。

## 2 快速开始

**环境**（Windows 10/11 x64，NVIDIA 显卡）：

- 训练用的 Python 需要带 `torch`（CUDA 版）。可以用任何现成的嵌入式 Python，也可以自己建 venv；核心服务本身是 **stdlib only**，系统 Python 就能跑。
- 第三方训练后端源码树**不进本仓库**，按 `third_party/MANIFEST.md` 固定 SHA 自取。

**起服务**：

```powershell
python core\modelsmith_core.py --port 0    # 随机端口，自动写 %LOCALAPPDATA%\ModelSmith\core.json
python core\modelsmith_core.py             # 固定 127.0.0.1:8765
```

浏览器打开 `http://127.0.0.1:<port>/`。核心在内存里注入 `window.__MS_TOKEN__`（磁盘上的 `design/prototype/index.html` 保持原字节）；token 文件在 `%LOCALAPPDATA%\ModelSmith\token`，接口鉴权用 `X-ModelSmith-Token` 头。

**三条线各自的前置**：

- 决策：本地 Laya 底座目录（`--base` 必须是本地目录，不能给 Hub id）+ JSONL/CSV 数据。
- LLM：`third_party/LlamaFactory` + `.pylibs-lf`（datasets / trl / dill / fsspec / multiprocess）+ dolly 或 alpaca 形状的 JSONL。
- 生图：`third_party/DiffSynth-Studio` + 本地 diffusers 目录（分片结构）+ 图片文件夹与 `metadata.csv`（列 `image,prompt`）。

## 3 架构

```
本地 Web UI（单文件，零外链） ──HTTP──► 核心服务 127.0.0.1:8765
                                          │ SQLite WAL 作业表 / GPU 锁 / 子进程 / 日志 offset 尾随
                                          ├── Laya        (kind="laya")
                                          ├── LlamaFactory(kind="lf")
                                          ├── 备用 runner (kind="llm")     ← 自研，回归基线
                                          ├── ai-toolkit  (kind="aitk")
                                          ├── DiffSynth   (kind="diffsynth")
                                          └── llama-server(kind="caption")  ← 图 → 同名 .txt
```

任务用应用层 `job_id` 标识；客户端退出不影响训练；写一半的作业在重启时标成 `interrupted`。

## 4 目录

| 路径 | 内容 |
|---|---|
| `core/` | 核心服务（stdlib only）+ 自测 + API/路由说明 |
| `adapters/` | 六个适配器 + 各自零 GPU 自测：`laya` / `lf` / `aitk` / `diffsynth` / `llm_lora`（备用）/ `caption` |
| `apps/cs-demo/` | 客服决策展示台：判定（情绪/目的/动作）+ 模板话术，独立进程 |
| `datasets/` | 两份可再分发语料：`laya-cs-own`（自有）、`cs-decision`（Apache-2.0 / CC BY 4.0 的改写派生） |
| `recipes/` | 可直接复制的训练配方 |
| `design/` | 单文件 UI 原型、美术层、设计规范 |
| `docs/` | 契约、流程、研究笔记、训练手册 |
| `skills/` | 给 agent 用的两个 skill：语料构造、MCP 调用 |
| `dist/` | 打包器（`pack.py`）、清单生成器（`collect.py`）、运行时盘点 |
| `third_party/` | 上游清单与 LICENSE 副本（源码树不进仓库） |
| `resources/` | 语料与图集的说明书与指针（本体单独打包） |

## 5 语料

| 目录 | 规模 | 来源与许可 |
|---|---|---|
| `datasets/laya-cs-own/` | train 414 / eval 115（281 个模板） | 自行撰写与人工标注，随本仓库 `LICENSE` 分发 |
| `datasets/cs-decision/` | train 649 / val 116 | `LocalLLaMA/typed-decisions`（Apache-2.0）+ Amazon MASSIVE / `zh-decision-bench`（CC BY 4.0）的中文改写派生；署名与「已修改」声明见 `NOTICE.md` |

两份都是 Laya typed-decision / alpaca 可直接读的 JSONL，字段说明见各自目录的 `README.md`。

## 6 第三方组件与许可

| 组件 | 固定点 | 许可证 | 仓库是否含源码 |
|---|---|---|---|
| Laya | v0.4.13 | Apache-2.0 | 否（`third_party/MANIFEST.md`） |
| LlamaFactory | v0.9.5 | Apache-2.0 | 否 |
| ai-toolkit | main@14f8f950… | MIT | 否 |
| sd-scripts | main@11d8cedd… | Apache-2.0 | 否（备用对照） |
| DiffSynth-Studio | 本机克隆 | Apache-2.0 | 否 |
| llama.cpp | prebuilt | MIT | 否（由使用者自备可执行文件） |

本项目代码：MIT（见 `LICENSE`）；第三方许可见 `third_party/LICENSES/` 与 `NOTICE.md`。

## 7 自测（零 GPU）

```powershell
python core\selftest_core.py                    # 118 PASS / 0 FAIL（约 31 s）
python adapters\selftest_laya_adapter.py        # 29 / 0
python adapters\selftest_aitk_adapter.py        # 31 / 0
python adapters\selftest_diffsynth_adapter.py   # 70 / 0
python mcp_server\selftest_mcp.py               # MCP 握手与 19 个工具
```

`selftest_llm.py` / `selftest_lf_adapter.py` 需要带 `torch` 的 Python（见 §2）。

## 8 打包分发

```powershell
python dist\collect.py      # 盘点本机模型与运行时（不代表进包）
python dist\pack.py --dry-run   # 只打印计划
python dist\pack.py             # 产出 dist\out\ 下的 zip
```

打包器**硬排除**权重与临时目录（`*.safetensors` / `*.gguf` / `_tmp/` / `_runs/` / `.aitk-pylibs/` / 第三方源码树），命中即报错退出，不静默跳过。详见 `dist/PACK-README.md`。

## 9 已知限制

- 只做到**路径证明**级别的 smoke：决策 22 updates、LLM 2 步、生图 4 步；长训质量与显存准入未验。
- 核心没有显存准入；GPU 锁只保证串行。
- Qwen-Image 2.1 该版 `train.py` 不出采样图（只存 safetensors），验收看 loss / 步时 / 产物。
- 打标只在 `llama.cpp` 的 MTMD 视觉通路上验证过；其它视觉模型未验。
- UI 冻结版 `design/prototype/index.html` 只读；改动走「快照 → 解锁 → 小块改 → 静态校验 + headless 回归 → 再锁」。
