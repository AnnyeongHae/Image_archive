from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from .common import DATA_ROOT, LEGACY_ROOT, atomic_write_json, atomic_write_text, read_json, sha256_text, stable_json
except ImportError:
    from common import DATA_ROOT, LEGACY_ROOT, atomic_write_json, atomic_write_text, read_json, sha256_text, stable_json


QUEUEABLE = {"new", "same_source_update", "near_duplicate", "remix_family"}


def decision_record(state: dict[str, Any], upstream_id: str, content_sha256: str) -> dict[str, Any] | None:
    """Return a durable decision for this exact source version, if one exists."""
    by_source = state.get("review_decision_ledger", {}).get(str(upstream_id), {})
    record = by_source.get(content_sha256) if isinstance(by_source, dict) else None
    return record if isinstance(record, dict) else None


def suppression_reason(state: dict[str, Any], upstream_id: str, content_sha256: str) -> str | None:
    record = decision_record(state, upstream_id, content_sha256)
    if record:
        action = str(record.get("decision") or "decided")
        return f"unchanged_decision_{action}"
    # Compatibility with states written before the durable decision ledger.
    if state.get("rejected_content_hashes", {}).get(str(upstream_id)) == content_sha256:
        return "unchanged_rejection"
    return None


def queue_revision_for_items(items: list[dict[str, Any]]) -> str:
    revision_basis = [
        {"queue_id": item["queue_id"], "content_sha256": item["content_sha256"]}
        for item in items
    ]
    return sha256_text(stable_json(revision_basis, indent=None))


def decision_draft_for_queue(queue: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "opennana-decision-draft-1.0",
        "run_id": queue["run_id"],
        "queue_revision": queue["queue_revision"],
        "created_from_observed_at": queue["observed_at"],
        "instructions": "Change decision from pending to approve, defer, reject, or group. Approval only creates canonicalization_pending; it is not publication approval.",
        "decisions": [
            {
                "queue_id": item["queue_id"],
                "upstream_id": item["upstream_id"],
                "content_sha256": item["content_sha256"],
                "decision": "pending",
                "group_with": None,
                "note": "",
            }
            for item in queue["items"]
        ],
    }


def history_path_for_queue(queue_path: Path, queue: dict[str, Any]) -> Path:
    run_id = re.sub(r"[^A-Za-z0-9._-]+", "-", str(queue.get("run_id") or "unknown-run"))
    revision = str(queue.get("queue_revision") or "no-revision")[:16]
    return queue_path.parent / "history" / f"{run_id}--{revision}.json"


def write_immutable_json(path: Path, value: dict[str, Any]) -> bool:
    """Write once; refuse to replace a different immutable snapshot."""
    if path.exists():
        if stable_json(read_json(path)) != stable_json(value):
            raise ValueError(f"immutable history collision: {path}")
        return False
    atomic_write_json(path, value)
    return True


def queue_id(record: dict[str, Any]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(record["slug"]).casefold()).strip("-")[:36] or "record"
    return f"ONN-{slug}-{record['content_sha256'][:12]}"


def compact_match(match: dict[str, Any]) -> dict[str, Any]:
    return {
        key: match.get(key)
        for key in ("relation_source", "catalog_key", "record_id", "style_id", "upstream_id", "title", "source_url", "similarity")
        if match.get(key) is not None
    }


def build_projection(queue: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    items: list[dict[str, Any]] = []
    for item in queue["items"]:
        rights = item.get("rights", {})
        images = item.get("image_urls", [])
        items.append({
            **item,
            "source_id": config.get("source_id") or "opennana-awesome-prompt-gallery",
            "style_id": item.get("style_id") or item["upstream_id"],
            "preview_image_url": images[0] if images else None,
            "preview_images": [],
            "rights": {
                **rights,
                "commercial_gate": "review_required",
                "license_spdx": None,
                "reuse_status": rights.get("item_rights", "unverified"),
                "usage_note": "Rights and release review remain open.",
            },
            "safety": {
                "human_subject_risk": "unknown",
                "brand_risk": "unknown",
                "mature_risk": "unknown",
                "notes": [],
            },
            "reviewed_at": item.get("updated_at"),
            "discovered_at": queue["observed_at"],
        })
    return {
        "schema_version": "opennana-review-dashboard-1.0",
        "generated_at": queue["observed_at"],
        "source": {
            "source_id": config.get("source_id") or "opennana-awesome-prompt-gallery",
            "source_name": config.get("source_name") or "OpenNana Awesome Prompt Gallery",
            "source_url": config.get("source_url") or "https://opennana.com/awesome-prompt-gallery",
            "collection_policy": {
                "canary_max_details": config.get("collection", {}).get("canary_max_details", 20),
                "requests_per_second": config.get("collection", {}).get("requests_per_second", 1.0),
                "concurrency": config.get("collection", {}).get("concurrency", 1),
                "daily_sync": {
                    key: config.get("daily_sync", {}).get(key)
                    for key in (
                        "enabled",
                        "interval_hours",
                        "mode",
                        "historical_backfill",
                        "baseline_strategy",
                        "fetch_details_for",
                        "selection_mode",
                        "list_page_size",
                        "detail_batch_size",
                        "detail_total_cap_per_run",
                        "skip_same_source_unchanged",
                        "collapse_exact_prompt_duplicates",
                        "retain_near_and_remix_as_relationship_candidates",
                    )
                },
                "paid_prompt_body": config.get("policy", {}).get("paid_prompt_body", "forbidden"),
                "download_source_images": config.get("policy", {}).get("download_source_images", False),
                "auto_publish": config.get("policy", {}).get("auto_publish", False),
            },
        },
        "queue_revision": queue["queue_revision"],
        "review_boundary": "browser_draft_only_not_rights_or_release_approval",
        "summary": {
            "total_items": len(items),
            "with_preview_count": sum(bool(item["preview_image_url"]) for item in items),
            "without_preview_count": sum(not item["preview_image_url"] for item in items),
            "workflow_status_counts": {"queued_for_review": len(items)},
            "dedupe_classification_counts": queue["summary"]["classification_counts"],
            # Unknown is deliberately review-required. A missing classifier is
            # never presented as a clean safety signal.
            "safety_review_count": sum(
                any(
                    item["safety"].get(key) not in {"clear", "not_detected"}
                    for key in ("human_subject_risk", "brand_risk", "mature_risk")
                )
                for item in items
            ),
        },
        "source_files": ["data/private-research/opennana/review_queue/current.json"],
        "decision_contract": {
            "schema_version": "opennana-decision-draft-1.0",
            "allowed": ["approve", "defer", "reject", "group"],
            "approval_effect": "canonicalization_pending",
            "public_release_effect": False,
        },
        "items": items,
    }


def projection_javascript(projection: dict[str, Any]) -> str:
    return (
        "// Generated from data/private-research/opennana/review_queue/current.json. Do not edit.\n"
        "window.OPENNANA_REVIEW_QUEUE = "
        + stable_json(projection, indent=None).strip()
        + ";\n"
    )


def build_queue(
    bundle: dict[str, Any],
    state: dict[str, Any],
    config: dict[str, Any] | None = None,
    previous_queue: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Build a queue without losing unresolved items from the previous batch.

    An explicit durable decision suppresses only the exact content hash that was
    reviewed. A changed upstream record is eligible for review again. Pending
    items have no state-ledger entry and therefore remain active.
    """
    previous_queue = previous_queue or {"items": []}
    previous_by_source: dict[str, dict[str, Any]] = {}
    suppression_counts: Counter[str] = Counter()
    for previous in previous_queue.get("items", []):
        upstream_id = str(previous.get("upstream_id") or "")
        content_sha256 = str(previous.get("content_sha256") or "")
        if not upstream_id or not content_sha256:
            suppression_counts["invalid_previous_item"] += 1
            continue
        reason = suppression_reason(state, upstream_id, content_sha256)
        if reason:
            suppression_counts[reason] += 1
            continue
        retained = dict(previous)
        retained.setdefault("first_queued_run_id", previous_queue.get("run_id"))
        retained.setdefault("queued_from_run_id", previous_queue.get("run_id"))
        retained["workflow_status"] = "queued_for_review"
        previous_by_source[upstream_id] = retained

    incoming_by_source: dict[str, dict[str, Any]] = {}
    for record in bundle.get("records", []):
        classification = record["dedupe"]["classification"]
        if classification not in QUEUEABLE:
            suppression_counts[classification] += 1
            continue
        upstream_id = str(record["upstream_id"])
        content_sha256 = record["content_sha256"]
        reason = suppression_reason(state, upstream_id, content_sha256)
        if reason:
            suppression_counts[reason] += 1
            continue
        prompt_text = record["prompt_text"]
        previous = previous_by_source.get(upstream_id)
        item = {
            "queue_id": queue_id(record),
            "source": "opennana",
            "upstream_id": upstream_id,
            "slug": record["slug"],
            "title": record["title"],
            "source_url": record["source_url"],
            "author": record.get("author"),
            "model": record.get("model"),
            "tags": record.get("tags", []),
            "media_type": record.get("media_type", "image"),
            "image_urls": record.get("image_urls", []),
            "prompt_text": prompt_text,
            "prompt_preview": prompt_text[:500] + ("…" if len(prompt_text) > 500 else ""),
            "prompt_sha256": record["prompt_sha256"],
            "content_sha256": content_sha256,
            "updated_at": record.get("updated_at"),
            "dedupe": {
                "classification": classification,
                "auto_collapsed": False,
                "auto_merged": False,
                "matches": [compact_match(match) for match in record["dedupe"].get("matches", [])],
            },
            "previous_rejection_content_changed": bool(
                state.get("rejected_content_hashes", {}).get(upstream_id)
                and state.get("rejected_content_hashes", {}).get(upstream_id) != content_sha256
            ),
            "rights": record["rights"],
            "workflow_status": "queued_for_review",
            "available_decisions": ["approve", "defer", "reject", "group"],
            "first_queued_run_id": (
                previous.get("first_queued_run_id") or previous_queue.get("run_id")
                if previous
                else bundle["run_id"]
            ),
            "queued_from_run_id": bundle["run_id"],
            "last_seen_run_id": bundle["run_id"],
        }
        incoming_by_source[upstream_id] = item

    retained_count = 0
    replaced_count = 0
    merged_by_source = dict(previous_by_source)
    for upstream_id, item in incoming_by_source.items():
        previous = merged_by_source.get(upstream_id)
        if previous and previous.get("content_sha256") == item["content_sha256"]:
            retained_count += 1
        elif previous:
            replaced_count += 1
            suppression_counts["superseded_pending_version"] += 1
        merged_by_source[upstream_id] = item

    items = list(merged_by_source.values())
    items.sort(key=lambda item: (item["queue_id"], item["content_sha256"]))
    queue_revision = queue_revision_for_items(items)
    classification_counts = Counter(item["dedupe"]["classification"] for item in items)
    queue = {
        "schema_version": "opennana-review-queue-1.0",
        "run_id": bundle["run_id"],
        "observed_at": bundle["observed_at"],
        "queue_revision": queue_revision,
        "source_dedupe": f"staging/dedupe-{bundle['run_id']}.json",
        "summary": {
            "queued": len(items),
            "new_from_run": sum(upstream_id not in previous_by_source for upstream_id in incoming_by_source),
            "retained_pending": len(previous_by_source) - replaced_count,
            "same_version_refreshed": retained_count,
            "superseded_pending": replaced_count,
            "classification_counts": dict(sorted(classification_counts.items())),
            "suppression_counts": dict(sorted(suppression_counts.items())),
            "approval_is_public_release": False,
        },
        "items": items,
    }
    draft = decision_draft_for_queue(queue)
    projection = build_projection(queue, config)
    return queue, draft, projection


def main() -> int:
    parser = argparse.ArgumentParser(description="Build deterministic private OpenNana review artifacts (dry-run by default).")
    parser.add_argument("--input", type=Path, default=DATA_ROOT / "staging" / "dedupe-sample-canary-v1.json")
    parser.add_argument("--state", type=Path, default=DATA_ROOT / "state.json")
    parser.add_argument("--config", type=Path, default=DATA_ROOT / "config.json")
    parser.add_argument("--queue-output", type=Path, default=DATA_ROOT / "review_queue" / "current.json")
    parser.add_argument("--draft-output", type=Path, default=DATA_ROOT / "decisions" / "decision-draft.json")
    parser.add_argument("--js-output", type=Path, default=LEGACY_ROOT / "opennana-review-data.js")
    parser.add_argument("--no-js", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    previous_queue = read_json(args.queue_output) if args.queue_output.exists() else None
    queue, draft, projection = build_queue(
        read_json(args.input),
        read_json(args.state),
        read_json(args.config),
        previous_queue,
    )
    outputs = [args.queue_output, args.draft_output] + ([] if args.no_js else [args.js_output])
    if args.apply:
        history_written: list[str] = []
        if previous_queue is not None:
            history_path = history_path_for_queue(args.queue_output, previous_queue)
            if write_immutable_json(history_path, previous_queue):
                history_written.append(str(history_path))
        atomic_write_json(args.queue_output, queue)
        atomic_write_json(args.draft_output, draft)
        if not args.no_js:
            atomic_write_text(args.js_output, projection_javascript(projection))
        print(stable_json({"written": history_written + [str(path) for path in outputs], "summary": queue["summary"]}), end="")
    else:
        print(stable_json({"writes": False, "would_write": [str(path) for path in outputs], "summary": queue["summary"]}), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
