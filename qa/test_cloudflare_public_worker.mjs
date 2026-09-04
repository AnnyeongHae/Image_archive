import assert from "node:assert/strict";
import worker from "../deploy/cloudflare-public/worker/index.js";

const requests = [];
const env = {
  DEPLOYMENT_LANE: "public-awesome-gpt-image-2-mvp",
  PUBLIC_RECORD_COUNT: "529",
  ASSETS: {
    fetch(request) {
      requests.push(request.url);
      return new Response("asset", { status: 200 });
    },
  },
};

async function call(path, init = {}) {
  return worker.fetch(new Request(`https://public.example${path}`, init), env);
}

const health = await call("/healthz");
assert.equal(health.status, 200);
assert.deepEqual(await health.json(), {
  ok: true,
  service: "image-prompt-archive-public-staging",
  lane: "public-awesome-gpt-image-2-mvp",
  public_records: 529,
  private_data_included: false,
  admin_data_included: false,
});

const summary = await call("/api/public/v1/summary");
assert.equal(summary.status, 200);
assert.equal((await summary.json()).public_records, 529);

const admin = await call("/admin/");
assert.equal(admin.status, 302);
assert.equal(
  admin.headers.get("location"),
  "https://image-prompt-archive-staging.andrew4may.workers.dev/admin/",
);

for (const path of [
  "/approval-requests.html",
  "/source-admin.html",
  "/duplicate-review.html",
  "/api/private",
]) {
  const response = await call(path);
  assert.equal(response.status, 404, `${path} must stay outside the public Worker`);
}

assert.equal((await call("/", { method: "POST" })).status, 405);
assert.equal((await call("/")).status, 200);
assert.deepEqual(requests, ["https://public.example/"]);

console.log("cloudflare public worker contract: ok");
