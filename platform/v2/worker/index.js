import { verifyAccessOwner, verifyApiToken } from "./auth.js";
import { ApiError, json, readJsonBounded } from "./http.js";
import { requireSnapshot, groupMembers, sql } from "./neon.js";
import { queryVector, searchGroups, TEXT_MODEL, TEXT_DIMENSION } from "./retrieval.js";
import { privateImage } from "./media.js";

export function searchInput(body) {
  if (!body || typeof body !== "object" || Array.isArray(body)
      || Object.keys(body).some(key => !["query", "query_id", "top"].includes(key))) throw new ApiError("invalid_search", 400);
  const top = body.top ?? 5;
  if (![1, 3, 5].includes(top) || (body.query === undefined) === (body.query_id === undefined)) throw new ApiError("invalid_search", 400);
  if (body.query_id !== undefined) {
    if (typeof body.query_id !== "string" || !/^[A-Za-z0-9_.:-]{1,100}$/.test(body.query_id)) throw new ApiError("invalid_query_id", 400);
    return { query_id: body.query_id, top };
  }
  if (typeof body.query !== "string" || body.query !== body.query.trim() || body.query.length < 2
      || body.query.length > 500 || new TextEncoder().encode(body.query).byteLength > 2000
      || /[\u0000-\u001F\u007F]/.test(body.query)) throw new ApiError("invalid_query", 400);
  return { query: body.query, top };
}

async function rateLimit(env, principal) {
  if (!env.OWNER_RATE_LIMITER?.limit) throw new ApiError("rate_limit_not_configured");
  let result;
  try { result = await env.OWNER_RATE_LIMITER.limit({ key: `owner:${principal.id}` }); }
  catch { throw new ApiError("rate_limit_unavailable"); }
  if (result?.success !== true) throw new ApiError("rate_limited", 429);
}

export default {
  async fetch(request, env) {
    try {
      const url = new URL(request.url);
      if (url.pathname === "/healthz" && request.method === "GET") return json({ ok: true, service: "image-archive-owner-api", version: "2.0.0" });
      if (url.pathname.startsWith("/api/admin/v2/")) {
        await verifyAccessOwner(request, env, "admin:read");
        if (url.pathname !== "/api/admin/v2/status" || request.method !== "GET") throw new ApiError("not_found", 404);
        await requireSnapshot(env);
        return json({ snapshot_id: env.SNAPSHOT_ID, private_library_ready: true, human_review_location: "local",
          new_query_embedding_enabled: env.LIVE_QUERY_EMBEDDING_ENABLED === "true", public_promotion_enabled: false });
      }
      if (!url.pathname.startsWith("/api/private/v2/")) throw new ApiError("not_found", 404);
      const isSearch = url.pathname === "/api/private/v2/search";
      const principal = await verifyApiToken(request, env, isSearch ? "rag:search" : "archive:read");
      if (env.PRIVATE_API_ENABLED !== "true") throw new ApiError("private_api_disabled");
      await rateLimit(env, principal);
      if (isSearch) {
        if (request.method !== "POST") throw new ApiError("method_not_allowed", 405);
        if (!/^application\/json(?:\s*;|$)/i.test(request.headers.get("content-type") || "")) throw new ApiError("json_required", 415);
        let body;
        try { body = await readJsonBounded(request, 4096); }
        catch { throw new ApiError("invalid_search_body", 400); }
        const input = searchInput(body);
        await requireSnapshot(env);
        const query = await queryVector(env, principal, input);
        const results = await searchGroups(env, query.vector, input.top);
        return json({ snapshot_id: env.SNAPSHOT_ID, model: TEXT_MODEL, dimension: TEXT_DIMENSION,
          modality: "usage_text", requested_groups: input.top, returned_groups: results.length,
          usage: query.usage, results, metadata_notice: "Luna metadata candidates; not human-approved metadata or public-use permission." });
      }
      if (request.method !== "GET") throw new ApiError("method_not_allowed", 405);
      await requireSnapshot(env);
      if (url.pathname === "/api/private/v2/queries") {
        const rows = await sql(env, "SELECT query_id,query_text FROM image_archive_v2.query_vectors WHERE snapshot_id=$1 ORDER BY query_id LIMIT 100", [env.SNAPSHOT_ID]);
        return json({ snapshot_id: env.SNAPSHOT_ID, queries: rows });
      }
      const imageMatch = url.pathname.match(/^\/api\/private\/v2\/images\/([^/]+)$/);
      if (imageMatch) {
        let itemId;
        try { itemId = decodeURIComponent(imageMatch[1]); } catch { throw new ApiError("invalid_item", 400); }
        return await privateImage(env, itemId);
      }
      const match = url.pathname.match(/^\/api\/private\/v2\/groups\/([^/]+)$/);
      if (match) {
        let groupId;
        try { groupId = decodeURIComponent(match[1]); } catch { throw new ApiError("invalid_group", 400); }
        const after = url.searchParams.get("after") || "";
        if (!/^[A-Za-z0-9_.:-]{1,160}$/.test(groupId) || (after && !/^[A-Za-z0-9_.:-]{1,160}$/.test(after))) throw new ApiError("invalid_group", 400);
        const rows = await groupMembers(env, groupId, after);
        const hasMore = rows.length > 20;
        return json({ group_id: groupId, members: rows.slice(0, 20), next_cursor: hasMore ? rows[19].item_id : null });
      }
      throw new ApiError("not_found", 404);
    } catch (error) {
      // Never echo exception strings, upstream bodies, tokens, query text or DSNs.
      const status = Number.isInteger(error?.status) && error.status >= 400 && error.status <= 599 ? error.status : 503;
      const code = typeof error?.code === "string" && /^[a-z_]{1,80}$/.test(error.code) ? error.code : "service_unavailable";
      return json({ error: code }, status);
    }
  },
};
