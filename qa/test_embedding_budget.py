from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from image_rag_eval.embedding_budget import (DIMENSION, DOCUMENT_PREFIX, build_plan, compact_projection,
                                            file_sha256, guard_rerank_budget, write_plan)


class FixtureTokenizer:
    """Character counter for deterministic fixtures, never a provider estimate."""
    def no_truncation(self):
        self.truncation_disabled = True

    def no_padding(self):
        self.padding_disabled = True

    def encode(self, text, add_special_tokens=False):
        assert not add_special_tokens
        return SimpleNamespace(ids=list(text))


def fixture_database(path):
    connection = sqlite3.connect(path)
    connection.executescript("""
      CREATE TABLE source_items(item_id TEXT,style_id TEXT,prompt_sha256 TEXT,approval_state TEXT);
      CREATE TABLE prompts(sha256 TEXT,original_text TEXT);
      CREATE TABLE analysis_results(candidate_id TEXT,item_id TEXT,effective_json TEXT);
      CREATE TABLE human_notes(item_id TEXT,memo TEXT);
      CREATE TABLE approval_groups(group_id TEXT,representative_item_id TEXT);
      CREATE TABLE group_memberships(group_id TEXT,item_id TEXT);
      CREATE TABLE candidate_qa(candidate_id TEXT,ordinal INTEGER,field_path TEXT);
      CREATE TABLE taxonomy_terms(taxonomy_sha256 TEXT,facet TEXT,term_id TEXT,label_ko TEXT);
      CREATE TABLE usage_assignments(candidate_id TEXT,ordinal INTEGER,taxonomy_sha256 TEXT,facet TEXT,use_case_id TEXT,raw_json TEXT);
      CREATE TABLE diagnostic_documents(item_id TEXT,text TEXT);
      INSERT INTO approval_groups VALUES('g1','a');
      INSERT INTO group_memberships VALUES('g1','a'),('g1','b');
      INSERT INTO taxonomy_terms VALUES('tax','use_case','brand.hero','브랜드 대표');
    """)
    usage = {"use_case_id": "brand.hero", "why_ko": "활용 이유", "changes": ["이름 교체"], "constraints": ["비율 유지"]}
    result = {"schema_version": "luna-compact-3", "visual": {"caption_ko": "승인된 그림", "styles": ["선화"]},
              "prompt": {"purpose_ko": "원하는 목적", "slots": [{"name": "인물", "current": "한 명", "change": "두 명"}]},
              "uses": [usage], "rights": "DO NOT PROJECT", "search_keywords_en": ["EXCLUDED KEYWORDS"]}
    for ident, state in (("a", "image_approved"), ("b", "image_approved"), ("c", "image_approved"), ("d", "unreviewed")):
        connection.execute("INSERT INTO source_items VALUES(?,?,?,?)", (ident, "STYLE-" + ident, ident, state))
        connection.execute("INSERT INTO prompts VALUES(?,?)", (ident, "RAW ORIGINAL " + ident))
        connection.execute("INSERT INTO analysis_results VALUES(?,?,?)", ("candidate-" + ident, ident, json.dumps(result, ensure_ascii=False)))
        connection.execute("INSERT INTO usage_assignments VALUES(?,0,'tax','use_case','brand.hero',?)", ("candidate-" + ident, json.dumps(usage)))
        connection.execute("INSERT INTO diagnostic_documents VALUES(?,?)", (ident, "FROZEN FTS " + ident))
    connection.commit()
    connection.close()


class EmbeddingBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "library.sqlite3"
        self.tokenizer_file = self.root / "fixture-tokenizer.json"
        self.tokenizer_file.write_text("{}", encoding="utf-8")
        fixture_database(self.database)

    def plan(self, **kwargs):
        return build_plan(self.database, self.tokenizer_file, tokenizer=FixtureTokenizer(), **kwargs)

    def execute(self, sql):
        connection = sqlite3.connect(self.database)
        try:
            connection.executescript(sql)
            connection.commit()
        finally:
            connection.close()

    def test_candidate_qa_excludes_whole_root_in_pointer_and_legacy_paths(self):
        result = {"visual": {"description_ko": "REMOVE_VISUAL", "styles": ["REMOVE_STYLE"]},
                  "prompt_intent": {"summary_ko": "REMOVE_INTENT"}, "prompt_analysis": {"intended_purpose_ko": "REMOVE_ANALYSIS"},
                  "prompt": {"purpose_ko": "REMOVE_PROMPT"}, "reuse_ideas": [{"use_case": "REMOVE_REUSE"}]}
        paths = ["/visual/layout/0", "prompt_intent.mismatch_candidates[0]", "/prompt/slots", "prompt_analysis.mismatch_candidates", "reuse_ideas[0].caution"]
        text, roots = compact_projection(result, paths, memo="메모 유지")
        self.assertNotIn("REMOVE_", text)
        self.assertIn("메모 유지", text)
        self.assertEqual(set(roots), set(result))
        for path in ("visual.description_ko", "/visual/caption_ko", "visual", "$.visual.subjects"):
            self.assertNotIn("REMOVE_VISUAL", compact_projection(result, [path])[0])

    def test_usage_qa_also_excludes_normalized_relational_rows(self):
        usage = [{"label_ko": "REMOVE_LABEL", "value": {"use_case_id": "REMOVE_ID", "why_ko": "REMOVE_REASON"}}]
        for root, path in (("uses", "/uses/0/why_ko"), ("usage_selection", "usage_selection.primary")):
            text, _ = compact_projection({root: []}, [path], usage)
            self.assertEqual(text, "")

    def test_legacy_schemas_keep_specific_semantics_not_keyword_lists(self):
        v1 = {"visual": {"description_ko": "설명", "style": ["양식"], "background": "바탕", "composition": ["구도"]},
              "prompt_intent": {"summary_ko": "목적", "requested_controls": ["고정규칙"]},
              "reuse_ideas": [{"use_case": "재활용", "visual_reason": "근거", "adaptation": "변경", "caution": "제약"}],
              "search_hints": {"keywords_en": ["KEYWORD_ONLY"]}}
        text, _ = compact_projection(v1)
        for value in ("설명", "양식", "바탕", "구도", "목적", "고정규칙", "재활용", "근거", "변경", "제약"):
            self.assertIn(value, text)
        self.assertNotIn("KEYWORD_ONLY", text)
        v2 = {"visual": {"background": {"description_ko": "배경설명"}},
              "prompt_analysis": {"intended_purpose_ko": "활용목적", "replaceable_slots": [
                  {"slot_ko": "이름", "current_value_ko": "현재값", "replacement_guidance_ko": "변경지침"}]}, "usage_selection": {}}
        use = {"value": {"use_case_id": "brand.hero", "why_usable_ko": "사용근거", "adaptation_ko": "수정방식", "constraints_ko": ["보존제약"]}, "label_ko": "브랜드 대표"}
        text, _ = compact_projection(v2, usage_rows=[use])
        for value in ("배경설명", "활용목적", "이름", "현재값", "변경지침", "brand.hero", "브랜드 대표", "사용근거", "수정방식", "보존제약"):
            self.assertIn(value, text)

    def test_deduplicate_exact_normalized_strings(self):
        text, _ = compact_projection({"visual": {"caption_ko": "  가   나 ", "styles": ["가 나", "가 나"]}}, memo="가 나")
        self.assertEqual(text.count("가 나"), 1)

    def test_readonly_dryrun_retains_approved_children_and_all_source_bytes(self):
        before = file_sha256(self.database)
        directory_before = set(self.root.iterdir())
        plan = self.plan()
        receipt = write_plan(plan, self.root / "data/private-research/plans", archive_root=self.root)
        self.assertEqual(set(self.root.iterdir()), directory_before)
        self.assertEqual(file_sha256(self.database), before)
        self.assertEqual(receipt["status"], "dry_run")
        self.assertEqual([d["item_id"] for d in plan["documents"]], ["a", "b", "c"])
        self.assertEqual([d["representative_item_id"] for d in plan["documents"]], ["a", "a", "c"])
        self.assertNotIn("RAW ORIGINAL", plan["documents"][0]["compact_text"])

    def test_no_group_mixing_or_overlapping_members(self):
        self.execute("INSERT INTO approval_groups VALUES('g2','c'); INSERT INTO group_memberships VALUES('g2','c'),('g2','b');")
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            self.plan()

    def test_representative_must_be_member_of_same_group(self):
        self.execute("UPDATE approval_groups SET representative_item_id='c';")
        with self.assertRaisesRegex(ValueError, "Representative"):
            self.plan()

    def test_duplicate_current_candidates_rejected(self):
        self.execute("INSERT INTO analysis_results SELECT 'duplicate',item_id,effective_json FROM analysis_results WHERE item_id='a';")
        with self.assertRaisesRegex(ValueError, "at most one"):
            self.plan()

    def test_estimates_and_future_cache_identity_without_group_collapse(self):
        plan = self.plan()
        summary, docs = plan["summary"], plan["documents"]
        self.assertEqual(summary["approved_document_count"], 3)
        self.assertEqual(summary["representative_document_count"], 2)
        self.assertEqual(summary["unique_compact_input_sha_count"], 1)
        self.assertEqual(len({d["future_cache_key"] for d in docs}), 1)
        self.assertEqual(summary["statistics"]["all_approved"]["raw_vector_bytes_float32_estimate"], 3 * DIMENSION * 4)
        self.assertEqual(summary["statistics"]["representative_only_optional"]["raw_vector_bytes_float32_estimate"], 2 * DIMENSION * 4)
        self.assertEqual(docs[0]["measurements"]["compact"]["prefixed_tokens"], len(DOCUMENT_PREFIX) + len(docs[0]["compact_text"]))
        for key in ("model_calls", "network_calls", "embedding_calls", "rerank_calls"):
            self.assertEqual(summary[key], 0)
        self.assertFalse(summary["rerank_policy"]["enabled"])
        self.assertIsNone(summary["actual_billed_cost"])

    def test_over_budget_does_not_truncate_text(self):
        unlimited, blocked = self.plan(max_tokens=10000), self.plan(max_tokens=1)
        self.assertEqual([d["compact_text"] for d in unlimited["documents"]], [d["compact_text"] for d in blocked["documents"]])
        self.assertEqual(blocked["summary"]["needs_compaction_count"], 3)
        self.assertTrue(all(d["budget_blocked"] for d in blocked["documents"]))

    def test_missing_metadata_is_blocked_and_not_omitted(self):
        self.execute("DELETE FROM analysis_results WHERE item_id='b';")
        plan = self.plan()
        self.assertEqual(len(plan["documents"]), 3)
        self.assertEqual(plan["documents"][1]["status"], "missing_semantics")
        self.assertTrue(plan["documents"][1]["budget_blocked"])

    def test_apply_is_private_immutable_and_idempotent(self):
        plan = self.plan()
        output = self.root / "data/private-research/plans"
        first = write_plan(plan, output, archive_root=self.root, apply=True)
        self.assertEqual(first, write_plan(plan, output, archive_root=self.root, apply=True))
        artifact = self.root / first["path"] / "summary.json"
        artifact.write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Immutable"):
            write_plan(plan, output, archive_root=self.root, apply=True)
        self.assertEqual(artifact.read_text(), "tampered")
        with self.assertRaisesRegex(ValueError, "private-research"):
            write_plan(plan, self.root / "dist", archive_root=self.root, apply=True)

    def test_live_wal_rejected_without_accessing_it_as_snapshot(self):
        Path(str(self.database) + "-wal").write_bytes(b"pending")
        with self.assertRaisesRegex(ValueError, "WAL"):
            self.plan()

    def test_symlink_artifacts_are_rejected(self):
        plan = self.plan()
        with mock.patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaisesRegex(ValueError, "Symlink"):
                write_plan(plan, self.root / "data/private-research/plans", archive_root=self.root, apply=True)

    def test_rerank_formula_and_boundaries(self):
        self.assertEqual(guard_rerank_budget(100, [200, 300]), 700)
        self.assertEqual(guard_rerank_budget(100, [400] * 20), 10000)
        self.assertEqual(guard_rerank_budget(0, []), 0)
        for args in ((True, [1]), (-1, [1]), (1.5, [1]), (1, [True]), (1, [-1]), (1, [1.5]),
                     (1, [1] * 21), (101, [400] * 20), (1, [1], True), (1, [1], 20, -1), (1, "1")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                guard_rerank_budget(*args)


if __name__ == "__main__":
    unittest.main()
