from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

UNIT_NAME = "hotaru.service"
SERVICE_NAME = "hotaru"
ROOT_UNIT_DIRECTORY = Path("/etc/systemd/system")
USER_UNIT_DIRECTORY = Path.home() / ".config" / "systemd" / "user"


class DaemonError(RuntimeError):
    pass


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def executable() -> str:
    candidate = repo_root() / ".venv" / "bin" / "python"
    if candidate.exists():
        return str(candidate)
    return sys.executable


def unit_directory() -> Path:
    return ROOT_UNIT_DIRECTORY if is_root() else USER_UNIT_DIRECTORY


def unit_path() -> Path:
    return unit_directory() / UNIT_NAME


def unit_content() -> str:
    target = "multi-user.target" if is_root() else "default.target"
    return (
        "[Unit]\n"
        "Description=Hotaru\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        f"WorkingDirectory={repo_root()}\n"
        f"ExecStart={executable()} -m hotaru\n"
        "Restart=always\n"
        "RestartSec=10\n"
        "\n"
        "[Install]\n"
        f"WantedBy={target}\n"
    )


def systemctl_command() -> list[str]:
    return ["systemctl"] if is_root() else ["systemctl", "--user"]


def user_environment() -> dict[str, str]:
    env = dict(os.environ)
    if is_root():
        return env
    try:
        uid = os.getuid()
    except AttributeError:
        return env
    runtime_dir = f"/run/user/{uid}"
    if not env.get("XDG_RUNTIME_DIR") and Path(runtime_dir).is_dir():
        env["XDG_RUNTIME_DIR"] = runtime_dir
    if not env.get("DBUS_SESSION_BUS_ADDRESS") and Path(runtime_dir, "bus").exists():
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime_dir}/bus"
    return env


def _run(command: list[str], timeout: float = 60.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=user_environment(),
        )
    except FileNotFoundError as exc:
        raise DaemonError(f"{command[0]} is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise DaemonError(f"{command[0]} timed out") from exc
    detail = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, detail.strip()


def enable_linger() -> bool:
    if is_root():
        return False
    if shutil.which("loginctl") is None:
        return False
    user = os.environ.get("USER") or os.environ.get("LOGNAME")
    if not user:
        return False
    code, _ = _run(["loginctl", "enable-linger", user])
    return code == 0


def install_daemon(start: bool = True) -> Path:
    if shutil.which("systemctl") is None:
        raise DaemonError("systemd is not available on this system")
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_content(), encoding="utf-8")
    if not is_root():
        enable_linger()
    base = systemctl_command()
    code, detail = _run(base + ["daemon-reload"])
    if code != 0:
        raise DaemonError(f"daemon-reload failed: {detail}")
    code, detail = _run(base + ["enable", SERVICE_NAME])
    if code != 0:
        raise DaemonError(f"enable failed: {detail}")
    if start:
        code, detail = _run(base + ["restart", SERVICE_NAME])
        if code != 0:
            raise DaemonError(f"restart failed: {detail}")
    return path


def daemon_status() -> str:
    base = systemctl_command()
    code, detail = _run(base + ["status", SERVICE_NAME])
    return detail if code == 0 else detail or "unknown"


def uninstall_daemon() -> bool:
    path = unit_path()
    base = systemctl_command()
    _run(base + ["disable", "--now", SERVICE_NAME])
    if path.exists():
        path.unlink()
    _run(base + ["daemon-reload"])
    return True
