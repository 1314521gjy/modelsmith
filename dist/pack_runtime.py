#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Plan (default) or copy the minimal Windows runtime for ModelSmith.

The default mode is a dry run: it scans the source trees, computes sizes and
SHA256 for the selected minimal set, and writes runtime-manifest.json.  It does
not copy anything.  Use --apply to copy, and only after the 维护者 has approved
the size (the default guard refuses >2 GiB unless --allow-large is given).

Layout produced by --apply:

    <dest>/
      python/            Python 3.12 embeddable-style interpreter
      python/python312._pth
      site-packages/     selected pure-Python and compiled packages
      licenses/<pkg>/    copied license texts (also kept inside dist-info)

The package root (one level above <dest>) is expected to contain
start-core.cmd and core/modelsmith_core.py.

Only the Python standard library is used.  All paths are built with pathlib /
os.path.join, so the Chinese project path works.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

THIS_FILE = Path(__file__).resolve()
PROJECT_ROOT = THIS_FILE.parent.parent

# Shared with dist/pack.py: never keep absolute local paths in the shipped
# runtime (the runtime zip is assembled from this staging directory).
sys.path.insert(0, str(THIS_FILE.parent))
from pack import is_text_entry, merge_stats, sanitize_text, scan_tree_leaks  # noqa: E402

DEFAULT_PYTHON_ROOT = Path(os.environ.get("MODELSMITH_PYTHON_ROOT", r"E:\ComfyUI\python_embeded"))
DEFAULT_USER_SITE = Path(
    os.environ.get(
        "MODELSMITH_USER_SITE",
        os.path.join(
            os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")),
            "Python",
            "Python312",
            "site-packages",
        ),
    )
)
DEFAULT_LF_LIBS = PROJECT_ROOT / ".pylibs-lf"
DEFAULT_DEST = PROJECT_ROOT / "dist" / "runtime"
DEFAULT_MANIFEST = PROJECT_ROOT / "dist" / "runtime-manifest.json"
DEFAULT_PYLIBS5_ZIP = PROJECT_ROOT / "dist" / "runtime-src" / "pylibs-5.zip"

# The generated ._pth.  python/ and site-packages/ are siblings under runtime/.
# The three third_party paths are relative to runtime/python and also work when
# the runtime is staged under dist/ (three levels up = project/package root).
PTH_TEXT = (
    "python312.zip\n"
    ".\n"
    "..\\site-packages\n"
    "..\\..\\..\\third_party\\DiffSynth-Studio\n"
    "..\\..\\..\\third_party\\LlamaFactory\\src\n"
    "..\\..\\..\\third_party\\laya\n"
    "import site\n"
)

# License texts that are not present in the installed dist-info.  They were
# fetched from the upstream tag and are recorded in
# dist/runtime-licenses/SOURCES.json; the packer checks each SHA256 before use.
EXTRA_LICENSES = {
    "tokenizers": dict(
        version="0.22.1",
        spdx="Apache-2.0",
        url="https://raw.githubusercontent.com/huggingface/tokenizers/v0.22.1/LICENSE",
        sha256="C71D239DF91726FC519C6EB72D318EC65820627232B2F796219E87DCF35D0AB4",
    ),
    "sentencepiece": dict(
        version="0.2.1",
        spdx="Apache-2.0",
        url="https://raw.githubusercontent.com/google/sentencepiece/v0.2.1/LICENSE",
        sha256="CFC7749B96F63BD31C3C42B5C471BF756814053E847C10F3EB003417BC523D30",
    ),
    "antlr4": dict(
        version="4.9.3",
        spdx="BSD-3-Clause",
        url="https://raw.githubusercontent.com/antlr/antlr4/4.9.3/LICENSE.txt",
        sha256="B1B379FCAF3219593A4C433FEB1B35C780BED23FAFAAE440B1AE2771A9521E3A",
    ),
}

# Top-level files of the source interpreter that are never part of a runtime.
PYTHON_BASE_SKIP = {
    "get-pip.py",
    "python.cat",
    "1.0.4",
    "python.pdb",
    "python3.pdb",
    "python312.pdb",
    "pythonw.pdb",
    "python312._pth",
}
PYTHON_BASE_SUFFIXES = (".exe", ".dll", ".pyd", ".zip", ".txt")

# Generic exclusions applied to every package.
GENERIC_SKIP_DIRS = {"__pycache__"}
GENERIC_SKIP_SUFFIXES = (".pyc", ".pdb")

# One entry per package copied to site-packages.  `rel` is relative to the
# chosen source root.  `dist` is the distribution name in METADATA (None when
# the path belongs to a larger distribution or has no metadata).  `spdx` is the
# license recorded in runtime-manifest.json and in RUNTIME-PACK.md.
PACKAGE_SPECS = [
    dict(key="torch", rel="torch", dist="torch", version="2.12.0+cu130",
         source="comfy", spdx="BSD-3-Clause",
         trim=["torch/include", "torch/lib/*.lib"]),
    dict(key="torchgen", rel="torchgen", dist=None, version=None,
         source="comfy", spdx="BSD-3-Clause", trim=[]),
    dict(key="functorch", rel="functorch", dist=None, version=None,
         source="comfy", spdx="BSD-3-Clause", trim=[]),
    dict(key="transformers", rel="transformers", dist="transformers",
         version="4.57.6", source="comfy", spdx="Apache-2.0", trim=[]),
    dict(key="tokenizers", rel="tokenizers", dist="tokenizers",
         version="0.22.1", source="comfy", spdx="Apache-2.0", trim=[]),
    dict(key="huggingface_hub", rel="huggingface_hub", dist="huggingface-hub",
         version="0.36.2", source="comfy", spdx="Apache-2.0", trim=[]),
    dict(key="safetensors", rel="safetensors", dist="safetensors",
         version="0.8.0", source="comfy", spdx="Apache-2.0", trim=[]),
    dict(key="peft", rel="peft", dist="peft", version="0.18.0",
         source="comfy", spdx="Apache-2.0", trim=[]),
    dict(key="accelerate", rel="accelerate", dist="accelerate",
         version="1.12.0", source="comfy", spdx="Apache-2.0", trim=[]),
    dict(key="diffusers", rel="diffusers", dist="diffusers",
         version="0.39.0", source="comfy", spdx="Apache-2.0", trim=[]),
    dict(key="torchvision", rel="torchvision", dist="torchvision",
         version="0.27.0+cu130", source="comfy", spdx="BSD-3-Clause",
         trim=["torchvision/tests", "torchvision/python312.dll"]),
    dict(key="numpy", rel="numpy", dist="numpy", version="2.5.4",
         source="user", spdx="BSD-3-Clause", trim=[]),
    dict(key="numpy.libs", rel="numpy.libs", dist=None, version=None,
         source="user", spdx="BSD-3-Clause", trim=[]),
    dict(key="PIL", rel="PIL", dist="pillow", version="11.3.0",
         source="comfy", spdx="MIT-CMU", trim=[]),
    dict(key="yaml", rel="yaml", dist="pyyaml", version="6.0.3",
         source="comfy", spdx="MIT", trim=[]),
    dict(key="tqdm", rel="tqdm", dist="tqdm", version="4.67.1",
         source="comfy", spdx="MPL-2.0 AND MIT", trim=[]),
    dict(key="regex", rel="regex", dist="regex", version="2025.11.3",
         source="comfy", spdx="Apache-2.0 AND CNRI-Python", trim=[]),
    dict(key="requests", rel="requests", dist="requests", version="2.32.5",
         source="comfy", spdx="Apache-2.0", trim=[]),
    dict(key="certifi", rel="certifi", dist="certifi", version="2025.11.12",
         source="comfy", spdx="MPL-2.0", trim=[]),
    dict(key="charset_normalizer", rel="charset_normalizer",
         dist="charset-normalizer", version="3.4.4",
         source="comfy", spdx="MIT", trim=[]),
    dict(key="idna", rel="idna", dist="idna", version="3.11",
         source="comfy", spdx="BSD-3-Clause", trim=[]),
    dict(key="urllib3", rel="urllib3", dist="urllib3", version="2.5.0",
         source="comfy", spdx="MIT", trim=[]),
    dict(key="packaging", rel="packaging", dist="packaging", version="25.0",
         source="comfy", spdx="Apache-2.0 OR BSD-2-Clause", trim=[]),
    dict(key="filelock", rel="filelock", dist="filelock", version="3.32.7",
         source="comfy", spdx="MIT", trim=[]),
    dict(key="fsspec", rel="fsspec", dist="fsspec", version="2025.3.0",
         source="lf", spdx="BSD-3-Clause", trim=[]),
    dict(key="psutil", rel="psutil", dist="psutil", version="7.1.3",
         source="comfy", spdx="BSD-3-Clause", trim=[]),
    dict(key="sentencepiece", rel="sentencepiece", dist="sentencepiece",
         version="0.2.1", source="comfy", spdx="Apache-2.0", trim=[]),
    dict(key="protobuf", rel="google/protobuf", dist="protobuf",
         version="4.25.8", source="comfy", spdx="BSD-3-Clause", trim=[]),
    dict(key="google_upb", rel="google/_upb", dist=None, version=None,
         source="comfy", spdx="BSD-3-Clause", trim=[]),
    dict(key="einops", rel="einops", dist="einops", version="0.8.1",
         source="comfy", spdx="MIT", trim=[]),
    dict(key="omegaconf", rel="omegaconf", dist="omegaconf",
         version="2.3.0", source="comfy", spdx="BSD-3-Clause", trim=[]),
    dict(key="antlr4", rel="antlr4", dist="antlr4-python3-runtime",
         version="4.9.3", source="comfy", spdx="BSD-3-Clause", trim=[]),
    dict(key="imageio", rel="imageio", dist="imageio", version="2.37.2",
         source="comfy", spdx="BSD-2-Clause", trim=[]),
    dict(key="matplotlib", rel="matplotlib", dist="matplotlib",
         version="3.10.7", source="comfy", spdx="PSF-2.0", trim=[]),
    dict(key="contourpy", rel="contourpy", dist="contourpy", version="1.3.3",
         source="comfy", spdx="BSD-3-Clause", trim=[]),
    dict(key="cycler", rel="cycler", dist="cycler", version="0.12.1",
         source="comfy", spdx="BSD-3-Clause", trim=[]),
    dict(key="fonttools", rel="fonttools", dist="fonttools",
         version="4.60.1", source="comfy", spdx="MIT", trim=[]),
    dict(key="kiwisolver", rel="kiwisolver", dist="kiwisolver",
         version="1.4.9", source="comfy", spdx="BSD-3-Clause", trim=[]),
    dict(key="pyparsing", rel="pyparsing", dist="pyparsing", version="3.2.5",
         source="comfy", spdx="MIT", trim=[]),
    dict(key="dateutil", rel="dateutil", dist="python-dateutil",
         version="2.9.0.post0", source="comfy",
         spdx="Apache-2.0 OR BSD-3-Clause", trim=[]),
    dict(key="pytz", rel="pytz", dist="pytz", version="2025.2",
         source="comfy", spdx="MIT", trim=[]),
    dict(key="tzdata", rel="tzdata", dist="tzdata", version="2025.2",
         source="comfy", spdx="Apache-2.0", trim=[]),
    dict(key="sympy", rel="sympy", dist="sympy", version="1.14.0",
         source="comfy", spdx="BSD-3-Clause", trim=["sympy/**/tests"]),
    dict(key="networkx", rel="networkx", dist="networkx", version="3.5",
         source="comfy", spdx="BSD-3-Clause", trim=[]),
    dict(key="jinja2", rel="jinja2", dist="jinja2", version="3.1.6",
         source="comfy", spdx="BSD-3-Clause", trim=[]),
    dict(key="markupsafe", rel="markupsafe", dist="markupsafe",
         version="2.1.5", source="comfy", spdx="BSD-3-Clause", trim=[]),
    dict(key="mpmath", rel="mpmath", dist="mpmath", version="1.3.0",
         source="comfy", spdx="BSD-3-Clause", trim=[]),
    dict(key="typing_extensions", rel="typing_extensions.py",
         dist="typing_extensions", version="4.15.0",
         source="comfy", spdx="PSF-2.0", trim=[]),
    dict(key="setuptools", rel="setuptools", dist="setuptools",
         version="80.10.1", source="comfy", spdx="MIT", trim=[]),
    dict(key="_distutils_hack", rel="_distutils_hack", dist=None, version=None,
         source="comfy", spdx="MIT", trim=[]),
    dict(key="six", rel="six.py", dist="six", version="1.17.0",
         source="comfy", spdx="MIT", trim=[]),
    dict(key="pandas", rel="pandas", dist="pandas", version="2.3.3",
         source="comfy", spdx="BSD-3-Clause", trim=["pandas/tests"]),
    dict(key="pyarrow", rel="pyarrow", dist="pyarrow", version="26.0.0",
         source="user", spdx="Apache-2.0",
         trim=["pyarrow/include", "pyarrow/includes", "pyarrow/src",
               "pyarrow/tests", "pyarrow/*.lib"]),
    dict(key="pyarrow.libs", rel="pyarrow.libs", dist=None, version=None,
         source="user", spdx="Apache-2.0", trim=[]),
    dict(key="modelscope", rel="modelscope", dist="modelscope",
         version="1.34.0", source="comfy", spdx="Apache-2.0", trim=[]),
    dict(key="xxhash", rel="xxhash", dist="xxhash", version="3.6.0",
         source="comfy", spdx="BSD-2-Clause", trim=[]),
    dict(key="datasets", rel="datasets", dist="datasets", version="4.0.0",
         source="lf", spdx="Apache-2.0", trim=[]),
    dict(key="dill", rel="dill", dist="dill", version="0.3.8",
         source="lf", spdx="BSD-3-Clause", trim=[]),
    dict(key="multiprocess", rel="multiprocess", dist="multiprocess",
         version="0.70.16", source="lf", spdx="BSD-3-Clause", trim=[]),
    dict(key="_multiprocess", rel="_multiprocess", dist=None, version=None,
         source="lf", spdx="BSD-3-Clause", trim=[]),
    dict(key="trl", rel="trl", dist="trl", version="0.24.0",
         source="lf", spdx="Apache-2.0", trim=[]),
]

LICENSE_RE = re.compile(r"^(license|licence|copying|notice|authors)([._-].*)?$", re.I)


def norm_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value or "").strip().lower()


def human_bytes(n: int) -> str:
    if n >= 1024 ** 3:
        return "%.2f GiB" % (n / 1024 ** 3)
    if n >= 1024 ** 2:
        return "%.2f MiB" % (n / 1024 ** 2)
    return "%d B" % n


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dir_stats(path: Path):
    total = 0
    files = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
                files += 1
            except OSError:
                pass
    return total, files


def read_metadata(dist_info: Path):
    meta = dist_info / "METADATA"
    name = ""
    version = ""
    lic = ""
    lic_exp = ""
    if not meta.is_file():
        return name, version, lic, lic_exp
    try:
        text = meta.read_text(encoding="utf-8", errors="replace")[:200000]
    except OSError:
        return name, version, lic, lic_exp
    for line in text.splitlines():
        low = line.lower()
        if line.startswith("Name: "):
            name = line[6:].strip()
        elif line.startswith("Version: "):
            version = line[9:].strip()
        elif low.startswith("license-expression:"):
            lic_exp = line.split(":", 1)[1].strip()
        elif low.startswith("license:") and not low.startswith("license-file"):
            lic = line.split(":", 1)[1].strip()
    return name, version, lic, lic_exp


def find_dist_info(root: Path, dist_name: str, version: str, allow_drift: bool):
    matches = []
    for di in sorted(root.glob("*.dist-info")):
        name, ver, _, _ = read_metadata(di)
        if norm_name(name) == norm_name(dist_name):
            matches.append((ver, di))
    if not matches:
        raise SystemExit("MISSING DIST-INFO: %s under %s" % (dist_name, root))
    exact = [m for m in matches if m[0] == version]
    if exact:
        return exact[0][1], exact[0][0]
    if allow_drift:
        ver, di = matches[0]
        print("WARN version drift: %s expected %s, using %s at %s"
              % (dist_name, version, ver, di), file=sys.stderr)
        return di, ver
    raise SystemExit("VERSION MISMATCH: %s expected %s, found %s under %s"
                     % (dist_name, version,
                        [m[0] for m in matches], root))


def is_trimmed(rel_site: str, trims):
    for pat in trims:
        pat = pat.replace("\\", "/")
        if pat.endswith("/**"):
            prefix = pat[:-3]
            if rel_site == prefix or rel_site.startswith(prefix + "/"):
                return True
        if fnmatch.fnmatch(rel_site, pat):
            return True
        if rel_site.startswith(pat + "/"):
            return True
    return False


def skip_file(rel_site: str, trims, extra_excludes):
    parts = rel_site.split("/")
    if any(p in GENERIC_SKIP_DIRS for p in parts):
        return True
    if rel_site.endswith(GENERIC_SKIP_SUFFIXES):
        return True
    if is_trimmed(rel_site, trims):
        return True
    for pat in extra_excludes:
        if fnmatch.fnmatch(rel_site, pat.replace("\\", "/")):
            return True
    return False


def iter_package_files(spec, roots, extra_excludes):
    root = roots[spec["source"]]
    source = root / spec["rel"]
    if not source.exists():
        raise SystemExit("MISSING PACKAGE: %s (%s)" % (spec["key"], source))
    if source.is_file():
        yield source, spec["rel"].replace("\\", "/")
        return
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames[:] = sorted(d for d in dirnames if d not in GENERIC_SKIP_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            rel_site = path.relative_to(root).as_posix()
            if skip_file(rel_site, spec.get("trim") or [], extra_excludes):
                continue
            yield path, rel_site


def license_files_in(path: Path):
    out = []
    if path.is_file():
        if LICENSE_RE.match(path.name):
            out.append(path)
        return out
    for dirpath, dirnames, filenames in os.walk(path):
        for name in filenames:
            p = Path(dirpath) / name
            if LICENSE_RE.match(name) or "licenses" in p.parts:
                out.append(p)
    return out


def pylibs5_info(zip_path: Path):
    """Validate the transformers-5.x isolation zip and describe it."""
    if not zip_path.is_file():
        raise SystemExit("MISSING PYLIBS5 ZIP: %s" % zip_path)
    blob = zip_path.read_bytes()
    info = dict(
        path=str(zip_path),
        bytes=len(blob),
        sha256=hashlib.sha256(blob).hexdigest().upper(),
        target="pylibs-5",
    )
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad is not None:
            raise SystemExit("PYLIBS5 ZIP CORRUPT: %s" % bad)
        members = [m for m in zf.infolist() if not m.is_dir()]
        names = [m.filename.replace("\\", "/") for m in members]
        if "SOURCES.json" not in names:
            raise SystemExit("PYLIBS5 ZIP MISSING SOURCES.json")
        for name in names:
            parts = name.split("/")
            if any(p == "__pycache__" for p in parts) or name.endswith((".pyc", ".pyo")):
                raise SystemExit("PYLIBS5 ZIP contains bytecode: %s" % name)
            if parts[0] in ("torch", "torchvision", "functorch"):
                raise SystemExit("PYLIBS5 ZIP contains forbidden package: %s" % name)
        info["entries"] = len(members)
        info["files"] = len(members)
        info["uncompressed_bytes"] = sum(m.file_size for m in members)
        try:
            sources = json.loads(zf.read("SOURCES.json").decode("utf-8"))
        except Exception as exc:
            raise SystemExit("PYLIBS5 SOURCES.json invalid: %s" % exc)
        info["packages"] = sources.get("packages", [])
    return info


def apply_pylibs5(info, dest: Path):
    """Replace <dest>/pylibs-5 with the exact zip contents."""
    target = dest / "pylibs-5"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    files = 0
    total = 0
    with zipfile.ZipFile(info["path"]) as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            rel = Path(member.filename)
            if rel.is_absolute() or ".." in rel.parts:
                raise SystemExit("PYLIBS5 UNSAFE MEMBER: %s" % member.filename)
            out = target / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, out.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            files += 1
            total += out.stat().st_size
    if files != info["files"] or total != info["uncompressed_bytes"]:
        raise SystemExit("PYLIBS5 EXTRACT MISMATCH: files=%d/%d bytes=%d/%d"
                         % (files, info["files"], total, info["uncompressed_bytes"]))
    print("PYLIBS5_EXTRACTED files=%d bytes=%d -> %s" % (files, total, target))
    return files, total


def build_plan(args):
    roots = {
        "comfy": Path(args.comfy_site) if args.comfy_site
        else Path(args.python_root) / "Lib" / "site-packages",
        "user": Path(args.user_site) if args.user_site else DEFAULT_USER_SITE,
        "lf": Path(args.lf_libs) if args.lf_libs else DEFAULT_LF_LIBS,
    }
    python_root = Path(args.python_root)
    pylibs5 = pylibs5_info(Path(args.pylibs5_zip))

    only = set(x.strip() for x in (args.only or "").split(",") if x.strip())
    excluded = set(args.exclude_package or [])
    if only:
        unknown = only - {s["key"] for s in PACKAGE_SPECS}
        if unknown:
            raise SystemExit("UNKNOWN --only keys: %s" % sorted(unknown))
    selected_specs = []
    for spec in PACKAGE_SPECS:
        if only and spec["key"] not in only:
            continue
        if spec["key"] in excluded:
            continue
        selected_specs.append(spec)

    entries = {}
    packages = []
    extra_licenses = []
    seen_dist = set()
    license_missing = []

    def add_entry(rel, source_path=None, content=None, license_id="", package="",
                  generated=False):
        rel = rel.replace("\\", "/")
        if rel in entries:
            return
        if generated:
            data = content.encode("utf-8")
            entries[rel] = dict(rel=rel, bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                                license=license_id, package=package, generated=True,
                                content=content)
            return
        st = source_path.stat()
        entries[rel] = dict(rel=rel, bytes=st.st_size, sha256=sha256_file(source_path),
                            license=license_id, package=package, source=str(source_path))

    # 1) interpreter base
    py_total = 0
    py_files = 0
    if not python_root.is_dir():
        raise SystemExit("MISSING PYTHON ROOT: %s" % python_root)
    for item in sorted(python_root.iterdir()):
        if not item.is_file():
            continue
        name = item.name
        if name in PYTHON_BASE_SKIP or name.endswith(".pdb"):
            continue
        if not name.endswith(PYTHON_BASE_SUFFIXES):
            continue
        add_entry("python/" + name, item, license_id="PSF-2.0", package="python")
        py_total += item.stat().st_size
        py_files += 1
    add_entry("python/python312._pth", content=PTH_TEXT.replace("\n", "\r\n"),
              license_id="PSF-2.0", package="python", generated=True)
    py_total += len(PTH_TEXT.replace("\n", "\r\n"))
    py_files += 1

    # 2) selected packages + their dist-info
    for spec in selected_specs:
        pkg_bytes = 0
        pkg_files = 0
        for source, rel_site in iter_package_files(spec, roots, args.exclude_path or []):
            add_entry("site-packages/" + rel_site, source,
                      license_id=spec["spdx"], package=spec["key"])
            pkg_bytes += source.stat().st_size
            pkg_files += 1
        dist_version = spec.get("version") or ""
        di = None
        if spec.get("dist") and spec["dist"] not in seen_dist:
            seen_dist.add(spec["dist"])
            di, actual_version = find_dist_info(
                roots[spec["source"]], spec["dist"], spec.get("version") or "",
                args.allow_version_drift)
            dist_version = actual_version
            for path in sorted(di.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(roots[spec["source"]]).as_posix()
                add_entry("site-packages/" + rel, path,
                          license_id=spec["spdx"], package=spec["key"])
                pkg_bytes += path.stat().st_size
                pkg_files += 1

        # Consolidated, easy-to-read license copies.
        license_sources = []
        license_sources += license_files_in(roots[spec["source"]] / spec["rel"])
        if di is not None:
            license_sources += license_files_in(di)
        extra_meta = EXTRA_LICENSES.get(spec["key"])
        if extra_meta:
            extra_path = PROJECT_ROOT / "dist" / "runtime-licenses" / spec["key"] / "LICENSE"
            if not extra_path.is_file():
                raise SystemExit("MISSING EXTRA LICENSE: %s" % extra_path)
            actual = sha256_file(extra_path)
            if actual.upper() != extra_meta["sha256"].upper():
                raise SystemExit("EXTRA LICENSE SHA MISMATCH: %s expected %s got %s"
                                 % (extra_path, extra_meta["sha256"], actual))
            license_sources.append(extra_path)
            extra_licenses.append(dict(
                package=spec["key"], version=extra_meta["version"],
                spdx=extra_meta["spdx"], source_url=extra_meta["url"],
                sha256=actual.upper(), bytes=extra_path.stat().st_size,
                runtime_rel="licenses/%s/LICENSE" % spec["key"],
            ))
        if spec.get("dist") and not license_sources:
            license_missing.append(spec["key"])
            print("WARN no license file in source for %s (%s); upstream license must be added"
                  % (spec["key"], spec["spdx"]), file=sys.stderr)
        seen_names = set()
        for lp in license_sources:
            name = lp.name
            if name in seen_names:
                name = lp.parent.name + "-" + name
            seen_names.add(name)
            add_entry("licenses/%s/%s" % (spec["key"], name), lp,
                      license_id=spec["spdx"], package=spec["key"])
            pkg_bytes += lp.stat().st_size
            pkg_files += 1

        packages.append(dict(
            key=spec["key"],
            rel=spec["rel"],
            version=dist_version,
            source=spec["source"],
            source_path=str(roots[spec["source"]] / spec["rel"]),
            bytes=pkg_bytes,
            files=pkg_files,
            spdx=spec["spdx"],
            license_files=len(license_sources),
        ))

    # 3) interpreter license copy
    py_license = python_root / "LICENSE.txt"
    if py_license.is_file():
        add_entry("licenses/python/LICENSE.txt", py_license,
                  license_id="PSF-2.0", package="python")

    # 4) source scans (sizes only; no hashing of rejected packages)
    source_total, source_files = dir_stats(python_root)
    site_root = Path(args.python_root) / "Lib" / "site-packages"
    site_total, site_files = dir_stats(site_root) if site_root.is_dir() else (0, 0)

    entry_list = [entries[k] for k in sorted(entries)]
    total_bytes = sum(e["bytes"] for e in entry_list)
    license_bytes = sum(e["bytes"] for e in entry_list if e["rel"].startswith("licenses/"))
    ordered = sorted(selected_specs, key=lambda s: s["key"])
    plan = dict(
        tool="dist/pack_runtime.py",
        mode="dry-run",
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        project_root=str(PROJECT_ROOT),
        python_root=str(python_root),
        source_roots={k: str(v) for k, v in roots.items()},
        manifest_path=str(Path(args.manifest)),
        dest=str(Path(args.dest)),
        source_scan=dict(
            python_root_bytes=source_total,
            python_root_files=source_files,
            site_packages_bytes=site_total,
            site_packages_files=site_files,
        ),
        packages=packages,
        entry_count=len(entry_list),
        total_bytes=total_bytes,
        total_mib=round(total_bytes / 1048576, 2),
        total_gib=round(total_bytes / 1073741824, 3),
        license_bytes=license_bytes,
        license_missing=license_missing,
        extra_licenses=extra_licenses,
        pylibs5=pylibs5,
        python_base_bytes=py_total,
        python_base_files=py_files,
        package_order=[s["key"] for s in ordered],
        entries=entry_list,
    )
    return plan


def print_plan(plan):
    scan = plan["source_scan"]
    print("MODELSMITH RUNTIME PACK")
    print("mode: %s" % plan["mode"])
    print("python_root: %s" % plan["python_root"])
    print("source_full: %d B (%s), %d files"
          % (scan["python_root_bytes"], human_bytes(scan["python_root_bytes"]),
             scan["python_root_files"]))
    print("site_packages: %d B (%s), %d files"
          % (scan["site_packages_bytes"], human_bytes(scan["site_packages_bytes"]),
             scan["site_packages_files"]))
    print("selected: %d B (%s), %d files"
          % (plan["total_bytes"], human_bytes(plan["total_bytes"]), plan["entry_count"]))
    print("  interpreter base: %d B" % plan["python_base_bytes"])
    p5 = plan["pylibs5"]
    print("  pylibs-5 zip: %d B / %d entries / unpacked %d B"
          % (p5["bytes"], p5["entries"], p5["uncompressed_bytes"]))
    for pkg in plan["packages"]:
        print("  %-20s %12d B  %6d files  %s"
              % (pkg["key"], pkg["bytes"], pkg["files"], pkg["version"] or "-"))
    print("manifest: %s" % plan["manifest_path"])
    print("dest (apply only): %s" % plan["dest"])


def write_manifest(plan, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Do not keep generated file bodies in the JSON; sha256 and bytes are enough.
    serial = dict(plan)
    serial["entries"] = [
        {k: v for k, v in e.items() if k != "content"} for e in plan["entries"]
    ]
    text = json.dumps(serial, ensure_ascii=False, indent=1) + "\n"
    clean, _ = sanitize_text(text)
    path.write_text(clean, encoding="utf-8", newline="\n")


def sanitize_tree(root: Path) -> dict:
    """Sanitize every text file under root in place (dest only, never sources)."""
    stats = {"files": 0}
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file() or not is_text_entry(path.name):
            continue
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8", "surrogateescape")
        except OSError:
            continue
        clean, rule_stats = sanitize_text(text)
        if clean != text:
            path.write_bytes(clean.encode("utf-8", "surrogateescape"))
            stats["files"] += 1
            merge_stats(stats, rule_stats)
    return stats


def apply_plan(plan, dest: Path, manifest: Path, force: bool, allow_large: bool):
    total = plan["total_bytes"]
    if total > 2 * 1024 ** 3 and not allow_large:
        raise SystemExit(
            "REFUSED --apply: planned size %s exceeds 2 GiB guard; "
            "pass --allow-large after 维护者 approval" % human_bytes(total))
    if dest.exists() and any(dest.iterdir()) and not force:
        raise SystemExit("REFUSED --apply: destination is not empty: %s "
                         "(use --force to overwrite planned files)" % dest)
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    sanitize_stats = {"files": 0}
    for entry in plan["entries"]:
        dst = dest / entry["rel"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        rel = entry["rel"]
        if entry.get("generated"):
            data = entry["content"].encode("utf-8")
        else:
            data = Path(entry["source"]).read_bytes()
        if is_text_entry(rel):
            text = data.decode("utf-8", "surrogateescape")
            clean, rule_stats = sanitize_text(text)
            if clean != text:
                sanitize_stats["files"] += 1
                merge_stats(sanitize_stats, rule_stats)
            data = clean.encode("utf-8", "surrogateescape")
        with dst.open("wb") as fh:
            fh.write(data)
        if not is_text_entry(rel) and dst.stat().st_size != entry["bytes"]:
            raise SystemExit("COPY SIZE MISMATCH: %s" % dst)
        copied += 1
    apply_pylibs5(plan["pylibs5"], dest)
    # pylibs-5 is extracted verbatim; sanitize the staging tree before the gate.
    extract_stats = sanitize_tree(dest)
    for key, value in extract_stats.items():
        sanitize_stats[key] = sanitize_stats.get(key, 0) + value
    replaced = sum(v for k, v in sanitize_stats.items() if k != "files")
    print("SANITIZE files=%d replacements=%d" % (sanitize_stats.get("files", 0), replaced))
    plan["mode"] = "apply"
    plan["dest"] = str(dest)
    plan["manifest_path"] = str(manifest)
    write_manifest(plan, manifest)
    leaks = scan_tree_leaks(dest)
    if leaks:
        print("LEAK GATE FAIL: %d hit(s) in runtime staging dir — refusing to ship" % len(leaks))
        for hit in leaks[:10]:
            print("  ! %s:%d [%s] %s" % (hit["entry"], hit["line"], hit["pattern"], hit["snippet"]))
        raise SystemExit(2)
    print("LEAK GATE PASS (0 hits)")
    print("APPLIED %d files, %s -> %s"
          % (copied, human_bytes(total), dest))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Plan (default) or copy the minimal ModelSmith Windows runtime.")
    parser.add_argument("--dry-run", action="store_true",
                        help="scan, hash and write the manifest; copy nothing (default)")
    parser.add_argument("--apply", action="store_true",
                        help="copy the selected set to --dest")
    parser.add_argument("--allow-large", action="store_true",
                        help="allow --apply when the plan is >2 GiB")
    parser.add_argument("--force", action="store_true",
                        help="allow --apply into a non-empty destination")
    parser.add_argument("--python-root", default=str(DEFAULT_PYTHON_ROOT),
                        help="source python_embeded directory")
    parser.add_argument("--comfy-site", default=None,
                        help="override <python-root>/Lib/site-packages")
    parser.add_argument("--user-site", default=None,
                        help="override %%APPDATA%%/Python/Python312/site-packages")
    parser.add_argument("--lf-libs", default=None,
                        help="override the .pylibs-lf directory")
    parser.add_argument("--pylibs5-zip", default=str(DEFAULT_PYLIBS5_ZIP),
                        help="transformers-5.x isolation zip (default dist/runtime-src/pylibs-5.zip)")
    parser.add_argument("--pylibs5-only", action="store_true",
                        help="only (re)extract pylibs-5; no base runtime copy")
    parser.add_argument("--dest", default=str(DEFAULT_DEST),
                        help="destination runtime directory for --apply")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST),
                        help="runtime-manifest.json path")
    parser.add_argument("--only", default="",
                        help="comma-separated package keys to include")
    parser.add_argument("--exclude-package", action="append", default=[],
                        help="package key to omit (repeatable)")
    parser.add_argument("--exclude-path", action="append", default=[],
                        help="extra glob (relative to site-packages) to omit (repeatable)")
    parser.add_argument("--allow-version-drift", action="store_true",
                        help="use the found version when the table version is absent")
    parser.add_argument("--list-packages", action="store_true",
                        help="list package specs and measured directory sizes, then exit")
    args = parser.parse_args(argv)

    if args.dry_run and args.apply:
        parser.error("choose --dry-run or --apply, not both")
    if (args.only or "").strip() == "pylibs-5":
        args.pylibs5_only = True
        args.only = ""
    if args.pylibs5_only:
        info = pylibs5_info(Path(args.pylibs5_zip))
        print("PYLIBS5 zip=%s" % info["path"])
        print("  bytes=%d entries=%d unpacked=%d sha256=%s"
              % (info["bytes"], info["entries"], info["uncompressed_bytes"], info["sha256"]))
        print("  packages=%s" % ", ".join(p.get("package", "?") for p in info["packages"]))
        if args.apply:
            dest = Path(args.dest)
            dest.mkdir(parents=True, exist_ok=True)
            apply_pylibs5(info, dest)
        return 0
    if args.list_packages:
        roots = {
            "comfy": Path(args.comfy_site) if args.comfy_site
            else Path(args.python_root) / "Lib" / "site-packages",
            "user": Path(args.user_site) if args.user_site else DEFAULT_USER_SITE,
            "lf": Path(args.lf_libs) if args.lf_libs else DEFAULT_LF_LIBS,
        }
        for spec in PACKAGE_SPECS:
            src = roots[spec["source"]] / spec["rel"]
            total, count = dir_stats(src) if src.is_dir() else ((src.stat().st_size, 1)
                                                                if src.is_file() else (0, 0))
            print("%-20s %-6s %14d B %6d files  %s"
                  % (spec["key"], spec["source"], total, count, spec.get("version") or "-"))
        return 0

    plan = build_plan(args)
    plan["mode"] = "apply" if args.apply else "dry-run"
    print_plan(plan)
    if args.apply:
        apply_plan(plan, Path(args.dest), Path(args.manifest),
                   args.force, args.allow_large)
    else:
        write_manifest(plan, Path(args.manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
