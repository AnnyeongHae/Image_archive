"""Prepare an immutable private deployment boundary; never deploy or infer.

Only a verified committed approval is exported. The public/static asset tree,
SQLite, source media, vector caches and credentials are never copied. The
separate local media plan is NOT an upload permission or a public release.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from .approval_handoff import HandoffError, _committed, _require_latest, _validate_commit
from .approved_library import build_prompt_catalog, project_approved_library
from .incremental_workflow import load_frozen_workflow
from .rights import build_rights_catalog

SCHEMA = "image-private-library-bundle-1"
OUTPUT = "data/private-research/image-rag-admin/deployment-bundles"
MAX_BUNDLE_BYTES = 8 * 1024 * 1024


def encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def build_bundle(root: Path, db_path: Path, run_id: str, *, expected_commit_id: str | None = None):
    """Read-only assembly. Full prompts and groups retain their source hashes."""
    root = Path(root).resolve()
    data = _committed(db_path, run_id)
    if not data["commit"]:
        raise HandoffError("A committed human approval is required")
    commit = data["commit"]
    if expected_commit_id is not None and commit["id"] != expected_commit_id:
        raise HandoffError("Requested commit is not the latest approval")
    spec = load_frozen_workflow(root, run_id)
    normalized = _validate_commit(spec, data)
    prompts = build_prompt_catalog(root, spec)
    rights = build_rights_catalog(root, spec)
    gallery = {"run_id": run_id, "commit_id": commit["id"], "revision": commit["revision"],
               "decisions_sha256": commit["decisions_sha256"], "items": data["front"]["items"],
               "groups": data["groups"]["groups"], "retained_ids": normalized["stage2_overlay"]["active_ids"]}
    library = project_approved_library(gallery, prompts, include_prompt_text=True)
    originals = {item["id"]: item for item in spec["items"]}
    directory = root / f"data/private-research/image-rag-canary/runs/{run_id}/group-workflow-v1"
    portable, media = [], {}
    for item in library["items"]:
        ident = item["id"]
        source = originals[ident]
        path = (directory / source["prepared_path"]).resolve()
        if not path.is_relative_to(root) or path.suffix != ".png":
            raise HandoffError("Unsafe prepared preview path")
        raw = path.read_bytes()
        sha = digest(raw)
        if sha != source["prepared_sha256"]:
            raise HandoffError("Prepared preview identity changed")
        key = f"private-library/media/{sha}.png"
        media[key] = {"key": key, "sha256": sha, "bytes": len(raw), "content_type": "image/png",
                      "local_source_path": path.relative_to(root).as_posix()}
        # Explicit allowlist, not a recursive local-path/secret removal heuristic.
        portable.append({"id": ident, "style_id": item["style_id"], "media_key": key,
                         "media_sha256": sha, "source_sha256": source["source_sha256"],
                         "memo_text": item.get("memo_text", ""), "tags_texts": item.get("tags_texts", []),
                         "original_prompt": item["original_prompt"], "rights_display": rights[ident],
                         "release_eligible": False, "public_rights_approved": False})
    library["items"] = portable
    bundle = {"schema_version": SCHEMA, "visibility": "private_access_only", "source_commit": commit,
              "library": library, "release_eligible": False, "public_rights_approved": False,
              "mutation_enabled": False, "provider_calls": 0}
    blob = encode(bundle)
    if len(blob) > MAX_BUNDLE_BYTES:
        raise HandoffError("Private canary bundle exceeds 8 MiB; shard before deployment")
    media_plan = {"schema_version": "image-private-media-plan-1", "source_commit_id": commit["id"],
                  "library_sha256": digest(blob), "items": sorted(media.values(), key=lambda item: item["key"]),
                  "upload_authorized": False, "public_release_authorized": False}
    _require_latest(db_path, run_id, commit["id"])
    return bundle, media_plan


def prepare_private_bundle(root: Path, db_path: Path, run_id: str, *, apply: bool = False,
                           expected_commit_id: str | None = None) -> dict:
    root = Path(root).resolve()
    bundle, media = build_bundle(root, db_path, run_id, expected_commit_id=expected_commit_id)
    blob, media_blob = encode(bundle), encode(media)
    sha = digest(blob)
    destination = (root / OUTPUT / sha).resolve()
    if not destination.is_relative_to(root / OUTPUT):
        raise HandoffError("Bundle destination escaped its private root")
    receipt = {"schema_version": "image-private-library-receipt-1", "library_sha256": sha,
               "media_plan_sha256": digest(media_blob), "source_commit_id": bundle["source_commit"]["id"],
               "r2_library_key": f"private-library/snapshots/{sha}.json", "deploy_enabled": False,
               "provider_calls": 0, "external_writes": 0}
    files = {"library.json": blob, "media-plan.local.json": media_blob, "receipt.json": encode(receipt)}
    summary = {**receipt, "status": "dry_run", "bundle_path": destination.relative_to(root).as_posix(),
               "library_bytes": len(blob), "media_objects": len(media["items"]),
               "media_bytes": sum(item["bytes"] for item in media["items"]), "counts": bundle["library"]["counts"]}
    if destination.exists():
        if {path.name for path in destination.iterdir()} != set(files):
            raise HandoffError("Existing private bundle has unexpected files")
        for name, content in files.items():
            if (destination / name).read_bytes() != content:
                raise HandoffError("Existing private bundle differs; never overwrite")
        _require_latest(db_path, run_id, bundle["source_commit"]["id"])
        return {**summary, "status": "unchanged"}
    if not apply:
        return summary
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Retain restrictive tempfile permissions; no broad ACL inheritance rewrite.
    temporary = Path(tempfile.mkdtemp(prefix=".bundle-", dir=destination.parent))
    try:
        for name, content in files.items():
            with (temporary / name).open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        _require_latest(db_path, run_id, bundle["source_commit"]["id"])
        temporary.rename(destination)
    finally:
        # Remove only our three possible temporary files, not a recursive tree.
        if temporary.exists():
            for name in files:
                (temporary / name).unlink(missing_ok=True)
            temporary.rmdir()
    _require_latest(db_path, run_id, bundle["source_commit"]["id"])
    return {**summary, "status": "prepared"}
