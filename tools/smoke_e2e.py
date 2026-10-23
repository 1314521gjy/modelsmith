#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""smoke_e2e.py — ModelSmith delivery gate (stdlib only, no GPU).

Run:  python tools\\smoke_e2e.py
Exit 0 = all PASS; non-zero = gate failed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "core" / "modelsmith_core.py"
PY = sys.executable
WIRE = ROOT / "core" / "assets" / "ms-wire.js"
V3 = ROOT / "design" / "prototype" / "v3"
BASELINE = {
    # 2026-10-22 (task-99): v3 pages re-frozen by ui-frame-2; hashes re-computed
    # locally with Get-FileHash before being written here.
    "index.html": "7AA1EFB41F2040FDA41C471063A12A9305BA804F09A96A0D8FDE115956E3A15B",
    "image.html": "22C3330B92A545A28A2A1B68CD812ED518C150C2D816B9841428FDCB969BE269",
    "image-data.html": "4BDAFC03EE2F800911EB914007F3FF34C3A13B34F7798B8A45892F4DA1C47174",
    "image-params.html": "F2325F4B8B3840991518AC6CE245C8E3D02E60A4A74F35DA3DAE6D969A381917",
    "image-train.html": "071C600BC21D269A10F835749BEFD0B91A5C6FEA2CD691E84CF248F6A4BA2F48",
    "llm.html": "4CDD19FEE08BBDD417B33870639D0F1FA7990F7456A71479512A7E7F2FF26DC7",
    "llm-data.html": "75D6EBF7C06CF887F59A3674374AE06669FF8486E788FF035DE0BE757E602061",
    "llm-train.html": "7B22FD8E49456D1DF0ABC608496054C00948CABFA03F6AB66D0E73BAE2A7C31F",
    "model-index.js": "CD66D8877CDFEEB6B6FCB687301E5F1B88E4D0874D3A58670E5E98C77163060F",
}
OLD_INDEX_SHA = "2AF81DC0AED7BD2D1AE82514EDF4041D88B203AA7A9371E966B98D32F7C704FD"
PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS %02d - %s%s" % (PASS + FAIL, name, (" :: " + detail) if detail else ""))
    else:
        FAIL += 1
        print("FAIL %02d - %s%s" % (PASS + FAIL, name, (" :: " + detail) if detail else ""))


def http(base: str, token: str, path: str, method: str = "GET", body=None, timeout: int = 30):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"X-ModelSmith-Token": token}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, raw
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def jbody(raw: bytes):
    return json.loads(raw.decode("utf-8"))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def start_core(data_dir: Path, pick_stub: str, models_dir: Path | None = None, extra_args=None):
    data_dir.mkdir(parents=True, exist_ok=True)
    args = [PY, str(CORE), "--port", "0", "--data-dir", str(data_dir),
            "--pick-stub", pick_stub, "--no-vram-check"]
    if models_dir is not None:
        args += ["--models-dir", str(models_dir)]
    if extra_args:
        args += [str(part) for part in extra_args]
    log = open(data_dir / "core.out.log", "w", encoding="utf-8")
    err = open(data_dir / "core.err.log", "w", encoding="utf-8")
    proc = subprocess.Popen(args, cwd=str(ROOT), stdout=log, stderr=err,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    cfg = None
    for _ in range(120):
        time.sleep(0.25)
        p = data_dir / "core.json"
        if p.is_file():
            try:
                cfg = json.loads(p.read_text(encoding="utf-8"))
                if cfg.get("pid") == proc.pid:
                    break
            except Exception:
                pass
    if not cfg:
        raise RuntimeError("core did not publish core.json")
    base = cfg["base_url"]
    for _ in range(60):
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.25)
    token = (data_dir / "token").read_text(encoding="utf-8").strip()
    return proc, base, token, cfg


def stop_core(proc, port: int):
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    time.sleep(0.5)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return False
    except OSError:
        return True


def model_by(models, key: str):
    for m in models:
        if m.get("id") == key or m.get("name") == key:
            return m
    return None


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="modelsmith_smoke_e2e_"))
    fixture_pick = ROOT / "resources" / "04-生图LoRA-图集" / "smoke-synthetic"
    pick_stub = str(fixture_pick) + "|__cancel__|__no_desktop__"
    proc = None
    proc2 = None
    procv = None
    try:
        proc, base, token, cfg = start_core(tmp / "core-main", pick_stub)
        check("core health 200", True, base)

        # /api/vram (task-99): 200/ok:true fields; 401 without token; ok:false still 200.
        st, raw = http(base, token, "/api/vram")
        vr = jbody(raw) if st == 200 else {}
        check(
            "/api/vram 200 ok:true used_mib/total_mib/pct",
            st == 200
            and vr.get("ok") is True
            and isinstance(vr.get("used_mib"), int)
            and isinstance(vr.get("total_mib"), int)
            and isinstance(vr.get("pct"), (int, float)),
            "status=%s %s" % (st, json.dumps({k: vr.get(k) for k in ("ok", "used_mib", "total_mib", "pct")}, ensure_ascii=False)),
        )
        st_no = -1
        try:
            req = urllib.request.Request(base + "/api/vram", method="GET")
            with urllib.request.urlopen(req, timeout=10) as r:
                st_no = r.status
        except urllib.error.HTTPError as exc:
            st_no = exc.code
        except Exception:
            st_no = -1
        check("/api/vram without token -> 401", st_no == 401, "status=%s" % st_no)

        # /api/caption/backend: 200/backends[]; 401; bad engine -> unavailable+reason.
        st, raw = http(base, token, "/api/caption/backend")
        cb = jbody(raw) if st == 200 else {}
        backends = cb.get("backends") if isinstance(cb.get("backends"), list) else []
        check(
            "/api/caption/backend 200 ok:true backends[]",
            st == 200 and cb.get("ok") is True and bool(backends),
            "status=%s ok=%s backends=%d engine_available=%s" % (
                st, cb.get("ok"), len(backends), cb.get("engine_available")),
        )
        check(
            "/api/caption/backend backends well-formed",
            bool(backends) and all(
                isinstance(item.get("id"), str)
                and isinstance(item.get("available"), bool)
                and (item.get("available") or bool(item.get("reason")))
                for item in backends
            ),
            json.dumps(backends, ensure_ascii=False)[:260],
        )
        st_no = -1
        try:
            req = urllib.request.Request(base + "/api/caption/backend", method="GET")
            with urllib.request.urlopen(req, timeout=10) as r:
                st_no = r.status
        except urllib.error.HTTPError as exc:
            st_no = exc.code
        check("/api/caption/backend without token -> 401", st_no == 401, "status=%s" % st_no)
        # Negative control (no CLI injection exists): call the core function with a bogus engine.
        bogus_engine = str(tmp / "no-such-llama-server.exe")
        probe_code = (
            "import json, sys\n"
            "sys.path.insert(0, %r)\n"
            "import modelsmith_core as m\n"
            "print(json.dumps(m.caption_backend_status(engine=%r)))\n"
            % (str(ROOT / "core"), bogus_engine)
        )
        try:
            probe = subprocess.run([PY, "-c", probe_code], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=30)
            bogus_status = json.loads((probe.stdout or "").strip().splitlines()[-1])
        except Exception as exc:
            bogus_status = {"error": str(exc)}
        bogus_backends = bogus_status.get("backends") or []
        check(
            "negative control: bogus caption engine -> available:false + reason",
            bogus_status.get("engine_available") is False
            and bool(bogus_backends)
            and all(item.get("available") is False and bool(item.get("reason")) for item in bogus_backends),
            "engine_available=%s reason=%s" % (
                bogus_status.get("engine_available"),
                (bogus_backends[0].get("reason") if bogus_backends else bogus_status.get("error")),
            ),
        )

        # kind=caption validation + terminal response has no fake artifact path.
        caption_dir = tmp / "caption-dir"
        caption_dir.mkdir(parents=True, exist_ok=True)
        (caption_dir / "c0.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        st, raw = http(base, token, "/api/jobs", "POST", {"kind": "caption", "params": {
            "dir": str(tmp / "no-such-caption-dir")}})
        check(
            "caption: dir missing -> 400 names dir",
            st == 400 and "dir" in raw.decode("utf-8", "replace"),
            "status=%s %s" % (st, raw.decode("utf-8", "replace")[:160]),
        )
        st, raw = http(base, token, "/api/jobs", "POST", {"kind": "caption", "params": {
            "dir": str(caption_dir), "backend": "llama"}})
        check(
            "caption: backend invalid -> 400",
            st == 400 and "backend" in raw.decode("utf-8", "replace"),
            "status=%s %s" % (st, raw.decode("utf-8", "replace")[:160]),
        )
        for bad_batch in (0, 9):
            st, raw = http(base, token, "/api/jobs", "POST", {"kind": "caption", "params": {
                "dir": str(caption_dir), "batch_size": bad_batch}})
            check(
                "caption: batch_size=%d -> 400" % bad_batch,
                st == 400 and "batch_size" in raw.decode("utf-8", "replace"),
                "status=%s %s" % (st, raw.decode("utf-8", "replace")[:160]),
            )

        def submit_caption(params):
            st, raw = http(base, token, "/api/jobs", "POST", {"kind": "caption", "params": params})
            payload = jbody(raw) if st == 202 else {}
            jid = payload.get("job_id") or (payload.get("job") or {}).get("id")
            job = None
            if jid:
                for _ in range(80):
                    time.sleep(0.25)
                    st2, raw2 = http(base, token, "/api/jobs/" + urllib.parse.quote(jid))
                    if st2 == 200:
                        job = jbody(raw2).get("job", {})
                        if job.get("state") in ("succeeded", "failed", "cancelled", "interrupted"):
                            break
            return st, job

        st, capjob = submit_caption({
            "dir": str(caption_dir),
            "backend": "auto",
            "server_cmd": [str(tmp / "no-such-caption-server.exe")],
            "startup_timeout": 1,
        })
        check(
            "caption: valid params + stub -> 202 and job visible",
            st == 202 and bool(capjob),
            "status=%s state=%s" % (st, (capjob or {}).get("state")),
        )
        check(
            "caption: terminal state reached",
            bool(capjob) and capjob.get("state") in ("failed", "cancelled", "succeeded"),
            "state=%s error=%s" % ((capjob or {}).get("state"), str((capjob or {}).get("error"))[:120]),
        )
        capjob_text = json.dumps(capjob or {}, ensure_ascii=False)
        check(
            "caption: response has no .safetensors literal",
            bool(capjob) and ".safetensors" not in capjob_text,
            "matches=%d" % capjob_text.count(".safetensors"),
        )
        # Static: caption engine paths must not be hardcoded in the wire JS.
        wire_text = WIRE.read_text(encoding="utf-8", errors="replace")
        hardcoded = wire_text.count("llama-cpp") + wire_text.count("ComfyUI")
        check(
            "wire.js: no llama-cpp/ComfyUI hardcode",
            hardcoded == 0,
            "llama-cpp=%d ComfyUI=%d" % (wire_text.count("llama-cpp"), wire_text.count("ComfyUI")),
        )
        check(
            "wire.js: has initCaptionEdit",
            wire_text.count("initCaptionEdit") >= 1,
            "count=%d" % wire_text.count("initCaptionEdit"),
        )
        post_captions = re.search(
            r'api\(\s*"/dataset/captions"\s*,\s*\{[^}]*method\s*:\s*"POST"',
            wire_text,
            re.S,
        )
        check(
            'wire.js: POSTs "/dataset/captions"',
            post_captions is not None,
            "matches=%d" % (1 if post_captions else 0),
        )
        # GET /api/dataset/captions (task: caption edit panel data source)
        st, raw = http(base, token, "/api/dataset/captions?" + urllib.parse.urlencode({"dir": str(caption_dir)}))
        cap_listing = jbody(raw) if st == 200 else {}
        check(
            "GET /api/dataset/captions 200 + items[]",
            st == 200 and isinstance(cap_listing.get("items"), list),
            "status=%s items=%s" % (st, type(cap_listing.get("items")).__name__),
        )
        status, raw = http(base, token, "/api/models?refresh=1")
        models = jbody(raw).get("models", []) if status == 200 else []
        check("GET /api/models 200", status == 200, "count=%d" % len(models))

        for mid in ("z-image-turbo", "moss-image-sft"):
            m = model_by(models, mid)
            check("model %s complete=true missing=[]" % mid,
                  bool(m) and m.get("complete") is True and not m.get("missing"),
                  json.dumps({"found": bool(m), "complete": (m or {}).get("complete"), "missing": (m or {}).get("missing")}, ensure_ascii=False))
        mz = model_by(models, "z-image")
        miss = set((mz or {}).get("missing") or [])
        check("model z-image linked complete=true",
              bool(mz) and mz.get("complete") is True and mz.get("linked") is True
              and mz.get("size_bytes", 0) >= 18 * 1024**3
              and set((mz.get("link_targets") or {}).keys()) == {"scheduler", "text_encoder", "tokenizer", "vae"},
              json.dumps({"complete": (mz or {}).get("complete"), "linked": (mz or {}).get("linked"),
                          "size": (mz or {}).get("size_bytes"), "targets": (mz or {}).get("link_targets")}, ensure_ascii=False))
        qm = model_by(models, "qwen-image-2.1")
        check("model qwen-image-2.1 linked complete=true",
              bool(qm) and qm.get("complete") is True and qm.get("linked") is True
              and qm.get("size_bytes", 0) >= 30 * 1024**3,
              json.dumps({"complete": (qm or {}).get("complete"), "linked": (qm or {}).get("linked"),
                          "size": (qm or {}).get("size_bytes")}, ensure_ascii=False))

        # Negative control: temp fixture root; rename text_encoder, rescan.
        fixture_root = tmp / "fixture-models"
        fm = fixture_root / "fixture-complete"
        (fm / "text_encoder").mkdir(parents=True)
        (fm / "vae").mkdir()
        (fm / "tokenizer").mkdir()
        (fm / "model_index.json").write_text(json.dumps({"_class_name": "FixturePipeline", "text_encoder": ["x", "y"], "vae": ["x", "y"], "tokenizer": ["x", "y"]}), encoding="utf-8")
        proc2, base2, token2, cfg2 = start_core(tmp / "core-fixture", str(fixture_pick) + "|__cancel__|__no_desktop__", fixture_root)
        st, raw = http(base2, token2, "/api/models?refresh=1")
        mf = model_by(jbody(raw).get("models", []) if st == 200 else [], "fixture-complete")
        check("fixture complete before rename", bool(mf) and mf.get("complete") is True, json.dumps(mf, ensure_ascii=False))
        (fm / "text_encoder").rename(fm / "text_encoder_off")
        st, raw = http(base2, token2, "/api/models?refresh=1")
        mf2 = model_by(jbody(raw).get("models", []) if st == 200 else [], "fixture-complete")
        check("negative control: renamed text_encoder -> missing names it",
              bool(mf2) and mf2.get("complete") is False and "text_encoder" in (mf2.get("missing") or []),
              json.dumps({"complete": (mf2 or {}).get("complete"), "missing": (mf2 or {}).get("missing")}, ensure_ascii=False))
        (fm / "text_encoder_off").rename(fm / "text_encoder")

        # Negative control on the REAL complete model: rename text_encoder, rescan, restore.
        real_te = ROOT / "models" / "z-image-turbo" / "text_encoder"
        real_off = ROOT / "models" / "z-image-turbo" / "text_encoder_smoke_off"
        real_ok = real_te.is_dir()
        if real_ok:
            try:
                real_te.rename(real_off)
                st, raw = http(base, token, "/api/models?refresh=1")
                mr = model_by(jbody(raw).get("models", []) if st == 200 else [], "z-image-turbo")
                check("negative control: z-image-turbo text_encoder renamed -> missing names it",
                      bool(mr) and mr.get("complete") is False and "text_encoder" in (mr.get("missing") or []),
                      json.dumps({"complete": (mr or {}).get("complete"), "missing": (mr or {}).get("missing")}, ensure_ascii=False))
            finally:
                if real_off.is_dir() and not real_te.exists():
                    real_off.rename(real_te)
        else:
            check("negative control: z-image-turbo text_encoder renamed -> missing names it", False, "text_encoder missing before test")

        # Dataset without metadata.csv: generate under OUT, never write the user dir.
        sys.path.insert(0, str(ROOT))
        from adapters import diffsynth_adapter as da
        ds = ROOT / "_tmp" / "ds-nocsv"
        if ds.is_dir():
            before_files = sorted(p.name for p in ds.iterdir() if p.is_file())
            prep = da.prepare_dataset_metadata({"dataset": str(ds)}, tmp / "dataset-out")
            after_files = sorted(p.name for p in ds.iterdir() if p.is_file())
            raw_csv = Path(prep["path"]).read_bytes()
            check("dataset no-csv: generated=true n=4 txt=2 default=2",
                  prep.get("generated") is True and prep.get("n") == 4 and prep.get("from_txt") == 2 and prep.get("from_default") == 2,
                  json.dumps({k: prep.get(k) for k in ("generated", "n", "from_txt", "from_default")}, ensure_ascii=False))
            check("dataset no-csv: CSV has no UTF-8 BOM", raw_csv[:3] != b"\xef\xbb\xbf", raw_csv[:3].hex())
            check("dataset no-csv: user dir not written", before_files == after_files and not (ds / "metadata.csv").exists(),
                  "before=%d after=%d" % (len(before_files), len(after_files)))
        else:
            check("dataset no-csv fixture exists", False, str(ds))

        empty_ds = tmp / "empty-ds"
        empty_ds.mkdir()
        try:
            da.prepare_dataset_metadata({"dataset": str(empty_ds)}, tmp / "empty-out")
            neg_empty = "no exception"
        except ValueError as exc:
            neg_empty = str(exc)
        check("negative control: empty dataset -> no images under", "no images under" in neg_empty, neg_empty)

        fixture_bad = fixture_root / "fixture-incomplete"
        (fixture_bad / "transformer").mkdir(parents=True, exist_ok=True)
        (fixture_bad / "model_index.json").write_text(
            json.dumps({"_class_name": "BadPipeline", "transformer": ["x", "y"],
                        "text_encoder": ["x", "y"], "vae": ["x", "y"]}),
            encoding="utf-8",
        )
        http(base2, token2, "/api/models?refresh=1")
        body = {"kind": "diffsynth", "params": {"arch": "z_image", "model": str(fixture_bad),
                "dataset": str(fixture_pick), "out": str(tmp / "runs" / "bad"),
                "height": 64, "width": 64}}
        st, raw = http(base2, token2, "/api/jobs", "POST", body)
        text = raw.decode("utf-8", "replace")
        check("incomplete fixture submit -> 400 names missing",
              st == 400 and ("missing" in text or "text_encoder" in text or "vae" in text),
              "status=%s %s" % (st, text[:240]))
        # Complete model + stub 202 (no GPU)
        complete = model_by(models, "z-image-turbo") or {}
        cbody = {"kind": "diffsynth", "params": {"arch": "z_image", "model": complete.get("path", ""), "dataset": str(fixture_pick), "out": str(tmp / "runs" / "ok"), "height": 64, "width": 64, "train_command": [PY, "-c", "import time; time.sleep(2)"]}}
        st, raw = http(base, token, "/api/jobs", "POST", cbody)
        ctext = raw.decode("utf-8", "replace")
        check("complete model + stub -> 202", st == 202, "status=%s %s" % (st, ctext[:200]))
        if st == 202:
            cj = jbody(raw)
            jid = cj.get("job_id") or (cj.get("job") or {}).get("id")
            if jid:
                http(base, token, "/api/jobs/" + urllib.parse.quote(jid) + "/cancel", "POST", {})

        # Artifacts truthfulness (task-99): out only non-artifacts + a stale 30-day
        # epoch-9.safetensors -> artifacts [] and no ".safetensors" literal anywhere.
        art_out = tmp / "runs" / "artifacts"
        art_out.mkdir(parents=True, exist_ok=True)
        (art_out / "training_args.json").write_text("{}", encoding="utf-8")
        stale = art_out / "epoch-9.safetensors"
        stale.write_bytes(b"\x00" * 32)
        old_ts = time.time() - 30 * 86400
        os.utime(stale, (old_ts, old_ts))
        art_body = {"kind": "diffsynth", "params": {
            "arch": "z_image", "model": complete.get("path", ""), "dataset": str(fixture_pick),
            "out": str(art_out), "height": 64, "width": 64,
            "train_command": [PY, "-c", "import time; time.sleep(1)"]}}
        st, raw = http(base, token, "/api/jobs", "POST", art_body)
        aj = jbody(raw) if st == 202 else {}
        ajid = aj.get("job_id") or (aj.get("job") or {}).get("id")
        ajob = None
        if ajid:
            for _ in range(80):
                time.sleep(0.25)
                st2, raw2 = http(base, token, "/api/jobs/" + urllib.parse.quote(ajid))
                if st2 == 200:
                    ajob = jbody(raw2).get("job", {})
                    if ajob.get("state") in ("succeeded", "failed", "cancelled", "interrupted"):
                        break
        ametrics = (ajob or {}).get("metrics") or {}
        check(
            "artifacts: non-artifact out + stale epoch -> []",
            bool(ajob) and ajob.get("state") == "succeeded" and ametrics.get("artifacts") == [],
            "state=%s artifacts=%s" % ((ajob or {}).get("state"), json.dumps(ametrics.get("artifacts"), ensure_ascii=False)),
        )
        ajob_text = json.dumps(ajob or {}, ensure_ascii=False)
        # argv legitimately carries model shard paths (*.safetensors); the fake-path
        # regression is about artifact surfaces, so compare everything except argv.
        ajob_surface = json.loads(ajob_text) if ajob is not None else {}
        if isinstance(ajob_surface.get("metrics"), dict):
            ajob_surface["metrics"].pop("argv", None)
        surface_text = json.dumps(ajob_surface, ensure_ascii=False)
        check(
            "artifacts: no .safetensors literal (argv excluded)",
            bool(ajob) and ".safetensors" not in surface_text,
            "matches=%d" % surface_text.count(".safetensors"),
        )
        check(
            "artifacts: stale 30-day epoch-9 not listed",
            bool(ajob) and not any("epoch-9" in json.dumps(item, ensure_ascii=False) for item in (ametrics.get("artifacts") or [])),
            json.dumps(ametrics.get("artifacts"), ensure_ascii=False),
        )

        # No-caption warning (task-99): metrics.no_captions / no_captions_images,
        # nested dataset_metadata.no_captions, warnings stays list[str].
        def run_stub_job(label: str, dataset_dir: Path):
            out_dir = tmp / "runs" / label
            body = {"kind": "diffsynth", "params": {
                "arch": "z_image", "model": complete.get("path", ""), "dataset": str(dataset_dir),
                "out": str(out_dir), "height": 64, "width": 64,
                "train_command": [PY, "-c", "import time; time.sleep(1)"]}}
            st, raw = http(base, token, "/api/jobs", "POST", body)
            payload = jbody(raw) if st == 202 else {}
            jid = payload.get("job_id") or (payload.get("job") or {}).get("id")
            job = None
            if jid:
                for _ in range(80):
                    time.sleep(0.25)
                    st2, raw2 = http(base, token, "/api/jobs/" + urllib.parse.quote(jid))
                    if st2 == 200:
                        job = jbody(raw2).get("job", {})
                        if job.get("state") in ("succeeded", "failed", "cancelled", "interrupted"):
                            break
            return job

        caps_zero = tmp / "caps-zero"
        caps_zero.mkdir(parents=True, exist_ok=True)
        for index in range(2):
            (caps_zero / ("p%d.png" % index)).write_bytes(b"\x89PNG\r\n\x1a\n")
        job_zero = run_stub_job("caps-zero", caps_zero)
        m_zero = (job_zero or {}).get("metrics") or {}
        nested_zero = (m_zero.get("dataset_metadata") or {})
        check(
            "no_captions: 0 .txt -> metrics.no_captions true",
            bool(job_zero) and job_zero.get("state") == "succeeded" and m_zero.get("no_captions") is True,
            "state=%s no_captions=%s" % ((job_zero or {}).get("state"), m_zero.get("no_captions")),
        )
        check(
            "no_captions: no_captions_images >= 1",
            isinstance(m_zero.get("no_captions_images"), int) and m_zero.get("no_captions_images") >= 1,
            str(m_zero.get("no_captions_images")),
        )
        check(
            "no_captions: nested dataset_metadata.no_captions true",
            nested_zero.get("no_captions") is True,
            json.dumps(nested_zero, ensure_ascii=False)[:200],
        )
        check(
            "no_captions: warnings stays list[str]",
            isinstance(m_zero.get("warnings"), list)
            and all(isinstance(item, str) for item in (m_zero.get("warnings") or []))
            and any(item.startswith("no_captions:") for item in (m_zero.get("warnings") or [])),
            json.dumps(m_zero.get("warnings"), ensure_ascii=False)[:240],
        )
        caps_full = tmp / "caps-full"
        caps_full.mkdir(parents=True, exist_ok=True)
        for index in range(2):
            (caps_full / ("p%d.png" % index)).write_bytes(b"\x89PNG\r\n\x1a\n")
            (caps_full / ("p%d.txt" % index)).write_text("a caption", encoding="utf-8")
        job_full = run_stub_job("caps-full", caps_full)
        m_full = (job_full or {}).get("metrics") or {}
        check(
            "negative control: captions present -> no_captions false",
            bool(job_full) and job_full.get("state") == "succeeded" and m_full.get("no_captions") is False,
            "state=%s no_captions=%s images=%s" % ((job_full or {}).get("state"), m_full.get("no_captions"), m_full.get("no_captions_images")),
        )

        # /api/pick dir three states (stub sequence)
        st, raw = http(base, token, "/api/pick", "POST", {"mode": "dir"})
        p1 = jbody(raw) if st == 200 else {}
        check("/pick dir ok path", st == 200 and p1.get("ok") is True and str(p1.get("path")) == str(fixture_pick), json.dumps(p1, ensure_ascii=False))
        st, raw = http(base, token, "/api/pick", "POST", {"mode": "dir"})
        p2 = jbody(raw) if st == 200 else {}
        check("/pick cancel", st == 200 and p2.get("cancelled") is True, json.dumps(p2, ensure_ascii=False))
        st, raw = http(base, token, "/api/pick", "POST", {"mode": "dir"})
        p3 = jbody(raw) if st == 200 else {}
        check("/pick no_desktop", st == 200 and p3.get("ok") is False and p3.get("reason") == "no_desktop", json.dumps(p3, ensure_ascii=False))

        # Fake job full chain
        st, raw = http(base, token, "/api/jobs", "POST", {"kind": "fake", "params": {"steps": 2, "interval_s": 0.05}})
        fj = jbody(raw) if st == 202 else {}
        fjd = fj.get("job_id") or (fj.get("job") or {}).get("id")
        check("fake job accepted 202", st == 202 and bool(fjd), "status=%s" % st)
        fstate = None
        if fjd:
            for _ in range(80):
                time.sleep(0.25)
                st2, raw2 = http(base, token, "/api/jobs/" + urllib.parse.quote(fjd))
                if st2 == 200:
                    fstate = jbody(raw2).get("job", {}).get("state")
                    if fstate in ("succeeded", "failed", "cancelled", "interrupted"):
                        break
        check("fake job succeeded", fstate == "succeeded", str(fstate))
        if fjd:
            st, raw = http(base, token, "/api/jobs/" + urllib.parse.quote(fjd) + "/metrics_history?since=0")
            rows = jbody(raw).get("rows", []) if st == 200 else []
            check("metrics_history has rows", st == 200 and len(rows) > 0, "rows=%d" % len(rows))
            st, raw = http(base, token, "/api/jobs/" + urllib.parse.quote(fjd) + "/logs?offset=0")
            logs = raw.decode("utf-8", "replace") if st == 200 else ""
            check("job logs non-empty", st == 200 and len(logs.strip()) > 0, "bytes=%d" % len(logs))

        # UI static
        st, raw = http(base, token, "/")
        html = raw.decode("utf-8", "replace") if st == 200 else ""
        check("GET / 200 + token + wire injection", st == 200 and "__MS_TOKEN__" in html and "/ms-wire.js" in html, "status=%s bytes=%d" % (st, len(raw)))
        st, raw = http(base, token, "/ms-wire.js")
        check("GET /ms-wire.js bytes == disk", st == 200 and raw == WIRE.read_bytes(), "status=%s bytes=%d disk=%d" % (st, len(raw), WIRE.stat().st_size))
        bad = []
        for name, want in BASELINE.items():
            got = sha256(V3 / name)
            if got != want:
                bad.append(name)
        check("v3 nine SHA baseline", not bad, "bad=" + ",".join(bad) if bad else "9/9")
        check(
            "negative control: old v3 baseline hash would fail",
            sha256(V3 / "index.html") != OLD_INDEX_SHA,
            "old=%s now=%s" % (OLD_INDEX_SHA[:12], sha256(V3 / "index.html")[:12]),
        )
        # Static regressions for the home-page layout : the old
        # "hover grows -> grid squeezed" fingerprint must not come back.
        index_text = (V3 / "index.html").read_text(encoding="utf-8", errors="replace")
        check(
            "v3 index.html: no 1.42fr hover fingerprint",
            index_text.count("1.42fr") == 0,
            "count=%d" % index_text.count("1.42fr"),
        )
        has_grid_rules = re.findall(r":has\([^)]*\)\s*\{[^}]*grid-template", index_text)
        check(
            "v3 index.html: no :has(...){grid-template} rule",
            not has_grid_rules,
            "matches=%d" % len(has_grid_rules),
        )
        # image-train.html: sample thumbnails must keep cover fit + lightbox entry.
        train_text = (V3 / "image-train.html").read_text(encoding="utf-8", errors="replace")
        cover_rules = re.findall(
            r"\.shot__img[^{}]*\{[^}]*background-size\s*:\s*cover", train_text
        )
        check(
            "v3 image-train.html: .shot__img has background-size:cover",
            bool(cover_rules),
            "matches=%d cover_occurrences=%d" % (len(cover_rules), train_text.count("background-size:cover")),
        )
        check(
            'v3 image-train.html: has id="msLightbox"',
            train_text.count('id="msLightbox"') >= 1,
            "count=%d" % train_text.count('id="msLightbox"'),
        )
        # image-train: 4 stat cells (5th "VRAM card" was removed) + two meters.
        stats_match = re.search(r'class="stats"[^>]*>(.*?)<div class="rounds"', train_text, re.S)
        cell_count = len(re.findall(r'<div class="cell"', stats_match.group(1))) if stats_match else -1
        check(
            "v3 image-train.html: .stats has 4 cells",
            stats_match is not None and cell_count == 4,
            "cells=%d" % cell_count,
        )
        check(
            'v3 image-train.html: has id="vramBar" once',
            train_text.count('id="vramBar"') == 1,
            "count=%d" % train_text.count('id="vramBar"'),
        )
        check(
            'v3 image-train.html: has id="ramBar" once',
            train_text.count('id="ramBar"') == 1,
            "count=%d" % train_text.count('id="ramBar"'),
        )
        # image-data.html: caption edit panel ids must exist exactly once each.
        data_text = (V3 / "image-data.html").read_text(encoding="utf-8", errors="replace")
        for caption_id in ("captionEditPanel", "captionEditList", "captionEditSaveAll"):
            check(
                'v3 image-data.html: has id="%s" once' % caption_id,
                data_text.count('id="%s"' % caption_id) == 1,
                "count=%d" % data_text.count('id="%s"' % caption_id),
            )

        # Terminal stability: stub exits 1 -> failed, state stable 2 s
        if complete.get("path"):
            tbody = {"kind": "diffsynth", "params": {"arch": "z_image", "model": complete["path"], "dataset": str(fixture_pick), "out": str(tmp / "runs" / "fail"), "height": 64, "width": 64, "train_command": [PY, "-c", "import sys; sys.exit(1)"]}}
            st, raw = http(base, token, "/api/jobs", "POST", tbody)
            tj = jbody(raw) if st == 202 else {}
            tjd = tj.get("job_id") or (tj.get("job") or {}).get("id")
            if tjd:
                last = None
                for _ in range(80):
                    time.sleep(0.25)
                    st2, raw2 = http(base, token, "/api/jobs/" + urllib.parse.quote(tjd))
                    if st2 == 200:
                        last = jbody(raw2).get("job", {})
                        if last.get("state") in ("failed", "succeeded", "cancelled", "interrupted"):
                            break
                before = json.dumps(last, sort_keys=True, ensure_ascii=False)
                time.sleep(2.0)
                st3, raw3 = http(base, token, "/api/jobs/" + urllib.parse.quote(tjd))
                after = json.dumps(jbody(raw3).get("job", {}), sort_keys=True, ensure_ascii=False) if st3 == 200 else before + "x"
                check("terminal state stable 2 s", bool(last) and before == after, "state=%s" % ((last or {}).get("state")))
            else:
                check("terminal failure job accepted", False, "status=%s" % st)
        else:
            check("terminal failure job accepted", False, "no complete z-image-turbo path")

        # Negative control: bogus --vram-cmd -> /api/vram ok:false but HTTP 200.
        bad_core_dir = tmp / "core-badvram"
        bogus_cmd = str(tmp / "no-such-nvidia-smi.exe")
        procv, basev, tokenv, cfgv = start_core(
            bad_core_dir,
            str(fixture_pick) + "|__cancel__|__no_desktop__",
            extra_args=["--vram-cmd", bogus_cmd],
        )
        stv, rawv = http(basev, tokenv, "/api/vram")
        vrv = jbody(rawv) if stv == 200 else {}
        check(
            "/api/vram ok:false still HTTP 200",
            stv == 200 and vrv.get("ok") is False and bool(vrv.get("reason")),
            "status=%s %s" % (stv, json.dumps(vrv, ensure_ascii=False)[:200]),
        )
        stop_core(procv, int(urllib.parse.urlsplit(basev).port))
        procv = None

        # shutdown + listener check
        port = int(urllib.parse.urlsplit(base).port)
        ok = stop_core(proc, port)
        proc = None
        check("core stopped, 0 listener", ok, "port=%d" % port)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        check("smoke harness exception", False, str(exc))
    finally:
        if proc is not None:
            try:
                stop_core(proc, int(urllib.parse.urlsplit(base).port))
            except Exception:
                pass
        if proc2 is not None:
            try:
                stop_core(proc2, int(urllib.parse.urlsplit(base2).port))
            except Exception:
                pass
        if procv is not None:
            try:
                stop_core(procv, int(urllib.parse.urlsplit(basev).port))
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)
    print("RESULT: PASS %d / FAIL %d" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
