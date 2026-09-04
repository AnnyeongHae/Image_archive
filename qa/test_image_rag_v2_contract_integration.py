from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from image_rag_eval.action_planning import build_action_plan
from image_rag_eval.experiment import digest, json_bytes, prepared_image, run_path, write_json
from image_rag_eval.human_review import build_human_review_artifacts, blank_review_labels
from image_rag_eval.human_review_v2 import (
    REVIEW_V2_SPEC_FILENAME,
    blank_review_labels_v2,
    build_human_review_v2_artifacts,
    load_bound_review_spec_v2,
    migrate_v1_labels_to_v2,
    validate_review_labels_v2,
)
from image_rag_eval.label_import import import_review_labels
from image_rag_eval.retention import build_retention
from image_rag_eval.similarity import image_signals


def _unit(*values: float) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


class HumanReviewV2ContractIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run_id = "voyage-review-v2-fixture"
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
        write_json(comparison_dir / "retention.json", build_retention(self.manifest["items"]))
        build_human_review_artifacts(self.root, self.run_id, max_pairs=15)
        build_human_review_v2_artifacts(self.root, self.run_id)
        self.spec_v2 = json.loads((self.destination / REVIEW_V2_SPEC_FILENAME).read_text(encoding="utf-8"))
        self.spec_v2_bound, self.spec_v1_bound, self.source_v2 = load_bound_review_spec_v2(self.root, self.run_id)
        self.spec_v1 = json.loads((self.destination / "human-similarity-review.spec.json").read_text(encoding="utf-8"))
        self.labels_path = self.root / "downloaded-v2-labels.json"

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

    def _find_pair(self, payload: dict[str, object], member_ids: set[str]) -> dict[str, object]:
        for row in payload["pairs"]:
            ids = {row["left"]["id"], row["right"]["id"]}
            if ids == member_ids:
                return row
        raise AssertionError(f"pair not found for {sorted(member_ids)}")

    def _write_labels(self, payload: dict[str, object]) -> None:
        self.labels_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_v2_identical_active_pair_dry_run_yields_actionable_logical_delete_plan_without_writes(self) -> None:
        labels = blank_review_labels_v2(self.spec_v2)
        labels["reviewer"] = "fixture-reviewer"
        labels["reviewed_at"] = "2026-09-03T12:00:00Z"
        active_row = self._find_pair(labels, {"asset-prompt-c", "asset-prompt-d"})
        active_row["human_label"] = "identical"
        active_row["human_verified"] = True
        active_row["action"] = "delete_duplicate"
        self._write_labels(labels)

        result = import_review_labels(self.root, self.run_id, self.labels_path, minimum_verified_pairs=1)

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["writes"], 0)
        self.assertTrue(result["action_plan_only"])
        self.assertEqual(result["action_plan_status"], "action_plan_only")
        self.assertEqual(result["actual_deletions"], 0)
        self.assertFalse(Path(result["import_dir"]).exists())

        normalized = validate_review_labels_v2(self.spec_v2_bound, labels)
        plan = build_action_plan(self.source_v2, self.spec_v2_bound, normalized)
        self.assertEqual(plan["status"], "action_plan_only")
        self.assertFalse(plan["conflicts"])
        self.assertEqual(len(plan["planned_deletions"]), 1)
        planned = plan["planned_deletions"][0]
        self.assertEqual(planned["pair_id"], active_row["pair_id"])
        self.assertEqual(planned["keep_id"], active_row["retention_suggestion"]["keep_id"])
        self.assertEqual(planned["delete_id"], active_row["retention_suggestion"]["delete_id"])
        self.assertEqual(plan["already_archived_controls"], [])

    def test_v2_group_only_pair_never_creates_planned_deletion(self) -> None:
        labels = blank_review_labels_v2(self.spec_v2)
        labels["reviewer"] = "fixture-reviewer"
        labels["reviewed_at"] = "2026-09-03T12:00:00Z"
        active_row = self._find_pair(labels, {"asset-prompt-c", "asset-prompt-d"})
        active_row["human_label"] = "near_duplicate"
        active_row["human_verified"] = True
        active_row["action"] = "group_only"
        self._write_labels(labels)

        result = import_review_labels(self.root, self.run_id, self.labels_path, minimum_verified_pairs=1)

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["writes"], 0)
        self.assertEqual(result["action_plan_status"], "action_plan_only")

        normalized = validate_review_labels_v2(self.spec_v2_bound, labels)
        plan = build_action_plan(self.source_v2, self.spec_v2_bound, normalized)
        self.assertEqual(plan["status"], "action_plan_only")
        self.assertEqual(plan["planned_deletions"], [])
        self.assertEqual(len(plan["group_relations"]), 1)
        self.assertEqual(plan["group_relations"][0]["pair_id"], active_row["pair_id"])
        self.assertEqual(plan["already_archived_controls"], [])

    def test_v1_near_duplicate_migration_never_upgrades_to_identical_or_delete(self) -> None:
        labels_v1 = blank_review_labels(self.spec_v1)
        labels_v1["reviewer"] = "fixture-reviewer"
        labels_v1["reviewed_at"] = "2026-09-03T12:00:00Z"
        active_row_v1 = self._find_pair(labels_v1, {"asset-prompt-c", "asset-prompt-d"})
        active_row_v1["human_label"] = "near_duplicate"
        active_row_v1["human_verified"] = True

        migrated = migrate_v1_labels_to_v2(self.spec_v2_bound, labels_v1)
        migrated_row = self._find_pair(migrated, {"asset-prompt-c", "asset-prompt-d"})
        self.assertEqual(migrated_row["human_label"], "near_duplicate")
        self.assertTrue(migrated_row["human_verified"])
        self.assertEqual(migrated_row["action"], "group_only")

        self._write_labels(migrated)
        result = import_review_labels(self.root, self.run_id, self.labels_path, minimum_verified_pairs=1)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["action_plan_status"], "action_plan_only")

        normalized = validate_review_labels_v2(self.spec_v2_bound, migrated)
        plan = build_action_plan(self.source_v2, self.spec_v2_bound, normalized)
        self.assertEqual(plan["planned_deletions"], [])
        self.assertEqual(len(plan["group_relations"]), 1)
        self.assertEqual(plan["group_relations"][0]["pair_id"], migrated_row["pair_id"])
        self.assertEqual(plan["already_archived_controls"], [])

    def test_v2_identical_exact_control_becomes_already_archived_control_not_new_deletion(self) -> None:
        labels = blank_review_labels_v2(self.spec_v2)
        labels["reviewer"] = "fixture-reviewer"
        labels["reviewed_at"] = "2026-09-03T12:00:00Z"
        exact_row = self._find_pair(labels, {"asset-exact-a", "asset-exact-b"})
        exact_row["human_label"] = "identical"
        exact_row["human_verified"] = True
        exact_row["action"] = "delete_duplicate"
        self._write_labels(labels)

        result = import_review_labels(self.root, self.run_id, self.labels_path, minimum_verified_pairs=1)

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["writes"], 0)
        self.assertEqual(result["action_plan_status"], "action_plan_only")

        normalized = validate_review_labels_v2(self.spec_v2_bound, labels)
        plan = build_action_plan(self.source_v2, self.spec_v2_bound, normalized)
        self.assertEqual(plan["planned_deletions"], [])
        self.assertEqual(len(plan["already_archived_controls"]), 1)
        control = plan["already_archived_controls"][0]
        self.assertEqual(control["pair_id"], exact_row["pair_id"])
        self.assertIn(exact_row["retention_suggestion"]["keep_id"], control["representative_ids"])
        self.assertEqual(control["keep_id"], exact_row["retention_suggestion"]["keep_id"])
        self.assertEqual(control["delete_id"], exact_row["retention_suggestion"]["delete_id"])
        self.assertEqual(plan["group_relations"], [])

    def test_v2_near_duplicate_exact_control_stays_calibration_only_without_group_operation(self) -> None:
        labels = blank_review_labels_v2(self.spec_v2)
        labels["reviewer"] = "fixture-reviewer"
        labels["reviewed_at"] = "2026-09-03T12:00:00Z"
        exact_row = self._find_pair(labels, {"asset-exact-a", "asset-exact-b"})
        exact_row["human_label"] = "near_duplicate"
        exact_row["human_verified"] = True
        exact_row["action"] = "group_only"
        self._write_labels(labels)

        result = import_review_labels(self.root, self.run_id, self.labels_path, minimum_verified_pairs=1)

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["writes"], 0)
        self.assertEqual(result["action_plan_status"], "action_plan_only")

        normalized = validate_review_labels_v2(self.spec_v2_bound, labels)
        plan = build_action_plan(self.source_v2, self.spec_v2_bound, normalized)
        self.assertEqual(plan["planned_deletions"], [])
        self.assertEqual(len(plan["already_archived_controls"]), 1)
        self.assertEqual(plan["already_archived_controls"][0]["pair_id"], exact_row["pair_id"])
        self.assertEqual(plan["group_relations"], [])


if __name__ == "__main__":
    unittest.main()
