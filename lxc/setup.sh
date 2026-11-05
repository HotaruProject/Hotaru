#!/usr/bin/env bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
DIM='\033[2m'
NC='\033[0m'

echo -e "${CYAN}┌  Hotaru LXC Container Provisioning${NC}"
echo -e "${DIM}│  Preparing runtime dependencies...${NC}"

export DEBIAN_FRONTEND=noninteractive

apt-get update -y
apt-get install -y --no-install-recommends \
    curl \
    git \
    ffmpeg \
    ca-certificates \
    build-essential \
    python3 \
    python3-venv \
    python3-dev \
    nodejs \
    npm

if ! command -v uv &>/dev/null; then
    echo -e "${DIM}│  Installing uv package manager...${NC}"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

APP_DIR="/opt/hotaru"
if [ ! -d "$APP_DIR" ]; then
    echo -e "${DIM}│  Cloning Hotaru repository to ${APP_DIR}...${NC}"
    git clone https://github.com/HotaruProject/Hotaru.git "$APP_DIR"
fi

cd "$APP_DIR"

echo -e "${DIM}│  Synchronizing Python dependencies...${NC}"
uv sync

cat << 'EOF' > /etc/systemd/system/hotaru.service
[Unit]
Description=Hotaru Userbot
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/hotaru
ExecStart=/root/.local/bin/uv run python -m hotaru
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable hotaru.service

echo -e "${GREEN}◆  LXC environment provisioned successfully!${NC}"
echo -e "${CYAN}└  To start initial setup, run:${NC}"
echo -e "   cd /opt/hotaru && uv run python -m hotaru\n"
