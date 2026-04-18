#!/bin/bash
set -euo pipefail

mkdir -p /data/.hermes/sessions \
         /data/.hermes/skills \
         /data/.hermes/workspace \
         /data/.hermes/pairing \
         /data/.hermes/webui/sessions \
         /data/code

WEBUI_SCRIPT="${HERMES_WEBUI_START_SCRIPT:-/data/hermes-webui/railway-start.sh}"
WEBUI_PID=""

cleanup() {
  if [[ -n "${WEBUI_PID}" ]] && kill -0 "${WEBUI_PID}" 2>/dev/null; then
    kill "${WEBUI_PID}" 2>/dev/null || true
    wait "${WEBUI_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if [[ -x "${WEBUI_SCRIPT}" ]]; then
  echo "[start.sh] Starting Hermes WebUI via ${WEBUI_SCRIPT}"
  "${WEBUI_SCRIPT}" &
  WEBUI_PID=$!
else
  echo "[start.sh] Hermes WebUI launcher not found at ${WEBUI_SCRIPT}; skipping WebUI startup"
fi

python /app/server.py &
ADMIN_PID=$!
wait "${ADMIN_PID}"
