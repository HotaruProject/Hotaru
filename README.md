<div align="center">
  <img src="https://raw.githubusercontent.com/HotaruProject/Hotaru/develop/hotaru/assets/banner.png" alt="Hotaru banner" width="100%">
  <br /><br />

[![Python][python-badge]][python-url]
[![GoyGram][goygram-badge]][goygram-url]
[![License][license-badge]][license-url]
<br />
[![Sandbox][sandbox-badge]][sandbox-url]
[![Website][website-badge]][website-url]
[![Telegram][telegram-badge]][telegram-url]
[![Stars][stars-badge]][stars-url]

  <p>A Telegram userbot built on GoyGram with Linux namespace sandboxing.</p>
</div>

> [!TIP]
> Visit the interactive 3D web portal and developer documentation at **[hotaruproject.github.io](https://hotaruproject.github.io/)**.

> [!NOTE]
> Hotaru isolates third-party code at the OS level. Each module runs in its own Linux mount and network namespace, so untrusted scripts cannot read your session files, encryption keys, or system directories.

## Security and architecture

### Linux namespace isolation

Third-party modules (`.hmod`) execute inside dedicated Linux namespaces (`CLONE_NEWNS`, `CLONE_NEWIPC`, `CLONE_NEWUTS`, `CLONE_NEWNET`). Worker processes run in private tmpfs mounts, drop privileges to user `nobody` (`uid: 65534`), and communicate strictly over UNIX pipes. An installed module cannot access files outside its directory or read credentials in memory.

### Explicit capability permissions

Modules declare required capabilities in their metadata:
- `mt`: Telegram API requests via GoyGram
- `state`: module-scoped key-value storage
- `net`: outbound network traffic

If a module does not declare the `net` capability, the kernel network namespace blocks all outbound sockets for that process.

### GoyGram async core

Hotaru uses GoyGram for MTProto transport, combining an asynchronous Python interface with native Rust cryptography for message processing and session handling.

### Web dashboard and Telegram Mini App

The embedded Singularity interface runs on Node.js and Vite. It exposes system metrics, logs, and module management locally on port 8787. When enabled, Hotaru opens a Cloudflare Horizon tunnel and registers with BotFather so you can open the control panel as a native Mini App in Telegram.

### Encrypted master vault

Session strings, database entries, and authorization keys are stored on disk encrypted with AES-GCM in `sanctuary/hotaru.vault`. Automatic rotation creates backup archives for state recovery.

### FloodWait guard

The `shield` module tracks Telegram API call rates in a sliding sample window. When request volume approaches Telegram limits, it pauses calls locally to prevent account bans and temporary floodwait restrictions.

### Multi-account supervisor

A single Hotaru instance can run multiple accounts concurrently. The supervisor handles separate MTProto sessions, routes commands to the active profile, and keeps state isolated between accounts.

## Requirements

- Linux (x86_64 or aarch64)
- Python 3.13 or newer
- Telegram API ID and API Hash from [my.telegram.org](https://my.telegram.org)
- Bot token from [@BotFather](https://t.me/BotFather) for inline UI and Mini App access
- `uv` package manager (installed automatically by the setup script)

## Installation

Run the interactive installer:

```bash
curl -fsSL https://raw.githubusercontent.com/HotaruProject/Hotaru/main/install.sh | bash
```

> [!TIP]
> The setup script installs system packages, prompts for your Telegram credentials, and sets up your preferred deployment: direct host systemd service, Docker Compose, or Proxmox LXC.

<details>
<summary><b>Docker Compose</b></summary>

Hotaru includes Compose manifests with namespace sandbox support:

```bash
git clone -b develop https://github.com/HotaruProject/Hotaru.git
cd Hotaru
docker compose run --rm hotaru
docker compose up -d
```

</details>

<details>
<summary><b>Proxmox VE and LXC</b></summary>

Run the LXC setup script to create an isolated container with unconfined nesting:

```bash
git clone -b develop https://github.com/HotaruProject/Hotaru.git
cd Hotaru
bash lxc/create.sh
```

</details>

<details>
<summary><b>Manual setup (uv)</b></summary>

Clone the repository and start the onboarding wizard:

```bash
git clone -b develop https://github.com/HotaruProject/Hotaru.git
cd Hotaru
uv sync
uv run python -m hotaru
```

</details>

> [!WARNING]
> Never share your `sanctuary/` folder or `hotaru.vault` key. They contain your active Telegram sessions and master encryption secrets.

## Commands

Send these from your account in any Telegram chat:

| Command | Action |
| --- | --- |
| `.help` | Open the interactive module catalog and help cards |
| `.ping` | Check MTProto gateway latency and connection health |
| `.dash` | Generate the Singularity panel link and launch the Telegram Mini App |
| `.addaccount` | Add another Telegram account to the supervisor pool |
| `.shield` | View API call rates and adjust FloodWait protection |
| `.config` | Open the interactive runtime settings editor |
| `.backup` | Create an encrypted backup archive of state and sessions |
| `.restart` | Reload kernel processes and active modules |

## License

This project is licensed under the GNU Affero General Public License v3.0 (AGPL-3.0). See [LICENSE](LICENSE) for details.

[python-badge]: https://img.shields.io/badge/Python-3.13+-3776AB.svg?style=for-the-badge&logo=python&logoColor=white
[python-url]: https://www.python.org/
[goygram-badge]: https://img.shields.io/badge/GoyGram-0.8.28-F58220.svg?style=for-the-badge&logo=telegram&logoColor=white
[goygram-url]: https://pypi.org/project/goygram/
[license-badge]: https://img.shields.io/badge/License-AGPL_3.0-blue.svg?style=for-the-badge
[license-url]: LICENSE
[sandbox-badge]: https://img.shields.io/badge/Security-Namespaces_Sandbox-000000.svg?style=for-the-badge&logo=linux&logoColor=white
[sandbox-url]: #linux-namespace-isolation
[website-badge]: https://img.shields.io/badge/Website-HotaruWeb-000000.svg?style=for-the-badge&logo=githubpages&logoColor=white
[website-url]: https://hotaruproject.github.io/
[telegram-badge]: https://img.shields.io/badge/Telegram-Community-2CA5E0.svg?style=for-the-badge&logo=telegram&logoColor=white
[telegram-url]: https://t.me/hotaru_ub
[docker-badge]: https://img.shields.io/badge/Docker-Ready-2496ED.svg?style=for-the-badge&logo=docker&logoColor=white
[docker-url]: #installation
[stars-badge]: https://img.shields.io/github/stars/HotaruProject/Hotaru?style=for-the-badge&logo=github&color=181717
[stars-url]: https://github.com/HotaruProject/Hotaru/stargazers
