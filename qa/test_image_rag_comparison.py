from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from image_rag_eval.comparison import (execute_comparison, plan_comparison, prepare_comparison_view, requests_for, validate_consent, refresh_comparison)
from image_rag_eval.experiment import digest, json_bytes, prepared_image, read_json, run_path, write_json
from image_rag_eval.similarity import image_signals


class FakeClient:
    def __init__(self, dimensions, fail=False):
        self.dimensions, self.fail, self.calls = dimensions, fail, 0

    def embed(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("mock failure")
        return {"vector": [1.0] + [0.0] * (self.dimensions - 1), "usage": {"total_tokens": 12}}


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run_id = "sample"
        self.source = run_path(self.root, self.run_id)
        (self.source / "inputs").mkdir(parents=True)
        (self.root / "data/canonical").mkdir(parents=True)
        Image.new("RGB", (40, 30), "red").save(self.root / "sample.png")
        data = prepared_image(self.root / "sample.png")
        (self.source / "inputs/sample.png").write_bytes(data)
        item = {"id": "asset1", "style_id": "TEST-1", "catalog_key": "test:1", "path": "sample.png",
            "sha256": digest((self.root / "sample.png").read_bytes()), "prepared_sha256": digest(data),
            "prepared_path": "inputs/sample.png", "prompt": "red block", "embedding_prompt": "red block",
            "signals": image_signals(self.root / "sample.png")}
        self.manifest = {"items": [item]}
        write_json(self.source / "manifest.json", self.manifest)
        write_json(self.source / "prepared.json", {"complete": True, "manifest_sha256": digest(json_bytes(self.manifest))})
        (self.root / "data/canonical/archive_records.jsonl").write_text(json.dumps({"catalog_key": "test:1"}) + "\n")
        self.consent = {"source_manifest_sha256": digest(json_bytes(self.manifest)),
            "authorization_source": "user_message", "user_quote": "synthetic unit fixture only",
            "recorded_at": "2026-09-03T00:00:00Z", "external_ai_approved": True,
            "max_cost_usd": .1, "providers": ["gemini", "voyage"], "approved_asset_ids": ["asset1"]}
        self.queries = [{"id": "q1", "text": "red", "relevance": {}, "human_judged": False}]
        self.clients = {"gemini": FakeClient(3072), "voyage": FakeClient(1024)}

    def run_fake(self, **kwargs):
        return execute_comparison(self.root, self.run_id, self.consent, clients=self.clients,
            queries=self.queries, sleep=lambda _: None, **kwargs)

    def test_plan_does_not_write_or_call(self):
        before = set(self.source.rglob("*"))
        result = plan_comparison(self.root, self.run_id, self.queries)
        self.assertEqual(result["unique_requests"], 5)
        self.assertEqual(result["network_calls"], 0)
        self.assertEqual(before, set(self.source.rglob("*")))

    def test_plan_single_arm_limits_documents_and_queries_to_selected_provider(self):
        result = plan_comparison(self.root, self.run_id, self.queries, arms_subset=["gemini_image"])
        self.assertEqual(result["arms"], ["gemini_image"])
        self.assertEqual(result["logical_requests"], 2)
        self.assertEqual(result["unique_requests"], 2)

    def test_active_profile_defaults_to_voyage_and_blocks_gemini(self):
        profile_dir = self.root / "data/private-research/image-rag-canary"
        profile_dir.mkdir(parents=True, exist_ok=True)
        write_json(profile_dir / "active-profile.json", {
            "schema_version": "1", "status": "active",
            "enabled_providers": ["voyage"], "default_arms": ["voyage_image"],
        })
        planned = plan_comparison(self.root, self.run_id, self.queries)
        self.assertEqual(planned["arms"], ["voyage_image"])
        with self.assertRaises(ValueError):
            self.run_fake(providers_subset=["gemini"], arms_subset=["gemini_image"])
        self.assertEqual(sum(client.calls for client in self.clients.values()), 0)

    def test_offline_retention_view_has_no_paid_calls(self):
        result = prepare_comparison_view(self.root, self.run_id)
        self.assertEqual(result["embedding_requests"], 0)
        self.assertEqual(result["physical_files_deleted_or_moved"], 0)
        self.assertEqual(result["arrival_timestamps_missing"], 1)
        self.assertFalse((self.source / "comparison-v1/budget.json").exists())

    def test_success_resume_cache_and_unjudged_results(self):
        self.run_fake()
        self.assertEqual(self.clients["gemini"].calls, 3)
        self.assertEqual(self.clients["voyage"].calls, 2)
        self.run_fake()
        self.assertEqual(self.clients["gemini"].calls, 3)
        evaluation = read_json(self.source / "comparison-v1/evaluation.json")
        self.assertIsNone(evaluation["winner"])
        self.assertTrue(all(row["metrics"] is None for row in evaluation["evaluations"]))
        self.assertTrue((self.source / "comparison-results-v1.html").is_file())

    def test_combined_budget_checked_before_calls(self):
        with self.assertRaises(ValueError):
            self.run_fake(maximum_usd=.0001)
        self.assertEqual(sum(c.calls for c in self.clients.values()), 0)

    def test_requests_single_arm_excludes_joint_text_and_other_provider_queries(self):
        requests = requests_for(
            self.manifest,
            {"asset1": 50_000},
            self.queries,
            arms_subset=["gemini_image"],
        )
        self.assertEqual(len(requests), 2)
        self.assertEqual([request["arm"] for request in requests if request["kind"] == "document"], ["gemini_image"])
        self.assertEqual([request["provider"] for request in requests if request["kind"] == "query"], ["gemini"])
        self.assertEqual([request["text"] for request in requests if request["kind"] == "document"], [""])

    def test_single_arm_execute_uses_selected_provider_queries_without_joint_requests(self):
        result = self.run_fake(arms_subset=["gemini_image"])
        self.assertEqual(result["new_requests_this_invocation"], 2)
        self.assertEqual(self.clients["gemini"].calls, 2)
        self.assertEqual(self.clients["voyage"].calls, 0)
        vectors = read_json(self.source / "comparison-v1/vectors.json")
        self.assertEqual(len(vectors["gemini_image"]), 1)
        self.assertEqual(len(vectors["gemini_joint"]), 0)
        self.assertEqual(len(vectors["voyage_image"]), 0)
        self.assertEqual(len(vectors["gemini_queries"]), 1)
        self.assertEqual(len(vectors["voyage_queries"]), 0)

    def test_full_plan_can_be_overbudget_even_when_bounded_first_three_would_fit(self):
        with self.assertRaises(ValueError):
            self.run_fake(maximum_usd=.002)
        self.assertEqual(self.clients["gemini"].calls, 0)
        self.assertEqual(self.clients["voyage"].calls, 0)

    def test_bounded_first_three_uncached_requests_fit_and_make_three_calls(self):
        result = self.run_fake(maximum_usd=.002, max_new_requests=3, request_interval_seconds=20)
        self.assertEqual(result["new_requests_this_invocation"], 3)
        self.assertEqual(self.clients["gemini"].calls, 2)
        self.assertEqual(self.clients["voyage"].calls, 1)
        vectors = read_json(self.source / "comparison-v1/vectors.json")
        self.assertEqual(len(vectors["gemini_image"]), 1)
        self.assertEqual(len(vectors["gemini_joint"]), 1)
        self.assertEqual(len(vectors["voyage_image"]), 1)
        self.assertEqual(len(vectors["gemini_queries"]), 0)
        self.assertEqual(len(vectors["voyage_queries"]), 0)

    def test_bounded_first_three_overbudget_blocks_zero_and_preserves_cached_results(self):
        self.run_fake(providers_subset=["voyage"])
        before_vectors = read_json(self.source / "comparison-v1/vectors.json")
        before_ledger = read_json(self.source / "comparison-v1/budget.json")
        self.assertEqual(self.clients["voyage"].calls, 2)

        with self.assertRaises(ValueError):
            self.run_fake(maximum_usd=.003, max_new_requests=3, request_interval_seconds=20)

        self.assertEqual(self.clients["gemini"].calls, 0)
        self.assertEqual(self.clients["voyage"].calls, 2)
        after_vectors = read_json(self.source / "comparison-v1/vectors.json")
        after_ledger = read_json(self.source / "comparison-v1/budget.json")
        self.assertEqual(after_vectors, before_vectors)
        self.assertEqual(after_ledger, before_ledger)

    def test_single_arm_run_preserves_other_provider_cache(self):
        self.run_fake(providers_subset=["voyage"])
        self.assertEqual(self.clients["voyage"].calls, 2)
        result = self.run_fake(arms_subset=["gemini_image"])
        self.assertEqual(result["new_requests_this_invocation"], 2)
        self.assertEqual(self.clients["voyage"].calls, 2)
        self.assertEqual(self.clients["gemini"].calls, 2)
        cache_dir = self.source / "comparison-v1" / "vector-cache"
        self.assertEqual(len(list(cache_dir.glob("*.json"))), 4)
        vectors = read_json(self.source / "comparison-v1/vectors.json")
        self.assertEqual(len(vectors["voyage_image"]), 1)
        self.assertEqual(len(vectors["voyage_queries"]), 1)
        evaluation = read_json(self.source / "comparison-v1/evaluation.json")
        self.assertEqual(set(evaluation["completed_arms"]), {"gemini_image", "voyage_image"})
        self.run_fake(providers_subset=["voyage"])
        self.assertEqual(self.clients["voyage"].calls, 2)

    def test_invalid_arm_provider_intersection_fails_before_calls(self):
        with self.assertRaises(ValueError):
            self.run_fake(providers_subset=["voyage"], arms_subset=["gemini_image"])
        self.assertEqual(self.clients["gemini"].calls, 0)
        self.assertEqual(self.clients["voyage"].calls, 0)

    def test_authorization_bound_to_manifest_and_providers(self):
        self.consent["providers"] = ["gemini"]
        with self.assertRaises(ValueError):
            self.run_fake()
        self.assertEqual(sum(c.calls for c in self.clients.values()), 0)

    def test_provider_failure_persists_reservation_and_blocks_retry(self):
        self.clients["gemini"].fail = True
        with self.assertRaises(RuntimeError):
            self.run_fake()
        with self.assertRaises(ValueError):
            self.run_fake()
        self.assertEqual(self.clients["gemini"].calls, 1)
        self.assertEqual(read_json(self.source / "comparison-v1/budget.json")["attempts"][0]["status"], "failed_or_uncertain")

    def test_duplicate_input_requests_share_cache_key(self):
        duplicate = copy.deepcopy(self.manifest["items"][0])
        duplicate["id"] = "asset2"
        manifest = {"items": [self.manifest["items"][0], duplicate]}
        requests = requests_for(manifest, {"asset1": 50_000, "asset2": 50_000}, self.queries)
        self.assertEqual(len(requests), 8)
        self.assertEqual(len({r["key"] for r in requests}), 5)

    def test_oversized_or_invalid_budget_rejected(self):
        for amount in (.11, float("nan"), 0):
            with self.assertRaises(ValueError):
                validate_consent(self.consent, self.manifest, amount)

    def recovery_fixture(self):
        ledger_path = self.source / "comparison-v1/budget.json"
        ledger = read_json(ledger_path)
        ledger["attempts"][0]["at"] = "2026-01-01T00:00:00Z"
        ledger["attempts"][0].update({"http_status": 429, "provider_status": "rate_limited", "quota_exhausted": False})
        write_json(ledger_path, ledger)
        return {"source_manifest_sha256": digest(json_bytes(self.manifest)),
            "ledger_sha256": digest(json_bytes(ledger)), "http_status": 429,
            "maximum_retries": 1, "observation_source": "reviewed_execution_output",
            "evidence_note": "synthetic fixture HTTP429 observation", "observed_at": "2026-01-01T00:00:00Z",
            "request_key": ledger["attempts"][0]["key"]}

    def test_reviewed_recovery_preserves_failed_reservation_and_cache(self):
        self.clients["gemini"].fail = True
        with self.assertRaises(RuntimeError):
            self.run_fake()
        evidence = self.recovery_fixture()
        self.clients["gemini"].fail = False
        self.run_fake(retry_evidence=evidence)
        ledger = read_json(self.source / "comparison-v1/budget.json")
        self.assertEqual(len(ledger["attempts"]), 6)
        self.assertEqual(ledger["attempts"][0]["status"], "failed_or_uncertain")
        self.assertTrue(ledger["attempts"][1]["key"].endswith(":http429-recovery-1"))
        calls = sum(c.calls for c in self.clients.values())
        self.run_fake()
        self.assertEqual(sum(c.calls for c in self.clients.values()), calls)

    def test_independent_provider_uses_same_ledger_and_never_retries_failed_provider(self):
        self.clients["gemini"].fail = True
        with self.assertRaises(RuntimeError):
            self.run_fake()
        result = self.run_fake(providers_subset=["voyage"])
        self.assertEqual(self.clients["gemini"].calls, 1)
        self.assertEqual(self.clients["voyage"].calls, 2)
        self.assertEqual(result["status"], "partial_unjudged_canary")
        ledger = read_json(self.source / "comparison-v1/budget.json")
        self.assertEqual(len(ledger["attempts"]), 3)
        self.assertEqual(ledger["attempts"][0]["status"], "failed_or_uncertain")
        result = refresh_comparison(self.root, self.run_id)
        self.assertEqual(result["network_calls"], 0)
        self.assertEqual(result["completed_arms"], ["voyage_image"])
        evaluation = read_json(self.source / "comparison-v1/evaluation.json")
        self.assertEqual(len(evaluation["evaluations"]), 1)
        self.assertIsNone(evaluation["winner"])
        self.assertEqual(len(read_json(self.source / "comparison-v1/budget.json")["attempts"]), 3)

    def test_recovery_rejects_wrong_status_or_stale_ledger_before_calls(self):
        self.clients["gemini"].fail = True
        with self.assertRaises(RuntimeError):
            self.run_fake()
        evidence = self.recovery_fixture()
        for bad in ({**evidence, "http_status": 500}, {**evidence, "ledger_sha256": "bad"},
                    {**evidence, "observed_at": "2999-01-01T00:00:00Z"}):
            with self.assertRaises(ValueError):
                self.run_fake(retry_evidence=bad)
        self.assertEqual(self.clients["gemini"].calls, 1)

    def test_recovery_rejects_fabricated_429_over_ambiguous_ledger_failure(self):
        self.clients["gemini"].fail = True
        with self.assertRaises(RuntimeError):
            self.run_fake()
        evidence = self.recovery_fixture()
        path = self.source / "comparison-v1/budget.json"
        ledger = read_json(path)
        ledger["attempts"][0]["http_status"] = 500
        write_json(path, ledger)
        evidence["ledger_sha256"] = digest(json_bytes(ledger))
        with self.assertRaises(ValueError):
            self.run_fake(retry_evidence=evidence)
        self.assertEqual(self.clients["gemini"].calls, 1)

    def test_recovery_cannot_repeat_or_exceed_combined_budget(self):
        self.clients["gemini"].fail = True
        with self.assertRaises(RuntimeError):
            self.run_fake()
        evidence = self.recovery_fixture()
        with self.assertRaises(ValueError):
            self.run_fake(retry_evidence=evidence, maximum_usd=.0002)
        self.assertEqual(self.clients["gemini"].calls, 1)
        with self.assertRaises(RuntimeError):
            self.run_fake(retry_evidence=evidence)
        ledger = read_json(self.source / "comparison-v1/budget.json")
        evidence["ledger_sha256"] = digest(json_bytes(ledger))
        with self.assertRaises(ValueError):
            self.run_fake(retry_evidence=evidence)
        self.assertEqual(self.clients["gemini"].calls, 2)

    def test_bounded_probe_preserves_other_provider_cache(self):
        self.run_fake(providers_subset=["voyage"])
        result = self.run_fake(providers_subset=["gemini"], max_new_requests=1, request_interval_seconds=20)
        self.assertEqual(result["new_requests_this_invocation"], 1)
        self.assertEqual(self.clients["gemini"].calls, 1)
        vectors = read_json(self.source / "comparison-v1/vectors.json")
        self.assertEqual(len(vectors["voyage_image"]), 1)
        self.assertEqual(len(vectors["voyage_queries"]), 1)
        self.run_fake()
        self.assertEqual(self.clients["gemini"].calls, 3)

    def test_new_user_authorization_can_renew_exact_429_once(self):
        self.clients["gemini"].fail = True
        with self.assertRaises(RuntimeError):
            self.run_fake()
        evidence = self.recovery_fixture()
        path = self.source / "comparison-v1/budget.json"
        ledger = read_json(path)
        ledger["attempts"].append({"key": "other:http429-recovery-1", "reserved_usd": .0001, "status": "completed", "at": "2026-01-01T00:00:00Z"})
        write_json(path, ledger)
        evidence["ledger_sha256"] = digest(json_bytes(ledger))
        with self.assertRaises(ValueError):
            self.run_fake(retry_evidence=evidence)
        self.consent["renewed_retry"] = {"request_key": evidence["request_key"], "phase_id": "fixture-new-user-request", "max_additional_attempts": 1}
        evidence["renewed_authorization_sha256"] = digest(json_bytes(self.consent))
        self.clients["gemini"].fail = False
        result = self.run_fake(retry_evidence=evidence, max_new_requests=1)
        self.assertEqual(result["new_requests_this_invocation"], 1)
        ledger = read_json(path)
        self.assertIn(":user-authorized-retry:", ledger["attempts"][-1]["key"])
        self.assertEqual(ledger["attempts"][0]["status"], "failed_or_uncertain")


if __name__ == "__main__":
    unittest.main()
