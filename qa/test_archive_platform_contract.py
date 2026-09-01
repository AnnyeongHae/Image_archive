from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from src.archive_platform.api import create_app
from src.archive_platform.auth import AdminPrincipal
from src.archive_platform.import_canonical import dry_run
from src.archive_platform.migrate import discover_migrations, migration_plan
from src.archive_platform.store import decode_cursor, encode_cursor


ROOT = Path(__file__).resolve().parents[1]


class FakeStore:
    def __init__(self) -> None:
        self.last_private_include_quarantine = None
        self.last_draft_subject = None

    def ready(self) -> bool:
        return True

    def list_public(self, *, q, cursor, limit):
        return {"items": [{"catalog_key": "public:1"}], "next_cursor": None}

    def get_public(self, catalog_key):
        return {"catalog_key": catalog_key} if catalog_key == "public:1" else None

    def list_private(self, *, q, cursor, limit, include_quarantine):
        self.last_private_include_quarantine = include_quarantine
        return {"items": [{"catalog_key": "private:1", "rights_tier": "P3"}], "next_cursor": None}

    def get_private(self, catalog_key):
        if catalog_key == "private:p4":
            return {"catalog_key": catalog_key, "rights_tier": "P4"}
        return {"catalog_key": catalog_key, "rights_tier": "P3"} if catalog_key == "private:1" else None

    def get_review_draft(self, *, subject, queue_revision):
        self.last_draft_subject = subject
        return None

    def put_review_draft(self, *, subject, queue_revision, decisions):
        self.last_draft_subject = subject
        return {"queue_revision": queue_revision, "decision_count": len(decisions), "updated_at": "2026-09-01T00:00:00+00:00"}


def normal_admin() -> AdminPrincipal:
    return AdminPrincipal(
        subject="admin-subject",
        email="admin@example.test",
        scopes=frozenset({"archive:read", "review:write"}),
    )


def quarantine_admin() -> AdminPrincipal:
    return AdminPrincipal(
        subject="quarantine-subject",
        email="quarantine@example.test",
        scopes=frozenset({"archive:read", "review:write", "quarantine:read"}),
    )


class MigrationContractTests(unittest.TestCase):
    def test_migrations_are_versioned_non_destructive_and_checksumed(self) -> None:
        migrations = discover_migrations()
        self.assertEqual([item.version for item in migrations], ["0001", "0002"])
        self.assertTrue(all(len(item.sha256) == 64 for item in migrations))
        text = "\n".join(item.sql for item in migrations)
        self.assertIn("archive_records_public", text)
        self.assertIn("archive_records_private", text)
        self.assertIn("archive_media_no_inline_base64", text)

    def test_dry_run_plan_does_not_connect(self) -> None:
        plan = migration_plan()
        self.assertEqual(plan["mode"], "dry_run")
        self.assertEqual(plan["migration_count"], 2)


class CursorContractTests(unittest.TestCase):
    def test_cursor_round_trip(self) -> None:
        cursor = encode_cursor("external:case-001")
        self.assertEqual(decode_cursor(cursor), "external:case-001")

    def test_invalid_cursor_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid_cursor"):
            decode_cursor("not-a-cursor")


class ImportContractTests(unittest.TestCase):
    def test_streaming_dry_run_never_promotes_p3(self) -> None:
        canonical = ROOT / "data" / "canonical" / "archive_records.jsonl"
        result = dry_run(canonical, limit=3)
        self.assertEqual(result["records_seen"], 3)
        self.assertEqual(result["rights_tiers"], {"P3": 3})
        self.assertEqual(result["public_projection_records"], 0)

    def test_inline_media_is_not_part_of_fixture_contract(self) -> None:
        record = {
            "rights": {"rights_tier": "P3"},
            "media": {"assets": []},
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.jsonl"
            path.write_text("{}\n".format(__import__("json").dumps(record)), encoding="utf-8")
            result = dry_run(path, limit=1)
        self.assertEqual(result["media_rows"], 0)


class ApiContractTests(unittest.TestCase):
    def test_public_route_never_requires_admin_auth(self) -> None:
        client = TestClient(create_app(store=FakeStore(), auth_dependency=normal_admin))
        response = client.get("/api/public/v1/records")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["catalog_key"], "public:1")

    def test_normal_admin_cannot_expand_quarantine(self) -> None:
        store = FakeStore()
        client = TestClient(create_app(store=store, auth_dependency=normal_admin))
        response = client.get("/api/admin/v1/records?include_quarantine=true")
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(store.last_private_include_quarantine)

    def test_normal_admin_cannot_open_p4_detail(self) -> None:
        client = TestClient(create_app(store=FakeStore(), auth_dependency=normal_admin))
        response = client.get("/api/admin/v1/records/private:p4")
        self.assertEqual(response.status_code, 403)

    def test_quarantine_admin_can_open_p4_detail(self) -> None:
        client = TestClient(create_app(store=FakeStore(), auth_dependency=quarantine_admin))
        response = client.get("/api/admin/v1/records/private:p4")
        self.assertEqual(response.status_code, 200)

    def test_review_draft_is_scoped_to_authenticated_subject(self) -> None:
        store = FakeStore()
        client = TestClient(create_app(store=store, auth_dependency=normal_admin))
        revision = "a" * 64
        response = client.put(
            f"/api/admin/v1/review-drafts/{revision}",
            json={"decisions": [{"item_key": "1", "decision": "hold"}]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(store.last_draft_subject, "admin-subject")


if __name__ == "__main__":
    unittest.main()
