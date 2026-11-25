#!/usr/bin/env bash
# One-time setup for a fresh Oracle Cloud Always Free VM (Ubuntu 24.04, Ampere A1 or AMD micro).
# Run as the default "ubuntu" user:  bash deploy/setup-vm.sh
set -euo pipefail

# Docker + compose plugin
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"

# Oracle's Ubuntu images ship an iptables REJECT rule, so 80/443 stay closed even after the
# VCN security list allows them. Open them in the OS firewall and keep the rule across reboots.
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y iptables-persistent
sudo netfilter-persistent save

# 2 GB swap: the AMD micro shape only has 1 GB RAM.
if [ ! -f /swapfile ]; then
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

# Nightly DB backup at 02:30, kept for 14 days.
mkdir -p "$HOME/backups"
( crontab -l 2>/dev/null | grep -v pg_dump || true
  echo "30 2 * * * cd $HOME/manka-client-api && docker compose exec -T db pg_dump -U manka manka | gzip > $HOME/backups/manka-\$(date +\%F).sql.gz && find $HOME/backups -name '*.sql.gz' -mtime +14 -delete"
) | crontab -

echo "Done. Log out and back in (docker group), then: cp .env.example .env, fill it, docker compose up -d --build"
