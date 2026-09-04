"""Immutable private v2 export and explicit bounded sync. Never embeds anything.

Run by path: platform intentionally has no __init__.py (stdlib name collision).
An offline dry-run reads only pinned local evidence. --apply writes a private
plan; network requires --preflight OR both --apply --execute with --plan.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import struct
import sys
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from image_rag_eval.shared_vector_cache import lookup_shared_vectors, _key as image_key
from image_rag_eval.text_embedding_run import load_manifest_vectors, cache_key

SCHEMA = "private-cloud-snapshot-2"
BASE = "data/private-research/image-rag-admin/embedding-budget/2026-09-04-v1"
SOURCE = "data/private-research/image-rag-admin/metadata-candidates/full-library-v3/snapshots/3cee2517be877b63638d18cda1bda55689cc07086410936a9d7a3d11a8cae8d4/library.sqlite3"
SEARCH = BASE + "/text-run/vector-store/ab47aa3b6ec3b06d36dd446683112c9ae0d9986bbbf22ef6f2ab617bab5a89c4"
IMAGE_REVISION = "cce68cebcdb92eb3249e441175a165e63fc860a70ed2189d55d7e1dc8b030dc3"
ITEM_COLUMNS = ("snapshot_id", "item_id", "group_id", "representative_id", "original_prompt", "rights_json",
                "metadata_json", "human_note", "text_ready", "retrieval_text", "private_data")
QUERY_COLUMNS = ("snapshot_id", "query_id", "query_text", "model", "dimension", "vector_json")
PAYLOAD_KEYS = {"item_id", "group_id", "representative_id", "snapshot_id", "image_approved"}
FILES = ("items.jsonl", "queries.jsonl", "text-points.jsonl", "image-points.jsonl")
HEX = re.compile(r"^[a-f0-9]{64}$")


class SnapshotError(ValueError):
    """Messages are fixed safe error codes, never transport/server bodies."""


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def private_path(path, root):
    path, root = Path(path).absolute(), Path(root).resolve()
    private_root = root / "data/private-research"
    try:
        relative = path.relative_to(private_root)
    except ValueError:
        raise SnapshotError("private_path_required") from None
    if ".." in relative.parts:
        raise SnapshotError("private_path_required")
    # root is the trusted resolved archive boundary. Do not stat unrelated
    # ancestors (including drive roots), which may be inaccessible to a launch
    # context even while this workspace is readable. Check each descendant.
    current = root
    for part in ("data", "private-research", *relative.parts):
        current = current / part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise SnapshotError("symlink_or_junction_refused")
    path = path.resolve()
    if not path.is_relative_to(private_root):
        raise SnapshotError("private_path_required")
    return path


def vector(values, dimension):
    if (not isinstance(values, list) or len(values) != dimension or
        any(type(v) not in (int, float) or not math.isfinite(v) for v in values) or
        not math.isfinite(sum(v*v for v in values)) or not any(values)):
        raise SnapshotError("invalid_vector")
    return [float(v) for v in values]


def f32(values, dimension):
    try:
        return list(struct.unpack(f"<{dimension}f", struct.pack(f"<{dimension}f", *vector(values, dimension))))
    except (OverflowError, struct.error):
        raise SnapshotError("invalid_float32_vector") from None


@contextmanager
def connect_ro(path):
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise SnapshotError("uncheckpointed_database")
    con = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()


def source_rows(con):
    rows = []
    query = """SELECT i.*,p.original_text,a.candidate_id,a.result_schema,a.raw_json analysis_raw,
      a.effective_json,a.raw_sha256,a.effective_sha256,a.review_status,a.metadata_human_approved,
      a.public_eligible metadata_public,n.memo,n.provenance memo_provenance,n.source_commit_id memo_commit,
      g.group_id,g.is_representative,ag.representative_item_id
      FROM source_items i JOIN prompts p ON p.sha256=i.prompt_sha256
      JOIN analysis_results a ON a.item_id=i.item_id LEFT JOIN human_notes n ON n.item_id=i.item_id
      LEFT JOIN group_memberships g ON g.item_id=i.item_id
      LEFT JOIN approval_groups ag ON ag.group_id=g.group_id
      WHERE i.approval_state='image_approved' ORDER BY i.item_id"""
    for row in con.execute(query):
        row = dict(row)
        if (sha(row["original_text"].encode()) != row["prompt_sha256"] or row["public_eligible"] or
            row["metadata_public"] or row["metadata_human_approved"]):
            raise SnapshotError("source_identity_or_approval_drift")
        raw, rights = json.loads(row["raw_json"]), json.loads(row["rights_json"])
        qa = [dict(q) for q in con.execute("SELECT ordinal,field_path,raw_json FROM candidate_qa WHERE candidate_id=? ORDER BY ordinal", (row["candidate_id"],))]
        qa = [{**q, "raw_json": json.loads(q["raw_json"])} for q in qa]
        rows.append({"item_id": row["item_id"], "group_id": row["group_id"] or row["item_id"],
          "representative_id": row["representative_item_id"] or row["item_id"],
          "original_prompt": row["original_text"], "rights_json": rights,
          "metadata_json": {"raw": json.loads(row["analysis_raw"]), "effective": json.loads(row["effective_json"]),
                            "qa": qa, "review_status": row["review_status"], "metadata_human_approved": False, "public_eligible": False},
          "human_note": row["memo"], "private_data": {
            "style_id": row["style_id"], "title": raw.get("handoff_record", {}).get("title") or raw.get("baseline_source_record", {}).get("title") or row["style_id"],
            "source_url": rights.get("source_url"), "source_name": rights.get("source_name"),
            "prepared_sha256": row["prepared_sha256"], "original_sha256": row["original_sha256"],
            "prompt_sha256": row["prompt_sha256"], "source_run_id": row["source_run_id"], "source_record": raw,
            "candidate_id": row["candidate_id"], "result_schema": row["result_schema"],
            "analysis_raw_sha256": row["raw_sha256"], "analysis_effective_sha256": row["effective_sha256"],
            "memo_provenance": row["memo_provenance"], "memo_source_commit_id": row["memo_commit"], "public_eligible": False}})
    if len({r["item_id"] for r in rows}) != len(rows):
        raise SnapshotError("ambiguous_item_metadata_or_group")
    return rows


def assemble(items, text_vectors, image_vectors, queries, bindings):
    identity = {"schema_version": SCHEMA, "bindings": bindings,
                "models": {"text": {"name": "voyage-4-lite", "dimension": 512},
                           "image": {"name": "voyage-multimodal-3.5", "dimension": 1024}}}
    sid = sha(encoded(identity))
    indexed = {i["item_id"]: i for i in items}
    if not items or len(indexed) != len(items) or len(items) > 379 or set(image_vectors) != set(indexed):
        raise SnapshotError("invalid_approved_scope")
    if set(text_vectors) != {i["item_id"] for i in items if i["text_ready"]}:
        raise SnapshotError("text_ready_vector_mismatch")
    text_points, image_points = [], []
    for row in items:
        rep = indexed.get(row["representative_id"])
        if not rep or rep["group_id"] != row["group_id"] or rep["representative_id"] != rep["item_id"]:
            raise SnapshotError("cross_group_representative")
        row["snapshot_id"] = sid
        payload = {k: row[k] for k in PAYLOAD_KEYS - {"image_approved"}}
        payload["image_approved"] = True
        point = {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, sid + ":" + row["item_id"])), "payload": payload}
        image_points.append({**point, "vector": vector(image_vectors[row["item_id"]], 1024)})
        if row["text_ready"]:
            text_points.append({**point, "vector": vector(text_vectors[row["item_id"]], 512)})
    for query in queries:
        query.update(snapshot_id=sid, model="voyage-4-lite", dimension=512)
        query["vector_json"] = vector(query["vector_json"], 512)
    bodies = dict(zip(FILES, (items, queries, text_points, image_points)))
    bodies = {name: b"".join(encoded(row) for row in rows) for name, rows in bodies.items()}
    manifest = {"identity": identity, "snapshot_id": sid,
      "files": {name: {"sha256": sha(body), "bytes": len(body)} for name, body in bodies.items()},
      "counts": {"items": len(items), "text_vectors": len(text_points), "image_vectors": len(image_points),
                 "queries": len(queries), "groups": len({r["group_id"] for r in items}),
                 "text_deferred": len(items)-len(text_points)},
      "qdrant_collections": {"text": f"image_archive_v2_{sid}_text512", "image": f"image_archive_v2_{sid}_image1024"},
      "media_manifest": [{"item_id": row["item_id"], **{key: row["private_data"][key] for key in
        ("prepared_sha256", "prepared_relative_path", "prepared_mime_type", "prepared_bytes")}} for row in items],
      "privacy": "owner_private", "release_eligible": False, "new_embedding_calls": 0}
    return manifest, bodies


def write_plan(target, manifest, bodies, root):
    """Exclusive final-path creation; manifest last is the completion marker.

    Windows must inherit the workspace ACL (as .env does), not tempfile's
    creator-only ACL, because offline and elevated launch identities differ.
    A partial directory is never overwritten or silently cleaned up.
    """
    target = private_path(target, root)
    files = {**bodies, "manifest.json": encoded(manifest)}
    if target.exists():
        for name, raw in files.items():
            file = private_path(target / name, root)
            if not file.is_file() or file.read_bytes() != raw:
                raise SnapshotError("immutable_plan_conflict")
        return
    target.mkdir(mode=0o777 if os.name == "nt" else 0o700)
    for name, raw in files.items():
        file = target / name
        handle = (file.open("xb") if os.name == "nt" else
                  os.fdopen(os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"))
        with handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())


def build_snapshot_plan(root=ROOT, *, output_dir=None, apply=False):
    root = Path(root).resolve()
    source, search_dir = private_path(root / SOURCE, root), private_path(root / SEARCH, root)
    summary = json.loads((search_dir / "summary.json").read_bytes())
    search_db = private_path(search_dir / "search.sqlite3", root)
    bound = {SOURCE: sha(source.read_bytes()), str(search_db.relative_to(root)).replace("\\", "/"): sha(search_db.read_bytes()),
             SEARCH + "/summary.json": sha((search_dir / "summary.json").read_bytes()),
             "db/v2/0001_private_library.sql": sha((root / "db/v2/0001_private_library.sql").read_bytes()),
             "platform/v2/local/cloud_snapshot.py": sha(Path(__file__).read_bytes())}
    if bound[SOURCE] != summary["source_database_sha256"] or bound[search_db.relative_to(root).as_posix()] != summary["database_sha256"]:
        raise SnapshotError("frozen_database_drift")
    run = private_path(root / summary["provider_run_path"], root)
    full = load_manifest_vectors(run, summary["full_manifest_sha256"], archive_root=root)
    query_cache = load_manifest_vectors(run, summary["query_manifest_sha256"], archive_root=root)
    if full["receipt"]["pending_or_uncertain_requests"] or query_cache["receipt"]["pending_or_uncertain_requests"]:
        raise SnapshotError("uncertain_embedding_history")
    plan = private_path(root / BASE / "plans" / summary["plan_sha256"], root)
    plan_raw, body = (plan / "summary.json").read_bytes(), (plan / "documents.jsonl").read_bytes()
    if sha(plan_raw) != summary["plan_sha256"] or sha(body) != json.loads(plan_raw)["documents_sha256"]:
        raise SnapshotError("compact_plan_drift")
    planned = {r["item_id"]: r for r in map(json.loads, body.splitlines())}
    with connect_ro(source) as con:
        items = source_rows(con)
    text_vectors, queries = {}, []
    with connect_ro(search_db) as con:
        search_rows = {r["item_id"]: dict(r) for r in con.execute("SELECT d.*,v.cache_key,v.vector_f32le,v.vector_sha256 FROM documents d LEFT JOIN document_vectors m USING(item_id) LEFT JOIN text_vectors v USING(cache_key)")}
        if set(search_rows) != {r["item_id"] for r in items} or set(planned) != set(search_rows):
            raise SnapshotError("source_item_sets_differ")
        for item in items:
            ident = item["item_id"]
            row, doc = search_rows[ident], planned[ident]
            if (row["prompt_text"] != item["original_prompt"] or row["compact_text"] != doc["compact_text"] or
                row["group_key"] != item["group_id"] or row["representative_item_id"] != item["representative_id"] or
                row["prepared_sha256"] != item["private_data"]["prepared_sha256"]):
                raise SnapshotError("search_source_mapping_drift")
            ready = not doc["budget_blocked"]
            item.update(text_ready=ready, retrieval_text=doc["compact_text"])
            item["private_data"].update(prepared_relative_path=row["image_path"], text_status=doc["status"] if "status" in doc else ("ready" if ready else "text_deferred"))
            media = private_path(root / row["image_path"], root).read_bytes()
            mime = ("image/png" if media.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg" if media.startswith(b"\xff\xd8\xff")
                    else "image/webp" if media.startswith(b"RIFF") and media[8:12] == b"WEBP" else None)
            if mime is None or sha(media) != row["prepared_sha256"]:
                raise SnapshotError("prepared_media_type_or_hash_drift")
            item["private_data"].update(prepared_mime_type=mime, prepared_bytes=len(media))
            if ready:
                values = f32(full["vectors"]["compact:" + ident], 512)
                blob = struct.pack("<512f", *values)
                if blob != row["vector_f32le"] or sha(blob) != row["vector_sha256"] or row["cache_key"] != cache_key({"text": doc["compact_text"], "input_type": "document"}):
                    raise SnapshotError("text_cache_sqlite_drift")
                text_vectors[ident] = values
            elif row["vector_f32le"] is not None:
                raise SnapshotError("deferred_vector_present")
        query_inputs = {q["input_id"]: q for q in query_cache["manifest"]["documents"] if q["input_type"] == "query"}
        for row in con.execute("SELECT q.*,v.vector_f32le,v.vector_sha256 FROM query_vectors q JOIN text_vectors v USING(cache_key) ORDER BY query_id"):
            values = f32(query_cache["vectors"][row["query_id"]], 512)
            blob = struct.pack("<512f", *values)
            if (blob != row["vector_f32le"] or sha(blob) != row["vector_sha256"] or
                row["query_text"] != query_inputs[row["query_id"]]["text"] or row["cache_key"] != cache_key(query_inputs[row["query_id"]])):
                raise SnapshotError("query_cache_sqlite_drift")
            queries.append({"query_id": row["query_id"], "query_text": row["query_text"], "vector_json": values})
        if {q["query_id"] for q in queries} != set(query_inputs):
            raise SnapshotError("query_fixture_set_drift")
    requests = [{"provider": "voyage", "model": "voyage-multimodal-3.5", "dimensions": 1024,
      "image_sha256": r["private_data"]["prepared_sha256"], "text": "", "task": "RETRIEVAL_DOCUMENT"} for r in items]
    hits = lookup_shared_vectors(root, requests, revision_id=IMAGE_REVISION)
    images = {}
    for item, request in zip(items, requests):
        hit = hits[image_key(request)]
        if hit is None:
            raise SnapshotError("missing_image_cache_no_inference_allowed")
        images[item["item_id"]] = hit["vector"]
        item["private_data"]["image_cache_key"] = hit["key"]
        item["private_data"]["image_vector_sha256"] = hit["vector_sha256"]
    if (len(items), len(text_vectors), len(images), len(queries)) != (379, 377, 379, 11):
        raise SnapshotError("pinned_scope_counts_changed")
    bound.update(image_cache_revision=IMAGE_REVISION, full_manifest_sha256=summary["full_manifest_sha256"],
                 query_manifest_sha256=summary["query_manifest_sha256"], compact_plan_sha256=summary["plan_sha256"])
    for rel, digest in list(bound.items()):
        if "/" in rel and sha((root / rel).read_bytes()) != digest:
            raise SnapshotError("source_changed_during_export")
    manifest, bodies = assemble(items, text_vectors, images, queries, bound)
    output = private_path(output_dir or root / "data/private-research/v2/cloud-plans", root)
    target = private_path(output / sha(encoded(manifest)), root)
    if apply:
        output.mkdir(parents=True, exist_ok=True)
        write_plan(target, manifest, bodies, root)
    return {"status": "prepared" if apply else "dry_run", "path": str(target),
            "snapshot_id": manifest["snapshot_id"], "manifest_sha256": sha(encoded(manifest)),
            "counts": manifest["counts"], "qdrant_collections": manifest["qdrant_collections"],
            "network_calls": 0, "new_embedding_calls": 0}


def read_plan(path, root=ROOT):
    path = private_path(path, root)
    raw = private_path(path / "manifest.json", root).read_bytes()
    manifest = json.loads(raw)
    sid = manifest["snapshot_id"]
    if (not HEX.fullmatch(sid) or sid != sha(encoded(manifest["identity"])) or path.name != sha(raw) or
        manifest["identity"]["schema_version"] != SCHEMA or set(manifest["files"]) != set(FILES) or
        manifest.get("privacy") != "owner_private" or manifest.get("release_eligible") is not False or manifest.get("new_embedding_calls") != 0):
        raise SnapshotError("invalid_plan_identity")
    rows = {}
    for name in FILES:
        body = private_path(path / name, root).read_bytes()
        if manifest["files"][name] != {"sha256": sha(body), "bytes": len(body)}:
            raise SnapshotError("plan_file_drift")
        rows[name] = [json.loads(line) for line in body.splitlines()]
    items, queries, texts, images = (rows[f] for f in FILES)
    expected, _ = assemble(items, {p["payload"]["item_id"]: p["vector"] for p in texts},
                           {p["payload"]["item_id"]: p["vector"] for p in images}, queries, manifest["identity"]["bindings"])
    if expected != manifest:
        raise SnapshotError("plan_semantic_or_count_drift")
    for row in items:
        if (set(row) != set(ITEM_COLUMNS) or type(row["text_ready"]) is not bool or
            set(row["metadata_json"]) != {"raw", "effective", "qa", "review_status", "metadata_human_approved", "public_eligible"} or
            row["metadata_json"].get("metadata_human_approved") is not False or row["metadata_json"].get("public_eligible") is not False):
            raise SnapshotError("invalid_private_item_contract")
    if len({q["query_id"] for q in queries}) != len(queries) or any(set(q) != set(QUERY_COLUMNS) for q in queries):
        raise SnapshotError("invalid_query_contract")
    return {"manifest": manifest, "manifest_sha256": sha(raw), "items": items, "queries": queries,
            "text": texts, "image": images, "path": path}


def vectors_equal(actual, expected):
    actual, expected = vector(actual, len(expected)), vector(expected, len(expected))
    # Qdrant Cosine normalizes stored vectors. Compare against that documented
    # transform, not claim byte-for-byte provider/cache equality after storage.
    norm = math.sqrt(sum(v*v for v in expected))
    return all(abs(a-b/norm) <= 2e-6 for a, b in zip(actual, expected))


def verify_points(actual, expected):
    by_id = {p["id"]: p for p in actual}
    if len(by_id) != len(actual) or set(by_id) != {p["id"] for p in expected}:
        raise SnapshotError("qdrant_point_set_mismatch")
    for point in expected:
        got = by_id[point["id"]]
        if got.get("payload") != point["payload"] or set(got["payload"]) != PAYLOAD_KEYS or not vectors_equal(got.get("vector"), point["vector"]):
            raise SnapshotError("qdrant_point_readback_mismatch")


def sync_plan(path, root=ROOT, *, apply=False, execute=False, neon=None, qdrant=None):
    plan = read_plan(path, root)
    if not execute:
        return {"status": "dry_run", "snapshot_id": plan["manifest"]["snapshot_id"], "counts": plan["manifest"]["counts"], "network_calls": 0, "new_embedding_calls": 0}
    if not apply or neon is None or qdrant is None:
        raise SnapshotError("explicit_apply_execute_and_clients_required")
    lock = private_path(plan["path"] / ".sync.lock", root)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise SnapshotError("snapshot_sync_locked") from None
    attempt = private_path(plan["path"] / "sync-attempts" / uuid.uuid4().hex, root)
    try:
        os.close(fd)
        attempt.mkdir(parents=True)
        with (attempt / "reservation.json").open("xb") as handle:
            handle.write(encoded({"manifest_sha256": plan["manifest_sha256"], "new_embedding_calls": 0, "max_items": 379, "batch_limit": 50}))
            handle.flush()
            os.fsync(handle.fileno())
        neon.stage(plan)
        for lane in ("text", "image"):
            name = plan["manifest"]["qdrant_collections"][lane]
            qdrant.stage(name, 512 if lane == "text" else 1024, plan[lane])
            verify_points(qdrant.read_points(name, len(plan[lane])), plan[lane])
        neon.verify(plan)
        neon.ready(plan)
        receipt = {"status": "ready", "snapshot_id": plan["manifest"]["snapshot_id"], "manifest_sha256": plan["manifest_sha256"],
                   "counts": plan["manifest"]["counts"], "new_embedding_calls": 0, "readback_verified": True,
                   "qdrant_float_comparison": "cosine-normalized abs tolerance 2e-6", "public_release": False}
        with (attempt / "receipt.json").open("xb") as handle:
            handle.write(encoded(receipt))
        return receipt
    except Exception:
        if attempt.exists():
            with (attempt / "failed.json").open("xb") as handle:
                handle.write(encoded({"status": "failed_requires_explicit_rerun", "public_release": False}))
        raise
    finally:
        lock.unlink()


def neon_parameters(dsn):
    """Validate URI AND libpq interpretation, then connect with explicit fields."""
    import psycopg2
    try:
        parsed = urlsplit(dsn)
        host = parsed.hostname or ""
        if (parsed.scheme not in ("postgres", "postgresql") or parsed.fragment or
            not re.fullmatch(r"[a-z0-9.-]+\.neon\.tech", host) or ".." in host):
            raise SnapshotError("neon_endpoint_refused")
        options = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        if (len({k for k, _ in options}) != len(options) or
            any(k not in {"sslmode", "channel_binding"} for k, _ in options)):
            raise SnapshotError("neon_connection_override_refused")
        effective = psycopg2.extensions.parse_dsn(dsn)
        if (set(effective) - {"host", "port", "dbname", "user", "password", "sslmode", "channel_binding"} or
            effective.get("host", "").lower() != host or
            effective.get("port", "5432") != str(parsed.port or 5432) or
            not effective.get("user") or not re.fullmatch(r"[A-Za-z0-9_.-]+", effective.get("dbname", ""))):
            raise SnapshotError("neon_effective_connection_refused")
        # libpq service/hostaddr environment defaults can otherwise bypass even
        # an explicit validated hostname. Do not inspect or log their values.
        if any(os.environ.get(key) for key in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGSYSCONFDIR", "PGOPTIONS")):
            raise SnapshotError("neon_environment_override_refused")
        effective.setdefault("port", "5432")
        effective.setdefault("sslmode", "require")
        effective.setdefault("channel_binding", "prefer")
        if effective["sslmode"] not in ("require", "verify-ca", "verify-full") or effective["channel_binding"] not in ("prefer", "require"):
            raise SnapshotError("neon_tls_policy_refused")
        return effective
    except SnapshotError:
        raise
    except (ValueError, TypeError, psycopg2.Error):
        raise SnapshotError("neon_dsn_invalid") from None


class Neon:
    def __init__(self, dsn, migration):
        import psycopg2
        parameters = neon_parameters(dsn)
        self.connection = psycopg2.connect(**parameters, connect_timeout=10)
        self.migration = Path(migration).read_bytes()

    def preflight(self):
        with self.connection, self.connection.cursor() as cur:
            cur.execute("SELECT pg_database_size(current_database()),to_regnamespace('image_archive_v2') IS NOT NULL")
            size, exists = cur.fetchone()
        return {"database_bytes": size, "v2_schema_exists": exists}

    def stage(self, plan):
        from psycopg2.extras import Json
        m, sid = plan["manifest"], plan["manifest"]["snapshot_id"]
        if sha(self.migration) != m["identity"]["bindings"]["db/v2/0001_private_library.sql"]:
            raise SnapshotError("migration_file_drift")
        with self.connection, self.connection.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout='5s'")
            cur.execute("SET LOCAL statement_timeout='30s'")
            cur.execute("SELECT pg_advisory_xact_lock(68264392470331)")
            cur.execute("SELECT to_regclass('image_archive_v2.schema_migrations')")
            if cur.fetchone()[0] is None:
                cur.execute(self.migration.decode())
                cur.execute("INSERT INTO image_archive_v2.schema_migrations VALUES('0001',%s)", (sha(self.migration),))
            else:
                cur.execute("SELECT sha256 FROM image_archive_v2.schema_migrations WHERE version='0001'")
                if cur.fetchone() != (sha(self.migration),):
                    raise SnapshotError("remote_schema_version_mismatch")
            cur.execute("SELECT manifest_sha256,manifest_json,state FROM image_archive_v2.snapshots WHERE snapshot_id=%s FOR UPDATE", (sid,))
            found = cur.fetchone()
            if found is not None and found[:2] != (plan["manifest_sha256"], m):
                raise SnapshotError("remote_snapshot_conflict")
            if found is None:
                cur.execute("INSERT INTO image_archive_v2.snapshots(snapshot_id,manifest_sha256,manifest_json) VALUES(%s,%s,%s)", (sid, plan["manifest_sha256"], Json(m)))
            for table, columns, rows in (("items", ITEM_COLUMNS, plan["items"]), ("query_vectors", QUERY_COLUMNS, plan["queries"])):
                # BEFORE INSERT guards also run for ON CONFLICT DO NOTHING.
                # A completed immutable snapshot is verified, never reinserted.
                if found is not None and found[2] == "ready":
                    continue
                sql = "INSERT INTO image_archive_v2." + table + "(" + ",".join(columns) + ") VALUES(" + ",".join(["%s"] * len(columns)) + ") ON CONFLICT DO NOTHING"
                cur.executemany(sql, [tuple(Json(r[k]) if isinstance(r[k], (dict, list)) else r[k] for k in columns) for r in rows])
        self.verify(plan)

    def verify(self, plan):
        with self.connection, self.connection.cursor() as cur:
            self._verify_rows(cur, plan)

    def _verify_rows(self, cur, plan):
        sid = plan["manifest"]["snapshot_id"]
        for table, columns, expected, key in (("items", ITEM_COLUMNS, plan["items"], "item_id"), ("query_vectors", QUERY_COLUMNS, plan["queries"], "query_id")):
            cur.execute("SELECT " + ",".join(columns) + " FROM image_archive_v2." + table + " WHERE snapshot_id=%s ORDER BY " + key, (sid,))
            actual = [dict(zip(columns, r)) for r in cur.fetchall()]
            if actual != sorted(expected, key=lambda r: r[key]):
                raise SnapshotError("neon_row_readback_mismatch")

    def ready(self, plan):
        with self.connection, self.connection.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout='5s'")
            cur.execute("SET LOCAL statement_timeout='30s'")
            cur.execute("SELECT state FROM image_archive_v2.snapshots WHERE snapshot_id=%s AND manifest_sha256=%s FOR UPDATE", (plan["manifest"]["snapshot_id"], plan["manifest_sha256"]))
            if cur.fetchone() not in (("staged",), ("ready",)):
                raise SnapshotError("neon_snapshot_missing_before_ready")
            # Keep the parent lock through complete row verification and UPDATE;
            # child INSERTs cannot slip into the readback-to-ready interval.
            self._verify_rows(cur, plan)
            cur.execute("UPDATE image_archive_v2.snapshots SET state='ready' WHERE snapshot_id=%s AND manifest_sha256=%s AND state='staged'", (plan["manifest"]["snapshot_id"], plan["manifest_sha256"]))
            cur.execute("SELECT state FROM image_archive_v2.snapshots WHERE snapshot_id=%s AND manifest_sha256=%s", (plan["manifest"]["snapshot_id"], plan["manifest_sha256"]))
            if cur.fetchone() != ("ready",):
                raise SnapshotError("neon_ready_readback_failed")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise SnapshotError("redirect_refused")


class Qdrant:
    def __init__(self, endpoint, key):
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".cloud.qdrant.io") or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/") or parsed.port not in (None, 443, 6333):
            raise SnapshotError("qdrant_endpoint_refused")
        self.endpoint, self.key = endpoint.rstrip("/"), key

    def request(self, method, path, body=None, *, missing=False):
        request = Request(self.endpoint + path, data=encoded(body) if body is not None else None,
                          method=method, headers={"api-key": self.key, "Content-Type": "application/json"})
        try:
            with build_opener(NoRedirect()).open(request, timeout=30) as response:
                result = json.loads(response.read(20_000_001))
                if result.get("status") != "ok":
                    raise SnapshotError("qdrant_response_failed")
                return result["result"]
        except HTTPError as exc:
            if missing and exc.code == 404:
                return None
            raise SnapshotError("qdrant_http_" + str(exc.code)) from None
        except (URLError, TimeoutError, OSError, json.JSONDecodeError):
            raise SnapshotError("qdrant_transport_failed") from None

    def preflight(self):
        result = self.request("GET", "/collections")
        return {"collection_count": len(result["collections"]), "collection_names": [r["name"] for r in result["collections"]]}

    def stage(self, name, dimension, points):
        if not re.fullmatch(r"image_archive_v2_[a-f0-9]{64}_(text512|image1024)", name) or dimension not in (512, 1024) or len(points) > 379:
            raise SnapshotError("qdrant_namespace_or_bound_refused")
        base = "/collections/" + name
        info = self.request("GET", base, missing=True)
        if info is None:
            self.request("PUT", base, {"vectors": {"size": dimension, "distance": "Cosine"}})
            info = {"payload_schema": {}}
        else:
            config = info["config"]["params"]["vectors"]
            if config.get("size") != dimension or config.get("distance") != "Cosine":
                raise SnapshotError("qdrant_collection_model_shape_conflict")
        existing = self.read_points(name, len(points))
        expected_ids = {p["id"]: p for p in points}
        if any(p["id"] not in expected_ids for p in existing):
            raise SnapshotError("qdrant_existing_point_conflict")
        verify_points(existing, [expected_ids[p["id"]] for p in existing])
        missing_indexes = []
        for field, kind in (("group_id", "keyword"), ("snapshot_id", "keyword"), ("image_approved", "bool")):
            existing_type = info.get("payload_schema", {}).get(field, {}).get("data_type")
            if existing_type is None:
                missing_indexes.append((field, kind))
            elif existing_type != kind:
                raise SnapshotError("qdrant_payload_index_conflict")
        for field, kind in missing_indexes:
            self.request("PUT", base + "/index?wait=true", {"field_name": field, "field_schema": kind})
        known = {p["id"] for p in existing}
        new = [p for p in points if p["id"] not in known]
        for offset in range(0, len(new), 50):
            self.request("PUT", base + "/points?wait=true", {"points": new[offset:offset+50]})

    def read_points(self, name, limit):
        if type(limit) is not int or not 0 <= limit <= 379:
            raise SnapshotError("qdrant_readback_limit_invalid")
        rows, offset = [], None
        seen_offsets = set()
        for _ in range(math.ceil(limit / 50) + 1):
            body = {"limit": 50, "with_payload": True, "with_vector": True}
            if offset is not None:
                body["offset"] = offset
            result = self.request("POST", "/collections/" + name + "/points/scroll", body)
            page = result.get("points") if isinstance(result, dict) else None
            if not isinstance(page, list) or len(page) > 50:
                raise SnapshotError("qdrant_invalid_scroll_page")
            rows.extend(page)
            next_offset = result.get("next_page_offset")
            if len(rows) > limit:
                raise SnapshotError("qdrant_readback_bound_exceeded")
            if next_offset is None:
                return rows
            if (not page or type(next_offset) not in (str, int) or
                (isinstance(next_offset, str) and not 0 < len(next_offset) <= 64) or
                (isinstance(next_offset, int) and next_offset < 0) or next_offset in seen_offsets):
                raise SnapshotError("qdrant_invalid_scroll_progress")
            seen_offsets.add(next_offset)
            offset = next_offset
        raise SnapshotError("qdrant_scroll_page_limit_exceeded")


def credentials(path):
    allowed = {"DATABASE_URL", "NEON_DATABASE_KEY", "QDRANT_ENDPOINT", "QDRANT_API_KEY"}
    values = {k: os.environ[k] for k in allowed if os.environ.get(k)}
    if Path(path).is_file():
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() in allowed and key.strip() not in values:
                values[key.strip()] = value.strip().strip("\"'")
    if not (values.get("DATABASE_URL") or values.get("NEON_DATABASE_KEY")) or not all(values.get(k) for k in ("QDRANT_ENDPOINT", "QDRANT_API_KEY")):
        raise SnapshotError("required_cloud_credentials_missing")
    return values


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", type=Path, default=ROOT)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--env-path", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.preflight or args.execute:
            if not args.plan or (args.execute and not args.apply) or (args.preflight and (args.apply or args.execute)):
                raise SnapshotError("invalid_live_mode_flags")
            plan = read_plan(args.plan, args.archive_root)
            values = credentials(args.env_path or args.archive_root / ".env")
            neon = Neon(values.get("DATABASE_URL") or values["NEON_DATABASE_KEY"], args.archive_root / "db/v2/0001_private_library.sql")
            try:
                qdrant = Qdrant(values["QDRANT_ENDPOINT"], values["QDRANT_API_KEY"])
                result = {"status": "read_only_preflight", "neon": neon.preflight(), "qdrant": qdrant.preflight(), "new_embedding_calls": 0} if args.preflight else sync_plan(args.plan, args.archive_root, apply=True, execute=True, neon=neon, qdrant=qdrant)
            finally:
                neon.connection.close()
        elif args.plan:
            result = sync_plan(args.plan, args.archive_root)
        else:
            result = build_snapshot_plan(args.archive_root, output_dir=args.output_dir, apply=args.apply)
        print(encoded(result).decode(), end="")
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, SnapshotError) else "snapshot_operation_failed"
        print(json.dumps({"status": "failed", "error_code": code, "new_embedding_calls": 0}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
