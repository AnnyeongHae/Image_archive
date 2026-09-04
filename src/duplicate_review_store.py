from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INDEX_PATH = (
    PLATFORM_ROOT
    / "data"
    / "private-research"
    / "duplicate-analysis"
    / "current"
    / "duplicate_index.sqlite3"
)
INDEX_SCHEMA_VERSION = "archive-duplicate-index-1.1"
GROUP_KINDS = {
    "exact_prompt_media",
    "exact_media",
    "same_media_variant",
    "exact_prompt",
    "same_prompt_variant",
    "perceptual_candidate",
}
SORT_SQL = {
    "size_desc": "member_count DESC, group_id ASC",
    "members_desc": "member_count DESC, group_id ASC",
    "size_asc": "member_count ASC, group_id ASC",
    "score_desc": "COALESCE(similarity_score, -1) DESC, member_count DESC, group_id ASC",
    "similarity": "COALESCE(similarity_score, -1) DESC, member_count DESC, group_id ASC",
    "kind": "kind ASC, member_count DESC, group_id ASC",
    "group_id": "group_id ASC",
    "group_id_asc": "group_id ASC",
}
WINDOWS_PATH_RE = re.compile(r"^[a-zA-Z]:[\\/]")


class DuplicateIndexUnavailable(RuntimeError):
    """The generated read-only duplicate index is missing or invalid."""


class DuplicateGroupNotFound(KeyError):
    """The requested duplicate group does not exist in the current snapshot."""


def _bounded_page(limit: int, offset: int) -> tuple[int, int]:
    try:
        parsed_limit = int(limit)
        parsed_offset = int(offset)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit and offset must be integers") from exc
    return min(100, max(1, parsed_limit)), max(0, parsed_offset)


def _decode_json_list(value: Any) -> list[str]:
    try:
        decoded = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(decoded, list):
        return []
    return [str(item) for item in decoded if isinstance(item, str)]


def _assert_public_payload(value: Any, key: str = "root") -> None:
    if isinstance(value, dict):
        forbidden = {"path", "filesystem_path", "original_uri", "prompt", "prompt_text", "raw_prompt"}
        for child_key, child in value.items():
            if str(child_key).casefold() in forbidden:
                raise DuplicateIndexUnavailable(f"unsafe field in duplicate response: {child_key}")
            _assert_public_payload(child, str(child_key))
        return
    if isinstance(value, list):
        for child in value:
            _assert_public_payload(child, key)
        return
    if not isinstance(value, str):
        return
    folded = value.strip().casefold()
    if folded.startswith(("data:", "file:", "\\\\")) or WINDOWS_PATH_RE.match(value.strip()):
        raise DuplicateIndexUnavailable(f"unsafe value in duplicate response: {key}")
    if "base64," in folded:
        raise DuplicateIndexUnavailable(f"base64 value in duplicate response: {key}")
    if key in {"thumbnail_uri", "thumbnail_uris"}:
        if value and not value.startswith("/media/derived/duplicate-review/"):
            raise DuplicateIndexUnavailable("thumbnail URI escaped the derived review boundary")


class DuplicateGroupStore:
    def __init__(self, index_path: str | Path = DEFAULT_INDEX_PATH):
        self.index_path = Path(index_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if not self.index_path.is_file():
            raise DuplicateIndexUnavailable("duplicate index is not available")
        connection: sqlite3.Connection | None = None
        try:
            uri = self.index_path.resolve().as_uri() + "?mode=ro&immutable=1"
            connection = sqlite3.connect(uri, uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            row = connection.execute(
                "SELECT value_json FROM meta WHERE key='index_schema_version'"
            ).fetchone()
            if row is None or json.loads(row["value_json"]) != INDEX_SCHEMA_VERSION:
                raise DuplicateIndexUnavailable("duplicate index schema is unsupported")
            yield connection
        except DuplicateIndexUnavailable:
            raise
        except (sqlite3.Error, OSError, json.JSONDecodeError) as exc:
            raise DuplicateIndexUnavailable("duplicate index is unreadable") from exc
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _group_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "group_id": row["group_id"],
            "kind": row["kind"],
            "member_count": row["member_count"],
            "display_title": row["display_title"],
            "exact_sha256": row["exact_sha256"],
            "phash_distance": row["phash_distance"],
            "dhash_distance": row["dhash_distance"],
            "similarity_score": row["similarity_score"],
            "lanes": _decode_json_list(row["lanes_json"]),
            "sources": _decode_json_list(row["sources_json"]),
            "thumbnail_uris": _decode_json_list(row["thumbnail_uris_json"]),
            "recommendation": json.loads(row["recommendation_json"] or "{}"),
        }

    def summary(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT value_json FROM meta WHERE key='public_summary'").fetchone()
            if row is None:
                raise DuplicateIndexUnavailable("duplicate summary is missing")
            try:
                payload = json.loads(row["value_json"])
            except json.JSONDecodeError as exc:
                raise DuplicateIndexUnavailable("duplicate summary is invalid") from exc
        if not isinstance(payload, dict):
            raise DuplicateIndexUnavailable("duplicate summary must be an object")
        artifacts = payload.setdefault("artifacts", {})
        sqlite_meta = artifacts.setdefault("sqlite", {})
        sqlite_meta["bytes"] = self.index_path.stat().st_size
        sqlite_meta.pop("path", None)
        _assert_public_payload(payload)
        return payload

    def list_groups(
        self,
        kind: str | None = None,
        limit: int = 20,
        offset: int = 0,
        q: str | None = None,
        sort: str = "size_desc",
    ) -> dict[str, Any]:
        page_limit, page_offset = _bounded_page(limit, offset)
        normalized_kind = None if kind in (None, "", "all") else str(kind)
        if normalized_kind is not None and normalized_kind not in GROUP_KINDS:
            raise ValueError(f"unsupported duplicate kind: {normalized_kind}")
        if sort not in SORT_SQL:
            raise ValueError(f"unsupported duplicate sort: {sort}")
        query = " ".join(str(q or "").split()).casefold()[:200]
        where: list[str] = []
        params: list[Any] = []
        if normalized_kind is not None:
            where.append("kind = ?")
            params.append(normalized_kind)
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("search_text LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
        where_sql = f" WHERE {' AND '.join(where)}" if where else ""
        with self._connect() as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM groups" + where_sql,
                params,
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT group_id,kind,member_count,display_title,exact_sha256,phash_distance,"
                "dhash_distance,similarity_score,lanes_json,sources_json,thumbnail_uris_json,recommendation_json "
                "FROM groups"
                + where_sql
                + f" ORDER BY {SORT_SQL[sort]} LIMIT ? OFFSET ?",
                [*params, page_limit, page_offset],
            ).fetchall()
        payload = {
            "schema_version": "duplicate-group-list-1.0",
            "kind": normalized_kind or "all",
            "limit": page_limit,
            "offset": page_offset,
            "total": total,
            "groups": [self._group_row(row) for row in rows],
        }
        _assert_public_payload(payload)
        return payload

    def group_detail(self, group_id: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        page_limit, page_offset = _bounded_page(limit, offset)
        normalized_id = str(group_id or "").strip()
        if not normalized_id or len(normalized_id) > 160:
            raise DuplicateGroupNotFound(normalized_id)
        with self._connect() as connection:
            group_row = connection.execute(
                "SELECT group_id,kind,member_count,display_title,exact_sha256,phash_distance,"
                "dhash_distance,similarity_score,lanes_json,sources_json,thumbnail_uris_json,recommendation_json "
                "FROM groups WHERE group_id=?",
                (normalized_id,),
            ).fetchone()
            if group_row is None:
                raise DuplicateGroupNotFound(normalized_id)
            rows = connection.execute(
                "SELECT gm.member_id,r.catalog_key,r.record_id,r.style_id,r.lane,r.title,r.source_name,"
                "r.source_url,r.rights_status,r.review_status,r.prompt_sha256,a.asset_sha256,a.phash,a.dhash,"
                "a.width,a.height,a.byte_size,a.thumbnail_uri "
                "FROM group_members gm JOIN records r ON r.catalog_key=gm.catalog_key "
                "LEFT JOIN assets a ON a.asset_id=gm.asset_id "
                "WHERE gm.group_id=? ORDER BY gm.ordinal LIMIT ? OFFSET ?",
                (normalized_id, page_limit, page_offset),
            ).fetchall()
        members = [
            {
                "member_id": row["member_id"],
                "catalog_key": row["catalog_key"],
                "record_id": row["record_id"],
                "style_id": row["style_id"],
                "lane": row["lane"],
                "title": row["title"],
                "source_name": row["source_name"],
                "source_url": row["source_url"],
                "rights_status": row["rights_status"],
                "review_status": row["review_status"],
                "prompt_sha256": row["prompt_sha256"],
                "asset_sha256": row["asset_sha256"],
                "phash": row["phash"],
                "dhash": row["dhash"],
                "width": row["width"],
                "height": row["height"],
                "byte_size": row["byte_size"],
                "thumbnail_uri": row["thumbnail_uri"],
                "is_recommended_primary": False,
            }
            for row in rows
        ]
        recommendation = self._group_row(group_row).get("recommendation") or {}
        primary_member_id = recommendation.get("recommended_primary_member_id")
        for member in members:
            member["is_recommended_primary"] = bool(
                primary_member_id and member["member_id"] == primary_member_id
            )
        payload = {
            "schema_version": "duplicate-group-detail-1.0",
            "group": {
                **self._group_row(group_row),
                "recommendation": recommendation,
            },
            "limit": page_limit,
            "offset": page_offset,
            "total": group_row["member_count"],
            "members": members,
        }
        _assert_public_payload(payload)
        return payload
