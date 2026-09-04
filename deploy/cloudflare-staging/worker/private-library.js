// Read-only private canary adapter. The caller MUST authenticate before calling.
// Disabled unless explicitly configured; no inference, uploads or approval writes.
const SHA = /^[a-f0-9]{64}$/;
const ID = /^[A-Za-z0-9_-]{1,100}$/;
const MAX_BYTES = 8 * 1024 * 1024;
const HEADERS = {
  "Cache-Control": "no-store", "Content-Type": "application/json; charset=utf-8",
  "X-Content-Type-Options": "nosniff", "X-Robots-Tag": "noindex, nofollow, noarchive",
  "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
};
// One immutable snapshot per isolate. Cache is never an authentication bypass.
let cached = null;

function reject(code, status) {
  return Response.json({ error: code }, { status, headers: HEADERS });
}

async function sha256(bytes) {
  return [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))]
    .map(value => value.toString(16).padStart(2, "0")).join("");
}

async function validPrompt(prompt, item, library) {
  if (prompt?.schema_version !== "image-original-prompt-1" || prompt.id !== item.id ||
      prompt.release_eligible !== false) return false;
  if (prompt.status === "unavailable") {
    return prompt.full_prompt === null && prompt.prompt_sha256 === null && prompt.source_binding === null;
  }
  if (!["available", "missing"].includes(prompt.status) || typeof prompt.full_prompt !== "string") return false;
  if (prompt.status === "missing" && prompt.full_prompt.trim() !== "") return false;
  if (prompt.status === "available" && prompt.full_prompt.trim() === "") return false;
  if (prompt.status === "missing" && prompt.prompt_sha256 === null && prompt.source_binding === null) return true;
  const binding = prompt.source_binding;
  return SHA.test(prompt.prompt_sha256 ?? "") &&
    await sha256(new TextEncoder().encode(prompt.full_prompt)) === prompt.prompt_sha256 &&
    binding?.run_id === library.run_id && SHA.test(binding.spec_sha256 ?? "") &&
    SHA.test(binding.manifest_sha256 ?? "") && binding.original_image_sha256 === item.source_sha256 &&
    binding.prompt_field === "prompt";
}

async function validBundle(value) {
  if (!value || value.schema_version !== "image-private-library-bundle-1" ||
      value.visibility !== "private_access_only" || value.release_eligible !== false ||
      value.public_rights_approved !== false || value.mutation_enabled !== false) return false;
  const library = value.library;
  if (!SHA.test(value.source_commit?.id ?? "") || !library ||
      library.schema_version !== "image-approved-library-1" ||
      library.source_commit_id !== value.source_commit.id || library.release_eligible !== false ||
      library.public_rights_approved !== false || !Array.isArray(library.items) || library.items.length > 2000 ||
      !Array.isArray(library.display_groups) || !Array.isArray(library.ungrouped_ids)) return false;
  const ids = new Set();
  for (const item of library.items) {
    if (typeof item.id !== "string" || !ID.test(item.id) || ids.has(item.id) || !SHA.test(item.media_sha256 ?? "") ||
        !SHA.test(item.source_sha256 ?? "") ||
        item.media_key !== `private-library/media/${item.media_sha256}.png` ||
        item.release_eligible !== false || item.public_rights_approved !== false ||
        item.rights_display?.schema_version !== "image-rights-notice-1" ||
        item.rights_display.release_eligible !== false ||
        typeof item.rights_display.notice_text !== "string" ||
        !await validPrompt(item.original_prompt, item, library)) return false;
    ids.add(item.id);
  }
  const groups = new Set(), grouped = new Set(), overlaps = new Set();
  for (const group of library.display_groups) {
    if (typeof group.group_id !== "string" || !ID.test(group.group_id) || groups.has(group.group_id) ||
        !Array.isArray(group.member_ids) || group.member_ids.length < 2 ||
        new Set(group.member_ids).size !== group.member_ids.length ||
        !group.member_ids.every(id => ids.has(id)) || !group.member_ids.includes(group.representative_id) ||
        group.membership_basis !== "committed_human_approval") return false;
    groups.add(group.group_id);
    for (const id of group.member_ids) {
      if (grouped.has(id)) overlaps.add(id);
      grouped.add(id);
    }
  }
  const ungrouped = new Set(library.ungrouped_ids);
  return ungrouped.size === library.ungrouped_ids.length &&
    [...ungrouped].every(id => ids.has(id) && !grouped.has(id)) &&
    ungrouped.size + grouped.size === ids.size &&
    library.counts?.approved_images === ids.size && library.counts?.display_groups === groups.size &&
    library.counts?.overlapping_images === overlaps.size &&
    library.counts?.grouped_images === grouped.size && library.counts?.ungrouped_images === ungrouped.size;
}

async function readBounded(object) {
  if (!Number.isSafeInteger(object.size) || object.size < 1 || object.size > MAX_BYTES || !object.body) {
    throw new Error("size");
  }
  const reader = object.body.getReader(), chunks = [];
  let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_BYTES || size > object.size) throw new Error("size");
      chunks.push(value);
    }
  } finally {
    await reader.cancel();
    reader.releaseLock();
  }
  if (size !== object.size) throw new Error("size");
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return bytes;
}

export async function privateLibraryResponse(request, env) {
  if (env.ADMIN_ENABLED !== "true" || env.PRIVATE_LIBRARY_ENABLED !== "true") {
    return reject("private_library_disabled", 503);
  }
  const sha = env.PRIVATE_LIBRARY_SHA256;
  if (!SHA.test(sha ?? "") || typeof env.ARCHIVE_MEDIA?.get !== "function") {
    return reject("private_library_not_configured", 503);
  }
  if (new URL(request.url).search) return reject("query_not_supported", 400);
  if (request.method !== "GET" && request.method !== "HEAD") return reject("method_not_allowed", 405);
  try {
    let bytes;
    if (cached?.bucket === env.ARCHIVE_MEDIA && cached.sha === sha) {
      bytes = cached.bytes;
    } else {
      const object = await env.ARCHIVE_MEDIA.get(`private-library/snapshots/${sha}.json`);
      if (!object) return reject("private_library_not_found", 503);
      bytes = await readBounded(object);
      const actual = await sha256(bytes);
      if (actual !== sha || !await validBundle(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)))) {
        return reject("private_library_invalid", 503);
      }
      cached = { bucket: env.ARCHIVE_MEDIA, sha, bytes };
    }
    return new Response(request.method === "HEAD" ? null : bytes.slice(), {
      headers: { ...HEADERS, ETag: `"${sha}"` },
    });
  } catch {
    // Never echo R2 errors, private keys, prompt contents or infrastructure names.
    return reject("private_library_unavailable", 503);
  }
}
