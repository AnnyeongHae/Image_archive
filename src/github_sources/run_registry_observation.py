#!/usr/bin/env python3
"""Observe every enabled GitHub source as a metadata-only daily snapshot.

This job intentionally does not download prompt bodies or images and does not
write Neon/canonical state.  The immutable repository commit and Git blob SHAs
make a later DB ingest idempotent once that adapter is reviewed.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from .collect_public_repo import (
        DATA_ROOT,
        DEFAULT_REGISTRY,
        MAX_CANDIDATES,
        PacedAPI,
        build_result,
        live_fixture,
        normalize_repository,
        read_json,
        stable_json,
        utc_now,
        write_json_atomic,
    )
except ImportError:
    from collect_public_repo import (
        DATA_ROOT,
        DEFAULT_REGISTRY,
        MAX_CANDIDATES,
        PacedAPI,
        build_result,
        live_fixture,
        normalize_repository,
        read_json,
        stable_json,
        utc_now,
        write_json_atomic,
    )


def enabled_sources(registry: dict[str, Any]) -> list[dict[str, Any]]:
    if (registry.get("policy") or {}).get("allowlist_only") is not True:
        raise ValueError("registry must remain allowlist_only")
    rows = [row for row in registry.get("sources") or [] if isinstance(row, dict) and row.get("enabled") is True]
    rows.sort(key=lambda row: normalize_repository(str(row.get("repository") or "")))
    if not rows:
        raise ValueError("no enabled GitHub sources")
    return rows


def observe_registry(registry: dict[str, Any], *, limit_per_source: int) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    api = PacedAPI()
    blob_occurrences: Counter[tuple[str, str]] = Counter()
    for source in enabled_sources(registry):
        repository = normalize_repository(str(source["repository"]))
        fixture, headers = live_fixture(repository, get=api)
        report = build_result(source, fixture, mode="daily_live_metadata_observation", headers=headers, limit=limit_per_source)
        reports.append(report)
        for candidate in report.get("candidates") or []:
            blob_occurrences[(str(candidate.get("kind")), str(candidate.get("git_blob_sha1")))] += 1
    duplicate_aliases = sum(count - 1 for count in blob_occurrences.values() if count > 1)
    return {
        "schema_version": "image-archive-github-daily-observation-1.0",
        "observed_at": utc_now(),
        "mode": "metadata_only_no_promotion",
        "source_count": len(reports),
        "candidate_count": sum(int((report.get("counts") or {}).get("unique_candidate_count") or 0) for report in reports),
        "cross_source_blob_alias_count": duplicate_aliases,
        "rights_policy": {
            "repository_license_is_item_rights_clearance": False,
            "download_prompt_bodies": False,
            "download_images": False,
            "canonical_promotion": False,
            "public_release": False,
        },
        "sources": reports,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--limit-per-source", type=int, default=MAX_CANDIDATES)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--fetch", action="store_true", help="Required explicit live-network gate")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.fetch:
        raise ValueError("--fetch is required for a live registry observation")
    if not 1 <= args.limit_per_source <= MAX_CANDIDATES:
        raise ValueError(f"--limit-per-source must be between 1 and {MAX_CANDIDATES}")
    registry = read_json(args.registry)
    result = observe_registry(registry, limit_per_source=args.limit_per_source)
    write_json_atomic(args.report, result)
    if not args.quiet:
        print(stable_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
