#!/usr/bin/env bash
# One-time setup of a Harvey staging server on Ubuntu 22.04 / 24.04.
#
# Run as root from inside the project folder (deploy/STAGING.md has the steps):
#   sudo bash deploy/staging-setup.sh
#
# Safe to run again. It never overwrites an existing .env, and it leaves the
# server in "staging" mode: every email waits for approval, and sending is
# paused until you resume it yourself.
set -euo pipefail

APP_USER="harvey"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$APP_DIR/.venv"
USER_HOME="/home/$APP_USER"

say() { printf '\n==> %s\n' "$*"; }
as_app() { sudo -u "$APP_USER" -H bash -lc "cd '$APP_DIR' && $*"; }

if [[ $EUID -ne 0 ]]; then
  echo "Run this as root: sudo bash deploy/staging-setup.sh" >&2
  exit 1
fi
if [[ ! -f "$APP_DIR/pyproject.toml" ]]; then
  echo "Can't find pyproject.toml in $APP_DIR. Run this from the Harvey folder." >&2
  exit 1
fi

say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git curl unzip sqlite3 ufw

say "Creating the '$APP_USER' user (Harvey never runs as root)"
id "$APP_USER" &>/dev/null || useradd --create-home --shell /bin/bash "$APP_USER"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# Chromium needs more memory than a 1-2 GB server has spare.
if ! swapon --show | grep -q .; then
  say "Adding 2 GB of swap"
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

say "Firewall: SSH only (the dashboard is reached through an SSH tunnel)"
ufw allow OpenSSH
ufw --force enable

say "Installing Harvey into $VENV"
as_app "python3 -m venv .venv && .venv/bin/pip install --upgrade pip && .venv/bin/pip install -e ."

say "Installing Chromium for LinkedIn prospecting"
"$VENV/bin/python" -m playwright install-deps chromium
as_app ".venv/bin/python -m playwright install chromium"

say "Installing Claude Code for the '$APP_USER' user"
if [[ ! -x "$USER_HOME/.local/bin/claude" ]]; then
  as_app "curl -fsSL https://claude.ai/install.sh | bash"
fi

say "Preparing .env (staging keys only)"
if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
fi
# DataForSEO answers from its free sandbox, so discovery tests cost nothing.
grep -q '^DATAFORSEO_SANDBOX=.\+' "$APP_DIR/.env" || echo 'DATAFORSEO_SANDBOX=1' >> "$APP_DIR/.env"
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

say "Staging safety: approval on, sending paused"
as_app ".venv/bin/python -c 'from harvey.config import set_require_approval; set_require_approval(True)'" \
  || echo "   Couldn't set require_approval. Check channels.email.require_approval: true by hand."
as_app ".venv/bin/harvey sending pause" \
  || echo "   Couldn't pause sending. Run 'harvey sending pause' after setup."

say "Creating services"
SERVICE_PATH="$VENV/bin:$USER_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

cat > /etc/systemd/system/harvey-dashboard.service <<EOF
[Unit]
Description=Harvey dashboard (127.0.0.1:5555, reach it through an SSH tunnel)
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=PATH=$SERVICE_PATH
ExecStart=$VENV/bin/harvey dashboard --host 127.0.0.1 --port 5555
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/harvey-run.service <<EOF
[Unit]
Description=Harvey heartbeat loop
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=PATH=$SERVICE_PATH
ExecStart=$VENV/bin/harvey run
Restart=on-failure
RestartSec=30
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now harvey-dashboard
# The loop needs Claude signed in and Harvey trained first, so it's enabled
# for later boots but not started now.
systemctl enable harvey-run

say "Done. Three things left, as the '$APP_USER' user:"
cat <<EOF

  sudo -iu $APP_USER
  cd $APP_DIR
  claude                                  # sign in to Claude, then /exit
  .venv/bin/harvey train https://your-website.com
  exit

  sudo systemctl start harvey-run         # start the 15-minute loop

Open the dashboard from your own computer:
  ssh -L 5555:127.0.0.1:5555 root@THIS-SERVER-IP
  then browse to http://127.0.0.1:5555/

EOF
