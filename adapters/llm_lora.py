#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# ModelSmith · L2 adapter 分块图谱  |  详见 adapters/README.md
# 角色   : kind="llm"（自研薄 LoRA，回归基线）——由 core 的 _llm_train_command
#          直接以 CLI 形态拉起（[llm_python, llm_lora.py, --mode train, ...]）
# 上游   : 无第三方 runner；本文件就是训练器（transformers + peft）
# 主链   : dolly JSONL -> build_sample（Qwen chat 模板，只对 response 计 loss）
#          -> AutoModelForCausalLM(bf16) + LoRA(r/alpha/targets) -> AdamW
#          -> 每步打印 "step N loss X lr Y" -> out\adapter_model.safetensors
#          + adapter_config.json + train_report.json
# 参数三分: ① core 传入：--model/--data/--out/--max-steps/--epochs/--rank/
#          --alpha/--lr/--max-len/--batch/--device/--template/--assert-updated
#          ② 本文件自算：target_modules（7 个）、常量 lr 调度、截断策略
#          ③ 上游默认：transformers/peft 的行为（无独立 argparse 表）
# 关键函数: build_sample / collate / run_train / verify_adapter / run_prepare
# 产物    : out\adapter_model.safetensors、adapter_config.json、train_report.json
#          （report 内含 loss.series / run / memory / update / verify 全表）
# 纪律    : 代码 token 不许改；本文件只加注释（注释不进 AST）
# =============================================================================
"""Thin LoRA SFT runner for the ModelSmith LLM line.

Only uses libraries already present in E:\\ComfyUI\\python_embeded
(torch / transformers / peft / accelerate / safetensors); nothing is installed.

Modes
  train    (default) : jsonl (dolly-15k shape) -> chat template -> LoRA SFT -> out/
  prepare            : build samples + print data stats, no torch, no GPU
  verify             : PeftModel.from_pretrained(adapter) + forward on CPU

Loss is computed on response tokens only: prompt tokens are masked with -100.

CLI (task-20 contract):
  --model --data --out --max-steps --epochs --rank --alpha --lr --max-len
  --batch --device --template
Extra: --mode --seed --limit --grad-clip --verify-load --assert-trend

Console output is ASCII by design so it survives cp936 / cp65001 consoles.
"""

import argparse
import glob
import json
import math
import os
import random
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATA_DIR = os.path.join(PROJECT_ROOT, "resources", "02-LLM-语料", "dolly-15k")
DEFAULT_MODEL_DIR = os.path.join(PROJECT_ROOT, "resources", "03-LLM-小模型", "Qwen2.5-0.5B-Instruct")
DEFAULT_OUT = os.path.join(PROJECT_ROOT, "_runs", "llm-smoke")

# Qwen2 / Qwen2.5 decoder: q,k,v,o + gated MLP
DEFAULT_TARGETS = ["q_proj", "k_proj", "v_proj", "o_proj",
                   "gate_proj", "up_proj", "down_proj"]


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
def resolve_data_path(path):
    """Accept a file, a directory (pick dolly*.jsonl first) or None."""
    if path is None or path == "":
        path = DEFAULT_DATA_DIR
    if os.path.isdir(path):
        cands = sorted(glob.glob(os.path.join(path, "*.jsonl")))
        for c in cands:
            if "dolly" in os.path.basename(c).lower():
                return c
        if cands:
            return cands[0]
        raise SystemExit("NO_JSONL_UNDER: %s" % path)
    if not os.path.isfile(path):
        raise SystemExit("DATA_NOT_FOUND: %s" % path)
    return path


def read_dolly(path, limit=None):
    """Read dolly-shaped jsonl. Empty responses are skipped (SFT needs a target)."""
    rows = []
    empty_response = 0
    bad_json = 0
    lines = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            lines += 1
            try:
                row = json.loads(line)
            except Exception:
                bad_json += 1
                continue
            resp = (row.get("response") or "").strip()
            if not resp:
                empty_response += 1
                continue
            rows.append(row)
            if limit and len(rows) >= limit:
                break
    meta = {
        "json_lines": lines,
        "rows_read": len(rows),
        "empty_response_skipped": empty_response,
        "bad_json_skipped": bad_json,
    }
    return rows, meta


def build_user_text(row):
    """instruction + optional context -> single user turn."""
    instr = (row.get("instruction") or "").strip()
    ctx = (row.get("context") or "").strip()
    if ctx:
        return instr + "\n\n" + ctx
    return instr


def build_prompt(tok, row, template="qwen"):
    """Prompt text. 'qwen' = official chat template, 'plain' = no template markers."""
    user = build_user_text(row)
    if template == "plain":
        return user + "\n\nAnswer:\n"
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": user},
    ]
    return tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)


def build_sample(tok, row, max_len, template="qwen"):
    """One SFT sample: response tokens keep their labels, prompt tokens are -100.

    Truncation policy: the response is preserved when possible; the prompt is
    left-truncated to fit max_len.  If the response alone exceeds max_len the
    response is right-truncated and keeps its eos.
    """
    prompt = build_prompt(tok, row, template)
    prompt_ids = tok(prompt, add_special_tokens=False)["input_ids"]
    eos = tok.eos_token if tok.eos_token is not None else ""
    resp_ids = tok((row.get("response") or "").strip() + eos,
                   add_special_tokens=False)["input_ids"]
    truncated = False
    if len(resp_ids) > max_len:                      # pathological long answer
        resp_ids = resp_ids[:max_len - 1] + [tok.eos_token_id]
        truncated = True
    budget = max_len - len(resp_ids)
    if len(prompt_ids) > budget:
        prompt_ids = prompt_ids[len(prompt_ids) - budget:] if budget > 0 else []
        truncated = True
    ids = list(prompt_ids) + list(resp_ids)
    labels = [-100] * len(prompt_ids) + list(resp_ids)
    return {
        "ids": ids,
        "labels": labels,
        "prompt_len": len(prompt_ids),
        "resp_len": len(resp_ids),
        "truncated": truncated,
        "prompt_chars": len(prompt),
        "resp_chars": len((row.get("response") or "")),
        "category": row.get("category") or "unknown",
    }


def build_samples(tok, rows, max_len, template="qwen"):
    samples = []
    truncated = 0
    for row in rows:
        s = build_sample(tok, row, max_len, template)
        if s["truncated"]:
            truncated += 1
        samples.append(s)
    return samples, truncated


def collate(samples, pad_id):
    """Right padding; pad labels are -100, pad attention is 0."""
    width = max(len(s["ids"]) for s in samples)
    input_ids, attention_mask, labels = [], [], []
    for s in samples:
        pad = width - len(s["ids"])
        input_ids.append(s["ids"] + [pad_id] * pad)
        attention_mask.append([1] * len(s["ids"]) + [0] * pad)
        labels.append(s["labels"] + [-100] * pad)
    return {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
        "labels": labels,
        "batch_maxlen": width,
    }


def length_stats(samples):
    lens = sorted(len(s["ids"]) for s in samples)
    if not lens:
        return {"min": 0, "median": 0, "max": 0}
    return {"min": lens[0], "median": lens[len(lens) // 2], "max": lens[-1]}


def category_hist(samples):
    hist = {}
    for s in samples:
        hist[s["category"]] = hist.get(s["category"], 0) + 1
    return dict(sorted(hist.items(), key=lambda kv: -kv[1]))


def load_tokenizer(model_path):
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    return tok


# --------------------------------------------------------------------------- #
# train
# --------------------------------------------------------------------------- #
def _load_causal(model_path, dtype):
    """transformers>=4.56 renamed torch_dtype -> dtype; accept both."""
    from transformers import AutoModelForCausalLM
    try:
        return AutoModelForCausalLM.from_pretrained(
            model_path, dtype=dtype, low_cpu_mem_usage=True)
    except TypeError:
        return AutoModelForCausalLM.from_pretrained(
            model_path, torch_dtype=dtype, low_cpu_mem_usage=True)


def _slope(xs, ys):
    """Least-squares slope, no numpy dependency."""
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


# --------------------------------------------------------------------------- #
# label-mask probe (acceptance #2) and trainable-parameter delta (acceptance #4)
# --------------------------------------------------------------------------- #
def mask_probe(sample):
    """First/last 10 label values + the same ids, plus an exact mask verdict."""
    pl = sample["prompt_len"]
    return {
        "category": sample["category"],
        "prompt_len": pl,
        "resp_len": sample["resp_len"],
        "total_len": len(sample["ids"]),
        "labels_head10": sample["labels"][:10],
        "labels_tail10": sample["labels"][-10:],
        "ids_head10": sample["ids"][:10],
        "ids_tail10": sample["ids"][-10:],
        "mask_ok": all(x == -100 for x in sample["labels"][:pl])
                   and sample["labels"][pl:] == sample["ids"][pl:],
    }


def print_mask_probe(p, tag="mask_probe"):
    print("%s: category=%s prompt_len=%d resp_len=%d total=%d"
          % (tag, p["category"], p["prompt_len"], p["resp_len"], p["total_len"]))
    print("%s: labels[0:10]=%s" % (tag, p["labels_head10"]))
    print("%s: labels[-10:]=%s" % (tag, p["labels_tail10"]))
    print("%s: ids[0:10]=%s" % (tag, p["ids_head10"]))
    print("%s: ids[-10:]=%s" % (tag, p["ids_tail10"]))
    print("%s: mask_ok=%s (prompt all -100 AND response labels == ids)"
          % (tag, p["mask_ok"]))


def snapshot_trainables(model):
    """float32 CPU copies of every trainable parameter (4.4M -> ~18 MB)."""
    return {n: p.detach().float().cpu().clone()
            for n, p in model.named_parameters() if p.requires_grad}


def compute_update(model, snap, top_k=5):
    """L2 delta between the snapshot and the live parameters."""
    total_sq = 0.0
    init_sq = 0.0
    b_sq = 0.0
    changed = 0
    unchanged = []
    rows = []
    for n, p in model.named_parameters():
        if n not in snap:
            continue
        d = p.detach().float().cpu() - snap[n]
        sq = float((d * d).sum())
        total_sq += sq
        init_sq += float((snap[n] * snap[n]).sum())
        dn = math.sqrt(sq)
        if dn > 0.0:
            changed += 1
        else:
            unchanged.append(n)
        if "lora_B" in n:
            b_sq += float((p.detach().float().cpu() ** 2).sum())
        rows.append((dn, n))
    rows.sort(reverse=True)
    return {
        "trainable_tensors": len(rows),
        "changed_tensors": changed,
        "unchanged_tensors": len(unchanged),
        "unchanged_names": unchanged[:10],
        "delta_l2_total": round(math.sqrt(total_sq), 8),
        "init_l2_total": round(math.sqrt(init_sq), 8),
        "delta_l2_relative": round(math.sqrt(total_sq) / max(1e-12, math.sqrt(init_sq)), 8),
        "b_delta_l2_total": round(math.sqrt(b_sq), 8),
        "largest_deltas": [{"name": n, "delta_l2": round(d, 8)} for d, n in rows[:top_k]],
        "smallest_deltas": [{"name": n, "delta_l2": round(d, 8)} for d, n in rows[-top_k:]],
    }


def run_train(args):
    import torch
    import transformers
    import peft
    from peft import LoraConfig, get_peft_model

    t_start = time.perf_counter()

    # ---- data (fail fast, before touching the GPU) ------------------------ #
    data_path = resolve_data_path(args.data)
    rows, meta = read_dolly(data_path, limit=args.limit)
    if not rows:
        raise SystemExit("NO_SAMPLES: %s" % data_path)

    tok = load_tokenizer(args.model)
    samples, truncated = build_samples(tok, rows, args.max_len, args.template)
    if len(samples) != len(rows):
        raise SystemExit("SAMPLE_COUNT_MISMATCH %d != %d" % (len(samples), len(rows)))
    n = len(samples)
    lens = length_stats(samples)
    hist = category_hist(samples)
    print("data: %s" % data_path)
    print("data: rows=%d empty_response_skipped=%d bad_json_skipped=%d samples=%d truncated=%d"
          % (meta["rows_read"], meta["empty_response_skipped"],
             meta["bad_json_skipped"], n, truncated))
    print("data: token_len min=%d median=%d max=%d" % (lens["min"], lens["median"], lens["max"]))
    probe = mask_probe(samples[0])
    print_mask_probe(probe)

    # ---- device / model --------------------------------------------------- #
    if args.device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    else:
        device = args.device
    if device.startswith("cuda") and not torch.cuda.is_available():
        raise SystemExit("CUDA_REQUESTED_BUT_UNAVAILABLE")

    torch.manual_seed(args.seed)
    random.seed(args.seed)
    if device.startswith("cuda"):
        torch.cuda.manual_seed_all(args.seed)
        torch.cuda.reset_peak_memory_stats()
        print("cuda: %s (%.1f GiB)" % (torch.cuda.get_device_name(0),
                                       torch.cuda.get_device_properties(0).total_memory / 2 ** 30))

    dtype = torch.bfloat16 if args.dtype == "bf16" else torch.float32
    model = _load_causal(args.model, dtype)
    model.config.use_cache = False
    model.to(device)
    print("model: %s dtype=%s device=%s" % (args.model, args.dtype, device))

    lora_cfg = LoraConfig(
        r=args.rank,
        lora_alpha=args.alpha,
        lora_dropout=args.lora_dropout,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=DEFAULT_TARGETS,
    )
    model = get_peft_model(model, lora_cfg)
    trainable = [p for p in model.parameters() if p.requires_grad]
    n_train = sum(p.numel() for p in trainable)
    n_all = sum(p.numel() for p in model.parameters())
    print("lora: r=%d alpha=%d dropout=%.2f targets=%s"
          % (args.rank, args.alpha, args.lora_dropout, ",".join(DEFAULT_TARGETS)))
    print("lora: trainable=%d total=%d pct=%.4f%%"
          % (n_train, n_all, 100.0 * n_train / max(1, n_all)))

    # acceptance #4: snapshot BEFORE any optimizer step; lora_B must start at 0
    snap = snapshot_trainables(model)
    b_init_all_zero = all(float(v.abs().max()) == 0.0
                          for k, v in snap.items() if "lora_B" in k)
    print("update: snapshot taken before training, tensors=%d, lora_B_init_all_zero=%s"
          % (len(snap), b_init_all_zero))

    optimizer = torch.optim.AdamW(trainable, lr=args.lr, betas=(0.9, 0.999),
                                  weight_decay=0.0)
    pad_id = tok.pad_token_id

    samples_per_epoch = int(math.ceil(n / float(args.batch)))
    planned = args.max_steps if args.max_steps > 0 else samples_per_epoch * args.epochs
    print("plan: samples=%d batch=%d samples_per_epoch=%d epochs=%d max_steps=%d planned_steps=%d"
          % (n, args.batch, samples_per_epoch, args.epochs, args.max_steps, planned))

    model.train()
    loss_series = []
    steps_done = 0
    samples_used = 0
    peak_alloc = 0.0
    t_train = time.perf_counter()
    stop = False
    for epoch in range(1, args.epochs + 1):
        order = list(range(n))
        random.shuffle(order)
        for i in range(0, n, args.batch):
            idx = order[i:i + args.batch]
            batch = collate([samples[j] for j in idx], pad_id)
            tensors = {k: torch.tensor(batch[k], dtype=torch.long, device=device)
                       for k in ("input_ids", "attention_mask", "labels")}
            out = model(**tensors)
            loss = out.loss
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            if args.grad_clip > 0:
                torch.nn.utils.clip_grad_norm_(trainable, args.grad_clip)
            optimizer.step()

            steps_done += 1
            samples_used += len(idx)
            val = float(loss.detach().float().cpu())
            if not math.isfinite(val):
                raise SystemExit("NON_FINITE_LOSS at step %d -> %r" % (steps_done, val))
            lr_now = float(optimizer.param_groups[0]["lr"])
            loss_series.append({"step": steps_done, "loss": round(val, 6),
                                "lr": lr_now, "batch": len(idx),
                                "maxlen": batch["batch_maxlen"]})
            print("step %d loss %.4f lr %.6e" % (steps_done, val, lr_now), flush=True)
            if device.startswith("cuda"):
                peak_alloc = max(peak_alloc,
                                 torch.cuda.max_memory_allocated() / 2 ** 20)
            if args.max_steps > 0 and steps_done >= args.max_steps:
                stop = True
                break
        if stop:
            break
    t_train_elapsed = time.perf_counter() - t_train
    if steps_done == 0:
        raise SystemExit("NO_STEPS_RUN")

    # ---- acceptance #4: parameters actually moved ------------------------- #
    upd = compute_update(model, snap)
    print("update: delta_l2_total=%.8f init_l2_total=%.8f relative=%.8f"
          % (upd["delta_l2_total"], upd["init_l2_total"], upd["delta_l2_relative"]))
    print("update: changed=%d/%d unchanged=%d b_delta_l2=%.8f"
          % (upd["changed_tensors"], upd["trainable_tensors"],
             upd["unchanged_tensors"], upd["b_delta_l2_total"]))

    # ---- loss trend (criterion for the smoke run) ------------------------- #
    ys = [p["loss"] for p in loss_series]
    xs = [float(p["step"]) for p in loss_series]
    k = max(1, min(5, len(ys) // 2)) if len(ys) >= 2 else 1
    first_k = sum(ys[:k]) / k
    last_k = sum(ys[-k:]) / k
    slope = _slope(xs, ys)
    down_slope = slope < 0
    down_means = last_k < first_k
    print("loss: first=%.4f last=%.4f mean=%.4f first%d_mean=%.4f last%d_mean=%.4f slope=%.6f down=(slope:%s,means:%s)"
          % (ys[0], ys[-1], sum(ys) / len(ys), k, first_k, k, last_k,
             slope, down_slope, down_means))

    # ---- save -------------------------------------------------------------- #
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    model.save_pretrained(out_dir)
    adapter_file = os.path.join(out_dir, "adapter_model.safetensors")
    cfg_file = os.path.join(out_dir, "adapter_config.json")
    if not (os.path.isfile(adapter_file) and os.path.getsize(adapter_file) > 0):
        raise SystemExit("SAVE_FAILED: %s" % adapter_file)
    if not os.path.isfile(cfg_file):
        raise SystemExit("SAVE_FAILED: %s" % cfg_file)
    with open(cfg_file, "r", encoding="utf-8") as f:
        saved_cfg = json.load(f)
    if int(saved_cfg.get("r", -1)) != args.rank or int(saved_cfg.get("lora_alpha", -1)) != args.alpha:
        raise SystemExit("ADAPTER_CFG_MISMATCH r=%r alpha=%r"
                         % (saved_cfg.get("r"), saved_cfg.get("lora_alpha")))
    print("saved: %s" % adapter_file)
    print("saved: %s" % cfg_file)

    peak_alloc_final = peak_alloc
    peak_reserved = 0.0
    if device.startswith("cuda"):
        peak_alloc_final = torch.cuda.max_memory_allocated() / 2 ** 20
        peak_reserved = torch.cuda.max_memory_reserved() / 2 ** 20

    # free the GPU before any CPU reload
    del model, optimizer
    import gc
    gc.collect()
    if device.startswith("cuda"):
        torch.cuda.empty_cache()

    verify = None
    if args.verify_load:
        verify = verify_adapter(args.model, out_dir, tok=None)
        print("VERIFY_RELOAD %s lora_tensors=%d logits_diff=%.6f"
              % ("PASS" if verify["ok"] else "FAIL", verify["lora_tensors"],
                 verify["logits_diff_max"]))

    # ---- report ------------------------------------------------------------ #
    report = {
        "status": "ok",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "mode": "train",
        "paths": {"model": os.path.abspath(args.model),
                  "data": os.path.abspath(data_path),
                  "out": out_dir},
        "env": {
            "python": sys.version.split()[0],
            "torch": torch.__version__,
            "cuda": torch.version.cuda,
            "transformers": transformers.__version__,
            "peft": getattr(peft, "__version__", "?"),
            "device": device,
            "gpu_name": torch.cuda.get_device_name(0) if device.startswith("cuda") else None,
            "gpu_total_mib": (torch.cuda.get_device_properties(0).total_memory / 2 ** 20)
                             if device.startswith("cuda") else None,
        },
        "lora": {"r": args.rank, "alpha": args.alpha, "dropout": args.lora_dropout,
                 "target_modules": DEFAULT_TARGETS,
                 "trainable_params": n_train, "total_params": n_all,
                 "trainable_pct": round(100.0 * n_train / max(1, n_all), 4)},
        "optimizer": {"name": "AdamW", "lr": args.lr, "betas": [0.9, 0.999],
                      "weight_decay": 0.0, "grad_clip": args.grad_clip,
                      "schedule": "constant"},
        "data": dict(meta, samples=n, truncated=truncated,
                     token_len=lens, categories=hist,
                     template=args.template, max_len=args.max_len,
                     mask_probe=probe),
        "run": {"batch": args.batch, "max_len": args.max_len,
                "max_steps": args.max_steps, "epochs": args.epochs,
                "planned_steps": planned, "steps_done": steps_done,
                "samples_used": samples_used,
                "train_elapsed_s": round(t_train_elapsed, 3),
                "total_elapsed_s": round(time.perf_counter() - t_start, 3),
                "steps_per_s": round(steps_done / max(1e-9, t_train_elapsed), 4)},
        "loss": {"series": loss_series, "first": ys[0], "last": ys[-1],
                 "mean": round(sum(ys) / len(ys), 6),
                 "first%d_mean" % k: round(first_k, 6),
                 "last%d_mean" % k: round(last_k, 6),
                 "slope_per_step": round(slope, 8),
                 "down_slope": down_slope, "down_means": down_means,
                 "all_finite": True},
        "memory": {"peak_vram_alloc_mib": round(peak_alloc_final, 1),
                   "peak_vram_reserved_mib": round(peak_reserved, 1)},
        "update": dict(upd, b_init_all_zero=b_init_all_zero,
                       snapshot_before_training=True),
        "artifacts": {
            "adapter_model.safetensors": {"path": adapter_file,
                                          "bytes": os.path.getsize(adapter_file)},
            "adapter_config.json": {"path": cfg_file,
                                    "r": saved_cfg.get("r"),
                                    "lora_alpha": saved_cfg.get("lora_alpha"),
                                    "target_modules": saved_cfg.get("target_modules")},
        },
        "verify": verify,
    }
    report_path = os.path.join(out_dir, "train_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("report: %s" % report_path)
    print("stats: steps=%d elapsed=%.2fs peak_vram=%.1fMiB"
          % (steps_done, t_train_elapsed, peak_alloc_final))

    if args.assert_trend and not down_slope:
        print("TREND_ASSERT_FAIL slope=%.8f" % slope)
        return 3
    if args.assert_updated and (upd["changed_tensors"] == 0 or upd["delta_l2_total"] <= 0.0):
        print("UPDATE_ASSERT_FAIL delta_l2_total=%.8f changed=%d"
              % (upd["delta_l2_total"], upd["changed_tensors"]))
        return 5
    print("TRAIN_OK")
    return 0


# --------------------------------------------------------------------------- #
# verify (CPU only): PeftModel.from_pretrained + forward + adapter-applied proof
# --------------------------------------------------------------------------- #
def verify_adapter(model_path, adapter_dir, tok=None):
    import torch
    from peft import PeftModel

    adapter_dir = os.path.abspath(adapter_dir)
    cfg_path = os.path.join(adapter_dir, "adapter_config.json")
    w_path = os.path.join(adapter_dir, "adapter_model.safetensors")
    res = {"ok": False, "adapter_dir": adapter_dir, "error": None,
           "files": {"adapter_config.json": os.path.isfile(cfg_path),
                     "adapter_model.safetensors": os.path.isfile(w_path)},
           "adapter_model_bytes": os.path.getsize(w_path) if os.path.isfile(w_path) else 0}
    if not (res["files"]["adapter_config.json"] and res["files"]["adapter_model.safetensors"]):
        res["error"] = "missing adapter files"
        return res
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    if tok is None:
        tok = load_tokenizer(model_path)

    # float32 on CPU: no GPU memory, numerically safe for a short sanity pass
    base = _load_causal(model_path, torch.float32)
    base.eval()
    ids = tok("Hello", return_tensors="pt").input_ids
    with torch.no_grad():
        base_logits = base(input_ids=ids).logits.clone()
    lora_model = PeftModel.from_pretrained(base, adapter_dir)
    lora_model.eval()
    with torch.no_grad():
        ad_logits = lora_model(input_ids=ids).logits
    diff = float((ad_logits - base_logits).abs().max())
    lora_tensors = [n for n, _ in lora_model.named_parameters() if "lora_" in n]
    finite = bool(torch.isfinite(ad_logits).all())
    targets = list(cfg.get("target_modules") or [])
    ok = (len(lora_tensors) > 0 and finite and diff > 0.0
          and int(cfg.get("r", -1)) > 0)
    res.update({
        "ok": ok,
        "lora_tensors": len(lora_tensors),
        "lora_tensor_names_sample": lora_tensors[:4],
        "r": cfg.get("r"),
        "lora_alpha": cfg.get("lora_alpha"),
        "target_modules": targets,
        "targets_cover_default": set(DEFAULT_TARGETS).issubset(set(targets)),
        "base_model_name_or_path": cfg.get("base_model_name_or_path"),
        "logits_shape": list(ad_logits.shape),
        "logits_finite": finite,
        "logits_diff_max": diff,
        "adapter_effect_nonzero": diff > 0.0,
    })
    if not ok:
        res["error"] = "verify assertions failed"
    return res


def run_verify(args):
    adapter_dir = args.adapter or args.out
    res = verify_adapter(args.model, adapter_dir)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if res["ok"]:
        print("VERIFY_RELOAD PASS lora_tensors=%d r=%s alpha=%s targets_cover=%s logits_diff=%.6f"
              % (res["lora_tensors"], res["r"], res["lora_alpha"],
                 res["targets_cover_default"], res["logits_diff_max"]))
        return 0
    print("VERIFY_RELOAD FAIL %s" % res.get("error"))
    return 4


# --------------------------------------------------------------------------- #
# prepare (no torch, no GPU)
# --------------------------------------------------------------------------- #
def run_prepare(args):
    tok = load_tokenizer(args.model)
    data_path = resolve_data_path(args.data)
    rows, meta = read_dolly(data_path, limit=args.limit)
    samples, truncated = build_samples(tok, rows, args.max_len, args.template)
    probe = mask_probe(samples[0])
    print_mask_probe(probe)
    out = {"paths": {"model": os.path.abspath(args.model),
                     "data": os.path.abspath(data_path)},
           "data": dict(meta, samples=len(samples), truncated=truncated,
                        token_len=length_stats(samples),
                        categories=category_hist(samples),
                        template=args.template, max_len=args.max_len,
                        mask_probe=probe)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print("PREPARE_OK")
    return 0


# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(description="Thin LoRA SFT runner (transformers+peft)")
    p.add_argument("--mode", choices=["train", "prepare", "verify"], default="train")
    p.add_argument("--model", default=DEFAULT_MODEL_DIR)
    p.add_argument("--data", default=DEFAULT_DATA_DIR,
                   help="jsonl file or directory containing it")
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--adapter", default=None, help="verify mode: adapter dir (default --out)")
    p.add_argument("--max-steps", type=int, default=0, help="0 = full epochs")
    p.add_argument("--epochs", type=int, default=1)
    p.add_argument("--rank", type=int, default=8)
    p.add_argument("--alpha", type=int, default=16)
    p.add_argument("--lr", type=float, default=5e-4)
    p.add_argument("--max-len", type=int, default=512)
    p.add_argument("--batch", type=int, default=1)
    p.add_argument("--device", default="auto", help="auto | cuda | cuda:0 | cpu")
    p.add_argument("--template", default="qwen", choices=["qwen", "plain"])
    p.add_argument("--dtype", default="bf16", choices=["bf16", "fp32"])
    p.add_argument("--lora-dropout", type=float, default=0.0)
    p.add_argument("--grad-clip", type=float, default=1.0)
    p.add_argument("--limit", type=int, default=0, help="cap rows read (0 = all)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--verify-load", action="store_true",
                   help="after saving, reload adapter on CPU and assert it applies")
    p.add_argument("--assert-trend", action="store_true",
                   help="exit 3 if the loss slope over the run is not negative")
    p.add_argument("--assert-updated", action="store_true",
                   help="exit 5 if no trainable parameter moved (delta L2 == 0)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.max_len < 8:
        raise SystemExit("--max-len must be >= 8")
    if args.limit and args.limit < args.batch:
        raise SystemExit("--limit must be >= --batch")
    if args.mode == "train":
        return run_train(args)
    if args.mode == "verify":
        return run_verify(args)
    return run_prepare(args)


if __name__ == "__main__":
    sys.exit(main())
