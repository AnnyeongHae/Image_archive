"""Offline-only, fail-closed text embedding planning; never executes embeddings."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from pathlib import Path

VERSION = "image-text-embedding-budget-1"
MODEL = "voyage-4-lite"
DIMENSION = 512
DOCUMENT_PREFIX = "Represent the document for retrieval: "
PREFIX_SOURCE = "https://docs.voyageai.com/docs/faq"


def encoded(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _integer(value, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(name + " must be a nonnegative integer (not bool)")
    return value


def guard_rerank_budget(query_tokens, document_tokens, max_docs=20, max_total=10000) -> int:
    """Optional future guard: rerank token formula is N * query + sum(documents)."""
    for value, name in ((query_tokens, "query_tokens"), (max_docs, "max_docs"), (max_total, "max_total")):
        _integer(value, name)
    if not isinstance(document_tokens, (list, tuple)):
        raise ValueError("document_tokens must be an explicit list or tuple")
    total = len(document_tokens) * query_tokens + sum(_integer(n, "document_tokens") for n in document_tokens)
    if len(document_tokens) > max_docs or total > max_total:
        raise ValueError("rerank budget exceeded")
    return total


def compact_projection(result: dict, qa_paths=(), usage_rows=(), memo="") -> tuple[str, list[str]]:
    """Explicit semantic whitelist; QA suppresses the complete indicated root."""
    roots = {re.split(r"[./\[]", p.lstrip("/$."))[0] for p in qa_paths}
    # An empty/root finding cannot safely be attributed to a narrower section.
    if "" in roots:
        roots.update(result)
    seen, lines = set(), []

    def add(label, value):
        if isinstance(value, list):
            for child in value:
                add(label, child)
        elif isinstance(value, str):
            normalized = " ".join(unicodedata.normalize("NFC", value).split())
            if normalized and normalized not in seen:
                seen.add(normalized)
                if lines and lines[-1][0] == label:
                    lines[-1][1].append(normalized)
                else:
                    lines.append((label, [normalized]))

    def fields(label, value, keys):
        if isinstance(value, dict):
            for key in keys:
                add(label, value.get(key))

    visual = result.get("visual", {}) if "visual" not in roots else {}
    fields("시각", visual, ("caption_ko", "description_ko", "subjects", "medium", "styles", "style"))
    background = visual.get("background")
    if isinstance(background, dict):
        fields("배경", background, ("setting", "detail_ko", "description_ko"))
    else:
        add("배경", background)
    fields("구도", visual, ("layout", "composition", "copy_space"))
    fields("편집 제약", visual.get("editability"), ("note_ko", "hard_constraints"))
    for root in ("prompt", "prompt_analysis", "prompt_intent"):
        value = result.get(root, {}) if root not in roots else {}
        fields("의도", value, ("purpose_ko", "intended_purpose_ko", "summary_ko"))
        fields("고정 조건", value, ("invariants", "fixed_rules", "requested_controls"))
        for slot in value.get("slots", value.get("replaceable_slots", [])):
            keys = ("name", "current", "change") if "name" in slot else ("slot_ko", "current_value_ko", "replacement_guidance_ko")
            add("교체 슬롯", " / ".join(slot.get(key, "") for key in keys))
    usage_root = "uses" if "uses" in result else "usage_selection"
    if usage_root not in roots:
        for row in usage_rows:
            use = row["value"]
            fields("용도", use, ("use_case_id",))
            add("용도", row["label_ko"])
            fields("활용 근거", use, ("fit", "reuse_mode", "why_ko", "why_usable_ko"))
            fields("변경", use, ("changes", "adaptation_ko"))
            fields("제약", use, ("constraints", "constraints_ko"))
    if "reuse_ideas" not in roots:
        for use in result.get("reuse_ideas", []):
            fields("활용 제안", use, ("use_case", "visual_reason", "adaptation", "caution"))
    add("사용자 메모", memo)
    return "\n".join(label + ": " + " · ".join(values) for label, values in lines), sorted(roots)


def _read_database(path: Path) -> list[dict]:
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError("A checkpointed immutable database without a live WAL/journal is required")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = [dict(row) for row in connection.execute("""SELECT i.item_id,i.style_id,
          p.original_text,r.candidate_id,r.effective_json,coalesce(n.memo,'') AS memo
          FROM source_items i LEFT JOIN prompts p ON p.sha256=i.prompt_sha256
          LEFT JOIN analysis_results r ON r.item_id=i.item_id
          LEFT JOIN human_notes n ON n.item_id=i.item_id
          WHERE i.approval_state='image_approved' ORDER BY i.item_id""")]
        ids = {row["item_id"] for row in rows}
        if not rows or len(ids) != len(rows):
            raise ValueError("Require nonempty approved items with at most one current candidate per item")
        mappings = {}
        for group, item, representative in connection.execute("""SELECT m.group_id,m.item_id,g.representative_item_id
          FROM group_memberships m JOIN approval_groups g USING(group_id)
          JOIN source_items i ON i.item_id=m.item_id WHERE i.approval_state='image_approved'"""):
            if item in mappings or representative not in ids:
                raise ValueError("Overlapping groups or unapproved representative require human resolution")
            mappings[item] = (group, representative)
        for group, representative in mappings.values():
            if mappings.get(representative) != (group, representative):
                raise ValueError("Representative must belong to its own group; never mix groups")
        for row in rows:
            candidate = row["candidate_id"]
            row["group_id"], row["representative_item_id"] = mappings.get(row["item_id"], (None, row["item_id"]))
            row["qa_paths"] = [r[0] for r in connection.execute(
                "SELECT field_path FROM candidate_qa WHERE candidate_id=? ORDER BY ordinal", (candidate,))]
            row["usage_rows"] = [{"value": json.loads(r[0]), "label_ko": r[1]} for r in connection.execute("""
              SELECT u.raw_json,t.label_ko FROM usage_assignments u JOIN taxonomy_terms t
              ON t.taxonomy_sha256=u.taxonomy_sha256 AND t.facet=u.facet AND t.term_id=u.use_case_id
              WHERE u.candidate_id=? ORDER BY u.ordinal""", (candidate,))]
        return rows
    finally:
        connection.close()


def _stats(values: list[int]) -> dict:
    values = sorted(values)
    return {"total": sum(values), "p50": values[max(0, math.ceil(len(values) * .50) - 1)] if values else 0,
            "p95": values[max(0, math.ceil(len(values) * .95) - 1)] if values else 0, "max": max(values, default=0)}


def build_plan(database: Path, tokenizer_path: Path, *, max_tokens=2048, document_prefix=DOCUMENT_PREFIX,
               tokenizer=None) -> dict:
    """A supplied tokenizer object is for offline fixtures only; CLI loads local JSON."""
    _integer(max_tokens, "max_tokens")
    if not max_tokens or not isinstance(document_prefix, str):
        raise ValueError("max_tokens must be positive; document_prefix must be text")
    database, tokenizer_path = Path(database).resolve(strict=True), Path(tokenizer_path).resolve(strict=True)
    database_sha, tokenizer_sha = file_sha256(database), file_sha256(tokenizer_path)
    if tokenizer is None:
        from tokenizers import Tokenizer
        tokenizer = Tokenizer.from_file(str(tokenizer_path))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    count = lambda value: len(tokenizer.encode(value, add_special_tokens=False).ids)
    documents = []
    for row in _read_database(database):
        result = json.loads(row["effective_json"]) if row["effective_json"] else {}
        compact, excluded = compact_projection(result, row["qa_paths"], row["usage_rows"], row["memo"])
        naive = "\n".join((row["original_text"] or "", row["effective_json"] or "", row["memo"]))
        measures = {kind: {"characters": len(value), "body_tokens": count(value),
                          "prefixed_tokens": count(document_prefix + value)} for kind, value in (("naive", naive), ("compact", compact))}
        over = measures["compact"]["body_tokens"] > max_tokens
        missing = not row["candidate_id"] or not compact
        identity = {"version": VERSION, "model": MODEL, "dimension": DIMENSION, "input_type": "document", "text": compact}
        documents.append({"item_id": row["item_id"], "style_id": row["style_id"], "candidate_id": row["candidate_id"],
            "group_id": row["group_id"], "representative_item_id": row["representative_item_id"],
            "is_representative": row["item_id"] == row["representative_item_id"], "compact_text": compact,
            "naive_baseline_sha256": sha256(naive.encode("utf-8")), "excluded_qa_roots": excluded,
            "measurements": measures, "needs_compaction": over, "budget_blocked": over or missing,
            "status": "missing_semantics" if missing else "needs_compaction" if over else "planned_needs_review",
            "input_sha256": sha256(compact.encode("utf-8")), "future_cache_key": sha256(encoded(identity)),
            "cache_identity": {key: value for key, value in identity.items() if key != "text"},
            "metadata_human_approved": False, "release_eligible": False})
    if database_sha != file_sha256(database) or tokenizer_sha != file_sha256(tokenizer_path):
        raise ValueError("Input artifact changed during offline planning")
    subsets = {"all_approved": documents, "representative_only_optional": [d for d in documents if d["is_representative"]]}
    summary = {"schema_version": VERSION, "model": MODEL, "dimension": DIMENSION, "input_type": "document",
        "database_sha256": database_sha, "tokenizer_sha256": tokenizer_sha,
        "planner_sha256": file_sha256(Path(__file__)), "approved_document_count": len(documents),
        "unique_compact_input_sha_count": len({d["input_sha256"] for d in documents}),
        "unique_nonempty_compact_input_sha_count": len({d["input_sha256"] for d in documents if d["compact_text"]}),
        "representative_document_count": len(subsets["representative_only_optional"]),
        "max_document_body_tokens": max_tokens, "needs_compaction_count": sum(d["needs_compaction"] for d in documents),
        "diagnostic_over_768_tokens_count": sum(d["measurements"]["compact"]["body_tokens"] > 768 for d in documents),
        "budget_blocked_count": sum(d["budget_blocked"] for d in documents),
        "naive_baseline": "original_prompt + newline + effective_json + newline + memo; hypothetical, not prior requests",
        "token_count_method": "local tokenizer encode(add_special_tokens=False), no_truncation, no_padding",
        "document_prefix_sensitivity": {"text": document_prefix, "prefix_only_tokens": count(document_prefix),
            "source": PREFIX_SOURCE if document_prefix == DOCUMENT_PREFIX else None,
            "provider_billing_verified": False},
        "recommendation": "Embed all approved documents after explicit authorization and budget/metadata gates; collapse retrieval results by group, retaining child variants",
        "cache_policy": "future identity only; no cache lookup or cache-hit claim",
        "percentile_method": "nearest-rank", "model_calls": 0, "network_calls": 0, "embedding_calls": 0, "rerank_calls": 0,
        "rerank_policy": {"enabled": False, "max_documents": 20, "max_total_tokens": 10000, "formula": "N * query_tokens + sum(document_tokens)"},
        "actual_billed_tokens": None, "actual_billed_cost": None, "metadata_human_approved": False, "release_eligible": False,
        "statistics": {name: {"count": len(docs), "raw_vector_bytes_float32_estimate": len(docs) * DIMENSION * 4,
            **{kind: {"body_overcap_count": sum(d["measurements"][kind]["body_tokens"] > max_tokens for d in docs),
                      **{metric: _stats([d["measurements"][kind][metric] for d in docs]) for metric in ("characters", "body_tokens", "prefixed_tokens")}} for kind in ("naive", "compact")}}
            for name, docs in subsets.items()}}
    return {"summary": summary, "documents": documents}


def write_plan(plan: dict, output_dir: Path, *, archive_root: Path, apply=False) -> dict:
    """Content-addressed private artifacts; exclusive creation, no replacement."""
    root, output_dir = Path(archive_root).resolve(), Path(output_dir).resolve()
    if not output_dir.is_relative_to(root / "data/private-research"):
        raise ValueError("Plan output must remain under archive data/private-research")
    doc_bytes = b"".join(encoded(document) for document in plan["documents"])
    summary = {**plan["summary"], "documents_sha256": sha256(doc_bytes)}
    summary_bytes = encoded(summary)
    key = sha256(summary_bytes)
    target = output_dir / key
    if target.is_symlink() or target.is_junction():
        raise ValueError("Symlink or junction plan target is forbidden")
    if not target.resolve().is_relative_to(root / "data/private-research"):
        raise ValueError("Content-addressed plan target escapes private-research")
    files = {"summary.json": summary_bytes, "documents.jsonl": doc_bytes}
    for name, value in files.items():
        path = target / name
        if path.is_symlink() or path.is_junction():
            raise ValueError("Symlink or junction plan file is forbidden")
        if path.exists() and path.read_bytes() != value:
            raise ValueError("Immutable plan artifact differs: " + name)
    if apply:
        target.mkdir(parents=True, exist_ok=True)
        for name, value in files.items():
            path = target / name
            if not path.exists():
                with path.open("xb") as handle:
                    handle.write(value)
    return {"status": "prepared" if apply else "dry_run", "plan_key": key,
            "path": target.relative_to(root).as_posix(), "files": {name: sha256(value) for name, value in files.items()}, **summary}
