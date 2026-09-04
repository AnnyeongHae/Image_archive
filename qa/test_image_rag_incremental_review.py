from __future__ import annotations

import sys
import unittest
import random
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from image_rag_eval.incremental_review import compare_incremental_vectors, render_incremental_comparison
from image_rag_eval import incremental_review


class IncrementalReviewTests(unittest.TestCase):
    def test_compares_members_not_just_representative_and_never_archived(self):
        result = compare_incremental_vectors({"rep": [1, 0], "member": [0, 1], "archived": [0, 1]},
                                             {"new": [0, 1]}, active_existing_ids=["rep", "member"], dimension=2)
        self.assertEqual(result["old_matches"][0]["top3_existing"][0]["id"], "member")
        self.assertEqual(result["old_new_comparisons"], 2)
        self.assertFalse(result["human_approved"])
        self.assertEqual(result["automatic_group_merges"], 0)

    def test_new_new_edges_not_transitively_merged(self):
        result = compare_incremental_vectors({"old": [1, 0]},
                    {"a": [1, 0], "b": [1, 1], "c": [0, 1]}, active_existing_ids=["old"], dimension=2, threshold=0.7)
        self.assertEqual(len(result["new_new_candidate_pairs"]), 2)
        self.assertNotIn("groups", result)
        self.assertEqual(result["new_new_comparisons"], 3)

    def test_fail_closed_for_bad_vector_or_reference_or_old_id(self):
        for new, active in (({"new": [1, float("nan")]}, ["old"]), ({"new": [0, 0]}, ["old"]),
                            ({"new": [1]}, ["old"]), ({"old": [1, 0]}, ["old"]),
                            ({"new": [1, 0]}, ["missing"]), ({"new": [1, 0]}, ["old", "old"])):
            with self.subTest(new=new, active=active), self.assertRaises(ValueError):
                compare_incremental_vectors({"old": [1, 0]}, new, active_existing_ids=active, dimension=2)

    def test_threshold_dimension_and_overflow_guards(self):
        for options in ({"dimension": 0}, {"dimension": True}, {"threshold": float("nan")}, {"threshold": 2}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                compare_incremental_vectors({}, {}, active_existing_ids=[], **options)
        with self.assertRaises(ValueError):
            compare_incremental_vectors({}, {"new": [1e308, 0]}, active_existing_ids=[], dimension=2)

    def test_renderer_is_local_only_and_escapes_labels(self):
        result = compare_incremental_vectors({}, {"new": [1, 0]}, active_existing_ids=[], dimension=2)
        item = {"style_id": "<script>bad()</script>", "review_image_path": "inputs/test.png"}
        rendered = render_incremental_comparison(result, {"new": item})
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        for path in ("https://example.com/a.png", "../inputs/../../.env", "data:image/png;base64,123"):
            item["review_image_path"] = path
            with self.assertRaises(ValueError):
                render_incremental_comparison(result, {"new": item})

    def test_matrix_matches_scalar_fallback_across_tiles_ties_and_rounding(self):
        rng = random.Random(839)
        existing = {f"old-{i:03d}": [rng.uniform(-1, 1) for _ in range(32)] for i in range(70)}
        incoming = {f"new-{i:03d}": [rng.uniform(-1, 1) for _ in range(32)] for i in range(67)}
        existing["old-tie-z"] = existing["old-tie-a"] = incoming["new-000"][:]
        options = {"active_existing_ids": list(existing), "dimension": 32, "threshold": .05}
        matrix = compare_incremental_vectors(existing, incoming, **options)
        with patch.object(incremental_review, "_np", None):
            scalar = compare_incremental_vectors(existing, incoming, **options)
        self.assertEqual(matrix, scalar)
        first = next(r for r in matrix["old_matches"] if r["id"] == "new-000")
        self.assertEqual([r["id"] for r in first["top3_existing"][:2]], ["old-tie-a", "old-tie-z"])

    def test_half_rounding_boundary_matches_scalar(self):
        import math
        for target in (.7299995, .7300005, .9799995, -.0000005):
            existing = {"old": [1., 0.]}
            incoming = {"new": [target, math.sqrt(1. - target ** 2)]}
            matrix = compare_incremental_vectors(existing, incoming, active_existing_ids=["old"], dimension=2)
            with patch.object(incremental_review, "_np", None):
                scalar = compare_incremental_vectors(existing, incoming, active_existing_ids=["old"], dimension=2)
            self.assertEqual(matrix, scalar)

    def test_empty_matrix_inputs_preserve_schema(self):
        self.assertEqual(compare_incremental_vectors({}, {}, active_existing_ids=[])["old_matches"], [])


if __name__ == "__main__":
    unittest.main()
