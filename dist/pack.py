#!/usr/bin/env python3
"""ModelSmith distribution packer — two zips, **no model weights**.

Builds, under ``dist/out/``:

  * ``ModelSmith-方案-<YYYYMMDD>.zip``  (main pack: UI + core + adapters + docs
    + review + recipes + DiffSynth-Studio source + third-party LICENSE list)
  * ``ModelSmith-数据包-<YYYYMMDD>.zip`` (data pack: resources 01/02/04 + notes)

Both zips carry ``PACK-MANIFEST.json`` (rel / bytes / SHA256 / group) and
``PACK-README.md`` (purpose, what is missing, licence note).

The single source of truth is ``dist/README.md``.

Hard exclusions abort the run (never silently skipped):
    _tmp/ _runs/ .aitk-pylibs/ .pylibs-lf/ .venv-aitk/
    resources/03-LLM-小模型/ resources/07-底模/
    third_party/laya/ third_party/ai-toolkit/ third_party/LlamaFactory/
    third_party/sd-scripts/  *.safetensors  *.gguf
Generated material is pruned and *reported*:
    __pycache__/ *.pyc  (维护者 rule)   dist/out/  (pack output)
    core|adapters/_tmp (task-37)     .git/ (VCS metadata)
    _latent_cache/ _t_e_cache/ (generated training caches, not weights)

Usage (stdlib only; no GPU, no downloads, no installs):

    python dist\\pack.py --dry-run          # plan only, writes nothing
    python dist\\pack.py                    # build both zips
    python dist\\pack.py --selftest-exclude # negative control for the scanner
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = PROJECT_ROOT / "dist"
OUT_DIR = DIST_DIR / "out"
DATE_STAMP = time.strftime("%Y%m%d")
MAIN_ZIP_NAME = "ModelSmith-方案-%s.zip" % DATE_STAMP
DATA_ZIP_NAME = "ModelSmith-数据包-%s.zip" % DATE_STAMP
ZIP_DATE_TIME = (2026, 10, 22, 0, 0, 0)
COMPRESS_LEVEL = 9
VERSION = "1.0"

# ---------------------------------------------------------------------------
# distribution-time sanitization (memory only; source files on disk are never
# modified).  Placeholders are documented in dist/PACK-README.md.
# ---------------------------------------------------------------------------

SANITIZE_EXTS = {".md", ".py", ".js", ".html", ".json", ".cmd", ".sh", ".txt", ".yaml"}
PLACEHOLDER_ROOT = "<包根>"
PLACEHOLDER_EXTERNAL = "<外部路径>"
PLACEHOLDER_USER = "<用户目录>"
PLACEHOLDER_PROXY = "<本机代理>"

# Applied in order.  JSON files store "\\" so allow 1..3 slashes per separator.
SANITIZE_RULES = [
    ("package_root", re.compile(r"E:[\\/]{1,3}视频[\\/]{1,3}modelsmith", re.I), PLACEHOLDER_ROOT),
    ("external_path", re.compile(r"E:[\\/]{1,3}视频", re.I), PLACEHOLDER_EXTERNAL),
    ("user_home", re.compile(r"C:[\\/]{1,3}Users[\\/]{1,3}Administrator", re.I), PLACEHOLDER_USER),
    ("user_home_any", re.compile(r"C:[\\/]{1,3}Users[\\/]{1,3}[^\\/\s\"'<>]+", re.I), PLACEHOLDER_USER),
    ("any_e_path", re.compile(r"E:[\\/]{1,3}[^\s\"'<>]*", re.I), PLACEHOLDER_EXTERNAL),
    ("local_proxy", re.compile(r"127\.0\.0\.1:12334"), PLACEHOLDER_PROXY),
]

LEAK_PATTERNS = [
    ("E-drive-abs", re.compile(r"E:[\\/]")),
    ("windows-user", re.compile(r"C:[\\/]{1,2}Users[\\/]")),
    ("local-proxy", re.compile(r"127\.0\.0\.1:12334")),
]


def sanitize_text(text: str):
    """Return (sanitized_text, {rule: replaced_count}); never mutates input."""
    stats = {}
    result = text
    for label, pattern, replacement in SANITIZE_RULES:
        result, count = pattern.subn(replacement, result)
        if count:
            stats[label] = stats.get(label, 0) + count
    return result, stats


def merge_stats(total: dict, stats: dict) -> None:
    for key, value in stats.items():
        total[key] = total.get(key, 0) + value


def is_text_entry(name: str) -> bool:
    lowered = name.lower()
    if any(lowered.endswith(ext) for ext in SANITIZE_EXTS):
        return True
    return lowered in {"pack-manifest.json", "pack-readme.md", "runtime-readme.md"}


def scan_zip_leaks(zip_path: Path) -> list:
    """Return [{entry, pattern, line, snippet}] for absolute-path leaks."""
    leaks = []
    with zipfile.ZipFile(zip_path) as archive:
        for name in archive.namelist():
            if not is_text_entry(name):
                continue
            try:
                text = archive.read(name).decode("utf-8")
            except UnicodeDecodeError:
                continue
            for label, pattern in LEAK_PATTERNS:
                for match in pattern.finditer(text):
                    line = text.count("\n", 0, match.start()) + 1
                    snippet = (
                        text[max(0, match.start() - 40): match.end() + 40]
                        .replace("\r", " ")
                        .replace("\n", " ")
                    )
                    leaks.append({"entry": name, "pattern": label, "line": line, "snippet": snippet})
    return leaks


BASELINE_PATH = OUT_DIR / ".pack-baseline.json"


def load_pack_baseline() -> dict:
    try:
        data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_pack_baseline(label: str, entry: dict) -> None:
    baseline = load_pack_baseline()
    packs = baseline.setdefault("packs", {})
    packs[label] = entry
    baseline["recorded_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    baseline["note"] = (
        "entries / uncompressed zip bytes / sanitize replacements; "
        "size guard = >20% deviation from these bytes needs --force-size"
    )
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = BASELINE_PATH.with_suffix(BASELINE_PATH.suffix + ".tmp")
    temporary.write_text(json.dumps(baseline, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(temporary, BASELINE_PATH)


def zip_uncompressed_bytes(zip_path: Path) -> int:
    with zipfile.ZipFile(zip_path) as archive:
        return sum(info.file_size for info in archive.infolist())


def check_size_guard(label: str, planned_bytes: int, force_size: bool) -> None:
    entry = load_pack_baseline().get("packs", {}).get(label)
    if not isinstance(entry, dict) or not entry.get("uncompressed_bytes"):
        print("  size guard: no baseline for %s (recorded on next build)" % label)
        return
    base = int(entry["uncompressed_bytes"])
    if base <= 0:
        return
    deviation = (planned_bytes - base) * 100.0 / base
    print(
        "  size guard: planned=%s baseline=%s deviation=%+.1f%%"
        % (human_mb(planned_bytes), human_mb(base), deviation)
    )
    if abs(deviation) > 20.0 and not force_size:
        raise SystemExit(
            "ABORT: pack size deviation %+.1f%% exceeds 20%% (baseline %s). "
            "Review the file diff, then rerun with --force-size." % (deviation, human_mb(base))
        )


def scan_tree_leaks(root: Path) -> list:
    leaks = []
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file() or not is_text_entry(path.name):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for label, pattern in LEAK_PATTERNS:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                snippet = (
                    text[max(0, match.start() - 40): match.end() + 40]
                    .replace("\r", " ")
                    .replace("\n", " ")
                )
                leaks.append({"entry": str(path), "pattern": label, "line": line, "snippet": snippet})
    return leaks



# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------
# rel paths use "/" and are relative to PROJECT_ROOT.

MAIN_SELECTION = [
    {"rel": "core", "group": "core", "required": True},
    {"rel": "adapters", "group": "adapters", "required": True},
    {"rel": "design/prototype/index.html", "group": "design", "required": True},
    {"rel": "design/art", "group": "design", "required": True},
    {"rel": "design/ART-NOTES.md", "group": "design", "required": True},
    {"rel": "design/DESIGN-SPEC.md", "group": "design", "required": True},
    
    {"rel": "docs", "group": "docs", "required": True},
    {"rel": "review", "group": "review", "required": True},
    {"rel": "dist/README.md", "group": "dist", "required": True},
    {"rel": "dist/collect.py", "group": "dist", "required": True},
    {"rel": "dist/pack.py", "group": "dist", "required": True},
    {"rel": "dist/manifest.json", "group": "dist", "required": True},
    {"rel": "dist/PACK-README.md", "group": "dist", "required": False},
    {"rel": "dist/RUNTIME-PACK.md", "group": "runtime", "required": True},
    {"rel": "dist/pack_runtime.py", "group": "runtime", "required": True},
    {"rel": "dist/runtime-licenses", "group": "runtime", "required": True},
    {"rel": "dist/start-core.cmd", "group": "runtime", "required": True, "arc": "start-core.cmd"},
    {"rel": "recipes", "group": "recipes", "required": True},
    {"rel": "skills", "group": "skills", "required": True},
    {"rel": "tools", "group": "tools", "required": True},
    {"rel": "third_party/DiffSynth-Studio", "group": "third_party", "required": True},
    {"rel": "third_party/MANIFEST.md", "group": "third_party", "required": True},
    {"rel": "third_party/LICENSES", "group": "third_party", "required": True},
    {"rel": "README.md", "group": "root", "required": False},
]

RUNTIME_ENTRY_CHECKS = [
    "start-core.cmd",
    "dist/RUNTIME-PACK.md",
    "dist/pack_runtime.py",
    "dist/runtime-licenses/tokenizers/LICENSE",
    "dist/runtime-licenses/sentencepiece/LICENSE",
    "dist/runtime-licenses/antlr4/LICENSE",
]

DATA_SELECTION = [
    {"rel": "resources/01-决策模型-业务语料", "group": "data-01", "required": True},
    {"rel": "resources/02-LLM-语料", "group": "data-02", "required": True},
    {"rel": "resources/04-生图LoRA-图集", "group": "data-04", "required": True},
    {"rel": "resources/README.md", "group": "data-common", "required": False},
    {"rel": "resources/00-底座与权重-指针.md", "group": "data-common", "required": False},
    {"rel": "resources/06-清单与说明", "group": "data-common", "required": True},
]

# Paths (relative) matched here are pruned and reported, never packed.
PRUNE_RULES = [
    (re.compile(r"^(core|adapters)/_tmp(/|$)"), "_tmp under core/adapters (task-37)"),
    (re.compile(r"(^|/)__pycache__(/|$)"), "generated bytecode __pycache__ (维护者 rule)"),
    (re.compile(r"\.py[co]$"), "generated bytecode *.pyc (维护者 rule)"),
    (re.compile(r"(^|/)\.git(/|$)"), "VCS metadata (.git)"),
    (re.compile(r"(^|/)\.github(/|$)"), "GitHub metadata (.github)"),
    (re.compile(r"(^|/)\.pytest_cache(/|$)"), "test cache (.pytest_cache)"),
    (re.compile(r"^dist/runtime-src(/|$)"), "runtime source bundle (ships in the runtime pack, not the plan pack)"),
    (re.compile(r"\.ipynb$"), "notebook (*.ipynb) is never shipped"),
    (re.compile(r"\.gif$"), "animated gif (*.gif) is never shipped"),
    (
        re.compile(r"(^|/)(_latent_cache|_t_e_cache)(/|$)"),
        "generated training cache dir (excluded by directory name; "
        "the *.safetensors hard rule has no exception)",
    ),
    (re.compile(r"^dist/out(/|$)"), "pack output itself (dist/out)"),
]

# Any file whose rel path matches these is a hard failure.
HARD_SEGMENTS = {
    "_tmp",
    "_runs",
    ".aitk-pylibs",
    ".pylibs-lf",
    ".venv-aitk",
}
HARD_PREFIXES = (
    "resources/03-LLM-小模型",
    "resources/07-底模",
    "third_party/laya",
    "third_party/ai-toolkit",
    "third_party/LlamaFactory",
    "third_party/sd-scripts",
    "dist/out/",
    "dist/runtime/",
    "dist/runtime-manifest.json",
)
HARD_SUFFIXES = {".safetensors", ".gguf"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def rel_to_path(rel: str) -> Path:
    return PROJECT_ROOT / rel.replace("/", os.sep)


def as_rel(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_pruned(rel: str):
    for pattern, reason in PRUNE_RULES:
        if pattern.search(rel):
            return reason
    return None


def hard_exclude_hits(rels):
    """Return [(rel, why)] for every rel that must never be packed."""
    hits = []
    for rel in rels:
        norm = rel.replace("\\", "/")
        parts = norm.split("/")
        for segment in parts:
            if segment in HARD_SEGMENTS:
                hits.append((norm, "hard-excluded directory segment: %s" % segment))
                break
        else:
            for prefix in HARD_PREFIXES:
                if norm == prefix.rstrip("/") or norm.startswith(prefix):
                    hits.append((norm, "hard-excluded path prefix: %s" % prefix))
                    break
            else:
                suffix = Path(norm).suffix.lower()
                if suffix in HARD_SUFFIXES:
                    hits.append((norm, "hard-excluded file type: %s" % suffix))
    return hits


def count_tree(path: Path):
    files = 0
    total = 0
    if path.is_file():
        return 1, path.stat().st_size
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
                files += 1
            except OSError:
                pass
    return files, total


def human_mb(num_bytes: int) -> str:
    return "%.2f MB" % (num_bytes / 1048576.0)


# ---------------------------------------------------------------------------
# collection
# ---------------------------------------------------------------------------


def collect(selection):
    """Walk the selection, prune generated material, return (files, pruned, missing)."""
    files = []
    pruned = []
    missing = []
    for entry in selection:
        rel = entry["rel"]
        path = rel_to_path(rel)
        if not path.exists():
            (missing if not entry["required"] else missing).append(rel)
            continue
        if path.is_file():
            if is_pruned(rel):
                n, b = count_tree(path)
                pruned.append({"path": rel, "files": n, "bytes": b, "reason": is_pruned(rel)})
            else:
                files.append(
                    {
                        "rel": entry.get("arc", rel),
                        "abs": path,
                        "bytes": path.stat().st_size,
                        "group": entry["group"],
                    }
                )
            continue
        for dirpath, dirnames, filenames in os.walk(path):
            current = Path(dirpath)
            kept_dirs = []
            for name in sorted(dirnames):
                child_rel = as_rel(current / name)
                reason = is_pruned(child_rel)
                if reason is not None:
                    n, b = count_tree(current / name)
                    pruned.append({"path": child_rel, "files": n, "bytes": b, "reason": reason})
                else:
                    kept_dirs.append(name)
            dirnames[:] = kept_dirs
            for name in sorted(filenames):
                file_path = current / name
                file_rel = as_rel(file_path)
                reason = is_pruned(file_rel)
                if reason is not None:
                    pruned.append(
                        {
                            "path": file_rel,
                            "files": 1,
                            "bytes": file_path.stat().st_size,
                            "reason": reason,
                        }
                    )
                    continue
                files.append(
                    {
                        "rel": file_rel,
                        "abs": file_path,
                        "bytes": file_path.stat().st_size,
                        "group": entry["group"],
                    }
                )
    files.sort(key=lambda item: item["rel"])
    return files, pruned, missing


def summarise(files, pruned, missing):
    groups = {}
    for item in files:
        bucket = groups.setdefault(item["group"], {"files": 0, "bytes": 0})
        bucket["files"] += 1
        bucket["bytes"] += item["bytes"]
    total = sum(item["bytes"] for item in files)
    pruned_files = sum(item["files"] for item in pruned)
    pruned_bytes = sum(item["bytes"] for item in pruned)
    return {
        "file_count": len(files),
        "total_bytes": total,
        "groups": groups,
        "pruned": pruned,
        "pruned_files": pruned_files,
        "pruned_bytes": pruned_bytes,
        "missing": missing,
    }


# ---------------------------------------------------------------------------
# manifest / readme payloads
# ---------------------------------------------------------------------------


MAIN_README = """# ModelSmith — 训练方案包（main pack）

本包是 **训练方案与工具层**，不含任何模型权重。

## 内容
- `core/`：本地核心服务（stdlib HTTP + SQLite 作业表 + GPU 锁，`python core\\modelsmith_core.py`）
- `adapters/`：训练适配器与零 GPU 自测（laya / llm_lora / lf / aitk / diffsynth）
- `design/`：UI 原型单文件 `prototype/index.html`、art 素材、设计规格与 QA 清单
- `docs/`：契约、流程、研究笔记
- `recipes/`：可直接复制的训练配方（Qwen-Image 2.1 LoRA 等）
- `third_party/DiffSynth-Studio/`：Apache-2.0 上游源码（Qwen-Image 2.1 训练入口）
- `third_party/MANIFEST.md` + `third_party/LICENSES/`：第三方来源与许可
- `dist/`：打包器与清单

## 怎么用
1. 解压到任意目录；
2. 运行时用你自己的 Python + torch（本机验证组合：Python 3.12 + torch 2.12.0+cu130）；
3. 训练按 `recipes/qwen_image_21_lora.md`；核心服务按 `core/README.md`。

## 缺什么（本包**故意不含**）
- 模型权重：Qwen-Image 2.1、FLUX、SD、LLM 权重、LoRA 产物——**由用户自备**；
- 运行时：Python embed / torch / 依赖目录（体积过大，另发或不发）；
- `_tmp/` `_runs/` 等生成物。

## 许可
- 本包自有代码：随项目交付；
- `third_party/DiffSynth-Studio`：**Apache-2.0**（`LICENSE` 原样保留）；
- **Qwen-Image 2.1 底模是 Research License，不随包分发**，请自行从官方渠道获取并遵守其条款；
- 其他第三方许可见 `third_party/LICENSES/` 与 `PACK-MANIFEST.json`。
"""

DATA_README = """# ModelSmith — 数据包（data pack）

本包是 **语料 / 图集**，不含任何模型权重。

## 内容
- `resources/01-决策模型-业务语料/`：决策/意图语料（逐条来源与许可见各目录 `MANIFEST.md`）
- `resources/02-LLM-语料/`：dolly-15k（CC-BY-SA-3.0）
- `resources/04-生图LoRA-图集/`：合成 smoke 图集（4 图 + `metadata.csv`）
- `resources/README.md`、`00-底座与权重-指针.md`、`06-清单与说明/`

## 缺什么（本包**故意不含**）
- `resources/03-LLM-小模型/`（953 MB）与 `resources/07-底模/`（2,035 MB）——**模型权重，不分发**；
- `resources/05-视频LoRA-图集/`（空）；
- 训练生成的 `_latent_cache/` `_t_e_cache/`（DiffSynth/ai-toolkit 缓存，非语料）。

## 许可
逐条见各目录的 `MANIFEST.md` 与 `PACK-MANIFEST.json` 的 `group` 字段；不含无许可数据集（SMP2017/2018、CLINC150 等只在本地研究）。
"""


def build_manifest(pack_kind, zip_name, files, summary):
    return {
        "pack": pack_kind,
        "name": zip_name,
        "version": VERSION,
        "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "date_stamp": DATE_STAMP,
        "project_root": ".",
        "file_count": summary["file_count"],
        "total_bytes": summary["total_bytes"],
        "total_mb": round(summary["total_bytes"] / 1048576.0, 3),
        "groups": {
            key: {
                "files": value["files"],
                "bytes": value["bytes"],
                "mb": round(value["bytes"] / 1048576.0, 3),
            }
            for key, value in sorted(summary["groups"].items())
        },
        "files": [
            {
                "rel": item["rel"],
                "bytes": item["bytes"],
                "sha256": item.get("sha256"),
                "group": item["group"],
            }
            for item in files
        ],
        "pruned": summary["pruned"],
        "missing_optional": summary["missing"],
        "hard_exclude_rules": {
            "segments": sorted(HARD_SEGMENTS),
            "prefixes": list(HARD_PREFIXES),
            "suffixes": sorted(HARD_SUFFIXES),
        },
        "note": "训练方案/语料 only; no model weights. Qwen-Image 2.1 base is Research License, supplied by the user.",
    }


def pack_readme(pack_kind, summary) -> str:
    body = MAIN_README if pack_kind == "main" else DATA_README
    lines = [
        body.rstrip(),
        "",
        "## 本包清单（生成时）",
        "",
        "| 组 | 文件 | 体积 |",
        "|---|---|---|",
    ]
    for key, value in sorted(summary["groups"].items()):
        lines.append("| %s | %d | %s |" % (key, value["files"], human_mb(value["bytes"])))
    lines.append("| **合计** | **%d** | **%s** |" % (summary["file_count"], human_mb(summary["total_bytes"])))
    lines += ["", "## 清单文件", "", "- `PACK-MANIFEST.json`：每个文件的 rel / bytes / sha256 / group。", ""]
    lines += [
        "",
        "## 分发脱敏（只在写 zip 时替换；源文件未改）",
        "",
        "- `E:\\视频\\modelsmith` / `E:/视频/modelsmith` → `<包根>`",
        "- 其它 `E:\\…` / `E:/…` → `<外部路径>`",
        "- `C:\\Users\\…` → `<用户目录>`",
        "- `127.0.0.1:12334` → `<本机代理>`；`127.0.0.1:8765` 是 ModelSmith 自身服务端口，保留。",
        "- 出包闸门：命中 `E:\\` / `E:/` / `C:\\Users\\` / `127.0.0.1:12334` 任一处即拒绝出包。",
        "",
    ]
    if summary["pruned"]:
        lines += ["## 已剔除的生成物（不静默）", ""]
        for item in summary["pruned"]:
            lines.append(
                "- `%s` — %d 文件 / %s — %s"
                % (item["path"], item["files"], human_mb(item["bytes"]), item["reason"])
            )
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# zip writing / verification
# ---------------------------------------------------------------------------


def write_zip(zip_path: Path, files, manifest: dict, readme_text: str, leak_probe=None):
    """Write the zip; text entries are sanitized in memory (disk is untouched)."""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = zip_path.with_suffix(zip_path.suffix + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    stats = {"files": 0}
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=COMPRESS_LEVEL) as archive:
        try:
            for item in files:
                rel = item["rel"]
                info = zipfile.ZipInfo(rel, date_time=ZIP_DATE_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                if is_text_entry(rel):
                    data = Path(item["abs"]).read_bytes()
                    text = data.decode("utf-8", "surrogateescape")
                    clean, rule_stats = sanitize_text(text)
                    if clean != text:
                        stats["files"] += 1
                        merge_stats(stats, rule_stats)
                    archive.writestr(info, clean.encode("utf-8", "surrogateescape"))
                else:
                    with archive.open(info, "w") as dest, open(item["abs"], "rb") as source:
                        shutil.copyfileobj(source, dest, 1 << 20)
            for name, text in (
                ("PACK-MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=1) + "\n"),
                ("PACK-README.md", readme_text if readme_text.endswith("\n") else readme_text + "\n"),
            ):
                clean, rule_stats = sanitize_text(text)
                if clean != text:
                    stats["files"] += 1
                    merge_stats(stats, rule_stats)
                info = zipfile.ZipInfo(name, date_time=ZIP_DATE_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, clean.encode("utf-8"))
            if leak_probe is not None:
                info = zipfile.ZipInfo("_selftest_leak.txt", date_time=ZIP_DATE_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, str(leak_probe).encode("utf-8"))
        except BaseException:
            archive.close()
            if tmp_path.exists():
                tmp_path.unlink()
            raise
    os.replace(tmp_path, zip_path)
    return zip_path.stat().st_size, stats



FORBIDDEN_SUFFIXES = {".safetensors", ".gguf", ".pyc", ".pyo"}


def verify_zip(zip_path: Path, pack_kind: str):
    problems = []
    with zipfile.ZipFile(zip_path) as archive:
        bad_member = archive.testzip()
        names = archive.namelist()
    if bad_member is not None:
        problems.append("testzip() reported bad member: %s" % bad_member)
    for required in ("PACK-MANIFEST.json", "PACK-README.md"):
        if required not in names:
            problems.append("missing %s" % required)
    if pack_kind == "main":
        for required in (
            "core/modelsmith_core.py",
            "recipes/qwen_image_21_lora.md",
            "start-core.cmd",
            "dist/RUNTIME-PACK.md",
            "dist/pack_runtime.py",
            "dist/runtime-licenses/tokenizers/LICENSE",
            "dist/runtime-licenses/sentencepiece/LICENSE",
            "dist/runtime-licenses/antlr4/LICENSE",
        ):
            if required not in names:
                problems.append("missing expected member: %s" % required)
    hits = hard_exclude_hits(names)
    for rel, why in hits:
        problems.append("forbidden member %s (%s)" % (rel, why))
    for name in names:
        lowered = name.lower()
        if lowered.endswith(".pyc") or lowered.endswith(".pyo"):
            problems.append("bytecode member: %s" % name)
        if "/__pycache__/" in "/" + name or name.startswith("__pycache__/"):
            problems.append("__pycache__ member: %s" % name)
        if name.startswith("dist/out/"):
            problems.append("pack-output member: %s" % name)
    return names, problems


# ---------------------------------------------------------------------------
# plan / build
# ---------------------------------------------------------------------------


def plan_pack(label, selection, zip_name):
    files, pruned, missing = collect(selection)
    summary = summarise(files, pruned, missing)
    hits = hard_exclude_hits([item["rel"] for item in files])
    print("[%s] %s" % (label, zip_name))
    for key, value in sorted(summary["groups"].items()):
        print("  %-16s files=%-5d %s" % (key, value["files"], human_mb(value["bytes"])))
    print(
        "  TOTAL            files=%-5d %s"
        % (summary["file_count"], human_mb(summary["total_bytes"]))
    )
    for item in summary["pruned"]:
        print(
            "  pruned  %-52s files=%-5d %s  (%s)"
            % (item["path"], item["files"], human_mb(item["bytes"]), item["reason"])
        )
    if summary["missing"]:
        print("  missing optional: %s" % ", ".join(summary["missing"]))
    if hits:
        print("  HARD-EXCLUDE HITS: %d" % len(hits))
        for rel, why in hits:
            print("    ! %s  (%s)" % (rel, why))
    else:
        print("  hard-exclude scan: clean")
    if label == "main":
        present = {item["rel"] for item in files}
        for rel in RUNTIME_ENTRY_CHECKS:
            print("  check %-48s %s" % (rel, "YES" if rel in present else "MISSING"))
    return files, summary, hits


def build_pack(label, selection, zip_name, pack_dir=None, leak_probe=None, force_size=False):
    files, summary, hits = plan_pack(label, selection, zip_name)
    if hits:
        raise SystemExit("ABORT: hard-excluded files selected (%d). See list above." % len(hits))
    for item in files:
        item["sha256"] = sha256_file(item["abs"])
    manifest = build_manifest(label, zip_name, files, summary)
    readme_text = pack_readme(label, summary)
    pack_dir = Path(pack_dir) if pack_dir is not None else OUT_DIR
    zip_path = pack_dir / zip_name
    check_size_guard(label, summary["total_bytes"], force_size)
    zip_bytes, sanitize_stats = write_zip(zip_path, files, manifest, readme_text, leak_probe=leak_probe)
    replaced = sum(v for k, v in sanitize_stats.items() if k != "files")
    print(
        "  sanitize: files=%-4d replacements=%-5d %s"
        % (sanitize_stats.get("files", 0), replaced, json.dumps(
            {k: v for k, v in sorted(sanitize_stats.items()) if k != "files"}, ensure_ascii=False))
    )
    leaks = scan_zip_leaks(zip_path)
    if leaks:
        print("  LEAK GATE FAIL: %d hit(s) — refusing to ship" % len(leaks))
        for hit in leaks[:10]:
            print("    ! %s:%d [%s] %s" % (hit["entry"], hit["line"], hit["pattern"], hit["snippet"]))
        try:
            zip_path.unlink()
        except OSError:
            pass
        raise SystemExit(2)
    print("  leak gate: clean (0 hits)")
    names, problems = verify_zip(zip_path, label)
    uncompressed_bytes = zip_uncompressed_bytes(zip_path)
    if not problems:
        save_pack_baseline(
            label,
            {
                "zip": zip_name,
                "entries": len(names),
                "uncompressed_bytes": uncompressed_bytes,
                "zip_bytes": zip_bytes,
                "sanitize": sanitize_stats,
            },
        )
    print(
        "  -> %s  zip=%s  entries=%d  testzip=%s"
        % (
            zip_path,
            human_mb(zip_bytes),
            len(names),
            "None" if not problems else "FAIL",
        )
    )
    if problems:
        for problem in problems:
            print("  SELF-CHECK FAIL: %s" % problem)
    else:
        print("  SELF-CHECK PASS")
    return {
        "zip_path": zip_path,
        "zip_bytes": zip_bytes,
        "entries": len(names),
        "files": summary["file_count"],
        "source_bytes": summary["total_bytes"],
        "problems": problems,
        "summary": summary,
        "sanitize": sanitize_stats,
        "uncompressed_bytes": uncompressed_bytes,
    }


EXCLUDE_SELFTEST_CASES = [
    "core/x.safetensors",
    "some/model.gguf",
    "_tmp/a.txt",
    "_runs/b.log",
    ".aitk-pylibs/c",
    ".pylibs-lf/d",
    ".venv-aitk/e",
    "resources/03-LLM-小模型/f",
    "resources/07-底模/g",
    "third_party/laya/h",
    "third_party/ai-toolkit/i",
    "third_party/LlamaFactory/j",
    "third_party/sd-scripts/k",
    "dist/out/l.zip",
    "resources/04-生图LoRA-图集/smoke-synthetic/stray.safetensors",
]


def selftest_exclude() -> int:
    passed = 0
    failed = 0
    for rel in EXCLUDE_SELFTEST_CASES:
        hits = hard_exclude_hits([rel])
        if hits:
            passed += 1
        else:
            failed += 1
            print("FAIL  negative control did not fire: %s" % rel)
    allowed = [
        "core/modelsmith_core.py",
        "recipes/qwen_image_21_lora.md",
        "third_party/DiffSynth-Studio/LICENSE",
    ]
    for rel in allowed:
        if hard_exclude_hits([rel]):
            failed += 1
            print("FAIL  allowed path was rejected: %s" % rel)
        else:
            passed += 1
    print("EXCLUDE-SELFTEST: PASS %d / FAIL %d" % (passed, failed))
    return 0 if failed == 0 else 1


def main(argv=None) -> int:
    global DATE_STAMP, MAIN_ZIP_NAME, DATA_ZIP_NAME
    parser = argparse.ArgumentParser(description="ModelSmith distribution packer (no model weights)")
    parser.add_argument("--dry-run", action="store_true", help="plan only; write nothing")
    parser.add_argument("--selftest-exclude", action="store_true", help="negative control for the exclusion scanner")
    parser.add_argument("--out-dir", default=str(OUT_DIR), help="output directory (default dist/out)")
    parser.add_argument("--inject-leak", default=None, metavar="TEXT",
                        help="negative control: inject TEXT as _selftest_leak.txt (build must fail)")
    parser.add_argument("--date-stamp", default=None, metavar="YYYYMMDD",
                        help="override the date stamp used in zip/manifest names")
    parser.add_argument("--force-size", action="store_true",
                        help="allow a >20%% deviation from the recorded .pack-baseline.json bytes")
    parser.add_argument("--list-only", action="store_true",
                        help="with --dry-run: print every file that would be packed")
    args = parser.parse_args(argv)

    if args.date_stamp:
        DATE_STAMP = str(args.date_stamp)
        MAIN_ZIP_NAME = "ModelSmith-方案-%s.zip" % DATE_STAMP
        DATA_ZIP_NAME = "ModelSmith-数据包-%s.zip" % DATE_STAMP

    if args.selftest_exclude:
        return selftest_exclude()

    out_dir = Path(args.out_dir)
    print("ModelSmith packer  date=%s  root=%s" % (DATE_STAMP, PROJECT_ROOT))
    if args.dry_run:
        print("MODE: dry-run (nothing written)\n")

    results = []
    for label, selection, zip_name in (
        ("main", MAIN_SELECTION, MAIN_ZIP_NAME),
        ("data", DATA_SELECTION, DATA_ZIP_NAME),
    ):
        if args.dry_run:
            _files, _summary, hits = plan_pack(label, selection, zip_name)
            print("")
            if hits:
                print("ABORT would trigger: %d hard-excluded files selected." % len(hits))
                return 2
            check_size_guard(label, _summary["total_bytes"], args.force_size)
            if args.list_only:
                print("  LIST %s files=%d" % (label, len(_files)))
                for item in _files:
                    print("    " + item["rel"])
            continue
        print("")
        result = build_pack(label, selection, zip_name, pack_dir=out_dir,
                            leak_probe=args.inject_leak, force_size=args.force_size)
        print("")
        results.append(result)

    if args.dry_run:
        print("dry-run complete; expected outputs when run for real:")
        print("  %s" % (out_dir / MAIN_ZIP_NAME))
        print("  %s" % (out_dir / DATA_ZIP_NAME))
        return 0

    print("=== SUMMARY ===")
    ok = True
    for result in results:
        status = "OK" if not result["problems"] else "FAIL"
        ok = ok and not result["problems"]
        sanitize = result.get("sanitize") or {}
        replaced = sum(v for k, v in sanitize.items() if k != "files")
        print(
            "%-5s %-46s files=%-5d src=%-10s zip=%-10s entries=%d  testzip=%s  sanitize=%d files/%d hits"
            % (
                status,
                result["zip_path"].name,
                result["files"],
                human_mb(result["source_bytes"]),
                human_mb(result["zip_bytes"]),
                result["entries"],
                "None" if not result["problems"] else "FAIL",
                sanitize.get("files", 0),
                replaced,
            )
        )
    print("SELF-CHECK: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
