from __future__ import annotations

import hashlib
import json
import sqlite3
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


ARCHIVE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = ARCHIVE_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from build_duplicate_index import BuildConfig, build_duplicate_index  # noqa: E402
from image_rag_eval.dataset import build_manifest  # noqa: E402


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def record(
    key: str,
    prompt_text: str | None,
    *,
    lane: str = "fixture",
    source_name: str = "fixture-source",
    local_uri: str | None = None,
    local_sha256: str | None = None,
    remote_uri: str | None = None,
) -> dict:
    prompt = {"text": prompt_text}
    if prompt_text is not None:
        prompt["sha256"] = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    assets: list[dict[str, object]] = []
    if local_uri is not None:
        assets.append(
            {
                "uri_kind": "local",
                "uri": local_uri,
                "sha256": local_sha256,
                "mime_type": "image/png",
            }
        )
    elif remote_uri is not None:
        assets.append(
            {
                "uri_kind": "remote",
                "uri": remote_uri,
                "mime_type": "image/png",
            }
        )
    return {
        "catalog_key": key,
        "record_id": key,
        "style_id": f"STYLE-{key}",
        "lane": lane,
        "title": f"Fixture {key}",
        "source": {"name": source_name, "url": f"https://example.invalid/{source_name}"},
        "rights": {"status": "research_only_needs_review", "release_eligible": False},
        "review_release": {"review_status": "needs_review"},
        "prompt": prompt,
        "media": {"assets": assets},
    }


class ImageRagDatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp_dir.name)
        cls.legacy_root = cls.root / "legacy" / "current_archive"
        image_root = cls.legacy_root / "images"
        hidden_root = cls.root / ".hidden"
        blob_root = cls.root / "data" / "private-research" / "remote-media-canary" / "blobs"
        image_root.mkdir(parents=True)
        hidden_root.mkdir(parents=True)
        blob_root.mkdir(parents=True)

        def write_png(path: Path, color: tuple[int, int, int]) -> str:
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (48, 48), color).save(path, format="PNG")
            return sha256_file(path)

        first = image_root / "first.png"
        second = image_root / "second.png"
        third = image_root / "third.png"
        fourth = image_root / "fourth.png"
        fifth = image_root / "fifth.png"
        sixth = image_root / "sixth.png"
        hidden = hidden_root / "secret.png"
        remote_blob = blob_root / "aa" / "remote-cached.png"

        first_sha = write_png(first, (10, 20, 30))
        shutil.copyfile(first, second)
        second_sha = sha256_file(second)
        third_sha = write_png(third, (40, 50, 60))
        fourth_sha = write_png(fourth, (70, 80, 90))
        fifth_sha = write_png(fifth, (90, 100, 110))
        sixth_sha = write_png(sixth, (120, 130, 140))
        hidden_sha = write_png(hidden, (150, 160, 170))
        remote_sha = write_png(remote_blob, (200, 210, 220))

        canonical = cls.root / "data" / "canonical" / "archive_records.jsonl"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        prompt_exact_media = "Hero layout for {product_name} on clean background."
        prompt_exact = "Exploded layout for {sku_name} with callout rails."
        prompt_remote = "Lifestyle composition with accessory tray."
        rows = [
            record("fixture:one", prompt_exact_media, source_name="source-a", local_uri="images/first.png", local_sha256=first_sha),
            record("fixture:two", prompt_exact_media, source_name="source-b", local_uri="images/second.png", local_sha256=second_sha),
            record("fixture:three", prompt_exact, lane="manual", source_name="source-c", local_uri="images/third.png", local_sha256=third_sha),
            record("fixture:four", prompt_exact, lane="manual", source_name="source-d", local_uri="images/fourth.png", local_sha256=fourth_sha),
            record("fixture:five", prompt_remote, lane="external", source_name="source-e", remote_uri="https://raw.githubusercontent.com/demo/repo/main/remote-cached.png"),
            record("fixture:six", "Packshot angle with note", lane="social", source_name="source-f", local_uri="images/fifth.png", local_sha256=fifth_sha),
            record("fixture:seven", "Another packshot angle", lane="legacy", source_name="source-g", local_uri="images/sixth.png", local_sha256=sixth_sha),
            record("fixture:hidden", "Hidden input should be excluded", lane="manual", source_name="source-h", local_uri=".hidden/secret.png", local_sha256=hidden_sha),
        ]
        canonical.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

        cache_index = cls.root / "data" / "private-research" / "remote-media-canary" / "current" / "cache_index.json"
        cache_index.parent.mkdir(parents=True, exist_ok=True)
        cache_index.write_text(
            json.dumps(
                {
                    "schema_version": "remote-media-cache-index-1.0",
                    "items": [
                        {
                            "catalog_key": "fixture:five",
                            "record_id": "fixture:five",
                            "style_id": "STYLE-fixture:five",
                            "asset_index": 0,
                            "asset_role": "source_preview",
                            "requested_url": "https://raw.githubusercontent.com/demo/repo/main/remote-cached.png",
                            "requested_url_sha256": hashlib.sha256(b"https://raw.githubusercontent.com/demo/repo/main/remote-cached.png").hexdigest(),
                            "result": {
                                "status": "fetched",
                                "sha256": remote_sha,
                                "blob_path": "data/private-research/remote-media-canary/blobs/aa/remote-cached.png",
                            },
                        }
                    ],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        config = BuildConfig(
            platform_root=cls.root,
            canonical_path=canonical,
            legacy_root=cls.legacy_root,
            output_dir=cls.root / "data" / "private-research" / "duplicate-analysis" / "current",
            thumbnail_root=cls.root / "media" / "derived" / "duplicate-review",
            remote_overlay_path=cache_index,
            perceptual_limit=8,
            phash_threshold=64,
            dhash_threshold=64,
            thumbnail_limit=0,
        )
        cls.index_summary = build_duplicate_index(config, apply=True)
        cls.canonical_sha_before = sha256_file(canonical)
        cls.index_sha_before = sha256_file(config.index_path)
        cls.overlay_sha_before = sha256_file(cache_index)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def test_build_manifest_returns_safe_bounded_shape(self) -> None:
        manifest = build_manifest(self.root, limit=6)

        self.assertEqual(manifest["schema_version"], "1")
        self.assertEqual(len(manifest["items"]), 6)
        self.assertEqual(len(manifest["selection_notes"]), 5)
        self.assertTrue(any(item["group_seed_kind"] == "exact_media" for item in manifest["items"]))
        self.assertTrue(any(item["group_seed_kind"] == "exact_prompt" for item in manifest["items"]))
        self.assertTrue(any(item["group_seed_kind"] == "perceptual_candidate" for item in manifest["items"]))
        self.assertTrue(any("remote-media-canary/blobs/aa/remote-cached.png" in item["path"] for item in manifest["items"]))
        self.assertTrue(all(item["external_ai_approved"] is False for item in manifest["items"]))
        self.assertTrue(all(not item["path"].startswith(".") for item in manifest["items"]))
        self.assertTrue(all(".hidden/" not in item["path"] for item in manifest["items"]))
        self.assertTrue(all("secret" not in item["path"].casefold() for item in manifest["items"]))
        self.assertTrue(all(isinstance(item["prompt"], str) for item in manifest["items"]))
        self.assertTrue(all("source_url" not in item for item in manifest["items"]))
        self.assertTrue(all(len(item["source_url_sha256"]) == 64 for item in manifest["items"] if item["source_url_sha256"]))
        source_e = next(item for item in manifest["items"] if item["catalog_key"] == "fixture:five")
        self.assertEqual(source_e["source_url_sha256"], sha256_text("https://example.invalid/source-e"))
        self.assertEqual(
            [item["group_seed_kind"] for item in manifest["items"][:6]],
            [
                "exact_media",
                "exact_media",
                "exact_prompt",
                "exact_prompt",
                "perceptual_candidate",
                "perceptual_candidate",
            ],
        )

    def test_limit_validation_and_input_immutability(self) -> None:
        with self.assertRaises(ValueError):
            build_manifest(self.root, limit=0)
        with self.assertRaises(ValueError):
            build_manifest(self.root, limit=21)

        manifest = build_manifest(self.root, limit=1)
        self.assertEqual(len(manifest["items"]), 1)

        canonical = self.root / "data" / "canonical" / "archive_records.jsonl"
        index_path = self.root / "data" / "private-research" / "duplicate-analysis" / "current" / "duplicate_index.sqlite3"
        overlay_path = self.root / "data" / "private-research" / "remote-media-canary" / "current" / "cache_index.json"
        self.assertEqual(sha256_file(canonical), self.canonical_sha_before)
        self.assertEqual(sha256_file(index_path), self.index_sha_before)
        self.assertEqual(sha256_file(overlay_path), self.overlay_sha_before)

    def test_paths_are_root_relative_and_existing(self) -> None:
        manifest = build_manifest(self.root, limit=7)
        for item in manifest["items"]:
            target = (self.root / item["path"]).resolve()
            self.assertTrue(target.is_file())
            self.assertEqual(target.relative_to(self.root).as_posix(), item["path"])

    def test_exact_prompt_seed_can_fallback_when_group_member_asset_is_null(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            canonical = root / "data" / "canonical" / "archive_records.jsonl"
            canonical.parent.mkdir(parents=True, exist_ok=True)
            legacy = root / "legacy" / "current_archive" / "images"
            legacy.mkdir(parents=True, exist_ok=True)
            image_path = legacy / "fallback.png"
            image_sha = sha256_file(image_path) if image_path.exists() else None
            if image_sha is None:
                Image.new("RGB", (32, 32), (1, 2, 3)).save(image_path, format="PNG")
                image_sha = sha256_file(image_path)
            canonical.write_text(
                json.dumps(
                    record(
                        "fixture:fallback",
                        "Shared prompt body with {slot_name}.",
                        source_name="source-z",
                        local_uri="images/fallback.png",
                        local_sha256=image_sha,
                    ),
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            index_dir = root / "data" / "private-research" / "duplicate-analysis" / "current"
            index_dir.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(index_dir / "duplicate_index.sqlite3")
            try:
                connection.executescript(
                    """
                    CREATE TABLE meta (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
                    CREATE TABLE records (
                        catalog_key TEXT PRIMARY KEY,
                        record_id TEXT NOT NULL,
                        style_id TEXT NOT NULL,
                        lane TEXT NOT NULL,
                        title TEXT NOT NULL,
                        source_name TEXT,
                        source_url TEXT,
                        rights_status TEXT,
                        review_status TEXT,
                        prompt_sha256 TEXT,
                        local_asset_count INTEGER NOT NULL,
                        remote_asset_count INTEGER NOT NULL
                    );
                    CREATE TABLE assets (
                        asset_id TEXT PRIMARY KEY,
                        catalog_key TEXT NOT NULL,
                        asset_index INTEGER NOT NULL,
                        asset_sha256 TEXT NOT NULL
                    );
                    CREATE TABLE groups (
                        group_id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        member_count INTEGER NOT NULL,
                        display_title TEXT NOT NULL,
                        exact_sha256 TEXT,
                        phash_distance INTEGER,
                        dhash_distance INTEGER,
                        similarity_score REAL,
                        lanes_json TEXT NOT NULL,
                        sources_json TEXT NOT NULL,
                        thumbnail_uris_json TEXT NOT NULL,
                        recommendation_json TEXT NOT NULL,
                        search_text TEXT NOT NULL
                    );
                    CREATE TABLE group_members (
                        group_id TEXT NOT NULL,
                        ordinal INTEGER NOT NULL,
                        member_id TEXT NOT NULL,
                        catalog_key TEXT NOT NULL,
                        asset_id TEXT
                    );
                    """
                )
                connection.execute(
                    "INSERT INTO meta(key,value_json) VALUES(?,?)",
                    ("index_schema_version", json.dumps("archive-duplicate-index-1.1")),
                )
                connection.execute(
                    "INSERT INTO records(catalog_key,record_id,style_id,lane,title,source_name,source_url,rights_status,review_status,prompt_sha256,local_asset_count,remote_asset_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "fixture:fallback",
                        "fixture:fallback",
                        "STYLE-fixture:fallback",
                        "fixture",
                        "Fixture fallback",
                        "source-z",
                        "https://example.invalid/source-z",
                        "research_only_needs_review",
                        "needs_review",
                        sha256_text("Shared prompt body with {slot_name}."),
                        1,
                        0,
                    ),
                )
                connection.execute(
                    "INSERT INTO assets(asset_id,catalog_key,asset_index,asset_sha256) VALUES(?,?,?,?)",
                    ("asset-fallback", "fixture:fallback", 0, image_sha),
                )
                connection.execute(
                    "INSERT INTO groups(group_id,kind,member_count,display_title,exact_sha256,phash_distance,dhash_distance,similarity_score,lanes_json,sources_json,thumbnail_uris_json,recommendation_json,search_text) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "exact-prompt-fallback",
                        "exact_prompt",
                        1,
                        "Exact prompt | 1 records",
                        sha256_text("Shared prompt body with {slot_name}."),
                        None,
                        None,
                        1.0,
                        "[]",
                        "[]",
                        "[]",
                        "{}",
                        "fixture fallback",
                    ),
                )
                connection.execute(
                    "INSERT INTO group_members(group_id,ordinal,member_id,catalog_key,asset_id) VALUES(?,?,?,?,?)",
                    ("exact-prompt-fallback", 0, "record-fallback", "fixture:fallback", None),
                )
                connection.commit()
            finally:
                connection.close()

            manifest = build_manifest(root, limit=1)
            self.assertEqual(len(manifest["items"]), 1)
            self.assertEqual(manifest["items"][0]["group_seed_kind"], "exact_prompt")
            self.assertEqual(manifest["items"][0]["path"], "legacy/current_archive/images/fallback.png")


if __name__ == "__main__":
    unittest.main()
