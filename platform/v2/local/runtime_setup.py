"""Prepare private v2 runtime files; provision/verify only with explicit flags.

No deployment, secret upload, Access-app creation, image upload or inference.
Windows files inherit the ignored workspace secret-store ACL, like local .env.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import secrets
import sys
from urllib.parse import quote, urlencode, urlsplit
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cloud_snapshot as cloud

ROOT = Path(__file__).resolve().parents[3]
SCHEMA = "image-archive-v2-runtime-1"
FILES = ("role-credentials.json", "owner-client.json", "worker-secrets.json", "wrangler.runtime.json")
SCOPES = ["rag:search", "archive:read"]
GRANTS = {"snapshots": {"SELECT"}, "items": {"SELECT"}, "query_vectors": {"SELECT"},
          "api_daily_budget": {"SELECT", "INSERT", "UPDATE"},
          "api_query_receipts": {"SELECT", "INSERT", "UPDATE"}, "api_model_guard": {"SELECT", "UPDATE"}}
RuntimeError = cloud.SnapshotError


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def exclusive_json(path, value):
    handle = (path.open("xb") if os.name == "nt" else
              os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb"))
    with handle:
        handle.write(cloud.encoded(value))
        handle.flush()
        os.fsync(handle.fileno())


def configuration(plan_path, owner_email, root=ROOT, *, output_dir=None, team_domain=None, policy_aud=None):
    root = Path(root).resolve()
    plan = cloud.read_plan(plan_path, root)
    email = owner_email.lower() if isinstance(owner_email, str) else ""
    if not re.fullmatch(r"[a-z0-9][a-z0-9._+-]*@[a-z0-9.-]+\.[a-z]{2,63}", email):
        raise RuntimeError("explicit_owner_email_required")
    base_path, access_path = root / "platform/v2/wrangler.jsonc", root / "deploy/cloudflare-staging/wrangler.jsonc"
    base, access = json.loads(base_path.read_bytes()), json.loads(access_path.read_bytes())
    team = team_domain or access["vars"].get("TEAM_DOMAIN")
    audience = policy_aud or access["vars"].get("POLICY_AUD")
    if (team != access["vars"].get("TEAM_DOMAIN") or audience != access["vars"].get("POLICY_AUD") or
        not re.fullmatch(r"https://[a-z0-9-]+\.cloudflareaccess\.com", team or "") or
        not re.fullmatch(r"[a-f0-9]{64}", audience or "")):
        raise RuntimeError("existing_access_configuration_required")
    buckets = base.get("r2_buckets")
    existing_buckets = {r["bucket_name"] for r in access.get("r2_buckets", [])}
    if (not isinstance(buckets, list) or len(buckets) != 1 or buckets[0].get("binding") != "PRIVATE_MEDIA" or
        buckets[0].get("bucket_name") not in existing_buckets or "assets" in base or "routes" in base):
        raise RuntimeError("existing_private_r2_binding_required")
    sid = plan["manifest"]["snapshot_id"]
    main = (root / "platform/v2/worker/index.js").resolve(strict=True)
    if not main.is_relative_to(root):
        raise RuntimeError("worker_main_outside_archive")
    budget_path = root / "db/v2/0002_api_budget.sql"
    identity = {"schema_version": SCHEMA, "snapshot_id": sid, "snapshot_manifest_sha256": plan["manifest_sha256"],
      "owner_email": email, "team_domain": team, "policy_aud": audience,
      "setup_code_sha256": cloud.sha(Path(__file__).read_bytes()),
      "base_wrangler_sha256": cloud.sha(base_path.read_bytes()), "access_config_sha256": cloud.sha(access_path.read_bytes()),
      "budget_migration_sha256": cloud.sha(budget_path.read_bytes())}
    base["main"] = str(main)
    base["workers_dev"], base["preview_urls"] = True, False
    base["observability"] = {"enabled": False}
    base["vars"].update(PRIVATE_API_ENABLED="true", LIVE_QUERY_EMBEDDING_ENABLED="false",
      ACCESS_JWT_REQUIRED="true", TEAM_DOMAIN=team, POLICY_AUD=audience,
      OWNER_EMAIL_ALLOWLIST=json.dumps([email]), SNAPSHOT_ID=sid,
      SNAPSHOT_MANIFEST_SHA256=plan["manifest_sha256"], TEXT_COLLECTION=plan["manifest"]["qdrant_collections"]["text"])
    role = "v2_api_" + sid[:12]
    target = cloud.private_path((output_dir or root / "data/private-research/v2/runtime") / sid, root)
    return {"root": root, "plan": plan, "identity": identity, "wrangler": base, "role": role,
            "target": target, "budget_path": budget_path}


def restricted_dsn(admin_dsn, role, password):
    params = cloud.neon_parameters(admin_dsn)
    if params["port"] != "5432" or params["sslmode"] not in ("require", "verify-full"):
        raise RuntimeError("worker_neon_tls_or_port_unsupported")
    return ("postgresql://" + quote(role, safe="") + ":" + quote(password, safe="") + "@" + params["host"] + ":5432/" +
            quote(params["dbname"], safe="") + "?" + urlencode({"sslmode": params["sslmode"], "channel_binding": params["channel_binding"]}))


def prepare(config, *, apply=False, credentials=None, now=None):
    target = config["target"]
    if not apply:
        return {"status": "dry_run", "runtime_directory": str(target), "role": config["role"],
                "new_credentials": 0, "network_calls": 0, "new_embedding_calls": 0}
    if target.exists():
        bundle = read_bundle(config)
        return safe_result(bundle, "prepared_reused")
    if credentials is None:
        raise RuntimeError("explicit_local_credentials_required")
    created = (now or utc_now()).replace(microsecond=0)
    expiry = iso(created + timedelta(days=90))
    admin_dsn = credentials.get("DATABASE_URL") or credentials.get("NEON_DATABASE_KEY")
    provisioning_owner = cloud.neon_parameters(admin_dsn)["user"]
    password, token = secrets.token_urlsafe(32), "iar_v2_" + secrets.token_urlsafe(32)
    dsn = restricted_dsn(admin_dsn, config["role"], password)
    endpoint, key = credentials.get("QDRANT_ENDPOINT"), credentials.get("QDRANT_API_KEY")
    if not isinstance(key, str) or not key:
        raise RuntimeError("qdrant_runtime_key_required")
    cloud.Qdrant(endpoint, key)  # Endpoint validation only; no request.
    descriptor = {"id": "owner_" + config["plan"]["manifest"]["snapshot_id"][:12],
                  "sha256": cloud.sha(token.encode()), "scopes": SCOPES, "expires_at": expiry, "revoked": False}
    values = {"role-credentials.json": {"role": config["role"], "password": password, "database_url": dsn, "expires_at": expiry},
      "owner-client.json": {"token": token, "token_id": descriptor["id"], "scopes": SCOPES, "expires_at": expiry,
                            "worker_name": config["wrangler"]["name"]},
      "worker-secrets.json": {"DATABASE_URL": dsn, "QDRANT_ENDPOINT": endpoint, "QDRANT_API_KEY": key,
                              "API_TOKEN_HASHES": json.dumps([descriptor], separators=(",", ":"))},
      "wrangler.runtime.json": config["wrangler"]}
    manifest = {"identity": config["identity"], "role": config["role"], "created_at": iso(created), "expires_at": expiry,
      "provisioning_owner_role": provisioning_owner,
      "files": {name: cloud.sha(cloud.encoded(value)) for name, value in values.items()},
      "status": "prepared_not_remote_verified", "new_embedding_calls": 0, "deployment_performed": False}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir(mode=0o777 if os.name == "nt" else 0o700)
    for name, value in values.items():
        exclusive_json(target / name, value)
    exclusive_json(target / "runtime-manifest.json", manifest)  # completion marker
    return safe_result(read_bundle(config, now=created), "prepared")


def read_bundle(config, *, now=None):
    root, target = config["root"], config["target"]
    raw = cloud.private_path(target / "runtime-manifest.json", root).read_bytes()
    manifest = json.loads(raw)
    if (manifest.get("identity") != config["identity"] or manifest.get("role") != config["role"] or
        not isinstance(manifest.get("provisioning_owner_role"), str) or not manifest["provisioning_owner_role"] or
        manifest["provisioning_owner_role"] == config["role"] or set(manifest.get("files", {})) != set(FILES)):
        raise RuntimeError("runtime_manifest_conflict")
    values = {}
    for name in FILES:
        body = cloud.private_path(target / name, root).read_bytes()
        if cloud.sha(body) != manifest["files"][name]:
            raise RuntimeError("runtime_file_drift")
        values[name] = json.loads(body)
    role, client, worker = (values[name] for name in FILES[:3])
    issued = datetime.fromisoformat(manifest["created_at"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(manifest["expires_at"].replace("Z", "+00:00"))
    if expires-issued != timedelta(days=90) or expires <= (now or utc_now()):
        raise RuntimeError("runtime_credentials_expired_or_invalid")
    if (role["role"] != config["role"] or role["database_url"] != worker.get("DATABASE_URL") or
        role["expires_at"] != manifest["expires_at"] or client["expires_at"] != manifest["expires_at"] or
        set(worker) != {"DATABASE_URL", "QDRANT_ENDPOINT", "QDRANT_API_KEY", "API_TOKEN_HASHES"} or
        values["wrangler.runtime.json"] != config["wrangler"]):
        raise RuntimeError("runtime_secret_contract_invalid")
    params = cloud.neon_parameters(role["database_url"])
    if params["user"] != config["role"] or params.get("password") != role["password"]:
        raise RuntimeError("owner_database_credentials_refused")
    token = client["token"]
    try:
        token_bytes = base64.urlsafe_b64decode(token.removeprefix("iar_v2_") + "=" * (-len(token.removeprefix("iar_v2_")) % 4))
    except (ValueError, TypeError):
        raise RuntimeError("invalid_owner_token") from None
    descriptor = {"id": client["token_id"], "sha256": cloud.sha(token.encode()), "scopes": SCOPES,
                  "expires_at": manifest["expires_at"], "revoked": False}
    if not token.startswith("iar_v2_") or len(token_bytes) != 32 or client["scopes"] != SCOPES or json.loads(worker["API_TOKEN_HASHES"]) != [descriptor]:
        raise RuntimeError("invalid_owner_token")
    return {"manifest": manifest, "manifest_sha256": cloud.sha(raw), "values": values, "config": config}


def safe_result(bundle, status):
    return {"status": status, "runtime_directory": str(bundle["config"]["target"]),
            "runtime_manifest_sha256": bundle["manifest_sha256"], "role": bundle["manifest"]["role"],
            "expires_at": bundle["manifest"]["expires_at"], "new_embedding_calls": 0, "deployment_performed": False}


def verify_snapshot(cur, plan):
    cur.execute("SELECT manifest_sha256,manifest_json,state FROM image_archive_v2.snapshots WHERE snapshot_id=%s", (plan["manifest"]["snapshot_id"],))
    if cur.fetchone() != (plan["manifest_sha256"], plan["manifest"], "ready"):
        raise RuntimeError("exact_ready_snapshot_required")


def check_table_privileges(rows):
    expected = {("image_archive_v2", table, privilege) for table, privileges in GRANTS.items() for privilege in privileges}
    table_grants, any_grants = set(), set()
    for schema, table, privilege, full_table, any_column in rows:
        key = (schema, table, privilege)
        if full_table:
            table_grants.add(key)
        if full_table or any_column:
            any_grants.add(key)
    if table_grants != expected or any_grants != expected:
        raise RuntimeError("runtime_table_privilege_conflict")


def audit_role(cur, bundle):
    role = bundle["manifest"]["role"]
    cur.execute("""SELECT rolname,rolsuper,rolinherit,rolcreaterole,rolcreatedb,rolcanlogin,
      rolreplication,rolbypassrls,rolconnlimit,rolvaliduntil,rolconfig,shobj_description(oid,'pg_authid')
      FROM pg_roles WHERE rolname=%s""", (role,))
    row = cur.fetchone()
    expires = datetime.fromisoformat(bundle["manifest"]["expires_at"].replace("Z", "+00:00"))
    if (not row or row[:9] != (role, False, False, False, False, True, False, False, 5) or
        row[9] != expires or row[10] not in (None, []) or row[11] != "image-archive-v2:" + bundle["manifest_sha256"]):
        raise RuntimeError("runtime_role_attribute_or_identity_conflict")
    cur.execute("SELECT count(*) FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=%s)", (role,))
    if cur.fetchone() != (0,):
        raise RuntimeError("runtime_role_membership_conflict")
    # PostgreSQL 16+ may automatically grant the creator ADMIN OPTION on a new
    # role. That incoming edge does not make the runtime role a privileged member.
    # Permit only this exact bound provisioning owner; record its observed flags.
    cur.execute("""SELECT r.rolname,m.admin_option,to_jsonb(m)->>'inherit_option',to_jsonb(m)->>'set_option'
      FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member
      WHERE m.roleid=(SELECT oid FROM pg_roles WHERE rolname=%s) ORDER BY r.rolname""", (role,))
    incoming = cur.fetchall()
    if len(incoming) > 1 or any(member != bundle["manifest"]["provisioning_owner_role"] or admin is not True or
      inherit not in (None, "true", "false") or can_set not in (None, "true", "false")
      for member, admin, inherit, can_set in incoming):
        raise RuntimeError("runtime_unexpected_incoming_role_member")
    membership_audit = [{"member": member, "admin_option": admin,
      "inherit_option": None if inherit is None else inherit == "true", "set_option": None if can_set is None else can_set == "true"}
      for member, admin, inherit, can_set in incoming]
    cur.execute("""SELECT n.nspname,c.relname,p.priv,has_table_privilege(%s,c.oid,p.priv),
      CASE WHEN p.priv IN ('SELECT','INSERT','UPDATE','REFERENCES') THEN has_any_column_privilege(%s,c.oid,p.priv) ELSE false END
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      CROSS JOIN (VALUES('SELECT'),('INSERT'),('UPDATE'),('DELETE'),('TRUNCATE'),('REFERENCES'),('TRIGGER')) p(priv)
      WHERE n.nspname NOT LIKE 'pg_%%' AND n.nspname<>'information_schema' AND c.relkind IN ('r','p','v','m','f')""", (role, role))
    check_table_privileges(cur.fetchall())
    cur.execute("""SELECT
      has_database_privilege(%s,current_database(),'CONNECT'),has_database_privilege(%s,current_database(),'CREATE'),
      has_schema_privilege(%s,'image_archive_v2','USAGE'),
      EXISTS(SELECT 1 FROM pg_namespace WHERE nspname NOT LIKE 'pg_%%' AND nspname<>'information_schema' AND has_schema_privilege(%s,oid,'CREATE')),
      EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='S' AND n.nspname NOT LIKE 'pg_%%' AND
        (has_sequence_privilege(%s,c.oid,'USAGE') OR has_sequence_privilege(%s,c.oid,'SELECT') OR has_sequence_privilege(%s,c.oid,'UPDATE'))),
      EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE p.prosecdef AND n.nspname NOT LIKE 'pg_%%' AND
        n.nspname<>'information_schema' AND has_function_privilege(%s,p.oid,'EXECUTE'))""", (role,)*8)
    surfaces = cur.fetchone()
    expected = (True, False, True, False, False, False)
    reasons = ("database_connect_missing", "database_create_granted", "schema_usage_missing",
               "schema_create_granted", "sequence_privileges_granted", "security_definer_execute_granted")
    if not surfaces or len(surfaces) != len(expected):
        raise RuntimeError("runtime_privilege_audit_incomplete")
    for actual, required, reason in zip(surfaces, expected, reasons):
        if actual is not required:
            raise RuntimeError("runtime_" + reason)
    cur.execute("""SELECT count(*) FROM (
      SELECT relowner AS owner FROM pg_class UNION ALL SELECT nspowner FROM pg_namespace
      UNION ALL SELECT proowner FROM pg_proc UNION ALL SELECT datdba FROM pg_database) objects
      WHERE owner=(SELECT oid FROM pg_roles WHERE rolname=%s)""", (role,))
    if cur.fetchone() != (0,):
        raise RuntimeError("runtime_object_ownership_conflict")
    return {"outbound_memberships": 0, "creator_memberships": membership_audit}


def provision(connection, bundle, *, execute=False):
    if not execute:
        raise RuntimeError("explicit_execute_required")
    from psycopg2 import sql
    config, manifest = bundle["config"], bundle["manifest"]
    plan, role = config["plan"], manifest["role"]
    migration = config["budget_path"].read_bytes()
    if cloud.sha(migration) != config["identity"]["budget_migration_sha256"]:
        raise RuntimeError("budget_migration_drift")
    created, migrated = False, False
    with connection, connection.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout='5s'")
        cur.execute("SET LOCAL statement_timeout='30s'")
        cur.execute("SELECT current_user")
        if cur.fetchone() != (manifest["provisioning_owner_role"],):
            raise RuntimeError("bound_provisioning_owner_required")
        cur.execute("SELECT pg_advisory_xact_lock(68264392470331)")
        verify_snapshot(cur, plan)
        cur.execute("SELECT sha256 FROM image_archive_v2.schema_migrations WHERE version='0001'")
        if cur.fetchone() != (plan["manifest"]["identity"]["bindings"]["db/v2/0001_private_library.sql"],):
            raise RuntimeError("library_schema_migration_conflict")
        cur.execute("SELECT sha256 FROM image_archive_v2.schema_migrations WHERE version='0002'")
        prior = cur.fetchone()
        if prior is not None and prior != (cloud.sha(migration),):
            raise RuntimeError("budget_schema_migration_conflict")
        if prior is None:
            cur.execute("SELECT to_regclass('image_archive_v2.api_daily_budget'),to_regclass('image_archive_v2.api_query_receipts'),to_regclass('image_archive_v2.api_model_guard')")
            if cur.fetchone() != (None, None, None):
                raise RuntimeError("untracked_budget_tables_refused")
            cur.execute(migration.decode())
            cur.execute("INSERT INTO image_archive_v2.schema_migrations VALUES('0002',%s)", (cloud.sha(migration),))
            migrated = True
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,))
        if cur.fetchone() is None:
            role_ident = sql.Identifier(role)
            cur.execute(sql.SQL("CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 5 PASSWORD %s VALID UNTIL %s").format(role_ident),
                        (bundle["values"]["role-credentials.json"]["password"], manifest["expires_at"]))
            cur.execute(sql.SQL("COMMENT ON ROLE {} IS %s").format(role_ident), ("image-archive-v2:" + bundle["manifest_sha256"],))
            database = cloud.neon_parameters(bundle["values"]["role-credentials.json"]["database_url"])["dbname"]
            cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), role_ident))
            cur.execute(sql.SQL("GRANT USAGE ON SCHEMA image_archive_v2 TO {}").format(role_ident))
            for table, privileges in GRANTS.items():
                cur.execute(sql.SQL("GRANT {} ON TABLE image_archive_v2.{} TO {}").format(sql.SQL(",".join(sorted(privileges))), sql.Identifier(table), role_ident))
            created = True
        # Existing roles are NEVER altered, regranted or password-reset.
        audit_role(cur, bundle)
    return {"role_created": created, "budget_migration_applied": migrated}


def verify_login(connection, bundle):
    connection.set_session(readonly=True)
    with connection, connection.cursor() as cur:
        cur.execute("SELECT current_user")
        if cur.fetchone() != (bundle["manifest"]["role"],):
            raise RuntimeError("restricted_role_login_required")
        verify_snapshot(cur, bundle["config"]["plan"])
        membership_audit = audit_role(cur, bundle)
        cur.execute("SELECT count(*),count(*) FILTER(WHERE text_ready),count(DISTINCT group_id) FROM image_archive_v2.items WHERE snapshot_id=%s", (bundle["config"]["plan"]["manifest"]["snapshot_id"],))
        counts = bundle["config"]["plan"]["manifest"]["counts"]
        if cur.fetchone() != (counts["items"], counts["text_vectors"], counts["groups"]):
            raise RuntimeError("runtime_item_readback_mismatch")
        cur.execute("SELECT count(*) FROM image_archive_v2.query_vectors WHERE snapshot_id=%s AND model='voyage-4-lite' AND dimension=512 AND jsonb_array_length(vector_json)=512", (bundle["config"]["plan"]["manifest"]["snapshot_id"],))
        if cur.fetchone() != (counts["queries"],):
            raise RuntimeError("runtime_query_readback_mismatch")
    return membership_audit


def execute_bundle(bundle, admin_connection, login_factory, *, execute=False, verify_only=False):
    if execute == verify_only:
        raise RuntimeError("explicit_execute_or_verify_required")
    target = bundle["config"]["target"]
    lock = cloud.private_path(target / ".provision.lock", bundle["config"]["root"])
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError("runtime_setup_locked") from None
    os.close(fd)
    attempt = cloud.private_path(target / ("verification-" + uuid.uuid4().hex), bundle["config"]["root"])
    try:
        attempt.mkdir(mode=0o777 if os.name == "nt" else 0o700)
        exclusive_json(attempt / "reservation.json", {"runtime_manifest_sha256": bundle["manifest_sha256"], "execute": execute, "new_embedding_calls": 0})
        if verify_only:
            admin_connection.set_session(readonly=True)
            with admin_connection, admin_connection.cursor() as cur:
                verify_snapshot(cur, bundle["config"]["plan"])
                audit_role(cur, bundle)
            changes = {"role_created": False, "budget_migration_applied": False}
        else:
            changes = provision(admin_connection, bundle, execute=True)
        login = login_factory(cloud.neon_parameters(bundle["values"]["role-credentials.json"]["database_url"]))
        try:
            membership_audit = verify_login(login, bundle)
        finally:
            login.close()
        result = {**safe_result(bundle, "remote_verified"), **changes, "restricted_login_verified": True,
                  "privileges_verified": True, "snapshot_verified": True, "verified_at": iso(utc_now()),
                  "role_membership_audit": membership_audit}
        exclusive_json(attempt / "receipt.json", result)
        return result
    except Exception:
        exclusive_json(attempt / "failed.json", {"status": "failed_requires_manual_review", "automatic_retry": False})
        raise
    finally:
        lock.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--owner-email", required=True)
    parser.add_argument("--archive-root", type=Path, default=ROOT)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--env-path", type=Path)
    parser.add_argument("--team-domain")
    parser.add_argument("--policy-aud")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    try:
        if (args.execute and not args.apply) or (args.verify and (args.apply or args.execute)):
            raise RuntimeError("invalid_runtime_mode_flags")
        config = configuration(args.plan, args.owner_email, args.archive_root, output_dir=args.output_dir,
                               team_domain=args.team_domain, policy_aud=args.policy_aud)
        if args.execute or args.verify:
            bundle = read_bundle(config)
            values = cloud.credentials(args.env_path or args.archive_root / ".env")
            admin = cloud.neon_parameters(values.get("DATABASE_URL") or values.get("NEON_DATABASE_KEY"))
            restricted = cloud.neon_parameters(bundle["values"]["role-credentials.json"]["database_url"])
            if any(admin[key] != restricted[key] for key in ("host", "port", "dbname")) or admin["user"] == restricted["user"]:
                raise RuntimeError("admin_runtime_database_boundary_mismatch")
            import psycopg2
            connection = psycopg2.connect(**admin, connect_timeout=10)
            try:
                result = execute_bundle(bundle, connection, lambda params: psycopg2.connect(**params, connect_timeout=10), execute=args.execute, verify_only=args.verify)
            finally:
                connection.close()
        else:
            values = cloud.credentials(args.env_path or args.archive_root / ".env") if args.apply and not config["target"].exists() else None
            result = prepare(config, apply=args.apply, credentials=values)
        print(cloud.encoded(result).decode(), end="")
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, RuntimeError) else "runtime_setup_failed"
        print(json.dumps({"status": "failed", "error_code": code, "new_embedding_calls": 0}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
