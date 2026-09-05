"""Read-only, loopback-only server for one hash-bound gallery-v2 bundle.

This is a local review surface, not a production server or a publication gate.
Only files enumerated by the build receipt are served. No workspace browsing,
credential loading, upstream fetch, database connection or mutation endpoints.
"""
from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[3]
SHA = re.compile(r"^[a-f0-9]{64}$")
MIME = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8", ".webp": "image/webp",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".svg": "image/svg+xml",
}
SHELL = {"index.html", "gallery.css", "gallery.js", "gallery-core.mjs", "favicon.svg"}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'none'")


class PreviewError(ValueError):
    """Safe, fixed error codes without private source data."""


def identity_sha(identity):
    raw = (json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def safe_resource_name(name: str) -> bool:
    if (not isinstance(name, str) or not name or "\\" in name or "%" in name
            or "?" in name or "#" in name or ":" in name
            or any(ord(char) < 33 for char in name)):
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {".", "..", ""} for part in name.split("/")):
        return False
    if name in SHELL or name == "data/catalog.json":
        return True
    if re.fullmatch(r"data/groups/[a-f0-9]{16,64}\.json", name):
        return True
    return bool(re.fullmatch(r"(?:media|assets)/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+\.(?:webp|jpe?g|png)", name))


def bound_file(directory: Path, name: str) -> Path:
    target = directory
    for part in PurePosixPath(name).parts:
        target = target / part
        if target.is_symlink() or (hasattr(target, "is_junction") and target.is_junction()):
            raise PreviewError("linked_resource_refused")
    resolved = target.resolve(strict=True)
    if not resolved.is_relative_to(directory) or not resolved.is_file():
        raise PreviewError("resource_outside_bundle")
    return resolved


def make_handler(bundle: Path) -> type[BaseHTTPRequestHandler]:
    original = Path(bundle).absolute()
    for part in (original, *original.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise PreviewError("linked_bundle_refused")
    bundle = original.resolve(strict=True)
    receipt = json.loads(bound_file(bundle, "build-receipt.json").read_text(encoding="utf-8"))
    files = receipt.get("served_files")
    if not isinstance(files, dict) or not files or len(files) > 10000:
        raise PreviewError("invalid_served_file_manifest")
    if not {"index.html", "data/catalog.json"}.issubset(files):
        raise PreviewError("required_resources_missing")
    allowed = {}
    for name, expected in files.items():
        if not safe_resource_name(name) or not isinstance(expected, str) or not SHA.fullmatch(expected):
            raise PreviewError("invalid_served_resource")
        path = bound_file(bundle, name)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise PreviewError("resource_hash_mismatch")
        allowed["/" + name] = (name, expected, MIME[path.suffix.lower()])
    catalog = json.loads(bound_file(bundle, "data/catalog.json").read_text(encoding="utf-8"))
    if catalog.get("schema_version") != "image-gallery-2" or catalog.get("mode") not in {"public", "private_local_preview"}:
        raise PreviewError("invalid_gallery_mode")
    identity = receipt.get("identity")
    build_id = receipt.get("build_id")
    if (receipt.get("schema_version") != "image-gallery-build-2" or not isinstance(identity, dict)
            or not isinstance(build_id, str) or not SHA.fullmatch(build_id)
            or identity.get("schema_version") != receipt["schema_version"]
            or identity_sha(identity) != build_id or bundle.name != build_id
            or identity.get("served_files") != files):
        raise PreviewError("invalid_build_identity")
    mode = catalog["mode"]
    if (identity.get("mode") != mode or receipt.get("mode") != mode
            or receipt.get("counts") != catalog.get("counts")
            or (mode == "private_local_preview" and receipt.get("status") != "ready")
            or (mode == "public" and (receipt.get("status") != "blocked" or catalog.get("groups") != []))):
        raise PreviewError("receipt_catalog_scope_mismatch")
    if mode == "public" and (set(files) - SHELL - {"data/catalog.json"}):
        raise PreviewError("blocked_public_bundle_has_private_resources")

    class Handler(BaseHTTPRequestHandler):
        server_version = "GalleryPreview/2"
        sys_version = ""

        def log_message(self, format, *args):
            # Requests can contain user search text. Do not log URLs or headers.
            pass

        def respond(self, status: int, data: bytes, mime="text/plain; charset=utf-8", *, head=False):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            self.end_headers()
            if not head:
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def trusted_request(self):
            port = self.server.server_port
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            host = self.headers.get("Host", "")
            if len(self.headers.get_all("Host", [])) != 1 or host not in hosts:
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin != "http://" + host:
                return False
            return self.headers.get("Sec-Fetch-Site", "none") in {"none", "same-origin"}

        def serve_resource(self, *, head=False):
            if not self.trusted_request():
                self.respond(403, b"forbidden", head=head)
                return
            try:
                parsed = urlsplit(self.path)
                if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
                    raise PreviewError("invalid_path")
                target = unquote(parsed.path, errors="strict")
                if target == "/":
                    target = "/index.html"
                entry = allowed.get(target)
                if entry is None:
                    self.respond(404, b"not_found", head=head)
                    return
                name, expected, mime = entry
                data = bound_file(bundle, name).read_bytes()
                if hashlib.sha256(data).hexdigest() != expected:
                    raise PreviewError("resource_hash_mismatch")
            except (ValueError, OSError, UnicodeError):
                self.respond(409, b"bundle_unavailable", head=head)
                return
            self.respond(200, data, mime, head=head)

        def do_GET(self):
            self.serve_resource()

        def do_HEAD(self):
            self.serve_resource(head=True)

        def reject_method(self):
            self.respond(405, b"read_only")

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = reject_method

    return Handler


def create_server(bundle: Path, port: int = 0):
    if not 0 <= port <= 65535:
        raise PreviewError("invalid_port")
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(bundle))
    server.daemon_threads = True
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    bundle = args.bundle if args.bundle.is_absolute() else ROOT / args.bundle
    try:
        server = create_server(bundle, args.port)
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc) if isinstance(exc, PreviewError) else "invalid_bundle_or_port"}))
        return 1
    print(json.dumps({"status": "ready", "read_only": True, "loopback_only": True,
                      "url": f"http://127.0.0.1:{server.server_port}/"}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
