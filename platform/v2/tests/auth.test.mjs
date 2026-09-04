import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { afterEach, before, test } from "node:test";

const issuer = "https://owner-fixture.cloudflareaccess.com";
const email = "owner@example.test";
const audience = "fixture-only-policy";
const originalFetch = globalThis.fetch;
const originalNow = Date.now;
let keyPair, secondKeyPair, jwk, secondJwk, moduleIndex = 0;
const encoder = new TextEncoder();

before(async () => {
  const parameters = { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048,
    publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" };
  keyPair = await crypto.subtle.generateKey(parameters, true, ["sign", "verify"]);
  secondKeyPair = await crypto.subtle.generateKey(parameters, true, ["sign", "verify"]);
  jwk = { ...await crypto.subtle.exportKey("jwk", keyPair.publicKey), kid: "fixture-key", alg: "RS256", use: "sig" };
  secondJwk = { ...await crypto.subtle.exportKey("jwk", secondKeyPair.publicKey), kid: "rotated-key", alg: "RS256", use: "sig" };
});

afterEach(() => { globalThis.fetch = originalFetch; Date.now = originalNow; });

function fresh() { return import(`../worker/auth.js?isolatedFixture=${++moduleIndex}`); }
function env() {
  return { ACCESS_JWT_REQUIRED: "true", TEAM_DOMAIN: issuer, POLICY_AUD: audience,
    OWNER_EMAIL_ALLOWLIST: JSON.stringify([email]) };
}
function encode(value) { return Buffer.from(typeof value === "string" ? value : JSON.stringify(value)).toString("base64url"); }
async function jwt(claims = {}, header = {}, pair = keyPair) {
  const now = Math.floor(Date.now() / 1000);
  const unsigned = `${encode({ alg: "RS256", kid: "fixture-key", ...header })}.${encode({
    iss: issuer, aud: audience, sub: "fixture-subject", email, exp: now + 3600, nbf: now - 10, ...claims,
  })}`;
  const signature = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", pair.privateKey, encoder.encode(unsigned));
  return `${unsigned}.${Buffer.from(signature).toString("base64url")}`;
}
function accessRequest(token) {
  return new Request("https://private.example.test/admin/", { headers: { "cf-access-jwt-assertion": token } });
}
function mockJwks(keys = [jwk]) {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    assert.equal(String(url), `${issuer}/cdn-cgi/access/certs`);
    assert.equal(options.redirect, "manual");
    assert.equal(options.credentials, "omit");
    assert.ok(options.signal);
    return Response.json({ keys });
  };
  return calls;
}
function error(status, code) {
  return value => {
    assert.equal(value.name, "AuthError");
    assert.equal(value.status, status);
    if (code) assert.equal(value.code, code);
    assert.equal(value.message, value.code);
    return true;
  };
}

test("owner signature and exact configured email return only principal fields", async () => {
  const auth = await fresh(), calls = mockJwks();
  const principal = await auth.verifyAccessOwner(accessRequest(await jwt()), env());
  assert.deepEqual(Object.keys(principal), ["id", "subject", "email", "scopes", "expires_at"]);
  assert.equal(principal.email, email);
  assert.deepEqual(principal.scopes, ["rag:search", "archive:read", "admin:read"]);
  assert.equal(calls.length, 1);
  assert.ok(Object.isFrozen(principal));
});

test("JWKS redirects are refused without following a new issuer", async () => {
  for (const status of [301, 302, 303, 307, 308]) {
    const auth = await fresh(); let calls = 0;
    globalThis.fetch = async (url, options) => {
      calls++;
      assert.equal(String(url), `${issuer}/cdn-cgi/access/certs`);
      assert.equal(options.redirect, "manual");
      return new Response(null, { status, headers: { Location: "https://untrusted.example.test/keys" } });
    };
    await assert.rejects(auth.verifyAccessOwner(accessRequest(await jwt()), env()),
      error(503, "access_keys_unavailable"));
    assert.equal(calls, 1);
  }
});

test("all owner configuration is explicit and missing or wildcard owner config fails before fetch", async () => {
  const auth = await fresh(), calls = mockJwks(), token = await jwt();
  for (const mutation of [e => delete e.ACCESS_JWT_REQUIRED, e => delete e.TEAM_DOMAIN,
    e => delete e.POLICY_AUD, e => delete e.OWNER_EMAIL_ALLOWLIST,
    e => e.OWNER_EMAIL_ALLOWLIST = '["*@example.test"]', e => e.OWNER_EMAIL_ALLOWLIST = "[]",
    e => e.TEAM_DOMAIN = "https://evil.example/", e => e.TEAM_DOMAIN = `${issuer}/other`,
    e => e.TEAM_DOMAIN = "https://user:password@owner-fixture.cloudflareaccess.com/"] ) {
    const values = env(); mutation(values);
    await assert.rejects(auth.verifyAccessOwner(accessRequest(token), values), error(503, "access_not_configured"));
  }
  assert.equal(calls.length, 0);
});

test("missing malformed and 16KiB JWT assertions are denied without key requests", async () => {
  const auth = await fresh(), calls = mockJwks();
  for (const token of ["", "not-a-jwt", "a.b.c", "x".repeat(16 * 1024), `${encode([])}.${encode({})}.AA`]) {
    await assert.rejects(auth.verifyAccessOwner(accessRequest(token), env()), error(403));
  }
  assert.equal(calls.length, 0);
});

test("issuer audience lifetime subject and owner identity fail closed before storage/network", async () => {
  const auth = await fresh(), calls = mockJwks();
  const now = Math.floor(Date.now() / 1000);
  for (const claims of [{ iss: "https://wrong.cloudflareaccess.com" }, { aud: "wrong" }, { aud: [audience, 2] },
    { exp: now - 1 }, { exp: "9999999999" }, { exp: Number.MAX_SAFE_INTEGER }, { nbf: now + 120 }, { nbf: "0" },
    { sub: "" }, { sub: "  " }, { sub: " padded " }, { sub: 7 }, { sub: "bad\u0000subject" },
    { email: "intruder@example.test" }, { email: null }, { email: `${email} ` }]) {
    await assert.rejects(auth.verifyAccessOwner(accessRequest(await jwt(claims)), env()), error(403));
  }
  await assert.rejects(auth.verifyAccessOwner(accessRequest(await jwt()), env(), "review:write"), error(403));
  assert.equal(calls.length, 0);
});

test("algorithm substitution and wrong-key signatures cannot authenticate", async () => {
  const auth = await fresh(), calls = mockJwks();
  await assert.rejects(auth.verifyAccessOwner(accessRequest(await jwt({}, { alg: "HS256" })), env()), error(403));
  assert.equal(calls.length, 0);
  await assert.rejects(auth.verifyAccessOwner(accessRequest(await jwt({}, {}, secondKeyPair)), env()), error(403));
  assert.equal(calls.length, 1);
});

test("warm JWKS cache never bypasses owner checks", async () => {
  const auth = await fresh(), calls = mockJwks(), request = accessRequest(await jwt());
  await auth.verifyAccessOwner(request, env());
  await auth.verifyAccessOwner(request, env(), "archive:read");
  await assert.rejects(auth.verifyAccessOwner(accessRequest(await jwt({ email: "other@example.test" })), env()), error(403));
  await assert.rejects(auth.verifyAccessOwner(accessRequest(""), env()), error(403));
  assert.equal(calls.length, 1);
});

test("unknown kid refresh is bounded, supports rotation, and TTL forces new retrieval", async () => {
  const auth = await fresh();
  let calls = 0;
  globalThis.fetch = async () => Response.json({ keys: ++calls === 1 ? [jwk] : [jwk, secondJwk] });
  const request = accessRequest(await jwt());
  await auth.verifyAccessOwner(request, env());
  await auth.verifyAccessOwner(accessRequest(await jwt({}, { kid: secondJwk.kid }, secondKeyPair)), env());
  assert.equal(calls, 2);
  await assert.rejects(auth.verifyAccessOwner(accessRequest(await jwt({}, { kid: "unknown-third" })), env()), error(403));
  await assert.rejects(auth.verifyAccessOwner(accessRequest(await jwt({}, { kid: "unknown-fourth" })), env()), error(403));
  assert.equal(calls, 2);
  Date.now = () => originalNow() + 301_000;
  await auth.verifyAccessOwner(request, env());
  assert.equal(calls, 3);
});

test("concurrent cold verification shares one bounded JWKS request", async () => {
  const auth = await fresh(), calls = mockJwks(), request = accessRequest(await jwt());
  await Promise.all(Array.from({ length: 5 }, () => auth.verifyAccessOwner(request, env())));
  assert.equal(calls.length, 1);
});

test("JWKS issuer cache evicts the oldest entry after four issuers", async () => {
  const auth = await fresh();
  let calls = 0;
  globalThis.fetch = async () => { calls++; return Response.json({ keys: [jwk] }); };
  const requests = [];
  for (let index = 0; index < 5; index++) {
    const otherIssuer = `https://owner-${index}.cloudflareaccess.com`;
    const values = { ...env(), TEAM_DOMAIN: otherIssuer };
    const request = accessRequest(await jwt({ iss: otherIssuer }));
    requests.push([request, values]);
    await auth.verifyAccessOwner(request, values);
  }
  assert.equal(calls, 5);
  await auth.verifyAccessOwner(...requests[0]);
  assert.equal(calls, 6);
});

test("JWKS failure has a bounded cooldown and can recover", async () => {
  const auth = await fresh();
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    return calls === 1 ? new Response("", { status: 503 }) : Response.json({ keys: [jwk] });
  };
  const request = accessRequest(await jwt());
  await assert.rejects(auth.verifyAccessOwner(request, env()), error(503));
  await assert.rejects(auth.verifyAccessOwner(request, env()), error(503));
  assert.equal(calls, 1);
  Date.now = () => originalNow() + 10_001;
  await auth.verifyAccessOwner(request, env());
  assert.equal(calls, 2);
});

test("JWKS HTTP, JSON, size and key-count failures disclose only safe errors", async () => {
  const request = accessRequest(await jwt());
  for (const fixture of [
    () => new Response("provider-private-detail", { status: 500 }),
    () => new Response("not json", { headers: { "content-type": "application/json" } }),
    () => new Response("x".repeat(64 * 1024 + 1), { headers: { "content-type": "application/json" } }),
    () => new Response("{}", { headers: { "content-type": "application/json", "content-length": "1000000" } }),
    () => Response.json({ keys: Array.from({ length: 33 }, () => jwk) }),
    () => Response.json({ keys: [jwk, jwk] }),
    () => { throw new Error("upstream-private-credential"); },
  ]) {
    const auth = await fresh(); globalThis.fetch = async () => fixture();
    await assert.rejects(auth.verifyAccessOwner(request, env()), value => {
      error(503, "access_keys_unavailable")(value);
      assert.doesNotMatch(JSON.stringify(value), /private-credential|provider-private-detail/);
      return true;
    });
  }
});

test("JWKS timeout terminates verification even if fetch ignores abort", async () => {
  const auth = await fresh(); globalThis.fetch = () => new Promise(() => {});
  const started = performance.now();
  await assert.rejects(auth.verifyAccessOwner(accessRequest(await jwt()), env()), error(503, "access_keys_unavailable"));
  assert.ok(performance.now() - started < 4500);
});

// Deliberately non-random SYNTHETIC test tokens. No production values are read.
const token = "iar_v2_" + Buffer.alloc(32, 1).toString("base64url");
const otherToken = "iar_v2_" + Buffer.alloc(32, 2).toString("base64url");
function descriptor(overrides = {}) {
  return { id: "fixture-token", sha256: createHash("sha256").update(token).digest("hex"),
    scopes: ["rag:search", "archive:read"], expires_at: new Date(Date.now() + 3600_000).toISOString(), revoked: false,
    ...overrides };
}
function tokenEnv(rows = [descriptor()]) { return { API_TOKEN_HASHES: JSON.stringify(rows) }; }
function tokenRequest(value = token) {
  return new Request("https://private.example.test/api/v2/search", { headers: { authorization: "Bearer " + value } });
}

test("API token returns descriptor identity only and performs zero network requests", async () => {
  const auth = await fresh(); globalThis.fetch = async () => { throw new Error("network forbidden"); };
  const values = descriptor();
  assert.deepEqual(await auth.verifyApiToken(tokenRequest(), tokenEnv([values]), "rag:search"), {
    id: values.id, scopes: values.scopes, expires_at: values.expires_at,
  });
});

test("API missing token short entropy wrong token expired revoked and scope are denied", async () => {
  const auth = await fresh();
  for (const value of ["", "x", otherToken, "iar_v2_" + Buffer.alloc(31).toString("base64url"), "iar_v2_" + "x".repeat(300)]) {
    await assert.rejects(auth.verifyApiToken(tokenRequest(value), tokenEnv(), "rag:search"), error(401, "api_token_invalid"));
  }
  for (const row of [descriptor({ revoked: true }), descriptor({ expires_at: new Date(Date.now() - 1000).toISOString() })]) {
    await assert.rejects(auth.verifyApiToken(tokenRequest(), tokenEnv([row]), "archive:read"), error(401));
  }
  await assert.rejects(auth.verifyApiToken(tokenRequest(), tokenEnv([descriptor({ scopes: ["archive:read"] })]), "rag:search"), error(403, "api_scope_denied"));
});

test("API token can never bypass owner Access on administrative scope", async () => {
  const auth = await fresh();
  await assert.rejects(auth.verifyApiToken(tokenRequest(), tokenEnv(), "admin:read"), error(403, "access_owner_required"));
  await assert.rejects(auth.verifyApiToken(tokenRequest(), tokenEnv([descriptor({ scopes: ["admin:read"] })]), "archive:read"), error(503));
});

test("API descriptors reject missing config excess tokens plaintext fields invalid expiry and duplicate identity/hash", async () => {
  const auth = await fresh();
  const invalid = [undefined, "not-json", "[]", JSON.stringify(Array.from({ length: 17 }, () => descriptor())),
    JSON.stringify([descriptor({ token: "synthetic-plaintext-must-not-be-stored" })]),
    JSON.stringify([descriptor({ expires_at: null })]), JSON.stringify([descriptor({ expires_at: "tomorrow" })]),
    JSON.stringify([descriptor({ expires_at: "2099-02-30T00:00:00Z" })]),
    JSON.stringify([descriptor({ revoked: undefined })]), JSON.stringify([descriptor({ sha256: "invalid" })]),
    JSON.stringify([descriptor(), descriptor()]), JSON.stringify([descriptor({ scopes: ["rag:search", "rag:search"] })])];
  for (const raw of invalid) {
    await assert.rejects(auth.verifyApiToken(tokenRequest(), { API_TOKEN_HASHES: raw }, "rag:search"), error(503, "api_tokens_not_configured"));
  }
});

test("API token scans valid bounded descriptor list and respects immediate revocation", async () => {
  const auth = await fresh();
  const rows = [descriptor({ id: "other", sha256: createHash("sha256").update(otherToken).digest("hex") }), descriptor()];
  assert.equal((await auth.verifyApiToken(tokenRequest(), tokenEnv(rows), "archive:read")).id, "fixture-token");
  rows[1].revoked = true;
  await assert.rejects(auth.verifyApiToken(tokenRequest(), tokenEnv(rows), "archive:read"), error(401));
});
