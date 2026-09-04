from __future__ import annotations

import copy
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
from image_rag_eval.carryover import import_parent_cache_and_ledger  # noqa: E402
from image_rag_eval.comparison import execute_comparison, load_inputs, refresh_comparison  # noqa: E402
from image_rag_eval.expansion import prepare50  # noqa: E402
from image_rag_eval.experiment import digest, json_bytes, prepare, read_json, run_path, write_json  # noqa: E402


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
        "style_id": key.replace("fixture:", "FIXTURE-"),
        "record_id": key.replace("fixture:", "record-"),
        "lane": lane,
        "title": key,
        "source": {"name": source_name, "url": f"https://example.invalid/{source_name}/{key}"},
        "rights": {"status": "unknown"},
        "review": {"status": "needs_review"},
        "prompt": {"text": prompt_text},
        "media": {
            "assets": [
                {
                    "uri": local_uri,
                    "uri_kind": "local",
                    "sha256": local_sha256,
                    "mime_type": "image/png",
                }
            ]
        },
    }


class FakeClient:
    def __init__(self, dimensions: int):
        self.dimensions = dimensions
        self.calls = 0

    def embed(self, **kwargs):
        self.calls += 1
        return {"vector": [1.0] + [0.0] * (self.dimensions - 1), "usage": {"total_tokens": 12}}


class ExpandedComparisonTests(unittest.TestCase):
    PARENT_QUERIES = [
        {"id": f"q{i:02d}", "text": f"query {i:02d}", "relevance": {}, "human_judged": False}
        for i in range(1, 6)
    ]

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
        prepare(cls.root, "source20", limit=20)
        cls.source20_dir = run_path(cls.root, "source20")
        cls.source20_manifest = read_json(cls.source20_dir / "manifest.json")
        parent_consent = {
            "source_manifest_sha256": digest(json_bytes(cls.source20_manifest)),
            "authorization_source": "user_message",
            "user_quote": "synthetic expanded comparison fixture only",
            "recorded_at": "2026-09-03T00:00:00Z",
            "external_ai_approved": True,
            "max_cost_usd": 0.10,
            "providers": ["gemini", "voyage"],
            "approved_asset_ids": [item["id"] for item in cls.source20_manifest["items"]],
        }
        execute_comparison(
            cls.root,
            "source20",
            parent_consent,
            clients={"gemini": FakeClient(3072), "voyage": FakeClient(1024)},
            queries=copy.deepcopy(cls.PARENT_QUERIES),
            providers_subset=["voyage"],
            sleep=lambda _: None,
        )
        prepare50(cls.root, "source20", "expanded50", limit=50, apply=True)
        prepare50(cls.root, "source20", "expanded50_nocarry", limit=50, apply=True)
        import_parent_cache_and_ledger(cls.root, "source20", "expanded50", apply=True)
        cls.expanded50_dir = run_path(cls.root, "expanded50")
        cls.expanded50_nocarry_dir = run_path(cls.root, "expanded50_nocarry")
        cls.expanded50_manifest = read_json(cls.expanded50_dir / "manifest.json")
        cls.expanded50_receipt = read_json(cls.expanded50_dir / "prepared.json")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def consent_for(self, manifest: dict) -> dict:
        return {
            "source_manifest_sha256": digest(json_bytes(manifest)),
            "authorization_source": "user_message",
            "user_quote": "synthetic expanded comparison fixture only",
            "recorded_at": "2026-09-03T00:00:00Z",
            "external_ai_approved": True,
            "max_cost_usd": 0.10,
            "providers": ["gemini", "voyage"],
            "approved_asset_ids": [item["id"] for item in manifest["items"]],
        }

    def five_queries(self) -> list[dict]:
        return copy.deepcopy(self.PARENT_QUERIES)

    def test_default_limit_rejects_expanded_50_run(self) -> None:
        with self.assertRaises(ValueError):
            load_inputs(self.root, "expanded50")

    def test_explicit_50_limit_accepts_expanded_run_and_preserves_parent20(self) -> None:
        manifest, images, pixels = load_inputs(self.root, "expanded50", maximum_items=50)

        self.assertEqual(len(manifest["items"]), 50)
        self.assertEqual(manifest["items"][:20], self.source20_manifest["items"])
        self.assertEqual(len(images), 50)
        self.assertEqual(len(pixels), 50)
        self.assertEqual(
            read_json(self.expanded50_dir / "prepared.json")["source_manifest_sha256"],
            digest(json_bytes(self.source20_manifest)),
        )

    def test_first20_drift_rejected_even_if_child_receipt_rehashed(self) -> None:
        manifest = copy.deepcopy(self.expanded50_manifest)
        receipt = copy.deepcopy(self.expanded50_receipt)
        manifest["items"][0]["prompt"] = "drifted first20 payload"
        write_json(self.expanded50_dir / "manifest.json", manifest)
        receipt["manifest_sha256"] = digest(json_bytes(manifest))
        write_json(self.expanded50_dir / "prepared.json", receipt)

        try:
            with self.assertRaises(ValueError):
                load_inputs(self.root, "expanded50", maximum_items=50)
        finally:
            write_json(self.expanded50_dir / "manifest.json", self.expanded50_manifest)
            write_json(self.expanded50_dir / "prepared.json", self.expanded50_receipt)

    def test_more_than_50_items_rejected(self) -> None:
        manifest = copy.deepcopy(self.expanded50_manifest)
        receipt = copy.deepcopy(self.expanded50_receipt)
        manifest["items"].append(copy.deepcopy(manifest["items"][-1]))
        write_json(self.expanded50_dir / "manifest.json", manifest)
        receipt["manifest_sha256"] = digest(json_bytes(manifest))
        write_json(self.expanded50_dir / "prepared.json", receipt)

        try:
            with self.assertRaises(ValueError):
                load_inputs(self.root, "expanded50", maximum_items=50)
        finally:
            write_json(self.expanded50_dir / "manifest.json", self.expanded50_manifest)
            write_json(self.expanded50_dir / "prepared.json", self.expanded50_receipt)

    def test_missing_carryover_fails_before_fake_provider_call(self) -> None:
        manifest = read_json(self.expanded50_nocarry_dir / "manifest.json")
        consent = self.consent_for(manifest)
        clients = {"gemini": FakeClient(3072), "voyage": FakeClient(1024)}

        with self.assertRaisesRegex(ValueError, "carryover receipt is required"):
            execute_comparison(
                self.root,
                "expanded50_nocarry",
                consent,
                clients=clients,
                queries=self.five_queries(),
                providers_subset=["voyage"],
                sleep=lambda _: None,
                maximum_items=50,
            )
        self.assertEqual(clients["gemini"].calls, 0)
        self.assertEqual(clients["voyage"].calls, 0)

    def test_voyage_only_execute_and_refresh_supports_50_items_without_gemini_calls(self) -> None:
        manifest = read_json(self.expanded50_dir / "manifest.json")
        consent = self.consent_for(manifest)
        queries = self.five_queries()
        clients = {"gemini": FakeClient(3072), "voyage": FakeClient(1024)}

        result = execute_comparison(
            self.root,
            "expanded50",
            consent,
            clients=clients,
            queries=queries,
            providers_subset=["voyage"],
            sleep=lambda _: None,
            maximum_items=50,
        )

        self.assertEqual(result["status"], "partial_unjudged_canary")
        self.assertEqual(clients["gemini"].calls, 0)
        self.assertEqual(clients["voyage"].calls, 30)

        refreshed = refresh_comparison(self.root, "expanded50", maximum_items=50)
        self.assertEqual(refreshed["status"], "partial_unjudged_canary")
        self.assertEqual(refreshed["network_calls"], 0)
        self.assertEqual(refreshed["completed_arms"], ["voyage_image"])
        self.assertEqual(refreshed["vector_counts"]["voyage_image"], 50)
        self.assertEqual(refreshed["vector_counts"]["voyage_queries"], 5)
        self.assertEqual(refreshed["vector_counts"]["gemini_image"], 0)
        self.assertEqual(refreshed["vector_counts"]["gemini_joint"], 0)
        self.assertEqual(refreshed["budget"]["actual_invoice_usd"], None)
        evaluation = read_json(self.expanded50_dir / "comparison-v1" / "evaluation.json")
        self.assertIsNone(evaluation["winner"])


if __name__ == "__main__":
    unittest.main()
