from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from src.github_sources.collect_public_repo import DEFAULT_FIXTURE, read_json
from src.github_sources.run_registry_observation import observe_registry


class GitHubDailyObservationTests(unittest.TestCase):
    def test_registry_observation_preserves_sources_and_reports_cross_source_aliases(self) -> None:
        fixture = read_json(DEFAULT_FIXTURE)
        registry = {
            "policy": {"allowlist_only": True},
            "sources": [
                {
                    "source_id": "one",
                    "repository": "example/one",
                    "enabled": True,
                    "prompt_path_globs": ["README*.md", "docs/*.md", "docs/**/*.md"],
                    "media_path_globs": ["assets/*.png", "assets/**/*.png"],
                    "observed_repository_license": "MIT",
                },
                {
                    "source_id": "two",
                    "repository": "example/two",
                    "enabled": True,
                    "prompt_path_globs": ["README*.md", "docs/*.md", "docs/**/*.md"],
                    "media_path_globs": ["assets/*.png", "assets/**/*.png"],
                    "observed_repository_license": "Apache-2.0",
                },
            ],
        }

        def fake_live(repository: str, *, get=None):
            payload = copy.deepcopy(fixture)
            payload["repository"]["full_name"] = repository
            payload["repository"]["html_url"] = f"https://github.com/{repository}"
            return payload, {}

        with patch("src.github_sources.run_registry_observation.live_fixture", side_effect=fake_live):
            result = observe_registry(registry, limit_per_source=100)
        self.assertEqual(result["source_count"], 2)
        self.assertEqual(result["candidate_count"], 6)
        self.assertGreater(result["cross_source_blob_alias_count"], 0)
        self.assertFalse(result["rights_policy"]["download_prompt_bodies"])
        self.assertFalse(result["rights_policy"]["download_images"])
        self.assertFalse(result["rights_policy"]["canonical_promotion"])


if __name__ == "__main__":
    unittest.main()
