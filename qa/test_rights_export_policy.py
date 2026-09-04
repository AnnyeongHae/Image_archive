from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from build_canonical_archive import normalized_rights, public_record  # noqa: E402


def rights(status: str, release_eligible: bool) -> dict:
    return normalized_rights(
        status=status,
        release_eligible=release_eligible,
        license_data={"effective_spdx": None},
        risk_flags=[],
        prompt_present=True,
        media_present=True,
    )


def record(rights_value: dict) -> dict:
    return {
        "catalog_key": "manual:fixture",
        "content_sha256": "0" * 64,
        "lane": "manual",
        "record_id": "FIXTURE",
        "style_id": "FIXTURE-001",
        "parent_style_id": None,
        "title": "Fixture title",
        "source": {"name": "Fixture", "url": "https://example.invalid/source", "type": "test", "repository": None, "commit": None, "pinned_url": None},
        "license": {"reported_spdx": None, "detected_spdx": None, "effective_spdx": None, "status": "not_verified", "scope": None, "evidence_url": None, "note": None},
        "rights": rights_value,
        "prompt": {"present": True, "text": "Fixture prompt", "sha256": "1" * 64, "format": "text", "language": "en"},
        "media": {"assets": [], "primary_role": None},
        "taxonomy": {"product_categories": [], "section_roles": [], "visual_techniques": [], "style_tags": [], "use_case_tags": [], "languages": [], "search_facets": {}},
        "review_release": {"review_status": "fixture", "release_eligible": rights_value["release_eligible"]},
    }


class RightsExportPolicyTests(unittest.TestCase):
    def test_four_tiers_have_distinct_visibility_and_admin_usage(self) -> None:
        p1 = rights("cleared", True)
        p2 = rights("public_metadata_link_only", True)
        p3 = rights("not_verified", False)
        p4 = rights("blocked", False)
        self.assertEqual((p1["rights_tier"], p1["portfolio_visibility"]), ("P1", "public"))
        self.assertEqual((p2["rights_tier"], p2["portfolio_visibility"]), ("P2", "metadata_link_only"))
        self.assertEqual((p3["rights_tier"], p3["portfolio_visibility"], p3["admin_usage_status"]), ("P3", "admin_only", "reference_allowed"))
        self.assertEqual((p4["rights_tier"], p4["portfolio_visibility"], p4["admin_usage_status"]), ("P4", "admin_only", "quarantine_only"))

    def test_only_p1_and_p2_can_be_projected(self) -> None:
        p1 = public_record(record(rights("cleared", True)))
        p2 = public_record(record(rights("public_metadata_link_only", True)))
        self.assertTrue(p1["prompt"]["text_included"])
        self.assertFalse(p2["prompt"]["text_included"])
        self.assertEqual(p2["media"]["assets"], [])
        for status in ("not_verified", "blocked"):
            with self.assertRaises(ValueError):
                public_record(record(rights(status, False)))


if __name__ == "__main__":
    unittest.main()
