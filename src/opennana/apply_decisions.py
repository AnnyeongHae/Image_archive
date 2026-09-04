from __future__ import annotations

import argparse
import copy
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from .build_review_queue import (
        build_projection,
        decision_draft_for_queue,
        history_path_for_queue,
        projection_javascript,
        queue_revision_for_items,
        write_immutable_json,
    )
    from .common import DATA_ROOT, LEGACY_ROOT, atomic_write_json, atomic_write_text, read_json, stable_json
except ImportError:
    from build_review_queue import (
        build_projection,
        decision_draft_for_queue,
        history_path_for_queue,
        projection_javascript,
        queue_revision_for_items,
        write_immutable_json,
    )
    from common import DATA_ROOT, LEGACY_ROOT, atomic_write_json, atomic_write_text, read_json, stable_json


ALLOWED = {"approve", "defer", "reject", "group"}


def remaining_queue_after_decisions(queue: dict[str, Any], applied: dict[str, Any]) -> dict[str, Any]:
    """Remove only explicit decisions; unchecked/pending rows remain active."""
    decided_ids = {item["queue_id"] for item in applied.get("decisions", [])}
    items = [copy.deepcopy(item) for item in queue.get("items", []) if item["queue_id"] not in decided_ids]
    items.sort(key=lambda item: (item["queue_id"], item["content_sha256"]))
    summary = copy.deepcopy(queue.get("summary", {}))
    summary["queued"] = len(items)
    summary["decision_counts_applied"] = copy.deepcopy(applied.get("summary", {}))
    summary["removed_by_decision"] = len(decided_ids)
    classification_counts = Counter(item.get("dedupe", {}).get("classification", "unknown") for item in items)
    summary["classification_counts"] = dict(sorted(classification_counts.items()))
    remaining = copy.deepcopy(queue)
    remaining["parent_queue_revision"] = queue["queue_revision"]
    remaining["queue_revision"] = queue_revision_for_items(items)
    remaining["items"] = items
    remaining["summary"] = summary
    remaining["last_decided_at"] = applied["decided_at"]
    return remaining


def validate_and_apply(queue: dict[str, Any], decisions: dict[str, Any], state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if decisions.get("schema_version") not in {None, "opennana-decision-draft-1.0"}:
        raise ValueError("unsupported OpenNana decision draft schema")
    if decisions.get("queue_revision") != queue.get("queue_revision"):
        raise ValueError("decision draft queue_revision does not match current review queue")
    if decisions.get("run_id") not in {None, queue.get("run_id")}:
        raise ValueError("decision draft run_id does not match current review queue")
    if decisions.get("decision_count") is not None and decisions.get("decision_count") != len(decisions.get("decisions", [])):
        raise ValueError("decision_count does not match decisions length")
    queue_by_id = {item["queue_id"]: item for item in queue.get("items", [])}
    if len(queue_by_id) != len(queue.get("items", [])):
        raise ValueError("review queue contains duplicate queue_id values")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    pending_records: list[dict[str, Any]] = []
    rejected = dict(state.get("rejected_content_hashes", {}))
    ledger = copy.deepcopy(state.get("review_decision_ledger", {}))
    counts: Counter[str] = Counter()
    decided_at = decisions.get("decided_at") or queue["observed_at"]
    for decision in decisions.get("decisions", []):
        queue_id = decision.get("queue_id")
        action = decision.get("decision")
        if action == "pending":
            continue
        if queue_id in seen:
            raise ValueError(f"duplicate decision for {queue_id}")
        seen.add(queue_id)
        if queue_id not in queue_by_id:
            raise ValueError(f"decision references unknown queue_id: {queue_id}")
        if action not in ALLOWED:
            raise ValueError(f"unsupported decision {action!r} for {queue_id}")
        item = queue_by_id[queue_id]
        if decision.get("content_sha256") != item["content_sha256"]:
            raise ValueError(f"stale content hash for {queue_id}")
        existing = ledger.get(str(item["upstream_id"]), {}).get(item["content_sha256"])
        if existing:
            raise ValueError(f"content version already has a durable decision: {queue_id}")
        group_with = decision.get("group_with")
        if action == "group" and (not isinstance(group_with, str) or not group_with.strip()):
            raise ValueError(f"group decision requires group_with for {queue_id}")
        normalized_decision = {
            "queue_id": queue_id,
            "upstream_id": item["upstream_id"],
            "content_sha256": item["content_sha256"],
            "decision": action,
            "group_with": group_with if action == "group" else None,
            "note": str(decision.get("note") or ""),
        }
        normalized.append(normalized_decision)
        counts[action] += 1
        upstream_ledger = ledger.setdefault(str(item["upstream_id"]), {})
        upstream_ledger[item["content_sha256"]] = {
            **normalized_decision,
            "run_id": queue["run_id"],
            "queue_revision": queue["queue_revision"],
            "decided_at": decided_at,
        }
        if action == "reject":
            rejected[str(item["upstream_id"])] = item["content_sha256"]
        elif action in {"approve", "group"}:
            promoted = dict(item)
            promoted["workflow_status"] = "canonicalization_pending"
            promoted["human_decision"] = normalized_decision
            promoted["rights"] = {
                **promoted["rights"],
                "release_eligible": False,
                "item_rights": "unverified",
            }
            pending_records.append(promoted)
    normalized.sort(key=lambda item: item["queue_id"])
    pending_records.sort(key=lambda item: item["queue_id"])
    applied = {
        "schema_version": "opennana-applied-decisions-1.0",
        "run_id": queue["run_id"],
        "queue_revision": queue["queue_revision"],
        "decided_at": decided_at,
        "summary": dict(sorted(counts.items())),
        "decisions": normalized,
    }
    canonicalization_pending = {
        "schema_version": "opennana-canonicalization-pending-1.0",
        "run_id": queue["run_id"],
        "queue_revision": queue["queue_revision"],
        "public_release_eligible": False,
        "record_count": len(pending_records),
        "records": pending_records,
    }
    next_state = dict(state)
    next_state["rejected_content_hashes"] = dict(sorted(rejected.items()))
    next_state["review_decision_ledger"] = {
        upstream_id: dict(sorted(by_hash.items()))
        for upstream_id, by_hash in sorted(ledger.items())
    }
    cumulative_counts = Counter(state.get("review_decision_counts", {}))
    cumulative_counts.update(counts)
    next_state["review_decision_counts"] = dict(sorted(cumulative_counts.items()))
    next_state["last_decision_run_id"] = queue["run_id"]
    next_state["last_decision_export_at"] = decided_at
    next_state["last_applied_run_id"] = queue["run_id"]
    return applied, canonicalization_pending, next_state


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply a human OpenNana decision draft to private staging only (dry-run by default).")
    parser.add_argument("--queue", type=Path, default=DATA_ROOT / "review_queue" / "current.json")
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--state", type=Path, default=DATA_ROOT / "state.json")
    parser.add_argument("--config", type=Path, default=DATA_ROOT / "config.json")
    parser.add_argument("--draft-output", type=Path, default=DATA_ROOT / "decisions" / "decision-draft.json")
    parser.add_argument("--js-output", type=Path, default=LEGACY_ROOT / "opennana-review-data.js")
    parser.add_argument("--applied-output", type=Path)
    parser.add_argument("--pending-output", type=Path)
    parser.add_argument("--remaining-output", type=Path)
    parser.add_argument("--no-js", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    queue = read_json(args.queue)
    applied, pending, next_state = validate_and_apply(queue, read_json(args.decisions), read_json(args.state))
    if not applied["decisions"]:
        print(stable_json({
            "writes": False,
            "reason": "no_explicit_decisions",
            "summary": {},
            "remaining_queued": len(queue.get("items", [])),
            "remaining_queue_revision": queue["queue_revision"],
        }), end="")
        return 0
    revision_suffix = queue["queue_revision"][:16]
    applied_path = args.applied_output or DATA_ROOT / "decisions" / f"applied-{queue['run_id']}--{revision_suffix}.json"
    pending_path = args.pending_output or DATA_ROOT / "staging" / f"canonicalization-pending-{queue['run_id']}--{revision_suffix}.json"
    remaining_path = args.remaining_output or args.queue.parent / f"remaining-{queue['run_id']}--{revision_suffix}.json"
    history_path = history_path_for_queue(args.queue, queue)
    remaining = remaining_queue_after_decisions(queue, applied)
    remaining_draft = decision_draft_for_queue(remaining)
    remaining_projection = build_projection(remaining, read_json(args.config))
    mutable_outputs = [args.queue, args.draft_output] + ([] if args.no_js else [args.js_output])
    immutable_outputs = [history_path, applied_path, pending_path, remaining_path]
    if args.apply:
        written: list[str] = []
        for path, value in (
            (history_path, queue),
            (applied_path, applied),
            (pending_path, pending),
            (remaining_path, remaining),
        ):
            if write_immutable_json(path, value):
                written.append(str(path))
        atomic_write_json(args.state, next_state)
        atomic_write_json(args.queue, remaining)
        atomic_write_json(args.draft_output, remaining_draft)
        if not args.no_js:
            atomic_write_text(args.js_output, projection_javascript(remaining_projection))
        written.extend([str(args.state), *[str(path) for path in mutable_outputs]])
        print(stable_json({
            "written": written,
            "summary": applied["summary"],
            "remaining_queued": len(remaining["items"]),
            "remaining_queue_revision": remaining["queue_revision"],
        }), end="")
    else:
        print(stable_json({
            "writes": False,
            "would_write": [*[str(path) for path in immutable_outputs], str(args.state), *[str(path) for path in mutable_outputs]],
            "summary": applied["summary"],
            "canonicalization_pending": pending["record_count"],
            "remaining_queued": len(remaining["items"]),
            "remaining_queue_revision": remaining["queue_revision"],
        }), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
