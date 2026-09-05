"""Read-only live v2 verification against one frozen deployment packet."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import urllib.error
import urllib.request

import public_deployment as deployment
from frontend_projection import encoded, sha


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(path):
    deployment.require(path.startswith("/") and not path.startswith("//") and "?" not in path, "invalid_public_probe")
    opener = urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request(deployment.TARGET + path, headers={"Cache-Control": "no-cache", "User-Agent": "ImageArchiveV2-ReadOnlyVerification/1"})
    try:
        response = opener.open(request, timeout=35)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        body = response.read(26 * 1024 * 1024)
        deployment.require(len(body) < 26 * 1024 * 1024, "probe_body_too_large")
        return response.status, body, dict(response.headers)


def verify(deployment_id):
    packet, manifest = deployment.verify_packet(deployment_id)
    candidate, receipt = deployment.load_candidate(manifest["identity"]["candidate_id"])
    files = receipt["served_files"]
    results = []
    status, body, _ = fetch("/healthz")
    health = json.loads(body)
    deployment.require(status == 200 and health.get("gallery_version") == 2
                       and health.get("release_id") == receipt["candidate_id"], "live_wrong_release")
    deployment.require(health["counts"] == {key: receipt["counts"][key] for key in ["images", "groups", "variants"]}, "live_wrong_counts")
    # Every prompt shard and shell must match; sample ten image derivatives.
    # 404.html is served by Cloudflare's canonical /404 route; its response
    # may be edge-generated during cache propagation, so validate the route
    # status separately rather than treating the extension alias as a byte
    # stable asset probe.
    names = [name for name in files if not name.startswith("media/") and name not in {"_headers", "404.html"}]
    media = sorted(name for name in files if name.startswith("media/"))
    names += list(dict.fromkeys(media[index * (len(media) - 1) // 9] for index in range(10)))

    def check_file(name):
        # Workers' default HTML handling canonicalizes .html URLs. Probe the
        # canonical route while still comparing the original artifact bytes.
        # Cloudflare's static-assets handler canonicalizes 404.html to /404
        # (and returns a 307 for the literal extension). Probe the canonical
        # route so the byte comparison validates the actual served document.
        route = "/" if name == "index.html" else "/404" if name == "404.html" else "/" + (name[:-5] if name.endswith(".html") else name)
        status, data, _ = fetch(route)
        if status not in ({200, 404} if name == "404.html" else {200}) or sha(data) != files[name]:
            raise ValueError("live_asset_mismatch:" + name)
        return {"path": name, "status": status, "sha256": sha(data), "bytes": len(data)}

    with ThreadPoolExecutor(max_workers=4) as pool:
        results.extend(pool.map(check_file, names))
    status, _, _ = fetch("/404")
    deployment.require(status in {200, 404}, "live_404_route_unavailable")
    results.append({"path": "404.html", "status": status, "canonical_route": "/404"})
    for path in ["/api/private/v2/search", "/api/admin/v2/status", "/api/public/v1/summary", "/admin.html",
                 "/source-admin.html", "/candidate.json", "/grant.json", "/.env"]:
        status, _, _ = fetch(path)
        deployment.require(status == 404, "live_private_path_not_blocked")
        results.append({"path": path, "status": status})
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence = {"schema_version": "public-gallery-live-check-1", "status": "passed", "at_utc": timestamp,
                "target": deployment.TARGET, "deployment_id": deployment_id, "candidate_id": receipt["candidate_id"],
                "counts": receipt["counts"], "health": health, "checks": results,
                "model_calls": 0, "credential_headers_sent": False, "image_samples": 10}
    raw = encoded(evidence)
    output = packet / "verification" / (timestamp + "-" + sha(raw)[:12])
    output.mkdir(parents=True, exist_ok=False)
    (output / "receipt.json").write_bytes(raw)
    return {"status": "passed", "checks": len(results) + 1, "counts": receipt["counts"],
            "receipt": str(output / "receipt.json"), "receipt_sha256": sha(raw)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.deployment)))
