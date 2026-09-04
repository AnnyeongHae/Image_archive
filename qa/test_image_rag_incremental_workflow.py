from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from image_rag_eval.experiment import digest, json_bytes, run_path, write_json
from image_rag_eval.group_workflow import blank_group_workflow_decisions, validate_group_workflow_decisions
from image_rag_eval.incremental_workflow import (
    POLICY, SCHEMA, REVIEW_DIR, assemble_spec, load_frozen_workflow, import_incremental_decisions,
)
from image_rag_eval.prompt_priority import rank_prompt


def fixture():
    def item(ident):
        sha = digest(ident.encode())
        return {"id": ident, "style_id": ident, "prepared_path": f"../inputs/{sha}.png",
                "prepared_sha256": sha, "source_sha256": sha, "sha256": sha,
                "prompt": f"different prompt {ident}", "priority": {**rank_prompt(ident), "ordinal": ord(ident), "rank_index": ord(ident)}}
    old = {"run_id": "old-fixture", "spec_sha256": "old-sha", "items": [item(i) for i in "ABCD"]}
    baseline = {"stage2_duplicate_gate_status": "complete", "stage3_similarity_gate_status": "complete",
                "stage2_overlay": {"active_ids": list("ABC"), "archived": [{"id": "D", "representative_id": "A"}]},
                "approved_similarity_groups": [
                    {"candidate_id": "smaller", "member_ids": list("AB"), "suggested_representative_id": "A"},
                    {"candidate_id": "larger", "member_ids": list("ABC"), "suggested_representative_id": "A"}],
                "private_front_export_items": [item(i) for i in "AB"],
                "individual_approvals": [{"id": "C", "approved": False, "tags_text": "old excluded memo"}]}
    incoming = {"items": [item(i) for i in "WXYZ"], "embedding_item_ids": list("XYZ"),
                "alias_routes": [{"id": "W", "representative_id": "D", "evidence": ["exact_file"]}]}
    vectors = {"A": [1, 0, 0, 0], "B": [0, 1, 0, 0], "C": [0, 0, 1, 0],
               "X": [.8, 0, 0, .6], "Y": [.8, .6, 0, 0], "Z": [.8, 0, .6, 0]}
    return old, baseline, incoming, vectors


def spec_fixture():
    return assemble_spec(*fixture(), review_run_id="review-fixture", created_at="2026-09-03T12:00:00Z", source_fingerprint="source")


def decisions_for(spec, complete=True):
    decisions = blank_group_workflow_decisions(spec)
    decisions.update({"reviewer": "synthetic-test-only", "reviewed_at": "2026-09-03T12:00:00Z"})
    if complete:
        for row in decisions["duplicate_reviews"]:
            row["decision"] = "distinct_images"
        for row in decisions["similarity_reviews"]:
            row["decision"] = "keep_separate"
    return decisions


class IncrementalWorkflowTests(unittest.TestCase):
    def test_same_baseline_anchor_shows_all_new_candidates_in_one_queue(self):
        spec = spec_fixture()
        self.assertEqual(len(spec["baseline"]["groups"]), 1)
        self.assertEqual(set(spec["baseline"]["groups"][0]["source_candidate_ids"]), {"smaller", "larger"})
        self.assertEqual(len(spec["similarity_candidates"]), 1)
        row = spec["similarity_candidates"][0]
        self.assertEqual(set(row["member_ids"]), set("ABCXYZ"))
        self.assertEqual(set(row["baseline_anchor_ids"]), set("ABC"))
        self.assertTrue(row["candidate_only"])
        self.assertEqual(row["known_positive_pairs"], [])

    def test_alias_follows_human_keeper_and_retention_partition(self):
        spec = spec_fixture()
        self.assertEqual({r["id"]: r["representative_id"] for r in spec["stage1"]["archived"]}, {"D": "A", "W": "A"})
        self.assertEqual(len(spec["stage1"]["active_ids"]), 6)
        self.assertEqual(len(spec["items"]), 8)

    def test_pending_gates_no_front_then_defaults_preserve_old_false(self):
        spec = spec_fixture()
        waiting = validate_group_workflow_decisions(spec, decisions_for(spec, complete=False))
        self.assertEqual(waiting["private_front_export_items"], [])
        ready = validate_group_workflow_decisions(spec, decisions_for(spec))
        self.assertEqual({r["id"] for r in ready["private_front_export_items"]}, set("ABXYZ"))
        old_false = next(r for r in ready["image_approvals"] if r["id"] == "C")
        self.assertFalse(old_false["approved"])
        self.assertEqual(old_false["memo_text"], "old excluded memo")

    def test_grouped_member_optout_and_optional_memo_are_independent(self):
        spec = spec_fixture()
        decisions = decisions_for(spec)
        decisions["similarity_reviews"][0].update({"decision": "approve_selected", "selected_ids": list("ABCXYZ")})
        decisions["image_approvals"] += [{"id": "X", "approved": False, "memo_text": "uncheck"},
                                          {"id": "Y", "approved": True, "memo_text": "스티커 상세페이지에 참고"}]
        normalized = validate_group_workflow_decisions(spec, decisions)
        self.assertEqual({r["id"] for r in normalized["private_front_export_items"]}, set("ABYZ"))
        self.assertEqual(len(normalized["approved_similarity_groups"]), 1)
        self.assertIn("X", normalized["approved_similarity_groups"][0]["member_ids"])
        self.assertEqual(next(r for r in normalized["private_front_export_items"] if r["id"] == "Y")["memo_text"], "스티커 상세페이지에 참고")

    def test_readonly_edit_partial_anchor_and_unsafe_baseline_rejected(self):
        spec = spec_fixture()
        decisions = decisions_for(spec)
        decisions["image_approvals"] = [{"id": "C", "approved": True, "memo_text": ""}]
        with self.assertRaisesRegex(ValueError, "read-only"):
            validate_group_workflow_decisions(spec, decisions)
        decisions = decisions_for(spec)
        decisions["similarity_reviews"][0].update({"decision": "approve_selected", "selected_ids": ["A", "X"]})
        with self.assertRaisesRegex(ValueError, "all baseline anchors"):
            validate_group_workflow_decisions(spec, decisions)
        old, baseline, incoming, vectors = fixture()
        baseline["stage3_similarity_gate_status"] = "pending"
        with self.assertRaisesRegex(ValueError, "complete human baseline"):
            assemble_spec(old, baseline, incoming, vectors, review_run_id="review", created_at="time", source_fingerprint="source")

    def test_no_missing_vector_or_alias_cycle(self):
        for mode in ("missing", "cycle"):
            old, baseline, incoming, vectors = fixture()
            if mode == "missing":
                vectors.pop("X")
            else:
                incoming["alias_routes"][0]["representative_id"] = "W"
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                assemble_spec(old, baseline, incoming, vectors, review_run_id="review", created_at="time", source_fingerprint="source")

    def frozen(self, root):
        spec = spec_fixture()
        directory = run_path(root, spec["run_id"]) / REVIEW_DIR
        directory.mkdir(parents=True)
        for row in spec["items"]:
            preview = directory / row["prepared_path"]
            preview.parent.mkdir(exist_ok=True)
            preview.write_bytes(row["id"].encode())
        evidence = root / "frozen-evidence.txt"
        evidence.write_bytes(b"immutable")
        raw = b'{"fixture":"old-human-decisions"}'
        (directory / "submitted-baseline.raw.json").write_bytes(raw)
        binding = {"schema_version": SCHEMA, "files": [{"path": evidence.name, "sha256": digest(evidence.read_bytes())}],
                   "source_decisions_sha256": digest(raw), "review_spec_sha256": spec["spec_sha256"]}
        receipt = {"schema_version": SCHEMA, "status": "ready", "run_id": spec["run_id"],
                   "spec_sha256": spec["spec_sha256"], "binding_sha256": digest(json_bytes(binding))}
        write_json(directory / "image-group-workflow.spec.json", spec)
        write_json(directory / "source-bindings.json", binding)
        write_json(directory / "build-receipt.json", receipt)
        return spec, directory, evidence

    def test_frozen_import_is_offline_dry_by_default_idempotent_and_renders_memo(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec, directory, _ = self.frozen(root)
            decisions = decisions_for(spec)
            decisions["image_approvals"].append({"id": "X", "approved": True, "memo_text": "개인 영감 <script>"})
            source = root / "submission.json"
            write_json(source, decisions)
            before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            with patch("urllib.request.urlopen", side_effect=AssertionError("no network allowed")):
                result = import_incremental_decisions(root, spec["run_id"], source)
                self.assertEqual(result["front_items"], 5)
                self.assertEqual(before, {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()})
                actual = import_incremental_decisions(root, spec["run_id"], source, apply=True)
                repeated = import_incremental_decisions(root, spec["run_id"], source, apply=True)
                self.assertEqual(actual, repeated)
            rendered = (Path(actual["import_dir"]) / "private-front.html").read_text(encoding="utf-8")
            self.assertIn("개인 영감 &lt;script&gt;", rendered)
            self.assertIn("승인 목록 준비됨 · 5개", rendered)
            # Additive old imports do not invalidate a frozen baseline.
            (root / "unrelated-new-human-import.json").write_text("{}")
            self.assertEqual(load_frozen_workflow(root, spec["run_id"])["spec_sha256"], spec["spec_sha256"])

    def test_frozen_source_or_preview_drift_fails_closed(self):
        for mode in ("source", "preview"):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                spec, directory, evidence = self.frozen(root)
                target = evidence if mode == "source" else directory / spec["items"][0]["prepared_path"]
                target.write_bytes(b"changed")
                with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "changed"):
                    load_frozen_workflow(root, spec["run_id"])


if __name__ == "__main__":
    unittest.main()
