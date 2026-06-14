#!/usr/bin/env bash
#
# fix-kempinski-tenant.sh — diagnose & fix the Çırağan/Kempinski KB tenant.
#
# Problem: the Çırağan docs (incl. the Tuğra menu) were ingested under
# hotel_id="demo", but the WhatsApp bot queries hotel_id="kempinski_ciragan",
# so KB lookups return nothing ("I don't have the menu"). The chunks are already
# embedded correctly — only the tenant tag is wrong — so the fix is a relabel
# (SQL UPDATE), NOT a re-ingest. Turkish docs mis-tagged language="en" are fixed
# too. No sqlite3 binary needed: runs a stdlib-only Python helper on the droplet.
#
# Connects over SSH (same host/user/paths as deploy-droplet.sh) and runs the helper
# as the service user with the prod env sourced, so it hits the exact DB the bot
# reads (/home/<user>/.voxtera/voxtera.db via $VOXTERA_DB_PATH).
#
# USAGE
#   ./scripts/fix-kempinski-tenant.sh                  # report only
#   ./scripts/fix-kempinski-tenant.sh --apply --restart  # relabel + restart
#
# FLAGS
#   --host <alias>      SSH host (default: voxtera)
#   --apply             Relabel the tenant + fix Turkish language tags
#   --restart           Restart the KB-caching service(s) after apply
#   --services "<list>" Services to restart (default: "voxtera-concierge voxtera-whatsapp")
#
set -euo pipefail

HOST="${HOST:-voxtera}"
REMOTE_USER="${REMOTE_USER:-voxtera}"
REMOTE_APP_DIR="${REMOTE_APP_DIR:-/opt/voxtera/app}"
REMOTE_ENV_FILE="${REMOTE_ENV_FILE:-/etc/voxtera/voxtera.env}"
SERVICES="voxtera-concierge voxtera-whatsapp"
DO_APPLY=0; DO_RESTART=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)     HOST="$2"; shift 2;;
    --services) SERVICES="$2"; shift 2;;
    --apply)    DO_APPLY=1; shift;;
    --restart)  DO_RESTART=1; shift;;
    -h|--help)  sed -n '2,30p' "$0"; exit 0;;
    *) echo "Unknown arg: $1" >&2; exit 2;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER_LOCAL="${SCRIPT_DIR}/kb_tenant_report.py"
HELPER_REMOTE="/tmp/kb_tenant_report.py"
[[ -f "${HELPER_LOCAL}" ]] || { echo "Missing ${HELPER_LOCAL}" >&2; exit 1; }

# Run a command on the droplet AS the service user with the prod env sourced.
appcmd() {
  ssh "${HOST}" "su - '${REMOTE_USER}' -c 'cd \"${REMOTE_APP_DIR}\" && set -a && source \"${REMOTE_ENV_FILE}\" && set +a && $*'"
}
rootcmd() { ssh "${HOST}" "$*"; }

echo "==> Host=${HOST}  user=${REMOTE_USER}  apply=${DO_APPLY}  restart=${DO_RESTART}"
rootcmd 'echo "connected to $(hostname) as $(whoami)"'

echo
echo "==> Services matching voxtera* (adjust --services if the names differ):"
rootcmd "systemctl list-units --type=service --all 'voxtera*' --no-pager | sed -n '1,20p'" || true

echo
echo "==> Shipping the report helper to ${HOST}:${HELPER_REMOTE}"
scp -q "${HELPER_LOCAL}" "${HOST}:${HELPER_REMOTE}"
rootcmd "chmod 644 '${HELPER_REMOTE}'"

echo
echo "==> WHATSAPP_HOTEL_ID on the droplet:"
appcmd 'echo "  WHATSAPP_HOTEL_ID=${WHATSAPP_HOTEL_ID:-<unset>}"'

if [[ "${DO_APPLY}" -eq 1 ]]; then
  echo
  echo "==> [APPLY] relabelling tenant + fixing language tags"
  appcmd "./.venv/bin/python '${HELPER_REMOTE}' --apply"

  if [[ "${DO_RESTART}" -eq 1 ]]; then
    echo
    echo "==> [RESTART] ${SERVICES}"
    for svc in ${SERVICES}; do
      rootcmd "systemctl restart '${svc}' && echo '  ${svc}: '\$(systemctl is-active '${svc}')" \
        || echo "  (could not restart ${svc} — check the name in the list above)"
    done
  else
    echo
    echo "NOTE: not restarted. The service caches the KB per hotel — restart it to"
    echo "      pick up the change:  ssh ${HOST} systemctl restart ${SERVICES}"
  fi
else
  echo
  echo "==> [REPORT ONLY]"
  appcmd "./.venv/bin/python '${HELPER_REMOTE}'"
  echo
  echo "If the report shows the Çırağan docs under hotel_id=demo and 0 chunks for"
  echo "kempinski_ciragan, fix it with:  $0 --host ${HOST} --apply --restart"
fi
