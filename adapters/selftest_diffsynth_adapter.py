#!/usr/bin/env python3
"""Zero-GPU selftest for ``adapters/diffsynth_adapter.py``.

Covers:
  * shard expansion (transformer 2 / text_encoder 4 / vae 1) on the real local
    model when present, otherwise on a synthetic layout with the same names
  * every ``train.py`` flag, empty ``--lora_target_modules``, JSON round-trip
  * missing model / dataset / out / metadata -> ValueError
  * tqdm ``N/M [.. x.xxs/it]`` and ``epoch-N.safetensors`` line parsing
  * ``run()`` success path against a stub (artifact scanned from disk)
  * ``run()`` cancellation via stop file (soft stop then hard stop)

Run with any Python 3.10+ (no torch, no GPU):

    python adapters\\selftest_diffsynth_adapter.py
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import diffsynth_adapter as A  # noqa: E402

REAL_MODEL = Path(r"E:\临时文件夹\Qwen-Image-2.1")
PASS = 0
FAIL = 0
FAILURES = []


def ok(name: str, detail: str = ""):
    global PASS
    PASS += 1
    print("PASS  %s%s" % (name, ("  [" + detail + "]") if detail else ""))


def bad(name: str, detail: str):
    global FAIL
    FAIL += 1
    FAILURES.append("%s: %s" % (name, detail))
    print("FAIL  %s  [%s]" % (name, detail))


def check(name: str, condition: bool, detail: str = ""):
    if condition:
        ok(name, detail)
    else:
        bad(name, detail or "condition false")


def expect_raises(name: str, exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        ok(name, str(exc)[:90])
        return
    except Exception as exc:  # wrong exception type
        bad(name, "raised %s instead of %s: %s" % (type(exc).__name__, exc_type.__name__, exc))
        return
    bad(name, "did not raise %s" % exc_type.__name__)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def make_synthetic_model(root: Path) -> None:
    layout = {
        "transformer": [
            "diffusion_pytorch_model-00001-of-00002.safetensors",
            "diffusion_pytorch_model-00002-of-00002.safetensors",
        ],
        "text_encoder": [
            "model-00001-of-00004.safetensors",
            "model-00002-of-00004.safetensors",
            "model-00003-of-00004.safetensors",
            "model-00004-of-00004.safetensors",
        ],
        "vae": ["diffusion_pytorch_model.safetensors"],
    }
    for sub, names in layout.items():
        folder = root / sub
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            (folder / name).write_bytes(b"\x00" * 8)
    processor = root / "processor"
    processor.mkdir(parents=True, exist_ok=True)
    (processor / "tokenizer.json").write_text("{}", encoding="utf-8")
    return root


def make_dataset(root: Path, rows: int = 1) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    lines = ["image,prompt"] + ["img.png,a test image"] * rows
    (root / "metadata.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (root / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return root


def base_params(model_dir: Path, dataset: Path, out: Path) -> dict:
    return {
        "model": str(model_dir),
        "dataset": str(dataset),
        "out": str(out),
        "lora_rank": 8,
        "num_epochs": 1,
        "learning_rate": 1e-4,
        "height": 512,
        "width": 512,
    }


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_expand(model_dir: Path, source: str):
    paths = A.expand_model_paths(model_dir)
    check("expand type", isinstance(paths, list) and len(paths) == 3, "len=%d" % len(paths))
    check(
        "expand transformer shards = 2",
        isinstance(paths[0], list) and len(paths[0]) == 2,
        "%s" % (paths[0] if isinstance(paths[0], list) else type(paths[0]).__name__),
    )
    check(
        "expand text_encoder shards = 4",
        isinstance(paths[1], list) and len(paths[1]) == 4,
        "%s" % (paths[1] if isinstance(paths[1], list) else type(paths[1]).__name__),
    )
    check(
        "expand vae single -> str",
        isinstance(paths[2], str) and paths[2].endswith("diffusion_pytorch_model.safetensors"),
        str(paths[2]),
    )
    check(
        "expand order by filename",
        paths[0] == sorted(paths[0]) and paths[1] == sorted(paths[1]),
        "transformer[0]=%s" % Path(paths[0][0]).name,
    )
    check("expand source", True, source)


def test_argv(model_dir: Path, dataset: Path, out: Path):
    params = base_params(model_dir, dataset, out)
    argv = A.build_argv(params, str(out))
    joined = " ".join(argv)

    def value_of(flag: str):
        if flag not in argv:
            return None
        index = argv.index(flag)
        return argv[index + 1] if index + 1 < len(argv) else None

    for flag in (
        "--dataset_base_path",
        "--dataset_metadata_path",
        "--model_paths",
        "--processor_path",
        "--output_path",
        "--lora_base_model",
        "--lora_target_modules",
        "--lora_rank",
        "--num_epochs",
        "--learning_rate",
        "--dataset_repeat",
        "--use_gradient_checkpointing",
        "--height",
        "--width",
        "--task",
        "--remove_prefix_in_ckpt",
    ):
        check("argv has %s" % flag, flag in argv)

    check("argv lora_base_model=dit", value_of("--lora_base_model") == "dit", str(value_of("--lora_base_model")))
    target = value_of("--lora_target_modules")
    check("argv lora_target_modules empty", target == "", repr(target))
    check("argv task=sft", value_of("--task") == "sft", str(value_of("--task")))
    check("argv remove_prefix=pipe.dit.", value_of("--remove_prefix_in_ckpt") == "pipe.dit.", str(value_of("--remove_prefix_in_ckpt")))
    check("argv rank=8", value_of("--lora_rank") == "8", str(value_of("--lora_rank")))
    check("argv epochs=1", value_of("--num_epochs") == "1", str(value_of("--num_epochs")))
    check("argv lr=0.0001", value_of("--learning_rate") == "0.0001", str(value_of("--learning_rate")))
    check("argv size 512x512", value_of("--height") == "512" and value_of("--width") == "512", "%s x %s" % (value_of("--height"), value_of("--width")))
    check("argv output_path", Path(value_of("--output_path")) == out.resolve(), str(value_of("--output_path")))
    check("argv processor local", value_of("--processor_path").endswith("/processor"), str(value_of("--processor_path")))
    check("argv metadata default", value_of("--dataset_metadata_path").endswith("/metadata.csv"), str(value_of("--dataset_metadata_path")))

    raw_paths = value_of("--model_paths")
    parsed = json.loads(raw_paths)
    check("argv model_paths JSON round-trip", isinstance(parsed, list) and len(parsed) == 3, "len=%d" % len(parsed))
    check("argv model_paths transformer=2", isinstance(parsed[0], list) and len(parsed[0]) == 2, str(len(parsed[0]) if isinstance(parsed[0], list) else parsed[0]))
    check("argv model_paths te=4", isinstance(parsed[1], list) and len(parsed[1]) == 4, str(len(parsed[1]) if isinstance(parsed[1], list) else parsed[1]))
    check("argv model_paths vae=str", isinstance(parsed[2], str), type(parsed[2]).__name__)
    check("argv forward slashes", "\\\\" not in raw_paths, raw_paths[:60])
    check("argv flags once", argv.count("--use_gradient_checkpointing") == 1)

    # overrides
    params2 = dict(params)
    params2.update(
        {
            "lora_target_modules": "q,k,v",
            "lora_rank": 16,
            "num_epochs": 2,
            "learning_rate": 5e-5,
            "dataset_repeat": 3,
            "height": 256,
            "width": 384,
            "use_gradient_checkpointing": False,
            "initialize_model_on_cpu": False,
            "enable_model_cpu_offload": False,
            "save_steps": 2,
        }
    )
    argv2 = A.build_argv(params2, str(out))

    def v2(flag):
        return argv2[argv2.index(flag) + 1] if flag in argv2 else None

    check("override target modules", v2("--lora_target_modules") == "q,k,v", str(v2("--lora_target_modules")))
    check("override rank=16", v2("--lora_rank") == "16", str(v2("--lora_rank")))
    check("override epochs=2", v2("--num_epochs") == "2", str(v2("--num_epochs")))
    check("override repeat=3", v2("--dataset_repeat") == "3", str(v2("--dataset_repeat")))
    check("override 256x384", v2("--height") == "256" and v2("--width") == "384", "%s x %s" % (v2("--height"), v2("--width")))
    check("override flags removed", "--use_gradient_checkpointing" not in argv2 and "--enable_model_cpu_offload" not in argv2)
    check("override save_steps", v2("--save_steps") == "2", str(v2("--save_steps")))

    # save_steps=0 -> epoch-only saves (no --save_steps); >0 -> flag; <0 -> error
    zero_params = dict(params)
    zero_params["save_steps"] = 0
    argv_zero = A.build_argv(zero_params, str(out))
    check(
        "save_steps=0 omits --save_steps",
        "--save_steps" not in argv_zero,
        json.dumps([x for x in argv_zero if x.startswith("--save")]),
    )
    pos_params = dict(params)
    pos_params["save_steps"] = 200
    argv_pos = A.build_argv(pos_params, str(out))
    check(
        "save_steps=200 passes flag",
        "--save_steps" in argv_pos
        and argv_pos[argv_pos.index("--save_steps") + 1] == "200",
        str(argv_pos[argv_pos.index("--save_steps") + 1] if "--save_steps" in argv_pos else None),
    )
    neg_params = dict(params)
    neg_params["save_steps"] = -1
    expect_raises("save_steps=-1 raises", ValueError, A.build_argv, neg_params, str(out))

    # required-param failures
    missing_model = base_params(model_dir, dataset, out)
    missing_model.pop("model")
    expect_raises("missing model raises", ValueError, A.build_argv, missing_model, str(out))

    missing_dataset = base_params(model_dir, dataset, out)
    missing_dataset.pop("dataset")
    expect_raises("missing dataset raises", ValueError, A.build_argv, missing_dataset, str(out))

    missing_out = base_params(model_dir, dataset, out)
    missing_out.pop("out")
    expect_raises("missing out raises", ValueError, A.build_argv, missing_out, None)

    # a dataset without metadata.csv is auto-prepared (under <out>, never the dataset dir)
    no_meta = Path(dataset).parent / "argv-no-meta"
    no_meta.mkdir(parents=True, exist_ok=True)
    (no_meta / "x.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    argv3 = A.build_argv(
        {"model": str(model_dir), "dataset": str(no_meta), "out": str(out)}, str(out)
    )
    generated_meta = argv3[argv3.index("--dataset_metadata_path") + 1]
    check(
        "missing metadata auto-generated",
        Path(generated_meta).is_file()
        and generated_meta.replace("\\", "/").endswith("/out/dataset_metadata.csv"),
        generated_meta,
    )

    bad_model = base_params(model_dir, dataset, out)
    bad_model["model"] = str(model_dir / "does-not-exist")
    expect_raises("bad model dir raises", ValueError, A.build_argv, bad_model, str(out))


def test_new_arches(dataset: Path, tmp: Path):
    """L6 translation only: FLUX.2-klein-9B single dir + Krea-2 three paths."""
    flux = tmp / "flux2-klein-9b"
    for component in ("transformer", "text_encoder", "vae", "tokenizer"):
        (flux / component).mkdir(parents=True)
    for component in ("transformer", "text_encoder", "vae"):
        (flux / component / "model.safetensors").write_bytes(b"stub")
    flux_argv = A.build_argv(
        {
            "arch": "flux2_klein_9b",
            "model": str(flux),
            "dataset": str(dataset),
            "out": str(tmp / "flux-out"),
        }
    )
    flux_target = flux_argv[flux_argv.index("--lora_target_modules") + 1]
    check(
        "flux2 arch builds argv",
        "--model_paths" in flux_argv and flux_target.count("single_transformer_blocks.") >= 24,
        flux_target[:100],
    )
    check(
        "flux2 target includes single_transformer_blocks.23",
        "single_transformer_blocks.23.attn.to_out" in flux_target,
        flux_target[-80:],
    )
    check(
        "flux2 offload default off (full load, no CPU memory)",
        "--enable_model_cpu_offload" not in flux_argv,
        "",
    )
    flux_off = A.build_argv(
        {
            "arch": "flux2_klein_9b",
            "model": str(flux),
            "dataset": str(dataset),
            "out": str(tmp / "flux-out"),
            "enable_model_cpu_offload": True,
        }
    )
    check(
        "flux2 offload opt-in",
        "--enable_model_cpu_offload" in flux_off,
        "",
    )

    krea = tmp / "krea2"
    (krea / "text_encoder").mkdir(parents=True)
    (krea / "raw.safetensors").write_bytes(b"stub")
    (krea / "text_encoder" / "model.safetensors").write_bytes(b"stub")
    (krea / "vae.safetensors").write_bytes(b"stub")
    krea_params = {
        "arch": "krea2_raw",
        "model": str(krea / "raw.safetensors"),
        "text_encoder": str(krea / "text_encoder"),
        "vae": str(krea / "vae.safetensors"),
        "dataset": str(dataset),
        "out": str(tmp / "krea-out"),
    }
    krea_argv = A.build_argv(krea_params)
    raw_paths = krea_argv[krea_argv.index("--model_paths") + 1]
    parsed = json.loads(raw_paths)
    check("krea2 three-path model_paths", isinstance(parsed, list) and len(parsed) == 3, raw_paths[:120])
    check("krea2 raw is file path", isinstance(parsed[0], str) and parsed[0].endswith("raw.safetensors"), str(parsed[0]))
    check("krea2 TE is shard list", isinstance(parsed[1], list) and len(parsed[1]) == 1, str(parsed[1]))
    check("krea2 vae is file path", isinstance(parsed[2], str) and parsed[2].endswith("vae.safetensors"), str(parsed[2]))
    check(
        "krea2 recipe flags",
        "--find_unused_parameters" in krea_argv
        and "--align_to_opensource_format" in krea_argv,
        "",
    )
    check(
        "krea2 offload default off (full load, no CPU memory)",
        "--enable_model_cpu_offload" not in krea_argv,
        "",
    )
    krea_off_in = dict(krea_params, enable_model_cpu_offload=True)
    check(
        "krea2 offload opt-in",
        "--enable_model_cpu_offload" in A.build_argv(krea_off_in),
        "",
    )
    missing_vae = dict(krea_params, vae=str(krea / "missing.safetensors"))
    expect_raises("krea2 missing vae names path", ValueError, A.build_argv, missing_vae)


def test_parse():
    event = A.parse_line("  Epoch 0: 100%|##########| 4/4 [00:15<00:00,  3.88s/it]")
    check("parse tqdm step", event and event.get("step") == 4 and event.get("total") == 4, json.dumps(event))
    check("parse tqdm sec_per_it", event and abs(event.get("sec_per_it", 0) - 3.88) < 1e-9, json.dumps(event))

    event = A.parse_line("  2/4 [00:06<00:06,  3.30s/it, loss: 0.1234]")
    check("parse loss", event and abs(event.get("loss", 0) - 0.1234) < 1e-9, json.dumps(event))

    event = A.parse_line("Saved LoRA to epoch-0.safetensors")
    check("parse artifact", event and event.get("type") == "artifact" and event.get("file") == "epoch-0.safetensors", json.dumps(event))

    event = A.parse_line("Traceback (most recent call last):")
    check("parse traceback", event and event.get("type") == "error", json.dumps(event))

    event = A.parse_line("[sample] step=2 file=E:/out/samples/step-2/sample_00.png")
    check(
        "parse sample line",
        event and event.get("type") == "sample" and event.get("step") == 2 and event.get("file", "").endswith("sample_00.png"),
        json.dumps(event),
    )
    event = A.parse_line("[sample-error] step=4 RuntimeError: boom")
    check(
        "parse sample-error line",
        event and event.get("type") == "sample_error" and event.get("step") == 4 and "boom" in event.get("message", ""),
        json.dumps(event),
    )

    check("parse non-match -> None", A.parse_line("hello world") is None)
    check("parse empty -> None", A.parse_line("") is None)

    begin = A.parse_line("[ms-sample-begin] step=400")
    check("parse sample-begin marker",
          begin and begin.get("type") == "sample_begin" and begin.get("step") == 400,
          json.dumps(begin))
    end = A.parse_line("[ms-sample-end] step=400")
    check("parse sample-end marker",
          end and end.get("type") == "sample_end" and end.get("step") == 400,
          json.dumps(end))


def test_sample_noise_unit():
    """A sampling tqdm (20) must never overwrite the training step (plan total 1002)."""
    def fresh(plan_total=1002):
        return {
            "step": None, "total": None, "artifacts": [], "sample_events": [],
            "sample_errors": [], "sample_files_total": 0, "in_sample": False,
            "samples_in_flight": 0, "dropped_tqdm": 0, "train_step": None,
            "train_total": None, "phase": "starting", "sampling_enabled": True,
            "steps_plan": {"total_steps": plan_total} if plan_total else {},
        }

    def feed(metrics, line):
        A._apply_event(metrics, A.parse_line(line))

    metrics = fresh()
    feed(metrics, "  399/1002 [01:00<01:30,  1.50s/it, loss: 0.4000]")
    check("noise: training step before sample", metrics["step"] == 399 and metrics["total"] == 1002,
          "%s/%s" % (metrics["step"], metrics["total"]))
    feed(metrics, "[ms-sample-begin] step=400")
    check("noise: begin sets in_sample", metrics.get("in_sample") is True
          and metrics.get("samples_in_flight") == 1, json.dumps(metrics.get("in_sample")))
    feed(metrics, "  5/20 [00:04<00:12,  1.25it/s]")
    check("noise: 5/20 during sample does NOT move step", metrics["step"] == 399,
          str(metrics["step"]))
    feed(metrics, " 20/20 [00:17<00:00,  1.17it/s]")
    check("noise: 20/20 during sample does NOT move step", metrics["step"] == 399,
          "%s/%s" % (metrics["step"], metrics["total"]))
    check("noise: sampling tqdm lines counted as dropped", metrics.get("dropped_tqdm") == 2,
          str(metrics.get("dropped_tqdm")))
    feed(metrics, "[ms-sample-end] step=400")
    check("noise: after end step restored to the marker training step",
          metrics["step"] == 400, str(metrics["step"]))
    check("noise: after end total restored to training total",
          metrics["total"] == 1002, str(metrics["total"]))
    check("noise: in_sample/samples_in_flight cleared",
          metrics.get("in_sample") is False and metrics.get("samples_in_flight") == 0,
          json.dumps({k: metrics.get(k) for k in ("in_sample", "samples_in_flight")}))
    feed(metrics, "  401/1002 [01:01<01:30,  1.50s/it, loss: 0.3900]")
    check("noise: final training progress 401/1002",
          metrics["step"] == 401 and metrics["total"] == 1002,
          "%s/%s" % (metrics["step"], metrics["total"]))

    # negative control: WITHOUT the markers the same tqdm noise wins (the old bug).
    # This proves the marker assertions above are load-bearing.
    old = fresh(plan_total=None)
    feed(old, "  399/1002 [01:00<01:30,  1.50s/it]")
    feed(old, "  5/20 [00:04<00:12,  1.25it/s]")
    feed(old, " 20/20 [00:17<00:00,  1.17it/s]")
    check("noise NEGATIVE CONTROL: without markers step becomes 20/20",
          old["step"] == 20 and old["total"] == 20,
          "%s/%s" % (old["step"], old["total"]))

    # fallback rule: mismatching tiny total is dropped even if a marker was lost
    fallback = fresh()
    feed(fallback, "  399/1002 [01:00<01:30,  1.50s/it]")
    feed(fallback, "  5/20 [00:04<00:12,  1.25it/s]")
    check("noise fallback: total=20 vs plan=1002 dropped, step unchanged",
          fallback["step"] == 399 and fallback["total"] == 1002
          and fallback.get("dropped_tqdm") == 1,
          "step=%s total=%s dropped=%s" % (fallback["step"], fallback["total"],
                                           fallback.get("dropped_tqdm")))


STUB_TEMPLATE = r'''
import os, sys, time
argv = sys.argv[1:]
out = None
if "--output_path" in argv:
    out = argv[argv.index("--output_path") + 1]
mode = os.environ.get("STUB_MODE", "ok")
if mode == "ok":
    print("  1/4 [00:03<00:09,  3.00s/it, loss: 0.5000]", flush=True)
    print("  2/4 [00:07<00:07,  3.50s/it, loss: 0.3000]", flush=True)
    print("  3/4 [00:11<00:03,  3.70s/it, loss: 0.2000]", flush=True)
    print("  4/4 [00:15<00:00,  3.88s/it, loss: 0.1000]", flush=True)
    if out:
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "epoch-0.safetensors"), "wb") as fh:
            fh.write(b"\x00" * 4096)
        if os.environ.get("STUB_SAMPLES") == "1":
            sdir = os.path.join(out, "samples", "step-2")
            os.makedirs(sdir, exist_ok=True)
            with open(os.path.join(sdir, "sample_00.png"), "wb") as fh:
                fh.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    print("done", flush=True)
    sys.exit(0)
if mode == "sample_noise":
    # training progress -> sample (DiffSynth's 20-step denoising tqdm) -> training
    print("  399/1002 [01:00<01:30,  1.50s/it, loss: 0.4000]", flush=True)
    print("[ms-sample-begin] step=400", flush=True)
    print("  5/20 [00:04<00:12,  1.25it/s]", flush=True)
    print(" 20/20 [00:17<00:00,  1.17it/s]", flush=True)
    print("[ms-sample-end] step=400", flush=True)
    print("  401/1002 [01:01<01:30,  1.50s/it, loss: 0.3900]", flush=True)
    if out:
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "epoch-0.safetensors"), "wb") as fh:
            fh.write(b"\x00" * 4096)
    print("done", flush=True)
    sys.exit(0)
print("  0/4 [00:00<?, ?it/s]", flush=True)
time.sleep(60)
'''


QWEN_FLAG_SEQUENCE = [
    "--dataset_base_path",
    "--dataset_metadata_path",
    "--model_paths",
    "--processor_path",
    "--output_path",
    "--lora_base_model",
    "--lora_target_modules",
    "--lora_rank",
    "--num_epochs",
    "--learning_rate",
    "--dataset_repeat",
    "--use_gradient_checkpointing",
    "--height",
    "--width",
    "--task",
    "--remove_prefix_in_ckpt",
]


def make_moss_model(root: Path) -> Path:
    make_synthetic_model(root)
    tokenizer = root / "tokenizer"
    tokenizer.mkdir(parents=True, exist_ok=True)
    (tokenizer / "tokenizer.json").write_text("{}", encoding="utf-8")
    return root


def make_zimage_model(root: Path) -> Path:
    layout = {
        "transformer": [
            "diffusion_pytorch_model-00001-of-00003.safetensors",
            "diffusion_pytorch_model-00002-of-00003.safetensors",
            "diffusion_pytorch_model-00003-of-00003.safetensors",
        ],
        "text_encoder": [
            "model-00001-of-00003.safetensors",
            "model-00002-of-00003.safetensors",
            "model-00003-of-00003.safetensors",
        ],
        "vae": ["diffusion_pytorch_model.safetensors"],
    }
    for sub, names in layout.items():
        folder = root / sub
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            (folder / name).write_bytes(b"\x00" * 8)
    tokenizer = root / "tokenizer"
    tokenizer.mkdir(parents=True, exist_ok=True)
    (tokenizer / "tokenizer.json").write_text("{}", encoding="utf-8")
    return root


def test_arch(model_dir: Path, dataset: Path, out: Path, tmp: Path):
    # --- qwen_image_21 regression: flag sequence byte-for-byte ---
    argv = A.build_argv(base_params(model_dir, dataset, out), str(out))
    flags = [item for item in argv if item.startswith("--")]
    check("qwen flag sequence unchanged", flags == QWEN_FLAG_SEQUENCE, json.dumps(flags))
    check(
        "qwen offload default off (full load, no CPU memory)",
        "--enable_model_cpu_offload" not in argv,
        "",
    )
    check(
        "qwen no --initialize_model_on_cpu by default",
        "--initialize_model_on_cpu" not in argv,
        "",
    )
    qwen_off_opt = dict(base_params(model_dir, dataset, out), enable_model_cpu_offload=True)
    check(
        "qwen offload opt-in",
        "--enable_model_cpu_offload" in A.build_argv(qwen_off_opt, str(out)),
        "",
    )
    qwen_cpu_opt = dict(base_params(model_dir, dataset, out), initialize_model_on_cpu=True)
    check(
        "qwen cpu-init opt-in",
        "--initialize_model_on_cpu" in A.build_argv(qwen_cpu_opt, str(out)),
        "",
    )
    check("qwen default arch", A.resolve_arch({}) == "qwen_image_21", A.resolve_arch({}))
    check(
        "qwen explicit arch",
        A.resolve_arch({"arch": "qwen_image_21"}) == "qwen_image_21",
        "qwen_image_21",
    )

    # --- moss_image branch ---
    moss_model = make_moss_model(tmp / "moss-model")
    moss_params = {
        "arch": "moss_image",
        "model": str(moss_model),
        "dataset": str(dataset),
        "out": str(out),
        "height": 1024,
        "width": 1024,
    }
    moss = A.build_argv(moss_params, str(out))

    def mv(flag):
        return moss[moss.index(flag) + 1] if flag in moss else None

    check("moss lora_base_model=dit", mv("--lora_base_model") == "dit", str(mv("--lora_base_model")))
    check("moss lora_target_modules empty", mv("--lora_target_modules") == "", repr(mv("--lora_target_modules")))
    check("moss remove_prefix=pipe.dit.", mv("--remove_prefix_in_ckpt") == "pipe.dit.", str(mv("--remove_prefix_in_ckpt")))
    check("moss rank default 32", mv("--lora_rank") == "32", str(mv("--lora_rank")))
    check("moss max_pixels 1024^2", mv("--max_pixels") == str(1024 * 1024), str(mv("--max_pixels")))
    check(
        "moss tokenizer local",
        (mv("--tokenizer_path") or "").endswith("/tokenizer"),
        str(mv("--tokenizer_path")),
    )
    check("moss no --initialize_model_on_cpu", "--initialize_model_on_cpu" not in moss, "absent")
    check("moss no --processor_path", "--processor_path" not in moss, "absent")
    check("moss no --enable_model_cpu_offload by default", "--enable_model_cpu_offload" not in moss, "absent")
    off_params = dict(moss_params)
    off_params["enable_model_cpu_offload"] = True
    moss_off = A.build_argv(off_params, str(out))
    check("moss offload opt-in", "--enable_model_cpu_offload" in moss_off, "present")
    check("moss has --use_gradient_checkpointing", "--use_gradient_checkpointing" in moss, "present")
    moss_zero = dict(moss_params)
    moss_zero["save_steps"] = 0
    moss_zero_argv = A.build_argv(moss_zero, str(out))
    check(
        "moss save_steps=0 omits --save_steps",
        "--save_steps" not in moss_zero_argv,
        json.dumps([x for x in moss_zero_argv if x.startswith("--save")]),
    )

    # --- 16 alignment ---
    check("align16 1020 -> 1024", A.align16(1020) == 1024, str(A.align16(1020)))
    check("align16 1000 -> 1008", A.align16(1000) == 1008, str(A.align16(1000)))
    odd = dict(moss_params)
    odd.update({"height": 1020, "width": 1000})
    size = A.size_alignment(odd)
    check(
        "size_alignment records aligned_from",
        size["aligned_from"] == {"width": 1000, "height": 1020},
        json.dumps(size),
    )
    check(
        "size_alignment max_pixels",
        size["max_pixels"] == 1024 * 1008,
        str(size["max_pixels"]),
    )
    odd_argv = A.build_argv(odd, str(out))
    odd_pixels = odd_argv[odd_argv.index("--max_pixels") + 1]
    check("moss argv max_pixels aligned", odd_pixels == str(1024 * 1008), odd_pixels)

    # --- 32 alignment (qwen_image_21 pipeline factor) ---
    check("align32 1020 -> 1024", A.align32(1020) == 1024, str(A.align32(1020)))
    check("align32 1000 -> 1024", A.align32(1000) == 1024, str(A.align32(1000)))
    qwen_1020 = dict(base_params(model_dir, dataset, out))
    qwen_1020.update({"height": 1020, "width": 1020})
    qwen_1020_argv = A.build_argv(qwen_1020, str(out))
    got_hw = (
        qwen_1020_argv[qwen_1020_argv.index("--height") + 1],
        qwen_1020_argv[qwen_1020_argv.index("--width") + 1],
    )
    check("qwen argv 1020 -> 1024 (32 factor)", got_hw == ("1024", "1024"), "%s x %s" % got_hw)
    check(
        "size_alignment factor 32 records aligned_from",
        A.size_alignment(qwen_1020, 32)["aligned_from"] == {"width": 1020, "height": 1020},
        json.dumps(A.size_alignment(qwen_1020, 32)),
    )

    # --- unknown arch must fail loudly ---
    bad = base_params(model_dir, dataset, out)
    bad["arch"] = "sdxl"
    expect_raises("unknown arch raises", ValueError, A.build_argv, bad, str(out))

    # --- z_image branch ---
    z_model = make_zimage_model(tmp / "zimage-model")
    z_params = {
        "arch": "z_image",
        "model": str(z_model),
        "dataset": str(dataset),
        "out": str(out),
        "height": 1024,
        "width": 1024,
    }
    z_argv = A.build_argv(z_params, str(out))

    def zv(flag):
        return z_argv[z_argv.index(flag) + 1] if flag in z_argv else None

    check("z_image lora_base_model=dit", zv("--lora_base_model") == "dit", str(zv("--lora_base_model")))
    check(
        "z_image target modules (7)",
        zv("--lora_target_modules") == "to_q,to_k,to_v,to_out.0,w1,w2,w3",
        str(zv("--lora_target_modules")),
    )
    check("z_image rank default 32", zv("--lora_rank") == "32", str(zv("--lora_rank")))
    check("z_image max_pixels 1024^2", zv("--max_pixels") == str(1024 * 1024), str(zv("--max_pixels")))
    check(
        "z_image tokenizer local",
        (zv("--tokenizer_path") or "").endswith("/tokenizer"),
        str(zv("--tokenizer_path")),
    )
    check("z_image remove_prefix", zv("--remove_prefix_in_ckpt") == "pipe.dit.", str(zv("--remove_prefix_in_ckpt")))
    check("z_image offload default off", "--enable_model_cpu_offload" not in z_argv, "absent (offload clears top-level pad tokens)")
    z_off_params = dict(z_params)
    z_off_params["enable_model_cpu_offload"] = True
    z_off_argv = A.build_argv(z_off_params, str(out))
    check("z_image offload opt-in", "--enable_model_cpu_offload" in z_off_argv, "present")
    check("z_image has gradient checkpointing", "--use_gradient_checkpointing" in z_argv, "present")
    check("z_image no --initialize_model_on_cpu", "--initialize_model_on_cpu" not in z_argv, "absent")
    check("z_image no --processor_path", "--processor_path" not in z_argv, "absent")
    check("z_image has 3 model groups", json.loads(zv("--model_paths"))[2].endswith("diffusion_pytorch_model.safetensors"), "3 groups")

    # mixed dirs: explicit model_paths (transformer from A, TE/VAE from B)
    mixed = {
        "arch": "z_image",
        "model_paths": [
            [str(z_model / "transformer" / ("diffusion_pytorch_model-0000%d-of-00003.safetensors" % i)) for i in (1, 2, 3)],
            [str(z_model / "text_encoder" / ("model-0000%d-of-00003.safetensors" % i)) for i in (1, 2, 3)],
            str(z_model / "vae" / "diffusion_pytorch_model.safetensors"),
        ],
        "dataset": str(dataset),
        "out": str(out),
        "tokenizer_path": str(z_model / "tokenizer"),
    }
    mixed_argv = A.build_argv(mixed, str(out))
    check(
        "z_image mixed model_paths accepted",
        len(json.loads(mixed_argv[mixed_argv.index("--model_paths") + 1])) == 3,
        "3 groups",
    )
    no_tok_z = tmp / "zimage-no-tokenizer"
    layout = make_zimage_model(no_tok_z)
    (layout / "tokenizer" / "tokenizer.json").unlink()
    (layout / "tokenizer").rmdir()
    z_bad = dict(z_params)
    z_bad["model"] = str(no_tok_z)
    expect_raises("z_image missing tokenizer raises", ValueError, A.build_argv, z_bad, str(out))

    # --- moss without local tokenizer must fail (no hub fallback) ---
    no_tok = tmp / "moss-no-tokenizer"
    make_synthetic_model(no_tok)
    bad2 = dict(moss_params)
    bad2["model"] = str(no_tok)
    expect_raises("moss missing tokenizer raises", ValueError, A.build_argv, bad2, str(out))


MOSS_FLAGS = {
    "--dataset_base_path",
    "--dataset_metadata_path",
    "--model_paths",
    "--output_path",
    "--tokenizer_path",
    "--lora_base_model",
    "--lora_target_modules",
    "--lora_rank",
    "--num_epochs",
    "--learning_rate",
    "--dataset_repeat",
    "--max_pixels",
    "--use_gradient_checkpointing",
    "--task",
    "--remove_prefix_in_ckpt",
}


def test_launch_command(model_dir: Path, dataset: Path, tmp: Path):
    """Regression: arch (not the caller) selects train.py.

    Caught a real core-integration failure: core handed in its hard-coded qwen
    train.py for a moss_image job, so the moss argv ran the qwen parser and
    died with ``unrecognized arguments: --tokenizer_path``.
    """
    out = tmp / "launch-out"
    moss_model = make_moss_model(tmp / "launch-moss-model")
    z_model = make_zimage_model(tmp / "launch-zimage-model")

    fixtures = {
        "qwen_image_21": (model_dir, "qwen_image_21"),
        "moss_image": (moss_model, "moss_image"),
        "z_image": (z_model, "z_image"),
    }
    for arch, (m_dir, marker) in fixtures.items():
        params = {
            "arch": arch,
            "model": str(m_dir),
            "dataset": str(dataset),
            "out": str(out),
            "height": 512,
            "width": 512,
        }
        if arch == "qwen_image_21":
            params.pop("arch")  # default arch path
        result = A.build_launch_command(
            params,
            out,
            python="PY",
            repo=str(A.DIFFSYNTH_ROOT),
            sample_cfg_path=str(tmp / "cfg.json"),
        )
        command = result["command"]
        check(
            "%s launch uses train.py[%s]" % (arch, marker),
            str(result["train_py"]).replace("\\", "/").find("/%s/" % marker) >= 0,
            str(result["train_py"]),
        )
        check(
            "%s argv[4] is arch train.py" % arch,
            command[4] == str(result["train_py"]),
            command[4],
        )
        check("%s ignored_train_py None" % arch, result["ignored_train_py"] is None, str(result["ignored_train_py"]))

    # negative control: a stale qwen train_py handed in for moss must be ignored
    params = {
        "arch": "moss_image",
        "model": str(moss_model),
        "dataset": str(dataset),
        "out": str(out),
        "height": 512,
        "width": 512,
    }
    result = A.build_launch_command(
        params,
        out,
        python="PY",
        repo=str(A.DIFFSYNTH_ROOT),
        train_py=str(A.TRAIN_PY_QWEN),  # simulates the core bug
        sample_cfg_path=str(tmp / "cfg.json"),
    )
    check(
        "moss ignores qwen train_py (uses moss)",
        "moss_image" in str(result["train_py"]).replace("\\", "/"),
        str(result["train_py"]),
    )
    check(
        "moss reports ignored qwen train_py",
        bool(result["ignored_train_py"]) and "qwen_image_21" in result["ignored_train_py"],
        str(result["ignored_train_py"]),
    )
    check(
        "train_py_matches_arch negative control",
        A.train_py_matches_arch("moss_image", A.TRAIN_PY_QWEN) is False,
        "False",
    )
    check(
        "train_py_matches_arch positive",
        A.train_py_matches_arch("qwen_image_21", str(A.TRAIN_PY_QWEN)) is True,
        "True",
    )
    # matching explicit train_py is honoured (no warning)
    result = A.build_launch_command(
        {"arch": "moss_image", "model": str(moss_model), "dataset": str(dataset), "out": str(out), "height": 512, "width": 512},
        out,
        python="PY",
        repo=str(A.DIFFSYNTH_ROOT),
        train_py=str(A.TRAIN_PY_MOSS),
        sample_cfg_path=str(tmp / "cfg.json"),
    )
    check("matching train_py honoured", result["ignored_train_py"] is None and "moss_image" in result["train_py"], str(result["train_py"]))

    # argv field-set regression per arch
    qwen_argv = A.build_argv(base_params(model_dir, dataset, out), str(out))
    moss_argv = A.build_argv(params, str(out))
    z_argv = A.build_argv(
        {"arch": "z_image", "model": str(z_model), "dataset": str(dataset), "out": str(out), "height": 512, "width": 512},
        str(out),
    )
    check(
        "qwen flag set unchanged",
        {x for x in qwen_argv if x.startswith("--")} == set(QWEN_FLAG_SEQUENCE),
        json.dumps(sorted({x for x in qwen_argv if x.startswith("--")})),
    )
    check(
        "moss flag set",
        {x for x in moss_argv if x.startswith("--")} == MOSS_FLAGS,
        json.dumps(sorted({x for x in moss_argv if x.startswith("--")})),
    )
    check(
        "z_image flag set",
        {x for x in z_argv if x.startswith("--")} == MOSS_FLAGS,
        json.dumps(sorted({x for x in z_argv if x.startswith("--")})),
    )


def test_metadata_autogen(tmp: Path):
    """Users point --dataset at a folder of images; metadata.csv is generated."""
    out = tmp / "meta-out"
    ds = tmp / "meta-auto-ds"
    ds.mkdir(parents=True, exist_ok=True)
    for index in range(4):
        (ds / ("img_%02d.png" % index)).write_bytes(b"\x89PNG\r\n\x1a\n")
    (ds / "img_00.txt").write_text("a cat on a mat\n", encoding="utf-8")
    (ds / "img_01.txt").write_text("a dog on a log\n", encoding="utf-8")

    info = A.prepare_dataset_metadata({"dataset": str(ds)}, str(out))
    check("metadata autogen flag", info["generated"] is True, str(info.get("generated")))
    check("metadata autogen n=4", info["n"] == 4, str(info.get("n")))
    check(
        "metadata autogen txt/default split",
        info["from_txt"] == 2 and info["from_default"] == 2,
        "txt=%s default=%s" % (info.get("from_txt"), info.get("from_default")),
    )
    generated = Path(info["path"])
    check("metadata generated under out", generated == out / "dataset_metadata.csv", str(generated))
    rows = list(csv.DictReader(open(generated, encoding="utf-8")))
    check("metadata rows=4", len(rows) == 4, str(len(rows)))
    prompts = {Path(row["image"]).name: row["prompt"] for row in rows}
    check("metadata txt caption wins", prompts.get("img_00.png") == "a cat on a mat", prompts.get("img_00.png"))
    check(
        "metadata default caption fallback",
        prompts.get("img_02.png") == "a photo of img_02",
        prompts.get("img_02.png"),
    )
    check(
        "metadata image paths absolute",
        all(Path(row["image"]).is_absolute() for row in rows),
        rows[0]["image"],
    )

    # caption_ext: a configurable suffix (".caption") must be matched, not ".txt"
    cap_out = tmp / "meta-caption-out"
    cap_ds = tmp / "meta-caption-ds"
    cap_ds.mkdir(parents=True, exist_ok=True)
    (cap_ds / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (cap_ds / "a.caption").write_text("a caption file\n", encoding="utf-8")
    cap_info = A.prepare_dataset_metadata(
        {"dataset": str(cap_ds), "caption_ext": ".caption"}, str(cap_out)
    )
    check(
        "caption_ext .caption matched",
        cap_info["from_txt"] == 1 and cap_info["from_default"] == 0,
        "txt=%s default=%s" % (cap_info.get("from_txt"), cap_info.get("from_default")),
    )
    check(
        "caption_ext echoed in metadata info",
        cap_info.get("caption_ext") == ".caption",
        str(cap_info.get("caption_ext")),
    )
    cap_rows = list(csv.DictReader(open(cap_info["path"], encoding="utf-8")))
    check("caption_ext caption text wins", cap_rows[0]["prompt"] == "a caption file", cap_rows[0]["prompt"])
    check(
        "caption_coverage clean dataset",
        A.caption_coverage(cap_info)["no_captions"] is False,
        json.dumps(A.caption_coverage(cap_info)),
    )

    # same shape but only ".txt" exists -> ".caption" mode must fall back to default
    txt_ds = tmp / "meta-caption-txtonly"
    txt_ds.mkdir(parents=True, exist_ok=True)
    (txt_ds / "b.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (txt_ds / "b.txt").write_text("should be ignored\n", encoding="utf-8")
    txt_info = A.prepare_dataset_metadata(
        {"dataset": str(txt_ds), "caption_ext": "caption"},
        str(tmp / "meta-caption-txtonly-out"),
    )
    check(
        "caption_ext=.caption ignores .txt (falls back to default)",
        txt_info["from_txt"] == 0 and txt_info["from_default"] == 1,
        "txt=%s default=%s" % (txt_info.get("from_txt"), txt_info.get("from_default")),
    )
    check(
        "caption_ext without leading dot normalised",
        txt_info.get("caption_ext") == ".caption",
        str(txt_info.get("caption_ext")),
    )
    cov = A.caption_coverage(txt_info)
    check("caption_coverage no_captions flag", cov["no_captions"] is True, json.dumps(cov))
    check("caption_coverage no_captions_images=1", cov["no_captions_images"] == 1, json.dumps(cov))
    check(
        "caption_coverage warning names the ext",
        "'%s'" % txt_info.get("caption_ext") in (cov.get("warning") or ""),
        str(cov.get("warning")),
    )
    expect_raises(
        "caption_ext with space rejected",
        ValueError,
        A.prepare_dataset_metadata,
        {"dataset": str(cap_ds), "caption_ext": ".bad ext"},
        str(cap_out),
    )

    # existing metadata.csv in the dataset dir is used as-is (never overwritten)
    ds2 = tmp / "meta-existing-ds"
    ds2.mkdir(parents=True, exist_ok=True)
    (ds2 / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    existing = ds2 / "metadata.csv"
    existing.write_text("image,prompt\na.png,custom caption\n", encoding="utf-8")
    before = existing.read_bytes()
    info2 = A.prepare_dataset_metadata({"dataset": str(ds2)}, str(out))
    check(
        "existing metadata reused (not overwritten)",
        info2["generated"] is False and Path(info2["path"]) == existing and existing.read_bytes() == before,
        str(info2["path"]),
    )

    # missing columns must fail loudly with the column names
    ds3 = tmp / "meta-bad-cols"
    ds3.mkdir(parents=True, exist_ok=True)
    (ds3 / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (ds3 / "metadata.csv").write_text("file,text\na.png,x\n", encoding="utf-8")
    expect_raises(
        "metadata missing columns raises",
        ValueError,
        A.prepare_dataset_metadata,
        {"dataset": str(ds3)},
        str(out),
    )

    # empty dataset must fail with the documented message
    ds4 = tmp / "meta-empty"
    ds4.mkdir(parents=True, exist_ok=True)
    try:
        A.prepare_dataset_metadata({"dataset": str(ds4)}, str(out))
    except ValueError as exc:
        check("empty dataset message", "no images under" in str(exc), str(exc))
    else:
        bad("empty dataset message", "did not raise")

    # BOM: Windows editors write UTF-8 with BOM; it must never leak into prompts
    bom_out = tmp / "meta-bom-out"
    bom_ds = tmp / "meta-bom-ds"
    bom_ds.mkdir(parents=True, exist_ok=True)
    (bom_ds / "b1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (bom_ds / "b1.txt").write_bytes(b"\xef\xbb\xbfmystyle subject b1\n")
    (bom_ds / "b2.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    bom_info = A.prepare_dataset_metadata(
        {"dataset": str(bom_ds), "dataset_default_caption": "  a photo of {image}  "},
        str(bom_out),
    )
    bom_rows = {
        Path(row["image"]).name: row["prompt"]
        for row in csv.DictReader(open(bom_info["path"], encoding="utf-8"))
    }
    b1 = bom_rows.get("b1.png", "")
    b2 = bom_rows.get("b2.png", "")
    check("BOM caption stripped", b1 == "mystyle subject b1", repr(b1))
    check("BOM caption first char", bool(b1) and not b1.startswith("\ufeff"), repr(b1[:1]))
    check("BOM template stripped", b2 == "a photo of b2", repr(b2))
    check("BOM template first char", bool(b2) and not b2.startswith("\ufeff"), repr(b2[:1]))
    raw_generated = Path(bom_info["path"]).read_bytes()
    check("generated CSV has no BOM", not raw_generated.startswith(b"\xef\xbb\xbf"), raw_generated[:3].hex())

    # existing metadata.csv written with BOM must still be readable
    bom_existing_ds = tmp / "meta-bom-existing"
    bom_existing_ds.mkdir(parents=True, exist_ok=True)
    (bom_existing_ds / "c1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    bom_csv = bom_existing_ds / "metadata.csv"
    bom_csv.write_bytes(b"\xef\xbb\xbfimage,prompt\nc1.png,custom\n")
    info_bom = A.prepare_dataset_metadata({"dataset": str(bom_existing_ds)}, str(bom_out))
    check(
        "BOM existing metadata reused",
        info_bom["generated"] is False and Path(info_bom["path"]) == bom_csv,
        str(info_bom["path"]),
    )
    check(
        "BOM existing metadata columns",
        A._metadata_columns(bom_csv) == ["image", "prompt"],
        str(A._metadata_columns(bom_csv)),
    )

    # build_argv on a caption-only dataset uses the generated file and keeps argv valid
    model = make_synthetic_model(tmp / "meta-layout-model")
    qwen_argv = A.build_argv(
        {"model": str(model), "dataset": str(ds), "out": str(out)},
        str(out),
    )
    metadata_arg = qwen_argv[qwen_argv.index("--dataset_metadata_path") + 1]
    check(
        "argv uses generated metadata",
        metadata_arg.replace("\\", "/").endswith("/meta-out/dataset_metadata.csv"),
        metadata_arg,
    )


def test_steps_conversion(tmp: Path, model_dir: Path):
    """params.steps -> num_epochs=1 + dataset_repeat (DiffSynth has no total steps)."""
    ds4 = make_dataset(tmp / "steps-4", rows=4)
    ds6 = make_dataset(tmp / "steps-6", rows=6)

    plan = A.steps_plan({"steps": 300}, str(ds4 / "metadata.csv"))
    check(
        "steps 300 / 4 rows -> repeat=75 effective=300",
        plan["steps_from_repeat"] == 75
        and plan["effective_steps"] == 300
        and plan["epochs"] == 1
        and plan["requested_steps"] == 300
        and plan["overshoot"] is False,
        json.dumps(plan),
    )
    plan = A.steps_plan({"steps": 4}, str(ds4 / "metadata.csv"))
    check(
        "steps 4 / 4 rows -> repeat=1 effective=4",
        plan["steps_from_repeat"] == 1 and plan["effective_steps"] == 4,
        json.dumps(plan),
    )
    plan = A.steps_plan({"steps": 300}, str(ds6 / "metadata.csv"))
    check(
        "steps 300 / 6 rows -> repeat=50 effective=300",
        plan["steps_from_repeat"] == 50 and plan["effective_steps"] == 300,
        json.dumps(plan),
    )
    warnings = []
    plan = A.steps_plan({"steps": 5}, str(ds6 / "metadata.csv"), warnings)
    check(
        "steps 5 / 6 rows -> repeat=1 effective=6 overshoot",
        plan["steps_from_repeat"] == 1
        and plan["effective_steps"] == 6
        and plan["overshoot"] is True,
        json.dumps(plan),
    )
    check(
        "steps overshoot warning text",
        warnings == ["steps_overshoot: requested=5 effective=6"],
        str(warnings),
    )
    plan = A.steps_plan({"dataset_repeat": 3, "num_epochs": 2}, str(ds4 / "metadata.csv"))
    check(
        "no steps -> original epochs/repeat, no steps fields",
        plan["requested_steps"] is None
        and plan["effective_steps"] is None
        and plan["steps_from_repeat"] is None
        and plan["epochs"] == 2
        and plan["repeat"] == 3,
        json.dumps(plan),
    )

    argv = A.build_argv(
        {"model": str(model_dir), "dataset": str(ds4), "out": str(tmp / "steps-out"), "steps": 300,
         "height": 512, "width": 512},
        str(tmp / "steps-out"),
    )
    check(
        "argv steps=300 -> epochs=1 repeat=75",
        argv[argv.index("--num_epochs") + 1] == "1"
        and argv[argv.index("--dataset_repeat") + 1] == "75",
        "epochs=%s repeat=%s"
        % (argv[argv.index("--num_epochs") + 1], argv[argv.index("--dataset_repeat") + 1]),
    )


def test_sampling(dataset: Path, tmp: Path):
    cfg = A.sample_config({})
    check("sample default requested_every=200", cfg["requested_every"] == 200, str(cfg["requested_every"]))
    cfg = A.sample_config({"sample_prompts": ["x"]})
    check("sample default every=200 with prompts", cfg["every"] == 200, str(cfg["every"]))
    check("sample default steps=20", cfg["steps"] == 20, str(cfg["steps"]))
    check("sample default guidance=1.0", abs(cfg["guidance"] - 1.0) < 1e-9, str(cfg["guidance"]))
    check("sample default seed=42", cfg["seed"] == 42, str(cfg["seed"]))
    check(
        "sample default 512x512",
        cfg["width"] == 512 and cfg["height"] == 512,
        "%sx%s" % (cfg["width"], cfg["height"]),
    )

    warnings = []
    cfg = A.sample_config({"sample_every": 0, "sample_prompts": ["x"]}, warnings)
    check("sample_every=0 disables", cfg["every"] == 0 and not warnings, str(cfg["every"]))

    warnings = []
    cfg = A.sample_config({"sample_every": 200}, warnings)
    check(
        "empty prompts + every>0 -> warning + disabled",
        cfg["every"] == 0 and len(warnings) == 1,
        str(warnings),
    )

    warnings = []
    cfg = A.sample_config(
        {
            "sample_every": 2,
            "sample_prompts": ["a red apple", "a blue cup"],
            "sample_steps": 4,
            "sample_guidance": 3.0,
            "sample_seed": 7,
            "sample_width": 256,
            "sample_height": 320,
        },
        warnings,
    )
    check(
        "sample overrides",
        cfg["every"] == 2
        and cfg["prompts"] == ["a red apple", "a blue cup"]
        and cfg["steps"] == 4
        and abs(cfg["guidance"] - 3.0) < 1e-9
        and cfg["seed"] == 7
        and cfg["width"] == 256
        and cfg["height"] == 320
        and not warnings,
        json.dumps({k: cfg[k] for k in ("every", "steps", "guidance", "seed", "width", "height")}),
    )
    expect_raises(
        "sample_prompts must be a list",
        ValueError,
        A.sample_config,
        {"sample_every": 2, "sample_prompts": "not-a-list"},
    )

    cfg = A.sample_config({"sample_prompts": ["x"]})
    check("python_paths default empty", cfg["python_paths"] == [], str(cfg["python_paths"]))
    cfg = A.sample_config({"sample_prompts": ["x"], "python_paths": [str(tmp)]})
    check("python_paths accepted", cfg["python_paths"] == [str(tmp)], str(cfg["python_paths"]))
    expect_raises(
        "python_paths bad dir raises",
        ValueError,
        A.sample_config,
        {"sample_prompts": ["x"], "python_paths": [str(tmp / "nope")]},
    )

    est = A.steps_estimate({"num_epochs": 2, "dataset_repeat": 1}, str(dataset / "metadata.csv"))
    check(
        "steps_estimate rows/repeat/epochs",
        est["dataset_rows"] == 1 and est["total_steps"] == 2,
        json.dumps(est),
    )
    four = make_dataset(tmp / "dataset4", rows=4)
    est = A.steps_estimate({"num_epochs": 1, "dataset_repeat": 2}, str(four / "metadata.csv"))
    check("steps_estimate 4 rows x repeat 2", est["total_steps"] == 8, json.dumps(est))
    check(
        "steps_estimate source documented",
        "dataset_rows" in est and est.get("source"),
        str(est.get("source")),
    )


def test_run(train_command, tmp: Path, model_dir: Path, dataset: Path):
    # success path
    out = tmp / "run-ok"
    log = tmp / "run-ok.log"
    job = {"id": "job-ok", "params": base_params(model_dir, dataset, out)}
    os.environ["STUB_MODE"] = "ok"
    result = A.run(job, log, train_command=train_command, metrics_path=tmp / "run-ok.metrics.json")
    os.environ.pop("STUB_MODE", None)
    check("run success state", result["state"] == "succeeded", "%s rc=%s" % (result["state"], result["returncode"]))
    check("run success step=4/4", result["metrics"].get("step") == 4 and result["metrics"].get("total") == 4, "step=%s total=%s" % (result["metrics"].get("step"), result["metrics"].get("total")))
    check("run success sec_per_it", abs(result["metrics"].get("sec_per_it", 0) - 3.88) < 1e-9, str(result["metrics"].get("sec_per_it")))
    check("run success loss", abs(result["metrics"].get("loss", 0) - 0.1) < 1e-9, str(result["metrics"].get("loss")))
    artifacts = [item["file"] for item in result["metrics"].get("artifact_files", [])]
    check("run success artifact scanned", "epoch-0.safetensors" in artifacts, str(artifacts))
    check("run success log written", log.is_file() and log.stat().st_size > 0, "%s B" % (log.stat().st_size if log.is_file() else -1))
    check(
        "run metrics arch default",
        result["metrics"].get("arch") == "qwen_image_21",
        str(result["metrics"].get("arch")),
    )
    check(
        "run metrics size_aligned_from None for 512",
        result["metrics"].get("size_aligned_from") is None,
        str(result["metrics"].get("size_aligned_from")),
    )
    check(
        "run default sampling disabled (no prompts)",
        result["metrics"].get("sampling_enabled") is False
        and result["metrics"].get("sample_every") == 0
        and len(result["metrics"].get("warnings", [])) == 1,
        str(result["metrics"].get("warnings")),
    )

    # sampling path: cfg file on disk + samples scanned into metrics
    out_s = tmp / "run-sampling"
    log_s = tmp / "run-sampling.log"
    params_s = base_params(model_dir, dataset, out_s)
    params_s.update(
        {
            "sample_every": 2,
            "sample_prompts": ["a red apple on a wooden table, studio light"],
            "sample_steps": 4,
            "sample_guidance": 1.0,
            "sample_seed": 7,
            "sample_width": 256,
            "sample_height": 256,
        }
    )
    os.environ["STUB_MODE"] = "ok"
    os.environ["STUB_SAMPLES"] = "1"
    result = A.run(
        {"id": "job-sampling", "params": params_s},
        log_s,
        train_command=train_command,
        metrics_path=tmp / "run-sampling.metrics.json",
    )
    os.environ.pop("STUB_MODE", None)
    os.environ.pop("STUB_SAMPLES", None)
    cfg_path = Path(params_s["out"]) / ".ms_sample.json"
    check("run wrote .ms_sample.json", cfg_path.is_file(), str(cfg_path))
    if cfg_path.is_file():
        written = json.loads(cfg_path.read_text(encoding="utf-8"))
        check(
            "run sample cfg every/prompts",
            written.get("every") == 2 and len(written.get("prompts", [])) == 1,
            json.dumps(written),
        )
    check("run sampling_enabled", result["metrics"].get("sampling_enabled") is True)
    sample_events = result["metrics"].get("sample_events", [])
    check(
        "run sample_events step=2 with files",
        any(item["step"] == 2 and item["files"] for item in sample_events),
        json.dumps(sample_events),
    )
    check(
        "run sample_events has lora_path key",
        bool(sample_events) and all("lora_path" in item for item in sample_events),
        json.dumps(sample_events),
    )
    check(
        "run sample_files_total",
        result["metrics"].get("sample_files_total", 0) >= 1,
        str(result["metrics"].get("sample_files_total")),
    )
    check(
        "run steps_estimate in metrics",
        result["metrics"].get("steps_estimate", {}).get("total_steps") == 1,
        json.dumps(result["metrics"].get("steps_estimate")),
    )

    # steps conversion through run() (metrics + spawn argv)
    ds4 = make_dataset(tmp / "run-steps-ds", rows=4)
    out_steps = tmp / "run-steps-out"
    steps_params = base_params(model_dir, ds4, out_steps)
    steps_params["steps"] = 300
    os.environ["STUB_MODE"] = "ok"
    result_steps = A.run(
        {"id": "job-steps", "params": steps_params},
        tmp / "run-steps.log",
        train_command=train_command,
        metrics_path=tmp / "run-steps.metrics.json",
    )
    os.environ.pop("STUB_MODE", None)
    check(
        "run requested_steps=300",
        result_steps["metrics"].get("requested_steps") == 300,
        str(result_steps["metrics"].get("requested_steps")),
    )
    check(
        "run effective_steps=300",
        result_steps["metrics"].get("effective_steps") == 300,
        str(result_steps["metrics"].get("effective_steps")),
    )
    check(
        "run steps_from_repeat=75",
        result_steps["metrics"].get("steps_from_repeat") == 75,
        str(result_steps["metrics"].get("steps_from_repeat")),
    )
    argv_steps = result_steps["metrics"].get("argv", [])
    check(
        "run argv epochs=1 repeat=75",
        "--num_epochs" in argv_steps
        and argv_steps[argv_steps.index("--num_epochs") + 1] == "1"
        and argv_steps[argv_steps.index("--dataset_repeat") + 1] == "75",
        "epochs=%s repeat=%s"
        % (
            argv_steps[argv_steps.index("--num_epochs") + 1] if "--num_epochs" in argv_steps else None,
            argv_steps[argv_steps.index("--dataset_repeat") + 1] if "--dataset_repeat" in argv_steps else None,
        ),
    )

    # cancellation path
    out2 = tmp / "run-cancel"
    log2 = tmp / "run-cancel.log"
    stop = tmp / "run-cancel.stop"
    job2 = {"id": "job-cancel", "params": base_params(model_dir, dataset, out2)}
    os.environ["STUB_MODE"] = "sleep"

    def _stop_later():
        time.sleep(1.0)
        stop.write_text("stop", encoding="utf-8")

    timer = threading.Thread(target=_stop_later, daemon=True)
    timer.start()
    started = time.monotonic()
    result = A.run(
        job2,
        log2,
        train_command=train_command,
        stop_file=stop,
        metrics_path=tmp / "run-cancel.metrics.json",
        grace_s=2.0,
    )
    elapsed = time.monotonic() - started
    os.environ.pop("STUB_MODE", None)
    check("run cancel state", result["state"] == "cancelled", "%s rc=%s" % (result["state"], result["returncode"]))
    check("run cancel prompt", elapsed < 20.0, "%.1fs" % elapsed)
    check("run cancel soft signal", bool(result["metrics"].get("soft_stop_signal")), str(result["metrics"].get("soft_stop_signal")))

    # missing out raises before spawn
    expect_raises(
        "run missing out raises",
        ValueError,
        A.run,
        {"id": "x", "params": {"model": str(model_dir), "dataset": str(dataset)}},
        tmp / "run-bad.log",
        train_command=train_command,
    )


def test_caption_metrics_and_artifact_freshness(train_command, tmp: Path, model_dir: Path):
    """P0: no-captions must reach metrics; stale/empty/wrong-ext files must not be artifacts."""
    # --- 1) no-captions metrics through run() (user: images present, 0 captions)
    ds = tmp / "nocap-ds"
    ds.mkdir(parents=True, exist_ok=True)
    for index in range(2):
        (ds / ("n%d.png" % index)).write_bytes(b"\x89PNG\r\n\x1a\n")
    out = tmp / "nocap-out"
    out.mkdir(parents=True, exist_ok=True)
    params = base_params(model_dir, ds, out)
    os.environ["STUB_MODE"] = "ok"
    result = A.run(
        {"id": "job-nocap", "params": params},
        tmp / "nocap.log",
        train_command=train_command,
        metrics_path=tmp / "nocap.metrics.json",
    )
    os.environ.pop("STUB_MODE", None)
    metrics = result["metrics"]
    meta = metrics.get("dataset_metadata") or {}
    check(
        "run no_captions=true",
        metrics.get("no_captions") is True,
        json.dumps({k: metrics.get(k) for k in ("no_captions", "no_captions_images")}),
    )
    check(
        "run no_captions_images=2",
        metrics.get("no_captions_images") == 2,
        str(metrics.get("no_captions_images")),
    )
    check(
        "run dataset_metadata keeps from_txt/from_default",
        meta.get("from_txt") == 0 and meta.get("from_default") == 2,
        json.dumps({k: meta.get(k) for k in ("from_txt", "from_default")}),
    )
    check(
        "run dataset_metadata carries no_captions",
        meta.get("no_captions") is True and meta.get("no_captions_images") == 2,
        json.dumps({k: meta.get(k) for k in ("no_captions", "no_captions_images")}),
    )
    check(
        "run warnings list carries no_captions",
        any("no_captions" in str(w) for w in (metrics.get("warnings") or [])),
        json.dumps(metrics.get("warnings")),
    )
    log_text = (tmp / "nocap.log").read_text(encoding="utf-8", errors="replace")
    check(
        "run log still carries the caption warning",
        "WARNING: no_captions" in log_text,
        log_text[-160:].replace("\n", " | "),
    )

    # --- 2) artifact scan filters (negative control: stale file pre-placed)
    probe = tmp / "scan-probe"
    probe.mkdir(parents=True, exist_ok=True)
    past = time.time() - 30 * 24 * 3600
    stale = probe / "epoch-9.safetensors"
    stale.write_bytes(b"\x00" * 64)
    os.utime(stale, (past, past))
    (probe / "epoch-8.safetensors").write_bytes(b"")
    (probe / "notes.txt").write_text("not an artifact", encoding="utf-8")
    (probe / "epoch-7.pt").write_bytes(b"\x00" * 32)
    (probe / "epoch-6.ckpt").write_bytes(b"\x00" * 32)
    since = time.time() - 5
    scanned = [item["file"] for item in A._scan_artifacts(probe, since=since)]
    check(
        "scan artifact: stale safetensors excluded (negative control)",
        "epoch-9.safetensors" not in scanned,
        str(scanned),
    )
    check("scan artifact: zero-byte excluded", "epoch-8.safetensors" not in scanned, str(scanned))
    check("scan artifact: .txt excluded", "notes.txt" not in scanned, str(scanned))
    check("scan artifact: fresh .pt included", "epoch-7.pt" in scanned, str(scanned))
    check("scan artifact: fresh .ckpt included", "epoch-6.ckpt" in scanned, str(scanned))

    # --- 3) samples scan filters
    step = probe / "samples" / "step-3"
    step.mkdir(parents=True, exist_ok=True)
    (step / "sample_00.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (step / "sample_01.jpg").write_bytes(b"")
    old_webp = step / "sample_02.webp"
    old_webp.write_bytes(b"x")
    os.utime(old_webp, (past, past))
    (step / "note.txt").write_text("x", encoding="utf-8")
    events = A._scan_samples(probe, since=since)
    files = [item["file"] for event in events for item in event.get("files", [])]
    check("scan samples: fresh png kept", "sample_00.png" in files, str(files))
    check("scan samples: zero-byte jpg dropped", "sample_01.jpg" not in files, str(files))
    check("scan samples: stale webp dropped", "sample_02.webp" not in files, str(files))
    check("scan samples: non-image dropped", "note.txt" not in files, str(files))
    check(
        "scan samples: step event kept",
        any(event["step"] == 3 for event in events),
        json.dumps(events)[:200],
    )

    # --- 4) run() end-to-end: stale artifact in out/ must not be reported
    run_out = tmp / "fresh-out"
    run_out.mkdir(parents=True, exist_ok=True)
    stale_run = run_out / "epoch-9.safetensors"
    stale_run.write_bytes(b"\x00" * 64)
    os.utime(stale_run, (past, past))
    ds2 = tmp / "fresh-ds"
    ds2.mkdir(parents=True, exist_ok=True)
    (ds2 / "x.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (ds2 / "x.txt").write_text("a caption\n", encoding="utf-8")
    os.environ["STUB_MODE"] = "ok"
    result2 = A.run(
        {"id": "job-fresh", "params": base_params(model_dir, ds2, run_out)},
        tmp / "fresh.log",
        train_command=train_command,
        metrics_path=tmp / "fresh.metrics.json",
    )
    os.environ.pop("STUB_MODE", None)
    artifacts = [item["file"] for item in result2["metrics"].get("artifact_files", [])]
    check(
        "run excludes stale artifact pre-placed in out (negative control)",
        "epoch-9.safetensors" not in artifacts,
        str(artifacts),
    )
    check("run includes the fresh stub artifact", "epoch-0.safetensors" in artifacts, str(artifacts))
    check(
        "run records started_ts for auditability",
        isinstance(result2["metrics"].get("started_ts"), (int, float)),
        str(result2["metrics"].get("started_ts")),
    )


def test_sample_noise_run(train_command, tmp: Path, model_dir: Path, dataset: Path):
    """End-to-end: sampling tqdm noise must not move the training progress."""
    out = tmp / "run-sample-noise"
    params = base_params(model_dir, dataset, out)
    os.environ["STUB_MODE"] = "sample_noise"
    result = A.run(
        {"id": "job-sample-noise", "params": params},
        tmp / "sample-noise.log",
        train_command=train_command,
        metrics_path=tmp / "sample-noise.metrics.json",
    )
    os.environ.pop("STUB_MODE", None)
    metrics = result["metrics"]
    check("sample-noise run: state succeeded", result.get("state") == "succeeded",
          str(result.get("state")))
    check("sample-noise run: final progress is 401/1002",
          metrics.get("step") == 401 and metrics.get("total") == 1002,
          "%s/%s" % (metrics.get("step"), metrics.get("total")))
    check("sample-noise run: sampling tqdm lines dropped", metrics.get("dropped_tqdm") == 2,
          str(metrics.get("dropped_tqdm")))
    check("sample-noise run: in_sample / samples_in_flight cleared",
          metrics.get("in_sample") is False and metrics.get("samples_in_flight") == 0,
          json.dumps({k: metrics.get(k) for k in ("in_sample", "samples_in_flight")}))
    check("sample-noise run: log kept every line",
          (tmp / "sample-noise.log").is_file()
          and "ms-sample-begin" in (tmp / "sample-noise.log").read_text(encoding="utf-8", errors="replace"),
          "")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="diffsynth-selftest-"))
    try:
        if REAL_MODEL.is_dir():
            model_dir = REAL_MODEL
            source = "real %s" % REAL_MODEL
        else:
            model_dir = tmp / "model"
            make_synthetic_model(model_dir)
            source = "synthetic layout (real model missing)"
        dataset = make_dataset(tmp / "dataset")
        out = tmp / "out"

        test_expand(model_dir, source)
        test_argv(model_dir, dataset, out)
        test_arch(model_dir, dataset, out, tmp)
        test_new_arches(dataset, tmp)
        test_launch_command(model_dir, dataset, tmp)
        test_metadata_autogen(tmp)
        test_steps_conversion(tmp, model_dir)
        test_parse()
        test_sample_noise_unit()
        test_sampling(dataset, tmp)

        stub = tmp / "stub_train.py"
        stub.write_text(STUB_TEMPLATE, encoding="utf-8")
        test_run([sys.executable, str(stub)], tmp, model_dir, dataset)
        test_sample_noise_run([sys.executable, str(stub)], tmp, model_dir, dataset)
        test_caption_metrics_and_artifact_freshness(
            [sys.executable, str(stub)], tmp, model_dir
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("")
    print("RESULT: PASS %d / FAIL %d" % (PASS, FAIL))
    for item in FAILURES:
        print("  - " + item)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
