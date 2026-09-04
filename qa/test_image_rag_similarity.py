from __future__ import annotations

import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image
from PIL.PngImagePlugin import PngInfo


ARCHIVE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = ARCHIVE_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from image_rag_eval.similarity import (  # noqa: E402
    build_groups,
    build_visual_families,
    compare_pair,
    cosine,
    image_signals,
    mmr,
    prompt_signals,
    rank,
    retrieval_metrics,
)


def _write_png(
    path: Path,
    color: tuple[int, ...],
    *,
    mode: str = "RGB",
    meta: str | None = None,
    size: tuple[int, int] = (32, 32),
) -> None:
    image = Image.new(mode, size, color)
    pnginfo = None
    if meta is not None:
        pnginfo = PngInfo()
        pnginfo.add_text("note", meta)
    image.save(path, format="PNG", pnginfo=pnginfo)


def _item(item_id: str, prompt: str, signals: dict) -> dict:
    return {"id": item_id, "prompt": prompt, "signals": signals}


class ImageRagSimilarityTests(unittest.TestCase):
    def test_image_signals_separate_file_hash_from_pixel_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.png"
            second = root / "second.png"
            _write_png(first, (10, 20, 30), meta="one")
            _write_png(second, (10, 20, 30), meta="two")

            first_signals = image_signals(first)
            second_signals = image_signals(second)

            self.assertNotEqual(first_signals["sha256"], second_signals["sha256"])
            self.assertEqual(first_signals["pixel_sha256"], second_signals["pixel_sha256"])
            self.assertEqual((first_signals["width"], first_signals["height"]), (32, 32))
            self.assertEqual(first_signals["metrics_max_side"], 256)

    def test_image_signals_preserve_alpha_in_pixel_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transparent = root / "transparent.png"
            opaque = root / "opaque.png"
            _write_png(transparent, (10, 20, 30, 0), mode="RGBA")
            _write_png(opaque, (10, 20, 30, 255), mode="RGBA")

            transparent_signals = image_signals(transparent)
            opaque_signals = image_signals(opaque)

            self.assertNotEqual(transparent_signals["pixel_sha256"], opaque_signals["pixel_sha256"])

    def test_image_signals_keep_original_size_while_metrics_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wide = root / "wide.png"
            _write_png(wide, (20, 40, 60, 128), mode="RGBA", size=(1024, 128))

            signals = image_signals(wide)

            self.assertEqual((signals["width"], signals["height"]), (1024, 128))
            self.assertEqual(signals["metrics_max_side"], 256)

    def test_image_signals_reject_multi_frame_images_before_decode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            animated = root / "animated.tiff"
            first = Image.new("RGB", (16, 16), (0, 0, 0))
            second = Image.new("RGB", (16, 16), (255, 255, 255))
            first.save(animated, save_all=True, append_images=[second], format="TIFF")

            with self.assertRaises(ValueError):
                image_signals(animated)

    def test_low_information_blocks_black_white_near_copy_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            black = root / "black.png"
            white = root / "white.png"
            _write_png(black, (0, 0, 0))
            _write_png(white, (255, 255, 255))

            pair = compare_pair(
                _item("black", "solid black", image_signals(black)),
                _item("white", "solid white", image_signals(white)),
            )

            self.assertTrue(pair["phash_hamming"] is not None)
            self.assertIn("low_information_blocks_near_copy", pair["evidence_flags"])
            self.assertNotEqual(pair["candidate_relation"], "near_copy_candidate")

    def test_build_groups_blocks_single_link_chaining_for_near_copy(self) -> None:
        items = [
            _item("A", "p1", {"sha256": "a", "pixel_sha256": "pa"}),
            _item("B", "p2", {"sha256": "b", "pixel_sha256": "pb"}),
            _item("C", "p3", {"sha256": "c", "pixel_sha256": "pc"}),
        ]
        pairs = [
            {"left_id": "A", "right_id": "B", "candidate_relation": "near_copy_candidate", "phash_hamming": 2, "dhash_hamming": 3},
            {"left_id": "B", "right_id": "C", "candidate_relation": "near_copy_candidate", "phash_hamming": 2, "dhash_hamming": 3},
            {"left_id": "A", "right_id": "C", "candidate_relation": "manual_candidate", "phash_hamming": 14, "dhash_hamming": 16},
        ]

        groups = [group for group in build_groups(items, pairs) if group["kind"] == "near_copy_candidate"]
        member_sets = [tuple(group["member_ids"]) for group in groups]

        self.assertNotIn(("A", "B", "C"), member_sets)
        self.assertIn(("A", "B"), member_sets)
        self.assertIn(("B", "C"), member_sets)

    def test_build_visual_families_blocks_single_link_chaining(self) -> None:
        vectors = {
            "A": np.array([1.0, 0.0, 0.0]),
            "B": np.array([0.98478356, 0.17378533, 0.0]),
            "C": np.array([0.93969262, 0.34202014, 0.0]),
        }

        groups = build_visual_families(vectors, k=2, min_cosine=0.95)
        member_sets = [tuple(group["member_ids"]) for group in groups]

        self.assertIn(("A", "B"), member_sets)
        self.assertIn(("B", "C"), member_sets)
        self.assertNotIn(("A", "B", "C"), member_sets)

    def test_prompt_exact_group_does_not_create_hard_image_merge(self) -> None:
        prompt = "Hero shot with feature chips"
        items = [
            _item("A", prompt, {"sha256": "file-a", "pixel_sha256": "pixel-a"}),
            _item("B", prompt, {"sha256": "file-b", "pixel_sha256": "pixel-b"}),
        ]

        groups = build_groups(items, [])
        kinds = {group["kind"] for group in groups}

        self.assertIn("prompt_exact", kinds)
        self.assertNotIn("exact_file", kinds)
        self.assertNotIn("exact_pixels", kinds)
        self.assertNotIn("near_copy_candidate", kinds)

    def test_compare_pair_rejects_invalid_similarity_percent_inputs(self) -> None:
        pair = compare_pair(
            _item("A", "prompt A", {"sha256": "a", "pixel_sha256": "pa", "phash": "0" * 16, "dhash": "0" * 16, "width": 32, "height": 32, "color_histogram": [1 / 24] * 24, "low_information": False}),
            _item("B", "prompt B", {"sha256": "b", "pixel_sha256": "pb", "phash": "0" * 16, "dhash": "0" * 16, "width": 32, "height": 32, "color_histogram": [1 / 24] * 24, "low_information": False}),
            image_cosine=80,
            joint_cosine=float("nan"),
        )

        self.assertIn("invalid_image_cosine", pair["evidence_flags"])
        self.assertIn("invalid_joint_cosine", pair["evidence_flags"])
        self.assertIsNone(pair["image_cosine"])
        self.assertIsNone(pair["joint_cosine"])

    def test_compare_pair_empty_prompts_do_not_match_exact_or_normalized(self) -> None:
        pair = compare_pair(
            _item("A", "", {"sha256": "a", "pixel_sha256": "pa"}),
            _item("B", "", {"sha256": "b", "pixel_sha256": "pb"}),
        )

        self.assertFalse(pair["prompt_exact"])
        self.assertFalse(pair["prompt_normalized_match"])

    def test_compare_pair_semantic_is_soft_candidate_only(self) -> None:
        pair = compare_pair(
            _item("A", "cat on red sofa", {"sha256": "a", "pixel_sha256": "pa"}),
            _item("B", "kitten on couch", {"sha256": "b", "pixel_sha256": "pb"}),
            joint_cosine=0.93,
        )

        self.assertEqual(pair["candidate_relation"], "semantic_related_candidate")

    def test_mmr_prefers_diversity_over_second_near_duplicate(self) -> None:
        query = np.array([1.0, 0.0])
        vectors = {
            "A": np.array([1.0, 0.0]),
            "B": np.array([0.99503719, 0.09950372]),
            "C": np.array([0.0, 1.0]),
        }

        ranked = rank(query, vectors, 3)
        diversified = mmr(query, vectors, 2, lambda_=0.4)

        self.assertEqual([item["id"] for item in ranked[:2]], ["A", "B"])
        self.assertEqual([item["id"] for item in diversified], ["A", "C"])

    def test_retrieval_metrics_return_none_when_labels_are_incomplete(self) -> None:
        ranked = [{"id": "A", "score": 0.9}, {"id": "B", "score": 0.8}, {"id": "C", "score": 0.1}]
        metrics = retrieval_metrics(ranked, {"A": 2, "C": 1}, 3)

        self.assertFalse(metrics["labels_complete"])
        self.assertEqual(metrics["unjudged_ids"], ["B"])
        self.assertIsNone(metrics["recall"])
        self.assertIsNone(metrics["ndcg"])
        self.assertIsNone(metrics["mrr"])
        self.assertIsNone(metrics["hit"])

    def test_retrieval_metrics_compute_when_labels_are_complete(self) -> None:
        ranked = [{"id": "A", "score": 0.9}, {"id": "B", "score": 0.8}, {"id": "C", "score": 0.1}]
        metrics = retrieval_metrics(ranked, {"A": 2, "B": 0, "C": 1}, 3)

        self.assertTrue(metrics["labels_complete"])
        self.assertEqual(metrics["hit"], 1.0)
        self.assertEqual(metrics["mrr"], 1.0)
        self.assertEqual(metrics["recall"], 1.0)
        self.assertTrue(math.isclose(metrics["ndcg"], 0.96394, rel_tol=1e-5))

    def test_prompt_signal_tracks_exact_and_normalized_hashes(self) -> None:
        first = prompt_signals("Hello   World")
        second = prompt_signals("hello world")
        empty = prompt_signals("")

        self.assertNotEqual(first["exact_sha256"], second["exact_sha256"])
        self.assertEqual(first["normalized_sha256"], second["normalized_sha256"])
        self.assertFalse(empty["has_text"])

    def test_cosine_requires_unit_norm(self) -> None:
        with self.assertRaises(ValueError):
            cosine(np.array([2.0, 0.0]), np.array([1.0, 0.0]))


if __name__ == "__main__":
    unittest.main()
