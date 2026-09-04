from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .action_planning import build_action_plan
from .experiment import digest, json_bytes, now, read_json, run_lock, run_path, write_json
from .human_review import (
    DEFAULT_COMPARISON_DIR,
    MIN_THRESHOLD_LABELS,
    REVIEW_LABELS_SCHEMA_VERSION,
    REVIEW_SPEC_SCHEMA_VERSION,
    build_review_spec,
    load_review_source,
    summarize_thresholds,
    validate_review_labels,
)


SPEC_FILENAME = "human-similarity-review.spec.json"
IMPORT_ROOT = "human-label-reviews"
LABEL_SUMMARY_SCHEMA_VERSION = "image-similarity-review-import-summary-1"
LABEL_RECEIPT_SCHEMA_VERSION = "image-similarity-review-import-receipt-1"
REVIEW_LABELS_SCHEMA_VERSION_V2 = "image-similarity-review-labels-2"


def _stored_spec_path(root: Path, source_run_id: str) -> Path:
    return run_path(root, source_run_id) / SPEC_FILENAME


def _normalized_spec_for_hash(spec: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(spec)
    normalized.pop("review_spec_sha256", None)
    return normalized


def _normalized_spec_for_drift(spec: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalized_spec_for_hash(spec)
    normalized.pop("created_at", None)
    return normalized


def _validate_stored_spec(spec: dict[str, Any], *, source_run_id: str) -> None:
    expected = digest(json_bytes(_normalized_spec_for_hash(spec)))
    actual = str(spec.get("review_spec_sha256") or "").strip()
    if not actual or actual != expected:
        raise ValueError("stored review spec self-hash mismatch")
    if str(spec.get("run_id") or "").strip() != source_run_id:
        raise ValueError("stored review spec run_id mismatch")
    if str(spec.get("schema_version") or "").strip() != REVIEW_SPEC_SCHEMA_VERSION:
        raise ValueError("stored review spec schema_version mismatch")


def load_bound_review_spec(root: Path, source_run_id: str, *, comparison_dir: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    spec_path = _stored_spec_path(root, source_run_id)
    if not spec_path.is_file():
        raise ValueError("stored review spec is required before label import")
    stored_spec = read_json(spec_path)
    _validate_stored_spec(stored_spec, source_run_id=source_run_id)
    stored_comparison_dir = str(stored_spec.get("comparison_dir") or "").strip() or DEFAULT_COMPARISON_DIR
    if comparison_dir is not None and comparison_dir != stored_comparison_dir:
        raise ValueError("requested comparison dir does not match stored review spec")
    source = load_review_source(root, source_run_id, comparison_dir=stored_comparison_dir)
    current_spec = build_review_spec(
        source,
        max_pairs=int(stored_spec["sampling_strategy"]["max_pairs"]),
        seed=str(stored_spec.get("sampling_seed") or ""),
    )
    if _normalized_spec_for_drift(stored_spec) != _normalized_spec_for_drift(current_spec):
        raise ValueError("stored review spec no longer matches current source/manifest/vector state")
    return stored_spec, source


def load_bound_review_spec_v2(root: Path, source_run_id: str, *, comparison_dir: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    from . import human_review_v2 as review_v2

    stored_spec, _stored_v1_spec, source = review_v2.load_bound_review_spec_v2(
        root,
        source_run_id,
        comparison_dir=comparison_dir,
    )
    return stored_spec, source


def validate_review_labels_v2(spec: dict[str, Any], labels: dict[str, Any]) -> dict[str, Any]:
    from . import human_review_v2 as review_v2

    return review_v2.validate_review_labels_v2(spec, labels)


def _threshold_histogram(spec: dict[str, Any], labels: dict[str, Any]) -> dict[str, Any]:
    pair_lookup = {pair["pair_id"]: pair for pair in spec["pairs"]}
    labeled = [row for row in labels["pairs"] if row["human_label"] is not None]
    bins = [
        ("lt_0_75", None, 0.75),
        ("0_75_to_lt_0_80", 0.75, 0.80),
        ("0_80_to_lt_0_85", 0.80, 0.85),
        ("0_85_to_lt_0_90", 0.85, 0.90),
        ("0_90_to_lt_0_95", 0.90, 0.95),
        ("gte_0_95", 0.95, None),
    ]
    rows = []
    for name, lower, upper in bins:
        matched = []
        for row in labeled:
            cosine = float(pair_lookup[row["pair_id"]]["voyage_cosine"])
            if lower is not None and cosine < lower:
                continue
            if upper is not None and cosine >= upper:
                continue
            matched.append(row)
        label_counts: dict[str, int] = {}
        unresolved = 0
        verified = 0
        for row in matched:
            label = row["human_label"]
            if label == "unsure":
                unresolved += 1
            if row["human_verified"] is True and label is not None and label != "unsure":
                verified += 1
                label_counts[str(label)] = label_counts.get(str(label), 0) + 1
        rows.append(
            {
                "bin": name,
                "lower_bound_inclusive": lower,
                "upper_bound_exclusive": upper,
                "labeled_pairs": len(matched),
                "verified_pairs": verified,
                "unresolved_pairs": unresolved,
                "verified_label_counts": label_counts,
            }
        )
    pair_rows = [
        {
            "pair_id": row["pair_id"],
            "sampling_bucket": pair_lookup[row["pair_id"]].get("sampling_bucket"),
            "voyage_cosine": pair_lookup[row["pair_id"]]["voyage_cosine"],
            "human_label": row["human_label"],
            "human_verified": row["human_verified"],
        }
        for row in labeled
    ]
    pair_rows.sort(key=lambda row: (-float(row["voyage_cosine"]), str(row["pair_id"])))
    return {
        "schema_version": "image-similarity-threshold-histogram-1",
        "bins": rows,
        "pair_rows": pair_rows,
        "note": "Sample-only histogram for reviewed pairs. Not an operating threshold approval.",
    }


def _existing_imported_at(destination: Path) -> str | None:
    for path in (destination / "receipt.json", destination / "summary.json"):
        if path.is_file():
            payload = read_json(path)
            imported_at = str(payload.get("imported_at") or "").strip()
            if imported_at:
                return imported_at
    return None


def _summary_payload(
    spec: dict[str, Any],
    normalized_labels: dict[str, Any],
    *,
    minimum_verified_pairs: int,
    imported_at: str,
) -> dict[str, Any]:
    if normalized_labels.get("schema_version") == REVIEW_LABELS_SCHEMA_VERSION_V2:
        from . import human_review_v2 as review_v2

        threshold_summary = review_v2.summarize_thresholds_v2(
            spec,
            normalized_labels,
            minimum_verified_pairs=minimum_verified_pairs,
        )
    else:
        threshold_summary = summarize_thresholds(spec, normalized_labels, minimum_verified_pairs=minimum_verified_pairs)
    return {
        "schema_version": LABEL_SUMMARY_SCHEMA_VERSION,
        "imported_at": imported_at,
        "run_id": spec["run_id"],
        "comparison_dir": spec["comparison_dir"],
        "provider": spec["provider"],
        "model": spec["model"],
        "dimensions": spec["dimensions"],
        "review_spec_sha256": spec["review_spec_sha256"],
        "labels_sha256": digest(json_bytes(normalized_labels)),
        "source_manifest_sha256": spec["source_manifest_sha256"],
        "vector_fingerprint": spec["vector_fingerprint"],
        "reviewer": normalized_labels["reviewer"],
        "reviewed_at": normalized_labels["reviewed_at"],
        "sampling_seed": spec["sampling_seed"],
        "sampled_pairs": spec["counts"]["sampled_pairs"],
        "total_pairs": spec["counts"]["total_pairs"],
        "threshold_summary": threshold_summary,
        "threshold_histogram": _threshold_histogram(spec, normalized_labels),
        "automatic_threshold_selection": None,
        "human_verified_scope": "pair_only",
        "unsure_status": "unresolved",
        "network_calls": 0,
    }


def _receipt_payload(
    *,
    source_run_id: str,
    labels_path: Path,
    destination: Path,
    spec: dict[str, Any],
    normalized_labels: dict[str, Any],
    summary: dict[str, Any],
    action_plan: dict[str, Any] | None,
) -> dict[str, Any]:
    files = {
        "labels": str(destination / "labels.json"),
        "summary": str(destination / "summary.json"),
        "receipt": str(destination / "receipt.json"),
    }
    if action_plan is not None:
        files["action_plan"] = str(destination / "action-plan.json")
    return {
        "schema_version": LABEL_RECEIPT_SCHEMA_VERSION,
        "status": "import_ready",
        "imported_at": summary["imported_at"],
        "run_id": source_run_id,
        "comparison_dir": spec["comparison_dir"],
        "review_spec_sha256": spec["review_spec_sha256"],
        "labels_sha256": summary["labels_sha256"],
        "source_manifest_sha256": spec["source_manifest_sha256"],
        "vector_fingerprint": spec["vector_fingerprint"],
        "reviewer": normalized_labels["reviewer"],
        "reviewed_at": normalized_labels["reviewed_at"],
        "input_labels_path": str(labels_path),
        "stored_dir": str(destination),
        "files": files,
        "writes": 4 if action_plan is not None else 3,
        "network_calls": 0,
        "current_binding_verified": True,
        "spec_self_hash_verified": True,
        "source_mutations": False,
        "canonical_writes": 0,
        "public_release_approval": False,
        "action_plan_only": action_plan is not None,
        "actual_deletions": 0,
    }


def _ensure_idempotent_file(path: Path, payload: dict[str, Any]) -> None:
    if path.exists() and digest(json_bytes(read_json(path))) != digest(json_bytes(payload)):
        raise ValueError(f"existing import artifact differs: {path.name}")


def _resolved_pair_state(row: dict[str, Any]) -> tuple[str, bool, dict[str, Any]] | None:
    label = row.get("human_label")
    if row.get("human_verified") is True and label is not None and label != "unsure":
        return str(label), True, copy.deepcopy(row.get("dimensions", {}))
    return None


def _v2_target_by_pair(action_plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for row in action_plan.get("pair_decisions", []):
        if not isinstance(row, dict):
            continue
        pair_id = str(row.get("pair_id") or "").strip()
        if pair_id:
            lookup[pair_id] = copy.deepcopy(row.get("target", {}))
    return lookup


def _resolved_pair_state_v2(row: dict[str, Any], *, target_lookup: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    label = row.get("human_label")
    action = row.get("action")
    if row.get("human_verified") is not True or label is None or label == "unsure":
        return None
    return {
        "label": str(label),
        "action": str(action),
        "dimensions": copy.deepcopy(row.get("dimensions", {})),
        "target": copy.deepcopy(target_lookup.get(str(row.get("pair_id") or ""), {})),
    }


def _validate_existing_import_conflicts(
    root: Path,
    source_run_id: str,
    *,
    review_spec_sha256: str,
    labels_sha256: str,
    normalized_labels: dict[str, Any],
) -> None:
    import_root = run_path(root, source_run_id) / IMPORT_ROOT
    if not import_root.is_dir():
        return
    current_reviewer = str(normalized_labels.get("reviewer") or "").strip()
    current_pairs = {str(row["pair_id"]): row for row in normalized_labels.get("pairs", [])}
    for receipt_path in sorted(import_root.glob("*/receipt.json")):
        payload = read_json(receipt_path)
        if str(payload.get("run_id") or "").strip() != source_run_id:
            continue
        if str(payload.get("review_spec_sha256") or "").strip() != review_spec_sha256:
            continue
        if str(payload.get("labels_sha256") or "").strip() == labels_sha256:
            continue
        prior_labels_path = receipt_path.parent / "labels.json"
        if not prior_labels_path.is_file():
            raise ValueError("existing import receipt is missing labels.json")
        prior_labels = read_json(prior_labels_path)
        prior_reviewer = str(prior_labels.get("reviewer") or "").strip()
        if prior_reviewer and current_reviewer and prior_reviewer != current_reviewer:
            raise ValueError("different reviewer imports for the same review spec are not supported")
        for prior_row in prior_labels.get("pairs", []):
            pair_id = str(prior_row.get("pair_id") or "").strip()
            if not pair_id:
                raise ValueError("existing imported labels contain invalid pair_id")
            prior_resolved = _resolved_pair_state(prior_row)
            if prior_resolved is None:
                continue
            current_row = current_pairs.get(pair_id)
            if current_row is None:
                raise ValueError("current labels are missing a previously resolved pair")
            if _resolved_pair_state(current_row) != prior_resolved:
                raise ValueError("conflicting labels already imported for this review spec")


def _validate_existing_import_conflicts_v2(
    root: Path,
    source_run_id: str,
    *,
    review_spec_sha256: str,
    labels_sha256: str,
    normalized_labels: dict[str, Any],
    action_plan: dict[str, Any],
) -> None:
    import_root = run_path(root, source_run_id) / IMPORT_ROOT
    if not import_root.is_dir():
        return
    current_reviewer = str(normalized_labels.get("reviewer") or "").strip()
    current_pairs = {str(row["pair_id"]): row for row in normalized_labels.get("pairs", [])}
    current_targets = _v2_target_by_pair(action_plan)
    for receipt_path in sorted(import_root.glob("*/receipt.json")):
        payload = read_json(receipt_path)
        if str(payload.get("run_id") or "").strip() != source_run_id:
            continue
        if str(payload.get("review_spec_sha256") or "").strip() != review_spec_sha256:
            continue
        if str(payload.get("labels_sha256") or "").strip() == labels_sha256:
            continue
        prior_labels_path = receipt_path.parent / "labels.json"
        prior_plan_path = receipt_path.parent / "action-plan.json"
        if not prior_labels_path.is_file():
            raise ValueError("existing import receipt is missing labels.json")
        if not prior_plan_path.is_file():
            raise ValueError("existing import receipt is missing action-plan.json")
        prior_labels = read_json(prior_labels_path)
        prior_plan = read_json(prior_plan_path)
        prior_reviewer = str(prior_labels.get("reviewer") or "").strip()
        if prior_reviewer and current_reviewer and prior_reviewer != current_reviewer:
            raise ValueError("different reviewer imports for the same review spec are not supported")
        prior_targets = _v2_target_by_pair(prior_plan)
        for prior_row in prior_labels.get("pairs", []):
            pair_id = str(prior_row.get("pair_id") or "").strip()
            if not pair_id:
                raise ValueError("existing imported labels contain invalid pair_id")
            prior_resolved = _resolved_pair_state_v2(prior_row, target_lookup=prior_targets)
            if prior_resolved is None:
                continue
            current_row = current_pairs.get(pair_id)
            if current_row is None:
                raise ValueError(f"current labels are missing previously resolved pair {pair_id}")
            current_resolved = _resolved_pair_state_v2(current_row, target_lookup=current_targets)
            if current_resolved != prior_resolved:
                raise ValueError(
                    "conflicting labels already imported for pair "
                    f"{pair_id}: prior action={prior_resolved['action']} prior_target={prior_resolved['target']} "
                    f"current action={current_resolved['action'] if current_resolved else None} "
                    f"current_target={current_resolved['target'] if current_resolved else None}"
                )


def _first_conflict_message(action_plan: dict[str, Any]) -> str:
    conflicts = action_plan.get("conflicts")
    if not isinstance(conflicts, list) or not conflicts:
        return "action plan is blocked"
    first = conflicts[0]
    if not isinstance(first, dict):
        return "action plan is blocked"
    return str(first.get("message") or first.get("kind") or "action plan is blocked")


def import_review_labels(
    root: Path,
    source_run_id: str,
    labels_path: Path,
    *,
    apply: bool = False,
    minimum_verified_pairs: int = MIN_THRESHOLD_LABELS,
    comparison_dir: str | None = None,
) -> dict[str, Any]:
    if minimum_verified_pairs < 1:
        raise ValueError("minimum_verified_pairs must be >= 1")
    root = Path(root).resolve()
    labels_path = Path(labels_path)
    if not labels_path.is_file():
        raise FileNotFoundError("labels file not found")
    submitted_labels = read_json(labels_path)
    schema_version = str(submitted_labels.get("schema_version") or "").strip()

    action_plan: dict[str, Any] | None = None
    if schema_version == REVIEW_LABELS_SCHEMA_VERSION_V2:
        spec, source = load_bound_review_spec_v2(root, source_run_id, comparison_dir=comparison_dir)
        normalized_labels = validate_review_labels_v2(spec, submitted_labels)
        labels_sha256 = digest(json_bytes(normalized_labels))
        action_plan = build_action_plan(source, spec, normalized_labels)
        action_plan["labels_sha256"] = labels_sha256
        for row in normalized_labels["pairs"]:
            if row.get("action") != "delete_duplicate":
                continue
            pair_id = str(row["pair_id"])
            target = next(
                (decision.get("target", {}) for decision in action_plan.get("pair_decisions", []) if decision.get("pair_id") == pair_id),
                {},
            )
            suggestion = row.get("retention_suggestion") if isinstance(row.get("retention_suggestion"), dict) else {}
            expected_keep_id = suggestion.get("keep_id")
            expected_delete_id = suggestion.get("delete_id")
            if target.get("keep_id") is not None and expected_keep_id is not None and target.get("keep_id") != expected_keep_id:
                raise ValueError("delete_duplicate keep_id does not match deterministic plan target")
            if target.get("delete_id") is not None and expected_delete_id is not None and target.get("delete_id") != expected_delete_id:
                raise ValueError("delete_duplicate delete_id does not match deterministic plan target")
        if action_plan.get("status") == "blocked":
            raise ValueError(f"action plan blocked: {_first_conflict_message(action_plan)}")
    elif schema_version == REVIEW_LABELS_SCHEMA_VERSION:
        spec, _source = load_bound_review_spec(root, source_run_id, comparison_dir=comparison_dir)
        normalized_labels = validate_review_labels(spec, submitted_labels)
        labels_sha256 = digest(json_bytes(normalized_labels))
    else:
        raise ValueError("unknown human review labels schema")

    destination = run_path(root, source_run_id) / IMPORT_ROOT / labels_sha256
    imported_at = _existing_imported_at(destination) or now()
    summary = _summary_payload(
        spec,
        normalized_labels,
        minimum_verified_pairs=minimum_verified_pairs,
        imported_at=imported_at,
    )
    receipt = _receipt_payload(
        source_run_id=source_run_id,
        labels_path=labels_path,
        destination=destination,
        spec=spec,
        normalized_labels=normalized_labels,
        summary=summary,
        action_plan=action_plan,
    )
    labels_target = destination / "labels.json"
    summary_target = destination / "summary.json"
    receipt_target = destination / "receipt.json"
    action_plan_target = destination / "action-plan.json"
    if schema_version == REVIEW_LABELS_SCHEMA_VERSION_V2:
        _validate_existing_import_conflicts_v2(
            root,
            source_run_id,
            review_spec_sha256=spec["review_spec_sha256"],
            labels_sha256=labels_sha256,
            normalized_labels=normalized_labels,
            action_plan=action_plan or {},
        )
    else:
        _validate_existing_import_conflicts(
            root,
            source_run_id,
            review_spec_sha256=spec["review_spec_sha256"],
            labels_sha256=labels_sha256,
            normalized_labels=normalized_labels,
        )
    _ensure_idempotent_file(labels_target, normalized_labels)
    _ensure_idempotent_file(summary_target, summary)
    _ensure_idempotent_file(receipt_target, receipt)
    if action_plan is not None:
        _ensure_idempotent_file(action_plan_target, action_plan)

    result = {
        "status": "dry_run" if not apply else "imported",
        "run_id": source_run_id,
        "review_spec_sha256": spec["review_spec_sha256"],
        "labels_sha256": labels_sha256,
        "source_manifest_sha256": spec["source_manifest_sha256"],
        "vector_fingerprint": spec["vector_fingerprint"],
        "import_dir": str(destination),
        "threshold_status": summary["threshold_summary"]["status"],
        "verified_pairs": summary["threshold_summary"]["verified_pairs"],
        "unresolved_pairs": summary["threshold_summary"]["unresolved_pairs"],
        "network_calls": 0,
        "writes": 0 if not apply else (4 if action_plan is not None else 3),
        "automatic_threshold_selection": None,
    }
    if action_plan is not None:
        result.update(
            {
                "action_plan_status": action_plan["status"],
                "action_plan_only": True,
                "action_plan_path": str(action_plan_target),
                "actual_deletions": 0,
                "comparison_changed": False,
                "canonical_changed": False,
            }
        )
    if not apply:
        return result
    with run_lock(run_path(root, source_run_id).parent), run_lock(run_path(root, source_run_id)):
        destination.mkdir(parents=True, exist_ok=True)
        if not labels_target.exists():
            write_json(labels_target, normalized_labels)
        if not summary_target.exists():
            write_json(summary_target, summary)
        if not receipt_target.exists():
            write_json(receipt_target, receipt)
        if action_plan is not None and not action_plan_target.exists():
            write_json(action_plan_target, action_plan)
    return result


__all__ = ["import_review_labels", "load_bound_review_spec", "load_bound_review_spec_v2", "validate_review_labels_v2"]
