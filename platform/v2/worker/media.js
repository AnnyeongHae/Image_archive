import { ApiError, PRIVATE_HEADERS } from "./http.js";
import { sql } from "./neon.js";

const MAX_BYTES = 15 * 1048576;
const PNG_SIGNATURE = [137, 80, 78, 71, 13, 10, 26, 10];

async function verifiedBody(object, expectedDigest) {
  if (!object.body?.getReader) throw new ApiError("media_contract_mismatch");
  const reader = object.body.getReader();
  const bytes = new Uint8Array(object.size); let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      if (!(value instanceof Uint8Array)) throw new ApiError("media_contract_mismatch");
      size += value.byteLength;
      // Check the declared R2 content length and hard cap during collection.
      if (size > object.size || size > MAX_BYTES) throw new ApiError("media_contract_mismatch");
      bytes.set(value, size - value.byteLength);
    }
    if (size !== object.size || size < PNG_SIGNATURE.length) throw new ApiError("media_contract_mismatch");
    if (!PNG_SIGNATURE.every((value, index) => bytes[index] === value)) throw new ApiError("media_contract_mismatch");
    // This buffers at most 15 MiB plus the current R2 chunk. Benchmark hashing/copying
    // against the deployed free-tier CPU/memory limits before full rollout;
    // the cap is the media contract, not evidence that free-tier CPU will fit.
    const actualDigest = [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))]
      .map(byte => byte.toString(16).padStart(2, "0")).join("");
    if (actualDigest !== expectedDigest) throw new ApiError("media_contract_mismatch");
    return bytes;
  } catch {
    await reader.cancel().catch(() => {});
    throw new ApiError("media_contract_mismatch");
  } finally { reader.releaseLock(); }
}

// Only authenticated item IDs resolve to pinned content-addressed private media.
// Neither the caller nor a stored source URL can control an upstream fetch.
export async function privateImage(env, itemId) {
  if (!/^[A-Za-z0-9_.:-]{1,160}$/.test(itemId)) throw new ApiError("invalid_item", 400);
  if (!env.PRIVATE_MEDIA?.get) throw new ApiError("private_media_not_configured");
  const rows = await sql(env, `SELECT private_data->>'prepared_sha256' AS sha256
    FROM image_archive_v2.items WHERE snapshot_id=$1 AND item_id=$2`, [env.SNAPSHOT_ID, itemId]);
  if (rows.length !== 1) throw new ApiError("not_found", 404);
  const digest = rows[0].sha256;
  if (!/^[a-f0-9]{64}$/.test(digest || "")) throw new ApiError("media_contract_mismatch");
  const object = await env.PRIVATE_MEDIA.get(`private/v2/sha256/${digest}.png`);
  if (!object) throw new ApiError("private_media_not_ready");
  if (!Number.isSafeInteger(object.size) || object.size < PNG_SIGNATURE.length || object.size > MAX_BYTES
      || object.httpMetadata?.contentType !== "image/png") {
    await object.body?.cancel();
    throw new ApiError("media_contract_mismatch");
  }
  const bytes = await verifiedBody(object, digest);
  return new Response(bytes, { headers: { ...PRIVATE_HEADERS,
    "Content-Type": "image/png", "Content-Length": String(object.size),
    "Content-Disposition": 'inline; filename="reference.png"' } });
}
