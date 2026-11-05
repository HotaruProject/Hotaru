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
    echo "  ██╗  ██╗ ██████╗ ████████╗ █████╗ ██████╗ ██╗   ██╗"
    echo "  ██║  ██║██╔═══██╗╚══██╔══╝██╔══██╗██╔══██╗██║   ██║"
    echo "  ███████║██║   ██║   ██║   ███████║██████╔╝██║   ██║"
    echo "  ██╔══██║██║   ██║   ██║   ██╔══██║██╔══██╗██║   ██║"
    echo "  ██║  ██║╚██████╔╝   ██║   ██║  ██║██║  ██║╚██████╔╝"
    echo "  ╚═╝  ╚═╝ ╚═════╝    ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝ "
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

ask_input() {
    local prompt="$1"
    local default_val="$2"
    local var_name="$3"
    local val=""

    echo -ne "$prompt"
    if [ -t 0 ]; then
        read -r val || true
    elif [ -r /dev/tty ] && [ -w /dev/tty ]; then
        read -r val </dev/tty || true
    else
        val=""
    fi
    val="${val:-$default_val}"
    printf -v "$var_name" '%s' "$val"
}

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

ensure_uv() {
    if ! command -v uv &>/dev/null; then
        step_dim "Installing uv package manager..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    fi
}

main() {
    trap 'echo -e "\n${C_GRAY}${S_END}  ${C_RED}Operation cancelled.${C_RESET}\n"; exit 130' INT

    print_banner
    intro "Welcome to Hotaru Setup"

    local current_dir="$(pwd)"
    local is_hotaru_repo=false
    if [ -f "$current_dir/pyproject.toml" ] && grep -q 'name = "hotaru"' "$current_dir/pyproject.toml" 2>/dev/null; then
        is_hotaru_repo=true
    fi

    local default_install_path="$current_dir"
    if [ "$is_hotaru_repo" = false ]; then
        default_install_path="$HOME/Hotaru"
    fi

    local user_path=""
    step_info "Where would you like to install Hotaru?"
    ask_input "${C_GRAY}${S_BAR}     ${C_DIM}Path [${default_install_path}]: ${C_RESET}" "$default_install_path" user_path
    local install_dir="${user_path:-$default_install_path}"
    install_dir="${install_dir/#\~/$HOME}"
    step_done "Target directory: ${C_CYAN}${install_dir}${C_RESET}"

    local deploy_mode="1"
    step_info "Select deployment environment:"
    echo -e "${C_GRAY}${S_BAR}     ${C_CYAN}1)${C_RESET} Standard / Systemd (Host OS) ${C_DIM}[Recommended]${C_RESET}"
    echo -e "${C_GRAY}${S_BAR}     ${C_CYAN}2)${C_RESET} Docker (Docker Compose container)"
    echo -e "${C_GRAY}${S_BAR}     ${C_CYAN}3)${C_RESET} LXC (Proxmox VE / Linux Containers)"
    ask_input "${C_GRAY}${S_BAR}     ${C_DIM}Enter choice [1-3] (Default: 1): ${C_RESET}" "1" deploy_mode
    deploy_mode="${deploy_mode:-1}"

    sync_repo() {
        local target_branch="develop"
        if [ ! -d "$install_dir" ]; then
            step_dim "Cloning Hotaru into ${install_dir}..."
            mkdir -p "$(dirname "$install_dir")"
            git clone -b "$target_branch" https://github.com/HotaruProject/Hotaru.git "$install_dir"
        elif [ -d "$install_dir/.git" ]; then
            step_dim "Updating repository in ${install_dir}..."
            (
                cd "$install_dir"
                git fetch origin "$target_branch" >/dev/null 2>&1 || true
                git checkout "$target_branch" >/dev/null 2>&1 || true
                git pull origin "$target_branch" >/dev/null 2>&1 || true
            )
        elif [ "$is_hotaru_repo" = false ] && [ ! -f "$install_dir/pyproject.toml" ]; then
            step_dim "Cloning Hotaru into ${install_dir}..."
            git clone -b "$target_branch" https://github.com/HotaruProject/Hotaru.git "$install_dir"
        fi

        if [ ! -f "$install_dir/docker-compose.yml" ]; then
            if [ -f "$current_dir/docker-compose.yml" ]; then
                cp "$current_dir/docker-compose.yml" "$install_dir/"
            else
                curl -fsSL https://raw.githubusercontent.com/HotaruProject/Hotaru/develop/docker-compose.yml -o "$install_dir/docker-compose.yml" 2>/dev/null || true
            fi
        fi
        if [ ! -f "$install_dir/Dockerfile" ]; then
            if [ -f "$current_dir/Dockerfile" ]; then
                cp "$current_dir/Dockerfile" "$install_dir/"
            else
                curl -fsSL https://raw.githubusercontent.com/HotaruProject/Hotaru/develop/Dockerfile -o "$install_dir/Dockerfile" 2>/dev/null || true
            fi
        fi
        if [ ! -f "$install_dir/.dockerignore" ]; then
            if [ -f "$current_dir/.dockerignore" ]; then
                cp "$current_dir/.dockerignore" "$install_dir/"
            fi
        fi

        cd "$install_dir"
    }

    case "$deploy_mode" in
        1)
            step_done "Environment: Standard Host Deployment"
            install_host_packages
            ensure_uv
            sync_repo

            step_info "Synchronizing dependencies..."
            uv sync
            step_done "Dependencies synchronized."

            if command -v systemctl &>/dev/null && [ "$(id -u)" -eq 0 ]; then
                local install_service="y"
                ask_input "${C_GRAY}${S_BAR}  ${C_CYAN}◆${C_RESET}  Install systemd service hotaru.service? [Y/n]: " "y" install_service
                if [[ "$install_service" =~ ^[Yy]$ ]]; then
                    local uv_path="$(command -v uv || echo "$HOME/.local/bin/uv")"
                    cat << EOF > /etc/systemd/system/hotaru.service
[Unit]
Description=Hotaru Userbot
After=network.target

[Service]
Type=simple
WorkingDirectory=${install_dir}
ExecStart=${uv_path} run python -m hotaru
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
            sync_repo

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
            sync_repo

            if command -v pct &>/dev/null || command -v lxc-create &>/dev/null; then
                step_info "Starting LXC Container creator..."
                exec bash lxc/create.sh
            else
                step_info "Provisioning current environment as LXC container..."
                exec bash lxc/setup.sh
            fi
            ;;

        *)
            step_error "Invalid selection: ${deploy_mode}"
            exit 1
            ;;
    esac
}

main "$@"
