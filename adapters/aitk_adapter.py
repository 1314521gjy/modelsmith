#!/usr/bin/env python3
# =============================================================================
# ModelSmith · L2 adapter 分块图谱  |  详见 adapters/README.md
# 角色   : kind="aitk"（生图/视频 LoRA）的适配器，由 core 的 _run_aitk 调用
# 上游   : third_party\ai-toolkit（run.py 读 YAML；字段名以
#          toolkit\config_modules.py 为准；见 adapters\README.md 映射表）
# 主链   : job params（core 已按 AITK_PARAM_MAP 改名）-> build_config
#          -> dump_yaml / write_config（<job>\aitk.yaml）-> [python, -c, runner,
#          run.py, yaml] 子进程 -> stdout parse_line -> metrics.json
# 参数三分: ① core 传入（AITK_PARAM_MAP：steps/batch/lr/rank/alpha/... 已改名）
#          ② 本文件自算：sample_plan（采样组数）、默认 save_every/keep、
#          grad 字段名互斥处理、YAML 序列化 ③ 上游默认：未写进 YAML 的键
# 关键函数: sample_plan / validate_params / build_config / dump_yaml /
#          write_config / parse_line / run
# 产物    : <training_folder>\<name>.safetensors + 采样图 + 上游日志
#          （本适配器不扫产物，core /api/jobs/<id>/artifacts 通用扫描可见）
# 纪律    : 代码 token 不许改；本文件只加注释（注释不进 AST）
# =============================================================================
"""ai-toolkit (image/video LoRA) adapter for ModelSmith — P4 preparation.

Stdlib only. No GPU, no dependency install, no model download.

The YAML field names below are checked against
``third_party/ai-toolkit/toolkit/config_modules.py`` (and the process-level
fields against ``jobs/TrainJob.py`` / ``jobs/BaseJob.py``); see
``adapters/README.md`` for the field-to-line table.

Public API:
    build_config(params, warnings=None) -> dict
    validate_params(params) -> list[str]
    sample_plan(steps, sample_every, ...) -> dict
    parse_line(line) -> dict | None
    dump_yaml(config) -> str
    write_config(path, config) -> Path
    run(job, log_path, ...) -> dict

Counter discipline:
    * ``step`` / ``total`` come from the progress postfix or config
    * ``sample_step`` comes from ``Sampling at step N`` / ``Sample at step N``
    * ``save_step`` comes from ``Saving at step N``
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

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AITK_ROOT = PROJECT_ROOT / "third_party" / "ai-toolkit"
AITK_RUN = AITK_ROOT / "run.py"
SAVE_FORMATS = ("safetensors", "diffusers")
DEFAULT_STEPS = 1000
DEFAULT_SAMPLE_EVERY = 100
DEFAULT_SAVE_EVERY = 1000
DEFAULT_KEEP = 5
DEFAULT_SAMPLE_STEPS = 20
DEFAULT_GUIDANCE = 7.0
DEFAULT_BATCH = 1
DEFAULT_GRAD_ACCUM = 1

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
BASELINE_RE = re.compile(
    r"Generating baseline samples before training|"
    r"Generating first sample from first sample config|"
    r"Generating baseline samples",
    re.IGNORECASE,
)
SAMPLE_RE = re.compile(r"(?:Sampling|Sample) at step\s+(?P<step>\d+)", re.IGNORECASE)
SAVE_RE = re.compile(r"Saving at step\s+(?P<step>\d+)", re.IGNORECASE)
PROGRESS_STEP_RE = re.compile(r"\b(?P<step>\d+)\s*/\s*(?P<total>\d+)\b")
LR_RE = re.compile(
    r"\blr:\s*(?P<lr>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)",
    re.IGNORECASE,
)
LOSS_RE = re.compile(
    r"\bloss(?:\.[A-Za-z0-9_/]+)?:\s*"
    r"(?P<loss>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)",
    re.IGNORECASE,
)
OOM_RE = re.compile(r"OOM during training step", re.IGNORECASE)
OOM_COUNT_RE = re.compile(r"OOM.*?(?P<n>\d+)\s*/\s*3", re.IGNORECASE)
DONE_RE = re.compile(
    r"Training complete|Done training|Training finished|Finished training|"
    r"Training done|Job stopped",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _clean_line(line) -> str:
    text = str(line or "").replace("\r", "")
    text = ANSI_RE.sub("", text)
    return text.rstrip("\n").strip()


def _present(value) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _param(params: dict, key: str, *fallbacks, default=None):
    """Look up dotted key (sample.prompts), then flat aliases."""
    candidates = (key,) + fallbacks
    for candidate in candidates:
        current = params
        found = True
        for part in str(candidate).split("."):
            if not isinstance(current, dict) or part not in current:
                found = False
                break
            current = current[part]
        if found and current is not None:
            return current
    return default


def _require_param(params: dict, key: str, *fallbacks):
    value = _param(params, key, *fallbacks, default=None)
    if not _present(value):
        raise ValueError("missing required param(s): %s" % key)
    return value


def _as_int(value, name: str, minimum: int = 1) -> int:
    if isinstance(value, bool):
        raise ValueError("%s must be an integer" % name)
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be an integer" % name)
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("%s must be an integer" % name)
    if number < minimum:
        raise ValueError("%s must be >= %d" % (name, minimum))
    return number


def _as_float(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s must be a number" % name)
    return float(value)


def _as_str(value, name: str) -> str:
    if value is None:
        raise ValueError("%s is required" % name)
    text = str(value)
    if not text.strip():
        raise ValueError("%s is required" % name)
    return text


# ---------------------------------------------------------------------------
# sampling plan (steps/sample_every arithmetic)
# ---------------------------------------------------------------------------


def sample_plan(
    steps,
    sample_every=DEFAULT_SAMPLE_EVERY,
    sample_start_step=0,
    skip_first_sample=False,
    disable_sampling=False,
) -> dict:
    """Return baseline + scheduled sample groups.

    ai-toolkit samples once before the loop (unless skipped/disabled), then at
    every ``sample_every`` step from ``sample_start_step`` onward; the loop
    skips step 0, so a divisible final step gets one group.
    """
    steps = _as_int(steps, "steps")
    sample_every = _as_int(sample_every, "sample_every", minimum=0)
    sample_start_step = _as_int(sample_start_step, "sample_start_step", minimum=0)
    baseline = 0 if (skip_first_sample or disable_sampling) else 1
    sample_steps = []
    if not disable_sampling and sample_every > 0:
        for step in range(1, steps + 1):
            if step >= sample_start_step and step % sample_every == 0:
                sample_steps.append(step)
    last_sample_step = sample_steps[-1] if sample_steps else None
    final_divisible = bool(
        sample_steps and steps % sample_every == 0 and last_sample_step == steps
    ) if sample_every > 0 else False
    warning = None
    if not disable_sampling and sample_every > 0 and not final_divisible:
        warning = (
            "末步不整除：steps=%d, sample_every=%d，最后采样停在 step %s；"
            "终版 LoRA 没有对应采样图"
            % (steps, sample_every, last_sample_step if last_sample_step is not None else "—")
        )
    return {
        "steps": steps,
        "sample_every": sample_every,
        "sample_start_step": sample_start_step,
        "baseline_groups": baseline,
        "sample_steps": sample_steps,
        "group_count": baseline + len(sample_steps),
        "last_sample_step": last_sample_step,
        "final_divisible": final_divisible,
        "warning": warning,
    }


# ---------------------------------------------------------------------------
# config generation
# ---------------------------------------------------------------------------


def _validate_hard(params: dict):
    """Return normalized scalar values; raise ValueError on hard errors."""
    name = _as_str(_require_param(params, "config.name", "name", "job_name"), "config.name")
    training_folder = _as_str(
        _require_param(params, "process.training_folder", "training_folder", "out", "output_dir"),
        "process.training_folder",
    )
    folder_path = _as_str(
        _require_param(params, "datasets.folder_path", "folder_path", "data", "data_dir"),
        "datasets.folder_path",
    )
    model_name = _as_str(
        _require_param(
            params,
            "model.name_or_path",
            "model_name_or_path",
            "name_or_path",
            "base",
        ),
        "model.name_or_path",
    )

    steps = _as_int(_param(params, "train.steps", "steps", default=DEFAULT_STEPS), "train.steps")
    batch = _as_int(
        _param(params, "train.batch_size", "batch_size", "batch", default=DEFAULT_BATCH),
        "train.batch_size",
    )
    lr_raw = _param(params, "train.lr", "lr", default=None)
    lr = None if lr_raw is None else _as_float(lr_raw, "train.lr")
    if lr is not None and lr <= 0:
        raise ValueError("train.lr must be > 0")

    ga_raw = _param(params, "train.gradient_accumulation", "gradient_accumulation", default=None)
    gas_raw = _param(
        params, "train.gradient_accumulation_steps", "gradient_accumulation_steps", default=None
    )
    ga = None if ga_raw is None else _as_int(ga_raw, "train.gradient_accumulation")
    gas = None if gas_raw is None else _as_int(gas_raw, "train.gradient_accumulation_steps")
    if ga is not None and gas is not None and ga > 1 and gas > 1:
        raise ValueError(
            "gradient_accumulation and gradient_accumulation_steps are mutually exclusive"
        )
    if ga is not None:
        grad_key, grad_value = "gradient_accumulation", ga
    elif gas is not None:
        grad_key, grad_value = "gradient_accumulation_steps", gas
    else:
        grad_key, grad_value = "gradient_accumulation", DEFAULT_GRAD_ACCUM

    sampler_raw = _param(params, "sample.sampler", "sampler", default=None)
    noise_raw = _param(params, "train.noise_scheduler", "noise_scheduler", default=None)
    if sampler_raw is not None and noise_raw is not None:
        if str(sampler_raw) != str(noise_raw):
            raise ValueError(
                "sample.sampler %r must match train.noise_scheduler %r"
                % (sampler_raw, noise_raw)
            )
    scheduler = str(noise_raw if noise_raw is not None else (sampler_raw or "ddpm"))

    save_format = str(
        _param(params, "save.save_format", "save_format", default="safetensors")
    )
    if save_format not in SAVE_FORMATS:
        raise ValueError(
            "save_format must be one of %s, got %r"
            % (", ".join(SAVE_FORMATS), save_format)
        )
    save_every = _as_int(
        _param(params, "save.save_every", "save_every", default=DEFAULT_SAVE_EVERY),
        "save.save_every",
    )
    keep = _as_int(
        _param(
            params,
            "save.max_step_saves_to_keep",
            "max_step_saves_to_keep",
            "keep",
            default=DEFAULT_KEEP,
        ),
        "save.max_step_saves_to_keep",
    )
    sample_every = _as_int(
        _param(
            params,
            "sample.sample_every",
            "sample_every",
            default=DEFAULT_SAMPLE_EVERY,
        ),
        "sample.sample_every",
        minimum=0,
    )
    sample_start = _as_int(
        _param(
            params,
            "sample.sample_start_step",
            "sample_start_step",
            default=0,
        ),
        "sample.sample_start_step",
        minimum=0,
    )
    sample_steps = _as_int(
        _param(
            params,
            "sample.sample_steps",
            "sample_steps",
            default=DEFAULT_SAMPLE_STEPS,
        ),
        "sample.sample_steps",
    )
    guidance = _as_float(
        _param(
            params,
            "sample.guidance_scale",
            "guidance_scale",
            default=DEFAULT_GUIDANCE,
        ),
        "sample.guidance_scale",
    )
    width = _as_int(_param(params, "sample.width", "width", default=512), "sample.width")
    height = _as_int(_param(params, "sample.height", "height", default=512), "sample.height")
    num_frames = _as_int(
        _param(params, "sample.num_frames", "num_frames", default=1), "sample.num_frames"
    )
    prompts_raw = _param(params, "sample.prompts", "prompts", "sample_prompts", default=[])
    if prompts_raw is None:
        prompts_raw = []
    if not isinstance(prompts_raw, list) or not all(
        isinstance(prompt, str) for prompt in prompts_raw
    ):
        raise ValueError("sample.prompts must be a list of strings")
    prompts = list(prompts_raw)

    trigger = _param(params, "datasets.trigger_word", "trigger_word", default=None)
    trigger = None if trigger is None else str(trigger)
    arch = _param(params, "model.arch", "arch", default=None)
    arch = None if arch is None else str(arch)
    caption_ext = str(_param(params, "datasets.caption_ext", "caption_ext", default=".txt"))
    if caption_ext and not caption_ext.startswith("."):
        caption_ext = "." + caption_ext
    resolution = _param(params, "datasets.resolution", "resolution", default=None)
    if resolution is not None and not isinstance(resolution, (int, list)):
        raise ValueError("datasets.resolution must be an int or a list")
    cache_text = _param(
        params, "train.cache_text_embeddings", "cache_text_embeddings", default=None
    )
    cache_latents = _param(
        params, "datasets.cache_latents_to_disk", "cache_latents_to_disk", default=None
    )
    skip_first = bool(
        _param(params, "train.skip_first_sample", "skip_first_sample", default=False)
    )
    disable_sampling = bool(
        _param(params, "train.disable_sampling", "disable_sampling", default=False)
    )
    linear = _param(params, "network.linear", "linear", "rank", default=None)
    linear_alpha = _param(
        params, "network.linear_alpha", "linear_alpha", "alpha", default=None
    )
    linear = None if linear is None else _as_int(linear, "network.linear")
    linear_alpha = None if linear_alpha is None else _as_float(linear_alpha, "network.linear_alpha")
    device = _param(params, "process.device", "device", default="cuda:0")

    return {
        "name": name,
        "training_folder": training_folder,
        "folder_path": folder_path,
        "model_name": model_name,
        "steps": steps,
        "batch": batch,
        "lr": lr,
        "grad_key": grad_key,
        "grad_value": grad_value,
        "scheduler": scheduler,
        "save_format": save_format,
        "save_every": save_every,
        "keep": keep,
        "sample_every": sample_every,
        "sample_start": sample_start,
        "sample_steps": sample_steps,
        "guidance": guidance,
        "width": width,
        "height": height,
        "num_frames": num_frames,
        "prompts": prompts,
        "trigger": trigger,
        "arch": arch,
        "caption_ext": caption_ext,
        "resolution": resolution,
        "cache_text": cache_text,
        "cache_latents": cache_latents,
        "skip_first": skip_first,
        "disable_sampling": disable_sampling,
        "linear": linear,
        "linear_alpha": linear_alpha,
        "device": device,
    }


def validate_params(params: dict) -> list:
    """Return soft warnings; hard errors raise ValueError.

    Warnings currently include: empty sample.prompts, final step not divisible
    by sample_every, trigger_word + cache_text_embeddings, and sampling off.
    """
    warnings = []
    values = _validate_hard(params)
    if not values["prompts"]:
        warnings.append(
            "sample.prompts 为空：不会产生任何采样图，LoRA 失去唯一视觉验收信号"
        )
    if values["sample_every"] == 0 or values["disable_sampling"]:
        warnings.append("采样已关闭（sample_every=0 或 disable_sampling=true）")
    plan = sample_plan(
        values["steps"],
        values["sample_every"],
        values["sample_start"],
        values["skip_first"],
        values["disable_sampling"],
    )
    if plan["warning"]:
        warnings.append(plan["warning"])
    if values["trigger"] and values["cache_text"]:
        warnings.append(
            "cache_text_embeddings=true 时 trigger_word 不生效（上游行为）"
        )
    return warnings


def build_config(params: dict, warnings=None) -> dict:
    """Build the full ai-toolkit job document accepted by ``run.py``."""
    if not isinstance(params, dict):
        raise ValueError("params must be a dict")
    values = _validate_hard(params)
    warnings = warnings if warnings is not None else []
    if not values["prompts"]:
        warnings.append(
            "sample.prompts 为空：不会产生任何采样图，LoRA 失去唯一视觉验收信号"
        )
    if values["sample_every"] == 0 or values["disable_sampling"]:
        warnings.append("采样已关闭（sample_every=0 或 disable_sampling=true）")
    plan = sample_plan(
        values["steps"],
        values["sample_every"],
        values["sample_start"],
        values["skip_first"],
        values["disable_sampling"],
    )
    if plan["warning"]:
        warnings.append(plan["warning"])
    if values["trigger"] and values["cache_text"]:
        warnings.append(
            "cache_text_embeddings=true 时 trigger_word 不生效（上游行为）"
        )

    train = {
        "steps": values["steps"],
        "batch_size": values["batch"],
    }
    if values["lr"] is not None:
        train["lr"] = values["lr"]
    train[values["grad_key"]] = values["grad_value"]
    train["noise_scheduler"] = values["scheduler"]
    if values["cache_text"] is not None:
        train["cache_text_embeddings"] = bool(values["cache_text"])
    if values["skip_first"]:
        train["skip_first_sample"] = True
    if values["disable_sampling"]:
        train["disable_sampling"] = True

    dataset = {
        "folder_path": values["folder_path"],
        "caption_ext": values["caption_ext"],
    }
    if values["trigger"]:
        dataset["trigger_word"] = values["trigger"]
    if values["resolution"] is not None:
        dataset["resolution"] = values["resolution"]
    if values["num_frames"] > 1:
        dataset["num_frames"] = values["num_frames"]
    if values["cache_latents"] is not None:
        dataset["cache_latents_to_disk"] = bool(values["cache_latents"])

    sample = {
        "sampler": values["scheduler"],
        "sample_every": values["sample_every"],
        "sample_start_step": values["sample_start"],
        "sample_steps": values["sample_steps"],
        "guidance_scale": values["guidance"],
        "width": values["width"],
        "height": values["height"],
        "num_frames": values["num_frames"],
        "prompts": values["prompts"],
    }

    network = {"type": "lora"}
    if values["linear"] is not None:
        network["linear"] = values["linear"]
    if values["linear_alpha"] is not None:
        network["linear_alpha"] = values["linear_alpha"]

    process = {
        "type": "sd_trainer",
        "training_folder": values["training_folder"],
        "device": str(values["device"]),
        "network": network,
        "save": {
            "save_every": values["save_every"],
            "max_step_saves_to_keep": values["keep"],
            "save_format": values["save_format"],
        },
        "datasets": [dataset],
        "train": train,
        "model": {"name_or_path": values["model_name"]},
        "sample": sample,
    }
    if values["arch"]:
        process["model"]["arch"] = values["arch"]
    if values["trigger"]:
        # process-level trigger is also real (BaseSDTrainProcess reads it and
        # propagates to datasets whose trigger_word is None); keep both explicit.
        process["trigger_word"] = values["trigger"]

    return {
        "job": "extension",
        "config": {
            "name": values["name"],
            "process": [process],
        },
        "meta": {"name": "[name]", "version": "1.0"},
    }


# ---------------------------------------------------------------------------
# YAML writer (stdlib only)
# ---------------------------------------------------------------------------


def _yaml_scalar(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("YAML cannot serialize non-finite float")
        return repr(value)
    return json.dumps(str(value), ensure_ascii=False)


def dump_yaml(value, indent: int = 0) -> str:
    lines = []
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return pad + "{}"
        for key, child in value.items():
            key_text = str(key)
            if isinstance(child, dict):
                if child:
                    lines.append(pad + key_text + ":")
                    lines.append(dump_yaml(child, indent + 2))
                else:
                    lines.append(pad + key_text + ": {}")
            elif isinstance(child, list):
                if child:
                    lines.append(pad + key_text + ":")
                    for item in child:
                        if isinstance(item, (dict, list)):
                            lines.append(pad + "  -")
                            lines.append(dump_yaml(item, indent + 4))
                        else:
                            lines.append(pad + "  - " + _yaml_scalar(item))
                else:
                    lines.append(pad + key_text + ": []")
            else:
                lines.append(pad + key_text + ": " + _yaml_scalar(child))
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return pad + "[]"
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(pad + "-")
                lines.append(dump_yaml(item, indent + 2))
            else:
                lines.append(pad + "- " + _yaml_scalar(item))
        return "\n".join(lines)
    return pad + _yaml_scalar(value)


def write_config(path, config: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "---\n" + dump_yaml(config) + "\n"
    temporary = path.with_name(path.name + ".tmp-%d-%d" % (os.getpid(), threading.get_ident()))
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
    return path


# ---------------------------------------------------------------------------
# log parser
# ---------------------------------------------------------------------------


def parse_line(line) -> dict | None:
    """Parse one ai-toolkit output line into an event, or None."""
    text = _clean_line(line)
    if not text:
        return None
    if BASELINE_RE.search(text):
        return {"type": "baseline_sample", "step": 0, "sample_step": 0}
    match = SAMPLE_RE.search(text)
    if match:
        step = int(match.group("step"))
        return {"type": "sample", "step": step, "sample_step": step}
    match = SAVE_RE.search(text)
    if match:
        step = int(match.group("step"))
        return {"type": "save", "step": step, "save_step": step}
    if OOM_RE.search(text):
        count = OOM_COUNT_RE.search(text)
        return {
            "type": "oom",
            "consecutive_oom": int(count.group("n")) if count else None,
            "message": text,
        }
    lr_match = LR_RE.search(text)
    loss_match = LOSS_RE.search(text)
    if lr_match or loss_match:
        step = total = None
        step_match = PROGRESS_STEP_RE.search(text)
        if step_match:
            step = int(step_match.group("step"))
            total = int(step_match.group("total"))
        return {
            "type": "progress",
            "step": step,
            "total": total,
            "lr": float(lr_match.group("lr")) if lr_match else None,
            "loss": float(loss_match.group("loss")) if loss_match else None,
        }
    if DONE_RE.search(text):
        return {"type": "done", "message": text}
    return None


# ---------------------------------------------------------------------------
# metrics / run
# ---------------------------------------------------------------------------


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


def _record_sample_step(metrics: dict, step) -> None:
    if step is None:
        return
    steps = metrics.setdefault("sample_steps", [])
    if step not in steps:
        steps.append(step)
        steps.sort()
    metrics["last_sample_step"] = steps[-1] if steps else None
    metrics["sample_groups"] = int(metrics.get("baseline_groups", 0)) + len(steps)


def _apply_event(
    metrics: dict,
    event: dict,
    warnings: list,
    sample_every: int = 0,
    sample_start_step: int = 0,
    disable_sampling: bool = False,
) -> None:
    kind = event.get("type")
    if kind == "baseline_sample":
        metrics["baseline_groups"] = 1
        metrics["sample_groups"] = 1 + len(metrics.get("sample_steps", []))
        metrics["phase"] = "baseline"
    elif kind == "sample":
        _record_sample_step(metrics, event.get("sample_step"))
        metrics["step"] = event.get("step")
        metrics["phase"] = "sample"
    elif kind == "save":
        metrics["saves"].append(event.get("save_step"))
        metrics["last_save_step"] = event.get("save_step")
        metrics["step"] = event.get("step")
        metrics["phase"] = "save"
    elif kind == "progress":
        if event.get("step") is not None:
            metrics["step"] = event.get("step")
        if event.get("total") is not None:
            metrics["total"] = event.get("total")
        if event.get("lr") is not None:
            metrics["lr"] = event.get("lr")
        if event.get("loss") is not None:
            metrics["loss"] = event.get("loss")
        # Scheduled samples do not always print "Sampling at step"; the
        # progress step itself is the reliable signal when sample_every is known.
        step = event.get("step")
        if (
            not disable_sampling
            and sample_every
            and step is not None
            and step >= sample_start_step
            and step % sample_every == 0
        ):
            _record_sample_step(metrics, step)
        metrics["phase"] = "training"
    elif kind == "oom":
        metrics["oom_skips"] = int(metrics.get("oom_skips", 0)) + 1
        if event.get("consecutive_oom") is not None:
            metrics["consecutive_oom"] = event.get("consecutive_oom")
    elif kind == "done":
        metrics["phase"] = "done"


def run(
    job,
    log_path,
    *,
    train_command=None,
    config_path=None,
    stop_file=None,
    metrics_path=None,
    cancel_event=None,
    poll_interval: float = 0.1,
    grace_s: float = 5.0,
    cwd=None,
    env=None,
) -> dict:
    """Write config.yaml, run ``python run.py config.yaml``, parse stdout.

    ``train_command`` is a prefix (for a venv python or a test stub); the YAML
    path is appended to it. Default is ``[sys.executable, run.py, <yaml>]``.
    Soft stop: stop_file/cancel_event -> SIGINT/CTRL_BREAK -> terminate/kill.
    No resume (ai-toolkit has no resume path); a cancelled run is cancelled.
    """
    job = job if isinstance(job, dict) else {}
    params = job.get("params")
    if not isinstance(params, dict):
        raw_params = job.get("params_json")
        if isinstance(raw_params, str):
            try:
                params = json.loads(raw_params)
            except ValueError:
                params = {}
        elif isinstance(raw_params, dict):
            params = raw_params
        else:
            params = job
    if not isinstance(params, dict):
        raise ValueError("job.params must be a dict")
    job_id = str(job.get("id") or job.get("job_id") or "")

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    config_path = (
        Path(config_path)
        if config_path is not None
        else Path(job.get("config_path") or log_path.with_suffix(".yaml"))
    )
    metrics_file = (
        Path(metrics_path)
        if metrics_path is not None
        else log_path.with_suffix(".metrics.json")
    )
    stop_path = Path(stop_file) if stop_file is not None else None

    warnings = []
    config = build_config(params, warnings=warnings)
    write_config(config_path, config)
    plan = sample_plan(
        config["config"]["process"][0]["train"]["steps"],
        config["config"]["process"][0]["sample"]["sample_every"],
        config["config"]["process"][0]["sample"]["sample_start_step"],
        config["config"]["process"][0]["train"].get("skip_first_sample", False),
        config["config"]["process"][0]["train"].get("disable_sampling", False),
    )
    total_steps = int(config["config"]["process"][0]["train"]["steps"])

    if train_command is None:
        python_exe = os.environ.get("MODELSMITH_AITK_PYTHON", sys.executable)
        command = [python_exe, str(AITK_RUN), str(config_path)]
        default_cwd = AITK_ROOT
    else:
        if isinstance(train_command, str):
            command = [train_command]
        else:
            command = [str(part) for part in train_command]
        if not command:
            raise ValueError("train_command must not be empty")
        command = command + [str(config_path)]
        default_cwd = cwd

    metrics = {
        "job_id": job_id,
        "state": "starting",
        "phase": "starting",
        "step": 0,
        "total": total_steps,
        "lr": None,
        "loss": None,
        "sample_groups": 0,
        "baseline_groups": 0,
        "sample_steps": [],
        "last_sample_step": None,
        "saves": [],
        "last_save_step": None,
        "oom_skips": 0,
        "warnings": list(warnings),
        "plan": plan,
        "config_path": str(config_path),
        "events": 0,
        "started_at": _now_utc(),
        "elapsed_s": 0.0,
        "returncode": None,
        "resume": False,
    }
    _write_metrics(metrics_file, metrics)

    with open(log_path, "w", encoding="utf-8", newline="\n") as log:
        log.write("# aitk adapter argv: " + json.dumps(command, ensure_ascii=False) + "\n")
        log.write("# aitk adapter config: " + str(config_path) + "\n")
        for warning in warnings:
            log.write("# WARNING: " + warning + "\n")
        log.write("# aitk adapter started_at: " + metrics["started_at"] + "\n")
        log.flush()

        if stop_path is not None and stop_path.exists():
            metrics.update(
                state="cancelled",
                cancelled=True,
                error="stop file present before start",
                phase="finished",
                elapsed_s=0.0,
            )
            log.write("# aitk adapter: cancelled before spawn (stop file present)\n")
            _write_metrics(metrics_file, metrics)
            return {
                "job_id": job_id,
                "state": "cancelled",
                "cancelled": True,
                "returncode": None,
                "metrics": metrics,
                "events": [],
                "argv": command,
                "config": config,
                "config_path": str(config_path),
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
        hard_sent = False
        fatal_error = None
        returncode = None

        try:
            proc = subprocess.Popen(
                command,
                cwd=str(default_cwd) if default_cwd is not None else None,
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
                target=_reader, name="aitk-adapter-reader", daemon=True
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
                        "# aitk adapter: cancel requested; soft stop sent (%s)\n"
                        % stop_signal
                    )
                    log.flush()
                if stop_sent and not hard_sent and proc.poll() is None and now >= stop_deadline:
                    log.write(
                        "# aitk adapter: soft stop grace %.1fs expired; hard stop\n"
                        % grace_s
                    )
                    log.flush()
                    _hard_stop(proc)
                    hard_sent = True

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
                    _apply_event(
                        metrics,
                        event,
                        warnings,
                        sample_every=plan["sample_every"],
                        sample_start_step=plan["sample_start_step"],
                        disable_sampling=bool(
                            config["config"]["process"][0]["train"].get(
                                "disable_sampling", False
                            )
                        ),
                    )
                    metrics["events"] = len(events)
                    metrics["last_event"] = event.get("type")
                    metrics["elapsed_s"] = round(time.monotonic() - started, 3)
                    _write_metrics(metrics_file, metrics)

            if proc.poll() is None:
                _hard_stop(proc)
            returncode = proc.wait(timeout=10)
        except Exception:
            fatal_error = traceback.format_exc()
            log.write("# aitk adapter exception:\n" + fatal_error + "\n")
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
        metrics["phase"] = "finished"
        metrics["finished_at"] = _now_utc()
        metrics["expected_sample_groups"] = plan["group_count"]
        metrics["sample_groups"] = int(metrics.get("sample_groups", 0))
        _write_metrics(metrics_file, metrics)
        log.write(
            "# aitk adapter finished: state=%s returncode=%s samples=%d/%d "
            "elapsed=%.3fs\n"
            % (
                state,
                returncode,
                metrics["sample_groups"],
                plan["group_count"],
                metrics["elapsed_s"],
            )
        )
        log.flush()

    return {
        "job_id": job_id,
        "state": state,
        "cancelled": bool(stop_sent),
        "returncode": returncode,
        "metrics": metrics,
        "events": events,
        "argv": command,
        "config": config,
        "config_path": str(config_path),
        "log_path": str(log_path),
    }
