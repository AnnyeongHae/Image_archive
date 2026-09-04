from __future__ import annotations

import sys
import unittest
from pathlib import Path


ARCHIVE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = ARCHIVE_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from image_rag_eval.results_view import render_results  # noqa: E402


def _manifest() -> dict:
    return {
        "title": "canary <view>",
        "items": [
            {
                "id": "img-1",
                "style_id": "DAV490-019",
                "prepared_path": "inputs/a.png",
                "prompt": "<script>alert(1)</script>",
                "review_status": "needs_review",
                "ordinal": 2,
            },
            {
                "id": "img-2",
                "style_id": "API-067",
                "prepared_path": r"D:\abs\folder\b.png",
                "prompt": "plain prompt",
                "review_status": "approved",
                "ordinal": 1,
            },
            {
                "id": "img-3",
                "style_id": "STRUCT-201",
                "prepared_path": "../escape/c.png",
                "prompt": "",
                "review_status": "needs_review",
                "ordinal": 3,
            },
        ],
    }


class ResultsViewTests(unittest.TestCase):
    def test_retention_exact_groups_display_without_embedding_groups(self) -> None:
        document = render_results(_manifest(), {
            "active_ids": ["img-1"], "archived": [],
            "priority_by_id": {"img-1": {"tier": 1, "label": "JSON tier1", "parse_status": "valid"}},
            "exact_groups": [{"group_id": "exact1", "kind": "exact_file", "member_ids": ["img-1", "img-2"]}],
        }, [], [])
        self.assertNotIn("논리삭제 대상 없음", document)
        self.assertIn("exact_file", document)
        self.assertIn("펼쳐보기", document)
        self.assertIn("JSON tier1", document)
        self.assertIn("valid", document)

    def test_render_results_sanitizes_prompt_and_adjusts_image_paths(self) -> None:
        html = render_results(
            _manifest(),
            {
                "active_ids": ["img-1", "img-2"],
                "archived": [],
                "priority_by_id": {
                    "img-1": {"tier": 1, "label": "JSON tier1", "reason": "대표 예시", "parse_status": "valid"},
                    "img-2": {"tier": 2, "label": "구조 우선", "reason": "structural fallback", "parse_status": "invalid"},
                },
            },
            [],
            [],
        )

        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn('src="inputs/a.png"', html)
        self.assertIn('src="inputs/b.png"', html)
        self.assertIn("canary &lt;view&gt;", html)
        self.assertNotIn("D:\\abs\\folder\\b.png", html)
        self.assertIn("JSON tier1", html)
        self.assertIn("구조 우선", html)

    def test_render_results_separates_active_and_archived_items(self) -> None:
        html = render_results(
            _manifest(),
            {
                "active_ids": ["img-1"],
                "archived": [
                    {"id": "img-2", "representative_id": "img-1", "reasons": ["exact_pixels", "representative_hidden"]},
                ],
                "priority_by_id": {
                    "img-1": {"tier": 1, "label": "JSON tier1", "parse_status": "valid"},
                    "img-2": {"tier": 2, "label": "구조 우선", "parse_status": "invalid"},
                },
            },
            [],
            [],
        )

        active_start = html.index("<h2>활성 카드</h2>")
        archived_start = html.index("보관 항목 보기")
        self.assertIn("DAV490-019", html[active_start:archived_start])
        self.assertNotIn("API-067", html[active_start:archived_start])
        self.assertIn("대표 유지: DAV490-019", html)
        self.assertIn("exact_pixels, representative_hidden", html)
        self.assertIn("보관 항목 보기", html)

    def test_render_results_splits_logical_deletions_prompt_variants_and_visual_similar(self) -> None:
        html = render_results(
            _manifest(),
            {
                "active_ids": ["img-1", "img-2", "img-3"],
                "archived": [{"id": "img-2", "representative_id": "img-1", "reasons": ["exact_pixels"]}],
                "priority_by_id": {
                    "img-1": {"rank_index": 1, "tier": 1, "label": "JSON tier1", "reason": "대표 예시", "parse_status": "valid"},
                    "img-2": {"rank_index": 2, "tier": 2, "label": "구조 우선", "reason": "구조 안정", "parse_status": "invalid"},
                    "img-3": {"rank_index": 3, "tier": 2, "label": "구조 우선", "structural_rank": 2},
                },
                "exact_groups": [
                    {"kind": "exact_pixels", "member_ids": ["img-1", "img-2"], "status": "observed", "evidence": {"pixel_sha256": "abc"}},
                ],
                "prompt_variant_groups": [
                    {"kind": "prompt_variant", "member_ids": ["img-2", "img-3"], "status": "needs_review", "evidence": {"prompt_exact_sha256": "def"}},
                ],
            },
            [],
            [
                {
                    "kind": "visual_family_candidate",
                    "member_ids": ["img-1", "img-3"],
                    "status": "needs_review",
                    "evidence": {"method": "image_only_embedding_mutual_knn_complete_link"},
                    "soft_collection": True,
                    "threshold_calibrated": False,
                },
            ],
        )

        self.assertIn("<summary>삭제 대상(논리삭제)</summary>", html)
        self.assertIn("<summary>동일 프롬프트의 다른 결과</summary>", html)
        self.assertIn("<summary>시각 유사 그룹</summary>", html)
        self.assertIn("exact_pixels", html)
        self.assertIn("prompt_variant", html)
        self.assertIn("visual_family_candidate", html)
        self.assertIn("사람 검토 필요", html)
        self.assertIn("자동 병합 금지", html)
        self.assertIn("임계값 가설", html)
        self.assertIn("우선 대표: DAV490-019", html)
        self.assertIn("similarity 보관은 적용하지 않습니다", html)
        self.assertIn("prompt match 기준이며 시각 유사도 판단이 아닙니다", html)
        self.assertIn("공유 rank index 1 우선 적용", html)
        deletion_start = html.index("<summary>삭제 대상(논리삭제)</summary>")
        prompt_start = html.index("<summary>동일 프롬프트의 다른 결과</summary>")
        self.assertIn("보관", html[deletion_start:prompt_start])
        self.assertIn("API-067", html[deletion_start:prompt_start])

    def test_render_results_uses_shared_rank_index_before_fallbacks(self) -> None:
        html = render_results(
            _manifest(),
            {
                "active_ids": ["img-1", "img-2"],
                "archived": [],
                "priority_by_id": {
                    "img-1": {"rank_index": 2, "tier": 1, "label": "JSON tier1", "parse_status": "valid"},
                    "img-2": {"rank_index": 1, "tier": 3, "label": "구조 우선", "parse_status": "invalid"},
                },
            },
            [],
            [{"kind": "semantic_related_candidate", "member_ids": ["img-1", "img-2"], "status": "needs_review", "soft_collection": True}],
        )

        self.assertIn("대표 표시는 우선순위 순서를 먼저 따르고", html)
        self.assertIn("우선 대표: API-067", html)
        self.assertIn("rank 1", html)

    def test_render_results_uses_ordinal_only_for_priority_ties(self) -> None:
        html = render_results(
            _manifest(),
            {
                "active_ids": ["img-1", "img-2"],
                "archived": [],
                "priority_by_id": {
                    "img-1": {"tier": 2, "label": "구조 우선", "parse_status": "invalid"},
                    "img-2": {"tier": 2, "label": "구조 우선", "parse_status": "invalid"},
                },
            },
            [],
            [{"kind": "semantic_related_candidate", "member_ids": ["img-1", "img-2"], "status": "needs_review", "soft_collection": True}],
        )

        self.assertIn("동률일 때만 입고 순서 증거를 보조 기준으로", html)
        self.assertIn("동률 시 ordinal 1 사용", html)

    def test_render_results_treats_legacy_prompt_exact_as_prompt_variant_fallback(self) -> None:
        html = render_results(
            _manifest(),
            {
                "active_ids": ["img-1", "img-2"],
                "archived": [],
                "exact_groups": [
                    {"kind": "prompt_exact", "member_ids": ["img-1", "img-2"], "status": "needs_review", "evidence": {"prompt_exact_sha256": "abc"}},
                ],
            },
            [],
            [],
        )

        deletion_start = html.index("<summary>삭제 대상(논리삭제)</summary>")
        variant_start = html.index("<summary>동일 프롬프트의 다른 결과</summary>")
        similar_start = html.index("<summary>시각 유사 그룹</summary>")
        self.assertNotIn("prompt_exact", html[deletion_start:variant_start])
        self.assertIn("prompt_exact", html[variant_start:similar_start])

    def test_render_results_keeps_prompt_variant_members_active(self) -> None:
        html = render_results(
            _manifest(),
            {
                "active_ids": ["img-1", "img-2"],
                "archived": [],
                "prompt_variant_groups": [
                    {"kind": "prompt_variant", "member_ids": ["img-1", "img-2"], "status": "needs_review"},
                ],
            },
            [],
            [],
        )

        variant_start = html.index("<summary>동일 프롬프트의 다른 결과</summary>")
        similar_start = html.index("<summary>시각 유사 그룹</summary>")
        self.assertIn("활성", html[variant_start:similar_start])
        self.assertNotIn("보관", html[variant_start:similar_start])

    def test_stale_group_never_relabels_a_deleted_member_as_active(self) -> None:
        html = render_results(_manifest(), {
            "active_ids": ["img-1"],
            "archived": [{"id": "img-2", "representative_id": "img-1", "reasons": ["exact_file"]}],
            "prompt_variant_groups": [{"kind": "prompt_variant", "member_ids": ["img-1", "img-2"]}],
        }, [], [])
        variant_start = html.index("<summary>동일 프롬프트의 다른 결과</summary>")
        similar_start = html.index("<summary>시각 유사 그룹</summary>")
        self.assertIn("보관", html[variant_start:similar_start])

    def test_render_results_shows_query_sections_and_read_only_labels(self) -> None:
        evaluations = [
            {
                "provider": "gemini",
                "arm": "A",
                "dimensions": 3072,
                "query_id": "q1",
                "query_text": "red sofa",
                "ranked": [
                    {"id": "img-2", "score": 0.91},
                    {"id": "img-1", "score": 0.88},
                    {"id": "img-3", "score": 0.55},
                    {"id": "img-9", "score": 0.30},
                    {"id": "img-8", "score": 0.20},
                ],
                "metrics_scope": "human_labeled_20_item_canary_only",
            }
        ]

        html = render_results(
            _manifest(),
            {
                "active_ids": ["img-1"],
                "archived": [],
                "priority_by_id": {"img-1": {"tier": 1, "label": "JSON tier1", "parse_status": "valid"}},
            },
            evaluations,
            [],
        )

        self.assertIn("질의 q1 · red sofa", html)
        self.assertIn("gemini · A · 3072d", html)
        self.assertIn("Top1 img-2", html)
        self.assertIn("Top3 제공", html)
        self.assertIn("Top5 제공", html)
        self.assertIn("cosine score는 절대 유사 확률이 아닙니다", html)
        self.assertIn("JSON 템플릿 parse 상태", html)
        self.assertNotIn("<button", html)

    def test_render_results_marks_partial_provider_run(self) -> None:
        evaluations = [
            {
                "provider": "voyage-multimodal-3.5",
                "arm": "voyage_image",
                "dimensions": 1024,
                "query_id": "q1",
                "query_text": "red sofa",
                "ranked": [{"id": "img-2", "score": 0.91}],
            }
        ]

        html = render_results(
            {"title": "partial run", "comparison_status": "partial_provider_run_429", "items": _manifest()["items"]},
            {"active_ids": ["img-1"], "archived": []},
            evaluations,
            [],
        )

        self.assertIn("부분 provider 실행 감지", html)
        self.assertIn("voyage-multimodal-3.5", html)
        self.assertIn("전체 비교나 최종 승자를 의미하지 않습니다", html)

    def test_render_results_shows_voyage_selected_without_claiming_partial_error(self) -> None:
        evaluations = [
            {
                "provider": "voyage-multimodal-3.5",
                "arm": "voyage_image",
                "dimensions": 1024,
                "query_id": "q1",
                "query_text": "red sofa",
                "ranked": [{"id": "img-2", "score": 0.91}],
            }
        ]

        html = render_results(
            {
                "title": "voyage selected",
                "items": _manifest()["items"],
                "selection_profile": {
                    "provider": "voyage",
                    "model": "voyage-multimodal-3.5",
                    "evaluation_arms": ["voyage_image"],
                    "gemini": "paused_by_user",
                },
            },
            {"active_ids": ["img-1"], "archived": []},
            evaluations,
            [],
        )

        self.assertIn("현재 표시 기준: voyage · voyage-multimodal-3.5", html)
        self.assertIn("표시 arm: voyage_image", html)
        self.assertIn("Gemini AB 비교는 사용자 선택으로 중단되어 아직 완료되지 않았습니다", html)
        self.assertIn("현재 화면은 Voyage 선택 결과를 보여 주며 최종 승자를 뜻하지 않습니다", html)
        self.assertNotIn("부분 provider 실행 감지", html)

    def test_render_results_merges_similar_groups_with_identical_member_sets(self) -> None:
        html = render_results(
            _manifest(),
            {
                "active_ids": ["img-1", "img-2"],
                "archived": [],
                "priority_by_id": {
                    "img-1": {"rank_index": 1, "tier": 1, "label": "JSON tier1", "parse_status": "valid"},
                    "img-2": {"rank_index": 2, "tier": 2, "label": "구조 우선", "parse_status": "invalid"},
                },
            },
            [],
            [
                {
                    "provider": "local-hash",
                    "group_id": "near-1",
                    "kind": "near_copy_candidate",
                    "member_ids": ["img-2", "img-1"],
                    "status": "needs_review",
                    "evidence": {"phash": 0, "dhash": 0},
                },
                {
                    "provider": "voyage-multimodal-3.5",
                    "group_id": "visual-1",
                    "kind": "visual_family_candidate",
                    "member_ids": ["img-1", "img-2"],
                    "status": "needs_review",
                    "soft_collection": True,
                    "threshold_calibrated": False,
                    "evidence": {"cosine": 0.999332},
                },
            ],
        )

        self.assertEqual(html.count('<details class="group">'), 1)
        self.assertIn("near_copy_candidate + visual_family_candidate", html)
        self.assertIn("local-hash", html)
        self.assertIn("voyage-multimodal-3.5", html)
        self.assertIn("near_copy_candidate", html)
        self.assertIn("visual_family_candidate", html)
        self.assertIn("우선 대표: DAV490-019", html)
        group_start = html.index('<details class="group">')
        self.assertLess(
            html.index("DAV490-019", group_start),
            html.index("API-067", group_start),
        )

    def test_render_results_keeps_distinct_similar_member_sets_separate(self) -> None:
        html = render_results(
            _manifest(),
            {"active_ids": ["img-1", "img-2", "img-3"], "archived": []},
            [],
            [
                {"kind": "near_copy_candidate", "member_ids": ["img-1", "img-2"], "status": "needs_review"},
                {"kind": "visual_family_candidate", "member_ids": ["img-1", "img-3"], "status": "needs_review"},
            ],
        )

        self.assertEqual(html.count('<details class="group">'), 2)

    def test_render_results_does_not_claim_never_executed_when_partial_without_evaluations(self) -> None:
        html = render_results(
            {"title": "partial cache", "comparison_status": "partial_provider_run_429", "items": _manifest()["items"]},
            {"active_ids": ["img-1"], "archived": []},
            [],
            [],
        )

        self.assertIn("검색 비교를 완성할 벡터가 아직 부족합니다", html)
        self.assertIn("부분 provider 실행 감지", html)
        self.assertNotIn("임베딩은 아직 실행되지 않았습니다", html)


if __name__ == "__main__":
    unittest.main()
