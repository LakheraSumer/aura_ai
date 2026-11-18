#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu server (Oracle Cloud Always Free, ARM or x86), then start Apna AI.
#   bash scripts/server_setup.sh
# Run it from the apna-ai folder after copying services/api/.env onto the server. Safe to run again:
# it rebuilds and restarts without touching the database.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "Installing Docker from Ubuntu's own packages..."
  sudo apt-get update -y
  sudo apt-get install -y docker.io docker-compose-v2
  sudo systemctl enable --now docker   # start on every boot
fi

if [ ! -f services/api/.env ]; then
  echo "Missing services/api/.env (your keys). Copy it from your PC first, then run this again." >&2
  exit 1
fi
chmod 600 services/api/.env

# Database password: generated once on this server, never in git.
if [ ! -f infrastructure/server/.env ]; then
  umask 077
  printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 24)" > infrastructure/server/.env
fi

# Small servers: a swap file keeps the image build from running out of memory.
if [ "$(swapon --show | wc -l)" -eq 0 ] && [ ! -f /swapfile ]; then
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

cd infrastructure/server
sudo docker compose up -d --build
sleep 15
sudo docker compose ps
echo
echo "Logs:     cd infrastructure/server && sudo docker compose logs -f telegram worker"
echo "Restart:  sudo docker compose restart      Stop: sudo docker compose down (data is kept)"
