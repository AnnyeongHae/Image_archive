from __future__ import annotations

import hashlib
import json
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
from image_rag_eval.experiment import digest, json_bytes, prepare, read_json  # noqa: E402
from image_rag_eval.expansion import build_expanded_manifest, plan_prepare50, prepare50  # noqa: E402


def sha256_file(path: Path) -> str:
    digestor = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digestor.update(chunk)
    return digestor.hexdigest()


def record(
    key: str,
    prompt_text: str,
    *,
    lane: str,
    source_name: str,
    local_uri: str,
    local_sha256: str,
) -> dict:
    return {
        "catalog_key": key,
        "record_id": key,
        "style_id": f"STYLE-{key}",
        "lane": lane,
        "title": f"Fixture {key}",
        "source": {"name": source_name, "url": f"https://example.invalid/{source_name}"},
        "rights": {"status": "research_only_needs_review", "release_eligible": False},
        "review_release": {"review_status": "needs_review"},
        "prompt": {
            "text": prompt_text,
            "sha256": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
        },
        "media": {
            "assets": [
                {
                    "uri_kind": "local",
                    "uri": local_uri,
                    "sha256": local_sha256,
                    "mime_type": "image/png",
                }
            ]
        },
    }


class ImageRagExpansionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.legacy_root = cls.root / "legacy" / "current_archive"
        image_root = cls.legacy_root / "images"
        image_root.mkdir(parents=True)

        def write_png(path: Path, color: tuple[int, int, int]) -> str:
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (48, 48), color).save(path, format="PNG")
            return sha256_file(path)

        rows = []
        shared_prompt = "Shared prompt for direct exact prompt grouping."
        long_prompt = "长篇视觉说明" * 1200
        for index in range(55):
            path = image_root / f"img-{index:02d}.png"
            sha = write_png(path, ((index * 19) % 255, (index * 31) % 255, (index * 47) % 255))
            prompt = shared_prompt if index in {2, 3} else f"Prompt {index:02d} with lane controls and source notes."
            if index == 54:
                prompt = long_prompt
            rows.append(
                record(
                    f"fixture:{index:02d}",
                    prompt,
                    lane=f"lane-{index % 5}",
                    source_name=f"source-{index % 9}",
                    local_uri=f"images/img-{index:02d}.png",
                    local_sha256=sha,
                )
            )
        # exact media pair
        shutil.copyfile(image_root / "img-00.png", image_root / "img-01.png")
        rows[1]["media"]["assets"][0]["sha256"] = sha256_file(image_root / "img-01.png")

        canonical = cls.root / "data" / "canonical" / "archive_records.jsonl"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

        cache_index = cls.root / "data" / "private-research" / "remote-media-canary" / "current" / "cache_index.json"
        cache_index.parent.mkdir(parents=True, exist_ok=True)
        cache_index.write_text(json.dumps({"schema_version": "remote-media-cache-index-1.0", "items": []}) + "\n", encoding="utf-8")

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
        build_duplicate_index(config, apply=True)
        cls.source_prepare = prepare(cls.root, "source20", limit=20)
        cls.source_dir = cls.root / "data" / "private-research" / "image-rag-canary" / "runs" / "source20"
        cls.source_manifest = read_json(cls.source_dir / "manifest.json")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_plan_prepare50_is_dry_run_and_validates_old20_subset(self) -> None:
        result = plan_prepare50(self.root, "source20", limit=50)

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["network_calls"], 0)
        self.assertEqual(result["writes"], 0)
        self.assertEqual(result["preserved_item_count"], 20)
        self.assertEqual(result["additional_item_count"], 30)
        self.assertEqual(result["target_item_count"], 50)
        self.assertEqual(result["pair_count"], 1225)
        self.assertTrue(result["preserved_subset_validated"])
        self.assertEqual(result["preserved_ids"], [item["id"] for item in self.source_manifest["items"]])
        manifest, _prepared_inputs, _meta = build_expanded_manifest(self.root, "source20", limit=50)
        self.assertTrue(manifest["experiment"]["preparation_only"])
        self.assertTrue(manifest["experiment"]["comparison_setup_pending"])
        self.assertEqual(manifest["experiment"]["arms"], {})
        self.assertEqual(manifest["experiment"]["max_inference_calls"], 0)

    def test_build_expanded_manifest_preserves_first20_payloads_and_caps_prompts(self) -> None:
        manifest, _prepared_inputs, meta = build_expanded_manifest(self.root, "source20", limit=50)

        self.assertEqual(len(manifest["items"]), 50)
        self.assertEqual(manifest["items"][:20], self.source_manifest["items"])
        self.assertEqual(meta["preserved_item_count"], 20)
        self.assertEqual(meta["additional_item_count"], 30)
        self.assertTrue(all(len(item["embedding_prompt"].encode("utf-8")) <= 6000 for item in manifest["items"]))
        self.assertEqual(len({item["id"] for item in manifest["items"]}), 50)
        self.assertTrue(all(item["external_ai_approved"] is False for item in manifest["items"]))

    def test_prepare50_apply_writes_additive_run_and_reuses_source_inputs(self) -> None:
        result = prepare50(self.root, "source20", "expanded50", limit=50, apply=True)

        destination = self.root / "data" / "private-research" / "image-rag-canary" / "runs" / "expanded50"
        manifest = read_json(destination / "manifest.json")
        prepared = read_json(destination / "prepared.json")
        offline = read_json(destination / "offline.json")

        self.assertEqual(result["status"], "prepared_local_only")
        self.assertEqual(result["items"], 50)
        self.assertEqual(result["pairs"], 1225)
        self.assertTrue(result["preserved_subset_validated"])
        self.assertEqual(manifest["items"][:20], self.source_manifest["items"])
        self.assertEqual(prepared["source_run_id"], "source20")
        self.assertEqual(prepared["source_manifest_sha256"], digest(json_bytes(self.source_manifest)))
        self.assertTrue(prepared["preserved_subset_validated"])
        self.assertEqual(prepared["pair_count"], 1225)
        self.assertEqual(len(offline["pairs"]), 1225)

        source_first = self.source_manifest["items"][0]
        source_bytes = (self.source_dir / source_first["prepared_path"]).read_bytes()
        target_bytes = (destination / source_first["prepared_path"]).read_bytes()
        self.assertEqual(source_bytes, target_bytes)

    def test_prepare50_dry_run_does_not_create_destination(self) -> None:
        result = prepare50(self.root, "source20", "dryrun50", limit=50, apply=False)

        self.assertEqual(result["status"], "dry_run")
        self.assertFalse((self.root / "data" / "private-research" / "image-rag-canary" / "runs" / "dryrun50").exists())


if __name__ == "__main__":
    unittest.main()
