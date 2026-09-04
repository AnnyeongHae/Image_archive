"""Transactional, local-only review drafts and last-confirmed private gallery.

This dedicated SQLite database never edits source runs or invokes providers.
Drafts deliberately retain unchecked/unfinished selections; only ``advance``
at stage four replaces the confirmed gallery, in the same transaction as its
decision snapshot, revision, immutable event and idempotent response.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .group_workflow import (
    DEFAULT_IMAGE_APPROVAL_POLICY, GROUP_WORKFLOW_DECISIONS_V3_SCHEMA_VERSION,
    blank_group_workflow_decisions, validate_group_workflow_decisions,
)


class AdminStoreError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 422, details: Any = None):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise AdminStoreError("invalid_json", "Only finite JSON values are allowed") from exc


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _text(value: Any, field: str, *, max_bytes: int = 8000) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > max_bytes:
        raise AdminStoreError("invalid_decisions", f"{field} must be text of at most {max_bytes} UTF-8 bytes")
    return value


WIRE_KEYS = {"schema_version", "run_id", "spec_sha256", "approval_policy", "reviewer", "reviewed_at",
             "duplicate_reviews", "similarity_reviews", "image_approvals", "metadata_optional", "notes"}
EDIT_FIELDS = {1: set(), 2: {"duplicate_reviews"}, 3: {"similarity_reviews"}, 4: {"image_approvals"}}
TABLES = {"image_admin_runs", "image_admin_commits", "image_admin_events"}


class AdminStore:
    def __init__(self, db_path: Path, spec: dict, seed_decisions: dict | None = None,
                 validate_source: Callable[[], Any] | None = None):
        self.db_path = Path(db_path).resolve()
        self.spec = copy.deepcopy(spec)
        self.run_id = spec["run_id"]
        self.spec_sha256 = spec["spec_sha256"]
        self.spec_content_sha256 = _hash(spec)
        self.validate_source = validate_source
        if spec.get("approval_policy") != DEFAULT_IMAGE_APPROVAL_POLICY:
            raise AdminStoreError("invalid_spec", "The administrator requires an opted-in v3 source spec")
        self.candidates = {field: {row["id"]: row for row in spec[field]}
                           for field in ("duplicate_candidates", "similarity_candidates")}
        self.active_source_ids = set(spec["stage1"]["active_ids"])
        self.baseline_choices = {row["id"]: {"id": row["id"], "approved": row["approved"], "memo_text": row.get("memo_text", "")}
                                 for row in spec.get("baseline", {}).get("image_approvals", [])}
        self.readonly_ids = set(spec.get("baseline", {}).get("read_only_ids", []))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection(create=True) as db:
            existing_tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'") if not row[0].startswith("sqlite_")}
            if existing_tables and existing_tables != TABLES:
                raise AdminStoreError("wrong_database", "Use a dedicated image-admin database; existing tables are not ours")
            if db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                raise AdminStoreError("database_version", "Unsupported image-admin database version")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS image_admin_runs (
                    run_id TEXT PRIMARY KEY, spec_sha256 TEXT NOT NULL, spec_content_sha256 TEXT NOT NULL,
                    revision INTEGER NOT NULL, active_stage INTEGER NOT NULL, completed_json TEXT NOT NULL,
                    decisions_json TEXT NOT NULL, summary_json TEXT NOT NULL, saved_at TEXT NOT NULL,
                    status TEXT NOT NULL, last_commit_id TEXT);
                CREATE TABLE IF NOT EXISTS image_admin_commits (
                    commit_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    kind TEXT NOT NULL, committed_at TEXT NOT NULL, decisions_sha256 TEXT NOT NULL,
                    normalized_json TEXT NOT NULL, front_json TEXT NOT NULL, groups_json TEXT NOT NULL,
                    UNIQUE(run_id, revision));
                CREATE TABLE IF NOT EXISTS image_admin_events (
                    event_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    operation TEXT NOT NULL, stage INTEGER NOT NULL, body_sha256 TEXT NOT NULL,
                    before_revision INTEGER NOT NULL, after_revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL, body_json TEXT NOT NULL, response_json TEXT NOT NULL,
                    UNIQUE(run_id, request_id));
                CREATE TRIGGER IF NOT EXISTS image_admin_events_no_update BEFORE UPDATE ON image_admin_events
                    BEGIN SELECT RAISE(ABORT, 'audit events are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS image_admin_events_no_delete BEFORE DELETE ON image_admin_events
                    BEGIN SELECT RAISE(ABORT, 'audit events are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS image_admin_commits_no_update BEFORE UPDATE ON image_admin_commits
                    BEGIN SELECT RAISE(ABORT, 'gallery commits are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS image_admin_commits_no_delete BEFORE DELETE ON image_admin_commits
                    BEGIN SELECT RAISE(ABORT, 'gallery commits are immutable'); END;
                PRAGMA user_version=1;
            """)
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM image_admin_runs WHERE run_id=?", (self.run_id,)).fetchone()
            if row:
                self._check_binding(row)
                db.commit()
                return  # An existing draft is never replaced by startup seed data.
            template = blank_group_workflow_decisions(spec)
            initial = self._wire(template)
            confirmed = None
            if seed_decisions is not None:
                # Startup seed may be a normalized import, not an HTTP payload.
                seed = {key: copy.deepcopy(value) for key, value in seed_decisions.items() if key in WIRE_KEYS}
                for field in ("duplicate_reviews", "similarity_reviews"):
                    allowed = self._review_keys(field)
                    seed[field] = [{key: value for key, value in row.items() if key in allowed}
                                   for row in seed.get(field, [])]
                initial = self._wire(seed, base=initial)
                confirmed = self._validated(initial, stage=4)
                if confirmed["private_front_export_status"] != "ready":
                    raise AdminStoreError("invalid_seed", "A seeded gallery requires a complete validated human decision")
            saved_at = _now()
            commit_id = self._insert_commit(db, confirmed, 0, saved_at, "seed") if confirmed else None
            summary = self._summary(initial, len(confirmed["private_front_export_items"]) if confirmed else 0)
            db.execute("INSERT INTO image_admin_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (self.run_id, self.spec_sha256, self.spec_content_sha256, 0, 1, "[]", _json(initial), _json(summary), saved_at, "saved", commit_id))
            db.commit()

    @contextmanager
    def _connection(self, *, create: bool = False):
        db = None
        try:
            if create:
                db = sqlite3.connect(str(self.db_path), timeout=5, isolation_level=None)
            else:
                db = sqlite3.connect(self.db_path.as_uri() + "?mode=rw", uri=True, timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=5000")
            yield db
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise AdminStoreError("database_busy", "Another save is in progress; retry this request", status=409) from exc
            raise
        finally:
            if db is not None:
                if db.in_transaction:
                    db.rollback()
                db.close()

    def _check_binding(self, row: sqlite3.Row) -> None:
        if row["spec_sha256"] != self.spec_sha256 or row["spec_content_sha256"] != self.spec_content_sha256:
            raise AdminStoreError("spec_mismatch", "The saved administrator belongs to a different immutable source spec", status=409)

    @staticmethod
    def _review_keys(field: str) -> set[str]:
        common = {"candidate_id", "decision", "selected_ids"}
        return common | ({"remainder_distinct"} if field == "duplicate_reviews" else {"tags_text", "memo_text"})

    def _wire(self, value: Any, *, base: dict | None = None) -> dict:
        if not isinstance(value, dict) or set(value) - WIRE_KEYS:
            raise AdminStoreError("invalid_decisions", "Unknown decision properties are not accepted")
        identity = {"schema_version": GROUP_WORKFLOW_DECISIONS_V3_SCHEMA_VERSION, "run_id": self.run_id, "spec_sha256": self.spec_sha256}
        if any(value.get(key) != expected for key, expected in identity.items()):
            raise AdminStoreError("decision_binding", "Decision schema, run and spec identity must match")
        result = copy.deepcopy(base or {**identity, "approval_policy": DEFAULT_IMAGE_APPROVAL_POLICY,
                                      "reviewer": "", "reviewed_at": "", "duplicate_reviews": [],
                                      "similarity_reviews": [], "image_approvals": []})
        if value.get("approval_policy", DEFAULT_IMAGE_APPROVAL_POLICY) != DEFAULT_IMAGE_APPROVAL_POLICY:
            raise AdminStoreError("decision_binding", "Approval policy is fixed")
        for field in ("reviewer", "reviewed_at", "notes"):
            if field in value:
                result[field] = _text(value[field], field, max_bytes=8000 if field == "notes" else 800)
        if "metadata_optional" in value:
            if value["metadata_optional"] is not True:
                raise AdminStoreError("decision_binding", "Personal memos remain optional")
            result["metadata_optional"] = True
        for field, candidate_field in (("duplicate_reviews", "duplicate_candidates"), ("similarity_reviews", "similarity_candidates")):
            if field not in value:
                continue
            rows = value[field]
            if not isinstance(rows, list):
                raise AdminStoreError("invalid_decisions", f"{field} must be an array")
            merged = {row["candidate_id"]: copy.deepcopy(row) for row in result[field]}
            seen = set()
            for row in rows:
                if not isinstance(row, dict) or set(row) - self._review_keys(field):
                    raise AdminStoreError("invalid_decisions", f"Unknown {field} row properties")
                ident = row.get("candidate_id")
                if not isinstance(ident, str) or ident not in self.candidates[candidate_field] or ident in seen:
                    raise AdminStoreError("invalid_id", f"Unknown or repeated {field} candidate")
                seen.add(ident)
                candidate = self.candidates[candidate_field][ident]
                selected = row.get("selected_ids", [])
                if (not isinstance(selected, list) or any(not isinstance(i, str) for i in selected)
                        or len(set(selected)) != len(selected) or not set(selected) <= set(candidate["member_ids"])):
                    raise AdminStoreError("invalid_id", "Selected images must be unique candidate members")
                choices = {"same_image_subset", "distinct_images", "defer"} if field == "duplicate_reviews" else {"approve_selected", "keep_separate", "defer"}
                if not isinstance(row.get("decision"), str) or row["decision"] not in choices:
                    raise AdminStoreError("invalid_decisions", "Unknown review decision")
                cleaned = {"candidate_id": ident, "decision": row["decision"],
                           "selected_ids": [i for i in candidate["member_ids"] if i in selected]}
                if field == "duplicate_reviews":
                    flag = row.get("remainder_distinct", False)
                    if not isinstance(flag, bool):
                        raise AdminStoreError("invalid_decisions", "remainder_distinct must be boolean")
                    cleaned["remainder_distinct"] = flag
                else:
                    cleaned.update({key: _text(row.get(key, ""), key) for key in ("tags_text", "memo_text")})
                merged[ident] = cleaned
            result[field] = [merged[ident] for ident in self.candidates[candidate_field] if ident in merged]
        if "image_approvals" in value:
            rows = value["image_approvals"]
            if not isinstance(rows, list):
                raise AdminStoreError("invalid_decisions", "image_approvals must be an array")
            merged = {row["id"]: copy.deepcopy(row) for row in result["image_approvals"]}
            seen = set()
            for row in rows:
                if not isinstance(row, dict) or set(row) - {"id", "approved", "memo_text"}:
                    raise AdminStoreError("invalid_decisions", "Unknown image approval properties")
                ident = row.get("id")
                if not isinstance(ident, str) or ident not in self.active_source_ids or ident in seen:
                    raise AdminStoreError("invalid_id", "Image approvals must reference unique eligible source images")
                seen.add(ident)
                if not isinstance(row.get("approved"), bool):
                    raise AdminStoreError("invalid_decisions", "Image approval must be boolean")
                cleaned = {"id": ident, "approved": row["approved"], "memo_text": _text(row.get("memo_text", ""), "memo_text")}
                if ident in self.readonly_ids and cleaned != self.baseline_choices[ident]:
                    raise AdminStoreError("readonly_baseline", "Existing baseline approvals and memos cannot change")
                merged[ident] = cleaned
            merged.update(copy.deepcopy(self.baseline_choices))
            result["image_approvals"] = [merged[ident] for ident in self.spec["stage1"]["active_ids"] if ident in merged]
        return result

    def _validated(self, decisions: dict, *, stage: int = 4, draft: bool = False) -> dict:
        projected = copy.deepcopy(decisions)
        if draft:
            projected.update({"reviewer": "draft-validation-not-human-approval", "reviewed_at": "2026-01-01T00:00:00Z"})
        for field in ("duplicate_reviews", "similarity_reviews"):
            for row in projected[field]:
                if field == "similarity_reviews" and stage < 3:
                    row.update({"decision": "defer", "selected_ids": []})
                elif row["decision"] in {"defer", "distinct_images", "keep_separate"}:
                    # Checkbox memory is useful when changing a radio decision
                    # back later. Non-grouping decisions carry no selected
                    # membership into the authoritative validator or gallery.
                    row["selected_ids"] = []
        # Retained-only validator input must not erase the durable preferences
        # of images hidden by a newly edited duplicate decision.
        duplicate_only = copy.deepcopy(projected)
        duplicate_only["similarity_reviews"] = []
        duplicate_only["image_approvals"] = []
        try:
            retention = validate_group_workflow_decisions(self.spec, duplicate_only)["stage2_overlay"]["active_ids"]
            projected["image_approvals"] = [row for row in projected["image_approvals"] if row["id"] in retention]
            return validate_group_workflow_decisions(self.spec, projected)
        except ValueError as exc:
            raise AdminStoreError("invalid_review", str(exc)) from exc

    def _summary(self, decisions: dict, confirmed_count: int) -> dict:
        errors = []
        try:
            normalized = self._validated(decisions, draft=True)
        except AdminStoreError as exc:
            errors.append(str(exc))
            try:
                normalized = self._validated(decisions, stage=2, draft=True)
            except AdminStoreError:
                normalized = None
        return {
            "retained_image_ids": normalized["stage2_overlay"]["active_ids"] if normalized else list(self.spec["stage1"]["active_ids"]),
            "duplicate_gate_status": normalized["stage2_duplicate_gate_status"] if normalized else "invalid_draft",
            "unresolved_duplicate_candidate_ids": normalized["unresolved_duplicate_candidate_ids"] if normalized else list(self.candidates["duplicate_candidates"]),
            "similarity_gate_status": normalized["stage3_similarity_gate_status"] if normalized and not errors else "invalid_or_pending_draft",
            "unresolved_similarity_candidate_ids": normalized["unresolved_similarity_candidate_ids"] if normalized else list(self.candidates["similarity_candidates"]),
            "skipped_similarity_candidate_ids": normalized["skipped_similarity_candidate_ids"] if normalized else [],
            "draft_front_count": len(normalized["private_front_export_items"]) if normalized and not errors else 0,
            "confirmed_front_count": confirmed_count, "validation_errors": errors,
            "physical_deletions": 0, "provider_calls": 0, "public_release_approval": False,
        }

    def _insert_commit(self, db: sqlite3.Connection, normalized: dict, revision: int, timestamp: str, kind: str) -> str:
        decision_sha = _hash(normalized)
        commit_id = _hash({"run_id": self.run_id, "revision": revision, "decisions_sha256": decision_sha})
        front = {"run_id": self.run_id, "spec_sha256": self.spec_sha256,
                 "decisions_schema_version": GROUP_WORKFLOW_DECISIONS_V3_SCHEMA_VERSION,
                 "front_approval_policy": DEFAULT_IMAGE_APPROVAL_POLICY,
                 "status": normalized["private_front_export_status"],
                 "front_review_complete": normalized["front_review_complete"],
                 "stage2_duplicate_gate_status": normalized["stage2_duplicate_gate_status"],
                 "stage3_similarity_gate_status": normalized["stage3_similarity_gate_status"],
                 "stage4_gate_status": normalized["stage4_gate_status"],
                 "items": normalized["private_front_export_items"], "release_eligible": False, "public_rights_approved": False}
        groups = {"run_id": self.run_id, "spec_sha256": self.spec_sha256, "groups": normalized["approved_similarity_groups"]}
        db.execute("INSERT INTO image_admin_commits VALUES (?,?,?,?,?,?,?,?,?)",
                   (commit_id, self.run_id, revision, kind, timestamp, decision_sha, _json(normalized), _json(front), _json(groups)))
        return commit_id

    def _commit_info(self, db: sqlite3.Connection, commit_id: str | None) -> dict | None:
        if commit_id is None:
            return None
        row = db.execute("SELECT * FROM image_admin_commits WHERE commit_id=? AND run_id=?", (commit_id, self.run_id)).fetchone()
        return {"id": row["commit_id"], "revision": row["revision"], "kind": row["kind"], "committed_at": row["committed_at"],
                "decisions_sha256": row["decisions_sha256"], "front_count": len(json.loads(row["front_json"])["items"])}

    def _state(self, db: sqlite3.Connection) -> dict:
        row = db.execute("SELECT * FROM image_admin_runs WHERE run_id=?", (self.run_id,)).fetchone()
        if row is None:
            raise AdminStoreError("missing_run", "Administrator run is missing", status=409)
        self._check_binding(row)
        return {"run_id": self.run_id, "revision": row["revision"], "active_stage": row["active_stage"],
                "completed_stages": json.loads(row["completed_json"]), "decisions": json.loads(row["decisions_json"]),
                "summary": json.loads(row["summary_json"]), "saved_at": row["saved_at"], "status": row["status"],
                "last_commit": self._commit_info(db, row["last_commit_id"])}

    def _with_spec(self, state: dict) -> dict:
        return {**copy.deepcopy(state), "spec": copy.deepcopy(self.spec)}

    def state(self) -> dict:
        with self._connection() as db:
            db.execute("BEGIN")
            return self._with_spec(self._state(db))

    def gallery(self) -> dict:
        with self._connection() as db:
            db.execute("BEGIN")
            state = self._state(db)
            if not state["last_commit"]:
                return {"run_id": self.run_id, "commit_id": None, "revision": None, "committed_at": None, "decisions_sha256": None,
                        "items": [], "groups": [], "retained_ids": [], "front_export": None, "release_eligible": False, "public_rights_approved": False}
            row = db.execute("SELECT * FROM image_admin_commits WHERE commit_id=?", (state["last_commit"]["id"],)).fetchone()
            front, groups = json.loads(row["front_json"]), json.loads(row["groups_json"])
            normalized = json.loads(row["normalized_json"])
            return {"run_id": self.run_id, "commit_id": row["commit_id"], "revision": row["revision"], "committed_at": row["committed_at"],
                    "decisions_sha256": row["decisions_sha256"], "items": front["items"], "groups": groups["groups"],
                    "retained_ids": normalized["stage2_overlay"]["active_ids"],
                    "front_export": front, "release_eligible": False, "public_rights_approved": False}

    def save_draft(self, body: dict) -> dict:
        return self._mutate("draft", body)

    def advance(self, body: dict) -> dict:
        return self._mutate("advance", body)

    def rewind(self, body: dict) -> dict:
        return self._mutate("rewind", body)

    def _mutate(self, operation: str, body: Any) -> dict:
        required = {"run_id", "expected_revision", "request_id", "stage"}
        allowed = required | ({"target_stage"} if operation == "rewind" else {"decisions"})
        if (not isinstance(body, dict) or set(body) - allowed or not required <= set(body)
                or (operation == "draft" and "decisions" not in body)):
            raise AdminStoreError("invalid_request", "Missing or unknown mutation properties")
        if body["run_id"] != self.run_id:
            raise AdminStoreError("run_mismatch", "The request belongs to another run", status=409)
        if type(body["expected_revision"]) is not int or body["expected_revision"] < 0:
            raise AdminStoreError("invalid_revision", "expected_revision must be a nonnegative integer")
        if type(body["stage"]) is not int or body["stage"] not in range(1, 5):
            raise AdminStoreError("invalid_stage", "stage must be an integer from 1 to 4")
        if not isinstance(body["request_id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", body["request_id"]):
            raise AdminStoreError("invalid_request_id", "request_id must be a short unique identifier")
        body_hash = _hash({"operation": operation, "body": body})
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM image_admin_events WHERE run_id=? AND request_id=?", (self.run_id, body["request_id"])).fetchone()
            if prior:
                if prior["body_sha256"] != body_hash:
                    raise AdminStoreError("request_id_conflict", "request_id was already used for a different request", status=409)
                return self._with_spec(json.loads(prior["response_json"]))
            state = self._state(db)
            if state["revision"] != body["expected_revision"]:
                raise AdminStoreError("revision_conflict", "A newer save exists; reload before editing", status=409,
                                      details={"expected_revision": body["expected_revision"], "current_revision": state["revision"]})
            stage = state["active_stage"]
            if body["stage"] != stage:
                raise AdminStoreError("stage_conflict", "Only the active stage can be edited", status=409,
                                      details={"active_stage": stage})
            decisions = self._wire(body["decisions"], base=state["decisions"]) if "decisions" in body else copy.deepcopy(state["decisions"])
            changed = {key for key in WIRE_KEYS if decisions.get(key) != state["decisions"].get(key)}
            if changed - EDIT_FIELDS[stage] - {"reviewer"}:
                raise AdminStoreError("stage_field_violation", "Rewind before changing fields from another stage")
            completed = [s for s in state["completed_stages"] if s < stage] if changed else list(state["completed_stages"])
            if stage == 2 and "duplicate_reviews" in changed:
                for row in decisions["similarity_reviews"]:
                    row["decision"] = "defer"  # Keep its selection, notes and all per-image choices.
            active_stage, status, commit_id = stage, "saved", state["last_commit"]["id"] if state["last_commit"] else None
            timestamp, revision = _now(), state["revision"] + 1
            if operation == "rewind":
                target = body.get("target_stage")
                if type(target) is not int or not 1 <= target <= stage:
                    raise AdminStoreError("invalid_rewind", "target_stage must be the current or an earlier stage")
                active_stage, completed = target, [s for s in completed if s < target]
            elif operation == "advance":
                reviewer = decisions.get("reviewer", "").strip()
                if not reviewer or len(reviewer.encode("utf-8")) > 200:
                    raise AdminStoreError("reviewer_required", "Enter a reviewer identity before approving a stage")
                if completed != list(range(1, stage)):
                    # A committed stage4 can be explicitly committed again, but
                    # never used to bypass an unacknowledged earlier stage.
                    if not (stage == 4 and completed == [1, 2, 3, 4]):
                        raise AdminStoreError("stage_gate", "All preceding stages must be explicitly acknowledged", status=409)
                if self.validate_source is not None:
                    self.validate_source()
                decisions.update({"reviewer": reviewer, "reviewed_at": timestamp})
                if stage >= 2:
                    normalized = self._validated(decisions, stage=stage)
                    if normalized["stage2_duplicate_gate_status"] != "complete":
                        raise AdminStoreError("duplicate_gate", "Resolve every duplicate review before advancing")
                    if stage >= 3 and normalized["stage3_similarity_gate_status"] != "complete":
                        raise AdminStoreError("similarity_gate", "Resolve every similarity review before advancing")
                    if stage == 4:
                        if normalized["private_front_export_status"] != "ready":
                            raise AdminStoreError("final_gate", "The final private gallery is not ready")
                        commit_id = self._insert_commit(db, normalized, revision, timestamp, "stage4")
                        status = "committed"
                completed, active_stage = list(range(1, stage + 1)), min(4, stage + 1)
            confirmed_count = self._commit_info(db, commit_id)["front_count"] if commit_id else 0
            summary = self._summary(decisions, confirmed_count)
            db.execute("UPDATE image_admin_runs SET revision=?,active_stage=?,completed_json=?,decisions_json=?,summary_json=?,saved_at=?,status=?,last_commit_id=? WHERE run_id=?",
                       (revision, active_stage, _json(completed), _json(decisions), _json(summary), timestamp, status, commit_id, self.run_id))
            response = self._state(db)
            db.execute("INSERT INTO image_admin_events (run_id,request_id,operation,stage,body_sha256,before_revision,after_revision,created_at,body_json,response_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (self.run_id, body["request_id"], operation, stage, body_hash, state["revision"], revision, timestamp, _json(body), _json(response)))
            db.commit()
            return self._with_spec(response)


__all__ = ["AdminStore", "AdminStoreError"]
