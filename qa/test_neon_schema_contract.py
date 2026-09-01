from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = sorted((ROOT / "db" / "migrations").glob("*.sql"))


def migration_text() -> str:
    chunks: list[str] = []
    for path in MIGRATIONS:
        chunks.append(path.read_text(encoding="utf-8"))
    return "\n\n".join(chunks)


class NeonSchemaContractTests(unittest.TestCase):
    def test_migration_declares_required_tables(self) -> None:
        self.assertTrue(MIGRATIONS, "expected at least one SQL migration")
        text = migration_text()
        required = [
            "CREATE TABLE IF NOT EXISTS image_archive.import_batches",
            "CREATE TABLE IF NOT EXISTS image_archive.archive_records_private",
            "CREATE TABLE IF NOT EXISTS image_archive.archive_media_private",
            "CREATE TABLE IF NOT EXISTS image_archive.archive_records_public",
            "CREATE TABLE IF NOT EXISTS image_archive.sources",
            "CREATE TABLE IF NOT EXISTS image_archive.source_runs",
            "CREATE TABLE IF NOT EXISTS image_archive.source_items",
            "CREATE TABLE IF NOT EXISTS image_archive.review_drafts",
            "CREATE TABLE IF NOT EXISTS image_archive.review_decisions",
            "CREATE TABLE IF NOT EXISTS image_archive.duplicate_groups",
            "CREATE TABLE IF NOT EXISTS image_archive.duplicate_group_members",
        ]
        for fragment in required:
            self.assertIn(fragment, text)

    def test_migration_keeps_fail_closed_rights_constraints(self) -> None:
        text = migration_text()
        self.assertRegex(text, r"rights_tier (?:TEXT|text|char\(2\)) NOT NULL(?: DEFAULT 'P3')? CHECK \(rights_tier IN \('P1', 'P2', 'P3', 'P4'\)\)")
        self.assertRegex(
            text,
            r"portfolio_visibility (?:TEXT|text) NOT NULL CHECK \(portfolio_visibility IN \('public', 'metadata_link_only', 'admin_only'\)\)",
        )
        self.assertIn("CREATE INDEX IF NOT EXISTS archive_private_fts_idx", text)


if __name__ == "__main__":
    unittest.main()
