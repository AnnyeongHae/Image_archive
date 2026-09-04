from __future__ import annotations

import hashlib
import http.cookiejar
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path


ARCHIVE_ROOT = Path(__file__).resolve().parents[1]
MODULE_DIR = ARCHIVE_ROOT / "src" / "opennana"
sys.path.insert(0, str(MODULE_DIR))
sys.path.insert(0, str(ARCHIVE_ROOT / "src"))

from public_catalog_store import PublicCatalogStore  # noqa: E402
from review_server import (  # noqa: E402
    ADMIN_API_PREFIX,
    API_PREFIX,
    PUBLIC_API_PREFIX,
    AdminAccessPolicy,
    ReviewHttpServer,
    ReviewPaths,
    ReviewRequestHandler,
    ReviewService,
)


def build_review_fixture(root: Path) -> ReviewPaths:
    paths = ReviewPaths(root)
    queue = {
        "schema_version": "opennana-review-queue-1.0",
        "run_id": "fixture-run",
        "observed_at": "2026-09-01T00:00:00Z",
        "queue_revision": "fixture-revision",
        "summary": {"queued": 1},
        "items": [
            {
                "queue_id": "fixture-1",
                "source": "opennana",
                "upstream_id": "fixture-1",
                "content_sha256": "1" * 64,
                "rights": {"release_eligible": False},
            }
        ],
    }
    for path, value in (
        (paths.queue, queue),
        (paths.state, {}),
        (paths.config, {"source_id": "opennana"}),
        (paths.draft, {"schema_version": "opennana-decision-draft-1.0", "decisions": []}),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    (root / "platform.config.json").write_text(
        json.dumps({"rights_access_policy": {"public_tiers": ["P1", "P2"], "admin_only_tiers": ["P3", "P4"]}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return paths


def build_public_catalog_fixture(root: Path) -> None:
    export_root = root / "data" / "public-export"
    shards_root = export_root / "shards"
    shards_root.mkdir(parents=True, exist_ok=True)
    shard = {
        "schema_version": "public-image-prompt-shard-1.1",
        "shard_id": "catalog-0001",
        "record_count": 2,
        "records": [
            {
                "schema_version": "public-image-prompt-record-1.1",
                "catalog_key": "manual:REC-001",
                "style_id": "MAN-001",
                "record_id": "REC-001",
                "lane": "manual",
                "title": "Hero beverage burst",
                "source": {"name": "Manual", "url": "https://example.com/manual"},
                "rights": {"rights_tier": "P1", "portfolio_visibility": "public"},
                "prompt": {"available_in_public_export": True, "text_included": True, "text": "beverage hero burst"},
                "media": {"available_in_public_export": True, "assets": []},
                "taxonomy": {},
                "review_release": {"review_status": "needs_review", "release_eligible": False},
                "search_text": "hero beverage burst drink splash",
                "content_sha256": "a" * 64,
            },
            {
                "schema_version": "public-image-prompt-record-1.1",
                "catalog_key": "secret:REC-002",
                "style_id": "SCR-002",
                "record_id": "REC-002",
                "lane": "secret_codes",
                "title": "Character sticker sheet",
                "source": {"name": "Secret Codes", "url": "https://example.com/secret"},
                "rights": {"rights_tier": "P2", "portfolio_visibility": "metadata_link_only"},
                "prompt": {"available_in_public_export": False, "text_included": False},
                "media": {"available_in_public_export": False, "assets": []},
                "taxonomy": {},
                "review_release": {"review_status": "needs_review", "release_eligible": False},
                "search_text": "character sticker sheet avatar kawaii",
                "content_sha256": "b" * 64,
            },
        ],
    }
    index = {
        "schema_version": "public-image-prompt-index-1.1",
        "generated_at": "2026-09-01T00:00:00Z",
        "record_count": 2,
        "canonical_record_count": 20,
        "shard_count": 1,
        "style_id_count": 2,
        "prompt_text_included_count": 1,
        "media_asset_included_count": 0,
        "rights_policy": {"mode": "fail_closed"},
        "records": [
            {
                "catalog_key": "manual:REC-001",
                "lane": "manual",
                "record_id": "REC-001",
                "style_id": "MAN-001",
                "title": "Hero beverage burst",
                "source_name": "Manual",
                "source_url": "https://example.com/manual",
                "rights_tier": "P1",
                "search_text": "hero beverage burst drink splash",
                "shard_id": "catalog-0001",
            },
            {
                "catalog_key": "secret:REC-002",
                "lane": "secret_codes",
                "record_id": "REC-002",
                "style_id": "SCR-002",
                "title": "Character sticker sheet",
                "source_name": "Secret Codes",
                "source_url": "https://example.com/secret",
                "rights_tier": "P2",
                "search_text": "character sticker sheet avatar kawaii",
                "shard_id": "catalog-0001",
            },
        ],
    }
    (shards_root / "catalog-0001.json").write_text(json.dumps(shard, ensure_ascii=False), encoding="utf-8")
    (export_root / "catalog-index.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")


class PublicArchiveApiTests(unittest.TestCase):
    def start_server(self, root: Path, auth: AdminAccessPolicy) -> tuple[ReviewHttpServer, str]:
        paths = build_review_fixture(root)
        build_public_catalog_fixture(root)
        service = ReviewService(paths)
        handler = lambda *args, **kwargs: ReviewRequestHandler(*args, directory=root, **kwargs)  # noqa: E731
        server = ReviewHttpServer(
            ("127.0.0.1", 0),
            handler,
            service=service,
            static_root=root,
            allowed_origins=set(),
            public_catalog_store=PublicCatalogStore(root),
            admin_access_policy=auth,
        )
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        server.allowed_origins = {origin}
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # unittest cleanups run LIFO: stop/join serve_forever before closing its socket.
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        return server, origin

    def test_public_summary_search_and_detail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _, origin = self.start_server(Path(temp_dir), AdminAccessPolicy(mode="loopback_local_only"))
            with urllib.request.urlopen(f"{origin}{PUBLIC_API_PREFIX}/summary", timeout=5) as response:
                payload = json.loads(response.read())
            self.assertEqual(payload["record_count"], 2)
            self.assertEqual(payload["prompt_text_included_count"], 1)

            with urllib.request.urlopen(f"{origin}{PUBLIC_API_PREFIX}/records?q=beverage&limit=1", timeout=5) as response:
                payload = json.loads(response.read())
            self.assertEqual(payload["total"], 1)
            self.assertEqual(payload["records"][0]["style_id"], "MAN-001")

            with urllib.request.urlopen(f"{origin}{PUBLIC_API_PREFIX}/records/SCR-002", timeout=5) as response:
                payload = json.loads(response.read())
            self.assertEqual(payload["record"]["style_id"], "SCR-002")

            with self.assertRaises(urllib.error.HTTPError) as context:
                urllib.request.urlopen(f"{origin}{PUBLIC_API_PREFIX}/records?unknown=1", timeout=5)
            self.assertEqual(context.exception.code, 400)

    def test_admin_status_is_loopback_only_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _, origin = self.start_server(Path(temp_dir), AdminAccessPolicy(mode="loopback_local_only"))
            with urllib.request.urlopen(f"{origin}{ADMIN_API_PREFIX}/status", timeout=5) as response:
                payload = json.loads(response.read())
            self.assertEqual(payload["auth_mode"], "loopback_local_only")
            self.assertEqual(payload["review_queue"]["item_count"], 1)

    def test_admin_status_requires_bearer_token_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _, origin = self.start_server(
                Path(temp_dir),
                AdminAccessPolicy(mode="bearer_token", token_sha256=hashlib.sha256(b"secret-token").hexdigest()),
            )
            with self.assertRaises(urllib.error.HTTPError) as context:
                urllib.request.urlopen(f"{origin}{ADMIN_API_PREFIX}/status", timeout=5)
            self.assertEqual(context.exception.code, 403)

            request = urllib.request.Request(
                f"{origin}{ADMIN_API_PREFIX}/status",
                headers={"Authorization": "Bearer secret-token"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read())
            self.assertEqual(payload["auth_mode"], "bearer_token")


if __name__ == "__main__":
    unittest.main()
