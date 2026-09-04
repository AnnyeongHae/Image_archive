import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from image_rag_eval.luna_compact import contract, validate_compact, worker_message
from image_rag_eval.luna_analysis_import import LunaImportError


class CompactContractTests(unittest.TestCase):
    def setUp(self):
        self.pinned = contract(ROOT)
        self.result = {"schema_version": "luna-compact-3", "style_id": "X-001",
            "visual": {"caption_ko": "단색 배경의 병", "subjects": ["병"], "medium": "photograph",
                "styles": ["미니멀"], "background": {"setting": "plain", "detail_ko": "회색 바탕"},
                "layout": ["중앙의 병과 좌측 여백"], "editability": {"level": "moderate", "note_ko": "병 경계가 명확하다"},
                "ocr": {"status": "none", "excerpt": "", "note_ko": ""}, "uncertainties": []},
            "prompt": {"purpose_ko": "제품 소개", "invariants": [], "slots": [], "conflicts": [], "not_assessable": []},
            "uses": [{"use_case_id": "commerce.product_hero", "priority": "primary", "reuse_mode": "layout_reference",
                "fit": "supported", "basis": "image", "evidence_refs": ["/visual/layout/0"],
                "why_ko": "제품과 제목을 배치할 수 있다", "changes": ["병과 제목 교체"], "constraints": []}],
            "abstention_reason_ko": None, "extras_json": {}}

    def test_valid_result_and_draft(self):
        validate_compact(self.result, self.pinned, expected_style_id="X-001",
            visual_draft={"style_id": "X-001", "visual": copy.deepcopy(self.result["visual"])})

    def test_invalid_evidence(self):
        for pointer in ("/visual/layout/2", "/prompt/purpose_ko", "/visual/uncertainties/0"):
            with self.subTest(pointer=pointer):
                row = copy.deepcopy(self.result)
                row["uses"][0]["evidence_refs"] = [pointer]
                with self.assertRaises(LunaImportError):
                    validate_compact(row, self.pinned, expected_style_id="X-001")

    def test_prompt_only_supported_is_rejected(self):
        self.result["uses"][0]["basis"] = "prompt"
        with self.assertRaises(LunaImportError):
            validate_compact(self.result, self.pinned, expected_style_id="X-001")

    def test_schema_rejects_bilingual_redundancy(self):
        self.result["visual"]["search_keywords_en"] = ["bottle"]
        with self.assertRaises(LunaImportError):
            validate_compact(self.result, self.pinned, expected_style_id="X-001")

    def test_draft_is_immutable(self):
        draft = {"style_id": "X-001", "visual": copy.deepcopy(self.result["visual"])}
        self.result["visual"]["subjects"] = ["고양이"]
        with self.assertRaises(LunaImportError):
            validate_compact(self.result, self.pinned, expected_style_id="X-001", visual_draft=draft)

    def test_abstention(self):
        self.result["uses"] = []
        with self.assertRaises(LunaImportError):
            validate_compact(self.result, self.pinned, expected_style_id="X-001")
        self.result["abstention_reason_ko"] = "적합한 시각 근거 없음"
        validate_compact(self.result, self.pinned, expected_style_id="X-001")

    def test_positive_conflict_requires_exact_quote(self):
        self.result["prompt"]["conflicts"] = [{"prompt_quote": "blue bottle", "visual_ref": "/visual/subjects/0", "reason_ko": "다른 색"}]
        with self.assertRaises(LunaImportError):
            validate_compact(self.result, self.pinned, expected_style_id="X-001", original_prompt="a red bottle")

    def test_stable_prefix_and_max_five(self):
        def assignment(i):
            return {"style_id": f"X-{i}", "prepared_image_path": f"{i}.png", "prompt_context_path": f"{i}.json",
                    "visual_draft_path": f"draft-{i}.json", "raw_result_path": f"result-{i}.json"}
        three = worker_message(self.pinned, [assignment(i) for i in range(3)])
        five = worker_message(self.pinned, [assignment(i) for i in range(5)])
        self.assertEqual(three[:len(self.pinned["prefix"])], five[:len(self.pinned["prefix"])])
        with self.assertRaises(LunaImportError):
            worker_message(self.pinned, [assignment(i) for i in range(6)])

    def test_extras_cannot_hide_approval(self):
        self.result["extras_json"] = {"rights": "cleared"}
        with self.assertRaises(LunaImportError):
            validate_compact(self.result, self.pinned, expected_style_id="X-001")


if __name__ == "__main__":
    unittest.main()
