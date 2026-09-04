from __future__ import annotations

import copy
import html
import json
import os
import re
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any

from .calibration import calibrate
from .experiment import digest, json_bytes, now, read_json, run_lock, run_path, write_json
from .group_review_ui import render_group_review as render_group_review_ui
from .human_review import DEFAULT_COMPARISON_DIR, REVIEW_LABELS_SCHEMA_VERSION, load_review_source, validate_review_labels
from .label_import import IMPORT_ROOT, load_bound_review_spec, load_bound_review_spec_v2, validate_review_labels_v2
from .machine_dedupe import build_machine_retention
from .prompt_priority import priority_sort_key
from .similarity import build_visual_families, prompt_signals


GROUP_WORKFLOW_DIR = "group-workflow-v1"
GROUP_WORKFLOW_SPEC_FILENAME = "image-group-workflow.spec.json"
GROUP_WORKFLOW_TEMPLATE_FILENAME = "image-group-workflow.template.json"
GROUP_WORKFLOW_SUMMARY_FILENAME = "image-group-workflow-summary.json"
GROUP_WORKFLOW_BUILD_RECEIPT_FILENAME = "image-group-workflow-build-receipt.json"
GROUP_WORKFLOW_HTML_FILENAME = "image-group-workflow.html"
GROUP_WORKFLOW_DECISION_IMPORTS_DIR = "decision-imports"
GROUP_WORKFLOW_DECISIONS_FILENAME = "decisions.json"
GROUP_WORKFLOW_DECISION_SUMMARY_FILENAME = "summary.json"
GROUP_WORKFLOW_RECEIPT_FILENAME = "receipt.json"
GROUP_WORKFLOW_RETENTION_OVERLAY_FILENAME = "retention-overlay.json"
GROUP_WORKFLOW_APPROVED_GROUPS_FILENAME = "approved-groups.json"
GROUP_WORKFLOW_FRONT_EXPORT_FILENAME = "private-front-export.json"
GROUP_WORKFLOW_SPEC_SCHEMA_VERSION = "image-group-workflow-spec-1"
GROUP_WORKFLOW_DECISIONS_SCHEMA_VERSION = "image-group-workflow-decisions-1"
GROUP_WORKFLOW_DECISIONS_V2_SCHEMA_VERSION = "image-group-workflow-decisions-2"
GROUP_WORKFLOW_DECISIONS_V3_SCHEMA_VERSION = "image-group-workflow-decisions-3"
DEFAULT_IMAGE_APPROVAL_POLICY = "default_retained_images_after_review_v1"
GROUP_WORKFLOW_SUMMARY_SCHEMA_VERSION = "image-group-workflow-summary-1"
GROUP_WORKFLOW_DECISION_SUMMARY_SCHEMA_VERSION = "image-group-workflow-decision-summary-1"
GROUP_WORKFLOW_RETENTION_OVERLAY_SCHEMA_VERSION = "image-group-workflow-retention-overlay-1"
GROUP_WORKFLOW_APPROVED_GROUPS_SCHEMA_VERSION = "image-group-workflow-approved-groups-1"
GROUP_WORKFLOW_FRONT_EXPORT_SCHEMA_VERSION = "image-group-workflow-private-front-export-1"
GROUP_WORKFLOW_RECEIPT_SCHEMA_VERSION = "image-group-workflow-decision-import-receipt-1"
GROUP_WORKFLOW_BUILD_RECEIPT_SCHEMA_VERSION = "image-group-workflow-build-receipt-1"
SIMILARITY_POSITIVE_LABELS = {"near_duplicate", "same_visual_family"}
SIMILARITY_NEGATIVE_LABELS = {"same_theme_only", "unrelated"}
V1_ACTION_BY_LABEL = {
    "near_duplicate": "group_only",
    "same_visual_family": "group_only",
    "same_theme_only": "keep_separate",
    "unrelated": "keep_separate",
    "unsure": "defer",
}
HIGH_VISUAL_IDENTITY_COSINE = 0.98
HIGH_VISUAL_IDENTITY_PHASH_HAMMING = 2
HIGH_VISUAL_IDENTITY_DHASH_HAMMING = 2
HIGH_VISUAL_IDENTITY_ASPECT_DELTA = 0.02
DEFAULT_SIMILARITY_REVIEW_THRESHOLD = 0.85


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _bounded_text(value: Any, byte_limit: int = 2000) -> str:
    return _text(value).encode("utf-8")[:byte_limit].decode("utf-8", errors="ignore").strip()


def _validated_reviewed_at(value: Any) -> str:
    reviewed_at = _text(value).strip()
    if not reviewed_at:
        raise ValueError("reviewer identity and reviewed_at are required")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", reviewed_at):
        raise ValueError("reviewed_at must be an ISO-8601 UTC timestamp")
    try:
        datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("reviewed_at must be an ISO-8601 UTC timestamp") from exc
    return reviewed_at


def _workflow_dir(root: Path, source_run_id: str) -> Path:
    return run_path(root, source_run_id) / GROUP_WORKFLOW_DIR


def _spec_path(root: Path, source_run_id: str) -> Path:
    return _workflow_dir(root, source_run_id) / GROUP_WORKFLOW_SPEC_FILENAME


def _safe_group_prepared_path(value: Any) -> str:
    raw = _text(value).strip().replace("\\", "/")
    if not raw:
        raise ValueError("prepared_path is required")
    drive, tail = os.path.splitdrive(raw)
    if drive:
        raw = tail.replace("\\", "/")
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if ".." in parts:
        raise ValueError("prepared_path escapes workflow directory")
    if not parts or parts[0] != "inputs":
        raise ValueError("prepared_path must remain under run inputs")
    return "../" + "/".join(parts)


def _normalize_spec_for_hash(spec: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(spec)
    normalized.pop("spec_sha256", None)
    return normalized


def _normalize_spec_for_drift(spec: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_spec_for_hash(spec)
    normalized.pop("created_at", None)
    return normalized


def _validate_stored_spec(spec: dict[str, Any], *, source_run_id: str) -> None:
    if _text(spec.get("schema_version")).strip() != GROUP_WORKFLOW_SPEC_SCHEMA_VERSION:
        raise ValueError("stored group workflow spec schema_version mismatch")
    if _text(spec.get("run_id")).strip() != source_run_id:
        raise ValueError("stored group workflow spec run_id mismatch")
    expected = digest(json_bytes(_normalize_spec_for_hash(spec)))
    actual = _text(spec.get("spec_sha256")).strip()
    if not actual or actual != expected:
        raise ValueError("stored group workflow spec self-hash mismatch")


def _priority(retention: dict[str, Any], item_id: str) -> dict[str, Any]:
    priority = retention.get("priority_by_id", {}).get(item_id)
    if isinstance(priority, dict):
        return copy.deepcopy(priority)
    raise ValueError(f"retention priority is missing item {item_id}")


def _candidate_member_sort_key(retention: dict[str, Any], item_id: str) -> tuple[int, int, int, str]:
    priority = _priority(retention, item_id)
    sort_key = priority_sort_key(priority)
    rank_index = int(priority.get("rank_index", 10**9))
    ordinal = int(priority.get("ordinal", 10**9))
    return (sort_key[0], sort_key[1], rank_index, f"{ordinal:010d}:{item_id}")


def _representative_id(retention: dict[str, Any], member_ids: list[str]) -> str:
    return sorted(member_ids, key=lambda item_id: _candidate_member_sort_key(retention, item_id))[0]


def _priority_map_from_spec(spec: dict[str, Any]) -> dict[str, Any]:
    return {"priority_by_id": {row["id"]: row["priority"] for row in spec["items"]}}


def _item_lookup(source: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {_text(item.get("id")).strip(): item for item in source["items"]}


def _machine_workflow_source(source: dict[str, Any]) -> dict[str, Any]:
    machine_source = copy.deepcopy(source)
    machine_source["retention"] = build_machine_retention(source)
    machine_source["retention_basis"] = "machine_exact_file_or_pixels_overlay_for_group_workflow_only"
    return machine_source


def _labels_import_dir(root: Path, source_run_id: str) -> Path:
    return run_path(root, source_run_id) / IMPORT_ROOT


def _load_latest_label_evidence(root: Path, source_run_id: str, *, comparison_dir: str | None = None) -> dict[str, Any]:
    import_dir = _labels_import_dir(root, source_run_id)
    if not import_dir.is_dir():
        return {"labels_sha256": "", "schema_version": "", "rows": []}
    receipts: list[dict[str, Any]] = []
    for receipt_path in sorted(import_dir.glob("*/receipt.json")):
        payload = read_json(receipt_path)
        imported_at = _text(payload.get("imported_at")).strip()
        labels_sha256 = _text(payload.get("labels_sha256")).strip()
        if not imported_at or not labels_sha256:
            continue
        receipts.append({"path": receipt_path, "imported_at": imported_at, "labels_sha256": labels_sha256})
    if not receipts:
        return {"labels_sha256": "", "schema_version": "", "rows": []}
    latest = sorted(receipts, key=lambda row: (row["imported_at"], row["labels_sha256"]))[-1]
    labels = read_json(latest["path"].parent / "labels.json")
    schema_version = _text(labels.get("schema_version")).strip()
    rows: list[dict[str, Any]] = []
    if schema_version in {GROUP_WORKFLOW_DECISIONS_SCHEMA_VERSION, GROUP_WORKFLOW_DECISIONS_V2_SCHEMA_VERSION, GROUP_WORKFLOW_DECISIONS_V3_SCHEMA_VERSION}:
        raise ValueError("group workflow decisions cannot be used as pair-label evidence")
    if schema_version == REVIEW_LABELS_SCHEMA_VERSION:
        spec_v1, _source = load_bound_review_spec(root, source_run_id, comparison_dir=comparison_dir)
        normalized = validate_review_labels(spec_v1, labels)
        for row in normalized["pairs"]:
            label = row.get("human_label")
            if row.get("human_verified") is not True or label in {None, "unsure"}:
                continue
            rows.append(
                {
                    "pair_id": _text(row.get("pair_id")).strip(),
                    "left_id": _text(row.get("a") or row.get("left", {}).get("id")).strip() or _text(row.get("left", {}).get("id")).strip(),
                    "right_id": _text(row.get("b") or row.get("right", {}).get("id")).strip() or _text(row.get("right", {}).get("id")).strip(),
                    "label": _text(label).strip(),
                    "action": V1_ACTION_BY_LABEL[_text(label).strip()],
                }
            )
    else:
        spec_v2, _source = load_bound_review_spec_v2(root, source_run_id, comparison_dir=comparison_dir)
        normalized = validate_review_labels_v2(spec_v2, labels)
        for row in normalized["pairs"]:
            label = row.get("human_label")
            if row.get("human_verified") is not True or label in {None, "unsure"}:
                continue
            left = row.get("left") if isinstance(row.get("left"), dict) else {}
            right = row.get("right") if isinstance(row.get("right"), dict) else {}
            rows.append(
                {
                    "pair_id": _text(row.get("pair_id")).strip(),
                    "left_id": _text(left.get("id")).strip(),
                    "right_id": _text(right.get("id")).strip(),
                    "label": _text(label).strip(),
                    "action": _text(row.get("action")).strip(),
                }
            )
    for row in rows:
        if not row["left_id"] or not row["right_id"] or row["left_id"] == row["right_id"]:
            raise ValueError("resolved label evidence has invalid pair binding")
    return {"labels_sha256": latest["labels_sha256"], "schema_version": schema_version, "rows": rows}


def _pair_key(left_id: str, right_id: str) -> tuple[str, str]:
    return tuple(sorted((left_id, right_id)))


def _human_pair_lookup(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    lookup: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        lookup[_pair_key(row["left_id"], row["right_id"])] = copy.deepcopy(row)
    return lookup


def _alias_lineage(source: dict[str, Any]) -> list[dict[str, Any]]:
    retention = source["retention"]
    by_representative: dict[str, list[str]] = defaultdict(list)
    for row in retention.get("archived", []):
        by_representative[_text(row.get("representative_id")).strip()].append(_text(row.get("id")).strip())
    prompt_variant_map: dict[str, list[str]] = {}
    for group in retention.get("prompt_variant_groups", []):
        if not isinstance(group, dict):
            continue
        members = [
            _text(item_id).strip()
            for item_id in group.get("member_ids", [])
            if _text(item_id).strip()
        ]
        for member_id in members:
            prompt_variant_map[member_id] = members
    items = _item_lookup(source)
    results = []
    for item_id in source["retention"]["active_ids"]:
        item = items[item_id]
        priority = _priority(retention, item_id)
        results.append(
            {
                "representative_id": item_id,
                "priority_rank_index": priority.get("rank_index"),
                "archived_exact_ids": sorted(item_id_ for item_id_ in by_representative.get(item_id, []) if item_id_),
                "prompt_variant_active_ids": prompt_variant_map.get(item_id, [item_id]),
                "source_aliases": copy.deepcopy(item.get("source_aliases", [])),
            }
        )
    results.sort(key=lambda row: (int(row.get("priority_rank_index") or 10**9), _text(row.get("representative_id")).strip()))
    return results


def _workflow_items(source: dict[str, Any]) -> list[dict[str, Any]]:
    retention = source["retention"]
    results = []
    for item in source["items"]:
        item_id = _text(item.get("id")).strip()
        if not item_id:
            raise ValueError("manifest item id missing")
        prompt = _text(item.get("prompt"))
        prompt_info = prompt_signals(prompt)
        results.append(
            {
                "id": item_id,
                "style_id": _text(item.get("style_id")).strip() or item_id,
                "prepared_path": _safe_group_prepared_path(item.get("prepared_path")),
                "source_sha256": _text(item.get("sha256")).strip(),
                "prepared_sha256": _text(item.get("prepared_sha256")).strip(),
                "prompt_exact_sha256": prompt_info["exact_sha256"] if prompt_info["has_text"] else None,
                "prompt_normalized_sha256": prompt_info["normalized_sha256"] if prompt_info["has_text"] else None,
                "priority": _priority(retention, item_id),
            }
        )
    results.sort(key=lambda row: (int(row["priority"].get("rank_index") or 10**9), row["id"]))
    return results


def _duplicate_components(source: dict[str, Any]) -> list[dict[str, Any]]:
    active_ids = set(source["retention"]["active_ids"])
    pair_lookup: dict[tuple[str, str], dict[str, Any]] = {}
    adjacency: dict[str, set[str]] = defaultdict(set)
    for pair in source["pair_records"]:
        left_id = _text(pair.get("left", {}).get("id")).strip()
        right_id = _text(pair.get("right", {}).get("id")).strip()
        if left_id not in active_ids or right_id not in active_ids:
            continue
        relation = _text(pair.get("machine_candidate", {}).get("local_relation")).strip()
        if relation not in {"exact_file", "exact_pixels"}:
            continue
        key = _pair_key(left_id, right_id)
        pair_lookup[key] = copy.deepcopy(pair)
        adjacency[left_id].add(right_id)
        adjacency[right_id].add(left_id)
    visited: set[str] = set()
    results = []
    for seed in sorted(adjacency):
        if seed in visited:
            continue
        component: list[str] = []
        queue = deque([seed])
        visited.add(seed)
        while queue:
            item_id = queue.popleft()
            component.append(item_id)
            for neighbor in sorted(adjacency[item_id]):
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                queue.append(neighbor)
        if len(component) < 2:
            continue
        member_ids = sorted(component, key=lambda item_id: _candidate_member_sort_key(source["retention"], item_id))
        evidence_pairs = []
        for left_id in member_ids:
            for right_id in member_ids:
                if left_id >= right_id:
                    continue
                pair = pair_lookup.get(_pair_key(left_id, right_id))
                if pair is None:
                    continue
                machine = pair["machine_candidate"]
                match_types = []
                if machine.get("local_relation") == "exact_file":
                    match_types.append("exact_file")
                elif machine.get("local_relation") == "exact_pixels":
                    match_types.append("exact_pixels")
                evidence_pairs.append(
                    {
                        "left_id": left_id,
                        "right_id": right_id,
                        "match_types": match_types,
                        "phash_hamming": machine.get("phash_hamming"),
                        "dhash_hamming": machine.get("dhash_hamming"),
                    }
                )
        candidate_payload = {"kind": "duplicate", "members": member_ids}
        results.append(
            {
                "id": "duplicate-candidate-" + digest(json_bytes(candidate_payload))[:24],
                "member_ids": member_ids,
                "suggested_representative_id": _representative_id(source["retention"], member_ids),
                "representative_priority_ids": member_ids,
                "evidence": {
                    "basis": "machine_exact_file_or_exact_pixels_active_only",
                    "pair_count": len(evidence_pairs),
                    "pairs": evidence_pairs,
                },
            }
        )
    results.sort(key=lambda row: (len(row["member_ids"]), row["id"]))
    return results


def _similarity_review_threshold(root: Path, source_run_id: str, source: dict[str, Any], label_evidence: dict[str, Any]) -> float | None:
    labels_sha256 = _text(label_evidence.get("labels_sha256")).strip()
    if not labels_sha256 or _text(label_evidence.get("schema_version")).strip() != "image-similarity-review-labels-2":
        return None
    spec_v2, _source = load_bound_review_spec_v2(root, source_run_id, comparison_dir=source["comparison_dir"])
    labels_path = _labels_import_dir(root, source_run_id) / labels_sha256 / "labels.json"
    if not labels_path.is_file():
        return None
    labels = read_json(labels_path)
    summary = calibrate(source, spec_v2, labels)
    if _text(summary.get("status")).strip() != "ok":
        return None
    observed = summary.get("observed_overlap") if isinstance(summary.get("observed_overlap"), dict) else {}
    threshold = observed.get("observed_clean_positive_from")
    if isinstance(threshold, (int, float)):
        return float(threshold)
    return None


def _is_high_visual_identity_candidate(pair: dict[str, Any]) -> bool:
    machine = pair.get("machine_candidate") if isinstance(pair.get("machine_candidate"), dict) else {}
    phash_hamming = machine.get("phash_hamming")
    dhash_hamming = machine.get("dhash_hamming")
    aspect_ratio_delta = machine.get("aspect_ratio_delta")
    voyage_cosine = pair.get("voyage_cosine")
    perceptual_gate = (
        isinstance(phash_hamming, int)
        and phash_hamming <= HIGH_VISUAL_IDENTITY_PHASH_HAMMING
        and isinstance(dhash_hamming, int)
        and dhash_hamming <= HIGH_VISUAL_IDENTITY_DHASH_HAMMING
        and isinstance(aspect_ratio_delta, (int, float))
        and float(aspect_ratio_delta) <= HIGH_VISUAL_IDENTITY_ASPECT_DELTA
    )
    cosine_gate = isinstance(voyage_cosine, (int, float)) and float(voyage_cosine) >= HIGH_VISUAL_IDENTITY_COSINE
    return perceptual_gate or cosine_gate


def _identity_review_candidates(source: dict[str, Any]) -> list[dict[str, Any]]:
    active_ids = set(source["retention"]["active_ids"])
    supporting_pairs: dict[tuple[str, str], dict[str, Any]] = {}
    adjacency: dict[str, set[str]] = defaultdict(set)
    for pair in source["pair_records"]:
        left_id = _text(pair.get("left", {}).get("id")).strip()
        right_id = _text(pair.get("right", {}).get("id")).strip()
        if left_id not in active_ids or right_id not in active_ids:
            continue
        relation = _text(pair.get("machine_candidate", {}).get("local_relation")).strip()
        if relation not in {"exact_file", "exact_pixels"} and not _is_high_visual_identity_candidate(pair):
            continue
        key = _pair_key(left_id, right_id)
        supporting_pairs[key] = copy.deepcopy(pair)
        adjacency[left_id].add(right_id)
        adjacency[right_id].add(left_id)

    visited: set[str] = set()
    results = []
    for seed in sorted(adjacency):
        if seed in visited:
            continue
        component: list[str] = []
        queue = deque([seed])
        visited.add(seed)
        while queue:
            item_id = queue.popleft()
            component.append(item_id)
            for neighbor in sorted(adjacency[item_id]):
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                queue.append(neighbor)
        if len(component) < 2:
            continue
        member_ids = sorted(component, key=lambda item_id: _candidate_member_sort_key(source["retention"], item_id))
        evidence_pairs = []
        for left_index, left_id in enumerate(member_ids):
            for right_id in member_ids[left_index + 1 :]:
                pair = supporting_pairs.get(_pair_key(left_id, right_id))
                if not isinstance(pair, dict):
                    continue
                machine = pair.get("machine_candidate", {})
                match_types = []
                relation = _text(machine.get("local_relation")).strip()
                if relation in {"exact_file", "exact_pixels"}:
                    match_types.append(relation)
                if _is_high_visual_identity_candidate(pair):
                    match_types.append("high_visual_identity_review_candidate")
                if not match_types:
                    continue
                evidence_pairs.append(
                    {
                        "left_id": left_id,
                        "right_id": right_id,
                        "match_types": sorted(set(match_types)),
                        "candidate_relation": relation or "identity_review_candidate",
                        "voyage_cosine": pair.get("voyage_cosine"),
                        "phash_hamming": machine.get("phash_hamming"),
                        "dhash_hamming": machine.get("dhash_hamming"),
                        "aspect_ratio_delta": machine.get("aspect_ratio_delta"),
                    }
                )
        candidate_payload = {"kind": "duplicate", "members": member_ids}
        results.append(
            {
                "id": "duplicate-candidate-" + digest(json_bytes(candidate_payload))[:24],
                "member_ids": member_ids,
                "suggested_representative_id": _representative_id(source["retention"], member_ids),
                "representative_priority_ids": member_ids,
                "evidence": {
                    "basis": "machine_exact_or_high_visual_identity_review_active_only",
                    "pair_count": len(evidence_pairs),
                    "pairs": evidence_pairs,
                    "heuristic": {
                        "voyage_cosine_at_or_above": HIGH_VISUAL_IDENTITY_COSINE,
                        "phash_hamming_at_or_below": HIGH_VISUAL_IDENTITY_PHASH_HAMMING,
                        "dhash_hamming_at_or_below": HIGH_VISUAL_IDENTITY_DHASH_HAMMING,
                        "aspect_ratio_delta_at_or_below": HIGH_VISUAL_IDENTITY_ASPECT_DELTA,
                        "review_only": True,
                    },
                },
            }
        )
    results.sort(key=lambda row: (len(row["member_ids"]), row["id"]))
    return results


def _load_voyage_vectors(source: dict[str, Any]) -> dict[str, Any]:
    comparison_path = source["source_dir"] / source["comparison_dir"] / "vectors.json"
    vectors_payload = read_json(comparison_path)
    voyage_vectors = vectors_payload.get("voyage_image")
    if not isinstance(voyage_vectors, dict) or not voyage_vectors:
        raise ValueError("group workflow requires voyage_image vectors")
    return voyage_vectors


def _known_pair_lists(member_ids: list[str], pair_lookup: dict[tuple[str, str], dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    positives = []
    negatives = []
    member_set = set(member_ids)
    for key, row in sorted(pair_lookup.items()):
        if key[0] not in member_set or key[1] not in member_set:
            continue
        payload = {
            "pair_id": row["pair_id"],
            "left_id": row["left_id"],
            "right_id": row["right_id"],
            "label": row["label"],
            "action": row["action"],
        }
        if row["label"] in SIMILARITY_POSITIVE_LABELS:
            positives.append(payload)
        elif row["label"] in SIMILARITY_NEGATIVE_LABELS:
            negatives.append(payload)
    return positives, negatives


def _similarity_candidates(source: dict[str, Any], label_rows: list[dict[str, Any]], *, min_cosine: float) -> list[dict[str, Any]]:
    active_ids = set(source["retention"]["active_ids"])
    voyage_vectors = _load_voyage_vectors(source)
    filtered_vectors = {item_id: vector for item_id, vector in voyage_vectors.items() if item_id in active_ids}
    pair_lookup = _human_pair_lookup(label_rows)
    seen_member_sets: set[tuple[str, ...]] = set()
    results = []
    for group in build_visual_families(filtered_vectors, min_cosine=min_cosine):
        member_ids = [
            _text(item_id).strip()
            for item_id in group.get("member_ids", [])
            if _text(item_id).strip() in active_ids
        ]
        if len(member_ids) < 2:
            continue
        positives, negatives = _known_pair_lists(member_ids, pair_lookup)
        payload = {"kind": "similarity", "members": sorted(member_ids)}
        seen_member_sets.add(tuple(sorted(member_ids)))
        evidence = copy.deepcopy(group.get("evidence", {}))
        if isinstance(evidence, dict):
            evidence["threshold_calibrated"] = min_cosine != DEFAULT_SIMILARITY_REVIEW_THRESHOLD
            evidence["calibrated_min_cosine_for_review"] = round(float(min_cosine), 6)
        results.append(
            {
                "id": "similarity-candidate-" + digest(json_bytes(payload))[:24],
                "member_ids": sorted(member_ids, key=lambda item_id: _candidate_member_sort_key(source["retention"], item_id)),
                "representative_priority_ids": sorted(member_ids, key=lambda item_id: _candidate_member_sort_key(source["retention"], item_id)),
                "candidate_only": True,
                "evidence": evidence,
                "known_positive_pairs": positives,
                "known_negative_pairs": negatives,
            }
        )
    positive_components: dict[str, set[str]] = defaultdict(set)
    for row in label_rows:
        if row["label"] not in SIMILARITY_POSITIVE_LABELS:
            continue
        left_id = row["left_id"]
        right_id = row["right_id"]
        if left_id not in active_ids or right_id not in active_ids:
            continue
        positive_components[left_id].add(right_id)
        positive_components[right_id].add(left_id)
    visited: set[str] = set()
    for seed in sorted(positive_components):
        if seed in visited:
            continue
        component = []
        queue = deque([seed])
        visited.add(seed)
        while queue:
            item_id = queue.popleft()
            component.append(item_id)
            for neighbor in sorted(positive_components[item_id]):
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                queue.append(neighbor)
        if len(component) < 2:
            continue
        member_ids = sorted(component, key=lambda item_id: _candidate_member_sort_key(source["retention"], item_id))
        member_key = tuple(sorted(member_ids))
        if member_key in seen_member_sets:
            continue
        positives, negatives = _known_pair_lists(member_ids, pair_lookup)
        payload = {"kind": "similarity", "members": list(member_key)}
        results.append(
            {
                "id": "similarity-candidate-" + digest(json_bytes(payload))[:24],
                "member_ids": member_ids,
                "representative_priority_ids": member_ids,
                "candidate_only": True,
                "evidence": {
                    "method": "known_positive_pair_component",
                    "pair_count": len(positives),
                },
                "known_positive_pairs": positives,
                "known_negative_pairs": negatives,
            }
        )
        seen_member_sets.add(member_key)
    results.sort(key=lambda row: (len(row["member_ids"]), row["id"]))
    return results


def _build_spec(root: Path, source_run_id: str, *, comparison_dir: str | None = None) -> dict[str, Any]:
    root = Path(root).resolve()
    source = load_review_source(root, source_run_id, comparison_dir=comparison_dir or DEFAULT_COMPARISON_DIR)
    label_evidence = _load_latest_label_evidence(root, source_run_id, comparison_dir=source["comparison_dir"])
    machine_source = _machine_workflow_source(source)
    similarity_threshold = _similarity_review_threshold(root, source_run_id, source, label_evidence) or DEFAULT_SIMILARITY_REVIEW_THRESHOLD
    duplicate_candidates = _identity_review_candidates(machine_source)
    spec = {
        "schema_version": GROUP_WORKFLOW_SPEC_SCHEMA_VERSION,
        "created_at": now(),
        "run_id": source_run_id,
        "source_manifest_sha256": source["manifest_sha256"],
        "vector_fingerprint": source["vector_fingerprint"],
        "source_labels_sha256": label_evidence["labels_sha256"],
        "items": _workflow_items(machine_source),
        "stage1": {
            "active_ids": copy.deepcopy(machine_source["retention"]["active_ids"]),
            "archived": copy.deepcopy(machine_source["retention"]["archived"]),
            "alias_lineage": copy.deepcopy(machine_source["retention"].get("alias_lineage", [])),
            "policy": _text(machine_source["retention"].get("policy")).strip() or "machine_exact_file_or_pixels_overlay_for_group_workflow_only",
        },
        "duplicate_candidates": duplicate_candidates,
        "similarity_candidates": _similarity_candidates(machine_source, label_evidence["rows"], min_cosine=similarity_threshold),
        "metadata_optional": True,
        "front_review_requires_explicit_complete": True,
        "release_eligible": False,
        "public_rights_approved": False,
        "notes": [
            "Stage 2 exact decisions affect only a private logical retention overlay for this run.",
            "Prompt-only matches never delete images in this workflow.",
            "Stage 3 group approvals never auto-approve an entire machine component; only explicitly selected members count.",
            "Stage 5 private front inclusion is separate from rights or public release approval.",
        ],
    }
    spec["spec_sha256"] = digest(json_bytes(spec))
    return spec


def _summary_payload(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": GROUP_WORKFLOW_SUMMARY_SCHEMA_VERSION,
        "created_at": spec["created_at"],
        "run_id": spec["run_id"],
        "spec_sha256": spec["spec_sha256"],
        "source_manifest_sha256": spec["source_manifest_sha256"],
        "vector_fingerprint": spec["vector_fingerprint"],
        "source_labels_sha256": spec["source_labels_sha256"],
        "item_count": len(spec["items"]),
        "duplicate_candidate_count": len(spec["duplicate_candidates"]),
        "similarity_candidate_count": len(spec["similarity_candidates"]),
        "active_stage1_items": len(spec["stage1"]["active_ids"]),
        "archived_stage1_items": len(spec["stage1"]["archived"]),
        "front_review_requires_explicit_complete": True,
        "release_eligible": False,
        "public_rights_approved": False,
    }


def _build_receipt_payload(spec: dict[str, Any], directory: Path) -> dict[str, Any]:
    return {
        "schema_version": GROUP_WORKFLOW_BUILD_RECEIPT_SCHEMA_VERSION,
        "status": "ready",
        "run_id": spec["run_id"],
        "spec_sha256": spec["spec_sha256"],
        "source_manifest_sha256": spec["source_manifest_sha256"],
        "vector_fingerprint": spec["vector_fingerprint"],
        "source_labels_sha256": spec["source_labels_sha256"],
        "files": {
            "spec": str(directory / GROUP_WORKFLOW_SPEC_FILENAME),
            "template": str(directory / GROUP_WORKFLOW_TEMPLATE_FILENAME),
            "summary": str(directory / GROUP_WORKFLOW_SUMMARY_FILENAME),
            "html": str(directory / GROUP_WORKFLOW_HTML_FILENAME),
            "receipt": str(directory / GROUP_WORKFLOW_BUILD_RECEIPT_FILENAME),
        },
        "network_calls": 0,
        "writes": 5,
        "source_mutations": False,
        "canonical_writes": 0,
        "public_release_approval": False,
    }


def _ensure_idempotent_file(path: Path, payload: dict[str, Any]) -> None:
    if path.exists() and digest(json_bytes(read_json(path))) != digest(json_bytes(payload)):
        raise ValueError(f"existing workflow artifact differs: {path.name}")


def blank_group_workflow_decisions(
    spec: dict[str, Any], *, schema_version: str | None = None,
) -> dict[str, Any]:
    if schema_version is None:
        schema_version = (GROUP_WORKFLOW_DECISIONS_V3_SCHEMA_VERSION
                          if spec.get("approval_policy") == DEFAULT_IMAGE_APPROVAL_POLICY
                          else GROUP_WORKFLOW_DECISIONS_V2_SCHEMA_VERSION)
    if schema_version not in {GROUP_WORKFLOW_DECISIONS_SCHEMA_VERSION, GROUP_WORKFLOW_DECISIONS_V2_SCHEMA_VERSION, GROUP_WORKFLOW_DECISIONS_V3_SCHEMA_VERSION}:
        raise ValueError("group workflow decisions schema_version mismatch")
    result = {
        "schema_version": schema_version,
        "spec_sha256": _text(spec.get("spec_sha256")).strip(),
        "run_id": _text(spec.get("run_id")).strip(),
        "reviewer": "",
        "reviewed_at": "",
        "duplicate_reviews": [
            {
                "candidate_id": row["id"],
                "decision": "defer",
                "selected_ids": [],
                "remainder_distinct": False,
            }
            for row in spec.get("duplicate_candidates", [])
        ],
        "similarity_reviews": [
            {
                "candidate_id": row["id"],
                "decision": "defer",
                "selected_ids": [],
                "tags_text": "",
            }
            for row in spec.get("similarity_candidates", [])
        ],
        "individual_approvals": [],
        "metadata_optional": True,
        "front_review_complete": False,
        "notes": "Unchecked or omitted members are not negative decisions. Duplicate review must be fully resolved before similarity approvals can advance to front inclusion.",
    }
    if schema_version == GROUP_WORKFLOW_DECISIONS_V2_SCHEMA_VERSION:
        result.pop("front_review_complete")
        result["group_approvals"] = []
        result["notes"] = (
            "Stage 4 opens only after duplicate and similarity review are complete. "
            "Stage 3 approves group membership only. Stage 4 group or individual approvals "
            "immediately enter the private front allowlist; manual tags are optional. "
            "Unchecked or omitted members remain unapproved, not negative decisions."
        )
    elif schema_version == GROUP_WORKFLOW_DECISIONS_V3_SCHEMA_VERSION:
        if spec.get("approval_policy") != DEFAULT_IMAGE_APPROVAL_POLICY:
            raise ValueError("v3 default image approval requires explicit source-spec opt-in")
        result.pop("front_review_complete")
        result.pop("individual_approvals")
        result["image_approvals"] = copy.deepcopy(spec.get("initial_image_approvals", []))
        result["approval_policy"] = DEFAULT_IMAGE_APPROVAL_POLICY
        result["notes"] = "After stage2 and stage3 are complete, retained editable images default approved; any image may be unchecked. Personal image memos are optional. Existing baseline decisions remain read-only."
    return result


def _candidate_lookup(spec: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    rows = spec.get(key)
    if not isinstance(rows, list):
        raise ValueError(f"{key} missing")
    lookup: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{key} rows must be objects")
        candidate_id = _text(row.get("id")).strip()
        if not candidate_id or candidate_id in lookup:
            raise ValueError(f"{key} contains invalid candidate ids")
        lookup[candidate_id] = row
    return lookup


def _normalize_selected_ids(value: Any, *, allowed: list[str], field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    allowed_set = set(allowed)
    seen: set[str] = set()
    normalized: list[str] = []
    for item_id in value:
        normalized_id = _text(item_id).strip()
        if not normalized_id or normalized_id in seen or normalized_id not in allowed_set:
            raise ValueError(f"{field_name} contains unknown or duplicate ids")
        seen.add(normalized_id)
        normalized.append(normalized_id)
    ordered = [item_id for item_id in allowed if item_id in seen]
    return ordered


def _stage2_overlay(spec: dict[str, Any], normalized_duplicates: list[dict[str, Any]], *, enforce_active_selection: bool = False) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    stage1 = spec["stage1"]
    active_ids = list(stage1["active_ids"])
    active_set = set(active_ids)
    archived_rows = copy.deepcopy(stage1["archived"])
    new_deleted_ids: list[str] = []
    unresolved: list[str] = []
    applied: list[dict[str, Any]] = []
    candidate_lookup = _candidate_lookup(spec, "duplicate_candidates")
    review_by_id = {row["candidate_id"]: row for row in normalized_duplicates}
    for candidate_id, candidate in candidate_lookup.items():
        member_ids = list(candidate["member_ids"])
        review = review_by_id.get(candidate_id)
        if review is None or review["decision"] == "defer":
            unresolved.append(candidate_id)
            continue
        if review["decision"] == "distinct_images":
            continue
        selected_ids = review["selected_ids"]
        if enforce_active_selection and not set(selected_ids) <= active_set:
            raise ValueError("overlapping duplicate selections include an already removed image")
        baseline_ids = set(spec.get("baseline", {}).get("read_only_ids", []))
        selected_baseline = [item_id for item_id in selected_ids if item_id in baseline_ids]
        if len(selected_baseline) > 1:
            raise ValueError("duplicate subset cannot merge multiple read-only baseline keepers")
        remaining = [item_id for item_id in member_ids if item_id not in selected_ids]
        if len(remaining) > 1 and review["remainder_distinct"] is not True:
            unresolved.append(candidate_id)
            continue
        keep_id = selected_baseline[0] if selected_baseline else _representative_id(_priority_map_from_spec(spec), selected_ids)
        deleted_ids = [item_id for item_id in selected_ids if item_id != keep_id]
        for deleted_id in deleted_ids:
            if deleted_id not in active_set:
                raise ValueError("duplicate decisions reference an item that is no longer active")
            active_set.remove(deleted_id)
            new_deleted_ids.append(deleted_id)
            archived_rows.append(
                {
                    "id": deleted_id,
                    "representative_id": keep_id,
                    "action": "logical_delete",
                    "reasons": [
                        {
                            "kind": "human_confirmed_same_image_subset",
                            "candidate_id": candidate_id,
                            "selected_ids": selected_ids,
                            "suggested_representative_id": candidate["suggested_representative_id"],
                            "remainder_distinct": review["remainder_distinct"],
                            "basis": "explicit_subset_only_no_transitive_component_approval",
                        }
                    ],
                }
            )
        applied.append(
            {
                "candidate_id": candidate_id,
                "keep_id": keep_id,
                "deleted_ids": deleted_ids,
                "remaining_distinct_ids": remaining,
                "remainder_distinct": review["remainder_distinct"],
            }
        )
    active_ids = [item_id for item_id in stage1["active_ids"] if item_id in active_set]
    overlay = {
        "schema_version": GROUP_WORKFLOW_RETENTION_OVERLAY_SCHEMA_VERSION,
        "run_id": spec["run_id"],
        "spec_sha256": spec["spec_sha256"],
        "policy": "group_workflow_stage2_human_exact_overlay_v1",
        "active_ids": active_ids,
        "archived": archived_rows,
        "deleted_ids": new_deleted_ids,
        "reversible": True,
        "source_mutations": False,
        "basis": "private_run_only_human_exact_confirmation",
    }
    return overlay, unresolved, applied


def _validate_group_workflow_decisions_v1(spec: dict[str, Any], decisions: dict[str, Any], *, enforce_active_duplicate_selection: bool = False) -> dict[str, Any]:
    if _text(decisions.get("schema_version")).strip() != GROUP_WORKFLOW_DECISIONS_SCHEMA_VERSION:
        raise ValueError("group workflow decisions schema_version mismatch")
    if _text(decisions.get("spec_sha256")).strip() != _text(spec.get("spec_sha256")).strip():
        raise ValueError("group workflow decisions belong to another spec")
    if _text(decisions.get("run_id")).strip() != _text(spec.get("run_id")).strip():
        raise ValueError("group workflow decisions run_id mismatch")
    reviewer = _text(decisions.get("reviewer")).strip()
    if not reviewer:
        raise ValueError("reviewer identity and reviewed_at are required")
    reviewed_at = _validated_reviewed_at(decisions.get("reviewed_at"))
    duplicate_candidates = _candidate_lookup(spec, "duplicate_candidates")
    similarity_candidates = _candidate_lookup(spec, "similarity_candidates")
    items = {row["id"]: row for row in spec["items"]}
    priority_map = {row["id"]: row["priority"] for row in spec["items"]}

    normalized_duplicates: list[dict[str, Any]] = []
    seen_duplicates: set[str] = set()
    for row in decisions.get("duplicate_reviews", []):
        if not isinstance(row, dict):
            raise ValueError("duplicate_reviews rows must be objects")
        candidate_id = _text(row.get("candidate_id")).strip()
        if not candidate_id or candidate_id in seen_duplicates or candidate_id not in duplicate_candidates:
            raise ValueError("duplicate_reviews contain unknown or duplicate candidate ids")
        seen_duplicates.add(candidate_id)
        decision = _text(row.get("decision")).strip()
        if decision not in {"same_image_subset", "distinct_images", "defer"}:
            raise ValueError("duplicate decision is invalid")
        candidate = duplicate_candidates[candidate_id]
        member_ids = list(candidate["member_ids"])
        selected_ids = _normalize_selected_ids(row.get("selected_ids", []), allowed=member_ids, field_name="duplicate selected_ids")
        remainder_distinct = bool(row.get("remainder_distinct") is True)
        if decision == "same_image_subset":
            if len(selected_ids) < 2:
                raise ValueError("same_image_subset requires at least two selected ids")
        else:
            if selected_ids:
                raise ValueError("only same_image_subset may include selected ids")
            remainder_distinct = False
        normalized_duplicates.append(
            {
                "candidate_id": candidate_id,
                "decision": decision,
                "selected_ids": selected_ids,
                "remainder_distinct": remainder_distinct,
                "representative_priority_ids": list(candidate.get("representative_priority_ids", member_ids)),
            }
        )

    overlay, unresolved_duplicates, applied_duplicates = _stage2_overlay(spec, normalized_duplicates, enforce_active_selection=enforce_active_duplicate_selection)
    active_after_stage2 = set(overlay["active_ids"])

    normalized_similarity: list[dict[str, Any]] = []
    seen_similarity: set[str] = set()
    approved_groups: list[dict[str, Any]] = []
    for row in decisions.get("similarity_reviews", []):
        if not isinstance(row, dict):
            raise ValueError("similarity_reviews rows must be objects")
        candidate_id = _text(row.get("candidate_id")).strip()
        if not candidate_id or candidate_id in seen_similarity or candidate_id not in similarity_candidates:
            raise ValueError("similarity_reviews contain unknown or duplicate candidate ids")
        seen_similarity.add(candidate_id)
        decision = _text(row.get("decision")).strip()
        if decision not in {"approve_selected", "defer"}:
            raise ValueError("similarity decision is invalid")
        candidate = similarity_candidates[candidate_id]
        member_ids = list(candidate["member_ids"])
        selected_ids = _normalize_selected_ids(row.get("selected_ids", []), allowed=member_ids, field_name="similarity selected_ids")
        tags_text = _bounded_text(row.get("tags_text"))
        if decision == "approve_selected":
            if len(selected_ids) < 2:
                raise ValueError("approve_selected requires at least two selected ids")
            inactive = [item_id for item_id in selected_ids if item_id not in active_after_stage2]
            if inactive:
                raise ValueError("similarity approvals cannot include logically deleted or inactive ids")
            negative_pairs = [
                copy.deepcopy(pair)
                for pair in candidate.get("known_negative_pairs", [])
                if _text(pair.get("left_id")).strip() in selected_ids and _text(pair.get("right_id")).strip() in selected_ids
            ]
            if negative_pairs:
                raise ValueError("similarity approvals cannot include a prior unrelated or same_theme_only pair")
            approved_groups.append(
                {
                    "candidate_id": candidate_id,
                    "member_ids": selected_ids,
                    "tags_text": tags_text,
                    "suggested_representative_id": _representative_id(_priority_map_from_spec(spec), selected_ids),
                    "representative_priority_ids": list(candidate.get("representative_priority_ids", member_ids)),
                    "candidate_only": True,
                    "known_positive_pairs": [
                        copy.deepcopy(pair)
                        for pair in candidate.get("known_positive_pairs", [])
                        if _text(pair.get("left_id")).strip() in selected_ids and _text(pair.get("right_id")).strip() in selected_ids
                    ],
                    "known_negative_pairs": negative_pairs,
                }
            )
        else:
            if selected_ids:
                raise ValueError("defer similarity review must not include selected ids")
        normalized_similarity.append(
            {
                "candidate_id": candidate_id,
                "decision": decision,
                "selected_ids": selected_ids,
                "tags_text": tags_text,
            }
        )

    normalized_individuals: list[dict[str, Any]] = []
    seen_individuals: set[str] = set()
    include_ids: set[str] = set()
    exclude_ids: set[str] = set()
    for row in decisions.get("individual_approvals", []):
        if not isinstance(row, dict):
            raise ValueError("individual_approvals rows must be objects")
        item_id = _text(row.get("id")).strip()
        if not item_id or item_id in seen_individuals or item_id not in items:
            raise ValueError("individual_approvals contain unknown or duplicate ids")
        seen_individuals.add(item_id)
        approved = row.get("approved")
        if not isinstance(approved, bool):
            raise ValueError("individual approval approved must be boolean")
        if item_id not in active_after_stage2:
            raise ValueError("individual approvals may reference only retained active ids")
        tags_text = _bounded_text(row.get("tags_text"))
        normalized_individuals.append({"id": item_id, "approved": approved, "tags_text": tags_text})
        if approved:
            include_ids.add(item_id)
        else:
            exclude_ids.add(item_id)

    front_review_complete = decisions.get("front_review_complete") is True
    approved_group_ids = {item_id for row in approved_groups for item_id in row["member_ids"]}
    front_candidate_ids = (approved_group_ids | include_ids) - exclude_ids
    effective_approved_groups = [] if unresolved_duplicates else approved_groups
    if unresolved_duplicates:
        front_candidate_ids = set()
    front_items = []
    if front_review_complete and not unresolved_duplicates:
        tags_by_id: dict[str, list[str]] = defaultdict(list)
        for row in approved_groups:
            tags_text = _text(row.get("tags_text")).strip()
            if tags_text:
                for item_id in row["member_ids"]:
                    tags_by_id[item_id].append(tags_text)
        for row in normalized_individuals:
            tags_text = _text(row.get("tags_text")).strip()
            if tags_text and row["approved"]:
                tags_by_id[row["id"]].append(tags_text)
        for item_id in sorted(front_candidate_ids, key=lambda value: _candidate_member_sort_key({"priority_by_id": priority_map}, value)):
            front_items.append(
                {
                    "id": item_id,
                    "style_id": items[item_id]["style_id"],
                    "prepared_path": items[item_id]["prepared_path"],
                    "priority": copy.deepcopy(items[item_id]["priority"]),
                    "tags_texts": tags_by_id.get(item_id, []),
                    "release_eligible": False,
                    "public_rights_approved": False,
                }
            )

    return {
        "schema_version": GROUP_WORKFLOW_DECISIONS_SCHEMA_VERSION,
        "spec_sha256": spec["spec_sha256"],
        "run_id": spec["run_id"],
        "reviewer": reviewer,
        "reviewed_at": reviewed_at,
        "duplicate_reviews": normalized_duplicates,
        "similarity_reviews": normalized_similarity,
        "individual_approvals": normalized_individuals,
        "metadata_optional": True,
        "front_review_complete": front_review_complete,
        "stage2_overlay": overlay,
        "stage2_duplicate_gate_status": "pending_duplicate_review" if unresolved_duplicates else "complete",
        "unresolved_duplicate_candidate_ids": unresolved_duplicates,
        "applied_duplicate_candidates": applied_duplicates,
        "approved_similarity_groups": effective_approved_groups,
        "approved_similarity_groups_pending_gate": approved_groups if unresolved_duplicates else [],
        "private_front_export_items": front_items,
        "private_front_export_status": (
            "blocked_pending_duplicate_review"
            if unresolved_duplicates
            else ("ready" if front_review_complete else "pending_front_review_complete")
        ),
        "actual_deletions": 0,
        "source_mutations": False,
        "canonical_writes": 0,
        "public_release_approval": False,
    }


def _validate_group_workflow_decisions_v2(spec: dict[str, Any], decisions: dict[str, Any], *, enforce_active_duplicate_selection: bool = False) -> dict[str, Any]:
    """Validate both the frozen five-step protocol and the sequential four-step one.

    The source spec stays frozen so saved human work remains bound to the same
    images. Only decisions-2 changes approval semantics: membership is not a
    front approval, and unfinished similarity review blocks every stage-4 item.
    """
    if not isinstance(decisions, dict):
        raise ValueError("group workflow decisions must be an object")
    version = _text(decisions.get("schema_version")).strip()
    if version == GROUP_WORKFLOW_DECISIONS_SCHEMA_VERSION:
        return _validate_group_workflow_decisions_v1(spec, decisions)
    if version != GROUP_WORKFLOW_DECISIONS_V2_SCHEMA_VERSION:
        raise ValueError("group workflow decisions schema_version mismatch")

    for field in ("duplicate_reviews", "similarity_reviews", "group_approvals", "individual_approvals"):
        if not isinstance(decisions.get(field, []), list):
            raise ValueError(f"{field} must be a list")
    legacy_input = copy.deepcopy(decisions)
    legacy_input["schema_version"] = GROUP_WORKFLOW_DECISIONS_SCHEMA_VERSION
    # Never honor a migrated or hand-edited fifth-stage flag under v2.
    legacy_input["front_review_complete"] = False
    keep_separate_ids: set[str] = set()
    for row in legacy_input.get("similarity_reviews", []):
        if not isinstance(row, dict):
            raise ValueError("similarity_reviews rows must be objects")
        if _text(row.get("decision")).strip() == "keep_separate":
            if row.get("selected_ids", []):
                raise ValueError("keep_separate similarity review must not include selected ids")
            keep_separate_ids.add(_text(row.get("candidate_id")).strip())
            row["decision"] = "defer"
    normalized = _validate_group_workflow_decisions_v1(spec, legacy_input, enforce_active_duplicate_selection=enforce_active_duplicate_selection)
    normalized["schema_version"] = GROUP_WORKFLOW_DECISIONS_V2_SCHEMA_VERSION
    for row in normalized["similarity_reviews"]:
        if row["candidate_id"] in keep_separate_ids:
            row["decision"] = "keep_separate"

    active_ids = set(normalized["stage2_overlay"]["active_ids"])
    similarity_candidates = _candidate_lookup(spec, "similarity_candidates")
    review_by_id = {row["candidate_id"]: row for row in normalized["similarity_reviews"]}
    skipped_similarity = [
        candidate_id for candidate_id, candidate in similarity_candidates.items()
        if len(active_ids.intersection(candidate["member_ids"])) < 2
    ]
    unresolved_similarity = [
        candidate_id for candidate_id in similarity_candidates
        if candidate_id not in skipped_similarity
        and review_by_id.get(candidate_id, {}).get("decision") not in {"approve_selected", "keep_separate"}
    ]
    membership_groups = (
        normalized["approved_similarity_groups"]
        + normalized["approved_similarity_groups_pending_gate"]
    )
    membership_by_candidate = {row["candidate_id"]: row for row in membership_groups}
    grouped_ids = {item_id for row in membership_groups for item_id in row["member_ids"]}

    group_approvals: list[dict[str, Any]] = []
    seen_groups: set[str] = set()
    for row in decisions.get("group_approvals", []):
        if not isinstance(row, dict):
            raise ValueError("group_approvals rows must be objects")
        candidate_id = _text(row.get("candidate_id")).strip()
        if not candidate_id or candidate_id in seen_groups or candidate_id not in similarity_candidates:
            raise ValueError("group_approvals contain unknown or duplicate candidate ids")
        if candidate_id not in membership_by_candidate:
            raise ValueError("stage4 group approval requires an approved stage3 selected group")
        if not isinstance(row.get("approved"), bool):
            raise ValueError("group approval approved must be boolean")
        seen_groups.add(candidate_id)
        group_approvals.append({
            "candidate_id": candidate_id,
            "approved": row["approved"],
            "tags_text": _bounded_text(row.get("tags_text")),
        })
    for row in normalized["individual_approvals"]:
        if row["id"] in grouped_ids:
            raise ValueError("stage4 individual approvals may reference only retained ungrouped ids")

    stage2_complete = normalized["stage2_duplicate_gate_status"] == "complete"
    stage3_complete = not unresolved_similarity
    stage4_unlocked = stage2_complete and stage3_complete
    group_approval_by_id = {row["candidate_id"]: row for row in group_approvals}
    front_ids: set[str] = set()
    tags_by_id: dict[str, list[str]] = defaultdict(list)
    for group in membership_groups:
        approval = group_approval_by_id.get(group["candidate_id"], {})
        # V1 stage-3 tags are retained in similarity_reviews as evidence, but
        # only optional stage-4 text is used in a v2 front export.
        group["tags_text"] = _text(approval.get("tags_text")).strip()
        group["stage4_approved"] = approval.get("approved") is True
        if group["stage4_approved"]:
            front_ids.update(group["member_ids"])
            if group["tags_text"]:
                for item_id in group["member_ids"]:
                    tags_by_id[item_id].append(group["tags_text"])
    for row in normalized["individual_approvals"]:
        if row["approved"]:
            front_ids.add(row["id"])
            if row["tags_text"]:
                tags_by_id[row["id"]].append(row["tags_text"])
    items = {row["id"]: row for row in spec["items"]}
    priority_lookup = _priority_map_from_spec(spec)
    front_items = []
    if stage4_unlocked:
        for item_id in sorted(front_ids, key=lambda value: _candidate_member_sort_key(priority_lookup, value)):
            front_items.append({
                "id": item_id,
                "style_id": items[item_id]["style_id"],
                "prepared_path": items[item_id]["prepared_path"],
                "priority": copy.deepcopy(items[item_id]["priority"]),
                "tags_texts": tags_by_id.get(item_id, []),
                "release_eligible": False,
                "public_rights_approved": False,
            })
    normalized.update({
        "group_approvals": group_approvals,
        "stage3_similarity_gate_status": "complete" if stage3_complete else "pending_similarity_review",
        "unresolved_similarity_candidate_ids": unresolved_similarity,
        "skipped_similarity_candidate_ids": skipped_similarity,
        "stage4_gate_status": (
            "blocked_pending_duplicate_review" if not stage2_complete
            else "blocked_pending_similarity_review" if not stage3_complete else "unlocked"
        ),
        # Compatibility for the existing private HTML consumer; this is a
        # derived gate result, never another user checkbox or approval input.
        "front_review_complete": stage4_unlocked,
        "front_approval_policy": "explicit_stage4_group_or_individual_approval_after_stage2_and_stage3",
        "private_front_export_items": front_items,
        "private_front_export_status": (
            "blocked_pending_duplicate_review" if not stage2_complete
            else "blocked_pending_similarity_review" if not stage3_complete else "ready"
        ),
    })
    return normalized


def canonicalize_approved_groups(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse only equal/fully contained approved sets, retaining provenance.

    Partial overlaps stay independent; this never creates a union of groups or
    deletes an image. A shared contained source can support multiple maximal
    groups without implying those maximal groups are mutually interchangeable.
    """
    if not isinstance(groups, list):
        raise ValueError("approved groups must be a list")
    rows: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("member_ids"), list):
            raise ValueError("approved group requires member_ids")
        members = group["member_ids"]
        if len(members) < 2 or any(not isinstance(item_id, str) or not item_id for item_id in members) or len(set(members)) != len(members):
            raise ValueError("approved group requires at least two unique image ids")
        candidate_id = _text(group.get("candidate_id") or group.get("group_id") or group.get("id")).strip()
        if not candidate_id:
            raise ValueError("approved group requires a provenance candidate id")
        rows.append({**copy.deepcopy(group), "candidate_id": candidate_id})
    rows.sort(key=lambda row: (-len(row["member_ids"]), row.get("baseline_group") is not True, row["candidate_id"]))
    maximal: list[dict] = []
    for row in rows:
        members = set(row["member_ids"])
        if not any(members <= set(parent["member_ids"]) for parent in maximal):
            maximal.append(row)
    result = []
    for parent in maximal:
        combined = copy.deepcopy(parent)
        members = set(parent["member_ids"])
        contributors = [row for row in rows if set(row["member_ids"]) <= members]
        source_ids: set[str] = set()
        sources: dict[str, dict] = {}
        memos: dict[tuple[str, str], dict] = {}
        for row in contributors:
            source_ids.add(row["candidate_id"])
            raw_ids = row.get("source_candidate_ids", [])
            if not isinstance(raw_ids, list) or any(not isinstance(value, str) or not value for value in raw_ids):
                raise ValueError("source_candidate_ids must be nonempty text ids")
            source_ids.update(raw_ids)
            source_rows = row.get("source_groups") or [{
                "candidate_id": row["candidate_id"], "member_ids": row["member_ids"],
                "memo_text": _text(row.get("memo_text")), "tags_text": _text(row.get("tags_text")),
            }]
            for source in source_rows:
                if not isinstance(source, dict) or not isinstance(source.get("candidate_id"), str):
                    raise ValueError("source_groups contains invalid provenance")
                source_members = source.get("member_ids")
                if not isinstance(source_members, list) or not set(source_members) <= members:
                    raise ValueError("source group provenance must remain within the maximal group")
                payload = {"candidate_id": source["candidate_id"], "member_ids": list(source_members),
                           "memo_text": _text(source.get("memo_text")), "tags_text": _text(source.get("tags_text"))}
                sources[digest(json_bytes(payload))] = payload
                source_ids.add(source["candidate_id"])
                for memo in (payload["memo_text"], payload["tags_text"]):
                    if memo.strip():
                        memos[(source["candidate_id"], memo)] = {"candidate_id": source["candidate_id"], "memo_text": memo}
            for memo in row.get("source_group_memos", []):
                if not isinstance(memo, dict) or not isinstance(memo.get("candidate_id"), str) or not isinstance(memo.get("memo_text"), str):
                    raise ValueError("source_group_memos contains invalid provenance")
                source_ids.add(memo["candidate_id"])
                memos[(memo["candidate_id"], memo["memo_text"])] = copy.deepcopy(memo)
        combined.update({"source_candidate_ids": sorted(source_ids),
            "source_groups": sorted(sources.values(), key=lambda row: (row["candidate_id"], digest(json_bytes(row)))),
            "source_group_memos": [memos[key] for key in sorted(memos)],
            "canonicalization": "fully_contained_or_equal_member_sets_only"})
        result.append(combined)
    return result


def _image_memo(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value.encode("utf-8")) > 8000:
        raise ValueError("personal image memo must be text of at most 8000 UTF-8 bytes")
    return value.strip()


def _image_choice_rows(value: Any, allowed_ids: set[str], *, field: str) -> dict[str, dict]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    result = {}
    for row in value:
        if not isinstance(row, dict):
            raise ValueError(f"{field} rows must be objects")
        item_id = _text(row.get("id")).strip()
        if item_id not in allowed_ids or item_id in result:
            raise ValueError(f"{field} may reference each retained image only once")
        if not isinstance(row.get("approved"), bool):
            raise ValueError(f"{field} approved must be boolean")
        result[item_id] = {"id": item_id, "approved": row["approved"], "memo_text": _image_memo(row.get("memo_text"))}
    return result


def _v3_baseline(spec: dict[str, Any]) -> tuple[set[str], dict[str, dict], list[dict]]:
    baseline = spec.get("baseline", {})
    if not isinstance(baseline, dict):
        raise ValueError("baseline must be an object")
    readonly = baseline.get("read_only_ids", [])
    stage1_ids = set(spec["stage1"]["active_ids"])
    if (not isinstance(readonly, list) or any(not isinstance(item_id, str) for item_id in readonly)
            or len(set(readonly)) != len(readonly) or not set(readonly) <= stage1_ids):
        raise ValueError("baseline read_only_ids must be unique retained source ids")
    choices = _image_choice_rows(baseline.get("image_approvals", []), set(readonly), field="baseline.image_approvals")
    if set(choices) != set(readonly):
        raise ValueError("baseline approvals must exactly cover every read-only image")
    groups = canonicalize_approved_groups(baseline.get("groups", []))
    for group in groups:
        if not set(group["member_ids"]) <= set(readonly):
            raise ValueError("baseline group members must be read-only images")
        group["baseline_group"] = True
    return set(readonly), choices, groups


def _validate_group_workflow_decisions_v3(spec: dict[str, Any], decisions: dict[str, Any]) -> dict[str, Any]:
    if spec.get("approval_policy") != DEFAULT_IMAGE_APPROVAL_POLICY:
        raise ValueError("v3 default image approval requires explicit source-spec opt-in")
    if decisions.get("approval_policy", DEFAULT_IMAGE_APPROVAL_POLICY) != DEFAULT_IMAGE_APPROVAL_POLICY:
        raise ValueError("v3 image approval policy mismatch")
    if decisions.get("group_approvals") or decisions.get("individual_approvals"):
        raise ValueError("v3 uses image_approvals, not legacy group or individual front approvals")
    readonly, baseline_choices, baseline_groups = _v3_baseline(spec)
    legacy_input = copy.deepcopy(decisions)
    legacy_input.update({"schema_version": GROUP_WORKFLOW_DECISIONS_V2_SCHEMA_VERSION,
                         "group_approvals": [], "individual_approvals": []})
    normalized = _validate_group_workflow_decisions_v2(spec, legacy_input, enforce_active_duplicate_selection=True)
    active_ids = normalized["stage2_overlay"]["active_ids"]
    active_set = set(active_ids)
    if not readonly <= active_set:
        raise ValueError("human duplicate review may not delete a read-only baseline image")
    candidate_lookup = _candidate_lookup(spec, "similarity_candidates")
    review_by_id = {row["candidate_id"]: row for row in normalized["similarity_reviews"]}
    skipped = set(normalized["skipped_similarity_candidate_ids"])
    for candidate_id, candidate in candidate_lookup.items():
        member_ids = set(candidate["member_ids"])
        anchors = candidate.get("baseline_anchor_ids", [])
        if (not isinstance(anchors, list) or any(not isinstance(item_id, str) for item_id in anchors)
                or len(set(anchors)) != len(anchors) or not set(anchors) <= member_ids & readonly):
            raise ValueError("candidate baseline anchors must be unique read-only members")
        if member_ids & readonly != set(anchors):
            raise ValueError("every baseline candidate member must be an explicit anchor")
        anchor_set = set(anchors)
        grouped_baseline_ids = {item_id for group in baseline_groups for item_id in group["member_ids"]}
        if anchors and not (
            any(anchor_set == set(group["member_ids"]) for group in baseline_groups)
            or (len(anchors) == 1 and not anchor_set & grouped_baseline_ids)
        ):
            raise ValueError("candidate anchors must be one complete baseline group or an ungrouped image")
        if not (member_ids & active_set) - readonly:
            skipped.add(candidate_id)
        review = review_by_id.get(candidate_id, {})
        if review.get("decision") != "approve_selected":
            continue
        selected = set(review["selected_ids"])
        selected_anchors = selected & readonly
        if selected_anchors and selected_anchors != set(anchors):
            raise ValueError("similarity attachment must select all baseline anchors or none")
        if not selected - readonly:
            raise ValueError("similarity attachment requires at least one new editable image")
        # The declared complete anchor group is indivisible. A different old
        # group may partially overlap it: that is not authorization to union
        # groups, nor a reason to make either group's attachment impossible.
    unresolved = [candidate_id for candidate_id in candidate_lookup
                  if candidate_id not in skipped and review_by_id.get(candidate_id, {}).get("decision") not in {"approve_selected", "keep_separate"}]
    stage2_complete = normalized["stage2_duplicate_gate_status"] == "complete"
    gate_complete = stage2_complete and not unresolved
    seeds = _image_choice_rows(spec.get("initial_image_approvals", []), set(spec["stage1"]["active_ids"]), field="initial_image_approvals")
    submitted = _image_choice_rows(decisions.get("image_approvals", []), active_set, field="image_approvals")
    for item_id in readonly:
        for choices in (seeds, submitted):
            if item_id in choices and choices[item_id] != baseline_choices[item_id]:
                raise ValueError("read-only baseline image approval or memo cannot be changed")
    image_choices = []
    for item_id in active_ids:
        choice = (baseline_choices[item_id] if item_id in readonly else submitted.get(item_id)
                  or seeds.get(item_id) or {"id": item_id, "approved": True, "memo_text": ""})
        image_choices.append(copy.deepcopy(choice))
    selected_groups = normalized["approved_similarity_groups"] + normalized["approved_similarity_groups_pending_gate"]
    raw_reviews = {row["candidate_id"]: row for row in decisions.get("similarity_reviews", [])}
    for review in normalized["similarity_reviews"]:
        review["memo_text"] = _image_memo(raw_reviews[review["candidate_id"]].get("memo_text"))
    for group in selected_groups:
        # Group notes are provenance only; they are never invented as personal
        # image memos or copied into every member's metadata.
        source_review = raw_reviews[group["candidate_id"]]
        group["memo_text"] = _image_memo(source_review.get("memo_text"))
        group["tags_text"] = _bounded_text(source_review.get("tags_text"))
        group.pop("stage4_approved", None)
    combined_groups = canonicalize_approved_groups(baseline_groups + selected_groups)
    items = {row["id"]: row for row in spec["items"]}
    front_items = []
    if gate_complete:
        for choice in image_choices:
            if not choice["approved"]:
                continue
            item = items[choice["id"]]
            front_items.append({"id": choice["id"], "style_id": item["style_id"],
                "prepared_path": item["prepared_path"], "priority": copy.deepcopy(item["priority"]),
                "memo_text": choice["memo_text"], "tags_texts": [choice["memo_text"]] if choice["memo_text"] else [],
                "read_only_baseline": choice["id"] in readonly,
                "release_eligible": False, "public_rights_approved": False})
    normalized.pop("group_approvals", None)
    normalized.pop("individual_approvals", None)
    normalized.update({"schema_version": GROUP_WORKFLOW_DECISIONS_V3_SCHEMA_VERSION,
        "approval_policy": DEFAULT_IMAGE_APPROVAL_POLICY, "image_approvals": image_choices,
        "baseline_read_only_ids": sorted(readonly), "baseline_approved_image_ids": sorted(item_id for item_id, row in baseline_choices.items() if row["approved"]),
        "stage3_similarity_gate_status": "pending_similarity_review" if unresolved else "complete",
        "unresolved_similarity_candidate_ids": unresolved, "skipped_similarity_candidate_ids": sorted(skipped),
        "stage4_gate_status": "blocked_pending_duplicate_review" if not stage2_complete else "blocked_pending_similarity_review" if unresolved else "unlocked",
        "front_review_complete": gate_complete,
        "front_approval_policy": DEFAULT_IMAGE_APPROVAL_POLICY,
        "approved_similarity_groups": combined_groups if gate_complete else baseline_groups,
        "approved_similarity_groups_pending_gate": combined_groups if not gate_complete else [],
        "private_front_export_items": front_items,
        "private_front_export_status": "blocked_pending_duplicate_review" if not stage2_complete else "blocked_pending_similarity_review" if unresolved else "ready",
        "personal_memos_optional": True, "automatic_metadata_tags": False})
    return normalized


def validate_group_workflow_decisions(spec: dict[str, Any], decisions: dict[str, Any]) -> dict[str, Any]:
    if isinstance(decisions, dict) and decisions.get("schema_version") == GROUP_WORKFLOW_DECISIONS_V3_SCHEMA_VERSION:
        return _validate_group_workflow_decisions_v3(spec, decisions)
    return _validate_group_workflow_decisions_v2(spec, decisions)


def _decision_gate_fields(normalized: dict[str, Any]) -> dict[str, Any]:
    """Add v2 diagnostics without changing any already-imported v1 hashes."""
    if normalized["schema_version"] not in {GROUP_WORKFLOW_DECISIONS_V2_SCHEMA_VERSION, GROUP_WORKFLOW_DECISIONS_V3_SCHEMA_VERSION}:
        return {}
    return {
        "decisions_schema_version": normalized["schema_version"],
        "stage3_similarity_gate_status": normalized["stage3_similarity_gate_status"],
        "unresolved_similarity_candidate_ids": normalized["unresolved_similarity_candidate_ids"],
        "skipped_similarity_candidate_ids": normalized["skipped_similarity_candidate_ids"],
        "stage4_gate_status": normalized["stage4_gate_status"],
        "front_approval_policy": normalized["front_approval_policy"],
    }


def _decision_summary(spec: dict[str, Any], normalized: dict[str, Any], *, imported_at: str) -> dict[str, Any]:
    return {
        "schema_version": GROUP_WORKFLOW_DECISION_SUMMARY_SCHEMA_VERSION,
        "imported_at": imported_at,
        "run_id": spec["run_id"],
        "spec_sha256": spec["spec_sha256"],
        "source_manifest_sha256": spec["source_manifest_sha256"],
        "vector_fingerprint": spec["vector_fingerprint"],
        "source_labels_sha256": spec["source_labels_sha256"],
        "reviewer": normalized["reviewer"],
        "reviewed_at": normalized["reviewed_at"],
        "duplicate_candidate_count": len(spec["duplicate_candidates"]),
        "duplicate_review_count": len(normalized["duplicate_reviews"]),
        "unresolved_duplicate_candidate_ids": normalized["unresolved_duplicate_candidate_ids"],
        "stage2_duplicate_gate_status": normalized["stage2_duplicate_gate_status"],
        "approved_similarity_group_count": len(normalized["approved_similarity_groups"]),
        "front_review_complete": normalized["front_review_complete"],
        "private_front_export_count": len(normalized["private_front_export_items"]),
        "private_front_export_status": normalized["private_front_export_status"],
        **_decision_gate_fields(normalized),
        "actual_deletions": 0,
        "source_mutations": False,
        "canonical_writes": 0,
        "public_release_approval": False,
    }


def _decision_receipt(
    spec: dict[str, Any],
    decisions_path: Path,
    destination: Path,
    normalized: dict[str, Any],
    summary: dict[str, Any],
    *,
    has_private_front_html: bool,
) -> dict[str, Any]:
    files = {
        "decisions": str(destination / GROUP_WORKFLOW_DECISIONS_FILENAME),
        "summary": str(destination / GROUP_WORKFLOW_DECISION_SUMMARY_FILENAME),
        "receipt": str(destination / GROUP_WORKFLOW_RECEIPT_FILENAME),
        "retention_overlay": str(destination / GROUP_WORKFLOW_RETENTION_OVERLAY_FILENAME),
        "approved_groups": str(destination / GROUP_WORKFLOW_APPROVED_GROUPS_FILENAME),
        "private_front_export": str(destination / GROUP_WORKFLOW_FRONT_EXPORT_FILENAME),
    }
    if has_private_front_html:
        files["private_front_html"] = str(destination / "private-front.html")
    relative_destination = f"{GROUP_WORKFLOW_DIR}/{GROUP_WORKFLOW_DECISION_IMPORTS_DIR}/{destination.name}"
    files = {key: relative_destination + "/" + Path(value).name for key, value in files.items()}
    return {
        "schema_version": GROUP_WORKFLOW_RECEIPT_SCHEMA_VERSION,
        "status": "import_ready",
        "imported_at": summary["imported_at"],
        "run_id": spec["run_id"],
        "spec_sha256": spec["spec_sha256"],
        "source_manifest_sha256": spec["source_manifest_sha256"],
        "vector_fingerprint": spec["vector_fingerprint"],
        "source_labels_sha256": spec["source_labels_sha256"],
        "decisions_sha256": digest(json_bytes(normalized)),
        **_decision_gate_fields(normalized),
        "reviewer": normalized["reviewer"],
        "reviewed_at": normalized["reviewed_at"],
        "input_decisions_name": decisions_path.name,
        "input_decisions_sha256": digest(decisions_path.read_bytes()),
        "relative_base": "run_directory",
        "stored_dir": relative_destination,
        "files": files,
        "writes": 7 if has_private_front_html else 6,
        "network_calls": 0,
        "actual_deletions": 0,
        "source_mutations": False,
        "canonical_writes": 0,
        "public_release_approval": False,
        "action_plan_only": False,
        "application_scope": "private_retention_and_approved_front_exports_only",
    }


def load_bound_group_workflow_spec(root: Path, source_run_id: str, *, comparison_dir: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    spec_path = _spec_path(Path(root).resolve(), source_run_id)
    if not spec_path.is_file():
        raise ValueError("stored group workflow spec is required")
    stored = read_json(spec_path)
    _validate_stored_spec(stored, source_run_id=source_run_id)
    current = _build_spec(root, source_run_id, comparison_dir=comparison_dir)
    if _normalize_spec_for_drift(stored) != _normalize_spec_for_drift(current):
        raise ValueError("stored group workflow spec no longer matches current source state")
    return stored, load_review_source(Path(root).resolve(), source_run_id, comparison_dir=comparison_dir or DEFAULT_COMPARISON_DIR)


def plan_group_workflow_build(root: Path, source_run_id: str, *, comparison_dir: str | None = None) -> dict[str, Any]:
    spec = _build_spec(root, source_run_id, comparison_dir=comparison_dir)
    return {
        "status": "dry_run",
        "run_id": source_run_id,
        "spec_sha256": spec["spec_sha256"],
        "source_manifest_sha256": spec["source_manifest_sha256"],
        "vector_fingerprint": spec["vector_fingerprint"],
        "source_labels_sha256": spec["source_labels_sha256"],
        "items": len(spec["items"]),
        "duplicate_candidates": len(spec["duplicate_candidates"]),
        "similarity_candidates": len(spec["similarity_candidates"]),
        "network_calls": 0,
        "writes": 0,
    }


def build_group_workflow_artifacts(root: Path, source_run_id: str, *, comparison_dir: str | None = None) -> dict[str, Any]:
    root = Path(root).resolve()
    directory = _workflow_dir(root, source_run_id)
    spec_path = directory / GROUP_WORKFLOW_SPEC_FILENAME
    template_path = directory / GROUP_WORKFLOW_TEMPLATE_FILENAME
    summary_path = directory / GROUP_WORKFLOW_SUMMARY_FILENAME
    html_path = directory / GROUP_WORKFLOW_HTML_FILENAME
    receipt_path = directory / GROUP_WORKFLOW_BUILD_RECEIPT_FILENAME
    generated = _build_spec(root, source_run_id, comparison_dir=comparison_dir)
    spec = generated
    if spec_path.exists():
        existing = read_json(spec_path)
        _validate_stored_spec(existing, source_run_id=source_run_id)
        if _normalize_spec_for_drift(existing) != _normalize_spec_for_drift(generated):
            raise ValueError("refusing to overwrite existing group workflow spec")
        spec = existing
    template = blank_group_workflow_decisions(spec)
    summary = _summary_payload(spec)
    review_html = render_group_review_ui(spec)
    receipt = _build_receipt_payload(spec, directory)
    with run_lock(directory.parent):
        directory.mkdir(parents=True, exist_ok=True)
        _ensure_idempotent_file(spec_path, spec)
        _ensure_idempotent_file(template_path, template)
        _ensure_idempotent_file(summary_path, summary)
        if html_path.exists() and html_path.read_text(encoding="utf-8") != review_html:
            raise ValueError(f"existing workflow artifact differs: {html_path.name}")
        _ensure_idempotent_file(receipt_path, receipt)
        if not spec_path.exists():
            write_json(spec_path, spec)
        if not template_path.exists():
            write_json(template_path, template)
        if not summary_path.exists():
            write_json(summary_path, summary)
        if not html_path.exists():
            html_path.write_text(review_html, encoding="utf-8")
        if not receipt_path.exists():
            write_json(receipt_path, receipt)
    return {
        "status": "ready",
        "run_id": source_run_id,
        "spec_sha256": spec["spec_sha256"],
        "source_labels_sha256": spec["source_labels_sha256"],
        "items": len(spec["items"]),
        "duplicate_candidates": len(spec["duplicate_candidates"]),
        "similarity_candidates": len(spec["similarity_candidates"]),
        "network_calls": 0,
        "writes": 5,
        "spec_path": str(spec_path),
        "template_path": str(template_path),
        "summary_path": str(summary_path),
        "html_path": str(html_path),
        "receipt_path": str(receipt_path),
    }


def _render_duplicate_candidate(candidate: dict[str, Any], items: dict[str, dict[str, Any]]) -> str:
    choices = "".join(
        f'<label><input type="checkbox" data-dup-member="{html.escape(member_id, quote=True)}" data-candidate-id="{html.escape(candidate["id"], quote=True)}"> {html.escape(items[member_id]["style_id"])} <small>{html.escape(member_id)}</small></label>'
        for member_id in candidate["member_ids"]
    )
    return (
        f'<article class="candidate"><h3>중복 후보</h3><p>{html.escape(candidate["id"])}</p>'
        f'<p>대표 제안: {html.escape(candidate["suggested_representative_id"])}</p>'
        f'<label><input type="radio" name="dup-{html.escape(candidate["id"], quote=True)}" value="same_image_subset"> 선택 항목은 같은 이미지</label>'
        f'<label><input type="radio" name="dup-{html.escape(candidate["id"], quote=True)}" value="distinct_images"> 모두 다른 이미지</label>'
        f'<label><input type="radio" name="dup-{html.escape(candidate["id"], quote=True)}" value="defer" checked> 보류</label>'
        f'<div class="members">{choices}</div>'
        f'<label><input type="checkbox" data-dup-remainder="{html.escape(candidate["id"], quote=True)}"> 선택하지 않은 나머지는 서로 다른 이미지임</label>'
        "</article>"
    )


def _render_similarity_candidate(candidate: dict[str, Any], items: dict[str, dict[str, Any]]) -> str:
    choices = "".join(
        f'<label><input type="checkbox" data-sim-member="{html.escape(member_id, quote=True)}" data-candidate-id="{html.escape(candidate["id"], quote=True)}"> {html.escape(items[member_id]["style_id"])} <small>{html.escape(member_id)}</small></label>'
        for member_id in candidate["member_ids"]
    )
    return (
        f'<article class="candidate"><h3>유사 그룹 후보</h3><p>{html.escape(candidate["id"])}</p>'
        f'<label><input type="radio" name="sim-{html.escape(candidate["id"], quote=True)}" value="approve_selected"> 선택 항목 그룹 승인</label>'
        f'<label><input type="radio" name="sim-{html.escape(candidate["id"], quote=True)}" value="defer" checked> 보류</label>'
        f'<div class="members">{choices}</div>'
        f'<label>태그 <input type="text" data-sim-tags="{html.escape(candidate["id"], quote=True)}"></label>'
        "</article>"
    )


def render_group_review(spec: dict[str, Any]) -> str:
    return render_group_review_ui(spec)


def refresh_group_workflow_html(root: Path, source_run_id: str, *, apply: bool = False) -> dict[str, Any]:
    """Refresh display code only, preserving bound decisions and the prior HTML.

    No source/spec/label changes or approvals are made here. Decision import
    separately performs the full live source validation before application.
    """
    directory = _workflow_dir(Path(root).resolve(), source_run_id)
    spec_path = directory / GROUP_WORKFLOW_SPEC_FILENAME
    html_path = directory / GROUP_WORKFLOW_HTML_FILENAME
    spec = read_json(spec_path)
    _validate_stored_spec(spec, source_run_id=source_run_id)
    previous = html_path.read_bytes()
    replacement = render_group_review_ui(spec).encode("utf-8")
    before_sha, after_sha = digest(previous), digest(replacement)
    changed = before_sha != after_sha
    result = {"status": "dry_run" if not apply else ("refreshed" if changed else "unchanged"),
              "run_id": source_run_id, "spec_sha256": spec["spec_sha256"],
              "before_html_sha256": before_sha, "after_html_sha256": after_sha,
              "changed": changed, "network_calls": 0, "writes": 0,
              "spec_changed": False, "labels_changed": False, "html_path": str(html_path)}
    if not apply or not changed:
        return result
    revision_dir = directory / "html-revisions"
    backup_path = revision_dir / (before_sha[:16] + ".html")
    with run_lock(directory.parent):
        if read_json(spec_path) != spec or digest(html_path.read_bytes()) != before_sha:
            raise ValueError("review changed during HTML refresh; retry after inspection")
        revision_dir.mkdir(parents=True, exist_ok=True)
        if backup_path.exists() and backup_path.read_bytes() != previous:
            raise ValueError("existing HTML revision digest conflict")
        if not backup_path.exists():
            backup_path.write_bytes(previous)
        html_path.write_bytes(replacement)
        result["writes"] = 3
        result["previous_html_path"] = str(backup_path)
        write_json(revision_dir / (before_sha[:16] + "-to-" + after_sha[:16] + ".json"), result)
    return result


def _existing_imported_at(destination: Path) -> str | None:
    for path in (destination / GROUP_WORKFLOW_RECEIPT_FILENAME, destination / GROUP_WORKFLOW_DECISION_SUMMARY_FILENAME):
        if path.is_file():
            payload = read_json(path)
            imported_at = _text(payload.get("imported_at")).strip()
            if imported_at:
                return imported_at
    return None


def import_group_workflow_decisions(
    root: Path,
    source_run_id: str,
    decisions_path: Path,
    *,
    apply: bool = False,
    comparison_dir: str | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    decisions_path = Path(decisions_path)
    if not decisions_path.is_file():
        raise FileNotFoundError("group workflow decisions file not found")
    spec, _source = load_bound_group_workflow_spec(root, source_run_id, comparison_dir=comparison_dir)
    submitted = read_json(decisions_path)
    normalized = validate_group_workflow_decisions(spec, submitted)
    decisions_sha256 = digest(json_bytes(normalized))
    # Keep deep Windows artifact paths below MAX_PATH. Full digest remains in
    # the receipt; all existing content is compared before any idempotent write.
    destination = _workflow_dir(root, source_run_id) / GROUP_WORKFLOW_DECISION_IMPORTS_DIR / decisions_sha256[:24]
    imported_at = _existing_imported_at(destination) or now()
    summary = _decision_summary(spec, normalized, imported_at=imported_at)
    overlay = normalized["stage2_overlay"]
    approved_groups = {
        "schema_version": GROUP_WORKFLOW_APPROVED_GROUPS_SCHEMA_VERSION,
        "run_id": spec["run_id"],
        "spec_sha256": spec["spec_sha256"],
        "groups": normalized["approved_similarity_groups"],
        "pending_gate_groups": normalized["approved_similarity_groups_pending_gate"],
        "stage2_duplicate_gate_status": normalized["stage2_duplicate_gate_status"],
        "candidate_only": True,
        **_decision_gate_fields(normalized),
        "source_mutations": False,
    }
    front_export = {
        "schema_version": GROUP_WORKFLOW_FRONT_EXPORT_SCHEMA_VERSION,
        "run_id": spec["run_id"],
        "spec_sha256": spec["spec_sha256"],
        "front_review_complete": normalized["front_review_complete"],
        "stage2_duplicate_gate_status": normalized["stage2_duplicate_gate_status"],
        "status": normalized["private_front_export_status"],
        **_decision_gate_fields(normalized),
        "items": normalized["private_front_export_items"],
        "release_eligible": False,
        "public_rights_approved": False,
        "source_mutations": False,
        "canonical_writes": 0,
    }
    private_front_html: str | None = None
    try:
        from .approved_front import render_approved_front

        private_front_html = render_approved_front(front_export, approved_groups)
    except ModuleNotFoundError:
        private_front_html = None
    receipt = _decision_receipt(
        spec,
        decisions_path,
        destination,
        normalized,
        summary,
        has_private_front_html=private_front_html is not None,
    )
    _ensure_idempotent_file(destination / GROUP_WORKFLOW_DECISIONS_FILENAME, normalized)
    _ensure_idempotent_file(destination / GROUP_WORKFLOW_DECISION_SUMMARY_FILENAME, summary)
    _ensure_idempotent_file(destination / GROUP_WORKFLOW_RECEIPT_FILENAME, receipt)
    _ensure_idempotent_file(destination / GROUP_WORKFLOW_RETENTION_OVERLAY_FILENAME, overlay)
    _ensure_idempotent_file(destination / GROUP_WORKFLOW_APPROVED_GROUPS_FILENAME, approved_groups)
    _ensure_idempotent_file(destination / GROUP_WORKFLOW_FRONT_EXPORT_FILENAME, front_export)
    if private_front_html is not None and (destination / "private-front.html").exists():
        existing_html = (destination / "private-front.html").read_text(encoding="utf-8")
        if existing_html != private_front_html:
            raise ValueError("existing workflow artifact differs: private-front.html")
    result = {
        "status": "dry_run" if not apply else "imported",
        "run_id": source_run_id,
        "spec_sha256": spec["spec_sha256"],
        "decisions_sha256": decisions_sha256,
        "import_dir": str(destination),
        "stage2_duplicate_gate_status": normalized["stage2_duplicate_gate_status"],
        "unresolved_duplicate_candidates": len(normalized["unresolved_duplicate_candidate_ids"]),
        "approved_similarity_groups": len(normalized["approved_similarity_groups"]),
        "private_front_export_status": normalized["private_front_export_status"],
        "private_front_export_count": len(normalized["private_front_export_items"]),
        **_decision_gate_fields(normalized),
        "actual_deletions": 0,
        "source_mutations": False,
        "canonical_writes": 0,
        "public_release_approval": False,
        "network_calls": 0,
        "writes": 0 if not apply else (7 if private_front_html is not None else 6),
    }
    if not apply:
        return result
    with run_lock(destination.parent.parent):
        destination.mkdir(parents=True, exist_ok=True)
        if not (destination / GROUP_WORKFLOW_DECISIONS_FILENAME).exists():
            write_json(destination / GROUP_WORKFLOW_DECISIONS_FILENAME, normalized)
        if not (destination / GROUP_WORKFLOW_DECISION_SUMMARY_FILENAME).exists():
            write_json(destination / GROUP_WORKFLOW_DECISION_SUMMARY_FILENAME, summary)
        if not (destination / GROUP_WORKFLOW_RECEIPT_FILENAME).exists():
            write_json(destination / GROUP_WORKFLOW_RECEIPT_FILENAME, receipt)
        if not (destination / GROUP_WORKFLOW_RETENTION_OVERLAY_FILENAME).exists():
            write_json(destination / GROUP_WORKFLOW_RETENTION_OVERLAY_FILENAME, overlay)
        if not (destination / GROUP_WORKFLOW_APPROVED_GROUPS_FILENAME).exists():
            write_json(destination / GROUP_WORKFLOW_APPROVED_GROUPS_FILENAME, approved_groups)
        if not (destination / GROUP_WORKFLOW_FRONT_EXPORT_FILENAME).exists():
            write_json(destination / GROUP_WORKFLOW_FRONT_EXPORT_FILENAME, front_export)
        if private_front_html is not None and not (destination / "private-front.html").exists():
            (destination / "private-front.html").write_text(private_front_html, encoding="utf-8")
    return result


__all__ = [
    "GROUP_WORKFLOW_DIR",
    "GROUP_WORKFLOW_SPEC_FILENAME",
    "GROUP_WORKFLOW_TEMPLATE_FILENAME",
    "GROUP_WORKFLOW_SUMMARY_FILENAME",
    "GROUP_WORKFLOW_SPEC_SCHEMA_VERSION",
    "GROUP_WORKFLOW_DECISIONS_SCHEMA_VERSION",
    "GROUP_WORKFLOW_DECISIONS_V2_SCHEMA_VERSION",
    "GROUP_WORKFLOW_DECISIONS_V3_SCHEMA_VERSION",
    "DEFAULT_IMAGE_APPROVAL_POLICY",
    "canonicalize_approved_groups",
    "blank_group_workflow_decisions",
    "build_group_workflow_artifacts",
    "import_group_workflow_decisions",
    "load_bound_group_workflow_spec",
    "plan_group_workflow_build",
    "render_group_review",
    "validate_group_workflow_decisions",
]
