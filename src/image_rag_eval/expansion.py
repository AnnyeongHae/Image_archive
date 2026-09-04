"""Additive 20 -> 50 local-only preparation. Never rewrites the source run."""
from __future__ import annotations

import copy
import itertools
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from build_duplicate_index import BuildConfig, load_remote_overlay

from . import dataset as dataset_module
from .experiment import (
    MAX_QUERIES,
    MODEL,
    RELATION_LABELS,
    annotations_template,
    bounded_text,
    digest,
    json_bytes,
    now,
    prepared_image,
    read_json,
    review_html,
    run_path,
    safe_source,
    write_json,
)
from .similarity import build_groups, compare_pair, image_signals, prompt_signals


MAX_EXPANDED_IMAGES = 50
SOURCE_RUN_IMAGES = 20


def _validated_limit(limit: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("expanded preparation limit must be an integer between 21 and 50") from exc
    if value < SOURCE_RUN_IMAGES + 1 or value > MAX_EXPANDED_IMAGES:
        raise ValueError("expanded preparation limit must be between 21 and 50")
    return value


def _plan(image_count: int, query_count: int = MAX_QUERIES) -> dict[str, Any]:
    if not 1 <= image_count <= MAX_EXPANDED_IMAGES or not 0 <= query_count <= MAX_QUERIES:
        raise ValueError("expanded canary permits 1..50 images and 0..15 queries")
    return {
        "status": "dry_run",
        "network_calls": 0,
        "writes": 0,
        "model": MODEL,
        "preparation_only": True,
        "comparison_setup_pending": True,
        "arms": {},
        "max_images": image_count,
        "max_queries": query_count,
        "max_inference_calls": 0,
        "dimensions_requested": 3072,
        "dimensions_evaluated_locally": [768, 1536, 3072],
        "reservation_upper_bound_usd": None,
        "billing_basis": "comparison/orchestration handled separately by root after preparation",
        "requires": ["explicit_paid_budget_approval", "sample_external_ai_approval", "human_relevance_labels_for_accuracy"],
        "qdrant_writes": 0,
        "canonical_writes": 0,
        "automatic_retry": False,
    }


def _load_source_run(root: Path, source_run_id: str) -> tuple[Path, dict[str, Any]]:
    source_dir = run_path(root, source_run_id)
    manifest_path = source_dir / "manifest.json"
    receipt_path = source_dir / "prepared.json"
    if not manifest_path.is_file() or not receipt_path.is_file():
        raise FileNotFoundError("source run manifest or receipt is missing")
    manifest = read_json(manifest_path)
    receipt = read_json(receipt_path)
    if receipt.get("complete") is not True or receipt.get("manifest_sha256") != digest(json_bytes(manifest)):
        raise ValueError("source run preparation receipt mismatch")
    items = manifest.get("items", [])
    if not isinstance(items, list) or len(items) != SOURCE_RUN_IMAGES:
        raise ValueError("source run must contain exactly 20 prepared items")
    ids = [item.get("id") for item in items]
    if len(set(ids)) != len(ids):
        raise ValueError("source run contains duplicate item ids")
    return source_dir, manifest


def _validated_source_item(root: Path, source_dir: Path, item: dict[str, Any]) -> bytes:
    if len(str(item.get("embedding_prompt") or "").encode("utf-8")) > 6000:
        raise ValueError("source run contains an embedding prompt above 6000 UTF-8 bytes")
    source_path = safe_source(root, str(item.get("path") or ""))
    if digest(source_path.read_bytes()) != item.get("sha256"):
        raise ValueError("source item digest changed after source preparation")
    relative = Path(str(item.get("prepared_path") or ""))
    prepared_path = (source_dir / relative).resolve()
    if relative.is_absolute() or not prepared_path.is_relative_to((source_dir / "inputs").resolve()):
        raise ValueError("source prepared input escapes source run inputs")
    data = prepared_path.read_bytes()
    if digest(data) != item.get("prepared_sha256"):
        raise ValueError("source prepared input digest changed")
    return data


def _build_dataset_config(root: Path) -> BuildConfig:
    canonical_path = dataset_module._canonical_path(root)
    return BuildConfig(
        platform_root=root,
        canonical_path=canonical_path,
        legacy_root=root / "legacy" / "current_archive",
        remote_overlay_path=dataset_module._remote_overlay_path(root),
    )


def _candidate_budget(count: int) -> int:
    return max(count * 12, 400)


def _select_additional_candidates(root: Path, exclude_ids: set[str], count: int) -> list[dict[str, Any]]:
    connection = dataset_module._connect_index(dataset_module._duplicate_index_path(root))
    try:
        pool = dataset_module._all_asset_candidates(connection)
    finally:
        connection.close()

    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in pool:
        asset_id = str(row.get("asset_id") or "")
        if not asset_id or asset_id in exclude_ids:
            continue
        bucket = (str(row.get("source_name") or "").casefold(), str(row.get("lane") or "").casefold())
        buckets[bucket].append(row)

    for rows in buckets.values():
        rows.sort(
            key=lambda row: (
                str(row.get("catalog_key") or ""),
                int(row.get("asset_index") or 0),
                str(row.get("asset_id") or ""),
            )
        )

    ordered: list[dict[str, Any]] = []
    seen_assets = set(exclude_ids)
    seen_records: set[str] = set()
    budget = _candidate_budget(count)
    bucket_keys = sorted(buckets)
    while len(ordered) < budget:
        progress = False
        for bucket in bucket_keys:
            rows = buckets[bucket]
            while rows and str(rows[0].get("catalog_key") or "") in seen_records:
                rows.pop(0)
            if not rows:
                continue
            row = rows.pop(0)
            asset_id = str(row.get("asset_id") or "")
            if asset_id in seen_assets:
                continue
            if dataset_module._append_candidate(ordered, seen_assets, row):
                seen_records.add(str(row.get("catalog_key") or ""))
                progress = True
                if len(ordered) >= budget:
                    break
        if not progress:
            break

    if len(ordered) < budget:
        for row in pool:
            asset_id = str(row.get("asset_id") or "")
            if not asset_id or asset_id in seen_assets:
                continue
            if dataset_module._append_candidate(ordered, seen_assets, row) and len(ordered) >= budget:
                break

    return ordered


def _resolve_additional_items(root: Path, candidates: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    canonical_path = dataset_module._canonical_path(root)
    config = _build_dataset_config(root)
    overlay = load_remote_overlay(config)
    wanted_by_catalog: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for candidate in candidates:
        wanted_by_catalog[str(candidate["catalog_key"])][int(candidate["asset_index"])] = candidate

    resolved_items: dict[str, dict[str, Any]] = {}
    with canonical_path.open("rb") as handle:
        for raw_line in handle:
            if len(resolved_items) == len(candidates):
                break
            if not raw_line.strip():
                continue
            raw = json.loads(raw_line)
            if not isinstance(raw, dict):
                continue
            catalog_key = str(raw.get("catalog_key") or "")
            wanted_assets = wanted_by_catalog.get(catalog_key)
            if not wanted_assets:
                continue
            for asset_index, candidate in wanted_assets.items():
                item = dataset_module._manifest_item(
                    root=root,
                    config=config,
                    candidate=candidate,
                    raw_record=raw,
                    overlay=overlay,
                )
                if item is not None:
                    resolved_items[candidate["asset_id"]] = item

    items: list[dict[str, Any]] = []
    for candidate in candidates:
        item = resolved_items.get(candidate["asset_id"])
        if item is None:
            continue
        items.append(item)
        if len(items) == count:
            break
    if len(items) != count:
        raise ValueError(f"unable to resolve {count} additional local/cached items")
    return items


def _prepare_item(root: Path, item: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    result = copy.deepcopy(item)
    path = safe_source(root, result["path"])
    actual = digest(path.read_bytes())
    if actual != result.get("sha256"):
        raise ValueError("source digest mismatch during expanded preparation")
    result["signals"] = image_signals(path)
    result["prompt_signals"] = prompt_signals(result.get("prompt", ""))
    result["embedding_prompt"] = bounded_text(result.get("prompt", ""))
    result["prompt_truncated"] = result["embedding_prompt"] != result.get("prompt", "").strip()
    data = prepared_image(path)
    result["prepared_sha256"] = digest(data)
    result["prepared_path"] = f"inputs/{result['prepared_sha256']}.png"
    result["external_ai_approved"] = False
    return result, data


def build_expanded_manifest(root: Path, source_run_id: str, limit: int = MAX_EXPANDED_IMAGES) -> tuple[dict[str, Any], dict[str, bytes], dict[str, Any]]:
    root_resolved = dataset_module._normalized_root(Path(root))
    bounded_limit = _validated_limit(limit)
    source_dir, source_manifest = _load_source_run(root_resolved, source_run_id)

    preserved_items = copy.deepcopy(source_manifest["items"])
    prepared_inputs: dict[str, bytes] = {}
    for item in preserved_items:
        data = _validated_source_item(root_resolved, source_dir, item)
        prepared_inputs[str(item["prepared_path"])] = data

    additional_count = bounded_limit - len(preserved_items)
    additional_candidates = _select_additional_candidates(
        root_resolved,
        exclude_ids={str(item["id"]) for item in preserved_items},
        count=additional_count,
    )
    additional_base_items = _resolve_additional_items(root_resolved, additional_candidates, additional_count)
    additional_items: list[dict[str, Any]] = []
    for item in additional_base_items:
        prepared_item, data = _prepare_item(root_resolved, item)
        existing = prepared_inputs.get(prepared_item["prepared_path"])
        if existing is not None and existing != data:
            raise ValueError("prepared input path collision with differing bytes")
        prepared_inputs[prepared_item["prepared_path"]] = data
        additional_items.append(prepared_item)

    manifest = {
        "schema_version": str(source_manifest.get("schema_version") or "1"),
        "created_at": now(),
        "selection_notes": list(source_manifest.get("selection_notes") or [])
        + [
            f"Preserved {len(preserved_items)} source-run items exactly from {source_run_id}.",
            f"Added {len(additional_items)} deterministic local/cached items without altering the source run.",
        ],
        "items": preserved_items + additional_items,
    }
    if manifest["items"][: len(source_manifest["items"])] != source_manifest["items"]:
        raise ValueError("preserved source subset drifted during manifest expansion")
    manifest["experiment"] = _plan(len(manifest["items"]))
    manifest["preprocessing"] = source_manifest.get(
        "preprocessing",
        "EXIF transpose; alpha on white; RGB; max side 768; PNG; both arms identical pixels",
    )
    meta = {
        "source_run_id": source_run_id,
        "source_manifest_sha256": digest(json_bytes(source_manifest)),
        "preserved_item_count": len(preserved_items),
        "additional_item_count": len(additional_items),
        "preserved_subset_validated": True,
    }
    return manifest, prepared_inputs, meta


def plan_prepare50(root: Path, source_run_id: str, limit: int = MAX_EXPANDED_IMAGES) -> dict[str, Any]:
    manifest, _prepared_inputs, meta = build_expanded_manifest(root, source_run_id, limit=limit)
    return {
        "status": "dry_run",
        "network_calls": 0,
        "writes": 0,
        "source_run_id": source_run_id,
        "source_manifest_sha256": meta["source_manifest_sha256"],
        "preserved_item_count": meta["preserved_item_count"],
        "additional_item_count": meta["additional_item_count"],
        "target_item_count": len(manifest["items"]),
        "pair_count": len(manifest["items"]) * (len(manifest["items"]) - 1) // 2,
        "preserved_subset_validated": meta["preserved_subset_validated"],
        "preserved_ids": [item["id"] for item in manifest["items"][: meta["preserved_item_count"]]],
        "additional_ids_preview": [item["id"] for item in manifest["items"][meta["preserved_item_count"] : meta["preserved_item_count"] + 5]],
    }


def prepare50(root: Path, source_run_id: str, run_id: str, limit: int = MAX_EXPANDED_IMAGES, *, apply: bool = False) -> dict[str, Any]:
    if not apply:
        return plan_prepare50(root, source_run_id, limit=limit)

    root_resolved = dataset_module._normalized_root(Path(root))
    destination = run_path(root_resolved, run_id)
    if destination.exists():
        raise ValueError("run already exists; use a new run id, never overwrite preparation")

    manifest, prepared_inputs, meta = build_expanded_manifest(root_resolved, source_run_id, limit=limit)
    pairs = [compare_pair(a, b) for a, b in itertools.combinations(manifest["items"], 2)]
    groups = build_groups(manifest["items"], pairs)

    destination.mkdir(parents=True)
    (destination / "inputs").mkdir()
    for relative, data in prepared_inputs.items():
        target = (destination / relative).resolve()
        if not target.is_relative_to((destination / "inputs").resolve()):
            raise ValueError("prepared output escapes destination inputs")
        target.write_bytes(data)

    write_json(destination / "manifest.json", manifest)
    write_json(destination / "annotations.template.json", annotations_template(manifest))
    write_json(
        destination / "offline.json",
        {
            "status": "offline_only",
            "pairs": pairs,
            "groups": groups,
            "embedding_calls": 0,
            "embedding_accuracy": None,
            "reason": "not_executed_no_human_gold",
        },
    )
    (destination / "review.html").write_text(review_html(manifest, {"groups": groups}), encoding="utf-8")
    write_json(
        destination / "prepared.json",
        {
            "complete": True,
            "manifest_sha256": digest(json_bytes(manifest)),
            "source_run_id": source_run_id,
            "source_manifest_sha256": meta["source_manifest_sha256"],
            "preserved_item_count": meta["preserved_item_count"],
            "preserved_subset_validated": True,
            "pair_count": len(pairs),
            "relation_labels": sorted(RELATION_LABELS),
            "at": now(),
        },
    )
    return {
        "status": "prepared_local_only",
        "run_id": run_id,
        "items": len(manifest["items"]),
        "pairs": len(pairs),
        "groups": len(groups),
        "embedding_calls": 0,
        "network_calls": 0,
        "preserved_item_count": meta["preserved_item_count"],
        "additional_item_count": meta["additional_item_count"],
        "preserved_subset_validated": True,
        "review_path": str(destination / "review.html"),
    }
