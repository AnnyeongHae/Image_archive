from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from image_rag_eval.calibration import calibrate


def _side(item_id: str) -> dict[str, str]:
    return {
        "id": item_id,
        "source_sha256": f"source-{item_id}",
        "prepared_sha256": f"prepared-{item_id}",
    }


class CalibrationTests(unittest.TestCase):
    def _spec(self) -> dict[str, object]:
        return {
            "schema_version": "image-similarity-review-spec-2",
            "review_spec_sha256": "spec-sha",
            "source_review_spec_sha256": "source-v1-sha",
            "run_id": "run-1",
            "provider": "voyage",
            "model": "voyage-multimodal-3.5",
            "dimensions": 1024,
            "source_manifest_sha256": "manifest-sha",
            "vector_fingerprint": "vector-fingerprint",
            "comparison_dir": "comparison-v1",
            "sampling_seed": "seed",
            "counts": {"sampled_pairs": 6, "total_pairs": 6},
            "pairs": [
                {
                    "pair_id": "p-pos-high",
                    "left": _side("a"),
                    "right": _side("b"),
                    "retention_suggestion": {"keep_id": "a", "delete_id": "b", "already_excluded": False},
                    "voyage_cosine": 0.81,
                    "sampling_bucket": "high_similarity",
                },
                {
                    "pair_id": "p-neg-high",
                    "left": _side("a"),
                    "right": _side("c"),
                    "retention_suggestion": {"keep_id": "a", "delete_id": "c", "already_excluded": False},
                    "voyage_cosine": 0.70,
                    "sampling_bucket": "high_similarity",
                },
                {
                    "pair_id": "p-pos-low",
                    "left": _side("b"),
                    "right": _side("c"),
                    "retention_suggestion": {"keep_id": "b", "delete_id": "c", "already_excluded": False},
                    "voyage_cosine": 0.43,
                    "sampling_bucket": "prompt_match_challenge",
                },
                {
                    "pair_id": "p-neg-low",
                    "left": _side("a"),
                    "right": _side("d"),
                    "retention_suggestion": {"keep_id": "a", "delete_id": "d", "already_excluded": False},
                    "voyage_cosine": 0.20,
                    "sampling_bucket": "negative_challenge",
                },
                {
                    "pair_id": "p-unlabeled",
                    "left": _side("b"),
                    "right": _side("d"),
                    "retention_suggestion": {"keep_id": "b", "delete_id": "d", "already_excluded": False},
                    "voyage_cosine": 0.52,
                    "sampling_bucket": "boundary_hypothesis_0.90",
                },
                {
                    "pair_id": "p-control",
                    "left": _side("a"),
                    "right": _side("arch"),
                    "retention_suggestion": {"keep_id": "a", "delete_id": "arch", "already_excluded": True},
                    "voyage_cosine": 0.99,
                    "sampling_bucket": "local_exact_or_near_copy",
                },
            ],
        }

    def _labels(self) -> dict[str, object]:
        return {
            "schema_version": "image-similarity-review-labels-2",
            "review_spec_sha256": "spec-sha",
            "source_review_spec_sha256": "source-v1-sha",
            "run_id": "run-1",
            "provider": "voyage",
            "model": "voyage-multimodal-3.5",
            "dimensions": 1024,
            "source_manifest_sha256": "manifest-sha",
            "vector_fingerprint": "vector-fingerprint",
            "reviewer": "reviewer",
            "reviewed_at": "2026-09-03T12:00:00Z",
            "pairs": [
                {
                    "pair_id": "p-pos-high",
                    "left": _side("a"),
                    "right": _side("b"),
                    "retention_suggestion": {"keep_id": "a", "delete_id": "b", "already_excluded": False},
                    "human_label": "same_visual_family",
                    "human_verified": True,
                    "action": "group_only",
                    "dimensions": {"composition": None, "style": None, "subject": None},
                    "reason": "",
                },
                {
                    "pair_id": "p-neg-high",
                    "left": _side("a"),
                    "right": _side("c"),
                    "retention_suggestion": {"keep_id": "a", "delete_id": "c", "already_excluded": False},
                    "human_label": "unrelated",
                    "human_verified": True,
                    "action": "keep_separate",
                    "dimensions": {"composition": None, "style": None, "subject": None},
                    "reason": "",
                },
                {
                    "pair_id": "p-pos-low",
                    "left": _side("b"),
                    "right": _side("c"),
                    "retention_suggestion": {"keep_id": "b", "delete_id": "c", "already_excluded": False},
                    "human_label": "same_visual_family",
                    "human_verified": True,
                    "action": "group_only",
                    "dimensions": {"composition": None, "style": None, "subject": None},
                    "reason": "",
                },
                {
                    "pair_id": "p-neg-low",
                    "left": _side("a"),
                    "right": _side("d"),
                    "retention_suggestion": {"keep_id": "a", "delete_id": "d", "already_excluded": False},
                    "human_label": "same_theme_only",
                    "human_verified": True,
                    "action": "keep_separate",
                    "dimensions": {"composition": None, "style": None, "subject": None},
                    "reason": "",
                },
                {
                    "pair_id": "p-unlabeled",
                    "left": _side("b"),
                    "right": _side("d"),
                    "retention_suggestion": {"keep_id": "b", "delete_id": "d", "already_excluded": False},
                    "human_label": None,
                    "human_verified": False,
                    "action": None,
                    "dimensions": {"composition": None, "style": None, "subject": None},
                    "reason": "",
                },
                {
                    "pair_id": "p-control",
                    "left": _side("a"),
                    "right": _side("arch"),
                    "retention_suggestion": {"keep_id": "a", "delete_id": "arch", "already_excluded": True},
                    "human_label": "identical",
                    "human_verified": True,
                    "action": "delete_duplicate",
                    "dimensions": {"composition": None, "style": None, "subject": None},
                    "reason": "",
                },
            ],
        }

    def test_calibrate_excludes_archived_touching_pairs_and_counts_active_labels(self) -> None:
        source = {
            "retention": {
                "active_ids": ["a", "b", "c", "d"],
                "archived": [{"id": "arch", "representative_id": "a"}],
            },
            "retention_basis": "source_frozen_validated",
        }

        result = calibrate(source, self._spec(), self._labels())

        self.assertEqual(result["counts"]["calibration_only_pairs"], 1)
        self.assertEqual(result["counts"]["active_visual_positive_pairs"], 2)
        self.assertEqual(result["counts"]["active_negative_pairs"], 2)
        self.assertEqual(result["counts"]["active_unlabeled_pairs"], 1)
        self.assertEqual(result["counts"]["active_identical_pairs"], 0)
        self.assertEqual(result["counts"]["active_same_visual_family_pairs"], 2)
        self.assertEqual(result["counts"]["active_same_theme_only_pairs"], 1)
        self.assertEqual(result["counts"]["active_unrelated_pairs"], 1)

    def test_calibrate_derives_observed_bands_from_active_pairs_only(self) -> None:
        source = {
            "retention": {
                "active_ids": ["a", "b", "c", "d"],
                "archived": [{"id": "arch", "representative_id": "a"}],
            }
        }

        result = calibrate(source, self._spec(), self._labels())

        self.assertEqual(result["observed_overlap"]["min_visual_positive_cosine"], 0.43)
        self.assertEqual(result["observed_overlap"]["max_negative_cosine"], 0.70)
        self.assertEqual(result["observed_overlap"]["observed_negative_only_below"], 0.43)
        self.assertEqual(result["observed_overlap"]["observed_clean_positive_from"], 0.71)
        bands = {row["name"]: row for row in result["candidate_bands"]}
        self.assertEqual(bands["high_review_visual_family_candidate"]["observed_pairs"], 1)
        self.assertEqual(bands["high_review_visual_family_candidate"]["observed_visual_positive_pairs"], 1)
        self.assertEqual(bands["high_review_visual_family_candidate"]["observed_negative_pairs"], 0)
        self.assertEqual(bands["boundary_mixed_review_band"]["observed_pairs"], 3)
        self.assertEqual(bands["boundary_mixed_review_band"]["observed_negative_pairs"], 1)
        self.assertEqual(bands["low_similarity_negative_skew_band"]["observed_negative_pairs"], 1)

    def test_calibrate_exposes_non_automatic_constraints_and_threshold_rows(self) -> None:
        source = {
            "retention": {
                "active_ids": ["a", "b", "c", "d"],
                "archived": [{"id": "arch", "representative_id": "a"}],
            }
        }

        result = calibrate(source, self._spec(), self._labels())

        self.assertFalse(result["constraints"]["automatic_grouping"])
        self.assertFalse(result["constraints"]["automatic_deletion"])
        self.assertEqual(result["constraints"]["complete_link_required_for_grouping"], "recommendation_only")
        by_threshold = {row["threshold"]: row for row in result["threshold_table"]}
        self.assertEqual(by_threshold[0.71]["observed_visual_positive_pairs"], 1)
        self.assertEqual(by_threshold[0.71]["observed_negative_pairs"], 0)
        self.assertEqual(by_threshold[0.43]["observed_pairs_at_or_above_threshold"], 4)

    def test_calibrate_fails_closed_when_sample_lacks_negative_pairs(self) -> None:
        source = {
            "retention": {
                "active_ids": ["a", "b", "c", "d"],
                "archived": [],
            }
        }
        labels = self._labels()
        for row in labels["pairs"]:
            if row["pair_id"] == "p-neg-high":
                row["human_label"] = "same_visual_family"
                row["human_verified"] = True
                row["action"] = "group_only"
            elif row["pair_id"] == "p-neg-low":
                row["human_label"] = None
                row["human_verified"] = False
                row["action"] = None

        result = calibrate(source, self._spec(), labels)

        self.assertEqual(result["status"], "insufficient")
        self.assertIn("no_active_negative_pairs", result["insufficiency_reasons"])
        self.assertEqual(result["candidate_bands"], [])

    def test_calibrate_fails_closed_when_band_edges_do_not_form_a_gap(self) -> None:
        source = {
            "retention": {
                "active_ids": ["a", "b", "c", "d"],
                "archived": [],
            }
        }
        spec = self._spec()
        for pair in spec["pairs"]:
            if pair["pair_id"] == "p-pos-low":
                pair["voyage_cosine"] = 0.431
            elif pair["pair_id"] == "p-neg-high":
                pair["voyage_cosine"] = 0.429

        result = calibrate(source, spec, self._labels())

        self.assertEqual(result["status"], "insufficient")
        self.assertIn("non_overlapping_or_reversed_candidate_bands", result["insufficiency_reasons"])
        self.assertEqual(result["candidate_bands"], [])


if __name__ == "__main__":
    unittest.main()
