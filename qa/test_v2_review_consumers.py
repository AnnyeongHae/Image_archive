"""Offline contracts for source-neutral runs using the existing private UI."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from image_rag_eval.approved_library import build_prompt_catalog
from image_rag_eval.approval_handoff import HandoffError, _sources
from image_rag_eval.experiment import digest, json_bytes
from image_rag_eval.incremental_workflow import load_frozen_workflow


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_bytes(value))


class IntakeConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run_id = "source-neutral-review"
        self.run = self.root / "data/private-research/image-rag-canary/runs" / self.run_id
        self.workflow = self.run / "group-workflow-v1"
        self.prompt = '  {\r\n  "원문": "스티커 🧩",\r\n  "spaces": "  "\r\n}\n'
        preview = b"synthetic-preview-identity-only"
        prepared_sha = digest(preview)
        self.preview = self.run / "inputs" / (prepared_sha + ".png")
        self.preview.parent.mkdir(parents=True)
        self.preview.write_bytes(preview)
        self.spec = {"schema_version": "image-group-workflow-spec-1", "run_id": self.run_id,
            "approval_policy": "default_retained_images_after_review_v1",
            "items": [{"id": "intake-A", "style_id": "INTAKE-A", "source_sha256": "a" * 64,
                       "prepared_sha256": prepared_sha, "prepared_path": "../inputs/" + self.preview.name}]}
        self.spec["spec_sha256"] = digest(json_bytes(self.spec))
        self.manifest_relative = "data/private-research/image-rag-canary/runs/source-neutral-intake/manifest.json"
        self.manifest = {"schema_version": "image-v2-intake-manifest-1", "items": [
            {"id": "intake-A", "style_id": "INTAKE-A", "sha256": "a" * 64,
             "prepared_sha256": prepared_sha, "prompt": self.prompt, "prompt_truncated": False,
             "embedding_prompt": "NOT the full prompt", "source_record": {
                 "source_id": "synthetic-source", "source_version": {"version": "fixture-only"},
                 "rights": {"rights_status": "unknown", "release_eligible": False}},
             "source_name": "Synthetic source", "source_url": "https://example.org/source"}]}
        self.persist()

    def persist(self):
        write_json(self.root / self.manifest_relative, self.manifest)
        self.workflow.mkdir(parents=True, exist_ok=True)
        baseline = b"{}"
        (self.workflow / "submitted-baseline.raw.json").write_bytes(baseline)
        self.binding = {"review_spec_sha256": self.spec["spec_sha256"],
            "source_decisions_sha256": digest(baseline), "files": [
                {"path": self.manifest_relative, "sha256": digest((self.root / self.manifest_relative).read_bytes())}]}
        write_json(self.workflow / "image-group-workflow.spec.json", self.spec)
        write_json(self.workflow / "source-bindings.json", self.binding)
        write_json(self.workflow / "build-receipt.json", {"schema_version": "image-v2-intake-review-1",
            "status": "ready", "run_id": self.run_id, "spec_sha256": self.spec["spec_sha256"],
            "binding_sha256": digest(json_bytes(self.binding))})

    def test_v2_receipt_dispatches_only_to_strict_source_neutral_loader(self):
        module = types.ModuleType("image_rag_eval.intake_review")
        module.load_intake_review = Mock(return_value=self.spec)
        with patch.dict(sys.modules, {module.__name__: module}):
            self.assertEqual(load_frozen_workflow(self.root, self.run_id), self.spec)
        module.load_intake_review.assert_called_once_with(self.root, self.run_id)

    def test_v2_readiness_failure_cannot_fall_back_to_legacy_loader(self):
        module = types.ModuleType("image_rag_eval.intake_review")
        module.load_intake_review = Mock(side_effect=ValueError("image vectors missing"))
        with patch.dict(sys.modules, {module.__name__: module}), self.assertRaisesRegex(ValueError, "vectors missing"):
            load_frozen_workflow(self.root, self.run_id)

    def test_unknown_review_schema_does_not_gain_source_neutral_dispatch(self):
        receipt = json.loads((self.workflow / "build-receipt.json").read_text(encoding="utf-8"))
        receipt["schema_version"] = "image-v2-intake-review-unknown"
        write_json(self.workflow / "build-receipt.json", receipt)
        with self.assertRaisesRegex(ValueError, "frozen workflow identity"):
            load_frozen_workflow(self.root, self.run_id)

    def test_exact_prompt_is_loaded_from_bound_v2_manifest_without_canonical_rewrite(self):
        before = copy.deepcopy(self.manifest)
        prompt = build_prompt_catalog(self.root, self.spec)["intake-A"]
        self.assertEqual(prompt["status"], "available")
        self.assertEqual(prompt["full_prompt"], self.prompt)
        self.assertEqual(prompt["prompt_sha256"], digest(self.prompt.encode("utf-8")))
        self.assertEqual(prompt["source_binding"]["origin"], "pinned_manifest.prompt")
        self.assertFalse(prompt["release_eligible"])
        self.assertEqual(self.manifest, before)
        self.assertFalse((self.root / "data/canonical").exists())

    def test_handoff_resolves_v2_manifest_with_complete_source_record(self):
        sources, evidence = _sources(self.root, self.spec)
        source = sources["intake-A"]
        self.assertEqual(source["prompt"], self.prompt)
        self.assertEqual(source["source_record"], self.manifest["items"][0]["source_record"])
        self.assertFalse(source["source_record"]["rights"]["release_eligible"])
        self.assertEqual(source["frozen_preview_path"], self.preview.relative_to(self.root).as_posix())
        self.assertIn(self.manifest_relative, {row["path"] for row in evidence})

    def test_prompt_and_handoff_reject_v2_image_identity_mismatch(self):
        self.manifest["items"][0]["sha256"] = "c" * 64
        self.persist()
        with self.assertRaisesRegex(ValueError, "image identity"):
            build_prompt_catalog(self.root, self.spec)
        with self.assertRaisesRegex(HandoffError, "identity"):
            _sources(self.root, self.spec)

    def test_prompt_and_handoff_reject_v2_source_byte_drift(self):
        (self.root / self.manifest_relative).write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "manifest changed"):
            build_prompt_catalog(self.root, self.spec)
        with self.assertRaisesRegex(HandoffError, "source evidence changed"):
            _sources(self.root, self.spec)


class AdminLauncherConsumerTests(unittest.TestCase):
    def test_source_guard_is_wired_to_store_and_http_adapter(self):
        import serve_image_admin
        spec = {"run_id": "v2-launcher-fixture"}
        server, store = Mock(), Mock()
        server.origin = "http://127.0.0.1:8964"
        store.state.return_value = {"status": "saved", "last_commit": None}
        with patch.object(sys, "argv", ["serve_image_admin.py", "--run-id", spec["run_id"], "--serve"]), \
             patch.object(serve_image_admin, "load_frozen_workflow", return_value=spec) as loader, \
             patch.object(serve_image_admin, "media_map", return_value={}), \
             patch.object(serve_image_admin, "AdminHTTPServer", return_value=server) as http_factory, \
             patch("image_rag_eval.admin_store.AdminStore", return_value=store) as store_factory, \
             patch("image_rag_eval.rights.build_rights_catalog", return_value={}), \
             patch("image_rag_eval.approved_library.build_prompt_catalog", return_value={}), \
             patch("builtins.print"):
            serve_image_admin.main()
            http_guard = http_factory.call_args.kwargs["validate_source"]
            store_guard = store_factory.call_args.kwargs["validate_source"]
            self.assertTrue(callable(http_guard))
            self.assertTrue(callable(store_guard))
            self.assertEqual(http_guard(), spec)
            self.assertEqual(store_guard(), spec)
            self.assertEqual(loader.call_count, 3)
            self.assertIsNone(store_factory.call_args.kwargs["seed_decisions"])
            server.server_close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
