import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { after, before, test } from "node:test";

import worker from "../deploy/cloudflare-staging/worker/index.js";

const TEAM_DOMAIN = "https://travel-agency.cloudflareaccess.com";
const POLICY_AUD = "test-access-policy-audience";
const KEY_ID = "cloudflare-access-test-key";
const OWNER_EMAIL = "staging-owner@example.test";
const encoder = new TextEncoder();

let privateKey;
let publicJwk;
let originalFetch;
let originalWarn;
let assetRequests;

function encodeBase64Url(value) {
  const bytes = typeof value === "string" ? encoder.encode(value) : value;
  return Buffer.from(bytes).toString("base64url");
}

async function signAccessJwt(overrides = {}) {
  const now = Math.floor(Date.now() / 1000);
  const header = { alg: "RS256", kid: KEY_ID, typ: "JWT" };
  const payload = {
    iss: TEAM_DOMAIN,
    aud: POLICY_AUD,
    sub: "test-admin",
    email: OWNER_EMAIL,
    exp: now + 300,
    nbf: now - 10,
    ...overrides,
  };
  const unsigned = `${encodeBase64Url(JSON.stringify(header))}.${encodeBase64Url(JSON.stringify(payload))}`;
  const signature = await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    privateKey,
    encoder.encode(unsigned),
  );
  return `${unsigned}.${encodeBase64Url(new Uint8Array(signature))}`;
}

function makeEnv() {
  return {
    ACCESS_JWT_REQUIRED: "true",
    TEAM_DOMAIN,
    POLICY_AUD,
    OWNER_EMAIL_ALLOWLIST: JSON.stringify([OWNER_EMAIL]),
    DEPLOYMENT_LANE: "private-staging",
    PUBLIC_RECORD_COUNT: "0",
    ADMIN_ENABLED: "true",
    ARCHIVE_MEDIA: {},
    ASSETS: {
      async fetch(request) {
        assetRequests.push(new URL(request.url).pathname);
        return new Response("<main>private admin shell</main>", {
          status: 200,
          headers: { "Content-Type": "text/html; charset=utf-8" },
        });
      },
    },
  };
}

async function authorizedRequest(path, init = {}, claims = {}) {
  const token = await signAccessJwt(claims);
  const headers = new Headers(init.headers);
  headers.set("cf-access-jwt-assertion", token);
  return new Request(`https://staging.example.test${path}`, { ...init, headers });
}

before(async () => {
  const keyPair = await crypto.subtle.generateKey(
    {
      name: "RSASSA-PKCS1-v1_5",
      modulusLength: 2048,
      publicExponent: new Uint8Array([1, 0, 1]),
      hash: "SHA-256",
    },
    true,
    ["sign", "verify"],
  );
  privateKey = keyPair.privateKey;
  publicJwk = await crypto.subtle.exportKey("jwk", keyPair.publicKey);
  publicJwk = { ...publicJwk, kid: KEY_ID, alg: "RS256", use: "sig" };

  originalFetch = globalThis.fetch;
  originalWarn = console.warn;
  assetRequests = [];
  console.warn = () => {};
  globalThis.fetch = async (input) => {
    assert.equal(
      String(input),
      `${TEAM_DOMAIN}/cdn-cgi/access/certs`,
      "the Worker must fetch JWKS only from its configured Access team domain",
    );
    return Response.json({ keys: [publicJwk] });
  };
});

after(() => {
  globalThis.fetch = originalFetch;
  console.warn = originalWarn;
});

test("rejects a request without a Cloudflare Access assertion", async () => {
  const response = await worker.fetch(
    new Request("https://staging.example.test/healthz"),
    makeEnv(),
  );

  assert.equal(response.status, 403);
  assert.deepEqual(await response.json(), { error: "access_denied" });
});

test("rejects a correctly signed token for the wrong Access audience", async () => {
  const request = await authorizedRequest("/healthz", {}, { aud: "wrong-audience" });
  const response = await worker.fetch(request, makeEnv());

  assert.equal(response.status, 403);
  assert.deepEqual(await response.json(), { error: "access_denied" });
});

test("rejects a signed non-owner and missing subject before private assets or R2", async () => {
  const env = makeEnv();
  let reads = 0;
  env.ARCHIVE_MEDIA = { get() { reads++; throw new Error("must not read"); } };
  assetRequests = [];
  for (const claims of [{ email: "not-owner@example.test" }, { sub: "" }, { email: null }]) {
    const response = await worker.fetch(await authorizedRequest("/api/admin/v1/library", {}, claims), env);
    assert.equal(response.status, 403);
    assert.deepEqual(await response.json(), { error: "access_denied" });
  }
  assert.equal(reads, 0);
  assert.deepEqual(assetRequests, []);
});

test("missing explicit owner allowlist fails closed and bearer token does not bypass Access", async () => {
  const env = makeEnv();
  delete env.OWNER_EMAIL_ALLOWLIST;
  assetRequests = [];
  const missingConfig = await worker.fetch(await authorizedRequest("/admin/"), env);
  assert.equal(missingConfig.status, 503);
  assert.deepEqual(await missingConfig.json(), { error: "access_not_configured" });
  const bearerOnly = new Request("https://staging.example.test/admin/", {
    headers: { Authorization: "Bearer iar_v2_" + Buffer.alloc(32, 1).toString("base64url") },
  });
  assert.equal((await worker.fetch(bearerOnly, makeEnv())).status, 403);
  assert.deepEqual(assetRequests, []);
});

test("returns an authenticated fail-closed health response", async () => {
  const response = await worker.fetch(await authorizedRequest("/healthz"), makeEnv());
  const body = await response.json();

  assert.equal(response.status, 200);
  assert.equal(body.ok, true);
  assert.equal(body.access_jwt_validated, true);
  assert.equal(body.private_data_included, false);
  assert.equal(body.public_records, 0);
});

test("keeps the authenticated admin status endpoint read-only and empty", async () => {
  const response = await worker.fetch(
    await authorizedRequest("/api/admin/v1/status"),
    makeEnv(),
  );
  const body = await response.json();

  assert.equal(response.status, 200);
  assert.equal(body.access_jwt_validated, true);
  assert.equal(body.private_records, 0);
  assert.equal(body.private_media_objects, 0);
  assert.equal(body.mutation_enabled, false);
});

test("serves the authenticated admin asset with a restrictive CSP", async () => {
  assetRequests = [];
  const response = await worker.fetch(await authorizedRequest("/admin/"), makeEnv());

  assert.equal(response.status, 200);
  assert.deepEqual(assetRequests, ["/admin/"]);
  assert.match(response.headers.get("Content-Security-Policy") ?? "", /default-src 'none'/);
  assert.match(response.headers.get("Content-Security-Policy") ?? "", /script-src 'self'/);
  assert.match(response.headers.get("Content-Security-Policy") ?? "", /connect-src 'self'/);
});

test("rejects mutating methods even when Access authentication succeeds", async () => {
  const response = await worker.fetch(
    await authorizedRequest("/api/admin/v1/status", { method: "POST" }),
    makeEnv(),
  );

  assert.equal(response.status, 405);
  assert.deepEqual(await response.json(), { error: "method_not_allowed" });
});

test("private library authenticates before any R2 access and stays disabled", async () => {
  const env = makeEnv();
  let reads = 0;
  env.ARCHIVE_MEDIA = { get() { reads++; throw new Error("must not read"); } };
  assert.equal((await worker.fetch(new Request("https://staging.example.test/api/admin/v1/library"), env)).status, 403);
  const response = await worker.fetch(await authorizedRequest("/api/admin/v1/library"), env);
  assert.equal(response.status, 503);
  assert.deepEqual(await response.json(), { error: "private_library_disabled" });
  assert.equal(reads, 0);
});

test("configured library status does not falsely report an empty private library", async () => {
  const env = makeEnv(); env.PRIVATE_LIBRARY_ENABLED = "true";
  const status = await worker.fetch(await authorizedRequest("/api/admin/v1/status"), env);
  const body = await status.json();
  assert.equal(body.private_records, null);
  assert.equal(body.private_record_count_status, "read_pinned_library_for_verified_count");
  assert.equal(body.mutation_enabled, false);
  const health = await worker.fetch(await authorizedRequest("/healthz"), env);
  assert.equal((await health.json()).private_library_read_enabled, true);
});

test("a warmed private snapshot cache never bypasses Access authentication", async () => {
  const env = makeEnv(), commit = "a".repeat(64);
  const fixture = {schema_version:"image-private-library-bundle-1",visibility:"private_access_only",
    release_eligible:false,public_rights_approved:false,mutation_enabled:false,source_commit:{id:commit},
    library:{schema_version:"image-approved-library-1",source_commit_id:commit,release_eligible:false,
      public_rights_approved:false,items:[],display_groups:[],ungrouped_ids:[],
      counts:{approved_images:0,display_groups:0,grouped_images:0,ungrouped_images:0,overlapping_images:0}}};
  const bytes = encoder.encode(JSON.stringify(fixture));
  let reads = 0;
  env.PRIVATE_LIBRARY_ENABLED = "true";
  env.PRIVATE_LIBRARY_SHA256 = createHash("sha256").update(bytes).digest("hex");
  env.ARCHIVE_MEDIA = {async get(){reads++;return {size:bytes.length,body:new Response(bytes).body};}};
  assert.equal((await worker.fetch(await authorizedRequest("/api/admin/v1/library"),env)).status,200);
  assert.equal((await worker.fetch(await authorizedRequest("/api/admin/v1/library"),env)).status,200);
  assert.equal(reads,1);
  assert.equal((await worker.fetch(new Request("https://staging.example.test/api/admin/v1/library"),env)).status,403);
  assert.equal((await worker.fetch(await authorizedRequest("/api/admin/v1/library",{}, {aud:"wrong"}),env)).status,403);
  assert.equal(reads,1);
});
