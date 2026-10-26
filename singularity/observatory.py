from __future__ import annotations

import json
import os
import platform
import re
import secrets
import shutil
import socket
import time
from pathlib import Path
from typing import Any

SECRET_TEXT_RE = re.compile(
    r"(\b\d{6,12}:[A-Za-z0-9_-]{30,}\b)"
    r"|(\b[0-9a-f]{32,}\b)"
    r"|(\bsk-[A-Za-z0-9]{16,}\b)"
    r"|(\bbot\d{6,12}:[\w-]{30,}\b)",
    re.IGNORECASE,
)

SECRET_FIELD_MARKERS = (
    "token",
    "password",
    "passwd",
    "secret",
    "api_hash",
    "apikey",
    "auth_key",
    "vault",
    "session",
    "phone",
    "code",
)


def _scrub(value: Any) -> str:
    return SECRET_TEXT_RE.sub("[redacted]", str(value or ""))


def _read_proc_cpu_totals() -> tuple[int, int] | None:
    try:
        with open("/proc/stat", "r", encoding="utf-8") as fh:
            first = fh.readline()
        if not first.startswith("cpu "):
            return None
        parts = [int(x) for x in first.split()[1:]]
        if len(parts) < 4:
            return None
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        total = sum(parts)
        return idle, total
    except OSError:
        return None


def _read_proc_meminfo() -> tuple[int, int, float, int, int, float]:
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if ":" not in line:
                    continue
                k, rest = line.split(":", 1)
                toks = rest.strip().split()
                if toks and toks[0].isdigit():
                    info[k] = int(toks[0]) * 1024
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        used = max(0, total - avail)
        pct = round((used / total) * 100.0, 1) if total > 0 else 0.0
        swap_total = info.get("SwapTotal", 0)
        swap_free = info.get("SwapFree", 0)
        swap_used = max(0, swap_total - swap_free)
        swap_pct = round((swap_used / swap_total) * 100.0, 1) if swap_total > 0 else 0.0
        return used, total, pct, swap_used, swap_total, swap_pct
    except OSError:
        return 0, 0, 0.0, 0, 0, 0.0


def _read_proc_net_dev() -> tuple[int, int]:
    try:
        rx = 0
        tx = 0
        with open("/proc/net/dev", "r", encoding="utf-8") as fh:
            lines = fh.readlines()[2:]
        for line in lines:
            if ":" not in line:
                continue
            iface, data = line.split(":", 1)
            if iface.strip() == "lo":
                continue
            cols = data.split()
            if len(cols) >= 9:
                rx += int(cols[0])
                tx += int(cols[8])
        return rx, tx
    except OSError:
        return 0, 0


def _read_system_uptime() -> int:
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as fh:
            return int(float(fh.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return 0


def _read_observatory_tail(obs_path: Path, max_events: int = 16) -> list[dict[str, Any]]:
    if not obs_path.is_file():
        return []
    try:
        size = obs_path.stat().st_size
        with open(obs_path, "rb") as fh:
            if size > 24576:
                fh.seek(size - 24576)
            raw = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []

    events: list[dict[str, Any]] = []
    skip_keys = {"ts", "time", "level", "component", "event", "tb"}
    for line in raw.splitlines():
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            obj = json.loads(text)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        details: list[str] = []
        for k, v in obj.items():
            if k in skip_keys or v is None:
                continue
            low_k = str(k).lower()
            if any(m in low_k for m in SECRET_FIELD_MARKERS):
                details.append(f"{k}=[redacted]")
            elif isinstance(v, (dict, list)):
                details.append(f"{k}={_scrub(json.dumps(v, ensure_ascii=False))[:56]}")
            else:
                details.append(f"{k}={_scrub(v)[:72]}")
        ts_val = obj.get("ts")
        ts_ms = int(float(ts_val) * 1000) if isinstance(ts_val, (int, float)) and ts_val < 1e11 else int(time.time() * 1000)
        events.append(
            {
                "ts": ts_ms,
                "level": str(obj.get("level") or "info").lower(),
                "component": _scrub(obj.get("component") or "observatory")[:32],
                "event": _scrub(obj.get("event") or "event")[:40],
                "detail": " ".join(details)[:160],
            }
        )
    return events[-max_events:]


class ObservatoryTelemetry:
    def __init__(self) -> None:
        self.instance_id = secrets.token_hex(6)
        self._started_mono = time.monotonic()
        self._prev_cpu = _read_proc_cpu_totals()
        self._last_cpu_pct = 0.0
        rx, tx = _read_proc_net_dev()
        self._prev_net = (rx, tx, time.monotonic())
        self._last_rates = (0, 0)

    def collect(self, runtime: Any | None = None) -> dict[str, Any]:
        now_mono = time.monotonic()

        cur_cpu = _read_proc_cpu_totals()
        if cur_cpu is not None and self._prev_cpu is not None:
            d_idle = cur_cpu[0] - self._prev_cpu[0]
            d_total = cur_cpu[1] - self._prev_cpu[1]
            if d_total > 0:
                busy = max(0.0, min(1.0, 1.0 - (d_idle / d_total)))
                self._last_cpu_pct = round(busy * 100.0, 1)
            self._prev_cpu = cur_cpu

        try:
            load1, load5, load15 = os.getloadavg()
            load_avg = [round(load1, 2), round(load5, 2), round(load15, 2)]
        except OSError:
            load_avg = [0.0, 0.0, 0.0]

        ram_used, ram_total, ram_pct, swap_used, swap_total, swap_pct = _read_proc_meminfo()

        try:
            du = shutil.disk_usage("/")
            disk_used = int(du.used)
            disk_total = int(du.total)
            disk_pct = round((du.used / du.total) * 100.0, 1) if du.total > 0 else 0.0
        except OSError:
            disk_used, disk_total, disk_pct = 0, 0, 0.0

        rx_bytes, tx_bytes = _read_proc_net_dev()
        prev_rx, prev_tx, prev_ts = self._prev_net
        dt = now_mono - prev_ts
        if dt >= 0.25:
            rx_rate = max(0, int((rx_bytes - prev_rx) / dt))
            tx_rate = max(0, int((tx_bytes - prev_tx) / dt))
            self._last_rates = (rx_rate, tx_rate)
            self._prev_net = (rx_bytes, tx_bytes, now_mono)

        userbot_info = self._collect_runtime_info(runtime, now_mono)
        debug_events = self._collect_debug_events(runtime)

        return {
            "timestamp": int(time.time() * 1000),
            "collector": "observatory.bridge",
            "system": {
                "cpuPercent": self._last_cpu_pct,
                "cpuCores": os.cpu_count() or 1,
                "loadAvg": load_avg,
                "ramUsedBytes": ram_used,
                "ramTotalBytes": ram_total,
                "ramPercent": ram_pct,
                "swapUsedBytes": swap_used,
                "swapTotalBytes": swap_total,
                "swapPercent": swap_pct,
                "diskUsedBytes": disk_used,
                "diskTotalBytes": disk_total,
                "diskPercent": disk_pct,
                "netRxBytes": rx_bytes,
                "netTxBytes": tx_bytes,
                "netRxRateBps": self._last_rates[0],
                "netTxRateBps": self._last_rates[1],
                "uptimeSeconds": _read_system_uptime(),
                "hostname": socket.gethostname() or platform.node() or "hotaru-host",
                "kernel": f"{platform.system()} {platform.release()}",
                "arch": platform.machine() or "x64",
            },
            "userbot": userbot_info,
            "debugEvents": debug_events,
        }

    def _collect_debug_events(self, runtime: Any | None) -> list[dict[str, Any]]:
        if runtime is None:
            return []
        obs = getattr(runtime, "observatory", None)
        obs_path = getattr(obs, "path", None) if obs is not None else None
        if isinstance(obs_path, Path):
            return _read_observatory_tail(obs_path)
        fallback = Path("observatory/runtime/events.jsonl")
        if fallback.is_file():
            return _read_observatory_tail(fallback)
        return []

    def _collect_runtime_info(self, runtime: Any | None, now_mono: float) -> dict[str, Any]:
        if runtime is None:
            return {
                "online": False,
                "uptimeSeconds": int(now_mono - self._started_mono),
                "loadedModulesCount": 0,
                "loadedModules": [],
                "activeTasks": 0,
                "mtConnected": False,
                "inlineBotReady": False,
                "kernelSuspended": False,
                "rssBytes": 0,
                "commit": "",
                "prefix": ".",
                "instanceId": self.instance_id,
            }

        started = getattr(runtime, "started_monotonic", None) or getattr(runtime, "started_at", None)
        bot_uptime = (
            max(0, int(now_mono - float(started)))
            if isinstance(started, (int, float))
            else int(now_mono - self._started_mono)
        )

        mod_names: list[str] = []
        modules_mgr = getattr(runtime, "modules", None)
        if modules_mgr is not None:
            items_fn = getattr(modules_mgr, "items", None)
            if callable(items_fn):
                try:
                    for item in items_fn():
                        mid = getattr(item, "module_id", None) or getattr(
                            getattr(getattr(item, "loaded", None), "manifest", None),
                            "module_id",
                            None,
                        )
                        if mid:
                            mod_names.append(str(mid))
                except Exception:
                    pass
        mod_names.sort()

        active_tasks = 0
        tasks_sup = getattr(runtime, "tasks", None)
        if tasks_sup is not None:
            raw_tasks = getattr(tasks_sup, "_tasks", None)
            if isinstance(raw_tasks, (set, dict, list)):
                active_tasks = len(raw_tasks)
        elif isinstance(getattr(runtime, "_active_tasks", None), (set, dict, list)):
            active_tasks = len(runtime._active_tasks)

        app_obj = getattr(runtime, "app", None)
        mt_obj = getattr(app_obj, "mt", None) if app_obj is not None else None
        mt_connected = mt_obj is not None and not bool(getattr(runtime, "closed", False))

        inline_mgr = getattr(runtime, "inline", None)
        inline_ready_ev = getattr(inline_mgr, "ready", None) if inline_mgr is not None else None
        inline_ready = bool(inline_ready_ev.is_set()) if inline_ready_ev is not None and hasattr(inline_ready_ev, "is_set") else False

        kernel = getattr(runtime, "kernel", None)
        suspended = bool(getattr(kernel, "suspended", False)) if kernel is not None else False

        rss_bytes = 0
        mem_fn = getattr(runtime, "get_memory_usage", None)
        if callable(mem_fn):
            try:
                rss_bytes = int(mem_fn() or 0)
            except Exception:
                rss_bytes = 0

        cfg = getattr(runtime, "config", None)
        prefix = str(getattr(cfg, "prefix", ".") or ".")

        return {
            "online": not bool(getattr(runtime, "closed", False)),
            "uptimeSeconds": bot_uptime,
            "loadedModulesCount": len(mod_names),
            "loadedModules": mod_names,
            "activeTasks": active_tasks,
            "mtConnected": mt_connected,
            "inlineBotReady": inline_ready,
            "kernelSuspended": suspended,
            "rssBytes": rss_bytes,
            "commit": str(getattr(runtime, "commit", "") or ""),
            "prefix": prefix,
            "instanceId": getattr(runtime, "instance_id", None) or self.instance_id,
        }


SingularityMetricsCollector = ObservatoryTelemetry
