# smoke_e2e.py — 交付前硬闸门

一条命令（无 GPU、stdlib、可重复）：

```powershell
cd E:\视频\modelsmith
python tools\smoke_e2e.py
```

期望：`RESULT: PASS 63 / FAIL 0`，exit 0（54 + 字幕编辑 6 项 + 训练页统计/显存条 3 项；实测 2026-10-23 为 63/0）。失败时逐条 `FAIL nn - ...`，非 0 退出。

覆盖：
1. 核心 `/health`、`GET /api/models`；
2. 完整性矩阵：`z-image-turbo`/`moss-image-sft` complete=true；`z-image` complete=false 且 missing 含 scheduler/text_encoder/tokenizer/vae；
3. 负控（自动、会恢复）：临时把真实 `z-image-turbo\text_encoder` 改名 → complete=false 且 missing 点名，再改回；
4. 残缺模型 `kind=diffsynth` 提交 → 400 点名缺失；
5. 完整模型 + `train_command` 桩 → 202（随后取消，不占 GPU）；
6. `/api/pick` dir 三态：正常路径 / `__cancel__` / `__no_desktop__`；
7. fake job → succeeded + `metrics_history` 行 + logs 非空；
8. 无 metadata 的数据集（`_tmp\ds-nocsv`）：`prepare_dataset_metadata` → generated=true、n=4、txt=2、default=2；CSV 首 3 字节 `696d61`（无 BOM）；用户目录前后 6 文件不变；空目录 → `no images under`；
9. `GET /` 200 + `__MS_TOKEN__` + `/ms-wire.js`；wire 字节与磁盘一致；v3 九文件 SHA 基线（8 个 v3 页 + `v3/model-index.js`，2026-10-23 随 ui-frame-2 三件落盘更新，值以本机 Get-FileHash 复算为准）；
10. 必失败 job 终态后 2 s 状态不变；
11. 停核心后端口 0 listener；
12. **`/api/vram`**：200 且 `ok:true` 含 `used_mib/total_mib/pct`；无 token → 401；负控：`--vram-cmd` 指不存在命令 → `ok:false` 但**仍 HTTP 200**；
13. **artifacts 真实性**：out 目录只有 `training_args.json` + 预置 30 天前 `epoch-9.safetensors` → `metrics.artifacts=[]`，且响应（除 `metrics.argv` 外）不含 `.safetensors` 字面量；
14. **无字幕警告**：数据目录 0 个 `.txt` → `metrics.no_captions=true`、`no_captions_images>=1`、嵌套 `dataset_metadata.no_captions=true`、`warnings` 仍是 `list[str]` 且含 `no_captions:`；负控：放同名 `.txt` → `no_captions=false`。
   注：`metrics.argv` 会合法携带模型分片路径（`*.safetensors`），第 13 条的“无字面量”断言因此显式排除 argv。
15. **`/api/caption/backend`**：200 + `ok:true` + `backends[]`（每项 `id/available/reason` 自洽）；无 token → 401；负控（无 CLI 注入，直接调 core 函数）`caption_backend_status(engine=坏路径)` → `engine_available=false` 且每个 backend `available=false` + `reason` 非空；
16. **`kind=caption` 校验**：`dir` 不存在 → 400 点名 dir；`backend` 非法 → 400；`batch_size` 0/9 → 400（范围 1–8）；合法参数 + `server_cmd` 桩 → 202 且 job 可见；
17. **caption 终态无假路径**：caption job 终态响应里 `.safetensors` 匹配数 = 0（打标只写 `.txt`）；
18. **前端无硬编码**：`core\assets\ms-wire.js` 里 `llama-cpp` + `ComfyUI` 出现次数 = 0；
19. **首页布局回归指纹**：`design\prototype\v3\index.html` 里 `1.42fr` 命中 = 0，且 `:has(...){...grid-template}` 规则命中 = 0（hover 放大挤占的旧实现一旦回来就变红）；
20. **训练页缩略图/灯箱**：`v3\image-train.html` 的 `.shot__img` 规则必须含 `background-size:cover`（命中 0 = 又裁切），且必须有 `id="msLightbox"`（命中 0 = 点开展开被删）；
21. **字幕编辑面板**：`v3\image-data.html` 必须各含 1 次 `id="captionEditPanel"` / `id="captionEditList"` / `id="captionEditSaveAll"`（3 条独立断言）；`ms-wire.js` 必须含 `initCaptionEdit` 且存在 `api("/dataset/captions", { … method: "POST" … })`；
22. **字幕读取接口**：`GET /api/dataset/captions?dir=…` → 200 且 `items` 是数组（字幕编辑面板的数据源）；
23. **训练页统计卡/显存条**：`v3\image-train.html` 的 `.stats` 段内 `<div class="cell"` 必须恰好 4 个（第 5 张显存卡已删），且 `id="vramBar"`、`id="ramBar"` 各 1 次。

## 三个“能变红”的负控（手工验证用）

- **完整性判据**：把 `z-image-turbo\text_encoder` 改名为 `text_encoder_off` → `/api/models?refresh=1` 必须 `complete=false, missing=["text_encoder"]`；smoke 自动做并恢复。若仍绿，说明把 `model_index.json` 的值当路径了。
- **残缺提交**：用 `z-image`（缺组件）提交 `kind=diffsynth` → 必须 **400** 且 error 点名 scheduler/text_encoder/tokenizer/vae；若 202/假成功即红。
- **UI/静态**：改 `core\assets\ms-wire.js` 一个字节或某个 v3 文件 → smoke 必须在 `/ms-wire.js` 字节或 SHA 项变红；UI 侧用 `_tmp\audit\g01-g03.mjs` 验 image-data 目录回填、z-image 置灰、失败态文本 3×500ms 不变。

## 本次 G01-G03 headless 读数

- G01：image-data 点按钮 → `#dataPath=E:\视频\modelsmith\resources\04-生图LoRA-图集\smoke-synthetic`
- G02：下拉 `z-image` disabled=true、`z-image-turbo` disabled=false
- G03：失败态标题 3 次采样（间隔 500 ms）均为「核心未连接：缺少底模或数据路径」，文本不变。
