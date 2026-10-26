const crypto = require("node:crypto");

const HEX64_RE = /^[0-9a-f]{64}$/i;
const DIGITS_RE = /^\d+$/;
const WEBAPP_HMAC_DOMAIN = Buffer.from([
  0x57, 0x65, 0x62, 0x41, 0x70, 0x70, 0x44, 0x61, 0x74, 0x61,
]);

function parseInitDataParams(rawInitData) {
  if (typeof rawInitData !== "string") {
    return { ok: false, reason: "missing_init_data" };
  }
  const trimmed = rawInitData.trim();
  if (!trimmed) {
    return { ok: false, reason: "missing_init_data" };
  }

  const params = new URLSearchParams(trimmed);
  const entries = new Map();

  for (const [key, value] of params.entries()) {
    if (!key) {
      return { ok: false, reason: "empty_param_key" };
    }
    if (entries.has(key)) {
      return { ok: false, reason: "duplicate_param" };
    }
    entries.set(key, value);
  }

  return { ok: true, entries, raw: trimmed };
}

function validateTelegramInitData(initData, botToken, options = {}) {
  const {
    ownerId,
    maxAgeSeconds = 3600,
    maxFutureSkewSeconds = 30,
    nowSeconds,
  } = options;

  if (typeof botToken !== "string" || !botToken.trim()) {
    return { ok: false, status: 403, reason: "missing_bot_token" };
  }

  const parsed = parseInitDataParams(initData);
  if (!parsed.ok) {
    return { ok: false, status: 403, reason: parsed.reason };
  }

  const { entries } = parsed;
  const receivedHash = entries.get("hash");
  if (!receivedHash) {
    return { ok: false, status: 403, reason: "missing_hash" };
  }
  if (!HEX64_RE.test(receivedHash)) {
    return { ok: false, status: 403, reason: "malformed_hash" };
  }

  const checkPairs = [];
  for (const [key, value] of entries.entries()) {
    if (key === "hash") continue;
    checkPairs.push([key, value]);
  }
  checkPairs.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));

  const dataCheckString = checkPairs
    .map(([key, value]) => `${key}=${value}`)
    .join("\n");

  const secretKey = crypto
    .createHmac("sha256", WEBAPP_HMAC_DOMAIN)
    .update(botToken)
    .digest();

  const expectedDigest = crypto
    .createHmac("sha256", secretKey)
    .update(dataCheckString)
    .digest();

  const receivedDigest = Buffer.from(receivedHash, "hex");
  if (
    receivedDigest.length !== expectedDigest.length ||
    !crypto.timingSafeEqual(expectedDigest, receivedDigest)
  ) {
    return { ok: false, status: 403, reason: "invalid_signature" };
  }

  const authDateRaw = entries.get("auth_date");
  if (!authDateRaw) {
    return { ok: false, status: 403, reason: "missing_auth_date" };
  }
  if (!DIGITS_RE.test(authDateRaw)) {
    return { ok: false, status: 403, reason: "invalid_auth_date" };
  }

  const authDate = Number(authDateRaw);
  if (!Number.isSafeInteger(authDate) || authDate <= 0) {
    return { ok: false, status: 403, reason: "invalid_auth_date" };
  }

  const now =
    typeof nowSeconds === "number" && Number.isFinite(nowSeconds)
      ? Math.floor(nowSeconds)
      : Math.floor(Date.now() / 1000);
  const age = now - authDate;

  if (age < -maxFutureSkewSeconds) {
    return { ok: false, status: 403, reason: "future_auth_date" };
  }
  if (maxAgeSeconds > 0 && age > maxAgeSeconds) {
    return { ok: false, status: 403, reason: "expired_init_data" };
  }

  const rawUser = entries.get("user");
  if (!rawUser) {
    return { ok: false, status: 403, reason: "missing_user" };
  }

  let user;
  try {
    user = JSON.parse(rawUser);
  } catch {
    return { ok: false, status: 403, reason: "invalid_user_json" };
  }

  if (
    !user ||
    typeof user !== "object" ||
    Array.isArray(user) ||
    typeof user.id !== "number" ||
    !Number.isSafeInteger(user.id) ||
    user.id <= 0
  ) {
    return { ok: false, status: 403, reason: "missing_user_id" };
  }

  if (ownerId === undefined || ownerId === null || String(ownerId).trim() === "") {
    return { ok: false, status: 403, reason: "missing_owner_id" };
  }

  const expectedOwnerId = Number(ownerId);
  if (!Number.isSafeInteger(expectedOwnerId) || expectedOwnerId <= 0) {
    return { ok: false, status: 403, reason: "invalid_owner_id" };
  }

  if (user.id !== expectedOwnerId) {
    return { ok: false, status: 403, reason: "not_owner" };
  }

  return {
    ok: true,
    status: 200,
    user,
    authDate,
    queryId: entries.get("query_id") || null,
    rawInitData: parsed.raw,
  };
}

function buildSignedInitData({
  user,
  botToken,
  authDate = Math.floor(Date.now() / 1000),
  queryId = "AAHdF6IQAAAAAN0XohDhrOrc",
  extraParams = {},
}) {
  const params = new URLSearchParams();
  if (authDate !== null && authDate !== undefined) {
    params.set("auth_date", String(authDate));
  }
  if (queryId !== null && queryId !== undefined) {
    params.set("query_id", String(queryId));
  }
  if (user !== null && user !== undefined) {
    params.set("user", typeof user === "string" ? user : JSON.stringify(user));
  }
  for (const [k, v] of Object.entries(extraParams)) {
    if (v !== undefined && v !== null) {
      params.set(k, String(v));
    }
  }

  const pairs = [];
  for (const [k, v] of params.entries()) {
    if (k === "hash") continue;
    pairs.push([k, v]);
  }
  pairs.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));

  const dataCheckString = pairs.map(([k, v]) => `${k}=${v}`).join("\n");
  const secretKey = crypto
    .createHmac("sha256", WEBAPP_HMAC_DOMAIN)
    .update(botToken)
    .digest();
  const hash = crypto
    .createHmac("sha256", secretKey)
    .update(dataCheckString)
    .digest("hex");

  params.set("hash", hash);
  return params.toString();
}

module.exports = {
  parseInitDataParams,
  validateTelegramInitData,
  buildSignedInitData,
};
