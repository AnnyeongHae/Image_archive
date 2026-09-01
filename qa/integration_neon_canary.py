from __future__ import annotations

import json

from fastapi.testclient import TestClient

from src.archive_platform.api import create_app
from src.archive_platform.auth import AdminPrincipal
from src.archive_platform.db import connect
from src.archive_platform.store import ArchiveStore


def scalar(cursor, sql: str):
    cursor.execute(sql)
    return cursor.fetchone()[0]


def main() -> int:
    with connect() as connection:
        with connection.cursor() as cursor:
            summary = {
                "migrations": scalar(cursor, "SELECT count(*) FROM image_archive.schema_migrations"),
                "private_records": scalar(cursor, "SELECT count(*) FROM image_archive.archive_records_private"),
                "public_records": scalar(cursor, "SELECT count(*) FROM image_archive.archive_records_public"),
                "private_media": scalar(cursor, "SELECT count(*) FROM image_archive.archive_media_private"),
                "p3_records": scalar(cursor, "SELECT count(*) FROM image_archive.archive_records_private WHERE rights_tier = 'P3'"),
                "p4_records": scalar(cursor, "SELECT count(*) FROM image_archive.archive_records_private WHERE rights_tier = 'P4'"),
                "inline_data_uris": scalar(cursor, "SELECT count(*) FROM image_archive.archive_media_private WHERE uri LIKE 'data:%'"),
                "completed_imports": scalar(cursor, "SELECT count(*) FROM image_archive.import_batches WHERE status = 'completed'"),
            }
    expected = {
        "migrations": 2,
        "private_records": 50,
        "public_records": 0,
        "private_media": 50,
        "p3_records": 50,
        "p4_records": 0,
        "inline_data_uris": 0,
        "completed_imports": 1,
    }
    if summary != expected:
        print(json.dumps({"ok": False, "observed": summary, "expected": expected}, ensure_ascii=False))
        return 1
    store = ArchiveStore()
    if not store.ready():
        return 1
    public_page = store.list_public(q=None, cursor=None, limit=50)
    admin_page = store.list_private(q=None, cursor=None, limit=5, include_quarantine=False)
    if public_page != {"items": [], "next_cursor": None} or len(admin_page["items"]) != 5:
        print(json.dumps({"ok": False, "public_page": public_page, "admin_count": len(admin_page["items"])}, ensure_ascii=False))
        return 1
    principal = AdminPrincipal(
        subject="neon-canary",
        email="canary@example.test",
        scopes=frozenset({"archive:read", "review:write"}),
    )
    client = TestClient(create_app(store=store, auth_dependency=lambda: principal))
    public_response = client.get("/api/public/v1/records")
    admin_response = client.get("/api/admin/v1/records?limit=5")
    if public_response.status_code != 200 or public_response.json()["items"]:
        print(json.dumps({"ok": False, "public_http_status": public_response.status_code}, ensure_ascii=False))
        return 1
    if admin_response.status_code != 200 or len(admin_response.json()["items"]) != 5:
        print(json.dumps({"ok": False, "admin_http_status": admin_response.status_code}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **summary, "admin_page_canary": 5, "http_contract": "passed"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
