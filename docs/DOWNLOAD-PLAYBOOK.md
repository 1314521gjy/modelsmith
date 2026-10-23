# ModelSmith 模型下载 playbook（唯一出处，别再每次现摸）

> 为什么有这份文件：下载路线长期没有沉淀，换个模型就要重新试错。本文件把已经走通的下载方式固定下来。
> 本文只钉**判定顺序 + 可复制的命令 + 判据**；具体某个 repo 下过什么记在 `docs\MODELS-DOWNLOADED-20261023.md` 与各模型目录的 `manifest.json`。
> 证据等级：A = 本机实测；C = 外部文档；D = 推断。

## 0 一句话判定树（照这个顺序走，别跳）

```
要下 HF 上的东西
 ├─ 1. 先查元数据：GET https://huggingface.co/api/models/<repo>
 │     ├─ gated 字段 = "auto"/"manual"  ⇒ 匿名拿不到（resolve 返回 401）
 │     │     └─ 走 ModelScope 公开镜像（§2）——这才是"被重置"的真身
 │     └─ gated 缺失/False ⇒ 走 §1 直连
 └─ 2. 直连开始下之后，若 aria2 报 0 B/s 停滞超过 ~60 s
       ⇒ HF CDN 对长连接不友好，切 ModelScope 同一批文件（§2）
```

## 1 HF 直连（非 gated 仓库）

```powershell
# 单文件（>1 GiB 一律走 aria2c 16 路）
aria2c -x16 -s16 -k1M --file-allocation=none --continue=true --max-tries=0 --retry-wait=5 `
  "https://huggingface.co/<repo>/resolve/main/<path>"

# 整目录（多文件）：先用 API 拿 siblings 列表，拼成 aria2 输入文件
#   GET https://huggingface.co/api/models/<repo>  → .siblings[].rfilename
#   aria2c --input-file=<列表> --continue=true -x16 -s16 -k1M
```

- 镜像兜底：`HF_ENDPOINT=https://hf-mirror.com`（在 hf CLI 场景）；**注意** hf-mirror 对 `krea/*`、`black-forest-labs/*` 这批 repo 实测也被 reset，别在它上面死磕。
- **本机代理**：`127.0.0.1:12334`（代理开着时 python/aria2 默认**不读**系统代理，要显式给）。需要时：
  `aria2c --all-proxy=http://127.0.0.1:12334 ...`；反过来若代理反而拖慢/阻断，就**绕过**：`--all-proxy=""` 并设 `NO_PROXY=*`。

## 2 ModelScope 公开镜像（gated 仓库的正解）

```powershell
# 直链形状（无需 CLI、无需登录）
https://modelscope.cn/models/<owner>/<name>/resolve/master/<path-in-repo>

# 例：这次真正跑通的两批
aria2c --input-file=E:\视频\modelsmith\_tmp\dl\aria2-ms.txt --continue=true -x16 -s16 -k1M
```

- 本机**没有** `modelscope` CLI、**没有** HF token；上面这条路不需要装任何东西（A）。
- 仓库在 ModelScope 的名字一般与 HF 同名（`krea/Krea-2-Raw`、`black-forest-labs/FLUX.2-klein-9B` 都命中）。

## 3 判据（能变红，不许只报"下完了"）

1. **逐文件字节数**必须与 repo 元数据 `size` **逐字节一致**；给 `files_ok / missing / mismatch` 三个数，`missing+mismatch=0` 才算过。
2. **不许用文件长度判进度**：多段下载长度会虚高（BUG 总账 D-04）。
3. 落盘后写 `manifest.json`（repo / file / bytes / URL / method / 时间）到模型目录根。
4. 结束检查：无 `.aria2` 残留；`E:` 剩余磁盘报一次；**未覆盖/未删除任何已有模型**。

## 4 本次实测账（2026-10-23，A 级）

| arch | repo | 路线 | 结果 |
|---|---|---|---|
| krea2 | `krea/Krea-2-Raw`（gated=auto）+ `Qwen/Qwen3-VL-4B-Instruct` + `Qwen/Qwen-Image` | ModelScope 镜像 | 15 文件 / **35,424,424,384 B**（33.0 GiB） |
| flux2 | `black-forest-labs/FLUX.2-klein-9B`（gated=auto）+ vae/tokenizer | ModelScope 镜像 | 22 文件 / **34,722,772,650 B**（32.3 GiB） |
| z_image | `Tongyi-MAI/Z-Image` + `Z-Image-Turbo` | **本机早已齐备，未重下** | 见 `docs\MODELS-DOWNLOADED-20261023.md` |
| flux2-dev | `black-forest-labs/FLUX.2-dev` | **未下** | HF 元数据 **177.64 GB / 39 文件**，32 GB 卡训不了 |

## 5 未验 / 边界

- 代理在 HF 直连上的增益**未做 A/B**（本次是"代理开着 + ModelScope 镜像"跑通的，两者贡献分不开）。
- ModelScope 上非同名仓库的映射规则未摸（只验了两个同名 repo）。
- 大文件断点续传在**切源**（HF → ModelScope）时是否会写坏，未专门验；本次是"新目录、单源写入"，不涉及切换续传。
