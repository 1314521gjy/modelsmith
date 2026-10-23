#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Black-box selftest for `mcp_server\\modelsmith_mcp.py`.

Starts a real core (`--port 0`, temp data dir) and talks JSON-RPC to a real MCP
subprocess over stdin/stdout pipes.  No GPU, no training: the only job kind used
is `fake`.

    python mcp_server\\selftest_mcp.py
    python mcp_server\\selftest_mcp.py --keep

Prints `PASS n / FAIL m`; exit code != 0 means failure. Run it twice and compare
the PASS count (the task's stability requirement).
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
CORE = PROJECT_ROOT / "core" / "modelsmith_core.py"
MCP = HERE / "modelsmith_mcp.py"
IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0

JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
sys.path.insert(0, str(HERE))
import modelsmith_mcp as mcp_module  # noqa: E402  (same module the subprocess runs)
BASE_TOOLS = [
    "modelsmith_health",
    "modelsmith_env_doctor",
    "modelsmith_list_models",
    "modelsmith_list_jobs",
    "modelsmith_submit_job",
    "modelsmith_get_job",
    "modelsmith_job_logs",
    "modelsmith_cancel_job",
    "modelsmith_wait_job",
]
# task-59 (R4 MCP v2): the six endpoint tools the core added after task-50.
V2_TOOLS: list[str] = [
    "modelsmith_job_metrics_history",
    "modelsmith_eval_job",
    "modelsmith_report_job",
    "modelsmith_star_artifact",
    "modelsmith_open_path",
    "modelsmith_download_model",
]
# task-72 (R11 MCP v3): artifacts / samples / pick / early_stop.
V3_TOOLS: list[str] = [
    "modelsmith_job_artifacts",
    "modelsmith_job_sample",
    "modelsmith_pick_path",
    "modelsmith_early_stop",
]
EXPECTED_TOOLS = BASE_TOOLS + V2_TOOLS + V3_TOOLS


class Checker:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures = []

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        if condition:
            self.passed += 1
            print("PASS %02d - %s%s" % (self.passed + self.failed, name, (" :: " + detail) if detail else ""), flush=True)
        else:
            self.failed += 1
            self.failures.append(name)
            print("FAIL %02d - %s%s" % (self.passed + self.failed, name, (" :: " + detail) if detail else ""), flush=True)
        return bool(condition)


# ---------------------------------------------------------------------------
# core lifecycle
# ---------------------------------------------------------------------------


def start_core(data_dir: Path, models_dir: Path = None, pick_stub: str = None):
    log_path = data_dir / "core-stdout.log"
    log_handle = open(log_path, "ab")
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    argv = [
        sys.executable,
        str(CORE),
        "--port",
        "0",
        "--data-dir",
        str(data_dir),
        "--project-root",
        str(PROJECT_ROOT),
    ]
    if models_dir is not None:
        argv += ["--models-dir", str(models_dir)]
    if pick_stub is not None:
        argv += ["--pick-stub", pick_stub]
    proc = subprocess.Popen(
        argv,
        cwd=str(PROJECT_ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=CREATE_NO_WINDOW,
    )
    core_json = data_dir / "core.json"
    deadline = time.time() + 30
    config = None
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("core exited early (rc=%s); see %s" % (proc.returncode, log_path))
        try:
            config = json.loads(core_json.read_text(encoding="utf-8"))
            if int(config.get("pid", -1)) == proc.pid and int(config.get("port", 0)) > 0:
                break
        except (OSError, ValueError):
            pass
        config = None
        time.sleep(0.05)
    if config is None:
        raise RuntimeError("core did not publish core.json within 30s")
    token = (data_dir / "token").read_text(encoding="utf-8").strip()
    return proc, log_handle, log_path, int(config["port"]), token


def stop_core(port: int, token: str, proc: subprocess.Popen, log_handle) -> None:
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/admin/shutdown",
            headers={"X-ModelSmith-Token": token},
        )
        conn.getresponse().read()
        conn.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    try:
        log_handle.close()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# MCP subprocess client
# ---------------------------------------------------------------------------


class McpClient:
    def __init__(self, api: str, token: str, name: str, stderr_path: Path):
        self.name = name
        self.stderr_path = stderr_path
        self.stderr_handle = open(stderr_path, "ab")
        argv = [sys.executable, str(MCP), "--api", api, "--token", token]
        self.proc = subprocess.Popen(
            argv,
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr_handle,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )
        self.frames: queue.Queue = queue.Queue()
        self.bad_frames = 0
        self.read_frames = []
        self._next_id = 0
        self._reader = threading.Thread(target=self._read_loop, name="mcp-reader-%s" % name, daemon=True)
        self._reader.start()

    def _read_loop(self):
        try:
            assert self.proc.stdout is not None
            for line in iter(self.proc.stdout.readline, ""):
                text = line.strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except ValueError:
                    self.bad_frames += 1
                    continue
                self.read_frames.append(message)
                self.frames.put(message)
        finally:
            self.frames.put(None)

    def send(self, method: str, params=None, notification: bool = False):
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notification:
            payload["id"] = self._next_id
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        return None if notification else self._next_id

    def recv(self, timeout: float = 30.0):
        try:
            item = self.frames.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("no JSON-RPC frame within %.1fs" % timeout)
        if item is None:
            raise EOFError("MCP stdout closed")
        return item

    def rpc(self, method: str, params=None, timeout: float = 30.0):
        msg_id = self.send(method, params)
        message = self.recv(timeout)
        if isinstance(message, list):
            raise RuntimeError("unexpected batch response: %r" % (message,))
        if message.get("id") != msg_id:
            raise RuntimeError("response id %r != request id %r" % (message.get("id"), msg_id))
        return message

    def call(self, tool: str, arguments=None, timeout: float = 120.0):
        message = self.rpc("tools/call", {"name": tool, "arguments": arguments or {}}, timeout)
        if "error" in message:
            return {"_jsonrpc_error": message["error"]}
        return message.get("result") or {}

    def close(self) -> int:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            rc = self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                rc = self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                rc = -1
        try:
            self.stderr_handle.close()
        except OSError:
            pass
        return rc

    def stderr_text(self) -> str:
        try:
            return self.stderr_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


def result_text(result: dict) -> str:
    content = result.get("content") or []
    if content and isinstance(content[0], dict):
        return str(content[0].get("text") or "")
    return ""


def result_payload(result: dict) -> dict:
    payload = result.get("structuredContent")
    if isinstance(payload, dict):
        return payload
    try:
        parsed = json.loads(result_text(result))
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        return {}


# ---------------------------------------------------------------------------
# scenario
# ---------------------------------------------------------------------------


class StubHttp:
    """Loopback-only HTTP server: a 146-byte file plus a slow trickle stream."""

    PAYLOAD = b"m" * 146
    SLOW_TOTAL = 20 * 1024 * 1024
    SLOW_CHUNK = 64 * 1024
    SLOW_DELAY = 0.02

    def __init__(self):
        import http.server

        payload = self.PAYLOAD
        slow_total = self.SLOW_TOTAL
        slow_chunk = self.SLOW_CHUNK
        slow_delay = self.SLOW_DELAY

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server API
                if self.path == "/f.bin":
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if self.path == "/slow.bin":
                    self.send_response(200)
                    self.send_header("Content-Length", str(slow_total))
                    self.end_headers()
                    sent = 0
                    try:
                        while sent < slow_total:
                            size = min(slow_chunk, slow_total - sent)
                            self.wfile.write(b"s" * size)
                            self.wfile.flush()
                            sent += size
                            time.sleep(slow_delay)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    return
                self.send_error(404)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, name: str = "f.bin") -> str:
        return "http://127.0.0.1:%d/%s" % (self.port, name)

    def close(self) -> None:
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except OSError:
            pass


def make_model_fixture(models_dir: Path, name: str) -> Path:
    """A directory core.scan_models() classifies as kind="llm"."""
    target = models_dir / name
    target.mkdir(parents=True, exist_ok=True)
    (target / "config.json").write_text("{}", encoding="utf-8")
    (target / "pytorch_model.safetensors").write_bytes(b"stub-weights")
    return target


def make_eval_fixture(out_dir: Path) -> None:
    """train_report.json that POST /api/jobs/{id}/eval consumes."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "train_report.json").write_text(
        json.dumps(
            {
                "n": 3,
                "metrics": {
                    "accuracy": 0.9,
                    "loss": 0.31,
                    "ece": 0.04,
                    "brier_top1": 0.11,
                    "mean_conf": 0.88,
                    "abstain_rate": 0.05,
                },
                "confusion": [],
                "errors": [{"q": "Q1", "answer": "refund", "expected": "refund"}],
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )


def run_v2_checks(client: "McpClient", checker: Checker, data_dir: Path, job_id: str,
                  models_dir: Path) -> None:
    """task-59: the six endpoint tools, on a real core, without GPU."""
    # ---- metrics_history (fake job wrote rows) ---------------------- #
    history = result_payload(client.call("modelsmith_job_metrics_history", {"id": job_id}))
    rows = history.get("rows") or []
    checker.check(
        "metrics_history returns rows + next_seq",
        history.get("ok") is True
        and isinstance(rows, list)
        and len(rows) > 0
        and int(history.get("next_seq") or 0) >= 1,
        "rows=%d next_seq=%s first_seq=%s"
        % (len(rows), history.get("next_seq"), (rows[0] or {}).get("seq") if rows else None),
    )

    # ---- eval -> report on a terminal job with a fixture output dir -- #
    # kind="nope" fails fast (no adapter, no GPU) and keeps params.out,
    # which is what /eval uses to locate train_report.json.
    eval_out = data_dir / "eval-out"
    fake = result_payload(
        client.call("modelsmith_submit_job", {"kind": "nope", "params": {"out": str(eval_out)}})
    )
    eval_job_id = str(fake.get("job_id") or "")
    waited = result_payload(
        client.call(
            "modelsmith_wait_job",
            {"id": eval_job_id, "timeout_s": 30, "poll_s": 0.2},
            timeout=45,
        )
    )
    make_eval_fixture(eval_out)
    evaluated = result_payload(client.call("modelsmith_eval_job", {"id": eval_job_id}))
    checker.check(
        "eval_job returns the b-path (per_question:[], note, calibration)",
        waited.get("state") == "failed"
        and evaluated.get("ok") is True
        and evaluated.get("data_source") == "calibration"
        and evaluated.get("per_question") == []
        and "本次训练未落逐条预测" in str(evaluated.get("note") or "")
        and isinstance(evaluated.get("metrics"), dict),
        "job_state=%s ok=%s data_source=%s n=%s note=%s"
        % (
            waited.get("state"),
            evaluated.get("ok"),
            evaluated.get("data_source"),
            evaluated.get("n"),
            str(evaluated.get("note"))[:40],
        ),
    )
    reported = result_payload(
        client.call("modelsmith_report_job", {"id": eval_job_id, "model": "selftest-model", "language": "zh"})
    )
    markdown = str(reported.get("markdown") or "")
    checker.check(
        "report_job renders engine=template with the in-sample warning",
        reported.get("ok") is True
        and reported.get("engine") == "template"
        and reported.get("model") == "selftest-model"
        and "in-sample ≠ 泛化" in markdown,
        "engine=%s model=%s markdown_bytes=%d"
        % (reported.get("engine"), reported.get("model"), len(markdown.encode("utf-8"))),
    )

    # ---- star ------------------------------------------------------- #
    starred = result_payload(
        client.call("modelsmith_star_artifact", {"id": job_id, "artifact": "adapter_model.safetensors"})
    )
    checker.check(
        "star_artifact records the candidate artifact",
        starred.get("ok") is True
        and "adapter_model.safetensors" in (starred.get("starred_artifacts") or []),
        "starred=%s" % json.dumps(starred.get("starred_artifacts"), ensure_ascii=False),
    )

    # ---- open_path: dry by default, 403 outside roots --------------- #
    opened = result_payload(client.call("modelsmith_open_path", {"path": str(PROJECT_ROOT)}))
    checker.check(
        "open_path defaults to dry=true (no Explorer window)",
        opened.get("ok") is True and opened.get("dry") is True,
        "dry=%s path=%s" % (opened.get("dry"), str(opened.get("path"))[-40:]),
    )
    blocked = client.call("modelsmith_open_path", {"path": "C:\\Windows"})
    blocked_payload = result_payload(blocked)
    checker.check(
        "negative control: open_path C:\\Windows -> 403 -> ok:false",
        blocked.get("isError") is True
        and int(blocked_payload.get("status") or 0) == 403
        and blocked_payload.get("ok") is False,
        "isError=%s status=%s body=%s"
        % (blocked.get("isError"), blocked_payload.get("status"),
           json.dumps(blocked_payload.get("body"), ensure_ascii=False)[:120]),
    )

    # ---- list_models refresh + download ---------------------------- #
    models = result_payload(
        client.call("modelsmith_list_models", {"refresh": True, "kind": "llm"})
    )
    ids = [item.get("id") for item in (models.get("models") or [])]
    checker.check(
        "list_models refresh=true lists the fixture model (kind=llm)",
        models.get("ok") is True and "dummy-llm" in ids,
        "ids=%s" % json.dumps(ids, ensure_ascii=False),
    )
    stub = StubHttp()
    try:
        good = result_payload(
            client.call(
                "modelsmith_download_model",
                {"model_id": "dummy-llm", "sources": [stub.url()], "timeout_s": 30},
                timeout=60,
            )
        )
        target = models_dir / "dummy-llm" / "f.bin"
        checker.check(
            "download_model pulls the local stub and writes 146 B",
            good.get("ok") is True
            and good.get("state") == "succeeded"
            and target.is_file()
            and target.stat().st_size == 146,
            "state=%s bytes=%s waited_s=%s"
            % (
                good.get("state"),
                target.stat().st_size if target.is_file() else None,
                good.get("waited_s"),
            ),
        )
    finally:
        stub.close()
    bad = client.call(
        "modelsmith_download_model",
        {"model_id": "dummy-bad", "sources": ["http://127.0.0.1:1/nope.bin"], "timeout_s": 20},
        timeout=40,
    )
    bad_payload = result_payload(bad)
    checker.check(
        "negative control: bad source -> ok:false, isError, bounded wait",
        bad.get("isError") is True
        and bad_payload.get("ok") is False
        and bad_payload.get("state") == "failed"
        and float(bad_payload.get("waited_s") or 0) < 20.0,
        "isError=%s state=%s waited_s=%s"
        % (bad.get("isError"), bad_payload.get("state"), bad_payload.get("waited_s")),
    )

    # ---- every v2 route keeps the 404 degrade contract -------------- #
    covered = {}
    for name in V2_TOOLS:
        if name in mcp_module.DEGRADE_ROUTES:
            covered[name] = mcp_module._http_result(name, 404, {"ok": False, "error": "not found"})
    checker.check(
        "all 6 v2 tools degrade on 404 (ok:false + note, no isError)",
        len(covered) == len(V2_TOOLS)
        and all(
            item.get("isError") is False
            and (item.get("structuredContent") or {}).get("ok") is False
            and "core 未提供" in str((item.get("structuredContent") or {}).get("note"))
            for item in covered.values()
        ),
        "covered=%s" % json.dumps(sorted(covered), ensure_ascii=False),
    )


def run_v3_checks(client: "McpClient", checker: Checker, data_dir: Path,
                  job_id: str, picked_path: str) -> None:
    """task-72: artifacts / samples / pick / early_stop (+ download cancel)."""
    # ---- fixture job with an output dir: safetensors + samples ------- #
    v3_out = data_dir / "v3-out"
    fixture = result_payload(
        client.call("modelsmith_submit_job", {"kind": "nope", "params": {"out": str(v3_out)}})
    )
    v3_job = str(fixture.get("job_id") or "")
    client.call(
        "modelsmith_wait_job",
        {"id": v3_job, "timeout_s": 30, "poll_s": 0.2},
        timeout=45,
    )
    (v3_out / "samples").mkdir(parents=True, exist_ok=True)
    (v3_out / "model.safetensors").write_bytes(b"stub-weights-0123456789abcdef")
    fig = b"\x89PNG\r\n\x1a\n" + b"x" * 56  # 64 bytes
    (v3_out / "samples" / "fig.png").write_bytes(fig)
    big_bytes = 2 * 1024 * 1024 + 1
    (v3_out / "samples" / "big.bin").write_bytes(b"b" * big_bytes)

    # ---- artifacts --------------------------------------------------- #
    artifacts = result_payload(client.call("modelsmith_job_artifacts", {"id": v3_job}))
    names = [item.get("name") for item in (artifacts.get("artifacts") or [])]
    checker.check(
        "job_artifacts lists safetensors + samples",
        artifacts.get("ok") is True
        and "model.safetensors" in names
        and "samples/fig.png" in names,
        "names=%s" % json.dumps(names[:6], ensure_ascii=False),
    )

    # ---- samples: metadata by default, b64 on demand, 2 MiB cap ------ #
    sample = result_payload(client.call("modelsmith_job_sample", {"id": v3_job, "path": "fig.png"}))
    checker.check(
        "job_sample returns metadata only by default",
        sample.get("ok") is True
        and sample.get("bytes") == 64
        and sample.get("content_type") == "image/png"
        and "bytes_b64" not in sample,
        "bytes=%s content_type=%s has_b64=%s"
        % (sample.get("bytes"), sample.get("content_type"), "bytes_b64" in sample),
    )
    sample_b64 = result_payload(
        client.call("modelsmith_job_sample", {"id": v3_job, "path": "fig.png", "include_b64": True})
    )
    decoded = b""
    try:
        decoded = base64.b64decode(sample_b64.get("bytes_b64") or "")
    except (ValueError, TypeError):
        decoded = b""
    checker.check(
        "job_sample include_b64 round-trips the 64 bytes",
        sample_b64.get("ok") is True and decoded == fig,
        "b64_chars=%d decoded=%d" % (len(str(sample_b64.get("bytes_b64") or "")), len(decoded)),
    )
    big_res = client.call(
        "modelsmith_job_sample", {"id": v3_job, "path": "big.bin", "include_b64": True}
    )
    big_payload = result_payload(big_res)
    checker.check(
        "negative cap: job_sample refuses >2 MiB base64 (413)",
        big_res.get("isError") is True
        and big_payload.get("ok") is False
        and int(big_payload.get("status") or 0) == 413,
        "isError=%s status=%s body=%s"
        % (
            big_res.get("isError"),
            big_payload.get("status"),
            json.dumps(big_payload.get("body"), ensure_ascii=False)[:120],
        ),
    )

    # ---- pick_path: dry by default, real path only when asked -------- #
    started = time.perf_counter()
    pick_dry = result_payload(client.call("modelsmith_pick_path", {"mode": "file"}))
    dry_elapsed = time.perf_counter() - started
    checker.check(
        "pick_path dry default opens no dialog",
        pick_dry.get("ok") is False and pick_dry.get("dry") is True and dry_elapsed < 2.0,
        "ok=%s dry=%s elapsed=%.2fs" % (pick_dry.get("ok"), pick_dry.get("dry"), dry_elapsed),
    )
    pick_live = result_payload(
        client.call("modelsmith_pick_path", {"mode": "file", "dry": False}, timeout=30)
    )
    checker.check(
        "pick_path dry=false reaches the core --pick-stub",
        pick_live.get("ok") is True and pick_live.get("path") == picked_path,
        "ok=%s path=%s" % (pick_live.get("ok"), pick_live.get("path")),
    )

    # ---- early_stop: enable then disable ----------------------------- #
    es_on = result_payload(
        client.call(
            "modelsmith_early_stop",
            {"id": job_id, "enabled": True, "patience": 2, "min_delta": 0.01},
        )
    )
    es_off = result_payload(
        client.call("modelsmith_early_stop", {"id": job_id, "enabled": False})
    )
    checker.check(
        "early_stop toggles enabled and keeps the config",
        es_on.get("ok") is True
        and (es_on.get("early_stop") or {}).get("enabled") is True
        and (es_on.get("early_stop") or {}).get("patience") == 2
        and es_off.get("ok") is True
        and (es_off.get("early_stop") or {}).get("enabled") is False,
        "on=%s off=%s"
        % (
            json.dumps(es_on.get("early_stop"), ensure_ascii=False),
            json.dumps(es_off.get("early_stop"), ensure_ascii=False),
        ),
    )

    # ---- download cancel: real on R9 cores, degrade on older ones ----- #
    # (a) start a slow download and let the tool poll-timeout in bounded time
    cancel_stub = StubHttp()
    try:
        slow = result_payload(
            client.call(
                "modelsmith_download_model",
                {
                    "model_id": "dummy-slow",
                    "sources": [cancel_stub.url("slow.bin")],
                    "timeout_s": 1,
                    "poll_s": 0.2,
                },
                timeout=60,
            )
        )
        checker.check(
            "slow download reaches poll timeout bounded (ok:false, state=running)",
            slow.get("ok") is False
            and slow.get("timed_out") is True
            and slow.get("state") == "running"
            and float(slow.get("waited_s") or 0) < 20.0,
            "state=%s timed_out=%s waited_s=%s"
            % (slow.get("state"), slow.get("timed_out"), slow.get("waited_s")),
        )

        # (b) dual-state assertion: 200+cancelled (R9) OR 404+note (older core)
        cancel = client.call(
            "modelsmith_download_model", {"model_id": "dummy-slow", "cancel": True}, timeout=30
        )
        cancel_payload = result_payload(cancel)
        real = cancel.get("isError") is False and cancel_payload.get("ok") is True
        degraded = (
            cancel.get("isError") is False
            and cancel_payload.get("ok") is False
            and "core 未提供" in str(cancel_payload.get("note"))
        )
        cancel_path = "real" if real else ("degrade" if degraded else "error")
        checker.check(
            "download cancel: 200+cancelled (real) or 404+note (degrade) — both PASS",
            (real and str(cancel_payload.get("state")) == "cancelled")
            or (degraded and int(cancel_payload.get("status") or 0) == 404),
            "path=%s state=%s note=%s"
            % (cancel_path, cancel_payload.get("state"), str(cancel_payload.get("note"))[:70]),
        )

        # (c) idempotent second cancel: same answer, bounded
        t0 = time.perf_counter()
        again = client.call(
            "modelsmith_download_model", {"model_id": "dummy-slow", "cancel": True}, timeout=30
        )
        again_elapsed = time.perf_counter() - t0
        again_payload = result_payload(again)
        again_ok = (
            again.get("isError") is False
            and (
                (again_payload.get("ok") is True
                 and str(again_payload.get("state")) == "cancelled")
                or "core 未提供" in str(again_payload.get("note"))
            )
            and again_elapsed < 10.0
        )
        checker.check(
            "download cancel is idempotent and bounded",
            again_ok,
            "state=%s elapsed=%.2fs note=%s"
            % (again_payload.get("state"), again_elapsed, str(again_payload.get("note"))[:60]),
        )

        # (d) model that never started a download -> 404 -> ok:false + note
        never = client.call(
            "modelsmith_download_model", {"model_id": "dummy-never", "cancel": True}, timeout=30
        )
        never_payload = result_payload(never)
        checker.check(
            "download cancel on a never-started model -> ok:false + note (404)",
            never.get("isError") is False
            and never_payload.get("ok") is False
            and int(never_payload.get("status") or 0) == 404
            and bool(never_payload.get("note")),
            "status=%s note=%s"
            % (never_payload.get("status"), str(never_payload.get("note"))[:70]),
        )
    finally:
        cancel_stub.close()

    # ---- every v3 route keeps the 404 degrade contract --------------- #
    covered = {name: mcp_module._http_result(name, 404, {"ok": False, "error": "not found"})
               for name in V3_TOOLS}
    checker.check(
        "all 4 v3 tools degrade on 404 (ok:false + note, no isError)",
        len(covered) == len(V3_TOOLS)
        and all(
            item.get("isError") is False
            and (item.get("structuredContent") or {}).get("ok") is False
            and "core 未提供" in str((item.get("structuredContent") or {}).get("note"))
            for item in covered.values()
        ),
        "covered=%s" % json.dumps(sorted(covered), ensure_ascii=False),
    )


def run_scenario(checker: Checker, data_dir: Path) -> None:
    models_dir = data_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    make_model_fixture(models_dir, "dummy-llm")
    make_model_fixture(models_dir, "dummy-bad")
    make_model_fixture(models_dir, "dummy-slow")
    make_model_fixture(models_dir, "dummy-never")
    picked_path = str(data_dir / "picked-fixture.jsonl")
    core_proc, core_log, core_log_path, port, token = start_core(
        data_dir, models_dir=models_dir, pick_stub=picked_path
    )
    api = "http://127.0.0.1:%d" % port
    client = McpClient(api, token, "main", data_dir / "mcp-main.stderr.log")
    try:
        # ---- handshake -------------------------------------------------
        init = client.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "selftest", "version": "0"}})
        info = (init.get("result") or {}).get("serverInfo") or {}
        checker.check(
            "initialize returns the 2025-06-18 handshake",
            (init.get("result") or {}).get("protocolVersion") == "2025-06-18"
            and isinstance((init.get("result") or {}).get("capabilities"), dict)
            and isinstance(((init.get("result") or {}).get("capabilities") or {}).get("tools"), dict)
            and info.get("name") == "modelsmith"
            and bool(info.get("version")),
            "protocol=%s serverInfo=%s" % ((init.get("result") or {}).get("protocolVersion"), json.dumps(info, ensure_ascii=False)),
        )
        # notification must not produce a response: the next frame is the ping
        client.send("notifications/initialized", notification=True)
        ping = client.rpc("ping")
        checker.check("notifications/initialized is silent and ping -> {}", ping.get("result") == {}, "result=%r" % (ping.get("result"),))

        # ---- tools/list ------------------------------------------------
        listed = client.rpc("tools/list")
        tools = (listed.get("result") or {}).get("tools") or []
        names = [t.get("name") for t in tools]
        schemas_ok = all(isinstance(t.get("inputSchema"), dict) and t["inputSchema"].get("type") == "object" for t in tools)
        checker.check(
            "tools/list returns all expected tools with object schemas",
            len(tools) == len(EXPECTED_TOOLS)
            and sorted(names) == sorted(EXPECTED_TOOLS)
            and schemas_ok,
            "count=%d expected=%d names=%s schemas_ok=%s"
            % (len(tools), len(EXPECTED_TOOLS), json.dumps(names, ensure_ascii=False), schemas_ok),
        )
        unknown = client.rpc("tools/call", {"name": "modelsmith_nope", "arguments": {}})
        checker.check(
            "unknown tool -> JSON-RPC -32602",
            isinstance(unknown.get("error"), dict) and unknown["error"].get("code") == -32602,
            "error=%s" % json.dumps(unknown.get("error"), ensure_ascii=False),
        )

        # ---- health / doctor / models / jobs --------------------------
        health = result_payload(client.call("modelsmith_health"))
        checker.check(
            "modelsmith_health -> ok with version/pid",
            health.get("ok") is True and bool(health.get("version")) and int(health.get("pid") or 0) == core_proc.pid,
            "body=%s" % json.dumps(health, ensure_ascii=False)[:220],
        )
        doctor = client.call("modelsmith_env_doctor")
        doctor_payload = result_payload(doctor)
        checker.check(
            "modelsmith_env_doctor -> python/torch/cuda fields",
            doctor_payload.get("ok") is True and "python" in doctor_payload and "torch" in doctor_payload,
            "keys=%s torch=%s cuda=%s" % (
                sorted(doctor_payload.keys()),
                (doctor_payload.get("torch") or {}).get("version") if isinstance(doctor_payload.get("torch"), dict) else doctor_payload.get("torch"),
                (doctor_payload.get("cuda") or {}).get("status") if isinstance(doctor_payload.get("cuda"), dict) else doctor_payload.get("cuda"),
            ),
        )
        models = client.call("modelsmith_list_models")
        models_payload = result_payload(models)
        models_note = str(models_payload.get("note") or "")
        checker.check(
            "modelsmith_list_models degrades without raising (404 -> ok:false + note)",
            models.get("isError") is False
            and (
                models_payload.get("ok") is True
                or ("未提供 /api/models" in models_note and models_payload.get("ok") is False)
            ),
            "isError=%s ok=%s note=%s" % (models.get("isError"), models_payload.get("ok"), models_note),
        )
        # the degrade branch must stay correct even when the live core *does*
        # serve /api/models: exercise the same function the tool uses.
        degrade = mcp_module._http_result(
            "modelsmith_list_models", 404, {"ok": False, "error": "not found"}
        )
        degrade_payload = degrade.get("structuredContent") or {}
        checker.check(
            "list_models 404 -> {ok:false, note} without isError/raise",
            degrade.get("isError") is False
            and degrade_payload.get("ok") is False
            and "core 未提供 /api/models" in str(degrade_payload.get("note")),
            "isError=%s payload=%s" % (degrade.get("isError"), json.dumps(degrade_payload, ensure_ascii=False)),
        )
        hard_fail = mcp_module._http_result("modelsmith_health", 500, {"ok": False})
        hard_payload = hard_fail.get("structuredContent") or {}
        checker.check(
            "other non-2xx stays a hard isError with status/body",
            hard_fail.get("isError") is True
            and hard_payload.get("ok") is False
            and int(hard_payload.get("status") or 0) == 500,
            "isError=%s status=%s" % (hard_fail.get("isError"), hard_payload.get("status")),
        )
        jobs_before = result_payload(client.call("modelsmith_list_jobs"))
        checker.check(
            "modelsmith_list_jobs -> ok + jobs list",
            jobs_before.get("ok") is True and isinstance(jobs_before.get("jobs"), list),
            "counts=%s" % json.dumps(jobs_before.get("counts"), ensure_ascii=False),
        )

        # ---- submit / get / logs / cancel / wait ----------------------
        submitted = client.call(
            "modelsmith_submit_job",
            {"kind": "fake", "params": {"flow": "decision", "steps": 20, "interval_s": 0.5}},
        )
        submitted_payload = result_payload(submitted)
        job_id = str(submitted_payload.get("job_id") or "")
        checker.check(
            "modelsmith_submit_job {kind:fake} -> job_id",
            submitted.get("isError") is False and bool(JOB_ID_RE.match(job_id)) and submitted_payload.get("ok") is True,
            "state=%s job_id=%s" % (submitted_payload.get("state"), job_id),
        )
        got = result_payload(client.call("modelsmith_get_job", {"id": job_id}))
        job = got.get("job") or {}
        job_params = job.get("params") or {}
        checker.check(
            "modelsmith_get_job -> same id and params passed through",
            got.get("ok") is True
            and job.get("id") == job_id
            and job.get("state") in ("queued", "starting", "running")
            # the core normalizes kind=fake params to steps/interval_s/; flow is
            # dropped there by design, steps proves MCP did not filter anything
            and job_params.get("steps") == 20,
            "state=%s params=%s" % (job.get("state"), json.dumps(job_params, ensure_ascii=False)),
        )
        # wait for the first log bytes, then tail twice by offset
        first = None
        deadline = time.time() + 15
        while time.time() < deadline:
            first = result_payload(client.call("modelsmith_job_logs", {"id": job_id, "offset": 0, "max_lines": 50}))
            if first.get("size", 0) > 0 and first.get("lines"):
                break
            time.sleep(0.2)
        first_offset = int((first or {}).get("next_offset") or 0)
        time.sleep(1.2)
        second = result_payload(client.call("modelsmith_job_logs", {"id": job_id, "offset": first_offset, "max_lines": 50}))
        second_offset = int(second.get("next_offset") or 0)
        checker.check(
            "modelsmith_job_logs offset increases and new lines arrive",
            first_offset > 0 and second_offset > first_offset and len(second.get("lines") or []) > 0,
            "offset1=%d offset2=%d new_lines=%d size=%s eof=%s" % (
                first_offset, second_offset, len(second.get("lines") or []), second.get("size"), second.get("eof")),
        )
        # wait_job timeout path on the still-running job
        short_wait = result_payload(client.call("modelsmith_wait_job", {"id": job_id, "timeout_s": 1, "poll_s": 0.2}))
        checker.check(
            "modelsmith_wait_job honours timeout_s -> timed_out true",
            short_wait.get("ok") is True and short_wait.get("timed_out") is True
            and short_wait.get("state") in ("queued", "starting", "running"),
            "state=%s timed_out=%s waited_s=%s" % (short_wait.get("state"), short_wait.get("timed_out"), short_wait.get("waited_s")),
        )
        cancelled = result_payload(client.call("modelsmith_cancel_job", {"id": job_id}))
        final_wait = result_payload(client.call("modelsmith_wait_job", {"id": job_id, "timeout_s": 60, "poll_s": 0.2}, timeout=90))
        final_metrics = final_wait.get("metrics") or {}
        checker.check(
            "cancel + wait_job reaches cancelled",
            cancelled.get("ok") is True and cancelled.get("cancel_requested") is True
            and final_wait.get("ok") is True and final_wait.get("state") == "cancelled"
            and final_wait.get("timed_out") is False
            and (final_metrics.get("state") == "cancelled"),
            "cancel=%s state=%s timed_out=%s metrics_state=%s" % (
                cancelled.get("state"), final_wait.get("state"), final_wait.get("timed_out"), final_metrics.get("state")),
        )

        # ---- negative control 1: train_command is refused -------------
        before_count = len((result_payload(client.call("modelsmith_list_jobs", {"limit": 500}))).get("jobs") or [])
        refused = client.call(
            "modelsmith_submit_job",
            {"kind": "fake", "params": {"flow": "decision", "train_command": [sys.executable, "-c", "print(1)"]}},
        )
        refused_payload = result_payload(refused)
        after_count = len((result_payload(client.call("modelsmith_list_jobs", {"limit": 500}))).get("jobs") or [])
        checker.check(
            "negative control: submit_job rejects params.train_command",
            refused.get("isError") is True
            and "train_command is a test hook, not allowed via MCP" in (refused_payload.get("error") or "")
            and after_count == before_count,
            "isError=%s error=%s jobs_before=%d jobs_after=%d" % (
                refused.get("isError"), refused_payload.get("error"), before_count, after_count),
        )

        # ---- negative control 2: wrong token -> ok:false, no crash ----
        bad_client = McpClient(api, "wrong-token-0000", "badtoken", data_dir / "mcp-badtoken.stderr.log")
        try:
            bad_init = bad_client.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "selftest", "version": "0"}})
            unauth = bad_client.call("modelsmith_list_jobs", {"limit": 5})
            unauth_payload = result_payload(unauth)
            ping_after = bad_client.rpc("ping")
            checker.check(
                "negative control: wrong token -> isError/ok:false and the server survives",
                isinstance(bad_init.get("result"), dict)
                and unauth.get("isError") is True
                and unauth_payload.get("ok") is False
                and int(unauth_payload.get("status") or 0) == 401
                and ping_after.get("result") == {},
                "isError=%s status=%s body_ok=%s ping_after=%s" % (
                    unauth.get("isError"), unauth_payload.get("status"),
                    (unauth_payload.get("body") or {}).get("ok"), ping_after.get("result")),
            )
        finally:
            bad_rc = bad_client.close()
        checker.check("wrong-token MCP exits 0 on stdin EOF", bad_rc == 0, "rc=%s" % bad_rc)

        # ---- task-59 / task-72 endpoint tools (no GPU) -----------------
        run_v2_checks(client, checker, data_dir, job_id, models_dir)
        run_v3_checks(client, checker, data_dir, job_id, picked_path)

        # ---- token hygiene --------------------------------------------
        main_stderr = client.stderr_text()
        bad_stderr = (data_dir / "mcp-badtoken.stderr.log").read_text(encoding="utf-8", errors="replace") if (data_dir / "mcp-badtoken.stderr.log").is_file() else ""
        stdout_blob = json.dumps(client.read_frames, ensure_ascii=False)
        checker.check(
            "token never appears in stdout frames or stderr logs",
            token not in stdout_blob and token not in main_stderr and token not in bad_stderr,
            "stdout_frames=%d stderr_bytes=%d bad_stderr_bytes=%d" % (
                len(client.read_frames), len(main_stderr.encode("utf-8")), len(bad_stderr.encode("utf-8"))),
        )
        checker.check(
            "stdout carries only JSON-RPC frames (no log lines)",
            client.bad_frames == 0 and len(client.read_frames) > 0,
            "non_json_lines=%d frames=%d" % (client.bad_frames, len(client.read_frames)),
        )

        # ---- stdin EOF -------------------------------------------------
        rc = client.close()
        checker.check("MCP exits 0 on stdin EOF", rc == 0, "rc=%s" % rc)
        client = None
        time.sleep(0.2)
    finally:
        if client is not None:
            client.close()
        stop_core(port, token, core_proc, core_log)
        core_stderr = ""
        try:
            core_stderr = core_log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        checker.check(
            "core shut down cleanly and its log has no traceback",
            "Traceback" not in core_stderr,
            "core_log_bytes=%d" % len(core_stderr.encode("utf-8")),
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ModelSmith MCP black-box selftest")
    parser.add_argument("--keep", action="store_true", help="keep the temp data dir")
    args = parser.parse_args(argv)
    if not CORE.is_file():
        print("FAIL - core missing: %s" % CORE)
        return 2
    if not MCP.is_file():
        print("FAIL - mcp server missing: %s" % MCP)
        return 2
    temp_root = Path(tempfile.mkdtemp(prefix="modelsmith_mcp_selftest_"))
    data_dir = temp_root / "中文 状态"
    data_dir.mkdir(parents=True, exist_ok=True)
    checker = Checker()
    try:
        run_scenario(checker, data_dir)
    except Exception:
        checker.check("mcp selftest completed without crashing", False, traceback.format_exc())
    finally:
        print("\nRESULT: PASS %d / FAIL %d" % (checker.passed, checker.failed))
        if checker.failed:
            print("FAILED CHECKS: " + ", ".join(checker.failures))
            print("data dir kept for debugging: %s" % data_dir)
            return 1
        if args.keep:
            print("data dir kept: %s" % data_dir)
        else:
            shutil.rmtree(temp_root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
