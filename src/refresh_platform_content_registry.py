#!/usr/bin/env python3
"""Refresh non-null hashes in the platform content registry.

Dry-run is the default.  A null ``artifact_sha256`` is intentionally preserved
for self-updating reports such as ``qa/latest_validation.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PLATFORM_ROOT.parent.resolve()
REGISTRY_PATH = PLATFORM_ROOT / "data" / "canonical" / "content_registry.json"
DYNAMIC_NULL_HASH_IDS = {"IMG-ARCHIVE-STRUCTURE-QA-001"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    items = payload.get("items")
    if not isinstance(items, list):
        raise SystemExit("content registry has no items array")

    changed: list[dict[str, str]] = []
    skipped_null: list[str] = []
    errors: list[dict[str, str]] = []
    for item in items:
        content_id = str(item.get("content_id") or "")
        artifact_value = str(item.get("artifact_path") or "")
        if not artifact_value:
            errors.append({"content_id": content_id, "error": "missing artifact_path"})
            continue
        artifact_path = (PLATFORM_ROOT / artifact_value).resolve()
        try:
            artifact_path.relative_to(WORKSPACE_ROOT)
        except ValueError:
            errors.append({"content_id": content_id, "error": "artifact escapes workspace"})
            continue
        if not artifact_path.is_file():
            errors.append({"content_id": content_id, "error": "artifact not found"})
            continue
        if item.get("artifact_sha256") is None and content_id in DYNAMIC_NULL_HASH_IDS:
            skipped_null.append(content_id)
            continue
        current_hash = sha256_file(artifact_path)
        prior_hash = str(item.get("artifact_sha256") or "")
        if current_hash != prior_hash:
            changed.append(
                {
                    "content_id": content_id,
                    "prior_sha256": prior_hash,
                    "current_sha256": current_hash,
                }
            )
            item["artifact_sha256"] = current_hash

    result = {
        "mode": "apply" if args.apply else "dry_run",
        "item_count": len(items),
        "changed_count": len(changed),
        "skipped_null": skipped_null,
        "errors": errors,
        "changed": changed,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if errors:
        return 1
    if args.apply and changed:
        REGISTRY_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
