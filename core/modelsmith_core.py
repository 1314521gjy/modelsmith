#!/usr/bin/env python3
r"""ModelSmith local core service (MVP).

Single-file, Python 3.10+, standard library only.

Responsibilities in this MVP:
  * bind to 127.0.0.1 only (never 0.0.0.0)
  * SQLite job table in WAL mode
  * serial scheduler + GPU file lock
  * fake training worker (subprocess, 20 steps by default, cancellable)
  * byte-offset log tailing
  * static hosting for design/prototype/index.html
  * token auth for /api/* (and /env/doctor)

Run:
    python core\modelsmith_core.py
    python core\modelsmith_core.py --port 0 --data-dir C:\temp\ms-core

The service intentionally has no third-party dependencies.
"""

from __future__ import annotations

import ctypes
import os

# ===========================================================================
# [CM-01B] WINDOWS JOB OBJECT: kill descendants when core dies
# 职责: KILL_ON_JOB_CLOSE;失败只记日志并降级到 reap_orphans。
# ===========================================================================


if os.name == "nt":
    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOB_BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class _JOB_EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOB_BASIC_LIMIT),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


class _LegacyWindowsJobGuard:
    """Assign this process to a kill-on-close job so OS reaps descendants."""

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self, enabled=True):
        self.enabled = False
        self.reason = None
        self.handle = None
        if os.name != "nt":
            self.reason = "not windows"
            return
        if not enabled:
            self.reason = "disabled by --no-job-object"
            return
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.restype = ctypes.c_void_p
            kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.AssignProcessToJobObject.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
            ]
            kernel32.SetInformationJobObject.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_uint32,
            ]
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                raise OSError(ctypes.get_last_error(), "CreateJobObjectW")
            info = _JOB_EXTENDED_LIMIT()
            info.BasicLimitInformation.LimitFlags = (
                self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            if not kernel32.SetInformationJobObject(
                handle,
                self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                raise OSError(ctypes.get_last_error(), "SetInformationJobObject")
            if not kernel32.AssignProcessToJobObject(
                handle, kernel32.GetCurrentProcess()
            ):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject")
            self.handle = handle
            self.enabled = True
        except Exception as exc:
            self.reason = "%s: %s" % (type(exc).__name__, exc)
            if self.handle:
                try:
                    ctypes.WinDLL("kernel32").CloseHandle(self.handle)
                except Exception:
                    pass
                self.handle = None


JOB_GUARD = None


# ===========================================================================
# [CM-01] MODULE / imports / constants / module map
# 职责: 服务常量、阈值、正则、路径默认值、终端态/状态枚举。
# 上游: start-modelsmith.cmd / build_parser(); 下游: 全模块。
# 路由与参数链说明见各函数注释与 core/README.md。
# ===========================================================================

import argparse
import ctypes
import hmac
import hashlib
import importlib
import json
import mimetypes
import os
import queue
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    _core_dir = str(Path(__file__).resolve().parent)
    if _core_dir not in sys.path:
        sys.path.insert(0, _core_dir)
    if sys.platform == "win32":
        import platform_win as _platform
    else:
        import platform_posix as _platform
    _PLATFORM_IMPORT_ERROR = None
except Exception as _platform_exc:
    _platform = None
    _PLATFORM_IMPORT_ERROR = "%s: %s" % (type(_platform_exc).__name__, _platform_exc)


def kill_process_tree(pid: int) -> None:
    if _platform is not None:
        return _platform.kill_tree(pid)
    return None


def system_memory_status(probe=None):
    if _platform is not None:
        return _platform.memory_info(probe)
    return {"ram_used_mib": None, "ram_total_mib": None, "ram_pct": None}


GpuLock = _platform.GpuLock if _platform is not None else None


VERSION = "0.1.0"
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
SCHEMA_VERSION = 1
MAX_BODY_BYTES = 1024 * 1024
MAX_LOG_CHUNK_BYTES = 256 * 1024
SOFT_STOP_GRACE_S = 15.0
MODELSMITH_HOME = Path(
    os.environ.get("MODELSMITH_HOME") or Path(__file__).resolve().parent.parent
).resolve()
PROJECT_ROOT = MODELSMITH_HOME


def _external_default(env_name, *parts):
    value = os.environ.get(env_name)
    if value:
        return value
    if os.name == "nt":
        return "E:" + "\\" + "\\".join(parts)
    if parts and str(parts[-1]).lower().endswith("python.exe"):
        return sys.executable or "/usr/bin/python3"
    return str(Path("/opt/ModelSmith").joinpath(*parts))


_COMFY_PYTHON = _external_default(
    "MODELSMITH_COMFY_PYTHON", "ComfyUI", "python_embeded", "python.exe"
)
_LLM_MODELS = Path(
    os.environ.get("MODELSMITH_LLM_MODELS")
    or _external_default("MODELSMITH_LLM_MODELS", "ComfyUI", "ComfyUI", "models", "LLM")
)
DEFAULT_LAYA_PYTHON = _COMFY_PYTHON
DEFAULT_LAYA_SRC = str(MODELSMITH_HOME / "third_party" / "laya")
DEFAULT_LLM_PYTHON = _COMFY_PYTHON
DEFAULT_AITK_PYTHON = _COMFY_PYTHON
DEFAULT_AITK_LIBS = str(MODELSMITH_HOME / ".aitk-pylibs")
DEFAULT_AITK_SRC = "third_party/ai-toolkit"
AITK_ENV_MISSING = "ai-toolkit environment missing (未安装或不完整)"
DEFAULT_DIFFSYNTH_PYTHON = _COMFY_PYTHON
DEFAULT_DIFFSYNTH_SRC = "third_party/DiffSynth-Studio"
DIFFSYNTH_TRAIN_REL = "examples/qwen_image_21/model_training/train.py"
DIFFSYNTH_ENV_MISSING = "diffsynth environment missing (未安装或不完整)"
DEFAULT_LF_PYTHON = _COMFY_PYTHON
DEFAULT_LF_LIBS = ".pylibs-lf"
DEFAULT_LF_SRC = "third_party/LlamaFactory"
LF_CLI_REL = "src/llamafactory/cli.py"
LF_ENV_MISSING = "LlamaFactory environment missing (未安装或不完整)"
LF_STOP_GRACE_S = 10.0
LF_TEMPLATE_DEFAULT = "qwen"
DEFAULT_MODELS_DIR = str(MODELSMITH_HOME / "models")
MODEL_KINDS = ("diffusion", "diffusion_single", "llm", "laya", "unknown")
MODEL_COMPONENT_DIRS = (
    "vae",
    "text_encoder",
    "transformer",
    "tokenizer",
    "scheduler",
)
SELECTABLE_MODEL_KINDS = ("diffusion", "diffusion_single", "llm", "laya")
MODEL_SCAN_TTL_S = 60.0
MODEL_MAX_BYTES = 200 * 1024**3
MODEL_MAX_FILES = 5000
MODEL_MAX_DEPTH = 3
MODEL_LINK_MAX_FILES = 2000
MODEL_LINK_MAX_BYTES = 80 * 1024**3
DEFAULT_CAPTION_ENGINE = os.environ.get(
    "MODELSMITH_LLAMA_SERVER"
) or _external_default("MODELSMITH_LLAMA_SERVER", "llama-cpp", "llama-server.exe")
CAPTION_BACKENDS = {
    "qw35-9b": {
        "model_glob": str(_LLM_MODELS / "Qwen3.5-9B-*.gguf"),
        "mmproj_glob": str(_LLM_MODELS / "mmproj-Qwen3.5-9B-*.gguf"),
    },
    "qw38-27b": {
        "model_glob": str(_LLM_MODELS / "Qwen3.8-27B-*.gguf"),
        "mmproj_glob": str(_LLM_MODELS / "mmproj-Qwen3.8-27B-*.gguf"),
    },
}
LLM_STOP_GRACE_S = 5.0
LLM_STEP_RE = re.compile(
    r"\bstep\s+(?P<step>\d+)\s+loss\s+"
    r"(?P<loss>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)"
    r"\s+lr\s+(?P<lr>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)",
    re.IGNORECASE,
)
LLM_ERROR_RE = re.compile(
    r"^\s*(?:error|Error|ERROR|SystemExit|RuntimeError|ValueError|"
    r"FileNotFoundError):\s*(?P<message>.*)$"
)
LLM_REPORT_RE = re.compile(r"^\s*report:\s*(?P<path>.+)$", re.IGNORECASE)
LAYA_PYTHON_DEFAULT_HELP = DEFAULT_LAYA_PYTHON
LAYA_SRC_DEFAULT_HELP = DEFAULT_LAYA_SRC
TERMINAL_STATES = ("succeeded", "failed", "cancelled", "interrupted")
ALL_STATES = ("queued", "starting", "running") + TERMINAL_STATES
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
KIND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

_IS_WINDOWS = os.name == "nt"
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _IS_WINDOWS else 0
_CREATE_NEW_PROCESS_GROUP = (
    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if _IS_WINDOWS else 0
)


# ===========================================================================
# [CM-02] UTILITIES: ISO time / JSON / atomic write / pid / port / token / id
# 职责: 无业务状态的通用工具；所有写盘都在这里或 JobStore.
# 上游: Scheduler/CoreApp/CoreHandler; 下游: stdlib.
# ===========================================================================

# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def utc_now() -> str:
    """ISO-8601 UTC timestamp, millisecond precision, always ends with Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def json_text(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-%d-%d" % (os.getpid(), threading.get_ident()))
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def file_size(path) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def is_pid_alive(pid) -> bool:
    """Best-effort liveness check. Never kills anything."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if _IS_WINDOWS:
        import ctypes

        synchronize = 0x00100000
        wait_timeout = 0x00000102
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def port_listening(port: int, host: str = HOST, timeout: float = 0.35) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, int(port))) == 0
    except OSError:
        return False
    finally:
        sock.close()


def default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "ModelSmith"


def load_or_create_token(token_path: Path) -> str:
    token_path = Path(token_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    if token_path.is_file():
        token = token_path.read_text(encoding="utf-8").strip()
        if len(token) >= 16:
            return token
    token = secrets.token_urlsafe(32)
    atomic_write_text(token_path, token + "\n")
    try:
        os.chmod(token_path, 0o600)
    except OSError:
        pass
    return token


def new_job_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return "job-%s-%s" % (stamp, secrets.token_hex(4))


def _tail(text: str, limit: int = 400) -> str:
    text = (text or "").strip()
    return text[-limit:] if len(text) > limit else text


# ===========================================================================
# [CM-03] MODEL REPOSITORY SCAN: content classify / size cap / complete / id
# 职责: 只扫 <project>\models; 输出 id/kind/complete/missing/class_name。
# 上游: CoreApp.models()/GET /api/models, _create_job 完整性预检;
# 下游: 只读文件系统, resolve_model_reference() 供四个 kind 解析相对 id.
# ===========================================================================

def _is_reparse_point(path: Path) -> bool:
    try:
        if Path(path).is_symlink():
            return True
        st = os.stat(path, follow_symlinks=False)
        attrs = getattr(st, "st_file_attributes", 0)
        flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(attrs & flag)
    except OSError:
        return True


def _visible_subdirs(path: Path):
    try:
        entries = sorted(os.scandir(path), key=lambda entry: entry.name.lower())
    except OSError:
        return []
    result = []
    for entry in entries:
        if entry.name.startswith(".") or entry.name == "__pycache__":
            continue
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        if _is_reparse_point(Path(entry.path)):
            continue
        result.append(Path(entry.path))
    return result


def _model_arch_hint(names) -> str:
    if not names:
        return ""
    stem = Path(sorted(names)[0]).stem
    match = re.match(r"[A-Za-z]+", stem)
    return (match.group(0) if match else stem).lower()


def _model_dir_stats(path: Path, follow_links=False):
    """Return (size_bytes, files, skipped_too_large); linked targets are capped."""
    total = 0
    files = 0
    stack = [Path(path)]
    seen = set()
    max_files = MODEL_LINK_MAX_FILES if follow_links else MODEL_MAX_FILES
    max_bytes = MODEL_LINK_MAX_BYTES if follow_links else MODEL_MAX_BYTES
    while stack:
        current = stack.pop()
        try:
            real = os.path.realpath(str(current))
        except OSError:
            real = str(current)
        if real in seen:
            continue
        seen.add(real)
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                is_link = entry.is_symlink() or _is_reparse_point(Path(entry.path))
                if entry.is_dir(follow_symlinks=follow_links):
                    if is_link and not follow_links:
                        continue
                    stack.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=follow_links):
                    continue
                files += 1
                total += entry.stat(follow_symlinks=follow_links).st_size
                if files > max_files or total > max_bytes:
                    return total, files, True
            except OSError:
                continue
    return total, files, False


def _classify_model_dir(path: Path):
    path = Path(path)
    try:
        if (path / "model_index.json").is_file():
            return "diffusion", None
        if (path / "rl_agent_config.json").is_file() or (
            (path / "model.safetensors").is_file()
            and (path / "encoder").is_dir()
            and (path / "tokenizer").is_dir()
        ):
            return "laya", None
        shards = [
            entry.name
            for entry in os.scandir(path)
            if entry.is_file(follow_symlinks=False)
            and entry.name.lower().endswith(".safetensors")
        ]
        if (path / "config.json").is_file() and shards:
            return "llm", None
        if shards:
            return "diffusion_single", _model_arch_hint(shards)
    except OSError:
        pass
    return "unknown", None


def _component_links(path: Path) -> dict:
    result = {}
    if not Path(path).is_dir():
        return result
    try:
        entries = list(os.scandir(path))
    except OSError:
        return result
    for entry in entries:
        try:
            if entry.name.startswith(".") or not _is_reparse_point(Path(entry.path)):
                continue
            if not entry.is_dir(follow_symlinks=True):
                continue
            result[entry.name] = os.path.realpath(entry.path)
        except OSError:
            continue
    return result


def scan_models(root):
    """Content-detect models under ``root`` only; returns (models, warning)."""
    root = Path(root)
    if not root.is_dir():
        return [], "models dir not found: %s" % root
    used_ids = set()
    models = []

    def add_model(
        model_id: str,
        name: str,
        path: Path,
        kind: str,
        arch_hint,
        size_bytes: int,
        files: int,
        skipped: bool = False,
    ) -> None:
        base_id = model_id or name or "model"
        unique = base_id
        suffix = 2
        while unique in used_ids:
            unique = "%s-%d" % (base_id, suffix)
            suffix += 1
        used_ids.add(unique)
        link_targets = _component_links(Path(path))
        if link_targets:
            size_bytes, files, skipped = _model_dir_stats(Path(path), True)
        item = {
            "id": unique,
            "name": name or base_id,
            "path": str(path.resolve()),
            "kind": kind,
            "size_bytes": int(size_bytes),
            "files": int(files),
            "arch_hint": arch_hint,
            "linked": bool(link_targets),
            "link_targets": link_targets,
            "size_capped": bool(skipped),
        }
        class_name = None
        if Path(path).is_dir():
            try:
                index_path = Path(path) / "model_index.json"
                if index_path.is_file():
                    index = json.loads(index_path.read_text(encoding="utf-8"))
                    if isinstance(index, dict):
                        class_name = index.get("_class_name")
            except (OSError, ValueError):
                class_name = None
        item["class_name"] = class_name
        if kind == "unknown":
            if not Path(path).is_dir():
                item["why"] = "not a directory"
            elif not (Path(path) / "model_index.json").is_file():
                item["why"] = "no model_index.json / no recognizable *.safetensors"
            else:
                item["why"] = "unrecognized model layout"
        complete = True
        missing = []
        if kind == "diffusion" and Path(path).is_dir():
            index_path = Path(path) / "model_index.json"
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
                if not isinstance(index, dict):
                    raise ValueError("model_index.json is not an object")
                for key, value in index.items():
                    if key.startswith("_"):
                        continue
                    # Pipeline-level ConfigSpec values (e.g. flux2
                    # is_distilled=True) are not components and may live
                    # outside model_index's component directories.
                    if not isinstance(value, (list, tuple, str)):
                        continue
                    # diffusers values are ["<library>", "<class>"], not paths.
                    # The component directory is always the key name.
                    if not (Path(path) / key).exists():
                        missing.append(key)
            except (OSError, ValueError):
                complete = False
                missing = ["model_index.json"]
            if missing:
                complete = False
        item["complete"] = bool(complete)
        item["missing"] = missing
        if skipped:
            item["skipped_too_large"] = True
        models.append(item)

    def add_directory(path: Path, depth: int) -> bool:
        kind, arch_hint = _classify_model_dir(path)
        if kind != "unknown":
            size, files, skipped = _model_dir_stats(path, bool(_component_links(path)))
            add_model(
                path.name,
                path.name,
                path,
                kind,
                arch_hint,
                size,
                files,
                skipped,
            )
            return True
        children = _visible_subdirs(path)
        found = False
        if depth < MODEL_MAX_DEPTH:
            for child in children:
                if child.name.lower() in MODEL_COMPONENT_DIRS:
                    continue
                if add_directory(child, depth + 1):
                    found = True
        if not found and (not children or depth >= MODEL_MAX_DEPTH):
            size, files, skipped = _model_dir_stats(path)
            add_model(
                path.name,
                path.name,
                path,
                "unknown",
                None,
                size,
                files,
                skipped,
            )
        return found

    # Top-level single-file diffusion weights: models\qwen-image.safetensors
    try:
        root_files = sorted(
            entry.name
            for entry in os.scandir(root)
            if entry.is_file(follow_symlinks=False)
            and entry.name.lower().endswith(".safetensors")
        )
    except OSError:
        root_files = []
    for filename in root_files:
        file_path = root / filename
        try:
            size = file_path.stat().st_size
        except OSError:
            size = 0
        add_model(
            Path(filename).stem,
            Path(filename).stem,
            file_path,
            "diffusion_single",
            _model_arch_hint([filename]),
            size,
            1,
        )

    for child in _visible_subdirs(root):
        if child.name.lower() in MODEL_COMPONENT_DIRS:
            continue
        add_directory(child, 1)

    models.sort(
        key=lambda item: (
            0 if item.get("complete") else 1,
            item["kind"],
            item["name"].lower(),
            item["id"],
        )
    )
    return models, None


def models_tree_signature(root):
    """Cheap directory mtime signature (depth <=3); None if root missing."""
    root = Path(root)
    if not root.is_dir():
        return None
    signature = []
    stack = [(root, 0)]
    while stack:
        path, depth = stack.pop()
        try:
            st = os.stat(path, follow_symlinks=False)
        except OSError:
            continue
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = ""
        signature.append((rel, st.st_mtime_ns))
        if depth >= MODEL_MAX_DEPTH:
            continue
        for child in _visible_subdirs(path):
            stack.append((child, depth + 1))
    return tuple(sorted(signature))


def resolve_model_reference(value, models_dir, models=None):
    """Resolve a models-dir relative id to its absolute path; absolute passthrough."""
    if not isinstance(value, str) or not value.strip():
        return value
    path = Path(value).expanduser()
    if path.is_absolute():
        return str(value)
    if models is None:
        models, _warning = scan_models(Path(models_dir))
    for item in models or []:
        if value in (item.get("id"), item.get("name")):
            if item.get("kind") in SELECTABLE_MODEL_KINDS:
                return item["path"]
            return str(value)
    candidate = Path(models_dir) / value
    try:
        if candidate.exists():
            return str(candidate.resolve())
    except OSError:
        pass
    return str(value)


# ===========================================================================
# [CM-04] ADMISSION / POLICY HELPERS: picker, data hint, grounding, early stop,
#          VRAM decision, diffsynth python_paths resolution.
# 职责: 无状态判定函数（可直接单测）；结果由 CoreHandler/Scheduler 消费。
# 上游: CoreHandler._handle_* / CoreApp.vram_admission / Scheduler;
# 下游: stdlib + 文件系统只读。
# ===========================================================================

def native_pick(mode: str, title: str, extensions, timeout_s: float = 180.0) -> dict:
    """Show a native Windows file/folder picker; returns {ok,path} or reason."""
    if os.name != "nt":
        return {"ok": False, "reason": "no_desktop"}
    if mode == "file":
        exts = [str(ext).lstrip(".") for ext in (extensions or [])]
        patterns = ";".join("*." + ext for ext in exts) if exts else "*.*"
        label = ";".join("*." + ext for ext in exts) if exts else "All files (*.*)"
        filter_text = "%s|%s|All files (*.*)|*.*" % (label, patterns)
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$f=New-Object System.Windows.Forms.Form;"
            "$f.TopMost=$true;$f.ShowInTaskbar=$false;$f.Opacity=0;"
            "$f.StartPosition=[System.Windows.Forms.FormStartPosition]::CenterScreen;$f.Show();"
            "$d=New-Object System.Windows.Forms.OpenFileDialog;"
            "$d.Title=%s;"
            "$d.Filter=%s;"
            "$r=$d.ShowDialog($f);$f.Close();"
            "if($r -eq [System.Windows.Forms.DialogResult]::OK)"
            "{[Console]::Out.WriteLine($d.FileName);exit 0}else{exit 2}"
            % (
                json.dumps(title or "Select a file"),
                json.dumps(filter_text),
            )
        )
    else:
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$f=New-Object System.Windows.Forms.Form;"
            "$f.TopMost=$true;$f.ShowInTaskbar=$false;$f.Opacity=0;"
            "$f.StartPosition=[System.Windows.Forms.FormStartPosition]::CenterScreen;$f.Show();"
            "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
            "$d.Description=%s;"
            "$r=$d.ShowDialog($f);$f.Close();"
            "if($r -eq [System.Windows.Forms.DialogResult]::OK)"
            "{[Console]::Out.WriteLine($d.SelectedPath);exit 0}else{exit 2}"
            % json.dumps(title or "Select a folder")
        )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
            capture_output=True,
            timeout=timeout_s,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "dialog_timeout"}
    except OSError:
        return {"ok": False, "reason": "no_desktop"}
    if completed.returncode == 2:
        return {"ok": False, "cancelled": True}
    if completed.returncode != 0:
        return {"ok": False, "reason": "no_desktop"}
    path = completed.stdout.decode("utf-8", errors="replace").strip()
    if not path:
        return {"ok": False, "cancelled": True}
    return {"ok": True, "path": path}


def grounded_numbers(text: str, metrics: dict, tolerance: float = 0.01) -> list:
    """Return numeric tokens in text not grounded in metrics (empty = grounded)."""
    values = []

    def collect(value):
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (int, float)):
            values.append(float(value))
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(metrics)
    ungrounded = []
    for match in re.finditer(
        r"(?<![\w.])(\d+\.\d+|\d+(?:\.\d+)?%)(?!\w)", text or ""
    ):
        token = match.group(1)
        if token.endswith("%"):
            number = float(token[:-1]) / 100.0
        else:
            number = float(token)
        if not any(abs(number - value) <= tolerance for value in values):
            ungrounded.append(token)
    return ungrounded


def inspect_data_hint(path: Path) -> dict:
    path = Path(path)
    result = {
        "ok": True,
        "path": str(path),
        "rows_guess": 0,
        "lang_hint": "mixed",
        "schema": {
            "has_state": False,
            "has_questions": False,
            "has_gold": False,
            "has_expected": False,
        },
        "format": "unknown",
        "issues": [],
    }
    if not path.is_file():
        result.update(ok=False, error="file not found: %s" % path)
        result["issues"].append("file not found")
        return result
    try:
        with open(path, "rb") as fh:
            raw = fh.read(256 * 1024)
    except OSError as exc:
        result.update(ok=False, error="could not read file: %s" % exc)
        result["issues"].append("read error")
        return result
    text = raw.decode("utf-8", errors="replace")
    if len(raw) >= 256 * 1024:
        result["issues"].append("truncated at 256 KiB")
    lines = [line for line in text.splitlines() if line.strip()]
    lines = lines[:200]
    result["rows_guess"] = len(lines)
    if not lines:
        result["issues"].append("empty file")
        return result
    header = [part.strip().lower() for part in lines[0].split(",")]
    is_csv = "text" in header and "label" in header

    def _collect_strings(value, out):
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                _collect_strings(item, out)
        elif isinstance(value, list):
            for item in value:
                _collect_strings(item, out)

    lang_source = text
    if is_csv:
        lang_source = "\n".join(lines[1:])
    else:
        try:
            strings = []
            for line in lines:
                _collect_strings(json.loads(line), strings)
            if strings:
                lang_source = "\n".join(strings)
        except ValueError:
            pass
    cjk = sum(
        1 for ch in lang_source
        if "\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf"
    )
    latin = sum(1 for ch in lang_source if ("a" <= ch <= "z") or ("A" <= ch <= "Z"))
    letters = cjk + latin
    ratio = cjk / letters if letters else 0.0
    result["lang_hint"] = "zh" if ratio > 0.3 else ("en" if ratio < 0.02 else "mixed")
    if is_csv:
        result["format"] = "csv"
        result["rows_guess"] = max(0, len(lines) - 1)
        return result
    try:
        first = json.loads(lines[0])
    except ValueError:
        result["issues"].append("first row is neither JSON nor text,label CSV")
        return result
    if isinstance(first, dict):
        schema = result["schema"]
        schema["has_state"] = "state" in first
        schema["has_questions"] = "questions" in first
        schema["has_gold"] = "gold" in first
        schema["has_expected"] = "expected" in first
        if schema["has_state"] and schema["has_questions"] and (
            schema["has_gold"] or schema["has_expected"]
        ):
            result["format"] = "laya"
        else:
            result["issues"].append("JSON object does not match a known training schema")
    else:
        result["issues"].append("JSON row is not an object")
    return result


def early_stop_step(state: dict, epoch_mean_loss, patience: int, min_delta: float):
    """Pure early-stop state machine; returns (new_state, triggered, reason)."""
    state = dict(state or {})
    if epoch_mean_loss is None:
        return state, False, ""
    try:
        loss = float(epoch_mean_loss)
    except (TypeError, ValueError):
        return state, False, ""
    best = state.get("best")
    stale = int(state.get("stale") or 0)
    if best is None:
        state.update(best=loss, stale=0)
        return state, False, ""
    improvement = float(best) - loss
    if improvement >= float(min_delta):
        state.update(best=loss, stale=0)
        return state, False, ""
    stale += 1
    state["stale"] = stale
    if stale >= max(1, int(patience)):
        return state, True, (
            "no improvement >= %.6g for %d epoch(s); best=%.6g last=%.6g"
            % (float(min_delta), stale, float(best), loss)
        )
    return state, False, ""


def vram_admission_decision(used_mib, total_mib, need_gib, margin_mib):
    need_mib = int(float(need_gib) * 1024)
    if used_mib + need_mib + margin_mib > total_mib:
        return {
            "ok": False,
            "status": 409,
            "error": (
                "insufficient VRAM: used=%d MiB total=%d MiB need=%d MiB margin=%d MiB"
                % (used_mib, total_mib, need_mib, margin_mib)
            ),
        }
    return {"ok": True, "used": used_mib, "total": total_mib, "need": need_mib}


def resolve_diffsynth_libs(project_root, explicit_libs, python_paths, arch):
    """Resolve python_paths for diffsynth; MOSS always gets pylibs-5 first."""
    root = Path(project_root)
    if arch == "moss_image":
        # MOSS needs transformers>=5; .aitk-pylibs is aitk-only and must not
        # leak into the moss sys.path (it is transformed 4.x and may be broken).
        pylibs5 = (root / "runtime" / "pylibs-5").resolve()
        if not pylibs5.is_dir():
            return [], "moss_needs_transformers_5", "none"
        incoming = list(python_paths or [])
        if explicit_libs and not incoming:
            explicit = Path(explicit_libs).expanduser()
            if str(explicit.resolve()) != str((root / ".aitk-pylibs").resolve()):
                incoming = [str(explicit)]
        paths = [str(pylibs5)]
        for item in incoming:
            candidate = Path(item).expanduser()
            if not candidate.is_absolute():
                candidate = root / candidate
            try:
                resolved = candidate.resolve()
            except OSError:
                resolved = candidate
            if candidate.name.lower() == ".aitk-pylibs":
                continue
            if resolved == pylibs5:
                continue
            if str(resolved) not in paths:
                paths.append(str(resolved))
        return paths, None, "params" if python_paths else ("cli" if explicit_libs else "auto")
    if python_paths:
        return list(python_paths), None, "params"
    # qwen/z use the runtime transformers 4.57.6; do not inject .aitk-pylibs.
    if arch != "moss_image":
        return [], None, "none"
    return [], None, "none"


def preflight_diffsynth_python(python, python_paths, arch):
    """Check target Python + sys.path before spawning DiffSynth; no GPU."""
    paths = [str(item) for item in (python_paths or [])]
    for raw in paths:
        base = Path(raw)
        if not base.is_dir():
            return False, "python_paths is not a directory: %s" % base, {}
        for dist in base.glob("*dist-info"):
            if not (dist / "METADATA").is_file():
                return (
                    False,
                    "python_paths dist-info missing METADATA: %s（先在预检修包，不要起训练）"
                    % dist,
                    {},
                )
    if arch == "moss_image" and not paths:
        return False, "moss 需要 transformers>=5，当前没有可用 python_paths", {}
    code = (
        "import sys, json\n"
        "sys.path[:0] = %s\n"
        "import transformers, huggingface_hub\n"
        "print(json.dumps({'transformers': getattr(transformers, '__version__', None),"
        " 'huggingface_hub': getattr(huggingface_hub, '__version__', None)}))\n"
    ) % json.dumps(paths)
    try:
        completed = subprocess.run(
            [str(python), "-c", code],
            capture_output=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return False, "moss 预检超时（20s）", {}
    except OSError as exc:
        return False, "moss 预检无法启动 python: %s" % exc, {}
    raw = completed.stdout.decode("utf-8", errors="replace").strip().splitlines()
    if completed.returncode != 0 or not raw:
        return (
            False,
            "moss 预检 import 失败（python=%s paths=%s）: %s"
            % (python, paths, _tail(completed.stderr.decode("utf-8", errors="replace"), 400)),
            {},
        )
    try:
        versions = json.loads(raw[-1])
    except ValueError:
        return False, "moss 预检输出无法解析: %s" % raw[-1], {}
    found = versions.get("transformers")
    hub = versions.get("huggingface_hub")
    if hub in (None, "", "None"):
        return False, "moss 预检失败: huggingface_hub found=None（路径 %s）" % ", ".join(paths), versions
    if arch == "moss_image":
        try:
            major = int(str(found).split(".", 1)[0])
        except (TypeError, ValueError):
            major = 0
        if not found or major < 5:
            return (
                False,
                "moss 需要 transformers>=5，当前 %s（路径 %s）"
                % (found, ", ".join(paths)),
                versions,
            )
    return True, None, versions


# ===========================================================================
# [CM-05] UI HOSTING: v3 bootstrap injection + legacy token injection
# 职责: 只在内存注入 token/API/UI/ms-wire，绝不改磁盘 UI 文件。
# 上游: CoreHandler._serve_v3/_serve_legacy; 下游: 静态文件读取。
# ===========================================================================

class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _legacy_impl_system_memory(probe=None):
    """Return RAM fields via GlobalMemoryStatusEx; null fields on any failure."""
    try:
        if probe is not None:
            values = probe()
        elif os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            status = _MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                raise OSError(ctypes.get_last_error(), "GlobalMemoryStatusEx")
            values = (int(status.ullTotalPhys), int(status.ullAvailPhys))
        else:
            raise OSError("not windows")
        total_bytes, avail_bytes = values
        total_mib = int(total_bytes) // (1024 * 1024)
        used_mib = max(0, int(total_bytes) - int(avail_bytes)) // (1024 * 1024)
        pct = round((used_mib / total_mib) * 100.0, 1) if total_mib else None
        if total_mib <= 0 or used_mib < 0 or used_mib > total_mib:
            raise ValueError("invalid memory readings")
        return {
            "ram_used_mib": used_mib,
            "ram_total_mib": total_mib,
            "ram_pct": pct,
        }
    except Exception:
        return {"ram_used_mib": None, "ram_total_mib": None, "ram_pct": None}


def probe_vram(cmd="nvidia-smi") -> dict:
    """Single nvidia-smi parser shared by /api/vram, admission and peak sampling."""
    try:
        completed = subprocess.run(
            [
                cmd,
                "--query-gpu=memory.total,memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        return {"ok": False, "reason": "nvidia-smi not found"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "timeout"}
    except OSError:
        return {"ok": False, "reason": "nvidia-smi not found"}
    try:
        line = completed.stdout.decode("utf-8", errors="replace").splitlines()[0]
        total_s, used_s = [part.strip() for part in line.split(",")[:2]]
        total = int(float(total_s))
        used = int(float(used_s))
    except (IndexError, ValueError):
        return {"ok": False, "reason": "parse failed"}
    return {
        "ok": True,
        "used_mib": used,
        "total_mib": total,
        "pct": round((used / total) * 100.0, 1) if total else 0.0,
        "source": "nvidia-smi",
        "ts": utc_now(),
    }


CAPTION_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
CAPTION_MAX_CAPTION_BYTES = 10 * 1024 * 1024


def normalise_caption_image_exts(value):
    if value is None or value == "":
        return CAPTION_IMAGE_EXTS
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple)):
        parts = [str(part).strip() for part in value if str(part).strip()]
    else:
        raise ValueError("dataset_image_exts must be a comma string or list")
    exts = []
    for part in parts:
        ext = part.lower()
        if not ext.startswith("."):
            ext = "." + ext
        if ext in exts:
            continue
        exts.append(ext)
    return tuple(exts) if exts else CAPTION_IMAGE_EXTS


def normalise_caption_ext(value):
    ext = str(value or ".txt").strip().lower()
    if not ext:
        ext = ".txt"
    if not ext.startswith("."):
        ext = "." + ext
    if "/" in ext or "\\" in ext:
        raise ValueError("caption ext must not contain path separators")
    return ext


def select_stale(procs, current_pid=None, model_markers=None):
    """Pure selector: return only stale llama-server processes we own.

    A process must have image ``llama-server.exe``, a command line carrying
    both ``--mmproj`` and one of our model markers, and must not be a child of
    the current core.  No process is killed here.
    """
    markers = [
        str(item).lower()
        for item in (
            model_markers
            or [
                str(_LLM_MODELS),
                "Qwen3.5-9B",
                "Qwen3.8-27B",
                "llama-cpp",
            ]
        )
        if str(item)
    ]
    selected = []
    for proc in procs or []:
        if not isinstance(proc, dict):
            continue
        name = str(proc.get("Name") or proc.get("name") or "").lower()
        if name != "llama-server.exe":
            continue
        command = str(
            proc.get("CommandLine") or proc.get("command_line") or ""
        ).lower()
        if "--mmproj" not in command:
            continue
        if not any(marker in command for marker in markers):
            continue
        parent = proc.get("ParentProcessId", proc.get("parent_pid"))
        try:
            parent = int(parent) if parent is not None else None
        except (TypeError, ValueError):
            parent = None
        if current_pid is not None and parent == int(current_pid):
            continue
        selected.append(proc)
    return selected


def _caption_processes():
    if os.name != "nt":
        return []
    script = (
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -ieq 'llama-server.exe' } | "
        "Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raw = completed.stdout.decode("utf-8", errors="replace").strip()
        if not raw:
            return []
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else [parsed]
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def sweep_stale_caption_servers(dry_run=True):
    """Sweep historical llama-server processes; dry-run by default."""
    procs = _caption_processes()
    stale = select_stale(procs, current_pid=os.getpid())
    if dry_run or not stale:
        print("sweep dry-run: would kill %d" % len(stale), flush=True)
        return []
    swept = []
    for proc in stale:
        pid = proc.get("ProcessId") or proc.get("pid")
        command = str(proc.get("CommandLine") or "")
        if not pid:
            continue
        kill_tree(int(pid))
        swept.append(int(pid))
        print("swept pid=%s cmd=%s" % (pid, _tail(command, 180)), flush=True)
    return swept


def validate_footprint_params(params: dict) -> dict:
    """Validate per-model memory/size knobs without dropping them."""
    if "max_pixels" in params:
        value = params["max_pixels"]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 4096 <= value <= 4194304
            or value % 256 != 0
        ):
            raise ApiError(
                400,
                "max_pixels must be an integer in [4096, 4194304] and a multiple of 256",
            )
    for key in ("enable_model_cpu_offload", "offload"):
        if key in params:
            value = params[key]
            if not isinstance(value, bool):
                raise ApiError(400, "%s must be a boolean" % key)
    return params


def validate_sample_params(params: dict) -> dict:
    """Validate UI sampling knobs without dropping or rewriting them."""
    if "sample_steps" in params:
        value = params["sample_steps"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ApiError(400, "sample_steps must be >= 1")
    if "sample_guidance" in params:
        value = params["sample_guidance"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ApiError(400, "sample_guidance must be a number >= 0")
    if "sample_seed" in params:
        value = params["sample_seed"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ApiError(400, "sample_seed must be >= 0")
    return params


def resolve_components_from_scan(arch, params, models, models_dir):
    """Fill missing diffsynth component paths from the package models scan."""
    out = dict(params or {})
    auto = {}
    entries = list(models or [])

    def by_name(fragment):
        wanted = str(fragment).lower()
        for item in entries:
            if wanted in str(item.get("name") or "").lower() or wanted in str(
                item.get("id") or ""
            ).lower():
                return item
        return None

    if not str(out.get("model") or "").strip() and arch in (
        "flux2_klein_9b",
        "krea2_raw",
    ):
        for candidate in {
            "flux2_klein_9b": "flux2-klein-9b",
            "krea2_raw": "krea-2-raw",
        }.get(arch, ""):
            item = by_name(candidate)
            if item and item.get("path"):
                out["model"] = item["path"]
                auto["model"] = item["path"]
                break
    if arch == "flux2_klein_9b" and not str(out.get("tokenizer_path") or "").strip():
        model_path = Path(str(out.get("model") or "")).expanduser()
        candidate = model_path / "tokenizer"
        if candidate.is_dir():
            out["tokenizer_path"] = str(candidate.resolve())
            auto["tokenizer_path"] = out["tokenizer_path"]
    if arch == "krea2_raw":
        if not str(out.get("model") or "").strip():
            item = by_name("krea-2-raw")
            if item and item.get("path"):
                p = Path(item["path"])
                out["model"] = str(p / "raw.safetensors") if p.is_dir() else str(p)
                auto["model"] = out["model"]
        if not str(out.get("text_encoder") or "").strip():
            item = by_name("qwen3-vl-4b-instruct")
            if item and item.get("path"):
                out["text_encoder"] = item["path"]
                auto["text_encoder"] = item["path"]
        if not str(out.get("vae") or "").strip():
            candidate = (
                Path(models_dir)
                / "qwen-image"
                / "vae"
                / "diffusion_pytorch_model.safetensors"
            )
            if candidate.is_file():
                out["vae"] = str(candidate.resolve())
                auto["vae"] = out["vae"]
    return out, auto


def caption_backend_status(engine=DEFAULT_CAPTION_ENGINE, overrides=None):
    """Stat-only backend availability; never starts llama-server."""
    overrides = overrides or {}
    engine_path = Path(engine)
    engine_available = engine_path.is_file()
    backends = []
    for backend_id, spec in CAPTION_BACKENDS.items():
        override = overrides.get(backend_id) or {}
        model_path = override.get("model")
        mmproj_path = override.get("mmproj")
        if model_path is None:
            matches = sorted(Path(spec["model_glob"]).parent.glob(Path(spec["model_glob"]).name))
            model_path = str(matches[0]) if matches else str(Path(spec["model_glob"]))
        if mmproj_path is None:
            matches = sorted(Path(spec["mmproj_glob"]).parent.glob(Path(spec["mmproj_glob"]).name))
            mmproj_path = str(matches[0]) if matches else str(Path(spec["mmproj_glob"]))
        model_file = Path(model_path)
        mmproj_file = Path(mmproj_path)
        reasons = []
        if not engine_available:
            reasons.append("engine not found: %s" % engine_path)
        if not model_file.is_file():
            reasons.append("model not found: %s" % model_file)
        if not mmproj_file.is_file():
            reasons.append("mmproj not found: %s" % mmproj_file)
        backends.append(
            {
                "id": backend_id,
                "available": not reasons,
                "model_bytes": model_file.stat().st_size if model_file.is_file() else 0,
                "mmproj_bytes": mmproj_file.stat().st_size if mmproj_file.is_file() else 0,
                "engine": str(engine_path),
                "reason": "; ".join(reasons) if reasons else None,
            }
        )
    return {
        "ok": True,
        "backends": backends,
        "engine_available": engine_available,
        "reason": None if engine_available else "engine not found: %s" % engine_path,
        "engine_flags_ok": None,
        "engine_flags_note": "not probed: stat-only endpoint; llama-server never started",
    }


def inject_ui_token(html: bytes, token: str) -> bytes:
    """Return the UI HTML with a token bootstrap script before </head>.

    The file on disk is never modified; this only builds the HTTP response.
    """
    script = (
        '<script>window.__MS_TOKEN__=%s;window.__MS_API__="/api";</script>'
        % json.dumps(token, ensure_ascii=True)
    ).encode("utf-8")
    match = re.search(br"</head\s*>", html, re.IGNORECASE)
    if match is not None:
        return html[: match.start()] + script + html[match.start() :]
    return script + html


def inject_v3_bootstrap(html: bytes, token: str) -> bytes:
    """Inject the v3 bootstrap + wire script before </body> (memory only)."""
    bootstrap = (
        '<script>window.__MS_TOKEN__=%s;window.__MS_API__="/api";'
        'window.__MS_UI="v3";</script>'
        '<script src="/ms-wire.js"></script>'
        % json.dumps(token, ensure_ascii=True)
    ).encode("utf-8")
    match = re.search(br"</body\s*>", html, re.IGNORECASE)
    if match is not None:
        return html[: match.start()] + bootstrap + html[match.start() :]
    return html + bootstrap


# ===========================================================================
# [CM-06] CHILD RUNNER BUILDERS: Laya / ai-toolkit / LlamaFactory
# 职责: 把 sys.path 注入/依赖顺序/YAML 路径编码成 -c 字符串（或命令前缀）。
# 上游: Scheduler._*_train_command; 下游: 子进程解释器.
# 注意: DiffSynth 没有本地 builder；必须走 adapters.diffsynth_adapter.run().
# ===========================================================================

def laya_runner_code(laya_src) -> str:
    """The -c payload used because embedded Python ignores PYTHONPATH."""
    return (
        "import sys; sys.path.insert(0, %r); "
        "from laya.train_cli import main; sys.exit(main())"
    ) % str(laya_src)


def build_laya_train_command(laya_python, laya_src) -> list:
    """Default Laya child command: [python, -c, sys.path-injecting runner]."""
    return [str(laya_python), "-c", laya_runner_code(laya_src)]


def aitk_runner_code(aitk_libs, aitk_src) -> str:
    """The -c payload used by the ComfyUI-python + .aitk-pylibs setup."""
    return (
        "import sys, runpy\n"
        "sys.path.insert(0, %r)\n"
        "sys.path.insert(0, %r)\n"
        "sys.argv = ['run.py', sys.argv[1]]\n"
        "runpy.run_path(%r, run_name='__main__')\n"
    ) % (
        str(aitk_libs),
        str(aitk_src),
        str(Path(aitk_src) / "run.py"),
    )


def build_aitk_train_command(aitk_python, aitk_libs, aitk_src, config_path=None) -> list:
    """Default ai-toolkit child command.

    The returned prefix is ``[python, "-c", runner]``; the adapter appends the
    YAML path. If ``config_path`` is supplied, the full 4-part command is
    returned (used by tests to assert the exact process shape).
    """
    command = [str(aitk_python), "-c", aitk_runner_code(aitk_libs, aitk_src)]
    if config_path is not None:
        command.append(str(config_path))
    return command


def lf_path_setup_code(lf_libs, lf_src, include_src: bool = True) -> str:
    """sys.path setup for LlamaFactory; <lf-src>\\src must be first.

    ``--lf-src`` is the repository root (the preflight checks
    ``<lf-src>\\src\\llamafactory\\cli.py``), so the package lives under
    ``<lf-src>\\src``, not directly under the repo root.
    """
    lines = [
        "import sys\n",
        "sys.path.insert(0, %r)\n" % str(lf_libs),
        "sys.path.insert(0, %r)\n" % str(lf_src),
    ]
    if include_src:
        lines.append("sys.path.insert(0, %r)\n" % str(Path(lf_src) / "src"))
    return "".join(lines)


def lf_runner_code(lf_libs, lf_src, yaml_path) -> str:
    """The -c payload for kind="lf" (mirrors adapters/lf_adapter.py's runner).

    Path order ends as ``<lf-src>\\src``, repo root, then ``.pylibs-lf``;
    ``src`` must be first so ``from llamafactory.cli import main`` resolves.
    A pre-seeded ``import dill`` is kept: torch imports dill itself (0.4.0 in
    the ComfyUI env) while datasets 4.0 requires dill<0.3.9.
    """
    return (
        lf_path_setup_code(lf_libs, lf_src, include_src=True)
        + "import dill\n"
        "sys.argv = ['llamafactory-cli', 'train', %r]\n"
        "from llamafactory.cli import main\n"
        "sys.exit(main())\n"
    ) % str(yaml_path)


def build_lf_train_command(lf_python, lf_libs, lf_src, yaml_path) -> list:
    """Default kind="lf" child: [python, -c, runner-with-yaml-baked-in]."""
    return [str(lf_python), "-c", lf_runner_code(lf_libs, lf_src, yaml_path)]


def lf_preflight(lf_python, lf_libs, lf_src, adapter_path) -> list:
    """Return the missing pieces of the LlamaFactory environment (empty = ok)."""
    missing = []
    if not Path(lf_python).is_file():
        missing.append("lf-python=%s" % lf_python)
    if not Path(lf_libs).is_dir():
        missing.append("lf-libs=%s" % lf_libs)
    if not (Path(lf_src) / Path(LF_CLI_REL)).is_file():
        missing.append("lf-src=%s" % lf_src)
    if not Path(adapter_path).is_file():
        missing.append("adapter=%s" % adapter_path)
    return missing


# ===========================================================================
# [CM-07] PERSISTENCE / LOCK: GpuLock then JobStore (SQLite WAL)
# 职责: 跨进程 GPU 锁；job 行 CRUD、状态迁移原语、metrics 历史、恢复。
# 上游: Scheduler/CoreApp; 下游: <data_dir>\core.db, jobs\<id>\metrics.jsonl.
# 注意: state 的最终写入在 Scheduler，JobStore 只执行 UPDATE；合法迁移见 MAP §5.
# ===========================================================================

# ---------------------------------------------------------------------------
# GPU lock (real OS file lock, released by the OS if the process dies)
# ---------------------------------------------------------------------------


class _LegacyGpuLock:
    """Exclusive lock on a file. Windows: msvcrt.locking; POSIX: fcntl.flock."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None
        self.owner = None

    def acquire(self, owner: str = "") -> bool:
        if self._fh is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fh = open(self.path, "r+b")
        except FileNotFoundError:
            fh = open(self.path, "w+b")
        try:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"0")
                fh.flush()
            fh.seek(0)
            if _IS_WINDOWS:
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                fh.close()
            except OSError:
                pass
            return False
        self._fh = fh
        self.owner = owner
        status = "pid=%d job=%s acquired_at=%s\n" % (os.getpid(), owner or "-", utc_now())
        try:
            raw = status.encode("utf-8")
            fh.seek(0)
            fh.write(raw)
            fh.flush()
            fh.truncate(len(raw))
        except OSError:
            pass
        return True

    def release(self) -> None:
        fh = self._fh
        if fh is None:
            return
        self._fh = None
        try:
            fh.seek(0)
            if _IS_WINDOWS:
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                fh.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# SQLite job store
# ---------------------------------------------------------------------------


class JobStore:
    """Thread-safe SQLite job table (WAL). One connection, serialized by _lock."""

    COLUMNS = (
        "id",
        "kind",
        "state",
        "pid",
        "log_path",
        "metrics_json",
        "created_at",
        "updated_at",
        "idempotency_key",
        "params_json",
        "cancel_requested",
        "exit_code",
        "error",
    )

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._history_lock = threading.Lock()
        self._history_seq = {}
        self._conn = sqlite3.connect(
            str(self.db_path), timeout=20.0, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.isolation_level = None
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=20000")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    pid INTEGER,
                    log_path TEXT,
                    metrics_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    idempotency_key TEXT,
                    params_json TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    exit_code INTEGER,
                    error TEXT
                )
                """
            )
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency "
                "ON jobs(idempotency_key) WHERE idempotency_key IS NOT NULL"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_state_created ON jobs(state, created_at)"
            )
            self._conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)

    # -- low level ---------------------------------------------------------

    def _row(self, row):
        return dict(row) if row is not None else None

    def journal_mode(self) -> str:
        with self._lock:
            return str(self._conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    # -- job API -----------------------------------------------------------

    def create_or_get(self, kind: str, params: dict, idempotency_key=None):
        params_json = json_text(params or {})
        now = utc_now()
        with self._lock:
            if idempotency_key:
                row = self._conn.execute(
                    "SELECT * FROM jobs WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
                if row is not None:
                    return self._row(row), False
            last_exc = None
            for _ in range(5):
                job_id = new_job_id()
                try:
                    self._conn.execute(
                        "INSERT INTO jobs (id, kind, state, pid, log_path, metrics_json, "
                        "created_at, updated_at, idempotency_key, params_json, "
                        "cancel_requested, exit_code, error) "
                        "VALUES (?, ?, 'queued', NULL, NULL, NULL, ?, ?, ?, ?, 0, NULL, NULL)",
                        (job_id, kind, now, now, idempotency_key, params_json),
                    )
                    row = self._conn.execute(
                        "SELECT * FROM jobs WHERE id=?", (job_id,)
                    ).fetchone()
                    return self._row(row), True
                except sqlite3.IntegrityError as exc:
                    last_exc = exc
                    if idempotency_key:
                        row = self._conn.execute(
                            "SELECT * FROM jobs WHERE idempotency_key=?",
                            (idempotency_key,),
                        ).fetchone()
                        if row is not None:
                            return self._row(row), False
                    continue
            raise last_exc or RuntimeError("could not insert job")

    def get(self, job_id: str):
        if not job_id:
            return None
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return self._row(row)

    def list(self, state=None, limit: int = 100):
        limit = max(1, min(int(limit), 500))
        with self._lock:
            if state:
                rows = self._conn.execute(
                    "SELECT * FROM jobs WHERE state=? ORDER BY rowid DESC LIMIT ?",
                    (state, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM jobs ORDER BY rowid DESC LIMIT ?", (limit,)
                ).fetchall()
            return [self._row(r) for r in rows]

    def _append_metrics_history(self, job_id: str, metrics_json: str) -> None:
        try:
            metrics = json.loads(metrics_json or "null")
        except ValueError:
            return
        if not isinstance(metrics, dict):
            return
        path = self.db_path.parent / "jobs" / job_id / "metrics.jsonl"
        with self._history_lock:
            seq = self._history_seq.get(job_id)
            if seq is None:
                seq = 0
                if path.is_file():
                    try:
                        for line in path.read_text(
                            encoding="utf-8", errors="replace"
                        ).splitlines():
                            try:
                                seq = max(seq, int(json.loads(line).get("seq") or 0))
                            except (ValueError, TypeError):
                                continue
                    except OSError:
                        seq = 0
            seq += 1
            row = {"ts": utc_now(), "seq": seq}
            row.update(metrics)
            row["seq"] = seq
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            except OSError:
                return
            self._history_seq[job_id] = seq

    def update(self, job_id: str, **fields):
        allowed = set(self.COLUMNS) - {"id", "created_at"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError("unknown job columns: %s" % sorted(unknown))
        if "updated_at" not in fields:
            fields["updated_at"] = utc_now()
        if "metrics_json" in fields:
            with self._lock:
                old = self._conn.execute(
                    "SELECT metrics_json FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
            try:
                old_metrics = json.loads(old["metrics_json"] or "{}") if old else {}
                new_metrics = json.loads(fields["metrics_json"] or "{}")
                if (
                    isinstance(old_metrics, dict)
                    and isinstance(new_metrics, dict)
                    and old_metrics.get("heartbeat_at")
                    and not new_metrics.get("heartbeat_at")
                ):
                    new_metrics["heartbeat_at"] = old_metrics["heartbeat_at"]
                    fields["metrics_json"] = json_text(new_metrics)
            except (TypeError, ValueError):
                pass
        sets = ", ".join("%s=?" % key for key in fields)
        values = list(fields.values()) + [job_id]
        with self._lock:
            self._conn.execute("UPDATE jobs SET %s WHERE id=?" % sets, values)
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            result = self._row(row)
        if "metrics_json" in fields and result is not None:
            self._append_metrics_history(job_id, fields["metrics_json"])
        return result

    def patch_metrics(self, job_id: str, patch: dict):
        """Read-modify-write only metrics_json (heartbeat, no schema change)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT metrics_json FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            try:
                metrics = json.loads(row["metrics_json"] or "{}")
            except ValueError:
                metrics = {}
            if not isinstance(metrics, dict):
                metrics = {}
            metrics.update(patch)
            return self.update(job_id, metrics_json=json_text(metrics))

    def claim_next(self):
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM jobs WHERE state='queued' ORDER BY rowid LIMIT 1"
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return None
                now = utc_now()
                self._conn.execute(
                    "UPDATE jobs SET state='starting', updated_at=? WHERE id=?",
                    (now, row["id"]),
                )
                self._conn.execute("COMMIT")
                result = self._row(row)
                result["state"] = "starting"
                result["updated_at"] = now
                return result
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    def request_cancel(self, job_id: str):
        """Returns (row, outcome) with outcome in missing|terminal|cancelled|requested."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                return None, "missing"
            row = self._row(row)
            if row["state"] in TERMINAL_STATES:
                return row, "terminal"
            now = utc_now()
            if row["state"] == "queued":
                self._conn.execute(
                    "UPDATE jobs SET state='cancelled', cancel_requested=1, "
                    "updated_at=?, error=? WHERE id=?",
                    (now, "cancelled before start", job_id),
                )
                return self.get(job_id), "cancelled"
            self._conn.execute(
                "UPDATE jobs SET cancel_requested=1, updated_at=? WHERE id=?",
                (now, job_id),
            )
            return self.get(job_id), "requested"

    def queue_position(self, job_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT state, rowid FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None or row["state"] != "queued":
                return 0
            count = self._conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE state='queued' AND rowid <= ?",
                (row["rowid"],),
            ).fetchone()[0]
            return int(count)

    def counts_by_state(self) -> dict:
        counts = {state: 0 for state in ALL_STATES}
        with self._lock:
            for row in self._conn.execute(
                "SELECT state, COUNT(*) AS n FROM jobs GROUP BY state"
            ).fetchall():
                counts[str(row["state"])] = int(row["n"])
        return counts

    def running_count(self) -> int:
        with self._lock:
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE state='running'"
                ).fetchone()[0]
            )

    def recover_interrupted(self):
        """Mark leftover starting/running rows interrupted after a restart.

        Rows whose worker pid is still alive are also marked interrupted: this
        MVP does not adopt orphan workers (documented limitation).
        """
        details = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE state IN ('starting','running') ORDER BY rowid"
            ).fetchall()
            for row in rows:
                pid = row["pid"]
                alive = is_pid_alive(pid) if pid else False
                if alive:
                    reason = "core restarted; orphan worker pid %s still alive, not adopted" % pid
                else:
                    reason = "core restarted; worker pid not alive"
                self._conn.execute(
                    "UPDATE jobs SET state='interrupted', updated_at=?, error=? WHERE id=?",
                    (utc_now(), reason, row["id"]),
                )
                details.append({"id": row["id"], "pid": pid, "pid_alive": alive})
        return details

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


# ===========================================================================
# [CM-08] FAKE WORKER / PROCESS KILL: selftest-only child + tree termination
# 职责: 无 GPU 的可取消 worker；15 秒软停/硬杀路径的负控载体。
# 上游: Scheduler._run_fake; 下游: <data_dir>\jobs\<id>\metrics.json, logs.
# ===========================================================================

# ---------------------------------------------------------------------------
# fake worker (runs in a child process)
# ---------------------------------------------------------------------------


def _write_metrics_file(path: Path, payload: dict) -> None:
    try:
        atomic_write_text(Path(path), json_text(payload) + "\n")
    except OSError:
        pass


def run_fake_worker(args) -> int:
    log_path = Path(args.log)
    metrics_path = Path(args.metrics)
    stop_path = Path(args.stop_file)
    job_id = args.job_id
    steps = max(1, int(args.steps))
    interval = max(0.01, float(args.interval))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.parent.mkdir(parents=True, exist_ok=True)

    stop_requested = {"value": False}
    ignore_soft_stop = bool(getattr(args, "ignore_soft_stop", False))

    def _on_signal(signum, frame):
        stop_requested["value"] = True

    def _cancel_requested():
        if stop_requested["value"]:
            return True
        if ignore_soft_stop:
            return False
        return stop_path.exists()

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _on_signal)
            except (ValueError, OSError):
                pass

    started = time.monotonic()
    with open(log_path, "a", encoding="utf-8", buffering=1) as log:
        try:
            log.write(
                "[%s] fake worker start job=%s steps=%d interval=%ss pid=%d "
                "ignore_soft_stop=%d\n"
                % (
                    utc_now(),
                    job_id,
                    steps,
                    interval,
                    os.getpid(),
                    1 if ignore_soft_stop else 0,
                )
            )
            _write_metrics_file(
                metrics_path,
                {
                    "job_id": job_id,
                    "step": 0,
                    "total": steps,
                    "percent": 0.0,
                    "loss": None,
                    "state": "starting",
                    "ts": utc_now(),
                },
            )
            completed = 0
            for step in range(1, steps + 1):
                if _cancel_requested():
                    log.write(
                        "[%s] cancel detected at step %d/%d; stopping gracefully\n"
                        % (utc_now(), completed, steps)
                    )
                    _write_metrics_file(
                        metrics_path,
                        {
                            "job_id": job_id,
                            "step": completed,
                            "total": steps,
                            "percent": round(100.0 * completed / steps, 1),
                            "loss": None,
                            "state": "cancelled",
                            "ts": utc_now(),
                        },
                    )
                    return 0
                time.sleep(interval)
                if _cancel_requested():
                    log.write(
                        "[%s] cancel detected at step %d/%d; stopping gracefully\n"
                        % (utc_now(), completed, steps)
                    )
                    _write_metrics_file(
                        metrics_path,
                        {
                            "job_id": job_id,
                            "step": completed,
                            "total": steps,
                            "percent": round(100.0 * completed / steps, 1),
                            "loss": None,
                            "state": "cancelled",
                            "ts": utc_now(),
                        },
                    )
                    return 0
                completed = step
                elapsed = time.monotonic() - started
                loss = round(1.8 / ((step + 1) ** 0.5) + 0.02 * ((step * 37) % 7), 4)
                percent = round(100.0 * step / steps, 1)
                log.write(
                    "[%s] step %d/%d (%.1f%%) loss=%.4f elapsed=%.2fs\n"
                    % (utc_now(), step, steps, percent, loss, elapsed)
                )
                _write_metrics_file(
                    metrics_path,
                    {
                        "job_id": job_id,
                        "step": step,
                        "total": steps,
                        "percent": percent,
                        "loss": loss,
                        "elapsed_s": round(elapsed, 2),
                        "state": "running",
                        "ts": utc_now(),
                    },
                )
            elapsed = time.monotonic() - started
            log.write(
                "[%s] fake worker done job=%s steps=%d elapsed=%.2fs\n"
                % (utc_now(), job_id, steps, elapsed)
            )
            _write_metrics_file(
                metrics_path,
                {
                    "job_id": job_id,
                    "step": steps,
                    "total": steps,
                    "percent": 100.0,
                    "loss": round(1.8 / ((steps + 1) ** 0.5), 4),
                    "elapsed_s": round(elapsed, 2),
                    "state": "succeeded",
                    "ts": utc_now(),
                },
            )
            return 0
        except Exception:
            tb = traceback.format_exc()
            try:
                log.write("[%s] fake worker crashed:\n%s\n" % (utc_now(), tb))
            except Exception:
                pass
            _write_metrics_file(
                metrics_path,
                {
                    "job_id": job_id,
                    "step": completed if "completed" in locals() else 0,
                    "total": steps,
                    "state": "failed",
                    "error": _tail(tb, 300),
                    "ts": utc_now(),
                },
            )
            return 3


def _legacy_impl_kill_process_tree(pid: int) -> None:
    """Hard stop a worker tree. Only ever called with a pid we spawned."""
    if not pid:
        return
    if _IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                creationflags=_CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


# ===========================================================================
# [CM-09] SCHEDULER / JOB EXECUTION (serial; one starting/running at a time)
# 职责: 取队首 -> GPU 锁 -> 子进程/适配器 -> metrics/日志 -> 终态写回.
# 上游: CoreApp.start_scheduler()/run(); 下游: JobStore/GpuLock/adapters.
# 子块: CM-09A fake / CM-09B laya / CM-09C llm / CM-09D aitk /
#       CM-09E diffsynth / CM-09F lf；状态机见 MAP §5，参数落点见 MAP §4.
# ===========================================================================

# ---------------------------------------------------------------------------
# scheduler
# ---------------------------------------------------------------------------


class Scheduler(threading.Thread):
    """Serial FIFO scheduler: at most one job in starting/running at a time."""

    def __init__(self, app: "CoreApp"):
        super().__init__(name="modelsmith-scheduler", daemon=True)
        self.app = app
        self.stop_event = app.stop_event
        self._early_stop_states = {}

    # -- public ------------------------------------------------------------

    def run(self) -> None:
        while not self.stop_event.is_set():
            row = self.app.store.claim_next()
            if row is None:
                self.stop_event.wait(0.1)
                continue
            try:
                self._execute(row)
            except Exception:
                tb = traceback.format_exc()
                try:
                    self._append_log(
                        Path(row.get("log_path") or self.app.log_path_for(row["id"])),
                        "scheduler exception:\n" + tb,
                    )
                except Exception:
                    pass
                current = self.app.store.get(row["id"])
                if current and current["state"] not in TERMINAL_STATES:
                    self.app.store.update(
                        row["id"],
                        state="failed",
                        error="scheduler exception: " + _tail(tb, 300),
                    )
            self._record_peak_vram(row["id"])

    def _record_peak_vram(self, job_id: str) -> None:
        row = self.app.store.get(job_id)
        if row is None:
            return
        try:
            metrics = json.loads(row.get("metrics_json") or "{}")
        except ValueError:
            metrics = {}
        if not isinstance(metrics, dict):
            metrics = {}
        if metrics.get("peak_vram") or metrics.get("peak_vram_mib"):
            return
        probe = probe_vram(self.app.vram_cmd)
        if not probe.get("ok"):
            return
        used = int(probe["used_mib"])
        metrics["peak_vram_mib"] = used
        latest = self.app.store.get(job_id)
        if latest is not None:
            try:
                latest_metrics = json.loads(latest.get("metrics_json") or "{}")
            except ValueError:
                latest_metrics = {}
            if isinstance(latest_metrics, dict):
                latest_metrics["peak_vram_mib"] = used
                metrics = latest_metrics
        self.app.store.update(job_id, metrics_json=json_text(metrics))

    # -- internals ---------------------------------------------------------

    def _maybe_early_stop(self, job_id: str, metrics: dict, stop_file: Path) -> None:
        row = self.app.store.get(job_id)
        if row is None:
            return
        try:
            params = json.loads(row.get("params_json") or "{}")
        except ValueError:
            params = {}
        config = params.get("early_stop") if isinstance(params, dict) else None
        if not isinstance(config, dict) or not config.get("enabled"):
            return
        state = self._early_stop_states.setdefault(job_id, {"best": None, "stale": 0})
        new_state, triggered, reason = early_stop_step(
            state,
            metrics.get("epoch_mean_loss"),
            int(config.get("patience") or 2),
            float(config.get("min_delta") or 0.01),
        )
        self._early_stop_states[job_id] = new_state
        if not triggered:
            return
        try:
            stop_file.write_text("early_stop\n", encoding="utf-8")
        except OSError:
            pass
        metrics["early_stop"] = {
            "triggered": True,
            "reason": reason,
            "best_epoch_mean_loss": new_state.get("best"),
            "stale_epochs": new_state.get("stale"),
        }
        self.app.store.update(
            job_id,
            cancel_requested=1,
            metrics_json=json_text(metrics),
        )

    def _heartbeat_loop(self, job_id: str, stop_event: threading.Event) -> None:
        while not stop_event.wait(2.0):
            row = self.app.store.get(job_id)
            if row is None or row.get("state") != "running":
                continue
            try:
                metrics = json.loads(row.get("metrics_json") or "{}")
            except ValueError:
                metrics = {}
            server_pid = metrics.get("server_pid") if isinstance(metrics, dict) else None
            if isinstance(server_pid, int) and server_pid > 0 and not is_pid_alive(server_pid):
                metrics["state"] = "failed"
                metrics["error"] = "container_gone: server pid %d is not alive" % server_pid
                self.app.store.update(
                    job_id,
                    state="failed",
                    error=metrics["error"],
                    metrics_json=json_text(metrics),
                )
                return
            self.app.store.patch_metrics(job_id, {"heartbeat_at": utc_now()})

    def _execute(self, job: dict) -> None:
        job_id = job["id"]
        log_path = Path(job.get("log_path") or self.app.log_path_for(job_id))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.app.store.update(job_id, log_path=str(log_path))

        current = self.app.store.get(job_id)
        if self.stop_event.is_set() or (current and current["cancel_requested"]):
            self.app.store.update(
                job_id,
                state="cancelled",
                error="cancelled before start",
            )
            return

        lock = GpuLock(self.app.lock_path)
        acquired = False
        for _ in range(120):
            current = self.app.store.get(job_id)
            if self.stop_event.is_set():
                self.app.store.update(
                    job_id, state="interrupted", error="core stopping before start"
                )
                return
            if current and current["cancel_requested"]:
                self.app.store.update(
                    job_id, state="cancelled", error="cancelled before start"
                )
                return
            if lock.acquire(owner=job_id):
                acquired = True
                break
            time.sleep(0.1)
        if not acquired:
            self.app.store.update(
                job_id, state="queued", error="gpu lock busy; requeued"
            )
            self.stop_event.wait(0.5)
            return

        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(job_id, heartbeat_stop),
            name="job-heartbeat-%s" % job_id,
            daemon=True,
        )
        heartbeat.start()
        try:
            if job["kind"] == "fake":
                self._run_fake(job, log_path)
            elif job["kind"] == "laya":
                self._run_laya(job, log_path)
            elif job["kind"] == "llm":
                self._run_llm(job, log_path)
            elif job["kind"] == "aitk":
                self._run_aitk(job, log_path)
            elif job["kind"] == "diffsynth":
                self._run_diffsynth(job, log_path)
            elif job["kind"] == "lf":
                self._run_lf(job, log_path)
            elif job["kind"] == "caption":
                self._run_caption(job, log_path)
            else:
                self._append_log(
                    log_path,
                    "job kind=%r has no adapter in this build "
                    "(fake, laya, llm, aitk, diffsynth, lf, caption); failing"
                    % job["kind"],
                )
                self.app.store.update(
                    job_id,
                    state="failed",
                    error="no adapter for kind %r in this build "
                    "(fake, laya, llm, aitk, diffsynth, lf, caption)" % job["kind"],
                )
                return
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=2)
            lock.release()

    # [CM-09A] fake route: selftest-only worker; no GPU, no adapter.
    def _run_fake(self, job: dict, log_path: Path) -> None:
        job_id = job["id"]
        try:
            params = json.loads(job.get("params_json") or "{}")
        except ValueError:
            params = {}
        if not isinstance(params, dict):
            params = {}
        try:
            steps = int(params.get("steps", 20))
        except (TypeError, ValueError):
            steps = 20
        try:
            interval = float(params.get("interval_s", 1.0))
        except (TypeError, ValueError):
            interval = 1.0
        steps = max(1, min(steps, 10000))
        interval = max(0.01, min(interval, 60.0))
        ignore_soft_stop = bool(params.get("ignore_soft_stop", False))

        job_dir = self.app.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        stop_file = job_dir / "stop"
        metrics_file = job_dir / "metrics.json"
        try:
            stop_file.unlink()
        except FileNotFoundError:
            pass

        argv = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--fake-worker",
            "--job-id",
            job_id,
            "--log",
            str(log_path),
            "--metrics",
            str(metrics_file),
            "--stop-file",
            str(stop_file),
            "--steps",
            str(steps),
            "--interval",
            str(interval),
        ]
        if ignore_soft_stop:
            argv.append("--ignore-soft-stop")
        creationflags = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(self.app.project_root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except OSError as exc:
            self._append_log(log_path, "could not spawn fake worker: %s" % exc)
            self.app.store.update(
                job_id, state="failed", error="spawn failed: %s" % exc
            )
            return

        self.app.store.update(job_id, state="running", pid=proc.pid, error=None)
        soft_sent = False
        soft_deadline = 0.0
        hard_sent = False
        hard_deadline = 0.0
        last_metrics = None

        try:
            while True:
                returncode = proc.poll()
                if returncode is not None:
                    break
                current = self.app.store.get(job_id)
                cancel_wanted = bool(current and current["cancel_requested"]) or (
                    self.stop_event.is_set()
                )
                now = time.monotonic()
                if cancel_wanted:
                    if not soft_sent:
                        try:
                            stop_file.write_text("cancel\n", encoding="utf-8")
                        except OSError:
                            pass
                        soft_sent = True
                        soft_deadline = now + SOFT_STOP_GRACE_S
                    elif not hard_sent and now >= soft_deadline:
                        self._append_log(
                            log_path,
                            "soft stop did not take effect within %gs; taskkill /T /F pid=%d"
                            % (SOFT_STOP_GRACE_S, proc.pid),
                        )
                        kill_process_tree(proc.pid)
                        hard_sent = True
                        hard_deadline = now + 3.0
                    elif hard_sent and now >= hard_deadline and proc.poll() is None:
                        self._append_log(
                            log_path,
                            "taskkill did not reap pid=%d; falling back to proc.kill()"
                            % proc.pid,
                        )
                        try:
                            proc.kill()
                        except OSError:
                            pass
                        hard_deadline = now + 60.0
                metrics = self._read_metrics_quiet(metrics_file)
                if metrics is not None and metrics != last_metrics:
                    last_metrics = metrics
                    self.app.store.update(
                        job_id, metrics_json=json_text(metrics)
                    )
                time.sleep(0.1)
            returncode = proc.poll()
        finally:
            if proc.poll() is None:
                kill_process_tree(proc.pid)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
            try:
                stop_file.unlink()
            except FileNotFoundError:
                pass

        final_metrics = self._read_metrics_quiet(metrics_file)
        if final_metrics is not None:
            last_metrics = final_metrics
        current = self.app.store.get(job_id)
        cancel_requested = bool(current and current["cancel_requested"])
        if self.stop_event.is_set() and not cancel_requested:
            state = "interrupted"
            error = "core shutting down while job was running"
        elif cancel_requested:
            state = "cancelled"
            error = "cancelled by request (soft stop / taskkill)"
        elif returncode == 0:
            state = "succeeded"
            error = None
        else:
            state = "failed"
            error = "worker exited with code %s" % returncode
        self.app.store.update(
            job_id,
            state=state,
            exit_code=returncode,
            error=error,
            metrics_json=json_text(last_metrics) if last_metrics is not None else None,
        )

    @staticmethod
    def _read_metrics(path: Path):
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            message = "metrics file is not valid UTF-8: %s (%s)" % (path, exc)
            print("[modelsmith] " + message, file=sys.stderr, flush=True)
            raise ValueError(message) from exc
        if "\ufffd" in text:
            message = "metrics file contains U+FFFD replacement chars: %s" % path
            print("[modelsmith] " + message, file=sys.stderr, flush=True)
            raise ValueError(message)
        try:
            payload = json.loads(text)
        except ValueError as exc:
            message = "metrics file is not valid JSON: %s (%s)" % (path, exc)
            print("[modelsmith] " + message, file=sys.stderr, flush=True)
            raise ValueError(message) from exc
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _read_metrics_quiet(path: Path):
        try:
            return Scheduler._read_metrics(path)
        except (OSError, ValueError):
            return None

    # -- Laya route --------------------------------------------------------

    @staticmethod
    def _job_params(job: dict) -> dict:
        try:
            params = json.loads(job.get("params_json") or "{}")
        except ValueError:
            return {}
        return params if isinstance(params, dict) else {}

    @staticmethod
    def _present(value) -> bool:
        return value is not None and bool(str(value).strip())

    # [CM-09B] laya route: adapter build/run, metrics file, stop file + signal.
    def _load_laya_adapter(self):
        if not self.app.laya_adapter_path.is_file():
            raise RuntimeError(
                "laya adapter not found: %s" % self.app.laya_adapter_path
            )
        root = str(self.app.project_root)
        if root not in sys.path:
            sys.path.insert(0, root)
        return importlib.import_module("adapters.laya_adapter")

    def _laya_train_command(self, params: dict):
        override = params.get("train_command")
        if override is not None:
            if (
                not isinstance(override, list)
                or not override
                or not all(isinstance(part, str) and part for part in override)
            ):
                raise ValueError(
                    "params.train_command must be a non-empty list of strings"
                )
            return list(override)
        if not self.app.laya_python.is_file():
            raise RuntimeError("laya python not found: %s" % self.app.laya_python)
        if not self.app.laya_src.is_dir():
            raise RuntimeError("laya src not found: %s" % self.app.laya_src)
        # ComfyUI embedded Python is isolated (python312._pth) and ignores
        # PYTHONPATH; inject the source path inside the child instead.
        return build_laya_train_command(self.app.laya_python, self.app.laya_src)

    def _phase_from_metrics(self, metrics: dict) -> str:
        if metrics.get("evaluation_seen"):
            return "evaluation"
        if metrics.get("checkpoint_latest"):
            return "checkpoint"
        if metrics.get("loss") is not None or metrics.get("updates_total") is not None:
            return "training"
        return "starting"

    def _fail_laya(self, job_id: str, log_path: Path, error: str) -> None:
        self._append_log(log_path, "laya job failed: " + error)
        self.app.store.update(
            job_id,
            state="failed",
            error=error,
            metrics_json=json_text(
                {
                    "job_id": job_id,
                    "state": "failed",
                    "phase": "finished",
                    "error": error,
                }
            ),
        )

    def _run_laya(self, job: dict, log_path: Path) -> None:
        job_id = job["id"]
        params = self._job_params(job)
        missing = [
            key for key in ("data", "out") if not self._present(params.get(key))
        ]
        if missing:
            self._fail_laya(
                job_id,
                log_path,
                "missing required param(s): %s" % ", ".join(missing),
            )
            return
        if self._present(params.get("base")):
            params = dict(params)
            params["base"] = self.app.resolve_model_ref(params["base"])

        try:
            adapter = self._load_laya_adapter()
            train_command = self._laya_train_command(params)
        except Exception as exc:
            self._fail_laya(
                job_id,
                log_path,
                "laya preflight failed: %s" % _tail(str(exc), 300),
            )
            return

        job_dir = self.app.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        stop_file = job_dir / "stop"
        metrics_file = job_dir / "metrics.json"
        try:
            stop_file.unlink()
        except FileNotFoundError:
            pass

        monitor_stop = threading.Event()
        cancel_event = threading.Event()
        monitor_state = {"stop_written": False}

        def _monitor():
            last_text = None
            while not monitor_stop.wait(0.1):
                current = self.app.store.get(job_id)
                if current is None:
                    break
                if (
                    current["cancel_requested"] or self.stop_event.is_set()
                ) and not monitor_state["stop_written"]:
                    try:
                        stop_file.write_text("cancel\n", encoding="utf-8")
                    except OSError:
                        pass
                    cancel_event.set()
                    monitor_state["stop_written"] = True
                metrics = self._read_metrics_quiet(metrics_file)
                if metrics is None:
                    continue
                metrics["phase"] = self._phase_from_metrics(metrics)
                text = json_text(metrics)
                if text == last_text:
                    continue
                fields = {"metrics_json": text}
                pid = metrics.get("pid")
                if pid and not current.get("pid"):
                    try:
                        fields["pid"] = int(pid)
                    except (TypeError, ValueError):
                        pass
                self.app.store.update(job_id, **fields)
                self._maybe_early_stop(job_id, metrics, stop_file)
                last_text = text

        initial_metrics = {
            "job_id": job_id,
            "state": "starting",
            "phase": "starting",
            "epoch": None,
            "updates_total": None,
            "micro_step": 0,
            "loss": None,
        }
        self.app.store.update(
            job_id, state="running", error=None, metrics_json=json_text(initial_metrics)
        )
        monitor = threading.Thread(
            target=_monitor, name="laya-metrics-%s" % job_id, daemon=True
        )
        monitor.start()
        try:
            result = adapter.run(
                job,
                log_path,
                train_command=train_command,
                stop_file=stop_file,
                metrics_path=metrics_file,
                cancel_event=cancel_event,
                cwd=self.app.project_root,
            )
        except Exception as exc:
            error = "laya adapter error: %s" % _tail(str(exc), 300)
            self._append_log(log_path, error)
            result = {
                "state": "failed",
                "cancelled": False,
                "returncode": None,
                "metrics": {"state": "failed", "error": error},
                "events": [],
            }
        finally:
            monitor_stop.set()
            monitor.join(timeout=2)
            try:
                stop_file.unlink()
            except FileNotFoundError:
                pass

        final_metrics = result.get("metrics") if isinstance(result, dict) else None
        if not isinstance(final_metrics, dict):
            final_metrics = self._read_metrics_quiet(metrics_file) or {}
        final_metrics = dict(final_metrics)
        final_metrics.setdefault("job_id", job_id)
        final_metrics["phase"] = "finished"
        params_out = params.get("out") if isinstance(params, dict) else None
        metric_out = final_metrics.get("output_dir")
        if params_out and (not metric_out or "\ufffd" in str(metric_out)):
            final_metrics["output_dir"] = str(params_out)
            final_metrics.setdefault("warnings", []).append(
                "output_dir repaired from params.out (adapter stdout was not valid UTF-8)"
            )

        current = self.app.store.get(job_id)
        cancel_requested = bool(current and current["cancel_requested"])
        result_state = result.get("state") if isinstance(result, dict) else None
        if self.stop_event.is_set() and not cancel_requested:
            state = "interrupted"
            error = "core shutting down while laya job was running"
        elif result_state == "cancelled" or cancel_requested:
            state = "cancelled"
            error = "cancelled by request"
        elif result_state == "succeeded":
            state = "succeeded"
            error = None
        else:
            state = "failed"
            error = final_metrics.get("error") or "laya adapter failed"
        final_metrics["state"] = state
        if error:
            final_metrics["error"] = error
        self.app.store.update(
            job_id,
            state=state,
            error=error,
            exit_code=result.get("returncode") if isinstance(result, dict) else None,
            metrics_json=json_text(final_metrics),
            pid=final_metrics.get("pid") or (current or {}).get("pid"),
        )
        self._append_log(
            log_path,
            "core: laya final state=%s returncode=%s"
            % (
                state,
                result.get("returncode") if isinstance(result, dict) else None,
            ),
        )

    # -- LLM route ---------------------------------------------------------

    # [CM-09C] llm route: self-built LoRA runner; log step/loss/lr parser.
    def _llm_fail(self, job_id: str, log_path: Path, error: str) -> None:
        self._append_log(log_path, "llm job failed: " + error)
        self.app.store.update(
            job_id,
            state="failed",
            error=error,
            metrics_json=json_text(
                {
                    "job_id": job_id,
                    "state": "failed",
                    "phase": "finished",
                    "error": error,
                    "report_loaded": False,
                }
            ),
        )

    @staticmethod
    def _llm_int(value, name: str, minimum: int = 1) -> int:
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

    @staticmethod
    def _llm_float(value, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("%s must be a number" % name)
        return float(value)

    def _llm_train_command(self, params: dict):
        missing = [
            key for key in ("model", "data", "out") if not self._present(params.get(key))
        ]
        if missing:
            raise ValueError("missing required param(s): %s" % ", ".join(missing))

        override = params.get("train_command")
        if override is not None:
            if (
                not isinstance(override, list)
                or not override
                or not all(isinstance(part, str) and part for part in override)
            ):
                raise ValueError(
                    "params.train_command must be a non-empty list of strings"
                )
            command = list(override)
        else:
            if not self.app.llm_python.is_file():
                raise RuntimeError("llm python not found: %s" % self.app.llm_python)
            if not self.app.llm_script_path.is_file():
                raise RuntimeError("llm script not found: %s" % self.app.llm_script_path)
            command = [str(self.app.llm_python), str(self.app.llm_script_path)]

        command += [
            "--mode",
            "train",
            "--model",
            str(params["model"]),
            "--data",
            str(params["data"]),
            "--out",
            str(params["out"]),
        ]

        int_options = (
            ("max_steps", "--max-steps", 0),
            ("epochs", "--epochs", 1),
            ("rank", "--rank", 1),
            ("alpha", "--alpha", 1),
            ("max_len", "--max-len", 8),
            ("batch", "--batch", 1),
        )
        for key, flag, minimum in int_options:
            if params.get(key) is not None:
                command += [flag, str(self._llm_int(params[key], key, minimum))]
        if params.get("lr") is not None:
            lr = self._llm_float(params["lr"], "lr")
            if lr <= 0:
                raise ValueError("lr must be > 0")
            command += ["--lr", str(lr)]
        if params.get("device") is not None:
            device = str(params["device"]).strip()
            if not device:
                raise ValueError("device must be a non-empty string")
            command += ["--device", device]
        if params.get("template") is not None:
            template = str(params["template"])
            if template not in ("qwen", "plain"):
                raise ValueError("template must be 'qwen' or 'plain'")
            command += ["--template", template]
        if params.get("assert_updated") is not None:
            if not isinstance(params["assert_updated"], bool):
                raise ValueError("assert_updated must be a boolean")
            if params["assert_updated"]:
                command.append("--assert-updated")
        return command

    @staticmethod
    def _soft_stop_process(proc) -> str:
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

    @staticmethod
    def _apply_llm_report(metrics: dict, report: dict, report_path: Path) -> None:
        metrics["report_loaded"] = True
        metrics["report_path"] = str(report_path)
        metrics["report_status"] = report.get("status")
        loss = report.get("loss") if isinstance(report.get("loss"), dict) else {}
        series = loss.get("series") if isinstance(loss, dict) else None
        if isinstance(series, list):
            metrics["loss_series"] = series
            if series:
                last = series[-1] if isinstance(series[-1], dict) else {}
                if last.get("step") is not None:
                    metrics["step"] = last.get("step")
                if last.get("loss") is not None:
                    metrics["loss"] = last.get("loss")
                if last.get("lr") is not None:
                    metrics["lr"] = last.get("lr")
        for key in ("first", "last", "mean", "all_finite"):
            if key in loss:
                metrics["loss_" + key] = loss[key]
        run = report.get("run") if isinstance(report.get("run"), dict) else {}
        for key in (
            "steps_done",
            "planned_steps",
            "samples_used",
            "train_elapsed_s",
            "total_elapsed_s",
            "steps_per_s",
        ):
            if key in run:
                metrics[key] = run[key]
        memory = report.get("memory") if isinstance(report.get("memory"), dict) else {}
        if "peak_vram_alloc_mib" in memory:
            metrics["peak_vram_alloc_mib"] = memory["peak_vram_alloc_mib"]
        if "peak_vram_reserved_mib" in memory:
            metrics["peak_vram_reserved_mib"] = memory["peak_vram_reserved_mib"]
        update = report.get("update") if isinstance(report.get("update"), dict) else {}
        if update:
            metrics["update"] = {
                key: update.get(key)
                for key in ("changed_tensors", "trainable_tensors", "delta_l2_total")
                if key in update
            }
        metrics["report_summary"] = {
            "status": report.get("status"),
            "run": run,
            "loss": {
                key: loss.get(key)
                for key in ("first", "last", "mean", "all_finite")
                if key in loss
            },
            "memory": memory,
        }

    def _run_llm(self, job: dict, log_path: Path) -> None:
        job_id = job["id"]
        params = self._job_params(job)
        missing = [
            key for key in ("model", "data", "out") if not self._present(params.get(key))
        ]
        if missing:
            self._llm_fail(
                job_id,
                log_path,
                "missing required param(s): %s" % ", ".join(missing),
            )
            return
        try:
            command = self._llm_train_command(params)
        except Exception as exc:
            self._llm_fail(
                job_id,
                log_path,
                "llm preflight failed: %s" % _tail(str(exc), 300),
            )
            return

        job_dir = self.app.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        stop_file = job_dir / "stop"
        metrics_file = job_dir / "metrics.json"
        try:
            stop_file.unlink()
        except FileNotFoundError:
            pass

        metrics = {
            "job_id": job_id,
            "state": "starting",
            "phase": "starting",
            "step": 0,
            "loss": None,
            "lr": None,
            "report_loaded": False,
            "events": 0,
            "started_at": utc_now(),
            "elapsed_s": 0.0,
            "returncode": None,
        }
        self.app.store.update(
            job_id,
            state="running",
            error=None,
            metrics_json=json_text(metrics),
        )
        self._append_log(log_path, "llm adapter argv=" + json.dumps(command, ensure_ascii=False))

        child_env = os.environ.copy()
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        child_env["PYTHONIOENCODING"] = "utf-8"
        popen_kwargs = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        else:
            popen_kwargs["start_new_session"] = True

        proc = None
        reader = None
        line_queue: queue.Queue = queue.Queue()
        stop_sent = False
        stop_signal = None
        stop_deadline = 0.0
        hard_sent = False
        error_message = None
        report_path = None
        returncode = None
        started = time.monotonic()

        try:
            proc = subprocess.Popen(
                command,
                cwd=str(self.app.project_root),
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
                target=_reader, name="llm-adapter-reader", daemon=True
            )
            reader.start()
            metrics["pid"] = proc.pid
            metrics["state"] = "running"
            self.app.store.update(job_id, pid=proc.pid, state="running")
            _write_metrics = lambda: self.app.store.update(
                job_id, metrics_json=json_text(metrics)
            )
            _write_metrics()

            while True:
                now = time.monotonic()
                current = self.app.store.get(job_id)
                cancel_wanted = bool(
                    (current and current["cancel_requested"])
                    or self.stop_event.is_set()
                )
                if cancel_wanted and not stop_sent:
                    try:
                        stop_file.write_text("cancel\n", encoding="utf-8")
                    except OSError:
                        pass
                    stop_signal = self._soft_stop_process(proc)
                    stop_sent = True
                    stop_deadline = now + LLM_STOP_GRACE_S
                    self._append_log(
                        log_path, "llm cancel requested; soft stop sent (%s)" % stop_signal
                    )
                if (
                    stop_sent
                    and not hard_sent
                    and proc.poll() is None
                    and now >= stop_deadline
                ):
                    self._append_log(
                        log_path, "llm soft stop grace expired; taskkill /T /F"
                    )
                    kill_process_tree(proc.pid)
                    hard_sent = True

                try:
                    line = line_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    break
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(line if line.endswith("\n") else line + "\n")
                step_match = LLM_STEP_RE.search(line)
                if step_match:
                    metrics["step"] = int(step_match.group("step"))
                    metrics["loss"] = float(step_match.group("loss"))
                    metrics["lr"] = float(step_match.group("lr"))
                    metrics["phase"] = "training"
                    metrics["events"] = int(metrics.get("events", 0)) + 1
                    metrics["elapsed_s"] = round(time.monotonic() - started, 3)
                    _write_metrics()
                    continue
                error_match = LLM_ERROR_RE.match(line)
                if error_match:
                    error_message = error_match.group("message").strip() or line.strip()
                    metrics["error"] = error_message
                    _write_metrics()
                    continue
                report_match = LLM_REPORT_RE.match(line)
                if report_match:
                    report_path = Path(report_match.group("path").strip())
                    continue
                if "TRAIN_OK" in line:
                    metrics["phase"] = "done"

            if proc.poll() is None:
                kill_process_tree(proc.pid)
            returncode = proc.wait(timeout=10)
        except Exception:
            error_message = _tail(traceback.format_exc(), 800)
            self._append_log(log_path, "llm adapter exception:\n" + error_message)
            if proc is not None and proc.poll() is None:
                kill_process_tree(proc.pid)
            returncode = None
        finally:
            if proc is not None and proc.poll() is None:
                kill_process_tree(proc.pid)
            if proc is not None:
                try:
                    returncode = proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    kill_process_tree(proc.pid)
                    try:
                        returncode = proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        returncode = None
            if reader is not None:
                reader.join(timeout=2)
            try:
                stop_file.unlink()
            except FileNotFoundError:
                pass

        default_report = Path(str(params["out"])) / "train_report.json"
        if default_report.is_file():
            report_path = default_report
        elif report_path is None:
            report_path = default_report
        elif not report_path.is_absolute():
            report_path = Path(str(params["out"])) / report_path
        if report_path.is_file():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                metrics["report_error"] = "could not read train_report.json: %s" % exc
            else:
                if isinstance(report, dict):
                    self._apply_llm_report(metrics, report, report_path)
        else:
            metrics["report_error"] = "train_report.json not found: %s" % report_path

        current = self.app.store.get(job_id)
        cancel_requested = bool(current and current["cancel_requested"])
        if self.stop_event.is_set() and not cancel_requested:
            state = "interrupted"
            error = "core shutting down while llm job was running"
        elif stop_sent or cancel_requested:
            state = "cancelled"
            error = "cancelled by request"
        elif error_message:
            state = "failed"
            error = error_message
        elif returncode != 0:
            state = "failed"
            error = "llm worker exited with code %s" % returncode
        elif metrics.get("report_status") not in (None, "ok"):
            state = "failed"
            error = "train_report status=%r" % metrics.get("report_status")
        else:
            state = "succeeded"
            error = None

        metrics["state"] = state
        metrics["phase"] = "finished"
        metrics["elapsed_s"] = round(time.monotonic() - started, 3)
        metrics["returncode"] = returncode
        metrics["cancelled"] = bool(stop_sent)
        if stop_signal:
            metrics["soft_stop_signal"] = stop_signal
        if error:
            metrics["error"] = error
        metrics["finished_at"] = utc_now()
        self.app.store.update(
            job_id,
            state=state,
            error=error,
            exit_code=returncode,
            metrics_json=json_text(metrics),
            pid=metrics.get("pid") or (current or {}).get("pid"),
        )
        self._append_log(
            log_path,
            "core: llm final state=%s returncode=%s step=%s report=%s"
            % (state, returncode, metrics.get("step"), report_path),
        )

    # -- ai-toolkit route --------------------------------------------------

    AITK_PARAM_MAP = {
        "steps": "steps",
        "batch": "batch_size",
        "lr": "lr",
        "rank": "linear",
        "alpha": "linear_alpha",
        "resolution": "resolution",
        "grad_accum": "gradient_accumulation",
        "save_every": "save_every",
        "keep": "max_step_saves_to_keep",
        "sample_every": "sample_every",
        "sample_start": "sample_start_step",
        "sample_steps": "sample_steps",
        "guidance": "guidance_scale",
        "sample_width": "width",
        "sample_height": "height",
        "num_frames": "num_frames",
        "trigger_word": "trigger_word",
        "caption_ext": "caption_ext",
        "sample_prompts": "prompts",
        "sampler": "sampler",
        "noise_scheduler": "noise_scheduler",
        "save_format": "save_format",
    }

    # [CM-09D] aitk route: ComfyUI python + .aitk-pylibs runner; YAML + sample events.
    def _load_aitk_adapter(self):
        if not self.app.aitk_adapter_path.is_file():
            raise RuntimeError(
                "aitk adapter not found: %s" % self.app.aitk_adapter_path
            )
        root = str(self.app.project_root)
        if root not in sys.path:
            sys.path.insert(0, root)
        return importlib.import_module("adapters.aitk_adapter")

    def _map_aitk_params(self, job_id: str, params: dict) -> dict:
        mapped = {
            "name": job_id,
            "model_name_or_path": str(params["model"]),
            "folder_path": str(params["dataset"]),
            "training_folder": str(params["out"]),
            "arch": str(params["arch"]),
        }
        for core_key, adapter_key in self.AITK_PARAM_MAP.items():
            if core_key in params and params[core_key] is not None:
                mapped[adapter_key] = params[core_key]
        return mapped

    def _aitk_fail(self, job_id: str, log_path: Path, error: str) -> None:
        self._append_log(log_path, "aitk job failed: " + error)
        self.app.store.update(
            job_id,
            state="failed",
            error=error,
            metrics_json=json_text(
                {
                    "job_id": job_id,
                    "state": "failed",
                    "phase": "finished",
                    "error": error,
                    "warnings": [],
                }
            ),
        )

    def _aitk_train_command(self, params: dict):
        override = params.get("train_command")
        if override is not None:
            if (
                not isinstance(override, list)
                or not override
                or not all(isinstance(part, str) and part for part in override)
            ):
                raise ValueError(
                    "params.train_command must be a non-empty list of strings"
                )
            return list(override)
        if not self.app.aitk_python.is_file():
            raise RuntimeError(
                "%s: aitk-python=%s" % (AITK_ENV_MISSING, self.app.aitk_python)
            )
        if not self.app.aitk_libs.is_dir():
            raise RuntimeError(
                "%s: aitk-libs=%s" % (AITK_ENV_MISSING, self.app.aitk_libs)
            )
        if not (self.app.aitk_src / "run.py").is_file():
            raise RuntimeError(
                "%s: run.py=%s"
                % (AITK_ENV_MISSING, self.app.aitk_src / "run.py")
            )
        return build_aitk_train_command(
            self.app.aitk_python, self.app.aitk_libs, self.app.aitk_src
        )

    def _run_aitk(self, job: dict, log_path: Path) -> None:
        job_id = job["id"]
        params = self._job_params(job)
        missing = [
            key
            for key in ("model", "dataset", "out", "arch")
            if not self._present(params.get(key))
        ]
        if missing:
            self._aitk_fail(
                job_id,
                log_path,
                "missing required param(s): %s" % ", ".join(missing),
            )
            return

        try:
            adapter = self._load_aitk_adapter()
            mapped = self._map_aitk_params(job_id, params)
            train_command = self._aitk_train_command(params)
        except Exception as exc:
            self._aitk_fail(
                job_id,
                log_path,
                "aitk preflight failed: %s" % _tail(str(exc), 400),
            )
            return

        job_dir = self.app.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        stop_file = job_dir / "stop"
        metrics_file = job_dir / "metrics.json"
        config_path = job_dir / "aitk.yaml"
        try:
            stop_file.unlink()
        except FileNotFoundError:
            pass

        initial_metrics = {
            "job_id": job_id,
            "state": "starting",
            "phase": "starting",
            "step": 0,
            "total": None,
            "sample_groups": 0,
            "last_sample_step": None,
            "warnings": [],
        }
        self.app.store.update(
            job_id,
            state="running",
            error=None,
            metrics_json=json_text(initial_metrics),
        )

        monitor_stop = threading.Event()
        cancel_event = threading.Event()
        monitor_state = {"stop_written": False, "last": None}

        def _monitor():
            while not monitor_stop.wait(0.1):
                current = self.app.store.get(job_id)
                if current is None:
                    break
                if (
                    current["cancel_requested"] or self.stop_event.is_set()
                ) and not monitor_state["stop_written"]:
                    try:
                        stop_file.write_text("cancel\n", encoding="utf-8")
                    except OSError:
                        pass
                    cancel_event.set()
                    monitor_state["stop_written"] = True
                metrics = self._read_metrics_quiet(metrics_file)
                if metrics is None:
                    continue
                text = json_text(metrics)
                if text == monitor_state["last"]:
                    continue
                fields = {"metrics_json": text}
                pid = metrics.get("pid")
                if pid and not current.get("pid"):
                    try:
                        fields["pid"] = int(pid)
                    except (TypeError, ValueError):
                        pass
                self.app.store.update(job_id, **fields)
                monitor_state["last"] = text

        monitor = threading.Thread(
            target=_monitor, name="aitk-metrics-%s" % job_id, daemon=True
        )
        monitor.start()
        result = None
        try:
            result = adapter.run(
                {"id": job_id, "params": mapped},
                log_path,
                train_command=train_command,
                config_path=config_path,
                stop_file=stop_file,
                metrics_path=metrics_file,
                cancel_event=cancel_event,
                cwd=self.app.aitk_src if self.app.aitk_src.is_dir() else self.app.project_root,
            )
        except Exception as exc:
            error = "aitk adapter error: %s" % _tail(str(exc), 500)
            self._append_log(log_path, error)
            result = {
                "state": "failed",
                "cancelled": False,
                "returncode": None,
                "metrics": {"state": "failed", "error": error, "warnings": []},
                "events": [],
            }
        finally:
            monitor_stop.set()
            monitor.join(timeout=2)
            try:
                stop_file.unlink()
            except FileNotFoundError:
                pass

        final_metrics = result.get("metrics") if isinstance(result, dict) else None
        if not isinstance(final_metrics, dict):
            final_metrics = self._read_metrics_quiet(metrics_file) or {}
        final_metrics = dict(final_metrics)
        final_metrics.setdefault("job_id", job_id)
        final_metrics["phase"] = "finished"
        if "warnings" not in final_metrics:
            final_metrics["warnings"] = []

        current = self.app.store.get(job_id)
        cancel_requested = bool(current and current["cancel_requested"])
        result_state = result.get("state") if isinstance(result, dict) else None
        if self.stop_event.is_set() and not cancel_requested:
            state = "interrupted"
            error = "core shutting down while aitk job was running"
        elif result_state == "cancelled" or cancel_requested:
            state = "cancelled"
            error = "cancelled by request"
        elif result_state == "succeeded":
            state = "succeeded"
            error = None
        else:
            state = "failed"
            error = final_metrics.get("error") or "aitk adapter failed"
        final_metrics["state"] = state
        if error:
            final_metrics["error"] = error
        self.app.store.update(
            job_id,
            state=state,
            error=error,
            exit_code=result.get("returncode") if isinstance(result, dict) else None,
            metrics_json=json_text(final_metrics),
            pid=final_metrics.get("pid") or (current or {}).get("pid"),
        )
        self._append_log(
            log_path,
            "core: aitk final state=%s returncode=%s samples=%s/%s"
            % (
                state,
                result.get("returncode") if isinstance(result, dict) else None,
                final_metrics.get("sample_groups"),
                final_metrics.get("expected_sample_groups"),
            ),
        )

    # -- DiffSynth route ---------------------------------------------------

    # [CM-09E] diffsynth route: MUST call adapters.diffsynth_adapter.run();
    # no local launcher (sample hook/cfg_path/device snapshot live in adapter).
    def _load_diffsynth_adapter(self):
        if not self.app.diffsynth_adapter_path.is_file():
            raise RuntimeError(
                "diffsynth adapter not found: %s"
                % self.app.diffsynth_adapter_path
            )
        root = str(self.app.project_root)
        if root not in sys.path:
            sys.path.insert(0, root)
        return importlib.import_module("adapters.diffsynth_adapter")

    def _diffsynth_fail(self, job_id: str, log_path: Path, error: str) -> None:
        self._append_log(log_path, "diffsynth job failed: " + error)
        self.app.store.update(
            job_id,
            state="failed",
            error=error,
            metrics_json=json_text(
                {
                    "job_id": job_id,
                    "state": "failed",
                    "phase": "finished",
                    "error": error,
                }
            ),
        )

    def _diffsynth_train_command(self, params: dict):
        override = params.get("train_command")
        if override is not None:
            if (
                not isinstance(override, list)
                or not override
                or not all(isinstance(part, str) and part for part in override)
            ):
                raise ValueError(
                    "params.train_command must be a non-empty list of strings"
                )
            return list(override)
        missing = []
        if not self.app.diffsynth_python.is_file():
            missing.append("diffsynth-python=%s" % self.app.diffsynth_python)
        if not self.app.diffsynth_src.is_dir():
            missing.append("diffsynth-src=%s" % self.app.diffsynth_src)
        if not self.app.diffsynth_train_py.is_file():
            missing.append("train.py=%s" % self.app.diffsynth_train_py)
        if not self.app.diffsynth_adapter_path.is_file():
            missing.append("adapter=%s" % self.app.diffsynth_adapter_path)
        if missing:
            raise RuntimeError(
                "%s: %s" % (DIFFSYNTH_ENV_MISSING, ", ".join(missing))
            )
        # Let the adapter build its full RUNNER (sample hook + device snapshot).
        # Returning None means "use adapter default"; there is no second launcher.
        return None

    def _run_diffsynth(self, job: dict, log_path: Path) -> None:
        job_id = job["id"]
        params = self._job_params(job)
        missing = [
            key for key in ("model", "dataset", "out") if not self._present(params.get(key))
        ]
        if missing:
            self._diffsynth_fail(
                job_id,
                log_path,
                "missing required param(s): %s" % ", ".join(missing),
            )
            return
        params = dict(params)
        params["model"] = self.app.resolve_model_ref(params["model"])
        try:
            validate_sample_params(params)
            validate_footprint_params(params)
        except ApiError as exc:
            self._diffsynth_fail(job_id, log_path, "diffsynth param error: %s" % exc.message)
            return
        arch = str(params.get("arch") or "qwen_image_21")
        python_paths = params.get("python_paths") or []
        if arch == "moss_image" or python_paths:
            ok, preflight_error, _versions = preflight_diffsynth_python(
                self.app.diffsynth_python, python_paths, arch
            )
            if not ok:
                self._diffsynth_fail(
                    job_id,
                    log_path,
                    "diffsynth preflight failed: %s" % _tail(str(preflight_error), 500),
                )
                return
        try:
            adapter = self._load_diffsynth_adapter()
            train_command = self._diffsynth_train_command(params)
        except Exception as exc:
            self._diffsynth_fail(
                job_id,
                log_path,
                "diffsynth preflight failed: %s" % _tail(str(exc), 400),
            )
            return

        job_dir = self.app.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        if params.get("krea2_auto_derived"):
            self._append_log(
                log_path,
                "[ms-krea2] auto text_encoder=%s vae=%s"
                % (params.get("text_encoder"), params.get("vae")),
            )
        if params.get("components_auto"):
            self._append_log(
                log_path,
                "[ms-components] auto arch=%s %s"
                % (
                    params.get("arch"),
                    " ".join(
                        "%s=%s" % (key, value)
                        for key, value in sorted(params["components_auto"].items())
                    ),
                ),
            )
        stop_file = job_dir / "stop"
        metrics_file = job_dir / "metrics.json"
        try:
            stop_file.unlink()
        except FileNotFoundError:
            pass

        initial_metrics = {
            "job_id": job_id,
            "state": "starting",
            "phase": "starting",
            "step": 0,
            "total": None,
            "sec_per_it": None,
            "artifact": None,
        }
        prior_row = self.app.store.get(job_id)
        try:
            prior_metrics = json.loads(prior_row.get("metrics_json") or "{}") if prior_row else {}
        except ValueError:
            prior_metrics = {}
        if isinstance(prior_metrics, dict) and prior_metrics.get("warnings"):
            initial_metrics["warnings"] = list(prior_metrics["warnings"])
        self.app.store.update(
            job_id,
            state="running",
            error=None,
            metrics_json=json_text(initial_metrics),
        )

        monitor_stop = threading.Event()
        cancel_event = threading.Event()
        monitor_state = {"stop_written": False, "last": None}

        def _monitor():
            while not monitor_stop.wait(0.1):
                current = self.app.store.get(job_id)
                if current is None:
                    break
                if (
                    current["cancel_requested"] or self.stop_event.is_set()
                ) and not monitor_state["stop_written"]:
                    try:
                        stop_file.write_text("cancel\n", encoding="utf-8")
                    except OSError:
                        pass
                    cancel_event.set()
                    monitor_state["stop_written"] = True
                metrics = self._read_metrics_quiet(metrics_file)
                if metrics is None:
                    continue
                text = json_text(metrics)
                if text == monitor_state["last"]:
                    continue
                fields = {"metrics_json": text}
                pid = metrics.get("pid")
                if pid and not current.get("pid"):
                    try:
                        fields["pid"] = int(pid)
                    except (TypeError, ValueError):
                        pass
                self.app.store.update(job_id, **fields)
                monitor_state["last"] = text

        monitor = threading.Thread(
            target=_monitor, name="diffsynth-metrics-%s" % job_id, daemon=True
        )
        monitor.start()
        result = None
        try:
            result = adapter.run(
                {"id": job_id, "params": params},
                log_path,
                train_command=train_command,
                stop_file=stop_file,
                metrics_path=metrics_file,
                cancel_event=cancel_event,
                python=str(self.app.diffsynth_python),
                repo=str(self.app.diffsynth_src),
                train_py=str(self.app.diffsynth_train_py),
            )
        except Exception as exc:
            error = "diffsynth adapter error: %s" % _tail(str(exc), 500)
            self._append_log(log_path, error)
            result = {
                "state": "failed",
                "cancelled": False,
                "returncode": None,
                "metrics": {"state": "failed", "error": error},
                "events": [],
            }
        finally:
            monitor_stop.set()
            monitor.join(timeout=2)
            try:
                stop_file.unlink()
            except FileNotFoundError:
                pass

        if params.get("components_auto"):
            self._append_log(
                log_path,
                "[ms-components] auto arch=%s %s"
                % (
                    params.get("arch"),
                    " ".join(
                        "%s=%s" % (key, value)
                        for key, value in sorted(params["components_auto"].items())
                    ),
                ),
            )
        final_metrics = result.get("metrics") if isinstance(result, dict) else None
        if not isinstance(final_metrics, dict):
            final_metrics = self._read_metrics_quiet(metrics_file) or {}
        final_metrics = dict(final_metrics)
        final_metrics.setdefault("job_id", job_id)
        final_metrics["phase"] = "finished"
        prior_row = self.app.store.get(job_id)
        try:
            prior_metrics = json.loads(prior_row.get("metrics_json") or "{}") if prior_row else {}
        except ValueError:
            prior_metrics = {}
        if isinstance(prior_metrics, dict) and prior_metrics.get("warnings"):
            merged_warnings = list(final_metrics.get("warnings") or [])
            for warning in prior_metrics["warnings"]:
                if warning not in merged_warnings:
                    merged_warnings.append(warning)
            final_metrics["warnings"] = merged_warnings
        dataset_meta = final_metrics.get("dataset_metadata")
        if isinstance(dataset_meta, dict):
            try:
                n_images = int(dataset_meta.get("n") or 0)
                from_txt = int(dataset_meta.get("from_txt") or 0)
            except (TypeError, ValueError):
                n_images = 0
                from_txt = 0
            if n_images > 0:
                warnings = list(final_metrics.get("warnings") or [])
                if from_txt == 0:
                    text = (
                        "no_captions: 全部图片没有同名 .txt，已用默认模板，"
                        "建议补 caption 或指定触发词"
                    )
                    if text not in warnings:
                        warnings.append(text)
                    self._append_log(log_path, "# WARNING: no captions; using default caption template")
                elif from_txt < n_images:
                    text = "partial_captions: %d/%d 张缺 caption" % (
                        n_images - from_txt,
                        n_images,
                    )
                    if text not in warnings:
                        warnings.append(text)
                    self._append_log(log_path, "# WARNING: partial captions: " + text)
                final_metrics["warnings"] = warnings
        artifacts = final_metrics.get("artifacts") or []
        if not final_metrics.get("artifact") and artifacts:
            final_metrics["artifact"] = artifacts[-1]
        if not final_metrics.get("artifact"):
            files = final_metrics.get("artifact_files") or []
            if files:
                final_metrics["artifact"] = files[-1]

        current = self.app.store.get(job_id)
        cancel_requested = bool(current and current["cancel_requested"])
        result_state = result.get("state") if isinstance(result, dict) else None
        if self.stop_event.is_set() and not cancel_requested:
            state = "interrupted"
            error = "core shutting down while diffsynth job was running"
        elif result_state == "cancelled" or cancel_requested:
            state = "cancelled"
            error = "cancelled by request"
        elif result_state == "succeeded":
            state = "succeeded"
            error = None
        else:
            state = "failed"
            error = (
                final_metrics.get("error")
                or final_metrics.get("last_error")
                or "diffsynth adapter failed"
            )
        final_metrics["state"] = state
        if error:
            final_metrics["error"] = error
        self.app.store.update(
            job_id,
            state=state,
            error=error,
            exit_code=result.get("returncode") if isinstance(result, dict) else None,
            metrics_json=json_text(final_metrics),
            pid=final_metrics.get("pid") or (current or {}).get("pid"),
        )
        self._append_log(
            log_path,
            "core: diffsynth final state=%s returncode=%s step=%s artifact=%s"
            % (
                state,
                result.get("returncode") if isinstance(result, dict) else None,
                final_metrics.get("step"),
                final_metrics.get("artifact"),
            ),
        )

    # -- LlamaFactory (kind="lf") route ------------------------------------

    # [CM-09F] lf route: LlamaFactory adapter; bar classification + loss count;
    # caption/data warnings; stop file + Ctrl-Break.
    def _load_lf_adapter(self):
        if not self.app.lf_adapter_path.is_file():
            raise RuntimeError(
                "lf adapter not found: %s" % self.app.lf_adapter_path
            )
        root = str(self.app.project_root)
        if root not in sys.path:
            sys.path.insert(0, root)
        return importlib.import_module("adapters.lf_adapter")

    def _lf_fail(self, job_id: str, log_path: Path, error: str) -> None:
        self._append_log(log_path, "lf job failed: " + error)
        self.app.store.update(
            job_id,
            state="failed",
            error=error,
            metrics_json=json_text(
                {
                    "job_id": job_id,
                    "state": "failed",
                    "phase": "finished",
                    "error": error,
                }
            ),
        )

    @staticmethod
    def _lf_int(params: dict, key: str, default: int, minimum: int = 0) -> int:
        value = params.get(key)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("%s must be an integer" % key)
        if value < minimum:
            raise ValueError("%s must be >= %d" % (key, minimum))
        return value

    @staticmethod
    def _lf_float(params: dict, key: str, default: float, minimum: float = 0.0) -> float:
        value = params.get(key)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("%s must be a number" % key)
        value = float(value)
        if value < minimum:
            raise ValueError("%s must be >= %s" % (key, minimum))
        return value

    def _lf_numbers(self, params: dict) -> dict:
        """Validate the contract's numeric knobs (no adapter import needed)."""
        template = str(params.get("template") or "").strip()
        if not template:
            raise ValueError("template must be a non-empty string")
        rank = self._lf_int(params, "rank", 8, minimum=1)
        numbers = {
            "template": template,
            "rank": rank,
            "alpha": self._lf_int(params, "alpha", 16, minimum=1),
            "lr": self._lf_float(params, "lr", 2e-4, minimum=1e-12),
            "epochs": self._lf_float(params, "epochs", 3.0, minimum=1e-9),
            "max_steps": self._lf_int(params, "max_steps", 0, minimum=0),
            "batch": self._lf_int(params, "batch", 1, minimum=1),
            "grad_accum": self._lf_int(params, "grad_accum", 1, minimum=1),
            "cutoff_len": self._lf_int(params, "cutoff_len", 512, minimum=16),
            "logging_steps": self._lf_int(params, "logging_steps", 1, minimum=1),
            "save_steps": self._lf_int(params, "save_steps", 0, minimum=0),
        }
        return numbers

    def _lf_adapter_params(
        self, params: dict, job_id: str, job_dir: Path, adapter
    ) -> dict:
        """Translate the core contract into adapters/lf_adapter.py params.

        ``data`` is a JSONL path in the contract while LlamaFactory needs a
        registered dataset name + dataset_dir, so the job-local ``lf_data``
        directory gets an alpaca conversion + ``dataset_info.json``.
        """
        numbers = self._lf_numbers(params)
        mapped = {
            "model": str(params["model"]),
            "data": str(params["data"]),
            "out": str(params["out"]),
            "template": numbers["template"],
            "lora_rank": numbers["rank"],
            "lora_alpha": numbers["alpha"],
            "learning_rate": numbers["lr"],
            "num_train_epochs": numbers["epochs"],
            "max_steps": numbers["max_steps"],
            "per_device_train_batch_size": numbers["batch"],
            "gradient_accumulation_steps": numbers["grad_accum"],
            "cutoff_len": numbers["cutoff_len"],
            "logging_steps": numbers["logging_steps"],
            "plot_loss": False,
            "report_to": "none",
            "bf16": True,
        }
        if numbers["save_steps"] > 0:
            mapped["save_steps"] = numbers["save_steps"]
        if mapped["max_steps"] <= 0:
            mapped["max_steps"] = -1  # LlamaFactory: -1 => use num_train_epochs
        prep = adapter.prepare_alpaca_data(
            mapped["data"],
            str(job_dir / "lf_data"),
            limit=None,
            dataset_name="ms_%s" % job_id.replace("-", "_"),
        )
        mapped["dataset"] = prep["dataset_name"]
        mapped["dataset_dir"] = str(job_dir / "lf_data")
        return mapped

    def _lf_train_command(self, params: dict, job_id: str, job_dir: Path):
        """Return ``(argv, meta)``; honours the ``params.train_command`` hook."""
        override = params.get("train_command")
        if override is not None:
            if (
                not isinstance(override, list)
                or not override
                or not all(isinstance(part, str) and part for part in override)
            ):
                raise ValueError(
                    "params.train_command must be a non-empty list of strings"
                )
            self._lf_numbers(params)  # validate the contract even for the hook
            return list(override), {"train_command_override": True}
        missing = lf_preflight(
            self.app.lf_python,
            self.app.lf_libs,
            self.app.lf_src,
            self.app.lf_adapter_path,
        )
        if missing:
            raise RuntimeError("%s: %s" % (LF_ENV_MISSING, ", ".join(missing)))
        adapter = self._load_lf_adapter()
        mapped = self._lf_adapter_params(params, job_id, job_dir, adapter)
        cfg = adapter.build_yaml(mapped)
        yaml_path = adapter.write_yaml(cfg, str(job_dir / "lf.yaml"))
        command = build_lf_train_command(
            self.app.lf_python, self.app.lf_libs, self.app.lf_src, yaml_path
        )
        return command, {
            "yaml_path": yaml_path,
            "dataset": mapped["dataset"],
            "dataset_dir": mapped["dataset_dir"],
        }

    @staticmethod
    def _lf_bar_kind(line: str) -> str:
        lower = (line or "").lower()
        if "it/s" in lower or "s/it" in lower:
            return "training"
        if "ex/s" in lower or "examples/s" in lower or "samples/s" in lower:
            return "preprocess"
        return "unknown"

    @staticmethod
    def _lf_expected_steps(metrics: dict):
        try:
            max_steps = int(metrics.get("max_steps") or 0)
        except (TypeError, ValueError):
            max_steps = 0
        if max_steps > 0:
            return max_steps
        total = metrics.get("total_steps")
        if total:
            try:
                return int(total)
            except (TypeError, ValueError):
                pass
        num_examples = metrics.get("num_examples")
        batch = metrics.get("batch")
        epochs = metrics.get("epochs")
        try:
            if num_examples and batch:
                dataset_steps = (int(num_examples) + int(batch) - 1) // int(batch)
                return int(round(float(epochs or 1.0) * dataset_steps))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        return None

    @staticmethod
    def _lf_apply_event(event: dict, metrics: dict) -> None:
        """Mirror of adapters/lf_adapter.py's event -> metrics mapping."""
        kind = event.get("type")
        if kind == "loss":
            if event.get("loss") is not None:
                metrics["loss"] = event["loss"]
                series = metrics.setdefault("loss_series", [])
                # LlamaFactory's loss dict has no step. Count appearances so
                # loss_series steps are 1..N, never a tqdm total.
                loss_step = len(series) + 1
                series.append(
                    {
                        "step": loss_step,
                        "loss": event["loss"],
                        "lr": event.get("learning_rate"),
                    }
                )
                del series[:-100]
            if event.get("learning_rate") is not None:
                metrics["lr"] = event["learning_rate"]
            if event.get("epoch") is not None:
                metrics["epoch"] = event["epoch"]
            metrics["phase"] = "training"
        elif kind == "progress":
            total = event.get("total") or 0
            step = event.get("step") or 0
            bars = metrics.setdefault("bars", {})
            bars[str(total)] = max(bars.get(str(total), 0), step)
            if event.get("bar_kind") != "training":
                return
            if total:
                metrics["step"] = step
                metrics["total"] = total
                metrics["total_steps"] = total
        elif kind == "info":
            if event.get("key") == "total_steps" and event.get("value"):
                metrics["total"] = event["value"]
                metrics["total_steps"] = event["value"]
            elif event.get("key") == "num_examples":
                metrics["num_examples"] = event["value"]
        elif kind == "checkpoint":
            metrics.setdefault("checkpoints", []).append(event.get("path"))
            metrics["phase"] = "checkpoint"
        elif kind == "summary":
            values = event.get("values") or {}
            for key in (
                "train_runtime",
                "train_loss",
                "train_samples_per_second",
                "train_steps_per_second",
                "epoch",
            ):
                if key in values:
                    metrics[key] = values[key]
            metrics["phase"] = "done"

    @staticmethod
    def _lf_collect_trainer_log(out_dir: Path, metrics: dict) -> None:
        """Peak VRAM from LlamaFactory's trainer_log.jsonl (RECORD_VRAM=1)."""
        path = Path(out_dir) / "trainer_log.jsonl"
        if not path.is_file():
            return
        peak_alloc = 0.0
        peak_res = 0.0
        lines = 0
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except ValueError:
                        continue
                    lines += 1
                    for key in ("vram_allocated", "vram_reserved"):
                        value = payload.get(key)
                        if isinstance(value, (int, float)) and not isinstance(
                            value, bool
                        ):
                            if key == "vram_allocated":
                                peak_alloc = max(peak_alloc, float(value))
                            else:
                                peak_res = max(peak_res, float(value))
        except OSError:
            return
        metrics["trainer_log_lines"] = lines
        metrics["jsonl_path"] = str(path)
        metrics["peak_vram"] = {
            "allocated_gib": peak_alloc or None,
            "reserved_gib": peak_res or None,
        }

    def _run_lf(self, job: dict, log_path: Path) -> None:
        job_id = job["id"]
        params = self._job_params(job)
        missing = [
            key
            for key in ("model", "data", "out", "template")
            if not self._present(params.get(key))
        ]
        if missing:
            self._lf_fail(
                job_id,
                log_path,
                "missing required param(s): %s" % ", ".join(missing),
            )
            return
        params = dict(params)
        params["model"] = self.app.resolve_model_ref(params["model"])

        job_dir = self.app.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        try:
            command, meta = self._lf_train_command(params, job_id, job_dir)
            adapter = self._load_lf_adapter()
        except Exception as exc:
            self._lf_fail(
                job_id,
                log_path,
                "lf preflight failed: %s" % _tail(str(exc), 400),
            )
            return

        out_dir = Path(str(params["out"]))
        if not out_dir.is_absolute():
            out_dir = self.app.project_root / out_dir
        stop_file = job_dir / "stop"
        try:
            stop_file.unlink()
        except FileNotFoundError:
            pass

        try:
            max_steps_hint = int(params.get("max_steps") or 0)
        except (TypeError, ValueError):
            max_steps_hint = 0
        try:
            epochs_hint = float(params.get("epochs") or 3.0)
        except (TypeError, ValueError):
            epochs_hint = 3.0
        try:
            batch_hint = int(params.get("batch") or 1)
        except (TypeError, ValueError):
            batch_hint = 1
        metrics = {
            "job_id": job_id,
            "state": "starting",
            "phase": "starting",
            "step": 0,
            "total": None,
            "max_steps": max_steps_hint,
            "epochs": epochs_hint,
            "batch": batch_hint,
            "loss": None,
            "lr": None,
            "train_runtime": None,
            "peak_vram": None,
            "jsonl_path": None,
            "events": 0,
            "started_at": utc_now(),
            "elapsed_s": 0.0,
            "returncode": None,
        }
        for key in ("yaml_path", "dataset", "dataset_dir"):
            if meta.get(key):
                metrics[key] = meta[key]
        self.app.store.update(
            job_id,
            state="running",
            error=None,
            metrics_json=json_text(metrics),
        )
        self._append_log(
            log_path, "lf child argv=" + json.dumps(command, ensure_ascii=False)
        )

        child_env = os.environ.copy()
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        child_env["PYTHONUTF8"] = "1"
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["RECORD_VRAM"] = "1"
        child_env["MODELSMITH_LF_OUT"] = str(out_dir)
        child_env["MODELSMITH_LF_JOB_DIR"] = str(job_dir)
        child_env["MODELSMITH_LF_STOP_FILE"] = str(stop_file)
        popen_kwargs = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        else:
            popen_kwargs["start_new_session"] = True

        proc = None
        reader = None
        line_queue: queue.Queue = queue.Queue()
        stop_sent = False
        stop_signal = None
        stop_deadline = 0.0
        hard_sent = False
        last_error = None
        returncode = None
        started = time.monotonic()

        try:
            proc = subprocess.Popen(
                command,
                cwd=str(self.app.project_root),
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
                target=_reader, name="lf-reader-%s" % job_id, daemon=True
            )
            reader.start()
            metrics["pid"] = proc.pid
            metrics["state"] = "running"
            self.app.store.update(job_id, pid=proc.pid, state="running")

            def _write_metrics():
                self.app.store.update(job_id, metrics_json=json_text(metrics))

            _write_metrics()

            while True:
                now = time.monotonic()
                current = self.app.store.get(job_id)
                cancel_wanted = bool(
                    (current and current["cancel_requested"])
                    or self.stop_event.is_set()
                )
                if cancel_wanted and not stop_sent:
                    try:
                        stop_file.write_text("cancel\n", encoding="utf-8")
                    except OSError:
                        pass
                    stop_signal = self._soft_stop_process(proc)
                    stop_sent = True
                    stop_deadline = now + LF_STOP_GRACE_S
                    self._append_log(
                        log_path,
                        "lf cancel requested; soft stop sent (%s)" % stop_signal,
                    )
                if (
                    stop_sent
                    and not hard_sent
                    and proc.poll() is None
                    and now >= stop_deadline
                ):
                    self._append_log(
                        log_path, "lf soft stop grace expired; taskkill /T /F"
                    )
                    kill_process_tree(proc.pid)
                    hard_sent = True

                try:
                    line = line_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    break
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(line if line.endswith("\n") else line + "\n")
                stripped = line.strip()
                if stripped:
                    if (
                        "Error" in stripped
                        or "error" in stripped
                        or stripped.startswith("Traceback")
                    ):
                        last_error = stripped[:400]
                    event = adapter.parse_line(line)
                    if event.get("type") == "progress":
                        event["bar_kind"] = self._lf_bar_kind(line)
                    before = (
                        metrics.get("step"),
                        metrics.get("loss"),
                        metrics.get("lr"),
                        metrics.get("train_runtime"),
                    )
                    self._lf_apply_event(event, metrics)
                    metrics["events"] = int(metrics.get("events", 0)) + 1
                    metrics["elapsed_s"] = round(time.monotonic() - started, 3)
                    after = (
                        metrics.get("step"),
                        metrics.get("loss"),
                        metrics.get("lr"),
                        metrics.get("train_runtime"),
                    )
                    if after != before or event.get("type") in ("checkpoint", "summary"):
                        _write_metrics()

            if proc.poll() is None:
                kill_process_tree(proc.pid)
            returncode = proc.wait(timeout=10)
        except Exception:
            last_error = _tail(traceback.format_exc(), 800)
            self._append_log(log_path, "lf child exception:\n" + last_error)
            if proc is not None and proc.poll() is None:
                kill_process_tree(proc.pid)
            returncode = None
        finally:
            if proc is not None and proc.poll() is None:
                kill_process_tree(proc.pid)
            if proc is not None:
                try:
                    returncode = proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    kill_process_tree(proc.pid)
                    try:
                        returncode = proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        returncode = None
            if reader is not None:
                reader.join(timeout=2)
            try:
                stop_file.unlink()
            except FileNotFoundError:
                pass

        self._lf_collect_trainer_log(out_dir, metrics)
        artifact_files = []
        for name in ("adapter_model.safetensors", "adapter_config.json"):
            candidate = out_dir / name
            if candidate.is_file():
                artifact_files.append(str(candidate))
        if artifact_files:
            metrics["artifacts"] = artifact_files
            metrics["artifact"] = artifact_files[0]
        if not metrics.get("jsonl_path"):
            trainer_log = out_dir / "trainer_log.jsonl"
            if trainer_log.is_file():
                metrics["jsonl_path"] = str(trainer_log)
        if metrics.get("total") is None:
            for fallback_key in (
                "total_steps",
                "steps_plan",
                "steps_estimate",
                "max_steps",
            ):
                fallback = metrics.get(fallback_key)
                try:
                    if fallback is not None and int(fallback) > 0:
                        metrics["total"] = int(fallback)
                        break
                except (TypeError, ValueError):
                    continue

        current = self.app.store.get(job_id)
        cancel_requested = bool(current and current["cancel_requested"])
        if self.stop_event.is_set() and not cancel_requested:
            state = "interrupted"
            error = "core shutting down while lf job was running"
        elif stop_sent or cancel_requested:
            state = "cancelled"
            error = "cancelled by request"
        elif last_error and (returncode != 0 or not metrics.get("artifact")):
            # Some libraries log a caught UnicodeDecodeError while still
            # finishing with rc=0 and a valid adapter; only fail on an error
            # line when the worker also failed or produced no artifact.
            state = "failed"
            error = last_error
        elif returncode != 0:
            state = "failed"
            error = "lf worker exited with code %s" % returncode
        else:
            state = "succeeded"
            error = None

        metrics["state"] = state
        metrics["phase"] = "finished"
        metrics["elapsed_s"] = round(time.monotonic() - started, 3)
        metrics["returncode"] = returncode
        metrics["cancelled"] = bool(stop_sent)
        if stop_signal:
            metrics["soft_stop_signal"] = stop_signal
        if error:
            metrics["error"] = error
        metrics["finished_at"] = utc_now()
        self.app.store.update(
            job_id,
            state=state,
            error=error,
            exit_code=returncode,
            metrics_json=json_text(metrics),
            pid=metrics.get("pid") or (current or {}).get("pid"),
        )
        self._append_log(
            log_path,
            "core: lf final state=%s returncode=%s step=%s loss=%s artifact=%s"
            % (
                state,
                returncode,
                metrics.get("step"),
                metrics.get("loss"),
                metrics.get("artifact"),
            ),
        )

    def _load_caption_adapter(self):
        if not self.app.caption_adapter_path.is_file():
            raise RuntimeError("caption adapter not found: %s" % self.app.caption_adapter_path)
        root = str(self.app.project_root)
        if root not in sys.path:
            sys.path.insert(0, root)
        return importlib.import_module("adapters.caption_adapter")

    def _caption_fail(self, job_id: str, log_path: Path, error: str) -> None:
        self._append_log(log_path, "caption job failed: " + error)
        self.app.store.update(
            job_id,
            state="failed",
            error=error,
            metrics_json=json_text(
                {
                    "job_id": job_id,
                    "state": "failed",
                    "phase": "finished",
                    "done": 0,
                    "total": 0,
                    "error": error,
                }
            ),
        )

    def _run_caption(self, job: dict, log_path: Path) -> None:
        job_id = job["id"]
        params = self._job_params(job)
        if not isinstance(params.get("dir"), str) or not Path(params["dir"]).is_dir():
            self._caption_fail(job_id, log_path, "missing or invalid param(s): dir")
            return
        try:
            adapter = self._load_caption_adapter()
        except Exception as exc:
            self._caption_fail(job_id, log_path, "caption preflight failed: %s" % _tail(str(exc), 300))
            return
        job_dir = self.app.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        stop_file = job_dir / "stop"
        metrics_file = job_dir / "metrics.json"
        try:
            stop_file.unlink()
        except FileNotFoundError:
            pass
        initial = {
            "job_id": job_id,
            "state": "starting",
            "phase": "starting",
            "done": 0,
            "total": 0,
            "images_total": 0,
            "written": 0,
            "skipped": 0,
            "failed": 0,
            "truncated": 0,
        }
        self.app.store.update(
            job_id, state="running", error=None, metrics_json=json_text(initial)
        )
        monitor_stop = threading.Event()
        cancel_event = threading.Event()
        state = {"stop_written": False, "last": None}

        def _monitor():
            while not monitor_stop.wait(0.1):
                current = self.app.store.get(job_id)
                if current is None:
                    break
                if (current["cancel_requested"] or self.stop_event.is_set()) and not state["stop_written"]:
                    try:
                        stop_file.write_text("cancel\n", encoding="utf-8")
                    except OSError:
                        pass
                    cancel_event.set()
                    state["stop_written"] = True
                metrics = self._read_metrics_quiet(metrics_file)
                if metrics is None:
                    continue
                text = json_text(metrics)
                if text == state["last"]:
                    continue
                self.app.store.update(job_id, metrics_json=text)
                state["last"] = text

        monitor = threading.Thread(target=_monitor, name="caption-metrics-%s" % job_id, daemon=True)
        monitor.start()

        def _on_caption_progress(payload):
            if not isinstance(payload, dict):
                return
            current = self.app.store.get(job_id)
            try:
                metrics = json.loads(current.get("metrics_json") or "{}") if current else {}
            except ValueError:
                metrics = {}
            if not isinstance(metrics, dict):
                metrics = {}
            metrics.update(payload)
            metrics["phase"] = "training"
            self.app.store.update(job_id, metrics_json=json_text(metrics))

        result = None
        try:
            kwargs = {
                "stop_file": stop_file,
                "metrics_path": metrics_file,
                "cancel_event": cancel_event,
                "metrics_cb": _on_caption_progress,
            }
            if params.get("server_cmd") is not None:
                kwargs["server_cmd"] = params["server_cmd"]
            result = adapter.run({"id": job_id, "params": params}, log_path, **kwargs)
        except Exception as exc:
            first_line = str(exc).splitlines()[0] if str(exc) else ""
            error = "caption adapter error: %s: %s" % (
                type(exc).__name__,
                _tail(first_line, 400),
            )
            self._append_log(log_path, error)
            self._append_log(log_path, traceback.format_exc())
            result = {
                "state": "failed",
                "returncode": None,
                "metrics": {"state": "failed", "error": error},
            }
        finally:
            monitor_stop.set()
            monitor.join(timeout=2)
            try:
                stop_file.unlink()
            except FileNotFoundError:
                pass
        final = result.get("metrics") if isinstance(result, dict) else None
        if not isinstance(final, dict):
            final = self._read_metrics_quiet(metrics_file) or {}
        final = dict(final)
        final.setdefault("job_id", job_id)
        final.setdefault("done", final.get("written", 0))
        final.setdefault("total", final.get("images_total", 0))
        final["phase"] = "finished"
        current = self.app.store.get(job_id)
        cancelled = bool(current and current["cancel_requested"])
        result_state = result.get("state") if isinstance(result, dict) else None
        if result_state == "cancelled" or cancelled:
            final_state, error = "cancelled", "cancelled by request"
        elif result_state == "succeeded" or (
            isinstance(result, dict) and result.get("returncode") == 0
        ):
            final_state, error = "succeeded", None
        else:
            final_state, error = "failed", final.get("error") or "caption adapter failed"
        final["state"] = final_state
        if error:
            final["error"] = error
        self.app.store.update(
            job_id,
            state=final_state,
            error=error,
            exit_code=result.get("returncode") if isinstance(result, dict) else None,
            metrics_json=json_text(final),
            pid=final.get("pid") or (current or {}).get("pid"),
        )
        self._append_log(
            log_path,
            "core: caption final state=%s done=%s/%s"
            % (final_state, final.get("done"), final.get("total")),
        )

    @staticmethod
    def _append_log(path: Path, message: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("[%s] %s\n" % (utc_now(), message))
        except OSError:
            pass


# ===========================================================================
# [CM-10] COREAPP: config resolution / model cache / downloads / VRAM / orphan
# 职责: 进程级共享状态与服务生命周期（非 HTTP 解析）。
# 上游: main() 构造 + CoreHandler 调用; 下游: JobStore/Scheduler/文件系统.
# ===========================================================================

# ---------------------------------------------------------------------------
# core application
# ---------------------------------------------------------------------------


class CoreApp:
    def __init__(
        self,
        data_dir: Path,
        project_root: Path,
        requested_port: int,
        laya_python=None,
        laya_src=None,
        llm_python=None,
        aitk_python=None,
        aitk_libs=None,
        aitk_src=None,
        diffsynth_python=None,
        diffsynth_src=None,
        diffsynth_libs=None,
        lf_python=None,
        lf_libs=None,
        lf_src=None,
        models_dir=None,
        assets_dir=None,
        pick_stub=None,
        report_llm=None,
        report_timeout=60.0,
        vram_margin_mib=1500,
        no_vram_check=False,
        no_orphan_reap=False,
        vram_cmd="nvidia-smi",
        ram_probe_fail=False,
        eval_cli_stub=None,
        eval_timeout=300.0,
    ):
        self.data_dir = Path(data_dir).resolve()
        self.project_root = Path(project_root).resolve()
        self.logs_dir = self.data_dir / "logs"
        self.locks_dir = self.data_dir / "locks"
        self.jobs_dir = self.data_dir / "jobs"
        for directory in (self.data_dir, self.logs_dir, self.locks_dir, self.jobs_dir):
            directory.mkdir(parents=True, exist_ok=True)

        self.db_path = self.data_dir / "core.db"
        self.token_path = self.data_dir / "token"
        self.core_json_path = self.data_dir / "core.json"
        self.lock_path = self.locks_dir / "gpu-0.lock"
        self.ui_path = self.project_root / "design" / "prototype" / "index.html"
        self.v3_dir = self.project_root / "design" / "prototype" / "v3"
        self.v3_index_path = self.v3_dir / "index.html"
        raw_assets_dir = (
            assets_dir
            or os.environ.get("MODELSMITH_ASSETS_DIR")
            or (self.project_root / "core" / "assets")
        )
        self.assets_dir = Path(raw_assets_dir).expanduser()
        if not self.assets_dir.is_absolute():
            self.assets_dir = self.project_root / self.assets_dir
        self.assets_dir = self.assets_dir.resolve()
        self.wire_path = self.assets_dir / "ms-wire.js"

        self.laya_python = Path(
            laya_python or os.environ.get("MODELSMITH_LAYA_PYTHON") or DEFAULT_LAYA_PYTHON
        ).expanduser()
        if not self.laya_python.is_absolute():
            self.laya_python = self.project_root / self.laya_python
        raw_src = laya_src or os.environ.get("MODELSMITH_LAYA_SRC") or DEFAULT_LAYA_SRC
        self.laya_src = Path(raw_src).expanduser()
        if not self.laya_src.is_absolute():
            self.laya_src = self.project_root / self.laya_src
        self.laya_src = self.laya_src.resolve()
        self.adapters_dir = self.project_root / "adapters"
        self.laya_adapter_path = self.adapters_dir / "laya_adapter.py"
        self.llm_python = Path(
            llm_python or os.environ.get("MODELSMITH_LLM_PYTHON") or DEFAULT_LLM_PYTHON
        ).expanduser()
        if not self.llm_python.is_absolute():
            self.llm_python = self.project_root / self.llm_python
        self.llm_script_path = self.adapters_dir / "llm_lora.py"
        self.aitk_python = Path(
            aitk_python or os.environ.get("MODELSMITH_AITK_PYTHON") or DEFAULT_AITK_PYTHON
        ).expanduser()
        if not self.aitk_python.is_absolute():
            self.aitk_python = self.project_root / self.aitk_python
        raw_aitk_libs = (
            aitk_libs or os.environ.get("MODELSMITH_AITK_LIBS") or DEFAULT_AITK_LIBS
        )
        self.aitk_libs = Path(raw_aitk_libs).expanduser()
        if not self.aitk_libs.is_absolute():
            self.aitk_libs = self.project_root / self.aitk_libs
        self.aitk_libs = self.aitk_libs.resolve()
        raw_aitk_src = (
            aitk_src or os.environ.get("MODELSMITH_AITK_SRC") or DEFAULT_AITK_SRC
        )
        self.aitk_src = Path(raw_aitk_src).expanduser()
        if not self.aitk_src.is_absolute():
            self.aitk_src = self.project_root / self.aitk_src
        self.aitk_src = self.aitk_src.resolve()
        self.aitk_adapter_path = self.adapters_dir / "aitk_adapter.py"
        self.diffsynth_python = Path(
            diffsynth_python
            or os.environ.get("MODELSMITH_DIFFSYNTH_PYTHON")
            or DEFAULT_DIFFSYNTH_PYTHON
        ).expanduser()
        if not self.diffsynth_python.is_absolute():
            self.diffsynth_python = self.project_root / self.diffsynth_python
        raw_diffsynth_src = (
            diffsynth_src
            or os.environ.get("MODELSMITH_DIFFSYNTH_SRC")
            or DEFAULT_DIFFSYNTH_SRC
        )
        self.diffsynth_src = Path(raw_diffsynth_src).expanduser()
        if not self.diffsynth_src.is_absolute():
            self.diffsynth_src = self.project_root / self.diffsynth_src
        self.diffsynth_src = self.diffsynth_src.resolve()
        self.diffsynth_train_py = self.diffsynth_src / Path(DIFFSYNTH_TRAIN_REL)
        self.diffsynth_adapter_path = self.adapters_dir / "diffsynth_adapter.py"
        self.diffsynth_libs = str(diffsynth_libs or "").strip()
        self.lf_python = Path(
            lf_python or os.environ.get("MODELSMITH_LF_PYTHON") or DEFAULT_LF_PYTHON
        ).expanduser()
        if not self.lf_python.is_absolute():
            self.lf_python = self.project_root / self.lf_python
        raw_lf_libs = (
            lf_libs or os.environ.get("MODELSMITH_LF_LIBS") or DEFAULT_LF_LIBS
        )
        self.lf_libs = Path(raw_lf_libs).expanduser()
        if not self.lf_libs.is_absolute():
            self.lf_libs = self.project_root / self.lf_libs
        self.lf_libs = self.lf_libs.resolve()
        raw_lf_src = lf_src or os.environ.get("MODELSMITH_LF_SRC") or DEFAULT_LF_SRC
        self.lf_src = Path(raw_lf_src).expanduser()
        if not self.lf_src.is_absolute():
            self.lf_src = self.project_root / self.lf_src
        self.lf_src = self.lf_src.resolve()
        self.lf_cli_path = self.lf_src / Path(LF_CLI_REL)
        self.lf_adapter_path = self.adapters_dir / "lf_adapter.py"
        self.caption_adapter_path = self.adapters_dir / "caption_adapter.py"

        raw_models_dir = (
            models_dir
            or os.environ.get("MODELSMITH_MODELS_DIR")
            or DEFAULT_MODELS_DIR
        )
        self.models_dir = Path(raw_models_dir).expanduser()
        if not self.models_dir.is_absolute():
            self.models_dir = self.project_root / self.models_dir
        self.models_dir = self.models_dir.resolve()
        self._models_lock = threading.Lock()
        self._models_cache = None
        self._downloads_lock = threading.Lock()
        self._downloads = {}
        self._download_cancels = {}
        self._download_threads = {}
        self.eval_cli_stub = str(eval_cli_stub) if eval_cli_stub else None
        self.eval_timeout = float(eval_timeout or 300.0)
        self._pick_stub = pick_stub
        self._pick_stub_parts = (pick_stub or "").split("|") if pick_stub else []
        self._pick_stub_index = 0
        self._pick_stub_lock = threading.Lock()
        self.report_llm = report_llm or ""
        self.report_timeout = float(report_timeout or 60.0)
        self.report_llm_script = self.project_root / "core" / "report_llm.py"
        self.vram_margin_mib = int(vram_margin_mib)
        self.no_vram_check = bool(no_vram_check)
        self.no_orphan_reap = bool(no_orphan_reap)
        self.vram_cmd = str(vram_cmd or "nvidia-smi")
        self.ram_probe_fail = bool(ram_probe_fail)

        self.requested_port = int(requested_port)
        self.port = int(requested_port)
        self.token = load_or_create_token(self.token_path)
        self.store = JobStore(self.db_path)
        self.stop_event = threading.Event()
        self.scheduler = Scheduler(self)
        self.server = None
        self.started_monotonic = time.monotonic()
        self.started_at = utc_now()
        self.code_sha256 = None
        self.code_mtime = None
        try:
            code_path = Path(__file__).resolve()
            self.code_sha256 = hashlib.sha256(code_path.read_bytes()).hexdigest()[:16]
            self.code_mtime = datetime.fromtimestamp(
                code_path.stat().st_mtime, timezone.utc
            ).isoformat()
        except OSError:
            pass
        self._doctor_lock = threading.Lock()
        self._doctor_cache = (0.0, None)

    # -- paths -------------------------------------------------------------

    # -- models repository --------------------------------------------------

    def models(self, refresh: bool = False) -> dict:
        with self._models_lock:
            now = time.monotonic()
            signature = models_tree_signature(self.models_dir)
            cached = self._models_cache
            if (
                not refresh
                and cached is not None
                and now - cached["cached_at"] < MODEL_SCAN_TTL_S
                and signature == cached["signature"]
            ):
                return dict(cached["payload"], cache_hit=True)
            models, warning = scan_models(self.models_dir)
            payload = {
                "ok": True,
                "root": str(self.models_dir),
                "scanned_at": utc_now(),
                "cache_hit": False,
                "models": models,
            }
            if warning:
                payload["root_warning"] = warning
            self._models_cache = {
                "cached_at": now,
                "signature": signature,
                "payload": payload,
            }
            return payload

    def resolve_model_ref(self, value):
        payload = self.models()
        return resolve_model_reference(
            value, self.models_dir, payload.get("models") or []
        )

    # -- model downloads (F07) ---------------------------------------------

    def _download_public(self, state: dict) -> dict:
        with self._downloads_lock:
            clean = {key: value for key, value in state.items() if not key.startswith("_")}
            return json.loads(json.dumps(clean, ensure_ascii=False))

    def model_download_status(self, model_id: str) -> dict:
        with self._downloads_lock:
            state = self._downloads.get(model_id)
            if state is None:
                raise KeyError(model_id)
            clean = {key: value for key, value in state.items() if not key.startswith("_")}
            return json.loads(json.dumps(clean, ensure_ascii=False))

    def start_model_download(self, model_id: str, sources: list, resume: bool) -> dict:
        entry = next(
            (
                item
                for item in (self.models().get("models") or [])
                if item.get("id") == model_id
            ),
            None,
        )
        if entry is None:
            raise KeyError(model_id)
        if not isinstance(sources, list) or not sources or not all(
            isinstance(item, str) and item.strip() for item in sources
        ):
            raise ValueError("sources must be a non-empty list of URLs")
        entry_path = Path(entry["path"])
        if entry_path.is_dir():
            filename = Path(
                urllib.parse.urlsplit(sources[0]).path
            ).name or "download.bin"
            target = entry_path / filename
        else:
            target = entry_path
        try:
            target.resolve().relative_to(self.models_dir.resolve())
        except ValueError:
            raise ValueError("download target escapes models dir")
        with self._downloads_lock:
            existing = self._downloads.get(model_id)
            if existing is not None:
                return json.loads(json.dumps(existing, ensure_ascii=False))
            state = {
                "id": model_id,
                "state": "running",
                "source": None,
                "bytes": 0,
                "total": None,
                "pct": 0.0,
                "speed_bps": 0.0,
                "eta_s": None,
                "path": None,
                "events": [],
                "errors": [],
                "started_at": utc_now(),
                "finished_at": None,
                "_part_path": str(target) + ".part",
            }
            self._downloads[model_id] = state
            cancel_event = threading.Event()
            self._download_cancels[model_id] = cancel_event

        thread = threading.Thread(
            target=self._download_worker,
            args=(model_id, entry_path, target, list(sources), bool(resume), cancel_event),
            name="model-download-%s" % model_id,
            daemon=True,
        )
        with self._downloads_lock:
            self._download_threads[model_id] = thread
        thread.start()
        return self._download_public(state)

    def cancel_model_download(self, model_id: str) -> dict:
        with self._downloads_lock:
            state = self._downloads.get(model_id)
            cancel = self._download_cancels.get(model_id)
            if state is None:
                raise KeyError(model_id)
            if state.get("state") in ("succeeded", "failed", "cancelled"):
                return json.loads(
                    json.dumps(
                        {k: v for k, v in state.items() if not k.startswith("_")},
                        ensure_ascii=False,
                    )
                )
            if cancel is not None:
                cancel.set()
            part = state.get("_part_path")
            state["state"] = "cancelled"
            state["finished_at"] = utc_now()
            state["_cancel_cleanup"] = True
            thread = self._download_threads.get(model_id)
        if thread is not None:
            thread.join(timeout=5.0)
        if part:
            for _ in range(20):
                try:
                    Path(part).unlink()
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    time.sleep(0.05)
        with self._downloads_lock:
            if thread is not None and thread.is_alive():
                state["_cancel_cleanup"] = True
            else:
                state.pop("_cancel_cleanup", None)
        self._download_event(model_id, {"type": "cancelled"})
        return self._download_public(state)

    def _download_event(self, model_id: str, event: dict) -> None:
        with self._downloads_lock:
            state = self._downloads.get(model_id)
            if state is None:
                return
            state["events"].append(event)
            del state["events"][:-2000]

    def _download_worker(
        self,
        model_id: str,
        entry_path: Path,
        target: Path,
        sources: list,
        resume: bool,
        cancel_event: threading.Event,
    ) -> None:
        errors = []
        part = Path(str(target) + ".part")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            source_name = None
            for index, source in enumerate(sources, start=1):
                source_name = Path(urllib.parse.urlsplit(source).path).name or "download.bin"
                self._download_event(
                    model_id,
                    {"type": "attempt", "source": source, "i": index, "n": len(sources)},
                )
                with self._downloads_lock:
                    state = self._downloads[model_id]
                    state["source"] = source
                try:
                    existing = part.stat().st_size if resume and part.is_file() else 0
                    headers = {"User-Agent": "ModelSmith-core/1"}
                    if existing:
                        headers["Range"] = "bytes=%d-" % existing
                    request = urllib.request.Request(source, headers=headers)
                    with urllib.request.urlopen(request, timeout=30) as response:
                        status = getattr(response, "status", 200)
                        if status == 206 and existing:
                            mode = "ab"
                            start_bytes = existing
                            content_length = int(response.headers.get("Content-Length") or 0)
                            total = start_bytes + content_length if content_length else None
                        else:
                            mode = "wb"
                            start_bytes = 0
                            length = response.headers.get("Content-Length")
                            total = int(length) if length else None
                        with open(part, mode) as fh:
                            bytes_written = start_bytes
                            started = time.monotonic()
                            last_emit = 0.0
                            while True:
                                if cancel_event.is_set():
                                    raise RuntimeError("download cancelled")
                                chunk = response.read(65536)
                                if not chunk:
                                    break
                                fh.write(chunk)
                                bytes_written += len(chunk)
                                elapsed = max(1e-6, time.monotonic() - started)
                                speed = bytes_written / elapsed
                                pct = (
                                    round(100.0 * bytes_written / total, 2)
                                    if total
                                    else None
                                )
                                eta = (
                                    int((total - bytes_written) / speed)
                                    if total and speed > 0
                                    else None
                                )
                                with self._downloads_lock:
                                    state = self._downloads[model_id]
                                    state.update(
                                        {
                                            "bytes": bytes_written,
                                            "total": total,
                                            "pct": pct,
                                            "speed_bps": round(speed, 1),
                                            "eta_s": eta,
                                        }
                                    )
                                now = time.monotonic()
                                if now - last_emit >= 0.2:
                                    self._download_event(
                                        model_id,
                                        {
                                            "type": "progress",
                                            "bytes": bytes_written,
                                            "total": total,
                                            "pct": pct,
                                            "speed_bps": round(speed, 1),
                                            "eta_s": eta,
                                        },
                                    )
                                    last_emit = now
                    # Verify target is still inside models root.
                    resolved_target = target.resolve()
                    resolved_root = self.models_dir.resolve()
                    resolved_target.relative_to(resolved_root)
                    if part.is_file():
                        part.replace(target)
                    self._download_event(
                        model_id, {"type": "done", "path": str(target)}
                    )
                    with self._downloads_lock:
                        state = self._downloads[model_id]
                        state.update(
                            {
                                "state": "succeeded",
                                "path": str(target),
                                "finished_at": utc_now(),
                            }
                        )
                    return
                except Exception as exc:
                    message = "%s: %s" % (source, exc)
                    errors.append(message)
                    self._download_event(
                        model_id, {"type": "error", "source": source, "reason": _tail(str(exc), 300)}
                    )
                    if index < len(sources):
                        self._download_event(
                            model_id,
                            {
                                "type": "switch",
                                "from": source,
                                "to": sources[index],
                                "reason": _tail(str(exc), 300),
                            },
                        )
        finally:
            if cancel_event.is_set():
                try:
                    part.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
            with self._downloads_lock:
                state = self._downloads.get(model_id)
                if state is not None and state.get("state") == "running":
                    state["state"] = "failed"
                    state["errors"] = errors
                    state["finished_at"] = utc_now()
                    state["events"].append(
                        {"type": "failed", "errors": list(errors)}
                    )

    # -- VRAM admission / orphan guard -------------------------------------

    def vram_admission(self, kind: str, params: dict) -> dict:
        if self.no_vram_check:
            return {"ok": True}
        needs = {"laya": 2.0, "lf": 2.0, "diffsynth": 12.0, "aitk": 6.0}
        est = None
        if isinstance(params, dict) and params.get("est_vram_gib") is not None:
            try:
                est = float(params["est_vram_gib"])
            except (TypeError, ValueError):
                return {"ok": False, "status": 400, "error": "params.est_vram_gib must be a number"}
            if est <= 0:
                return {"ok": False, "status": 400, "error": "params.est_vram_gib must be > 0"}
        need = est if est is not None else needs.get(kind)
        if need is None:
            return {"ok": True}
        probe = probe_vram(self.vram_cmd)
        if not probe.get("ok"):
            return {"ok": True, "warning": "vram_probe_failed"}
        used = int(probe["used_mib"])
        total = int(probe["total_mib"])
        return vram_admission_decision(
            used, total, need, self.vram_margin_mib
        )

    def _pid_cmdline(self, pid: int) -> str:
        try:
            completed = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-Command",
                    "try { (Get-CimInstance Win32_Process -Filter \"ProcessId=%d\").CommandLine } catch { '' }"
                    % int(pid),
                ],
                capture_output=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return completed.stdout.decode("utf-8", errors="replace").strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    def _llama_server_pids(self):
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq llama-server.exe", "/FO", "CSV", "/NH"],
                capture_output=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return []
        pids = []
        for line in completed.stdout.decode("utf-8", errors="replace").splitlines():
            parts = line.strip().split('","')
            if len(parts) < 2:
                continue
            raw_pid = parts[1].strip('"')
            if raw_pid.isdigit():
                pids.append(int(raw_pid))
        return pids

    def reap_orphans(self):
        if self.no_orphan_reap:
            return []
        candidates = {}
        # Independent startup sweep: a killed core leaves llama-server alive
        # even after the job row became interrupted; DB state is not trusted.
        for pid in self._llama_server_pids():
            command = self._pid_cmdline(pid)
            if not command:
                continue
            if "--mmproj" in command or str(self.models_dir) in command:
                candidates[pid] = None
        try:
            for row in self.store.list(limit=1000):
                pid = row.get("pid")
                if pid:
                    candidates[int(pid)] = row.get("id")
                try:
                    metrics = json.loads(row.get("metrics_json") or "{}")
                    server_pid = metrics.get("server_pid") if isinstance(metrics, dict) else None
                    if server_pid:
                        candidates[int(server_pid)] = row.get("id")
                except (TypeError, ValueError):
                    pass
        except Exception:
            pass
        jobs_root = self.jobs_dir
        if jobs_root.is_dir():
            for job_dir in jobs_root.iterdir():
                if not job_dir.is_dir():
                    continue
                for name in ("pid", "worker.pid"):
                    path = job_dir / name
                    if not path.is_file():
                        continue
                    try:
                        pid = int(path.read_text(encoding="utf-8").strip())
                    except (OSError, ValueError):
                        continue
                    candidates.setdefault(pid, job_dir.name)
        markers = (
            "modelsmith_core.py",
            "modelsmith_orphan",
            "laya_adapter.py",
            "llm_lora.py",
            "aitk_adapter.py",
            "diffsynth_adapter.py",
            "lf_adapter.py",
            "fake_worker",
            "caption_adapter.py",
            "llama-server.exe",
            "llama-server",
        )
        reaped = []
        for pid, job_id in candidates.items():
            if pid <= 0 or pid == os.getpid():
                continue
            command = self._pid_cmdline(pid)
            if not command or not any(marker in command for marker in markers):
                continue
            if job_id:
                try:
                    (self.jobs_dir / job_id / "stop").write_text(
                        "orphan_reaped\n", encoding="utf-8"
                    )
                except OSError:
                    pass
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if not self._pid_cmdline(pid):
                    break
                time.sleep(0.2)
            if self._pid_cmdline(pid):
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True,
                        timeout=10,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                except (OSError, subprocess.SubprocessError):
                    pass
            if job_id:
                row = self.store.get(job_id)
                if row is not None:
                    self.store.update(
                        job_id,
                        error="orphan_reaped pid=%d" % pid,
                    )
            reaped.append(pid)
            print("reaped orphan pid=%d command=%s" % (pid, _tail(command, 180)), flush=True)
        return reaped

    def log_path_for(self, job_id: str) -> Path:
        return self.logs_dir / ("%s.log" % job_id)

    # -- lifecycle ---------------------------------------------------------

    def start_scheduler(self) -> None:
        self.scheduler.start()

    def shutdown_scheduler(self, timeout: float = 15.0) -> None:
        self.stop_event.set()
        if self.scheduler.is_alive():
            self.scheduler.join(timeout)

    def request_shutdown(self) -> None:
        self.stop_event.set()
        server = self.server
        if server is not None:
            threading.Thread(target=server.shutdown, daemon=True).start()

    def write_core_json(self) -> None:
        payload = {
            "ok": True,
            "host": HOST,
            "port": self.port,
            "base_url": "http://%s:%d" % (HOST, self.port),
            "pid": os.getpid(),
            "version": VERSION,
            "started_at": self.started_at,
            "data_dir": str(self.data_dir),
            "token_file": str(self.token_path),
            "ui_file": str(self.ui_path),
            "ui_exists": self.ui_path.is_file(),
            "ui_bytes": file_size(self.ui_path),
            "laya": {
                "python": str(self.laya_python),
                "src": str(self.laya_src),
                "adapter": str(self.laya_adapter_path),
            },
            "llm": {
                "python": str(self.llm_python),
                "script": str(self.llm_script_path),
            },
            "aitk": {
                "python": str(self.aitk_python),
                "libs": str(self.aitk_libs),
                "src": str(self.aitk_src),
                "adapter": str(self.aitk_adapter_path),
            },
            "diffsynth": {
                "python": str(self.diffsynth_python),
                "src": str(self.diffsynth_src),
                "train_py": str(self.diffsynth_train_py),
                "adapter": str(self.diffsynth_adapter_path),
            },
            "lf": {
                "python": str(self.lf_python),
                "libs": str(self.lf_libs),
                "src": str(self.lf_src),
                "cli": str(self.lf_cli_path),
                "adapter": str(self.lf_adapter_path),
            },
            "models_dir": str(self.models_dir),
            "v3_index": str(self.v3_index_path),
            "legacy_ui": str(self.ui_path),
        }
        atomic_write_text(self.core_json_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    def remove_core_json(self) -> None:
        try:
            current = json.loads(self.core_json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = None
        if isinstance(current, dict) and current.get("pid") not in (None, os.getpid()):
            return
        try:
            self.core_json_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    # -- doctor ------------------------------------------------------------

    def doctor(self, refresh: bool = False) -> dict:
        with self._doctor_lock:
            now = time.monotonic()
            cached_at, cached = self._doctor_cache
            if not refresh and cached is not None and now - cached_at < 30.0:
                return cached
            payload = self._build_doctor()
            self._doctor_cache = (now, payload)
            return payload

    def _build_doctor(self) -> dict:
        disk = shutil.disk_usage(str(self.data_dir))
        counts = self.store.counts_by_state()
        ports = {}
        for port in sorted({DEFAULT_PORT, self.port}):
            ports[str(port)] = {
                "listening": port_listening(port),
                "is_core": port == self.port,
            }
        lock_info = {
            "path": str(self.lock_path),
            "exists": self.lock_path.is_file(),
            "bytes": file_size(self.lock_path),
        }
        if self.lock_path.is_file():
            try:
                lock_info["content"] = self.lock_path.read_text(
                    encoding="utf-8", errors="replace"
                ).strip()[:300]
            except OSError:
                lock_info["content"] = None
        return {
            "ok": True,
            "core_version": VERSION,
            "generated_at": utc_now(),
            "server": {
                "version": VERSION,
                "pid": os.getpid(),
                "host": HOST,
                "port": self.port,
                "uptime_s": round(time.monotonic() - self.started_monotonic, 3),
                "db_journal_mode": self.store.journal_mode(),
                "jobs": counts,
            },
            "python": {
                "version": sys.version.split()[0],
                "full_version": sys.version,
                "executable": sys.executable,
                "bits": 64 if sys.maxsize > 2**32 else 32,
                "platform": sys.platform,
            },
            "torch": self._probe_torch(),
            "cuda": self._probe_nvidia(),
            "disk": {
                "path": str(self.data_dir),
                "total_gb": round(disk.total / (1024**3), 1),
                "free_gb": round(disk.free / (1024**3), 1),
                "used_gb": round(disk.used / (1024**3), 1),
            },
            "ports": ports,
            "gpu_lock": lock_info,
            "laya": {
                "python": str(self.laya_python),
                "python_exists": self.laya_python.is_file(),
                "src": str(self.laya_src),
                "src_exists": self.laya_src.is_dir(),
                "adapter": str(self.laya_adapter_path),
                "adapter_exists": self.laya_adapter_path.is_file(),
            },
            "llm": {
                "python": str(self.llm_python),
                "python_exists": self.llm_python.is_file(),
                "script": str(self.llm_script_path),
                "script_exists": self.llm_script_path.is_file(),
            },
            "aitk": {
                "python": str(self.aitk_python),
                "python_exists": self.aitk_python.is_file(),
                "libs": str(self.aitk_libs),
                "libs_exists": self.aitk_libs.is_dir(),
                "src": str(self.aitk_src),
                "src_exists": self.aitk_src.is_dir(),
                "run_py_exists": (self.aitk_src / "run.py").is_file(),
                "adapter": str(self.aitk_adapter_path),
                "adapter_exists": self.aitk_adapter_path.is_file(),
            },
            "diffsynth": {
                "python": str(self.diffsynth_python),
                "python_exists": self.diffsynth_python.is_file(),
                "src": str(self.diffsynth_src),
                "src_exists": self.diffsynth_src.is_dir(),
                "train_py": str(self.diffsynth_train_py),
                "train_py_exists": self.diffsynth_train_py.is_file(),
                "adapter": str(self.diffsynth_adapter_path),
                "adapter_exists": self.diffsynth_adapter_path.is_file(),
            },
            "paths": {
                "data_dir": str(self.data_dir),
                "logs_dir": str(self.logs_dir),
                "db_path": str(self.db_path),
                "token_file": str(self.token_path),
                "project_root": str(self.project_root),
                "ui_file": str(self.ui_path),
                "ui_exists": self.ui_path.is_file(),
                "ui_bytes": file_size(self.ui_path),
                "v3_dir": str(self.v3_dir),
                "v3_index": str(self.v3_index_path),
                "v3_index_exists": self.v3_index_path.is_file(),
                "wire_js": str(self.wire_path),
                "wire_js_exists": self.wire_path.is_file(),
                "models_dir": str(self.models_dir),
                "models_dir_exists": self.models_dir.is_dir(),
            },
        }

    @staticmethod
    def _probe_torch() -> dict:
        python_path = os.environ.get(
            "MODELSMITH_TORCH_PYTHON", _COMFY_PYTHON
        )
        result = {
            "checked_python": python_path,
            "found": False,
            "version": None,
            "origin": None,
            "error": None,
        }
        if not Path(python_path).is_file():
            result["error"] = "probe python not found"
            return result
        code = (
            "import json, importlib.util\n"
            "import importlib.metadata as md\n"
            "spec = importlib.util.find_spec('torch')\n"
            "version = None\n"
            "if spec is not None:\n"
            "    try:\n"
            "        version = md.version('torch')\n"
            "    except Exception:\n"
            "        version = 'unknown'\n"
            "print(json.dumps({'found': spec is not None, 'version': version, "
            "'origin': (spec.origin if spec else None)}))\n"
        )
        try:
            completed = subprocess.run(
                [python_path, "-c", code],
                capture_output=True,
                timeout=25,
                creationflags=_CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            result["error"] = _tail(str(exc), 300)
            return result
        stdout = completed.stdout.decode("utf-8", errors="replace").strip()
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        if completed.returncode != 0:
            result["error"] = _tail(stderr or stdout, 300)
            return result
        try:
            payload = json.loads(stdout.splitlines()[-1])
        except (ValueError, IndexError):
            result["error"] = "unparseable probe output: " + _tail(stdout, 200)
            return result
        result["found"] = bool(payload.get("found"))
        result["version"] = payload.get("version")
        result["origin"] = payload.get("origin")
        return result

    @staticmethod
    def _probe_nvidia() -> dict:
        exe = shutil.which("nvidia-smi") or "C:\\Windows\\System32\\nvidia-smi.exe"
        base = {
            "status": "unknown",
            "driver_version": None,
            "gpus": [],
            "error": None,
        }
        if not Path(exe).is_file():
            base["error"] = "nvidia-smi not found"
            return base
        query = "name,driver_version,memory.total,memory.used"
        try:
            completed = subprocess.run(
                [exe, "--query-gpu=" + query, "--format=csv,noheader,nounits"],
                capture_output=True,
                timeout=10,
                creationflags=_CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            base["error"] = _tail(str(exc), 300)
            return base
        if completed.returncode != 0:
            base["error"] = _tail(
                completed.stderr.decode("utf-8", errors="replace"), 300
            )
            return base
        gpus = []
        for line in completed.stdout.decode("utf-8", errors="replace").splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 4:
                continue
            gpus.append(
                {
                    "name": parts[0],
                    "driver_version": parts[1],
                    "memory_total_mb": parts[2],
                    "memory_used_mb": parts[3],
                }
            )
        base["status"] = "ok" if gpus else "unknown"
        base["gpus"] = gpus
        if gpus:
            base["driver_version"] = gpus[0].get("driver_version")
        return base


# ===========================================================================
# [CM-11] HTTP LAYER: ThreadingHTTPServer + CoreHandler (routing/auth/static)
# 职责: 解析路径/JSON、鉴权、静态 v3、路由到 CoreApp/Scheduler/JobStore。
# 上游: 127.0.0.1:<port>; 下游: CoreApp/JobStore/Scheduler/文件系统.
# 路由清单见 MAP §2；GET 入口在 _handle_get，写入在 _handle_post.
# ===========================================================================

# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class CoreHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # Windows SO_REUSEADDR lets a second process bind the same port silently.
    # Keep it off there so "port already in use" is a real bind error.
    allow_reuse_address = not _IS_WINDOWS

    def __init__(self, address, handler, app: CoreApp):
        self.app = app
        super().__init__(address, handler)


class CoreHandler(BaseHTTPRequestHandler):
    server_version = "ModelSmithCore/" + VERSION
    sys_version = ""
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> CoreApp:
        return self.server.app

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt, *args):
        try:
            sys.stderr.write(
                "[%s] %s %s\n" % (utc_now(), self.address_string(), fmt % args)
            )
            sys.stderr.flush()
        except Exception:
            pass

    def _send(self, status: int, body: bytes, content_type, extra_headers=None):
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD" and body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, status: int, payload: dict):
        self._send(
            status,
            json_text(payload).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _api_error(self, status: int, message: str, **extra):
        payload = {"ok": False, "error": message}
        payload.update(extra)
        self._json(status, payload)

    def _read_json_body(self) -> dict:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return {}
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            raise ApiError(400, "invalid Content-Length")
        if length < 0:
            raise ApiError(400, "invalid Content-Length")
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "body larger than %d bytes" % MAX_BODY_BYTES)
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError:
            raise ApiError(400, "body must be UTF-8 JSON")
        except ValueError as exc:
            raise ApiError(400, "invalid JSON body: %s" % exc)
        if not isinstance(payload, dict):
            raise ApiError(400, "JSON body must be an object")
        return payload

    def _authed(self) -> bool:
        supplied = self.headers.get("X-ModelSmith-Token")
        if not supplied:
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                supplied = auth[7:].strip()
        if not supplied:
            return False
        try:
            return hmac.compare_digest(supplied, self.app.token)
        except TypeError:
            return False

    def _require_auth(self) -> bool:
        if self._authed():
            return True
        self._api_error(
            401,
            "missing or invalid token",
            hint="send the X-ModelSmith-Token header; token file: %s"
            % self.app.token_path,
        )
        return False

    def _job_public(self, row: dict) -> dict:
        job = dict(row)
        try:
            job["params"] = json.loads(job.pop("params_json") or "{}")
        except ValueError:
            job["params"] = {}
            job.pop("params_json", None)
        try:
            job["metrics"] = json.loads(job.pop("metrics_json") or "null")
        except ValueError:
            job["metrics"] = None
            job.pop("metrics_json", None)
        job["cancel_requested"] = bool(job.get("cancel_requested"))
        heartbeat_at = None
        if isinstance(job.get("metrics"), dict):
            heartbeat_at = job["metrics"].get("heartbeat_at")
        job["heartbeat_age_s"] = None
        if heartbeat_at:
            try:
                beat = datetime.fromisoformat(
                    str(heartbeat_at).replace("Z", "+00:00")
                )
                if beat.tzinfo is None:
                    beat = beat.replace(tzinfo=timezone.utc)
                job["heartbeat_age_s"] = max(
                    0, int((datetime.now(timezone.utc) - beat).total_seconds())
                )
            except ValueError:
                job["heartbeat_age_s"] = None
        metrics = job.get("metrics")
        if isinstance(metrics, dict):
            output = metrics.get("output_dir")
            output_path = Path(output) if isinstance(output, str) and output else None
            def _real_artifact(value):
                if not isinstance(value, str) or not value:
                    return False
                candidate = Path(value)
                if not candidate.is_absolute() and output_path is not None:
                    candidate = output_path / candidate
                try:
                    return candidate.is_file() and candidate.stat().st_size > 0
                except OSError:
                    return False
            artifacts = metrics.get("artifacts")
            if isinstance(artifacts, list):
                metrics["artifacts"] = [item for item in artifacts if _real_artifact(item)]
            if metrics.get("artifact") and not _real_artifact(metrics.get("artifact")):
                metrics.pop("artifact", None)
        if job.get("state") == "queued":
            job["queue_position"] = self.app.store.queue_position(job["id"])
        else:
            job["queue_position"] = 0
        log_path = job.get("log_path")
        job["log_size"] = file_size(log_path) if log_path else 0
        return job

    @staticmethod
    def _int_query(qs: dict, name: str, default: int, minimum: int, maximum: int) -> int:
        raw = qs.get(name, [None])[0]
        if raw in (None, ""):
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ApiError(400, "query parameter %r must be an integer" % name)
        return max(minimum, min(maximum, value))

    def _job_id_from_path(self, raw: str) -> str:
        job_id = urllib.parse.unquote(raw)
        if not JOB_ID_RE.match(job_id or ""):
            raise ApiError(400, "invalid job id")
        return job_id

    # -- GET ---------------------------------------------------------------

    def do_GET(self):
        try:
            self._handle_get()
        except ApiError as exc:
            self._api_error(exc.status, exc.message)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self._api_error(
                500, "internal error", detail=_tail(traceback.format_exc(), 800)
            )

    def _handle_get(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        if path in ("/health", "/api/health"):
            self._json(
                200,
                {
                    "ok": True,
                    "version": VERSION,
                    "pid": os.getpid(),
                    "uptime_s": round(
                        time.monotonic() - self.app.started_monotonic, 3
                    ),
                    "host": HOST,
                    "port": self.app.port,
                    "db": self.app.store.journal_mode(),
                    "schema": SCHEMA_VERSION,
                    "jobs": self.app.store.counts_by_state(),
                    "code_sha256": self.app.code_sha256,
                    "code_mtime": self.app.code_mtime,
                    "started_at": self.app.started_at,
                },
            )
            return

        if path in ("/", "/index.html", "/v3", "/v3/"):
            self._serve_v3("index.html")
            return

        if path.startswith("/v3/"):
            self._serve_v3(path[len("/v3/") :])
            return

        if path == "/legacy":
            self._serve_legacy()
            return

        if path == "/ms-wire.js":
            self._serve_wire()
            return

        if path == "/favicon.ico":
            self._send(204, b"", None)
            return

        if path.startswith("/assets/"):
            self._serve_asset(path[len("/assets/") :])
            return

        if path in ("/env/doctor", "/api/env/doctor"):
            if not self._require_auth():
                return
            refresh = qs.get("refresh", ["0"])[0] in ("1", "true", "yes")
            self._json(200, self.app.doctor(refresh=refresh))
            return

        if path == "/api/data/hint":
            if not self._require_auth():
                return
            raw_path = qs.get("path", [None])[0]
            if not raw_path:
                raise ApiError(400, "missing query parameter: path")
            candidate = Path(raw_path).expanduser()
            if not candidate.is_absolute():
                candidate = self.app.project_root / candidate
            try:
                target = candidate.resolve()
            except OSError as exc:
                raise ApiError(400, "invalid path: %s" % exc)
            allowed = False
            for root in (
                self.app.models_dir,
                self.app.project_root / "_runs",
                self.app.project_root,
            ):
                try:
                    target.relative_to(root.resolve())
                    allowed = True
                    break
                except ValueError:
                    continue
            if not allowed:
                raise ApiError(403, "path is outside allowed roots: %s" % target)
            self._json(200, inspect_data_hint(target))
            return

        if path == "/api/dataset/captions":
            if not self._require_auth():
                return
            raw_dir = qs.get("dir", [None])[0]
            if not raw_dir:
                raise ApiError(400, "missing query parameter: dir")
            caption_dir = Path(raw_dir).expanduser()
            if not caption_dir.is_absolute():
                raise ApiError(400, "dir 必须是绝对路径（例如 <绝对路径>）")
            if not caption_dir.is_dir():
                raise ApiError(400, "dir does not exist: %s" % caption_dir)
            try:
                image_exts = normalise_caption_image_exts(
                    qs.get("dataset_image_exts", [None])[0]
                    or qs.get("image_exts", [None])[0]
                )
                caption_ext = normalise_caption_ext(
                    qs.get("ext", [".txt"])[0]
                )
            except ValueError as exc:
                raise ApiError(400, str(exc))
            items = []
            missing = 0
            for image in sorted(
                (
                    entry
                    for entry in caption_dir.iterdir()
                    if entry.is_file() and entry.suffix.lower() in image_exts
                ),
                key=lambda entry: entry.name,
            ):
                caption_path = image.with_suffix(caption_ext)
                has_caption = caption_path.is_file()
                caption = ""
                caption_bytes = 0
                caption_mtime = None
                too_large = False
                if has_caption:
                    try:
                        stat = caption_path.stat()
                        caption_bytes = int(stat.st_size)
                        caption_mtime = (
                            datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                            .isoformat()
                            .replace("+00:00", "Z")
                        )
                        if caption_bytes <= CAPTION_MAX_CAPTION_BYTES:
                            caption = (
                                caption_path.read_bytes()
                                .decode("utf-8-sig", errors="replace")
                                .strip()
                            )
                        else:
                            too_large = True
                    except OSError:
                        has_caption = False
                        caption_bytes = 0
                        caption_mtime = None
                if not has_caption:
                    missing += 1
                items.append(
                    {
                        "name": image.name,
                        "stem": image.stem,
                        "caption": caption,
                        "has_caption": bool(has_caption),
                        "caption_bytes": caption_bytes,
                        "caption_mtime": caption_mtime,
                        "too_large": too_large,
                    }
                )
            self._json(
                200,
                {
                    "ok": True,
                    "dir": str(caption_dir.resolve()),
                    "count": len(items),
                    "missing_caption": missing,
                    "items": items,
                },
            )
            return

        if path == "/api/caption/backend":
            if not self._require_auth():
                return
            try:
                payload = caption_backend_status()
            except Exception as exc:
                payload = {
                    "ok": True,
                    "backends": [],
                    "engine_available": False,
                    "reason": "backend probe failed: %s" % _tail(str(exc), 300),
                }
            self._json(200, payload)
            return

        if path == "/api/vram":
            if not self._require_auth():
                return
            payload = probe_vram(self.app.vram_cmd)
            if self.app.ram_probe_fail:
                payload.update(system_memory_status(probe=lambda: (_ for _ in ()).throw(RuntimeError("ram probe disabled"))))
            else:
                payload.update(system_memory_status())
            self._json(200, payload)
            return

        if path == "/api/models":
            if not self._require_auth():
                return
            kind = qs.get("kind", [None])[0]
            if kind and kind not in MODEL_KINDS:
                raise ApiError(400, "unknown model kind filter")
            refresh = qs.get("refresh", ["0"])[0] in ("1", "true", "yes")
            payload = dict(self.app.models(refresh=refresh))
            models = list(payload.get("models") or [])
            if kind:
                if kind == "diffusion":
                    models = [
                        item
                        for item in models
                        if item.get("kind") in ("diffusion", "diffusion_single")
                    ]
                else:
                    models = [item for item in models if item.get("kind") == kind]
            models.sort(
                key=lambda item: (
                    0 if item.get("complete") else 1,
                    str(item.get("kind") or ""),
                    str(item.get("name") or "").lower(),
                    str(item.get("id") or ""),
                )
            )
            payload["models"] = models
            self._json(200, payload)
            return

        match = re.match(r"^/api/models/([^/]+)/download/status$", path)
        if match:
            if not self._require_auth():
                return
            model_id = urllib.parse.unquote(match.group(1))
            try:
                state = self.app.model_download_status(model_id)
            except KeyError:
                raise ApiError(404, "download not found")
            self._json(200, {"ok": True, **state})
            return

        if path == "/api/jobs":
            if not self._require_auth():
                return
            state = qs.get("state", [None])[0]
            if state and state not in ALL_STATES:
                raise ApiError(400, "unknown state filter")
            limit = self._int_query(qs, "limit", 100, 1, 500)
            rows = self.app.store.list(state=state, limit=limit)
            self._json(
                200,
                {
                    "ok": True,
                    "jobs": [self._job_public(row) for row in rows],
                    "counts": self.app.store.counts_by_state(),
                },
            )
            return

        match = re.match(r"^/api/jobs/([^/]+)/samples/(.+)$", path)
        if match:
            job_id = self._job_id_from_path(match.group(1))
            self._serve_job_sample(job_id, match.group(2))
            return

        match = re.match(r"^/api/jobs/([^/]+)/artifacts$", path)
        if match:
            if not self._require_auth():
                return
            job_id = self._job_id_from_path(match.group(1))
            self._job_artifacts(job_id)
            return

        match = re.match(r"^/api/jobs/([^/]+)/metrics_history$", path)
        if match:
            if not self._require_auth():
                return
            job_id = self._job_id_from_path(match.group(1))
            if self.app.store.get(job_id) is None:
                raise ApiError(404, "job not found")
            since = self._int_query(qs, "since", 0, 0, 2**31 - 1)
            rows = []
            try:
                history = self.app.jobs_dir / job_id / "metrics.jsonl"
                if history.is_file():
                    with open(history, "r", encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                row = json.loads(line)
                            except ValueError:
                                continue
                            if isinstance(row, dict) and int(row.get("seq") or 0) > since:
                                rows.append(row)
            except OSError:
                rows = []
            next_seq = max([since] + [int(row.get("seq") or 0) for row in rows])
            self._json(200, {"ok": True, "job_id": job_id, "rows": rows, "next_seq": next_seq})
            return

        match = re.match(r"^/api/jobs/([^/]+)/logs$", path)
        if match:
            if not self._require_auth():
                return
            self._get_logs(self._job_id_from_path(match.group(1)), qs)
            return

        match = re.match(r"^/api/jobs/([^/]+)$", path)
        if match:
            if not self._require_auth():
                return
            job_id = self._job_id_from_path(match.group(1))
            row = self.app.store.get(job_id)
            if row is None:
                raise ApiError(404, "job not found")
            self._json(200, {"ok": True, "job": self._job_public(row)})
            return

        # v3 pages use relative links (llm.html, model-index.js, ...), so the
        # root URL space is equivalent to /v3/ for any v3 file that exists.
        relative = path.lstrip("/")
        if relative and "\\" not in relative:
            try:
                target = self._safe_static(self.app.v3_dir, relative)
            except ApiError:
                target = None
            if target is not None:
                self._serve_v3(relative)
                return

        raise ApiError(404, "not found")

    def _safe_static(self, root: Path, relative: str) -> Path:
        decoded = urllib.parse.unquote(urllib.parse.unquote(relative or ""))
        normalized = decoded.replace("\\", "/")
        if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
            raise ApiError(404, "invalid UI path")
        parts = [part for part in normalized.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise ApiError(404, "invalid UI path")
        root_resolved = root.resolve()
        target = root.joinpath(*parts).resolve()
        try:
            target.relative_to(root_resolved)
        except ValueError:
            raise ApiError(404, "invalid UI path")
        if not target.is_file():
            raise ApiError(404, "UI file not found")
        return target

    @staticmethod
    def _static_mime(path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix in (".html", ".htm"):
            return "text/html; charset=utf-8"
        if suffix in (".js", ".mjs"):
            return "application/javascript; charset=utf-8"
        if suffix == ".css":
            return "text/css; charset=utf-8"
        if suffix == ".json":
            return "application/json; charset=utf-8"
        mime, _ = mimetypes.guess_type(str(path))
        return mime or "application/octet-stream"

    def _serve_v3(self, relative: str):
        relative = relative or "index.html"
        if relative.endswith("/"):
            relative += "index.html"
        target = self._safe_static(self.app.v3_dir, relative)
        try:
            data = target.read_bytes()
        except OSError as exc:
            raise ApiError(503, "could not read v3 file: %s" % exc)
        mime = self._static_mime(target)
        if target.suffix.lower() in (".html", ".htm"):
            data = inject_v3_bootstrap(data, self.app.token)
        self._send(200, data, mime, {"Cache-Control": "no-store"})

    def _serve_legacy(self):
        ui = self.app.ui_path
        if not ui.is_file():
            raise ApiError(503, "legacy UI baseline missing: %s" % ui)
        try:
            data = inject_ui_token(ui.read_bytes(), self.app.token)
        except OSError as exc:
            raise ApiError(503, "could not read legacy UI: %s" % exc)
        self._send(200, data, "text/html; charset=utf-8", {"Cache-Control": "no-store"})

    def _serve_wire(self):
        wire = self.app.wire_path
        if not wire.is_file():
            print(
                "[modelsmith] /ms-wire.js requested but missing: %s" % wire,
                file=sys.stderr,
                flush=True,
            )
            raise ApiError(404, "ms-wire.js not found")
        try:
            data = wire.read_bytes()
        except OSError as exc:
            raise ApiError(503, "could not read ms-wire.js: %s" % exc)
        self._send(
            200,
            data,
            "application/javascript; charset=utf-8",
            {"Cache-Control": "no-store"},
        )

    def _serve_asset(self, relative: str):
        relative = urllib.parse.unquote(relative or "")
        if not relative or relative.endswith("/"):
            raise ApiError(404, "asset not found")
        roots = [
            self.app.project_root / "design" / "assets",
            self.app.project_root / "core" / "assets",
            self.app.project_root / "design" / "prototype",
        ]
        for root in roots:
            root_resolved = root.resolve()
            target = (root / relative).resolve()
            try:
                target.relative_to(root_resolved)
            except ValueError:
                continue
            if target.is_file():
                try:
                    data = target.read_bytes()
                except OSError:
                    continue
                mime, _ = mimetypes.guess_type(str(target))
                self._send(200, data, mime or "application/octet-stream")
                return
        raise ApiError(404, "asset not found")

    def _get_logs(self, job_id: str, qs: dict):
        row = self.app.store.get(job_id)
        if row is None:
            raise ApiError(404, "job not found")
        offset = self._int_query(qs, "offset", 0, 0, 2**31 - 1)
        max_lines = self._int_query(qs, "max_lines", 200, 1, 1000)
        log_path = row.get("log_path")
        size = file_size(log_path) if log_path else 0
        start = min(offset, size)
        consumed = 0
        lines = []
        if log_path and Path(log_path).is_file():
            try:
                with open(log_path, "rb") as fh:
                    fh.seek(start)
                    while len(lines) < max_lines and consumed < MAX_LOG_CHUNK_BYTES:
                        raw = fh.readline(65536)
                        if not raw:
                            break
                        consumed += len(raw)
                        lines.append(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
            except OSError as exc:
                raise ApiError(500, "could not read log: %s" % exc)
        next_offset = start + consumed
        self._json(
            200,
            {
                "ok": True,
                "job_id": job_id,
                "offset": start,
                "next_offset": next_offset,
                "size": size,
                "eof": next_offset >= size,
                "lines": lines,
                "max_lines": max_lines,
            },
        )

    def _job_output_dir_info(self, job_id: str):
        row = self.app.store.get(job_id)
        if row is None:
            raise ApiError(404, "job not found")
        try:
            metrics = json.loads(row.get("metrics_json") or "{}")
        except ValueError:
            metrics = {}
        try:
            params = json.loads(row.get("params_json") or "{}")
        except ValueError:
            params = {}
        warnings = []

        def _candidate(value, source):
            if not isinstance(value, str) or not value.strip():
                return None, "%s has no output_dir" % source
            if "\ufffd" in value:
                safe_value = value.encode("unicode_escape").decode("ascii", "replace")
                return None, "%s.output_dir contains U+FFFD: %s" % (source, safe_value)
            target = Path(value).expanduser()
            if not target.is_absolute():
                target = self.app.project_root / target
            target = target.resolve()
            for root in (
                self.app.project_root,
                self.app.models_dir,
                self.app.project_root / "_runs",
                self.app.data_dir,
            ):
                try:
                    target.relative_to(root.resolve())
                    return target, None
                except ValueError:
                    continue
            return None, "%s.output_dir is outside allowed roots: %s" % (source, target)

        metric_value = None
        if isinstance(metrics, dict):
            metric_value = metrics.get("output_dir") or metrics.get("out")
        target, reason = _candidate(metric_value, "metrics")
        if target is not None:
            return target, "metrics", warnings
        if reason:
            warnings.append(reason)
        param_value = params.get("out") if isinstance(params, dict) else None
        target, reason = _candidate(param_value, "params")
        if target is not None:
            return target, "params", warnings
        if reason:
            warnings.append(reason)
        raise ApiError(404, "job has no usable output_dir: %s" % "; ".join(warnings))

    def _job_output_dir(self, job_id: str) -> Path:
        return self._job_output_dir_info(job_id)[0]

    def _serve_job_sample(self, job_id: str, relative: str):
        output = self._job_output_dir(job_id)
        samples = output / "samples"
        if not samples.is_dir():
            raise ApiError(404, "job has no samples directory: %s" % samples)
        decoded = urllib.parse.unquote(urllib.parse.unquote(relative or ""))
        normalized = decoded.replace("\\", "/")
        if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
            raise ApiError(403, "invalid sample path")
        parts = [part for part in normalized.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise ApiError(403, "invalid sample path")
        target = samples.joinpath(*parts).resolve()
        try:
            target.relative_to(samples.resolve())
        except ValueError:
            raise ApiError(403, "invalid sample path")
        if not target.is_file():
            raise ApiError(404, "sample not found")
        suffix = target.suffix.lower()
        mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }.get(suffix, "application/octet-stream")
        try:
            data = target.read_bytes()
        except OSError as exc:
            raise ApiError(503, "could not read sample: %s" % exc)
        self._send(200, data, mime, {"Cache-Control": "no-store"})

    def _job_artifacts(self, job_id: str):
        artifacts = []
        try:
            output, source, _warnings = self._job_output_dir_info(job_id)
        except ApiError:
            self._json(200, {"ok": True, "job_id": job_id, "artifacts": []})
            return
        # Never infer a path from params.out; only metrics-declared output counts.
        if source == "metrics" and output.is_dir():
            candidates = []
            for pattern in ("*.safetensors", "*.sft"):
                candidates.extend(output.glob(pattern))
            samples_dir = output / "samples"
            if samples_dir.is_dir():
                candidates.extend(
                    path for path in samples_dir.rglob("*") if path.is_file()
                )
            for path in sorted(set(candidates), key=str):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if not path.is_file() or stat.st_size <= 0:
                    continue
                artifacts.append(
                    {
                        "name": str(path.relative_to(output)).replace("\\", "/"),
                        "path": str(path),
                        "bytes": int(stat.st_size),
                        "mtime": datetime.fromtimestamp(
                            stat.st_mtime, timezone.utc
                        ).isoformat(),
                    }
                )
        self._json(200, {"ok": True, "job_id": job_id, "artifacts": artifacts})

    def _run_eval_cli(self, job_id: str, data: str, output: Path):
        job_dir = self.app.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        json_path = job_dir / ("eval_cli_%s.json" % datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f"))
        if self.app.eval_cli_stub:
            command = [
                sys.executable,
                self.app.eval_cli_stub,
                data,
                "--model",
                str(output),
                "--json",
                str(json_path),
            ]
        else:
            code = (
                "import sys; sys.path.insert(0, %r); "
                "from laya.evals_cli import main; sys.exit(main())"
                % str(self.app.laya_src)
            )
            command = [
                str(self.app.laya_python),
                "-c",
                code,
                "run",
                data,
                "--model",
                str(output),
                "--device",
                "cuda",
                "--json",
                str(json_path),
            ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=max(1.0, self.app.eval_timeout),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            return None, "eval_cli_timeout"
        except OSError as exc:
            return None, "eval_cli_error: %s" % exc
        if completed.returncode != 0:
            return None, "eval_cli_exit_%s: %s" % (
                completed.returncode,
                _tail(completed.stderr.decode("utf-8", errors="replace"), 400),
            )
        try:
            raw = json.loads(json_path.read_text(encoding="utf-8", errors="strict"))
        except (OSError, ValueError) as exc:
            return None, "eval_cli_json_invalid: %s" % exc
        if not isinstance(raw, dict):
            return None, "eval_cli_json_not_object"
        metrics = raw.get("metrics")
        if not isinstance(metrics, dict):
            metrics = {
                key: raw.get(key)
                for key in (
                    "accuracy",
                    "loss",
                    "ece",
                    "brier_multi",
                    "brier_top1",
                    "mean_conf",
                    "abstain_rate",
                )
                if key in raw
            }
        per_question = raw.get("per_question") or raw.get("per_case") or raw.get("cases") or []
        normalized = []
        for item in per_question[:96]:
            if not isinstance(item, dict):
                continue
            normalized.append(
                {
                    "q": str(item.get("q") or item.get("question") or "")[:80],
                    "answer": item.get("answer") or item.get("prediction"),
                    "expected": item.get("expected") or item.get("gold"),
                    "match": item.get("match", item.get("correct")),
                    "confidence": item.get("confidence"),
                }
            )
        return {
            "metrics": metrics,
            "per_question": normalized,
            "confusion": raw.get("confusion") or [],
            "errors": raw.get("errors") or [],
            "n": raw.get("n") or len(normalized) or 1,
        }, None

    def _handle_eval(self, job_id: str):
        row = self.app.store.get(job_id)
        if row is None:
            raise ApiError(404, "job not found")
        if row.get("state") not in ("succeeded", "failed", "cancelled", "interrupted"):
            raise ApiError(409, "job is not terminal: %s" % row.get("state"))
        body = self._read_json_body()
        data = body.get("data")
        try:
            output, output_source, output_warnings = self._job_output_dir_info(job_id)
        except ApiError as exc:
            self._json(200, {"ok": False, "error": exc.message})
            return
        mode = body.get("mode", "auto")
        if mode not in ("auto", "replay", "cli"):
            raise ApiError(400, "mode must be auto, replay or cli")
        data_text = data.strip() if isinstance(data, str) else ""
        cli_result = None
        cli_warn = None
        if data_text and mode in ("auto", "cli") and (
            mode == "cli"
            or self.app.eval_cli_stub
            or (self.app.laya_python.is_file() and self.app.laya_src.is_dir())
        ):
            cli_result, cli_warn = self._run_eval_cli(job_id, data_text, output)
            if cli_result is not None:
                cli_result.update(
                    {
                        "ok": True,
                        "job_id": job_id,
                        "data_source": "heldout",
                        "mode": "cli",
                        "output_dir_source": output_source,
                    }
                )
                if output_warnings:
                    cli_result["warnings"] = output_warnings
                job_dir = self.app.jobs_dir / job_id
                cache = job_dir / (
                    "eval_%s.json" % datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
                )
                try:
                    cache.write_text(
                        json.dumps(cli_result, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except OSError:
                    pass
                self._json(200, cli_result)
                return
        train_report = output / "train_report.json"
        if not train_report.is_file():
            self._json(
                200,
                {
                    "ok": False,
                    "error": "train_report.json not found: %s" % train_report,
                    "output_dir_source": output_source,
                    "warnings": output_warnings,
                },
            )
            return
        try:
            raw_report = train_report.read_text(encoding="utf-8", errors="strict")
            if "\ufffd" in raw_report:
                raise ValueError("train_report contains U+FFFD")
            report = json.loads(raw_report)
        except (OSError, ValueError) as exc:
            self._json(
                200,
                {
                    "ok": False,
                    "error": "invalid train_report: %s" % exc,
                    "output_dir_source": output_source,
                    "warnings": output_warnings,
                },
            )
            return
        metrics = report.get("metrics") if isinstance(report, dict) else {}
        if not isinstance(metrics, dict) or not metrics:
            after = report.get("after") if isinstance(report, dict) else {}
            if isinstance(after, dict):
                metrics = {
                    "accuracy": after.get("accuracy"),
                    "loss": after.get("loss"),
                    "ece": after.get("ece"),
                    "brier_multi": after.get("brier"),
                    "brier_top1": after.get("brier_top1"),
                    "mean_conf": after.get("mean_confidence"),
                    "abstain_rate": after.get("abstain_rate", 0.0),
                }
        if not isinstance(metrics, dict):
            metrics = {}
        data_source = "heldout" if isinstance(data, str) and data.strip() else "calibration"
        n = report.get("n") or report.get("rows_guess") or report.get("eval_items") or 1
        if isinstance(report, dict) and isinstance(report.get("after"), dict):
            n = report.get("eval_items") or report["after"].get("items") or n
        try:
            n = max(1, int(n))
        except (TypeError, ValueError):
            n = 1
        result = {
            "ok": True,
            "job_id": job_id,
            "data_source": data_source,
            "n": n,
            "metrics": metrics,
            "per_question": [],
            "confusion": list(report.get("confusion") or []),
            "errors": list(report.get("errors") or []),
            "note": "本次训练未落逐条预测",
            "output_dir_source": output_source,
            "mode": "replay",
        }
        if cli_warn:
            result["warn"] = "eval_cli_failed"
            result["eval_cli_error"] = cli_warn
        if output_warnings:
            result["warnings"] = output_warnings
        job_dir = self.app.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        cache = job_dir / ("eval_%s.json" % datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f"))
        try:
            cache.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
        self._json(200, result)

    def _handle_report(self, job_id: str):
        row = self.app.store.get(job_id)
        if row is None:
            raise ApiError(404, "job not found")
        body = self._read_json_body()
        job_dir = self.app.jobs_dir / job_id
        candidates = sorted(job_dir.glob("eval_*.json")) if job_dir.is_dir() else []
        if not candidates:
            self._json(200, {"ok": False, "error": "run /eval first"})
            return
        try:
            evaluation = json.loads(candidates[-1].read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._json(200, {"ok": False, "error": "invalid eval cache: %s" % exc})
            return
        metrics = evaluation.get("metrics") or {}
        errors = evaluation.get("errors") or []
        confusion = evaluation.get("confusion") or []
        data_source = evaluation.get("data_source") or "calibration"
        n = evaluation.get("n") or 0
        model = body.get("model")
        language = body.get("language") or "zh"
        started = time.monotonic()

        def fmt(key, default=0.0):
            try:
                return "%.4f" % float(metrics.get(key, default))
            except (TypeError, ValueError):
                return "%.4f" % default

        summary = {
            "verdict": "指标已回放，待人工确认是否达标",
            "top_cause": "错例主要集中在低置信/边界样本",
            "actions": [
                "用 held-out 复测后再决定是否上线",
                "对错例补数据或提高拒答阈值",
                "校准 in-sample 与 held-out 的差距",
            ],
        }
        error_lines = []
        for item in errors[:5]:
            if isinstance(item, dict):
                error_lines.append(
                    "- Q: %s → answer: %s / expected: %s"
                    % (
                        str(item.get("q") or "")[:80],
                        str(item.get("answer") or "")[:60],
                        str(item.get("expected") or "")[:60],
                    )
                )
        if not error_lines:
            error_lines.append("- 无逐条错例（本次训练未落逐条预测）")
        markdown = "\n".join(
            [
                "# ModelSmith 评测报告",
                "",
                "## 总体",
                "- data_source: %s; n: %s" % (data_source, n),
                "- accuracy: %s; loss: %s; ece: %s" % (fmt("accuracy"), fmt("loss"), fmt("ece")),
                "- brier_top1: %s; mean_conf: %s; abstain_rate: %s"
                % (fmt("brier_top1"), fmt("mean_conf"), fmt("abstain_rate")),
                "",
                "## 错例",
                *error_lines,
                "",
                "## 归因",
                "- 混淆/边界样本需要结合 held-out 复测判断；当前校准切片不能代表泛化。",
                "",
                "## 建议",
                "- 先跑 held-out 评测，再决定是否上线。",
                "- 对高频错例补数据，必要时提高拒答阈值。",
                "- 记录 baseline，按同一口径比较下一轮。",
                "",
                "> in-sample ≠ 泛化：校准切片只代表训练分布，上线前请用 held-out 复测。",
                "",
            ]
        )
        ungrounded = grounded_numbers(markdown, metrics)
        engine = "template"
        warn = None
        llm_error = None
        if self.app.report_llm:
            counts = self.app.store.counts_by_state()
            busy = int(counts.get("running", 0)) + int(counts.get("starting", 0)) > 0
            lock = None
            if not busy:
                lock = GpuLock(self.app.lock_path)
                busy = not lock.acquire(owner="report-%s" % job_id)
            if busy:
                llm_error = "GPU busy or lock unavailable"
            else:
                try:
                    input_path = job_dir / "report_llm_input.json"
                    input_path.write_text(
                        json.dumps(
                            {
                                "metrics": metrics,
                                "errors": errors[:5],
                                "data_source": data_source,
                                "n": n,
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                    llm_dir = job_dir / (
                        "report_llm_%s" % datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
                    )
                    completed = subprocess.run(
                        [
                            str(self.app.llm_python),
                            str(self.app.report_llm_script),
                            "--metrics",
                            str(input_path),
                            "--out",
                            str(llm_dir),
                            "--model",
                            str(self.app.report_llm),
                            "--timeout",
                            str(self.app.report_timeout),
                        ],
                        capture_output=True,
                        timeout=max(1.0, self.app.report_timeout + 10.0),
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    if completed.returncode != 0:
                        llm_error = _tail(
                            completed.stderr.decode("utf-8", errors="replace"), 500
                        ) or "report_llm exited with code %s" % completed.returncode
                    else:
                        llm_md_path = llm_dir / "report.md"
                        llm_md = llm_md_path.read_text(
                            encoding="utf-8", errors="strict"
                        )
                        if not all(section in llm_md for section in (
                            "## 总体",
                            "## 错例",
                            "## 归因",
                            "## 建议",
                        )) or "in-sample ≠ 泛化" not in llm_md:
                            llm_error = "LLM report missing required sections/caveat"
                        else:
                            llm_ungrounded = grounded_numbers(llm_md, metrics)
                            if llm_ungrounded:
                                warn = "ungrounded_numbers"
                                ungrounded = llm_ungrounded
                                llm_error = "LLM report has ungrounded numbers"
                            else:
                                markdown = llm_md
                                engine = "llm"
                                ungrounded = []
                except subprocess.TimeoutExpired:
                    llm_error = "report_llm timed out after %.1fs" % self.app.report_timeout
                except (OSError, ValueError) as exc:
                    llm_error = "%s: %s" % (type(exc).__name__, exc)
                finally:
                    if lock is not None:
                        lock.release()
            if engine != "llm" and warn is None:
                warn = "llm_failed"
        report_json = {
            "job_id": job_id,
            "data_source": data_source,
            "n": n,
            "metrics": metrics,
            "confusion": confusion,
            "errors": errors,
            "summary": summary,
            "engine": engine,
            "generated_at": utc_now(),
        }
        response = {
            "ok": True,
            "engine": engine,
            "model": model or (self.app.report_llm if engine == "llm" else None),
            "elapsed_s": round(time.monotonic() - started, 3),
            "markdown": markdown,
            "json": report_json,
            "output_dir_source": evaluation.get("output_dir_source", "metrics"),
        }
        if evaluation.get("warnings"):
            response["warnings"] = evaluation.get("warnings")
        if warn:
            response["warn"] = warn
        if ungrounded:
            response["ungrounded"] = ungrounded
        if llm_error:
            response["llm_error"] = llm_error
        self._json(200, response)

    # -- POST --------------------------------------------------------------

    def do_POST(self):
        try:
            self._handle_post()
        except ApiError as exc:
            self._api_error(exc.status, exc.message)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self._api_error(
                500, "internal error", detail=_tail(traceback.format_exc(), 800)
            )

    def _handle_post(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        match = re.match(r"^/api/jobs/([^/]+)/eval$", path)
        if match:
            if not self._require_auth():
                return
            self._handle_eval(self._job_id_from_path(match.group(1)))
            return

        match = re.match(r"^/api/jobs/([^/]+)/report$", path)
        if match:
            if not self._require_auth():
                return
            self._handle_report(self._job_id_from_path(match.group(1)))
            return

        match = re.match(r"^/api/models/([^/]+)/download/cancel$", path)
        if match:
            if not self._require_auth():
                return
            model_id = urllib.parse.unquote(match.group(1))
            try:
                state = self.app.cancel_model_download(model_id)
            except KeyError:
                raise ApiError(404, "download not found")
            self._json(200, {"ok": True, **state})
            return

        match = re.match(r"^/api/models/([^/]+)/download$", path)
        if match:
            if not self._require_auth():
                return
            model_id = urllib.parse.unquote(match.group(1))
            body = self._read_json_body()
            sources = body.get("sources")
            resume = bool(body.get("resume", True))
            try:
                state = self.app.start_model_download(model_id, sources, resume)
            except KeyError:
                raise ApiError(404, "model not found")
            except ValueError as exc:
                raise ApiError(400, str(exc))
            self._json(202, {"ok": True, **state,
                             "status_url": "/api/models/%s/download/status" % model_id})
            return

        if path == "/api/pick":
            if not self._require_auth():
                return
            body = self._read_json_body()
            mode = body.get("mode", "file")
            if mode not in ("file", "dir"):
                raise ApiError(400, "mode must be file or dir")
            title = str(body.get("title") or "")
            extensions = body.get("filter") or []
            if isinstance(extensions, str):
                extensions = [extensions]
            if self.app._pick_stub:
                with self.app._pick_stub_lock:
                    parts = self.app._pick_stub_parts or [""]
                    index = min(self.app._pick_stub_index, len(parts) - 1)
                    self.app._pick_stub_index += 1
                    stub = parts[index]
                if stub == "__cancel__":
                    self._json(200, {"ok": False, "cancelled": True})
                    return
                if stub == "__no_desktop__":
                    self._json(200, {"ok": False, "reason": "no_desktop"})
                    return
                time.sleep(1.0)
                self._json(200, {"ok": True, "path": stub})
                return
            result = native_pick(mode, title, extensions)
            self._json(200, result)
            return

        if path == "/api/open":
            if not self._require_auth():
                return
            body = self._read_json_body()
            raw_path = body.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise ApiError(400, "path must be a non-empty string")
            candidate = Path(raw_path).expanduser()
            if not candidate.is_absolute():
                candidate = self.app.project_root / candidate
            try:
                target = candidate.resolve()
            except OSError as exc:
                raise ApiError(400, "invalid path: %s" % exc)
            allowed_roots = [
                self.app.models_dir,
                self.app.project_root / "_runs",
                self.app.project_root,
            ]
            allowed = False
            for root in allowed_roots:
                try:
                    target.relative_to(root.resolve())
                    allowed = True
                    break
                except ValueError:
                    continue
            if not allowed:
                raise ApiError(403, "path is outside allowed roots: %s" % target)
            dry = qs.get("dry", ["0"])[0] in ("1", "true", "yes")
            if not dry:
                if not target.exists():
                    raise ApiError(404, "path not found: %s" % target)
                try:
                    subprocess.Popen(
                        ["explorer", str(target)],
                        shell=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except OSError as exc:
                    raise ApiError(500, "could not open path: %s" % exc)
            self._json(
                200, {"ok": True, "dry": dry, "path": str(target)}
            )
            return

        match = re.match(r"^/api/jobs/([^/]+)/early_stop$", path)
        if match:
            if not self._require_auth():
                return
            job_id = self._job_id_from_path(match.group(1))
            row = self.app.store.get(job_id)
            if row is None:
                raise ApiError(404, "job not found")
            body = self._read_json_body()
            enabled = bool(body.get("enabled", False))
            try:
                patience = int(body.get("patience", 2))
                min_delta = float(body.get("min_delta", 0.01))
            except (TypeError, ValueError):
                raise ApiError(400, "patience/min_delta must be numbers")
            if patience < 1 or min_delta < 0:
                raise ApiError(400, "patience>=1 and min_delta>=0 required")
            try:
                params = json.loads(row.get("params_json") or "{}")
            except ValueError:
                params = {}
            if not isinstance(params, dict):
                params = {}
            params["early_stop"] = {
                "enabled": enabled,
                "patience": patience,
                "min_delta": min_delta,
            }
            self.app.store.update(job_id, params_json=json_text(params))
            self._json(
                200,
                {"ok": True, "job_id": job_id, "early_stop": params["early_stop"]},
            )
            return

        match = re.match(r"^/api/jobs/([^/]+)/star$", path)
        if match:
            if not self._require_auth():
                return
            job_id = self._job_id_from_path(match.group(1))
            row = self.app.store.get(job_id)
            if row is None:
                raise ApiError(404, "job not found")
            body = self._read_json_body()
            artifact = body.get("artifact")
            starred = body.get("starred", True)
            if not isinstance(artifact, str) or not artifact.strip():
                raise ApiError(400, "artifact must be a non-empty string")
            artifact = artifact.strip()
            try:
                metrics = json.loads(row.get("metrics_json") or "{}")
            except ValueError:
                metrics = {}
            if not isinstance(metrics, dict):
                metrics = {}
            values = list(metrics.get("starred_artifacts") or [])
            if starred:
                if artifact not in values:
                    values.append(artifact)
            else:
                values = [item for item in values if item != artifact]
            metrics["starred_artifacts"] = values
            self.app.store.update(job_id, metrics_json=json_text(metrics))
            self._json(
                200,
                {
                    "ok": True,
                    "job_id": job_id,
                    "starred_artifacts": values,
                },
            )
            return

        if path == "/api/dataset/captions":
            if not self._require_auth():
                return
            body = self._read_json_body()
            raw_dir = body.get("dir")
            if not isinstance(raw_dir, str) or not raw_dir:
                raise ApiError(400, "missing or invalid param: dir")
            caption_dir = Path(raw_dir).expanduser()
            if not caption_dir.is_absolute():
                raise ApiError(400, "dir 必须是绝对路径（例如 <绝对路径>）")
            if not caption_dir.is_dir():
                raise ApiError(400, "dir does not exist: %s" % caption_dir)
            items = body.get("items")
            if not isinstance(items, list):
                raise ApiError(400, "items must be a list")
            if len(items) > 2000:
                raise ApiError(400, "too many items (max 2000)")
            try:
                image_exts = normalise_caption_image_exts(
                    body.get("dataset_image_exts")
                )
                caption_ext = normalise_caption_ext(body.get("ext"))
            except ValueError as exc:
                raise ApiError(400, str(exc))
            root = caption_dir.resolve()
            validated = []
            for item in items:
                if not isinstance(item, dict):
                    raise ApiError(400, "each item must be an object")
                name = item.get("name")
                caption = item.get("caption")
                if not isinstance(name, str) or not name:
                    raise ApiError(400, "item name must be a non-empty string")
                if (
                    name in (".", "..")
                    or ".." in Path(name).parts
                    or "/" in name
                    or "\\" in name
                    or Path(name).is_absolute()
                ):
                    raise ApiError(400, "invalid caption name: %s" % name)
                if Path(name).name != name:
                    raise ApiError(400, "invalid caption name: %s" % name)
                if not isinstance(caption, str):
                    raise ApiError(400, "caption must be a string: %s" % name)
                image = caption_dir / name
                if not image.is_file() or image.suffix.lower() not in image_exts:
                    raise ApiError(400, "image not found: %s" % name)
                try:
                    image.resolve().relative_to(root)
                    target = image.with_suffix(caption_ext)
                    target.resolve().relative_to(root)
                except (OSError, ValueError):
                    raise ApiError(400, "invalid caption name: %s" % name)
                validated.append((name, image, target, caption))
            written = 0
            deleted = 0
            skipped = 0
            errors = []
            for name, image, target, caption in validated:
                if not caption.strip():
                    try:
                        target.unlink()
                        deleted += 1
                    except FileNotFoundError:
                        skipped += 1
                    except OSError as exc:
                        skipped += 1
                        errors.append({"name": name, "reason": str(exc)})
                    continue
                text = caption if caption.endswith("\n") else caption + "\n"
                temp_path = target.with_name(target.name + ".tmp")
                try:
                    temp_path.write_bytes(text.encode("utf-8"))
                    os.replace(temp_path, target)
                    written += 1
                except OSError as exc:
                    skipped += 1
                    errors.append({"name": name, "reason": str(exc)})
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass
            self._json(
                200,
                {
                    "ok": True,
                    "written": written,
                    "deleted": deleted,
                    "skipped": skipped,
                    "errors": errors,
                },
            )
            return

        if path == "/api/jobs":
            if not self._require_auth():
                return
            self._create_job()
            return

        match = re.match(r"^/api/jobs/([^/]+)/cancel$", path)
        if match:
            if not self._require_auth():
                return
            self._cancel_job(self._job_id_from_path(match.group(1)))
            return

        if path == "/admin/shutdown":
            if not self._require_auth():
                return
            self._json(
                200, {"ok": True, "message": "shutting down", "pid": os.getpid()}
            )
            threading.Timer(0.15, self.app.request_shutdown).start()
            return

        raise ApiError(404, "not found")

    def _create_job(self):
        body = self._read_json_body()
        kind = body.get("kind")
        if not isinstance(kind, str) or not KIND_RE.match(kind.strip()):
            raise ApiError(400, "kind must match %s" % KIND_RE.pattern)
        kind = kind.strip()
        params = body.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ApiError(400, "params must be an object")
        diffsynth_warning = None
        if kind == "diffsynth":
            try:
                sys.path.insert(0, str(self.app.project_root))
                import adapters.diffsynth_adapter as diffsynth_adapter
                known_archs = tuple(sorted(diffsynth_adapter.ARCHES))
            except Exception:
                known_archs = ("moss_image", "qwen_image_21")
            arch = params.get("arch") or "qwen_image_21"
            if arch not in known_archs:
                raise ApiError(
                    400,
                    "unknown arch %r; known archs: %s"
                    % (arch, ", ".join(known_archs)),
                )
            params = dict(params)
            params["arch"] = arch
            python_paths = params.get("python_paths", [])
            if isinstance(python_paths, str):
                python_paths = [python_paths]
            if not isinstance(python_paths, list) or not all(
                isinstance(item, str) for item in python_paths
            ):
                raise ApiError(400, "python_paths must be a string or list of strings")
            resolved_paths, libs_warning, libs_source = resolve_diffsynth_libs(
                self.app.project_root,
                self.app.diffsynth_libs,
                python_paths,
                arch,
            )
            if arch == "moss_image" and not resolved_paths:
                # Installed layout keeps runtime next to <data_dir>\state.
                alt_paths, alt_warning, alt_source = resolve_diffsynth_libs(
                    self.app.data_dir.parent,
                    self.app.diffsynth_libs,
                    python_paths,
                    arch,
                )
                if alt_paths:
                    resolved_paths, libs_warning, libs_source = (
                        alt_paths,
                        alt_warning,
                        alt_source,
                    )
            params["python_paths"] = resolved_paths
            image_exts = params.get("dataset_image_exts")
            if image_exts is not None:
                if isinstance(image_exts, str):
                    if not image_exts.strip():
                        params.pop("dataset_image_exts", None)
                elif isinstance(image_exts, list) and all(
                    isinstance(item, str) for item in image_exts
                ):
                    if not image_exts:
                        params.pop("dataset_image_exts", None)
                else:
                    raise ApiError(
                        400,
                        "dataset_image_exts must be a comma string or list of strings",
                    )
            default_caption = params.get("dataset_default_caption")
            if default_caption is not None:
                if not isinstance(default_caption, str):
                    raise ApiError(400, "dataset_default_caption must be a string")
                if not default_caption.strip():
                    params.pop("dataset_default_caption", None)
            resume_from = params.get("resume_from")
            if resume_from is not None:
                if not isinstance(resume_from, str):
                    raise ApiError(400, "resume_from must be an absolute *.safetensors path")
                resume_path = Path(resume_from).expanduser()
                if not resume_path.is_absolute() or resume_path.suffix.lower() != ".safetensors" or not resume_path.is_file():
                    raise ApiError(400, "resume_from must be an existing absolute *.safetensors file")
                params["resume_from"] = str(resume_path.resolve())
            diffsynth_warning = libs_warning
            model_ref = params.get("model")
            if isinstance(model_ref, str) and model_ref.strip():
                resolved_model = self.app.resolve_model_ref(model_ref)
                entry = None
                try:
                    resolved_path = Path(resolved_model).resolve()
                    for item in (self.app.models().get("models") or []):
                        if (
                            item.get("id") == model_ref
                            or Path(item.get("path") or "").resolve() == resolved_path
                        ):
                            entry = item
                            break
                except OSError:
                    entry = None
                if entry is not None and not entry.get("complete", True):
                    raise ApiError(
                        400,
                        "model incomplete: missing %s"
                        % ", ".join(entry.get("missing") or ["unknown"]),
                    )
            params, components_auto = resolve_components_from_scan(
                arch,
                params,
                self.app.models().get("models") or [],
                self.app.models_dir,
            )
            if components_auto:
                params["components_auto"] = components_auto
            if arch == "flux2_klein_9b":
                flux2_dir = Path(str(params.get("model") or "")).expanduser()
                required = ("transformer", "text_encoder", "vae", "tokenizer")
                missing = [name for name in required if not (flux2_dir / name).exists()]
                if missing:
                    raise ApiError(
                        400,
                        "flux2_klein_9b missing component(s): %s" % ", ".join(missing),
                    )
                flux_tokenizer = str(params.get("tokenizer_path") or "").strip()
                if not flux_tokenizer or not Path(flux_tokenizer).is_dir():
                    raise ApiError(
                        400,
                        "flux2_klein_9b asset input missing: tokenizer_path（模型仓里应有 <model>\\tokenizer，也可 API 显式传 tokenizer_path）",
                    )
            if arch == "krea2_raw":
                params = dict(params)
                auto_derived = []
                if not str(params.get("text_encoder") or "").strip():
                    for item in (self.app.models().get("models") or []):
                        if str(item.get("name") or "").lower() == "qwen3-vl-4b-instruct" or str(item.get("id") or "").lower() == "qwen3-vl-4b-instruct":
                            params["text_encoder"] = item.get("path")
                            auto_derived.append("text_encoder")
                            break
                if not str(params.get("vae") or "").strip():
                    candidate = (
                        self.app.models_dir
                        / "qwen-image"
                        / "vae"
                        / "diffusion_pytorch_model.safetensors"
                    )
                    if candidate.is_file():
                        params["vae"] = str(candidate.resolve())
                        auto_derived.append("vae")
                if auto_derived:
                    params["krea2_auto_derived"] = auto_derived
                krea_raw = Path(str(params.get("model") or "")).expanduser()
                if krea_raw.is_dir():
                    krea_raw = krea_raw / "raw.safetensors"
                krea_te = Path(str(params.get("text_encoder") or "")).expanduser()
                krea_vae = Path(str(params.get("vae") or "")).expanduser()
                missing = []
                if not krea_raw.is_file():
                    missing.append("model(raw)=%s" % krea_raw)
                if not krea_te.is_dir() or not any(krea_te.glob("*.safetensors")):
                    missing.append("text_encoder=%s" % krea_te)
                if not krea_vae.is_file():
                    missing.append("vae=%s" % krea_vae)
                if missing:
                    raise ApiError(
                        400,
                        "krea2_raw 需要模型仓里有 qwen3-vl-4b-instruct 目录与 qwen-image/vae/diffusion_pytorch_model.safetensors（或 API 显式传 text_encoder/vae）；缺: %s"
                        % ", ".join(missing),
                    )
            validate_sample_params(params)
            validate_footprint_params(params)
        if kind == "diffsynth" and isinstance(params.get("dataset"), str):
            dataset_dir = Path(params["dataset"]).expanduser()
            if dataset_dir.is_dir() and any(dataset_dir.glob("*.txt")):
                params = dict(params)
                params["dataset_metadata_hint"] = "local_txt"
        if kind == "caption":
            # All path-like caption params are absolute-only; never resolve
            # against the core process CWD (a relative dir could pick a
            # same-named directory and caption the wrong images silently).
            params = dict(params)
            caption_dir = params.get("dir")
            if not isinstance(caption_dir, str):
                raise ApiError(400, "missing or invalid param: dir")
            caption_path = Path(caption_dir).expanduser()
            if not caption_path.is_absolute():
                raise ApiError(400, "dir 必须是绝对路径（例如 <绝对路径>）")
            if not caption_path.is_dir():
                raise ApiError(400, "missing or invalid param: dir")
            params["dir"] = str(caption_path.resolve())
            backend = str(params.get("backend") or "auto").strip()
            if backend not in ("auto", "qw35-9b", "qw38-27b"):
                raise ApiError(400, "backend must be auto|qw35-9b|qw38-27b")
            params["backend"] = backend
            lang = str(params.get("lang") or "zh").strip()
            if lang not in ("zh", "en"):
                raise ApiError(400, "lang must be zh or en")
            params["lang"] = lang
            ext = str(params.get("caption_ext") or ".txt").strip()
            if not ext.startswith("."):
                ext = "." + ext
            params["caption_ext"] = ext
            overwrite = params.get("overwrite", False)
            if not isinstance(overwrite, bool):
                raise ApiError(400, "overwrite must be a boolean")
            params["overwrite"] = overwrite
            limit = params.get("limit")
            if limit is not None:
                if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                    raise ApiError(400, "limit must be a non-negative integer")
            try:
                max_chars = int(params.get("max_chars", 40))
            except (TypeError, ValueError):
                raise ApiError(400, "max_chars must be an integer")
            params["max_chars"] = max(10, min(200, max_chars))
            batch_size = params.get("batch_size", 6)
            if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 8:
                raise ApiError(400, "batch_size must be an integer in [1, 8]")
            params["batch_size"] = batch_size
            for key in ("model", "mmproj", "engine"):
                value = params.get(key)
                if value is not None:
                    if not isinstance(value, str):
                        raise ApiError(400, "%s must be an absolute existing file" % key)
                    candidate = Path(value).expanduser()
                    if not candidate.is_absolute():
                        raise ApiError(400, "%s must be an absolute path" % key)
                    if not candidate.is_file():
                        raise ApiError(400, "%s must be an existing file" % key)
                    params[key] = str(candidate.resolve())
            if params.get("engine") and not params.get("server_exe"):
                params["server_exe"] = params["engine"]
        if kind == "fake":
            normalized = {}
            if "steps" in params:
                steps = params["steps"]
                if isinstance(steps, bool) or not isinstance(steps, int):
                    raise ApiError(400, "params.steps must be an integer")
                if not 1 <= steps <= 10000:
                    raise ApiError(400, "params.steps must be in [1, 10000]")
                normalized["steps"] = steps
            if "interval_s" in params:
                interval = params["interval_s"]
                if isinstance(interval, bool) or not isinstance(
                    interval, (int, float)
                ):
                    raise ApiError(400, "params.interval_s must be a number")
                if not 0.01 <= float(interval) <= 60.0:
                    raise ApiError(400, "params.interval_s must be in [0.01, 60]")
                normalized["interval_s"] = float(interval)
            if "ignore_soft_stop" in params:
                ignore_soft_stop = params["ignore_soft_stop"]
                if not isinstance(ignore_soft_stop, bool):
                    raise ApiError(
                        400, "params.ignore_soft_stop must be a boolean"
                    )
                normalized["ignore_soft_stop"] = ignore_soft_stop
            params = normalized
        idempotency_key = body.get("idempotency_key")
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) or not idempotency_key.strip():
                raise ApiError(400, "idempotency_key must be a non-empty string")
            if len(idempotency_key) > 128:
                raise ApiError(400, "idempotency_key too long (max 128)")
            idempotency_key = idempotency_key.strip()

        admission = self.app.vram_admission(kind, params)
        if not admission.get("ok"):
            raise ApiError(
                int(admission.get("status") or 409),
                str(admission.get("error") or "insufficient VRAM"),
            )
        row, created = self.app.store.create_or_get(kind, params, idempotency_key)
        if diffsynth_warning:
            self.app.store.update(
                row["id"],
                metrics_json=json_text({"warnings": [diffsynth_warning]}),
            )
        if admission.get("warning"):
            self.app.store.update(
                row["id"],
                metrics_json=json_text({"warnings": [admission["warning"]]}),
            )
        job = self._job_public(row)
        self._json(
            202 if created else 200,
            {
                "ok": True,
                "job_id": row["id"],
                "state": row["state"],
                "queue_position": job["queue_position"],
                "idempotent_replay": not created,
                "job": job,
            },
        )

    def _cancel_job(self, job_id: str):
        row, outcome = self.app.store.request_cancel(job_id)
        if outcome == "missing":
            raise ApiError(404, "job not found")
        if outcome == "terminal":
            self._json(
                409,
                {
                    "ok": False,
                    "error": "job already finished",
                    "job_id": job_id,
                    "state": row["state"],
                },
            )
            return
        self._json(
            200,
            {
                "ok": True,
                "job_id": job_id,
                "state": row["state"],
                "cancel_requested": bool(row["cancel_requested"]),
                "mode": "soft stop file, poll up to %gs, then taskkill /T /F"
                % SOFT_STOP_GRACE_S,
            },
        )


# ===========================================================================
# [CM-12] CLI / STARTUP ORDER: build_parser + main + signals + banner
# 职责: 参数解析、data_dir 选择、CoreApp 构造、recover->reap->scheduler 顺序。
# 上游: start-modelsmith.cmd / python core\modelsmith_core.py;
# 下游: CoreApp.start_scheduler, CoreHTTPServer, OS 信号.
# ===========================================================================

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="modelsmith_core.py",
        description="ModelSmith local core service (MVP, stdlib only)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MODELSMITH_PORT", str(DEFAULT_PORT))),
        help="listen port; 0 picks a random free port and writes core.json (default: %(default)s)",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="state directory (default: %%LOCALAPPDATA%%\\ModelSmith)",
    )
    parser.add_argument(
        "--project-root",
        default=None,
        help="ModelSmith checkout root (default: parent of core/)",
    )
    parser.add_argument(
        "--laya-python",
        default=os.environ.get("MODELSMITH_LAYA_PYTHON", DEFAULT_LAYA_PYTHON),
        help="Python for the Laya child (default: %(default)s)",
    )
    parser.add_argument(
        "--laya-src",
        default=os.environ.get("MODELSMITH_LAYA_SRC", DEFAULT_LAYA_SRC),
        help="Laya source tree injected via -c runner (default: %(default)s)",
    )
    parser.add_argument(
        "--llm-python",
        default=os.environ.get("MODELSMITH_LLM_PYTHON", DEFAULT_LLM_PYTHON),
        help="Python for kind=llm / adapters/llm_lora.py (default: %(default)s)",
    )
    parser.add_argument(
        "--aitk-python",
        default=os.environ.get("MODELSMITH_AITK_PYTHON", DEFAULT_AITK_PYTHON),
        help="Python for kind=aitk (default: %(default)s)",
    )
    parser.add_argument(
        "--aitk-libs",
        default=os.environ.get("MODELSMITH_AITK_LIBS", DEFAULT_AITK_LIBS),
        help="Pinned dependency directory injected before ai-toolkit (default: %(default)s)",
    )
    parser.add_argument(
        "--aitk-src",
        default=os.environ.get("MODELSMITH_AITK_SRC", DEFAULT_AITK_SRC),
        help="ai-toolkit source tree (default: %(default)s)",
    )
    parser.add_argument(
        "--diffsynth-python",
        default=os.environ.get(
            "MODELSMITH_DIFFSYNTH_PYTHON", DEFAULT_DIFFSYNTH_PYTHON
        ),
        help="Python for kind=diffsynth (default: %(default)s)",
    )
    parser.add_argument(
        "--diffsynth-src",
        default=os.environ.get("MODELSMITH_DIFFSYNTH_SRC", DEFAULT_DIFFSYNTH_SRC),
        help="DiffSynth-Studio repo (default: %(default)s)",
    )
    parser.add_argument(
        "--diffsynth-libs",
        default=os.environ.get("MODELSMITH_DIFFSYNTH_LIBS", ""),
        help="default python_paths for diffsynth (empty = auto runtime/pylibs-5 or .aitk-pylibs)",
    )
    parser.add_argument(
        "--lf-python",
        default=os.environ.get("MODELSMITH_LF_PYTHON", DEFAULT_LF_PYTHON),
        help="Python for kind=lf / LlamaFactory (default: %(default)s)",
    )
    parser.add_argument(
        "--lf-libs",
        default=os.environ.get("MODELSMITH_LF_LIBS", DEFAULT_LF_LIBS),
        help="Isolated dependency dir injected before LlamaFactory (default: %(default)s)",
    )
    parser.add_argument(
        "--lf-src",
        default=os.environ.get("MODELSMITH_LF_SRC", DEFAULT_LF_SRC),
        help="LlamaFactory checkout; src/llamafactory/cli.py must exist (default: %(default)s)",
    )
    parser.add_argument(
        "--models-dir",
        default=os.environ.get("MODELSMITH_MODELS_DIR", DEFAULT_MODELS_DIR),
        help="package-local model repository scanned by /api/models (default: %(default)s)",
    )
    parser.add_argument(
        "--assets-dir",
        default=os.environ.get(
            "MODELSMITH_ASSETS_DIR", r"core\assets"
        ),
        help="directory for /ms-wire.js (default: <project-root>/core/assets)",
    )
    parser.add_argument(
        "--pick-stub",
        default=os.environ.get("MODELSMITH_PICK_STUB"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--report-llm",
        default=os.environ.get("MODELSMITH_REPORT_LLM", ""),
        help="local HF model dir for /report LLM engine (empty = template only)",
    )
    parser.add_argument(
        "--report-timeout",
        type=float,
        default=float(os.environ.get("MODELSMITH_REPORT_TIMEOUT", "60")),
        help="LLM report subprocess timeout seconds (default: 60)",
    )
    parser.add_argument(
        "--vram-margin-mib",
        type=int,
        default=int(os.environ.get("MODELSMITH_VRAM_MARGIN_MIB", "1500")),
        help="free-VRAM safety margin MiB (default: 1500)",
    )
    parser.add_argument(
        "--no-vram-check",
        action="store_true",
        default=os.environ.get("MODELSMITH_NO_VRAM_CHECK") == "1",
        help="disable admission VRAM check",
    )
    parser.add_argument(
        "--no-orphan-reap",
        action="store_true",
        default=os.environ.get("MODELSMITH_NO_ORPHAN_REAP") == "1",
        help="disable startup orphan worker reap",
    )
    parser.add_argument(
        "--vram-cmd",
        default=os.environ.get("MODELSMITH_NVIDIA_SMI", "nvidia-smi"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--no-job-object",
        action="store_true",
        default=os.environ.get("MODELSMITH_NO_JOB_OBJECT") == "1",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--ram-probe-fail",
        action="store_true",
        default=False,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--sweep-stale-servers",
        action="store_true",
        default=os.environ.get("MODELSMITH_SWEEP_STALE") == "1",
        help="sweep historical llama-server processes (default: dry-run)",
    )
    parser.add_argument(
        "--platform-probe",
        action="store_true",
        default=False,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--eval-cli-stub",
        default=os.environ.get("MODELSMITH_EVAL_CLI_STUB"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--eval-timeout",
        type=float,
        default=float(os.environ.get("MODELSMITH_EVAL_TIMEOUT", "300")),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--fake-worker", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument("--job-id", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--log", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--metrics", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--stop-file", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--steps", type=int, default=20, help=argparse.SUPPRESS)
    parser.add_argument("--interval", type=float, default=1.0, help=argparse.SUPPRESS)
    parser.add_argument(
        "--ignore-soft-stop", action="store_true", help=argparse.SUPPRESS
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.platform_probe:
        payload = {
            "ok": _platform is not None,
            "platform": _platform.PLATFORM if _platform is not None else "unavailable",
            "import_error": _PLATFORM_IMPORT_ERROR,
            "abilities": {
                "install_child_death_guard": bool(
                    _platform is not None and callable(_platform.install_child_death_guard)
                ),
                "kill_tree": bool(_platform is not None and callable(_platform.kill_tree)),
                "memory_info": bool(_platform is not None and callable(_platform.memory_info)),
                "GpuLock": bool(_platform is not None and _platform.GpuLock is not None),
            },
            "note": "POSIX implementation imported on this host; not executed" if os.name != "nt" else "Windows implementation active",
        }
        print(json_text(payload), flush=True)
        return 0

    if args.fake_worker:
        missing = [
            name
            for name in ("job_id", "log", "metrics", "stop_file")
            if not getattr(args, name)
        ]
        if missing:
            print("fake worker missing arguments: %s" % ", ".join(missing), file=sys.stderr)
            return 2
        return run_fake_worker(args)

    if not 0 <= args.port <= 65535:
        print("ERROR: --port must be in [0, 65535]", file=sys.stderr)
        return 2

    project_root = (
        Path(args.project_root).expanduser()
        if args.project_root
        else MODELSMITH_HOME
    )

    if args.data_dir:
        data_dir = Path(args.data_dir).expanduser()
    else:
        # Portable-first: keep state next to the app. Writing under %LOCALAPPDATA%
        # can fail for the bundled runtime on some machines (ACL / CFA / redirected
        # profile), and only the app folder is guaranteed writable by the app.
        env_dir = os.environ.get("MODELSMITH_DATA_DIR", "").strip()
        candidates = []
        if env_dir:
            candidates.append(Path(env_dir).expanduser())
        candidates.append(project_root / "_state")
        candidates.append(default_data_dir())
        data_dir = candidates[-1]
        for candidate in candidates:
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                probe = candidate / ".write-probe"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
                data_dir = candidate
                break
            except OSError:
                continue
        else:
            data_dir = candidates[-1]

    global JOB_GUARD
    JOB_GUARD = (
        _platform.install_child_death_guard(enabled=not args.no_job_object)
        if _platform is not None
        else None
    )
    try:
        app = CoreApp(
            data_dir=data_dir,
            project_root=project_root,
            requested_port=args.port,
            laya_python=args.laya_python,
            laya_src=args.laya_src,
            llm_python=args.llm_python,
            aitk_python=args.aitk_python,
            aitk_libs=args.aitk_libs,
            aitk_src=args.aitk_src,
            diffsynth_python=args.diffsynth_python,
            diffsynth_src=args.diffsynth_src,
            diffsynth_libs=args.diffsynth_libs,
            lf_python=args.lf_python,
            lf_libs=args.lf_libs,
            lf_src=args.lf_src,
            models_dir=args.models_dir,
            assets_dir=args.assets_dir,
            pick_stub=args.pick_stub,
            report_llm=args.report_llm,
            report_timeout=args.report_timeout,
            vram_margin_mib=args.vram_margin_mib,
            no_vram_check=args.no_vram_check,
            no_orphan_reap=args.no_orphan_reap,
            vram_cmd=args.vram_cmd,
            ram_probe_fail=args.ram_probe_fail,
            eval_cli_stub=args.eval_cli_stub,
            eval_timeout=args.eval_timeout,
        )
    except Exception as exc:
        print("ERROR: could not initialize core state: %s" % exc, file=sys.stderr)
        traceback.print_exc()
        return 1

    try:
        server = CoreHTTPServer((HOST, args.port), CoreHandler, app)
    except OSError as exc:
        app.store.close()
        print(
            "ERROR: cannot bind %s:%d (%s). Choose another --port or --port 0."
            % (HOST, args.port, exc),
            file=sys.stderr,
        )
        return 2

    app.server = server
    app.port = int(server.server_address[1])

    recovered = app.store.recover_interrupted()
    app.reap_orphans()
    app.write_core_json()
    app.start_scheduler()

    def _handle_signal(signum, frame):
        app.request_shutdown()

    for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, signal_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handle_signal)
            except (ValueError, OSError):
                pass

    print("data_dir=%s" % app.data_dir, flush=True)
    print(
        "job_object=%s"
        % (
            "enabled"
            if JOB_GUARD is not None and JOB_GUARD.enabled
            else "degraded: %s"
            % (JOB_GUARD.reason if JOB_GUARD is not None else "not initialized")
        ),
        flush=True,
    )
    print(
        "platform=%s"
        % (_platform.PLATFORM if _platform is not None else "unavailable"),
        flush=True,
    )
    print(
        "ModelSmith core v%s listening on http://%s:%d"
        % (VERSION, HOST, app.port),
        flush=True,
    )
    print("token_file=%s" % app.token_path, flush=True)
    print("core_json=%s" % app.core_json_path, flush=True)
    print("laya_python=%s" % app.laya_python, flush=True)
    print("laya_src=%s" % app.laya_src, flush=True)
    print("llm_python=%s" % app.llm_python, flush=True)
    print("llm_script=%s" % app.llm_script_path, flush=True)
    print("aitk_python=%s" % app.aitk_python, flush=True)
    print("aitk_libs=%s" % app.aitk_libs, flush=True)
    print("aitk_src=%s" % app.aitk_src, flush=True)
    print("diffsynth_python=%s" % app.diffsynth_python, flush=True)
    print("diffsynth_src=%s" % app.diffsynth_src, flush=True)
    print("lf_python=%s" % app.lf_python, flush=True)
    print("lf_libs=%s" % app.lf_libs, flush=True)
    print("lf_src=%s" % app.lf_src, flush=True)
    print("models_dir=%s" % app.models_dir, flush=True)
    print("assets_dir=%s" % app.assets_dir, flush=True)
    print("report_llm=%s" % (app.report_llm or "<template-only>"), flush=True)
    if recovered:
        print(
            "recovered jobs marked interrupted: %s"
            % ", ".join(item["id"] for item in recovered),
            flush=True,
        )

    sweep_stale_caption_servers(dry_run=not args.sweep_stale_servers)

    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        app.stop_event.set()
        app.shutdown_scheduler()
        server.server_close()
        app.store.close()
        app.remove_core_json()
    print("ModelSmith core stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
