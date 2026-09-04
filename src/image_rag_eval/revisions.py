"""Append-only, offline Voyage view revision; never instantiate an API client."""
from __future__ import annotations

from pathlib import Path

from .carryover import validate_parent_checkpoint
from .comparison import (ARMS, MODELS, add_order_evidence, evaluate_comparison,
                         load_inputs, requests_for)
from .experiment import digest, json_bytes, now, read_json, run_lock, run_path, unit_prefix, write_json
from .retention import build_retention
from .results_view import render_results
from .similarity import cosine


REVISION_DIRECTORY = "comparison-v2"
RESULTS_FILE = "comparison-results-v2.html"


def revise_voyage_view(root: Path, source_run_id: str, *, apply=False, maximum_items=50) -> dict:
    manifest, _, pixels = load_inputs(root, source_run_id, maximum_items)
    source = run_path(root, source_run_id)
    origin = source / "comparison-v1"
    target = source / REVISION_DIRECTORY
    results_path = source / RESULTS_FILE
    with run_lock(source.parent), run_lock(source):
        if apply and (target.exists() or results_path.exists()):
            raise FileExistsError("revision already exists; never overwrite historical view evidence")
        # Validate provenance inputs before creating any revision artifacts.
        source_hashes = {
            "source_manifest_sha256": digest((source / "manifest.json").read_bytes()),
            "source_budget_sha256": digest((origin / "budget.json").read_bytes()),
            "source_retention_sha256": digest((origin / "retention.json").read_bytes()),
        }
        ledger = read_json(origin / "budget.json")
        if len(manifest["items"]) > 20:
            validate_parent_checkpoint(root, source_run_id, ledger)
        queries = read_json(origin / "queries.json")
        requests = requests_for(manifest, pixels, queries, arms_subset=["voyage_image"])
        vectors = {key: {} for key in [*ARMS, "gemini_queries", "voyage_queries"]}
        receipt_hashes = {}
        completed_keys = {a["key"].split(":", 1)[0] for a in ledger["attempts"] if a["status"] == "completed"}
        for request in requests:
            path = origin / "vector-cache" / (request["key"] + ".json")
            value = read_json(path)
            if (value.get("key") != request["key"] or value.get("provider") != "voyage"
                    or value.get("model") != request["model"]
                    or len(value.get("vector", [])) != request["dimensions"]
                    or value.get("vector_sha256") != digest(json_bytes(value["vector"]))
                    or request["key"] not in completed_keys):
                raise ValueError("Voyage cache receipt lacks valid identity or completed attempt")
            key = "voyage_image" if request["kind"] == "document" else "voyage_queries"
            vectors[key][request["id"]] = unit_prefix(value["vector"], request["dimensions"])
            receipt_hashes[request["key"]] = digest(path.read_bytes())
        manifest = add_order_evidence(root, manifest)
        manifest["title"] = "Voyage 이미지 검색 · 중복 정책 v2"
        manifest["selection_profile"] = {"provider": "voyage", "model": MODELS["voyage"],
            "evaluation_arms": ["voyage_image"], "gemini": "paused_by_user"}
        retention = build_retention(manifest["items"])
        for group in retention.get("prompt_variant_groups", []):
            ids = group["member_ids"]
            group.setdefault("evidence", {})["voyage_image_cosines"] = [
                {"left_id": left, "right_id": right,
                 "cosine": round(cosine(vectors["voyage_image"][left], vectors["voyage_image"][right]), 6)}
                for pos, left in enumerate(ids) for right in ids[pos + 1:]]
            group["evidence"]["embedding_model"] = MODELS["voyage"]
            group["evidence"]["group_basis"] = "same_prompt_not_visual_similarity"
        evaluation = evaluate_comparison(manifest, vectors, queries, retention, evaluation_arms=["voyage_image"])
        if evaluation["status"] != "completed_unjudged_canary":
            raise ValueError("complete Voyage document and query coverage required for this revision")
        evaluation["selection_profile"] = manifest["selection_profile"]
        evaluation["historical_ab_complete"] = False
        evaluation["budget"] = {"attempts": len(ledger["attempts"]),
            "reserved_upper_bound_usd": sum(a["reserved_usd"] for a in ledger["attempts"]),
            "additional_reserved_usd": 0, "actual_invoice_usd": None}
        evaluation["refresh_network_calls"] = 0
        manifest["comparison_status"] = evaluation["status"]
        summary = {"schema_version": "2", "status": evaluation["status"] if apply else "dry_run",
            "source_run_id": source_run_id, "total_items": len(manifest["items"]),
            "active": len(retention["active_ids"]), "logical_deleted": len(retention["archived"]),
            "prompt_variant_groups": len(retention.get("prompt_variant_groups", [])),
            "unique_cache_receipts_reused": len(receipt_hashes), "vector_counts": evaluation["vector_counts"],
            "network_calls": 0, "new_embedding_calls": 0, "physical_files_deleted": 0,
            "canonical_writes": 0, "old_view_writes": 0, "budget": evaluation["budget"],
            "results_path": results_path.relative_to(root).as_posix(), "comparison_directory": REVISION_DIRECTORY}
        if not apply:
            return summary
        target.mkdir()
        for name, value in (("manifest", manifest), ("retention", retention), ("vectors", vectors),
                            ("queries", queries), ("evaluation", evaluation), ("summary", summary)):
            write_json(target / f"{name}.json", value)
        results_path.write_text(render_results(manifest, retention, evaluation["evaluations"],
                                              evaluation["similarity_groups"]), encoding="utf-8")
        write_json(target / "revision-receipt.json", {"schema_version": "1", "recorded_at": now(),
            **source_hashes,
            "reused_cache_receipt_sha256": receipt_hashes,
            "retention_policy": retention["policy"], "network_calls": 0, "complete": True})
        return summary
