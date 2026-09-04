from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from image_rag_eval.approved_library import build_prompt_catalog, project_approved_library


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


def sha(blob):
    return hashlib.sha256(blob).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded(value))


class LibraryProjectionTests(unittest.TestCase):
    def gallery(self):
        return {"run_id": "fixture", "commit_id": "committed", "revision": 12, "decisions_sha256": "decision-sha",
                "retained_ids": list("ABCDEF"), "items": [{"id": ident, "style_id": ident, "memo_text": "개인 메모 " + ident,
                                                          "rights_display": {"release_eligible": False}} for ident in "ABCDEF"],
                "groups": [{"candidate_id": "group-ab", "member_ids": list("AB"), "suggested_representative_id": "B"},
                           {"candidate_id": "group-cde", "member_ids": list("CDE"), "suggested_representative_id": "C",
                            "source_candidate_ids": ["group-cde", "old-cd"]}], "release_eligible": False}

    def test_committed_membership_becomes_whole_group_units(self):
        result = project_approved_library(self.gallery())
        self.assertEqual([row["member_ids"] for row in result["display_groups"]], [list("AB"), list("CDE")])
        self.assertEqual([row["representative_id"] for row in result["display_groups"]], ["B", "C"])
        self.assertEqual(result["ungrouped_ids"], ["F"])
        self.assertEqual(result["counts"], {"approved_images": 6, "display_groups": 2, "grouped_images": 5,
            "ungrouped_images": 1, "overlapping_images": 0, "source_human_groups": 2})
        self.assertEqual(result["source_commit_id"], "committed")
        self.assertFalse(result["release_eligible"])

    def test_partial_overlap_is_not_transitively_merged(self):
        gallery = self.gallery()
        gallery["groups"][1]["member_ids"] = list("BCD")
        result = project_approved_library(gallery)
        self.assertEqual([row["member_ids"] for row in result["display_groups"]], [list("AB"), list("BCD")])
        self.assertEqual(result["counts"]["overlapping_images"], 1)
        self.assertEqual(result["counts"]["grouped_images"], 4)

    def test_projection_does_not_recanonicalize_committed_subset_groups(self):
        gallery = self.gallery()
        gallery["groups"][1]["member_ids"] = list("ABC")
        result = project_approved_library(gallery)
        self.assertEqual(len(result["display_groups"]), 2)
        self.assertEqual(gallery["groups"][0]["member_ids"], list("AB"))

    def test_unchecked_representative_falls_back_without_reviving_hidden_image(self):
        gallery = self.gallery()
        gallery["items"] = [row for row in gallery["items"] if row["id"] != "C"]
        gallery["groups"][1]["representative_priority_ids"] = list("CED")
        result = project_approved_library(gallery)
        group = result["display_groups"][1]
        self.assertEqual(group["member_ids"], list("DE"))
        self.assertEqual(group["representative_id"], "E")
        self.assertEqual(group["hidden_member_count"], 1)
        self.assertNotIn("C", [row["id"] for row in result["items"]])

    def test_one_visible_group_member_returns_to_ungrouped_display_only(self):
        gallery = self.gallery()
        gallery["items"] = [row for row in gallery["items"] if row["id"] != "A"]
        result = project_approved_library(gallery)
        self.assertEqual(result["ungrouped_ids"], list("BF"))
        self.assertEqual(result["counts"]["source_human_groups"], 2)
        self.assertEqual(len(gallery["groups"]), 2)

    def test_no_groups_does_not_infer_similarity(self):
        gallery = self.gallery()
        gallery["groups"] = []
        result = project_approved_library(gallery)
        self.assertEqual(result["ungrouped_ids"], list("ABCDEF"))
        self.assertEqual(result["display_groups"], [])

    def test_inputs_and_personal_memos_remain_unchanged(self):
        gallery = self.gallery()
        before = copy.deepcopy(gallery)
        result = project_approved_library(gallery)
        self.assertEqual(gallery, before)
        self.assertEqual(result["items"][0]["memo_text"], "개인 메모 A")
        result["items"][0]["memo_text"] = "mutated output"
        self.assertEqual(gallery, before)

    def test_duplicate_or_unknown_member_ids_fail_closed(self):
        for members in (["A", "A"], ["A", "unknown"]):
            gallery = self.gallery()
            gallery["groups"][0]["member_ids"] = members
            with self.subTest(members=members), self.assertRaises(ValueError):
                project_approved_library(gallery)

    def test_duplicate_group_and_approved_ids_fail_closed(self):
        gallery = self.gallery()
        gallery["groups"].append(copy.deepcopy(gallery["groups"][0]))
        with self.assertRaisesRegex(ValueError, "group ID"):
            project_approved_library(gallery)
        gallery = self.gallery()
        gallery["items"].append(copy.deepcopy(gallery["items"][0]))
        with self.assertRaisesRegex(ValueError, "approved image"):
            project_approved_library(gallery)

    def test_portable_prompt_text_is_explicit_optin_and_not_mutated(self):
        catalog = {"A": {"id": "A", "status": "available", "full_prompt": "  {\n \"메모\":\"🧩\"\n}\n", "prompt_sha256": "sha"}}
        compact = project_approved_library(self.gallery(), catalog)
        self.assertEqual(compact["items"][0]["prompt_status"], "available")
        self.assertNotIn("original_prompt", compact["items"][0])
        portable = project_approved_library(self.gallery(), catalog, include_prompt_text=True)
        self.assertEqual(portable["items"][0]["original_prompt"]["full_prompt"], catalog["A"]["full_prompt"])


class PromptSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = "full-prompt-fixture"
        self.directory = self.root / f"data/private-research/image-rag-canary/runs/{self.run}/group-workflow-v1"
        self.original = '  {\n  "스타일": "스티커 🧩",\n  "text": "Unicode 원본"\n}\n'
        self.manifest_relative = "data/private-research/image-rag-canary/runs/manifest-fixture/manifest.json"
        self.canonical_relative = "data/canonical/archive_records.jsonl"
        self.manifest = {"schema_version": "1", "items": [{"id": "A", "style_id": "DAV490-019", "sha256": "a" * 64,
            "prepared_sha256": "b" * 64, "catalog_key": "source:A", "prompt": self.original,
            "embedding_prompt": "deliberately truncated", "prompt_truncated": True}]}
        self.canonical = {"catalog_key": "source:A", "style_id": "DAV490-019", "prompt": {"text": self.original}}
        self.spec = {"schema_version": "image-group-workflow-spec-1", "run_id": self.run, "items": [
            {"id": "A", "style_id": "DAV490-019", "source_sha256": "a" * 64, "prepared_sha256": "b" * 64}]}
        self.spec["spec_sha256"] = sha(encoded(self.spec))
        self.persist()

    def persist(self, *, include_canonical=True):
        write(self.root / self.manifest_relative, self.manifest)
        write(self.root / self.canonical_relative, self.canonical)
        files = [{"path": self.manifest_relative, "sha256": sha((self.root / self.manifest_relative).read_bytes())}]
        if include_canonical:
            files.append({"path": self.canonical_relative, "sha256": sha((self.root / self.canonical_relative).read_bytes())})
        binding = {"review_spec_sha256": self.spec["spec_sha256"], "files": files}
        write(self.directory / "image-group-workflow.spec.json", self.spec)
        write(self.directory / "source-bindings.json", binding)
        write(self.directory / "build-receipt.json", {"status": "ready", "run_id": self.run,
            "spec_sha256": self.spec["spec_sha256"], "binding_sha256": sha(encoded(binding))})

    def load(self):
        return build_prompt_catalog(self.root, self.spec)["A"]

    def test_original_json_whitespace_unicode_and_hash_are_preserved(self):
        result = self.load()
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["full_prompt"], self.original)
        self.assertEqual(result["prompt_sha256"], sha(self.original.encode("utf-8")))
        self.assertEqual(result["source_binding"]["origin"], "pinned_canonical.prompt.text")
        self.assertFalse(result["release_eligible"])
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_long_full_prompt_is_not_embedding_bounded(self):
        text = self.original + "아주 긴 원문\n" * 3000
        self.manifest["items"][0]["prompt"] = text
        self.canonical["prompt"]["text"] = text
        self.persist()
        self.assertEqual(self.load()["full_prompt"], text)
        self.assertGreater(len(text.encode("utf-8")), 6000)

    def test_embedding_truncation_flag_without_full_canonical_source_is_unavailable(self):
        self.persist(include_canonical=False)
        result = self.load()
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["full_prompt"])

    def test_untruncated_pinned_manifest_full_field_remains_supported(self):
        self.manifest["items"][0]["prompt_truncated"] = False
        self.persist(include_canonical=False)
        self.assertEqual(self.load()["full_prompt"], self.original)
        self.assertEqual(self.load()["source_binding"]["origin"], "pinned_manifest.prompt")

    def test_embedding_field_is_never_used_as_full_prompt(self):
        self.manifest["items"][0].pop("prompt")
        self.manifest["items"][0]["prompt_truncated"] = False
        self.persist(include_canonical=False)
        self.assertEqual(self.load()["status"], "unavailable")

    def test_pinned_manifest_and_canonical_disagreement_fails(self):
        self.manifest["items"][0]["prompt"] = "short incorrect value"
        self.persist()
        with self.assertRaisesRegex(ValueError, "disagree"):
            self.load()

    def test_original_catalog_byte_drift_fails(self):
        (self.root / self.canonical_relative).write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "catalog changed"):
            self.load()

    def test_manifest_byte_drift_fails(self):
        (self.root / self.manifest_relative).write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "manifest changed"):
            self.load()

    def test_image_identity_mismatch_fails(self):
        self.manifest["items"][0]["sha256"] = "c" * 64
        self.persist()
        with self.assertRaisesRegex(ValueError, "image identity"):
            self.load()

    def test_empty_prompt_is_missing_without_whitespace_normalization(self):
        self.manifest["items"][0]["prompt"] = " \n"
        self.canonical["prompt"]["text"] = " \n"
        self.persist()
        result = self.load()
        self.assertEqual(result["status"], "missing")
        self.assertEqual(result["full_prompt"], " \n")

    def test_spec_mutation_cannot_rebind_original_source(self):
        changed = copy.deepcopy(self.spec)
        changed["items"][0]["style_id"] = "OTHER"
        with self.assertRaisesRegex(ValueError, "binding mismatch"):
            build_prompt_catalog(self.root, changed)


if __name__ == "__main__":
    unittest.main()
