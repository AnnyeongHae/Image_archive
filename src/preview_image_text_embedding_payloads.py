"""Show exact pending outbound text payloads offline, without calling providers."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path

from evaluate_image_text_embeddings import freeze_files
from image_rag_eval.embedding_budget import encoded


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", required=True, type=Path)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--plan-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    preparation = json.loads((args.inputs / "preparation.json").read_bytes())
    fixture_raw = args.fixture.read_bytes()
    if hashlib.sha256(fixture_raw).hexdigest() != preparation["evaluation_fixture_sha256"]:
        raise ValueError("Frozen fixture changed")
    fixture = json.loads(fixture_raw)
    plan_raw = (args.plan_dir / "summary.json").read_bytes()
    document_raw = (args.plan_dir / "documents.jsonl").read_bytes()
    if hashlib.sha256(plan_raw).hexdigest() != preparation["plan_summary_sha256"] or hashlib.sha256(document_raw).hexdigest() != json.loads(plan_raw)["documents_sha256"]:
        raise ValueError("Frozen plan changed")
    names = {d["item_id"]: d["style_id"] for d in map(json.loads, document_raw.splitlines())}
    inputs = {}
    hashes = {}
    for phase in ("canary", "full"):
        raw = (args.inputs / (phase + "-inputs.json")).read_bytes()
        inputs[phase] = json.loads(raw)
        hashes[phase] = hashlib.sha256(raw).hexdigest()
    if inputs["canary"] != fixture["embedding_manifest"]:
        raise ValueError("Canary payload changed")
    ready = {"compact:" + d["item_id"]: d["compact_text"] for d in map(json.loads, document_raw.splitlines()) if not d["budget_blocked"]}
    if {d["input_id"]: d["text"] for d in inputs["full"]["documents"]} != ready:
        raise ValueError("Full payload changed")
    consent = {"schema_version": "image-text-outbound-consent-1", "status": "awaiting_explicit_outbound_payload_approval",
               "destination": "https://api.voyageai.com/v1/embeddings", "model": "voyage-4-lite", "dimension": 512,
               "data": ["12 canary original prompts and analysis JSON with optional human memo",
                        "377 compact usage descriptions derived from approved image metadata, prompts and optional human memo",
                        "11 Korean test queries"], "image_binaries_sent": False, "rerank_calls": 0,
               "provider_calls": 0, "canary_inputs": 35, "full_ready_documents": 377,
               "unique_inputs_combined": 400, "combined_conservative_token_reservation": preparation["conservative_reservation_tokens"],
               "execution_token_cap": 260000, "canary_pass_required_for_full": True,
               "manifest_sha256": hashes, "metadata_human_approved": False, "release_eligible": False,
               "approval_review_result": "Command rejected before process creation; no alternative outbound execution attempted"}
    escape = lambda text: html.escape(str(text), quote=True)
    pieces = ['<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
              '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
              '<title>Voyage 전송 전 입력 확인</title><style>body{font:16px/1.6 system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#17212b;background:#f7f8fa}details{background:white;border:1px solid #cbd5e1;border-radius:8px;margin:.6rem 0;padding:1rem}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.6 system-ui}.notice{background:#fff3cd;padding:1rem;border-radius:8px}code{overflow-wrap:anywhere}</style>',
              '<h1>Voyage 전송 전 입력 확인</h1><p class="notice">전송 승인 대기 · 실제 API 호출 0회. 이 페이지는 로컬 검토용이며 데이터를 외부로 보내는 기능이 없습니다.</p>',
              '<p>대상: <code>https://api.voyageai.com/v1/embeddings</code> · voyage-4-lite · 512차원</p>',
              '<p>비공개 원문 프롬프트·분석 설명·사용자 메모와 한국어 질의를 외부 사업자 서버로 보내 처리합니다. 이미지 파일 자체는 보내지 않습니다. 공개 배포·Qdrant 적재·리랭킹은 포함하지 않습니다.</p>',
              '<p>1차: 12개 이미지의 축약/원문 구성 24개 + 질의 11개. 검증 통과 후 2차: 준비된 축약 문서 377개 중 이미 성공한 12개는 재사용합니다. 합계 400개 고유 입력, 보수적 예약 250,827토큰 / 상한 260,000.</p>']
    for phase, title in (("canary", "1차 비교 검증 입력"), ("full", "검증 통과 시 전체 입력")):
        pieces.append('<h2>' + title + '</h2><p>고정 입력 SHA-256: <code>' + hashes[phase] + '</code></p>')
        for doc in inputs[phase]["documents"]:
            label = names.get(doc.get("item_id"), doc["input_id"])
            kind = "원문 전체 비교용" if doc["input_id"].startswith("baseline:") else "질의" if doc["input_type"] == "query" else "활용 중심 축약"
            pieces.append('<details><summary>' + escape(label + ' · ' + kind) + '</summary><pre>' + escape(doc["text"]) + '</pre></details>')
    pieces.append('</html>')
    if args.apply:
        freeze_files(args.output_dir, {"consent.json": encoded(consent), "payload-review.html": ''.join(pieces).encode("utf-8")}, root)
    print(json.dumps({"status": consent["status"], "apply": args.apply, "provider_calls": 0,
                      "path": args.output_dir.as_posix(), "manifest_sha256": hashes}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
