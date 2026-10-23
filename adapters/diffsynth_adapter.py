#!/usr/bin/env python3
# =============================================================================
# ModelSmith · L2 adapter 分块图谱  |  详见 adapters/README.md
# 角色   : kind="diffsynth" 的子进程适配器，由 core\modelsmith_core.py 的
#          _run_diffsynth 调用；本文件不碰 core / UI / 上游源码
# 上游   : third_party\DiffSynth-Studio\examples\qwen_image_21\model_training\train.py
#          （按 arch 分派：qwen_image_21 / moss_image / z_image）
# 主链   : job params -> prepare_dataset_metadata -> steps_plan + sample_config
#          -> build_launch_command（[python, -c, RUNNER, train.py, *argv]）
#          -> 子进程 stdout 逐行 parse_line -> metrics.json -> 交回 core
# 参数三分: ① core 传入：job params 原样透传；arch / python_paths 由 core 在
#          _create_job 里补齐与解析 ② 本文件自算：steps->epochs=1+repeat、
#          argv 默认值、metadata.csv 生成 ③ 上游默认：train.py 的 argparse
# 关键函数: steps_plan / _save_steps_value / sample_config / build_argv /
#          build_launch_command / parse_line / run
# 采样    : RUNNER 在子进程 monkey-patch ModelLogger.on_step_end（上游没有
#          采样回调），失败会写 [sample-error] 而不影响训练
# 产物    : <out>\epoch-*.safetensors、<out>\samples\step-N\sample_XX.png、
#          <out>\.ms_sample.json、<out>\metadata.csv（可能生成）
# 纪律    : 代码 token 不许改；本文件只加注释（注释不进 AST）
# =============================================================================
"""DiffSynth-Studio (Qwen-Image 2.1 LoRA) adapter for ModelSmith.

Stdlib only. No dependency install, no model download, no GPU work in this
module. The recipe was verified by hand on this machine (A-level):

    repo      third_party/DiffSynth-Studio            (Apache-2.0)
    python    E:\\ComfyUI\\python_embeded\\python.exe  (torch 2.12.0+cu130)
    model     E:\\临时文件夹\\Qwen-Image-2.1          (diffusers, sharded)
    run       1 epoch x 4 images @512, rank 8 -> 4/4 steps, 3.88 s/it,
              epoch-0.safetensors 40.1 MB

The train entry point is
``examples/qwen_image_21/model_training/train.py`` and its accepted flags come
from ``diffsynth/diffusion/parsers.py`` (add_general_config /
add_image_size_config). ``--model_paths`` is a JSON string decoding to a list
whose items are a path string or a list of shard paths (see
``training_module.parse_model_configs`` -> ``json.loads`` -> ``ModelConfig(path=...)``).

Public API:
    expand_model_paths(model_dir) -> list           # [transformer[], text_encoder[], vae]
    build_argv(params, out_dir) -> list[str]        # train.py argv
    parse_line(line) -> dict | None                 # tqdm / artifact / error events
    run(job, log_path, ...) -> dict                 # spawn + parse + cancel

Counter discipline:
    * ``step`` / ``total`` come from tqdm ``N/M [..]``
    * ``sec_per_it`` comes from tqdm ``x.xxs/it``
    * ``artifacts`` come from ``epoch-N.safetensors`` / ``step-N.safetensors``
      (log lines when printed, plus a post-run scan of the output dir)
"""

from __future__ import annotations

import json
import csv
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
DIFFSYNTH_ROOT = PROJECT_ROOT / "third_party" / "DiffSynth-Studio"
TRAIN_PY_QWEN = (
    DIFFSYNTH_ROOT / "examples" / "qwen_image_21" / "model_training" / "train.py"
)
TRAIN_PY_MOSS = (
    DIFFSYNTH_ROOT / "examples" / "moss_image" / "model_training" / "train.py"
)
TRAIN_PY_ZIMAGE = (
    DIFFSYNTH_ROOT / "examples" / "z_image" / "model_training" / "train.py"
)
TRAIN_PY_FLUX2 = (
    DIFFSYNTH_ROOT / "examples" / "flux2" / "model_training" / "train.py"
)
TRAIN_PY_KREA2 = (
    DIFFSYNTH_ROOT / "examples" / "krea2" / "model_training" / "train.py"
)
TRAIN_PY = TRAIN_PY_QWEN  # backwards-compatible alias
FLUX2_KLEIN_TARGETS = (
    "to_q,to_k,to_v,to_out.0,add_q_proj,add_k_proj,add_v_proj,to_add_out,"
    "linear_in,linear_out,to_qkv_mlp_proj,"
    + ",".join(
        "single_transformer_blocks.%d.attn.to_out" % index for index in range(24)
    )
)
KREA2_RAW_TARGETS = (
    "wq,wk,wv,gate,wo,gate,up,down,first,tmlp.0,tmlp.2,projector,"
    "txtmlp.1,txtmlp.3,last.linear,tproj.1"
)
DEFAULT_ARCH = "qwen_image_21"
ARCHES = {
    "qwen_image_21": {
        "train_py": TRAIN_PY_QWEN,
        "default_rank": 8,
        # 2026-10-23 用户要求：默认不进内存（不 offload），正常全量上卡。
        # 实测 qwen-image-2.1 全量放不下时，用 enable_model_cpu_offload 显式打开。
        "uses_initialize_cpu": False,
        "offload_default": False,
        "uses_max_pixels": False,
        "requires_tokenizer": False,
        "align16": False,
        "fixed": {
            "lora_base_model": "dit",
            "lora_target_modules": "",
            "remove_prefix_in_ckpt": "pipe.dit.",
        },
    },
    "moss_image": {
        "train_py": TRAIN_PY_MOSS,
        "default_rank": 32,
        "uses_initialize_cpu": False,
        "uses_max_pixels": True,
        "requires_tokenizer": True,
        "align16": True,
        "offload_default": False,
        "fixed": {
            "lora_base_model": "dit",
            "lora_target_modules": "",
            "remove_prefix_in_ckpt": "pipe.dit.",
        },
    },
    "z_image": {
        "train_py": TRAIN_PY_ZIMAGE,
        "default_rank": 32,
        "uses_initialize_cpu": False,
        "uses_max_pixels": True,
        "requires_tokenizer": True,
        "align16": True,
        "offload_default": False,
        "fixed": {
            "lora_base_model": "dit",
            # official: examples\z_image\model_training\lora\Z-Image-Turbo.sh:14
            "lora_target_modules": "to_q,to_k,to_v,to_out.0,w1,w2,w3",
            "remove_prefix_in_ckpt": "pipe.dit.",
        },
    },
    "flux2_klein_9b": {
        "train_py": TRAIN_PY_FLUX2,
        "default_rank": 32,
        "uses_initialize_cpu": False,
        "uses_max_pixels": True,
        "requires_tokenizer": True,
        "align16": True,
        # 2026-10-23 用户要求：默认不进内存（不 offload），正常全量上卡。
        # 注意：offload 在 MOSS/Z-Image 上会清缓存导致崩（IMAGE-ARCH-PRESETS 坑 1/2），
        # flux2 的 VAE 同样会崩在 flux2_vae.py:326，必须显式打开且自己承担风险。
        "offload_default": False,
        "fixed": {
            "lora_base_model": "dit",
            "lora_target_modules": FLUX2_KLEIN_TARGETS,
            "remove_prefix_in_ckpt": "pipe.dit.",
        },
    },
    "krea2_raw": {
        "train_py": TRAIN_PY_KREA2,
        "default_rank": 32,
        "uses_initialize_cpu": False,
        "uses_max_pixels": True,
        "requires_tokenizer": True,
        "align16": True,
        # 2026-10-23 用户要求：默认不进内存（不 offload），正常全量上卡。
        # krea2 全量 26.28+8.28=34.56 > 32.76 GiB，显式开 offload 才有 8.09 s/it 那条路。
        "offload_default": False,
        "three_path": True,
        "fixed": {
            "lora_base_model": "dit",
            "lora_target_modules": KREA2_RAW_TARGETS,
            "remove_prefix_in_ckpt": "pipe.dit.",
            "align_to_opensource_format": True,
            "find_unused_parameters": True,
        },
    },
}
DEFAULT_PYTHON_CANDIDATES = (
    os.environ.get("MODELSMITH_DIFFSYNTH_PYTHON"),
    r"E:\ComfyUI\python_embeded\python.exe",
)
DEFAULT_LORA_BASE_MODEL = "dit"
DEFAULT_TARGET_MODULES = ""  # empty = DiffSynth auto-detect (verified baseline)
DEFAULT_LORA_RANK = 8
DEFAULT_EPOCHS = 1
DEFAULT_LR = 1e-4
DEFAULT_REPEAT = 1
DEFAULT_SIZE = 512
DEFAULT_TASK = "sft"
DEFAULT_REMOVE_PREFIX = "pipe.dit."
MODEL_SUBDIRS = ("transformer", "text_encoder", "vae")
DEFAULT_SAMPLE_EVERY = 200
DEFAULT_SAMPLE_STEPS = 20
DEFAULT_SAMPLE_SIZE = 512
DEFAULT_SAMPLE_GUIDANCE = 1.0
DEFAULT_SAMPLE_SEED = 42

# --- [01] 采样钩子：RUNNER 是交给子进程的 -c 载荷（字符串，不在本进程执行）----
# 上游 train.py 没有 "每 N 步采样" 回调，只能在子进程里包 ModelLogger.on_step_end。
# 详见 adapters/README.md（为什么必须打补丁 / 为什么要恢复
# scheduler.timesteps(1000, training=True) 与各组件 device 快照 / 失败模式）
RUNNER = (
    "import json, os, runpy, sys\n"
    "import torch\n"
    "if os.environ.get('MS_TF32') == '1':\n"
    "    torch.backends.cuda.matmul.allow_tf32 = True\n"
    "    torch.backends.cudnn.benchmark = True\n"
    "    torch.set_float32_matmul_precision('high')\n"
    "    print('[ms-tf32] on', flush=True)\n"
    "print('[ms-flags] tf32=%s bench=%s prec=%s' % ("
    "torch.backends.cuda.matmul.allow_tf32, "
    "torch.backends.cudnn.benchmark, "
    "torch.get_float32_matmul_precision()), flush=True)\n"
    "repo, train_py, cfg_path = sys.argv[1], sys.argv[2], sys.argv[3]\n"
    "sys.path.insert(0, repo)\n"
    "sys.argv = [train_py] + sys.argv[4:]\n"
    "try:\n"
    "    cfg = json.load(open(cfg_path, encoding='utf-8'))\n"
    "except Exception:\n"
    "    cfg = {}\n"
    "for _p in reversed(cfg.get('python_paths') or []):\n"
    "    if _p and _p not in sys.path:\n"
    "        sys.path.insert(0, _p)\n"
    "every = int(cfg.get('every') or 0)\n"
    "if every > 0 and cfg.get('prompts'):\n"
    "    import torch\n"
    "    from diffsynth.diffusion.logger import ModelLogger\n"
    "    _orig = ModelLogger.on_step_end\n"
    "    def _ms_sample(logger, model, step):\n"
    "        mod = model\n"
    "        pipe = getattr(mod, 'pipe', None)\n"
    "        if pipe is None and getattr(mod, 'module', None) is not None:\n"
    "            pipe = getattr(mod.module, 'pipe', None)\n"
    "        if pipe is None:\n"
    "            print('[sample-error] step=%d no training pipe' % step, flush=True)\n"
    "            return\n"
    "        outdir = os.path.join(logger.output_path, 'samples', 'step-%d' % step)\n"
    "        os.makedirs(outdir, exist_ok=True)\n"
    "        dit = getattr(pipe, 'dit', None)\n"
    "        was_training = bool(dit.training) if dit is not None else False\n"
    "        targets = {}\n"
    "        for name, child in pipe.named_children():\n"
    "            try:\n"
    "                targets[name] = next(child.parameters()).device\n"
    "            except StopIteration:\n"
    "                pass\n"
    "        try:\n"
    "            print('[ms-sample-begin] step=%d' % step, flush=True)\n"
    "            if dit is not None:\n"
    "                dit.eval()\n"
    "            with torch.no_grad():\n"
    "                for i, prompt in enumerate(cfg.get('prompts') or []):\n"
    "                    image = pipe(\n"
    "                        prompt=prompt,\n"
    "                        negative_prompt=cfg.get('negative', ''),\n"
    "                        cfg_scale=float(cfg.get('guidance', 1.0)),\n"
    "                        height=int(cfg['height']), width=int(cfg['width']),\n"
    "                        seed=int(cfg.get('seed', 42)) + i,\n"
    "                        num_inference_steps=int(cfg.get('steps', 20)),\n"
    "                    )\n"
    "                    path = os.path.join(outdir, 'sample_%02d.png' % i)\n"
    "                    image.save(path)\n"
    "                    print('[sample] step=%d file=%s' % (step, path), flush=True)\n"
    "        except Exception as exc:\n"
    "            print('[sample-error] step=%d %s: %s' % (step, type(exc).__name__, exc), flush=True)\n"
    "        finally:\n"
    "            if dit is not None:\n"
    "                dit.train(was_training)\n"
    "            for name, dev in targets.items():\n"
    "                child = getattr(pipe, name, None)\n"
    "                if child is not None:\n"
    "                    try:\n"
    "                        child.to(dev)\n"
    "                    except Exception:\n"
    "                        pass\n"
    "            try:\n"
    "                torch.cuda.empty_cache()\n"
    "            except Exception:\n"
    "                pass\n"
    "            try:\n"
    "                pipe.scheduler.set_timesteps(1000, training=True)\n"
    "            except Exception:\n"
    "                pass\n"
    "            print('[ms-sample-end] step=%d' % step, flush=True)\n"
    "    def _patched(self, accelerator, model, save_steps=None, **kwargs):\n"
    "        _orig(self, accelerator, model, save_steps, **kwargs)\n"
    "        step = int(getattr(self, 'num_steps', 0) or 0)\n"
    "        if every > 0 and step > 0 and step % every == 0:\n"
    "            if getattr(accelerator, 'is_main_process', True):\n"
    "                _ms_sample(self, model, step)\n"
    "    ModelLogger.on_step_end = _patched\n"
    "runpy.run_path(train_py, run_name='__main__')\n"
)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PROGRESS_RE = re.compile(r"(?P<step>\d+)\s*/\s*(?P<total>\d+)\s*\[")
SPIT_RE = re.compile(r"(?P<spit>\d+(?:\.\d+)?)\s*s/it")
LOSS_RE = re.compile(
    r"\bloss(?:\.[A-Za-z0-9_/]+)?\s*[:=]\s*"
    r"(?P<loss>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)",
    re.IGNORECASE,
)
ARTIFACT_RE = re.compile(r"(?P<file>(?:epoch|step)-\d+\.safetensors)")
ERROR_RE = re.compile(r"^Traceback \(most recent call last\)|^[A-Za-z_.]+Error:")
SAMPLE_LINE_RE = re.compile(r"^\[sample\]\s+step=(?P<step>\d+)\s+file=(?P<file>.+)$")
SAMPLE_ERROR_RE = re.compile(r"^\[sample-error\]\s+step=(?P<step>\d+)\s*(?P<msg>.*)$")
# Sampling markers emitted by the runner (_ms_sample). While a sample is in flight,
# DiffSynth prints its own 20-step denoising tqdm -- that must NOT be read as a
# training step (it made the UI jump from 400/1002 to 10/20 and back).
SAMPLE_BEGIN_RE = re.compile(r"^\[ms-sample-begin\]\s*step=(?P<step>\d+)\s*$")
SAMPLE_END_RE = re.compile(r"^\[ms-sample-end\]\s*step=(?P<step>\d+)\s*$")
SAMPLING_TOTAL_MAX = 100  # totals <= 100 that disagree with steps_plan are sampling noise


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _fwd(path) -> str:
    """Forward-slash absolute path (the verified baseline feeds train.py these)."""
    return str(Path(path).resolve()).replace("\\", "/")


def _present(value) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _param(params: dict, key: str, *fallbacks, default=None):
    """Look up a dotted key (``train.lr``) then flat aliases."""
    for candidate in (key,) + fallbacks:
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
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("%s must be a number" % name)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be a number" % name)


def _as_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValueError("expected a boolean, got %r" % (value,))


def _clean_line(line) -> str:
    text = str(line or "").replace("\r", "")
    return ANSI_RE.sub("", text).rstrip("\n").strip()


# ---------------------------------------------------------------------------
# model layout
# ---------------------------------------------------------------------------


def _safetensors(folder: Path):
    if not folder.is_dir():
        return []
    return sorted(
        (p for p in folder.glob("*.safetensors") if p.is_file()),
        key=lambda p: p.name,
    )


def expand_model_paths(model_dir) -> list:
    """Expand a diffusers model directory into DiffSynth's ``--model_paths``.

    Returns ``[transformer_shards, text_encoder_shards, vae]`` where each item
    is a list of absolute forward-slash paths, except ``vae`` which is a plain
    string when the VAE is a single file (the verified baseline shape).

    Shard order is by file name (``model-00001-of-00004`` ...). Raises
    ValueError naming the missing subdirectory/shard set.
    """
    root = Path(model_dir)
    if not root.is_dir():
        raise ValueError("model_dir does not exist or is not a directory: %s" % model_dir)
    out = []
    for sub in MODEL_SUBDIRS:
        files = _safetensors(root / sub)
        if not files:
            raise ValueError(
                "no *.safetensors shards under %s (expected %s/; "
                "point model at the diffusers directory, not a single file)" % (root / sub, sub)
            )
        paths = [_fwd(p) for p in files]
        if sub == "vae" and len(paths) == 1:
            out.append(paths[0])
        else:
            out.append(paths)
    return out


def default_processor_path(model_dir) -> str:
    return str(Path(model_dir) / "processor")


def default_metadata_path(dataset_dir) -> str:
    return str(Path(dataset_dir) / "metadata.csv")


DEFAULT_IMAGE_EXTS = ("jpg", "jpeg", "png")


def _normalise_exts(value) -> list:
    if value is None:
        return list(DEFAULT_IMAGE_EXTS)
    if isinstance(value, str):
        parts = [part.strip().lower().lstrip(".") for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple)):
        parts = [str(part).strip().lower().lstrip(".") for part in value if str(part).strip()]
    else:
        raise ValueError("dataset_image_exts must be a comma string or a list")
    if not parts:
        raise ValueError("dataset_image_exts must not be empty")
    return parts


def _metadata_columns(path: Path) -> list:
    # utf-8-sig: Windows editors/Notepad/PowerShell often prepend a BOM
    with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
        first = handle.readline().strip()
    return [cell.strip().strip('"') for cell in first.split(",")]


def _clean_caption(text: str) -> str:
    """Drop BOM / zero-width leftovers and surrounding whitespace."""
    return (text or "").replace("\ufeff", "").strip()


CAPTION_EXT_DEFAULT = ".txt"
_CAPTION_EXT_RE = re.compile(r"^\.[A-Za-z0-9_]+$")


def _caption_ext(params) -> str:
    """params.caption_ext -> normalised ``'.ext'`` (default ``.txt``, leading dot added)."""
    raw = _param(
        params, "caption_ext", "datasets.caption_ext", "caption_suffix", default=None
    )
    if not _present(raw):
        return CAPTION_EXT_DEFAULT
    text = str(raw).strip().lower()
    if not text:
        return CAPTION_EXT_DEFAULT
    if not text.startswith("."):
        text = "." + text
    if not _CAPTION_EXT_RE.match(text):
        raise ValueError(
            "caption_ext must be a single extension like '.txt' or '.caption', got %r"
            % (raw,)
        )
    return text


def caption_coverage(prepared: dict) -> dict:
    """Summarise caption coverage for a generated metadata.csv (metrics + warnings).

    ``no_captions`` is only meaningful when *we generated* the CSV (an explicit
    user metadata.csv already carries its own prompts).
    """
    n = prepared.get("n")
    from_txt = prepared.get("from_txt")
    no_captions = (
        bool(prepared.get("generated"))
        and isinstance(n, int)
        and n > 0
        and from_txt == 0
    )
    info = {
        "caption_ext": prepared.get("caption_ext") or CAPTION_EXT_DEFAULT,
        "no_captions": no_captions,
        "no_captions_images": int(n) if no_captions else 0,
        "warning": None,
    }
    if no_captions:
        info["warning"] = (
            "no_captions: %d image(s) have no '%s' caption; using default caption template"
            % (int(n), info["caption_ext"])
        )
    return info


def prepare_dataset_metadata(params: dict, out_dir) -> dict:
    """Return a usable metadata.csv, generating one under ``<out>`` if needed.

    Ordinary users point ``dataset`` at a folder of images (often with
    same-name ``.txt`` captions) and do not have a ``metadata.csv``. Instead of
    failing, we build ``<out>/dataset_metadata.csv`` (never write the user's
    dataset folder) with absolute ``image`` paths.
    """
    dataset = _require_param(params, "dataset", "data", "dataset_base_path")
    dataset_path = Path(dataset)
    if not dataset_path.is_dir():
        raise ValueError("dataset directory does not exist: %s" % dataset_path)
    exts = _normalise_exts(_param(params, "dataset_image_exts", "image_exts", default=None))
    caption_ext = _caption_ext(params)

    explicit = _param(params, "dataset_metadata_path", "metadata", "metadata_path", default=None)
    candidates = []
    if _present(explicit):
        candidates.append(Path(explicit))
    candidates.append(dataset_path / "metadata.csv")
    for candidate in candidates:
        if candidate.is_file():
            columns = _metadata_columns(candidate)
            missing = [name for name in ("image", "prompt") if name not in columns]
            if missing:
                raise ValueError(
                    "dataset metadata %s is missing column(s): %s (need 'image,prompt')"
                    % (candidate, ", ".join(missing))
                )
            return {
                "path": str(candidate),
                "generated": False,
                "n": None,
                "from_txt": None,
                "from_default": None,
                "image_exts": exts,
                "caption_ext": caption_ext,
            }

    images = {}
    for entry in dataset_path.iterdir():
        if entry.is_file() and entry.suffix.lower().lstrip(".") in exts:
            images.setdefault(entry.name.lower(), entry)
    ordered = sorted(images.values(), key=lambda item: item.name.lower())
    if not ordered:
        raise ValueError(
            "no images under %s (looked for %s)" % (dataset_path, "/".join(exts))
        )

    template = str(
        _param(params, "dataset_default_caption", "default_caption", default=None)
        or "a photo of {image}"
    )
    out_root = Path(out_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    destination = out_root / "dataset_metadata.csv"
    rows = []
    from_txt = 0
    from_default = 0
    for image in ordered:
        caption_file = image.with_suffix(caption_ext)
        prompt = ""
        if caption_file.is_file():
            try:
                # utf-8-sig: strip the BOM that Notepad/PowerShell UTF-8 writes
                prompt = _clean_caption(
                    caption_file.read_text(encoding="utf-8-sig", errors="replace")
                )
            except OSError:
                prompt = ""
        if prompt:
            from_txt += 1
        else:
            prompt = _clean_caption(
                template.format(image=image.stem) if "{image}" in template else template
            )
            from_default += 1
        rows.append({"image": image.as_posix(), "prompt": prompt})

    with open(destination, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["image", "prompt"])
        writer.writeheader()
        writer.writerows(rows)
    return {
        "path": str(destination),
        "generated": True,
        "n": len(rows),
        "from_txt": from_txt,
        "from_default": from_default,
        "image_exts": exts,
        "caption_ext": caption_ext,
    }


# ---------------------------------------------------------------------------
# argv
# ---------------------------------------------------------------------------


def _normalise_model_paths(value) -> list:
    """Accept the nested list, or a JSON string of it, or a flat path."""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            value = json.loads(text)
        else:
            value = [text]
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("model_paths must be a non-empty list")
    out = []
    for item in value:
        if isinstance(item, (list, tuple)):
            out.append([_fwd(p) for p in item])
        else:
            out.append(_fwd(item))
    return out


# --- [02] steps 换算链：requested steps -> (num_epochs=1, dataset_repeat) ------
# 上游没有 --steps：total = num_epochs * rows * dataset_repeat。repeat 必须取
# ceil(steps/rows)（见 MAP-adapters.md §4 与 metrics 四字段谁写谁读）。
def _save_steps_value(params: dict):
    """None = param absent; 0 = epoch-only saves (omit --save_steps); >0 = step saves."""
    raw = _param(params, "save_steps", default=None)
    if not _present(raw):
        return None
    return _as_int(raw, "save_steps", minimum=0)


def _target_modules(value) -> str:
    if value is None:
        return DEFAULT_TARGET_MODULES
    if isinstance(value, (list, tuple)):
        return ",".join(str(x) for x in value)
    return str(value)


# ---------------------------------------------------------------------------
# arch dispatch
# ---------------------------------------------------------------------------


def resolve_arch(params: dict) -> str:
    """Return the arch key; unknown archs fail loudly (never guess a train.py)."""
    arch = _param(params, "arch", "model.arch", default=None)
    arch = DEFAULT_ARCH if not _present(arch) else str(arch).strip()
    if arch not in ARCHES:
        raise ValueError(
            "unknown arch %r; known archs: %s" % (arch, ", ".join(sorted(ARCHES)))
        )
    return arch


def _align_up(value: int, factor: int) -> int:
    value = int(value)
    return ((value + factor - 1) // factor) * factor


def align16(value: int) -> int:
    """Round **up** to the next multiple of 16 (MOSS-Image height/width factor)."""
    return _align_up(value, 16)


def align32(value: int) -> int:
    """Round **up** to the next multiple of 32 (Qwen-Image 2.1 pipeline factor).

    ``diffsynth/pipelines/qwen_image_21.py`` declares
    ``height/width_division_factor=32``.  Handing the trainer a size such as 1020
    makes the VAE patchify view die with
    ``RuntimeError: shape '[1, 192, 1, 2, 127, 2, 127, 2]' is invalid for input
    of size 24969600`` (1020*1020*24).  Upstream only *prints* "We round it up to
    1024" and keeps going, so the guard has to live here.
    """
    return _align_up(value, 32)


def train_py_for(arch: str) -> Path:
    """Return the upstream train.py for an arch (never the caller's guess)."""
    return ARCHES[arch]["train_py"]


def train_py_matches_arch(arch: str, path) -> bool:
    """True only when *path* is that arch's train.py (symlink/relative safe)."""
    if path is None:
        return False
    try:
        return Path(path).resolve() == train_py_for(arch).resolve()
    except OSError:
        return False


def build_launch_command(
    params: dict,
    out_dir,
    *,
    python=None,
    repo=None,
    train_py=None,
    train_command=None,
    sample_cfg_path=None,
) -> dict:
    """Build the launch command; **arch selects train.py, not the caller**.

    Regression: core used to hand in its hard-coded qwen ``train_py`` for every
    arch, so a MOSS job ran the qwen parser and died on ``--tokenizer_path``.
    A ``train_py`` that does not match the resolved arch is ignored (and
    reported via ``ignored_train_py``); the arch's own path always wins.
    """
    arch = resolve_arch(params)
    argv = build_argv(params, out_dir)
    repo = Path(repo) if repo is not None else DIFFSYNTH_ROOT
    expected = train_py_for(arch)
    ignored_train_py = None

    if train_command is not None:
        command = [str(part) for part in train_command] + argv
        cwd = str(repo) if Path(repo).is_dir() else None
        return {
            "arch": arch,
            "command": command,
            "cwd": cwd,
            "train_py": None,
            "ignored_train_py": None,
            "argv": argv,
        }

    chosen = expected
    if train_py is not None:
        if train_py_matches_arch(arch, train_py):
            chosen = Path(train_py)
        else:
            ignored_train_py = str(train_py)

    if not Path(repo).is_dir():
        raise ValueError("DiffSynth repo not found: %s" % repo)
    if not Path(chosen).is_file():
        raise ValueError("DiffSynth train.py not found: %s" % chosen)
    if sample_cfg_path is None:
        sample_cfg_path = Path(out_dir) / ".ms_sample.json"
    command = [
        str(python or _default_python()),
        "-c",
        RUNNER,
        str(repo),
        str(chosen),
        str(sample_cfg_path),
    ] + argv
    return {
        "arch": arch,
        "command": command,
        "cwd": str(repo),
        "train_py": str(chosen),
        "ignored_train_py": ignored_train_py,
        "argv": argv,
    }


def size_alignment(params: dict, factor: int = 16) -> dict:
    """Compute the effective size and whether it had to be aligned to ``factor``."""
    width = _param(params, "width", default=None)
    height = _param(params, "height", default=None)
    resolution = _param(params, "resolution", default=None)
    if width is None:
        width = resolution if isinstance(resolution, int) else DEFAULT_SIZE
    if height is None:
        height = resolution if isinstance(resolution, int) else DEFAULT_SIZE
    width = _as_int(width, "width", minimum=64)
    height = _as_int(height, "height", minimum=64)
    aligned_width, aligned_height = _align_up(width, factor), _align_up(height, factor)
    changed = aligned_width != width or aligned_height != height
    return {
        "width": aligned_width,
        "height": aligned_height,
        "max_pixels": aligned_width * aligned_height,
        "aligned_from": {"width": width, "height": height} if changed else None,
    }


# ---------------------------------------------------------------------------
# sampling config (preview images every N optimizer steps)
# ---------------------------------------------------------------------------


def sample_config(params: dict, warnings=None) -> dict:
    """Build the per-N-step sampling config consumed by the ``-c`` runner.

    Sampling is implemented as a runtime hook on ``ModelLogger.on_step_end``
    (upstream has no sampling callback), so the training process renders with
    the pipe it already has loaded — no restart, no extra model load.

    ``sample_every`` defaults to 200; ``0`` disables sampling. If
    ``sample_every > 0`` but ``sample_prompts`` is empty, sampling is disabled
    with an explicit warning (keeps old jobs working; documented in the recipe).
    """
    warnings = warnings if warnings is not None else []
    every = _as_int(_param(params, "sample_every", default=DEFAULT_SAMPLE_EVERY), "sample_every", minimum=0)
    raw_prompts = _param(params, "sample_prompts", "sample.prompts", default=None)
    if raw_prompts is None:
        raw_prompts = []
    if not isinstance(raw_prompts, (list, tuple)) or not all(isinstance(p, str) for p in raw_prompts):
        raise ValueError("sample_prompts must be a list of strings")
    prompts = [p for p in raw_prompts if p.strip()]
    requested_every = every
    if every > 0 and not prompts:
        warnings.append(
            "sample_every=%d but sample_prompts is empty: sampling disabled (no preview images)" % every
        )
        every = 0
    steps = _as_int(
        _param(params, "sample_steps", default=DEFAULT_SAMPLE_STEPS), "sample_steps", minimum=1
    )
    guidance = _as_float(
        _param(params, "sample_guidance", "sample.guidance_scale", default=DEFAULT_SAMPLE_GUIDANCE),
        "sample_guidance",
    )
    seed = _as_int(_param(params, "sample_seed", "sample.seed", default=DEFAULT_SAMPLE_SEED), "sample_seed", minimum=0)
    width = _as_int(_param(params, "sample_width", "sample.width", default=DEFAULT_SAMPLE_SIZE), "sample_width", minimum=64)
    height = _as_int(_param(params, "sample_height", "sample.height", default=DEFAULT_SAMPLE_SIZE), "sample_height", minimum=64)
    negative = str(_param(params, "sample_negative", "sample.neg", default="") or "")
    raw_paths = _param(params, "python_paths", "extra_python_paths", default=None) or []
    if not isinstance(raw_paths, (list, tuple)) or not all(isinstance(p, str) for p in raw_paths):
        raise ValueError("python_paths must be a list of strings")
    python_paths = []
    for entry in raw_paths:
        path = Path(entry)
        if not path.is_dir():
            raise ValueError("python_paths entry is not a directory: %s" % entry)
        python_paths.append(str(path))
    return {
        "requested_every": requested_every,
        "every": every,
        "prompts": prompts,
        "python_paths": python_paths,
        "steps": steps,
        "guidance": guidance,
        "seed": seed,
        "width": width,
        "height": height,
        "negative": negative,
    }


def _count_dataset_rows(metadata_path) -> int | None:
    try:
        with open(metadata_path, "r", encoding="utf-8", errors="replace") as handle:
            lines = [line for line in handle.read().splitlines() if line.strip()]
    except OSError:
        return None
    return max(0, len(lines) - 1)  # minus header


def steps_estimate(params: dict, metadata_path) -> dict:
    """Estimate optimizer steps for the run (written into metrics).

    DiffSynth's ``launch_training_task`` uses ``DataLoader(dataset)`` with no
    batch size (one record per step) and ``repeat`` folds into the dataset
    length, so: total = num_epochs * dataset_rows * dataset_repeat.
    """
    rows = _param(params, "dataset_rows", default=None)
    if rows is None:
        rows = _count_dataset_rows(metadata_path)
    repeat = _as_int(_param(params, "dataset_repeat", "repeat", default=DEFAULT_REPEAT), "dataset_repeat")
    epochs = _as_int(_param(params, "num_epochs", "epochs", "train.epochs", default=DEFAULT_EPOCHS), "num_epochs")
    if rows is None:
        return {"dataset_rows": None, "dataset_repeat": repeat, "num_epochs": epochs,
                "steps_per_epoch": None, "total_steps": None,
                "source": "metadata.csv not counted"}
    return {
        "dataset_rows": int(rows),
        "dataset_repeat": repeat,
        "num_epochs": epochs,
        "steps_per_epoch": int(rows) * repeat,
        "total_steps": int(rows) * repeat * epochs,
        "source": "num_epochs * dataset_rows * dataset_repeat",
    }


def steps_plan(params: dict, metadata_path, warnings=None) -> dict:
    """Translate ``params.steps`` into DiffSynth's (num_epochs, dataset_repeat).

    DiffSynth train.py has no "total steps" flag: total = num_epochs *
    dataset_rows * dataset_repeat. When the UI sends ``steps`` we honour it by
    fixing num_epochs=1 and choosing repeat = ceil(steps / rows), so the actual
    step count is as close as possible (multiple of rows).
    """
    warnings = warnings if warnings is not None else []
    epochs = _as_int(
        _param(params, "num_epochs", "epochs", "train.epochs", default=DEFAULT_EPOCHS),
        "num_epochs",
    )
    repeat = _as_int(
        _param(params, "dataset_repeat", "repeat", default=DEFAULT_REPEAT),
        "dataset_repeat",
    )
    rows = _param(params, "dataset_rows", default=None)
    if rows is None:
        rows = _count_dataset_rows(metadata_path)
    rows = int(rows) if rows is not None else None

    requested_raw = _param(params, "steps", "train.steps", default=None)
    if not _present(requested_raw):
        return {
            "requested_steps": None,
            "effective_steps": None,
            "steps_from_repeat": None,
            "rows": rows,
            "epochs": epochs,
            "repeat": repeat,
            "overshoot": False,
        }

    requested = _as_int(requested_raw, "steps", minimum=1)
    if not rows or rows <= 0:
        raise ValueError(
            "cannot convert steps=%d: dataset has no rows (%s)" % (requested, metadata_path)
        )
    repeat_from_steps = max(1, -(-requested // rows))
    effective = rows * repeat_from_steps
    overshoot = effective != requested
    if overshoot:
        warnings.append(
            "steps_overshoot: requested=%d effective=%d" % (requested, effective)
        )
    return {
        "requested_steps": requested,
        "effective_steps": effective,
        "steps_from_repeat": repeat_from_steps,
        "rows": rows,
        "epochs": 1,
        "repeat": repeat_from_steps,
        "overshoot": overshoot,
    }


SAMPLE_EXTS = (".png", ".jpg", ".jpeg", ".webp")  # jpeg is an accepted jpg alias


def _file_ok(path, exts, since=None):
    """Return True only for a real, non-empty file of an allowed ext (and fresh enough).

    ``since`` (epoch seconds) drops files whose mtime is older than this run, so
    a stale artifact left in ``out`` is never reported as this run's output.
    """
    path = Path(path)
    if path.suffix.lower() not in exts:
        return False
    try:
        stat = path.stat()
    except OSError:
        return False
    if not path.is_file() or stat.st_size <= 0:
        return False
    if since is not None and stat.st_mtime < since:
        return False
    return True


def _scan_samples(out_dir: Path, since=None) -> list:
    """Return [{step, files:[{file,bytes,path}], lora_path}] for <out>/samples/step-*.

    Only ``png/jpg/webp`` files with ``size>0`` and ``mtime >= since`` are kept
    (``since`` = this run's start; None disables the mtime filter for callers
    that scan an archive directory).
    """
    root = Path(out_dir) / "samples"
    if not root.is_dir():
        return []
    events = []
    for folder in sorted(root.glob("step-*"), key=lambda p: int(p.name.split("-", 1)[1]) if p.name.split("-", 1)[1].isdigit() else 0):
        if not folder.is_dir() or not folder.name.split("-", 1)[1].isdigit():
            continue
        step = int(folder.name.split("-", 1)[1])
        files = []
        for path in sorted(folder.iterdir()):
            if not _file_ok(path, SAMPLE_EXTS, since):
                continue
            try:
                files.append({"file": path.name, "bytes": path.stat().st_size, "path": str(path)})
            except OSError:
                pass
        lora_file = Path(out_dir) / ("step-%d.safetensors" % step)
        events.append(
            {
                "step": step,
                "files": files,
                "lora_path": str(lora_file) if _file_ok(lora_file, (".safetensors",), since) else None,
            }
        )
    events.sort(key=lambda item: item["step"])
    return events


# --- [03] argv 契约：params -> train.py 的真实命令行（qwen/moss/z 三分支）-----
# 完整「参数名 -> flag -> 上游语义」表见 MAP-adapters.md §2.1；save_steps=0 的
# 语义见 _save_steps_value()（0 = 省略 --save_steps，只出 epoch 权重）。
def _resume_rank(params: dict):
    arch = resolve_arch(params)
    arch_cfg = ARCHES[arch]
    return _as_int(
        _param(params, "lora_rank", "network.linear", "rank", default=arch_cfg["default_rank"]),
        "lora_rank",
        minimum=1,
    )


def read_safetensors_header(path) -> dict:
    """Read the safetensors header with stdlib only (no torch, no file load)."""
    import struct

    source = Path(path)
    with open(source, "rb") as fh:
        raw_len = fh.read(8)
        if len(raw_len) != 8:
            raise ValueError("resume_from is not a safetensors file: %s" % source)
        header_len = struct.unpack("<Q", raw_len)[0]
        if header_len <= 0 or header_len > 64 * 1024 * 1024:
            raise ValueError("invalid safetensors header length: %s" % source)
        raw = fh.read(header_len)
    header = json.loads(raw.decode("utf-8"))
    if not isinstance(header, dict):
        raise ValueError("invalid safetensors header object: %s" % source)
    metadata = header.get("__metadata__") if isinstance(header.get("__metadata__"), dict) else {}
    tensors = {k: v for k, v in header.items() if k != "__metadata__"}
    return {"metadata": metadata, "tensors": tensors, "path": str(source)}


def preflight_resume(params: dict) -> dict:
    """Reject a resume checkpoint before spawn if arch/rank/base mismatch.

    Pitfall: same arch/rank LoRA artifacts have a constant byte size; judge a
    real continuation by SHA256 change, never by "new file is larger".
    """
    raw = _param(params, "resume_from", default=None)
    if not _present(raw):
        return {}
    source = Path(str(raw)).expanduser()
    if not source.is_absolute():
        raise ValueError("resume_from 必须是绝对路径: %s" % source)
    if source.suffix.lower() != ".safetensors" or not source.is_file():
        raise ValueError("resume_from 必须是已存在的 *.safetensors: %s" % source)
    expected_arch = resolve_arch(params)
    expected_rank = _resume_rank(params)
    header = read_safetensors_header(source)
    metadata = header["metadata"]
    sidecar = source.parent / "training_args.json"
    sidecar_args = {}
    if sidecar.is_file():
        try:
            sidecar_args = json.loads(sidecar.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            sidecar_args = {}
    expected_base = _param(params, "model", default=None)
    sidecar_models = sidecar_args.get("model_paths") if isinstance(sidecar_args, dict) else None
    if _present(expected_base) and _present(sidecar_models):
        expected_text = str(Path(str(expected_base)).resolve()).replace("\\", "/").lower()
        if expected_text not in str(sidecar_models).replace("\\", "/").lower():
            raise ValueError(
                "resume preflight rejected: 底模不符，期望 %s，ckpt 来源模型 %s（ckpt=%s）"
                % (expected_base, _tail(str(sidecar_models), 180), source)
            )
    sidecar_rank = sidecar_args.get("lora_rank") if isinstance(sidecar_args, dict) else None
    if _present(sidecar_rank):
        try:
            if int(sidecar_rank) != expected_rank:
                raise ValueError(
                    "resume preflight rejected: rank 不符，期望 %d，实到 %s（ckpt=%s）"
                    % (expected_rank, sidecar_rank, source)
                )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ValueError) and "rank 不符" in str(exc):
                raise
            raise ValueError("resume preflight rejected: 无法解析 sidecar rank=%r" % sidecar_rank)
    meta_arch = metadata.get("arch") or metadata.get("ms_arch")
    if _present(meta_arch) and str(meta_arch) != expected_arch:
        raise ValueError(
            "resume preflight rejected: arch 期望 %s，实到 %s（ckpt=%s）"
            % (expected_arch, meta_arch, source)
        )
    meta_rank = metadata.get("rank") or metadata.get("ms_rank")
    found_ranks = set()
    if _present(meta_rank):
        try:
            found_ranks.add(int(meta_rank))
        except (TypeError, ValueError):
            raise ValueError("resume preflight rejected: 无法解析 ckpt rank=%r" % meta_rank)
    for key, value in header["tensors"].items():
        shape = value.get("shape") if isinstance(value, dict) else None
        if not isinstance(shape, list):
            continue
        if "lora_A" in key and shape:
            found_ranks.add(int(shape[0]))
        elif "lora_B" in key and len(shape) >= 2:
            found_ranks.add(int(shape[1]))
    if not found_ranks:
        raise ValueError(
            "resume preflight rejected: ckpt 里没有可识别的 LoRA rank key（ckpt=%s）" % source
        )
    if len(found_ranks) > 1:
        raise ValueError(
            "resume preflight rejected: ckpt rank 不一致 %s（ckpt=%s）"
            % (sorted(found_ranks), source)
        )
    found_rank = found_ranks.pop()
    if found_rank != expected_rank:
        raise ValueError(
            "resume preflight rejected: rank 不符，期望 %d，实到 %d（ckpt=%s）"
            % (expected_rank, found_rank, source)
        )
    meta_base = metadata.get("base_model") or metadata.get("ms_base") or metadata.get("model")
    expected_base = _param(params, "model", default=None)
    if _present(meta_base) and _present(expected_base):
        if Path(str(meta_base)).resolve() != Path(str(expected_base)).resolve():
            raise ValueError(
                "resume preflight rejected: 底模不符，期望 %s，实到 %s（ckpt=%s）"
                % (expected_base, meta_base, source)
            )
    missing = metadata.get("missing_keys") or []
    if isinstance(missing, list) and missing:
        raise ValueError(
            "resume preflight rejected: missing_keys=%d（ckpt=%s）"
            % (len(missing), source)
        )
    return {
        "resume_from": str(source),
        "arch": expected_arch,
        "rank": expected_rank,
        "missing_keys": 0,
    }


def _append_resume_arg(argv: list, params: dict) -> list:
    resume = _param(params, "resume_from", default=None)
    if _present(resume):
        # Upstream supports continuing a LoRA through --lora_checkpoint only;
        # --resume_from_checkpoint runs before LoRA injection and rejects keys.
        argv += ["--lora_checkpoint", str(Path(str(resume)).expanduser())]
    return argv


def krea2_model_paths(params: dict) -> list:
    """Krea-2-Raw: [raw.safetensors, [TE shards], vae.safetensors]."""
    raw = _param(params, "model", "model_path", default=None)
    text_encoder = _param(params, "text_encoder", "text_encoder_path", default=None)
    vae = _param(params, "vae", "vae_path", default=None)
    missing = [
        name
        for name, value in (
            ("model(raw)", raw),
            ("text_encoder", text_encoder),
            ("vae", vae),
        )
        if not _present(value)
    ]
    if missing:
        raise ValueError("krea2_raw missing required path(s): %s" % ", ".join(missing))
    raw_path = Path(str(raw)).expanduser()
    if raw_path.is_dir():
        raw_path = raw_path / "raw.safetensors"
    if not raw_path.is_file() or raw_path.suffix.lower() != ".safetensors":
        raise ValueError("krea2_raw model raw file not found: %s" % raw_path)
    te_path = Path(str(text_encoder)).expanduser()
    if not te_path.is_dir():
        raise ValueError("krea2_raw text_encoder directory not found: %s" % te_path)
    te_files = _safetensors(te_path)
    if not te_files:
        raise ValueError("krea2_raw has no *.safetensors under text_encoder: %s" % te_path)
    vae_path = Path(str(vae)).expanduser()
    if not vae_path.is_file():
        raise ValueError("krea2_raw vae file not found: %s" % vae_path)
    return [str(raw_path), [str(item) for item in te_files], str(vae_path)]


def build_argv(params: dict, out_dir=None) -> list:
    """Translate job params into ``train.py`` argv (no shell quoting).

    Required: model directory (``model``/``model_dir``/``model_path`` or an
    explicit ``model_paths``), dataset directory (``dataset``/``data``/
    ``dataset_base_path``), and an output directory (``out``/``output_path``,
    or the ``out_dir`` argument).
    """
    if not isinstance(params, dict):
        raise ValueError("params must be a dict")
    arch = resolve_arch(params)
    arch_cfg = ARCHES[arch]

    model_dir = _param(params, "model", "model_dir", "model_path", "name_or_path", default=None)
    explicit_paths = _param(params, "model_paths", default=None)
    if arch == "krea2_raw":
        model_paths = krea2_model_paths(params)
    elif explicit_paths is None:
        if not _present(model_dir):
            raise ValueError("missing required param(s): model (diffusers model directory)")
        model_paths = expand_model_paths(model_dir)
    else:
        model_paths = _normalise_model_paths(explicit_paths)

    dataset = _require_param(params, "dataset", "data", "dataset_base_path")
    dataset = _fwd(dataset)
    if not Path(dataset).is_dir():
        raise ValueError("dataset directory does not exist: %s" % dataset)

    output = out_dir if _present(out_dir) else _param(params, "out", "output_path", default=None)
    if not _present(output):
        raise ValueError("missing required param(s): out (output directory)")
    output = _fwd(output)

    prepared = prepare_dataset_metadata(params, output)
    metadata = _fwd(prepared["path"])

    processor = None
    if arch == "qwen_image_21":
        # only qwen_image_21 takes --processor_path; MOSS-Image / Z-Image take
        # --tokenizer_path instead (their parsers have no --processor_path).
        processor = _param(params, "processor_path", "processor", default=None)
        if _present(processor):
            processor = _fwd(processor)
        elif _present(model_dir):
            processor = _fwd(Path(model_dir) / "processor")
        else:
            raise ValueError("missing required param(s): processor_path")
        if not Path(processor).is_dir():
            raise ValueError("processor directory does not exist: %s" % processor)

    rank = _as_int(
        _param(params, "lora_rank", "network.linear", "rank", default=arch_cfg["default_rank"]),
        "lora_rank",
    )
    plan = steps_plan(params, metadata)
    epochs = plan["epochs"]
    lr = _as_float(
        _param(params, "learning_rate", "lr", "train.lr", default=DEFAULT_LR),
        "learning_rate",
    )
    if lr <= 0:
        raise ValueError("learning_rate must be > 0")
    repeat = plan["repeat"]
    resolution = _param(params, "resolution", default=None)
    height = _param(params, "height", default=None)
    width = _param(params, "width", default=None)
    if height is None:
        height = resolution if isinstance(resolution, int) else DEFAULT_SIZE
    if width is None:
        width = resolution if isinstance(resolution, int) else DEFAULT_SIZE
    height = _as_int(height, "height", minimum=64)
    width = _as_int(width, "width", minimum=64)

    task = str(_param(params, "task", default=DEFAULT_TASK))

    if arch in ("moss_image", "z_image", "flux2_klein_9b", "krea2_raw"):
        # MOSS-Image / Z-Image / FLUX.2 / Krea-2: fixed LoRA wiring, local
        # tokenizer, max_pixels.  Krea-2 is a three-path model (raw/TE/VAE).
        base_model = arch_cfg["fixed"]["lora_base_model"]
        target = arch_cfg["fixed"]["lora_target_modules"]
        remove_prefix = str(
            _param(
                params,
                "remove_prefix_in_ckpt",
                default=arch_cfg["fixed"]["remove_prefix_in_ckpt"],
            )
        )
        tokenizer = _param(params, "tokenizer_path", "tokenizer", default=None)
        if not _present(tokenizer) and arch == "krea2_raw":
            tokenizer = _param(params, "text_encoder", default=None)
        if not _present(tokenizer) and _present(model_dir):
            candidate = Path(model_dir) / "tokenizer"
            if candidate.is_dir():
                tokenizer = str(candidate)
        if not _present(tokenizer):
            raise ValueError(
                "%s requires a local tokenizer_path (model_dir/tokenizer); "
                "refusing the hub fallback (would download the base tokenizer)" % arch
            )
        tokenizer = _fwd(tokenizer)
        if not Path(tokenizer).is_dir():
            raise ValueError("tokenizer_path is not a directory: %s" % tokenizer)
        size = size_alignment(params)
        argv = [
            "--dataset_base_path", dataset,
            "--dataset_metadata_path", metadata,
            "--model_paths", json.dumps(model_paths, ensure_ascii=False),
            "--output_path", output,
            "--tokenizer_path", tokenizer,
            "--lora_base_model", base_model,
            "--lora_target_modules", target,
            "--lora_rank", str(rank),
            "--num_epochs", str(epochs),
            "--learning_rate", repr(float(lr)),
            "--dataset_repeat", str(repeat),
            "--max_pixels", str(size["max_pixels"]),
        ]
        if _as_bool(_param(params, "use_gradient_checkpointing", default=None), True):
            argv.append("--use_gradient_checkpointing")
        # Offload default is per-arch: MOSS off (it clears VAE stats), Z-Image on.
        if _as_bool(
            _param(params, "enable_model_cpu_offload", "offload", default=None),
            bool(arch_cfg.get("offload_default", False)),
        ):
            argv.append("--enable_model_cpu_offload")
        workers = _param(params, "dataset_num_workers", "train.dataset_num_workers", default=None)
        if _present(workers):
            argv += ["--dataset_num_workers", str(_as_int(workers, "dataset_num_workers", minimum=0))]
        if _as_bool(_param(params, "find_unused_parameters", default=None), False):
            argv.append("--find_unused_parameters")
        save_steps = _save_steps_value(params)
        if save_steps:  # 0 -> no --save_steps, so epoch-*.safetensors are produced
            argv += ["--save_steps", str(save_steps)]
        grad_accum = _param(
            params, "gradient_accumulation_steps", "train.gradient_accumulation", default=None
        )
        if _present(grad_accum):
            argv += [
                "--gradient_accumulation_steps",
                str(_as_int(grad_accum, "gradient_accumulation_steps")),
            ]
        argv += ["--task", task, "--remove_prefix_in_ckpt", remove_prefix]
        if arch == "krea2_raw":
            argv += ["--find_unused_parameters", "--align_to_opensource_format"]
        _append_resume_arg(argv, params)
        return argv

    # qwen_image_21 (default): argv unchanged (regression-tested field by field)
    base_model = str(
        _param(params, "lora_base_model", default=arch_cfg["fixed"]["lora_base_model"])
    )
    target = _target_modules(_param(params, "lora_target_modules", "target_modules", default=None))
    remove_prefix = str(
        _param(params, "remove_prefix_in_ckpt", default=arch_cfg["fixed"]["remove_prefix_in_ckpt"])
    )

    argv = [
        "--dataset_base_path", dataset,
        "--dataset_metadata_path", metadata,
        "--model_paths", json.dumps(model_paths, ensure_ascii=False),
        "--processor_path", processor,
        "--output_path", output,
        "--lora_base_model", base_model,
        "--lora_target_modules", target,
        "--lora_rank", str(rank),
        "--num_epochs", str(epochs),
        "--learning_rate", repr(float(lr)),
        "--dataset_repeat", str(repeat),
    ]
    if _as_bool(_param(params, "use_gradient_checkpointing", default=None), True):
        argv.append("--use_gradient_checkpointing")
    # 2026-10-23 用户实测：qwen-image-2.1 全量上卡放不下 ⇒ 默认逐层 offload。
    # 显式传 enable_model_cpu_offload=false 可关掉（改回全量上卡）。
    if _as_bool(_param(params, "initialize_model_on_cpu", default=None), False):
        argv.append("--initialize_model_on_cpu")
    if _as_bool(
        _param(params, "enable_model_cpu_offload", "offload", default=None),
        bool(arch_cfg.get("offload_default", False)),
    ):
        argv.append("--enable_model_cpu_offload")
    # qwen_image_21 divides by 32 (pipeline height/width_division_factor); 1020
    # crashes the VAE patchify view, so align up before it reaches the trainer.
    argv += ["--height", str(align32(height)), "--width", str(align32(width))]

    save_steps = _save_steps_value(params)
    if save_steps:  # 0 -> no --save_steps, so epoch-*.safetensors are produced
        argv += ["--save_steps", str(save_steps)]
    grad_accum = _param(
        params, "gradient_accumulation_steps", "train.gradient_accumulation", default=None
    )
    if _present(grad_accum):
        argv += [
            "--gradient_accumulation_steps",
            str(_as_int(grad_accum, "gradient_accumulation_steps")),
        ]
    max_pixels = _param(params, "max_pixels", default=None)
    if _present(max_pixels):
        argv += ["--max_pixels", str(_as_int(max_pixels, "max_pixels"))]
    data_keys = _param(params, "data_file_keys", "train.data_file_keys", default=None)
    if _present(data_keys):
        arg = ",".join(data_keys) if isinstance(data_keys, (list, tuple)) else str(data_keys)
        argv += ["--data_file_keys", arg]

    argv += ["--task", task, "--remove_prefix_in_ckpt", remove_prefix]
    _append_resume_arg(argv, params)
    return argv


# ---------------------------------------------------------------------------
# log parser
# ---------------------------------------------------------------------------


def parse_line(line) -> dict | None:
    """Parse one DiffSynth output line into an event, or None."""
    text = _clean_line(line)
    if not text:
        return None
    match = SAMPLE_BEGIN_RE.match(text)
    if match:
        return {"type": "sample_begin", "step": int(match.group("step"))}
    match = SAMPLE_END_RE.match(text)
    if match:
        return {"type": "sample_end", "step": int(match.group("step"))}
    match = SAMPLE_ERROR_RE.search(text)
    if match:
        return {
            "type": "sample_error",
            "step": int(match.group("step")),
            "message": match.group("msg").strip(),
        }
    match = SAMPLE_LINE_RE.search(text)
    if match:
        return {"type": "sample", "step": int(match.group("step")), "file": match.group("file").strip()}
    match = ARTIFACT_RE.search(text)
    if match:
        return {"type": "artifact", "file": match.group("file")}
    match = PROGRESS_RE.search(text)
    if match:
        spit = SPIT_RE.search(text)
        loss = LOSS_RE.search(text)
        return {
            "type": "progress",
            "step": int(match.group("step")),
            "total": int(match.group("total")),
            "sec_per_it": float(spit.group("spit")) if spit else None,
            "loss": float(loss.group("loss")) if loss else None,
        }
    if ERROR_RE.search(text):
        return {"type": "error", "message": text}
    return None


def _plan_total_steps(metrics: dict):
    """Training total from steps_plan / steps_estimate (None when unknown)."""
    for key in ("steps_plan", "steps_estimate"):
        plan = metrics.get(key)
        if isinstance(plan, dict):
            value = plan.get("total_steps")
            if isinstance(value, int) and value > 0:
                return value
    return None


def _apply_event(metrics: dict, event: dict) -> None:
    kind = event.get("type")
    if kind == "sample_begin":
        # DiffSynth's own denoising tqdm starts now; keep the training step frozen.
        metrics["in_sample"] = True
        metrics["sample_seen"] = True
        metrics["samples_in_flight"] = 1
        metrics["sample_step"] = event.get("step")
        metrics["phase"] = "sample"
    elif kind == "sample_end":
        metrics["in_sample"] = False
        metrics["samples_in_flight"] = 0
        # restore the training progress (never leave step/total at the sample total)
        if event.get("step") is not None:
            metrics["step"] = event.get("step")
        elif metrics.get("train_step") is not None:
            metrics["step"] = metrics.get("train_step")
        if metrics.get("train_total") is not None:
            metrics["total"] = metrics.get("train_total")
        metrics["phase"] = "training"
    elif kind == "progress":
        plan_total = _plan_total_steps(metrics)
        total = event.get("total")
        if metrics.get("in_sample"):
            # inside [ms-sample-begin]..[ms-sample-end]: pure sampling noise
            metrics["dropped_tqdm"] = int(metrics.get("dropped_tqdm") or 0) + 1
            metrics["sample_tqdm"] = {"step": event.get("step"), "total": total}
            return
        if (
            plan_total is not None
            and total != plan_total
            and total is not None
            and total <= SAMPLING_TOTAL_MAX
            and (metrics.get("sampling_enabled") is True or metrics.get("sample_seen") is True)
        ):
            # fallback: a tqdm total that disagrees with the plan and is tiny is
            # sampling noise (covers a lost/late marker). Only when sampling is
            # actually enabled -- a small training total is legitimate otherwise.
            metrics["dropped_tqdm"] = int(metrics.get("dropped_tqdm") or 0) + 1
            return
        metrics["step"] = event.get("step")
        metrics["total"] = total
        metrics["train_step"] = event.get("step")
        metrics["train_total"] = total
        if event.get("sec_per_it") is not None:
            metrics["sec_per_it"] = event.get("sec_per_it")
        if event.get("loss") is not None:
            metrics["loss"] = event.get("loss")
        metrics["phase"] = "training"
    elif kind == "artifact":
        name = event.get("file")
        if name and name not in metrics["artifacts"]:
            metrics["artifacts"].append(name)
        metrics["phase"] = "save"
    elif kind == "error":
        metrics["last_error"] = str(event.get("message"))[:500]
    elif kind == "sample":
        step = event.get("step")
        name = os.path.basename(str(event.get("file") or ""))
        events_list = metrics.setdefault("sample_events", [])
        bucket = next((item for item in events_list if item["step"] == step), None)
        if bucket is None:
            bucket = {"step": step, "files": []}
            events_list.append(bucket)
            events_list.sort(key=lambda item: item["step"])
        if name and all(item.get("file") != name for item in bucket["files"]):
            bucket["files"].append({"file": name, "path": event.get("file")})
        metrics["phase"] = "sample"
    elif kind == "sample_error":
        metrics.setdefault("sample_errors", []).append(
            {"step": event.get("step"), "message": str(event.get("message"))[:300]}
        )


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _resolve_params(job) -> dict:
    job = job if isinstance(job, dict) else {}
    params = job.get("params")
    if not isinstance(params, dict):
        raw = job.get("params_json")
        if isinstance(raw, str):
            try:
                params = json.loads(raw)
            except ValueError:
                params = {}
        elif isinstance(raw, dict):
            params = raw
        else:
            params = job
    if not isinstance(params, dict):
        raise ValueError("job.params must be a dict")
    return params


def _default_python() -> str:
    for candidate in DEFAULT_PYTHON_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return candidate
    return sys.executable


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


ARTIFACT_EXTS = (".safetensors", ".pt", ".ckpt")


def _scan_artifacts(out_dir: Path, since=None) -> list:
    """Return real weight files under ``out`` (excluding ``samples/``).

    Filters (P0: a stale file must never be reported as this run's output):
    * suffix in ``.safetensors/.pt/.ckpt``；
    * ``size > 0``（空占位文件不算）；
    * ``mtime >= since``（``since`` = 本 run 启动时间；None = 不过滤，仅供归档扫描）。
    """
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        return []
    samples_root = out_dir / "samples"
    found = []
    for path in sorted(out_dir.rglob("*")):
        if not path.is_file() or samples_root in path.parents:
            continue
        if not _file_ok(path, ARTIFACT_EXTS, since):
            continue
        try:
            found.append(
                {"file": path.name, "bytes": path.stat().st_size, "path": str(path)}
            )
        except OSError:
            pass
    return found


def _write_metrics(path, payload: dict) -> None:
    if path is None:
        return
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    except OSError:
        pass


def run(
    job,
    log_path,
    *,
    train_command=None,
    stop_file=None,
    metrics_path=None,
    cancel_event=None,
    python=None,
    repo=None,
    train_py=None,
    poll_interval: float = 0.1,
    grace_s: float = 5.0,
) -> dict:
    """Run the DiffSynth trainer selected by ``params["arch"]``.

    train.py is chosen by arch (``ARCHES[arch]["train_py"]``); an incoming
    ``train_py`` that belongs to another arch is ignored and logged.
    ``train_command`` is a test hook: when given it replaces the
    ``[python, -c, RUNNER, repo, train_py]`` prefix and the argv is appended.

    Soft stop: stop_file / cancel_event -> CTRL_BREAK (Windows, new process
    group) or SIGINT -> terminate -> kill. DiffSynth has no resume path here;
    a cancelled run is cancelled.
    """
    params = _resolve_params(job)
    job_id = str(job.get("id") or job.get("job_id") or "") if isinstance(job, dict) else ""
    arch = resolve_arch(params)

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_file = Path(metrics_path) if metrics_path is not None else log_path.with_suffix(".metrics.json")
    stop_path = Path(stop_file) if stop_file is not None else None

    out_dir = _param(params, "out", "output_path", default=None)
    if not _present(out_dir):
        raise ValueError("missing required param(s): out (output directory)")
    resume_info = preflight_resume(params)

    started_ts = time.time()  # artifacts/samples must be mtime >= this run
    params = dict(params)
    prepared = prepare_dataset_metadata(params, out_dir)
    params["dataset_metadata_path"] = prepared["path"]

    warnings = []
    caption_info = caption_coverage(prepared)
    if caption_info["warning"]:
        warnings.append(caption_info["warning"])  # surfaced to metrics + log below
    plan = steps_plan(params, prepared["path"], warnings)
    sample_cfg = sample_config(params, warnings)
    sample_cfg_path = Path(out_dir) / ".ms_sample.json"
    sample_cfg_path.parent.mkdir(parents=True, exist_ok=True)
    sample_cfg_path.write_text(
        json.dumps(sample_cfg, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )

    dataset_dir = _param(params, "dataset", "data", "dataset_base_path", default=None)
    metadata_path = _param(params, "dataset_metadata_path", "metadata", "metadata_path", default=None)
    if not _present(metadata_path) and _present(dataset_dir):
        metadata_path = str(Path(dataset_dir) / "metadata.csv")
    steps = steps_estimate(params, metadata_path)
    if plan["requested_steps"] is not None:
        steps = {
            "dataset_rows": plan["rows"],
            "dataset_repeat": plan["repeat"],
            "num_epochs": plan["epochs"],
            "steps_per_epoch": plan["rows"] * plan["repeat"],
            "total_steps": plan["effective_steps"],
            "source": "steps -> num_epochs=1 * dataset_rows * dataset_repeat",
        }

    repo = Path(repo) if repo is not None else DIFFSYNTH_ROOT
    launch = build_launch_command(
        params,
        out_dir,
        python=python,
        repo=repo,
        train_py=train_py,
        train_command=train_command,
        sample_cfg_path=sample_cfg_path,
    )
    command = launch["command"]
    cwd = launch["cwd"]

    child_env = os.environ.copy()
    child_env.setdefault("PYTHONUTF8", "1")
    child_env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    child_env.setdefault("TOKENIZERS_PARALLELISM", "false")

    metrics = {
        "job_id": job_id,
        "state": "starting",
        "phase": "starting",
        "step": 0,
        "total": None,
        "sec_per_it": None,
        "loss": None,
        "artifacts": [],
        "artifact_files": [],
        "last_error": None,
        "argv": command,
        "output_dir": _fwd(out_dir),
        "arch": arch,
        "resume_from": resume_info.get("resume_from"),
        "resume_rank": resume_info.get("rank"),
        "train_py": launch.get("train_py"),
        "ignored_train_py": launch.get("ignored_train_py"),
        "dataset_metadata": dict(
            prepared,
            no_captions=caption_info["no_captions"],
            no_captions_images=caption_info["no_captions_images"],
        ),
        "no_captions": caption_info["no_captions"],
        "no_captions_images": caption_info["no_captions_images"],
        "caption_ext": caption_info["caption_ext"],
        "size_aligned_from": size_alignment(
            params, 32 if arch == "qwen_image_21" else 16
        )["aligned_from"],
        "python_paths": list(sample_cfg["python_paths"]),
        "sample_every": sample_cfg["every"],
        "sampling_enabled": bool(sample_cfg["every"] > 0 and sample_cfg["prompts"]),
        "sample_prompts": list(sample_cfg["prompts"]),
        "sample_steps": sample_cfg["steps"],
        "sample_seed": sample_cfg["seed"],
        "sample_size": [sample_cfg["width"], sample_cfg["height"]],
        "sample_events": [],
        "sample_errors": [],
        "sample_files_total": 0,
        "in_sample": False,
        "samples_in_flight": 0,
        "sample_step": None,
        "dropped_tqdm": 0,
        "train_step": None,
        "train_total": None,
        "steps_estimate": steps,
        "requested_steps": plan["requested_steps"],
        "effective_steps": plan["effective_steps"],
        "steps_from_repeat": plan["steps_from_repeat"],
        "steps_plan": plan,
        "warnings": list(warnings),
        "events": 0,
        "started_at": _now_utc(),
        "started_ts": round(started_ts, 3),
        "elapsed_s": 0.0,
        "returncode": None,
        "cancelled": False,
    }
    _write_metrics(metrics_file, metrics)

    events = []
    state = "failed"
    returncode = None
    stop_sent = False
    soft_signal = None

    with open(log_path, "w", encoding="utf-8", newline="\n") as log:
        log.write("# diffsynth adapter argv: " + json.dumps(command, ensure_ascii=False) + "\n")
        log.write("# diffsynth adapter cwd: " + str(cwd) + "\n")
        log.write("# diffsynth adapter arch: " + arch + "\n")
        if resume_info.get("resume_from"):
            log.write(
                "[ms-resume] from=%s check=ok arch=%s rank=%s\n"
                % (
                    resume_info["resume_from"],
                    resume_info.get("arch"),
                    resume_info.get("rank"),
                )
            )
        if launch.get("ignored_train_py"):
            log.write(
                "# WARNING: ignored caller train_py=%s (arch=%s uses %s)\n"
                % (launch["ignored_train_py"], arch, launch["train_py"])
            )
        if launch.get("train_py"):
            log.write("# diffsynth adapter train_py: " + str(launch["train_py"]) + "\n")
        if prepared.get("generated"):
            log.write(
                "# generated dataset metadata: %s (n=%d, captions_from=txt=%d,default=%d)\n"
                % (prepared["path"], prepared["n"], prepared["from_txt"], prepared["from_default"])
            )
        log.write("# diffsynth adapter sampling: " + json.dumps(sample_cfg, ensure_ascii=False) + "\n")
        log.write("# diffsynth adapter steps_estimate: " + json.dumps(steps, ensure_ascii=False) + "\n")
        if plan["requested_steps"] is not None:
            log.write(
                "# steps: requested=%d -> epochs=%d repeat=%d effective=%d\n"
                % (
                    plan["requested_steps"],
                    plan["epochs"],
                    plan["repeat"],
                    plan["effective_steps"],
                )
            )
        for warning in warnings:
            log.write("# WARNING: " + warning + "\n")
        log.write("# diffsynth adapter started_at: " + metrics["started_at"] + "\n")
        log.flush()

        if stop_path is not None and stop_path.exists():
            metrics.update(
                state="cancelled",
                cancelled=True,
                error="stop file present before start",
                phase="finished",
            )
            _write_metrics(metrics_file, metrics)
            return {
                "job_id": job_id,
                "state": "cancelled",
                "cancelled": True,
                "returncode": None,
                "metrics": metrics,
                "events": [],
                "argv": command,
                "log_path": str(log_path),
            }

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
        fatal_error = None
        stop_deadline = 0.0
        hard_sent = False

        try:
            proc = subprocess.Popen(
                command,
                cwd=cwd,
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

            reader = threading.Thread(target=_reader, name="diffsynth-adapter-reader", daemon=True)
            reader.start()
            metrics["pid"] = proc.pid
            metrics["state"] = "running"
            _write_metrics(metrics_file, metrics)

            while True:
                now = time.monotonic()
                if not stop_sent and _cancel_requested(stop_path, cancel_event):
                    soft_signal = _soft_stop(proc)
                    stop_sent = True
                    stop_deadline = now + grace_s
                    log.write(
                        "# diffsynth adapter: cancel requested; soft stop sent (%s)\n"
                        % soft_signal
                    )
                    log.flush()
                if stop_sent and not hard_sent and proc.poll() is None and now >= stop_deadline:
                    log.write(
                        "# diffsynth adapter: soft stop grace %.1fs expired; hard stop\n"
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
                    _apply_event(metrics, event)
                    metrics["events"] = len(events)
                    metrics["elapsed_s"] = round(time.monotonic() - started, 3)
                    _write_metrics(metrics_file, metrics)

            if proc.poll() is None:
                _hard_stop(proc)
            returncode = proc.wait(timeout=10)
        except Exception:
            fatal_error = traceback.format_exc()
            log.write("# diffsynth adapter exception:\n" + fatal_error + "\n")
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

        # artifacts from disk: DiffSynth's ModelLogger writes them silently
        scanned = _scan_artifacts(Path(out_dir), since=started_ts)
        metrics["artifact_files"] = scanned
        for item in scanned:
            if item["file"] not in metrics["artifacts"]:
                metrics["artifacts"].append(item["file"])

        # preview images from disk (authoritative: includes bytes)
        scanned_samples = _scan_samples(Path(out_dir), since=started_ts)
        if scanned_samples:
            metrics["sample_events"] = scanned_samples
        metrics["sample_files_total"] = sum(len(item["files"]) for item in metrics["sample_events"])
        metrics["sampling_enabled"] = bool(metrics["sample_every"] > 0 and metrics["sample_prompts"])

        metrics["state"] = state
        metrics["cancelled"] = bool(stop_sent)
        if soft_signal:
            metrics["soft_stop_signal"] = soft_signal
        metrics["phase"] = "finished"
        metrics["finished_at"] = _now_utc()
        _write_metrics(metrics_file, metrics)
        log.write(
            "# diffsynth adapter finished: state=%s returncode=%s step=%s/%s "
            "artifacts=%d samples=%d/%d elapsed=%.3fs\n"
            % (
                state,
                returncode,
                metrics.get("step"),
                metrics.get("total"),
                len(metrics["artifacts"]),
                metrics.get("sample_files_total", 0),
                len(metrics.get("sample_events", [])),
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
        "log_path": str(log_path),
    }
