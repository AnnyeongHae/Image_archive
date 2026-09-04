from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PLATFORM_ROOT / "src" / "github_sources" / "collect_public_repo.py"
SPEC = importlib.util.spec_from_file_location("github_source_collector", MODULE_PATH)
assert SPEC and SPEC.loader
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


class GitHubSourceCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = collector.read_json(collector.DEFAULT_FIXTURE)
        self.source = {
            "source_id": "offline-fixture",
            "repository": "example/reference-gallery",
            "prompt_path_globs": ["README*.md", "docs/*.md", "docs/**/*.md"],
            "media_path_globs": ["assets/*.png", "assets/**/*.png"],
            "observed_repository_license": "MIT",
        }

    def test_fixture_is_metadata_only_and_exact_blob_aliases_group(self) -> None:
        result = collector.build_result(self.source, self.fixture, mode="offline_fixture", headers={}, limit=100)
        self.assertEqual(result["counts"]["candidate_count"], 4)
        self.assertEqual(result["counts"]["unique_candidate_count"], 3)
        self.assertEqual(result["counts"]["exact_blob_alias_count"], 1)
        self.assertFalse(result["rights_policy"]["download_binaries"])
        self.assertFalse(result["rights_policy"]["repository_license_is_item_rights_clearance"])
        self.assertTrue(all(row["rights_status"] == "private_reference_only" for row in result["candidates"]))
        self.assertTrue(all(row["rights_tier"] == "P3" for row in result["candidates"]))
        self.assertTrue(all(row["portfolio_visibility"] == "admin_only" for row in result["candidates"]))
        self.assertTrue(all(row["binary_downloaded"] is False for row in result["candidates"]))

    def test_registry_is_allowlist_only(self) -> None:
        registry = collector.read_json(collector.DEFAULT_REGISTRY)
        self.assertTrue(registry["policy"]["allowlist_only"])
        with self.assertRaises(collector.CollectorError):
            collector.source_by_repository(registry, "untrusted/example")

    def test_explicit_report_does_not_require_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            status = collector.main(["--report", str(report), "--quiet"])
            self.assertEqual(status, 0)
            saved = collector.read_json(report)
            self.assertEqual(saved["mode"], "offline_fixture")
            self.assertNotIn("private_run_artifact", saved)

    def test_live_snapshot_resolves_real_commit_then_pinned_tree(self) -> None:
        commit, tree = "1" * 40, "2" * 40
        responses = [
            ({"private": False, "full_name": "example/repo", "default_branch": "feature/gallery"}, {}),
            ({"sha": commit, "commit": {"tree": {"sha": tree}}}, {}),
            ({"sha": tree, "tree": []}, {}),
            ({}, {}),
        ]
        with patch.object(collector, "api_get", side_effect=responses) as get:
            fixture, _ = collector.live_fixture("example/repo", get=get)
        self.assertEqual(fixture["commit_sha"], commit)
        self.assertEqual(fixture["tree_sha"], tree)
        paths = [call.args[0] for call in get.call_args_list]
        self.assertEqual(paths[1], "/repos/example/repo/commits/feature%2Fgallery")
        self.assertEqual(paths[2], f"/repos/example/repo/git/trees/{tree}?recursive=1")
        self.assertEqual(paths[3], f"/repos/example/repo/license?ref={commit}")

    def test_pinned_tree_mismatch_and_private_repository_fail_closed(self) -> None:
        with patch.object(collector, "api_get", return_value=({"private": True}, {})) as get:
            with self.assertRaises(collector.CollectorError):
                collector.live_fixture("example/repo", get=get)
            self.assertEqual(get.call_count, 1)
        with patch.object(collector, "api_get", side_effect=[
            ({"private": False, "full_name": "example/repo", "default_branch": "main"}, {}),
            ({"sha": "1" * 40, "commit": {"tree": {"sha": "2" * 40}}}, {}),
            ({"sha": "3" * 40, "tree": []}, {}),
        ]) as get:
            with self.assertRaises(collector.CollectorError):
                collector.live_fixture("example/repo", get=get)

    def test_redirect_is_not_followed(self) -> None:
        with self.assertRaises(collector.CollectorError):
            collector._NoRedirect().redirect_request(None, None, 302, "moved", {}, "https://other.example/")


if __name__ == "__main__":
    unittest.main()
