import test from "node:test";
import assert from "node:assert/strict";
import worker, { releaseSummary } from "../worker/public-gallery.js";

const vars = {
  PUBLIC_RELEASE_ID: "a".repeat(64),
  PUBLIC_IMAGE_COUNT: "379", PUBLIC_GROUP_COUNT: "326", PUBLIC_VARIANT_COUNT: "53",
};
const request = (path, method = "GET") => new Request(`https://photoposting.shop${path}`, { method });

test("summary identifies actual v2 release and counters, not legacy 529", async () => {
  for (const path of ["/healthz", "/api/public/v2/summary"]) {
    const response = await worker.fetch(request(path), vars);
    assert.equal(response.status, 200);
    const value = await response.json();
    assert.equal(value.gallery_version, 2);
    assert.equal(value.public_records, 379);
    assert.equal(value.release_id, vars.PUBLIC_RELEASE_ID);
    assert.deepEqual(value.counts, { images: 379, groups: 326, variants: 53 });
    assert.equal(response.headers.get("cache-control"), "no-store");
    assert.equal(value.private_data_included, false);
  }
});

test("missing or inconsistent release variables fail closed", () => {
  for (const change of [{ PUBLIC_RELEASE_ID: "" }, { PUBLIC_IMAGE_COUNT: "529" },
    { PUBLIC_GROUP_COUNT: "0" }, { PUBLIC_VARIANT_COUNT: "-1" }, { PUBLIC_IMAGE_COUNT: "379foo" }]) {
    assert.equal(releaseSummary({ ...vars, ...change }), null);
  }
});

test("private/legacy API and internal files never reach static binding", async () => {
  const env = { ...vars, ASSETS: { fetch() { throw new Error("private path reached assets"); } } };
  for (const path of ["/api/public/v1/summary", "/api/private/v2/search", "/api/admin/v2/status", "/admin",
    "/approval-requests", "/source-admin", "/duplicate-review", "/.env", "/.git/config", "/candidate.json",
    "/grant.json", "/data/private-research/example", "/%2Eenv", "/%zz", "/api", "/admin.html",
    "/source-admin.html", "/duplicate-review.html", "/approval-requests.html"]) {
    assert.equal((await worker.fetch(request(path), env)).status, 404, path);
  }
});

test("HEAD has no response body and writes are rejected", async () => {
  assert.equal(await (await worker.fetch(request("/healthz", "HEAD"), vars)).text(), "");
  assert.equal((await worker.fetch(request("/data/catalog.json", "POST"), vars)).status, 405);
});

test("Worker fallback refuses assets without a configured release", async () => {
  const response = await worker.fetch(request("/data/catalog.json"), { ASSETS: { fetch() {
    throw new Error("unconfigured static fallback");
  } } });
  assert.equal(response.status, 503);
});

test("static assets pass through without injecting private credentials", async () => {
  const input = request("/data/catalog.json");
  let called = 0;
  const response = await worker.fetch(input, { ...vars, ASSETS: { fetch(received) {
    called++;
    assert.equal(received, input);
    return new Response("static-v2");
  } } });
  assert.equal(await response.text(), "static-v2");
  assert.equal(called, 1);
});
