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

import build_image_similarity_review_v2 as build_review_v2_cli
from image_rag_eval.comparison import add_order_evidence
from image_rag_eval.experiment import digest, json_bytes, prepared_image, run_path, write_json
from image_rag_eval.human_review import (
    blank_review_labels,
    build_human_review_artifacts,
    load_review_source,
    normalized_review_spec_for_hash,
)
from image_rag_eval.human_review_v2 import (
    REVIEW_V2_SPEC_FILENAME,
    blank_review_labels_v2,
    build_human_review_v2_artifacts,
    load_bound_review_spec_v2,
    migrate_v1_labels_to_v2,
    plan_human_review_v2_build,
    review_html_v2,
    validate_review_labels_v2,
)
from image_rag_eval.retention import build_retention
from image_rag_eval.similarity import image_signals


def _unit(*values: float) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


class HumanReviewV2Tests(unittest.TestCase):
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
        canonical_dir = self.root / "data" / "canonical"
        canonical_dir.mkdir(parents=True, exist_ok=True)
        ordered_rows = [
            {"catalog_key": item["catalog_key"], "ordinal": index + 1}
            for index, item in enumerate(self.manifest["items"])
        ]
        (canonical_dir / "archive_records.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in ordered_rows),
            encoding="utf-8",
        )
        comparison_dir = self.destination / "comparison-v1"
        comparison_dir.mkdir(parents=True, exist_ok=True)
        ordered_manifest = add_order_evidence(self.root, self.manifest)
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
        write_json(comparison_dir / "retention.json", build_retention(ordered_manifest["items"]))
        self.source = load_review_source(self.root, self.run_id)
        self.v1_built = build_human_review_artifacts(self.root, self.run_id, max_pairs=15)

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
            raise ValueError(f"unknown mode: {mode}")
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

    def test_v2_plan_build_and_bound_load_preserve_frozen_v1_pairs(self) -> None:
        planned = plan_human_review_v2_build(self.root, self.run_id)
        self.assertEqual(planned["status"], "dry_run")
        self.assertEqual(planned["network_calls"], 0)

        built = build_human_review_v2_artifacts(self.root, self.run_id)
        self.assertEqual(built["status"], "ready")
        self.assertEqual(built["writes"], 4)

        spec_v2, spec_v1, source = load_bound_review_spec_v2(self.root, self.run_id)
        self.assertEqual(spec_v2["source_review_spec_sha256"], spec_v1["review_spec_sha256"])
        self.assertEqual(
            [pair["pair_id"] for pair in spec_v2["pairs"]],
            [pair["pair_id"] for pair in spec_v1["pairs"]],
        )
        self.assertEqual(spec_v2["counts"]["sampled_pairs"], spec_v1["counts"]["sampled_pairs"])
        self.assertEqual(source["retention_basis"], "comparison-v1 retention.active_ids (source_frozen_validated)")

        html = review_html_v2(spec_v2)
        self.assertIn("동일 — 중복 삭제", html)
        self.assertIn("거의 동일 — 그룹핑/둘 다 보존", html)
        self.assertIn("크게 보기", html)
        self.assertIn("기존 중복 제외(새 삭제 아님)", html)
        self.assertIn("초안은 보존되었습니다", html)
        self.assertEqual(html.count("localStorage.removeItem(draftKey)"), 1)

    def test_validate_v2_identical_requires_verified_and_delete_action(self) -> None:
        build_human_review_v2_artifacts(self.root, self.run_id)
        spec_v2, _, _ = load_bound_review_spec_v2(self.root, self.run_id)
        labels = blank_review_labels_v2(spec_v2)
        labels["reviewer"] = "fixture-reviewer-v2"
        labels["reviewed_at"] = "2026-09-03T12:00:00Z"
        labels["pairs"][0]["human_label"] = "identical"
        labels["pairs"][0]["human_verified"] = True
        labels["pairs"][0]["action"] = "delete_duplicate"

        normalized = validate_review_labels_v2(spec_v2, labels)
        self.assertEqual(normalized["pairs"][0]["action"], "delete_duplicate")
        self.assertTrue(normalized["pairs"][0]["human_verified"])

        bad_verified = json.loads(json.dumps(labels))
        bad_verified["pairs"][0]["human_verified"] = False
        with self.assertRaisesRegex(ValueError, "resolved v2 labels must be human_verified"):
            validate_review_labels_v2(spec_v2, bad_verified)

        bad_action = json.loads(json.dumps(labels))
        bad_action["pairs"][0]["action"] = "group_only"
        with self.assertRaisesRegex(ValueError, "action"):
            validate_review_labels_v2(spec_v2, bad_action)

    def test_v1_migration_keeps_near_duplicate_group_only(self) -> None:
        build_human_review_v2_artifacts(self.root, self.run_id)
        spec_v2, _, _ = load_bound_review_spec_v2(self.root, self.run_id)
        source_v1 = json.loads((self.destination / "human-similarity-review.template.json").read_text(encoding="utf-8"))
        source_v1["reviewer"] = "fixture-reviewer"
        source_v1["reviewed_at"] = "2026-09-03T12:00:00Z"
        source_v1["pairs"][0]["human_label"] = "near_duplicate"
        source_v1["pairs"][0]["human_verified"] = True
        source_v1["pairs"][1]["human_label"] = "unsure"
        source_v1["pairs"][1]["human_verified"] = False

        migrated = migrate_v1_labels_to_v2(spec_v2, source_v1)
        self.assertEqual(migrated["pairs"][0]["human_label"], "near_duplicate")
        self.assertEqual(migrated["pairs"][0]["action"], "group_only")
        self.assertTrue(migrated["pairs"][0]["human_verified"])
        self.assertEqual(migrated["pairs"][1]["human_label"], "unsure")
        self.assertEqual(migrated["pairs"][1]["action"], "defer")
        self.assertFalse(migrated["pairs"][1]["human_verified"])
        self.assertIn("group_only", migrated["migration_note"])

    def test_load_bound_review_spec_v2_detects_drift(self) -> None:
        built = build_human_review_v2_artifacts(self.root, self.run_id)
        spec_path = Path(built["spec_path"])
        tampered = json.loads(spec_path.read_text(encoding="utf-8"))
        tampered["action_definitions"]["group_only"] = "tampered"
        tampered["review_spec_sha256"] = digest(json_bytes(normalized_review_spec_for_hash(tampered)))
        write_json(spec_path, tampered)

        with self.assertRaisesRegex(ValueError, "stored v2 review spec no longer matches"):
            load_bound_review_spec_v2(self.root, self.run_id)

    def test_v2_cli_dry_run_is_standalone_safe(self) -> None:
        output = io.StringIO()
        fake_script = self.root / "src" / "build_image_similarity_review_v2.py"
        fake_script.parent.mkdir(parents=True, exist_ok=True)
        fake_script.write_text("# fixture path only\n", encoding="utf-8")
        with mock.patch.object(
            sys,
            "argv",
            [
                "build_image_similarity_review_v2.py",
                "--source-run-id",
                self.run_id,
            ],
        ):
            with redirect_stdout(output):
                with mock.patch.object(build_review_v2_cli, "__file__", str(fake_script)):
                    build_review_v2_cli.main()
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["writes"], 0)
        self.assertEqual(result["sampled_pairs"], 11)


if __name__ == "__main__":
    unittest.main()
