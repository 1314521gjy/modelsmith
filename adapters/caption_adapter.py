#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local image captioning adapter: llama-server + Qwen3.5/3.8-VL -> same-name .txt.

Why: the user wants **local** auto-captioning for LoRA datasets (no cloud upload).
This module starts `llama-server.exe` **once per job** with the VL model + mmproj,
sends every image through the local OpenAI-style endpoint, and writes a cleaned
single-line caption next to each image (UTF-8, no BOM).

Hard-won facts baked in (A-level, measured by the 维护者 on this machine):
  * engine   E:\\llama-cpp\\llama-server.exe (build 10786, split-DLL build)
  * model    Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf (5.24 GB)
  * mmproj   mmproj-Qwen3.5-9B-...-BF16.gguf (0.86 GB)
  * thinking OFF (`--reasoning off --reasoning-budget 0 --image-min-tokens 1024`
    + per-request `chat_template_kwargs.enable_thinking=false`):
    load 4.0-4.1 s, 0.69-0.87 s/image, 6 images 4.7-5.2 s total,
    VRAM peak 7,777 MiB -> 1,145-1,151 MiB after exit,
    `reasoning_content` length 0 on every response.
  * WHY thinking off matters: without it the model burns the token budget on
    reasoning first; max_tokens=160 left `content` empty with
    finish_reason="length" -- which looks like "vision unsupported" but is a
    budget problem. max_tokens stays >= 1024 and only `content` is used.
  * prompts/post-processing are the B variant the 维护者 measured on 6 real
    images (0/6 first answers over 40 chars; the A variant was 5/6):
    strip boilerplate prefix -> strip trailing punctuation -> if still over
    max_chars: re-ask once -> else fit by WHOLE clauses (never cut mid-clause).

Public API (core wires this module):
    run(job, log_path, *, stop_file=None, cancel_event=None,
        metrics_path=None, metrics_cb=None, ...) -> dict
    build_caption_job(params, log_path, metrics_cb=None, **kwargs) -> dict

No network beyond 127.0.0.1 (the local llama-server). No GPU work happens in
this module itself -- the server does it; this module only spawns/stop it.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# stdlib only, on purpose (packaging constraint):
#   C:\ModelSmith\runtime has NO llama_cpp / torch / transformers / onnxruntime.
#   This adapter talks to the standalone `llama-server.exe` over
#   http://127.0.0.1:<port> -- never `import llama_cpp`.
# --------------------------------------------------------------------------- #
import base64
import json
import os
import re
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------- #
# defaults / constants
# --------------------------------------------------------------------------- #

LLAMA_SERVER_DEFAULT = (
    os.environ.get("MODELSMITH_LLAMA_SERVER") or r"E:\llama-cpp\llama-server.exe"
)
MODEL_DIR_DEFAULT = (
    os.environ.get("MODELSMITH_LLM_MODELS") or r"E:\ComfyUI\ComfyUI\models\LLM"
)

BACKENDS = {
    "qw35-9b": {
        "label": "Qwen3.5-9B-VL",
        "model": os.path.join(
            MODEL_DIR_DEFAULT,
            "Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf",
        ),
        "mmproj": os.path.join(
            MODEL_DIR_DEFAULT,
            "mmproj-Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-BF16.gguf",
        ),
    },
    "qw38-27b": {
        "label": "Qwen3.8-27B-VL",
        "model": os.path.join(
            MODEL_DIR_DEFAULT,
            "Qwen3.8-27B-Uncensored-0bserverx-RVN-IQ3_XXS-multilingual.gguf",
        ),
        "mmproj": os.path.join(
            MODEL_DIR_DEFAULT,
            "mmproj-Qwen3.8-27B-Uncensored-0bserverx-RVN-Q.gguf",
        ),
    },
}
AUTO_CHAIN = ("qw35-9b", "qw38-27b")

IMAGE_EXTS_DEFAULT = (".png", ".jpg", ".jpeg", ".webp")
CAPTION_EXT_DEFAULT = ".txt"
MAX_TOKENS_DEFAULT = 1024          # >=1024 or the thinking model eats the budget
MAX_TOKENS_RETRY = 2048            # one retry when content came back empty
MAX_CHARS_DEFAULT = 40             # user asked for "40 字以内" (len of the caption)
# B 版提示词（维护者 6 张真图实测：首答超 40 字 0/6，直接可用；A 版是 5/6）
PROMPTS = {
    "zh": (
        "用一句简体中文描述这张图，不超过40个字。要求："
        "1) 不要以“图中”“这是一张”“图片显示”开头，直接写主体；"
        "2) 顺序为：主体（是谁/是什么）+ 关键特征（服装、姿态）+ 环境或光线；"
        "3) 只输出这一句话，不要解释、不要标点以外的符号。"
    ),
    "en": (
        "Describe this image in one simplified English sentence, at most 40 characters. "
        "Do not start with \"This image shows\" / \"The picture\". "
        "Order: subject + key features (clothing, pose) + environment or lighting. "
        "Output only that sentence."
    ),
}
# 超长重问时追加的句子（zh 为 维护者 定稿原文；en 为对应英文）
TOO_LONG_SUFFIX = {
    "zh": "这句话太长了，请压到{limit}个字以内，保留主体与场景，只输出这一句。",
    "en": "This sentence is too long; keep it within {limit} characters, "
          "keep the subject and scene, output only this sentence.",
}
TOO_LONG_TARGET = 35               # 重问目标字数（默认 max_chars=40 时的定稿值）
# 重问阈值（用户定稿）：一次重问 = 一次完整模型往返（实测 0.7-1.0 s）。
#   L <= max_chars                    -> 原样
#   max_chars < L <= REASK_RATIO*max_chars -> 不重问，直接整句/标点截断（便宜）
#   L > REASK_RATIO*max_chars ("跑飞") -> 才值得再花一次往返重问
# REASK_RATIO=2 是本次分水岭：把它改成 1 就等于退回"每次超长都重问"的旧行为。
REASK_RATIO = 2
STARTUP_TIMEOUT_DEFAULT = 30.0     # real load 4.0-5.0 s; 30 s = hard cap (then kill)
REQUEST_TIMEOUT_DEFAULT = 180.0    # per image / per batch request
JOB_BUDGET_MIN_S = 120.0           # whole-job budget = max(120, images * 90)
JOB_BUDGET_PER_IMAGE_S = 90.0
SERVER_STOP_GRACE = 5.0
MAX_IMAGE_BYTES = 50 * 1024 * 1024  # > 50 MB -> failed (image_too_large), keep going
# error reason codes (UI maps each to its own copy)
REASON_ENGINE_MISSING = "engine_missing"
REASON_MODEL_MISSING = "model_missing"
REASON_MMPROJ_MISSING = "mmproj_missing"
REASON_MODEL_EMPTY = "model_empty"
REASON_START_TIMEOUT = "engine_start_timeout"
REASON_START_FAILED = "engine_start_failed"
REASON_IMAGE_TIMEOUT = "image_timeout"
REASON_IMAGE_TOO_LARGE = "image_too_large"
REASON_IMAGE_EMPTY = "empty_image"
REASON_BAD_IMAGE = "bad_image"
REASON_EMPTY_CAPTION = "empty_caption"
REASON_WRITE_FAILED = "write_failed"
REASON_REQUEST_ERROR = "request_error"


class CaptionEnvError(RuntimeError):
    """Bad environment; ``reason`` is one of the engine_*/model_* codes."""

    def __init__(self, reason, message, details=None):
        super().__init__(message)
        self.reason = str(reason)
        self.details = list(details or [])

    def to_dict(self):
        return {"reason": self.reason, "message": str(self), "details": self.details}


class CaptionServerError(RuntimeError):
    """llama-server could not start (timeout / early exit)."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = str(reason)
# 关思考（用户实测：reasoning_content 长度 0，单图 5.7s -> 0.75–1.06s）
THINKING_FLAGS = ("--reasoning", "off", "--reasoning-budget", "0")

_MD_RE = re.compile(r"[*_#>]+")
_WS_RE = re.compile(r"\s+")
_PREFIX_RE = re.compile(
    r"^\s*(?:caption|description|prompt|output|描述|图片描述|标题)\s*[:：]\s*", re.I
)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def _present(value) -> bool:
    return value is not None and bool(str(value).strip())


def _int_param(params: dict, key: str, default: int, minimum: int = 0) -> int:
    value = params.get(key)
    if value is None or value == "":
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s must be an integer" % key)
    if value < minimum:
        raise ValueError("%s must be >= %d" % (key, minimum))
    return value


def _normalise_caption_ext(value) -> str:
    if not _present(value):
        return CAPTION_EXT_DEFAULT
    text = str(value).strip().lower()
    if not text.startswith("."):
        text = "." + text
    if not re.match(r"^\.[A-Za-z0-9_]+$", text):
        raise ValueError(
            "caption_ext must be a single extension like '.txt', got %r" % (value,)
        )
    return text


def _normalise_image_exts(value) -> tuple:
    if value is None:
        return IMAGE_EXTS_DEFAULT
    if isinstance(value, str):
        raw = [part for part in re.split(r"[,;\s]+", value) if part]
    elif isinstance(value, (list, tuple)):
        raw = [str(part) for part in value if str(part).strip()]
    else:
        raise ValueError("image_exts must be a string or a list of strings")
    exts = []
    for part in raw:
        text = part.strip().lower()
        if not text.startswith("."):
            text = "." + text
        if not re.match(r"^\.[A-Za-z0-9]+$", text):
            raise ValueError("bad image extension %r" % (part,))
        exts.append(text)
    return tuple(exts) or IMAGE_EXTS_DEFAULT


# 套话前缀（后处理第 1 层）：命中就剥掉；剥完为空则保留原句
BOILERPLATE_RE = re.compile(
    r"^(图中是|图中|这是一张|这是一幅|图片显示|照片显示|画面中|图里|本图|该图)[，,、:：\s]*"
)
# 训练字幕不要句号（后处理第 2 层）
TRAILING_PUNCT = "。．.！!？?  "
# when a cut lands between clauses, also drop the clause separator
CUT_TRIM = "，,、；;。．.！!？?  "
# 分句（后处理第 3 层）：在标点之后切开，保留标点本身
CLAUSE_RE = re.compile(r"(?<=[，,、；;。])")


def strip_boilerplate(text: str) -> str:
    """Layer 1: drop '图中/这是一张/图片显示...' prefixes; keep the original if empty."""
    stripped = BOILERPLATE_RE.sub("", text, count=1).strip()
    return stripped if stripped else text


def strip_trailing_punct(text: str) -> str:
    """Layer 2: no sentence-final punctuation in a training caption."""
    return text.rstrip(TRAILING_PUNCT)


def clean_caption(text) -> str:
    """Markdown/quotes/whitespace cleanup + layer 1 (boilerplate) + layer 2 (punct)."""
    if not isinstance(text, str):
        return ""
    value = text.replace("\r", " ").replace("\n", " ")
    value = re.sub(r"```[A-Za-z0-9_-]*", " ", value)
    value = value.replace("`", "")
    value = _PREFIX_RE.sub("", value)
    value = _MD_RE.sub("", value)
    value = value.strip().strip("\"'“”‘’「」")
    value = _WS_RE.sub(" ", value).strip()
    value = strip_boilerplate(value)
    value = strip_trailing_punct(value)
    return value


def fit_caption(text: str, max_chars: int):
    """Layer 3: fit into max_chars by WHOLE clauses; returns (text, cut).

    Rule (from the 维护者's 6-image comparison): never cut inside a clause --
    accumulate ``re.split(r"(?<=[，,、；;。])", s)`` pieces greedily. Only when the
    very first clause alone is too long do we fall back to a hard cut, and even
    then we cut at punctuation when one exists.
    """
    text = clean_caption(text or "")
    if len(text) <= max_chars:
        return text, False
    clauses = [part for part in CLAUSE_RE.split(text) if part]
    accumulated = ""
    for clause in clauses:
        if len(accumulated) + len(clause) <= max_chars:
            accumulated += clause
        else:
            break
    accumulated = accumulated.strip().rstrip(CUT_TRIM)
    if accumulated and len(accumulated) <= max_chars:
        return accumulated, True
    # first clause alone is longer than max_chars -> hard cut, but break at punctuation
    head = text[:max_chars]
    boundary = -1
    for position, char in enumerate(head):
        if char in "，,、；;。":
            boundary = position
    if boundary > 0:
        candidate = head[:boundary].strip().rstrip(CUT_TRIM)
        if candidate:
            return candidate, True
    tail = head.strip().rstrip(CUT_TRIM) or head
    return tail[:max_chars], True


def _too_long_prompt(prompt: str, lang: str, max_chars: int) -> str:
    suffix = TOO_LONG_SUFFIX.get(lang) or TOO_LONG_SUFFIX["zh"]
    target = TOO_LONG_TARGET if int(max_chars) >= TOO_LONG_TARGET else max(1, int(max_chars) - 5)
    return "%s %s" % (prompt, suffix.format(limit=int(target)))


def _write_caption_atomic(path: Path, text: str) -> None:
    """UTF-8 without BOM, single line, atomic replace (never leaves a half file)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n")
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _is_cancelled(stop_file, cancel_event) -> bool:
    if cancel_event is not None and cancel_event.is_set():
        return True
    if stop_file is not None:
        try:
            return Path(stop_file).exists()
        except OSError:
            return False
    return False


def _now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def _tail(text, limit: int = 400) -> str:
    text = str(text or "").strip()
    return text[-limit:] if len(text) > limit else text


def _kill_tree(proc: subprocess.Popen) -> None:
    """terminate -> taskkill /T /F (Windows) -> kill; never raise."""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=SERVER_STOP_GRACE)
    except subprocess.TimeoutExpired:
        pass
    except OSError:
        pass
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        if proc.poll() is None:
            proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _encode_image(path: Path):
    suffix = path.suffix.lower()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(suffix, "application/octet-stream")
    data = Path(path).read_bytes()
    return mime, base64.b64encode(data).decode("ascii")


# --------------------------------------------------------------------------- #
# llama-server lifecycle
# --------------------------------------------------------------------------- #


def _server_argv(server_exe, model, mmproj, port, params, reasoning=True) -> list:
    ngl = _int_param(params, "ngl", 99, minimum=0)
    ctx = _int_param(params, "ctx", 4096, minimum=128)
    image_min_tokens = _int_param(params, "image_min_tokens", 1024, minimum=0)
    argv = [
        str(server_exe),
        "-m",
        str(model),
        "--mmproj",
        str(mmproj),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--no-webui",
        "-ngl",
        str(ngl),
        "-c",
        str(ctx),
    ]
    if reasoning:
        argv += ["--reasoning", "off", "--reasoning-budget", "0"]
    argv += ["--image-min-tokens", str(image_min_tokens)]
    return argv


def _expand_server_cmd(server_cmd, port, model, mmproj) -> list:
    """Test hook: expand {port}/{model}/{mmproj}; append --port when absent."""
    if isinstance(server_cmd, str):
        cmd = [server_cmd]
    else:
        cmd = [str(part) for part in server_cmd]
    substitutions = {"{port}": str(port), "{model}": str(model), "{mmproj}": str(mmproj)}
    expanded = []
    has_port = False
    for part in cmd:
        for token, value in substitutions.items():
            if token in part:
                part = part.replace(token, value)
                if token == "{port}":
                    has_port = True
        expanded.append(part)
    if not has_port:
        expanded += ["--port", str(port)]
    return expanded


def _start_server(argv, log_path, log_handle, startup_timeout, port, proc_box):
    """Start llama-server and wait for /health; returns the Popen."""
    proc = subprocess.Popen(
        argv,
        cwd=str(Path(argv[0]).parent if Path(argv[0]).suffix else Path.cwd()),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    proc_box["proc"] = proc
    globals()["_LAST_SERVER_PROC"] = proc  # test hook: assert poll() is not None after cleanup
    tail_lines = []

    def _drain():
        try:
            assert proc.stdout is not None
            for line in iter(proc.stdout.readline, ""):
                stripped = line.rstrip("\r\n")
                if stripped:
                    tail_lines.append(stripped)
                    del tail_lines[:-40]
                    if log_handle is not None:
                        log_handle.write("[server] " + stripped + "\n")
                        log_handle.flush()
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=_drain, name="caption-server-log", daemon=True)
    reader.start()
    deadline = time.time() + float(startup_timeout)
    url = "http://127.0.0.1:%d/health" % port
    while time.time() < deadline:
        if proc.poll() is not None:
            raise CaptionServerError(
                REASON_START_FAILED,
                "llama-server exited during startup (rc=%s): %s"
                % (proc.returncode, _tail(" | ".join(tail_lines))),
            )
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                body = response.read(2048).decode("utf-8", errors="replace")
            if response.status == 200 and (
                '"ok"' in body or "ready" in body.lower() or body.strip().startswith("{")
            ):
                return proc
        except (urllib.error.URLError, ConnectionError, OSError, ValueError):
            pass
        time.sleep(0.1)
    raise CaptionServerError(
        REASON_START_TIMEOUT,
        "llama-server did not become ready within %.0fs: %s"
        % (float(startup_timeout), _tail(" | ".join(tail_lines))),
    )


# --------------------------------------------------------------------------- #
# one caption request
# --------------------------------------------------------------------------- #


def _post_chat(port, model_label, prompt, images, max_tokens, timeout):
    """One /v1/chat/completions call; ``images`` = [(mime, base64), ...] in order."""
    content = [{"type": "text", "text": prompt}]
    for mime, image_b64 in images:
        content.append(
            {"type": "image_url",
             "image_url": {"url": "data:%s;base64,%s" % (mime, image_b64)}}
        )
    payload = {
        "model": model_label,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": int(max_tokens),
        "temperature": 0.2,
        "stream": False,
        # double insurance for the thinking model: server flags + per-request template kwarg
        "chat_template_kwargs": {"enable_thinking": False},
    }
    request = urllib.request.Request(
        "http://127.0.0.1:%d/v1/chat/completions" % int(port),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=float(timeout)) as response:
        raw = response.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("no choices in response: %s" % _tail(raw))
    choice = choices[0] or {}
    message = choice.get("message") or {}
    content_value = message.get("content")
    reasoning = message.get("reasoning_content")
    return {
        "content": content_value if isinstance(content_value, str) else "",
        "reasoning_present": bool(reasoning),
        "reasoning_len": len(reasoning) if isinstance(reasoning, str) else 0,
        "finish_reason": choice.get("finish_reason"),
        "max_tokens": int(max_tokens),
    }


def _post_caption(port, model_label, prompt, mime, image_b64, max_tokens, timeout):
    return _post_chat(port, model_label, prompt, [(mime, image_b64)], max_tokens, timeout)


# --------------------------------------------------------------------------- #
# batch (unified) mode: N images in ONE request, strict "1: ..." lines
# --------------------------------------------------------------------------- #

BATCH_PROMPTS = {
    "zh": (
        "下面依次给你 {n} 张图，它们是同一个人（或同一主体）的训练集，编号 1..{n}。\n"
        "请为每一张各写一句简体中文描述，要求：\n"
        "1) 每句不超过 40 个字；\n"
        "2) {n} 句必须使用同一个主体称呼（例如都用“赤膊男子”），不要换同义词；\n"
        "3) {n} 句共用同一套场景与服装词汇（如“健身房”“光线明亮”），保持用词一致；\n"
        "4) 顺序：主体 + 本图特有的姿势或服装 + 环境；\n"
        "5) 只输出 {n} 行，格式严格为 `1: 描述` 到 `{n}: 描述`，不要任何解释。"
    ),
    "en": (
        "You will see {n} images in order; they are a training set of the same subject, numbered 1..{n}.\n"
        "Write one simplified English sentence for each image:\n"
        "1) at most 40 characters per sentence;\n"
        "2) all {n} sentences must use the SAME subject term (do not switch synonyms);\n"
        "3) all {n} sentences share the same scene/clothing vocabulary;\n"
        "4) order: subject + this image's pose or clothing + environment;\n"
        "5) output exactly {n} lines in the form `1: description` .. `{n}: description`, nothing else."
    ),
}
SUBJECT_HINT = {"zh": "统一用『{term}』称呼主体。", "en": "Use \"{term}\" as the subject term."}
BATCH_LINE_RE = re.compile(r"^\s*(\d+)\s*[:：]\s*(.+)$")
CJK_RUN_RE = re.compile(r"^[\s“”\"']*([\u4e00-\u9fff]{2,})")
SUBJECT_TRIM_CHARS = "在的着地是了和与及、 　"
BATCH_CTX_DEFAULT = 16384          # 6 images ~= 6,482 prompt tokens (维护者 measured)
BATCH_IMAGE_TOKEN_BUDGET = 500     # prompt text + template slack
BATCH_SIZE_DEFAULT = 6             # unified mode is the default (维护者 measured 6 images)
BATCH_SIZE_MAX = 8


def batch_prompt(lang: str, n: int, subject_term=None) -> str:
    text = (BATCH_PROMPTS.get(lang) or BATCH_PROMPTS["zh"]).format(n=int(n))
    if subject_term:
        hint = SUBJECT_HINT.get(lang) or SUBJECT_HINT["zh"]
        text += " " + hint.format(term=subject_term)
    return text


def parse_batch_lines(text: str, n: int):
    """Return (mapping {1-based index: caption}, dropped_lines).

    Mapping is by the NUMBER on the line (not by line order); indices outside
    1..n and repeated numbers are dropped and counted.
    """
    mapping = {}
    dropped = 0
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        match = BATCH_LINE_RE.match(line)
        if not match:
            dropped += 1
            continue
        index = int(match.group(1))
        caption = match.group(2).strip()
        if 1 <= index <= int(n) and index not in mapping and caption:
            mapping[index] = caption
        else:
            dropped += 1
    return mapping, dropped


def extract_subject_term(captions) -> str | None:
    """Heuristic: the 2-6 char CJK subject shared by >= half of the first batch.

    Uses the longest common prefix of the leading CJK runs, then trims particles
    (在/的/着/...). Returns None when it cannot find a stable 2-6 char term.
    """
    runs = []
    for caption in captions:
        match = CJK_RUN_RE.match(caption or "")
        if match:
            runs.append(match.group(1))
    if len(runs) < 2:
        return None
    need = max(2, (len(captions) + 1) // 2)
    base = runs[0]
    for run in runs[1:need]:
        common = ""
        for left, right in zip(base, run):
            if left != right:
                break
            common += left
        base = common
        if not base:
            return None
    term = base.strip().strip(SUBJECT_TRIM_CHARS)
    if 2 <= len(term) <= 6:
        return term
    term = term[:6].strip().strip(SUBJECT_TRIM_CHARS)
    return term if 2 <= len(term) <= 6 else None


# --------------------------------------------------------------------------- #
# job
# --------------------------------------------------------------------------- #


def _resolve_params(job) -> dict:
    if not isinstance(job, dict):
        return {}
    params = job.get("params")
    if isinstance(params, dict):
        return dict(params)
    raw = job.get("params_json")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}
    return dict(job)


def _pick_backends(params) -> list:
    """Return candidate backend dicts (auto = 9B then 27B)."""
    explicit_model = params.get("model")
    explicit_mmproj = params.get("mmproj")
    if _present(explicit_model) or _present(explicit_mmproj):
        return [
            {
                "key": "custom",
                "label": str(params.get("backend") or "custom"),
                "model": str(explicit_model or ""),
                "mmproj": str(explicit_mmproj or ""),
            }
        ]
    backend = str(params.get("backend") or "auto").strip()
    if backend == "auto":
        keys = list(AUTO_CHAIN)
    elif backend in BACKENDS:
        keys = [backend]
    else:
        raise ValueError(
            "backend must be one of auto/%s, got %r" % ("/".join(sorted(BACKENDS)), backend)
        )
    return [dict(BACKENDS[key], key=key) for key in keys]


def _validate_backend(candidate, server_exe) -> list:
    """Return [(reason_code, message)] for every environment problem found."""
    problems = []
    if not Path(str(server_exe)).is_file():
        problems.append((REASON_ENGINE_MISSING, "engine missing: %s" % server_exe))
    model = candidate.get("model")
    if not _present(model) or not Path(str(model)).is_file():
        problems.append((REASON_MODEL_MISSING, "model missing: %s" % model))
    elif Path(str(model)).stat().st_size <= 0:
        problems.append((REASON_MODEL_EMPTY, "model is 0 bytes: %s" % model))
    mmproj = candidate.get("mmproj")
    if not _present(mmproj) or not Path(str(mmproj)).is_file():
        problems.append((REASON_MMPROJ_MISSING, "mmproj missing: %s" % mmproj))
    return problems


def _list_images(directory: Path, exts, limit: int) -> list:
    images = [
        entry
        for entry in sorted(directory.iterdir(), key=lambda item: item.name.lower())
        if entry.is_file() and entry.suffix.lower() in exts
    ]
    if limit > 0:
        images = images[:limit]
    return images


def _track_reasoning(metrics: dict, answer: dict) -> None:
    """Record thinking-model leakage: any reasoning_content => thinking_still_on."""
    _track_reasoning_chars(metrics, answer.get("reasoning_len") or 0)


def _track_reasoning_chars(metrics: dict, length) -> None:
    length = int(length or 0)
    metrics["reasoning_chars_total"] = int(metrics.get("reasoning_chars_total") or 0) + length
    if length > 0:
        metrics["thinking_still_on"] = True
        metrics["thinking_not_off"] = True


def _classify_request_error(exc) -> str:
    """Map a request exception to a stable reason code (UI copy depends on it)."""
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return REASON_IMAGE_TIMEOUT
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return REASON_IMAGE_TIMEOUT
    return REASON_REQUEST_ERROR


def _magic_ok(path: Path, head: bytes) -> bool:
    """Signature check by extension: a text file renamed to .png must not be sent."""
    suffix = path.suffix.lower()
    if suffix == ".webp":
        return len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WEBP"
    if suffix in (".jpg", ".jpeg"):
        return head[:3] == b"\xff\xd8\xff"
    if suffix == ".png":
        return head[:4] == b"\x89PNG"
    return True  # unknown extension: no magic contract


def _image_problem(image) -> str | None:
    """Pre-request checks: 0-byte / too-large / bad magic / unreadable -> reason code.

    A bad image must be kept OUT of the batch request: llama-server answers 400
    for the whole batch when one image is undecodable.
    """
    path = Path(image)
    try:
        stat = path.stat()
    except OSError:
        return REASON_REQUEST_ERROR
    if stat.st_size <= 0:
        return REASON_IMAGE_EMPTY
    if stat.st_size > MAX_IMAGE_BYTES:
        return REASON_IMAGE_TOO_LARGE
    try:
        with open(str(path), "rb") as handle:
            head = handle.read(12)
    except OSError:
        return REASON_REQUEST_ERROR
    if not _magic_ok(path, head):
        return REASON_BAD_IMAGE
    return None


def _record_failure(metrics: dict, image, reason: str, detail: str = "") -> None:
    metrics["failed"] += 1
    codes = metrics.setdefault("failed_codes", {})
    codes[reason] = int(codes.get(reason) or 0) + 1
    message = "%s: %s" % (Path(image).name if image is not None else "-", reason)
    if detail:
        message += ": " + _tail(detail, 160)
    metrics["errors"].append(message)


def _write_caption_safe(target, caption, trigger, metrics, log) -> bool:
    """Atomic write; on failure count failed + errors[] and return False."""
    text = ("%s, %s" % (trigger, caption)) if trigger else caption
    try:
        _write_caption_atomic(target, text)
    except OSError as exc:
        _record_failure(metrics, target, REASON_WRITE_FAILED, str(exc))
        log.write("# write failed %s: %s\n" % (target, _tail(str(exc), 200)))
        log.flush()
        return False
    metrics["written"] += 1
    log.write("# wrote %s (%d chars)\n" % (Path(target).name, len(text)))
    log.flush()
    return True


def _install_signal_handlers(cancel_event):
    """SIGINT/SIGTERM -> set cancel_event so the loop stops between images."""
    previous = {}
    if threading.current_thread() is not threading.main_thread():
        return previous
    for name in ("SIGINT", "SIGTERM"):
        number = getattr(signal, name, None)
        if number is None:
            continue

        def _handler(signum, frame, _event=cancel_event):
            _event.set()

        try:
            previous[number] = signal.signal(number, _handler)
        except (ValueError, OSError):
            pass
    return previous


def _restore_signal_handlers(previous) -> None:
    for number, handler in (previous or {}).items():
        try:
            signal.signal(number, handler)
        except (ValueError, OSError):
            pass


def _caption_one(port, label, prompt, mime, image_b64, max_tokens, max_chars, lang,
                 request_timeout, allow_reask=True):
    """One image: request -> empty retry -> (only if runaway) re-ask -> fit.

    Threshold rule (user's final spec): a re-ask costs a full round trip
    (0.7-1.0 s measured), so a slight overflow (<= REASK_RATIO * max_chars) is
    just cut with punctuation; only a runaway caption (> 2x) is worth another
    model call.
    """
    answer = _post_caption(port, label, prompt, mime, image_b64, max_tokens, request_timeout)
    reasoning_len = int(answer.get("reasoning_len") or 0)
    caption = clean_caption(answer.get("content"))
    empty_retried = False
    truncated = False
    reasked = 0
    if not caption:
        empty_retried = True
        answer = _post_caption(port, label, prompt, mime, image_b64,
                               max(max_tokens, MAX_TOKENS_RETRY), request_timeout)
        reasoning_len += int(answer.get("reasoning_len") or 0)
        caption = clean_caption(answer.get("content"))
    if allow_reask and caption and len(caption) > REASK_RATIO * max_chars:
        reasked = 1
        long_answer = _post_caption(
            port, label, _too_long_prompt(prompt, lang, max_chars), mime, image_b64,
            max(max_tokens, MAX_TOKENS_RETRY), request_timeout,
        )
        reasoning_len += int(long_answer.get("reasoning_len") or 0)
        retried = clean_caption(long_answer.get("content"))
        if retried:
            caption = retried
    if caption and len(caption) > max_chars:
        caption, cut = fit_caption(caption, max_chars)
        truncated = bool(cut)
    return {
        "caption": caption,
        "reasoning_len": reasoning_len,
        "empty_retried": empty_retried,
        "truncated": truncated,
        "reasked": reasked,
    }


def _on_progress(metrics_cb, metrics: dict, total: int, done: int) -> None:
    if metrics_cb is None:
        return
    payload = {
        "done": int(done),
        "total": int(total),
        "written": int(metrics.get("written") or 0),
        "skipped": int(metrics.get("skipped") or 0),
        "failed": int(metrics.get("failed") or 0),
        "state": metrics.get("state"),
    }
    try:
        metrics_cb(payload)
    except Exception:  # noqa: BLE001 - a bad callback must not kill the job
        pass


def _write_metrics(path, payload) -> None:
    if path is None:
        return
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        pass


def run(
    job,
    log_path,
    *,
    stop_file=None,
    cancel_event=None,
    metrics_path=None,
    metrics_cb=None,
    server_cmd=None,
    startup_timeout=None,
    request_timeout=None,
    server_exe=None,
):
    """Caption every image under ``params.dir``; one llama-server per call."""
    params = _resolve_params(job)
    job_id = str(job.get("id") or job.get("job_id") or "") if isinstance(job, dict) else ""

    directory_raw = params.get("dir")
    if not _present(directory_raw):
        raise ValueError("missing required param(s): dir (image directory)")
    directory = Path(str(directory_raw)).expanduser()
    if not directory.is_absolute():
        raise ValueError("dir must be an absolute path, got %r" % directory_raw)
    if not directory.is_dir():
        raise ValueError("dir does not exist: %s" % directory)

    exts = _normalise_image_exts(params.get("image_exts"))
    caption_ext = _normalise_caption_ext(params.get("caption_ext"))
    overwrite = bool(params.get("overwrite", False))
    limit = _int_param(params, "limit", 0, minimum=0)
    lang = str(params.get("lang") or "zh").strip().lower()
    if lang not in PROMPTS:
        raise ValueError("lang must be 'en' or 'zh', got %r" % (params.get("lang"),))
    prompt = str(params.get("prompt") or PROMPTS[lang])
    trigger = str(params.get("trigger") or "").strip()
    max_tokens = _int_param(params, "max_tokens", MAX_TOKENS_DEFAULT, minimum=64)
    if max_tokens < 1024:
        max_tokens = 1024  # thinking model: below this the content comes back empty
    max_chars = _int_param(params, "max_chars", MAX_CHARS_DEFAULT, minimum=1)
    startup_timeout = float(
        startup_timeout
        if startup_timeout is not None
        else params.get("startup_timeout") or STARTUP_TIMEOUT_DEFAULT
    )
    request_timeout = float(
        request_timeout
        if request_timeout is not None
        else params.get("request_timeout") or REQUEST_TIMEOUT_DEFAULT
    )
    exe = str(
        server_exe
        or params.get("engine")
        or params.get("server_exe")
        or LLAMA_SERVER_DEFAULT
    )

    images = _list_images(directory, exts, limit)
    if not images:
        # nothing to caption must be an explicit FAILED result, never a fake success
        empty_metrics = {
            "job_id": job_id,
            "state": "failed",
            "phase": "finished",
            "images_total": 0,
            "written": 0,
            "skipped": 0,
            "failed": 0,
            "no_caption_left": 0,
            "truncated": 0,
            "reasked": 0,
            "failed_codes": {},
            "errors": ["目录里没有可处理的图片"],
            "error": "目录里没有可处理的图片",
            "cancelled": False,
            "seconds": 0.0,
            "per_image_seconds": None,
            "vram_peak_mib": None,
            "started_at": _now_utc(),
            "finished_at": _now_utc(),
        }
        _write_metrics(metrics_path, empty_metrics)
        try:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            Path(log_path).write_text(
                "# caption adapter: 目录里没有可处理的图片: %s\n" % directory,
                encoding="utf-8",
            )
        except OSError:
            pass
        return {
            "job_id": job_id,
            "state": "failed",
            "cancelled": False,
            "metrics": empty_metrics,
            "log_path": str(log_path),
        }

    # batch (unified) mode is the default; batch_size=1 = the old per-image path.
    requested_batch = _int_param(params, "batch_size", BATCH_SIZE_DEFAULT, minimum=1)
    if requested_batch > BATCH_SIZE_MAX:
        raise ValueError(
            "batch_size must be between 1 and %d, got %d"
            % (BATCH_SIZE_MAX, requested_batch)
        )
    base_ctx = _int_param(
        params,
        "ctx",
        BATCH_CTX_DEFAULT if requested_batch > 1 else 4096,
        minimum=2048,
    )
    image_min_tokens = _int_param(params, "image_min_tokens", 1024, minimum=0)
    # never hard-stuff the context: images * image_min_tokens must fit in ctx
    if image_min_tokens:
        budget_slots = (base_ctx - BATCH_IMAGE_TOKEN_BUDGET) // image_min_tokens
    else:
        budget_slots = requested_batch
    effective_batch = max(1, min(requested_batch, int(budget_slots)))
    params["ctx"] = base_ctx
    params["batch_size"] = effective_batch

    started = time.time()
    # Decide what actually needs work BEFORE starting llama-server: a directory whose
    # captions are all present must succeed instantly (no 8 GB VRAM held for nothing).
    skipped = 0
    pending = []
    for image in images:
        if (image.with_suffix(caption_ext)).is_file() and not overwrite:
            skipped += 1
            continue
        pending.append(image)
    if not pending:
        message = ("全部已有字幕，已跳过 %d 张（勾选“覆盖已有字幕”可重写）" % skipped)
        idle_metrics = {
            "job_id": job_id,
            "state": "succeeded",
            "phase": "finished",
            "images_total": len(images),
            "mode": "batch" if effective_batch > 1 else "per_image",
            "batch_size": effective_batch,
            "written": 0,
            "skipped": skipped,
            "failed": 0,
            "no_caption_left": 0,
            "truncated": 0,
            "reasked": 0,
            "failed_codes": {},
            "errors": [],
            "message": message,
            "cancelled": False,
            "seconds": round(time.time() - started, 3),
            "per_image_seconds": None,
            "vram_peak_mib": None,
            "started_at": _now_utc(),
            "finished_at": _now_utc(),
        }
        _write_metrics(metrics_path, idle_metrics)
        _on_progress(metrics_cb, idle_metrics, len(images), skipped)
        try:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            Path(log_path).write_text(
                "# caption adapter: %s\n" % message, encoding="utf-8"
            )
        except OSError:
            pass
        return {
            "job_id": job_id,
            "state": "succeeded",
            "cancelled": False,
            "metrics": idle_metrics,
            "log_path": str(log_path),
        }

    candidates = _pick_backends(params)
    if server_cmd is None and params.get("server_cmd"):
        server_cmd = params.get("server_cmd")
    if server_cmd is None:
        ready = []
        problems = []
        for candidate in candidates:
            found = _validate_backend(candidate, exe)
            if found:
                problems.extend("%s: %s" % (reason, message) for reason, message in found)
            else:
                ready.append(candidate)
        if not ready:
            first_reason = found[0][0] if found else REASON_ENGINE_MISSING
            raise CaptionEnvError(
                first_reason,
                "caption backend environment missing: %s" % ", ".join(problems),
                details=problems,
            )
        candidates = ready

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    metrics = {
        "job_id": job_id,
        "state": "starting",
        "caption_backend": candidates[0]["label"] if candidates else "?",
        "backend_key": candidates[0]["key"] if candidates else "?",
        "model": candidates[0].get("model") if candidates else None,
        "mmproj": candidates[0].get("mmproj") if candidates else None,
        "prompt": prompt,
        "lang": lang,
        "max_chars": max_chars,
        "caption_ext": caption_ext,
        "images_total": len(images),
        "mode": "batch" if effective_batch > 1 else "per_image",
        "batch_size": effective_batch,
        "batch_reduced_from": requested_batch if effective_batch < requested_batch else None,
        "batches": None,
        "fallback_individual": 0,
        "dropped_lines": 0,
        "subject_term": None,
        "written": 0,
        "skipped": skipped,
        "failed": 0,
        "no_caption_left": 0,
        "truncated": 0,
        "reasked": 0,
        "thinking_still_on": False,
        "thinking_not_off": False,
        "reasoning_chars_total": 0,
        "failed_codes": {},
        "batch_request_errors": 0,
        "stopped_by_budget": False,
        "budget_seconds": None,
        "seconds": 0.0,
        "per_image_seconds": None,
        "vram_peak_mib": None,
        "errors": [],
        "cancelled": False,
        "started_at": _now_utc(),
    }
    _write_metrics(metrics_path, metrics)
    _on_progress(metrics_cb, metrics, len(images), 0)

    budget_seconds = float(
        params.get("budget_s")
        or max(JOB_BUDGET_MIN_S, len(images) * JOB_BUDGET_PER_IMAGE_S)
    )
    metrics["budget_seconds"] = budget_seconds
    cancel_event = cancel_event if cancel_event is not None else threading.Event()
    previous_handlers = _install_signal_handlers(cancel_event)
    state = "succeeded"
    request_seconds = []
    cancelled = False
    last_error = None
    proc = None
    proc_box = {}
    with open(log_path, "w", encoding="utf-8", newline="\n") as log:
        log.write("# caption adapter started_at: %s\n" % metrics["started_at"])
        log.write("# caption adapter dir: %s\n" % directory)
        log.write("# caption adapter images=%d ext=%s overwrite=%s\n"
                  % (len(images), caption_ext, overwrite))
        log.write("# caption adapter prompt: %s\n" % prompt)
        log.flush()
        try:
            backend_errors = []
            backend_reasons = []
            server = None
            thinking_flags_supported = True
            for candidate in candidates:
                port = _free_port()
                if server_cmd is not None:
                    argv = _expand_server_cmd(
                        server_cmd, port, candidate.get("model"), candidate.get("mmproj")
                    )
                else:
                    argv = _server_argv(exe, candidate["model"], candidate["mmproj"], port, params)
                log.write("# caption adapter server argv: %s\n" % json.dumps(argv, ensure_ascii=False))
                log.flush()
                try:
                    server = _start_server(
                        argv, log_path, log, startup_timeout, port, proc_box
                    )
                except RuntimeError as exc:
                    reason = getattr(exc, "reason", REASON_START_FAILED)
                    # Older llama-server builds may not know --reasoning/--reasoning-budget.
                    # Fall back to the plain argv once, and detect thinking from responses.
                    if (
                        server_cmd is None
                        and "reasoning" in " ".join(argv)
                        and reason == REASON_START_FAILED
                    ):
                        plain = _server_argv(
                            exe, candidate["model"], candidate["mmproj"], port, params,
                            reasoning=False,
                        )
                        log.write(
                            "# caption adapter: server rejected reasoning flags; retrying without them\n"
                        )
                        log.flush()
                        _kill_tree(proc_box.get("proc"))
                        try:
                            server = _start_server(
                                plain, log_path, log, startup_timeout, port, proc_box
                            )
                            thinking_flags_supported = False
                            metrics["thinking_flags_supported"] = False
                        except RuntimeError as exc2:
                            backend_errors.append(str(exc2))
                            backend_reasons.append(getattr(exc2, "reason", REASON_START_FAILED))
                            log.write("# caption adapter backend %s failed: %s\n"
                                      % (candidate["key"], _tail(str(exc2), 300)))
                            log.flush()
                            _kill_tree(proc_box.get("proc"))
                            continue
                    else:
                        backend_errors.append(str(exc))
                        backend_reasons.append(reason)
                        log.write("# caption adapter backend %s failed: %s\n"
                                  % (candidate["key"], _tail(str(exc), 300)))
                        log.flush()
                        _kill_tree(proc_box.get("proc"))
                        continue
                metrics.update(
                    caption_backend=candidate["label"],
                    backend_key=candidate["key"],
                    model=candidate.get("model"),
                    mmproj=candidate.get("mmproj"),
                    thinking_flags_supported=thinking_flags_supported,
                    server_pid=getattr(proc_box.get("proc"), "pid", None),
                    port=port,
                    state="running",
                )
                break
            if server is None:
                raise CaptionEnvError(
                    backend_reasons[0] if backend_reasons else REASON_START_FAILED,
                    "no caption backend could start: %s"
                    % _tail(" | ".join(backend_errors), 500),
                    details=backend_errors,
                )
            proc = proc_box["proc"]
            label = metrics["caption_backend"]
            done = 0
            # pending/skipped were decided BEFORE the server started (see above)
            if effective_batch <= 1:
                metrics.update(mode="per_image", batch_size=1, batches=len(pending))
                log.write("# mode=per_image images=%d\n" % len(pending))
                log.flush()
                for image in pending:
                    if time.time() - started > budget_seconds:
                        metrics["stopped_by_budget"] = True
                        cancelled = True
                        state = "cancelled"
                        log.write("# budget reached (%.0fs), stopping before %s\n"
                                  % (budget_seconds, image.name))
                        log.flush()
                        break
                    if _is_cancelled(stop_file, cancel_event):
                        cancelled = True
                        log.write("# caption adapter cancelled before %s\n" % image.name)
                        log.flush()
                        break
                    target = image.with_suffix(caption_ext)
                    started_one = time.time()
                    try:
                        problem = _image_problem(image)
                        if problem:
                            _record_failure(metrics, image, problem)
                            log.write("# skip %s: %s\n" % (image.name, problem))
                            log.flush()
                        else:
                            mime, image_b64 = _encode_image(image)
                            one = _caption_one(port, label, prompt, mime, image_b64,
                                               max_tokens, max_chars, lang, request_timeout)
                            _track_reasoning_chars(metrics, one["reasoning_len"])
                            metrics["reasked"] += int(one.get("reasked") or 0)
                            if one["truncated"]:
                                metrics["truncated"] += 1
                            if not one["caption"]:
                                _record_failure(metrics, image, REASON_EMPTY_CAPTION)
                                metrics["no_caption_left"] += 1
                                log.write("# empty caption for %s after retry\n" % image.name)
                                log.flush()
                            else:
                                _write_caption_safe(
                                    target, one["caption"], trigger, metrics, log
                                )
                    except Exception as exc:  # noqa: BLE001 - one bad image must not stop the batch
                        _record_failure(metrics, image, _classify_request_error(exc),
                                        str(exc))
                        log.write("# failed %s: %s\n" % (image.name, _tail(str(exc), 300)))
                        log.flush()
                    finally:
                        request_seconds.append(round(time.time() - started_one, 3))
                        done += 1
                        metrics["seconds"] = round(time.time() - started, 3)
                        _write_metrics(metrics_path, metrics)
                        _on_progress(metrics_cb, metrics, len(images), done)
            else:
                chunks = [pending[i:i + effective_batch]
                          for i in range(0, len(pending), effective_batch)]
                metrics.update(
                    mode="batch",
                    batch_size=effective_batch,
                    batches=len(chunks),
                    fallback_individual=0,
                    dropped_lines=0,
                    subject_term=None,
                )
                log.write("# mode=batch batch_size=%d chunks=%d\n"
                          % (effective_batch, len(chunks)))
                log.flush()
                subject_term = None
                for chunk_index, chunk in enumerate(chunks):
                    if time.time() - started > budget_seconds:
                        metrics["stopped_by_budget"] = True
                        cancelled = True
                        state = "cancelled"
                        log.write("# budget reached (%.0fs), stopping before batch %d\n"
                                  % (budget_seconds, chunk_index + 1))
                        log.flush()
                        break
                    if _is_cancelled(stop_file, cancel_event):
                        cancelled = True
                        log.write("# caption adapter cancelled before batch %d\n"
                                  % (chunk_index + 1))
                        log.flush()
                        break
                    started_one = time.time()
                    # Pre-filter: a bad image must never enter the batch request,
                    # otherwise llama-server answers 400 for the WHOLE batch.
                    good = []
                    for image in chunk:
                        problem = _image_problem(image)
                        if problem:
                            _record_failure(metrics, image, problem)
                            log.write("# skip %s: %s\n" % (image.name, problem))
                            log.flush()
                            done += 1
                            metrics["seconds"] = round(time.time() - started, 3)
                            _write_metrics(metrics_path, metrics)
                            _on_progress(metrics_cb, metrics, len(images), done)
                        else:
                            good.append(image)
                    if not good:
                        request_seconds.append(round(time.time() - started_one, 3))
                        continue
                    try:
                        encoded = [_encode_image(image) for image in good]
                        chunk_prompt = batch_prompt(lang, len(good), subject_term)
                        answer = _post_chat(
                            port, label, chunk_prompt, encoded,
                            max(max_tokens, 200 * len(good)), request_timeout,
                        )
                        _track_reasoning_chars(metrics, answer.get("reasoning_len") or 0)
                        mapping, dropped = parse_batch_lines(answer.get("content"), len(good))
                        metrics["dropped_lines"] += dropped
                        if dropped:
                            metrics["errors"].append(
                                "batch %d: dropped %d line(s)" % (chunk_index + 1, dropped)
                            )
                        if subject_term is None and chunk_index == 0 and len(mapping) >= 2:
                            subject_term = extract_subject_term(
                                [mapping[number] for number in sorted(mapping)]
                            )
                            metrics["subject_term"] = subject_term
                            if subject_term:
                                log.write("# subject_term=%s\n" % subject_term)
                                log.flush()
                        for offset, image in enumerate(good):
                            number = offset + 1
                            target = image.with_suffix(caption_ext)
                            caption = clean_caption(mapping.get(number) or "")
                            if not caption:
                                # missing line -> per-image fallback (never fail the batch)
                                metrics["fallback_individual"] += 1
                                one = _caption_one(port, label, prompt, *_encode_image(image),
                                                   max_tokens, max_chars, lang, request_timeout)
                                _track_reasoning_chars(metrics, one["reasoning_len"])
                                metrics["reasked"] += int(one.get("reasked") or 0)
                                if one["truncated"]:
                                    metrics["truncated"] += 1
                                caption = one["caption"]
                            elif len(caption) > max_chars:
                                # same threshold rule as the per-image path: a slight
                                # overflow (<= 2x) is cut, only a runaway caption (> 2x)
                                # is worth another full round trip.
                                if len(caption) > REASK_RATIO * max_chars:
                                    metrics["reasked"] += 1
                                    one = _caption_one(
                                        port, label,
                                        _too_long_prompt(prompt, lang, max_chars),
                                        *_encode_image(image), max_tokens, max_chars, lang,
                                        request_timeout, allow_reask=False,
                                    )
                                    _track_reasoning_chars(metrics, one["reasoning_len"])
                                    if one["caption"]:
                                        caption = one["caption"]
                                    if one["truncated"]:
                                        metrics["truncated"] += 1
                                if len(caption) > max_chars:
                                    caption, cut = fit_caption(caption, max_chars)
                                    if cut:
                                        metrics["truncated"] += 1
                            if not caption:
                                _record_failure(metrics, image, REASON_EMPTY_CAPTION)
                                metrics["no_caption_left"] += 1
                                log.write("# empty caption for %s\n" % image.name)
                                log.flush()
                            else:
                                _write_caption_safe(target, caption, trigger, metrics, log)
                            done += 1
                            metrics["seconds"] = round(time.time() - started, 3)
                            _write_metrics(metrics_path, metrics)
                            _on_progress(metrics_cb, metrics, len(images), done)
                    except Exception as exc:  # noqa: BLE001 - batch failed -> per-image fallback
                        reason = _classify_request_error(exc)
                        metrics["batch_request_errors"] = int(
                            metrics.get("batch_request_errors") or 0
                        ) + 1
                        metrics["errors"].append(
                            "batch %d: %s: %s"
                            % (chunk_index + 1, reason, _tail(str(exc), 160))
                        )
                        log.write("# batch %d failed: %s\n"
                                  % (chunk_index + 1, _tail(str(exc), 300)))
                        log.flush()
                        for image in good:
                            if _is_cancelled(stop_file, cancel_event):
                                cancelled = True
                                break
                            metrics["fallback_individual"] += 1
                            try:
                                one = _caption_one(
                                    port, label, prompt, *_encode_image(image),
                                    max_tokens, max_chars, lang, request_timeout,
                                )
                                _track_reasoning_chars(metrics, one["reasoning_len"])
                                metrics["reasked"] += int(one.get("reasked") or 0)
                                if one["truncated"]:
                                    metrics["truncated"] += 1
                                if one["caption"]:
                                    _write_caption_safe(
                                        image.with_suffix(caption_ext),
                                        one["caption"], trigger, metrics, log,
                                    )
                                else:
                                    _record_failure(metrics, image, REASON_EMPTY_CAPTION)
                                    metrics["no_caption_left"] += 1
                            except Exception as exc2:  # noqa: BLE001
                                _record_failure(metrics, image,
                                                _classify_request_error(exc2), str(exc2))
                            finally:
                                done += 1
                                metrics["seconds"] = round(time.time() - started, 3)
                                _write_metrics(metrics_path, metrics)
                                _on_progress(metrics_cb, metrics, len(images), done)
                    finally:
                        request_seconds.append(round(time.time() - started_one, 3))
            if cancelled:
                state = "cancelled"
            elif metrics["written"] > 0:
                state = "succeeded"
            elif metrics["failed"] == 0 and metrics["skipped"] > 0:
                # Nothing failed, nothing left to do: all images already had captions.
                # Reporting "failed" here was a false red (and contradicted failed=0).
                state = "succeeded"
                metrics["message"] = (
                    "全部已有字幕，已跳过 %d 张（勾选“覆盖已有字幕”可重写）"
                    % metrics["skipped"]
                )
            elif metrics["failed"] > 0:
                state = "failed"
                codes = metrics.get("failed_codes") or {}
                summary = ", ".join(
                    "%s=%d" % (key, codes[key]) for key in sorted(codes)
                )
                last_error = "所有图片均失败（%d 张）" % metrics["failed"]
                if summary:
                    last_error += " — " + summary
            else:
                state = "failed"
                last_error = "所有图片均失败（%d 张）" % metrics["images_total"]
        except (KeyboardInterrupt, SystemExit) as exc:
            # interrupted from outside: same cleanup path (the finally below kills the
            # server and writes metrics), then re-raise so the caller sees it.
            state = "cancelled"
            cancelled = True
            last_error = "interrupted: %s" % type(exc).__name__
            metrics["errors"].append(last_error)
            log.write("# caption adapter interrupted: %s\n" % last_error)
            log.flush()
            raise
        except Exception as exc:
            state = "failed"
            last_error = "%s: %s" % (type(exc).__name__, exc)
            metrics["errors"].append(_tail(last_error, 300))
            log.write("# caption adapter error: %s\n" % _tail(last_error, 500))
            log.flush()
            raise
        finally:
            _restore_signal_handlers(previous_handlers)
            _kill_tree(proc_box.get("proc"))
            metrics["state"] = state
            metrics["cancelled"] = cancelled
            metrics["seconds"] = round(time.time() - started, 3)
            metrics["finished_at"] = _now_utc()
            processed = metrics["written"] + metrics["skipped"] + metrics["failed"]
            if metrics["seconds"] and processed:
                metrics["per_image_seconds"] = round(metrics["seconds"] / processed, 3)
            if request_seconds:
                metrics["per_request_seconds"] = round(
                    sum(request_seconds) / len(request_seconds), 3
                )
            if last_error:
                metrics["error"] = last_error
            _write_metrics(metrics_path, metrics)
            _on_progress(metrics_cb, metrics, len(images), processed)
            log.write(
                "# caption adapter done state=%s mode=%s written=%d skipped=%d failed=%d "
                "fallback_individual=%d dropped_lines=%d subject_term=%s seconds=%.2f\n"
                % (state, metrics.get("mode"), metrics["written"], metrics["skipped"],
                   metrics["failed"], metrics.get("fallback_individual") or 0,
                   metrics.get("dropped_lines") or 0, metrics.get("subject_term"),
                   metrics["seconds"])
            )
            log.flush()
    return {
        "job_id": job_id,
        "state": state,
        "cancelled": cancelled,
        "metrics": metrics,
        "log_path": str(log_path),
    }


def build_caption_job(params, log_path, metrics_cb=None, **kwargs):
    """Params-first entry point (the interface the core wires to)."""
    return run(
        {"params": dict(params or {})},
        log_path,
        metrics_cb=metrics_cb,
        **kwargs,
    )
