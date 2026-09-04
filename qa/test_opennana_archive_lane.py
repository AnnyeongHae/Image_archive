from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ARCHIVE_ROOT = Path(__file__).resolve().parents[1]
MODULE_DIR = ARCHIVE_ROOT / "src" / "opennana"
sys.path.insert(0, str(MODULE_DIR))
sys.path.insert(0, str(ARCHIVE_ROOT / "src"))

from build_archive_lane import build  # noqa: E402
from common import read_json, stable_json  # noqa: E402
from build_canonical_archive import normalize_opennana, public_record  # noqa: E402


def source_hash(number: int) -> str:
    return f"{number:064x}"


def queue_record(upstream_id: str, number: int, updated_at: str) -> dict:
    content_hash = source_hash(number)
    return {
        "queue_id": f"ONN-{upstream_id}-{content_hash[:12]}",
        "upstream_id": upstream_id,
        "content_sha256": content_hash,
        "prompt_text": f"Prompt {upstream_id} version {number}",
        "prompt_sha256": source_hash(number + 100),
        "title": f"Title {upstream_id}",
        "slug": f"slug-{upstream_id}",
        "source_url": f"https://opennana.com/awesome-prompt-gallery/slug-{upstream_id}",
        "image_urls": [f"https://img.opennana.com/{upstream_id}-{number}.jpg"],
        "model": "ChatGPT",
        "media_type": "image",
        "tags": ["fixture"],
        "updated_at": updated_at,
        "rights": {"release_eligible": False, "item_rights": "unverified"},
        "workflow_status": "canonicalization_pending",
        "human_decision": {
            "queue_id": f"ONN-{upstream_id}-{content_hash[:12]}",
            "upstream_id": upstream_id,
            "content_sha256": content_hash,
            "decision": "approve",
            "group_with": None,
            "note": "",
        },
    }


def write_batch(root: Path, batch: str, rows: list[dict], decided_at: str) -> tuple[Path, Path]:
    pending = root / "staging" / f"canonicalization-pending-{batch}.json"
    applied = root / "decisions" / f"applied-{batch}.json"
    pending.parent.mkdir(parents=True, exist_ok=True)
    applied.parent.mkdir(parents=True, exist_ok=True)
    pending_payload = {
        "schema_version": "opennana-canonicalization-pending-1.0",
        "run_id": batch,
        "queue_revision": source_hash(len(rows) + 900),
        "public_release_eligible": False,
        "record_count": len(rows),
        "records": rows,
    }
    applied_payload = {
        "schema_version": "opennana-applied-decisions-1.0",
        "run_id": batch,
        "queue_revision": pending_payload["queue_revision"],
        "decided_at": decided_at,
        "summary": {"approve": len(rows)},
        "decisions": [row["human_decision"] for row in rows],
    }
    pending.write_text(stable_json(pending_payload), encoding="utf-8")
    applied.write_text(stable_json(applied_payload), encoding="utf-8")
    return pending, applied


class OpenNanaArchiveLaneTests(unittest.TestCase):
    def test_dry_run_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pending, applied = write_batch(root, "batch-1", [queue_record("10", 1, "2026-08-30T00:00:00Z")], "2026-08-31T00:00:00Z")
            archive = root / "archive.json"
            projection = root / "projection.js"
            result = build(
                pending_paths=[pending],
                applied_paths=[applied],
                data_root=root,
                archive_path=archive,
                projection_path=projection,
                apply=False,
                rebuild_canonical=False,
            )
            self.assertEqual(result["record_count"], 1)
            self.assertFalse(result["public_release_effect"])
            self.assertFalse(archive.exists())
            self.assertFalse(projection.exists())

    def test_historical_batches_union_and_latest_source_version_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pending1, applied1 = write_batch(
                root,
                "batch-1",
                [queue_record("10", 1, "2026-08-30T00:00:00Z")],
                "2026-08-31T00:00:00Z",
            )
            archive = root / "archive" / "opennana_records.json"
            projection = root / "projection.js"
            first = build(
                pending_paths=[pending1],
                applied_paths=[applied1],
                data_root=root,
                archive_path=archive,
                projection_path=projection,
                apply=True,
                rebuild_canonical=False,
            )
            self.assertEqual(first["promoted_internal_archive"], 1)

            pending2, applied2 = write_batch(
                root,
                "batch-2",
                [
                    queue_record("10", 2, "2026-08-31T01:00:00Z"),
                    queue_record("20", 3, "2026-08-31T01:00:00Z"),
                ],
                "2026-08-31T02:00:00Z",
            )
            second = build(
                pending_paths=[pending2],
                applied_paths=[applied2],
                data_root=root,
                archive_path=archive,
                projection_path=projection,
                apply=True,
                rebuild_canonical=False,
            )
            payload = read_json(archive)
            self.assertEqual(second["record_count"], 2)
            self.assertEqual(second["promoted_from_trigger"], 2)
            self.assertEqual(second["promoted_internal_archive"], 2)
            self.assertEqual([row["upstream_id"] for row in payload["records"]], ["10", "20"])
            by_id = {row["upstream_id"]: row for row in payload["records"]}
            self.assertEqual(by_id["10"]["source_content_sha256"], source_hash(2))
            self.assertEqual(by_id["10"]["record_id"], "OPENNANA-10")
            self.assertEqual(by_id["10"]["reference_style_id"], "ONN-10")
            self.assertEqual(by_id["10"]["rights_tier"], "P3")
            self.assertEqual(by_id["10"]["portfolio_visibility"], "admin_only")
            self.assertFalse(by_id["10"]["rights"]["release_eligible"])
            self.assertFalse(by_id["10"]["rights"]["prompt_publication_eligible"])
            self.assertIn("window.DETAILPAGE_OPENNANA_RECORDS", projection.read_text(encoding="utf-8"))

            unchanged = build(
                data_root=root,
                archive_path=archive,
                projection_path=projection,
                apply=True,
                rebuild_canonical=False,
            )
            self.assertTrue(unchanged["unchanged"])
            self.assertFalse(unchanged["outputs_written"])
            self.assertEqual(unchanged["promoted_from_trigger"], 0)

    def test_pending_without_matching_applied_decision_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pending, applied = write_batch(root, "batch-1", [queue_record("10", 1, "2026-08-30T00:00:00Z")], "2026-08-31T00:00:00Z")
            payload = json.loads(applied.read_text(encoding="utf-8"))
            payload["decisions"][0]["decision"] = "defer"
            applied.write_text(stable_json(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                build(
                    pending_paths=[pending],
                    applied_paths=[applied],
                    data_root=root,
                    archive_path=root / "archive.json",
                    projection_path=root / "projection.js",
                    apply=False,
                    rebuild_canonical=False,
                )

    def test_p3_canonical_record_is_excluded_from_public_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pending, applied = write_batch(root, "batch-1", [queue_record("10", 1, "2026-08-30T00:00:00Z")], "2026-08-31T00:00:00Z")
            archive = root / "archive.json"
            projection = root / "projection.js"
            build(
                pending_paths=[pending],
                applied_paths=[applied],
                data_root=root,
                archive_path=archive,
                projection_path=projection,
                apply=True,
                rebuild_canonical=False,
            )
            private_row = read_json(archive)["records"][0]
            canonical = normalize_opennana(private_row, index=0)
            self.assertEqual(canonical["prompt"]["text"], private_row["prompt"])
            self.assertEqual(len(canonical["media"]["assets"]), 1)
            self.assertEqual(canonical["rights"]["rights_tier"], "P3")
            self.assertEqual(canonical["rights"]["portfolio_visibility"], "admin_only")
            with self.assertRaises(ValueError):
                public_record(canonical)


if __name__ == "__main__":
    unittest.main()
