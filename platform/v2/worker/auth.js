// Owner Access and read-only API tokens are independent authentication lanes.
// No credentials, JWT contents, authorization headers or raw errors are logged.
const ENCODER = new TextEncoder();
const MAX_JWT_BYTES = 16 * 1024;
const MAX_JWKS_BYTES = 64 * 1024;
const MAX_JWKS_KEYS = 32;
const MAX_ISSUERS = 4;
const JWKS_TTL_MS = 300_000;
const UNKNOWN_KEY_REFRESH_MS = 30_000;
const JWKS_TIMEOUT_MS = 3_000;
const JWKS_FAILURE_COOLDOWN_MS = 10_000;
const OWNER_SCOPES = Object.freeze(["rag:search", "archive:read", "admin:read"]);
const TOKEN_SCOPES = new Set(["rag:search", "archive:read"]);
const keySets = new Map();
const pendingKeySets = new Map();
const failedKeySets = new Map();

export class AuthError extends Error {
  constructor(code, status) {
    super(code);
    this.name = "AuthError";
    this.code = code;
    this.status = status;
  }
}

function denied(code = "access_denied", status = 403) {
  throw new AuthError(code, status);
}

function configured(condition) {
  if (!condition) denied("access_not_configured", 503);
}

function base64url(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/.test(value) || value.length % 4 === 1) {
    denied("access_token_invalid");
  }
  try {
    const padded = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
    const decoded = atob(padded);
    const bytes = Uint8Array.from(decoded, char => char.charCodeAt(0));
    // Reject noncanonical padding bits as well as malformed encodings.
    if (btoa(decoded).replace(/=/g, "").replace(/\+/g, "-").replace(/\//g, "_") !== value) {
      denied("access_token_invalid");
    }
    return bytes;
  } catch (error) {
    if (error instanceof AuthError) throw error;
    denied("access_token_invalid");
  }
}

function jsonPart(value) {
  try {
    const parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(base64url(value)));
    if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") denied("access_token_invalid");
    return parsed;
  } catch (error) {
    if (error instanceof AuthError) throw error;
    denied("access_token_invalid");
  }
}

function issuerOrigin(value) {
  let url;
  try { url = new URL(value); } catch { denied("access_not_configured", 503); }
  configured(url.protocol === "https:" && /^[a-z0-9-]+\.cloudflareaccess\.com$/.test(url.hostname)
    && !url.username && !url.password && !url.port && url.pathname === "/" && !url.search && !url.hash);
  return url.origin;
}

function ownerEmails(raw) {
  configured(typeof raw === "string" && raw.length <= 8192);
  let values;
  try { values = JSON.parse(raw); } catch { denied("access_not_configured", 503); }
  configured(Array.isArray(values) && values.length > 0 && values.length <= 16);
  const result = new Set();
  for (const value of values) {
    configured(typeof value === "string" && value.length <= 254
      && /^[A-Za-z0-9.!#$%&'+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/.test(value)
      && !value.includes("*") && value === value.trim());
    const normalized = value.toLowerCase();
    configured(!result.has(normalized));
    result.add(normalized);
  }
  return result;
}

async function boundedJwks(issuer) {
  const controller = new AbortController();
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new AuthError("access_keys_unavailable", 503));
    }, JWKS_TIMEOUT_MS);
  });
  const download = async () => {
    const response = await fetch(`${issuer}/cdn-cgi/access/certs`, {
      headers: { Accept: "application/json" }, redirect: "error", credentials: "omit", signal: controller.signal,
    });
    if (!response.ok || !response.body) denied("access_keys_unavailable", 503);
    const declared = response.headers.get("content-length");
    if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > MAX_JWKS_BYTES)) {
      denied("access_keys_unavailable", 503);
    }
    const type = response.headers.get("content-type") || "";
    if (!/^application\/json(?:\s*;|$)/i.test(type)) denied("access_keys_unavailable", 503);
    const chunks = [];
    let size = 0;
    const reader = response.body.getReader();
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > MAX_JWKS_BYTES) denied("access_keys_unavailable", 503);
        chunks.push(value);
      }
    } finally {
      // Do not await cancellation: a hostile/unavailable upstream stream may
      // never acknowledge it, but the request still must honor its timeout.
      reader.cancel().catch(() => {});
      reader.releaseLock();
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    let value;
    try { value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)); }
    catch { denied("access_keys_unavailable", 503); }
    if (!Array.isArray(value?.keys) || !value.keys.length || value.keys.length > MAX_JWKS_KEYS) {
      denied("access_keys_unavailable", 503);
    }
    const keys = new Map();
    for (const key of value.keys) {
      if (key?.kty !== "RSA") continue;
      if (typeof key.kid !== "string" || !key.kid.length || key.kid.length > 128 || keys.has(key.kid)
        || (key.alg && key.alg !== "RS256") || (key.use && key.use !== "sig")
        || (key.key_ops && (!Array.isArray(key.key_ops) || !key.key_ops.includes("verify")))) {
        denied("access_keys_unavailable", 503);
      }
      let modulus, exponent;
      try { modulus = base64url(key.n); exponent = base64url(key.e); }
      catch { denied("access_keys_unavailable", 503); }
      if (modulus.length < 256 || modulus.length > 1024 || exponent.length < 1 || exponent.length > 8) {
        denied("access_keys_unavailable", 503);
      }
      keys.set(key.kid, { jwk: key, imported: null });
    }
    if (!keys.size) denied("access_keys_unavailable", 503);
    return keys;
  };
  try { return await Promise.race([download(), timeout]); }
  catch (error) {
    if (error instanceof AuthError) throw error;
    denied("access_keys_unavailable", 503);
  } finally { clearTimeout(timer); }
}

async function refreshKeys(issuer, unknownRefreshAt = null) {
  if (pendingKeySets.has(issuer)) return pendingKeySets.get(issuer);
  if ((failedKeySets.get(issuer) || 0) > Date.now() || pendingKeySets.size >= MAX_ISSUERS) {
    denied("access_keys_unavailable", 503);
  }
  const pending = (async () => {
    const keys = await boundedJwks(issuer);
    const entry = { keys, expiresAt: Date.now() + JWKS_TTL_MS, unknownRefreshAt };
    keySets.delete(issuer);
    while (keySets.size >= MAX_ISSUERS) keySets.delete(keySets.keys().next().value);
    keySets.set(issuer, entry);
    failedKeySets.delete(issuer);
    return entry;
  })();
  pendingKeySets.set(issuer, pending);
  try { return await pending; }
  catch (error) {
    failedKeySets.delete(issuer);
    while (failedKeySets.size >= MAX_ISSUERS) failedKeySets.delete(failedKeySets.keys().next().value);
    failedKeySets.set(issuer, Date.now() + JWKS_FAILURE_COOLDOWN_MS);
    throw error;
  }
  finally { pendingKeySets.delete(issuer); }
}

async function accessKey(issuer, kid) {
  let entry = keySets.get(issuer);
  let justFetched = false;
  if (!entry || entry.expiresAt <= Date.now()) {
    entry = await refreshKeys(issuer);
    justFetched = true;
  }
  if (!entry.keys.has(kid) && !justFetched
      && (entry.unknownRefreshAt === null || Date.now() - entry.unknownRefreshAt >= UNKNOWN_KEY_REFRESH_MS)) {
    // Record the attempt before fetching, so repeated failures cannot hammer
    // the issuer with one refresh per attacker-selected unknown key.
    entry.unknownRefreshAt = Date.now();
    entry = await refreshKeys(issuer, entry.unknownRefreshAt);
  }
  const selected = entry.keys.get(kid);
  if (!selected) denied("access_token_invalid");
  if (!selected.imported) {
    try {
      selected.imported = await crypto.subtle.importKey("jwk", selected.jwk,
        { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
    } catch { denied("access_keys_unavailable", 503); }
  }
  return selected.imported;
}

export async function verifyAccessOwner(request, env, requiredScope = "admin:read") {
  configured(env.ACCESS_JWT_REQUIRED === "true" && typeof env.POLICY_AUD === "string"
    && env.POLICY_AUD.length > 0 && env.POLICY_AUD.length <= 256);
  const issuer = issuerOrigin(env.TEAM_DOMAIN);
  const allowedEmails = ownerEmails(env.OWNER_EMAIL_ALLOWLIST);
  if (!OWNER_SCOPES.includes(requiredScope)) denied("access_scope_denied");
  const token = request.headers.get("cf-access-jwt-assertion");
  if (!token || ENCODER.encode(token).length >= MAX_JWT_BYTES) denied("access_token_invalid");
  const parts = token.split(".");
  if (parts.length !== 3) denied("access_token_invalid");
  const header = jsonPart(parts[0]), payload = jsonPart(parts[1]);
  if (header.alg !== "RS256" || typeof header.kid !== "string" || !header.kid.length
      || header.kid.length > 128 || /[\u0000-\u001f\u007f]/.test(header.kid)) denied("access_token_invalid");
  const now = Math.floor(Date.now() / 1000);
  const audience = typeof payload.aud === "string" ? [payload.aud] : payload.aud;
  if (payload.iss !== issuer || !Array.isArray(audience) || audience.length > 16
      || !audience.every(value => typeof value === "string") || !audience.includes(env.POLICY_AUD)
      || !Number.isSafeInteger(payload.exp) || payload.exp <= now || payload.exp > 8_640_000_000_000
      || (payload.nbf !== undefined && (!Number.isSafeInteger(payload.nbf) || payload.nbf > now + 60))
      || typeof payload.sub !== "string" || !payload.sub.trim() || payload.sub !== payload.sub.trim() || payload.sub.length > 256
      || /[\u0000-\u001f\u007f]/.test(payload.sub)
      || typeof payload.email !== "string" || !allowedEmails.has(payload.email.toLowerCase())) {
    denied("access_token_invalid");
  }
  const key = await accessKey(issuer, header.kid);
  let valid;
  try {
    valid = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, base64url(parts[2]),
      ENCODER.encode(`${parts[0]}.${parts[1]}`));
  } catch { denied("access_token_invalid"); }
  if (!valid) denied("access_token_invalid");
  return Object.freeze({ id: payload.sub, subject: payload.sub, email: payload.email.toLowerCase(),
    scopes: OWNER_SCOPES, expires_at: new Date(payload.exp * 1000).toISOString() });
}

function tokenConfiguration(raw) {
  if (typeof raw !== "string" || raw.length > 16 * 1024) denied("api_tokens_not_configured", 503);
  let values;
  try { values = JSON.parse(raw); } catch { denied("api_tokens_not_configured", 503); }
  if (!Array.isArray(values) || !values.length || values.length > 16) denied("api_tokens_not_configured", 503);
  const ids = new Set(), hashes = new Set();
  for (const value of values) {
    if (!value || typeof value !== "object" || Array.isArray(value)
      || Object.keys(value).some(key => !["id", "sha256", "scopes", "expires_at", "revoked"].includes(key))
      || typeof value.id !== "string" || !/^[A-Za-z0-9_-]{1,64}$/.test(value.id) || ids.has(value.id)
      || typeof value.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(value.sha256) || hashes.has(value.sha256)
      || !Array.isArray(value.scopes) || !value.scopes.length || value.scopes.length > TOKEN_SCOPES.size
      || new Set(value.scopes).size !== value.scopes.length || !value.scopes.every(scope => TOKEN_SCOPES.has(scope))
      || typeof value.revoked !== "boolean" || typeof value.expires_at !== "string"
      || !validExpiry(value.expires_at)) denied("api_tokens_not_configured", 503);
    ids.add(value.id); hashes.add(value.sha256);
  }
  return values;
}

function validExpiry(value) {
  const match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,3}))?Z$/.exec(value);
  const milliseconds = Date.parse(value);
  if (!match || !Number.isFinite(milliseconds)) return false;
  // Date.parse silently normalizes dates such as February 30; reject those.
  return new Date(milliseconds).toISOString() === `${match[1]}.${(match[2] || "").padEnd(3, "0")}Z`;
}

function equalHash(left, right) {
  // Both digests are fixed-length Uint8Arrays; never use string equality or an
  // early-return byte comparison for a bearer-token digest.
  let difference = left.length ^ right.length;
  for (let index = 0; index < 32; index++) difference |= left[index] ^ right[index];
  return difference === 0;
}

export async function verifyApiToken(request, env, requiredScope) {
  if (requiredScope === "admin:read") denied("access_owner_required");
  if (!TOKEN_SCOPES.has(requiredScope)) denied("api_scope_denied");
  const descriptors = tokenConfiguration(env.API_TOKEN_HASHES);
  const authorization = request.headers.get("authorization") || "";
  if (authorization.length > 256 || !/^Bearer iar_v2_[A-Za-z0-9_-]+$/.test(authorization)) {
    denied("api_token_invalid", 401);
  }
  const token = authorization.slice(7), encodedRandom = token.slice("iar_v2_".length);
  let randomBytes;
  try { randomBytes = base64url(encodedRandom); }
  catch { denied("api_token_invalid", 401); }
  // This enforces the 32-byte encoding contract, not a claim that a caller used
  // a cryptographic RNG. Issuers must generate random bytes securely.
  if (randomBytes.length < 32 || randomBytes.length > 128) denied("api_token_invalid", 401);
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", ENCODER.encode(token)));
  let matched = null;
  for (const descriptor of descriptors) {
    const expected = Uint8Array.from(descriptor.sha256.match(/../g), value => Number.parseInt(value, 16));
    if (equalHash(digest, expected)) matched = descriptor;
  }
  if (!matched || matched.revoked || Date.parse(matched.expires_at) <= Date.now()) denied("api_token_invalid", 401);
  if (!matched.scopes.includes(requiredScope)) denied("api_scope_denied");
  return Object.freeze({ id: matched.id, scopes: Object.freeze([...matched.scopes]), expires_at: matched.expires_at });
}
