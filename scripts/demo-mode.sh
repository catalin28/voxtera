#!/usr/bin/env bash
#
# demo-mode.sh — flip the WhatsApp channel between the two demos.
#
#   scripts/demo-mode.sh hotel <hotel_id>   # hotel concierge (id is REQUIRED)
#   scripts/demo-mode.sh travel             # travel agent (call center)
#   scripts/demo-mode.sh status             # show which mode is live
#
# Examples:
#   scripts/demo-mode.sh hotel demo
#   scripts/demo-mode.sh hotel kempinski_ciragan
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
REMOTE_APP_DIR="${REMOTE_APP_DIR:-/opt/voxtera/app}"

MODE="${1:-status}"
# No default — `hotel` mode requires an explicit id so you never silently
# fall back to the wrong property (the bug that set 'demo' unexpectedly).
HOTEL_ID="${2:-}"

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
    if [[ -z "${HOTEL_ID}" ]]; then
      echo "Error: 'hotel' mode requires a hotel_id." >&2
      echo "Usage: $0 hotel <hotel_id>" >&2
      echo "Available hotels on ${HOST}:" >&2
      ssh "${HOST}" "ls '${REMOTE_APP_DIR}/config/hotels/' 2>/dev/null | sed 's/\.yaml\$//' | sed 's/^/  - /'" >&2 || true
      exit 1
    fi
    # Fail fast if the hotel has no config on the droplet — otherwise the bot
    # would start, then error at call time when load_hotel_config can't find it.
    if ! ssh "${HOST}" "test -f '${REMOTE_APP_DIR}/config/hotels/${HOTEL_ID}.yaml'"; then
      echo "Error: no config found at ${REMOTE_APP_DIR}/config/hotels/${HOTEL_ID}.yaml on ${HOST}." >&2
      echo "Deploy the hotel first (scripts/deploy-fra.sh) or check the id. Available:" >&2
      ssh "${HOST}" "ls '${REMOTE_APP_DIR}/config/hotels/' 2>/dev/null | sed 's/\.yaml\$//' | sed 's/^/  - /'" >&2 || true
      exit 1
    fi
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
    echo "Usage: $0 hotel <hotel_id> | travel | status" >&2
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
