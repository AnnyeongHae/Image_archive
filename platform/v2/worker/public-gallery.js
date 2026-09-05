/** Public static gallery only. No private DB, vector, token or model bindings. */
const BLOCKED_PREFIXES = [
  "/api", "/admin", "/approval-requests", "/source-admin", "/duplicate-review",
  "/.env", "/.git", "/data/private-research", "/candidate.json", "/grant.json",
];

function json(request, value, status = 200) {
  return new Response(request.method === "HEAD" ? null : JSON.stringify(value), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
      "X-Robots-Tag": "noindex, nofollow, noarchive",
    },
  });
}

export function releaseSummary(env) {
  if (!/^[a-f0-9]{64}$/.test(env.PUBLIC_RELEASE_ID || "")) return null;
  const names = ["PUBLIC_IMAGE_COUNT", "PUBLIC_GROUP_COUNT", "PUBLIC_VARIANT_COUNT"];
  if (names.some(name => !/^(0|[1-9][0-9]*)$/.test(String(env[name] ?? "")))) return null;
  const [images, groups, variants] = names.map(name => Number(env[name]));
  if (images < 1 || groups < 1 || groups > images || variants !== images - groups) return null;
  return {
    ok: true,
    service: "image-archive-public-gallery",
    gallery_version: 2,
    release_id: env.PUBLIC_RELEASE_ID,
    public_records: images,
    counts: { images, groups, variants },
    search: "local_keyword",
    private_data_included: false,
    api_credentials_included: false,
    rights_notice: "reference_display_only_individual_rights_unverified",
  };
}

export default {
  async fetch(request, env) {
    if (!["GET", "HEAD"].includes(request.method)) {
      return json(request, { error: "method_not_allowed" }, 405);
    }
    let path;
    try { path = decodeURIComponent(new URL(request.url).pathname); }
    catch { return json(request, { error: "not_found" }, 404); }
    if (path === "/healthz" || path === "/api/public/v2/summary") {
      const summary = releaseSummary(env);
      return summary ? json(request, summary) : json(request, { error: "release_not_configured" }, 503);
    }
    if (BLOCKED_PREFIXES.some(prefix => path === prefix || path.startsWith(`${prefix}/`) || path.startsWith(`${prefix}.`))
        || /(?:^|\/)\./.test(path) || path.includes("\\")) {
      return json(request, { error: "not_found" }, 404);
    }
    if (!releaseSummary(env)) return json(request, { error: "release_not_configured" }, 503);
    if (!env.ASSETS?.fetch) return json(request, { error: "assets_unavailable" }, 503);
    return env.ASSETS.fetch(request);
  },
};
