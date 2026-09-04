"""Offline, immutable SQLite projection of the two frozen Luna canaries.

The live approval DB is read-only. This candidate store never authorizes search,
embedding, publication, rights, deletion or human decisions.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import unicodedata
from pathlib import Path

from .approval_handoff import _committed, _require_latest, _validate_commit
from .incremental_workflow import load_frozen_workflow
from .luna_analysis_import import _check_evidence, _json, digest, encode
from .luna_analysis_view import _load_review
from .luna_reuse_analysis_import import RELATIVE_ROOT, _group_map
from .luna_reuse_analysis_view import _load as _load_reuse

SCHEMA = "luna-candidate-store-1"
MIGRATION = "db/metadata/0001_luna_candidates.sqlite.sql"
OUTPUT = "data/private-research/image-rag-admin/metadata-candidates/v1/candidates.sqlite3"
V1 = "2026-09-03-luna-analysis-10-v1"
V2 = "2026-09-04-luna-reuse-analysis-10-v2"


class CandidateStoreError(ValueError):
    pass


def _text(value) -> str:
    return encode(value).decode("utf-8")


def normalize_term(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _usage_valid(receipt: dict, run_id: str, manifest_sha: str) -> None:
    if (receipt.get("analysis_run_id") != run_id or receipt.get("task_manifest_sha256") != manifest_sha
            or receipt.get("actual_billed_tokens") is not None or receipt.get("actual_billed_cost") is not None
            or receipt.get("completed_image_count") != 10
            or receipt.get("evidence_status") not in {"observed_local_codex_log", "observed_isolated_local_codex_logs"}):
        raise CandidateStoreError("Unbound or unsupported actual-token receipt")
    row = receipt.get("usage", {})
    names = ("input_tokens_including_cached", "cached_input_tokens", "uncached_input_tokens_calculated",
             "output_tokens_including_reasoning", "reasoning_output_tokens", "total_tokens")
    if any(type(row.get(key)) is not int or row[key] < 0 for key in names):
        raise CandidateStoreError("Incomplete token usage")
    if (row[names[2]] != row[names[0]] - row[names[1]] or row[names[5]] != row[names[0]] + row[names[3]]
            or row[names[4]] > row[names[3]]):
        raise CandidateStoreError("Token subsets or totals are inconsistent")


def load_validated_sources(root: Path, approval_db: Path) -> dict:
    """Use frozen read-only validation, including latest human-approval bindings."""
    root = root.resolve()
    views = [_load_review(root, approval_db, V1), _load_reuse(root, approval_db, V2)]
    runs, evidence, seen = [], [], set()
    taxonomy_relative = views[1]["manifest"]["taxonomy_path"].removeprefix("../")
    taxonomy, taxonomy_raw = _json(root.parent / taxonomy_relative)
    taxonomy_sha = digest(taxonomy_raw)
    if taxonomy_sha != views[1]["manifest"]["taxonomy_sha256"]:
        raise CandidateStoreError("Pinned taxonomy changed")
    evidence.append({"scope": "workspace", "path": taxonomy_relative, "sha256": taxonomy_sha})
    for view in views:
        manifest = view["manifest"]
        run_id = manifest["analysis_run_id"]
        directory = root / RELATIVE_ROOT / run_id
        imported = directory / "imports" / view["task_manifest_sha256"]
        import_receipt, _ = _json(imported / "receipt.json")
        evidence.extend(import_receipt["source_files"])
        for path, sha in view["evidence"].items():
            evidence.append({"scope": "archive", "path": Path(path).relative_to(root).as_posix(), "sha256": sha})
        token, token_raw = _json(directory / "token-usage-receipt.json")
        _usage_valid(token, run_id, view["task_manifest_sha256"])
        execution, execution_raw = _json(directory / "execution-receipt.json")
        for name, raw in (("token-usage-receipt.json", token_raw), ("execution-receipt.json", execution_raw)):
            evidence.append({"scope": "archive", "path": f"{RELATIVE_ROOT}/{run_id}/{name}", "sha256": digest(raw)})
        if view.get("qa_findings_sha256"):
            evidence.append({"scope": "archive", "path": f"{RELATIVE_ROOT}/{run_id}/qa-findings.json", "sha256": view["qa_findings_sha256"]})
        committed = _committed(approval_db, manifest["source_run_id"])
        groups = _group_map(_validate_commit(load_frozen_workflow(root, manifest["source_run_id"]), committed))
        cards = []
        for card in view["cards"]:
            task = card["task"]
            if task["item_id"] in seen:
                raise CandidateStoreError("Two frozen batches overlap")
            seen.add(task["item_id"])
            raw_result = (root / task["raw_result_path"]).read_bytes()
            if json.loads(raw_result) != card["result"]:
                raise CandidateStoreError("Raw result changed after validation")
            group = groups.get(task["item_id"], {"group_id": None, "representative_id": None,
                                                "member_count": 1, "selected_is_representative": True})
            cards.append({**card, "raw_result_json": raw_result.decode("utf-8"), "group": group})
        runs.append({"manifest": manifest, "cards": cards, "task_manifest_sha256": view["task_manifest_sha256"],
                     "validated_results_sha256": view["validated_results_sha256"],
                     "import_receipt_sha256": view["import_receipt_sha256"],
                     "token": token, "token_raw_json": token_raw.decode("utf-8"),
                     "execution": execution, "execution_raw_json": execution_raw.decode("utf-8")})
    if len(seen) != 20:
        raise CandidateStoreError("Frozen v1 projection requires exactly twenty images")
    # Drop renderer-only Path objects/relative UI links; preserve source data exactly.
    for run in runs:
        for card in run["cards"]:
            card.pop("image_relative", None)
    _check_evidence(root, evidence)
    return {"runs": runs, "taxonomy": taxonomy, "taxonomy_raw_json": taxonomy_raw.decode("utf-8"),
            "taxonomy_relative_path": taxonomy_relative, "taxonomy_sha256": taxonomy_sha, "evidence": evidence}


def _insert(connection: sqlite3.Connection, table: str, values: dict) -> None:
    # Table and column names originate solely in this module, never user input.
    columns = tuple(values)
    placeholders = ",".join("?" for _ in columns)
    existing = connection.execute(f'SELECT 1 FROM "{table}" WHERE ' + " AND ".join(f'"{key}" IS ?' for key in columns),
                                  tuple(values.values())).fetchone()
    if not existing:
        connection.execute(f'INSERT INTO "{table}" ({",".join(columns)}) VALUES ({placeholders})', tuple(values.values()))


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for row in value:
            yield from _strings(row)
    elif isinstance(value, dict):
        for key in sorted(value):
            yield from _strings(value[key])


def _lexical(card: dict, aliases: dict) -> tuple[str, list[str]]:
    result = card["result"]
    excluded = sorted({row["field"].split(".")[0].split("[")[0] for row in card["qa_findings"]})
    selected = {key: result[key] for key in ("visual", "search_hints", "prompt_intent", "prompt_analysis", "reuse_ideas", "usage_selection")
                if key in result and key not in excluded}
    terms = [card["task"]["style_id"], card["full_prompt"], *_strings(selected)]
    if "usage_selection" not in excluded:
        for row in _assignments(result):
            terms.extend(aliases.get(row["use_case_id"], []))
    unique = {}
    for term in terms:
        if term.strip():
            unique.setdefault(normalize_term(term), term.strip())
    return "\n".join(unique.values()), excluded


def _assignments(result: dict) -> list[dict]:
    selection = result.get("usage_selection")
    if selection is None:
        return []  # v1 freeform use_case is never coerced to a normalized ID.
    return [selection["primary"], *selection["secondary"]] if selection["primary"] else []


def _populate(connection: sqlite3.Connection, bundle: dict, migration_sha: str) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        _insert(connection, "snapshot", {"id": 1, "schema_version": SCHEMA, "source_sha256": digest(encode(bundle)),
                "migration_sha256": migration_sha, "status": "needs_review", "release_eligible": 0, "public_search_eligible": 0})
        taxonomy, taxonomy_sha = bundle["taxonomy"], bundle["taxonomy_sha256"]
        if digest(bundle["taxonomy_raw_json"].encode("utf-8")) != taxonomy_sha or json.loads(bundle["taxonomy_raw_json"]) != taxonomy:
            raise CandidateStoreError("Taxonomy raw JSON mismatch")
        _insert(connection, "taxonomy_versions", {"sha256": taxonomy_sha, "schema_version": taxonomy["schema_version"],
                "source_relative_path": bundle["taxonomy_relative_path"], "raw_json": bundle["taxonomy_raw_json"], "status": "proposal_needs_review"})
        terms = [("use_case", row) for family in taxonomy["families"] for row in family["use_cases"]]
        terms.extend(("asset_format", row) for row in taxonomy.get("asset_formats", []))
        labels = {}
        for facet, row in terms:
            label = row["label_ko"]
            _insert(connection, "taxonomy_terms", {"taxonomy_sha256": taxonomy_sha, "facet": facet, "term_id": row["id"],
                    "label_ko": label, "definition_json": _text(row)})
            labels[(facet, row["id"])] = label
        aliases = {}
        alias_rows = [(facet, ident, label, "pinned_label") for (facet, ident), label in labels.items()]
        alias_rows += [(facet, ident, ident, "pinned_id") for facet, ident in labels]
        alias_rows += [(row["facet"], row["target_id"], row["term"], "pinned_alias") for row in taxonomy.get("aliases", [])]
        inserted_aliases = set()
        for facet, ident, term, origin in alias_rows:
            key = facet, ident, normalize_term(term)
            if (facet, ident) not in labels:
                raise CandidateStoreError("Alias references unknown pinned taxonomy term")
            if key in inserted_aliases:
                continue
            inserted_aliases.add(key)
            _insert(connection, "taxonomy_aliases", {"taxonomy_sha256": taxonomy_sha, "facet": facet, "term_id": ident,
                    "term": term, "normalized_term": key[2], "origin": origin})
            if facet == "use_case":
                aliases.setdefault(ident, []).append(term)
        for run in bundle["runs"]:
            manifest = run["manifest"]
            run_id, commit = manifest["analysis_run_id"], manifest["source_commit"]["id"]
            token = run["token"]
            _usage_valid(token, run_id, run["task_manifest_sha256"])
            if json.loads(run["token_raw_json"]) != token:
                raise CandidateStoreError("Token JSON mismatch")
            versions = {card["result"]["schema_version"] for card in run["cards"]}
            if len(versions) != 1 or len(run["cards"]) != token["completed_image_count"]:
                raise CandidateStoreError("Run result versions or completed count mismatch")
            _insert(connection, "analysis_runs", {"run_id": run_id, "result_schema_version": next(iter(versions)),
                    "task_manifest_sha256": run["task_manifest_sha256"], "validated_results_sha256": run["validated_results_sha256"],
                    "import_receipt_sha256": run["import_receipt_sha256"], "model": manifest["model_family"],
                    "source_run_id": manifest["source_run_id"], "source_commit_id": commit,
                    "taxonomy_sha256": taxonomy_sha if "taxonomy_sha256" in manifest else None,
                    "manifest_json": _text(manifest), "execution_json": run["execution_raw_json"]})
            usage = token["usage"]
            _insert(connection, "run_usage", {"run_id": run_id, "receipt_sha256": digest(run["token_raw_json"].encode()),
                    "scope": token.get("scope", "isolated_sessions_including_recorded_retries"), "evidence_status": token["evidence_status"],
                    "input_including_cached": usage["input_tokens_including_cached"], "cached_input": usage["cached_input_tokens"],
                    "uncached_input": usage["uncached_input_tokens_calculated"], "output_including_reasoning": usage["output_tokens_including_reasoning"],
                    "reasoning_output": usage["reasoning_output_tokens"], "total_tokens": usage["total_tokens"],
                    "actual_billed_tokens": None, "actual_billed_cost": None, "raw_json": run["token_raw_json"]})
            for card in run["cards"]:
                task, result, group = card["task"], card["result"], card["group"]
                if (result.get("review_status") != "needs_review" or result.get("metadata_human_approved") is not False
                        or result.get("release_eligible") is not False or card["rights"].get("release_eligible") is not False
                        or json.loads(card["raw_result_json"]) != result):
                    raise CandidateStoreError("Unsafe candidate visibility or changed raw JSON")
                if digest(card["full_prompt"].encode("utf-8")) != task["prompt_sha256"]:
                    raise CandidateStoreError("Original prompt SHA mismatch")
                for name in ("source_image_sha256", "prepared_image_sha256"):
                    if not re.fullmatch(r"[a-f0-9]{64}", task[name]):
                        raise CandidateStoreError("Invalid image SHA")
                    _insert(connection, "assets", {"sha256": task[name]})
                _insert(connection, "asset_locations", {"sha256": task["prepared_image_sha256"],
                        "relative_path": task["prepared_image_path"], "role": "prepared_image"})
                _insert(connection, "prompts", {"sha256": task["prompt_sha256"], "original_text": card["full_prompt"]})
                if group["group_id"]:
                    _insert(connection, "approval_groups", {"source_run_id": manifest["source_run_id"], "source_commit_id": commit,
                            "group_id": group["group_id"], "representative_item_id": group["representative_id"], "member_count": group["member_count"]})
                _insert(connection, "items", {"item_id": task["item_id"], "style_id": task["style_id"],
                        "source_image_sha256": task["source_image_sha256"], "prepared_image_sha256": task["prepared_image_sha256"],
                        "prompt_sha256": task["prompt_sha256"], "source_run_id": manifest["source_run_id"], "source_commit_id": commit,
                        "group_id": group["group_id"], "is_group_representative": int(group["selected_is_representative"]),
                        "rights_json": _text(card["rights"])})
                raw_sha = digest(card["raw_result_json"].encode("utf-8"))
                candidate_id = digest(encode([run_id, task["task_id"], result["schema_version"], raw_sha]))
                _insert(connection, "candidates", {"candidate_id": candidate_id, "run_id": run_id, "task_id": task["task_id"],
                        "item_id": task["item_id"], "input_fingerprint": task["input_fingerprint"], "result_version": result["schema_version"],
                        "raw_result_sha256": raw_sha, "raw_json": card["raw_result_json"], "visual_json": _text(result["visual"]),
                        "prompt_analysis_json": _text(result.get("prompt_analysis", result.get("prompt_intent"))),
                        "freeform_usage_json": _text(result["reuse_ideas"]) if "reuse_ideas" in result else None,
                        "review_status": "needs_review", "metadata_human_approved": 0, "release_eligible": 0, "public_search_eligible": 0})
                for ordinal, assignment in enumerate(_assignments(result)):
                    _insert(connection, "usage_assignments", {"candidate_id": candidate_id, "ordinal": ordinal, "taxonomy_sha256": taxonomy_sha,
                            "facet": "use_case", "use_case_id": assignment["use_case_id"], "fit": assignment["fit"],
                            "reuse_mode": assignment["reuse_mode"], "evidence_basis": assignment["evidence_basis"], "detail_json": _text(assignment)})
                for ordinal, finding in enumerate(card["qa_findings"]):
                    _insert(connection, "candidate_qa", {"candidate_id": candidate_id, "ordinal": ordinal, "field_path": finding["field"],
                            "status": finding["status"], "detail_json": _text(finding)})
                matches = [row for row in token.get("per_image", []) if row["style_id"] == task["style_id"]]
                if matches:
                    if len(matches) != 1:
                        raise CandidateStoreError("Ambiguous per-image usage")
                    _insert(connection, "candidate_usage", {"candidate_id": candidate_id, "evidence_status": "observed_isolated_local_codex_logs",
                            "total_tokens": matches[0]["total_tokens"], "raw_json": _text(matches[0])})
                text, excluded = _lexical(card, aliases)
                _insert(connection, "lexical_documents", {"candidate_id": candidate_id, "text": text,
                        "purpose": "private_diagnostic_only", "excluded_qa_roots_json": _text(excluded), "public_search_eligible": 0})
                connection.execute("INSERT INTO lexical_fts(candidate_id,text) VALUES (?,?)", (candidate_id, text))
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise CandidateStoreError("Foreign key validation failed")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _logical_dump(connection: sqlite3.Connection) -> str:
    schema = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    tables = [row[1] for row in schema if row[0] == "table"]
    rows = {name: [[{"blob_hex": cell.hex()} if isinstance(cell, bytes) else cell for cell in row]
                   for row in sorted(connection.execute(f'SELECT * FROM "{name}"').fetchall(), key=repr)] for name in tables}
    return _text({"schema": schema, "rows": rows})


def project_snapshot(bundle: dict, output: Path, migration: Path, *, apply: bool = False, recheck=None) -> dict:
    """Build transactionally in memory. Existing snapshots are compared, never overwritten."""
    raw_migration = migration.read_bytes()
    output = Path(output)
    if output.is_symlink() or any(parent.is_symlink() for parent in output.parents):
        raise CandidateStoreError("Output path must not contain symlinks")
    expected = sqlite3.connect(":memory:")
    try:
        expected.executescript(raw_migration.decode("utf-8"))
        _populate(expected, bundle, digest(raw_migration))
        summary = {"schema_version": SCHEMA, "status": "dry_run", "output_path": str(output),
                   "source_sha256": digest(encode(bundle)), "migration_sha256": digest(raw_migration),
                   "metadata_human_approved": False, "release_eligible": False, "public_search_eligible": False,
                   "provider_calls": 0, "embedding_calls": 0, "human_approved_candidate_count": 0,
                   "projected_database_bytes": len(expected.serialize()),
                   "table_names": [row[0] for row in expected.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")],
                   "counts": {table: expected.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                              for table in ("assets", "prompts", "items", "approval_groups", "analysis_runs", "candidates",
                                            "taxonomy_terms", "taxonomy_aliases", "usage_assignments", "candidate_qa", "candidate_usage", "lexical_documents")}}
        if output.exists():
            existing = sqlite3.connect(output.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                if existing.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or _logical_dump(existing) != _logical_dump(expected):
                    raise CandidateStoreError("Existing snapshot differs; use an explicitly versioned new migration, never overwrite")
            finally:
                existing.close()
            if recheck:
                recheck()
            return {**summary, "status": "unchanged", "database_sha256": digest(output.read_bytes())}
        if recheck:
            recheck()
        if not apply:
            return summary
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".candidate-store-", suffix=".sqlite3", dir=output.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            destination = sqlite3.connect(temporary)
            try:
                expected.backup(destination)
            finally:
                destination.close()
            if recheck:
                recheck()
            os.link(temporary, output)  # Atomic create-only: fails if another writer wins.
        finally:
            temporary.unlink(missing_ok=True)
        return {**summary, "status": "prepared", "database_sha256": digest(output.read_bytes())}
    finally:
        expected.close()


def diagnostic_search(db: sqlite3.Connection, query: str, limit: int = 5) -> list[dict]:
    """Plain text only; quoted FTS plus escaped substring fallback, never public search."""
    if not isinstance(query, str) or not 0 < len(query.strip()) <= 200 or type(limit) is not int or not 1 <= limit <= 50:
        raise CandidateStoreError("Bounded nonempty diagnostic query required")
    term = normalize_term(query)
    quoted = '"' + term.replace('"', '""') + '"'
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = db.execute("""SELECT c.candidate_id,i.style_id,c.review_status FROM candidates c
        JOIN items i USING(item_id) JOIN lexical_documents l USING(candidate_id)
        WHERE c.candidate_id IN (SELECT candidate_id FROM lexical_fts WHERE lexical_fts MATCH ?)
           OR lower(l.text) LIKE ? ESCAPE '\\'
           OR c.candidate_id IN (SELECT a.candidate_id FROM usage_assignments a JOIN taxonomy_aliases t
              ON a.taxonomy_sha256=t.taxonomy_sha256 AND t.facet='use_case' AND a.use_case_id=t.term_id
              WHERE t.normalized_term=? AND NOT EXISTS (SELECT 1 FROM candidate_qa q
                  WHERE q.candidate_id=a.candidate_id AND (q.field_path='usage_selection' OR q.field_path LIKE 'usage_selection.%')))
        ORDER BY i.style_id LIMIT ?""", (quoted, "%" + escaped + "%", term, limit)).fetchall()
    return [{"candidate_id": row[0], "style_id": row[1], "review_status": row[2],
             "purpose": "private_diagnostic_only", "public_search_eligible": False} for row in rows]


def build_metadata_store(root: Path, *, approval_db: Path | None = None, apply: bool = False) -> dict:
    root = Path(root).resolve()
    approval_db = (approval_db or root / "data/private-research/image-rag-admin/state.sqlite3").resolve()
    bundle = load_validated_sources(root, approval_db)
    def recheck():
        _check_evidence(root, bundle["evidence"])
        for run in bundle["runs"]:
            manifest = run["manifest"]
            _require_latest(approval_db, manifest["source_run_id"], manifest["source_commit"]["id"])
    return project_snapshot(bundle, root / OUTPUT, root / MIGRATION, apply=apply, recheck=recheck)
