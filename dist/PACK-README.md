# ModelSmith 打包说明（PACK-README）

**三个包**：方案包/数据包由 `python dist\pack.py` 生成到 `dist\out\`；**运行时包单独打包**（本文件同时收进主包）。
口径唯一来源：`dist\README.md` + `dist\RUNTIME-PACK.md`。
运行时由 `pack_runtime.py --apply --allow-large` 在 `dist\runtime\` 生成（~3 GiB，**不进 git、不塞进方案包**），再打成独立运行时 zip；`pylibs-5` 由 `dist\runtime-src\pylibs-5.zip` 自动解压，zip 随方案包分发。

## 用法

```powershell
# 0) 先刷新全量清单（dist\manifest.json，会被主包收走）
E:\ComfyUI\python_embeded\python.exe E:\视频\modelsmith\dist\collect.py
# 1) 只打印计划，不写盘
E:\ComfyUI\python_embeded\python.exe E:\视频\modelsmith\dist\pack.py --dry-run
# 2) 真打包
E:\ComfyUI\python_embeded\python.exe E:\视频\modelsmith\dist\pack.py
# 3) 排除扫描负控（15 条硬排除用例 + 3 条允许用例，实测 PASS 18 / FAIL 0）
E:\ComfyUI\python_embeded\python.exe E:\视频\modelsmith\dist\pack.py --selftest-exclude
```

## 三包读数（2026-10-22 实测）

| 包 | 文件 | 源体积 | zip 体积 | zip 条目 | `testzip()` |
|---|---|---|---|---|---|
| `ModelSmith-方案-20261022.zip`（方案包） | 1,745（旧读数；本次新增 runtime 条目后下次重打） | 12.76 MB | ≈ 5.15 MB | 1,747 | `None` |
| `ModelSmith-数据包-20261022.zip`（数据包） | 44 | 20.81 MB | 7.11 MB | 46 | `None` |
| `ModelSmith-运行时-20261022.zip`（运行时包，**单独发**） | 22,914（22,913 runtime + README） | 3,315,512,554 B（3.09 GiB） | **2,076,824,996 B（1.93 GiB）** | 22,914 | `None` |

方案包/数据包根目录有 `PACK-MANIFEST.json` + 本 README；运行时包根目录有 `RUNTIME-README.md`（无 PACK-MANIFEST）。运行时包压缩率 **62.6%**，打包耗时 **208.6 s**；**解压到 `C:\ModelSmith\runtime`**（ASCII 路径，不要放中文路径），状态目录用 `C:\ModelSmith\state`。

## 主包内容

`core/`（3 文件：服务 + README + selftest）｜`adapters/`（11：5 适配器 + 5 自测 + README）｜
`design/`（原型 `prototype/index.html`、`art/`、ART-NOTES、DESIGN-SPEC）｜
`docs/`（含 research/）｜`recipes/`（1：`qwen_image_21_lora.md`）｜
`dist/`（README、collect.py、**pack.py**、manifest.json、本文件、**RUNTIME-PACK.md**、**pack_runtime.py**、`runtime-licenses/`、`runtime-src/pylibs-5.zip`）｜`third_party/DiffSynth-Studio/`（1,695 文件，Apache-2.0，整棵）｜
`third_party/MANIFEST.md` + `third_party/LICENSES/`（4）｜根 `README.md`、**`start-core.cmd`**。

## 数据包内容

`resources/01-决策模型-业务语料/`（24，8.25 MB）｜`resources/02-LLM-语料/`（4，12.52 MB）｜
`resources/04-生图LoRA-图集/`（11，0.01 MB，含 `metadata.csv`）｜`resources/README.md`、`00-底座与权重-指针.md`、`06-清单与说明/`（5，0.03 MB）。

## 硬排除（命中即**报错退出**，不静默跳过）

`_tmp/`｜`_runs/`｜`.aitk-pylibs/`｜`.pylibs-lf/`｜`.venv-aitk/`｜
`resources/03-LLM-小模型/`｜`resources/07-底模/`｜
`third_party/laya/`｜`third_party/ai-toolkit/`｜`third_party/LlamaFactory/`｜`third_party/sd-scripts/`｜
`dist/runtime/`｜`dist/runtime-manifest.json`｜`*.safetensors`｜`*.gguf`。

## 剔除的生成物（打印清单，不算硬排除）

- `core/__pycache__/`、`adapters/__pycache__/`、`*.pyc`：自测生成的字节码（维护者 补充规则）；
- `adapters/_tmp/`：104 文件 / 212.96 MB 的测试脚本与 LoRA 产物；
- `third_party/DiffSynth-Studio/.git/`：29 文件 / 3.85 MB VCS 元数据（提交号见 `third_party/MANIFEST.md`）；
- `resources/04-生图LoRA-图集/smoke-synthetic/_latent_cache/`、`_t_e_cache/`：训练生成的 latent/TE 缓存目录，**按目录名排除**；`*.safetensors` 这条硬排除本身**没有例外**——缓存目录之外的任何 `.safetensors` 仍会让打包 **abort**；
- `dist/out/`：打包器自己的产物，防止自我打包。

## 许可口径

- 本包**只发训练方案/语料，不发任何模型权重**；
- `third_party/DiffSynth-Studio`：**Apache-2.0**，`LICENSE` 原样保留；
- **Qwen-Image 2.1 底模是 Research License，由用户自备**，不随包分发；
- 数据包逐条许可见各目录 `MANIFEST.md` 与 `PACK-MANIFEST.json` 的 `group`；
- 无许可数据（SMP2017/2018、CLINC150 等）只在本地研究，不进包。

## 与 `collect.py` 清单的关系

`collect.py` 是全机盘点：本次 **113 文件 / 3010.93 MB**（含 `resources\03` 953.29 MB、`resources\07` 2035.33 MB 等权重）。
**那些权重不在三个包内** —— 清单用于核对"本机有哪些件"，包只装可分发部分；manifest **没有 license 字段**，许可见 `third_party\LICENSES\` 与各 `resources\*\MANIFEST.md`。

## 分发脱敏（只在写 zip 时替换；磁盘源文件一个字节都不改）

`dist\pack.py` 对文本条目（`.md/.py/.js/.html/.json/.cmd/.sh/.txt/.yaml`）做内存替换：

| 原文 | 替换为 |
|---|---|
| `E:\视频\modelsmith` / `E:/视频/modelsmith` | `<包根>` |
| 其它 `E:\…` / `E:/…` | `<外部路径>` |
| `C:\Users\Administrator`（以及其它 `C:\Users\…`） | `<用户目录>` |
| `127.0.0.1:12334`（本机代理端口） | `<本机代理>` |

- `127.0.0.1:8765` 是 ModelSmith 自身服务端口，**保留不替换**。
- **出包闸门**：写完 zip 后遍历所有文本条目，命中 `E:\` / `E:/` / `C:\Users\` / `127.0.0.1:12334` 任一处即 **拒绝出包**（打印前 10 处并删除该 zip）。
- **负控**：`python dist\pack.py --inject-leak "E:\视频\x" --out-dir _tmp\pack-leak-test` 必须 exit≠0。
