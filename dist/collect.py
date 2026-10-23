import os, json, hashlib
from pathlib import Path

BASE = str(
    Path(
        os.environ.get("MODELSMITH_HOME") or Path(__file__).resolve().parent.parent
    ).resolve()
)
OUT = os.path.join(BASE, "dist", "manifest.json")

GROUPS = [
    ("ui", [os.path.join(BASE, "design", "prototype", "index.html")]),
    ("core", [os.path.join(BASE, "core")]),
    ("adapters", [os.path.join(BASE, "adapters")]),
    ("docs", [os.path.join(BASE, "docs")]),
    ("review", [os.path.join(BASE, "review")]),
    ("recipes", [os.path.join(BASE, "recipes")]),
    ("data", [os.path.join(BASE, "resources")]),
]
SKIP_PARTS = {"_tmp", "_recovery", "__pycache__", ".cache", ".git"}
SKIP_NAMES = {"watchdog.log", "watchdog.stop"}
DATA_DIRS = {"01-决策模型-业务语料", "02-LLM-语料", "03-LLM-小模型",
             "04-生图LoRA-图集", "05-视频LoRA-图集", "07-底模"}


def skip(path):
    parts = set(path.split(os.sep))
    if parts & SKIP_PARTS:
        return True
    return os.path.basename(path) in SKIP_NAMES


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


entries = []
for group, roots in GROUPS:
    for root in roots:
        if os.path.isfile(root):
            files = [root]
        elif os.path.isdir(root):
            files = []
            for dp, dn, fn in os.walk(root):
                for f in fn:
                    files.append(os.path.join(dp, f))
        else:
            continue
        for p in files:
            rel = os.path.relpath(p, BASE)
            if skip(rel):
                continue
            pkg = "main"
            if group == "data":
                rel2 = os.path.relpath(p, os.path.join(BASE, "resources"))
                top = rel2.split(os.sep)[0]
                pkg = "data:" + (top if top in DATA_DIRS else "common")
            entries.append({
                "group": group,
                "package": pkg,
                "path": rel,
                "bytes": os.path.getsize(p),
                "sha256": sha256(p),
            })

entries.sort(key=lambda e: (e["package"], e["path"]))
total = sum(e["bytes"] for e in entries)
manifest = {
    "generated_at": "2026-10-22",
    "base": BASE,
    "file_count": len(entries),
    "total_bytes": total,
    "total_mb": round(total / 1048576, 2),
    "entries": entries,
}
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(manifest, f, ensure_ascii=False, indent=1)

by_pkg = {}
for e in entries:
    by_pkg.setdefault(e["package"], [0, 0])
    by_pkg[e["package"]][0] += 1
    by_pkg[e["package"]][1] += e["bytes"]
print("MANIFEST", OUT)
print("FILES", len(entries), "MB", round(total / 1048576, 2))
for k in sorted(by_pkg):
    n, b = by_pkg[k]
    print(f"  {k}: files={n} MB={round(b/1048576,2)}")
