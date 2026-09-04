"""Build a private immutable checkpoint gallery; never changes approval."""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from image_rag_eval.luna_analysis_import import _json, _path, digest, encode
from image_rag_eval.luna_compact import contract, validate_compact
from image_rag_eval.compact_projection import project_compact
from prepare_luna_full_library import BASE, read_manifest, immutable, validate_progress


def build(root: Path, *, apply=False) -> dict:
    manifest, _ = read_manifest(root)
    progress = validate_progress(root)
    pinned = contract(root)
    valid = set(progress["completed_styles"])
    sources = {r["id"]: r for r in manifest["source_records"]}
    groups = manifest["approved_groups"]
    membership = {ident: g for g in groups for ident in g["member_ids"]}
    representatives = {g["suggested_representative_id"] for g in groups}
    tasks = {t["item_id"]: t for t in manifest["tasks"]}
    qa_path = root / BASE / "quality-review.json"
    quality = _json(qa_path)[0] if qa_path.exists() else {"findings": []}
    result_hashes = []
    def esc(value):
        return html.escape(str(value), quote=True)
    def card(ident):
        task, source = tasks[ident], sources[ident]
        style = task["style_id"]
        raw = None
        result_sha = None
        status = "분석 대기"
        if task["analysis_mode"] == "legacy_reuse":
            raw_path = _path(root, task["legacy"]["task"]["raw_result_path"])
            raw, blob = _json(raw_path)
            status = "기존 분석 재사용 · " + task["legacy"]["analysis_run_id"]
            result_hashes.append(digest(blob))
            result_sha = digest(blob)
        elif style in valid:
            raw, blob = _json(_path(root, task["raw_result_path"]))
            draft, _ = _json(_path(root, task["visual_draft_path"]))
            projection = project_compact(raw, draft, pinned, expected_style_id=style,
                                         original_prompt=source["original_prompt"]["full_prompt"])
            raw = projection["result"]
            status = "Luna v3 후보 · 사람 메타데이터 검수 전"
            if projection["normalization"]:
                status += " · 항목 무손실 정규화 파생본"
            result_hashes.append(digest(blob))
            result_sha = digest(blob)
        visual = (raw or {}).get("visual", (raw or {}).get("visual_analysis", {}))
        uses = (raw or {}).get("uses", (raw or {}).get("usage_selection", (raw or {}).get("reuse_ideas", [])))
        memo = (source.get("approval") or {}).get("memo_text", "")
        image_path = _path(root, task["prepared_image_path"])
        current_findings = [f for f in quality["findings"] if f["style_id"] == style
                            and f.get("applies_to_result_sha256", result_sha) == result_sha]
        historical_findings = [f for f in quality["findings"] if f["style_id"] == style and f not in current_findings]
        warning = ('<div class="warn">현재 결과 검수 경고<ul>' + ''.join('<li>' + esc(f.get("observation_ko", f.get("kind", ""))) + '</li>' for f in current_findings) + '</ul></div>') if current_findings else ""
        if historical_findings:
            warning += '<details><summary>수정 전 결과의 검토 이력</summary><pre>' + esc(json.dumps(historical_findings, ensure_ascii=False, indent=2)) + '</pre></details>'
        info = '<p>메타데이터 생성 대기</p>' if raw is None else (
            '<details><summary>관찰·스타일·배경</summary><pre>' + esc(json.dumps(visual, ensure_ascii=False, indent=2)) + '</pre></details>'
            '<details open><summary>활용 아이디어</summary><pre>' + esc(json.dumps(uses, ensure_ascii=False, indent=2)) + '</pre></details>'
            '<details><summary>현재 검토용 분석 JSON</summary><pre>' + esc(json.dumps(raw, ensure_ascii=False, indent=2)) + '</pre></details>')
        return f'<article><img loading="lazy" src="{esc(image_path.as_uri())}" alt="{esc(style)}"><section><h3>{esc(style)}</h3><small>{esc(status)}</small>{warning}<p class="memo">개인 메모: {esc(memo or "없음")}</p>{info}<details><summary>원문 프롬프트</summary><pre>{esc(source["original_prompt"]["full_prompt"])}</pre></details></section></article>'
    blocks = []
    for ident, task in tasks.items():
        group = membership.get(ident)
        if group and ident not in representatives:
            continue
        block = card(ident)
        if group:
            members = [x for x in group["member_ids"] if x != ident and x in tasks]
            block += f'<details class="members"><summary>그룹 하위 이미지 {len(members)}개 펼치기</summary>' + ''.join(card(x) for x in members) + '</details>'
        blocks.append('<div class="unit">' + block + '</div>')
    identity = {"manifest": digest(encode(manifest)), "result_hashes": result_hashes, "quality": quality,
                "progress": progress, "renderer_sha256": digest(Path(__file__).read_bytes())}
    key = digest(encode(identity))
    html_text = '''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Luna 전체 승인본 분석</title>
<style>body{background:#f5f6f8;color:#16202b;font:15px/1.6 system-ui;margin:0}header,main{max-width:1280px;margin:auto;padding:24px}header{position:sticky;top:0;background:#f5f6f8ee;z-index:1}h1{font-size:24px;margin:0}article{display:grid;grid-template-columns:minmax(200px,35%) 1fr;gap:20px;padding:20px;background:white;border-radius:12px}img{width:100%;max-height:500px;object-fit:contain;background:#eef0f4}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.65 system-ui}summary{cursor:pointer;padding:10px;border-bottom:1px solid #ddd}section{min-width:0}small{color:#526274}.unit{margin-bottom:24px;border:1px solid #d6dce4;border-radius:12px;overflow:hidden}.members{padding:12px;background:#e9edf4}.members article{margin-top:12px}.warn{color:#8e3400;background:#fff2d4;padding:10px}.memo{background:#edf7f2;padding:8px}@media(max-width:680px){article{grid-template-columns:1fr}header{position:static}}</style>
<header><h1>Luna 전체 승인본 분석 · 비공개 검토</h1><p>''' + esc(f"승인 379개 / 기존 20개 + 새 검증 후보 {progress['new_valid']}개 / 새 분석 대기 {progress['missing']}개") + '''</p><p>대표 1개만 기본 표시합니다. 하위 이미지는 펼쳐 확인하세요. 이미지 승인 ≠ 메타데이터 검수 ≠ 권리/공개 승인.</p></header><main>''' + ''.join(blocks) + '</main></html>'
    directory = root / BASE / "review-checkpoints" / key
    if apply:
        immutable(directory / "review.html", html_text.encode("utf-8"))
        immutable(directory / "receipt.json", encode({"schema_version": "luna-full-review-1", "checkpoint": key,
                    "html_sha256": digest(html_text.encode("utf-8")), "counts": {"approved": len(tasks), "visible_cards": len(blocks)},
                    "metadata_human_approved": False, "public_eligible": False}))
    return {"status": "prepared" if apply else "dry_run", "path": str(directory / "review.html"),
            "top_level_cards": len(blocks), "approved_images": len(tasks), "new_valid": progress["new_valid"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build(Path(__file__).resolve().parents[1], apply=args.apply), ensure_ascii=False))
