import { ApiError, fetchJson, finiteVector, sha256 } from "./http.js";
import { sql, itemsByIds } from "./neon.js";

export const TEXT_MODEL = "voyage-4-lite";
export const TEXT_DIMENSION = 512;
const queryCache = new Map();
const TTL = 10 * 60 * 1000;

function boundedSetting(value, fallback, maximum) {
  if (value === undefined || value === "") return fallback;
  if (!/^\d+$/.test(String(value))) throw new ApiError("invalid_budget_configuration");
  const number = Number(value);
  if (number < 1 || number > maximum) throw new ApiError("invalid_budget_configuration");
  return number;
}

export async function queryVector(env, principal, { query, query_id }) {
  if (query_id !== undefined) {
    const rows = await sql(env, `SELECT query_id, query_text, model, dimension, vector_json
      FROM image_archive_v2.query_vectors WHERE snapshot_id=$1 AND query_id=$2`, [env.SNAPSHOT_ID, query_id]);
    if (rows.length !== 1) throw new ApiError("query_not_found", 404);
    const row = rows[0];
    if (row.model !== TEXT_MODEL || Number(row.dimension) !== TEXT_DIMENSION) throw new ApiError("query_model_mismatch");
    return { vector: finiteVector(row.vector_json, TEXT_DIMENSION), query_text: row.query_text,
      usage: { mode: "stored_query", provider_calls: 0, actual_tokens: 0 } };
  }

  const queryHash = await sha256(query);
  const cacheKey = `${env.SNAPSHOT_ID}|${principal.id}|${TEXT_MODEL}|${queryHash}`;
  const cached = queryCache.get(cacheKey);
  if (cached && cached.expires > Date.now()) {
    return { vector: cached.vector, query_text: query,
      usage: { mode: "warm_isolate_cache", provider_calls: 0, actual_tokens: 0 } };
  }
  if (env.LIVE_QUERY_EMBEDDING_ENABLED !== "true" || !env.VOYAGE_API_KEY) {
    throw new ApiError("new_query_embedding_disabled", 503);
  }
  const callCap = boundedSetting(env.DAILY_QUERY_CALL_LIMIT, 20, 100);
  const tokenCap = boundedSetting(env.DAILY_QUERY_TOKEN_LIMIT, 40000, 200000);
  // Conservative UTF-8 byte upper budget, not an exact token estimate. No refunds
  // on timeout: an upstream may have processed an uncertain request.
  const reserved = new TextEncoder().encode(query).byteLength + 256;
  const requestId = crypto.randomUUID();
  const reservation = await sql(env, `WITH budget AS (
    INSERT INTO image_archive_v2.api_daily_budget (usage_day,model,reserved_calls,reserved_tokens)
    SELECT (now() AT TIME ZONE 'UTC')::date,$1,1,$2::bigint WHERE $2::bigint <= $4::bigint
      AND EXISTS (SELECT 1 FROM image_archive_v2.api_model_guard WHERE model=$1 AND blocked=false)
    ON CONFLICT (usage_day,model) DO UPDATE SET
      reserved_calls=image_archive_v2.api_daily_budget.reserved_calls+1,
      reserved_tokens=image_archive_v2.api_daily_budget.reserved_tokens+EXCLUDED.reserved_tokens
    WHERE image_archive_v2.api_daily_budget.reserved_calls<$3::int
      AND image_archive_v2.api_daily_budget.reserved_tokens+EXCLUDED.reserved_tokens<=$4::bigint
    RETURNING model
  ) INSERT INTO image_archive_v2.api_query_receipts
    (request_id,token_id,query_sha256,model,reserved_tokens,state)
    SELECT $5::uuid,$6,$7,model,$2::int,'reserved' FROM budget RETURNING request_id`,
  [TEXT_MODEL, reserved, callCap, tokenCap, requestId, principal.id, queryHash]);
  if (reservation.length !== 1) throw new ApiError("daily_query_budget_exhausted", 429);

  try {
    const result = await fetchJson("https://api.voyageai.com/v1/embeddings", {
      method: "POST", headers: { "Content-Type": "application/json", "Authorization": `Bearer ${env.VOYAGE_API_KEY}` },
      body: JSON.stringify({ model: TEXT_MODEL, input: [query], input_type: "query", output_dimension: TEXT_DIMENSION }),
    });
    if (result.model !== TEXT_MODEL || result.data?.length !== 1 || result.data[0].index !== 0
      || !Number.isInteger(result.usage?.total_tokens) || result.usage.total_tokens < 0) {
      throw new ApiError("embedding_response_mismatch");
    }
    const vector = finiteVector(result.data[0].embedding, TEXT_DIMENSION);
    const actual = result.usage.total_tokens;
    await sql(env, `WITH observed AS (
      UPDATE image_archive_v2.api_query_receipts SET state='observed',actual_tokens=$2::int
      WHERE request_id=$1::uuid AND state='reserved' RETURNING model,reserved_tokens,created_at
    ), reconciled AS (
      UPDATE image_archive_v2.api_daily_budget b SET reserved_tokens=b.reserved_tokens+GREATEST(0,$2::bigint-o.reserved_tokens)
      FROM observed o WHERE b.model=o.model AND b.usage_day=(o.created_at AT TIME ZONE 'UTC')::date RETURNING b.model
    ) UPDATE image_archive_v2.api_model_guard SET blocked=true,reason='token_bound_exceeded',updated_at=now()
      WHERE model IN (SELECT model FROM observed WHERE $2::bigint>reserved_tokens)`, [requestId, actual]);
    if (actual > reserved) throw new ApiError("query_token_bound_exceeded");
    queryCache.delete(cacheKey);
    queryCache.set(cacheKey, { vector, expires: Date.now() + TTL });
    while (queryCache.size > 128) queryCache.delete(queryCache.keys().next().value);
    return { vector, query_text: query, usage: { mode: "new_query_embedding", provider_calls: 1,
      actual_tokens: actual, reserved_tokens: reserved, request_id: requestId } };
  } catch (error) {
    await sql(env, "UPDATE image_archive_v2.api_query_receipts SET state='uncertain' WHERE request_id=$1::uuid AND state='reserved'", [requestId]).catch(() => {});
    throw error;
  }
}

export function qdrantBase(env) {
  let endpoint;
  try { endpoint = new URL(env.QDRANT_ENDPOINT); } catch { throw new ApiError("qdrant_not_configured"); }
  if (endpoint.protocol !== "https:" || endpoint.username || endpoint.password || endpoint.pathname !== "/"
      || endpoint.search || endpoint.hash || (endpoint.port && endpoint.port !== "6333")
      || !/^[a-z0-9-]+(?:\.[a-z0-9-]+)*\.cloud\.qdrant\.io$/.test(endpoint.hostname)
      || !env.QDRANT_API_KEY) throw new ApiError("qdrant_not_configured");
  if (!/^image_archive_v2_[a-f0-9]{12,64}_text512$/.test(env.TEXT_COLLECTION || "")) {
    throw new ApiError("text_collection_not_configured");
  }
  return endpoint.origin;
}

export async function searchGroups(env, vector, top) {
  const result = await fetchJson(`${qdrantBase(env)}/collections/${env.TEXT_COLLECTION}/points/query/groups`, {
    method: "POST", headers: { "api-key": env.QDRANT_API_KEY, "Content-Type": "application/json" },
    body: JSON.stringify({ query: finiteVector(vector, TEXT_DIMENSION), group_by: "group_id", group_size: 1,
      limit: top, with_payload: ["item_id", "group_id", "representative_id", "snapshot_id", "image_approved"],
      with_vector: false, filter: { must: [
        { key: "snapshot_id", match: { value: env.SNAPSHOT_ID } },
        { key: "image_approved", match: { value: true } },
      ] }, params: { exact: false, hnsw_ef: 64 } }),
  });
  const groups = result.result?.groups;
  if (!Array.isArray(groups) || groups.length > top) throw new ApiError("retrieval_contract_mismatch");
  const seen = new Set();
  const candidates = groups.map(group => {
    const hit = group.hits?.[0]; const payload = hit?.payload;
    if (!hit || !Number.isFinite(hit.score) || !payload || typeof group.id !== "string" || seen.has(group.id)
        || payload.group_id !== group.id || payload.snapshot_id !== env.SNAPSHOT_ID || payload.image_approved !== true
        || typeof payload.item_id !== "string" || typeof payload.representative_id !== "string") {
      throw new ApiError("retrieval_contract_mismatch");
    }
    seen.add(group.id);
    return { group_id: group.id, score: hit.score, matched_item_id: payload.item_id, representative_id: payload.representative_id };
  });
  if (!candidates.length) return [];
  const ids = [...new Set(candidates.flatMap(row => [row.representative_id, row.matched_item_id]))];
  const rows = await itemsByIds(env, ids); const byId = new Map(rows.map(row => [row.item_id, row]));
  return candidates.map(candidate => {
    const representative = byId.get(candidate.representative_id), matched = byId.get(candidate.matched_item_id);
    if (!representative || !matched || representative.group_id !== candidate.group_id || matched.group_id !== candidate.group_id
        || representative.representative_id !== representative.item_id || matched.representative_id !== representative.item_id) {
      throw new ApiError("group_hydration_mismatch");
    }
    return { ...candidate, representative, members_url: `/api/private/v2/groups/${encodeURIComponent(candidate.group_id)}` };
  });
}

export function resetQueryCacheForTests() { queryCache.clear(); }
