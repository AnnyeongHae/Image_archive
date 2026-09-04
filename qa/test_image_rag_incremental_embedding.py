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
from image_rag_eval import incremental_embedding as module
from image_rag_eval.experiment import digest, json_bytes, read_json, run_path, write_json
from image_rag_eval.providers import ProviderError


VECTOR = [1.0] + [0.0] * 1023


def response(count):
    return {"model": module.VOYAGE_MODEL,
            "data": [{"index": i, "embedding": VECTOR} for i in reversed(range(count))],
            "usage": {"image_pixels": count * 50000, "text_tokens": 0, "secret": "not retained"}}


class FakeClient:
    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at

    def embed_images(self, images):
        self.calls.append(images)
        if self.fail_at == len(self.calls):
            raise ProviderError("voyage", 429)
        return response(len(images))


class IncrementalEmbeddingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.run_id = "case-incremental-test"
        self.run = run_path(self.root, self.run_id)
        (self.run / "inputs").mkdir(parents=True)
        (self.root / "originals").mkdir()
        self.bindings = {"schema_version": "image-incremental-source-bindings-1",
                         "reference_run_id": "old200", "reference_ids": ["old-id"], "files": []}
        self.manifest = {"schema_version": "image-incremental-manifest-1", "run_id": self.run_id,
                         "reference_run_id": "old200", "items": [], "embedding_item_ids": []}
        for i in range(10):
            original = self.root / "originals" / f"{i}.png"
            Image.new("RGB", (10 + i, 10), (i * 20, 2, 3)).save(original)
            blob = original.read_bytes()
            sha = digest(blob)
            prepared = self.run / "inputs" / (sha + ".png")
            prepared.write_bytes(blob)
            self.manifest["items"].append({"id": f"new-{i}", "lane": "legacy", "style_id": f"CASE-{i+1:03d}",
                "path": original.relative_to(self.root).as_posix(), "sha256": sha,
                "prepared_path": prepared.relative_to(self.run).as_posix(), "prepared_sha256": sha})
            self.manifest["embedding_item_ids"].append(f"new-{i}")
        self.sync()
        self.validator = patch("image_rag_eval.incremental.validate_incremental_prepared", side_effect=self.validate)
        self.validator.start()
        self.addCleanup(self.validator.stop)
        self.addCleanup(self.tmp.cleanup)

    def validate(self, root, run_id):
        self.assertEqual(Path(root), self.root)
        self.assertEqual(run_id, self.run_id)
        return read_json(self.run / "manifest.json"), read_json(self.run / "source-bindings.json")

    def sync(self):
        self.manifest["source_bindings_sha256"] = digest(json_bytes(self.bindings))
        write_json(self.run / "manifest.json", self.manifest)
        write_json(self.run / "source-bindings.json", self.bindings)
        write_json(self.run / "prepared.json", {"complete": True,
            "manifest_sha256": digest(json_bytes(self.manifest)), "source_bindings_sha256": digest(json_bytes(self.bindings))})

    def consent(self):
        result = module.plan_incremental_embedding(self.root, self.run_id)["consent_template"]
        result.update(approved=True, external_ai_approved=True, approved_by="test human")
        return result

    def run_fake(self, client=None, **kwargs):
        return module.execute_incremental_embedding(self.root, self.run_id, self.consent(), maximum_usd=.10,
            client=client or FakeClient(), sleep=lambda _: None, **kwargs)

    def test_dry_run_no_writes_no_provider(self):
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with patch.object(module, "load_credentials", side_effect=AssertionError("dryrun secret access")):
            plan = module.plan_incremental_embedding(self.root, self.run_id)
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(plan["selected_new_images"], 10)
        self.assertEqual(plan["query_calls"], 0)
        self.assertFalse(plan["consent_template"]["approved"])

    def test_one_image_canary_then_eight_standard_batch_and_new_ids_only(self):
        client = FakeClient()
        result = self.run_fake(client)
        self.assertEqual([len(b) for b in client.calls], [1, 8, 1])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["standard_requests_this_invocation"], 3)
        exported = read_json(self.run / "embedding-v1/vectors.json")
        self.assertEqual(set(exported), {"voyage_image"})
        self.assertEqual(set(exported["voyage_image"]), set(self.manifest["embedding_item_ids"]))
        self.assertNotIn("old-id", exported["voyage_image"])
        ledger = read_json(self.run / "embedding-v1/budget.json")
        self.assertEqual(len(ledger["attempts"]), 10)
        self.assertTrue(all(a["status"] == "completed" for a in ledger["attempts"]))
        self.assertEqual(len(list((self.run / "embedding-v1/batch-receipts").glob("*.json"))), 3)

    def test_canary_resume_reuses_completed_keys(self):
        first = FakeClient()
        result = self.run_fake(first, max_new_images=1)
        self.assertEqual(result["status"], "partial")
        second = FakeClient()
        self.run_fake(second)
        self.assertEqual([len(b) for b in second.calls], [8, 1])
        third = FakeClient()
        self.run_fake(third)
        self.assertEqual(third.calls, [])

    def test_uncertain_batch_blocks_retry(self):
        consent = self.consent()
        client = FakeClient(fail_at=2)
        with self.assertRaises(ProviderError):
            module.execute_incremental_embedding(self.root, self.run_id, consent, maximum_usd=.1,
                client=client, sleep=lambda _: None)
        ledger = read_json(self.run / "embedding-v1/budget.json")
        self.assertEqual(len(ledger["attempts"]), 9)
        self.assertEqual(sum(a["status"] == "failed_or_uncertain" for a in ledger["attempts"]), 8)
        blocked = FakeClient()
        with self.assertRaisesRegex(ValueError, "uncertain"):
            module.execute_incremental_embedding(self.root, self.run_id, consent, maximum_usd=.1,
                client=blocked, sleep=lambda _: None)
        self.assertEqual(blocked.calls, [])

    def test_batch_receipt_recovers_missing_cache_without_resend(self):
        consent = self.consent()
        self.run_fake(max_new_images=1)
        cache = next((self.run / "embedding-v1/vector-cache").glob("*.json"))
        cache.unlink()
        ledger = read_json(self.run / "embedding-v1/budget.json")
        ledger["attempts"][0]["status"] = "reserved"
        write_json(self.run / "embedding-v1/budget.json", ledger)
        client = FakeClient()
        module.execute_incremental_embedding(self.root, self.run_id, consent, maximum_usd=.1,
            client=client, max_new_images=1, sleep=lambda _: None)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(client.calls[0]), 1)
        self.assertTrue(cache.exists())
        self.assertEqual(len(read_json(self.run / "embedding-v1/vectors.json")["voyage_image"]), 2)

    def test_parent_pinned_cache_reused_and_duplicate_ids_not_embedded(self):
        self.manifest["embedding_item_ids"] = ["new-0", "new-1"]
        self.sync()
        data = module._load(self.root, self.run_id)
        request = data["requests"]["new-0"]
        parent = run_path(self.root, "old200") / "comparison-v1"
        (parent / "vector-cache").mkdir(parents=True)
        cache = parent / "vector-cache" / (request["key"] + ".json")
        write_json(cache, {"key": request["key"], "provider": "voyage", "model": module.VOYAGE_MODEL,
                          "vector": VECTOR, "vector_sha256": digest(json_bytes(VECTOR))})
        budget = parent / "budget.json"
        write_json(budget, {"attempts": [{"key": request["key"], "status": "completed"}]})
        self.bindings["files"] += [{"path": p.relative_to(self.root).as_posix(), "sha256": digest(p.read_bytes())}
                                   for p in (cache, budget)]
        self.sync()
        client = FakeClient()
        result = self.run_fake(client)
        self.assertEqual(result["new_images_this_invocation"], 1)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(set(read_json(self.run / "embedding-v1/vectors.json")["voyage_image"]), {"new-0", "new-1"})

    def test_consent_identity_and_budget_fail_before_calls(self):
        client = FakeClient()
        consent = self.consent()
        for field, value in [("approved", False), ("provider", "gemini"), ("manifest_sha256", "0" * 64),
                             ("request_keys_sha256", "0" * 64), ("external_ai_approved", False)]:
            changed = {**consent, field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                module.execute_incremental_embedding(self.root, self.run_id, changed, maximum_usd=.1, client=client)
        with self.assertRaisesRegex(ValueError, "budget"):
            module.execute_incremental_embedding(self.root, self.run_id, consent, maximum_usd=.000001, client=client)
        self.assertEqual(client.calls, [])
        self.assertFalse((self.run / "embedding-v1").exists())

    def test_manifest_binding_and_original_tamper_fail_closed(self):
        original = self.root / self.manifest["items"][0]["path"]
        original.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "original"):
            module.plan_incremental_embedding(self.root, self.run_id)

    def test_bound_source_and_prepared_tamper_fail_closed(self):
        evidence = self.root / "evidence.json"
        write_json(evidence, {"a": 1})
        self.bindings["files"] = [{"path": "evidence.json", "sha256": digest(evidence.read_bytes())}]
        self.sync()
        write_json(evidence, {"a": 2})
        with self.assertRaisesRegex(ValueError, "bound source"):
            module.plan_incremental_embedding(self.root, self.run_id)
        self.bindings["files"] = []
        self.sync()
        prepared = self.run / self.manifest["items"][0]["prepared_path"]
        prepared.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "prepared"):
            module.plan_incremental_embedding(self.root, self.run_id)

    def test_reference_id_or_noncase_and_invalid_new_ids_rejected(self):
        for field, value in [("id", "old-id"), ("lane", "external"), ("style_id", "API-001")]:
            previous = self.manifest["items"][0][field]
            self.manifest["items"][0][field] = value
            self.sync()
            with self.subTest(field=field), self.assertRaises(ValueError):
                module.plan_incremental_embedding(self.root, self.run_id)
            self.manifest["items"][0][field] = previous
        self.manifest["embedding_item_ids"] = ["old-id"]
        self.sync()
        with self.assertRaises(ValueError):
            module.plan_incremental_embedding(self.root, self.run_id)

    def test_limits_and_oversized_cap(self):
        for options in [{"maximum_usd": .11}, {"maximum_usd": float("nan")}, {"max_new_images": 301},
                        {"batch_size": 9}, {"batch_size": True}, {"interval": 3.0}]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                module.plan_incremental_embedding(self.root, self.run_id, **options)

    def test_provider_index_model_vector_contract(self):
        result, usage = module.parse_batch_response(response(2), 2)
        self.assertEqual(result, [VECTOR, VECTOR])
        self.assertNotIn("secret", usage)
        invalid = []
        base = response(2)
        for key, value in [("model", "wrong"), ("data", [])]:
            invalid.append({**base, key: value})
        for index in [0, True, -1, 2, None]:
            obj = copy.deepcopy(base)
            obj["data"][0]["index"] = index
            invalid.append(obj)
        for vector in [[0.] * 1024, [1.], [float("nan")] * 1024, [True] * 1024]:
            obj = copy.deepcopy(base)
            obj["data"][0]["embedding"] = vector
            invalid.append(obj)
        for obj in invalid:
            with self.subTest(obj=str(obj)[:50]), self.assertRaises(ProviderError):
                module.parse_batch_response(obj, 2)

    def test_standard_request_payload_is_images_only(self):
        client = module.VoyageImageBatchClient("fake-secret")
        with patch.object(module, "_request_json", return_value=response(2)) as mocked:
            client.embed_images([b"one", b"two"])
        args = mocked.call_args.kwargs
        self.assertEqual(args["url"], "https://api.voyageai.com/v1/multimodalembeddings")
        self.assertEqual(args["payload"]["input_type"], "document")
        self.assertEqual(len(args["payload"]["inputs"]), 2)
        self.assertTrue(all([c["type"] for c in item["content"]] == ["image_base64"] for item in args["payload"]["inputs"]))
        self.assertNotIn("fake-secret", json.dumps(args["payload"]))

    def retry_fixture(self):
        consent = self.consent()
        with self.assertRaises(ProviderError):
            module.execute_incremental_embedding(self.root, self.run_id, consent, maximum_usd=.1,
                client=FakeClient(fail_at=1), max_new_images=1, sleep=lambda _: None)
        path = self.run / "embedding-v1/budget.json"
        raw = path.read_bytes()
        ledger = read_json(path)
        evidence = self.root / "network-diagnostic.json"
        write_json(evidence, {"synthetic": True, "tls_check": "fake-success-no-network"})
        retry = {"schema_version": module.RETRY_SCHEMA, "approved": True, "approved_by": "fixture investigation",
            "run_id": self.run_id, "manifest_sha256": ledger["manifest_sha256"],
            "source_bindings_sha256": ledger["source_bindings_sha256"], "failed_ledger_sha256": digest(raw),
            "failed_request_key": ledger["attempts"][0]["key"], "max_retry_count": 1, "max_retry_images": 1,
            "max_cost_usd": .1, "investigation_evidence_path": "network-diagnostic.json",
            "investigation_evidence_sha256": digest(evidence.read_bytes()),
            "reason_code": "manual_network_permission_investigated", "charge_both_attempts": True}
        return consent, retry, raw, ledger["attempts"][0]

    def test_manual_retry_preserves_failed_attempt_double_charges_and_resumes(self):
        consent, retry, raw, failed = self.retry_fixture()
        client = FakeClient()
        plan = module.plan_incremental_embedding(self.root, self.run_id, max_new_images=1, retry_consent=retry)
        self.assertAlmostEqual(plan["reserved_upper_bound_usd"], 2 * failed["reserved_usd"])
        result = module.execute_incremental_embedding(self.root, self.run_id, consent, maximum_usd=.1,
            max_new_images=1, retry_consent=retry, client=client, sleep=lambda _: None)
        self.assertEqual([len(b) for b in client.calls], [1])
        self.assertEqual(result["manual_retry_authorizations"], 1)
        ledger = read_json(self.run / "embedding-v1/budget.json")
        self.assertEqual(ledger["attempts"][0], failed)
        self.assertEqual(ledger["attempts"][1]["key"], failed["key"] + ":manual-retry-1")
        self.assertEqual(ledger["attempts"][1]["status"], "completed")
        archive = self.root / ledger["retry_authorizations"][0]["failed_ledger_archive_path"]
        self.assertEqual(archive.read_bytes(), raw)
        with self.assertRaisesRegex(ValueError, "already completed"):
            module.execute_incremental_embedding(self.root, self.run_id, consent, maximum_usd=.1,
                max_new_images=1, retry_consent=retry, client=FakeClient())
        ordinary = FakeClient()
        done = module.execute_incremental_embedding(self.root, self.run_id, consent, maximum_usd=.1,
            client=ordinary, sleep=lambda _: None)
        self.assertEqual(done["status"], "completed")
        self.assertEqual([len(b) for b in ordinary.calls], [8, 1])
        self.assertEqual(read_json(self.run / "embedding-v1/budget.json")["attempts"][0], failed)

    def test_manual_retry_failed_again_cannot_retry_or_resume(self):
        consent, retry, _, failed = self.retry_fixture()
        with self.assertRaises(ProviderError):
            module.execute_incremental_embedding(self.root, self.run_id, consent, maximum_usd=.1,
                max_new_images=1, retry_consent=retry, client=FakeClient(fail_at=1), sleep=lambda _: None)
        second = FakeClient()
        with self.assertRaisesRegex(ValueError, "only one"):
            module.execute_incremental_embedding(self.root, self.run_id, consent, maximum_usd=.1,
                max_new_images=1, retry_consent=retry, client=second)
        with self.assertRaisesRegex(ValueError, "uncertain"):
            module.execute_incremental_embedding(self.root, self.run_id, consent, maximum_usd=.1, client=second)
        self.assertEqual(second.calls, [])
        self.assertEqual(read_json(self.run / "embedding-v1/budget.json")["attempts"][0], failed)

    def test_manual_retry_binding_evidence_ack_and_cap_fail_closed(self):
        consent, retry, _, failed = self.retry_fixture()
        client = FakeClient()
        for field, value in [("failed_ledger_sha256", "0" * 64), ("approved", False),
                             ("investigation_evidence_sha256", "0" * 64), ("charge_both_attempts", False),
                             ("max_retry_count", 2), ("max_retry_images", 2),
                             ("failed_request_key", "unknown"), ("manifest_sha256", "0" * 64)]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                module.execute_incremental_embedding(self.root, self.run_id, consent, maximum_usd=.1,
                    max_new_images=1, retry_consent={**retry, field: value}, client=client)
        with self.assertRaisesRegex(ValueError, "budget"):
            module.execute_incremental_embedding(self.root, self.run_id, consent,
                maximum_usd=failed["reserved_usd"] * 1.5, max_new_images=1, retry_consent=retry, client=client)
        with self.assertRaisesRegex(ValueError, "max-new-images"):
            module.execute_incremental_embedding(self.root, self.run_id, consent,
                maximum_usd=.1, retry_consent=retry, client=client)
        self.assertEqual(client.calls, [])
        self.assertFalse((self.run / "embedding-v1/manual-retry-evidence").exists())

    def test_manual_retry_history_and_orphan_cache_tamper_rejected(self):
        consent, retry, _, _ = self.retry_fixture()
        module.execute_incremental_embedding(self.root, self.run_id, consent, maximum_usd=.1,
            max_new_images=1, retry_consent=retry, client=FakeClient(), sleep=lambda _: None)
        path = self.run / "embedding-v1/budget.json"
        ledger = read_json(path)
        ledger["attempts"][0]["status"] = "completed"
        write_json(path, ledger)
        with self.assertRaisesRegex(ValueError, "history"):
            module.plan_incremental_embedding(self.root, self.run_id)

    def test_unrelated_local_cache_is_rejected(self):
        self.run_fake(max_new_images=1)
        write_json(self.run / "embedding-v1/vector-cache/unrelated.json", {})
        with self.assertRaisesRegex(ValueError, "unrelated"):
            module.plan_incremental_embedding(self.root, self.run_id)


class PreparedContractIntegrationTests(unittest.TestCase):
    def test_real_preparation_validator_and_executor_share_contract(self):
        # Use preparation's actual fixture/validator, not the isolated-unit-test stub.
        from test_image_rag_incremental import IncrementalPreparationTests
        fixture = IncrementalPreparationTests()
        fixture.setUp()
        try:
            fixture.prepare(apply=True)
            plan = module.plan_incremental_embedding(fixture.root, fixture.new_id)
            self.assertEqual(plan["selected_new_images"], 1)
            consent = plan["consent_template"]
            consent.update(approved=True, external_ai_approved=True, approved_by="fixture human")
            client = FakeClient()
            result = module.execute_incremental_embedding(fixture.root, fixture.new_id, consent,
                maximum_usd=.1, client=client, sleep=lambda _: None)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["completed_image_ids"], 1)
            self.assertEqual(len(client.calls), 1)
            vectors = read_json(run_path(fixture.root, fixture.new_id) / "embedding-v1/vectors.json")
            self.assertEqual(set(vectors["voyage_image"]), {"incoming-3"})
        finally:
            fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
