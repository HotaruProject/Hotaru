#!/usr/bin/env bash
set -euo pipefail

CYAN='\033[0;36m'
GREEN='\033[0;32m'
RED='\033[0;31m'
DIM='\033[2m'
NC='\033[0m'

echo -e "${CYAN}┌  Hotaru LXC Creator${NC}"

CT_ID="${HOTARU_CT_ID:-888}"
CT_NAME="${HOTARU_CT_NAME:-hotaru}"
MEMORY="${HOTARU_CT_RAM:-1024}"
CORES="${HOTARU_CT_CORES:-2}"
DISK="${HOTARU_CT_DISK:-8}"

if command -v pct &>/dev/null; then
    echo -e "${DIM}│  Detected Proxmox VE host.${NC}"
    echo -e "${CYAN}◆${NC} Container ID: ${CT_ID}"
    echo -e "${CYAN}◆${NC} Container Name: ${CT_NAME}"

    TEMPLATE=$(pveam list local 2>/dev/null | grep -E "debian-12|ubuntu-24.04" | tail -n 1 | awk '{print $1}')
    if [ -z "$TEMPLATE" ]; then
        echo -e "${DIM}│  Downloading Debian 12 standard template...${NC}"
        pveam update
        pveam download local debian-12-standard_12.7-1_amd64.tar.zst || true
        TEMPLATE="local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst"
    fi

    echo -e "${DIM}│  Creating LXC container ${CT_ID}...${NC}"
    pct create "$CT_ID" "$TEMPLATE" \
        --hostname "$CT_NAME" \
        --memory "$MEMORY" \
        --cores "$CORES" \
        --rootfs "local-lvm:${DISK}" \
        --features nesting=1 \
        --net0 name=eth0,bridge=vmbr0,ip=dhcp \
        --unprivileged 1 \
        --onboot 1

    echo -e "${DIM}│  Starting container ${CT_ID}...${NC}"
    pct start "$CT_ID"
    sleep 5

    echo -e "${DIM}│  Running provisioning inside container...${NC}"
    pct exec "$CT_ID" -- bash -c "$(curl -fsSL https://raw.githubusercontent.com/HotaruProject/Hotaru/develop/lxc/setup.sh)"

    echo -e "${GREEN}◆  LXC container ${CT_ID} successfully created and configured!${NC}"
    echo -e "${CYAN}└  Enter container: pct enter ${CT_ID}${NC}"
    exit 0
fi

if command -v lxc-create &>/dev/null; then
    echo -e "${DIM}│  Detected standard Linux LXC host.${NC}"
    if lxc-info -n "$CT_NAME" &>/dev/null; then
        echo -e "${RED}■  Container ${CT_NAME} already exists!${NC}"
        exit 1
    fi

    echo -e "${DIM}│  Creating Debian Bookworm container ${CT_NAME}...${NC}"
    lxc-create -t download -n "$CT_NAME" -- -d debian -r bookworm -a amd64

    echo -e "${DIM}│  Starting container ${CT_NAME}...${NC}"
    lxc-start -n "$CT_NAME" -d
    sleep 5

    echo -e "${DIM}│  Running provisioning inside container...${NC}"
    lxc-attach -n "$CT_NAME" -- bash -c "$(curl -fsSL https://raw.githubusercontent.com/HotaruProject/Hotaru/develop/lxc/setup.sh)"

    echo -e "${GREEN}◆  LXC container ${CT_NAME} successfully created!${NC}"
    echo -e "${CYAN}└  Attach to container: lxc-attach -n ${CT_NAME}${NC}"
    exit 0
fi

echo -e "${RED}■  Neither Proxmox (pct) nor standard LXC (lxc-create) was detected on this host.${NC}"
echo -e "${DIM}│  To provision a generic Linux system/container, run: bash lxc/setup.sh${NC}"
exit 1
