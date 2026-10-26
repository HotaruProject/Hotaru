export function formatBytes(bytes) {
  const n = Number(bytes || 0);
  if (!Number.isFinite(n) || n <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const idx = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  const val = n / Math.pow(1024, idx);
  return `${val >= 100 ? val.toFixed(0) : val.toFixed(1)}${units[idx]}`;
}

export function formatDuration(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds || 0)));
  const days = Math.floor(s / 86400);
  const hrs = Math.floor((s % 86400) / 3600);
  const mins = Math.floor((s % 3600) / 60);
  const secs = s % 60;
  const pad = (v) => String(v).padStart(2, "0");
  if (days > 0) {
    return `${days}d ${pad(hrs)}h ${pad(mins)}m`;
  }
  return `${pad(hrs)}h ${pad(mins)}m ${pad(secs)}s`;
}

export function formatTimeClock(ts) {
  const ms =
    typeof ts === "number"
      ? ts < 1e11
        ? Math.round(ts * 1000)
        : Math.round(ts)
      : Date.now();
  const d = new Date(ms);
  const pad = (v) => String(v).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function normalizeLevel(lvl) {
  const s = String(lvl || "info").toLowerCase();
  if (s.startsWith("err") || s === "fatal" || s === "crit") return "ERR";
  if (s.startsWith("warn")) return "WRN";
  if (s.startsWith("deb") || s === "trace") return "DBG";
  return "INF";
}

function truncateText(str, maxChars) {
  const s = String(str ?? "");
  if (s.length <= maxChars) return s;
  return `${s.slice(0, Math.max(0, maxChars - 1))}…`;
}

export function formatTelemetrySummaryText(snapshot) {
  if (!snapshot) return "HOTARU // KERNEL TELEMETRY :: INITIALIZING";
  const sys = snapshot.system || {};
  const bot = snapshot.userbot || {};
  const loads = Array.isArray(sys.loadAvg)
    ? sys.loadAvg.map((v) => Number(v || 0).toFixed(2)).join(" ")
    : "0.00 0.00 0.00";
  const events = Array.isArray(snapshot.debugEvents)
    ? snapshot.debugEvents.slice(-8)
    : [];
  const logLines = events.map((ev) => {
    const lvl = normalizeLevel(ev.level);
    const comp = String(ev.component || "kernel");
    const name = String(ev.event || "tick");
    const detail = ev.detail ? ` ${ev.detail}` : "";
    return `${formatTimeClock(ev.ts)} [${lvl}] ${comp} :: ${name}${detail}`;
  });

  return [
    `HOTARU // KERNEL TELEMETRY [${String(snapshot.collector || snapshot.source || "node.probe").toUpperCase()}]`,
    `HOST      : ${sys.hostname || "node"} (${sys.kernel || "Linux"} ${sys.arch || ""})`.trim(),
    `CPU       : ${Number(sys.cpuPercent || 0).toFixed(1)}% (${sys.cpuCores || 1} cores | load ${loads})`,
    `MEMORY    : ${formatBytes(sys.ramUsedBytes)} / ${formatBytes(sys.ramTotalBytes)} (${Number(sys.ramPercent || 0).toFixed(1)}%)`,
    `STORAGE   : ${formatBytes(sys.diskUsedBytes)} / ${formatBytes(sys.diskTotalBytes)} (${Number(sys.diskPercent || 0).toFixed(1)}%)`,
    `NETWORK   : RX ${formatBytes(sys.netRxRateBps)}/s | TX ${formatBytes(sys.netTxRateBps)}/s`,
    `UPTIME    : host ${formatDuration(sys.uptimeSeconds)} | kernel ${formatDuration(bot.uptimeSeconds)}`,
    `KERNEL    : ${bot.online ? "ONLINE" : "STANDBY"} | constellations ${bot.loadedModulesCount || 0} | tasks ${bot.activeTasks ?? 0}`,
    `OBSERVATORY // READ-ONLY DEBUG STREAM`,
    ...(logLines.length > 0 ? logLines : ["-- awaiting observatory telemetry --"]),
  ].join("\n");
}

function drawProgressGauge(ctx, x, y, w, h, pct) {
  const clamped = Math.max(0, Math.min(100, Number(pct || 0)));
  ctx.fillStyle = "rgba(120, 175, 205, 0.14)";
  ctx.fillRect(x, y, w, h);

  const fillW = Math.round((clamped / 100) * w);
  if (fillW > 0) {
    ctx.fillStyle =
      clamped >= 88
        ? "rgba(255, 130, 130, 0.92)"
        : clamped >= 72
          ? "rgba(255, 210, 122, 0.90)"
          : "rgba(138, 238, 255, 0.90)";
    ctx.fillRect(x, y, fillW, h);
  }
}

export class TelemetryProjector {
  constructor(options = {}) {
    this.width = options.width || 1024;
    this.height = options.height || 680;
    this._snapshot = null;
    this._listeners = new Set();
    this._dirty = true;
    this._lastRenderMs = 0;

    this.canvas =
      typeof document !== "undefined" ? document.createElement("canvas") : null;
    if (this.canvas) {
      this.canvas.width = this.width;
      this.canvas.height = this.height;
      this._ctx = this.canvas.getContext("2d");
    } else {
      this._ctx = null;
    }
  }

  onChange(listener) {
    if (typeof listener !== "function") return () => {};
    this._listeners.add(listener);
    return () => {
      this._listeners.delete(listener);
    };
  }

  _emit() {
    const state = this.getState();
    for (const cb of this._listeners) {
      try {
        cb(state);
      } catch {}
    }
  }

  updateSnapshot(snapshot) {
    this._snapshot = snapshot;
    this._dirty = true;
    this._emit();
  }

  getState() {
    const snap = this._snapshot;
    return {
      source: snap?.source || "live",
      collector: snap?.collector || "node.probe",
      connectionStatus: snap?.connectionStatus || "connecting",
      online: Boolean(snap?.userbot?.online),
      text: formatTelemetrySummaryText(snap),
      snapshot: snap,
    };
  }

  renderToCanvas(nowMs = Date.now()) {
    if (!this._ctx) return false;
    if (!this._dirty && nowMs - this._lastRenderMs < 500) {
      return false;
    }
    this._dirty = false;
    this._lastRenderMs = nowMs;

    const ctx = this._ctx;
    const w = this.width;
    const h = this.height;
    const snap = this._snapshot || {};
    const sys = snap.system || {};
    const bot = snap.userbot || {};

    ctx.clearRect(0, 0, w, h);

    ctx.fillStyle = "rgba(5, 10, 18, 0.90)";
    ctx.fillRect(0, 0, w, h);

    ctx.strokeStyle = "rgba(130, 224, 252, 0.48)";
    ctx.lineWidth = 2;
    ctx.strokeRect(3, 3, w - 6, h - 6);

    ctx.strokeStyle = "rgba(182, 244, 255, 0.94)";
    ctx.lineWidth = 3;
    const cLen = 22;
    const brackets = [
      [2, cLen, 2, 2, cLen, 2],
      [w - cLen, 2, w - 2, 2, w - 2, cLen],
      [2, h - cLen, 2, h - 2, cLen, h - 2],
      [w - cLen, h - 2, w - 2, h - 2, w - 2, h - cLen],
    ];
    for (const [x0, y0, x1, y1, x2, y2] of brackets) {
      ctx.beginPath();
      ctx.moveTo(x0, y0);
      ctx.lineTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
    }

    ctx.fillStyle = "rgba(148, 228, 255, 0.06)";
    ctx.fillRect(4, 4, w - 8, 52);
    ctx.strokeStyle = "rgba(130, 224, 252, 0.28)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(4, 56);
    ctx.lineTo(w - 4, 56);
    ctx.stroke();

    const connStatus = String(snap.connectionStatus || "connecting");
    const isHealthy =
      connStatus === "connected" || connStatus === "mock" || connStatus === "polling";
    ctx.fillStyle = isHealthy ? "#86f7d2" : "#ffd479";
    ctx.beginPath();
    ctx.arc(28, 30, 5.5, 0, Math.PI * 2);
    ctx.fill();

    const hostLabel = truncateText(sys.hostname || "singularity-node", 24).toUpperCase();
    ctx.font = '600 18px "JetBrains Mono", "SFMono-Regular", Consolas, monospace';
    ctx.fillStyle = "#e8f8ff";
    ctx.fillText(`HOTARU // KERNEL TELEMETRY · ${hostLabel}`, 46, 36);

    const badge = "NODE.PROBE // READ-ONLY";
    ctx.font = '600 14px "JetBrains Mono", "SFMono-Regular", Consolas, monospace';
    const badgeW = ctx.measureText(badge).width + 22;
    const badgeX = w - badgeW - 22;
    ctx.strokeStyle = "rgba(138, 238, 255, 0.42)";
    ctx.strokeRect(badgeX, 16, badgeW, 28);
    ctx.fillStyle = "rgba(176, 240, 255, 0.92)";
    ctx.fillText(badge, badgeX + 11, 35);

    const loads = Array.isArray(sys.loadAvg)
      ? sys.loadAvg.map((v) => Number(v || 0).toFixed(2)).join(" ")
      : "0.00 0.00 0.00";
    const cpuPct = Number(sys.cpuPercent || 0);
    const ramPct = Number(sys.ramPercent || 0);
    const diskPct = Number(sys.diskPercent || 0);

    const cells = [
      {
        tag: "CPU // COMPUTE",
        primary: `${cpuPct.toFixed(1)}%`,
        pct: cpuPct,
        sub: `cores:${sys.cpuCores || 1} · load:${loads}`,
      },
      {
        tag: "MEM // ALLOCATION",
        primary: `${ramPct.toFixed(1)}%`,
        pct: ramPct,
        sub: `${formatBytes(sys.ramUsedBytes)} / ${formatBytes(sys.ramTotalBytes)} · swp:${formatBytes(sys.swapUsedBytes)}`,
      },
      {
        tag: "DSK // SANCTUARY FS",
        primary: `${diskPct.toFixed(1)}%`,
        pct: diskPct,
        sub: `${formatBytes(sys.diskUsedBytes)} / ${formatBytes(sys.diskTotalBytes)} · root /`,
      },
      {
        tag: "NET // RELAY THROUGHPUT",
        primary: `▼ ${formatBytes(sys.netRxRateBps)}/s   ▲ ${formatBytes(sys.netTxRateBps)}/s`,
        pct: null,
        sub: `rx:${formatBytes(sys.netRxBytes)} · tx:${formatBytes(sys.netTxBytes)}`,
      },
      {
        tag: "HOST // KERNEL UPTIME",
        primary: formatDuration(sys.uptimeSeconds),
        pct: null,
        sub: truncateText(`${sys.kernel || "Linux"} · ${sys.arch || "x64"}`, 42),
      },
      {
        tag: "HOTARU // CONSTELLATIONS",
        primary: bot.online
          ? `ONLINE · ${formatDuration(bot.uptimeSeconds)}`
          : `PROBE ONLY · ${formatDuration(bot.uptimeSeconds)}`,
        pct: null,
        sub: `mt:${bot.mtConnected ? "linked" : "idle"} · mods:${bot.loadedModulesCount || 0} · tasks:${bot.activeTasks ?? 0}`,
      },
    ];

    const gridLeft = 24;
    const gridTop = 70;
    const colGap = 20;
    const rowGap = 10;
    const cellW = Math.floor((w - gridLeft * 2 - colGap) / 2);
    const cellH = 82;

    for (let i = 0; i < cells.length; i += 1) {
      const col = i % 2;
      const row = Math.floor(i / 2);
      const cx = gridLeft + col * (cellW + colGap);
      const cy = gridTop + row * (cellH + rowGap);
      const item = cells[i];

      ctx.fillStyle = "rgba(11, 20, 32, 0.72)";
      ctx.fillRect(cx, cy, cellW, cellH);
      ctx.strokeStyle = "rgba(130, 210, 242, 0.22)";
      ctx.lineWidth = 1;
      ctx.strokeRect(cx + 0.5, cy + 0.5, cellW - 1, cellH - 1);

      ctx.font = '600 13px "JetBrains Mono", "SFMono-Regular", Consolas, monospace';
      ctx.fillStyle = "rgba(148, 208, 236, 0.78)";
      ctx.fillText(item.tag, cx + 14, cy + 21);

      if (item.pct !== null) {
        ctx.font = '700 20px "JetBrains Mono", "SFMono-Regular", Consolas, monospace';
        ctx.fillStyle = "#f2fbff";
        const valText = item.primary;
        const valW = ctx.measureText(valText).width;
        ctx.fillText(valText, cx + cellW - valW - 14, cy + 24);
        drawProgressGauge(ctx, cx + 14, cy + 33, cellW - 28, 10, item.pct);
        ctx.font = '500 14px "JetBrains Mono", "SFMono-Regular", Consolas, monospace';
        ctx.fillStyle = "rgba(198, 224, 240, 0.84)";
        ctx.fillText(truncateText(item.sub, 46), cx + 14, cy + 66);
      } else {
        ctx.font = '600 17px "JetBrains Mono", "SFMono-Regular", Consolas, monospace';
        ctx.fillStyle = "#eaf9ff";
        ctx.fillText(truncateText(item.primary, 38), cx + 14, cy + 46);
        ctx.font = '500 14px "JetBrains Mono", "SFMono-Regular", Consolas, monospace';
        ctx.fillStyle = "rgba(186, 214, 232, 0.80)";
        ctx.fillText(truncateText(item.sub, 46), cx + 14, cy + 68);
      }
    }

    const streamHeaderY = 346;
    ctx.strokeStyle = "rgba(130, 224, 252, 0.26)";
    ctx.beginPath();
    ctx.moveTo(24, streamHeaderY);
    ctx.lineTo(w - 24, streamHeaderY);
    ctx.stroke();

    ctx.font = '600 15px "JetBrains Mono", "SFMono-Regular", Consolas, monospace';
    ctx.fillStyle = "rgba(164, 234, 255, 0.92)";
    ctx.fillText("OBSERVATORY // LIVE DEBUG STREAM", 24, streamHeaderY + 24);

    const streamMeta = `src:${snap.collector || snap.source || "node.probe"} · status:${connStatus}`;
    ctx.font = '500 13px "JetBrains Mono", "SFMono-Regular", Consolas, monospace';
    const metaW = ctx.measureText(streamMeta).width;
    ctx.fillStyle = "rgba(154, 198, 222, 0.72)";
    ctx.fillText(streamMeta, w - metaW - 24, streamHeaderY + 24);

    const logBoxX = 24;
    const logBoxY = streamHeaderY + 36;
    const logBoxW = w - 48;
    const logBoxH = h - logBoxY - 22;

    ctx.fillStyle = "rgba(4, 9, 15, 0.84)";
    ctx.fillRect(logBoxX, logBoxY, logBoxW, logBoxH);
    ctx.strokeStyle = "rgba(124, 206, 238, 0.22)";
    ctx.strokeRect(logBoxX + 0.5, logBoxY + 0.5, logBoxW - 1, logBoxH - 1);

    const rawEvents = Array.isArray(snap.debugEvents) ? snap.debugEvents : [];
    const visibleRows = rawEvents.slice(-8);

    ctx.font = '500 15px "JetBrains Mono", "SFMono-Regular", Consolas, monospace';
    if (visibleRows.length === 0) {
      ctx.fillStyle = "rgba(156, 196, 218, 0.65)";
      ctx.fillText("-- awaiting observatory debug telemetry --", logBoxX + 16, logBoxY + 34);
    } else {
      const rowPitch = 31;
      for (let idx = 0; idx < visibleRows.length; idx += 1) {
        const ev = visibleRows[idx];
        const ry = logBoxY + 28 + idx * rowPitch;
        const clock = formatTimeClock(ev.ts);
        const lvl = normalizeLevel(ev.level);
        const comp = truncateText(ev.component || "kernel", 15).padEnd(15, " ");
        const name = truncateText(ev.event || "event", 18);
        const detail = ev.detail ? ` · ${ev.detail}` : "";

        ctx.fillStyle = "rgba(140, 178, 204, 0.74)";
        ctx.fillText(clock, logBoxX + 14, ry);

        ctx.fillStyle =
          lvl === "ERR"
            ? "#ff8b8b"
            : lvl === "WRN"
              ? "#ffd479"
              : lvl === "DBG"
                ? "rgba(158, 204, 230, 0.78)"
                : "#86f2ff";
        ctx.fillText(lvl, logBoxX + 102, ry);

        ctx.fillStyle = "rgba(176, 224, 248, 0.90)";
        ctx.fillText(comp, logBoxX + 148, ry);

        const bodyText = truncateText(`${name}${detail}`, 58);
        ctx.fillStyle = "#e6f6ff";
        ctx.fillText(bodyText, logBoxX + 304, ry);
      }
    }

    ctx.fillStyle = "rgba(170, 232, 255, 0.022)";
    for (let sy = 6; sy < h - 6; sy += 4) {
      ctx.fillRect(6, sy, w - 12, 1);
    }

    return true;
  }
}

export const SystemSummaryProjector = TelemetryProjector;
export const formatSystemSummaryText = formatTelemetrySummaryText;
