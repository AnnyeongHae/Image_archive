from __future__ import annotations

import copy
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from image_rag_eval.experiment import digest, json_bytes, prepared_image, run_path, write_json
from image_rag_eval.group_workflow import (
    GROUP_WORKFLOW_DECISION_IMPORTS_DIR,
    GROUP_WORKFLOW_DECISIONS_SCHEMA_VERSION,
    GROUP_WORKFLOW_DECISIONS_V2_SCHEMA_VERSION,
    GROUP_WORKFLOW_DECISIONS_V3_SCHEMA_VERSION,
    DEFAULT_IMAGE_APPROVAL_POLICY,
    GROUP_WORKFLOW_DIR,
    GROUP_WORKFLOW_HTML_FILENAME,
    GROUP_WORKFLOW_SPEC_FILENAME,
    blank_group_workflow_decisions,
    build_group_workflow_artifacts,
    import_group_workflow_decisions,
    validate_group_workflow_decisions,
    canonicalize_approved_groups,
)
from image_rag_eval.human_review import build_human_review_artifacts
from image_rag_eval.human_review_v2 import REVIEW_V2_SPEC_FILENAME, blank_review_labels_v2, build_human_review_v2_artifacts
from image_rag_eval.label_import import import_review_labels
from image_rag_eval.similarity import image_signals


def _unit(*values: float) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


class GroupWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run_id = "group-workflow-fixture"
        self.destination = run_path(self.root, self.run_id)
        (self.destination / "inputs").mkdir(parents=True, exist_ok=True)
        self.fixtures_dir = self.root / "fixtures"
        self.fixtures_dir.mkdir(parents=True, exist_ok=True)
        manifest = {"items": self._fixture_items()}
        write_json(self.destination / "manifest.json", manifest)
        write_json(
            self.destination / "prepared.json",
            {
                "schema_version": "1",
                "complete": True,
                "manifest_sha256": digest(json_bytes(manifest)),
                "at": "2026-09-03T00:00:00Z",
            },
        )
        comparison_dir = self.destination / "comparison-v1"
        comparison_dir.mkdir(parents=True, exist_ok=True)
        write_json(
            comparison_dir / "vectors.json",
            {
                "voyage_image": {
                    "API-049": _unit(1.0, 0.0, 0.0),
                    "DAV490-276": _unit(1.0, 0.0, 0.0),
                    "ERK-1365": _unit(0.993437, math.sqrt(1 - 0.993437**2), 0.0),
                    "FAM-A": _unit(0.0, 1.0, 0.0),
                    "FAM-B": _unit(0.0, 0.97, 0.243),
                    "NEG-C": _unit(0.0, 0.93, 0.368),
                }
            },
        )
        write_json(
            comparison_dir / "evaluation.json",
            {
                "schema_version": "1",
                "completed_arms": ["voyage_image"],
                "evaluations": [{"arm": "voyage_image", "provider": "voyage-multimodal-3.5"}],
            },
        )
        build_human_review_artifacts(self.root, self.run_id, max_pairs=15)
        with mock.patch("image_rag_eval.human_review_v2.review_html_v2", return_value="<!doctype html><html></html>"):
            build_human_review_v2_artifacts(self.root, self.run_id)
        self.spec_v2 = json.loads((self.destination / REVIEW_V2_SPEC_FILENAME).read_text(encoding="utf-8"))
        labels = blank_review_labels_v2(self.spec_v2)
        labels["reviewer"] = "fixture-reviewer"
        labels["reviewed_at"] = "2026-09-03T12:00:00Z"
        self._set_label(labels, "API-049", "DAV490-276", "identical", "delete_duplicate")
        self._set_label(labels, "FAM-A", "FAM-B", "same_visual_family", "group_only")
        self._set_label(labels, "FAM-A", "NEG-C", "unrelated", "keep_separate")
        labels_path = self.root / "group-workflow.labels.json"
        labels_path.write_text(json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8")
        self.imported_labels = import_review_labels(self.root, self.run_id, labels_path, apply=True, minimum_verified_pairs=1)

    def _make_image(self, name: str, background: str, accent: str, *, mode: str, size: tuple[int, int] = (96, 72)) -> Path:
        path = self.fixtures_dir / name
        image = Image.new("RGB", size, background)
        draw = ImageDraw.Draw(image)
        if mode == "diagonal":
            draw.line((0, 0, size[0] - 1, size[1] - 1), fill=accent, width=12)
            draw.line((0, size[1] - 1, size[0] - 1, 0), fill="white", width=4)
        elif mode == "circle":
            draw.ellipse((12, 8, size[0] - 12, size[1] - 8), fill=accent, outline="white", width=3)
        elif mode == "bars":
            for x in range(0, size[0], 14):
                draw.rectangle((x, 0, min(size[0] - 1, x + 6), size[1] - 1), fill=accent)
        elif mode == "triangle":
            draw.polygon([(size[0] // 2, 8), (size[0] - 8, size[1] - 10), (8, size[1] - 10)], fill=accent, outline="white")
        else:
            raise ValueError(mode)
        image.save(path)
        return path

    def _manifest_item(self, item_id: str, style_id: str, source_path: Path, prompt: str, prepared_name: str) -> dict[str, object]:
        prepared_bytes = prepared_image(source_path)
        prepared_rel = Path("inputs") / prepared_name
        (self.destination / prepared_rel).write_bytes(prepared_bytes)
        return {
            "id": item_id,
            "style_id": style_id,
            "catalog_key": f"fixture:{item_id}",
            "path": str(source_path.relative_to(self.root)).replace("\\", "/"),
            "sha256": digest(source_path.read_bytes()),
            "prepared_sha256": digest(prepared_bytes),
            "prepared_path": str(prepared_rel).replace("\\", "/"),
            "prompt": prompt,
            "review_status": "approved",
            "signals": image_signals(source_path),
        }

    def _fixture_items(self) -> list[dict[str, object]]:
        api = self._make_image("api.png", "#23395d", "#f95738", mode="diagonal", size=(80, 120))
        erk = self._make_image("erk.png", "#23395d", "#f95738", mode="diagonal", size=(102, 153))
        fam_a = self._make_image("fam-a.png", "#1f6f8b", "#99f3bd", mode="circle")
        fam_b = self._make_image("fam-b.png", "#1f6f8b", "#99f3bd", mode="bars")
        neg_c = self._make_image("neg-c.png", "#374151", "#f97316", mode="triangle")
        return [
            self._manifest_item("API-049", "API049", api, "same prompt reference", "api-049.png"),
            self._manifest_item("DAV490-276", "DAV490276", api, "same prompt reference", "dav490-276.png"),
            self._manifest_item("ERK-1365", "ERK1365", erk, "same prompt reference", "erk-1365.png"),
            self._manifest_item("FAM-A", "FAMA", fam_a, "family prompt alpha", "fam-a.png"),
            self._manifest_item("FAM-B", "FAMB", fam_b, "family prompt beta", "fam-b.png"),
            self._manifest_item("NEG-C", "NEGC", neg_c, "family prompt gamma", "neg-c.png"),
        ]

    def _pair_row(self, labels: dict[str, object], left_id: str, right_id: str) -> dict[str, object]:
        wanted = {left_id, right_id}
        for row in labels["pairs"]:
            row_ids = {
                row["left"]["id"],
                row["right"]["id"],
            }
            if row_ids == wanted:
                return row
        raise AssertionError(f"pair not found: {left_id}/{right_id}")

    def _set_label(self, labels: dict[str, object], left_id: str, right_id: str, human_label: str, action: str) -> None:
        row = self._pair_row(labels, left_id, right_id)
        row["human_label"] = human_label
        row["human_verified"] = True
        row["action"] = action

    def test_build_workflow_preserves_label_binding_and_active_identity_candidate(self) -> None:
        result = build_group_workflow_artifacts(self.root, self.run_id)
        self.assertEqual(result["source_labels_sha256"], self.imported_labels["labels_sha256"])
        spec = json.loads((self.destination / GROUP_WORKFLOW_DIR / GROUP_WORKFLOW_SPEC_FILENAME).read_text(encoding="utf-8"))
        duplicate_sets = [set(row["member_ids"]) for row in spec["duplicate_candidates"]]
        self.assertIn({"API-049", "ERK-1365"}, duplicate_sets)
        self.assertNotIn({"API-049", "DAV490-276", "ERK-1365"}, duplicate_sets)
        candidate = next(row for row in spec["duplicate_candidates"] if set(row["member_ids"]) == {"API-049", "ERK-1365"})
        self.assertEqual(candidate["suggested_representative_id"], "API-049")
        self.assertEqual(candidate["representative_priority_ids"][0], "API-049")
        self.assertIn("machine_exact_or_high_visual_identity_review_active_only", candidate["evidence"]["basis"])
        lineage = next(row for row in spec["stage1"]["alias_lineage"] if row["representative_id"] == "API-049")
        self.assertIn("DAV490-276", lineage["archived_exact_ids"])
        self.assertTrue((self.destination / GROUP_WORKFLOW_DIR / GROUP_WORKFLOW_HTML_FILENAME).is_file())

    def test_similarity_known_positive_pair_is_preserved_as_two_member_candidate(self) -> None:
        build_group_workflow_artifacts(self.root, self.run_id)
        spec = json.loads((self.destination / GROUP_WORKFLOW_DIR / GROUP_WORKFLOW_SPEC_FILENAME).read_text(encoding="utf-8"))
        self.assertTrue(any(set(row["member_ids"]) == {"FAM-A", "FAM-B"} for row in spec["similarity_candidates"]))

    def test_selected_subset_keeper_recomputes_without_global_suggested_representative(self) -> None:
        spec = {
            "schema_version": "image-group-workflow-spec-1",
            "spec_sha256": "spec-sha",
            "run_id": "fixture",
            "items": [
                {"id": "A", "style_id": "A", "prepared_path": "../inputs/a.png", "priority": {"rank_index": 1, "tier": 1, "label": "tier1", "reason": "json", "parse_status": "valid", "ordinal": 1}},
                {"id": "B", "style_id": "B", "prepared_path": "../inputs/b.png", "priority": {"rank_index": 2, "tier": 1, "label": "tier1", "reason": "json", "parse_status": "valid", "ordinal": 2}},
                {"id": "C", "style_id": "C", "prepared_path": "../inputs/c.png", "priority": {"rank_index": 3, "tier": 2, "label": "tier2", "reason": "kv", "parse_status": "not_json", "ordinal": 3}},
            ],
            "stage1": {"active_ids": ["A", "B", "C"], "archived": [], "alias_lineage": [], "policy": "fixture"},
            "duplicate_candidates": [
                {
                    "id": "dup1",
                    "member_ids": ["A", "B", "C"],
                    "suggested_representative_id": "A",
                    "representative_priority_ids": ["A", "B", "C"],
                    "evidence": {},
                }
            ],
            "similarity_candidates": [],
        }
        decisions = {
            "schema_version": "image-group-workflow-decisions-1",
            "spec_sha256": "spec-sha",
            "run_id": "fixture",
            "reviewer": "tester",
            "reviewed_at": "2026-09-03T12:00:00Z",
            "duplicate_reviews": [{"candidate_id": "dup1", "decision": "same_image_subset", "selected_ids": ["B", "C"], "remainder_distinct": False}],
            "similarity_reviews": [],
            "individual_approvals": [],
            "metadata_optional": True,
            "front_review_complete": False,
        }

        normalized = validate_group_workflow_decisions(spec, decisions)

        self.assertEqual(normalized["stage2_overlay"]["deleted_ids"], ["C"])
        archived = normalized["stage2_overlay"]["archived"][0]
        self.assertEqual(archived["id"], "C")
        self.assertEqual(archived["representative_id"], "B")

    def test_similarity_selection_rejects_prior_negative_pair(self) -> None:
        spec = {
            "schema_version": "image-group-workflow-spec-1",
            "spec_sha256": "spec-sha",
            "run_id": "fixture",
            "items": [
                {"id": "F", "style_id": "F", "prepared_path": "../inputs/f.png", "priority": {"rank_index": 1, "tier": 1, "label": "tier1", "reason": "json", "parse_status": "valid", "ordinal": 1}},
                {"id": "G", "style_id": "G", "prepared_path": "../inputs/g.png", "priority": {"rank_index": 2, "tier": 2, "label": "tier2", "reason": "kv", "parse_status": "not_json", "ordinal": 2}},
                {"id": "H", "style_id": "H", "prepared_path": "../inputs/h.png", "priority": {"rank_index": 3, "tier": 3, "label": "tier3", "reason": "desc", "parse_status": "not_json", "ordinal": 3}},
            ],
            "stage1": {"active_ids": ["F", "G", "H"], "archived": [], "alias_lineage": [], "policy": "fixture"},
            "duplicate_candidates": [],
            "similarity_candidates": [
                {
                    "id": "sim1",
                    "member_ids": ["F", "G", "H"],
                    "representative_priority_ids": ["F", "G", "H"],
                    "candidate_only": True,
                    "evidence": {},
                    "known_positive_pairs": [],
                    "known_negative_pairs": [{"pair_id": "pair-f-h", "left_id": "F", "right_id": "H", "label": "unrelated", "action": "keep_separate"}],
                }
            ],
        }
        decisions = {
            "schema_version": "image-group-workflow-decisions-1",
            "spec_sha256": "spec-sha",
            "run_id": "fixture",
            "reviewer": "tester",
            "reviewed_at": "2026-09-03T12:00:00Z",
            "duplicate_reviews": [],
            "similarity_reviews": [{"candidate_id": "sim1", "decision": "approve_selected", "selected_ids": ["F", "H"], "tags_text": ""}],
            "individual_approvals": [],
            "metadata_optional": True,
            "front_review_complete": False,
        }

        with self.assertRaisesRegex(ValueError, "prior unrelated or same_theme_only pair"):
            validate_group_workflow_decisions(spec, decisions)

    def test_import_blocks_front_until_duplicate_gate_complete_and_writes_private_front_html(self) -> None:
        build_group_workflow_artifacts(self.root, self.run_id)
        spec = json.loads((self.destination / GROUP_WORKFLOW_DIR / GROUP_WORKFLOW_SPEC_FILENAME).read_text(encoding="utf-8"))
        fam_candidate = next(row for row in spec["similarity_candidates"] if set(row["member_ids"]) == {"FAM-A", "FAM-B"})
        duplicate_candidate = next(row for row in spec["duplicate_candidates"] if set(row["member_ids"]) == {"API-049", "ERK-1365"})

        blocked = blank_group_workflow_decisions(spec, schema_version=GROUP_WORKFLOW_DECISIONS_SCHEMA_VERSION)
        blocked["reviewer"] = "tester"
        blocked["reviewed_at"] = "2026-09-03T12:00:00Z"
        blocked["front_review_complete"] = True
        for row in blocked["duplicate_reviews"]:
            row["decision"] = "distinct_images"
        next(row for row in blocked["duplicate_reviews"] if row["candidate_id"] == duplicate_candidate["id"])["decision"] = "defer"
        sim_row = next(row for row in blocked["similarity_reviews"] if row["candidate_id"] == fam_candidate["id"])
        sim_row["decision"] = "approve_selected"
        sim_row["selected_ids"] = ["FAM-A", "FAM-B"]
        blocked_path = self.root / "blocked.decisions.json"
        blocked_path.write_text(json.dumps(blocked, ensure_ascii=False, indent=2), encoding="utf-8")
        blocked_result = import_group_workflow_decisions(self.root, self.run_id, blocked_path, apply=True)
        self.assertEqual(blocked_result["stage2_duplicate_gate_status"], "pending_duplicate_review")
        self.assertEqual(blocked_result["private_front_export_count"], 0)

        ready = copy.deepcopy(blocked)
        next(row for row in ready["duplicate_reviews"] if row["candidate_id"] == duplicate_candidate["id"])["decision"] = "distinct_images"
        ready_path = self.root / "ready.decisions.json"
        ready_path.write_text(json.dumps(ready, ensure_ascii=False, indent=2), encoding="utf-8")
        ready_result = import_group_workflow_decisions(self.root, self.run_id, ready_path, apply=True)
        self.assertEqual(ready_result["stage2_duplicate_gate_status"], "complete")
        self.assertEqual(ready_result["private_front_export_count"], 2)
        import_dir = Path(ready_result["import_dir"])
        self.assertEqual(import_dir.name, ready_result["decisions_sha256"][:24])
        self.assertTrue((import_dir / "private-front.html").is_file())
        front_export = json.loads((import_dir / "private-front-export.json").read_text(encoding="utf-8"))
        self.assertEqual(front_export["status"], "ready")
        self.assertEqual({item["id"] for item in front_export["items"]}, {"FAM-A", "FAM-B"})

    def test_v2_import_uses_stage4_approval_without_a_fifth_gate(self) -> None:
        build_group_workflow_artifacts(self.root, self.run_id)
        spec = json.loads((self.destination / GROUP_WORKFLOW_DIR / GROUP_WORKFLOW_SPEC_FILENAME).read_text(encoding="utf-8"))
        fam_candidate = next(row for row in spec["similarity_candidates"] if set(row["member_ids"]) == {"FAM-A", "FAM-B"})
        decisions = blank_group_workflow_decisions(spec)
        decisions.update({"reviewer": "tester", "reviewed_at": "2026-09-03T12:00:00Z"})
        for row in decisions["duplicate_reviews"]:
            row["decision"] = "distinct_images"
        for row in decisions["similarity_reviews"]:
            row["decision"] = "keep_separate"
        selected = next(row for row in decisions["similarity_reviews"] if row["candidate_id"] == fam_candidate["id"])
        selected.update({"decision": "approve_selected", "selected_ids": ["FAM-A", "FAM-B"]})
        decisions["group_approvals"] = [{"candidate_id": fam_candidate["id"], "approved": True, "tags_text": ""}]
        self.assertNotIn("front_review_complete", decisions)
        decisions_path = self.root / "four-stage.decisions.json"
        decisions_path.write_text(json.dumps(decisions, ensure_ascii=False, indent=2), encoding="utf-8")
        dry = import_group_workflow_decisions(self.root, self.run_id, decisions_path)
        self.assertEqual(dry["writes"], 0)
        self.assertFalse(Path(dry["import_dir"]).exists())
        result = import_group_workflow_decisions(self.root, self.run_id, decisions_path, apply=True)
        self.assertEqual(result["stage3_similarity_gate_status"], "complete")
        self.assertEqual(result["stage4_gate_status"], "unlocked")
        self.assertEqual(result["private_front_export_count"], 2)
        import_dir = Path(result["import_dir"])
        front = json.loads((import_dir / "private-front-export.json").read_text(encoding="utf-8"))
        self.assertEqual(front["decisions_schema_version"], GROUP_WORKFLOW_DECISIONS_V2_SCHEMA_VERSION)
        self.assertEqual(front["stage3_similarity_gate_status"], "complete")
        self.assertEqual({row["id"] for row in front["items"]}, {"FAM-A", "FAM-B"})
        self.assertTrue(all(row["tags_texts"] == [] for row in front["items"]))
        self.assertIn("FAM-A", (import_dir / "private-front.html").read_text(encoding="utf-8"))


class FourStageValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = {
            "schema_version": "image-group-workflow-spec-1",
            "spec_sha256": "frozen-spec-sha",
            "run_id": "frozen-fixture",
            "items": [
                {
                    "id": item_id, "style_id": item_id, "prepared_path": f"../inputs/{item_id}.png",
                    "priority": {"rank_index": index, "tier": 1, "label": "tier1", "reason": "json", "parse_status": "valid", "ordinal": index},
                }
                for index, item_id in enumerate(["A", "B", "C", "D", "E"], 1)
            ],
            "stage1": {"active_ids": ["A", "B", "C", "D", "E"], "archived": [], "alias_lineage": [], "policy": "fixture"},
            "duplicate_candidates": [],
            "similarity_candidates": [
                {"id": "sim1", "member_ids": ["A", "B"], "known_negative_pairs": [], "known_positive_pairs": []},
                {"id": "sim2", "member_ids": ["C", "D"], "known_negative_pairs": [], "known_positive_pairs": []},
            ],
        }
        self.decisions = blank_group_workflow_decisions(self.spec)
        self.decisions.update({"reviewer": "tester", "reviewed_at": "2026-09-03T12:00:00Z"})
        self.decisions["similarity_reviews"] = [
            {"candidate_id": "sim1", "decision": "approve_selected", "selected_ids": ["A", "B"], "tags_text": "old stage3 note"},
            {"candidate_id": "sim2", "decision": "keep_separate", "selected_ids": [], "tags_text": ""},
        ]

    def validate(self) -> dict[str, object]:
        return validate_group_workflow_decisions(self.spec, self.decisions)

    def approve_group(self, *, tags: str = "") -> None:
        self.decisions["group_approvals"] = [{"candidate_id": "sim1", "approved": True, "tags_text": tags}]

    def add_duplicate_candidate(self) -> None:
        self.spec["duplicate_candidates"] = [{
            "id": "dup1", "member_ids": ["A", "B"],
            "suggested_representative_id": "A", "representative_priority_ids": ["A", "B"],
        }]

    def test_default_template_has_four_stage_contract_and_legacy_is_explicit(self) -> None:
        self.assertEqual(self.decisions["schema_version"], GROUP_WORKFLOW_DECISIONS_V2_SCHEMA_VERSION)
        self.assertNotIn("front_review_complete", self.decisions)
        self.assertEqual(self.decisions["group_approvals"], [])
        legacy = blank_group_workflow_decisions(self.spec, schema_version=GROUP_WORKFLOW_DECISIONS_SCHEMA_VERSION)
        self.assertIs(legacy["front_review_complete"], False)
        self.assertNotIn("group_approvals", legacy)

    def test_stage3_membership_alone_never_approves_front_or_invents_tags(self) -> None:
        result = self.validate()
        self.assertEqual(result["stage4_gate_status"], "unlocked")
        self.assertEqual(result["private_front_export_items"], [])
        self.assertEqual(len(result["approved_similarity_groups"]), 1)
        self.assertIs(result["approved_similarity_groups"][0]["stage4_approved"], False)
        self.assertEqual(result["approved_similarity_groups"][0]["tags_text"], "")
        self.assertEqual(result["similarity_reviews"][0]["tags_text"], "old stage3 note")

    def test_tagless_stage4_group_approval_immediately_reaches_front(self) -> None:
        self.approve_group()
        result = self.validate()
        self.assertEqual(result["private_front_export_status"], "ready")
        self.assertEqual({row["id"] for row in result["private_front_export_items"]}, {"A", "B"})
        self.assertTrue(all(row["tags_texts"] == [] for row in result["private_front_export_items"]))
        self.assertNotIn("front_review_complete", self.decisions)

    def test_only_explicit_stage4_group_and_individual_approvals_are_exported(self) -> None:
        self.approve_group(tags="스티커, magazine")
        self.decisions["individual_approvals"] = [
            {"id": "C", "approved": True, "tags_text": ""},
            {"id": "D", "approved": False, "tags_text": "not approved"},
        ]
        by_id = {row["id"]: row for row in self.validate()["private_front_export_items"]}
        self.assertEqual(set(by_id), {"A", "B", "C"})
        self.assertEqual(by_id["A"]["tags_texts"], ["스티커, magazine"])
        self.assertEqual(by_id["C"]["tags_texts"], [])

    def test_unresolved_similarity_blocks_stage4_even_with_old_front_flag(self) -> None:
        self.approve_group()
        self.decisions["front_review_complete"] = True
        self.decisions["similarity_reviews"][1]["decision"] = "defer"
        result = self.validate()
        self.assertEqual(result["unresolved_similarity_candidate_ids"], ["sim2"])
        self.assertEqual(result["stage4_gate_status"], "blocked_pending_similarity_review")
        self.assertEqual(result["private_front_export_items"], [])
        self.assertIs(result["front_review_complete"], False)
        self.decisions["similarity_reviews"].pop()
        self.assertEqual(self.validate()["unresolved_similarity_candidate_ids"], ["sim2"])

    def test_explicit_keep_separate_completes_review_without_grouping_members(self) -> None:
        self.approve_group()
        result = self.validate()
        self.assertEqual(result["stage3_similarity_gate_status"], "complete")
        self.assertEqual(result["similarity_reviews"][1]["decision"], "keep_separate")
        self.assertEqual({row["id"] for row in result["private_front_export_items"]}, {"A", "B"})

    def test_duplicate_gate_blocks_stage4_regardless_of_similarity_completion(self) -> None:
        self.add_duplicate_candidate()
        self.approve_group()
        result = self.validate()
        self.assertEqual(result["stage4_gate_status"], "blocked_pending_duplicate_review")
        self.assertEqual(result["private_front_export_items"], [])
        self.assertEqual(len(result["approved_similarity_groups_pending_gate"]), 1)

    def test_duplicate_reduced_singleton_does_not_require_similarity_review(self) -> None:
        self.add_duplicate_candidate()
        self.decisions["duplicate_reviews"] = [{
            "candidate_id": "dup1", "decision": "same_image_subset", "selected_ids": ["A", "B"], "remainder_distinct": False,
        }]
        self.decisions["similarity_reviews"] = [self.decisions["similarity_reviews"][1]]
        self.decisions["individual_approvals"] = [{"id": "A", "approved": True, "tags_text": ""}]
        result = self.validate()
        self.assertEqual(result["skipped_similarity_candidate_ids"], ["sim1"])
        self.assertEqual(result["stage3_similarity_gate_status"], "complete")
        self.assertEqual([row["id"] for row in result["private_front_export_items"]], ["A"])

    def test_stage4_group_must_reference_explicit_stage3_group(self) -> None:
        self.decisions["group_approvals"] = [{"candidate_id": "sim2", "approved": True, "tags_text": ""}]
        with self.assertRaisesRegex(ValueError, "requires an approved stage3 selected group"):
            self.validate()

    def test_grouped_members_cannot_be_approved_as_individuals(self) -> None:
        self.decisions["individual_approvals"] = [{"id": "A", "approved": False, "tags_text": ""}]
        with self.assertRaisesRegex(ValueError, "only retained ungrouped ids"):
            self.validate()

    def test_v2_keeps_prior_negative_and_inactive_guards(self) -> None:
        self.spec["similarity_candidates"][0]["known_negative_pairs"] = [{"left_id": "A", "right_id": "B", "label": "unrelated"}]
        with self.assertRaisesRegex(ValueError, "prior unrelated or same_theme_only pair"):
            self.validate()
        self.spec["similarity_candidates"][0]["known_negative_pairs"] = []
        self.spec["stage1"]["active_ids"].remove("B")
        with self.assertRaisesRegex(ValueError, "logically deleted or inactive ids"):
            self.validate()

    def test_v2_rejects_malformed_approvals_and_draft_envelope(self) -> None:
        self.approve_group()
        valid = copy.deepcopy(self.decisions)
        for mutation in [
            {"group_approvals": None},
            {"group_approvals": [{"candidate_id": "sim1", "approved": "true"}]},
            {"group_approvals": [valid["group_approvals"][0], valid["group_approvals"][0]]},
            {"schema_version": "image-group-workflow-draft-2"},
            {"spec_sha256": "other-spec"},
            {"run_id": "other-run"},
        ]:
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValueError):
                    validate_group_workflow_decisions(self.spec, {**valid, **mutation})

    def test_keep_separate_rejects_selected_members(self) -> None:
        self.decisions["similarity_reviews"][1]["selected_ids"] = ["C"]
        with self.assertRaisesRegex(ValueError, "keep_separate similarity review must not include selected ids"):
            self.validate()

    def test_legacy_import_semantics_are_not_reinterpreted_as_v2(self) -> None:
        legacy = copy.deepcopy(self.decisions)
        legacy["schema_version"] = GROUP_WORKFLOW_DECISIONS_SCHEMA_VERSION
        legacy.pop("group_approvals")
        legacy["similarity_reviews"][1]["decision"] = "defer"
        legacy["front_review_complete"] = True
        result = validate_group_workflow_decisions(self.spec, legacy)
        self.assertEqual(result["schema_version"], GROUP_WORKFLOW_DECISIONS_SCHEMA_VERSION)
        self.assertNotIn("stage3_similarity_gate_status", result)
        self.assertNotIn("group_approvals", result)
        self.assertEqual({row["id"] for row in result["private_front_export_items"]}, {"A", "B"})


class ImageApprovalV3Tests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = FourStageValidationTests()
        fixture.setUp()
        self.spec = fixture.spec
        self.spec["approval_policy"] = DEFAULT_IMAGE_APPROVAL_POLICY
        self.spec["baseline"] = {
            "read_only_ids": ["A", "B"],
            "image_approvals": [{"id": "A", "approved": True, "memo_text": "original memo"},
                                {"id": "B", "approved": False, "memo_text": ""}],
            "groups": [{"candidate_id": "baseline-ab", "member_ids": ["A", "B"],
                        "suggested_representative_id": "A", "memo_text": "baseline group note"}],
        }
        self.spec["similarity_candidates"] = [
            {"id": "attach", "member_ids": ["A", "B", "C", "D"], "baseline_anchor_ids": ["A", "B"],
             "known_negative_pairs": [], "known_positive_pairs": []},
            {"id": "new-only", "member_ids": ["D", "E"], "known_negative_pairs": [], "known_positive_pairs": []},
        ]
        self.decisions = blank_group_workflow_decisions(self.spec)
        self.decisions.update({"reviewer": "tester", "reviewed_at": "2026-09-03T12:00:00Z"})
        self.decisions["similarity_reviews"] = [
            {"candidate_id": "attach", "decision": "approve_selected", "selected_ids": ["A", "B", "C"], "memo_text": "new group note"},
            {"candidate_id": "new-only", "decision": "keep_separate", "selected_ids": []},
        ]

    def validate(self) -> dict:
        return validate_group_workflow_decisions(self.spec, self.decisions)

    def test_v3_requires_spec_optin_and_leaves_legacy_default_v2(self) -> None:
        self.assertEqual(self.decisions["schema_version"], GROUP_WORKFLOW_DECISIONS_V3_SCHEMA_VERSION)
        self.assertNotIn("individual_approvals", self.decisions)
        self.assertNotIn("front_review_complete", self.decisions)
        self.spec.pop("approval_policy")
        self.assertEqual(blank_group_workflow_decisions(self.spec)["schema_version"], GROUP_WORKFLOW_DECISIONS_V2_SCHEMA_VERSION)
        with self.assertRaisesRegex(ValueError, "explicit source-spec opt-in"):
            self.validate()

    def test_after_gates_new_images_default_true_but_baseline_unchecked_stays_false(self) -> None:
        result = self.validate()
        self.assertEqual({row["id"] for row in result["private_front_export_items"]}, {"A", "C", "D", "E"})
        self.assertEqual(len(result["image_approvals"]), 5)
        self.assertFalse(next(row for row in result["image_approvals"] if row["id"] == "B")["approved"])
        self.assertEqual(result["baseline_approved_image_ids"], ["A"])
        self.assertEqual(len(result["approved_similarity_groups"]), 1)
        self.assertEqual(result["approved_similarity_groups"][0]["member_ids"], ["A", "B", "C"])
        self.assertEqual(result["approved_similarity_groups"][0]["source_candidate_ids"], ["attach", "baseline-ab"])

    def test_uncheck_group_member_and_optional_image_memo_are_independent(self) -> None:
        self.decisions["image_approvals"] = [{"id": "C", "approved": False, "memo_text": "keep for later"},
                                             {"id": "D", "approved": True, "memo_text": "landscape idea"}]
        result = self.validate()
        by_id = {row["id"]: row for row in result["private_front_export_items"]}
        self.assertEqual(set(by_id), {"A", "D", "E"})
        self.assertEqual(by_id["D"]["memo_text"], "landscape idea")
        self.assertEqual(by_id["D"]["tags_texts"], ["landscape idea"])
        self.assertEqual(by_id["E"]["memo_text"], "")
        self.assertIn("C", result["approved_similarity_groups"][0]["member_ids"])
        self.assertFalse(result["automatic_metadata_tags"])

    def test_default_approval_is_blocked_until_both_predecessors_finish(self) -> None:
        self.decisions["similarity_reviews"][1]["decision"] = "defer"
        result = self.validate()
        self.assertEqual(result["private_front_export_items"], [])
        self.assertEqual(result["stage4_gate_status"], "blocked_pending_similarity_review")
        self.decisions["similarity_reviews"][1]["decision"] = "keep_separate"
        self.spec["duplicate_candidates"] = [{"id": "dup", "member_ids": ["A", "C"], "suggested_representative_id": "A"}]
        result = self.validate()
        self.assertEqual(result["private_front_export_items"], [])
        self.assertEqual(result["stage4_gate_status"], "blocked_pending_duplicate_review")

    def test_prior_explicit_unchecked_seed_survives_default_and_can_be_explicitly_changed(self) -> None:
        self.spec["initial_image_approvals"] = [{"id": "E", "approved": False, "memo_text": "prior choice"}]
        self.assertNotIn("E", {row["id"] for row in self.validate()["private_front_export_items"]})
        self.decisions["image_approvals"] = [{"id": "E", "approved": True, "memo_text": "new personal note"}]
        self.assertIn("E", {row["id"] for row in self.validate()["private_front_export_items"]})

    def test_readonly_baseline_approval_and_memo_cannot_be_changed(self) -> None:
        for choice in ({"id": "A", "approved": False, "memo_text": "original memo"},
                       {"id": "A", "approved": True, "memo_text": "changed"},
                       {"id": "B", "approved": True, "memo_text": ""}):
            with self.subTest(choice=choice):
                self.decisions["image_approvals"] = [choice]
                with self.assertRaisesRegex(ValueError, "read-only baseline"):
                    self.validate()
        self.decisions["image_approvals"] = copy.deepcopy(self.spec["baseline"]["image_approvals"])
        self.validate()

    def test_baseline_keeper_wins_over_new_json_prompt_priority(self) -> None:
        self.spec["items"][0]["priority"]["tier"] = 4
        self.spec["duplicate_candidates"] = [{"id": "dup", "member_ids": ["A", "C"], "suggested_representative_id": "C"}]
        self.decisions["duplicate_reviews"] = [{"candidate_id": "dup", "decision": "same_image_subset", "selected_ids": ["A", "C"], "remainder_distinct": False}]
        self.decisions["similarity_reviews"][0] = {"candidate_id": "attach", "decision": "keep_separate", "selected_ids": []}
        result = self.validate()
        self.assertEqual(result["stage2_overlay"]["deleted_ids"], ["C"])
        self.assertEqual(result["applied_duplicate_candidates"][0]["keep_id"], "A")

    def test_human_duplicate_review_cannot_merge_two_existing_keepers(self) -> None:
        self.spec["duplicate_candidates"] = [{"id": "dup", "member_ids": ["A", "B", "C"], "suggested_representative_id": "A"}]
        self.decisions["duplicate_reviews"] = [{"candidate_id": "dup", "decision": "same_image_subset", "selected_ids": ["A", "B", "C"], "remainder_distinct": False}]
        with self.assertRaisesRegex(ValueError, "multiple read-only baseline keepers"):
            self.validate()

    def test_overlap_cannot_reuse_an_already_removed_image_as_later_keeper(self) -> None:
        self.spec["duplicate_candidates"] = [
            {"id": "first", "member_ids": ["A", "C"], "suggested_representative_id": "A"},
            {"id": "later", "member_ids": ["C", "D"], "suggested_representative_id": "C"},
        ]
        self.decisions["duplicate_reviews"] = [
            {"candidate_id": "first", "decision": "same_image_subset", "selected_ids": ["A", "C"], "remainder_distinct": False},
            {"candidate_id": "later", "decision": "same_image_subset", "selected_ids": ["C", "D"], "remainder_distinct": False},
        ]
        with self.assertRaisesRegex(ValueError, "overlapping duplicate selections"):
            self.validate()

    def test_partial_baseline_anchor_selection_is_rejected(self) -> None:
        self.decisions["similarity_reviews"][0]["selected_ids"] = ["A", "C"]
        with self.assertRaisesRegex(ValueError, "all baseline anchors or none"):
            self.validate()
        self.decisions["similarity_reviews"][0]["selected_ids"] = ["A", "B"]
        with self.assertRaisesRegex(ValueError, "at least one new editable"):
            self.validate()
        self.decisions["similarity_reviews"][0]["selected_ids"] = ["C", "D"]
        self.assertEqual(len(self.validate()["approved_similarity_groups"]), 2)

    def test_complete_anchor_attachment_preserves_other_partially_overlapping_group(self) -> None:
        self.spec["baseline"]["read_only_ids"].append("E")
        self.spec["baseline"]["image_approvals"].append({"id": "E", "approved": True, "memo_text": ""})
        self.spec["baseline"]["groups"].append({"candidate_id": "baseline-be", "member_ids": ["B", "E"]})
        self.spec["similarity_candidates"] = self.spec["similarity_candidates"][:1]
        self.decisions["similarity_reviews"] = self.decisions["similarity_reviews"][:1]
        result = self.validate()
        self.assertEqual({frozenset(row["member_ids"]) for row in result["approved_similarity_groups"]},
                         {frozenset("ABC"), frozenset("BE")})

    def test_source_candidate_cannot_declare_partial_old_group_as_anchor(self) -> None:
        self.spec["similarity_candidates"][0].update({"member_ids": ["A", "C", "D"], "baseline_anchor_ids": ["A"]})
        self.decisions["similarity_reviews"][0]["selected_ids"] = ["A", "C"]
        with self.assertRaisesRegex(ValueError, "one complete baseline group"):
            self.validate()

    def test_no_new_survivor_candidate_does_not_reopen_existing_group_review(self) -> None:
        self.spec["similarity_candidates"][0]["member_ids"] = ["A", "B", "C"]
        self.spec["duplicate_candidates"] = [{"id": "dup", "member_ids": ["A", "C"], "suggested_representative_id": "A"}]
        self.decisions["duplicate_reviews"] = [{"candidate_id": "dup", "decision": "same_image_subset", "selected_ids": ["A", "C"], "remainder_distinct": False}]
        self.decisions["similarity_reviews"][0] = {"candidate_id": "attach", "decision": "defer", "selected_ids": []}
        result = self.validate()
        self.assertEqual(result["stage3_similarity_gate_status"], "complete")
        self.assertIn("attach", result["skipped_similarity_candidate_ids"])

    def test_baseline_contract_requires_complete_prior_choices(self) -> None:
        self.spec["baseline"]["image_approvals"].pop()
        with self.assertRaisesRegex(ValueError, "exactly cover"):
            self.validate()

    def test_v3_does_not_silently_ignore_old_approval_fields_or_bad_memo(self) -> None:
        self.decisions["individual_approvals"] = [{"id": "E", "approved": False}]
        with self.assertRaisesRegex(ValueError, "not legacy"):
            self.validate()
        self.decisions.pop("individual_approvals")
        self.decisions["image_approvals"] = [{"id": "E", "approved": True, "memo_text": "a" * 8001}]
        with self.assertRaisesRegex(ValueError, "8000 UTF-8 bytes"):
            self.validate()

    def test_normalized_v3_is_idempotent_and_source_spec_unchanged(self) -> None:
        before = copy.deepcopy(self.spec)
        result = self.validate()
        self.assertEqual(validate_group_workflow_decisions(self.spec, result), result)
        self.assertEqual(self.spec, before)


class ApprovedGroupCanonicalizationTests(unittest.TestCase):
    def test_named_contained_group_collapses_without_image_deletion_or_memo_loss(self) -> None:
        groups = [
            {"candidate_id": "e9f618cd4cada4786ad10b93", "member_ids": ["CASE-074", "CASE-075"], "memo_text": "small-group note"},
            {"candidate_id": "96c2794beccc71854fc0f76b", "member_ids": ["CASE-074", "CASE-075", "CASE-076", "CASE-077"], "tags_text": "old category note"},
        ]
        before = copy.deepcopy(groups)
        result = canonicalize_approved_groups(groups)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["candidate_id"], "96c2794beccc71854fc0f76b")
        self.assertEqual(result[0]["member_ids"], groups[1]["member_ids"])
        self.assertEqual(set(result[0]["source_candidate_ids"]), {row["candidate_id"] for row in groups})
        self.assertEqual({row["memo_text"] for row in result[0]["source_group_memos"]}, {"small-group note", "old category note"})
        self.assertEqual(groups, before)
        self.assertEqual(canonicalize_approved_groups(result), result)

    def test_partial_overlap_never_becomes_one_group(self) -> None:
        groups = [{"candidate_id": "one", "member_ids": ["A", "B", "C"]},
                  {"candidate_id": "two", "member_ids": ["B", "C", "D"]}]
        result = canonicalize_approved_groups(groups)
        self.assertEqual(len(result), 2)
        self.assertFalse(any(set(row["member_ids"]) == {"A", "B", "C", "D"} for row in result))

    def test_equal_member_sets_prefer_locked_baseline_and_keep_both_sources(self) -> None:
        result = canonicalize_approved_groups([
            {"candidate_id": "a-new", "member_ids": ["B", "A"]},
            {"candidate_id": "z-old", "member_ids": ["A", "B"], "baseline_group": True}])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["candidate_id"], "z-old")
        self.assertEqual(result[0]["source_candidate_ids"], ["a-new", "z-old"])


if __name__ == "__main__":
    unittest.main()
