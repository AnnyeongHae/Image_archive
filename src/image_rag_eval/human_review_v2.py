from __future__ import annotations

import copy
import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .experiment import digest, json_bytes, now, read_json, run_path
from .human_review import (
    DEFAULT_COMPARISON_DIR,
    DIMENSION_OPTIONS,
    MIN_THRESHOLD_LABELS,
    PAIR_LABELS as V1_PAIR_LABELS,
    REVIEW_HTML_FILENAME,
    REVIEW_LABELS_SCHEMA_VERSION,
    REVIEW_SPEC_FILENAME,
    REVIEW_SPEC_SCHEMA_VERSION,
    REVIEW_SUMMARY_FILENAME,
    REVIEW_TEMPLATE_FILENAME,
    _write_path_if_missing_or_same,
    load_review_source,
    normalized_review_spec_for_drift,
    stored_review_spec_path,
    validate_stored_review_spec,
)
from .label_import import load_bound_review_spec as load_bound_review_spec_v1


REVIEW_V2_SPEC_FILENAME = "human-similarity-review-v2.spec.json"
REVIEW_V2_TEMPLATE_FILENAME = "human-similarity-review-v2.template.json"
REVIEW_V2_SUMMARY_FILENAME = "human-similarity-review-v2-summary.json"
REVIEW_V2_HTML_FILENAME = "human-similarity-review-v2.html"
REVIEW_V2_SPEC_SCHEMA_VERSION = "image-similarity-review-spec-2"
REVIEW_V2_LABELS_SCHEMA_VERSION = "image-similarity-review-labels-2"
REVIEW_V2_SUMMARY_SCHEMA_VERSION = "image-similarity-review-summary-2"
LOCAL_STORAGE_V1_PREFIX = "image-rag-human-review:"
LOCAL_STORAGE_V2_PREFIX = "image-rag-human-review-v2:"

PAIR_LABELS_V2 = {
    "identical": {
        "title": "동일",
        "short": "동일 — 중복 삭제",
        "description": "최종 시각 결과가 사실상 같음. 포맷/압축/해상도 차이만 있고 한쪽을 논리삭제 후보로 둘 수 있음.",
    },
    "near_duplicate": {
        "title": "거의 동일",
        "short": "거의 동일 — 그룹핑/둘 다 보존",
        "description": "차이는 작지만 의미가 있어 둘 다 보존할 가치가 있음.",
    },
    "same_visual_family": {
        "title": "시각 가족군",
        "short": "시각 가족군 — 그룹핑/둘 다 보존",
        "description": "주제·구도·스타일이 매우 비슷하지만 같은 결과라고 보긴 어려움.",
    },
    "same_theme_only": {
        "title": "같은 테마만",
        "short": "같은 테마만 — 분리 유지",
        "description": "큰 테마만 비슷하고 실제 결과는 분리해서 봐야 함.",
    },
    "unrelated": {
        "title": "무관",
        "short": "무관 — 분리 유지",
        "description": "같은 묶음으로 볼 이유가 거의 없음.",
    },
    "unsure": {
        "title": "보류",
        "short": "판단 보류",
        "description": "지금 판단하기 어렵고 추가 확인이 필요함.",
    },
}

ACTION_BY_LABEL = {
    "identical": "delete_duplicate",
    "near_duplicate": "group_only",
    "same_visual_family": "group_only",
    "same_theme_only": "keep_separate",
    "unrelated": "keep_separate",
    "unsure": "defer",
}

V1_TO_V2_LABEL_MAP = {
    "near_duplicate": "near_duplicate",
    "same_visual_family": "same_visual_family",
    "same_theme_only": "same_theme_only",
    "unrelated": "unrelated",
    "unsure": "unsure",
}


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _escape(value: Any) -> str:
    return html.escape(_text(value), quote=True)


def _bounded_text(value: str, byte_limit: int) -> str:
    return value.encode("utf-8")[:byte_limit].decode("utf-8", errors="ignore").strip()


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


def _normalized_dimensions(dimensions: Any) -> dict[str, str | None]:
    if not isinstance(dimensions, dict):
        raise ValueError("dimension review object missing")
    normalized: dict[str, str | None] = {}
    for name, options in DIMENSION_OPTIONS.items():
        value = dimensions.get(name)
        if value is not None and _text(value).strip() not in options:
            raise ValueError(f"invalid optional dimension label for {name}")
        normalized[name] = _text(value).strip() or None
    return normalized


def _review_action(label: str | None) -> str | None:
    if label is None:
        return None
    return ACTION_BY_LABEL[label]


def _side_state(retention: dict[str, Any], item_id: str) -> dict[str, Any]:
    archived_by_id = {
        _text(row.get("id")).strip(): row
        for row in retention.get("archived", [])
        if isinstance(row, dict) and _text(row.get("id")).strip()
    }
    priority = retention.get("priority_by_id", {}).get(item_id, {})
    archived_row = archived_by_id.get(item_id)
    if archived_row is not None:
        representative_id = _text(archived_row.get("representative_id")).strip() or item_id
        return {
            "state": "already_excluded",
            "display": "기존 중복 제외(새 삭제 아님)",
            "representative_id": representative_id,
            "rank_index": priority.get("rank_index"),
            "tier": priority.get("tier"),
            "label": priority.get("label"),
            "reason": priority.get("reason"),
            "selected_as_representative": False,
        }
    return {
        "state": "active",
        "display": "현재 대표/활성 후보",
        "representative_id": _text(priority.get("representative_id")).strip() or item_id,
        "rank_index": priority.get("rank_index"),
        "tier": priority.get("tier"),
        "label": priority.get("label"),
        "reason": priority.get("reason"),
        "selected_as_representative": True,
    }


def _retention_suggestion(retention: dict[str, Any], left_id: str, right_id: str) -> dict[str, Any]:
    left_state = _side_state(retention, left_id)
    right_state = _side_state(retention, right_id)

    if left_state["representative_id"] == right_id and left_state["state"] == "already_excluded":
        keep_id, delete_id = right_id, left_id
        basis = "retention.archived.direct_representative"
    elif right_state["representative_id"] == left_id and right_state["state"] == "already_excluded":
        keep_id, delete_id = left_id, right_id
        basis = "retention.archived.direct_representative"
    else:
        priority_by_id = retention.get("priority_by_id", {})

        def rank_key(item_id: str) -> tuple[int, str]:
            rank = priority_by_id.get(item_id, {}).get("rank_index")
            if not isinstance(rank, int) or rank <= 0:
                rank = 10**9
            return (rank, item_id)

        keep_id, delete_id = sorted((left_id, right_id), key=rank_key)
        basis = "retention.priority_by_id.rank_index_then_id"
    keep_state = _side_state(retention, keep_id)
    delete_state = _side_state(retention, delete_id)
    return {
        "keep_id": keep_id,
        "delete_id": delete_id,
        "basis": basis,
        "keep_rank_index": keep_state.get("rank_index"),
        "delete_rank_index": delete_state.get("rank_index"),
        "keep_state": keep_state["state"],
        "delete_state": delete_state["state"],
        "already_excluded": delete_state["state"] == "already_excluded",
        "keep_label": keep_state.get("label"),
        "delete_label": delete_state.get("label"),
    }


def _pair_with_v2_controls(pair: dict[str, Any], retention: dict[str, Any]) -> dict[str, Any]:
    row = copy.deepcopy(pair)
    left_id = _text(row["left"]["id"]).strip()
    right_id = _text(row["right"]["id"]).strip()
    row["left"]["retention_state"] = _side_state(retention, left_id)
    row["right"]["retention_state"] = _side_state(retention, right_id)
    row["retention_suggestion"] = _retention_suggestion(retention, left_id, right_id)
    return row


def _build_v2_spec_from_bound_v1(
    source_run_id: str,
    stored_v1_spec: dict[str, Any],
    source: dict[str, Any],
) -> dict[str, Any]:
    pairs = [_pair_with_v2_controls(pair, source["retention"]) for pair in stored_v1_spec["pairs"]]
    spec = {
        "schema_version": REVIEW_V2_SPEC_SCHEMA_VERSION,
        "created_at": now(),
        "run_id": source_run_id,
        "comparison_dir": stored_v1_spec["comparison_dir"],
        "provider": stored_v1_spec["provider"],
        "model": stored_v1_spec["model"],
        "arm": stored_v1_spec["arm"],
        "dimensions": stored_v1_spec["dimensions"],
        "source_manifest_sha256": stored_v1_spec["source_manifest_sha256"],
        "vector_fingerprint": stored_v1_spec["vector_fingerprint"],
        "source_review_spec_sha256": stored_v1_spec["review_spec_sha256"],
        "source_review_spec_schema_version": stored_v1_spec["schema_version"],
        "source_review_spec_filename": REVIEW_SPEC_FILENAME,
        "source_retention_sha256": digest(json_bytes(source["retention"])),
        "retention_basis": source["retention_basis"],
        "sampling_seed": stored_v1_spec["sampling_seed"],
        "sampling_strategy": copy.deepcopy(stored_v1_spec["sampling_strategy"]),
        "counts": copy.deepcopy(stored_v1_spec["counts"]),
        "migration": {
            "source_labels_schema_version": REVIEW_LABELS_SCHEMA_VERSION,
            "old_local_storage_key": f"{LOCAL_STORAGE_V1_PREFIX}{stored_v1_spec['review_spec_sha256']}",
            "new_local_storage_key": f"{LOCAL_STORAGE_V2_PREFIX}{source_run_id}:{stored_v1_spec['review_spec_sha256']}",
            "note": "v1 near_duplicate는 v2에서도 항상 그룹 보존으로만 이관되며 identical로 승격되지 않습니다.",
        },
        "human_label_definitions": copy.deepcopy(PAIR_LABELS_V2),
        "dimension_definitions": copy.deepcopy(DIMENSION_OPTIONS),
        "action_definitions": {
            "delete_duplicate": "JSON 전달/검증 후 대표 유지 + 목록·검색 논리삭제 예정. 원본 파일 삭제는 아님.",
            "group_only": "둘 다 보존하고 그룹만 형성.",
            "keep_separate": "별개 항목으로 유지.",
            "defer": "판단 보류.",
        },
        "pairs": pairs,
    }
    spec["review_spec_sha256"] = digest(json_bytes(spec))
    return spec


def load_bound_review_spec_v2(
    root: Path,
    source_run_id: str,
    *,
    comparison_dir: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    root = Path(root).resolve()
    spec_path = stored_review_spec_path(root, source_run_id, filename=REVIEW_V2_SPEC_FILENAME)
    if not spec_path.is_file():
        raise ValueError("stored v2 review spec is required")
    stored_v2_spec = read_json(spec_path)
    validate_stored_review_spec(stored_v2_spec, source_run_id=source_run_id)
    stored_comparison_dir = _text(stored_v2_spec.get("comparison_dir")).strip() or DEFAULT_COMPARISON_DIR
    if comparison_dir is not None and comparison_dir != stored_comparison_dir:
        raise ValueError("requested comparison dir does not match stored v2 review spec")
    stored_v1_spec, source = load_bound_review_spec_v1(root, source_run_id, comparison_dir=stored_comparison_dir)
    current_v2_spec = _build_v2_spec_from_bound_v1(source_run_id, stored_v1_spec, source)
    if normalized_review_spec_for_drift(stored_v2_spec) != normalized_review_spec_for_drift(current_v2_spec):
        raise ValueError("stored v2 review spec no longer matches the current bound v1 spec/source state")
    return stored_v2_spec, stored_v1_spec, source


def blank_review_labels_v2(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": REVIEW_V2_LABELS_SCHEMA_VERSION,
        "review_spec_sha256": _text(spec.get("review_spec_sha256")).strip(),
        "source_review_spec_sha256": _text(spec.get("source_review_spec_sha256")).strip(),
        "run_id": _text(spec.get("run_id")).strip(),
        "provider": _text(spec.get("provider")).strip(),
        "model": _text(spec.get("model")).strip(),
        "dimensions": spec.get("dimensions"),
        "source_manifest_sha256": _text(spec.get("source_manifest_sha256")).strip(),
        "vector_fingerprint": _text(spec.get("vector_fingerprint")).strip(),
        "reviewer": "",
        "reviewed_at": "",
        "pairs": [
            {
                "pair_id": pair["pair_id"],
                "left": {
                    "id": pair["left"]["id"],
                    "source_sha256": pair["left"]["source_sha256"],
                    "prepared_sha256": pair["left"]["prepared_sha256"],
                },
                "right": {
                    "id": pair["right"]["id"],
                    "source_sha256": pair["right"]["source_sha256"],
                    "prepared_sha256": pair["right"]["prepared_sha256"],
                },
                "retention_suggestion": copy.deepcopy(pair["retention_suggestion"]),
                "human_label": None,
                "human_verified": False,
                "action": None,
                "dimensions": {name: None for name in DIMENSION_OPTIONS},
                "reason": "",
            }
            for pair in spec["pairs"]
        ],
        "notes": "사람 검토 결과만 기록합니다. identical은 삭제 파일 작업이 아니라 대표 유지 + 중복 논리삭제 의도 승인입니다.",
    }


def _normalize_v1_labels_for_migration(spec_v2: dict[str, Any], labels_v1: dict[str, Any], *, require_complete: bool) -> dict[str, Any]:
    if _text(labels_v1.get("schema_version")).strip() != REVIEW_LABELS_SCHEMA_VERSION:
        raise ValueError("unsupported source labels schema for migration")
    if _text(labels_v1.get("review_spec_sha256")).strip() != _text(spec_v2.get("source_review_spec_sha256")).strip():
        raise ValueError("v1 labels are not bound to this v2 review")
    if _text(labels_v1.get("run_id")).strip() != _text(spec_v2.get("run_id")).strip():
        raise ValueError("v1 labels run_id mismatch")
    submitted_pairs = labels_v1.get("pairs")
    if not isinstance(submitted_pairs, list) or len(submitted_pairs) != len(spec_v2.get("pairs", [])):
        raise ValueError("v1 labels pair count mismatch")
    reviewer = _text(labels_v1.get("reviewer")).strip()
    reviewed_at_raw = _text(labels_v1.get("reviewed_at")).strip()
    if require_complete:
        if not reviewer:
            raise ValueError("reviewer identity and reviewed_at are required")
        reviewed_at = _validated_reviewed_at(reviewed_at_raw)
    else:
        reviewed_at = reviewed_at_raw
    expected_bindings = {pair["pair_id"]: pair for pair in spec_v2["pairs"]}
    normalized_pairs = []
    seen: set[str] = set()
    for row in submitted_pairs:
        if not isinstance(row, dict):
            raise ValueError("v1 pair rows must be objects")
        pair_id = _text(row.get("pair_id")).strip()
        if not pair_id or pair_id in seen or pair_id not in expected_bindings:
            raise ValueError("unknown or duplicate pair_id in v1 labels")
        seen.add(pair_id)
        expected = expected_bindings[pair_id]
        for side in ("left", "right"):
            actual_side = row.get(side)
            expected_side = expected.get(side)
            if not isinstance(actual_side, dict) or not isinstance(expected_side, dict):
                raise ValueError("v1 pair side binding missing")
            for key in ("id", "source_sha256", "prepared_sha256"):
                if _text(actual_side.get(key)).strip() != _text(expected_side.get(key)).strip():
                    raise ValueError("v1 pair binding mismatch")
        label = _text(row.get("human_label")).strip() or None
        if label is not None and label not in V1_PAIR_LABELS:
            raise ValueError("unknown v1 human similarity label")
        verified = row.get("human_verified")
        if not isinstance(verified, bool):
            raise ValueError("v1 human_verified must be boolean")
        if verified and label in {None, "unsure"}:
            raise ValueError("invalid resolved v1 label state")
        dimensions = row.get("dimensions")
        normalized_pairs.append(
            {
                "pair_id": pair_id,
                "human_label": label,
                "human_verified": verified,
                "dimensions": _normalized_dimensions(dimensions),
                "reason": _bounded_text(_text(row.get("reason")).strip(), 1000),
            }
        )
    return {
        "schema_version": REVIEW_LABELS_SCHEMA_VERSION,
        "review_spec_sha256": _text(labels_v1.get("review_spec_sha256")).strip(),
        "run_id": _text(labels_v1.get("run_id")).strip(),
        "reviewer": reviewer,
        "reviewed_at": reviewed_at,
        "pairs": normalized_pairs,
    }


def migrate_v1_labels_to_v2(spec_v2: dict[str, Any], labels_v1: dict[str, Any], *, require_complete: bool = True) -> dict[str, Any]:
    normalized_v1 = _normalize_v1_labels_for_migration(spec_v2, labels_v1, require_complete=require_complete)
    output = blank_review_labels_v2(spec_v2)
    output["reviewer"] = normalized_v1["reviewer"]
    output["reviewed_at"] = normalized_v1["reviewed_at"]
    output["migration_note"] = "v1 near_duplicate는 v2 identical로 승격되지 않고 group_only로만 이관했습니다."
    by_pair = {row["pair_id"]: row for row in normalized_v1["pairs"]}
    for row in output["pairs"]:
        previous = by_pair[row["pair_id"]]
        label = previous["human_label"]
        mapped = V1_TO_V2_LABEL_MAP.get(label) if label is not None else None
        row["human_label"] = mapped
        row["human_verified"] = bool(previous["human_verified"]) if mapped not in {None, "unsure"} else False
        row["action"] = _review_action(mapped)
        row["dimensions"] = previous["dimensions"]
        row["reason"] = previous["reason"]
    return output


def validate_review_labels_v2(spec: dict[str, Any], labels: dict[str, Any]) -> dict[str, Any]:
    if _text(labels.get("schema_version")).strip() != REVIEW_V2_LABELS_SCHEMA_VERSION:
        raise ValueError("unknown v2 human review labels schema")
    reviewer = _text(labels.get("reviewer")).strip()
    reviewed_at = _validated_reviewed_at(labels.get("reviewed_at"))
    if not reviewer:
        raise ValueError("reviewer identity and reviewed_at are required")
    for field, expected in (
        ("review_spec_sha256", spec.get("review_spec_sha256")),
        ("source_review_spec_sha256", spec.get("source_review_spec_sha256")),
        ("run_id", spec.get("run_id")),
        ("provider", spec.get("provider")),
        ("model", spec.get("model")),
        ("dimensions", spec.get("dimensions")),
        ("source_manifest_sha256", spec.get("source_manifest_sha256")),
        ("vector_fingerprint", spec.get("vector_fingerprint")),
    ):
        if labels.get(field) != expected:
            raise ValueError(f"labels {field} mismatch")
    submitted_pairs = labels.get("pairs")
    expected_bindings = {pair["pair_id"]: pair for pair in spec.get("pairs", [])}
    if not isinstance(submitted_pairs, list) or len(submitted_pairs) != len(expected_bindings):
        raise ValueError("submitted pairs must exactly match the sampled review pairs")
    seen: set[str] = set()
    normalized_pairs = []
    for row in submitted_pairs:
        if not isinstance(row, dict):
            raise ValueError("pair label rows must be objects")
        pair_id = _text(row.get("pair_id")).strip()
        if pair_id in seen or pair_id not in expected_bindings:
            raise ValueError("unknown or duplicate pair_id in labels")
        seen.add(pair_id)
        expected = expected_bindings[pair_id]
        for side in ("left", "right"):
            actual_side = row.get(side)
            expected_side = expected.get(side)
            if not isinstance(actual_side, dict) or not isinstance(expected_side, dict):
                raise ValueError("pair side binding missing")
            for key in ("id", "source_sha256", "prepared_sha256"):
                if _text(actual_side.get(key)).strip() != _text(expected_side.get(key)).strip():
                    raise ValueError("pair binding mismatch")
        actual_suggestion = row.get("retention_suggestion")
        expected_suggestion = expected.get("retention_suggestion")
        if actual_suggestion != expected_suggestion:
            raise ValueError("retention suggestion mismatch")
        label = _text(row.get("human_label")).strip() or None
        if label is not None and label not in PAIR_LABELS_V2:
            raise ValueError("unknown v2 human similarity label")
        verified = row.get("human_verified")
        if not isinstance(verified, bool):
            raise ValueError("human_verified must be boolean")
        action = row.get("action")
        expected_action = _review_action(label)
        if action != expected_action:
            raise ValueError("action does not match the selected v2 label")
        if label is None:
            if verified:
                raise ValueError("unlabeled pairs cannot be human_verified")
        elif label == "unsure":
            if verified:
                raise ValueError("unsure pairs cannot be marked human_verified")
        else:
            if verified is not True:
                raise ValueError("resolved v2 labels must be human_verified")
        suggestion = copy.deepcopy(expected_suggestion)
        if label == "identical":
            keep_id = _text(suggestion.get("keep_id")).strip()
            delete_id = _text(suggestion.get("delete_id")).strip()
            if not keep_id or not delete_id or keep_id == delete_id:
                raise ValueError("identical rows require a deterministic keep/delete suggestion")
        normalized_pairs.append(
            {
                "pair_id": pair_id,
                "left": row["left"],
                "right": row["right"],
                "retention_suggestion": suggestion,
                "human_label": label,
                "human_verified": verified,
                "action": expected_action,
                "dimensions": _normalized_dimensions(row.get("dimensions")),
                "reason": _bounded_text(_text(row.get("reason")).strip(), 1000),
            }
        )
    return {
        **labels,
        "reviewer": reviewer,
        "reviewed_at": reviewed_at,
        "pairs": normalized_pairs,
    }


def summarize_thresholds_v2(
    spec: dict[str, Any],
    labels: dict[str, Any],
    *,
    minimum_verified_pairs: int = MIN_THRESHOLD_LABELS,
) -> dict[str, Any]:
    if minimum_verified_pairs < 1:
        raise ValueError("minimum_verified_pairs must be >= 1")
    normalized = validate_review_labels_v2(spec, labels)
    pair_lookup = {pair["pair_id"]: pair for pair in spec["pairs"]}
    labeled = [row for row in normalized["pairs"] if row["human_label"] is not None]
    unresolved = [row for row in labeled if row["human_label"] == "unsure"]
    verified = [row for row in normalized["pairs"] if row["human_verified"] is True]
    if len(verified) < minimum_verified_pairs:
        return {
            "status": "insufficient_human_labels",
            "labeled_pairs": len(labeled),
            "unresolved_pairs": len(unresolved),
            "verified_pairs": len(verified),
            "minimum_required": minimum_verified_pairs,
            "bias_warning": spec["sampling_strategy"]["challenge_sample_bias_warning"],
            "threshold_summary": None,
        }
    strict_positive = {"identical"}
    broad_positive = {"identical", "near_duplicate", "same_visual_family"}
    rows = []
    for threshold in [0.75, 0.80, 0.85, 0.90, 0.95]:
        matched = [row for row in verified if pair_lookup[row["pair_id"]]["voyage_cosine"] >= threshold]
        strict_hits = sum(1 for row in matched if row["human_label"] in strict_positive)
        broad_hits = sum(1 for row in matched if row["human_label"] in broad_positive)
        rows.append(
            {
                "threshold": threshold,
                "reviewed_pairs_at_or_above_threshold": len(matched),
                "sampled_identical_rate": round(strict_hits / len(matched), 6) if matched else None,
                "sampled_groupable_rate": round(broad_hits / len(matched), 6) if matched else None,
            }
        )
    label_counts: dict[str, int] = {}
    action_counts: dict[str, int] = {}
    for row in verified:
        label_counts[row["human_label"]] = label_counts.get(row["human_label"], 0) + 1
        action_counts[row["action"]] = action_counts.get(row["action"], 0) + 1
    return {
        "status": "ok",
        "labeled_pairs": len(labeled),
        "unresolved_pairs": len(unresolved),
        "verified_pairs": len(verified),
        "label_counts": label_counts,
        "action_counts": action_counts,
        "threshold_summary": rows,
        "bias_warning": "이 요약은 검토된 challenge sample에만 해당합니다. 전체 정확도, 재현율, 임계값 보정 성능을 주장할 수 없습니다.",
    }


def _summary_payload_v2(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": REVIEW_V2_SUMMARY_SCHEMA_VERSION,
        "created_at": spec["created_at"],
        "run_id": spec["run_id"],
        "provider": spec["provider"],
        "model": spec["model"],
        "dimensions": spec["dimensions"],
        "comparison_dir": spec["comparison_dir"],
        "review_spec_sha256": spec["review_spec_sha256"],
        "source_review_spec_sha256": spec["source_review_spec_sha256"],
        "source_manifest_sha256": spec["source_manifest_sha256"],
        "source_retention_sha256": spec["source_retention_sha256"],
        "vector_fingerprint": spec["vector_fingerprint"],
        "sampled_pairs": spec["counts"]["sampled_pairs"],
        "total_pairs": spec["counts"]["total_pairs"],
        "migration": copy.deepcopy(spec["migration"]),
        "network_calls": 0,
        "writes": 4,
    }


def _label_buttons(pair_id: str) -> str:
    buttons = []
    for value, meta in PAIR_LABELS_V2.items():
        buttons.append(
            '<label class="radio">'
            f'<input type="radio" name="label-{_escape(pair_id)}" value="{_escape(value)}" data-pair-id="{_escape(pair_id)}">'
            f'<span>{_escape(meta["short"])}</span><small>{_escape(meta["description"])}</small>'
            "</label>"
        )
    return f'<fieldset><legend>사람 판단</legend><div class="label-grid">{"".join(buttons)}</div></fieldset>'


def _dimension_select(name: str, pair_id: str) -> str:
    options = ['<option value="">선택 안 함</option>']
    for value, description in DIMENSION_OPTIONS[name].items():
        options.append(f'<option value="{_escape(value)}">{_escape(description)}</option>')
    return (
        '<label class="dimension">'
        f'{_escape(name)}'
        f'<select data-dimension="{_escape(name)}" data-pair-id="{_escape(pair_id)}">{"".join(options)}</select>'
        "</label>"
    )


def _side_display_label(side: dict[str, Any]) -> str:
    style_id = _text(side.get("style_id")).strip()
    item_id = _text(side.get("id")).strip()
    if style_id and item_id:
        return f"{style_id} ({item_id})"
    return style_id or item_id


def review_html_v2(spec: dict[str, Any]) -> str:
    template = json.dumps(blank_review_labels_v2(spec), ensure_ascii=False).replace("<", "\\u003c")
    spec_json = json.dumps(
        {
            "review_spec_sha256": spec["review_spec_sha256"],
            "source_review_spec_sha256": spec["source_review_spec_sha256"],
            "run_id": spec["run_id"],
            "provider": spec["provider"],
            "model": spec["model"],
            "dimensions": spec["dimensions"],
            "source_manifest_sha256": spec["source_manifest_sha256"],
            "vector_fingerprint": spec["vector_fingerprint"],
            "pairs": [
                {
                    "pair_id": pair["pair_id"],
                    "left": {
                        "id": pair["left"]["id"],
                        "source_sha256": pair["left"]["source_sha256"],
                        "prepared_sha256": pair["left"]["prepared_sha256"],
                    },
                    "right": {
                        "id": pair["right"]["id"],
                        "source_sha256": pair["right"]["source_sha256"],
                        "prepared_sha256": pair["right"]["prepared_sha256"],
                    },
                    "retention_suggestion": pair["retention_suggestion"],
                }
                for pair in spec["pairs"]
            ],
            "migration": spec["migration"],
        },
        ensure_ascii=False,
    ).replace("<", "\\u003c")
    cards = []
    for pair in spec["pairs"]:
        machine = {
            "voyage_cosine": pair["voyage_cosine"],
            "local_relation": pair["machine_candidate"]["local_relation"],
            "prompt_exact": pair["machine_candidate"]["prompt_exact"],
            "prompt_normalized_match": pair["machine_candidate"]["prompt_normalized_match"],
            "threshold_hypothesis": "0.85/0.90_candidate_only_not_calibrated",
        }
        suggestion = pair["retention_suggestion"]
        left_label = _side_display_label(pair["left"])
        right_label = _side_display_label(pair["right"])
        keep_label = left_label if suggestion["keep_id"] == pair["left"]["id"] else right_label
        delete_label = left_label if suggestion["delete_id"] == pair["left"]["id"] else right_label
        delete_display = (
            pair["left"]["retention_state"]["display"]
            if suggestion["delete_id"] == pair["left"]["id"]
            else pair["right"]["retention_state"]["display"]
        )
        existing_exclusion = (
            pair["left"]["retention_state"]["state"] == "already_excluded"
            or pair["right"]["retention_state"]["state"] == "already_excluded"
        )
        if existing_exclusion:
            retention_plan_html = (
                '<p><strong>기존 제외 대조군</strong> — 새 삭제 없음. 판정은 검증용이며 기존 제외 상태를 유지합니다.</p>'
                '<p class="status">한쪽 이상이 이미 기존 중복 제외 상태입니다. export JSON의 retention_suggestion은 무결성 검증용으로만 유지됩니다.</p>'
            )
        else:
            retention_plan_html = (
                f'<p><strong>identical 선택 시 제안:</strong> 대표 유지 <code>{_escape(keep_label)}</code> / 중복 논리삭제 후보 <code>{_escape(delete_label)}</code></p>'
                f'<p class="status">기준: {_escape(suggestion["basis"])} · 대상 상태: {_escape(delete_display)}</p>'
            )
        cards.append(
            '<article class="pair-card" '
            f'data-pair-id="{_escape(pair["pair_id"])}" '
            f'data-retention="{_escape(json.dumps(suggestion, ensure_ascii=False))}" '
            f'data-keep-label="{_escape(keep_label)}" '
            f'data-delete-label="{_escape(delete_label)}" '
            f'data-delete-display="{_escape(delete_display)}" '
            f'data-existing-exclusion="{str(existing_exclusion).lower()}" '
            f"data-machine='{_escape(json.dumps(machine, ensure_ascii=False))}'>"
            f'<h2>{_escape(pair["pair_id"])} <span class="bucket">{_escape(pair["sampling_bucket"])}</span></h2>'
            '<div class="retention-plan">'
            f"{retention_plan_html}"
            '</div>'
            '<div class="pair-grid">'
            f'<section><img loading="lazy" src="{_escape(pair["left"]["prepared_path"])}" alt="{_escape(pair["left"]["style_id"])} prepared preview"><h3>{_escape(pair["left"]["style_id"])}</h3><p class="mono">{_escape(pair["left"]["id"])}</p><p>{_escape(pair["left"]["retention_state"]["display"])}</p><p><a href="{_escape(pair["left"]["prepared_path"])}" target="_blank" rel="noopener">크게 보기</a></p><div class="prompt-gate">시각 판단 선택 후 프롬프트/점수 공개</div><details class="prompt-panel" id="prompt-left-{_escape(pair["pair_id"])}" hidden><summary>프롬프트 의도</summary><p>{_escape(pair["left"]["prompt_intent"])}</p></details></section>'
            f'<section><img loading="lazy" src="{_escape(pair["right"]["prepared_path"])}" alt="{_escape(pair["right"]["style_id"])} prepared preview"><h3>{_escape(pair["right"]["style_id"])}</h3><p class="mono">{_escape(pair["right"]["id"])}</p><p>{_escape(pair["right"]["retention_state"]["display"])}</p><p><a href="{_escape(pair["right"]["prepared_path"])}" target="_blank" rel="noopener">크게 보기</a></p><div class="prompt-gate">시각 판단 선택 후 프롬프트/점수 공개</div><details class="prompt-panel" id="prompt-right-{_escape(pair["pair_id"])}" hidden><summary>프롬프트 의도</summary><p>{_escape(pair["right"]["prompt_intent"])}</p></details></section>'
            '</div>'
            f'{_label_buttons(pair["pair_id"])}'
            '<div class="dimension-row">'
            f'{_dimension_select("composition", pair["pair_id"])}'
            f'{_dimension_select("style", pair["pair_id"])}'
            f'{_dimension_select("subject", pair["pair_id"])}'
            '</div>'
            f'<label class="reason">판단 근거(선택) <textarea rows="3" maxlength="1000" data-reason-for="{_escape(pair["pair_id"])}"></textarea></label>'
            f'<div class="action-panel" id="action-{_escape(pair["pair_id"])}">현재 액션: 미선택</div>'
            f'<div class="machine-panel" id="machine-{_escape(pair["pair_id"])}" hidden><strong>기계 후보 정보</strong><pre>{_escape(json.dumps(machine, ensure_ascii=False, indent=2))}</pre></div>'
            '</article>'
        )
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <title>이미지 유사도 사람 검토 v2</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0f1320;
      --panel: #182033;
      --line: #33405c;
      --text: #e8eef9;
      --muted: #9fb0c9;
      --accent: #d0a64d;
      --danger: #f59e0b;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--text); font: 15px/1.5 system-ui, sans-serif; }}
    main {{ max-width: 1520px; margin: 0 auto; padding: 20px; }}
    h1, h2, h3, p {{ margin: 0 0 10px; }}
    code, .mono {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }}
    .notice {{ background: var(--panel); border-left: 4px solid var(--accent); padding: 12px 14px; border-radius: 10px; margin-bottom: 18px; }}
    .toolbar {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin: 14px 0 18px; }}
    input, button, select, textarea {{ font: inherit; }}
    input[type=text], select, textarea {{ width: 100%; background: #0d1220; color: var(--text); border: 1px solid var(--line); border-radius: 8px; padding: 8px; }}
    button {{ background: #24324f; color: var(--text); border: 1px solid var(--line); border-radius: 8px; padding: 8px 12px; cursor: pointer; }}
    a {{ color: #b7d4ff; }}
    .pair-card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 14px; margin-bottom: 18px; overflow-wrap: anywhere; }}
    .pair-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
    .pair-grid section, .retention-plan {{ background: #111827; border: 1px solid var(--line); border-radius: 10px; padding: 12px; }}
    img {{ width: 100%; height: 280px; object-fit: contain; background: #0b1020; border-radius: 8px; }}
    fieldset {{ border: 1px solid var(--line); border-radius: 10px; margin: 12px 0; }}
    legend {{ padding: 0 6px; }}
    .label-grid {{ display: grid; gap: 8px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }}
    .radio {{ display: block; background: #111827; border-radius: 8px; padding: 8px; border: 1px solid var(--line); }}
    .radio span {{ display: block; font-weight: 600; margin-bottom: 4px; }}
    .radio small {{ color: var(--muted); display: block; }}
    .dimension-row {{ display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); margin-bottom: 12px; }}
    .reason {{ display: block; }}
    .machine-panel, .action-panel {{ background: #101826; border: 1px dashed var(--line); border-radius: 10px; padding: 10px; margin-top: 10px; }}
    .prompt-gate, .status {{ color: var(--muted); font-size: 13px; overflow-wrap: anywhere; word-break: break-word; }}
    .bucket {{ font-size: 12px; color: var(--muted); }}
    pre {{ margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }}
    code, .mono {{ overflow-wrap: anywhere; word-break: break-word; }}
    @media (max-width: 900px) {{
      .pair-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <h1>이미지 유사도 사람 검토 v2</h1>
    <p class="status">run_id: {_escape(spec["run_id"])} · source_v1_spec: {_escape(spec["source_review_spec_sha256"])} · {spec["counts"]["sampled_pairs"]}/{spec["counts"]["total_pairs"]} frozen pairs</p>
    <div class="notice">
      <p>이 화면은 v1의 frozen 80쌍을 그대로 가져온 v2 검토 화면입니다. 기존 v1 artifact와 labels는 바뀌지 않습니다.</p>
      <p><strong>동일</strong>은 파일 삭제가 아니라 사람의 삭제 의도 승인입니다. JSON 전달/검증 후 대표 유지 + 목록·검색 논리삭제가 적용 예정이며, 원본은 복구 가능 보관을 전제로 합니다.</p>
      <p><strong>거의 동일</strong>은 그룹핑만 하고 둘 다 보존합니다. 같은 프롬프트만으로 identical을 선택하면 안 됩니다.</p>
      <p>기계 점수와 프롬프트는 앵커링을 줄이기 위해 시각 판단 전까지 숨깁니다.</p>
      <p>{_escape(spec["sampling_strategy"]["challenge_sample_bias_warning"])}</p>
    </div>
    <label>검토자 이름 <input id="reviewer" type="text" autocomplete="off" placeholder="실제 검토자 이름 또는 식별자"></label>
    <div class="toolbar">
      <button id="download">검토 JSON 내려받기</button>
      <button id="clear-draft" type="button">로컬 초안 지우기</button>
      <label>기존 검토 JSON 불러오기 <input id="import-file" type="file" accept="application/json"></label>
      <span id="message" class="status" role="status" aria-live="polite"></span>
    </div>
    {"".join(cards)}
  </main>
  <script>
    const template = {template};
    const spec = {spec_json};
    const draftKey = `{LOCAL_STORAGE_V2_PREFIX}${{spec.run_id}}:${{spec.review_spec_sha256}}`;
    const oldDraftKey = spec.migration.old_local_storage_key;
    const message = document.getElementById("message");

    function actionForLabel(label) {{
      if (!label) return null;
      const map = {{
        identical: "delete_duplicate",
        near_duplicate: "group_only",
        same_visual_family: "group_only",
        same_theme_only: "keep_separate",
        unrelated: "keep_separate",
        unsure: "defer",
      }};
      return map[label] || null;
    }}

    function actionTextForLabel(label, card) {{
      if (!label) return "현재 액션: 미선택";
      const keepLabel = card?.dataset.keepLabel || "대표";
      const deleteLabel = card?.dataset.deleteLabel || "중복 후보";
      const deleteDisplay = card?.dataset.deleteDisplay || "";
      const existingExclusion = card?.dataset.existingExclusion === "true";
      if (label === "identical") {{
        if (existingExclusion) {{
          return "현재 액션: 기존 제외 대조군 — 새 삭제 없음, 기존 제외 상태 유지";
        }}
        return `현재 액션: 동일 판단 — 대표 유지 ${{keepLabel}}, 중복 논리삭제 계획 ${{deleteLabel}} (${{deleteDisplay}})`;
      }}
      if (label === "near_duplicate") return "현재 액션: 거의 동일 — 그룹핑만 하고 둘 다 보존";
      if (label === "same_visual_family") return "현재 액션: 시각 가족군 — 그룹핑만 하고 둘 다 보존";
      if (label === "same_theme_only") return "현재 액션: 같은 테마만 — 별개 항목으로 유지";
      if (label === "unrelated") return "현재 액션: 무관 — 별개 항목으로 유지";
      if (label === "unsure") return "현재 액션: 판단 보류";
      return "현재 액션: 미선택";
    }}

    function validateV2Bindings(payload) {{
      if (!payload || payload.review_spec_sha256 !== spec.review_spec_sha256 || payload.run_id !== spec.run_id) {{
        throw new Error("현재 v2 review spec과 맞지 않는 파일입니다.");
      }}
      if (!payload.reviewed_at || !/^\\d{{4}}-\\d{{2}}-\\d{{2}}T\\d{{2}}:\\d{{2}}:\\d{{2}}(?:\\.\\d+)?Z$/.test(payload.reviewed_at)) {{
        throw new Error("reviewed_at UTC 시각 형식이 올바르지 않습니다.");
      }}
      if (!Array.isArray(payload.pairs) || payload.pairs.length !== spec.pairs.length) {{
        throw new Error("pair 개수가 현재 표본과 다릅니다.");
      }}
      const expected = new Map(spec.pairs.map((pair) => [pair.pair_id, pair]));
      for (const row of payload.pairs) {{
        const pair = expected.get(row.pair_id);
        if (!pair) throw new Error("알 수 없는 pair_id가 있습니다.");
        if (JSON.stringify(row.retention_suggestion) !== JSON.stringify(pair.retention_suggestion)) {{
          throw new Error("retention suggestion이 현재 샘플과 다릅니다.");
        }}
        const expectedAction = actionForLabel(row.human_label || null);
        if ((row.action ?? null) !== expectedAction) {{
          throw new Error("action이 선택 라벨과 맞지 않습니다.");
        }}
        if (row.human_label === "unsure" && row.human_verified) {{
          throw new Error("unsure pair는 human_verified=true일 수 없습니다.");
        }}
        if (row.human_label && row.human_label !== "unsure" && row.human_verified !== true) {{
          throw new Error("resolved pair는 human_verified=true여야 합니다.");
        }}
        for (const side of ["left", "right"]) {{
          if (!row[side] || row[side].id !== pair[side].id || row[side].prepared_sha256 !== pair[side].prepared_sha256 || row[side].source_sha256 !== pair[side].source_sha256) {{
            throw new Error("pair binding이 현재 샘플과 다릅니다.");
          }}
        }}
      }}
      return payload;
    }}

    function migrateV1Payload(payload) {{
      if (!payload || payload.schema_version !== "{REVIEW_LABELS_SCHEMA_VERSION}" || payload.review_spec_sha256 !== spec.source_review_spec_sha256 || payload.run_id !== spec.run_id) {{
        throw new Error("현재 v2에 연결된 v1 초안이 아닙니다.");
      }}
      if (!Array.isArray(payload.pairs) || payload.pairs.length !== spec.pairs.length) {{
        throw new Error("v1 pair 개수가 현재 frozen pair와 다릅니다.");
      }}
      const migrated = structuredClone(template);
      migrated.reviewer = payload.reviewer || "";
      migrated.reviewed_at = payload.reviewed_at || "";
      const expected = new Map(spec.pairs.map((pair) => [pair.pair_id, pair]));
      for (const row of payload.pairs) {{
        const pair = expected.get(row.pair_id);
        if (!pair) throw new Error("알 수 없는 pair_id가 있습니다.");
        const target = migrated.pairs.find((item) => item.pair_id === row.pair_id);
        const labelMap = {{
          near_duplicate: "near_duplicate",
          same_visual_family: "same_visual_family",
          same_theme_only: "same_theme_only",
          unrelated: "unrelated",
          unsure: "unsure",
        }};
        const mapped = row.human_label ? (labelMap[row.human_label] || null) : null;
        target.human_label = mapped;
        target.human_verified = Boolean(mapped && mapped !== "unsure" && row.human_verified === true);
        target.action = actionForLabel(mapped);
        target.dimensions = row.dimensions || target.dimensions;
        target.reason = row.reason || "";
      }}
      migrated.migration_note = "v1 초안을 v2로 복사했습니다. near_duplicate는 identical로 바뀌지 않고 그룹 보존으로만 이관됩니다.";
      return migrated;
    }}

    function collectState() {{
      const output = structuredClone(template);
      output.reviewer = document.getElementById("reviewer").value.trim();
      output.reviewed_at = new Date().toISOString();
      output.pairs = output.pairs.map((pair) => {{
        const pairId = pair.pair_id;
        const selected = document.querySelector(`input[data-pair-id="${{pairId}}"]:checked`);
        const humanLabel = selected ? selected.value : null;
        pair.human_label = humanLabel;
        pair.human_verified = Boolean(humanLabel && humanLabel !== "unsure");
        pair.action = actionForLabel(humanLabel);
        pair.reason = (document.querySelector(`[data-reason-for="${{pairId}}"]`)?.value || "").trim();
        pair.dimensions = {{
          composition: document.querySelector(`select[data-dimension="composition"][data-pair-id="${{pairId}}"]`)?.value || null,
          style: document.querySelector(`select[data-dimension="style"][data-pair-id="${{pairId}}"]`)?.value || null,
          subject: document.querySelector(`select[data-dimension="subject"][data-pair-id="${{pairId}}"]`)?.value || null,
        }};
        return pair;
      }});
      return output;
    }}

    function applyState(payload) {{
      document.getElementById("reviewer").value = payload.reviewer || "";
      for (const row of payload.pairs || []) {{
        if (row.human_label) {{
          const radio = document.querySelector(`input[data-pair-id="${{row.pair_id}}"][value="${{row.human_label}}"]`);
          if (radio) radio.checked = true;
        }}
        for (const [name, value] of Object.entries(row.dimensions || {{}})) {{
          const select = document.querySelector(`select[data-dimension="${{name}}"][data-pair-id="${{row.pair_id}}"]`);
          if (select) select.value = value || "";
        }}
        const reason = document.querySelector(`[data-reason-for="${{row.pair_id}}"]`);
        if (reason) reason.value = row.reason || "";
      }}
      refreshPanels();
    }}

    function saveDraft() {{
      localStorage.setItem(draftKey, JSON.stringify(collectState()));
    }}

    function refreshPanels() {{
      for (const card of document.querySelectorAll(".pair-card")) {{
        const pairId = card.dataset.pairId;
        const checked = document.querySelector(`input[data-pair-id="${{pairId}}"]:checked`);
        const label = checked ? checked.value : null;
        const actionPanel = document.getElementById(`action-${{pairId}}`);
        const panel = document.getElementById(`machine-${{pairId}}`);
        const leftPrompt = document.getElementById(`prompt-left-${{pairId}}`);
        const rightPrompt = document.getElementById(`prompt-right-${{pairId}}`);
        if (panel) panel.hidden = !checked;
        if (leftPrompt) leftPrompt.hidden = !checked;
        if (rightPrompt) rightPrompt.hidden = !checked;
        if (actionPanel) {{
          actionPanel.textContent = actionTextForLabel(label, card);
        }}
      }}
    }}

    document.getElementById("download").addEventListener("click", () => {{
      const payload = collectState();
      if (!payload.reviewer) {{
        message.textContent = "검토자 이름을 입력하세요.";
        return;
      }}
      const blob = new Blob([JSON.stringify(payload, null, 2)], {{ type: "application/json" }});
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "human-similarity-review-v2.labels.json";
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      saveDraft();
      message.textContent = "v2 검토 JSON을 내려받았습니다.";
    }});

    document.getElementById("clear-draft").addEventListener("click", () => {{
      localStorage.removeItem(draftKey);
      location.reload();
    }});

    document.getElementById("import-file").addEventListener("change", async (event) => {{
      const file = event.target.files?.[0];
      if (!file) return;
      try {{
        const payload = JSON.parse(await file.text());
        let normalized = payload;
        if (payload.schema_version === "{REVIEW_LABELS_SCHEMA_VERSION}") {{
          normalized = migrateV1Payload(payload);
          message.textContent = normalized.migration_note || "v1 초안을 v2로 옮겼습니다.";
        }}
        validateV2Bindings(normalized);
        applyState(normalized);
        saveDraft();
        if (!message.textContent) {{
          message.textContent = "검토 JSON을 불러왔습니다.";
        }}
      }} catch (error) {{
        message.textContent = error instanceof Error ? error.message : "검토 JSON을 읽지 못했습니다.";
      }}
    }});

    document.addEventListener("input", () => {{
      refreshPanels();
      saveDraft();
    }});

    try {{
      const newDraft = localStorage.getItem(draftKey);
      if (newDraft) {{
        try {{
          const payload = JSON.parse(newDraft);
          validateV2Bindings(payload);
          applyState(payload);
          message.textContent = "v2 로컬 초안을 복원했습니다.";
        }} catch (error) {{
          message.textContent = "v2 로컬 초안을 자동 복원하지 못했습니다. 초안은 보존되었습니다. 필요 시 검토 JSON을 백업한 뒤 직접 지우세요.";
        }}
      }} else {{
        const oldDraft = localStorage.getItem(oldDraftKey);
        if (oldDraft) {{
          try {{
            const migrated = migrateV1Payload(JSON.parse(oldDraft));
            applyState(migrated);
            localStorage.setItem(draftKey, JSON.stringify(migrated));
            message.textContent = migrated.migration_note || "v1 초안을 v2로 복사했습니다.";
          }} catch (error) {{
            message.textContent = "기존 v1 초안을 자동 복원하지 못했습니다. 원본 초안은 그대로 보존되었습니다.";
          }}
        }}
      }}
    }} catch (error) {{
      message.textContent = "로컬 초안 상태를 확인하지 못했습니다. 기존 초안은 삭제하지 않았습니다.";
    }}
    refreshPanels();
  </script>
</body>
</html>"""


def plan_human_review_v2_build(
    root: Path,
    source_run_id: str,
    *,
    comparison_dir: str | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    stored_v1_spec, source = load_bound_review_spec_v1(root, source_run_id, comparison_dir=comparison_dir)
    spec_v2 = _build_v2_spec_from_bound_v1(source_run_id, stored_v1_spec, source)
    return {
        "status": "dry_run",
        "run_id": source_run_id,
        "source_review_spec_sha256": stored_v1_spec["review_spec_sha256"],
        "provider": spec_v2["provider"],
        "model": spec_v2["model"],
        "dimensions": spec_v2["dimensions"],
        "comparison_dir": spec_v2["comparison_dir"],
        "sampled_pairs": spec_v2["counts"]["sampled_pairs"],
        "total_pairs": spec_v2["counts"]["total_pairs"],
        "network_calls": 0,
        "writes": 0,
    }


def build_human_review_v2_artifacts(
    root: Path,
    source_run_id: str,
    *,
    comparison_dir: str | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    stored_v1_spec, source = load_bound_review_spec_v1(root, source_run_id, comparison_dir=comparison_dir)
    destination = run_path(root, source_run_id)
    spec_path = destination / REVIEW_V2_SPEC_FILENAME
    template_path = destination / REVIEW_V2_TEMPLATE_FILENAME
    summary_path = destination / REVIEW_V2_SUMMARY_FILENAME
    html_path = destination / REVIEW_V2_HTML_FILENAME
    generated_spec = _build_v2_spec_from_bound_v1(source_run_id, stored_v1_spec, source)
    spec_v2 = generated_spec
    if spec_path.exists():
        existing_spec = read_json(spec_path)
        validate_stored_review_spec(existing_spec, source_run_id=source_run_id)
        if normalized_review_spec_for_drift(existing_spec) != normalized_review_spec_for_drift(generated_spec):
            raise ValueError(f"refusing to overwrite existing artifact: {spec_path.name}")
        spec_v2 = existing_spec
    template = blank_review_labels_v2(spec_v2)
    summary = _summary_payload_v2(spec_v2)
    _write_path_if_missing_or_same(spec_path, json_bytes(spec_v2))
    _write_path_if_missing_or_same(template_path, json_bytes(template))
    _write_path_if_missing_or_same(summary_path, json_bytes(summary))
    _write_path_if_missing_or_same(html_path, review_html_v2(spec_v2).encode("utf-8"))
    return {
        "status": "ready",
        "run_id": source_run_id,
        "source_review_spec_sha256": stored_v1_spec["review_spec_sha256"],
        "provider": spec_v2["provider"],
        "model": spec_v2["model"],
        "dimensions": spec_v2["dimensions"],
        "comparison_dir": spec_v2["comparison_dir"],
        "sampled_pairs": spec_v2["counts"]["sampled_pairs"],
        "total_pairs": spec_v2["counts"]["total_pairs"],
        "network_calls": 0,
        "writes": 4,
        "spec_path": str(spec_path),
        "template_path": str(template_path),
        "summary_path": str(summary_path),
        "html_path": str(html_path),
    }


__all__ = [
    "ACTION_BY_LABEL",
    "PAIR_LABELS_V2",
    "REVIEW_V2_HTML_FILENAME",
    "REVIEW_V2_LABELS_SCHEMA_VERSION",
    "REVIEW_V2_SPEC_FILENAME",
    "REVIEW_V2_SPEC_SCHEMA_VERSION",
    "blank_review_labels_v2",
    "build_human_review_v2_artifacts",
    "load_bound_review_spec_v2",
    "migrate_v1_labels_to_v2",
    "plan_human_review_v2_build",
    "review_html_v2",
    "summarize_thresholds_v2",
    "validate_review_labels_v2",
]
