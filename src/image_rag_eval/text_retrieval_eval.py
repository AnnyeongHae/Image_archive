"""Offline text-retrieval smoke fixtures, group ranking and escaped local reports.

No provider, credentials, network, database writes, Qdrant or LLM judge. The
source-derived anchors are deliberately not human relevance gold or release
approval. Image vectors are a different space and must never enter this module.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import math
import sqlite3
from pathlib import Path

from .embedding_budget import DOCUMENT_PREFIX, _read_database, compact_projection

SCHEMA = "image-text-retrieval-smoke-1"
MODEL = "voyage-4-lite"
DIMENSION = 512
QUERY_PREFIX = "Represent the query for retrieving supporting documents: "
LABEL_BASIS = "source_derived_smoke_not_human_gold"
MAX_DROP = 0.10
SELECTION = (
    "BST-003", "BST-004", "API-071", "API-087", "CASE-011", "CASE-017",
    "CASE-130", "CASE-387", "DAV490-301", "DAV490-320", "YOM-045", "WUY-134",
)
# Fixed before provider execution; anchors are examples, not exhaustive relevance.
QUERY_SPECS = (
    ("q01", "온실과 영화관 건물을 흰 여백의 독립 스티커로 만들 참고", ("BST-003", "BST-004")),
    ("q02", "청색 네온과 방사형 대칭으로 근미래 우주 세계관을 표현할 그림", ("API-071",)),
    ("q03", "아이디어에서 코드와 배포로 이어지는 개발 흐름을 손그림으로 보여줄 참고", ("API-087",)),
    ("q04", "지역 맛집과 방문 지점을 번호로 안내하는 수채화 여행 지도", ("CASE-011",)),
    ("q05", "제품 내부 부품과 구조를 분해도와 주석으로 설명할 포스터", ("CASE-017",)),
    ("q06", "캐릭터 브랜드 색상과 모티프를 굿즈와 SNS에 일관되게 적용할 보드", ("CASE-130",)),
    ("q07", "영화 소개와 재생 버튼이 먼저 보이는 어두운 스트리밍 서비스 첫 화면", ("CASE-387",)),
    ("q08", "음식 사진과 가격을 섹션별로 정리한 전통풍 식당 메뉴판", ("DAV490-301",)),
    ("q09", "어두운 보라색 배경에 꽃과 연기로 화장품을 강조할 제품 광고", ("DAV490-320",)),
    ("q10", "밝은 교실에서 세 학생의 감정과 관계를 보여줄 애니메이션 장면", ("YOM-045",)),
    ("q11", "전통 복식의 재료와 구조와 착용 순서를 설명할 인포그래픽", ("WUY-134",)),
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("Require an archive-relative evidence path")
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("Evidence path escapes archive or is not a file")
    return path


def _token_counts(text: str, input_type: str, tokenizer) -> dict:
    prefix = DOCUMENT_PREFIX if input_type == "document" else QUERY_PREFIX
    count = lambda value: len(tokenizer.encode(value, add_special_tokens=False).ids)
    prefixed = count(prefix + text)
    return {"body_tokens": count(text), "prefixed_tokens": prefixed,
            "reserved_tokens": math.ceil(prefixed * 1.02) + 8}


def _load_sources(root: Path, database: Path, plan_dir: Path) -> tuple[dict, list[dict], dict]:
    root, database, plan_dir = root.resolve(), database.resolve(), plan_dir.resolve()
    if not database.is_relative_to(root) or not plan_dir.is_relative_to(root):
        raise ValueError("Source artifacts must remain in the archive")
    summary_bytes = (plan_dir / "summary.json").read_bytes()
    summary = json.loads(summary_bytes)
    doc_bytes = (plan_dir / "documents.jsonl").read_bytes()
    if (_sha(summary_bytes) != plan_dir.name or _sha(doc_bytes) != summary["documents_sha256"]
            or _sha(database.read_bytes()) != summary["database_sha256"]
            or summary["model"] != MODEL or summary["dimension"] != DIMENSION):
        raise ValueError("Pinned plan or database identity mismatch")
    plan_docs = [json.loads(line) for line in doc_bytes.splitlines() if line]
    rows = {row["item_id"]: row for row in _read_database(database)}
    if len(plan_docs) != len(rows) or {d["item_id"] for d in plan_docs} != set(rows):
        raise ValueError("Approved plan IDs differ from database")
    db = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        for ident, raw in db.execute("SELECT item_id,raw_json FROM source_items WHERE approval_state='image_approved'"):
            record = json.loads(raw)
            rows[ident]["image_path"] = record["frozen_record"]["prepared_image_path"]
            _safe_path(root, rows[ident]["image_path"])
        commit = db.execute("SELECT source_commit_id FROM snapshot WHERE id=1").fetchone()[0]
    finally:
        db.close()
    for document in plan_docs:
        row = rows[document["item_id"]]
        if (document["group_id"] != row["group_id"]
                or document["representative_item_id"] != row["representative_item_id"]):
            raise ValueError("Current group mapping differs from pinned plan")
    return {**summary, "source_commit_id": commit, "plan_key": plan_dir.name}, plan_docs, rows


def _index_record(document: dict, rows: dict) -> dict:
    row = rows[document["item_id"]]
    representative = rows[document["representative_item_id"]]
    return {"item_id": row["item_id"], "style_id": row["style_id"],
            "group_id": row["group_id"] or row["item_id"], "source_group_id": row["group_id"],
            "representative_item_id": representative["item_id"],
            "representative_style_id": representative["style_id"],
            "image_path": row["image_path"], "representative_image_path": representative["image_path"],
            "budget_blocked": document["budget_blocked"], "approval_state": "image_approved",
            "release_eligible": False}


def load_index_documents(archive_root: Path, database: Path, plan_dir: Path) -> list[dict]:
    """Ready-only group metadata plus compact text; blocked documents never enter."""
    _, documents, rows = _load_sources(Path(archive_root), Path(database), Path(plan_dir))
    return [{**_index_record(d, rows), "compact_text": d["compact_text"],
             "original_prompt": rows[d["item_id"]]["original_text"] or "",
             "qa_count": len(rows[d["item_id"]]["qa_paths"]), "excluded_qa_roots": d["excluded_qa_roots"],
             "input_id": "compact:" + d["item_id"]} for d in documents
            if not d["budget_blocked"] and d["compact_text"].strip()]


def build_canary_input(archive_root: Path, database: Path, plan_dir: Path,
                      tokenizer_path: Path, *, tokenizer=None) -> dict:
    """Read-only deterministic preparation. Caller persists with its write gate."""
    root, database, plan_dir = Path(archive_root), Path(database), Path(plan_dir)
    summary, plan_docs, rows = _load_sources(root, database, plan_dir)
    tokenizer_path = Path(tokenizer_path)
    if _sha(tokenizer_path.read_bytes()) != summary["tokenizer_sha256"]:
        raise ValueError("Pinned tokenizer identity mismatch")
    if tokenizer is None:
        from tokenizers import Tokenizer
        tokenizer = Tokenizer.from_file(str(tokenizer_path))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    by_style = {d["style_id"]: d for d in plan_docs}
    documents, requests = [], []
    for style in SELECTION:
        d = by_style[style]
        row = rows[d["item_id"]]
        if d["budget_blocked"] or row["qa_paths"]:
            raise ValueError("Canary baseline requires ready documents with zero current QA findings")
        compact, excluded = compact_projection(json.loads(row["effective_json"]), (), row["usage_rows"], row["memo"])
        baseline = "\n".join((row["original_text"] or "", row["effective_json"] or "", row["memo"]))
        if (excluded or compact != d["compact_text"] or _sha(compact.encode()) != d["input_sha256"]
                or _sha(baseline.encode()) != d["naive_baseline_sha256"]):
            raise ValueError("Canary texts differ from pinned plan")
        document = {**_index_record(d, rows), "compact_text": compact, "baseline_text": baseline,
                    "original_prompt": row["original_text"] or "", "candidate_id": row["candidate_id"],
                    "qa_count": 0, "metadata_human_approved": False,
                    "compact_input_id": "compact:" + row["item_id"],
                    "baseline_input_id": "baseline:" + row["item_id"]}
        for lane, text in (("compact", compact), ("baseline", baseline)):
            document[lane + "_counts"] = _token_counts(text, "document", tokenizer)
            requests.append({"input_id": document[lane + "_input_id"], "item_id": row["item_id"],
                             "text": text, "input_type": "document"})
        documents.append(document)
    selected = {d["style_id"]: d for d in documents}
    queries = []
    for ident, text, styles in QUERY_SPECS:
        evidence = [selected[style] for style in styles]
        queries.append({"query_id": ident, "input_id": "query:" + ident, "text": text,
                        "relevant_group_ids": sorted({d["group_id"] for d in evidence}),
                        "evidence_item_ids": [d["item_id"] for d in evidence],
                        "evidence": [{"item_id": d["item_id"], "style_id": d["style_id"],
                                      "field": "compact_text", "excerpt": d["compact_text"].split("\n", 1)[0]}
                                     for d in evidence],
                        "label_basis": LABEL_BASIS, "human_judged": False,
                        "counts": _token_counts(text, "query", tokenizer)})
        requests.append({"input_id": "query:" + ident, "text": text, "input_type": "query"})
    blocked = [d["item_id"] for d in plan_docs if d["budget_blocked"]]
    full_reserve = sum(_token_counts(d["compact_text"], "document", tokenizer)["reserved_tokens"]
                       for d in plan_docs if not d["budget_blocked"])
    reserves = {lane: sum(d[lane + "_counts"]["reserved_tokens"] for d in documents)
                for lane in ("compact", "baseline")}
    query_reserve = sum(q["counts"]["reserved_tokens"] for q in queries)
    if reserves["baseline"] > 17000 or full_reserve + reserves["baseline"] + query_reserve > 260000:
        raise ValueError("Canary would exhaust the approved combined token cap")
    result = {"schema_version": SCHEMA, "model": MODEL, "dimension": DIMENSION,
              "label_basis": LABEL_BASIS, "human_judged": False, "release_eligible": False,
              "selection_frozen_before_execution": True,
              "scope_note": "12 hand-selected zero-QA documents; 11 source-derived purpose anchors. Not random, exhaustive relevance, human gold or a production quality benchmark.",
              "baseline_definition": "original_prompt + newline + effective_json + newline + memo; current QA count must be zero",
              "database_sha256": summary["database_sha256"], "source_commit_id": summary["source_commit_id"],
              "source_snapshot_key": database.parent.name, "compact_plan_sha256": summary["plan_key"],
              "plan_documents_sha256": summary["documents_sha256"], "tokenizer_sha256": summary["tokenizer_sha256"],
              "documents": documents, "queries": queries,
              "ready_item_ids": sorted(d["item_id"] for d in plan_docs if not d["budget_blocked"]),
              "blocked_item_ids": sorted(blocked),
              "gate_policy": {"top_k": 5, "max_mean_recall_drop": MAX_DROP, "max_mrr_drop": MAX_DROP,
                              "cache_replay_required": True, "release_approval_granted": False},
              "embedding_manifest": {"schema_version": "image-text-embedding-inputs-1", "model": MODEL,
                                     "dimension": DIMENSION, "total_token_cap": 260000, "documents": requests},
              "budget": {"compact_canary_reserved": reserves["compact"], "baseline_canary_reserved": reserves["baseline"],
                         "query_reserved": query_reserve, "canary_reserved": sum(reserves.values()) + query_reserve,
                         "full_ready_compact_reserved": full_reserve,
                         "combined_deduplicated_reserved": full_reserve + reserves["baseline"] + query_reserve,
                         "total_token_cap": 260000, "actual_billed_tokens": None,
                         "count_method": "pinned local tokenizer, no truncation, prefixed encode; reserve ceil(tokens*1.02)+8 per input"}}
    if _sha(database.read_bytes()) != summary["database_sha256"]:
        raise ValueError("Database changed while preparing canary")
    return result


def _unit(vector, dimension: int) -> tuple[float, ...]:
    if (type(dimension) is not int or dimension < 1 or not isinstance(vector, (list, tuple))
            or len(vector) != dimension or any(type(x) not in (int, float) or not math.isfinite(x) for x in vector)):
        raise ValueError("Wrong-dimensional, nonnumeric or nonfinite vector")
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("Vector must have finite nonzero norm")
    return tuple(x / norm for x in vector)


def validate_vectors(vectors: dict, expected_ids, *, dimension=DIMENSION) -> dict:
    expected = list(expected_ids)
    if not isinstance(vectors, dict) or len(expected) != len(set(expected)) or set(vectors) != set(expected):
        raise ValueError("Vector input IDs are missing, unexpected or duplicated")
    return {ident: _unit(vectors[ident], dimension) for ident in expected}


def cosine(left, right, *, dimension=DIMENSION) -> float:
    return max(-1.0, min(1.0, math.fsum(a * b for a, b in zip(_unit(left, dimension), _unit(right, dimension)))))


def rank_groups(documents: list[dict], vectors: dict, query_vector, *, top_k=5,
                dimension=DIMENSION, blocked_item_ids=()) -> list[dict]:
    """Max member score, one result per group, always the canonical representative.

    Vectors are keyed by item_id. Representative metadata may describe an item
    outside the candidate subset; it is never replaced by the matching child.
    """
    if type(top_k) is not int or top_k < 1:
        raise ValueError("top_k must be a positive integer")
    normalized = validate_vectors(vectors, [d["item_id"] for d in documents], dimension=dimension)
    query = _unit(query_vector, dimension)
    blocked, groups = set(blocked_item_ids), {}
    for document in documents:
        ident = document["item_id"]
        if ident in blocked or document.get("budget_blocked") or document.get("approval_state") != "image_approved":
            raise ValueError("Unapproved or blocked document entered the index")
        group = document.get("group_id") or ident
        rep = document.get("representative_item_id")
        if not isinstance(group, str) or not group or not isinstance(rep, str) or not rep:
            raise ValueError("Missing canonical group identity")
        if group in groups and groups[group]["representative_item_id"] != rep:
            raise ValueError("Group contains conflicting representatives")
        score = max(-1.0, min(1.0, math.fsum(a * b for a, b in zip(normalized[ident], query))))
        member = {"item_id": ident, "style_id": document["style_id"], "score": score,
                  "image_path": document.get("image_path")}
        entry = groups.setdefault(group, {"group_id": group, "representative_item_id": rep,
            "representative_style_id": document.get("representative_style_id", rep),
            "representative_image_path": document.get("representative_image_path"), "member_scores": []})
        entry["member_scores"].append(member)
    for entry in groups.values():
        entry["member_scores"].sort(key=lambda r: (-r["score"], r["item_id"]))
        best = entry["member_scores"][0]
        entry.update({"score": best["score"], "matched_item_id": best["item_id"],
                      "matched_style_id": best["style_id"], "matched_image_path": best["image_path"],
                      "matched_item_ids": [r["item_id"] for r in entry["member_scores"]]})
    return sorted(groups.values(), key=lambda r: (-r["score"], r["group_id"]))[:top_k]


def _metrics(rankings: list[dict], relevant: list[str]) -> dict:
    relevant_set = set(relevant)
    if not relevant_set or len(relevant_set) != len(relevant):
        raise ValueError("Require distinct nonempty source-derived anchor groups")
    ranks = [rank for rank, row in enumerate(rankings, 1) if row["group_id"] in relevant_set]
    return {"recall_at_5": len(ranks) / len(relevant_set),
            "reciprocal_rank_at_5": 1 / min(ranks) if ranks else 0.0}


def evaluate_canary(fixture: dict, vectors_by_input_id: dict, *, cache_audit=None) -> dict:
    """Evaluate frozen fixtures. Missing replay evidence blocks the technical gate."""
    if (fixture.get("schema_version") != SCHEMA or fixture.get("model") != MODEL
            or fixture.get("dimension") != DIMENSION or fixture.get("label_basis") != LABEL_BASIS
            or fixture.get("human_judged") is not False or fixture.get("release_eligible") is not False):
        raise ValueError("Unsupported or misleading evaluation fixture")
    documents, queries = fixture["documents"], fixture["queries"]
    if not documents or not queries or any(d.get("qa_count") != 0 for d in documents):
        raise ValueError("Nonempty zero-QA canary required")
    ids = [d[lane + "_input_id"] for d in documents for lane in ("compact", "baseline")]
    ids += [q["input_id"] for q in queries]
    validate_vectors(vectors_by_input_id, ids)
    if len({q["query_id"] for q in queries}) != len(queries):
        raise ValueError("Duplicate query ID")
    document_ids = {d["item_id"] for d in documents}
    group_ids = {d.get("group_id") or d["item_id"] for d in documents}
    group_by_item = {d["item_id"]: d.get("group_id") or d["item_id"] for d in documents}
    if "embedding_manifest" in fixture:
        expected_requests = [{"input_id": d[lane + "_input_id"], "item_id": d["item_id"],
                              "text": d[lane + "_text"], "input_type": "document"}
                             for d in documents for lane in ("compact", "baseline")]
        expected_requests.extend({"input_id": q["input_id"], "text": q["text"], "input_type": "query"} for q in queries)
        expected_manifest = {"schema_version": "image-text-embedding-inputs-1", "model": MODEL,
                             "dimension": DIMENSION, "total_token_cap": 260000, "documents": expected_requests}
        if fixture["embedding_manifest"] != expected_manifest:
            raise ValueError("Embedding manifest does not bind the frozen fixture texts")
    if not document_ids <= set(fixture["ready_item_ids"]) or document_ids & set(fixture["blocked_item_ids"]):
        raise ValueError("Canary contains unready or blocked items")
    lanes, self_checks = {}, {}
    for lane in ("compact", "baseline"):
        vectors = {d["item_id"]: vectors_by_input_id[d[lane + "_input_id"]] for d in documents}
        results = []
        for query in queries:
            if (query.get("label_basis") != LABEL_BASIS or query.get("human_judged") is not False
                    or not set(query["relevant_group_ids"]) <= group_ids
                    or not set(query["evidence_item_ids"]) <= document_ids
                    or {group_by_item.get(i) for i in query["evidence_item_ids"]} != set(query["relevant_group_ids"])):
                raise ValueError("Unbound relevance anchor")
            ranked = rank_groups(documents, vectors, vectors_by_input_id[query["input_id"]],
                                 blocked_item_ids=fixture["blocked_item_ids"])
            results.append({"query_id": query["query_id"], "text": query["text"],
                            "rankings": ranked, **_metrics(ranked, query["relevant_group_ids"])})
        passed = 0
        for document in documents:
            own = vectors[document["item_id"]]
            ranked = rank_groups(documents, vectors, own, top_k=len(group_ids),
                                 blocked_item_ids=fixture["blocked_item_ids"])
            own_group = next(r for r in ranked if r["group_id"] == document["group_id"])
            passed += int(abs(cosine(own, own) - 1) <= 1e-10 and own_group["score"] >= ranked[0]["score"] - 1e-10)
        self_checks[lane] = {"passed": passed, "total": len(documents), "tie_aware": True,
                             "purpose": "numeric and mapping consistency, not model quality"}
        lanes[lane] = {"mean_recall_at_5": math.fsum(r["recall_at_5"] for r in results) / len(results),
                       "mrr_at_5": math.fsum(r["reciprocal_rank_at_5"] for r in results) / len(results), "queries": results}
    compact, baseline = lanes["compact"], lanes["baseline"]
    replay = (isinstance(cache_audit, dict) and cache_audit.get("verified") is True
              and type(cache_audit.get("provider_calls")) is int and cache_audit["provider_calls"] == 0
              and isinstance(cache_audit.get("cache_hit_input_ids"), list)
              and sorted(cache_audit["cache_hit_input_ids"]) == sorted(ids))
    gates = {"exact_input_ids_and_512_finite_nonzero_dimensions": True, "zero_qa_baseline_only": True,
             "blocked_documents_excluded": True, "top5_distinct_canonical_groups": all(
                 len(r["rankings"]) == len({x["group_id"] for x in r["rankings"]})
                 for lane in lanes.values() for r in lane["queries"]),
             "self_retrieval_consistency": all(c["passed"] == c["total"] for c in self_checks.values()),
             "cache_replay_no_provider_calls": replay,
             "compact_recall_no_gross_loss": compact["mean_recall_at_5"] + MAX_DROP + 1e-12 >= baseline["mean_recall_at_5"],
             "compact_mrr_no_gross_loss": compact["mrr_at_5"] + MAX_DROP + 1e-12 >= baseline["mrr_at_5"]}
    return {"schema_version": SCHEMA, "status": "technical_smoke_passed" if all(gates.values()) else "blocked",
            "technical_gate_passed": all(gates.values()), "gates": gates, "lanes": lanes, "self_retrieval": self_checks,
            "label_basis": LABEL_BASIS, "human_judged": False, "production_accuracy_claim_allowed": False,
            "metadata_human_approved": False, "release_eligible": False, "quality_threshold_max_drop": MAX_DROP,
            "cache_audit": cache_audit, "document_count": len(documents), "group_count": len(group_ids),
            "query_count": len(queries), "model": MODEL, "dimension": DIMENSION}


def render_report(fixture: dict, evaluation: dict, archive_root: Path) -> str:
    """Return self-contained HTML; caller chooses a private append-only location."""
    escape = lambda value: html.escape(str(value), quote=True)
    root = Path(archive_root).resolve()
    script = """document.addEventListener('click', async function(event) {
  const button = event.target.closest('button[data-copy]');
  if (!button) return;
  const input = document.getElementById(button.dataset.copy);
  const status = document.getElementById('copy-status');
  if (!input) return;
  input.focus(); input.select();
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(input.value);
    } else if (!document.execCommand('copy')) { throw new Error('copy unavailable'); }
    status.textContent = '원문을 복사했습니다.';
  } catch (error) {
    status.textContent = '자동 복사가 차단되었습니다. 선택된 원문을 Ctrl+C로 복사하세요.';
  }
});"""
    script_hash = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")

    def picture(path, label):
        if not path:
            return ""
        uri = _safe_path(root, path).as_uri()
        return '<figure><img loading="lazy" src="' + escape(uri) + '" alt="' + escape(label) + '"><figcaption>' + escape(label) + '</figcaption></figure>'

    parts = ['<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src file: data:; style-src \'unsafe-inline\'; script-src \'sha256-' + script_hash + '\'; base-uri \'none\'; form-action \'none\'">',
        '<title>비공개 텍스트 검색 스모크 검증</title><style>body{font:16px/1.55 system-ui,sans-serif;margin:2rem auto;max-width:1200px;padding:0 1rem;background:#f7f8fa;color:#17212b}section,article{background:white;border:1px solid #ccd5dd;border-radius:8px;padding:1rem;margin:1rem 0}table{border-collapse:collapse}td,th{padding:.5rem;border:1px solid #ccd5dd;text-align:left}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:1rem}img{max-width:100%;height:180px;object-fit:contain}figure{margin:.3rem}textarea{width:98%;min-height:8rem}code{overflow-wrap:anywhere}.warn{background:#fff3cd;padding:1rem}h1,h2,h3{line-height:1.25}</style>',
        '<h1>비공개 텍스트 검색 스모크 검증</h1><p class="warn">출처 기반 예시 질의이며 사람 정답셋·정식 벤치마크가 아닙니다. 미판정 결과를 오답으로 단정하지 않습니다. 메타데이터·권리·공개 승인은 별도입니다.</p>',
        '<p>상태: <strong>' + escape(evaluation['status']) + '</strong> · 모델 ' + escape(evaluation['model']) + ' · 차원 ' + escape(evaluation['dimension']) + '</p>',
        '<table><tr><th>문서</th><th>그룹 Recall@5</th><th>MRR@5</th></tr>']
    for lane in ("compact", "baseline"):
        result = evaluation["lanes"][lane]
        parts.append('<tr><td>' + escape(lane) + '</td><td>' + format(result['mean_recall_at_5'], '.3f') + '</td><td>' + format(result['mrr_at_5'], '.3f') + '</td></tr>')
    parts.append('</table><h2>기술 게이트</h2><ul>')
    parts.extend('<li>' + escape(name) + ': ' + ('PASS' if passed else 'BLOCKED') + '</li>' for name, passed in evaluation['gates'].items())
    parts.append('</ul>')
    query_index = {q['query_id']: q for q in fixture['queries']}
    for ordinal, query in enumerate(evaluation['lanes']['compact']['queries']):
        anchor = query_index[query['query_id']]
        parts.append('<section><h2>' + escape(query['query_id'] + ' · ' + query['text']) + '</h2><p>출처 기반 기준 그룹: <code>' + escape(', '.join(anchor['relevant_group_ids'])) + '</code></p>')
        for lane in ('compact', 'baseline'):
            result = evaluation['lanes'][lane]['queries'][ordinal]
            parts.append('<h3>' + escape(lane) + '</h3><div class="grid">')
            for rank, row in enumerate(result['rankings'], 1):
                parts.append('<article><strong>' + str(rank) + '. 대표 ' + escape(row['representative_style_id']) + '</strong><p>최고 일치 항목 ' + escape(row['matched_style_id']) + ' · cosine ' + format(row['score'], '.4f') + '</p>')
                parts.append(picture(row.get('representative_image_path'), '대표 ' + row['representative_style_id']))
                if len(row['member_scores']) > 1 or row['matched_item_id'] != row['representative_item_id']:
                    parts.append('<details class="group-members"><summary>그룹 구성원 ' + str(len(row['member_scores'])) + '개 보기</summary>')
                    for member in row['member_scores']:
                        parts.append(picture(member.get('image_path'), '구성원 ' + member['style_id'] + ' · cosine ' + format(member['score'], '.4f')))
                    parts.append('</details>')
                parts.append('<small><code>' + escape(row['group_id']) + '</code></small></article>')
            parts.append('</div>')
        parts.append('</section>')
    parts.append('<h2>선정 문서와 원문</h2><p>원문 복사 버튼을 사용할 수 있습니다. 이미지 속 브랜드·가격·효능은 사실 증거가 아닙니다.</p><p id="copy-status" role="status" aria-live="polite"></p>')
    for index, document in enumerate(fixture['documents']):
        element_id = 'copy-prompt-' + str(index)
        parts.append('<details><summary>' + escape(document['style_id']) + ' · 대표 ' + escape(document['representative_style_id']) + '</summary><p>' + escape(document['compact_text']) + '</p><label for="' + element_id + '">보존 원문</label><textarea id="' + element_id + '" readonly>' + escape(document.get('original_prompt', '')) + '</textarea><button type="button" data-copy="' + element_id + '">원문 복사</button></details>')
    return ''.join(parts) + '<script>' + script + '</script></html>'


def render_full_report(full_evaluation: dict, documents: list[dict], archive_root: Path) -> str:
    """Render only precomputed full-corpus results; no search/provider invocation.

    The caller supplies source-bound documents from load_index_documents and
    an evaluation from verified cached vectors. Receipt usage is cumulative for
    the shared execution directory, not attributable to just this HTML/corpus.
    """
    result = full_evaluation
    if (result.get("schema_version") != "image-full-text-search-smoke-1"
            or result.get("metadata_human_approved") is not False or result.get("release_eligible") is not False
            or result.get("canary_technical_gate_passed") is not True
            or result.get("image_embedding_calls") != 0 or result.get("rerank_calls") != 0):
        raise ValueError("Unsupported or approval-ambiguous full evaluation")
    if not documents or len({d["item_id"] for d in documents}) != len(documents):
        raise ValueError("Full report needs distinct nonempty source documents")
    catalog = {d["item_id"]: d for d in documents}
    groups = {}
    for document in documents:
        if document.get("budget_blocked") or document.get("approval_state") != "image_approved":
            raise ValueError("Blocked or unapproved document in full report")
        ident = document["item_id"]
        group = document.get("group_id") or ident
        representative = document["representative_item_id"]
        entry = groups.setdefault(group, {"representative": representative, "members": set()})
        if entry["representative"] != representative or representative not in catalog:
            raise ValueError("Missing or conflicting canonical representative")
        entry["members"].add(ident)
    if (result.get("document_count") != len(documents) or result.get("group_count") != len(groups)
            or any(entry["representative"] not in entry["members"] for entry in groups.values())):
        raise ValueError("Full document/group count or representative membership mismatch")
    usage = result.get("usage", {})
    usage_fields = ("actual_reported_tokens", "conservative_charged_tokens", "total_token_cap",
                    "pending_or_uncertain_requests")
    if (any(type(usage.get(k)) is not int or usage[k] < 0 for k in usage_fields)
            or usage["total_token_cap"] <= 0 or usage["pending_or_uncertain_requests"] != 0
            or not usage["actual_reported_tokens"] <= usage["conservative_charged_tokens"] <= usage["total_token_cap"]):
        raise ValueError("Full receipt has missing, uncertain or over-budget usage")
    queries = result.get("queries")
    if (not isinstance(queries, list) or not queries
            or len({q["query_id"] for q in queries}) != len(queries)):
        raise ValueError("Full report requires distinct precomputed queries")
    for query in queries:
        rankings = query.get("rankings", [])
        if (not isinstance(query.get("text"), str) or not query["text"].strip()
                or not rankings or len(rankings) > 5
                or len({r["group_id"] for r in rankings}) != len(rankings)):
            raise ValueError("Invalid top-five distinct group results")
        if not set(query.get("source_anchor_groups", [])) <= set(groups):
            raise ValueError("Unknown source anchor group")
        for row in rankings:
            group = groups.get(row["group_id"])
            if (group is None or row["representative_item_id"] != group["representative"]
                    or row["matched_item_id"] not in group["members"]):
                raise ValueError("Result does not preserve the canonical group")
            members = row.get("member_scores", [])
            member_ids = [m["item_id"] for m in members]
            if len(member_ids) != len(set(member_ids)) or set(member_ids) != group["members"]:
                raise ValueError("Full result must retain every group member exactly once")
            scores = [row.get("score"), *(m.get("score") for m in members)]
            if any(type(score) not in (int, float) or not math.isfinite(score) or not -1 <= score <= 1 for score in scores):
                raise ValueError("Invalid cosine score in full result")
            matched = next(m for m in members if m["item_id"] == row["matched_item_id"])
            if abs(row["score"] - max(m["score"] for m in members)) > 1e-10 or abs(matched["score"] - row["score"]) > 1e-10:
                raise ValueError("Full group score is not its best member score")

    escape = lambda value: html.escape(str(value), quote=True)
    root = Path(archive_root).resolve()
    script = """const selector = document.getElementById('saved-query');
function showSavedQuery() {
  document.querySelectorAll('[data-query-panel]').forEach(function(panel) {
    panel.hidden = panel.dataset.queryPanel !== selector.value;
  });
  document.getElementById('query-status').textContent =
    '저장된 결과 표시: ' + selector.selectedOptions[0].textContent;
}
selector.addEventListener('change', showSavedQuery);
showSavedQuery();
document.addEventListener('click', async function(event) {
  const button = event.target.closest('button[data-copy]');
  if (!button) return;
  const input = document.getElementById(button.dataset.copy);
  const status = document.getElementById('copy-status');
  if (!input) return;
  input.focus(); input.select();
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(input.value);
    } else if (!document.execCommand('copy')) { throw new Error('copy unavailable'); }
    status.textContent = '보존 원문을 복사했습니다.';
  } catch (error) {
    status.textContent = '자동 복사가 차단되었습니다. 선택된 원문을 Ctrl+C로 복사하세요.';
  }
});"""
    script_hash = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
    prompt_counter = 0

    def picture(document, label):
        path = document.get("image_path")
        if not path:
            return '<p>로컬 이미지 경로가 제공되지 않았습니다.</p>'
        uri = _safe_path(root, path).as_uri()
        return '<figure><img loading="lazy" src="' + escape(uri) + '" alt="' + escape(label) + '"><figcaption>' + escape(label) + '</figcaption></figure>'

    def prompt_controls(document):
        nonlocal prompt_counter
        prompt_counter += 1
        ident = 'full-prompt-' + str(prompt_counter)
        prompt = document.get("original_prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return '<p class="muted">이 항목의 보존 원문이 제공되지 않아 복사할 수 없습니다.</p>'
        return ('<details class="prompt-details"><summary>보존 원문 보기 · 복사</summary><label for="' + ident + '">'
                + escape(document['style_id']) + ' 원문</label><textarea readonly id="' + ident + '">'
                + escape(prompt) + '</textarea><button type="button" data-copy="' + ident + '">원문 복사</button></details>')

    parts = ['<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src file: data:; style-src \'unsafe-inline\'; script-src \'sha256-' + script_hash + '\'; base-uri \'none\'; form-action \'none\'">',
        '<title>비공개 전체 텍스트 검색 결과</title><style>body{font:16px/1.55 system-ui,sans-serif;max-width:1320px;margin:1.25rem auto;padding:0 1rem;background:#f7f8fa;color:#17212b}h1,h2,h3{line-height:1.3}h1{font-size:clamp(1.35rem,2.2vw,1.9rem);margin:0 0 .7rem}h2{font-size:clamp(1.1rem,1.8vw,1.35rem);margin:.2rem 0 .7rem}h3{margin:.2rem 0 .5rem}section,article{background:white;border:1px solid #ccd5dd;border-radius:10px;padding:1rem;margin:1rem 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:1rem}.grid article{margin:0;min-width:0}.result-card>p{margin:.5rem 0}figure{margin:.5rem 0}img{display:block;width:100%;height:230px;object-fit:contain}figcaption,.muted{font-size:.9rem;color:#475569}select,button{font:inherit;padding:.6rem;max-width:100%}select{width:100%;margin:.5rem 0}button{cursor:pointer}details{margin:.75rem 0;border-top:1px solid #dbe2e8;padding-top:.65rem}summary{cursor:pointer;font-weight:600}textarea{box-sizing:border-box;width:100%;min-height:9rem;margin:.5rem 0;font:14px/1.5 monospace}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccd5dd;padding:.5rem;text-align:left}code{overflow-wrap:anywhere;font-size:.8rem}.summary{display:flex;gap:.5rem;flex-wrap:wrap}.pill{background:#e6edf5;padding:.3rem .6rem;border-radius:6px;font-size:.9rem}.brief{font-size:.9rem;color:#475569;margin:.65rem 0}.member{border-left:3px solid #dbe2e8;padding-left:.75rem;margin:1rem 0}[hidden]{display:none!important}.semantic-text{white-space:pre-wrap}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}#copy-status:empty{display:none}button:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid #2563eb;outline-offset:3px}</style></head><body>',
        '<h1>비공개 전체 텍스트 검색 결과</h1>',
        '<div class="summary"><span class="pill">문서 ' + format(len(documents), ',') + '</span><span class="pill">대표 그룹 ' + format(len(groups), ',') + '</span><span class="pill">저장된 질의 ' + str(len(queries)) + '</span><span class="pill">' + MODEL + ' · ' + str(DIMENSION) + '차원</span></div>',
        '<p class="brief">저장된 ' + str(len(queries)) + '개 질의의 결과 · 실시간 검색 아님 · 정확도는 사람 검토 필요</p>',
        '<label for="saved-query"><strong>저장된 질의 선택 — 실시간 검색 아님</strong></label><select id="saved-query" aria-controls="saved-results">']
    for index, query in enumerate(queries):
        parts.append('<option value="' + str(index) + '">' + escape(query['query_id'] + ' · ' + query['text']) + '</option>')
    parts.append('</select><p id="query-status" class="sr-only" role="status" aria-live="polite"></p><p id="copy-status" role="status" aria-live="polite"></p><noscript><p>스크립트가 꺼져 있어 모든 저장 결과를 표시합니다. 복사할 원문을 직접 선택하고 Ctrl+C를 누르세요.</p></noscript><main id="saved-results">')
    for index, query in enumerate(queries):
        parts.append('<section data-query-panel="' + str(index) + '" aria-labelledby="query-title-' + str(index) + '"><h2 id="query-title-' + str(index) + '">' + escape(query['query_id'] + ' · ' + query['text']) + '</h2>')
        anchor_labels = [catalog[groups[g]['representative']]['style_id'] for g in query['source_anchor_groups']]
        parts.append('<p class="muted">출처 기반 예시 대표: ' + escape(', '.join(anchor_labels)) + ' — 사람 확정 정답이 아닙니다.</p><div class="grid">')
        for rank, row in enumerate(query['rankings'], 1):
            representative = catalog[row['representative_item_id']]
            matched = catalog[row['matched_item_id']]
            parts.append('<article class="result-card" data-representative-id="' + escape(representative['item_id']) + '"><h3>' + str(rank) + '. 대표 ' + escape(representative['style_id']) + '</h3><p>최고 일치 구성원 ' + escape(matched['style_id']) + ' · cosine ' + format(row['score'], '.4f') + '</p>')
            parts.append(picture(representative, '정본 대표 ' + representative['style_id']))
            parts.append(prompt_controls(representative))
            members = row['member_scores']
            if len(members) > 1:
                parts.append('<details class="group-members"><summary>그룹 구성원 ' + str(len(members)) + '개 보기</summary>')
                for member in members:
                    document = catalog[member['item_id']]
                    suffix = ' · 정본 대표' if document['item_id'] == representative['item_id'] else ''
                    parts.append('<div class="member"><strong>' + escape(document['style_id'] + suffix) + '</strong><p>cosine ' + format(member['score'], '.4f') + '</p>')
                    parts.append(picture(document, '구성원 ' + document['style_id']))
                    parts.append(prompt_controls(document))
                    parts.append('</div>')
                parts.append('</details>')
            parts.append('<details><summary>검색에 사용한 요약과 ID</summary><p class="semantic-text">' + escape(matched.get('compact_text', '')) + '</p><p>그룹 <code>' + escape(row['group_id']) + '</code></p><p>일치 항목 <code>' + escape(matched['item_id']) + '</code></p></details></article>')
        parts.append('</div></section>')
    parts.extend(['</main><details class="run-details"><summary>실행·사용량·검증 안내</summary>',
        '<p>사전 계산된 질의의 결과만 전환합니다. 실시간 검색·새 임베딩·API 호출은 없습니다. 출처 기반 예시 질의이며 사람 정답셋이나 정식 정확도 벤치마크가 아닙니다.</p>',
        '<p>텍스트 보류 2개: CASE-176, CASE-530. 기존 이미지 캐시는 변경하지 않았으며 이번 실행의 이미지 재임베딩은 0회입니다. Rerank 0회. 메타데이터·권리·공개 승인은 부여하지 않습니다.</p>',
        '<section aria-labelledby="usage-title"><h2 id="usage-title">실측 사용량과 예약 한도</h2><p>아래 수치는 동일 실행 디렉터리의 누적 합계입니다. Canary 비교 문서·질의와 전체 compact 요청을 포함하며, 전체 377개 문서만의 사용량으로 해석하지 않습니다.</p>',
        '<table><tr><th>구분</th><th>토큰</th></tr><tr><td>API 응답에서 관측한 누적 사용량</td><td>' + format(usage['actual_reported_tokens'], ',') + '</td></tr>',
        '<tr><td>예산 원장에서 보수적으로 차감한 토큰</td><td>' + format(usage['conservative_charged_tokens'], ',') + '</td></tr>',
        '<tr><td>승인된 누적 토큰 상한</td><td>' + format(usage['total_token_cap'], ',') + '</td></tr>',
        '<tr><td>보수적 예약 기준 남은 토큰</td><td>' + format(usage['total_token_cap'] - usage['conservative_charged_tokens'], ',') + '</td></tr></table>',
        '<p class="muted">예약 차감은 실제 청구액이 아닙니다. 실제 청구 금액·무료 잔액은 이 결과로 확인되지 않습니다. 미완료·불확실 요청: 0개.</p></section>',
        '<p>12개 문서의 canary compact/baseline 비교와 이 전체 문서 검색은 후보 집합이 다릅니다. 두 결과의 순위·점수를 직접 성능 비교하지 않습니다. 아래 기준 그룹은 출처에서 고른 예시이며, 다른 결과는 사람 관련성 판정을 받지 않았습니다.</p>',
        '<details><summary>검증 아티팩트 식별자</summary>'])
    for field in ('full_manifest_sha256', 'query_manifest_sha256', 'fixture_sha256'):
        parts.append('<p>' + escape(field) + ': <code>' + escape(result.get(field, '미제공')) + '</code></p>')
    return ''.join(parts) + '</details></details><script>' + script + '</script></body></html>'
