#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""扫描本机模型目录，生成 v3 页面用的 model-index.js（下拉选项数据源）。

用法： python tools\\scan_models.py
输出： design\\prototype\\v3\\model-index.js  →  window.MS_MODELS = [{name,path,kind,size_gb}]
判定（看内容，不看名字）：rl_agent_config.json→laya；model_index.json→diffusion；
config.json + *.safetensors→llm；只有 *.safetensors→diffusion_single；其它 unknown（不收录）。
"""
import json
import os
from pathlib import Path

ROOT = str(
    Path(
        os.environ.get("MODELSMITH_HOME") or Path(__file__).resolve().parent.parent
    ).resolve()
)
OUT = os.path.join(ROOT, "design", "prototype", "v3", "model-index.js")
ROOTS = [
    os.path.join(ROOT, "models"),
    os.path.join(ROOT, "resources", "03-LLM-小模型"),
    os.path.join(ROOT, "resources", "07-底模"),
    str(Path(ROOT).parent / "拉亚决策模型" / "models"),
]
_qwen_model = os.environ.get("MODELSMITH_QWEN_IMAGE_MODEL")
if _qwen_model:
    ROOTS.append(_qwen_model)
SKIP_DIRS = {"_tmp", ".git", "__pycache__", "_latent_cache", "_t_e_cache"}


def kind_of(d):
    if os.path.isfile(os.path.join(d, "rl_agent_config.json")):
        return "laya"
    if os.path.isfile(os.path.join(d, "model_index.json")):
        return "diffusion"
    has_cfg = os.path.isfile(os.path.join(d, "config.json"))
    try:
        names = os.listdir(d)
    except OSError:
        return "unknown"
    has_sf = any(n.lower().endswith(".safetensors") for n in names)
    if has_cfg and has_sf:
        return "llm"
    if has_sf:
        return "diffusion_single"
    return "unknown"


def size_gb(d, cap_files=20000):
    total = 0
    n = 0
    for base, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
            n += 1
            if n >= cap_files:
                return round(total / (1024 ** 3), 2)
    return round(total / (1024 ** 3), 2)


def main():
    items, seen = [], set()
    for r in ROOTS:
        if not os.path.isdir(r):
            continue
        # 根本身就是一个模型（例如 diffusers 目录）时只收它，别再钻进 texture/transformer 子目录
        root_kind = kind_of(r)
        if root_kind != "unknown":
            cands = [r]
        else:
            cands = [r]
            for name in sorted(os.listdir(r)):
                p = os.path.join(r, name)
                if os.path.isdir(p) and name not in SKIP_DIRS:
                    cands.append(p)
        for d in cands:
            # 只看两层：根 + 一层子目录（diffusers 目录内部不再展开）
            if d in seen:
                continue
            k = kind_of(d)
            if k == "unknown":
                continue
            seen.add(d)
            items.append({"name": os.path.basename(d.rstrip("\\/")) or d,
                          "path": d, "kind": k, "size_gb": size_gb(d)})

    body = "// 由 tools\\scan_models.py 生成，勿手改\nwindow.MS_MODELS = " + \
           json.dumps(items, ensure_ascii=False, indent=2) + ";\n"
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(body)
    print("wrote %s  models=%d" % (OUT, len(items)))
    for it in items:
        print("  %-34s %-16s %6.2f GB  %s" % (it["name"], it["kind"], it["size_gb"], it["path"]))


if __name__ == "__main__":
    main()
