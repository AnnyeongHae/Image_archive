from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src" / "remote_media_canary.py"


def load_module():
    spec = importlib.util.spec_from_file_location("remote_media_canary", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RemoteMediaCanaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_public_image_candidate_detects_supported_hosts(self):
        self.assertTrue(
            self.module.public_image_candidate(
                "https://raw.githubusercontent.com/example/repo/main/image.png"
            )
        )
        self.assertTrue(
            self.module.public_image_candidate(
                "https://pbs.twimg.com/media/Example.jpg?format=jpg&name=large"
            )
        )
        self.assertFalse(self.module.public_image_candidate("https://example.com/private/image.png"))
        self.assertFalse(
            self.module.public_image_candidate(
                "https://raw.githubusercontent.com.evil.example/repo/image.png"
            )
        )
        self.assertFalse(
            self.module.public_image_candidate(
                "http://raw.githubusercontent.com/example/repo/main/image.png"
            )
        )

    def test_select_canary_prefers_host_diversity(self):
        rows = [
            {"url": "https://a.example/1.jpg", "host": "a.example", "public_direct_candidate": True},
            {"url": "https://a.example/2.jpg", "host": "a.example", "public_direct_candidate": True},
            {"url": "https://b.example/1.jpg", "host": "b.example", "public_direct_candidate": True},
            {"url": "https://c.example/1.jpg", "host": "c.example", "public_direct_candidate": False},
        ]
        chosen = self.module.select_canary(rows, limit=2)
        self.assertEqual([row["host"] for row in chosen], ["a.example", "b.example"])

    def test_signed_query_is_redacted_from_artifacts(self):
        url = "https://example.com/a.png?X-Amz-Signature=secret&X-Amz-Expires=300#fragment"
        self.assertEqual(self.module.redact_url(url), "https://example.com/a.png")

    def test_private_literal_ip_is_blocked(self):
        with self.assertRaisesRegex(self.module.CanaryError, "non_public_address_blocked"):
            self.module.validate_public_http_url(
                "https://10.0.0.1/image.png", require_allowed_host=False
            )

    def test_http_is_rejected_before_network_resolution(self):
        with self.assertRaisesRegex(self.module.CanaryError, "https_required"):
            self.module.validate_public_http_url(
                "http://raw.githubusercontent.com/example/repo/main/image.png"
            )

    def test_all_selection_has_no_ten_item_ceiling_and_deduplicates_urls(self):
        rows = [
            {
                "url": f"https://raw.githubusercontent.com/demo/repo/main/{index}.jpg",
                "host": "raw.githubusercontent.com",
                "public_direct_candidate": True,
                "catalog_key": f"external:{index}",
                "asset_index": 0,
            }
            for index in range(15)
        ]
        rows.append(dict(rows[0]))
        selected = self.module.select_all(rows)
        self.assertEqual(len(selected), 15)
        self.assertEqual(selected[0]["reference_count"], 2)

    def test_bulk_and_fetch_write_guards(self):
        with self.assertRaisesRegex(self.module.CanaryError, "fetch_requires_apply"):
            self.module.run(fetch=True, apply=False, limit=1)
        with self.assertRaisesRegex(self.module.CanaryError, "all_requires_fetch_and_apply"):
            self.module.run(fetch=False, apply=True, limit=1, all_records=True)
        with self.assertRaisesRegex(self.module.CanaryError, "bounded_canary_concurrency_must_be_one"):
            self.module.run(fetch=False, apply=False, limit=1, concurrency=2)
        with self.assertRaisesRegex(ValueError, "concurrency must be between 1 and 8"):
            self.module.run(
                fetch=True,
                apply=True,
                limit=1,
                all_records=True,
                concurrency=9,
            )
        with self.assertRaisesRegex(ValueError, "concurrency must be between 1 and 8"):
            self.module.run(
                fetch=True,
                apply=True,
                limit=1,
                all_records=True,
                concurrency=0,
            )

    def test_host_pacer_serializes_same_host_but_allows_different_hosts(self):
        pacer = self.module.HostPacer(minimum_interval_seconds=0)
        state_lock = threading.Lock()
        same_active = 0
        same_max = 0

        def same_host_job():
            nonlocal same_active, same_max
            with pacer.request_slot("raw.githubusercontent.com"):
                with state_lock:
                    same_active += 1
                    same_max = max(same_max, same_active)
                time.sleep(0.02)
                with state_lock:
                    same_active -= 1

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _: same_host_job(), range(2)))
        self.assertEqual(same_max, 1)

        different_active = 0
        different_max = 0
        release = threading.Event()

        def different_host_job(host):
            nonlocal different_active, different_max
            with pacer.request_slot(host):
                with state_lock:
                    different_active += 1
                    different_max = max(different_max, different_active)
                    if different_active == 2:
                        release.set()
                release.wait(timeout=1)
                with state_lock:
                    different_active -= 1

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(
                executor.map(
                    different_host_job,
                    ["raw.githubusercontent.com", "cms-assets.youmind.com"],
                )
            )
        self.assertEqual(different_max, 2)

    def test_bulk_scheduler_parallelizes_distinct_hosts_and_main_thread_owns_metadata_writes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            canonical = root / "data" / "canonical" / "archive_records.jsonl"
            canonical.parent.mkdir(parents=True, exist_ok=True)
            urls = [
                "https://raw.githubusercontent.com/demo/repo/main/a.png",
                "https://raw.githubusercontent.com/demo/repo/main/b.png",
                "https://cms-assets.youmind.com/media/c.png",
                "https://pbs.twimg.com/media/d.png",
                "https://cdn.jsdelivr.net/gh/demo/repo@abc/e.png",
            ]
            records = []
            for index, url in enumerate(urls):
                records.append(
                    {
                        "catalog_key": f"external:EXT-{index}",
                        "record_id": f"EXT-{index}",
                        "style_id": f"EXT-{index}",
                        "lane": "external",
                        "source": {"name": "Demo"},
                        "rights": {"status": "research_only_needs_review", "release_eligible": False},
                        "media": {"assets": [{"uri_kind": "remote", "uri": url, "role": "source_preview"}]},
                    }
                )
            canonical.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            module = self.module
            output = root / "data" / "private-research" / "remote-media-canary"
            originals = (module.ROOT, module.CANONICAL, module.OUTPUT_ROOT, module.CURRENT_DIR, module.RUNS_DIR)
            main_thread_id = threading.get_ident()
            worker_thread_ids = []
            metadata_write_thread_ids = []
            state_lock = threading.Lock()
            active_hosts = set()
            active_total = 0
            max_active_total = 0
            same_host_overlap = False
            release = threading.Event()

            def fake_network(row, pacer):
                nonlocal active_total, max_active_total, same_host_overlap
                host = row["host"]
                with state_lock:
                    worker_thread_ids.append(threading.get_ident())
                    if host in active_hosts:
                        same_host_overlap = True
                    active_hosts.add(host)
                    active_total += 1
                    max_active_total = max(max_active_total, active_total)
                    if active_total >= 2:
                        release.set()
                release.wait(timeout=1)
                time.sleep(0.01)
                with state_lock:
                    active_hosts.remove(host)
                    active_total -= 1
                return {
                    "status": "fetched",
                    "requested_url": module.redact_url(row["url"]),
                    "requested_url_sha256": module.sha256_text(row["url"]),
                    "sha256": module.sha256_text(row["url"]),
                    "blob_path": f"fake/{module.sha256_text(row['url'])}.png",
                }

            original_write_receipt = module._write_receipt
            original_merge_cache = module._merge_cache_entries
            original_write_checkpoint = module._write_checkpoint

            def tracked_write_receipt(*args, **kwargs):
                metadata_write_thread_ids.append(threading.get_ident())
                return original_write_receipt(*args, **kwargs)

            def tracked_merge_cache(*args, **kwargs):
                metadata_write_thread_ids.append(threading.get_ident())
                return original_merge_cache(*args, **kwargs)

            def tracked_write_checkpoint(*args, **kwargs):
                metadata_write_thread_ids.append(threading.get_ident())
                return original_write_checkpoint(*args, **kwargs)

            try:
                module.ROOT = root
                module.CANONICAL = canonical
                module.OUTPUT_ROOT = output
                module.CURRENT_DIR = output / "current"
                module.RUNS_DIR = output / "runs"
                with (
                    patch.object(module, "_network_fetch_result", side_effect=fake_network),
                    patch.object(module, "_write_receipt", side_effect=tracked_write_receipt),
                    patch.object(module, "_merge_cache_entries", side_effect=tracked_merge_cache),
                    patch.object(module, "_write_checkpoint", side_effect=tracked_write_checkpoint),
                ):
                    payload = module.run(
                        fetch=True,
                        apply=True,
                        limit=1,
                        all_records=True,
                        min_free_gib=0,
                        concurrency=4,
                    )
            finally:
                (
                    module.ROOT,
                    module.CANONICAL,
                    module.OUTPUT_ROOT,
                    module.CURRENT_DIR,
                    module.RUNS_DIR,
                ) = originals

            self.assertGreaterEqual(max_active_total, 2)
            self.assertFalse(same_host_overlap)
            self.assertTrue(worker_thread_ids)
            self.assertTrue(all(thread_id != main_thread_id for thread_id in worker_thread_ids))
            self.assertTrue(metadata_write_thread_ids)
            self.assertTrue(all(thread_id == main_thread_id for thread_id in metadata_write_thread_ids))
            self.assertEqual(payload["summary"]["concurrency"], 4)
            self.assertEqual(payload["summary"]["downloaded_this_run"], len(urls))
            self.assertEqual(
                [item["style_id"] for item in payload["fetch_results"]],
                [f"EXT-{index}" for index in range(len(urls))],
            )

    def test_probe_decodes_before_atomic_blob_commit(self):
        image_bytes = io.BytesIO()
        Image.new("RGB", (12, 8), (12, 34, 56)).save(image_bytes, format="PNG")
        payload = image_bytes.getvalue()

        class FakeResponse:
            def __init__(self):
                self.stream = io.BytesIO(payload)
                self.headers = Message()
                self.headers["Content-Type"] = "image/png"
                self.headers["Content-Length"] = str(len(payload))

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def geturl(self):
                return "https://raw.githubusercontent.com/demo/repo/main/image.png"

            def read(self, size=-1):
                return self.stream.read(size)

        opener = SimpleNamespace(open=lambda request, timeout: FakeResponse())
        module = self.module
        with tempfile.TemporaryDirectory() as temp_dir:
            original_root = module.ROOT
            original_output = module.OUTPUT_ROOT
            try:
                module.ROOT = Path(temp_dir)
                module.OUTPUT_ROOT = Path(temp_dir) / "private-cache"
                with patch.object(module, "validate_public_http_url", return_value=None):
                    result = module.probe_and_fetch(
                        {"url": "https://raw.githubusercontent.com/demo/repo/main/image.png"},
                        max_bytes=1024 * 1024,
                        max_pixels=1000,
                        opener=opener,
                    )
            finally:
                module.ROOT = original_root
                module.OUTPUT_ROOT = original_output

            self.assertEqual(result["detected_format"], "PNG")
            self.assertEqual(result["pixel_count"], 96)
            blob = Path(temp_dir) / result["blob_path"]
            self.assertTrue(blob.is_file())
            self.assertEqual(list((Path(temp_dir) / "private-cache" / ".tmp").glob("*.download")), [])

    def test_probe_accepts_octet_stream_only_after_image_decode(self):
        image_bytes = io.BytesIO()
        Image.new("RGB", (10, 6), (90, 80, 70)).save(image_bytes, format="PNG")
        payload = image_bytes.getvalue()

        class FakeResponse:
            def __init__(self):
                self.stream = io.BytesIO(payload)
                self.headers = Message()
                self.headers["Content-Type"] = "application/octet-stream"
                self.headers["Content-Length"] = str(len(payload))

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def geturl(self):
                return "https://raw.githubusercontent.com/demo/repo/main/image.bin"

            def read(self, size=-1):
                return self.stream.read(size)

        opener = SimpleNamespace(open=lambda request, timeout: FakeResponse())
        module = self.module
        with tempfile.TemporaryDirectory() as temp_dir:
            original_root = module.ROOT
            original_output = module.OUTPUT_ROOT
            try:
                module.ROOT = Path(temp_dir)
                module.OUTPUT_ROOT = Path(temp_dir) / "private-cache"
                with patch.object(module, "validate_public_http_url", return_value=None):
                    result = module.probe_and_fetch(
                        {"url": "https://raw.githubusercontent.com/demo/repo/main/image.bin"},
                        max_bytes=1024 * 1024,
                        max_pixels=1000,
                        opener=opener,
                    )
            finally:
                module.ROOT = original_root
                module.OUTPUT_ROOT = original_output

            self.assertEqual(result["content_type"], "application/octet-stream")
            self.assertEqual(result["detected_format"], "PNG")
            self.assertTrue((Path(temp_dir) / result["blob_path"]).is_file())

    def test_probe_rejects_non_image_octet_stream_without_committing_blob(self):
        payload = b"not an image"

        class FakeResponse:
            def __init__(self):
                self.stream = io.BytesIO(payload)
                self.headers = Message()
                self.headers["Content-Type"] = "application/octet-stream"

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def geturl(self):
                return "https://raw.githubusercontent.com/demo/repo/main/not-image.bin"

            def read(self, size=-1):
                return self.stream.read(size)

        opener = SimpleNamespace(open=lambda request, timeout: FakeResponse())
        module = self.module
        with tempfile.TemporaryDirectory() as temp_dir:
            original_root = module.ROOT
            original_output = module.OUTPUT_ROOT
            try:
                module.ROOT = Path(temp_dir)
                module.OUTPUT_ROOT = Path(temp_dir) / "private-cache"
                with patch.object(module, "validate_public_http_url", return_value=None):
                    with self.assertRaisesRegex(module.CanaryError, "pillow_decode_failed"):
                        module.probe_and_fetch(
                            {"url": "https://raw.githubusercontent.com/demo/repo/main/not-image.bin"},
                            max_bytes=1024 * 1024,
                            max_pixels=1000,
                            opener=opener,
                        )
            finally:
                module.ROOT = original_root
                module.OUTPUT_ROOT = original_output

            self.assertEqual(list((Path(temp_dir) / "private-cache" / "blobs").rglob("*")), [])

    def test_failure_receipt_is_durable_but_never_reused_as_cache(self):
        module = self.module
        row = {
            "url": "https://raw.githubusercontent.com/demo/repo/main/broken.jpg",
            "catalog_key": "external:EXT-BROKEN",
            "record_id": "EXT-BROKEN",
            "style_id": "BROKEN-001",
            "asset_index": 0,
            "asset_role": "source_preview",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            original_root = module.ROOT
            original_output = module.OUTPUT_ROOT
            try:
                module.ROOT = Path(temp_dir)
                module.OUTPUT_ROOT = Path(temp_dir) / "private-cache"
                module._write_receipt(
                    row,
                    {"status": "blocked", "error": "pillow_decode_failed"},
                )
                url_sha = module.sha256_text(row["url"])
                receipt_path = module._receipt_path(url_sha)
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                cached = module._read_receipt(url_sha)
            finally:
                module.ROOT = original_root
                module.OUTPUT_ROOT = original_output

            self.assertEqual(receipt["result"]["status"], "blocked")
            self.assertEqual(receipt["result"]["error"], "pillow_decode_failed")
            self.assertIsNone(cached)

    def test_probe_rejects_decoded_pixel_overflow_without_committing_blob(self):
        image_bytes = io.BytesIO()
        Image.new("RGB", (12, 8), (12, 34, 56)).save(image_bytes, format="PNG")
        payload = image_bytes.getvalue()

        class FakeResponse:
            def __init__(self):
                self.stream = io.BytesIO(payload)
                self.headers = Message()
                self.headers["Content-Type"] = "image/png"

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def geturl(self):
                return "https://raw.githubusercontent.com/demo/repo/main/image.png"

            def read(self, size=-1):
                return self.stream.read(size)

        opener = SimpleNamespace(open=lambda request, timeout: FakeResponse())
        module = self.module
        with tempfile.TemporaryDirectory() as temp_dir:
            original_root = module.ROOT
            original_output = module.OUTPUT_ROOT
            try:
                module.ROOT = Path(temp_dir)
                module.OUTPUT_ROOT = Path(temp_dir) / "private-cache"
                with patch.object(module, "validate_public_http_url", return_value=None):
                    with self.assertRaisesRegex(module.CanaryError, "decoded_pixel_limit_exceeded"):
                        module.probe_and_fetch(
                            {"url": "https://raw.githubusercontent.com/demo/repo/main/image.png"},
                            max_bytes=1024 * 1024,
                            max_pixels=50,
                            opener=opener,
                        )
            finally:
                module.ROOT = original_root
                module.OUTPUT_ROOT = original_output

            self.assertEqual(list((Path(temp_dir) / "private-cache" / "blobs").rglob("*")), [])

    def test_retry_after_429_then_success(self):
        headers = Message()
        headers["Retry-After"] = "0"
        error = self.module.HTTPError(
            "https://raw.githubusercontent.com/demo/repo/main/image.png",
            429,
            "rate limited",
            headers,
            None,
        )
        success = {"status": "fetched", "sha256": "a" * 64}
        sleeps = []
        pacer = self.module.HostPacer(minimum_interval_seconds=0)
        with patch.object(self.module, "probe_and_fetch", side_effect=[error, success]):
            result = self.module.fetch_with_retries(
                {
                    "url": "https://raw.githubusercontent.com/demo/repo/main/image.png",
                    "host": "raw.githubusercontent.com",
                },
                max_bytes=1024,
                max_pixels=100,
                pacer=pacer,
                sleeper=sleeps.append,
            )
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(sleeps, [0.0])

    def test_disk_floor_pauses_cleanly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(
                self.module.shutil,
                "disk_usage",
                return_value=SimpleNamespace(free=10),
            ):
                with self.assertRaises(self.module.DiskFloorReached):
                    self.module.ensure_free_disk(Path(temp_dir), minimum_free_bytes=11)

    def test_cached_url_is_resumed_without_network_and_writes_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            canonical = root / "data" / "canonical" / "archive_records.jsonl"
            canonical.parent.mkdir(parents=True, exist_ok=True)
            url = "https://raw.githubusercontent.com/demo/repo/main/a.png"
            record = {
                "catalog_key": "external:EXT-001",
                "record_id": "EXT-001",
                "style_id": "EXT-001",
                "lane": "external",
                "source": {"name": "Demo"},
                "rights": {"status": "research_only_needs_review", "release_eligible": False},
                "media": {"assets": [{"uri_kind": "remote", "uri": url, "role": "source_preview"}]},
            }
            canonical.write_text(json.dumps(record) + "\n", encoding="utf-8")

            image_bytes = io.BytesIO()
            Image.new("RGB", (4, 4), (1, 2, 3)).save(image_bytes, format="PNG")
            blob_bytes = image_bytes.getvalue()
            blob_sha = self.module.hashlib.sha256(blob_bytes).hexdigest()
            output = root / "data" / "private-research" / "remote-media-canary"
            blob = output / "blobs" / blob_sha[:2] / f"{blob_sha}.png"
            blob.parent.mkdir(parents=True, exist_ok=True)
            blob.write_bytes(blob_bytes)
            current = output / "current"
            current.mkdir(parents=True, exist_ok=True)
            cached_result = {
                "status": "fetched",
                "requested_url": self.module.redact_url(url),
                "requested_url_sha256": self.module.sha256_text(url),
                "content_type": "image/png",
                "sha256": blob_sha,
                "blob_path": blob.relative_to(root).as_posix(),
                "blob_sha256_verified": True,
                "width": 4,
                "height": 4,
                "phash": "0" * 16,
                "dhash": "0" * 16,
            }
            (current / "cache_index.json").write_text(
                json.dumps(
                    {
                        "schema_version": "remote-media-cache-index-1.0",
                        "items": [
                            {
                                "catalog_key": "external:EXT-001",
                                "style_id": "EXT-001",
                                "asset_index": 0,
                                "host": "raw.githubusercontent.com",
                                "requested_url": self.module.redact_url(url),
                                "requested_url_sha256": self.module.sha256_text(url),
                                "result": cached_result,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            module = self.module
            originals = (module.ROOT, module.CANONICAL, module.OUTPUT_ROOT, module.CURRENT_DIR, module.RUNS_DIR)
            try:
                module.ROOT = root
                module.CANONICAL = canonical
                module.OUTPUT_ROOT = output
                module.CURRENT_DIR = current
                module.RUNS_DIR = output / "runs"
                with patch.object(module, "fetch_with_retries", side_effect=AssertionError("network called")):
                    payload = module.run(
                        fetch=True,
                        apply=True,
                        limit=1,
                        all_records=True,
                        min_free_gib=0,
                    )
            finally:
                (
                    module.ROOT,
                    module.CANONICAL,
                    module.OUTPUT_ROOT,
                    module.CURRENT_DIR,
                    module.RUNS_DIR,
                ) = originals

            self.assertEqual(payload["summary"]["already_cached"], 1)
            self.assertEqual(payload["mode"], "bulk_private_fetch")
            self.assertEqual(payload["summary"]["downloaded_this_run"], 0)
            checkpoint = json.loads((current / "download_checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["run_status"], "completed")
            self.assertEqual(checkpoint["processed_unique_urls"], 1)

    def test_run_inventory_only_writes_expected_private_outputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            canonical = root / "data" / "canonical" / "archive_records.jsonl"
            canonical.parent.mkdir(parents=True, exist_ok=True)
            records = [
                {
                    "catalog_key": "external:EXT-001",
                    "record_id": "EXT-001",
                    "style_id": "EXT-001",
                    "lane": "external",
                    "source": {"name": "Demo"},
                    "rights": {"status": "research_only_needs_review", "release_eligible": False},
                    "media": {
                        "assets": [
                            {"uri_kind": "remote", "uri": "https://raw.githubusercontent.com/demo/repo/main/a.jpg", "role": "source_preview"},
                            {"uri_kind": "local", "uri": "assets/local.png", "role": "generated_preview"},
                        ]
                    },
                },
                {
                    "catalog_key": "external:EXT-002",
                    "record_id": "EXT-002",
                    "style_id": "EXT-002",
                    "lane": "external",
                    "source": {"name": "Demo 2"},
                    "rights": {"status": "research_only_needs_review", "release_eligible": False},
                    "media": {
                        "assets": [
                            {"uri_kind": "remote", "uri": "https://example.com/not-direct.jpg", "role": "source_preview"}
                        ]
                    },
                },
            ]
            canonical.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records), encoding="utf-8")

            module = self.module
            original_root = module.ROOT
            original_canonical = module.CANONICAL
            original_output = module.OUTPUT_ROOT
            original_current = module.CURRENT_DIR
            original_runs = module.RUNS_DIR
            try:
                module.ROOT = root
                module.CANONICAL = canonical
                module.OUTPUT_ROOT = root / "data" / "private-research" / "remote-media-canary"
                module.CURRENT_DIR = module.OUTPUT_ROOT / "current"
                module.RUNS_DIR = module.OUTPUT_ROOT / "runs"
                payload = module.run(fetch=False, apply=True, limit=2)
            finally:
                module.ROOT = original_root
                module.CANONICAL = original_canonical
                module.OUTPUT_ROOT = original_output
                module.CURRENT_DIR = original_current
                module.RUNS_DIR = original_runs

            self.assertEqual(payload["mode"], "inventory_only")
            self.assertEqual(payload["summary"]["requested"], 1)
            inventory = json.loads((root / "data" / "private-research" / "remote-media-canary" / "current" / "inventory.json").read_text(encoding="utf-8"))
            self.assertEqual(inventory["record_count"], 2)
            latest = json.loads((root / "data" / "private-research" / "remote-media-canary" / "current" / "latest_run.json").read_text(encoding="utf-8"))
            self.assertEqual(latest["schema_version"], "remote-media-canary-run-1.0")
            cache = json.loads((root / "data" / "private-research" / "remote-media-canary" / "current" / "cache_index.json").read_text(encoding="utf-8"))
            self.assertEqual(cache["schema_version"], "remote-media-cache-index-1.0")
            self.assertEqual(cache["items"], [])


if __name__ == "__main__":
    unittest.main()
