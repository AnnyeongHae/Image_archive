from __future__ import annotations

import copy
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from image_rag_eval.admin_store import AdminStore, AdminStoreError
from image_rag_eval.group_workflow import (
    DEFAULT_IMAGE_APPROVAL_POLICY, blank_group_workflow_decisions, validate_group_workflow_decisions,
)


def fixture():
    spec = {
        "schema_version": "image-group-workflow-spec-1", "run_id": "admin-fixture", "spec_sha256": "frozen-fixture-sha",
        "approval_policy": DEFAULT_IMAGE_APPROVAL_POLICY,
        "items": [{"id": ident, "style_id": ident, "prepared_path": f"../inputs/{ident}.png",
                   "priority": {"tier": 1, "ordinal": index, "rank_index": index, "label": "tier1", "reason": "json", "parse_status": "valid"}}
                  for index, ident in enumerate("ABCDEF")],
        "stage1": {"active_ids": list("ABCDEF"), "archived": [], "alias_lineage": [], "policy": "fixture"},
        "baseline": {"read_only_ids": list("AB"),
                     "image_approvals": [{"id": "A", "approved": True, "memo_text": "old memo"},
                                         {"id": "B", "approved": False, "memo_text": "excluded baseline"}],
                     "groups": [{"candidate_id": "old-ab", "member_ids": list("AB"), "suggested_representative_id": "A"}]},
        "duplicate_candidates": [{"id": "dup-ac", "member_ids": list("AC"), "suggested_representative_id": "A"}],
        "similarity_candidates": [{"id": "sim-attach", "member_ids": list("ABCD"), "baseline_anchor_ids": list("AB"),
                                   "known_negative_pairs": [], "known_positive_pairs": []},
                                  {"id": "sim-ef", "member_ids": list("EF"), "known_negative_pairs": [], "known_positive_pairs": []}],
    }
    seed = blank_group_workflow_decisions(spec)
    seed.update({"reviewer": "human-fixture", "reviewed_at": "2026-09-03T12:00:00Z"})
    seed["duplicate_reviews"][0]["decision"] = "distinct_images"
    seed["similarity_reviews"] = [{"candidate_id": "sim-attach", "decision": "approve_selected", "selected_ids": list("ABD")},
                                   {"candidate_id": "sim-ef", "decision": "keep_separate", "selected_ids": []}]
    seed["image_approvals"] = [{"id": "E", "approved": False, "memo_text": "later"}]
    return spec, seed


class AdminStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "dedicated" / "state.sqlite3"
        self.spec, self.seed = fixture()
        self.store = AdminStore(self.path, self.spec, self.seed)
        self.counter = 0

    def body(self, *, state=None, **extra):
        state = state or self.store.state()
        self.counter += 1
        return {"run_id": self.spec["run_id"], "expected_revision": state["revision"],
                "request_id": f"test-{self.counter}", "stage": state["active_stage"], **extra}

    def advance_to(self, stage):
        while self.store.state()["active_stage"] < stage:
            self.store.advance(self.body())

    def sql_count(self, table):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_seeded_gallery_is_preserved_while_wizard_starts_at_stage1(self):
        state = self.store.state()
        self.assertEqual((state["revision"], state["active_stage"], state["completed_stages"]), (0, 1, []))
        self.assertEqual({row["id"] for row in self.store.gallery()["items"]}, set("ACDF"))
        self.assertEqual(state["last_commit"]["kind"], "seed")
        self.assertEqual(state["summary"]["confirmed_front_count"], 4)
        self.assertFalse(self.store.gallery()["release_eligible"])

    def test_blank_store_has_no_implicit_confirmed_gallery(self):
        blank = AdminStore(Path(self.temp.name) / "blank.sqlite3", self.spec)
        self.assertEqual(blank.gallery()["items"], [])
        with self.assertRaisesRegex(AdminStoreError, "reviewer"):
            blank.advance(self.body(state=blank.state()))

    def test_normalized_seed_is_accepted_but_partial_seed_is_not(self):
        normalized = validate_group_workflow_decisions(self.spec, self.seed)
        another = AdminStore(Path(self.temp.name) / "normalized.sqlite3", self.spec, normalized)
        self.assertEqual(len(another.gallery()["items"]), 4)
        partial = copy.deepcopy(self.seed)
        partial["similarity_reviews"][1]["decision"] = "defer"
        with self.assertRaises(AdminStoreError):
            AdminStore(Path(self.temp.name) / "partial.sqlite3", self.spec, partial)

    def test_stages_must_be_explicitly_acknowledged_in_order(self):
        for stage in (2, 3, 4):
            with self.subTest(stage=stage), self.assertRaises(AdminStoreError) as raised:
                self.store.advance(self.body(stage=stage))
            self.assertEqual(raised.exception.status, 409)
        for expected in (2, 3, 4):
            result = self.store.advance(self.body())
            self.assertEqual(result["active_stage"], expected)
            self.assertEqual(result["completed_stages"], list(range(1, expected)))
        committed = self.store.advance(self.body())
        self.assertEqual(committed["status"], "committed")
        self.assertEqual(committed["completed_stages"], [1, 2, 3, 4])

    def test_wrong_stage_field_edit_is_rejected_without_revision_change(self):
        decisions = self.store.state()["decisions"]
        decisions["image_approvals"].append({"id": "D", "approved": False, "memo_text": "early"})
        before = self.store.state()
        with self.assertRaises(AdminStoreError) as raised:
            self.store.save_draft(self.body(decisions=decisions))
        self.assertEqual(raised.exception.code, "stage_field_violation")
        self.assertEqual(self.store.state(), before)

    def test_deferred_selections_survive_save_restart_and_do_not_approve(self):
        self.advance_to(2)
        decisions = self.store.state()["decisions"]
        decisions["duplicate_reviews"][0].update({"decision": "defer", "selected_ids": list("AC")})
        saved = self.store.save_draft(self.body(decisions=decisions))
        self.assertEqual(saved["decisions"]["duplicate_reviews"][0]["selected_ids"], list("AC"))
        self.assertEqual(saved["decisions"]["similarity_reviews"][0]["selected_ids"], list("ABD"))
        self.assertTrue(all(row["decision"] == "defer" for row in saved["decisions"]["similarity_reviews"]))
        self.assertEqual(saved["summary"]["duplicate_gate_status"], "pending_duplicate_review")
        with self.assertRaises(AdminStoreError) as raised:
            self.store.advance(self.body())
        self.assertEqual(raised.exception.code, "duplicate_gate")
        restarted = AdminStore(self.path, self.spec, self.seed)
        self.assertEqual(restarted.state(), saved)
        self.assertEqual(len(restarted.gallery()["items"]), 4)

    def test_upstream_edit_invalidates_later_acknowledgements_without_erasing_memos(self):
        self.advance_to(4)
        committed = self.store.advance(self.body())
        self.store.rewind(self.body(target_stage=2))
        decisions = self.store.state()["decisions"]
        decisions["duplicate_reviews"][0].update({"decision": "same_image_subset", "selected_ids": list("AC")})
        saved = self.store.save_draft(self.body(decisions=decisions))
        self.assertEqual(saved["completed_stages"], [1])
        self.assertEqual(next(row for row in saved["decisions"]["image_approvals"] if row["id"] == "E")["memo_text"], "later")
        self.assertTrue(all(row["decision"] == "defer" for row in saved["decisions"]["similarity_reviews"]))
        self.assertEqual(saved["last_commit"], committed["last_commit"])
        self.store.advance(self.body())
        with self.assertRaises(AdminStoreError) as raised:
            self.store.advance(self.body())
        self.assertEqual(raised.exception.code, "similarity_gate")

    def test_grouped_image_optout_and_unicode_memo_commit_only_at_stage4(self):
        self.advance_to(4)
        before = self.store.gallery()
        decisions = self.store.state()["decisions"]
        decisions["image_approvals"] += [{"id": "D", "approved": False, "memo_text": "그룹 안 제외"},
                                          {"id": "F", "approved": True, "memo_text": "선택 메모 🧩\n다음 줄"}]
        saved = self.store.save_draft(self.body(decisions=decisions))
        self.assertEqual(self.store.gallery(), before)
        self.assertEqual(saved["status"], "saved")
        committed = self.store.advance(self.body())
        gallery = self.store.gallery()
        self.assertEqual({row["id"] for row in gallery["items"]}, set("ACF"))
        self.assertEqual(next(row for row in gallery["items"] if row["id"] == "F")["memo_text"], "선택 메모 🧩\n다음 줄")
        self.assertIn("D", gallery["groups"][0]["member_ids"])
        restarted = AdminStore(self.path, self.spec)
        self.assertEqual(restarted.state(), committed)
        self.assertEqual(restarted.gallery(), gallery)

    def test_empty_memo_is_valid_and_omitted_choice_cannot_reset_explicit_false(self):
        self.advance_to(4)
        decisions = self.store.state()["decisions"]
        decisions["image_approvals"] = [{"id": "F", "approved": True, "memo_text": ""}]
        saved = self.store.save_draft(self.body(decisions=decisions))
        excluded = next(row for row in saved["decisions"]["image_approvals"] if row["id"] == "E")
        self.assertEqual(excluded, {"id": "E", "approved": False, "memo_text": "later"})
        self.store.advance(self.body())
        self.assertNotIn("E", {row["id"] for row in self.store.gallery()["items"]})

    def test_hidden_image_preferences_survive_changed_duplicate_retention(self):
        self.advance_to(4)
        decisions = self.store.state()["decisions"]
        decisions["image_approvals"].append({"id": "C", "approved": False, "memo_text": "remember me"})
        self.store.save_draft(self.body(decisions=decisions))
        self.store.rewind(self.body(target_stage=2))
        decisions = self.store.state()["decisions"]
        decisions["duplicate_reviews"][0].update({"decision": "same_image_subset", "selected_ids": list("AC")})
        self.store.save_draft(self.body(decisions=decisions))
        self.store.advance(self.body())
        decisions = self.store.state()["decisions"]
        for row in decisions["similarity_reviews"]:
            row.update({"decision": "keep_separate", "selected_ids": []})
        self.store.advance(self.body(decisions=decisions))
        self.store.advance(self.body())
        self.assertNotIn("C", {row["id"] for row in self.store.gallery()["items"]})
        self.assertEqual(next(row for row in self.store.state()["decisions"]["image_approvals"] if row["id"] == "C")["memo_text"], "remember me")

    def test_distinct_images_radio_keeps_checkbox_memory_without_blocking_advance(self):
        self.advance_to(2)
        decisions = self.store.state()["decisions"]
        decisions["duplicate_reviews"][0].update({"decision": "same_image_subset", "selected_ids": list("AC")})
        self.store.save_draft(self.body(decisions=decisions))
        decisions = self.store.state()["decisions"]
        decisions["duplicate_reviews"][0]["decision"] = "distinct_images"
        saved = self.store.save_draft(self.body(decisions=decisions))
        self.assertEqual(saved["decisions"]["duplicate_reviews"][0]["selected_ids"], list("AC"))
        self.assertEqual(saved["summary"]["duplicate_gate_status"], "complete")
        advanced = self.store.advance(self.body())
        self.assertEqual(advanced["active_stage"], 3)
        self.assertIn("C", advanced["summary"]["retained_image_ids"])
        self.assertEqual(advanced["decisions"]["duplicate_reviews"][0]["selected_ids"], list("AC"))

    def test_keep_separate_radio_retains_choice_but_does_not_commit_group_membership(self):
        self.advance_to(3)
        decisions = self.store.state()["decisions"]
        decisions["similarity_reviews"][0]["decision"] = "keep_separate"
        saved = self.store.save_draft(self.body(decisions=decisions))
        self.assertEqual(saved["decisions"]["similarity_reviews"][0]["selected_ids"], list("ABD"))
        self.assertEqual(saved["summary"]["similarity_gate_status"], "complete")
        self.store.advance(self.body())
        self.store.advance(self.body())
        self.assertEqual([row["member_ids"] for row in self.store.gallery()["groups"]], [list("AB")])
        self.assertEqual(self.store.state()["decisions"]["similarity_reviews"][0]["selected_ids"], list("ABD"))

    def test_immutable_baseline_and_unknown_ids_fail_closed(self):
        self.advance_to(4)
        for row in ({"id": "B", "approved": True, "memo_text": "excluded baseline"},
                    {"id": "A", "approved": True, "memo_text": "changed"},
                    {"id": "UNKNOWN", "approved": True, "memo_text": ""}):
            decisions = self.store.state()["decisions"]
            decisions["image_approvals"] = [row]
            before = self.store.state()
            with self.subTest(row=row), self.assertRaises(AdminStoreError):
                self.store.save_draft(self.body(decisions=decisions))
            self.assertEqual(self.store.state(), before)

    def test_duplicate_keeper_and_similarity_anchor_constraints_are_enforced_on_advance(self):
        self.advance_to(2)
        decisions = self.store.state()["decisions"]
        decisions["duplicate_reviews"][0].update({"decision": "same_image_subset", "selected_ids": list("AC")})
        self.store.advance(self.body(decisions=decisions))
        self.assertEqual(self.store.state()["summary"]["retained_image_ids"], list("ABDEF"))
        decisions = self.store.state()["decisions"]
        decisions["similarity_reviews"][0].update({"decision": "approve_selected", "selected_ids": list("AD")})
        decisions["similarity_reviews"][1].update({"decision": "keep_separate", "selected_ids": []})
        with self.assertRaisesRegex(AdminStoreError, "all baseline anchors"):
            self.store.advance(self.body(decisions=decisions))

    def test_idempotent_retry_returns_original_snapshot_after_newer_saves(self):
        body = self.body()
        original = self.store.advance(body)
        self.store.advance(self.body())
        self.assertEqual(self.store.advance(body), original)
        self.assertEqual(self.store.state()["revision"], 2)
        self.assertEqual(self.sql_count("image_admin_events"), 2)
        changed = {**body, "stage": 4}
        with self.assertRaises(AdminStoreError) as raised:
            self.store.advance(changed)
        self.assertEqual(raised.exception.code, "request_id_conflict")
        with self.assertRaises(AdminStoreError):
            self.store.rewind({**body, "target_stage": 1})

    def test_stale_revision_and_concurrent_saves_have_one_winner(self):
        other = AdminStore(self.path, self.spec)
        barrier = threading.Barrier(2)
        bodies = [self.body(), self.body()]
        def attempt(store, body):
            barrier.wait(timeout=5)
            try:
                return store.advance(body)
            except AdminStoreError as exc:
                return exc
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt, store, body) for store, body in zip((self.store, other), bodies)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        error = next(result for result in results if isinstance(result, AdminStoreError))
        self.assertEqual(error.code, "revision_conflict")
        self.assertEqual(self.sql_count("image_admin_events"), 1)
        self.assertEqual(self.store.state()["revision"], 1)

    def test_failure_after_gallery_insert_rolls_back_gallery_revision_and_event(self):
        self.advance_to(4)
        before_state, before_gallery = self.store.state(), self.store.gallery()
        counts = (self.sql_count("image_admin_commits"), self.sql_count("image_admin_events"))
        insert = self.store._insert_commit
        def fail_after_insert(*args):
            insert(*args)
            raise RuntimeError("simulated interruption")
        request = self.body()
        with patch.object(self.store, "_insert_commit", side_effect=fail_after_insert):
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                self.store.advance(request)
        self.assertEqual(self.store.state(), before_state)
        self.assertEqual(self.store.gallery(), before_gallery)
        self.assertEqual((self.sql_count("image_admin_commits"), self.sql_count("image_admin_events")), counts)
        self.assertEqual(self.store.advance(request)["status"], "committed")

    def test_audit_events_and_gallery_commits_reject_update_or_delete(self):
        self.store.advance(self.body())
        with closing(sqlite3.connect(self.path)) as db:
            for query in ("DELETE FROM image_admin_events", "UPDATE image_admin_events SET operation='forged'",
                          "DELETE FROM image_admin_commits", "UPDATE image_admin_commits SET kind='forged'"):
                with self.subTest(query=query), self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    db.execute(query)
                db.rollback()

    def test_audit_insert_failure_rolls_back_updated_state_and_new_gallery(self):
        self.advance_to(4)
        before_state, before_gallery = self.store.state(), self.store.gallery()
        counts = (self.sql_count("image_admin_commits"), self.sql_count("image_admin_events"))
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("""CREATE TRIGGER fixture_fail_final_event BEFORE INSERT ON image_admin_events
                          WHEN NEW.stage=4 BEGIN SELECT RAISE(ABORT, 'simulated audit failure'); END""")
            db.commit()
        request = self.body()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "simulated audit failure"):
            self.store.advance(request)
        self.assertEqual(self.store.state(), before_state)
        self.assertEqual(self.store.gallery(), before_gallery)
        self.assertEqual((self.sql_count("image_admin_commits"), self.sql_count("image_admin_events")), counts)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DROP TRIGGER fixture_fail_final_event")
            db.commit()
        self.assertEqual(self.store.advance(request)["status"], "committed")

    def test_source_guard_runs_for_advance_only_and_not_idempotent_replays(self):
        with patch.object(self.store, "validate_source") as guard:
            self.store.save_draft(self.body(decisions=self.store.state()["decisions"]))
            request = self.body()
            response = self.store.advance(request)
            self.assertEqual(self.store.advance(request), response)
            self.assertEqual(guard.call_count, 1)

    def test_noop_stage4_save_keeps_ack_and_explicit_recommit_is_a_new_commit(self):
        self.advance_to(4)
        first = self.store.advance(self.body())
        saved = self.store.save_draft(self.body(decisions=first["decisions"]))
        self.assertEqual(saved["completed_stages"], [1, 2, 3, 4])
        self.assertEqual(saved["last_commit"], first["last_commit"])
        second = self.store.advance(self.body())
        self.assertNotEqual(second["last_commit"]["id"], first["last_commit"]["id"])
        self.assertEqual(self.sql_count("image_admin_commits"), 3)

    def test_advance_reviewer_and_optional_source_guard(self):
        self.store.validate_source = lambda: (_ for _ in ()).throw(ValueError("source changed"))
        before = self.store.state()
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.store.advance(self.body())
        self.assertEqual(self.store.state(), before)
        decisions = self.store.state()["decisions"]
        decisions["reviewer"] = ""
        self.store.save_draft(self.body(decisions=decisions))
        self.store.validate_source = None
        with self.assertRaises(AdminStoreError) as raised:
            self.store.advance(self.body())
        self.assertEqual(raised.exception.code, "reviewer_required")

    def test_spec_drift_rejected_and_restart_does_not_replace_saved_decisions(self):
        changed = copy.deepcopy(self.spec)
        changed["items"][0]["style_id"] = "forged while keeping declared sha"
        with self.assertRaises(AdminStoreError) as raised:
            AdminStore(self.path, changed, self.seed)
        self.assertEqual(raised.exception.code, "spec_mismatch")
        decisions = self.store.state()["decisions"]
        decisions["reviewer"] = "persisted reviewer"
        saved = self.store.save_draft(self.body(decisions=decisions))
        self.assertEqual(AdminStore(self.path, self.spec, self.seed).state(), saved)

    def test_unknown_properties_wrong_identity_and_wrong_types_are_rejected(self):
        for mutate in (
            lambda body: body.update({"front_review_complete": True}),
            lambda body: body.update({"expected_revision": False}),
            lambda body: body.update({"stage": True}),
            lambda body: body.update({"run_id": "other"}),
            lambda body: body["decisions"].update({"front_review_complete": True}),
            lambda body: body["decisions"].update({"spec_sha256": "other"}),
            lambda body: body["decisions"]["duplicate_reviews"][0].update({"selected_ids": ["not-a-member"]}),
            lambda body: body["decisions"]["duplicate_reviews"][0].update({"remainder_distinct": "true"}),
            lambda body: body["decisions"]["duplicate_reviews"][0].update({"decision": ["defer"]}),
        ):
            body = self.body(decisions=self.store.state()["decisions"])
            mutate(body)
            with self.subTest(body=body), self.assertRaises(AdminStoreError):
                self.store.save_draft(body)
        self.assertEqual(self.store.state()["revision"], 0)

    def test_reads_do_not_create_events_or_change_persisted_run(self):
        before = self.store.state()
        with patch("urllib.request.urlopen", side_effect=AssertionError("No providers")):
            for _ in range(3):
                self.assertEqual(self.store.state(), before)
                self.store.gallery()
        self.assertEqual(self.sql_count("image_admin_events"), 0)
        self.assertEqual(self.sql_count("image_admin_commits"), 1)

    def test_another_subsystems_database_is_not_adopted(self):
        path = Path(self.temp.name) / "other.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE source_records(id INTEGER)")
            db.commit()
        original = path.read_bytes()
        with self.assertRaises(AdminStoreError) as raised:
            AdminStore(path, self.spec)
        self.assertEqual(raised.exception.code, "wrong_database")
        self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
