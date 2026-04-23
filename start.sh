#!/bin/bash
set -euo pipefail

# Mirror dashboard-ref-only's startup: create every directory hermes expects
# and seed a default config.yaml if the volume is empty. Without these,
# `hermes dashboard` endpoints that hit logs/, sessions/, cron/, etc. can fail
# with opaque errors even though no auth is actually involved.
mkdir -p /data/.hermes/cron /data/.hermes/sessions /data/.hermes/logs          /data/.hermes/memories /data/.hermes/skills /data/.hermes/pairing          /data/.hermes/hooks /data/.hermes/image_cache /data/.hermes/audio_cache          /data/.hermes/workspace /data/.hermes/webui/sessions /data/code

if [ ! -f /data/.hermes/config.yaml ] && [ -f /opt/hermes-agent/cli-config.yaml.example ]; then
  cp /opt/hermes-agent/cli-config.yaml.example /data/.hermes/config.yaml
fi

[ ! -f /data/.hermes/.env ] && touch /data/.hermes/.env

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
