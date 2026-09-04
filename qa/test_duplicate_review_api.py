from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ARCHIVE_ROOT = Path(__file__).resolve().parents[1]
MODULE_DIR = ARCHIVE_ROOT / "src" / "opennana"
SRC_ROOT = ARCHIVE_ROOT / "src"
sys.path.insert(0, str(MODULE_DIR))
sys.path.insert(0, str(SRC_ROOT))

from duplicate_review_store import DuplicateGroupNotFound, DuplicateIndexUnavailable  # noqa: E402
from review_server import ReviewHttpServer, ReviewPaths, ReviewRequestHandler, ReviewService  # noqa: E402


class FakeDuplicateStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.failure: Exception | None = None

    def _fail(self) -> None:
        if self.failure is not None:
            raise self.failure

    def summary(self) -> dict:
        self._fail()
        self.calls.append(("summary", None))
        return {
            "schema_version": "duplicate-analysis-summary-1.0",
            "counts": {"group_count": 2, "member_count": 5},
            "artifacts": {"sqlite": {"bytes": 512}},
        }

    def list_groups(self, **kwargs: object) -> dict:
        self._fail()
        self.calls.append(("list", kwargs))
        return {
            "schema_version": "duplicate-group-list-1.0",
            "kind": kwargs.get("kind") or "all",
            "limit": kwargs["limit"],
            "offset": kwargs["offset"],
            "total": 1,
            "groups": [{
                "group_id": "DG-한글-1",
                "kind": "exact_media",
                "member_count": 2,
                "display_title": "Fixture",
                "thumbnail_uris": ["/fixture-thumb.webp"],
            }],
        }

    def group_detail(self, group_id: str, **kwargs: object) -> dict:
        self._fail()
        self.calls.append(("detail", {"group_id": group_id, **kwargs}))
        return {
            "schema_version": "duplicate-group-detail-1.0",
            "group": {"group_id": group_id, "kind": "exact_media", "member_count": 2},
            "limit": kwargs["limit"],
            "offset": kwargs["offset"],
            "total": 2,
            "members": [],
        }


class DuplicateApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "index.html").write_text("<!doctype html><title>fixture</title>", encoding="utf-8")
        self.store = FakeDuplicateStore()
        handler = lambda *args, **kwargs: ReviewRequestHandler(*args, directory=self.root, **kwargs)  # noqa: E731
        self.server = ReviewHttpServer(
            ("127.0.0.1", 0),
            handler,
            service=ReviewService(ReviewPaths(self.root)),
            static_root=self.root,
            allowed_origins=set(),
            duplicate_store=self.store,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()

    def get(self, path: str) -> tuple[int, dict, dict[str, str], bytes]:
        request = urllib.request.Request(self.base_url + path, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                raw = response.read()
                return response.status, json.loads(raw), dict(response.headers), raw
        except urllib.error.HTTPError as error:
            raw = error.read()
            return error.code, json.loads(raw), dict(error.headers), raw

    def test_summary_is_read_only_compact_json(self) -> None:
        status, body, headers, raw = self.get("/api/duplicates/v1/summary")
        self.assertEqual(status, 200)
        self.assertEqual(body["counts"]["group_count"], 2)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn(b"\n", raw)
        self.assertEqual(self.store.calls, [("summary", None)])

    def test_group_list_forwards_bounded_query(self) -> None:
        query = urllib.parse.urlencode({
            "kind": "perceptual_candidate",
            "limit": "50",
            "offset": "20",
            "q": "CASE 431",
            "sort": "score_desc",
        })
        status, body, _, _ = self.get(f"/api/duplicates/v1/groups?{query}")
        self.assertEqual(status, 200)
        self.assertEqual(body["limit"], 50)
        self.assertEqual(self.store.calls[-1], ("list", {
            "kind": "perceptual_candidate",
            "limit": 50,
            "offset": 20,
            "q": "CASE 431",
            "sort": "score_desc",
        }))

    def test_group_list_defaults_to_twenty(self) -> None:
        status, _, _, _ = self.get("/api/duplicates/v1/groups")
        self.assertEqual(status, 200)
        self.assertEqual(self.store.calls[-1][1]["limit"], 20)
        self.assertEqual(self.store.calls[-1][1]["offset"], 0)

    def test_invalid_or_repeated_query_fails_before_store(self) -> None:
        for path in (
            "/api/duplicates/v1/groups?limit=51",
            "/api/duplicates/v1/groups?offset=-1",
            "/api/duplicates/v1/groups?limit=1&limit=2",
            "/api/duplicates/v1/groups?unknown=1",
        ):
            with self.subTest(path=path):
                self.store.calls.clear()
                status, body, _, _ = self.get(path)
                self.assertEqual(status, 400)
                self.assertEqual(body["schema_version"], "duplicate-review-api-error-1.0")
                self.assertEqual(body["error"]["code"], "invalid_query")
                self.assertTrue(body["read_only"])
                self.assertEqual(self.store.calls, [])

    def test_group_detail_decodes_id_and_caps_limit(self) -> None:
        group_id = urllib.parse.quote("DG-한글-1")
        status, body, _, _ = self.get(f"/api/duplicates/v1/groups/{group_id}?limit=50&offset=0")
        self.assertEqual(status, 200)
        self.assertEqual(body["group"]["group_id"], "DG-한글-1")
        self.assertEqual(self.store.calls[-1], ("detail", {"group_id": "DG-한글-1", "limit": 50, "offset": 0}))

    def test_missing_index_and_group_are_json_errors(self) -> None:
        self.store.failure = DuplicateIndexUnavailable("private path must not escape")
        status, body, _, _ = self.get("/api/duplicates/v1/summary")
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "duplicate_index_unavailable")
        self.assertNotIn("private path", body["error"]["message"])

        self.store.failure = DuplicateGroupNotFound("missing")
        status, body, _, _ = self.get("/api/duplicates/v1/groups/missing")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "duplicate_group_not_found")


if __name__ == "__main__":
    unittest.main()
