#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ModelSmith MCP server — stdio JSON-RPC 2.0, stdlib only.

Exposes the local core service (default http://127.0.0.1:8765) as 9 MCP tools
so an Agent can drive training with fine-grained parameters while the UI keeps
its simple defaults.

Transport: one JSON-RPC 2.0 object per line on stdin/stdout (NOT the LSP
Content-Length framing).  Logs go to stderr; stdout carries frames only.

    python mcp_server\\modelsmith_mcp.py --api http://127.0.0.1:8765

Core must be running first (python core\\modelsmith_core.py).  The token is read
from %LOCALAPPDATA%\\ModelSmith\\token unless --token/--token-file is given, and
is never printed or logged.

Red lines enforced here:
  * params.train_command is a test hook and is rejected for every submit.
  * only one GPU job at a time is the core's job; this server never submits two
    without the caller listing jobs first (documented, not enforced).
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import re
import sys
import time
import traceback
import urllib.parse
from pathlib import Path

VERSION = "0.3.0"
PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "modelsmith"
TERMINAL_STATES = ("succeeded", "failed", "cancelled", "interrupted")
DOWNLOAD_TERMINAL_STATES = ("succeeded", "failed", "cancelled")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
MAX_SAMPLE_B64_BYTES = 2 * 1024 * 1024  # 2 MiB: never push bigger binaries as base64

TOOL_TRAIN_COMMAND_ERROR = "train_command is a test hook, not allowed via MCP"

# Routes the core gained after the first MCP build (R4 v2).  A 404 on any of
# them means "core not upgraded yet" -> degrade to {ok:false, note} and do NOT
# raise; the agent should fall back instead of retrying blindly.
DEGRADE_ROUTES = {
    "modelsmith_list_models": "/api/models",
    "modelsmith_job_metrics_history": "/api/jobs/{id}/metrics_history",
    "modelsmith_eval_job": "/api/jobs/{id}/eval",
    "modelsmith_report_job": "/api/jobs/{id}/report",
    "modelsmith_star_artifact": "/api/jobs/{id}/star",
    "modelsmith_open_path": "/api/open",
    "modelsmith_download_model": "/api/models/{id}/download",
    "modelsmith_job_artifacts": "/api/jobs/{id}/artifacts",
    "modelsmith_job_sample": "/api/jobs/{id}/samples/<rel>",
    "modelsmith_pick_path": "/api/pick",
    "modelsmith_early_stop": "/api/jobs/{id}/early_stop",
}
# 200-with-ok:false is a state answer for most tools, but for a download it
# means the transfer failed -> surface as isError.
ERROR_ON_OK_FALSE = {"modelsmith_download_model"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def log(message: str) -> None:
    text = str(message).replace("\r", " ").replace("\n", " ")
    sys.stderr.write("[modelsmith-mcp] %s\n" % text)
    sys.stderr.flush()


def default_token_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "ModelSmith" / "token"


def read_token(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# HTTP client for the core
# ---------------------------------------------------------------------------


class ApiClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 30.0):
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme not in ("http", ""):
            raise ValueError("--api must be an http:// URL, got %r" % base_url)
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or 80
        self.prefix = parts.path.rstrip("/")
        self.token = token or ""
        self.timeout = float(timeout)

    def request(self, method: str, path: str, body=None, auth: bool = True):
        """Return (status:int, payload:dict). Never raises for HTTP 4xx/5xx."""
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if auth and self.token:
            headers["X-ModelSmith-Token"] = self.token
        try:
            conn.request(method, self.prefix + path, body=data, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            status = int(response.status)
        finally:
            conn.close()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            payload = {"raw": raw.decode("utf-8", errors="replace")}
        if not isinstance(payload, dict):
            payload = {"body": payload}
        return status, payload

    def request_raw(self, method: str, path: str, auth: bool = True,
                    max_bytes: int = None):
        """Return (status, headers_lower, body_bytes); used for binary samples."""
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        headers = {"Accept": "*/*"}
        if auth and self.token:
            headers["X-ModelSmith-Token"] = self.token
        try:
            conn.request(method, self.prefix + path, headers=headers)
            response = conn.getresponse()
            status = int(response.status)
            hdrs = {name.lower(): value for name, value in response.getheaders()}
            data = response.read(max_bytes) if max_bytes is not None else response.read()
        finally:
            conn.close()
        return status, hdrs, data


class ToolError(Exception):
    """Bad arguments supplied by the caller (surfaced as isError, not a crash)."""


class UnknownTool(Exception):
    """tools/call named a tool that is not in the registry (JSON-RPC -32602)."""


# ---------------------------------------------------------------------------
# tool implementations
# ---------------------------------------------------------------------------


def _require_str(args: dict, name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ToolError("%s must be a non-empty string" % name)
    return value.strip()


def _int_arg(args: dict, name: str, default: int, minimum: int, maximum: int) -> int:
    value = args.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError("%s must be an integer" % name)
    return max(minimum, min(maximum, value))


def _float_arg(args: dict, name: str, default: float, minimum: float, maximum: float) -> float:
    value = args.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError("%s must be a number" % name)
    return max(minimum, min(maximum, float(value)))


def _quote(segment: str) -> str:
    return urllib.parse.quote(segment, safe="")


def tool_health(client: ApiClient, args: dict):
    # /health is the only endpoint the core serves without a token
    return client.request("GET", "/health", auth=False)


def tool_env_doctor(client: ApiClient, args: dict):
    refresh = "1" if bool(args.get("refresh")) else "0"
    return client.request("GET", "/env/doctor?refresh=%s" % refresh)


def tool_list_models(client: ApiClient, args: dict):
    query = []
    kind = args.get("kind")
    if kind is not None:
        if not isinstance(kind, str) or kind.strip() not in ("diffusion", "llm", "laya"):
            raise ToolError("kind must be one of diffusion|llm|laya")
        query.append("kind=" + _quote(kind.strip()))
    refresh = args.get("refresh")
    if refresh is not None:
        if not isinstance(refresh, bool):
            raise ToolError("refresh must be a boolean")
        if refresh:
            query.append("refresh=1")
    suffix = ("?" + "&".join(query)) if query else ""
    return client.request("GET", "/api/models" + suffix)


def tool_list_jobs(client: ApiClient, args: dict):
    query = []
    state = args.get("state")
    if state is not None:
        if not isinstance(state, str) or state.strip() not in (
            "queued",
            "starting",
            "running",
        ) + TERMINAL_STATES:
            raise ToolError("state must be one of queued|starting|running|succeeded|failed|cancelled|interrupted")
        query.append("state=" + _quote(state.strip()))
    if args.get("limit") is not None:
        query.append("limit=%d" % _int_arg(args, "limit", 100, 1, 500))
    suffix = ("?" + "&".join(query)) if query else ""
    return client.request("GET", "/api/jobs" + suffix)


def tool_submit_job(client: ApiClient, args: dict):
    kind = _require_str(args, "kind")
    params = args.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ToolError("params must be an object")
    if "train_command" in params:
        # the core hook executes an arbitrary child command; never via MCP
        raise ToolError(TOOL_TRAIN_COMMAND_ERROR)
    body = {"kind": kind, "params": params}
    if args.get("idempotency_key") is not None:
        body["idempotency_key"] = _require_str(args, "idempotency_key")[:128]
    return client.request("POST", "/api/jobs", body)


def tool_get_job(client: ApiClient, args: dict):
    job_id = _require_str(args, "id")
    return client.request("GET", "/api/jobs/" + _quote(job_id))


def tool_job_logs(client: ApiClient, args: dict):
    job_id = _require_str(args, "id")
    offset = _int_arg(args, "offset", 0, 0, 2**31 - 1)
    max_lines = _int_arg(args, "max_lines", 200, 1, 1000)
    return client.request(
        "GET",
        "/api/jobs/%s/logs?offset=%d&max_lines=%d"
        % (_quote(job_id), offset, max_lines),
    )


def tool_cancel_job(client: ApiClient, args: dict):
    job_id = _require_str(args, "id")
    return client.request("POST", "/api/jobs/%s/cancel" % _quote(job_id), {})


def tool_wait_job(client: ApiClient, args: dict):
    job_id = _require_str(args, "id")
    timeout_s = _float_arg(args, "timeout_s", 600.0, 1.0, 7200.0)
    poll_s = _float_arg(args, "poll_s", 1.0, 0.05, 10.0)
    started = time.monotonic()
    last_job = None
    while True:
        status, payload = client.request("GET", "/api/jobs/" + _quote(job_id))
        job = payload.get("job") if isinstance(payload, dict) else None
        if 200 <= status < 300 and isinstance(job, dict):
            last_job = job
            state = job.get("state")
            if state in TERMINAL_STATES:
                return status, {
                    "ok": True,
                    "job_id": job_id,
                    "state": state,
                    "metrics": job.get("metrics"),
                    "waited_s": round(time.monotonic() - started, 1),
                    "timed_out": False,
                    "job": job,
                }
        else:
            return status, payload
        waited = time.monotonic() - started
        if waited >= timeout_s:
            return 200, {
                "ok": True,
                "job_id": job_id,
                "state": (last_job or {}).get("state"),
                "metrics": (last_job or {}).get("metrics"),
                "waited_s": round(waited, 1),
                "timed_out": True,
                "job": last_job,
            }
        time.sleep(min(poll_s, max(0.05, timeout_s - waited)))


def tool_job_metrics_history(client: ApiClient, args: dict):
    job_id = _require_str(args, "id")
    since = _int_arg(args, "since", 0, 0, 2**31 - 1)
    return client.request(
        "GET", "/api/jobs/%s/metrics_history?since=%d" % (_quote(job_id), since)
    )


def tool_eval_job(client: ApiClient, args: dict):
    job_id = _require_str(args, "id")
    body = {}
    if args.get("data") is not None:
        body["data"] = _require_str(args, "data")
    if args.get("limit") is not None:
        body["limit"] = _int_arg(args, "limit", 0, 1, 100000)
    return client.request("POST", "/api/jobs/%s/eval" % _quote(job_id), body)


def tool_report_job(client: ApiClient, args: dict):
    job_id = _require_str(args, "id")
    body = {}
    for key in ("language", "model", "template"):
        if args.get(key) is not None:
            body[key] = _require_str(args, key)
    return client.request("POST", "/api/jobs/%s/report" % _quote(job_id), body)


def tool_star_artifact(client: ApiClient, args: dict):
    job_id = _require_str(args, "id")
    artifact = _require_str(args, "artifact")
    starred = args.get("starred", True)
    if not isinstance(starred, bool):
        raise ToolError("starred must be a boolean")
    return client.request(
        "POST",
        "/api/jobs/%s/star" % _quote(job_id),
        {"artifact": artifact, "starred": starred},
    )


def tool_open_path(client: ApiClient, args: dict):
    path = _require_str(args, "path")
    # dry defaults to True: an Agent must never pop an Explorer window on its own
    dry = args.get("dry", True)
    if not isinstance(dry, bool):
        raise ToolError("dry must be a boolean")
    return client.request(
        "POST", "/api/open?dry=%d" % (1 if dry else 0), {"path": path}
    )


def tool_download_model(client: ApiClient, args: dict):
    """POST download / download-cancel, then poll .../download/status to terminal."""
    model_id = _require_str(args, "model_id")
    cancel = args.get("cancel", False)
    if not isinstance(cancel, bool):
        raise ToolError("cancel must be a boolean")
    if cancel:
        status, payload = client.request(
            "POST", "/api/models/%s/download/cancel" % _quote(model_id), {}
        )
        if status == 404:
            detail = ""
            if isinstance(payload, dict):
                detail = str(
                    payload.get("error")
                    or (payload.get("body") or {}).get("error")
                    or ""
                )
            # R9 core: 404 means "download not found"; older cores: route missing
            route_missing = detail.strip().lower() in ("", "not found")
            note = (
                "core 未提供 /api/models/{id}/download/cancel"
                if route_missing
                else "该 model 当前没有下载任务（核心 404: %s）" % detail
            )
            return 200, {
                "ok": False,
                "degraded": True,
                "note": note,
                "status": 404,
                "body": payload,
            }
        return status, payload
    sources = args.get("sources")
    if sources is not None:
        if (
            not isinstance(sources, list)
            or not sources
            or not all(isinstance(item, str) and item.strip() for item in sources)
        ):
            raise ToolError("sources must be a non-empty list of URL strings")
    timeout_s = _float_arg(args, "timeout_s", 600.0, 1.0, 7200.0)
    poll_s = _float_arg(args, "poll_s", 0.25, 0.05, 5.0)
    body = {}
    if sources is not None:
        body["sources"] = [item.strip() for item in sources]
    status, payload = client.request(
        "POST", "/api/models/%s/download" % _quote(model_id), body
    )
    if not (200 <= status < 300):
        return status, payload
    started = time.monotonic()
    state = payload.get("state") or "running"
    last = payload
    while state not in DOWNLOAD_TERMINAL_STATES:
        waited = time.monotonic() - started
        if waited >= timeout_s:
            return 200, {
                "ok": False,
                "model_id": model_id,
                "state": state,
                "timed_out": True,
                "waited_s": round(waited, 1),
                "status": last,
                "note": "download still running after timeout_s (not cancelled)",
            }
        time.sleep(min(poll_s, max(0.05, timeout_s - waited)))
        st, body_status = client.request(
            "GET", "/api/models/%s/download/status" % _quote(model_id)
        )
        if not (200 <= st < 300):
            return st, body_status
        last = body_status
        state = body_status.get("state") or state
    waited = time.monotonic() - started
    return 200, {
        "ok": state == "succeeded",
        "model_id": model_id,
        "state": state,
        "timed_out": False,
        "waited_s": round(waited, 1),
        "status": last,
        "errors": (last or {}).get("errors") or [],
    }


def tool_job_artifacts(client: ApiClient, args: dict):
    job_id = _require_str(args, "id")
    return client.request("GET", "/api/jobs/%s/artifacts" % _quote(job_id))


def tool_job_sample(client: ApiClient, args: dict):
    """GET /api/jobs/{id}/samples/<rel>; metadata by default, base64 on demand."""
    job_id = _require_str(args, "id")
    rel = _require_str(args, "path").replace("\\", "/").lstrip("/")
    include_b64 = args.get("include_b64", False)
    if not isinstance(include_b64, bool):
        raise ToolError("include_b64 must be a boolean")
    url = "/api/jobs/%s/samples/%s" % (_quote(job_id), urllib.parse.quote(rel, safe="/"))
    probe_limit = (MAX_SAMPLE_B64_BYTES + 1) if include_b64 else 64
    status, headers, probe = client.request_raw("GET", url, max_bytes=probe_limit)
    if not (200 <= status < 300):
        try:
            payload = json.loads(probe.decode("utf-8"))
        except ValueError:
            payload = {"raw": probe.decode("utf-8", errors="replace")[:200]}
        if not isinstance(payload, dict):
            payload = {"body": payload}
        return status, payload
    total = int(headers.get("content-length") or 0)
    if include_b64 and total > MAX_SAMPLE_B64_BYTES:
        return 413, {
            "ok": False,
            "error": "sample too large for b64 (> 2 MiB)",
            "bytes": total,
            "hint": "call without include_b64 to get metadata, then read the file locally",
        }
    if not total:  # no content-length: read it fully
        status, headers, probe = client.request_raw("GET", url)
        total = len(probe)
    result = {
        "ok": True,
        "job_id": job_id,
        "path": rel,
        "bytes": total,
        "content_type": headers.get("content-type") or "application/octet-stream",
    }
    if include_b64:
        result["bytes_b64"] = base64.b64encode(probe).decode("ascii")
    return 200, result


def tool_pick_path(client: ApiClient, args: dict):
    """POST /api/pick — dry by default so an Agent never pops a dialog."""
    mode = args.get("mode", "file")
    if mode not in ("file", "dir"):
        raise ToolError("mode must be file or dir")
    dry = args.get("dry", True)
    if not isinstance(dry, bool):
        raise ToolError("dry must be a boolean")
    if dry:
        return 200, {
            "ok": False,
            "dry": True,
            "mode": mode,
            "note": "dry mode: no dialog opened. Pass dry=false to actually pick "
                    "(safe only when the core runs with --pick-stub, or a desktop session exists).",
        }
    body = {"mode": mode}
    if args.get("title") is not None:
        body["title"] = _require_str(args, "title")
    filters = args.get("filter")
    if filters is not None:
        if isinstance(filters, str):
            filters = [filters]
        if not isinstance(filters, list) or not all(
            isinstance(item, str) and item for item in filters
        ):
            raise ToolError("filter must be a string or a list of strings")
        body["filter"] = filters
    return client.request("POST", "/api/pick", body)


def tool_early_stop(client: ApiClient, args: dict):
    job_id = _require_str(args, "id")
    enabled = args.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ToolError("enabled must be a boolean")
    body = {"enabled": enabled}
    if args.get("patience") is not None:
        body["patience"] = _int_arg(args, "patience", 2, 1, 1000)
    if args.get("min_delta") is not None:
        body["min_delta"] = _float_arg(args, "min_delta", 0.01, 0.0, 1e9)
    return client.request("POST", "/api/jobs/%s/early_stop" % _quote(job_id), body)


# ---------------------------------------------------------------------------
# tool registry (JSON Schema for each tool)
# ---------------------------------------------------------------------------

_JOB_ID_SCHEMA = {"type": "string", "description": "job id, e.g. job-20261022-...", "minLength": 1}

TOOLS = [
    {
        "name": "modelsmith_health",
        "description": "Core liveness: GET /health (no token needed). Returns version/pid/uptime/listener/job counts.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "_impl": tool_health,
    },
    {
        "name": "modelsmith_env_doctor",
        "description": "Environment probe: python/torch/cuda/disk/ports. Use before the first real run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "refresh": {"type": "boolean", "description": "bypass the core's 30s doctor cache (default false)"},
            },
            "additionalProperties": False,
        },
        "_impl": tool_env_doctor,
    },
    {
        "name": "modelsmith_list_models",
        "description": "List locally known base models: GET /api/models. Degrades to {ok:false,note} if the core does not expose that route yet.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["diffusion", "llm", "laya"], "description": "optional filter"},
                "refresh": {"type": "boolean", "description": "force a rescan (core caches the models dir for 60 s)"},
            },
            "additionalProperties": False,
        },
        "_impl": tool_list_models,
    },
    {
        "name": "modelsmith_list_jobs",
        "description": "List jobs newest-first with queue/wait counts: GET /api/jobs. Call this before submitting to check nothing is running.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "enum": ["queued", "starting", "running", "succeeded", "failed", "cancelled", "interrupted"],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            },
            "additionalProperties": False,
        },
        "_impl": tool_list_jobs,
    },
    {
        "name": "modelsmith_submit_job",
        "description": "Submit a training job: POST /api/jobs. params is passed through unchanged (per-kind fields in docs/MCP-TOOLS.md). Rejects params.train_command.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "fake | laya | lf | diffsynth | aitk | llm", "minLength": 1},
                "params": {"type": "object", "description": "per-kind parameter object, passed through as-is"},
                "idempotency_key": {"type": "string", "description": "optional replay-safe key (max 128 chars)"},
            },
            "required": ["kind", "params"],
            "additionalProperties": False,
        },
        "_impl": tool_submit_job,
    },
    {
        "name": "modelsmith_get_job",
        "description": "One job's full record (state, params, metrics, log_size, exit_code, error): GET /api/jobs/{id}.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": _JOB_ID_SCHEMA},
            "required": ["id"],
            "additionalProperties": False,
        },
        "_impl": tool_get_job,
    },
    {
        "name": "modelsmith_job_logs",
        "description": "Tail a job log by byte offset: GET /api/jobs/{id}/logs. Returns lines plus next_offset/size/eof; keep passing next_offset to follow.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": _JOB_ID_SCHEMA,
                "offset": {"type": "integer", "minimum": 0, "default": 0, "description": "byte offset from a previous next_offset"},
                "max_lines": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        "_impl": tool_job_logs,
    },
    {
        "name": "modelsmith_cancel_job",
        "description": "Ask the core to cancel a job: POST /api/jobs/{id}/cancel. Soft stop first, taskkill after the grace period. There is no resume.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": _JOB_ID_SCHEMA},
            "required": ["id"],
            "additionalProperties": False,
        },
        "_impl": tool_cancel_job,
    },
    {
        "name": "modelsmith_wait_job",
        "description": "Poll GET /api/jobs/{id} until a terminal state or timeout_s (default 600, max 7200). Returns {state, metrics, waited_s, timed_out}.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": _JOB_ID_SCHEMA,
                "timeout_s": {"type": "number", "minimum": 1, "maximum": 7200, "default": 600},
                "poll_s": {"type": "number", "minimum": 0.05, "maximum": 10, "default": 1.0},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        "_impl": tool_wait_job,
    },
    {
        "name": "modelsmith_job_metrics_history",
        "description": "Incremental metrics timeline: GET /api/jobs/{id}/metrics_history?since=. Pass the previous next_seq to get only new rows.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": _JOB_ID_SCHEMA,
                "since": {"type": "integer", "minimum": 0, "default": 0, "description": "last seen next_seq"},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        "_impl": tool_job_metrics_history,
    },
    {
        "name": "modelsmith_eval_job",
        "description": "Evaluate a terminal job: POST /api/jobs/{id}/eval. This core returns metrics + per_question:[] + note (no per-row predictions yet); non-terminal jobs -> 409.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": _JOB_ID_SCHEMA,
                "data": {"type": "string", "description": "optional held-out JSONL; omit = calibration slice (in-sample)"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100000, "description": "row cap passed through (this core currently ignores it)"},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        "_impl": tool_eval_job,
    },
    {
        "name": "modelsmith_report_job",
        "description": "Markdown report from the last eval: POST /api/jobs/{id}/report. Requires /eval first; this core renders engine=\"template\" with an in-sample warning.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": _JOB_ID_SCHEMA,
                "language": {"type": "string", "description": "zh (default) | en"},
                "model": {"type": "string", "description": "optional model label echoed in the report"},
                "template": {"type": "string", "description": "optional template hint (this core uses the template engine)"},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        "_impl": tool_report_job,
    },
    {
        "name": "modelsmith_star_artifact",
        "description": "Mark/unmark a candidate artifact: POST /api/jobs/{id}/star. Returns the job's starred_artifacts list.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": _JOB_ID_SCHEMA,
                "artifact": {"type": "string", "description": "artifact name or path, e.g. adapter_model.safetensors", "minLength": 1},
                "starred": {"type": "boolean", "default": True, "description": "true = add, false = remove"},
            },
            "required": ["id", "artifact"],
            "additionalProperties": False,
        },
        "_impl": tool_star_artifact,
    },
    {
        "name": "modelsmith_open_path",
        "description": "Validate (dry) or open a path in Explorer: POST /api/open. dry defaults to true; paths outside the allowed roots are 403.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "absolute path under the project/models roots", "minLength": 1},
                "dry": {"type": "boolean", "default": True, "description": "true = validate only (no window); false = actually open Explorer"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "_impl": tool_open_path,
    },
    {
        "name": "modelsmith_download_model",
        "description": "Start a model download and poll status to terminal: POST /api/models/{id}/download + GET .../download/status. ok:false on failed download or timeout.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model_id": {"type": "string", "description": "id from modelsmith_list_models", "minLength": 1},
                "sources": {"type": "array", "items": {"type": "string"}, "description": "fallback URL list (core tries them in order)"},
                "timeout_s": {"type": "number", "minimum": 1, "maximum": 7200, "default": 600},
                "poll_s": {"type": "number", "minimum": 0.05, "maximum": 5, "default": 0.25},
                "cancel": {"type": "boolean", "default": False, "description": "true = POST .../download/cancel instead of starting a download"},
            },
            "required": ["model_id"],
            "additionalProperties": False,
        },
        "_impl": tool_download_model,
    },
    {
        "name": "modelsmith_job_artifacts",
        "description": "Artifact list for a job: GET /api/jobs/{id}/artifacts (name/path/bytes/sha256 of *.safetensors + samples/*).",
        "inputSchema": {
            "type": "object",
            "properties": {"id": _JOB_ID_SCHEMA},
            "required": ["id"],
            "additionalProperties": False,
        },
        "_impl": tool_job_artifacts,
    },
    {
        "name": "modelsmith_job_sample",
        "description": "One sample file under <out>/samples: metadata by default; include_b64=true returns base64 but refuses >2 MiB.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": _JOB_ID_SCHEMA,
                "path": {"type": "string", "description": "path relative to <out>/samples, e.g. step-100/0.png", "minLength": 1},
                "include_b64": {"type": "boolean", "default": False, "description": "true = also return bytes_b64 (max 2 MiB)"},
            },
            "required": ["id", "path"],
            "additionalProperties": False,
        },
        "_impl": tool_job_sample,
    },
    {
        "name": "modelsmith_pick_path",
        "description": "Native file/dir picker via POST /api/pick. dry defaults to true (no dialog); dry=false actually picks (use only with --pick-stub or a real desktop session).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["file", "dir"], "default": "file"},
                "title": {"type": "string", "description": "dialog title (optional)"},
                "filter": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}], "description": "extension filter, e.g. [\"jsonl\", \"csv\"]"},
                "dry": {"type": "boolean", "default": True, "description": "true = validate only, no dialog"},
            },
            "additionalProperties": False,
        },
        "_impl": tool_pick_path,
    },
    {
        "name": "modelsmith_early_stop",
        "description": "Configure early stopping for a job: POST /api/jobs/{id}/early_stop. Disabled by default; patience>=1, min_delta>=0.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": _JOB_ID_SCHEMA,
                "enabled": {"type": "boolean", "default": False},
                "patience": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 2},
                "min_delta": {"type": "number", "minimum": 0, "default": 0.01},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        "_impl": tool_early_stop,
    },
]

TOOL_MAP = {tool["name"]: tool for tool in TOOLS}


# ---------------------------------------------------------------------------
# JSON-RPC
# ---------------------------------------------------------------------------


def _frame(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _error(msg_id, code: int, message: str, data=None) -> dict:
    frame = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
    if data is not None:
        frame["error"]["data"] = data
    return frame


def _result_payload(payload, is_error: bool) -> dict:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
        "isError": bool(is_error),
    }


def _http_result(tool_name: str, status: int, payload: dict) -> dict:
    if 200 <= status < 300:
        if (
            tool_name in ERROR_ON_OK_FALSE
            and payload.get("ok") is False
            and not payload.get("degraded")
        ):
            return _result_payload(payload, True)
        return _result_payload(payload, False)
    route = DEGRADE_ROUTES.get(tool_name)
    if status == 404 and route:
        # graceful degradation: the core may not have this route yet
        return _result_payload(
            {
                "ok": False,
                "note": "core 未提供 %s" % route,
                "status": 404,
                "body": payload,
            },
            False,
        )
    return _result_payload({"ok": False, "status": status, "body": payload}, True)


def call_tool(client: ApiClient, params: dict) -> dict:
    if not isinstance(params, dict):
        raise ToolError("tools/call params must be an object")
    name = params.get("name")
    if not isinstance(name, str) or name not in TOOL_MAP:
        raise UnknownTool("unknown tool: %r" % (name,))
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ToolError("arguments must be an object for %s" % name)
    tool = TOOL_MAP[name]
    try:
        status, payload = tool["_impl"](client, arguments)
    except ToolError as exc:
        return _result_payload({"ok": False, "error": str(exc), "tool": name}, True)
    except (OSError, http.client.HTTPException) as exc:
        return _result_payload(
            {
                "ok": False,
                "error": "core unreachable: %s: %s" % (type(exc).__name__, exc),
                "tool": name,
                "api": "%s:%s" % (client.host, client.port),
            },
            True,
        )
    return _http_result(name, status, payload)


def handle_message(client: ApiClient, message):
    if not isinstance(message, dict):
        return _error(None, -32600, "invalid request: expected a JSON-RPC object")
    method = message.get("method")
    if method is None:
        return None  # a response from the client; ignore
    msg_id = message.get("id")
    if "id" not in message:
        # notification (e.g. notifications/initialized) -> no response
        return None
    if not isinstance(method, str):
        return _error(msg_id, -32600, "invalid request: method must be a string")
    try:
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": VERSION},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {
                "tools": [
                    {"name": tool["name"], "description": tool["description"],
                     "inputSchema": tool["inputSchema"]}
                    for tool in TOOLS
                ]
            }
        elif method == "tools/call":
            try:
                result = call_tool(client, message.get("params") or {})
            except UnknownTool as exc:
                return _error(msg_id, -32602, str(exc))
        else:
            return _error(msg_id, -32601, "method not found: %s" % method)
    except Exception as exc:  # noqa: BLE001 - never kill the server loop
        log("internal error: %s\n%s" % (exc, traceback.format_exc()))
        return _error(msg_id, -32603, "internal error: %s" % exc)
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def serve(client: ApiClient, stdin=None) -> int:
    stream = stdin if stdin is not None else sys.stdin.buffer
    for raw in stream:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            _frame(_error(None, -32700, "parse error"))
            continue
        if isinstance(message, list):
            if not message:
                _frame(_error(None, -32600, "invalid request: empty batch"))
                continue
            responses = [handle_message(client, item) for item in message]
            responses = [resp for resp in responses if resp is not None]
            _frame(responses if responses else _error(None, -32600, "empty batch"))
            continue
        response = handle_message(client, message)
        if response is not None:
            _frame(response)
    log("stdin closed; exiting")
    return 0


def _force_utf8_stdio() -> None:
    """MCP over stdio is UTF-8 by spec; Windows pipes default to cp936."""
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="modelsmith_mcp.py", description="ModelSmith MCP server (stdio JSON-RPC)")
    parser.add_argument("--api", default=os.environ.get("MODELSMITH_API", "http://127.0.0.1:8765"))
    parser.add_argument("--token", default=os.environ.get("MODELSMITH_TOKEN"))
    parser.add_argument("--token-file", default=None, help="default: %LOCALAPPDATA%\\ModelSmith\\token")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout seconds (default 30)")
    parser.add_argument("--print-tools", action="store_true", help="print the tool registry and exit (debug)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    _force_utf8_stdio()
    args = parse_args(argv)
    if args.print_tools:
        print(json.dumps([{"name": t["name"], "description": t["description"]} for t in TOOLS], ensure_ascii=False, indent=2))
        return 0
    token = args.token or read_token(args.token_file or default_token_path())
    try:
        client = ApiClient(args.api, token, args.timeout)
    except ValueError as exc:
        log("bad --api: %s" % exc)
        return 2
    log("ready api=%s:%s token=%s tools=%d" % (client.host, client.port, "set" if token else "missing", len(TOOLS)))
    return serve(client)


if __name__ == "__main__":
    sys.exit(main())
