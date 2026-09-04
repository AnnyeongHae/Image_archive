from __future__ import annotations

import math
from collections import Counter
from typing import Any

from .human_review_v2 import validate_review_labels_v2


SCHEMA_VERSION = "image-similarity-calibration-1"
VISUAL_POSITIVE_LABELS = {"identical", "near_duplicate", "same_visual_family"}
NEGATIVE_LABELS = {"same_theme_only", "unrelated"}


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def _strictly_above_hundredth(value: float | None) -> float | None:
    if value is None:
        return None
    return round((math.floor(value * 100.0) + 1.0) / 100.0, 2)


def _round_down_hundredth(value: float | None) -> float | None:
    if value is None:
        return None
    return round(math.floor(value * 100.0) / 100.0, 2)


def _threshold_row(name: str, rows: list[dict[str, Any]], *, lower: float | None, upper: float | None) -> dict[str, Any]:
    selected = []
    for row in rows:
        cosine = row["voyage_cosine"]
        if lower is not None and cosine < lower:
            continue
        if upper is not None and cosine >= upper:
            continue
        selected.append(row)
    return {
        "name": name,
        "lower_bound_inclusive": lower,
        "upper_bound_exclusive": upper,
        "observed_pairs": len(selected),
        "observed_visual_positive_pairs": sum(
            1 for row in selected if row["verified"] and row["label"] in VISUAL_POSITIVE_LABELS
        ),
        "observed_negative_pairs": sum(1 for row in selected if row["verified"] and row["label"] in NEGATIVE_LABELS),
        "observed_unlabeled_pairs": sum(
            1 for row in selected if row["label"] is None or row["label"] == "unsure" or row["verified"] is not True
        ),
    }


def _threshold_at_or_above(rows: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
    selected = [row for row in rows if row["voyage_cosine"] >= threshold]
    positives = sum(1 for row in selected if row["verified"] and row["label"] in VISUAL_POSITIVE_LABELS)
    negatives = sum(1 for row in selected if row["verified"] and row["label"] in NEGATIVE_LABELS)
    labeled = positives + negatives
    return {
        "threshold": threshold,
        "observed_pairs_at_or_above_threshold": len(selected),
        "observed_visual_positive_pairs": positives,
        "observed_negative_pairs": negatives,
        "observed_unlabeled_pairs": sum(
            1 for row in selected if row["label"] is None or row["label"] == "unsure" or row["verified"] is not True
        ),
        "observed_visual_positive_rate": round(positives / labeled, 6) if labeled else None,
    }


def calibrate(source: dict[str, Any], spec: dict[str, Any], labels: dict[str, Any]) -> dict[str, Any]:
    normalized = validate_review_labels_v2(spec, labels)
    retention = source.get("retention") if isinstance(source.get("retention"), dict) else {}
    active_ids = {_text(item_id).strip() for item_id in retention.get("active_ids", []) if _text(item_id).strip()}
    archived_ids = {
        _text(row.get("id")).strip()
        for row in retention.get("archived", [])
        if isinstance(row, dict) and _text(row.get("id")).strip()
    }
    spec_by_pair_id = {
        _text(pair.get("pair_id")).strip(): pair for pair in spec.get("pairs", []) if isinstance(pair, dict)
    }

    rows: list[dict[str, Any]] = []
    for label_row in normalized.get("pairs", []):
        pair_id = _text(label_row.get("pair_id")).strip()
        pair = spec_by_pair_id.get(pair_id)
        if pair is None:
            raise ValueError(f"review pair is missing from spec: {pair_id}")
        left_id = _text(label_row.get("left", {}).get("id")).strip()
        right_id = _text(label_row.get("right", {}).get("id")).strip()
        if not _is_number(pair.get("voyage_cosine")):
            raise ValueError(f"pair voyage_cosine must be finite: {pair_id}")
        touch_archived = (
            left_id in archived_ids
            or right_id in archived_ids
            or left_id not in active_ids
            or right_id not in active_ids
        )
        label = _text(label_row.get("human_label")).strip() or None
        verified = label_row.get("human_verified") is True
        row = {
            "pair_id": pair_id,
            "left_id": left_id,
            "right_id": right_id,
            "label": label,
            "action": _text(label_row.get("action")).strip() or None,
            "verified": verified,
            "voyage_cosine": float(pair["voyage_cosine"]),
            "sampling_bucket": _text(pair.get("sampling_bucket")).strip() or None,
            "touch_archived": touch_archived,
            "calibration_scope": "calibration_only" if touch_archived else "active_pair",
        }
        rows.append(row)

    active_rows = [row for row in rows if not row["touch_archived"]]
    active_labeled_rows = [row for row in active_rows if row["label"] is not None]
    active_resolved_rows = [row for row in active_rows if row["verified"] and row["label"] not in {None, "unsure"}]
    active_visual_rows = [row for row in active_resolved_rows if row["label"] in VISUAL_POSITIVE_LABELS]
    active_negative_rows = [row for row in active_resolved_rows if row["label"] in NEGATIVE_LABELS]

    visual_cosines = [row["voyage_cosine"] for row in active_visual_rows]
    negative_cosines = [row["voyage_cosine"] for row in active_negative_rows]
    min_visual_positive = min(visual_cosines) if visual_cosines else None
    max_negative = max(negative_cosines) if negative_cosines else None
    observed_negative_only_below = _round_down_hundredth(min_visual_positive)
    observed_clean_positive_from = _strictly_above_hundredth(max_negative)

    insufficiency_reasons: list[str] = []
    if not active_visual_rows:
        insufficiency_reasons.append("no_active_visual_positive_pairs")
    if not active_negative_rows:
        insufficiency_reasons.append("no_active_negative_pairs")
    if (
        observed_negative_only_below is not None
        and observed_clean_positive_from is not None
        and observed_clean_positive_from <= observed_negative_only_below
    ):
        insufficiency_reasons.append("non_overlapping_or_reversed_candidate_bands")

    candidate_bands = []
    if not insufficiency_reasons and observed_clean_positive_from is not None:
        candidate_bands.append(
            {
                **_threshold_row(
                    "high_review_visual_family_candidate",
                    active_rows,
                    lower=observed_clean_positive_from,
                    upper=None,
                ),
                "note": "Observed clean-positive band in this sample only. Review first; no automatic group, delete, or approval.",
            }
        )
    if not insufficiency_reasons and observed_negative_only_below is not None and observed_clean_positive_from is not None:
        candidate_bands.append(
            {
                **_threshold_row(
                    "boundary_mixed_review_band",
                    active_rows,
                    lower=observed_negative_only_below,
                    upper=observed_clean_positive_from,
                ),
                "note": "Observed overlap band. Mixed positives and negatives; keep complete-link and no-chaining constraints.",
            }
        )
    if not insufficiency_reasons and observed_negative_only_below is not None:
        candidate_bands.append(
            {
                **_threshold_row(
                    "low_similarity_negative_skew_band",
                    active_rows,
                    lower=None,
                    upper=observed_negative_only_below,
                ),
                "note": "Observed negatives only in this sample below the band edge. Do not treat as a universal rejection rule.",
            }
        )

    threshold_points = sorted(
        {
            0.40,
            0.42,
            0.50,
            0.55,
            0.60,
            0.65,
            0.70,
            0.73,
            0.75,
            0.80,
            *(value for value in (observed_negative_only_below, observed_clean_positive_from) if value is not None),
        }
    )

    label_counts = Counter(row["label"] for row in active_rows if row["label"] is not None)
    bucket_counts: dict[str, dict[str, int]] = {}
    by_bucket: dict[str, Counter[str]] = {}
    for row in active_rows:
        bucket = row["sampling_bucket"] or "unknown"
        by_bucket.setdefault(bucket, Counter())
        by_bucket[bucket][row["label"] or "UNLABELED"] += 1
    for bucket, counter in sorted(by_bucket.items()):
        bucket_counts[bucket] = dict(counter)

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok" if not insufficiency_reasons else "insufficient",
        "run_id": _text(spec.get("run_id")).strip(),
        "comparison_dir": _text(spec.get("comparison_dir")).strip(),
        "review_spec_sha256": _text(spec.get("review_spec_sha256")).strip(),
        "vector_fingerprint": _text(spec.get("vector_fingerprint")).strip(),
        "provider": _text(spec.get("provider")).strip(),
        "model": _text(spec.get("model")).strip(),
        "dimensions": spec.get("dimensions"),
        "retention_basis": _text(source.get("retention_basis")).strip() or None,
        "sample_scope": {
            "review_spec_pairs": len(rows),
            "archived_touching_pairs_excluded_from_calibration": sum(1 for row in rows if row["touch_archived"]),
            "active_active_pairs_used_for_calibration": len(active_rows),
            "sample_bias_warning": (
                "Challenge-enriched sample only. Observed bands are for future review prioritization, not "
                "production accuracy, automatic grouping, or deletion approval."
            ),
        },
        "counts": {
            "active_visual_positive_pairs": len(active_visual_rows),
            "active_negative_pairs": len(active_negative_rows),
            "active_unlabeled_pairs": sum(1 for row in active_rows if row["label"] is None),
            "active_identical_pairs": sum(1 for row in active_visual_rows if row["label"] == "identical"),
            "active_near_duplicate_pairs": sum(1 for row in active_visual_rows if row["label"] == "near_duplicate"),
            "active_same_visual_family_pairs": sum(1 for row in active_visual_rows if row["label"] == "same_visual_family"),
            "active_same_theme_only_pairs": sum(1 for row in active_negative_rows if row["label"] == "same_theme_only"),
            "active_unrelated_pairs": sum(1 for row in active_negative_rows if row["label"] == "unrelated"),
            "active_labeled_pairs": len(active_labeled_rows),
            "active_resolved_pairs": len(active_resolved_rows),
            "calibration_only_pairs": sum(1 for row in rows if row["touch_archived"]),
        },
        "label_counts_active_only": dict(label_counts),
        "bucket_counts_active_only": bucket_counts,
        "observed_overlap": {
            "min_visual_positive_cosine": min_visual_positive,
            "max_negative_cosine": max_negative,
            "observed_negative_only_below": observed_negative_only_below,
            "observed_clean_positive_from": observed_clean_positive_from,
            "observed_overlap_band": {
                "lower_bound_inclusive": observed_negative_only_below,
                "upper_bound_exclusive": observed_clean_positive_from,
            }
            if observed_negative_only_below is not None and observed_clean_positive_from is not None
            else None,
        },
        "insufficiency_reasons": insufficiency_reasons,
        "candidate_bands": candidate_bands,
        "threshold_table": [_threshold_at_or_above(active_rows, threshold) for threshold in threshold_points],
        "constraints": {
            "archived_touching_pairs": "calibration_only_excluded_from_thresholds",
            "identical_requires_both_active_for_new_delete_plan": True,
            "automatic_grouping": False,
            "automatic_deletion": False,
            "complete_link_required_for_grouping": "recommendation_only",
            "single_link_chaining_allowed": False,
            "human_review_required": True,
        },
    }


__all__ = ["SCHEMA_VERSION", "VISUAL_POSITIVE_LABELS", "NEGATIVE_LABELS", "calibrate"]
