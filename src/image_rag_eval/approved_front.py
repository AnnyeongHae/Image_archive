"""Private, approval-only HTML consumer for validated group-workflow exports.

The output lives in group-workflow-v1/decision-imports/<digest>/private-front.html.
This is deliberately not the public archive or a public-rights approval.
"""
from __future__ import annotations

import html
import re
from typing import Any


def _escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _image_path(value: Any) -> str:
    path = str(value or "")
    # Only the already-bound local run inputs are allowed; no remote requests.
    if not re.fullmatch(r"\.\./inputs/[A-Za-z0-9_.-]+", path):
        raise ValueError("approved front requires a bound local run input path")
    return "../../" + path


def render_approved_front(front_export: dict[str, Any], approved_groups: dict[str, Any]) -> str:
    """Render only the validator's allowlist after sequencing gates.

    Legacy exports omit stage 3/4 status and retain their v1 final-review gate.
    For v2/v3, front_review_complete is derived compatibility data, not a fifth UI step.
    V3 uses per-image opt-outs after explicitly authorized default approval.
    """
    image_choice_v3 = front_export.get("decisions_schema_version") == "image-group-workflow-decisions-3"
    ready = (
        front_export.get("status") == "ready"
        and front_export.get("front_review_complete") is True
        and front_export.get("stage2_duplicate_gate_status") == "complete"
        and front_export.get("stage3_similarity_gate_status", None if image_choice_v3 else "complete") == "complete"
        and front_export.get("stage4_gate_status", None if image_choice_v3 else "unlocked") == "unlocked"
        and (not image_choice_v3 or front_export.get("front_approval_policy") == "default_retained_images_after_review_v1")
    )
    items = front_export.get("items", []) if ready else []
    if not isinstance(items, list):
        raise ValueError("front items must be an array")
    by_id: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise ValueError("front item requires an id")
        if item["id"] in by_id:
            raise ValueError("duplicate front item id")
        _image_path(item.get("prepared_path"))
        by_id[item["id"]] = item

    def card(item_id: str) -> str:
        item = by_id[item_id]
        tags = item.get("tags_texts", [])
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise ValueError("manual tags must be text strings")
        label = _escape(item.get("style_id") or item_id)
        tags_html = "".join(f"<p class=tag>{_escape(tag)}</p>" for tag in tags)
        if image_choice_v3:
            memo = item.get("memo_text", "")
            if not isinstance(memo, str):
                raise ValueError("personal memo must be text")
            tags_html = f'<p class="tag"><span>개인 메모 · </span>{_escape(memo)}</p>' if memo else ""
        return (
            f'<article class="image-card" data-item-id="{_escape(item_id)}">'
            f'<img loading="lazy" src="{_escape(_image_path(item["prepared_path"]))}" alt="{label}">'
            f'<h3>{label}</h3>{tags_html}</article>'
        )

    sections: list[str] = []
    grouped_ids: set[str] = set()
    groups = approved_groups.get("groups", []) if ready else []
    if ready and (
        approved_groups.get("run_id") != front_export.get("run_id")
        or approved_groups.get("spec_sha256") != front_export.get("spec_sha256")
    ):
        raise ValueError("approved groups and front export binding mismatch")
    if not isinstance(groups, list):
        raise ValueError("approved groups must be an array")
    if image_choice_v3:
        from .group_workflow import canonicalize_approved_groups

        groups = canonicalize_approved_groups(groups)
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("member_ids"), list):
            raise ValueError("approved group requires a member_ids array")
        if any(not isinstance(item_id, str) or not item_id for item_id in group["member_ids"]):
            raise ValueError("approved group member ids must be nonempty strings")
        # Group approval and individual front exclusion are independent. The
        # authoritative validator checks membership; this view intersects with
        # the narrower front allowlist and must never expand that allowlist.
        member_ids = list(dict.fromkeys(item_id for item_id in group["member_ids"] if item_id in by_id))
        if len(member_ids) < 2:
            continue
        grouped_ids.update(member_ids)
        representative = group.get("suggested_representative_id")
        if representative not in member_ids:
            representative = member_ids[0]
        others = [item_id for item_id in member_ids if item_id != representative]
        sections.append(
            '<section class="family">'
            + card(representative)
            + f'<details><summary>승인된 유사 이미지 {len(others)}개 펼쳐보기 · 전체 {len(member_ids)}개</summary>'
            + '<div class="grid">' + "".join(card(item_id) for item_id in others) + '</div></details></section>'
        )
    for item_id in by_id:
        if item_id not in grouped_ids:
            sections.append('<section class="family">' + card(item_id) + '</section>')

    empty_message = ("1~3단계 검토 완료 후 유지 이미지는 기본 승인됩니다. 개별 이미지의 체크를 해제할 수 있고 개인 메모는 선택사항입니다."
                     if image_choice_v3 else "아직 노출할 승인 이미지가 없습니다. 1~3단계를 마친 뒤 4단계에서 승인하세요. 태그는 비워도 됩니다.")
    empty = "" if by_id else f'<p class="empty">{empty_message}</p>'
    run_id = _escape(front_export.get("run_id"))
    status = "승인 목록 준비됨" if ready else "노출 잠김 — 승인 대기"
    metadata_notice = "개인 메모는 선택사항이며 자동 메타데이터 태그가 아닙니다." if image_choice_v3 else "태그는 사람이 작성한 내용만 표시합니다."
    return f'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>승인 이미지 · 비공개 프론트</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#f4f2ed;color:#172b2a;font-family:system-ui,sans-serif;line-height:1.6}}
main{{max-width:1320px;margin:auto;padding:28px 20px}}h1{{margin:0;font-size:clamp(24px,4vw,36px)}}
.notice,.empty{{padding:16px;background:#fff;border:1px solid #c7ceca;border-radius:10px}}.muted{{color:#586763;overflow-wrap:anywhere}}
.catalog{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr));gap:20px;margin-top:22px;align-items:start}}
.family{{background:white;border:1px solid #c7ceca;border-radius:12px;overflow:hidden;min-width:0}}
.image-card{{padding:14px;min-width:0}}img{{display:block;width:100%;height:280px;object-fit:contain;background:#eeede9}}
h3{{margin:8px 0 0;font-size:16px;overflow-wrap:anywhere}}.tag{{margin:6px 0;white-space:pre-wrap;overflow-wrap:anywhere;color:#425955}}
details{{border-top:1px solid #d4dad6}}summary{{padding:14px;cursor:pointer;color:#14574d;font-weight:650}}summary:focus-visible{{outline:3px solid #287cbe;outline-offset:-3px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,220px),1fr))}}
</style></head><body><main>
<h1>승인 이미지</h1><p>{status} · {len(by_id)}개</p>
<p class="notice">비공개 검토용 프론트입니다. 이 목록의 승인은 공개 배포·상업 이용·권리 승인이 아닙니다.</p>
<p class="muted">실행: {run_id} · {metadata_notice}</p>
{empty}<div class="catalog">{"".join(sections)}</div>
</main></body></html>'''


__all__ = ["render_approved_front"]
