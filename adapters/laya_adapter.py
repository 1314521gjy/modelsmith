# =============================================================================
# ModelSmith · L2 adapter 分块图谱  |  详见 adapters/README.md
# 角色   : kind="laya"（决策模型）的子进程适配器，由 core 的 _run_laya 调用
# 上游   : third_party\laya（laya-train CLI，contract 以 train_cli.py 为准）
# 主链   : job params -> build_argv（只发上游真实存在的 flag）->
#          [laya-train, *argv] 子进程 -> stdout 逐行 parse_line ->
#          metrics.json（updates_total / micro_step / epoch / epoch_mean_loss）
# 参数三分: ① core 传入 job.params 原样 ② 本文件自算：estimate_updates 预算、
#          argv 默认（[laya-train]）、非法值硬抛 ③ 上游默认：没传的 flag 不写，
#          由 train_cli.py 的默认值决定
# 无继训  : 上游没有 resume；取消 = 取消，只能新开 job（docs\TRAINING-FLOWS.md §1)
# 产物    : 上游落 <out>\model.safetensors / encoder\ / tokenizer\ /
#          rl_agent_config.json / train_report.json / checkpoint_latest\
#          （本适配器不扫产物，core 的 /api/jobs/<id>/artifacts 通用扫描可见）
# 纪律    : 代码 token 不许改；本文件只加注释（注释不进 AST）
# =============================================================================
r"""Laya training adapter for ModelSmith (P3 preparation).

Stdlib only. This module knows the real ``laya-train`` CLI contract (verified
against ``third_party/laya/laya/train_cli.py``) and the real stdout formats
(``third_party/laya/laya/train.py``).

It deliberately does **not** implement resume: upstream Laya has no resume
path (see docs/TRAINING-FLOWS.md §1.6). A cancelled run is a cancelled run.

Public API:
    estimate_updates(items, epochs, micro_batch, grad_accum) -> int
    build_argv(params, train_command=None) -> list[str]
    parse_line(line) -> dict | None
    run(job, log_path, ...) -> dict

The two counters that must never be mixed:
    * loss lines carry ``micro_step``  (micro-batch counter, 8x by default)
    * the budget line carries ``updates_total`` (optimizer steps)
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

LAYA_TRAIN_DEFAULT = "laya-train"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

BUDGET_RE = re.compile(
    r"optimizer budget:\s*(?P<items>[\d,]+)\s+train item\(s\).*?"
    r"(?P<updates>\d+)\s+optimizer updates over\s+(?P<epochs>\d+)\s+epoch",
    re.IGNORECASE,
)
EFFECTIVE_BATCH_RE = re.compile(
    r"effective batch up to\s+(?P<effective>\d+)\s*"
    r"\(micro_batch\s+(?P<micro>\d+)\s+x\s+grad_accum\s+(?P<grad>\d+)\)",
    re.IGNORECASE,
)
DATASET_RE = re.compile(
    r"train items\s+(?P<train>\d+)\s*,\s*calibration items\s+(?P<calib>\d+)"
    r"\s*,\s*eval items\s+(?P<eval>\d+)",
    re.IGNORECASE,
)
LOSS_RE = re.compile(
    r"\bepoch\s+(?P<epoch>\d+)\s*/\s*(?P<epochs>\d+)\s+step\s+"
    r"(?P<step>\d+)\s+loss\s+(?P<loss>[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
    r"(?:[eE][-+]?\d+)?)",
    re.IGNORECASE,
)
MEAN_RE = re.compile(
    r"\bepoch\s+(?P<epoch>\d+)\s*/\s*(?P<epochs>\d+)\s+mean loss\s+"
    r"(?P<loss>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)",
    re.IGNORECASE,
)
EVAL_RE = re.compile(r"Evaluation\s*\(\s*before\s+vs\s+after", re.IGNORECASE)
EVAL_ITEMS_RE = re.compile(
    r"on\s+(?P<items>\d+)\s+(?:held-out\s+)?items", re.IGNORECASE
)
CALIB_EVIDENCE_RE = re.compile(r"Calibration Evidence", re.IGNORECASE)
COMPLETE_RE = re.compile(r"Training complete! Model saved to\s+(?P<out>.+)$", re.IGNORECASE)

CALIBRATION_MARKERS = (
    "not fitted",
    "fewer than",
    "temperature stays 1.0",
    "fitted on only",
    "calibration cross-entropy",
    "failed to fit abstention",
    "calibration logits have collapsed",
)


# ---------------------------------------------------------------------------
# arithmetic (mirrors laya/train.py:607-619)
# ---------------------------------------------------------------------------


def estimate_updates(items: int, epochs: int, micro_batch: int, grad_accum: int) -> int:
    """Optimizer updates = epochs * ceil(ceil(items / micro_batch) / grad_accum)."""
    values = {
        "items": items,
        "epochs": epochs,
        "micro_batch": micro_batch,
        "grad_accum": grad_accum,
    }
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("%s must be a positive integer, got %r" % (name, value))
    steps_per_epoch = math.ceil(items / micro_batch)
    updates_per_epoch = math.ceil(steps_per_epoch / grad_accum)
    return updates_per_epoch * epochs


# ---------------------------------------------------------------------------
# argv
# ---------------------------------------------------------------------------


def _require_str(params: dict, key: str) -> str:
    value = params.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError("params.%s is required" % key)
    return str(value)


def _as_positive_int(value, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError("%s must be an integer" % name)
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be an integer" % name)
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("%s must be an integer" % name)
    if number < 1:
        raise ValueError("%s must be >= 1" % name)
    return number


def _as_float(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s must be a number" % name)
    return float(value)


def _append_value(argv, flag, value):
    argv.append(flag)
    argv.append(str(value))


def build_argv(params: dict, train_command=None) -> list:
    """Translate ModelSmith job params into a real ``laya-train`` argv.

    Only flags that exist in ``laya/train_cli.py`` are emitted. Unknown keys
    (for example UI-only ``calib_frac`` / ``calib_max`` / ``items``) are
    ignored, not guessed into CLI flags.

    ``train_command`` is only for test/integration overrides: it lets the
    caller run the same argv through a stub or through an installed
    ``laya-train.exe``. It defaults to ``["laya-train"]``.
    """
    if not isinstance(params, dict):
        raise ValueError("params must be a dict")
    if train_command is None:
        argv = [LAYA_TRAIN_DEFAULT]
    elif isinstance(train_command, str):
        argv = [train_command]
    else:
        argv = [str(part) for part in train_command]
    if not argv:
        raise ValueError("train_command must not be empty")

    data = _require_str(params, "data")
    out = _require_str(params, "out")
    argv.extend(["--data", data, "--out", out])

    if params.get("base") is not None:
        _append_value(argv, "--base", str(params["base"]))

    if params.get("loss") is not None:
        loss = str(params["loss"])
        if loss not in ("rlcd", "soft-ce"):
            raise ValueError("params.loss must be 'rlcd' or 'soft-ce', got %r" % loss)
        _append_value(argv, "--loss", loss)

    if params.get("epochs") is not None:
        _append_value(argv, "--epochs", _as_positive_int(params["epochs"], "epochs"))
    if params.get("micro_batch") is not None:
        _append_value(
            argv, "--micro-batch", _as_positive_int(params["micro_batch"], "micro_batch")
        )
    if params.get("grad_accum") is not None:
        _append_value(
            argv, "--grad-accum", _as_positive_int(params["grad_accum"], "grad_accum")
        )
    if params.get("encoder_lr") is not None:
        _append_value(argv, "--encoder-lr", _as_float(params["encoder_lr"], "encoder_lr"))
    if params.get("head_lr") is not None:
        _append_value(argv, "--head-lr", _as_float(params["head_lr"], "head_lr"))
    if params.get("max_len") is not None:
        _append_value(argv, "--max-len", _as_positive_int(params["max_len"], "max_len"))
    if params.get("head_max_len") is not None:
        _append_value(
            argv,
            "--head-max-len",
            _as_positive_int(params["head_max_len"], "head_max_len"),
        )
    if params.get("shuffle_options") is not None:
        if not isinstance(params["shuffle_options"], bool):
            raise ValueError("params.shuffle_options must be a boolean")
        if params["shuffle_options"]:
            argv.append("--shuffle-options")
    if params.get("label_smoothing") is not None:
        value = _as_float(params["label_smoothing"], "label_smoothing")
        if not 0.0 <= value < 1.0:
            raise ValueError("params.label_smoothing must be in [0, 1)")
        _append_value(argv, "--label-smoothing", value)
    if params.get("target_error") is not None:
        value = _as_float(params["target_error"], "target_error")
        if not 0.0 <= value <= 1.0:
            raise ValueError("params.target_error must be in [0, 1]")
        _append_value(argv, "--target-error", value)
    if params.get("min_abstain_n") is not None:
        _append_value(
            argv,
            "--min-abstain-n",
            _as_positive_int(params["min_abstain_n"], "min_abstain_n"),
        )
    if params.get("eval_data") is not None:
        _append_value(argv, "--eval-data", str(params["eval_data"]))
    return argv


# ---------------------------------------------------------------------------
# line parser
# ---------------------------------------------------------------------------


def _clean_line(line) -> str:
    text = str(line or "").replace("\r", "")
    text = ANSI_RE.sub("", text)
    return text.rstrip("\n").strip()


def parse_line(line) -> dict | None:
    """Parse one Laya stdout/stderr line into an event dict, or return None.

    Counter discipline:
      * ``epoch e/E step n loss x`` -> ``micro_step = n`` and NO updates field
      * ``optimizer budget: ... U optimizer updates ...`` -> ``updates_total = U``
    """
    text = _clean_line(line)
    if not text:
        return None

    match = BUDGET_RE.search(text)
    if match:
        event = {
            "type": "budget",
            "train_items": int(match.group("items").replace(",", "")),
            "updates_total": int(match.group("updates")),
            "epochs_total": int(match.group("epochs")),
        }
        batch = EFFECTIVE_BATCH_RE.search(text)
        if batch:
            event.update(
                {
                    "effective_batch": int(batch.group("effective")),
                    "micro_batch": int(batch.group("micro")),
                    "grad_accum": int(batch.group("grad")),
                }
            )
        return event

    match = LOSS_RE.search(text)
    if match:
        return {
            "type": "loss",
            "epoch": int(match.group("epoch")),
            "epochs_total": int(match.group("epochs")),
            "micro_step": int(match.group("step")),
            "loss": float(match.group("loss")),
            "step_kind": "micro_batch",
        }

    match = MEAN_RE.search(text)
    if match:
        return {
            "type": "epoch_mean",
            "epoch": int(match.group("epoch")),
            "epochs_total": int(match.group("epochs")),
            "epoch_mean_loss": float(match.group("loss")),
        }

    match = DATASET_RE.search(text)
    if match:
        return {
            "type": "dataset",
            "train_items": int(match.group("train")),
            "calibration_items": int(match.group("calib")),
            "eval_items": int(match.group("eval")),
        }

    if CALIB_EVIDENCE_RE.search(text):
        return {
            "type": "evaluation",
            "mode": "in_sample_calibration",
            "message": text,
        }

    if EVAL_RE.search(text):
        lower = text.lower()
        if "held-out" in lower or "held out" in lower:
            mode = "held_out"
        elif "overlaps training" in lower:
            mode = "overlapping_eval"
        else:
            mode = "evaluation"
        event = {"type": "evaluation", "mode": mode, "message": text}
        items = EVAL_ITEMS_RE.search(text)
        if items:
            event["eval_items"] = int(items.group("items"))
        return event

    if "checkpoint_latest" in text.lower():
        event = {"type": "checkpoint", "checkpoint_latest": True}
        path_match = re.search(
            r"checkpoint_latest\s*(?:->|:)\s*(?P<path>.+)$", text, re.IGNORECASE
        )
        if path_match:
            event["path"] = path_match.group("path").strip()
        return event

    lower = text.lower()
    if "calibration" in lower and any(marker in lower for marker in CALIBRATION_MARKERS):
        tail = text.split("laya.train:", 1)[-1].strip() if "laya.train:" in text else text
        qtype_match = re.match(r"(?P<qtype>[A-Za-z][A-Za-z0-9_ -]*?)\s+calibration:", tail)
        return {
            "type": "calibration_warning",
            "qtype": qtype_match.group("qtype").strip() if qtype_match else None,
            "message": text,
        }

    match = COMPLETE_RE.search(text)
    if match:
        return {"type": "complete", "output_dir": match.group("out").strip()}

    if lower.startswith("laya-train: error:"):
        return {"type": "error", "message": text}

    return None


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


def _now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write_metrics(path, payload: dict) -> None:
    if path is None:
        return
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError:
        pass


def _apply_event(metrics: dict, event: dict) -> None:
    kind = event.get("type")
    if kind == "dataset":
        metrics["train_items"] = event.get("train_items")
        metrics["calibration_items"] = event.get("calibration_items")
        metrics["eval_items"] = event.get("eval_items")
    elif kind == "budget":
        metrics["updates_total"] = event.get("updates_total")
        metrics["epochs_total"] = event.get("epochs_total")
        metrics["train_items"] = event.get("train_items")
        metrics["effective_batch"] = event.get("effective_batch")
        metrics["budget_kind"] = "optimizer_updates"
    elif kind == "loss":
        metrics["epoch"] = event.get("epoch")
        metrics["epochs_total"] = event.get("epochs_total")
        metrics["micro_step"] = event.get("micro_step")
        metrics["loss"] = event.get("loss")
        metrics["step_kind"] = "micro_batch"
    elif kind == "epoch_mean":
        metrics["epoch"] = event.get("epoch")
        metrics["epoch_mean_loss"] = event.get("epoch_mean_loss")
    elif kind == "checkpoint":
        metrics["checkpoint_latest"] = True
        if event.get("path"):
            metrics["checkpoint_path"] = event.get("path")
    elif kind == "evaluation":
        metrics["evaluation"] = event.get("mode")
        metrics["evaluation_seen"] = True
    elif kind == "calibration_warning":
        warnings = metrics.setdefault("warnings", [])
        if len(warnings) < 20:
            warnings.append(event.get("message"))
    elif kind == "complete":
        metrics["output_dir"] = event.get("output_dir")
    elif kind == "error":
        errors = metrics.setdefault("errors", [])
        if len(errors) < 20:
            errors.append(event.get("message"))


# ---------------------------------------------------------------------------
# subprocess runner
# ---------------------------------------------------------------------------


def _cancel_requested(stop_file, cancel_event) -> bool:
    if cancel_event is not None and cancel_event.is_set():
        return True
    if stop_file is not None:
        try:
            return Path(stop_file).exists()
        except OSError:
            return False
    return False


def _soft_stop(proc) -> str:
    if os.name == "nt":
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
            return "CTRL_BREAK_EVENT"
        except (OSError, ValueError, AttributeError):
            pass
    else:
        try:
            proc.send_signal(signal.SIGINT)
            return "SIGINT"
        except OSError:
            pass
    try:
        proc.terminate()
        return "terminate"
    except OSError:
        return "none"


def _hard_stop(proc) -> None:
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def run(
    job,
    log_path,
    *,
    train_command=None,
    stop_file=None,
    metrics_path=None,
    cancel_event=None,
    poll_interval: float = 0.1,
    grace_s: float = 5.0,
    cwd=None,
    env=None,
) -> dict:
    """Run one Laya job, stream/parse stdout, write the log and metrics.

    ``job`` is the core job dict (``{"id": ..., "params": {...}}``) or a bare
    params dict. ``stop_file`` + ``cancel_event`` are both optional soft-stop
    inputs; when either fires the child gets SIGINT/CTRL_BREAK_EVENT, then a
    hard terminate/kill after ``grace_s``. There is no resume.
    """
    job = job if isinstance(job, dict) else {}
    params = job.get("params")
    if not isinstance(params, dict):
        raw_params = job.get("params_json")
        if isinstance(raw_params, dict):
            params = raw_params
        elif isinstance(raw_params, str):
            try:
                params = json.loads(raw_params)
            except ValueError:
                params = {}
        else:
            params = job
    if not isinstance(params, dict):
        raise ValueError("job.params must be a dict")
    job_id = str(job.get("id") or job.get("job_id") or "")

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_file = (
        Path(metrics_path)
        if metrics_path is not None
        else log_path.with_suffix(".metrics.json")
    )
    stop_path = Path(stop_file) if stop_file is not None else None
    argv = build_argv(params, train_command=train_command)

    metrics = {
        "job_id": job_id,
        "state": "starting",
        "epoch": None,
        "epochs_total": None,
        "updates_total": None,
        "micro_step": 0,
        "loss": None,
        "epoch_mean_loss": None,
        "checkpoint_latest": False,
        "evaluation": None,
        "warnings": [],
        "events": 0,
        "started_at": _now_utc(),
        "elapsed_s": 0.0,
        "returncode": None,
    }
    _write_metrics(metrics_file, metrics)

    with open(log_path, "w", encoding="utf-8", newline="\n") as log:
        log.write("# laya adapter argv: " + json.dumps(argv, ensure_ascii=False) + "\n")
        log.write("# laya adapter started_at: " + metrics["started_at"] + "\n")
        log.flush()

        if stop_path is not None and stop_path.exists():
            metrics.update(
                state="cancelled",
                cancelled=True,
                elapsed_s=0.0,
                error="stop file present before start",
            )
            log.write("# laya adapter: cancelled before spawn (stop file present)\n")
            log.flush()
            _write_metrics(metrics_file, metrics)
            return {
                "job_id": job_id,
                "state": "cancelled",
                "cancelled": True,
                "returncode": None,
                "metrics": metrics,
                "events": [],
                "argv": argv,
                "log_path": str(log_path),
            }

        child_env = os.environ.copy()
        if env:
            child_env.update({str(key): str(value) for key, value in env.items()})
        popen_kwargs = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        else:
            popen_kwargs["start_new_session"] = True

        started = time.monotonic()
        proc = None
        reader = None
        line_queue: queue.Queue = queue.Queue()
        events = []
        stop_sent = False
        stop_signal = None
        stop_deadline = 0.0
        fatal_error = None

        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd) if cwd is not None else None,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **popen_kwargs,
            )

            def _reader():
                try:
                    assert proc.stdout is not None
                    for line in iter(proc.stdout.readline, ""):
                        line_queue.put(line)
                finally:
                    line_queue.put(None)

            reader = threading.Thread(
                target=_reader, name="laya-adapter-reader", daemon=True
            )
            reader.start()
            metrics["pid"] = proc.pid
            metrics["state"] = "running"
            _write_metrics(metrics_file, metrics)

            while True:
                now = time.monotonic()
                if not stop_sent and _cancel_requested(stop_path, cancel_event):
                    stop_signal = _soft_stop(proc)
                    stop_sent = True
                    stop_deadline = now + grace_s
                    log.write(
                        "# laya adapter: cancel requested; soft stop sent (%s)\n"
                        % stop_signal
                    )
                    log.flush()
                if (
                    stop_sent
                    and not any(
                        event.get("type") == "adapter_hard_stop" for event in events
                    )
                    and proc.poll() is None
                    and now >= stop_deadline
                ):
                    log.write(
                        "# laya adapter: soft stop grace %.1fs expired; hard stop\n"
                        % grace_s
                    )
                    log.flush()
                    _hard_stop(proc)
                    events.append({"type": "adapter_hard_stop"})

                try:
                    line = line_queue.get(timeout=max(0.01, poll_interval))
                except queue.Empty:
                    continue
                if line is None:
                    break

                log.write(line if line.endswith("\n") else line + "\n")
                log.flush()
                event = parse_line(line)
                if event is not None:
                    events.append(event)
                    _apply_event(metrics, event)
                    metrics["events"] = len(events)
                    metrics["last_event"] = event.get("type")
                    metrics["elapsed_s"] = round(time.monotonic() - started, 3)
                    _write_metrics(metrics_file, metrics)

            if proc.poll() is None:
                _hard_stop(proc)
            returncode = proc.wait(timeout=10)
        except Exception:
            fatal_error = traceback.format_exc()
            log.write("# laya adapter exception:\n" + fatal_error + "\n")
            log.flush()
            if proc is not None and proc.poll() is None:
                _hard_stop(proc)
            returncode = None
        finally:
            if proc is not None and proc.poll() is None:
                _hard_stop(proc)
            if proc is not None:
                try:
                    returncode = proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    _hard_stop(proc)
                    try:
                        returncode = proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        returncode = None
            if reader is not None:
                reader.join(timeout=2)
            metrics["elapsed_s"] = round(time.monotonic() - started, 3)
            metrics["returncode"] = returncode

        if fatal_error:
            state = "failed"
            metrics["error"] = fatal_error.splitlines()[-1][:500]
        elif stop_sent:
            state = "cancelled"
        elif returncode == 0:
            state = "succeeded"
        else:
            state = "failed"
        metrics["state"] = state
        metrics["cancelled"] = bool(stop_sent)
        if stop_signal:
            metrics["soft_stop_signal"] = stop_signal
        metrics["finished_at"] = _now_utc()
        _write_metrics(metrics_file, metrics)
        log.write(
            "# laya adapter finished: state=%s returncode=%s elapsed=%.3fs\n"
            % (state, returncode, metrics["elapsed_s"])
        )
        log.flush()

    return {
        "job_id": job_id,
        "state": state,
        "cancelled": bool(stop_sent),
        "returncode": returncode,
        "metrics": metrics,
        "events": events,
        "argv": argv,
        "log_path": str(log_path),
    }
