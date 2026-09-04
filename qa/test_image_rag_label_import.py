from __future__ import annotations

import io
import json
import math
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import build_image_similarity_review as build_review_cli
import import_image_similarity_labels as import_labels_cli
from image_rag_eval.experiment import digest, json_bytes, prepared_image, run_path, write_json
from image_rag_eval.action_planning import build_action_plan
from image_rag_eval.human_review import build_human_review_artifacts, blank_review_labels, load_review_source, build_review_spec
from image_rag_eval.human_review_v2 import build_human_review_v2_artifacts, blank_review_labels_v2, REVIEW_V2_SPEC_FILENAME
from image_rag_eval.label_import import import_review_labels, load_bound_review_spec, load_bound_review_spec_v2
from image_rag_eval.similarity import image_signals


def _unit(*values: float) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


class LabelImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run_id = "voyage-review-fixture"
        self.destination = run_path(self.root, self.run_id)
        (self.destination / "inputs").mkdir(parents=True, exist_ok=True)
        self.fixtures_dir = self.root / "fixtures"
        self.fixtures_dir.mkdir(parents=True, exist_ok=True)
        self.manifest = {"items": self._fixture_items()}
        write_json(self.destination / "manifest.json", self.manifest)
        write_json(
            self.destination / "prepared.json",
            {
                "schema_version": "1",
                "complete": True,
                "manifest_sha256": digest(json_bytes(self.manifest)),
                "at": "2026-09-03T00:00:00+00:00",
            },
        )
        comparison_dir = self.destination / "comparison-v1"
        comparison_dir.mkdir(parents=True, exist_ok=True)
        write_json(
            comparison_dir / "vectors.json",
            {
                "voyage_image": {
                    "asset-exact-a": _unit(1.0, 0.0, 0.0),
                    "asset-exact-b": _unit(1.0, 0.0, 0.0),
                    "asset-prompt-c": _unit(0.0, 1.0, 0.0),
                    "asset-prompt-d": _unit(0.0, 0.0, 1.0),
                    "asset-boundary-e": _unit(0.90, math.sqrt(1 - 0.90**2), 0.0),
                    "asset-boundary-f": _unit(0.85, math.sqrt(1 - 0.85**2), 0.0),
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
        self.spec = json.loads((self.destination / "human-similarity-review.spec.json").read_text(encoding="utf-8"))
        with mock.patch("image_rag_eval.human_review_v2.review_html_v2", return_value="<!doctype html><html></html>"):
            build_human_review_v2_artifacts(self.root, self.run_id)
        self.spec_v2 = json.loads((self.destination / REVIEW_V2_SPEC_FILENAME).read_text(encoding="utf-8"))
        self.labels_path = self.root / "downloaded-labels.json"

    def _make_image(self, name: str, background: str, accent: str, *, mode: str) -> Path:
        path = self.fixtures_dir / name
        image = Image.new("RGB", (96, 72), background)
        draw = ImageDraw.Draw(image)
        if mode == "diagonal":
            draw.line((0, 0, 95, 71), fill=accent, width=12)
            draw.line((0, 71, 95, 0), fill="white", width=6)
        elif mode == "circle":
            draw.ellipse((14, 10, 82, 58), fill=accent, outline="white", width=3)
        elif mode == "bars":
            for x in range(0, 96, 16):
                draw.rectangle((x, 0, min(95, x + 7), 71), fill=accent)
        elif mode == "triangle":
            draw.polygon([(48, 8), (88, 62), (8, 62)], fill=accent, outline="white")
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
            "embedding_prompt": prompt,
            "review_status": "approved",
            "signals": image_signals(source_path),
        }

    def _fixture_items(self) -> list[dict[str, object]]:
        exact = self._make_image("exact.png", "#23395d", "#f95738", mode="diagonal")
        prompt_c = self._make_image("prompt-c.png", "#1f6f8b", "#99f3bd", mode="circle")
        prompt_d = self._make_image("prompt-d.png", "#6c4ab6", "#ffd93d", mode="bars")
        boundary_e = self._make_image("boundary-e.png", "#374151", "#f97316", mode="diagonal")
        boundary_f = self._make_image("boundary-f.png", "#111827", "#60a5fa", mode="triangle")
        return [
            self._manifest_item("asset-exact-a", "CASE001", exact, "warm diagonal poster", "asset-exact-a.png"),
            self._manifest_item("asset-exact-b", "CASE002", exact, "warm diagonal poster duplicate", "asset-exact-b.png"),
            self._manifest_item("asset-prompt-c", "CASE088", prompt_c, "same prompt challenge", "asset-prompt-c.png"),
            self._manifest_item("asset-prompt-d", "CASE089", prompt_d, "same prompt challenge", "asset-prompt-d.png"),
            self._manifest_item("asset-boundary-e", "BST001", boundary_e, "boundary candidate one", "asset-boundary-e.png"),
            self._manifest_item("asset-boundary-f", "BST002", boundary_f, "boundary candidate two", "asset-boundary-f.png"),
        ]

    def _labels(self) -> dict[str, object]:
        labels = blank_review_labels(self.spec)
        labels["reviewer"] = "fixture-reviewer"
        labels["reviewed_at"] = "2026-09-03T12:00:00Z"
        labels["pairs"][0]["human_label"] = "unsure"
        labels["pairs"][0]["human_verified"] = False
        labels["pairs"][1]["human_label"] = "near_duplicate"
        labels["pairs"][1]["human_verified"] = True
        labels["pairs"][1]["dimensions"]["composition"] = "same"
        labels["pairs"][2]["human_label"] = "same_visual_family"
        labels["pairs"][2]["human_verified"] = True
        labels["pairs"][2]["dimensions"]["style"] = "similar"
        return labels

    def _write_labels(self, payload: dict[str, object]) -> None:
        self.labels_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _pair_row(self, spec: dict[str, object], left_id: str, right_id: str) -> dict[str, object]:
        wanted = {left_id, right_id}
        for pair in spec["pairs"]:
            if {pair["left"]["id"], pair["right"]["id"]} == wanted:
                return pair
        raise KeyError((left_id, right_id))

    def _labels_v2(self) -> dict[str, object]:
        labels = blank_review_labels_v2(self.spec_v2)
        labels["reviewer"] = "fixture-reviewer"
        labels["reviewed_at"] = "2026-09-03T12:00:00Z"
        return labels

    def test_load_bound_review_spec_validates_self_hash_and_current_binding(self) -> None:
        spec, source = load_bound_review_spec(self.root, self.run_id)
        rebuilt = build_review_spec(load_review_source(self.root, self.run_id), max_pairs=15)
        self.assertEqual(spec["review_spec_sha256"], digest(json_bytes({k: v for k, v in spec.items() if k != "review_spec_sha256"})))
        self.assertEqual(source["manifest_sha256"], spec["source_manifest_sha256"])
        self.assertEqual(spec["sampling_seed"], rebuilt["sampling_seed"])

    def test_dry_run_returns_threshold_histogram_without_writing(self) -> None:
        labels = self._labels()
        self._write_labels(labels)

        result = import_review_labels(self.root, self.run_id, self.labels_path, minimum_verified_pairs=2)

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["verified_pairs"], 2)
        self.assertEqual(result["unresolved_pairs"], 1)
        self.assertEqual(result["writes"], 0)
        self.assertFalse((self.destination / "human-label-reviews").exists())

    def test_load_bound_review_spec_v2_uses_current_ui_contract(self) -> None:
        spec_v2, source = load_bound_review_spec_v2(self.root, self.run_id)

        self.assertEqual(spec_v2["schema_version"], "image-similarity-review-spec-2")
        self.assertEqual(spec_v2["run_id"], self.run_id)
        self.assertEqual(source["manifest_sha256"], spec_v2["source_manifest_sha256"])

    def test_v1_labels_never_create_delete_action_plan(self) -> None:
        labels = self._labels()
        self._write_labels(labels)

        result = import_review_labels(self.root, self.run_id, self.labels_path, apply=True, minimum_verified_pairs=2)

        self.assertEqual(result["status"], "imported")
        target = self.destination / "human-label-reviews" / result["labels_sha256"]
        self.assertFalse((target / "action-plan.json").exists())

    def test_v2_ui_export_dry_run_creates_action_plan_only(self) -> None:
        labels = self._labels_v2()
        pair = self._pair_row(self.spec_v2, "asset-prompt-c", "asset-prompt-d")
        row = next(entry for entry in labels["pairs"] if entry["pair_id"] == pair["pair_id"])
        row["human_label"] = "identical"
        row["human_verified"] = True
        row["action"] = "delete_duplicate"
        self._write_labels(labels)

        result = import_review_labels(self.root, self.run_id, self.labels_path, minimum_verified_pairs=1)

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["action_plan_status"], "action_plan_only")
        self.assertEqual(result["action_plan_only"], True)
        self.assertEqual(result["actual_deletions"], 0)
        self.assertEqual(result["comparison_changed"], False)
        self.assertEqual(result["canonical_changed"], False)
        self.assertEqual(result["writes"], 0)

    def test_v2_apply_writes_action_plan_with_direct_delete_suggestion(self) -> None:
        labels = self._labels_v2()
        pair = self._pair_row(self.spec_v2, "asset-prompt-c", "asset-prompt-d")
        row = next(entry for entry in labels["pairs"] if entry["pair_id"] == pair["pair_id"])
        row["human_label"] = "identical"
        row["human_verified"] = True
        row["action"] = "delete_duplicate"
        self._write_labels(labels)

        result = import_review_labels(self.root, self.run_id, self.labels_path, apply=True, minimum_verified_pairs=1)

        self.assertEqual(result["status"], "imported")
        target = self.destination / "human-label-reviews" / result["labels_sha256"]
        plan = json.loads((target / "action-plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["status"], "action_plan_only")
        self.assertEqual(plan["action_plan_only"], True)
        self.assertEqual(plan["actual_deletions"], 0)
        self.assertEqual(len(plan["planned_deletions"]), 1)
        self.assertEqual(plan["planned_deletions"][0]["keep_id"], "asset-prompt-c")
        self.assertEqual(plan["planned_deletions"][0]["delete_id"], "asset-prompt-d")

    def test_v2_tampered_retention_suggestion_fails_before_write(self) -> None:
        labels = self._labels_v2()
        pair = self._pair_row(self.spec_v2, "asset-prompt-c", "asset-prompt-d")
        row = next(entry for entry in labels["pairs"] if entry["pair_id"] == pair["pair_id"])
        row["human_label"] = "identical"
        row["human_verified"] = True
        row["action"] = "delete_duplicate"
        row["retention_suggestion"]["keep_id"] = "asset-prompt-d"
        row["retention_suggestion"]["delete_id"] = "asset-prompt-c"
        self._write_labels(labels)

        with self.assertRaisesRegex(ValueError, "retention suggestion mismatch"):
            import_review_labels(self.root, self.run_id, self.labels_path, minimum_verified_pairs=1)

    def test_v2_action_label_mismatch_fails_before_write(self) -> None:
        labels = self._labels_v2()
        pair = self._pair_row(self.spec_v2, "asset-prompt-c", "asset-prompt-d")
        row = next(entry for entry in labels["pairs"] if entry["pair_id"] == pair["pair_id"])
        row["human_label"] = "same_visual_family"
        row["human_verified"] = True
        row["action"] = "delete_duplicate"
        self._write_labels(labels)

        with self.assertRaisesRegex(ValueError, "action does not match the selected v2 label"):
            import_review_labels(self.root, self.run_id, self.labels_path, minimum_verified_pairs=1)

    def test_v2_already_archived_identical_pair_becomes_control_not_new_delete(self) -> None:
        labels = self._labels_v2()
        pair = self._pair_row(self.spec_v2, "asset-exact-a", "asset-exact-b")
        row = next(entry for entry in labels["pairs"] if entry["pair_id"] == pair["pair_id"])
        row["human_label"] = "identical"
        row["human_verified"] = True
        row["action"] = "delete_duplicate"
        self._write_labels(labels)

        result = import_review_labels(self.root, self.run_id, self.labels_path, apply=True, minimum_verified_pairs=1)

        target = self.destination / "human-label-reviews" / result["labels_sha256"]
        plan = json.loads((target / "action-plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["status"], "action_plan_only")
        self.assertEqual(len(plan["planned_deletions"]), 0)
        self.assertEqual(len(plan["already_archived_controls"]), 1)
        self.assertEqual(plan["already_archived_controls"][0]["keep_id"], "asset-exact-a")
        self.assertEqual(plan["already_archived_controls"][0]["delete_id"], "asset-exact-b")
        self.assertEqual(plan["already_archived_controls"][0]["selected_action"], "delete_duplicate")

    def test_v2_archived_group_only_pair_is_control_not_group_relation(self) -> None:
        labels = self._labels_v2()
        pair = self._pair_row(self.spec_v2, "asset-exact-a", "asset-exact-b")
        row = next(entry for entry in labels["pairs"] if entry["pair_id"] == pair["pair_id"])
        row["human_label"] = "same_visual_family"
        row["human_verified"] = True
        row["action"] = "group_only"
        self._write_labels(labels)

        result = import_review_labels(self.root, self.run_id, self.labels_path, apply=True, minimum_verified_pairs=1)

        target = self.destination / "human-label-reviews" / result["labels_sha256"]
        plan = json.loads((target / "action-plan.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["status"], "action_plan_only")
        self.assertEqual(plan["planned_deletions"], [])
        self.assertEqual(plan["group_relations"], [])
        self.assertEqual(len(plan["already_archived_controls"]), 1)
        self.assertEqual(plan["already_archived_controls"][0]["selected_action"], "group_only")

    def test_v2_both_archived_non_representative_pair_is_control_not_conflict(self) -> None:
        source = {
            "items": [
                {"id": "rep-a", "prompt": "primary json prompt"},
                {"id": "archived-b", "prompt": "duplicate prompt"},
                {"id": "archived-c", "prompt": "duplicate prompt"},
                {"id": "rep-d", "prompt": "secondary json prompt"},
            ],
            "retention": {
                "active_ids": ["rep-a", "rep-d"],
                "archived": [
                    {"id": "archived-b", "representative_id": "rep-a"},
                    {"id": "archived-c", "representative_id": "rep-d"},
                ],
            },
        }
        spec = {
            "run_id": self.run_id,
            "comparison_dir": "comparison-v1",
            "review_spec_sha256": "spec-sha",
            "pairs": [
                {
                    "pair_id": "pair-archived-b-c",
                    "left": {"id": "archived-b"},
                    "right": {"id": "archived-c"},
                }
            ],
        }
        normalized = {
            "pairs": [
                {
                    "pair_id": "pair-archived-b-c",
                    "human_label": "identical",
                    "human_verified": True,
                    "action": "delete_duplicate",
                    "reason": "control calibration only",
                    "dimensions": {},
                    "left": {"id": "archived-b"},
                    "right": {"id": "archived-c"},
                }
            ]
        }

        plan = build_action_plan(source, spec, normalized)

        self.assertEqual(plan["status"], "action_plan_only")
        self.assertEqual(plan["planned_deletions"], [])
        self.assertEqual(plan["conflicts"], [])
        self.assertEqual(len(plan["already_archived_controls"]), 1)
        control = plan["already_archived_controls"][0]
        self.assertEqual(control["member_ids"], ["archived-b", "archived-c"])
        self.assertEqual(control["representative_ids"], ["rep-a", "rep-d"])
        self.assertEqual(control["selected_action"], "delete_duplicate")
        self.assertIsNone(control["keep_id"])
        self.assertIsNone(control["delete_id"])

    def test_v2_conflict_graph_blocks_delete_when_victim_has_other_group_relation(self) -> None:
        labels = self._labels_v2()
        pair_delete = self._pair_row(self.spec_v2, "asset-prompt-c", "asset-prompt-d")
        row_delete = next(entry for entry in labels["pairs"] if entry["pair_id"] == pair_delete["pair_id"])
        row_delete["human_label"] = "identical"
        row_delete["human_verified"] = True
        row_delete["action"] = "delete_duplicate"
        pair_group = self._pair_row(self.spec_v2, "asset-prompt-d", "asset-boundary-e")
        row_group = next(entry for entry in labels["pairs"] if entry["pair_id"] == pair_group["pair_id"])
        row_group["human_label"] = "same_visual_family"
        row_group["human_verified"] = True
        row_group["action"] = "group_only"
        self._write_labels(labels)

        with self.assertRaisesRegex(ValueError, "planned delete target also has another resolved keep/group relation"):
            import_review_labels(self.root, self.run_id, self.labels_path, minimum_verified_pairs=1)

    def test_v2_incremental_progress_is_append_only(self) -> None:
        first = self._labels_v2()
        pair_first = self._pair_row(self.spec_v2, "asset-prompt-c", "asset-prompt-d")
        row_first = next(entry for entry in first["pairs"] if entry["pair_id"] == pair_first["pair_id"])
        row_first["human_label"] = "identical"
        row_first["human_verified"] = True
        row_first["action"] = "delete_duplicate"
        self._write_labels(first)
        imported_first = import_review_labels(self.root, self.run_id, self.labels_path, apply=True, minimum_verified_pairs=1)

        second = self._labels_v2()
        second["reviewed_at"] = "2026-09-03T13:00:00Z"
        row_first_second = next(entry for entry in second["pairs"] if entry["pair_id"] == pair_first["pair_id"])
        row_first_second["human_label"] = "identical"
        row_first_second["human_verified"] = True
        row_first_second["action"] = "delete_duplicate"
        pair_second = self._pair_row(self.spec_v2, "asset-boundary-e", "asset-boundary-f")
        row_second = next(entry for entry in second["pairs"] if entry["pair_id"] == pair_second["pair_id"])
        row_second["human_label"] = "near_duplicate"
        row_second["human_verified"] = True
        row_second["action"] = "group_only"
        self._write_labels(second)
        imported_second = import_review_labels(self.root, self.run_id, self.labels_path, apply=True, minimum_verified_pairs=1)

        self.assertNotEqual(imported_first["labels_sha256"], imported_second["labels_sha256"])
        first_dir = self.destination / "human-label-reviews" / imported_first["labels_sha256"]
        second_dir = self.destination / "human-label-reviews" / imported_second["labels_sha256"]
        self.assertTrue((first_dir / "receipt.json").is_file())
        self.assertTrue((second_dir / "receipt.json").is_file())
        self.assertTrue((first_dir / "action-plan.json").is_file())
        self.assertTrue((second_dir / "action-plan.json").is_file())

    def test_v2_conflicting_resolved_pair_change_is_blocked_with_action_target_context(self) -> None:
        first = self._labels_v2()
        pair = self._pair_row(self.spec_v2, "asset-prompt-c", "asset-prompt-d")
        row = next(entry for entry in first["pairs"] if entry["pair_id"] == pair["pair_id"])
        row["human_label"] = "identical"
        row["human_verified"] = True
        row["action"] = "delete_duplicate"
        self._write_labels(first)
        imported = import_review_labels(self.root, self.run_id, self.labels_path, apply=True, minimum_verified_pairs=1)
        self.assertEqual(imported["status"], "imported")

        changed = self._labels_v2()
        changed["reviewed_at"] = "2026-09-03T13:00:00Z"
        row_changed = next(entry for entry in changed["pairs"] if entry["pair_id"] == pair["pair_id"])
        row_changed["human_label"] = "same_visual_family"
        row_changed["human_verified"] = True
        row_changed["action"] = "group_only"
        self._write_labels(changed)

        with self.assertRaisesRegex(ValueError, "prior action=delete_duplicate"):
            import_review_labels(self.root, self.run_id, self.labels_path, apply=True, minimum_verified_pairs=1)

    def test_apply_writes_append_only_import_and_is_idempotent(self) -> None:
        labels = self._labels()
        self._write_labels(labels)

        first = import_review_labels(self.root, self.run_id, self.labels_path, apply=True, minimum_verified_pairs=2)
        second = import_review_labels(self.root, self.run_id, self.labels_path, apply=True, minimum_verified_pairs=2)

        self.assertEqual(first["status"], "imported")
        self.assertEqual(second["status"], "imported")
        target = self.destination / "human-label-reviews" / first["labels_sha256"]
        self.assertTrue((target / "labels.json").is_file())
        self.assertTrue((target / "summary.json").is_file())
        self.assertTrue((target / "receipt.json").is_file())
        summary = json.loads((target / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["threshold_summary"]["status"], "ok")
        self.assertEqual(summary["threshold_histogram"]["schema_version"], "image-similarity-threshold-histogram-1")
        self.assertIsNone(summary["automatic_threshold_selection"])
        self.assertEqual(summary["human_verified_scope"], "pair_only")
        self.assertEqual(summary["unsure_status"], "unresolved")

    def test_conflicting_labels_for_same_review_spec_fail(self) -> None:
        labels = self._labels()
        self._write_labels(labels)
        imported = import_review_labels(self.root, self.run_id, self.labels_path, apply=True, minimum_verified_pairs=2)
        self.assertEqual(imported["status"], "imported")

        changed = self._labels()
        changed["pairs"][1]["human_label"] = "same_theme_only"
        changed["pairs"][1]["human_verified"] = True
        self._write_labels(changed)
        with self.assertRaisesRegex(ValueError, "conflicting labels already imported"):
            import_review_labels(self.root, self.run_id, self.labels_path, apply=True, minimum_verified_pairs=2)

    def test_incremental_progress_snapshot_is_accepted_without_overwriting_old_receipt(self) -> None:
        first = self._labels()
        self._write_labels(first)
        imported_first = import_review_labels(self.root, self.run_id, self.labels_path, apply=True, minimum_verified_pairs=2)

        second = self._labels()
        second["reviewed_at"] = "2026-09-03T13:00:00Z"
        second["pairs"][3]["human_label"] = "same_theme_only"
        second["pairs"][3]["human_verified"] = False
        second["pairs"][4]["human_label"] = "near_duplicate"
        second["pairs"][4]["human_verified"] = True
        second["pairs"][4]["dimensions"]["subject"] = "same"
        self._write_labels(second)
        imported_second = import_review_labels(self.root, self.run_id, self.labels_path, apply=True, minimum_verified_pairs=2)

        self.assertNotEqual(imported_first["labels_sha256"], imported_second["labels_sha256"])
        first_dir = self.destination / "human-label-reviews" / imported_first["labels_sha256"]
        second_dir = self.destination / "human-label-reviews" / imported_second["labels_sha256"]
        self.assertTrue((first_dir / "receipt.json").is_file())
        self.assertTrue((second_dir / "receipt.json").is_file())
        first_summary = json.loads((first_dir / "summary.json").read_text(encoding="utf-8"))
        second_summary = json.loads((second_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(first_summary["threshold_summary"]["verified_pairs"], 2)
        self.assertEqual(second_summary["threshold_summary"]["verified_pairs"], 3)
        self.assertEqual(first_summary["reviewed_at"], "2026-09-03T12:00:00Z")
        self.assertEqual(second_summary["reviewed_at"], "2026-09-03T13:00:00Z")

    def test_outside_pair_or_binding_mismatch_fail_before_write(self) -> None:
        labels = self._labels()
        labels["pairs"][0]["pair_id"] = "pair-fake"
        self._write_labels(labels)

        with self.assertRaisesRegex(ValueError, "unknown or duplicate pair_id"):
            import_review_labels(self.root, self.run_id, self.labels_path, minimum_verified_pairs=2)
        self.assertFalse((self.destination / "human-label-reviews").exists())

    def test_changed_corpus_after_spec_build_fails_before_write(self) -> None:
        labels = self._labels()
        self._write_labels(labels)
        vectors_path = self.destination / "comparison-v1" / "vectors.json"
        vectors = json.loads(vectors_path.read_text(encoding="utf-8"))
        vectors["voyage_image"]["asset-boundary-f"] = _unit(0.1, math.sqrt(1 - 0.1**2), 0.0)
        write_json(vectors_path, vectors)

        with self.assertRaisesRegex(ValueError, "stored review spec no longer matches current source/manifest/vector state"):
            import_review_labels(self.root, self.run_id, self.labels_path, minimum_verified_pairs=2)
        self.assertFalse((self.destination / "human-label-reviews").exists())

    def test_cli_dry_run_and_apply_follow_contract(self) -> None:
        build_output = io.StringIO()
        with mock.patch.object(build_review_cli, "__file__", str(self.root / "src" / "build_image_similarity_review.py")):
            with mock.patch.object(
                sys,
                "argv",
                ["build_image_similarity_review.py", "--source-run-id", self.run_id, "--max-pairs", "15", "--apply"],
            ):
                with redirect_stdout(build_output):
                    build_review_cli.main()
        built = json.loads(build_output.getvalue())
        self.assertEqual(built["status"], "ready")

        labels = self._labels()
        self._write_labels(labels)

        dry_output = io.StringIO()
        with mock.patch.object(import_labels_cli, "__file__", str(self.root / "src" / "import_image_similarity_labels.py")):
            with mock.patch.object(
                sys,
                "argv",
                [
                    "import_image_similarity_labels.py",
                    "--source-run-id",
                    self.run_id,
                    "--labels",
                    str(self.labels_path),
                    "--minimum-verified-pairs",
                    "2",
                ],
            ):
                with redirect_stdout(dry_output):
                    import_labels_cli.main()
        dry_result = json.loads(dry_output.getvalue())
        self.assertEqual(dry_result["status"], "dry_run")
        self.assertEqual(dry_result["writes"], 0)

        apply_output = io.StringIO()
        with mock.patch.object(import_labels_cli, "__file__", str(self.root / "src" / "import_image_similarity_labels.py")):
            with mock.patch.object(
                sys,
                "argv",
                [
                    "import_image_similarity_labels.py",
                    "--source-run-id",
                    self.run_id,
                    "--labels",
                    str(self.labels_path),
                    "--minimum-verified-pairs",
                    "2",
                    "--apply",
                ],
            ):
                with redirect_stdout(apply_output):
                    import_labels_cli.main()
        apply_result = json.loads(apply_output.getvalue())
        self.assertEqual(apply_result["status"], "imported")
        self.assertEqual(apply_result["writes"], 3)


if __name__ == "__main__":
    unittest.main()
