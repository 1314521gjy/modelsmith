#!/usr/bin/env bash
# ModelSmith POSIX launcher (mirrors start-modelsmith.cmd; .cmd is untouched).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${START_CORE_PORT:-8765}"
DATA="${MODELSMITH_STATE:-$ROOT/state}"
RUNTIME="${MODELSMITH_RUNTIME:-}"

if [ -z "$RUNTIME" ] || [ ! -x "$RUNTIME/python/bin/python3" ]; then
  for candidate in "$ROOT/dist/runtime" "$ROOT/runtime" "/opt/ModelSmith/runtime" "/C/ModelSmith/runtime"; do
    if [ -x "$candidate/python/bin/python3" ]; then RUNTIME="$candidate"; break; fi
  done
fi
if [ -z "$RUNTIME" ] || [ ! -x "$RUNTIME/python/bin/python3" ]; then
  echo "RUNTIME MISSING. Looked for: MODELSMITH_RUNTIME, dist/runtime, runtime, /opt/ModelSmith/runtime, /C/ModelSmith/runtime" >&2
  exit 1
fi

PY="$RUNTIME/python/bin/python3"
CORE="$ROOT/core/modelsmith_core.py"
mkdir -p "$DATA" "$DATA/logs"
export MODELSMITH_HOME="$ROOT"
export PYTHONUTF8=1
export PYTHONNOUSERSITE=1

if command -v curl >/dev/null 2>&1 && curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  echo "http://127.0.0.1:$PORT/"
  exit 0
fi

echo "ModelSmith URL: http://127.0.0.1:$PORT/"
exec "$PY" "$CORE" \
  --port "$PORT" \
  --data-dir "$DATA" \
  --project-root "$ROOT" \
  --laya-python "$PY" \
  --llm-python "$PY" \
  --aitk-python "$PY" \
  --diffsynth-python "$PY" \
  --lf-python "$PY" \
  "$@"
