# R13 分发运行时（RUNTIME-PACK）

2026-10-22 · 目标：主包内自带 Python + 训练依赖，解压即用。运行时已 `--apply` 到 `dist\runtime\`；全量运行时不进 zip（用户端跑 `pack_runtime.py --apply --allow-large` 生成）。
- 产物：`E:\视频\modelsmith\dist\RUNTIME-PACK.md`、`pack_runtime.py`、`start-core.cmd`、`runtime-manifest.json`、`runtime-licenses\`、`runtime\pylibs-5\`
## 0 用户路径口径（实测确认）
- 运行时包解压到 **`C:\ModelSmith\runtime`**（ASCII 路径），状态目录 **`C:\ModelSmith\state`**；日志 `C:\ModelSmith\state\logs\launcher.log`。
- `start-core.cmd` 查找顺序：**`MODELSMITH_RUNTIME` → `C:\ModelSmith\runtime` → `dist\runtime` → `runtime`**；以 `--data-dir "C:\ModelSmith\state"` 启动。
- 仍保留 `dist\runtime` 只作为本机 staging/测试；最终用户不要解到 `E:\视频\...` 这类中文路径。
## 1 目标形态
```
ModelSmith-<date>/
├─ start-core.cmd                    ASCII + CRLF；设 PYTHONHOME/PYTHONPATH/PYTHONUTF8/PYTHONNOUSERSITE
├─ core\modelsmith_core.py
├─ runtime\
│  ├─ python\python.exe + python312.dll + python312.zip + *.pyd/*.dll
│  ├─ python\python312._pth          python312.zip / . / ..\site-packages / 3 个 third_party 路径 / import site
│  ├─ site-packages\                 61 个运行时包（逐文件见 runtime-manifest.json）
│  ├─ pylibs-5\                      MOSS-Image 用 transformers 5.x 隔离目录（不在 manifest 内）
│  └─ licenses\<pkg>\                60 个包的许可证原文（52+3+5）
├─ third_party\DiffSynth-Studio\     core 用 --diffsynth-src 指；pylibs-5 的搜索路径也含它
└─ design\ core\ docs\ ...
```
`start-core.cmd` 额外把 core 的 5 个 `--*-python` 指到 `%MS_PY%`；`--diffsynth-libs` 默认会找 `<package-root>\runtime\pylibs-5`（开发树回退 `.aitk-pylibs`）。
## 2 实测体积（本机，A 级）
| 项 | 字节 | 说明 |
|---|---:|---|
| `E:\ComfyUI\python_embeded` 全量 | **13,241,901,444**（12.33 GiB） | 187,137 files |
| `Lib\site-packages` | **12,486,616,853**（11.63 GiB） | 183,672 files |
| `torch` | **2,925,651,321** | `torch/lib` 2,787,134,320；`include` 39,681,811；`.lib` 49,325,852 |
| `transformers` | 107,028,248 | 4.57.6 |
| `peft` / `diffusers` / `accelerate` | 3,784,385 / 49,189,374 / 2,653,977 | 0.18.0 / 0.39.0 / 1.12.0 |
| `safetensors` / `numpy` / `pyarrow` | 856,100 / 32,570,879 / 91,737,211 | numpy/pyarrow 来自 user site |
| `modelscope` / `pandas` / `torchvision` | 49,667,065 / 44,982,756 / 26,609,852 | 1.34.0 / 2.3.3 / 0.27.0+cu130 |
| 被排除大件 | tensorrt_libs 2,326,274,520；nvvfx 830,279,450；onnxruntime 410,169,824；llama_cpp 368,896,545；nunchaku 268,517,421；jaxlib 225,804,983；bitsandbytes 211,083,870；triton 145,677,975；scipy 119,449,206；cv2 111,518,017 | 不进最小集 |
## 3 最小集与 apply 读数
- `pack_runtime.py --dry-run` / `--apply` 计划：**17,103 entries / 3,193,406,242 B（2.974 GiB）**；解释器 base 21,907,134 B / 34 files；包+dist-info+许可 3,171,499,108 B；许可证副本 1,415,056 B；另加 `runtime-src\pylibs-5.zip` 11,355,705 B（解压 46,315,389 B）。
- `--apply --allow-large --force`：17,103 files + `pylibs-5` 2,626 files，25.4 s（首次 42.6 s），`APPLY_EXIT=0`；`--apply --only pylibs-5` 单独重建 **1.15 s**；`dist\runtime\python\python.exe` 存在。
- 当前 `dist\runtime\` 实际：**3,315,512,554 B / 22,913 files（3.09 GiB）**；其中 `pylibs-5` 46,315,389 B / 2,626 files，`licenses` 1,432,876 B / 117 files（60 个目录）。运行时目录另含不在 manifest 的外部新增文件。
- 压缩后体积**未测**；按 CUDA DLL 占比粗估 1.4–2.0 GiB（D 级，不作承诺）。
## 4 `runtime\pylibs-5`（MOSS-Image / transformers 5.x）
- 用途：MOSS-Image 需要 `Qwen3_5TextConfig`（transformers 5.x），而运行时的通用 `site-packages` 是 4.57.6；`pylibs-5` 插到 `sys.path` 前部，只覆盖 5.x 依赖。
- 内容（46,315,389 B / 2,626 files）：transformers 5.5.3（41,560,030 B）、huggingface_hub 1.23.0（2,971,639）、httpx 0.28.1（284,396）、httpcore 1.0.9（261,935）、h11 0.16.0（93,124）、anyio 4.15.1（529,017）、sniffio 1.3.1（5,325）；**无** torch/torchvision/functorch（已断言三项均不存在）。
- 来源/自动生成：`E:\视频\modelsmith\dist\runtime-src\pylibs-5.zip`（**11,355,705 B / 2,626 entries / SHA256 `AD3E06B242744B0190AD13E36C0B6AD8768F7572FE5ED2DE75DCA7F0A13FB6F0`**）；`pack_runtime.py --apply` 自动解压到 `runtime\pylibs-5\`，`--apply --only pylibs-5` 可单独重建（1.15 s）；`pack.py` 把 zip 收进主包。
- 手工验证（本机实测，exit 0）：
```powershell
cd E:\视频\modelsmith
$env:PYTHONDONTWRITEBYTECODE=1
.\dist\runtime\python\python.exe -B -c "import sys; sys.path.insert(0, r'dist\runtime\pylibs-5'); import transformers; print(transformers.__version__); import torch; print(torch.__version__, torch.cuda.is_available()); from transformers import Qwen3_5TextConfig"
# 输出：5.5.3 / 2.12.0+cu130 True / Qwen3_5TextConfig OK
```
- 10.4 MB 的 `datasets 4.0.0 / trl 0.24.0 / multiprocess / dill 0.3.8 / fsspec` 已在通用 `site-packages`（本包 52+3 许可表覆盖）；`pylibs-5` 增量主要是 transformers 5.x 本体。
## 5 许可清单（SPDX + 能否随包）
| SPDX | 包（本包版本） | 能否随包 | 许可证原文 |
|---|---|---|---|
| PSF-2.0 | Python 3.12.12；typing_extensions 4.15.0 | 可（保留 PSF 文本/版权） | https://docs.python.org/3/license.html |
| BSD-3-Clause | torch 2.12.0+cu130、torchgen、functorch、torchvision 0.27.0+cu130、numpy 2.5.4、pandas 2.3.3、protobuf 4.25.8、idna 3.11、psutil 7.1.3、sympy 1.14.0、networkx 3.5、jinja2 3.1.6、markupsafe 2.1.5、mpmath 1.3.0、contourpy 1.3.3、cycler 0.12.1、kiwisolver 1.4.9、antlr4 4.9.3、fsspec 2025.3.0、dill 0.3.8、multiprocess 0.70.16 | 可（保留版权/免责声明） | https://opensource.org/license/bsd-3-clause |
| Apache-2.0 | transformers 4.57.6 + 5.5.3、tokenizers 0.22.1、huggingface_hub 0.36.2 + 1.23.0、safetensors 0.8.0、peft 0.18.0、accelerate 1.12.0、diffusers 0.39.0、sentencepiece 0.2.1、tzdata 2025.2、datasets 4.0.0、trl 0.24.0、pyarrow 26.0.0、modelscope 1.34.0 | 可（保留 LICENSE/NOTICE） | https://www.apache.org/licenses/LICENSE-2.0 |
| MIT | PyYAML、charset_normalizer、urllib3、filelock、einops、six、setuptools、_distutils_hack、fonttools、pyparsing、pytz、h11、anyio、sniffio | 可（保留版权/许可） | https://opensource.org/license/mit |
| MIT-CMU | Pillow 11.3.0 | 可（保留 HPND/MIT-CMU） | https://github.com/python-pillow/Pillow/blob/main/LICENSE |
| MPL-2.0 / MPL-2.0 AND MIT | certifi 2025.11.12 / tqdm 4.67.1 | 可（保留文本；未修改 MPL 文件） | https://www.mozilla.org/en-US/MPL/2.0/ · https://github.com/tqdm/tqdm/blob/master/LICENCE |
| Apache-2.0 AND CNRI-Python | regex 2025.11.3 | 可（保留双许可文本） | https://github.com/mrabarnett/mrab-regex/blob/master/LICENSE.txt |
| Apache-2.0 OR BSD-2-Clause | packaging 25.0 | 可（择一保留） | https://github.com/pypa/packaging/blob/main/LICENSE |
| Apache-2.0 OR BSD-3-Clause | python-dateutil 2.9.0.post0 | 可（择一保留） | https://github.com/dateutil/dateutil/blob/master/LICENSE |
| BSD-2-Clause / BSD-3-Clause | imageio 2.37.2、xxhash 3.6.0 / httpx 0.28.1、httpcore 1.0.9 | 可（保留版权/免责声明） | https://opensource.org/license/bsd-2-clause · https://opensource.org/license/bsd-3-clause |
| PSF-2.0（Matplotlib 自定义 BSD 风格） | matplotlib 3.10.7 | 可（保留 dist-info LICENSE） | https://github.com/matplotlib/matplotlib/blob/main/LICENSE/LICENSE |
| Apache-2.0（补原文） | tokenizers 0.22.1（11,357 B，SHA256 `C71D239DF91726FC519C6EB72D318EC65820627232B2F796219E87DCF35D0AB4`，https://raw.githubusercontent.com/huggingface/tokenizers/v0.22.1/LICENSE） | 可（原文已落入 `dist\runtime-licenses\tokenizers\LICENSE`） | 同上 |
| Apache-2.0（补原文） | sentencepiece 0.2.1（11,358 B，SHA256 `CFC7749B96F63BD31C3C42B5C471BF756814053E847C10F3EB003417BC523D30`，https://raw.githubusercontent.com/google/sentencepiece/v0.2.1/LICENSE） | 可（原文已落入 `dist\runtime-licenses\sentencepiece\LICENSE`） | 同上 |
| BSD-3-Clause（补原文） | antlr4 4.9.3（2,699 B，SHA256 `B1B379FCAF3219593A4C433FEB1B35C780BED23FAFAAE440B1AE2771A9521E3A`，https://raw.githubusercontent.com/antlr/antlr4/4.9.3/LICENSE.txt） | 可（原文已落入 `dist\runtime-licenses\antlr4\LICENSE`） | 同上 |
| 随包（分发方承担） | `torch/lib` 内 NVIDIA CUDA/cuDNN DLL（cublasLt 477,896,816；cudnn_* 约 4.4 亿；cufft 284,331,040 等） | **保留在运行时包内**；依据 NVIDIA CUDA EULA Attachment A 与 cuDNN SLA，**由分发方（我们）承担合规责任**（用户已确认 CUDA 备好），**不再逐文件核**；公开分发前建议法务复核 | https://docs.nvidia.com/cuda/eula/index.html · https://docs.nvidia.com/deeplearning/cudnn/sla/index.html |
- PyTorch 代码：https://github.com/pytorch/pytorch/blob/main/LICENSE（BSD-3-Clause）；`torch-2.12.0+cu130.dist-info\LICENSE/NOTICE` 已复制。
- `runtime\licenses\` 现 **60 个目录**：基础 52 + 补的 3（tokenizers/sentencepiece/antlr4）+ `pylibs-5` 新增 5（httpx/httpcore/h11/anyio/sniffio）；`datasets`/`trl` 的 Apache-2.0 在基础 52 内。
- `runtime-manifest.json` 的 `license` 是**包级 SPDX**；包内 bundled 二进制（CUDA/cuDNN、libpng/zlib/libjpeg/libwebp/nvjpeg）未逐文件核，按上述分发方责任声明处理。
## 6 风险
1. **`._pth` 隔离 + 中文路径已知坑**：存在 `._pth` 时 `PYTHONPATH` 被忽略（实测），真正生效的是 `..\site-packages` 行；`start-core.cmd` 另设 `PYTHONNOUSERSITE=1`。**运行时不要放在中文路径下**：用户实测 embed Python 在中文路径创建 sqlite 状态库会报 `unable to open database file`，必须用 `C:\ModelSmith\runtime` + `C:\ModelSmith\state`。
2. **CUDA DLL 体积**：`torch/lib` 2,787,134,320 B，占最小集约 87%；本次保留全部 CUDA/cuDNN DLL，未裁剪；裁 DLL 属未验。
3. **Windows torch 再分发**：PyTorch 代码 BSD-3；wheel 内 NVIDIA 二进制按 CUDA EULA Attachment A 与 cuDNN SLA **由分发方承担**；保留版权/NOTICE，公开分发前建议法务复核。
4. **VC++ 运行库**：Python base 只带 `vcruntime140.dll`/`_1`，不带 `msvcp140.dll`；干净 Windows 需 VC++ 2015–2022 x64（未核/目标机验）。
5. **源码/额外运行时**：主包当前不装 `third_party\laya`、`third_party\LlamaFactory`；`pylibs-5` 由 `runtime-src\pylibs-5.zip` 自动解压，zip 由 `pack.py` 收进主包；主包 `pack.py` 只放 `start-core.cmd` 到包根，运行时本体不进 zip（独立运行时包另发）。
6. **ai-toolkit**：需 `transformers==5.5.3` + 指定 diffusers，独立 venv；本包 `pylibs-5` 只服务 MOSS/transformers 5.x，不替代 ai-toolkit 环境。
## 7 自检读数
- `dist\start-core.cmd --help`：exit 0；`start-core.cmd` 557 B、13 CRLF、0 lone LF、0 非 ASCII（SHA256 `426F4F0B2B32E7D212557200EA2A10081DA6B60D3DDB855F30E4370C0AEA4BFC`）。
- runtime python 直接验证：`import torch,transformers,diffsynth` → `2.12.0+cu130 True 4.57.6`，exit 0。
- `pylibs-5` 验证：`5.5.3 / 2.12.0+cu130 True / Qwen3_5TextConfig OK`，exit 0；`pylibs-5` 内无 torch/torchvision/functorch。
- 起 `start-core.cmd --port 0 --data-dir dist\_tmp\core-health-data`：`GET /health` **200**，body `{"ok":true,...,"db":"wal"}`；测试后 `taskkill /T /F`，cmd 与 python 进程均退出，端口 0 个 listener。
## 8 未做 / 未验
- 全量运行时 zip 压缩后体积；干净 Windows / 断网 / VC++ 运行库。
- 三条训练线真实 GPU 训练（本次禁止）；`torch.compile`/triton（triton 未纳入）。
- `pylibs-5` 的 `transformers 5.x` 真跑 MOSS 权重（未下载权重，只验证 import/Qwen3_5TextConfig）。
- Laya/LlamaFactory 源码是否进主包（维护者 决策）；运行时独立包（`dist\out\ModelSmith-运行时-*.zip`）已另做，见 `PACK-README.md`。
- 公开分发前的 CUDA/cuDNN 法务复核（分发方责任声明已写，不代替法务意见）。
