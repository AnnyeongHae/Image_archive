from __future__ import annotations

import sys
import unittest
from pathlib import Path


ARCHIVE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = ARCHIVE_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from image_rag_eval.prompt_priority import priority_sort_key, rank_prompt  # noqa: E402


DAV490_019_PROMPT = """{ "type": "illustrated map infographic", "style": "{argument name=\\"art style\\" default=\\"watercolor and ink hand-drawn illustration on vintage parchment\\"}", "title_section": { "text": "{argument name=\\"city name\\" default=\\"成都\\"} {argument name=\\"map title\\" default=\\"吃货暴走地图\\"}", "mascot": "cartoon red chili pepper wearing sunglasses and giving a thumbs up" }, "border": "{argument name=\\"border decoration\\" default=\\"vine of green leaves and red chili peppers\\"}", "layout": { "background": "textured beige parchment paper with yellow roads, blue rivers, and green park areas", "sections": [ { "title": "landmarks", "count": 6, "illustrations": ["traditional pavilion", "traditional monastery", "modern skyscraper with climbing panda", "tall TV tower", "traditional gate", "industrial buildings"], "labels": ["人民公园", "文殊院", "IFS", "339电视塔", "宽窄巷子", "东郊记忆"] }, { "title": "food_spots", "count": 12, "illustrations": ["mapo tofu", "dumplings in chili oil", "skewers in pot", "sticky rice balls", "egg baking cake", "nine-grid hotpot", "sweet potato noodles", "cold skewers", "spicy mixed dish", "covered tea bowl", "ice jelly dessert", "spicy rabbit heads"], "labels": ["1 陈麻婆豆腐", "2 钟水饺", "3 春熙路", "4 宽窄巷子·三大炮", "5 建设路·叶婆婆蛋烘糕", "6 玉林路·小龙坎火锅", "7 香香巷·肥肠粉", "8 武侯祠大街·钵钵鸡", "9 东郊记忆·冒椒火辣", "10 人民公园·鹤鸣茶社", "11 锦里古街·冰粉", "12 双流老妈兔头"] }, { "title": "图例", "position": "bottom-right", "count": 5, "items": ["red dot", "green house", "green tree", "blue line", "yellow double line"], "labels": ["美食地点", "地标景点", "公园绿地", "河流湖泊", "主要道路"] } ], "centerpiece": "giant panda sitting and eating bamboo", "bottom_right_extras": ["vintage compass rose with N, S, E, W", "disclaimer text '温馨提示：吃辣需谨慎，肠胃要保护~' with a red chili pepper icon"] } }"""


class ImageRagPromptPriorityTests(unittest.TestCase):
    def test_dav490_019_is_tier1_strict_json_template(self) -> None:
        result = rank_prompt(DAV490_019_PROMPT)

        self.assertEqual(result["tier"], 1)
        self.assertEqual(result["variant"], "strict_json_template")
        self.assertTrue(result["signals"]["is_valid_json"])
        self.assertGreaterEqual(result["signals"]["top_level_key_count"], 4)
        self.assertGreaterEqual(result["signals"]["template_control_count"], 3)
        self.assertGreaterEqual(result["signals"]["useful_named_leaf_controls"], 2)
        self.assertEqual(result["parse_status"], "valid")
        self.assertEqual(result["reason"], "strict_json_template")

    def test_explicit_sections_rank_as_tier2(self) -> None:
        prompt = """Goal: Create a cinematic anime classroom still.\nScene: Three students in a bright classroom.\nTypography: Poster-like Japanese title.\nLighting: Soft afternoon daylight."""

        result = rank_prompt(prompt)

        self.assertEqual(result["tier"], 2)
        self.assertEqual(result["variant"], "explicit_sections")

    def test_template_controls_without_json_rank_as_tier2(self) -> None:
        prompt = 'Create a widescreen key visual titled {argument name="title text" default="負けヒロインが多すぎる!"}. Scene: bright classroom. Style: polished TV anime.'

        result = rank_prompt(prompt)

        self.assertEqual(result["tier"], 2)
        self.assertIn(result["variant"], {"sectioned_template_controls", "template_controls"})
        self.assertEqual(result["parse_status"], "not_json")

    def test_descriptive_natural_language_ranks_as_tier3(self) -> None:
        prompt = "A hand-drawn Chengdu food map with warm watercolor textures, cute landmarks, twelve food spots, and playful travel notes."

        result = rank_prompt(prompt)

        self.assertEqual(result["tier"], 3)
        self.assertEqual(result["variant"], "descriptive_natural_language")

    def test_empty_or_minimal_prompt_ranks_last(self) -> None:
        empty = rank_prompt("")
        minimal = rank_prompt("poster")

        self.assertEqual(empty["tier"], 4)
        self.assertEqual(minimal["tier"], 4)
        self.assertEqual(empty["parse_status"], "empty")
        self.assertLess(priority_sort_key(rank_prompt(DAV490_019_PROMPT)), priority_sort_key(minimal))

    def test_empty_or_thin_json_is_not_tier1(self) -> None:
        self.assertEqual(rank_prompt("{}")["tier"], 4)
        self.assertEqual(rank_prompt("{}")["parse_status"], "valid_empty")
        self.assertEqual(rank_prompt("[]")["tier"], 4)
        self.assertEqual(rank_prompt('{"prompt":"one blob"}')["tier"], 4)
        self.assertEqual(rank_prompt('{"prompt":"one blob"}')["parse_status"], "valid_thin_wrapper")
        self.assertEqual(rank_prompt('{"a":null,"b":null}')["tier"], 4)

    def test_invalid_json_nonfinite_and_duplicate_keys_are_explicit(self) -> None:
        nonfinite = rank_prompt('{"a": NaN, "b": 1}')
        duplicate = rank_prompt('{"a": 1, "a": 2}')

        self.assertEqual(nonfinite["parse_status"], "invalid_nonfinite")
        self.assertEqual(duplicate["parse_status"], "invalid_duplicate_keys")
        self.assertEqual(nonfinite["tier"], 4)
        self.assertEqual(duplicate["tier"], 4)

    def test_fenced_json_object_is_supported_without_losing_tier1(self) -> None:
        prompt = """```json
{"layout":{"title":"hero"},"style":"{argument name=\\"style\\" default=\\"ink\\"}","labels":["A","B"]}
```"""

        result = rank_prompt(prompt)

        self.assertEqual(result["tier"], 1)
        self.assertEqual(result["parse_status"], "valid")
        self.assertTrue(result["signals"]["fenced_json"])

    def test_short_cjk_descriptive_prompt_can_reach_tier3(self) -> None:
        prompt = "手绘成都美食地图暖色水彩质感可爱地标与十二个小吃地点旅行说明"

        result = rank_prompt(prompt)

        self.assertEqual(result["tier"], 3)

    def test_non_json_wrapper_text_stays_non_json(self) -> None:
        prompt = "json style map prompt with title and cute icons"

        result = rank_prompt(prompt)

        self.assertEqual(result["parse_status"], "not_json")


if __name__ == "__main__":
    unittest.main()
