"""No providers or real DB: pure relation tests and isolated package fixtures."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from image_rag_eval import remaining_review as module
from image_rag_eval.experiment import digest, json_bytes, run_path


def item(ident, file="a", pixel="b", prompt="prompt", **extra):
    return {"id": ident, "style_id": ident, "sha256": file * 64,
            "signals": {"sha256": file * 64, "pixel_sha256": pixel * 64}, "prompt": prompt, **extra}


class RemainingExactTests(unittest.TestCase):
    def test_exact_file_uses_frozen_anchor_despite_better_json_prompt(self):
        old = [item("keeper", prompt="plain")]
        new = [item("new-json", prompt='{"subject":"cup","style":"photo","light":"soft"}')]
        before = copy.deepcopy((old, new))
        result = module.propose_exact_relations(new, old, ["keeper"], [])
        self.assertEqual(result["retained_new_candidate_ids"], [])
        proposal = result["exact_alias_proposals"][0]
        self.assertEqual(proposal["representative_id"], "keeper")
        self.assertEqual(proposal["proposed_alias_ids"], ["new-json"])
        self.assertFalse(proposal["existing_anchor_replaced"])
        self.assertEqual(proposal["action"], "proposed_logical_alias_not_applied")
        self.assertEqual((old, new), before)

    def test_exact_pixels_plus_exact_prompt_are_proposed_alias(self):
        result = module.propose_exact_relations([item("new", "c")], [item("old")], ["old"], [])
        self.assertEqual(result["retained_new_candidate_ids"], [])
        self.assertEqual(result["exact_alias_proposals"][0]["evidence_edges"][0]["kind"], "exact_pixels_and_prompt")

    def test_pixels_without_same_prompt_are_human_review_not_deleted(self):
        result = module.propose_exact_relations([item("new", "c", prompt="different")], [item("old")], ["old"], [])
        self.assertEqual(result["retained_new_candidate_ids"], ["new"])
        self.assertEqual(result["exact_alias_proposals"], [])
        self.assertEqual(result["human_groups"][0]["kind"], "same_pixels_different_prompt")
        self.assertFalse(result["human_groups"][0]["automatic_union"])

    def test_blank_prompts_do_not_qualify_pixel_alias_or_prompt_group(self):
        for prompt in ("", "   "):
            result = module.propose_exact_relations([item("new", "c", prompt=prompt)], [item("old", prompt=prompt)], ["old"], [])
            self.assertEqual(result["exact_alias_proposals"], [])
            self.assertEqual([x["kind"] for x in result["human_groups"]], ["same_pixels_different_prompt"])

    def test_prompt_only_is_one_full_sibling_group_and_never_deletion(self):
        old = [item("old")]
        new = [item("n1", "c", "d"), item("n2", "e", "f"), item("n3", "1", "2")]
        result = module.propose_exact_relations(new, old, ["old"], [])
        self.assertEqual(result["exact_alias_proposals"], [])
        self.assertEqual(result["retained_new_candidate_ids"], ["n1", "n2", "n3"])
        group = result["human_groups"][0]
        self.assertEqual(group["kind"], "prompt_exact_group_only")
        self.assertEqual(group["member_ids"], ["old", "n1", "n2", "n3"])

    def test_normalized_but_not_exact_prompt_does_not_trigger_pixel_deletion(self):
        result = module.propose_exact_relations([item("new", "c", prompt="PROMPT ")], [item("old")], ["old"], [])
        self.assertEqual(result["exact_alias_proposals"], [])
        self.assertEqual(result["retained_new_candidate_ids"], ["new"])

    def test_archived_hash_routes_through_chain_without_resurrection(self):
        old = [item("keeper", "a", "b"), item("alias-a", "c", "d"), item("alias-b", "e", "f")]
        aliases = [{"id": "alias-a", "representative_id": "keeper"}, {"id": "alias-b", "representative_id": "alias-a"}]
        result = module.propose_exact_relations([item("new", "e", "f", "newprompt")], old, ["keeper"], aliases)
        proposal = result["exact_alias_proposals"][0]
        self.assertEqual(proposal["representative_id"], "keeper")
        self.assertEqual(proposal["matched_archived_alias_ids"], ["alias-b"])
        self.assertEqual(result["retained_new_candidate_ids"], [])

    def test_new_exact_component_ranks_json_first_but_never_changes_input(self):
        new = [item("plain", "c", "d", "plain"), item("json", "c", "d", '{"subject":"cup","style":"photo","light":"soft"}')]
        result = module.propose_exact_relations(new, [item("old")], ["old"], [])
        self.assertEqual(result["retained_new_candidate_ids"], ["json"])
        self.assertEqual(result["exact_alias_proposals"][0]["proposed_alias_ids"], ["plain"])

    def test_multiple_committed_anchors_are_queued_not_merged(self):
        old = [item("old-a"), item("old-b")]
        result = module.propose_exact_relations([item("new")], old, ["old-a", "old-b"], [])
        self.assertEqual(result["exact_alias_proposals"], [])
        self.assertEqual(result["retained_new_candidate_ids"], [])
        self.assertEqual(result["anchor_conflicts"][0]["existing_anchor_ids"], ["old-a", "old-b"])
        self.assertEqual(result["anchor_conflicts"][0]["action"], "human_resolution_required")

    def test_invalid_partition_cycle_duplicate_and_missing_hash_fail_closed(self):
        old = [item("old"), item("alias", "c", "d")]
        for keepers, aliases in ((["old"], []), (["old"], [{"id": "alias", "representative_id": "alias"}]),
                                 (["old"], [{"id": "alias", "representative_id": "missing"}])):
            with self.assertRaises(ValueError):
                module.propose_exact_relations([item("new", "e", "f")], old, keepers, aliases)
        with self.assertRaises(ValueError):
            module.propose_exact_relations([item("old")], [item("old")], ["old"], [])
        with self.assertRaises(ValueError):
            module.propose_exact_relations([{**item("new"), "signals": {}}], [item("old")], ["old"], [])

    def test_comparison_counts_cover_all_archives_and_new_new(self):
        result = module.propose_exact_relations([item("n1", "e", "f"), item("n2", "1", "2")],
            [item("old"), item("alias", "c", "d")], ["old"], [{"id": "alias", "representative_id": "old"}])
        self.assertEqual(result["old_new_exact_comparisons"], 4)
        self.assertEqual(result["new_new_exact_comparisons"], 1)
        self.assertFalse(result["human_approval_inferred"])
        self.assertEqual(result["physical_deletions"], 0)


class RemainingPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "data/private-research/image-rag-admin/state.sqlite3"
        self.original = self.root / "source.json"
        self.original.write_bytes(b'{"immutable":true}')
        self.binding = {"files": [{"path": "source.json", "sha256": digest(self.original.read_bytes())}]}
        self.summary = {"source_commit_id": "a" * 64, "run_id": "new", "provider_calls": 0, "human_approvals_created": 0}
        self.payloads = {"manifest.json": b'{"human_review_status":"pending"}', "source-bindings.json": json_bytes(self.binding),
                         "review.html": b'<html>read-only proposal</html>', "inputs/" + "b" * 64 + ".png": b"fixture-image"}
        self.build = mock.patch.object(module, "_build_payloads", return_value=(self.payloads, self.binding, self.summary)).start()
        self.latest = mock.patch.object(module, "_require_latest").start()
        self.addCleanup(mock.patch.stopall)

    def prepare(self, **kwargs):
        return module.prepare_remaining_case_review(self.root, self.db, "old", "new", **kwargs)

    def test_dry_run_never_creates_destination_and_never_calls_provider(self):
        result = self.prepare()
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["writes"], 0)
        self.assertFalse((self.root / "data").exists())

    def test_apply_publishes_immutable_receipted_package_and_resume_is_unchanged(self):
        before = self.original.read_bytes()
        result = self.prepare(apply=True, expected_commit_id="a" * 64)
        destination = Path(result["package_path"])
        self.assertEqual(result["status"], "prepared_pending_human_review")
        receipt = json.loads((destination / "receipt.json").read_text())
        self.assertTrue(receipt["complete"])
        for name, sha in receipt["files"].items():
            self.assertEqual(digest((destination / name).read_bytes()), sha)
        # Production _build_payloads returns a fresh dict per call.
        self.payloads.pop("receipt.json")
        repeated = self.prepare(apply=True)
        self.assertEqual(repeated["status"], "unchanged")
        self.assertEqual(repeated["writes"], 0)
        self.assertEqual(self.original.read_bytes(), before)

    def test_changed_source_or_commit_blocks_publication(self):
        self.original.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "source evidence"):
            self.prepare(apply=True)
        self.assertFalse((run_path(self.root, "new") / module.DIRECTORY).exists())

    def test_wrong_expected_commit_blocks_without_writes(self):
        with self.assertRaisesRegex(ValueError, "expected commit"):
            self.prepare(apply=True, expected_commit_id="b" * 64)
        self.assertFalse((self.root / "data").exists())

    def test_live_commit_change_blocks_without_published_destination(self):
        self.latest.side_effect = ValueError("latest committed approval changed")
        with self.assertRaisesRegex(ValueError, "latest committed"):
            self.prepare(apply=True)
        self.assertFalse((run_path(self.root, "new") / module.DIRECTORY).exists())

    def test_existing_user_artifact_is_not_overwritten(self):
        destination = run_path(self.root, "new") / module.DIRECTORY
        destination.mkdir(parents=True)
        path = destination / "manifest.json"
        path.write_bytes(b"user-owned")
        with self.assertRaisesRegex(ValueError, "overwrite"):
            self.prepare(apply=True)
        self.assertEqual(path.read_bytes(), b"user-owned")

    def test_commit_change_after_staging_still_never_publishes_ready_package(self):
        self.latest.side_effect = [None, ValueError("latest committed approval changed")]
        with self.assertRaisesRegex(ValueError, "latest committed"):
            self.prepare(apply=True)
        self.assertFalse((run_path(self.root, "new") / module.DIRECTORY).exists())
        self.assertTrue(any(run_path(self.root, "new").glob(".remaining-review-*")))


class RemainingCacheAndHtmlTests(unittest.TestCase):
    def test_missing_requests_deduplicate_by_key_and_skip_exact_aliases(self):
        incoming = [item("one", prepared_sha256="1" * 64), item("same-prepared", prepared_sha256="1" * 64),
                    item("exact-alias", prepared_sha256="2" * 64)]
        def missing(root, requests):
            return {module.request_key(request): None for request in requests}
        with mock.patch.object(module, "lookup_shared_vectors", side_effect=missing):
            refs, requests, evidence = module._vector_inventory(Path("."), incoming, {"one", "same-prepared"})
        self.assertEqual(len(refs), 3)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["item_ids"], ["one", "same-prepared"])
        self.assertFalse(requests[0]["execution_authorized"])
        self.assertEqual(evidence, [])

    def test_successful_shared_cache_is_reused_and_bound_without_new_request(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            revision = "c" * 64
            relative = "data/private-research/image-rag-vector-cache/objects/vector.json"
            paths = [relative, f"{module.CACHE_ROOT}/revisions/{revision}/manifest.json",
                     f"{module.CACHE_ROOT}/revisions/{revision}/receipt.json"]
            for value in paths:
                path = root / value
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"{}")
            incoming = [item("cached", prepared_sha256="1" * 64)]
            def cached(root, requests):
                return {module.request_key(requests[0]): {"vector_sha256": "d" * 64,
                        "shared_object_path": relative, "shared_revision_id": revision}}
            with mock.patch.object(module, "lookup_shared_vectors", side_effect=cached):
                refs, requests, evidence = module._vector_inventory(root, incoming, {"cached"})
            self.assertEqual(refs["cached"]["status"], "cached")
            self.assertEqual(requests, [])
            self.assertEqual({row["path"] for row in evidence}, set(paths))

    def test_read_only_html_escapes_style_and_labels_archived_alias_without_resurrection(self):
        existing = item("old", review_image_path="../../old/inputs/" + "a" * 64 + ".png")
        new = item("new", review_image_path="inputs/" + "b" * 64 + ".png")
        new["style_id"] = '<script>alert("x")</script>'
        baseline = {"items": [existing], "approved_ids": [], "retained_ids": [],
                    "archived_aliases": [{"id": "old"}]}
        manifest = {"items": [new], "incoming_ids": ["new"]}
        review = {"exact_alias_proposals": [], "anchor_conflicts": [], "retained_new_candidate_ids": ["new"],
                  "human_groups": [{"kind": "prompt_exact_group_only", "member_ids": ["old", "new"]}]}
        rendered = module._render(manifest, baseline, review)
        self.assertIn("기존 보관 별칭 · 복원 안 함", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("아직 계산하지 않았습니다", rendered)
        self.assertNotIn("<form", rendered)
        new["review_image_path"] = "../../../../.env"
        with self.assertRaisesRegex(ValueError, "prepared local"):
            module._render(manifest, baseline, review)


if __name__ == "__main__":
    unittest.main()
