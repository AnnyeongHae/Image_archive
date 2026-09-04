import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { test } from "node:test";
import { privateLibraryResponse } from "../deploy/cloudflare-staging/worker/private-library.js";

const request = (method = "GET", query = "") => new Request(`https://private.test/api/admin/v1/library${query}`, { method });
function fixture() {
  const sha = "a".repeat(64);
  const prompt = "{\n  \"한글\": \"원본\"\n}";
  return { schema_version: "image-private-library-bundle-1", visibility: "private_access_only",
    release_eligible: false, public_rights_approved: false, mutation_enabled: false, source_commit: { id: sha },
    library: { schema_version: "image-approved-library-1", run_id: "test", source_commit_id: sha, release_eligible: false,
      public_rights_approved: false, items: [{ id: "a", source_sha256: sha, media_sha256: sha, media_key: `private-library/media/${sha}.png`,
        release_eligible: false, public_rights_approved: false,
        rights_display: { schema_version: "image-rights-notice-1", notice_text: "권리 미확인", release_eligible: false },
        original_prompt: { schema_version: "image-original-prompt-1", id: "a", status: "available", full_prompt: prompt,
          prompt_sha256: createHash("sha256").update(prompt).digest("hex"), release_eligible: false,
          source_binding: { run_id: "test", spec_sha256: sha, manifest_sha256: sha, original_image_sha256: sha, prompt_field: "prompt" } } }],
      display_groups: [], ungrouped_ids: ["a"],
      counts: { approved_images: 1, display_groups: 0, grouped_images: 0, ungrouped_images: 1, overlapping_images: 0 } } };
}
function environment(value = fixture(), options = {}) {
  const bytes = new TextEncoder().encode(JSON.stringify(value));
  const sha = createHash("sha256").update(bytes).digest("hex");
  const calls = [];
  return { ADMIN_ENABLED: "true", PRIVATE_LIBRARY_ENABLED: "true", PRIVATE_LIBRARY_SHA256: sha, calls,
    ARCHIVE_MEDIA: { async get(key) {
      calls.push(key);
      return { size: options.size ?? bytes.length, body: new Response(bytes).body };
    } } };
}
test("disabled by default without any storage access", async () => {
  const env = environment(); delete env.PRIVATE_LIBRARY_ENABLED;
  assert.equal((await privateLibraryResponse(request(), env)).status, 503);
  assert.equal(env.calls.length, 0);
});
test("requires an exact SHA pin, never client supplied R2 key", async () => {
  const env = environment(); env.PRIVATE_LIBRARY_SHA256 = "../../secret";
  assert.equal((await privateLibraryResponse(request(), env)).status, 503);
  assert.equal(env.calls.length, 0);
});
test("returns intact approved private data and reuses one pinned object", async () => {
  const env = environment();
  const response = await privateLibraryResponse(request(), env);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("Cache-Control"), "no-store");
  assert.deepEqual(await response.json(), fixture());
  assert.equal((await privateLibraryResponse(request(), env)).status, 200);
  assert.deepEqual(env.calls, [`private-library/snapshots/${env.PRIVATE_LIBRARY_SHA256}.json`]);
});
test("HEAD validates the object but returns no body", async () => {
  const response = await privateLibraryResponse(request("HEAD"), environment());
  assert.equal(response.status, 200); assert.equal(await response.text(), "");
});
test("rejects mutations and unrecognized query parameters before R2", async () => {
  const env = environment();
  assert.equal((await privateLibraryResponse(request("POST"), env)).status, 405);
  assert.equal((await privateLibraryResponse(request("GET", "?key=other"), env)).status, 400);
  assert.equal(env.calls.length, 0);
});
test("rejects hash mismatches and retries rather than caching failure", async () => {
  const env = environment(); env.PRIVATE_LIBRARY_SHA256 = "0".repeat(64);
  assert.equal((await privateLibraryResponse(request(), env)).status, 503);
  assert.equal((await privateLibraryResponse(request(), env)).status, 503);
  assert.equal(env.calls.length, 2);
});
test("rejects oversize and lying size metadata", async () => {
  for (const size of [0, 1, 9 * 1024 * 1024]) {
    assert.equal((await privateLibraryResponse(request(), environment(fixture(), { size }))).status, 503);
  }
});
test("fails closed for release flags or invalid group membership", async () => {
  for (const mutate of [v => v.release_eligible = true, v => v.library.items[0].rights_display = {},
    v => v.library.ungrouped_ids.push("missing"), v => v.library.display_groups.push({ group_id: "bad", member_ids: ["a", "missing"] }),
    v => v.library.items.push(v.library.items[0]), v => v.library.source_commit_id = "b".repeat(64)]) {
    const value = fixture(); mutate(value);
    assert.equal((await privateLibraryResponse(request(), environment(value))).status, 503);
  }
});
test("R2 failures never expose private exception messages", async () => {
  const env = environment(); env.ARCHIVE_MEDIA.get = async () => { throw new Error("private credential"); };
  const response = await privateLibraryResponse(request(), env);
  assert.equal(response.status, 503); assert.doesNotMatch(await response.text(), /credential/);
});

test("rejects poisoned nested rights, prompt identity and raw prompt hash even if pinned", async () => {
  for (const mutate of [v => v.library.items[0].rights_display.release_eligible = true,
    v => v.library.items[0].original_prompt.release_eligible = true,
    v => v.library.items[0].original_prompt.id = "other",
    v => v.library.items[0].original_prompt.full_prompt += "corrupt",
    v => v.library.items[0].original_prompt.source_binding.run_id = "other",
    v => v.library.items[0].original_prompt.status = "fake",
    v => v.library.counts.overlapping_images = 999]) {
    const value = fixture(); mutate(value);
    assert.equal((await privateLibraryResponse(request(), environment(value))).status, 503);
  }
});

test("accepts explicit unavailable or empty original prompt without claiming it exists", async () => {
  for (const status of ["unavailable", "missing"]) {
    const value = fixture();
    value.library.items[0].original_prompt = { schema_version: "image-original-prompt-1", id: "a", status,
      full_prompt: status === "missing" ? "" : null, prompt_sha256: null, source_binding: null, release_eligible: false };
    assert.equal((await privateLibraryResponse(request(), environment(value))).status, 200);
  }
});
