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
        "source_id": "opennana-awesome-prompt-gallery",
        "source_name": "OpenNana Awesome Prompt Gallery",
        "source_url": "https://example.test/gallery",
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
            "user_agent": "DailySyncTest/1.0",
            "canary_max_details": 20,
        },
        "daily_sync": {
            "enabled": True,
            "mode": "forward_only_from_baseline",
            "historical_backfill": False,
        },
        "policy": {
            "paid_prompt_body": "forbidden",
            "auto_publish": False,
            "download_source_images": False,
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


def detail(number: int, prompt: str | None = None) -> dict:
    value = listing(number)
    value.update(
        {
            "is_unlocked": True,
            "model": "gpt-image-2",
            "prompts": prompt or f"Create a distinct reference image numbered {number} with clean studio light.",
            "cover_image_url": f"https://images.example.test/{number}.jpg",
        }
    )
    return value


class FakeClient:
    def __init__(self, pages: dict[int, list[dict]], details: dict[str, dict], robots: str = ROBOTS) -> None:
        self.pages = pages
        self.details = details
        self.robots = robots
        self.request_count = 0
        self.urls: list[str] = []

    def get_text(self, url: str) -> str:
        self.request_count += 1
        self.urls.append(url)
        return self.robots

    def get_json(self, url: str):
        self.request_count += 1
        self.urls.append(url)
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path.endswith("/api/prompts"):
            page = int(query["page"][0])
            return {"data": self.pages.get(page, [])}
        slug = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1])
        return {"data": self.details[slug]}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DailySyncTests(unittest.TestCase):
    def make_paths(self, root: Path, *, baselined: bool = True) -> daily.SyncPaths:
        data_root = root / "opennana"
        data_root.mkdir(parents=True)
        (data_root / "config.json").write_text(json.dumps(config()), encoding="utf-8")
        state = {"schema_version": "opennana-state-1.0", "source_versions": {}}
        if baselined:
            state["forward_baseline"] = {
                "schema_version": "opennana-forward-baseline-1.0",
                "mode": "forward_only_from_baseline",
                "status": "established",
                "established_at": "2026-08-31T00:00:00Z",
                "listed_unique": 1,
            }
        (data_root / "state.json").write_text(
            json.dumps(state),
            encoding="utf-8",
        )
        canonical = root / "canonical.jsonl"
        canonical.write_text("", encoding="utf-8")
        return daily.SyncPaths(data_root=data_root, canonical=canonical, review_js=root / "review-data.js")

    def test_default_is_write_free_and_partial_activation_fails_closed(self) -> None:
        script = MODULE_DIR / "run_daily_sync.py"
        dry = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, check=False)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        plan = json.loads(dry.stdout)
        self.assertFalse(plan["network"])
        self.assertFalse(plan["writes"])
        self.assertFalse(plan["automatic_decision_apply"])
        partial = subprocess.run(
            [sys.executable, str(script), "--fetch", "--apply"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(partial.returncode, 2)
        self.assertIn("--fetch --apply", partial.stderr)
        self.assertIn("--baseline-only", partial.stderr)

    def test_forward_baseline_reads_only_lists_and_preserves_pipeline_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root, baselined=False)
            state = json.loads(paths.state.read_text(encoding="utf-8"))
            state["source_versions"] = {"historical-disappeared": "keep-this-version"}
            paths.state.write_text(json.dumps(state), encoding="utf-8")

            paths.queue.parent.mkdir(parents=True, exist_ok=True)
            paths.queue.write_text('{"items":[{"upstream_id":"pending"}]}', encoding="utf-8")
            paths.draft.parent.mkdir(parents=True, exist_ok=True)
            paths.draft.write_text('{"decisions":{"pending":"hold"}}', encoding="utf-8")
            paths.review_js.write_text("window.QUEUE = 'unchanged';", encoding="utf-8")
            raw_sentinel = paths.data_root / "raw" / "existing.json"
            raw_sentinel.parent.mkdir(parents=True, exist_ok=True)
            raw_sentinel.write_text('{"preserve":true}', encoding="utf-8")

            protected = [paths.canonical, paths.queue, paths.draft, paths.review_js, raw_sentinel]
            before = {path: sha256_file(path) for path in protected}
            client = FakeClient({1: [listing(1), listing(2)]}, {})
            result, exit_code = daily.execute_forward_baseline(
                paths=paths,
                client_factory=lambda _config: client,
            )

            self.assertEqual(exit_code, 0, result)
            self.assertEqual(result["summary"]["detail_requests"], 0)
            self.assertEqual(result["summary"]["newly_baselined"], 2)
            self.assertEqual(
                [url for url in client.urls if "/api/prompts/" in url and not url.endswith("robots.txt")],
                [],
            )
            self.assertEqual({path: sha256_file(path) for path in protected}, before)
            self.assertEqual(sorted((paths.data_root / "raw").glob("*.json")), [raw_sentinel])

            durable = json.loads(paths.state.read_text(encoding="utf-8"))
            self.assertEqual(
                set(durable["source_versions"]),
                {"historical-disappeared", "1", "2"},
            )
            self.assertTrue(daily.forward_baseline_ready(durable))
            self.assertEqual(durable["forward_baseline"]["detail_requests"], 0)

    def test_forward_only_daily_sync_fails_before_network_when_baseline_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root, baselined=False)
            factory_called = False

            def factory(_config):
                nonlocal factory_called
                factory_called = True
                return FakeClient({}, {})

            result, exit_code = daily.execute_daily_sync(paths=paths, client_factory=factory)

            self.assertEqual(exit_code, 1)
            self.assertEqual(result["status"], "failed")
            self.assertIn("establish the inventory baseline", result["summary"]["error"])
            self.assertFalse(factory_called)

    def test_after_baseline_only_new_or_list_metadata_changed_details_are_fetched(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root, baselined=False)
            baseline_client = FakeClient({1: [listing(1), listing(2)]}, {})
            baseline_result, baseline_exit = daily.execute_forward_baseline(
                paths=paths,
                client_factory=lambda _config: baseline_client,
            )
            self.assertEqual(baseline_exit, 0, baseline_result)

            unchanged = listing(1)
            changed = listing(2, reviewed_at="2026-09-02T00:00:00Z")
            new = listing(3)
            daily_client = FakeClient(
                {1: [unchanged, changed, new]},
                {"prompt-2": detail(2), "prompt-3": detail(3)},
            )
            result, exit_code = daily.execute_daily_sync(
                paths=paths,
                client_factory=lambda _config: daily_client,
            )

            self.assertEqual(exit_code, 0, result)
            self.assertEqual(result["summary"]["unchanged_excluded"], 1)
            self.assertEqual(result["summary"]["changed_or_new"], 2)
            detail_urls = [
                url
                for url in daily_client.urls
                if "/api/prompts/" in url and not url.endswith("robots.txt")
            ]
            self.assertEqual(
                [urllib.parse.urlparse(url).path.rsplit("/", 1)[-1] for url in detail_urls],
                ["prompt-2", "prompt-3"],
            )
            self.assertEqual(result["summary"]["detail_batch_size"], 100)
            self.assertIsNone(result["summary"]["detail_total_cap_per_run"])

    def test_more_than_one_hundred_changed_rows_are_fully_processed_across_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root, baselined=False)
            baseline_pages = {
                1: [listing(number) for number in range(1, 101)],
                2: [listing(number) for number in range(101, 106)],
            }
            baseline_result, baseline_exit = daily.execute_forward_baseline(
                paths=paths,
                client_factory=lambda _config: FakeClient(baseline_pages, {}),
            )
            self.assertEqual(baseline_exit, 0, baseline_result)

            changed_pages = {
                1: [listing(number, reviewed_at="2026-09-02T00:00:00Z") for number in range(1, 101)],
                2: [listing(number, reviewed_at="2026-09-02T00:00:00Z") for number in range(101, 141)],
            }
            details = {
                f"prompt-{number}": detail(number, prompt=f"Prompt body {number}")
                for number in range(1, 141)
            }
            client = FakeClient(changed_pages, details)

            result, exit_code = daily.execute_daily_sync(
                paths=paths,
                client_factory=lambda _config: client,
                batch_size=100,
            )

            self.assertEqual(exit_code, 0, result)
            self.assertEqual(result["summary"]["changed_or_new"], 140)
            self.assertEqual(result["summary"]["batches_planned"], 2)
            self.assertEqual(result["summary"]["batches_completed"], 2)
            self.assertEqual(result["summary"]["detail_batch_size"], 100)
            self.assertIsNone(result["summary"]["detail_total_cap_per_run"])
            durable_state = json.loads(paths.state.read_text(encoding="utf-8"))
            self.assertEqual(durable_state["daily_sync_checkpoint"]["batch_number"], 2)
            self.assertIn("140", durable_state["source_versions"])
            detail_urls = [
                url
                for url in client.urls
                if "/api/prompts/" in url and not url.endswith("robots.txt")
            ]
            self.assertEqual(len(detail_urls), 140)

    def test_lists_every_page_with_access_type_zero_and_strips_list_prompt_bodies(self) -> None:
        first = [listing(number) for number in range(1, 101)]
        first[0]["prompts"] = "must not persist from the list response"
        second = [listing(101), listing(102)]
        client = FakeClient({1: first, 2: second}, {})
        metadata, summary = daily.collect_all_free_list_metadata(config(), client)
        self.assertEqual(len(metadata), 102)
        self.assertEqual(summary["list_pages"], 2)
        self.assertEqual(summary["stripped_list_prompt_bodies"], 1)
        self.assertNotIn("prompts", metadata[0])
        list_urls = [url for url in client.urls if "/api/prompts?" in url]
        self.assertEqual(len(list_urls), 2)
        for url in list_urls:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            self.assertEqual(query["access_type"], ["0"])
            self.assertEqual(query["limit"], ["100"])

    def test_incremental_batches_strip_locked_body_merge_queue_and_never_mutate_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            old = listing(1, reviewed_at="2026-09-01T00:01:00Z")
            state = json.loads(paths.state.read_text(encoding="utf-8"))
            state["source_versions"] = {"1": daily.metadata_version(old)}
            paths.state.write_text(json.dumps(state), encoding="utf-8")
            locked = detail(3)
            locked["is_unlocked"] = False
            locked["prompts"] = "forbidden paid or locked prompt body"
            pages = {1: [old, listing(2), listing(3)]}
            client = FakeClient(pages, {"prompt-2": detail(2), "prompt-3": locked})
            canonical_before = sha256_file(paths.canonical)

            result, exit_code = daily.execute_daily_sync(
                paths=paths,
                client_factory=lambda _config: client,
                batch_size=1,
            )

            self.assertEqual(exit_code, 0, result)
            self.assertEqual(result["summary"]["unchanged_excluded"], 1)
            self.assertEqual(result["summary"]["batches_completed"], 2)
            self.assertEqual(result["summary"]["locked_metadata_only"], 1)
            self.assertEqual(sha256_file(paths.canonical), canonical_before)
            durable_state = json.loads(paths.state.read_text(encoding="utf-8"))
            self.assertEqual(set(durable_state["source_versions"]), {"1", "2", "3"})
            queue = json.loads(paths.queue.read_text(encoding="utf-8"))
            self.assertEqual([item["upstream_id"] for item in queue["items"]], ["2"])
            self.assertTrue(all(item["workflow_status"] == "queued_for_review" for item in queue["items"]))
            self.assertFalse(queue["summary"]["approval_is_public_release"])
            raw_files = sorted((paths.data_root / "raw").glob("daily-*.json"))
            self.assertEqual(len(raw_files), 2)
            raw_text = "\n".join(path.read_text(encoding="utf-8") for path in raw_files)
            self.assertNotIn("forbidden paid or locked prompt body", raw_text)
            self.assertNotIn('"automatic_decision_apply": true', raw_text.casefold())

    def test_failed_second_batch_does_not_advance_its_source_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            client = FakeClient(
                {1: [listing(10), listing(11)]},
                {"prompt-10": detail(10), "prompt-11": detail(11)},
            )
            real_normalize = daily.normalize_bundle
            calls = 0

            def fail_second(raw):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("synthetic normalize failure")
                return real_normalize(raw)

            with mock.patch.object(daily, "normalize_bundle", side_effect=fail_second):
                result, exit_code = daily.execute_daily_sync(
                    paths=paths,
                    client_factory=lambda _config: client,
                    batch_size=1,
                )

            self.assertEqual(exit_code, 1)
            self.assertEqual(result["summary"]["batches_completed"], 1)
            durable_state = json.loads(paths.state.read_text(encoding="utf-8"))
            self.assertIn("10", durable_state["source_versions"])
            self.assertNotIn("11", durable_state["source_versions"])
            self.assertEqual(durable_state["daily_sync_checkpoint"]["batch_number"], 1)
            manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertIn("synthetic normalize failure", manifest["error"])

    def test_exact_canonical_duplicate_is_not_added_to_review_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            prompt = "Create a clean blue product hero with broad copy space."
            canonical = {
                "record_id": "existing-1",
                "style_id": "STYLE-1",
                "prompt": {"text": prompt},
                "source": {"provider": "manual", "url": "https://example.test/source"},
            }
            paths.canonical.write_text(json.dumps(canonical) + "\n", encoding="utf-8")
            client = FakeClient({1: [listing(20)]}, {"prompt-20": detail(20, prompt)})
            result, exit_code = daily.execute_daily_sync(
                paths=paths,
                client_factory=lambda _config: client,
            )
            self.assertEqual(exit_code, 0, result)
            self.assertEqual(result["summary"]["auto_collapsed"], 1)
            queue = json.loads(paths.queue.read_text(encoding="utf-8"))
            self.assertEqual(queue["items"], [])

    def test_exact_prompt_seen_in_prior_batch_is_durably_collapsed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            prompt = "Create one minimal ceramic cup on a pale stone pedestal."
            client = FakeClient(
                {1: [listing(30), listing(31)]},
                {"prompt-30": detail(30, prompt), "prompt-31": detail(31, prompt)},
            )
            result, exit_code = daily.execute_daily_sync(
                paths=paths,
                client_factory=lambda _config: client,
                batch_size=1,
            )
            self.assertEqual(exit_code, 0, result)
            self.assertEqual(result["summary"]["auto_collapsed"], 1)
            queue = json.loads(paths.queue.read_text(encoding="utf-8"))
            self.assertEqual([item["upstream_id"] for item in queue["items"]], ["30"])
            state = json.loads(paths.state.read_text(encoding="utf-8"))
            prompt_sha = daily.sha256_text(prompt)
            occurrence = state["prompt_hash_occurrences"][prompt_sha]
            self.assertEqual(occurrence["first_upstream_id"], "30")
            self.assertEqual(occurrence["source_ids"], ["30", "31"])

    def test_historical_deferred_queue_hash_prevents_new_upstream_reentry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            prompt = "Render a monochrome architectural paper model on a white sweep."
            history = paths.queue.parent / "history" / "old--revision.json"
            history.parent.mkdir(parents=True)
            history.write_text(
                json.dumps(
                    {
                        "run_id": "old",
                        "observed_at": "2026-08-31T00:00:00Z",
                        "items": [
                            {
                                "upstream_id": "deferred-old-source",
                                "prompt_sha256": daily.sha256_text(prompt),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            client = FakeClient({1: [listing(40)]}, {"prompt-40": detail(40, prompt)})
            result, exit_code = daily.execute_daily_sync(
                paths=paths,
                client_factory=lambda _config: client,
            )
            self.assertEqual(exit_code, 0, result)
            self.assertEqual(result["summary"]["auto_collapsed"], 1)
            queue = json.loads(paths.queue.read_text(encoding="utf-8"))
            self.assertEqual(queue["items"], [])
            state = json.loads(paths.state.read_text(encoding="utf-8"))
            occurrence = state["prompt_hash_occurrences"][daily.sha256_text(prompt)]
            self.assertEqual(occurrence["first_upstream_id"], "deferred-old-source")
            self.assertEqual(occurrence["source_ids"], ["deferred-old-source", "40"])

    def test_exclusive_lock_skips_overlapping_run_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.make_paths(root)
            factory_called = False

            def factory(_config):
                nonlocal factory_called
                factory_called = True
                return FakeClient({}, {})

            with daily.exclusive_sync_lock(paths):
                result, exit_code = daily.execute_daily_sync(paths=paths, client_factory=factory)
            self.assertEqual(exit_code, 0)
            self.assertEqual(result["status"], "skipped_locked")
            self.assertFalse(result["network"])
            self.assertFalse(factory_called)

    def test_robots_generic_and_content_signal_are_both_required(self) -> None:
        missing_signal = "User-agent: *\nAllow: /api/prompts\n"
        with self.assertRaisesRegex(RuntimeError, "Content-Signal"):
            daily.verify_robots(config(), FakeClient({}, {}, robots=missing_signal))
        no_generic = "User-agent: GPTBot\nAllow: /api/prompts\nContent-Signal: search=yes, ai-train=no\n"
        with self.assertRaisesRegex(RuntimeError, "generic robots group"):
            daily.verify_robots(config(), FakeClient({}, {}, robots=no_generic))


if __name__ == "__main__":
    unittest.main()
