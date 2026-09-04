from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .prompt_priority import priority_sort_key, rank_prompt
from .similarity import prompt_signals


SCHEMA_VERSION = "2"
ARRIVAL_TIMESTAMP_KEYS = ("arrival_at", "ingested_at", "first_seen_at", "collected_at")
NON_ARRIVAL_TIMESTAMP_KEYS = (
    "approved_at",
    "discovered_at",
    "observed_at",
    "updated_at",
    "reviewed_at",
    "created_at",
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _nonempty_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _as_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _source_aliases(item: dict[str, Any]) -> list[str]:
    aliases: list[str] = []
    for key in ("style_id", "catalog_key", "record_id", "source_name", "lane", "path"):
        value = _nonempty_text(item.get(key))
        if value and value not in aliases:
            aliases.append(value)
    return aliases


def _normalized_utc_timestamp(value: Any) -> str | None:
    text = _nonempty_text(value)
    if text is None:
        return None
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    normalized = parsed.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z")


def _arrival_fields(item: dict[str, Any], index: int) -> dict[str, Any]:
    non_arrival_timestamps = {
        key: value
        for key in NON_ARRIVAL_TIMESTAMP_KEYS
        if (value := _nonempty_text(item.get(key))) is not None
    }
    explicit_basis = _nonempty_text(item.get("arrival_basis"))
    run_ids = {
        key: value
        for key in ("first_queued_run_id", "queued_from_run_id", "last_seen_run_id")
        if (value := _nonempty_text(item.get(key))) is not None
    }

    arrival_at = "unknown"
    arrival_basis = None
    invalid_arrival_sources: list[str] = []
    for key in ARRIVAL_TIMESTAMP_KEYS:
        raw_value = item.get(key)
        if _nonempty_text(raw_value) is None:
            continue
        normalized = _normalized_utc_timestamp(raw_value)
        if normalized is not None:
            arrival_at = normalized
            arrival_basis = explicit_basis or f"item.{key}" if key == "arrival_at" else f"item.{key}"
            break
        invalid_arrival_sources.append(key)

    if arrival_basis is None:
        if invalid_arrival_sources:
            arrival_basis = "fallback_invalid_arrival_timestamp"
        elif run_ids:
            arrival_basis = "fallback_no_explicit_arrival_timestamp_run_ids_only"
        else:
            arrival_basis = "fallback_no_explicit_arrival_evidence"

    explicit_ordinal = _as_positive_int(item.get("ordinal"))
    if explicit_ordinal is not None:
        ordinal = explicit_ordinal
        ordinal_basis = "item.ordinal"
    else:
        ordinal = index + 1
        ordinal_basis = "fallback_input_order"

    return {
        "arrival_at": arrival_at,
        "arrival_basis": arrival_basis,
        "ordinal": ordinal,
        "ordinal_basis": ordinal_basis,
        "run_ids": run_ids,
        "non_arrival_timestamps": non_arrival_timestamps,
        "invalid_arrival_sources": invalid_arrival_sources,
    }


def _normalized_item(item: dict[str, Any], index: int) -> dict[str, Any]:
    item_id = _nonempty_text(item.get("id"))
    if item_id is None:
        raise ValueError("each item must include a non-empty id")

    signals = item.get("signals") if isinstance(item.get("signals"), dict) else {}
    prompt = item.get("prompt") if isinstance(item.get("prompt"), str) else ""
    prompt_info = prompt_signals(prompt)
    prompt_priority = rank_prompt(prompt)
    arrival = _arrival_fields(item, index)
    return {
        "id": item_id,
        "index": index,
        "prompt": prompt,
        "prompt_priority": prompt_priority,
        "source_aliases": _source_aliases(item),
        "arrival_at": arrival["arrival_at"],
        "arrival_basis": arrival["arrival_basis"],
        "ordinal": arrival["ordinal"],
        "ordinal_basis": arrival["ordinal_basis"],
        "run_ids": arrival["run_ids"],
        "non_arrival_timestamps": arrival["non_arrival_timestamps"],
        "invalid_arrival_sources": arrival["invalid_arrival_sources"],
        "file_sha256": _nonempty_text(signals.get("sha256")) or _nonempty_text(item.get("sha256")),
        "pixel_sha256": _nonempty_text(signals.get("pixel_sha256")) or _nonempty_text(item.get("pixel_sha256")),
        "prompt_exact_sha256": prompt_info["exact_sha256"] if prompt_info["has_text"] else None,
    }


def _order_key(item: dict[str, Any]) -> tuple[int, str, int, str]:
    unknown = item["arrival_at"] == "unknown"
    return (1 if unknown else 0, item["arrival_at"] if not unknown else "", item["ordinal"], item["id"])


def _representative_key(item: dict[str, Any]) -> tuple[int, int, int, int, str, int, str]:
    return (*priority_sort_key(item["prompt_priority"]), *_order_key(item))


def _pair_match(record: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    kinds: list[str] = []
    evidence: dict[str, str] = {}
    file_sha = record.get("file_sha256")
    candidate_file_sha = candidate.get("file_sha256")
    if file_sha and file_sha == candidate_file_sha:
        kinds.append("exact_file")
        evidence["file_sha256"] = str(file_sha)
    pixel_sha = record.get("pixel_sha256")
    candidate_pixel_sha = candidate.get("pixel_sha256")
    if pixel_sha and pixel_sha == candidate_pixel_sha:
        kinds.append("exact_pixels")
        evidence["pixel_sha256"] = str(pixel_sha)
    prompt_sha = record.get("prompt_exact_sha256")
    candidate_prompt_sha = candidate.get("prompt_exact_sha256")
    if prompt_sha and prompt_sha == candidate_prompt_sha:
        kinds.append("prompt_exact")
        evidence["prompt_exact_sha256"] = str(prompt_sha)
    return {
        "match_types": sorted(kinds),
        "evidence": evidence,
        "eligible": "exact_file" in kinds or ("exact_pixels" in kinds and "prompt_exact" in kinds),
    }


def _prompt_variant_groups(active_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in active_records:
        prompt_sha = record.get("prompt_exact_sha256")
        if isinstance(prompt_sha, str) and prompt_sha:
            grouped[prompt_sha].append(record)

    groups: list[dict[str, Any]] = []
    for prompt_sha, members in sorted(grouped.items()):
        if len(members) < 2:
            continue
        ordered_members = sorted(members, key=_representative_key)
        groups.append(
            {
                "group_id": f"prompt-variant-{_sha256_text(prompt_sha)[:24]}",
                "kind": "prompt_variant",
                "representative_id": ordered_members[0]["id"],
                "member_ids": [member["id"] for member in ordered_members],
                "evidence": {
                    "prompt_exact_sha256": prompt_sha,
                    "basis": "raw_nonempty_prompt_equality_active_only",
                },
            }
        )
    return groups


def build_retention(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(items, list):
        raise ValueError("items must be a list")

    records = [_normalized_item(item, index) for index, item in enumerate(items)]
    ids = [record["id"] for record in records]
    if len(set(ids)) != len(ids):
        raise ValueError("item ids must be unique")

    records_by_id = {record["id"]: record for record in records}
    priority_order = sorted(records, key=_representative_key)
    rank_index_by_id = {record["id"]: index + 1 for index, record in enumerate(priority_order)}
    active_records: list[dict[str, Any]] = []
    archived_rows: list[dict[str, Any]] = []

    for record in priority_order:
        matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for candidate in active_records:
            match = _pair_match(record, candidate)
            if match["eligible"]:
                matches.append((candidate, match))
        if not matches:
            active_records.append(record)
            continue
        representative, match = min(matches, key=lambda item: (_representative_key(item[0]), item[0]["id"]))
        archived_rows.append(
            {
                "id": record["id"],
                "representative_id": representative["id"],
                "action": "logical_delete",
                "reasons": [
                    {
                        "kind": "direct_delete_eligible_match",
                        "matched_to_id": representative["id"],
                        "match_types": match["match_types"],
                        "evidence": match["evidence"],
                        "matched_to_source_aliases": representative["source_aliases"],
                        "basis": "exact_file_or_exact_pixels_plus_prompt_exact_same_pair",
                    }
                ],
            }
        )

    archived_by_id = {row["id"]: row for row in archived_rows}
    active_ids = [record["id"] for record in priority_order if record["id"] not in archived_by_id]
    archived = [archived_by_id[record["id"]] for record in priority_order if record["id"] in archived_by_id]

    exact_group_members: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in archived:
        exact_group_members[row["representative_id"]].append(row)

    exact_groups: list[dict[str, Any]] = []
    for representative_id, members in sorted(exact_group_members.items(), key=lambda item: _representative_key(records_by_id[item[0]])):
        ordered_archived = sorted(members, key=lambda row: _representative_key(records_by_id[row["id"]]))
        exact_groups.append(
            {
                "group_id": f"exact-logical-delete-{_sha256_text(representative_id)[:24]}",
                "kind": "exact_logical_delete",
                "representative_id": representative_id,
                "member_ids": [representative_id] + [row["id"] for row in ordered_archived],
                "action": "logical_delete",
                "evidence": {
                    "basis": "exact_file_or_exact_pixels_plus_prompt_exact_same_pair",
                    "pairs": [
                        {
                            "id": row["id"],
                            "match_types": row["reasons"][0]["match_types"],
                            "evidence": row["reasons"][0]["evidence"],
                        }
                        for row in ordered_archived
                    ],
                },
            }
        )

    prompt_variant_groups = _prompt_variant_groups([records_by_id[item_id] for item_id in active_ids])

    order_evidence = [
        {
            "id": record["id"],
            "arrival_at": record["arrival_at"],
            "arrival_basis": record["arrival_basis"],
            "ordinal": record["ordinal"],
            "ordinal_basis": record["ordinal_basis"],
            "run_ids": record["run_ids"],
            "non_arrival_timestamps": record["non_arrival_timestamps"],
            "invalid_arrival_sources": record["invalid_arrival_sources"],
            "source_aliases": record["source_aliases"],
        }
        for record in sorted(records, key=_order_key)
    ]
    priority_by_id = {
        record["id"]: {
            **record["prompt_priority"],
            "rank_index": rank_index_by_id[record["id"]],
            "tier": record["prompt_priority"]["tier"],
            "label": record["prompt_priority"]["label"],
            "reason": record["prompt_priority"]["reason"],
            "parse_status": record["prompt_priority"]["parse_status"],
            "ordinal": record["ordinal"],
            "representative_id": archived_by_id[record["id"]]["representative_id"] if record["id"] in archived_by_id else record["id"],
            "selected_as_representative": record["id"] not in archived_by_id,
        }
        for record in priority_order
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "policy": "logical_delete_exact_duplicate_retention_view_v2",
        "selection_mode": "greedy_priority_then_chronology_direct_active_only",
        "reversible": True,
        "source_mutations": False,
        "active_ids": active_ids,
        "archived": archived,
        "deleted_ids": [row["id"] for row in archived],
        "exact_groups": exact_groups,
        "prompt_variant_groups": prompt_variant_groups,
        "priority_by_id": priority_by_id,
        "order_evidence": order_evidence,
    }
