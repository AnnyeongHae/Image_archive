"""Freeze/verify a public Worker packet and serve its exact assets on loopback.

No credentials, network or deployment calls. A packet is pending until a human
release decision binds its digest and the item-level reference-display scope.
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

import frontend_projection as projection
import frontend_preview as preview

ROOT = projection.ROOT
WORKER_NAME = "image-prompt-archive-public-staging"
TARGET = "https://photoposting.shop"
ACCOUNT = "b39fad7b5ebf74e820209ed506fd989b"
PACKET_BASE = "data/private-research/platform-v2/public-deployments"
CANDIDATE_BASE = "data/private-research/v2/p"
STATIC_NAMES = {"index.html", "gallery.css", "gallery.js", "gallery-core.mjs", "notice.html",
                "privacy.html", "404.html", "robots.txt", "_headers", "data/catalog.json", "THIRD_PARTY_NOTICES.txt"}


def require(value, code):
    if not value:
        raise ValueError(code)


def load_candidate(candidate_id):
    require(projection._valid_hash(candidate_id), "invalid_candidate_id")
    directory = projection.local_path(ROOT, f"{CANDIDATE_BASE}/{candidate_id}", private=True)
    receipt = json.loads(projection.local_path(directory, "candidate.json").read_bytes())
    require(receipt["candidate_id"] == candidate_id == projection.sha(projection.encoded(receipt["identity"])), "candidate_identity_mismatch")
    require(receipt["schema_version"] == "image-gallery-public-reference-candidate-1" and receipt["mode"] == "public", "invalid_candidate_schema")
    require(receipt["assets_directory"] == "assets", "invalid_asset_root")
    files = receipt["served_files"]
    require(files == receipt["identity"]["served_files"], "manifest_mismatch")
    require(STATIC_NAMES.issubset(files) and len(files) <= 20000, "invalid_static_file_set")
    for name, digest in files.items():
        require(name in STATIC_NAMES or bool(re.fullmatch(r"data/groups/[a-f0-9]{64}\.json", name))
                or bool(re.fullmatch(r"media/[A-Za-z0-9_./-]+\.(?:webp|jpe?g|png)", name)), "unexpected_public_asset")
        path = projection.local_path(directory / "assets", name)
        require(path.stat().st_size < 25 * 1024 * 1024, "asset_too_large")
        require(projection.sha(path.read_bytes()) == digest, "asset_hash_mismatch")
    actual = {p.relative_to(directory / "assets").as_posix() for p in (directory / "assets").rglob("*") if p.is_file()}
    require(actual == set(files), "unregistered_asset")
    require(projection.sha(projection.local_path(directory, "grant.json").read_bytes()) == receipt["grant_sha256"]
            == receipt["identity"]["grant_sha256"], "grant_hash_mismatch")
    catalog = json.loads((directory / "assets/data/catalog.json").read_bytes())
    require(catalog["mode"] == "public" and catalog["counts"] == receipt["counts"], "catalog_mismatch")
    return directory, receipt


def stage(candidate_id, *, apply=False):
    candidate, receipt = load_candidate(candidate_id)
    worker = (ROOT / "platform/v2/worker/public-gallery.js").read_bytes()
    counts = receipt["counts"]
    # Asset-first serves only this strict, verified public file set. Protected
    # namespaces always invoke the Worker; no DB/R2/model/token binding exists.
    config = {
        "name": WORKER_NAME, "account_id": ACCOUNT, "main": "worker.js",
        "compatibility_date": "2026-09-04", "workers_dev": True, "preview_urls": False,
        "routes": [{"pattern": "photoposting.shop", "custom_domain": True}],
        "vars": {"PUBLIC_RELEASE_ID": candidate_id, "PUBLIC_IMAGE_COUNT": str(counts["images"]),
                 "PUBLIC_GROUP_COUNT": str(counts["groups"]), "PUBLIC_VARIANT_COUNT": str(counts["variants"])},
        "assets": {"directory": f"../../../v2/p/{candidate_id}/assets", "binding": "ASSETS",
                   "not_found_handling": "404-page",
                   "run_worker_first": ["/healthz", "/api*", "/admin*", "/approval-requests*", "/source-admin*",
                                        "/duplicate-review*", "/.env*", "/.git*", "/candidate.json*", "/grant.json*",
                                        "/data/private-research*"]},
        "observability": {"enabled": False},
    }
    config_raw = projection.encoded(config)
    identity = {"schema_version": "image-gallery-public-deployment-1", "target": TARGET, "worker_name": WORKER_NAME,
                "candidate_id": candidate_id, "candidate_receipt_sha256": projection.sha((candidate / "candidate.json").read_bytes()),
                "grant_sha256": receipt["grant_sha256"], "worker_sha256": projection.sha(worker),
                "config_sha256": projection.sha(config_raw), "counts": counts}
    digest = projection.sha(projection.encoded(identity))
    packet = ROOT / PACKET_BASE / digest
    require((packet / config["assets"]["directory"]).resolve() == (candidate / "assets").resolve(), "wrong_assets_directory")
    manifest = {"deployment_id": digest, "identity": identity, "status": "pending_human_release",
                "previous_worker_version": "b426a4d9-3bde-4dc6-a47b-c58da630e883",
                "new_model_calls": 0, "public_search_calls_models": False, "license_verified": False,
                "public_reference_scope_requires_human_approval": True}
    if apply:
        packet.mkdir(parents=True, exist_ok=True)
        for name, raw in {"worker.js": worker, "wrangler.json": config_raw, "deployment.json": projection.encoded(manifest)}.items():
            path = packet / name
            if path.exists():
                require(path.read_bytes() == raw, "immutable_packet_conflict")
            else:
                with path.open("xb") as handle:
                    handle.write(raw)
    return {"status": "pending_human_release" if apply else "dry_run", "deployment_id": digest,
            "path": packet.relative_to(ROOT).as_posix(), "candidate_id": candidate_id,
            "target": TARGET, "counts": counts}


def verify_packet(deployment_id):
    require(projection._valid_hash(deployment_id), "invalid_deployment_id")
    packet = projection.local_path(ROOT, f"{PACKET_BASE}/{deployment_id}", private=True)
    manifest = json.loads((packet / "deployment.json").read_bytes())
    identity = manifest["identity"]
    require(projection.sha(projection.encoded(identity)) == deployment_id == manifest["deployment_id"], "deployment_hash_mismatch")
    candidate, receipt = load_candidate(identity["candidate_id"])
    require(projection.sha((candidate / "candidate.json").read_bytes()) == identity["candidate_receipt_sha256"], "receipt_hash_mismatch")
    for name, field in [("worker.js", "worker_sha256"), ("wrangler.json", "config_sha256")]:
        require(projection.sha((packet / name).read_bytes()) == identity[field], "packet_hash_mismatch")
    config = json.loads((packet / "wrangler.json").read_bytes())
    require((packet / config["assets"]["directory"]).resolve() == (candidate / "assets").resolve(), "wrong_assets_directory")
    require(identity["target"] == TARGET and identity["worker_name"] == WORKER_NAME, "wrong_target")
    return packet, manifest


def serve(candidate_id, port):
    directory, receipt = load_candidate(candidate_id)
    assets = directory / "assets"
    files = {name: digest for name, digest in receipt["served_files"].items() if name != "_headers"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.respond(False)

        def do_HEAD(self):
            self.respond(True)

        def respond(self, head):
            host = f"127.0.0.1:{self.server.server_port}"
            trusted = (self.headers.get_all("Host") == [host]
                       and self.headers.get("Origin", f"http://{host}") == f"http://{host}"
                       and self.headers.get("Sec-Fetch-Site", "none") in {"none", "same-origin"})
            parsed = urlsplit(self.path)
            name = "index.html" if parsed.path == "/" else unquote(parsed.path).removeprefix("/")
            status, body, mime = 404, b"not_found", "text/plain; charset=utf-8"
            if not trusted:
                status, body = 403, b"forbidden"
            elif not parsed.scheme and not parsed.netloc and not parsed.query and name in files:
                path = projection.local_path(assets, name)
                body = path.read_bytes()
                if projection.sha(body) == files[name]:
                    status, mime = 200, preview.MIME.get(path.suffix, "text/plain; charset=utf-8")
                else:
                    status, body = 409, b"asset_changed"
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", preview.CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if not head:
                try:
                    self.wfile.write(body)
                except (ConnectionResetError, BrokenPipeError):
                    pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    print(json.dumps({"status": "ready", "url": f"http://127.0.0.1:{server.server_port}/", "publicly_deployed": False}), flush=True)
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["stage", "verify", "preview"])
    parser.add_argument("--candidate")
    parser.add_argument("--deployment")
    parser.add_argument("--port", type=int, default=8966)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.command == "stage":
        print(json.dumps(stage(args.candidate, apply=args.apply)))
    elif args.command == "verify":
        packet, manifest = verify_packet(args.deployment)
        print(json.dumps({"verified": True, "deployment_id": manifest["deployment_id"], "publication_approved": False}))
    else:
        serve(args.candidate, args.port)


if __name__ == "__main__":
    main()
