"""Read the last committed approval and prepare immutable, offline work outboxes.

The mutable administrator draft and the SQLite file's physical bytes are not
approval evidence. No provider, canonical writer, or incremental importer is
called here. A handoff is a versioned consumer contract, not executed work.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "image-admin-approval-handoff-1"
RELATIVE_ROOT = "data/private-research/image-rag-admin/handoffs"
FILES = ("snapshot.json", "luna-pending.json", "text-embedding-pending.json")
MODEL = "voyage-multimodal-3.5"
DIMENSIONS = 1024


class HandoffError(ValueError):
    """Fail closed without changing the committed administrator approval."""


def _bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise HandoffError("Unsafe handoff evidence path")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or ":" in relative:
        raise HandoffError("Unsafe handoff evidence path")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise HandoffError("Handoff evidence escaped archive root")
    return resolved


def _read(path: Path) -> dict:
    result = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(result, dict):
        raise HandoffError("Expected a JSON object")
    return result


def _committed(db_path: Path, run_id: str) -> dict:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", run_id):
        raise HandoffError("Invalid run ID")
    path = Path(db_path).resolve()
    if not path.is_file():
        raise HandoffError("Administrator database does not exist")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            run = db.execute("SELECT * FROM image_admin_runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise HandoffError("Administrator run does not exist")
            if not run["last_commit_id"]:
                return {"run_id": run_id, "commit": None}
            row = db.execute("SELECT * FROM image_admin_commits WHERE run_id=? AND commit_id=?",
                             (run_id, run["last_commit_id"])).fetchone()
            if row is None:
                raise HandoffError("Referenced administrator commit is missing")
            result = {"run_id": run_id, "spec_sha256": run["spec_sha256"],
                      "spec_content_sha256": run["spec_content_sha256"],
                      "commit": {"id": row["commit_id"], "revision": row["revision"], "kind": row["kind"],
                                 "committed_at": row["committed_at"], "decisions_sha256": row["decisions_sha256"]},
                      "normalized": json.loads(row["normalized_json"]),
                      "front": json.loads(row["front_json"]), "groups": json.loads(row["groups_json"])}
            db.execute("ROLLBACK")
    except sqlite3.Error as exc:
        raise HandoffError("Could not read a consistent administrator commit") from exc
    if _hash(result["normalized"]) != result["commit"]["decisions_sha256"]:
        raise HandoffError("Committed decisions hash mismatch")
    expected = _hash({"run_id": run_id, "revision": result["commit"]["revision"],
                      "decisions_sha256": result["commit"]["decisions_sha256"]})
    if expected != result["commit"]["id"]:
        raise HandoffError("Committed identity mismatch")
    return result


def _require_latest(db_path: Path, run_id: str, commit_id: str) -> None:
    latest = _committed(db_path, run_id)
    if not latest["commit"] or latest["commit"]["id"] != commit_id:
        raise HandoffError("Latest committed approval changed; prepare again")


def _load_spec(root: Path, run_id: str) -> dict:
    from .incremental_workflow import load_frozen_workflow
    return load_frozen_workflow(root, run_id)


def _validate_commit(spec: dict, data: dict) -> dict:
    from .group_workflow import validate_group_workflow_decisions
    if (spec["run_id"] != data["run_id"] or spec["spec_sha256"] != data["spec_sha256"]
            or _hash(spec) != data["spec_content_sha256"]):
        raise HandoffError("Administrator and frozen spec binding mismatch")
    normalized = validate_group_workflow_decisions(spec, data["normalized"])
    if _hash(normalized) != _hash(data["normalized"]):
        raise HandoffError("Committed approval does not match authoritative validation")
    if (normalized["private_front_export_status"] != "ready"
            or normalized["stage2_duplicate_gate_status"] != "complete"
            or normalized["stage3_similarity_gate_status"] != "complete"
            or normalized["stage4_gate_status"] != "unlocked"
            or normalized["front_review_complete"] is not True):
        raise HandoffError("Committed approval gates are not complete")
    front = {"run_id": data["run_id"], "spec_sha256": spec["spec_sha256"],
             "decisions_schema_version": "image-group-workflow-decisions-3",
             "front_approval_policy": "default_retained_images_after_review_v1",
             "status": "ready", "front_review_complete": True,
             "stage2_duplicate_gate_status": "complete", "stage3_similarity_gate_status": "complete",
             "stage4_gate_status": "unlocked", "items": normalized["private_front_export_items"],
             "release_eligible": False, "public_rights_approved": False}
    groups = {"run_id": data["run_id"], "spec_sha256": spec["spec_sha256"],
              "groups": normalized["approved_similarity_groups"]}
    if data["front"] != front or data["groups"] != groups:
        raise HandoffError("Committed front or group projection mismatch")
    return normalized


def _sources(root: Path, spec: dict) -> tuple[dict[str, dict], list[dict]]:
    directory = f"data/private-research/image-rag-canary/runs/{spec['run_id']}/group-workflow-v1"
    binding = _read(_safe(root, directory + "/source-bindings.json"))
    evidence = {}
    for relative in (directory + "/image-group-workflow.spec.json", directory + "/source-bindings.json",
                     directory + "/build-receipt.json", directory + "/submitted-baseline.raw.json"):
        evidence[relative] = {"path": relative, "sha256": _file_hash(_safe(root, relative))}
    manifests = {}
    for row in binding["files"]:
        path = _safe(root, row["path"])
        if _file_hash(path) != row["sha256"]:
            raise HandoffError("Frozen source evidence changed")
        evidence[row["path"]] = {"path": row["path"], "sha256": row["sha256"]}
        if path.name != "manifest.json":
            continue
        document = _read(path)
        if document.get("schema_version") not in {"1", "image-incremental-manifest-1", "image-v2-intake-manifest-1"}:
            continue
        for item in document.get("items", []):
            ident = item.get("id")
            if ident in manifests:
                raise HandoffError("Ambiguous item in pinned source manifests")
            manifests[ident] = {**item, "manifest_path": row["path"], "manifest_sha256": row["sha256"]}
    result = {}
    for item in spec["items"]:
        source = manifests.get(item["id"])
        if (source is None or source.get("style_id") != item["style_id"]
                or source.get("sha256") != item["source_sha256"]
                or source.get("prepared_sha256") != item["prepared_sha256"]):
            raise HandoffError("Pinned prompt/image identity does not match frozen review")
        # Frozen previews are copied into this run, not inferred from a source URL.
        relative = item["prepared_path"]
        if not re.fullmatch(r"\.\./inputs/[a-f0-9]{64}\.png", relative):
            raise HandoffError("Unsafe frozen preview")
        preview = f"data/private-research/image-rag-canary/runs/{spec['run_id']}/inputs/{Path(relative).name}"
        if _file_hash(_safe(root, preview)) != item["prepared_sha256"]:
            raise HandoffError("Frozen preview hash mismatch")
        evidence[preview] = {"path": preview, "sha256": item["prepared_sha256"]}
        result[item["id"]] = {**source, "frozen_preview_path": preview}
    return result, sorted(evidence.values(), key=lambda row: row["path"])


def _rights(root: Path, spec: dict) -> dict:
    from .rights import build_rights_catalog
    result = build_rights_catalog(root, spec)
    if set(result) != {item["id"] for item in spec["items"]}:
        raise HandoffError("Rights catalog must cover every frozen item")
    if any(row.get("release_eligible") is not False for row in result.values()):
        raise HandoffError("Handoff cannot grant public release rights")
    return result


def _vector_refs(root: Path, spec: dict) -> tuple[dict, list[dict]]:
    from .comparison import request_key
    from .shared_vector_cache import RELATIVE_ROOT as CACHE_ROOT, lookup_shared_vectors
    requests = [{"provider": "voyage", "model": MODEL, "dimensions": DIMENSIONS,
                 "image_sha256": item["prepared_sha256"], "text": "", "task": "RETRIEVAL_DOCUMENT"}
                for item in spec["items"]]
    receipts = lookup_shared_vectors(root, requests)
    result, evidence = {}, {}
    for item, request in zip(spec["items"], requests):
        key = request_key(request)
        receipt = receipts[key]
        reference = {"status": "missing", "request_key": key, "request_identity": request}
        if receipt is not None:
            if (receipt["key"] != key or receipt["model"] != MODEL or receipt["provider"] != "voyage"
                    or len(receipt["vector"]) != DIMENSIONS):
                raise HandoffError("Existing vector identity mismatch")
            revision = receipt["shared_revision_id"]
            if not re.fullmatch(r"[a-f0-9]{64}", revision):
                raise HandoffError("Invalid shared cache revision")
            paths = [receipt["shared_object_path"], f"{CACHE_ROOT}/revisions/{revision}/manifest.json",
                     f"{CACHE_ROOT}/revisions/{revision}/receipt.json"]
            for relative in paths:
                evidence[relative] = {"path": relative, "sha256": _file_hash(_safe(root, relative))}
            reference.update({"status": "cached", "vector_sha256": receipt["vector_sha256"],
                              "shared_revision_id": revision, "object_path": receipt["shared_object_path"],
                              "object_sha256": evidence[receipt["shared_object_path"]]["sha256"],
                              "provenance": receipt["shared_provenance"]})
        result[item["id"]] = reference
    return result, sorted(evidence.values(), key=lambda row: row["path"])


def _metadata_contract(root: Path) -> dict:
    # These are project-level contracts, one directory above the archive root.
    paths = {"output_schema": "00_CORE/schemas/image_archive_luna_metadata.schema.json",
             "instructions": "00_CORE/templates/image_archive_luna_metadata.instructions.md"}
    return {key: {"workspace_relative_path": relative, "sha256": _file_hash(root.parent / relative)}
            for key, relative in paths.items()}


def _archive(normalized: dict, spec: dict) -> list[dict]:
    rows = copy.deepcopy(normalized["stage2_overlay"].get("archived", []))
    # The authoritative overlay includes stage1 aliases. Reject conflicting
    # projections instead of silently choosing a keeper.
    mapping = {}
    for row in rows:
        if row["id"] in mapping and mapping[row["id"]] != row:
            raise HandoffError("Conflicting archived alias")
        mapping[row["id"]] = row
    retained = set(normalized["stage2_overlay"]["active_ids"])
    all_ids = {item["id"] for item in spec["items"]}
    if retained & set(mapping) or retained | set(mapping) != all_ids:
        raise HandoffError("Retention and archived aliases must partition all source items")
    for ident, row in mapping.items():
        keeper, visited = row["representative_id"], {ident}
        while keeper in mapping:
            if keeper in visited:
                raise HandoffError("Archived alias cycle")
            visited.add(keeper)
            keeper = mapping[keeper]["representative_id"]
        if keeper not in retained:
            raise HandoffError("Archived alias has no retained keeper")
        row["final_representative_id"] = keeper
    return [mapping[key] for key in sorted(mapping)]


def _build(root: Path, spec: dict, data: dict) -> tuple[dict, dict, dict, list[dict]]:
    normalized = _validate_commit(spec, data)
    sources, evidence = _sources(root, spec)
    rights = _rights(root, spec)
    vectors, vector_evidence = _vector_refs(root, spec)
    evidence.extend(vector_evidence)
    contract = _metadata_contract(root)
    retained = normalized["stage2_overlay"]["active_ids"]
    approved = [item["id"] for item in normalized["private_front_export_items"]]
    choices = {item["id"]: item for item in normalized["image_approvals"]}
    front_items = {item["id"]: item for item in normalized["private_front_export_items"]}
    groups = normalized["approved_similarity_groups"]
    archived = _archive(normalized, spec)
    alias = {row["id"]: row for row in archived}
    items = []
    for item in spec["items"]:
        ident, source = item["id"], sources[item["id"]]
        items.append({"id": ident, "style_id": item["style_id"], "title": source.get("title", ""),
            "retention_status": "retained" if ident in choices else "archived_alias",
            "approved": choices.get(ident, {}).get("approved", False),
            "human_memo": choices.get(ident, {}).get("memo_text", ""),
            "human_tags": [], "human_tags_provenance": "no_explicit_tag_contract_in_v3",
            "legacy_tags_texts": copy.deepcopy(front_items.get(ident, {}).get("tags_texts", [])),
            "legacy_tags_texts_provenance": "committed_private_front.tags_texts_may_project_personal_memo_not_categorical_tags",
            "prompt": source.get("prompt", ""), "prompt_truncated": source.get("prompt_truncated", False),
            "embedding_prompt": source.get("embedding_prompt", ""), "prompt_signals": source.get("prompt_signals", {}),
            "image": {"path": source["frozen_preview_path"], "prepared_sha256": item["prepared_sha256"],
                      "original_sha256": item["source_sha256"], "signals": source.get("signals", {})},
            "source": {key: source.get(key) for key in ("lane", "catalog_key", "record_id", "asset_index",
                      "source_name", "source_url_sha256", "manifest_path", "manifest_sha256")},
            "priority": item.get("priority", {}), "rights_display": rights[ident],
            "archived_alias": alias.get(ident), "image_vector": vectors[ident],
            "external_ai_approval_inherited": False})
    snapshot = {"schema_version": SCHEMA, "run_id": spec["run_id"], "source_commit": data["commit"],
        "spec_sha256": spec["spec_sha256"], "spec_content_sha256": data["spec_content_sha256"],
        "committed_front_sha256": _hash(data["front"]), "committed_groups_sha256": _hash(data["groups"]),
        "normalized_decisions": normalized, "items": items, "retained_ids": retained, "approved_ids": approved,
        "archived_aliases": archived, "alias_lineage": spec["stage1"].get("alias_lineage", []),
        "groups": groups, "image_approvals": normalized["image_approvals"], "rights_catalog_sha256": _hash(rights),
        "image_vector_policy": {"provider": "voyage", "model": MODEL, "dimensions": DIMENSIONS, "input": "image_only"},
        "downstream_status": {"incremental_baseline": "contract_ready_consumer_not_connected",
                              "luna": "pending_not_executed", "text_embedding": "pending_not_executed"},
        "provider_calls": 0, "release_eligible": False, "public_rights_approved": False}
    snapshot_sha = hashlib.sha256(_bytes(snapshot)).hexdigest()
    common = {"run_id": spec["run_id"], "source_commit_id": data["commit"]["id"], "snapshot_sha256": snapshot_sha,
              "requires_explicit_execution": True, "provider_calls": 0, "actual_inference_performed": False}
    luna = {**common, "schema_version": "image-admin-luna-outbox-1", "status": "pending", "model": "gpt-5.6-luna",
        "metadata_contract": contract, "instruction_version": "luna-metadata-instruction-2026-09-03",
        "input_policy": "Image observations, original prompt intent and human memo stay separate; all content is untrusted data.",
        "human_review_required": True, "tasks": [{"id": ident, "task_key": _hash({"snapshot": snapshot_sha, "id": ident, "kind": "luna"}),
            "status": "pending", "snapshot_item_id": ident, "result": None,
            "external_ai_authorization_required": True} for ident in approved]}
    text = {**common, "schema_version": "image-admin-text-embedding-outbox-1", "status": "blocked_pending_metadata_and_model",
        "model": None, "dimensions": None, "tasks": [{"id": ident, "status": "blocked_pending_metadata_and_model",
            "snapshot_item_id": ident, "input_sections": ["original_prompt", "human_approved_llm_metadata", "human_memo", "explicit_human_tags"],
            "requires_human_approved_metadata": True, "embedding_request_key": None, "vector": None} for ident in approved]}
    return snapshot, luna, text, sorted({row["path"]: row for row in evidence}.values(), key=lambda row: row["path"])


def _key(run_id: str, commit_id: str) -> str:
    return _hash({"schema_version": SCHEMA, "run_id": run_id, "commit_id": commit_id})


def _summary(snapshot: dict, path: str, status: str) -> dict:
    return {"status": status, "schema_version": SCHEMA, "run_id": snapshot["run_id"],
        "commit_id": snapshot["source_commit"]["id"], "commit_revision": snapshot["source_commit"]["revision"],
        "handoff_path": path, "total_items": len(snapshot["items"]), "retained_items": len(snapshot["retained_ids"]),
        "approved_items": len(snapshot["approved_ids"]), "archived_aliases": len(snapshot["archived_aliases"]),
        "groups": len(snapshot["groups"]), "luna_pending": len(snapshot["approved_ids"]),
        "text_embedding_pending": len(snapshot["approved_ids"]),
        "cached_retained_image_vectors": sum(row["retention_status"] == "retained" and row["image_vector"]["status"] == "cached" for row in snapshot["items"]),
        "provider_calls": 0, "actual_inference_performed": False, "release_eligible": False,
        "incremental_consumer_connected": False}


def _verify_existing(root: Path, relative: str, data: dict, spec: dict | None = None) -> dict:
    directory = _safe(root, relative)
    receipt = _read(directory / "receipt.json")
    if (receipt.get("schema_version") != SCHEMA or receipt.get("status") != "prepared"
            or receipt.get("source_commit") != data["commit"] or receipt.get("run_id") != data["run_id"]
            or set(receipt.get("files", {})) != set(FILES)):
        raise HandoffError("Existing handoff receipt identity mismatch")
    for name, sha in receipt["files"].items():
        if _file_hash(_safe(root, relative + "/" + name)) != sha:
            raise HandoffError("Existing handoff artifact changed")
    for row in receipt["source_files"]:
        if _file_hash(_safe(root, row["path"])) != row["sha256"]:
            raise HandoffError("Existing handoff source evidence changed")
    luna = _read(directory / "luna-pending.json")
    ordered_contract = sorted(luna["metadata_contract"].values(), key=lambda row: row["workspace_relative_path"])
    if receipt.get("workspace_contract_files") != ordered_contract:
        raise HandoffError("Prepared metadata contract receipt mismatch")
    for row in receipt["workspace_contract_files"]:
        if _file_hash(_safe(root.parent, row["workspace_relative_path"])) != row["sha256"]:
            raise HandoffError("Prepared metadata contract changed")
    snapshot = _read(directory / "snapshot.json")
    if (snapshot["source_commit"] != data["commit"] or snapshot["normalized_decisions"] != data["normalized"]
            or snapshot["committed_front_sha256"] != _hash(data["front"])
            or snapshot["committed_groups_sha256"] != _hash(data["groups"])):
        raise HandoffError("Existing handoff does not match committed approval")
    spec = spec if spec is not None else _load_spec(root, data["run_id"])
    _validate_commit(spec, data)
    if snapshot.get("rights_catalog_sha256") != _hash(_rights(root, spec)):
        raise HandoffError("Prepared rights notices changed; explicit version review is required")
    text = _read(directory / "text-embedding-pending.json")
    snapshot_sha = receipt["files"]["snapshot.json"]
    for document in (luna, text):
        if (document.get("source_commit_id") != data["commit"]["id"]
                or document.get("snapshot_sha256") != snapshot_sha
                or document.get("requires_explicit_execution") is not True
                or document.get("actual_inference_performed") is not False
                or document.get("provider_calls") != 0
                or [row["id"] for row in document.get("tasks", [])] != snapshot["approved_ids"]):
            raise HandoffError("Prepared outbox is not bound to this approved snapshot")
    if (luna.get("model") != "gpt-5.6-luna" or luna.get("status") != "pending"
            or any(row.get("status") != "pending" or row.get("result") is not None for row in luna["tasks"])
            or text.get("model") is not None or text.get("dimensions") is not None
            or text.get("status") != "blocked_pending_metadata_and_model"
            or any(row.get("status") != "blocked_pending_metadata_and_model" or row.get("vector") is not None
                   or row.get("embedding_request_key") is not None for row in text["tasks"])):
        raise HandoffError("Preparation artifacts cannot claim executed metadata or embeddings")
    return snapshot


def prepare_admin_handoff(root: Path, db_path: Path, run_id: str, *, apply: bool = False,
                          expected_commit_id: str | None = None) -> dict:
    """Default is read-only; apply appends one commit-keyed, immutable directory.

    A newer commit detected during preparation aborts. There is deliberately no
    mutable 'latest' pointer; consumers compare source_commit_id to the live
    committed ID. This also makes a commit racing the final rename non-current.
    """
    root = Path(root).resolve()
    data = _committed(db_path, run_id)
    if data["commit"] is None:
        raise HandoffError("No committed approval is available for handoff")
    commit_id = data["commit"]["id"]
    if expected_commit_id is not None and expected_commit_id != commit_id:
        raise HandoffError("Expected committed approval is stale")
    spec = _load_spec(root, run_id)
    _validate_commit(spec, data)
    relative = f"{RELATIVE_ROOT}/{_key(run_id, commit_id)}"
    destination = _safe(root, relative)
    if destination.exists():
        snapshot = _verify_existing(root, relative, data, spec)
        _require_latest(db_path, run_id, commit_id)
        return _summary(snapshot, relative, "unchanged")
    snapshot, luna, text, evidence = _build(root, spec, data)
    _require_latest(db_path, run_id, commit_id)
    if not apply:
        return _summary(snapshot, relative, "dry_run")
    parent = _safe(root, RELATIVE_ROOT)
    parent.mkdir(parents=True, exist_ok=True)
    from .experiment import run_lock
    with run_lock(parent):
        _require_latest(db_path, run_id, commit_id)
        if destination.exists():
            snapshot = _verify_existing(root, relative, data, spec)
            _require_latest(db_path, run_id, commit_id)
            return _summary(snapshot, relative, "unchanged")
        staging = Path(tempfile.mkdtemp(prefix=".handoff-", dir=parent)).resolve()
        if not staging.is_relative_to(parent) or staging == parent:
            raise HandoffError("Unsafe handoff staging directory")
        try:
            file_hashes = {}
            for name, document in zip(FILES, (snapshot, luna, text)):
                content = _bytes(document)
                with (staging / name).open("xb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                file_hashes[name] = hashlib.sha256(content).hexdigest()
            receipt = {"schema_version": SCHEMA, "status": "prepared", "run_id": run_id,
                       "source_commit": data["commit"], "spec_sha256": spec["spec_sha256"],
                       "files": file_hashes, "source_files": evidence, "provider_calls": 0,
                       "workspace_contract_files": sorted(luna["metadata_contract"].values(), key=lambda row: row["workspace_relative_path"]),
                       "prepared_at": datetime.now(timezone.utc).isoformat(), "release_eligible": False}
            with (staging / "receipt.json").open("xb") as handle:
                handle.write(_bytes(receipt))
                handle.flush()
                os.fsync(handle.fileno())
            for row in evidence:
                if _file_hash(_safe(root, row["path"])) != row["sha256"]:
                    raise HandoffError("Source evidence changed during handoff preparation")
            for row in receipt["workspace_contract_files"]:
                if _file_hash(_safe(root.parent, row["workspace_relative_path"])) != row["sha256"]:
                    raise HandoffError("Metadata contract changed during handoff preparation")
            _require_latest(db_path, run_id, commit_id)
            staging.rename(destination)
        finally:
            if staging.exists():
                # This exact newly-created target was validated inside the
                # dedicated handoff root above; never clean arbitrary inputs.
                shutil.rmtree(staging)
    _require_latest(db_path, run_id, commit_id)
    return _summary(snapshot, relative, "prepared")


def handoff_status(root: Path, db_path: Path, run_id: str) -> dict:
    """Read only the current commit and its immutable prepared handoff."""
    root = Path(root).resolve()
    data = _committed(db_path, run_id)
    if data["commit"] is None:
        return {"status": "pending_no_commit", "run_id": run_id, "provider_calls": 0}
    relative = f"{RELATIVE_ROOT}/{_key(run_id, data['commit']['id'])}"
    if not _safe(root, relative).exists():
        return {"status": "pending_preparation", "run_id": run_id, "commit_id": data["commit"]["id"], "provider_calls": 0}
    snapshot = _verify_existing(root, relative, data)
    _require_latest(db_path, run_id, data["commit"]["id"])
    return _summary(snapshot, relative, "prepared")
