"""Offline full-library completion gate and immutable partial checkpoints.

Default is read-only. --allow-partial is mandatory until all requested images
and new-work core token coverage are complete. This never approves metadata or
rights, calls models, creates automations or performs embeddings.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from jsonschema import Draft202012Validator

from image_rag_eval.luna_analysis_import import _path, digest, encode
from image_rag_eval.luna_library_store import _check_tokens, _read, _turn_receipt, build_library_store
from prepare_luna_full_library import BASE, RUN, read_manifest, validate_progress

SCHEMA = "luna-full-run-execution-summary-1"
REGISTRY_SCHEMA = "00_CORE/schemas/content_registry.schema.json"
CORE_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens")
TRACKED_ARTIFACTS = (
    "db/metadata/0002_luna_library.sqlite.sql", "src/image_rag_eval/luna_library_store.py", "src/build_luna_library_store.py",
    "src/finalize_luna_full_run.py", "src/prepare_luna_full_library.py", "src/image_rag_eval/luna_compact.py",
    "src/image_rag_eval/luna_exec_usage.py",
    "src/image_rag_eval/compact_projection.py",
    "src/measure_luna_full_turn.py", "src/build_luna_full_review.py",
    "src/image_rag_eval/prompt_arguments.py",
    "src/prepare_luna_result_revision.py",
    "src/audit_luna_rendered_images.py", "db/metadata/README.md",
)
WORKSPACE_CONTRACTS = (
    REGISTRY_SCHEMA, "00_CORE/schemas/image_luna_compact_result.schema.json",
    "00_CORE/templates/image_luna_compact.instructions.md",
)


class FinalizeError(ValueError):
    pass


def _aggregate(rows: list[dict]) -> dict:
    fields = (*CORE_FIELDS, "total_tokens_calculated")
    totals, known, unknown = {}, {}, {}
    for key in fields:
        values = [row.get(key) for row in rows]
        if any(value is not None and (type(value) is not int or value < 0) for value in values):
            raise FinalizeError("Negative or invalid usage partition")
        known[key] = sum(value for value in values if value is not None)
        unknown[key] = sum(value is None for value in values)
        totals[key] = known[key] if values and not unknown[key] else None
    def remainder(*keys):
        values = [totals[key] for key in keys]
        return values[0] - sum(values[1:]) if all(value is not None for value in values) else None
    totals["uncached_input_tokens_calculated"] = remainder("input_tokens", "cached_input_tokens")
    totals["ordinary_input_tokens_calculated"] = remainder("input_tokens", "cached_input_tokens", "cache_write_input_tokens")
    return {"usage": totals, "known_usage_subtotals": known, "unknown_partition_counts": unknown,
            "actual_billed_tokens": None, "actual_billed_cost": None}


def normalize_turn_receipts(entries: list[dict], style_items: dict) -> dict:
    """Deduplicate exact (session,turn) rows, not aggregate/cumulative receipts."""
    turns, receipts, source_hashes = {}, {}, {}
    for entry in entries:
        receipt, raw = entry["receipt"], entry["raw"]
        sha = digest(raw)
        if json.loads(raw) != receipt:
            raise FinalizeError("Token receipt raw bytes changed")
        if receipt.get("schema_version") != "image-luna-rollout-turn-usage-receipt-1":
            raise FinalizeError("Full-run finalization requires explicit turn-scoped receipts")
        _turn_receipt(receipt, style_items)
        if receipt.get("actual_billed_tokens") is not None or receipt.get("actual_billed_cost") is not None:
            raise FinalizeError("Local telemetry must not claim billed usage")
        path = entry["path"]
        if path in source_hashes and source_hashes[path] != sha:
            raise FinalizeError("Conflicting token file evidence")
        source_hashes[path] = sha
        receipts.setdefault(sha, receipt)
        session = receipt["session_id_reported"]
        for turn in receipt["turns"]:
            identity = session, turn["turn_id"]
            if identity in turns and encode(turns[identity]) != encode(turn):
                raise FinalizeError("Same completed turn has conflicting token evidence")
            turns[identity] = turn
    ordered = sorted(turns.items())
    rows = [{**turn["attributable_usage"], "total_tokens_calculated": turn["total_tokens_calculated"]} for _, turn in ordered]
    covered = sorted({style for _, turn in ordered if turn["total_tokens_calculated"] is not None for style in turn["style_ids"]})
    unknown_core = [list(key) for key, turn in ordered if turn["total_tokens_calculated"] is None]
    return {**_aggregate(rows), "scope": "unique_explicit_completed_session_turns_only",
            "receipt_count_unique": len(receipts), "source_file_count": len(source_hashes),
            "session_count_unique": len({key[0] for key in turns}), "turn_count_unique": len(turns),
            "covered_style_ids": covered, "covered_image_count": len(covered), "unknown_core_turn_ids": unknown_core,
            "turn_identities": [{"session_id": key[0], "turn_id": key[1], "style_ids": turn["style_ids"],
                                 "batch_ids": turn["batch_ids"], "total_tokens_calculated": turn["total_tokens_calculated"]} for key, turn in ordered],
            "per_image_allocation": "not_divided_for_shared_turns", "source_files": source_hashes}


def collect_usage(root: Path, manifest: dict) -> tuple[dict, dict]:
    style_items = {task["style_id"]: task["item_id"] for task in manifest["tasks"] if task["analysis_mode"] == "new_compact"}
    entries = []
    execution = root / BASE / "execution"
    for path in sorted(execution.glob("*.tokens.json")):
        receipt, raw = _read(path)
        entries.append({"receipt": receipt, "raw": raw, "path": path.relative_to(root.parent).as_posix()})
    new_usage = normalize_turn_receipts(entries, style_items)
    legacy_runs = sorted({task["legacy"]["analysis_run_id"] for task in manifest["tasks"] if task["analysis_mode"] == "legacy_reuse"})
    rows, receipts, files = [], [], {}
    for run in legacy_runs:
        path = root / "data/private-research/image-rag-admin/luna-analysis" / run / "token-usage-receipt.json"
        receipt, raw = _read(path)
        if receipt.get("analysis_run_id") != run or receipt.get("actual_billed_tokens") is not None or receipt.get("actual_billed_cost") is not None:
            raise FinalizeError("Legacy token receipt identity changed")
        usage = receipt["usage"]
        _check_tokens(usage)
        rows.append({"input_tokens": usage["input_tokens_including_cached"], "cached_input_tokens": usage["cached_input_tokens"],
                     "cache_write_input_tokens": usage.get("cache_write_input_tokens"), "output_tokens": usage["output_tokens_including_reasoning"],
                     "reasoning_output_tokens": usage["reasoning_output_tokens"], "total_tokens_calculated": usage["total_tokens"]})
        receipts.append({"analysis_run_id": run, "scope": receipt.get("scope", "observed_isolated_local_codex_logs"),
                         "evidence_status": receipt["evidence_status"], "total_tokens": usage["total_tokens"],
                         "notes": receipt.get("notes", [])})
        files[path.relative_to(root.parent).as_posix()] = digest(raw)
    return new_usage, {**_aggregate(rows), "scope": "historical_receipt_scopes_reused_without_new_calls", "runs": receipts, "source_files": files}


def assemble_summary(manifest: dict, progress: dict, database: dict, new_usage: dict, legacy_usage: dict, *, allow_partial=False) -> dict:
    tasks = manifest["tasks"]
    new_styles = {task["style_id"] for task in tasks if task["analysis_mode"] == "new_compact"}
    legacy_styles = {task["style_id"] for task in tasks if task["analysis_mode"] == "legacy_reuse"}
    if (len(new_styles | legacy_styles) != len(tasks) or new_styles & legacy_styles
            or manifest["counts"]["approved_images"] != len(tasks)
            or manifest["counts"]["legacy_reused"] != len(legacy_styles) or manifest["counts"]["new_compact"] != len(new_styles)):
        raise FinalizeError("Requested task scope is ambiguous")
    valid = set(progress["completed_styles"])
    if not valid <= new_styles or len(valid) != progress["new_valid"] or progress["legacy_reused"] != len(legacy_styles):
        raise FinalizeError("Progress does not match requested tasks")
    normalized_styles = set(progress.get("literal_normalized_styles", []))
    raw_strict_count = progress.get("raw_strict_valid_count", len(valid) - len(normalized_styles))
    if not normalized_styles <= valid or raw_strict_count != len(valid) - len(normalized_styles):
        raise FinalizeError("Raw-strict and normalized candidate counts disagree")
    invalid = progress.get("invalid", [])
    input_invalid = progress.get("input_invalid", [])
    if len(valid) + progress["missing"] + len(invalid) + len(input_invalid) != len(new_styles):
        raise FinalizeError("Progress coverage does not partition new work")
    covered = set(new_usage["covered_style_ids"])
    if not covered <= new_styles:
        raise FinalizeError("Usage scope includes an unrelated image")
    results_complete = len(valid) == len(new_styles) and not invalid and not input_invalid and not progress["missing"]
    usage_complete = covered == new_styles and not new_usage["unknown_core_turn_ids"]
    complete = results_complete and usage_complete
    if not complete and not allow_partial:
        raise FinalizeError("Run is incomplete; --allow-partial is required for a truthful checkpoint")
    if database:
        states = database["analysis_states"]
        if (states.get("legacy_reused", 0) != len(legacy_styles) or states.get("validated_candidate", 0) != len(valid)
                or states.get("invalid_result", 0) != len(invalid)
                or database["results"] != len(legacy_styles) + len(valid)
                or database.get("literal_normalized_candidates", 0) != len(normalized_styles)
                or database.get("new_raw_strict_valid_candidates", raw_strict_count) != raw_strict_count
                or database["source_commit_id"] != manifest["source_commit"]["id"]):
            raise FinalizeError("Analysis changed between validation and DB snapshot; retry")
        observed = database["usage_states"].get("observed_turn_scope", 0)
        if observed != len(covered):
            raise FinalizeError("Usage changed between receipts and DB snapshot; retry")
    return {"schema_version": SCHEMA, "analysis_run_id": manifest["analysis_run_id"],
            "completion_status": "analysis_and_usage_complete" if complete else "partial_checkpoint",
            "all_requested_analysis_complete": results_complete, "all_new_core_usage_observed": usage_complete,
            "partial_checkpoint_authorized": bool(allow_partial and not complete), "source_commit": manifest["source_commit"],
            "task_manifest_sha256": progress["tasks_sha256"],
            "coverage": {"requested_approved_images": len(tasks), "legacy_reused": len(legacy_styles), "new_requested": len(new_styles),
                         "new_valid": len(valid), "total_analysis_candidates": len(valid) + len(legacy_styles), "new_missing": progress["missing"],
                         "new_raw_strict_valid": raw_strict_count, "literal_normalized_count": len(normalized_styles),
                         "literal_normalized_style_ids": sorted(normalized_styles),
                         "new_invalid": invalid, "input_invalid": input_invalid, "new_usage_observed_images": len(covered),
                         "new_usage_pending_images": len(new_styles - covered), "completed_style_ids": sorted(valid),
                         "pending_usage_style_ids": sorted(new_styles - covered)},
            "tokens": {"new_execution": new_usage, "legacy_reused_history": legacy_usage,
                       "combined_billing_claim": None, "actual_billed_tokens": None, "actual_billed_cost": None,
                       "scope_note": "Historical and new scopes differ; cached and reasoning are subsets, not extra tokens."},
            "database": {key: value for key, value in database.items() if key != "status"},
            "metadata_human_approved": False, "release_eligible": False, "public_rights_approved": False,
            "provider_calls_by_finalizer": 0, "model_calls_by_finalizer": 0, "embedding_calls": 0,
            "automation_changes": 0, "public_deployments": 0,
            "review_note": "Execution completeness does not approve metadata, resolve QA findings or grant image rights."}


def _artifact_evidence(root: Path, database: dict, new_usage: dict, legacy_usage: dict) -> list[dict]:
    workspace = root.parent
    paths = [root / relative for relative in TRACKED_ARTIFACTS]
    paths += [workspace / relative for relative in WORKSPACE_CONTRACTS]
    paths.append(root / BASE / "tasks.json")
    database_path = Path(database["database_path"])
    if database_path.is_file():
        paths += [database_path, database_path.parent / "receipt.json"]
    paths += [workspace / path for usage in (new_usage, legacy_usage) for path in usage["source_files"]]
    unique = {}
    for path in paths:
        absolute = path.resolve()
        if not absolute.is_relative_to(workspace) or not absolute.is_file() or path.is_symlink():
            raise FinalizeError("Artifact must be an existing workspace file")
        relative = absolute.relative_to(workspace).as_posix()
        unique[relative] = digest(absolute.read_bytes())
    return [{"path": path, "sha256": unique[path]} for path in sorted(unique)]


def _registry(summary_path: str, summary_sha: str, evidence: list[dict], checkpoint_id: str) -> dict:
    artifacts = [{"path": summary_path, "sha256": summary_sha}, *evidence]
    return {"schema_version": "1.0.0", "registry_id": "luna-full-checkpoint-" + checkpoint_id, "campaign_id": "image-rag-private-research",
            "items": [{"content_id": "luna-full-checkpoint-" + checkpoint_id[:16] + f"-{index:03d}", "deliverable_id": "luna-full-library-checkpoint",
                       "channel": "private-research", "content_type": "execution-summary" if index == 0 else "evidence-artifact",
                       "revision": 1, "status": "needs_review", "artifact_path": row["path"], "artifact_sha256": row["sha256"],
                       "claim_ids": [], "source_reference_ids": []} for index, row in enumerate(artifacts)]}


def write_checkpoint(root: Path, summary: dict, evidence: list[dict], *, apply=False) -> dict:
    workspace = root.parent
    summary = {**summary, "artifact_evidence": evidence}
    raw_summary = encode(summary)
    key = digest(raw_summary)
    relative_base = f"{root.name}/{BASE}/checkpoints/{key}"
    directory = _path(workspace, relative_base)
    summary_relative = relative_base + "/execution-summary.json"
    registry = _registry(summary_relative, key, evidence, key)
    schema, _ = _read(workspace / REGISTRY_SCHEMA)
    Draft202012Validator(schema).validate(registry)
    files = {"execution-summary.json": raw_summary, "content-registry.json": encode(registry)}
    result = {"status": "dry_run", "checkpoint_id": key, "execution_summary_path": summary_relative,
              "execution_summary_sha256": key, "content_registry_path": relative_base + "/content-registry.json",
              "completion_status": summary["completion_status"], "coverage": summary["coverage"],
              "database": summary["database"], "tokens": summary["tokens"], "metadata_human_approved": False,
              "release_eligible": False, "provider_calls": 0, "embedding_calls": 0}
    def check_sources():
        for row in evidence:
            if digest(_path(workspace, row["path"]).read_bytes()) != row["sha256"]:
                raise FinalizeError("Registered evidence changed before publication")
    if directory.exists():
        if set(path.name for path in directory.iterdir()) != set(files) or any((directory / name).read_bytes() != raw for name, raw in files.items()):
            raise FinalizeError("Existing immutable checkpoint differs")
        check_sources()
        return {**result, "status": "unchanged"}
    check_sources()
    if not apply:
        return result
    directory.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".finalize-", dir=directory.parent))
    try:
        for name, raw in files.items():
            with (temporary / name).open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        check_sources()
        temporary.rename(directory)
    finally:
        if temporary.exists():
            for name in files:
                (temporary / name).unlink(missing_ok=True)
            temporary.rmdir()
    return {**result, "status": "prepared"}


def finalize(root: Path, *, apply=False, allow_partial=False) -> dict:
    root = root.resolve()
    manifest, manifest_raw = read_manifest(root)
    progress = validate_progress(root)
    new_usage, legacy_usage = collect_usage(root, manifest)
    # Reject an unauthorized partial checkpoint before any new DB is created.
    assemble_summary(manifest, progress, {}, new_usage, legacy_usage, allow_partial=allow_partial)
    database = build_library_store(root, apply=apply)
    latest_progress = validate_progress(root)
    latest_new, latest_legacy = collect_usage(root, manifest)
    if (digest((root / BASE / "tasks.json").read_bytes()) != digest(manifest_raw)
            or encode(new_usage) != encode(latest_new) or encode(legacy_usage) != encode(latest_legacy)):
        raise FinalizeError("Manifest or token receipts changed during finalization; retry")
    if apply:
        database_path = Path(database["database_path"])
        if not database_path.is_file() or digest(database_path.read_bytes()) != database.get("database_sha256"):
            raise FinalizeError("Applied immutable DB artifact is missing or changed")
    summary = assemble_summary(manifest, latest_progress, database, latest_new, latest_legacy, allow_partial=allow_partial)
    evidence = _artifact_evidence(root, database, latest_new, latest_legacy)
    summary["database"]["database_path"] = Path(database["database_path"]).relative_to(root.parent).as_posix()
    return write_checkpoint(root, summary, evidence, apply=apply)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Append DB and execution checkpoint; no existing evidence is replaced")
    parser.add_argument("--allow-partial", action="store_true", help="Permit a clearly marked incomplete checkpoint")
    args = parser.parse_args()
    result = finalize(Path(__file__).resolve().parents[1], apply=args.apply, allow_partial=args.allow_partial)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
