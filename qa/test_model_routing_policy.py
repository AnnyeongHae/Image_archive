from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src" / "model_routing_policy.py"
SPEC = importlib.util.spec_from_file_location("image_archive_model_routing_policy", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class ModelRoutingPolicyTests(unittest.TestCase):
    def test_deterministic_tasks_never_escalate_for_people_or_brands(self):
        decision = MODULE.route_task(
            "media_sha_indexing",
            contains_people=True,
            contains_brands=True,
            rights_sensitive=True,
        )
        self.assertEqual(decision.model, MODULE.MODEL_NONE)

    def test_people_taxonomy_stays_on_luna(self):
        decision = MODULE.route_task("taxonomy_backfill", contains_people=True, confidence=0.7)
        self.assertEqual(decision.model, MODULE.MODEL_LUNA)
        self.assertFalse(decision.escalation_required)

    def test_risk_review_uses_terra_until_final_release(self):
        draft = MODULE.route_task(
            "rights_sensitive_release_review",
            rights_sensitive=True,
            final_release_decision=False,
        )
        final = MODULE.route_task(
            "rights_sensitive_release_review",
            rights_sensitive=True,
            final_release_decision=True,
        )
        self.assertEqual(draft.model, MODULE.MODEL_TERRA)
        self.assertEqual(final.model, MODULE.MODEL_SOL)
        self.assertTrue(final.escalation_required)

    def test_high_confidence_family_naming_skips_model(self):
        decision = MODULE.route_task("prompt_family_naming", confidence=0.95)
        self.assertEqual(decision.model, MODULE.MODEL_NONE)


if __name__ == "__main__":
    unittest.main()
