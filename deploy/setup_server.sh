#!/usr/bin/env bash
# Prepare a fresh Ubuntu 24.04 droplet to run the main + reverse bots.
# Run as root on the server:  bash setup_server.sh
set -euo pipefail

echo "==> system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv ufw git tzdata

echo "==> timezone -> Asia/Kolkata (matches the bots' IST session logic)"
timedatectl set-timezone Asia/Kolkata

echo "==> firewall"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'ssh'
# Dashboards are NOT opened to the internet. Reach them over an SSH tunnel:
#   ssh -L 8765:localhost:8765 -L 8767:localhost:8767 root@<ip>
# Exposing them publicly would put an unauthenticated view of your account
# (and a restart button) on a public IP.
ufw --force enable
ufw status verbose

echo "==> bot user (bots should not run as root)"
id -u bots &>/dev/null || useradd -m -s /bin/bash bots

echo "==> directories"
install -d -o bots -g bots /home/bots/main /home/bots/reverse

echo "==> python venv + deps"
sudo -u bots python3 -m venv /home/bots/venv
sudo -u bots /home/bots/venv/bin/pip install -q --upgrade pip
sudo -u bots /home/bots/venv/bin/pip install -q \
    rich "websockets>=12.0,<14.0" requests pandas numpy psutil

echo
echo "DONE. Next: copy the bot files up, then install the services."
echo "  /home/bots/main     <- solana-arbitrage-bot/trading"
echo "  /home/bots/reverse  <- binance-reverse-bot"
