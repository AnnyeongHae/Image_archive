from __future__ import annotations

import base64
import json
import re
from typing import Any

from .db import connect


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def encode_cursor(catalog_key: str | None) -> str | None:
    if not catalog_key:
        return None
    payload = json.dumps({"catalog_key": catalog_key}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(value: str | None) -> str | None:
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        key = payload["catalog_key"]
    except Exception as exc:
        raise ValueError("invalid_cursor") from exc
    if not isinstance(key, str) or not key:
        raise ValueError("invalid_cursor")
    return key


class ArchiveStore:
    def __init__(self, *, dsn: str | None = None):
        self.dsn = dsn

    def ready(self) -> bool:
        with connect(dsn=self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM image_archive.schema_migrations LIMIT 1")
                cursor.fetchone()
        return True

    def list_public(self, *, q: str | None, cursor: str | None, limit: int) -> dict[str, Any]:
        after = decode_cursor(cursor)
        clauses = ["(%s IS NULL OR catalog_key > %s)"]
        params: list[Any] = [after, after]
        if q:
            clauses.append(
                "to_tsvector('simple'::regconfig, COALESCE(style_id, '') || ' ' || COALESCE(title, '') || ' ' || COALESCE(public_dto ->> 'search_text', '')) @@ plainto_tsquery('simple'::regconfig, %s)"
            )
            params.append(q)
        params.append(limit + 1)
        sql = f"""
            SELECT catalog_key, public_dto
            FROM image_archive.archive_records_public
            WHERE {' AND '.join(clauses)}
            ORDER BY catalog_key
            LIMIT %s
        """
        with connect(dsn=self.dsn) as connection:
            with connection.cursor() as db_cursor:
                db_cursor.execute(sql, params)
                rows = db_cursor.fetchall()
        has_more = len(rows) > limit
        visible = rows[:limit]
        return {
            "items": [row[1] for row in visible],
            "next_cursor": encode_cursor(visible[-1][0]) if has_more and visible else None,
        }

    def get_public(self, catalog_key: str) -> dict[str, Any] | None:
        with connect(dsn=self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT public_dto FROM image_archive.archive_records_public WHERE catalog_key = %s",
                    (catalog_key,),
                )
                row = cursor.fetchone()
        return row[0] if row else None

    def list_private(
        self,
        *,
        q: str | None,
        cursor: str | None,
        limit: int,
        include_quarantine: bool,
    ) -> dict[str, Any]:
        after = decode_cursor(cursor)
        clauses = ["(%s IS NULL OR catalog_key > %s)"]
        params: list[Any] = [after, after]
        if not include_quarantine:
            clauses.append("rights_tier <> 'P4'")
        if q:
            clauses.append(
                "to_tsvector('simple'::regconfig, COALESCE(style_id, '') || ' ' || COALESCE(title, '') || ' ' || COALESCE(prompt_text, '') || ' ' || COALESCE(source_name, '')) @@ plainto_tsquery('simple'::regconfig, %s)"
            )
            params.append(q)
        params.append(limit + 1)
        sql = f"""
            SELECT catalog_key, style_id, title, lane, rights_tier,
                   portfolio_visibility, source_name, source_url,
                   prompt_sha256, prompt_text, taxonomy_payload, rights_payload
            FROM image_archive.archive_records_private
            WHERE {' AND '.join(clauses)}
            ORDER BY catalog_key
            LIMIT %s
        """
        with connect(dsn=self.dsn) as connection:
            with connection.cursor() as db_cursor:
                db_cursor.execute(sql, params)
                rows = db_cursor.fetchall()
        has_more = len(rows) > limit
        visible = rows[:limit]
        items = [
            {
                "catalog_key": row[0], "style_id": row[1], "title": row[2], "lane": row[3],
                "rights_tier": row[4], "portfolio_visibility": row[5],
                "source": {"name": row[6], "url": row[7]},
                "prompt": {"sha256": row[8], "text": row[9]},
                "taxonomy": row[10], "rights": row[11],
            }
            for row in visible
        ]
        return {
            "items": items,
            "next_cursor": encode_cursor(visible[-1][0]) if has_more and visible else None,
        }

    def get_private(self, catalog_key: str) -> dict[str, Any] | None:
        with connect(dsn=self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT catalog_key, style_id, title, lane, record_id, parent_style_id,
                           content_sha256, prompt_sha256, prompt_text, prompt_language,
                           source_payload, license_payload, rights_payload, taxonomy_payload,
                           generation_payload, review_payload, provenance_payload, rights_tier
                    FROM image_archive.archive_records_private
                    WHERE catalog_key = %s
                    """,
                    (catalog_key,),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                cursor.execute(
                    """
                    SELECT asset_ordinal, role, uri, private_path, uri_kind, origin, sha256,
                           mime_type, width, height, generated_staging, release_eligible
                    FROM image_archive.archive_media_private
                    WHERE catalog_key = %s ORDER BY asset_ordinal
                    """,
                    (catalog_key,),
                )
                media_rows = cursor.fetchall()
        return {
            "catalog_key": row[0], "style_id": row[1], "title": row[2], "lane": row[3],
            "record_id": row[4], "parent_style_id": row[5], "content_sha256": row[6],
            "prompt": {"sha256": row[7], "text": row[8], "language": row[9]},
            "source": row[10], "license": row[11], "rights": row[12], "taxonomy": row[13],
            "generation": row[14], "review_release": row[15], "provenance": row[16],
            "rights_tier": row[17],
            "media": [
                {
                    "asset_ordinal": item[0], "role": item[1], "uri": item[2],
                    "private_path": item[3], "uri_kind": item[4], "origin": item[5],
                    "sha256": item[6], "mime_type": item[7], "width": item[8],
                    "height": item[9], "generated_staging": item[10],
                    "release_eligible": item[11],
                }
                for item in media_rows
            ],
        }

    def get_review_draft(self, *, subject: str, queue_revision: str) -> dict[str, Any] | None:
        with connect(dsn=self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT decisions, decision_count, updated_at
                    FROM image_archive.review_drafts
                    WHERE admin_subject = %s AND queue_revision = %s
                    """,
                    (subject, queue_revision),
                )
                row = cursor.fetchone()
        if not row:
            return None
        return {"queue_revision": queue_revision, "decisions": row[0], "decision_count": row[1], "updated_at": row[2].isoformat()}

    def put_review_draft(self, *, subject: str, queue_revision: str, decisions: list[dict[str, Any]]) -> dict[str, Any]:
        if not SHA256_RE.fullmatch(queue_revision):
            raise ValueError("queue_revision_must_be_sha256")
        payload = json.dumps(decisions, ensure_ascii=False)
        with connect(dsn=self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO image_archive.review_drafts (
                        admin_subject, queue_revision, decisions, decision_count
                    ) VALUES (%s, %s, %s::jsonb, %s)
                    ON CONFLICT (admin_subject, queue_revision) DO UPDATE SET
                        decisions = EXCLUDED.decisions,
                        decision_count = EXCLUDED.decision_count,
                        updated_at = now()
                    RETURNING updated_at
                    """,
                    (subject, queue_revision, payload, len(decisions)),
                )
                updated_at = cursor.fetchone()[0]
            connection.commit()
        return {"queue_revision": queue_revision, "decision_count": len(decisions), "updated_at": updated_at.isoformat()}
