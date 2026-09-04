from __future__ import annotations

import hashlib
import json
import random
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


ARCHIVE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = ARCHIVE_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from build_duplicate_index import BuildConfig, build_duplicate_index  # noqa: E402
from duplicate_review_store import (  # noqa: E402
    DuplicateGroupNotFound,
    DuplicateGroupStore,
    DuplicateIndexUnavailable,
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record(
    key: str,
    prompt_text: str | None,
    *,
    local_uri: str | None = None,
    local_sha256: str | None = None,
) -> dict:
    prompt = {"text": prompt_text}
    if prompt_text is not None:
        prompt["sha256"] = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    assets = []
    if local_uri is not None:
        assets.append(
            {
                "uri_kind": "local",
                "uri": local_uri,
                "sha256": local_sha256,
                "mime_type": "image/png",
            }
        )
    else:
        assets.append(
            {
                "uri_kind": "remote",
                "uri": f"https://example.invalid/{key}.jpg",
                "mime_type": "image/jpeg",
            }
        )
    return {
        "catalog_key": key,
        "record_id": key,
        "style_id": f"STYLE-{key}",
        "lane": "fixture",
        "title": f"Fixture {key}",
        "source": {"name": "fixture-source", "url": "https://example.invalid/source"},
        "rights": {"status": "needs_review"},
        "review_release": {"review_status": "needs_review"},
        "prompt": prompt,
        "media": {"assets": assets},
    }


class DuplicateIndexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.platform_root = Path(cls.temp_dir.name)
        cls.legacy_root = cls.platform_root / "legacy" / "current_archive"
        image_root = cls.legacy_root / "images"
        image_root.mkdir(parents=True)

        rng = random.Random(20260831)
        pixels = rng.randbytes(800 * 600 * 3)
        first_path = image_root / "first.png"
        second_path = image_root / "second.png"
        third_path = image_root / "third.png"
        fourth_path = image_root / "fourth.png"
        Image.frombytes("RGB", (800, 600), pixels).save(first_path, format="PNG")
        shutil.copyfile(first_path, second_path)
        changed = bytearray(pixels)
        changed[0:12] = b"\x00" * 12
        Image.frombytes("RGB", (800, 600), bytes(changed)).save(third_path, format="PNG")
        changed_again = bytearray(pixels)
        changed_again[24:48] = b"\xff" * 24
        Image.frombytes("RGB", (800, 600), bytes(changed_again)).save(fourth_path, format="PNG")

        first_sha = sha256_file(first_path)
        third_sha = sha256_file(third_path)
        fourth_sha = sha256_file(fourth_path)
        canonical = cls.platform_root / "data" / "canonical" / "archive_records.jsonl"
        canonical.parent.mkdir(parents=True)
        prompt_exact = "Hero layout for {product_name} on clean background with bold copy block and feature chips."
        prompt_variant = "Hero layout for {sku_name} on clean background with bold copy block and feature chips."
        prompt_alternate = "Exploded technical layout for {device_name} with floating parts and callout rails."
        rows = [
            record("fixture:one", prompt_exact, local_uri="images/first.png", local_sha256=first_sha),
            record("fixture:two", prompt_exact, local_uri="images/second.png", local_sha256=first_sha),
            record("fixture:three", "Different full prompt body without template slots", local_uri="images/third.png", local_sha256=third_sha),
            record("fixture:four", prompt_variant, local_uri="images/fourth.png", local_sha256=fourth_sha),
            record("fixture:five", prompt_alternate, local_uri="images/second.png", local_sha256=first_sha),
            record("fixture:remote", None),
        ]
        canonical.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        cls.canonical_before = sha256_file(canonical)
        cls.config = BuildConfig(
            platform_root=cls.platform_root,
            canonical_path=canonical,
            legacy_root=cls.legacy_root,
            output_dir=cls.platform_root
            / "data"
            / "private-research"
            / "duplicate-analysis"
            / "current",
            thumbnail_root=cls.platform_root / "media" / "derived" / "duplicate-review",
            perceptual_limit=5,
            phash_threshold=64,
            dhash_threshold=64,
            thumbnail_limit=3,
            thumbnail_max_px=640,
            thumbnail_quality=78,
        )

        cls.dry_run = build_duplicate_index(cls.config, apply=False)
        cls.dry_run_outputs_absent = (
            not cls.config.index_path.exists()
            and not cls.config.summary_path.exists()
            and not cls.config.thumbnail_root.exists()
        )
        cls.applied = build_duplicate_index(cls.config, apply=True)
        cls.store = DuplicateGroupStore(cls.config.index_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def test_dry_run_is_write_free_and_canonical_is_immutable(self) -> None:
        self.assertEqual(self.dry_run["mode"], "dry_run")
        self.assertFalse(self.dry_run["writes"])
        self.assertTrue(self.dry_run_outputs_absent)
        self.assertEqual(sha256_file(self.config.canonical_path), self.canonical_before)
        self.assertTrue(self.config.index_path.is_file())
        self.assertTrue(self.config.summary_path.is_file())

    def test_exact_and_perceptual_counts_are_separate(self) -> None:
        analysis = self.applied["analysis"]
        self.assertEqual(analysis["exact_prompt"]["requested"], 6)
        self.assertEqual(analysis["exact_prompt"]["completed"], 5)
        self.assertEqual(analysis["exact_prompt"]["skipped"], 1)
        self.assertEqual(analysis["exact_media"]["requested"], 5)
        self.assertEqual(analysis["exact_media"]["completed"], 5)
        self.assertEqual(analysis["perceptual"]["requested"], 5)
        self.assertEqual(analysis["perceptual"]["completed"], 5)
        self.assertEqual(self.applied["counts"]["groups_by_kind"]["exact_prompt_media"], 1)
        self.assertEqual(self.applied["counts"]["groups_by_kind"]["exact_prompt"], 1)
        self.assertEqual(self.applied["counts"]["groups_by_kind"]["exact_media"], 1)
        self.assertEqual(self.applied["counts"]["groups_by_kind"]["same_prompt_variant"], 1)
        self.assertEqual(self.applied["counts"]["groups_by_kind"]["same_media_variant"], 1)
        self.assertGreaterEqual(self.applied["counts"]["groups_by_kind"]["perceptual_candidate"], 1)

        candidates = self.store.list_groups(kind="perceptual_candidate", limit=10)
        self.assertGreaterEqual(candidates["total"], 1)
        self.assertTrue(all(group["member_count"] == 2 for group in candidates["groups"]))

    def test_outputs_are_compact_path_free_and_content_addressed(self) -> None:
        summary_bytes = self.config.summary_path.stat().st_size
        summary = json.loads(self.config.summary_path.read_text(encoding="utf-8"))
        self.assertEqual(summary["artifacts"]["summary_json"]["bytes"], summary_bytes)
        thumbnail_files = list(self.config.thumbnail_root.glob("*.webp"))
        self.assertLessEqual(len(thumbnail_files), 3)
        self.assertTrue(thumbnail_files)
        self.assertEqual(summary["artifacts"]["thumbnails"]["count"], len(thumbnail_files))
        self.assertEqual(
            summary["artifacts"]["thumbnails"]["bytes"],
            sum(path.stat().st_size for path in thumbnail_files),
        )
        for path in thumbnail_files:
            self.assertEqual(path.stem, sha256_file(path))
            self.assertLess(path.stat().st_size, (self.legacy_root / "images" / "first.png").stat().st_size)

        connection = sqlite3.connect(self.config.index_path)
        try:
            asset_columns = {row[1] for row in connection.execute("PRAGMA table_info(assets)")}
            self.assertNotIn("path", asset_columns)
            self.assertNotIn("uri", asset_columns)
        finally:
            connection.close()

        public_payloads = [
            self.store.summary(),
            self.store.list_groups(limit=10, q="fixture", sort="members_desc"),
        ]
        group_id = public_payloads[1]["groups"][0]["group_id"]
        public_payloads.append(self.store.group_detail(group_id, limit=10))
        self.assertIn("recommendation", public_payloads[1]["groups"][0])
        self.assertTrue(
            public_payloads[1]["groups"][0]["recommendation"]["recommended_primary_member_id"]
        )
        rendered = json.dumps(public_payloads, ensure_ascii=False)
        self.assertNotIn(str(self.platform_root), rendered)
        self.assertNotIn("same full prompt body", rendered)
        self.assertNotIn("base64,", rendered.casefold())

    def test_store_errors_are_typed(self) -> None:
        with self.assertRaises(DuplicateGroupNotFound):
            self.store.group_detail("missing")
        with self.assertRaises(DuplicateIndexUnavailable):
            DuplicateGroupStore(self.platform_root / "missing.sqlite3").summary()


if __name__ == "__main__":
    unittest.main()
