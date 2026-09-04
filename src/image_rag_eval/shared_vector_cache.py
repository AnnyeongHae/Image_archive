"""Append-only private vector snapshots; no provider calls or human approvals.

Import validates the live execution contract. Subsequent reads verify its frozen
file evidence, not mutable human-review directory listings. Additive human
decisions therefore do not invalidate an unchanged embedding execution.
"""
from __future__ import annotations

import re
import os
from contextlib import nullcontext
from pathlib import Path

from .comparison import load_inputs, request_key, requests_for
from .dataset import _safe_input_path
from .experiment import digest, json_bytes, read_json, run_lock, run_path
from .incremental_embedding import SCHEMA as EXECUTION_SCHEMA, _load, _receipt_vector, _state
from .voyage_provider import VOYAGE_MODEL

SCHEMA = "image-shared-vector-cache-1"
OBJECT_SCHEMA = "image-shared-vector-object-1"
PROTOCOL = "three-arm-canary-v1"
RELATIVE_ROOT = "data/private-research/image-rag-canary/shared-vector-cache"
IDENTITY_FIELDS = ("provider", "model", "dimensions", "image_sha256", "text", "task")
HEX = re.compile(r"^[a-f0-9]{64}$")


def _identity(request: dict) -> dict:
    if not isinstance(request, dict) or any(k not in request for k in IDENTITY_FIELDS):
        raise ValueError("complete embedding request identity required")
    identity = {k: request[k] for k in IDENTITY_FIELDS}
    identity["protocol"] = request.get("protocol", PROTOCOL)
    if (not all(isinstance(identity[k], str) and identity[k] for k in ("provider", "model", "task", "protocol"))
            or type(identity["dimensions"]) is not int or identity["dimensions"] <= 0
            or not isinstance(identity["text"], str)
            or (identity["image_sha256"] is not None and not HEX.fullmatch(str(identity["image_sha256"])))):
        raise ValueError("invalid embedding identity")
    return identity


def _key(request: dict) -> str:
    key = digest(json_bytes(_identity(request)))
    if "key" in request and request["key"] != key:
        raise ValueError("request key does not match model, input and protocol")
    return key


def _path(root: Path, relative: str) -> Path:
    rel = Path(relative)
    path = (root / rel).resolve()
    if rel.is_absolute() or not _safe_input_path(root, path) or not path.is_file():
        raise ValueError("unsafe or missing shared cache evidence")
    return path


def _bind(root: Path, files: dict, path: Path) -> str:
    path = path.resolve()
    relative = path.relative_to(root).as_posix()
    path = _path(root, relative)
    sha = digest(path.read_bytes())
    if relative in files and files[relative] != sha:
        raise ValueError("source changed during shared cache import")
    files[relative] = sha
    return relative


def _object(request: dict, receipt: dict) -> dict:
    if (_identity(request) != {**{k: request[k] for k in IDENTITY_FIELDS}, "protocol": PROTOCOL}
            or request["provider"] != "voyage" or request["model"] != VOYAGE_MODEL
            or request["dimensions"] != 1024 or _key(request) != request_key(request)):
        raise ValueError("unsupported imported model or protocol")
    values = _receipt_vector(receipt, {**request, "key": _key(request)})
    return {"schema_version": OBJECT_SCHEMA, "key": _key(request), "request_identity": _identity(request),
            "vector": values, "vector_sha256": digest(json_bytes(values))}


def _collect_comparison(root: Path, run_id: str) -> tuple[dict, dict]:
    source = run_path(root, run_id)
    manifest, _, pixels = load_inputs(root, run_id, maximum_items=200)
    dest = source / "comparison-v1"
    queries = read_json(dest / "queries.json")
    requests = requests_for(manifest, pixels, queries, arms_subset=["voyage_image"])
    ledger, aggregate = read_json(dest / "budget.json"), read_json(dest / "vectors.json")
    if not isinstance(ledger.get("attempts"), list):
        raise ValueError("comparison reservation history required")
    completed = {str(a.get("key", "")).split(":", 1)[0]: a["key"]
                 for a in ledger["attempts"] if a.get("status") == "completed"}
    expected_ids = {"voyage_image": {i["id"] for i in manifest["items"]},
                    "voyage_queries": {q["id"] for q in queries}}
    if any(not isinstance(aggregate.get(arm), dict) or set(aggregate[arm]) != ids
           for arm, ids in expected_ids.items()):
        raise ValueError("comparison aggregate is incomplete")
    files, objects, entries = {}, {}, {}
    budget_path = _bind(root, files, dest / "budget.json")
    for path in (source / "manifest.json", source / "prepared.json", dest / "queries.json", dest / "vectors.json"):
        _bind(root, files, path)
    # Bind recursively validated parent preparation receipts too.
    parent = source
    seen = set()
    while parent.name not in seen:
        seen.add(parent.name)
        prepared = read_json(parent / "prepared.json")
        _bind(root, files, parent / "manifest.json")
        _bind(root, files, parent / "prepared.json")
        if not prepared.get("source_run_id"):
            break
        parent = run_path(root, prepared["source_run_id"])
    for item in manifest["items"]:
        _bind(root, files, root / item["path"])
        _bind(root, files, source / item["prepared_path"])
    for request in requests:
        key = _key(request)
        if key not in completed:
            raise ValueError("comparison vector has no completed reservation")
        path = dest / "vector-cache" / (key + ".json")
        receipt = read_json(path)
        obj = _object(request, receipt)
        arm = "voyage_queries" if request["kind"] == "query" else "voyage_image"
        if aggregate[arm][request["id"]] != obj["vector"]:
            raise ValueError("comparison vector differs from successful receipt")
        objects[key] = obj
        receipt_path = _bind(root, files, path)
        entry = entries.setdefault(key, {"aliases": [], "provenance": []})
        entry["aliases"].append({"run_id": run_id, "id": request["id"], "kind": request["kind"]})
        provenance = {"run_id": run_id, "receipt_path": receipt_path, "budget_path": budget_path,
                      "attempt_key": completed[key]}
        if provenance not in entry["provenance"]:
            entry["provenance"].append(provenance)
    return {"run_id": run_id, "kind": "comparison", "files": files, "entries": entries}, objects


def _collect_incremental(root: Path, run_id: str) -> tuple[dict, dict]:
    # Import BEFORE later human decision imports: this deliberately retains the
    # preparation validator's full live source-binding and human-import guard.
    data = _load(root, run_id)
    ledger, cache, batches = _state(data)
    source, dest = data["source"], data["destination"]
    execution = read_json(dest / "execution-receipt.json")
    required = {"schema_version": EXECUTION_SCHEMA, "run_id": run_id, "provider": "voyage", "model": VOYAGE_MODEL,
                "manifest_sha256": data["manifest_sha256"], "source_bindings_sha256": data["source_bindings_sha256"],
                "status": "completed", "completed_image_ids": len(data["chosen"]), "target_image_ids": len(data["chosen"])}
    if (any(execution.get(k) != v for k, v in required.items()) or set(cache) != set(data["unique"])
            or type(execution.get("completed_image_ids")) is not int or type(execution.get("target_image_ids")) is not int):
        raise ValueError("complete source-bound incremental execution required")
    aggregate = read_json(dest / "vectors.json")
    if set(aggregate) != {"voyage_image"} or set(aggregate["voyage_image"]) != set(data["chosen"]):
        raise ValueError("incremental aggregate must contain exact new-only ids")
    files, objects, entries = {}, {}, {}
    for path, expected_sha in data["bound"].items():
        rel = _bind(root, files, path)
        if files[rel] != expected_sha:
            raise ValueError("incremental source binding changed during import")
    for path in (source / "manifest.json", source / "prepared.json", source / "source-bindings.json",
                 dest / "budget.json", dest / "execution-receipt.json", dest / "vectors.json"):
        _bind(root, files, path)
    for item in data["manifest"]["items"]:
        _bind(root, files, root / item["path"])
        _bind(root, files, source / item["prepared_path"])
    batch_paths = {}
    for path in sorted((dest / "batch-receipts").glob("*.json")):
        relative = _bind(root, files, path)
        for receipt in read_json(path)["receipts"]:
            batch_paths[receipt["key"]] = relative
    for record in ledger.get("retry_authorizations", []):
        _bind(root, files, root / record["failed_ledger_archive_path"])
        _bind(root, files, root / record["consent"]["investigation_evidence_path"])
    old = run_path(root, data["manifest"]["reference_run_id"]) / "comparison-v1"
    for ident in data["chosen"]:
        request = data["requests"][ident]
        key, receipt = request["key"], cache[request["key"]]
        obj = _object(request, receipt)
        if aggregate["voyage_image"][ident] != obj["vector"]:
            raise ValueError("incremental aggregate differs from successful receipt")
        local = dest / "vector-cache" / (key + ".json")
        receipt_path = local if local.is_file() else old / "vector-cache" / (key + ".json")
        # A full checkpoint without its per-key cache must be resumed by its
        # executor, not silently materialized by this cache import.
        if not receipt_path.is_file() or read_json(receipt_path) != receipt:
            raise ValueError("durable per-key cache receipt required")
        relative = _bind(root, files, receipt_path)
        budget_path = _bind(root, files, (dest if key in batches else old) / "budget.json")
        attempt = receipt.get("attempt_key", key)
        history = read_json(root / budget_path)["attempts"]
        if not any(a.get("key") == attempt and a.get("status") == "completed" for a in history):
            raise ValueError("incremental cache reservation is not completed")
        provenance = {"run_id": run_id, "receipt_path": relative, "budget_path": budget_path, "attempt_key": attempt}
        if key in batches:
            provenance["batch_path"] = batch_paths[key]
        objects[key] = obj
        entries[key] = {"aliases": [{"run_id": run_id, "id": ident, "kind": "document"}], "provenance": [provenance]}
    return {"run_id": run_id, "kind": "incremental", "files": files, "entries": entries}, objects


def _collect_source(root: Path, run_id: str) -> tuple[dict, dict]:
    manifest = read_json(run_path(root, run_id) / "manifest.json")
    return (_collect_incremental if manifest.get("schema_version") == "image-incremental-manifest-1"
            else _collect_comparison)(root, run_id)


def _revision(root: Path, revision_id: str) -> dict:
    if not isinstance(revision_id, str) or not HEX.fullmatch(revision_id):
        raise ValueError("invalid shared cache revision id")
    directory = root / RELATIVE_ROOT / "revisions" / revision_id
    manifest = read_json(_path(root, (directory / "manifest.json").relative_to(root).as_posix()))
    receipt = read_json(_path(root, (directory / "receipt.json").relative_to(root).as_posix()))
    if (manifest.get("schema_version") != SCHEMA or digest(json_bytes(manifest)) != revision_id
            or receipt != {"schema_version": SCHEMA, "revision_id": revision_id, "complete": True}):
        raise ValueError("shared cache revision identity mismatch")
    return manifest


def _head(root: Path, revision_id: str | None = None) -> tuple[str | None, dict | None]:
    if revision_id is not None:
        return revision_id, _revision(root, revision_id)
    revisions = {}
    for path in sorted((root / RELATIVE_ROOT / "revisions").glob("*/receipt.json")):
        revisions[path.parent.name] = _revision(root, path.parent.name)
    if not revisions:
        return None, None
    parents = {value.get("parent_revision_id") for value in revisions.values()} - {None}
    if not parents <= set(revisions):
        raise ValueError("shared cache revision parent is missing")
    heads = set(revisions) - parents
    if len(heads) != 1:
        raise ValueError("ambiguous shared cache revision heads")
    head = heads.pop()
    return head, revisions[head]


def _validate_snapshot(root: Path, manifest: dict) -> dict:
    objects, files = {}, {}
    if (not isinstance(manifest.get("sources"), dict) or not isinstance(manifest.get("entries"), dict)
            or manifest.get("human_approval_inferred") is not False or manifest.get("provider_calls") != 0):
        raise ValueError("invalid shared cache manifest")
    source_keys = {key for source in manifest["sources"].values() for key in source["entries"]}
    if source_keys != set(manifest["entries"]):
        raise ValueError("shared source content keys differ from index")
    for source in manifest["sources"].values():
        for relative, sha in source["files"].items():
            if relative in files and files[relative] != sha:
                raise ValueError("conflicting frozen source bindings")
            files[relative] = sha
    for relative, sha in files.items():
        if digest(_path(root, relative).read_bytes()) != sha:
            raise ValueError("frozen vector source evidence changed")
    parsed = {}
    def evidence(relative):
        if relative not in parsed:
            parsed[relative] = read_json(root / relative)
        return parsed[relative]
    for key, entry in manifest["entries"].items():
        if not HEX.fullmatch(key):
            raise ValueError("invalid shared content key")
        expected_path = f"{RELATIVE_ROOT}/objects/{key[:2]}/{key}.json"
        if entry.get("object_path") != expected_path:
            raise ValueError("shared object path is not content-addressed")
        obj = read_json(_path(root, expected_path))
        if (obj.get("schema_version") != OBJECT_SCHEMA or obj.get("key") != key
                or _key(obj.get("request_identity")) != key
                or digest(json_bytes(obj)) != entry.get("object_sha256")):
            raise ValueError("shared vector object identity mismatch")
        request = {**obj["request_identity"], "key": key}
        canonical = _object(request, {"key": key, "provider": request["provider"], "model": request["model"],
                                      "vector": obj["vector"], "vector_sha256": obj["vector_sha256"]})
        if canonical != obj or not entry.get("provenance") or not entry.get("aliases"):
            raise ValueError("shared vector provenance missing")
        for provenance in entry["provenance"]:
            source = manifest["sources"].get(provenance["run_id"])
            if source is None or key not in source["entries"] or provenance not in source["entries"][key]["provenance"]:
                raise ValueError("shared vector provenance is not source-bound")
            receipt_path, budget_path = provenance["receipt_path"], provenance["budget_path"]
            if receipt_path not in source["files"] or budget_path not in source["files"]:
                raise ValueError("shared receipt or ledger is not frozen")
            receipt = evidence(receipt_path)
            if _object(request, receipt) != obj:
                raise ValueError("shared object differs from original successful receipt")
            if not any(a.get("key") == provenance["attempt_key"] and a.get("status") == "completed"
                       for a in evidence(budget_path)["attempts"]):
                raise ValueError("shared receipt has no completed reservation")
            if "batch_path" in provenance:
                if provenance["batch_path"] not in source["files"]:
                    raise ValueError("shared batch checkpoint is not frozen")
                batch = evidence(provenance["batch_path"])
                if batch.get("status") != "completed" or receipt not in batch.get("receipts", []):
                    raise ValueError("shared receipt lacks successful full batch checkpoint")
        aliases = [alias for source in manifest["sources"].values()
                   for alias in source["entries"].get(key, {}).get("aliases", [])]
        if entry["aliases"] != _unique_rows(aliases):
            raise ValueError("shared alias lineage changed")
        objects[key] = obj
    return objects


def _unique_rows(rows: list[dict]) -> list[dict]:
    return [read for _, read in sorted({json_bytes(row): row for row in rows}.items())]


def _append_json(path: Path, value: dict, *, cache_root: Path) -> int:
    if not path.resolve().is_relative_to(cache_root.resolve()):
        raise ValueError("shared cache write path escapes cache root")
    payload = json_bytes(value)
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError("refusing to overwrite immutable shared cache evidence")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return 1


def build_shared_vector_cache(root: Path, source_run_ids: list[str], *, apply: bool = False) -> dict:
    """Import only declared, fully completed runs; dry-run never writes."""
    root = Path(root).resolve()
    if not isinstance(source_run_ids, list) or not source_run_ids or len(set(source_run_ids)) != len(source_run_ids):
        raise ValueError("declare distinct source run ids")
    for run_id in source_run_ids:
        run_path(root, run_id)
    destination = root / RELATIVE_ROOT
    if not destination.resolve().is_relative_to(root):
        raise ValueError("shared cache destination escapes archive root")
    if apply:
        destination.mkdir(parents=True, exist_ok=True)
    with run_lock(destination) if apply else nullcontext():
        head_id, head = _head(root)
        objects = _validate_snapshot(root, head) if head else {}
        sources = dict(head["sources"]) if head else {}
        for run_id in source_run_ids:
            if run_id in sources:
                continue  # Frozen file evidence already checked; no review-state revalidation.
            source, imported = _collect_source(root, run_id)
            for key, obj in imported.items():
                if key in objects and objects[key] != obj:
                    raise ValueError("conflicting vectors for the same content key")
                objects[key] = obj
            sources[run_id] = source
        entries = {}
        for run_id in sorted(sources):
            for key, evidence in sources[run_id]["entries"].items():
                entry = entries.setdefault(key, {"object_path": f"{RELATIVE_ROOT}/objects/{key[:2]}/{key}.json",
                    "object_sha256": digest(json_bytes(objects[key])), "aliases": [], "provenance": []})
                entry["aliases"].extend(evidence["aliases"])
                entry["provenance"].extend(evidence["provenance"])
        for entry in entries.values():
            entry["aliases"], entry["provenance"] = _unique_rows(entry["aliases"]), _unique_rows(entry["provenance"])
        unchanged = bool(head and sources == head["sources"] and entries == head["entries"])
        manifest = head if unchanged else {"schema_version": SCHEMA, "parent_revision_id": head_id,
            "sources": sources, "entries": entries, "provider_calls": 0, "human_approval_inferred": False,
            "original_vectors_preserved": True}
        revision_id = digest(json_bytes(manifest))
        writes = 0
        if apply and not unchanged:
            for key, entry in entries.items():
                writes += _append_json(root / entry["object_path"], objects[key], cache_root=destination)
            revision = destination / "revisions" / revision_id
            _validate_snapshot(root, manifest)
            writes += _append_json(revision / "manifest.json", manifest, cache_root=destination)
            writes += _append_json(revision / "receipt.json", {"schema_version": SCHEMA, "revision_id": revision_id, "complete": True}, cache_root=destination)
        aliases = [alias for entry in entries.values() for alias in entry["aliases"]]
        return {"status": "unchanged" if unchanged else ("built" if apply else "dry_run"), "writes": writes,
            "revision_id": revision_id, "revision_manifest_path": str(destination / "revisions" / revision_id / "manifest.json"),
            "source_run_ids": sorted(sources), "unique_vectors": len(objects),
            "image_vectors": sum(o["request_identity"]["image_sha256"] is not None for o in objects.values()),
            "query_vectors": sum(o["request_identity"]["task"] == "RETRIEVAL_QUERY" for o in objects.values()),
            "document_aliases": sum(a["kind"] == "document" for a in aliases),
            "query_aliases": sum(a["kind"] == "query" for a in aliases), "provider_calls": 0, "human_approved": False}


def lookup_shared_vectors(root: Path, requests: list[dict], *, revision_id: str | None = None) -> dict[str, dict | None]:
    """Verify one immutable snapshot once per batch; missing identities return None."""
    root = Path(root).resolve()
    keys = [_key(request) for request in requests]
    found_id, manifest = _head(root, revision_id)
    if manifest is None:
        return {key: None for key in keys}
    objects = _validate_snapshot(root, manifest)
    result = {}
    for key in keys:
        obj = objects.get(key)
        result[key] = None if obj is None else {"key": key, "provider": obj["request_identity"]["provider"],
            "model": obj["request_identity"]["model"], "vector": obj["vector"], "vector_sha256": obj["vector_sha256"],
            "shared_revision_id": found_id, "shared_object_path": manifest["entries"][key]["object_path"],
            "shared_provenance": manifest["entries"][key]["provenance"]}
    return result


def lookup_shared_vector(root: Path, request: dict, *, revision_id: str | None = None) -> dict | None:
    return lookup_shared_vectors(root, [request], revision_id=revision_id)[_key(request)]
