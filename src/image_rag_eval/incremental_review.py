"""Local-only retrieval evidence; never promotes candidate edges into approvals."""
from __future__ import annotations

import html
import itertools
import math
import re
from typing import Any

try:
    import numpy as _np
except ImportError:  # Optional acceleration; no package installation is required.
    _np = None


def _score_blocks(left_ids, right_ids, normalized, *, block_size=64):
    """Bounded float64 tiles; keep historical scalar rounding at half-unit edges."""
    if _np is None:
        for left in left_ids:
            for right in right_ids:
                yield left, right, round(sum(a * b for a, b in zip(normalized[left], normalized[right])), 6)
        return
    for offset in range(0, len(left_ids), block_size):
        left_block = left_ids[offset:offset + block_size]
        left_matrix = _np.asarray([normalized[i] for i in left_block], dtype=_np.float64)
        for column in range(0, len(right_ids), block_size):
            right_block = right_ids[column:column + block_size]
            right_matrix = _np.asarray([normalized[i] for i in right_block], dtype=_np.float64)
            scores = left_matrix @ right_matrix.T
            for row, left in enumerate(left_block):
                for col, right in enumerate(right_block):
                    value = float(scores[row, col])
                    scaled = value * 1_000_000
                    # BLAS summation order can differ below the sixth decimal place.
                    if abs(scaled - (math.floor(scaled) + .5)) <= 1e-7:
                        value = sum(a * b for a, b in zip(normalized[left], normalized[right]))
                    yield left, right, round(value, 6)


def compare_incremental_vectors(
    existing: dict[str, list[float]], incoming: dict[str, list[float]], *,
    active_existing_ids: list[str], dimension: int = 1024, threshold: float = 0.73,
) -> dict[str, Any]:
    """Compare every retained old member and every new/new pair, without chaining."""
    if type(dimension) is not int or dimension < 1:
        raise ValueError("dimension must be a positive integer")
    if type(threshold) not in (int, float) or not math.isfinite(threshold) or not -1 <= threshold <= 1:
        raise ValueError("threshold must be a finite cosine value")
    if set(incoming) & set(existing):
        raise ValueError("incremental vectors must contain new ids only")
    if len(incoming) > 300 or len(set(active_existing_ids)) != len(active_existing_ids):
        raise ValueError("invalid bounded input or duplicate active reference id")
    if any(ident not in existing for ident in active_existing_ids):
        raise ValueError("missing retained reference vector")
    normalized = {}
    for ident, values in {**existing, **incoming}.items():
        if len(values) != dimension or any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            raise ValueError("embedding dimension or finite-number contract failed")
        norm = math.sqrt(sum(v * v for v in values))
        if not math.isfinite(norm) or norm <= 0:
            raise ValueError("zero vector")
        normalized[ident] = [v / norm for v in values]

    incoming_ids = list(incoming)
    best = {ident: [] for ident in incoming_ids}
    for ident, ref, score in _score_blocks(incoming_ids, active_existing_ids, normalized):
        best[ident].append({"id": ref, "cosine": score})
        # At most four entries per row survive between candidates; tie order stays exact.
        best[ident].sort(key=lambda row: (-row["cosine"], row["id"]))
        del best[ident][3:]
    old_matches = []
    for ident in incoming_ids:
        ranked = best[ident]
        old_matches.append({"id": ident, "top3_existing": ranked[:3],
                            "priority_review": bool(ranked and ranked[0]["cosine"] >= threshold),
                            "identity_review_candidate": bool(ranked and ranked[0]["cosine"] >= 0.98)})
    new_edges = []
    incoming_order = {ident: index for index, ident in enumerate(incoming_ids)}
    for left, right, score in _score_blocks(incoming_ids, incoming_ids, normalized):
        if incoming_order[left] >= incoming_order[right]:
            continue
        if score >= threshold:
            new_edges.append({"left_id": left, "right_id": right, "cosine": score,
                              "identity_review_candidate": score >= 0.98})
    new_edges.sort(key=lambda row: (-row["cosine"], row["left_id"], row["right_id"]))
    old_matches.sort(key=lambda row: (-row["top3_existing"][0]["cosine"] if row["top3_existing"] else 0, row["id"]))
    return {"schema_version": "image-incremental-comparison-1", "status": "computer_candidates_only",
            "retained_reference_count": len(active_existing_ids), "new_vector_count": len(incoming),
            "old_new_comparisons": len(active_existing_ids) * len(incoming),
            "new_new_comparisons": len(incoming) * (len(incoming) - 1) // 2,
            "candidate_threshold": threshold, "candidate_threshold_is_probability": False,
            "old_matches": old_matches, "new_new_candidate_pairs": new_edges,
            "human_approved": False, "automatic_group_merges": 0, "automatic_deletions": 0,
            "metadata_calls": 0, "provider_calls_for_comparison": 0,
            "group_attachment_status": "pending_imported_current_human_decisions"}


def render_incremental_comparison(result: dict[str, Any], items: dict[str, dict[str, Any]]) -> str:
    for key in ("new_vector_count", "retained_reference_count"):
        if type(result.get(key)) is not int or result[key] < 0:
            raise ValueError("comparison counts must be nonnegative integers")
    def esc(value: Any) -> str:
        return html.escape(str(value), quote=True)

    def card(ident: str, caption: str) -> str:
        item = items[ident]
        if not re.fullmatch(r"(?:\.\./[A-Za-z0-9_-]+/)?inputs/[A-Za-z0-9_.-]+\.png", str(item["review_image_path"])):
            raise ValueError("comparison images must be local prepared inputs")
        return (f'<figure><img loading="lazy" src="{esc(item["review_image_path"])}" alt="{esc(item["style_id"])}">'
                f'<figcaption>{esc(item["style_id"])} · {esc(caption)}</figcaption></figure>')

    sections = []
    for row in result["old_matches"]:
        label = "동일 여부 우선 검토" if row["identity_review_candidate"] else ("유사도 우선 검토" if row["priority_review"] else "낮은 우선순위")
        comparisons = card(row["id"], "새 이미지")
        comparisons += "".join(card(match["id"], f'기존 유지 항목 · cosine {match["cosine"]:.4f}') for match in row["top3_existing"])
        sections.append(f'<section><h2>{esc(label)}</h2><div class="grid">{comparisons}</div></section>')
    new_pairs = "".join('<section><div class="grid">' + card(row["left_id"], "새 이미지")
                        + card(row["right_id"], f'새 이미지 · cosine {row["cosine"]:.4f}') + '</div></section>'
                        for row in result["new_new_candidate_pairs"])
    return f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer">
<title>CASE 신규 배치 · 컴퓨터 비교 후보</title><style>
*{{box-sizing:border-box}}body{{font:16px/1.6 system-ui,sans-serif;background:#f4f2ed;color:#172b2a;margin:0}}main{{max-width:1380px;margin:auto;padding:24px}}
h1{{font-size:28px}}h2{{font-size:18px}}section,.notice{{background:white;padding:16px;margin:20px 0;border:1px solid #c7ceca;border-radius:10px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,240px),1fr));gap:14px}}figure{{margin:0;min-width:0}}img{{width:100%;height:280px;object-fit:contain;background:#eeede9}}figcaption{{overflow-wrap:anywhere}}
</style><main><h1>신규 CASE 이미지 · 기존 유지 이미지와 비교</h1>
<p class="notice">새 이미지 {result['new_vector_count']}개 × 기존 유지 항목 {result['retained_reference_count']}개를 모두 비교했습니다.
표시된 기존 항목은 가장 가까운 멤버이며, 아직 확정된 유사 그룹 대표라고 보장하지 않습니다.
현재 검토 화면의 사람 선택을 확정 파일로 가져온 뒤 그룹 연결을 판단합니다. 이 페이지는 읽기 전용 후보이며 승인·삭제·태그 상속·공개 반영을 하지 않습니다.</p>
<p>신규→기존 상위 3개. cosine은 동일 확률이 아닙니다. 0.73은 검토 우선순위 기준입니다.</p>
{''.join(sections)}<h1>신규 이미지끼리의 추가 후보</h1><p>쌍별 후보만 기록하며, 연결되어 있다는 이유로 하나의 그룹으로 합치지 않습니다.</p>{new_pairs}
</main></html>'''


__all__ = ["compare_incremental_vectors", "render_incremental_comparison"]
