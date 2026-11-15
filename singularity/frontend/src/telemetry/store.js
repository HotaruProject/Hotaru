const DEFAULT_MODULES = [
  "config",
  "help",
  "info",
  "loader",
  "logger",
  "power",
  "presets",
  "security",
  "singularity",
  "translator",
  "updater",
];

function createInitialDebugEvents(now = Date.now()) {
  return [
    {
      seq: 1,
      ts: now - 3200,
      level: "info",
      component: "node.probe",
      event: "probe_online",
      detail: "kernel=Linux-x64 mode=read_only",
    },
    {
      seq: 2,
      ts: now - 2100,
      level: "info",
      component: "relay.inline",
      event: "ready_set",
      detail: "bot=verified transport=mtproto",
    },
    {
      seq: 3,
      ts: now - 900,
      level: "debug",
      component: "observatory",
      event: "telemetry_sync",
      detail: "constellations=11 tasks=3",
    },
  ];
}

function createInitialSnapshot(mode) {
  const now = Date.now();
  return {
    seq: 0,
    timestamp: now,
    source: mode,
    collector: mode === "mock" ? "mock.probe" : "node.probe",
    connectionStatus: mode === "mock" ? "mock" : "connecting",
    system: {
      cpuPercent: 16.4,
      cpuCores: 8,
      loadAvg: [0.38, 0.34, 0.29],
      ramUsedBytes: 3.42 * 1024 ** 3,
      ramTotalBytes: 8.0 * 1024 ** 3,
      ramPercent: 42.8,
      swapUsedBytes: 0,
      swapTotalBytes: 2.0 * 1024 ** 3,
      swapPercent: 0,
      diskUsedBytes: 6.4 * 1024 ** 3,
      diskTotalBytes: 32.0 * 1024 ** 3,
      diskPercent: 20.0,
      netRxBytes: 184_520_192,
      netTxBytes: 62_914_560,
      netRxRateBps: 18_432,
      netTxRateBps: 6_144,
      uptimeSeconds: 172_845,
      hostname: "singularity-node",
      kernel: "Linux 6.x",
      arch: "x64",
    },
    userbot: {
      online: true,
      uptimeSeconds: 4_812,
      loadedModulesCount: DEFAULT_MODULES.length,
      loadedModules: [...DEFAULT_MODULES],
      activeTasks: 3,
      mtConnected: true,
      inlineBotReady: true,
      kernelSuspended: false,
      prefix: ".",
    },
    debugEvents: createInitialDebugEvents(now),
  };
}

export class MetricsStore {
  constructor(options = {}) {
    const initialMode =
      String(options.mode || "live").toLowerCase() === "mock" ? "mock" : "live";
    this._mode = initialMode;
    this._sessionToken = options.sessionToken || "";
    this._wsFactory =
      options.wsFactory ||
      ((url) => (typeof WebSocket !== "undefined" ? new WebSocket(url) : null));
    this._fetch =
      options.fetchImpl ||
      (typeof fetch !== "undefined" ? fetch.bind(globalThis) : null);
    this._mockIntervalMs = Number(options.mockIntervalMs ?? 1100);
    this._pollIntervalMs = Number(options.pollIntervalMs ?? 2000);

    this._seq = 0;
    this._snapshot = createInitialSnapshot(initialMode);
    this._listeners = new Set();

    this._ws = null;
    this._mockTimer = null;
    this._reconnectTimer = null;
    this._pollTimer = null;
    this._reconnectAttempts = 0;
    this._destroyed = false;
    this._mockTick = 0;

    if (options.autoStart !== false) {
      this.start();
    }
  }

  getMode() {
    return this._mode;
  }

  getStatus() {
    return this._snapshot.connectionStatus;
  }

  getSnapshot() {
    return this._snapshot;
  }

  setSessionToken(token) {
    this._sessionToken = token || "";
  }

  subscribe(listener) {
    if (typeof listener !== "function") return () => {};
    this._listeners.add(listener);
    return () => {
      this._listeners.delete(listener);
    };
  }

  _notify() {
    const snap = this._snapshot;
    for (const cb of this._listeners) {
      try {
        cb(snap);
      } catch {}
    }
  }

  setMode(nextMode) {
    const normalized =
      String(nextMode || "").toLowerCase() === "mock" ? "mock" : "live";
    if (this._mode === normalized) {
      return this._mode;
    }
    this._stopTransports();
    this._mode = normalized;
    this._snapshot = {
      ...this._snapshot,
      source: normalized,
      connectionStatus: normalized === "mock" ? "mock" : "connecting",
    };
    this._notify();
    if (!this._destroyed) {
      this.start();
    }
    return this._mode;
  }

  start() {
    this._stopTransports();
    if (this._destroyed) return;
    if (this._mode === "mock") {
      this._startMockGenerator();
    } else {
      this._startLiveConnection();
    }
  }

  _stopTransports() {
    if (this._mockTimer) {
      clearInterval(this._mockTimer);
      this._mockTimer = null;
    }
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    if (this._pollTimer) {
      clearInterval(this._pollTimer);
      this._pollTimer = null;
    }
    if (this._ws) {
      const ws = this._ws;
      this._ws = null;
      try {
        ws.onopen = null;
        ws.onmessage = null;
        ws.onerror = null;
        ws.onclose = null;
        ws.close();
      } catch {}
    }
  }

  _startMockGenerator() {
    const mockTemplates = [
      ["debug", "node.probe", "telemetry_tick", "sampling /proc/stat /proc/meminfo"],
      ["info", "observatory", "dispatch_ok", "cmd=dash latency_ms=14"],
      ["debug", "relay.mtproto", "ping_ack", "dc=2 rtt_ms=28"],
      ["info", "sanctuary", "state_checkpoint", "ns=singularity wal=clean"],
    ];

    const stepMock = () => {
      this._mockTick += 1;
      const t = this._mockTick;
      const prev = this._snapshot;
      const cpu = Number(
        (21.5 + Math.sin(t * 0.43) * 9.4 + Math.cos(t * 0.19) * 3.8).toFixed(1)
      );
      const ramPct = Number((44.0 + Math.sin(t * 0.17) * 3.6).toFixed(1));
      const ramTotal = 8 * 1024 ** 3;
      const ramUsed = Math.round((ramPct / 100) * ramTotal);
      const rxRate = Math.round(16_384 + Math.abs(Math.sin(t * 0.61)) * 42_000);
      const txRate = Math.round(5_120 + Math.abs(Math.cos(t * 0.47)) * 19_000);

      this._seq += 1;
      const nextEvents = Array.isArray(prev.debugEvents)
        ? prev.debugEvents.slice(-23)
        : [];
      const [level, component, event, detail] =
        mockTemplates[t % mockTemplates.length];
      nextEvents.push({
        seq: this._seq + 3,
        ts: Date.now(),
        level,
        component,
        event,
        detail: `${detail} cpu=${cpu}%`,
      });

      this._snapshot = {
        seq: this._seq,
        timestamp: Date.now(),
        source: "mock",
        collector: "mock.probe",
        connectionStatus: "mock",
        system: {
          ...prev.system,
          cpuPercent: Math.max(1.2, Math.min(98.5, cpu)),
          loadAvg: [
            Number((0.35 + Math.abs(Math.sin(t * 0.21)) * 0.45).toFixed(2)),
            0.39,
            0.32,
          ],
          ramUsedBytes: ramUsed,
          ramTotalBytes: ramTotal,
          ramPercent: ramPct,
          netRxBytes: prev.system.netRxBytes + rxRate,
          netTxBytes: prev.system.netTxBytes + txRate,
          netRxRateBps: rxRate,
          netTxRateBps: txRate,
          uptimeSeconds: prev.system.uptimeSeconds + 1,
        },
        userbot: {
          ...prev.userbot,
          online: true,
          uptimeSeconds: prev.userbot.uptimeSeconds + 1,
          loadedModulesCount: DEFAULT_MODULES.length,
          loadedModules: [...DEFAULT_MODULES],
          activeTasks: 2 + (t % 3),
        },
        debugEvents: nextEvents,
      };
      this._notify();
    };

    stepMock();
    this._mockTimer = setInterval(stepMock, this._mockIntervalMs);
  }

  _getStoredSessionToken() {
    if (this._sessionToken) return this._sessionToken;
    try {
      if (typeof window !== "undefined") {
        const cfgToken = window.__SINGULARITY_CONFIG__?.sessionToken;
        if (cfgToken) return cfgToken;
        const stored = window.sessionStorage?.getItem("singularity_session_token");
        if (stored) return stored;
      }
    } catch {}
    return "";
  }

  _getStoredInitData() {
    try {
      if (typeof window !== "undefined") {
        if (window.Telegram?.WebApp?.initData) {
          return window.Telegram.WebApp.initData;
        }
        const stored = window.sessionStorage?.getItem("singularity_init_data");
        if (stored) return stored;
        const qp = new URLSearchParams(window.location.search);
        if (qp.get("initData")) return qp.get("initData");
        const hp = new URLSearchParams(window.location.hash.slice(1));
        if (hp.get("tgWebAppData")) return hp.get("tgWebAppData");
      }
    } catch {}
    return "";
  }

  async _tryReverifySession() {
    if (!this._fetch) return false;
    const initData = this._getStoredInitData();
    if (!initData) return false;
    try {
      const resp = await this._fetch("/api/auth/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ initData }),
      });
      if (!resp.ok) return false;
      const data = await resp.json();
      if (data && data.ok && data.sessionToken) {
        this._sessionToken = data.sessionToken;
        try {
          window.sessionStorage?.setItem(
            "singularity_session_token",
            data.sessionToken
          );
        } catch {}
        return true;
      }
    } catch {}
    return false;
  }

  _ingestLivePayload(metrics, status = "connected") {
    if (!metrics || typeof metrics !== "object") return;
    this._seq += 1;
    const prev = this._snapshot;
    this._snapshot = {
      seq: this._seq,
      timestamp: Number(metrics.timestamp || Date.now()),
      source: "live",
      connectionStatus: status,
      collector: metrics.collector || "node.probe",
      system: {
        ...prev.system,
        ...(metrics.system || {}),
      },
      userbot: {
        ...prev.userbot,
        ...(metrics.userbot || {}),
      },
      debugEvents: Array.isArray(metrics.debugEvents)
        ? metrics.debugEvents
        : prev.debugEvents,
    };
    this._notify();
  }

  async _pollHttpOnce() {
    if (!this._fetch || this._mode !== "live" || this._destroyed) return false;
    const headers = {};
    const token = this._getStoredSessionToken();
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }
    try {
      const res = await this._fetch("/api/metrics", {
        method: "GET",
        headers,
        credentials: "include",
      });
      if (res.status === 403) {
        const refreshed = await this._tryReverifySession();
        if (refreshed) {
          return this._pollHttpOnce();
        }
        return false;
      }
      if (!res.ok) return false;
      const body = await res.json();
      if (body && body.ok && body.metrics) {
        const wsOpen = Boolean(this._ws && this._ws.readyState === 1);
        this._ingestLivePayload(
          body.metrics,
          wsOpen ? "connected" : "polling"
        );
        if (!wsOpen && !this._ws) {
          this._openWebSocket();
        }
        return true;
      }
    } catch {}
    return false;
  }

  _buildWsUrl() {
    if (typeof window === "undefined") {
      return "ws://127.0.0.1:8787/ws/metrics";
    }
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = new URL(`${proto}//${window.location.host}/ws/metrics`);
    const token = this._getStoredSessionToken();
    if (token) {
      url.searchParams.set("session", token);
    }
    return url.toString();
  }

  _scheduleReconnect() {
    if (this._destroyed || this._mode !== "live" || this._reconnectTimer) return;
    this._reconnectAttempts += 1;
    const delay = Math.min(
      3000,
      400 * Math.pow(1.5, Math.min(5, this._reconnectAttempts - 1))
    );

    if (this._snapshot.connectionStatus !== "reconnecting") {
      this._snapshot = {
        ...this._snapshot,
        connectionStatus: "reconnecting",
      };
      this._notify();
    }

    this._reconnectTimer = setTimeout(async () => {
      this._reconnectTimer = null;
      if (this._destroyed || this._mode !== "live") return;
      await this._tryReverifySession();
      this._openWebSocket();
    }, delay);
  }

  _startLiveConnection() {
    this._pollHttpOnce();
    this._openWebSocket();
    this._pollTimer = setInterval(() => {
      if (!this._ws || this._ws.readyState !== 1) {
        this._pollHttpOnce();
      }
    }, this._pollIntervalMs);
  }

  _openWebSocket() {
    if (this._destroyed || this._mode !== "live") return;
    let ws;
    try {
      ws = this._wsFactory(this._buildWsUrl());
    } catch {
      this._scheduleReconnect();
      return;
    }
    if (!ws) return;
    this._ws = ws;

    ws.onopen = () => {
      this._reconnectAttempts = 0;
      this._snapshot = {
        ...this._snapshot,
        connectionStatus: "connected",
      };
      this._notify();
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(String(event.data));
        if (
          (msg.type === "hello" || msg.type === "metrics") &&
          msg.metrics
        ) {
          this._ingestLivePayload(msg.metrics, "connected");
        }
      } catch {}
    };

    ws.onerror = () => {};

    ws.onclose = () => {
      if (this._ws === ws) {
        this._ws = null;
      }
      if (!this._destroyed && this._mode === "live") {
        this._scheduleReconnect();
      }
    };
  }

  destroy() {
    this._destroyed = true;
    this._stopTransports();
    this._listeners.clear();
  }
}
