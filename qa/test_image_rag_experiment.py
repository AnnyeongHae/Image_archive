from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from image_rag_eval.experiment import (BudgetLedger, annotations_template, digest, execute,
    json_bytes, plan, prepare, prepared_image, read_json, review_html, run_path, run_lock,
    safe_source, unit_prefix, validate_annotations, write_json)
from image_rag_eval.similarity import image_signals


class FakeEmbedder:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def embed(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("synthetic transport failure")
        return {"vector": [1.0] + [0.0] * 3071, "usage": {"prompt_token_count": 10}}


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "sample.png"
        Image.new("RGB", (30, 30), "orange").save(self.source)
        self.run_id = "test-run"
        self.destination = run_path(self.root, self.run_id)
        self.destination.mkdir(parents=True)
        (self.destination / "inputs").mkdir()
        data = prepared_image(self.source)
        (self.destination / "inputs/a.png").write_bytes(data)
        self.manifest = {"schema_version": "1", "experiment": plan(1, 1), "items": [{
            "id": "sample-1", "style_id": "TEST-1", "path": "sample.png",
            "prompt": "an orange square", "embedding_prompt": "an orange square",
            "sha256": digest(self.source.read_bytes()), "signals": image_signals(self.source),
            "prepared_path": "inputs/a.png", "prepared_sha256": digest(data),
            "external_ai_approved": False,
        }]}
        write_json(self.destination / "manifest.json", self.manifest)
        write_json(self.destination / "prepared.json", {"complete": True,
            "manifest_sha256": digest(json_bytes(self.manifest))})
        self.annotations = annotations_template(self.manifest)
        self.annotations.update(reviewer="unit-test-fixture", reviewed_at="2026-09-03T00:00:00Z")
        self.annotations["items"][0]["approved_for_external_ai"] = True
        self.annotations["queries"] = [{"id": "q1", "text": "orange", "relevance": {}, "human_judged": False}]

    def test_default_plan_is_offline_and_conservative(self):
        result = plan()
        self.assertEqual(result["network_calls"], 0)
        self.assertEqual(result["writes"], 0)
        self.assertEqual(result["max_inference_calls"], 55)
        self.assertLess(result["reservation_upper_bound_usd"], 0.10)

    def test_spending_and_sample_approval_fail_closed(self):
        client = FakeEmbedder()
        with self.assertRaises(ValueError):
            execute(self.root, self.run_id, self.annotations, {}, allow_paid=False, embedder=client)
        self.annotations["items"][0]["approved_for_external_ai"] = False
        with self.assertRaises(ValueError):
            execute(self.root, self.run_id, self.annotations, {}, allow_paid=True, maximum_usd=.1, embedder=client)
        self.assertEqual(client.calls, 0)

    def test_budget_reserved_before_request_and_no_retry_after_failure(self):
        client = FakeEmbedder(fail=True)
        with self.assertRaises(RuntimeError):
            execute(self.root, self.run_id, self.annotations, {}, allow_paid=True, maximum_usd=.1, embedder=client)
        ledger = read_json(self.destination / "budget.json")
        self.assertEqual(ledger["attempts"][0]["status"], "failed_or_uncertain")
        with self.assertRaises(ValueError):
            execute(self.root, self.run_id, self.annotations, {}, allow_paid=True, maximum_usd=.1, embedder=client)
        self.assertEqual(client.calls, 1)

    def test_success_resume_uses_cache_and_leaves_accuracy_unknown(self):
        client = FakeEmbedder()
        before = digest(self.source.read_bytes())
        result = execute(self.root, self.run_id, self.annotations, {}, allow_paid=True,
            maximum_usd=.1, embedder=client, sleep=lambda _: None)
        self.assertEqual(client.calls, 3)
        self.assertTrue(all(entry["metrics"] is None for entry in result["retrieval"]))
        self.assertIsNone(result["winner"])
        execute(self.root, self.run_id, self.annotations, {}, allow_paid=True,
            maximum_usd=.1, embedder=client, sleep=lambda _: None)
        self.assertEqual(client.calls, 3)
        self.assertEqual(before, digest(self.source.read_bytes()))

    def test_source_changed_after_review_stops_all_calls(self):
        Image.new("RGB", (30, 30), "blue").save(self.source)
        client = FakeEmbedder()
        with self.assertRaises(ValueError):
            execute(self.root, self.run_id, self.annotations, {}, allow_paid=True, maximum_usd=.1, embedder=client)
        self.assertEqual(client.calls, 0)

    def test_manifest_changed_invalidates_review(self):
        altered = copy.deepcopy(self.manifest)
        altered["items"][0]["embedding_prompt"] = "different prompt"
        with self.assertRaises(ValueError):
            validate_annotations(altered, self.annotations, require_approval=True)

    def test_judged_queries_require_entire_sample_labels(self):
        self.annotations["queries"][0]["human_judged"] = True
        with self.assertRaises(ValueError):
            validate_annotations(self.manifest, self.annotations, require_approval=False)
        self.annotations["queries"][0]["relevance"] = {"sample-1": 3}
        validate_annotations(self.manifest, self.annotations, require_approval=False)

    def test_paths_and_non_images_are_rejected(self):
        for name in ("../outside.png", ".env", "anything.json"):
            with self.assertRaises(ValueError):
                safe_source(self.root, name)
        with self.assertRaises(ValueError):
            run_path(self.root, "../../outside")

    def test_budget_zero_oversized_nan_and_call_cap(self):
        for amount in (0, -.01, .11, float("nan")):
            with self.assertRaises(ValueError):
                BudgetLedger(self.destination / "budget.json", amount)
        ledger = BudgetLedger(self.destination / "budget.json", .001, maximum_calls=1)
        ledger.reserve("first", .0001)
        with self.assertRaises(ValueError):
            ledger.reserve("second", .0001)

    def test_entire_plan_budget_precheck_prevents_partial_spend(self):
        client = FakeEmbedder()
        with self.assertRaises(ValueError):
            execute(self.root, self.run_id, self.annotations, {}, allow_paid=True,
                maximum_usd=.001, embedder=client, sleep=lambda _: None)
        self.assertEqual(client.calls, 0)

    def test_another_canary_run_lock_blocks_parallel_paid_calls(self):
        client = FakeEmbedder()
        with run_lock(self.destination.parent):
            with self.assertRaises(FileExistsError):
                execute(self.root, self.run_id, self.annotations, {}, allow_paid=True,
                    maximum_usd=.1, embedder=client)
            self.assertTrue((self.destination.parent / ".execution.lock").exists())
        self.assertEqual(client.calls, 0)

    def test_incomplete_preparation_cannot_execute(self):
        write_json(self.destination / "prepared.json", {"complete": False})
        client = FakeEmbedder()
        with self.assertRaises(ValueError):
            execute(self.root, self.run_id, self.annotations, {}, allow_paid=True,
                maximum_usd=.1, embedder=client)
        self.assertEqual(client.calls, 0)

    def test_mrl_prefix_renormalizes_and_invalid_vectors_fail(self):
        self.assertEqual(unit_prefix([3.0, 4.0, 12.0], 2), [.6, .8])
        for vector in ([0.0], [float("nan")], [True]):
            with self.assertRaises(ValueError):
                unit_prefix(vector, 1)

    def test_review_html_escapes_untrusted_prompts(self):
        self.manifest["items"][0]["embedding_prompt"] = '</script><img src=x onerror="alert(1)">'
        document = review_html(self.manifest, {"groups": []})
        self.assertNotIn('<img src=x', document)
        self.assertIn('&lt;/script&gt;', document)

    def test_prepare_refuses_existing_run_and_stays_offline(self):
        with patch("image_rag_eval.dataset.build_manifest") as builder:
            with self.assertRaises(ValueError):
                prepare(self.root, self.run_id)
            builder.assert_not_called()


if __name__ == "__main__":
    unittest.main()
