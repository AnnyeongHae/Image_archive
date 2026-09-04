from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


ARCHIVE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = ARCHIVE_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from image_rag_eval.retention import build_retention  # noqa: E402
from image_rag_eval.prompt_priority import rank_prompt  # noqa: E402


def _item(
    item_id: str,
    *,
    prompt: str = "",
    sha256: str | None = None,
    pixel_sha256: str | None = None,
    arrival_at: str | None = None,
    arrival_basis: str | None = None,
    ordinal: int | None = None,
    source_name: str = "source",
    lane: str = "canonical",
    record_id: str | None = None,
    style_id: str | None = None,
    path: str | None = None,
    extra: dict | None = None,
) -> dict:
    item = {
        "id": item_id,
        "prompt": prompt,
        "sha256": sha256,
        "source_name": source_name,
        "lane": lane,
        "record_id": record_id or f"record-{item_id}",
        "style_id": style_id or f"style-{item_id}",
        "path": path or f"Reference/_derived/{item_id}.png",
        "signals": {},
    }
    if pixel_sha256 is not None:
        item["signals"]["pixel_sha256"] = pixel_sha256
    if arrival_at is not None:
        item["arrival_at"] = arrival_at
    if arrival_basis is not None:
        item["arrival_basis"] = arrival_basis
    if ordinal is not None:
        item["ordinal"] = ordinal
    if extra:
        item.update(extra)
    return item


class ImageRagRetentionTests(unittest.TestCase):
    def test_exact_file_pair_is_logically_deleted(self) -> None:
        items = [
            _item("A", sha256="same-file", prompt="alpha", arrival_at="2026-09-01T00:00:00Z"),
            _item("B", sha256="same-file", prompt="beta", arrival_at="2026-09-02T00:00:00Z"),
            _item("C", sha256="other-file", prompt="gamma", arrival_at="2026-09-03T00:00:00Z"),
        ]

        result = build_retention(items)

        self.assertEqual(result["active_ids"], ["A", "C"])
        self.assertEqual([row["id"] for row in result["archived"]], ["B"])
        self.assertEqual(result["archived"][0]["representative_id"], "A")
        self.assertEqual(result["archived"][0]["reasons"][0]["match_types"], ["exact_file"])
        self.assertEqual(result["archived"][0]["action"], "logical_delete")
        self.assertEqual(result["deleted_ids"], ["B"])
        self.assertEqual(result["schema_version"], "2")
        self.assertEqual(result["policy"], "logical_delete_exact_duplicate_retention_view_v2")
        self.assertTrue(result["reversible"])
        self.assertFalse(result["source_mutations"])
        self.assertEqual(result["priority_by_id"]["A"]["tier"], 4)
        self.assertEqual(result["priority_by_id"]["A"]["rank_index"], 1)
        self.assertEqual(result["priority_by_id"]["A"]["label"], "tier4_minimal_or_empty")
        self.assertEqual(result["priority_by_id"]["A"]["reason"], "minimal_text")
        self.assertEqual(result["priority_by_id"]["A"]["parse_status"], "not_json")
        self.assertEqual(result["priority_by_id"]["A"]["ordinal"], 1)

    def test_prompt_only_pair_stays_active_and_forms_prompt_variant_group(self) -> None:
        shared_prompt = "Hero shot with feature chips"
        items = [
            _item("CASE088", pixel_sha256="pixels-a", prompt=shared_prompt, ordinal=1),
            _item("CASE089", pixel_sha256="pixels-b", prompt=shared_prompt, ordinal=2),
        ]

        result = build_retention(items)

        self.assertEqual(result["active_ids"], ["CASE088", "CASE089"])
        self.assertEqual(result["archived"], [])
        self.assertEqual(len(result["prompt_variant_groups"]), 1)
        self.assertEqual(result["prompt_variant_groups"][0]["kind"], "prompt_variant")
        self.assertEqual(result["prompt_variant_groups"][0]["member_ids"], ["CASE088", "CASE089"])
        self.assertEqual(result["prompt_variant_groups"][0]["representative_id"], "CASE088")
        self.assertEqual(result["exact_groups"], [])

    def test_pixel_only_pair_stays_active(self) -> None:
        items = [
            _item("A", pixel_sha256="same-pixels", prompt="left", ordinal=1),
            _item("B", pixel_sha256="same-pixels", prompt="right", ordinal=2),
        ]

        result = build_retention(items)

        self.assertEqual(result["active_ids"], ["A", "B"])
        self.assertEqual(result["archived"], [])
        self.assertEqual(result["exact_groups"], [])

    def test_exact_pixels_and_prompt_exact_same_pair_is_logically_deleted(self) -> None:
        shared_prompt = "same prompt same pixels"
        items = [
            _item("A", pixel_sha256="same-pixels", prompt=shared_prompt, ordinal=1),
            _item("B", pixel_sha256="same-pixels", prompt=shared_prompt, ordinal=2),
        ]

        result = build_retention(items)

        self.assertEqual(result["active_ids"], ["A"])
        self.assertEqual([row["id"] for row in result["archived"]], ["B"])
        self.assertEqual(result["archived"][0]["reasons"][0]["match_types"], ["exact_pixels", "prompt_exact"])
        self.assertEqual(result["exact_groups"][0]["member_ids"], ["A", "B"])

    def test_blank_prompt_prevents_exact_pixels_delete(self) -> None:
        items = [
            _item("A", pixel_sha256="same-pixels", prompt="", ordinal=1),
            _item("B", pixel_sha256="same-pixels", prompt="", ordinal=2),
        ]

        result = build_retention(items)

        self.assertEqual(result["active_ids"], ["A", "B"])
        self.assertEqual(result["archived"], [])

    def test_bridge_safety_keeps_nonmatching_active_even_when_middle_item_is_deleted(self) -> None:
        shared_prompt = "same prompt"
        items = [
            _item("A", sha256="same-file", prompt="simple alpha", ordinal=1),
            _item("B", sha256="same-file", prompt=shared_prompt, ordinal=2),
            _item("C", pixel_sha256="bridge-pixels", prompt=shared_prompt, ordinal=3),
        ]

        result = build_retention(items)

        self.assertEqual(result["active_ids"], ["A", "C"])
        self.assertEqual([row["id"] for row in result["archived"]], ["B"])
        self.assertEqual(result["prompt_variant_groups"], [])

    def test_prompt_variant_group_requires_at_least_two_active_members(self) -> None:
        shared_prompt = "same prompt"
        items = [
            _item("A", sha256="same-file", prompt=shared_prompt, ordinal=1),
            _item("B", sha256="same-file", prompt=shared_prompt, ordinal=2),
            _item("C", pixel_sha256="other-pixels", prompt="different prompt", ordinal=3),
        ]

        result = build_retention(items)

        self.assertEqual(result["active_ids"], ["A", "C"])
        self.assertEqual(result["prompt_variant_groups"], [])

    def test_empty_prompts_do_not_create_prompt_variant_groups(self) -> None:
        items = [
            _item("A", prompt="", arrival_at="2026-09-01T00:00:00Z"),
            _item("B", prompt="", arrival_at="2026-09-02T00:00:00Z"),
        ]

        result = build_retention(items)

        self.assertEqual(result["active_ids"], ["A", "B"])
        self.assertEqual(result["archived"], [])
        self.assertEqual(result["prompt_variant_groups"], [])

    def test_missing_arrival_fields_are_explicit_unknown_with_fallback_ordinal(self) -> None:
        items = [
            _item("A", prompt="alpha"),
            _item("B", prompt="beta", extra={"first_queued_run_id": "run-1", "queued_from_run_id": "run-2"}),
        ]

        result = build_retention(items)
        order = {row["id"]: row for row in result["order_evidence"]}

        self.assertEqual(order["A"]["arrival_at"], "unknown")
        self.assertEqual(order["A"]["arrival_basis"], "fallback_no_explicit_arrival_evidence")
        self.assertEqual(order["A"]["ordinal"], 1)
        self.assertEqual(order["A"]["ordinal_basis"], "fallback_input_order")
        self.assertEqual(order["B"]["arrival_basis"], "fallback_no_explicit_arrival_timestamp_run_ids_only")
        self.assertEqual(order["B"]["run_ids"]["first_queued_run_id"], "run-1")

    def test_non_arrival_timestamps_do_not_drive_first_arrival_order(self) -> None:
        items = [
            _item("A", prompt="alpha", ordinal=1, extra={"updated_at": "2026-09-03T00:00:00Z"}),
            _item("B", prompt="beta", ordinal=2, extra={"updated_at": "2026-09-01T00:00:00Z"}),
        ]

        result = build_retention(items)
        order = {row["id"]: row for row in result["order_evidence"]}

        self.assertEqual(result["active_ids"], ["A", "B"])
        self.assertEqual(order["A"]["arrival_at"], "unknown")
        self.assertEqual(order["B"]["arrival_at"], "unknown")
        self.assertEqual(order["A"]["arrival_basis"], "fallback_no_explicit_arrival_evidence")
        self.assertEqual(order["B"]["arrival_basis"], "fallback_no_explicit_arrival_evidence")
        self.assertEqual(order["A"]["non_arrival_timestamps"]["updated_at"], "2026-09-03T00:00:00Z")
        self.assertEqual(order["B"]["non_arrival_timestamps"]["updated_at"], "2026-09-01T00:00:00Z")

    def test_invalid_arrival_timestamp_falls_back_to_ordinal_and_preserves_evidence_only(self) -> None:
        items = [
            _item(
                "A",
                prompt="alpha",
                ordinal=2,
                extra={
                    "arrival_at": "2026-09-03 00:00:00",
                    "approved_at": "2026-09-04T00:00:00Z",
                },
            ),
            _item("B", prompt="beta", ordinal=1),
        ]

        result = build_retention(items)
        order = {row["id"]: row for row in result["order_evidence"]}

        self.assertEqual(result["active_ids"], ["B", "A"])
        self.assertEqual(order["A"]["arrival_at"], "unknown")
        self.assertEqual(order["A"]["arrival_basis"], "fallback_invalid_arrival_timestamp")
        self.assertEqual(order["A"]["invalid_arrival_sources"], ["arrival_at"])
        self.assertEqual(order["A"]["non_arrival_timestamps"]["approved_at"], "2026-09-04T00:00:00Z")

    def test_first_seen_is_allowed_and_normalized_to_utc(self) -> None:
        items = [
            _item("A", prompt="alpha", extra={"first_seen_at": "2026-09-01T09:00:00+09:00"}),
            _item("B", prompt="beta", extra={"first_seen_at": "2026-09-01T00:30:00Z"}),
        ]

        result = build_retention(items)
        order = result["order_evidence"]

        self.assertEqual([row["id"] for row in order], ["A", "B"])
        self.assertEqual(order[0]["arrival_at"], "2026-09-01T00:00:00Z")
        self.assertEqual(order[0]["arrival_basis"], "item.first_seen_at")

    def test_later_json_priority_wins_over_earlier_exact_match(self) -> None:
        json_prompt = '{"type":"illustrated map infographic","style":"{argument name=\\"art style\\" default=\\"watercolor\\"}","layout":{"sections":[{"title":"food_spots","count":12}]}}'
        items = [
            _item("A", sha256="same-file", prompt="simple poster prompt", ordinal=1),
            _item("B", sha256="same-file", prompt=json_prompt, ordinal=2),
        ]

        result = build_retention(items)

        self.assertEqual(result["active_ids"], ["B"])
        self.assertEqual(result["archived"][0]["id"], "A")
        self.assertEqual(result["archived"][0]["representative_id"], "B")
        self.assertEqual(result["priority_by_id"]["B"]["tier"], 1)
        self.assertEqual(result["priority_by_id"]["B"]["variant"], "strict_json_template")
        self.assertEqual(result["priority_by_id"]["B"]["rank_index"], 1)
        self.assertEqual(result["priority_by_id"]["A"]["rank_index"], 2)
        self.assertEqual(rank_prompt(json_prompt)["tier"], 1)

    def test_chronology_breaks_ties_within_same_priority(self) -> None:
        items = [
            _item("A", sha256="same-file", prompt="poster", arrival_at="2026-09-01T00:00:00Z"),
            _item("B", sha256="same-file", prompt="poster", arrival_at="2026-09-02T00:00:00Z"),
        ]

        result = build_retention(items)

        self.assertEqual(result["active_ids"], ["A"])
        self.assertEqual(result["archived"][0]["representative_id"], "A")
        self.assertEqual(result["priority_by_id"]["A"]["rank_index"], 1)
        self.assertEqual(result["priority_by_id"]["B"]["rank_index"], 2)

    def test_exact_groups_are_logical_delete_groups_with_direct_pair_evidence(self) -> None:
        items = [
            _item("REP", sha256="same-file", prompt="poster", ordinal=1),
            _item("DROP1", sha256="same-file", prompt="poster alt", ordinal=2),
            _item("DROP2", pixel_sha256="px", prompt="same prompt", ordinal=3),
            _item("KEEP2", pixel_sha256="px", prompt="same prompt", ordinal=4),
        ]

        result = build_retention(items)

        self.assertEqual(len(result["exact_groups"]), 2)
        first = result["exact_groups"][0]
        self.assertTrue(first["kind"].startswith("exact"))
        self.assertEqual(first["action"], "logical_delete")
        self.assertIn("pairs", first["evidence"])
        self.assertNotIn("prompt_exact", [group["kind"] for group in result["exact_groups"]])

    def test_bst001_bst002_both_active_and_grouped(self) -> None:
        prompt = "BST shared prompt"
        items = [
            _item("BST001", pixel_sha256="bst-a", prompt=prompt, ordinal=1),
            _item("BST002", pixel_sha256="bst-b", prompt=prompt, ordinal=2),
        ]

        result = build_retention(items)

        self.assertEqual(result["active_ids"], ["BST001", "BST002"])
        self.assertEqual(result["prompt_variant_groups"][0]["member_ids"], ["BST001", "BST002"])

    def test_build_retention_does_not_mutate_input_items(self) -> None:
        items = [
            _item("A", sha256="same-file", arrival_at="2026-09-01T00:00:00Z"),
            _item("B", sha256="same-file", arrival_at="2026-09-02T00:00:00Z"),
        ]
        before = copy.deepcopy(items)

        build_retention(items)

        self.assertEqual(items, before)

    def test_duplicate_ids_raise(self) -> None:
        items = [
            _item("A", sha256="one"),
            _item("A", sha256="two"),
        ]

        with self.assertRaises(ValueError):
            build_retention(items)


if __name__ == "__main__":
    unittest.main()
