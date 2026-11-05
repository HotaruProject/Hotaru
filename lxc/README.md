# Hotaru LXC Deployment

Run Hotaru in an isolated, lightweight Linux container (LXC) on Proxmox VE or standard Linux hosts.

## Quick Install on Proxmox VE

From your Proxmox host shell, run:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/HotaruProject/Hotaru/develop/lxc/create.sh)"
```

This will automatically:
1. Download or select a Debian 12 container template.
2. Create an unprivileged LXC container (`CT 888`, 1GB RAM, 2 vCPUs, 8GB disk, nesting enabled).
3. Install Python 3.13, uv, ffmpeg, node.js, and Hotaru.
4. Enable the `hotaru.service` systemd service.

To attach and complete the initial setup:

```bash
pct enter 888
cd /opt/hotaru && uv run python -m hotaru
```

## Running Inside an Existing LXC / VPS

If you already have a clean Debian/Ubuntu container or virtual machine:

```bash
curl -fsSL https://raw.githubusercontent.com/HotaruProject/Hotaru/develop/lxc/setup.sh | bash
```
