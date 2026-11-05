#!/usr/bin/env bash
set -euo pipefail

C_RESET='\033[0m'
C_BOLD='\033[1m'
C_DIM='\033[2m'
C_RED='\033[31m'
C_GREEN='\033[32m'
C_YELLOW='\033[33m'
C_CYAN='\033[36m'
C_GRAY='\033[90m'

S_START="┌"
S_BAR="│"
S_END="└"
S_ACTIVE="◆"
S_SUBMIT="◇"

print_banner() {
    clear 2>/dev/null || true
    echo -e "${C_GRAY}"
    echo "  ████████╗██████╗ ████████╗ █████╗ ██████╗ ██╗   ██╗"
    echo "  ╚══██╔══╝██╔══██╗╚══██╔══╝██╔══██╗██╔══██╗██║   ██║"
    echo "     ██║   ██████╔╝   ██║   ███████║██████╔╝██║   ██║"
    echo "     ██║   ██╔══██╗   ██║   ██╔══██║██╔══██╗██║   ██║"
    echo "     ██║   ██║  ██║   ██║   ██║  ██║██║  ██║╚██████╔╝"
    echo "     ╚═╝   ╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝ "
    echo -e "                   ${C_DIM}Hotaru Userbot${C_RESET}\n"
}

intro() {
    echo -e "${C_GRAY}${S_START}  ${C_BOLD}${1}${C_RESET}"
}

step_info() {
    echo -e "${C_GRAY}${S_BAR}  ${C_CYAN}${S_ACTIVE}${C_RESET}  ${1}"
}

step_done() {
    echo -e "${C_GRAY}${S_BAR}  ${C_GREEN}${S_SUBMIT}${C_RESET}  ${1}"
}

step_warn() {
    echo -e "${C_GRAY}${S_BAR}  ${C_YELLOW}▲${C_RESET}  ${1}"
}

step_error() {
    echo -e "${C_GRAY}${S_BAR}  ${C_RED}■${C_RESET}  ${1}"
}

step_dim() {
    echo -e "${C_GRAY}${S_BAR}  ${C_DIM}${1}${C_RESET}"
}

outro() {
    echo -e "${C_GRAY}${S_BAR}"
    echo -e "${C_GRAY}${S_END}  ${C_GREEN}${1}${C_RESET}\n"
}

trap 'echo -e "\n${C_GRAY}${S_END}  ${C_RED}Operation cancelled.${C_RESET}\n"; exit 130' INT

print_banner
intro "Welcome to Hotaru Setup"

CURRENT_DIR="$(pwd)"
IS_HOTARU_REPO=false
if [ -f "$CURRENT_DIR/pyproject.toml" ] && grep -q 'name = "hotaru"' "$CURRENT_DIR/pyproject.toml" 2>/dev/null; then
    IS_HOTARU_REPO=true
fi

DEFAULT_INSTALL_PATH="$CURRENT_DIR"
if [ "$IS_HOTARU_REPO" = false ]; then
    DEFAULT_INSTALL_PATH="$HOME/Hotaru"
fi

step_info "Where would you like to install Hotaru?"
echo -ne "${C_GRAY}${S_BAR}     ${C_DIM}Path [${DEFAULT_INSTALL_PATH}]: ${C_RESET}"
read -r USER_PATH || true
INSTALL_DIR="${USER_PATH:-$DEFAULT_INSTALL_PATH}"
INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"
step_done "Target directory: ${C_CYAN}${INSTALL_DIR}${C_RESET}"

step_info "Select deployment environment:"
echo -e "${C_GRAY}${S_BAR}     ${C_CYAN}1)${C_RESET} Standard / Systemd (Host OS) ${C_DIM}[Recommended]${C_RESET}"
echo -e "${C_GRAY}${S_BAR}     ${C_CYAN}2)${C_RESET} Docker (Docker Compose container)"
echo -e "${C_GRAY}${S_BAR}     ${C_CYAN}3)${C_RESET} LXC (Proxmox VE / Linux Containers)"
echo -ne "${C_GRAY}${S_BAR}     ${C_DIM}Enter choice [1-3] (Default: 1): ${C_RESET}"
read -r DEPLOY_MODE || true
DEPLOY_MODE="${DEPLOY_MODE:-1}"

install_host_packages() {
    if command -v apt-get &>/dev/null; then
        step_dim "Updating apt repositories and installing packages..."
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -y >/dev/null 2>&1 || sudo apt-get update -y
        apt-get install -y --no-install-recommends git curl ffmpeg ca-certificates build-essential python3 python3-venv nodejs npm >/dev/null 2>&1 || \
            sudo apt-get install -y --no-install-recommends git curl ffmpeg ca-certificates build-essential python3 python3-venv nodejs npm
    elif command -v pacman &>/dev/null; then
        step_dim "Installing packages via pacman..."
        sudo pacman -Sy --noconfirm git curl ffmpeg python nodejs npm base-devel || true
    elif command -v dnf &>/dev/null; then
        step_dim "Installing packages via dnf..."
        sudo dnf install -y git curl ffmpeg python3 nodejs npm gcc gcc-c++ make || true
    fi
}

clone_if_needed() {
    if [ ! -d "$INSTALL_DIR" ]; then
        step_dim "Cloning Hotaru into ${INSTALL_DIR}..."
        git clone https://github.com/HotaruProject/Hotaru.git "$INSTALL_DIR"
    elif [ "$IS_HOTARU_REPO" = false ] && [ ! -f "$INSTALL_DIR/pyproject.toml" ]; then
        step_dim "Cloning Hotaru into ${INSTALL_DIR}..."
        git clone https://github.com/HotaruProject/Hotaru.git "$INSTALL_DIR"
    fi
    cd "$INSTALL_DIR"
}

ensure_uv() {
    if ! command -v uv &>/dev/null; then
        step_dim "Installing uv package manager..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    fi
}

case "$DEPLOY_MODE" in
    1)
        step_done "Environment: Standard Host Deployment"
        install_host_packages
        ensure_uv
        clone_if_needed
        
        step_info "Synchronizing dependencies..."
        uv sync
        step_done "Dependencies synchronized."

        if command -v systemctl &>/dev/null && [ "$(id -u)" -eq 0 ]; then
            echo -ne "${C_GRAY}${S_BAR}  ${C_CYAN}◆${C_RESET}  Install systemd service hotaru.service? [Y/n]: "
            read -r INSTALL_SERVICE || true
            INSTALL_SERVICE="${INSTALL_SERVICE:-y}"
            if [[ "$INSTALL_SERVICE" =~ ^[Yy]$ ]]; then
                UV_PATH="$(command -v uv || echo "$HOME/.local/bin/uv")"
                cat << EOF > /etc/systemd/system/hotaru.service
[Unit]
Description=Hotaru Userbot
After=network.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=${UV_PATH} run python -m hotaru
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
                systemctl daemon-reload
                systemctl enable hotaru.service >/dev/null 2>&1 || true
                step_done "Systemd service hotaru.service created and enabled."
            fi
        fi

        outro "Launching Hotaru setup..."
        exec uv run python -m hotaru
        ;;

    2)
        step_done "Environment: Docker Deployment"
        clone_if_needed

        if ! command -v docker &>/dev/null; then
            step_warn "Docker is not installed. Installing Docker..."
            curl -fsSL https://get.docker.com | sh
        fi

        step_info "Building Hotaru Docker image..."
        docker compose build
        step_done "Docker image built successfully."

        outro "Starting Hotaru interactive first-time login..."
        docker compose run --rm hotaru
        
        echo -e "${C_GRAY}${S_START}  ${C_BOLD}Starting Hotaru Background Service${C_RESET}"
        docker compose up -d
        outro "Hotaru is now running in Docker! (Dashboard: http://localhost:8787)"
        ;;

    3)
        step_done "Environment: LXC Deployment"
        clone_if_needed

        if command -v pct &>/dev/null || command -v lxc-create &>/dev/null; then
            step_info "Starting LXC Container creator..."
            exec bash lxc/create.sh
        else
            step_info "Provisioning current environment as LXC container..."
            exec bash lxc/setup.sh
        fi
        ;;

    *)
        step_error "Invalid selection: ${DEPLOY_MODE}"
        exit 1
        ;;
esac
