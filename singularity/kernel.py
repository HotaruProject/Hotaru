from __future__ import annotations

import asyncio
import http.client
import json
import logging
import os
import secrets
import shutil
import signal
import subprocess
import time
import urllib.parse
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import hotaru as _hotaru_pkg
except Exception:
    _hotaru_pkg = None

try:
    from relay.firewall import trusted_scope
except Exception:
    trusted_scope = nullcontext

from .horizon import HorizonTunnel
from .observatory import ObservatoryTelemetry
from .relay import (
    RelaySyncReport,
    configure_botfather_webapp,
    ensure_inline_bot_ready,
    get_sanctuary_app_state,
    sync_owner_menu_button,
    resolve_inline_banner_url,
)

log = logging.getLogger("hotaru.singularity.kernel")

SINGULARITY_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SINGULARITY_DIR / "backend"
FRONTEND_DIR = SINGULARITY_DIR / "frontend"


def _parse_bool(val: Any, default: bool) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    text = str(val).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


@dataclass(slots=True)
class KernelConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8787
    data_mode: str = "live"
    tunnel_enabled: bool = True
    auto_botfather: bool = True
    short_name: str = "dashboard"
    bot_credential: str = ""
    bot_username: str = ""
    bot_id: int = 0
    owner_id: int | None = None
    signing_key: str = ""
    cache_dir: Path | None = None
    observatory_path: Path | None = None

    @classmethod
    def from_runtime(cls, runtime: Any | None = None) -> KernelConfig:
        rt_cfg = getattr(runtime, "config", None) if runtime is not None else None
        state = getattr(runtime, "state", None) if runtime is not None else None
        inline_info = getattr(getattr(runtime, "inline", None), "info", None)

        def _setting(key: str) -> str | None:
            if state is not None and hasattr(state, "get_setting"):
                try:
                    val = state.get_setting(key)
                    return str(val) if val is not None else None
                except Exception:
                    return None
            return None

        enabled = _parse_bool(
            os.environ.get("DASHBOARD_ENABLED")
            or os.environ.get("HOTARU_DASHBOARD_ENABLED")
            or _setting("singularity-enabled"),
            getattr(rt_cfg, "singularity_enabled", True),
        )
        host = (
            os.environ.get("DASHBOARD_HOST")
            or os.environ.get("HOTARU_DASHBOARD_HOST")
            or getattr(rt_cfg, "singularity_host", "127.0.0.1")
            or "127.0.0.1"
        )
        raw_port = (
            os.environ.get("DASHBOARD_PORT")
            or os.environ.get("HOTARU_DASHBOARD_PORT")
            or _setting("singularity-port")
            or getattr(rt_cfg, "singularity_port", 8787)
        )
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            port = 8787

        raw_mode = (
            os.environ.get("DASHBOARD_MODE")
            or os.environ.get("HOTARU_DASHBOARD_MODE")
            or _setting("singularity-mode")
            or getattr(rt_cfg, "singularity_mode", "live")
            or "live"
        )
        data_mode = "mock" if str(raw_mode).strip().lower() == "mock" else "live"

        tunnel_enabled = _parse_bool(
            os.environ.get("DASHBOARD_TUNNEL")
            or os.environ.get("HOTARU_DASHBOARD_TUNNEL")
            or _setting("singularity-tunnel"),
            getattr(rt_cfg, "singularity_tunnel", True),
        )
        auto_botfather = _parse_bool(
            os.environ.get("DASHBOARD_AUTO_BOTFATHER")
            or os.environ.get("HOTARU_DASHBOARD_AUTO_BOTFATHER")
            or _setting("singularity-auto-botfather"),
            getattr(rt_cfg, "singularity_auto_botfather", True),
        )
        short_name = (
            os.environ.get("DASHBOARD_SHORT_NAME")
            or _setting("singularity-short-name")
            or getattr(rt_cfg, "singularity_short_name", "dashboard")
            or "dashboard"
        ).strip()

        bot_cred = (
            os.environ.get("DASHBOARD_BOT_TOKEN")
            or os.environ.get("BOT_TOKEN")
            or getattr(inline_info, "token", None)
            or _setting("inline-bot-token")
            or getattr(rt_cfg, "bot_token", None)
            or ""
        ).strip()
        bot_username = (
            os.environ.get("DASHBOARD_BOT_USERNAME")
            or getattr(inline_info, "username", None)
            or _setting("inline-bot-username")
            or ""
        ).strip()
        raw_bid = getattr(inline_info, "bot_id", None) or _setting("inline-bot-id") or 0
        bot_id = int(raw_bid) if isinstance(raw_bid, int) or str(raw_bid).isdigit() else 0

        raw_owner = (
            os.environ.get("DASHBOARD_OWNER_ID")
            or os.environ.get("OWNER_ID")
            or getattr(runtime, "owner_id", None)
            or getattr(getattr(runtime, "kernel", None), "owner_id", None)
            or _setting("owner-id")
        )
        owner_id: int | None = None
        if raw_owner is not None and str(raw_owner).strip().isdigit():
            owner_id = int(str(raw_owner).strip())

        signing_key = (
            os.environ.get("DASHBOARD_SIGNING_KEY")
            or _setting("singularity-signing-key")
            or ""
        ).strip()
        if not signing_key:
            signing_key = secrets.token_hex(32)
            if state is not None and hasattr(state, "set_setting"):
                try:
                    state.set_setting("singularity-signing-key", signing_key)
                except Exception:
                    pass

        session_dir = getattr(rt_cfg, "session_dir", None) or getattr(
            rt_cfg, "data_dir", None
        )
        cache_dir = (
            (Path(session_dir) / "bin")
            if session_dir
            else (Path.home() / ".hotaru" / "bin")
        )

        obs = getattr(runtime, "observatory", None) if runtime is not None else None
        obs_path = getattr(obs, "path", None) if obs is not None else None
        resolved_obs = (
            obs_path.resolve()
            if isinstance(obs_path, Path)
            else (SINGULARITY_DIR.parent / "observatory" / "runtime" / "events.jsonl")
        )

        return cls(
            enabled=enabled,
            host=host,
            port=port,
            data_mode=data_mode,
            tunnel_enabled=tunnel_enabled,
            auto_botfather=auto_botfather,
            short_name=short_name,
            bot_credential=bot_cred,
            bot_username=bot_username,
            bot_id=bot_id,
            owner_id=owner_id,
            signing_key=signing_key,
            cache_dir=cache_dir,
            observatory_path=resolved_obs,
        )


def _http_local_json(
    method: str,
    url: str,
    ipc_header: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 2.5,
) -> tuple[int, dict[str, Any]]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
        raise ValueError("Internal IPC requires loopback HTTP URL")
    body_bytes = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"X-Internal-Secret": ipc_header}
    if body_bytes is not None:
        headers["Content-Type"] = "application/json"
    path_part = parsed.path or "/"
    if parsed.query:
        path_part = f"{path_part}?{parsed.query}"
    with trusted_scope():
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout)
        try:
            conn.request(method, path_part, body=body_bytes, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
            if 200 <= resp.status < 300:
                return resp.status, (json.loads(raw) if raw else {})
            return resp.status, {}
        finally:
            conn.close()


class SingularityKernel:
    def __init__(
        self,
        runtime: Any | None = None,
        config: KernelConfig | None = None,
        *,
        watchdog_interval: float = 1.0,
        tunnel_restart_cooldown: float = 5.0,
        sync_retry_cooldown: float = 12.0,
    ) -> None:
        self.runtime = runtime
        self.config = config or KernelConfig.from_runtime(runtime)
        self.collector = ObservatoryTelemetry()
        self.watchdog_interval = watchdog_interval
        self.tunnel_restart_cooldown = tunnel_restart_cooldown
        self.sync_retry_cooldown = sync_retry_cooldown
        self._ipc_key = secrets.token_hex(24)
        self._provision_lock = asyncio.Lock()

        self._node_proc: asyncio.subprocess.Process | None = None
        self._metrics_task: asyncio.Task[None] | None = None
        self._tunnel_task: asyncio.Task[None] | None = None
        self._rotation_task: asyncio.Task[None] | None = None
        self._tunnel: HorizonTunnel | None = None

        db_state = get_sanctuary_app_state(getattr(runtime, "state", None))
        self.tunnel_url: str | None = None
        self.webapp_registered: bool = db_state.registered
        self.webapp_link: str | None = db_state.app_link or None
        self._synced_tunnel_url: str = ""
        self._last_tunnel_restart_ts: float = 0.0
        self._last_sync_attempt_ts: float = 0.0
        self._running = False

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.config.port}"

    @property
    def is_running(self) -> bool:
        return (
            self._running
            and self._node_proc is not None
            and self._node_proc.returncode is None
        )

    def _resolve_owner_id(self) -> int | None:
        if self.config.owner_id:
            return self.config.owner_id
        if self.runtime is not None:
            cand = (
                getattr(self.runtime, "owner_id", None)
                or getattr(getattr(self.runtime, "kernel", None), "owner_id", None)
                or getattr(getattr(getattr(self.runtime, "app", None), "session", None), "self_id", None)
            )
            if isinstance(cand, int) and cand > 0:
                self.config.owner_id = cand
                return cand
        return None

    def _resolve_bot_username(self) -> str:
        if self.runtime is not None:
            inline_info = getattr(getattr(self.runtime, "inline", None), "info", None)
            if inline_info is not None and getattr(inline_info, "username", None):
                uname = str(inline_info.username).lstrip("@").strip()
                if uname:
                    self.config.bot_username = uname
                    return uname
        if self.config.bot_username:
            return self.config.bot_username.lstrip("@").strip()
        if self.runtime is not None:
            state = getattr(self.runtime, "state", None)
            if state is not None and hasattr(state, "get_setting"):
                try:
                    uname = str(state.get_setting("inline-bot-username") or "").lstrip("@").strip()
                    if uname:
                        self.config.bot_username = uname
                        return uname
                except Exception:
                    pass
        return ""

    async def _sync_backend_auth(self, bot_token: str = "", owner_id: int | None = None) -> None:
        if not self._running:
            return
        tok = (bot_token or self.config.bot_credential or "").strip()
        oid = owner_id if owner_id is not None else self._resolve_owner_id()
        if tok:
            self.config.bot_credential = tok
        if oid:
            self.config.owner_id = oid
        if not tok and not oid:
            return
        payload: dict[str, Any] = {}
        if tok:
            payload["botToken"] = tok
        if oid:
            payload["ownerId"] = oid
        loop = asyncio.get_running_loop()
        url = f"{self.local_url}/internal/auth-sync"
        try:
            with trusted_scope():
                await loop.run_in_executor(
                    None, _http_local_json, "POST", url, self._ipc_key, payload, 1.5
                )
        except Exception:
            pass

    async def _run_npm_step(self, cwd: Path, *args: str) -> None:
        with trusted_scope():
            npm_bin = shutil.which("npm")
            if not npm_bin:
                raise RuntimeError("`npm` is not installed in PATH")
            proc = await asyncio.create_subprocess_exec(
                npm_bin,
                *args,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                close_fds=True,
            )
            out, _ = await proc.communicate()
            if proc.returncode != 0:
                tail = (out or b"").decode("utf-8", errors="replace")[-600:]
                raise RuntimeError(f"npm {' '.join(args)} failed in {cwd.name}: {tail}")

    async def ensure_built(self) -> None:
        with trusted_scope():
            if not (
                (BACKEND_DIR / "node_modules" / "ws").exists()
                and (BACKEND_DIR / "node_modules" / "jsonwebtoken").exists()
            ):
                log.info("Installing Singularity probe dependencies...")
                await self._run_npm_step(BACKEND_DIR, "install", "--no-audit", "--no-fund")

            if self._frontend_deps_changed():
                log.info("Installing Singularity frontend dependencies...")
                try:
                    await self._run_npm_step(
                        FRONTEND_DIR, "install", "--no-audit", "--no-fund", "--prefer-offline"
                    )
                except Exception as exc:
                    if not (FRONTEND_DIR / "node_modules" / "vite").exists():
                        raise
                    log.warning(
                        "Singularity frontend dependency refresh skipped: %s",
                        exc,
                    )

            log.info("Building Singularity frontend bundle...")
            try:
                await self._run_npm_step(FRONTEND_DIR, "run", "build")
            except Exception as exc:
                if (FRONTEND_DIR / "dist" / "index.html").is_file():
                    log.warning(
                        "Singularity frontend build failed, keeping the previous bundle: %s",
                        exc,
                    )
                    return
                raise

    def _frontend_deps_changed(self) -> bool:
        vite = FRONTEND_DIR / "node_modules" / "vite"
        if not vite.exists():
            return True
        lock = FRONTEND_DIR / "package-lock.json"
        stamp = FRONTEND_DIR / "node_modules" / ".package-lock.json"
        if not lock.is_file() or not stamp.is_file():
            return True
        try:
            return lock.stat().st_mtime > stamp.stat().st_mtime
        except OSError:
            return False

    def _stale_backend_pids(self, port: int) -> list[int]:
        ss_bin = shutil.which("ss")
        if not ss_bin:
            return []
        try:
            proc = subprocess.run(
                [ss_bin, "-ltnpH"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception:
            return []
        pids: list[int] = []
        for line in (proc.stdout or "").splitlines():
            fields = line.split()
            if len(fields) < 4:
                continue
            local = fields[3]
            if not local.rsplit(":", 1)[-1].isdigit():
                continue
            if int(local.rsplit(":", 1)[-1]) != port:
                continue
            for chunk in line.split("pid=")[1:]:
                digits = ""
                for ch in chunk:
                    if ch.isdigit():
                        digits += ch
                    else:
                        break
                if digits:
                    value = int(digits)
                    if value not in pids:
                        pids.append(value)
        return pids

    def _release_stale_backend(self, port: int) -> None:
        for pid in self._stale_backend_pids(port):
            try:
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(
                    "utf-8", errors="replace"
                )
            except OSError:
                continue
            if "server.js" not in cmdline:
                continue
            log.warning(
                "Stopping a stale Singularity backend (pid %s) that still holds port %s",
                pid,
                port,
            )
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                continue
            for _ in range(30):
                if not Path(f"/proc/{pid}").exists():
                    break
                time.sleep(0.1)
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

    async def _spawn_backend(self) -> bool:
        with trusted_scope():
            node_bin = shutil.which("node")
            if not node_bin:
                log.warning("Singularity skipped: `node` binary not found in PATH")
                return False

            self._release_stale_backend(self.config.port)

            if self.runtime is not None:
                fresh = KernelConfig.from_runtime(self.runtime)
                if not self.config.bot_credential and fresh.bot_credential:
                    self.config.bot_credential = fresh.bot_credential
                if not self.config.bot_username and fresh.bot_username:
                    self.config.bot_username = fresh.bot_username
                if not self.config.bot_id and fresh.bot_id:
                    self.config.bot_id = fresh.bot_id
                if self.config.owner_id is None and fresh.owner_id is not None:
                    self.config.owner_id = fresh.owner_id

            child_env = os.environ.copy()
            child_env.update(
                {
                    "NODE_ENV": "production",
                    "HOST": self.config.host,
                    "PORT": str(self.config.port),
                    "BOT_TOKEN": self.config.bot_credential,
                    "OWNER_ID": str(self.config.owner_id or ""),
                    "SESSION_JWT_SECRET": self.config.signing_key,
                    "INTERNAL_IPC_SECRET": self._ipc_key,
                    "DATA_MODE": self.config.data_mode,
                    "DEV_BYPASS": "false",
                    "OBSERVATORY_LOG_PATH": str(self.config.observatory_path or ""),
                }
            )

            self._node_proc = await asyncio.create_subprocess_exec(
                node_bin,
                str(BACKEND_DIR / "server.js"),
                cwd=str(BACKEND_DIR),
                env=child_env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                close_fds=True,
                start_new_session=True,
            )

            loop = asyncio.get_running_loop()
            health_url = f"{self.local_url}/internal/health"
            deadline = loop.time() + 10.0
            while loop.time() < deadline:
                if self._node_proc.returncode is not None:
                    err_bytes = b""
                    if self._node_proc.stderr is not None:
                        err_bytes = await self._node_proc.stderr.read()
                    log.warning(
                        "Singularity probe exited early (%s): %s",
                        self._node_proc.returncode,
                        err_bytes.decode("utf-8", errors="replace")[:300],
                    )
                    return False
                try:
                    status, body = await loop.run_in_executor(
                        None,
                        _http_local_json,
                        "GET",
                        health_url,
                        self._ipc_key,
                        None,
                        1.0,
                    )
                    if status == 200 and body.get("ok"):
                        return True
                except Exception:
                    pass
                await asyncio.sleep(0.15)

            log.warning("Timed out waiting for Singularity probe on %s", self.local_url)
            return False

    async def start(self) -> bool:
        with trusted_scope():
            if not self.config.enabled:
                log.info("Singularity kernel is disabled in config")
                return False
            if self.is_running:
                return True

            try:
                await self.ensure_built()
            except Exception as exc:
                log.warning("Singularity build failed: %s", exc)
                return False

            self._running = True
            ok = await self._spawn_backend()
            if not ok:
                self._running = False
                await self._stop_backend_proc()
                return False

            await self._push_metrics_once()
            self._metrics_task = asyncio.create_task(
                self._metrics_and_watchdog_loop(), name="singularity:observatory-loop"
            )

            if self.config.tunnel_enabled:
                self._last_tunnel_restart_ts = time.monotonic()
                self._tunnel_task = asyncio.create_task(
                    self._start_tunnel_and_provision(), name="singularity:horizon-boot"
                )

            log.info("Singularity probe active on %s", self.local_url)
            return True

    async def _push_metrics_once(self) -> None:
        if not self._running:
            return
        with trusted_scope():
            payload = self.collector.collect(self.runtime)
            loop = asyncio.get_running_loop()
            url = f"{self.local_url}/internal/metrics"
            try:
                await loop.run_in_executor(
                    None, _http_local_json, "POST", url, self._ipc_key, payload, 1.5
                )
            except Exception:
                pass

    async def _check_live_inline_bot_drift(self) -> None:
        if self.runtime is None:
            return
        inline_info = getattr(getattr(self.runtime, "inline", None), "info", None)
        if inline_info is None:
            return
        live_tok = str(getattr(inline_info, "token", "") or "").strip()
        live_usr = str(getattr(inline_info, "username", "") or "").lstrip("@").strip()
        live_bid = int(getattr(inline_info, "bot_id", 0) or 0)

        tok_changed = bool(live_tok and live_tok != self.config.bot_credential)
        usr_changed = bool(
            live_usr
            and self.config.bot_username
            and live_usr.casefold() != self.config.bot_username.casefold()
        )
        if live_usr and not self.config.bot_username:
            self.config.bot_username = live_usr
        if live_bid:
            self.config.bot_id = live_bid

        if tok_changed:
            await self._sync_backend_auth(live_tok, self._resolve_owner_id())
        if usr_changed:
            self.config.bot_username = live_usr
            self.webapp_registered = False
            self.webapp_link = None
            self._synced_tunnel_url = ""

    def _handle_tunnel_url_change(self, new_url: str) -> None:
        if not self._running or not new_url:
            return
        prev = self.tunnel_url
        if prev is None or new_url.rstrip("/") == prev.rstrip("/"):
            return
        self.tunnel_url = new_url
        self._synced_tunnel_url = ""
        log.info("Singularity Horizon tunnel URL rotated: %s", new_url)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._rotation_task is not None and not self._rotation_task.done():
            self._rotation_task.cancel()
        self._rotation_task = loop.create_task(
            self._sync_and_notify(new_url),
            name="singularity:horizon-rotate",
        )

    async def _metrics_and_watchdog_loop(self) -> None:
        try:
            while self._running:
                if self._node_proc is not None and self._node_proc.returncode is not None:
                    log.warning("Singularity probe exited unexpectedly; restarting...")
                    await asyncio.sleep(0.5)
                    if self._running:
                        await self._spawn_backend()

                await self._check_live_inline_bot_drift()
                await self._push_metrics_once()

                now = time.monotonic()
                if (
                    self.config.tunnel_enabled
                    and (self._tunnel_task is None or self._tunnel_task.done())
                    and (self._tunnel is None or not self._tunnel.is_alive)
                    and now - self._last_tunnel_restart_ts >= self.tunnel_restart_cooldown
                ):
                    self._last_tunnel_restart_ts = now
                    if self._tunnel is not None:
                        try:
                            await self._tunnel.stop()
                        except Exception:
                            pass
                        self._tunnel = None
                    log.warning("Singularity Horizon tunnel stopped; reconnecting...")
                    self._tunnel_task = asyncio.create_task(
                        self._start_tunnel_and_provision(),
                        name="singularity:horizon-recover",
                    )
                elif (
                    self.runtime is not None
                    and self.config.auto_botfather
                    and self.tunnel_url
                    and self._synced_tunnel_url.rstrip("/") != self.tunnel_url.rstrip("/")
                    and (self._tunnel_task is None or self._tunnel_task.done())
                    and (self._rotation_task is None or self._rotation_task.done())
                    and now - self._last_sync_attempt_ts >= self.sync_retry_cooldown
                ):
                    self._rotation_task = asyncio.create_task(
                        self._sync_and_notify(self.tunnel_url),
                        name="singularity:botfather-retry",
                    )

                await asyncio.sleep(self.watchdog_interval)
        except asyncio.CancelledError:
            raise

    async def _provision_bot_and_miniapp(self, url: str) -> RelaySyncReport:
        with trusted_scope():
            async with self._provision_lock:
                self._last_sync_attempt_ts = time.monotonic()
                if self.runtime is None:
                    return RelaySyncReport(ok=False, detail="runtime_unavailable")

                bid, buser, btoken = await ensure_inline_bot_ready(self.runtime)
                if buser:
                    self.config.bot_username = buser
                if bid:
                    self.config.bot_id = bid
                if btoken:
                    self.config.bot_credential = btoken
                await self._sync_backend_auth(btoken, self._resolve_owner_id())

                bot_user = buser or self._resolve_bot_username()
                if not bot_user:
                    return RelaySyncReport(ok=False, detail="inline_bot_missing")

                if not self.config.auto_botfather:
                    fallback_link = self.webapp_link or f"https://t.me/{bot_user}/{self.config.short_name}"
                    self._synced_tunnel_url = url
                    return RelaySyncReport(
                        ok=True,
                        app_link=fallback_link,
                        registered=self.webapp_registered,
                        bot_username=bot_user,
                        bot_token=btoken,
                        short_name=self.config.short_name,
                        detail="auto_botfather_disabled",
                    )

                res = await configure_botfather_webapp(
                    self.runtime,
                    bot_user,
                    url,
                    bot_id=self.config.bot_id,
                    short_name=self.config.short_name,
                    title="Hotaru",
                )
                if res.ok:
                    self.webapp_registered = True
                    self._synced_tunnel_url = url
                    if res.short_name:
                        self.config.short_name = res.short_name
                if res.app_link:
                    self.webapp_link = res.app_link
                return res

    async def _sync_and_notify(self, url: str) -> RelaySyncReport:
        if self.runtime is None or not self._running:
            return RelaySyncReport(ok=False, detail="runtime_unavailable")
        sync_res = RelaySyncReport(ok=False, detail="not_attempted")
        for attempt in range(2):
            if not self._running or (self.tunnel_url and self.tunnel_url.rstrip("/") != url.rstrip("/")):
                return sync_res
            sync_res = await self._provision_bot_and_miniapp(url)
            if sync_res.ok or sync_res.detail in ("invalid_url_rejected", "cover_photo_rejected"):
                break
            if attempt == 0:
                await asyncio.sleep(1.5)

        if self._running and self.runtime is not None:
            await sync_owner_menu_button(self.runtime, url)
        return sync_res

    async def _start_tunnel_and_provision(self) -> None:
        try:
            tunnel = HorizonTunnel(
                self.config.port,
                local_host="127.0.0.1",
                cache_dir=self.config.cache_dir,
                on_url_change=self._handle_tunnel_url_change,
            )
            self._tunnel = tunnel
            url = await tunnel.start(timeout=30.0)
            if not url or not self._running:
                return
            self.tunnel_url = url
            log.info("Singularity Horizon tunnel ready: %s", url)

            if self.runtime is not None:
                await self._sync_and_notify(url)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Singularity Horizon setup error: %s", exc)

    async def ensure_ready(self) -> dict[str, Any]:
        with trusted_scope():
            if not self.is_running:
                await self.start()
            if self._tunnel_task is not None and not self._tunnel_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(self._tunnel_task), timeout=25.0)
                except Exception:
                    pass
            elif self._rotation_task is not None and not self._rotation_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(self._rotation_task), timeout=15.0)
                except Exception:
                    pass
            elif (
                self.tunnel_url
                and (not self.webapp_link or self._synced_tunnel_url.rstrip("/") != self.tunnel_url.rstrip("/"))
                and self.runtime is not None
            ):
                await self._provision_bot_and_miniapp(self.tunnel_url)

            bot_user = self._resolve_bot_username()
            db_state = get_sanctuary_app_state(getattr(self.runtime, "state", None))
            app_link = (
                self.webapp_link
                or db_state.app_link
                or (f"https://t.me/{bot_user}/{db_state.short_name or self.config.short_name}" if bot_user else "")
            )
            banner_url = await resolve_inline_banner_url(self.tunnel_url)
            return {
                "running": self.is_running,
                "tunnel_url": self.tunnel_url,
                "app_link": app_link,
                "bot_username": bot_user,
                "short_name": db_state.short_name or self.config.short_name,
                "registered": bool(self.webapp_registered or db_state.registered),
                "banner_url": banner_url,
            }

    async def setup_botfather_now(self) -> RelaySyncReport:
        with trusted_scope():
            if self.runtime is None:
                return RelaySyncReport(ok=False, detail="runtime_unavailable")
            if not self.tunnel_url:
                return RelaySyncReport(ok=False, detail="tunnel_not_ready")
            return await self._provision_bot_and_miniapp(self.tunnel_url)

    async def _stop_backend_proc(self) -> None:
        proc = self._node_proc
        self._node_proc = None
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=4.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except Exception:
                    pass
            except Exception:
                pass

    async def stop(self) -> None:
        self._running = False
        for task in (self._rotation_task, self._tunnel_task, self._metrics_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._rotation_task = None
        self._tunnel_task = None
        self._metrics_task = None

        if self._tunnel is not None:
            await self._tunnel.stop()
            self._tunnel = None
        self.tunnel_url = None

        await self._stop_backend_proc()


SingularityConfig = KernelConfig
SingularityService = SingularityKernel
