#!/usr/bin/env bash
#
# demo-mode.sh — flip the WhatsApp channel between the two demos.
#
#   scripts/demo-mode.sh hotel [hotel_id]   # hotel concierge (default id: demo)
#   scripts/demo-mode.sh travel             # travel agent (call center)
#   scripts/demo-mode.sh status             # show which mode is live
#
# Sets/clears WHATSAPP_HOTEL_ID in the droplet env file and restarts the
# concierge service (P1.4 "one brain": the same pipeline serves both demos;
# the hotel id scope is the only difference). Affects WhatsApp calls AND
# texts. Takes effect on the next call (~5s restart).
#
set -euo pipefail

HOST="${HOST:-voxtera}"
ENV_FILE="${ENV_FILE:-/etc/voxtera/voxtera.env}"
SERVICE="${SERVICE:-voxtera-concierge}"
PORT="${PORT:-8300}"

MODE="${1:-status}"
HOTEL_ID="${2:-demo}"

status() {
  CURRENT=$(ssh "${HOST}" "grep -E '^WHATSAPP_HOTEL_ID=' '${ENV_FILE}' 2>/dev/null | cut -d= -f2" || true)
  ACTIVE=$(ssh "${HOST}" "systemctl is-active '${SERVICE}' 2>/dev/null" || true)
  HEALTH=$(ssh "${HOST}" "curl -fsS --max-time 3 http://127.0.0.1:${PORT}/health 2>/dev/null" || true)
  if [[ -n "${CURRENT}" ]]; then
    echo "Mode:    HOTEL CONCIERGE (hotel_id=${CURRENT})"
  else
    echo "Mode:    TRAVEL AGENT"
  fi
  echo "Service: ${SERVICE} is ${ACTIVE:-unknown}"
  echo "Health:  ${HEALTH:-NOT RESPONDING}"
}

case "${MODE}" in
  hotel)
    echo "==> Switching WhatsApp channel to HOTEL CONCIERGE (hotel_id=${HOTEL_ID})"
    ssh "${HOST}" "sed -i '/^WHATSAPP_HOTEL_ID=/d' '${ENV_FILE}' && \
                   printf 'WHATSAPP_HOTEL_ID=%s\n' '${HOTEL_ID}' >> '${ENV_FILE}' && \
                   systemctl restart '${SERVICE}'"
    ;;
  travel)
    echo "==> Switching WhatsApp channel to TRAVEL AGENT"
    ssh "${HOST}" "sed -i '/^WHATSAPP_HOTEL_ID=/d' '${ENV_FILE}' && \
                   systemctl restart '${SERVICE}'"
    ;;
  status)
    status
    exit 0
    ;;
  *)
    echo "Usage: $0 hotel [hotel_id] | travel | status" >&2
    exit 1
    ;;
esac

# Wait for the service to come back, then report.
echo "==> Waiting for ${SERVICE} to come back up"
for _ in $(seq 1 30); do
  if ssh "${HOST}" "curl -fsS --max-time 2 http://127.0.0.1:${PORT}/health > /dev/null 2>&1"; then
    break
  fi
  sleep 1
done
status
