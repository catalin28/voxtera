#!/usr/bin/env bash

set -euo pipefail

HOST="${HOST:-voxtera}"
REMOTE_APP_DIR="${REMOTE_APP_DIR:-/opt/voxtera/app}"
REMOTE_USER="${REMOTE_USER:-voxtera}"
SERVICE_NAME="${SERVICE_NAME:-voxtera}"
UI_SERVICE_NAME="${UI_SERVICE_NAME:-voxtera-demo-ui}"
REMOTE_ENV_FILE="${REMOTE_ENV_FILE:-/etc/voxtera/voxtera.env}"
HOTEL_ID="${HOTEL_ID:-demo}"
CONTENT_DIR="${CONTENT_DIR:-/opt/voxtera/app/demo-hotel}"
PSTN_WEBHOOK_URL="${PSTN_WEBHOOK_URL:-https://voxtera.io/pstn/webhook}"
CONFIGURE_PINLESS_DIALIN="${CONFIGURE_PINLESS_DIALIN:-true}"
# Concierge service (warm pipeline + WhatsApp text/calls) — one async service
# on CONCIERGE_PORT, fronted by the reverse proxy at voxtera.io/api/concierge*
# and voxtera.io/whatsapp/*. Supersedes the old voxtera-whatsapp unit (:8200).
CONCIERGE_SERVICE_NAME="${CONCIERGE_SERVICE_NAME:-voxtera-concierge}"
CONCIERGE_PORT="${CONCIERGE_PORT:-8300}"
LEGACY_WHATSAPP_SERVICE_NAME="${LEGACY_WHATSAPP_SERVICE_NAME:-voxtera-whatsapp}"
CADDYFILE="${CADDYFILE:-/etc/caddy/Caddyfile}"
CONFIGURE_CADDY="${CONFIGURE_CADDY:-true}"

INGEST_RAG="true"
SKIP_SYNC="false"

usage() {
  cat <<'EOF'
Usage: scripts/deploy-droplet.sh [options]

Deploy Voxtera to the configured SSH host.

Options:
  --host <ssh-host>           SSH host alias (default: voxtera)
  --app-dir <remote-path>     Remote app path (default: /opt/voxtera/app)
  --user <remote-user>        Remote Linux user that owns app files (default: voxtera)
  --service <name>            Backend systemd service name (default: voxtera)
  --ui-service <name>         UI systemd service name (default: voxtera-demo-ui)
  --env-file <remote-path>    Remote env file path (default: /etc/voxtera/voxtera.env)
  --hotel-id <id>             Hotel id for ingest (default: demo)
  --content-dir <remote-path> Content folder for ingest (default: /opt/voxtera/app/demo-hotel)
  --concierge-service <name>  Concierge systemd service name (default: voxtera-concierge)
  --concierge-port <port>     Concierge service port (default: 8300)
  --caddyfile <remote-path>   Caddyfile to route /api/concierge* + /whatsapp/* (default: /etc/caddy/Caddyfile)
  --skip-caddy                Do not touch the Caddyfile (print the route snippet instead)
  --skip-ingest               Do not run RAG ingest
  --skip-sync                 Do not rsync files (restart/install only)
  -h, --help                  Show this help

Examples:
  scripts/deploy-droplet.sh
  scripts/deploy-droplet.sh --host voxtera --hotel-id demo
  scripts/deploy-droplet.sh --skip-ingest
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      HOST="$2"
      shift 2
      ;;
    --app-dir)
      REMOTE_APP_DIR="$2"
      shift 2
      ;;
    --user)
      REMOTE_USER="$2"
      shift 2
      ;;
    --service)
      SERVICE_NAME="$2"
      shift 2
      ;;
    --ui-service)
      UI_SERVICE_NAME="$2"
      shift 2
      ;;
    --env-file)
      REMOTE_ENV_FILE="$2"
      shift 2
      ;;
    --hotel-id)
      HOTEL_ID="$2"
      shift 2
      ;;
    --content-dir)
      CONTENT_DIR="$2"
      shift 2
      ;;
    --concierge-service)
      CONCIERGE_SERVICE_NAME="$2"
      shift 2
      ;;
    --concierge-port)
      CONCIERGE_PORT="$2"
      shift 2
      ;;
    --caddyfile)
      CADDYFILE="$2"
      shift 2
      ;;
    --skip-caddy)
      CONFIGURE_CADDY="false"
      shift
      ;;
    --skip-ingest)
      INGEST_RAG="false"
      shift
      ;;
    --skip-sync)
      SKIP_SYNC="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if ! command -v ssh >/dev/null 2>&1; then
  echo "Error: ssh not found on this machine." >&2
  exit 1
fi

if [[ "$SKIP_SYNC" == "false" ]] && ! command -v rsync >/dev/null 2>&1; then
  echo "Error: rsync not found on this machine." >&2
  exit 1
fi

echo "==> Checking SSH connectivity to ${HOST}"
ssh "${HOST}" 'echo "Connected to $(hostname) as $(whoami)"'

if [[ "$SKIP_SYNC" == "false" ]]; then
  echo "==> Syncing project files to ${HOST}:${REMOTE_APP_DIR}"
  ssh "${HOST}" "mkdir -p '${REMOTE_APP_DIR}'"
  ssh "${HOST}" "rm -rf '${REMOTE_APP_DIR}/tests'"
  rsync -az --delete \
    --exclude '.git/' \
    --exclude '.venv/' \
    --exclude '__pycache__/' \
    --exclude '.pytest_cache/' \
    --exclude '.mypy_cache/' \
    --exclude '.ruff_cache/' \
    --exclude 'tests/' \
    --exclude 'downloads/' \
    --exclude 'logs/*.jsonl' \
    --exclude 'logs/*.db*' \
    --exclude 'logs/calls/' \
    --exclude 'demo-hotel/logs/' \
    --exclude 'demo-hotel/traces/' \
    --exclude '.env' \
    ./ "${HOST}:${REMOTE_APP_DIR}/"

  ssh "${HOST}" "chown -R '${REMOTE_USER}:${REMOTE_USER}' '${REMOTE_APP_DIR}'"
else
  echo "==> Skipping file sync"
fi

if [[ -f .env ]]; then
  echo "==> Deploying .env to ${HOST}:${REMOTE_ENV_FILE}"
  REMOTE_PSTN_HMAC_VERIFY=$(ssh "${HOST}" "grep '^PSTN_HMAC_VERIFY=' '${REMOTE_ENV_FILE}' 2>/dev/null || true")
  ssh "${HOST}" "mkdir -p '$(dirname "${REMOTE_ENV_FILE}")'"
  scp .env "${HOST}:${REMOTE_ENV_FILE}"
  # Rewrite any relative path values that need to be absolute on the server.
  # GOOGLE_APPLICATION_CREDENTIALS is relative locally (project root) but
  # serve.py runs from demo-hotel/, so it must be absolute on the server.
  ssh "${HOST}" "sed -i 's|GOOGLE_APPLICATION_CREDENTIALS=\.secrets/|GOOGLE_APPLICATION_CREDENTIALS=${REMOTE_APP_DIR}/.secrets/|g' '${REMOTE_ENV_FILE}'"
  if ! grep -q '^PSTN_HMAC_VERIFY=' .env && [[ -n "${REMOTE_PSTN_HMAC_VERIFY}" ]]; then
    echo "==> Preserving remote PSTN_HMAC_VERIFY override"
    ssh "${HOST}" "printf '\n%s\n' '${REMOTE_PSTN_HMAC_VERIFY}' >> '${REMOTE_ENV_FILE}'"
  fi
  ssh "${HOST}" "chown root:'${REMOTE_USER}' '${REMOTE_ENV_FILE}' && chmod 640 '${REMOTE_ENV_FILE}'"
else
  echo "==> No local .env found, skipping env file deploy"
fi

if [[ "$CONFIGURE_PINLESS_DIALIN" == "true" ]]; then
  if [[ ! -f .env ]]; then
    echo "Error: .env is required to configure Daily pinless dial-in." >&2
    exit 1
  fi

  echo "==> Configuring Daily pinless dial-in"
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a

  if [[ -z "${DAILY_API_KEY:-}" || -z "${PSTN_PHONE_NUMBER:-}" || -z "${PSTN_WEBHOOK_HMAC:-}" ]]; then
    echo "Error: DAILY_API_KEY, PSTN_PHONE_NUMBER, and PSTN_WEBHOOK_HMAC must be set in .env" >&2
    exit 1
  fi

  PINLESS_PAYLOAD=$(printf '{"properties":{"pinless_dialin":[{"phone_number":"%s","room_creation_api":"%s","name_prefix":"VCI","hmac":"%s"}]}}' \
    "$PSTN_PHONE_NUMBER" "$PSTN_WEBHOOK_URL" "$PSTN_WEBHOOK_HMAC")
  PINLESS_RESPONSE=$(curl -fsS -X POST https://api.daily.co/v1/ \
    -H "Authorization: Bearer ${DAILY_API_KEY}" \
    -H 'Content-Type: application/json' \
    --data "$PINLESS_PAYLOAD")

  if command -v jq >/dev/null 2>&1; then
    echo "$PINLESS_RESPONSE" | jq '{pinless_dialin:(.config.pinless_dialin // null)}'
  else
    echo "$PINLESS_RESPONSE"
  fi
fi

echo "==> Ensuring system libs for WhatsApp-call WebRTC (OpenCV needs libGL)"
ssh "${HOST}" "DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libgl1 libglib2.0-0" \
  || echo "    WARNING: could not install libgl1/libglib2.0-0 — WhatsApp calls will fail to import cv2 without them."

echo "==> Installing/updating dependencies"
ssh "${HOST}" "su - '${REMOTE_USER}' -c 'cd \"${REMOTE_APP_DIR}\" && /home/${REMOTE_USER}/.local/bin/uv sync'"

if [[ "$INGEST_RAG" == "true" ]]; then
  echo "==> Running RAG ingest for hotel_id=${HOTEL_ID}"
  ssh "${HOST}" "su - '${REMOTE_USER}' -c 'cd \"${REMOTE_APP_DIR}\" && set -a && source \"${REMOTE_ENV_FILE}\" && set +a && ./.venv/bin/voxtera ingest --hotel \"${HOTEL_ID}\" \"${CONTENT_DIR}\"'"
else
  echo "==> Skipping RAG ingest"
fi

echo "==> Restarting services"
# In on-demand (serve.py) mode, the standalone bot service must NEVER run
# alongside the UI launcher — both would join the same Daily room and the
# standalone bot would hold the Gladia STT session, causing 429 errors on
# every user session.  We stop + disable it unconditionally on every deploy.
ssh "${HOST}" "systemctl stop '${SERVICE_NAME}' || true"
ssh "${HOST}" "systemctl disable '${SERVICE_NAME}' || true"
# Kill UI service and any stale process on port 8080, then restart.
ssh "${HOST}" "systemctl stop '${UI_SERVICE_NAME}' || true; fuser -k 8080/tcp 2>/dev/null || true"
echo "    Waiting 30s for Telegram long-poll to expire..."
sleep 30
ssh "${HOST}" "systemctl start '${UI_SERVICE_NAME}'"

echo "==> Health checks"
ssh "${HOST}" "systemctl --no-pager --full status '${UI_SERVICE_NAME}' | head -n 25"
ssh "${HOST}" "journalctl -u '${UI_SERVICE_NAME}' -n 20 --no-pager | egrep 'error|FATAL|Fatal' || true"

# --------------------------------------------------------------------------
# Concierge service (warm pipeline + WhatsApp text/calls), on CONCIERGE_PORT.
# Generated inline so the unit always matches the deploy variables.
# Supersedes the old voxtera-whatsapp unit, which is stopped + disabled.
# --------------------------------------------------------------------------
echo "==> Installing/updating ${CONCIERGE_SERVICE_NAME} systemd service (port ${CONCIERGE_PORT})"
ssh "${HOST}" "REMOTE_APP_DIR='${REMOTE_APP_DIR}' REMOTE_USER='${REMOTE_USER}' REMOTE_ENV_FILE='${REMOTE_ENV_FILE}' CONCIERGE_SERVICE_NAME='${CONCIERGE_SERVICE_NAME}' CONCIERGE_PORT='${CONCIERGE_PORT}' LEGACY_WHATSAPP_SERVICE_NAME='${LEGACY_WHATSAPP_SERVICE_NAME}' bash -s" <<'REMOTE_WA'
set -e
# Retire the superseded WhatsApp-only unit (its routes now live here).
systemctl stop "${LEGACY_WHATSAPP_SERVICE_NAME}" 2>/dev/null || true
systemctl disable "${LEGACY_WHATSAPP_SERVICE_NAME}" 2>/dev/null || true
UNIT="/etc/systemd/system/${CONCIERGE_SERVICE_NAME}.service"
cat > "$UNIT" <<UNITEOF
[Unit]
Description=Voxtera concierge service (pipeline + WhatsApp text/calls)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${REMOTE_USER}
WorkingDirectory=${REMOTE_APP_DIR}
EnvironmentFile=${REMOTE_ENV_FILE}
Environment=CONCIERGE_PORT=${CONCIERGE_PORT}
ExecStart=/home/${REMOTE_USER}/.local/bin/uv run python -m voxtera.concierge_service
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNITEOF
systemctl daemon-reload
systemctl enable "${CONCIERGE_SERVICE_NAME}"
# Free the port in case a stale instance is bound, then (re)start.
fuser -k "${CONCIERGE_PORT}/tcp" 2>/dev/null || true
systemctl restart "${CONCIERGE_SERVICE_NAME}"
REMOTE_WA

echo "==> ${CONCIERGE_SERVICE_NAME} health check"
ssh "${HOST}" "systemctl --no-pager --full status '${CONCIERGE_SERVICE_NAME}' | head -n 15"
ssh "${HOST}" "journalctl -u '${CONCIERGE_SERVICE_NAME}' -n 20 --no-pager | egrep -i 'error|FATAL|Traceback' || true"
echo "==> ${CONCIERGE_SERVICE_NAME} /health gate (30s)"
ssh "${HOST}" "for i in \$(seq 1 30); do curl -fsS --max-time 2 http://127.0.0.1:${CONCIERGE_PORT}/health >/dev/null 2>&1 && { echo '    OK — /health responding'; exit 0; }; sleep 1; done; echo '    ERROR: /health not responding after 30s' >&2; exit 1"

# --------------------------------------------------------------------------
# Reverse proxy: route voxtera.io/whatsapp/* AND voxtera.io/api/concierge*
# to the concierge service. Safe + idempotent: backs up the Caddyfile,
# inserts missing routes inside the voxtera.io site block, validates,
# reloads, and rolls back on failure. Stale routes pointing at the old
# WhatsApp port (:8200) are rewritten to the concierge port.
# --------------------------------------------------------------------------
if [[ "$CONFIGURE_CADDY" == "true" ]]; then
  echo "==> Ensuring Caddy routes /whatsapp/* + /api/concierge* → localhost:${CONCIERGE_PORT}"
  ssh "${HOST}" "CADDYFILE='${CADDYFILE}' CONCIERGE_PORT='${CONCIERGE_PORT}' bash -s" <<'REMOTE_CADDY'
set -e
ROUTES=(
  "reverse_proxy /whatsapp/* localhost:${CONCIERGE_PORT}"
  "reverse_proxy /api/concierge* localhost:${CONCIERGE_PORT}"
)
if [[ ! -f "$CADDYFILE" ]]; then
  echo "    WARNING: $CADDYFILE not found. Add these inside your voxtera.io site block manually:"
  printf '        %s\n' "${ROUTES[@]}"
  exit 0
fi
BACKUP="${CADDYFILE}.bak.$(date +%s)"
cp "$CADDYFILE" "$BACKUP"
WORK="${CADDYFILE}.new"
cp "$CADDYFILE" "$WORK"
CHANGED=0
# Rewrite a stale /whatsapp route still pointing at the retired :8200 service.
if grep -qE "reverse_proxy /whatsapp/\* localhost:[0-9]+" "$WORK" \
   && ! grep -qF "${ROUTES[0]}" "$WORK"; then
  sed -i -E "s|reverse_proxy /whatsapp/\* localhost:[0-9]+|${ROUTES[0]}|" "$WORK"
  CHANGED=1
fi
for ROUTE in "${ROUTES[@]}"; do
  if grep -qF "$ROUTE" "$WORK"; then
    continue
  fi
  # Insert the route on the line right after the 'voxtera.io ... {' opener.
  awk -v route="    ${ROUTE}" '
    !ins && $0 ~ /voxtera\.io[^{]*\{[[:space:]]*$/ { print; print route; ins=1; next }
    { print }
    END { if (!ins) exit 3 }
  ' "$WORK" > "${WORK}.tmp" || {
    echo "    WARNING: no 'voxtera.io {' opener found. Add this inside that block manually:"
    echo "        $ROUTE"
    rm -f "${WORK}.tmp"
    continue
  }
  mv "${WORK}.tmp" "$WORK"
  CHANGED=1
done
if [[ "$CHANGED" == "0" ]]; then
  echo "    Routes already present — nothing to do."
  rm -f "$WORK"
  exit 0
fi
if command -v caddy >/dev/null 2>&1; then
  if ! caddy validate --adapter caddyfile --config "$WORK" >/dev/null 2>&1; then
    echo "    ERROR: caddy validate failed — keeping the original Caddyfile."
    rm -f "$WORK"
    exit 1
  fi
fi
mv "$WORK" "$CADDYFILE"
if ! { systemctl reload caddy 2>/dev/null || caddy reload --adapter caddyfile --config "$CADDYFILE" 2>/dev/null; }; then
  echo "    ERROR: Caddy reload failed — restoring backup."
  cp "$BACKUP" "$CADDYFILE"
  systemctl reload caddy 2>/dev/null || true
  exit 1
fi
echo "    Routes ensured and Caddy reloaded (backup: $BACKUP)."
REMOTE_CADDY
else
  echo "==> Skipping Caddy config (add manually inside voxtera.io block):"
  echo "        reverse_proxy /whatsapp/* localhost:${CONCIERGE_PORT}"
  echo "        reverse_proxy /api/concierge* localhost:${CONCIERGE_PORT}"
fi

echo "==> Conflict guard — verifying standalone bot is NOT running"
BOT_ACTIVE=$(ssh "${HOST}" "systemctl is-active '${SERVICE_NAME}' 2>/dev/null || true")
if [[ "$BOT_ACTIVE" == "active" ]]; then
  echo "ERROR: ${SERVICE_NAME} is still active! This will cause a dual-bot conflict." >&2
  echo "       Run: ssh ${HOST} systemctl stop ${SERVICE_NAME} && systemctl disable ${SERVICE_NAME}" >&2
  exit 1
fi
echo "    OK — ${SERVICE_NAME} is not running (status: ${BOT_ACTIVE})"

# --------------------------------------------------------------------------
# Post-deploy smoke test: hit the public WhatsApp webhook handshake exactly
# as Meta does. A correct echo proves the Caddy route + the WhatsApp service
# + the verify token are all live end-to-end.
# --------------------------------------------------------------------------
echo "==> Verifying WhatsApp webhook at https://voxtera.io/whatsapp/webhook"
WA_VERIFY_TOKEN=""
if [[ -f .env ]]; then
  WA_VERIFY_TOKEN=$(grep -E '^WHATSAPP_WEBHOOK_VERIFY_TOKEN=' .env | head -1 | cut -d= -f2- | tr -d '\r"' | tr -d "'" | xargs || true)
fi
if [[ -n "${WA_VERIFY_TOKEN}" ]]; then
  WA_NONCE="vxq$(date +%s)"
  WA_RESP=$(curl -fsS --max-time 15 \
    "https://voxtera.io/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=${WA_VERIFY_TOKEN}&hub.challenge=${WA_NONCE}" \
    2>/dev/null || true)
  if [[ "${WA_RESP}" == "${WA_NONCE}" ]]; then
    echo "    OK — handshake echoed the challenge (Caddy route + ${CONCIERGE_SERVICE_NAME} live)."
  else
    echo "    WARNING: handshake did not echo the challenge (got: '${WA_RESP}')." >&2
    echo "             Check: '${CONCIERGE_SERVICE_NAME}' active, Caddy /whatsapp/* route, verify token." >&2
  fi
else
  echo "    Skipped — WHATSAPP_WEBHOOK_VERIFY_TOKEN not found in local .env."
fi

echo "==> Deploy complete"
echo "Open: https://143.198.35.136.sslip.io/demo.html"
