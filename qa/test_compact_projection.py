import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import qa.test_luna_compact as fixtures
from image_rag_eval.compact_projection import project_compact
from image_rag_eval.luna_analysis_import import LunaImportError


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        test = fixtures.CompactContractTests(); test.setUp()
        self.result, self.pinned = test.result, test.pinned
        self.result["visual"]["layout"] = ["정사각형 캔버스", "좌측 제품", "우측 제목", "하단의 가격 영역"]
        self.result["uses"][0]["evidence_refs"] = ["/visual/layout/0", "/visual/layout/1", "/visual/layout/2"]
        self.draft = {"style_id": "X-001", "visual": copy.deepcopy(self.result["visual"])}

    def run_projection(self):
        return project_compact(self.result, self.draft, self.pinned, expected_style_id="X-001", original_prompt="original")

    def test_lossless_immutable_join_and_reference_mapping(self):
        original = copy.deepcopy(self.result)
        draft = copy.deepcopy(self.draft)
        projected = self.run_projection()
        self.assertEqual(self.result, original); self.assertEqual(self.draft, draft)
        self.assertEqual(projected["result"]["visual"]["layout"], ["정사각형 캔버스 · 좌측 제품", "우측 제목", "하단의 가격 영역"])
        self.assertEqual(projected["result"]["uses"][0]["evidence_refs"], ["/visual/layout/0", "/visual/layout/1"])
        self.assertTrue(projected["normalization"]["lossless_literal_join"])

    def test_reject_other_error(self):
        self.result["visual"]["medium"] = "invented"
        with self.assertRaises(LunaImportError): self.run_projection()

    def test_reject_prompt_changed_visual(self):
        self.draft["visual"]["subjects"] = ["different"]
        with self.assertRaises(LunaImportError): self.run_projection()

    def test_reject_long_join(self):
        self.result["visual"]["layout"][:2] = ["가" * 60, "나" * 60]
        self.draft["visual"] = copy.deepcopy(self.result["visual"])
        with self.assertRaises(LunaImportError): self.run_projection()

    def test_do_not_relax_source_evidence_contract(self):
        self.result["uses"][0]["evidence_refs"] = ["/visual/layout/3"]
        with self.assertRaises(LunaImportError): self.run_projection()

    def test_strict_valid_no_adapter(self):
        self.result["visual"]["layout"] = self.result["visual"]["layout"][:3]
        self.draft["visual"] = copy.deepcopy(self.result["visual"])
        self.assertIsNone(self.run_projection()["normalization"])

    def test_redundant_draft_envelope_preserved_in_original(self):
        self.result["visual"]["layout"] = self.result["visual"]["layout"][:3]
        self.draft = {"schema_version": "luna-compact-3", "style_id": "X-001", "visual": copy.deepcopy(self.result["visual"])}
        original = copy.deepcopy(self.draft)
        projected = self.run_projection()
        self.assertEqual(self.draft, original)
        self.assertEqual(projected["result"], self.result)
        self.assertEqual(set(projected["draft"]), {"style_id", "visual"})
        self.assertEqual(projected["draft"]["visual"], self.draft["visual"])
        self.assertEqual(projected["normalization"]["adapter_version"], "compact-draft-envelope-1")

    def test_envelope_adapter_does_not_hide_other_changes(self):
        self.result["visual"]["layout"] = self.result["visual"]["layout"][:3]
        valid = {"schema_version": "luna-compact-3", "style_id": "X-001", "visual": copy.deepcopy(self.result["visual"])}
        for change in ({"schema_version": "invented"}, {"extra": 1}, {"style_id": "X-002"}, {"visual": {**valid["visual"], "subjects": ["changed"]}}):
            with self.subTest(change=change):
                self.draft = {**valid, **change}
                with self.assertRaises(LunaImportError): self.run_projection()

    def test_envelope_and_layout_overflow_do_not_compose_implicitly(self):
        self.draft["schema_version"] = "luna-compact-3"
        with self.assertRaises(LunaImportError): self.run_projection()


if __name__ == "__main__": unittest.main()
