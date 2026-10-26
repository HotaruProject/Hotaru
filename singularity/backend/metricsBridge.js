const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const EventEmitter = require("node:events");

const SECRET_RE =
  /(\b\d{6,12}:[A-Za-z0-9_-]{30,}\b)|(\b[0-9a-f]{32,}\b)|(\bsk-[A-Za-z0-9]{16,}\b)|(\bbot\d{6,12}:[\w-]{30,}\b)|(\bBearer\s+[A-Za-z0-9._-]{16,}\b)/gi;

const SECRET_KEYS = [
  "token",
  "password",
  "passwd",
  "secret",
  "api_hash",
  "apikey",
  "auth_key",
  "vault",
  "session",
  "cookie",
  "authorization",
  "phone",
  "code",
];

function scrubText(value) {
  return String(value ?? "").replace(SECRET_RE, "[redacted]");
}

function isSecretKey(key) {
  const lower = String(key || "").toLowerCase();
  return SECRET_KEYS.some((marker) => lower.includes(marker));
}

function readMemInfo() {
  const fallbackTotal = os.totalmem();
  const fallbackFree = os.freemem();
  try {
    const raw = fs.readFileSync("/proc/meminfo", "utf8");
    const map = {};
    for (const line of raw.split("\n")) {
      const m = line.match(/^([A-Za-z0-9_()]+):\s+(\d+)\s*kB$/);
      if (m) {
        map[m[1]] = Number(m[2]) * 1024;
      }
    }
    const total = map.MemTotal || fallbackTotal;
    const avail = map.MemAvailable ?? fallbackFree;
    const used = Math.max(0, total - avail);
    const swapTotal = map.SwapTotal || 0;
    const swapFree = map.SwapFree || 0;
    const swapUsed = Math.max(0, swapTotal - swapFree);
    return {
      ramTotalBytes: total,
      ramUsedBytes: used,
      ramCachedBytes: (map.Cached || 0) + (map.Buffers || 0),
      ramPercent: total > 0 ? Number(((used / total) * 100).toFixed(1)) : 0,
      swapTotalBytes: swapTotal,
      swapUsedBytes: swapUsed,
      swapPercent: swapTotal > 0 ? Number(((swapUsed / swapTotal) * 100).toFixed(1)) : 0,
    };
  } catch {
    const used = Math.max(0, fallbackTotal - fallbackFree);
    return {
      ramTotalBytes: fallbackTotal,
      ramUsedBytes: used,
      ramCachedBytes: 0,
      ramPercent:
        fallbackTotal > 0 ? Number(((used / fallbackTotal) * 100).toFixed(1)) : 0,
      swapTotalBytes: 0,
      swapUsedBytes: 0,
      swapPercent: 0,
    };
  }
}

function readProcLoadTasks() {
  try {
    const raw = fs.readFileSync("/proc/loadavg", "utf8").trim();
    const parts = raw.split(/\s+/);
    if (parts.length >= 4 && parts[3].includes("/")) {
      const [running, total] = parts[3].split("/").map((v) => Number(v) || 0);
      return { tasksRunning: running, tasksTotal: total };
    }
  } catch {}
  return { tasksRunning: 1, tasksTotal: 0 };
}

function readLinuxNetTotals() {
  try {
    const raw = fs.readFileSync("/proc/net/dev", "utf8");
    const lines = raw.split("\n").slice(2);
    let rx = 0;
    let tx = 0;
    const ifaces = [];
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      const [ifacePart, statsPart] = trimmed.split(":");
      if (!statsPart) continue;
      const iface = ifacePart.trim();
      if (iface === "lo") continue;
      const cols = statsPart.trim().split(/\s+/);
      if (cols.length >= 9) {
        const ifaceRx = Number(cols[0]) || 0;
        const ifaceTx = Number(cols[8]) || 0;
        rx += ifaceRx;
        tx += ifaceTx;
        if (ifaceRx > 0 || ifaceTx > 0) {
          ifaces.push(iface);
        }
      }
    }
    return { rxBytes: rx, txBytes: tx, ifaces };
  } catch {
    return { rxBytes: 0, txBytes: 0, ifaces: [] };
  }
}

function readDiskStats(targetPath = "/") {
  try {
    if (typeof fs.statfsSync === "function") {
      const s = fs.statfsSync(targetPath);
      const blockSize = Number(s.bsize) || 4096;
      const total = Number(s.blocks) * blockSize;
      const avail = Number(s.bavail) * blockSize;
      const used = Math.max(0, total - avail);
      const percent = total > 0 ? Number(((used / total) * 100).toFixed(1)) : 0;
      return { usedBytes: used, totalBytes: total, percent };
    }
  } catch {}
  return { usedBytes: 0, totalBytes: 0, percent: 0 };
}

function snapshotCpuTimes() {
  const cpus = os.cpus() || [];
  let idle = 0;
  let total = 0;
  const cores = cpus.map((cpu) => {
    const t = cpu.times;
    const cTotal = t.user + t.nice + t.sys + t.idle + t.irq;
    idle += t.idle;
    total += cTotal;
    return { idle: t.idle, total: cTotal };
  });
  const model = cpus[0]?.model ? String(cpus[0].model).trim() : os.arch();
  return { idle, total, cores: cpus.length || 1, perCore: cores, model };
}

function resolveDefaultObservatoryPath() {
  if (process.env.OBSERVATORY_LOG_PATH) {
    return path.resolve(process.env.OBSERVATORY_LOG_PATH);
  }
  return path.resolve(__dirname, "../../../observatory/runtime/events.jsonl");
}

class NodeTelemetryProbe extends EventEmitter {
  constructor({
    runtimeTtlMs = 6000,
    maxDebugEvents = 48,
    observatoryPath = resolveDefaultObservatoryPath(),
    pollIntervalMs = 1000,
  } = {}) {
    super();
    this.runtimeTtlMs = runtimeTtlMs;
    this.maxDebugEvents = maxDebugEvents;
    this.observatoryPath = observatoryPath;
    this.startedAtMs = Date.now();
    this.lastRuntimePushMs = 0;
    this.lastRuntimePayload = null;
    this._runtimeWasOnline = false;

    this._prevCpu = snapshotCpuTimes();
    this._prevNet = readLinuxNetTotals();
    this._prevNetTs = Date.now();
    this._lastCpuPercent = 0;
    this._lastPerCore = new Array(this._prevCpu.cores).fill(0);
    this._lastRates = { rxBps: 0, txBps: 0 };

    this._debugSeq = 0;
    this._debugBuffer = [];
    this._seenEventKeys = new Set();
    this._obsOffset = 0;
    this._obsInode = null;
    this._lastHeartbeatMs = 0;

    this._recordDebug({
      ts: Date.now(),
      level: "info",
      component: "node.probe",
      event: "probe_online",
      detail: `host=${os.hostname()} kernel=${os.type()}-${os.release()} arch=${os.arch()}`,
    });
    this._pollObservatoryLog();

    this._timer = null;
    if (pollIntervalMs > 0) {
      this._timer = setInterval(() => {
        this._pollObservatoryLog();
        const snap = this.getSnapshot();
        if (Date.now() - this._lastHeartbeatMs >= 8000) {
          this._lastHeartbeatMs = Date.now();
          this._recordDebug({
            ts: Date.now(),
            level: "debug",
            component: "node.probe",
            event: "telemetry_tick",
            detail: `cpu=${snap.system.cpuPercent}% mem=${snap.system.ramPercent}% rx=${snap.system.netRxRateBps}B/s tx=${snap.system.netTxRateBps}B/s`,
          });
        }
      }, pollIntervalMs);
      this._timer.unref?.();
    }
  }

  _recordDebug(entry) {
    if (!entry || typeof entry !== "object") return;
    const tsMs =
      typeof entry.ts === "number"
        ? entry.ts < 1e11
          ? Math.round(entry.ts * 1000)
          : Math.round(entry.ts)
        : Date.now();
    const level = String(entry.level || "info").toLowerCase();
    const component = scrubText(entry.component || "kernel").slice(0, 36);
    const event = scrubText(entry.event || "state").slice(0, 48);
    const detail = scrubText(entry.detail || "").slice(0, 180);
    const dedupKey = `${tsMs}:${component}:${event}:${detail}`;
    if (this._seenEventKeys.has(dedupKey)) return;
    this._seenEventKeys.add(dedupKey);
    if (this._seenEventKeys.size > 256) {
      const first = this._seenEventKeys.values().next().value;
      this._seenEventKeys.delete(first);
    }
    this._debugSeq += 1;
    this._debugBuffer.push({
      seq: this._debugSeq,
      ts: tsMs,
      level,
      component,
      event,
      detail,
    });
    if (this._debugBuffer.length > this.maxDebugEvents) {
      this._debugBuffer.splice(0, this._debugBuffer.length - this.maxDebugEvents);
    }
  }

  _formatObservatoryRecord(obj) {
    if (!obj || typeof obj !== "object") return null;
    const ignored = new Set(["ts", "time", "level", "component", "event", "tb"]);
    const parts = [];
    for (const [k, v] of Object.entries(obj)) {
      if (ignored.has(k) || v === null || v === undefined) continue;
      if (isSecretKey(k)) {
        parts.push(`${k}=[redacted]`);
      } else if (typeof v === "object") {
        parts.push(`${k}=${scrubText(JSON.stringify(v)).slice(0, 64)}`);
      } else {
        parts.push(`${k}=${scrubText(v).slice(0, 80)}`);
      }
    }
    return {
      ts: typeof obj.ts === "number" ? obj.ts : Date.now(),
      level: String(obj.level || "info"),
      component: String(obj.component || obj.module || "observatory"),
      event: String(obj.event || "event"),
      detail: parts.join(" "),
    };
  }

  _pollObservatoryLog() {
    if (!this.observatoryPath) return;
    try {
      if (!fs.existsSync(this.observatoryPath)) return;
      const stat = fs.statSync(this.observatoryPath);
      if (this._obsInode !== stat.ino || stat.size < this._obsOffset) {
        this._obsInode = stat.ino;
        this._obsOffset = Math.max(0, stat.size - 16384);
      }
      if (stat.size <= this._obsOffset) return;
      const len = Math.min(65536, stat.size - this._obsOffset);
      const fd = fs.openSync(this.observatoryPath, "r");
      let chunk = "";
      try {
        const buf = Buffer.alloc(len);
        const read = fs.readSync(fd, buf, 0, len, this._obsOffset);
        this._obsOffset += read;
        chunk = buf.subarray(0, read).toString("utf8");
      } finally {
        fs.closeSync(fd);
      }
      const lines = chunk.split("\n");
      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed.startsWith("{")) continue;
        try {
          const parsed = JSON.parse(trimmed);
          const formatted = this._formatObservatoryRecord(parsed);
          if (formatted) {
            this._recordDebug(formatted);
          }
        } catch {}
      }
    } catch {}
  }

  ingestRuntimeMetrics(payload) {
    if (!payload || typeof payload !== "object") return null;
    const wasOnline = this._runtimeWasOnline;
    this.lastRuntimePushMs = Date.now();
    this.lastRuntimePayload = payload;
    this._runtimeWasOnline = true;

    if (!wasOnline) {
      const inst = payload.userbot?.instanceId || "primary";
      const mods = payload.userbot?.loadedModulesCount ?? 0;
      this._recordDebug({
        ts: Date.now(),
        level: "info",
        component: "hotaru.bridge",
        event: "runtime_linked",
        detail: `instance=${inst} modules=${mods}`,
      });
    }

    if (Array.isArray(payload.debugEvents)) {
      for (const ev of payload.debugEvents) {
        this._recordDebug(ev);
      }
    }

    this._pollObservatoryLog();
    const snap = this.getSnapshot();
    this.emit("update", snap);
    return snap;
  }

  _sampleNativeSystem() {
    const curCpu = snapshotCpuTimes();
    const dTotal = curCpu.total - this._prevCpu.total;
    const dIdle = curCpu.idle - this._prevCpu.idle;
    if (dTotal > 0) {
      const busy = Math.max(0, Math.min(1, 1 - dIdle / dTotal));
      this._lastCpuPercent = Number((busy * 100).toFixed(1));
      this._lastPerCore = curCpu.perCore.map((core, idx) => {
        const prev = this._prevCpu.perCore[idx];
        if (!prev) return 0;
        const cdTotal = core.total - prev.total;
        const cdIdle = core.idle - prev.idle;
        if (cdTotal <= 0) return this._lastPerCore[idx] || 0;
        return Number((Math.max(0, Math.min(1, 1 - cdIdle / cdTotal)) * 100).toFixed(1));
      });
      this._prevCpu = curCpu;
    }

    const mem = readMemInfo();
    const procTasks = readProcLoadTasks();
    const disk = readDiskStats("/");
    const nodeMem = process.memoryUsage();

    const now = Date.now();
    const curNet = readLinuxNetTotals();
    const dtSec = (now - this._prevNetTs) / 1000;
    if (dtSec >= 0.25) {
      this._lastRates = {
        rxBps: Math.max(0, Math.round((curNet.rxBytes - this._prevNet.rxBytes) / dtSec)),
        txBps: Math.max(0, Math.round((curNet.txBytes - this._prevNet.txBytes) / dtSec)),
      };
      this._prevNet = curNet;
      this._prevNetTs = now;
    }

    const loadAvg = os.loadavg().map((v) => Number(v.toFixed(2)));

    return {
      cpuPercent: this._lastCpuPercent,
      cpuCores: curCpu.cores,
      perCorePercent: this._lastPerCore,
      cpuModel: curCpu.model,
      loadAvg,
      tasksRunning: procTasks.tasksRunning,
      tasksTotal: procTasks.tasksTotal,
      ramUsedBytes: mem.ramUsedBytes,
      ramTotalBytes: mem.ramTotalBytes,
      ramCachedBytes: mem.ramCachedBytes,
      ramPercent: mem.ramPercent,
      swapUsedBytes: mem.swapUsedBytes,
      swapTotalBytes: mem.swapTotalBytes,
      swapPercent: mem.swapPercent,
      nodeRssBytes: nodeMem.rss || 0,
      diskUsedBytes: disk.usedBytes,
      diskTotalBytes: disk.totalBytes,
      diskPercent: disk.percent,
      netRxBytes: curNet.rxBytes,
      netTxBytes: curNet.txBytes,
      netRxRateBps: this._lastRates.rxBps,
      netTxRateBps: this._lastRates.txBps,
      netIfaces: curNet.ifaces,
      uptimeSeconds: Math.floor(os.uptime()),
      hostname: os.hostname(),
      kernel: `${os.type()} ${os.release()}`,
      arch: os.arch(),
    };
  }

  getSnapshot() {
    const now = Date.now();
    const runtimeFresh =
      this.lastRuntimePayload !== null &&
      now - this.lastRuntimePushMs <= this.runtimeTtlMs;

    if (!runtimeFresh && this._runtimeWasOnline) {
      this._runtimeWasOnline = false;
      this._recordDebug({
        ts: now,
        level: "warn",
        component: "hotaru.bridge",
        event: "runtime_detached",
        detail: "switching to autonomous node.probe telemetry",
      });
    }

    const nativeSys = this._sampleNativeSystem();
    const rtSys =
      runtimeFresh && this.lastRuntimePayload.system
        ? this.lastRuntimePayload.system
        : null;
    const rtBot =
      this.lastRuntimePayload && this.lastRuntimePayload.userbot
        ? this.lastRuntimePayload.userbot
        : null;

    const system = rtSys
      ? {
          ...nativeSys,
          cpuPercent: Number(rtSys.cpuPercent ?? nativeSys.cpuPercent),
          cpuCores: Number(rtSys.cpuCores ?? nativeSys.cpuCores),
          loadAvg: Array.isArray(rtSys.loadAvg) ? rtSys.loadAvg : nativeSys.loadAvg,
          ramUsedBytes: Number(rtSys.ramUsedBytes ?? nativeSys.ramUsedBytes),
          ramTotalBytes: Number(rtSys.ramTotalBytes ?? nativeSys.ramTotalBytes),
          ramPercent: Number(rtSys.ramPercent ?? nativeSys.ramPercent),
          diskUsedBytes: Number(rtSys.diskUsedBytes ?? nativeSys.diskUsedBytes),
          diskTotalBytes: Number(rtSys.diskTotalBytes ?? nativeSys.diskTotalBytes),
          diskPercent: Number(rtSys.diskPercent ?? nativeSys.diskPercent),
          netRxBytes: Number(rtSys.netRxBytes ?? nativeSys.netRxBytes),
          netTxBytes: Number(rtSys.netTxBytes ?? nativeSys.netTxBytes),
          netRxRateBps: Number(rtSys.netRxRateBps ?? nativeSys.netRxRateBps),
          netTxRateBps: Number(rtSys.netTxRateBps ?? nativeSys.netTxRateBps),
          uptimeSeconds: Number(rtSys.uptimeSeconds ?? nativeSys.uptimeSeconds),
          hostname: String(rtSys.hostname || nativeSys.hostname),
        }
      : nativeSys;

    const userbot = {
      online: runtimeFresh ? Boolean(rtBot?.online ?? true) : false,
      uptimeSeconds: runtimeFresh
        ? Number(rtBot?.uptimeSeconds ?? 0)
        : Math.floor((now - this.startedAtMs) / 1000),
      loadedModulesCount: Number(rtBot?.loadedModulesCount ?? 0),
      loadedModules: Array.isArray(rtBot?.loadedModules) ? rtBot.loadedModules : [],
      activeTasks: Number(rtBot?.activeTasks ?? 0),
      mtConnected: runtimeFresh ? Boolean(rtBot?.mtConnected ?? true) : false,
      inlineBotReady: runtimeFresh ? Boolean(rtBot?.inlineBotReady ?? false) : false,
      kernelSuspended: Boolean(rtBot?.kernelSuspended ?? false),
      rssBytes: Number(rtBot?.rssBytes ?? 0),
      commit: String(rtBot?.commit || ""),
      prefix: String(rtBot?.prefix || "."),
      instanceId: rtBot?.instanceId || null,
    };

    return {
      timestamp: now,
      collector: runtimeFresh ? "node.probe+hotaru" : "node.probe",
      system,
      userbot,
      debugEvents: this._debugBuffer.slice(-24),
    };
  }

  close() {
    if (this._timer) {
      clearInterval(this._timer);
      this._timer = null;
    }
  }
}

module.exports = {
  NodeTelemetryProbe,
  MetricsBridge: NodeTelemetryProbe,
};
