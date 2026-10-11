#!/usr/bin/env bash
# Content Studio — self-hosting setup for an Ubuntu box (e.g. Latitude 5420, Mac mini).
# Idempotent: safe to re-run to update. Installs deps, builds the UI, installs a systemd service.
# Does NOT contain or create secrets: you put your .env at $APP_DIR/backend/.env yourself.
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/content-studio}"
REPO_BE="${REPO_BE:-https://github.com/KahaanKapasi/contentstudio-be.git}"
REPO_FE="${REPO_FE:-https://github.com/KahaanKapasi/studiodecontent.git}"
PORT="${PORT:-8000}"

echo "==> System packages"
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip git ffmpeg curl ca-certificates \
  libgl1 libglib2.0-0 fonts-dejavu-core

echo "==> Code ($APP_DIR)"
mkdir -p "$APP_DIR"
for pair in "backend|$REPO_BE" "frontend|$REPO_FE"; do
  dir="${pair%%|*}"; url="${pair#*|}"
  if [ -d "$APP_DIR/$dir/.git" ]; then git -C "$APP_DIR/$dir" pull --ff-only
  else git clone "$url" "$APP_DIR/$dir"; fi
done
# NOTE: the frontend repo is cloned into $APP_DIR/frontend; if the repos are private, run `gh auth login` first.

echo "==> Python environment"
cd "$APP_DIR/backend"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

if [ ! -f .env ]; then
  echo "!! $APP_DIR/backend/.env is missing. Copy your .env there (scp from your Mac), then re-run this script."
  exit 1
fi

echo "==> Frontend build (needs Node 20+; skipped if absent — the Vercel UI keeps working either way)"
if command -v node >/dev/null && [ "$(node -p 'process.versions.node.split(".")[0]')" -ge 20 ]; then
  cd "$APP_DIR/frontend"
  npm ci
  VITE_API_BASE_URL=same-origin npm run build
  grep -q '^FRONTEND_DIST=' "$APP_DIR/backend/.env" || echo "FRONTEND_DIST=$APP_DIR/frontend/dist" >> "$APP_DIR/backend/.env"
else
  echo "   Node 20+ not found. Install it (https://github.com/nvm-sh/nvm) and re-run, or keep using the Vercel UI."
fi

echo "==> systemd service"
sudo tee /etc/systemd/system/contentstudio.service >/dev/null <<UNIT
[Unit]
Description=Content Studio API
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$APP_DIR/backend
ExecStart=$APP_DIR/backend/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=3
# Studio renders are CPU-heavy; keep the API responsive.
Nice=5

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now contentstudio
sleep 3
curl -fsS "http://127.0.0.1:$PORT/api/health" && echo && echo "API is up on 127.0.0.1:$PORT"

cat <<'NEXT'

Next (once): make it reachable from your phone/laptop WITHOUT opening it to the internet:
  curl -fsSL https://tailscale.com/install.sh | sh
  sudo tailscale up                 # log in with the same account on your phone + Mac
  sudo tailscale serve --bg 8000    # HTTPS URL, visible only inside your tailnet
  tailscale serve status            # shows https://<machine>.<tailnet>.ts.net
Then open that URL on any device that has Tailscale installed. Logs: journalctl -u contentstudio -f
NEXT
