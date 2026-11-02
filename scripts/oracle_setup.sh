#!/usr/bin/env bash
# Sets the bot up on an Oracle Cloud Always Free Ampere A1 instance (Ubuntu 22.04 / 24.04, ARM).
#
#   1. Copy the project to the server (without .venv and data/):   see README "Oracle Cloud"
#   2. bash scripts/oracle_setup.sh
#   3. Copy .env, channels.json and data/telegram.session, then:   sudo systemctl start tradebot
#
# Runs as the normal user (ubuntu); asks for sudo where needed. Safe to run again.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

echo "== time zone: India (day / week / month stops count in IST)"
sudo timedatectl set-timezone Asia/Kolkata

if [ "$(free -m | awk '/^Mem:/ {print $2}')" -lt 3000 ] && [ ! -f /swapfile ]; then
  echo "== 2 GB swap (small server: Chromium + the bot fit in 2 GB RAM with it)"
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

echo "== system packages"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip

echo "== Python packages"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "== browser: Playwright's Chromium (Google Chrome has no ARM Linux build)"
.venv/bin/python -m playwright install --with-deps chromium

if [ -d /etc/apparmor.d ]; then
  echo "== AppArmor: let Playwright's Chromium use its sandbox (Ubuntu 23.10+ blocks it otherwise)"
  sudo tee /etc/apparmor.d/playwright-chromium >/dev/null <<'EOF'
abi <abi/4.0>,
include <tunables/global>

profile playwright-chromium /home/*/.cache/ms-playwright/**/chrome* flags=(unconfined) {
  userns,
  include if exists <local/playwright-chromium>
}
EOF
  sudo apparmor_parser -r /etc/apparmor.d/playwright-chromium
fi

echo "== service: starts at boot, restarts 30 s after any stop"
sed -e "s|__ROOT__|$ROOT|g" -e "s|__USER__|$(whoami)|g" scripts/tradebot.service | sudo tee /etc/systemd/system/tradebot.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable tradebot

mkdir -p data
cat <<EOF

Done. Before starting:
  - .env from your PC, with  BROWSER_CHANNEL=chromium  HEADLESS=true  DRY_RUN=true
  - channels.json from your PC
  - data/telegram.session from your PC (STOP the bot on the PC first: one session in one place only)
The site login is done again here automatically (the PC's Chrome profile doesn't work on Linux);
if the site asks for an OTP from this new IP, that shows in the log.

  sudo systemctl start tradebot      # start
  journalctl -u tradebot -f          # watch the log (Ctrl+C to leave)
  sudo systemctl stop tradebot       # stop
EOF
