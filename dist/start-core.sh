#!/usr/bin/env bash
# ModelSmith packaged-core POSIX launcher (mirrors dist/start-core.cmd).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
DATA="${MODELSMITH_STATE:-$ROOT/state}"
RUNTIME="${MODELSMITH_RUNTIME:-}"

if [ -z "$RUNTIME" ] || [ ! -x "$RUNTIME/python/bin/python3" ]; then
  for candidate in "$HERE/runtime" "$HERE/../runtime" "/C/ModelSmith/runtime" "/opt/ModelSmith/runtime"; do
    if [ -x "$candidate/python/bin/python3" ]; then RUNTIME="$candidate"; break; fi
  done
fi
if [ -z "$RUNTIME" ] || [ ! -x "$RUNTIME/python/bin/python3" ]; then
  echo "RUNTIME MISSING. Looked for: MODELSMITH_RUNTIME, dist/runtime, ../runtime, /C/ModelSmith/runtime" >&2
  exit 1
fi

PY="$RUNTIME/python/bin/python3"
CORE="$HERE/core/modelsmith_core.py"
if [ ! -f "$CORE" ]; then CORE="$ROOT/core/modelsmith_core.py"; fi
if [ ! -f "$CORE" ]; then echo "CORE MISSING: $CORE" >&2; exit 1; fi

mkdir -p "$DATA" "$DATA/logs"
export MODELSMITH_HOME="$ROOT"
export PYTHONUTF8=1
export PYTHONNOUSERSITE=1
exec "$PY" "$CORE" --data-dir "$DATA" --project-root "$ROOT" \
  --laya-python "$PY" --llm-python "$PY" --aitk-python "$PY" \
  --diffsynth-python "$PY" --lf-python "$PY" "$@"
