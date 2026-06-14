#!/usr/bin/env bash
#
# ingest-kempinski.sh — (re)ingest the Çırağan/Kempinski knowledge CORRECTLY.
#
# deploy-droplet.sh's built-in ingest can't do this property: it uses one --hotel
# and one --language (default en) for a whole folder, so it mis-files the docs
# under hotel_id=demo and tags the Turkish docs as en. This script ingests each
# language folder under the correct tenant + language, which is idempotent
# (the CLI deletes+re-adds per doc), then restarts the KB-caching service.
#
# Run AFTER syncing files to the droplet, e.g.:
#   ./scripts/deploy-droplet.sh --skip-ingest   # sync code+docs, no ingest
#   ./scripts/ingest-kempinski.sh               # ingest KB correctly under the right tenant
#
# FLAGS
#   --host <alias>        SSH host (default: voxtera)
#   --hotel-id <id>       Tenant (default: kempinski_ciragan)
#   --content-dir <path>  Remote docs root with en/ and tr/ (default: <app>/kempinski-hotel)
#   --no-restart          Ingest only; don't restart services
#   --services "<list>"   Services to restart (default: "voxtera-concierge voxtera-whatsapp")
#
set -euo pipefail

HOST="${HOST:-voxtera}"
REMOTE_USER="${REMOTE_USER:-voxtera}"
REMOTE_APP_DIR="${REMOTE_APP_DIR:-/opt/voxtera/app}"
REMOTE_ENV_FILE="${REMOTE_ENV_FILE:-/etc/voxtera/voxtera.env}"
HOTEL_ID="kempinski_ciragan"
CONTENT_DIR=""
SERVICES="voxtera-concierge voxtera-whatsapp"
DO_RESTART=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)        HOST="$2"; shift 2;;
    --hotel-id)    HOTEL_ID="$2"; shift 2;;
    --content-dir) CONTENT_DIR="$2"; shift 2;;
    --services)    SERVICES="$2"; shift 2;;
    --no-restart)  DO_RESTART=0; shift;;
    -h|--help)     sed -n '2,30p' "$0"; exit 0;;
    *) echo "Unknown arg: $1" >&2; exit 2;;
  esac
done
CONTENT_DIR="${CONTENT_DIR:-${REMOTE_APP_DIR}/kempinski-hotel}"

appcmd() {
  ssh "${HOST}" "su - '${REMOTE_USER}' -c 'cd \"${REMOTE_APP_DIR}\" && set -a && source \"${REMOTE_ENV_FILE}\" && set +a && $*'"
}
rootcmd() { ssh "${HOST}" "$*"; }

echo "==> Host=${HOST}  tenant=${HOTEL_ID}  content=${CONTENT_DIR}"
rootcmd 'echo "connected to $(hostname) as $(whoami)"'

echo
echo "==> Ingest EN docs (language=en)"
appcmd "test -d '${CONTENT_DIR}/en' && ./.venv/bin/voxtera ingest --hotel '${HOTEL_ID}' --language en '${CONTENT_DIR}/en' || echo 'WARN: no ${CONTENT_DIR}/en'"

echo
echo "==> Ingest TR docs (language=tr)"
appcmd "test -d '${CONTENT_DIR}/tr' && ./.venv/bin/voxtera ingest --hotel '${HOTEL_ID}' --language tr '${CONTENT_DIR}/tr' || echo 'WARN: no ${CONTENT_DIR}/tr'"

echo
echo "==> Verify: chunks now under ${HOTEL_ID}"
appcmd "./.venv/bin/voxtera list-chunks --hotel '${HOTEL_ID}' | tail -n 5 || true"
appcmd "./.venv/bin/voxtera search --hotel '${HOTEL_ID}' --query 'Tuğra menu' || true"

if [[ "${DO_RESTART}" -eq 1 ]]; then
  echo
  echo "==> Restart ${SERVICES} (clears in-memory KB cache)"
  for svc in ${SERVICES}; do
    rootcmd "systemctl restart '${svc}' && echo '  ${svc}: '\$(systemctl is-active '${svc}')" \
      || echo "  (could not restart ${svc} — check the name)"
  done
else
  echo
  echo "NOTE: not restarted. Run: ssh ${HOST} systemctl restart ${SERVICES}"
fi
