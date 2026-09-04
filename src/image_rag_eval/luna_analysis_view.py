"""Private read-only review of immutable imported Luna candidates, not approval."""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import tempfile
from pathlib import Path

from .luna_analysis_import import RELATIVE_ROOT, _json, import_luna_results

SCHEMA = "image-luna-analysis-review-1"
OUTPUT_NAME = "results-review.html"
TASK_COUNT = 10
MEDIUM = {"photograph": "사진", "illustration": "일러스트", "3d_render_appearance": "3D 렌더처럼 보임",
          "graphic_design": "그래픽 디자인", "screenshot": "화면 캡처", "mixed": "혼합", "unknown": "판단 불가"}
OCR_STATUS = {"none": "보이는 글자 없음", "legible": "읽을 수 있는 일부 글자", "partial": "부분적으로만 읽힘", "unclear": "판독 불가"}


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _safe(root, relative):
    if (not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative
            or Path(relative).is_absolute() or ".." in Path(relative).parts):
        raise ValueError("unsafe Luna review source path")
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Luna review path escapes archive")
    return path


def _rights_catalog(root, source_run_id):
    from .incremental_workflow import load_frozen_workflow
    from .rights import build_rights_catalog
    return build_rights_catalog(root, load_frozen_workflow(root, source_run_id))


def _qa_findings(directory, analysis_run_id, tasks):
    path = directory / "qa-findings.json"
    if not path.exists():
        return [], None
    if path.is_symlink() or path.resolve().parent != directory:
        raise ValueError("agent QA findings must stay inside the analysis run")
    document, raw = _json(path)
    if (document.get("schema_version") != "image-luna-agent-qa-1" or document.get("analysis_run_id") != analysis_run_id
            or document.get("qa_kind") != "orchestrator_spot_check_not_human_approval"
            or document.get("metadata_human_approved") is not False or document.get("release_eligible") is not False
            or set(document) != {"schema_version", "analysis_run_id", "qa_kind", "metadata_human_approved", "release_eligible", "findings"}):
        raise ValueError("invalid agent QA findings contract")
    findings = document["findings"]
    identities = {(task["task_id"], task["style_id"]) for task in tasks}
    if not isinstance(findings, list) or len(findings) > 50:
        raise ValueError("agent QA findings must be a bounded list")
    for row in findings:
        if (not isinstance(row, dict) or set(row) != {"task_id", "style_id", "field", "status", "message", "disposition"}
                or not isinstance(row["task_id"], str) or not isinstance(row["style_id"], str)
                or (row["task_id"], row["style_id"]) not in identities
                or row["status"] not in {"needs_correction_before_acceptance", "needs_review"}
                or any(not isinstance(row[key], str) or not row[key] or len(row[key]) > 1800 for key in ("message", "disposition"))
                or not isinstance(row["field"], str) or len(row["field"]) > 160
                or not re.fullmatch(r"(?:visual|search_hints|prompt_intent|reuse_ideas|limitations)(?:\.[a-z_]+|\[[0-9]+\])*", row["field"])):
            raise ValueError("agent QA finding does not match an assigned task or field")
    return findings, _sha(raw)


def _load_review(root, db_path, analysis_run_id):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", analysis_run_id):
        raise ValueError("invalid analysis run ID")
    base = f"{RELATIVE_ROOT}/{analysis_run_id}"
    directory = _safe(root, base)
    manifest, manifest_raw = _json(directory / "tasks.json")
    # This recomputes schema, visual-draft, image, original-prompt and current
    # committed-approval bindings. It is deliberately never an apply operation.
    checked = import_luna_results(root, db_path, analysis_run_id, apply=False)
    tasks_sha = _sha(manifest_raw)
    expected_import = f"{base}/imports/{tasks_sha}"
    if (checked.get("status") != "unchanged" or checked.get("output_path") != expected_import
            or checked.get("candidate_count") != TASK_COUNT):
        raise ValueError("complete immutable imported candidates are required before review rendering")
    imported = _safe(root, expected_import)
    payload, payload_raw = _json(imported / "validated-results.json")
    receipt, receipt_raw = _json(imported / "receipt.json")
    tasks = manifest.get("tasks")
    if (manifest.get("schema_version") != "image-luna-analysis-tasks-1" or manifest.get("analysis_run_id") != analysis_run_id
            or not isinstance(tasks, list) or len(tasks) != TASK_COUNT or manifest.get("selected_count") != TASK_COUNT
            or type(manifest.get("approved_library_count")) is not int or manifest["approved_library_count"] < TASK_COUNT
            or manifest.get("human_memos_in_model_input") is not False
            or manifest.get("embedding_calls_authorized") is not False
            or manifest.get("release_eligible") is not False):
        raise ValueError("unsupported or incomplete review tasks")
    if (payload.get("schema_version") != "image-luna-validated-results-1" or payload.get("analysis_run_id") != analysis_run_id
            or payload.get("task_manifest_sha256") != tasks_sha or payload.get("source_commit") != manifest.get("source_commit")
            or payload.get("source_run_id") != manifest.get("source_run_id") or payload.get("model_family") != manifest.get("model_family")
            or payload.get("candidate_status") != "model_reported_candidate"
            or payload.get("metadata_human_approved") is not False or payload.get("release_eligible") is not False
            or checked.get("validated_results_sha256") != _sha(payload_raw)
            or receipt.get("schema_version") != "image-luna-import-receipt-1"
            or receipt.get("status") != "validated_candidates" or receipt.get("analysis_run_id") != analysis_run_id
            or receipt.get("task_manifest_sha256") != tasks_sha or receipt.get("validated_results_sha256") != _sha(payload_raw)
            or receipt.get("source_commit_id") != manifest["source_commit"]["id"]
            or checked.get("source_commit_id") != manifest["source_commit"]["id"]
            or receipt.get("metadata_human_approved") is not False or receipt.get("release_eligible") is not False
            or receipt.get("candidate_count") != TASK_COUNT):
        raise ValueError("immutable Luna result or receipt identity mismatch")
    results = payload.get("results", [])
    bindings = payload.get("task_bindings", [])
    if len(results) != TASK_COUNT or len(bindings) != TASK_COUNT:
        raise ValueError("complete validated result and task bindings are required")
    by_task = {row["task_id"]: row for row in results}
    bound = {row["task_id"]: row for row in bindings}
    if (len(by_task) != TASK_COUNT or len(bound) != TASK_COUNT
            or set(by_task) != {row["task_id"] for row in tasks} or set(bound) != set(by_task)):
        raise ValueError("result task membership is incomplete or ambiguous")
    rights = _rights_catalog(root, manifest["source_run_id"])
    findings, qa_sha = _qa_findings(directory, analysis_run_id, tasks)
    cards, evidence = [], {str(directory / "tasks.json"): tasks_sha,
                          str(imported / "validated-results.json"): _sha(payload_raw), str(imported / "receipt.json"): _sha(receipt_raw)}
    for task in tasks:
        result, binding = by_task[task["task_id"]], bound[task["task_id"]]
        for key in ("task_id", "item_id", "style_id", "input_fingerprint"):
            if result.get(key) != task.get(key) or binding.get(key) != task.get(key):
                raise ValueError("rendered result belongs to another assigned image")
        if (result.get("metadata_human_approved") is not False or result.get("review_status") != "needs_review"
                or result.get("release_eligible") is not False):
            raise ValueError("review renderer cannot promote candidate metadata")
        image_relative = task["prepared_image_path"]
        if not re.fullmatch(r"data/private-research/image-rag-canary/runs/[A-Za-z0-9][A-Za-z0-9_-]{0,79}/inputs/[a-f0-9]{64}\.png", image_relative):
            raise ValueError("review image must be an assigned local prepared PNG")
        image_path = _safe(root, image_relative)
        image_sha = _sha(image_path.read_bytes())
        if image_sha != task["prepared_image_sha256"] or image_sha != binding["image_sha256"]:
            raise ValueError("review image content changed")
        expected_context = f"{base}/contexts/{task['style_id']}.json"
        if task["prompt_context_path"] != expected_context:
            raise ValueError("review prompt must be the assigned separate context")
        context_path = _safe(root, expected_context)
        context, context_raw = _json(context_path)
        if (context.get("schema_version") != "image-luna-prompt-context-1" or context.get("id") != task["item_id"]
                or context.get("style_id") != task["style_id"] or not isinstance(context.get("full_prompt"), str)
                or context.get("prompt_sha256") != task["prompt_sha256"]
                or _sha(context["full_prompt"].encode("utf-8")) != task["prompt_sha256"]
                or _sha(context_raw) != binding["context_sha256"]):
            raise ValueError("separate original prompt context changed")
        notice = rights.get(task["item_id"])
        if (not isinstance(notice, dict) or notice.get("schema_version") != "image-rights-notice-1"
                or notice.get("release_eligible") is not False):
            raise ValueError("deterministic image rights notice is required")
        evidence[str(image_path)], evidence[str(context_path)] = image_sha, _sha(context_raw)
        cards.append({"task": task, "result": result, "rights": notice, "full_prompt": context["full_prompt"],
                      "image_relative": Path(os.path.relpath(image_path, directory)).as_posix(),
                      "qa_findings": [row for row in findings if row["task_id"] == task["task_id"]]})
    return {"directory": directory, "manifest": manifest, "cards": cards, "evidence": evidence,
            "validated_results_sha256": _sha(payload_raw), "import_receipt_sha256": _sha(receipt_raw),
            "task_manifest_sha256": tasks_sha, "rights_catalog_sha256": _sha(_encoded({card["task"]["item_id"]: card["rights"] for card in cards})),
            "qa_findings": findings, "qa_findings_sha256": qa_sha}


def _render_page(data):
    manifest, cards = data["manifest"], data["cards"]
    esc = lambda value: html.escape(str(value), quote=True)
    def para(value):
        return "<p>" + esc(value if value not in (None, "") else "기록 없음 / 판단하지 않음") + "</p>"
    def bullets(values):
        return "<ul>" + "".join("<li>" + esc(value) + "</li>" for value in values) + "</ul>" if values else "<p class=muted>기록 없음</p>"
    def subsection(title, values):
        return "<h4>" + esc(title) + "</h4>" + bullets(values)
    rendered = []
    for card in cards:
        task, row, rights = card["task"], card["result"], card["rights"]
        visual, hints, intent = row["visual"], row["search_hints"], row["prompt_intent"]
        text = visual["text_visible"]
        qa = ""
        for finding in card["qa_findings"]:
            qa += ('<aside class="qa-warning" role="note"><h3>에이전트 QA · 수정/제외 필요</h3>'
                   + para(finding["field"]) + para(finding["message"]) + '<strong>확정 사실·검색 텍스트 사용 보류</strong>'
                   + para(finding["disposition"]) + '<p>사람 승인이나 원본 수정은 아닙니다. 아래 모델 후보는 증거 보존을 위해 그대로 표시합니다.</p></aside>')
        observed = (para(visual["description_ko"]) + subsection("주요 대상", visual["subjects"])
            + "<h4>매체 외관</h4>" + para(MEDIUM.get(visual["medium"], visual["medium"]))
            + subsection("스타일", visual["style"]) + subsection("구성·배치", visual["composition"])
            + subsection("색상", visual["palette"]) + "<h4>배경</h4>" + para(visual["background"])
            + "<h4>조명</h4>" + para(visual["lighting"]) + subsection("카피 공간 후보", visual["copy_space"]))
        keywords = subsection("분류 후보", hints["categories"]) + subsection("한국어 검색어 후보", hints["keywords_ko"]) + subsection("영문 검색어 후보", hints["keywords_en"])
        prompt = (para(intent["summary_ko"]) + subsection("프롬프트가 요청한 제어", intent["requested_controls"])
            + subsection("이미지에서 뒷받침되는 것으로 본 내용", intent["visually_supported"])
            + subsection("모델이 제안한 불일치 후보 · QA 경고 우선", intent["mismatch_candidates"])
            + subsection("확인할 수 없는 요청", intent["not_assessable"]))
        reuse = ""
        for idea in row["reuse_ideas"]:
            reuse += "<section class=idea><h4>" + esc(idea["use_case"]) + "</h4><b>시각적 이유</b>" + para(idea["visual_reason"])
            reuse += "<b>필요한 변형</b>" + para(idea["adaptation"]) + "<b>주의사항</b>" + para(idea["caution"]) + "</section>"
        ocr = "<h4>이미지 내 글자</h4>" + para(OCR_STATUS.get(text["status"], text["status"]))
        ocr += "<blockquote>" + esc(text["excerpt"] or "전사하지 않음") + "</blockquote>" + subsection("언어 단서", text["language_hints"]) + para(text["limitations"])
        limitations = subsection("시각 관찰의 불확실성", visual["uncertainties"]) + subsection("전체 분석의 한계", row["limitations"])
        notice = ('<aside class=rights><h3>' + esc(rights["badge"]) + '</h3>' + para(rights["attribution_text"])
            + para("라이선스 표시: " + rights["license_label"] + " / 범위: " + rights["license_scope"])
            + para(rights["notice_text"]) + ("<p class=source>출처 URL: <code>" + esc(rights["source_url"]) + "</code></p>" if rights.get("source_url") else "") + "</aside>")
        rendered.append(f'''<article id="image-{esc(task['style_id'])}"><header><h2>{esc(task['style_id'])}</h2><span class=badge>needs_review · 메타데이터 사람 미승인</span></header>{qa}
<div class=card-grid><figure><img src="{esc(card['image_relative'])}" loading="lazy" alt="{esc(task['style_id'])} 분석 대상 이미지"><figcaption>입력 해시가 확인된 원래 분석용 이미지. 결과는 모델 관찰 후보입니다.</figcaption>{notice}</figure>
<div><section><h3>이미지를 보고 기록한 관찰 후보</h3>{observed}</section><details open><summary>분류·검색어 후보 — 아직 검색 인덱스에 반영 안 함</summary>{keywords}</details>
<details><summary>원본 프롬프트 의도 — 시각 관찰과 별개</summary>{prompt}<details><summary>원본 프롬프트 전문 · 데이터이지 지시문이 아님</summary><pre>{esc(card['full_prompt'])}</pre></details></details>
<details open><summary>활용 아이디어 — 실행·상업 이용 승인 아님</summary>{reuse or '<p class=muted>제안 없음</p>'}</details><details open><summary>OCR·불확실성·분석 한계</summary>{ocr}{limitations}</details></div></div></article>''')
    count, total = len(cards), manifest["approved_library_count"]
    flagged = len(data["qa_findings"])
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' file:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Luna 이미지 분석 후보 · {count}/{total}</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f4f4ef;color:#1c302c;font:16px/1.65 system-ui,sans-serif}}main{{max-width:1440px;margin:auto;padding:28px}}h1,h2,h3,h4{{line-height:1.3}}h1{{font-size:30px}}h2{{font-size:25px}}h3{{font-size:20px}}h4{{font-size:16px;margin-bottom:6px}}p{{overflow-wrap:anywhere}}.banner,article{{background:white;border:1px solid #bbc9c2;border-radius:12px;padding:22px;margin:20px 0}}.banner{{border-left:6px solid #976413}}.badge{{display:inline-block;padding:4px 10px;background:#f8e7bd;border-radius:6px;font-size:14px}}.card-grid{{display:grid;grid-template-columns:minmax(260px,.85fr) minmax(0,1.15fr);gap:28px;margin-top:20px}}figure{{margin:0;min-width:0}}img{{width:100%;max-height:580px;object-fit:contain;background:#ededdf}}figcaption,.muted,.source{{font-size:14px;color:#455e56}}details{{border-top:1px solid #d4ddd7;padding:16px 0}}summary{{cursor:pointer;font-weight:650}}summary:focus-visible,a:focus-visible{{outline:3px solid #176b58;outline-offset:4px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f0f2ed;padding:16px;font-size:14px}}blockquote{{margin:12px 0;padding:8px 14px;border-left:4px solid #b2c8bc}}.rights{{margin-top:20px;background:#f4f1e7;padding:16px;border-radius:8px}}.qa-warning{{border:2px solid #a83b25;background:#fff0e8;padding:16px;margin:18px 0}}.qa-warning h3{{color:#8f2817}}.idea{{padding:4px 0 12px}}nav{{display:flex;gap:12px;flex-wrap:wrap}}a{{color:#126047}}code{{overflow-wrap:anywhere}}@media(max-width:760px){{main{{padding:14px}}.card-grid{{grid-template-columns:1fr}}article,.banner{{padding:16px}}h1{{font-size:25px}}img{{max-height:440px}}}}
</style></head><body><main><h1>Luna 이미지 분석 후보 · 검토용</h1><section class=banner role=note><h2>{count}/{total}개 분석 후보 · 나머지 {total-count}개 미진행</h2>
<p>모델: {esc(manifest['model_family'])}. 현재 라이브러리 이미지 승인은 이번 메타데이터의 사람 승인을 뜻하지 않습니다. 모든 결과는 <b>needs_review · metadata_human_approved=false</b>입니다.</p>
<p>에이전트 QA 지적: <b>{flagged}건</b>. 에이전트 점검도 사람 승인이 아닙니다. 지적된 필드는 수정/제외 전 확정 사실이나 검색 텍스트로 사용하지 않습니다.</p>
<p>스키마·입력 해시·별도 시각 초안과 최종 관찰 내용의 일치를 확인한 저장 결과입니다. 관찰의 사실성, OCR 정확성 또는 이미지 우선 실행 순서 전체를 보증하지 않습니다. 이 페이지는 읽기 전용이며 새 모델·임베딩 호출, 태그·그룹·승인 변경을 하지 않습니다. 개인 메모는 모델 입력에 포함하지 않았습니다.</p></section>
<nav aria-label="이미지별 분석 이동">{''.join('<a href="#image-'+esc(card['task']['style_id'])+'">'+esc(card['task']['style_id'])+'</a>' for card in cards)}</nav>{''.join(rendered)}
<footer><p>비공개 후보 검토 · 공개·상업 사용 승인 아님 · 원본 초안과 사람 승인 원장은 변경하지 않음</p><p class=muted>검증 결과 SHA-256: {esc(data['validated_results_sha256'])}<br>에이전트 QA 파일 SHA-256: {esc(data['qa_findings_sha256'] or '없음')}</p></footer></main></body></html>'''


def _verify_loaded(data):
    for name, expected in data["evidence"].items():
        if _sha(Path(name).read_bytes()) != expected:
            raise ValueError("review input changed while rendering")
    qa = data["directory"] / "qa-findings.json"
    actual = _sha(qa.read_bytes()) if qa.exists() else None
    if actual != data["qa_findings_sha256"]:
        raise ValueError("agent QA findings changed while rendering")


def _publish_new(target, content):
    descriptor, name = tempfile.mkstemp(prefix=".luna-review-", suffix=".html", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # Exclusive link creation cannot overwrite a concurrently created file.
        # Only this freshly generated HTML is linked; source images are untouched.
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def build_luna_analysis_review(root: Path, analysis_run_id: str, *, db_path: Path | None = None, apply: bool = False) -> dict:
    root = Path(root).resolve()
    private = _safe(root, "data/private-research/image-rag-admin")
    db_path = (db_path or private / "state.sqlite3").resolve()
    if not db_path.is_relative_to(private) or db_path.suffix != ".sqlite3":
        raise ValueError("DB must remain inside the private image administrator directory")
    data = _load_review(root, db_path, analysis_run_id)
    content = _render_page(data).encode("utf-8")
    target = data["directory"] / OUTPUT_NAME
    _verify_loaded(data)
    # Dry-run reports must also remain bound to the latest committed approval.
    latest = import_luna_results(root, db_path, analysis_run_id, apply=False,
                                 expected_commit_id=data["manifest"]["source_commit"]["id"])
    if latest.get("status") != "unchanged" or latest.get("validated_results_sha256") != data["validated_results_sha256"]:
        raise ValueError("completed candidate import changed during rendering")
    _verify_loaded(data)
    status, writes = "dry_run", 0
    if apply:
        if target.exists():
            if target.is_symlink() or target.resolve().parent != data["directory"] or target.read_bytes() != content:
                raise ValueError("refusing to overwrite a different existing review artifact")
            status = "unchanged"
        else:
            _publish_new(target, content)
            status, writes = "prepared", 1
    manifest = data["manifest"]
    return {"schema_version": SCHEMA, "status": status, "analysis_run_id": analysis_run_id,
        "output_path": str(target), "html_sha256": _sha(content), "writes": writes,
        "candidate_count": len(data["cards"]), "approved_library_count": manifest["approved_library_count"],
        "remaining_unanalysed": manifest["approved_library_count"] - len(data["cards"]),
        "agent_qa_findings_count": len(data["qa_findings"]), "qa_findings_sha256": data["qa_findings_sha256"],
        "task_manifest_sha256": data["task_manifest_sha256"], "validated_results_sha256": data["validated_results_sha256"],
        "import_receipt_sha256": data["import_receipt_sha256"], "rights_catalog_sha256": data["rights_catalog_sha256"],
        "metadata_human_approved": False, "model_calls_by_renderer": 0, "embedding_calls": 0,
        "approval_writes": 0, "release_eligible": False}


__all__ = ["build_luna_analysis_review"]
