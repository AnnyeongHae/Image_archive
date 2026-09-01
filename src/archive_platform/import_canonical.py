from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

from .db import PLATFORM_ROOT, connect


DEFAULT_CANONICAL = PLATFORM_ROOT / "data" / "canonical" / "archive_records.jsonl"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def records(path: Path, *, limit: int | None) -> Iterator[dict[str, Any]]:
    yielded = 0
    with path.open(encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError(f"canonical line {line_number} is not an object")
            yield payload
            yielded += 1
            if limit is not None and yielded >= limit:
                break


def _private_values(record: dict[str, Any], batch_id: str) -> tuple[Any, ...]:
    prompt = record.get("prompt") or {}
    source = record.get("source") or {}
    rights = record.get("rights") or {}
    return (
        record["catalog_key"],
        record["schema_version"],
        record["lane"],
        record["record_id"],
        record["style_id"],
        record.get("parent_style_id"),
        record["title"],
        record["content_sha256"],
        prompt.get("sha256"),
        prompt.get("text"),
        prompt.get("language"),
        prompt.get("format"),
        source.get("name"),
        source.get("url"),
        source.get("type"),
        source.get("repository"),
        source.get("commit"),
        rights.get("rights_tier"),
        rights.get("portfolio_visibility"),
        rights.get("admin_usage_status"),
        bool(rights.get("public_metadata_eligible")),
        bool(rights.get("prompt_publication_eligible")),
        bool(rights.get("media_publication_eligible")),
        bool(rights.get("release_eligible")),
        json.dumps(source, ensure_ascii=False),
        json.dumps(record.get("license") or {}, ensure_ascii=False),
        json.dumps(rights, ensure_ascii=False),
        json.dumps(record.get("taxonomy") or {}, ensure_ascii=False),
        json.dumps(record.get("generation") or {}, ensure_ascii=False),
        json.dumps(record.get("review_release") or {}, ensure_ascii=False),
        json.dumps(record.get("provenance") or {}, ensure_ascii=False),
        batch_id,
    )


PRIVATE_UPSERT = """
INSERT INTO image_archive.archive_records_private (
    catalog_key, schema_version, lane, record_id, style_id, parent_style_id, title,
    content_sha256, prompt_sha256, prompt_text, prompt_language, prompt_format,
    source_name, source_url, source_type, source_repository, source_commit,
    rights_tier, portfolio_visibility, admin_usage_status,
    public_metadata_eligible, prompt_publication_eligible,
    media_publication_eligible, release_eligible,
    source_payload, license_payload, rights_payload, taxonomy_payload,
    generation_payload, review_payload, provenance_payload, import_batch_id
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
    %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s
)
ON CONFLICT (catalog_key) DO UPDATE SET
    schema_version = EXCLUDED.schema_version,
    lane = EXCLUDED.lane,
    record_id = EXCLUDED.record_id,
    style_id = EXCLUDED.style_id,
    parent_style_id = EXCLUDED.parent_style_id,
    title = EXCLUDED.title,
    content_sha256 = EXCLUDED.content_sha256,
    prompt_sha256 = EXCLUDED.prompt_sha256,
    prompt_text = EXCLUDED.prompt_text,
    prompt_language = EXCLUDED.prompt_language,
    prompt_format = EXCLUDED.prompt_format,
    source_name = EXCLUDED.source_name,
    source_url = EXCLUDED.source_url,
    source_type = EXCLUDED.source_type,
    source_repository = EXCLUDED.source_repository,
    source_commit = EXCLUDED.source_commit,
    rights_tier = EXCLUDED.rights_tier,
    portfolio_visibility = EXCLUDED.portfolio_visibility,
    admin_usage_status = EXCLUDED.admin_usage_status,
    public_metadata_eligible = EXCLUDED.public_metadata_eligible,
    prompt_publication_eligible = EXCLUDED.prompt_publication_eligible,
    media_publication_eligible = EXCLUDED.media_publication_eligible,
    release_eligible = EXCLUDED.release_eligible,
    source_payload = EXCLUDED.source_payload,
    license_payload = EXCLUDED.license_payload,
    rights_payload = EXCLUDED.rights_payload,
    taxonomy_payload = EXCLUDED.taxonomy_payload,
    generation_payload = EXCLUDED.generation_payload,
    review_payload = EXCLUDED.review_payload,
    provenance_payload = EXCLUDED.provenance_payload,
    import_batch_id = EXCLUDED.import_batch_id,
    updated_at = now()
WHERE image_archive.archive_records_private.content_sha256 IS DISTINCT FROM EXCLUDED.content_sha256
"""


MEDIA_INSERT = """
INSERT INTO image_archive.archive_media_private (
    catalog_key, asset_ordinal, role, uri, private_path, uri_kind, origin, sha256,
    mime_type, width, height, generated_staging, release_eligible
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def _public_projection(record: dict[str, Any]) -> dict[str, Any] | None:
    rights = record.get("rights") or {}
    if rights.get("rights_tier") not in {"P1", "P2"} or not rights.get("public_metadata_eligible"):
        return None
    from src.build_canonical_archive import public_record

    return public_record(record)


def _upsert_record(cursor, record: dict[str, Any], batch_id: str) -> int:
    cursor.execute(PRIVATE_UPSERT, _private_values(record, batch_id))
    cursor.execute(
        "DELETE FROM image_archive.archive_media_private WHERE catalog_key = %s",
        (record["catalog_key"],),
    )
    media_written = 0
    for ordinal, asset in enumerate((record.get("media") or {}).get("assets") or []):
        if not isinstance(asset, dict):
            continue
        uri = asset.get("uri")
        if isinstance(uri, str) and uri.startswith("data:"):
            raise ValueError(f"inline base64 media is forbidden: {record['catalog_key']}")
        cursor.execute(
            MEDIA_INSERT,
            (
                record["catalog_key"], ordinal, asset.get("role"), uri,
                asset.get("private_path"), asset.get("uri_kind"), asset.get("origin"),
                asset.get("sha256"), asset.get("mime_type"), asset.get("width"),
                asset.get("height"), bool(asset.get("generated_staging")),
                bool(asset.get("release_eligible")),
            ),
        )
        media_written += 1
    public = _public_projection(record)
    if public is None:
        cursor.execute(
            "DELETE FROM image_archive.archive_records_public WHERE catalog_key = %s",
            (record["catalog_key"],),
        )
    else:
        cursor.execute(
            """
            INSERT INTO image_archive.archive_records_public (
                catalog_key, style_id, title, rights_tier, source_name, source_url,
                public_dto, content_sha256
            ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (catalog_key) DO UPDATE SET
                style_id = EXCLUDED.style_id,
                title = EXCLUDED.title,
                rights_tier = EXCLUDED.rights_tier,
                source_name = EXCLUDED.source_name,
                source_url = EXCLUDED.source_url,
                public_dto = EXCLUDED.public_dto,
                content_sha256 = EXCLUDED.content_sha256,
                updated_at = now()
            """,
            (
                public["catalog_key"], public["style_id"], public["title"],
                public["rights"]["rights_tier"], public["source"].get("name"),
                public["source"].get("url"), json.dumps(public, ensure_ascii=False),
                public["content_sha256"],
            ),
        )
    return media_written


def dry_run(path: Path, *, limit: int | None) -> dict[str, Any]:
    count = 0
    public_count = 0
    media_count = 0
    tier_counts: dict[str, int] = {}
    for record in records(path, limit=limit):
        count += 1
        tier = str((record.get("rights") or {}).get("rights_tier") or "unknown")
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        media_count += len((record.get("media") or {}).get("assets") or [])
        public_count += int(_public_projection(record) is not None)
    return {
        "mode": "dry_run",
        "source_path": str(path),
        "requested_limit": limit,
        "records_seen": count,
        "public_projection_records": public_count,
        "private_only_records": count - public_count,
        "media_rows": media_count,
        "rights_tiers": tier_counts,
    }


def apply_import(path: Path, *, limit: int | None, dsn: str | None = None) -> dict[str, Any]:
    manifest_sha = file_sha256(path)
    limit_label = "all" if limit is None else str(limit)
    batch_id = f"canonical-{manifest_sha[:16]}-{limit_label}"
    seen = 0
    media_written = 0
    public_count = 0
    with connect(dsn=dsn) as connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT status, source_manifest_sha256 FROM image_archive.import_batches WHERE batch_id = %s",
                    (batch_id,),
                )
                existing = cursor.fetchone()
                if existing and existing[0] == "completed" and existing[1] == manifest_sha:
                    connection.rollback()
                    return {"mode": "apply", "status": "already_completed", "batch_id": batch_id}
                cursor.execute(
                    """
                    INSERT INTO image_archive.import_batches (
                        batch_id, source_manifest_sha256, source_path, requested_limit, status
                    ) VALUES (%s, %s, %s, %s, 'running')
                    ON CONFLICT (batch_id) DO UPDATE SET
                        status = 'running', started_at = now(), finished_at = NULL, error_code = NULL
                    """,
                    (batch_id, manifest_sha, str(path), limit),
                )
                for record in records(path, limit=limit):
                    media_written += _upsert_record(cursor, record, batch_id)
                    public_count += int(_public_projection(record) is not None)
                    seen += 1
                cursor.execute(
                    """
                    UPDATE image_archive.import_batches
                    SET status = 'completed', records_seen = %s, records_written = %s,
                        media_written = %s, finished_at = now()
                    WHERE batch_id = %s
                    """,
                    (seen, seen, media_written, batch_id),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {
        "mode": "apply",
        "status": "completed",
        "batch_id": batch_id,
        "records_written": seen,
        "public_projection_records": public_count,
        "private_only_records": seen - public_count,
        "media_written": media_written,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stream canonical JSONL into Neon (50-row dry-run by default).")
    parser.add_argument("--source", type=Path, default=DEFAULT_CANONICAL)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--all", action="store_true", help="Process the full canonical archive")
    parser.add_argument("--apply", action="store_true", help="Write transactionally to Neon")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    limit = None if args.all else args.limit
    result = apply_import(args.source, limit=limit) if args.apply else dry_run(args.source, limit=limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
