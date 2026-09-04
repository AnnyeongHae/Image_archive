from __future__ import annotations

import html
import json
import os
from typing import Any


MAX_GROUP_MEMBERS = 20


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _status_text(manifest: dict[str, Any]) -> str:
    return " ".join(
        _text(value).strip().casefold()
        for value in (
            manifest.get("status"),
            manifest.get("comparison_status"),
            (manifest.get("comparison") or {}).get("status") if isinstance(manifest.get("comparison"), dict) else None,
        )
        if _text(value).strip()
    )


def _escape(value: Any) -> str:
    return html.escape(_text(value), quote=True)


def _safe_rel_image(path_value: Any) -> str:
    raw = _text(path_value).strip().replace("\\", "/")
    if not raw:
        return ""
    drive, tail = os.path.splitdrive(raw)
    if drive:
        raw = tail.replace("\\", "/")
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if not parts:
        return ""
    safe_parts: list[str] = []
    for part in parts:
        if part == "..":
            continue
        safe_parts.append(part)
    if not safe_parts:
        return ""
    if safe_parts[0] != "inputs":
        safe_parts = ["inputs", safe_parts[-1]]
    return "/".join(safe_parts)


def _item_lookup(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    items = manifest.get("items") or []
    lookup: dict[str, dict[str, Any]] = {}
    for item in items:
        item_id = _text(item.get("id")).strip()
        if item_id and item_id not in lookup:
            lookup[item_id] = item
    return lookup


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _tier_label(tier: int | None) -> str:
    labels = {
        1: "Tier1 구조화 JSON",
        2: "Tier2 항목·섹션형",
        3: "Tier3 설명형",
        4: "Tier4 검토 필요",
    }
    if tier is None:
        return "우선순위 정보 없음"
    return labels.get(tier, f"Tier{tier}")


def _normalize_priority(raw: Any, item: dict[str, Any]) -> dict[str, Any]:
    info = raw if isinstance(raw, dict) else {}
    item_priority = item.get("prompt_priority") if isinstance(item.get("prompt_priority"), dict) else {}
    merged = {**item_priority, **info}
    if isinstance(item.get("priority"), dict):
        merged = {**item.get("priority"), **merged}

    tier = _positive_int(merged.get("tier")) or _positive_int(merged.get("priority_tier"))
    structural_rank = (
        _positive_int(merged.get("structural_rank"))
        or _positive_int(merged.get("priority_rank"))
        or _positive_int(merged.get("rank"))
    )
    rank_index = (
        _positive_int(merged.get("rank_index"))
        or _positive_int(merged.get("canonical_rank_index"))
        or _positive_int(merged.get("representative_rank"))
        or _positive_int(merged.get("priority_index"))
    )
    ordinal = _positive_int(merged.get("ordinal")) or _positive_int(item.get("ordinal"))
    parse_status = _text(
        merged.get("template_parse_status")
        or merged.get("parse_status")
        or merged.get("json_template_status")
    ).strip()
    label = _text(merged.get("label") or merged.get("priority_label")).strip()
    reason = _text(merged.get("reason") or merged.get("priority_reason")).strip()
    source = _text(merged.get("source") or merged.get("basis")).strip()
    if not label:
        if tier is not None:
            label = _tier_label(tier)
        elif structural_rank is not None:
            label = f"구조 우선 {structural_rank}"
        else:
            label = "우선순위 정보 없음"
    parse_rank = 2
    if parse_status.casefold() in {"valid", "ok", "json_valid", "json_template_valid"}:
        parse_rank = 0
    elif parse_status:
        parse_rank = 1
    return {
        "tier": tier,
        "structural_rank": structural_rank,
        "rank_index": rank_index,
        "ordinal": ordinal,
        "ordinal_basis": _text(item.get("ordinal_basis") or merged.get("ordinal_basis")).strip(),
        "parse_status": parse_status,
        "parse_rank": parse_rank,
        "label": label,
        "reason": reason,
        "source": source,
    }


def _priority_by_id(retention: dict[str, Any], lookup: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    raw_map = retention.get("priority_by_id")
    explicit = raw_map if isinstance(raw_map, dict) else {}
    result: dict[str, dict[str, Any]] = {}
    for item_id, item in lookup.items():
        result[item_id] = _normalize_priority(explicit.get(item_id), item)
    return result


def _priority_sort_key(priority: dict[str, Any], item_id: str) -> tuple[Any, ...]:
    explicit_rank = priority.get("rank_index")
    if explicit_rank is not None:
        return (0, explicit_rank, item_id)
    return (
        1,
        priority["tier"] if priority["tier"] is not None else 999,
        priority["parse_rank"],
        priority["structural_rank"] if priority["structural_rank"] is not None else 999,
        priority["ordinal"] if priority["ordinal"] is not None else 999999,
        item_id,
    )


def _priority_badges(priority: dict[str, Any]) -> str:
    badges = []
    rank_index = priority.get("rank_index")
    tier = priority.get("tier")
    structural_rank = priority.get("structural_rank")
    parse_status = priority.get("parse_status")
    if rank_index is not None:
        badges.append(_status_badge(f"rank {rank_index}", "good"))
    if tier is not None:
        badges.append(_status_badge(f"T{tier}", "good" if tier == 1 else "neutral"))
    if structural_rank is not None:
        badges.append(_status_badge(f"구조 {structural_rank}", "neutral"))
    if parse_status:
        tone = "good" if priority.get("parse_rank") == 0 else "warn"
        badges.append(_status_badge(parse_status, tone))
    return " ".join(badges)


def _priority_explanation(priority: dict[str, Any]) -> str:
    parts = []
    if priority.get("label"):
        parts.append(priority["label"])
    if priority.get("reason"):
        parts.append(priority["reason"])
    if priority.get("source"):
        parts.append(f"기준: {priority['source']}")
    if priority.get("rank_index") is not None:
        parts.append(f"공유 rank index {priority['rank_index']} 우선 적용")
    if priority.get("ordinal") is not None:
        basis = priority.get("ordinal_basis") or "unknown"
        parts.append(f"동률 시 ordinal {priority['ordinal']} 사용 ({basis})")
    return " · ".join(parts) if parts else "우선순위 설명 없음"


def _partial_provider_run_notice(manifest: dict[str, Any], evaluations: list[dict[str, Any]]) -> str:
    providers = sorted({_text(entry.get("provider")).strip() for entry in evaluations if _text(entry.get("provider")).strip()})
    query_ids = {_text(entry.get("query_id")).strip() for entry in evaluations if _text(entry.get("query_id")).strip()}
    expected_minimum = len(query_ids) * 3 if query_ids else 0
    status_text = _status_text(manifest)
    profile = manifest.get("selection_profile")
    selected_provider = _text(profile.get("provider")).strip() if isinstance(profile, dict) else ""
    gemini_state = _text(profile.get("gemini")).strip() if isinstance(profile, dict) else ""
    intentional_single_provider = (
        len(providers) == 1
        and selected_provider
        and providers[0].casefold().startswith(selected_provider.casefold())
        and gemini_state == "paused_by_user"
    )
    if intentional_single_provider and "429" not in status_text and "partial" not in status_text:
        return ""
    partial = (
        "partial" in status_text
        or "429" in status_text
        or (len(providers) == 1 and len(evaluations) > 0)
        or (expected_minimum > 0 and len(evaluations) < expected_minimum)
    )
    if not partial:
        return ""
    provider_text = ", ".join(providers) if providers else "unknown"
    return f"부분 provider 실행 감지: {provider_text}. 현재 결과만 표시하며 전체 비교나 최종 승자를 의미하지 않습니다."


def _evaluation_notice(manifest: dict[str, Any], evaluations: list[dict[str, Any]]) -> str:
    if evaluations:
        return "임베딩 결과는 정답 라벨 없는 소규모 실험이며 정확도나 승자를 뜻하지 않습니다."
    status_text = _status_text(manifest)
    if "partial" in status_text or "429" in status_text:
        return "검색 비교를 완성할 벡터가 아직 부족합니다. 현재는 준비된 로컬 결과와 그룹만 표시합니다."
    return "임베딩은 아직 실행되지 않았습니다. 아래 그룹은 로컬 해시 검사 결과입니다."


def _status_badge(label: str, tone: str = "neutral") -> str:
    return f'<span class="badge badge-{_escape(tone)}">{_escape(label)}</span>'


def _item_card(item: dict[str, Any], priority: dict[str, Any], *, archived: bool = False, preferred: bool = False) -> str:
    item_id = _text(item.get("id")).strip() or "unknown"
    style_id = _text(item.get("style_id")).strip() or item_id
    review_status = _text(item.get("review_status")).strip() or "unknown"
    prompt = _text(item.get("prompt")).strip()
    image_src = _safe_rel_image(item.get("prepared_path"))
    archived_badge = _status_badge("보관", "muted") if archived else _status_badge("활성", "good")
    image_html = (
        f'<img loading="lazy" src="{_escape(image_src)}" alt="{_escape(style_id)} 미리보기">'
        if image_src
        else '<div class="thumb thumb-empty" aria-hidden="true">이미지 없음</div>'
    )
    prompt_html = (
        f'<details><summary>프롬프트</summary><pre>{_escape(prompt)}</pre></details>'
        if prompt
        else '<p class="muted">프롬프트 없음</p>'
    )
    preferred_badge = _status_badge("우선 대표", "good") if preferred else ""
    return (
        '<article class="card">'
        f'{image_html}'
        '<div class="card-body">'
        f'<div class="card-meta">{archived_badge} {_status_badge(review_status, "neutral")} {preferred_badge} {_priority_badges(priority)}</div>'
        f'<h3>{_escape(style_id)}</h3>'
        f'<p class="mono">{_escape(item_id)}</p>'
        f'<p class="path">run 입력: {_escape(image_src or "없음")}</p>'
        f'<p class="muted">우선순위: {_escape(_priority_explanation(priority))}</p>'
        f"{prompt_html}"
        "</div></article>"
    )


def _group_summary(group: dict[str, Any]) -> str:
    kind = _group_kind_label(group)
    status = _text(group.get("status")).strip() or "unknown"
    member_ids = [member for member in group.get("member_ids", []) if isinstance(member, str)]
    pieces = [kind, f"{len(member_ids)}개"]
    if group.get("soft_collection") is True:
        pieces.append("soft")
    return " · ".join(pieces) + f" · {status}"


def _evidence_block(evidence: Any) -> str:
    if not evidence:
        return '<p class="muted">추가 근거 없음</p>'
    rendered = json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2)
    return f"<pre>{_escape(rendered)}</pre>"


def _normalized_member_ids(group: dict[str, Any]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for member in group.get("member_ids", []):
        if not isinstance(member, str):
            continue
        if member in seen:
            continue
        seen.add(member)
        normalized.append(member)
    return normalized


def _member_set_key(group: dict[str, Any]) -> tuple[str, ...]:
    return tuple(sorted(_normalized_member_ids(group)))


def _group_kind_label(group: dict[str, Any]) -> str:
    kinds = group.get("_merged_kinds")
    if isinstance(kinds, list):
        labels = [_text(kind).strip() for kind in kinds if _text(kind).strip()]
        if labels:
            return " + ".join(labels)
    return _text(group.get("kind")).strip() or "unknown"


def _group_source_badges(group: dict[str, Any]) -> str:
    providers = group.get("_merged_providers")
    if not isinstance(providers, list):
        provider = _text(group.get("provider")).strip()
        providers = [provider] if provider else []
    kinds = group.get("_merged_kinds")
    if not isinstance(kinds, list):
        kind = _text(group.get("kind")).strip()
        kinds = [kind] if kind else []
    badges = []
    for provider in providers:
        if provider:
            badges.append(_status_badge(provider, "neutral"))
    for kind in kinds:
        if kind:
            badges.append(_status_badge(kind, "muted"))
    return " ".join(badges)


def _group_evidence_payload(group: dict[str, Any]) -> Any:
    merged_records = group.get("_merged_evidence_records")
    if isinstance(merged_records, list) and merged_records:
        return {"records": merged_records}
    return group.get("evidence")


def _group_kinds(group: dict[str, Any]) -> list[str]:
    merged = group.get("_merged_kinds")
    if isinstance(merged, list):
        kinds = [_text(kind).strip() for kind in merged if _text(kind).strip()]
        if kinds:
            return kinds
    kind = _text(group.get("kind")).strip()
    return [kind] if kind else []


def _is_prompt_variant_group(group: dict[str, Any]) -> bool:
    kinds = {kind.casefold() for kind in _group_kinds(group)}
    return bool(kinds & {"prompt_variant", "prompt_exact"})


def _group_identity(group: dict[str, Any]) -> tuple[str, str]:
    return (
        _text(group.get("provider") or "local").strip() or "local",
        _text(group.get("group_id")).strip() or json.dumps(group, sort_keys=True, ensure_ascii=False),
    )


def _dedupe_groups(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    ordered: list[dict[str, Any]] = []
    for group in groups:
        key = _group_identity(group)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(group)
    return ordered


def _merge_similar_groups(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged_by_members: dict[tuple[str, ...], dict[str, Any]] = {}
    ordered_keys: list[tuple[str, ...]] = []
    passthrough: list[dict[str, Any]] = []
    for group in groups:
        if _text(group.get("kind")).startswith("exact") or _text(group.get("kind")) == "prompt_exact":
            passthrough.append(group)
            continue
        key = _member_set_key(group)
        if not key:
            passthrough.append(group)
            continue
        if key not in merged_by_members:
            merged = dict(group)
            merged["member_ids"] = _normalized_member_ids(group)
            merged["_merged_kinds"] = []
            merged["_merged_providers"] = []
            merged["_merged_evidence_records"] = []
            merged_by_members[key] = merged
            ordered_keys.append(key)
        target = merged_by_members[key]
        kind = _text(group.get("kind")).strip()
        provider = _text(group.get("provider") or "local").strip() or "local"
        if kind and kind not in target["_merged_kinds"]:
            target["_merged_kinds"].append(kind)
        if provider and provider not in target["_merged_providers"]:
            target["_merged_providers"].append(provider)
        target["_merged_evidence_records"].append(
            {
                "provider": provider,
                "kind": kind or "unknown",
                "status": _text(group.get("status")).strip() or "unknown",
                "group_id": _text(group.get("group_id")).strip() or None,
                "soft_collection": group.get("soft_collection"),
                "threshold_calibrated": group.get("threshold_calibrated"),
                "evidence": group.get("evidence"),
            }
        )
        if target.get("soft_collection") is not True and group.get("soft_collection") is True:
            target["soft_collection"] = True
        if group.get("threshold_calibrated") is False:
            target["threshold_calibrated"] = False
        if group.get("status") == "needs_review":
            target["status"] = "needs_review"
        if not _text(target.get("representative_id")).strip():
            representative_id = _text(group.get("representative_id")).strip()
            if representative_id:
                target["representative_id"] = representative_id
    merged_groups = [merged_by_members[key] for key in ordered_keys]
    return passthrough + merged_groups


def _preferred_member_id(
    group: dict[str, Any],
    lookup: dict[str, dict[str, Any]],
    priorities: dict[str, dict[str, Any]],
) -> str | None:
    explicit = _text(
        group.get("representative_id")
        or (group.get("evidence") or {}).get("representative_id")
        or (group.get("evidence") or {}).get("preferred_representative_id")
    ).strip()
    if explicit and explicit in lookup:
        return explicit
    member_ids = [member for member in group.get("member_ids", []) if isinstance(member, str) and member in lookup]
    if not member_ids:
        return None
    ranked = sorted(member_ids, key=lambda item_id: _priority_sort_key(priorities[item_id], item_id))
    return ranked[0]


def _group_details(
    group: dict[str, Any],
    lookup: dict[str, dict[str, Any]],
    priorities: dict[str, dict[str, Any]],
    *,
    archived_ids: set[str] | None = None,
) -> str:
    archived_ids = archived_ids or set()
    member_ids = sorted(
        [member for member in _normalized_member_ids(group) if member in lookup],
        key=lambda item_id: _priority_sort_key(priorities[item_id], item_id),
    )
    preferred_id = _preferred_member_id(group, lookup, priorities)
    cards = "".join(
        _item_card(
            lookup[member],
            priorities[member],
            archived=(member in archived_ids),
            preferred=(member == preferred_id),
        )
        for member in member_ids[:MAX_GROUP_MEMBERS]
    )
    summary_badges = [
        _status_badge(_text(group.get("status")) or "unknown", "neutral"),
        _status_badge("사람 검토 필요", "warn"),
    ]
    source_badges = _group_source_badges(group)
    if group.get("soft_collection") is True:
        summary_badges.append(_status_badge("자동 병합 금지", "warn"))
    if group.get("threshold_calibrated") is False:
        summary_badges.append(_status_badge("임계값 가설", "muted"))
    preferred_label = ""
    if preferred_id and preferred_id in lookup:
        preferred_label = f'<p class="muted">우선 대표: {_escape(_text(lookup[preferred_id].get("style_id")) or preferred_id)} · {_escape(_priority_explanation(priorities[preferred_id]))}</p>'
    return (
        '<details class="group">'
        f'<summary>{_escape(_group_summary(group))}</summary>'
        f'<div class="group-meta">{source_badges} {" ".join(summary_badges)}</div>'
        f"{preferred_label}"
        f'<div class="grid">{cards or "<p class=\"muted\">표시 가능한 멤버 없음</p>"}</div>'
        '<details class="evidence"><summary>근거 보기</summary>'
        f"{_evidence_block(_group_evidence_payload(group))}"
        "</details></details>"
    )


def _evaluation_card(
    evaluation: dict[str, Any],
    lookup: dict[str, dict[str, Any]],
) -> str:
    provider = _text(evaluation.get("provider")).strip() or "provider"
    arm = _text(evaluation.get("arm")).strip() or "unknown-arm"
    dimensions = _text(evaluation.get("dimensions")).strip()
    query_id = _text(evaluation.get("query_id")).strip() or "query"
    query_text = _text(evaluation.get("query_text")).strip()
    ranked = evaluation.get("ranked") or []
    top_ids = [item.get("id") for item in ranked[:5] if isinstance(item, dict)]
    top_labels = []
    if top_ids:
        top_labels.append(_status_badge(f"Top1 {top_ids[0]}", "good"))
    if len(top_ids) >= 3:
        top_labels.append(_status_badge("Top3 제공", "neutral"))
    if len(top_ids) >= 5:
        top_labels.append(_status_badge("Top5 제공", "neutral"))
    rows = []
    for index, row in enumerate(ranked[:5], start=1):
        item_id = _text(row.get("id")).strip()
        score = _text(row.get("score")).strip()
        item = lookup.get(item_id, {})
        style_id = _text(item.get("style_id")).strip() or item_id
        image_src = _safe_rel_image(item.get("prepared_path"))
        thumb = (
            f'<img loading="lazy" src="{_escape(image_src)}" alt="" class="mini-thumb">'
            if image_src
            else '<span class="mini-thumb thumb-empty" aria-hidden="true"></span>'
        )
        rows.append(
            "<tr>"
            f"<td>{index}</td><td>{thumb}</td><td>{_escape(style_id)}</td>"
            f"<td class=\"mono\">{_escape(item_id)}</td><td>{_escape(score)}</td>"
            "</tr>"
        )
    metrics_scope = _text(evaluation.get("metrics_scope")).strip()
    heading = f"{provider} · {arm}"
    if dimensions:
        heading += f" · {dimensions}d"
    return (
        '<article class="eval-card">'
        f"<h4>{_escape(heading)}</h4>"
        f"<p class=\"mono\">{_escape(query_id)}</p>"
        f"<p>{_escape(query_text or '질의문 없음')}</p>"
        f'<div class="card-meta">{" ".join(top_labels) or _status_badge("결과 없음", "muted")}</div>'
        '<table><thead><tr><th>순위</th><th></th><th>Style</th><th>ID</th><th>score</th></tr></thead>'
        f"<tbody>{''.join(rows) or '<tr><td colspan=\"5\">결과 없음</td></tr>'}</tbody></table>"
        f"<p class=\"muted\">metrics_scope: {_escape(metrics_scope or 'none')}</p>"
        "</article>"
    )


def _query_sections(evaluations: list[dict[str, Any]], lookup: dict[str, dict[str, Any]]) -> str:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for evaluation in evaluations:
        key = (_text(evaluation.get("query_id")).strip(), _text(evaluation.get("query_text")).strip())
        grouped.setdefault(key, []).append(evaluation)
    sections = []
    for (query_id, query_text), entries in sorted(grouped.items()):
        ordered = sorted(
            entries,
            key=lambda item: (
                _text(item.get("provider")).strip(),
                _text(item.get("arm")).strip(),
                _text(item.get("dimensions")).strip(),
            ),
        )
        cards = "".join(_evaluation_card(entry, lookup) for entry in ordered)
        sections.append(
            '<details class="query-block" open>'
            f'<summary>질의 {_escape(query_id or "unknown")} · {_escape(query_text or "질의문 없음")}</summary>'
            f'<div class="grid eval-grid">{cards}</div>'
            "</details>"
        )
    return "".join(sections) or '<p class="muted">평가 결과 없음</p>'


def _selection_profile_notice(manifest: dict[str, Any], evaluations: list[dict[str, Any]]) -> str:
    profile = manifest.get("selection_profile")
    if not isinstance(profile, dict):
        return ""
    provider = _text(profile.get("provider")).strip()
    model = _text(profile.get("model")).strip()
    arms = [
        _text(arm).strip()
        for arm in (profile.get("evaluation_arms") or [])
        if _text(arm).strip()
    ]
    gemini_state = _text(profile.get("gemini")).strip()
    observed_providers = sorted({_text(entry.get("provider")).strip() for entry in evaluations if _text(entry.get("provider")).strip()})
    provider_label = provider or (observed_providers[0] if len(observed_providers) == 1 else "")
    parts = []
    if provider_label:
        line = f"현재 표시 기준: {provider_label}"
        if model:
            line += f" · {model}"
        parts.append(line)
    if arms:
        parts.append(f"표시 arm: {', '.join(arms)}")
    if gemini_state == "paused_by_user":
        parts.append("Gemini AB 비교는 사용자 선택으로 중단되어 아직 완료되지 않았습니다.")
    elif gemini_state:
        parts.append(f"Gemini 상태: {gemini_state}")
    if provider_label.casefold().startswith("voyage") or "voyage" in model.casefold():
        parts.append("현재 화면은 Voyage 선택 결과를 보여 주며 최종 승자를 뜻하지 않습니다.")
    return " ".join(parts)


def render_results(
    manifest: dict[str, Any],
    retention: dict[str, Any],
    evaluations: list[dict[str, Any]],
    similarity_groups: list[dict[str, Any]],
) -> str:
    lookup = _item_lookup(manifest)
    priorities = _priority_by_id(retention, lookup)
    manifest_items = list(lookup.values())
    active_ids = [item_id for item_id in retention.get("active_ids", []) if item_id in lookup]
    archived_entries = [entry for entry in retention.get("archived", []) if isinstance(entry, dict)]
    if not active_ids:
        archived_ids = {entry.get("id") for entry in archived_entries}
        active_ids = [item.get("id") for item in manifest_items if item.get("id") not in archived_ids]
    archived_id_set = {_text(entry.get("id")).strip() for entry in archived_entries if _text(entry.get("id")).strip()}

    active_cards = "".join(
        _item_card(lookup[item_id], priorities[item_id], archived=False)
        for item_id in active_ids
        if item_id in lookup
    )
    archived_blocks = []
    for entry in archived_entries:
        item_id = _text(entry.get("id")).strip()
        if item_id not in lookup:
            continue
        reasons = entry.get("reasons") or []
        representative_id = _text(entry.get("representative_id")).strip()
        representative = lookup.get(representative_id)
        priority = priorities[item_id]
        archived_blocks.append(
            '<details class="archive-block">'
            f'<summary>{_escape(item_id)} 보관</summary>'
            f'<p class="muted">대표 유지: {_escape(_text(representative.get("style_id")) if representative else representative_id or "없음")}</p>'
            f'<p>사유: {_escape(", ".join(_text(reason) for reason in reasons) or "없음")}</p>'
            f"{_item_card(lookup[item_id], priority, archived=True)}"
            "</details>"
        )

    raw_exact_groups = [group for group in retention.get("exact_groups", []) if isinstance(group, dict)]
    explicit_prompt_variant_groups = retention.get("prompt_variant_groups")
    has_explicit_prompt_variant_groups = isinstance(explicit_prompt_variant_groups, list)
    raw_prompt_variant_groups = [group for group in (explicit_prompt_variant_groups or []) if isinstance(group, dict)]
    logical_deletion_groups: list[dict[str, Any]] = []
    if has_explicit_prompt_variant_groups:
        logical_deletion_groups = raw_exact_groups
    else:
        for group in raw_exact_groups:
            if _is_prompt_variant_group(group):
                raw_prompt_variant_groups.append(group)
            else:
                logical_deletion_groups.append(group)
    logical_deletion_groups = _dedupe_groups(logical_deletion_groups)
    prompt_variant_groups = _dedupe_groups(raw_prompt_variant_groups)
    merged_similar_groups = _merge_similar_groups(_dedupe_groups([group for group in similarity_groups if isinstance(group, dict)]))
    deletion_html = "".join(
        _group_details(group, lookup, priorities, archived_ids=archived_id_set)
        for group in logical_deletion_groups
    ) or '<p class="muted">논리삭제 대상 없음</p>'
    prompt_variant_html = "".join(
        _group_details(group, lookup, priorities, archived_ids=archived_id_set)
        for group in prompt_variant_groups
    ) or '<p class="muted">동일 프롬프트 그룹 없음</p>'
    similar_html = "".join(
        _group_details(group, lookup, priorities, archived_ids=archived_id_set)
        for group in merged_similar_groups
    ) or '<p class="muted">시각 유사 그룹 없음</p>'
    query_html = _query_sections(evaluations, lookup)
    partial_provider_notice = _partial_provider_run_notice(manifest, evaluations)
    evaluation_notice = _evaluation_notice(manifest, evaluations)
    selection_profile_notice = _selection_profile_notice(manifest, evaluations)

    title = _text(manifest.get("title")).strip() or "이미지 RAG 결과"
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <title>{_escape(title)}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0f1320;
      --panel: #1a2235;
      --panel-2: #111827;
      --text: #e8eef9;
      --muted: #9fb0c9;
      --line: #33405c;
      --good: #1f7a4c;
      --warn: #8a5d10;
      --neutral: #33405c;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--text); font: 15px/1.5 system-ui, sans-serif; }}
    main {{ max-width: 1400px; margin: 0 auto; padding: 20px; }}
    h1, h2, h3, h4 {{ margin: 0 0 8px; }}
    p {{ margin: 0 0 10px; }}
    .notice {{ background: var(--panel); border-left: 4px solid #d0a64d; padding: 12px 14px; border-radius: 10px; margin-bottom: 18px; }}
    .grid {{ display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }}
    .eval-grid {{ grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }}
    .card, .eval-card, .group, .archive-block, .query-block {{ background: var(--panel); border: 1px solid var(--line); border-radius: 12px; }}
    .card {{ overflow: hidden; }}
    .card img, .thumb {{ display: block; width: 100%; height: 220px; object-fit: contain; background: #0b1020; }}
    .mini-thumb {{ width: 48px; height: 48px; object-fit: contain; background: #0b1020; border-radius: 8px; }}
    .thumb-empty {{ display: grid; place-items: center; color: var(--muted); }}
    .card-body, .eval-card, .group > div, .archive-block > *:not(summary), .query-block > *:not(summary) {{ padding: 12px; }}
    .card-meta {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; }}
    .badge {{ display: inline-block; padding: 3px 8px; border-radius: 999px; font-size: 12px; }}
    .badge-good {{ background: var(--good); }}
    .badge-warn {{ background: var(--warn); }}
    .badge-neutral {{ background: var(--neutral); }}
    .badge-muted {{ background: #414b60; }}
    .muted {{ color: var(--muted); }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; overflow-wrap: anywhere; }}
    .path {{ color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }}
    details summary {{ cursor: pointer; padding: 12px; font-weight: 600; }}
    details > summary {{ list-style: none; }}
    details > summary::-webkit-details-marker {{ display: none; }}
    details > summary::after {{ content: ' ＋ 펼쳐보기'; color: var(--muted); font-weight: 400; }}
    details[open] > summary::after {{ content: ' − 접기'; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ border-top: 1px solid var(--line); padding: 8px; text-align: left; vertical-align: middle; }}
    pre {{ margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; background: var(--panel-2); border-radius: 10px; padding: 10px; }}
    section {{ margin-top: 22px; }}
    @media (max-width: 640px) {{
      main {{ padding: 12px; }}
      .card img, .thumb {{ height: 180px; }}
    }}
  </style>
</head>
<body>
  <main>
    <h1>{_escape(title)}</h1>
    <p class="muted">읽기 전용 local results view · 원본 제거/병합/공개 배포 버튼 없음</p>
    <div class="notice">
      <p>cosine score는 절대 유사 확률이 아닙니다.</p>
      <p>provider 또는 모델별 벡터 공간이 다르면 절대값으로 승자 비교를 하지 않습니다.</p>
      <p>보관 항목은 숨김 정리일 뿐 원본 제거가 아닙니다.</p>
      <p>대표 표시는 우선순위 순서를 먼저 따르고, 동률일 때만 입고 순서 증거를 보조 기준으로 씁니다.</p>
      <p>JSON 템플릿 parse 상태가 있으면 우선순위 설명에 함께 표시합니다. 없으면 우선순위만 표시합니다.</p>
      <p>{_escape(evaluation_notice)}</p>
      {f'<p>{_escape(selection_profile_notice)}</p>' if selection_profile_notice else ''}
      {f'<p>{_escape(partial_provider_notice)}</p>' if partial_provider_notice else ''}
      <p>{'주의: 최초 입고 시각이 없는 항목은 기존 정본의 기록 순서를 대체 기준으로 사용했습니다. 실제 최초 입고가 확인된 것은 아닙니다.' if any(row.get('arrival_at') == 'unknown' for row in retention.get('order_evidence', [])) else '대표 선택 순서는 보관 정책의 order_evidence에 기록되어 있습니다.'}</p>
    </div>

    <section>
      <h2>활성 카드</h2>
      <p class="muted">100% 동일 계열은 대표 1개만 먼저 보이고 나머지는 아래 보관 섹션에 둡니다.</p>
      <div class="grid">{active_cards or '<p class="muted">활성 항목 없음</p>'}</div>
    </section>

    <section>
      <details>
        <summary>보관 항목 보기</summary>
        <p class="muted">대표 외 항목을 접어 둔 것입니다. 제거가 아닙니다.</p>
        {''.join(archived_blocks) or '<p class="muted">보관 항목 없음</p>'}
      </details>
    </section>

    <section>
      <details open>
        <summary>질의별 retrieval 결과</summary>
        {query_html}
      </details>
    </section>

    <section>
      <details open>
        <summary>삭제 대상(논리삭제)</summary>
        <p class="muted">이 섹션은 복구 가능한 논리삭제 후보만 보여 줍니다. source 원본은 바꾸지 않으며 exact 계열 근거를 표시용으로 남깁니다.</p>
        {deletion_html}
      </details>
    </section>

    <section>
      <details open>
        <summary>동일 프롬프트의 다른 결과</summary>
        <p class="muted">이 섹션은 prompt match 기준이며 시각 유사도 판단이 아닙니다. 동일 프롬프트에서 나온 변형 결과는 활성 상태로 남기고 비교만 돕습니다.</p>
        {prompt_variant_html}
      </details>
    </section>

    <section>
      <details open>
        <summary>시각 유사 그룹</summary>
        <p class="muted">유사 그룹은 최대 {MAX_GROUP_MEMBERS}개까지만 펼쳐 보여 주며 사람 검토 배지를 유지합니다. 우선 대표 표시는 검토 편의를 위한 것이며 similarity 보관은 적용하지 않습니다.</p>
        {similar_html}
      </details>
    </section>
  </main>
</body>
</html>"""
