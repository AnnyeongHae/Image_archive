"""Offline preparation regressions; no real image-analysis outputs are fabricated."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import prepare_image_luna_canary as module
import test_image_admin_handoff as fixtures
from image_rag_eval.approval_handoff import _committed


class LunaCanaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.HandoffTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.run = self.fixture.run
        self.analysis = "luna-test"
        self.base = self.root / "data/private-research/image-rag-admin/luna-analysis" / self.analysis
        workspace = Path(__file__).resolve().parents[2]
        for relative in (module.SCHEMA, module.INSTRUCTIONS):
            target = (self.root / relative).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((workspace / relative.removeprefix("../")).read_bytes())

    def prepare(self, **kwargs):
        return module.prepare(self.root, self.run, self.analysis, style_ids=kwargs.pop("style_ids", ("A", "D", "E", "F")), **kwargs)

    def manifest(self):
        return json.loads((self.base / "tasks.json").read_text(encoding="utf-8"))

    def test_dry_run_is_offline_and_writes_no_outputs(self):
        before = _committed(self.fixture.path, self.run)
        with patch("urllib.request.urlopen", side_effect=AssertionError("No network")):
            result = self.prepare()
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual((result["model_inferences"], result["embedding_calls"]), (0, 0))
        self.assertFalse(self.base.exists())
        self.assertEqual(_committed(self.fixture.path, self.run), before)

    def test_approved_images_only_and_original_prompt_kept_separate(self):
        self.prepare(apply=True)
        manifest = self.manifest()
        self.assertEqual([task["style_id"] for task in manifest["tasks"]], list("ADEF"))
        self.assertFalse(manifest["human_memos_in_model_input"])
        self.assertFalse(manifest["embedding_calls_authorized"])
        self.assertNotIn("주관적인 메모", (self.base / "tasks.json").read_text(encoding="utf-8"))
        self.assertNotIn("원본 프롬프트", (self.base / "tasks.json").read_text(encoding="utf-8"))
        context = json.loads((self.base / "contexts/F.json").read_text(encoding="utf-8"))
        self.assertEqual(context["full_prompt"], "원본 프롬프트 F")
        self.assertEqual(list((self.base / "raw-results").iterdir()), [])
        self.assertEqual(list((self.base / "visual-drafts").iterdir()), [])

    def test_identities_are_deterministic_and_do_not_claim_model_execution(self):
        self.prepare(apply=True)
        task = self.manifest()["tasks"][0]
        self.assertEqual(task["input_fingerprint"], module.sha(module.encode(task["identity"])))
        self.assertEqual(task["task_id"], module.sha(module.encode({"input_fingerprint": task["input_fingerprint"], "item_id": task["item_id"]})))
        self.assertEqual(self.prepare(apply=True)["status"], "unchanged")

    def test_unapproved_and_archived_styles_are_rejected_without_writes(self):
        for style in ("B", "C", "G", "UNKNOWN"):
            with self.subTest(style=style), self.assertRaisesRegex(ValueError, "not currently approved"):
                self.prepare(style_ids=(style,), apply=True)
        self.assertFalse(self.base.exists())

    def test_bound_and_unsafe_run_validation(self):
        for styles in ((), ("A", "A"), tuple(str(i) for i in range(21))):
            with self.assertRaises(ValueError):
                self.prepare(style_ids=styles)
        for ident in ("../outside", "/absolute", "a/b", "x" * 81):
            with self.assertRaises(ValueError):
                module.prepare(self.root, self.run, ident, style_ids=("A",), apply=True)

    def test_existing_context_or_task_is_never_overwritten(self):
        self.prepare(apply=True)
        path = self.base / "contexts/A.json"
        path.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "do not overwrite"):
            self.prepare(apply=True)
        self.assertEqual(path.read_text(encoding="utf-8"), "changed")

    def test_expected_latest_commit_is_checked(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.prepare(expected_commit_id="f" * 64, apply=True)
        self.assertFalse(self.base.exists())

    def test_preview_hash_mismatch_rejected(self):
        image = self.fixture.spec["items"][0]
        path = (self.fixture.directory / image["prepared_path"]).resolve()
        path.write_bytes(b"changed preview")
        with self.assertRaises(ValueError):
            self.prepare(apply=True)
        self.assertFalse(self.base.exists())

    def test_contract_change_changes_fingerprint_without_mutating_existing_batch(self):
        self.prepare(apply=True)
        before = self.manifest()["tasks"][0]["input_fingerprint"]
        schema = (self.root / module.SCHEMA).resolve()
        schema.write_bytes(schema.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "do not overwrite"):
            self.prepare(apply=True)
        other = module.prepare(self.root, self.run, "luna-v2", style_ids=("A",), apply=True)
        manifest = json.loads((self.root / other["tasks_path"]).read_text(encoding="utf-8"))
        self.assertNotEqual(before, manifest["tasks"][0]["input_fingerprint"])


if __name__ == "__main__":
    unittest.main()
