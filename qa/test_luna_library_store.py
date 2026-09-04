"""Full-library checkpoint tests use synthetic local records only."""
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
from image_rag_eval.luna_library_store import MIGRATION, LibraryStoreError, diagnostic_search, populate, project_library, validate_enum_repair


def fixture():
    taxonomy = {"families": [{"use_cases": [{"id": "content.entry_cover", "label_ko": "콘텐츠 대표 화면"}]}],
                "asset_formats": [], "aliases": [{"facet": "use_case", "target_id": "content.entry_cover", "term": "entry cover"}]}
    items = []
    states = ["image_approved", "image_approved", "image_approved", "archived_alias", "unreviewed", "retained_unchecked"]
    for index, state in enumerate(states):
        items.append({"item_id": f"i{index}", "style_id": f"CASE-{index}", "source_run_id": "source", "state": state,
                      "original_sha256": digest(f"o{index}".encode()), "prepared_sha256": digest(f"p{index}".encode()),
                      "original_path": f"original/{index}.jpg", "prepared_path": f"prepared/{index}.png",
                      "prompt_sha256": digest("원문 교육자료".encode()), "prompt_text": "원문 교육자료",
                      "memo": "감정 표현에 활용" if index == 0 else "", "rights": {"release_eligible": False, "status": "unverified"},
                      "raw": {"original": "raw evidence"}})
    tasks = [{"item_id": f"i{index}", "style_id": f"CASE-{index}", "analysis_mode": "legacy_reuse" if index == 0 else "new_compact",
              "input_fingerprint": digest(str(index).encode()), "batch_id": None if index == 0 else "batch-1",
              "state": ["legacy_reused", "validated_candidate", "pending"][index], "error": None} for index in range(3)]
    usage = {"input_tokens_including_cached": 100, "cached_input_tokens": 70, "output_tokens_including_reasoning": 20,
             "reasoning_output_tokens": 5, "total_tokens": 120}
    old_token = {"usage": usage, "scope": "legacy_successful_turn"}
    legacy_result = {"schema_version": "legacy-fixture", "visual": {"description_ko": "노란 캐릭터"},
                     "prompt_intent": {"purpose": "교육"}, "reuse_ideas": [{"use_case": "unknown_unmapped"}]}
    compact_result = {"schema_version": "luna-compact-3", "visual": {"caption_ko": "파란 캐릭터"},
                      "prompt": {"purpose": "영상"}, "uses": [{"use_case_id": "content.entry_cover", "fit": "supported", "priority": "primary"}],
                      "extras_json": {"note": "freeform"}}
    return {"source_commit": {"id": "commit-1"}, "analysis_run_id": "full-run", "manifest": {"source_run_id": "source"},
            "manifest_sha256": "a" * 64, "items": items, "tasks": tasks,
            "groups": [{"candidate_id": "group-1", "suggested_representative_id": "i0", "member_ids": ["i0", "i1"]}],
            "aliases": [{"id": "i3", "final_representative_id": "i0"}],
            "taxonomy": taxonomy, "taxonomy_raw_json": encode(taxonomy).decode(), "taxonomy_sha256": digest(encode(taxonomy)),
            "legacy_runs": [{"manifest": {"analysis_run_id": "legacy-run"}, "task_manifest_sha256": "b" * 64,
                             "token": old_token, "token_raw_json": encode(old_token).decode()}],
            "results": [{"item_id": "i0", "source_run_id": "legacy-run", "mode": "legacy", "result": legacy_result,
                         "raw_json": json.dumps(legacy_result, ensure_ascii=False, indent=2), "qa": []},
                        {"item_id": "i1", "source_run_id": "full-run", "mode": "compact", "result": compact_result,
                         "raw_json": json.dumps(compact_result, ensure_ascii=False, indent=2), "qa": []}],
            "tokens": [], "evidence": {}, "watched_outputs": {}}


def turn_token(unknown=False):
    usage = {"input_tokens": None if unknown else 100, "cached_input_tokens": None if unknown else 70,
             "cache_write_input_tokens": None, "output_tokens": None if unknown else 20, "reasoning_output_tokens": None}
    total = None if unknown else 120
    aggregate = {**usage, "total_tokens_calculated": total, "uncached_input_tokens_calculated": None if unknown else 30,
                 "ordinary_input_tokens_calculated": None}
    counts = {key: int(value is None) for key, value in {**usage, "total_tokens_calculated": total}.items()}
    known = {key: value if value is not None else 0 for key, value in {**usage, "total_tokens_calculated": total}.items()}
    turn = {"turn_id": "turn-1", "style_ids": ["CASE-1", "CASE-2"], "batch_ids": ["batch-1"],
            "model_reported": "gpt-5.6-luna", "completion_event_reported": "task_complete", "work_success_inferred": False,
            "attributable_usage": usage, "total_tokens_calculated": total}
    receipt = {"schema_version": "image-luna-rollout-turn-usage-receipt-1", "session_id_reported": "session-1",
               "agent_path_reported": "/root/luna_worker", "scope": "explicit_completed_turn_ids_only",
               "turn_ids": ["turn-1"], "excluded_historical_turn_ids": ["old-turn"], "turns": [turn],
               "usage": aggregate, "known_usage_subtotals": known, "unknown_turn_counts": counts,
               "actual_billed_tokens": None, "actual_billed_cost": None}
    return {"receipt": receipt, "raw_json": json.dumps(receipt), "path": "execution/first.tokens.json"}


class LibraryStoreTests(unittest.TestCase):
    def test_enum_repair_cannot_change_observation_or_invent_mapping(self):
        original = {"visual": {"medium": "digital_illustration", "caption_ko": "original"}}
        effective = {"visual": {"medium": "illustration", "caption_ko": "original"}}
        validate_enum_repair(original, effective, [["medium", "digital_illustration", "illustration"]])
        with self.assertRaisesRegex(LibraryStoreError, "Unapproved"):
            validate_enum_repair(original, effective, [["medium", "digital_illustration", "photograph"]])
        effective["visual"]["caption_ko"] = "changed observation"
        with self.assertRaisesRegex(LibraryStoreError, "changed observation"):
            validate_enum_repair(original, effective, [["medium", "digital_illustration", "illustration"]])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / "snapshots"

    def build(self, bundle=None, apply=True):
        return project_library(bundle or fixture(), self.base, ROOT / MIGRATION, apply=apply)

    def connect(self, result):
        db = sqlite3.connect(result["database_path"])
        self.addCleanup(db.close)
        return db

    def test_dry_run_no_write_apply_once(self):
        result = self.build(apply=False)
        self.assertFalse(self.base.exists())
        self.assertEqual(result["source_images"], 6)
        first, second = self.build(), self.build()
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(first["database_sha256"], second["database_sha256"])

    def test_progress_creates_new_snapshot_preserving_old(self):
        first = self.build()
        previous = Path(first["database_path"]).read_bytes()
        bundle = fixture()
        bundle["tasks"][2]["state"] = "visual_draft_ready"
        second = self.build(bundle)
        self.assertNotEqual(first["snapshot_key"], second["snapshot_key"])
        self.assertEqual(Path(first["database_path"]).read_bytes(), previous)
        self.assertEqual(len(list(self.base.iterdir())), 2)

    def test_pending_usage_null_not_zero(self):
        result = self.build()
        self.assertIsNone(result["observed_compact_tokens"])
        self.assertEqual(result["usage_states"]["usage_pending"], 2)
        self.assertEqual(result["analysis_states"]["pending"], 1)
        self.assertEqual(result["observed_legacy_scope_tokens"], 120)

    def test_all_states_memos_rights_and_raw_json_preserved(self):
        bundle = fixture()
        result = self.build(bundle)
        db = self.connect(result)
        self.assertEqual(result["approval_states"], {"image_approved": 3, "archived_alias": 1, "retained_unchecked": 1, "unreviewed": 1})
        self.assertEqual(db.execute("SELECT memo FROM human_notes").fetchone()[0], "감정 표현에 활용")
        self.assertEqual(db.execute("SELECT count(*) FROM public_search_items").fetchone()[0], 0)
        stored = db.execute("SELECT raw_json FROM analysis_results WHERE item_id='i0'").fetchone()[0]
        self.assertEqual(stored, bundle["results"][0]["raw_json"])
        self.assertEqual(db.execute("SELECT count(*) FROM usage_assignments").fetchone()[0], 1)

    def test_source_arguments_normalized_once_without_llm_inference(self):
        bundle = fixture()
        prompt = '원문 {argument name="제목" default="풍자"} {argument name=missing}'
        sha = digest(prompt.encode())
        for row in bundle["items"]:
            row.update(prompt_text=prompt, prompt_sha256=sha)
        result = self.build(bundle)
        db = self.connect(result)
        self.assertEqual(result["literal_source_arguments"], 1)
        self.assertEqual(result["prompts_with_unparsed_argument_markers"], 1)
        row = db.execute("SELECT name_raw,default_raw,provenance,start_char,end_char,literal FROM source_prompt_arguments").fetchone()
        self.assertEqual(row[:3], ("제목", "풍자", "literal_source_not_llm_or_human_approval"))
        self.assertEqual(prompt[row[3]:row[4]], row[5])
        self.assertEqual(db.execute("SELECT original_text FROM prompts WHERE sha256=?", (sha,)).fetchone()[0], prompt)
        self.assertEqual(db.execute("SELECT count(*) FROM public_search_items").fetchone()[0], 0)

    def test_previous_luna_result_preserved_separately_from_current(self):
        bundle = fixture()
        raw, draft = '{"visual":{"caption_ko":"이전 오류"}}', '{"visual":{"caption_ko":"이전 오류"}}'
        bundle["result_history"] = [{"item_id": "i1", "history_sha256": "a" * 64,
            "input_fingerprint": "b" * 64, "raw_json": raw, "draft_json": draft,
            "raw_sha256": digest(raw.encode()), "draft_sha256": digest(draft.encode()),
            "reason": "quality_flagged_luna_reanalysis"}]
        result = self.build(bundle)
        db = self.connect(result)
        self.assertEqual(result["preserved_result_revisions"], 1)
        self.assertEqual(db.execute("SELECT raw_json FROM analysis_result_history").fetchone()[0], raw)
        self.assertNotEqual(db.execute("SELECT raw_json FROM analysis_results WHERE item_id='i1'").fetchone()[0], raw)
        self.assertEqual(diagnostic_search(db, "이전 오류"), [])
        bundle["result_history"][0]["raw_json"] = '{}'
        with self.assertRaisesRegex(LibraryStoreError, "history bytes changed"):
            self.build(bundle)

    def test_group_member_match_returns_representative_once(self):
        db = self.connect(self.build())
        hits = diagnostic_search(db, "캐릭터")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["item_id"], "i0")
        self.assertEqual(hits[0]["matched_style_ids"], ["CASE-0", "CASE-1"])
        self.assertEqual(diagnostic_search(db, "entry cover")[0]["item_id"], "i0")
        self.assertEqual(len(diagnostic_search(db, "교육")), 2)  # One group plus one ungrouped approved item.

    def test_injection_and_wildcards_plain(self):
        db = self.connect(self.build())
        self.assertEqual(diagnostic_search(db, "\" OR 1=1; DROP TABLE source_items --"), [])
        self.assertEqual(diagnostic_search(db, "%"), [])
        self.assertEqual(db.execute("SELECT count(*) FROM source_items").fetchone()[0], 6)

    def test_unreviewed_task_forbidden_and_transaction_rollback(self):
        bundle = fixture()
        bundle["tasks"][-1]["item_id"] = "i4"
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.executescript((ROOT / MIGRATION).read_text(encoding="utf-8"))
        with self.assertRaises(LibraryStoreError):
            populate(db, bundle, "a" * 64)
        self.assertEqual(db.execute("SELECT count(*) FROM source_items").fetchone()[0], 0)

    def test_changed_existing_database_rejected(self):
        first = self.build()
        db = sqlite3.connect(first["database_path"])
        db.execute("UPDATE human_notes SET memo='tampered'")
        db.commit()
        db.close()
        with self.assertRaises(LibraryStoreError):
            self.build()

    def test_source_race_does_not_publish(self):
        def changed():
            raise LibraryStoreError("source changed")
        with self.assertRaises(LibraryStoreError):
            project_library(fixture(), self.base, ROOT / MIGRATION, apply=True, recheck=changed)
        self.assertFalse(self.base.exists())

    def test_turn_tokens_known_core_optional_fields_null(self):
        bundle = fixture()
        bundle["tokens"] = [turn_token()]
        result = self.build(bundle)
        self.assertEqual(result["observed_compact_tokens"], 120)
        self.assertEqual(result["usage_states"]["observed_turn_scope"], 2)
        db = self.connect(result)
        self.assertIsNone(db.execute("SELECT reasoning_output_tokens FROM token_turns").fetchone()[0])

    def test_completed_turn_without_usage_remains_unknown(self):
        bundle = fixture()
        bundle["tokens"] = [turn_token(unknown=True)]
        result = self.build(bundle)
        self.assertIsNone(result["observed_compact_tokens"])
        self.assertEqual(result["compact_unobserved_token_turns"], 1)
        self.assertEqual(result["usage_states"]["usage_unobserved_completed_turn"], 2)

    def test_overlapping_turn_receipts_dedup_exact_turn(self):
        bundle = fixture()
        first, second = turn_token(), turn_token()
        second["path"] = "execution/second.tokens.json"
        second["receipt"]["notes"] = ["cumulative receipt preserves same turn"]
        second["raw_json"] = json.dumps(second["receipt"])
        bundle["tokens"] = [first, second]
        result = self.build(bundle)
        self.assertEqual(result["observed_compact_tokens"], 120)
        self.assertEqual(result["compact_observed_turns"], 1)
        self.assertEqual(result["token_receipts"], 3)

    def test_invalid_cache_partition_rejected(self):
        bundle = fixture()
        token = turn_token()
        token["receipt"]["turns"][0]["attributable_usage"]["cache_write_input_tokens"] = 40
        token["raw_json"] = json.dumps(token["receipt"])
        bundle["tokens"] = [token]
        with self.assertRaises(LibraryStoreError):
            self.build(bundle)

    def test_cross_granularity_same_session_rejected(self):
        bundle = fixture()
        turn = turn_token()
        usage = {"input_tokens_including_cached": 100, "cached_input_tokens": 70, "output_tokens_including_reasoning": 20,
                 "reasoning_output_tokens": 5, "total_tokens": 120}
        session = {"schema_version": "image-luna-batch-token-usage-receipt-1", "analysis_run_id": "full-run",
                   "usage": usage, "expected_style_ids": ["CASE-1", "CASE-2"], "scope": "session",
                   "sessions": [{"session_id": "session-1", "style_ids": ["CASE-1", "CASE-2"], "usage": {"total_tokens": 120}}],
                   "actual_billed_tokens": None, "actual_billed_cost": None}
        bundle["tokens"] = [turn, {"receipt": session, "raw_json": json.dumps(session), "path": "execution/session.tokens.json"}]
        with self.assertRaises(LibraryStoreError):
            self.build(bundle)

    def test_quality_warning_excludes_flagged_visual_from_diagnostic(self):
        bundle = fixture()
        bundle["results"][1]["qa"] = [{"field": "/visual/caption_ko", "finding": {"status": "needs_review"}}]
        result = self.build(bundle)
        db = self.connect(result)
        self.assertEqual(diagnostic_search(db, "파란"), [])
        self.assertEqual(result["qa_findings"], 1)
        self.assertIn("파란", db.execute("SELECT raw_json FROM analysis_results WHERE item_id='i1'").fetchone()[0])

    def test_literal_normalization_preserves_raw_and_effective_separately(self):
        from qa.test_compact_projection import ProjectionTests
        import copy
        projection_fixture = ProjectionTests()
        projection_fixture.setUp()
        projection_fixture.result["uses"][0]["use_case_id"] = "content.entry_cover"
        projected = projection_fixture.run_projection()
        bundle = fixture()
        bundle["results"][1] = {"item_id": "i1", "source_run_id": "full-run", "mode": "compact",
                "result": projection_fixture.result, "raw_json": json.dumps(projection_fixture.result, ensure_ascii=False, indent=2),
                "raw_draft_json": json.dumps(projection_fixture.draft, ensure_ascii=False),
                "effective_result": projected["result"], "effective_draft": projected["draft"],
                "normalization": projected["normalization"], "qa": []}
        result = self.build(bundle)
        db = self.connect(result)
        raw, effective, visual = db.execute("SELECT raw_json,effective_json,visual_json FROM analysis_results WHERE item_id='i1'").fetchone()
        self.assertEqual(len(json.loads(raw)["visual"]["layout"]), 4)
        self.assertEqual(len(json.loads(effective)["visual"]["layout"]), 3)
        self.assertEqual(len(json.loads(visual)["layout"]), 3)
        self.assertEqual(result["literal_normalized_candidates"], 1)
        self.assertEqual(result["new_raw_strict_valid_candidates"], 0)
        invalid = copy.deepcopy(bundle)
        invalid["results"][1]["normalization"]["derived_value_sha256"] = "0" * 64
        with self.assertRaises(LibraryStoreError):
            self.build(invalid)

    def test_draft_envelope_projection_preserves_original_evidence(self):
        from qa.test_compact_projection import ProjectionTests
        import copy
        sample = ProjectionTests(); sample.setUp()
        sample.result["uses"][0]["use_case_id"] = "content.entry_cover"
        sample.result["visual"]["layout"] = sample.result["visual"]["layout"][:3]
        sample.draft = {"schema_version": "luna-compact-3", "style_id": "X-001", "visual": copy.deepcopy(sample.result["visual"])}
        projected = sample.run_projection()
        bundle = fixture()
        bundle["results"][1] = {"item_id": "i1", "source_run_id": "full-run", "mode": "compact",
            "result": sample.result, "raw_json": json.dumps(sample.result, ensure_ascii=False),
            "raw_draft_json": json.dumps(sample.draft, ensure_ascii=False), "effective_result": projected["result"],
            "effective_draft": projected["draft"], "normalization": projected["normalization"], "qa": []}
        db = self.connect(self.build(bundle))
        raw, effective = db.execute("SELECT raw_draft_json,effective_draft_json FROM candidate_normalizations").fetchone()
        self.assertEqual(json.loads(raw), sample.draft)
        self.assertNotIn("schema_version", json.loads(effective))
        self.assertEqual(json.loads(raw)["visual"], json.loads(effective)["visual"])
        invalid = copy.deepcopy(bundle)
        invalid["results"][1]["normalization"]["visual_fields_unchanged"] = False
        with self.assertRaises(LibraryStoreError): self.build(invalid)


if __name__ == "__main__":
    unittest.main()
