from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock


ARCHIVE_ROOT = Path(__file__).resolve().parents[1]
MODULE_DIR = ARCHIVE_ROOT / "src" / "opennana"
sys.path.insert(0, str(MODULE_DIR))

import run_backlog_sync as backlog  # noqa: E402
import run_daily_sync as daily  # noqa: E402


ROBOTS = """User-agent: *
Allow: /api/prompts
Content-Signal: search=yes, ai-train=no, use=reference

User-agent: GPTBot
Disallow: /
"""


def config() -> dict:
    return {
        "schema_version": "opennana-config-1.0",
        "source": {
            "list_endpoint": "https://api.example.test/api/prompts",
            "detail_endpoint_template": "https://api.example.test/api/prompts/{slug}",
            "robots_url": "https://api.example.test/robots.txt",
        },
        "collection": {
            "access_type": 0,
            "page_size": 100,
            "sort": "reviewed_at",
            "order": "DESC",
            "requests_per_second": 1.0,
            "concurrency": 1,
            "timeout_seconds": 20,
            "user_agent": "BacklogSyncTest/1.0",
        },
        "policy": {
            "paid_prompt_body": "forbidden",
            "auto_publish": False,
        },
        "collect_paid_prompt_bodies": False,
        "public_release_allowed": False,
    }


def listing(number: int, *, reviewed_at: str | None = None) -> dict:
    return {
        "id": str(number),
        "slug": f"prompt-{number}",
        "title": f"Prompt {number}",
        "access_type": 0,
        "reviewed_at": reviewed_at or f"2026-09-01T00:{number % 60:02d}:00Z",
    }


def detail(number: int) -> dict:
    value = listing(number)
    value.update(
        {
            "is_unlocked": True,
            "model": "gpt-image-2",
            "prompts": f"Create a distinct private reference prompt number {number}.",
            "cover_image_url": f"https://images.example.test/{number}.jpg",
        }
    )
    return value


class FakeClient:
    def __init__(self, pages: dict[int, list[dict]], details: dict[str, dict]) -> None:
        self.pages = pages
        self.details = details
        self.request_count = 0
        self.urls: list[str] = []

    def get_text(self, url: str) -> str:
        self.request_count += 1
        self.urls.append(url)
        return ROBOTS

    def get_json(self, url: str):
        self.request_count += 1
        self.urls.append(url)
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path.endswith("/api/prompts"):
            return {"data": self.pages.get(int(query["page"][0]), [])}
        slug = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
        return {"data": self.details[slug]}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BacklogSyncTests(unittest.TestCase):
    def make_paths(self, root: Path, *, source_versions: dict[str, str] | None = None) -> daily.SyncPaths:
        data_root = root / "opennana"
        data_root.mkdir(parents=True)
        (data_root / "config.json").write_text(json.dumps(config()), encoding="utf-8")
        (data_root / "state.json").write_text(
            json.dumps(
                {
                    "schema_version": "opennana-state-1.0",
                    "source_versions": source_versions or {},
                }
            ),
            encoding="utf-8",
        )
        canonical = root / "canonical.jsonl"
        canonical.write_text("", encoding="utf-8")
        return daily.SyncPaths(
            data_root=data_root,
            canonical=canonical,
            review_js=root / "review-data.js",
        )

    def test_default_is_network_and_write_free_and_live_bound_is_required(self) -> None:
        script = MODULE_DIR / "run_backlog_sync.py"
        dry = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(dry.returncode, 0, dry.stderr)
        plan = json.loads(dry.stdout)
        self.assertFalse(plan["network"])
        self.assertFalse(plan["writes"])
        self.assertIsNone(plan["max_details"])
        partial = subprocess.run(
            [sys.executable, str(script), "--fetch", "--apply"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(partial.returncode, 2)
        self.assertIn("--max-details", partial.stderr)

    def test_bootstrap_uses_selected_detail_rows_not_observed_source_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root, source_versions={"never-fetched": "inventory-only"})
            raw_dir = paths.data_root / "raw"
            raw_dir.mkdir()
            fetch_raw = raw_dir / "fetch-20260901T000000Z.json"
            fetch_raw.write_text(
                json.dumps(
                    {
                        "run_id": "fetch-20260901T000000Z",
                        "observed_at": "2026-09-01T00:00:00Z",
                        "list_metadata": [listing(1), listing(99)],
                        "selected_list_metadata": [listing(1)],
                    }
                ),
                encoding="utf-8",
            )
            runs = paths.data_root / "runs"
            runs.mkdir()
            (runs / "pipeline-fetch-20260901T000000Z.json").write_text(
                json.dumps(
                    {
                        "run_id": "fetch-20260901T000000Z",
                        "status": "passed",
                        "exit_code": 0,
                        "raw_path": str(fetch_raw),
                    }
                ),
                encoding="utf-8",
            )
            changed = listing(1, reviewed_at="2026-09-02T00:00:00Z")
            (raw_dir / "daily-20260902T000000Z-b0001.json").write_text(
                json.dumps(
                    {
                        "run_id": "daily-20260902T000000Z-b0001",
                        "observed_at": "2026-09-02T00:00:00Z",
                        "list_metadata": [changed, listing(2)],
                        "selected_list_metadata": [changed, listing(2)],
                    }
                ),
                encoding="utf-8",
            )
            (runs / "batch-daily-20260902T000000Z-b0001.json").write_text(
                json.dumps(
                    {
                        "run_id": "daily-20260902T000000Z-b0001",
                        "status": "pipeline_complete",
                    }
                ),
                encoding="utf-8",
            )
            state = json.loads(paths.state.read_text(encoding="utf-8"))
            bootstrapped, report = backlog.bootstrap_detail_processed_versions(state, paths)
            processed = bootstrapped["detail_processed_versions"]
            self.assertEqual(set(processed), {"1", "2"})
            self.assertEqual(processed["1"], daily.metadata_version(changed))
            self.assertNotIn("99", processed)
            self.assertNotIn("never-fetched", processed)
            self.assertEqual(report["source"], "raw_completion_evidence")
            self.assertTrue(report["source_versions_ignored_for_detail_watermark"])

    def test_raw_only_artifact_is_not_counted_as_detail_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            raw_dir = paths.data_root / "raw"
            raw_dir.mkdir()
            (raw_dir / "fetch-20260901T010000Z.json").write_text(
                json.dumps(
                    {
                        "run_id": "fetch-20260901T010000Z",
                        "observed_at": "2026-09-01T01:00:00Z",
                        "selected_list_metadata": [listing(10)],
                    }
                ),
                encoding="utf-8",
            )
            state = json.loads(paths.state.read_text(encoding="utf-8"))
            bootstrapped, report = backlog.bootstrap_detail_processed_versions(state, paths)
            self.assertEqual(bootstrapped["detail_processed_versions"], {})
            self.assertEqual(report["source"], "raw_completion_evidence")
            self.assertEqual(report["raw_artifacts_discovered"], 1)
            self.assertEqual(report["raw_artifacts_scanned"], 0)
            self.assertEqual(report["raw_artifacts_excluded_without_completion"], 1)

    def test_existing_detail_ledger_is_trusted_without_raw_rescan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            state = json.loads(paths.state.read_text(encoding="utf-8"))
            state["detail_processed_versions"] = {}
            with mock.patch.object(
                backlog,
                "_raw_bootstrap_paths",
                side_effect=AssertionError("raw must not be scanned when ledger exists"),
            ):
                bootstrapped, report = backlog.bootstrap_detail_processed_versions(state, paths)
            self.assertEqual(bootstrapped["detail_processed_versions"], {})
            self.assertEqual(report["source"], "state")
            self.assertEqual(report["raw_artifacts_scanned"], 0)

    def test_more_than_100_is_chunked_checkpointed_and_reports_remaining(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            all_listed = [listing(number) for number in range(1, 251)]
            source_versions = {
                daily.source_id(item): daily.metadata_version(item) for item in all_listed
            }
            paths = self.make_paths(root, source_versions=source_versions)
            pages = {1: all_listed[:100], 2: all_listed[100:200], 3: all_listed[200:]}
            details = {f"prompt-{number}": detail(number) for number in range(1, 251)}
            client = FakeClient(pages, details)
            canonical_before = sha256_file(paths.canonical)

            result, exit_code = backlog.execute_backlog_sync(
                paths=paths,
                max_details=225,
                batch_size=100,
                client_factory=lambda _config: client,
            )

            self.assertEqual(exit_code, 0, result)
            self.assertEqual(result["summary"]["candidate_total"], 250)
            self.assertEqual(result["summary"]["selected"], 225)
            self.assertEqual(result["summary"]["batches_completed"], 3)
            self.assertEqual(result["summary"]["remaining_estimate"], 25)
            manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual([batch["selected"] for batch in manifest["batches"]], [100, 100, 25])
            self.assertEqual([batch["remaining_estimate"] for batch in manifest["batches"]], [150, 50, 25])
            durable = json.loads(paths.state.read_text(encoding="utf-8"))
            self.assertEqual(len(durable["detail_processed_versions"]), 225)
            self.assertEqual(durable["backlog_sync_checkpoint"]["remaining_estimate"], 25)
            self.assertNotIn("daily_sync_checkpoint", durable)
            self.assertEqual(sha256_file(paths.canonical), canonical_before)
            raw = sorted((paths.data_root / "raw").glob("backlog-*.json"))
            self.assertEqual(len(raw), 3)
            list_urls = [url for url in client.urls if "/api/prompts?" in url]
            self.assertTrue(list_urls)
            for url in list_urls:
                query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                self.assertEqual(query["sort"], ["reviewed_at"])
                self.assertEqual(query["order"], ["DESC"])

    def test_failed_batch_resumes_from_last_completed_detail_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            rows = [listing(number) for number in range(1, 6)]
            pages = {1: rows}
            details = {f"prompt-{number}": detail(number) for number in range(1, 6)}
            first_client = FakeClient(pages, details)
            real_normalize = daily.normalize_bundle
            normalize_calls = 0

            def fail_second(raw):
                nonlocal normalize_calls
                normalize_calls += 1
                if normalize_calls == 2:
                    raise RuntimeError("synthetic backlog normalize failure")
                return real_normalize(raw)

            with mock.patch.object(daily, "normalize_bundle", side_effect=fail_second):
                failed, failed_exit = backlog.execute_backlog_sync(
                    paths=paths,
                    max_details=5,
                    batch_size=2,
                    client_factory=lambda _config: first_client,
                )

            self.assertEqual(failed_exit, 1)
            self.assertEqual(failed["summary"]["batches_completed"], 1)
            self.assertEqual(failed["summary"]["remaining_estimate"], 3)
            after_failure = json.loads(paths.state.read_text(encoding="utf-8"))
            completed_ids = set(after_failure["detail_processed_versions"])
            self.assertEqual(len(completed_ids), 2)
            self.assertEqual(after_failure["backlog_sync_checkpoint"]["remaining_estimate"], 3)

            resume_client = FakeClient(pages, details)
            resumed, resumed_exit = backlog.execute_backlog_sync(
                paths=paths,
                max_details=5,
                batch_size=2,
                client_factory=lambda _config: resume_client,
            )
            self.assertEqual(resumed_exit, 0, resumed)
            self.assertEqual(resumed["summary"]["candidate_total"], 3)
            self.assertEqual(resumed["summary"]["selected"], 3)
            self.assertEqual(resumed["summary"]["batches_completed"], 2)
            self.assertEqual(resumed["summary"]["remaining_estimate"], 0)
            detail_urls = [
                url
                for url in resume_client.urls
                if "/api/prompts/" in url and not url.endswith("robots.txt")
            ]
            resumed_ids = {
                urllib.parse.urlparse(url).path.rsplit("-", 1)[-1] for url in detail_urls
            }
            self.assertEqual(len(detail_urls), 3)
            self.assertTrue(completed_ids.isdisjoint(resumed_ids))
            final_state = json.loads(paths.state.read_text(encoding="utf-8"))
            self.assertEqual(set(final_state["detail_processed_versions"]), {"1", "2", "3", "4", "5"})


if __name__ == "__main__":
    unittest.main()
