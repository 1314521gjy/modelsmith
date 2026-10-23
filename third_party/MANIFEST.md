# ModelSmith 第三方源码清单（MANIFEST）

- 清单日期：2026-10-22（Asia/Shanghai）
- 来源：第三方仓库随包固定
- 落地根目录：`E:\视频\modelsmith\third_party\`
- 性质：**只读参考源码**，不是产品运行时依赖；产品通过独立进程调用（见同目录 `README.md`）。
- 本次未做：`pip install` / `npm install` / 训练 / 推理 / GPU；未改任何上游文件。
- 网络：本次 **github.com 直连可用**，四个仓库首次直连即成功，未触发 `gh-proxy.com` 镜像，重试次数 0。
- 所有仓库均为 **浅克隆（`--depth 1`，`git rev-parse --is-shallow-repository` = true）**，保留 `.git` 供复核；`.git` 不计入产品依赖，体积单列。
- 克隆时设置 `GIT_LFS_SKIP_SMUDGE=1`，未下载任何 LFS 大文件；LFS 指针扫描结果见各节。

## 汇总表

| # | 仓库 | 固定 ref | HEAD SHA | 克隆日期 | 总体积（含 .git） | 工作树体积（不含 .git） | LICENSE | SPDX | 状态 |
|---|------|----------|----------|----------|-------------------|--------------------------|---------|------|------|
| 1 | NandhaKishorM/laya | tag `v0.4.13` | `6f0496f797dc7127265d62e1543a62de507d943c` | 2026-10-22 | 52,419,580 B（50.0 MiB） | 42,001,567 B（40.1 MiB） | `LICENSE` | Apache-2.0 | ✅ 成功 |
| 2 | ostris/ai-toolkit | `main` | `14f8f95090689b585093e5aaf48bc1e2e0a67ea4` | 2026-10-22 | 79,512,321 B（75.8 MiB） | 46,743,175 B（44.6 MiB） | `LICENSE` | MIT | ✅ 成功 |
| 3 | hiyouga/LlamaFactory | tag `v0.9.5` | `7af909522a951e3ad9f022ea6f88b6755257eaa5` | 2026-10-22 | 18,976,623 B（18.1 MiB） | 13,281,244 B（12.7 MiB） | `LICENSE` | Apache-2.0 | ✅ 成功 |
| 4 | kohya-ss/sd-scripts（可选 fallback） | `main` | `11d8cedd0c67b93a10f8b2d75f43554f6f12c794` | 2026-10-22 | 26,849,299 B（25.6 MiB） | 20,313,140 B（19.4 MiB） | `LICENSE.md` | Apache-2.0 | ✅ 成功 |

- **四库总体积：177,757,823 B = 169.5 MiB**（含全部 `.git`；工作树合计 122,339,126 B ≈ 116.7 MiB）。
- 失败清单：**无**（因此没有 `CLONE-FAILED.md` 目录）。
- 所有仓库 `.git` 目录创建时间（本地）：laya `2026-10-22 11:45:31`、ai-toolkit `11:45:34`、LlamaFactory `11:45:38`、sd-scripts `11:45:39`。

## tag 预检记录（`git ls-remote --tags`，直连）

- laya：`refs/tags/v0.4.13` 存在，**annotated tag**，tag 对象 `0a68f8938d5f5fc354d4263152a6bee9d138e757`，peeled commit `6f0496f797dc7127265d62e1543a62de507d943c`（克隆后 HEAD 与此一致）。
- LlamaFactory：`refs/tags/v0.9.5` 存在，**lightweight tag**，直接指向 commit `7af909522a951e3ad9f022ea6f88b6755257eaa5`（克隆后 HEAD 与此一致）。
- ai-toolkit：按任务要求使用 `main`（无 release 固定要求），HEAD 见上表。
- sd-scripts：按任务要求使用 `main` 浅克隆。注意：浅克隆内 `git describe --tags` 显示 `v0.12.2`（浅历史中该 tag 恰好可达 HEAD），**本清单不把 v0.12.2 当作固定 tag**，固定依据只有 HEAD SHA。

## 1) NandhaKishorM/laya

- repo URL：https://github.com/NandhaKishorM/laya.git
- 固定 ref：tag `v0.4.13`（annotated；tag 对象 `0a68f8938d5f5fc354d4263152a6bee9d138e757`）
- HEAD SHA：`6f0496f797dc7127265d62e1543a62de507d943c`
- HEAD 提交时间/作者：2026-10-21T19:04:02+05:30 / Nandakishor
- 克隆命令：
  ```
  git -c core.longpaths=true clone --depth 1 --single-branch --branch v0.4.13 https://github.com/NandhaKishorM/laya.git E:\视频\modelsmith\third_party\laya
  ```
- 体积：总 52,419,580 B（50.0 MiB）；工作树 42,001,567 B（40.1 MiB）；`.git` 10,418,013 B（9.9 MiB）；文件数 1,611（含 `.git` 内部文件）。
- 版本元数据：`pyproject.toml` → `version = "0.4.13"`，与 tag 一致。
- LICENSE：`LICENSE`；首行读作 `Apache License`，次行 `Version 2.0, January 2004`，共 176 行 → **SPDX: Apache-2.0**。无 `NOTICE` 文件。
- LICENSE 副本：`E:\视频\modelsmith\third_party\LICENSES\laya-LICENSE`（10,349 B，SHA256 `6D1D968FB225ECA367CB7F0B8831AB012A35D92B547E945E17EF8E7B05C3E5CC`，与源文件逐字节一致）。
- 关键入口：
  - `laya/train_cli.py` ✅（7,418 B，存在）
  - `laya/cli.py` ✅、`laya/serve.py` ✅
  - `pyproject.toml` `[project.scripts]` 声明：`laya`、`laya-serve`、`laya-evals`、`laya-mcp-server`、`laya-train`
  - `setup.py` ✅、`pyproject.toml` ✅
  - Windows 脚本：`start.bat` ✅（根目录，启动 Web UI/API）；另有 `laya-java/gradlew.bat`（JVM SDK 的 Gradle 包装，不是训练入口）
  - 仓库无 `requirements.txt`（依赖在 `pyproject.toml`）
- LFS：`.gitattributes` 只有换行符（`text eol=lf`）规则，**无 `filter=lfs`**；指针文本扫描命中 0。
- 未验项：未安装依赖、未运行 `laya-train`/`start.bat`，入口仅做存在性检查；未验证 Windows 下 CUDA/torch 兼容性；浅历史无法 `bisect` 或查看旧 tag。

## 2) ostris/ai-toolkit

- repo URL：https://github.com/ostris/ai-toolkit.git
- 固定 ref：`main`（无 release 固定要求）
- HEAD SHA：`14f8f95090689b585093e5aaf48bc1e2e0a67ea4`
- HEAD 提交时间/作者：2026-10-21T21:17:12-06:00 / Jaret Burkett
- 克隆命令：
  ```
  git -c core.longpaths=true clone --depth 1 --single-branch https://github.com/ostris/ai-toolkit.git E:\视频\modelsmith\third_party\ai-toolkit
  ```
- 体积：总 79,512,321 B（75.8 MiB）；工作树 46,743,175 B（44.6 MiB）；`.git` 32,769,146 B（31.3 MiB）；文件数 863。
- 版本元数据：`version.py` → `VERSION = "0.13.36"`。
- LICENSE：`LICENSE`；首行读作 `MIT License`，`Copyright (c) 2024 Ostris, LLC`，共 21 行 → **SPDX: MIT**。无 `NOTICE` 文件。
- LICENSE 副本：`E:\视频\modelsmith\third_party\LICENSES\ai-toolkit-LICENSE`（1,089 B，SHA256 `19848C085CC6E5C354142C67ED67B3E399028E49982546E5C3AB01223A6AA565`，逐字节一致）。
- 关键入口：
  - `run.py` ✅（根目录主入口）
  - `run_windows.bat` ✅（Windows 启动脚本：引导 uv + 调用 `python -m manager`，**本身不装依赖**，运行它才会装；本次未运行）
  - `requirements.txt` ✅、`requirements_base.txt` ✅
  - `manager/` ✅、`toolkit/` ✅、`ui/` ✅
  - 无 `setup.py` / `pyproject.toml`（用 requirements 安装，未安装）
- 子模块：`.gitmodules` 存在但**长度为 0 字节**；`git ls-files -s` 中 gitlink（mode 160000）数量 0 → 无未初始化子模块。
- LFS：无 `.gitattributes`；指针文本扫描命中 0。
- 未验项：未安装依赖、未运行 `run.py`/`run_windows.bat`（该脚本会联网安装 uv/Python/依赖，明确不执行）；未验证 GPU 训练配置与模型兼容性；浅历史不可追溯旧版本。

## 3) hiyouga/LlamaFactory

- repo URL：https://github.com/hiyouga/LlamaFactory.git
- 固定 ref：tag `v0.9.5`（lightweight）
- HEAD SHA：`7af909522a951e3ad9f022ea6f88b6755257eaa5`
- HEAD 提交时间/作者：2026-05-30T23:57:09+08:00 / Yaowei Zheng
- 克隆命令：
  ```
  git -c core.longpaths=true clone --depth 1 --single-branch --branch v0.9.5 https://github.com/hiyouga/LlamaFactory.git E:\视频\modelsmith\third_party\LlamaFactory
  ```
- 体积：总 18,976,623 B（18.1 MiB）；工作树 13,281,244 B（12.7 MiB）；`.git` 5,695,379 B（5.4 MiB）；文件数 564。
- 版本元数据：`pyproject.toml` 项目名 `llamafactory`；tag 即锁版本，`pyproject.toml` 未写静态 `version`（由构建后端/标签管理；未实测）。
- LICENSE：`LICENSE`；首行读作 `Apache License`，次行 `Version 2.0, January 2004`，共 201 行 → **SPDX: Apache-2.0**。无 `NOTICE` 文件。
- LICENSE 副本：`E:\视频\modelsmith\third_party\LICENSES\LlamaFactory-LICENSE`（11,525 B，SHA256 `313605FBAC6945E9324D4825470796B5B7DBC012F523FDC181F6E6FD234EB88F`，逐字节一致）。
- 关键入口：
  - **实际路径是 `src/llamafactory/cli.py` ✅**（该 tag 采用 `src/` 布局；不存在根目录 `llamafactory/cli.py`）
  - `pyproject.toml` `[project.scripts]` 声明 `llamafactory-cli = "llamafactory.cli:main"` ✅
  - `src/llamafactory/` ✅、`requirements/` ✅、`data/` ✅、`examples/` ✅
  - Windows 脚本：根目录**无** `.bat` 启动脚本；扫描到的 `docs/make.bat` 只是 Sphinx 文档构建，不是训练/推理入口
- LFS：`.gitattributes` 仅 `* text=auto`（换行归一化），**无 `filter=lfs`**；指针文本扫描命中 0。
- 未验项：未安装依赖（`src` 布局需要 `pip install -e .` 类安装，未执行）；未验证 `llamafactory-cli` 可运行性；tag 提交日期为 2026-05-30，明显早于当前 main，选它是有意按任务锁定，不是最新状态；浅历史不可追溯。

## 4) kohya-ss/sd-scripts（可选 fallback）

- repo URL：https://github.com/kohya-ss/sd-scripts.git
- 固定 ref：`main`（浅克隆）
- HEAD SHA：`11d8cedd0c67b93a10f8b2d75f43554f6f12c794`
- HEAD 提交时间/作者：2026-10-20T23:22:07+09:00 / Kohya S.
- 克隆命令：
  ```
  git -c core.longpaths=true clone --depth 1 --single-branch https://github.com/kohya-ss/sd-scripts.git E:\视频\modelsmith\third_party\sd-scripts
  ```
- 体积：总 26,849,299 B（25.6 MiB）；工作树 20,313,140 B（19.4 MiB）；`.git` 6,536,159 B（6.2 MiB）；文件数 337。
- LICENSE：`LICENSE.md`（注意扩展名是 `.md`）；首行读作 `Apache License`，次行 `Version 2.0, January 2004`，共 201 行 → **SPDX: Apache-2.0**。无 `NOTICE` 文件。
- LICENSE 副本：`E:\视频\modelsmith\third_party\LICENSES\sd-scripts-LICENSE`（11,543 B，SHA256 `FAEB63342DDF1231A3ED1ABD8C64C98F5D9C24388CE9B931B081F8BB557D84D7`，逐字节一致；内容来自 `LICENSE.md`）。
- 关键入口：
  - `train_network.py` ✅（100,492 B）、`fine_tune.py` ✅、`sdxl_train.py` ✅、`flux_train_network.py` ✅、`anima_train.py` ✅
  - `setup.py` ✅、`requirements.txt` ✅
  - Windows 脚本：根目录无 `.bat`；仅有 `tests/run_sd15_inpainting_test.ps1`、`tests/run_sdxl_inpainting_test.ps1`（测试脚本，非启动入口）
- LFS：无 `.gitattributes`；指针文本扫描命中 0。
- 未验项：未安装依赖、未运行任何训练脚本；未验证 `bitsandbytes`/`torch` 在 Windows 下可用性；`main` 未打 tag 固定，后续如需复现只能用本 SHA；浅历史不可追溯。

## 验收读数（可复核命令）

在 `E:\视频\modelsmith\third_party\<repo>` 内执行：

```
git rev-parse HEAD                  # 必须等于本清单 SHA
git rev-parse --is-shallow-repository  # true
git describe --tags --always        # laya=v0.4.13, LlamaFactory=v0.9.5, ai-toolkit=14f8f95, sd-scripts=v0.12.2(浅历史巧合)
git ls-files -s | findstr "^160000" # 期望 0 行（无未初始化 gitlink）
```

本次实际读数：

| 仓库 | `rev-parse HEAD` | `.git` 存在 | 工作树是否干净（`git status --porcelain`） | LICENSE 副本非空 |
|------|------------------|-------------|--------------------------------------------|------------------|
| laya | `6f0496f797dc7127265d62e1543a62de507d943c` | 是（目录） | 干净（0 行） | 是，10,349 B |
| ai-toolkit | `14f8f95090689b585093e5aaf48bc1e2e0a67ea4` | 是（目录） | 干净（0 行） | 是，1,089 B |
| LlamaFactory | `7af909522a951e3ad9f022ea6f88b6755257eaa5` | 是（目录） | 干净（0 行） | 是，11,525 B |
| sd-scripts | `11d8cedd0c67b93a10f8b2d75f43554f6f12c794` | 是（目录） | 干净（0 行） | 是，11,543 B |

**复核结论（2026-10-22 本地脚本实测）**：4/4 仓库 `rev-parse HEAD` 与 MANIFEST 记录一致；4/4 LICENSE 副本尺寸与 SHA256 与记录一致；`git status --porcelain` 均为 0 行。脚本输出 `ALL_PASS=True`。

### 负控（判据会变红）

- SHA 一致性判据：把 `laya` 的 `rev-parse HEAD` 与 `ai-toolkit` 的清单 SHA 比较，**必然不相等**；若某仓库 SHA 与清单不符，脚本会打印 MISMATCH。已用「跨仓库 SHA 交叉比较」验证比较逻辑本身能判否。
- LICENSE 判据：副本与源文件做 SHA256 比对（相等才通过）；空文件会因 0 字节在尺寸检查处变红。
- `.git` 存在判据：用 `Test-Path <repo>\.git`（隐藏目录需 `-Force`）；`git -C` 能读出 SHA 即说明对象库可用。

## 未验项（总）

1. **可运行性未验**：四个仓库都未安装依赖、未 import、未跑任何入口脚本（按要求）。
2. **GPU/训练未验**：未跑训练/推理，未验证 CUDA、bitsandbytes、torch 版本兼容性。
3. **镜像通道未验**：本次直连成功，`https://gh-proxy.com/https://github.com/<owner>/<repo>.git` 未实际使用（保留为下次的备选）。
4. **LFS**：未下载任何 LFS 对象；扫描口径是「<4 KB 文本文件中查找 `git-lfs.github.com/spec`」，四个仓库均 0 命中且无 `filter=lfs` 属性，但未做全字节级 LFS 服务端校验。
5. **历史完整性**：全部 `--depth 1` 浅克隆，未做 `git fsck` 全量对象校验，不能 `git bisect` 或 checkout 旧 tag。
6. **GitHub 元数据**：未用 GitHub API 交叉校验 release 名/许可证；许可证一律以本地 `LICENSE` 首行与全文为准。
7. **产品接入**：未验证 ModelSmith 产品侧如何以独立进程调用这些源码（不在本任务范围）。
8. **sd-scripts 固定方式**：只有 `main` 的 SHA 锁定，没有 tag；浅克隆里 `describe` 显示的 `v0.12.2` 不作为锁版本依据。

## 目录约定

- `third_party/<repo>/`：只读参考源码（保留 `.git`）。
- `third_party/LICENSES/<repo>-LICENSE`：许可证副本（`<repo>` 取 laya / ai-toolkit / LlamaFactory / sd-scripts）。
- `third_party/MANIFEST.md`：本文件（版本与许可的唯一台账）。
- `third_party/README.md`：用途与只读声明。
- 失败仓库（本次无）：应建 `third_party/<repo>/CLONE-FAILED.md` 记录 URL、命令、HTTP/错误与重试次数——本次四个仓库均成功，不存在此类目录。
