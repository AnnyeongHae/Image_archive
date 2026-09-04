"""Build a read-only computer comparison after the bounded embedding batch."""
from __future__ import annotations

import argparse
import json
from contextlib import nullcontext
from pathlib import Path

from image_rag_eval.experiment import digest, json_bytes, read_json, run_lock, run_path, write_json
from image_rag_eval.incremental_embedding import SCHEMA as EMBEDDING_SCHEMA, _load, _state
from image_rag_eval.incremental_review import compare_incremental_vectors, render_incremental_comparison
from image_rag_eval.voyage_provider import VOYAGE_MODEL

ROOT = Path(__file__).resolve().parents[1]


def build_review(root: Path, run_id: str, *, apply: bool = False) -> dict:
    root = Path(root).resolve()
    source = run_path(root, run_id)
    # Provider execution uses the same private-run lock. Validate and emit under
    # that lock in apply mode; dry-run remains strictly non-mutating.
    with run_lock(source) if apply else nullcontext():
        return _build_review_snapshot(root, run_id, apply=apply)


def _build_review_snapshot(root: Path, run_id: str, *, apply: bool) -> dict:
    data = _load(root, run_id)
    ledger, cache, _batch_receipts = _state(data)
    manifest, bindings, source = data["manifest"], data["bindings"], data["source"]
    reference = run_path(root, manifest["reference_run_id"])
    receipt = read_json(source / "embedding-v1/execution-receipt.json")
    expected_receipt = {
        "schema_version": EMBEDDING_SCHEMA, "run_id": run_id, "provider": "voyage", "model": VOYAGE_MODEL,
        "manifest_sha256": data["manifest_sha256"], "source_bindings_sha256": data["source_bindings_sha256"],
        "status": "completed", "completed_image_ids": len(data["chosen"]), "target_image_ids": len(data["chosen"]),
    }
    if (not isinstance(receipt, dict) or any(receipt.get(key) != value for key, value in expected_receipt.items())
            or type(receipt.get("completed_image_ids")) is not int or type(receipt.get("target_image_ids")) is not int
            or set(cache) != set(data["unique"])):
        raise ValueError("complete bound new-only embedding execution required")
    incoming = read_json(source / "embedding-v1/vectors.json")["voyage_image"]
    if not isinstance(incoming, dict) or set(incoming) != set(data["chosen"]):
        raise ValueError("aggregate vectors must exactly match the deduplicated new ids")
    items = {item["id"]: {**item, "review_image_path": item["prepared_path"]} for item in manifest["items"]}
    for ident, values in incoming.items():
        # _state already enforces source-bound reused caches or complete
        # current batch checkpoints plus the latest authorized reservation.
        key = data["requests"][ident]["key"]
        if cache[key]["vector"] != values or cache[key]["vector_sha256"] != digest(json_bytes(values)):
            raise ValueError("aggregate vector does not match its content-bound receipt")
    existing = read_json(reference / "comparison-v1/vectors.json")["voyage_image"]
    reference_manifest = read_json(reference / "manifest.json")
    spec = read_json(reference / "group-workflow-v1/image-group-workflow.spec.json")
    for item in reference_manifest["items"]:
        items[item["id"]] = {**item, "review_image_path": f'../{reference.name}/{item["prepared_path"]}'}
    result = compare_incremental_vectors(existing, incoming, active_existing_ids=spec["stage1"]["active_ids"])
    result.update({"run_id": run_id, "reference_run_id": reference.name,
                   "manifest_sha256": digest(json_bytes(manifest)),
                   "source_bindings_sha256": digest(json_bytes(bindings)),
                   "existing_vectors_sha256": digest(json_bytes(existing)),
                   "new_vectors_sha256": digest(json_bytes(incoming)),
                   "execution_receipt_sha256": digest(json_bytes(receipt)),
                   "embedding_ledger_sha256": digest(json_bytes(ledger)),
                   "validated_cache_receipts_sha256": digest(json_bytes({key: cache[key] for key in sorted(cache)}))})
    rendered = render_incremental_comparison(result, items)
    json_path, html_path = source / "incremental-comparison.json", source / "incremental-comparison.html"
    if json_path.exists() and read_json(json_path) != result:
        raise ValueError("comparison already exists with different evidence")
    if html_path.exists() and html_path.read_text(encoding="utf-8") != rendered:
        raise ValueError("comparison already exists with different display")
    if apply:
        write_json(json_path, result)
        html_path.write_text(rendered, encoding="utf-8")
    return {"status": "computer_candidates_ready" if apply else "dry_run", "writes": 2 if apply else 0,
            "new_vectors": len(incoming), "retained_reference_count": result["retained_reference_count"],
            "old_new_comparisons": result["old_new_comparisons"], "new_new_comparisons": result["new_new_comparisons"],
            "old_match_priority_candidates": sum(row["priority_review"] for row in result["old_matches"]),
            "old_match_identity_candidates": sum(row["identity_review_candidate"] for row in result["old_matches"]),
            "new_new_candidate_pairs": len(result["new_new_candidate_pairs"]),
            "provider_calls": 0, "human_approved": False, "html_path": str(html_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build_review(ROOT, args.run_id, apply=args.apply), ensure_ascii=False))


if __name__ == "__main__":
    main()
