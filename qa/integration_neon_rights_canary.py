from __future__ import annotations

import json

from src.archive_platform.db import connect


PRIVATE_INSERT = """
INSERT INTO image_archive.archive_records_private (
    catalog_key, schema_version, lane, record_id, style_id, title, content_sha256,
    rights_tier, portfolio_visibility, admin_usage_status, public_metadata_eligible,
    prompt_publication_eligible, media_publication_eligible, release_eligible,
    import_batch_id
) VALUES (%s, 'synthetic-canary-1.0', 'manual', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def public_dto(key: str, tier: str, *, prompt_text: bool = False) -> dict[str, object]:
    prompt: dict[str, object] = {"available_in_public_export": prompt_text}
    if prompt_text:
        prompt["text"] = "synthetic prompt"
    return {
        "catalog_key": key,
        "rights": {"rights_tier": tier},
        "prompt": prompt,
        "media": {"assets": []},
        "search_text": "synthetic",
    }


def main() -> int:
    connection = connect()
    checks = {"p1_valid": False, "p2_valid": False, "p3_blocked": False, "p2_prompt_blocked": False}
    try:
        with connection.cursor() as cursor:
            batch_id = "synthetic-rights-canary"
            cursor.execute(
                """
                INSERT INTO image_archive.import_batches (
                    batch_id, source_manifest_sha256, source_path, status
                ) VALUES (%s, %s, 'synthetic-rollback', 'running')
                ON CONFLICT (batch_id) DO NOTHING
                """,
                (batch_id, "f" * 64),
            )
            fixtures = [
                ("manual:synthetic-p1", "synthetic-p1", "SYN-P1", "Synthetic P1", "1" * 64, "P1", "public", "public_or_metadata", True, True, True, True, batch_id),
                ("manual:synthetic-p2", "synthetic-p2", "SYN-P2", "Synthetic P2", "2" * 64, "P2", "metadata_link_only", "public_or_metadata", True, False, False, False, batch_id),
                ("manual:synthetic-p3", "synthetic-p3", "SYN-P3", "Synthetic P3", "3" * 64, "P3", "admin_only", "reference_allowed", False, False, False, False, batch_id),
            ]
            for fixture in fixtures:
                cursor.execute(PRIVATE_INSERT, fixture)
            for key, style, title, tier, digest, include_prompt in [
                ("manual:synthetic-p1", "SYN-P1", "Synthetic P1", "P1", "a" * 64, True),
                ("manual:synthetic-p2", "SYN-P2", "Synthetic P2", "P2", "b" * 64, False),
            ]:
                cursor.execute(
                    """
                    INSERT INTO image_archive.archive_records_public (
                        catalog_key, style_id, title, rights_tier, public_dto, content_sha256
                    ) VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                    """,
                    (key, style, title, tier, json.dumps(public_dto(key, tier, prompt_text=include_prompt)), digest),
                )
            cursor.execute("SELECT rights_tier FROM image_archive.archive_records_public WHERE catalog_key = 'manual:synthetic-p1'")
            checks["p1_valid"] = cursor.fetchone()[0] == "P1"
            cursor.execute("SELECT rights_tier FROM image_archive.archive_records_public WHERE catalog_key = 'manual:synthetic-p2'")
            checks["p2_valid"] = cursor.fetchone()[0] == "P2"

            cursor.execute("SAVEPOINT p3_public_guard")
            try:
                cursor.execute(
                    """
                    INSERT INTO image_archive.archive_records_public (
                        catalog_key, style_id, title, rights_tier, public_dto, content_sha256
                    ) VALUES (%s, %s, %s, 'P3', %s::jsonb, %s)
                    """,
                    ("manual:synthetic-p3", "SYN-P3", "Synthetic P3", json.dumps(public_dto("manual:synthetic-p3", "P3")), "c" * 64),
                )
            except Exception:
                cursor.execute("ROLLBACK TO SAVEPOINT p3_public_guard")
                checks["p3_blocked"] = True

            cursor.execute("SAVEPOINT p2_prompt_guard")
            try:
                cursor.execute(
                    """
                    UPDATE image_archive.archive_records_public
                    SET public_dto = %s::jsonb
                    WHERE catalog_key = 'manual:synthetic-p2'
                    """,
                    (json.dumps(public_dto("manual:synthetic-p2", "P2", prompt_text=True)),),
                )
            except Exception:
                cursor.execute("ROLLBACK TO SAVEPOINT p2_prompt_guard")
                checks["p2_prompt_blocked"] = True
        connection.rollback()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    ok = all(checks.values())
    print(json.dumps({"ok": ok, "checks": checks, "persisted": False}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
