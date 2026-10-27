require("dotenv").config();

const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const jwt = require("jsonwebtoken");
const { WebSocketServer, WebSocket } = require("ws");

const { validateTelegramInitData } = require("./telegramAuth");
const { NodeTelemetryProbe } = require("./metricsBridge");

const SESSION_COOKIE = "singularity_session";
const LEGACY_COOKIE = "observatory_session";
const SESSION_TTL_SECONDS = 12 * 3600;
const MAX_JSON_BODY_BYTES = 64 * 1024;
const STATIC_BANNER_PATH = path.resolve(__dirname, "../assets/WebApp.jpg");

const MIME_BY_EXT = {
  ".html": "text/html; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".mjs": "application/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".ico": "image/x-icon",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
};

function parseCookiesHeader(cookieHeader) {
  const out = {};
  if (!cookieHeader || typeof cookieHeader !== "string") return out;
  for (const part of cookieHeader.split(";")) {
    const idx = part.indexOf("=");
    if (idx <= 0) continue;
    const k = part.slice(0, idx).trim();
    const v = part.slice(idx + 1).trim();
    try {
      out[k] = decodeURIComponent(v);
    } catch {
      out[k] = v;
    }
  }
  return out;
}

function isLoopbackAddress(remoteAddress) {
  if (!remoteAddress) return false;
  return (
    remoteAddress === "127.0.0.1" ||
    remoteAddress === "::1" ||
    remoteAddress === "::ffff:127.0.0.1"
  );
}

function safeEqualStrings(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || !a || !b) return false;
  const bufA = Buffer.from(a, "utf8");
  const bufB = Buffer.from(b, "utf8");
  if (bufA.length !== bufB.length) return false;
  return crypto.timingSafeEqual(bufA, bufB);
}

function isHttpsRequest(req) {
  if (req.socket && req.socket.encrypted) return true;
  const xfProto = String(req.headers["x-forwarded-proto"] || "").toLowerCase();
  if (xfProto.includes("https")) return true;
  const cfVisitor = String(req.headers["cf-visitor"] || "").toLowerCase();
  if (cfVisitor.includes("https")) return true;
  return false;
}

function sendJson(res, statusCode, payload) {
  const body = Buffer.from(JSON.stringify(payload), "utf8");
  res.statusCode = statusCode;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Content-Length", String(body.length));
  res.end(body);
}

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let total = 0;
    let aborted = false;

    req.on("data", (chunk) => {
      if (aborted) return;
      total += chunk.length;
      if (total > MAX_JSON_BODY_BYTES) {
        aborted = true;
        reject(new Error("payload_too_large"));
        return;
      }
      chunks.push(chunk);
    });

    req.on("end", () => {
      if (aborted) return;
      if (total === 0) {
        resolve({});
        return;
      }
      try {
        const raw = Buffer.concat(chunks).toString("utf8");
        resolve(JSON.parse(raw));
      } catch {
        resolve({});
      }
    });

    req.on("error", (err) => {
      if (!aborted) reject(err);
    });
  });
}

function renderAuthGateHtml() {
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <link rel="icon" href="data:," />
  <title>403 // HOTARU SINGULARITY</title>
  <style>
    html, body {
      margin: 0;
      padding: 0;
      width: 100%;
      height: 100%;
      background: #030407;
      color: rgba(215, 224, 234, 0.86);
      font-family: "JetBrains Mono", "SFMono-Regular", Consolas, monospace;
      display: flex;
      align-items: center;
      justify-content: center;
      overflow: hidden;
    }
    .gate {
      max-width: 420px;
      padding: 24px 28px;
      border: 1px solid rgba(185, 205, 225, 0.18);
      background: rgba(8, 11, 16, 0.92);
      text-align: left;
      letter-spacing: 0.06em;
    }
    .code {
      font-size: 11px;
      color: rgba(160, 185, 210, 0.6);
      text-transform: uppercase;
      margin-bottom: 10px;
    }
    .title {
      font-size: 14px;
      font-weight: 600;
      color: #e4ebf2;
      margin-bottom: 8px;
    }
    .desc {
      font-size: 12px;
      line-height: 1.55;
      color: rgba(185, 198, 214, 0.68);
    }
  </style>
</head>
<body>
  <div class="gate" data-testid="auth-gate">
    <div class="code">HTTP 403 // SANCTUARY_GUARD</div>
    <div class="title" id="gate-title">KERNEL TELEMETRY LOCKED - OWNER ONLY</div>
    <div class="desc" id="gate-desc">Launch via your Hotaru inline Mini App button inside Telegram.</div>
  </div>
  <script>
    (function () {
      function extractInitData() {
        try {
          if (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData) {
            return window.Telegram.WebApp.initData;
          }
        } catch (_) {}
        try {
          var hash = window.location.hash ? window.location.hash.slice(1) : "";
          if (hash) {
            var hp = new URLSearchParams(hash);
            var tgData = hp.get("tgWebAppData") || hp.get("initData");
            if (tgData) return tgData;
          }
        } catch (_) {}
        return "";
      }

      var attempted = false;
      function tryVerify() {
        if (attempted) return;
        var initData = extractInitData();
        if (!initData) return;
        attempted = true;
        var titleEl = document.getElementById("gate-title");
        var descEl = document.getElementById("gate-desc");
        if (titleEl) titleEl.textContent = "VERIFYING RELAY SIGNATURE...";
        if (descEl) descEl.textContent = "Validating owner initData HMAC...";

        try {
          sessionStorage.setItem("singularity_init_data", initData);
        } catch (_) {}

        fetch("/api/auth/verify", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ initData: initData })
        })
          .then(function (r) { return r.json().then(function (d) { return { status: r.status, data: d }; }); })
          .then(function (res) {
            if (res.status === 200 && res.data && res.data.ok) {
              try {
                if (res.data.sessionToken) {
                  sessionStorage.setItem("singularity_session_token", res.data.sessionToken);
                }
              } catch (_) {}
              var nextUrl = "/?session=" + encodeURIComponent(res.data.sessionToken || "") + (window.location.hash || "");
              window.location.replace(nextUrl);
              return;
            }
            if (titleEl) titleEl.textContent = "403 // ACCESS DENIED";
            if (descEl) descEl.textContent = "Owner signature required.";
          })
          .catch(function () {
            if (titleEl) titleEl.textContent = "403 // VERIFICATION FAILED";
          });
      }

      tryVerify();
      if (!attempted && (window.TelegramWebviewProxy || window.TelegramWebview)) {
        var sdk = document.createElement("script");
        sdk.src = "https://telegram.org/js/telegram-web-app.js";
        sdk.async = true;
        sdk.onload = tryVerify;
        sdk.onerror = function () {};
        document.head.appendChild(sdk);
      }
      var polls = 0;
      var timer = setInterval(function () {
        polls += 1;
        tryVerify();
        if (attempted || polls >= 12) clearInterval(timer);
      }, 120);
    })();
  </script>
</body>
</html>`;
}

function createSingularityServer(options = {}) {
  const port = Number(options.port ?? process.env.PORT ?? 8787);
  const host = String(options.host ?? process.env.HOST ?? "127.0.0.1");
  let botToken = String(options.botToken ?? process.env.BOT_TOKEN ?? "").trim();
  const ownerIdRaw = options.ownerId ?? process.env.OWNER_ID ?? process.env.ADMIN_ID ?? "";
  let ownerId = String(ownerIdRaw).trim() ? Number(ownerIdRaw) : null;
  const sessionSecret =
    String(options.sessionSecret ?? process.env.SESSION_JWT_SECRET ?? "").trim() ||
    crypto.randomBytes(32).toString("hex");
  const internalSecret =
    String(options.internalSecret ?? process.env.INTERNAL_IPC_SECRET ?? "").trim();
  const dataMode =
    String(options.dataMode ?? process.env.DATA_MODE ?? "live").toLowerCase() === "mock"
      ? "mock"
      : "live";
  const initDataMaxAgeSeconds = Number(
    options.initDataMaxAgeSeconds ?? process.env.INITDATA_MAX_AGE_SECONDS ?? 3600
  );
  const shotDurationMs = Number(
    options.shotDurationMs ?? process.env.SHOT_DURATION_MS ?? 2400
  );
  const devBypass =
    Boolean(options.devBypass ?? process.env.DEV_BYPASS === "true") &&
    process.env.NODE_ENV !== "production";
  const distDir = path.resolve(
    options.distDir || path.resolve(__dirname, "../frontend/dist")
  );

  const metricsBridge = options.metricsBridge || new NodeTelemetryProbe();
  const startedAt = Date.now();

  function issueSessionToken(user) {
    return jwt.sign(
      {
        uid: Number(user.id),
        username: user.username || null,
        first_name: user.first_name || null,
      },
      sessionSecret,
      { expiresIn: SESSION_TTL_SECONDS }
    );
  }

  function setSessionCookie(req, res, token) {
    const secure = isHttpsRequest(req);
    const parts = [
      `${SESSION_COOKIE}=${encodeURIComponent(token)}`,
      `Max-Age=${SESSION_TTL_SECONDS}`,
      "Path=/",
      "HttpOnly",
      secure ? "SameSite=None" : "SameSite=Lax",
    ];
    if (secure) parts.push("Secure");
    res.setHeader("Set-Cookie", parts.join("; "));
  }

  function clearSessionCookie(res) {
    res.setHeader(
      "Set-Cookie",
      `${SESSION_COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax`
    );
  }

  function verifySessionToken(token) {
    if (!token || typeof token !== "string") {
      return { ok: false, status: 403, reason: "missing_session" };
    }
    try {
      const decoded = jwt.verify(token, sessionSecret);
      if (!decoded || typeof decoded.uid !== "number") {
        return { ok: false, status: 403, reason: "invalid_session" };
      }
      if (!devBypass) {
        if (!ownerId || !Number.isSafeInteger(ownerId) || ownerId <= 0) {
          return { ok: false, status: 403, reason: "missing_owner_id" };
        }
        if (decoded.uid !== ownerId) {
          return { ok: false, status: 403, reason: "not_owner" };
        }
      }
      return {
        ok: true,
        status: 200,
        user: {
          id: decoded.uid,
          username: decoded.username || null,
          first_name: decoded.first_name || null,
        },
        sessionToken: token,
      };
    } catch {
      return { ok: false, status: 403, reason: "invalid_session" };
    }
  }

  function extractExplicitInitDataFromHttp(req, searchParams) {
    const headerInit = req.headers["x-telegram-init-data"];
    if (typeof headerInit === "string") {
      return { present: true, value: headerInit };
    }
    const authHeader = req.headers.authorization;
    if (typeof authHeader === "string" && /^tma\s+/i.test(authHeader)) {
      return { present: true, value: authHeader.replace(/^tma\s+/i, "") };
    }
    const qInit = searchParams.get("initData");
    if (qInit !== null) {
      return { present: true, value: qInit };
    }
    const qTg = searchParams.get("tgWebAppData");
    if (qTg !== null) {
      return { present: true, value: qTg };
    }
    return { present: false, value: "" };
  }

  function extractSessionTokenFromHttp(req, searchParams) {
    const authHeader = req.headers.authorization;
    if (typeof authHeader === "string" && /^bearer\s+/i.test(authHeader)) {
      return authHeader.replace(/^bearer\s+/i, "").trim();
    }
    if (typeof req.headers["x-singularity-session"] === "string") {
      return req.headers["x-singularity-session"].trim();
    }
    if (typeof req.headers["x-observatory-session"] === "string") {
      return req.headers["x-observatory-session"].trim();
    }
    const qSession = searchParams.get("session");
    if (qSession && qSession.trim()) {
      return qSession.trim();
    }
    const cookies = parseCookiesHeader(req.headers.cookie);
    if (typeof cookies[SESSION_COOKIE] === "string") {
      return cookies[SESSION_COOKIE];
    }
    if (typeof cookies[LEGACY_COOKIE] === "string") {
      return cookies[LEGACY_COOKIE];
    }
    return "";
  }

  function authenticateHttpRequest(req, res, searchParams) {
    const explicit = extractExplicitInitDataFromHttp(req, searchParams);
    if (explicit.present) {
      const check = validateTelegramInitData(explicit.value, botToken, {
        ownerId,
        maxAgeSeconds: initDataMaxAgeSeconds,
      });
      if (!check.ok) {
        return { ok: false, status: 403, reason: check.reason, explicitFailed: true };
      }
      const token = issueSessionToken(check.user);
      if (res) {
        setSessionCookie(req, res, token);
      }
      return {
        ok: true,
        status: 200,
        user: check.user,
        sessionToken: token,
      };
    }

    const sessionToken = extractSessionTokenFromHttp(req, searchParams);
    if (sessionToken) {
      const verified = verifySessionToken(sessionToken);
      const cookies = parseCookiesHeader(req.headers.cookie);
      if (verified.ok && res && !cookies[SESSION_COOKIE]) {
        setSessionCookie(req, res, sessionToken);
      }
      return verified;
    }

    if (devBypass) {
      const devUser = { id: ownerId || 1, username: "dev_owner", first_name: "Owner" };
      return {
        ok: true,
        status: 200,
        user: devUser,
        sessionToken: issueSessionToken(devUser),
      };
    }

    return { ok: false, status: 403, reason: "missing_auth", explicitFailed: false };
  }

  function checkInternalLoopback(req) {
    const hasProxyHeaders = Boolean(
      req.headers["cf-connecting-ip"] ||
        req.headers["x-forwarded-for"] ||
        req.headers["cf-ray"]
    );
    const providedSecret = String(req.headers["x-internal-secret"] || "");
    return (
      !hasProxyHeaders &&
      isLoopbackAddress(req.socket?.remoteAddress) &&
      Boolean(internalSecret) &&
      safeEqualStrings(providedSecret, internalSecret)
    );
  }

  function tryServeStaticAsset(pathname, res) {
    if (!pathname || pathname === "/" || pathname === "/index.html") {
      return false;
    }
    let decoded;
    try {
      decoded = decodeURIComponent(pathname);
    } catch {
      return false;
    }
    const rel = decoded.replace(/^\/+/, "");
    if (!rel || rel.includes("\0")) return false;
    const candidate = path.resolve(distDir, rel);
    if (candidate !== distDir && !candidate.startsWith(distDir + path.sep)) {
      return false;
    }
    let st;
    try {
      st = fs.statSync(candidate);
    } catch {
      return false;
    }
    if (!st.isFile()) return false;
    const ext = path.extname(candidate).toLowerCase();
    const mime = MIME_BY_EXT[ext] || "application/octet-stream";
    const data = fs.readFileSync(candidate);
    res.statusCode = 200;
    res.setHeader("Content-Type", mime);
    res.setHeader("Content-Length", String(data.length));
    res.end(data);
    return true;
  }

  async function app(req, res) {
    let parsedUrl;
    try {
      parsedUrl = new URL(req.url || "/", "http://localhost");
    } catch {
      return sendJson(res, 400, { ok: false, error: "bad_request" });
    }
    const pathname = parsedUrl.pathname || "/";
    const searchParams = parsedUrl.searchParams;
    const method = String(req.method || "GET").toUpperCase();

    if (method === "GET" && pathname === "/public/WebApp.jpg") {
      if (!fs.existsSync(STATIC_BANNER_PATH)) {
        res.statusCode = 404;
        return res.end();
      }
      const jpg = fs.readFileSync(STATIC_BANNER_PATH);
      res.statusCode = 200;
      res.setHeader("Cache-Control", "public, max-age=3600");
      res.setHeader("Content-Type", "image/jpeg");
      res.setHeader("Content-Length", String(jpg.length));
      return res.end(jpg);
    }

    if (pathname.startsWith("/internal/")) {
      if (!checkInternalLoopback(req)) {
        return sendJson(res, 403, { ok: false, error: "forbidden" });
      }
      if (method === "GET" && pathname === "/internal/health") {
        return sendJson(res, 200, {
          ok: true,
          service: "hotaru-singularity-probe",
          uptime: process.uptime(),
          hasBotToken: Boolean(botToken),
          ownerId,
        });
      }
      if (method === "POST" && pathname === "/internal/auth-sync") {
        let body = {};
        try {
          body = await readJsonBody(req);
        } catch {
          return sendJson(res, 413, { ok: false, error: "payload_too_large" });
        }
        if (body && typeof body === "object") {
          if (typeof body.botToken === "string" && body.botToken.trim()) {
            botToken = body.botToken.trim();
          }
          if (body.ownerId !== undefined && body.ownerId !== null) {
            const nextOwner = Number(body.ownerId);
            if (Number.isSafeInteger(nextOwner) && nextOwner > 0) {
              ownerId = nextOwner;
            }
          }
        }
        return sendJson(res, 200, {
          ok: true,
          hasBotToken: Boolean(botToken),
          ownerId,
        });
      }
      if (method === "POST" && pathname === "/internal/metrics") {
        let body = {};
        try {
          body = await readJsonBody(req);
        } catch {
          return sendJson(res, 413, { ok: false, error: "payload_too_large" });
        }
        metricsBridge.ingestRuntimeMetrics(body);
        return sendJson(res, 200, { ok: true });
      }
      return sendJson(res, 404, { ok: false, error: "not_found" });
    }

    if (method === "POST" && pathname === "/api/auth/verify") {
      let body = {};
      try {
        body = await readJsonBody(req);
      } catch {
        return sendJson(res, 413, { ok: false, error: "payload_too_large" });
      }
      const rawInitData =
        (body && typeof body.initData === "string" ? body.initData : null) ??
        (typeof req.headers["x-telegram-init-data"] === "string"
          ? req.headers["x-telegram-init-data"]
          : "");

      if (devBypass && !rawInitData) {
        const devUser = { id: ownerId || 1, username: "dev_owner", first_name: "Owner" };
        const token = issueSessionToken(devUser);
        setSessionCookie(req, res, token);
        return sendJson(res, 200, {
          ok: true,
          user: devUser,
          sessionToken: token,
          config: { dataMode, shotDurationMs },
        });
      }

      const result = validateTelegramInitData(rawInitData, botToken, {
        ownerId,
        maxAgeSeconds: initDataMaxAgeSeconds,
      });

      if (!result.ok) {
        return sendJson(res, 403, {
          ok: false,
          error: "forbidden",
          reason: result.reason,
        });
      }

      const sessionToken = issueSessionToken(result.user);
      setSessionCookie(req, res, sessionToken);
      return sendJson(res, 200, {
        ok: true,
        user: result.user,
        sessionToken,
        config: { dataMode, shotDurationMs },
      });
    }

    if (method === "GET" && pathname === "/api/health") {
      return sendJson(res, 200, {
        ok: true,
        uptime_seconds: Math.floor((Date.now() - startedAt) / 1000),
      });
    }

    if (pathname.startsWith("/api/")) {
      const auth = authenticateHttpRequest(req, res, searchParams);
      if (!auth.ok) {
        return sendJson(res, 403, {
          ok: false,
          error: "forbidden",
          reason: auth.reason,
        });
      }
      if (method === "POST" && pathname === "/api/auth/logout") {
        clearSessionCookie(res);
        return sendJson(res, 200, { ok: true });
      }
      if (method === "GET" && pathname === "/api/config") {
        return sendJson(res, 200, {
          ok: true,
          dataMode,
          shotDurationMs,
          user: auth.user,
        });
      }
      if (
        method === "GET" &&
        (pathname === "/api/metrics" || pathname === "/api/dashboard")
      ) {
        return sendJson(res, 200, {
          ok: true,
          dataMode,
          metrics: metricsBridge.getSnapshot(),
        });
      }
      return sendJson(res, 404, { ok: false, error: "not_found" });
    }

    const auth = authenticateHttpRequest(req, res, searchParams);
    if (!auth.ok) {
      const wantsHtml =
        method === "GET" &&
        (pathname === "/" || pathname === "/index.html") &&
        !auth.explicitFailed &&
        String(req.headers.accept || "").includes("text/html");

      if (wantsHtml) {
        const gateBuf = Buffer.from(renderAuthGateHtml(), "utf8");
        res.statusCode = 403;
        res.setHeader("Cache-Control", "no-store");
        res.setHeader("Content-Type", "text/html; charset=utf-8");
        res.setHeader("Content-Length", String(gateBuf.length));
        return res.end(gateBuf);
      }

      return sendJson(res, 403, {
        ok: false,
        error: "forbidden",
        reason: auth.reason,
      });
    }

    if (method === "GET" && tryServeStaticAsset(pathname, res)) {
      return undefined;
    }

    if (method !== "GET" && method !== "HEAD") {
      return sendJson(res, 405, { ok: false, error: "method_not_allowed" });
    }

    const indexPath = path.join(distDir, "index.html");
    if (!fs.existsSync(indexPath)) {
      return sendJson(res, 503, { ok: false, error: "frontend_not_built" });
    }

    let html = fs.readFileSync(indexPath, "utf8");
    const safeJwt =
      typeof auth.sessionToken === "string" && /^[A-Za-z0-9._-]+$/.test(auth.sessionToken)
        ? auth.sessionToken
        : "";

    if (safeJwt) {
      html = html.replace(
        /(src|href)="(\/assets\/[^"?]+)"/g,
        `$1="$2?session=${safeJwt}"`
      );
    }
    const b64Config = Buffer.from(
      JSON.stringify({
        dataMode,
        shotDurationMs,
        sessionToken: safeJwt || null,
      }),
      "utf8"
    ).toString("base64");
    html = html.replace(
      "</head>",
      `<meta name="singularity-cfg" content="${b64Config}" /></head>`
    );

    const htmlBuf = Buffer.from(html, "utf8");
    res.statusCode = 200;
    res.setHeader("Cache-Control", "no-store");
    res.setHeader("Content-Type", "text/html; charset=utf-8");
    res.setHeader("Content-Length", String(htmlBuf.length));
    return res.end(method === "HEAD" ? undefined : htmlBuf);
  }

  const httpServer = http.createServer((req, res) => {
    Promise.resolve(app(req, res)).catch(() => {
      if (!res.headersSent) {
        sendJson(res, 500, { ok: false, error: "internal_error" });
      } else {
        res.end();
      }
    });
  });

  const wss = new WebSocketServer({ noServer: true });

  function authenticateUpgradeRequest(req) {
    let parsedUrl;
    try {
      parsedUrl = new URL(req.url || "/", "http://localhost");
    } catch {
      return { ok: false, status: 403, reason: "invalid_url" };
    }

    const headerInit = req.headers["x-telegram-init-data"];
    const authHeader = req.headers.authorization;
    const queryInit =
      parsedUrl.searchParams.get("initData") ??
      parsedUrl.searchParams.get("tgWebAppData");

    let explicitInit = null;
    if (typeof headerInit === "string") {
      explicitInit = headerInit;
    } else if (typeof authHeader === "string" && /^tma\s+/i.test(authHeader)) {
      explicitInit = authHeader.replace(/^tma\s+/i, "");
    } else if (queryInit !== null) {
      explicitInit = queryInit;
    }

    if (explicitInit !== null) {
      const check = validateTelegramInitData(explicitInit, botToken, {
        ownerId,
        maxAgeSeconds: initDataMaxAgeSeconds,
      });
      if (!check.ok) {
        return { ok: false, status: 403, reason: check.reason };
      }
      return { ok: true, status: 200, user: check.user, pathname: parsedUrl.pathname };
    }

    const cookies = parseCookiesHeader(req.headers.cookie);
    const bearerToken =
      typeof authHeader === "string" && /^bearer\s+/i.test(authHeader)
        ? authHeader.replace(/^bearer\s+/i, "").trim()
        : "";
    const sessionToken =
      bearerToken ||
      parsedUrl.searchParams.get("session") ||
      cookies[SESSION_COOKIE] ||
      cookies[LEGACY_COOKIE] ||
      "";

    if (sessionToken) {
      const verified = verifySessionToken(sessionToken);
      if (!verified.ok) return verified;
      return { ...verified, pathname: parsedUrl.pathname };
    }

    if (devBypass) {
      return {
        ok: true,
        status: 200,
        user: { id: ownerId || 1, username: "dev_owner" },
        pathname: parsedUrl.pathname,
      };
    }

    return { ok: false, status: 403, reason: "missing_auth" };
  }

  function rejectUpgradeWith403(socket, reason) {
    const body = JSON.stringify({
      ok: false,
      error: "forbidden",
      reason: reason || "forbidden",
    });
    const response =
      "HTTP/1.1 403 Forbidden\r\n" +
      "Content-Type: application/json; charset=utf-8\r\n" +
      `Content-Length: ${Buffer.byteLength(body)}\r\n` +
      "Connection: close\r\n\r\n" +
      body;
    try {
      socket.write(response);
    } catch {}
    socket.destroy();
  }

  httpServer.on("upgrade", (req, socket, head) => {
    const auth = authenticateUpgradeRequest(req);
    if (!auth.ok) {
      rejectUpgradeWith403(socket, auth.reason);
      return;
    }
    if (auth.pathname !== "/ws/metrics") {
      rejectUpgradeWith403(socket, "invalid_ws_path");
      return;
    }

    wss.handleUpgrade(req, socket, head, (ws) => {
      ws.ownerUser = auth.user;
      wss.emit("connection", ws, req);
    });
  });

  function broadcastMetrics(snapshot) {
    if (wss.clients.size === 0) return;
    const message = JSON.stringify({
      type: "metrics",
      dataMode,
      metrics: snapshot || metricsBridge.getSnapshot(),
    });
    for (const client of wss.clients) {
      if (client.readyState === WebSocket.OPEN) {
        client.send(message);
      }
    }
  }

  const onBridgeUpdate = (snap) => broadcastMetrics(snap);
  metricsBridge.on("update", onBridgeUpdate);

  const broadcastTimer = setInterval(() => {
    if (wss.clients.size > 0) {
      broadcastMetrics(metricsBridge.getSnapshot());
    }
  }, 1000);
  broadcastTimer.unref?.();

  wss.on("connection", (ws) => {
    ws.send(
      JSON.stringify({
        type: "hello",
        dataMode,
        shotDurationMs,
        metrics: metricsBridge.getSnapshot(),
      })
    );

    ws.on("message", (raw) => {
      try {
        const msg = JSON.parse(String(raw));
        if (msg && msg.type === "ping") {
          ws.send(JSON.stringify({ type: "pong", ts: Date.now() }));
        } else if (msg && msg.type === "pull") {
          ws.send(
            JSON.stringify({
              type: "metrics",
              dataMode,
              metrics: metricsBridge.getSnapshot(),
            })
          );
        }
      } catch {}
    });
  });

  function close() {
    return new Promise((resolve) => {
      clearInterval(broadcastTimer);
      metricsBridge.off("update", onBridgeUpdate);
      if (typeof metricsBridge.close === "function") {
        metricsBridge.close();
      }
      for (const client of wss.clients) {
        try {
          client.terminate();
        } catch {}
      }
      wss.close(() => {
        if (!httpServer.listening) {
          resolve();
          return;
        }
        httpServer.close(() => resolve());
      });
    });
  }

  return {
    app,
    httpServer,
    wss,
    metricsBridge,
    config: { port, host, ownerId, dataMode, shotDurationMs, distDir },
    close,
  };
}

if (require.main === module) {
  const srv = createSingularityServer();
  const { port, host } = srv.config;
  srv.httpServer.listen(port, host, () => {
    process.stdout.write(`[singularity] probe listening on http://${host}:${port}\n`);
  });

  const shutdown = () => {
    srv.close().finally(() => process.exit(0));
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
}

module.exports = {
  createSingularityServer,
  createDashboardServer: createSingularityServer,
  SESSION_COOKIE,
};
