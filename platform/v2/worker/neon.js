import { ApiError, fetchJson } from "./http.js";

// Small parameterized HTTP adapter. Protocol follows the upstream Neon driver;
// no SQL, host, connection string or provider error body comes from the caller.
export function neonEndpoint(dsn) {
  let url;
  try { url = new URL(dsn); } catch { throw new ApiError("database_not_configured"); }
  if (!["postgres:", "postgresql:"].includes(url.protocol) || !url.username || !url.password
      || !/^ep-[a-z0-9-]+\.[a-z0-9.-]+\.neon\.tech$/.test(url.hostname)
      || (url.port && url.port !== "5432") || url.pathname.length < 2 || url.hash) {
    throw new ApiError("database_not_configured");
  }
  const options = [...url.searchParams];
  if (new Set(options.map(([key]) => key)).size !== options.length || options.some(([key, value]) =>
    !(key === "sslmode" && ["require", "verify-full"].includes(value))
    && !(key === "channel_binding" && ["require", "prefer"].includes(value)))) {
    throw new ApiError("database_not_configured");
  }
  // Neon HTTPS SQL endpoint replaces the first hostname label, not /sql on ep-*.
  return `https://${url.hostname.replace(/^[^.]+\./, "api.")}/sql`;
}

export async function sql(env, query, params = []) {
  const endpoint = neonEndpoint(env.DATABASE_URL);
  const result = await fetchJson(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Neon-Connection-String": env.DATABASE_URL,
      "Neon-Raw-Text-Output": "true", "Neon-Array-Mode": "true" },
    body: JSON.stringify({ query, params: params.map(value => value === null ? null : String(value)) }),
  }, { maxBytes: 2 * 1048576 });
  if (!Array.isArray(result.fields) || !Array.isArray(result.rows) || result.rows.length > 400
      || result.fields.length > 32) throw new ApiError("database_contract_mismatch");
  const fields = result.fields;
  return result.rows.map(row => {
    if (!Array.isArray(row) || row.length !== fields.length) throw new ApiError("database_contract_mismatch");
    return Object.fromEntries(fields.map((field, index) => {
      let value = row[index];
      if (value !== null && [114, 3802].includes(field.dataTypeID) && typeof value === "string") {
        try { value = JSON.parse(value); } catch { throw new ApiError("database_contract_mismatch"); }
      } else if (value !== null && field.dataTypeID === 16) {
        if (![true, false, "t", "f", "true", "false"].includes(value)) throw new ApiError("database_contract_mismatch");
        value = [true, "t", "true"].includes(value);
      }
      return [field.name, value];
    }));
  });
}

export async function requireSnapshot(env) {
  if (!/^[a-f0-9]{64}$/.test(env.SNAPSHOT_ID || "") || !/^[a-f0-9]{64}$/.test(env.SNAPSHOT_MANIFEST_SHA256 || "")) {
    throw new ApiError("snapshot_not_configured");
  }
  const rows = await sql(env, "SELECT snapshot_id, manifest_sha256, state FROM image_archive_v2.snapshots WHERE snapshot_id=$1", [env.SNAPSHOT_ID]);
  if (rows.length !== 1 || rows[0].state !== "ready" || rows[0].manifest_sha256 !== env.SNAPSHOT_MANIFEST_SHA256) {
    throw new ApiError("snapshot_not_ready");
  }
}

const ITEM_COLUMNS = `item_id, group_id, representative_id, original_prompt, rights_json,
  metadata_json->'effective' AS metadata, metadata_json->>'review_status' AS metadata_review_status,
  human_note, text_ready, private_data->>'style_id' AS style_id,
  private_data->>'title' AS title, private_data->>'source_url' AS source_url,
  '/api/private/v2/images/' || item_id AS image_url`;

export async function itemsByIds(env, ids) {
  return sql(env, `SELECT ${ITEM_COLUMNS} FROM image_archive_v2.items
    WHERE snapshot_id=$1 AND item_id IN (SELECT jsonb_array_elements_text($2::jsonb)) ORDER BY item_id`,
  [env.SNAPSHOT_ID, JSON.stringify(ids)]);
}

export async function groupMembers(env, groupId, after = "", limit = 20) {
  return sql(env, `SELECT ${ITEM_COLUMNS} FROM image_archive_v2.items
    WHERE snapshot_id=$1 AND group_id=$2 AND item_id>$3 ORDER BY item_id LIMIT $4`,
  [env.SNAPSHOT_ID, groupId, after, limit + 1]);
}
