from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

try:
    from .common import ARCHIVE_ROOT, DATA_ROOT, LEGACY_ROOT, atomic_write_json, atomic_write_text, read_json, stable_json
except ImportError:
    from common import ARCHIVE_ROOT, DATA_ROOT, LEGACY_ROOT, atomic_write_json, atomic_write_text, read_json, stable_json


ARCHIVE_PATH = DATA_ROOT / "archive" / "opennana_records.json"
PROJECTION_PATH = LEGACY_ROOT / "opennana-catalog-data.js"
ACCEPTED_DECISIONS = {"approve", "group"}
ARCHIVE_SCHEMA = "opennana-internal-archive-1.0"
RECORD_SCHEMA = "opennana-internal-archive-record-1.0"


def sha256_json(value: Any) -> str:
    return hashlib.sha256(stable_json(value, indent=None).encode("utf-8")).hexdigest()


def source_sort_key(value: str) -> tuple[int, int | str]:
    return (0, int(value)) if value.isdigit() else (1, value.casefold())


def relative_artifact_path(path: Path, root: Path = ARCHIVE_ROOT) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def json_object(path: Path) -> dict[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def resolve_artifact_paths(
    *,
    pending_paths: Iterable[Path] | None = None,
    applied_paths: Iterable[Path] | None = None,
    data_root: Path = DATA_ROOT,
) -> tuple[list[Path], list[Path]]:
    pending = [Path(path) for path in (pending_paths or [])]
    applied = [Path(path) for path in (applied_paths or [])]
    env_pending = os.environ.get("OPENNANA_PENDING_PATH")
    env_applied = os.environ.get("OPENNANA_APPLIED_PATH")
    if env_pending:
        pending.append(Path(env_pending))
    if env_applied:
        applied.append(Path(env_applied))
    # The hook paths identify and validate the just-committed batch, but the
    # projection is always a fold of every immutable historical artifact. This
    # prevents a later review batch from replacing earlier approved records.
    pending.extend(sorted((data_root / "staging").glob("canonicalization-pending-*.json")))
    applied.extend(sorted((data_root / "decisions").glob("applied-*.json")))
    if bool(pending) != bool(applied):
        raise ValueError("pending and applied artifacts must be supplied together")
    missing = [str(path) for path in [*pending, *applied] if not path.is_file()]
    if missing:
        raise ValueError("missing immutable decision artifacts: " + ", ".join(missing))
    return sorted(set(path.resolve() for path in pending)), sorted(set(path.resolve() for path in applied))


def decision_index(applied_paths: Iterable[Path]) -> dict[tuple[str, str], dict[str, Any]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for path in sorted(applied_paths):
        payload = json_object(path)
        if payload.get("schema_version") != "opennana-applied-decisions-1.0":
            raise ValueError(f"unsupported applied decision artifact: {path}")
        for raw in payload.get("decisions") or []:
            if not isinstance(raw, dict):
                raise ValueError(f"non-object decision in {path}")
            action = str(raw.get("decision") or "")
            if action not in ACCEPTED_DECISIONS:
                continue
            upstream_id = str(raw.get("upstream_id") or "").strip()
            content_sha256 = str(raw.get("content_sha256") or "").strip()
            if not upstream_id or len(content_sha256) != 64:
                raise ValueError(f"invalid approved decision identity in {path}")
            key = (upstream_id, content_sha256)
            normalized = {
                "queue_id": str(raw.get("queue_id") or ""),
                "upstream_id": upstream_id,
                "content_sha256": content_sha256,
                "decision": action,
                "group_with": raw.get("group_with") if action == "group" else None,
                "note": str(raw.get("note") or ""),
                "run_id": str(payload.get("run_id") or ""),
                "queue_revision": str(payload.get("queue_revision") or ""),
                "decided_at": str(payload.get("decided_at") or ""),
                "applied_artifact": relative_artifact_path(path),
            }
            previous = index.get(key)
            if previous and any(
                previous.get(field) != normalized.get(field)
                for field in ("decision", "group_with", "queue_id")
            ):
                raise ValueError(f"conflicting durable decisions for OpenNana source version {key}")
            if previous is None or (
                normalized["decided_at"], normalized["run_id"], normalized["applied_artifact"]
            ) > (
                previous["decided_at"], previous["run_id"], previous["applied_artifact"]
            ):
                index[key] = normalized
    return index


def normalized_image_urls(value: Any) -> list[str]:
    result: list[str] = []
    for item in value if isinstance(value, list) else []:
        url = str(item or "").strip()
        if url.startswith(("https://", "http://")) and url not in result:
            result.append(url)
    return result


def internal_record(
    raw: dict[str, Any],
    decision: dict[str, Any],
    pending_path: Path,
) -> dict[str, Any]:
    upstream_id = str(raw.get("upstream_id") or "").strip()
    source_content_sha256 = str(raw.get("content_sha256") or "").strip()
    if not upstream_id or len(source_content_sha256) != 64:
        raise ValueError(f"invalid pending record identity in {pending_path}")
    if str((raw.get("human_decision") or {}).get("decision") or "") != decision["decision"]:
        raise ValueError(f"pending/applied decision mismatch for {upstream_id}:{source_content_sha256}")
    prompt = str(raw.get("prompt_text") or "").strip()
    source_url = str(raw.get("source_url") or "").strip()
    if not prompt or not source_url.startswith(("https://", "http://")):
        raise ValueError(f"approved OpenNana row lacks prompt or source URL: {upstream_id}")
    image_urls = normalized_image_urls(raw.get("image_urls"))
    record: dict[str, Any] = {
        "schema_version": RECORD_SCHEMA,
        "record_id": f"OPENNANA-{upstream_id}",
        "reference_style_id": f"ONN-{upstream_id}",
        "source_id": "opennana-approved",
        "source_name": "OpenNana",
        "source_type": "curated_prompt_gallery_internal",
        "source": "opennana",
        "source_url": source_url,
        "upstream_id": upstream_id,
        "slug": str(raw.get("slug") or ""),
        "title": str(raw.get("title") or f"OpenNana {upstream_id}"),
        "prompt": prompt,
        "prompt_sha256": str(raw.get("prompt_sha256") or hashlib.sha256(prompt.encode("utf-8")).hexdigest()),
        "source_content_sha256": source_content_sha256,
        "image": image_urls[0] if image_urls else None,
        "image_url": image_urls[0] if image_urls else None,
        "image_urls": image_urls,
        "thumbnail": image_urls[0] if image_urls else None,
        "media_type": raw.get("media_type"),
        "model_relation": "reported_generation_model",
        "reported_model": raw.get("model"),
        "tags": list(dict.fromkeys(str(item) for item in (raw.get("tags") or []) if str(item).strip())),
        "style_tags": list(dict.fromkeys(str(item) for item in (raw.get("tags") or []) if str(item).strip())),
        "use_case_tags": ["prompt_reference", "internal_archive"],
        "languages": [],
        "author": raw.get("author"),
        "updated_at": raw.get("updated_at"),
        "approved_at": decision["decided_at"],
        "human_decision": {
            key: decision.get(key)
            for key in ("decision", "group_with", "note", "queue_id", "run_id", "queue_revision")
        },
        "workflow_status": "approved_internal_archive",
        "review_status": "human_approved_internal_reference",
        "rights_status": "not_cleared",
        "rights_tier": "P3",
        "portfolio_visibility": "admin_only",
        "release_eligible": False,
        "rights": {
            "status": "not_cleared",
            "rights_tier": "P3",
            "portfolio_visibility": "admin_only",
            "release_eligible": False,
            "explicitly_cleared": False,
            "prompt_publication_eligible": False,
            "media_publication_eligible": False,
            "commercial_reuse_claimed": False,
            "requires_human_review": True,
            "item_rights": "unverified",
            "source_image_downloaded": False,
            "purpose": "private_reference_archive",
        },
        "risk_flags": ["item_rights_unverified", "public_release_not_approved"],
        "prompt_image_pair_status": "upstream_gallery_pair_unverified",
        "provenance_status": "human_curated_from_immutable_review_artifacts",
        "ingest_mode": "opennana_human_approval_api",
        "local_asset_status": "remote_reference_only" if image_urls else "not_provided",
        "raw_metadata": {
            "archive_group": "opennana_approved",
            "upstream_id": upstream_id,
            "source_content_sha256": source_content_sha256,
            "pending_artifact": relative_artifact_path(pending_path),
            "applied_artifact": decision["applied_artifact"],
            "public_release_eligible": False,
        },
    }
    record["archive_record_sha256"] = sha256_json(record)
    return record


def fold_records(
    pending_paths: Iterable[Path],
    applied_paths: Iterable[Path],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    decisions = decision_index(applied_paths)
    exact_versions: dict[tuple[str, str], dict[str, Any]] = {}
    duplicate_version_count = 0
    for path in sorted(pending_paths):
        payload = json_object(path)
        if payload.get("schema_version") != "opennana-canonicalization-pending-1.0":
            raise ValueError(f"unsupported canonicalization-pending artifact: {path}")
        if payload.get("public_release_eligible") is not False:
            raise ValueError(f"pending artifact attempts public release: {path}")
        records = payload.get("records") or []
        if int(payload.get("record_count") or 0) != len(records):
            raise ValueError(f"pending record_count mismatch: {path}")
        for raw in records:
            if not isinstance(raw, dict):
                raise ValueError(f"non-object pending record in {path}")
            upstream_id = str(raw.get("upstream_id") or "").strip()
            source_hash = str(raw.get("content_sha256") or "").strip()
            key = (upstream_id, source_hash)
            decision = decisions.get(key)
            if decision is None:
                raise ValueError(f"pending approved version has no matching durable decision: {key}")
            row = internal_record(raw, decision, path)
            previous = exact_versions.get(key)
            if previous is not None:
                duplicate_version_count += 1
                comparable_fields = ("prompt", "source_url", "image_urls", "slug", "title")
                if any(previous.get(field) != row.get(field) for field in comparable_fields):
                    raise ValueError(f"same OpenNana source hash carries conflicting content: {key}")
                if (row["approved_at"], row["raw_metadata"]["pending_artifact"]) <= (
                    previous["approved_at"], previous["raw_metadata"]["pending_artifact"]
                ):
                    continue
            exact_versions[key] = row

    latest_by_upstream: dict[str, dict[str, Any]] = {}
    superseded_count = 0
    for row in exact_versions.values():
        upstream_id = row["upstream_id"]
        previous = latest_by_upstream.get(upstream_id)
        if previous is None:
            latest_by_upstream[upstream_id] = row
            continue
        candidate_key = (
            str(row.get("updated_at") or ""),
            str(row.get("approved_at") or ""),
            row["source_content_sha256"],
        )
        previous_key = (
            str(previous.get("updated_at") or ""),
            str(previous.get("approved_at") or ""),
            previous["source_content_sha256"],
        )
        superseded_count += 1
        if candidate_key > previous_key:
            latest_by_upstream[upstream_id] = row

    result = sorted(latest_by_upstream.values(), key=lambda row: source_sort_key(row["upstream_id"]))
    return result, {
        "durable_accepted_decision_count": len(decisions),
        "exact_source_version_count": len(exact_versions),
        "duplicate_source_version_count": duplicate_version_count,
        "superseded_source_version_count": superseded_count,
    }


def archive_payload(
    records: list[dict[str, Any]],
    pending_paths: Iterable[Path],
    applied_paths: Iterable[Path],
    fold_summary: dict[str, Any],
) -> dict[str, Any]:
    projected_at = max((str(row.get("approved_at") or "") for row in records), default=None)
    payload: dict[str, Any] = {
        "schema_version": ARCHIVE_SCHEMA,
        "projected_at": projected_at,
        "record_count": len(records),
        "public_release_eligible": False,
        "rights_policy": {
            "mode": "fail_closed",
            "human_curation_is_not_rights_clearance": True,
            "prompt_publication_eligible": False,
            "media_publication_eligible": False,
            "source_image_downloaded": False,
        },
        "fold_summary": fold_summary,
        "source_artifacts": {
            "pending": [relative_artifact_path(path) for path in sorted(pending_paths)],
            "applied": [relative_artifact_path(path) for path in sorted(applied_paths)],
        },
        "records": records,
    }
    payload["content_sha256"] = sha256_json(payload)
    return payload


def projection_javascript(payload: dict[str, Any]) -> str:
    return "window.DETAILPAGE_OPENNANA_RECORDS = " + json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + ";\n"


def trigger_promoted_count(trigger_pending_paths: Iterable[Path], records: list[dict[str, Any]]) -> int:
    final_versions = {
        (str(row.get("upstream_id") or ""), str(row.get("source_content_sha256") or ""))
        for row in records
    }
    trigger_versions: set[tuple[str, str]] = set()
    for path in trigger_pending_paths:
        payload = json_object(path)
        for row in payload.get("records") or []:
            if not isinstance(row, dict):
                continue
            action = str((row.get("human_decision") or {}).get("decision") or "")
            if action in ACCEPTED_DECISIONS:
                trigger_versions.add(
                    (str(row.get("upstream_id") or ""), str(row.get("content_sha256") or ""))
                )
    return len(trigger_versions & final_versions)


def canonical_output_paths(root: Path = ARCHIVE_ROOT) -> list[Path]:
    fixed = [
        root / "data" / "canonical" / "archive_records.jsonl",
        root / "data" / "canonical" / "archive_records_manifest.json",
        root / "data" / "canonical" / "archive_inventory.json",
        root / "data" / "public-export" / "catalog-index.json",
    ]
    shards = sorted((root / "data" / "public-export" / "shards").glob("catalog-*.json"))
    return [*fixed, *shards]


def snapshot_canonical_outputs(snapshot_root: Path, root: Path = ARCHIVE_ROOT) -> set[str]:
    present: set[str] = set()
    for path in canonical_output_paths(root):
        if not path.is_file():
            continue
        relative = path.resolve().relative_to(root.resolve()).as_posix()
        present.add(relative)
        destination = snapshot_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    return present


def restore_canonical_outputs(snapshot_root: Path, present: set[str], root: Path = ARCHIVE_ROOT) -> None:
    for path in canonical_output_paths(root):
        relative = path.resolve().relative_to(root.resolve()).as_posix()
        if relative not in present and path.is_file():
            path.unlink()
    for relative in sorted(present):
        source = snapshot_root / relative
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    for directory in (
        root / "data" / "canonical",
        root / "data" / "public-export",
        root / "data" / "public-export" / "shards",
    ):
        if directory.is_dir():
            for temporary in directory.glob("*.tmp"):
                temporary.unlink()


def build(
    *,
    pending_paths: Iterable[Path] | None = None,
    applied_paths: Iterable[Path] | None = None,
    archive_path: Path = ARCHIVE_PATH,
    projection_path: Path = PROJECTION_PATH,
    data_root: Path = DATA_ROOT,
    apply: bool,
    rebuild_canonical: bool = True,
) -> dict[str, Any]:
    requested_pending = [Path(path) for path in (pending_paths or [])]
    requested_applied = [Path(path) for path in (applied_paths or [])]
    env_pending = os.environ.get("OPENNANA_PENDING_PATH")
    trigger_pending_paths = requested_pending or ([Path(env_pending)] if env_pending else [])
    pending, applied = resolve_artifact_paths(
        pending_paths=requested_pending,
        applied_paths=requested_applied,
        data_root=data_root,
    )
    records, fold_summary = fold_records(pending, applied)
    promoted_from_trigger = trigger_promoted_count(trigger_pending_paths, records)
    payload = archive_payload(records, pending, applied, fold_summary)
    archive_text = stable_json(payload)
    projection_text = projection_javascript(payload)
    unchanged = (
        archive_path.is_file()
        and projection_path.is_file()
        and archive_path.read_text(encoding="utf-8") == archive_text
        and projection_path.read_text(encoding="utf-8") == projection_text
    )
    summary: dict[str, Any] = {
        "mode": "apply" if apply else "dry_run",
        "record_count": len(records),
        "count": len(records),
        "promoted_from_trigger": promoted_from_trigger,
        "promoted_internal_archive": promoted_from_trigger,
        "source_version_count": fold_summary["exact_source_version_count"],
        "duplicate_source_version_count": fold_summary["duplicate_source_version_count"],
        "superseded_source_version_count": fold_summary["superseded_source_version_count"],
        "archive_path": relative_artifact_path(archive_path),
        "projection_path": relative_artifact_path(projection_path),
        "content_sha256": payload["content_sha256"],
        "unchanged": unchanged,
        "outputs_written": False,
        "canonical_rebuilt": False,
        "inventory_rebuilt": False,
        "release_eligible": False,
        "public_release_effect": False,
    }
    if not apply:
        return summary

    previous_archive = archive_path.read_bytes() if archive_path.exists() else None
    previous_projection = projection_path.read_bytes() if projection_path.exists() else None
    rollback_parent = DATA_ROOT if rebuild_canonical else archive_path.parent
    rollback_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="opennana-archive-rollback-", dir=rollback_parent) as temp_dir:
        snapshot_root = Path(temp_dir)
        canonical_before = snapshot_canonical_outputs(snapshot_root) if rebuild_canonical else set()
        try:
            if not unchanged:
                atomic_write_json(archive_path, payload)
                atomic_write_text(projection_path, projection_text)
                summary["outputs_written"] = True
            if rebuild_canonical:
                command = [sys.executable, str(ARCHIVE_ROOT / "src" / "build_canonical_archive.py"), "--apply"]
                completed = subprocess.run(command, cwd=ARCHIVE_ROOT, capture_output=True, text=True, check=False)
                if completed.returncode != 0:
                    raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "canonical rebuild failed")
                canonical_summary = json.loads(completed.stdout)
                summary["canonical_rebuilt"] = True
                summary["canonical_record_count"] = canonical_summary.get("record_count")
                summary["canonical_lane_counts"] = canonical_summary.get("lane_counts")
                inventory_command = [
                    sys.executable,
                    str(ARCHIVE_ROOT / "src" / "build_archive_inventory.py"),
                    "--apply",
                ]
                inventory_completed = subprocess.run(
                    inventory_command,
                    cwd=ARCHIVE_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if inventory_completed.returncode != 0:
                    raise RuntimeError(
                        inventory_completed.stderr.strip()
                        or inventory_completed.stdout.strip()
                        or "inventory rebuild failed"
                    )
                inventory_summary = json.loads(inventory_completed.stdout)
                summary["inventory_rebuilt"] = True
                summary["inventory_record_count"] = (
                    inventory_summary.get("record_model") or {}
                ).get("displayed_total")
        except Exception:
            if previous_archive is None:
                archive_path.unlink(missing_ok=True)
            else:
                archive_path.parent.mkdir(parents=True, exist_ok=True)
                archive_path.write_bytes(previous_archive)
            if previous_projection is None:
                projection_path.unlink(missing_ok=True)
            else:
                projection_path.parent.mkdir(parents=True, exist_ok=True)
                projection_path.write_bytes(previous_projection)
            if rebuild_canonical:
                restore_canonical_outputs(snapshot_root, canonical_before)
            raise
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fold durable OpenNana approvals into the private internal archive lane (dry-run by default)."
    )
    parser.add_argument("--pending", type=Path, action="append", default=[])
    parser.add_argument("--applied", type=Path, action="append", default=[])
    parser.add_argument("--archive-output", type=Path, default=ARCHIVE_PATH)
    parser.add_argument("--projection-output", type=Path, default=PROJECTION_PATH)
    parser.add_argument("--no-canonical-rebuild", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        summary = build(
            pending_paths=args.pending,
            applied_paths=args.applied,
            archive_path=args.archive_output,
            projection_path=args.projection_output,
            apply=args.apply,
            rebuild_canonical=not args.no_canonical_rebuild,
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc), "public_release_effect": False}, ensure_ascii=False))
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
