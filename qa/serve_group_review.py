"""Read-only, loopback-only browser QA surface for one private review run.

This never accesses a browser profile, imports decisions, or serves the workspace.
Only the named HTML and the preview images bound by its spec are allowlisted.
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


def make_handler(workflow_dir: Path) -> type[BaseHTTPRequestHandler]:
    workflow_dir = workflow_dir.resolve(strict=True)
    run_dir = workflow_dir.parent
    spec = json.loads((workflow_dir / "image-group-workflow.spec.json").read_text(encoding="utf-8"))
    allowed = {"/group-workflow-v1/image-group-workflow.html": (
        workflow_dir / "image-group-workflow.html", "text/html; charset=utf-8")}
    for item in spec["items"]:
        path = (workflow_dir / item["prepared_path"]).resolve(strict=True)
        relative = path.relative_to(run_dir)
        if relative.parts[0] != "inputs" or path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise ValueError("only spec-bound prepared preview images can be served")
        mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}[path.suffix.lower()]
        allowed["/" + relative.as_posix()] = (path, mime)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            target = unquote(urlsplit(self.path).path)
            entry = allowed.get(target)
            if entry is None:
                self.send_error(404)
                return
            path, mime = entry
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow-dir", type=Path, required=True)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(args.workflow_dir))
    print(json.dumps({"qa_only": True, "read_only": True,
                      "url": f"http://127.0.0.1:{server.server_port}/group-workflow-v1/image-group-workflow.html"}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
