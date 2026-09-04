"""Carry validated parent comparison state into a new prepared run without network calls."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .comparison import load_inputs, requests_for
from .experiment import digest, json_bytes, now, read_json, run_lock, run_path, write_json


def _payload_counter(manifest: dict[str, Any]) -> Counter[tuple[str, str]]:
    payloads: Counter[tuple[str, str]] = Counter()
    for item in manifest.get("items", []):
        payloads[(str(item["prepared_sha256"]), str(item.get("embedding_prompt", "")))] += 1
    return payloads


def _validate_prepared_receipt(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    payload = read_json(path)
    if payload.get("complete") is not True or payload.get("manifest_sha256") != digest(json_bytes(manifest)):
        raise ValueError("prepared receipt mismatch")
    return payload


def _semantic_attempt_key(attempt_key: str) -> str:
    return str(attempt_key).split(":", 1)[0]


def _validated_parent_cache(parent_manifest: dict[str, Any], parent_pixels: dict[str, int],
                            parent_queries: list[dict[str, Any]], parent_ledger: dict[str, Any],
                            parent_cache_dir: Path) -> dict[str, dict[str, Any]]:
    expected = {request["key"]: request for request in requests_for(parent_manifest, parent_pixels, parent_queries)}
    attempts = parent_ledger.get("attempts")
    if not isinstance(attempts, list):
        raise ValueError("parent ledger attempts missing")
    completed_keys = {_semantic_attempt_key(str(attempt.get("key", ""))) for attempt in attempts
                      if attempt.get("status") == "completed"}
    validated: dict[str, dict[str, Any]] = {}
    if not parent_cache_dir.is_dir():
        raise ValueError("parent vector cache missing")
    for path in sorted(parent_cache_dir.glob("*.json")):
        payload = read_json(path)
        key = path.stem
        request = expected.get(key)
        if request is None:
            raise ValueError("parent cache contains unexpected key")
        vector = payload.get("vector")
        if (payload.get("key") != key or payload.get("model") != request["model"]
                or not isinstance(vector, list) or len(vector) != request["dimensions"]
                or payload.get("vector_sha256") != digest(json_bytes(vector))):
            raise ValueError("parent cached vector identity mismatch")
        if key not in completed_keys:
            raise ValueError("parent cache receipt has no completed attempt")
        validated[key] = payload
    missing = sorted(completed_keys - set(validated))
    if missing:
        raise ValueError("completed parent attempts missing cache receipts")
    return validated


def _carryover_receipt(*, parent_run_id: str, new_run_id: str, parent_manifest: dict[str, Any],
                       new_manifest: dict[str, Any], parent_prepared: dict[str, Any], new_prepared: dict[str, Any],
                       parent_ledger: dict[str, Any], copied_cache: dict[str, dict[str, Any]],
                       recorded_at: str) -> dict[str, Any]:
    copied_keys = sorted(copied_cache)
    return {
        "schema_version": "1",
        "status": "carryover_ready",
        "recorded_at": recorded_at,
        "parent_run_id": parent_run_id,
        "new_run_id": new_run_id,
        "parent_manifest_sha256": digest(json_bytes(parent_manifest)),
        "new_manifest_sha256": digest(json_bytes(new_manifest)),
        "parent_prepared_receipt_sha256": digest(json_bytes(parent_prepared)),
        "new_prepared_receipt_sha256": digest(json_bytes(new_prepared)),
        "parent_ledger_sha256": digest(json_bytes(parent_ledger)),
        "copied_cache_receipt_count": len(copied_keys),
        "copied_cache_keys_sha256": digest(json_bytes(copied_keys)),
        "copied_attempt_count": len(parent_ledger["attempts"]),
        "copied_completed_attempt_count": sum(1 for attempt in parent_ledger["attempts"] if attempt.get("status") == "completed"),
        "payload_match_basis": "prepared_sha256_plus_embedding_prompt_multiset_subset",
        "network_calls": 0,
        "secrets_copied": 0,
    }


def _ensure_idempotent_json(path: Path, expected: dict[str, Any], *, label: str) -> None:
    if path.exists() and digest(json_bytes(read_json(path))) != digest(json_bytes(expected)):
        raise ValueError(f"existing {label} differs from carryover source")


def _ensure_idempotent_cache(cache_dir: Path, expected: dict[str, dict[str, Any]]) -> None:
    if not cache_dir.exists():
        return
    if not cache_dir.is_dir():
        raise ValueError("existing vector cache path is not a directory")
    existing = {path.stem: path for path in cache_dir.glob("*.json")}
    unexpected = sorted(set(existing) - set(expected))
    if unexpected:
        raise ValueError("existing vector cache contains unrelated state")
    for key, payload in expected.items():
        path = existing.get(key)
        if path is None:
            continue
        if digest(json_bytes(read_json(path))) != digest(json_bytes(payload)):
            raise ValueError("existing vector cache differs from parent receipt")


def _ensure_idempotent_list_json(path: Path, expected: list[dict[str, Any]], *, label: str) -> None:
    if path.exists() and digest(json_bytes(read_json(path))) != digest(json_bytes(expected)):
        raise ValueError(f"existing {label} differs from carryover source")


def _assert_parent_matches_existing_receipt(path: Path, *, parent_manifest: dict[str, Any],
                                            parent_prepared: dict[str, Any], parent_ledger: dict[str, Any]) -> None:
    if not path.exists():
        return
    payload = read_json(path)
    if payload.get("parent_manifest_sha256") != digest(json_bytes(parent_manifest)):
        raise ValueError("parent source changed since carryover receipt")
    if payload.get("parent_prepared_receipt_sha256") != digest(json_bytes(parent_prepared)):
        raise ValueError("parent prepared receipt changed since carryover receipt")
    if payload.get("parent_ledger_sha256") != digest(json_bytes(parent_ledger)):
        raise ValueError("parent ledger changed since carryover receipt")


def _existing_recorded_at(path: Path) -> str | None:
    if not path.exists():
        return None
    recorded_at = read_json(path).get("recorded_at")
    if isinstance(recorded_at, str) and recorded_at.strip():
        return recorded_at
    return None


def _load_existing_carryover(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError("carryover receipt is required before inference")
    payload = read_json(path)
    if payload.get("status") != "carryover_ready":
        raise ValueError("invalid carryover receipt")
    return payload


def validate_parent_checkpoint(root: Path, new_run_id: str, ledger_data: dict[str, Any]) -> dict[str, Any]:
    new_source = run_path(root, new_run_id)
    if not new_source.is_dir():
        raise ValueError("new run must already exist")
    carryover_path = new_source / "comparison-v1" / "carryover.json"
    carryover = _load_existing_carryover(carryover_path)
    parent_run_id = str(carryover.get("parent_run_id") or "").strip()
    if not parent_run_id:
        raise ValueError("carryover receipt missing parent run id")
    parent_source = run_path(root, parent_run_id)
    if not parent_source.is_dir():
        raise ValueError("parent run must still exist")
    parent_manifest, _, _ = load_inputs(root, parent_run_id, maximum_items=50)
    parent_prepared = _validate_prepared_receipt(parent_source / "prepared.json", parent_manifest)
    parent_ledger = read_json(parent_source / "comparison-v1" / "budget.json")
    _assert_parent_matches_existing_receipt(
        carryover_path,
        parent_manifest=parent_manifest,
        parent_prepared=parent_prepared,
        parent_ledger=parent_ledger,
    )
    attempts = ledger_data.get("attempts")
    parent_attempts = parent_ledger.get("attempts")
    copied_attempt_count = carryover.get("copied_attempt_count")
    if not isinstance(attempts, list) or not isinstance(parent_attempts, list):
        raise ValueError("ledger attempts missing")
    if not isinstance(copied_attempt_count, int) or copied_attempt_count < 0:
        raise ValueError("carryover receipt missing copied attempt count")
    if len(parent_attempts) != copied_attempt_count or len(attempts) < copied_attempt_count:
        raise ValueError("child ledger does not preserve the pinned parent attempt prefix")
    if digest(json_bytes(attempts[:copied_attempt_count])) != digest(json_bytes(parent_attempts)):
        raise ValueError("child ledger does not preserve the pinned parent attempt prefix")
    return {
        "status": "checkpoint_valid",
        "parent_run_id": parent_run_id,
        "new_run_id": new_run_id,
        "copied_attempt_count": copied_attempt_count,
        "parent_ledger_sha256": digest(json_bytes(parent_ledger)),
        "parent_manifest_sha256": digest(json_bytes(parent_manifest)),
        "parent_prepared_receipt_sha256": digest(json_bytes(parent_prepared)),
        "network_calls": 0,
    }


def import_parent_cache_and_ledger(root: Path, parent_run_id: str, new_run_id: str, apply: bool = False) -> dict[str, Any]:
    parent_source = run_path(root, parent_run_id)
    new_source = run_path(root, new_run_id)
    if parent_source == new_source:
        raise ValueError("parent and new run ids must differ")
    if not parent_source.is_dir() or not new_source.is_dir():
        raise ValueError("both parent and new runs must already exist")

    runs_root = parent_source.parent
    parent_comparison = parent_source / "comparison-v1"
    new_comparison = new_source / "comparison-v1"
    parent_budget_path = parent_comparison / "budget.json"
    parent_queries_path = parent_comparison / "queries.json"
    parent_cache_dir = parent_comparison / "vector-cache"
    new_budget_path = new_comparison / "budget.json"
    new_queries_path = new_comparison / "queries.json"
    new_cache_dir = new_comparison / "vector-cache"
    carryover_path = new_comparison / "carryover.json"

    with run_lock(runs_root), run_lock(parent_source), run_lock(new_source):
        parent_manifest, _, parent_pixels = load_inputs(root, parent_run_id, maximum_items=50)
        new_manifest, _, _ = load_inputs(root, new_run_id, maximum_items=200)
        parent_prepared = _validate_prepared_receipt(parent_source / "prepared.json", parent_manifest)
        new_prepared = _validate_prepared_receipt(new_source / "prepared.json", new_manifest)

        parent_payloads = _payload_counter(parent_manifest)
        new_payloads = _payload_counter(new_manifest)
        if any(new_payloads[payload] < count for payload, count in parent_payloads.items()):
            raise ValueError("new run does not include the parent sample by payload")

        if not parent_budget_path.is_file() or not parent_queries_path.is_file():
            raise ValueError("parent comparison state is incomplete")
        parent_ledger = read_json(parent_budget_path)
        parent_queries = read_json(parent_queries_path)
        if not isinstance(parent_queries, list):
            raise ValueError("parent queries must be a list")
        copied_cache = _validated_parent_cache(parent_manifest, parent_pixels, parent_queries, parent_ledger, parent_cache_dir)
        recorded_at = _existing_recorded_at(carryover_path) or now()
        receipt = _carryover_receipt(
            parent_run_id=parent_run_id,
            new_run_id=new_run_id,
            parent_manifest=parent_manifest,
            new_manifest=new_manifest,
            parent_prepared=parent_prepared,
            new_prepared=new_prepared,
            parent_ledger=parent_ledger,
            copied_cache=copied_cache,
            recorded_at=recorded_at,
        )

        if new_comparison.exists() and not new_comparison.is_dir():
            raise ValueError("new comparison path is not a directory")
        _assert_parent_matches_existing_receipt(
            carryover_path,
            parent_manifest=parent_manifest,
            parent_prepared=parent_prepared,
            parent_ledger=parent_ledger,
        )
        _ensure_idempotent_json(new_budget_path, parent_ledger, label="budget ledger")
        _ensure_idempotent_list_json(new_queries_path, parent_queries, label="queries")
        _ensure_idempotent_cache(new_cache_dir, copied_cache)
        _ensure_idempotent_json(carryover_path, receipt, label="carryover receipt")

        if not apply:
            return {
                "status": "dry_run",
                "network_calls": 0,
                "parent_run_id": parent_run_id,
                "new_run_id": new_run_id,
                "copied_attempt_count": len(parent_ledger["attempts"]),
                "copied_cache_receipt_count": len(copied_cache),
                "parent_ledger_sha256": receipt["parent_ledger_sha256"],
                "parent_manifest_sha256": receipt["parent_manifest_sha256"],
                "new_manifest_sha256": receipt["new_manifest_sha256"],
            }

        new_comparison.mkdir(exist_ok=True)
        new_cache_dir.mkdir(exist_ok=True)
        write_json(new_budget_path, parent_ledger)
        write_json(new_queries_path, parent_queries)
        for key, payload in copied_cache.items():
            write_json(new_cache_dir / f"{key}.json", payload)
        write_json(carryover_path, receipt)
        return {
            "status": "carried_over",
            "network_calls": 0,
            "parent_run_id": parent_run_id,
            "new_run_id": new_run_id,
            "copied_attempt_count": len(parent_ledger["attempts"]),
            "copied_cache_receipt_count": len(copied_cache),
            "carryover_path": str(carryover_path),
            "budget_path": str(new_budget_path),
        }


__all__ = ["import_parent_cache_and_ledger", "validate_parent_checkpoint"]
