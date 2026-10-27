export function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(
    units.length - 1,
    Math.floor(Math.log(value) / Math.log(1024))
  );
  const scaled = value / Math.pow(1024, index);
  return `${scaled >= 100 ? scaled.toFixed(0) : scaled.toFixed(1)}${units[index]}`;
}

export function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds || 0)));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = (value) => String(value).padStart(2, "0");
  if (days > 0) {
    return `${days}d ${pad(hours)}h ${pad(minutes)}m`;
  }
  return `${pad(hours)}h ${pad(minutes)}m ${pad(secs)}s`;
}

export function formatClock(timestamp) {
  const raw =
    typeof timestamp === "number"
      ? timestamp < 1e11
        ? Math.round(timestamp * 1000)
        : Math.round(timestamp)
      : Date.now();
  const date = new Date(raw);
  const pad = (value) => String(value).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

export function normalizeLevel(level) {
  const value = String(level || "info").toLowerCase();
  if (value.startsWith("err") || value === "fatal" || value === "crit") return "ERR";
  if (value.startsWith("warn")) return "WRN";
  if (value.startsWith("deb") || value === "trace") return "DBG";
  return "INF";
}

export function truncate(value, limit) {
  const text = String(value ?? "");
  if (text.length <= limit) return text;
  return `${text.slice(0, Math.max(0, limit - 1))}...`;
}

export function buildTelemetryModel(snapshot) {
  const data = snapshot || {};
  const system = data.system || {};
  const userbot = data.userbot || {};
  const loads = Array.isArray(system.loadAvg)
    ? system.loadAvg.map((value) => Number(value || 0).toFixed(2)).join(" ")
    : "0.00 0.00 0.00";

  const cards = [
    {
      key: "cpu",
      label: "CPU",
      value: `${Number(system.cpuPercent || 0).toFixed(1)}%`,
      percent: Number(system.cpuPercent || 0),
      detail: `${system.cpuCores || 1} cores | load ${loads}`,
    },
    {
      key: "memory",
      label: "MEMORY",
      value: `${Number(system.ramPercent || 0).toFixed(1)}%`,
      percent: Number(system.ramPercent || 0),
      detail: `${formatBytes(system.ramUsedBytes)} / ${formatBytes(system.ramTotalBytes)}`,
    },
    {
      key: "storage",
      label: "STORAGE",
      value: `${Number(system.diskPercent || 0).toFixed(1)}%`,
      percent: Number(system.diskPercent || 0),
      detail: `${formatBytes(system.diskUsedBytes)} / ${formatBytes(system.diskTotalBytes)}`,
    },
    {
      key: "network",
      label: "NETWORK",
      value: `${formatBytes(system.netRxRateBps)}/s`,
      percent: null,
      detail: `rx ${formatBytes(system.netRxBytes)} | tx ${formatBytes(system.netTxBytes)}`,
    },
    {
      key: "uptime",
      label: "HOST UPTIME",
      value: formatDuration(system.uptimeSeconds),
      percent: null,
      detail: `${system.kernel || "Linux"} ${system.arch || "x64"} | ${system.hostname || "node"}`,
    },
    {
      key: "hotaru",
      label: "HOTARU",
      value: userbot.online ? "ONLINE" : "STANDBY",
      percent: null,
      detail: `uptime ${formatDuration(userbot.uptimeSeconds)} | modules ${userbot.loadedModulesCount || 0} | tasks ${userbot.activeTasks ?? 0}`,
    },
  ];

  const events = Array.isArray(data.debugEvents) ? data.debugEvents.slice(-60) : [];

  return {
    collector: data.collector || data.source || "node.probe",
    status: data.connectionStatus || "connecting",
    cards,
    events,
    summary: [
      `HOTARU // KERNEL TELEMETRY [${String(data.collector || data.source || "node.probe").toUpperCase()}]`,
      `HOST      : ${system.hostname || "node"} (${system.kernel || "Linux"} ${system.arch || ""})`.trim(),
      `CPU       : ${Number(system.cpuPercent || 0).toFixed(1)}% (${system.cpuCores || 1} cores | load ${loads})`,
      `MEMORY    : ${formatBytes(system.ramUsedBytes)} / ${formatBytes(system.ramTotalBytes)}`,
      `STORAGE   : ${formatBytes(system.diskUsedBytes)} / ${formatBytes(system.diskTotalBytes)}`,
      `NETWORK   : RX ${formatBytes(system.netRxRateBps)}/s | TX ${formatBytes(system.netTxRateBps)}/s`,
      `UPTIME    : host ${formatDuration(system.uptimeSeconds)} | kernel ${formatDuration(userbot.uptimeSeconds)}`,
      `OBSERVATORY // READ-ONLY DEBUG STREAM`,
    ].join("\n"),
  };
}
