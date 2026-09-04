"""Truthful finalization gates and immutable registry checkpoints, offline."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from finalize_luna_full_run import (FinalizeError, REGISTRY_SCHEMA, _aggregate, assemble_summary,
                                   finalize, normalize_turn_receipts, write_checkpoint)
from image_rag_eval.luna_analysis_import import digest, encode
from qa.test_luna_library_store import turn_token


def fixture(complete=True, known_usage=True):
    manifest = {"analysis_run_id": "fixture-full", "source_commit": {"id": "c1"},
                "counts": {"approved_images": 3, "legacy_reused": 1, "new_compact": 2},
                "tasks": [{"item_id": "i0", "style_id": "CASE-0", "analysis_mode": "legacy_reuse"},
                          {"item_id": "i1", "style_id": "CASE-1", "analysis_mode": "new_compact"},
                          {"item_id": "i2", "style_id": "CASE-2", "analysis_mode": "new_compact"}]}
    progress = {"completed_styles": ["CASE-1", "CASE-2"] if complete else ["CASE-1"],
                "new_valid": 2 if complete else 1, "legacy_reused": 1, "missing": 0 if complete else 1,
                "invalid": [], "input_invalid": [], "tasks_sha256": "a" * 64}
    token = turn_token(unknown=not known_usage)
    entry = {"receipt": token["receipt"], "raw": token["raw_json"].encode(), "path": token["path"]}
    usage = normalize_turn_receipts([entry], {"CASE-1": "i1", "CASE-2": "i2"})
    return manifest, progress, usage


class FullFinalizeTests(unittest.TestCase):
    def test_complete_requires_all_results_and_core_usage(self):
        manifest, progress, usage = fixture()
        summary = assemble_summary(manifest, progress, {}, usage, {}, allow_partial=False)
        self.assertEqual(summary["completion_status"], "analysis_and_usage_complete")
        self.assertTrue(summary["all_requested_analysis_complete"])
        self.assertTrue(summary["all_new_core_usage_observed"])
        self.assertFalse(summary["metadata_human_approved"])
        self.assertIsNone(summary["tokens"]["actual_billed_cost"])

    def test_incomplete_requires_allow_partial(self):
        manifest, progress, usage = fixture(complete=False)
        with self.assertRaises(FinalizeError):
            assemble_summary(manifest, progress, {}, usage, {})
        summary = assemble_summary(manifest, progress, {}, usage, {}, allow_partial=True)
        self.assertEqual(summary["completion_status"], "partial_checkpoint")
        self.assertEqual(summary["coverage"]["new_missing"], 1)
        self.assertFalse(summary["all_requested_analysis_complete"])

    def test_usage_pending_prevents_complete_even_with_all_results(self):
        manifest, progress, usage = fixture(known_usage=False)
        with self.assertRaises(FinalizeError):
            assemble_summary(manifest, progress, {}, usage, {})
        summary = assemble_summary(manifest, progress, {}, usage, {}, allow_partial=True)
        self.assertTrue(summary["all_requested_analysis_complete"])
        self.assertFalse(summary["all_new_core_usage_observed"])
        self.assertEqual(summary["coverage"]["new_usage_pending_images"], 2)
        self.assertIsNone(summary["tokens"]["new_execution"]["usage"]["total_tokens_calculated"])

    def test_optional_missing_cache_write_and_reasoning_not_core_failure(self):
        _, _, usage = fixture()
        self.assertEqual(usage["usage"]["total_tokens_calculated"], 120)
        self.assertEqual(usage["usage"]["uncached_input_tokens_calculated"], 30)
        self.assertIsNone(usage["usage"]["ordinary_input_tokens_calculated"])
        self.assertIsNone(usage["usage"]["reasoning_output_tokens"])

    def test_duplicate_existing_receipt_and_overlap_count_turn_once(self):
        token = turn_token()
        entry = {"receipt": token["receipt"], "raw": token["raw_json"].encode(), "path": "one.tokens.json"}
        duplicate = {**entry, "path": "two.tokens.json"}
        overlap = copy.deepcopy(entry)
        overlap["receipt"]["notes"] = ["later cumulative receipt with same completed turn"]
        overlap["raw"] = encode(overlap["receipt"])
        overlap["path"] = "three.tokens.json"
        usage = normalize_turn_receipts([entry, duplicate, overlap], {"CASE-1": "i1", "CASE-2": "i2"})
        self.assertEqual(usage["receipt_count_unique"], 2)
        self.assertEqual(usage["source_file_count"], 3)
        self.assertEqual(usage["turn_count_unique"], 1)
        self.assertEqual(usage["usage"]["total_tokens_calculated"], 120)

    def test_conflicting_same_turn_rejected(self):
        first, second = turn_token(), turn_token()
        second["receipt"]["turns"][0]["counter_mode"] = "changed"
        second["raw_json"] = json.dumps(second["receipt"])
        entries = [{"receipt": token["receipt"], "raw": token["raw_json"].encode(), "path": f"{index}.tokens.json"}
                   for index, token in enumerate((first, second))]
        with self.assertRaises(FinalizeError):
            normalize_turn_receipts(entries, {"CASE-1": "i1", "CASE-2": "i2"})

    def test_dry_run_no_new_database_when_partial_not_authorized(self):
        manifest, progress, usage = fixture(complete=False)
        with patch("finalize_luna_full_run.read_manifest", return_value=(manifest, encode(manifest))), \
             patch("finalize_luna_full_run.validate_progress", return_value=progress), \
             patch("finalize_luna_full_run.collect_usage", return_value=(usage, {})), \
             patch("finalize_luna_full_run.build_library_store") as builder:
            with self.assertRaises(FinalizeError):
                finalize(ROOT, apply=True)
            builder.assert_not_called()

    def test_progress_database_race_rejected(self):
        manifest, progress, usage = fixture()
        database = {"analysis_states": {"legacy_reused": 1, "validated_candidate": 1},
                    "results": 2, "source_commit_id": "c1", "usage_states": {"observed_turn_scope": 2}}
        with self.assertRaises(FinalizeError):
            assemble_summary(manifest, progress, database, usage, {}, allow_partial=True)

    def test_checkpoint_is_idempotent_and_registry_schema_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "archive"
            root.mkdir()
            schema = workspace / REGISTRY_SCHEMA
            schema.parent.mkdir(parents=True)
            schema.write_bytes((ROOT.parent / REGISTRY_SCHEMA).read_bytes())
            artifact = root / "contract.txt"
            artifact.write_text("immutable local evidence", encoding="utf-8")
            evidence = [{"path": "archive/contract.txt", "sha256": digest(artifact.read_bytes())}]
            manifest, progress, usage = fixture(complete=False)
            summary = assemble_summary(manifest, progress, {}, usage, {}, allow_partial=True)
            preview = write_checkpoint(root, summary, evidence)
            self.assertEqual(preview["status"], "dry_run")
            self.assertFalse((workspace / preview["execution_summary_path"]).exists())
            first = write_checkpoint(root, summary, evidence, apply=True)
            second = write_checkpoint(root, summary, evidence, apply=True)
            self.assertEqual(first["status"], "prepared")
            self.assertEqual(second["status"], "unchanged")
            registry = json.loads((workspace / first["content_registry_path"]).read_bytes())
            Draft202012Validator(json.loads(schema.read_bytes())).validate(registry)
            self.assertTrue(all(row["status"] == "needs_review" for row in registry["items"]))
            self.assertEqual(registry["items"][0]["artifact_sha256"], first["execution_summary_sha256"])

    def test_unknown_aggregate_is_not_zero(self):
        aggregate = _aggregate([])
        self.assertIsNone(aggregate["usage"]["total_tokens_calculated"])

    def test_normalized_results_not_counted_as_raw_strict(self):
        manifest, progress, usage = fixture()
        progress["literal_normalized_styles"] = ["CASE-1"]
        progress["raw_strict_valid_count"] = 1
        summary = assemble_summary(manifest, progress, {}, usage, {})
        self.assertEqual(summary["coverage"]["new_raw_strict_valid"], 1)
        self.assertEqual(summary["coverage"]["literal_normalized_count"], 1)
        self.assertEqual(summary["coverage"]["new_valid"], 2)


if __name__ == "__main__":
    unittest.main()
