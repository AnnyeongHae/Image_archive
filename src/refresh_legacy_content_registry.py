#!/usr/bin/env python3
"""Refresh legacy content-registry hashes after the archive relocation.

The command is dry-run by default.  ``--apply`` changes only
``artifact_sha256`` values for artifact paths that resolve to existing files
inside the marketer workspace.  It does not add, remove, approve, or release
registry records.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE_ROOT = PLATFORM_ROOT.parent
DEFAULT_ARCHIVE_ROOT = PLATFORM_ROOT / "legacy" / "current_archive"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write refreshed hashes")
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=DEFAULT_WORKSPACE_ROOT,
        help="marketer workspace containing 00_CORE and Reports",
    )
    parser.add_argument(
        "--archive-root",
        type=Path,
        default=DEFAULT_ARCHIVE_ROOT,
        help="relocated legacy archive root",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=None,
        help="content registry path; defaults to <archive-root>/content_registry.json",
    )
    return parser.parse_args()


def resolve_artifact(workspace_root: Path, artifact_value: str) -> Path:
    candidate = Path(artifact_value)
    resolved = candidate.resolve() if candidate.is_absolute() else (workspace_root / candidate).resolve()
    try:
        resolved.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError(f"artifact escapes workspace: {artifact_value}") from exc
    return resolved


def main() -> int:
    args = parse_args()
    workspace_root = args.workspace_root.resolve()
    archive_root = args.archive_root.resolve()
    registry_path = (args.registry or (archive_root / "content_registry.json")).resolve()

    if not (workspace_root / "00_CORE").is_dir() or not (workspace_root / "Reports").is_dir():
        raise SystemExit(f"invalid workspace root: {workspace_root}")
    if not (archive_root / "index.html").is_file():
        raise SystemExit(f"invalid legacy archive root: {archive_root}")
    if not registry_path.is_file():
        raise SystemExit(f"missing content registry: {registry_path}")

    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    items = payload.get("items")
    if not isinstance(items, list):
        raise SystemExit("content registry has no items array")

    changed: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []
    invalid: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            invalid.append({"content_id": "", "reason": "item is not an object"})
            continue
        content_id = str(item.get("content_id") or "")
        artifact_value = str(item.get("artifact_path") or "")
        if not artifact_value:
            invalid.append({"content_id": content_id, "reason": "missing artifact_path"})
            continue
        try:
            artifact_path = resolve_artifact(workspace_root, artifact_value)
        except ValueError as exc:
            invalid.append({"content_id": content_id, "reason": str(exc)})
            continue
        if not artifact_path.is_file():
            missing.append({"content_id": content_id, "artifact_path": artifact_value})
            continue
        current_hash = sha256_file(artifact_path)
        prior_hash = str(item.get("artifact_sha256") or "")
        if current_hash != prior_hash:
            changed.append(
                {
                    "content_id": content_id,
                    "artifact_path": artifact_value,
                    "prior_sha256": prior_hash,
                    "current_sha256": current_hash,
                }
            )
            item["artifact_sha256"] = current_hash

    result = {
        "mode": "apply" if args.apply else "dry_run",
        "registry": str(registry_path),
        "item_count": len(items),
        "changed_count": len(changed),
        "missing_count": len(missing),
        "invalid_count": len(invalid),
        "changed": changed,
        "missing": missing,
        "invalid": invalid,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if missing or invalid:
        return 1
    if args.apply and changed:
        registry_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
