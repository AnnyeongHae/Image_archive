from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from image_rag_eval import text_embedding_run as runner


class FixtureTokenizer:
    def no_truncation(self):
        pass

    def no_padding(self):
        pass

    def encode(self, text, add_special_tokens=False):
        assert add_special_tokens is False
        return SimpleNamespace(ids=list(text))


def response(count=1, tokens=1):
    return {"model": runner.MODEL, "usage": {"total_tokens": tokens},
            "data": [{"index": index, "embedding": [float(index + 1)] + [0.0] * 511} for index in range(count)]}


class TextEmbeddingRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run_dir = self.root / "data/private-research/test-run"
        self.tokenizer = self.root / "tokenizer.json"
        self.tokenizer.write_text("{}", encoding="utf-8")
        self.calls = []
        self.counter = 0

    def manifest(self, docs=None):
        self.counter += 1
        document = {"schema_version": runner.MANIFEST_SCHEMA, "model": runner.MODEL,
                    "dimension": 512, "total_token_cap": 260000,
                    "documents": docs or [{"input_id": "compact:a", "item_id": "a", "text": "A useful document", "input_type": "document"}]}
        path = self.root / ("manifest-" + str(self.counter) + ".json")
        path.write_bytes(runner._bytes(document))
        return path

    def transport(self, payload, key):
        self.calls.append(copy.deepcopy(payload))
        state = runner._state(self.run_dir, self.root)
        self.assertTrue(state["pending"], "Durable reservation must precede sending")
        self.assertEqual(payload["output_dimension"], 512)
        self.assertEqual(payload["output_dtype"], "float")
        self.assertIs(payload["truncation"], False)
        result = response(len(payload["input"]))
        result["ignored_secret_echo"] = key
        return result

    def run_manifest(self, manifest, *, live=True, **kwargs):
        return runner.execute_manifest(manifest, self.tokenizer, self.run_dir, archive_root=self.root,
            apply=live, execute=live, tokenizer=FixtureTokenizer(), api_key="CANARY_SECRET_DO_NOT_PERSIST",
            transport=kwargs.pop("transport", self.transport), **kwargs)

    def test_dry_run_has_no_writes_no_credentials_and_no_calls(self):
        manifest = self.manifest()
        before = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with mock.patch.object(runner, "_credential", side_effect=AssertionError("credential read")):
            summary = self.run_manifest(manifest, live=False)
        self.assertEqual(summary["status"], "dry_run")
        self.assertEqual(self.calls, [])
        self.assertEqual(before, {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()})

    def test_request_raw_text_and_sanitized_evidence(self):
        manifest = self.manifest()
        summary = self.run_manifest(manifest)
        self.assertEqual(summary["provider_calls_this_invocation"], 1)
        self.assertEqual(self.calls[0]["input"], ["A useful document"])
        for path in self.run_dir.rglob("*.json"):
            self.assertNotIn("CANARY_SECRET_DO_NOT_PERSIST", path.read_text(encoding="utf-8"))
        vectors = runner.load_manifest_vectors(self.run_dir, summary["manifest_sha256"], archive_root=self.root)
        self.assertEqual(vectors["vectors"]["compact:a"], [1.0] + [0.0] * 511)
        self.assertGreater(summary["conservative_charged_tokens"], summary["actual_reported_tokens"])

    def test_response_indices_are_validated_and_reordered_safely(self):
        good = response(2)
        good["data"].reverse()
        clean = runner.validate_response(good, 2)
        self.assertEqual([r["index"] for r in clean["data"]], [0, 1])
        self.assertEqual(clean["data"][1]["embedding"][0], 2.0)
        for index in (None, True, -1, 2, 0):
            bad = response(2)
            bad["data"][1]["index"] = index
            with self.subTest(index=index), self.assertRaises(runner.TextRunError):
                runner.validate_response(bad, 2)

    def test_model_absence_is_unknown_and_mismatch_rejected(self):
        good = response()
        del good["model"]
        self.assertIsNone(runner.validate_response(good, 1)["model_reported"])
        self.assertEqual(runner.validate_response(good, 1)["model_reported_status"], "absent_unknown")
        for model in (None, "voyage-multimodal-3.5", "voyage-4"):
            good["model"] = model
            with self.assertRaises(runner.TextRunError):
                runner.validate_response(good, 1)

    def test_response_shapes_usage_and_vectors_fail_closed(self):
        bad_responses = [None, [], {}, {"data": None}, response(2)]
        for tokens in (None, True, -1, 0, 2.3, "1"):
            bad_responses.append(response(tokens=tokens))
        for vector in (None, [], [0.0] * 512, [float("nan")] * 512, [float("inf")] * 512, [True] * 512, ["1"] * 512):
            bad = response()
            bad["data"][0]["embedding"] = vector
            bad_responses.append(bad)
        bad = response()
        bad["truncated"] = True
        bad_responses.append(bad)
        for value in bad_responses:
            with self.subTest(value=str(value)[:80]), self.assertRaises(runner.TextRunError):
                runner.validate_response(value, 1)

    def test_same_and_changed_manifest_reuse_content_without_new_reservation(self):
        first = self.run_manifest(self.manifest())
        second_manifest = self.manifest([{"input_id": "full:b", "item_id": "b", "text": "A useful document", "input_type": "document"}])
        second = self.run_manifest(second_manifest)
        self.assertEqual(second["provider_calls_this_invocation"], 0)
        self.assertEqual(second["cache_hits"], 1)
        self.assertEqual(first["conservative_charged_tokens"], second["conservative_charged_tokens"])
        self.assertEqual(len(self.calls), 1)

    def test_text_and_task_drift_require_different_cache_identities(self):
        self.run_manifest(self.manifest())
        changed = self.manifest([{"input_id": "a", "text": "A useful document", "input_type": "query"},
                                 {"input_id": "b", "text": "Changed document", "input_type": "document"}])
        summary = self.run_manifest(changed)
        self.assertEqual(summary["provider_calls_this_invocation"], 2)
        self.assertEqual(summary["missing_inputs"], 0)

    def test_cache_tamper_fails_without_retry(self):
        manifest = self.manifest()
        self.run_manifest(manifest)
        path = next((self.run_dir / "cache").glob("*.json"))
        content = json.loads(path.read_bytes())
        content["vector"][0] = 7
        path.write_bytes(runner._bytes(content))
        with self.assertRaisesRegex(runner.TextRunError, "integrity"):
            self.run_manifest(manifest)
        self.assertEqual(len(self.calls), 1)

    def test_response_tamper_and_missing_completed_cache_fail_closed(self):
        manifest = self.manifest()
        self.run_manifest(manifest)
        path = next((self.run_dir / "responses").glob("*.json"))
        path.write_bytes(b"{}")
        with self.assertRaisesRegex(runner.TextRunError, "integrity"):
            self.run_manifest(manifest)
        next((self.run_dir / "cache").glob("*.json")).unlink()
        with self.assertRaisesRegex(runner.TextRunError, "completed_cache_missing"):
            self.run_manifest(manifest)

    def test_transport_uncertainty_never_retried_or_released_across_manifests(self):
        def fail(payload, key):
            self.calls.append(payload)
            raise RuntimeError(key)
        with self.assertRaisesRegex(runner.TextRunError, "uncertain"):
            self.run_manifest(self.manifest(), transport=fail)
        state = runner._state(self.run_dir, self.root)
        self.assertGreater(state["charged_tokens"], 0)
        self.assertEqual(state["actual_tokens"], 0)
        self.assertEqual(len(state["pending"]), 1)
        for path in self.run_dir.rglob("*.json"):
            self.assertNotIn("CANARY_SECRET_DO_NOT_PERSIST", path.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(runner.TextRunError, "uncertain"):
            self.run_manifest(self.manifest())
        self.assertEqual(len(self.calls), 1)

    def test_invalid_response_leaves_uncertain_reservation(self):
        with self.assertRaisesRegex(runner.TextRunError, "uncertain"):
            self.run_manifest(self.manifest(), transport=lambda *_: {"data": None})
        self.assertEqual(len(runner._state(self.run_dir, self.root)["uncertain"]), 1)

    def test_usage_above_reservation_reconciles_and_halts_future_batches(self):
        documents = [{"input_id": str(i), "text": "small " + str(i), "input_type": "document"} for i in range(2)]
        def over(payload, _):
            self.calls.append(payload)
            return response(tokens=1000)
        with self.assertRaisesRegex(runner.TextRunError, "exceeded_reservation"):
            self.run_manifest(self.manifest(documents), batch_size=1, transport=over)
        state = runner._state(self.run_dir, self.root)
        self.assertEqual(state["actual_tokens"], 1000)
        self.assertEqual(state["charged_tokens"], 1000)
        self.assertFalse(state["pending"])
        self.assertEqual(len(self.calls), 1)
        with self.assertRaisesRegex(runner.TextRunError, "exceeded_reservation"):
            self.run_manifest(self.manifest())

    def test_global_budget_spans_canary_and_full_manifests(self):
        first = [{"input_id": str(i), "text": "x" * 10000 + str(i), "input_type": "document"} for i in range(20)]
        summary = self.run_manifest(self.manifest(first))
        self.assertGreater(summary["conservative_charged_tokens"], 200000)
        later = [{"input_id": str(i), "text": "y" * 10000 + str(i), "input_type": "document"} for i in range(6)]
        with self.assertRaisesRegex(runner.TextRunError, "global_token_budget"):
            self.run_manifest(self.manifest(later))
        self.assertEqual(len(self.calls), 1)

    def test_lock_blocks_execution_and_preserves_owner_lock(self):
        manifest = self.manifest()
        self.run_manifest(manifest)
        with runner._lock(self.run_dir, self.root):
            with self.assertRaisesRegex(runner.TextRunError, "run_locked"):
                self.run_manifest(manifest)
            self.assertTrue((self.run_dir / ".execute.lock").exists())
        self.assertEqual(len(self.calls), 1)

    def test_private_paths_symlinks_and_execution_flags(self):
        manifest = self.manifest()
        with self.assertRaisesRegex(runner.TextRunError, "private_path"):
            runner.execute_manifest(manifest, self.tokenizer, self.root / "dist", archive_root=self.root)
        with mock.patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaisesRegex(runner.TextRunError, "symlink"):
                self.run_manifest(manifest)
        with self.assertRaisesRegex(runner.TextRunError, "required_together"):
            runner.execute_manifest(manifest, self.tokenizer, self.run_dir, archive_root=self.root, execute=True)
        self.assertEqual(runner._private(self.run_dir / "sub/../file", self.root), self.run_dir / "file")

    def test_manifest_drift_truncation_and_tokenizer_pin_rejected(self):
        manifest = self.manifest()
        document = json.loads(manifest.read_bytes())
        for field, value in (("total_token_cap", 260001), ("dimension", True), ("model", "voyage-4"), ("truncation", True)):
            mutated = {**document, field: value}
            manifest.write_bytes(runner._bytes(mutated))
            with self.assertRaises(runner.TextRunError):
                self.run_manifest(manifest)
        manifest.write_bytes(runner._bytes(document))
        with self.assertRaisesRegex(runner.TextRunError, "unpinned_tokenizer"):
            runner.execute_manifest(manifest, self.tokenizer, self.run_dir, archive_root=self.root)

    def test_http_status_retry_after_only_no_body_or_header_leak(self):
        headers = Message()
        headers["Retry-After"] = "37"
        headers["secret-header"] = "SECRET"
        error = HTTPError(runner.URL, 429, "SECRET RESPONSE", headers, None)
        opener = mock.Mock()
        opener.open.side_effect = error
        with mock.patch.object(runner, "build_opener", return_value=opener):
            with self.assertRaises(runner.TextRunError) as failure:
                runner._post({"input": ["text"]}, "SECRET KEY")
        self.assertEqual(failure.exception.http_status, 429)
        self.assertEqual(failure.exception.retry_after_seconds, 37)
        self.assertNotIn("SECRET", str(failure.exception))
        with self.assertRaises(runner.TextRunError):
            self.run_manifest(self.manifest(), transport=lambda *_: (_ for _ in ()).throw(failure.exception))
        uncertain = runner._state(self.run_dir, self.root)["events"][-1]
        self.assertEqual(uncertain["http_status"], 429)
        self.assertEqual(uncertain["retry_after_seconds"], 37)
        self.assertIn("observed_at_utc", uncertain)

    def test_credential_parser_only_loads_one_requested_key(self):
        env = self.root / ".env"
        env.write_text('OTHER_SECRET=ignored\nVOYAGE_API_KEY="the-key"\n', encoding="utf-8")
        self.assertEqual(runner._credential(env, self.root), "the-key")
        env.write_text("VOYAGE_API_KEY=one\nVOYAGE_API_KEY=two\n", encoding="utf-8")
        with self.assertRaisesRegex(runner.TextRunError, "ambiguous"):
            runner._credential(env, self.root)

    def test_removed_ledger_tail_does_not_restore_budget(self):
        self.run_manifest(self.manifest())
        for path in (self.run_dir / "events").glob("*.json"):
            path.unlink()
        other = self.manifest([{"input_id": "other", "text": "unrelated input", "input_type": "document"}])
        with self.assertRaisesRegex(runner.TextRunError, "orphan"):
            self.run_manifest(other)
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
