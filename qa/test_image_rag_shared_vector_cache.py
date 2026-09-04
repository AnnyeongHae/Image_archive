from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from image_rag_eval import shared_vector_cache as module
from image_rag_eval import incremental_embedding as executor
from image_rag_eval.comparison import requests_for
from image_rag_eval.experiment import digest, json_bytes, read_json, run_path, write_json

VECTOR = [1.0] + [0.0] * 1023


def make_comparison(root, run_id, offset=0):
    run = run_path(root, run_id)
    (run / "inputs").mkdir(parents=True)
    originals = root / "originals" / run_id
    originals.mkdir(parents=True)
    manifest = {"run_id": run_id, "items": []}
    pixels = {}
    for index, color in enumerate([offset + 1, offset + 2, offset + 1]):
        original = originals / f"{index}.png"
        Image.new("RGB", (10, 12), (color, 3, 5)).save(original)
        blob, ident = original.read_bytes(), f"{run_id}-{index}"
        sha = digest(blob)
        (run / "inputs" / (sha + ".png")).write_bytes(blob)
        manifest["items"].append({"id": ident, "path": original.relative_to(root).as_posix(), "sha256": sha,
            "prepared_path": "inputs/" + sha + ".png", "prepared_sha256": sha, "embedding_prompt": "prompt"})
        pixels[ident] = 50000
    write_json(run / "manifest.json", manifest)
    write_json(run / "prepared.json", {"complete": True, "manifest_sha256": digest(json_bytes(manifest))})
    dest = run / "comparison-v1"
    (dest / "vector-cache").mkdir(parents=True)
    queries = [{"id": f"q{i}", "text": f"query {i}"} for i in range(2)]
    requests = requests_for(manifest, pixels, queries, arms_subset=["voyage_image"])
    write_json(dest / "queries.json", queries)
    write_json(dest / "budget.json", {"attempts": [{"key": key, "status": "completed"}
                                                  for key in sorted({r["key"] for r in requests})]})
    vectors = {"voyage_image": {}, "voyage_queries": {}}
    for request in requests:
        write_json(dest / "vector-cache" / (request["key"] + ".json"), {"key": request["key"],
            "provider": "voyage", "model": module.VOYAGE_MODEL, "vector": VECTOR,
            "vector_sha256": digest(json_bytes(VECTOR)), "usage": {}})
        vectors["voyage_queries" if request["kind"] == "query" else "voyage_image"][request["id"]] = VECTOR
    write_json(dest / "vectors.json", vectors)
    return run, requests


class SharedVectorCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run, self.requests = make_comparison(self.root, "old-run")

    def build(self, *run_ids, apply=True):
        with patch.object(executor, "load_credentials", side_effect=AssertionError("secret access")), \
             patch.object(executor.VoyageImageBatchClient, "embed_images", side_effect=AssertionError("network")):
            return module.build_shared_vector_cache(self.root, list(run_ids or ["old-run"]), apply=apply)

    def test_dry_run_never_writes_or_calls_provider(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = self.build(apply=False)
        self.assertEqual(result["writes"], 0)
        self.assertEqual(result["unique_vectors"], 4)
        self.assertEqual(result["document_aliases"], 3)
        self.assertEqual(result["query_vectors"], 2)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_shared_content_dedup_query_lookup_and_id_independence(self):
        result = self.build()
        self.assertEqual(result["writes"], 6)
        found = module.lookup_shared_vectors(self.root, self.requests)
        self.assertEqual(len(found), 4)
        self.assertTrue(all(receipt["vector"] == VECTOR for receipt in found.values()))
        request = {**self.requests[0], "id": "different-asset", "topk": 5}
        self.assertEqual(module.lookup_shared_vector(self.root, request)["key"], request["key"])
        self.assertEqual(self.requests[0]["key"], self.requests[2]["key"])
        self.assertFalse(result["human_approved"])

    def test_all_model_input_protocol_changes_are_misses(self):
        self.build()
        for field, value in {"model": "voyage-future", "provider": "other", "dimensions": 512,
                             "image_sha256": "f" * 64, "text": "new text", "task": "RETRIEVAL_QUERY",
                             "protocol": "future-protocol"}.items():
            request = {k: v for k, v in self.requests[0].items() if k != "key"}
            request[field] = value
            with self.subTest(field=field):
                self.assertIsNone(module.lookup_shared_vector(self.root, request))
        with self.assertRaisesRegex(ValueError, "request key"):
            module.lookup_shared_vector(self.root, {**self.requests[0], "text": "changed"})

    def test_missing_cache_returns_miss_without_writes(self):
        self.assertIsNone(module.lookup_shared_vector(self.root, self.requests[0]))
        self.assertFalse((self.root / module.RELATIVE_ROOT).exists())

    def test_bulk_lookup_validates_snapshot_once_and_no_outside_writes(self):
        self.build()
        with patch.object(module, "_validate_snapshot", wraps=module._validate_snapshot) as validate:
            result = module.lookup_shared_vectors(self.root, self.requests)
        self.assertEqual(validate.call_count, 1)
        self.assertTrue(all(result.values()))
        with self.assertRaisesRegex(ValueError, "escapes cache root"):
            module._append_json(self.root / "outside.json", {"bad": True}, cache_root=self.root / module.RELATIVE_ROOT)
        self.assertFalse((self.root / "outside.json").exists())

    def test_idempotent_existing_source_uses_frozen_evidence(self):
        first = self.build()
        # Later human import is additive and outside the frozen file list.
        (self.run / "group-workflow-v1/decision-imports/new").mkdir(parents=True)
        write_json(self.run / "group-workflow-v1/decision-imports/new/receipt.json", {"human": True})
        with patch.object(module, "_collect_source", side_effect=AssertionError("must use validated snapshot")):
            second = self.build()
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(first["revision_id"], second["revision_id"])
        self.assertEqual(second["writes"], 0)

    def test_growth_appends_revision_and_preserves_every_previous_file(self):
        first = self.build()
        before = {p: p.read_bytes() for p in (self.root / module.RELATIVE_ROOT).rglob("*") if p.is_file()}
        make_comparison(self.root, "second-run", offset=4)
        second = self.build("second-run")
        self.assertEqual(second["unique_vectors"], 6)
        self.assertEqual(second["document_aliases"], 6)
        self.assertNotEqual(first["revision_id"], second["revision_id"])
        self.assertTrue(all(p.read_bytes() == raw for p, raw in before.items()))
        old = module.lookup_shared_vector(self.root, self.requests[0], revision_id=first["revision_id"])
        self.assertEqual(old["shared_revision_id"], first["revision_id"])

    def test_source_original_hash_change_blocks_reuse(self):
        self.build()
        path = self.root / read_json(self.run / "manifest.json")["items"][0]["path"]
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "frozen vector source evidence"):
            module.lookup_shared_vector(self.root, self.requests[0])

    def test_original_receipt_and_ledger_changes_block_reuse(self):
        self.build()
        for path in (self.run / "comparison-v1/budget.json", self.run / "comparison-v1/vector-cache" / (self.requests[0]["key"] + ".json")):
            original = path.read_bytes()
            path.write_bytes(original + b" ")
            with self.subTest(path=path.name), self.assertRaisesRegex(ValueError, "frozen vector source evidence"):
                module.lookup_shared_vector(self.root, self.requests[0])
            path.write_bytes(original)

    def test_object_and_revision_tampering_block_lookup(self):
        result = self.build()
        receipt = module.lookup_shared_vector(self.root, self.requests[0])
        path = self.root / receipt["shared_object_path"]
        original = path.read_bytes()
        obj = read_json(path)
        obj["vector"][0] = .5
        write_json(path, obj)
        with self.assertRaisesRegex(ValueError, "object identity"):
            module.lookup_shared_vector(self.root, self.requests[0])
        path.write_bytes(original)
        revision = Path(result["revision_manifest_path"])
        manifest = read_json(revision)
        manifest["human_approval_inferred"] = True
        write_json(revision, manifest)
        with self.assertRaisesRegex(ValueError, "revision identity"):
            module.lookup_shared_vector(self.root, self.requests[0])

    def test_incomplete_revision_ignored_but_fork_is_not_implicitly_chosen(self):
        self.build()
        incomplete = self.root / module.RELATIVE_ROOT / "revisions" / ("e" * 64)
        incomplete.mkdir(parents=True)
        write_json(incomplete / "manifest.json", {"incomplete": True})
        self.assertIsNotNone(module.lookup_shared_vector(self.root, self.requests[0]))
        _, manifest = module._head(self.root)
        fork = copy.deepcopy(manifest)
        fork["branch"] = "test fork"
        ident = digest(json_bytes(fork))
        directory = self.root / module.RELATIVE_ROOT / "revisions" / ident
        directory.mkdir(parents=True)
        write_json(directory / "manifest.json", fork)
        write_json(directory / "receipt.json", {"schema_version": module.SCHEMA, "revision_id": ident, "complete": True})
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            module.lookup_shared_vector(self.root, self.requests[0])

    def test_bad_completion_vector_dimension_and_aggregate_rejected_at_import(self):
        budget = self.run / "comparison-v1/budget.json"
        value = read_json(budget)
        value["attempts"][0]["status"] = "failed_or_uncertain"
        write_json(budget, value)
        with self.assertRaisesRegex(ValueError, "completed reservation"):
            self.build(apply=False)
        value["attempts"][0]["status"] = "completed"
        write_json(budget, value)
        path = self.run / "comparison-v1/vector-cache" / (self.requests[0]["key"] + ".json")
        receipt = read_json(path)
        receipt["vector"] = [1.0]
        receipt["vector_sha256"] = digest(json_bytes(receipt["vector"]))
        write_json(path, receipt)
        with self.assertRaisesRegex(ValueError, "1024"):
            self.build(apply=False)

    def test_same_content_key_conflicting_vector_never_overwrites(self):
        self.build()
        run, requests = make_comparison(self.root, "same-images")
        alternate = [0.0, 1.0] + [0.0] * 1022
        aggregate = read_json(run / "comparison-v1/vectors.json")
        key = requests[0]["key"]
        path = run / "comparison-v1/vector-cache" / (key + ".json")
        receipt = read_json(path)
        receipt.update(vector=alternate, vector_sha256=digest(json_bytes(alternate)))
        write_json(path, receipt)
        for request in requests:
            if request["key"] == key:
                aggregate["voyage_image"][request["id"]] = alternate
        write_json(run / "comparison-v1/vectors.json", aggregate)
        with self.assertRaisesRegex(ValueError, "conflicting vectors"):
            self.build("same-images")
        self.assertEqual(module.lookup_shared_vector(self.root, self.requests[0])["vector"], VECTOR)

    def make_incremental(self):
        run_id = "new-case-run"
        run = run_path(self.root, run_id)
        (run / "inputs").mkdir(parents=True)
        original = self.root / "originals/new.png"
        Image.new("RGB", (13, 14), (99, 44, 22)).save(original)
        blob, sha = original.read_bytes(), digest(original.read_bytes())
        (run / "inputs" / (sha + ".png")).write_bytes(blob)
        bindings = {"schema_version": "image-incremental-source-bindings-1", "reference_run_id": "old-run",
                    "reference_ids": ["old-id"], "files": []}
        manifest = {"schema_version": "image-incremental-manifest-1", "run_id": run_id, "reference_run_id": "old-run",
            "source_bindings_sha256": digest(json_bytes(bindings)), "embedding_item_ids": ["new-id"],
            "items": [{"id": "new-id", "lane": "legacy", "style_id": "CASE-991",
                       "path": original.relative_to(self.root).as_posix(), "sha256": sha,
                       "prepared_path": "inputs/" + sha + ".png", "prepared_sha256": sha}]}
        write_json(run / "manifest.json", manifest)
        write_json(run / "source-bindings.json", bindings)
        write_json(run / "prepared.json", {"complete": True, "manifest_sha256": digest(json_bytes(manifest)),
                                          "source_bindings_sha256": digest(json_bytes(bindings))})
        validator = patch("image_rag_eval.incremental.validate_incremental_prepared", return_value=(manifest, bindings))
        validator.start()
        self.addCleanup(validator.stop)
        consent = executor.plan_incremental_embedding(self.root, run_id)["consent_template"]
        consent.update(approved=True, external_ai_approved=True, approved_by="fixture human")
        class Fake:
            def embed_images(self, images):
                return {"model": module.VOYAGE_MODEL, "data": [{"index": 0, "embedding": VECTOR}]}
        executor.execute_incremental_embedding(self.root, run_id, consent, maximum_usd=.10, client=Fake(), sleep=lambda _: None)
        return run

    def test_incremental_full_batch_checkpoint_and_originals_frozen(self):
        run = self.make_incremental()
        result = self.build("old-run", run.name)
        self.assertEqual(result["unique_vectors"], 5)
        self.assertEqual(result["document_aliases"], 4)
        manifest = read_json(Path(result["revision_manifest_path"]))
        source = manifest["sources"][run.name]
        self.assertTrue(any("batch-receipts" in p for p in source["files"]))
        # Original source image is required even if the preparation test stub
        # omitted it from bindings: the production collector must bind it.
        original = read_json(run / "manifest.json")["items"][0]["path"]
        self.assertIn(original, source["files"])

    def test_incremental_failed_execution_or_broken_full_batch_rejected(self):
        run = self.make_incremental()
        path = run / "embedding-v1/execution-receipt.json"
        execution = read_json(path)
        execution["status"] = "partial"
        write_json(path, execution)
        with self.assertRaisesRegex(ValueError, "complete source-bound"):
            self.build(run.name, apply=False)
        execution["status"] = "completed"
        write_json(path, execution)
        path = next((run / "embedding-v1/batch-receipts").glob("*.json"))
        batch = read_json(path)
        batch["source_bindings_sha256"] = "0" * 64
        write_json(path, batch)
        with self.assertRaisesRegex(ValueError, "batch checkpoint"):
            self.build(run.name, apply=False)


if __name__ == "__main__":
    unittest.main()
