"""Synthetic ten-image fixtures only; no real model outputs or production DB writes."""
from __future__ import annotations

import copy
import io
import json
import runpy
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

QA = Path(__file__).resolve().parent
sys.path.insert(0, str(QA))
sys.path.insert(0, str(QA.parent / "src"))

import test_image_luna_analysis_import as fixtures
from image_rag_eval import luna_analysis_view as module
from image_rag_eval.admin_store import AdminStore
from image_rag_eval.approval_handoff import _committed


class LunaAnalysisViewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LunaImportTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.run, self.directory = self.fixture.root, self.fixture.run, self.fixture.directory
        # A separate private fixture DB, never the real runtime DB.
        self.db = self.root / "data/private-research/image-rag-admin/view-fixture.sqlite3"
        self.fixture.db = self.db
        self.fixture.store = AdminStore(self.db, self.fixture.spec, self.fixture.seed)
        self.fixture.manifest["source_commit"] = _committed(self.db, self.fixture.source_run)["commit"]
        self.fixture.persist_manifest()

    def import_candidates(self):
        return self.fixture.run_import(apply=True)

    def build(self, **kwargs):
        return module.build_luna_analysis_review(self.root, self.run, db_path=self.db, **kwargs)

    def files(self):
        return {str(path): path.read_bytes() for path in self.directory.rglob("*") if path.is_file()}

    def qa_document(self, **overrides):
        task = self.fixture.manifest["tasks"][0]
        finding = {"task_id": task["task_id"], "style_id": task["style_id"], "field": "prompt_intent.mismatch_candidates",
                   "status": "needs_correction_before_acceptance", "message": "불일치 판단 근거 부족", "disposition": "확정 검색어에서 제외"}
        finding.update(overrides)
        return {"schema_version": "image-luna-agent-qa-1", "analysis_run_id": self.run,
                "qa_kind": "orchestrator_spot_check_not_human_approval", "metadata_human_approved": False,
                "release_eligible": False, "findings": [finding]}

    def write_qa(self, document):
        path = self.directory / "qa-findings.json"
        path.write_bytes(module._encoded(document))
        return path

    def test_raw_complete_results_without_immutable_import_are_not_rendered(self):
        with self.assertRaisesRegex(ValueError, "immutable imported candidates"):
            self.build(apply=True)
        self.assertFalse((self.directory / module.OUTPUT_NAME).exists())
        self.assertFalse((self.directory / "imports").exists())

    def test_dry_run_validates_import_and_writes_nothing(self):
        self.import_candidates()
        files, before = self.files(), _committed(self.db, self.fixture.source_run)
        with patch("urllib.request.urlopen", side_effect=AssertionError("No external requests")):
            result = self.build()
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["candidate_count"], 10)
        self.assertEqual((result["writes"], result["model_calls_by_renderer"], result["embedding_calls"], result["approval_writes"]), (0, 0, 0, 0))
        self.assertFalse(result["metadata_human_approved"])
        self.assertEqual(files, self.files())
        self.assertEqual(before, _committed(self.db, self.fixture.source_run))

    def test_apply_writes_only_read_only_html_and_reuses_unchanged_output(self):
        self.import_candidates()
        files, before = self.files(), _committed(self.db, self.fixture.source_run)
        result = self.build(apply=True)
        page = Path(result["output_path"]).read_text(encoding="utf-8")
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(result["writes"], 1)
        self.assertEqual(page.count("<article "), 10)
        self.assertEqual(page.count("<img "), 10)
        self.assertIn("needs_review", page)
        self.assertIn("Luna 이미지 분석 후보 · 검토용", page)
        self.assertNotIn("Luna 실제 이미지 분석", page)
        self.assertIn("개인 메모는 모델 입력에 포함하지 않았습니다", page)
        self.assertIn("시각 관찰과 별개", page)
        self.assertIn("아직 검색 인덱스에 반영 안 함", page)
        self.assertNotIn("<form", page)
        self.assertNotIn("<script", page)
        self.assertEqual(self.build(apply=True)["status"], "unchanged")
        self.assertEqual(before, _committed(self.db, self.fixture.source_run))
        for name, raw in files.items():
            self.assertEqual(Path(name).read_bytes(), raw)

    def test_counts_show_completed_canary_and_remaining_coverage_separately(self):
        self.import_candidates()
        data = module._load_review(self.root, self.db, self.run)
        data["manifest"]["approved_library_count"] = 379
        page = module._render_page(data)
        self.assertIn("10/379개 분석 후보 · 나머지 369개 미진행", page)

    def test_every_model_text_section_and_ocr_is_html_escaped(self):
        attack = '<img src=x onerror=alert(1)>'
        result = self.fixture.first_result()
        visual = result["visual"]
        visual["description_ko"] = attack
        for key in ("subjects", "style", "composition", "palette", "copy_space", "uncertainties"):
            visual[key] = [attack]
        for key in ("background", "lighting"):
            visual[key] = attack
        visual["text_visible"] = {"status": "legible", "excerpt": attack, "language_hints": [attack], "limitations": attack}
        for key in result["search_hints"]:
            result["search_hints"][key] = [attack]
        for key in result["prompt_intent"]:
            result["prompt_intent"][key] = attack if key == "summary_ko" else [attack]
        result["reuse_ideas"] = [{key: attack for key in ("use_case", "visual_reason", "adaptation", "caution")}]
        result["limitations"] = [attack]
        self.fixture.update_result(result, draft=True)
        self.import_candidates()
        data = module._load_review(self.root, self.db, self.run)
        data["cards"][0]["full_prompt"] = '</pre><script>alert("source")</script>\n{"문자":"원문"}'
        data["cards"][0]["rights"]["notice_text"] = attack
        page = module._render_page(data)
        self.assertNotIn(attack, page)
        self.assertNotIn('<script>alert', page)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", page)
        self.assertIn("&lt;/pre&gt;&lt;script&gt;", page)
        self.assertIn("원문", page)

    def test_qa_flags_are_prominent_pinned_and_do_not_modify_model_output(self):
        self.import_candidates()
        raw_before = (self.root / self.fixture.manifest["tasks"][0]["raw_result_path"]).read_bytes()
        path = self.write_qa(self.qa_document(message='<script>unsafe QA</script>', disposition='확정 검색어 제외 <img src=x>'))
        result = self.build(apply=True)
        page = Path(result["output_path"]).read_text(encoding="utf-8")
        self.assertEqual(result["agent_qa_findings_count"], 1)
        self.assertEqual(result["qa_findings_sha256"], module._sha(path.read_bytes()))
        self.assertIn("확정 사실·검색 텍스트 사용 보류", page)
        self.assertIn("에이전트 QA · 수정/제외 필요", page)
        self.assertIn("&lt;script&gt;unsafe QA&lt;/script&gt;", page)
        self.assertNotIn("<script>unsafe QA", page)
        self.assertEqual((self.root / self.fixture.manifest["tasks"][0]["raw_result_path"]).read_bytes(), raw_before)

    def test_qa_requires_exact_style_task_and_candidate_status(self):
        self.import_candidates()
        for changes in ({"style_id": "unknown"}, {"task_id": "a" * 64}, {"status": "approved"}, {"field": "rights.release_eligible"}):
            self.write_qa(self.qa_document(**changes))
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "assigned task or field"):
                self.build()

    def test_qa_manifest_cannot_claim_human_approval_or_another_run(self):
        self.import_candidates()
        for key, value in (("metadata_human_approved", True), ("release_eligible", True), ("analysis_run_id", "other"),
                           ("schema_version", "unknown"), ("qa_kind", "human_approved")):
            document = self.qa_document()
            document[key] = value
            self.write_qa(document)
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "QA findings contract"):
                self.build()

    def test_qa_change_during_render_is_rejected_without_output(self):
        self.import_candidates()
        path = self.write_qa(self.qa_document())
        original = module._render_page
        def changed(data):
            rendered = original(data)
            path.write_bytes(module._encoded(self.qa_document(message="사후 바뀐 QA")))
            return rendered
        with patch.object(module, "_render_page", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "QA findings changed"):
                self.build(apply=True)
        self.assertFalse((self.directory / module.OUTPUT_NAME).exists())

    def test_dry_run_rejects_new_commit_created_during_render(self):
        self.import_candidates()
        before = _committed(self.db, self.fixture.source_run)["commit"]
        original = module._render_page
        def changed(data):
            rendered = original(data)
            for index in range(4):
                state = self.fixture.store.state()
                self.fixture.store.advance({"run_id": self.fixture.source_run,
                    "expected_revision": state["revision"], "stage": state["active_stage"],
                    "request_id": f"fixture-render-race-{index}"})
            return rendered
        with patch.object(module, "_render_page", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "latest committed approval"):
                self.build()
        self.assertNotEqual(_committed(self.db, self.fixture.source_run)["commit"], before)
        self.assertFalse((self.directory / module.OUTPUT_NAME).exists())

    def test_missing_rights_or_release_permission_is_rejected(self):
        self.import_candidates()
        for rights in ({}, {task["item_id"]: {"schema_version": "image-rights-notice-1", "release_eligible": True}
                           for task in self.fixture.manifest["tasks"]}):
            with patch.object(module, "_rights_catalog", return_value=rights):
                with self.assertRaisesRegex(ValueError, "rights notice"):
                    self.build()

    def test_changed_imported_payload_and_receipt_fail_closed(self):
        imported = self.import_candidates()
        payload_path = self.root / imported["output_path"] / "validated-results.json"
        original = payload_path.read_bytes()
        payload_path.write_bytes(original + b" ")
        with self.assertRaisesRegex(ValueError, "import changed"):
            self.build()
        payload_path.write_bytes(original)
        receipt = self.root / imported["output_path"] / "receipt.json"
        receipt.write_bytes(receipt.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "import changed"):
            self.build()

    def test_mutated_raw_output_is_not_a_new_review_without_import(self):
        self.import_candidates()
        result = self.fixture.first_result()
        result["prompt_intent"]["summary_ko"] = "changed after import"
        self.fixture.update_result(result)
        with self.assertRaisesRegex(ValueError, "import changed"):
            self.build(apply=True)
        self.assertFalse((self.directory / module.OUTPUT_NAME).exists())

    def test_source_image_and_context_tamper_are_rejected(self):
        self.import_candidates()
        task = self.fixture.manifest["tasks"][0]
        context = self.root / task["prompt_context_path"]
        original = context.read_bytes()
        document = json.loads(original)
        document["full_prompt"] = "tampered context"
        fixtures.write(context, document)
        with self.assertRaisesRegex(ValueError, "context differs"):
            self.build()
        context.write_bytes(original)
        image = self.root / task["prepared_image_path"]
        image.write_bytes(b"changed image")
        with self.assertRaises(ValueError):
            self.build()

    def test_unsafe_run_and_outside_db_are_rejected_before_writing(self):
        for run in ("../outside", "/absolute", "a/b", "a" * 81):
            with self.subTest(run=run), self.assertRaisesRegex(ValueError, "run ID"):
                module.build_luna_analysis_review(self.root, run, db_path=self.db)
        with self.assertRaisesRegex(ValueError, "DB must remain"):
            module.build_luna_analysis_review(self.root, self.run, db_path=self.root.parent / "outside.sqlite3")

    def test_different_existing_html_is_preserved(self):
        self.import_candidates()
        target = self.directory / module.OUTPUT_NAME
        target.write_bytes(b"user-owned existing content")
        with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
            self.build(apply=True)
        self.assertEqual(target.read_bytes(), b"user-owned existing content")

    def test_failed_publish_leaves_no_partial_final_html(self):
        self.import_candidates()
        with patch.object(module.os, "fsync", side_effect=OSError("fixture disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                self.build(apply=True)
        self.assertFalse((self.directory / module.OUTPUT_NAME).exists())
        self.assertEqual(list(self.directory.glob(".luna-review-*")), [])

    def test_all_images_resolve_relative_to_output_and_no_remote_image_requests(self):
        self.import_candidates()
        data = module._load_review(self.root, self.db, self.run)
        for card in data["cards"]:
            relative = card["image_relative"]
            self.assertFalse(relative.startswith(("http:", "https:", "file:")))
            self.assertEqual((self.directory / relative).resolve(), (self.root / card["task"]["prepared_image_path"]).resolve())


class LunaAnalysisViewCLITests(unittest.TestCase):
    def test_cli_dry_run_default_and_explicit_apply(self):
        script = QA.parent / "src/build_image_luna_review.py"
        for extra, apply in (([], False), (["--apply"], True)):
            with patch.object(sys, "argv", [str(script), "--analysis-run-id", "fixture", *extra]), \
                 patch.object(module, "build_luna_analysis_review", return_value={"status": "fixture"}) as builder, \
                 redirect_stdout(io.StringIO()):
                runpy.run_path(str(script), run_name="__main__")
            self.assertEqual(builder.call_args.args[1], "fixture")
            self.assertEqual(builder.call_args.kwargs["apply"], apply)


if __name__ == "__main__":
    unittest.main()
