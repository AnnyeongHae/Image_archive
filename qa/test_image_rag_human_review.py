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
from image_rag_eval.experiment import digest, json_bytes, prepared_image, run_path, write_json
from image_rag_eval.human_review import (
    build_human_review_artifacts,
    build_review_spec,
    blank_review_labels,
    load_review_source,
    plan_human_review_build,
    review_html,
    summarize_thresholds,
    validate_review_labels,
)
from image_rag_eval.comparison import add_order_evidence
from image_rag_eval.retention import build_retention
from image_rag_eval.similarity import image_signals


def _unit(*values: float) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


class HumanReviewTests(unittest.TestCase):
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
        write_json(comparison_dir / "retention.json", build_retention(self.manifest["items"]))
        self.source = load_review_source(self.root, self.run_id)
        self.spec = build_review_spec(self.source, max_pairs=15)

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

    def _labels_with_unsure_and_verified(self) -> dict[str, object]:
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

    def test_plan_and_build_artifacts_are_bound_and_idempotent(self) -> None:
        planned = plan_human_review_build(self.root, self.run_id, max_pairs=15)
        self.assertEqual(planned["status"], "dry_run")
        self.assertEqual(planned["network_calls"], 0)
        self.assertEqual(planned["retention_basis"], "comparison-v1 retention.active_ids (source_frozen_validated)")
        self.assertEqual(planned["active_items"], 5)
        self.assertEqual(planned["sampled_pairs"], 11)
        self.assertGreaterEqual(planned["bucket_counts"].get("local_exact_or_near_copy", 0), 1)
        self.assertGreaterEqual(planned["bucket_counts"].get("prompt_match_challenge", 0), 1)

        built = build_human_review_artifacts(self.root, self.run_id, max_pairs=15)
        self.assertEqual(built["status"], "ready")
        self.assertEqual(built["writes"], 4)
        html_path = Path(built["html_path"])
        spec_path = Path(built["spec_path"])
        summary_path = Path(built["summary_path"])
        template_path = Path(built["template_path"])
        for path in (html_path, spec_path, summary_path, template_path):
            self.assertTrue(path.is_file(), path)
        html = html_path.read_text(encoding="utf-8")
        self.assertIn("시각 라벨 선택 후 프롬프트 의도 공개", html)
        self.assertIn("0.85/0.90 수치는 검토 우선순위 가설", html)
        self.assertIn("near_duplicate", html)
        self.assertIn("localStorage", html)
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        self.assertEqual(summary["threshold_hypotheses"]["candidate_only"], [0.85, 0.9])
        self.assertEqual(summary["retention_basis"], "comparison-v1 retention.active_ids (source_frozen_validated)")

        rebuilt = build_human_review_artifacts(self.root, self.run_id, max_pairs=15)
        self.assertEqual(rebuilt["status"], "ready")
        self.assertEqual(spec_path.read_bytes(), Path(rebuilt["spec_path"]).read_bytes())

    def test_validate_labels_accepts_unsure_but_rejects_verified_unsure_and_bad_timestamp(self) -> None:
        labels = self._labels_with_unsure_and_verified()
        normalized = validate_review_labels(self.spec, labels)
        self.assertEqual(normalized["pairs"][0]["human_label"], "unsure")
        self.assertFalse(normalized["pairs"][0]["human_verified"])
        self.assertEqual(normalized["reviewed_at"], "2026-09-03T12:00:00Z")

        bad_unsure = json.loads(json.dumps(labels))
        bad_unsure["pairs"][0]["human_verified"] = True
        with self.assertRaisesRegex(ValueError, "unsure"):
            validate_review_labels(self.spec, bad_unsure)

        bad_time = json.loads(json.dumps(labels))
        bad_time["reviewed_at"] = "2026-09-03 12:00:00"
        with self.assertRaisesRegex(ValueError, "reviewed_at"):
            validate_review_labels(self.spec, bad_time)

    def test_summarize_thresholds_excludes_unsure_and_reports_unresolved(self) -> None:
        labels = self._labels_with_unsure_and_verified()
        insufficient = summarize_thresholds(self.spec, labels, minimum_verified_pairs=3)
        self.assertEqual(insufficient["status"], "insufficient_human_labels")
        self.assertEqual(insufficient["labeled_pairs"], 3)
        self.assertEqual(insufficient["unresolved_pairs"], 1)
        self.assertEqual(insufficient["verified_pairs"], 2)

        summary = summarize_thresholds(self.spec, labels, minimum_verified_pairs=2)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["labeled_pairs"], 3)
        self.assertEqual(summary["unresolved_pairs"], 1)
        self.assertEqual(summary["verified_pairs"], 2)
        self.assertNotIn("unsure", summary["label_counts"])
        self.assertTrue(any(row["threshold"] == 0.85 for row in summary["threshold_summary"]))

    def test_load_review_source_requires_prepared_receipt_binding(self) -> None:
        write_json(
            self.destination / "prepared.json",
            {
                "schema_version": "1",
                "complete": False,
                "manifest_sha256": digest(json_bytes(self.manifest)),
                "at": "2026-09-03T00:00:00+00:00",
            },
        )
        with self.assertRaisesRegex(ValueError, "private sample preparation is incomplete or changed"):
            load_review_source(self.root, self.run_id)

    def test_html_and_cli_dry_run_follow_contract(self) -> None:
        html = review_html(self.spec)
        self.assertIn("AI의 사람 승인 대체를 하지 않습니다", html)
        self.assertIn("prompt_exact", html)
        self.assertIn("threshold_hypothesis", html)

        output = io.StringIO()
        fake_script = self.root / "src" / "build_image_similarity_review.py"
        fake_script.parent.mkdir(parents=True, exist_ok=True)
        fake_script.write_text("# fixture path only\n", encoding="utf-8")
        with mock.patch.object(
            sys,
            "argv",
            [
                "build_image_similarity_review.py",
                "--source-run-id",
                self.run_id,
                "--max-pairs",
                "15",
            ],
        ):
            with redirect_stdout(output):
                with mock.patch.object(build_review_cli, "__file__", str(fake_script)):
                    build_review_cli.main()
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["writes"], 0)
        self.assertEqual(result["sampled_pairs"], 11)

    def test_load_review_source_accepts_source_frozen_retention_when_canonical_order_matches(self) -> None:
        canonical_dir = self.root / "data" / "canonical"
        canonical_dir.mkdir(parents=True, exist_ok=True)
        rows = [
            {"catalog_key": item["catalog_key"], "ordinal": index + 1}
            for index, item in enumerate(reversed(self.manifest["items"]))
        ]
        (canonical_dir / "archive_records.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        ordered_manifest = add_order_evidence(self.root, self.manifest)
        write_json(self.destination / "comparison-v1" / "retention.json", build_retention(ordered_manifest["items"]))

        loaded = load_review_source(self.root, self.run_id)
        self.assertEqual(loaded["retention_basis"], "comparison-v1 retention.active_ids (source_frozen_validated)")
        self.assertEqual(len(loaded["retention"]["active_ids"]), 5)


if __name__ == "__main__":
    unittest.main()
