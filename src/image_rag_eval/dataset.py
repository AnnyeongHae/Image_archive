from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from build_duplicate_index import BuildConfig, DuplicateBuildError, load_remote_overlay, resolve_local_asset
from .rights import normalize_image_rights


SCHEMA_VERSION = "1"
MAX_LIMIT = 20
INDEX_SCHEMA_VERSION = "archive-duplicate-index-1.1"
ALLOWED_GROUP_KINDS = ("exact_media", "exact_prompt", "perceptual_candidate")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_path(root: Path) -> Path:
    return root / "data" / "canonical" / "archive_records.jsonl"


def _duplicate_index_path(root: Path) -> Path:
    return root / "data" / "private-research" / "duplicate-analysis" / "current" / "duplicate_index.sqlite3"


def _remote_overlay_path(root: Path) -> Path:
    return root / "data" / "private-research" / "remote-media-canary" / "current" / "cache_index.json"


def _normalized_root(root: Path) -> Path:
    resolved = root.resolve()
    if not resolved.is_dir():
        raise FileNotFoundError(f"archive root is missing: {resolved}")
    return resolved


def _validated_limit(limit: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer between 1 and 20") from exc
    if value < 1 or value > MAX_LIMIT:
        raise ValueError("limit must be between 1 and 20")
    return value


def _connect_index(index_path: Path) -> sqlite3.Connection:
    if not index_path.is_file():
        raise FileNotFoundError(f"duplicate index is unavailable: {index_path}")
    connection = sqlite3.connect(index_path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    row = connection.execute("SELECT value_json FROM meta WHERE key='index_schema_version'").fetchone()
    if row is None or json.loads(row["value_json"]) != INDEX_SCHEMA_VERSION:
        connection.close()
        raise ValueError("duplicate index schema is unsupported")
    return connection


def _safe_input_path(root: Path, candidate: Path) -> bool:
    try:
        relative = candidate.resolve().relative_to(root)
    except ValueError:
        return False
    for part in relative.parts:
        folded = part.casefold()
        if folded.startswith("."):
            return False
        if folded == ".env" or folded.startswith(".env."):
            return False
        if "secret" in folded or "secrets" in folded:
            return False
    return True


def _manifest_item(
    *,
    root: Path,
    config: BuildConfig,
    candidate: dict[str, Any],
    raw_record: dict[str, Any],
    overlay: dict[tuple[str, int], dict[str, Any]],
) -> dict[str, Any] | None:
    media = raw_record.get("media") if isinstance(raw_record.get("media"), dict) else {}
    assets = media.get("assets") if isinstance(media.get("assets"), list) else []
    asset_index = int(candidate["asset_index"])
    if asset_index < 0 or asset_index >= len(assets):
        return None
    asset = assets[asset_index]
    if not isinstance(asset, dict):
        return None
    uri_kind = str(asset.get("uri_kind") or "").casefold()
    uri = asset.get("uri")
    if uri_kind == "remote" or (isinstance(uri, str) and uri.casefold().startswith(("http://", "https://", "data:"))):
        overlay_entry = overlay.get((candidate["catalog_key"], asset_index))
        if not overlay_entry:
            return None
        uri = overlay_entry.get("local_original_path")
        if not isinstance(uri, str) or not uri.strip():
            return None
    elif uri_kind != "local":
        return None
    try:
        resolved = resolve_local_asset(uri, config)
    except (DuplicateBuildError, OSError):
        return None
    if not _safe_input_path(root, resolved):
        return None
    relative = resolved.resolve().relative_to(root).as_posix()
    prompt = raw_record.get("prompt") if isinstance(raw_record.get("prompt"), dict) else {}
    prompt_text = prompt.get("text")
    return {
        "id": candidate["asset_id"],
        "style_id": candidate["style_id"],
        "path": relative,
        "prompt": prompt_text if isinstance(prompt_text, str) else "",
        "sha256": candidate["sha256"],
        "review_status": candidate["review_status"],
        "rights_status": candidate["rights_status"],
        "rights_display": normalize_image_rights({**raw_record, "asset_index": asset_index, "sha256": candidate["sha256"]}),
        "external_ai_approved": False,
        "catalog_key": candidate["catalog_key"],
        "record_id": candidate["record_id"],
        "asset_index": asset_index,
        "lane": candidate["lane"],
        "source_name": candidate["source_name"],
        "source_url_sha256": _sha256_text(candidate["source_url"]) if candidate["source_url"] else None,
        "title": candidate["title"],
        "group_seed_kind": candidate.get("group_seed_kind"),
    }


def _group_candidates(connection: sqlite3.Connection, kind: str) -> list[dict[str, Any]]:
    if kind == "exact_prompt":
        rows = connection.execute(
            """
            SELECT g.group_id, gm.ordinal, COALESCE(gm.asset_id, fallback.asset_id) AS asset_id,
                   gm.catalog_key, COALESCE(a.asset_index, fallback.asset_index) AS asset_index,
                   COALESCE(a.asset_sha256, fallback.asset_sha256) AS asset_sha256,
                   r.style_id, r.record_id, r.lane, r.title, r.source_name, r.source_url, r.rights_status, r.review_status
            FROM groups g
            JOIN group_members gm ON gm.group_id = g.group_id
            JOIN records r ON r.catalog_key = gm.catalog_key
            LEFT JOIN assets a ON a.asset_id = gm.asset_id
            LEFT JOIN assets fallback
              ON fallback.catalog_key = gm.catalog_key
             AND fallback.asset_index = (
                 SELECT MIN(asset_index) FROM assets WHERE catalog_key = gm.catalog_key
             )
            WHERE g.kind = ?
              AND COALESCE(gm.asset_id, fallback.asset_id) IS NOT NULL
            ORDER BY g.member_count DESC, g.group_id ASC, gm.ordinal ASC, COALESCE(gm.asset_id, fallback.asset_id) ASC
            """,
            (kind,),
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT g.group_id, gm.ordinal, gm.asset_id, gm.catalog_key, a.asset_index, a.asset_sha256,
                   r.style_id, r.record_id, r.lane, r.title, r.source_name, r.source_url, r.rights_status, r.review_status
            FROM groups g
            JOIN group_members gm ON gm.group_id = g.group_id
            JOIN records r ON r.catalog_key = gm.catalog_key
            LEFT JOIN assets a ON a.asset_id = gm.asset_id
            WHERE g.kind = ? AND gm.asset_id IS NOT NULL
            ORDER BY g.member_count DESC, g.group_id ASC, gm.ordinal ASC, gm.asset_id ASC
            """,
            (kind,),
        ).fetchall()
    return [dict(row) for row in rows]


def _all_asset_candidates(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT a.asset_id, a.catalog_key, a.asset_index, a.asset_sha256,
               r.style_id, r.record_id, r.lane, r.title, r.source_name, r.source_url, r.rights_status, r.review_status
        FROM assets a
        JOIN records r ON r.catalog_key = a.catalog_key
        ORDER BY COALESCE(r.source_name, '') ASC, r.lane ASC, r.catalog_key ASC, a.asset_index ASC, a.asset_id ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _append_candidate(
    ordered: list[dict[str, Any]],
    seen_assets: set[str],
    row: dict[str, Any],
    *,
    group_seed_kind: str | None = None,
) -> bool:
    asset_id = str(row.get("asset_id") or "")
    if not asset_id or asset_id in seen_assets:
        return False
    sha256 = str(row.get("asset_sha256") or "").strip()
    if not sha256:
        return False
    ordered.append(
        {
            "asset_id": asset_id,
            "catalog_key": str(row.get("catalog_key") or ""),
            "record_id": str(row.get("record_id") or ""),
            "style_id": str(row.get("style_id") or ""),
            "lane": str(row.get("lane") or "unknown"),
            "title": str(row.get("title") or ""),
            "source_name": str(row.get("source_name") or ""),
            "source_url": str(row.get("source_url") or ""),
            "rights_status": str(row.get("rights_status") or ""),
            "review_status": str(row.get("review_status") or ""),
            "asset_index": int(row.get("asset_index") or 0),
            "sha256": sha256,
            "group_seed_kind": group_seed_kind,
        }
    )
    seen_assets.add(asset_id)
    return True


def _select_candidates(connection: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    budget = max(limit * 4, 8)
    ordered: list[dict[str, Any]] = []
    seen_assets: set[str] = set()
    seen_records: set[str] = set()

    for kind in ALLOWED_GROUP_KINDS:
        added = 0
        for row in _group_candidates(connection, kind):
            if _append_candidate(ordered, seen_assets, row, group_seed_kind=kind):
                added += 1
                if kind != "perceptual_candidate":
                    seen_records.add(str(row.get("catalog_key") or ""))
            if added == 2:
                break

    pool = _all_asset_candidates(connection)
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in pool:
        asset_id = str(row.get("asset_id") or "")
        if not asset_id or asset_id in seen_assets:
            continue
        bucket = (str(row.get("source_name") or "").casefold(), str(row.get("lane") or "").casefold())
        buckets[bucket].append(row)

    for rows in buckets.values():
        rows.sort(
            key=lambda row: (
                str(row.get("catalog_key") or ""),
                int(row.get("asset_index") or 0),
                str(row.get("asset_id") or ""),
            )
        )

    bucket_keys = sorted(buckets)
    while len(ordered) < budget:
        progress = False
        for bucket in bucket_keys:
            rows = buckets[bucket]
            while rows and str(rows[0].get("catalog_key") or "") in seen_records:
                rows.pop(0)
            if not rows:
                continue
            row = rows.pop(0)
            if _append_candidate(ordered, seen_assets, row):
                seen_records.add(str(row.get("catalog_key") or ""))
                progress = True
            if len(ordered) >= budget:
                break
        if not progress:
            break

    if len(ordered) < budget:
        for row in pool:
            if _append_candidate(ordered, seen_assets, row):
                if len(ordered) >= budget:
                    break

    return ordered


def build_manifest(root: Path, limit: int = 20) -> dict[str, Any]:
    root_resolved = _normalized_root(Path(root))
    bounded_limit = _validated_limit(limit)
    canonical_path = _canonical_path(root_resolved)
    if not canonical_path.is_file():
        raise FileNotFoundError(f"canonical archive is missing: {canonical_path}")
    config = BuildConfig(
        platform_root=root_resolved,
        canonical_path=canonical_path,
        legacy_root=root_resolved / "legacy" / "current_archive",
        remote_overlay_path=_remote_overlay_path(root_resolved),
    )
    overlay = load_remote_overlay(config)
    connection = _connect_index(_duplicate_index_path(root_resolved))
    try:
        candidates = _select_candidates(connection, bounded_limit)
    finally:
        connection.close()

    wanted_by_catalog: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for candidate in candidates:
        wanted_by_catalog[candidate["catalog_key"]][candidate["asset_index"]] = candidate

    resolved_items: dict[str, dict[str, Any]] = {}
    with canonical_path.open("rb") as handle:
        for raw_line in handle:
            if len(resolved_items) == len(candidates):
                break
            if not raw_line.strip():
                continue
            raw = json.loads(raw_line)
            if not isinstance(raw, dict):
                continue
            catalog_key = str(raw.get("catalog_key") or "")
            wanted_assets = wanted_by_catalog.get(catalog_key)
            if not wanted_assets:
                continue
            for asset_index, candidate in wanted_assets.items():
                item = _manifest_item(
                    root=root_resolved,
                    config=config,
                    candidate=candidate,
                    raw_record=raw,
                    overlay=overlay,
                )
                if item is not None:
                    resolved_items[candidate["asset_id"]] = item

    items: list[dict[str, Any]] = []
    for candidate in candidates:
        item = resolved_items.get(candidate["asset_id"])
        if item is None:
            continue
        items.append(item)
        if len(items) == bounded_limit:
            break

    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": _utc_now(),
        "selection_notes": [
            "Read-only local image manifest for bounded similarity canaries.",
            "At most two items are seeded from exact_media groups and at most two from exact_prompt groups when available.",
            "Remaining items are selected deterministically to diversify source, lane, and record coverage.",
            "Only root-relative local raster paths are included; remote-only, hidden, .env, and secret-like inputs are excluded.",
            "external_ai_approved remains false for every item; review, rights, and release approvals are not inferred.",
        ],
        "items": items,
    }
