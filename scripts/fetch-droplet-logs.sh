#!/usr/bin/env bash
# Fetch Voxtera service logs from the droplet via journalctl over SSH.
#
# Snapshot mode (default): pulls the last N lines (or a time window) into a
# timestamped file under droplet-logs/. Old snapshots are pruned so the folder
# doesn't grow unbounded.
#
# Follow mode (--follow): live-tails the service into droplet-logs/<svc>_live.log
# and to your terminal. Ctrl-C to stop.
#
# Examples:
#   scripts/fetch-droplet-logs.sh                          # last 1000 lines of voxtera.service
#   scripts/fetch-droplet-logs.sh -n 5000                  # last 5000 lines
#   scripts/fetch-droplet-logs.sh --since "1 hour ago"     # last hour, regardless of length
#   scripts/fetch-droplet-logs.sh --service voxtera-demo-ui
#   scripts/fetch-droplet-logs.sh --follow                 # live tail

set -euo pipefail

HOST="${VOXTERA_SSH_HOST:-voxtera}"
SERVICE="voxtera"
LINES=1000
SINCE=""
FOLLOW=0
KEEP=20

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Fetch logs from the Voxtera droplet's systemd service via SSH.

Options:
  -n, --lines N        Number of lines to fetch (default: $LINES). Ignored with --follow or --since.
  -s, --since EXPR     Only logs since this expression, e.g. "1 hour ago", "today", "2026-05-02".
  -u, --service NAME   systemd unit name (default: $SERVICE). Try "voxtera-demo-ui" for the frontend.
  -H, --host HOST      SSH host alias (default: $HOST, override with VOXTERA_SSH_HOST).
  -f, --follow         Live-tail to droplet-logs/<service>_live.log (Ctrl-C to stop).
  -k, --keep N         Keep at most N timestamped snapshots per service (default: $KEEP).
  -h, --help           Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--lines)   LINES="$2"; shift 2 ;;
    -s|--since)   SINCE="$2"; shift 2 ;;
    -u|--service) SERVICE="$2"; shift 2 ;;
    -H|--host)    HOST="$2"; shift 2 ;;
    -f|--follow)  FOLLOW=1; shift ;;
    -k|--keep)    KEEP="$2"; shift 2 ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

# Project root = parent of this scripts/ directory. Logs land in droplet-logs/.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$REPO_ROOT/droplet-logs"
mkdir -p "$LOG_DIR"

# Sanity-check SSH host before we start streaming.
if ! ssh -o BatchMode=yes -o ConnectTimeout=5 "$HOST" true 2>/dev/null; then
  echo "[fetch-droplet-logs] ERROR: cannot ssh to '$HOST' non-interactively." >&2
  echo "                     Check ~/.ssh/config and that the key is loaded (ssh-add)." >&2
  exit 1
fi

if [[ $FOLLOW -eq 1 ]]; then
  OUT="$LOG_DIR/${SERVICE}_live.log"
  echo "[fetch-droplet-logs] following $SERVICE on $HOST → $OUT (Ctrl-C to stop)"
  # -f follow, --no-pager so output streams, short-iso for sortable timestamps.
  ssh "$HOST" "journalctl -u $SERVICE -f --no-pager -o short-iso" | tee "$OUT"
  exit 0
fi

# Snapshot mode.
TS="$(date +%Y-%m-%d_%H-%M-%S)"
OUT="$LOG_DIR/${SERVICE}_${TS}.log"

if [[ -n "$SINCE" ]]; then
  REMOTE_CMD="journalctl -u $SERVICE --no-pager -o short-iso --since \"$SINCE\""
  WINDOW_DESC="since \"$SINCE\""
else
  REMOTE_CMD="journalctl -u $SERVICE --no-pager -o short-iso -n $LINES"
  WINDOW_DESC="last $LINES lines"
fi

echo "[fetch-droplet-logs] $HOST :: $SERVICE :: $WINDOW_DESC"
ssh "$HOST" "$REMOTE_CMD" > "$OUT"

LINE_COUNT=$(wc -l < "$OUT" | tr -d ' ')
SIZE=$(du -h "$OUT" | cut -f1)
echo "[fetch-droplet-logs] saved $LINE_COUNT lines ($SIZE) → $OUT"

# Prune old timestamped snapshots for this service. The [0-9]* pattern keeps
# us from accidentally deleting <service>_live.log.
mapfile -t OLD < <(ls -1t "$LOG_DIR"/${SERVICE}_[0-9]*.log 2>/dev/null | tail -n +$((KEEP + 1)) || true)
if [[ ${#OLD[@]} -gt 0 ]]; then
  echo "[fetch-droplet-logs] pruning ${#OLD[@]} old snapshot(s) (keep=$KEEP)"
  rm -f "${OLD[@]}"
fi
