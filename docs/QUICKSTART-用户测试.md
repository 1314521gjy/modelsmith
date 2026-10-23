# ModelSmith 用户测试 Quickstart

适用：Windows 10/11 + NVIDIA 显卡。整个测试**不需要联网**（依赖与运行时已随包）；第一次启动约 10–30 秒。
仓库根：`E:\视频\modelsmith`（以下相对路径都相对它）。

## 三步开始

### 1. 解压运行时包到 ASCII 路径

把 `ModelSmith-运行时-20261022.zip` 解压到 **`C:\ModelSmith\runtime`**，确认存在：

```
C:\ModelSmith\runtime\python\python.exe
C:\ModelSmith\runtime\site-packages\
C:\ModelSmith\runtime\pylibs-5\
```

**不要放在中文路径下**（如 `E:\视频\...`）：embed Python 在中文路径下创建 sqlite 状态库时会报
`unable to open database file`。状态目录固定用 ASCII 路径 **`C:\ModelSmith\state`**。

### 2. 双击启动

双击仓库根的 `start-modelsmith.cmd`。脚本会：

- 按 `MODELSMITH_RUNTIME` → `C:\ModelSmith\runtime` → `dist\runtime` → `runtime` 顺序找运行时；
- 用 `--data-dir C:\ModelSmith\state` 启动 `core\modelsmith_core.py --port 8765`（固定端口）；
- 已在运行：再次双击会直接打开浏览器，不再报端口占用；若 `core\modelsmith_core.py`、`core\assets\ms-wire.js` 或 `adapters\diffsynth_adapter.py` 有更新，会自动重启核心（调试可设 `START_CORE_NO_RESTART=1` 跳过）；
- 端口被其他程序占用：打印 `netstat` 行并提示，不会误杀；
- 等 `/health` 最多 5 秒，然后打开默认浏览器 `http://127.0.0.1:8765/`。

浏览器打不开时手动访问 `http://127.0.0.1:8765/`。核心日志在 `C:\ModelSmith\state\logs\launcher.log`
（启动失败会自动显示最后 20 行并暂停）。

### 3. 在浏览器里选模型 / 数据 / 参数

- **模型**：放到 `models\<名字>\`。核心 `GET /api/models` 会自动扫描 `models\` 并出现在基座下拉里；
  - 目录含 `model_index.json` → 识别为 diffusion；
  - 例：`models\z-image-turbo\`、`models\moss-image-sft\`、`models\qwen-image-2.1\`，或任意 `models\<名字>\`。
- **数据**：用一键脚本生成 DiffSynth 数据集：
  ```powershell
  C:\ModelSmith\runtime\python\python.exe .\tools\make_image_dataset.py --images .\我的图 ^
    --trigger "my style" --out .\datasets\my-style
  ```
  手工准备也可以：图片文件 + **同名 `.txt`**（每张一个 caption）+ `metadata.csv`（表头 `image,prompt`）。
- **参数**：第一次建议 **Z-Image-Turbo、512 分辨率、rank 32、300–600 步、`sample_every=50`**，约 **7–15 分钟**。
  **不要一上来跑 3000 步**；先用小步数确认数据、显存、采样都正常。

## 三条关键命令

```powershell
# 1) 运行时就位检查（必须 True）
Test-Path C:\ModelSmith\runtime\python\python.exe
# 2) 一键启动（浏览器自动打开）
.\start-modelsmith.cmd
# 3) 生成数据集
C:\ModelSmith\runtime\python\python.exe .\tools\make_image_dataset.py --images .\我的图 --trigger "my style" --out .\datasets\my-style
```

## 训练完成后看哪里

- 采样图：`_runs\ui-diffsynth-*\samples\step-N\*.png`
- LoRA 权重：`_runs\ui-diffsynth-*\epoch-0.safetensors`
- 出图预览：用 `C:\ModelSmith\runtime\python\python.exe tools\preview_lora.py` 加载上面的 LoRA 出图（参数见脚本 `--help`）。
  若本版没有该脚本，用 `recipes\qwen_image_21_lora.md` 里的 `validate_lora\Qwen-Image-2.1.py` 代替，把 `load_lora(...)` 换成实际 `epoch-0.safetensors` 路径。

## 常见问题

**OOM（显存不足）**：先把分辨率/`max_pixels` 降到 512；rank 从 32 降到 16/8；若界面有 offload/梯度检查点开关，保持开启；再关掉其它占卡进程。

**核心起不来 / 浏览器一直转圈**：端口 8765 被占用时先 `netstat -ano | findstr :8765` 找 PID，再 `taskkill /PID <pid> /T /F`；然后看 `C:\ModelSmith\state\logs\launcher.log`。不要同时开两个核心。

**浏览器没有自动打开**：手动访问 `http://127.0.0.1:8765/`；若仍失败，检查 Windows 防火墙是否拦了本机回环（通常不会）。

## 停止

在任务管理器结束 `python.exe`（命令行含 `modelsmith_core.py`），或关掉对应的隐藏核心进程；测试数据在 `C:\ModelSmith\state` 与 `_runs\`。
