"""Offline CLI evidence-gate tests; synthetic images/vectors in a temp archive."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import build_image_incremental_review as review_cli
from image_rag_eval import incremental
from image_rag_eval.experiment import digest, json_bytes, read_json, run_path, write_json
from image_rag_eval.expansion import _prepare_item
from image_rag_eval.incremental_embedding import execute_incremental_embedding, plan_incremental_embedding
import test_image_rag_incremental as preparation_fixture


def vector(axis: int) -> list[float]:
    return [1.0 if index == axis else 0.0 for index in range(1024)]


class _OfflineClient:
    def __init__(self) -> None:
        self.calls = 0

    def embed_images(self, images: list[bytes]) -> dict:
        self.calls += 1
        return {"model": "voyage-multimodal-3.5",
                "data": [{"index": index, "embedding": vector(1)} for index in range(len(images))],
                "usage": {"image_pixels": 3072 * len(images)}}


class IncrementalReviewCliTests(unittest.TestCase):
    def setUp(self) -> None:
        # Reuse the independent preparation fixture, not its test methods.
        self.fixture = preparation_fixture.IncrementalPreparationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.run_id = self.fixture.root, self.fixture.new_id
        self.reference = self.fixture.reference
        patcher = mock.patch.object(incremental, "REFERENCE_COUNT", 3)
        patcher.start()
        self.addCleanup(patcher.stop)
        member_path = self.root / "fixtures/retained-member.png"
        Image.new("RGB", (44, 62), "green").save(member_path)
        member, blob = _prepare_item(self.root, {"id": "old-member", "style_id": "MEMBER-001", "lane": "external",
            "catalog_key": "external:member", "path": member_path.relative_to(self.root).as_posix(),
            "sha256": digest(member_path.read_bytes()), "prompt": "retained member"})
        (self.reference / member["prepared_path"]).write_bytes(blob)
        parent = read_json(self.reference / "manifest.json")
        parent["items"].append(member)
        write_json(self.reference / "manifest.json", parent)
        write_json(self.reference / "prepared.json", {"complete": True, "manifest_sha256": digest(json_bytes(parent))})
        spec = read_json(self.reference / incremental.REFERENCE_SPEC_PATH)
        spec.pop("spec_sha256")
        spec["source_manifest_sha256"] = digest(json_bytes(parent))
        spec["stage1"]["active_ids"].append("old-member")
        spec["spec_sha256"] = digest(json_bytes(spec))
        write_json(self.reference / incremental.REFERENCE_SPEC_PATH, spec)
        write_json(self.reference / "comparison-v1/vectors.json", {
            "voyage_image": {"old": vector(0), "old-alias": vector(1), "old-member": vector(1)}})
        self.fixture.prepare(apply=True)
        plan = plan_incremental_embedding(self.root, self.run_id)
        consent = {**plan["consent_template"], "approved": True, "external_ai_approved": True,
                   "approved_by": "synthetic offline fixture; not a human decision"}
        self.client = _OfflineClient()
        result = execute_incremental_embedding(self.root, self.run_id, consent, maximum_usd=0.1,
            client=self.client, sleep=lambda _: None)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.client.calls, 1)
        self.source = run_path(self.root, self.run_id)
        self.embedding = self.source / "embedding-v1"

    def build(self, *, apply: bool = False) -> dict:
        return review_cli.build_review(self.root, self.run_id, apply=apply)

    def test_default_dry_run_is_nonmutating_and_checks_all_retained_members(self) -> None:
        before = {p.relative_to(self.root).as_posix(): digest(p.read_bytes()) for p in self.root.rglob("*") if p.is_file()}
        result = self.build()
        after = {p.relative_to(self.root).as_posix(): digest(p.read_bytes()) for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["writes"], 0)
        self.assertEqual(result["new_vectors"], 1)
        self.assertEqual(result["retained_reference_count"], 2)
        self.assertEqual(result["old_new_comparisons"], 2)
        self.assertEqual(result["provider_calls"], 0)
        self.assertFalse(result["human_approved"])

    def test_apply_keeps_sources_and_compares_member_without_auto_group_merge(self) -> None:
        originals = {p: digest(p.read_bytes()) for p in self.root.rglob("*") if p.is_file()}
        result = self.build(apply=True)
        self.assertEqual(result["writes"], 2)
        self.assertTrue(all(digest(p.read_bytes()) == sha for p, sha in originals.items()))
        evidence = read_json(self.source / "incremental-comparison.json")
        self.assertEqual(evidence["old_matches"][0]["top3_existing"][0]["id"], "old-member")
        self.assertEqual({row["id"] for row in evidence["old_matches"][0]["top3_existing"]}, {"old", "old-member"})
        self.assertNotIn("old-alias", {row["id"] for row in evidence["old_matches"][0]["top3_existing"]})
        self.assertEqual(evidence["automatic_group_merges"], 0)
        self.assertEqual(evidence["automatic_deletions"], 0)
        self.assertFalse(evidence["human_approved"])
        self.assertEqual(evidence["group_attachment_status"], "pending_imported_current_human_decisions")
        self.assertEqual(len(evidence["execution_receipt_sha256"]), 64)
        self.assertEqual(len(evidence["embedding_ledger_sha256"]), 64)
        self.assertEqual(len(evidence["validated_cache_receipts_sha256"]), 64)
        self.assertFalse((self.source / ".execution.lock").exists())

    def test_full_execution_receipt_binding_is_required(self) -> None:
        path = self.embedding / "execution-receipt.json"
        valid = read_json(path)
        for field, value in (("source_bindings_sha256", "wrong"), ("manifest_sha256", "wrong"),
                             ("run_id", "other-run"), ("schema_version", "old"), ("provider", "gemini"),
                             ("model", "other-model"), ("completed_image_ids", 0), ("target_image_ids", 2),
                             ("target_image_ids", True), ("status", "partial")):
            with self.subTest(field=field, value=value):
                write_json(path, {**valid, field: value})
                with self.assertRaisesRegex(ValueError, "complete bound new-only embedding execution required"):
                    self.build()
        write_json(path, valid)

    def test_missing_execution_receipt_fails_closed(self) -> None:
        (self.embedding / "execution-receipt.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.build()

    def test_aggregate_must_equal_validated_per_input_cache(self) -> None:
        payload = read_json(self.embedding / "vectors.json")
        ident = next(iter(payload["voyage_image"]))
        payload["voyage_image"][ident] = vector(0)
        write_json(self.embedding / "vectors.json", payload)
        with self.assertRaisesRegex(ValueError, "aggregate vector does not match"):
            self.build()

    def test_local_cache_requires_full_batch_checkpoint(self) -> None:
        path = next((self.embedding / "batch-receipts").glob("*.json"))
        payload = read_json(path)
        payload["status"] = "failed_or_uncertain"
        write_json(path, payload)
        with self.assertRaises(ValueError):
            self.build()

    def test_aggregate_requires_matching_digest_not_only_numeric_equality(self) -> None:
        payload = read_json(self.embedding / "vectors.json")
        ident = next(iter(payload["voyage_image"]))
        payload["voyage_image"][ident] = [int(value) for value in payload["voyage_image"][ident]]
        write_json(self.embedding / "vectors.json", payload)
        with self.assertRaisesRegex(ValueError, "aggregate vector does not match"):
            self.build()

    def test_cache_without_bound_reservation_is_rejected(self) -> None:
        path = self.embedding / "budget.json"
        payload = read_json(path)
        payload["attempts"] = []
        write_json(path, payload)
        with self.assertRaises(ValueError):
            self.build()

    def test_reuse_or_orphan_cache_cannot_bypass_ledger_binding(self) -> None:
        path = self.embedding / "budget.json"
        payload = read_json(path)
        payload["source_bindings_sha256"] = "another-snapshot"
        write_json(path, payload)
        with self.assertRaisesRegex(ValueError, "ledger identity mismatch"):
            self.build()

    def test_escaping_prepared_input_is_rejected_before_read_or_output(self) -> None:
        manifest = read_json(self.source / "manifest.json")
        manifest["items"][0]["prepared_path"] = "../outside.png"
        write_json(self.source / "manifest.json", manifest)
        receipt = read_json(self.source / "prepared.json")
        receipt["manifest_sha256"] = digest(json_bytes(manifest))
        write_json(self.source / "prepared.json", receipt)
        with self.assertRaisesRegex(ValueError, "prepared input escapes"):
            self.build()
        self.assertFalse((self.source / "incremental-comparison.json").exists())

    def test_cli_defaults_to_dry_run(self) -> None:
        stdout = io.StringIO()
        with mock.patch.object(review_cli, "ROOT", self.root), mock.patch.object(sys, "argv", ["build_image_incremental_review.py", "--run-id", self.run_id]), contextlib.redirect_stdout(stdout):
            review_cli.main()
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["writes"], 0)
        self.assertFalse((self.source / "incremental-comparison.html").exists())


if __name__ == "__main__":
    unittest.main()
