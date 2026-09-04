from __future__ import annotations

import copy
import importlib.util
import io
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("v2_cloud_snapshot", ROOT / "platform/v2/local/cloud_snapshot.py")
cloud = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cloud)


class FakeNeon:
    def __init__(self):
        self.rows = None
        self.state = None
        self.calls = []

    def stage(self, plan):
        self.calls.append("stage")
        if self.rows is not None and self.rows != plan["items"]:
            raise cloud.SnapshotError("remote_snapshot_conflict")
        self.rows = copy.deepcopy(plan["items"])
        self.state = self.state or "staged"

    def verify(self, plan):
        self.calls.append("verify")
        if self.rows != plan["items"]:
            raise cloud.SnapshotError("neon_row_readback_mismatch")

    def ready(self, plan):
        self.calls.append("ready")
        self.state = "ready"


class FakeQdrant:
    def __init__(self):
        self.points = {}
        self.new_points = 0
        self.fail = False
        self.corrupt = False

    def stage(self, name, dimension, points):
        if self.fail:
            raise cloud.SnapshotError("qdrant_transport_failed")
        if name not in self.points:
            self.points[name] = copy.deepcopy(points)
            self.new_points += len(points)

    def read_points(self, name, limit):
        rows = copy.deepcopy(self.points[name])
        if self.corrupt:
            rows[0]["payload"]["group_id"] = "wrong"
        return rows


class CloudSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def fixture(self, count=3):
        items = []
        for index in range(count):
            ident = f"item{index}"
            ready = index < count-1
            items.append({"item_id": ident, "group_id": "g" if index < 2 else ident,
              "representative_id": "item0" if index < 2 else ident,
              "original_prompt": "  Exact 원문\r\n--x 1  ", "rights_json": {"release_eligible": False},
              "metadata_json": {"raw": {"unsafe": "retained audit"}, "effective": {"caption": "candidate"},
                                "qa": [{"field_path": "/unsafe"}], "review_status": "needs_review", "metadata_human_approved": False, "public_eligible": False},
              "human_note": None, "text_ready": ready, "retrieval_text": "clean compact " + ident if ready else "",
              "private_data": {"style_id": ident, "prepared_sha256": "a"*64,
                               "prepared_relative_path": "data/private-research/a.png", "prepared_mime_type": "image/png", "prepared_bytes": 20}})
        texts = {r["item_id"]: [1.] + [0.]*511 for r in items if r["text_ready"]}
        images = {r["item_id"]: [1.] + [0.]*1023 for r in items}
        queries = [{"query_id": "query:test", "query_text": "purpose", "vector_json": [1.] + [0.]*511}]
        return items, texts, images, queries, {"source_sha256": "a"*64}

    def save(self, data=None):
        manifest, bodies = cloud.assemble(*(data or self.fixture()))
        path = self.root / "data/private-research/plans" / cloud.sha(cloud.encoded(manifest))
        path.mkdir(parents=True, exist_ok=True)
        (path / "manifest.json").write_bytes(cloud.encoded(manifest))
        for name, raw in bodies.items():
            (path / name).write_bytes(raw)
        return path

    def test_roundtrip_retains_original_deferred_child_and_privacy(self):
        plan = cloud.read_plan(self.save(), self.root)
        self.assertEqual(plan["manifest"]["counts"], {"items": 3, "text_vectors": 2, "image_vectors": 3, "queries": 1, "groups": 2, "text_deferred": 1})
        self.assertEqual(plan["items"][0]["original_prompt"], "  Exact 원문\r\n--x 1  ")
        self.assertEqual(plan["items"][1]["representative_id"], "item0")
        self.assertFalse(plan["items"][2]["text_ready"])
        self.assertEqual(set(plan["text"][0]["payload"]), cloud.PAYLOAD_KEYS)
        self.assertEqual(len(plan["manifest"]["media_manifest"]), 3)
        self.assertEqual(plan["queries"][0]["dimension"], 512)

    def test_dryrun_does_not_write_or_use_clients_or_credentials(self):
        path = self.save()
        before = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*"))
        with patch.object(cloud, "credentials", side_effect=AssertionError("must not read credentials")), patch.object(cloud.Neon, "__init__", side_effect=AssertionError("network")):
            with patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(cloud.main(["--archive-root", str(self.root), "--plan", str(path)]), 0)
        self.assertEqual(before, sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*")))

    def test_apply_without_execute_remains_readonly_sync(self):
        result = cloud.sync_plan(self.save(), self.root, apply=True, neon=object(), qdrant=object())
        self.assertEqual(result["status"], "dry_run")

    def test_execute_without_apply_rejected(self):
        with self.assertRaisesRegex(cloud.SnapshotError, "explicit_apply"):
            cloud.sync_plan(self.save(), self.root, execute=True, neon=FakeNeon(), qdrant=FakeQdrant())

    def test_exact_readback_and_explicit_repeat_reuses_remote_points(self):
        path, neon, qdrant = self.save(), FakeNeon(), FakeQdrant()
        for _ in range(2):
            result = cloud.sync_plan(path, self.root, apply=True, execute=True, neon=neon, qdrant=qdrant)
            self.assertEqual(result["status"], "ready")
        self.assertEqual(qdrant.new_points, 5)
        self.assertEqual(len(list((path / "sync-attempts").glob("*/receipt.json"))), 2)
        self.assertEqual(neon.calls, ["stage", "verify", "ready"]*2)

    def test_transport_failure_keeps_staged_and_no_retry(self):
        path, neon, qdrant = self.save(), FakeNeon(), FakeQdrant()
        qdrant.fail = True
        with self.assertRaisesRegex(cloud.SnapshotError, "transport"):
            cloud.sync_plan(path, self.root, apply=True, execute=True, neon=neon, qdrant=qdrant)
        self.assertEqual(neon.state, "staged")
        self.assertEqual(neon.calls, ["stage"])
        self.assertEqual(len(list((path / "sync-attempts").glob("*/failed.json"))), 1)
        self.assertFalse((path / ".sync.lock").exists())

    def test_bad_readback_never_ready(self):
        path, neon, qdrant = self.save(), FakeNeon(), FakeQdrant()
        qdrant.corrupt = True
        with self.assertRaisesRegex(cloud.SnapshotError, "readback"):
            cloud.sync_plan(path, self.root, apply=True, execute=True, neon=neon, qdrant=qdrant)
        self.assertNotIn("ready", neon.calls)

    def test_exclusive_lock_is_not_removed_by_second_caller(self):
        path = self.save()
        (path / ".sync.lock").write_text("owned elsewhere")
        with self.assertRaisesRegex(cloud.SnapshotError, "locked"):
            cloud.sync_plan(path, self.root, apply=True, execute=True, neon=FakeNeon(), qdrant=FakeQdrant())
        self.assertTrue((path / ".sync.lock").exists())

    def test_manifest_and_payload_tamper_fail_closed(self):
        path = self.save()
        (path / "items.jsonl").write_bytes(b"{}\n")
        with self.assertRaisesRegex(cloud.SnapshotError, "file_drift"):
            cloud.read_plan(path, self.root)
        path = self.save()
        manifest = json.loads((path / "manifest.json").read_bytes())
        manifest["counts"]["items"] = 4
        (path / "manifest.json").write_bytes(cloud.encoded(manifest))
        with self.assertRaisesRegex(cloud.SnapshotError, "identity"):
            cloud.read_plan(path, self.root)

    def test_final_path_writer_uses_no_tempfile_and_is_immutable(self):
        manifest, bodies = cloud.assemble(*self.fixture())
        output = self.root / "data/private-research/plans"
        output.mkdir(parents=True)
        path = output / cloud.sha(cloud.encoded(manifest))
        with patch("tempfile.TemporaryDirectory", side_effect=AssertionError("creator-only ACL")):
            cloud.write_plan(path, manifest, bodies, self.root)
        self.assertEqual(cloud.read_plan(path, self.root)["manifest"], manifest)
        before = {f.name: f.read_bytes() for f in path.iterdir()}
        cloud.write_plan(path, manifest, bodies, self.root)
        self.assertEqual(before, {f.name: f.read_bytes() for f in path.iterdir()})
        (path / "items.jsonl").write_bytes(b"partial")
        with self.assertRaisesRegex(cloud.SnapshotError, "immutable_plan_conflict"):
            cloud.write_plan(path, manifest, bodies, self.root)
        self.assertEqual((path / "items.jsonl").read_bytes(), b"partial")

    def test_final_path_writer_manifest_is_last_completion_marker(self):
        manifest, bodies = cloud.assemble(*self.fixture())
        output = self.root / "data/private-research/plans"
        output.mkdir(parents=True)
        path = output / cloud.sha(cloud.encoded(manifest))
        original_open = Path.open
        def fail_later(file, *args, **kwargs):
            if file.name == "queries.jsonl":
                raise OSError("synthetic interrupted writer")
            return original_open(file, *args, **kwargs)
        if cloud.os.name != "nt":
            self.skipTest("Windows inherited-ACL writer branch regression")
        with patch.object(Path, "open", fail_later), self.assertRaises(OSError):
            cloud.write_plan(path, manifest, bodies, self.root)
        self.assertFalse((path / "manifest.json").exists())
        self.assertTrue((path / "items.jsonl").is_file())
        with self.assertRaisesRegex(cloud.SnapshotError, "immutable_plan_conflict"):
            cloud.write_plan(path, manifest, bodies, self.root)

    def test_cross_group_and_missing_images_fail(self):
        data = self.fixture()
        data[0][2]["representative_id"] = "item0"
        with self.assertRaisesRegex(cloud.SnapshotError, "cross_group"):
            cloud.assemble(*data)
        data = self.fixture()
        del data[2]["item0"]
        with self.assertRaisesRegex(cloud.SnapshotError, "scope"):
            cloud.assemble(*data)

    def test_deferred_vector_and_oversized_scope_rejected(self):
        data = self.fixture()
        data[1]["item2"] = [1.]+[0.]*511
        with self.assertRaisesRegex(cloud.SnapshotError, "text_ready"):
            cloud.assemble(*data)
        with self.assertRaisesRegex(cloud.SnapshotError, "scope"):
            cloud.assemble(*self.fixture(380))

    def test_private_path_guard(self):
        with self.assertRaisesRegex(cloud.SnapshotError, "private_path"):
            cloud.private_path(self.root / "public", self.root)

    def test_private_path_does_not_stat_ancestors_outside_trusted_root(self):
        target = self.root / "data/private-research/nested/file.json"
        checked = []
        def guarded_check(path):
            checked.append(path)
            if not path.is_relative_to(self.root) or path == self.root:
                raise PermissionError("ancestor cannot be inspected")
            return False
        with patch.object(Path, "is_symlink", guarded_check), patch.object(Path, "is_junction", guarded_check):
            self.assertEqual(cloud.private_path(target, self.root), target)
        self.assertIn(target, checked)
        self.assertIn(self.root / "data", checked)
        self.assertNotIn(self.root.parent, checked)

    def test_private_path_rejects_lexical_escape_before_link_checks(self):
        for path in (self.root.parent / "outside", self.root / "data/private-research/../outside"):
            with patch.object(Path, "is_symlink", side_effect=AssertionError("out-of-scope stat")):
                with self.assertRaisesRegex(cloud.SnapshotError, "private_path"):
                    cloud.private_path(path, self.root)

    def test_leaf_symlink_refused(self):
        path = self.save()
        target = self.root / "outside.json"
        target.write_bytes((path / "items.jsonl").read_bytes())
        (path / "items.jsonl").unlink()
        try:
            (path / "items.jsonl").symlink_to(target)
        except OSError:
            self.skipTest("symlink privilege unavailable")
        with self.assertRaisesRegex(cloud.SnapshotError, "symlink"):
            cloud.read_plan(path, self.root)

    def test_invalid_vectors_and_cosine_normalization(self):
        for bad in (None, [True]+[0.]*511, [0.]*512, [math.nan]+[0.]*511, [math.inf]+[0.]*511, [1.]*1024):
            with self.assertRaises(cloud.SnapshotError):
                cloud.vector(bad, 512)
        self.assertTrue(cloud.vectors_equal([1., 0.], [2., 0.]))
        self.assertFalse(cloud.vectors_equal([0., 1.], [2., 0.]))

    def test_source_database_readonly_bytes_unchanged(self):
        path = self.root / "source.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE x(value TEXT)")
            db.execute("INSERT INTO x VALUES('immutable')")
        db.close()
        before = path.read_bytes()
        with cloud.connect_ro(path) as db:
            self.assertEqual(db.execute("SELECT * FROM x").fetchone()[0], "immutable")
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM x")
        self.assertEqual(before, path.read_bytes())

    def test_network_modes_require_explicit_flags_before_credentials(self):
        for flags in (["--execute"], ["--preflight", "--apply"], ["--preflight", "--execute"]):
            with patch.object(cloud, "credentials", side_effect=AssertionError("credentials")), patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(cloud.main(flags), 1)
                self.assertIn("invalid_live_mode_flags", out.getvalue())

    def test_endpoint_allowlist_and_redirect_refused(self):
        for endpoint in ("http://a.cloud.qdrant.io", "https://evil.example", "https://a.cloud.qdrant.io/path", "https://a.cloud.qdrant.io@evil.example"):
            with self.assertRaises(cloud.SnapshotError):
                cloud.Qdrant(endpoint, "never-send")
        with self.assertRaisesRegex(cloud.SnapshotError, "redirect"):
            cloud.NoRedirect().redirect_request(None)

    def test_qdrant_batches_bounded_and_legacy_namespace_refused(self):
        client = cloud.Qdrant("https://test.cloud.qdrant.io", "not-real")
        calls = []
        def request(method, path, body=None, **kwargs):
            calls.append((method, path, body))
            if path.endswith("/scroll"):
                return {"points": [], "next_page_offset": None}
            return None
        client.request = request
        points = [{"id": str(i), "payload": {}, "vector": [1.]+[0.]*511} for i in range(121)]
        client.stage("image_archive_v2_"+"a"*64+"_text512", 512, points)
        self.assertEqual([len(body["points"]) for _, _, body in calls if body and "points" in body], [50,50,21])
        with self.assertRaisesRegex(cloud.SnapshotError, "namespace"):
            client.stage("legacy", 512, points)

    def test_qdrant_existing_conflict_never_overwritten(self):
        client = cloud.Qdrant("https://test.cloud.qdrant.io", "not-real")
        calls = []
        def request(method, path, body=None, **kwargs):
            calls.append(method)
            if path.endswith("/scroll"):
                return {"points": [{"id": "unexpected"}], "next_page_offset": None}
            return {"config": {"params": {"vectors": {"size":512,"distance":"Cosine"}}},
                    "payload_schema": {}}
        client.request = request
        with self.assertRaisesRegex(cloud.SnapshotError, "existing_point"):
            client.stage("image_archive_v2_"+"a"*64+"_text512", 512, [{"id": "expected"}])
        self.assertNotIn("PUT", calls)

    def test_neon_query_overrides_rejected_before_connect(self):
        base = "postgresql://owner:fake@ep-test.neon.tech/neondb?"
        queries = ("host=outside.invalid", "hostaddr=127.0.0.1", "service=other", "options=-csearch_path=public",
                   "port=1234", "password=override", "sslrootcert=other", "%68ost=outside.invalid",
                   "sslmode=require&sslmode=disable")
        with patch("psycopg2.connect") as connect, patch.dict(cloud.os.environ, {}, clear=True):
            for query in queries:
                with self.subTest(query=query), self.assertRaisesRegex(cloud.SnapshotError, "override"):
                    cloud.Neon(base + query, ROOT / "db/v2/0001_private_library.sql")
            connect.assert_not_called()

    def test_neon_effective_libpq_host_is_validated(self):
        with patch("psycopg2.connect") as connect, patch("psycopg2.extensions.parse_dsn", return_value={
            "host": "outside.invalid", "user": "owner", "dbname": "neondb"}):
            with self.assertRaisesRegex(cloud.SnapshotError, "effective_connection"):
                cloud.Neon("postgresql://owner:fake@ep-test.neon.tech/neondb", ROOT / "db/v2/0001_private_library.sql")
            connect.assert_not_called()

    def test_neon_connect_uses_validated_explicit_fields_and_preserves_tls(self):
        dsn = "postgresql://owner:fake@ep-test.neon.tech:5432/neondb?sslmode=verify-full&channel_binding=require"
        with patch("psycopg2.connect") as connect, patch.dict(cloud.os.environ, {}, clear=True):
            cloud.Neon(dsn, ROOT / "db/v2/0001_private_library.sql")
            self.assertEqual(connect.call_args.args, ())
            self.assertEqual(connect.call_args.kwargs, {"host": "ep-test.neon.tech", "port": "5432", "dbname": "neondb",
                "user": "owner", "password": "fake", "sslmode": "verify-full", "channel_binding": "require", "connect_timeout": 10})

    def test_neon_environment_route_override_and_weak_tls_rejected(self):
        base = "postgresql://owner:fake@ep-test.neon.tech/neondb"
        for key in ("PGHOSTADDR", "PGSERVICE", "PGOPTIONS"):
            with patch.dict(cloud.os.environ, {key: "override"}, clear=True), self.assertRaisesRegex(cloud.SnapshotError, "environment_override"):
                cloud.neon_parameters(base)
        with patch.dict(cloud.os.environ, {}, clear=True):
            for query in ("sslmode=disable", "sslmode=allow", "sslmode=prefer", "channel_binding=disable"):
                with self.subTest(query=query), self.assertRaisesRegex(cloud.SnapshotError, "tls_policy"):
                    cloud.neon_parameters(base + "?" + query)

    def test_qdrant_empty_page_with_next_offset_stops_immediately(self):
        client = cloud.Qdrant("https://test.cloud.qdrant.io", "not-real")
        client.request = MagicMock(return_value={"points": [], "next_page_offset": "new-offset"})
        with self.assertRaisesRegex(cloud.SnapshotError, "scroll_progress"):
            client.read_points("unused", 379)
        self.assertEqual(client.request.call_count, 1)

    def test_qdrant_offset_cycle_stops(self):
        client = cloud.Qdrant("https://test.cloud.qdrant.io", "not-real")
        client.request = MagicMock(side_effect=[{"points": [{"id": str(i)}], "next_page_offset": offset}
                                               for i, offset in enumerate(("a", "b", "a"))])
        with self.assertRaisesRegex(cloud.SnapshotError, "scroll_progress"):
            client.read_points("unused", 379)
        self.assertEqual(client.request.call_count, 3)

    def test_qdrant_distinct_offset_pages_have_hard_ceiling(self):
        client = cloud.Qdrant("https://test.cloud.qdrant.io", "not-real")
        client.request = MagicMock(side_effect=[{"points": [{"id": str(i)}], "next_page_offset": str(i)} for i in range(10)])
        with self.assertRaisesRegex(cloud.SnapshotError, "page_limit"):
            client.read_points("unused", 100)
        self.assertEqual(client.request.call_count, 3)  # ceil(100/50)+1

    def test_qdrant_full_pages_and_terminal_empty_page_are_bounded(self):
        client = cloud.Qdrant("https://test.cloud.qdrant.io", "not-real")
        pages = [{"points": [{"id": str(i)} for i in range(start, start+50)], "next_page_offset": str(start+50)} for start in (0,50)]
        pages.append({"points": [], "next_page_offset": None})
        client.request = MagicMock(side_effect=pages)
        self.assertEqual(len(client.read_points("unused", 100)), 100)
        self.assertEqual(client.request.call_count, 3)

    def test_qdrant_invalid_readback_limit_never_calls_transport(self):
        client = cloud.Qdrant("https://test.cloud.qdrant.io", "not-real")
        client.request = MagicMock()
        for bad in (True, -1, 380, 1.5):
            with self.assertRaisesRegex(cloud.SnapshotError, "limit_invalid"):
                client.read_points("unused", bad)
        client.request.assert_not_called()

    def test_schema_scoped_and_append_only(self):
        sql = (ROOT / "db/v2/0001_private_library.sql").read_text()
        self.assertNotIn("image_archive.", sql)
        self.assertNotIn("DROP ", sql)
        self.assertIn("BEFORE UPDATE OR DELETE", sql)
        self.assertIn("state='staged' AND NEW.state='ready'", sql)
        self.assertIn("dimension=512", sql)

    def test_schema_rejects_direct_ready_creation_and_locks_child_inserts(self):
        sql = (ROOT / "db/v2/0001_private_library.sql").read_text()
        create_guard = sql.split("CREATE FUNCTION image_archive_v2.only_staged_snapshot_insert()", 1)[1].split("END $$;", 1)[0]
        self.assertIn("NEW.state IS DISTINCT FROM 'staged'", create_guard)
        self.assertIn("v2_snapshot_insert BEFORE INSERT ON image_archive_v2.snapshots", sql)
        child_guard = sql.split("CREATE FUNCTION image_archive_v2.only_staged_child_insert()", 1)[1].split("END $$;", 1)[0]
        self.assertIn("WHERE snapshot_id=NEW.snapshot_id FOR UPDATE", child_guard)
        self.assertIn("parent_state IS DISTINCT FROM 'staged'", child_guard)
        for trigger, table in (("v2_items_insert", "items"), ("v2_queries_insert", "query_vectors")):
            self.assertIn(f"{trigger} BEFORE INSERT ON image_archive_v2.{table}", sql)
        self.assertEqual(sql.count("EXECUTE FUNCTION image_archive_v2.only_staged_child_insert();"), 2)
        transition = sql.split("CREATE FUNCTION image_archive_v2.only_ready_transition()", 1)[1]
        self.assertIn("WHERE snapshot_id=OLD.snapshot_id FOR UPDATE", transition)

    def test_metadata_presence_and_false_are_both_database_constraints(self):
        sql = (ROOT / "db/v2/0001_private_library.sql").read_text()
        self.assertIn("metadata_json @> '{\"metadata_human_approved\":false,\"public_eligible\":false}'::jsonb", sql)

    def test_ready_adapter_locks_then_rechecks_rows_before_transition(self):
        plan = cloud.read_plan(self.save(), self.root)
        client = cloud.Neon.__new__(cloud.Neon)
        client.connection = MagicMock()
        cursor = client.connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [("staged",), ("ready",)]
        events = []
        cursor.execute.side_effect = lambda sql, *args: events.append(sql)
        client._verify_rows = lambda cur, payload: events.append("full_row_readback")
        client.ready(plan)
        lock = next(i for i, sql in enumerate(events) if "FOR UPDATE" in sql)
        readback = events.index("full_row_readback")
        transition = next(i for i, sql in enumerate(events) if sql.startswith("UPDATE "))
        self.assertLess(lock, readback)
        self.assertLess(readback, transition)

    def test_ready_adapter_changed_rows_block_transition(self):
        plan = cloud.read_plan(self.save(), self.root)
        client = cloud.Neon.__new__(cloud.Neon)
        client.connection = MagicMock()
        cursor = client.connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = ("staged",)
        client._verify_rows = MagicMock(side_effect=cloud.SnapshotError("neon_row_readback_mismatch"))
        with self.assertRaisesRegex(cloud.SnapshotError, "readback"):
            client.ready(plan)
        self.assertFalse(any(call.args[0].startswith("UPDATE ") for call in cursor.execute.call_args_list))

    def test_ready_snapshot_rerun_does_not_attempt_child_inserts(self):
        plan = cloud.read_plan(self.save(), self.root)
        client = cloud.Neon.__new__(cloud.Neon)
        client.connection = MagicMock()
        client.migration = (ROOT / "db/v2/0001_private_library.sql").read_bytes()
        plan["manifest"]["identity"]["bindings"]["db/v2/0001_private_library.sql"] = cloud.sha(client.migration)
        cursor = client.connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [("exists",), (cloud.sha(client.migration),),
                                      (plan["manifest_sha256"], plan["manifest"], "ready")]
        client.verify = MagicMock()
        client.stage(plan)
        cursor.executemany.assert_not_called()
        client.verify.assert_called_once_with(plan)


if __name__ == "__main__":
    unittest.main()
