"""Reuse-oriented Luna contract and local token-meter regressions."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from image_rag_eval.luna_analysis_import import validate_result_schema
from image_rag_eval.luna_reuse_analysis_import import LunaImportError, _validate_selection
from measure_luna_token_usage import UsageError, measure


class ReuseAnalysisContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        workspace = Path(__file__).resolve().parents[2]
        cls.schema = json.loads((workspace / "00_CORE/schemas/image_luna_reuse_analysis_result.schema.json").read_text(encoding="utf-8"))

    def result(self):
        selection = {
            "use_case_id": "content.entry_cover", "reuse_mode": "layout_reference", "fit": "supported",
            "evidence_basis": "image", "why_usable_ko": "큰 초점과 제목 여백이 보임",
            "adaptation_ko": "제목과 대상을 교체", "constraints_ko": ["권리 별도 확인"],
            "visual_evidence_ko": ["중앙 대형 초점", "상단 여백"],
        }
        return {
            "schema_version": "image-luna-reuse-analysis-result-2", "task_id": "a" * 64,
            "item_id": "asset-1", "style_id": "CASE-1", "input_fingerprint": "b" * 64,
            "visual": {
                "description_ko": "중앙 대상과 여백이 있는 그래픽", "subjects": ["중앙 대상"],
                "medium": "graphic_design", "styles": ["편집 디자인"],
                "background": {"description_ko": "단색 배경", "setting": "plain", "removability": "easy", "evidence_ko": "경계가 선명함"},
                "layout": ["중앙 정렬"], "palette": ["검정"], "lighting": None, "copy_space": ["상단"],
                "editability": {"overall": "moderate", "separable_elements": ["제목"], "hard_constraints": [], "evidence_ko": "평면 미리보기만 확인"},
                "search_keywords_ko": ["표지"], "search_keywords_en": ["cover"],
                "text_visible": {"status": "none", "excerpt": "", "language_hints": [], "limitations": "글자 없음"},
                "uncertainties": [],
            },
            "prompt_analysis": {
                "intended_purpose_ko": "대표 화면", "fixed_rules": ["중앙 구성"], "replaceable_slots": [],
                "visually_supported": ["중앙 구성"], "mismatch_candidates": [], "not_assessable": [],
            },
            "usage_selection": {"primary": selection, "secondary": [], "abstention_reason_ko": None,
                                "taxonomy_proposals_not_indexed": []},
            "limitations": ["권리 미확인"], "metadata_human_approved": False,
            "review_status": "needs_review", "release_eligible": False,
        }

    def test_schema_requires_style_background_editability_and_usage(self):
        result = self.result()
        validate_result_schema(result, self.schema)
        for parent, key in ((result["visual"], "styles"), (result["visual"], "background"),
                            (result["visual"], "editability"), (result, "usage_selection")):
            removed = parent.pop(key)
            with self.assertRaises(LunaImportError):
                validate_result_schema(result, self.schema)
            parent[key] = removed

    def test_selection_is_normalized_and_prompt_only_cannot_be_supported(self):
        result = self.result()
        _validate_selection(result, {"content.entry_cover"})
        result["usage_selection"]["primary"]["use_case_id"] = "invented.unknown"
        with self.assertRaisesRegex(LunaImportError, "pinned taxonomy"):
            _validate_selection(result, {"content.entry_cover"})
        result = self.result()
        result["usage_selection"]["primary"]["evidence_basis"] = "prompt"
        with self.assertRaisesRegex(LunaImportError, "Prompt-only"):
            _validate_selection(result, {"content.entry_cover"})

    def test_abstention_is_explicit(self):
        result = self.result()
        result["usage_selection"] = {"primary": None, "secondary": [], "abstention_reason_ko": "근거 부족",
                                     "taxonomy_proposals_not_indexed": []}
        _validate_selection(result, {"content.entry_cover"})
        result["usage_selection"]["abstention_reason_ko"] = None
        with self.assertRaisesRegex(LunaImportError, "Abstention"):
            _validate_selection(result, {"content.entry_cover"})


class TokenMeterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "archive"
        self.run = self.root / "data/private-research/image-rag-admin/luna-analysis/run-1"
        self.run.mkdir(parents=True)
        tasks = [{"style_id": "A"}, {"style_id": "B"}]
        (self.run / "tasks.json").write_text(json.dumps({
            "analysis_run_id": "run-1", "model_family": "gpt-5.6-luna",
            "worker_partition": "one_isolated_luna_session_per_image", "token_metering_required": True,
            "tasks": tasks,
        }), encoding="utf-8")

    def log(self, style: str, first: dict, second: dict | None = None, *, reset_each_turn: bool = False) -> Path:
        path = Path(self.temp.name) / f"{style}.jsonl"
        rows = [{"type": "session_meta", "payload": {"id": f"session-{style}", "agent_path": f"/root/luna_{style.lower()}", "model_provider": "openai"}}]
        cumulative = {key: 0 for key in first}
        for index, delta in enumerate([first] + ([second] if second else []), 1):
            cumulative = dict(delta) if reset_each_turn else {key: cumulative[key] + delta[key] for key in cumulative}
            rows.extend([
                {"type": "turn_context", "payload": {"turn_id": f"turn-{style}-{index}", "model": "gpt-5.6-luna", "effort": "high"}},
                {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": cumulative, "last_token_usage": delta}}},
                {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": f"turn-{style}-{index}", "started_at": index, "completed_at": index + 1}},
            ])
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        return path

    @staticmethod
    def usage(input_tokens, cached, output, reasoning):
        return {"input_tokens": input_tokens, "cached_input_tokens": cached, "cache_write_input_tokens": 0,
                "output_tokens": output, "reasoning_output_tokens": reasoning, "total_tokens": input_tokens + output}

    def test_sums_independent_sessions_without_double_counting_subsets(self):
        a = self.log("A", self.usage(100, 80, 20, 5), self.usage(50, 40, 10, 2))
        b = self.log("B", self.usage(70, 60, 15, 3))
        result = measure(self.root, "run-1", [f"A={a}", f"B={b}"], apply=True)
        self.assertEqual(result["usage"]["input_tokens_including_cached"], 220)
        self.assertEqual(result["usage"]["cached_input_tokens"], 180)
        self.assertEqual(result["usage"]["uncached_input_tokens_calculated"], 40)
        self.assertEqual(result["usage"]["output_tokens_including_reasoning"], 45)
        self.assertEqual(result["usage"]["reasoning_output_tokens"], 10)
        self.assertEqual(result["usage"]["total_tokens"], 265)
        receipt = json.loads((self.run / "token-usage-receipt.json").read_text(encoding="utf-8"))
        self.assertEqual({row["style_id"] for row in receipt["per_image"]}, {"A", "B"})
        self.assertEqual(measure(self.root, "run-1", [f"A={a}", f"B={b}"], apply=True)["status"], "unchanged")

    def test_accepts_turn_counters_that_reset_on_followup(self):
        a = self.log("A", self.usage(100, 80, 20, 5), self.usage(50, 40, 10, 2), reset_each_turn=True)
        b = self.log("B", self.usage(70, 60, 15, 3))
        result = measure(self.root, "run-1", [f"A={a}", f"B={b}"])
        self.assertEqual(result["usage"]["total_tokens"], 265)

    def test_requires_exact_task_coverage_and_luna_model(self):
        a = self.log("A", self.usage(100, 0, 20, 0))
        with self.assertRaisesRegex(UsageError, "cover every"):
            measure(self.root, "run-1", [f"A={a}"])
        rows = [json.loads(line) for line in a.read_text().splitlines()]
        rows[1]["payload"]["model"] = "other-model"
        a.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        b = self.log("B", self.usage(70, 0, 10, 0))
        with self.assertRaisesRegex(UsageError, "non-Luna"):
            measure(self.root, "run-1", [f"A={a}", f"B={b}"])


if __name__ == "__main__":
    unittest.main()
