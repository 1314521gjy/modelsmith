#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GPU-free data-pipeline selftest for the ModelSmith LLM line.

Uses the real tokenizer (transformers) and the REAL functions from llm_lora.py
-- no copy of the pipeline, no torch import, no CUDA initialisation.

  python adapters\\selftest_llm.py --model <dir> --data <jsonl-or-dir> [--limit 512]
  MUST run with E:\\ComfyUI\\python_embeded\\python.exe（系统 Python312 无 torch/transformers，会得 0/1 假红）

Every check prints PASS/FAIL with its numbers; exit code != 0 if any FAIL.
Each PASS has a negative control that would turn it red (see --help notes below):
  * truncated/not-truncated pair at max_len 64 vs 512 proves the truncation branch
  * plain template run proves the qwen template markers are a real switch
  * synthetic jsonl (good/empty/bad line) proves the skip counters are wired
  * mask boundary check asserts prompt_len-1 is -100 AND prompt_len is not -100
"""

import argparse
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import llm_lora as L  # noqa: E402  (import after sys.path fix)


class Ledger:
    def __init__(self):
        self.p = 0
        self.f = 0
        self.rows = []

    def check(self, ok, cid, name, detail):
        tag = "PASS" if ok else "FAIL"
        if ok:
            self.p += 1
        else:
            self.f += 1
        self.rows.append({"id": cid, "name": name, "ok": bool(ok), "detail": detail})
        print("%s %02d %-34s :: %s" % (tag, cid, name, detail), flush=True)
        return ok

    def info(self, cid, name, detail):
        print("INFO %02d %-34s :: %s" % (cid, name, detail), flush=True)

    def summary(self):
        total = self.p + self.f
        print("-" * 78)
        print("SELFTEST_LLM %s  PASS %d / FAIL %d  (checks=%d)"
              % ("PASS" if self.f == 0 else "FAIL", self.p, self.f, total))
        return 0 if self.f == 0 else 1


def main(argv=None):
    p = argparse.ArgumentParser(description="GPU-free data pipeline selftest")
    p.add_argument("--model", default=L.DEFAULT_MODEL_DIR)
    p.add_argument("--data", default=L.DEFAULT_DATA_DIR)
    p.add_argument("--limit", type=int, default=512, help="rows tokenized (0 = all)")
    p.add_argument("--max-len", type=int, default=512)
    p.add_argument("--batch", type=int, default=4)
    p.add_argument("--template", default="qwen", choices=["qwen", "plain"])
    p.add_argument("--json-out", default=None, help="optional machine-readable result")
    args = p.parse_args(argv)
    T = Ledger()

    # 01 ---- model dir / tokenizer ----------------------------------------- #
    model_dir = os.path.abspath(args.model)
    need = ["config.json"]
    have_cfg = os.path.isfile(os.path.join(model_dir, "config.json"))
    tok_files = [f for f in ("tokenizer.json", "vocab.json", "merges.txt",
                             "tokenizer_config.json")
                 if os.path.isfile(os.path.join(model_dir, f))]
    tok = None
    err = ""
    try:
        tok = L.load_tokenizer(model_dir)
    except Exception as e:  # noqa: BLE001 - reported, not swallowed
        err = "%s: %s" % (type(e).__name__, e)
    T.check(tok is not None, 1, "tokenizer_loads",
            "dir=%s cfg=%s tok_files=%s%s"
            % (model_dir, have_cfg, ",".join(tok_files) or "none",
               "" if tok is not None else " ERR=" + err))
    if tok is None:
        print("cannot continue without tokenizer")
        return T.summary()
    T.info(2, "tokenizer_meta", "class=%s vocab=%s pad=%r id=%s eos=%r id=%s"
           % (type(tok).__name__, getattr(tok, "vocab_size", "?"),
              tok.pad_token, tok.pad_token_id, tok.eos_token, tok.eos_token_id))
    T.check(tok.pad_token_id is not None and tok.eos_token_id is not None,
            3, "pad_eos_defined",
            "pad=%s eos=%s" % (tok.pad_token_id, tok.eos_token_id))

    # 04 ---- data file ------------------------------------------------------ #
    data_path = L.resolve_data_path(args.data)
    size = os.path.getsize(data_path) if os.path.isfile(data_path) else 0
    T.check(size > 1_000_000, 4, "data_file_size_gt_1MB",
            "path=%s bytes=%d" % (data_path, size))
    rows, meta = L.read_dolly(data_path, limit=args.limit or None)
    T.check(len(rows) > 0, 5, "rows_positive",
            "rows=%d meta=%s" % (len(rows), json.dumps(meta, ensure_ascii=False)))
    T.check(meta["rows_read"] + meta["empty_response_skipped"] + meta["bad_json_skipped"]
            == meta["json_lines"], 6, "jsonl_accounting_exact",
            "rows=%d empty=%d bad=%d lines=%d"
            % (meta["rows_read"], meta["empty_response_skipped"],
               meta["bad_json_skipped"], meta["json_lines"]))
    T.check(len(rows) >= 100, 7, "rows_ge_100",
            "rows=%d (limit=%d)" % (len(rows), args.limit))

    # 08 ---- sample building ------------------------------------------------ #
    samples, truncated = L.build_samples(tok, rows, args.max_len, args.template)
    T.check(len(samples) == len(rows) and len(samples) > 0, 8, "samples_built",
            "samples=%d rows=%d truncated=%d max_len=%d"
            % (len(samples), len(rows), truncated, args.max_len))

    hist = L.category_hist(samples)
    T.check(len(hist) >= 3, 9, "category_diversity_ge_3", json.dumps(hist, ensure_ascii=False))

    lens = L.length_stats(samples)
    T.check(all(len(s["ids"]) <= args.max_len for s in samples), 10,
            "all_len_le_max_len",
            "len min=%d median=%d max=%d cap=%d"
            % (lens["min"], lens["median"], lens["max"], args.max_len))

    # 11 ---- prompt masked exactly ----------------------------------------- #
    bad_prefix = [i for i, s in enumerate(samples)
                  if any(x != -100 for x in s["labels"][:s["prompt_len"]])]
    T.check(len(bad_prefix) == 0, 11, "prompt_tokens_all_minus100",
            "violations=%d of %d (mask prefix = prompt_len)"
            % (len(bad_prefix), len(samples)))

    # boundary: last prompt token masked AND first response token labelled
    bad_boundary = []
    for i, s in enumerate(samples):
        if s["prompt_len"] == 0:
            continue
        if s["labels"][s["prompt_len"] - 1] != -100:
            bad_boundary.append((i, "last_prompt_not_masked"))
        if s["prompt_len"] < len(s["labels"]) and s["labels"][s["prompt_len"]] == -100:
            bad_boundary.append((i, "first_response_masked"))
    T.check(len(bad_boundary) == 0, 12, "mask_boundary_exact",
            "violations=%d %s" % (len(bad_boundary), bad_boundary[:3]))

    # 13 ---- response labels equal ids ------------------------------------- #
    bad_suffix = [i for i, s in enumerate(samples)
                  if s["labels"][s["prompt_len"]:] != s["ids"][s["prompt_len"]:]]
    T.check(len(bad_suffix) == 0, 13, "labels_echo_ids_on_response",
            "violations=%d" % len(bad_suffix))

    # 14 ---- every sample has a trainable label ---------------------------- #
    no_label = [i for i, s in enumerate(samples)
                if sum(1 for x in s["labels"] if x != -100) == 0]
    unmasked = [sum(1 for x in s["labels"] if x != -100) for s in samples]
    T.check(len(no_label) == 0, 14, "labels_nonempty_every_sample",
            "empty=%d unmasked_min=%d unmasked_median=%d unmasked_max=%d"
            % (len(no_label), min(unmasked), sorted(unmasked)[len(unmasked) // 2],
               max(unmasked)))
    T.check(all(u == s["resp_len"] for u, s in zip(unmasked, samples)), 15,
            "unmasked_count_eq_resp_len",
            "mismatch=%d" % sum(1 for u, s in zip(unmasked, samples)
                                if u != s["resp_len"]))

    # 16 ---- eos keeps the stop signal ------------------------------------- #
    no_eos = [i for i, s in enumerate(samples)
              if s["ids"][-1] != tok.eos_token_id]
    T.check(len(no_eos) == 0, 16, "eos_is_last_token",
            "missing_eos=%d of %d (eos_id=%s)"
            % (len(no_eos), len(samples), tok.eos_token_id))

    # 17 ---- truncation branch is reachable (negative control 64 vs 512) --- #
    _, trunc64 = L.build_samples(tok, rows, 64, args.template)
    samples64, _ = L.build_samples(tok, rows, 64, args.template)
    T.check(trunc64 > 0, 17, "truncation_branch_fires",
            "truncated@64=%d of %d (a max_len where nothing truncates would be a red flag)"
            % (trunc64, len(rows)))
    T.check(all(len(s["ids"]) <= 64 for s in samples64), 18,
            "truncated_len_within_64",
            "max=%d" % max(len(s["ids"]) for s in samples64))
    T.check(truncated < trunc64, 19, "truncation_monotone_in_max_len",
            "truncated@%d=%d < truncated@64=%d" % (args.max_len, truncated, trunc64))
    T.check(all(sum(1 for x in s["labels"] if x != -100) == s["resp_len"]
                for s in samples64), 20, "truncation_keeps_response_labels",
            "max_len=64 keeps every response token labelled")

    # 21 ---- collate / padding --------------------------------------------- #
    batch = samples[:args.batch]
    col = L.collate(batch, tok.pad_token_id)
    width = max(len(s["ids"]) for s in batch)
    widths = {len(col[k]) for k in ("input_ids", "attention_mask", "labels")}
    T.check(widths == {args.batch} and col["batch_maxlen"] == width, 21,
            "collate_shapes",
            "rows=%d cols=%d width=%d" % (args.batch, len(widths), width))
    pad_ok = True
    for row_att, row_lab, row_ids, s in zip(col["attention_mask"], col["labels"],
                                            col["input_ids"], batch):
        real = len(s["ids"])
        if any(x != 0 for x in row_att[real:]) or any(x != -100 for x in row_lab[real:]):
            pad_ok = False
        if any(x != tok.pad_token_id for x in row_ids[real:]):
            pad_ok = False
    T.check(pad_ok, 22, "collate_padding_masked",
            "pad_id=%s: attn=0, label=-100, ids=pad on the padded tail"
            % tok.pad_token_id)
    T.check(all(sum(1 for x in lab if x != -100) == s["resp_len"]
                for lab, s in zip(col["labels"], batch)), 23,
            "collate_preserves_labels",
            "unmasked per row=%s resp_len=%s"
            % ([sum(1 for x in lab if x != -100) for lab in col["labels"]],
               [s["resp_len"] for s in batch]))

    # 24 ---- template switch is real (negative control: plain) ------------ #
    q_prompt = L.build_prompt(tok, rows[0], "qwen")
    p_prompt = L.build_prompt(tok, rows[0], "plain")
    T.check("<|im_start|>" in q_prompt and "assistant" in q_prompt, 24,
            "qwen_template_markers",
            "len=%d ends_with=%r" % (len(q_prompt), q_prompt[-24:]))
    T.check("<|im_start|>" not in p_prompt, 25, "plain_template_has_no_markers",
            "len=%d (switch is real, not cosmetic)" % len(p_prompt))
    T.check(L.build_sample(tok, rows[0], args.max_len, "qwen")["ids"]
            != L.build_sample(tok, rows[0], args.max_len, "plain")["ids"], 26,
            "template_changes_token_ids",
            "qwen prompt_len=%d plain prompt_len=%d"
            % (L.build_sample(tok, rows[0], args.max_len, "qwen")["prompt_len"],
               L.build_sample(tok, rows[0], args.max_len, "plain")["prompt_len"]))

    # 27 ---- determinism ---------------------------------------------------- #
    a = L.build_sample(tok, rows[0], args.max_len, args.template)
    b = L.build_sample(tok, rows[0], args.max_len, args.template)
    T.check(a["ids"] == b["ids"] and a["labels"] == b["labels"], 27,
            "build_sample_deterministic",
            "ids=%d labels_unmasked=%d"
            % (len(a["ids"]), sum(1 for x in a["labels"] if x != -100)))

    # 28 ---- skip logic on a synthetic file -------------------------------- #
    tmpdir = tempfile.mkdtemp(prefix="ms_llm_selftest_")
    tmp = os.path.join(tmpdir, "mini.jsonl")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps({"instruction": "Say hi", "context": "",
                            "response": "Hi.", "category": "open_qa"}) + "\n")
        f.write(json.dumps({"instruction": "Empty answer", "context": "",
                            "response": "   ", "category": "open_qa"}) + "\n")
        f.write("{this is not json\n")
    mini_rows, mini_meta = L.read_dolly(tmp)
    ok_skip = (len(mini_rows) == 1 and mini_meta["empty_response_skipped"] == 1
               and mini_meta["bad_json_skipped"] == 1 and mini_meta["json_lines"] == 3)
    T.check(ok_skip, 28, "skip_counters_exact_synthetic",
            json.dumps(mini_meta, ensure_ascii=False))
    try:
        os.remove(tmp)
        os.rmdir(tmpdir)
    except OSError:
        pass

    # 29 ---- long-context rows exist (why truncation matters) ------------- #
    max_chars = 0
    n_long = 0
    with open(data_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            c = len(L.build_user_text(r))
            max_chars = max(max_chars, c)
            if c > 2000:
                n_long += 1
    T.check(max_chars > 4000, 29, "long_context_present",
            "max_user_chars=%d rows_gt_2000chars=%d" % (max_chars, n_long))

    # 30 ---- dolly schema sample ------------------------------------------- #
    r0 = rows[0]
    keys = sorted(k for k in r0.keys())
    T.check(all(k in r0 for k in ("instruction", "response", "category")), 30,
            "dolly_schema_fields", "keys=%s category=%r resp_chars=%d"
            % (keys, r0.get("category"), len((r0.get("response") or ""))))

    result = {"pass": T.p, "fail": T.f, "checks": T.rows,
              "paths": {"model": model_dir, "data": data_path},
              "params": {"limit": args.limit, "max_len": args.max_len,
                         "batch": args.batch, "template": args.template},
              "data": dict(meta, samples=len(samples), truncated=truncated,
                           token_len=lens, categories=hist)}
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print("json_out: %s" % os.path.abspath(args.json_out))
    return T.summary()


if __name__ == "__main__":
    sys.exit(main())
