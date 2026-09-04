from __future__ import annotations

import base64
import copy
from datetime import timedelta
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("v2_runtime_setup", ROOT / "platform/v2/local/runtime_setup.py")
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


class RuntimeSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        for relative in ("platform/v2/worker", "deploy/cloudflare-staging", "db/v2"):
            (self.root / relative).mkdir(parents=True)
        for relative in ("platform/v2/wrangler.jsonc", "deploy/cloudflare-staging/wrangler.jsonc", "db/v2/0002_api_budget.sql"):
            (self.root / relative).write_bytes((ROOT / relative).read_bytes())
        (self.root / "platform/v2/worker/index.js").write_text("// synthetic", encoding="utf-8")
        self.plan = {"manifest_sha256": "b"*64, "manifest": {"snapshot_id": "a"*64,
          "identity": {"bindings": {"db/v2/0001_private_library.sql": "c"*64}},
          "qdrant_collections": {"text": "image_archive_v2_"+"a"*64+"_text512"},
          "counts": {"items": 3, "text_vectors": 2, "groups": 2, "queries": 1}}}
        self.credentials = {"DATABASE_URL": "postgresql://owner:SYNTHETIC_ADMIN_PASSWORD@ep-test.us.aws.neon.tech/neondb?sslmode=require",
                            "QDRANT_ENDPOINT": "https://test.cloud.qdrant.io", "QDRANT_API_KEY": "SYNTHETIC_QDRANT_KEY"}
        self.now = runtime.utc_now()
        self.addCleanup(patch.stopall)
        patch.object(runtime, "utc_now", return_value=self.now).start()
        patch.dict(runtime.os.environ, {}, clear=True).start()
        with patch.object(runtime.cloud, "read_plan", return_value=copy.deepcopy(self.plan)):
            self.config = runtime.configuration(self.root / "synthetic-plan", "owner@example.test", self.root)

    def prepare(self):
        synthetic = [base64.urlsafe_b64encode(letter*32).decode().rstrip("=") for letter in (b"p", b"t")]
        with patch.object(runtime.secrets, "token_urlsafe", side_effect=synthetic) as generate:
            result = runtime.prepare(self.config, apply=True, credentials=self.credentials, now=self.now)
            self.assertEqual(generate.call_args_list[0].args, (32,))
            self.assertEqual(generate.call_count, 2)
        return result, runtime.read_bundle(self.config)

    def expected_privileges(self):
        return [("image_archive_v2", table, privilege, True, privilege in {"SELECT", "INSERT", "UPDATE"})
                for table, privileges in runtime.GRANTS.items() for privilege in privileges]

    def test_dryrun_creates_nothing_and_does_not_generate_secrets(self):
        before = sorted(p.as_posix() for p in self.root.rglob("*"))
        with patch.object(runtime.secrets, "token_urlsafe", side_effect=AssertionError("no generation")), patch.object(runtime.cloud, "credentials", side_effect=AssertionError("no env")):
            result = runtime.prepare(self.config)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(before, sorted(p.as_posix() for p in self.root.rglob("*")))

    def test_prepared_files_use_restricted_role_and_expiring_scoped_token(self):
        result, bundle = self.prepare()
        values = bundle["values"]
        worker, client = values["worker-secrets.json"], values["owner-client.json"]
        self.assertNotIn("SYNTHETIC_ADMIN_PASSWORD", json.dumps(values))
        self.assertEqual(set(worker), {"DATABASE_URL", "QDRANT_ENDPOINT", "QDRANT_API_KEY", "API_TOKEN_HASHES"})
        self.assertIn("v2_api_aaaaaaaaaaaa:", worker["DATABASE_URL"])
        self.assertEqual(client["scopes"], ["rag:search", "archive:read"])
        self.assertEqual(bundle["manifest"]["expires_at"], runtime.iso(self.now+timedelta(days=90)))
        self.assertEqual(json.loads(worker["API_TOKEN_HASHES"])[0]["sha256"], runtime.cloud.sha(client["token"].encode()))
        self.assertNotIn(client["token"], json.dumps(result))
        self.assertNotIn(values["role-credentials.json"]["password"], json.dumps(result))
        config = values["wrangler.runtime.json"]
        self.assertTrue(Path(config["main"]).is_absolute())
        self.assertEqual(config["vars"]["PRIVATE_API_ENABLED"], "true")
        self.assertEqual(config["vars"]["LIVE_QUERY_EMBEDDING_ENABLED"], "false")
        self.assertEqual(config["vars"]["SNAPSHOT_MANIFEST_SHA256"], self.plan["manifest_sha256"])
        self.assertEqual(config["r2_buckets"][0]["binding"], "PRIVATE_MEDIA")

    def test_existing_bundle_reused_without_secret_regeneration(self):
        _, bundle = self.prepare()
        with patch.object(runtime.secrets, "token_urlsafe", side_effect=AssertionError("no rotation")):
            result = runtime.prepare(self.config, apply=True)
        self.assertEqual(result["status"], "prepared_reused")
        self.assertEqual(result["runtime_manifest_sha256"], bundle["manifest_sha256"])

    def test_direct_writer_does_not_use_restricted_tempfile(self):
        with patch("tempfile.TemporaryDirectory", side_effect=AssertionError("restricted temp ACL")):
            self.prepare()
        self.assertTrue((self.config["target"] / "runtime-manifest.json").is_file())

    def test_file_tamper_and_expired_credentials_fail_closed(self):
        self.prepare()
        with self.assertRaisesRegex(runtime.RuntimeError, "expired"):
            runtime.read_bundle(self.config, now=self.now+timedelta(days=91))
        (self.config["target"] / "worker-secrets.json").write_text("{}")
        with self.assertRaisesRegex(runtime.RuntimeError, "file_drift"):
            runtime.read_bundle(self.config)

    def test_owner_identity_and_access_policy_not_inferred_or_changed(self):
        with patch.object(runtime.cloud, "read_plan", return_value=self.plan):
            for bad in ("", "*@example.test", "owner@example.test,other@example.test"):
                with self.assertRaisesRegex(runtime.RuntimeError, "owner_email"):
                    runtime.configuration(self.root / "plan", bad, self.root)
            with self.assertRaisesRegex(runtime.RuntimeError, "existing_access"):
                runtime.configuration(self.root / "plan", "owner@example.test", self.root, policy_aud="f"*64)

    def test_table_and_column_scope_is_exact_and_has_no_delete(self):
        rows = self.expected_privileges()
        runtime.check_table_privileges(rows)
        for extra in (("image_archive_v2", "items", "DELETE", True, False),
                      ("legacy", "secrets", "SELECT", False, True),
                      ("image_archive_v2", "api_model_guard", "INSERT", True, True)):
            with self.assertRaisesRegex(runtime.RuntimeError, "privilege_conflict"):
                runtime.check_table_privileges(rows+[extra])
        with self.assertRaisesRegex(runtime.RuntimeError, "privilege_conflict"):
            runtime.check_table_privileges(rows[1:])

    def test_exact_ready_snapshot_required_before_schema_or_role_changes(self):
        _, bundle = self.prepare()
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [("owner",), (self.plan["manifest_sha256"], self.plan["manifest"], "staged")]
        with self.assertRaisesRegex(runtime.RuntimeError, "ready_snapshot"):
            runtime.provision(connection, bundle, execute=True)
        self.assertFalse(any("CREATE " in str(call.args[0]) or "GRANT " in str(call.args[0]) for call in cursor.execute.call_args_list))

    def test_new_role_is_additive_bounded_and_budget_migration_pinned(self):
        _, bundle = self.prepare()
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [("owner",), (self.plan["manifest_sha256"], self.plan["manifest"], "ready"),
                                      ("c"*64,), None, (None,None,None), None]
        with patch.object(runtime, "audit_role") as audit:
            result = runtime.provision(connection, bundle, execute=True)
        self.assertEqual(result, {"role_created": True, "budget_migration_applied": True})
        calls = [str(call.args[0]) for call in cursor.execute.call_args_list]
        creation = next(value for value in calls if "CREATE ROLE" in value)
        for guard in ("NOINHERIT", "NOSUPERUSER", "NOCREATEDB", "NOCREATEROLE", "NOREPLICATION", "NOBYPASSRLS"):
            self.assertIn(guard, creation)
        self.assertNotIn("DELETE", "\n".join(value for value in calls if "GRANT " in value))
        self.assertNotIn("ALTER ROLE", "\n".join(calls))
        audit.assert_called_once_with(cursor, bundle)

    def test_existing_role_only_verified_no_password_reset_or_regrant(self):
        _, bundle = self.prepare()
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [("owner",), (self.plan["manifest_sha256"], self.plan["manifest"], "ready"),
                                      ("c"*64,), (self.config["identity"]["budget_migration_sha256"],), (1,)]
        with patch.object(runtime, "audit_role") as audit:
            result = runtime.provision(connection, bundle, execute=True)
        self.assertEqual(result, {"role_created": False, "budget_migration_applied": False})
        calls = "\n".join(str(call.args[0]) for call in cursor.execute.call_args_list)
        self.assertFalse(any(token in calls for token in ("CREATE ROLE", "ALTER ROLE", "GRANT ", "COMMENT ON")))
        audit.assert_called_once()

    def test_untracked_budget_tables_and_changed_migration_block(self):
        _, bundle = self.prepare()
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [("owner",), (self.plan["manifest_sha256"], self.plan["manifest"], "ready"),
                                      ("c"*64,), None, ("existing",None,None)]
        with self.assertRaisesRegex(runtime.RuntimeError, "untracked_budget"):
            runtime.provision(connection, bundle, execute=True)
        self.config["budget_path"].write_text("-- changed")
        with self.assertRaisesRegex(runtime.RuntimeError, "migration_drift"):
            runtime.provision(connection, bundle, execute=True)

    def test_existing_elevated_role_is_rejected_not_mutated(self):
        _, bundle = self.prepare()
        cursor = MagicMock()
        cursor.fetchone.return_value = (bundle["manifest"]["role"], True, False, False, False, True, False, False, 5,
                                       self.now+timedelta(days=90), None, "image-archive-v2:"+bundle["manifest_sha256"])
        with self.assertRaisesRegex(runtime.RuntimeError, "role_attribute"):
            runtime.audit_role(cursor, bundle)
        self.assertFalse(any("ALTER " in str(call.args[0]) for call in cursor.execute.call_args_list))

    def test_postgres_creator_admin_membership_is_bound_and_recorded(self):
        _, bundle = self.prepare()
        cursor = MagicMock()
        role_row = (bundle["manifest"]["role"], False, False, False, False, True, False, False, 5,
                    self.now+timedelta(days=90), None, "image-archive-v2:"+bundle["manifest_sha256"])
        cursor.fetchone.side_effect = [role_row, (0,), (True,False,True,False,False,False), (0,)]
        cursor.fetchall.side_effect = [[("owner", True, "false", "false")], self.expected_privileges()]
        audit = runtime.audit_role(cursor, bundle)
        self.assertEqual(audit, {"outbound_memberships": 0,
          "creator_memberships": [{"member": "owner", "admin_option": True, "inherit_option": False, "set_option": False}]})

    def test_runtime_outbound_membership_and_unknown_incoming_member_rejected(self):
        _, bundle = self.prepare()
        role_row = (bundle["manifest"]["role"], False, False, False, False, True, False, False, 5,
                    self.now+timedelta(days=90), None, "image-archive-v2:"+bundle["manifest_sha256"])
        cursor = MagicMock()
        cursor.fetchone.side_effect = [role_row, (1,)]
        with self.assertRaisesRegex(runtime.RuntimeError, "membership_conflict"):
            runtime.audit_role(cursor, bundle)
        cursor = MagicMock()
        cursor.fetchone.side_effect = [role_row, (0,)]
        cursor.fetchall.return_value = [("other_owner", True, "false", "false")]
        with self.assertRaisesRegex(runtime.RuntimeError, "incoming_role_member"):
            runtime.audit_role(cursor, bundle)

    def test_public_escalation_surface_is_specific_and_not_auto_revoked(self):
        _, bundle = self.prepare()
        cursor = MagicMock()
        role_row = (bundle["manifest"]["role"], False, False, False, False, True, False, False, 5,
                    self.now+timedelta(days=90), None, "image-archive-v2:"+bundle["manifest_sha256"])
        cursor.fetchone.side_effect = [role_row, (0,), (True,False,True,True,False,False)]
        cursor.fetchall.side_effect = [[], self.expected_privileges()]
        with self.assertRaisesRegex(runtime.RuntimeError, "schema_create_granted"):
            runtime.audit_role(cursor, bundle)
        self.assertFalse(any("REVOKE" in str(call.args[0]) for call in cursor.execute.call_args_list))

    def test_setup_lock_and_failed_login_do_not_write_success_receipt(self):
        _, bundle = self.prepare()
        target = self.config["target"]
        (target / ".provision.lock").write_text("another caller")
        with self.assertRaisesRegex(runtime.RuntimeError, "locked"):
            runtime.execute_bundle(bundle, MagicMock(), MagicMock(), execute=True)
        self.assertTrue((target / ".provision.lock").exists())
        (target / ".provision.lock").unlink()
        with patch.object(runtime, "provision", return_value={"role_created": True}), self.assertRaises(OSError):
            runtime.execute_bundle(bundle, MagicMock(), MagicMock(side_effect=OSError("synthetic")), execute=True)
        self.assertFalse(list(target.glob("verification-*/receipt.json")))
        self.assertEqual(len(list(target.glob("verification-*/failed.json"))), 1)
        self.assertFalse((target / ".provision.lock").exists())

    def test_remote_success_requires_restricted_login_and_verification(self):
        _, bundle = self.prepare()
        login = MagicMock()
        with patch.object(runtime, "provision", return_value={"role_created": True, "budget_migration_applied": True}), patch.object(runtime, "verify_login", return_value={"outbound_memberships": 0, "creator_memberships": []}) as verify:
            result = runtime.execute_bundle(bundle, MagicMock(), lambda _: login, execute=True)
        verify.assert_called_once_with(login, bundle)
        login.close.assert_called_once()
        self.assertTrue(result["restricted_login_verified"])
        self.assertNotIn(bundle["values"]["owner-client.json"]["token"], json.dumps(result))

    def test_verify_mode_never_provisions(self):
        _, bundle = self.prepare()
        with patch.object(runtime, "provision", side_effect=AssertionError("write")), patch.object(runtime, "audit_role"), patch.object(runtime, "verify_snapshot"), patch.object(runtime, "verify_login", return_value={"outbound_memberships": 0, "creator_memberships": []}):
            result = runtime.execute_bundle(bundle, MagicMock(), lambda _: MagicMock(), verify_only=True)
        self.assertFalse(result["role_created"])

    def test_main_dryrun_no_credentials_and_invalid_execute_flags(self):
        arguments = ["--plan", "synthetic", "--owner-email", "owner@example.test"]
        with patch.object(runtime, "configuration", return_value=self.config), patch.object(runtime.cloud, "credentials", side_effect=AssertionError("no env")), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(runtime.main(arguments), 0)
            self.assertEqual(runtime.main(arguments+["--execute"]), 1)


if __name__ == "__main__":
    unittest.main()
