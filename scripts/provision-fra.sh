#!/usr/bin/env bash
#
# One-time bootstrap for the Frankfurt (EU) Voxtera droplet.
#
# Run this ONCE on a fresh Ubuntu 24.04 box, then use scripts/deploy-fra.sh for
# every release. It does NOT touch app code or the Python venv — deploy-fra.sh's
# `uv sync` builds the venv. This only lays the foundation:
#   - voxtera system user + /opt/voxtera/app + /etc/voxtera
#   - uv (the package manager) installed for the voxtera user
#   - a swap file (graceful memory cushion instead of OOM kills)
#   - the voxtera-demo-ui systemd unit (enabled, but NOT started — no code yet)
#   - Caddy reverse proxy with automatic Let's Encrypt TLS for the domain
#
# Run from your Mac (the voxtera-fra SSH alias + key live here, not on the box):
#   scripts/provision-fra.sh
#   scripts/provision-fra.sh --host voxtera-fra --email you@voxtera.io
#
# Assumes you SSH in as root (DigitalOcean default). If you log in as a sudo
# user instead, prefix the remote commands with sudo.
#
set -euo pipefail

HOST="${HOST:-voxtera-fra}"
REMOTE_USER="${REMOTE_USER:-voxtera}"
REMOTE_APP_DIR="${REMOTE_APP_DIR:-/opt/voxtera/app}"
REMOTE_ENV_FILE="${REMOTE_ENV_FILE:-/etc/voxtera/voxtera.env}"
UI_SERVICE_NAME="${UI_SERVICE_NAME:-voxtera-demo-ui}"
DOMAIN="${DOMAIN:-eu.voxtera.io}"
APP_PORT="${APP_PORT:-8080}"
SWAP_SIZE="${SWAP_SIZE:-2G}"
ACME_EMAIL="${ACME_EMAIL:-admin@voxtera.io}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)   HOST="$2";       shift 2;;
    --email)  ACME_EMAIL="$2"; shift 2;;
    --domain) DOMAIN="$2";     shift 2;;
    --port)   APP_PORT="$2";   shift 2;;
    --swap)   SWAP_SIZE="$2";  shift 2;;
    -h|--help)
      echo "Usage: scripts/provision-fra.sh [--host h] [--email e] [--domain d] [--port p] [--swap 2G]"
      exit 0;;
    *) echo "Unknown argument: $1" >&2; exit 1;;
  esac
done

command -v ssh >/dev/null 2>&1 || { echo "Error: ssh not found." >&2; exit 1; }

echo "==> Provisioning ${HOST} for ${DOMAIN} (app port ${APP_PORT})"
ssh "${HOST}" 'echo "Connected to $(hostname) as $(whoami)"'

ssh "${HOST}" \
  "REMOTE_USER='${REMOTE_USER}' REMOTE_APP_DIR='${REMOTE_APP_DIR}' REMOTE_ENV_FILE='${REMOTE_ENV_FILE}' UI_SERVICE_NAME='${UI_SERVICE_NAME}' DOMAIN='${DOMAIN}' APP_PORT='${APP_PORT}' SWAP_SIZE='${SWAP_SIZE}' ACME_EMAIL='${ACME_EMAIL}' bash -s" <<'REMOTE'
set -euo pipefail

echo "==> [1/7] Base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl rsync psmisc jq ca-certificates gnupg \
  python3 python3-venv debian-keyring debian-archive-keyring apt-transport-https >/dev/null

echo "==> [2/7] voxtera user + directories"
id -u "$REMOTE_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$REMOTE_USER"
mkdir -p "$REMOTE_APP_DIR" "$REMOTE_APP_DIR/.secrets" "$(dirname "$REMOTE_ENV_FILE")" /opt/voxtera/logs
chown -R "$REMOTE_USER:$REMOTE_USER" /opt/voxtera
chmod 750 "$(dirname "$REMOTE_ENV_FILE")"

echo "==> [3/7] uv (package manager) for $REMOTE_USER"
if ! su - "$REMOTE_USER" -c 'test -x "$HOME/.local/bin/uv"'; then
  su - "$REMOTE_USER" -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
fi
su - "$REMOTE_USER" -c '"$HOME/.local/bin/uv" --version'

echo "==> [4/7] swap ($SWAP_SIZE)"
if ! swapon --show | grep -q '/swapfile'; then
  fallocate -l "$SWAP_SIZE" /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
free -h | awk '/Swap/{print "    "$0}'

echo "==> [5/7] systemd unit ($UI_SERVICE_NAME) — enabled, NOT started (deploy starts it)"
cat > "/etc/systemd/system/${UI_SERVICE_NAME}.service" <<UNIT
[Unit]
Description=Voxtera demo UI launcher (serve.py)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${REMOTE_USER}
Group=${REMOTE_USER}
WorkingDirectory=${REMOTE_APP_DIR}/demo-hotel
EnvironmentFile=${REMOTE_ENV_FILE}
ExecStart=${REMOTE_APP_DIR}/.venv/bin/python serve.py ${APP_PORT}
Restart=on-failure
RestartSec=3
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable "$UI_SERVICE_NAME" >/dev/null

echo "==> [6/7] Caddy (auto-TLS reverse proxy → 127.0.0.1:${APP_PORT})"
if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy >/dev/null
fi
cat > /etc/caddy/Caddyfile <<CADDY
{
    email ${ACME_EMAIL}
}

${DOMAIN} {
    reverse_proxy 127.0.0.1:${APP_PORT}
}
CADDY
systemctl enable caddy >/dev/null
systemctl restart caddy

echo "==> [7/7] Firewall"
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 22/tcp  >/dev/null || true
  ufw allow 80/tcp  >/dev/null || true
  ufw allow 443/tcp >/dev/null || true
  echo "    ufw active — opened 22/80/443"
else
  echo "    ufw not active — make sure any DigitalOcean cloud firewall allows 22/80/443"
fi

echo
echo "==> Bootstrap complete on $(hostname)."
echo "    Caddy will obtain a Let's Encrypt cert for ${DOMAIN} on the first HTTPS request."
echo "    ${UI_SERVICE_NAME} is enabled but NOT started yet (no code/venv until you deploy)."
REMOTE

echo
echo "==> Provision complete."
echo "Next steps:"
echo "  1) (optional) Copy Google creds if you want Google STT/TTS:"
echo "       scp .secrets/<your-google-creds>.json ${HOST}:${REMOTE_APP_DIR}/.secrets/"
echo "  2) Deploy the app (builds the venv + local hotel RAG, starts the service):"
echo "       scripts/deploy-fra.sh"
echo "  3) Open: https://${DOMAIN}/demo.html"
