#!/usr/bin/env python3
r"""Black-box selftest for adapters/laya_adapter.py (no GPU, no torch).

Run from the project root:
    python adapters\selftest_laya_adapter.py
    python adapters\selftest_laya_adapter.py --keep

It uses adapters/_tmp/laya_stub.py as a fake ``laya-train`` CLI, so it tests
the real argv/parser/runner paths without installing Laya or loading a model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True
import laya_adapter as adapter  # noqa: E402

STUB = HERE / "_tmp" / "laya_stub.py"
PYTHON = sys.executable
TRAIN_CLI = PROJECT_ROOT / "third_party" / "laya" / "laya" / "train_cli.py"


class Checker:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures = []

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        if condition:
            self.passed += 1
            print("PASS %02d - %s%s" % (self.passed + self.failed, name, (" :: " + detail) if detail else ""))
        else:
            self.failed += 1
            print("FAIL %02d - %s%s" % (self.passed + self.failed, name, (" :: " + detail) if detail else ""))
            self.failures.append(name)
        return bool(condition)


def parse_flags(argv):
    """Split a CLI argv into (flags set, value map, unknown flags)."""
    flags = set()
    values = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token.startswith("--"):
            flags.add(token)
            if index + 1 < len(argv) and not argv[index + 1].startswith("--"):
                values[token] = argv[index + 1]
                index += 2
                continue
        index += 1
    return flags, values


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Laya adapter selftest")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)

    if not STUB.is_file():
        print("FAIL - stub missing: %s" % STUB)
        return 2

    temp_root = Path(tempfile.mkdtemp(prefix="modelsmith_laya_adapter_selftest_"))
    work = temp_root / "中文 适配器"
    work.mkdir(parents=True, exist_ok=True)
    checker = Checker()
    try:
        run_all(work, checker)
    except Exception:
        checker.check("selftest harness completed without crashing", False, traceback.format_exc())
    finally:
        if checker.failed:
            print("\nFAILED CHECKS:")
            for name in checker.failures:
                print("  - " + name)
            print("\nRESULT: PASS %d / FAIL %d" % (checker.passed, checker.failed))
            print("data dir kept for debugging: %s" % work)
            return 1
        if args.keep:
            print("\nRESULT: PASS %d / FAIL %d" % (checker.passed, checker.failed))
            print("data dir kept: %s" % work)
        else:
            print("\nRESULT: PASS %d / FAIL %d" % (checker.passed, checker.failed))
            shutil.rmtree(temp_root, ignore_errors=True)
    return 0


def run_all(work: Path, c: Checker) -> None:
    data_path = work / "train.jsonl"
    eval_path = work / "eval.jsonl"
    out_dir = work / "run-out"
    data_path.write_text(
        '{"text": "hello", "label": "a"}\n', encoding="utf-8"
    )
    eval_path.write_text(
        '{"text": "held out", "label": "a"}\n', encoding="utf-8"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    # -- upstream arithmetic ------------------------------------------------
    c.check(
        "estimate_updates(5600, 4, 8, 8) == 352 (upstream formula)",
        adapter.estimate_updates(5600, 4, 8, 8) == 352,
        "value=%d" % adapter.estimate_updates(5600, 4, 8, 8),
    )
    c.check(
        "estimate_updates rejects non-positive values",
        _raises(lambda: adapter.estimate_updates(0, 4, 8, 8)),
        "items=0",
    )

    params = {
        "data": str(data_path),
        "out": str(out_dir),
        "base": "convaiinnovations/laya",
        "loss": "rlcd",
        "epochs": 4,
        "micro_batch": 8,
        "grad_accum": 8,
        "encoder_lr": 2.5e-5,
        "head_lr": 1e-4,
        "max_len": 512,
        "head_max_len": 192,
        "shuffle_options": True,
        "label_smoothing": 0.0,
        "target_error": 0.10,
        "min_abstain_n": 10,
        "eval_data": str(eval_path),
        "items": 5600,          # UI-only; must NOT become a CLI flag
        "calib_frac": 0.1,      # Python-only; must NOT become a CLI flag
        "calib_max": 400,       # Python-only; must NOT become a CLI flag
    }
    command = [PYTHON, str(STUB)]
    argv = adapter.build_argv(params, train_command=command)
    flags, values = parse_flags(argv)
    allowed = {
        "--data", "--out", "--base", "--loss", "--epochs", "--micro-batch",
        "--grad-accum", "--encoder-lr", "--head-lr", "--max-len",
        "--head-max-len", "--shuffle-options", "--label-smoothing",
        "--target-error", "--min-abstain-n", "--eval-data",
    }
    unexpected = sorted(flag for flag in flags if flag not in allowed)
    c.check(
        "build_argv emits only real laya/train_cli.py flags",
        not unexpected,
        "unexpected=%s flags=%s" % (unexpected, sorted(flags)),
    )
    train_cli_text = (
        TRAIN_CLI.read_text(encoding="utf-8") if TRAIN_CLI.is_file() else ""
    )
    missing_source = sorted(
        flag for flag in flags if flag not in train_cli_text
    )
    c.check(
        "every emitted flag is present in laya/train_cli.py (grep check)",
        bool(train_cli_text) and not missing_source,
        "flags=%d missing=%s source=%s"
        % (len(flags), missing_source, TRAIN_CLI),
    )
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "third_party" / "laya"))
        from laya.train_cli import build_parser as upstream_parser  # noqa: E402

        parsed = upstream_parser().parse_args(argv[2:])
        upstream_ok = (
            parsed.data == str(data_path)
            and parsed.output_dir == str(out_dir)
            and parsed.model_dir == "convaiinnovations/laya"
            and parsed.loss == "rlcd"
            and parsed.epochs == 4
            and parsed.micro_batch == 8
            and parsed.grad_accum == 8
            and parsed.max_len == 512
            and parsed.head_max_len == 192
            and parsed.shuffle_options is True
            and parsed.eval_data == str(eval_path)
        )
        upstream_detail = (
            "data=%s out=%s loss=%s epochs=%s micro=%s grad=%s shuffle=%s eval=%s"
            % (
                parsed.data,
                parsed.output_dir,
                parsed.loss,
                parsed.epochs,
                parsed.micro_batch,
                parsed.grad_accum,
                parsed.shuffle_options,
                parsed.eval_data,
            )
        )
    except Exception as exc:
        upstream_ok = False
        upstream_detail = "%s: %s" % (type(exc).__name__, exc)
    c.check(
        "upstream laya.train_cli argparse accepts the generated argv",
        upstream_ok,
        upstream_detail,
    )
    c.check(
        "build_argv maps data/out/base/loss",
        argv[:2] == command
        and values.get("--data") == str(data_path)
        and values.get("--out") == str(out_dir)
        and values.get("--base") == "convaiinnovations/laya"
        and values.get("--loss") == "rlcd",
        "argv=%s" % json.dumps(argv, ensure_ascii=False),
    )
    c.check(
        "build_argv maps epochs/micro/grad/lr/max-len/head-max-len",
        values.get("--epochs") == "4"
        and values.get("--micro-batch") == "8"
        and values.get("--grad-accum") == "8"
        and abs(float(values.get("--encoder-lr", "nan")) - 2.5e-5) < 1e-12
        and abs(float(values.get("--head-lr", "nan")) - 1e-4) < 1e-12
        and values.get("--max-len") == "512"
        and values.get("--head-max-len") == "192",
        "values=%s" % json.dumps(values, ensure_ascii=False),
    )
    c.check(
        "build_argv maps shuffle/label-smoothing/target-error/min-abstain/eval",
        "--shuffle-options" in flags
        and values.get("--label-smoothing") == "0.0"
        and values.get("--target-error") == "0.1"
        and values.get("--min-abstain-n") == "10"
        and values.get("--eval-data") == str(eval_path),
        "values=%s" % json.dumps(values, ensure_ascii=False),
    )
    c.check(
        "build_argv ignores UI-only items/calib_frac/calib_max",
        "--items" not in flags
        and "--calib-frac" not in flags
        and "--calib-max" not in flags,
        "flags=%s" % sorted(flags),
    )
    c.check(
        "build_argv requires --data and --out",
        _raises(lambda: adapter.build_argv({"out": str(out_dir)}))
        and _raises(lambda: adapter.build_argv({"data": str(data_path)})),
        "missing required fields",
    )
    c.check(
        "build_argv rejects loss outside {rlcd,soft-ce}",
        _raises(lambda: adapter.build_argv(dict(params, loss="bogus"))),
        "loss=bogus",
    )

    # -- line parser --------------------------------------------------------
    budget_line = (
        "optimizer budget: 5600 train item(s), effective batch up to 64 "
        "(micro_batch 8 x grad_accum 8), 352 optimizer updates over 4 epoch(s)"
    )
    budget = adapter.parse_line(budget_line) or {}
    c.check(
        "parse budget: updates_total=352, epochs_total=4, train_items=5600",
        budget.get("type") == "budget"
        and budget.get("updates_total") == 352
        and budget.get("epochs_total") == 4
        and budget.get("train_items") == 5600,
        "event=%s" % json.dumps(budget, ensure_ascii=False),
    )
    c.check(
        "parse budget never claims a micro_step",
        "micro_step" not in budget,
        "keys=%s" % sorted(budget),
    )

    loss = adapter.parse_line("epoch 1/4 step 100 loss 1.2345") or {}
    c.check(
        "parse loss: micro_step=100 (NOT updates_total)",
        loss.get("type") == "loss"
        and loss.get("micro_step") == 100
        and loss.get("loss") == 1.2345
        and loss.get("step_kind") == "micro_batch"
        and "updates_total" not in loss,
        "event=%s" % json.dumps(loss, ensure_ascii=False),
    )
    mean = adapter.parse_line("epoch 2/4 mean loss 0.4321") or {}
    c.check(
        "parse epoch mean loss",
        mean.get("type") == "epoch_mean"
        and mean.get("epoch") == 2
        and mean.get("epoch_mean_loss") == 0.4321,
        "event=%s" % json.dumps(mean, ensure_ascii=False),
    )
    checkpoint = adapter.parse_line("checkpoint_latest: C:\\tmp\\out\\checkpoint_latest") or {}
    c.check(
        "parse checkpoint_latest line",
        checkpoint.get("type") == "checkpoint"
        and checkpoint.get("checkpoint_latest") is True
        and checkpoint.get("path") == "C:\\tmp\\out\\checkpoint_latest",
        "event=%s" % json.dumps(checkpoint, ensure_ascii=False),
    )
    evaluation = adapter.parse_line(
        "=== Evaluation (before vs after fine-tuning on 500 held-out items from eval.jsonl) ==="
    ) or {}
    c.check(
        "parse Evaluation (before vs after) line",
        evaluation.get("type") == "evaluation"
        and evaluation.get("mode") == "held_out"
        and evaluation.get("eval_items") == 500,
        "event=%s" % json.dumps(evaluation, ensure_ascii=False),
    )
    warning = adapter.parse_line(
        "laya.train: choice calibration: not fitted: 2 calibration items, "
        "fewer than 10, so the temperature stays 1.0"
    ) or {}
    c.check(
        "parse calibration warning",
        warning.get("type") == "calibration_warning"
        and warning.get("qtype") == "choice"
        and "not fitted" in warning.get("message", ""),
        "event=%s" % json.dumps(warning, ensure_ascii=False),
    )
    dataset = adapter.parse_line(
        "train items 5600, calibration items 560, eval items 500 (eval.jsonl), "
        "skipped {}, device cpu"
    ) or {}
    c.check(
        "parse dataset counts",
        dataset.get("type") == "dataset"
        and dataset.get("train_items") == 5600
        and dataset.get("calibration_items") == 560
        and dataset.get("eval_items") == 500,
        "event=%s" % json.dumps(dataset, ensure_ascii=False),
    )
    c.check(
        "unrecognized line -> None",
        adapter.parse_line("just some progress noise") is None,
        "line=noise",
    )

    # -- full lifecycle through the stub -----------------------------------
    log_path = work / "logs" / "full.log"
    metrics_path = work / "metrics" / "full.json"
    job = {"id": "selftest-full", "params": params}
    result = adapter.run(
        job,
        log_path,
        train_command=command + ["--stub-delay", "0.01"],
        metrics_path=metrics_path,
    )
    log_text = log_path.read_text(encoding="utf-8")
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    c.check(
        "stub full run succeeded (rc=0)",
        result.get("state") == "succeeded"
        and result.get("returncode") == 0
        and result.get("cancelled") is False,
        "state=%s rc=%s" % (result.get("state"), result.get("returncode")),
    )
    c.check(
        "log persists budget/loss/mean/checkpoint/evaluation/warning",
        all(
            marker in log_text
            for marker in (
                "optimizer budget:",
                "step 100 loss",
                "mean loss",
                "checkpoint_latest",
                "Evaluation (before vs after",
                "not fitted",
            )
        )
        and len(log_text.encode("utf-8")) > 500,
        "log_bytes=%d" % len(log_text.encode("utf-8")),
    )
    c.check(
        "metrics keep updates_total=352 and micro_step=100 separately",
        metrics.get("updates_total") == 352
        and metrics.get("micro_step") == 100
        and metrics.get("loss") is not None
        and metrics.get("updates_total") != metrics.get("micro_step"),
        "updates_total=%s micro_step=%s loss=%s"
        % (metrics.get("updates_total"), metrics.get("micro_step"), metrics.get("loss")),
    )
    c.check(
        "metrics final state/epoch/evaluation",
        metrics.get("state") == "succeeded"
        and metrics.get("epoch") == 4
        and metrics.get("evaluation") == "held_out"
        and bool(metrics.get("warnings")),
        "state=%s epoch=%s evaluation=%s warnings=%d"
        % (
            metrics.get("state"),
            metrics.get("epoch"),
            metrics.get("evaluation"),
            len(metrics.get("warnings") or []),
        ),
    )
    c.check(
        "runner event stream contains all expected event types",
        {"budget", "loss", "epoch_mean", "checkpoint", "evaluation", "calibration_warning"}
        <= {event.get("type") for event in (result.get("events") or [])},
        "types=%s" % sorted({event.get("type") for event in (result.get("events") or [])}),
    )
    c.check(
        "log file is UTF-8 and under the temp work dir",
        Path(result.get("log_path", "")).resolve().is_relative_to(work.resolve())
        and hashlib.sha256(log_text.encode("utf-8")).hexdigest() == hashlib.sha256(
            log_path.read_bytes()
        ).hexdigest(),
        "log_path=%s" % result.get("log_path"),
    )

    # -- cancellation via stop file ----------------------------------------
    cancel_log = work / "logs" / "cancel.log"
    cancel_metrics = work / "metrics" / "cancel.json"
    stop_file = work / "cancel.stop"
    box = {}

    def _worker():
        try:
            box["result"] = adapter.run(
                {"id": "selftest-cancel", "params": params},
                cancel_log,
                train_command=command + ["--stub-delay", "0.25"],
                stop_file=stop_file,
                metrics_path=cancel_metrics,
            )
        except Exception:
            box["error"] = traceback.format_exc()

    thread = threading.Thread(target=_worker, name="selftest-cancel", daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        if cancel_log.is_file():
            try:
                if "step 100" in cancel_log.read_text(encoding="utf-8"):
                    break
            except OSError:
                pass
        time.sleep(0.05)
    stop_file.write_text("cancel\n", encoding="utf-8")
    thread.join(timeout=20)
    c.check(
        "cancel thread finished and did not raise",
        not thread.is_alive() and "result" in box and "error" not in box,
        "alive=%s error=%s" % (thread.is_alive(), box.get("error")),
    )
    cancel_result = box.get("result") or {}
    cancel_text = cancel_log.read_text(encoding="utf-8") if cancel_log.is_file() else ""
    c.check(
        "stop file cancels a running Laya job",
        cancel_result.get("state") == "cancelled"
        and cancel_result.get("cancelled") is True
        and cancel_result.get("returncode") != 0
        and "cancel" in cancel_text.lower(),
        "state=%s rc=%s log_has_cancel=%s"
        % (
            cancel_result.get("state"),
            cancel_result.get("returncode"),
            "cancel" in cancel_text.lower(),
        ),
    )
    cancel_json = (
        json.loads(cancel_metrics.read_text(encoding="utf-8"))
        if cancel_metrics.is_file()
        else {}
    )
    c.check(
        "cancel metrics are persisted as cancelled",
        cancel_json.get("state") == "cancelled"
        and cancel_json.get("cancelled") is True
        and stop_file.is_file(),
        "metrics_state=%s cancelled=%s stop_file=%s"
        % (
            cancel_json.get("state"),
            cancel_json.get("cancelled"),
            stop_file.is_file(),
        ),
    )


def _raises(callable_) -> bool:
    try:
        callable_()
    except Exception:
        return True
    return False


if __name__ == "__main__":
    sys.exit(main())
