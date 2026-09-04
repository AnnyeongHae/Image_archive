"""Bounded CASE-only incremental preparation; no providers or public writes.

Raw-record selection is capped before exact deduplication. Existing keepers are
frozen, including aliases with different prompts. Semantic group membership is
never inferred here: the existing human decisions have not been imported yet.
"""
from __future__ import annotations

import copy
import io
import json
import re
from pathlib import Path
from typing import Any

from PIL import Image

from . import dataset
from .comparison import request_key
from .experiment import digest, json_bytes, now, read_json, run_lock, run_path, safe_source, unit_prefix, write_json
from .expansion import _prepare_item, _resolve_additional_items, _validated_source_item
from .prompt_priority import priority_sort_key, rank_prompt
from .similarity import _decoded_rgba_image, _pixel_sha256


MAX_RAW_RECORDS = 300
REFERENCE_COUNT = 200
PUBLIC_CASE_COUNT = 529
DEFAULT_REFERENCE_RUN = "2026-09-03-voyage-similarity-200-v1"
MANIFEST_SCHEMA = "image-incremental-manifest-1"
BINDINGS_SCHEMA = "image-incremental-source-bindings-1"
PUBLIC_CATALOG_PATH = "deploy/cloudflare-public/public/catalog-data.js"
REFERENCE_SPEC_PATH = "group-workflow-v1/image-group-workflow.spec.json"


def _limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= MAX_RAW_RECORDS:
        raise ValueError("max_records must be an integer between 1 and 300")
    return value


def _file_binding(root: Path, path: Path) -> dict[str, str]:
    path = path.resolve()
    if not dataset._safe_input_path(root, path) or not path.is_file():
        raise ValueError("binding source must be a nonsecret file within the archive")
    return {"path": path.relative_to(root).as_posix(), "sha256": digest(path.read_bytes())}


def _validate_files(root: Path, bindings: dict[str, Any]) -> None:
    if bindings.get("schema_version") != BINDINGS_SCHEMA or not isinstance(bindings.get("files"), list):
        raise ValueError("incremental source bindings are invalid")
    seen: set[str] = set()
    for row in bindings["files"]:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str) or row["path"] in seen:
            raise ValueError("incremental source bindings contain duplicate or invalid paths")
        seen.add(row["path"])
        relative = Path(row["path"])
        if relative.is_absolute():
            raise ValueError("source binding path must be relative")
        actual = _file_binding(root, root / relative)
        if actual != row:
            raise ValueError("incremental source drift: " + row["path"])
    reference = run_path(root, bindings["reference_run_id"])
    imports = sorted(path.relative_to(root).as_posix() for path in
                     (reference / "group-workflow-v1/decision-imports").glob("*/receipt.json"))
    if imports != bindings.get("human_group_imports", []):
        raise ValueError("reference human decisions changed; prepare a new incremental snapshot")


def _public_case_ids(root: Path) -> set[str]:
    text = (root / PUBLIC_CATALOG_PATH).read_text(encoding="utf-8")
    matches = list(re.finditer(r"(?:^|\n)window\.DETAILPAGE_CATALOG_CASES\s*=\s*", text))
    if len(matches) != 1:
        raise ValueError("expected one public CASE catalog assignment")
    rows, end = json.JSONDecoder().raw_decode(text, matches[0].end())
    if text[end:].lstrip()[:1] != ";" or not isinstance(rows, list) or len(rows) != PUBLIC_CASE_COUNT:
        raise ValueError("public CASE scope must contain the exact frozen 529-record catalog")
    ids: set[str] = set()
    for row in rows:
        case_id = row.get("id") if isinstance(row, dict) else None
        if type(case_id) is not int or case_id < 1:
            raise ValueError("public CASE id must be a positive integer")
        style_id = f"CASE-{case_id:03d}"
        if style_id in ids:
            raise ValueError("public CASE ids must be unique")
        ids.add(style_id)
    return ids


def select_case_candidates(pool: list[dict[str, Any]], public_ids: set[str],
                           reference_items: list[dict[str, Any]], max_records: int) -> tuple[list[dict], dict]:
    """One primary asset per raw public record, deterministic CASE numeric order."""
    maximum = _limit(max_records)
    eligible = [row for row in pool if row.get("lane") == "legacy" and row.get("style_id") in public_ids]
    if {row["style_id"] for row in eligible} != public_ids:
        raise ValueError("duplicate index does not cover the exact public CASE allowlist")
    eligible.sort(key=lambda row: (int(row["style_id"].split("-", 1)[1]), str(row["catalog_key"]),
                                   int(row["asset_index"]), str(row["asset_id"])))
    reference_styles = {row.get("style_id") for row in reference_items if row.get("lane") == "legacy"}
    reference_catalogs = {row.get("catalog_key") for row in reference_items}
    reference_assets = {row["id"] for row in reference_items}
    selected: list[dict[str, Any]] = []
    seen_styles: set[str] = set()
    seen_assets: set[str] = set()
    unsampled = 0
    for row in eligible:
        if row["style_id"] in seen_styles:
            continue
        seen_styles.add(row["style_id"])
        if (row["style_id"] in reference_styles or row["catalog_key"] in reference_catalogs
                or row["asset_id"] in reference_assets):
            continue
        unsampled += 1
        if len(selected) < maximum and not dataset._append_candidate(selected, seen_assets, row):
            raise ValueError("selected CASE record has no original file identity")
    return selected, {"public_case_records": len(public_ids), "already_sampled_case_records": len(public_ids) - unsampled,
                      "unsampled_case_records": unsampled, "selected_raw_records": len(selected),
                      "remaining_unsampled_case_records": unsampled - len(selected),
                      "selection_order": "CASE_numeric_then_catalog_key_then_primary_asset_index",
                      "selection_cap_applied_before_dedup": True}


def _load_reference(root: Path, reference_run_id: str) -> tuple[dict, dict, list[dict]]:
    source = run_path(root, reference_run_id)
    manifest = read_json(source / "manifest.json")
    prepared = read_json(source / "prepared.json")
    spec = read_json(source / REFERENCE_SPEC_PATH)
    items = manifest.get("items", [])
    if not isinstance(items, list) or len(items) != REFERENCE_COUNT or len({row["id"] for row in items}) != len(items):
        raise ValueError("incremental reference must contain exactly 200 unique prepared records")
    if prepared.get("complete") is not True or prepared.get("manifest_sha256") != digest(json_bytes(manifest)):
        raise ValueError("reference preparation receipt does not match manifest")
    unhashed_spec = copy.deepcopy(spec)
    unhashed_spec.pop("spec_sha256", None)
    if (spec.get("schema_version") != "image-group-workflow-spec-1" or spec.get("run_id") != reference_run_id
            or spec.get("spec_sha256") != digest(json_bytes(unhashed_spec))
            or spec.get("source_manifest_sha256") != digest(json_bytes(manifest))):
        raise ValueError("reference workflow spec binding mismatch")
    imports = list((source / "group-workflow-v1/decision-imports").glob("*/receipt.json"))
    if imports:
        raise ValueError("human decisions now exist; explicit verified representative snapshot is required")
    files = [_file_binding(root, source / relative) for relative in (
        "manifest.json", "prepared.json", REFERENCE_SPEC_PATH, "comparison-v1/vectors.json", "comparison-v1/budget.json")]
    for row in items:
        _validated_source_item(root, source, row)
        original = safe_source(root, row["path"])
        actual_pixel = _pixel_sha256(_decoded_rgba_image(original))
        if row.get("signals", {}).get("pixel_sha256") != actual_pixel:
            raise ValueError("reference original decoded pixel evidence changed")
        files.extend([_file_binding(root, original), _file_binding(root, source / row["prepared_path"])])
    return manifest, spec, files


def _identity(item: dict[str, Any]) -> dict[str, str]:
    signals = item.get("signals", {})
    hashes = {"exact_file": item.get("sha256"), "exact_pixels": signals.get("pixel_sha256")}
    if signals.get("sha256") not in {None, hashes["exact_file"]}:
        raise ValueError("original file identity evidence conflicts")
    if any(not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha) for sha in hashes.values()):
        raise ValueError("full original file and decoded pixel SHA-256 are required")
    return hashes


def route_exact_aliases(incoming: list[dict], reference_items: list[dict], stage1: dict) -> tuple[list[str], list[dict]]:
    """Union exact hashes; prompt quality ranks only new-component keepers.

    Prompt/pHash never creates an identity edge. All reference keepers stay
    frozen, even when a new alias has a better structured prompt.
    """
    ref_ids = {row["id"] for row in reference_items}
    active_ids = set(stage1["active_ids"])
    keeper = {item_id: item_id for item_id in active_ids}
    for row in stage1["archived"]:
        if row["id"] in keeper or row["representative_id"] not in active_ids:
            raise ValueError("frozen stage1 keeper map is inconsistent")
        keeper[row["id"]] = row["representative_id"]
    if set(keeper) != ref_ids or len(ref_ids) != len(reference_items):
        raise ValueError("frozen stage1 retention must partition all reference aliases")
    incoming_ids = [row["id"] for row in incoming]
    if len(set(incoming_ids)) != len(incoming_ids) or set(incoming_ids) & ref_ids:
        raise ValueError("incoming ids must be unique and unsampled")
    all_items = reference_items + incoming
    by_id = {row["id"]: row for row in all_items}
    parent = {item_id: item_id for item_id in by_id}
    identities = {row["id"]: _identity(row) for row in all_items}
    first_by_hash: dict[tuple[str, str], str] = {}
    hash_members: dict[tuple[str, str], list[str]] = {}
    edges: list[dict] = []

    def find(item_id: str) -> str:
        while parent[item_id] != item_id:
            parent[item_id] = parent[parent[item_id]]
            item_id = parent[item_id]
        return item_id

    for item in all_items:
        item_id = item["id"]
        for kind, sha in identities[item_id].items():
            key = (kind, sha)
            hash_members.setdefault(key, []).append(item_id)
            previous = first_by_hash.get(key)
            if previous is None:
                first_by_hash[key] = item_id
            else:
                parent[find(item_id)] = find(previous)
                edges.append({"left_id": previous, "right_id": item_id, "kind": kind, "sha256": sha})
    components: dict[str, list[str]] = {}
    for item in all_items:
        components.setdefault(find(item["id"]), []).append(item["id"])
    arrival = {row["id"]: index for index, row in enumerate(incoming)}
    routes_by_id: dict[str, dict] = {}
    novel_keepers: set[str] = set()
    for members in components.values():
        frozen_keepers = {keeper[item_id] for item_id in members if item_id in ref_ids}
        new_members = [item_id for item_id in members if item_id not in ref_ids]
        if len(frozen_keepers) > 1:
            raise ValueError("exact identity bridges multiple frozen keepers; human resolution required")
        if not new_members:
            continue
        if frozen_keepers:
            representative_id, scope = next(iter(frozen_keepers)), "existing"
            selection_policy = "frozen_existing_representative"
        else:
            representative_id = min(new_members, key=lambda item_id: (
                *priority_sort_key(rank_prompt(by_id[item_id].get("prompt", ""))), arrival[item_id], item_id))
            scope = "incoming"
            selection_policy = "new_component_prompt_quality_then_raw_CASE_order"
            novel_keepers.add(representative_id)
        member_set = set(members)
        component_edges = [edge for edge in edges if edge["left_id"] in member_set and edge["right_id"] in member_set]
        for item_id in new_members:
            if scope == "incoming" and item_id == representative_id:
                continue
            matches = [(kind, sha, other_id) for kind, sha in identities[item_id].items()
                       for other_id in hash_members[(kind, sha)] if other_id != item_id]
            routes_by_id[item_id] = {"id": item_id, "style_id": by_id[item_id]["style_id"],
                "representative_id": representative_id, "reference_scope": scope,
                "match_kinds": sorted({kind for kind, _sha, _other_id in matches}),
                "matched_alias_ids": sorted({other_id for _kind, _sha, other_id in matches}),
                "evidence": [{"kind": kind, "sha256": sha} for kind, sha in sorted({(kind, sha) for kind, sha, _other_id in matches})],
                "exact_component_ids": members, "component_identity_edges": component_edges,
                "keeper_selection_policy": selection_policy,
                "representative_prompt_priority": rank_prompt(by_id[representative_id].get("prompt", "")),
                "action": "attach_provenance_alias_only", "physical_deletions": 0,
                "replacement_of_existing_keeper": False, "prompt_preserved_in_manifest": True}
    return ([item_id for item_id in incoming_ids if item_id in novel_keepers],
            [routes_by_id[item_id] for item_id in incoming_ids if item_id in routes_by_id])


def _cache_request(root: Path, reference: Path, request: dict, ledger: dict) -> dict | None:
    path = reference / "comparison-v1/vector-cache" / (request["key"] + ".json")
    if not path.is_file():
        return None
    payload = read_json(path)
    vector = payload.get("vector")
    completed = {str(row.get("key", "")).split(":", 1)[0] for row in ledger.get("attempts", [])
                 if row.get("status") == "completed"}
    if (payload.get("key") != request["key"] or payload.get("model") != request["model"]
            or not isinstance(vector, list) or len(vector) != request["dimensions"]
            or payload.get("vector_sha256") != digest(json_bytes(vector)) or request["key"] not in completed):
        raise ValueError("reusable reference vector cache identity or completion mismatch")
    unit_prefix(vector, request["dimensions"])
    return _file_binding(root, path)


def build_incremental_payloads(root: Path, reference_run_id: str, run_id: str, *, max_records: int = 300,
                               progress=None) -> tuple[dict, dict, dict, dict[str, bytes]]:
    root = dataset._normalized_root(Path(root))
    maximum = _limit(max_records)
    reference = run_path(root, reference_run_id)
    if run_path(root, run_id) == reference:
        raise ValueError("incremental run must differ from reference")
    files = [_file_binding(root, path) for path in (root / PUBLIC_CATALOG_PATH, dataset._canonical_path(root),
                                                    dataset._duplicate_index_path(root), dataset._remote_overlay_path(root))]
    parent, spec, reference_files = _load_reference(root, reference_run_id)
    files.extend(reference_files)
    if progress:
        progress({"stage": "reference_validated", "records": len(parent["items"]), "frozen_keepers": len(spec["stage1"]["active_ids"])})
    connection = dataset._connect_index(dataset._duplicate_index_path(root))
    try:
        candidates, inventory = select_case_candidates(dataset._all_asset_candidates(connection), _public_case_ids(root), parent["items"], maximum)
    finally:
        connection.close()
    if not candidates:
        raise ValueError("no unsampled public CASE records remain")
    raw_items = _resolve_additional_items(root, candidates, len(candidates))
    prepared_items: list[dict] = []
    inputs: dict[str, bytes] = {}
    for index, raw in enumerate(raw_items, 1):
        item, blob = _prepare_item(root, raw)
        if item["prepared_path"] in inputs and inputs[item["prepared_path"]] != blob:
            raise ValueError("prepared input content hash collision")
        inputs[item["prepared_path"]] = blob
        prepared_items.append(item)
        original_binding = _file_binding(root, safe_source(root, item["path"]))
        if original_binding["sha256"] != item["sha256"]:
            raise ValueError("incoming source changed during preparation")
        files.append(original_binding)
        if progress and index % 25 == 0:
            progress({"stage": "incoming_originals_prepared", "records": index})
    embedding_ids, aliases = route_exact_aliases(prepared_items, parent["items"], spec["stage1"])
    ledger = read_json(reference / "comparison-v1/budget.json")
    requests: list[dict] = []
    for item in prepared_items:
        if item["id"] not in embedding_ids:
            continue
        with Image.open(io.BytesIO(inputs[item["prepared_path"]])) as image:
            pixel_count = image.width * image.height
        pixels = max(50_000, pixel_count)
        request = {"provider": "voyage", "model": "voyage-multimodal-3.5", "dimensions": 1024,
            "image_sha256": item["prepared_sha256"], "text": "", "task": "RETRIEVAL_DOCUMENT",
            "id": item["id"], "arm": "voyage_image", "kind": "document", "pixels": pixels,
            "prepared_pixel_count": pixel_count, "reserved_usd": pixels * .60 / 1_000_000_000 + 256 * .12 / 1_000_000}
        request["key"] = request_key(request)
        cache = _cache_request(root, reference, request, ledger)
        request["cache_status"] = "validated_reusable" if cache else "new_request_required"
        if cache:
            files.append(cache)
            request["cache_path"] = cache["path"]
            request["cache_sha256"] = cache["sha256"]
        requests.append(request)
    unique_files: dict[str, dict] = {}
    for row in files:
        if row["path"] in unique_files and unique_files[row["path"]] != row:
            raise ValueError("source changed during incremental preparation")
        unique_files[row["path"]] = row
    bindings = {"schema_version": BINDINGS_SCHEMA, "reference_run_id": reference_run_id,
        "reference_spec_sha256": spec["spec_sha256"], "reference_ids": [row["id"] for row in parent["items"]],
        "frozen_representative_ids": spec["stage1"]["active_ids"], "human_group_imports": [],
        "human_decisions_status": "pending_not_imported", "files": [unique_files[key] for key in sorted(unique_files)]}
    _validate_files(root, bindings)
    manifest = {"schema_version": MANIFEST_SCHEMA, "created_at": now(), "run_id": run_id,
        "reference_run_id": reference_run_id, "reference_spec_sha256": spec["spec_sha256"],
        "source_bindings_sha256": digest(json_bytes(bindings)), "selection_profile": {**inventory, "max_raw_records": maximum,
            "lane": "legacy", "public_catalog": PUBLIC_CATALOG_PATH},
        "items": prepared_items, "embedding_item_ids": embedding_ids, "alias_routes": aliases,
        "preprocessing": parent.get("preprocessing", "EXIF transpose; alpha on white; RGB; max side 768; PNG"),
        "evaluation_arms": ["voyage_image"], "metadata_generation": "not_executed",
        "semantic_group_matching_status": "pending_new_vectors_and_imported_human_decisions",
        "existing_keeper_policy": "frozen_all_reference_aliases_no_prompt_priority_replacement",
        "incoming_keeper_policy": "exact_hash_components_then_prompt_quality_then_raw_CASE_order",
        "source_mutations": False, "canonical_writes": 0, "public_release_approval": False}
    unique_requests = {row["key"]: row for row in requests}
    pending = [row for row in unique_requests.values() if row["cache_status"] == "new_request_required"]
    plan = {"schema_version": "image-incremental-plan-1", "run_id": run_id,
        "manifest_sha256": digest(json_bytes(manifest)), "source_bindings_sha256": digest(json_bytes(bindings)),
        **inventory, "novel_representative_records": len(embedding_ids),
        "existing_alias_records": sum(row["reference_scope"] == "existing" for row in aliases),
        "incoming_duplicate_records": sum(row["reference_scope"] == "incoming" for row in aliases),
        "embedding_requests": requests, "unique_embedding_request_count": len(unique_requests),
        "new_image_request_count": len(pending), "reusable_image_request_count": len(unique_requests) - len(pending),
        "incremental_reserved_usd": sum(row["reserved_usd"] for row in pending),
        "cost_basis": "existing_repository_voyage_paid_reservation_formula_not_invoice_or_free_credit_claim",
        "network_calls": 0, "embedding_calls": 0, "qdrant_writes": 0, "r2_writes": 0,
        "canonical_writes": 0, "front_writes": 0, "physical_deletions": 0,
        "human_decisions_status": "pending_not_imported", "semantic_group_auto_append": False}
    return manifest, bindings, plan, inputs


def validate_incremental_prepared(root: Path, run_id: str) -> tuple[dict, dict]:
    """Revalidate immutable evidence before a separate provider execution step."""
    root = Path(root).resolve()
    source = run_path(root, run_id)
    manifest = read_json(source / "manifest.json")
    bindings = read_json(source / "source-bindings.json")
    receipt = read_json(source / "prepared.json")
    if (manifest.get("schema_version") != MANIFEST_SCHEMA or manifest.get("run_id") != run_id
            or receipt.get("complete") is not True or receipt.get("manifest_sha256") != digest(json_bytes(manifest))
            or manifest.get("source_bindings_sha256") != digest(json_bytes(bindings))
            or receipt.get("source_bindings_sha256") != digest(json_bytes(bindings))):
        raise ValueError("incremental preparation receipt binding mismatch")
    if (manifest.get("reference_run_id") != bindings.get("reference_run_id")
            or manifest.get("reference_spec_sha256") != bindings.get("reference_spec_sha256")):
        raise ValueError("incremental reference binding mismatch")
    items = manifest.get("items", [])
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_RAW_RECORDS:
        raise ValueError("incremental preparation exceeds raw-record cap")
    _validate_files(root, bindings)
    parent = read_json(run_path(root, bindings["reference_run_id"]) / "manifest.json")
    spec = read_json(run_path(root, bindings["reference_run_id"]) / REFERENCE_SPEC_PATH)
    if (spec.get("spec_sha256") != bindings.get("reference_spec_sha256")
            or [row["id"] for row in parent["items"]] != bindings.get("reference_ids")
            or spec["stage1"]["active_ids"] != bindings.get("frozen_representative_ids")):
        raise ValueError("incremental frozen representative binding mismatch")
    public_ids = _public_case_ids(root)
    connection = dataset._connect_index(dataset._duplicate_index_path(root))
    try:
        candidates, _inventory = select_case_candidates(dataset._all_asset_candidates(connection), public_ids,
            parent["items"], manifest.get("selection_profile", {}).get("max_raw_records"))
    finally:
        connection.close()
    if [row["asset_id"] for row in candidates] != [row.get("id") for row in items]:
        raise ValueError("incremental selected raw CASE records changed")
    for item in items:
        if item.get("lane") != "legacy" or item.get("style_id") not in public_ids:
            raise ValueError("incremental item escapes the public legacy CASE scope")
        _validated_source_item(root, source, item)
    embedding_ids, aliases = route_exact_aliases(items, parent["items"], spec["stage1"])
    if embedding_ids != manifest.get("embedding_item_ids") or aliases != manifest.get("alias_routes"):
        raise ValueError("incremental exact-alias routing changed")
    return manifest, bindings


def prepare_incremental_batch(root: Path, reference_run_id: str, run_id: str, *, max_records: int = 300,
                              apply: bool = False, progress=None) -> dict:
    root = Path(root).resolve()
    destination = run_path(root, run_id)
    if destination.exists():
        raise FileExistsError("incremental destination exists; never overwrite a prepared run")
    manifest, bindings, plan, inputs = build_incremental_payloads(root, reference_run_id, run_id,
        max_records=max_records, progress=progress)
    result = {**{key: value for key, value in plan.items() if key != "embedding_requests"},
        "status": "dry_run", "run_id": run_id, "manifest_path": str(destination / "manifest.json"), "writes": 0}
    if not apply:
        return result
    _validate_files(root, bindings)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with run_lock(destination.parent):
        if destination.exists():
            raise FileExistsError("incremental destination exists; never overwrite a prepared run")
        destination.mkdir()
        (destination / "inputs").mkdir()
        for relative, blob in inputs.items():
            target = (destination / relative).resolve()
            if not target.is_relative_to((destination / "inputs").resolve()):
                raise ValueError("incremental prepared path escapes inputs")
            target.write_bytes(blob)
        write_json(destination / "manifest.json", manifest)
        write_json(destination / "source-bindings.json", bindings)
        write_json(destination / "incremental-plan.json", plan)
        _validate_files(root, bindings)
        write_json(destination / "prepared.json", {"complete": True, "at": now(),
            "manifest_sha256": digest(json_bytes(manifest)), "source_bindings_sha256": digest(json_bytes(bindings)),
            "reference_run_id": reference_run_id, "raw_record_count": len(manifest["items"]),
            "novel_representative_count": len(manifest["embedding_item_ids"]), "network_calls": 0})
    return {**result, "status": "prepared_local_only", "writes": len(inputs) + 4}


__all__ = ["prepare_incremental_batch", "validate_incremental_prepared", "build_incremental_payloads",
           "route_exact_aliases", "select_case_candidates"]
