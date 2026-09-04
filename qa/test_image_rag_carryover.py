from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from image_rag_eval.carryover import import_parent_cache_and_ledger, validate_parent_checkpoint
from image_rag_eval.comparison import load_inputs, requests_for
from image_rag_eval.experiment import digest, json_bytes, prepared_image, read_json, run_path, write_json


class CarryoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.parent_run_id = "parent20"
        self.new_run_id = "new50"
        self.parent_source = run_path(self.root, self.parent_run_id)
        self.new_source = run_path(self.root, self.new_run_id)
        (self.parent_source / "inputs").mkdir(parents=True)
        (self.new_source / "inputs").mkdir(parents=True)

        self._write_source_image("source-a.png", "red")
        self._write_source_image("source-b.png", "blue")
        self._write_source_image("source-c.png", "green")

        parent_items = [
            self._item("old-a", "source-a.png", "prompt a", self.parent_source / "inputs" / "old-a.png"),
            self._item("old-b", "source-b.png", "prompt b", self.parent_source / "inputs" / "old-b.png"),
        ]
        new_items = [
            self._item("new-a", "source-a.png", "prompt a", self.new_source / "inputs" / "new-a.png"),
            self._item("new-b", "source-b.png", "prompt b", self.new_source / "inputs" / "new-b.png"),
            self._item("new-c", "source-c.png", "prompt c", self.new_source / "inputs" / "new-c.png"),
        ]
        self.parent_manifest = {"items": parent_items}
        self.new_manifest = {"items": new_items}
        write_json(self.parent_source / "manifest.json", self.parent_manifest)
        write_json(self.new_source / "manifest.json", self.new_manifest)
        write_json(self.parent_source / "prepared.json", {"complete": True, "manifest_sha256": digest(json_bytes(self.parent_manifest))})
        write_json(self.new_source / "prepared.json", {"complete": True, "manifest_sha256": digest(json_bytes(self.new_manifest))})

        self.parent_queries = [{"id": "q1", "text": "prompt a", "relevance": {}, "human_judged": False}]
        self.parent_comparison = self.parent_source / "comparison-v1"
        (self.parent_comparison / "vector-cache").mkdir(parents=True)
        write_json(self.parent_comparison / "queries.json", self.parent_queries)

        parent_manifest, _, parent_pixels = load_inputs(self.root, self.parent_run_id)
        self.parent_requests = {request["key"]: request for request in requests_for(parent_manifest, parent_pixels, self.parent_queries)}
        gemini_joint = next(request for request in self.parent_requests.values() if request["provider"] == "gemini" and request["arm"] == "gemini_joint")
        voyage_query = next(request for request in self.parent_requests.values() if request["provider"] == "voyage" and request["kind"] == "query")

        self._write_cache_receipt(gemini_joint["key"], gemini_joint["model"], gemini_joint["dimensions"])
        self._write_cache_receipt(voyage_query["key"], voyage_query["model"], voyage_query["dimensions"])
        self.parent_ledger = {
            "attempts": [
                {"key": gemini_joint["key"], "reserved_usd": 0.001, "status": "failed_or_uncertain", "at": "2026-09-03T06:00:00Z", "usage": {}},
                {"key": gemini_joint["key"] + ":user-authorized-retry:phase-1", "reserved_usd": 0.001, "status": "completed", "at": "2026-09-03T06:10:00Z", "usage": {"prompt_token_count": 111}},
                {"key": voyage_query["key"], "reserved_usd": 0.001, "status": "completed", "at": "2026-09-03T06:20:00Z", "usage": {"text_tokens": 8}},
            ],
            "pricing_verified_date": "2026-09-03",
        }
        write_json(self.parent_comparison / "budget.json", self.parent_ledger)

    def _write_source_image(self, name: str, color: str) -> None:
        Image.new("RGB", (40, 30), color).save(self.root / name)

    def _item(self, item_id: str, source_name: str, prompt: str, prepared_path: Path) -> dict[str, object]:
        source_path = self.root / source_name
        data = prepared_image(source_path)
        prepared_path.write_bytes(data)
        return {
            "id": item_id,
            "style_id": item_id.upper(),
            "catalog_key": f"catalog:{item_id}",
            "path": source_name,
            "sha256": digest(source_path.read_bytes()),
            "prepared_sha256": digest(data),
            "prepared_path": str(prepared_path.relative_to(prepared_path.parents[1])).replace("\\", "/"),
            "prompt": prompt,
            "embedding_prompt": prompt,
        }

    def _write_cache_receipt(self, key: str, model: str, dimensions: int) -> None:
        vector = [1.0] + [0.0] * (dimensions - 1)
        payload = {
            "key": key,
            "provider": "gemini" if "gemini" in model else "voyage",
            "model": model,
            "usage": {},
            "latency_seconds": 0.1,
            "vector": vector,
            "vector_sha256": digest(json_bytes(vector)),
        }
        write_json(self.parent_comparison / "vector-cache" / f"{key}.json", payload)

    def test_dry_run_reports_without_writing_new_comparison_state(self):
        result = import_parent_cache_and_ledger(self.root, self.parent_run_id, self.new_run_id, apply=False)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["copied_attempt_count"], 3)
        self.assertEqual(result["copied_cache_receipt_count"], 2)
        self.assertFalse((self.new_source / "comparison-v1").exists())

    def test_apply_copies_validated_cache_and_preserves_parent_ledger(self):
        result = import_parent_cache_and_ledger(self.root, self.parent_run_id, self.new_run_id, apply=True)
        new_comparison = self.new_source / "comparison-v1"
        self.assertEqual(result["status"], "carried_over")
        self.assertEqual(read_json(new_comparison / "budget.json"), self.parent_ledger)
        self.assertEqual(read_json(new_comparison / "queries.json"), self.parent_queries)
        self.assertEqual(sorted(path.name for path in (new_comparison / "vector-cache").glob("*.json")), sorted(path.name for path in (self.parent_comparison / "vector-cache").glob("*.json")))
        receipt = read_json(new_comparison / "carryover.json")
        self.assertEqual(receipt["parent_ledger_sha256"], digest(json_bytes(self.parent_ledger)))
        self.assertEqual(receipt["copied_attempt_count"], 3)
        self.assertEqual(receipt["copied_cache_receipt_count"], 2)

    def test_apply_is_idempotent_when_existing_state_matches(self):
        first = import_parent_cache_and_ledger(self.root, self.parent_run_id, self.new_run_id, apply=True)
        second = import_parent_cache_and_ledger(self.root, self.parent_run_id, self.new_run_id, apply=True)
        self.assertEqual(first["copied_attempt_count"], second["copied_attempt_count"])
        self.assertEqual(first["copied_cache_receipt_count"], second["copied_cache_receipt_count"])

    def test_rejects_new_run_missing_parent_payload(self):
        broken = copy.deepcopy(self.new_manifest)
        broken["items"][1]["embedding_prompt"] = "changed prompt"
        write_json(self.new_source / "manifest.json", broken)
        write_json(self.new_source / "prepared.json", {"complete": True, "manifest_sha256": digest(json_bytes(broken))})
        with self.assertRaisesRegex(ValueError, "parent sample by payload"):
            import_parent_cache_and_ledger(self.root, self.parent_run_id, self.new_run_id, apply=False)

    def test_rejects_existing_mismatched_new_state(self):
        new_comparison = self.new_source / "comparison-v1"
        (new_comparison / "vector-cache").mkdir(parents=True)
        write_json(new_comparison / "budget.json", {"attempts": [], "pricing_verified_date": "2026-09-03"})
        with self.assertRaisesRegex(ValueError, "budget ledger"):
            import_parent_cache_and_ledger(self.root, self.parent_run_id, self.new_run_id, apply=False)

    def test_detects_parent_drift_against_existing_carryover_receipt(self):
        import_parent_cache_and_ledger(self.root, self.parent_run_id, self.new_run_id, apply=True)
        drifted = read_json(self.parent_comparison / "budget.json")
        drifted["attempts"][0]["reserved_usd"] = 0.002
        write_json(self.parent_comparison / "budget.json", drifted)
        with self.assertRaisesRegex(ValueError, "parent ledger changed"):
            import_parent_cache_and_ledger(self.root, self.parent_run_id, self.new_run_id, apply=False)

    def test_checkpoint_validator_passes_for_fresh_carried_state(self):
        import_parent_cache_and_ledger(self.root, self.parent_run_id, self.new_run_id, apply=True)
        ledger = read_json(self.new_source / "comparison-v1" / "budget.json")
        result = validate_parent_checkpoint(self.root, self.new_run_id, ledger)
        self.assertEqual(result["status"], "checkpoint_valid")
        self.assertEqual(result["copied_attempt_count"], 3)

    def test_checkpoint_validator_fails_when_parent_changes(self):
        import_parent_cache_and_ledger(self.root, self.parent_run_id, self.new_run_id, apply=True)
        ledger = read_json(self.new_source / "comparison-v1" / "budget.json")
        drifted = read_json(self.parent_comparison / "budget.json")
        drifted["attempts"][0]["reserved_usd"] = 0.002
        write_json(self.parent_comparison / "budget.json", drifted)
        with self.assertRaisesRegex(ValueError, "parent ledger changed"):
            validate_parent_checkpoint(self.root, self.new_run_id, ledger)

    def test_checkpoint_validator_fails_when_child_prefix_changes(self):
        import_parent_cache_and_ledger(self.root, self.parent_run_id, self.new_run_id, apply=True)
        ledger = read_json(self.new_source / "comparison-v1" / "budget.json")
        ledger["attempts"][0]["reserved_usd"] = 0.009
        with self.assertRaisesRegex(ValueError, "parent attempt prefix"):
            validate_parent_checkpoint(self.root, self.new_run_id, ledger)

    def test_checkpoint_validator_requires_existing_carryover_receipt(self):
        with self.assertRaisesRegex(ValueError, "carryover receipt is required"):
            validate_parent_checkpoint(self.root, self.new_run_id, {"attempts": []})


if __name__ == "__main__":
    unittest.main()
