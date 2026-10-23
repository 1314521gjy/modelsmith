#!/usr/bin/env python3
r"""Black-box selftest for adapters/aitk_adapter.py (no GPU, no install).

Run from the project root:
    python adapters\selftest_aitk_adapter.py
    python adapters\selftest_aitk_adapter.py --keep
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True
import aitk_adapter as adapter  # noqa: E402

STUB = HERE / "_tmp" / "aitk_stub.py"
PYTHON = sys.executable


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


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:
        return True
    return False


def base_params(work: Path, steps: int = 1200) -> dict:
    data = work / "images"
    data.mkdir(parents=True, exist_ok=True)
    return {
        "name": "aitk-selftest",
        "training_folder": str(work / "out"),
        "folder_path": str(data),
        "model_name_or_path": "black-forest-labs/FLUX.1-dev",
        "arch": "flux",
        "steps": steps,
        "batch_size": 2,
        "lr": 1e-4,
        "gradient_accumulation": 2,
        "linear": 8,
        "linear_alpha": 16,
        "save_every": 500,
        "max_step_saves_to_keep": 3,
        "save_format": "safetensors",
        "sample_every": 100,
        "sample_start_step": 0,
        "sample_steps": 20,
        "guidance_scale": 7,
        "prompts": ["a photo of sks person", "a portrait in the rain"],
        "width": 512,
        "height": 512,
        "num_frames": 1,
        "trigger_word": "sks",
        "caption_ext": "txt",
        "resolution": [512, 768],
        "sampler": "flowmatch",
        "noise_scheduler": "flowmatch",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ai-toolkit adapter selftest")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)

    if not STUB.is_file():
        print("FAIL - stub missing: %s" % STUB)
        return 2

    temp_root = Path(tempfile.mkdtemp(prefix="modelsmith_aitk_selftest_"))
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
    params = base_params(work, steps=1200)
    config = adapter.build_config(params)
    process = config["config"]["process"][0]
    dataset = process["datasets"][0]
    train = process["train"]
    sample = process["sample"]
    network = process["network"]
    model = process["model"]
    save = process["save"]

    # -- config field mapping ----------------------------------------------
    c.check(
        "top-level job/config.name",
        config.get("job") == "extension"
        and config["config"]["name"] == "aitk-selftest",
        "job=%s name=%s" % (config.get("job"), config["config"].get("name")),
    )
    c.check(
        "process/training_folder + datasets[0].folder_path",
        process.get("type") == "sd_trainer"
        and process.get("training_folder") == params["training_folder"]
        and dataset.get("folder_path") == params["folder_path"],
        "type=%s training_folder=%s folder=%s"
        % (process.get("type"), process.get("training_folder"), dataset.get("folder_path")),
    )
    c.check(
        "train steps/batch/lr/gradient_accumulation",
        train.get("steps") == 1200
        and train.get("batch_size") == 2
        and abs(float(train.get("lr")) - 1e-4) < 1e-12
        and train.get("gradient_accumulation") == 2
        and "gradient_accumulation_steps" not in train,
        "train=%s" % json.dumps(train, ensure_ascii=False, sort_keys=True),
    )
    c.check(
        "network linear/linear_alpha",
        network.get("type") == "lora"
        and network.get("linear") == 8
        and abs(float(network.get("linear_alpha")) - 16) < 1e-12,
        "network=%s" % json.dumps(network, ensure_ascii=False),
    )
    c.check(
        "save save_every/keep/save_format",
        save.get("save_every") == 500
        and save.get("max_step_saves_to_keep") == 3
        and save.get("save_format") == "safetensors",
        "save=%s" % json.dumps(save, ensure_ascii=False),
    )
    c.check(
        "sample every/start/steps/guidance/width/height/frames/prompts",
        sample.get("sample_every") == 100
        and sample.get("sample_start_step") == 0
        and sample.get("sample_steps") == 20
        and float(sample.get("guidance_scale")) == 7.0
        and sample.get("width") == 512
        and sample.get("height") == 512
        and sample.get("num_frames") == 1
        and sample.get("prompts") == params["prompts"]
        and sample.get("sampler") == "flowmatch",
        "sample=%s" % json.dumps(sample, ensure_ascii=False, sort_keys=True),
    )
    c.check(
        "trigger_word + model name/arch",
        dataset.get("trigger_word") == "sks"
        and process.get("trigger_word") == "sks"
        and model.get("name_or_path") == "black-forest-labs/FLUX.1-dev"
        and model.get("arch") == "flux",
        "dataset_trigger=%s process_trigger=%s model=%s"
        % (dataset.get("trigger_word"), process.get("trigger_word"), json.dumps(model, ensure_ascii=False)),
    )
    c.check(
        "dataset caption_ext/resolution",
        dataset.get("caption_ext") == ".txt"
        and dataset.get("resolution") == [512, 768],
        "dataset=%s" % json.dumps(dataset, ensure_ascii=False),
    )

    # -- sampling arithmetic ------------------------------------------------
    plan = adapter.sample_plan(1200, 100)
    c.check(
        "sample_plan(1200,100) = baseline + 12 = 13 groups",
        plan["group_count"] == 13
        and plan["baseline_groups"] == 1
        and len(plan["sample_steps"]) == 12
        and plan["sample_steps"][0] == 100
        and plan["sample_steps"][-1] == 1200
        and plan["final_divisible"] is True
        and plan["warning"] is None,
        "plan=%s" % json.dumps(plan, ensure_ascii=False),
    )
    plan_1250 = adapter.sample_plan(1250, 100)
    c.check(
        "sample_plan(1250,100) warns 末步不整除 and last=1200",
        plan_1250["group_count"] == 13
        and plan_1250["last_sample_step"] == 1200
        and "末步不整除" in (plan_1250.get("warning") or "")
        and "1200" in (plan_1250.get("warning") or ""),
        "plan=%s" % json.dumps(plan_1250, ensure_ascii=False),
    )

    # -- hard validation ----------------------------------------------------
    bad = dict(params, gradient_accumulation=2, gradient_accumulation_steps=2)
    c.check(
        "gradient_accumulation vs _steps mutually exclusive",
        _raises(lambda: adapter.build_config(bad)),
        "both=2",
    )
    c.check(
        "sampler must match noise_scheduler",
        _raises(lambda: adapter.build_config(dict(params, sampler="euler", noise_scheduler="ddpm"))),
        "euler vs ddpm",
    )
    c.check(
        "save_format enum rejects bogus",
        _raises(lambda: adapter.build_config(dict(params, save_format="bogus"))),
        "save_format=bogus",
    )
    empty_prompts = adapter.validate_params(dict(params, prompts=[]))
    c.check(
        "empty sample.prompts emits an explicit warning",
        any("prompts" in warning or "采样" in warning for warning in empty_prompts),
        "warnings=%s" % json.dumps(empty_prompts, ensure_ascii=False),
    )
    trigger_warning = adapter.validate_params(
        dict(params, cache_text_embeddings=True)
    )
    c.check(
        "trigger_word + cache_text_embeddings warns",
        any("trigger" in warning for warning in trigger_warning),
        "warnings=%s" % json.dumps(trigger_warning, ensure_ascii=False),
    )

    # -- YAML ---------------------------------------------------------------
    yaml_text = adapter.dump_yaml(config)
    c.check(
        "dump_yaml contains job/config/train/sample/dataset fields",
        ("job: extension" in yaml_text or 'job: "extension"' in yaml_text)
        and 'name: "aitk-selftest"' in yaml_text
        and "steps: 1200" in yaml_text
        and "folder_path:" in yaml_text
        and "prompts:" in yaml_text
        and 'trigger_word: "sks"' in yaml_text
        and "gradient_accumulation_steps" not in yaml_text,
        "yaml_bytes=%d" % len(yaml_text.encode("utf-8")),
    )
    yaml_path = work / "config" / "job.yaml"
    adapter.write_config(yaml_path, config)
    c.check(
        "write_config writes UTF-8 YAML atomically",
        yaml_path.is_file()
        and yaml_path.read_text(encoding="utf-8").startswith("---\n")
        and "steps: 1200" in yaml_path.read_text(encoding="utf-8"),
        "path=%s bytes=%d" % (yaml_path, yaml_path.stat().st_size),
    )

    # -- parser -------------------------------------------------------------
    baseline = adapter.parse_line("Generating baseline samples before training") or {}
    sample_line = adapter.parse_line("Sampling at step 300") or {}
    task_sample = adapter.parse_line("Sample at step 300") or {}
    save_line = adapter.parse_line("Saving at step 1000") or {}
    progress = adapter.parse_line("1/1200 lr: 1.0e-04 loss: 1.234e-01") or {}
    oom = adapter.parse_line("# OOM during training step, skipping batch 1/3 #") or {}
    done = adapter.parse_line("Training complete") or {}
    c.check(
        "parse baseline sample",
        baseline.get("type") == "baseline_sample" and baseline.get("sample_step") == 0,
        "event=%s" % json.dumps(baseline, ensure_ascii=False),
    )
    c.check(
        "parse Sampling at step N and Sample at step N",
        sample_line.get("type") == "sample"
        and sample_line.get("sample_step") == 300
        and task_sample.get("sample_step") == 300,
        "sampling=%s sample=%s"
        % (json.dumps(sample_line, ensure_ascii=False), json.dumps(task_sample, ensure_ascii=False)),
    )
    c.check(
        "parse Saving at step N",
        save_line.get("type") == "save" and save_line.get("save_step") == 1000,
        "event=%s" % json.dumps(save_line, ensure_ascii=False),
    )
    c.check(
        "parse lr/loss postfix with step/total",
        progress.get("type") == "progress"
        and progress.get("step") == 1
        and progress.get("total") == 1200
        and abs(progress.get("lr") - 1e-4) < 1e-12
        and abs(progress.get("loss") - 0.1234) < 1e-9,
        "event=%s" % json.dumps(progress, ensure_ascii=False),
    )
    c.check(
        "parse OOM skip",
        oom.get("type") == "oom" and oom.get("consecutive_oom") == 1,
        "event=%s" % json.dumps(oom, ensure_ascii=False),
    )
    c.check(
        "parse done and unknown lines",
        done.get("type") == "done" and adapter.parse_line("some noise") is None,
        "done=%s unknown=%s" % (done.get("type"), adapter.parse_line("some noise")),
    )

    # -- full lifecycle -----------------------------------------------------
    success_log = work / "logs" / "success.log"
    success_metrics = work / "metrics" / "success.json"
    success_cfg = work / "config" / "success.yaml"
    result = adapter.run(
        {"id": "aitk-success", "params": params},
        success_log,
        train_command=[PYTHON, str(STUB), "--stub-delay", "0.005"],
        config_path=success_cfg,
        metrics_path=success_metrics,
    )
    metrics = json.loads(success_metrics.read_text(encoding="utf-8"))
    log_text = success_log.read_text(encoding="utf-8")
    c.check(
        "stub full run succeeded (rc=0) with config written",
        result.get("state") == "succeeded"
        and result.get("returncode") == 0
        and success_cfg.is_file(),
        "state=%s rc=%s config=%s"
        % (result.get("state"), result.get("returncode"), success_cfg),
    )
    c.check(
        "metrics sample_groups=13/expected=13, step=1200, last_sample=1200",
        metrics.get("sample_groups") == 13
        and metrics.get("expected_sample_groups") == 13
        and metrics.get("step") == 1200
        and metrics.get("total") == 1200
        and metrics.get("last_sample_step") == 1200,
        "metrics=%s" % json.dumps(
            {key: metrics.get(key) for key in ("sample_groups", "expected_sample_groups", "step", "total", "last_sample_step")},
            ensure_ascii=False,
        ),
    )
    c.check(
        "metrics lr/loss and no warnings on divisible run",
        metrics.get("lr") is not None
        and metrics.get("loss") is not None
        and metrics.get("warnings") == [],
        "lr=%s loss=%s warnings=%s"
        % (metrics.get("lr"), metrics.get("loss"), metrics.get("warnings")),
    )
    c.check(
        "log contains baseline/sample/save/done lines",
        all(
            marker in log_text
            for marker in (
                "Generating baseline samples before training",
                "Sampling at step 100",
                "Saving at step 500",
                "Training complete",
            )
        ),
        "log_bytes=%d" % len(log_text.encode("utf-8")),
    )

    # non-divisible steps: 1250 -> warning but still 13 groups
    nodiv_params = dict(params, steps=1250)
    nodiv_log = work / "logs" / "nodiv.log"
    nodiv_metrics = work / "metrics" / "nodiv.json"
    nodiv_cfg = work / "config" / "nodiv.yaml"
    nodiv = adapter.run(
        {"id": "aitk-nodiv", "params": nodiv_params},
        nodiv_log,
        train_command=[PYTHON, str(STUB), "--stub-delay", "0.005"],
        config_path=nodiv_cfg,
        metrics_path=nodiv_metrics,
    )
    nodiv_data = json.loads(nodiv_metrics.read_text(encoding="utf-8"))
    c.check(
        "steps=1250 warns 末步不整除 and keeps 13 groups",
        nodiv.get("state") == "succeeded"
        and nodiv_data.get("sample_groups") == 13
        and any("末步不整除" in warning for warning in (nodiv_data.get("warnings") or []))
        and nodiv_data.get("last_sample_step") == 1200,
        "state=%s groups=%s last=%s warnings=%s"
        % (
            nodiv.get("state"),
            nodiv_data.get("sample_groups"),
            nodiv_data.get("last_sample_step"),
            nodiv_data.get("warnings"),
        ),
    )

    # -- cancellation -------------------------------------------------------
    cancel_log = work / "logs" / "cancel.log"
    cancel_metrics = work / "metrics" / "cancel.json"
    cancel_cfg = work / "config" / "cancel.yaml"
    stop_file = work / "aitk.stop"
    box = {}

    def _worker():
        try:
            box["result"] = adapter.run(
                {"id": "aitk-cancel", "params": params},
                cancel_log,
                train_command=[PYTHON, str(STUB), "--stub-delay", "0.2"],
                config_path=cancel_cfg,
                metrics_path=cancel_metrics,
                stop_file=stop_file,
            )
        except Exception:
            box["error"] = traceback.format_exc()

    thread = threading.Thread(target=_worker, name="aitk-cancel", daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        if cancel_log.is_file():
            try:
                if "Generating baseline samples" in cancel_log.read_text(
                    encoding="utf-8", errors="replace"
                ):
                    break
            except OSError:
                pass
        time.sleep(0.05)
    stop_file.write_text("cancel\n", encoding="utf-8")
    thread.join(timeout=20)
    c.check(
        "cancel thread finished without raising",
        not thread.is_alive() and "result" in box and "error" not in box,
        "alive=%s error=%s" % (thread.is_alive(), box.get("error")),
    )
    cancel_result = box.get("result") or {}
    cancel_text = cancel_log.read_text(encoding="utf-8") if cancel_log.is_file() else ""
    c.check(
        "stop file cancels running ai-toolkit job",
        cancel_result.get("state") == "cancelled"
        and cancel_result.get("cancelled") is True
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
        "cancel metrics persisted as cancelled",
        cancel_json.get("state") == "cancelled"
        and cancel_json.get("cancelled") is True
        and stop_file.is_file(),
        "state=%s cancelled=%s stop_file=%s"
        % (
            cancel_json.get("state"),
            cancel_json.get("cancelled"),
            stop_file.is_file(),
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
