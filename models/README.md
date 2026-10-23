# models\ —— 唯一模型仓（把东西放这里，程序自动扫描）

> 程序只扫这一个目录（`E:\视频\modelsmith\models\`），**不扫** ComfyUI / 系统其它目录。
> 扫描结果出现在「选一个底模」的下拉与 `/api/models` 里；扫到但认不出的，会标 `unknown` 并提示你看本文件的"目录契约"。

## 1 目录契约（按 arch 放，层级固定）

| arch | 目录 / 文件 | 必须有的组件 |
|---|---|---|
| `z_image` | `models\z-image\` | `transformer\*.safetensors`（+ `scheduler\`、`text_encoder\`、`tokenizer\`、`vae\`；后四项可 junction 到 turbo 目录） |
| `z_image`（Turbo 版同样按 z_image 训） | `models\z-image-turbo\` | `transformer\` + `text_encoder\` + `vae\` + `tokenizer\` + `scheduler\`（本机现存的是 **F32** transformer，22.93 GiB） |
| `flux2_klein_9b` | `models\flux2-klein-9b\` | `transformer\` + `text_encoder\` + `vae\` + `tokenizer\` + `model_index.json` |
| `qwen_image_21` | `models\qwen-image-2.1\` | `transformer\` + `text_encoder\` + `vae\` + `processor\` + `scheduler\` + `model_index.json` |
| `moss_image` | `models\moss-image-sft\` | `transformer\` + `text_encoder\` + `vae\` + `tokenizer\` + `scheduler\` |
| `krea2_raw`（三路径） | `models\krea-2-raw\raw.safetensors` + `models\qwen3-vl-4b-instruct\` + `models\qwen-image\vae\diffusion_pytorch_model.safetensors` | 单文件 transformer + TE 目录 + VAE 文件 |

- **单文件模型**（如 krea2 的 `raw.safetensors`）允许直接平铺在 `models\<名字>\` 下，扫描器按 `kind=diffusion_single` 归类。
- **允许 junction / 软链接**：为了省盘，可以把组件目录链到别处（本机已有两例：`z-image` 的 4 个组件 → `z-image-turbo`；`qwen-image-2.1` 的 5 个组件 → `E:\临时文件夹\Qwen-Image-2.1`）。扫描器会跟进统计大小并给 `linked=true`、`link_targets`。
- **不要**把模型放进 `models\` 的子子目录（深度上限 3），也不要放 `token`、`.env`、日志。

## 2 自动扫描的时机

- 每次打开页面 / 调 `GET /api/models` 时扫描（mtime 变化才重扫，60 s TTL 内走缓存）
- 下拉里 **双击** 可强制刷新
- 新增/移动文件后，最迟 60 s 或刷新一次就能看到

## 3 判"放对了没有"

- `GET /api/models` 里该模型 `complete=true`、`missing=[]` ⇒ 放对了
- `complete=false` 且 `missing` 非空 ⇒ 缺的就是列出来的组件目录（照着上面表补）
- `kind=unknown` ⇒ 目录里既没有 `model_index.json`，也没有可识别的单文件 → 多半是层级或命名不对

## 4 已有模型（会随扫描更新，此处只举例）

`flux2-klein-9b` 32.34 GB · `moss-image-sft` 25.36 GB · `qwen-image-2.1` 30.86 GB(linked) · `z-image` 19.13 GB(linked) · `z-image-turbo` 30.64 GB · `krea-2-raw` 24.48 GB · `qwen3-vl-4b-instruct` 8.28 GB
