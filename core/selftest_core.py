#!/usr/bin/env python3
r"""Black-box selftest for the ModelSmith core service MVP.

Usage:
    python core\selftest_core.py
    python core\selftest_core.py --keep     # keep the temporary state dir
    python core\selftest_core.py --verbose

What it does (all through public HTTP + read-only SQLite inspection):
  1. start the core on a random port (--port 0) in a temporary data dir
  2. GET /health, GET / (frozen UI baseline + in-memory token bootstrap),
     auth failures, /env/doctor
  3. prove the listener is bound to 127.0.0.1 only (netstat + 127.0.0.2 probe)
  4. submit a fake job, tail its log by byte offset, wait for succeeded
  5. submit two jobs at once, prove only one is running, cancel the long one,
     prove the queued one then succeeds and the worker pid is gone
  6. prove WAL mode, idempotency, unsupported-kind honesty
  7. insert a stale running row, restart, prove it becomes interrupted
  8. print PASS/FAIL counts; exit code != 0 means failure

Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import base64
import functools
import hashlib
import http.client
import http.server
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import struct
import sys
import tempfile
import threading
import time
import traceback
import uuid
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORE = HERE / "modelsmith_core.py"
PROJECT_ROOT = HERE.parent
UI_BASELINE = PROJECT_ROOT / "design" / "prototype" / "index.html"
V3_DIR = PROJECT_ROOT / "design" / "prototype" / "v3"
V3_INDEX = V3_DIR / "index.html"
WIRE_JS = PROJECT_ROOT / "core" / "assets" / "ms-wire.js"
LAYA_STUB = PROJECT_ROOT / "adapters" / "_tmp" / "laya_stub.py"
LLM_STUB = PROJECT_ROOT / "adapters" / "_tmp" / "llm_stub.py"
AITK_STUB = PROJECT_ROOT / "adapters" / "_tmp" / "aitk_stub.py"
DIFFSYNTH_STUB = PROJECT_ROOT / "adapters" / "_tmp" / "diffsynth_stub.py"
LF_STUB = PROJECT_ROOT / "adapters" / "_tmp" / "lf_stub.py"

sys.path.insert(0, str(HERE))
sys.dont_write_bytecode = True
import modelsmith_core as core  # noqa: E402


IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0


# ---------------------------------------------------------------------------
# check harness
# ---------------------------------------------------------------------------


class Checker:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.records = []

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        if condition:
            self.passed += 1
            print("PASS %02d - %s%s" % (self.passed + self.failed, name, (" :: " + detail) if detail else ""))
        else:
            self.failed += 1
            print("FAIL %02d - %s%s" % (self.passed + self.failed, name, (" :: " + detail) if detail else ""))
            self.records.append(name)
        return bool(condition)

    def note(self, text: str) -> None:
        print("NOTE - " + text)


# ---------------------------------------------------------------------------
# HTTP helpers (one fresh connection per call; no third-party deps)
# ---------------------------------------------------------------------------


def http_json(
    port: int,
    method: str,
    path: str,
    body=None,
    token=None,
    timeout: float = 20.0,
    raw: bool = False,
):
    conn = None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=timeout)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if token:
            headers["X-ModelSmith-Token"] = token
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        raw_body = response.read()
        response_headers = {key: value for key, value in response.getheaders()}
        if raw:
            return response.status, response_headers, raw_body
        try:
            decoded = json.loads(raw_body.decode("utf-8")) if raw_body else None
        except (UnicodeDecodeError, ValueError):
            decoded = None
        return response.status, response_headers, decoded
    except (OSError, http.client.HTTPException):
        return 0, {}, None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def get_job(port: int, token: str, job_id: str):
    status, _, body = http_json(port, "GET", "/api/jobs/%s" % job_id, token=token)
    if status != 200 or not isinstance(body, dict):
        return None
    return body.get("job")


def wait_job(port: int, token: str, job_id: str, states, timeout: float = 30.0):
    states = set(states)
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        job = get_job(port, token, job_id)
        if job is not None:
            last = job
            if job.get("state") in states:
                return job
        time.sleep(0.1)
    return last


def submit(port: int, token: str, payload: dict):
    return http_json(port, "POST", "/api/jobs", payload, token=token)


# ---------------------------------------------------------------------------
# server lifecycle
# ---------------------------------------------------------------------------


def start_server(data_dir: Path, log_name: str, extra_args=None):
    log_path = data_dir / log_name
    log_handle = open(log_path, "ab")
    child_env = os.environ.copy()
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
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
    if extra_args:
        argv.extend(str(part) for part in extra_args)
    proc = subprocess.Popen(
        argv,
        cwd=str(PROJECT_ROOT),
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=CREATE_NO_WINDOW,
    )
    return proc, log_handle, log_path


def wait_core_json(data_dir: Path, proc: subprocess.Popen, old_pid=None, timeout: float = 30.0):
    path = data_dir / "core.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                "core exited during startup rc=%s (see data_dir logs)" % proc.returncode
            )
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            time.sleep(0.05)
            continue
        if old_pid is not None and config.get("pid") == old_pid:
            time.sleep(0.05)
            continue
        return config
    raise TimeoutError("core.json did not appear within %.1fs" % timeout)


def stop_server(port, token, proc, log_handle):
    if proc is None:
        return
    if proc.poll() is None:
        http_json(port, "POST", "/admin/shutdown", token=token, timeout=5)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    if log_handle is not None:
        try:
            log_handle.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# netstat / sockets
# ---------------------------------------------------------------------------


def netstat_listeners(port: int):
    try:
        completed = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    text = completed.stdout.decode("utf-8", errors="replace")
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        local, state, pid = parts[1], parts[3], parts[4]
        if state.upper() != "LISTENING":
            continue
        if local.rsplit(":", 1)[-1] != str(port):
            continue
        rows.append({"local": local, "pid": pid, "raw": line.strip()})
    return rows


def can_connect(host: str, port: int, timeout: float = 1.2) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, int(port))) == 0
    except OSError:
        return False
    finally:
        sock.close()


def lan_ipv4_addresses():
    addresses = set()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = item[4][0]
            if not address.startswith("127."):
                addresses.add(address)
    except OSError:
        pass
    return sorted(addresses)


# ---------------------------------------------------------------------------
# sqlite inspection
# ---------------------------------------------------------------------------


def db_connect(data_dir: Path):
    connection = sqlite3.connect(str(data_dir / "core.db"), timeout=15)
    connection.row_factory = sqlite3.Row
    return connection


def db_count(data_dir: Path, sql: str, args=()):
    connection = db_connect(data_dir)
    try:
        return int(connection.execute(sql, args).fetchone()[0])
    finally:
        connection.close()


def find_dead_pid() -> int:
    candidate = 999999
    while candidate > 100:
        if not core.is_pid_alive(candidate):
            return candidate
        candidate -= 1
    return 987654


# ---------------------------------------------------------------------------
# main test body
# ---------------------------------------------------------------------------


def test_orphan_reaper(checker: Checker):
    temp_root = Path(tempfile.mkdtemp(prefix="modelsmith_orphan_selftest_"))
    app = None
    fake = negative = None
    try:
        app = core.CoreApp(temp_root, PROJECT_ROOT, 0)
        fake = subprocess.Popen(
            [sys.executable, "-c", "import time; # modelsmith_orphan\ntime.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        negative = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        fake_row, _ = app.store.create_or_get("fake", {"steps": 1, "interval_s": 0.01})
        neg_row, _ = app.store.create_or_get("fake", {"steps": 1, "interval_s": 0.01})
        app.store.update(fake_row["id"], pid=fake.pid, state="running")
        app.store.update(neg_row["id"], pid=negative.pid, state="running")
        for job_id, pid in ((fake_row["id"], fake.pid), (neg_row["id"], negative.pid)):
            job_dir = app.jobs_dir / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / "pid").write_text(str(pid), encoding="utf-8")
        app.reap_orphans()
        fake.wait(timeout=10)
        checker.check(
            "orphan reaper kills marker-matching worker and records orphan_reaped",
            fake.poll() is not None
            and "orphan_reaped" in (app.store.get(fake_row["id"]).get("error") or ""),
            "fake_rc=%s row_error=%s"
            % (fake.returncode, app.store.get(fake_row["id"]).get("error")),
        )
        checker.check(
            "orphan reaper does not kill unrelated pid",
            negative.poll() is None,
            "negative_alive=%s" % (negative.poll() is None),
        )
    finally:
        for proc in (fake, negative):
            if proc is not None and proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        if app is not None:
            app.store.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def run_phase1(data_dir: Path, checker: Checker):
    print("selftest data_dir=%s" % data_dir)
    token_file = data_dir / "token"
    proc = None
    log_handle = None
    try:
        aitk_libs_sentinel = data_dir / "aitk-libs-SENTINEL"
        fake_models_root = data_dir / "fake-models"
        if fake_models_root.exists():
            shutil.rmtree(fake_models_root)
        (fake_models_root / "diffusers-a").mkdir(parents=True)
        (fake_models_root / "diffusers-a" / "model_index.json").write_text(
            "{}\n", encoding="utf-8"
        )
        (fake_models_root / "llm-a").mkdir()
        (fake_models_root / "llm-a" / "config.json").write_text(
            "{}\n", encoding="utf-8"
        )
        (fake_models_root / "llm-a" / "model.safetensors").write_bytes(b"stub")
        (fake_models_root / "laya-a").mkdir()
        (fake_models_root / "laya-a" / "rl_agent_config.json").write_text(
            "{}\n", encoding="utf-8"
        )
        (fake_models_root / "unknown-a").mkdir()
        (fake_models_root / "single-a").mkdir()
        (fake_models_root / "single-a" / "model.safetensors").write_bytes(b"stub")
        bad_index = {
            "_class_name": "QwenImagePipeline",
            "transformer": ["transformer"],
            "text_encoder": ["text_encoder"],
            "vae": ["vae"],
            "tokenizer": ["tokenizer"],
            "scheduler": ["scheduler"],
            "processor": ["processor"],
        }
        (fake_models_root / "diffusers-bad").mkdir()
        (fake_models_root / "diffusers-bad" / "model_index.json").write_text(
            json.dumps(bad_index), encoding="utf-8"
        )
        (fake_models_root / "diffusers-bad" / "transformer").mkdir()
        (fake_models_root / "diffusers-full").mkdir()
        (fake_models_root / "diffusers-full" / "model_index.json").write_text(
            json.dumps(bad_index), encoding="utf-8"
        )
        for component in ("transformer", "text_encoder", "vae", "tokenizer", "scheduler", "processor"):
            component_dir = fake_models_root / "diffusers-full" / component
            component_dir.mkdir()
            if component in ("transformer", "text_encoder", "vae"):
                (component_dir / "model.safetensors").write_bytes(b"stub")
        fake_assets_dir = data_dir / "assets-sentinel"
        fake_assets_dir.mkdir(parents=True, exist_ok=True)
        fake_pick_path = data_dir / "picked.jsonl"
        fake_eval_stub = data_dir / "eval_stub.py"
        caption_stub = data_dir / "caption_stub_server.py"
        caption_stub.write_text(
            '''import argparse, json, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
p = argparse.ArgumentParser()
p.add_argument("--port", type=int, required=True)
p.add_argument("--delay", type=float, default=0.05)
p.add_argument("--crash", action="store_true")
args = p.parse_args()
if args.crash:
    print("boom", flush=True)
    raise SystemExit(3)
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        body = b'{"ok":true}'
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        time.sleep(args.delay)
        body = json.dumps({"choices":[{"message":{"content":"客户在问退款，先确认订单号。"},"finish_reason":"stop"}]}, ensure_ascii=False).encode("utf-8")
        self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
ThreadingHTTPServer(("127.0.0.1", args.port), H).serve_forever()
''',
            encoding="utf-8",
        )
        fake_diffsynth_libs = data_dir / "fake-pylibs-5"
        fake_diffsynth_libs.mkdir(parents=True, exist_ok=True)
        fake_eval_stub.write_text(
            r'''import argparse, json, sys
p = argparse.ArgumentParser()
p.add_argument("data")
p.add_argument("--model")
p.add_argument("--json")
a = p.parse_args()
if "fail" in a.data:
    print("stub eval failure", file=sys.stderr)
    sys.exit(1)
payload = {"metrics": {"accuracy": 0.875, "loss": 0.5, "ece": 0.1,
                       "brier_top1": 0.2, "mean_conf": 0.8, "abstain_rate": 0.0},
           "per_question": [{"q": "q1", "answer": "a", "expected": "a",
                             "match": True, "confidence": 0.9}],
           "confusion": [], "errors": [], "n": 1}
with open(a.json, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, ensure_ascii=False)
''',
            encoding="utf-8",
        )
        fail_eval_data = data_dir / "fail-eval.jsonl"
        fail_eval_data.write_text('{"x":1}\n', encoding="utf-8")
        proc, log_handle, server_log = start_server(
            data_dir,
            "core-stdout-1.log",
            extra_args=[
                "--aitk-libs",
                str(aitk_libs_sentinel),
                "--models-dir",
                str(fake_models_root),
                "--assets-dir",
                str(fake_assets_dir),
                "--pick-stub",
                "%s|__cancel__|__no_desktop__" % fake_pick_path,
                "--report-llm",
                r"Z:\no-such-report-model",
                "--eval-cli-stub",
                str(fake_eval_stub),
                "--diffsynth-libs",
                str(fake_diffsynth_libs),
                "--no-vram-check",
            ],
        )
        config = wait_core_json(data_dir, proc)
        port = int(config["port"])
        token = token_file.read_text(encoding="utf-8").strip()

        high_vram = core.vram_admission_decision(30000, 32760, 12, 1500)
        low_vram = core.vram_admission_decision(1000, 32760, 12, 1500)
        checker.check(
            "VRAM admission fake-high used -> 409 with used/total/need/margin",
            high_vram.get("ok") is False
            and high_vram.get("status") == 409
            and all(
                part in high_vram.get("error", "")
                for part in ("used=30000", "total=32760", "need=12288", "margin=1500")
            ),
            "decision=%s" % high_vram,
        )
        checker.check(
            "VRAM admission low used -> ok",
            low_vram.get("ok") is True,
            "decision=%s" % low_vram,
        )
        auto_libs_root = data_dir / "auto-project"
        (auto_libs_root / "runtime" / "pylibs-5").mkdir(parents=True, exist_ok=True)
        auto_paths, auto_warn, auto_source = core.resolve_diffsynth_libs(
            auto_libs_root, "", [], "moss_image"
        )
        checker.check(
            "diffsynth auto python_paths finds runtime/pylibs-5",
            auto_paths == [str((auto_libs_root / "runtime" / "pylibs-5").resolve())]
            and auto_warn is None
            and auto_source == "auto",
            "paths=%s warning=%s source=%s"
            % (auto_paths, auto_warn, auto_source),
        )
        missing_paths, missing_warn, missing_source = core.resolve_diffsynth_libs(
            data_dir / "no-libs-project", "", [], "moss_image"
        )
        checker.check(
            "diffsynth missing libs + moss -> empty paths + warning",
            missing_paths == []
            and missing_warn == "moss_needs_transformers_5"
            and missing_source == "none",
            "paths=%s warning=%s source=%s"
            % (missing_paths, missing_warn, missing_source),
        )
        explicit_paths, explicit_warn, explicit_source = core.resolve_diffsynth_libs(
            auto_libs_root, "", [str(auto_libs_root / ".aitk-pylibs")], "moss_image"
        )
        checker.check(
            "diffsynth explicit params.python_paths replaced by moss pylibs-5",
            explicit_paths
            == [str((auto_libs_root / "runtime" / "pylibs-5").resolve())]
            and explicit_warn is None
            and explicit_source == "params",
            "paths=%s warning=%s source=%s"
            % (explicit_paths, explicit_warn, explicit_source),
        )
        qwen_paths, qwen_warn, qwen_source = core.resolve_diffsynth_libs(
            auto_libs_root, str(fake_diffsynth_libs), [], "qwen_image_21"
        )
        checker.check(
            "diffsynth qwen default paths stay empty (no .aitk-pylibs injection)",
            qwen_paths == [] and qwen_warn is None and qwen_source == "none",
            "paths=%s warning=%s source=%s"
            % (qwen_paths, qwen_warn, qwen_source),
        )
        moss_root = data_dir / "moss-project"
        (moss_root / "runtime" / "pylibs-5").mkdir(parents=True, exist_ok=True)
        (moss_root / ".aitk-pylibs").mkdir(parents=True, exist_ok=True)
        moss_paths, moss_warn, _ = core.resolve_diffsynth_libs(
            moss_root, "", [], "moss_image"
        )
        checker.check(
            "moss auto python_paths starts with runtime/pylibs-5",
            bool(moss_paths) and moss_paths[0] == str((moss_root / "runtime" / "pylibs-5").resolve()),
            "paths=%s warning=%s" % (moss_paths, moss_warn),
        )
        moss_explicit, _, _ = core.resolve_diffsynth_libs(
            moss_root,
            "",
            [str(moss_root / ".aitk-pylibs")],
            "moss_image",
        )
        checker.check(
            "moss explicit .aitk-pylibs is replaced by pylibs-5",
            moss_explicit
            and moss_explicit[0] == str((moss_root / "runtime" / "pylibs-5").resolve())
            and not any(".aitk-pylibs" in item for item in moss_explicit),
            "paths=%s" % moss_explicit,
        )
        fake_moss_libs = data_dir / "fake-moss-pylibs"
        fake_moss_libs.mkdir(parents=True, exist_ok=True)
        (fake_moss_libs / "transformers.py").write_text(
            '__version__ = "4.57.6"\n', encoding="utf-8"
        )
        (fake_moss_libs / "huggingface_hub.py").write_text(
            '__version__ = "1.23.0"\n', encoding="utf-8"
        )
        ok_moss, err_moss, _ = core.preflight_diffsynth_python(
            sys.executable, [str(fake_moss_libs)], "moss_image"
        )
        checker.check(
            "moss preflight rejects transformers<5 before spawning",
            ok_moss is False and "transformers>=5" in str(err_moss),
            "ok=%s error=%s" % (ok_moss, err_moss),
        )
        broken_dist = data_dir / "broken-dist" / "bad-1.0.dist-info"
        broken_dist.mkdir(parents=True, exist_ok=True)
        ok_dist, err_dist, _ = core.preflight_diffsynth_python(
            sys.executable, [str(data_dir / "broken-dist")], "moss_image"
        )
        checker.check(
            "moss preflight names dist-info missing METADATA",
            ok_dist is False and "METADATA" in str(err_dist),
            "ok=%s error=%s" % (ok_dist, err_dist),
        )
        scan_root = data_dir / "scan-models"
        flux_dir = scan_root / "flux2-klein-9b"
        for component in ("transformer", "text_encoder", "vae", "tokenizer", "scheduler"):
            (flux_dir / component).mkdir(parents=True, exist_ok=True)
        (flux_dir / "model_index.json").write_text(
            json.dumps(
                {
                    "_class_name": "Flux2KleinPipeline",
                    "is_distilled": True,
                    "transformer": ["diffusers", "Flux2Transformer2DModel"],
                    "text_encoder": ["transformers", "Qwen3ForCausalLM"],
                    "vae": ["diffusers", "AutoencoderKLFlux2"],
                    "tokenizer": ["transformers", "Qwen2TokenizerFast"],
                    "scheduler": ["diffusers", "FlowMatchEulerDiscreteScheduler"],
                }
            ),
            encoding="utf-8",
        )
        (scan_root / "krea-2-raw").mkdir(parents=True, exist_ok=True)
        (scan_root / "krea-2-raw" / "raw.safetensors").write_bytes(b"stub")
        (scan_root / "krea-2-raw" / "te").mkdir(parents=True, exist_ok=True)
        (scan_root / "krea-2-raw" / "te" / "model.safetensors").write_bytes(b"stub")
        (scan_root / "qwen-image" / "vae").mkdir(parents=True, exist_ok=True)
        (scan_root / "qwen-image" / "vae" / "config.json").write_text("{}", encoding="utf-8")
        (scan_root / "qwen-image" / "vae" / "model.safetensors").write_bytes(b"stub")
        scanned, _ = core.scan_models(scan_root)
        scanned_names = {item["name"]: item for item in scanned}
        checker.check(
            "scan flux2 is complete (is_distilled pipeline-level skipped)",
            scanned_names.get("flux2-klein-9b", {}).get("complete") is True
            and scanned_names.get("flux2-klein-9b", {}).get("missing") == [],
            "item=%s" % scanned_names.get("flux2-klein-9b"),
        )
        checker.check(
            "scan krea raw is complete",
            scanned_names.get("krea-2-raw", {}).get("complete") is True
            and scanned_names.get("krea-2-raw", {}).get("kind") == "diffusion_single",
            "item=%s" % scanned_names.get("krea-2-raw"),
        )
        checker.check(
            "scan excludes component directories (vae)",
            "vae" not in scanned_names,
            "names=%s" % sorted(scanned_names),
        )
        status, _, _ = http_json(
            port,
            "POST",
            "/api/jobs",
            {
                "kind": "diffsynth",
                "params": {
                    "arch": "krea2_raw",
                    "model": str(scan_root / "krea-2-raw" / "raw.safetensors"),
                    "text_encoder": str(scan_root / "krea-2-raw" / "te"),
                    "vae": str(scan_root / "krea-2-raw" / "missing-vae.safetensors"),
                    "dataset": str(data_dir),
                    "out": str(data_dir / "missing-krea-out"),
                },
            },
            token=token,
        )
        checker.check(
            "krea2 missing vae rejected with 400",
            status == 400,
            "status=%d" % status,
        )
        component_root = data_dir / "component-scan"
        (component_root / "qwen3-vl-4b-instruct").mkdir(parents=True, exist_ok=True)
        (component_root / "qwen3-vl-4b-instruct" / "model.safetensors").write_bytes(b"stub")
        (component_root / "qwen-image" / "vae").mkdir(parents=True, exist_ok=True)
        (component_root / "qwen-image" / "vae" / "diffusion_pytorch_model.safetensors").write_bytes(b"stub")
        krea_model = data_dir / "krea-model"
        krea_model.mkdir(parents=True, exist_ok=True)
        (krea_model / "raw.safetensors").write_bytes(b"stub")
        auto_models = [
            {"name": "qwen3-vl-4b-instruct", "id": "qwen3-vl-4b-instruct", "path": str(component_root / "qwen3-vl-4b-instruct")},
            {"name": "krea-2-raw", "id": "krea-2-raw", "path": str(krea_model)},
        ]
        auto_params, auto_fields = core.resolve_components_from_scan(
            "krea2_raw",
            {"arch": "krea2_raw", "model": str(krea_model / "raw.safetensors")},
            auto_models,
            component_root,
        )
        checker.check(
            "component scan auto fills krea2 texture/vae",
            auto_params.get("text_encoder") == str(component_root / "qwen3-vl-4b-instruct")
            and auto_params.get("vae")
            == str((component_root / "qwen-image" / "vae" / "diffusion_pytorch_model.safetensors").resolve())
            and set(auto_fields) == {"text_encoder", "vae"},
            "params=%s auto=%s" % (auto_params, auto_fields),
        )
        flux_model = data_dir / "flux2-auto"
        (flux_model / "tokenizer").mkdir(parents=True, exist_ok=True)
        flux_params, flux_auto = core.resolve_components_from_scan(
            "flux2_klein_9b",
            {"arch": "flux2_klein_9b", "model": str(flux_model)},
            [],
            component_root,
        )
        checker.check(
            "component scan auto fills flux2 tokenizer",
            flux_params.get("tokenizer_path") == str((flux_model / "tokenizer").resolve())
            and "tokenizer_path" in flux_auto,
            "params=%s auto=%s" % (flux_params, flux_auto),
        )

        checker.check(
            "core.json written for --port 0 with host=127.0.0.1",
            config.get("host") == "127.0.0.1" and port > 0,
            "pid=%s port=%d host=%s" % (config.get("pid"), port, config.get("host")),
        )
        checker.check(
            "core.json pid equals server process pid",
            int(config.get("pid", -1)) == proc.pid,
            "core.json=%s process=%s" % (config.get("pid"), proc.pid),
        )
        checker.check(
            "core.json records the frozen UI path",
            Path(config.get("ui_file", "")).name == "index.html"
            and bool(config.get("ui_exists")),
            "ui_file=%s exists=%s bytes=%s"
            % (config.get("ui_file"), config.get("ui_exists"), config.get("ui_bytes")),
        )
        laya_cfg = config.get("laya") or {}
        laya_python_path = Path(laya_cfg.get("python") or "")
        laya_src_path = Path(laya_cfg.get("src") or "")
        checker.check(
            "core.json records the Laya python/src/adapter environment",
            laya_python_path.is_file()
            and laya_src_path.is_dir()
            and Path(laya_cfg.get("adapter") or "").is_file(),
            "python=%s src=%s adapter=%s"
            % (laya_cfg.get("python"), laya_cfg.get("src"), laya_cfg.get("adapter")),
        )
        laya_runner_command = core.build_laya_train_command(
            laya_cfg.get("python"), laya_cfg.get("src")
        )
        checker.check(
            "Laya default runner is [python, -c, sys.path-injecting code]",
            len(laya_runner_command) == 3
            and laya_runner_command[1] == "-c"
            and "sys.path.insert(0," in laya_runner_command[2]
            and "from laya.train_cli import main" in laya_runner_command[2]
            and repr(str(laya_src_path)) in laya_runner_command[2],
            "argv=%s" % json.dumps(laya_runner_command, ensure_ascii=False),
        )
        llm_cfg = config.get("llm") or {}
        checker.check(
            "core.json records the LLM python/script environment",
            Path(llm_cfg.get("python") or "").is_file()
            and Path(llm_cfg.get("script") or "").name == "llm_lora.py"
            and Path(llm_cfg.get("script") or "").is_file(),
            "python=%s script=%s"
            % (llm_cfg.get("python"), llm_cfg.get("script")),
        )
        aitk_cfg = config.get("aitk") or {}
        checker.check(
            "core.json records the aitk python/libs/src/adapter environment",
            Path(aitk_cfg.get("python") or "").name == "python.exe"
            and Path(aitk_cfg.get("libs") or "").name == "aitk-libs-SENTINEL"
            and Path(aitk_cfg.get("src") or "").name == "ai-toolkit"
            and Path(aitk_cfg.get("adapter") or "").name == "aitk_adapter.py"
            and core.DEFAULT_AITK_PYTHON.endswith(
                r"ComfyUI\python_embeded\python.exe"
            )
            and core.DEFAULT_AITK_LIBS.endswith(".aitk-pylibs"),
            "python=%s libs=%s src=%s adapter=%s defaults=%s|%s"
            % (
                aitk_cfg.get("python"),
                aitk_cfg.get("libs"),
                aitk_cfg.get("src"),
                aitk_cfg.get("adapter"),
                core.DEFAULT_AITK_PYTHON,
                core.DEFAULT_AITK_LIBS,
            ),
        )
        aitk_runner_command = core.build_aitk_train_command(
            core.DEFAULT_AITK_PYTHON,
            core.DEFAULT_AITK_LIBS,
            str(PROJECT_ROOT / "third_party" / "ai-toolkit"),
            config_path=str(data_dir / "aitk.yaml"),
        )
        checker.check(
            "aitk runner is [python, -c, path-injecting runpy, config]",
            len(aitk_runner_command) == 4
            and aitk_runner_command[0] == core.DEFAULT_AITK_PYTHON
            and aitk_runner_command[1] == "-c"
            and ".aitk-pylibs" in aitk_runner_command[2]
            and "sys.path.insert(0," in aitk_runner_command[2]
            and "runpy.run_path" in aitk_runner_command[2]
            and "sys.argv = ['run.py', sys.argv[1]]" in aitk_runner_command[2]
            and "third_party" in aitk_runner_command[2]
            and aitk_runner_command[3].endswith("aitk.yaml"),
            "argv=%s" % json.dumps(aitk_runner_command, ensure_ascii=False),
        )
        diffsynth_cfg = config.get("diffsynth") or {}
        checker.check(
            "core.json records the diffsynth python/src/train.py/adapter environment",
            Path(diffsynth_cfg.get("python") or "").is_file()
            and Path(diffsynth_cfg.get("src") or "").name == "DiffSynth-Studio"
            and Path(diffsynth_cfg.get("train_py") or "").name == "train.py"
            and Path(diffsynth_cfg.get("train_py") or "").is_file()
            and Path(diffsynth_cfg.get("adapter") or "").name
            == "diffsynth_adapter.py"
            and Path(diffsynth_cfg.get("adapter") or "").is_file(),
            "python=%s src=%s train_py=%s adapter=%s"
            % (
                diffsynth_cfg.get("python"),
                diffsynth_cfg.get("src"),
                diffsynth_cfg.get("train_py"),
                diffsynth_cfg.get("adapter"),
            ),
        )
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        import adapters.diffsynth_adapter as diffsynth_adapter

        diffsynth_runner = diffsynth_adapter.RUNNER
        checker.check(
            "diffsynth core reuses adapter RUNNER with sampling hook (no naive launcher)",
            "ModelLogger.on_step_end" in diffsynth_runner
            and "cfg_path" in diffsynth_runner
            and not hasattr(core, "build_diffsynth_train_command"),
            "runner_bytes=%d on_step_end=%s cfg_path=%s"
            % (
                len(diffsynth_runner),
                "ModelLogger.on_step_end" in diffsynth_runner,
                "cfg_path" in diffsynth_runner,
            ),
        )
        lf_cfg = config.get("lf") or {}
        checker.check(
            "core.json records the lf python/libs/src/cli/adapter environment",
            Path(lf_cfg.get("python") or "").is_file()
            and Path(lf_cfg.get("libs") or "").name == ".pylibs-lf"
            and Path(lf_cfg.get("libs") or "").is_dir()
            and Path(lf_cfg.get("src") or "").name == "LlamaFactory"
            and Path(lf_cfg.get("cli") or "").name == "cli.py"
            and Path(lf_cfg.get("cli") or "").is_file()
            and Path(lf_cfg.get("adapter") or "").name == "lf_adapter.py"
            and Path(lf_cfg.get("adapter") or "").is_file(),
            "python=%s libs=%s src=%s cli=%s adapter=%s"
            % (
                lf_cfg.get("python"),
                lf_cfg.get("libs"),
                lf_cfg.get("src"),
                lf_cfg.get("cli"),
                lf_cfg.get("adapter"),
            ),
        )
        lf_yaml = str(data_dir / "lf-runner-check.yaml")
        lf_libs_path = str(PROJECT_ROOT / ".pylibs-lf")
        lf_src_path = str(PROJECT_ROOT / "third_party" / "LlamaFactory")
        lf_runner_command = core.build_lf_train_command(
            core.DEFAULT_LF_PYTHON,
            lf_libs_path,
            lf_src_path,
            lf_yaml,
        )
        lf_runner = lf_runner_command[2] if len(lf_runner_command) == 3 else ""
        lf_src_pkg = str(Path(lf_src_path) / "src")
        checker.check(
            "lf runner is [python, -c, libs->repo->src->dill->llamafactory.cli main]",
            len(lf_runner_command) == 3
            and lf_runner_command[0] == core.DEFAULT_LF_PYTHON
            and lf_runner_command[1] == "-c"
            and lf_runner.index(".pylibs-lf") < lf_runner.index("LlamaFactory")
            and lf_runner.index("LlamaFactory") < lf_runner.index(repr(lf_src_pkg))
            and "sys.path.insert(0," in lf_runner
            and "import dill" in lf_runner
            and lf_runner.index("import dill") < lf_runner.index("llamafactory.cli")
            and "sys.argv = ['llamafactory-cli', 'train'" in lf_runner
            and "lf-runner-check.yaml" in lf_runner
            and "sys.exit(main())" in lf_runner,
            "argv=%s bytes=%d"
            % (json.dumps(lf_runner_command[:2], ensure_ascii=False), len(lf_runner)),
        )
        probe_code = (
            core.lf_path_setup_code(lf_libs_path, lf_src_path)
            + "import llamafactory\nprint(llamafactory.extras.env.VERSION)\n"
        )
        probe = subprocess.run(
            [core.DEFAULT_LF_PYTHON, "-B", "-c", probe_code],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            timeout=90,
            creationflags=CREATE_NO_WINDOW,
        )
        probe_out = probe.stdout.decode("utf-8", errors="replace")
        probe_err = probe.stderr.decode("utf-8", errors="replace")
        checker.check(
            "lf real import probe sees llamafactory 0.9.5 with <lf-src>\\src",
            probe.returncode == 0 and "0.9.5" in probe_out,
            "exit=%d stdout=%r stderr_tail=%r"
            % (probe.returncode, probe_out.strip()[:200], probe_err.strip()[-300:]),
        )
        neg_code = (
            core.lf_path_setup_code(lf_libs_path, lf_src_path, include_src=False)
            + "import llamafactory\nprint(llamafactory.extras.env.VERSION)\n"
        )
        neg = subprocess.run(
            [core.DEFAULT_LF_PYTHON, "-B", "-c", neg_code],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            timeout=90,
            creationflags=CREATE_NO_WINDOW,
        )
        neg_out = neg.stdout.decode("utf-8", errors="replace")
        neg_err = neg.stderr.decode("utf-8", errors="replace")
        checker.check(
            "lf negative control without \\src fails ModuleNotFoundError",
            neg.returncode != 0
            and "ModuleNotFoundError" in (neg_out + neg_err)
            and "llamafactory" in (neg_out + neg_err),
            "exit=%d stdout=%r stderr_tail=%r"
            % (neg.returncode, neg_out.strip()[:120], neg_err.strip()[-300:]),
        )
        lf_missing = core.lf_preflight(
            "Z:\\nope\\python.exe",
            "Z:\\nope\\libs",
            "Z:\\nope\\src",
            "Z:\\nope\\adapter.py",
        )
        checker.check(
            "lf preflight names all four missing pieces",
            len(lf_missing) == 4
            and any(item.startswith("lf-python=") for item in lf_missing)
            and any(item.startswith("lf-libs=") for item in lf_missing)
            and any(item.startswith("lf-src=") for item in lf_missing)
            and any(item.startswith("adapter=") for item in lf_missing),
            "missing=%s" % json.dumps(lf_missing, ensure_ascii=False),
        )
        checker.check(
            "token file exists with >=32 chars",
            len(token) >= 32,
            "path=%s chars=%d" % (token_file, len(token)),
        )

        status, _, health = http_json(port, "GET", "/health")
        health = health or {}
        checker.check(
            "GET /health -> 200 with ok/version/pid/uptime_s",
            status == 200
            and health.get("ok") is True
            and health.get("version") == core.VERSION
            and health.get("pid") == proc.pid
            and isinstance(health.get("uptime_s"), (int, float))
            and health.get("uptime_s") >= 0,
            "status=%d body=%s"
            % (status, json.dumps(health, ensure_ascii=False)[:220]),
        )
        status, _, _ = http_json(port, "GET", "/health")
        checker.check("/health is exempt from token auth", status == 200, "status=%d" % status)
        status, _, vram = http_json(port, "GET", "/api/vram", token=token)
        vram = vram or {}
        checker.check(
            "/api/vram -> 200 with used/total/pct/source/ts",
            status == 200
            and vram.get("ok") is True
            and isinstance(vram.get("used_mib"), int)
            and isinstance(vram.get("total_mib"), int)
            and isinstance(vram.get("pct"), (int, float))
            and vram.get("source") == "nvidia-smi"
            and bool(vram.get("ts"))
            and isinstance(vram.get("ram_used_mib"), int)
            and isinstance(vram.get("ram_total_mib"), int)
            and 0 < vram.get("ram_used_mib") <= vram.get("ram_total_mib")
            and isinstance(vram.get("ram_pct"), (int, float))
            and 0 <= vram.get("ram_pct") <= 100,
            "status=%d body=%s" % (status, vram),
        )
        status, _, _ = http_json(port, "GET", "/api/vram")
        checker.check(
            "/api/vram without token -> 401",
            status == 401,
            "status=%d" % status,
        )
        direct_ram = core.system_memory_status()
        checker.check(
            "system_memory_status reports positive used/total/pct",
            isinstance(direct_ram.get("ram_used_mib"), int)
            and isinstance(direct_ram.get("ram_total_mib"), int)
            and 0 < direct_ram.get("ram_used_mib") <= direct_ram.get("ram_total_mib")
            and 0 <= direct_ram.get("ram_pct", -1) <= 100,
            "ram=%s" % direct_ram,
        )
        vram_fail_dir = data_dir / "vram-failure"
        vram_fail_dir.mkdir(parents=True, exist_ok=True)
        vram_proc = vram_log = None
        try:
            vram_proc, vram_log, _vram_log_path = start_server(
                vram_fail_dir,
                "vram-failure.log",
                extra_args=["--vram-cmd", r"Z:\no-such\nvidia-smi.exe", "--ram-probe-fail"],
            )
            vram_cfg = wait_core_json(vram_fail_dir, vram_proc)
            vram_port = int(vram_cfg["port"])
            vram_token = (vram_fail_dir / "token").read_text(encoding="utf-8").strip()
            status, _, vram_fail = http_json(
                vram_port, "GET", "/api/vram", token=vram_token
            )
            checker.check(
                "/api/vram missing nvidia-smi -> HTTP 200 ok:false reason",
                status == 200
                and (vram_fail or {}).get("ok") is False
                and bool((vram_fail or {}).get("reason"))
                and (vram_fail or {}).get("ram_used_mib") is None
                and (vram_fail or {}).get("ram_total_mib") is None
                and (vram_fail or {}).get("ram_pct") is None,
                "status=%d body=%s" % (status, vram_fail),
            )
        finally:
            if vram_proc is not None:
                stop_server(locals().get("vram_port", 0), locals().get("vram_token", ""), vram_proc, vram_log)
        status, _, caption_backends = http_json(
            port, "GET", "/api/caption/backend", token=token
        )
        caption_backends = caption_backends or {}
        backend_ids = [item.get("id") for item in caption_backends.get("backends") or []]
        backend_ok = {
            item.get("id"): item.get("available")
            for item in caption_backends.get("backends") or []
        }
        checker.check(
            "GET /api/caption/backend returns qw35/qw38 with stat-only availability",
            status == 200
            and backend_ids == ["qw35-9b", "qw38-27b"]
            and backend_ok.get("qw35-9b") is True
            and caption_backends.get("engine_available") is True,
            "status=%d body=%s" % (status, caption_backends),
        )
        negative_backend = core.caption_backend_status(
            overrides={
                "qw35-9b": {
                    "model": str(data_dir / "missing-model.gguf"),
                    "mmproj": str(data_dir / "missing-mmproj.gguf"),
                }
            }
        )
        negative_item = (negative_backend.get("backends") or [{}])[0]
        checker.check(
            "caption backend missing model/mmproj -> available:false + reason",
            negative_item.get("available") is False
            and bool(negative_item.get("reason")),
            "body=%s" % negative_item,
        )
        missing_engine = core.caption_backend_status(
            engine=str(data_dir / "missing-llama-server.exe")
        )
        checker.check(
            "caption backend missing engine -> available:false, no exception",
            missing_engine.get("engine_available") is False
            and bool(missing_engine.get("reason"))
            and all(item.get("available") is False for item in missing_engine.get("backends") or []),
            "body=%s" % missing_engine,
        )
        status, _, _ = http_json(
            port,
            "POST",
            "/api/jobs",
            {"kind": "caption", "params": {"dir": str(data_dir / "no-such-caption-dir")}},
            token=token,
        )
        checker.check(
            "caption invalid dir -> 400 names dir",
            status == 400,
            "status=%d" % status,
        )
        status, _, relative_dir_body = http_json(
            port,
            "POST",
            "/api/jobs",
            {"kind": "caption", "params": {"dir": "data/imgs"}},
            token=token,
        )
        checker.check(
            "caption relative dir -> 400 names absolute-path requirement",
            status == 400 and "绝对路径" in str((relative_dir_body or {}).get("error") or ""),
            "status=%d body=%s" % (status, relative_dir_body),
        )
        captions_dir = data_dir / "dataset-captions"
        captions_dir.mkdir(parents=True, exist_ok=True)
        png_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        (captions_dir / "001.png").write_bytes(png_bytes)
        (captions_dir / "002.png").write_bytes(png_bytes)
        (captions_dir / "001.txt").write_bytes(
            b"\xef\xbb\xbf" + "  第一句  \r\n".encode("utf-8")
        )
        status, _, caption_read = http_json(
            port,
            "GET",
            "/api/dataset/captions?dir=" + urllib.parse.quote(str(captions_dir)),
            token=token,
        )
        caption_read = caption_read or {}
        read_items = {item.get("name"): item for item in caption_read.get("items") or []}
        checker.check(
            "dataset captions GET reads srt with BOM stripped and missing item",
            status == 200
            and caption_read.get("ok") is True
            and caption_read.get("count") == 2
            and caption_read.get("missing_caption") == 1
            and read_items.get("001.png", {}).get("caption") == "第一句"
            and read_items.get("001.png", {}).get("has_caption") is True
            and read_items.get("002.png", {}).get("has_caption") is False,
            "status=%d body=%s" % (status, caption_read),
        )
        status, _, _ = http_json(
            port,
            "GET",
            "/api/dataset/captions?dir=" + urllib.parse.quote(str(captions_dir)),
        )
        checker.check(
            "dataset captions GET without token -> 401",
            status == 401,
            "status=%d" % status,
        )
        status, _, caption_write = http_json(
            port,
            "POST",
            "/api/dataset/captions",
            {
                "dir": str(captions_dir),
                "items": [{"name": "002.png", "caption": "新的一句话"}],
            },
            token=token,
        )
        written_path = captions_dir / "002.txt"
        written_bytes = written_path.read_bytes() if written_path.is_file() else b""
        checker.check(
            "dataset captions POST writes utf-8 no BOM with trailing newline",
            status == 200
            and (caption_write or {}).get("written") == 1
            and written_bytes == "新的一句话\n".encode("utf-8"),
            "status=%d body=%s bytes=%r"
            % (status, caption_write, written_bytes),
        )
        status, _, caption_delete = http_json(
            port,
            "POST",
            "/api/dataset/captions",
            {"dir": str(captions_dir), "items": [{"name": "001.png", "caption": ""}]},
            token=token,
        )
        checker.check(
            "dataset captions POST empty string deletes txt",
            status == 200
            and (caption_delete or {}).get("deleted") == 1
            and not (captions_dir / "001.txt").exists(),
            "status=%d body=%s exists=%s"
            % (status, caption_delete, (captions_dir / "001.txt").exists()),
        )
        status, _, traversal_body = http_json(
            port,
            "POST",
            "/api/dataset/captions",
            {
                "dir": str(captions_dir),
                "items": [{"name": "../evil.png", "caption": "pwn"}],
            },
            token=token,
        )
        checker.check(
            "dataset captions traversal rejected without writing",
            status == 400
            and "invalid caption name" in str((traversal_body or {}).get("error") or "")
            and not (captions_dir.parent / "evil.txt").exists(),
            "status=%d body=%s" % (status, traversal_body),
        )
        status, _, _ = http_json(
            port,
            "POST",
            "/api/dataset/captions",
            {"dir": "data/captions", "items": []},
            token=token,
        )
        checker.check(
            "dataset captions relative dir -> 400",
            status == 400,
            "status=%d" % status,
        )
        status, _, _ = http_json(
            port,
            "POST",
            "/api/jobs",
            {"kind": "caption", "params": {"dir": str(data_dir), "backend": "bad"}},
            token=token,
        )
        checker.check(
            "caption invalid backend -> 400 names backend",
            status == 400,
            "status=%d" % status,
        )
        status, _, _ = http_json(
            port,
            "POST",
            "/api/jobs",
            {
                "kind": "caption",
                "params": {
                    "dir": str(data_dir),
                    "batch_size": 9,
                },
            },
            token=token,
        )
        checker.check(
            "caption invalid batch_size -> 400",
            status == 400,
            "status=%d" % status,
        )
        caption_dir = data_dir / "caption-images"
        caption_dir.mkdir(parents=True, exist_ok=True)
        for index in range(2):
            (caption_dir / ("%d.png" % index)).write_bytes(
                base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
                )
            )
        caption_model = data_dir / "caption-model.gguf"
        caption_model.write_bytes(b"fake")
        caption_mmproj = data_dir / "caption-mmproj.gguf"
        caption_mmproj.write_bytes(b"fake")
        caption_params = {
            "dir": str(caption_dir),
            "backend": "auto",
            "model": str(caption_model),
            "mmproj": str(caption_mmproj),
            "server_cmd": [sys.executable, str(caption_stub), "--port", "{port}"],
            "lang": "zh",
            "max_chars": 40,
            "caption_ext": ".txt",
            "overwrite": True,
            "limit": 2,
            "batch_size": 6,
        }
        status, _, created_caption = http_json(
            port, "POST", "/api/jobs", {"kind": "caption", "params": caption_params}, token=token
        )
        caption_final = wait_job(
            port,
            token,
            (created_caption or {}).get("job_id"),
            ("succeeded", "failed", "cancelled", "interrupted"),
            30,
        ) or {}
        caption_metrics = caption_final.get("metrics") or {}
        checker.check(
            "caption job succeeds with done/total progress",
            status == 202
            and caption_final.get("state") == "succeeded"
            and caption_metrics.get("done") == 2
            and caption_metrics.get("total") == 2
            and (caption_final.get("params") or {}).get("batch_size") == 6
            and (caption_dir / "0.txt").is_file()
            and (caption_dir / "1.txt").is_file(),
            "status=%d state=%s metrics=%s"
            % (status, caption_final.get("state"), caption_metrics),
        )
        crash_params = dict(caption_params)
        crash_params["server_cmd"] = [
            sys.executable,
            str(caption_stub),
            "--port",
            "{port}",
            "--crash",
        ]
        status, _, created_crash = http_json(
            port, "POST", "/api/jobs", {"kind": "caption", "params": crash_params}, token=token
        )
        crash_final = wait_job(
            port, token, (created_crash or {}).get("job_id"), ("failed", "cancelled", "interrupted"), 30
        ) or {}
        health_after_crash = http_json(port, "GET", "/health")[0]
        checker.check(
            "caption adapter crash -> failed with boom, core health stays 200",
            status == 202
            and crash_final.get("state") == "failed"
            and "boom" in str(crash_final.get("error") or "").lower()
            and health_after_crash == 200,
            "state=%s error=%s health=%s"
            % (crash_final.get("state"), crash_final.get("error"), health_after_crash),
        )
        parallel_dir = data_dir / "caption-parallel"
        parallel_dir.mkdir(parents=True, exist_ok=True)
        (parallel_dir / "0.png").write_bytes(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            )
        )
        parallel_params = dict(caption_params)
        parallel_params.update(
            {"dir": str(parallel_dir), "batch_size": 1, "limit": 1,
             "server_cmd": [sys.executable, str(caption_stub), "--port", "{port}", "--delay", "0.5"]}
        )
        _, _, p1_body = http_json(
            port, "POST", "/api/jobs", {"kind": "caption", "params": parallel_params}, token=token
        )
        _, _, p2_body = http_json(
            port, "POST", "/api/jobs", {"kind": "caption", "params": parallel_params}, token=token
        )
        p1_id = (p1_body or {}).get("job_id")
        p2_id = (p2_body or {}).get("job_id")
        p2_seen = {}
        deadline = time.time() + 10
        while time.time() < deadline:
            p2_seen = get_job(port, token, p2_id) or {}
            if p2_seen.get("state") == "queued" and p2_seen.get("queue_position") == 1:
                break
            time.sleep(0.1)
        p1_final = wait_job(port, token, p1_id, ("succeeded", "failed", "cancelled"), 30) or {}
        p2_final = wait_job(port, token, p2_id, ("succeeded", "failed", "cancelled"), 30) or {}
        p1_metrics = p1_final.get("metrics") or {}
        p2_metrics = p2_final.get("metrics") or {}
        checker.check(
            "two caption jobs serialize: second queued, starts after first finished",
            p2_seen.get("state") == "queued"
            and p2_seen.get("queue_position") == 1
            and p1_final.get("state") == "succeeded"
            and p2_final.get("state") == "succeeded"
            and str(p2_metrics.get("started_at") or "") >= str(p1_metrics.get("finished_at") or ""),
            "p2_seen=%s p1=%s p2=%s"
            % (p2_seen.get("state"), p1_metrics.get("finished_at"), p2_metrics.get("started_at")),
        )
        cancel_dir = data_dir / "caption-cancel"
        cancel_dir.mkdir(parents=True, exist_ok=True)
        for index in range(4):
            (cancel_dir / ("%d.png" % index)).write_bytes(
                base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
                )
            )
        cancel_params = dict(caption_params)
        cancel_params["dir"] = str(cancel_dir)
        cancel_params["batch_size"] = 1
        cancel_params["server_cmd"] = [
            sys.executable,
            str(caption_stub),
            "--port",
            "{port}",
            "--delay",
            "0.8",
        ]
        status, _, created_cancel = http_json(
            port, "POST", "/api/jobs", {"kind": "caption", "params": cancel_params}, token=token
        )
        cancel_id = (created_cancel or {}).get("job_id")
        cancel_running = {}
        cancel_deadline = time.time() + 10
        while time.time() < cancel_deadline:
            cancel_running = get_job(port, token, cancel_id) or {}
            if cancel_running.get("state") == "running":
                break
            time.sleep(0.1)
        time.sleep(1.2)
        written_before = list(cancel_dir.glob("*.txt"))
        status, _, cancel_body = http_json(
            port, "POST", "/api/jobs/%s/cancel" % cancel_id, token=token
        )
        cancelled_job = wait_job(
            port, token, cancel_id, ("cancelled", "succeeded", "failed", "interrupted"), 30
        ) or {}
        checker.check(
            "caption job cancel -> cancelled and written captions survive",
            status == 200
            and cancelled_job.get("state") == "cancelled"
            and bool(written_before)
            and all(path.is_file() for path in written_before),
            "status=%d state=%s written=%s"
            % (status, cancelled_job.get("state"), [p.name for p in written_before]),
        )
        cleanup_conn = db_connect(data_dir)
        try:
            cleanup_conn.execute(
                "DELETE FROM jobs WHERE id IN (?, ?, ?, ?, ?)",
                (
                    (created_caption or {}).get("job_id"),
                    cancel_id,
                    (created_crash or {}).get("job_id"),
                    p1_id,
                    p2_id,
                ),
            )
            cleanup_conn.commit()
        finally:
            cleanup_conn.close()
        expected_code_sha = hashlib.sha256(
            Path(core.__file__).resolve().read_bytes()
        ).hexdigest()[:16]
        checker.check(
            "GET /health exposes code_sha256/code_mtime/started_at",
            health.get("code_sha256") == expected_code_sha
            and bool(health.get("code_mtime"))
            and bool(health.get("started_at")),
            "code_sha256=%s code_mtime=%s started_at=%s"
            % (
                health.get("code_sha256"),
                health.get("code_mtime"),
                health.get("started_at"),
            ),
        )
        first_log_line = ""
        try:
            first_log_line = server_log.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()[0]
        except (OSError, IndexError):
            pass
        startup_log_text = ""
        try:
            startup_log_text = server_log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        checker.check(
            "startup log first line is data_dir=<actual>",
            first_log_line.startswith("data_dir=")
            and Path(first_log_line.split("=", 1)[1]).resolve()
            == data_dir.resolve(),
            "first_line=%r" % first_log_line.replace("\ufffd", "<U+FFFD>"),
        )
        checker.check(
            "Windows Job Object enabled (kill-on-close)",
            "job_object=enabled" in startup_log_text,
            "job_object_line=%r"
            % next(
                (
                    line
                    for line in startup_log_text.splitlines()
                    if line.startswith("job_object=")
                ),
                "",
            ),
        )
        checker.check(
            "platform backend is win on this host",
            "platform=win" in startup_log_text,
            "platform_line=%r"
            % next(
                (
                    line
                    for line in startup_log_text.splitlines()
                    if line.startswith("platform=")
                ),
                "",
            ),
        )
        checker.check(
            "PROJECT_ROOT derives from core file when MODELSMITH_HOME is unset",
            Path(core.PROJECT_ROOT).resolve()
            == Path(core.__file__).resolve().parent.parent,
            "project_root=%s" % core.PROJECT_ROOT,
        )
        marker = str(core._LLM_MODELS)
        stale_proc = {
            "Name": "llama-server.exe",
            "ProcessId": 41001,
            "ParentProcessId": 999999,
            "CommandLine": "--mmproj %s\\mmproj.gguf -m %s\\model.gguf" % (marker, marker),
        }
        selected = core.select_stale([stale_proc], current_pid=1234)
        checker.check(
            "select_stale picks matching stale llama-server",
            [item.get("ProcessId") for item in selected] == [41001],
            "selected=%s" % selected,
        )
        no_feature = dict(stale_proc, ProcessId=41002, CommandLine="--model other.gguf")
        checker.check(
            "select_stale rejects llama-server without --mmproj",
            core.select_stale([no_feature], current_pid=1234) == [],
            "selected=%s" % core.select_stale([no_feature], current_pid=1234),
        )
        wrong_name = dict(stale_proc, Name="python.exe", ProcessId=41003)
        checker.check(
            "select_stale rejects non-llama-server image",
            core.select_stale([wrong_name], current_pid=1234) == [],
            "selected=%s" % core.select_stale([wrong_name], current_pid=1234),
        )
        own_child = dict(stale_proc, ProcessId=41004, ParentProcessId=1234)
        checker.check(
            "select_stale rejects child of current core",
            core.select_stale([own_child], current_pid=1234) == [],
            "selected=%s" % core.select_stale([own_child], current_pid=1234),
        )
        checker.check(
            "select_stale empty list -> empty",
            core.select_stale([], current_pid=1234) == [],
            "selected=%s" % core.select_stale([], current_pid=1234),
        )

        status, headers, body = http_json(port, "GET", "/", raw=True)
        v3_baseline = V3_INDEX.read_bytes() if V3_INDEX.is_file() else b""
        body_bytes = body or b""
        expected_body = core.inject_v3_bootstrap(v3_baseline, token)
        v3_disk_after = V3_INDEX.read_bytes() if V3_INDEX.is_file() else b""
        token_marker = b'window.__MS_TOKEN__="' + token.encode("utf-8") + b'"'
        api_marker = b'window.__MS_API__="/api"'
        v3_marker = b'window.__MS_UI="v3"'
        wire_marker = b'<script src="/ms-wire.js"></script>'
        served_sha = hashlib.sha256(body_bytes).hexdigest() if body_bytes else ""
        v3_sha = hashlib.sha256(v3_baseline).hexdigest() if v3_baseline else ""
        checker.check(
            "GET / -> 200 text/html serving v3",
            status == 200
            and "text/html" in headers.get("Content-Type", "")
            and b"ModelSmith" in body_bytes,
            "status=%d content-type=%s bytes=%d"
            % (status, headers.get("Content-Type"), len(body_bytes)),
        )
        checker.check(
            "GET / injects v3 token/API/UI bootstrap + ms-wire.js before </body>",
            bool(body_bytes)
            and body_bytes == expected_body
            and token_marker in body_bytes
            and api_marker in body_bytes
            and v3_marker in body_bytes
            and wire_marker in body_bytes
            and body_bytes.lower().rfind(b"</body>") > body_bytes.find(b"window.__MS_UI"),
            "served_bytes=%d v3_bytes=%d delta=%d sha256=%s"
            % (
                len(body_bytes),
                len(v3_baseline),
                len(body_bytes) - len(v3_baseline),
                served_sha[:16],
            ),
        )
        checker.check(
            "frozen design/prototype/v3/index.html on disk is untouched",
            v3_disk_after == v3_baseline
            and token_marker not in v3_baseline
            and v3_sha == hashlib.sha256(v3_disk_after).hexdigest(),
            "v3_bytes=%d v3_sha256=%s disk_sha256=%s"
            % (
                len(v3_baseline),
                v3_sha[:16],
                hashlib.sha256(v3_disk_after).hexdigest()[:16],
            ),
        )

        model_index_disk = V3_DIR / "model-index.js"
        status, headers, body = http_json(
            port, "GET", "/v3/model-index.js", raw=True
        )
        checker.check(
            "GET /v3/model-index.js -> 200 bytes equal disk",
            status == 200
            and body == model_index_disk.read_bytes()
            and "javascript" in headers.get("Content-Type", "").lower(),
            "status=%d bytes=%d content-type=%s"
            % (status, len(body or b""), headers.get("Content-Type")),
        )
        status, _, body_root = http_json(
            port, "GET", "/model-index.js", raw=True
        )
        checker.check(
            "GET /model-index.js (root-relative v3 asset) -> 200 bytes equal disk",
            status == 200 and body_root == model_index_disk.read_bytes(),
            "status=%d bytes=%d" % (status, len(body_root or b"")),
        )

        llm_disk = (V3_DIR / "llm.html").read_bytes()
        status, _, llm_body = http_json(port, "GET", "/llm.html", raw=True)
        checker.check(
            "GET /llm.html (root-relative v3 page) -> 200 injected, disk unchanged",
            status == 200
            and llm_body == core.inject_v3_bootstrap(llm_disk, token)
            and (V3_DIR / "llm.html").read_bytes() == llm_disk,
            "status=%d bytes=%d disk_bytes=%d"
            % (status, len(llm_body or b""), len(llm_disk)),
        )
        status, _, legacy_body = http_json(port, "GET", "/legacy", raw=True)
        checker.check(
            "GET /legacy -> 200 old baseline with token injection",
            status == 200
            and bool(legacy_body)
            and b"window.__MS_TOKEN__" in legacy_body
            and b'window.__MS_UI="v3"' not in legacy_body,
            "status=%d bytes=%d" % (status, len(legacy_body or b"")),
        )
        status, _, _ = http_json(
            port, "GET", "/v3/../../core/modelsmith_core.py"
        )
        checker.check(
            "GET /v3/../ traversal blocked (403/404)",
            status in (403, 404),
            "status=%d" % status,
        )

        wire_temp = fake_assets_dir / "ms-wire.js"
        wire_preexisting = wire_temp.is_file()
        status, _, _ = http_json(port, "GET", "/ms-wire.js")
        checker.check(
            "GET /ms-wire.js missing in temp assets dir -> 404, service alive",
            status == 404 and not wire_preexisting,
            "status=%d preexisting=%s" % (status, wire_preexisting),
        )
        wire_temp.write_bytes(b"// ms-wire selftest\n")
        try:
            status, headers, wire_body = http_json(
                port, "GET", "/ms-wire.js", raw=True
            )
            checker.check(
                "GET /ms-wire.js after temp file -> 200 javascript bytes",
                status == 200
                and wire_body == b"// ms-wire selftest\n"
                and "javascript" in headers.get("Content-Type", "").lower(),
                "status=%d bytes=%d content-type=%s"
                % (status, len(wire_body or b""), headers.get("Content-Type")),
            )
        finally:
            try:
                wire_temp.unlink()
            except FileNotFoundError:
                pass
        checker.check(
            "service still alive after ms-wire 404",
            http_json(port, "GET", "/health")[0] == 200,
            "health=%d" % http_json(port, "GET", "/health")[0],
        )
        checker.check(
            "GET /assets/<missing> -> 404 (reserved route)",
            http_json(port, "GET", "/assets/does-not-exist.png")[0] == 404,
            "status=%d" % http_json(port, "GET", "/assets/does-not-exist.png")[0],
        )

        status, _, _ = http_json(port, "GET", "/api/models")
        checker.check(
            "GET /api/models without token -> 401",
            status == 401,
            "status=%d" % status,
        )
        status, _, models_payload = http_json(
            port, "GET", "/api/models", token=token
        )
        models_payload = models_payload or {}
        api_models = models_payload.get("models") or []
        kinds = {item.get("name"): item.get("kind") for item in api_models}
        checker.check(
            "GET /api/models content-detects four fake model kinds",
            status == 200
            and Path(models_payload.get("root") or "").resolve()
            == fake_models_root.resolve()
            and kinds
            == {
                "diffusers-a": "diffusion",
                "diffusers-bad": "diffusion",
                "diffusers-full": "diffusion",
                "single-a": "diffusion_single",
                "llm-a": "llm",
                "laya-a": "laya",
                "unknown-a": "unknown",
            },
            "status=%d root=%s kinds=%s"
            % (status, models_payload.get("root"), kinds),
        )
        checker.check(
            "GET /api/models sorts complete first and lists unknown as non-selectable",            all(
                isinstance(item.get("size_bytes"), int)
                and isinstance(item.get("files"), int)
                and item.get("path")
                and "class_name" in item
                and "complete" in item
                and "missing" in item
                for item in api_models
            )
            and api_models[-1].get("name") == "diffusers-bad"
            and api_models[-1].get("complete") is False
            and "text_encoder" in (api_models[-1].get("missing") or [])
            and next(
                (item for item in api_models if item.get("name") == "diffusers-full"),
                {},
            ).get("complete")
            is True
            and next(
                (item for item in api_models if item.get("name") == "unknown-a"),
                {},
            ).get("kind")
            == "unknown",
            "kinds=%s"
            % [(item.get("name"), item.get("kind"), item.get("complete")) for item in api_models],
        )
        status, _, _ = http_json(
            port,
            "POST",
            "/api/jobs",
            {
                "kind": "diffsynth",
                "params": {
                    "model": str(fake_models_root / "diffusers-bad"),
                    "dataset": str(data_dir / "fake-dataset"),
                    "out": str(data_dir / "fake-out"),
                    "train_command": [sys.executable, str(DIFFSYNTH_STUB)],
                },
            },
            token=token,
        )
        checker.check(
            "diffsynth incomplete model rejected with 400 naming missing component",
            status == 400,
            "status=%d" % status,
        )
        status, _, filtered_models = http_json(
            port, "GET", "/api/models?kind=llm", token=token
        )
        checker.check(
            "GET /api/models?kind=llm filters to llm only",
            status == 200
            and [item.get("kind") for item in ((filtered_models or {}).get("models") or [])]
            == ["llm"],
            "status=%d models=%s"
            % (
                status,
                [
                    (item.get("name"), item.get("kind"))
                    for item in ((filtered_models or {}).get("models") or [])
                ],
            ),
        )
        resolved_llm = core.resolve_model_reference(
            "llm-a", fake_models_root, api_models
        )
        checker.check(
            "models relative id -> absolute path; unknown/absolute unchanged",
            Path(resolved_llm).resolve() == (fake_models_root / "llm-a").resolve()
            and core.resolve_model_reference("unknown-a", fake_models_root, api_models)
            == "unknown-a"
            and core.resolve_model_reference(
                str(fake_models_root / "llm-a"), fake_models_root, api_models
            )
            == str(fake_models_root / "llm-a"),
            "llm=%s unknown=%s"
            % (
                resolved_llm,
                core.resolve_model_reference("unknown-a", fake_models_root, api_models),
            ),
        )
        status, _, models_payload_two = http_json(
            port, "GET", "/api/models", token=token
        )
        checker.check(
            "models scan cache returns same scanned_at",
            status == 200
            and (models_payload_two or {}).get("scanned_at")
            == models_payload.get("scanned_at")
            and (models_payload_two or {}).get("cache_hit") is True,
            "first=%s second=%s cache_hit=%s"
            % (
                models_payload.get("scanned_at"),
                (models_payload_two or {}).get("scanned_at"),
                (models_payload_two or {}).get("cache_hit"),
            ),
        )
        status, _, diffusion_models = http_json(
            port, "GET", "/api/models?kind=diffusion", token=token
        )
        diffusion_names = [
            item.get("name") for item in (diffusion_models or {}).get("models") or []
        ]
        checker.check(
            "kind=diffusion includes diffusion_single",
            status == 200
            and "single-a" in diffusion_names
            and "diffusers-bad" in diffusion_names
            and "llm-a" not in diffusion_names,
            "names=%s" % diffusion_names,
        )
        unknown_item = next(
            (item for item in api_models if item.get("name") == "unknown-a"), {}
        )
        checker.check(
            "unknown model has why field",
            bool(unknown_item.get("why")),
            "unknown=%s" % unknown_item,
        )

        # -- F18 native picker via --pick-stub, health stays responsive -----
        pick_result = {}

        def _do_pick():
            pick_result.update(
                http_json(
                    port,
                    "POST",
                    "/api/pick",
                    {"mode": "file", "title": "pick selftest", "filter": ["jsonl"]},
                    token=token,
                )[2]
                or {}
            )

        pick_thread = threading.Thread(target=_do_pick)
        pick_thread.start()
        time.sleep(0.25)
        health_during_pick = http_json(port, "GET", "/health")[0]
        pick_thread.join(timeout=5)
        checker.check(
            "F18 /api/pick stub returns absolute path while /health stays 200",
            health_during_pick == 200
            and pick_result.get("ok") is True
            and pick_result.get("path") == str(fake_pick_path),
            "health=%d result=%s" % (health_during_pick, pick_result),
        )
        status, _, picked_cancel = http_json(
            port, "POST", "/api/pick", {"mode": "dir"}, token=token
        )
        checker.check(
            "F18 /api/pick cancel -> ok:false cancelled:true",
            status == 200 and (picked_cancel or {}).get("ok") is False
            and (picked_cancel or {}).get("cancelled") is True,
            "status=%d body=%s" % (status, picked_cancel),
        )
        status, _, picked_nod = http_json(
            port, "POST", "/api/pick", {"mode": "file"}, token=token
        )
        checker.check(
            "F18 /api/pick no desktop -> reason=no_desktop",
            status == 200 and (picked_nod or {}).get("ok") is False
            and (picked_nod or {}).get("reason") == "no_desktop",
            "status=%d body=%s" % (status, picked_nod),
        )
        status, _, _ = http_json(
            port, "POST", "/api/pick", {"mode": "bogus"}, token=token
        )
        checker.check("F18 /api/pick invalid mode -> 400", status == 400, "status=%d" % status)

        # -- F07 model download: local HTTP server, no external network -----
        download_root = data_dir / "download-source"
        download_root.mkdir(parents=True, exist_ok=True)
        download_payload = b"D" * 146
        (download_root / "payload.bin").write_bytes(download_payload)

        class _QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path.startswith("/slow.bin"):
                    self.send_response(200)
                    self.send_header("Content-Length", "204800")
                    self.end_headers()
                    try:
                        for _ in range(200):
                            self.wfile.write(b"X" * 1024)
                            self.wfile.flush()
                            time.sleep(0.1)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                super().do_GET()

        httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            functools.partial(_QuietHandler, directory=str(download_root)),
        )
        httpd_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        httpd_thread.start()
        local_base = "http://127.0.0.1:%d" % httpd.server_port
        download_url = local_base + "/payload.bin"
        missing_url = local_base + "/missing.bin"

        def wait_download(model_id, timeout=15):
            deadline = time.time() + timeout
            last = None
            while time.time() < deadline:
                status, _, body = http_json(
                    port,
                    "GET",
                    "/api/models/%s/download/status" % model_id,
                    token=token,
                )
                if status == 200 and isinstance(body, dict):
                    last = body
                    if body.get("state") in ("succeeded", "failed"):
                        return body
                time.sleep(0.05)
            return last or {}

        try:
            status, _, download_started = http_json(
                port,
                "POST",
                "/api/models/diffusers-a/download",
                {"sources": [download_url], "resume": True},
                token=token,
            )
            download_state = wait_download("diffusers-a")
            download_target = (
                fake_models_root / "diffusers-a" / "payload.bin"
            )
            download_events = [item.get("type") for item in download_state.get("events", [])]
            checker.check(
                "F07 download local source: attempt/done, 146 B, .part gone",
                status == 202
                and download_state.get("state") == "succeeded"
                and "attempt" in download_events
                and "done" in download_events
                and download_state.get("bytes") == 146
                and download_target.read_bytes() == download_payload
                and not Path(str(download_target) + ".part").exists(),
                "status=%d state=%s bytes=%s events=%s target=%s"
                % (
                    status,
                    download_state.get("state"),
                    download_state.get("bytes"),
                    download_events,
                    download_target,
                ),
            )

            status, _, fallback_started = http_json(
                port,
                "POST",
                "/api/models/llm-a/download",
                {"sources": [missing_url, download_url], "resume": True},
                token=token,
            )
            fallback_state = wait_download("llm-a")
            fallback_events = [item.get("type") for item in fallback_state.get("events", [])]
            checker.check(
                "F07 404 source switches to second and succeeds",
                status == 202
                and fallback_state.get("state") == "succeeded"
                and "switch" in fallback_events
                and fallback_state.get("bytes") == 146,
                "status=%d state=%s events=%s"
                % (status, fallback_state.get("state"), fallback_events),
            )

            status, _, failed_started = http_json(
                port,
                "POST",
                "/api/models/laya-a/download",
                {"sources": [missing_url, missing_url + "?x=1"], "resume": True},
                token=token,
            )
            failed_state = wait_download("laya-a")
            checker.check(
                "F07 all sources 404 -> failed with non-empty errors",
                status == 202
                and failed_state.get("state") == "failed"
                and len(failed_state.get("errors") or []) >= 2
                and "failed" in [item.get("type") for item in failed_state.get("events", [])],
                "status=%d state=%s errors=%s"
                % (status, failed_state.get("state"), failed_state.get("errors")),
            )

            status, _, cancel_started = http_json(
                port,
                "POST",
                "/api/models/unknown-a/download",
                {"sources": [local_base + "/slow.bin"], "resume": True},
                token=token,
            )
            time.sleep(0.25)
            status, _, cancel_body = http_json(
                port,
                "POST",
                "/api/models/unknown-a/download/cancel",
                token=token,
            )
            cancelled_state = wait_download("unknown-a")
            part_path = fake_models_root / "unknown-a" / "slow.bin.part"
            size_before = (
                (fake_models_root / "unknown-a" / "slow.bin").stat().st_size
                if (fake_models_root / "unknown-a" / "slow.bin").is_file()
                else 0
            )
            time.sleep(0.5)
            size_after = (
                (fake_models_root / "unknown-a" / "slow.bin").stat().st_size
                if (fake_models_root / "unknown-a" / "slow.bin").is_file()
                else 0
            )
            checker.check(
                "F07 download cancel -> cancelled, .part gone, growth stops",
                status == 200
                and (cancel_body or {}).get("state") == "cancelled"
                and cancelled_state.get("state") == "cancelled"
                and not part_path.exists()
                and size_before == size_after,
                "status=%d body_state=%s poll_state=%s part=%s size=%d/%d"
                % (
                    status,
                    (cancel_body or {}).get("state"),
                    cancelled_state.get("state"),
                    part_path.exists(),
                    size_before,
                    size_after,
                ),
            )
        finally:
            httpd.shutdown()
            httpd.server_close()

        # -- F18 /api/open dry-run and path confinement ---------------------
        allowed_open = str(PROJECT_ROOT / "_runs")
        status, _, opened = http_json(
            port, "POST", "/api/open?dry=1", {"path": allowed_open}, token=token
        )
        checker.check(
            "F18 /api/open?dry=1 allows project _runs path",
            status == 200 and (opened or {}).get("ok") is True
            and (opened or {}).get("dry") is True,
            "status=%d body=%s" % (status, opened),
        )
        for bad_path in ("C:\\Windows", "..\\..\\x"):
            status, _, _ = http_json(
                port, "POST", "/api/open?dry=1", {"path": bad_path}, token=token
            )
            checker.check(
                "F18 /api/open rejects %r with 403" % bad_path,
                status == 403,
                "status=%d" % status,
            )

        status, _, _ = http_json(port, "GET", "/api/jobs")
        checker.check("GET /api/jobs without token -> 401", status == 401, "status=%d" % status)
        status, _, _ = http_json(port, "GET", "/api/jobs", token="definitely-wrong")
        checker.check(
            "GET /api/jobs with wrong token -> 401", status == 401, "status=%d" % status
        )
        status, _, listing = http_json(port, "GET", "/api/jobs", token=token)
        checker.check(
            "GET /api/jobs with valid token -> 200 and empty list",
            status == 200
            and isinstance(listing, dict)
            and listing.get("ok") is True
            and listing.get("jobs") == [],
            "status=%d counts=%s"
            % (status, json.dumps((listing or {}).get("counts"), ensure_ascii=False)),
        )

        # -- F34 starred artifacts, idempotent merge ------------------------
        status, _, star_created = submit(
            port, token, {"kind": "fake", "params": {"steps": 4, "interval_s": 0.05}}
        )
        star_job_id = (star_created or {}).get("job_id")
        wait_job(
            port, token, star_job_id, ("succeeded", "failed", "cancelled", "interrupted"), 20
        )
        status = 0
        starred = None
        for artifact in ("candidate-a.safetensors", "candidate-a.safetensors"):
            status, _, starred = http_json(
                port,
                "POST",
                "/api/jobs/%s/star" % star_job_id,
                {"artifact": artifact, "starred": True},
                token=token,
            )
        status2, _, starred2 = http_json(
            port,
            "POST",
            "/api/jobs/%s/star" % star_job_id,
            {"artifact": "candidate-b.safetensors", "starred": True},
            token=token,
        )
        star_job = get_job(port, token, star_job_id) or {}
        star_metrics = star_job.get("metrics") or {}
        checker.check(
            "F34 star is idempotent and merges two distinct artifacts",
            status == 200
            and status2 == 200
            and (starred or {}).get("starred_artifacts") == ["candidate-a.safetensors"]
            and (starred2 or {}).get("starred_artifacts")
            == ["candidate-a.safetensors", "candidate-b.safetensors"]
            and star_metrics.get("starred_artifacts")
            == ["candidate-a.safetensors", "candidate-b.safetensors"],
            "first=%s second=%s metrics=%s"
            % (
                (starred or {}).get("starred_artifacts"),
                (starred2 or {}).get("starred_artifacts"),
                star_metrics.get("starred_artifacts"),
            ),
        )
        status, _, _ = http_json(
            port,
            "POST",
            "/api/jobs/job-20200101-000000-deadbeef/star",
            {"artifact": "x.safetensors", "starred": True},
            token=token,
        )
        checker.check("F34 star nonexistent job -> 404", status == 404, "status=%d" % status)

        # -- F22 metrics history replay ------------------------------------
        status, _, history = http_json(
            port,
            "GET",
            "/api/jobs/%s/metrics_history" % star_job_id,
            token=token,
        )
        history = history or {}
        history_rows = history.get("rows") or []
        history_seqs = [row.get("seq") for row in history_rows]
        checker.check(
            "F22 metrics_history has >=3 monotonic rows",
            status == 200
            and len(history_rows) >= 3
            and history_seqs == sorted(history_seqs)
            and len(set(history_seqs)) == len(history_seqs),
            "status=%d rows=%d seq=%s next=%s"
            % (status, len(history_rows), history_seqs, history.get("next_seq")),
        )
        last_seq = history_seqs[-1] if history_seqs else 0
        status, _, history_after = http_json(
            port,
            "GET",
            "/api/jobs/%s/metrics_history?since=%d" % (star_job_id, last_seq),
            token=token,
        )
        checker.check(
            "F22 metrics_history?since=last returns no rows",
            status == 200
            and (history_after or {}).get("rows") == []
            and (history_after or {}).get("next_seq") == last_seq,
            "status=%d body=%s" % (status, history_after),
        )

        # -- F17 corpus hint: laya sample, CSV, missing --------------------
        laya_sample = PROJECT_ROOT / "skills" / "laya-corpus" / "examples" / "customer-service-3rows.jsonl"
        status, _, laya_hint = http_json(
            port,
            "GET",
            "/api/data/hint?path=%s" % urllib.parse.quote(str(laya_sample)),
            token=token,
        )
        checker.check(
            "F17 laya JSONL -> format=laya, lang_hint=zh",
            status == 200
            and (laya_hint or {}).get("ok") is True
            and (laya_hint or {}).get("format") == "laya"
            and (laya_hint or {}).get("lang_hint") == "zh"
            and (laya_hint or {}).get("rows_guess") == 3,
            "status=%d body=%s" % (status, laya_hint),
        )
        csv_path = PROJECT_ROOT / "_tmp" / (
            "task54-hint-%s.csv" % uuid.uuid4().hex
        )
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_text("text,label\nhello,1\nworld,0\n", encoding="utf-8")
        try:
            status, _, csv_hint = http_json(
                port,
                "GET",
                "/api/data/hint?path=%s" % urllib.parse.quote(str(csv_path)),
                token=token,
            )
        finally:
            try:
                csv_path.unlink()
            except FileNotFoundError:
                pass
        checker.check(
            "F17 text,label CSV -> format=csv, lang_hint=en",
            status == 200
            and (csv_hint or {}).get("format") == "csv"
            and (csv_hint or {}).get("lang_hint") == "en"
            and (csv_hint or {}).get("rows_guess") == 2,
            "status=%d body=%s" % (status, csv_hint),
        )
        missing_hint = PROJECT_ROOT / "resources" / "task54-does-not-exist.jsonl"
        status, _, missing_body = http_json(
            port,
            "GET",
            "/api/data/hint?path=%s" % urllib.parse.quote(str(missing_hint)),
            token=token,
        )
        checker.check(
            "F17 missing file -> ok:false with named path",
            status == 200
            and (missing_body or {}).get("ok") is False
            and "task54-does-not-exist" in str((missing_body or {}).get("error")),
            "status=%d body=%s" % (status, missing_body),
        )

        # -- F15 early stop: endpoint + pure state machine -----------------
        status, _, early = http_json(
            port,
            "POST",
            "/api/jobs/%s/early_stop" % star_job_id,
            {"enabled": True, "patience": 2, "min_delta": 0.01},
            token=token,
        )
        checker.check(
            "F15 early_stop endpoint stores explicit config",
            status == 200
            and ((early or {}).get("early_stop") or {}).get("enabled") is True
            and ((early or {}).get("early_stop") or {}).get("patience") == 2,
            "status=%d body=%s" % (status, early),
        )
        state = {"best": None, "stale": 0}
        triggered = False
        reason = ""
        for loss in (1.0, 1.001, 1.0005):
            state, triggered, reason = core.early_stop_step(state, loss, 2, 0.01)
        checker.check(
            "F15 stale epochs trigger early stop",
            triggered is True and state.get("stale") == 2 and bool(reason),
            "state=%s triggered=%s reason=%s" % (state, triggered, reason),
        )
        state2 = {"best": None, "stale": 0}
        triggered2 = False
        for loss in (1.0, 0.9, 0.8):
            state2, triggered2, _ = core.early_stop_step(state2, loss, 2, 0.01)
        checker.check(
            "F15 negative control: real improvement does not trigger",
            triggered2 is False and state2.get("stale") == 0,
            "state=%s triggered=%s" % (state2, triggered2),
        )

        status, _, _ = http_json(port, "POST", "/api/jobs", {"kind": "fake"}, token="wrong")
        checker.check(
            "POST /api/jobs with wrong token -> 401", status == 401, "status=%d" % status
        )
        status, _, _ = http_json(port, "GET", "/env/doctor")
        checker.check(
            "GET /env/doctor without token -> 401", status == 401, "status=%d" % status
        )
        status, _, doctor = http_json(port, "GET", "/env/doctor", token=token)
        doctor = doctor or {}
        checker.check(
            "GET /env/doctor -> 200 with python/torch/cuda/disk/ports",
            status == 200
            and doctor.get("ok") is True
            and str(doctor.get("python", {}).get("version", "")).startswith("3.12")
            and "found" in doctor.get("torch", {})
            and doctor.get("cuda", {}).get("status") in ("ok", "unknown")
            and float(doctor.get("disk", {}).get("free_gb", 0)) > 0
            and str(port) in doctor.get("ports", {}),
            "status=%d python=%s torch_found=%s torch_version=%s cuda=%s free_gb=%s"
            % (
                status,
                doctor.get("python", {}).get("version"),
                doctor.get("torch", {}).get("found"),
                doctor.get("torch", {}).get("version"),
                doctor.get("cuda", {}).get("status"),
                doctor.get("disk", {}).get("free_gb"),
            ),
        )
        checker.check(
            "doctor reports the current port as listening and is_core",
            (doctor.get("ports", {}).get(str(port), {}) or {}).get("listening") is True
            and (doctor.get("ports", {}).get(str(port), {}) or {}).get("is_core") is True,
            "ports=%s" % json.dumps(doctor.get("ports", {}), ensure_ascii=False),
        )

        wal_mode = db_connect(data_dir).execute("PRAGMA journal_mode").fetchone()[0]
        checker.check(
            "SQLite journal_mode=wal",
            str(wal_mode).lower() == "wal",
            "journal_mode=%s db=%s" % (wal_mode, data_dir / "core.db"),
        )

        listeners = netstat_listeners(port)
        listener_text = "; ".join(row["raw"] for row in listeners)
        checker.check(
            "netstat: exactly one listener, on 127.0.0.1, owned by server pid",
            len(listeners) >= 1
            and all(row["local"].startswith("127.0.0.1:") for row in listeners)
            and all(row["pid"] == str(proc.pid) for row in listeners),
            "listeners=%d [%s] pid=%s" % (len(listeners), listener_text, proc.pid),
        )
        checker.check(
            "no 0.0.0.0 / [::] wildcard listener on this port",
            not any(
                row["local"].startswith(("0.0.0.0:", "[::]:", "*:")) for row in listeners
            ),
            "locals=%s" % [row["local"] for row in listeners],
        )
        checker.check(
            "negative control: 127.0.0.2:port is refused (would connect if bound to 0.0.0.0)",
            not can_connect("127.0.0.2", port),
            "127.0.0.2:%d reachable=%s" % (port, can_connect("127.0.0.2", port)),
        )
        lan_addresses = lan_ipv4_addresses()
        lan_open = [address for address in lan_addresses if can_connect(address, port)]
        checker.check(
            "LAN IPv4 addresses cannot reach the core",
            not lan_open,
            "tested=%s open=%s" % (lan_addresses or ["<none>"], lan_open),
        )

        # -- job A: idempotency + success + log tailing ---------------------
        idem_key = "selftest-idem-%d" % int(time.time() * 1000)
        job_a_payload = {
            "kind": "fake",
            "params": {"steps": 8, "interval_s": 0.15},
            "idempotency_key": idem_key,
        }
        status, _, created = submit(port, token, job_a_payload)
        created = created or {}
        job_a = created.get("job_id")
        checker.check(
            "POST /api/jobs (fake) -> 202 queued queue_position=1",
            status == 202
            and created.get("state") == "queued"
            and created.get("queue_position") == 1
            and bool(job_a),
            "status=%d job_id=%s state=%s queue_position=%s"
            % (status, job_a, created.get("state"), created.get("queue_position")),
        )
        status, _, replay = submit(port, token, job_a_payload)
        replay = replay or {}
        checker.check(
            "idempotency_key replay returns the same job id (200)",
            status == 200
            and replay.get("job_id") == job_a
            and replay.get("idempotent_replay") is True,
            "status=%d job_id=%s replay=%s"
            % (status, replay.get("job_id"), replay.get("idempotent_replay")),
        )
        checker.check(
            "idempotency_key stored once in SQLite",
            db_count(
                data_dir,
                "SELECT COUNT(*) FROM jobs WHERE idempotency_key=?",
                (idem_key,),
            )
            == 1,
            "rows=%d key=%s"
            % (
                db_count(
                    data_dir,
                    "SELECT COUNT(*) FROM jobs WHERE idempotency_key=?",
                    (idem_key,),
                ),
                idem_key,
            ),
        )

        offsets = []
        log_lines = []
        job_a_state = None
        deadline = time.time() + 30
        while time.time() < deadline:
            status, _, logs = http_json(
                port,
                "GET",
                "/api/jobs/%s/logs?offset=0&max_lines=50" % job_a,
                token=token,
            )
            if status == 200 and isinstance(logs, dict):
                offsets.append(int(logs.get("next_offset", 0)))
                log_lines.extend(logs.get("lines", []))
            job_a_state = get_job(port, token, job_a) or {}
            if job_a_state.get("state") in ("succeeded", "failed", "cancelled", "interrupted"):
                break
            time.sleep(0.12)
        increases = sum(
            1 for index in range(1, len(offsets)) if offsets[index] > offsets[index - 1]
        )
        checker.check(
            "log byte offset advanced at least twice while job ran",
            increases >= 2,
            "polls=%d offset_increases=%d last_offset=%s"
            % (len(offsets), increases, offsets[-1] if offsets else 0),
        )
        checker.check(
            "log tail contains step lines",
            any("step" in line for line in log_lines),
            "lines_seen=%d first=%s"
            % (len(log_lines), log_lines[0][:80] if log_lines else None),
        )

        job_a_state = wait_job(
            port, token, job_a, ("succeeded", "failed", "cancelled", "interrupted"), 30
        ) or {}
        metrics_a = job_a_state.get("metrics") or {}
        checker.check(
            "fake job A reached succeeded",
            job_a_state.get("state") == "succeeded",
            "state=%s exit_code=%s error=%s"
            % (job_a_state.get("state"), job_a_state.get("exit_code"), job_a_state.get("error")),
        )
        checker.check(
            "job A metrics step=8/8 and progress 100",
            metrics_a.get("step") == 8
            and metrics_a.get("total") == 8
            and metrics_a.get("percent") == 100.0,
            "metrics=%s" % json.dumps(metrics_a, ensure_ascii=False),
        )
        log_size_a = int(job_a_state.get("log_size", 0))
        log_path_a = Path(str(job_a_state.get("log_path") or "")).resolve()
        logs_root = (data_dir / "logs").resolve()
        checker.check(
            "job A log file exists under data_dir/logs with bytes > 100",
            log_size_a > 100 and log_path_a.is_relative_to(logs_root),
            "log_path=%s bytes=%d logs_root=%s"
            % (log_path_a, log_size_a, logs_root),
        )
        row_a = db_connect(data_dir).execute("SELECT * FROM jobs WHERE id=?", (job_a,)).fetchone()
        checker.check(
            "DB row for job A: succeeded, exit_code=0, metrics_json parses",
            row_a is not None
            and row_a["state"] == "succeeded"
            and row_a["exit_code"] == 0
            and isinstance(json.loads(row_a["metrics_json"]), dict),
            "state=%s exit_code=%s metrics_bytes=%s"
            % (
                row_a["state"] if row_a else None,
                row_a["exit_code"] if row_a else None,
                len(row_a["metrics_json"] or "") if row_a else 0,
            ),
        )
        status, _, logs_limited = http_json(
            port,
            "GET",
            "/api/jobs/%s/logs?offset=0&max_lines=2" % job_a,
            token=token,
        )
        logs_limited = logs_limited or {}
        checker.check(
            "log tail honors max_lines=2",
            status == 200
            and len(logs_limited.get("lines", [])) <= 2
            and logs_limited.get("size", 0) == log_size_a,
            "status=%d lines=%d size=%s"
            % (status, len(logs_limited.get("lines", [])), logs_limited.get("size")),
        )
        status, _, _ = http_json(
            port, "POST", "/api/jobs/%s/cancel" % job_a, token=token
        )
        checker.check(
            "cancel of a finished job -> 409 conflict", status == 409, "status=%d" % status
        )

        # -- jobs B + C: serial queue, GPU lock, cancellation --------------
        status, _, job_b_created = submit(
            port,
            token,
            {"kind": "fake", "params": {"steps": 300, "interval_s": 0.05}},
        )
        job_b = (job_b_created or {}).get("job_id")
        checker.check(
            "long fake job B accepted (202)", status == 202 and bool(job_b), "job_id=%s" % job_b
        )
        status, _, job_c_created = submit(
            port, token, {"kind": "fake", "params": {"steps": 3, "interval_s": 0.05}}
        )
        job_c = (job_c_created or {}).get("job_id")
        checker.check(
            "second job C accepted while B queued/running (202)",
            status == 202 and bool(job_c),
            "job_id=%s" % job_c,
        )
        job_b_running = wait_job(port, token, job_b, ("running",), 20) or {}
        checker.check(
            "job B reached running",
            job_b_running.get("state") == "running",
            "state=%s pid=%s" % (job_b_running.get("state"), job_b_running.get("pid")),
        )
        time.sleep(2.2)
        hb_job = get_job(port, token, job_b) or {}
        hb_age = hb_job.get("heartbeat_age_s")
        checker.check(
            "running job heartbeat_age_s is fresh (<=3s)",
            isinstance(hb_age, int) and 0 <= hb_age <= 3,
            "state=%s heartbeat_age_s=%s"
            % (hb_job.get("state"), hb_age),
        )
        job_c_queued = get_job(port, token, job_c) or {}
        checker.check(
            "job C is queued with queue_position=1 while B runs",
            job_c_queued.get("state") == "queued"
            and job_c_queued.get("queue_position") == 1,
            "state=%s queue_position=%s"
            % (job_c_queued.get("state"), job_c_queued.get("queue_position")),
        )

        # a job cancelled while it is still queued must never get a pid
        status, _, queued_cancel_created = submit(
            port, token, {"kind": "fake", "params": {"steps": 2, "interval_s": 0.05}}
        )
        queued_cancel_id = (queued_cancel_created or {}).get("job_id")
        status, _, queued_cancel_body = http_json(
            port, "POST", "/api/jobs/%s/cancel" % queued_cancel_id, token=token
        )
        queued_cancel_body = queued_cancel_body or {}
        queued_cancel_job = get_job(port, token, queued_cancel_id) or {}
        checker.check(
            "cancel of a queued job -> 200 and state=cancelled",
            status == 200
            and queued_cancel_body.get("state") == "cancelled"
            and queued_cancel_job.get("state") == "cancelled",
            "status=%d state=%s" % (status, queued_cancel_body.get("state")),
        )
        checker.check(
            "queued-cancelled job never got a pid",
            queued_cancel_job.get("pid") is None,
            "job_id=%s pid=%s" % (queued_cancel_id, queued_cancel_job.get("pid")),
        )
        connection = db_connect(data_dir)
        running_samples = []
        for _ in range(10):
            running_samples.append(
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM jobs WHERE state='running'"
                    ).fetchone()[0]
                )
            )
            time.sleep(0.08)
        connection.close()
        checker.check(
            "two concurrent submissions: DB running count is exactly 1",
            max(running_samples) == 1 and 1 in running_samples,
            "samples=%s" % running_samples,
        )

        lock_path = data_dir / "locks" / "gpu-0.lock"
        lock_size = core.file_size(lock_path)
        lock_read_error = None
        lock_text_while_running = ""
        if lock_path.is_file():
            try:
                lock_text_while_running = lock_path.read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError as exc:
                lock_read_error = type(exc).__name__
        checker.check(
            "GPU lock file exists and is non-empty while job B runs",
            lock_path.is_file() and lock_size > 0,
            "path=%s bytes=%d" % (lock_path, lock_size),
        )
        checker.check(
            "GPU lock file is held (byte-range lock denies reads or owner visible)",
            lock_read_error is not None
            or ("job=" + str(job_b)) in lock_text_while_running,
            "read_error=%s content=%r"
            % (lock_read_error, lock_text_while_running.strip()[:160]),
        )
        lock = core.GpuLock(lock_path)
        acquired_while_running = lock.acquire(owner="selftest-negative-control")
        checker.check(
            "GPU lock is exclusive across processes (selftest cannot acquire it)",
            not acquired_while_running,
            "acquired_while_B_running=%s" % acquired_while_running,
        )
        if acquired_while_running:
            lock.release()

        pid_b = job_b_running.get("pid")
        status, _, cancel_body = http_json(
            port, "POST", "/api/jobs/%s/cancel" % job_b, token=token
        )
        cancel_body = cancel_body or {}
        checker.check(
            "POST cancel on running job B -> 200 with cancel_requested",
            status == 200 and cancel_body.get("cancel_requested") is True,
            "status=%d state=%s cancel_requested=%s mode=%s"
            % (
                status,
                cancel_body.get("state"),
                cancel_body.get("cancel_requested"),
                cancel_body.get("mode"),
            ),
        )
        job_b_final = wait_job(
            port,
            token,
            job_b,
            ("cancelled", "failed", "succeeded", "interrupted"),
            20,
        ) or {}
        checker.check(
            "job B reached cancelled after soft stop",
            job_b_final.get("state") == "cancelled",
            "state=%s error=%s"
            % (job_b_final.get("state"), job_b_final.get("error")),
        )
        checker.check(
            "job B worker pid is dead after cancel",
            bool(pid_b) and not core.is_pid_alive(pid_b),
            "pid=%s alive=%s" % (pid_b, core.is_pid_alive(pid_b)),
        )
        log_b_text = ""
        if job_b_final.get("log_path"):
            try:
                log_b_text = Path(job_b_final["log_path"]).read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                log_b_text = ""
        checker.check(
            "soft stop was recorded in B's log before any hard kill",
            "cancel detected" in log_b_text,
            "log_bytes=%d contains_cancel=%s"
            % (len(log_b_text.encode("utf-8")), "cancel detected" in log_b_text),
        )

        job_c_final = wait_job(
            port, token, job_c, ("succeeded", "failed", "cancelled", "interrupted"), 30
        ) or {}
        metrics_c = job_c_final.get("metrics") or {}
        checker.check(
            "queued job C started after B was cancelled and succeeded",
            job_c_final.get("state") == "succeeded"
            and metrics_c.get("step") in (2, 3),
            "state=%s step=%s error=%s"
            % (job_c_final.get("state"), metrics_c.get("step"), job_c_final.get("error")),
        )
        checker.check(
            "DB has no running jobs after B/C",
            db_count(data_dir, "SELECT COUNT(*) FROM jobs WHERE state='running'") == 0,
            "running_rows=%d"
            % db_count(data_dir, "SELECT COUNT(*) FROM jobs WHERE state='running'"),
        )
        lock_after_text = ""
        try:
            lock_after_text = lock_path.read_text(
                encoding="utf-8", errors="replace"
            ).strip()
        except OSError as exc:
            lock_after_text = "<read failed: %s>" % type(exc).__name__
        checker.check(
            "released lock file keeps the last worker owner (job C, pid)",
            ("job=" + str(job_c)) in lock_after_text and "pid=" in lock_after_text,
            "content=%r" % lock_after_text[:160],
        )
        lock = core.GpuLock(lock_path)
        acquired_after = lock.acquire(owner="selftest-after")
        checker.check(
            "GPU lock is released after jobs finish",
            acquired_after,
            "acquired_after_jobs=%s" % acquired_after,
        )
        lock.release()

        # -- default fake parameters: 20 steps, 1.0 s interval -------------
        status, _, default_created = submit(port, token, {"kind": "fake"})
        default_id = (default_created or {}).get("job_id")
        checker.check(
            "fake job with no params accepted (defaults path)",
            status == 202 and bool(default_id),
            "status=%d job_id=%s" % (status, default_id),
        )
        default_running = wait_job(port, token, default_id, ("running",), 15) or {}
        checker.check(
            "default fake job reached running",
            default_running.get("state") == "running",
            "state=%s pid=%s"
            % (default_running.get("state"), default_running.get("pid")),
        )
        default_total = None
        default_log_text = ""
        deadline = time.time() + 10
        while time.time() < deadline:
            status, _, default_logs = http_json(
                port,
                "GET",
                "/api/jobs/%s/logs?offset=0&max_lines=50" % default_id,
                token=token,
            )
            if status == 200 and isinstance(default_logs, dict):
                default_log_text = "\n".join(default_logs.get("lines", []))
            current = get_job(port, token, default_id) or {}
            metrics = current.get("metrics") or {}
            if metrics.get("total") is not None:
                default_total = metrics.get("total")
            if default_total == 20 and "interval=1.0s" in default_log_text:
                break
            time.sleep(0.1)
        checker.check(
            "fake job defaults are 20 steps at 1.0 s interval",
            default_total == 20 and "interval=1.0s" in default_log_text,
            "metrics.total=%s log_has_interval=%s"
            % (default_total, "interval=1.0s" in default_log_text),
        )
        status, _, _ = http_json(
            port, "POST", "/api/jobs/%s/cancel" % default_id, token=token
        )
        default_final = wait_job(
            port,
            token,
            default_id,
            ("cancelled", "failed", "succeeded", "interrupted"),
            15,
        ) or {}
        checker.check(
            "default fake job can be cancelled early",
            default_final.get("state") == "cancelled",
            "status=%d state=%s" % (status, default_final.get("state")),
        )

        # -- hard-kill path: worker ignores the stop file on purpose ---------
        status, _, hard_created = submit(
            port,
            token,
            {
                "kind": "fake",
                "params": {
                    "steps": 2,
                    "interval_s": 60,
                    "ignore_soft_stop": True,
                },
            },
        )
        hard_id = (hard_created or {}).get("job_id")
        hard_running = wait_job(port, token, hard_id, ("running",), 15) or {}
        hard_pid = hard_running.get("pid")
        checker.check(
            "slow fake job reached running (hard-kill fixture)",
            hard_running.get("state") == "running" and bool(hard_pid),
            "job_id=%s pid=%s" % (hard_id, hard_pid),
        )
        status, _, _ = http_json(
            port, "POST", "/api/jobs/%s/cancel" % hard_id, token=token
        )
        cancel_started = time.monotonic()
        hard_final = wait_job(
            port, token, hard_id, ("cancelled", "failed", "succeeded", "interrupted"), 60
        ) or {}
        hard_log = ""
        hard_log_path = hard_final.get("log_path")
        deadline = time.time() + 5
        while time.time() < deadline:
            if hard_log_path:
                try:
                    hard_log = Path(hard_log_path).read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    hard_log = ""
            if "taskkill" in hard_log:
                break
            time.sleep(0.1)
        deadline = time.time() + 5
        while hard_pid and core.is_pid_alive(hard_pid) and time.time() < deadline:
            time.sleep(0.1)
        hard_elapsed = time.monotonic() - cancel_started
        checker.check(
            "cancel escalates to taskkill when soft stop cannot be observed",
            hard_final.get("state") == "cancelled"
            and "ignore_soft_stop=1" in hard_log
            and "taskkill" in hard_log
            and bool(hard_pid)
            and not core.is_pid_alive(hard_pid),
            "status=%d state=%s elapsed_s=%.1f pid_alive=%s "
            "log_has_ignore=%s log_has_taskkill=%s"
            % (
                status,
                hard_final.get("state"),
                hard_elapsed,
                core.is_pid_alive(hard_pid) if hard_pid else None,
                "ignore_soft_stop=1" in hard_log,
                "taskkill" in hard_log,
            ),
        )

        # -- Laya route: stub CLI, no GPU -----------------------------------
        laya_data = data_dir / "laya-train.jsonl"
        laya_eval = data_dir / "laya-eval.jsonl"
        laya_out = data_dir / "laya-out"
        laya_data.write_text('{"text":"a","label":"x"}\n', encoding="utf-8")
        laya_eval.write_text('{"text":"b","label":"x"}\n', encoding="utf-8")
        laya_out.mkdir(parents=True, exist_ok=True)
        laya_stub_command = [sys.executable, str(LAYA_STUB)]
        laya_params = {
            "data": str(laya_data),
            "out": str(laya_out),
            "base": "convaiinnovations/laya",
            "loss": "rlcd",
            "epochs": 4,
            "micro_batch": 8,
            "grad_accum": 8,
            "encoder_lr": 2.5e-5,
            "head_lr": 1e-4,
            "max_len": 512,
            "head_max_len": 192,
            "label_smoothing": 0.0,
            "target_error": 0.10,
            "min_abstain_n": 10,
            "eval_data": str(laya_eval),
            "train_command": laya_stub_command + ["--stub-delay", "0.02"],
        }
        status, _, laya_created = submit(
            port, token, {"kind": "laya", "params": laya_params}
        )
        laya_created = laya_created or {}
        laya_id = laya_created.get("job_id")
        checker.check(
            "laya job accepted into the queue (202)",
            status == 202 and bool(laya_id),
            "status=%d job_id=%s" % (status, laya_id),
        )
        laya_final = wait_job(
            port,
            token,
            laya_id,
            ("succeeded", "failed", "cancelled", "interrupted"),
            30,
        ) or {}
        laya_metrics = laya_final.get("metrics") or {}
        checker.check(
            "laya stub job reached succeeded",
            laya_final.get("state") == "succeeded",
            "state=%s error=%s"
            % (laya_final.get("state"), laya_final.get("error")),
        )
        checker.check(
            "laya metrics keep updates_total=352 and micro_step=100 separate",
            laya_metrics.get("updates_total") == 352
            and laya_metrics.get("micro_step") == 100
            and laya_metrics.get("updates_total") != laya_metrics.get("micro_step")
            and laya_metrics.get("phase") == "finished",
            "updates_total=%s micro_step=%s phase=%s"
            % (
                laya_metrics.get("updates_total"),
                laya_metrics.get("micro_step"),
                laya_metrics.get("phase"),
            ),
        )
        checker.check(
            "laya metrics parse epoch/loss/evaluation from stdout",
            laya_metrics.get("epoch") == 4
            and laya_metrics.get("loss") is not None
            and laya_metrics.get("evaluation") == "held_out",
            "epoch=%s loss=%s evaluation=%s"
            % (
                laya_metrics.get("epoch"),
                laya_metrics.get("loss"),
                laya_metrics.get("evaluation"),
            ),
        )
        laya_log_path = Path(laya_final.get("log_path") or "")
        laya_log_text = ""
        try:
            laya_log_text = laya_log_path.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            laya_log_text = ""
        checker.check(
            "laya log persisted and contains real stdout lines",
            "optimizer budget:" in laya_log_text
            and "step 100 loss" in laya_log_text
            and "mean loss" in laya_log_text
            and len(laya_log_text.encode("utf-8")) > 500,
            "log_path=%s bytes=%d"
            % (laya_log_path, len(laya_log_text.encode("utf-8"))),
        )

        # cancel a running Laya stub job
        cancel_params = dict(laya_params)
        cancel_params["train_command"] = laya_stub_command + [
            "--stub-delay",
            "0.2",
        ]
        status, _, laya_cancel_created = submit(
            port, token, {"kind": "laya", "params": cancel_params}
        )
        laya_cancel_id = (laya_cancel_created or {}).get("job_id")
        laya_cancel_running = wait_job(
            port, token, laya_cancel_id, ("running",), 15
        ) or {}
        cancel_log_path = Path(laya_cancel_running.get("log_path") or "")
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                if cancel_log_path.is_file() and "step 100" in cancel_log_path.read_text(
                    encoding="utf-8", errors="replace"
                ):
                    break
            except OSError:
                pass
            time.sleep(0.05)
        status, _, _ = http_json(
            port, "POST", "/api/jobs/%s/cancel" % laya_cancel_id, token=token
        )
        laya_cancel_final = wait_job(
            port,
            token,
            laya_cancel_id,
            ("cancelled", "failed", "succeeded", "interrupted"),
            30,
        ) or {}
        cancel_metrics = laya_cancel_final.get("metrics") or {}
        cancel_log_text = ""
        try:
            cancel_log_text = Path(
                laya_cancel_final.get("log_path") or ""
            ).read_text(encoding="utf-8", errors="replace")
        except OSError:
            cancel_log_text = ""
        checker.check(
            "laya cancel reaches cancelled via adapter stop",
            status == 200
            and laya_cancel_final.get("state") == "cancelled"
            and cancel_metrics.get("state") == "cancelled"
            and "cancel" in cancel_log_text.lower(),
            "status=%d state=%s metrics_state=%s log_has_cancel=%s"
            % (
                status,
                laya_cancel_final.get("state"),
                cancel_metrics.get("state"),
                "cancel" in cancel_log_text.lower(),
            ),
        )

        # missing required data/out fails with a named error
        status, _, laya_missing_created = submit(
            port,
            token,
            {
                "kind": "laya",
                "params": {"train_command": laya_stub_command},
            },
        )
        laya_missing_final = wait_job(
            port,
            token,
            (laya_missing_created or {}).get("job_id"),
            ("failed", "cancelled", "succeeded", "interrupted"),
            15,
        ) or {}
        missing_error = str(laya_missing_final.get("error") or "")
        checker.check(
            "laya missing data/out fails and names both fields",
            status == 202
            and laya_missing_final.get("state") == "failed"
            and "missing required param(s)" in missing_error
            and "data" in missing_error
            and "out" in missing_error,
            "status=%d state=%s error=%s"
            % (status, laya_missing_final.get("state"), missing_error),
        )

        # -- LLM route: stub CLI, no GPU ------------------------------------
        llm_data = data_dir / "llm-train.jsonl"
        llm_data.write_text('{"instruction":"x","response":"y"}\n', encoding="utf-8")
        llm_out = data_dir / "llm-out"
        llm_out.mkdir(parents=True, exist_ok=True)
        llm_stub_command = [sys.executable, str(LLM_STUB)]
        llm_params = {
            "model": str(data_dir / "Qwen2.5-0.5B-Instruct"),
            "data": str(llm_data),
            "out": str(llm_out),
            "max_steps": 20,
            "epochs": 1,
            "rank": 8,
            "alpha": 16,
            "lr": 5e-4,
            "max_len": 512,
            "batch": 1,
            "device": "cpu",
            "template": "qwen",
            "assert_updated": True,
            "train_command": llm_stub_command + ["--stub-delay", "0.01"],
        }
        status, _, llm_created = submit(
            port, token, {"kind": "llm", "params": llm_params}
        )
        llm_id = (llm_created or {}).get("job_id")
        checker.check(
            "llm job accepted into the queue (202)",
            status == 202 and bool(llm_id),
            "status=%d job_id=%s" % (status, llm_id),
        )
        llm_final = wait_job(
            port,
            token,
            llm_id,
            ("succeeded", "failed", "cancelled", "interrupted"),
            30,
        ) or {}
        llm_metrics = llm_final.get("metrics") or {}
        checker.check(
            "llm stub job reached succeeded",
            llm_final.get("state") == "succeeded",
            "state=%s error=%s"
            % (llm_final.get("state"), llm_final.get("error")),
        )
        checker.check(
            "llm metrics have step/loss/lr and report fields",
            llm_metrics.get("step") == 20
            and llm_metrics.get("loss") is not None
            and llm_metrics.get("lr") is not None
            and llm_metrics.get("report_loaded") is True
            and len(llm_metrics.get("loss_series") or []) == 20
            and llm_metrics.get("train_elapsed_s") is not None
            and llm_metrics.get("peak_vram_alloc_mib") == 3449.5
            and llm_metrics.get("report_status") == "ok",
            "step=%s loss=%s lr=%s report=%s series=%s elapsed=%s peak=%s"
            % (
                llm_metrics.get("step"),
                llm_metrics.get("loss"),
                llm_metrics.get("lr"),
                llm_metrics.get("report_loaded"),
                len(llm_metrics.get("loss_series") or []),
                llm_metrics.get("train_elapsed_s"),
                llm_metrics.get("peak_vram_alloc_mib"),
            ),
        )
        llm_log_text = ""
        try:
            llm_log_text = Path(llm_final.get("log_path") or "").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            llm_log_text = ""
        checker.check(
            "llm log persisted with step/report/done lines",
            "step 1 loss" in llm_log_text
            and "report:" in llm_log_text
            and "TRAIN_OK" in llm_log_text,
            "log_path=%s bytes=%d"
            % (llm_final.get("log_path"), len(llm_log_text.encode("utf-8"))),
        )

        # cancel a running LLM stub job
        llm_cancel_params = dict(llm_params)
        llm_cancel_params["train_command"] = llm_stub_command + [
            "--stub-delay",
            "0.2",
        ]
        status, _, llm_cancel_created = submit(
            port, token, {"kind": "llm", "params": llm_cancel_params}
        )
        llm_cancel_id = (llm_cancel_created or {}).get("job_id")
        llm_cancel_running = wait_job(
            port, token, llm_cancel_id, ("running",), 15
        ) or {}
        llm_cancel_log = Path(llm_cancel_running.get("log_path") or "")
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                if llm_cancel_log.is_file() and "step 1 loss" in llm_cancel_log.read_text(
                    encoding="utf-8", errors="replace"
                ):
                    break
            except OSError:
                pass
            time.sleep(0.05)
        status, _, _ = http_json(
            port, "POST", "/api/jobs/%s/cancel" % llm_cancel_id, token=token
        )
        llm_cancel_final = wait_job(
            port,
            token,
            llm_cancel_id,
            ("cancelled", "failed", "succeeded", "interrupted"),
            30,
        ) or {}
        llm_cancel_metrics = llm_cancel_final.get("metrics") or {}
        llm_cancel_text = ""
        try:
            llm_cancel_text = Path(
                llm_cancel_final.get("log_path") or ""
            ).read_text(encoding="utf-8", errors="replace")
        except OSError:
            llm_cancel_text = ""
        checker.check(
            "llm cancel reaches cancelled via stop/signal",
            status == 200
            and llm_cancel_final.get("state") == "cancelled"
            and llm_cancel_metrics.get("state") == "cancelled"
            and "cancel" in llm_cancel_text.lower(),
            "status=%d state=%s metrics_state=%s log_has_cancel=%s"
            % (
                status,
                llm_cancel_final.get("state"),
                llm_cancel_metrics.get("state"),
                "cancel" in llm_cancel_text.lower(),
            ),
        )

        # missing model preflight
        status, _, llm_missing_created = submit(
            port,
            token,
            {
                "kind": "llm",
                "params": {
                    "data": str(llm_data),
                    "out": str(llm_out),
                    "train_command": llm_stub_command,
                },
            },
        )
        llm_missing_final = wait_job(
            port,
            token,
            (llm_missing_created or {}).get("job_id"),
            ("failed", "cancelled", "succeeded", "interrupted"),
            15,
        ) or {}
        llm_missing_error = str(llm_missing_final.get("error") or "")
        checker.check(
            "llm missing model fails and names model",
            status == 202
            and llm_missing_final.get("state") == "failed"
            and "missing required param(s)" in llm_missing_error
            and "model" in llm_missing_error,
            "status=%d state=%s error=%s"
            % (status, llm_missing_final.get("state"), llm_missing_error),
        )

        # -- ai-toolkit route: stub CLI, no GPU -----------------------------
        aitk_dataset = data_dir / "aitk-images"
        aitk_dataset.mkdir(parents=True, exist_ok=True)
        aitk_out = data_dir / "aitk-out"
        aitk_out.mkdir(parents=True, exist_ok=True)
        aitk_stub_command = [sys.executable, str(AITK_STUB), "--stub-delay", "0.005"]
        aitk_params = {
            "model": "black-forest-labs/FLUX.1-dev",
            "dataset": str(aitk_dataset),
            "out": str(aitk_out),
            "arch": "flux",
            "steps": 1200,
            "batch": 1,
            "lr": 1e-4,
            "rank": 8,
            "alpha": 16,
            "resolution": [512, 768],
            "grad_accum": 2,
            "save_every": 500,
            "keep": 3,
            "sample_every": 100,
            "sample_start": 0,
            "sample_steps": 20,
            "guidance": 7,
            "sample_width": 512,
            "sample_height": 512,
            "num_frames": 1,
            "trigger_word": "sks",
            "caption_ext": "txt",
            "sample_prompts": ["a photo of sks person", "a portrait"],
            "sampler": "flowmatch",
            "noise_scheduler": "flowmatch",
            "save_format": "safetensors",
            "train_command": aitk_stub_command,
        }
        status, _, aitk_created = submit(
            port, token, {"kind": "aitk", "params": aitk_params}
        )
        aitk_id = (aitk_created or {}).get("job_id")
        checker.check(
            "aitk job accepted into the queue (202)",
            status == 202 and bool(aitk_id),
            "status=%d job_id=%s" % (status, aitk_id),
        )
        aitk_final = wait_job(
            port,
            token,
            aitk_id,
            ("succeeded", "failed", "cancelled", "interrupted"),
            30,
        ) or {}
        aitk_metrics = aitk_final.get("metrics") or {}
        checker.check(
            "aitk stub job reached succeeded with 13 sample groups",
            aitk_final.get("state") == "succeeded"
            and aitk_metrics.get("step") == 1200
            and aitk_metrics.get("total") == 1200
            and aitk_metrics.get("sample_groups") == 13
            and aitk_metrics.get("expected_sample_groups") == 13
            and aitk_metrics.get("last_sample_step") == 1200
            and aitk_metrics.get("warnings") == [],
            "state=%s step=%s total=%s groups=%s expected=%s last=%s warnings=%s"
            % (
                aitk_final.get("state"),
                aitk_metrics.get("step"),
                aitk_metrics.get("total"),
                aitk_metrics.get("sample_groups"),
                aitk_metrics.get("expected_sample_groups"),
                aitk_metrics.get("last_sample_step"),
                aitk_metrics.get("warnings"),
            ),
        )
        aitk_log_text = ""
        try:
            aitk_log_text = Path(aitk_final.get("log_path") or "").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            aitk_log_text = ""
        checker.check(
            "aitk log persisted with baseline/sample/save lines",
            "Generating baseline samples" in aitk_log_text
            and ("Sampling at step 100" in aitk_log_text or "Sample at step 100" in aitk_log_text)
            and "Saving at step 500" in aitk_log_text,
            "log_path=%s bytes=%d"
            % (aitk_final.get("log_path"), len(aitk_log_text.encode("utf-8"))),
        )

        # non-divisible final step warning
        aitk_nodiv_params = dict(aitk_params, steps=1250)
        status, _, aitk_nodiv_created = submit(
            port, token, {"kind": "aitk", "params": aitk_nodiv_params}
        )
        aitk_nodiv_final = wait_job(
            port,
            token,
            (aitk_nodiv_created or {}).get("job_id"),
            ("succeeded", "failed", "cancelled", "interrupted"),
            30,
        ) or {}
        aitk_nodiv_metrics = aitk_nodiv_final.get("metrics") or {}
        aitk_nodiv_warnings = " ".join(
            str(item) for item in (aitk_nodiv_metrics.get("warnings") or [])
        )
        checker.check(
            "aitk steps=1250 keeps 13 groups and warns 末步不整除",
            status == 202
            and aitk_nodiv_final.get("state") == "succeeded"
            and aitk_nodiv_metrics.get("sample_groups") == 13
            and aitk_nodiv_metrics.get("last_sample_step") == 1200
            and "末步不整除" in aitk_nodiv_warnings,
            "status=%d state=%s groups=%s last=%s warnings=%s"
            % (
                status,
                aitk_nodiv_final.get("state"),
                aitk_nodiv_metrics.get("sample_groups"),
                aitk_nodiv_metrics.get("last_sample_step"),
                aitk_nodiv_metrics.get("warnings"),
            ),
        )

        # cancel a running aitk stub job
        aitk_cancel_params = dict(aitk_params)
        aitk_cancel_params["train_command"] = [
            sys.executable,
            str(AITK_STUB),
            "--stub-delay",
            "0.2",
        ]
        status, _, aitk_cancel_created = submit(
            port, token, {"kind": "aitk", "params": aitk_cancel_params}
        )
        aitk_cancel_id = (aitk_cancel_created or {}).get("job_id")
        aitk_cancel_running = wait_job(
            port, token, aitk_cancel_id, ("running",), 15
        ) or {}
        aitk_cancel_log = Path(aitk_cancel_running.get("log_path") or "")
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                if aitk_cancel_log.is_file() and "Generating baseline samples" in aitk_cancel_log.read_text(
                    encoding="utf-8", errors="replace"
                ):
                    break
            except OSError:
                pass
            time.sleep(0.05)
        status, _, _ = http_json(
            port, "POST", "/api/jobs/%s/cancel" % aitk_cancel_id, token=token
        )
        aitk_cancel_final = wait_job(
            port,
            token,
            aitk_cancel_id,
            ("cancelled", "failed", "succeeded", "interrupted"),
            30,
        ) or {}
        aitk_cancel_metrics = aitk_cancel_final.get("metrics") or {}
        aitk_cancel_text = ""
        try:
            aitk_cancel_text = Path(
                aitk_cancel_final.get("log_path") or ""
            ).read_text(encoding="utf-8", errors="replace")
        except OSError:
            aitk_cancel_text = ""
        checker.check(
            "aitk cancel reaches cancelled via stop/signal",
            status == 200
            and aitk_cancel_final.get("state") == "cancelled"
            and aitk_cancel_metrics.get("state") == "cancelled"
            and "cancel" in aitk_cancel_text.lower(),
            "status=%d state=%s metrics_state=%s log_has_cancel=%s"
            % (
                status,
                aitk_cancel_final.get("state"),
                aitk_cancel_metrics.get("state"),
                "cancel" in aitk_cancel_text.lower(),
            ),
        )

        # missing required param
        status, _, aitk_missing_created = submit(
            port,
            token,
            {
                "kind": "aitk",
                "params": {
                    "dataset": str(aitk_dataset),
                    "out": str(aitk_out),
                    "arch": "flux",
                    "train_command": aitk_stub_command,
                },
            },
        )
        aitk_missing_final = wait_job(
            port,
            token,
            (aitk_missing_created or {}).get("job_id"),
            ("failed", "cancelled", "succeeded", "interrupted"),
            15,
        ) or {}
        aitk_missing_error = str(aitk_missing_final.get("error") or "")
        checker.check(
            "aitk missing model fails and names model",
            status == 202
            and aitk_missing_final.get("state") == "failed"
            and "missing required param(s)" in aitk_missing_error
            and "model" in aitk_missing_error,
            "status=%d state=%s error=%s"
            % (status, aitk_missing_final.get("state"), aitk_missing_error),
        )

        # no venv: must fail with the explicit installation message, no fallback
        aitk_noenv_params = dict(aitk_params)
        aitk_noenv_params.pop("train_command", None)
        status, _, aitk_noenv_created = submit(
            port, token, {"kind": "aitk", "params": aitk_noenv_params}
        )
        aitk_noenv_final = wait_job(
            port,
            token,
            (aitk_noenv_created or {}).get("job_id"),
            ("failed", "cancelled", "succeeded", "interrupted"),
            15,
        ) or {}
        aitk_noenv_error = str(aitk_noenv_final.get("error") or "")
        checker.check(
            "aitk missing environment fails with named missing path",
            status == 202
            and aitk_noenv_final.get("state") == "failed"
            and "ai-toolkit environment missing" in aitk_noenv_error
            and "aitk-libs" in aitk_noenv_error,
            "status=%d state=%s error=%s"
            % (status, aitk_noenv_final.get("state"), aitk_noenv_error),
        )

        # -- DiffSynth route: stub train.py, no GPU -------------------------
        diffsynth_model = data_dir / "Qwen-Image-2.1"
        for subdir, count in (("transformer", 2), ("text_encoder", 4), ("vae", 1)):
            sub = diffsynth_model / subdir
            sub.mkdir(parents=True, exist_ok=True)
            for index in range(1, count + 1):
                (sub / ("%s-%05d.safetensors" % (subdir, index))).write_bytes(
                    b"stub"
                )
        (diffsynth_model / "processor").mkdir(parents=True, exist_ok=True)
        diffsynth_dataset = data_dir / "diffsynth-images"
        diffsynth_dataset.mkdir(parents=True, exist_ok=True)
        diffsynth_metadata = diffsynth_dataset / "metadata.csv"
        diffsynth_metadata.write_text("image,prompt\n0.png,a photo\n", encoding="utf-8")
        (diffsynth_dataset / "0.txt").write_text("local caption", encoding="utf-8")
        diffsynth_out = data_dir / "diffsynth-out"
        diffsynth_out.mkdir(parents=True, exist_ok=True)
        bootstrap_sample = diffsynth_out / "samples" / "step-2" / "a.png"
        bootstrap_sample.parent.mkdir(parents=True, exist_ok=True)
        bootstrap_sample.write_bytes(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            )
        )
        future = time.time() + 3600
        os.utime(bootstrap_sample, (future, future))
        diffsynth_stub_command = [
            sys.executable,
            str(DIFFSYNTH_STUB),
            "--stub-delay",
            "0.005",
        ]
        diffsynth_params = {
            "model": str(diffsynth_model),
            "dataset": str(diffsynth_dataset),
            "metadata": str(diffsynth_metadata),
            "processor": str(diffsynth_model / "processor"),
            "out": str(diffsynth_out),
            "rank": 8,
            "epochs": 1,
            "lr": 1e-4,
            "height": 512,
            "width": 512,
            "dataset_repeat": 1,
            "dataset_image_exts": ["png", "jpg"],
            "dataset_default_caption": "a photo of te_man man, portrait",
            "sample_steps": 8,
            "sample_guidance": 2.5,
            "sample_seed": 7,
            "max_pixels": 262144,
            "enable_model_cpu_offload": True,
            "train_command": diffsynth_stub_command,
        }
        status, _, diffsynth_created = submit(
            port, token, {"kind": "diffsynth", "params": diffsynth_params}
        )
        diffsynth_id = (diffsynth_created or {}).get("job_id")
        checker.check(
            "diffsynth job accepted into the queue (202)",
            status == 202 and bool(diffsynth_id),
            "status=%d job_id=%s" % (status, diffsynth_id),
        )
        diffsynth_job = get_job(port, token, diffsynth_id) or {}
        checker.check(
            "diffsynth qwen_image_21 gets empty default python_paths (arch-aware)",
            str(fake_diffsynth_libs)
            not in list((diffsynth_job.get("params") or {}).get("python_paths") or [])
            and list((diffsynth_job.get("params") or {}).get("python_paths") or []) == [],
            "python_paths=%s"
            % (diffsynth_job.get("params") or {}).get("python_paths"),
        )
        diffsynth_job_params = diffsynth_job.get("params") or {}
        checker.check(
            "diffsynth dataset_image_exts/default_caption pass through",
            diffsynth_job_params.get("dataset_image_exts") == ["png", "jpg"]
            and diffsynth_job_params.get("dataset_default_caption")
            == "a photo of te_man man, portrait"
            and diffsynth_job_params.get("dataset_metadata_hint") == "local_txt"
            and diffsynth_job_params.get("sample_steps") == 8
            and diffsynth_job_params.get("sample_guidance") == 2.5
            and diffsynth_job_params.get("sample_seed") == 7
            and diffsynth_job_params.get("max_pixels") == 262144
            and diffsynth_job_params.get("enable_model_cpu_offload") is True,
            "image_exts=%s default_caption=%s sample=%s max_pixels=%s offload=%s"
            % (
                diffsynth_job_params.get("dataset_image_exts"),
                diffsynth_job_params.get("dataset_default_caption"),
                (
                    diffsynth_job_params.get("sample_steps"),
                    diffsynth_job_params.get("sample_guidance"),
                    diffsynth_job_params.get("sample_seed"),
                ),
                diffsynth_job_params.get("max_pixels"),
                diffsynth_job_params.get("enable_model_cpu_offload"),
            ),
        )
        bad_dataset_params = dict(diffsynth_params)
        bad_dataset_params["dataset_image_exts"] = 123
        status, _, _ = http_json(
            port,
            "POST",
            "/api/jobs",
            {"kind": "diffsynth", "params": bad_dataset_params},
            token=token,
        )
        checker.check(
            "diffsynth bad dataset_image_exts type -> 400 named",
            status == 400,
            "status=%d" % status,
        )
        bad_footprint = dict(diffsynth_params)
        bad_footprint["max_pixels"] = 100
        status, _, _ = http_json(
            port,
            "POST",
            "/api/jobs",
            {"kind": "diffsynth", "params": bad_footprint},
            token=token,
        )
        checker.check(
            "diffsynth max_pixels=100 rejected with 400 named",
            status == 400,
            "status=%d" % status,
        )
        bad_sample_params = dict(diffsynth_params)
        bad_sample_params["sample_steps"] = 0
        status, _, _ = http_json(
            port,
            "POST",
            "/api/jobs",
            {"kind": "diffsynth", "params": bad_sample_params},
            token=token,
        )
        checker.check(
            "diffsynth sample_steps=0 rejected with 400 named",
            status == 400,
            "status=%d" % status,
        )
        diffsynth_final = wait_job(
            port,
            token,
            diffsynth_id,
            ("succeeded", "failed", "cancelled", "interrupted"),
            30,
        ) or {}
        diffsynth_metrics = diffsynth_final.get("metrics") or {}
        checker.check(
            "diffsynth stub job succeeded with step/artifact metrics",
            diffsynth_final.get("state") == "succeeded"
            and diffsynth_metrics.get("step") == 4
            and diffsynth_metrics.get("total") == 4
            and diffsynth_metrics.get("sec_per_it") == 3.88
            and str(diffsynth_metrics.get("artifact") or "").endswith(
                "epoch-0.safetensors"
            ),
            "state=%s step=%s total=%s sec_per_it=%s artifact=%s"
            % (
                diffsynth_final.get("state"),
                diffsynth_metrics.get("step"),
                diffsynth_metrics.get("total"),
                diffsynth_metrics.get("sec_per_it"),
                diffsynth_metrics.get("artifact"),
            ),
        )
        checker.check(
            "diffsynth adapter sample_events merged into job metrics",
            bool(diffsynth_metrics.get("sample_events"))
            and any(
                item.get("file") == "a.png"
                and "step-2" in str(item.get("path") or "")
                for event in (diffsynth_metrics.get("sample_events") or [])
                for item in (event.get("files") or [])
                if isinstance(event, dict) and isinstance(item, dict)
            ),
            "sample_events=%s" % (diffsynth_metrics.get("sample_events")),
        )
        bad_arch_params = dict(diffsynth_params)
        bad_arch_params["arch"] = "nope_image"
        status, _, _ = http_json(
            port,
            "POST",
            "/api/jobs",
            {"kind": "diffsynth", "params": bad_arch_params},
            token=token,
        )
        checker.check(
            "diffsynth unknown arch -> 400 named",
            status == 400,
            "status=%d" % status,
        )
        resume_ckpt = data_dir / "rank8-lora.safetensors"
        resume_header = {
            "__metadata__": {"arch": "z_image", "rank": "8"},
            "transformer.lora_A.weight": {
                "dtype": "F16",
                "shape": [8, 4],
                "data_offsets": [0, 64],
            },
            "transformer.lora_B.weight": {
                "dtype": "F16",
                "shape": [4, 8],
                "data_offsets": [64, 128],
            },
        }
        raw_header = json.dumps(resume_header, separators=(",", ":")).encode("utf-8")
        resume_ckpt.write_bytes(struct.pack("<Q", len(raw_header)) + raw_header + b"\0" * 128)
        resume_params = dict(diffsynth_params)
        resume_params.update(
            {
                "arch": "z_image",
                "lora_rank": 32,
                "resume_from": str(resume_ckpt),
                "out": str(data_dir / "resume-negative-out"),
            }
        )
        status, _, resume_created = http_json(
            port, "POST", "/api/jobs", {"kind": "diffsynth", "params": resume_params}, token=token
        )
        resume_job = wait_job(
            port,
            token,
            (resume_created or {}).get("job_id"),
            ("failed", "succeeded", "cancelled", "interrupted"),
            30,
        ) or {}
        resume_error = str(resume_job.get("error") or "")
        checker.check(
            "resume rank mismatch fails preflight with expected/actual rank",
            status == 202
            and resume_job.get("state") == "failed"
            and "rank" in resume_error
            and "期望 32" in resume_error
            and "实到 8" in resume_error,
            "status=%d state=%s error=%s"
            % (status, resume_job.get("state"), resume_error),
        )
        cleanup_resume = db_connect(data_dir)
        try:
            cleanup_resume.execute(
                "DELETE FROM jobs WHERE id=?",
                ((resume_created or {}).get("job_id"),),
            )
            cleanup_resume.commit()
        finally:
            cleanup_resume.close()

        def run_caption_case(label, txt_count):
            dataset = data_dir / ("caption-%s" % label)
            dataset.mkdir(parents=True, exist_ok=True)
            for index in range(2):
                (dataset / ("%d.png" % index)).write_bytes(
                    base64.b64decode(
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
                    )
                )
                if index < txt_count:
                    (dataset / ("%d.txt" % index)).write_text(
                        "a useful caption", encoding="utf-8"
                    )
            out = data_dir / ("caption-out-%s" % label)
            params = dict(diffsynth_params)
            params.pop("metadata", None)
            params["dataset"] = str(dataset)
            params["out"] = str(out)
            params["train_command"] = diffsynth_stub_command
            status, _, created = submit(
                port, token, {"kind": "diffsynth", "params": params}
            )
            final = wait_job(
                port,
                token,
                (created or {}).get("job_id"),
                ("succeeded", "failed", "cancelled", "interrupted"),
                30,
            ) or {}
            metrics = final.get("metrics") or {}
            return status, final, metrics

        status, final, metrics = run_caption_case("none", 0)
        warnings = list(metrics.get("warnings") or [])
        log_text = ""
        try:
            log_text = Path(final.get("log_path") or "").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            pass
        checker.check(
            "diffsynth 0 captions -> no_captions warning + log line",
            status == 202
            and final.get("state") == "succeeded"
            and any(item.startswith("no_captions:") for item in warnings)
            and "# WARNING: no captions" in log_text,
            "warnings=%s log_has_warning=%s"
            % (warnings, "# WARNING: no captions" in log_text),
        )
        status, final, metrics = run_caption_case("partial", 1)
        warnings = list(metrics.get("warnings") or [])
        checker.check(
            "diffsynth partial captions -> partial_captions 1/2",
            final.get("state") == "succeeded"
            and any(item.startswith("partial_captions: 1/2") for item in warnings),
            "warnings=%s" % warnings,
        )
        status, final, metrics = run_caption_case("full", 2)
        warnings = list(metrics.get("warnings") or [])
        checker.check(
            "diffsynth all captions -> no caption warnings",
            final.get("state") == "succeeded"
            and not any(
                item.startswith("no_captions:") or item.startswith("partial_captions:")
                for item in warnings
            ),
            "warnings=%s" % warnings,
        )
        diffsynth_log_text = ""
        try:
            diffsynth_log_text = Path(
                diffsynth_final.get("log_path") or ""
            ).read_text(encoding="utf-8", errors="replace")
        except OSError:
            diffsynth_log_text = ""
        checker.check(
            "diffsynth log contains tqdm and artifact lines",
            "4/4 [" in diffsynth_log_text
            and "s/it" in diffsynth_log_text
            and "epoch-0.safetensors" in diffsynth_log_text,
            "log_path=%s bytes=%d"
            % (
                diffsynth_final.get("log_path"),
                len(diffsynth_log_text.encode("utf-8")),
            ),
        )
        checker.check(
            "diffsynth footprint knobs reach adapter argv",
            '"--max_pixels", "262144"' in diffsynth_log_text
            and '"--enable_model_cpu_offload"' in diffsynth_log_text,
            "max_pixels=%s offload=%s"
            % (
                '"--max_pixels", "262144"' in diffsynth_log_text,
                '"--enable_model_cpu_offload"' in diffsynth_log_text,
            ),
        )
        diffsynth_cancel_params = dict(diffsynth_params)
        diffsynth_cancel_params["train_command"] = [
            sys.executable,
            str(DIFFSYNTH_STUB),
            "--stub-delay",
            "0.2",
        ]
        status, _, diffsynth_cancel_created = submit(
            port, token, {"kind": "diffsynth", "params": diffsynth_cancel_params}
        )
        diffsynth_cancel_id = (diffsynth_cancel_created or {}).get("job_id")
        diffsynth_cancel_running = wait_job(
            port, token, diffsynth_cancel_id, ("running",), 15
        ) or {}
        diffsynth_cancel_log = Path(
            diffsynth_cancel_running.get("log_path") or ""
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                if diffsynth_cancel_log.is_file() and "s/it" in diffsynth_cancel_log.read_text(
                    encoding="utf-8", errors="replace"
                ):
                    break
            except OSError:
                pass
            time.sleep(0.05)
        status, _, _ = http_json(
            port, "POST", "/api/jobs/%s/cancel" % diffsynth_cancel_id, token=token
        )
        diffsynth_cancel_final = wait_job(
            port,
            token,
            diffsynth_cancel_id,
            ("cancelled", "failed", "succeeded", "interrupted"),
            30,
        ) or {}
        diffsynth_cancel_metrics = diffsynth_cancel_final.get("metrics") or {}
        diffsynth_cancel_text = ""
        try:
            diffsynth_cancel_text = Path(
                diffsynth_cancel_final.get("log_path") or ""
            ).read_text(encoding="utf-8", errors="replace")
        except OSError:
            diffsynth_cancel_text = ""
        checker.check(
            "diffsynth cancel reaches cancelled via stop/signal",
            status == 200
            and diffsynth_cancel_final.get("state") == "cancelled"
            and diffsynth_cancel_metrics.get("state") == "cancelled"
            and "cancel" in diffsynth_cancel_text.lower(),
            "status=%d state=%s metrics_state=%s log_has_cancel=%s"
            % (
                status,
                diffsynth_cancel_final.get("state"),
                diffsynth_cancel_metrics.get("state"),
                "cancel" in diffsynth_cancel_text.lower(),
            ),
        )

        # missing model preflight
        status, _, diffsynth_missing_created = submit(
            port,
            token,
            {
                "kind": "diffsynth",
                "params": {
                    "dataset": str(diffsynth_dataset),
                    "out": str(diffsynth_out),
                    "train_command": diffsynth_stub_command,
                },
            },
        )
        diffsynth_missing_final = wait_job(
            port,
            token,
            (diffsynth_missing_created or {}).get("job_id"),
            ("failed", "cancelled", "succeeded", "interrupted"),
            15,
        ) or {}
        diffsynth_missing_error = str(diffsynth_missing_final.get("error") or "")
        checker.check(
            "diffsynth missing model fails and names model",
            status == 202
            and diffsynth_missing_final.get("state") == "failed"
            and "missing required param(s)" in diffsynth_missing_error
            and "model" in diffsynth_missing_error,
            "status=%d state=%s error=%s"
            % (
                status,
                diffsynth_missing_final.get("state"),
                diffsynth_missing_error,
            ),
        )

        # -- sample/artifact static hosting --------------------------------
        png_1x1 = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        sample_dir = diffsynth_out / "samples" / "step-2"
        sample_dir.mkdir(parents=True, exist_ok=True)
        sample_file = sample_dir / "a.png"
        sample_file.write_bytes(png_1x1)
        status, headers, sample_body = http_json(
            port,
            "GET",
            "/api/jobs/%s/samples/step-2/a.png" % diffsynth_id,
            raw=True,
        )
        checker.check(
            "samples static: 1x1 PNG 200 image/png bytes equal",
            status == 200
            and "image/png" in headers.get("Content-Type", "")
            and sample_body == png_1x1,
            "status=%d content-type=%s bytes=%d"
            % (status, headers.get("Content-Type"), len(sample_body or b"")),
        )
        status, _, _ = http_json(
            port,
            "GET",
            "/api/jobs/%s/samples/../core.db" % diffsynth_id,
        )
        checker.check(
            "samples static: ../core.db traversal -> 403/404",
            status in (403, 404),
            "status=%d" % status,
        )
        status, _, _ = http_json(
            port,
            "GET",
            "/api/jobs/%s/samples/step-2/missing.png" % diffsynth_id,
        )
        checker.check("samples static: missing file -> 404", status == 404, "status=%d" % status)
        status, _, artifacts_payload = http_json(
            port,
            "GET",
            "/api/jobs/%s/artifacts" % diffsynth_id,
            token=token,
        )
        artifact_names = [
            item.get("name") for item in ((artifacts_payload or {}).get("artifacts") or [])
        ]
        checker.check(
            "artifacts listing includes epoch-0.safetensors and samples/step-2/a.png",
            status == 200
            and "epoch-0.safetensors" in artifact_names
            and "samples/step-2/a.png" in artifact_names,
            "status=%d names=%s" % (status, artifact_names),
        )

        # -- F23/F24/F25/F26 eval metrics replay + deterministic report -----
        eval_metrics = {
            "accuracy": 0.75,
            "loss": 0.42,
            "ece": 0.08,
            "brier_multi": 0.18,
            "brier_top1": 0.22,
            "mean_conf": 0.81,
            "abstain_rate": 0.05,
        }
        train_report = {
            "n": 12,
            "metrics": eval_metrics,
            "confusion": [{"label": "refund", "support": 5, "correct": 4}],
            "errors": [
                {
                    "q": "重复扣款怎么办",
                    "answer": "refund",
                    "expected": "escalate",
                    "confidence": 0.61,
                }
            ],
        }
        (diffsynth_out / "train_report.json").write_text(
            json.dumps(train_report, ensure_ascii=False), encoding="utf-8"
        )
        (diffsynth_out / "questions.json").write_text(
            json.dumps([{"q": "重复扣款怎么办"}], ensure_ascii=False),
            encoding="utf-8",
        )
        status, _, eval_cal = http_json(
            port,
            "POST",
            "/api/jobs/%s/eval" % diffsynth_id,
            {"data": "", "limit": 96},
            token=token,
        )
        eval_cal = eval_cal or {}
        checker.check(
            "F23 calibration eval returns metrics + per_question=[] + note",
            status == 200
            and eval_cal.get("ok") is True
            and eval_cal.get("data_source") == "calibration"
            and eval_cal.get("n") == 12
            and eval_cal.get("per_question") == []
            and "未落逐条预测" in str(eval_cal.get("note")),
            "status=%d body=%s" % (status, eval_cal),
        )
        heldout_sample = (
            PROJECT_ROOT
            / "skills"
            / "laya-corpus"
            / "examples"
            / "customer-service-3rows.jsonl"
        )
        status, _, eval_held = http_json(
            port,
            "POST",
            "/api/jobs/%s/eval" % diffsynth_id,
            {"data": str(heldout_sample), "limit": 96, "mode": "replay"},
            token=token,
        )
        checker.check(
            "F23 heldout eval is honest (no fabricated per_question)",
            status == 200
            and (eval_held or {}).get("data_source") == "heldout"
            and (eval_held or {}).get("per_question") == []
            and "未落逐条预测" in str((eval_held or {}).get("note")),
            "status=%d body=%s" % (status, eval_held),
        )
        status, _, eval_cli = http_json(
            port,
            "POST",
            "/api/jobs/%s/eval" % diffsynth_id,
            {"data": str(heldout_sample), "limit": 96, "mode": "cli"},
            token=token,
        )
        checker.check(
            "F23 eval mode=cli uses CLI predictions (1 per_question)",
            status == 200
            and (eval_cli or {}).get("mode") == "cli"
            and len((eval_cli or {}).get("per_question") or []) == 1
            and (eval_cli or {}).get("data_source") == "heldout",
            "status=%d body=%s" % (status, eval_cli),
        )
        status, _, eval_cli_fail = http_json(
            port,
            "POST",
            "/api/jobs/%s/eval" % diffsynth_id,
            {"data": str(fail_eval_data), "limit": 96, "mode": "cli"},
            token=token,
        )
        checker.check(
            "F23 eval CLI exit1 -> replay + warn=eval_cli_failed",
            status == 200
            and (eval_cli_fail or {}).get("mode") == "replay"
            and (eval_cli_fail or {}).get("warn") == "eval_cli_failed"
            and (eval_cli_fail or {}).get("per_question") == [],
            "status=%d body=%s" % (status, eval_cli_fail),
        )
        status, _, report = http_json(
            port,
            "POST",
            "/api/jobs/%s/report" % diffsynth_id,
            {"language": "zh", "template": "laya-eval-v1"},
            token=token,
        )
        report = report or {}
        markdown = str(report.get("markdown") or "")
        checker.check(
            "F24/F25 template report has four sections + in-sample caveat",
            status == 200
            and report.get("engine") == "template"
            and all(
                heading in markdown
                for heading in ("## 总体", "## 错例", "## 归因", "## 建议")
            )
            and "in-sample ≠ 泛化" in markdown,
            "status=%d engine=%s headings=%s warn=%s"
            % (
                status,
                report.get("engine"),
                [h for h in ("## 总体", "## 错例", "## 归因", "## 建议") if h in markdown],
                report.get("warn"),
            ),
        )
        checker.check(
            "F25 LLM unavailable falls back to template with warn=llm_failed",
            status == 200
            and report.get("engine") == "template"
            and report.get("warn") == "llm_failed"
            and bool(report.get("llm_error")),
            "engine=%s warn=%s llm_error=%s"
            % (report.get("engine"), report.get("warn"), report.get("llm_error")),
        )
        ungrounded_negative = core.grounded_numbers(
            "声称 accuracy=0.1234", {"accuracy": 0.75, "loss": 0.42}
        )
        checker.check(
            "F26 grounding rejects 0.1234 not present in metrics",
            ungrounded_negative == ["0.1234"],
            "ungrounded=%s" % ungrounded_negative,
        )
        status, _, eval_missing = http_json(
            port,
            "POST",
            "/api/jobs/%s/eval" % star_job_id,
            {"data": ""},
            token=token,
        )
        checker.check(
            "F23 job without output dir -> ok:false named",
            status == 200 and (eval_missing or {}).get("ok") is False,
            "status=%d body=%s" % (status, eval_missing),
        )
        long_job_status, _, long_job = submit(
            port, token, {"kind": "fake", "params": {"steps": 200, "interval_s": 0.05}}
        )
        long_job_id = (long_job or {}).get("job_id")
        wait_job(port, token, long_job_id, ("running",), 15)
        status, _, eval_running = http_json(
            port,
            "POST",
            "/api/jobs/%s/eval" % long_job_id,
            {"data": ""},
            token=token,
        )
        checker.check(
            "F23 non-terminal job -> 409",
            status == 409,
            "status=%d body=%s" % (status, eval_running),
        )
        http_json(port, "POST", "/api/jobs/%s/cancel" % long_job_id, token=token)

        # -- strict UTF-8 metrics + bad metrics -> params.out fallback -------
        utf8_metrics_path = data_dir / "metrics-utf8.json"
        utf8_metrics_path.write_text(
            json.dumps(
                {"state": "running", "output_dir": str(diffsynth_out)},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        strict_metrics = core.Scheduler._read_metrics(utf8_metrics_path)
        checker.check(
            "metrics reader strict UTF-8 preserves Chinese output_dir (no U+FFFD)",
            isinstance(strict_metrics, dict)
            and strict_metrics.get("output_dir") == str(diffsynth_out)
            and "\ufffd" not in strict_metrics.get("output_dir", ""),
            "output_dir=%r" % (strict_metrics or {}).get("output_dir"),
        )
        connection = db_connect(data_dir)
        try:
            good_params = {
                "out": str(diffsynth_out),
                "model": str(data_dir / "fake-model"),
                "data": str(data_dir / "fake-data.jsonl"),
                "template": "qwen",
            }
            bad_metrics = {"state": "succeeded", "output_dir": "E:\\\ufffd\\broken\\run"}
            connection.execute(
                "UPDATE jobs SET metrics_json=?, params_json=? WHERE id=?",
                (
                    json.dumps(bad_metrics, ensure_ascii=False),
                    json.dumps(good_params, ensure_ascii=False),
                    star_job_id,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        status, _, fallback_eval = http_json(
            port,
            "POST",
            "/api/jobs/%s/eval" % star_job_id,
            {"data": ""},
            token=token,
        )
        checker.check(
            "F23 bad metrics + good params.out -> output_dir_source=params",
            status == 200
            and (fallback_eval or {}).get("ok") is True
            and (fallback_eval or {}).get("output_dir_source") == "params"
            and any("U+FFFD" in item for item in ((fallback_eval or {}).get("warnings") or [])),
            "status=%d body=%s" % (status, fallback_eval),
        )
        connection = db_connect(data_dir)
        try:
            bad_params = dict(good_params)
            bad_params["out"] = "E:\\\ufffd\\broken\\run"
            connection.execute(
                "UPDATE jobs SET params_json=? WHERE id=?",
                (json.dumps(bad_params, ensure_ascii=False), star_job_id),
            )
            connection.commit()
        finally:
            connection.close()
        status, _, both_bad = http_json(
            port,
            "POST",
            "/api/jobs/%s/eval" % star_job_id,
            {"data": ""},
            token=token,
        )
        checker.check(
            "F23 bad metrics + bad params.out -> ok:false named",
            status == 200
            and (both_bad or {}).get("ok") is False
            and "usable output_dir" in str((both_bad or {}).get("error")),
            "status=%d body=%s" % (status, both_bad),
        )
        real_laya_dir = PROJECT_ROOT / "_runs" / "ui-laya-20261022-190622"
        if (real_laya_dir / "train_report.json").is_file():
            connection = db_connect(data_dir)
            try:
                real_params = dict(good_params)
                real_params["out"] = str(real_laya_dir)
                connection.execute(
                    "UPDATE jobs SET metrics_json=?, params_json=? WHERE id=?",
                    (
                        json.dumps(bad_metrics, ensure_ascii=False),
                        json.dumps(real_params, ensure_ascii=False),
                        star_job_id,
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            status, _, real_eval = http_json(
                port,
                "POST",
                "/api/jobs/%s/eval" % star_job_id,
                {"data": ""},
                token=token,
            )
            real_eval = real_eval or {}
            checker.check(
                "F23 real laya run dir -> eval ok, accuracy 0.8542, source=params",
                status == 200
                and real_eval.get("ok") is True
                and real_eval.get("output_dir_source") == "params"
                and abs(float((real_eval.get("metrics") or {}).get("accuracy", 0)) - 0.8542)
                < 1e-9,
                "status=%d body=%s" % (status, real_eval),
            )

        # artifacts must stat; never infer from params.out
        empty_artifacts = data_dir / "empty-artifact-out"
        empty_artifacts.mkdir(parents=True, exist_ok=True)
        params_artifact_out = data_dir / "params-only-artifact-out"
        params_artifact_out.mkdir(parents=True, exist_ok=True)
        (params_artifact_out / "epoch-0.safetensors").write_bytes(b"real")
        connection = db_connect(data_dir)
        try:
            fake_metrics = {
                "state": "succeeded",
                "output_dir": str(empty_artifacts),
                "artifacts": [str(empty_artifacts / "epoch-0.safetensors")],
                "artifact": str(empty_artifacts / "epoch-0.safetensors"),
            }
            fake_params = {"out": str(params_artifact_out), "model": "x", "data": "x", "template": "qwen"}
            connection.execute(
                "UPDATE jobs SET metrics_json=?, params_json=? WHERE id=?",
                (
                    json.dumps(fake_metrics, ensure_ascii=False),
                    json.dumps(fake_params, ensure_ascii=False),
                    star_job_id,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        status, _, fake_job = http_json(
            port, "GET", "/api/jobs/%s" % star_job_id, token=token
        )
        fake_job = fake_job or {}
        fake_metrics_resp = fake_job.get("job", {}).get("metrics") or {}
        status2, _, fake_artifacts = http_json(
            port, "GET", "/api/jobs/%s/artifacts" % star_job_id, token=token
        )
        checker.check(
            "artifacts non-existent path is filtered; params.out is not used",
            status == 200
            and status2 == 200
            and fake_metrics_resp.get("artifacts") == []
            and "artifact" not in fake_metrics_resp
            and (fake_artifacts or {}).get("artifacts") == [],
            "job_artifacts=%s endpoint=%s"
            % (fake_metrics_resp.get("artifacts"), fake_artifacts),
        )

        # -- LlamaFactory route (kind="lf"): stub child, no GPU -------------
        lf_model = data_dir / "Qwen2.5-0.5B-Instruct"
        lf_model.mkdir(parents=True, exist_ok=True)
        (lf_model / "config.json").write_text("{}", encoding="utf-8")
        lf_data = data_dir / "lf-dolly.jsonl"
        with open(lf_data, "w", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "instruction": "Q1",
                        "context": "ctx one",
                        "response": "A1",
                        "category": "closed_qa",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            fh.write(
                json.dumps(
                    {
                        "instruction": "Q2",
                        "context": "",
                        "response": "A2",
                        "category": "open_qa",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        lf_out = data_dir / "lf-out"
        lf_out.mkdir(parents=True, exist_ok=True)
        lf_stub_command = [sys.executable, str(LF_STUB), "--steps", "2", "--delay", "0.005"]
        lf_params = {
            "model": str(lf_model),
            "data": str(lf_data),
            "out": str(lf_out),
            "template": "qwen",
            "rank": 8,
            "alpha": 16,
            "lr": 2e-4,
            "epochs": 3,
            "max_steps": 2,
            "batch": 1,
            "grad_accum": 1,
            "cutoff_len": 512,
            "logging_steps": 1,
            "save_steps": 0,
            "train_command": lf_stub_command,
        }
        status, _, lf_created = submit(port, token, {"kind": "lf", "params": lf_params})
        lf_id = (lf_created or {}).get("job_id")
        checker.check(
            "lf job accepted into the queue (202)",
            status == 202 and bool(lf_id),
            "status=%d job_id=%s" % (status, lf_id),
        )
        lf_final = wait_job(
            port,
            token,
            lf_id,
            ("succeeded", "failed", "cancelled", "interrupted"),
            30,
        ) or {}
        lf_metrics = lf_final.get("metrics") or {}
        lf_peak = lf_metrics.get("peak_vram")
        checker.check(
            "lf stub job succeeded with step/total/loss/lr/peak_vram/jsonl_path",
            lf_final.get("state") == "succeeded"
            and lf_metrics.get("step") == 2
            and lf_metrics.get("total") == 2
            and abs(float(lf_metrics.get("loss") or 0.0) - 1.5) < 1e-9
            and float(lf_metrics.get("lr") or 0.0) == 2e-4
            and isinstance(lf_peak, dict)
            and float(lf_peak.get("allocated_gib") or 0.0) == 1.2
            and float(lf_peak.get("reserved_gib") or 0.0) == 1.4
            and str(lf_metrics.get("jsonl_path") or "").endswith("trainer_log.jsonl")
            and Path(lf_metrics.get("jsonl_path") or "").is_file()
            and str(lf_metrics.get("artifact") or "").endswith(
                "adapter_model.safetensors"
            )
            and lf_metrics.get("returncode") == 0,
            "state=%s step=%s total=%s loss=%s lr=%s peak=%s jsonl=%s artifact=%s"
            % (
                lf_final.get("state"),
                lf_metrics.get("step"),
                lf_metrics.get("total"),
                lf_metrics.get("loss"),
                lf_metrics.get("lr"),
                json.dumps(lf_peak, ensure_ascii=False),
                lf_metrics.get("jsonl_path"),
                lf_metrics.get("artifact"),
            ),
        )
        lf_log_text = ""
        try:
            lf_log_text = Path(lf_final.get("log_path") or "").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            lf_log_text = ""
        checker.check(
            "lf log contains child argv, loss dicts and TRAIN_OK",
            "lf child argv=" in lf_log_text
            and "{'loss':" in lf_log_text
            and "TRAIN_OK" in lf_log_text
            and "core: lf final state=succeeded" in lf_log_text,
            "log_path=%s bytes=%d"
            % (
                lf_final.get("log_path"),
                len(lf_log_text.encode("utf-8")),
            ),
        )

        # cancel a running lf stub job
        lf_cancel_out = data_dir / "lf-out-cancel"
        lf_cancel_out.mkdir(parents=True, exist_ok=True)
        lf_cancel_params = dict(lf_params)
        lf_cancel_params["out"] = str(lf_cancel_out)
        lf_cancel_params["train_command"] = [
            sys.executable,
            str(LF_STUB),
            "--steps",
            "200",
            "--delay",
            "0.1",
        ]
        status, _, lf_cancel_created = submit(
            port, token, {"kind": "lf", "params": lf_cancel_params}
        )
        lf_cancel_id = (lf_cancel_created or {}).get("job_id")
        lf_cancel_running = wait_job(port, token, lf_cancel_id, ("running",), 15) or {}
        lf_cancel_log = Path(lf_cancel_running.get("log_path") or "")
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                if lf_cancel_log.is_file() and "{'loss':" in lf_cancel_log.read_text(
                    encoding="utf-8", errors="replace"
                ):
                    break
            except OSError:
                pass
            time.sleep(0.05)
        status, _, _ = http_json(
            port, "POST", "/api/jobs/%s/cancel" % lf_cancel_id, token=token
        )
        lf_cancel_final = wait_job(
            port,
            token,
            lf_cancel_id,
            ("cancelled", "failed", "succeeded", "interrupted"),
            45,
        ) or {}
        lf_cancel_metrics = lf_cancel_final.get("metrics") or {}
        lf_cancel_text = ""
        try:
            lf_cancel_text = Path(lf_cancel_final.get("log_path") or "").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            lf_cancel_text = ""
        checker.check(
            "lf cancel reaches cancelled via stop file + soft signal",
            status == 200
            and lf_cancel_final.get("state") == "cancelled"
            and lf_cancel_metrics.get("state") == "cancelled"
            and "lf cancel requested" in lf_cancel_text,
            "status=%d state=%s metrics_state=%s log_has_cancel=%s"
            % (
                status,
                lf_cancel_final.get("state"),
                lf_cancel_metrics.get("state"),
                "lf cancel requested" in lf_cancel_text,
            ),
        )

        # missing required params are named one by one
        status, _, lf_missing_created = submit(
            port,
            token,
            {"kind": "lf", "params": {"train_command": lf_stub_command}},
        )
        lf_missing_final = wait_job(
            port,
            token,
            (lf_missing_created or {}).get("job_id"),
            ("failed", "cancelled", "succeeded", "interrupted"),
            15,
        ) or {}
        lf_missing_error = str(lf_missing_final.get("error") or "")
        for field in ("model", "data", "out", "template"):
            checker.check(
                "lf missing %s fails and names it" % field,
                status == 202
                and lf_missing_final.get("state") == "failed"
                and "missing required param(s)" in lf_missing_error
                and field in lf_missing_error,
                "state=%s error=%s"
                % (lf_missing_final.get("state"), lf_missing_error),
            )

        # -- LF progress classification: preprocessing bar vs training bar --
        lf_bars_stub = data_dir / "lf_bars_stub.py"
        lf_bars_stub.write_text(
            r'''import os, sys
mode = sys.argv[1] if len(sys.argv) > 1 else "full"
out = os.environ.get("MODELSMITH_LF_OUT") or "."
os.makedirs(out, exist_ok=True)
print("***** Running training *****", flush=True)
print("  Total optimization steps = 2", flush=True)
print("100%|##########| 15011/15011 [00:03<00:00, 5000.00ex/s]", flush=True)
if mode == "full":
    print("100%|##########| 2/2 [00:01<00:00, 15.00it/s]", flush=True)
    print("{'loss': 1.5000, 'learning_rate': 0.0002, 'epoch': 0.5}", flush=True)
    print("{'loss': 1.0000, 'learning_rate': 0.0002, 'epoch': 1.0}", flush=True)
    print("{'train_runtime': 1.0, 'train_loss': 1.25, 'epoch': 1.0}", flush=True)
with open(os.path.join(out, "adapter_model.safetensors"), "wb") as fh:
    fh.write(b"lf-bars-stub\n")
with open(os.path.join(out, "adapter_config.json"), "w", encoding="utf-8") as fh:
    fh.write("{}\n")
''',
            encoding="utf-8",
        )
        lf_bars_out = data_dir / "lf-bars-out"
        lf_bars_out.mkdir(parents=True, exist_ok=True)
        lf_bars_params = dict(lf_params)
        lf_bars_params["out"] = str(lf_bars_out)
        lf_bars_params["train_command"] = [
            sys.executable,
            str(lf_bars_stub),
            "full",
        ]
        status, _, lf_bars_created = submit(
            port, token, {"kind": "lf", "params": lf_bars_params}
        )
        lf_bars_id = (lf_bars_created or {}).get("job_id")
        lf_bars_final = wait_job(
            port,
            token,
            lf_bars_id,
            ("succeeded", "failed", "cancelled", "interrupted"),
            30,
        ) or {}
        lf_bars_metrics = lf_bars_final.get("metrics") or {}
        lf_bars_series = [
            item.get("step") for item in (lf_bars_metrics.get("loss_series") or [])
        ]
        checker.check(
            "lf bars: only training bar sets step=2/total=2; loss series steps [1,2]",
            lf_bars_final.get("state") == "succeeded"
            and lf_bars_metrics.get("step") == 2
            and lf_bars_metrics.get("total") == 2
            and lf_bars_series == [1, 2]
            and set((lf_bars_metrics.get("bars") or {}).keys()) >= {"15011", "2"},
            "state=%s step=%s total=%s series=%s bars=%s"
            % (
                lf_bars_final.get("state"),
                lf_bars_metrics.get("step"),
                lf_bars_metrics.get("total"),
                lf_bars_series,
                lf_bars_metrics.get("bars"),
            ),
        )
        lf_325_metrics = {"max_steps": 0, "total_steps": 2, "bars": {}}
        core.Scheduler._lf_apply_event(
            {
                "type": "progress",
                "bar_kind": "training",
                "step": 325,
                "total": 325,
            },
            lf_325_metrics,
        )
        checker.check(
            "lf tqdm 325/325 wins over stale total_steps=2",
            lf_325_metrics.get("step") == 325
            and lf_325_metrics.get("total") == 325
            and lf_325_metrics.get("total_steps") == 325,
            "metrics=%s" % lf_325_metrics,
        )
        lf_neg_out = data_dir / "lf-bars-negative-out"
        lf_neg_out.mkdir(parents=True, exist_ok=True)
        lf_neg_params = dict(lf_bars_params)
        lf_neg_params["out"] = str(lf_neg_out)
        lf_neg_params["train_command"] = [
            sys.executable,
            str(lf_bars_stub),
            "preprocess",
        ]
        status, _, lf_neg_created = submit(
            port, token, {"kind": "lf", "params": lf_neg_params}
        )
        lf_neg_final = wait_job(
            port,
            token,
            (lf_neg_created or {}).get("job_id"),
            ("succeeded", "failed", "cancelled", "interrupted"),
            30,
        ) or {}
        lf_neg_metrics = lf_neg_final.get("metrics") or {}
        checker.check(
            "lf negative control: preprocessing-only bar never becomes step=15011",
            lf_neg_final.get("state") == "succeeded"
            and lf_neg_metrics.get("step") in (0, None)
            and (lf_neg_metrics.get("bars") or {}).get("15011") == 15011,
            "state=%s step=%s bars=%s"
            % (
                lf_neg_final.get("state"),
                lf_neg_metrics.get("step"),
                lf_neg_metrics.get("bars"),
            ),
        )

        # -- unsupported kind is honest -------------------------------------
        status, _, unsupported = submit(port, token, {"kind": "nope", "params": {}})
        unsupported = unsupported or {}
        checker.check(
            "unsupported kind is accepted into the queue (202)",
            status == 202 and bool(unsupported.get("job_id")),
            "status=%d job_id=%s" % (status, unsupported.get("job_id")),
        )
        unsupported_final = wait_job(
            port,
            token,
            unsupported.get("job_id"),
            ("succeeded", "failed", "cancelled", "interrupted"),
            20,
        ) or {}
        checker.check(
            "unsupported kind fails honestly (no fake success)",
            unsupported_final.get("state") == "failed"
            and "no adapter" in str(unsupported_final.get("error", "")),
            "state=%s error=%s"
            % (unsupported_final.get("state"), unsupported_final.get("error")),
        )

        # -- validation negative controls -----------------------------------
        status, _, _ = http_json(
            port, "POST", "/api/jobs", {"kind": "BAD!"}, token=token
        )
        checker.check("invalid kind rejected with 400", status == 400, "status=%d" % status)
        status, _, _ = http_json(
            port,
            "POST",
            "/api/jobs",
            {"kind": "fake", "params": {"steps": 0}},
            token=token,
        )
        checker.check("steps=0 rejected with 400", status == 400, "status=%d" % status)

        status, _, health_after = http_json(port, "GET", "/health")
        counts = (health_after or {}).get("jobs", {})
        checker.check(
            "/health counts jobs by state after the run",
            status == 200
            and counts.get("succeeded", 0) >= 2
            and counts.get("cancelled", 0) >= 1
            and counts.get("failed", 0) >= 1,
            "counts=%s" % json.dumps(counts, ensure_ascii=False),
        )

        # -- graceful shutdown ----------------------------------------------
        stop_server(port, token, proc, log_handle)
        log_handle = None
        proc = None
        checker.check(
            "POST /admin/shutdown exits the server cleanly",
            True,
            "server log bytes=%d" % (server_log.stat().st_size if server_log.is_file() else 0),
        )
        checker.check(
            "clean shutdown removes stale core.json",
            not (data_dir / "core.json").exists(),
            "core.json_exists=%s" % (data_dir / "core.json").exists(),
        )
        return token

    finally:
        if proc is not None:
            stop_server(locals().get("port", 0), locals().get("token", ""), proc, log_handle)
        fake_root = locals().get("fake_models_root")
        if fake_root is not None:
            for _ in range(20):
                try:
                    shutil.rmtree(fake_root, ignore_errors=False)
                    break
                except (OSError, PermissionError):
                    time.sleep(0.2)


def run_recovery_phase(data_dir: Path, token: str, checker: Checker):
    dead_pid = find_dead_pid()
    connection = db_connect(data_dir)
    now = core.utc_now()
    connection.execute(
        "INSERT INTO jobs (id, kind, state, pid, log_path, metrics_json, created_at, "
        "updated_at, idempotency_key, params_json, cancel_requested, exit_code, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "job-recover-selftest",
            "fake",
            "running",
            dead_pid,
            str(data_dir / "logs" / "recover.log"),
            None,
            now,
            now,
            None,
            "{}",
            0,
            None,
            None,
        ),
    )
    connection.execute(
        "INSERT INTO jobs (id, kind, state, pid, log_path, metrics_json, created_at, "
        "updated_at, idempotency_key, params_json, cancel_requested, exit_code, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "job-recover-caption",
            "caption",
            "running",
            dead_pid,
            str(data_dir / "logs" / "recover-caption.log"),
            None,
            now,
            now,
            None,
            "{}",
            0,
            None,
            None,
        ),
    )
    connection.commit()
    connection.close()
    checker.check(
        "seeded a stale running row with a dead pid for recovery test",
        not core.is_pid_alive(dead_pid),
        "pid=%s alive=%s" % (dead_pid, core.is_pid_alive(dead_pid)),
    )

    proc, log_handle, server_log = start_server(
        data_dir,
        "core-stdout-2.log",
        extra_args=["--lf-python", "Z:\\nope\\python.exe", "--no-vram-check"],
    )
    port = 0
    try:
        config = wait_core_json(data_dir, proc)
        port = int(config["port"])
        checker.check(
            "restart on --port 0 picks a fresh port/pid",
            port > 0 and int(config.get("pid", -1)) == proc.pid,
            "port=%d pid=%s" % (port, config.get("pid")),
        )
        recovered = get_job(port, token, "job-recover-selftest") or {}
        checker.check(
            "stale running job is marked interrupted after restart",
            recovered.get("state") == "interrupted",
            "state=%s error=%s"
            % (recovered.get("state"), recovered.get("error")),
        )
        recovered_caption = get_job(port, token, "job-recover-caption") or {}
        checker.check(
            "stale caption job is marked interrupted after restart",
            recovered_caption.get("state") == "interrupted",
            "state=%s error=%s"
            % (recovered_caption.get("state"), recovered_caption.get("error")),
        )
        log_text = (
            server_log.read_text(encoding="utf-8", errors="replace")
            if server_log.is_file()
            else ""
        )
        checker.check(
            "restart log names the recovered job id",
            "recovered jobs marked interrupted" in log_text
            and "job-recover-selftest" in log_text,
            "server_log_bytes=%d" % len(log_text.encode("utf-8")),
        )
        status, _, health = http_json(port, "GET", "/health")
        checker.check(
            "restarted core still answers /health 200",
            status == 200 and (health or {}).get("pid") == proc.pid,
            "status=%d pid=%s" % (status, (health or {}).get("pid")),
        )
        status, _, lf_bad_env = submit(
            port,
            token,
            {
                "kind": "lf",
                "params": {
                    "model": str(data_dir / "Qwen2.5-0.5B-Instruct"),
                    "data": str(data_dir / "lf-dolly.jsonl"),
                    "out": str(data_dir / "lf-out-badenv"),
                    "template": "qwen",
                },
            },
        )
        lf_bad_env_final = wait_job(
            port,
            token,
            (lf_bad_env or {}).get("job_id"),
            ("failed", "cancelled", "succeeded", "interrupted"),
            15,
        ) or {}
        lf_bad_env_error = str(lf_bad_env_final.get("error") or "")
        checker.check(
            "lf missing environment fails and names the lf-python path",
            status == 202
            and lf_bad_env_final.get("state") == "failed"
            and "LlamaFactory environment missing" in lf_bad_env_error
            and "lf-python=Z:\\nope\\python.exe" in lf_bad_env_error,
            "status=%d state=%s error=%s"
            % (status, lf_bad_env_final.get("state"), lf_bad_env_error),
        )
    finally:
        stop_server(port, token, proc, log_handle)


def summarize(data_dir: Path, checker: Checker) -> None:
    connection = db_connect(data_dir)
    try:
        counts = {
            str(row["state"]): int(row["n"])
            for row in connection.execute(
                "SELECT state, COUNT(*) AS n FROM jobs GROUP BY state"
            ).fetchall()
        }
        rows = int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()
    log_bytes = sum(path.stat().st_size for path in (data_dir / "logs").glob("*.log"))
    token_bytes = (data_dir / "token").stat().st_size if (data_dir / "token").is_file() else 0
    checker.note(
        "DB rows=%d states=%s journal_mode=%s"
        % (rows, json.dumps(counts, ensure_ascii=False), journal_mode)
    )
    checker.note("log bytes total=%d token bytes=%d" % (log_bytes, token_bytes))
    checker.note(
        "PASS %d / FAIL %d" % (checker.passed, checker.failed)
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ModelSmith core selftest")
    parser.add_argument("--keep", action="store_true", help="keep temp data dir")
    parser.add_argument("--verbose", action="store_true", help="no-op (compat)")
    args = parser.parse_args(argv)

    if not CORE.is_file():
        print("FAIL - core file missing: %s" % CORE)
        return 2
    if not UI_BASELINE.is_file():
        print("FAIL - UI baseline missing: %s" % UI_BASELINE)
        return 2
    if not V3_INDEX.is_file():
        print("FAIL - v3 index missing: %s" % V3_INDEX)
        return 2

    temp_root = Path(tempfile.mkdtemp(prefix="modelsmith_core_selftest_"))
    data_dir = temp_root / "中文 状态"
    data_dir.mkdir(parents=True, exist_ok=True)
    checker = Checker()
    token = None
    try:
        token = run_phase1(data_dir, checker)
        if token is None:
            token = (data_dir / "token").read_text(encoding="utf-8").strip()
        run_recovery_phase(data_dir, token, checker)
        test_orphan_reaper(checker)
        summarize(data_dir, checker)
    except Exception:
        checker.check("selftest harness completed without crashing", False, traceback.format_exc())
    finally:
        if checker.failed:
            print("\nFAILED CHECKS:")
            for name in checker.records:
                print("  - " + name)
            print("\nRESULT: PASS %d / FAIL %d" % (checker.passed, checker.failed))
            print("data dir kept for debugging: %s" % data_dir)
            return 1
        if args.keep:
            print("\nRESULT: PASS %d / FAIL %d" % (checker.passed, checker.failed))
            print("data dir kept: %s" % data_dir)
        else:
            print("\nRESULT: PASS %d / FAIL %d" % (checker.passed, checker.failed))
            shutil.rmtree(temp_root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
