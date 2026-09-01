from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .db import PLATFORM_ROOT, connect


MIGRATIONS_DIR = PLATFORM_ROOT / "db" / "migrations"
VERSION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
FORBIDDEN_SQL = re.compile(
    r"\b(?:DROP\s+(?:TABLE|SCHEMA|DATABASE)|TRUNCATE|ALTER\s+TABLE\s+[^;]+\s+DROP\b)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path
    sql: str
    sha256: str


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = VERSION_RE.fullmatch(path.name)
        if not match:
            raise ValueError(f"invalid migration filename: {path.name}")
        sql = path.read_text(encoding="utf-8-sig")
        if FORBIDDEN_SQL.search(sql):
            raise ValueError(f"destructive SQL is forbidden in migration: {path.name}")
        migrations.append(
            Migration(
                version=match.group(1),
                name=path.name,
                path=path,
                sql=sql,
                sha256=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            )
        )
    versions = [item.version for item in migrations]
    if not migrations or len(versions) != len(set(versions)):
        raise ValueError("migration set is empty or contains duplicate versions")
    return migrations


def migration_plan(directory: Path = MIGRATIONS_DIR) -> dict[str, object]:
    rows = discover_migrations(directory)
    return {
        "schema": "image_archive",
        "mode": "dry_run",
        "migration_count": len(rows),
        "migrations": [
            {"version": row.version, "name": row.name, "sha256": row.sha256}
            for row in rows
        ],
    }


def apply_migrations(*, directory: Path = MIGRATIONS_DIR, dsn: str | None = None) -> dict[str, object]:
    migrations = discover_migrations(directory)
    applied: list[str] = []
    skipped: list[str] = []
    with connect(dsn=dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("CREATE SCHEMA IF NOT EXISTS image_archive")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS image_archive.schema_migrations (
                    version text PRIMARY KEY,
                    name text NOT NULL,
                    sha256 char(64) NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
        connection.commit()
        for migration in migrations:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT name, sha256 FROM image_archive.schema_migrations WHERE version = %s",
                        (migration.version,),
                    )
                    existing = cursor.fetchone()
                    if existing:
                        if existing[1] != migration.sha256:
                            raise RuntimeError(
                                f"migration checksum drift for {migration.version}: "
                                f"database={existing[1]} local={migration.sha256}"
                            )
                        skipped.append(migration.version)
                        connection.rollback()
                        continue
                    cursor.execute(migration.sql)
                    cursor.execute(
                        "INSERT INTO image_archive.schema_migrations(version, name, sha256) VALUES (%s, %s, %s)",
                        (migration.version, migration.name, migration.sha256),
                    )
                connection.commit()
                applied.append(migration.version)
            except Exception:
                connection.rollback()
                raise
    return {
        "schema": "image_archive",
        "mode": "apply",
        "migration_count": len(migrations),
        "applied": applied,
        "skipped": skipped,
    }


def check_migrations(*, directory: Path = MIGRATIONS_DIR, dsn: str | None = None) -> dict[str, object]:
    """Execute every migration in one transaction and always roll it back."""

    migrations = discover_migrations(directory)
    connection = connect(dsn=dsn)
    try:
        with connection.cursor() as cursor:
            cursor.execute("CREATE SCHEMA IF NOT EXISTS image_archive")
            for migration in migrations:
                cursor.execute(migration.sql)
        connection.rollback()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "schema": "image_archive",
        "mode": "transaction_rollback_check",
        "migration_count": len(migrations),
        "validated": [item.version for item in migrations],
        "persisted": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply append-only Neon migrations (dry-run by default).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Apply migrations transactionally")
    mode.add_argument("--check", action="store_true", help="Execute all migrations and roll the transaction back")
    parser.add_argument("--migrations", type=Path, default=MIGRATIONS_DIR)
    args = parser.parse_args(argv)
    if args.apply:
        result = apply_migrations(directory=args.migrations)
    elif args.check:
        result = check_migrations(directory=args.migrations)
    else:
        result = migration_plan(args.migrations)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
