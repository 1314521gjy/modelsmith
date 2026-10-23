#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
# ModelSmith · L2 adapter 分块图谱  |  详见 adapters/README.md
# 角色   : kind="lf"（LlamaFactory v0.9.5 主线）——core 用本文件的
#          build_yaml / write_yaml / prepare_alpaca_data，并用 core 自带的
#          build_lf_train_command(runner) 拉起；本文件也提供独立 CLI/run()
# 上游   : third_party\LlamaFactory（llamafactory-cli train <yaml>）
# 主链   : job params -> build_yaml（27 键）-> <job>\lf.yaml
#          -> [python, -c, runner(.pylibs-lf -> src -> import dill -> main)]
#          -> stdio 逐行 parse_line -> metrics -> 交回 core
# 参数三分: ① core 传入（core._lf_adapter_params 把 rank/alpha/lr/... 改成
#          lora_rank/learning_rate/... 并托管 dataset/dataset_dir）② 本文件
#          自算：runner 字符串、YAML 写入、alpaca 转换、默认值（r=8/alpha=16 等）
#          ③ 上游默认：LlamaFactory 的 Seq2SeqTrainingArguments（未写的键）
# 关键函数: build_yaml / build_argv / parse_line / run / prepare_alpaca_data
# 产物    : <out>\adapter_model.safetensors、adapter_config.json、
#          trainer_log.jsonl（RECORD_VRAM 峰值显存）、tokenizer 全套、checkpoint-N\
# 纪律    : 代码 token 不许改；本文件只加注释（注释不进 AST）
# =============================================================================
"""LlamaFactory v0.9.5 adapter (thin): params -> YAML -> `llamafactory-cli train`.

Why a thin adapter: the LLM line must run on the mature stack (LlamaFactory,
Apache-2.0) instead of a home-grown trainer; `adapters/llm_lora.py` is kept only
as a regression baseline.  This file never edits `third_party/LlamaFactory`.

The child process is started as:

    <python> -c "<runner>"          # no args
        runner = "import sys; sys.argv=['llamafactory-cli','train',<yaml>];
                  sys.path.insert(0,<lf_src>); from llamafactory.cli import main;
                  sys.exit(main())"

Reason: the ComfyUI embedded python ignores PYTHONPATH (`._pth`), so the only
reliable way to put `third_party/LlamaFactory/src` on sys.path is to do it
inside the child interpreter.

Upstream provenance for every YAML field (file:line, LlamaFactory v0.9.5):
  model_name_or_path            src/llamafactory/hparams/model_args.py:38
  trust_remote_code             src/llamafactory/hparams/model_args.py:201
  stage                         src/llamafactory/hparams/finetuning_args.py:460
  finetuning_type               src/llamafactory/hparams/finetuning_args.py:464
  lora_alpha                    src/llamafactory/hparams/finetuning_args.py:69
  lora_dropout                  src/llamafactory/hparams/finetuning_args.py:73
  lora_rank                     src/llamafactory/hparams/finetuning_args.py:77
  lora_target                   src/llamafactory/hparams/finetuning_args.py:81
  template                      src/llamafactory/hparams/data_args.py:26
  dataset                       src/llamafactory/hparams/data_args.py:30
  eval_dataset                  src/llamafactory/hparams/data_args.py:34
  dataset_dir                   src/llamafactory/hparams/data_args.py:38
  cutoff_len                    src/llamafactory/hparams/data_args.py:46
  preprocessing_num_workers     src/llamafactory/hparams/data_args.py:84
  max_samples                   src/llamafactory/hparams/data_args.py:88
  val_size                      src/llamafactory/hparams/data_args.py:100
  plot_loss                     src/llamafactory/hparams/finetuning_args.py:551
  overwrite_output_dir          src/llamafactory/hparams/training_args.py:146
  do_train / output_dir / logging_steps / save_steps / save_only_model /
  per_device_train_batch_size / gradient_accumulation_steps / learning_rate /
  num_train_epochs / max_steps / lr_scheduler_type / warmup_ratio / bf16 /
  ddp_timeout / resume_from_checkpoint / dataloader_num_workers / report_to
      -> NOT declared in LlamaFactory; inherited from
         transformers.Seq2SeqTrainingArguments
         (training_args.py:143 `class TrainingArguments(ProfilerArguments,
          Fp8Arguments, RayArguments, BaseTrainingArguments)` with
          training_args.py:19/36 BaseTrainingArguments = Seq2SeqTrainingArguments)
CLI entry:  src/llamafactory/cli.py:16 main() -> launcher.launch();
            launcher.py:154-157 `train` -> `from .train.tuner import run_exp; run_exp()`;
            hparams/parser.py:85-99 read_args(): sys.argv[1] endswith .yaml -> OmegaConf.load
"""

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import threading
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LF_ROOT = os.path.join(PROJECT_ROOT, "third_party", "LlamaFactory")
LF_SRC = os.path.join(LF_ROOT, "src")
LF_LIBS = os.path.join(PROJECT_ROOT, ".pylibs-lf")
DEFAULT_PYTHON = r"E:\ComfyUI\python_embeded\python.exe"
DEFAULT_WORKDIR = os.path.join(PROJECT_ROOT, "adapters", "_tmp", "lf_jobs")

REQUIRED = ("model", "dataset", "template", "out")

# The child seeds `dill` from LF_LIBS BEFORE importing llamafactory:
# torch imports dill itself (0.4.0 in the ComfyUI env) and puts it into
# sys.modules, while datasets 4.0 requires dill<0.3.9 -> path order alone is not
# enough, the pre-seed is what makes datasets see 0.3.8.
RUNNER_TEMPLATE = (
    "import sys; "
    "sys.path.insert(0,{libs_lit}); "
    "sys.path.insert(0,{src_lit}); "
    "import dill as _ms_dill; "
    "sys.argv=['llamafactory-cli','train',{yaml_lit}]; "
    "from llamafactory.cli import main; "
    "sys.exit(main())"
)


# --------------------------------------------------------------------------- #
# params -> YAML
# --------------------------------------------------------------------------- #
_MISSING = object()


def _get(params, *names, **kw):
    default = kw.get("default", _MISSING)
    for n in names:
        if n in params and params[n] is not None:
            return params[n]
    return None if default is _MISSING else default


def build_yaml(params):
    """Turn an LLM job's params into a LlamaFactory training config dict.

    Accepts both UI-ish keys (model/dataset/out) and upstream keys
    (model_name_or_path/output_dir).  Raises ValueError naming every missing
    required field before anything is written or spawned.
    """
    if not isinstance(params, dict):
        raise ValueError("params must be a dict, got %r" % type(params).__name__)

    model = _get(params, "model", "model_name_or_path")
    dataset = _get(params, "dataset")
    template = _get(params, "template")
    out = _get(params, "out", "output_dir")
    missing = [name for name, val in
               (("model", model), ("dataset", dataset),
                ("template", template), ("out", out)) if not val]
    if missing:
        raise ValueError("MISSING_REQUIRED: " + ",".join(missing))

    cfg = {}
    # ---- model ----------------------------------------------------------- #
    cfg["model_name_or_path"] = str(model)
    trc = _get(params, "trust_remote_code")
    if trc is not None:
        cfg["trust_remote_code"] = bool(trc)

    # ---- method ---------------------------------------------------------- #
    cfg["stage"] = str(_get(params, "stage", default="sft"))
    cfg["do_train"] = bool(_get(params, "do_train", default=True))
    cfg["finetuning_type"] = str(_get(params, "finetuning_type", default="lora"))
    if cfg["finetuning_type"] == "lora":
        rank = int(_get(params, "lora_rank", default=8))
        if rank <= 0:
            raise ValueError("lora_rank must be > 0, got %d" % rank)
        cfg["lora_rank"] = rank
        cfg["lora_alpha"] = int(_get(params, "lora_alpha", default=rank * 2))
        cfg["lora_target"] = str(_get(params, "lora_target", default="all"))
        cfg["lora_dropout"] = float(_get(params, "lora_dropout", default=0.0))
    elif cfg["finetuning_type"] not in ("full", "freeze", "oft"):
        raise ValueError("finetuning_type must be one of lora/full/freeze/oft, got %r"
                         % cfg["finetuning_type"])

    # ---- dataset --------------------------------------------------------- #
    cfg["dataset"] = str(dataset)
    ddir = _get(params, "dataset_dir")
    if ddir:
        cfg["dataset_dir"] = str(ddir)
    cfg["template"] = str(template)
    cfg["cutoff_len"] = int(_get(params, "cutoff_len", default=2048))
    if cfg["cutoff_len"] < 16:
        raise ValueError("cutoff_len must be >= 16, got %d" % cfg["cutoff_len"])
    for key in ("eval_dataset", "max_samples", "val_size",
                "preprocessing_num_workers"):
        val = _get(params, key)
        if val is not None:
            cfg[key] = val

    # ---- output ---------------------------------------------------------- #
    cfg["output_dir"] = str(out)
    cfg["logging_steps"] = int(_get(params, "logging_steps", default=10))
    cfg["save_steps"] = int(_get(params, "save_steps", default=500))
    cfg["plot_loss"] = bool(_get(params, "plot_loss", default=False))
    cfg["overwrite_output_dir"] = bool(_get(params, "overwrite_output_dir", default=True))
    cfg["save_only_model"] = bool(_get(params, "save_only_model", default=False))
    cfg["report_to"] = str(_get(params, "report_to", default="none"))

    # ---- train ----------------------------------------------------------- #
    cfg["per_device_train_batch_size"] = int(
        _get(params, "per_device_train_batch_size", default=1))
    cfg["gradient_accumulation_steps"] = int(
        _get(params, "gradient_accumulation_steps", default=1))
    cfg["learning_rate"] = float(_get(params, "learning_rate", default=5e-5))
    cfg["num_train_epochs"] = float(_get(params, "num_train_epochs", default=3.0))
    cfg["max_steps"] = int(_get(params, "max_steps", default=-1))
    cfg["lr_scheduler_type"] = str(_get(params, "lr_scheduler_type", default="cosine"))
    cfg["warmup_ratio"] = float(_get(params, "warmup_ratio", default=0.0))
    cfg["bf16"] = bool(_get(params, "bf16", default=True))
    cfg["ddp_timeout"] = int(_get(params, "ddp_timeout", default=180000000))
    for key in ("resume_from_checkpoint", "dataloader_num_workers"):
        val = _get(params, key)
        if val is not None:
            cfg[key] = val

    return cfg


def write_yaml(cfg, path):
    import yaml
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
    return path


def build_argv(yaml_path, python=None, lf_src=None, libs=None):
    """argv for the child: `<python> -c <runner>` (yaml path baked into runner)."""
    py = python or DEFAULT_PYTHON
    src = lf_src or LF_SRC
    lib = libs or LF_LIBS
    runner = RUNNER_TEMPLATE.format(yaml_lit=repr(os.path.abspath(yaml_path)),
                                    src_lit=repr(os.path.abspath(src)),
                                    libs_lit=repr(os.path.abspath(lib)))
    return [py, "-c", runner]


# --------------------------------------------------------------------------- #
# stdout parsing
# --------------------------------------------------------------------------- #
_DICT_RE = re.compile(r"^\{.*\}$")
_TQDM_RE = re.compile(r"(\d+)%\|[^|]*\|\s*(\d+)/(\d+)")


def _as_number(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


def parse_line(line):
    """Map one LlamaFactory/transformers stdout line to an event dict.

    Event types:
      loss        {'loss','learning_rate','epoch','grad_norm'}   (transformers on_log dict)
      summary     {'train_runtime','train_loss',...}             (final metrics dict)
      progress    {'step','total','percent'}                     (tqdm bar)
      info        {'key','value'} or {'event'}                   (***** / Num examples ...)
      checkpoint  {'path'}
      resume      {'path'}
      raw         {'text'}
    """
    text = (line if isinstance(line, str) else str(line)).rstrip("\r\n")
    stripped = text.strip()
    if not stripped:
        return {"type": "empty", "text": ""}

    # 1) transformers logging dicts: {'loss': 1.2, 'learning_rate': 2e-4, ...}
    if _DICT_RE.match(stripped):
        try:
            obj = ast.literal_eval(stripped)
        except (ValueError, SyntaxError):
            obj = None
        if isinstance(obj, dict):
            nums = {k: _as_number(v) for k, v in obj.items()}
            if "train_runtime" in obj or "train_loss" in obj:
                ev = {"type": "summary", "values": obj, "text": stripped}
                for key in ("train_runtime", "train_loss", "train_samples_per_second",
                            "train_steps_per_second", "epoch"):
                    if key in nums and nums[key] is not None:
                        ev[key] = nums[key]
                return ev
            if "loss" in obj:
                return {"type": "loss",
                        "loss": nums.get("loss"),
                        "learning_rate": nums.get("learning_rate"),
                        "epoch": nums.get("epoch"),
                        "grad_norm": nums.get("grad_norm"),
                        "text": stripped}
            return {"type": "dict", "values": obj, "text": stripped}

    # 2) tqdm progress bar
    m = _TQDM_RE.search(stripped)
    if m:
        return {"type": "progress", "percent": int(m.group(1)),
                "step": int(m.group(2)), "total": int(m.group(3)),
                "text": stripped}

    # 3) trainer banners / counters
    for marker in ("***** Running training *****", "***** Running Evaluation *****",
                   "***** Running Prediction *****"):
        if marker in stripped:
            return {"type": "info", "key": "phase", "value": stripped.strip("* "),
                    "text": stripped}
    for prefix, key in (("Num examples =", "num_examples"),
                        ("Num Epochs =", "num_epochs"),
                        ("Total optimization steps =", "total_steps"),
                        ("Number of trainable parameters =", "num_trainable_params"),
                        ("Total train batch size", "total_train_batch_size")):
        if stripped.startswith(prefix):
            value = stripped[len(prefix):].strip().split(" ")[0].replace(",", "")
            try:
                value = int(value)
            except ValueError:
                pass
            return {"type": "info", "key": key, "value": value, "text": stripped}

    # 4) checkpoints / resume
    if stripped.startswith("Saving model checkpoint to"):
        return {"type": "checkpoint", "path": stripped.split("to", 1)[1].strip(),
                "text": stripped}
    if stripped.startswith("Resuming training from"):
        return {"type": "resume", "path": stripped[len("Resuming training from"):].strip(),
                "text": stripped}
    if stripped.startswith("Training completed"):
        return {"type": "info", "key": "training_completed", "value": stripped,
                "text": stripped}

    return {"type": "raw", "text": stripped}


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def run(job, log_path, stop_event=None, timeout_s=None, python=None,
        lf_src=None, runner_argv=None):
    """Spawn the LlamaFactory CLI, stream+parse stdout, return a result dict.

    job: {'params': {...}, 'job_id': str?, 'yaml_path': str?, 'workdir': str?}
         (a bare params dict is also accepted)
    stop_event: threading.Event; when set the child gets terminate() then kill().
    runner_argv: test hook -- bypass the LF runner (used by the GPU-free selftest).
    """
    t0 = time.perf_counter()
    job = dict(job or {})
    params = job.get("params") or job
    job_id = str(job.get("job_id") or "lf%d" % int(time.time()))
    workdir = job.get("workdir") or DEFAULT_WORKDIR
    yaml_path = job.get("yaml_path") or os.path.join(workdir, "%s.yaml" % job_id)

    result = {"job_id": job_id, "state": "starting", "returncode": None,
              "yaml_path": None, "log_path": os.path.abspath(log_path) if log_path else None,
              "out_dir": None, "events": [], "metrics": {}, "stderr_tail": [],
              "duration_s": 0.0, "error": None, "argv": None}

    try:
        cfg = build_yaml(params)
    except ValueError as exc:
        result.update(state="failed", error=str(exc), duration_s=0.0)
        return result

    result["out_dir"] = cfg["output_dir"]
    result["metrics"] = {"step": 0, "total_steps": None, "loss": None,
                         "epoch": None, "learning_rate": None,
                         "train_runtime": None, "train_loss": None,
                         "losses": [], "checkpoints": []}
    yaml_path = write_yaml(cfg, yaml_path)
    result["yaml_path"] = yaml_path

    argv = runner_argv or build_argv(yaml_path, python=python, lf_src=lf_src)
    result["argv"] = argv

    if log_path:
        log_path = os.path.abspath(log_path)
        parent = os.path.dirname(log_path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
    log_fh = open(log_path, "a", encoding="utf-8") if log_path else None

    events = result["events"]
    metrics = result["metrics"]
    proc = None
    cancelled = False
    child_env = dict(os.environ)
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"  # never drop __pycache__ into third_party
    # UTF-8 mode: huggingface_hub's ModelCard.save() writes with the platform
    # default encoding (cp936 here) while ModelCard.load() reads UTF-8; a Chinese
    # project path in the model card then kills the post-training model-card step.
    child_env["PYTHONUTF8"] = "1"
    # make LlamaFactory's LogCallback record vram_allocated/vram_reserved into
    # <out>/trainer_log.jsonl (extras env flag, see train/callbacks.py:295)
    child_env.setdefault("RECORD_VRAM", "1")
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            bufsize=1, cwd=project_cwd(), env=child_env,
        )
        result["state"] = "running"
        t_stream = time.perf_counter()
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\r\n")
            if log_fh:
                log_fh.write(line + "\n")
                log_fh.flush()
            ev = parse_line(line)
            if len(events) < 20000:
                events.append(ev)
            _apply_event(ev, metrics)
            if len(result["stderr_tail"]) < 40 and ev["type"] in ("raw",) and \
                    ("Error" in line or "error" in line or "Traceback" in line):
                result["stderr_tail"].append(line[:400])
            if stop_event is not None and stop_event.is_set():
                cancelled = True
                break
            if timeout_s and (time.perf_counter() - t_stream) > timeout_s:
                cancelled = True
                result["error"] = "TIMEOUT_%ss" % timeout_s
                break
        if cancelled:
            _terminate(proc)
        rc = proc.wait()
        result["returncode"] = rc
        _collect_trainer_log(cfg["output_dir"], metrics)
        if cancelled:
            result["state"] = "cancelled"
        elif rc == 0:
            result["state"] = "succeeded"
        else:
            result["state"] = "failed"
            if not result["error"]:
                result["error"] = "EXIT_%d" % rc
    except FileNotFoundError as exc:
        result.update(state="failed", error="PYTHON_NOT_FOUND: %s" % exc)
    except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
        result.update(state="failed", error="%s: %s" % (type(exc).__name__, exc))
        if proc is not None and proc.poll() is None:
            _terminate(proc)
    finally:
        if log_fh:
            log_fh.close()
        result["duration_s"] = round(time.perf_counter() - t0, 3)

    metrics["losses"] = metrics["losses"][-1000:]
    result["metrics"] = metrics
    return result


def _apply_event(ev, metrics):
    kind = ev.get("type")
    if kind == "loss":
        if ev.get("loss") is not None:
            metrics["loss"] = ev["loss"]
            metrics["losses"].append({"loss": ev["loss"],
                                      "epoch": ev.get("epoch"),
                                      "learning_rate": ev.get("learning_rate")})
        if ev.get("epoch") is not None:
            metrics["epoch"] = ev["epoch"]
        if ev.get("learning_rate") is not None:
            metrics["learning_rate"] = ev["learning_rate"]
    elif kind == "progress":
        # Two different bars can appear: dataset preprocessing (e.g. 200/200)
        # and the training bar (2/2).  Keep them apart by total, otherwise the
        # preprocessing bar wins the "step" field.
        total = ev.get("total") or 0
        step = ev.get("step") or 0
        bars = metrics.setdefault("bars", {})
        bars[str(total)] = max(bars.get(str(total), 0), step)
        if total and (not metrics.get("total_steps") or total == metrics["total_steps"]):
            metrics["step"] = step
            metrics["total_steps"] = total
    elif kind == "info":
        if ev.get("key") == "total_steps" and ev.get("value"):
            metrics["total_steps"] = ev["value"]
        elif ev.get("key") == "num_examples":
            metrics["num_examples"] = ev.get("value")
    elif kind == "checkpoint":
        metrics["checkpoints"].append(ev.get("path"))
    elif kind == "summary":
        values = ev.get("values") or {}
        for key in ("train_runtime", "train_loss", "train_samples_per_second",
                    "train_steps_per_second", "epoch"):
            if key in values:
                metrics[key] = values[key]


def _collect_trainer_log(out_dir, metrics):
    """Peak VRAM recorded by LlamaFactory's LogCallback (RECORD_VRAM=1), in GiB."""
    path = os.path.join(out_dir or "", "trainer_log.jsonl")
    if not out_dir or not os.path.isfile(path):
        return
    peak_alloc = 0.0
    peak_res = 0.0
    lines = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                lines += 1
                for key, bucket in (("vram_allocated", "alloc"), ("vram_reserved", "res")):
                    val = obj.get(key)
                    if isinstance(val, (int, float)) and not isinstance(val, bool):
                        if bucket == "alloc":
                            peak_alloc = max(peak_alloc, float(val))
                        else:
                            peak_res = max(peak_res, float(val))
    except OSError:
        return
    metrics["trainer_log_lines"] = lines
    if peak_alloc:
        metrics["peak_vram_allocated_gib"] = peak_alloc
    if peak_res:
        metrics["peak_vram_reserved_gib"] = peak_res


def _terminate(proc):
    try:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    except Exception:
        pass


def project_cwd():
    """Run from the project root, never from inside third_party (no stray writes)."""
    return PROJECT_ROOT if os.path.isdir(PROJECT_ROOT) else os.getcwd()


# --------------------------------------------------------------------------- #
# dataset preparation: dolly jsonl -> alpaca json + dataset_info.json
# (LlamaFactory reads a JSON *list* of {instruction,input,output} and a
#  dataset_info.json entry {"file_name": "..."} -- data/dataset_info.json:5-7)
# --------------------------------------------------------------------------- #
def _row_text(row, *keys):
    """First non-empty string among ``keys`` in ``row`` (alpaca & dolly aliases)."""
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def read_dolly_rows(path, limit=None):
    """jsonl rows with a trainable target; empty responses are skipped.

    Accepts the dolly schema (``instruction/context/response``) and the alpaca
    schema (``instruction/input/output``); LlamaFactory's alpaca columns are
    ``instruction/input/output`` (``data/parser.py:51-53``), so both land there.
    """
    rows = []
    skipped = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not _row_text(row, "response", "output").strip():
                skipped += 1
                continue
            rows.append(row)
            if limit and len(rows) >= limit:
                break
    return rows, skipped


def prepare_alpaca_data(dolly_path, data_out, limit=200, dataset_name=None):
    """Write <data_out>/<dataset_name>.json + dataset_info.json, return a summary."""
    rows, skipped = read_dolly_rows(dolly_path, limit=limit)
    if not rows:
        raise ValueError("NO_ROWS: %s" % dolly_path)
    name = dataset_name or ("ms_dolly_%d" % len(rows))
    os.makedirs(data_out, exist_ok=True)
    records = []
    for row in rows:
        records.append({
            "instruction": _row_text(row, "instruction", "prompt").strip(),
            "input": _row_text(row, "context", "input", "query").strip(),
            "output": _row_text(row, "response", "output").strip(),
        })
    data_path = os.path.join(data_out, "%s.json" % name)
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=1)

    info_path = os.path.join(data_out, "dataset_info.json")
    info = {}
    if os.path.isfile(info_path):
        with open(info_path, "r", encoding="utf-8") as f:
            info = json.load(f)
    info[name] = {"file_name": os.path.basename(data_path)}
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)

    cats = {}
    for row in rows:
        cats[row.get("category") or "unknown"] = cats.get(row.get("category") or "unknown", 0) + 1
    return {"dataset_name": name, "data_path": os.path.abspath(data_path),
            "dataset_info": os.path.abspath(info_path), "rows": len(records),
            "skipped": skipped, "categories": dict(sorted(cats.items(), key=lambda kv: -kv[1])),
            "has_input": sum(1 for r in records if r["input"]),
            "total_chars": sum(len(r["instruction"]) + len(r["input"]) + len(r["output"])
                               for r in records)}


# --------------------------------------------------------------------------- #
# dependency probe (no install, no GPU)
# --------------------------------------------------------------------------- #
PROBE_MODULES = ("llamafactory", "transformers", "datasets", "accelerate",
                 "peft", "trl", "torchdata", "tyro", "sse_starlette",
                 "hf_transfer", "yaml", "pyarrow", "dill", "multiprocess")


def probe_dependencies():
    """Import each package; a namespace-package stand-in counts as BROKEN.

    Rationale (measured on this machine): `import datasets` may "succeed" by
    finding an unrelated directory named `datasets` on sys.path (ComfyUI custom
    node), yielding `__file__ is None` and no `DatasetDict`.  Reporting that as
    OK would be a false green.
    """
    import importlib
    out = {"python": sys.executable, "version": sys.version.split()[0],
           "libs": os.path.abspath(LF_LIBS), "libs_exists": os.path.isdir(LF_LIBS),
           "src": os.path.abspath(LF_SRC),
           "modules": {}, "runtime": {}, "lf_train_import": {}}

    sys.dont_write_bytecode = True  # importing LF must not drop __pycache__ into third_party
    if os.path.isdir(LF_LIBS) and LF_LIBS not in sys.path:
        sys.path.insert(0, LF_LIBS)
    if LF_SRC not in sys.path:
        sys.path.insert(0, LF_SRC)
    if os.path.isdir(LF_LIBS):
        try:
            import dill  # noqa: F401  -- seed before torch, see RUNNER_TEMPLATE
        except Exception:  # noqa: BLE001
            pass

    for name in PROBE_MODULES:
        try:
            mod = importlib.import_module(name)
            f = getattr(mod, "__file__", None)
            if f is None:
                out["modules"][name] = {"status": "BROKEN_NAMESPACE",
                                        "detail": list(getattr(mod, "__path__", []))[:2]}
            else:
                out["modules"][name] = {"status": "OK",
                                        "version": str(getattr(mod, "__version__", "") or ""),
                                        "file": f}
        except Exception as exc:  # noqa: BLE001
            out["modules"][name] = {"status": "MISSING",
                                    "detail": "%s: %s" % (type(exc).__name__, exc)}

    # the decisive test: can LlamaFactory's train entry actually import?
    stmts = ("from llamafactory.hparams.parser import get_train_args",
             "from llamafactory.model import load_model",
             "from llamafactory.train.tuner import run_exp")
    for stmt in stmts:
        try:
            exec(compile(stmt, "<probe>", "exec"), {})  # noqa: S102 - fixed stmts
            out["lf_train_import"][stmt] = {"ok": True}
        except Exception as exc:  # noqa: BLE001
            out["lf_train_import"][stmt] = {"ok": False,
                                            "error": "%s: %s" % (type(exc).__name__, exc)}
    # runtime sanity: the lib dir must NOT shadow torch/transformers (it once
    # contained a CPU-only torch 2.14.1 because `pip --target` installs every dep)
    libs_abs = os.path.abspath(LF_LIBS)
    for name in ("torch", "transformers", "huggingface_hub"):
        try:
            mod = importlib.import_module(name)
            f = getattr(mod, "__file__", None) or ""
            out["runtime"]["%s_version" % name] = str(getattr(mod, "__version__", "") or "")
            out["runtime"]["%s_file" % name] = f
            out["runtime"]["%s_from_libs" % name] = bool(f) and os.path.abspath(f).startswith(libs_abs)
        except Exception as exc:  # noqa: BLE001
            out["runtime"]["%s_error" % name] = "%s: %s" % (type(exc).__name__, exc)
    try:
        import torch
        out["runtime"]["cuda_available"] = bool(torch.cuda.is_available())
        out["runtime"]["cuda_version"] = torch.version.cuda
    except Exception:  # noqa: BLE001
        out["runtime"]["cuda_available"] = None
    shadowed = [k for k in ("torch", "transformers") if out["runtime"].get("%s_from_libs" % k)]
    out["shadowed_from_libs"] = shadowed
    out["ready_for_sft"] = (all(v["ok"] for v in out["lf_train_import"].values())
                            and not shadowed)
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _params_from_args(args):
    if args.params_json:
        if os.path.isfile(args.params_json):
            with open(args.params_json, "r", encoding="utf-8") as f:
                return json.load(f)
        return json.loads(args.params_json)
    return {k: getattr(args, k) for k in (
        "model", "dataset", "dataset_dir", "template", "out", "cutoff_len",
        "lora_rank", "lora_alpha", "lora_target", "learning_rate", "num_train_epochs",
        "max_steps", "per_device_train_batch_size", "gradient_accumulation_steps",
        "logging_steps", "save_steps", "max_samples", "bf16")
        if getattr(args, k) is not None}


def main(argv=None):
    p = argparse.ArgumentParser(description="LlamaFactory v0.9.5 thin adapter")
    p.add_argument("--params-json", default=None,
                   help="JSON string or path with the job params")
    p.add_argument("--model", default=None)
    p.add_argument("--dataset", default=None)
    p.add_argument("--dataset-dir", default=None)
    p.add_argument("--template", default=None)
    p.add_argument("--out", default=None)
    p.add_argument("--cutoff-len", type=int, default=None)
    p.add_argument("--lora-rank", type=int, default=None)
    p.add_argument("--lora-alpha", type=int, default=None)
    p.add_argument("--lora-target", default=None)
    p.add_argument("--learning-rate", type=float, default=None)
    p.add_argument("--num-train-epochs", type=float, default=None)
    p.add_argument("--max-steps", type=int, default=None)
    p.add_argument("--per-device-train-batch-size", type=int, default=None)
    p.add_argument("--gradient-accumulation-steps", type=int, default=None)
    p.add_argument("--logging-steps", type=int, default=None)
    p.add_argument("--save-steps", type=int, default=None)
    p.add_argument("--max-samples", type=int, default=None)
    p.add_argument("--bf16", dest="bf16", action="store_true", default=None)
    p.add_argument("--yaml-out", default=None, help="where to write the YAML")
    p.add_argument("--workdir", default=DEFAULT_WORKDIR)
    p.add_argument("--log", default=None)
    p.add_argument("--python", default=None)
    p.add_argument("--dry-run", action="store_true", help="write YAML + print argv, do not spawn")
    p.add_argument("--probe-deps", action="store_true", help="dependency probe, no training")
    p.add_argument("--prepare-dolly", default=None,
                   help="dolly jsonl -> alpaca json + dataset_info.json (no GPU)")
    p.add_argument("--data-out", default=None, help="dataset dir for --prepare-dolly")
    p.add_argument("--dataset-name", default=None, help="registered dataset name")
    p.add_argument("--limit", type=int, default=200, help="rows for --prepare-dolly")
    p.add_argument("--json", action="store_true", help="print result as JSON")
    args = p.parse_args(argv)

    if args.prepare_dolly:
        data_out = args.data_out or os.path.join(DEFAULT_WORKDIR, "data")
        try:
            res = prepare_alpaca_data(args.prepare_dolly, data_out,
                                      limit=args.limit or 200,
                                      dataset_name=args.dataset_name)
        except ValueError as exc:
            print("LF_DATA_PREP_FAILED: %s" % exc)
            return 5
        print(json.dumps(res, ensure_ascii=False, indent=2))
        print("LF_DATA_PREP_OK rows=%d name=%s" % (res["rows"], res["dataset_name"]))
        return 0

    if args.probe_deps:
        res = probe_dependencies()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        print("LF_READY_FOR_SFT=%s" % res["ready_for_sft"])
        return 0 if res["ready_for_sft"] else 2

    try:
        params = _params_from_args(args)
        cfg = build_yaml(params)
    except ValueError as exc:
        print("LF_PARAMS_INVALID: %s" % exc)
        return 3

    yaml_path = args.yaml_out or os.path.join(args.workdir, "lf_cli.yaml")
    yaml_path = write_yaml(cfg, yaml_path)
    print("yaml: %s" % yaml_path)
    argv_child = build_argv(yaml_path, python=args.python)
    print("argv: %s -c <%d chars runner>" % (argv_child[0], len(argv_child[2])))
    print("runner: %s" % argv_child[2])

    if args.dry_run:
        print("YAML_DRY_RUN_OK")
        return 0

    log_path = args.log or os.path.join(args.workdir, "lf_cli.log")
    res = run({"params": params, "yaml_path": yaml_path, "workdir": args.workdir},
              log_path, python=args.python)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    else:
        print("state=%s rc=%s duration=%.1fs" % (res["state"], res["returncode"],
                                                 res["duration_s"]))
        keys = ("step", "total_steps", "loss", "epoch", "train_runtime", "train_loss",
                "peak_vram_allocated_gib", "peak_vram_reserved_gib", "bars",
                "trainer_log_lines")
        print("metrics=%s" % json.dumps({k: res["metrics"].get(k) for k in keys},
                                        ensure_ascii=False))
        if res["error"]:
            print("error=%s" % res["error"])
    return 0 if res["state"] == "succeeded" else 4


if __name__ == "__main__":
    sys.exit(main())
