"""Build a private read-only search snapshot from verified Voyage text caches."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
import tempfile
from pathlib import Path

from .embedding_budget import encoded, file_sha256
from .text_embedding_run import DIMENSION, MODEL, _vector, cache_key, load_manifest_vectors


def populate(connection, documents, vectors, queries, meta):
    connection.executescript("""
      PRAGMA foreign_keys=ON;
      CREATE TABLE snapshot(key TEXT PRIMARY KEY,value_json TEXT NOT NULL);
      CREATE TABLE documents(item_id TEXT PRIMARY KEY,style_id TEXT NOT NULL,group_key TEXT NOT NULL,
        representative_item_id TEXT NOT NULL REFERENCES documents(item_id) DEFERRABLE INITIALLY DEFERRED,
        compact_text TEXT NOT NULL,prompt_text TEXT NOT NULL,rights_json TEXT NOT NULL,
        image_path TEXT NOT NULL,prepared_sha256 TEXT NOT NULL,metadata_status TEXT NOT NULL,
        public_eligible INTEGER NOT NULL CHECK(public_eligible=0));
      CREATE TABLE text_vectors(cache_key TEXT PRIMARY KEY,model TEXT NOT NULL,dimension INTEGER NOT NULL CHECK(dimension=512),
        input_type TEXT NOT NULL CHECK(input_type IN ('document','query')),vector_f32le BLOB NOT NULL CHECK(length(vector_f32le)=2048),
        vector_sha256 TEXT NOT NULL);
      CREATE TABLE document_vectors(item_id TEXT PRIMARY KEY REFERENCES documents(item_id),cache_key TEXT NOT NULL REFERENCES text_vectors(cache_key));
      CREATE TABLE query_vectors(query_id TEXT PRIMARY KEY,query_text TEXT NOT NULL,cache_key TEXT NOT NULL REFERENCES text_vectors(cache_key));
      CREATE INDEX document_groups ON documents(group_key);
    """)
    connection.execute("BEGIN")
    for key, value in meta.items():
        connection.execute("INSERT INTO snapshot VALUES(?,?)", (key, encoded(value).decode().strip()))
    for doc in documents:
        connection.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,0)", (
            doc["item_id"], doc["style_id"], doc["group_id"] or doc["item_id"], doc["representative_item_id"],
            doc["compact_text"], doc["prompt_text"], doc["rights_json"], doc["image_path"], doc["prepared_sha256"],
            "text_deferred" if doc["budget_blocked"] else "candidate_needs_review"))
    inserted = set()

    def add(entry, vector):
        key = cache_key(entry)
        blob = struct.pack("<512f", *_vector(vector))
        _vector(list(struct.unpack("<512f", blob)))
        if key not in inserted:
            connection.execute("INSERT INTO text_vectors VALUES(?,?,?,?,?,?)", (
                key, MODEL, DIMENSION, entry["input_type"], blob, hashlib.sha256(blob).hexdigest()))
            inserted.add(key)
        return key

    for doc in documents:
        ident = "compact:" + doc["item_id"]
        if doc["budget_blocked"]:
            if ident in vectors:
                raise ValueError("Blocked text unexpectedly has an embedding")
            continue
        key = add({"text": doc["compact_text"], "input_type": "document"}, vectors[ident])
        connection.execute("INSERT INTO document_vectors VALUES(?,?)", (doc["item_id"], key))
    for query in queries:
        key = add(query, query["vector"])
        connection.execute("INSERT INTO query_vectors VALUES(?,?,?)", (query["input_id"], query["text"], key))
    connection.commit()
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or connection.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("Search snapshot integrity check failed")


def build_store(root: Path, source_database: Path, plan_dir: Path, run_dir: Path, full_manifest_sha: str,
                query_manifest_sha: str, output_dir: Path, *, apply=False):
    root = root.resolve()
    output_dir = output_dir.resolve()
    if not output_dir.is_relative_to(root / "data/private-research"):
        raise ValueError("Search snapshot must remain private")
    source_database, plan_dir = source_database.resolve(strict=True), plan_dir.resolve(strict=True)
    summary_raw = (plan_dir / "summary.json").read_bytes()
    summary = json.loads(summary_raw)
    body = (plan_dir / "documents.jsonl").read_bytes()
    if (hashlib.sha256(summary_raw).hexdigest() != plan_dir.name or
        hashlib.sha256(body).hexdigest() != summary["documents_sha256"] or
        file_sha256(source_database) != summary["database_sha256"]):
        raise ValueError("Frozen source/plan drift")
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(source_database) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError("Checkpointed source database required")
    full = load_manifest_vectors(run_dir, full_manifest_sha, archive_root=root)
    query_bundle = load_manifest_vectors(run_dir, query_manifest_sha, archive_root=root)
    if full["receipt"]["pending_or_uncertain_requests"]:
        raise ValueError("Uncertain provider calls prevent finalization")
    documents = [json.loads(line) for line in body.splitlines()]
    expected = {"compact:" + d["item_id"]: d["compact_text"] for d in documents if not d["budget_blocked"]}
    if expected != {d["input_id"]: d["text"] for d in full["manifest"]["documents"] if d["input_type"] == "document"} or set(full["vectors"]) != set(expected):
        raise ValueError("Full text vectors do not match ready approved documents")
    connection = sqlite3.connect(source_database.as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = {r["item_id"]: dict(r) for r in connection.execute("""SELECT i.item_id,i.style_id,i.prepared_sha256,i.rights_json,
          p.original_text,(SELECT min(a.path) FROM asset_locations a WHERE a.sha256=i.prepared_sha256 AND a.role='prepared') image_path
          FROM source_items i JOIN prompts p ON p.sha256=i.prompt_sha256 WHERE i.approval_state='image_approved'""")}
        if set(rows) != {d["item_id"] for d in documents}:
            raise ValueError("Approved item set drift")
        for doc in documents:
            row = rows[doc["item_id"]]
            if row["style_id"] != doc["style_id"] or not row["image_path"]:
                raise ValueError("Style or image mapping unavailable")
            image_path = root / row["image_path"]
            if not image_path.resolve().is_relative_to(root) or file_sha256(image_path) != row["prepared_sha256"]:
                raise ValueError("Prepared image mapping changed")
            doc.update(prompt_text=row["original_text"], rights_json=row["rights_json"],
                       image_path=row["image_path"], prepared_sha256=row["prepared_sha256"])
    finally:
        connection.close()
    queries = [{**q, "vector": query_bundle["vectors"][q["input_id"]]} for q in query_bundle["manifest"]["documents"] if q["input_type"] == "query"]
    meta = {"schema_version": "private-text-vector-store-1", "source_database_sha256": summary["database_sha256"],
            "source_database_path": source_database.relative_to(root).as_posix(), "plan_sha256": plan_dir.name,
            "provider_run_path": run_dir.resolve().relative_to(root).as_posix(), "full_manifest_sha256": full_manifest_sha,
            "query_manifest_sha256": query_manifest_sha, "model": MODEL, "dimension": DIMENSION,
            "approved_documents": len(documents), "embedded_documents": len(expected), "query_count": len(queries),
            "group_count_including_singletons": len({d["group_id"] or d["item_id"] for d in documents}),
            "usage": full["receipt"], "metadata_human_approved": False, "release_eligible": False,
            "image_reembedding_calls": 0, "image_vectors_remain_in_existing_shared_cache": True}
    key = hashlib.sha256(encoded(meta)).hexdigest()
    target = output_dir / key
    if target.is_symlink() or target.is_junction():
        raise ValueError("Unsafe output target")
    if apply:
        output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".build-", dir=output_dir) as temporary:
            temp = Path(temporary)
            db = sqlite3.connect(temp / "search.sqlite3")
            try:
                populate(db, documents, full["vectors"], queries, meta)
                db.execute("VACUUM")
            finally:
                db.close()
            if file_sha256(source_database) != summary["database_sha256"]:
                raise ValueError("Source changed during search snapshot creation")
            receipt = {**meta, "snapshot_key": key, "database_sha256": file_sha256(temp / "search.sqlite3"),
                       "database_bytes": (temp / "search.sqlite3").stat().st_size}
            raw = encoded(receipt)
            if target.exists():
                if (target / "summary.json").is_symlink() or (target / "search.sqlite3").is_symlink() or (target / "summary.json").read_bytes() != raw or file_sha256(target / "search.sqlite3") != receipt["database_sha256"]:
                    raise ValueError("Immutable search snapshot differs")
            else:
                with (temp / "summary.json").open("xb") as handle:
                    handle.write(raw)
                temp.rename(target)
        return {"status": "prepared", "path": target.relative_to(root).as_posix(), **receipt}
    return {"status": "dry_run", "path": target.relative_to(root).as_posix(), **meta}
