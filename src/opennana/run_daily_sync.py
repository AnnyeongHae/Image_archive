from __future__ import annotations

import argparse
import copy
import datetime as dt
import os
import socket
import urllib.parse
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

try:
    from .build_review_queue import (
        build_projection,
        build_queue,
        history_path_for_queue,
        projection_javascript,
        write_immutable_json,
    )
    from .collect import (
        PROMPT_BODY_KEYS,
        RateLimitedClient,
        extract_detail,
        extract_payload_items,
        generic_robots_allows,
        is_paid_or_locked,
        metadata_version,
        strip_prompt_bodies,
    )
    from .common import (
        DATA_ROOT,
        DEFAULT_CANONICAL,
        LEGACY_ROOT,
        atomic_write_json,
        atomic_write_text,
        read_json,
        sha256_text,
        source_id,
        stable_json,
    )
    from .dedupe import classify_bundle
    from .normalize import normalize_bundle
except ImportError:  # direct script execution
    from build_review_queue import (
        build_projection,
        build_queue,
        history_path_for_queue,
        projection_javascript,
        write_immutable_json,
    )
    from collect import (
        PROMPT_BODY_KEYS,
        RateLimitedClient,
        extract_detail,
        extract_payload_items,
        generic_robots_allows,
        is_paid_or_locked,
        metadata_version,
        strip_prompt_bodies,
    )
    from common import (
        DATA_ROOT,
        DEFAULT_CANONICAL,
        LEGACY_ROOT,
        atomic_write_json,
        atomic_write_text,
        read_json,
        sha256_text,
        source_id,
        stable_json,
    )
    from dedupe import classify_bundle
    from normalize import normalize_bundle


PAGE_SIZE = 100
MAX_BATCH_SIZE = 100
MAX_PAGES_FAILSAFE = 10_000
PROMPT_HASH_SOURCE_ID_CAP = 32
FORWARD_ONLY_MODE = "forward_only_from_baseline"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def filesystem_run_id(observed_at: str) -> str:
    return observed_at.replace(":", "").replace("-", "")


class SyncClient(Protocol):
    request_count: int

    def get_text(self, url: str) -> str: ...

    def get_json(self, url: str) -> Any: ...


ClientFactory = Callable[[dict[str, Any]], SyncClient]


@dataclass(frozen=True)
class SyncPaths:
    data_root: Path = DATA_ROOT
    canonical: Path = DEFAULT_CANONICAL
    review_js: Path = LEGACY_ROOT / "opennana-review-data.js"

    @property
    def config(self) -> Path:
        return self.data_root / "config.json"

    @property
    def state(self) -> Path:
        return self.data_root / "state.json"

    @property
    def queue(self) -> Path:
        return self.data_root / "review_queue" / "current.json"

    @property
    def draft(self) -> Path:
        return self.data_root / "decisions" / "decision-draft.json"

    @property
    def lock(self) -> Path:
        return self.data_root / "runs" / "daily-sync.lock"


class SyncAlreadyRunning(RuntimeError):
    pass


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@contextmanager
def exclusive_sync_lock(paths: SyncPaths):
    """Hold one process-wide daily sync lock for the complete network run."""
    paths.lock.parent.mkdir(parents=True, exist_ok=True)
    owner = {
        "schema_version": "opennana-daily-sync-lock-1.0",
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "acquired_at": utc_now(),
        "token": sha256_text(f"{os.getpid()}:{utc_now()}:{paths.lock}"),
    }
    descriptor: int | None = None
    for attempt in range(2):
        try:
            descriptor = os.open(paths.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, stable_json(owner).encode("utf-8"))
            os.fsync(descriptor)
            break
        except FileExistsError as exc:
            stale = False
            try:
                existing = read_json(paths.lock)
                same_host = existing.get("hostname") == socket.gethostname()
                stale = bool(same_host and not _pid_is_alive(int(existing.get("pid", -1))))
            except (OSError, ValueError, TypeError):
                # An unreadable lock is treated as active rather than removed.
                stale = False
            if stale and attempt == 0:
                try:
                    paths.lock.unlink()
                except FileNotFoundError:
                    pass
                continue
            raise SyncAlreadyRunning(f"daily sync already running: {paths.lock}") from exc
    if descriptor is None:
        raise SyncAlreadyRunning(f"daily sync already running: {paths.lock}")
    try:
        yield owner
    finally:
        os.close(descriptor)
        try:
            existing = read_json(paths.lock)
            if existing.get("token") == owner["token"]:
                paths.lock.unlink()
        except FileNotFoundError:
            pass


def ensure_sync_directories(paths: SyncPaths) -> None:
    for name in ("raw", "staging", "review_queue", "decisions", "runs"):
        (paths.data_root / name).mkdir(parents=True, exist_ok=True)


def default_client_factory(config: dict[str, Any]) -> SyncClient:
    collection = config["collection"]
    return RateLimitedClient(
        requests_per_second=float(collection["requests_per_second"]),
        timeout=int(collection["timeout_seconds"]),
        user_agent=str(collection["user_agent"]),
    )


def validate_config(config: dict[str, Any]) -> None:
    collection = config.get("collection", {})
    policy = config.get("policy", {})
    if int(collection.get("access_type", -1)) != 0:
        raise ValueError("daily sync requires the public free access_type=0 lane")
    if int(collection.get("page_size", 0)) != PAGE_SIZE:
        raise ValueError(f"daily sync requires page_size={PAGE_SIZE}")
    if int(collection.get("concurrency", 0)) != 1:
        raise ValueError("daily sync requires concurrency=1")
    requests_per_second = float(collection.get("requests_per_second", 0))
    if requests_per_second <= 0 or requests_per_second > 1.0:
        raise ValueError("daily sync detail reads must be rate limited to at most 1 request/second")
    if policy.get("paid_prompt_body") != "forbidden" or config.get("collect_paid_prompt_bodies") is not False:
        raise ValueError("daily sync requires paid prompt bodies to be forbidden")
    if policy.get("auto_publish") is not False or config.get("public_release_allowed") is not False:
        raise ValueError("daily sync cannot auto-publish or enable public release")


def activation_mode(
    *,
    fetch: bool,
    apply: bool,
    all_free: bool,
    baseline_only: bool,
) -> str | None:
    """Return the explicitly activated live mode, or ``None`` for dry-run.

    Baseline initialization and normal daily collection are deliberately
    separate commands.  This prevents a missing baseline from silently
    turning a forward-only schedule into a historical detail backfill.
    """
    flags = (fetch, apply, all_free, baseline_only)
    if not any(flags):
        return None
    if not fetch or not apply or all_free == baseline_only:
        raise ValueError(
            "live mode requires --fetch --apply and exactly one of "
            "--all-free or --baseline-only"
        )
    return "forward_baseline" if baseline_only else "daily_sync"


def config_requires_forward_baseline(config: dict[str, Any]) -> bool:
    daily_sync = config.get("daily_sync", {})
    return isinstance(daily_sync, dict) and daily_sync.get("mode") == FORWARD_ONLY_MODE


def forward_baseline_ready(state: dict[str, Any]) -> bool:
    baseline = state.get("forward_baseline")
    return bool(
        isinstance(baseline, dict)
        and baseline.get("mode") == FORWARD_ONLY_MODE
        and baseline.get("status") == "established"
        and int(baseline.get("listed_unique", 0)) > 0
    )


def require_forward_baseline(config: dict[str, Any], state: dict[str, Any]) -> None:
    if config_requires_forward_baseline(config) and not forward_baseline_ready(state):
        raise RuntimeError(
            "forward-only daily sync blocked: establish the inventory baseline first with "
            "--fetch --apply --baseline-only"
        )


def verify_robots(config: dict[str, Any], client: SyncClient) -> dict[str, Any]:
    robots = client.get_text(str(config["source"]["robots_url"]))
    allowed, parsed = generic_robots_allows(robots, "/api/prompts")
    signals = parsed.get("content_signals", {})
    if not parsed.get("generic_group_found"):
        raise RuntimeError("collection stopped: generic robots group was not found")
    if not allowed:
        raise RuntimeError("collection stopped: generic robots policy disallows /api/prompts")
    if signals.get("search") != "yes" or signals.get("ai-train") != "no":
        raise RuntimeError("collection stopped: Content-Signal must include search=yes and ai-train=no")
    return {
        "evaluated_user_agent": "*",
        "evaluated_path": "/api/prompts",
        "allowed": True,
        "content_signals": signals,
        "robots_observed_sha256": sha256_text(robots),
    }


def list_url(config: dict[str, Any], page: int) -> str:
    collection = config["collection"]
    params = urllib.parse.urlencode(
        {
            "page": page,
            "limit": PAGE_SIZE,
            "sort": collection["sort"],
            "order": collection["order"],
            "access_type": 0,
        }
    )
    return f"{config['source']['list_endpoint']}?{params}"


def _free_list_item(item: dict[str, Any]) -> bool:
    value = item.get("access_type", 0)
    return value is None or str(value).casefold() in {"0", "free"}


def collect_all_free_list_metadata(
    config: dict[str, Any],
    client: SyncClient,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Read every access_type=0 list page and return unique visible metadata.

    The list response is treated as metadata even if an upstream field drift
    unexpectedly places a prompt body in it. Prompt body keys are stripped
    before version comparison or persistence.
    """
    unique: dict[str, dict[str, Any]] = {}
    page_fingerprints: set[str] = set()
    pages = 0
    repeated_ids = 0
    stripped_list_bodies = 0
    for page in range(1, MAX_PAGES_FAILSAFE + 1):
        payload = client.get_json(list_url(config, page))
        raw_items = extract_payload_items(payload)
        pages += 1
        if not raw_items:
            break
        if any(not _free_list_item(item) for item in raw_items):
            raise RuntimeError("collection stopped: access_type=0 list returned a non-free row")
        page_basis = [
            {"source_id": source_id(item), "metadata_version": metadata_version(item)}
            for item in raw_items
        ]
        fingerprint = sha256_text(stable_json(page_basis, indent=None))
        if fingerprint in page_fingerprints:
            raise RuntimeError("collection stopped: repeated list page detected")
        page_fingerprints.add(fingerprint)
        for item in raw_items:
            clean, removed = strip_prompt_bodies(item)
            stripped_list_bodies += int(removed)
            upstream_id = source_id(clean)
            if upstream_id in unique:
                repeated_ids += 1
                continue
            unique[upstream_id] = clean
        if len(raw_items) < PAGE_SIZE:
            break
    else:
        raise RuntimeError(f"collection stopped: list exceeded {MAX_PAGES_FAILSAFE} pages")
    return list(unique.values()), {
        "list_pages": pages,
        "listed_unique": len(unique),
        "repeated_list_ids": repeated_ids,
        "stripped_list_prompt_bodies": stripped_list_bodies,
    }


def select_changed_metadata(
    list_metadata: list[dict[str, Any]],
    state: dict[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    previous_versions = state.get("source_versions", {})
    if not isinstance(previous_versions, dict):
        raise ValueError("state.source_versions must be an object")
    changed = [
        item
        for item in list_metadata
        if str(previous_versions.get(source_id(item), "")) != metadata_version(item)
    ]
    return changed, len(list_metadata) - len(changed)


def inventory_version_sha256(list_metadata: list[dict[str, Any]]) -> str:
    basis = sorted(
        (
            {
                "source_id": source_id(item),
                "metadata_version": metadata_version(item),
            }
            for item in list_metadata
        ),
        key=lambda item: item["source_id"],
    )
    return sha256_text(stable_json(basis, indent=None))


def _execute_forward_baseline_unlocked(
    *,
    paths: SyncPaths,
    client_factory: ClientFactory = default_client_factory,
) -> tuple[dict[str, Any], int]:
    """Checkpoint the current public/free inventory without detail reads.

    This is intentionally not a collector run: it only records list-visible
    source/version pairs.  Review queues, canonical records, raw details,
    staging artifacts, and decision drafts are not read or written.
    """
    ensure_sync_directories(paths)
    config = read_json(paths.config)
    state = read_json(paths.state)
    validate_config(config)
    started_at = utc_now()
    run_id = filesystem_run_id(started_at)
    manifest_path = paths.data_root / "runs" / f"forward-baseline-{run_id}.json"
    manifest: dict[str, Any] = {
        "schema_version": "opennana-forward-baseline-run-1.0",
        "run_id": run_id,
        "started_at": started_at,
        "mode": FORWARD_ONLY_MODE,
        "status": "running",
        "access_type": 0,
        "page_size": PAGE_SIZE,
        "detail_requests": 0,
        "raw_detail_writes": 0,
        "queue_mutated": False,
        "canonical_mutated": False,
        "automatic_decision_apply": False,
        "public_release_effect": False,
    }
    exit_code = 0
    error: str | None = None
    summary: dict[str, Any] = {
        "listed_unique": 0,
        "source_versions_before": 0,
        "source_versions_after": 0,
        "newly_baselined": 0,
        "metadata_versions_updated": 0,
        "metadata_versions_unchanged": 0,
        "detail_requests": 0,
        "raw_detail_writes": 0,
        "queue_mutated": False,
        "canonical_mutated": False,
    }
    try:
        if not config_requires_forward_baseline(config):
            raise ValueError(
                f"baseline initialization requires daily_sync.mode={FORWARD_ONLY_MODE}"
            )
        previous_versions = state.get("source_versions", {})
        if not isinstance(previous_versions, dict):
            raise ValueError("state.source_versions must be an object")
        client = client_factory(config)
        robots_policy = verify_robots(config, client)
        listed, list_summary = collect_all_free_list_metadata(config, client)
        if not listed:
            raise RuntimeError("forward baseline refused: public/free inventory was empty")

        versions = dict(previous_versions)
        newly_baselined = 0
        metadata_versions_updated = 0
        metadata_versions_unchanged = 0
        for item in listed:
            upstream_id = source_id(item)
            current_version = metadata_version(item)
            prior_version = versions.get(upstream_id)
            if prior_version is None:
                newly_baselined += 1
            elif str(prior_version) != current_version:
                metadata_versions_updated += 1
            else:
                metadata_versions_unchanged += 1
            versions[upstream_id] = current_version

        inventory_sha256 = inventory_version_sha256(listed)
        established_at = utc_now()
        summary.update(list_summary)
        summary.update(
            {
                "source_versions_before": len(previous_versions),
                "source_versions_after": len(versions),
                "newly_baselined": newly_baselined,
                "metadata_versions_updated": metadata_versions_updated,
                "metadata_versions_unchanged": metadata_versions_unchanged,
                "network_requests": int(getattr(client, "request_count", 0)),
            }
        )
        baseline = {
            "schema_version": "opennana-forward-baseline-1.0",
            "mode": FORWARD_ONLY_MODE,
            "status": "established",
            "established_at": established_at,
            "listed_unique": len(listed),
            "source_versions_total": len(versions),
            "inventory_version_sha256": inventory_sha256,
            "list_pages": int(list_summary["list_pages"]),
            "detail_requests": 0,
            "manifest": str(manifest_path),
        }
        next_state = copy.deepcopy(state)
        next_state["source_versions"] = versions
        next_state["forward_baseline"] = baseline

        manifest["robots_policy"] = robots_policy
        manifest["inventory_version_sha256"] = inventory_sha256
        manifest["summary"] = dict(summary)
        manifest["status"] = "passed"
        manifest["finished_at"] = established_at
        manifest["error"] = None
        # The manifest is written before the authoritative state checkpoint.
        # If the state write fails, the normal daily job remains fail-closed.
        atomic_write_json(manifest_path, manifest)
        atomic_write_json(paths.state, next_state)
    except Exception as exc:
        exit_code = 1
        error = str(exc)
        manifest["status"] = "failed"
        manifest["finished_at"] = utc_now()
        manifest["error"] = error
        manifest["summary"] = dict(summary)
        atomic_write_json(manifest_path, manifest)

    summary["status"] = manifest["status"]
    summary["error"] = error
    summary["run_id"] = run_id
    return {
        "status": manifest["status"],
        "exit_code": exit_code,
        "run_id": run_id,
        "manifest": str(manifest_path),
        "summary": summary,
    }, exit_code


def execute_forward_baseline(
    *,
    paths: SyncPaths,
    client_factory: ClientFactory = default_client_factory,
) -> tuple[dict[str, Any], int]:
    ensure_sync_directories(paths)
    try:
        with exclusive_sync_lock(paths):
            return _execute_forward_baseline_unlocked(paths=paths, client_factory=client_factory)
    except SyncAlreadyRunning as exc:
        return {
            "status": "skipped_locked",
            "exit_code": 0,
            "network": False,
            "writes": False,
            "reason": str(exc),
        }, 0


def _record_prompt_occurrence(
    occurrences: dict[str, Any],
    *,
    prompt_sha256: str,
    upstream_id: str,
    observed_at: str,
) -> None:
    if not prompt_sha256 or not upstream_id:
        return
    existing = occurrences.get(prompt_sha256)
    if not isinstance(existing, dict):
        existing = {
            "first_upstream_id": upstream_id,
            "first_seen_at": observed_at,
            "source_ids": [],
            "source_count": 0,
        }
    source_ids = [str(value) for value in existing.get("source_ids", []) if value]
    if upstream_id not in source_ids:
        source_ids.append(upstream_id)
    existing["source_ids"] = source_ids[:PROMPT_HASH_SOURCE_ID_CAP]
    existing["source_count"] = max(int(existing.get("source_count", 0)), len(source_ids))
    existing["last_seen_at"] = observed_at
    occurrences[prompt_sha256] = existing


def update_prompt_hash_occurrences(
    state: dict[str, Any],
    records: list[dict[str, Any]],
    observed_at: str,
) -> None:
    occurrences = state.setdefault("prompt_hash_occurrences", {})
    if not isinstance(occurrences, dict):
        raise ValueError("state.prompt_hash_occurrences must be an object")
    for record in records:
        _record_prompt_occurrence(
            occurrences,
            prompt_sha256=str(record.get("prompt_sha256") or ""),
            upstream_id=str(record.get("upstream_id") or ""),
            observed_at=observed_at,
        )


def bootstrap_prompt_hash_occurrences(state: dict[str, Any], paths: SyncPaths) -> dict[str, Any]:
    """Seed the compact hash ledger from pending and historical review queues."""
    result = copy.deepcopy(state)
    queue_paths = sorted((paths.queue.parent / "history").glob("*.json")) if paths.queue.parent.exists() else []
    if paths.queue.exists():
        queue_paths.append(paths.queue)
    for queue_path in queue_paths:
        try:
            queue = read_json(queue_path)
        except (OSError, ValueError):
            continue
        observed_at = str(queue.get("observed_at") or "historical")
        update_prompt_hash_occurrences(result, queue.get("items", []), observed_at)
    return result


def collapse_durable_exact_duplicates(bundle: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Collapse only exact hashes previously seen under another upstream id.

    Near-duplicate and remix-family classifications remain human-reviewable.
    """
    occurrences = state.get("prompt_hash_occurrences", {})
    if not isinstance(occurrences, dict):
        raise ValueError("state.prompt_hash_occurrences must be an object")
    collapsed = copy.deepcopy(bundle)
    for record in collapsed.get("records", []):
        if record.get("dedupe", {}).get("classification") != "new":
            continue
        prompt_sha = str(record.get("prompt_sha256") or "")
        occurrence = occurrences.get(prompt_sha)
        if not isinstance(occurrence, dict):
            continue
        current_source = str(record.get("upstream_id") or "")
        prior_sources = {str(value) for value in occurrence.get("source_ids", []) if value}
        first_source = str(occurrence.get("first_upstream_id") or "")
        if not first_source or (prior_sources == {current_source} and first_source == current_source):
            continue
        record["dedupe"] = {
            "classification": "exact_duplicate",
            "auto_collapsed": True,
            "auto_merged": False,
            "matches": [
                {
                    "relation_source": "state_prompt_hash_occurrences",
                    "upstream_id": first_source,
                    "title": "Previously processed exact prompt",
                }
            ],
            "method": "durable_prompt_sha256_occurrence_index",
        }
        record["workflow_status"] = "duplicate_collapsed"
    counts = Counter(record["dedupe"]["classification"] for record in collapsed.get("records", []))
    collapsed["summary"]["classification_counts"] = dict(sorted(counts.items()))
    collapsed["summary"]["auto_collapsed"] = sum(
        bool(record["dedupe"].get("auto_collapsed")) for record in collapsed.get("records", [])
    )
    return collapsed


def chunks(values: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    if not 1 <= size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch size must be between 1 and {MAX_BATCH_SIZE}")
    return [values[index : index + size] for index in range(0, len(values), size)]


def fetch_detail_batch(
    config: dict[str, Any],
    client: SyncClient,
    selected_metadata: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    free_details: list[dict[str, Any]] = []
    locked_metadata: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    for listed in selected_metadata:
        listed_id = source_id(listed)
        slug = str(listed.get("slug") or listed_id)
        detail_url = str(config["source"]["detail_endpoint_template"]).format(
            slug=urllib.parse.quote(slug, safe="")
        )
        detail = extract_detail(client.get_json(detail_url))
        detail.setdefault("id", listed_id)
        detail.setdefault("slug", slug)
        if source_id(detail) != listed_id:
            raise RuntimeError(f"collection stopped: detail identity mismatch for {listed_id}")
        if is_paid_or_locked(detail) or not _free_list_item(detail):
            clean, removed = strip_prompt_bodies(detail)
            locked_metadata.append(clean)
            anomalies.append(
                {
                    "code": "locked_or_non_free_detail_suppressed",
                    "upstream_id": listed_id,
                    "prompt_body_removed": removed,
                }
            )
            continue
        free_details.append(detail)
    return free_details, locked_metadata, anomalies


def raw_bundle_for_batch(
    *,
    run_id: str,
    observed_at: str,
    selected_metadata: list[dict[str, Any]],
    free_details: list[dict[str, Any]],
    locked_metadata: list[dict[str, Any]],
    anomalies: list[dict[str, Any]],
    robots_policy: dict[str, Any],
    detail_requests: int,
    mode: str = "network_daily_all_free",
) -> dict[str, Any]:
    return {
        "schema_version": "opennana-raw-bundle-1.0",
        "run_id": run_id,
        "mode": mode,
        "observed_at": observed_at,
        "source": "opennana",
        "request_summary": {
            "network_requests": detail_requests,
            "selected_details": len(selected_metadata),
            "free_details": len(free_details),
            "locked_metadata_only": len(locked_metadata),
        },
        "robots_observed_sha256": robots_policy["robots_observed_sha256"],
        "robots_policy": {
            key: value for key, value in robots_policy.items() if key != "robots_observed_sha256"
        },
        "list_metadata": selected_metadata,
        "selected_list_metadata": selected_metadata,
        "free_details": free_details,
        "locked_metadata_only": locked_metadata,
        "anomalies": anomalies,
    }


def _batch_paths(paths: SyncPaths, batch_run_id: str) -> dict[str, Path]:
    return {
        "raw": paths.data_root / "raw" / f"{batch_run_id}.json",
        "normalized": paths.data_root / "staging" / f"normalized-{batch_run_id}.json",
        "dedupe": paths.data_root / "staging" / f"dedupe-{batch_run_id}.json",
        "manifest": paths.data_root / "runs" / f"batch-{batch_run_id}.json",
    }


def process_completed_batch(
    *,
    paths: SyncPaths,
    config: dict[str, Any],
    state: dict[str, Any],
    client: SyncClient,
    selected_metadata: list[dict[str, Any]],
    batch_run_id: str,
    observed_at: str,
    robots_policy: dict[str, Any],
    batch_number: int,
    run_mode: str = "network_daily_all_free",
    checkpoint_name: str = "daily_sync_checkpoint",
    checkpoint_extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Process one batch and commit its watermark only after queue merge.

    Raw/staging/queue writes are idempotent precursors. ``state.json`` is the
    authoritative completion checkpoint and is written last, together with all
    source versions from this batch. If any earlier step fails, the caller's
    in-memory state and the durable watermark remain unchanged.
    """
    before_requests = int(getattr(client, "request_count", 0))
    free_details, locked_metadata, anomalies = fetch_detail_batch(config, client, selected_metadata)
    detail_requests = int(getattr(client, "request_count", 0)) - before_requests
    raw = raw_bundle_for_batch(
        run_id=batch_run_id,
        observed_at=observed_at,
        selected_metadata=selected_metadata,
        free_details=free_details,
        locked_metadata=locked_metadata,
        anomalies=anomalies,
        robots_policy=robots_policy,
        detail_requests=detail_requests,
        mode=run_mode,
    )
    normalized = normalize_bundle(raw)
    dedupe = classify_bundle(normalized, paths.canonical)
    dedupe = collapse_durable_exact_duplicates(dedupe, state)
    previous_queue = read_json(paths.queue) if paths.queue.exists() else None
    queue, draft, projection = build_queue(dedupe, state, config, previous_queue)

    outputs = _batch_paths(paths, batch_run_id)
    atomic_write_json(outputs["raw"], raw)
    atomic_write_json(outputs["normalized"], normalized)
    atomic_write_json(outputs["dedupe"], dedupe)
    if previous_queue is not None:
        write_immutable_json(history_path_for_queue(paths.queue, previous_queue), previous_queue)
    atomic_write_json(paths.queue, queue)
    atomic_write_json(paths.draft, draft)
    atomic_write_text(paths.review_js, projection_javascript(projection))

    batch_manifest = {
        "schema_version": "opennana-daily-sync-batch-1.0",
        "run_id": batch_run_id,
        "mode": run_mode,
        "batch_number": batch_number,
        "observed_at": observed_at,
        "status": "pipeline_complete",
        "source_versions_checkpointed_by": "state.json",
        "detail_processed_versions_checkpointed_by": "state.json",
        "checkpoint_name": checkpoint_name,
        "canonical_compared": str(paths.canonical),
        "canonical_mutated": False,
        "automatic_decision_apply": False,
        "public_release_effect": False,
        "selected": len(selected_metadata),
        "free_details": len(free_details),
        "locked_metadata_only": len(locked_metadata),
        "normalized": normalized["summary"]["normalized"],
        "dedupe": dedupe["summary"],
        "queue": queue["summary"],
        "artifacts": {name: str(path) for name, path in outputs.items() if name != "manifest"},
    }
    atomic_write_json(outputs["manifest"], batch_manifest)

    next_state = copy.deepcopy(state)
    versions = dict(next_state.get("source_versions", {}))
    detail_versions = dict(next_state.get("detail_processed_versions", {}))
    for item in selected_metadata:
        upstream_id = source_id(item)
        version = metadata_version(item)
        versions[upstream_id] = version
        detail_versions[upstream_id] = version
    next_state["source_versions"] = versions
    next_state["detail_processed_versions"] = detail_versions
    update_prompt_hash_occurrences(next_state, normalized.get("records", []), observed_at)
    next_state["last_collection_run_id"] = batch_run_id
    next_state["last_observed_at"] = observed_at
    checkpoint = {
        "run_id": batch_run_id,
        "batch_number": batch_number,
        "completed_at": utc_now(),
        "selected": len(selected_metadata),
        "batch_manifest": str(outputs["manifest"]),
        "queue_revision": queue["queue_revision"],
    }
    if checkpoint_extra:
        checkpoint.update(copy.deepcopy(checkpoint_extra))
    next_state[checkpoint_name] = checkpoint
    # This is deliberately the final write in the batch. Failed normalize,
    # dedupe, queue merge, projection, or manifest writes remain retryable.
    atomic_write_json(paths.state, next_state)
    return next_state, {
        "batch_number": batch_number,
        "run_id": batch_run_id,
        "selected": len(selected_metadata),
        "free_details": len(free_details),
        "locked_metadata_only": len(locked_metadata),
        "normalized": normalized["summary"]["normalized"],
        "auto_collapsed": dedupe["summary"]["auto_collapsed"],
        "queue_total": queue["summary"]["queued"],
        "queue_new_from_run": queue["summary"]["new_from_run"],
        "checkpoint_committed": True,
    }


def _write_run_outputs(
    paths: SyncPaths,
    run_id: str,
    manifest: dict[str, Any],
    summary: dict[str, Any],
) -> tuple[Path, Path]:
    manifest_path = paths.data_root / "runs" / f"daily-sync-{run_id}.json"
    summary_path = paths.data_root / "runs" / f"daily-sync-{run_id}-summary.json"
    atomic_write_json(manifest_path, manifest)
    atomic_write_json(summary_path, summary)
    return manifest_path, summary_path


def _execute_daily_sync_unlocked(
    *,
    paths: SyncPaths,
    client_factory: ClientFactory = default_client_factory,
    batch_size: int = MAX_BATCH_SIZE,
) -> tuple[dict[str, Any], int]:
    ensure_sync_directories(paths)
    config = read_json(paths.config)
    state = bootstrap_prompt_hash_occurrences(read_json(paths.state), paths)
    validate_config(config)
    chunks([], batch_size)  # validate before network access
    started_at = utc_now()
    run_id = filesystem_run_id(started_at)
    manifest: dict[str, Any] = {
        "schema_version": "opennana-daily-sync-run-1.0",
        "run_id": run_id,
        "started_at": started_at,
        "status": "running",
        "mode": "network_daily_all_free",
        "access_type": 0,
        "page_size": PAGE_SIZE,
        "batch_size": batch_size,
        "selection_mode": "all_changed_or_new_since_baseline",
        "detail_total_cap_per_run": None,
        "requests_per_second_max": float(config["collection"]["requests_per_second"]),
        "concurrency": 1,
        "canonical_mutated": False,
        "automatic_decision_apply": False,
        "public_release_effect": False,
        "batches": [],
    }
    summary: dict[str, Any] = {
        "listed_unique": 0,
        "unchanged_excluded": 0,
        "changed_or_new": 0,
        "batches_planned": 0,
        "batches_completed": 0,
        "detail_batch_size": batch_size,
        "detail_total_cap_per_run": None,
        "free_details": 0,
        "locked_metadata_only": 0,
        "normalized": 0,
        "auto_collapsed": 0,
        "queue_new_from_run": 0,
        "resumable": True,
    }
    exit_code = 0
    error: str | None = None
    current_state = state
    try:
        require_forward_baseline(config, current_state)
        client = client_factory(config)
        robots_policy = verify_robots(config, client)
        listed, list_summary = collect_all_free_list_metadata(config, client)
        changed, unchanged_count = select_changed_metadata(listed, current_state)
        batches = chunks(changed, batch_size)
        summary.update(list_summary)
        summary.update(
            {
                "unchanged_excluded": unchanged_count,
                "changed_or_new": len(changed),
                "batches_planned": len(batches),
            }
        )
        manifest["robots_policy"] = robots_policy
        manifest["list_summary"] = list_summary
        manifest["unchanged_excluded"] = unchanged_count
        manifest["changed_or_new"] = len(changed)
        for number, batch in enumerate(batches, 1):
            observed_at = utc_now()
            batch_run_id = f"daily-{run_id}-b{number:04d}"
            current_state, batch_summary = process_completed_batch(
                paths=paths,
                config=config,
                state=current_state,
                client=client,
                selected_metadata=batch,
                batch_run_id=batch_run_id,
                observed_at=observed_at,
                robots_policy=robots_policy,
                batch_number=number,
            )
            manifest["batches"].append(batch_summary)
            summary["batches_completed"] += 1
            for key in (
                "free_details",
                "locked_metadata_only",
                "normalized",
                "auto_collapsed",
                "queue_new_from_run",
            ):
                summary[key] += int(batch_summary[key])
            # A completed-batch manifest is durable alongside the state
            # checkpoint, so an interrupted multi-hour run is inspectable.
            manifest["status"] = "running"
            manifest["last_completed_batch"] = number
            manifest["summary"] = dict(summary)
            _write_run_outputs(paths, run_id, manifest, summary)
        manifest["status"] = "passed"
    except Exception as exc:
        exit_code = 1
        error = str(exc)
        manifest["status"] = "failed"
        manifest["failed_after_completed_batches"] = summary["batches_completed"]
    manifest["finished_at"] = utc_now()
    manifest["error"] = error
    manifest["summary"] = dict(summary)
    summary["status"] = manifest["status"]
    summary["error"] = error
    summary["run_id"] = run_id
    manifest_path, summary_path = _write_run_outputs(paths, run_id, manifest, summary)

    if exit_code == 0:
        final_state = copy.deepcopy(current_state)
        final_state["last_daily_sync"] = {
            "run_id": run_id,
            "finished_at": manifest["finished_at"],
            "status": "passed",
            "listed_unique": summary["listed_unique"],
            "changed_or_new": summary["changed_or_new"],
            "batches_completed": summary["batches_completed"],
            "manifest": str(manifest_path),
            "summary": str(summary_path),
        }
        atomic_write_json(paths.state, final_state)
    result = {
        "status": manifest["status"],
        "exit_code": exit_code,
        "run_id": run_id,
        "manifest": str(manifest_path),
        "summary_path": str(summary_path),
        "summary": summary,
    }
    return result, exit_code


def execute_daily_sync(
    *,
    paths: SyncPaths,
    client_factory: ClientFactory = default_client_factory,
    batch_size: int = MAX_BATCH_SIZE,
) -> tuple[dict[str, Any], int]:
    ensure_sync_directories(paths)
    try:
        with exclusive_sync_lock(paths):
            return _execute_daily_sync_unlocked(
                paths=paths,
                client_factory=client_factory,
                batch_size=batch_size,
            )
    except SyncAlreadyRunning as exc:
        return {
            "status": "skipped_locked",
            "exit_code": 0,
            "network": False,
            "writes": False,
            "reason": str(exc),
        }, 0


def dry_run_plan(*, batch_size: int) -> dict[str, Any]:
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch size must be between 1 and {MAX_BATCH_SIZE}")
    return {
        "schema_version": "opennana-daily-sync-plan-1.0",
        "mode": "offline_dry_run",
        "network": False,
        "writes": False,
        "access_type": 0,
        "page_size": PAGE_SIZE,
        "batch_size": batch_size,
        "selection_mode": "all_changed_or_new_since_baseline",
        "detail_total_cap_per_run": None,
        "detail_requests_per_second_max": 1.0,
        "concurrency": 1,
        "pipeline": ["require_forward_baseline", "list_all_pages", "changed_details_only", "raw", "normalize", "dedupe", "merge_review_queue"],
        "baseline_pipeline": ["list_all_pages", "checkpoint_source_versions", "no_detail_reads"],
        "checkpoint": "after each completed batch only",
        "automatic_decision_apply": False,
        "canonical_mutation": False,
        "public_release_effect": False,
        "next": {
            "initialize_once": "use --fetch --apply --baseline-only to establish the forward-only inventory watermark",
            "daily": "then use --fetch --apply --all-free to collect only new or list-metadata-changed details",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run forward-only OpenNana inventory sync (dry-run by default).")
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Enable public API reads; requires --apply and one explicit live mode.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write private state/run artifacts; requires --fetch and one explicit live mode.",
    )
    live_group = parser.add_mutually_exclusive_group()
    live_group.add_argument(
        "--all-free",
        action="store_true",
        help="Traverse the free list and fetch only new/changed details; requires an established baseline.",
    )
    live_group.add_argument(
        "--baseline-only",
        action="store_true",
        help="Initialize list source/version watermarks without fetching details or changing the review queue.",
    )
    parser.add_argument("--batch-size", type=int, default=MAX_BATCH_SIZE)
    parser.add_argument("--data-root", type=Path, default=DATA_ROOT)
    parser.add_argument("--canonical", type=Path, default=DEFAULT_CANONICAL)
    parser.add_argument("--review-js", type=Path, default=LEGACY_ROOT / "opennana-review-data.js")
    args = parser.parse_args()
    try:
        mode = activation_mode(
            fetch=args.fetch,
            apply=args.apply,
            all_free=args.all_free,
            baseline_only=args.baseline_only,
        )
        if mode is None:
            print(stable_json(dry_run_plan(batch_size=args.batch_size)), end="")
            return 0
        paths = SyncPaths(data_root=args.data_root, canonical=args.canonical, review_js=args.review_js)
        if mode == "forward_baseline":
            result, exit_code = execute_forward_baseline(paths=paths)
        else:
            result, exit_code = execute_daily_sync(paths=paths, batch_size=args.batch_size)
        print(stable_json(result), end="")
        return exit_code
    except ValueError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
