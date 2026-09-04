from __future__ import annotations

import argparse
import copy
import os
import time
from pathlib import Path
from typing import Any, Callable

try:
    from . import run_daily_sync as daily
    from .common import (
        DATA_ROOT,
        DEFAULT_CANONICAL,
        LEGACY_ROOT,
        atomic_write_json,
        read_json,
    )
except ImportError:  # direct script execution
    import run_daily_sync as daily
    from common import DATA_ROOT, DEFAULT_CANONICAL, LEGACY_ROOT, atomic_write_json, read_json


# Detail reads remain bounded per request even when the explicit run allowance is
# larger. OpenNana currently returns at most 100 list rows per page, so keeping
# detail batches at the same ceiling also makes every checkpoint inspectable.
MAX_DETAIL_BATCH = 100
RAW_BOOTSTRAP_PATTERNS = ("fetch-*.json", "daily-*.json", "backlog-*.json")
BACKLOG_MODE = "network_backlog_free"


def _run_id(started_at: str) -> str:
    nonce = daily.sha256_text(f"{time.time_ns()}:{os.getpid()}:{started_at}")[:8]
    return f"{daily.filesystem_run_id(started_at)}-{nonce}"


def activation_mode(*, fetch: bool, apply: bool, max_details: int | None) -> str | None:
    """Require a complete, explicitly bounded live activation."""
    if not fetch and not apply and max_details is None:
        return None
    if not fetch or not apply or max_details is None:
        raise ValueError("live backlog mode requires --fetch --apply --max-details N")
    if max_details < 1:
        raise ValueError("max-details must be a positive explicit bound")
    return "bounded_backlog"


def validate_backlog_config(config: dict[str, Any]) -> None:
    daily.validate_config(config)
    collection = config.get("collection", {})
    if str(collection.get("sort", "")).casefold() != "reviewed_at":
        raise ValueError("backlog sync requires sort=reviewed_at")
    if str(collection.get("order", "")).casefold() != "desc":
        raise ValueError("backlog sync requires order=DESC")


def _raw_bootstrap_paths(paths: daily.SyncPaths) -> list[Path]:
    candidates: set[Path] = set()
    raw_dir = paths.data_root / "raw"
    for pattern in RAW_BOOTSTRAP_PATTERNS:
        candidates.update(raw_dir.glob(pattern))
    return sorted(candidates, key=lambda path: path.name)


def _queue_run_ids(paths: daily.SyncPaths) -> set[str]:
    queue_paths = sorted((paths.queue.parent / "history").glob("*.json"))
    if paths.queue.exists():
        queue_paths.append(paths.queue)
    run_ids: set[str] = set()
    for queue_path in queue_paths:
        try:
            queue = read_json(queue_path)
        except (OSError, ValueError) as exc:
            raise ValueError(f"cannot verify durable queue evidence {queue_path}: {exc}") from exc
        run_id = str(queue.get("run_id") or "")
        if run_id and isinstance(queue.get("items"), list):
            run_ids.add(run_id)
    return run_ids


def _completion_evidence(
    paths: daily.SyncPaths,
    *,
    raw_path: Path,
    run_id: str,
    durable_queue_run_ids: set[str],
) -> str | None:
    """Return durable downstream evidence for one historical raw bundle."""
    batch_manifest_path = paths.data_root / "runs" / f"batch-{run_id}.json"
    if batch_manifest_path.exists():
        manifest = read_json(batch_manifest_path)
        if (
            str(manifest.get("run_id") or "") == run_id
            and str(manifest.get("status") or "") in {"pipeline_complete", "passed"}
        ):
            return "successful_batch_manifest"

    pipeline_manifest_path = paths.data_root / "runs" / f"pipeline-{run_id}.json"
    if pipeline_manifest_path.exists():
        manifest = read_json(pipeline_manifest_path)
        reported_raw = Path(str(manifest.get("raw_path") or ""))
        if (
            str(manifest.get("run_id") or "") == run_id
            and str(manifest.get("status") or "") == "passed"
            and int(manifest.get("exit_code", 1)) == 0
            and reported_raw.name == raw_path.name
        ):
            return "successful_pipeline_manifest"

    if run_id in durable_queue_run_ids:
        return "durable_review_queue"
    return None


def bootstrap_detail_processed_versions(
    state: dict[str, Any],
    paths: daily.SyncPaths,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a detail-only watermark from real private raw detail runs.

    ``source_versions`` is deliberately ignored: the forward inventory baseline
    records every visible list version, including rows whose detail was never
    fetched. An existing ledger is authoritative, including an intentionally
    empty one. Only the first migration reconstructs it from selected detail
    rows whose raw bundle has durable downstream completion evidence.
    """
    if "detail_processed_versions" in state:
        stored = state["detail_processed_versions"]
        if not isinstance(stored, dict):
            raise ValueError("state.detail_processed_versions must be an object")
        if any(
            not isinstance(upstream_id, str)
            or not upstream_id
            or not isinstance(version, str)
            or not version
            for upstream_id, version in stored.items()
        ):
            raise ValueError(
                "state.detail_processed_versions must contain non-empty string pairs"
            )
        trusted = dict(stored)
        bootstrapped = copy.deepcopy(state)
        bootstrapped["detail_processed_versions"] = trusted
        return bootstrapped, {
            "source": "state",
            "raw_artifacts_discovered": 0,
            "raw_artifacts_scanned": 0,
            "raw_artifacts_excluded_without_completion": 0,
            "raw_selected_rows": 0,
            "raw_version_pairs": 0,
            "stored_detail_versions_overlaid": len(trusted),
            "detail_processed_unique": len(trusted),
            "source_versions_ignored_for_detail_watermark": True,
        }

    artifacts: list[tuple[str, str, Path, list[dict[str, Any]]]] = []
    selected_rows = 0
    raw_paths = _raw_bootstrap_paths(paths)
    durable_queue_run_ids = _queue_run_ids(paths)
    excluded_without_completion = 0
    evidence_counts: dict[str, int] = {}
    for raw_path in raw_paths:
        try:
            bundle = read_json(raw_path)
        except (OSError, ValueError) as exc:
            raise ValueError(f"cannot bootstrap detail watermark from {raw_path}: {exc}") from exc
        run_id = str(bundle.get("run_id") or raw_path.stem)
        evidence = _completion_evidence(
            paths,
            raw_path=raw_path,
            run_id=run_id,
            durable_queue_run_ids=durable_queue_run_ids,
        )
        if evidence is None:
            excluded_without_completion += 1
            continue
        evidence_counts[evidence] = evidence_counts.get(evidence, 0) + 1
        selected = bundle.get("selected_list_metadata")
        if not isinstance(selected, list):
            raise ValueError(f"raw detail bundle lacks selected_list_metadata array: {raw_path}")
        clean_selected: list[dict[str, Any]] = []
        for item in selected:
            if not isinstance(item, dict):
                raise ValueError(f"raw selected_list_metadata contains a non-object: {raw_path}")
            clean_selected.append(item)
        selected_rows += len(clean_selected)
        artifacts.append(
            (
                str(bundle.get("observed_at") or ""),
                raw_path.name,
                raw_path,
                clean_selected,
            )
        )

    # Process by observation time so a later fetched version wins. Filename is
    # a deterministic tie-breaker for historical bundles with equal timestamps.
    versions: dict[str, str] = {}
    raw_version_pairs = 0
    for _observed_at, _name, _path, selected in sorted(artifacts):
        for item in selected:
            upstream_id = daily.source_id(item)
            versions[upstream_id] = daily.metadata_version(item)
            raw_version_pairs += 1

    bootstrapped = copy.deepcopy(state)
    bootstrapped["detail_processed_versions"] = versions
    report = {
        "source": "raw_completion_evidence",
        "raw_artifacts_discovered": len(raw_paths),
        "raw_artifacts_scanned": len(artifacts),
        "raw_artifacts_excluded_without_completion": excluded_without_completion,
        "completion_evidence_counts": dict(sorted(evidence_counts.items())),
        "raw_selected_rows": selected_rows,
        "raw_version_pairs": raw_version_pairs,
        "stored_detail_versions_overlaid": 0,
        "detail_processed_unique": len(versions),
        "source_versions_ignored_for_detail_watermark": True,
    }
    return bootstrapped, report


def _reviewed_at(item: dict[str, Any]) -> str:
    return str(item.get("reviewed_at") or item.get("updated_at") or "")


def select_backlog_metadata(
    list_metadata: list[dict[str, Any]],
    state: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Return unprocessed or list-version-changed free rows, newest first."""
    processed = state.get("detail_processed_versions", {})
    if not isinstance(processed, dict):
        raise ValueError("state.detail_processed_versions must be an object")
    ordered = sorted(list_metadata, key=_reviewed_at, reverse=True)
    candidates: list[dict[str, Any]] = []
    unprocessed = 0
    changed = 0
    unchanged = 0
    for item in ordered:
        upstream_id = daily.source_id(item)
        current_version = daily.metadata_version(item)
        previous_version = processed.get(upstream_id)
        if previous_version is None:
            candidates.append(item)
            unprocessed += 1
        elif str(previous_version) != current_version:
            candidates.append(item)
            changed += 1
        else:
            unchanged += 1
    return candidates, {
        "unprocessed_details": unprocessed,
        "processed_version_changed": changed,
        "processed_version_unchanged": unchanged,
        "candidate_total": len(candidates),
    }


def _run_output_paths(paths: daily.SyncPaths, run_id: str) -> tuple[Path, Path]:
    return (
        paths.data_root / "runs" / f"backlog-sync-{run_id}.json",
        paths.data_root / "runs" / f"backlog-sync-{run_id}-summary.json",
    )


def _write_run_outputs(
    paths: daily.SyncPaths,
    run_id: str,
    manifest: dict[str, Any],
    summary: dict[str, Any],
) -> tuple[Path, Path]:
    manifest_path, summary_path = _run_output_paths(paths, run_id)
    atomic_write_json(manifest_path, manifest)
    atomic_write_json(summary_path, summary)
    return manifest_path, summary_path


def _execute_backlog_sync_unlocked(
    *,
    paths: daily.SyncPaths,
    max_details: int,
    batch_size: int = MAX_DETAIL_BATCH,
    client_factory: daily.ClientFactory = daily.default_client_factory,
) -> tuple[dict[str, Any], int]:
    daily.ensure_sync_directories(paths)
    if max_details < 1:
        raise ValueError("max-details must be a positive explicit bound")
    daily.chunks([], batch_size)  # validates the <=100 completed-batch ceiling
    config = read_json(paths.config)
    validate_backlog_config(config)
    initial_state = daily.bootstrap_prompt_hash_occurrences(read_json(paths.state), paths)
    current_state, bootstrap_summary = bootstrap_detail_processed_versions(initial_state, paths)

    started_at = daily.utc_now()
    run_id = _run_id(started_at)
    manifest: dict[str, Any] = {
        "schema_version": "opennana-backlog-sync-run-1.0",
        "run_id": run_id,
        "started_at": started_at,
        "status": "running",
        "mode": BACKLOG_MODE,
        "access_type": 0,
        "sort": "reviewed_at",
        "order": "DESC",
        "page_size": daily.PAGE_SIZE,
        "max_details": max_details,
        "batch_size": batch_size,
        "requests_per_second_max": float(config["collection"]["requests_per_second"]),
        "concurrency": 1,
        "detail_processed_watermark": "state.detail_processed_versions",
        "observed_inventory_watermark": "state.source_versions",
        "canonical_mutated": False,
        "automatic_decision_apply": False,
        "public_release_effect": False,
        "batches": [],
    }
    summary: dict[str, Any] = {
        "listed_unique": 0,
        "candidate_total": 0,
        "selected": 0,
        "remaining_estimate": 0,
        "batches_planned": 0,
        "batches_completed": 0,
        "free_details": 0,
        "locked_metadata_only": 0,
        "normalized": 0,
        "auto_collapsed": 0,
        "queue_new_from_run": 0,
        "resumable": True,
        "bootstrap": bootstrap_summary,
    }
    exit_code = 0
    error: str | None = None
    manifest_path, summary_path = _run_output_paths(paths, run_id)
    try:
        client = client_factory(config)
        robots_policy = daily.verify_robots(config, client)
        listed, list_summary = daily.collect_all_free_list_metadata(config, client)
        candidates, selection_summary = select_backlog_metadata(listed, current_state)
        selected = candidates[:max_details]
        batches = daily.chunks(selected, batch_size)
        summary.update(list_summary)
        summary.update(selection_summary)
        summary.update(
            {
                "selected": len(selected),
                "remaining_estimate": max(0, len(candidates) - len(selected)),
                "batches_planned": len(batches),
            }
        )
        manifest["robots_policy"] = robots_policy
        manifest["list_summary"] = list_summary
        manifest["selection_summary"] = selection_summary
        manifest["selected"] = len(selected)
        manifest["remaining_estimate_initial"] = summary["remaining_estimate"]
        # Create the run record before the first detail request. Per-batch raw,
        # staging, dedupe, and batch manifests are written before state commits.
        manifest["summary"] = dict(summary)
        _write_run_outputs(paths, run_id, manifest, summary)

        completed_selected = 0
        for number, batch in enumerate(batches, 1):
            observed_at = daily.utc_now()
            batch_run_id = f"backlog-{run_id}-b{number:04d}"
            remaining_after_batch = max(0, len(candidates) - completed_selected - len(batch))
            current_state, batch_summary = daily.process_completed_batch(
                paths=paths,
                config=config,
                state=current_state,
                client=client,
                selected_metadata=batch,
                batch_run_id=batch_run_id,
                observed_at=observed_at,
                robots_policy=robots_policy,
                batch_number=number,
                run_mode=BACKLOG_MODE,
                checkpoint_name="backlog_sync_checkpoint",
                checkpoint_extra={
                    "parent_run_id": run_id,
                    "max_details": max_details,
                    "batch_size": batch_size,
                    "processed_in_run": completed_selected + len(batch),
                    "candidate_total_at_start": len(candidates),
                    "remaining_estimate": remaining_after_batch,
                    "run_manifest": str(manifest_path),
                },
            )
            completed_selected += len(batch)
            batch_summary["remaining_estimate"] = remaining_after_batch
            manifest["batches"].append(batch_summary)
            summary["batches_completed"] += 1
            summary["remaining_estimate"] = remaining_after_batch
            for key in (
                "free_details",
                "locked_metadata_only",
                "normalized",
                "auto_collapsed",
                "queue_new_from_run",
            ):
                summary[key] += int(batch_summary[key])
            manifest["last_completed_batch"] = number
            manifest["summary"] = dict(summary)
            _write_run_outputs(paths, run_id, manifest, summary)
        manifest["status"] = "passed"
    except Exception as exc:
        exit_code = 1
        error = str(exc)
        manifest["status"] = "failed"
        manifest["failed_after_completed_batches"] = summary["batches_completed"]

    manifest["finished_at"] = daily.utc_now()
    manifest["error"] = error
    manifest["summary"] = dict(summary)
    summary["status"] = manifest["status"]
    summary["error"] = error
    summary["run_id"] = run_id
    manifest_path, summary_path = _write_run_outputs(paths, run_id, manifest, summary)
    return {
        "status": manifest["status"],
        "exit_code": exit_code,
        "run_id": run_id,
        "manifest": str(manifest_path),
        "summary_path": str(summary_path),
        "summary": summary,
    }, exit_code


def execute_backlog_sync(
    *,
    paths: daily.SyncPaths,
    max_details: int,
    batch_size: int = MAX_DETAIL_BATCH,
    client_factory: daily.ClientFactory = daily.default_client_factory,
) -> tuple[dict[str, Any], int]:
    daily.ensure_sync_directories(paths)
    try:
        with daily.exclusive_sync_lock(paths):
            return _execute_backlog_sync_unlocked(
                paths=paths,
                max_details=max_details,
                batch_size=batch_size,
                client_factory=client_factory,
            )
    except daily.SyncAlreadyRunning as exc:
        return {
            "status": "skipped_locked",
            "exit_code": 0,
            "network": False,
            "writes": False,
            "reason": str(exc),
        }, 0


def dry_run_plan(*, batch_size: int = MAX_DETAIL_BATCH) -> dict[str, Any]:
    daily.chunks([], batch_size)
    return {
        "schema_version": "opennana-backlog-sync-plan-1.0",
        "mode": "offline_dry_run",
        "network": False,
        "writes": False,
        "max_details": None,
        "batch_size": batch_size,
        "batch_size_max": MAX_DETAIL_BATCH,
        "selection": "reviewed_at DESC; detail-unprocessed or detail-version-changed free rows",
        "watermarks": {
            "observed_list_versions": "state.source_versions",
            "processed_detail_versions": "state.detail_processed_versions",
        },
        "bootstrap": list(RAW_BOOTSTRAP_PATTERNS),
        "checkpoint": "after each completed detail batch only",
        "canonical_mutation": False,
        "automatic_decision_apply": False,
        "public_release_effect": False,
        "next": "use --fetch --apply --max-details N; N may exceed 100 and is chunked into batches of at most 100",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run an explicit, bounded, resumable OpenNana free-detail backlog sync."
    )
    parser.add_argument("--fetch", action="store_true", help="Enable public API reads; requires --apply and --max-details.")
    parser.add_argument("--apply", action="store_true", help="Write private workflow artifacts; requires --fetch and --max-details.")
    parser.add_argument("--max-details", type=int, default=None, help="Required positive live-run detail ceiling; may exceed 100.")
    parser.add_argument("--batch-size", type=int, default=MAX_DETAIL_BATCH, help="Completed checkpoint batch size, 1..100.")
    parser.add_argument("--data-root", type=Path, default=DATA_ROOT)
    parser.add_argument("--canonical", type=Path, default=DEFAULT_CANONICAL)
    parser.add_argument("--review-js", type=Path, default=LEGACY_ROOT / "opennana-review-data.js")
    args = parser.parse_args()
    try:
        mode = activation_mode(fetch=args.fetch, apply=args.apply, max_details=args.max_details)
        if mode is None:
            print(daily.stable_json(dry_run_plan(batch_size=args.batch_size)), end="")
            return 0
        paths = daily.SyncPaths(
            data_root=args.data_root,
            canonical=args.canonical,
            review_js=args.review_js,
        )
        result, exit_code = execute_backlog_sync(
            paths=paths,
            max_details=int(args.max_details),
            batch_size=args.batch_size,
        )
        print(daily.stable_json(result), end="")
        return exit_code
    except ValueError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
