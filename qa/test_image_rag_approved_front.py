from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from image_rag_eval.approved_front import render_approved_front


class ApprovedFrontTests(unittest.TestCase):
    def setUp(self):
        self.export = {
            "status": "ready", "front_review_complete": True,
            "stage2_duplicate_gate_status": "complete", "run_id": "test", "spec_sha256": "test-sha",
            "items": [
                {"id": "a", "style_id": "A", "prepared_path": "../inputs/a.png", "tags_texts": ["스티커"]},
                {"id": "b", "style_id": "B", "prepared_path": "../inputs/b.png", "tags_texts": []},
            ],
        }
        self.groups = {"run_id": "test", "spec_sha256": "test-sha", "groups": [
            {"member_ids": ["a", "b", "unapproved"], "suggested_representative_id": "a"}
        ]}

    def test_approved_only_and_correct_nested_paths(self):
        result = render_approved_front(self.export, self.groups)
        self.assertIn('src="../../../inputs/a.png"', result)
        self.assertIn("전체 2개", result)
        self.assertNotIn('data-item-id="unapproved"', result)
        self.assertIn("<details>", result)
        self.assertIn("스티커", result)

    def test_each_gate_blocks_all_images(self):
        for key, value in (("status", "pending"), ("front_review_complete", False),
                           ("front_review_complete", "true"), ("stage2_duplicate_gate_status", "pending_duplicate_review")):
            with self.subTest(key=key, value=value):
                payload = copy.deepcopy(self.export)
                payload[key] = value
                result = render_approved_front(payload, self.groups)
                self.assertNotIn("<img ", result)
                self.assertIn("0개", result)

    def test_escape_manual_tags(self):
        self.export["items"][0]["tags_texts"] = ['<script>alert("x")</script>']
        result = render_approved_front(self.export, self.groups)
        self.assertNotIn("<script>", result)
        self.assertIn("&lt;script&gt;", result)

    def test_four_stage_export_requires_completed_predecessors(self):
        for key in ("stage3_similarity_gate_status", "stage4_gate_status"):
            with self.subTest(key=key):
                payload = copy.deepcopy(self.export)
                payload[key] = "pending"
                self.assertNotIn("<img ", render_approved_front(payload, self.groups))

    def test_four_stage_approval_needs_no_tags(self):
        self.export["stage3_similarity_gate_status"] = "complete"
        self.export["stage4_gate_status"] = "unlocked"
        for item in self.export["items"]:
            item["tags_texts"] = []
        rendered = render_approved_front(self.export, self.groups)
        self.assertIn('data-item-id="a"', rendered)
        self.assertIn('data-item-id="b"', rendered)

    def test_remote_and_path_escape_rejected(self):
        for path in ("https://example.com/a.png", "../inputs/../../secret.png", "C:/secret.png"):
            self.export["items"][0]["prepared_path"] = path
            with self.assertRaises(ValueError):
                render_approved_front(self.export, self.groups)

    def test_binding_mismatch_rejected(self):
        self.groups["spec_sha256"] = "different"
        with self.assertRaises(ValueError):
            render_approved_front(self.export, self.groups)

    def test_duplicate_items_rejected(self):
        self.export["items"].append(copy.deepcopy(self.export["items"][0]))
        with self.assertRaises(ValueError):
            render_approved_front(self.export, self.groups)

    def test_malformed_groups_raise_contract_error(self):
        for groups in ("bad", ["bad"], [{"member_ids": "bad"}], [{"member_ids": [["a"]]}]):
            self.groups["groups"] = groups
            with self.assertRaises(ValueError):
                render_approved_front(self.export, self.groups)


class ApprovedFrontV3Tests(unittest.TestCase):
    def setUp(self):
        self.export = {
            "status": "ready", "front_review_complete": True,
            "stage2_duplicate_gate_status": "complete",
            "stage3_similarity_gate_status": "complete", "stage4_gate_status": "unlocked",
            "decisions_schema_version": "image-group-workflow-decisions-3",
            "front_approval_policy": "default_retained_images_after_review_v1",
            "run_id": "test", "spec_sha256": "test-sha",
            "items": [{"id": item_id, "style_id": item_id.upper(),
                       "prepared_path": f"../inputs/{item_id}.png", "tags_texts": [], "memo_text": ""}
                      for item_id in ("a", "b", "c", "d")],
        }
        self.groups = {"run_id": "test", "spec_sha256": "test-sha", "groups": [
            {"candidate_id": "small", "member_ids": ["a", "b"], "suggested_representative_id": "a"},
            {"candidate_id": "large", "member_ids": ["a", "b", "c", "d"], "suggested_representative_id": "a"},
        ]}

    def test_contained_group_renders_once_without_losing_images(self):
        rendered = render_approved_front(self.export, self.groups)
        for item_id in ("a", "b", "c", "d"):
            self.assertEqual(rendered.count(f'data-item-id="{item_id}"'), 1)
        self.assertEqual(rendered.count("<details>"), 1)
        self.assertIn("전체 4개", rendered)

    def test_group_membership_cannot_restore_unchecked_representative(self):
        self.export["items"] = [row for row in self.export["items"] if row["id"] != "a"]
        rendered = render_approved_front(self.export, self.groups)
        self.assertNotIn('data-item-id="a"', rendered)
        self.assertIn('data-item-id="b"', rendered)
        self.assertIn("전체 3개", rendered)

    def test_single_approved_group_member_is_rendered_individually(self):
        self.export["items"] = [self.export["items"][2]]
        rendered = render_approved_front(self.export, self.groups)
        self.assertIn('data-item-id="c"', rendered)
        self.assertNotIn("<details>", rendered)
        self.assertEqual(rendered.count("<img "), 1)

    def test_personal_memo_escaped_and_not_duplicated_as_tag(self):
        memo = "<script>memo-only</script>"
        self.export["items"][0].update({"memo_text": memo, "tags_texts": [memo]})
        rendered = render_approved_front(self.export, self.groups)
        self.assertNotIn("<script>", rendered)
        self.assertEqual(rendered.count("&lt;script&gt;memo-only&lt;/script&gt;"), 1)
        self.assertIn("개인 메모 ·", rendered)
        self.assertIn("자동 메타데이터 태그가 아닙니다", rendered)

    def test_incomplete_or_missing_gate_hides_entire_combined_front(self):
        self.export["items"][0]["read_only_baseline"] = True
        for key in ("stage2_duplicate_gate_status", "stage3_similarity_gate_status", "stage4_gate_status", "front_approval_policy"):
            for missing in (False, True):
                with self.subTest(key=key, missing=missing):
                    payload = copy.deepcopy(self.export)
                    if missing:
                        payload.pop(key)
                    else:
                        payload[key] = "pending"
                    rendered = render_approved_front(payload, self.groups)
                    self.assertNotIn("<img ", rendered)
                    self.assertIn("0개", rendered)

    def test_personal_memo_must_be_text(self):
        self.export["items"][0]["memo_text"] = ["not text"]
        with self.assertRaisesRegex(ValueError, "personal memo"):
            render_approved_front(self.export, self.groups)


if __name__ == "__main__":
    unittest.main()
