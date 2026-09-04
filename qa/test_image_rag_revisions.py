from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ARCHIVE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = ARCHIVE_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from image_rag_eval.comparison import MODELS, add_order_evidence, load_inputs, requests_for  # noqa: E402
from image_rag_eval.experiment import digest, json_bytes, prepared_image, read_json, run_path, write_json  # noqa: E402
from image_rag_eval.retention import build_retention  # noqa: E402
from image_rag_eval.revisions import RESULTS_FILE, REVISION_DIRECTORY, revise_voyage_view  # noqa: E402


class RevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.parent_run_id = "parent20"
        self.child_run_id = "child21"
        self.parent_source = run_path(self.root, self.parent_run_id)
        self.child_source = run_path(self.root, self.child_run_id)
        (self.parent_source / "inputs").mkdir(parents=True)
        (self.child_source / "inputs").mkdir(parents=True)
        (self.root / "data" / "canonical").mkdir(parents=True)

        self.parent_items = []
        canonical_rows = []
        for index in range(20):
            item = self._build_item(index)
            self.parent_items.append(item)
            canonical_rows.append({"catalog_key": item["catalog_key"]})
        self.extra_item = self._build_item(20)
        canonical_rows.append({"catalog_key": self.extra_item["catalog_key"]})
        self._write_canonical(canonical_rows)

        self.parent_manifest = {"items": copy.deepcopy(self.parent_items)}
        self.child_manifest = {"items": copy.deepcopy(self.parent_items) + [copy.deepcopy(self.extra_item)]}
        write_json(self.parent_source / "manifest.json", self.parent_manifest)
        write_json(self.child_source / "manifest.json", self.child_manifest)

        self.parent_prepared = {
            "complete": True,
            "manifest_sha256": digest(json_bytes(self.parent_manifest)),
        }
        self.child_prepared = {
            "complete": True,
            "manifest_sha256": digest(json_bytes(self.child_manifest)),
            "source_run_id": self.parent_run_id,
            "preserved_item_count": 20,
            "source_manifest_sha256": digest(json_bytes(self.parent_manifest)),
        }
        write_json(self.parent_source / "prepared.json", self.parent_prepared)
        write_json(self.child_source / "prepared.json", self.child_prepared)

        self.queries = [{"id": "q01", "text": "sample query", "relevance": {}, "human_judged": False}]
        self.parent_comparison = self.parent_source / "comparison-v1"
        self.child_comparison = self.child_source / "comparison-v1"
        (self.parent_comparison / "vector-cache").mkdir(parents=True)
        (self.child_comparison / "vector-cache").mkdir(parents=True)
        write_json(self.parent_comparison / "queries.json", self.queries)
        write_json(self.child_comparison / "queries.json", self.queries)

        parent_manifest, _, parent_pixels = load_inputs(self.root, self.parent_run_id)
        child_manifest, _, child_pixels = load_inputs(self.root, self.child_run_id, 50)
        self.parent_requests = requests_for(parent_manifest, parent_pixels, self.queries, arms_subset=["voyage_image"])
        self.child_requests = requests_for(child_manifest, child_pixels, self.queries, arms_subset=["voyage_image"])

        self.parent_ledger = self._completed_ledger(self.parent_requests)
        parent_keys = {request["key"] for request in self.parent_requests}
        extra_child_requests = [request for request in self.child_requests if request["key"] not in parent_keys]
        self.child_ledger = {
            "attempts": self.parent_ledger["attempts"] + self._completed_attempts(extra_child_requests),
            "pricing_verified_date": "2026-09-03",
        }
        write_json(self.parent_comparison / "budget.json", self.parent_ledger)
        write_json(self.child_comparison / "budget.json", self.child_ledger)
        child_origin_manifest = add_order_evidence(self.root, copy.deepcopy(self.child_manifest))
        write_json(self.child_comparison / "retention.json", build_retention(child_origin_manifest["items"]))

        for request in self.child_requests:
            payload = self._cache_receipt(request)
            write_json(self.child_comparison / "vector-cache" / f"{request['key']}.json", payload)
            if any(parent["key"] == request["key"] for parent in self.parent_requests):
                write_json(self.parent_comparison / "vector-cache" / f"{request['key']}.json", payload)

        write_json(
            self.child_comparison / "carryover.json",
            {
                "status": "carryover_ready",
                "parent_run_id": self.parent_run_id,
                "parent_manifest_sha256": digest(json_bytes(self.parent_manifest)),
                "parent_prepared_receipt_sha256": digest(json_bytes(self.parent_prepared)),
                "parent_ledger_sha256": digest(json_bytes(self.parent_ledger)),
                "copied_attempt_count": len(self.parent_ledger["attempts"]),
            },
        )

    def _build_item(self, index: int) -> dict[str, object]:
        source_rel = f"sources/source-{index:02d}.png"
        source_path = self.root / source_rel
        source_path.parent.mkdir(parents=True, exist_ok=True)
        color = ((index * 31) % 256, (80 + index * 17) % 256, (140 + index * 23) % 256)
        Image.new("RGB", (48 + index, 40 + index), color).save(source_path)
        prepared = prepared_image(source_path)
        parent_input_name = f"asset-{index:02d}.png"
        child_input_name = f"asset-{index:02d}.png"
        (self.parent_source / "inputs" / parent_input_name).write_bytes(prepared)
        (self.child_source / "inputs" / child_input_name).write_bytes(prepared)
        item = {
            "id": f"asset-{index:02d}",
            "style_id": f"STYLE-{index:02d}",
            "catalog_key": f"catalog:asset-{index:02d}",
            "path": source_rel,
            "sha256": digest(source_path.read_bytes()),
            "prepared_sha256": digest(prepared),
            "prepared_path": f"inputs/{child_input_name}",
            "prompt": f"prompt {index}",
            "embedding_prompt": f"prompt {index}",
            "signals": {
                "sha256": digest(f"file-{index}".encode("utf-8")),
                "pixel_sha256": digest(f"pixel-{index}".encode("utf-8")),
                "phash": f"{index:016x}",
                "dhash": f"{(index + 1):016x}",
                "width": 48 + index,
                "height": 40 + index,
                "color_histogram": [1.0] + [0.0] * 23,
                "low_information": False,
                "metrics_max_side": 256,
            },
        }
        return item

    def _write_canonical(self, rows: list[dict[str, object]]) -> None:
        path = self.root / "data" / "canonical" / "archive_records.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _completed_attempts(self, requests: list[dict[str, object]]) -> list[dict[str, object]]:
        attempts = []
        for request in requests:
            attempts.append(
                {
                    "key": request["key"],
                    "reserved_usd": request["reserved_usd"],
                    "status": "completed",
                    "at": "2026-09-03T00:00:00Z",
                    "usage": {"text_tokens": 1 if request["kind"] == "query" else 0},
                }
            )
        return attempts

    def _completed_ledger(self, requests: list[dict[str, object]]) -> dict[str, object]:
        attempts = self._completed_attempts(requests)
        return {"attempts": attempts, "pricing_verified_date": "2026-09-03"}

    def _cache_receipt(self, request: dict[str, object]) -> dict[str, object]:
        dimensions = int(request["dimensions"])
        vector = [1.0] + [0.0] * (dimensions - 1)
        return {
            "key": request["key"],
            "provider": "voyage",
            "model": request["model"],
            "usage": {"text_tokens": 1 if request["kind"] == "query" else 0},
            "latency_seconds": 0.1,
            "vector": vector,
            "vector_sha256": digest(json_bytes(vector)),
        }

    def _file_hashes(self, base: Path) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for path in sorted(base.rglob("*")):
            if path.is_file():
                hashes[str(path.relative_to(base)).replace("\\", "/")] = digest(path.read_bytes())
        return hashes

    def test_dry_run_is_write_free_and_reports_voyage_only(self) -> None:
        before = self._file_hashes(self.child_comparison)

        result = revise_voyage_view(self.root, self.child_run_id, apply=False, maximum_items=50)

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["network_calls"], 0)
        self.assertEqual(result["new_embedding_calls"], 0)
        self.assertEqual(result["comparison_directory"], REVISION_DIRECTORY)
        self.assertEqual(result["unique_cache_receipts_reused"], len(self.child_requests))
        self.assertEqual(result["vector_counts"]["voyage_image"], len(self.child_manifest["items"]))
        self.assertEqual(result["vector_counts"]["voyage_queries"], len(self.queries))
        self.assertEqual(result["vector_counts"]["gemini_image"], 0)
        self.assertFalse((self.child_source / REVISION_DIRECTORY).exists())
        self.assertFalse((self.child_source / RESULTS_FILE).exists())
        self.assertEqual(before, self._file_hashes(self.child_comparison))

    def test_apply_creates_v2_and_preserves_v1_bytes(self) -> None:
        before_origin = self._file_hashes(self.child_comparison)
        before_budget = read_json(self.child_comparison / "budget.json")

        result = revise_voyage_view(self.root, self.child_run_id, apply=True, maximum_items=50)

        self.assertEqual(result["status"], "completed_unjudged_canary")
        self.assertEqual(before_budget, read_json(self.child_comparison / "budget.json"))
        self.assertEqual(before_origin, self._file_hashes(self.child_comparison))

        target = self.child_source / REVISION_DIRECTORY
        self.assertTrue(target.is_dir())
        self.assertTrue((self.child_source / RESULTS_FILE).is_file())

        manifest_v2 = read_json(target / "manifest.json")
        evaluation_v2 = read_json(target / "evaluation.json")
        receipt = read_json(target / "revision-receipt.json")

        self.assertEqual(manifest_v2["title"], "Voyage 이미지 검색 · 중복 정책 v2")
        self.assertEqual(manifest_v2["selection_profile"]["provider"], "voyage")
        self.assertEqual(manifest_v2["selection_profile"]["evaluation_arms"], ["voyage_image"])
        self.assertEqual(evaluation_v2["requested_arms"], ["voyage_image"])
        self.assertEqual(evaluation_v2["completed_arms"], ["voyage_image"])
        self.assertTrue(all(row["provider"] == MODELS["voyage"] for row in evaluation_v2["evaluations"]))
        self.assertEqual(evaluation_v2["vector_counts"]["voyage_image"], len(self.child_manifest["items"]))
        self.assertEqual(evaluation_v2["vector_counts"]["voyage_queries"], len(self.queries))
        self.assertEqual(evaluation_v2["vector_counts"]["gemini_image"], 0)
        self.assertEqual(evaluation_v2["vector_counts"]["gemini_joint"], 0)
        self.assertEqual(receipt["source_budget_sha256"], digest((self.child_comparison / "budget.json").read_bytes()))
        self.assertEqual(len(receipt["reused_cache_receipt_sha256"]), len(self.child_requests))
        self.assertEqual(set(receipt["reused_cache_receipt_sha256"]), {request["key"] for request in self.child_requests})

    def test_missing_voyage_cache_blocks_before_creating_v2(self) -> None:
        missing = self.child_requests[0]["key"]
        (self.child_comparison / "vector-cache" / f"{missing}.json").unlink()

        with self.assertRaises(FileNotFoundError):
            revise_voyage_view(self.root, self.child_run_id, apply=True, maximum_items=50)

        self.assertFalse((self.child_source / REVISION_DIRECTORY).exists())
        self.assertFalse((self.child_source / RESULTS_FILE).exists())

    def test_corrupt_voyage_cache_blocks_before_creating_v2(self) -> None:
        request = self.child_requests[0]
        path = self.child_comparison / "vector-cache" / f"{request['key']}.json"
        payload = read_json(path)
        payload["vector_sha256"] = "0" * 64
        write_json(path, payload)

        with self.assertRaisesRegex(ValueError, "Voyage cache receipt lacks valid identity or completed attempt"):
            revise_voyage_view(self.root, self.child_run_id, apply=True, maximum_items=50)

        self.assertFalse((self.child_source / REVISION_DIRECTORY).exists())
        self.assertFalse((self.child_source / RESULTS_FILE).exists())

    def test_repeated_apply_refuses_overwrite(self) -> None:
        revise_voyage_view(self.root, self.child_run_id, apply=True, maximum_items=50)

        with self.assertRaisesRegex(FileExistsError, "revision already exists"):
            revise_voyage_view(self.root, self.child_run_id, apply=True, maximum_items=50)


if __name__ == "__main__":
    unittest.main()
