# 客服决策展示台（cs-demo）

独立单页演示：左边发一句客户原话，右边显示客服决策面板。纯静态页面 + stdlib-only HTTP 服务，不依赖构建工具或第三方包。

## 快速启动

```powershell
python E:\视频\modelsmith\apps\cs-demo\server.py
```

或使用本机嵌入式 Python：

```powershell
E:\ComfyUI\python_embeded\python.exe E:\视频\modelsmith\apps\cs-demo\server.py
```

浏览器打开：<http://127.0.0.1:8766/>

服务只监听 `127.0.0.1:8766`，明确拒绝 8765（ModelSmith 核心端口）。

### 独立运行

展示台（`server.py` + `index.html`）与本地模型服务（`model_server.py`）都是独立程序：不需要启动 ModelSmith core、训练调度器或 8765 服务；规则后端可直接独立演示，`model_server.py` 只依赖本目录、模型权重和 embedded Python。目录迁移/打包等用户点头后再做。

## 当前 UI 布局（已定稿）

右侧固定六项：**情绪 / 意图 / 动作 / 置信度 / 命中规则 / 回复草稿**。不再做三行卡改造。

- 前端面板标签写「**意图**」；Laya 后端对应字段是 `purpose`，`server.py` 会把 `purpose` 映射成 UI 的 `intent` 并补上动作置信度。
- 这是既成约定，**不要为了字段名对齐去改 UI**。

## 接口

### `POST /decide`

请求：

```json
{"input": "你们这破东西，我要退款！"}
```

Laya 后端的响应（默认，统一结构）：

```json
{
  "emotion": "愤怒",
  "emotion_level": 5,
  "emotion_confidence": 0.24,
  "emotion_probabilities": {"平静": 0.12, "失望": 0.08, "焦虑": 0.10, "不满": 0.21, "愤怒": 0.24, "威胁投诉": 0.25},
  "purpose": "退款",
  "purpose_confidence": 0.31,
  "purpose_probabilities": {"退款": 0.31, "物流": 0.12, "其他": 0.57},
  "action": "退款",
  "action_confidence": 0.22,
  "action_probabilities": {"退款": 0.22, "转人工": 0.18, "安抚话术": 0.15, "其他": 0.45},
  "confidence_source": "raw",
  "thresholds": {"emotion": null, "purpose": null, "action": null},
  "low_confidence": {"emotion": false, "purpose": false, "action": false},
  "rule": "template:退款",
  "reply": "理解您想退款。请提供订单号……",
  "reply_source": "template",
  "backend": "laya"
}
```

- `emotion_confidence` / `purpose_confidence` / `action_confidence` 是三个 choice 的 top-1 原始概率；三个 `*_probabilities` 是完整原始概率分布，**不做 LLM 式 0.6 阈值改写**。
- `thresholds` 每题可配，默认 `null` = 不启用；设了 action 阈值且低于阈值时才 `action:"转人工"`、`rule:"low-confidence"`。emotion/purpose 阈值只置 `low_confidence` 标记。
- `confidence_source:"raw"` 表示未做温度/校准改写。
- `reply_source:"template"` 表示话术来自 `reply_templates.json`；模板缺失时用兜底模板并带 `template_fallback:true`。
- 解析/白名单失败时 `rule:"model-fallback"`、`fallback:true`，仍走模板回复。
- 展示台兼容规则后端的旧字段；演示服务会额外返回 `backend`（`rule`/`http`/`fallback`）和 `fallback`。

动作白名单固定为：

`安抚话术 / 补发 / 退款 / 换货 / 运费补偿 / 发红包 / 转人工 / 拒绝并解释`

情绪档位：`平静 / 失望 / 不满 / 愤怒 / 焦虑 / 威胁投诉`；按 `datasets\cs-decision\ACTIONS.md` 对齐为：

| emotion | emotion_level |
|---|---:|
| 平静 | 1 |
| 失望 | 2 |
| 焦虑 | 3 |
| 不满 | 4 |
| 愤怒 | 5 |
| 威胁投诉 | 5 |

策略表优先级（与 `ACTIONS.md` 对齐）：威胁/曝光/监管/上级/二次投诉/辱骂失控/要求人工 → `转人工`；越界索赔/超出售后 → `拒绝并解释`；物流未达/丢件 → `补发`；错发/破损/不符 → `换货`；运费争议 → `运费补偿`；明确退款 → `退款`；体验差但无越界 → `发红包`；一般咨询/轻微不满 → `安抚话术`；极模糊/与客服无关 → `转人工`。

## 后端切换

### 1. 规则引擎（默认）

```powershell
$env:CS_BACKEND = "rule"
python E:\视频\modelsmith\apps\cs-demo\server.py
```

规则引擎按情绪关键词 + 意图关键词 + 策略表即时产出，保证无模型也能演示。

### 2. HTTP 模型服务

```powershell
$env:CS_BACKEND = "http://127.0.0.1:9000"
python E:\视频\modelsmith\apps\cs-demo\server.py
```

服务会向 `<CS_BACKEND>/decide` 发送：

```json
{"input": "在吗"}
```

模型服务必须返回上面同结构的 JSON。若连接失败、返回无法解析的 JSON、缺少字段或 `action` 不在白名单，服务自动回落：

```json
{"action": "转人工", "rule": "fallback"}
```

并带上完整字段与 `fallback: true`；页面显示「已回落」。

如果模型服务的接口不是 `/decide`，把 `CS_BACKEND` 设为完整 URL（例如 `http://127.0.0.1:9000/v1/decide`）。

### 3. 本地 Laya 决策后端（默认）

`model_server.py` 默认走 **Laya**，监听 `127.0.0.1:8767`，不依赖 ModelSmith core/8765 或训练调度器。

- 离线包：`E:\视频\拉亚决策模型\vendor\拉亚本地包.py`
- 权重：默认 `E:\视频\modelsmith\_runs\laya-ft-20261023-1555`（0.4.13 全参，**旧基线，不是最终模型**）
- 待切换：`E:\视频\modelsmith\_runs\laya-cs-20261023-1605`（已出现，但目的标签集还要 10 类重训，**未经 维护者 通知不切**）
- 覆盖权重：`$env:CS_LAYA_MODEL = "E:\...\新checkpoint"`
- Python：`E:\ComfyUI\python_embeded\python.exe`
- 依赖路径：脚本会把 `E:\视频\modelsmith\.pylibs-lf` 和 Laya vendor 插到 `sys.path`
- GPU：Laya 推理只占几百 MB 到数 GB；仍然同一时刻只跑一个 GPU 作业

Laya 做三项判定：**情绪（6 选 1）**、**目的（purpose）**、**动作（8 选 1）**，各自返回完整 choice 概率。目的标签集以 checkpoint 的 `questions.json` 为准（新 checkpoint 当前 11 类，最终会重训为 10 类）；服务端优先读取该文件并归一成 `emotion`/`purpose`/`action`。**Laya 不生成话术**；回复一律从 `reply_templates.json` 按动作（可选目的）选取，返回 `reply_source:"template"`。Laya 原始输出不会透传给前端。

启动：

```powershell
$env:CS_MODEL_BACKEND = "laya"
E:\ComfyUI\python_embeded\python.exe E:\视频\modelsmith\apps\cs-demo\model_server.py
```

健康检查：

```powershell
curl.exe -sS http://127.0.0.1:8767/health
```

展示台切过去：

```powershell
$env:CS_BACKEND = "http://127.0.0.1:8767"
python E:\视频\modelsmith\apps\cs-demo\server.py
```

阈值**每题可配**，默认 `null` = 不启用（不要再用 0.6 这种 LLM 式阈值）：

```json
{"emotion": null, "purpose": null, "action": null}
```

配置文件：`thresholds.json`；也可用环境变量覆盖：

```powershell
$env:CS_LAYA_THRESHOLDS = '{"emotion":null,"purpose":null,"action":null}'
```

- 只有 action 阈值设为数值且实际低于它时，才 `action:"转人工"`、`rule:"low-confidence"`；回复仍从模板取。
- emotion/purpose 阈值只置对应 `low_confidence` 标记，不改动作。
- 三个概率原样返回（`emotion_probabilities`/`purpose_probabilities`/`action_probabilities`），并标 `confidence_source:"raw"`。
- 是否启用阈值等评测席的分题概率分布，当前默认全关。

### 3.1 回复模板

`reply_templates.json` 按动作组织；`default` 是该动作的默认话术，键名可写可选目的/意图。缺模板时用 `_fallback`，并返回 `template_fallback:true` 供 UI 标注。改文案不需要重启模型，重启展示台 `server.py` 即可生效。

### 3.2 4B 后端（封存备选）

`CS_MODEL_BACKEND=hf4b` 仍保留 4B 路径（merged 或 base+LoRA），但**已不是默认，也不再作为交付后端**；实测定为 Laya。4B 的原始 `<think>` 会剥离，回复字段不会被直接透传。除非做对照，不建议启动。

### 3.3 不占 GPU 的桩

```powershell
$env:CS_MODEL_FAKE = "valid"
python E:\视频\modelsmith\apps\cs-demo\model_server.py --port 8767
```

`CS_MODEL_FAKE` 还支持 `bad-json` / `bad-action`，验证回落逻辑；不 import torch、不占显存。

## curl 自测

```powershell
curl.exe -sS -X POST http://127.0.0.1:8766/decide -H "Content-Type: application/json" -d "{\"input\":\"你们这破东西，我要退款！\"}"
curl.exe -sS -X POST http://127.0.0.1:8766/decide -H "Content-Type: application/json" -d "{\"input\":\"在吗\"}"
```

## 文件

- `index.html` — 单页 UI（内联 CSS/JS，无外链）
- `server.py` — `POST /decide` + 静态文件，stdlib-only
- `model_server.py` — Laya 判定服务（默认，8767；`hf4b` 封存备选），另含不占 GPU 的 fake 模式
- `reply_templates.json` — 按动作（+ 可选目的）的话术模板
- `thresholds.json` — 分题阈值，默认三项 `null`（关闭）
- `README.md` — 本文件

## 约束

- 演示模式：不接支付、不真发红包、不连真实客服系统。
- 不读写 ModelSmith `core\`、`adapters\`、`design\`，不使用 8765 端口。
- 规则引擎已按 `E:\视频\modelsmith\datasets\cs-decision\ACTIONS.md` 的动作白名单、情绪档位与策略优先级对齐（该文件在开发期间出现，已重新核对）。
