#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Zero-GPU selftest for ``adapters/caption_adapter.py``.

Uses a **stub llama-server** (a tiny stdlib HTTP server in a temp file) and
temporary image folders only -- no torch, no GPU, no network.

Baseline measured by the 维护者 with the REAL Qwen3.5-9B-VL (A-level, thinking off):
  * server load 4.0-4.1 s；单图 0.69-0.87 s；6 张合计 4.7-5.2 s
  * VRAM peak 7,777 MiB，跑完回落 1,145-1,151 MiB
  * `reasoning_content` 每次都是 0 字（关思考生效）
These numbers are documentation only; this selftest never loads the model.

Covers (each is a PASS/FAIL line):
  * normal path: N images -> N same-name .txt (UTF-8, no BOM, single line)
  * server lifecycle: exactly one llama-server per job, killed in `finally`
  * thinking off: server argv has --reasoning off --reasoning-budget 0,
    every request carries chat_template_kwargs.enable_thinking=false,
    max_tokens >= 1024, and reasoning_content never reaches the .txt
  * thinking leak detection: when the stub still returns reasoning_content,
    metrics.thinking_still_on becomes True (never silent)
  * post-processing layers: boilerplate prefix strip / trailing punctuation /
    whole-clause cut (never mid-clause) / one re-ask / truncated counter
  * content empty -> one retry with a bigger budget
  * trigger prefix, overwrite/skip, limit, caption_ext, lang, max_chars
  * cancellation stops after the current image (already written files kept)
  * negative controls: missing mmproj / missing dir / relative dir -> clear
    error and NO half-written .txt
  * packaging guard: the adapter imports stdlib only (no llama_cpp/torch/...)
"""

from __future__ import annotations

import ast
import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ADAPTER = HERE / "caption_adapter.py"
sys.path.insert(0, str(HERE))

import caption_adapter as A  # noqa: E402

PASS = 0
FAIL = 0
FAILURES = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if condition:
        PASS += 1
        print("PASS  %s%s" % (name, ("  [" + detail + "]") if detail else ""))
    else:
        FAIL += 1
        FAILURES.append("%s: %s" % (name, detail))
        print("FAIL  %s  [%s]" % (name, detail))
    return bool(condition)


# --------------------------------------------------------------------------- #
# stub llama-server (a real process; the adapter must start/kill it)
# --------------------------------------------------------------------------- #

STUB_SERVER = r'''
import argparse, json, os, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODE = os.environ.get("STUB_MODE", "ok")
REQS = os.environ.get("STUB_REQS", "")
ARGS = os.environ.get("STUB_ARGS", "")
STARTS = os.environ.get("STUB_STARTS", "")

if ARGS:
    with open(ARGS, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\n")
if STARTS:
    with open(STARTS, "a", encoding="utf-8") as fh:
        fh.write("start %s\n" % time.time())
if MODE == "reject_reasoning" and "--reasoning" in sys.argv:
    sys.exit(3)

COUNT = {"n": 0}
# stub answers deliberately include a boilerplate prefix + a trailing period:
# the adapter must strip both before writing (post-processing layers 1 and 2).
SHORT = "图中是一只橘猫坐在木桌上。"
LONG = "一只橘猫坐在木桌上，窗外是绿色的树，阳光照在桌面上。" * 2  # > 40 chars
ASCII_CLAUSES = "aaaa，bbbb，cccc，dddd，eeee，ffff"  # exact whole-clause test
FIRST_LONG = "一" * 45 + "，后面是场景。"  # first clause alone > 40 chars


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if MODE == "no_health":
            return self._send(503, {"status": "loading"})
        if self.path.startswith("/health"):
            return self._send(200, {"status": "ok"})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        index = COUNT["n"]
        COUNT["n"] += 1
        if REQS:
            with open(REQS, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"index": index, "path": self.path, "body": body},
                                    ensure_ascii=False) + "\n")
        content, reasoning = SHORT, ""
        body_content = (body.get("messages") or [{}])[0].get("content") or []
        images = [part for part in body_content if part.get("type") == "image_url"]
        n_img = len(images)
        if MODE == "http_500":
            return self._send(500, {"error": "simulated server error"})
        if MODE == "batch_400" and n_img > 1:
            return self._send(400, {"error": "simulated bad image in batch"})
        if MODE == "timeout_once" and index == 0:
            time.sleep(1.5)
        if MODE == "fail_second" and index == 1:
            return self._send(500, {"error": "simulated bad image"})
        # exact-length answers for the re-ask threshold boundaries
        if MODE == "len39":
            content = "一" * 39
        elif MODE == "len45":
            content = "一" * 45
        elif MODE == "len80":
            content = "一" * 80
        elif MODE == "len81":
            content = ("一" * 81) if index == 0 else "橘猫坐在木桌上"
        elif MODE == "len81_long":
            content = "一" * 81
        elif MODE == "batch_long81" and n_img == 1:
            content = "一" * 81  # the re-ask for a runaway caption still runs away
        batch_terms = [
            "赤膊男子在健身房深蹲", "赤膊男子站在窗前", "赤膊男子举着哑铃",
            "赤膊男子靠在墙边", "赤膊男子看向镜头", "赤膊男子擦汗",
            "赤膊男子坐着休息", "赤膊男子回头",
        ]
        if MODE.startswith("batch_") and n_img > 1:
            lines = ["%d: %s" % (i + 1, batch_terms[i % len(batch_terms)])
                     for i in range(n_img)]
            if MODE == "batch_short":
                lines = lines[:3]
            elif MODE == "batch_extra":
                lines = lines + ["%d: 多余的一行" % (n_img + 1),
                                 "%d: 又一行" % (n_img + 2)]
            elif MODE == "batch_reorder":
                lines = list(reversed(lines))
            elif MODE == "batch_reasoning":
                reasoning = "SECRET-BATCH-REASONING " * 2
            elif MODE == "batch_slow":
                time.sleep(0.5)
            elif MODE == "batch_long45":
                lines = ["%d: %s" % (i + 1, "一" * 45) for i in range(n_img)]
            elif MODE == "batch_long81":
                lines = ["%d: %s" % (i + 1, "一" * 81) for i in range(n_img)]
            content = "\n".join(lines)
        elif MODE == "reasoning_on":
            reasoning = "SECRET-REASONING-MUST-NOT-LEAK " * 3
        elif MODE == "empty_then_ok":
            if index == 0:
                content, reasoning = "", "thinking about the image"
        elif MODE == "too_long_once":
            content = LONG if index == 0 else "图中是橘猫位于木桌上。"
        elif MODE == "always_long":
            content = LONG
        elif MODE == "ascii_clauses":
            content = ASCII_CLAUSES
        elif MODE == "first_clause_long":
            content = FIRST_LONG
        elif MODE == "boilerplate_only":
            content = "图中"
        elif MODE == "always_empty":
            content, reasoning = "", "thinking forever"
        elif MODE == "slow":
            time.sleep(0.6)
        self._send(200, {
            "choices": [{
                "message": {"role": "assistant", "content": content,
                            "reasoning_content": reasoning},
                "finish_reason": "stop",
            }],
            "model": body.get("model", "?"),
        })


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args, _unknown = parser.parse_known_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
'''


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def _server_ended(pid) -> bool:
    """Authoritative residue check: the Popen we spawned has terminated.

    ``pid`` alone can be reused by Windows between runs (flaky), so the Popen's
    poll() is the evidence -- this is exactly what the task requires.
    """
    proc = getattr(A, "_LAST_SERVER_PROC", None)
    if proc is None:
        return False
    if pid is not None and proc.pid != pid:
        return False
    return proc.poll() is not None


class Case:
    """One run_case() result bundle."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _stub_env(tmp: Path, tag: str, mode: str):
    reqs = tmp / ("reqs-%s.jsonl" % tag)
    args = tmp / ("args-%s.log" % tag)
    starts = tmp / ("starts-%s.log" % tag)
    for path in (reqs, args, starts):
        if path.exists():
            path.unlink()
    os.environ["STUB_MODE"] = mode
    os.environ["STUB_REQS"] = str(reqs)
    os.environ["STUB_ARGS"] = str(args)
    os.environ["STUB_STARTS"] = str(starts)
    return reqs, args, starts


def _clear_stub_env():
    for key in ("STUB_MODE", "STUB_REQS", "STUB_ARGS", "STUB_STARTS"):
        os.environ.pop(key, None)


def _make_images(root: Path, names, ext=".png"):
    root.mkdir(parents=True, exist_ok=True)
    for name in names:
        (root / (name + ext)).write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 32)
    return root


def run_case(tmp: Path, stub: Path, tag: str, *, mode="ok", names=("a", "b", "c"),
             extra=None, cancel_after=None, metrics=True, server_cmd=None):
    out = _make_images(tmp / ("out-" + tag), names)
    reqs, args, starts = _stub_env(tmp, tag, mode)
    params = {
        "dir": str(out),
        "startup_timeout": 20,
        "request_timeout": 20,
        "batch_size": 1,  # legacy tests exercise the per-image path explicitly
    }
    params.update(extra or {})
    stop = tmp / ("stop-" + tag)

    def callback(payload):
        if cancel_after is not None and int(payload.get("done") or 0) >= cancel_after:
            stop.write_text("cancel\n", encoding="utf-8")

    metrics_path = (tmp / ("metrics-%s.json" % tag)) if metrics else None
    cmd = server_cmd or [sys.executable, str(stub), "--port", "{port}"]
    result = None
    exc = None
    try:
        result = A.run(
            {"id": "job-" + tag, "params": params},
            tmp / ("cap-%s.log" % tag),
            stop_file=(stop if cancel_after is not None else None),
            metrics_path=metrics_path,
            metrics_cb=callback,
            server_cmd=cmd,
        )
    except Exception as error:  # noqa: BLE001 - the caller asserts on it
        exc = error
    finally:
        _clear_stub_env()
    return Case(result=result, exc=exc, out=out, reqs=reqs, args=args, starts=starts,
                metrics_path=metrics_path)


def _requests(reqs: Path):
    if not reqs.is_file():
        return []
    return [json.loads(line) for line in reqs.read_text(encoding="utf-8").splitlines() if line.strip()]


def _starts(starts: Path):
    if not starts.is_file():
        return []
    return [line for line in starts.read_text(encoding="utf-8").splitlines() if line.strip()]


def _server_args(args: Path):
    if not args.is_file():
        return []
    return [json.loads(line) for line in args.read_text(encoding="utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #


def test_normal(tmp: Path, stub: Path):
    case = run_case(tmp, stub, "normal")
    result = case.result or {}
    metrics = result.get("metrics") or {}
    files = sorted(path.name for path in case.out.glob("*.txt"))
    check("normal: run succeeded", result.get("state") == "succeeded", str(result.get("state")))
    check("normal: three .txt written", files == ["a.txt", "b.txt", "c.txt"], str(files))
    first = (case.out / "a.txt")
    raw = first.read_bytes() if first.is_file() else b""
    text = raw.decode("utf-8", errors="replace")
    # stub says "图中是一只橘猫坐在木桌上。" -> boilerplate + trailing period stripped
    check("normal: caption cleaned (boilerplate + trailing punct)",
          text == "一只橘猫坐在木桌上\n", repr(text))
    check("normal: boilerplate prefix removed", not text.startswith("图中"), repr(text[:6]))
    check("normal: trailing period removed", not text.rstrip("\n").endswith(("。", ".", "！", "？")), repr(text[-3:]))
    check("normal: utf-8 without BOM", not raw.startswith(b"\xef\xbb\xbf"), raw[:3].hex())
    check("normal: single line", raw.count(b"\n") == 1 and not raw.rstrip(b"\n").count(b"\n"), repr(text[-6:]))
    check("normal: metrics counts",
          metrics.get("images_total") == 3 and metrics.get("written") == 3
          and metrics.get("skipped") == 0 and metrics.get("failed") == 0,
          json.dumps({k: metrics.get(k) for k in ("images_total", "written", "skipped", "failed")}))
    check("normal: default lang=zh / max_chars=40",
          metrics.get("lang") == "zh" and metrics.get("max_chars") == 40,
          "%s/%s" % (metrics.get("lang"), metrics.get("max_chars")))
    check("normal: thinking off detected",
          metrics.get("thinking_still_on") is False and metrics.get("reasoning_chars_total") == 0,
          json.dumps({k: metrics.get(k) for k in ("thinking_still_on", "reasoning_chars_total")}))
    starts = _starts(case.starts)
    check("normal: exactly one llama-server per job", len(starts) == 1, str(starts))
    # the stub overrides the real argv, so assert the builder itself (unit)
    built = " ".join(A._server_argv("engine.exe", "model.gguf", "mmproj.gguf", 1234, {}))
    plain = " ".join(A._server_argv("engine.exe", "model.gguf", "mmproj.gguf", 1234, {}, reasoning=False))
    check("normal: server argv disables thinking",
          "--reasoning off" in built and "--reasoning-budget 0" in built
          and "--image-min-tokens 1024" in built,
          built[:160])
    check("normal: fallback argv has no reasoning flags", "--reasoning" not in plain, plain[:120])
    pid = metrics.get("server_pid")
    check("normal: server process is killed in finally", _server_ended(pid),
          "pid=%s poll=%s" % (pid, getattr(getattr(A, "_LAST_SERVER_PROC", None), "poll", lambda: None)()))
    requests = _requests(case.reqs)
    check("normal: one request per image", len(requests) == 3, str(len(requests)))
    tokens_ok = all(int(item["body"].get("max_tokens") or 0) >= 1024 for item in requests)
    check("normal: max_tokens >= 1024", tokens_ok, str([item["body"].get("max_tokens") for item in requests]))
    thinking_off = all(
        (item["body"].get("chat_template_kwargs") or {}).get("enable_thinking") is False
        for item in requests
    )
    check("normal: chat_template_kwargs.enable_thinking=false on every request", thinking_off, "")
    image_ok = all(
        any(part.get("type") == "image_url"
            and str((part.get("image_url") or {}).get("url", "")).startswith("data:image/png;base64,")
            for part in (item["body"].get("messages") or [{}])[0].get("content") or [])
        for item in requests
    )
    check("normal: image sent as base64 data URI", image_ok, "")
    check("normal: only 127.0.0.1 traffic", all(item.get("path") == "/v1/chat/completions" for item in requests), "")
    meta = json.loads(case.metrics_path.read_text(encoding="utf-8")) if case.metrics_path.is_file() else {}
    check("normal: metrics file written", meta.get("written") == 3 and meta.get("state") == "succeeded", str(meta.get("state")))


def test_thinking_leak(tmp: Path, stub: Path):
    case = run_case(tmp, stub, "leak", mode="reasoning_on", names=("a",))
    metrics = (case.result or {}).get("metrics") or {}
    text = (case.out / "a.txt").read_text(encoding="utf-8") if (case.out / "a.txt").is_file() else ""
    check("leak: reasoning_content never leaks into .txt",
          "SECRET-REASONING" not in text and text.strip() == "一只橘猫坐在木桌上", repr(text))
    check("leak: thinking_still_on detected (not silent)",
          metrics.get("thinking_still_on") is True
          and int(metrics.get("reasoning_chars_total") or 0) > 0,
          json.dumps({k: metrics.get(k) for k in ("thinking_still_on", "reasoning_chars_total")}))


def test_empty_retry(tmp: Path, stub: Path):
    case = run_case(tmp, stub, "empty", mode="empty_then_ok", names=("a",))
    metrics = (case.result or {}).get("metrics") or {}
    requests = _requests(case.reqs)
    tokens = [int(item["body"].get("max_tokens") or 0) for item in requests]
    check("empty: content empty -> retried once", len(requests) == 2, str(len(requests)))
    check("empty: retry uses a bigger budget", len(tokens) == 2 and tokens[1] > tokens[0], str(tokens))
    check("empty: caption written after retry",
          (case.out / "a.txt").is_file() and metrics.get("written") == 1 and metrics.get("failed") == 0,
          json.dumps({k: metrics.get(k) for k in ("written", "failed")}))


def test_mid_overflow_no_reask(tmp: Path, stub: Path):
    """52 chars: over max_chars but <= 2x -> cut, NEVER re-ask (one round trip saved)."""
    case = run_case(tmp, stub, "long1", mode="too_long_once", names=("a",))
    metrics = (case.result or {}).get("metrics") or {}
    requests = _requests(case.reqs)
    text = (case.out / "a.txt").read_text(encoding="utf-8").strip() if (case.out / "a.txt").is_file() else ""
    expected = "一只橘猫坐在木桌上，窗外是绿色的树，阳光照在桌面上。一只橘猫坐在木桌上"
    check("mid-overflow: NO re-ask round trip", len(requests) == 1, str(len(requests)))
    check("mid-overflow: reasked=0", int(metrics.get("reasked") or 0) == 0,
          str(metrics.get("reasked")))
    check("mid-overflow: truncated=1", int(metrics.get("truncated") or 0) == 1,
          str(metrics.get("truncated")))
    check("mid-overflow: cut by whole clauses", text == expected, repr(text))


def test_always_long(tmp: Path, stub: Path):
    case = run_case(tmp, stub, "long2", mode="always_long", names=("a",))
    metrics = (case.result or {}).get("metrics") or {}
    text = (case.out / "a.txt").read_text(encoding="utf-8").strip() if (case.out / "a.txt").is_file() else ""
    expected = "一只橘猫坐在木桌上，窗外是绿色的树，阳光照在桌面上。一只橘猫坐在木桌上"
    check("long2: fits by WHOLE clauses (no mid-clause cut)",
          text == expected, "%d chars %r" % (len(text), text[-8:]))
    check("long2: hard-cut <= max_chars", bool(text) and len(text) <= 40, "%d chars" % len(text))
    check("long2: cut on a boundary without trailing punctuation",
          bool(text) and text[-1] not in "，,、；;。.!！?？:： ", repr(text[-4:]))
    check("long2: truncated counted", int(metrics.get("truncated") or 0) == 1, str(metrics.get("truncated")))
    check("long2: one request only (52 chars <= 2x, no re-ask)",
          len(_requests(case.reqs)) == 1, str(len(_requests(case.reqs))))
    check("long2: reasked=0", int(metrics.get("reasked") or 0) == 0,
          str(metrics.get("reasked")))


def test_reask_threshold(tmp: Path, stub: Path):
    """The 2x boundary is the load-bearing detail (40-80 cut, >80 re-ask once)."""
    case = run_case(tmp, stub, "len39", mode="len39", names=("a",))
    m = (case.result or {}).get("metrics") or {}
    check("threshold 39: under max_chars -> no re-ask, no truncation",
          int(m.get("reasked") or 0) == 0 and int(m.get("truncated") or 0) == 0
          and len(_requests(case.reqs)) == 1,
          "reasked=%s truncated=%s" % (m.get("reasked"), m.get("truncated")))

    case = run_case(tmp, stub, "len45", mode="len45", names=("a",))
    m = (case.result or {}).get("metrics") or {}
    text = (case.out / "a.txt").read_text(encoding="utf-8").strip() if (case.out / "a.txt").is_file() else ""
    check("threshold 45: reasked=0 / truncated=1",
          int(m.get("reasked") or 0) == 0 and int(m.get("truncated") or 0) == 1
          and len(_requests(case.reqs)) == 1,
          "reasked=%s truncated=%s requests=%d" % (m.get("reasked"), m.get("truncated"),
                                                   len(_requests(case.reqs))))
    check("threshold 45: final caption <= max_chars", bool(text) and len(text) <= 40,
          "%d chars" % len(text))

    case = run_case(tmp, stub, "len80", mode="len80", names=("a",))
    m = (case.result or {}).get("metrics") or {}
    check("threshold 80: exactly 2x -> NO re-ask (strict >)",
          int(m.get("reasked") or 0) == 0 and int(m.get("truncated") or 0) == 1
          and len(_requests(case.reqs)) == 1,
          "reasked=%s truncated=%s" % (m.get("reasked"), m.get("truncated")))

    case = run_case(tmp, stub, "len81", mode="len81", names=("a",))
    m = (case.result or {}).get("metrics") or {}
    check("threshold 81: > 2x -> re-ask exactly once",
          int(m.get("reasked") or 0) == 1 and len(_requests(case.reqs)) == 2,
          "reasked=%s requests=%d" % (m.get("reasked"), len(_requests(case.reqs))))
    check("threshold 81: short re-answer needs no truncation",
          int(m.get("truncated") or 0) == 0, str(m.get("truncated")))

    case = run_case(tmp, stub, "len81b", mode="len81_long", names=("a",))
    m = (case.result or {}).get("metrics") or {}
    check("threshold 81 still long: re-ask once then truncated=1",
          int(m.get("reasked") or 0) == 1 and int(m.get("truncated") or 0) == 1
          and len(_requests(case.reqs)) == 2,
          "reasked=%s truncated=%s" % (m.get("reasked"), m.get("truncated")))

    # batch path must apply the same threshold
    case = run_case(tmp, stub, "batch45", mode="batch_long45",
                    names=("a", "b"), extra={"batch_size": 6})
    m = (case.result or {}).get("metrics") or {}
    check("batch threshold 45: truncated=2 / reasked=0",
          int(m.get("truncated") or 0) == 2 and int(m.get("reasked") or 0) == 0,
          "truncated=%s reasked=%s" % (m.get("truncated"), m.get("reasked")))
    case = run_case(tmp, stub, "batch81", mode="batch_long81",
                    names=("a", "b"), extra={"batch_size": 6})
    m = (case.result or {}).get("metrics") or {}
    check("batch threshold 81: reasked=2 / truncated=2",
          int(m.get("reasked") or 0) == 2 and int(m.get("truncated") or 0) == 2,
          "reasked=%s truncated=%s" % (m.get("reasked"), m.get("truncated")))


def test_postprocess_layers(tmp: Path, stub: Path):
    """The three post-processing layers from the final spec (each can go red)."""
    prefixes = ["图中是", "图中", "这是一张", "这是一幅", "图片显示",
                "照片显示", "画面中", "图里", "本图", "该图"]
    stripped = {prefix: A.clean_caption(prefix + "，一只橘猫坐在桌上。") for prefix in prefixes}
    check("layer1: all 10 boilerplate prefixes stripped",
          all(value == "一只橘猫坐在桌上" for value in stripped.values()),
          json.dumps(stripped, ensure_ascii=False))
    check("layer1: boilerplate-only keeps the original", A.clean_caption("图中") == "图中",
          repr(A.clean_caption("图中")))
    check("layer2: trailing punctuation removed",
          [A.clean_caption("一只猫坐在桌上" + mark) for mark in ("。", "．", ".", "！", "？", "  ")]
          == ["一只猫坐在桌上"] * 6, "")
    whole, cut = A.fit_caption("aaaa，bbbb，cccc，dddd，eeee，ffff", 20)
    check("layer3: whole-clause accumulation (no half clause)",
          whole == "aaaa，bbbb，cccc，dddd" and cut is True, "%r cut=%s" % (whole, cut))

    case = run_case(tmp, stub, "ascii", mode="ascii_clauses", names=("a",), extra={"max_chars": 20})
    text = (case.out / "a.txt").read_text(encoding="utf-8").strip() if (case.out / "a.txt").is_file() else ""
    check("layer3 e2e: ascii clauses accumulate exactly",
          text == "aaaa，bbbb，cccc，dddd", repr(text))

    case = run_case(tmp, stub, "firstlong", mode="first_clause_long", names=("a",))
    text = (case.out / "a.txt").read_text(encoding="utf-8").strip() if (case.out / "a.txt").is_file() else ""
    check("layer3 fallback: first clause too long -> bounded hard cut",
          bool(text) and len(text) <= 40, "%d chars" % len(text))

    case = run_case(tmp, stub, "boiler", mode="boilerplate_only", names=("a",))
    metrics = (case.result or {}).get("metrics") or {}
    text = (case.out / "a.txt").read_text(encoding="utf-8").strip() if (case.out / "a.txt").is_file() else ""
    check("layer1 e2e: '图中' alone still writes a non-empty caption",
          text == "图中" and metrics.get("failed") == 0 and metrics.get("written") == 1,
          "%r failed=%s" % (text, metrics.get("failed")))


def test_trigger_overwrite_skip(tmp: Path, stub: Path):
    case = run_case(tmp, stub, "trig", names=("a",), extra={"trigger": "sks-cat"})
    text = (case.out / "a.txt").read_text(encoding="utf-8").strip() if (case.out / "a.txt").is_file() else ""
    check("trigger: prefix '<trigger>, '", text == "sks-cat, 一只橘猫坐在木桌上", repr(text))

    out = _make_images(tmp / "out-skip", ("a",))
    (out / "a.txt").write_text("old caption\n", encoding="utf-8")
    case = run_case(tmp, stub, "skip", names=("a",))
    metrics = (case.result or {}).get("metrics") or {}
    check("skip: existing caption not overwritten",
          (case.out / "a.txt").read_text(encoding="utf-8").strip() == "old caption"
          and metrics.get("skipped") == 1 and metrics.get("written") == 0,
          json.dumps({k: metrics.get(k) for k in ("skipped", "written")}))
    check("skip: no request for a skipped image", len(_requests(case.reqs)) == 0, str(len(_requests(case.reqs))))

    out2 = _make_images(tmp / "out-ow", ("a",))
    (out2 / "a.txt").write_text("old caption\n", encoding="utf-8")
    case = run_case(tmp, stub, "overwrite", names=("a",), extra={"overwrite": True})
    metrics = (case.result or {}).get("metrics") or {}
    check("overwrite: existing caption replaced",
          (case.out / "a.txt").read_text(encoding="utf-8").strip() == "一只橘猫坐在木桌上"
          and metrics.get("written") == 1 and metrics.get("skipped") == 0,
          json.dumps({k: metrics.get(k) for k in ("written", "skipped")}))


def test_limit_ext_lang_chars(tmp: Path, stub: Path):
    case = run_case(tmp, stub, "limit", names=("a", "b", "c"), extra={"limit": 1})
    metrics = (case.result or {}).get("metrics") or {}
    check("limit: only N images considered",
          metrics.get("images_total") == 1 and metrics.get("written") == 1
          and len(_requests(case.reqs)) == 1,
          "total=%s written=%s" % (metrics.get("images_total"), metrics.get("written")))

    case = run_case(tmp, stub, "ext", names=("a",), extra={"caption_ext": ".caption"})
    check("caption_ext: writes <name>.caption",
          (case.out / "a.caption").is_file() and not (case.out / "a.txt").is_file(), "")

    case = run_case(tmp, stub, "lang", names=("a",), extra={"lang": "en"})
    requests = _requests(case.reqs)
    prompt = ""
    if requests:
        for part in (requests[0]["body"].get("messages") or [{}])[0].get("content") or []:
            if part.get("type") == "text":
                prompt = part.get("text") or ""
    check("lang=en: English 40-character prompt", "40 characters" in prompt, prompt[:60])

    case = run_case(tmp, stub, "chars", mode="always_long", names=("a",), extra={"max_chars": 12})
    metrics = (case.result or {}).get("metrics") or {}
    text = (case.out / "a.txt").read_text(encoding="utf-8").strip() if (case.out / "a.txt").is_file() else ""
    check("max_chars=12 honoured", bool(text) and len(text) <= 12 and metrics.get("max_chars") == 12,
          "%d chars" % len(text))


def test_cancel(tmp: Path, stub: Path):
    case = run_case(tmp, stub, "cancel", mode="slow", names=("a", "b", "c"), cancel_after=1)
    result = case.result or {}
    metrics = result.get("metrics") or {}
    files = sorted(path.name for path in case.out.glob("*.txt"))
    pid = metrics.get("server_pid")
    check("cancel: stops after the current image",
          result.get("state") == "cancelled" and metrics.get("cancelled") is True,
          "state=%s" % result.get("state"))
    check("cancel: exactly one caption written", files == ["a.txt"], str(files))
    check("cancel: metrics written=1 / total=3",
          metrics.get("written") == 1 and metrics.get("images_total") == 3,
          "%s/%s" % (metrics.get("written"), metrics.get("images_total")))
    check("cancel: server killed after cancel", _server_ended(pid), "pid=%s" % pid)
    check("cancel: no request for the next image", len(_requests(case.reqs)) == 1, str(len(_requests(case.reqs))))


def test_failures(tmp: Path, stub: Path):
    case = run_case(tmp, stub, "emptyall", mode="always_empty", names=("a",))
    result = case.result or {}
    metrics = result.get("metrics") or {}
    pid = metrics.get("server_pid")
    check("empty-all: no caption left is counted",
          result.get("state") == "failed" and metrics.get("failed") == 1
          and metrics.get("no_caption_left") == 1 and metrics.get("written") == 0,
          json.dumps({k: metrics.get(k) for k in ("state", "failed", "no_caption_left", "written")}))
    check("empty-all: no .txt written", not list(case.out.glob("*.txt")), "")
    check("empty-all: server killed on the failed path", _server_ended(pid), "pid=%s" % pid)

    # server exits immediately -> run raises and leaves nothing behind
    out = _make_images(tmp / "out-die", ("a",))
    before = sorted(path.name for path in out.iterdir())
    try:
        A.run({"id": "job-die", "params": {"dir": str(out), "startup_timeout": 10}},
              tmp / "die.log", server_cmd=[sys.executable, "-c", "raise SystemExit(3)"])
    except Exception as error:  # noqa: BLE001
        exc = error
    else:
        exc = None
    check("dead server: run raises a clear error", exc is not None and "llama-server" in str(exc),
          "%s: %s" % (type(exc).__name__ if exc else "-", str(exc)[:80]))
    check("dead server: no caption written", sorted(path.name for path in out.iterdir()) == before, str(before))


def test_negative_controls(tmp: Path):
    out = _make_images(tmp / "out-neg", ("a",))
    before = sorted(path.name for path in out.iterdir())
    missing_mmproj = tmp / "does-not-exist-mmproj.gguf"
    real_model = A.BACKENDS["qw35-9b"]["model"]
    try:
        A.run({"id": "job-neg", "params": {
            "dir": str(out), "model": str(real_model), "mmproj": str(missing_mmproj),
        }}, tmp / "neg.log")
    except Exception as error:  # noqa: BLE001
        exc = error
    else:
        exc = None
    message = str(exc or "")
    check("neg: missing mmproj raises with its name",
          exc is not None and "mmproj" in message, "%s: %s" % (type(exc).__name__ if exc else "-", message[:90]))
    check("neg: missing mmproj leaves no half caption",
          sorted(path.name for path in out.iterdir()) == before, str(before))

    try:
        A.run({"id": "job-nodir", "params": {"dir": str(tmp / "no-such-dir")}}, tmp / "nodir.log")
    except Exception as error:  # noqa: BLE001
        exc = error
    else:
        exc = None
    check("neg: missing dir raises ValueError", isinstance(exc, ValueError) and "does not exist" in str(exc),
          str(exc)[:80])

    try:
        A.run({"id": "job-rel", "params": {"dir": "relative/path"}}, tmp / "rel.log")
    except Exception as error:  # noqa: BLE001
        exc = error
    else:
        exc = None
    check("neg: relative dir rejected", isinstance(exc, ValueError) and "absolute" in str(exc), str(exc)[:80])


def test_flags_fallback(tmp: Path, stub: Path):
    """Old llama-server builds reject --reasoning: fall back once, mark unsupported."""
    out = _make_images(tmp / "out-fallback", ("a",))
    fake_engine = tmp / "fake-engine.exe"
    fake_engine.write_bytes(b"stub")
    fake_model = tmp / "fake-model.gguf"
    fake_model.write_bytes(b"stub")
    fake_mmproj = tmp / "fake-mmproj.gguf"
    fake_mmproj.write_bytes(b"stub")
    reqs, args, starts = _stub_env(tmp, "fallback", "ok")

    real_start = A._start_server
    calls = {"n": 0}

    def flaky_start(argv, log_path, log_handle, timeout, port, box):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("error: unknown argument '--reasoning' (old build)")
        return real_start([sys.executable, str(stub), "--port", str(port)],
                          log_path, log_handle, timeout, port, box)

    A._start_server = flaky_start
    try:
        result = A.run({"id": "job-fallback", "params": {
            "dir": str(out), "engine": str(fake_engine),
            "model": str(fake_model), "mmproj": str(fake_mmproj),
            "startup_timeout": 20, "request_timeout": 20,
        }}, tmp / "fallback.log")
    finally:
        A._start_server = real_start
        _clear_stub_env()
    metrics = result.get("metrics") or {}
    check("fallback: old build -> retried without reasoning flags",
          metrics.get("thinking_flags_supported") is False, str(metrics.get("thinking_flags_supported")))
    check("fallback: caption still written", (out / "a.txt").is_file() and metrics.get("written") == 1, "")


def test_build_caption_job(tmp: Path, stub: Path):
    """The params-first entry point the core wires to must work too."""
    out = _make_images(tmp / "out-build", ("a",))
    reqs, args, starts = _stub_env(tmp, "build", "ok")
    try:
        result = A.build_caption_job(
            {"dir": str(out), "startup_timeout": 20, "request_timeout": 20,
             "batch_size": 1},
            tmp / "build.log",
            server_cmd=[sys.executable, str(stub), "--port", "{port}"],
        )
    finally:
        _clear_stub_env()
    check("build_caption_job: params-first entry works",
          result.get("state") == "succeeded" and (out / "a.txt").is_file(),
          str(result.get("state")))


def test_batch_mode(tmp: Path, stub: Path):
    """Default unified mode: N images in one request, strict `1: ...` lines."""
    case = run_case(tmp, stub, "batch", mode="batch_ok",
                    names=("a", "b", "c"), extra={"batch_size": 6})
    metrics = (case.result or {}).get("metrics") or {}
    requests = _requests(case.reqs)
    body = requests[0]["body"] if requests else {}
    parts = (body.get("messages") or [{}])[0].get("content") or []
    n_img = len([part for part in parts if part.get("type") == "image_url"])
    prompt_text = next((part.get("text") for part in parts if part.get("type") == "text"), "")
    check("batch: mode=batch / batch_size=6 / batches=1",
          metrics.get("mode") == "batch" and metrics.get("batch_size") == 6
          and metrics.get("batches") == 1,
          "%s/%s/%s" % (metrics.get("mode"), metrics.get("batch_size"), metrics.get("batches")))
    check("batch: 3 images in ONE request", len(requests) == 1 and n_img == 3,
          "requests=%d images=%d" % (len(requests), n_img))
    check("batch: prompt is the numbered-lines contract",
          "1..3" in prompt_text and "1: 描述" in prompt_text, prompt_text[:70])
    contents = {path.name: path.read_text(encoding="utf-8").strip()
                for path in case.out.glob("*.txt")}
    check("batch: captions mapped by index",
          contents.get("a.txt") == "赤膊男子在健身房深蹲"
          and contents.get("b.txt") == "赤膊男子站在窗前"
          and contents.get("c.txt") == "赤膊男子举着哑铃",
          json.dumps(contents, ensure_ascii=False))
    check("batch: subject_term extracted", metrics.get("subject_term") == "赤膊男子",
          str(metrics.get("subject_term")))
    built = " ".join(A._server_argv("engine.exe", "m.gguf", "mm.gguf", 1234,
                                    {"ctx": A.BATCH_CTX_DEFAULT}))
    check("batch: server argv opens -c 16384", "-c 16384" in built, built[-80:])


def test_batch_short_lines(tmp: Path, stub: Path):
    """Fewer lines than images -> missing ones go through the per-image path."""
    case = run_case(tmp, stub, "bshort", mode="batch_short",
                    names=("a", "b", "c", "d", "e"), extra={"batch_size": 6})
    metrics = (case.result or {}).get("metrics") or {}
    check("batch short: fallback_individual=2",
          metrics.get("fallback_individual") == 2, str(metrics.get("fallback_individual")))
    check("batch short: all 5 captions written", metrics.get("written") == 5,
          str(metrics.get("written")))
    check("batch short: 1 batch + 2 fallback requests", len(_requests(case.reqs)) == 3,
          str(len(_requests(case.reqs))))
    check("batch short: fallback captions are per-image ones",
          (case.out / "d.txt").read_text(encoding="utf-8").strip() == "一只橘猫坐在木桌上",
          repr((case.out / "d.txt").read_text(encoding="utf-8")))


def test_batch_extra_lines(tmp: Path, stub: Path):
    """More lines than images -> extras are dropped and counted, never written to a file."""
    case = run_case(tmp, stub, "bextra", mode="batch_extra",
                    names=("a", "b", "c"), extra={"batch_size": 6})
    metrics = (case.result or {}).get("metrics") or {}
    files = sorted(path.name for path in case.out.glob("*.txt"))
    check("batch extra: dropped_lines=2", metrics.get("dropped_lines") == 2,
          str(metrics.get("dropped_lines")))
    check("batch extra: exactly 3 .txt files", files == ["a.txt", "b.txt", "c.txt"], str(files))
    check("batch extra: extras never became files", not (case.out / "d.txt").exists(), "")


def test_batch_reorder(tmp: Path, stub: Path):
    """Lines come 2,1 but the mapping must follow the NUMBER, not the line order."""
    case = run_case(tmp, stub, "breorder", mode="batch_reorder",
                    names=("a", "b"), extra={"batch_size": 6})
    contents = {path.name: path.read_text(encoding="utf-8").strip()
                for path in case.out.glob("*.txt")}
    check("batch reorder: index 1 -> first file, index 2 -> second file",
          contents.get("a.txt") == "赤膊男子在健身房深蹲"
          and contents.get("b.txt") == "赤膊男子站在窗前",
          json.dumps(contents, ensure_ascii=False))


def test_batch_autochunk(tmp: Path, stub: Path):
    """ctx budget < images*image_min_tokens -> smaller batch + subject hint in later chunks."""
    case = run_case(tmp, stub, "bchunk", mode="batch_ok", names=("a", "b", "c", "d", "e", "f"),
                    extra={"batch_size": 6, "image_min_tokens": 4096})
    metrics = (case.result or {}).get("metrics") or {}
    requests = _requests(case.reqs)
    second_prompt = ""
    if len(requests) >= 2:
        parts = (requests[1]["body"].get("messages") or [{}])[0].get("content") or []
        second_prompt = next((p.get("text") for p in parts if p.get("type") == "text"), "")
    check("autochunk: batch_size reduced to 3 / batches=2 / reduced_from=6",
          metrics.get("batch_size") == 3 and metrics.get("batches") == 2
          and metrics.get("batch_reduced_from") == 6,
          "%s/%s/%s" % (metrics.get("batch_size"), metrics.get("batches"),
                        metrics.get("batch_reduced_from")))
    check("autochunk: two batch requests", len(requests) == 2, str(len(requests)))
    check("autochunk: later chunk carries the subject hint",
          "统一用『赤膊男子』称呼主体" in second_prompt, second_prompt[-60:])
    check("autochunk: all 6 written", metrics.get("written") == 6, str(metrics.get("written")))


def test_batch_switch_and_cancel(tmp: Path, stub: Path):
    case = run_case(tmp, stub, "bsize1", names=("a", "b", "c"), extra={"batch_size": 1})
    metrics = (case.result or {}).get("metrics") or {}
    check("batch_size=1 -> per_image mode",
          metrics.get("mode") == "per_image" and metrics.get("batch_size") == 1
          and len(_requests(case.reqs)) == 3,
          "%s requests=%d" % (metrics.get("mode"), len(_requests(case.reqs))))

    try:
        run_case(tmp, stub, "bsize9", names=("a",), extra={"batch_size": 9})
    except Exception:  # run_case swallows exceptions into .exc
        pass
    case9 = run_case(tmp, stub, "bsize9b", names=("a",), extra={"batch_size": 9})
    check("batch_size=9 rejected", isinstance(case9.exc, ValueError), str(case9.exc)[:60])

    case = run_case(tmp, stub, "bcancel", mode="batch_slow",
                    names=("a", "b", "c", "d"), extra={"batch_size": 2}, cancel_after=2)
    metrics = (case.result or {}).get("metrics") or {}
    files = sorted(path.name for path in case.out.glob("*.txt"))
    check("batch cancel: stopped before the next chunk",
          (case.result or {}).get("state") == "cancelled" and metrics.get("written") == 2
          and files == ["a.txt", "b.txt"],
          "state=%s written=%s files=%s" % ((case.result or {}).get("state"),
                                            metrics.get("written"), files))


def test_batch_reasoning(tmp: Path, stub: Path):
    case = run_case(tmp, stub, "breason", mode="batch_reasoning",
                    names=("a", "b"), extra={"batch_size": 6})
    metrics = (case.result or {}).get("metrics") or {}
    text = (case.out / "a.txt").read_text(encoding="utf-8") if (case.out / "a.txt").is_file() else ""
    check("batch reasoning: reasoning_content never leaks",
          "SECRET-BATCH-REASONING" not in text, repr(text[:40]))
    check("batch reasoning: thinking_still_on detected",
          metrics.get("thinking_still_on") is True
          and int(metrics.get("reasoning_chars_total") or 0) > 0,
          json.dumps({k: metrics.get(k) for k in ("thinking_still_on", "reasoning_chars_total")}))


def _run_inline(tmp: Path, stub: Path, tag: str, mode: str, images: dict,
                params_extra=None, cancel_after=None):
    """Create exact image files (name -> bytes) then run; returns a Case."""
    out = tmp / ("inline-" + tag)
    out.mkdir(parents=True, exist_ok=True)
    for name, payload in images.items():
        (out / name).write_bytes(payload)
    reqs, args, starts = _stub_env(tmp, "inline-" + tag, mode)
    params = {"dir": str(out), "batch_size": 1, "startup_timeout": 20,
              "request_timeout": 20}
    params.update(params_extra or {})
    stop = tmp / ("inline-stop-" + tag)

    def callback(payload):
        if cancel_after is not None and int(payload.get("done") or 0) >= cancel_after:
            stop.write_text("cancel\n", encoding="utf-8")

    try:
        result = A.run({"id": "job-" + tag, "params": params}, tmp / ("inline-%s.log" % tag),
                       stop_file=(stop if cancel_after is not None else None),
                       metrics_path=tmp / ("inline-%s.metrics.json" % tag),
                       metrics_cb=callback,
                       server_cmd=[sys.executable, str(stub), "--port", "{port}"])
        exc = None
    except Exception as error:  # noqa: BLE001
        result, exc = None, error
    finally:
        _clear_stub_env()
    return Case(result=result, exc=exc, out=out, reqs=reqs, args=args, starts=starts,
                metrics_path=tmp / ("inline-%s.metrics.json" % tag))


def test_timeouts_and_residue(tmp: Path, stub: Path):
    """1) startup timeout  2) per-image timeout  3) normal end -> all kill the process."""
    out = _make_images(tmp / "out-startup", ("a",))
    _stub_env(tmp, "startup", "no_health")
    try:
        A.run({"id": "job-startup", "params": {"dir": str(out), "startup_timeout": 1}},
              tmp / "startup.log",
              server_cmd=[sys.executable, str(stub), "--port", "{port}"])
        exc = None
    except BaseException as error:  # noqa: BLE001
        exc = error
    finally:
        _clear_stub_env()
    proc = A._LAST_SERVER_PROC
    check("timeout: startup timeout -> engine_start_timeout",
          getattr(exc, "reason", None) == A.REASON_START_TIMEOUT,
          "%s %s" % (type(exc).__name__, getattr(exc, "reason", None)))
    check("timeout: startup-failure process killed (poll not None)",
          proc is not None and proc.poll() is not None,
          "poll=%s" % (proc.poll() if proc else None))
    check("timeout: startup failure leaves no caption", not list(out.glob("*.txt")), "")

    case = run_case(tmp, stub, "itimeout", mode="timeout_once",
                    names=("a", "b"), extra={"request_timeout": 1})
    metrics = (case.result or {}).get("metrics") or {}
    proc = A._LAST_SERVER_PROC
    check("timeout: per-image timeout counted as image_timeout",
          int((metrics.get("failed_codes") or {}).get("image_timeout") or 0) == 1,
          json.dumps(metrics.get("failed_codes"), ensure_ascii=False))
    check("timeout: batch continues after the timed-out image",
          metrics.get("written") == 1 and metrics.get("failed") == 1,
          "%s/%s" % (metrics.get("written"), metrics.get("failed")))
    check("timeout: timeout path kills the server (poll not None)",
          proc is not None and proc.poll() is not None, "poll=%s" % (proc.poll() if proc else None))

    case = run_case(tmp, stub, "normalend", names=("a", "b"))
    proc = A._LAST_SERVER_PROC
    check("residue: normal end kills the server (poll not None)",
          proc is not None and proc.poll() is not None, "poll=%s" % (proc.poll() if proc else None))

    case = run_case(tmp, stub, "budget", names=("a", "b", "c"), extra={"budget_s": 0.01})
    metrics = (case.result or {}).get("metrics") or {}
    check("budget: stopped_by_budget + cancelled, nothing written",
          metrics.get("stopped_by_budget") is True
          and (case.result or {}).get("state") == "cancelled"
          and metrics.get("written") == 0,
          "budget=%s state=%s" % (metrics.get("stopped_by_budget"),
                                  (case.result or {}).get("state")))


def test_keyboard_interrupt(tmp: Path, stub: Path):
    out = _make_images(tmp / "out-int", ("a", "b"))
    reqs, args, starts = _stub_env(tmp, "int", "slow")
    state = {"raised": False}

    def boom(payload):
        if int(payload.get("done") or 0) >= 1:
            raise KeyboardInterrupt("simulated Ctrl+C")

    metrics_path = tmp / "int.metrics.json"
    try:
        A.run({"id": "job-int", "params": {"dir": str(out), "batch_size": 1,
                                           "startup_timeout": 20, "request_timeout": 20}},
              tmp / "int.log", metrics_cb=boom, metrics_path=metrics_path,
              server_cmd=[sys.executable, str(stub), "--port", "{port}"])
    except KeyboardInterrupt:
        state["raised"] = True
    finally:
        _clear_stub_env()
    proc = A._LAST_SERVER_PROC
    meta = json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.is_file() else {}
    check("interrupt: KeyboardInterrupt propagates", state["raised"] is True, "")
    check("interrupt: server killed on the interrupt path",
          proc is not None and proc.poll() is not None,
          "poll=%s" % (proc.poll() if proc else None))
    check("interrupt: metrics written as cancelled", meta.get("state") == "cancelled",
          str(meta.get("state")))


def test_bad_environment(tmp: Path):
    out = _make_images(tmp / "out-env", ("a",))
    real_model = A.BACKENDS["qw35-9b"]["model"]
    real_mmproj = A.BACKENDS["qw35-9b"]["mmproj"]
    empty_model = tmp / "empty-model.gguf"
    empty_model.write_bytes(b"")

    def env_reason(params, server_cmd=None):
        before = sorted(path.name for path in out.iterdir())
        try:
            A.run({"id": "job-env", "params": params}, tmp / "env.log",
                  server_cmd=server_cmd)
            error = None
        except BaseException as exc:  # noqa: BLE001
            error = exc
        after = sorted(path.name for path in out.iterdir())
        return getattr(error, "reason", None), error, before == after

    reason, _, clean = env_reason({"dir": str(out), "engine": str(tmp / "no-engine.exe")})
    check("env: engine_missing", reason == A.REASON_ENGINE_MISSING, str(reason))
    check("env: engine_missing leaves no caption", clean, "")
    reason, _, clean = env_reason({"dir": str(out), "model": str(tmp / "no-model.gguf"),
                                   "mmproj": real_mmproj})
    check("env: model_missing", reason == A.REASON_MODEL_MISSING, str(reason))
    reason, _, clean = env_reason({"dir": str(out), "model": real_model,
                                   "mmproj": str(tmp / "no-mmproj.gguf")})
    check("env: mmproj_missing", reason == A.REASON_MMPROJ_MISSING, str(reason))
    check("env: mmproj_missing leaves no caption", clean, "")
    reason, _, _ = env_reason({"dir": str(out), "model": str(empty_model),
                               "mmproj": real_mmproj})
    check("env: model_empty", reason == A.REASON_MODEL_EMPTY, str(reason))
    reason, _, _ = env_reason({"dir": str(out), "batch_size": 1, "startup_timeout": 10},
                              server_cmd=[sys.executable, "-c", "raise SystemExit(3)"])
    check("env: server exits -> engine_start_failed", reason == A.REASON_START_FAILED,
          str(reason))


def test_atomic_write_and_bad_inputs(tmp: Path, stub: Path):
    # atomic write: target path occupied by a directory -> write_failed, no partial file
    out = _make_images(tmp / "out-atomic", ("a", "b"))
    (out / "a.txt").mkdir()
    case = run_case(tmp, stub, "atomic", names=("a", "b"))
    metrics = (case.result or {}).get("metrics") or {}
    check("atomic: write failure -> write_failed",
          int((metrics.get("failed_codes") or {}).get("write_failed") or 0) == 1,
          json.dumps(metrics.get("failed_codes"), ensure_ascii=False))
    check("atomic: no .tmp leftover", not list(case.out.glob("*.tmp*")),
          str([p.name for p in case.out.glob("*.tmp*")]))
    check("atomic: the other image is still written", (case.out / "b.txt").is_file(), "")
    check("atomic: errors[] carries path + reason",
          any("a.txt" in item and "write_failed" in item for item in metrics.get("errors") or []),
          str((metrics.get("errors") or [])[:2]))

    # bad inputs: 0-byte / corrupt / chinese+emoji+space / long name / non-image / >50MB
    images = {
        "good1.png": b"\x89PNG\r\n\x1a\n" + b"x" * 32,
        "empty.png": b"",
        "corrupt.png": b"this is not a png",
        "中文 名😀.png": b"\x89PNG\r\n\x1a\n" + b"x" * 32,
        ("长" + "a" * 110) + ".png": b"\x89PNG\r\n\x1a\n" + b"x" * 32,
        "notes.md": b"not an image extension",
    }
    case = _run_inline(tmp, stub, "bad", "ok", images)
    metrics = (case.result or {}).get("metrics") or {}
    files = sorted(path.name for path in case.out.glob("*.txt"))
    check("bad input: 0-byte -> empty_image (continue)",
          int((metrics.get("failed_codes") or {}).get("empty_image") or 0) == 1,
          json.dumps(metrics.get("failed_codes"), ensure_ascii=False))
    check("bad input: corrupt image -> bad_image, not sent",
          int((metrics.get("failed_codes") or {}).get("bad_image") or 0) == 1
          and "corrupt.txt" not in files,
          json.dumps(metrics.get("failed_codes"), ensure_ascii=False))
    check("bad input: chinese/space/emoji filename written",
          "中文 名😀.txt" in files, str(files))
    check("bad input: very long filename written",
          any(name.startswith("长") and len(name) > 100 for name in files), str(files)[:80])
    check("bad input: non-image extension ignored",
          "notes.txt" not in files and metrics.get("images_total") == 5,
          "total=%s" % metrics.get("images_total"))
    check("bad input: the rest keeps running (written>=3)",
          int(metrics.get("written") or 0) >= 3, str(metrics.get("written")))

    # > 50 MB image -> image_too_large, and it is never sent to the server
    out = tmp / "out-big"
    out.mkdir(parents=True, exist_ok=True)
    big = out / "big.png"
    big.write_bytes(b"\x89PNG")
    os.truncate(str(big), A.MAX_IMAGE_BYTES + 1)
    reqs, args, starts = _stub_env(tmp, "big", "ok")
    try:
        result = A.run(
            {"id": "job-big", "params": {"dir": str(out), "batch_size": 1,
                                         "startup_timeout": 20, "request_timeout": 20}},
            tmp / "big.log", metrics_path=tmp / "big.metrics.json",
            server_cmd=[sys.executable, str(stub), "--port", "{port}"],
        )
    finally:
        _clear_stub_env()
    metrics = result.get("metrics") or {}
    check("bad input: >50MB -> image_too_large",
          int((metrics.get("failed_codes") or {}).get("image_too_large") or 0) == 1,
          json.dumps(metrics.get("failed_codes"), ensure_ascii=False))
    check("bad input: >50MB image is never requested", len(_requests(reqs)) == 0,
          str(len(_requests(reqs))))
    check("bad input: >50MB leaves no caption", not list(out.glob("*.txt")), "")


def test_idempotency(tmp: Path, stub: Path):
    out = _make_images(tmp / "out-idem", ("a", "b"))

    def go(tag, extra=None):
        reqs, args, starts = _stub_env(tmp, tag, "ok")
        params = {"dir": str(out), "batch_size": 1, "startup_timeout": 20,
                  "request_timeout": 20}
        params.update(extra or {})
        try:
            return A.run({"id": "job-" + tag, "params": params},
                         tmp / ("%s.log" % tag),
                         metrics_path=tmp / ("%s.metrics.json" % tag),
                         server_cmd=[sys.executable, str(stub), "--port", "{port}"])
        finally:
            _clear_stub_env()

    first = go("idem1")
    first_bytes = {path.name: path.read_bytes() for path in out.glob("*.txt")}
    second = go("idem2")
    m2 = second.get("metrics") or {}
    second_bytes = {path.name: path.read_bytes() for path in out.glob("*.txt")}
    check("idem: second run skips everything (overwrite=false)",
          m2.get("written") == 0 and m2.get("skipped") == 2,
          "written=%s skipped=%s" % (m2.get("written"), m2.get("skipped")))
    check("idem: second run is byte-identical", first_bytes == second_bytes,
          str(sorted(first_bytes) == sorted(second_bytes)))
    third = go("idem3", {"overwrite": True})
    m3 = third.get("metrics") or {}
    third_bytes = {path.name: path.read_bytes() for path in out.glob("*.txt")}
    check("idem: overwrite=true rewrites and stays byte-identical",
          m3.get("written") == 2 and third_bytes == first_bytes,
          "written=%s" % m3.get("written"))


def test_partial_failure_continues(tmp: Path, stub: Path):
    images = {"p%d.png" % index: b"\x89PNG\r\n\x1a\n" + b"x" * 16 for index in range(1, 7)}
    images["p3.png"] = b""  # the 3rd image is broken
    case = _run_inline(tmp, stub, "partial", "batch_ok", images,
                       params_extra={"batch_size": 6})
    metrics = (case.result or {}).get("metrics") or {}
    files = sorted(path.name for path in case.out.glob("*.txt"))
    check("partial: written=5 failed=1 (no rollback, no all-green)",
          metrics.get("written") == 5 and metrics.get("failed") == 1,
          "written=%s failed=%s" % (metrics.get("written"), metrics.get("failed")))
    check("partial: the 5 good images have captions",
          files == ["p1.txt", "p2.txt", "p4.txt", "p5.txt", "p6.txt"], str(files))
    check("partial: broken image counted as empty_image",
          int((metrics.get("failed_codes") or {}).get("empty_image") or 0) == 1,
          json.dumps(metrics.get("failed_codes"), ensure_ascii=False))


def test_thinking_not_off(tmp: Path, stub: Path):
    case = run_case(tmp, stub, "thinkper", mode="reasoning_on", names=("a",))
    m = (case.result or {}).get("metrics") or {}
    check("thinking: per-image reasoning -> thinking_not_off",
          m.get("thinking_not_off") is True and m.get("thinking_still_on") is True,
          json.dumps({k: m.get(k) for k in ("thinking_not_off", "reasoning_chars_total")}))
    case = run_case(tmp, stub, "thinkbatch", mode="batch_reasoning",
                    names=("a", "b"), extra={"batch_size": 6})
    m = (case.result or {}).get("metrics") or {}
    check("thinking: batch reasoning -> thinking_not_off",
          m.get("thinking_not_off") is True
          and int(m.get("reasoning_chars_total") or 0) > 0,
          json.dumps({k: m.get(k) for k in ("thinking_not_off", "reasoning_chars_total")}))


def test_signal_and_port_release(tmp: Path, stub: Path):
    """SIGINT/SIGTERM handler sets the cancel event; the port is free after exit."""
    import signal as _signal

    event = __import__("threading").Event()
    previous = A._install_signal_handlers(event)
    try:
        for name in ("SIGTERM", "SIGINT"):
            number = getattr(_signal, name, None)
            if number is None:
                continue
            handler = _signal.getsignal(number)
            if not callable(handler):
                check("signal: %s handler installed" % name, False, repr(handler))
                continue
            handler(number, None)  # Windows os.kill(SIGTERM) terminates, so call directly
            check("signal: %s handler sets the cancel event" % name, event.is_set(), "")
            event.clear()
    finally:
        A._restore_signal_handlers(previous)

    case = run_case(tmp, stub, "portrel", names=("a",))
    metrics = (case.result or {}).get("metrics") or {}
    port = metrics.get("port")
    freed = False
    if port:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", int(port)))
            freed = True
        except OSError:
            freed = False
        finally:
            probe.close()
    check("residue: listening port released after exit", freed, "port=%s" % port)


def test_task112_bad_batch_and_terminal_state(tmp: Path, stub: Path):
    """P0: bad images never enter the batch; batch 400 degrades; written=0 => failed."""
    good_png = b"\x89PNG\r\n\x1a\n" + b"x" * 32
    bad_magic = b"\x00\x00\x00\x00" + b"x" * 32

    # 1) batch request 400 -> per-image fallback for the good images, job succeeded
    case = _run_inline(tmp, stub, "b400", "batch_400", {
        "g1.png": good_png,
        "bad1.png": b"",                 # 0 bytes
        "g2.png": good_png,
        "bad2.png": bad_magic,           # wrong magic bytes
    }, params_extra={"batch_size": 6})
    m = (case.result or {}).get("metrics") or {}
    files = sorted(path.name for path in case.out.glob("*.txt"))
    check("P0: bad images skipped, good ones written via per-image fallback",
          files == ["g1.txt", "g2.txt"], str(files))
    check("P0: job succeeded because written>0",
          (case.result or {}).get("state") == "succeeded", str((case.result or {}).get("state")))
    check("P0: fallback_individual=2", int(m.get("fallback_individual") or 0) == 2,
          str(m.get("fallback_individual")))
    check("P0: batch 400 counted as one batch error",
          int(m.get("batch_request_errors") or 0) == 1, str(m.get("batch_request_errors")))

    # 2) ALL images bad -> written=0, job failed, errors only name the 2 files
    case = _run_inline(tmp, stub, "allbad", "ok", {
        "bad1.png": b"",
        "bad2.png": bad_magic,
    }, params_extra={"batch_size": 6})
    m = (case.result or {}).get("metrics") or {}
    check("P0: all-bad job is failed with the explicit message",
          (case.result or {}).get("state") == "failed"
          and str(m.get("error") or "").startswith("所有图片均失败（2 张）")
          and m.get("written") == 0,
          "state=%s error=%s" % ((case.result or {}).get("state"), m.get("error")))
    check("P0: failed_codes summary is inside error",
          "empty_image=1" in str(m.get("error") or "")
          and "bad_image=1" in str(m.get("error") or ""),
          str(m.get("error")))
    check("P0: all-bad errors name exactly the two files",
          sorted(m.get("errors") or []) == ["bad1.png: empty_image", "bad2.png: bad_image"],
          str(m.get("errors")))
    check("P0: no batch request_error for all-bad input",
          not any("batch" in str(item) for item in m.get("errors") or [])
          and int(m.get("batch_request_errors") or 0) == 0,
          str(m.get("errors")))
    check("P0: no request was sent for all-bad input",
          len(_requests(case.reqs)) == 0, str(len(_requests(case.reqs))))

    # 3) magic bytes: real PNG header replaced by 00 00 00 00 -> bad_image, zero requests
    case = _run_inline(tmp, stub, "magic", "ok", {"m.png": bad_magic},
                       params_extra={"batch_size": 1})
    m = (case.result or {}).get("metrics") or {}
    check("P0: zeroed PNG header -> bad_image",
          int((m.get("failed_codes") or {}).get("bad_image") or 0) == 1,
          json.dumps(m.get("failed_codes"), ensure_ascii=False))
    check("P0: bad magic is never requested",
          len(_requests(case.reqs)) == 0, str(len(_requests(case.reqs))))

    # 4) empty directory -> explicit failed（不是异常、也不是假成功）
    empty_dir = tmp / "empty-dir"
    empty_dir.mkdir(parents=True, exist_ok=True)
    _stub_env(tmp, "emptydir", "ok")
    try:
        result = A.run({"id": "job-empty", "params": {"dir": str(empty_dir)}},
                       tmp / "empty.log", metrics_path=tmp / "empty.metrics.json",
                       server_cmd=[sys.executable, str(stub), "--port", "{port}"])
    finally:
        _clear_stub_env()
    metrics = result.get("metrics") or {}
    check("P0: empty dir -> failed + 目录里没有可处理的图片",
          result.get("state") == "failed"
          and metrics.get("error") == "目录里没有可处理的图片"
          and metrics.get("images_total") == 0,
          "state=%s error=%s" % (result.get("state"), metrics.get("error")))
    check("P0: empty dir never starts a server",
          len(_starts(tmp / "starts-emptydir.log")) == 0,
          str(_starts(tmp / "starts-emptydir.log")))


def test_all_skipped_and_terminal_rules(tmp: Path, stub: Path):
    """P1: all-skipped must be SUCCESS and must not start llama-server (no VRAM waste).

    Negative control: delete the "written==0 && failed==0 && skipped>0 => succeeded"
    rule and the first assertion below goes red (it would be reported as failed).
    """
    out = _make_images(tmp / "out-allskip", ("a", "b", "c"))
    for name in ("a", "b", "c"):
        (out / (name + ".txt")).write_text("old caption\n", encoding="utf-8")
    reqs, args, starts = _stub_env(tmp, "allskip", "ok")
    metrics_path = tmp / "allskip.metrics.json"
    try:
        result = A.run({"id": "job-skip", "params": {"dir": str(out), "batch_size": 6}},
                       tmp / "allskip.log", metrics_path=metrics_path,
                       server_cmd=[sys.executable, str(stub), "--port", "{port}"])
    finally:
        _clear_stub_env()
    m = result.get("metrics") or {}
    check("skip: all-skipped -> succeeded (not a false red)",
          result.get("state") == "succeeded", str(result.get("state")))
    check("skip: counts are written=0 / skipped=3 / failed=0",
          m.get("written") == 0 and m.get("skipped") == 3 and m.get("failed") == 0,
          "w=%s s=%s f=%s" % (m.get("written"), m.get("skipped"), m.get("failed")))
    check("skip: message explains how to rewrite",
          "全部已有字幕" in str(m.get("message") or "")
          and "覆盖已有字幕" in str(m.get("message") or ""),
          str(m.get("message")))
    check("skip: NO llama-server started (starts=0)",
          len(_starts(starts)) == 0, str(_starts(starts)))
    check("skip: elapsed < 0.5s", float(m.get("seconds") or 0) < 0.5,
          str(m.get("seconds")))
    check("skip: metrics file also says succeeded",
          json.loads(metrics_path.read_text(encoding="utf-8")).get("state") == "succeeded",
          "")

    # overwrite=true -> one server, 3 rewrites (bytes deterministic)
    reqs2, args2, starts2 = _stub_env(tmp, "allskip-ow", "ok")
    try:
        result2 = A.run(
            {"id": "job-skip-ow",
             "params": {"dir": str(out), "batch_size": 6, "overwrite": True}},
            tmp / "allskip-ow.log", metrics_path=tmp / "allskip-ow.metrics.json",
            server_cmd=[sys.executable, str(stub), "--port", "{port}"],
        )
    finally:
        _clear_stub_env()
    m2 = result2.get("metrics") or {}
    check("skip: overwrite=true rewrites 3 and starts one server",
          m2.get("written") == 3 and len(_starts(starts2)) == 1,
          "written=%s starts=%d" % (m2.get("written"), len(_starts(starts2))))

    # partial: 2 bad + 1 good -> succeeded, written=1, failed=2
    good_png = b"\x89PNG\r\n\x1a\n" + b"x" * 32
    case = _run_inline(tmp, stub, "mix", "ok", {
        "bad1.png": b"",
        "good.png": good_png,
        "bad2.png": b"\x00\x00\x00\x00" + b"x" * 32,
    }, params_extra={"batch_size": 6})
    m = (case.result or {}).get("metrics") or {}
    check("rules: 2 bad + 1 good -> succeeded with written=1/failed=2",
          (case.result or {}).get("state") == "succeeded"
          and m.get("written") == 1 and m.get("failed") == 2,
          "state=%s w=%s f=%s" % ((case.result or {}).get("state"),
                                  m.get("written"), m.get("failed")))


def test_packaging_guard():
    tree = ast.parse(ADAPTER.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    forbidden = modules & {"llama_cpp", "torch", "transformers", "onnxruntime"}
    allowed = set(sys.stdlib_module_names) | {"__future__"}
    check("packaging: no llama_cpp/torch/transformers/onnxruntime", not forbidden, str(sorted(forbidden)))
    check("packaging: stdlib imports only", modules <= allowed, str(sorted(modules - allowed)))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="caption-selftest-"))
    try:
        stub = tmp / "stub_server.py"
        stub.write_text(STUB_SERVER, encoding="utf-8")
        test_normal(tmp, stub)
        test_thinking_leak(tmp, stub)
        test_empty_retry(tmp, stub)
        test_mid_overflow_no_reask(tmp, stub)
        test_always_long(tmp, stub)
        test_reask_threshold(tmp, stub)
        test_postprocess_layers(tmp, stub)
        test_trigger_overwrite_skip(tmp, stub)
        test_limit_ext_lang_chars(tmp, stub)
        test_cancel(tmp, stub)
        test_failures(tmp, stub)
        test_negative_controls(tmp)
        test_flags_fallback(tmp, stub)
        test_batch_mode(tmp, stub)
        test_batch_short_lines(tmp, stub)
        test_batch_extra_lines(tmp, stub)
        test_batch_reorder(tmp, stub)
        test_batch_autochunk(tmp, stub)
        test_batch_switch_and_cancel(tmp, stub)
        test_batch_reasoning(tmp, stub)
        test_timeouts_and_residue(tmp, stub)
        test_keyboard_interrupt(tmp, stub)
        test_bad_environment(tmp)
        test_atomic_write_and_bad_inputs(tmp, stub)
        test_idempotency(tmp, stub)
        test_partial_failure_continues(tmp, stub)
        test_thinking_not_off(tmp, stub)
        test_task112_bad_batch_and_terminal_state(tmp, stub)
        test_all_skipped_and_terminal_rules(tmp, stub)
        test_signal_and_port_release(tmp, stub)
        test_build_caption_job(tmp, stub)
        test_packaging_guard()
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
