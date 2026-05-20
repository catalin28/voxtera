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
  rsync -az --delete \
    --exclude '.git/' \
    --exclude '.venv/' \
    --exclude '__pycache__/' \
    --exclude '.pytest_cache/' \
    --exclude '.mypy_cache/' \
    --exclude '.ruff_cache/' \
    --exclude 'logs/*.jsonl' \
    --exclude 'logs/*.db*' \
    --exclude '.env' \
    ./ "${HOST}:${REMOTE_APP_DIR}/"

  ssh "${HOST}" "chown -R '${REMOTE_USER}:${REMOTE_USER}' '${REMOTE_APP_DIR}'"
else
  echo "==> Skipping file sync"
fi

if [[ -f .env ]]; then
  echo "==> Deploying .env to ${HOST}:${REMOTE_ENV_FILE}"
  ssh "${HOST}" "mkdir -p '$(dirname "${REMOTE_ENV_FILE}")'"
  scp .env "${HOST}:${REMOTE_ENV_FILE}"
  # Rewrite any relative path values that need to be absolute on the server.
  # GOOGLE_APPLICATION_CREDENTIALS is relative locally (project root) but
  # serve.py runs from demo-hotel/, so it must be absolute on the server.
  ssh "${HOST}" "sed -i 's|GOOGLE_APPLICATION_CREDENTIALS=\.secrets/|GOOGLE_APPLICATION_CREDENTIALS=${REMOTE_APP_DIR}/.secrets/|g' '${REMOTE_ENV_FILE}'"
  ssh "${HOST}" "chown root:'${REMOTE_USER}' '${REMOTE_ENV_FILE}' && chmod 640 '${REMOTE_ENV_FILE}'"
else
  echo "==> No local .env found, skipping env file deploy"
fi

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

echo "==> Conflict guard — verifying standalone bot is NOT running"
BOT_ACTIVE=$(ssh "${HOST}" "systemctl is-active '${SERVICE_NAME}' 2>/dev/null || true")
if [[ "$BOT_ACTIVE" == "active" ]]; then
  echo "ERROR: ${SERVICE_NAME} is still active! This will cause a dual-bot conflict." >&2
  echo "       Run: ssh ${HOST} systemctl stop ${SERVICE_NAME} && systemctl disable ${SERVICE_NAME}" >&2
  exit 1
fi
echo "    OK — ${SERVICE_NAME} is not running (status: ${BOT_ACTIVE})"

echo "==> Deploy complete"
echo "Open: https://143.198.35.136.sslip.io/demo.html"
