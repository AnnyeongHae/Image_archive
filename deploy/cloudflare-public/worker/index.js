const ADMIN_ORIGIN = "https://image-prompt-archive-staging.andrew4may.workers.dev";
const PROTECTED_PUBLIC_PATHS = ["/approval-requests", "/approval-requests.html", "/source-admin", "/source-admin.html", "/duplicate-review", "/duplicate-review.html"];

function json(payload, status = 200) {
  return Response.json(payload, {
    status,
    headers: {
      "Cache-Control": "no-store",
      "Content-Type": "application/json; charset=utf-8",
      "X-Content-Type-Options": "nosniff",
      "X-Robots-Tag": "noindex, nofollow, noarchive",
    },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const lane = env.DEPLOYMENT_LANE || "public-awesome-gpt-image-2-mvp";
    if (request.method !== "GET" && request.method !== "HEAD") {
      return json({ error: "method_not_allowed" }, 405);
    }
    if (url.pathname === "/healthz") {
      return json({
        ok: true,
        service: "image-prompt-archive-public-staging",
        lane,
        public_records: Number(env.PUBLIC_RECORD_COUNT || 0),
        private_data_included: false,
        admin_data_included: false,
      });
    }
    if (url.pathname === "/api/public/v1/summary") {
      return json({
        ok: true,
        lane,
        collection: "awesome-gpt-image-2",
        public_records: Number(env.PUBLIC_RECORD_COUNT || 0),
      });
    }
    if (url.pathname === "/admin" || url.pathname.startsWith("/admin/")) {
      const target = new URL(url.pathname.replace(/^\/admin/, "/admin") || "/admin/", ADMIN_ORIGIN);
      return Response.redirect(target.toString(), 302);
    }
    if (PROTECTED_PUBLIC_PATHS.some((prefix) => url.pathname === prefix || url.pathname.startsWith(prefix + "/"))) {
      return json({ error: "not_found" }, 404);
    }
    if (url.pathname.startsWith("/api/")) {
      return json({ error: "not_found" }, 404);
    }
    return env.ASSETS.fetch(request);
  },
};
