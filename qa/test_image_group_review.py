from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from image_rag_eval.group_review_ui import (  # noqa: E402
    GROUP_REVIEW_DECISIONS_SCHEMA_VERSION,
    GROUP_REVIEW_SPEC_SCHEMA_VERSION,
    render_group_review,
)
from image_rag_eval.group_workflow import validate_group_workflow_decisions  # noqa: E402


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9WnK8yQAAAAASUVORK5CYII="
)


def _fixture_spec(root: Path) -> dict:
    inputs_dir = root / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    items = []
    item_defs = [
        ("A", "CASE001"),
        ("B", "CASE002"),
        ("C", "CASE003"),
        ("D", "CASE004"),
        ("E", "BST001"),
        ("F", "BST002"),
    ]
    for index, (ident, style_id) in enumerate(item_defs, start=1):
        image_path = inputs_dir / f"{ident}.png"
        image_path.write_bytes(PNG_BYTES)
        items.append(
            {
                "id": ident,
                "style_id": style_id,
                "prepared_path": f"../inputs/{ident}.png",
                "source_sha256": f"source-{ident}",
                "prepared_sha256": f"prepared-{ident}",
                "prompt_sha256": f"prompt-{ident}",
                "priority": {"rank_index": index, "tier": 1, "label": f"p{index}"},
            }
        )
    return {
        "schema_version": GROUP_REVIEW_SPEC_SCHEMA_VERSION,
        "approval_policy": "default_retained_images_after_review_v1",
        "spec_sha256": "spec-fixture-001",
        "run_id": "fixture-group-review",
        "source_manifest_sha256": "manifest-fixture-001",
        "vector_fingerprint": "voyage-fixture-001",
        "source_labels_sha256": "labels-fixture-001",
        "created_at": "2026-09-03T10:00:00Z",
        "items": items,
        "stage1": {
            "active_ids": ["A", "B", "C", "D", "E"],
            "archived": [{"id": "F", "representative_id": "E", "reason": "existing duplicate"}],
            "alias_lineage": [{"from": "F", "to": "E"}],
            "policy": "retention-v1",
        },
        "duplicate_candidates": [
            {
                "id": "dup-main",
                "member_ids": ["A", "B", "C", "D"],
                "suggested_representative_id": "A",
                "evidence": {"kind": "pixel_or_identity_candidate"},
            }
        ],
        "similarity_candidates": [
            {
                "id": "sim-main",
                "member_ids": ["A", "B", "C", "E"],
                "candidate_only": True,
                "evidence": {"kind": "voyage-high-similarity"},
                "known_positive_pairs": [{"left_id": "A", "right_id": "B", "human_label": "near_duplicate"}],
                "known_negative_pairs": [{"left_id": "A", "right_id": "C", "human_label": "unrelated"}],
            }
        ],
    }


class GroupReviewUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.spec = _fixture_spec(self.root)

    def test_render_contains_four_stage_flow_and_contract_copy(self) -> None:
        html = render_group_review(self.spec)
        self.assertIn("1단계 · 컴퓨터 정리 결과 확인", html)
        self.assertIn("2단계 · 동일 이미지 검토", html)
        self.assertIn("3단계 · 시각 유사 그룹 확정", html)
        self.assertIn("4단계 · 기본 승인 / 선택 메모", html)
        self.assertNotIn('id="stage-5"', html)
        self.assertNotIn('id="front-review-complete"', html)
        self.assertIn('id="stage-4" hidden', html)
        self.assertIn("기존 pair 사람 라벨은 참고 근거일 뿐", html)
        self.assertIn("선택하지 않은 나머지는 서로 다른 이미지임", html)
        self.assertIn("모두 비워도 됩니다", html)
        self.assertIn("그룹 안의 이미지도 개별 해제", html)

    def test_render_uses_bound_schema_and_defer_defaults(self) -> None:
        html = render_group_review(self.spec)
        self.assertIn(GROUP_REVIEW_DECISIONS_SCHEMA_VERSION, html)
        self.assertIn('value="defer" checked', html)
        self.assertIn("image-rag-group-review:fixture-group-review:spec-fixture-001", html)
        self.assertIn("손상된 로컬 초안을 자동 복원하지 못했습니다. 기존 초안은 삭제하지 않았습니다.", html)

    def test_render_includes_select_all_and_known_negative_guard(self) -> None:
        html = render_group_review(self.spec)
        self.assertIn("data-select-all-duplicate", html)
        self.assertIn("data-select-all-similarity", html)
        self.assertIn("known negative pair 충돌", html)
        self.assertIn("기존 decisions JSON 불러오기", html)
        self.assertIn("크게 보기", html)
        self.assertIn('value="keep_separate"', html)

    def test_draft_is_separate_from_approval_and_has_recovery_fallback(self) -> None:
        html = render_group_review(self.spec)
        self.assertIn("image-group-workflow-draft-3", html)
        self.assertIn("backupRawDraft(raw)", html)
        self.assertIn("autosaveAllowed=false", html)
        self.assertIn('id="export-json"', html)
        self.assertIn('id="import-pasted"', html)
        self.assertIn("document.body.appendChild(a)", html)
        self.assertIn("group-review.draft.json", html)
        self.assertIn("legacyPayload=clone(input)", html)
        self.assertIn("n.checked=raw.selected_ids.includes(n.value)", html)

    def test_render_sorts_similarity_cards_largest_first(self) -> None:
        spec = json.loads(json.dumps(self.spec))
        spec["similarity_candidates"] = [
            {"id": "sim-2", "member_ids": ["A", "B"], "candidate_only": True, "evidence": {}},
            {"id": "sim-4", "member_ids": ["A", "B", "C", "E"], "candidate_only": True, "evidence": {}},
            {"id": "sim-3", "member_ids": ["A", "B", "E"], "candidate_only": True, "evidence": {}},
        ]
        html = render_group_review(spec)
        self.assertLess(html.index("sim-4"), html.index("sim-3"))
        self.assertLess(html.index("sim-3"), html.index("sim-2"))

    def test_backend_validator_accepts_filtered_individual_approvals_with_nonempty_front(self) -> None:
        decisions = {
            "schema_version": GROUP_REVIEW_DECISIONS_SCHEMA_VERSION,
            "spec_sha256": self.spec["spec_sha256"],
            "run_id": self.spec["run_id"],
            "reviewer": "fixture-reviewer",
            "reviewed_at": "2026-09-03T12:00:00Z",
            "duplicate_reviews": [
                {
                    "candidate_id": "dup-main",
                    "decision": "same_image_subset",
                    "selected_ids": ["A", "B"],
                    "remainder_distinct": True,
                }
            ],
            "similarity_reviews": [
                {
                    "candidate_id": "sim-main",
                    "decision": "approve_selected",
                    "selected_ids": ["C", "E"],
                    "tags_text": "magazine",
                }
            ],
            "image_approvals": [
                {"id": "A", "approved": True, "memo_text": ""},
                {"id": "C", "approved": False, "memo_text": ""},
                {"id": "D", "approved": True, "memo_text": "상세페이지 헤더에 활용"},
                {"id": "E", "approved": True, "memo_text": ""},
            ],
            "metadata_optional": True,
        }
        normalized = validate_group_workflow_decisions(self.spec, decisions)
        self.assertEqual(normalized["private_front_export_status"], "ready")
        self.assertEqual({row["id"] for row in normalized["private_front_export_items"]}, {"A", "D", "E"})

    def test_new_policy_has_per_image_memo_and_subset_display_provenance(self) -> None:
        html = render_group_review(self.spec)
        self.assertIn('data-image-approval=', html)
        self.assertIn('data-image-memo=', html)
        self.assertIn("collapseApprovedGroups", html)
        self.assertIn("source_candidate_ids", html)
        self.assertIn("row.member_ids.every(id=>g.member_ids.includes(id))", html)
        self.assertIn("const defaultPolicy=spec.approval_policy", html)

    def test_baseline_is_readonly_and_context_anchors_are_initialized(self) -> None:
        spec = json.loads(json.dumps(self.spec))
        spec["baseline"] = {"read_only_ids": ["A"], "image_approvals": [
            {"id": "A", "approved": False, "memo_text": "기존 판단"}], "groups": []}
        spec["similarity_candidates"][0]["baseline_anchor_ids"] = ["A"]
        html = render_group_review(spec)
        self.assertIn("기존 승인 기록 · 읽기 전용", html)
        self.assertIn('id="baseline-context"', html)
        self.assertIn("기존 읽기 전용 승인/메모는 변경할 수 없습니다", html)
        self.assertIn('"selected_ids": ["A"]', html)

    def test_baseline_front_link_is_local_and_escaped(self) -> None:
        spec = json.loads(json.dumps(self.spec))
        spec["baseline_front_url"] = "baseline-front/current/private-front.html"
        self.assertIn("기존 승인 이미지 프론트 열기", render_group_review(spec))
        for url in ("https://untrusted.invalid/a", "javascript:alert(1)", "//untrusted.invalid/a", "C:/outside.html"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                render_group_review({**spec, "baseline_front_url": url})


if __name__ == "__main__":
    unittest.main()
