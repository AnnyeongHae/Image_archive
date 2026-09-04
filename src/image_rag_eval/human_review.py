from __future__ import annotations

import html
import json
import math
import os
import re
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Any

from .experiment import digest, json_bytes, now, read_json, run_path, safe_source
from .comparison import add_order_evidence
from .retention import build_retention
from .similarity import compare_pair


DEFAULT_COMPARISON_DIR = "comparison-v1"
DEFAULT_PROVIDER = "voyage"
DEFAULT_MODEL = "voyage-multimodal-3.5"
DEFAULT_SEED = "voyage-human-similarity-review-v1"
MAX_REVIEW_PAIRS = 80
MAX_EXACT_CONTROL_PAIRS = 6
MAX_PAIRS_PER_IMAGE = 6
MIN_THRESHOLD_LABELS = 12
REVIEW_SPEC_FILENAME = "human-similarity-review.spec.json"
REVIEW_TEMPLATE_FILENAME = "human-similarity-review.template.json"
REVIEW_SUMMARY_FILENAME = "human-similarity-review-summary.json"
REVIEW_HTML_FILENAME = "human-similarity-review.html"
REVIEW_SPEC_SCHEMA_VERSION = "image-similarity-review-spec-1"
REVIEW_LABELS_SCHEMA_VERSION = "image-similarity-review-labels-1"
PAIR_LABELS = {
    "near_duplicate": "거의 같은 이미지. 같은 결과의 중복/재저장/아주 미세한 후처리 수준.",
    "same_visual_family": "주제·구도·스타일이 매우 비슷해 한 묶음으로 검토할 가치가 있는 경우.",
    "same_theme_only": "큰 주제만 비슷하고 실제 시각 결과는 꽤 다름.",
    "unrelated": "같은 묶음으로 볼 이유가 거의 없음.",
    "unsure": "판단 보류. 추가 검토가 필요함.",
}
DIMENSION_OPTIONS = {
    "composition": {
        "same": "구도 거의 동일",
        "similar": "구도 비슷",
        "different": "구도 다름",
        "unsure": "판단 보류",
    },
    "style": {
        "same": "스타일 거의 동일",
        "similar": "스타일 비슷",
        "different": "스타일 다름",
        "unsure": "판단 보류",
    },
    "subject": {
        "same": "주제/대상 거의 동일",
        "similar": "주제/대상 비슷",
        "different": "주제/대상 다름",
        "unsure": "판단 보류",
    },
}


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _bounded_text(value: str, byte_limit: int) -> str:
    return value.encode("utf-8")[:byte_limit].decode("utf-8", errors="ignore").strip()


def _escape(value: Any) -> str:
    return html.escape(_text(value), quote=True)


def _safe_rel_image(path_value: Any) -> str:
    raw = _text(path_value).strip().replace("\\", "/")
    if not raw:
        return ""
    drive, tail = os.path.splitdrive(raw)
    if drive:
        raw = tail.replace("\\", "/")
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    safe_parts = [part for part in parts if part != ".."]
    if not safe_parts:
        return ""
    if safe_parts[0] != "inputs":
        safe_parts = ["inputs", safe_parts[-1]]
    return "/".join(safe_parts)


def _normalize_comparison_dir(value: str) -> str:
    candidate = Path(value.strip())
    normalized = candidate.name
    if normalized != value.strip() or normalized in {"", ".", ".."}:
        raise ValueError("comparison dir must be a safe basename")
    return normalized


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


def _vector_as_unit(vector: Any) -> list[float]:
    if not isinstance(vector, list) or not vector:
        raise ValueError("vector must be a non-empty list")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector):
        raise ValueError("vector must contain only finite numbers")
    norm = math.sqrt(sum(float(v) * float(v) for v in vector))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("vector norm must be positive")
    if abs(norm - 1.0) > 1e-3:
        raise ValueError("vector must be unit-normalized")
    return [float(v) for v in vector]


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("vectors must have equal dimensions")
    return round(sum(a * b for a, b in zip(left, right)), 6)


def _pair_id(left: dict[str, Any], right: dict[str, Any]) -> str:
    left_id = _text(left.get("id")).strip()
    right_id = _text(right.get("id")).strip()
    if not left_id or not right_id or left_id == right_id:
        raise ValueError("pair items must have distinct ids")
    ids = sorted((left_id, right_id))
    payload = {
        "ids": ids,
        "prepared_sha256": sorted((_text(left.get("prepared_sha256")).strip(), _text(right.get("prepared_sha256")).strip())),
        "source_sha256": sorted((_text(left.get("sha256")).strip(), _text(right.get("sha256")).strip())),
    }
    return "pair-" + digest(json_bytes(payload))[:24]


def _stable_order(seed: str, pair_id: str) -> str:
    return digest(f"{seed}:{pair_id}".encode("utf-8"))


def _prompt_preview(item: dict[str, Any], byte_limit: int = 280) -> str:
    prompt = _text(item.get("embedding_prompt") or item.get("prompt")).strip()
    if not prompt:
        return "프롬프트 없음"
    compact = re.sub(r"\s+", " ", prompt).strip()
    preview = _bounded_text(compact, byte_limit)
    if preview != compact:
        preview += " …"
    return preview


def _item_binding(item: dict[str, Any]) -> dict[str, Any]:
    item_id = _text(item.get("id")).strip()
    if not item_id:
        raise ValueError("item id missing")
    source_sha = _text(item.get("sha256")).strip()
    prepared_sha = _text(item.get("prepared_sha256")).strip()
    prepared_path = _safe_rel_image(item.get("prepared_path"))
    if not source_sha or not prepared_sha or not prepared_path:
        raise ValueError("item image bindings incomplete")
    return {
        "id": item_id,
        "style_id": _text(item.get("style_id")).strip() or item_id,
        "source_sha256": source_sha,
        "prepared_sha256": prepared_sha,
        "prepared_path": prepared_path,
        "prompt_intent": _prompt_preview(item),
        "review_status": _text(item.get("review_status")).strip() or "unknown",
    }


def _infer_voyage_model(manifest: dict[str, Any], evaluation: dict[str, Any]) -> str:
    for row in evaluation.get("evaluations", []):
        provider = _text(row.get("provider")).strip()
        if provider:
            if "voyage" not in provider.casefold():
                raise ValueError("comparison results do not appear to be Voyage-backed")
            return provider
    selection = manifest.get("selection_profile")
    if isinstance(selection, dict):
        model = _text(selection.get("model")).strip()
        if model:
            if "voyage" not in model.casefold():
                raise ValueError("selection profile is not Voyage-backed")
            return model
    return DEFAULT_MODEL


def _vector_fingerprint(provider: str, model: str, dimensions: int, vectors_by_id: dict[str, list[float]]) -> str:
    entries = [
        {"id": item_id, "vector_sha256": digest(json_bytes(vector))}
        for item_id, vector in sorted(vectors_by_id.items())
    ]
    return digest(json_bytes({
        "provider": provider,
        "model": model,
        "dimensions": dimensions,
        "entries": entries,
    }))


def _pair_record(
    left: dict[str, Any],
    right: dict[str, Any],
    left_vector: list[float],
    right_vector: list[float],
) -> dict[str, Any]:
    local = compare_pair(left, right)
    left_binding = _item_binding(left)
    right_binding = _item_binding(right)
    return {
        "pair_id": _pair_id(left, right),
        "left": left_binding,
        "right": right_binding,
        "voyage_cosine": _cosine(left_vector, right_vector),
        "machine_candidate": {
            "local_relation": _text(local.get("candidate_relation")).strip() or "unknown",
            "prompt_exact": bool(local.get("prompt_exact")),
            "prompt_normalized_match": bool(local.get("prompt_normalized_match")),
            "phash_hamming": local.get("phash_hamming"),
            "dhash_hamming": local.get("dhash_hamming"),
            "aspect_ratio_delta": local.get("aspect_ratio_delta"),
            "color_histogram_l1": local.get("color_histogram_l1"),
            "evidence_flags": list(local.get("evidence_flags", [])),
        },
    }


def _effective_retention(root: Path, manifest: dict[str, Any], supplied_retention: dict[str, Any] | None) -> tuple[dict[str, Any], str]:
    items = manifest.get("items")
    if not isinstance(items, list):
        raise ValueError("manifest items missing")
    if supplied_retention is None:
        ordered_manifest = manifest
        canonical_path = root / "data/canonical/archive_records.jsonl"
        if canonical_path.exists():
            catalog_keys = [_text(item.get("catalog_key")).strip() for item in items]
            if all(catalog_keys):
                ordered_manifest = add_order_evidence(root, manifest)
        return build_retention(ordered_manifest["items"]), "recomputed_current_retention_policy"

    item_ids = {_text(item.get("id")).strip() for item in items}
    active_ids = supplied_retention.get("active_ids")
    archived = supplied_retention.get("archived")
    if not isinstance(active_ids, list) or not active_ids:
        raise ValueError("retention active_ids are required when retention is present")
    if len(set(active_ids)) != len(active_ids):
        raise ValueError("retention active_ids must be unique")
    if not isinstance(archived, list):
        raise ValueError("retention archived must be a list")

    archived_ids: list[str] = []
    representative_ids: list[str] = []
    for row in archived:
        if not isinstance(row, dict):
            raise ValueError("retention archived rows must be objects")
        archived_id = _text(row.get("id")).strip()
        representative_id = _text(row.get("representative_id")).strip()
        if not archived_id:
            raise ValueError("retention archived rows must include id")
        if not representative_id:
            raise ValueError("retention archived rows must include representative_id")
        archived_ids.append(archived_id)
        representative_ids.append(representative_id)

    if len(set(archived_ids)) != len(archived_ids):
        raise ValueError("retention archived ids must be unique")
    unknown = [item_id for item_id in [*active_ids, *archived_ids, *representative_ids] if item_id not in item_ids]
    if unknown:
        raise ValueError("retention references unknown item ids")
    if set(active_ids).intersection(archived_ids):
        raise ValueError("retention active_ids and archived ids must not overlap")
    if set(active_ids).union(archived_ids) != item_ids:
        raise ValueError("retention must partition the manifest item ids completely")
    if any(rep not in set(active_ids) for rep in representative_ids):
        raise ValueError("retention representatives must remain active")
    canonical_path = root / "data/canonical/archive_records.jsonl"
    if canonical_path.exists():
        catalog_keys = [_text(item.get("catalog_key")).strip() for item in items]
        if all(catalog_keys):
            ordered_manifest = add_order_evidence(root, manifest)
            computed = build_retention(ordered_manifest["items"])
            if supplied_retention.get("policy") and supplied_retention.get("policy") != computed.get("policy"):
                raise ValueError("retention policy does not match the current ordered retention policy")
            if active_ids != computed.get("active_ids"):
                raise ValueError("retention active_ids do not match the current ordered retention policy")
            supplied_pairs = sorted((_text(row.get("id")).strip(), _text(row.get("representative_id")).strip()) for row in archived)
            computed_pairs = sorted(
                (_text(row.get("id")).strip(), _text(row.get("representative_id")).strip())
                for row in computed.get("archived", [])
                if isinstance(row, dict)
            )
            if supplied_pairs != computed_pairs:
                raise ValueError("retention archived representative links do not match the current ordered retention policy")
    return supplied_retention, "comparison-v1 retention.active_ids (source_frozen_validated)"


def load_review_source(root: Path, source_run_id: str, *, comparison_dir: str = DEFAULT_COMPARISON_DIR) -> dict[str, Any]:
    comparison_name = _normalize_comparison_dir(comparison_dir)
    source = run_path(root, source_run_id)
    manifest = read_json(source / "manifest.json")
    prepared = read_json(source / "prepared.json")
    items = manifest.get("items")
    if not isinstance(items, list) or len(items) < 2:
        raise ValueError("human review requires at least two manifest items")
    if prepared.get("complete") is not True or prepared.get("manifest_sha256") != digest(json_bytes(manifest)):
        raise ValueError("private sample preparation is incomplete or changed")
    comparison_path = source / comparison_name
    vectors_payload = read_json(comparison_path / "vectors.json")
    evaluation = read_json(comparison_path / "evaluation.json") if (comparison_path / "evaluation.json").exists() else {}
    supplied_retention = read_json(comparison_path / "retention.json") if (comparison_path / "retention.json").exists() else None
    voyage_vectors = vectors_payload.get("voyage_image")
    if not isinstance(voyage_vectors, dict) or not voyage_vectors:
        raise ValueError("complete voyage_image vectors are required")
    item_ids = []
    item_lookup: dict[str, dict[str, Any]] = {}
    for item in items:
        item_id = _text(item.get("id")).strip()
        if not item_id or item_id in item_lookup:
            raise ValueError("manifest item ids must be unique")
        source_path = safe_source(root, _text(item.get("path")).strip())
        if digest(source_path.read_bytes()) != _text(item.get("sha256")).strip():
            raise ValueError("source asset changed")
        relative = Path(_text(item.get("prepared_path")).strip())
        prepared_path = (source / relative).resolve()
        if relative.is_absolute() or not prepared_path.is_relative_to((source / "inputs").resolve()):
            raise ValueError("prepared input escapes run")
        prepared_bytes = prepared_path.read_bytes()
        if digest(prepared_bytes) != _text(item.get("prepared_sha256")).strip():
            raise ValueError("prepared input changed")
        item_ids.append(item_id)
        item_lookup[item_id] = item
    if set(voyage_vectors) != set(item_ids):
        raise ValueError("voyage vectors must exactly match manifest ids")
    retention, retention_basis = _effective_retention(root, manifest, supplied_retention)
    normalized_vectors: dict[str, list[float]] = {}
    dimensions = None
    for item_id in item_ids:
        vector = _vector_as_unit(voyage_vectors[item_id])
        if dimensions is None:
            dimensions = len(vector)
        elif len(vector) != dimensions:
            raise ValueError("voyage vectors must have one fixed dimension")
        normalized_vectors[item_id] = vector
    if not isinstance(dimensions, int) or dimensions <= 0:
        raise ValueError("invalid voyage vector dimensions")
    model = _infer_voyage_model(manifest, evaluation)
    provider = DEFAULT_PROVIDER
    fingerprint = _vector_fingerprint(provider, model, dimensions, normalized_vectors)
    pairs = [
        _pair_record(item_lookup[left_id], item_lookup[right_id], normalized_vectors[left_id], normalized_vectors[right_id])
        for left_id, right_id in combinations(sorted(item_ids), 2)
    ]
    return {
        "schema_version": "1",
        "run_id": source_run_id,
        "comparison_dir": comparison_name,
        "provider": provider,
        "model": model,
        "dimensions": dimensions,
        "manifest_sha256": digest(json_bytes(manifest)),
        "vector_fingerprint": fingerprint,
        "manifest": manifest,
        "items": items,
        "pair_records": pairs,
        "retention": retention,
        "retention_basis": retention_basis,
        "source_dir": source,
    }


def _bucket_key(pair: dict[str, Any], seed: str) -> tuple[Any, ...]:
    return (_stable_order(seed, pair["pair_id"]), pair["pair_id"])


def sample_review_pairs(
    pair_records: list[dict[str, Any]],
    *,
    active_ids: set[str] | None = None,
    max_pairs: int = MAX_REVIEW_PAIRS,
    seed: str = DEFAULT_SEED,
    max_exact_controls: int = MAX_EXACT_CONTROL_PAIRS,
    per_image_cap: int = MAX_PAIRS_PER_IMAGE,
) -> list[dict[str, Any]]:
    if not 1 <= max_pairs <= MAX_REVIEW_PAIRS:
        raise ValueError(f"max_pairs must be between 1 and {MAX_REVIEW_PAIRS}")
    if max_exact_controls < 0:
        raise ValueError("max_exact_controls must be >= 0")
    if per_image_cap < 1:
        raise ValueError("per_image_cap must be >= 1")
    if active_ids is None:
        active_ids = {
            _text(pair.get("left", {}).get("id")).strip()
            for pair in pair_records
        }.union({
            _text(pair.get("right", {}).get("id")).strip()
            for pair in pair_records
        })
    exact_relations = {"exact_file", "exact_pixels", "near_copy_candidate"}
    eligible_non_exact = [
        pair
        for pair in pair_records
        if (
            _text(pair["left"]["id"]).strip() in active_ids
            and _text(pair["right"]["id"]).strip() in active_ids
            and pair["machine_candidate"]["local_relation"] not in exact_relations
        )
    ]
    local_exact = sorted(
        [
            pair for pair in pair_records
            if pair["machine_candidate"]["local_relation"] in exact_relations
        ],
        key=lambda pair: (-pair["voyage_cosine"], _bucket_key(pair, seed)),
    )
    prompt_challenge = sorted(
        [
            pair for pair in eligible_non_exact
            if pair["machine_candidate"]["prompt_exact"] or pair["machine_candidate"]["prompt_normalized_match"]
        ],
        key=lambda pair: (pair["voyage_cosine"], _bucket_key(pair, seed)),
    )
    high_similarity = sorted(
        eligible_non_exact,
        key=lambda pair: (-pair["voyage_cosine"], _bucket_key(pair, seed)),
    )
    boundary = sorted(
        eligible_non_exact,
        key=lambda pair: (abs(pair["voyage_cosine"] - 0.90), -pair["voyage_cosine"], _bucket_key(pair, seed)),
    )
    negative = sorted(
        eligible_non_exact,
        key=lambda pair: (pair["voyage_cosine"], _bucket_key(pair, seed)),
    )
    member_counts: dict[str, int] = {}

    def allow_pair(pair: dict[str, Any], *, enforce_cap: bool) -> bool:
        if not enforce_cap:
            return True
        left_id = _text(pair["left"]["id"]).strip()
        right_id = _text(pair["right"]["id"]).strip()
        return member_counts.get(left_id, 0) < per_image_cap and member_counts.get(right_id, 0) < per_image_cap

    def mark_pair(pair: dict[str, Any], *, enforce_cap: bool) -> None:
        if not enforce_cap:
            return
        left_id = _text(pair["left"]["id"]).strip()
        right_id = _text(pair["right"]["id"]).strip()
        member_counts[left_id] = member_counts.get(left_id, 0) + 1
        member_counts[right_id] = member_counts.get(right_id, 0) + 1

    quotas = [
        ("local_exact_or_near_copy", local_exact, max_exact_controls, False),
        ("prompt_match_challenge", prompt_challenge, 12, True),
        ("high_similarity", high_similarity, 28, True),
        ("boundary_hypothesis_0.90", boundary, 22, True),
        ("negative_challenge", negative, 12, True),
    ]
    selected: set[str] = set()
    sampled: list[dict[str, Any]] = []
    for name, bucket, limit, enforce_cap in quotas:
        if len(sampled) >= max_pairs:
            break
        remaining = min(limit, max_pairs - len(sampled))
        picked: list[dict[str, Any]] = []
        for pair in bucket:
            if pair["pair_id"] in selected or not allow_pair(pair, enforce_cap=enforce_cap):
                continue
            item = dict(pair)
            item["sampling_bucket"] = name
            picked.append(item)
            selected.add(pair["pair_id"])
            mark_pair(pair, enforce_cap=enforce_cap)
            if len(picked) >= remaining:
                break
        sampled.extend(picked)
    if len(sampled) < max_pairs:
        remainder = sorted(
            eligible_non_exact,
            key=lambda pair: (
                abs(pair["voyage_cosine"] - 0.90),
                -pair["voyage_cosine"],
                _bucket_key(pair, seed),
            ),
        )
        picked: list[dict[str, Any]] = []
        for pair in remainder:
            if pair["pair_id"] in selected or not allow_pair(pair, enforce_cap=True):
                continue
            item = dict(pair)
            item["sampling_bucket"] = "remainder_fill"
            picked.append(item)
            selected.add(pair["pair_id"])
            mark_pair(pair, enforce_cap=True)
            if len(picked) >= max_pairs - len(sampled):
                break
        sampled.extend(picked)
    return sampled


def build_review_spec(
    source: dict[str, Any],
    *,
    max_pairs: int = MAX_REVIEW_PAIRS,
    seed: str = DEFAULT_SEED,
) -> dict[str, Any]:
    active_ids = {
        _text(item_id).strip()
        for item_id in source["retention"].get("active_ids", [])
        if _text(item_id).strip()
    }
    sampled_pairs = sample_review_pairs(
        source["pair_records"],
        active_ids=active_ids,
        max_pairs=max_pairs,
        seed=seed,
    )
    spec = {
        "schema_version": REVIEW_SPEC_SCHEMA_VERSION,
        "created_at": now(),
        "run_id": source["run_id"],
        "comparison_dir": source["comparison_dir"],
        "provider": source["provider"],
        "model": source["model"],
        "arm": "voyage_image",
        "dimensions": source["dimensions"],
        "source_manifest_sha256": source["manifest_sha256"],
        "vector_fingerprint": source["vector_fingerprint"],
        "sampling_seed": seed,
        "sampling_strategy": {
            "max_pairs": max_pairs,
            "total_possible_pairs": len(source["pair_records"]),
            "active_item_count": len(active_ids),
            "active_only_non_exact_pairs": True,
            "exact_control_pair_cap": MAX_EXACT_CONTROL_PAIRS,
            "per_image_cap_non_exact": MAX_PAIRS_PER_IMAGE,
            "challenge_sample_bias_warning": "이 표본은 전체 N^2 쌍을 다 보지 않고 고유사도/경계/음성 도전 표본을 섞은 검토용 샘플입니다. 정확도나 임계값 보정 성능을 주장할 수 없습니다.",
            "bucket_notes": {
                "local_exact_or_near_copy": "쉬운 exact/near-copy control은 최대 6쌍만 포함합니다. 삭제 결정을 위한 것이 아닙니다.",
                "high_similarity": "active set 내부에서 Voyage cosine이 높은 쌍을 우선 포함합니다.",
                "boundary_hypothesis_0.90": "active set 내부의 0.90 근처 경계 쌍을 포함합니다. 운영 임계값을 의미하지 않습니다.",
                "prompt_match_challenge": "active set 내부의 같은 프롬프트 계열 도전 표본을 포함합니다.",
                "negative_challenge": "active set 내부의 낮은 cosine 음성 도전 표본입니다.",
                "remainder_fill": "정해진 quota 이후 남은 슬롯을 active set 내부에서 결정적으로 보충한 표본입니다.",
            },
            "threshold_hypotheses": {
                "candidate_only": [0.85, 0.90],
                "note": "0.85/0.90은 검토 우선순위 가설일 뿐이며 보정되거나 승인된 운영 임계값이 아닙니다.",
            },
        },
        "retention_basis": source["retention_basis"],
        "human_label_definitions": PAIR_LABELS,
        "dimension_definitions": DIMENSION_OPTIONS,
        "pairs": sampled_pairs,
        "counts": {
            "items": len(source["items"]),
            "sampled_pairs": len(sampled_pairs),
            "total_pairs": len(source["pair_records"]),
        },
    }
    spec["review_spec_sha256"] = digest(json_bytes(spec))
    return spec


def blank_review_labels(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": REVIEW_LABELS_SCHEMA_VERSION,
        "review_spec_sha256": _text(spec.get("review_spec_sha256")).strip(),
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
                "human_label": None,
                "human_verified": False,
                "dimensions": {name: None for name in DIMENSION_OPTIONS},
                "reason": "",
            }
            for pair in spec["pairs"]
        ],
        "notes": "사람 검토 결과만 기록합니다. AI 승인/삭제/병합/공개 승인은 아닙니다.",
    }


def validate_review_labels(spec: dict[str, Any], labels: dict[str, Any]) -> dict[str, Any]:
    if labels.get("schema_version") != REVIEW_LABELS_SCHEMA_VERSION:
        raise ValueError("unknown human review labels schema")
    expected_bindings = {
        pair["pair_id"]: pair
        for pair in spec.get("pairs", [])
    }
    if not expected_bindings:
        raise ValueError("review spec has no pairs")
    for field, expected in (
        ("review_spec_sha256", spec.get("review_spec_sha256")),
        ("run_id", spec.get("run_id")),
        ("provider", spec.get("provider")),
        ("model", spec.get("model")),
        ("dimensions", spec.get("dimensions")),
        ("source_manifest_sha256", spec.get("source_manifest_sha256")),
        ("vector_fingerprint", spec.get("vector_fingerprint")),
    ):
        if labels.get(field) != expected:
            raise ValueError(f"labels {field} mismatch")
    reviewer = _text(labels.get("reviewer")).strip()
    reviewed_at = _validated_reviewed_at(labels.get("reviewed_at"))
    if not reviewer:
        raise ValueError("reviewer identity and reviewed_at are required")
    submitted_pairs = labels.get("pairs")
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
        label = row.get("human_label")
        if label is not None and _text(label).strip() not in PAIR_LABELS:
            raise ValueError("unknown human similarity label")
        normalized_label = _text(label).strip() or None
        verified = row.get("human_verified")
        if not isinstance(verified, bool):
            raise ValueError("human_verified must be boolean")
        if verified and normalized_label is None:
            raise ValueError("verified pairs must include a human label")
        if verified and normalized_label == "unsure":
            raise ValueError("unsure pairs cannot be marked human_verified")
        dimensions = row.get("dimensions")
        if not isinstance(dimensions, dict):
            raise ValueError("dimension review object missing")
        normalized_dimensions = {}
        for name, options in DIMENSION_OPTIONS.items():
            value = dimensions.get(name)
            if value is not None and _text(value).strip() not in options:
                raise ValueError(f"invalid optional dimension label for {name}")
            normalized_dimensions[name] = _text(value).strip() or None
        reason = _bounded_text(_text(row.get("reason")).strip(), 1000)
        normalized_pairs.append({
            "pair_id": pair_id,
            "left": row["left"],
            "right": row["right"],
            "human_label": normalized_label,
            "human_verified": verified,
            "dimensions": normalized_dimensions,
            "reason": reason,
        })
    return {
        **labels,
        "reviewer": reviewer,
        "reviewed_at": reviewed_at,
        "pairs": normalized_pairs,
    }


def summarize_thresholds(
    spec: dict[str, Any],
    labels: dict[str, Any],
    *,
    minimum_verified_pairs: int = MIN_THRESHOLD_LABELS,
) -> dict[str, Any]:
    if minimum_verified_pairs < 1:
        raise ValueError("minimum_verified_pairs must be >= 1")
    normalized = validate_review_labels(spec, labels)
    pair_lookup = {pair["pair_id"]: pair for pair in spec["pairs"]}
    labeled = [row for row in normalized["pairs"] if row["human_label"] is not None]
    unresolved = [row for row in labeled if row["human_label"] == "unsure"]
    verified = [
        row
        for row in normalized["pairs"]
        if row["human_verified"] is True and row["human_label"] is not None and row["human_label"] != "unsure"
    ]
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
    strict_positive = {"near_duplicate"}
    broad_positive = {"near_duplicate", "same_visual_family"}
    thresholds = [0.75, 0.80, 0.85, 0.90, 0.95]
    rows = []
    for threshold in thresholds:
        matched = [row for row in verified if pair_lookup[row["pair_id"]]["voyage_cosine"] >= threshold]
        strict_hits = sum(1 for row in matched if row["human_label"] in strict_positive)
        broad_hits = sum(1 for row in matched if row["human_label"] in broad_positive)
        rows.append({
            "threshold": threshold,
            "reviewed_pairs_at_or_above_threshold": len(matched),
            "sampled_match_rate_strict": round(strict_hits / len(matched), 6) if matched else None,
            "sampled_match_rate_broad": round(broad_hits / len(matched), 6) if matched else None,
        })
    label_counts: dict[str, int] = {}
    for row in verified:
        label_counts[row["human_label"]] = label_counts.get(row["human_label"], 0) + 1
    return {
        "status": "ok",
        "labeled_pairs": len(labeled),
        "unresolved_pairs": len(unresolved),
        "verified_pairs": len(verified),
        "label_counts": label_counts,
        "threshold_summary": rows,
        "bias_warning": "이 요약은 검토된 challenge sample에만 해당합니다. 전체 정확도, 재현율, 임계값 보정 성능을 주장할 수 없습니다.",
    }


def _bucket_counts(spec: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for pair in spec.get("pairs", []):
        bucket = _text(pair.get("sampling_bucket")).strip() or "unknown"
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def _summary_payload(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "image-similarity-review-summary-1",
        "created_at": spec["created_at"],
        "run_id": spec["run_id"],
        "provider": spec["provider"],
        "model": spec["model"],
        "dimensions": spec["dimensions"],
        "comparison_dir": spec["comparison_dir"],
        "source_manifest_sha256": spec["source_manifest_sha256"],
        "vector_fingerprint": spec["vector_fingerprint"],
        "sampled_pairs": spec["counts"]["sampled_pairs"],
        "total_pairs": spec["counts"]["total_pairs"],
        "items": spec["counts"]["items"],
        "active_items": spec["sampling_strategy"]["active_item_count"],
        "retention_basis": spec["retention_basis"],
        "bucket_counts": _bucket_counts(spec),
        "network_calls": 0,
        "writes": 4,
        "bias_warning": spec["sampling_strategy"]["challenge_sample_bias_warning"],
        "threshold_hypotheses": spec["sampling_strategy"]["threshold_hypotheses"],
    }


def _radio_group(name: str, label: str, pair_id: str) -> str:
    buttons = []
    for value, description in PAIR_LABELS.items():
        buttons.append(
            '<label class="radio">'
            f'<input type="radio" name="{_escape(name)}" value="{_escape(value)}" data-pair-id="{_escape(pair_id)}">'
            f'<span>{_escape(value)}</span><small>{_escape(description)}</small>'
            "</label>"
        )
    return f'<fieldset><legend>{_escape(label)}</legend><div class="label-grid">{"".join(buttons)}</div></fieldset>'


def _dimension_select(name: str, pair_id: str) -> str:
    options = ['<option value="">선택 안 함</option>']
    for value, description in DIMENSION_OPTIONS[name].items():
        options.append(f'<option value="{_escape(value)}">{_escape(description)}</option>')
    return (
        '<label class="dimension">'
        f'{_escape(name)}'
        f'<select data-dimension="{_escape(name)}" data-pair-id="{_escape(pair_id)}">{"".join(options)}</select>'
        '</label>'
    )


def review_html(spec: dict[str, Any]) -> str:
    cards = []
    for pair in spec["pairs"]:
        machine = pair["machine_candidate"]
        hidden_machine = {
            "voyage_cosine": pair["voyage_cosine"],
            "local_relation": machine["local_relation"],
            "prompt_exact": machine["prompt_exact"],
            "prompt_normalized_match": machine["prompt_normalized_match"],
            "threshold_hypothesis": "0.85/0.90_candidate_only_not_calibrated",
        }
        cards.append(
            '<article class="pair-card" '
            f'data-pair-id="{_escape(pair["pair_id"])}" '
            f"data-machine='{_escape(json.dumps(hidden_machine, ensure_ascii=False))}'>"
            f'<h2>{_escape(pair["pair_id"])} <span class="bucket">{_escape(pair["sampling_bucket"])}</span></h2>'
            '<div class="pair-grid">'
            f'<section><img loading="lazy" src="{_escape(pair["left"]["prepared_path"])}" alt="{_escape(pair["left"]["style_id"])} 미리보기"><h3>{_escape(pair["left"]["style_id"])}</h3><p class="mono">{_escape(pair["left"]["id"])}</p><p>{_escape(pair["left"]["review_status"])}</p><div class="prompt-gate">시각 라벨 선택 후 프롬프트 의도 공개</div><details class="prompt-panel" id="prompt-left-{_escape(pair["pair_id"])}" hidden><summary>프롬프트 의도</summary><p>{_escape(pair["left"]["prompt_intent"])}</p></details></section>'
            f'<section><img loading="lazy" src="{_escape(pair["right"]["prepared_path"])}" alt="{_escape(pair["right"]["style_id"])} 미리보기"><h3>{_escape(pair["right"]["style_id"])}</h3><p class="mono">{_escape(pair["right"]["id"])}</p><p>{_escape(pair["right"]["review_status"])}</p><div class="prompt-gate">시각 라벨 선택 후 프롬프트 의도 공개</div><details class="prompt-panel" id="prompt-right-{_escape(pair["pair_id"])}" hidden><summary>프롬프트 의도</summary><p>{_escape(pair["right"]["prompt_intent"])}</p></details></section>'
            '</div>'
            f'{_radio_group(f"label-{pair["pair_id"]}", "사람 유사도 라벨", pair["pair_id"])}'
            '<div class="dimension-row">'
            f'{_dimension_select("composition", pair["pair_id"])}'
            f'{_dimension_select("style", pair["pair_id"])}'
            f'{_dimension_select("subject", pair["pair_id"])}'
            '</div>'
            f'<label class="reason">판단 근거(선택) <textarea rows="3" maxlength="1000" data-reason-for="{_escape(pair["pair_id"])}"></textarea></label>'
            f'<div class="machine-panel" id="machine-{_escape(pair["pair_id"])}" hidden><strong>기계 후보 정보</strong><pre>{_escape(json.dumps(hidden_machine, ensure_ascii=False, indent=2))}</pre></div>'
            '</article>'
        )
    template = json.dumps(blank_review_labels(spec), ensure_ascii=False).replace("<", "\\u003c")
    spec_json = json.dumps({
        "review_spec_sha256": spec["review_spec_sha256"],
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
            }
            for pair in spec["pairs"]
        ],
    }, ensure_ascii=False).replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <title>Voyage 유사도 사람 검토</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0f1320;
      --panel: #182033;
      --line: #33405c;
      --text: #e8eef9;
      --muted: #9fb0c9;
      --accent: #d0a64d;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--text); font: 15px/1.5 system-ui, sans-serif; }}
    main {{ max-width: 1500px; margin: 0 auto; padding: 20px; }}
    h1, h2, h3, p {{ margin: 0 0 10px; }}
    .notice {{ background: var(--panel); border-left: 4px solid var(--accent); padding: 12px 14px; border-radius: 10px; margin-bottom: 18px; }}
    .toolbar {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin: 14px 0 18px; }}
    input, button, select, textarea {{ font: inherit; }}
    input[type=text], select, textarea {{ width: 100%; background: #0d1220; color: var(--text); border: 1px solid var(--line); border-radius: 8px; padding: 8px; }}
    button {{ background: #24324f; color: var(--text); border: 1px solid var(--line); border-radius: 8px; padding: 8px 12px; cursor: pointer; }}
    .pair-card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 14px; margin-bottom: 18px; overflow-wrap: anywhere; }}
    .pair-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
    .pair-grid section {{ background: #111827; border: 1px solid var(--line); border-radius: 10px; padding: 12px; }}
    img {{ width: 100%; height: 260px; object-fit: contain; background: #0b1020; border-radius: 8px; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }}
    fieldset {{ border: 1px solid var(--line); border-radius: 10px; margin: 12px 0; }}
    legend {{ padding: 0 6px; }}
    .label-grid {{ display: grid; gap: 8px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }}
    .radio {{ display: block; background: #111827; border-radius: 8px; padding: 8px; border: 1px solid var(--line); }}
    .radio span {{ display: block; font-weight: 600; margin-bottom: 4px; }}
    .radio small {{ color: var(--muted); display: block; }}
    .dimension-row {{ display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); margin-bottom: 12px; }}
    .reason {{ display: block; }}
    .machine-panel {{ background: #101826; border: 1px dashed var(--line); border-radius: 10px; padding: 10px; }}
    .prompt-gate {{ color: var(--muted); font-size: 13px; margin-bottom: 8px; }}
    .bucket {{ font-size: 12px; color: var(--muted); }}
    .status {{ color: var(--muted); }}
    pre {{ margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }}
    @media (max-width: 900px) {{
      .pair-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <h1>Voyage 유사도 사람 검토</h1>
    <p class="status">run_id: {_escape(spec["run_id"])} · provider: {_escape(spec["provider"])} · model: {_escape(spec["model"])} · {spec["counts"]["sampled_pairs"]}/{spec["counts"]["total_pairs"]} sampled pairs</p>
    <div class="notice">
      <p>이 페이지는 오프라인 검토용입니다. 네트워크 호출, 자동 삭제, 자동 병합, 공개 승인, AI의 사람 승인 대체를 하지 않습니다.</p>
      <p>기계 점수와 기계 후보 정보는 앵커링을 줄이기 위해 라벨 선택 전에는 숨깁니다.</p>
      <p><code>near_duplicate</code>는 사람의 시각 판단 라벨일 뿐이며 exact file/pixel 삭제 결정이 아닙니다. 이 화면은 삭제를 수행하지 않습니다.</p>
      <p>0.85/0.90 수치는 검토 우선순위 가설일 뿐이며 보정되거나 승인된 운영 임계값이 아닙니다.</p>
      <p>{_escape(spec["sampling_strategy"]["challenge_sample_bias_warning"])}</p>
      <p>라벨 정의: {" / ".join(f"{name}: {definition}" for name, definition in PAIR_LABELS.items())}</p>
    </div>
    <label>검토자 이름 <input id="reviewer" type="text" autocomplete="off" placeholder="실제 검토자 이름 또는 식별자"></label>
    <div class="toolbar">
      <button id="download">검토 JSON 내려받기</button>
      <button id="clear-draft" type="button">로컬 초안 지우기</button>
      <label>이전 검토 JSON 불러오기 <input id="import-file" type="file" accept="application/json"></label>
      <span id="message" class="status" role="status" aria-live="polite"></span>
    </div>
    {"".join(cards)}
  </main>
  <script>
    const template = {template};
    const spec = {spec_json};
    const draftKey = `image-rag-human-review:${{spec.review_spec_sha256}}`;
    const message = document.getElementById("message");

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

    function validateImported(payload) {{
      if (!payload || payload.review_spec_sha256 !== spec.review_spec_sha256 || payload.run_id !== spec.run_id) {{
        throw new Error("현재 review spec과 맞지 않는 파일입니다.");
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
        if (row.human_verified && row.human_label === "unsure") {{
          throw new Error("unsure pair는 human_verified=true일 수 없습니다.");
        }}
        for (const side of ["left", "right"]) {{
          if (!row[side] || row[side].id !== pair[side].id || row[side].prepared_sha256 !== pair[side].prepared_sha256 || row[side].source_sha256 !== pair[side].source_sha256) {{
            throw new Error("pair binding이 현재 샘플과 다릅니다.");
          }}
        }}
      }}
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
      refreshMachinePanels();
    }}

    function saveDraft() {{
      localStorage.setItem(draftKey, JSON.stringify(collectState()));
    }}

    function refreshMachinePanels() {{
      for (const card of document.querySelectorAll(".pair-card")) {{
        const pairId = card.dataset.pairId;
        const checked = document.querySelector(`input[data-pair-id="${{pairId}}"]:checked`);
        const panel = document.getElementById(`machine-${{pairId}}`);
        const leftPrompt = document.getElementById(`prompt-left-${{pairId}}`);
        const rightPrompt = document.getElementById(`prompt-right-${{pairId}}`);
        if (panel) panel.hidden = !checked;
        if (leftPrompt) leftPrompt.hidden = !checked;
        if (rightPrompt) rightPrompt.hidden = !checked;
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
      anchor.download = "human-similarity-review.labels.json";
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      saveDraft();
      message.textContent = "검토 JSON을 내려받았습니다.";
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
        validateImported(payload);
        applyState(payload);
        saveDraft();
        message.textContent = "검토 JSON을 불러왔습니다.";
      }} catch (error) {{
        message.textContent = error instanceof Error ? error.message : "검토 JSON을 읽지 못했습니다.";
      }}
    }});

    document.addEventListener("input", () => {{
      refreshMachinePanels();
      saveDraft();
    }});

    try {{
      const draft = localStorage.getItem(draftKey);
      if (draft) {{
        const payload = JSON.parse(draft);
        validateImported(payload);
        applyState(payload);
        message.textContent = "로컬 초안을 복원했습니다.";
      }}
    }} catch (error) {{
      localStorage.removeItem(draftKey);
      message.textContent = "손상된 로컬 초안을 지웠습니다.";
    }}
    refreshMachinePanels();
  </script>
</body>
</html>"""


def _spec_identity(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": spec.get("run_id"),
        "comparison_dir": spec.get("comparison_dir"),
        "provider": spec.get("provider"),
        "model": spec.get("model"),
        "arm": spec.get("arm"),
        "dimensions": spec.get("dimensions"),
        "source_manifest_sha256": spec.get("source_manifest_sha256"),
        "vector_fingerprint": spec.get("vector_fingerprint"),
        "sampling_seed": spec.get("sampling_seed"),
        "retention_basis": spec.get("retention_basis"),
        "pair_ids": [pair.get("pair_id") for pair in spec.get("pairs", [])],
    }


def stored_review_spec_path(root: Path, source_run_id: str, *, filename: str = REVIEW_SPEC_FILENAME) -> Path:
    return run_path(root, source_run_id) / filename


def normalized_review_spec_for_hash(spec: dict[str, Any]) -> dict[str, Any]:
    normalized = json.loads(json.dumps(spec))
    normalized.pop("review_spec_sha256", None)
    return normalized


def normalized_review_spec_for_drift(spec: dict[str, Any]) -> dict[str, Any]:
    normalized = normalized_review_spec_for_hash(spec)
    normalized.pop("created_at", None)
    return normalized


def validate_stored_review_spec(spec: dict[str, Any], *, source_run_id: str) -> None:
    expected = digest(json_bytes(normalized_review_spec_for_hash(spec)))
    actual = _text(spec.get("review_spec_sha256")).strip()
    if not actual or actual != expected:
        raise ValueError("stored review spec self-hash mismatch")
    if _text(spec.get("run_id")).strip() != source_run_id:
        raise ValueError("stored review spec run_id mismatch")


def _write_path_if_missing_or_same(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"refusing to overwrite existing artifact: {path.name}")
        return
    path.write_bytes(payload)


def plan_human_review_build(
    root: Path,
    source_run_id: str,
    *,
    comparison_dir: str = DEFAULT_COMPARISON_DIR,
    max_pairs: int = MAX_REVIEW_PAIRS,
    seed: str = DEFAULT_SEED,
) -> dict[str, Any]:
    source = load_review_source(root, source_run_id, comparison_dir=comparison_dir)
    spec = build_review_spec(source, max_pairs=max_pairs, seed=seed)
    summary = _summary_payload(spec)
    return {
        "status": "dry_run",
        "run_id": source_run_id,
        "provider": source["provider"],
        "model": source["model"],
        "dimensions": source["dimensions"],
        "comparison_dir": source["comparison_dir"],
        "manifest_sha256": source["manifest_sha256"],
        "vector_fingerprint": source["vector_fingerprint"],
        "active_items": len(source["retention"].get("active_ids", [])),
        "retention_basis": source["retention_basis"],
        "sampled_pairs": spec["counts"]["sampled_pairs"],
        "total_pairs": spec["counts"]["total_pairs"],
        "items": spec["counts"]["items"],
        "bucket_counts": summary["bucket_counts"],
        "network_calls": 0,
        "writes": 0,
    }


def build_human_review_artifacts(
    root: Path,
    source_run_id: str,
    *,
    comparison_dir: str = DEFAULT_COMPARISON_DIR,
    max_pairs: int = MAX_REVIEW_PAIRS,
    seed: str = DEFAULT_SEED,
) -> dict[str, Any]:
    source = load_review_source(root, source_run_id, comparison_dir=comparison_dir)
    destination = source["source_dir"]
    spec_path = destination / "human-similarity-review.spec.json"
    template_path = destination / "human-similarity-review.template.json"
    summary_path = destination / "human-similarity-review-summary.json"
    html_path = destination / "human-similarity-review.html"
    generated_spec = build_review_spec(source, max_pairs=max_pairs, seed=seed)
    spec = generated_spec
    if spec_path.exists():
        existing_spec = read_json(spec_path)
        if _spec_identity(existing_spec) != _spec_identity(generated_spec):
            raise ValueError(f"refusing to overwrite existing artifact: {spec_path.name}")
        spec = existing_spec
    template = blank_review_labels(spec)
    summary = _summary_payload(spec)
    _write_path_if_missing_or_same(spec_path, json_bytes(spec))
    _write_path_if_missing_or_same(template_path, json_bytes(template))
    _write_path_if_missing_or_same(summary_path, json_bytes(summary))
    _write_path_if_missing_or_same(html_path, review_html(spec).encode("utf-8"))
    return {
        "status": "ready",
        "run_id": source_run_id,
        "provider": source["provider"],
        "model": source["model"],
        "dimensions": source["dimensions"],
        "comparison_dir": source["comparison_dir"],
        "active_items": len(source["retention"].get("active_ids", [])),
        "retention_basis": source["retention_basis"],
        "sampled_pairs": spec["counts"]["sampled_pairs"],
        "total_pairs": spec["counts"]["total_pairs"],
        "items": spec["counts"]["items"],
        "bucket_counts": summary["bucket_counts"],
        "network_calls": 0,
        "writes": 4,
        "html_path": str(html_path),
        "spec_path": str(spec_path),
        "template_path": str(template_path),
        "summary_path": str(summary_path),
    }
