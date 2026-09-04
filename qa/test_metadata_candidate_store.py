"""Private candidate projection: offline fixture-only regression tests."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from image_rag_eval.luna_analysis_import import digest, encode
from image_rag_eval.metadata_candidate_store import (
    MIGRATION, CandidateStoreError, _populate, diagnostic_search, project_snapshot,
)


def fixture():
    taxonomy = {"schema_version": "fixture-1", "families": [{"use_cases": [
        {"id": "character.reaction_pack", "label_ko": "감정 반응 세트"}]}],
        "asset_formats": [{"id": "thumbnail", "label_ko": "썸네일"}],
        "aliases": [{"facet": "use_case", "target_id": "character.reaction_pack", "term": "reaction pack"},
                    {"facet": "use_case", "target_id": "character.reaction_pack", "term": "이모티콘 세트"}]}
    taxonomy_raw = encode(taxonomy).decode()
    sha = digest(taxonomy_raw.encode())
    manifest = {"analysis_run_id": "fixture-run", "source_run_id": "approved-run", "source_commit": {"id": "commit-1"},
                "schema_version": "tasks-fixture", "model_family": "gpt-5.6-luna", "taxonomy_sha256": sha}
    token = {"analysis_run_id": "fixture-run", "task_manifest_sha256": "a" * 64, "completed_image_count": 10,
             "evidence_status": "observed_local_codex_log", "actual_billed_tokens": None, "actual_billed_cost": None,
             "usage": {"input_tokens_including_cached": 100, "cached_input_tokens": 60, "uncached_input_tokens_calculated": 40,
                       "output_tokens_including_reasoning": 20, "reasoning_output_tokens": 5, "total_tokens": 120}}
    cards = []
    for number in range(10):
        ident = f"item-{number}"
        prompt = "같은 원문 프롬프트"  # Deduplicated text, image-dependent interpretations stay separate.
        task = {"item_id": ident, "style_id": f"CASE-{number:03}", "task_id": digest(ident.encode()),
                "source_image_sha256": digest((ident + "source").encode()), "prepared_image_sha256": digest(ident.encode()),
                "prepared_image_path": f"private/{ident}.png", "prompt_sha256": digest(prompt.encode()), "input_fingerprint": "b" * 64}
        assignment = {"use_case_id": "character.reaction_pack", "fit": "supported", "reuse_mode": "layout_reference",
                      "evidence_basis": "image", "why_usable_ko": "표정이 다양함"}
        result = {"schema_version": "fixture-result-2", "review_status": "needs_review", "metadata_human_approved": False,
                  "release_eligible": False, "visual": {"description_ko": "교육자료 감정 표현", "styles": ["일러스트"]},
                  "prompt_analysis": {"mismatch_candidates": [f"그림별 차이 {number}"]},
                  "usage_selection": {"primary": assignment, "secondary": []}}
        cards.append({"task": task, "result": result, "raw_result_json": json.dumps(result, ensure_ascii=False, indent=2),
                      "rights": {"release_eligible": False}, "full_prompt": prompt, "qa_findings": [],
                      "group": {"group_id": None, "representative_id": None, "member_count": 1, "selected_is_representative": True}})
    return {"taxonomy": taxonomy, "taxonomy_raw_json": taxonomy_raw, "taxonomy_sha256": sha,
            "taxonomy_relative_path": "Reports/fixture.json", "evidence": [], "runs": [
                {"manifest": manifest, "cards": cards, "task_manifest_sha256": "a" * 64,
                 "validated_results_sha256": "c" * 64, "import_receipt_sha256": "d" * 64,
                 "token": token, "token_raw_json": encode(token).decode(), "execution_raw_json": "{}"}]}


class CandidateStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / "private/candidates.sqlite3"
        self.migration = ROOT / MIGRATION

    def build(self, bundle=None, apply=True):
        return project_snapshot(bundle or fixture(), self.output, self.migration, apply=apply)

    def connect(self):
        db = sqlite3.connect(self.output)
        self.addCleanup(db.close)
        return db

    def test_dry_run_writes_nothing_and_idempotent_apply(self):
        self.assertEqual(self.build(apply=False)["status"], "dry_run")
        self.assertFalse(self.output.parent.exists())
        first = self.build()
        second = self.build()
        self.assertEqual(first["status"], "prepared")
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(first["database_sha256"], second["database_sha256"])
        self.assertEqual(second["counts"]["candidates"], 10)
        self.assertEqual(second["counts"]["prompts"], 1)

    def test_raw_json_and_image_dependent_prompt_blocks_preserved(self):
        bundle = fixture()
        self.build(bundle)
        db = self.connect()
        original = bundle["runs"][0]["cards"][0]["raw_result_json"]
        stored = db.execute("SELECT raw_json,prompt_analysis_json FROM candidates JOIN items USING(item_id) WHERE style_id='CASE-000'").fetchone()
        self.assertEqual(stored[0], original)
        self.assertEqual(json.loads(stored[1]), {"mismatch_candidates": ["그림별 차이 0"]})
        self.assertEqual(db.execute("SELECT count(DISTINCT prompt_analysis_json) FROM candidates").fetchone()[0], 10)

    def test_rollback_when_late_record_invalid(self):
        bundle = fixture()
        bundle["runs"][0]["cards"][-1]["result"]["release_eligible"] = True
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.executescript(self.migration.read_text(encoding="utf-8"))
        with self.assertRaises(CandidateStoreError):
            _populate(db, bundle, "a" * 64)
        self.assertEqual(db.execute("SELECT count(*) FROM candidates").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT count(*) FROM snapshot").fetchone()[0], 0)

    def test_changed_snapshot_does_not_overwrite(self):
        self.build()
        before = self.output.read_bytes()
        bundle = fixture()
        bundle["evidence"].append({"new": "evidence"})
        with self.assertRaises(CandidateStoreError):
            self.build(bundle)
        self.assertEqual(before, self.output.read_bytes())

    def test_bilingual_aliases_and_korean_substring_are_diagnostic(self):
        self.build()
        db = self.connect()
        for query in ("reaction pack", "이모티콘 세트", "교육"):
            results = diagnostic_search(db, query)
            self.assertEqual(len(results), 5)
            self.assertFalse(results[0]["public_search_eligible"])

    def test_no_public_eligibility_and_sql_injection(self):
        self.build()
        db = self.connect()
        self.assertEqual(db.execute("SELECT count(*) FROM public_search_candidates").fetchone()[0], 0)
        self.assertEqual(diagnostic_search(db, "\" OR 1=1; DROP TABLE candidates; --"), [])
        self.assertEqual(diagnostic_search(db, "%"), [])
        self.assertEqual(db.execute("SELECT count(*) FROM candidates").fetchone()[0], 10)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE candidates SET public_search_eligible=1")

    def test_v1_freeform_usage_not_inferred(self):
        bundle = fixture()
        for card in bundle["runs"][0]["cards"]:
            card["result"].pop("usage_selection")
            card["result"]["reuse_ideas"] = [{"use_case": "character.reaction_pack", "visual_reason": "freeform"}]
            card["raw_result_json"] = json.dumps(card["result"], ensure_ascii=False)
        self.assertEqual(self.build(bundle)["counts"]["usage_assignments"], 0)
        self.assertIsNotNone(self.connect().execute("SELECT freeform_usage_json FROM candidates").fetchone()[0])

    def test_qa_flagged_block_omitted_from_lexical_but_raw_preserved(self):
        bundle = fixture()
        for card in bundle["runs"][0]["cards"]:
            card["qa_findings"] = [{"field": "visual.description_ko", "status": "needs_review"}]
        self.build(bundle)
        db = self.connect()
        self.assertEqual(diagnostic_search(db, "교육자료"), [])
        self.assertIn("교육자료", db.execute("SELECT raw_json FROM candidates").fetchone()[0])
        self.assertEqual(db.execute("SELECT count(*) FROM candidate_qa").fetchone()[0], 10)

    def test_recheck_failure_prevents_snapshot_creation(self):
        def stale():
            raise CandidateStoreError("Approval is stale")
        with self.assertRaises(CandidateStoreError):
            project_snapshot(fixture(), self.output, self.migration, apply=True, recheck=stale)
        self.assertFalse(self.output.exists())

    def test_unknown_use_case_and_item_collision_fail_closed(self):
        bundle = fixture()
        card = bundle["runs"][0]["cards"][-1]
        card["result"]["usage_selection"]["primary"]["use_case_id"] = "invented.case"
        card["raw_result_json"] = json.dumps(card["result"], ensure_ascii=False)
        with self.assertRaises(sqlite3.IntegrityError):
            self.build(bundle)
        self.assertFalse(self.output.exists())
        bundle = fixture()
        bundle["runs"][0]["cards"][-1]["task"]["item_id"] = "item-0"
        with self.assertRaises(sqlite3.IntegrityError):
            self.build(bundle)
        self.assertFalse(self.output.exists())

    def test_bad_usage_arithmetic_rejected(self):
        bundle = fixture()
        bundle["runs"][0]["token"]["usage"]["total_tokens"] = 999
        with self.assertRaises(CandidateStoreError):
            self.build(bundle)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
