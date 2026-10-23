#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GPU-free selftest for adapters/lf_adapter.py (LlamaFactory v0.9.5 thin adapter).

No GPU, no training, no third_party writes:
  * YAML construction + required-field failures + aliases + coercion
  * YAML round-trip on a Chinese path
  * runner argv: sys.path injection, yaml literal round-trip, compile check
  * parse_line for every real LlamaFactory/transformers line shape
  * run() end-to-end against a stub child (fake LF output), incl. cancellation,
    spawn failure and missing-params failure
  * dependency probe (imports only; detects the "namespace package" false green)
  * static contract: every emitted YAML key exists upstream (declared or inherited)

Usage:
  python adapters\\selftest_lf_adapter.py [--skip-probe]
  MUST run with E:\\ComfyUI\\python_embeded\\python.exe（系统 Python312 无 torch/transformers，会得 23/1 假红）
Exit code 0 iff every check PASSes.
"""

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import lf_adapter as A  # noqa: E402

ROOT = Path(
    os.environ.get("MODELSMITH_HOME") or Path(__file__).resolve().parents[1]
).resolve()
BASE = {
    "model": str(ROOT / "resources" / "03-LLM-小模型" / "Qwen2.5-0.5B-Instruct"),
    "dataset": "ms_dolly_200",
    "template": "qwen",
    "out": str(ROOT / "adapters" / "_tmp" / "lf_jobs" / "out"),
}

# LlamaFactory v0.9.5: keys declared in hparams/*.py (file:line in lf_adapter.py docstring)
UPSTREAM_DECLARED = {
    "model_name_or_path", "trust_remote_code",
    "stage", "finetuning_type", "lora_alpha", "lora_dropout", "lora_rank",
    "lora_target", "plot_loss",
    "template", "dataset", "eval_dataset", "dataset_dir", "cutoff_len",
    "max_samples", "preprocessing_num_workers", "val_size",
    "overwrite_output_dir",
}
# keys inherited from transformers.Seq2SeqTrainingArguments (training_args.py:143)
UPSTREAM_INHERITED = {
    "do_train", "output_dir", "logging_steps", "save_steps", "save_only_model",
    "per_device_train_batch_size", "gradient_accumulation_steps", "learning_rate",
    "num_train_epochs", "max_steps", "lr_scheduler_type", "warmup_ratio", "bf16",
    "ddp_timeout", "resume_from_checkpoint", "dataloader_num_workers", "report_to",
}

STUB_OK = (
    'print("***** Running training *****")\n'
    'print("  Num examples = 200")\n'
    'print("  Total optimization steps = 2")\n'
    'print("  Number of trainable parameters = 4,399,104")\n'
    'for i in (1, 2):\n'
    '    print("  %d%%|## | %d/2 [00:00<00:00,  1.00it/s]" % (i * 50, i))\n'
    '    print("{\'loss\': %.4f, \'grad_norm\': 0.5, \'learning_rate\': 0.0002, \'epoch\': 0.1}" % (2.0 - i * 0.5))\n'
    'print("Saving model checkpoint to .\\\\out\\\\checkpoint-2")\n'
    'print("{\'train_runtime\': 3.21, \'train_samples_per_second\': 62.3, '
    '\'train_steps_per_second\': 0.62, \'train_loss\': 1.25, \'epoch\': 0.1}")\n'
)

STUB_PREP = (
    'print("***** Running training *****")\n'
    'print("  Total optimization steps = 2")\n'
    'print("100%|##########| 200/200 [00:00<00:00, 900.00ex/s]")\n'
    'for i in (1, 2):\n'
    '    print("  %d%%|## | %d/2 [00:00<00:00,  2.00it/s]" % (i * 50, i))\n'
    'print("{\'loss\': 1.5, \'learning_rate\': 5e-05, \'epoch\': 0.01}")\n'
)

STUB_CANCEL = (
    'import sys, time\n'
    'print("***** Running training *****")\n'
    'print("  Total optimization steps = 1000")\n'
    'for i in range(1, 1000):\n'
    '    print("  %d%%|# | %d/1000 [00:00<00:00,  1.00it/s]" % (min(99, i // 10), i))\n'
    '    sys.stdout.flush()\n'
    '    time.sleep(0.05)\n'
)


class Ledger:
    def __init__(self):
        self.p = 0
        self.f = 0
        self.rows = []

    def check(self, ok, cid, name, detail=""):
        tag = "PASS" if ok else "FAIL"
        if ok:
            self.p += 1
        else:
            self.f += 1
        self.rows.append({"id": cid, "name": name, "ok": bool(ok), "detail": detail})
        print("%s %02d %-40s :: %s" % (tag, cid, name, detail), flush=True)
        return ok

    def summary(self):
        print("-" * 78)
        print("SELFTEST_LF_ADAPTER %s  PASS %d / FAIL %d  (checks=%d)"
              % ("PASS" if self.f == 0 else "FAIL", self.p, self.f, self.p + self.f))
        return 0 if self.f == 0 else 1


def raise_of(fn, *a, **kw):
    try:
        fn(*a, **kw)
        return None
    except ValueError as exc:
        return exc


def main(argv=None):
    p = argparse.ArgumentParser(description="GPU-free selftest for lf_adapter")
    p.add_argument("--skip-probe", action="store_true",
                   help="skip the dependency probe (it imports transformers/torch)")
    p.add_argument("--json-out", default=None)
    args = p.parse_args(argv)
    T = Ledger()
    tmp = tempfile.mkdtemp(prefix="ms_lf_selftest_")

    # ---------------- 1. required fields ---------------------------------- #
    for i, key in enumerate(A.REQUIRED):
        params = dict(BASE)
        params.pop(key)
        exc = raise_of(A.build_yaml, params)
        ok = exc is not None and key in str(exc)
        T.check(ok, 1 + i, "missing_%s_raises" % key,
                "ValueError mentions %r: %s" % (key, str(exc)[:80] if exc else "NO RAISE"))

    # ---------------- 5. defaults ----------------------------------------- #
    cfg = A.build_yaml(dict(BASE))
    expect = {"stage": "sft", "do_train": True, "finetuning_type": "lora",
              "lora_rank": 8, "lora_alpha": 16, "lora_target": "all",
              "lora_dropout": 0.0, "cutoff_len": 2048, "logging_steps": 10,
              "save_steps": 500, "report_to": "none", "bf16": True,
              "max_steps": -1, "num_train_epochs": 3.0, "learning_rate": 5e-5,
              "per_device_train_batch_size": 1, "gradient_accumulation_steps": 1,
              "plot_loss": False, "overwrite_output_dir": True,
              "save_only_model": False, "ddp_timeout": 180000000,
              "lr_scheduler_type": "cosine", "warmup_ratio": 0.0,
              "model_name_or_path": BASE["model"], "dataset": BASE["dataset"],
              "template": BASE["template"], "output_dir": BASE["out"]}
    bad = {k: (v, cfg.get(k)) for k, v in expect.items() if cfg.get(k) != v}
    T.check(not bad, 5, "defaults_match_upstream", "mismatch=%s" % bad)

    optional = ("max_samples", "dataset_dir", "eval_dataset", "val_size",
                "preprocessing_num_workers", "dataloader_num_workers",
                "resume_from_checkpoint", "trust_remote_code")
    present = [k for k in optional if k in cfg]
    T.check(not present, 6, "optional_absent_by_default", "unexpected=%s" % present)

    params = dict(BASE)
    params.update({"max_samples": 200, "dataset_dir": r"E:\tmp\data",
                   "trust_remote_code": False, "eval_dataset": "x"})
    cfg2 = A.build_yaml(params)
    got = [k for k in ("max_samples", "dataset_dir", "trust_remote_code", "eval_dataset")
           if k in cfg2]
    T.check(len(got) == 4, 7, "optional_emitted_when_given", "keys=%s" % got)

    # ---------------- 8. aliases + coercion ------------------------------- #
    alias = {"model_name_or_path": BASE["model"], "dataset": BASE["dataset"],
             "template": "qwen", "output_dir": BASE["out"],
             "max_steps": "2", "num_train_epochs": "1", "lora_rank": "16"}
    cfg3 = A.build_yaml(alias)
    ok = (cfg3["model_name_or_path"] == BASE["model"]
          and cfg3["output_dir"] == BASE["out"]
          and cfg3["max_steps"] == 2 and isinstance(cfg3["max_steps"], int)
          and cfg3["num_train_epochs"] == 1.0
          and cfg3["lora_alpha"] == 32)  # alpha defaults to rank*2
    T.check(ok, 8, "alias_and_coercion",
            "max_steps=%r(%s) epochs=%r alpha=%r"
            % (cfg3["max_steps"], type(cfg3["max_steps"]).__name__,
               cfg3["num_train_epochs"], cfg3["lora_alpha"]))

    # ---------------- 9. bad values --------------------------------------- #
    bads = [("finetuning_type", "qlora"), ("lora_rank", 0), ("cutoff_len", 8)]
    errs = []
    for k, v in bads:
        q = dict(BASE)
        q[k] = v
        e = raise_of(A.build_yaml, q)
        errs.append("%s=%r -> %s" % (k, v, "raises" if e else "NO RAISE"))
    T.check(all(raise_of(A.build_yaml, dict(BASE, **{k: v})) is not None for k, v in bads),
            9, "bad_values_rejected", "; ".join(errs))

    # ---------------- 10. YAML round-trip on a Chinese path --------------- #
    cn_dir = os.path.join(tmp, "中文 路径")
    yaml_path = A.write_yaml(A.build_yaml(dict(BASE, max_steps=2)),
                             os.path.join(cn_dir, "lf job.yaml"))
    import yaml as _yaml
    with open(yaml_path, "r", encoding="utf-8") as f:
        loaded = _yaml.safe_load(f)
    T.check(loaded == A.build_yaml(dict(BASE, max_steps=2)), 10,
            "yaml_roundtrip_utf8_path",
            "bytes=%d keys=%d path_has_space_and_cjk=%s"
            % (os.path.getsize(yaml_path), len(loaded), "中文 路径" in yaml_path))

    # ---------------- 11. argv / runner literal --------------------------- #
    argv_child = A.build_argv(yaml_path)
    runner = argv_child[2]
    m = re.search(r"sys\.argv=\['llamafactory-cli','train',(.*?)\];", runner)
    lit_ok = False
    if m:
        try:
            lit_ok = (os.path.abspath(ast.literal_eval(m.group(1))) == os.path.abspath(yaml_path))
        except Exception:
            lit_ok = False
    paths_ok = False
    pm = re.search(r"sys\.path\.insert\(0,(.*?)\); sys\.path\.insert\(0,(.*?)\);", runner)
    if pm:
        try:
            libs_lit = os.path.abspath(ast.literal_eval(pm.group(1)))
            src_lit = os.path.abspath(ast.literal_eval(pm.group(2)))
            paths_ok = (libs_lit == os.path.abspath(A.LF_LIBS)
                        and src_lit == os.path.abspath(A.LF_SRC))
        except Exception:
            paths_ok = False
    dill_ok = ("import dill" in runner
               and runner.index("import dill") < runner.index("from llamafactory.cli import main"))
    compile_ok = True
    try:
        compile(runner, "<runner>", "exec")
    except SyntaxError:
        compile_ok = False
    T.check(len(argv_child) == 3 and argv_child[1] == "-c"
            and argv_child[0].lower().endswith("python.exe")
            and "sys.path.insert" in runner and "llamafactory.cli" in runner
            and "'train'" in runner and lit_ok and compile_ok
            and paths_ok and dill_ok, 11, "runner_argv_literal",
            "python=%s compile=%s yaml_literal=%s paths=%s dill_seed=%s len=%d"
            % (os.path.basename(argv_child[0]), compile_ok, lit_ok, paths_ok,
               dill_ok, len(runner)))

    # ---------------- 12. parse_line: real line shapes -------------------- #
    cases = [
        ("{'loss': 1.2345, 'grad_norm': 0.5, 'learning_rate': 0.0002, 'epoch': 0.05}",
         "loss", {"loss": 1.2345, "epoch": 0.05, "learning_rate": 0.0002}, "transformers on_log"),
        ("  5%|?? | 1/20 [00:01<00:19,  1.00it/s]", "progress",
         {"percent": 5, "step": 1, "total": 20}, "tqdm"),
        ("{'train_runtime': 12.34, 'train_samples_per_second': 4.5, "
         "'train_steps_per_second': 0.16, 'train_loss': 1.2, 'epoch': 0.1}",
         "summary", {"train_runtime": 12.34, "train_loss": 1.2}, "final metrics"),
        ("***** Running training *****", "info", {"key": "phase"}, "banner"),
        ("  Num examples = 200", "info", {"key": "num_examples", "value": 200}, "counter"),
        ("  Total optimization steps = 2", "info", {"key": "total_steps", "value": 2}, "counter"),
        ("Saving model checkpoint to .\\out\\checkpoint-2", "checkpoint",
         {"path": ".\\out\\checkpoint-2"}, "checkpoint"),
        ("Resuming training from .\\out\\checkpoint-1", "resume",
         {"path": ".\\out\\checkpoint-1"}, "resume"),
        ("some random banner text", "raw", {}, "fallback"),
        ("   ", "empty", {}, "blank"),
        ("{'eval_loss': 0.9, 'epoch': 0.2}", "dict", {}, "non-loss dict must not be loss"),
    ]
    fails = []
    for line, want, fields, label in cases:
        ev = A.parse_line(line)
        if ev.get("type") != want or any(ev.get(k) != v for k, v in fields.items()):
            fails.append("%s -> %s %s" % (label, ev.get("type"),
                                          {k: ev.get(k) for k in fields}))
    T.check(not fails, 12, "parse_line_shapes", "cases=%d fails=%s" % (len(cases), fails[:3]))

    # ---------------- 13. run() end-to-end with stub child ---------------- #
    stub_log = os.path.join(tmp, "stub.log")
    res = A.run({"params": dict(BASE, max_steps=2, learning_rate=2e-4),
                 "workdir": tmp},
                stub_log, runner_argv=[sys.executable, "-c", STUB_OK])
    log_lines = open(stub_log, "r", encoding="utf-8").read().splitlines() if os.path.isfile(stub_log) else []
    ok = (res["state"] == "succeeded" and res["returncode"] == 0
          and res["metrics"]["loss"] == 1.0 and res["metrics"]["step"] == 2
          and res["metrics"]["total_steps"] == 2
          and res["metrics"]["train_loss"] == 1.25
          and len(res["metrics"]["checkpoints"]) == 1
          and len(log_lines) == 10 and len(res["events"]) >= 8)
    T.check(ok, 13, "run_stub_success",
            "state=%s rc=%s loss=%r step=%r total=%r summary_loss=%r ckpt=%d log=%d ev=%d"
            % (res["state"], res["returncode"], res["metrics"]["loss"],
               res["metrics"]["step"], res["metrics"]["total_steps"],
               res["metrics"]["train_loss"], len(res["metrics"]["checkpoints"]),
               len(log_lines), len(res["events"])))

    # ---------------- 14. run() cancellation ------------------------------- #
    stop = threading.Event()
    cancel_log = os.path.join(tmp, "cancel.log")
    timer = threading.Timer(0.8, stop.set)
    timer.start()
    t_cancel = time.perf_counter()
    res2 = A.run({"params": dict(BASE), "workdir": tmp}, cancel_log,
                 stop_event=stop, runner_argv=[sys.executable, "-c", STUB_CANCEL])
    dur = time.perf_counter() - t_cancel
    timer.cancel()
    T.check(res2["state"] == "cancelled" and dur < 20.0, 14, "run_cancel_stops_child",
            "state=%s rc=%s duration=%.2fs (stub would run 50s)" % (res2["state"], res2["returncode"], dur))

    # ---------------- 15. missing params: no yaml, no spawn ---------------- #
    yaml_file = os.path.join(tmp, "should_not_exist.yaml")
    res3 = A.run({"params": {"dataset": "x"}, "yaml_path": yaml_file, "workdir": tmp},
                 os.path.join(tmp, "missing.log"))
    T.check(res3["state"] == "failed" and "MISSING_REQUIRED" in (res3["error"] or "")
            and not os.path.exists(yaml_file), 15, "missing_params_fail_fast",
            "state=%s error=%s yaml_written=%s"
            % (res3["state"], res3["error"], os.path.exists(yaml_file)))

    # ---------------- 16. spawn failure is reported ------------------------ #
    res4 = A.run({"params": dict(BASE), "workdir": tmp}, os.path.join(tmp, "spawn.log"),
                 runner_argv=[r"Z:\no_such\python.exe", "-c", "pass"])
    T.check(res4["state"] == "failed" and "PYTHON_NOT_FOUND" in (res4["error"] or ""),
            16, "spawn_failure_reported", "state=%s error=%s" % (res4["state"], res4["error"]))

    # ---------------- 17. emitted keys are upstream keys ------------------- #
    allowed = UPSTREAM_DECLARED | UPSTREAM_INHERITED
    extra = sorted(set(A.build_yaml(dict(BASE, max_samples=1, dataset_dir="d",
                                         eval_dataset="e", val_size=0.1,
                                         resume_from_checkpoint=None,
                                         dataloader_num_workers=0))) - allowed)
    T.check(not extra, 17, "yaml_keys_exist_upstream",
            "declared=%d inherited=%d unknown=%s"
            % (len(UPSTREAM_DECLARED), len(UPSTREAM_INHERITED), extra))

    # ---------------- 18. dependency probe --------------------------------- #
    if args.skip_probe:
        print("INFO 18 dependency_probe                 :: SKIPPED (--skip-probe)")
    else:
        probe = A.probe_dependencies()
        statuses = {k: v["status"] for k, v in probe["modules"].items()}
        valid = {"OK", "MISSING", "BROKEN_NAMESPACE"}
        all_definitive = set(statuses) == set(A.PROBE_MODULES) and \
            all(s in valid for s in statuses.values())
        bad = {k: s for k, s in statuses.items() if s != "OK"}
        # a BROKEN_NAMESPACE must carry its shadow path, otherwise it is a bad probe
        ns = probe["modules"].get("datasets", {})
        ns_ok = ns.get("status") != "BROKEN_NAMESPACE" or bool(ns.get("detail"))
        T.check(all_definitive and ns_ok, 18, "dependency_probe_definitive",
                "not_ok=%s ready_for_sft=%s" % (bad, probe["ready_for_sft"]))
        fails_import = [k for k, v in probe["lf_train_import"].items() if not v["ok"]]
        T.check(probe["ready_for_sft"] == (len(fails_import) == 0
                                           and not probe.get("shadowed_from_libs")), 19,
                "probe_ready_flag_consistent",
                "ready=%s failing_imports=%d shadowed=%s first=%s"
                % (probe["ready_for_sft"], len(fails_import),
                   probe.get("shadowed_from_libs"),
                   (fails_import[0] if fails_import else "-")))
        rt = probe.get("runtime", {})
        T.check(not probe.get("shadowed_from_libs")
                and rt.get("torch_from_libs") is False
                and rt.get("transformers_from_libs") is False, 23, "no_shadowing_from_libs",
                "torch=%s(%s) from_libs=%s | transformers=%s from_libs=%s | cuda=%s"
                % (rt.get("torch_version"), (rt.get("torch_file") or "")[-40:],
                   rt.get("torch_from_libs"), rt.get("transformers_version"),
                   rt.get("transformers_from_libs"), rt.get("cuda_available")))

    # ---------------- 24. dataset-preprocessing bar must not win step ------ #
    res5 = A.run({"params": dict(BASE), "workdir": tmp},
                 os.path.join(tmp, "prep.log"),
                 runner_argv=[sys.executable, "-c", STUB_PREP])
    bars = res5["metrics"].get("bars") or {}
    T.check(res5["metrics"]["step"] == 2 and res5["metrics"]["total_steps"] == 2
            and bars.get("200") == 200 and res5["metrics"]["loss"] == 1.5, 24,
            "preprocessing_bar_not_step",
            "step=%r total=%r bars=%r loss=%r"
            % (res5["metrics"]["step"], res5["metrics"]["total_steps"], bars,
               res5["metrics"]["loss"]))

    # ---------------- 20. no third_party writes ---------------------------- #
    lf_files_before = _count_files(A.LF_ROOT)
    A.build_yaml(dict(BASE))
    A.write_yaml(A.build_yaml(dict(BASE)), os.path.join(tmp, "x.yaml"))
    T.check(_count_files(A.LF_ROOT) == lf_files_before, 20, "third_party_untouched",
            "files under LlamaFactory=%d (unchanged)" % lf_files_before)

    # ---------------- 21. dolly -> alpaca preparation ---------------------- #
    syn = os.path.join(tmp, "dolly_mini.jsonl")
    with open(syn, "w", encoding="utf-8") as f:
        f.write(json.dumps({"instruction": "Q1", "context": "ctx-1",
                            "response": "A1", "category": "closed_qa"}) + "\n")
        f.write(json.dumps({"instruction": "Q2", "context": "",
                            "response": "A2", "category": "open_qa"}) + "\n")
        f.write(json.dumps({"instruction": "Q3", "context": "",
                            "response": "  ", "category": "open_qa"}) + "\n")
    prep_dir = os.path.join(tmp, "data dir 中文")
    prep = A.prepare_alpaca_data(syn, prep_dir, limit=10, dataset_name="mini_ds")
    with open(prep["data_path"], "r", encoding="utf-8") as f:
        recs = json.load(f)
    with open(prep["dataset_info"], "r", encoding="utf-8") as f:
        info = json.load(f)
    keys_ok = all(sorted(r) == ["input", "instruction", "output"] for r in recs)
    T.check(prep["rows"] == 2 and prep["skipped"] == 1 and keys_ok
            and info.get("mini_ds", {}).get("file_name") == os.path.basename(prep["data_path"])
            and prep["has_input"] == 1, 21, "prepare_alpaca_data",
            "rows=%d skipped=%d input_rows=%d keys_ok=%s info=%s"
            % (prep["rows"], prep["skipped"], prep["has_input"], keys_ok,
               info.get("mini_ds")))

    # ---------------- 22. dataset_info merge does not clobber ------------- #
    prep2 = A.prepare_alpaca_data(syn, prep_dir, limit=1, dataset_name="second_ds")
    with open(prep2["dataset_info"], "r", encoding="utf-8") as f:
        info2 = json.load(f)
    T.check(set(info2) == {"mini_ds", "second_ds"}, 22, "dataset_info_merge",
            "names=%s" % sorted(info2))

    # ---------------- 23. native alpaca jsonl (instruction/input/output) --- #
    alp = os.path.join(tmp, "alpaca_mini.jsonl")
    with open(alp, "w", encoding="utf-8") as f:
        f.write(json.dumps({"instruction": "Q1", "input": "customer is angry",
                            "output": "A1"}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"instruction": "Q2", "input": "", "output": ""},
                           ensure_ascii=False) + "\n")
    prep3 = A.prepare_alpaca_data(alp, prep_dir, limit=10, dataset_name="alpaca_ds")
    with open(prep3["data_path"], "r", encoding="utf-8") as f:
        recs3 = json.load(f)
    T.check(prep3["rows"] == 1 and prep3["skipped"] == 1
            and recs3[0]["input"] == "customer is angry"
            and recs3[0]["output"] == "A1", 23, "alpaca_native_keys",
            "rows=%d skipped=%d rec=%s"
            % (prep3["rows"], prep3["skipped"],
               recs3[0] if recs3 else None))

    out = {"pass": T.p, "fail": T.f, "checks": T.rows}
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print("json_out: %s" % os.path.abspath(args.json_out))
    return T.summary()


def _count_files(root):
    if not os.path.isdir(root):
        return -1
    n = 0
    for _dirpath, _dirnames, filenames in os.walk(root):
        n += len(filenames)
    return n


if __name__ == "__main__":
    sys.exit(main())
