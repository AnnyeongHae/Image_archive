"""Offline canary gate and full-library text search evaluation; no network."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from image_rag_eval.embedding_budget import encoded
from image_rag_eval.text_embedding_run import execute_manifest, load_manifest_vectors
from image_rag_eval.text_retrieval_eval import evaluate_canary, load_index_documents, rank_groups, render_full_report, render_report


def freeze_files(directory, files, root):
    directory = Path(directory).resolve()
    if not directory.is_relative_to(root.resolve() / "data/private-research"):
        raise ValueError("Evaluation must remain private")
    for name, raw in files.items():
        path = directory / name
        if path.is_symlink() or path.is_junction() or (path.exists() and path.read_bytes() != raw):
            raise ValueError("Immutable evaluation output differs")
    directory.mkdir(parents=True, exist_ok=True)
    for name, raw in files.items():
        path = directory / name
        if not path.exists():
            with path.open("xb") as handle:
                handle.write(raw)


def evaluate(root, manifest, tokenizer, run_dir, fixture, *, full_manifest=None, database=None, plan_dir=None):
    fixture_raw = fixture.read_bytes()
    fixture_data = json.loads(fixture_raw)
    manifest_raw = manifest.read_bytes()
    manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
    if json.loads(manifest_raw) != fixture_data["embedding_manifest"]:
        raise ValueError("Canary manifest and evaluation fixture differ")
    dry = execute_manifest(manifest, tokenizer, run_dir, archive_root=root)
    if dry["missing_inputs"] or dry["pending_or_uncertain_requests"]:
        raise ValueError("Canary cache is not complete")

    def forbid_network(*args, **kwargs):
        raise AssertionError("Offline replay must not attempt provider calls")

    # Exercise the real execute/cache branch with networking physically replaced.
    replay = execute_manifest(manifest, tokenizer, run_dir, archive_root=root, apply=True, execute=True,
                              transport=forbid_network, api_key="unused-offline-cache-replay")
    if replay["provider_calls_this_invocation"] != 0:
        raise ValueError("Replay tried to re-embed cached inputs")
    loaded = load_manifest_vectors(run_dir, manifest_sha, archive_root=root)
    result = evaluate_canary(fixture_data, loaded["vectors"], cache_audit={
        "verified": True, "provider_calls": replay["provider_calls_this_invocation"],
        "cache_hit_input_ids": sorted(loaded["vectors"]), "manifest_sha256": manifest_sha})
    result.update(fixture_sha256=hashlib.sha256(fixture_raw).hexdigest(),
                  manifest_sha256=manifest_sha, usage=loaded["receipt"], rerank_calls=0, image_embedding_calls=0)
    if full_manifest is not None:
        if not result["technical_gate_passed"]:
            raise ValueError("Canary gate failed")
        if database is None or plan_dir is None:
            raise ValueError("Full evaluation requires --database and --plan-dir")
        full_dry = execute_manifest(full_manifest, tokenizer, run_dir, archive_root=root)
        if full_dry["missing_inputs"] or full_dry["pending_or_uncertain_requests"]:
            raise ValueError("Full cache is not complete")
        full_replay = execute_manifest(full_manifest, tokenizer, run_dir, archive_root=root,
                                       apply=True, execute=True, transport=forbid_network,
                                       api_key="unused-offline-cache-replay")
        if full_replay["provider_calls_this_invocation"] != 0:
            raise ValueError("Full replay tried to re-embed cached inputs")
        full_sha = hashlib.sha256(full_manifest.read_bytes()).hexdigest()
        full = load_manifest_vectors(run_dir, full_sha, archive_root=root)
        documents = load_index_documents(root, database, plan_dir)
        if {d["input_id"] for d in documents} != set(full["vectors"]):
            raise ValueError("Full corpus and vector IDs differ")
        vectors = {d["item_id"]: full["vectors"][d["input_id"]] for d in documents}
        results = [{"query_id": q["query_id"], "text": q["text"], "source_anchor_groups": q["relevant_group_ids"],
                    "rankings": rank_groups(documents, vectors, loaded["vectors"][q["input_id"]],
                                            blocked_item_ids=fixture_data["blocked_item_ids"])} for q in fixture_data["queries"]]
        result = {"schema_version": "image-full-text-search-smoke-1", "status": "prepared_needs_human_review",
                  "canary_technical_gate_passed": True, "full_manifest_sha256": full_sha,
                  "query_manifest_sha256": manifest_sha, "fixture_sha256": hashlib.sha256(fixture_raw).hexdigest(),
                  "document_count": len(documents), "group_count": len({d["group_id"] for d in documents}),
                  "queries": results, "usage": full["receipt"], "rerank_calls": 0, "image_embedding_calls": 0,
                  "offline_replay_provider_calls": full_replay["provider_calls_this_invocation"],
                  "offline_replay_cached_inputs": len(full["vectors"]),
                  "metadata_human_approved": False, "release_eligible": False,
                  "quality_note": "Source-derived anchors; full corpus has unjudged relevant results. Not a human accuracy score."}
        return result, render_full_report(result, documents, root)
    return result, render_report(fixture_data, result, root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("manifest", "tokenizer", "run-dir", "fixture", "output-dir"):
        parser.add_argument("--" + flag, required=True, type=Path)
    for flag in ("full-manifest", "database", "plan-dir"):
        parser.add_argument("--" + flag, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if not args.apply:
        parser.exit(2, "blocked: --apply is required for cache replay and private evaluation evidence\n")
    try:
        result, report = evaluate(root, args.manifest, args.tokenizer, args.run_dir, args.fixture,
                                  full_manifest=args.full_manifest, database=args.database, plan_dir=args.plan_dir)
        raw = encoded(result)
        key = hashlib.sha256(raw).hexdigest()
        files = {"evaluation.json": raw}
        if report is not None:
            files["review.html"] = report.encode("utf-8")
        output = args.output_dir / key
        freeze_files(output, files, root)
        print(json.dumps({"status": result["status"], "path": output.relative_to(root) .as_posix() if output.is_absolute() else output.as_posix(),
                          "gates": result.get("gates"), "technical_gate_passed": result.get("technical_gate_passed"),
                          "usage": result["usage"], "hashes": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
                          "metrics": {name: {k: v for k, v in lane.items() if k != "queries"} for name, lane in result.get("lanes", {}).items()}},
                         ensure_ascii=False, indent=2))
        if result.get("technical_gate_passed") is False:
            raise SystemExit(2)
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, "blocked: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
