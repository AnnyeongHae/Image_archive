from __future__ import annotations

import html
import json
from urllib.parse import urlsplit
from typing import Any


GROUP_REVIEW_SPEC_SCHEMA_VERSION = "image-group-workflow-spec-1"
GROUP_REVIEW_DECISIONS_SCHEMA_VERSION = "image-group-workflow-decisions-3"
GROUP_REVIEW_LOCAL_STORAGE_PREFIX = "image-rag-group-review:"


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _escape(value: Any) -> str:
    return html.escape(_text(value), quote=True)


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _ordered_unique(values: list[Any]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = _text(value).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _priority_label(priority: Any) -> str:
    if isinstance(priority, dict):
        parts = []
        for key in ("rank_index", "tier", "label", "reason"):
            value = _text(priority.get(key)).strip()
            if value:
                parts.append(f"{key}={value}")
        return " · ".join(parts) if parts else "priority 정보 없음"
    value = _text(priority).strip()
    return value or "priority 정보 없음"


def _stage1_summary(spec: dict[str, Any]) -> str:
    stage1 = spec.get("stage1", {})
    active_ids = _ordered_unique(stage1.get("active_ids", []))
    archived = stage1.get("archived", [])
    alias_lineage = stage1.get("alias_lineage", [])
    duplicate_candidates = spec.get("duplicate_candidates", [])
    similarity_candidates = spec.get("similarity_candidates", [])
    return (
        '<section class="stage-card" id="stage-1">'
        '<h2>1단계 · 컴퓨터 정리 결과 확인</h2>'
        '<p class="stage-copy">현재 활성 후보, 기존 제외, 별칭 계보를 먼저 확인합니다. 이 단계는 정보 요약이며 사람 승인 단계가 아닙니다.</p>'
        '<div class="summary-grid">'
        f'<article><h3>활성 후보</h3><p class="metric">{len(active_ids)}</p><p>{" / ".join(_escape(value) for value in active_ids[:8]) or "없음"}</p></article>'
        f'<article><h3>기존 제외</h3><p class="metric">{len(archived) if isinstance(archived, list) else 0}</p><p>{_escape(_text(stage1.get("policy")).strip() or "policy 없음")}</p></article>'
        f'<article><h3>정확 중복 후보 그룹</h3><p class="metric">{len(duplicate_candidates) if isinstance(duplicate_candidates, list) else 0}</p><p>동일 이미지 여부를 먼저 해결해야 다음 단계 승인 가능</p></article>'
        f'<article><h3>유사 그룹 후보</h3><p class="metric">{len(similarity_candidates) if isinstance(similarity_candidates, list) else 0}</p><p>3개 이상 함께 보고 승인할 하위 그룹만 선택</p></article>'
        '</div>'
        '<details class="evidence-block"><summary>별칭 계보 / 기존 제외 근거</summary>'
        f'<pre>{_escape(_json_text({"alias_lineage": alias_lineage, "archived": archived}))}</pre>'
        '</details>'
        '</section>'
    )


def _member_card(item: dict[str, Any], *, selectable_html: str, badge: str) -> str:
    style_id = _text(item.get("style_id")).strip() or _text(item.get("id")).strip()
    ident = _text(item.get("id")).strip()
    priority = _priority_label(item.get("priority"))
    return (
        '<article class="member-card">'
        f'<img loading="lazy" src="{_escape(item.get("prepared_path"))}" alt="{_escape(style_id)} prepared preview">'
        f'<h4>{_escape(style_id)}</h4>'
        f'<p class="mono">{_escape(ident)}</p>'
        f'<p class="badge">{_escape(badge)}</p>'
        f'<p class="priority">{_escape(priority)}</p>'
        f'<p><a href="{_escape(item.get("prepared_path"))}" target="_blank" rel="noopener">크게 보기</a></p>'
        f'{selectable_html}'
        '</article>'
    )


def _duplicate_candidate_card(candidate: dict[str, Any], item_map: dict[str, dict[str, Any]], stage1: dict[str, Any]) -> str:
    candidate_id = _text(candidate.get("id")).strip()
    suggested_rep = _text(candidate.get("suggested_representative_id")).strip()
    member_ids = _ordered_unique(candidate.get("member_ids", []))
    active_ids = set(_ordered_unique(stage1.get("active_ids", [])))
    read_only_ids = set(_ordered_unique(stage1.get("read_only_ids", [])))
    archived_map = {
        _text(row.get("id")).strip(): row
        for row in stage1.get("archived", [])
        if isinstance(row, dict) and _text(row.get("id")).strip()
    }
    cards: list[str] = []
    for member_id in member_ids:
        item = item_map.get(member_id, {"id": member_id, "style_id": member_id, "prepared_path": ""})
        badge_parts = []
        if member_id in read_only_ids:
            badge_parts.append("기존 승인 기록 · 읽기 전용")
        if member_id == suggested_rep:
            badge_parts.append("제안 대표")
        if member_id in active_ids:
            badge_parts.append("현재 활성")
        if member_id in archived_map:
            badge_parts.append("기존 제외")
        badge = " / ".join(badge_parts) if badge_parts else "상태 미기록"
        selectable = (
            f'<label class="checkbox-row"><input type="checkbox" data-duplicate-member="{_escape(candidate_id)}" value="{_escape(member_id)}"> '
            '같은 최종 이미지로 묶기</label>'
        )
        cards.append(_member_card(item, selectable_html=selectable, badge=badge))
    evidence = {
        "evidence": candidate.get("evidence"),
        "suggested_representative_id": suggested_rep,
    }
    return (
        f'<article class="candidate-card" data-duplicate-candidate-id="{_escape(candidate_id)}" data-suggested-representative="{_escape(suggested_rep)}" '
        f'data-member-ids="{_escape(json.dumps(member_ids, ensure_ascii=False))}">'
        f'<h3>{_escape(candidate_id)}</h3>'
        '<p class="stage-copy">정확히 같은 최종 이미지라고 판단하는 멤버만 체크하세요. 같은 프롬프트라는 이유만으로 동일 처리하면 안 됩니다.</p>'
        '<div class="candidate-toolbar">'
        f'<button type="button" data-select-all-duplicate="{_escape(candidate_id)}">체크 가능한 멤버 전체 선택</button>'
        f'<button type="button" data-clear-duplicate="{_escape(candidate_id)}">선택 해제</button>'
        f'<span class="status" id="duplicate-status-{_escape(candidate_id)}"></span>'
        '</div>'
        f'<fieldset><legend>판정</legend>'
        f'<label class="radio"><input type="radio" name="dup-decision-{_escape(candidate_id)}" value="same_image_subset"> 동일 이미지 하위집합</label>'
        f'<label class="radio"><input type="radio" name="dup-decision-{_escape(candidate_id)}" value="distinct_images"> 서로 다른 이미지</label>'
        f'<label class="radio"><input type="radio" name="dup-decision-{_escape(candidate_id)}" value="defer" checked> 판단 보류</label>'
        '</fieldset>'
        f'<label class="checkbox-row remainder-row"><input type="checkbox" data-duplicate-remainder="{_escape(candidate_id)}"> 선택하지 않은 나머지는 서로 다른 이미지임</label>'
        f'<p class="status helper" id="duplicate-helper-{_escape(candidate_id)}">제안 대표: <code>{_escape(suggested_rep or "없음")}</code>. 선택한 하위집합 내부에서 대표를 다시 정합니다. 2개 이상 남는 미선택 멤버는 명시적으로 distinct 처리해야 합니다.</p>'
        f'<section class="member-grid">{"".join(cards)}</section>'
        '<details class="evidence-block"><summary>컴퓨터 근거</summary>'
        f'<pre>{_escape(_json_text(evidence))}</pre>'
        '</details>'
        '</article>'
    )


def _pair_evidence_block(title: str, value: Any) -> str:
    if not value:
        return ""
    return (
        '<details class="evidence-block">'
        f'<summary>{_escape(title)}</summary>'
        f'<pre>{_escape(_json_text(value))}</pre>'
        '</details>'
    )


def _similarity_candidate_card(candidate: dict[str, Any], item_map: dict[str, dict[str, Any]], stage1: dict[str, Any]) -> str:
    candidate_id = _text(candidate.get("id")).strip()
    member_ids = _ordered_unique(candidate.get("member_ids", []))
    active_ids = set(_ordered_unique(stage1.get("active_ids", [])))
    read_only_ids = set(_ordered_unique(stage1.get("read_only_ids", [])))
    archived_ids = {
        _text(row.get("id")).strip()
        for row in stage1.get("archived", [])
        if isinstance(row, dict) and _text(row.get("id")).strip()
    }
    cards: list[str] = []
    for member_id in member_ids:
        item = item_map.get(member_id, {"id": member_id, "style_id": member_id, "prepared_path": ""})
        disabled = member_id not in active_ids or member_id in archived_ids or member_id in read_only_ids
        badge = "기존 승인 기록 · 읽기 전용" if member_id in read_only_ids else ("현재 활성" if not disabled else "선택 불가(기존 제외/비활성)")
        selectable = (
            f'<label class="checkbox-row"><input type="checkbox" data-similarity-member="{_escape(candidate_id)}" value="{_escape(member_id)}"'
            + (" disabled" if disabled else "")
            + '> 이 유사 그룹에 포함</label>'
        )
        cards.append(_member_card(item, selectable_html=selectable, badge=badge))
    evidence = {
        "evidence": candidate.get("evidence"),
        "candidate_only": candidate.get("candidate_only"),
    }
    return (
        f'<article class="candidate-card" data-similarity-candidate-id="{_escape(candidate_id)}" data-member-ids="{_escape(json.dumps(member_ids, ensure_ascii=False))}">'
        f'<h3>{_escape(candidate_id)}</h3>'
        '<p class="stage-copy">3개 이상 함께 보고 유사 그룹을 확정합니다. 프런트 승인은 1~3단계가 끝난 뒤 4단계에서 진행합니다.</p>'
        '<div class="candidate-toolbar">'
        f'<button type="button" data-select-all-similarity="{_escape(candidate_id)}">선택 가능한 멤버 전체 선택</button>'
        f'<button type="button" data-clear-similarity="{_escape(candidate_id)}">선택 해제</button>'
        f'<span class="status" id="similarity-status-{_escape(candidate_id)}"></span>'
        '</div>'
        f'<fieldset><legend>판정</legend>'
        f'<label class="radio"><input type="radio" name="sim-decision-{_escape(candidate_id)}" value="approve_selected"> 선택한 멤버끼리 그룹 확정</label>'
        f'<label class="radio"><input type="radio" name="sim-decision-{_escape(candidate_id)}" value="keep_separate"> 그룹으로 묶지 않고 각각 유지</label>'
        f'<label class="radio"><input type="radio" name="sim-decision-{_escape(candidate_id)}" value="defer" checked> 판단 보류</label>'
        '</fieldset>'
        f'<section class="member-grid">{"".join(cards)}</section>'
        '<p class="status helper">기존 pair 사람 라벨은 근거일 뿐이며, 이 그룹 전체를 자동 승인하지 않습니다.</p>'
        f'{_pair_evidence_block("알려진 positive pair 근거", candidate.get("known_positive_pairs"))}'
        f'{_pair_evidence_block("알려진 negative pair 근거", candidate.get("known_negative_pairs"))}'
        f'{_pair_evidence_block("컴퓨터 후보 근거", evidence)}'
        '</article>'
    )


def _blank_decisions(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": GROUP_REVIEW_DECISIONS_SCHEMA_VERSION,
        "spec_sha256": _text(spec.get("spec_sha256")).strip(),
        "run_id": _text(spec.get("run_id")).strip(),
        "reviewer": "", "reviewed_at": "", "metadata_optional": True,
        "duplicate_reviews": [
            {"candidate_id": row["id"], "decision": "defer",
             "selected_ids": _ordered_unique(row.get("baseline_anchor_ids", [])), "remainder_distinct": False}
            for row in spec.get("duplicate_candidates", [])
        ],
        "similarity_reviews": [
            {"candidate_id": row["id"], "decision": "defer",
             "selected_ids": _ordered_unique(row.get("baseline_anchor_ids", [])), "tags_text": ""}
            for row in spec.get("similarity_candidates", [])
        ],
        "image_approvals": [],
    }


def render_group_review(spec: dict[str, Any]) -> str:
    if spec.get("schema_version") != GROUP_REVIEW_SPEC_SCHEMA_VERSION:
        raise ValueError("group review UI requires image-group-workflow-spec-1")
    item_map = {_text(row.get("id")): row for row in spec.get("items", []) if isinstance(row, dict)}
    stage1 = {**spec.get("stage1", {}), "read_only_ids": spec.get("baseline", {}).get("read_only_ids", [])}
    duplicates = "".join(_duplicate_candidate_card(row, item_map, stage1) for row in spec.get("duplicate_candidates", []))
    candidates = sorted(spec.get("similarity_candidates", []), key=lambda row: (-len(row.get("member_ids", [])), row["id"]))
    similarities = "".join(_similarity_candidate_card(row, item_map, stage1) for row in candidates)
    spec_json = json.dumps(spec, ensure_ascii=False).replace("<", "\\u003c")
    template_json = json.dumps(_blank_decisions(spec), ensure_ascii=False).replace("<", "\\u003c")
    draft_key = json.dumps(f"{GROUP_REVIEW_LOCAL_STORAGE_PREFIX}{spec.get('run_id')}:{spec.get('spec_sha256')}")
    baseline_front_url = spec.get("baseline_front_url", "")
    if not isinstance(baseline_front_url, str):
        raise ValueError("baseline_front_url must be a relative local URL")
    parsed = urlsplit(baseline_front_url)
    if parsed.scheme or parsed.netloc or baseline_front_url.startswith(("/", "\\")):
        raise ValueError("baseline_front_url must be a relative local URL")
    baseline_front_link = (f'<p><a href="{_escape(baseline_front_url)}" target="_blank" rel="noopener">기존 승인 이미지 프론트 열기</a> · 새 검토 진행 중에도 기존 프론트는 유지됩니다.</p>' if baseline_front_url else "")
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>이미지 그룹 검토 · 4단계</title>
<style>
:root {{color-scheme:dark;--bg:#0f1320;--panel:#182033;--line:#33405c;--text:#e8eef9;--muted:#9fb0c9}}
* {{box-sizing:border-box}} body {{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,sans-serif}}
main {{max-width:1560px;margin:auto;padding:20px}} h1,h2,h3,h4,p {{margin:0 0 10px}}
code,.mono,pre {{font-family:ui-monospace,Consolas,monospace}} pre {{white-space:pre-wrap;overflow-wrap:anywhere}}
a {{color:#b7d4ff}} input,button,textarea {{font:inherit}} button {{background:#24324f;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px 12px;cursor:pointer}}
button:disabled,input:disabled,textarea:disabled {{opacity:.55;cursor:not-allowed}}
input[type=text],textarea {{width:100%;background:#0d1220;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px}}
.notice {{background:var(--panel);border-left:4px solid #d0a64d;padding:12px 14px;border-radius:10px;margin-bottom:18px}}
.toolbar,.candidate-toolbar,.summary-progress {{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:14px 0}}
.toolbar label {{flex:1 1 280px;min-width:220px}}
.summary-grid,.member-grid,.preview-grid,.item-approval-grid {{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}}
.summary-grid article,.stage-card,.candidate-card,.preview-card,.tag-card,.item-approval-card {{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px;overflow-wrap:anywhere;min-width:0}}
.stage-card,.candidate-card,.tag-card {{margin-bottom:18px}} .stage-copy,.status,.helper,.priority {{color:var(--muted);font-size:13px;overflow-wrap:anywhere}}
.metric {{font-size:28px;font-weight:700;color:#70d7a1}} .member-card {{background:#101826;border:1px solid var(--line);border-radius:10px;padding:12px;min-width:0}}
img {{width:100%;height:240px;object-fit:contain;background:#0b1020;border-radius:8px}}
.checkbox-row,.radio {{display:block;margin:8px 0}} fieldset {{border:1px solid var(--line);border-radius:10px;margin:12px 0}} legend {{padding:0 6px}}
.badge {{display:inline-block;padding:3px 8px;border-radius:999px;background:#263451}} [hidden] {{display:none!important}}
.tags-area {{min-height:80px}} .excluded {{opacity:.6}} #export-json {{min-height:240px}}
</style></head><body><main>
<h1>이미지 그룹 검토 · 4단계</h1><p class="status">run_id: {_escape(spec.get("run_id"))} · spec_sha256: {_escape(spec.get("spec_sha256"))}</p>
<div class="notice"><p>1. 컴퓨터 완전 중복 정리 → 2. 사람 동일 판정 → 3. 유사 그룹 확정 → 4. 기본 승인 확인 / 필요한 이미지에만 메모</p>
<p>1~3단계를 모두 완료하면 남은 새 이미지는 기본 승인됩니다. 제외할 이미지만 체크를 해제하세요. 그룹 안의 이미지도 개별 해제할 수 있습니다. 기존 승인·미승인 기록은 읽기 전용으로 유지됩니다.</p>
<p>기존 pair 사람 라벨은 참고 근거일 뿐 자동 승인이 아닙니다. 비공개 검토용이며 원본 삭제·공개 배포를 직접 실행하지 않습니다.</p></div>
<div class="toolbar"><label>검토자 이름 <input id="reviewer" type="text" autocomplete="off" placeholder="승인 JSON에만 필요; 초안은 이름 없이 가능"></label>
<button id="download" type="button">현재 선택 초안 JSON 저장</button><button id="download-decisions" type="button">승인 결과 JSON 저장</button>
<button id="clear-draft" type="button">로컬 초안 초기화</button><label>기존 decisions JSON 불러오기 <input id="import-file" type="file" accept="application/json"></label></div>
<p id="message" class="status" role="status" aria-live="polite"></p><div id="export-blockers" class="status"></div>
<details id="export-fallback" class="stage-card"><summary>JSON 직접 복사 / 붙여넣기 — 다운로드가 안 될 때</summary>
<p class="status">초안은 미완료·이름 미입력 상태에서도 저장됩니다. 다운로드가 막히면 아래 JSON을 복사해서 보관하세요.</p>
<label>내보낸 JSON<textarea id="export-json" readonly aria-label="내보낸 JSON"></textarea></label><button id="copy-json" type="button">JSON 복사</button>
<label>복구할 JSON 붙여넣기<textarea id="import-json" aria-label="복구할 JSON"></textarea></label><button id="import-pasted" type="button">붙여넣은 JSON 불러오기</button></details>
<section class="stage-card"><h2>4단계 진행 요약</h2><div class="summary-progress">
<div><strong>2단계</strong><p id="stage2-global" class="status"></p></div><div><strong>3단계</strong><p id="stage3-global" class="status"></p></div>
<div><strong>4단계</strong><p id="stage4-global" class="status"></p></div><div><strong>승인된 front</strong><p id="front-preview-count" class="status"></p></div>
</div></section>
<section id="baseline-context" class="stage-card" hidden><h2>기존 승인 기준선 · 읽기 전용</h2>{baseline_front_link}<p id="baseline-summary" class="status"></p><details><summary>기존 이미지 / 메모 보기</summary><div id="baseline-items" class="member-grid"></div></details></section>
{_stage1_summary(spec)}
<section class="stage-card" id="stage-2"><h2>2단계 · 동일 이미지 검토</h2><p class="stage-copy">같은 최종 이미지인 멤버를 체크하고 판정하세요. 체크만으로 판정이 확정되지는 않습니다.</p><div id="duplicate-candidates">{duplicates}</div></section>
<section class="stage-card" id="stage-3"><h2>3단계 · 시각 유사 그룹 확정</h2><p class="stage-copy" id="stage3-lock-copy"></p><div id="similarity-candidates">{similarities}</div></section>
<div id="stage4-lock-copy" class="notice">1~3단계를 모두 완료하면 기본 승인 확인과 선택 메모가 열립니다.</div>
<section class="stage-card" id="stage-4" hidden><h2>4단계 · 기본 승인 / 선택 메모</h2><p class="stage-copy">남은 새 이미지는 기본 승인입니다. 원하지 않는 이미지의 체크만 해제하세요. 메모는 영감·활용 아이디어 등을 자유롭게 남기는 선택 항목이며, 모두 비워도 됩니다. 앞 단계 변경 중에는 새 이미지 승인이 잠기고, 다시 완료되면 기본 승인 정책이 적용됩니다. 직접 해제한 체크는 유지됩니다.</p>
<p id="stage4-policy" class="status"></p><div id="group-tags"></div><h3>그룹에 포함되지 않은 유지 이미지</h3><div id="individual-approvals" class="item-approval-grid"></div>
<h3>승인된 private front 미리보기</h3><div id="front-preview" class="preview-grid"></div></section>
</main><script>const spec = {spec_json}; const template = {template_json}; const draftKey = {draft_key};
{_GROUP_REVIEW_SCRIPT}
</script></body></html>"""


_GROUP_REVIEW_SCRIPT = r"""
const $=s=>document.querySelector(s),$$=s=>Array.from(document.querySelectorAll(s));
const message=$("#message"),clone=v=>JSON.parse(JSON.stringify(v));
const normalizeIds=v=>[...new Set((v||[]).map(String))];
const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const duplicateCandidateMap=new Map(spec.duplicate_candidates.map(r=>[r.id,r]));
const similarityCandidateMap=new Map(spec.similarity_candidates.map(r=>[r.id,r]));
const itemMap=new Map(spec.items.map(r=>[r.id,r])),initialActiveIds=new Set(spec.stage1.active_ids);
const readOnlyIds=new Set(spec.baseline?.read_only_ids||[]);
const baselineChoices=new Map((spec.baseline?.image_approvals||[]).map(r=>[r.id,r]));
const defaultPolicy=spec.approval_policy==="default_retained_images_after_review_v1";
let state=clone(template),imageChoices={},legacyPayload=null,autosaveAllowed=true,tagSignature="";
for(const r of spec.initial_image_approvals||[])if(!readOnlyIds.has(r.id))imageChoices[r.id]={approved:r.approved,memo_text:r.memo_text||""};

function chooseRepresentativeId(candidate,ids){
  const selected=normalizeIds(ids),old=selected.filter(id=>readOnlyIds.has(id));
  if(old.length===1)return old[0];
  const explicit=(candidate.representative_priority_ids||[]).find(id=>selected.includes(id));
  return explicit||selected.sort((a,b)=>(Number(itemMap.get(a)?.priority?.rank_index)||1e9)-(Number(itemMap.get(b)?.priority?.rank_index)||1e9)||a.localeCompare(b))[0]||"";
}
function duplicateReviewState(candidate){
  const raw=state.duplicate_reviews.find(r=>r.candidate_id===candidate.id),selected=raw.selected_ids;
  const remaining=candidate.member_ids.filter(id=>!selected.includes(id)),old=selected.filter(id=>readOnlyIds.has(id));
  const valid=selected.length>=2&&old.length<=1&&(remaining.length<=1||raw.remainder_distinct);
  const resolved=raw.decision==="distinct_images"||(raw.decision==="same_image_subset"&&valid);
  const representative=resolved&&raw.decision==="same_image_subset"?chooseRepresentativeId(candidate,selected):"";
  let status=raw.decision==="distinct_images"?"서로 다른 이미지로 해결됨":"판단 보류";
  if(raw.decision==="same_image_subset")status=old.length>1?"기존 대표 2개 이상을 함께 제거할 수 없습니다.":selected.length<2?"동일 하위집합은 2개 이상 선택해야 합니다.":remaining.length>=2&&!raw.remainder_distinct?"나머지 2개 이상을 distinct로 명시해야 합니다.":`동일 하위집합으로 해결됨 · 대표 ${representative}`;
  return{...raw,resolved,status,representative_id:representative,deleted_active_ids:representative?selected.filter(id=>id!==representative&&!readOnlyIds.has(id)):[]};
}
function collectDuplicateReviews(){return spec.duplicate_candidates.map(duplicateReviewState);}
function effectiveActiveIds(reviews){const active=new Set(initialActiveIds);for(const row of reviews)for(const id of row.deleted_active_ids)active.delete(id);return active;}
function blockedNegativePairs(candidate,ids){const selected=new Set(ids);return(candidate.known_negative_pairs||[]).filter(r=>selected.has(r.left_id||r.left?.id||r.a_id||r.source_id)&&selected.has(r.right_id||r.right?.id||r.b_id||r.target_id));}
function similarityReviewState(candidate,active,stage2){
  const raw=state.similarity_reviews.find(r=>r.candidate_id===candidate.id),anchors=candidate.baseline_anchor_ids||[];
  const eligible=candidate.member_ids.filter(id=>active.has(id)),selected=raw.selected_ids.filter(id=>active.has(id));
  const newEligible=eligible.filter(id=>!readOnlyIds.has(id)),newSelected=selected.filter(id=>!readOnlyIds.has(id));
  const conflicts=blockedNegativePairs(candidate,selected),anchorsValid=anchors.every(id=>selected.includes(id));
  const skipped=stage2&&(eligible.length<=1||(anchors.length>0&&!newEligible.length));
  const approved=stage2&&!skipped&&raw.decision==="approve_selected"&&selected.length>=2&&!conflicts.length&&anchorsValid&&(!anchors.length||newSelected.length>0);
  const resolved=stage2&&(skipped||approved||raw.decision==="keep_separate");
  let status=!stage2?"2단계 해결 전이라 잠금":skipped?"중복 정리 후 신규 비교 대상 없음 · 검토 불필요":raw.decision==="keep_separate"?"그룹으로 묶지 않고 각각 유지":"판단 보류";
  if(stage2&&!skipped&&raw.decision==="approve_selected")status=conflicts.length?"known negative pair 충돌: 이전에 분리한 이미지가 포함되었습니다.":!anchorsValid?"기존 기준 그룹 멤버를 모두 유지해야 합니다.":selected.length<2||!newSelected.length?"그룹 확정에는 새 이미지와 비교 멤버를 선택해야 합니다.":`선택한 ${selected.length}개 멤버 그룹 확정`;
  return{...raw,selected_ids:selected,eligible_ids:eligible,approved,resolved,skipped,status,negative_conflicts:conflicts};
}
function snapshot(){
  const duplicates=collectDuplicateReviews(),stage2Resolved=duplicates.every(r=>r.resolved),active=effectiveActiveIds(duplicates);
  const similarities=spec.similarity_candidates.map(r=>similarityReviewState(r,active,stage2Resolved));
  return{duplicates,active,similarities,stage2Resolved,stage3Resolved:stage2Resolved&&similarities.every(r=>r.resolved)};
}
function imageChoice(id){
  if(readOnlyIds.has(id))return baselineChoices.get(id)||{approved:false,memo_text:""};
  return imageChoices[id]||{approved:defaultPolicy,memo_text:""};
}
function collectState(){
  const s=snapshot(),gate=s.stage3Resolved&&defaultPolicy;
  return{schema_version:template.schema_version,spec_sha256:spec.spec_sha256,run_id:spec.run_id,reviewer:state.reviewer.trim(),reviewed_at:"",metadata_optional:true,
    duplicate_reviews:state.duplicate_reviews.map(r=>({...r,selected_ids:r.decision==="same_image_subset"?r.selected_ids:[],remainder_distinct:r.decision==="same_image_subset"&&r.remainder_distinct})),
    similarity_reviews:s.similarities.map(r=>({candidate_id:r.candidate_id,decision:r.skipped?"keep_separate":r.decision,selected_ids:!r.skipped&&r.decision==="approve_selected"?r.selected_ids:[],tags_text:r.tags_text||""})),
    image_approvals:[...s.active].filter(id=>!readOnlyIds.has(id)).map(id=>({id,approved:gate&&imageChoice(id).approved,memo_text:imageChoice(id).memo_text||""}))};
}
function draftEnvelope(){return{schema_version:"image-group-workflow-draft-3",run_id:spec.run_id,spec_sha256:spec.spec_sha256,saved_at:new Date().toISOString(),decisions:clone(state),image_choices:clone(imageChoices),legacy_payload:legacyPayload};}
function invalidateApprovals(kind,id){
  // A changed already-reviewed member set must receive a fresh stage2/3 decision.
  if(snapshot().stage3Resolved&&id){
    const rows=kind==="duplicate"?state.duplicate_reviews:state.similarity_reviews;
    const row=rows.find(r=>r.candidate_id===id);if(row)row.decision="defer";
    message.textContent="구성 변경으로 해당 판정을 보류로 돌렸습니다. 1~3단계를 다시 완료하면 기본 승인이 적용됩니다. 직접 해제한 체크와 메모는 유지됩니다.";
  }
}
function frontPreviewIds(s,d){
  if(!s.stage3Resolved||!defaultPolicy)return[];
  return normalizeIds([...readOnlyIds].filter(id=>s.active.has(id)&&imageChoice(id).approved).concat(d.image_approvals.filter(r=>r.approved).map(r=>r.id)));
}
function previewCard(id){
  const item=itemMap.get(id)||{id,style_id:id};
  return `<article class="member-card"><img loading="lazy" src="${esc(item.prepared_path)}" alt="${esc(item.style_id)} prepared preview"><h4>${esc(item.style_id)}</h4><p class="mono">${esc(id)}</p><a href="${esc(item.prepared_path)}" target="_blank" rel="noopener">크게 보기</a></article>`;
}
function approvalCard(id){
  if(readOnlyIds.has(id)){
    const choice=imageChoice(id);
    return `<article class="item-approval-card" data-readonly-image="${esc(id)}">${previewCard(id)}<p class="badge">기존 기록 · ${choice.approved?"승인":"미승인"} · 수정 불가</p><p class="status">기존 메모: ${esc(choice.memo_text||"없음")}</p></article>`;
  }
  return `<article class="item-approval-card">${previewCard(id)}<label class="checkbox-row"><input type="checkbox" data-image-approval="${esc(id)}"> 이 이미지 승인 · 제외하려면 해제</label><label>자유 메모(선택 · 영감 / 활용 아이디어)<textarea class="tags-area" data-image-memo="${esc(id)}" placeholder="떠오르는 아이디어가 있을 때만 작성하세요."></textarea></label><p class="status">선택사항 · 승인 결과에는 UTF-8 기준 최대 8,000바이트. 긴 메모도 초안에는 그대로 보존됩니다.</p></article>`;
}
function collapseApprovedGroups(s){
  const rows=[
    ...(spec.baseline?.groups||[]).map(r=>({id:r.id||r.group_id||r.candidate_id,member_ids:r.member_ids.filter(id=>s.active.has(id)),source_candidate_ids:normalizeIds([...(r.source_candidate_ids||[]),r.id||r.group_id||r.candidate_id].filter(Boolean)),baseline:true})),
    ...s.similarities.filter(r=>r.approved).map(r=>({id:r.candidate_id,member_ids:r.selected_ids,source_candidate_ids:[r.candidate_id],baseline:false}))
  ].filter(r=>r.member_ids.length>=2).sort((a,b)=>b.member_ids.length-a.member_ids.length||a.id.localeCompare(b.id));
  const groups=[];
  for(const row of rows)if(!groups.some(g=>row.member_ids.every(id=>g.member_ids.includes(id))))groups.push(row);
  return groups.map(group=>{
    const contributors=rows.filter(row=>row.member_ids.every(id=>group.member_ids.includes(id)));
    return {...group,source_candidate_ids:normalizeIds(contributors.flatMap(row=>row.source_candidate_ids)),
      source_memberships:contributors.map(row=>({source_id:row.id,member_ids:row.member_ids,
        display_relation:row.id===group.id?"maximal":row.member_ids.length===group.member_ids.length?"equal":"subset"}))};
  });
}
function renderBaseline(){
  $("#baseline-context").hidden=!readOnlyIds.size;
  if(!readOnlyIds.size)return;
  $("#baseline-summary").textContent=`기존 유지 이미지 ${readOnlyIds.size}개 · 승인 ${[...readOnlyIds].filter(id=>imageChoice(id).approved).length}개. 이 기록은 신규 검토와 별도로 유지되며 수정되지 않습니다.`;
  $("#baseline-items").innerHTML=[...readOnlyIds].map(approvalCard).join("");
}
function renderTagStage(s){
  $("#stage-4").hidden=!s.stage3Resolved;$("#stage4-lock-copy").hidden=s.stage3Resolved;
  if(!s.stage3Resolved)return;
  const groups=collapseApprovedGroups(s),grouped=new Set(groups.flatMap(r=>r.member_ids)),ids=[...s.active].filter(id=>!grouped.has(id));
  const signature=JSON.stringify([groups,ids]);
  if(signature!==tagSignature){
    $("#group-tags").innerHTML=groups.map(r=>`<article class="tag-card" data-display-group="${esc(r.id)}"><h3>확정 유사 그룹 · ${esc(r.id)}</h3><p class="status">표시 출처: ${r.source_candidate_ids.map(esc).join(" · ")} — 동일 / 완전 부분집합만 묶어 표시합니다.</p><section class="member-grid">${r.member_ids.map(approvalCard).join("")}</section><details><summary>원래 그룹별 멤버와 표시 근거</summary><pre>${esc(JSON.stringify(r.source_memberships,null,2))}</pre></details></article>`).join("")||'<p class="status">확정 유사 그룹 없음</p>';
    $("#individual-approvals").innerHTML=ids.map(approvalCard).join("");tagSignature=signature;
  }
  for(const n of $$("[data-image-approval]"))n.checked=imageChoice(n.dataset.imageApproval).approved;
  for(const n of $$("[data-image-memo]"))n.value=imageChoice(n.dataset.imageMemo).memo_text||"";
  $("#stage4-policy").textContent=defaultPolicy?"기본 승인 정책 적용: 남은 새 이미지는 승인, 직접 해제한 이미지와 기존 기록은 그대로 유지. 자유 메모는 모두 선택사항.":"기본 승인 정책이 이 spec에 연결되지 않아 승인 결과 내보내기를 잠갔습니다.";
}
function refreshUi(){
  const s=snapshot();
  for(const r of s.duplicates){
    const c=duplicateCandidateMap.get(r.candidate_id),card=$(`[data-duplicate-candidate-id="${r.candidate_id}"]`);card.id=`review-${r.candidate_id}`;
    $(`input[name="dup-decision-${r.candidate_id}"][value="${r.decision}"]`).checked=true;
    for(const n of $$(`input[data-duplicate-member="${r.candidate_id}"]`)){n.checked=r.selected_ids.includes(n.value);n.disabled=readOnlyIds.has(n.value);}
    $(`input[data-duplicate-remainder="${r.candidate_id}"]`).checked=r.remainder_distinct;
    $(`#duplicate-status-${r.candidate_id}`).textContent=r.status;
    $(`#duplicate-helper-${r.candidate_id}`).textContent=`제안 대표: ${c.suggested_representative_id||"없음"} · 현재 선택 대표: ${r.representative_id||"미정"} · 기존 대표는 수정하지 않습니다.`;
  }
  for(const r of s.similarities){
    const card=$(`[data-similarity-candidate-id="${r.candidate_id}"]`),raw=state.similarity_reviews.find(x=>x.candidate_id===r.candidate_id);card.id=`review-${r.candidate_id}`;
    $(`input[name="sim-decision-${r.candidate_id}"][value="${raw.decision}"]`).checked=true;
    for(const n of card.querySelectorAll("input[data-similarity-member]")){
      n.checked=raw.selected_ids.includes(n.value);n.disabled=!s.stage2Resolved||!s.active.has(n.value)||readOnlyIds.has(n.value);n.closest(".member-card").classList.toggle("excluded",!s.active.has(n.value));
    }
    for(const n of card.querySelectorAll("button,input[type=radio]"))n.disabled=!s.stage2Resolved||r.skipped;
    const excluded=raw.selected_ids.filter(id=>!s.active.has(id)).length;
    $(`#similarity-status-${r.candidate_id}`).textContent=r.status+(excluded?` · 이전 체크 ${excluded}개는 중복 제외로 최종 그룹에 미포함(초안 보존)`:"");
  }
  const pending=[...s.duplicates.filter(r=>!r.resolved),...s.similarities.filter(r=>!r.resolved)];
  const links=pending.map(r=>`<a href="#review-${esc(r.candidate_id)}">${esc(r.candidate_id)}</a>`).join(" · ");
  $("#export-blockers").innerHTML=pending.length?`승인 결과 저장 전 미완료 ${pending.length}개: ${links}<br>현재 선택 초안 JSON은 지금도 저장할 수 있습니다.`:"1~3단계 완료 · 기본 승인과 해제 항목을 확인한 뒤 결과 JSON을 저장하세요.";
  $("#stage4-lock-copy").innerHTML=`1~3단계 모두 완료 후 열립니다. 남은 판정 ${pending.length}개: ${links}`;
  $("#stage2-global").textContent=s.stage2Resolved?"모든 동일 후보 해결":`미해결 duplicate candidate ${s.duplicates.filter(r=>!r.resolved).length}개`;
  $("#stage3-global").textContent=s.stage2Resolved?`미해결 ${s.similarities.filter(r=>!r.resolved).length}개 · 그룹 확정 ${s.similarities.filter(r=>r.approved).length}개`:"2단계 완료 후 진행";
  $("#stage4-global").textContent=s.stage3Resolved?"남은 새 이미지 기본 승인 / 선택 메모":"1~3단계 모두 완료해야 열림";
  $("#stage3-lock-copy").textContent=s.stage2Resolved?"각 후보를 그룹 확정 또는 각각 유지로 판정하세요. 전부 완료하면 남은 새 이미지가 기본 승인됩니다. 기존 그룹의 읽기 전용 체크는 변경하지 않습니다.":"2단계가 해결되기 전에는 유사 그룹 판정을 잠급니다.";
  renderTagStage(s);const ids=frontPreviewIds(s,collectState());
  $("#front-preview-count").textContent=ids.length?`${ids.length}개 승인된 retained active item`:"아직 승인된 retained item 없음";
  $("#front-preview").innerHTML=ids.map(id=>`<div class="preview-card">${previewCard(id)}</div>`).join("");
}
function validateImportedDecisions(input){
  if(!input||typeof input!=="object"||Array.isArray(input))throw Error("JSON 객체가 필요합니다.");
  const draft=["image-group-workflow-draft-2","image-group-workflow-draft-3"].includes(input.schema_version),p=draft?input.decisions:input;
  if(!p||!["image-group-workflow-decisions-1","image-group-workflow-decisions-2",template.schema_version].includes(p.schema_version))throw Error("지원하지 않는 group-review decisions schema입니다.");
  if(p.run_id!==spec.run_id||p.spec_sha256!==spec.spec_sha256||(draft&&(input.run_id!==spec.run_id||input.spec_sha256!==spec.spec_sha256)))throw Error("현재 spec과 맞지 않는 decisions 파일입니다.");
  if(p.metadata_optional!==true||typeof p.reviewer!=="string"||typeof p.reviewed_at!=="string")throw Error("metadata_optional/reviewer/reviewed_at 형식이 올바르지 않습니다.");
  for(const[field,candidates,choices]of[["duplicate_reviews",duplicateCandidateMap,["same_image_subset","distinct_images","defer"]],["similarity_reviews",similarityCandidateMap,["approve_selected","keep_separate","defer"]]]){
    if(!Array.isArray(p[field])||p[field].length!==candidates.size)throw Error("candidate 개수가 현재 spec과 다릅니다.");
    const seen=new Set();
    for(const r of p[field]){
      const c=candidates.get(r.candidate_id);if(!c||seen.has(r.candidate_id))throw Error("알 수 없거나 중복된 candidate_id입니다.");seen.add(r.candidate_id);
      if(!choices.includes(r.decision))throw Error("올바르지 않은 decision입니다.");
      if(!Array.isArray(r.selected_ids)||normalizeIds(r.selected_ids).length!==r.selected_ids.length||r.selected_ids.some(id=>typeof id!=="string"||!c.member_ids.includes(id)))throw Error("selected_ids가 candidate 범위를 벗어났습니다.");
      if(field==="duplicate_reviews"&&typeof r.remainder_distinct!=="boolean")throw Error("remainder_distinct는 boolean이어야 합니다.");
      if(field==="similarity_reviews"&&typeof r.tags_text!=="string")throw Error("tags_text는 문자열이어야 합니다.");
    }
  }
  for(const[field,key,allowed,textKey]of[["individual_approvals","id",initialActiveIds,"tags_text"],["group_approvals","candidate_id",new Set(similarityCandidateMap.keys()),"tags_text"],["image_approvals","id",initialActiveIds,"memo_text"]]){
    const rows=p[field]||[];if(!Array.isArray(rows))throw Error("approvals는 배열이어야 합니다.");const seen=new Set();
    for(const r of rows){if(!allowed.has(r[key])||seen.has(r[key]))throw Error("알 수 없거나 중복된 approval id입니다.");seen.add(r[key]);if(typeof r.approved!=="boolean"||typeof r[textKey]!=="string")throw Error("approved는 boolean, 메모/태그는 문자열이어야 합니다.");}
  }
  if(draft&&input.stage4_selections){
    for(const[field,allowed]of[["groups",new Set(similarityCandidateMap.keys())],["individuals",initialActiveIds]]){
      const rows=input.stage4_selections[field]||{};if(typeof rows!=="object"||Array.isArray(rows))throw Error("초안 승인 구조가 올바르지 않습니다.");
      for(const[id,r]of Object.entries(rows))if(!allowed.has(id)||!r||typeof r.approved!=="boolean"||typeof r.tags_text!=="string")throw Error("초안 승인에는 유효한 id, boolean, tags_text가 필요합니다.");
    }
  }
  if(draft&&input.image_choices){
    if(typeof input.image_choices!=="object"||Array.isArray(input.image_choices))throw Error("초안 이미지 선택 구조가 올바르지 않습니다.");
    for(const[id,r]of Object.entries(input.image_choices))if(!initialActiveIds.has(id)||readOnlyIds.has(id)||!r||typeof r.approved!=="boolean"||typeof r.memo_text!=="string")throw Error("초안 이미지 승인에는 편집 가능한 id, boolean, memo_text가 필요합니다.");
  }
  for(const r of p.image_approvals||[])if(readOnlyIds.has(r.id)&&(r.approved!==imageChoice(r.id).approved||r.memo_text!==(imageChoice(r.id).memo_text||"")))throw Error("기존 읽기 전용 승인/메모는 변경할 수 없습니다.");
  return input;
}
function applyState(input){
  const draft=["image-group-workflow-draft-2","image-group-workflow-draft-3"].includes(input.schema_version),p=draft?input.decisions:input;
  const legacy=p.schema_version!==template.schema_version;
  state=clone(p);state.schema_version=template.schema_version;state.reviewed_at="";delete state.front_review_complete;delete state.group_approvals;delete state.individual_approvals;state.image_approvals=[];
  imageChoices={};for(const r of spec.initial_image_approvals||[])if(!readOnlyIds.has(r.id))imageChoices[r.id]={approved:r.approved,memo_text:r.memo_text||""};
  if(legacy){
    legacyPayload=clone(input);
    const groups=draft?Object.entries(input.stage4_selections?.groups||{}).map(([candidate_id,r])=>({candidate_id,...r})):(p.group_approvals||[]);
    const individuals=draft?Object.entries(input.stage4_selections?.individuals||{}).map(([id,r])=>({id,...r})):(p.individual_approvals||[]);
    // True group choices do not override a false decision from any overlapping source.
    for(const r of groups.filter(r=>r.approved))for(const id of p.similarity_reviews.find(s=>s.candidate_id===r.candidate_id)?.selected_ids||[])if(!readOnlyIds.has(id))imageChoices[id]={approved:true,memo_text:""};
    for(const r of groups.filter(r=>!r.approved))for(const id of p.similarity_reviews.find(s=>s.candidate_id===r.candidate_id)?.selected_ids||[])if(!readOnlyIds.has(id))imageChoices[id]={approved:false,memo_text:""};
    for(const r of individuals)if(!readOnlyIds.has(r.id))imageChoices[r.id]={approved:r.approved,memo_text:r.memo_text||""};
  }else{
    legacyPayload=input.legacy_payload||null;
    const rows=draft?Object.entries(input.image_choices||{}).map(([id,r])=>({id,...r})):(p.image_approvals||[]);
    for(const r of rows)if(!readOnlyIds.has(r.id))imageChoices[r.id]={approved:r.approved,memo_text:r.memo_text||""};
  }
  // Read-only anchors are context, never a inferred new group decision.
  for(const[field,candidates]of[["duplicate_reviews",duplicateCandidateMap],["similarity_reviews",similarityCandidateMap]])for(const r of state[field])r.selected_ids=normalizeIds([...r.selected_ids,...(candidates.get(r.candidate_id)?.baseline_anchor_ids||[])]);
  $("#reviewer").value=state.reviewer;tagSignature="";renderBaseline();refreshUi();
}
function backupRawDraft(raw){
  const base=draftKey+":recovery:";
  if(Object.keys(localStorage).filter(k=>k.startsWith(base)).some(k=>localStorage.getItem(k)===raw))return;
  localStorage.setItem(base+Date.now()+":"+Math.random().toString(16).slice(2),raw);
}
function saveDraft(){if(!autosaveAllowed)return;try{localStorage.setItem(draftKey,JSON.stringify(draftEnvelope()));}catch{message.textContent="로컬 초안 저장이 불가능합니다. 초안 JSON 저장 또는 JSON 복사로 보관하세요.";}}
function exportPayload(payload,filename){
  const text=JSON.stringify(payload,null,2);$("#export-json").value=text;$("#export-fallback").open=true;
  try{const url=URL.createObjectURL(new Blob([text],{type:"application/json"})),a=document.createElement("a");a.href=url;a.download=filename;a.hidden=true;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);message.textContent=filename+" 다운로드를 요청했습니다. 파일이 안 생기면 아래 JSON 복사를 이용하세요.";}
  catch{message.textContent="브라우저 다운로드를 실행하지 못했습니다. 아래 JSON은 준비되었으니 복사해서 보관하세요.";}
}
function exportReviewed(){
  if(!defaultPolicy){message.textContent="이 spec에는 v3 기본 승인 정책이 연결되지 않았습니다. 초안만 저장 가능합니다.";return;}
  if(!state.reviewer.trim()){message.textContent="승인 결과에는 검토자 이름이 필요합니다. 이름 없이도 현재 선택 초안 JSON은 저장할 수 있습니다.";return;}
  const s=snapshot();if(!s.stage2Resolved||!s.stage3Resolved){message.textContent="승인 결과는 1~3단계 모두 완료 후 저장할 수 있습니다. 아래 미완료 항목을 확인하세요. 초안 JSON은 언제든 저장 가능합니다.";return;}
  const p=collectState();
  const oversized=p.image_approvals.filter(r=>new TextEncoder().encode(r.memo_text).length>8000);
  if(oversized.length){message.textContent="메모가 8,000바이트를 넘는 이미지: "+oversized.map(r=>itemMap.get(r.id)?.style_id||r.id).join(", ")+". 메모를 줄여 승인 결과를 저장하세요. 긴 원문은 초안 JSON에 그대로 저장할 수 있습니다.";return;}
  p.reviewed_at=new Date().toISOString();exportPayload(p,"group-review.decisions.json");
}
function importValue(input){
  try{const p=validateImportedDecisions(input);try{const raw=localStorage.getItem(draftKey);if(raw)backupRawDraft(raw);autosaveAllowed=true;}catch{autosaveAllowed=false;}
    applyState(p);message.textContent="초안을 복원했습니다. 실제 저장된 이미지 선택과 명시적인 해제는 보존됩니다. 1~3단계가 끝나야 기본 승인이 적용됩니다.";saveDraft();}
  catch(e){message.textContent=(e.message||"JSON을 읽지 못했습니다.")+" 현재 선택은 변경하지 않았습니다.";}
}
document.addEventListener("change",e=>{
  const n=e.target;if(!(n instanceof HTMLInputElement)||n.type==="file")return;
  if(n.dataset.duplicateMember){if(readOnlyIds.has(n.value))return;invalidateApprovals("duplicate",n.dataset.duplicateMember);const r=state.duplicate_reviews.find(r=>r.candidate_id===n.dataset.duplicateMember);r.selected_ids=n.checked?normalizeIds([...r.selected_ids,n.value]):r.selected_ids.filter(id=>id!==n.value);}
  else if(n.dataset.duplicateRemainder){invalidateApprovals("duplicate",n.dataset.duplicateRemainder);state.duplicate_reviews.find(r=>r.candidate_id===n.dataset.duplicateRemainder).remainder_distinct=n.checked;}
  else if(n.name.startsWith("dup-decision-"))state.duplicate_reviews.find(r=>r.candidate_id===n.name.slice(13)).decision=n.value;
  else if(n.dataset.similarityMember){if(readOnlyIds.has(n.value))return;invalidateApprovals("similarity",n.dataset.similarityMember);const r=state.similarity_reviews.find(r=>r.candidate_id===n.dataset.similarityMember);r.selected_ids=n.checked?normalizeIds([...r.selected_ids,n.value]):r.selected_ids.filter(id=>id!==n.value);}
  else if(n.name.startsWith("sim-decision-"))state.similarity_reviews.find(r=>r.candidate_id===n.name.slice(13)).decision=n.value;
  else if(n.dataset.imageApproval){const id=n.dataset.imageApproval;if(readOnlyIds.has(id))return;imageChoices[id]={approved:n.checked,memo_text:imageChoice(id).memo_text||""};}
  else return;refreshUi();saveDraft();
});
document.addEventListener("input",e=>{
  const n=e.target;
  if(n.id==="reviewer")state.reviewer=n.value;
  else if(n.dataset.imageMemo){const id=n.dataset.imageMemo;if(readOnlyIds.has(id))return;imageChoices[id]={approved:imageChoice(id).approved,memo_text:n.value};for(const other of $$(`[data-image-memo="${id}"]`))if(other!==n)other.value=n.value;}
  else return;saveDraft();
});
document.addEventListener("click",e=>{
  const n=e.target.closest("button");if(!n)return;
  const d=n.dataset.selectAllDuplicate||n.dataset.clearDuplicate,s=n.dataset.selectAllSimilarity||n.dataset.clearSimilarity;if(!d&&!s)return;
  invalidateApprovals(d?"duplicate":"similarity",d||s);
  const candidate=(d?duplicateCandidateMap:similarityCandidateMap).get(d||s),rows=d?state.duplicate_reviews:state.similarity_reviews,r=rows.find(r=>r.candidate_id===(d||s));
  const anchors=candidate.baseline_anchor_ids||[];
  if(n.dataset.selectAllDuplicate||n.dataset.selectAllSimilarity)r.selected_ids=normalizeIds([...r.selected_ids,...candidate.member_ids.filter(id=>snapshot().active.has(id)),...anchors]);else r.selected_ids=[...anchors];
  refreshUi();saveDraft();
});
$("#download").addEventListener("click",()=>{saveDraft();exportPayload(draftEnvelope(),"group-review.draft.json");});
$("#download-decisions").addEventListener("click",exportReviewed);
$("#copy-json").addEventListener("click",async()=>{
  const text=$("#export-json").value;if(!text){message.textContent="먼저 현재 선택 초안 JSON 저장을 눌러 JSON을 준비하세요.";return;}
  try{if(!navigator.clipboard?.writeText)throw Error();await navigator.clipboard.writeText(text);message.textContent="JSON을 복사했습니다.";}
  catch{$("#export-json").focus();$("#export-json").select();let ok=false;try{ok=document.execCommand("copy");}catch{}message.textContent=ok?"JSON을 복사했습니다.":"JSON 전체를 선택했습니다. Ctrl+C로 복사하세요.";}
});
$("#import-file").addEventListener("change",async e=>{const file=e.target.files?.[0];if(!file)return;try{importValue(JSON.parse(await file.text()));}catch{message.textContent="JSON 파일을 읽지 못했습니다. 현재 선택은 보존됩니다.";}e.target.value="";});
$("#import-pasted").addEventListener("click",()=>{try{importValue(JSON.parse($("#import-json").value));}catch{message.textContent="붙여넣은 JSON 형식이 올바르지 않습니다.";}});
$("#clear-draft").addEventListener("click",()=>{
  if(!window.confirm("현재 초안을 초기화할까요? 기존 저장값은 복구용으로 보관합니다."))return;
  try{const raw=localStorage.getItem(draftKey);if(raw)backupRawDraft(raw);}catch{message.textContent="복구 백업에 실패해 초기화를 중단했습니다.";return;}
  state=clone(template);imageChoices={};for(const r of spec.initial_image_approvals||[])if(!readOnlyIds.has(r.id))imageChoices[r.id]={approved:r.approved,memo_text:r.memo_text||""};
  legacyPayload=null;autosaveAllowed=true;$("#reviewer").value="";tagSignature="";refreshUi();saveDraft();
});
try{
  const raw=localStorage.getItem(draftKey);
  if(raw){backupRawDraft(raw);
    try{applyState(validateImportedDecisions(JSON.parse(raw)));message.textContent="로컬 group review 초안을 복원했습니다. 원문과 명시적인 해제를 보존합니다. 예전 앱이 저장하지 않은 체크는 복구할 수 없습니다.";saveDraft();}
    catch{autosaveAllowed=false;$("#export-json").value=raw;$("#export-fallback").open=true;message.textContent="손상된 로컬 초안을 자동 복원하지 못했습니다. 기존 초안은 삭제하지 않았습니다. 자동 덮어쓰기를 중단했으며 아래 원문을 복사할 수 있습니다.";}}
}catch{autosaveAllowed=false;message.textContent="로컬 초안/복구 백업에 접근하지 못했습니다. 자동 덮어쓰기를 중단했습니다. 초안 JSON으로 저장하세요.";}
renderBaseline();refreshUi();
"""
__all__ = ["GROUP_REVIEW_DECISIONS_SCHEMA_VERSION", "GROUP_REVIEW_LOCAL_STORAGE_PREFIX", "GROUP_REVIEW_SPEC_SCHEMA_VERSION", "render_group_review"]
