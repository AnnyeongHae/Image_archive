"""Render the second Luna batch with cumulative and measured-token context."""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import tempfile
from pathlib import Path

from .incremental_workflow import load_frozen_workflow
from .luna_analysis_import import _json
from .luna_reuse_analysis_import import RELATIVE_ROOT, import_luna_reuse_results
from .rights import build_rights_catalog

SCHEMA = "image-luna-reuse-analysis-review-2"
OUTPUT_NAME = "results-review.html"
TASK_COUNT = 10
MEDIUM = {
    "photograph": "사진", "illustration": "일러스트", "3d_render_appearance": "3D 렌더처럼 보임",
    "graphic_design": "그래픽 디자인", "screenshot": "화면 캡처", "mixed": "혼합", "unknown": "판단 불가",
}
SETTING = {
    "transparent_or_cutout": "투명/누끼형", "plain": "단색", "studio": "스튜디오", "interior": "실내",
    "exterior": "외부 공간", "landscape": "풍경", "abstract": "추상", "information_layout": "정보 레이아웃",
    "mixed": "혼합", "unknown": "판단 불가",
}


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _encoded(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _safe(root: Path, relative: str) -> Path:
    if (not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative
            or Path(relative).is_absolute() or ".." in Path(relative).parts):
        raise ValueError("Unsafe Luna review source path")
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Luna review path escaped archive")
    return path


def _qa_findings(directory: Path, analysis_run_id: str, tasks: list[dict]) -> tuple[list[dict], str | None]:
    path = directory / "qa-findings.json"
    if not path.exists():
        return [], None
    if path.is_symlink() or path.resolve().parent != directory:
        raise ValueError("QA findings must stay in the analysis run")
    document, raw = _json(path)
    required = {"schema_version", "analysis_run_id", "qa_kind", "metadata_human_approved", "release_eligible", "findings"}
    if (set(document) != required or document.get("schema_version") != "image-luna-reuse-agent-qa-2"
            or document.get("analysis_run_id") != analysis_run_id
            or document.get("qa_kind") != "orchestrator_visual_review_not_human_approval"
            or document.get("metadata_human_approved") is not False or document.get("release_eligible") is not False):
        raise ValueError("Invalid reuse-analysis QA contract")
    identities = {(task["task_id"], task["style_id"]) for task in tasks}
    findings = document["findings"]
    if not isinstance(findings, list) or len(findings) > 50:
        raise ValueError("QA findings must be a bounded list")
    pattern = re.compile(r"(?:visual|prompt_analysis|usage_selection|limitations)(?:\.[a-z_]+|\[[0-9]+\])*")
    for row in findings:
        if (not isinstance(row, dict) or set(row) != {"task_id", "style_id", "field", "status", "message", "disposition"}
                or (row.get("task_id"), row.get("style_id")) not in identities
                or row.get("status") not in {"needs_correction_before_acceptance", "needs_review"}
                or not isinstance(row.get("field"), str) or not pattern.fullmatch(row["field"])
                or any(not isinstance(row.get(key), str) or not row[key] or len(row[key]) > 1800
                       for key in ("message", "disposition"))):
            raise ValueError("QA finding is not bound to an assigned result field")
    return findings, _sha(raw)


def _token_receipt(directory: Path, manifest: dict, manifest_sha: str) -> tuple[dict, bytes]:
    receipt, raw = _json(directory / "token-usage-receipt.json")
    tasks = manifest["tasks"]
    expected_styles = {task["style_id"] for task in tasks}
    per_image = receipt.get("per_image")
    usage = receipt.get("usage")
    numeric = {
        "input_tokens_including_cached", "cached_input_tokens", "cache_write_input_tokens",
        "uncached_input_tokens_calculated", "output_tokens_including_reasoning",
        "reasoning_output_tokens", "total_tokens",
    }
    if (receipt.get("schema_version") != "image-luna-token-usage-receipt-2"
            or receipt.get("analysis_run_id") != manifest["analysis_run_id"]
            or receipt.get("evidence_status") != "observed_isolated_local_codex_logs"
            or receipt.get("model_reported") != manifest["model_family"]
            or receipt.get("completed_image_count") != TASK_COUNT
            or receipt.get("task_manifest_sha256") != manifest_sha
            or receipt.get("actual_billed_tokens") is not None or receipt.get("actual_billed_cost") is not None
            or not isinstance(usage, dict) or any(type(usage.get(key)) is not int or usage[key] < 0 for key in numeric)
            or not isinstance(per_image, list) or len(per_image) != TASK_COUNT
            or {row.get("style_id") for row in per_image} != expected_styles):
        raise ValueError("Measured token receipt is incomplete or mismatched")
    if (usage["uncached_input_tokens_calculated"] != usage["input_tokens_including_cached"] - usage["cached_input_tokens"]
            or usage["total_tokens"] != usage["input_tokens_including_cached"] + usage["output_tokens_including_reasoning"]
            or any(usage[key] != sum(row.get(key, -1) for row in per_image) for key in numeric)):
        raise ValueError("Measured token totals do not match per-image sessions")
    return receipt, raw


def _execution_receipt(directory: Path, manifest: dict, manifest_sha: str, validated_sha: str, token_sha: str) -> tuple[dict, bytes]:
    receipt, raw = _json(directory / "execution-receipt.json")
    if (receipt.get("schema_version") != "image-luna-reuse-orchestrator-execution-2"
            or receipt.get("analysis_run_id") != manifest["analysis_run_id"]
            or receipt.get("model_assignment", {}).get("model") != manifest["model_family"]
            or receipt.get("orchestrator_observed", {}).get("task_manifest_sha256") != manifest_sha
            or receipt.get("orchestrator_observed", {}).get("validated_results_sha256") != validated_sha
            or receipt.get("orchestrator_observed", {}).get("token_usage_receipt_sha256") != token_sha
            or receipt.get("agent_reported", {}).get("completed_after_retries") != TASK_COUNT
            or receipt.get("metadata_human_approved") is not False
            or receipt.get("release_eligible") is not False):
        raise ValueError("Execution receipt is incomplete or mismatched")
    return receipt, raw


def _load(root: Path, db_path: Path, analysis_run_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", analysis_run_id):
        raise ValueError("Invalid analysis run ID")
    base = f"{RELATIVE_ROOT}/{analysis_run_id}"
    directory = _safe(root, base)
    manifest, manifest_raw = _json(directory / "tasks.json")
    manifest_sha = _sha(manifest_raw)
    checked = import_luna_reuse_results(root, db_path, analysis_run_id, apply=False)
    expected_import = f"{base}/imports/{manifest_sha}"
    if (checked.get("status") != "unchanged" or checked.get("output_path") != expected_import
            or checked.get("candidate_count") != TASK_COUNT or checked.get("cumulative_unique_count") != 20):
        raise ValueError("Complete immutable reuse-analysis import is required")
    imported = _safe(root, expected_import)
    payload, payload_raw = _json(imported / "validated-results.json")
    import_receipt, import_raw = _json(imported / "receipt.json")
    if (payload.get("schema_version") != "image-luna-reuse-validated-results-2"
            or payload.get("task_manifest_sha256") != manifest_sha
            or import_receipt.get("validated_results_sha256") != _sha(payload_raw)
            or import_receipt.get("task_manifest_sha256") != manifest_sha
            or payload.get("metadata_human_approved") is not False or payload.get("release_eligible") is not False):
        raise ValueError("Imported reuse-analysis identity mismatch")
    token, token_raw = _token_receipt(directory, manifest, manifest_sha)
    execution, execution_raw = _execution_receipt(directory, manifest, manifest_sha, _sha(payload_raw), _sha(token_raw))
    tasks, results, bindings = manifest["tasks"], payload["results"], payload["task_bindings"]
    by_task = {row["task_id"]: row for row in results}
    bound = {row["task_id"]: row for row in bindings}
    if (len(by_task) != TASK_COUNT or len(bound) != TASK_COUNT
            or set(by_task) != {task["task_id"] for task in tasks} or set(bound) != set(by_task)):
        raise ValueError("Result task membership is incomplete")
    taxonomy, taxonomy_raw = _json(_safe(root, manifest["taxonomy_context_path"]))
    labels = {row["use_case_id"]: row["label_ko"] for row in taxonomy["use_cases"]}
    rights = build_rights_catalog(root, load_frozen_workflow(root, manifest["source_run_id"]))
    findings, qa_sha = _qa_findings(directory, analysis_run_id, tasks)
    cards, evidence = [], {
        str(directory / "tasks.json"): manifest_sha,
        str(imported / "validated-results.json"): _sha(payload_raw),
        str(imported / "receipt.json"): _sha(import_raw),
        str(directory / "token-usage-receipt.json"): _sha(token_raw),
        str(directory / "execution-receipt.json"): _sha(execution_raw),
        str(directory / "taxonomy-context.json"): _sha(taxonomy_raw),
    }
    for task in tasks:
        result, binding = by_task[task["task_id"]], bound[task["task_id"]]
        image = _safe(root, task["prepared_image_path"])
        if _sha(image.read_bytes()) != task["prepared_image_sha256"]:
            raise ValueError("Review image changed")
        context, context_raw = _json(_safe(root, task["prompt_context_path"]))
        if (context.get("prompt_sha256") != task["prompt_sha256"]
                or _sha(context.get("full_prompt", "").encode("utf-8")) != task["prompt_sha256"]):
            raise ValueError("Review prompt changed")
        notice = rights.get(task["item_id"])
        if not isinstance(notice, dict) or notice.get("release_eligible") is not False:
            raise ValueError("Conservative rights notice is required")
        evidence[str(image)] = task["prepared_image_sha256"]
        evidence[str(_safe(root, task["prompt_context_path"]))] = _sha(context_raw)
        cards.append({
            "task": task, "result": result, "binding": binding, "rights": notice,
            "full_prompt": context["full_prompt"],
            "image_relative": Path(os.path.relpath(image, directory)).as_posix(),
            "qa_findings": [row for row in findings if row["task_id"] == task["task_id"]],
        })
    prior_id = manifest["prior_batches"][0]["analysis_run_id"]
    prior_review = _safe(root, f"{RELATIVE_ROOT}/{prior_id}/results-review.html")
    if not prior_review.is_file():
        raise ValueError("Prior ten-image review is missing")
    evidence[str(prior_review)] = _sha(prior_review.read_bytes())
    return {
        "directory": directory, "manifest": manifest, "cards": cards, "labels": labels, "token": token,
        "execution": execution, "evidence": evidence, "task_manifest_sha256": manifest_sha,
        "validated_results_sha256": _sha(payload_raw), "import_receipt_sha256": _sha(import_raw),
        "token_receipt_sha256": _sha(token_raw), "execution_receipt_sha256": _sha(execution_raw),
        "qa_findings": findings, "qa_findings_sha256": qa_sha, "prior_id": prior_id,
        "rights_catalog_sha256": _sha(_encoded({card["task"]["item_id"]: card["rights"] for card in cards})),
    }


def _render(data: dict) -> str:
    esc = lambda value: html.escape(str(value), quote=True)
    para = lambda value: "<p>" + esc(value if value not in (None, "") else "기록 없음 / 판단하지 않음") + "</p>"
    def bullets(values):
        return "<ul>" + "".join("<li>" + esc(value) + "</li>" for value in values) + "</ul>" if values else "<p class=muted>기록 없음</p>"
    def subsection(title, values):
        return "<h4>" + esc(title) + "</h4>" + bullets(values)
    def use_case(row, rank):
        if row is None:
            return "<section class=idea><h4>정규화 활용 작업 기권</h4></section>"
        label = data["labels"].get(row["use_case_id"], "알 수 없는 ID")
        return (f'<section class=idea><h4>{esc(rank)} · {esc(row["use_case_id"])} · {esc(label)}</h4>'
                + para(f"재사용 방식: {row['reuse_mode']} / 적합도: {row['fit']} / 근거: {row['evidence_basis']}")
                + "<b>왜 쓸 수 있나</b>" + para(row["why_usable_ko"])
                + "<b>무엇을 바꿔야 하나</b>" + para(row["adaptation_ko"])
                + subsection("보이는 근거", row["visual_evidence_ko"])
                + subsection("제약", row["constraints_ko"]) + "</section>")
    rendered = []
    for card in data["cards"]:
        task, row, rights = card["task"], card["result"], card["rights"]
        visual, prompt, selection = row["visual"], row["prompt_analysis"], row["usage_selection"]
        background, editability = visual["background"], visual["editability"]
        group = task["group_context"]
        group_text = (f"그룹 대표 · {group['member_count']}개 묶음 · {group['group_id']}"
                      if group["group_id"] else "미그룹 승인 이미지")
        qa = "".join(
            '<aside class="qa-warning" role="note"><h3>에이전트 QA · 수정/제외 필요</h3>'
            + para(finding["field"]) + para(finding["message"])
            + "<strong>수정 전 검색 텍스트 사용 보류</strong>" + para(finding["disposition"]) + "</aside>"
            for finding in card["qa_findings"]
        )
        visual_html = (para(visual["description_ko"]) + subsection("스타일", visual["styles"])
            + "<h4>배경</h4>" + para(background["description_ko"])
            + para(f"배경 유형: {SETTING.get(background['setting'], background['setting'])} / 분리 난이도: {background['removability']}")
            + para("판단 근거: " + background["evidence_ko"]) + subsection("레이아웃", visual["layout"])
            + subsection("주요 대상", visual["subjects"]) + subsection("카피 공간", visual["copy_space"])
            + "<h4>편집 가능성</h4>" + para("전체 난이도: " + editability["overall"])
            + para(editability["evidence_ko"]) + subsection("분리·교체 후보", editability["separable_elements"])
            + subsection("편집 제약", editability["hard_constraints"])
            + subsection("한국어 검색어 후보", visual["search_keywords_ko"])
            + subsection("영문 검색어 후보", visual["search_keywords_en"]))
        prompt_html = (para(prompt["intended_purpose_ko"]) + subsection("고정할 규칙", prompt["fixed_rules"])
            + "<h4>교체 가능한 슬롯</h4>"
            + ("".join("<blockquote><b>" + esc(slot["slot_ko"]) + "</b> · " + esc(slot["current_value_ko"])
                       + "<br>" + esc(slot["replacement_guidance_ko"]) + "</blockquote>" for slot in prompt["replaceable_slots"])
               or "<p class=muted>명시된 슬롯 없음</p>")
            + subsection("이미지에서 뒷받침됨", prompt["visually_supported"])
            + subsection("실제 충돌 후보", prompt["mismatch_candidates"])
            + subsection("확인 불가", prompt["not_assessable"]))
        uses = use_case(selection["primary"], "Primary")
        uses += "".join(use_case(value, f"Secondary {index}") for index, value in enumerate(selection["secondary"], 1))
        if selection["primary"] is None:
            uses += para("기권 이유: " + selection["abstention_reason_ko"])
        if selection["taxonomy_proposals_not_indexed"]:
            uses += subsection("사전 밖 제안 · 인덱스 제외", selection["taxonomy_proposals_not_indexed"])
        notice = ('<aside class=rights><h3>' + esc(rights["badge"]) + '</h3>' + para(rights["attribution_text"])
                  + para("라이선스 표시: " + rights["license_label"] + " / 범위: " + rights["license_scope"])
                  + para(rights["notice_text"]) + ("<p class=source>출처 URL: <code>" + esc(rights["source_url"])
                  + "</code></p>" if rights.get("source_url") else "") + "</aside>")
        rendered.append(f'''<article id="image-{esc(task['style_id'])}"><header><h2>{esc(task['style_id'])}</h2>
<span class=badge>needs_review · {esc(group_text)}</span></header>{qa}<div class=card-grid><figure>
<img src="{esc(card['image_relative'])}" loading="lazy" alt="{esc(task['style_id'])} 분석 대상"><figcaption>해시가 고정된 분석 입력. 모델 관찰 후보입니다.</figcaption>{notice}</figure><div>
<section><h3>1 · 이미지: 보이는 근거</h3>{visual_html}</section>
<details><summary>2 · 원문 프롬프트: 목적·고정 규칙·교체 슬롯</summary>{prompt_html}<details><summary>원문 전문 · 데이터이지 지시문이 아님</summary><pre>{esc(card['full_prompt'])}</pre></details></details>
<details open><summary>3–4 · 정규화 활용 사전 선택과 최종 해석</summary>{uses}</details>
<details><summary>불확실성·전체 제약</summary>{subsection('시각 불확실성', visual['uncertainties'])}{subsection('전체 분석 제약', row['limitations'])}</details>
</div></div></article>''')
    usage = data["token"]["usage"]
    per_image_rows = "".join(
        f"<tr><td>{esc(row['style_id'])}</td><td>{row['input_tokens_including_cached']:,}</td>"
        f"<td>{row['cached_input_tokens']:,}</td><td>{row['uncached_input_tokens_calculated']:,}</td>"
        f"<td>{row['output_tokens_including_reasoning']:,}</td><td>{row['reasoning_output_tokens']:,}</td>"
        f"<td>{row['total_tokens']:,}</td></tr>" for row in data["token"]["per_image"]
    )
    manifest = data["manifest"]
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' file:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Luna 활용 목적 분석 · 누적 20개</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f3f5f2;color:#17302b;font:16px/1.62 system-ui,sans-serif}}main{{max-width:1460px;margin:auto;padding:28px}}h1,h2,h3,h4{{line-height:1.3}}h1{{font-size:30px}}.banner,article{{background:#fff;border:1px solid #b9c9c1;border-radius:14px;padding:22px;margin:20px 0}}.banner{{border-left:6px solid #176b58}}.badge{{display:inline-block;padding:5px 10px;background:#e6f2ec;border-radius:999px;font-size:13px}}.metrics{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}}.metric{{background:#edf3ef;padding:12px;border-radius:9px}}.metric b{{display:block;font-size:21px}}.card-grid{{display:grid;grid-template-columns:minmax(280px,.8fr) minmax(0,1.2fr);gap:28px;margin-top:18px}}figure{{margin:0;min-width:0}}img{{width:100%;max-height:620px;object-fit:contain;background:#eee}}figcaption,.muted,.source{{font-size:14px;color:#496159}}details{{border-top:1px solid #d4ddd7;padding:15px 0}}summary{{cursor:pointer;font-weight:700}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f0f2ed;padding:16px}}blockquote{{margin:10px 0;padding:9px 14px;border-left:4px solid #9ab9aa}}.rights{{margin-top:18px;background:#f5f0e4;padding:15px;border-radius:9px}}.qa-warning{{border:2px solid #a83b25;background:#fff0e8;padding:16px;margin:16px 0}}.idea{{padding:5px 0 14px}}nav{{display:flex;gap:10px;flex-wrap:wrap}}a{{color:#126047}}table{{width:100%;border-collapse:collapse;font-size:14px}}th,td{{padding:8px;border-bottom:1px solid #ccd8d1;text-align:right}}th:first-child,td:first-child{{text-align:left}}.table-wrap{{overflow-x:auto}}code{{overflow-wrap:anywhere}}@media(max-width:800px){{main{{padding:13px}}.card-grid{{grid-template-columns:1fr}}.metrics{{grid-template-columns:1fr 1fr}}article,.banner{{padding:15px}}}}
</style></head><body><main><h1>Luna 활용 목적 이미지 분석 · 누적 20개</h1><section class=banner role=note>
<h2>기존 10개 + 신규 v2 10개 = 20개</h2><p><a href="../{esc(data['prior_id'])}/results-review.html">기존 10개 v1 검토 페이지</a>는 그대로 보존했습니다. 이 페이지는 새 10개에 이미지/프롬프트/활용 사전/최종 해석 역할 분리를 적용합니다.</p>
<p>379개 승인 라이브러리 중 분석 후보 누적 20개, 미분석 359개입니다. 신규 10개는 승인 그룹 6개의 대표와 미그룹 4개이며 종속 이미지를 중복 분석하지 않았습니다.</p>
<p>모든 결과는 <b>needs_review</b>이고 메타데이터 사람 미승인·공개 불가입니다. 임베딩/Voyage/Gemini/Qdrant 호출은 없었습니다.</p></section>
<section class=banner><h2>이번 신규 10개 실제 토큰 사용량</h2><div class=metrics>
<div class=metric>Input · cached 포함<b>{usage['input_tokens_including_cached']:,}</b></div><div class=metric>그중 cached<b>{usage['cached_input_tokens']:,}</b></div>
<div class=metric>계산된 uncached input<b>{usage['uncached_input_tokens_calculated']:,}</b></div><div class=metric>Output · reasoning 포함<b>{usage['output_tokens_including_reasoning']:,}</b></div>
<div class=metric>그중 reasoning<b>{usage['reasoning_output_tokens']:,}</b></div><div class=metric>Total<b>{usage['total_tokens']:,}</b></div></div>
<p>로컬 Codex의 이미지별 격리 세션 로그에서 계측했습니다. cached는 input의 부분집합이고 reasoning은 output의 부분집합이므로 다시 더하지 않습니다. 실제 청구 토큰·금액은 이 로그만으로 확정하지 않습니다.</p>
<details><summary>이미지별 사용량 보기</summary><div class=table-wrap><table><thead><tr><th>Style ID</th><th>Input</th><th>Cached</th><th>Uncached</th><th>Output</th><th>Reasoning</th><th>Total</th></tr></thead><tbody>{per_image_rows}</tbody></table></div></details></section>
<p>에이전트 시각 QA 지적: <b>{len(data['qa_findings'])}건</b>. 지적 없음도 오류 없음 인증은 아닙니다.</p>
<nav aria-label="신규 이미지별 이동">{''.join('<a href="#image-'+esc(card['task']['style_id'])+'">'+esc(card['task']['style_id'])+'</a>' for card in data['cards'])}</nav>
{''.join(rendered)}<footer><p>비공개 분석 후보 · 권리/메타데이터/릴리스 사람 승인 아님</p>
<p class=muted>tasks {esc(data['task_manifest_sha256'])}<br>validated {esc(data['validated_results_sha256'])}<br>tokens {esc(data['token_receipt_sha256'])}<br>execution {esc(data['execution_receipt_sha256'])}</p></footer></main></body></html>'''


def _verify(data: dict) -> None:
    for path, expected in data["evidence"].items():
        if _sha(Path(path).read_bytes()) != expected:
            raise ValueError("Review input changed during rendering")
    qa = data["directory"] / "qa-findings.json"
    actual = _sha(qa.read_bytes()) if qa.exists() else None
    if actual != data["qa_findings_sha256"]:
        raise ValueError("QA findings changed during rendering")


def _publish_new(target: Path, content: bytes) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".luna-reuse-review-", suffix=".html", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def build_luna_reuse_review(root: Path, analysis_run_id: str, *, db_path: Path | None = None, apply: bool = False) -> dict:
    root = Path(root).resolve()
    private = _safe(root, "data/private-research/image-rag-admin")
    db_path = (db_path or private / "state.sqlite3").resolve()
    if not db_path.is_relative_to(private) or db_path.suffix != ".sqlite3":
        raise ValueError("DB must remain inside the private administrator directory")
    data = _load(root, db_path, analysis_run_id)
    content = _render(data).encode("utf-8")
    target = data["directory"] / OUTPUT_NAME
    _verify(data)
    latest = import_luna_reuse_results(root, db_path, analysis_run_id, apply=False,
                                       expected_commit_id=data["manifest"]["source_commit"]["id"])
    if latest.get("status") != "unchanged" or latest.get("validated_results_sha256") != data["validated_results_sha256"]:
        raise ValueError("Candidate import changed during rendering")
    _verify(data)
    status, writes = "dry_run", 0
    if apply:
        if target.exists():
            if target.is_symlink() or target.resolve().parent != data["directory"] or target.read_bytes() != content:
                raise ValueError("Refusing to overwrite a different review artifact")
            status = "unchanged"
        else:
            _publish_new(target, content)
            status, writes = "prepared", 1
    return {
        "schema_version": SCHEMA, "status": status, "analysis_run_id": analysis_run_id,
        "output_path": str(target), "html_sha256": _sha(content), "writes": writes,
        "new_candidate_count": len(data["cards"]), "cumulative_candidate_count": data["manifest"]["cumulative_unique_target"],
        "approved_library_count": data["manifest"]["approved_library_count"],
        "agent_qa_findings_count": len(data["qa_findings"]), "qa_findings_sha256": data["qa_findings_sha256"],
        "token_usage": data["token"]["usage"], "token_receipt_sha256": data["token_receipt_sha256"],
        "task_manifest_sha256": data["task_manifest_sha256"], "validated_results_sha256": data["validated_results_sha256"],
        "import_receipt_sha256": data["import_receipt_sha256"], "rights_catalog_sha256": data["rights_catalog_sha256"],
        "metadata_human_approved": False, "model_calls_by_renderer": 0, "embedding_calls": 0,
        "approval_writes": 0, "release_eligible": False,
    }


__all__ = ["build_luna_reuse_review"]
