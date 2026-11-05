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

is_termux() {
    if [ -n "${TERMUX_VERSION:-}" ] || [ -d "/data/data/com.termux" ] || [ "${PREFIX:-}" = "/data/data/com.termux/files/usr" ] || [[ "${PATH:-}" == *"/com.termux/"* ]]; then
        return 0
    fi
    return 1
}

run_cmd() {
    if is_termux || [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo &>/dev/null; then
        sudo "$@"
    elif command -v doas &>/dev/null; then
        doas "$@"
    else
        "$@"
    fi
}

install_host_packages() {
    step_info "Detecting system package manager (scanning 60+ package managers)..."

    if is_termux; then
        step_dim "Termux environment detected. Using pkg manager..."
        pkg update -y >/dev/null 2>&1 || true
        pkg install -y git curl ffmpeg python python-pip nodejs-lts clang make libffi openssl libcrypt >/dev/null 2>&1 || \
            pkg install -y git curl ffmpeg python nodejs clang make || true
        step_done "Termux packages installed."
        return 0
    fi

    local PM_FOUND=false

    if command -v apt-get &>/dev/null; then
        step_dim "[1/61] Installing packages via apt-get..."
        export DEBIAN_FRONTEND=noninteractive
        run_cmd apt-get update -y >/dev/null 2>&1 || true
        run_cmd apt-get install -y --no-install-recommends git curl ffmpeg ca-certificates build-essential python3 python3-venv python3-pip nodejs npm >/dev/null 2>&1 || \
            run_cmd apt-get install -y git curl ffmpeg python3 python3-pip nodejs npm || true
        PM_FOUND=true
    elif command -v apt &>/dev/null; then
        step_dim "[2/61] Installing packages via apt..."
        export DEBIAN_FRONTEND=noninteractive
        run_cmd apt update -y >/dev/null 2>&1 || true
        run_cmd apt install -y --no-install-recommends git curl ffmpeg ca-certificates build-essential python3 python3-venv python3-pip nodejs npm >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v pacman &>/dev/null; then
        step_dim "[3/61] Installing packages via pacman..."
        run_cmd pacman -Sy --noconfirm git curl ffmpeg python python-pip nodejs npm base-devel >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v paru &>/dev/null; then
        step_dim "[4/61] Installing packages via paru..."
        paru -Sy --noconfirm git curl ffmpeg python python-pip nodejs npm base-devel >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v yay &>/dev/null; then
        step_dim "[5/61] Installing packages via yay..."
        yay -Sy --noconfirm git curl ffmpeg python python-pip nodejs npm base-devel >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v dnf5 &>/dev/null; then
        step_dim "[6/61] Installing packages via dnf5..."
        run_cmd dnf5 install -y git curl ffmpeg python3 python3-pip nodejs npm gcc gcc-c++ make >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v dnf &>/dev/null; then
        step_dim "[7/61] Installing packages via dnf..."
        run_cmd dnf install -y git curl ffmpeg python3 python3-pip nodejs npm gcc gcc-c++ make >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v microdnf &>/dev/null; then
        step_dim "[8/61] Installing packages via microdnf..."
        run_cmd microdnf install -y git curl ffmpeg python3 python3-pip nodejs npm gcc make >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v yum &>/dev/null; then
        step_dim "[9/61] Installing packages via yum..."
        run_cmd yum install -y git curl ffmpeg python3 python3-pip nodejs npm gcc gcc-c++ make >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v tdnf &>/dev/null; then
        step_dim "[10/61] Installing packages via tdnf..."
        run_cmd tdnf install -y git curl ffmpeg python3 python3-pip nodejs npm build-essential >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v rpm-ostree &>/dev/null; then
        step_dim "[11/61] Installing packages via rpm-ostree..."
        rpm-ostree install --idempotent -y git curl ffmpeg python3 nodejs npm gcc make >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v zypper &>/dev/null; then
        step_dim "[12/61] Installing packages via zypper..."
        run_cmd zypper --non-interactive install git curl ffmpeg python3 python3-pip nodejs npm gcc gcc-c++ make >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v apk &>/dev/null; then
        step_dim "[13/61] Installing packages via apk..."
        run_cmd apk update >/dev/null 2>&1 || true
        run_cmd apk add git curl ffmpeg ca-certificates build-base python3 py3-pip nodejs npm >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v xbps-install &>/dev/null; then
        step_dim "[14/61] Installing packages via xbps-install..."
        run_cmd xbps-install -Syu >/dev/null 2>&1 || true
        run_cmd xbps-install -y git curl ffmpeg python3 python3-pip nodejs base-devel >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v emerge &>/dev/null; then
        step_dim "[15/61] Installing packages via emerge..."
        run_cmd emerge --ask=n --noreplace dev-vcs/git net-misc/curl media-video/ffmpeg dev-lang/python net-libs/nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v eopkg &>/dev/null; then
        step_dim "[16/61] Installing packages via eopkg..."
        run_cmd eopkg install -y git curl ffmpeg python3 nodejs system.devel >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v swupd &>/dev/null; then
        step_dim "[17/61] Installing packages via swupd..."
        run_cmd swupd bundle-add git curl ffmpeg python-basic nodejs-basic c-basic >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v nix-env &>/dev/null; then
        step_dim "[18/61] Installing packages via nix-env..."
        nix-env -iA nixpkgs.git nixpkgs.curl nixpkgs.ffmpeg nixpkgs.python3 nixpkgs.nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v guix &>/dev/null; then
        step_dim "[19/61] Installing packages via guix..."
        guix install git curl ffmpeg python node >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v brew &>/dev/null; then
        step_dim "[20/61] Installing packages via Homebrew..."
        brew install git curl ffmpeg python node >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v port &>/dev/null; then
        step_dim "[21/61] Installing packages via MacPorts..."
        run_cmd port install git curl ffmpeg python312 nodejs20 >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v pkgin &>/dev/null; then
        step_dim "[22/61] Installing packages via pkgin..."
        run_cmd pkgin -y install git curl ffmpeg python311 nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v pkg_add &>/dev/null; then
        step_dim "[23/61] Installing packages via pkg_add..."
        run_cmd pkg_add -I git curl ffmpeg python3 node >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v slackpkg &>/dev/null; then
        step_dim "[24/61] Installing packages via slackpkg..."
        run_cmd slackpkg -default_answer=y -batch=on install git curl ffmpeg python3 nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v slapt-get &>/dev/null; then
        step_dim "[25/61] Installing packages via slapt-get..."
        run_cmd slapt-get --update >/dev/null 2>&1 || true
        run_cmd slapt-get --install -y git curl ffmpeg python3 nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v prt-get &>/dev/null; then
        step_dim "[26/61] Installing packages via prt-get..."
        run_cmd prt-get depinst git curl ffmpeg python3 nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v cards &>/dev/null; then
        step_dim "[27/61] Installing packages via cards..."
        run_cmd cards sync >/dev/null 2>&1 || true
        run_cmd cards install git curl ffmpeg python nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v kiss &>/dev/null; then
        step_dim "[28/61] Installing packages via kiss..."
        kiss b git curl ffmpeg python nodejs >/dev/null 2>&1 || true
        kiss i git curl ffmpeg python nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v scratchpkg &>/dev/null; then
        step_dim "[29/61] Installing packages via scratchpkg..."
        run_cmd scratchpkg sync >/dev/null 2>&1 || true
        run_cmd scratchpkg install git curl ffmpeg python3 nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v opkg &>/dev/null; then
        step_dim "[30/61] Installing packages via opkg..."
        run_cmd opkg update >/dev/null 2>&1 || true
        run_cmd opkg install git curl ffmpeg python3 python3-pip node >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v tce-load &>/dev/null; then
        step_dim "[31/61] Installing packages via tce-load..."
        tce-load -wi git curl ffmpeg python3.9 nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v equo &>/dev/null; then
        step_dim "[32/61] Installing packages via equo..."
        run_cmd equo update >/dev/null 2>&1 || true
        run_cmd equo install git curl ffmpeg python nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v cave &>/dev/null; then
        step_dim "[33/61] Installing packages via cave..."
        run_cmd cave resolve -x repository/git repository/curl repository/ffmpeg dev-lang/python net-libs/nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v pisi &>/dev/null; then
        step_dim "[34/61] Installing packages via pisi..."
        run_cmd pisi install -y git curl ffmpeg python3 nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v urpmi &>/dev/null; then
        step_dim "[35/61] Installing packages via urpmi..."
        run_cmd urpmi --auto git curl ffmpeg python3 nodejs task-c-devel >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v petget &>/dev/null; then
        step_dim "[36/61] Installing packages via petget..."
        petget git curl ffmpeg python3 nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v ppm &>/dev/null; then
        step_dim "[37/61] Installing packages via ppm..."
        ppm --install git,curl,ffmpeg,python3,nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v cast &>/dev/null; then
        step_dim "[38/61] Installing packages via cast..."
        run_cmd cast git curl ffmpeg python3 nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v sorcery &>/dev/null; then
        step_dim "[39/61] Installing packages via sorcery..."
        run_cmd sorcery cast git curl ffmpeg python3 nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v lin &>/dev/null; then
        step_dim "[40/61] Installing packages via lin..."
        run_cmd lin git curl ffmpeg python3 nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v lvu &>/dev/null; then
        step_dim "[41/61] Installing packages via lvu..."
        run_cmd lvu compile git curl ffmpeg python3 nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v conary &>/dev/null; then
        step_dim "[42/61] Installing packages via conary..."
        run_cmd conary update git curl ffmpeg python nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v alps &>/dev/null; then
        step_dim "[43/61] Installing packages via alps..."
        run_cmd alps install git curl ffmpeg python3 nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v pacstall &>/dev/null; then
        step_dim "[44/61] Installing packages via pacstall..."
        pacstall -I git curl ffmpeg >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v pixi &>/dev/null; then
        step_dim "[45/61] Installing packages via pixi..."
        pixi global install git curl ffmpeg python nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v pkgx &>/dev/null; then
        step_dim "[46/61] Installing packages via pkgx..."
        pkgx install git curl ffmpeg python node >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v aqua &>/dev/null; then
        step_dim "[47/61] Installing packages via aqua..."
        aqua install git curl ffmpeg uv >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v mamba &>/dev/null; then
        step_dim "[48/61] Installing packages via mamba..."
        mamba install -y git curl ffmpeg python nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v conda &>/dev/null; then
        step_dim "[49/61] Installing packages via conda..."
        conda install -y git curl ffmpeg python nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v spack &>/dev/null; then
        step_dim "[50/61] Installing packages via spack..."
        spack install git curl ffmpeg python node-js >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v snap &>/dev/null; then
        step_dim "[51/61] Installing packages via snap..."
        run_cmd snap install --classic git >/dev/null 2>&1 || true
        run_cmd snap install --classic node >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v flatpak &>/dev/null; then
        step_dim "[52/61] Installing packages via flatpak..."
        run_cmd flatpak install -y flathub org.freedesktop.Sdk//23.08 >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v winget.exe &>/dev/null || command -v winget &>/dev/null; then
        step_dim "[53/61] Installing packages via winget..."
        winget install --accept-source-agreements --accept-package-agreements Git.Git Python.Python.3.12 OpenJS.NodeJS Gyan.FFmpeg >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v scoop.cmd &>/dev/null || command -v scoop &>/dev/null; then
        step_dim "[54/61] Installing packages via scoop..."
        scoop install git curl ffmpeg python nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v choco.exe &>/dev/null || command -v choco &>/dev/null; then
        step_dim "[55/61] Installing packages via choco..."
        choco install -y git curl ffmpeg python nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v apt-cyg &>/dev/null; then
        step_dim "[56/61] Installing packages via apt-cyg..."
        apt-cyg install git curl ffmpeg python3 python3-devel nodejs >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v pkg &>/dev/null; then
        step_dim "[57/61] Installing packages via pkg..."
        run_cmd pkg update -f >/dev/null 2>&1 || true
        run_cmd pkg install -y git curl ffmpeg python3 nodejs npm >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v pip3 &>/dev/null || command -v pip &>/dev/null; then
        step_dim "[58/61] Installing Python base tools via pip..."
        local pip_bin="$(command -v pip3 || command -v pip)"
        "$pip_bin" install --upgrade --user pip setuptools wheel virtualenv >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v cargo &>/dev/null; then
        step_dim "[59/61] Installing uv via cargo..."
        cargo install uv >/dev/null 2>&1 || true
        PM_FOUND=true
    elif command -v pdm &>/dev/null; then
        step_dim "[60/61] PDM environment found..."
        PM_FOUND=true
    fi

    if [ "$PM_FOUND" = true ]; then
        step_done "Package manager processed successfully."
    else
        step_warn "No recognized package manager was found. Continuing with existing system binaries."
    fi
}

ensure_uv() {
    if command -v uv &>/dev/null; then
        return 0
    fi

    step_dim "Installing uv package manager..."

    if is_termux; then
        step_dim "Attempting Termux uv install..."
        pkg install -y uv >/dev/null 2>&1 || true
        if command -v uv &>/dev/null; then
            step_done "uv installed via Termux pkg."
            return 0
        fi
        pip install --upgrade uv >/dev/null 2>&1 || python3 -m pip install --upgrade uv >/dev/null 2>&1 || true
        if command -v uv &>/dev/null; then
            step_done "uv installed via Termux pip."
            return 0
        fi
    fi

    local TMP_UV
    TMP_UV=$(mktemp 2>/dev/null || echo "/tmp/uv_install.sh")
    if curl -LsSf https://astral.sh/uv/install.sh -o "$TMP_UV" 2>/dev/null; then
        sh "$TMP_UV" 2>/dev/null || true
        rm -f "$TMP_UV" 2>/dev/null || true
    fi

    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:${PREFIX:-}/bin:$PATH"

    if ! command -v uv &>/dev/null; then
        if command -v pip3 &>/dev/null || command -v pip &>/dev/null; then
            local pip_bin="$(command -v pip3 || command -v pip)"
            "$pip_bin" install --upgrade --user uv >/dev/null 2>&1 || "$pip_bin" install --upgrade uv >/dev/null 2>&1 || true
        elif command -v python3 &>/dev/null; then
            python3 -m pip install --upgrade --user uv >/dev/null 2>&1 || python3 -m pip install --upgrade uv >/dev/null 2>&1 || true
        fi
    fi

    if ! command -v uv &>/dev/null; then
        step_warn "uv binary not found in standard paths. Creating python shim..."
        mkdir -p "$HOME/.local/bin"
        cat << 'EOF' > "$HOME/.local/bin/uv"
#!/usr/bin/env bash
if [ "$1" = "run" ]; then
    shift
    exec python3 "$@"
elif [ "$1" = "sync" ]; then
    if [ ! -d ".venv" ]; then
        python3 -m venv .venv 2>/dev/null || true
    fi
    if [ -f ".venv/bin/pip" ]; then
        .venv/bin/pip install --upgrade pip >/dev/null 2>&1 || true
        .venv/bin/pip install -e . >/dev/null 2>&1 || true
    fi
    exit 0
else
    exec python3 "$@"
fi
EOF
        chmod +x "$HOME/.local/bin/uv"
        export PATH="$HOME/.local/bin:$PATH"
    fi

    if command -v uv &>/dev/null; then
        step_done "uv ready: $(command -v uv)"
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
    if is_termux; then
        echo -e "${C_GRAY}${S_BAR}     ${C_CYAN}1)${C_RESET} Standard Host (Termux) ${C_DIM}[Recommended]${C_RESET}"
        echo -e "${C_GRAY}${S_BAR}     ${C_CYAN}2)${C_RESET} Docker (Requires rooted Termux / proot)"
        echo -e "${C_GRAY}${S_BAR}     ${C_CYAN}3)${C_RESET} LXC (Proxmox VE / Linux Containers)"
    else
        echo -e "${C_GRAY}${S_BAR}     ${C_CYAN}1)${C_RESET} Standard / Daemon (Host OS) ${C_DIM}[Recommended]${C_RESET}"
        echo -e "${C_GRAY}${S_BAR}     ${C_CYAN}2)${C_RESET} Docker (Docker Compose container)"
        echo -e "${C_GRAY}${S_BAR}     ${C_CYAN}3)${C_RESET} LXC (Proxmox VE / Linux Containers)"
    fi
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
            uv sync || pip install -e . >/dev/null 2>&1 || true
            step_done "Dependencies synchronized."

            local uv_path
            uv_path="$(command -v uv || echo "$HOME/.local/bin/uv")"
            local detected_init=""
            local init_name=""
            local init_cmd=""
            local init_meta=""

            init_meta="$("$uv_path" run python -c "from hotaru.daemon import detect_init_system; i = detect_init_system(); print(f'{i.id}|{i.name}|{i.status_command()}')" 2>/dev/null || true)"
            if [ -n "$init_meta" ]; then
                IFS='|' read -r detected_init init_name init_cmd <<< "$init_meta"
            fi

            if [ -n "$detected_init" ]; then
                local install_service="y"
                ask_input "${C_GRAY}${S_BAR}  ${C_CYAN}◆${C_RESET}  Install background service via ${init_name} (${detected_init})? [Y/n]: " "y" install_service
                if [[ "$install_service" =~ ^[Yy]$ ]]; then
                    step_dim "Registering service with ${init_name}..."
                    local install_res=""
                    install_res="$("$uv_path" run python -c "from hotaru.daemon import install_daemon; res = install_daemon('${detected_init}'); print(res.message)" 2>/dev/null || true)"
                    if [ -n "$install_res" ]; then
                        step_done "$install_res"
                    else
                        step_done "Daemon installed. Status command: ${init_cmd}"
                    fi
                fi
            elif command -v systemctl &>/dev/null && [ "$(id -u)" -eq 0 ]; then
                local install_service="y"
                ask_input "${C_GRAY}${S_BAR}  ${C_CYAN}◆${C_RESET}  Install systemd service hotaru.service? [Y/n]: " "y" install_service
                if [[ "$install_service" =~ ^[Yy]$ ]]; then
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
            if [ -r /dev/tty ] && [ -w /dev/tty ]; then
                exec "$uv_path" run python -m hotaru </dev/tty
            else
                exec "$uv_path" run python -m hotaru
            fi
            ;;

        2)
            step_done "Environment: Docker Deployment"
            sync_repo

            if ! command -v docker &>/dev/null; then
                step_warn "Docker is not installed. Installing Docker..."
                TMP_DOCKER=$(mktemp)
                curl -fsSL https://get.docker.com -o "$TMP_DOCKER"
                sh "$TMP_DOCKER"
                rm -f "$TMP_DOCKER"
            fi

            step_info "Building Hotaru Docker image..."
            docker compose build
            step_done "Docker image built successfully."

            outro "Starting Hotaru interactive first-time login..."
            if [ -r /dev/tty ] && [ -w /dev/tty ]; then
                docker compose run --rm hotaru </dev/tty
            else
                docker compose run --rm hotaru
            fi

            echo -e "${C_GRAY}${S_START}  ${C_BOLD}Starting Hotaru Background Service${C_RESET}"
            docker compose up -d
            outro "Hotaru is now running in Docker! (Dashboard: http://localhost:8787)"
            ;;

        3)
            step_done "Environment: LXC Deployment"
            sync_repo

            if command -v pct &>/dev/null || command -v lxc-create &>/dev/null; then
                step_info "Starting LXC Container creator..."
                if [ -r /dev/tty ] && [ -w /dev/tty ]; then
                    exec bash lxc/create.sh </dev/tty
                else
                    exec bash lxc/create.sh
                fi
            else
                step_info "Provisioning current environment as LXC container..."
                if [ -r /dev/tty ] && [ -w /dev/tty ]; then
                    exec bash lxc/setup.sh </dev/tty
                else
                    exec bash lxc/setup.sh
                fi
            fi
            ;;

        *)
            step_error "Invalid selection: ${deploy_mode}"
            exit 1
            ;;
    esac
}

main "$@"
