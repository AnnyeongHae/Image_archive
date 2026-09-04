import { privateLibraryResponse } from "./private-library.js";
import { AuthError, verifyAccessOwner } from "../../../platform/v2/worker/auth.js";

const COMMON_HEADERS = {
  "Cache-Control": "no-store",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "X-Robots-Tag": "noindex, nofollow, noarchive",
};

const CONTENT_SECURITY_POLICY = [
  "default-src 'none'",
  "style-src 'self'",
  "script-src 'self'",
  "img-src 'self' data:",
  "connect-src 'self'",
  "base-uri 'none'",
  "form-action 'none'",
  "frame-ancestors 'none'",
].join("; ");

function jsonResponse(payload, status = 200) {
  return Response.json(payload, {
    status,
    headers: {
      ...COMMON_HEADERS,
      "Content-Type": "application/json; charset=utf-8",
    },
  });
}

function withSafetyHeaders(response) {
  const headers = new Headers(response.headers);
  for (const [name, value] of Object.entries(COMMON_HEADERS)) {
    headers.set(name, value);
  }
  headers.set("Content-Security-Policy", CONTENT_SECURITY_POLICY);
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

export async function verifyAccessJwt(request, env) {
  // Keep the old export name for callers, but require explicit owner identity.
  // API bearer tokens never authenticate this administrator deployment.
  return verifyAccessOwner(request, env, "admin:read");
}

function methodNotAllowed() {
  return jsonResponse({ error: "method_not_allowed" }, 405);
}

function accessDenied(error) {
  const status = error instanceof AuthError && error.status === 503 ? 503 : 403;
  const code = status === 503
    ? (error.code === "access_not_configured" ? "access_not_configured" : "access_unavailable")
    : "access_denied";
  return jsonResponse({ error: code }, status);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    try {
      await verifyAccessJwt(request, env);
    } catch (error) {
      return accessDenied(error);
    }

    if (request.method !== "GET" && request.method !== "HEAD") {
      return methodNotAllowed();
    }

    if (url.pathname === "/healthz") {
      return jsonResponse({
        ok: true,
        service: "image-prompt-archive-staging",
        lane: env.DEPLOYMENT_LANE,
        public_records: Number(env.PUBLIC_RECORD_COUNT),
        private_data_included: env.PRIVATE_LIBRARY_ENABLED === "true",
        private_library_read_enabled: env.PRIVATE_LIBRARY_ENABLED === "true",
        admin_enabled: env.ADMIN_ENABLED === "true",
        access_jwt_validated: true,
        r2_binding_present: Boolean(env.ARCHIVE_MEDIA),
        neon_or_hyperdrive_bound: false,
      });
    }

    if (url.pathname === "/api/public/v1/summary") {
      return jsonResponse({
        total: Number(env.PUBLIC_RECORD_COUNT),
        release_state: "fail_closed",
        note: "No archive records are published by this infrastructure canary.",
      });
    }

    if (url.pathname === "/api/admin/v1/status") {
      if (env.ADMIN_ENABLED !== "true") {
        return jsonResponse({ error: "admin_disabled" }, 503);
      }
      return jsonResponse(
        {
          ok: true,
          lane: env.DEPLOYMENT_LANE,
          access_jwt_validated: true,
          private_records: env.PRIVATE_LIBRARY_ENABLED === "true" ? null : 0,
          private_record_count_status: env.PRIVATE_LIBRARY_ENABLED === "true" ? "read_pinned_library_for_verified_count" : "disabled",
          private_library_read_enabled: env.PRIVATE_LIBRARY_ENABLED === "true",
          private_media_objects: 0,
          r2_binding_present: Boolean(env.ARCHIVE_MEDIA),
          neon_or_hyperdrive_bound: false,
          mutation_enabled: false,
          next_gate: env.PRIVATE_LIBRARY_ENABLED === "true"
            ? "private read-only library configured; media serving and durable approval writes are not enabled"
            : "explicitly approve a pinned private library canary before activation",
        },
        200,
      );
    }

    if (url.pathname === "/api/admin/v1/library") {
      return privateLibraryResponse(request, env);
    }

    if (url.pathname.startsWith("/api/admin/")) {
      return jsonResponse({ error: "not_found" }, 404);
    }

    if (url.pathname === "/admin") {
      return Response.redirect(`${url.origin}/admin/`, 308);
    }

    return withSafetyHeaders(await env.ASSETS.fetch(request));
  },
};
