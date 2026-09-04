from __future__ import annotations

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

from build_review_queue import queue_revision_for_items  # noqa: E402
from common import read_json, stable_json  # noqa: E402
from review_server import (  # noqa: E402
    ReviewApiError,
    ReviewHttpServer,
    ReviewPaths,
    ReviewRequestHandler,
    ReviewService,
    loopback_origins,
)


def queue_item(number: int) -> dict:
    content_hash = f"{number:064x}"
    return {
        "queue_id": f"ONN-FIXTURE-{number}",
        "source": "opennana",
        "upstream_id": str(number),
        "slug": f"fixture-{number}",
        "title": f"Fixture {number}",
        "source_url": f"https://opennana.com/awesome-prompt-gallery/fixture-{number}",
        "author": None,
        "model": "gpt-image-2",
        "tags": ["fixture"],
        "media_type": "image",
        "image_urls": [f"https://example.invalid/{number}.png"],
        "prompt_text": f"Fixture prompt {number}",
        "prompt_preview": f"Fixture prompt {number}",
        "prompt_sha256": f"{number + 100:064x}",
        "content_sha256": content_hash,
        "updated_at": "2026-08-31T00:00:00Z",
        "dedupe": {"classification": "new", "matches": []},
        "rights": {"release_eligible": False, "item_rights": "unverified"},
        "workflow_status": "queued_for_review",
    }


def build_fixture(root: Path) -> tuple[ReviewPaths, dict, dict]:
    paths = ReviewPaths(root)
    items = [queue_item(1), queue_item(2)]
    queue = {
        "schema_version": "opennana-review-queue-1.0",
        "run_id": "fixture-run",
        "observed_at": "2026-08-31T00:00:00Z",
        "queue_revision": queue_revision_for_items(items),
        "summary": {"queued": 2, "classification_counts": {"new": 2}},
        "items": items,
    }
    draft = {
        "schema_version": "opennana-decision-draft-1.0",
        "run_id": queue["run_id"],
        "queue_revision": queue["queue_revision"],
        "decided_at": "2026-08-31T01:00:00Z",
        "decisions": [
            {
                "queue_id": items[0]["queue_id"],
                "content_sha256": items[0]["content_sha256"],
                "decision": "approve",
                "group_with": None,
                "note": "approved fixture",
            },
            {
                "queue_id": items[1]["queue_id"],
                "content_sha256": items[1]["content_sha256"],
                "decision": "defer",
                "group_with": None,
                "note": "deferred fixture",
            },
        ],
    }
    config = {
        "source_id": "opennana-awesome-prompt-gallery",
        "source_name": "OpenNana",
        "source_url": "https://opennana.com/awesome-prompt-gallery",
        "collection": {},
        "policy": {"auto_publish": False, "download_source_images": False},
    }
    for path, value in (
        (paths.queue, queue),
        (paths.state, {}),
        (paths.config, config),
        (paths.draft, draft),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(stable_json(value), encoding="utf-8")
    paths.review_js.parent.mkdir(parents=True, exist_ok=True)
    paths.review_js.write_text("window.OPENNANA_REVIEW_QUEUE = {};\n", encoding="utf-8")
    (root / "index.html").write_text("<!doctype html><title>fixture</title>", encoding="utf-8")
    return paths, queue, draft


class ReviewServiceTests(unittest.TestCase):
    def test_state_exposes_current_durable_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths, queue, _ = build_fixture(Path(temp_dir))
            service = ReviewService(paths)
            state = service.state()
            self.assertEqual(state["queue_revision"], queue["queue_revision"])
            self.assertEqual(state["durable_draft"]["status"], "ready")
            self.assertEqual(state["durable_draft"]["decision_count"], 2)

    def test_save_draft_persists_partial_current_queue_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths, queue, draft = build_fixture(Path(temp_dir))
            service = ReviewService(paths)
            partial = {
                "schema_version": "opennana-decision-draft-1.0",
                "run_id": queue["run_id"],
                "queue_revision": queue["queue_revision"],
                "decisions": [draft["decisions"][0]],
            }
            saved = service.save_draft(partial)
            self.assertEqual(saved["status"], "saved")
            self.assertEqual(saved["decision_count"], 1)
            stored = read_json(paths.draft)
            self.assertEqual(len(stored["decisions"]), 1)
            self.assertEqual(stored["decisions"][0]["queue_id"], draft["decisions"][0]["queue_id"])

    def test_preview_requires_complete_exact_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths, _, draft = build_fixture(Path(temp_dir))
            service = ReviewService(paths)
            incomplete = dict(draft)
            incomplete["decisions"] = draft["decisions"][:1]
            with self.assertRaises(ReviewApiError) as context:
                service.preview(incomplete, session_id="session")
            self.assertEqual(context.exception.code, "decision_set_incomplete")

            pending = json.loads(json.dumps(draft))
            pending["decisions"][0]["decision"] = "pending"
            with self.assertRaises(ReviewApiError) as context:
                service.preview(pending, session_id="session")
            self.assertEqual(context.exception.code, "pending_decisions")

    def test_preview_rejects_stale_hash_and_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths, _, draft = build_fixture(Path(temp_dir))
            service = ReviewService(paths)
            stale_hash = json.loads(json.dumps(draft))
            stale_hash["decisions"][0]["content_sha256"] = "f" * 64
            with self.assertRaises(ReviewApiError) as context:
                service.preview(stale_hash, session_id="session")
            self.assertIn(context.exception.code, {"stale_queue_revision", "decision_validation_failed"})

            stale_revision = json.loads(json.dumps(draft))
            stale_revision["queue_revision"] = "stale"
            with self.assertRaises(ReviewApiError) as context:
                service.preview(stale_revision, session_id="session")
            self.assertEqual(context.exception.code, "stale_queue_revision")

    def test_commit_applies_once_and_retry_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths, queue, draft = build_fixture(Path(temp_dir))
            service = ReviewService(paths)
            self.assertFalse(service.state()["internal_archive_auto_promotion"])
            preview = service.preview(draft, session_id="session")
            result = service.commit(
                draft,
                decision_batch_id=preview["decision_batch_id"],
                commit_token=preview["commit_token"],
                session_id="session",
            )
            self.assertEqual(result["status"], "committed")
            self.assertFalse(result["idempotent"])
            self.assertEqual(result["counts"]["approve"], 1)
            self.assertEqual(result["counts"]["defer"], 1)
            self.assertEqual(result["promoted_internal_archive"], 1)
            self.assertEqual(result["remaining_queued"], 0)
            self.assertFalse(result["public_release_effect"])
            self.assertEqual(read_json(paths.queue)["items"], [])
            self.assertEqual(read_json(paths.state)["review_decision_counts"], {"approve": 1, "defer": 1})
            pending_files = list((paths.data_root / "staging").glob("canonicalization-pending-*.json"))
            self.assertEqual(len(pending_files), 1)
            pending = read_json(pending_files[0])
            self.assertEqual(pending["record_count"], 1)
            self.assertFalse(pending["public_release_eligible"])
            self.assertTrue(all(record["rights"]["release_eligible"] is False for record in pending["records"]))
            history_files = list((paths.queue.parent / "history").glob("*.json"))
            self.assertEqual(len(history_files), 1)
            self.assertEqual(read_json(history_files[0])["queue_revision"], queue["queue_revision"])

            retried = service.commit(
                draft,
                decision_batch_id=preview["decision_batch_id"],
                commit_token=preview["commit_token"],
                session_id="new-session-after-reload",
            )
            self.assertTrue(retried["idempotent"])
            self.assertEqual(read_json(paths.state)["review_decision_counts"], {"approve": 1, "defer": 1})

    def test_invalid_token_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths, _, draft = build_fixture(Path(temp_dir))
            service = ReviewService(paths)
            preview = service.preview(draft, session_id="session")
            with self.assertRaises(ReviewApiError) as context:
                service.commit(
                    draft,
                    decision_batch_id=preview["decision_batch_id"],
                    commit_token="wrong-token",
                    session_id="session",
                )
            self.assertEqual(context.exception.code, "commit_token_invalid")
            self.assertEqual(len(read_json(paths.queue)["items"]), 2)

    def test_failed_promotion_rolls_back_decision_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths, queue, draft = build_fixture(root)
            hook = root / "fail_hook.py"
            hook.write_text("import sys\nsys.stderr.write('hook failed')\nraise SystemExit(7)\n", encoding="utf-8")
            state_before = paths.state.read_bytes()
            queue_before = paths.queue.read_bytes()
            service = ReviewService(paths, promotion_command=[sys.executable, str(hook)])
            preview = service.preview(draft, session_id="session")
            with self.assertRaises(ReviewApiError) as context:
                service.commit(
                    draft,
                    decision_batch_id=preview["decision_batch_id"],
                    commit_token=preview["commit_token"],
                    session_id="session",
                )
            self.assertEqual(context.exception.code, "commit_apply_failed")
            self.assertEqual(paths.state.read_bytes(), state_before)
            self.assertEqual(paths.queue.read_bytes(), queue_before)
            self.assertEqual(read_json(paths.queue)["queue_revision"], queue["queue_revision"])
            self.assertEqual(list((paths.data_root / "staging").glob("canonicalization-pending-*.json")), [])
            self.assertEqual(list((paths.queue.parent / "history").glob("*.json")), [])

    def test_promotion_hook_receives_private_boundary_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths, _, draft = build_fixture(root)
            hook = root / "success_hook.py"
            hook.write_text(
                "import json, os, pathlib\n"
                "target = pathlib.Path(os.environ['OPENNANA_ARCHIVE_ROOT']) / 'hook-env.json'\n"
                "target.write_text(json.dumps({\n"
                "  'batch': os.environ['OPENNANA_DECISION_BATCH_ID'],\n"
                "  'pending': os.environ['OPENNANA_PENDING_PATH'],\n"
                "  'public': os.environ['OPENNANA_PUBLIC_RELEASE_ALLOWED'],\n"
                "}), encoding='utf-8')\n"
                "print(json.dumps({'lane_updated': True, 'promoted_from_trigger': 1, 'record_count': 99, 'public_release_effect': False}))\n",
                encoding="utf-8",
            )
            service = ReviewService(paths, promotion_command=[sys.executable, str(hook)])
            preview = service.preview(draft, session_id="session")
            result = service.commit(
                draft,
                decision_batch_id=preview["decision_batch_id"],
                commit_token=preview["commit_token"],
                session_id="session",
            )
            hook_env = json.loads((root / "hook-env.json").read_text(encoding="utf-8"))
            self.assertEqual(hook_env["batch"], preview["decision_batch_id"])
            self.assertEqual(hook_env["public"], "0")
            self.assertTrue(Path(hook_env["pending"]).exists())
            self.assertEqual(result["promotion"]["status"], "succeeded")
            self.assertEqual(result["promoted_internal_archive"], 1)
            self.assertFalse(result["promotion"]["public_release_effect"])

    def test_hook_reporting_public_release_true_fails_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths, _, draft = build_fixture(root)
            hook = root / "unsafe_hook.py"
            hook.write_text(
                "import json\nprint(json.dumps({'public_release_eligible': True}))\n",
                encoding="utf-8",
            )
            queue_before = paths.queue.read_bytes()
            service = ReviewService(paths, promotion_command=[sys.executable, str(hook)])
            preview = service.preview(draft, session_id="session")
            with self.assertRaises(ReviewApiError) as context:
                service.commit(
                    draft,
                    decision_batch_id=preview["decision_batch_id"],
                    commit_token=preview["commit_token"],
                    session_id="session",
                )
            self.assertEqual(context.exception.code, "commit_apply_failed")
            self.assertEqual(paths.queue.read_bytes(), queue_before)


class ReviewHttpTests(unittest.TestCase):
    def test_loopback_origins_include_ipv4_hostname_and_ipv6(self) -> None:
        self.assertEqual(
            loopback_origins(8765),
            {
                "http://127.0.0.1:8765",
                "http://localhost:8765",
                "http://[::1]:8765",
            },
        )

    def test_same_origin_session_preview_and_no_cors_header(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths, _, draft = build_fixture(root)
            service = ReviewService(paths)
            handler = lambda *args, **kwargs: ReviewRequestHandler(*args, directory=root, **kwargs)  # noqa: E731
            server = ReviewHttpServer(("127.0.0.1", 0), handler, service=service, static_root=root, allowed_origins=set())
            port = server.server_address[1]
            origin = f"http://127.0.0.1:{port}"
            server.allowed_origins = {origin}
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                cookie_jar = http.cookiejar.CookieJar()
                opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
                with opener.open(f"{origin}/api/review/v1/state", timeout=5) as response:
                    state = json.loads(response.read())
                    self.assertEqual(response.status, 200)
                    self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
                request = urllib.request.Request(
                    f"{origin}/api/review/v1/draft",
                    data=stable_json({"decisions": [draft["decisions"][0]]}).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Origin": origin,
                        "X-Review-CSRF": state["csrf_token"],
                    },
                    method="POST",
                )
                with opener.open(request, timeout=5) as response:
                    saved = json.loads(response.read())
                    self.assertEqual(saved["status"], "saved")
                    self.assertEqual(saved["decision_count"], 1)

                request = urllib.request.Request(
                    f"{origin}/api/review/v1/preview",
                    data=stable_json(draft).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Origin": origin,
                        "X-Review-CSRF": state["csrf_token"],
                    },
                    method="POST",
                )
                with opener.open(request, timeout=5) as response:
                    preview = json.loads(response.read())
                    self.assertEqual(preview["status"], "preview_ready")
                    self.assertEqual(preview["decision_count"], 2)

                denied = urllib.request.Request(
                    f"{origin}/api/review/v1/preview",
                    data=stable_json(draft).encode("utf-8"),
                    headers={"Content-Type": "application/json", "X-Review-CSRF": state["csrf_token"]},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as context:
                    opener.open(denied, timeout=5)
                self.assertEqual(context.exception.code, 403)
                denied_body = json.loads(context.exception.read())
                self.assertEqual(denied_body["error"]["code"], "same_origin_required")

                commit_request = urllib.request.Request(
                    f"{origin}/api/review/v1/commit",
                    data=stable_json({
                        "decision_batch_id": preview["decision_batch_id"],
                        "commit_token": preview["commit_token"],
                        "decisions": draft,
                    }).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Origin": origin,
                        "X-Review-CSRF": state["csrf_token"],
                    },
                    method="POST",
                )
                with opener.open(commit_request, timeout=5) as response:
                    committed = json.loads(response.read())
                    self.assertEqual(committed["status"], "committed")
                    self.assertEqual(committed["remaining_queued"], 0)
                    self.assertFalse(committed["public_release_effect"])

                with opener.open(commit_request, timeout=5) as response:
                    retried = json.loads(response.read())
                    self.assertTrue(retried["idempotent"])

                with opener.open(f"{origin}/index.html", timeout=5) as response:
                    self.assertIn(b"fixture", response.read())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
