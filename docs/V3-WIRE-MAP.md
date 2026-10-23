# v3 九页接线地图（R0 · 只读取证版）

> 对象：`E:\视频\modelsmith\design\prototype\v3\`（9 文件，2,550 行）。**只读**，未改任何 UI 文件、未跑 GPU。
> 证据：全部为**本机文件:行号**（A 级）。行号即当前文件行号，可直接 `Select-String` 复核。
> 给接线层（`core\assets\ms-wire.js`）的唯一锚点：**下面所有选择器都逐字来自源码，不要改写。**

## 0. 先说三条硬事实（决定了接线方式）

1. **v3 没有任何全局函数可调用。** index.html 的逻辑全部包在 IIFE 里（`index.html:937` 起，`})();` 收尾于 1266/1362/1413/1442/1465/1553/1586），独立页同样是单 IIFE（如 `image.html:90`、`llm-train.html:98`）。⇒ 接线**不能覆盖 `tick()`/`showPage()`**（闭包内不可达），只能：
   - 用**已暴露的 `window.__*` 钩子**（§3 表），或
   - 在**同一个事件上后注册监听**（后加载脚本 `addEventListener` 会排队执行，但**不能阻止**原处理器先跑——原演示会继续动 DOM），或
   - 直接**改 DOM 文本/宽度**（§4 每页清单），让演示继续空转也行。
2. **v3 完全没有后端调用**：全目录 grep `fetch(`/`XMLHttpRequest`/`EventSource`/`WebSocket`/`__MS_TOKEN__` = **0 命中**。所以「接真核心」= 从零加一层，不是改现有请求。
3. **`model-index.js` 只被两页引用**：`image.html:88`、`llm.html:97`；其余 7 页不依赖 `window.MS_MODELS`。

## 1. 九文件速查表

| 文件 | 角色 | 最终提交 kind | 全局入口 | 跳转 |
|---|---|---|---|---|
| `index.html`（1,516 行） | 决策线 deck：home + dec1–dec5 + llm1 + next 占位（8 个 `<section class="page">`） | **laya** | 9 个 `window.__*`（§3） | 同页 `showPage()`；llm/image 走 `location.href`（1473-1474） |
| `llm.html`（149） | LLM 第 1 步：选模型 | **lf** | 无（IIFE） | `location.href="llm-data.html"`（154） |
| `llm-data.html`（109） | LLM 第 2 步：选 JSONL | **lf** | 无 | `llm-train.html?model=&data=`（111） |
| `llm-train.html`（152） | LLM 第 3 步：训练运行（2 步演示） | **lf** | 无 | 完成 `#next` → `index.html`（94） |
| `image.html`（152） | 生图第 1 步：选底模 | **diffsynth** | 无（写 `window.__imageModel`：116） | `image-data.html?model=`（154） |
| `image-data.html`（113） | 生图第 2 步：图片文件夹 | **diffsynth** | 无（`window.__imageData`：93） | `image-params.html?model=&data=`（113） |
| `image-params.html`（95） | 生图第 3 步：提示词/采样计划 | **diffsynth** | 无 | `image-train.html?model=&data=&prompts=`（94） |
| `image-train.html`（237） | 生图第 4 步：训练运行（3000 步演示） | **diffsynth** | 无 | 完成 `#finish` → `index.html`（106） |
| `model-index.js`（27） | 模型清单 `window.MS_MODELS`（4 条，绝对路径） | 无 | `MS_MODELS`（2） | — |

## 2. 逐页 DOM 锚点与演示数据源

### 2.1 `index.html` — 决策线（kind: laya）

**页容器**（`#pages` 477）：`#pageHome` 478 / `#pageDec1` 552 / `#pageDec2` 620 / `#pageDec3` 678 / `#pageDec4` 728 / `#pageDec5` 803 / `#pageLlm1` 872 / `#pageNext` 912（占位）。
页面切换 = `translateX` + `aria-hidden/inert`（`showPage` 954-971，切到 dec3 触发 `window.__startRun` 969，切到 dec4 触发 `window.__dec4Sync` 970）。

| 页 | 关键 DOM（逐字） | 演示数据源 |
|---|---|---|
| dec1 底座 | `#dec1Opts .opt`（`aria-checked`、`data-state`、`data-srcs`、`data-size`、`data-bytes`、`data-id`）、`.dlpanel`、`.dlbar>i`、`.dlmsg`、`#dec1Next`、`#dec1Back` | **假下载**：`startDownload()` 1000，`timer=setInterval(...,500)` 1034，每 tick `pct += 1.4+Math.random()*1.2`，到 38% 假装换源（1044-1050）；`done()` 1021 改 `data-state="ready"` |
| dec2 数据 | `#dec2Dz`（拖放区）、`#dec2File`、`#dec2Pick`、`#dec2Sample`、`#dec2PickCard`、`#dec2Name`、`#dec2Meta`、`#dec2LangNote`、`#dec2Clear`、`#dec2Next`、`#dec2GoBase` | `show()` 1099；示例写死 `样例-闸门.train.jsonl` + `E:\视频\拉亚决策模型\数据\样例-闸门.train.jsonl` + `1258291` B（1121-1123）；`sizeHint()` 1095 用文件名正则编体积 |
| dec3 训练 | `#dec3Title #dec3Data #dec3Upd2 #dec3Micro #dec3Loss #dec3Ep #dec3Vram #dec3Bar #dec3Time #dec3Chart #dec3Log #dec3Tbl #dec3Cells #dec3Done #dec3Out #dec3Copy #dec3Finish #dec3Cancel #dec3Back` | 常量 `TOTAL=88, EPOCHS=4, MB_PER_UPD=8, VRAM_TOTAL=32, EST_SEC=96, CURVE_A=1.06, CURVE_B=0.44`（1156-1157），画布 `W=600,H=220,PL=46,PR=14,PT=14,PB=26,MIN=0.5,MAX=1.1`（1158），状态 `pts,u,timer,running,finished`（1159）。`tick()` 1184 用 `1.06-0.44*(1-exp(-u/26))+噪声` 造 loss；`draw()` 1163 输出 `<svg><polyline class="curve">`；`setInterval(tick,110)` **1250**；`finishRun()` 1205 硬写 5 行指标表 + 校准温度 1.00 / 拒答阈值 0.42 |
| dec4 测试 | `#dec4Run #dec4Ai #dec4Export #dec4Steps #dec4Result #dec4Tbl #dec4Report #dec4Banner #dec4More #dec4Scratch #dec4Again #dec4Done #dec4FinishCard #dec4Home #dec4NextActs #dec4CopyModel #dec4ModelPath #dec4CopyOut #dec4OutDir`；指标卡 **`#evAcc #evLoss #evEce #evAbs`** | `SAMPLES` 6 条硬写问答（1275-1282）；`metrics()` 1283 写死 `0.79/0.63/0.11/8.3%`；`aiBtn` 的 5 条假进度（1301）+ `setTimeout(nextStep,520)` 1304；报告 Markdown 拼在 1308-1330；续训对比追加 1333 |
| dec5 加训 | `#dec5Start .opt[data-id=last|scratch]`、`#dec5Ep #dec5File #dec5FileInfo #dec5Data #dec5Dz #dec5Pick #dec5Sample2 #dec5Run #dec5Back #dec5Prev` | `setFile()` 1371 默认「样例增量 120 条」1391；`pick()` 1392 只改 `aria-checked` 与 `#dec5Ep`（2/4） |
| llm1 | `#llm1Path #llm1Next #llm1Need #llm1Prev #llm1Back` | 无演示，仅 `window.__llmModelPath`（1452） |

### 2.2 `llm*.html` — LLM 微调（kind: lf）

- **llm.html**：`#modelSel`（70）、`#browseBtn`（73）、`#folderPick`（74）、`#modelPath`（78）、`#pasteBtn`（80）、`#next`（93）。`MODELS = MS_MODELS.filter(kind==="llm")`（103）；`sync()` 110 写 `window.__llmModelPath`（114）；下一步 154。
- **llm-data.html**：`#dataPath`（59）、`#openBtn`（60）、`#filePick`（61）、`#sampleBtn`（65）、`#next`（71）；`SAMPLE=…\dolly-15k\databricks-dolly-15k.jsonl`（79）；下一步 108-116（`window.__llmDataPath` 90）。
- **llm-train.html**：`#title #mName #dName #bar #step #loss #time #log #done #adapterPath #copyAdapter #outPath #copyOut #cancel #next`。常量 **`TOTAL=2`（110）**、`losses=[1.8127,1.6421]`（111）；`stepOnce()` 115（每次 +1 步、`setTimeout(stepOnce,1400)` 125）；`finish()` 127 生成 `E:\视频\modelsmith\_runs\ui-lf-<stamp>`（135-137）；首跑 `setTimeout(stepOnce,900)` **158**。
- 假产物路径：`llm-train.html:82,86` 硬写 `_runs\ui-lf-20261022-180000`。

### 2.3 `image*.html` — 生图 LoRA（kind: diffsynth）

- **image.html**：`#modelSel`（63）、`#browseBtn`（66）、`#folderPick`（67）、`#modelPath`（71）、`#pasteBtn`（72）、`#arch`（76）、`#size`（77）、`#form`（78）、`#next`（84）。`guessArch()` 97 靠**路径字符串**猜架构（`qwen-image-2.1`→"Qwen-Image 2.1"，99）；`sync()` 111 写 `window.__imageModel`（116）；`sizeEl` 只在路径**完全等于** `MS_MODELS[].path` 时才有值（118-119）⇒ 手打路径永远显示 "—"。
- **image-data.html**：`#dataPath`（61）、`#browseBtn`（62）、`#folderPick`（63）、`#sampleBtn`（66）、`#nImg`（69）、`#metaState`（70）、`#res`（71）、`#next`（77）。`SAMPLE=…\04-生图LoRA-图集\smoke-synthetic`（88）；只有路径含 `smoke-synthetic` 才显示「4 张/有」（95）。
- **image-params.html**：`#prompts`（67，两行英文默认）、`#plan`（71）、`#start`（76）。`render()` 85 把采样计划写死为 `steps=3000, every=200`（86-88）；`#start` 91 → 94 跳 image-train。
- **image-train.html**：`#title #mName #dName #bar #step #sp #shots #time #rounds #log #done #lorolist #cancel #finish`；轮次卡 `.round/.round__head/.round__t/.round__lora/.round__sp/.round__imgs/.shot/.shot__img/.shot__cap`，按钮 `.mini[data-act=star|pick|stop]`，产物行 `.lorow`。常量 **`TOTAL=3000, EVERY=200, KEEP=5, STEP_PER_TICK=25, TICK=110`（117）**；`var step=0,t0,timer=null,finished=false,groups=0,lastSampled=0`（124）；`addRound()` 132、`choose()` 172、`tick()` 178、`finishRun()` 193、`stopTraining()` 201、`showDone()` 210；首跑 `setTimeout(tick,600)` **248**；每 tick `setTimeout(tick,TICK)`（190）。假输出 **`OUT="E:\视频\modelsmith\_runs\ui-diffsynth-20261022-180000"`（126）**，`loraPath()` 129 拼 `step-xxxx.safetensors`。

## 3. 全部 `window.__*` 一览（唯一可调用接口）

| 名字 | 定义位置 | 类型 | 语义 | 后加载脚本能否覆盖 |
|---|---|---|---|---|
| `window.__startRun(name, mode)` | `index.html:1265` | function | 进 dec3 时由 showPage 调用；`mode` 只认 `"retrain"`（1228） | ✅ 可（最后赋值者生效） |
| `window.__openEval(mode)` | `index.html:1357` | function | 直接跳 dec4 并点 `ai`/`run` | ✅ |
| `window.__dec4Sync()` | `index.html:1358` | function | 按 `__lastMode` 显隐 `#dec4Banner` | ✅ |
| `window.__dec5Pick(id)` | `index.html:1396` | function | `"last"`/`"scratch"` 选起点 | ✅ |
| `window.__runMode` | `index.html:1409` 写 | string | dec5 点击后写 | ✅（数据） |
| `window.__lastMode` | `index.html:1236` 写 | string | `"train"`/`"retrain"` | ✅（数据） |
| `window.__decDataName` | `index.html:1103` 写 | string | 数据名（dec3 日志用） | ✅（数据） |
| `window.__dec5File` | `index.html:1374` 写 | string | 增量文件名 | ✅（数据） |
| `window.__llmModelPath` | `index.html:1452` / `llm.html:114` | string | LLM 模型路径（两页各自写，同名） | ✅（数据） |
| `window.__llmDataPath` | `llm-data.html:90` | string | LLM 数据路径 | ✅（数据） |
| `window.__imageModel` | `image.html:116` | string | 底模路径 | ✅（数据） |
| `window.__imageData` | `image-data.html:93` | string | 图片目录 | ✅（数据） |
| `window.MS_MODELS` | `model-index.js:2` | array | 4 条模型（lldm/sd15/laya/Qwen-Image-2.1） | ✅（但只在两页被读） |

**接线的两个可行姿势**：① `window.__openEval("run")` 这类钩子做「跳到某页并触发」；② 捕获阶段监听（`addEventListener(..., true)`）也不能拦住原处理器，**要停演示只能让 core 在页面加载后 `clearInterval` 不到（闭包内 timer 不可见）** ⇒ 实际做法：**不阻止演示，改为用真实数据覆盖它的输出**（§4），或整页替换时自己接管 DOM。

## 4. 每页 wire 最小动作清单（不改 DOM 结构）

| 页 | 必须被覆盖/调用的东西（按顺序） | 可安全更新的节点 |
|---|---|---|
| `index` dec3 | ① 换掉 `start()` 里 `setInterval(tick,110)`（1250） ② 用真实事件驱动同一批节点 ③ 完成时替换 `finishRun()`（1205）的硬写表格 | `#dec3Upd2 #dec3Micro #dec3Loss #dec3Ep #dec3Vram #dec3Bar(style.width) #dec3Time #dec3Log(append) #dec3Chart(innerHTML 重画 svg) #dec3Tbl #dec3Cells #dec3Done #dec3Out` |
| `index` dec4 | ① `SAMPLES`（1275）换真实逐条 ② `metrics()`（1283）换真值 ③ `#dec4Report` 文本换真报告 ④ `#dec4Export` 的 Blob（1339-1345）保留即可 | `#evAcc #evLoss #evEce #evAbs #dec4Tbl #dec4Report #dec4Steps` |
| `index` dec2 | ① `show()`（1099）之后追加真实体检（行数/字段/跳过统计） ② `sizeHint()`（1095）的假体积要换 | `#dec2Name #dec2Meta #dec2LangNote #dec2Next(aria-disabled)` |
| `index` dec1 | 下载面板（`startDownload` 1000）要换成真下载状态机 | `.dlbar>i`（width）、`.dlmsg`、`.opt[data-state]`、`.opt__end` |
| `llm-train` | ① 接管 `stepOnce()`（115） ② 完成时改 `#adapterPath/#outPath`（`finish()` 127） | `#step #loss #bar #time #log #done #adapterPath #outPath #title` |
| `image-train` | ① 接管 `tick()`（178）/`addRound()`（132） ② 接管 `stopTraining()`（201）为真中断 ③ `#lorolist` 换真实档 | `#step #sp #shots #time #bar #rounds #log #done #lorolist #title` |
| `image-params` | `render()`（85）的 3000/200 必须改成真实 `steps/sample_every` | `#plan` |
| `llm.html`/`llm-data`/`image.html`/`image-data` | 仅需在 `sync()` 后把真实「本机已装模型/数据」灌进清单 | `#modelSel`（重建 option）、`#arch #size #form`、`#nImg #metaState` |

## 5. 托管到 `/` 与 `/v3/xxx.html` 会不会断

- 全部跳转都是**同目录相对路径**（`href="index.html"`、`location.href="llm.html"`、`"image-data.html?model=…"`）⇒ **目录整体搬到哪里都不改写**（`/`、`/v3/` 均可）。
- **唯一的断法**：core 只挂了一个入口路由（如只把 `/` 映射到 `index.html`）而没有把 9 个文件作为静态资源暴露 ⇒ `/llm.html`、`/image-params.html` 会 404。**要求 core 静态目录 = 整个 v3/**。
- query 传参（`?model=&data=&prompts=`）在静态托管与 `file://` 下都正常；`#` hash 全站 0 使用。
- `model-index.js` 内含**绝对 Windows 路径**（如 `E:\临时文件夹\Qwen-Image-2.1`）⇒ 浏览器里只是字符串比较，能跑；**换机/换盘即失效**（需要重跑 `tools\scan_models.py`，见 `model-index.js:1` 注释）。

## 6. Laya 评测入口：**有**，现成

| 入口 | 位置 | 形态 |
|---|---|---|
| 控制台脚本 | `laya/pyproject.toml:101` → `laya-evals = "laya.evals_cli:main"` | `laya-evals run <jsonl> --model <out目录>` |
| CLI 主函数 | `laya/evals_cli.py:606` `def main(argv=None)` | 子命令 `validate`(191) / `run`(196) / `agreement`(252) / `compare`(263) / `evidence`(272) |
| `run` 参数 | `evals_cli.py:200-250` | `--model --device --batch-size --min-confidence --baseline --tolerance --min-accuracy --max-ece --slice --gate-policy --json --markdown` |
| 库函数 | `laya/evals.py:1084` `def evaluate(runner, dataset, evaluators=None, batch_size=None, on_error="fail", config=None, sort_by_length=False, min_confidence=None) -> EvalReport` | runner 需 `predict(state, questions, model=...)`；`batch_size>1` 时用 `predict_batch`（evals.py:1090-1099） |
| 数据集类 | `laya/evals.py:200` `class Dataset`；指纹 `file_fingerprint` 341 | — |

**最小调用形态（两种，都是「已训 out 目录 + heldout jsonl → 逐条/指标」）**

```powershell
# A. CLI（推荐，最省接线）
E:\ComfyUI\python_embeded\python.exe -m laya.evals_cli run `
  "E:\视频\_kb_research\htlb\train\data\human_test.jsonl" `
  --model "E:\视频\_kb_research\htlb\train\ckpt\sel-v1" `
  --device cuda --json "E:\...\report.json" --markdown "E:\...\report.md"
#   逐条预测：--json 的报告带 per-case；若只要预测行，用 --markdown 或自己遍历 Dataset
```

```python
# B. 库内（需要逐条答案时）
from laya.evals import Dataset, evaluate
from laya import load
agent = load(r"E:\...\out")                 # 已训目录
report = evaluate(agent, Dataset(...), batch_size=16)   # Dataset 构造签名见 evals.py:200（未取 __init__ 细节）
```

> 注意：`laya.load()` 得到的 `Agent.predict` **不接受 `model=`**，`evaluate` 会自动丢这个 kwarg（evals.py:1095-1099）——接 sel-v1 这种单模型目录正好。

## 7. 未验 / 边界

- `Dataset` 的构造签名与 `from_jsonl` 是否存在：**未验**（只取到 `class Dataset` 在 evals.py:200，未读 `__init__`）。检索式：`evals.py "class Dataset"`。
- dec3/dec4/dec5 演示与真实 Laya 产物字段的对齐（如 `#dec3Tbl` 五指标 vs `train_report.json` 的 before/after）**未验**（需要真实 out 目录跑一次评测）。
- core 托管后的实际路由与 404 行为：**未验**（本次只读文件，未起服务）。
- `image-train.html` 的「选这份/这轮达标停训」是**纯 UI 状态**（`choose()` 172 / `stopTraining()` 201），真实 diffsynth 中断信号如何映射**未验**。
- v3 无 `__MS_TOKEN__`、无任何网络请求（全目录 0 命中）——所以「鉴权/进度流」在 v3 里**没有现成挂点**，需 core 自建。
