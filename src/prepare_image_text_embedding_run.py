"""Freeze a private canary/full text-embedding input set; never call a provider."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

from image_rag_eval.embedding_budget import DIMENSION, MODEL, encoded, file_sha256

CAP = 260000
PREFIXES = {"document": "Represent the document for retrieval: ",
            "query": "Represent the query for retrieving supporting documents: "}


def prepare(plan_dir: Path, database: Path, fixture: Path, tokenizer_path: Path, *, tokenizer=None):
    plan_dir, database = plan_dir.resolve(strict=True), database.resolve(strict=True)
    summary_path, documents_path = plan_dir / "summary.json", plan_dir / "documents.jsonl"
    summary_raw, document_raw = summary_path.read_bytes(), documents_path.read_bytes()
    summary = json.loads(summary_raw)
    if hashlib.sha256(summary_raw).hexdigest() != plan_dir.name:
        raise ValueError("Plan directory does not match its frozen summary hash")
    if hashlib.sha256(document_raw).hexdigest() != summary["documents_sha256"]:
        raise ValueError("Plan document hash mismatch")
    if file_sha256(database) != summary["database_sha256"]:
        raise ValueError("Source database no longer matches the plan")
    if file_sha256(tokenizer_path) != summary["tokenizer_sha256"]:
        raise ValueError("Tokenizer no longer matches the plan")
    documents = [json.loads(line) for line in document_raw.splitlines()]
    ready = {"compact:" + d["item_id"]: d for d in documents if not d["budget_blocked"]}
    if len(documents) != summary["approved_document_count"] or len(ready) != len(documents) - summary["budget_blocked_count"]:
        raise ValueError("Approved/ready document count drift")
    full = {"schema_version": "image-text-embedding-inputs-1", "model": MODEL,
            "dimension": DIMENSION, "total_token_cap": CAP,
            "documents": [{"input_id": key, "item_id": d["item_id"], "text": d["compact_text"],
                           "input_type": "document"} for key, d in ready.items()]}
    fixture_raw = fixture.read_bytes()
    evaluation = json.loads(fixture_raw)
    canary = evaluation["embedding_manifest"]
    for key in ("schema_version", "model", "dimension", "total_token_cap"):
        if canary.get(key) != full[key]:
            raise ValueError("Canary execution configuration differs: " + key)
    ids = set()
    for d in canary["documents"]:
        if d["input_id"] in ids or not isinstance(d.get("text"), str) or not d["text"].strip():
            raise ValueError("Duplicate or empty canary input")
        ids.add(d["input_id"])
        if d["input_type"] == "query":
            if not d["input_id"].startswith("query:") or len(d["text"]) > 2000:
                raise ValueError("Canary query identity or size invalid")
            continue
        if d["input_type"] != "document" or not isinstance(d.get("item_id"), str):
            raise ValueError("Canary document identity invalid")
        planned = ready.get("compact:" + d["item_id"])
        if planned is None:
            raise ValueError("Canary includes blocked or unapproved item")
        if d["input_id"] == "compact:" + d["item_id"]:
            if d["text"] != planned["compact_text"]:
                raise ValueError("Canary compact text differs from frozen plan")
        elif d["input_id"] == "baseline:" + d["item_id"]:
            if planned["excluded_qa_roots"] or hashlib.sha256(d["text"].encode("utf-8")).hexdigest() != planned["naive_baseline_sha256"]:
                raise ValueError("Baseline must be QA-clean and match the frozen original")
        else:
            raise ValueError("Unknown canary document purpose")
    if tokenizer is None:
        from tokenizers import Tokenizer
        tokenizer = Tokenizer.from_file(str(tokenizer_path))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    unique = {(d["input_type"], d["text"]) for d in full["documents"] + canary["documents"]}
    estimates = [len(tokenizer.encode(PREFIXES[role] + text, add_special_tokens=False).ids) for role, text in unique]
    reserve = sum(math.ceil(n * 1.02) + 8 for n in estimates)
    if reserve > CAP:
        raise ValueError("Combined canary/full reservation exceeds approved cap")
    control = {"schema_version": "image-text-run-preparation-1", "database_sha256": summary["database_sha256"],
               "plan_summary_sha256": plan_dir.name, "tokenizer_sha256": summary["tokenizer_sha256"],
               "evaluation_fixture_sha256": hashlib.sha256(fixture_raw).hexdigest(),
               "approved_count": len(documents), "ready_count": len(ready),
               "blocked_style_ids": [d["style_id"] for d in documents if d["budget_blocked"]],
               "canary_inputs": len(canary["documents"]), "unique_combined_inputs": len(unique),
               "calculated_prefixed_tokens": sum(estimates), "conservative_reservation_tokens": reserve,
               "total_token_cap": CAP, "image_embedding_calls": 0, "rerank_enabled": False,
               "metadata_human_approved": False, "release_eligible": False,
               "canary_gate_required_before_full": True, "source_derived_smoke_not_human_accuracy": True}
    return {"canary-inputs.json": canary, "full-inputs.json": full, "preparation.json": control}


def save(artifacts, output_dir: Path, root: Path, *, apply=False):
    private = root.resolve() / "data/private-research"
    output_dir = output_dir.resolve()
    if not output_dir.is_relative_to(private):
        raise ValueError("Output must remain private")
    files = {name: encoded(value) for name, value in artifacts.items()}
    for name, raw in files.items():
        path = output_dir / name
        if path.is_symlink() or path.is_junction() or (path.exists() and path.read_bytes() != raw):
            raise ValueError("Refusing non-immutable output: " + name)
    if apply:
        output_dir.mkdir(parents=True, exist_ok=True)
        for name, raw in files.items():
            path = output_dir / name
            if not path.exists():
                with path.open("xb") as handle:
                    handle.write(raw)
    return {"status": "prepared" if apply else "dry_run", "path": output_dir.relative_to(root.resolve()).as_posix(),
            "hashes": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
            **artifacts["preparation.json"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("plan-dir", "database", "fixture", "tokenizer", "output-dir"):
        parser.add_argument("--" + flag, required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        artifacts = prepare(args.plan_dir, args.database, args.fixture, args.tokenizer)
        result = save(artifacts, args.output_dir, Path(__file__).resolve().parents[1], apply=args.apply)
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, "blocked: " + str(exc) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
