#!/usr/bin/env python3
"""Render one preview PNG from a trained LoRA, via DiffSynth's validate_lora scripts.

Stdlib only (the heavy imports happen in the ComfyUI Python subprocess).
Parameters are **read from the upstream validate_lora script** for the chosen
arch (pipeline class, component list, load_lora call, seed/steps/guidance);
local model paths replace the upstream hub ids.

Usage:
    python tools/preview_lora.py --arch z_image \
        --model models\\z-image-turbo \
        --lora _tmp\\zimage-smoke\\epoch-0.safetensors \
        --prompt "a red apple on a wooden table, studio light" \
        --out _tmp\\preview-z\\preview.png

    python tools/preview_lora.py --arch z_image ... --dry-run   # no GPU, prints the plan

Prints a JSON line: {"ok": true, "path": ..., "bytes": ..., "elapsed_s": ...}
Exit 0 = ok, 2 = bad args / missing files, 1 = render failed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPO = PROJECT_ROOT / "third_party" / "DiffSynth-Studio"
DEFAULT_PYTHON = r"E:\ComfyUI\python_embeded\python.exe"

ARCHS = {
    "qwen_image_21": {
        "script": "examples/qwen_image_21/model_training/validate_lora/Qwen-Image-2.1.py",
        "extra_component": "processor_config",
        "default_steps": 20,
        "default_guidance": 1.0,
        "default_seed": 0,
    },
    "moss_image": {
        "script": "examples/moss_image/model_training/validate_lora/MOSS-Image-SFT.py",
        "extra_component": "tokenizer_config",
        "default_steps": 50,
        "default_guidance": 3.0,
        "default_seed": 0,
    },
    "z_image": {
        "script": "examples/z_image/model_training/validate_lora/Z-Image-Turbo.py",
        "extra_component": "tokenizer_config",
        "default_steps": 8,
        "default_guidance": 1.0,
        "default_seed": 42,
    },
}

IMPORT_RE = re.compile(r"from diffsynth\.pipelines\.(\w+) import (\w+), ModelConfig")
COMPONENT_RE = re.compile(
    r'ModelConfig\(model_id="[^"]+",\s*origin_file_pattern="([^"]+)"((?:,\s*[^)]*)?)\)'
)
EXTRA_RE = re.compile(
    r'(tokenizer_config|processor_config)=ModelConfig\(model_id="[^"]+",\s*origin_file_pattern="[^"]+"\)'
)
CALL_RE = re.compile(r"image\s*=\s*pipe\((.*?)\)\s*$", re.MULTILINE | re.DOTALL)
LOAD_LORA_RE = re.compile(r"load_lora\(\s*pipe\.dit\s*,\s*[^)]*\)")


def parse_upstream(text: str, path: Path) -> dict:
    """Extract the pipeline/component/call shape from the upstream script."""
    match = IMPORT_RE.search(text)
    if not match:
        raise ValueError("cannot parse pipeline import from %s" % path)
    module, cls = match.group(1), match.group(2)

    # tokenizer_config / processor_config are not model_configs entries
    text_without_extra = re.sub(
        r"(tokenizer_config|processor_config)=ModelConfig\([^)]*\)", "", text
    )
    components = []
    for pattern, extra in COMPONENT_RE.findall(text_without_extra):
        components.append({"pattern": pattern.strip(), "extra": extra.strip(" ,")})
    if not components:
        raise ValueError("cannot parse ModelConfig components from %s" % path)

    extra_match = EXTRA_RE.search(text)
    extra_component = extra_match.group(1) if extra_match else None

    call_kwargs = {}
    call_match = CALL_RE.search(text)
    if call_match:
        for key, value in re.findall(r"(\w+)\s*=\s*([^,]+)", call_match.group(1)):
            if key == "prompt":
                continue
            value = value.strip()
            if value.startswith('"') and value.endswith('"'):
                call_kwargs[key] = value[1:-1]
            else:
                try:
                    call_kwargs[key] = float(value) if "." in value else int(value)
                except ValueError:
                    call_kwargs[key] = value
    if not LOAD_LORA_RE.search(text):
        raise ValueError("cannot parse load_lora call from %s" % path)

    return {
        "module": module,
        "class": cls,
        "components": components,
        "extra_component": extra_component,
        "call_kwargs": call_kwargs,
    }


def shards(folder: Path, label: str):
    files = sorted(p for p in folder.glob("*.safetensors") if p.is_file())
    if not files:
        raise ValueError("no %s/*.safetensors under %s" % (label, folder))
    return [str(p).replace("\\", "/") for p in files]


def resolve_local_model(model_dir: Path, parsed: dict, arch: str) -> list:
    configs = []
    for component in parsed["components"]:
        pattern = component["pattern"]
        extra = component["extra"]
        if pattern.startswith("transformer"):
            paths = shards(model_dir / "transformer", "transformer")
            entry = {"path": paths}
        elif pattern.startswith("text_encoder"):
            paths = shards(model_dir / "text_encoder", "text_encoder")
            entry = {"path": paths}
        elif pattern.startswith("vae"):
            vae_files = sorted((model_dir / "vae").glob("*.safetensors"))
            if not vae_files:
                raise ValueError("no vae/*.safetensors under %s" % model_dir)
            entry = {"path": str(vae_files[0]).replace("\\", "/")}
        else:
            raise ValueError("unsupported component pattern %r for arch %s" % (pattern, arch))
        if "computation_dtype" in extra:
            entry["computation_dtype"] = "torch.float32"
        configs.append(entry)
    return configs


def build_runner(arch: str, parsed: dict, model_dir: Path, lora: Path, prompt: str, out: Path,
                 steps: int, guidance: float, seed: int, height: int, width: int,
                 rand_device: str | None) -> str:
    configs = resolve_local_model(model_dir, parsed, arch)
    cfg_lines = []
    for entry in configs:
        if isinstance(entry["path"], list):
            path_expr = "[" + ", ".join(json.dumps(p) for p in entry["path"]) + "]"
        else:
            path_expr = json.dumps(entry["path"])
        extra = ""
        if entry.get("computation_dtype"):
            extra = ", computation_dtype=torch.float32"
        cfg_lines.append("        ModelConfig(path=%s%s)," % (path_expr, extra))

    extra_component = parsed["extra_component"]
    extra_line = ""
    if extra_component == "tokenizer_config":
        tokenizer = model_dir / "tokenizer"
        if not tokenizer.is_dir():
            raise ValueError("missing tokenizer dir: %s" % tokenizer)
        extra_line = "    tokenizer_config=ModelConfig(path=%s),\n" % json.dumps(tokenizer.as_posix())
    elif extra_component == "processor_config":
        processor = model_dir / "processor"
        if not processor.is_dir():
            raise ValueError("missing processor dir: %s" % processor)
        extra_line = "    processor_config=ModelConfig(path=%s),\n" % json.dumps(processor.as_posix())

    kwargs = {"seed": seed, "height": height, "width": width, "num_inference_steps": steps,
              "cfg_scale": guidance}
    if rand_device:
        kwargs["rand_device"] = rand_device
    kwargs_expr = ", ".join("%s=%r" % (key, value) for key, value in kwargs.items())

    return (
        "import json, time\n"
        "import torch\n"
        "import sys\n"
        "sys.path.insert(0, %s)\n"
        "from diffsynth.pipelines.%s import %s, ModelConfig\n"
        "\n"
        "_t0 = time.time()\n"
        "pipe = %s.from_pretrained(\n"
        "    torch_dtype=torch.bfloat16,\n"
        "    device='cuda',\n"
        "    model_configs=[\n"
        "%s\n"
        "    ],\n"
        "%s"
        ")\n"
        "pipe.load_lora(pipe.dit, %s)\n"
        "image = pipe(%s, %s)\n"
        "image.save(%s)\n"
        "import os\n"
        "print(json.dumps({'ok': True, 'path': %s, 'bytes': os.path.getsize(%s), "
        "'elapsed_s': round(time.time() - _t0, 2)}))\n"
        % (
            json.dumps(str(REPO)),
            parsed["module"],
            parsed["class"],
            parsed["class"],
            "\n".join(cfg_lines),
            extra_line,
            json.dumps(str(lora).replace("\\", "/")),
            json.dumps(prompt),
            kwargs_expr,
            json.dumps(str(out).replace("\\", "/")),
            json.dumps(str(out).replace("\\", "/")),
            json.dumps(str(out).replace("\\", "/")),
        )
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Render one LoRA preview PNG via DiffSynth")
    parser.add_argument("--arch", required=True, help="qwen_image_21 | moss_image | z_image")
    parser.add_argument("--model", required=True, help="local diffusers model directory")
    parser.add_argument("--lora", required=True, help="trained LoRA .safetensors")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--out", required=True, help="output .png path")
    parser.add_argument("--steps", type=int, default=None)
    parser.add_argument("--guidance", type=float, default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--height", type=int, default=512)
    parser.add_argument("--width", type=int, default=512)
    parser.add_argument("--python", default=DEFAULT_PYTHON)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--dry-run", action="store_true", help="print plan/runner, do not run GPU")
    args = parser.parse_args(argv)

    arch = args.arch.strip()
    if arch not in ARCHS:
        print("ERROR: unknown arch %r; known archs: %s" % (arch, ", ".join(sorted(ARCHS))), file=sys.stderr)
        return 2

    model_dir = Path(args.model).expanduser().resolve()
    lora = Path(args.lora).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    if not model_dir.is_dir():
        print("ERROR: model dir not found: %s" % model_dir, file=sys.stderr)
        return 2
    if not lora.is_file():
        print("ERROR: lora not found: %s" % lora, file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)

    script = REPO / ARCHS[arch]["script"]
    if not script.is_file():
        print("ERROR: upstream validate_lora script not found: %s" % script, file=sys.stderr)
        return 2
    parsed = parse_upstream(script.read_text(encoding="utf-8"), script)

    upstream_kwargs = parsed["call_kwargs"]
    steps = args.steps if args.steps is not None else int(upstream_kwargs.get("num_inference_steps", ARCHS[arch]["default_steps"]))
    guidance = args.guidance if args.guidance is not None else float(upstream_kwargs.get("cfg_scale", ARCHS[arch]["default_guidance"]))
    seed = args.seed if args.seed is not None else int(upstream_kwargs.get("seed", ARCHS[arch]["default_seed"]))
    rand_device = upstream_kwargs.get("rand_device")

    try:
        runner_code = build_runner(arch, parsed, model_dir, lora, args.prompt, out,
                                   steps, guidance, seed, args.height, args.width, rand_device)
    except ValueError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2

    plan = {
        "arch": arch,
        "upstream_script": str(script),
        "pipeline": "%s.%s" % (parsed["module"], parsed["class"]),
        "upstream_call_kwargs": upstream_kwargs,
        "effective": {"steps": steps, "guidance": guidance, "seed": seed,
                      "height": args.height, "width": args.width},
        "model": str(model_dir),
        "lora": str(lora),
        "out": str(out),
        "python": args.python,
    }

    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=1))
        print("---- runner ----")
        print(runner_code)
        return 0

    tmp_dir = Path(tempfile.mkdtemp(prefix="preview-lora-", dir=str(PROJECT_ROOT / "_tmp")))
    runner_path = tmp_dir / "runner.py"
    runner_path.write_text(runner_code, encoding="utf-8")
    started = time.time()
    try:
        proc = subprocess.run(
            [args.python, str(runner_path)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=args.timeout,
        )
    except subprocess.TimeoutExpired:
        print("ERROR: preview timed out after %.0fs" % args.timeout, file=sys.stderr)
        return 1
    elapsed = time.time() - started
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    result = None
    for line in reversed([ln.strip() for ln in stdout.splitlines() if ln.strip()]):
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict) and candidate.get("ok"):
            result = candidate
            break
    if proc.returncode != 0 or result is None:
        print("ERROR: preview failed (rc=%s, %.1fs)" % (proc.returncode, elapsed), file=sys.stderr)
        if stdout:
            print("---- stdout tail ----", file=sys.stderr)
            print("\n".join(stdout.splitlines()[-12:]), file=sys.stderr)
        if stderr:
            print("---- stderr tail ----", file=sys.stderr)
            print("\n".join(stderr.splitlines()[-12:]), file=sys.stderr)
        return 1
    result["elapsed_s"] = round(elapsed, 2)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if out.is_file() else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
