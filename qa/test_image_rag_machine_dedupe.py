from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from image_rag_eval.machine_dedupe import build_machine_retention


class MachineDedupeTests(unittest.TestCase):
    def source(self):
        return {"items": [
            {"id": "a", "style_id": "A", "sha256": "a" * 64, "signals": {"pixel_sha256": "c" * 64}, "prompt": "plain"},
            {"id": "b", "style_id": "B", "sha256": "b" * 64, "signals": {"pixel_sha256": "c" * 64}, "prompt": '{"structured": true}'},
        ], "retention": {"priority_by_id": {"a": {"rank_index": 2}, "b": {"rank_index": 1}}, "order_evidence": []}}

    def test_pixels_dedupe_different_prompts_keep_json_priority_and_aliases(self):
        source = self.source()
        original = copy.deepcopy(source)
        result = build_machine_retention(source)
        self.assertEqual(result["active_ids"], ["b"])
        self.assertEqual(result["deleted_ids"], ["a"])
        self.assertEqual(len(result["alias_lineage"][0]["aliases"]), 2)
        self.assertEqual({row["prompt"] for row in result["alias_lineage"][0]["aliases"]}, {"plain", '{"structured": true}'})
        self.assertEqual(source, original)

    def test_prompt_only_never_deletes(self):
        source = self.source()
        source["items"][0]["signals"]["pixel_sha256"] = "d" * 64
        source["items"][0]["prompt"] = source["items"][1]["prompt"]
        result = build_machine_retention(source)
        self.assertEqual(set(result["active_ids"]), {"a", "b"})
        self.assertEqual(result["deleted_ids"], [])
        self.assertEqual(len(result["prompt_variant_groups"]), 1)

    def test_file_only_exact_is_sufficient(self):
        source = self.source()
        for item in source["items"]:
            item["signals"] = {}
            item["sha256"] = "f" * 64
        self.assertEqual(build_machine_retention(source)["active_ids"], ["b"])

    def test_missing_hashes_do_not_match(self):
        source = self.source()
        for item in source["items"]:
            item.pop("sha256")
            item["signals"] = {}
        self.assertEqual(len(build_machine_retention(source)["active_ids"]), 2)

    def test_malformed_or_conflicting_evidence_rejected(self):
        source = self.source()
        source["items"][0]["sha256"] = "short"
        with self.assertRaises(ValueError):
            build_machine_retention(source)
        source = self.source()
        source["items"][0]["signals"]["sha256"] = "e" * 64
        with self.assertRaises(ValueError):
            build_machine_retention(source)
        source = self.source()
        source["items"][0]["pixel_sha256"] = "e" * 64
        with self.assertRaises(ValueError):
            build_machine_retention(source)


if __name__ == "__main__":
    unittest.main()
