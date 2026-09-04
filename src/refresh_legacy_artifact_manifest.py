#!/usr/bin/env python3
"""Refresh only the legacy artifact inventory; dry-run by default."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "legacy" / "current_archive"
MANIFEST = LEGACY / "artifact_manifest.json"
EXCLUDED_PREFIXES = (
    "assets/images/",
    "tools/__pycache__/",
    "upstream_snapshot/buluslan-gpt-image2-ecommerce-a3673fb/.git/",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_records() -> list[dict]:
    records = []
    for path in sorted((item for item in LEGACY.rglob("*") if item.is_file()), key=lambda item: item.as_posix()):
        relative = path.relative_to(LEGACY).as_posix()
        if relative == "artifact_manifest.json" or relative.startswith(EXCLUDED_PREFIXES):
            continue
        records.append({"path": relative, "bytes": path.stat().st_size, "sha256": sha256(path)})
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    records = build_records()
    previous = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.is_file() else {"files": []}
    previous_by_path = {item["path"]: item for item in previous.get("files", [])}
    current_by_path = {item["path"]: item for item in records}
    changed = sorted(
        path for path in set(previous_by_path) | set(current_by_path)
        if previous_by_path.get(path) != current_by_path.get(path)
    )
    summary = {"mode": "apply" if args.apply else "dry_run", "file_count": len(records), "changed_count": len(changed), "changed_sample": changed[:30]}
    if not args.apply:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(), "files": records}
    temporary = MANIFEST.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(MANIFEST)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
