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


def _is_pid1(name: str) -> bool:
    try:
        comm = Path("/proc/1/comm").read_text(encoding="utf-8").strip().lower()
        return name.lower() in comm
    except Exception:
        return False


def _current_user() -> str:
    return os.environ.get("USER") or os.environ.get("LOGNAME") or "hotaru"


class BaseInitSystem:
    id: str = "base"
    display_name: str = "Base Init"
    priority: int = 100

    def detect(self) -> bool:
        return False

    def scope_name(self) -> str:
        return "system" if is_root() else "user"

    def install(self, start: bool = True) -> Path:
        raise NotImplementedError

    def status(self) -> str:
        raise NotImplementedError

    def uninstall(self) -> bool:
        raise NotImplementedError

    def status_command_hint(self) -> str:
        return f"{self.id} status {SERVICE_NAME}"


class SystemdInit(BaseInitSystem):
    id = "systemd"
    display_name = "systemd"
    priority = 10

    def detect(self) -> bool:
        if shutil.which("systemctl") is None:
            return False
        return (
            Path("/run/systemd/system").is_dir()
            or Path("/sys/fs/cgroup/systemd").exists()
            or _is_pid1("systemd")
            or is_root()
        )

    def install(self, start: bool = True) -> Path:
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
                _run(base + ["reset-failed", SERVICE_NAME])
                code, detail = _run(base + ["restart", SERVICE_NAME])
                if code != 0:
                    clean_msg = detail.splitlines()[0] if detail else "service start failed"
                    raise DaemonError(f"daemon restart failed: {clean_msg}")
        return path

    def status(self) -> str:
        base = systemctl_command()
        code, detail = _run(base + ["status", SERVICE_NAME])
        return detail if code == 0 else detail or "unknown"

    def uninstall(self) -> bool:
        path = unit_path()
        base = systemctl_command()
        _run(base + ["disable", "--now", SERVICE_NAME])
        if path.exists():
            path.unlink()
        _run(base + ["daemon-reload"])
        return True

    def status_command_hint(self) -> str:
        return "systemctl status hotaru" if is_root() else "systemctl --user status hotaru"


class DinitInit(BaseInitSystem):
    id = "dinit"
    display_name = "dinit"
    priority = 15

    def detect(self) -> bool:
        return (
            _is_pid1("dinit")
            or shutil.which("dinitctl") is not None
            or Path("/etc/dinit.d").is_dir()
            or Path("/run/dinit").is_dir()
        )

    def _service_dir(self) -> Path:
        if is_root():
            return Path("/etc/dinit.d")
        return Path.home() / ".config" / "dinit.d"

    def _service_file(self) -> Path:
        return self._service_dir() / SERVICE_NAME

    def _content(self) -> str:
        return (
            "type = process\n"
            f"command = {executable()} -m hotaru\n"
            f"working-dir = {repo_root()}\n"
            "restart = yes\n"
            "smooth-recovery = yes\n"
        )

    def install(self, start: bool = True) -> Path:
        sdir = self._service_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        path = self._service_file()
        path.write_text(self._content(), encoding="utf-8")
        cmd = ["dinitctl"] if is_root() else ["dinitctl", "--user"]
        if shutil.which("dinitctl"):
            _run(cmd + ["enable", SERVICE_NAME])
            if start:
                _run(cmd + ["start", SERVICE_NAME])
        return path

    def status(self) -> str:
        cmd = ["dinitctl"] if is_root() else ["dinitctl", "--user"]
        code, detail = _run(cmd + ["status", SERVICE_NAME])
        return detail if code == 0 else detail or "unknown"

    def uninstall(self) -> bool:
        cmd = ["dinitctl"] if is_root() else ["dinitctl", "--user"]
        if shutil.which("dinitctl"):
            _run(cmd + ["stop", SERVICE_NAME])
            _run(cmd + ["disable", SERVICE_NAME])
        path = self._service_file()
        if path.exists():
            path.unlink()
        return True

    def status_command_hint(self) -> str:
        return "dinitctl status hotaru" if is_root() else "dinitctl --user status hotaru"


class OpenRCInit(BaseInitSystem):
    id = "openrc"
    display_name = "OpenRC"
    priority = 20

    def detect(self) -> bool:
        return (
            Path("/run/openrc").is_dir()
            or _is_pid1("openrc-init")
            or (shutil.which("rc-service") is not None and shutil.which("rc-update") is not None)
        )

    def _service_file(self) -> Path:
        return Path("/etc/init.d") / SERVICE_NAME

    def _content(self) -> str:
        return (
            "#!/sbin/openrc-run\n"
            "name=\"hotaru\"\n"
            "description=\"Hotaru Userbot\"\n"
            f"directory=\"{repo_root()}\"\n"
            f"command=\"{executable()}\"\n"
            "command_args=\"-m hotaru\"\n"
            "command_background=\"yes\"\n"
            "pidfile=\"/run/hotaru.pid\"\n"
            "\n"
            "depend() {\n"
            "    need net\n"
            "}\n"
        )

    def install(self, start: bool = True) -> Path:
        path = self._service_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._content(), encoding="utf-8")
        path.chmod(0o755)
        if shutil.which("rc-update"):
            _run(["rc-update", "add", SERVICE_NAME, "default"])
        if start and shutil.which("rc-service"):
            _run(["rc-service", SERVICE_NAME, "restart"])
        return path

    def status(self) -> str:
        code, detail = _run(["rc-service", SERVICE_NAME, "status"])
        return detail if code == 0 else detail or "unknown"

    def uninstall(self) -> bool:
        if shutil.which("rc-service"):
            _run(["rc-service", SERVICE_NAME, "stop"])
        if shutil.which("rc-update"):
            _run(["rc-update", "del", SERVICE_NAME, "default"])
        path = self._service_file()
        if path.exists():
            path.unlink()
        return True

    def status_command_hint(self) -> str:
        return "rc-service hotaru status"


class RunitInit(BaseInitSystem):
    id = "runit"
    display_name = "runit"
    priority = 25

    def detect(self) -> bool:
        return (
            _is_pid1("runit")
            or Path("/etc/runit").is_dir()
            or Path("/run/runit").is_dir()
            or shutil.which("runsvdir") is not None
        )

    def _sv_dir(self) -> Path:
        if is_root():
            return Path("/etc/sv") / SERVICE_NAME
        return Path.home() / ".service" / SERVICE_NAME

    def _link_dir(self) -> Path:
        if is_root():
            return Path("/var/service") / SERVICE_NAME
        return Path.home() / "service" / SERVICE_NAME

    def install(self, start: bool = True) -> Path:
        sv_dir = self._sv_dir()
        sv_dir.mkdir(parents=True, exist_ok=True)
        run_file = sv_dir / "run"
        content = (
            "#!/bin/sh\n"
            "exec 2>&1\n"
            f"cd \"{repo_root()}\"\n"
            f"exec \"{executable()}\" -m hotaru\n"
        )
        run_file.write_text(content, encoding="utf-8")
        run_file.chmod(0o755)
        link = self._link_dir()
        if not link.exists() and link.parent.exists():
            try:
                link.symlink_to(sv_dir)
            except OSError:
                pass
        if start and shutil.which("sv"):
            _run(["sv", "up", SERVICE_NAME])
        return run_file

    def status(self) -> str:
        code, detail = _run(["sv", "status", SERVICE_NAME])
        return detail if code == 0 else detail or "unknown"

    def uninstall(self) -> bool:
        if shutil.which("sv"):
            _run(["sv", "down", SERVICE_NAME])
        link = self._link_dir()
        if link.is_symlink():
            link.unlink()
        run_file = self._sv_dir() / "run"
        if run_file.exists():
            run_file.unlink()
        return True

    def status_command_hint(self) -> str:
        return "sv status hotaru"


class S6Init(BaseInitSystem):
    id = "s6"
    display_name = "s6"
    priority = 30

    def detect(self) -> bool:
        return (
            shutil.which("s6-svc") is not None
            or _is_pid1("s6-svscan")
            or Path("/run/s6").is_dir()
            or Path("/etc/s6").is_dir()
        )

    def _service_dir(self) -> Path:
        return Path("/etc/s6/services") / SERVICE_NAME

    def install(self, start: bool = True) -> Path:
        sdir = self._service_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        run_file = sdir / "run"
        content = (
            "#!/bin/sh\n"
            "exec 2>&1\n"
            f"cd \"{repo_root()}\"\n"
            f"exec \"{executable()}\" -m hotaru\n"
        )
        run_file.write_text(content, encoding="utf-8")
        run_file.chmod(0o755)
        if start and shutil.which("s6-svc"):
            _run(["s6-svc", "-u", str(sdir)])
        return run_file

    def status(self) -> str:
        sdir = self._service_dir()
        code, detail = _run(["s6-svstat", str(sdir)])
        return detail if code == 0 else detail or "unknown"

    def uninstall(self) -> bool:
        sdir = self._service_dir()
        if shutil.which("s6-svc"):
            _run(["s6-svc", "-d", str(sdir)])
        run_file = sdir / "run"
        if run_file.exists():
            run_file.unlink()
        return True

    def status_command_hint(self) -> str:
        return f"s6-svstat /etc/s6/services/{SERVICE_NAME}"


class S6_66Init(BaseInitSystem):
    id = "66"
    display_name = "66 (Obarun)"
    priority = 32

    def detect(self) -> bool:
        return shutil.which("66-enable") is not None or shutil.which("66-all") is not None

    def _service_file(self) -> Path:
        if is_root():
            return Path("/etc/66/service") / SERVICE_NAME
        return Path.home() / ".66" / "service" / SERVICE_NAME

    def install(self, start: bool = True) -> Path:
        path = self._service_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "[main]\n"
            "@type = classic\n"
            "@version = 0.1.0\n"
            "@description = \"Hotaru Userbot\"\n"
            f"@user = ( {_current_user()} )\n"
            "\n"
            "[start]\n"
            "@build = auto\n"
            f"@execute = ( {executable()} -m hotaru )\n"
        )
        path.write_text(content, encoding="utf-8")
        if shutil.which("66-enable"):
            _run(["66-enable", SERVICE_NAME])
        if start and shutil.which("66-start"):
            _run(["66-start", SERVICE_NAME])
        return path

    def status(self) -> str:
        code, detail = _run(["66-info", SERVICE_NAME])
        return detail if code == 0 else detail or "unknown"

    def uninstall(self) -> bool:
        if shutil.which("66-stop"):
            _run(["66-stop", SERVICE_NAME])
        if shutil.which("66-disable"):
            _run(["66-disable", SERVICE_NAME])
        path = self._service_file()
        if path.exists():
            path.unlink()
        return True

    def status_command_hint(self) -> str:
        return "66-info hotaru"


class LaunchdInit(BaseInitSystem):
    id = "launchd"
    display_name = "launchd (macOS)"
    priority = 5

    def detect(self) -> bool:
        return sys.platform == "darwin" or shutil.which("launchctl") is not None

    def _plist_path(self) -> Path:
        if is_root():
            return Path("/Library/LaunchDaemons/com.hotaru.userbot.plist")
        return Path.home() / "Library" / "LaunchAgents" / "com.hotaru.userbot.plist"

    def _content(self) -> str:
        return (
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
            "<plist version=\"1.0\">\n"
            "<dict>\n"
            "    <key>Label</key>\n"
            "    <string>com.hotaru.userbot</string>\n"
            "    <key>ProgramArguments</key>\n"
            "    <array>\n"
            f"        <string>{executable()}</string>\n"
            "        <string>-m</string>\n"
            "        <string>hotaru</string>\n"
            "    </array>\n"
            "    <key>WorkingDirectory</key>\n"
            f"    <string>{repo_root()}</string>\n"
            "    <key>KeepAlive</key>\n"
            "    <true/>\n"
            "    <key>RunAtLoad</key>\n"
            "    <true/>\n"
            "</dict>\n"
            "</plist>\n"
        )

    def install(self, start: bool = True) -> Path:
        path = self._plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._content(), encoding="utf-8")
        if shutil.which("launchctl"):
            _run(["launchctl", "unload", str(path)])
            _run(["launchctl", "load", "-w", str(path)])
        return path

    def status(self) -> str:
        _, detail = _run(["launchctl", "list"])
        lines = [line for line in detail.splitlines() if "com.hotaru.userbot" in line]
        return "\n".join(lines) if lines else "not loaded"

    def uninstall(self) -> bool:
        path = self._plist_path()
        if shutil.which("launchctl"):
            _run(["launchctl", "unload", "-w", str(path)])
        if path.exists():
            path.unlink()
        return True

    def status_command_hint(self) -> str:
        return "launchctl list | grep hotaru"


class TermuxServicesInit(BaseInitSystem):
    id = "termux"
    display_name = "Termux Services"
    priority = 2

    def detect(self) -> bool:
        prefix = os.environ.get("PREFIX", "")
        is_termux = bool(os.environ.get("TERMUX_VERSION")) or "/com.termux/" in prefix or Path("/data/data/com.termux").is_dir()
        return is_termux and (shutil.which("sv-enable") is not None or Path(prefix, "var/service").is_dir())

    def _service_dir(self) -> Path:
        prefix = Path(os.environ.get("PREFIX", "/data/data/com.termux/files/usr"))
        return prefix / "var" / "service" / SERVICE_NAME

    def install(self, start: bool = True) -> Path:
        sdir = self._service_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        run_file = sdir / "run"
        sh_bin = os.environ.get("PREFIX", "/data/data/com.termux/files/usr") + "/bin/sh"
        if not Path(sh_bin).exists():
            sh_bin = "/bin/sh"
        content = (
            f"#!{sh_bin}\n"
            "exec 2>&1\n"
            f"cd \"{repo_root()}\"\n"
            f"exec \"{executable()}\" -m hotaru\n"
        )
        run_file.write_text(content, encoding="utf-8")
        run_file.chmod(0o755)
        if shutil.which("sv-enable"):
            _run(["sv-enable", SERVICE_NAME])
        if start and shutil.which("sv"):
            _run(["sv", "up", SERVICE_NAME])
        return run_file

    def status(self) -> str:
        code, detail = _run(["sv", "status", SERVICE_NAME])
        return detail if code == 0 else detail or "unknown"

    def uninstall(self) -> bool:
        if shutil.which("sv-disable"):
            _run(["sv-disable", SERVICE_NAME])
        if shutil.which("sv"):
            _run(["sv", "down", SERVICE_NAME])
        run_file = self._service_dir() / "run"
        if run_file.exists():
            run_file.unlink()
        return True

    def status_command_hint(self) -> str:
        return "sv status hotaru"


class TermuxBootInit(BaseInitSystem):
    id = "termux_boot"
    display_name = "Termux:Boot"
    priority = 3

    def detect(self) -> bool:
        prefix = os.environ.get("PREFIX", "")
        is_termux = bool(os.environ.get("TERMUX_VERSION")) or "/com.termux/" in prefix or Path("/data/data/com.termux").is_dir()
        return is_termux and Path.home().joinpath(".termux").is_dir()

    def _boot_file(self) -> Path:
        return Path.home() / ".termux" / "boot" / "start-hotaru.sh"

    def install(self, start: bool = True) -> Path:
        bfile = self._boot_file()
        bfile.parent.mkdir(parents=True, exist_ok=True)
        sh_bin = os.environ.get("PREFIX", "/data/data/com.termux/files/usr") + "/bin/sh"
        if not Path(sh_bin).exists():
            sh_bin = "/bin/sh"
        content = (
            f"#!{sh_bin}\n"
            "termux-wake-lock 2>/dev/null || true\n"
            f"cd \"{repo_root()}\"\n"
            f"nohup \"{executable()}\" -m hotaru >/dev/null 2>&1 &\n"
        )
        bfile.write_text(content, encoding="utf-8")
        bfile.chmod(0o755)
        return bfile

    def status(self) -> str:
        code, detail = _run(["pgrep", "-f", "python -m hotaru"])
        return f"running (pid {detail})" if code == 0 and detail else "not running"

    def uninstall(self) -> bool:
        bfile = self._boot_file()
        if bfile.exists():
            bfile.unlink()
        return True

    def status_command_hint(self) -> str:
        return "pgrep -f 'python -m hotaru'"


class SysVInit(BaseInitSystem):
    id = "sysvinit"
    display_name = "SysVinit"
    priority = 40

    def detect(self) -> bool:
        return (
            Path("/etc/init.d").is_dir()
            and (shutil.which("update-rc.d") is not None or shutil.which("chkconfig") is not None)
            and is_root()
        )

    def _service_file(self) -> Path:
        return Path("/etc/init.d") / SERVICE_NAME

    def _content(self) -> str:
        return (
            "#!/bin/sh\n"
            "### BEGIN INIT INFO\n"
            "# Provides:          hotaru\n"
            "# Required-Start:    $remote_fs $syslog $network\n"
            "# Required-Stop:     $remote_fs $syslog $network\n"
            "# Default-Start:     2 3 4 5\n"
            "# Default-Stop:      0 1 6\n"
            "# Short-Description: Hotaru Userbot\n"
            "### END INIT INFO\n"
            f"DIR=\"{repo_root()}\"\n"
            f"DAEMON=\"{executable()}\"\n"
            "DAEMON_ARGS=\"-m hotaru\"\n"
            "PIDFILE=\"/run/hotaru.pid\"\n"
            "\n"
            "case \"$1\" in\n"
            "  start)\n"
            "    start-stop-daemon --start --background --make-pidfile --pidfile $PIDFILE --chdir $DIR --exec $DAEMON -- $DAEMON_ARGS\n"
            "    ;;\n"
            "  stop)\n"
            "    start-stop-daemon --stop --pidfile $PIDFILE\n"
            "    ;;\n"
            "  restart)\n"
            "    $0 stop\n"
            "    sleep 1\n"
            "    $0 start\n"
            "    ;;\n"
            "  status)\n"
            "    start-stop-daemon --status --pidfile $PIDFILE\n"
            "    ;;\n"
            "  *)\n"
            "    echo \"Usage: $0 {start|stop|restart|status}\"\n"
            "    exit 1\n"
            "    ;;\n"
            "esac\n"
            "exit 0\n"
        )

    def install(self, start: bool = True) -> Path:
        path = self._service_file()
        path.write_text(self._content(), encoding="utf-8")
        path.chmod(0o755)
        if shutil.which("update-rc.d"):
            _run(["update-rc.d", SERVICE_NAME, "defaults"])
        elif shutil.which("chkconfig"):
            _run(["chkconfig", "--add", SERVICE_NAME])
        if start:
            _run(["service", SERVICE_NAME, "restart"])
        return path

    def status(self) -> str:
        code, detail = _run(["service", SERVICE_NAME, "status"])
        return detail if code == 0 else detail or "unknown"

    def uninstall(self) -> bool:
        _run(["service", SERVICE_NAME, "stop"])
        if shutil.which("update-rc.d"):
            _run(["update-rc.d", "-f", SERVICE_NAME, "remove"])
        elif shutil.which("chkconfig"):
            _run(["chkconfig", "--del", SERVICE_NAME])
        path = self._service_file()
        if path.exists():
            path.unlink()
        return True

    def status_command_hint(self) -> str:
        return "service hotaru status"


class UpstartInit(BaseInitSystem):
    id = "upstart"
    display_name = "Upstart"
    priority = 42

    def detect(self) -> bool:
        return shutil.which("initctl") is not None and Path("/etc/init").is_dir() and is_root()

    def _service_file(self) -> Path:
        return Path("/etc/init/hotaru.conf")

    def install(self, start: bool = True) -> Path:
        path = self._service_file()
        content = (
            "description \"Hotaru Userbot\"\n"
            "start on runlevel [2345]\n"
            "stop on runlevel [016]\n"
            "respawn\n"
            "respawn limit 10 5\n"
            f"chdir {repo_root()}\n"
            f"exec {executable()} -m hotaru\n"
        )
        path.write_text(content, encoding="utf-8")
        if start and shutil.which("initctl"):
            _run(["initctl", "start", SERVICE_NAME])
        return path

    def status(self) -> str:
        code, detail = _run(["initctl", "status", SERVICE_NAME])
        return detail if code == 0 else detail or "unknown"

    def uninstall(self) -> bool:
        if shutil.which("initctl"):
            _run(["initctl", "stop", SERVICE_NAME])
        path = self._service_file()
        if path.exists():
            path.unlink()
        return True

    def status_command_hint(self) -> str:
        return "initctl status hotaru"


class BsdRcInit(BaseInitSystem):
    id = "bsd_rc"
    display_name = "BSD rc.d"
    priority = 35

    def detect(self) -> bool:
        return (
            sys.platform.startswith(("freebsd", "netbsd", "dragonfly"))
            or Path("/etc/rc.subr").is_file()
        )

    def _service_file(self) -> Path:
        if Path("/usr/local/etc/rc.d").is_dir():
            return Path("/usr/local/etc/rc.d") / SERVICE_NAME
        return Path("/etc/rc.d") / SERVICE_NAME

    def install(self, start: bool = True) -> Path:
        path = self._service_file()
        content = (
            "#!/bin/sh\n"
            "# PROVIDE: hotaru\n"
            "# REQUIRE: DAEMON NETWORKING\n"
            "# KEYWORD: shutdown\n"
            ". /etc/rc.subr\n"
            "name=\"hotaru\"\n"
            "rcvar=\"hotaru_enable\"\n"
            "command=\"/usr/sbin/daemon\"\n"
            f"command_args=\"-r -f -p /var/run/hotaru.pid {executable()} -m hotaru\"\n"
            "load_rc_config $name\n"
            ": ${hotaru_enable:=NO}\n"
            "run_rc_command \"$1\"\n"
        )
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
        if start and shutil.which("service"):
            _run(["service", SERVICE_NAME, "start"])
        return path

    def status(self) -> str:
        code, detail = _run(["service", SERVICE_NAME, "status"])
        return detail if code == 0 else detail or "unknown"

    def uninstall(self) -> bool:
        if shutil.which("service"):
            _run(["service", SERVICE_NAME, "stop"])
        path = self._service_file()
        if path.exists():
            path.unlink()
        return True

    def status_command_hint(self) -> str:
        return "service hotaru status"


class OpenBsdRcInit(BaseInitSystem):
    id = "openbsd_rc"
    display_name = "OpenBSD rc.d"
    priority = 36

    def detect(self) -> bool:
        return sys.platform.startswith("openbsd") or shutil.which("rcctl") is not None

    def _service_file(self) -> Path:
        return Path("/etc/rc.d") / SERVICE_NAME

    def install(self, start: bool = True) -> Path:
        path = self._service_file()
        content = (
            "#!/bin/ksh\n"
            f"daemon=\"{executable()}\"\n"
            "daemon_flags=\"-m hotaru\"\n"
            f"daemon_user=\"{_current_user()}\"\n"
            ". /etc/rc.d/rc.subr\n"
            "rc_bg=YES\n"
            "rc_reload=NO\n"
            "rc_cmd $1\n"
        )
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
        if shutil.which("rcctl"):
            _run(["rcctl", "enable", SERVICE_NAME])
            if start:
                _run(["rcctl", "start", SERVICE_NAME])
        return path

    def status(self) -> str:
        code, detail = _run(["rcctl", "check", SERVICE_NAME])
        return detail if code == 0 else detail or "unknown"

    def uninstall(self) -> bool:
        if shutil.which("rcctl"):
            _run(["rcctl", "stop", SERVICE_NAME])
            _run(["rcctl", "disable", SERVICE_NAME])
        path = self._service_file()
        if path.exists():
            path.unlink()
        return True

    def status_command_hint(self) -> str:
        return "rcctl check hotaru"


class ProcdInit(BaseInitSystem):
    id = "procd"
    display_name = "procd (OpenWrt)"
    priority = 38

    def detect(self) -> bool:
        return Path("/etc/openwrt_release").is_file() or Path("/sbin/procd").is_file()

    def _service_file(self) -> Path:
        return Path("/etc/init.d") / SERVICE_NAME

    def install(self, start: bool = True) -> Path:
        path = self._service_file()
        content = (
            "#!/bin/sh /etc/rc.common\n"
            "USE_PROCD=1\n"
            "START=95\n"
            "STOP=10\n"
            "start_service() {\n"
            "    procd_open_instance\n"
            f"    procd_set_param command {executable()} -m hotaru\n"
            "    procd_set_param respawn\n"
            f"    procd_set_param working_dir {repo_root()}\n"
            "    procd_close_instance\n"
            "}\n"
        )
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
        _run([str(path), "enable"])
        if start:
            _run([str(path), "restart"])
        return path

    def status(self) -> str:
        code, detail = _run([str(self._service_file()), "status"])
        return detail if code == 0 else detail or "unknown"

    def uninstall(self) -> bool:
        path = self._service_file()
        _run([str(path), "stop"])
        _run([str(path), "disable"])
        if path.exists():
            path.unlink()
        return True

    def status_command_hint(self) -> str:
        return "/etc/init.d/hotaru status"


class ShepherdInit(BaseInitSystem):
    id = "shepherd"
    display_name = "GNU Shepherd"
    priority = 45

    def detect(self) -> bool:
        return shutil.which("herd") is not None or shutil.which("shepherd") is not None

    def _conf_file(self) -> Path:
        return Path.home() / ".config" / "shepherd" / "init.d" / f"{SERVICE_NAME}.scm"

    def install(self, start: bool = True) -> Path:
        path = self._conf_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            f"(define hotaru\n"
            f"  (make <service>\n"
            f"    #:provides '(hotaru)\n"
            f"    #:requires '()\n"
            f"    #:start (make-forkexec-constructor '(\"{executable()}\" \"-m\" \"hotaru\")\n"
            f"                                        #:directory \"{repo_root()}\")\n"
            f"    #:stop (make-kill-destructor)\n"
            f"    #:respawn? #t))\n"
            f"(register-services hotaru)\n"
            f"(start-service hotaru)\n"
        )
        path.write_text(content, encoding="utf-8")
        if start and shutil.which("herd"):
            _run(["herd", "load", "root", str(path)])
        return path

    def status(self) -> str:
        code, detail = _run(["herd", "status", SERVICE_NAME])
        return detail if code == 0 else detail or "unknown"

    def uninstall(self) -> bool:
        if shutil.which("herd"):
            _run(["herd", "stop", SERVICE_NAME])
        path = self._conf_file()
        if path.exists():
            path.unlink()
        return True

    def status_command_hint(self) -> str:
        return "herd status hotaru"


class SupervisordInit(BaseInitSystem):
    id = "supervisord"
    display_name = "Supervisord"
    priority = 50

    def detect(self) -> bool:
        return (
            shutil.which("supervisorctl") is not None
            or Path("/etc/supervisor/conf.d").is_dir()
            or Path("/etc/supervisord.d").is_dir()
        )

    def _conf_file(self) -> Path:
        if Path("/etc/supervisor/conf.d").is_dir():
            return Path("/etc/supervisor/conf.d/hotaru.conf")
        if Path("/etc/supervisord.d").is_dir():
            return Path("/etc/supervisord.d/hotaru.conf")
        return Path.home() / ".supervisor" / "hotaru.conf"

    def install(self, start: bool = True) -> Path:
        path = self._conf_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "[program:hotaru]\n"
            f"command={executable()} -m hotaru\n"
            f"directory={repo_root()}\n"
            "autostart=true\n"
            "autorestart=true\n"
            "redirect_stderr=true\n"
        )
        path.write_text(content, encoding="utf-8")
        if shutil.which("supervisorctl"):
            _run(["supervisorctl", "reread"])
            _run(["supervisorctl", "update"])
            if start:
                _run(["supervisorctl", "restart", SERVICE_NAME])
        return path

    def status(self) -> str:
        code, detail = _run(["supervisorctl", "status", SERVICE_NAME])
        return detail if code == 0 else detail or "unknown"

    def uninstall(self) -> bool:
        if shutil.which("supervisorctl"):
            _run(["supervisorctl", "stop", SERVICE_NAME])
        path = self._conf_file()
        if path.exists():
            path.unlink()
        if shutil.which("supervisorctl"):
            _run(["supervisorctl", "update"])
        return True

    def status_command_hint(self) -> str:
        return "supervisorctl status hotaru"


class CircusInit(BaseInitSystem):
    id = "circus"
    display_name = "Circus"
    priority = 52

    def detect(self) -> bool:
        return shutil.which("circusctl") is not None or Path("/etc/circus.d").is_dir()

    def _conf_file(self) -> Path:
        return Path("/etc/circus.d/hotaru.ini") if is_root() else Path.home() / ".circus.d" / "hotaru.ini"

    def install(self, start: bool = True) -> Path:
        path = self._conf_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "[watcher:hotaru]\n"
            f"cmd = {executable()} -m hotaru\n"
            f"working_dir = {repo_root()}\n"
            "warmup_delay = 0\n"
            "numprocesses = 1\n"
            "autostart = true\n"
        )
        path.write_text(content, encoding="utf-8")
        if start and shutil.which("circusctl"):
            _run(["circusctl", "restart", SERVICE_NAME])
        return path

    def status(self) -> str:
        code, detail = _run(["circusctl", "status", SERVICE_NAME])
        return detail if code == 0 else detail or "unknown"

    def uninstall(self) -> bool:
        if shutil.which("circusctl"):
            _run(["circusctl", "stop", SERVICE_NAME])
        path = self._conf_file()
        if path.exists():
            path.unlink()
        return True

    def status_command_hint(self) -> str:
        return "circusctl status hotaru"


class MonitInit(BaseInitSystem):
    id = "monit"
    display_name = "Monit"
    priority = 54

    def detect(self) -> bool:
        return shutil.which("monit") is not None and (Path("/etc/monit/conf.d").is_dir() or Path("/etc/monit.d").is_dir())

    def _conf_file(self) -> Path:
        if Path("/etc/monit/conf.d").is_dir():
            return Path("/etc/monit/conf.d/hotaru")
        return Path("/etc/monit.d/hotaru")

    def install(self, start: bool = True) -> Path:
        path = self._conf_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            f"check process hotaru matching \"python -m hotaru\"\n"
            f"    start program = \"/bin/sh -c 'cd {repo_root()} && nohup {executable()} -m hotaru >/dev/null 2>&1 &'\"\n"
            f"    stop program = \"/usr/bin/pkill -f 'python -m hotaru'\"\n"
        )
        path.write_text(content, encoding="utf-8")
        if shutil.which("monit"):
            _run(["monit", "reload"])
        return path

    def status(self) -> str:
        code, detail = _run(["monit", "status", SERVICE_NAME])
        return detail if code == 0 else detail or "unknown"

    def uninstall(self) -> bool:
        path = self._conf_file()
        if path.exists():
            path.unlink()
        if shutil.which("monit"):
            _run(["monit", "reload"])
        return True

    def status_command_hint(self) -> str:
        return "monit status hotaru"


class Pm2Init(BaseInitSystem):
    id = "pm2"
    display_name = "PM2"
    priority = 48

    def detect(self) -> bool:
        return shutil.which("pm2") is not None

    def install(self, start: bool = True) -> Path:
        cmd = [
            "pm2",
            "start",
            executable(),
            "--name",
            SERVICE_NAME,
            "--cwd",
            str(repo_root()),
            "--",
            "-m",
            "hotaru",
        ]
        code, detail = _run(cmd)
        if code != 0:
            raise DaemonError(f"pm2 start failed: {detail}")
        _run(["pm2", "save"])
        return Path.home() / ".pm2"

    def status(self) -> str:
        code, detail = _run(["pm2", "show", SERVICE_NAME])
        return detail if code == 0 else detail or "unknown"

    def uninstall(self) -> bool:
        _run(["pm2", "delete", SERVICE_NAME])
        _run(["pm2", "save"])
        return True

    def status_command_hint(self) -> str:
        return "pm2 show hotaru"


class GodInit(BaseInitSystem):
    id = "god"
    display_name = "God"
    priority = 56

    def detect(self) -> bool:
        return shutil.which("god") is not None and Path("/etc/god").is_dir()

    def _conf_file(self) -> Path:
        return Path("/etc/god/conf.d/hotaru.god")

    def install(self, start: bool = True) -> Path:
        path = self._conf_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "God.watch do |w|\n"
            "  w.name = \"hotaru\"\n"
            f"  w.dir = \"{repo_root()}\"\n"
            f"  w.start = \"{executable()} -m hotaru\"\n"
            "  w.keepalive\n"
            "end\n"
        )
        path.write_text(content, encoding="utf-8")
        if start and shutil.which("god"):
            _run(["god", "load", str(path)])
        return path

    def status(self) -> str:
        code, detail = _run(["god", "status", SERVICE_NAME])
        return detail if code == 0 else detail or "unknown"

    def uninstall(self) -> bool:
        if shutil.which("god"):
            _run(["god", "remove", SERVICE_NAME])
        path = self._conf_file()
        if path.exists():
            path.unlink()
        return True

    def status_command_hint(self) -> str:
        return "god status hotaru"


class DaemontoolsInit(BaseInitSystem):
    id = "daemontools"
    display_name = "daemontools"
    priority = 58

    def detect(self) -> bool:
        has_tools = shutil.which("svc") is not None or shutil.which("supervise") is not None
        return has_tools and (Path("/service").is_dir() or Path("/etc/service").is_dir())

    def _service_dir(self) -> Path:
        if Path("/service").is_dir():
            return Path("/service") / SERVICE_NAME
        return Path("/etc/service") / SERVICE_NAME

    def install(self, start: bool = True) -> Path:
        sdir = self._service_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        run_file = sdir / "run"
        content = (
            "#!/bin/sh\n"
            "exec 2>&1\n"
            f"cd \"{repo_root()}\"\n"
            f"exec \"{executable()}\" -m hotaru\n"
        )
        run_file.write_text(content, encoding="utf-8")
        run_file.chmod(0o755)
        if start and shutil.which("svc"):
            _run(["svc", "-u", str(sdir)])
        return run_file

    def status(self) -> str:
        sdir = self._service_dir()
        code, detail = _run(["svstat", str(sdir)])
        return detail if code == 0 else detail or "unknown"

    def uninstall(self) -> bool:
        sdir = self._service_dir()
        if shutil.which("svc"):
            _run(["svc", "-d", str(sdir)])
        run_file = sdir / "run"
        if run_file.exists():
            run_file.unlink()
        return True

    def status_command_hint(self) -> str:
        return f"svstat {self._service_dir()}"


class PerpInit(BaseInitSystem):
    id = "perp"
    display_name = "perp"
    priority = 60

    def detect(self) -> bool:
        return shutil.which("perpctl") is not None or Path("/etc/perp").is_dir()

    def _service_dir(self) -> Path:
        return Path("/etc/perp") / SERVICE_NAME

    def install(self, start: bool = True) -> Path:
        sdir = self._service_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        rc_main = sdir / "rc.main"
        content = (
            "#!/bin/sh\n"
            "exec 2>&1\n"
            f"TARGET=\"{executable()}\"\n"
            "case \"$1\" in\n"
            "  start)\n"
            f"    cd \"{repo_root()}\"\n"
            f"    exec {executable()} -m hotaru\n"
            "    ;;\n"
            "esac\n"
            "exit 0\n"
        )
        rc_main.write_text(content, encoding="utf-8")
        rc_main.chmod(0o755)
        if start and shutil.which("perpctl"):
            _run(["perpctl", "A", SERVICE_NAME])
        return rc_main

    def status(self) -> str:
        code, detail = _run(["perpctl", "q", SERVICE_NAME])
        return detail if code == 0 else detail or "unknown"

    def uninstall(self) -> bool:
        if shutil.which("perpctl"):
            _run(["perpctl", "k", SERVICE_NAME])
        rc_main = self._service_dir() / "rc.main"
        if rc_main.exists():
            rc_main.unlink()
        return True

    def status_command_hint(self) -> str:
        return "perpctl q hotaru"


class FinitInit(BaseInitSystem):
    id = "finit"
    display_name = "Finit"
    priority = 62

    def detect(self) -> bool:
        return shutil.which("finit") is not None or Path("/etc/finit.d").is_dir()

    def _conf_file(self) -> Path:
        return Path("/etc/finit.d/hotaru.conf")

    def install(self, start: bool = True) -> Path:
        path = self._conf_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        content = f"service [2345] {executable()} -m hotaru -- Hotaru Userbot\n"
        path.write_text(content, encoding="utf-8")
        if shutil.which("initctl"):
            _run(["initctl", "reload"])
            if start:
                _run(["initctl", "start", SERVICE_NAME])
        return path

    def status(self) -> str:
        code, detail = _run(["initctl", "status", SERVICE_NAME])
        return detail if code == 0 else detail or "unknown"

    def uninstall(self) -> bool:
        if shutil.which("initctl"):
            _run(["initctl", "stop", SERVICE_NAME])
        path = self._conf_file()
        if path.exists():
            path.unlink()
        if shutil.which("initctl"):
            _run(["initctl", "reload"])
        return True

    def status_command_hint(self) -> str:
        return "initctl status hotaru"


class BusyboxInit(BaseInitSystem):
    id = "busybox"
    display_name = "BusyBox init"
    priority = 64

    def detect(self) -> bool:
        return shutil.which("busybox") is not None and (Path("/etc/inittab").is_file() or Path("/etc/init.d/rcS").is_file())

    def _service_file(self) -> Path:
        return Path("/etc/init.d/hotaru")

    def install(self, start: bool = True) -> Path:
        path = self._service_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "#!/bin/sh\n"
            f"cd \"{repo_root()}\"\n"
            f"nohup {executable()} -m hotaru >/dev/null 2>&1 &\n"
        )
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
        if start:
            _run([str(path)])
        return path

    def status(self) -> str:
        code, detail = _run(["pgrep", "-f", "python -m hotaru"])
        return f"running (pid {detail})" if code == 0 and detail else "not running"

    def uninstall(self) -> bool:
        path = self._service_file()
        if path.exists():
            path.unlink()
        return True

    def status_command_hint(self) -> str:
        return "pgrep -f 'python -m hotaru'"


class MinitInit(BaseInitSystem):
    id = "minit"
    display_name = "minit"
    priority = 66

    def detect(self) -> bool:
        return shutil.which("msvc") is not None or Path("/etc/minit").is_dir()

    def _service_dir(self) -> Path:
        return Path("/etc/minit") / SERVICE_NAME

    def install(self, start: bool = True) -> Path:
        sdir = self._service_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        run_file = sdir / "run"
        content = (
            "#!/bin/sh\n"
            f"cd \"{repo_root()}\"\n"
            f"exec {executable()} -m hotaru\n"
        )
        run_file.write_text(content, encoding="utf-8")
        run_file.chmod(0o755)
        if start and shutil.which("msvc"):
            _run(["msvc", "-u", SERVICE_NAME])
        return run_file

    def status(self) -> str:
        code, detail = _run(["msvc", "-s", SERVICE_NAME])
        return detail if code == 0 else detail or "unknown"

    def uninstall(self) -> bool:
        if shutil.which("msvc"):
            _run(["msvc", "-d", SERVICE_NAME])
        run_file = self._service_dir() / "run"
        if run_file.exists():
            run_file.unlink()
        return True

    def status_command_hint(self) -> str:
        return "msvc -s hotaru"


class CinitInit(BaseInitSystem):
    id = "cinit"
    display_name = "cinit"
    priority = 68

    def detect(self) -> bool:
        return shutil.which("cinit") is not None or Path("/etc/cinit").is_dir()

    def _service_dir(self) -> Path:
        return Path("/etc/cinit") / SERVICE_NAME

    def install(self, start: bool = True) -> Path:
        sdir = self._service_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        cmd_file = sdir / "cmd"
        cmd_file.write_text(f"{executable()} -m hotaru\n", encoding="utf-8")
        cmd_file.chmod(0o755)
        return cmd_file

    def status(self) -> str:
        code, detail = _run(["cinit", "-s", SERVICE_NAME])
        return detail if code == 0 else detail or "unknown"

    def uninstall(self) -> bool:
        cmd_file = self._service_dir() / "cmd"
        if cmd_file.exists():
            cmd_file.unlink()
        return True

    def status_command_hint(self) -> str:
        return "cinit -s hotaru"


class SmfInit(BaseInitSystem):
    id = "smf"
    display_name = "Solaris SMF"
    priority = 70

    def detect(self) -> bool:
        return shutil.which("svcadm") is not None and shutil.which("svcs") is not None

    def _manifest(self) -> Path:
        return Path("/var/svc/manifest/site/hotaru.xml")

    def install(self, start: bool = True) -> Path:
        path = self._manifest()
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "<?xml version=\"1.0\"?>\n"
            "<!DOCTYPE service_bundle SYSTEM \"/usr/share/lib/xml/dtd/service_bundle.dtd.1\">\n"
            "<service_bundle type=\"manifest\" name=\"hotaru\">\n"
            "  <service name=\"site/hotaru\" type=\"service\" version=\"1\">\n"
            "    <create_default_instance enabled=\"true\"/>\n"
            "    <single_instance/>\n"
            f"    <exec_method type=\"method\" name=\"start\" exec=\"{executable()} -m hotaru &amp;\" timeout_seconds=\"60\"/>\n"
            "    <exec_method type=\"method\" name=\"stop\" exec=\":kill\" timeout_seconds=\"60\"/>\n"
            "  </service>\n"
            "</service_bundle>\n"
        )
        path.write_text(content, encoding="utf-8")
        if shutil.which("svccfg"):
            _run(["svccfg", "import", str(path)])
        if start and shutil.which("svcadm"):
            _run(["svcadm", "enable", "site/hotaru"])
        return path

    def status(self) -> str:
        code, detail = _run(["svcs", "site/hotaru"])
        return detail if code == 0 else detail or "unknown"

    def uninstall(self) -> bool:
        if shutil.which("svcadm"):
            _run(["svcadm", "disable", "site/hotaru"])
        if shutil.which("svccfg"):
            _run(["svccfg", "delete", "site/hotaru"])
        path = self._manifest()
        if path.exists():
            path.unlink()
        return True

    def status_command_hint(self) -> str:
        return "svcs site/hotaru"


class NoshInit(BaseInitSystem):
    id = "nosh"
    display_name = "nosh"
    priority = 72

    def detect(self) -> bool:
        return shutil.which("system-control") is not None and Path("/etc/service-bundles").is_dir()

    def _service_dir(self) -> Path:
        return Path("/etc/service-bundles/services") / SERVICE_NAME

    def install(self, start: bool = True) -> Path:
        sdir = self._service_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        run_file = sdir / "run"
        content = (
            "#!/bin/sh\n"
            f"cd \"{repo_root()}\"\n"
            f"exec {executable()} -m hotaru\n"
        )
        run_file.write_text(content, encoding="utf-8")
        run_file.chmod(0o755)
        if shutil.which("system-control"):
            _run(["system-control", "enable", SERVICE_NAME])
            if start:
                _run(["system-control", "start", SERVICE_NAME])
        return run_file

    def status(self) -> str:
        code, detail = _run(["system-control", "status", SERVICE_NAME])
        return detail if code == 0 else detail or "unknown"

    def uninstall(self) -> bool:
        if shutil.which("system-control"):
            _run(["system-control", "stop", SERVICE_NAME])
            _run(["system-control", "disable", SERVICE_NAME])
        run_file = self._service_dir() / "run"
        if run_file.exists():
            run_file.unlink()
        return True

    def status_command_hint(self) -> str:
        return "system-control status hotaru"


class SimpleDaemonInit(BaseInitSystem):
    id = "simple"
    display_name = "Background Script (nohup)"
    priority = 999

    def detect(self) -> bool:
        return True

    def _script_file(self) -> Path:
        return repo_root() / "hotaru-service.sh"

    def install(self, start: bool = True) -> Path:
        path = self._script_file()
        content = (
            "#!/usr/bin/env bash\n"
            f"DIR=\"{repo_root()}\"\n"
            f"EXE=\"{executable()}\"\n"
            "PIDFILE=\"$DIR/hotaru.pid\"\n"
            "case \"$1\" in\n"
            "  start)\n"
            "    if [ -f \"$PIDFILE\" ] && kill -0 \"$(cat \"$PIDFILE\")\" 2>/dev/null; then\n"
            "      echo \"Hotaru is already running with PID $(cat \"$PIDFILE\")\"\n"
            "      exit 0\n"
            "    fi\n"
            "    cd \"$DIR\"\n"
            "    nohup \"$EXE\" -m hotaru >/dev/null 2>&1 & echo $! > \"$PIDFILE\"\n"
            "    echo \"Hotaru started with PID $(cat \"$PIDFILE\")\"\n"
            "    ;;\n"
            "  stop)\n"
            "    if [ -f \"$PIDFILE\" ]; then\n"
            "      kill \"$(cat \"$PIDFILE\")\" 2>/dev/null || true\n"
            "      rm -f \"$PIDFILE\"\n"
            "      echo \"Hotaru stopped\"\n"
            "    else\n"
            "      pkill -f 'python -m hotaru' || true\n"
            "    fi\n"
            "    ;;\n"
            "  restart)\n"
            "    $0 stop\n"
            "    sleep 1\n"
            "    $0 start\n"
            "    ;;\n"
            "  status)\n"
            "    if [ -f \"$PIDFILE\" ] && kill -0 \"$(cat \"$PIDFILE\")\" 2>/dev/null; then\n"
            "      echo \"Hotaru is running with PID $(cat \"$PIDFILE\")\"\n"
            "    else\n"
            "      echo \"Hotaru is stopped\"\n"
            "    fi\n"
            "    ;;\n"
            "  *)\n"
            "    echo \"Usage: $0 {start|stop|restart|status}\"\n"
            "    exit 1\n"
            "    ;;\n"
            "esac\n"
        )
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
        if start:
            _run([str(path), "start"])
        return path

    def status(self) -> str:
        path = self._script_file()
        if not path.exists():
            return "not installed"
        code, detail = _run([str(path), "status"])
        return detail if code == 0 else detail or "unknown"

    def uninstall(self) -> bool:
        path = self._script_file()
        if path.exists():
            _run([str(path), "stop"])
            path.unlink()
        return True

    def status_command_hint(self) -> str:
        return f"bash {self._script_file()} status"


ALL_INIT_SYSTEMS: tuple[type[BaseInitSystem], ...] = (
    TermuxServicesInit,
    TermuxBootInit,
    LaunchdInit,
    SystemdInit,
    DinitInit,
    OpenRCInit,
    RunitInit,
    S6_66Init,
    S6Init,
    BsdRcInit,
    OpenBsdRcInit,
    ProcdInit,
    SysVInit,
    UpstartInit,
    ShepherdInit,
    SupervisordInit,
    CircusInit,
    MonitInit,
    Pm2Init,
    GodInit,
    DaemontoolsInit,
    PerpInit,
    FinitInit,
    BusyboxInit,
    MinitInit,
    CinitInit,
    SmfInit,
    NoshInit,
    SimpleDaemonInit,
)


def detect_init_system(preferred: str | None = None) -> BaseInitSystem:
    candidates: list[BaseInitSystem] = [cls() for cls in ALL_INIT_SYSTEMS]
    if preferred:
        pref = preferred.strip().lower()
        for cand in candidates:
            if cand.id.lower() == pref:
                return cand
    for cand in candidates:
        if cand.id != "simple" and cand.detect():
            return cand
    return SimpleDaemonInit()


def available_init_systems() -> list[BaseInitSystem]:
    return [cls() for cls in ALL_INIT_SYSTEMS if cls().detect()]


def install_daemon(start: bool = True, init_name: str | None = None) -> Path:
    init = detect_init_system(init_name)
    return init.install(start=start)


def daemon_status(init_name: str | None = None) -> str:
    init = detect_init_system(init_name)
    return init.status()


def uninstall_daemon(init_name: str | None = None) -> bool:
    init = detect_init_system(init_name)
    return init.uninstall()
