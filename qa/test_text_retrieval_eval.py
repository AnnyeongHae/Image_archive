from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from image_rag_eval.text_retrieval_eval import (
    DIMENSION, LABEL_BASIS, MODEL, SCHEMA, cosine, evaluate_canary,
    rank_groups, render_full_report, render_report, validate_vectors,
)


def unit(axis, dimension=DIMENSION):
    return [1.0 if index == axis else 0.0 for index in range(dimension)]


def document(ident, group=None, representative=None):
    return {"item_id": ident, "style_id": "STYLE-" + ident, "group_id": group or ident,
            "representative_item_id": representative or ident,
            "representative_style_id": "STYLE-" + (representative or ident),
            "image_path": "preview.png", "representative_image_path": "preview.png",
            "approval_state": "image_approved", "budget_blocked": False, "qa_count": 0,
            "compact_input_id": "compact:" + ident, "baseline_input_id": "baseline:" + ident,
            "compact_text": "compact " + ident, "baseline_text": "baseline " + ident,
            "original_prompt": "original " + ident}


def fixture_data():
    # A canonical representative is intentionally not in this candidate subset.
    docs = [document("a", "building", "canonical-building"), document("b", "building", "canonical-building")]
    docs += [document(ident) for ident in "cdefghi"]
    queries = [{"query_id": "q1", "input_id": "query:q1", "text": "purpose for h",
                "relevant_group_ids": ["h"], "evidence_item_ids": ["h"],
                "label_basis": LABEL_BASIS, "human_judged": False}]
    fixture = {"schema_version": SCHEMA, "model": MODEL, "dimension": DIMENSION,
               "label_basis": LABEL_BASIS, "human_judged": False, "release_eligible": False,
               "documents": docs, "queries": queries,
               "ready_item_ids": [d["item_id"] for d in docs], "blocked_item_ids": ["blocked-1", "blocked-2"]}
    vectors = {d[lane + "_input_id"]: unit(index) for index, d in enumerate(docs) for lane in ("compact", "baseline")}
    vectors["query:q1"] = unit(7)
    replay = {"verified": True, "provider_calls": 0, "cache_hit_input_ids": sorted(vectors)}
    return fixture, vectors, replay


def full_fixture_data():
    fixture, _, _ = fixture_data()
    docs = fixture["documents"] + [document("canonical-building", "building", "canonical-building")]
    vectors = {d["item_id"]: unit(index) for index, d in enumerate(docs)}
    queries = [{"query_id": "q1", "text": "building sticker", "source_anchor_groups": ["building"],
                "rankings": rank_groups(docs, vectors, unit(1))},
               {"query_id": "q2", "text": "second saved purpose", "source_anchor_groups": ["h"],
                "rankings": rank_groups(docs, vectors, unit(7))}]
    result = {"schema_version": "image-full-text-search-smoke-1", "status": "prepared_needs_human_review",
              "canary_technical_gate_passed": True, "document_count": len(docs),
              "group_count": len({d["group_id"] for d in docs}), "queries": queries,
              "usage": {"actual_reported_tokens": 241234, "conservative_charged_tokens": 250827,
                        "total_token_cap": 260000, "pending_or_uncertain_requests": 0},
              "rerank_calls": 0, "image_embedding_calls": 0,
              "metadata_human_approved": False, "release_eligible": False}
    return result, docs


class TextRetrievalEvaluationTests(unittest.TestCase):
    def test_group_collapses_members_and_preserves_outside_representative(self):
        docs = [document("a", "g", "rep"), document("b", "g", "rep"), document("c")]
        ranked = rank_groups(docs, {"a": unit(0), "b": unit(1), "c": unit(2)}, unit(1))
        self.assertEqual(len(ranked), 2)
        self.assertEqual(ranked[0]["representative_item_id"], "rep")
        self.assertEqual(ranked[0]["matched_item_id"], "b")
        self.assertEqual(ranked[0]["matched_item_ids"], ["b", "a"])
        self.assertEqual(ranked[0]["score"], 1)

    def test_top5_are_distinct_groups_with_no_early_member_truncation(self):
        docs = [document("a" + str(i), "same", "rep") for i in range(6)]
        docs += [document("b" + str(i)) for i in range(6)]
        vectors = {d["item_id"]: unit(0) for d in docs}
        result = rank_groups(docs, vectors, unit(0))
        self.assertEqual(len(result), 5)
        self.assertEqual(len({row["group_id"] for row in result}), 5)

    def test_conflicting_group_representatives_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "conflicting"):
            rank_groups([document("a", "g", "rep-a"), document("b", "g", "rep-b")],
                        {"a": unit(0), "b": unit(1)}, unit(0))

    def test_duplicate_item_ids_and_missing_or_extra_vectors_rejected(self):
        for vectors, ids in (({"a": unit(0)}, ["a", "a"]), ({}, ["a"]),
                             ({"a": unit(0), "extra": unit(1)}, ["a"])):
            with self.assertRaises(ValueError):
                validate_vectors(vectors, ids)

    def test_dimensions_nonfinite_boolean_and_zero_vectors_rejected(self):
        invalid = [unit(0, 1024), [0.0] * DIMENSION, [float("nan")] * DIMENSION,
                   [float("inf")] * DIMENSION, [True] * DIMENSION, ["1"] * DIMENSION]
        for vector in invalid:
            with self.subTest(vector_type=type(vector[0])):
                with self.assertRaises(ValueError):
                    validate_vectors({"a": vector}, ["a"])

    def test_cosine_handles_unnormalized_vectors(self):
        self.assertAlmostEqual(cosine([x * 3 for x in unit(0)], unit(0)), 1)
        self.assertAlmostEqual(cosine(unit(0), unit(1)), 0)

    def test_unapproved_and_blocked_documents_rejected(self):
        for change in ({"approval_state": "unreviewed"}, {"budget_blocked": True}):
            doc = {**document("a"), **change}
            with self.assertRaises(ValueError):
                rank_groups([doc], {"a": unit(0)}, unit(0))
        with self.assertRaises(ValueError):
            rank_groups([document("a")], {"a": unit(0)}, unit(0), blocked_item_ids=["a"])

    def test_complete_valid_smoke_passes_without_claiming_human_accuracy(self):
        fixture, vectors, replay = fixture_data()
        result = evaluate_canary(fixture, vectors, cache_audit=replay)
        self.assertTrue(result["technical_gate_passed"])
        self.assertEqual(result["lanes"]["compact"]["mean_recall_at_5"], 1)
        self.assertEqual(result["lanes"]["compact"]["mrr_at_5"], 1)
        self.assertFalse(result["production_accuracy_claim_allowed"])
        self.assertFalse(result["metadata_human_approved"])
        self.assertFalse(result["release_eligible"])

    def test_absent_or_incomplete_replay_blocks(self):
        fixture, vectors, replay = fixture_data()
        for audit in (None, {**replay, "provider_calls": 1}, {**replay, "provider_calls": False},
                      {**replay, "cache_hit_input_ids": replay["cache_hit_input_ids"][:-1]}):
            result = evaluate_canary(fixture, vectors, cache_audit=audit)
            self.assertFalse(result["technical_gate_passed"])
            self.assertFalse(result["gates"]["cache_replay_no_provider_calls"])

    def test_qa_and_ready_list_guards(self):
        fixture, vectors, replay = fixture_data()
        fixture["documents"][0]["qa_count"] = 1
        with self.assertRaisesRegex(ValueError, "zero-QA"):
            evaluate_canary(fixture, vectors, cache_audit=replay)
        fixture, vectors, replay = fixture_data()
        fixture["blocked_item_ids"].append("h")
        with self.assertRaisesRegex(ValueError, "unready or blocked"):
            evaluate_canary(fixture, vectors, cache_audit=replay)

    def test_unbound_anchor_groups_and_evidence_fail(self):
        fixture, vectors, replay = fixture_data()
        fixture["queries"][0]["relevant_group_ids"] = ["i"]
        with self.assertRaisesRegex(ValueError, "Unbound"):
            evaluate_canary(fixture, vectors, cache_audit=replay)

    def test_gross_recall_and_mrr_loss_blocks(self):
        fixture, vectors, replay = fixture_data()
        vectors["compact:h"] = [-x for x in unit(7)]
        result = evaluate_canary(fixture, vectors, cache_audit=replay)
        self.assertEqual(result["lanes"]["baseline"]["mean_recall_at_5"], 1)
        self.assertEqual(result["lanes"]["compact"]["mean_recall_at_5"], 0)
        self.assertFalse(result["gates"]["compact_recall_no_gross_loss"])
        self.assertFalse(result["gates"]["compact_mrr_no_gross_loss"])

    def test_self_consistency_is_tie_aware(self):
        fixture, vectors, replay = fixture_data()
        vectors = {key: unit(0) for key in vectors}
        result = evaluate_canary(fixture, vectors, cache_audit=replay)
        self.assertTrue(result["gates"]["self_retrieval_consistency"])
        self.assertTrue(result["technical_gate_passed"])

    def test_foreign_image_vector_space_never_accepted(self):
        fixture, vectors, replay = fixture_data()
        vectors["query:q1"] = unit(0, 1024)
        with self.assertRaisesRegex(ValueError, "dimensional"):
            evaluate_canary(fixture, vectors, cache_audit=replay)

    def test_embedding_manifest_text_mismatch_fails(self):
        fixture, vectors, replay = fixture_data()
        fixture["embedding_manifest"] = {"documents": []}
        with self.assertRaisesRegex(ValueError, "bind"):
            evaluate_canary(fixture, vectors, cache_audit=replay)

    def test_report_escapes_query_and_prompt_and_has_no_remote_assets(self):
        fixture, vectors, replay = fixture_data()
        fixture["queries"][0]["text"] = '<script>alert("bad")</script>'
        fixture["documents"][0]["original_prompt"] = '</textarea><script>bad</script>'
        result = evaluate_canary(fixture, vectors, cache_audit=replay)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "preview.png").write_bytes(b"local fixture only")
            rendered = render_report(fixture, result, root)
        self.assertNotIn('<script>alert("bad")</script>', rendered)
        self.assertIn('&lt;script&gt;', rendered)
        self.assertEqual(rendered.count('<script>'), 1)
        self.assertNotIn('https://', rendered)
        self.assertIn('default-src', rendered)
        self.assertIn('대표 STYLE-canonical-building', rendered)
        self.assertIn('<details class="group-members"><summary>그룹 구성원 2개 보기</summary>', rendered)
        self.assertNotIn('<details class="group-members" open', rendered)
        self.assertIn('data-copy="copy-prompt-0"', rendered)
        script = re.search(r'<script>(.*?)</script>', rendered, re.S).group(1)
        expected_hash = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        self.assertIn("script-src 'sha256-" + expected_hash + "'", rendered)
        self.assertNotIn('innerHTML', script)

    def test_report_rejects_escape_to_external_file(self):
        fixture, vectors, replay = fixture_data()
        result = evaluate_canary(fixture, vectors, cache_audit=replay)
        result["lanes"]["compact"]["queries"][0]["rankings"][0]["representative_image_path"] = "../outside.png"
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises((ValueError, FileNotFoundError)):
                render_report(fixture, result, Path(folder))

    def render_full_fixture(self, result, docs):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "preview.png").write_bytes(b"local fixture only")
            return render_full_report(result, docs, root)

    def test_full_report_is_cached_query_switcher_with_cumulative_usage(self):
        result, docs = full_fixture_data()
        rendered = self.render_full_fixture(result, docs)
        self.assertIn('id="saved-query"', rendered)
        self.assertIn('data-query-panel="0"', rendered)
        self.assertIn('data-query-panel="1"', rendered)
        self.assertIn('241,234', rendered)
        self.assertIn('250,827', rendered)
        self.assertIn('9,173', rendered)
        self.assertIn('동일 실행 디렉터리의 누적 합계', rendered)
        self.assertIn('후보 집합이 다릅니다', rendered)
        self.assertIn('메타데이터·권리·공개 승인은 부여하지 않습니다', rendered)
        self.assertIn('CASE-176, CASE-530', rendered)
        script = re.search(r'<script>(.*?)</script>', rendered, re.S).group(1)
        self.assertIn('panel.hidden', script)
        self.assertNotIn('fetch(', script)
        self.assertNotIn('XMLHttpRequest', script)

    def test_full_report_prioritizes_results_and_collapses_all_detailed_notices(self):
        result, docs = full_fixture_data()
        rendered = self.render_full_fixture(result, docs)
        self.assertLess(rendered.index('id="saved-query"'), rendered.index('<img '))
        self.assertLess(rendered.index('<img '), rendered.index('id="usage-title"'))
        self.assertIn('</main><details class="run-details"><summary>실행·사용량·검증 안내</summary>', rendered)
        self.assertNotIn('<details class="run-details" open', rendered)
        before_details, details = rendered.split('<details class="run-details">', 1)
        for notice in ('동일 실행 디렉터리의 누적 합계', 'CASE-176, CASE-530', '후보 집합이 다릅니다',
                       '메타데이터·권리·공개 승인은 부여하지 않습니다', '실제 청구 금액·무료 잔액'):
            self.assertNotIn(notice, before_details)
            self.assertIn(notice, details)
        self.assertIn('저장된 2개 질의의 결과 · 실시간 검색 아님 · 정확도는 사람 검토 필요', before_details)
        self.assertIn('id="query-status" class="sr-only" role="status" aria-live="polite"', rendered)

    def test_full_report_keeps_only_canonical_picture_outside_collapsed_members(self):
        result, docs = full_fixture_data()
        rendered = self.render_full_fixture(result, docs)
        card = rendered.split('<article class="result-card"', 1)[1].split('</article>', 1)[0]
        before_members, members = card.split('<details class="group-members">', 1)
        self.assertEqual(before_members.count('<img '), 1)
        self.assertIn('정본 대표 STYLE-canonical-building', before_members)
        self.assertIn('최고 일치 구성원 STYLE-b', before_members)
        self.assertNotIn('구성원 STYLE-b', before_members.split('<figcaption>', 1)[1])
        self.assertIn('그룹 구성원 3개 보기', members)
        self.assertNotIn('<details class="group-members" open', rendered)
        self.assertIn('구성원 STYLE-a', members)
        self.assertIn('구성원 STYLE-b', members)

    def test_full_report_prompt_buttons_use_distinct_matching_ids(self):
        result, docs = full_fixture_data()
        rendered = self.render_full_fixture(result, docs)
        ids = re.findall(r'<textarea readonly id="([^"]+)"', rendered)
        buttons = re.findall(r'data-copy="([^"]+)"', rendered)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, buttons)
        self.assertGreater(len(ids), 0)
        self.assertIn('original canonical-building', rendered)

    def test_full_report_escapes_all_payloads_and_pins_script(self):
        result, docs = full_fixture_data()
        result['queries'][0]['text'] = '<script>alert("query")</script>'
        docs[-1]['original_prompt'] = '</textarea><img src="https://evil.example/x" onerror="alert(1)">'
        rendered = self.render_full_fixture(result, docs)
        self.assertEqual(rendered.count('<script>'), 1)
        self.assertNotIn('<script>alert("query")</script>', rendered)
        self.assertNotRegex(rendered, r'(?:src|href)="https?://')
        self.assertIn('&lt;/textarea&gt;', rendered)
        script = re.search(r'<script>(.*?)</script>', rendered, re.S).group(1)
        expected_hash = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        self.assertIn("script-src 'sha256-" + expected_hash + "'", rendered)
        self.assertNotIn('innerHTML', script)

    def test_full_report_rejects_missing_members_and_wrong_representative(self):
        result, docs = full_fixture_data()
        result['queries'][0]['rankings'][0]['member_scores'].pop()
        with self.assertRaisesRegex(ValueError, 'every group member'):
            self.render_full_fixture(result, docs)
        result, docs = full_fixture_data()
        result['queries'][0]['rankings'][0]['representative_item_id'] = 'b'
        with self.assertRaisesRegex(ValueError, 'canonical group'):
            self.render_full_fixture(result, docs)

    def test_full_report_rejects_ambiguous_usage_or_approval(self):
        for change in ({'actual_reported_tokens': None}, {'conservative_charged_tokens': 260001},
                       {'pending_or_uncertain_requests': 1}, {'actual_reported_tokens': True}):
            result, docs = full_fixture_data()
            result['usage'].update(change)
            with self.assertRaisesRegex(ValueError, 'receipt'):
                self.render_full_fixture(result, docs)
        result, docs = full_fixture_data()
        result['release_eligible'] = True
        with self.assertRaisesRegex(ValueError, 'approval'):
            self.render_full_fixture(result, docs)

    def test_full_report_rejects_count_mismatch_duplicate_groups_and_score_tampering(self):
        result, docs = full_fixture_data()
        result['document_count'] += 1
        with self.assertRaisesRegex(ValueError, 'count'):
            self.render_full_fixture(result, docs)
        result, docs = full_fixture_data()
        result['queries'][0]['rankings'][1] = result['queries'][0]['rankings'][0]
        with self.assertRaisesRegex(ValueError, 'distinct'):
            self.render_full_fixture(result, docs)
        result, docs = full_fixture_data()
        result['queries'][0]['rankings'][0]['score'] = float('nan')
        with self.assertRaisesRegex(ValueError, 'cosine'):
            self.render_full_fixture(result, docs)

    def test_full_report_rejects_external_image_path(self):
        result, docs = full_fixture_data()
        docs[-1]['image_path'] = 'https://evil.example/a.png'
        with self.assertRaises((ValueError, FileNotFoundError, OSError)):
            self.render_full_fixture(result, docs)


if __name__ == "__main__":
    unittest.main()
