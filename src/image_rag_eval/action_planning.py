from __future__ import annotations

import copy
from typing import Any

from .prompt_priority import priority_sort_key, rank_prompt


ACTION_PLAN_SCHEMA_VERSION = "image-similarity-action-plan-2"


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _priority_entry(source: dict[str, Any], item_id: str) -> dict[str, Any]:
    retention = source.get("retention") if isinstance(source.get("retention"), dict) else {}
    priority_by_id = retention.get("priority_by_id") if isinstance(retention.get("priority_by_id"), dict) else {}
    priority = priority_by_id.get(item_id)
    if isinstance(priority, dict):
        return priority
    if priority_by_id:
        raise ValueError(f"retention priority_by_id is missing item {item_id}")
    items = source.get("items") if isinstance(source.get("items"), list) else []
    fallback_index = 10**9
    for index, item in enumerate(items, start=1):
        if _text(item.get("id")).strip() == item_id:
            return {
                **rank_prompt(_text(item.get("prompt"))),
                "rank_index": fallback_index + index,
                "ordinal": index,
            }
    raise ValueError(f"unknown item id in action planning: {item_id}")


def _already_archived_lookup(source: dict[str, Any]) -> dict[str, dict[str, Any]]:
    retention = source.get("retention") if isinstance(source.get("retention"), dict) else {}
    archived = retention.get("archived") if isinstance(retention.get("archived"), list) else []
    lookup: dict[str, dict[str, Any]] = {}
    for row in archived:
        if not isinstance(row, dict):
            continue
        archived_id = _text(row.get("id")).strip()
        if archived_id:
            lookup[archived_id] = row
    return lookup


def _active_id_set(source: dict[str, Any]) -> set[str]:
    retention = source.get("retention") if isinstance(source.get("retention"), dict) else {}
    active_ids = retention.get("active_ids") if isinstance(retention.get("active_ids"), list) else []
    return {_text(item_id).strip() for item_id in active_ids if _text(item_id).strip()}


def _archived_control_entry(
    pair_id: str,
    *,
    label: str,
    action: str,
    left_id: str,
    right_id: str,
    archived_left: dict[str, Any] | None,
    archived_right: dict[str, Any] | None,
) -> dict[str, Any]:
    archived_members: list[dict[str, Any]] = []
    representative_ids: list[str] = []
    for archived_row in (archived_left, archived_right):
        if not isinstance(archived_row, dict):
            continue
        archived_id = _text(archived_row.get("id")).strip()
        representative_id = _text(archived_row.get("representative_id")).strip()
        archived_members.append(
            {
                "archived_id": archived_id,
                "representative_id": representative_id or None,
            }
        )
        if representative_id and representative_id not in representative_ids:
            representative_ids.append(representative_id)
    keep_id = None
    delete_id = None
    if archived_left and not archived_right:
        delete_id = _text(archived_left.get("id")).strip() or None
        representative_id = _text(archived_left.get("representative_id")).strip()
        if representative_id == right_id:
            keep_id = right_id
    elif archived_right and not archived_left:
        delete_id = _text(archived_right.get("id")).strip() or None
        representative_id = _text(archived_right.get("representative_id")).strip()
        if representative_id == left_id:
            keep_id = left_id
    return {
        "pair_id": pair_id,
        "label": label,
        "action": "already_archived_control",
        "selected_action": action,
        "member_ids": [left_id, right_id],
        "archived_members": archived_members,
        "representative_ids": representative_ids,
        "keep_id": keep_id,
        "delete_id": delete_id,
    }


def _pair_key(source: dict[str, Any], left_id: str, right_id: str) -> tuple[tuple[int, int, int, str], str]:
    priority = _priority_entry(source, left_id)
    rank_index = int(priority.get("rank_index", 10**9))
    sort_key = priority_sort_key(priority)
    ordinal = int(priority.get("ordinal", 10**9))
    return ((sort_key[0], sort_key[1], sort_key[2], f"{rank_index:010d}:{ordinal:010d}:{left_id}"), left_id)


def _direct_representative(source: dict[str, Any], left_id: str, right_id: str) -> tuple[str, str]:
    ordered = sorted((left_id, right_id), key=lambda item_id: _pair_key(source, item_id, right_id if item_id == left_id else left_id))
    return ordered[0], ordered[1]


def _pair_decision_rows(spec: dict[str, Any], normalized_labels: dict[str, Any]) -> dict[str, dict[str, Any]]:
    spec_pairs = {str(pair.get("pair_id")): pair for pair in spec.get("pairs", []) if isinstance(pair, dict)}
    rows: dict[str, dict[str, Any]] = {}
    for row in normalized_labels.get("pairs", []):
        if not isinstance(row, dict):
            continue
        pair_id = _text(row.get("pair_id")).strip()
        if not pair_id or pair_id not in spec_pairs:
            continue
        rows[pair_id] = {
            "label": row.get("human_label"),
            "action": row.get("action"),
            "verified": row.get("human_verified") is True,
            "reason": _text(row.get("reason")).strip(),
            "dimensions": copy.deepcopy(row.get("dimensions", {})),
            "left_id": _text(row.get("left", {}).get("id")).strip(),
            "right_id": _text(row.get("right", {}).get("id")).strip(),
        }
    return rows


def build_action_plan(source: dict[str, Any], spec: dict[str, Any], normalized_labels: dict[str, Any]) -> dict[str, Any]:
    pair_rows = _pair_decision_rows(spec, normalized_labels)
    active_ids = _active_id_set(source)
    archived_lookup = _already_archived_lookup(source)
    planned_deletions: list[dict[str, Any]] = []
    group_relations: list[dict[str, Any]] = []
    already_archived_controls: list[dict[str, Any]] = []
    kept_pairs: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    for pair_id, row in sorted(pair_rows.items()):
        if row["verified"] is not True:
            continue
        label = _text(row.get("label")).strip()
        action = _text(row.get("action")).strip()
        left_id = row["left_id"]
        right_id = row["right_id"]
        if not left_id or not right_id:
            conflicts.append({"pair_id": pair_id, "kind": "invalid_pair_binding", "message": "pair ids missing"})
            continue
        if label in {"", "unsure"}:
            continue
        if action in {"", "none"}:
            conflicts.append(
                {
                    "pair_id": pair_id,
                    "kind": "missing_action",
                    "member_ids": [left_id, right_id],
                    "label": label,
                    "message": "resolved label is missing explicit action",
                }
            )
            continue
        archived_left = archived_lookup.get(left_id)
        archived_right = archived_lookup.get(right_id)
        if archived_left or archived_right:
            already_archived_controls.append(
                _archived_control_entry(
                    pair_id,
                    label=label,
                    action=action,
                    left_id=left_id,
                    right_id=right_id,
                    archived_left=archived_left,
                    archived_right=archived_right,
                )
            )
            continue
        if action == "keep_separate":
            kept_pairs.append(
                {
                    "pair_id": pair_id,
                    "label": label,
                    "action": action,
                    "member_ids": [left_id, right_id],
                }
            )
            continue
        if action == "group_only":
            group_relations.append(
                {
                    "pair_id": pair_id,
                    "label": label,
                    "action": action,
                    "member_ids": [left_id, right_id],
                    "evidence": {
                        "dimensions": row["dimensions"],
                        "reason": row["reason"],
                    },
                }
            )
            continue
        if action != "delete_duplicate":
            conflicts.append({"pair_id": pair_id, "kind": "unknown_action", "message": f"unsupported action {action}"})
            continue

        if left_id not in active_ids or right_id not in active_ids:
            conflicts.append(
                {
                    "pair_id": pair_id,
                    "kind": "inactive_pair_member",
                    "member_ids": [left_id, right_id],
                    "message": "logical delete planning requires both pair members to be active or already archived control",
                }
            )
            continue

        keep_id, delete_id = _direct_representative(source, left_id, right_id)
        planned_deletions.append(
            {
                "pair_id": pair_id,
                "label": label,
                "action": action,
                "keep_id": keep_id,
                "delete_id": delete_id,
                "member_ids": [keep_id, delete_id],
                "reason": row["reason"],
                "dimensions": row["dimensions"],
                "selection_basis": "deterministic_json_priority_then_chronology_direct_pair_only",
            }
        )

    delete_to_keep: dict[str, str] = {}
    keep_targets = {row["keep_id"] for row in planned_deletions}
    for row in planned_deletions:
        delete_id = row["delete_id"]
        keep_id = row["keep_id"]
        previous = delete_to_keep.get(delete_id)
        if previous and previous != keep_id:
            conflicts.append(
                {
                    "pair_id": row["pair_id"],
                    "kind": "overlapping_deletion_targets",
                    "delete_id": delete_id,
                    "keep_id": keep_id,
                    "previous_keep_id": previous,
                    "message": "same delete target was assigned to multiple keep targets",
                }
            )
            continue
        delete_to_keep[delete_id] = keep_id
    for row in planned_deletions:
        if row["delete_id"] in keep_targets:
            conflicts.append(
                {
                    "pair_id": row["pair_id"],
                    "kind": "losing_keep_target_chain",
                    "delete_id": row["delete_id"],
                    "keep_id": row["keep_id"],
                    "message": "a planned delete target is also needed as another keep target",
                }
            )
    protected_members = {}
    for row in kept_pairs + group_relations:
        for member_id in row["member_ids"]:
            protected_members.setdefault(member_id, []).append(
                {"pair_id": row["pair_id"], "action": row["action"], "label": row["label"]}
            )
    for row in planned_deletions:
        protected = protected_members.get(row["delete_id"], [])
        if protected:
            conflicts.append(
                {
                    "pair_id": row["pair_id"],
                    "kind": "delete_target_has_other_resolved_relations",
                    "delete_id": row["delete_id"],
                    "keep_id": row["keep_id"],
                    "blocking_pairs": protected,
                    "message": "planned delete target also has another resolved keep/group relation",
                }
            )

    pair_decisions: list[dict[str, Any]] = []
    for pair_id, row in sorted(pair_rows.items()):
        decision: dict[str, Any] = {
            "pair_id": pair_id,
            "label": row.get("label"),
            "action": row.get("action"),
            "verified": row.get("verified"),
            "left_id": row.get("left_id"),
            "right_id": row.get("right_id"),
        }
        for planned in planned_deletions:
            if planned["pair_id"] == pair_id:
                decision["target"] = {"keep_id": planned["keep_id"], "delete_id": planned["delete_id"]}
        for planned in group_relations:
            if planned["pair_id"] == pair_id:
                decision["target"] = {"member_ids": planned["member_ids"]}
        for planned in already_archived_controls:
            if planned["pair_id"] == pair_id:
                decision["target"] = {
                    "representative_ids": planned["representative_ids"],
                    "archived_members": planned["archived_members"],
                    "keep_id": planned["keep_id"],
                    "delete_id": planned["delete_id"],
                }
        pair_decisions.append(decision)

    return {
        "schema_version": ACTION_PLAN_SCHEMA_VERSION,
        "status": "blocked" if conflicts else "action_plan_only",
        "run_id": spec.get("run_id"),
        "comparison_dir": spec.get("comparison_dir"),
        "review_spec_sha256": spec.get("review_spec_sha256"),
        "labels_sha256": None,
        "action_plan_only": True,
        "actual_deletions": 0,
        "comparison_changed": False,
        "canonical_changed": False,
        "source_mutations": False,
        "reversible": True,
        "planned_deletions": planned_deletions,
        "group_relations": group_relations,
        "already_archived_controls": already_archived_controls,
        "kept_pairs": kept_pairs,
        "pair_decisions": pair_decisions,
        "conflicts": conflicts,
        "note": "Ready-to-apply logical deletion plan only. No physical delete, canonical write, or comparison mutation was performed.",
    }
