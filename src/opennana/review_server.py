from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit


SRC_ROOT = Path(__file__).resolve().parents[1]
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from duplicate_review_store import (  # noqa: E402
    DuplicateGroupNotFound,
    DuplicateGroupStore,
    DuplicateIndexUnavailable,
)
from public_catalog_store import (  # noqa: E402
    DEFAULT_LIMIT as PUBLIC_DEFAULT_LIMIT,
    MAX_LIMIT as PUBLIC_MAX_LIMIT,
    PublicCatalogRecordNotFound,
    PublicCatalogStore,
    PublicCatalogUnavailable,
)

try:
    from .apply_decisions import validate_and_apply
    from .build_review_queue import history_path_for_queue
    from .common import ARCHIVE_ROOT, atomic_write_json, atomic_write_text, read_json, sha256_text, stable_json
except ImportError:
    from apply_decisions import validate_and_apply
    from build_review_queue import history_path_for_queue
    from common import ARCHIVE_ROOT, atomic_write_json, atomic_write_text, read_json, sha256_text, stable_json


API_PREFIX = "/api/review/v1"
PUBLIC_API_PREFIX = "/api/public/v1"
ADMIN_API_PREFIX = "/api/admin/v1"
DUPLICATES_API_PREFIX = "/api/duplicates/v1"
DUPLICATES_DEFAULT_LIMIT = 20
DUPLICATES_MAX_LIMIT = 50
SESSION_COOKIE = "opennana_review_session"
MAX_BODY_BYTES = 2 * 1024 * 1024
TOKEN_TTL_SECONDS = 5 * 60
DECISION_ACTIONS = ("approve", "defer", "group", "reject")


class ReviewApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AdminAccessPolicy:
    mode: str
    token_sha256: str | None = None

    @classmethod
    def from_env(cls) -> "AdminAccessPolicy":
        token = str(os.environ.get("IMAGE_ARCHIVE_ADMIN_TOKEN") or "").strip()
        if token:
            return cls(mode="bearer_token", token_sha256=secure_hash(token))
        return cls(mode="loopback_local_only", token_sha256=None)

    def authorize(self, handler: "ReviewRequestHandler") -> None:
        if self.mode == "loopback_local_only":
            if _is_loopback_address(handler.client_address[0]):
                return
            raise ReviewApiError(HTTPStatus.FORBIDDEN, "admin_loopback_required", "admin routes require loopback access")
        if self.mode == "bearer_token":
            header = str(handler.headers.get("Authorization", "")).strip()
            if not header.startswith("Bearer "):
                raise ReviewApiError(HTTPStatus.FORBIDDEN, "admin_token_required", "admin bearer token is required")
            token = header.removeprefix("Bearer ").strip()
            if not token or not self.token_sha256 or not hmac.compare_digest(secure_hash(token), self.token_sha256):
                raise ReviewApiError(HTTPStatus.FORBIDDEN, "admin_token_invalid", "admin bearer token is invalid")
            return
        raise ReviewApiError(HTTPStatus.FORBIDDEN, "admin_auth_mode_invalid", "admin auth mode is invalid")


def _is_loopback_address(value: str) -> bool:
    try:
        return ipaddress.ip_address(str(value).split("%", 1)[0]).is_loopback
    except ValueError:
        return str(value).casefold() == "localhost"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def epoch_to_iso(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def secure_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def decision_request_digest(draft: dict[str, Any]) -> str:
    rows = []
    for row in draft.get("decisions", []):
        rows.append({
            "queue_id": row.get("queue_id"),
            "content_sha256": row.get("content_sha256"),
            "decision": row.get("decision"),
            "group_with": row.get("group_with"),
            "note": str(row.get("note") or ""),
        })
    rows.sort(key=lambda item: str(item.get("queue_id") or ""))
    return sha256_text(stable_json({
        "queue_revision": draft.get("queue_revision"),
        "run_id": draft.get("run_id"),
        "decisions": rows,
    }, indent=None))


@dataclass(frozen=True)
class ReviewPaths:
    archive_root: Path

    @property
    def data_root(self) -> Path:
        return self.archive_root / "data" / "private-research" / "opennana"

    @property
    def queue(self) -> Path:
        return self.data_root / "review_queue" / "current.json"

    @property
    def state(self) -> Path:
        return self.data_root / "state.json"

    @property
    def config(self) -> Path:
        return self.data_root / "config.json"

    @property
    def draft(self) -> Path:
        return self.data_root / "decisions" / "decision-draft.json"

    @property
    def review_js(self) -> Path:
        return self.archive_root / "legacy" / "current_archive" / "opennana-review-data.js"

    @property
    def preview_dir(self) -> Path:
        return self.data_root / "decisions" / "api-previews"

    @property
    def request_dir(self) -> Path:
        return self.data_root / "decisions" / "api-requests"

    @property
    def receipt_dir(self) -> Path:
        return self.data_root / "decisions" / "api-commits"


class ReviewService:
    """Pure review workflow boundary used by both localhost and future adapters."""

    def __init__(
        self,
        paths: ReviewPaths,
        *,
        token_ttl_seconds: int = TOKEN_TTL_SECONDS,
        promotion_command: list[str] | None = None,
        apply_script: Path | None = None,
        clock: Any = time.time,
    ) -> None:
        self.paths = paths
        self.token_ttl_seconds = max(30, int(token_ttl_seconds))
        self.promotion_command = list(promotion_command or [])
        self.apply_script = apply_script or Path(__file__).with_name("apply_decisions.py")
        self.clock = clock
        self._commit_lock = threading.Lock()

    def state(self) -> dict[str, Any]:
        queue = self._load_queue()
        return {
            "schema_version": "opennana-review-api-state-1.0",
            "status": "ready",
            "queue_revision": queue["queue_revision"],
            "run_id": queue["run_id"],
            "item_count": len(queue.get("items", [])),
            "queue_summary": queue.get("summary", {}),
            "allowed_decisions": list(DECISION_ACTIONS),
            "requires_complete_decision_set": True,
            "approval_effect": "canonicalization_pending",
            "internal_archive_auto_promotion": bool(self.promotion_command),
            "rights_clearance_effect": False,
            "public_release_effect": False,
            "durable_draft": self._load_durable_draft(queue),
        }

    def save_draft(self, draft: dict[str, Any]) -> dict[str, Any]:
        queue, normalized = self._validate_partial_draft(draft)
        atomic_write_json(self.paths.draft, normalized)
        counts = Counter(row["decision"] for row in normalized["decisions"])
        return {
            "schema_version": "opennana-review-draft-save-1.0",
            "status": "saved",
            "queue_revision": queue["queue_revision"],
            "run_id": queue["run_id"],
            "decision_count": len(normalized["decisions"]),
            "counts": {action: counts.get(action, 0) for action in DECISION_ACTIONS},
            "public_release_effect": False,
        }

    def preview(self, draft: dict[str, Any], *, session_id: str) -> dict[str, Any]:
        queue, normalized, pending = self._validate_complete_draft(draft)
        request_sha256 = decision_request_digest(draft)
        batch_id = f"ONN-BATCH-{request_sha256[:24].upper()}"
        token = secrets.token_urlsafe(32)
        expires_at = float(self.clock()) + self.token_ttl_seconds
        grant = {
            "schema_version": "opennana-review-preview-grant-1.0",
            "decision_batch_id": batch_id,
            "queue_revision": queue["queue_revision"],
            "run_id": queue["run_id"],
            "request_sha256": request_sha256,
            "commit_token_sha256": secure_hash(token),
            "session_sha256": secure_hash(session_id),
            "expires_at_epoch": expires_at,
            "expires_at": epoch_to_iso(expires_at),
            "created_at": utc_now(),
        }
        atomic_write_json(self.paths.preview_dir / f"{batch_id}.json", grant)
        counts = Counter(row["decision"] for row in normalized["decisions"])
        return {
            "schema_version": "opennana-review-preview-1.0",
            "status": "preview_ready",
            "decision_batch_id": batch_id,
            "queue_revision": queue["queue_revision"],
            "decision_count": len(normalized["decisions"]),
            "counts": {action: counts.get(action, 0) for action in DECISION_ACTIONS},
            "canonicalization_pending": pending["record_count"],
            "remaining_queued": 0,
            "commit_token": token,
            "commit_token_expires_at": grant["expires_at"],
            "approval_effect": "canonicalization_pending",
            "rights_clearance_effect": False,
            "public_release_effect": False,
        }

    def commit(
        self,
        draft: dict[str, Any],
        *,
        decision_batch_id: str,
        commit_token: str,
        session_id: str,
    ) -> dict[str, Any]:
        if not decision_batch_id or not commit_token:
            raise ReviewApiError(HTTPStatus.BAD_REQUEST, "commit_credentials_missing", "decision_batch_id and commit_token are required")
        request_sha256 = decision_request_digest(draft)
        expected_batch_id = f"ONN-BATCH-{request_sha256[:24].upper()}"
        if not hmac.compare_digest(decision_batch_id, expected_batch_id):
            raise ReviewApiError(HTTPStatus.CONFLICT, "decision_batch_mismatch", "decision_batch_id does not match the submitted decisions")

        with self._commit_lock:
            receipt_path = self.paths.receipt_dir / f"{decision_batch_id}.json"
            if receipt_path.exists():
                receipt = read_json(receipt_path)
                self._validate_committed_retry(receipt, request_sha256, commit_token)
                result = dict(receipt["result"])
                result["idempotent"] = True
                return result

            grant_path = self.paths.preview_dir / f"{decision_batch_id}.json"
            if not grant_path.exists():
                raise ReviewApiError(HTTPStatus.CONFLICT, "preview_required", "preview this complete decision set before commit")
            grant = read_json(grant_path)
            self._validate_grant(grant, request_sha256, commit_token, session_id)

            queue, normalized, pending = self._validate_complete_draft(draft)
            if queue["queue_revision"] != grant.get("queue_revision"):
                raise ReviewApiError(HTTPStatus.CONFLICT, "stale_queue_revision", "the review queue changed after preview")
            if pending.get("public_release_eligible") is not False or any(
                record.get("rights", {}).get("release_eligible") is not False
                for record in pending.get("records", [])
            ):
                raise ReviewApiError(HTTPStatus.CONFLICT, "release_boundary_violation", "decision apply attempted to change public release eligibility")

            request_path = self.paths.request_dir / f"{decision_batch_id}.json"
            if request_path.exists() and stable_json(read_json(request_path)) != stable_json(draft):
                raise ReviewApiError(HTTPStatus.CONFLICT, "immutable_request_collision", "an immutable request with this batch id already exists")
            atomic_write_json(request_path, draft)
            result = self._apply_with_rollback(
                draft=draft,
                queue=queue,
                decision_batch_id=decision_batch_id,
                request_path=request_path,
                request_sha256=request_sha256,
            )
            receipt = {
                "schema_version": "opennana-review-commit-receipt-1.0",
                "decision_batch_id": decision_batch_id,
                "request_sha256": request_sha256,
                "commit_token_sha256": secure_hash(commit_token),
                "queue_revision": queue["queue_revision"],
                "committed_at": utc_now(),
                "result": result,
            }
            atomic_write_json(receipt_path, receipt)
            return result

    def _load_queue(self) -> dict[str, Any]:
        if not self.paths.queue.exists():
            raise ReviewApiError(HTTPStatus.SERVICE_UNAVAILABLE, "review_queue_missing", "current review queue is unavailable")
        queue = read_json(self.paths.queue)
        if not isinstance(queue, dict) or not isinstance(queue.get("items"), list):
            raise ReviewApiError(HTTPStatus.SERVICE_UNAVAILABLE, "review_queue_malformed", "current review queue is malformed")
        return queue

    def _load_durable_draft(self, queue: dict[str, Any]) -> dict[str, Any]:
        empty = {
            "schema_version": "opennana-durable-draft-1.0",
            "status": "empty",
            "run_id": queue["run_id"],
            "queue_revision": queue["queue_revision"],
            "decision_count": 0,
            "decisions": [],
            "stale_decision_count": 0,
        }
        if not self.paths.draft.exists():
            return empty
        try:
            payload = read_json(self.paths.draft)
        except Exception:
            return {
                **empty,
                "status": "malformed",
            }
        if not isinstance(payload, dict):
            return {
                **empty,
                "status": "malformed",
            }
        payload_run_id = payload.get("run_id")
        payload_revision = payload.get("queue_revision")
        if payload_run_id not in {None, queue["run_id"]} or payload_revision not in {None, queue["queue_revision"]}:
            stale_count = len(payload.get("decisions") or []) if isinstance(payload.get("decisions"), list) else 0
            return {
                **empty,
                "status": "stale",
                "stale_decision_count": stale_count,
            }
        try:
            _, normalized = self._validate_partial_draft(payload)
        except ReviewApiError:
            return {
                **empty,
                "status": "malformed",
            }
        return {
            "schema_version": "opennana-durable-draft-1.0",
            "status": "ready",
            "run_id": queue["run_id"],
            "queue_revision": queue["queue_revision"],
            "decision_count": len(normalized["decisions"]),
            "decisions": normalized["decisions"],
            "stale_decision_count": 0,
        }

    def _validate_partial_draft(self, draft: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(draft, dict):
            raise ReviewApiError(HTTPStatus.BAD_REQUEST, "decision_draft_malformed", "decision draft must be a JSON object")
        queue = self._load_queue()
        rows = draft.get("decisions")
        if not isinstance(rows, list):
            raise ReviewApiError(HTTPStatus.BAD_REQUEST, "decision_draft_malformed", "decisions must be a JSON array")
        queue_items = queue.get("items", [])
        queue_by_id = {item.get("queue_id"): item for item in queue_items}
        normalized_rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ReviewApiError(HTTPStatus.BAD_REQUEST, "decision_rows_invalid", "every decision row must be an object")
            queue_id = str(row.get("queue_id") or "").strip()
            if not queue_id or queue_id not in queue_by_id:
                raise ReviewApiError(HTTPStatus.CONFLICT, "decision_rows_invalid", "decision references an unknown current queue item")
            if queue_id in seen:
                raise ReviewApiError(HTTPStatus.BAD_REQUEST, "decision_rows_invalid", "every decision must have one unique queue_id")
            seen.add(queue_id)
            action = str(row.get("decision") or "").strip()
            if action not in DECISION_ACTIONS:
                raise ReviewApiError(HTTPStatus.BAD_REQUEST, "decision_rows_invalid", f"unsupported decision {action!r}")
            item = queue_by_id[queue_id]
            content_sha256 = str(row.get("content_sha256") or item.get("content_sha256") or "").strip()
            if content_sha256 != str(item.get("content_sha256") or ""):
                raise ReviewApiError(HTTPStatus.CONFLICT, "stale_queue_revision", f"stale content hash for {queue_id}")
            group_with = str(row.get("group_with") or "").strip() if action == "group" else ""
            if action == "group" and not group_with:
                raise ReviewApiError(HTTPStatus.BAD_REQUEST, "decision_rows_invalid", f"group decision requires group_with for {queue_id}")
            normalized_rows.append({
                "queue_id": queue_id,
                "style_id": str(row.get("style_id") or item.get("style_id") or item.get("upstream_id") or "").strip() or None,
                "source_id": str(row.get("source_id") or item.get("source_id") or item.get("source") or "").strip() or None,
                "upstream_id": str(row.get("upstream_id") or item.get("upstream_id") or "").strip() or None,
                "content_sha256": content_sha256,
                "decision": action,
                "group_with": group_with or None,
                "note": str(row.get("note") or ""),
                "saved_at": str(row.get("saved_at") or utc_now()),
            })
        normalized_rows.sort(key=lambda item: item["queue_id"])
        return queue, {
            "schema_version": "opennana-decision-draft-1.0",
            "run_id": queue["run_id"],
            "queue_revision": queue["queue_revision"],
            "instructions": "Partial durable draft for the current review queue. Commit still requires a complete explicit decision set.",
            "decision_count": len(normalized_rows),
            "decisions": normalized_rows,
        }

    def _validate_complete_draft(self, draft: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        if not isinstance(draft, dict):
            raise ReviewApiError(HTTPStatus.BAD_REQUEST, "decision_draft_malformed", "decision draft must be a JSON object")
        queue = self._load_queue()
        rows = draft.get("decisions")
        if not isinstance(rows, list):
            raise ReviewApiError(HTTPStatus.BAD_REQUEST, "decision_draft_malformed", "decisions must be a JSON array")
        queue_items = queue.get("items", [])
        queue_ids = {item.get("queue_id") for item in queue_items}
        row_ids = [row.get("queue_id") for row in rows if isinstance(row, dict)]
        if len(row_ids) != len(rows) or len(set(row_ids)) != len(row_ids):
            raise ReviewApiError(HTTPStatus.BAD_REQUEST, "decision_rows_invalid", "every decision must have one unique queue_id")
        if len(rows) != len(queue_items) or set(row_ids) != queue_ids:
            raise ReviewApiError(HTTPStatus.CONFLICT, "decision_set_incomplete", "all and only current queue items must be decided")
        if any(row.get("decision") in {None, "", "pending"} for row in rows):
            raise ReviewApiError(HTTPStatus.CONFLICT, "pending_decisions", "all current queue items require an explicit decision")
        complete = dict(draft)
        complete["decision_count"] = len(rows)
        try:
            applied, pending, _ = validate_and_apply(queue, complete, read_json(self.paths.state))
        except (KeyError, TypeError, ValueError) as exc:
            message = str(exc)
            code = "stale_queue_revision" if "queue_revision" in message or "stale content hash" in message else "decision_validation_failed"
            status = HTTPStatus.CONFLICT if code == "stale_queue_revision" else HTTPStatus.BAD_REQUEST
            raise ReviewApiError(status, code, message) from exc
        if len(applied.get("decisions", [])) != len(queue_items):
            raise ReviewApiError(HTTPStatus.CONFLICT, "decision_set_incomplete", "validated decision count does not cover the current queue")
        return queue, applied, pending

    def _validate_grant(self, grant: dict[str, Any], request_sha256: str, token: str, session_id: str) -> None:
        if grant.get("request_sha256") != request_sha256:
            raise ReviewApiError(HTTPStatus.CONFLICT, "preview_decisions_changed", "decisions changed after preview")
        if not hmac.compare_digest(str(grant.get("commit_token_sha256") or ""), secure_hash(token)):
            raise ReviewApiError(HTTPStatus.FORBIDDEN, "commit_token_invalid", "commit token is invalid")
        if not hmac.compare_digest(str(grant.get("session_sha256") or ""), secure_hash(session_id)):
            raise ReviewApiError(HTTPStatus.FORBIDDEN, "review_session_changed", "review session changed after preview")
        if float(grant.get("expires_at_epoch") or 0) < float(self.clock()):
            raise ReviewApiError(HTTPStatus.GONE, "commit_token_expired", "commit token expired; preview again")

    @staticmethod
    def _validate_committed_retry(receipt: dict[str, Any], request_sha256: str, token: str) -> None:
        if receipt.get("request_sha256") != request_sha256:
            raise ReviewApiError(HTTPStatus.CONFLICT, "decision_batch_collision", "committed batch has different decisions")
        if not hmac.compare_digest(str(receipt.get("commit_token_sha256") or ""), secure_hash(token)):
            raise ReviewApiError(HTTPStatus.FORBIDDEN, "commit_token_invalid", "commit token is invalid")

    def _apply_with_rollback(
        self,
        *,
        draft: dict[str, Any],
        queue: dict[str, Any],
        decision_batch_id: str,
        request_path: Path,
        request_sha256: str,
    ) -> dict[str, Any]:
        revision_suffix = queue["queue_revision"][:16]
        applied_path = self.paths.data_root / "decisions" / f"applied-{queue['run_id']}--{revision_suffix}.json"
        pending_path = self.paths.data_root / "staging" / f"canonicalization-pending-{queue['run_id']}--{revision_suffix}.json"
        remaining_path = self.paths.queue.parent / f"remaining-{queue['run_id']}--{revision_suffix}.json"
        history_path = history_path_for_queue(self.paths.queue, queue)
        mutable_paths = [self.paths.state, self.paths.queue, self.paths.draft, self.paths.review_js]
        immutable_paths = [history_path, applied_path, pending_path, remaining_path]
        before_mutable = {path: path.read_bytes() if path.exists() else None for path in mutable_paths}
        immutable_existed = {path: path.exists() for path in immutable_paths}
        command = [
            sys.executable,
            str(self.apply_script),
            "--queue", str(self.paths.queue),
            "--decisions", str(request_path),
            "--state", str(self.paths.state),
            "--config", str(self.paths.config),
            "--draft-output", str(self.paths.draft),
            "--js-output", str(self.paths.review_js),
            "--applied-output", str(applied_path),
            "--pending-output", str(pending_path),
            "--remaining-output", str(remaining_path),
            "--apply",
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "decision apply failed")
            try:
                apply_result = json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError("decision apply returned malformed JSON") from exc
            promotion = self._run_promotion_hook(
                decision_batch_id=decision_batch_id,
                request_sha256=request_sha256,
                queue_revision=queue["queue_revision"],
                applied_path=applied_path,
                pending_path=pending_path,
            )
        except Exception as exc:
            self._rollback_files(before_mutable, immutable_existed)
            if isinstance(exc, ReviewApiError):
                raise
            raise ReviewApiError(HTTPStatus.BAD_GATEWAY, "commit_apply_failed", str(exc)) from exc

        counts = Counter(row.get("decision") for row in draft.get("decisions", []))
        promotion_output = promotion.get("output") if isinstance(promotion.get("output"), dict) else {}
        promoted_internal_archive = (
            promotion_output.get("promoted_from_trigger")
            if isinstance(promotion_output.get("promoted_from_trigger"), int)
            else counts.get("approve", 0) + counts.get("group", 0)
        )
        return {
            "schema_version": "opennana-review-commit-result-1.0",
            "status": "committed",
            "idempotent": False,
            "decision_batch_id": decision_batch_id,
            "queue_revision": queue["queue_revision"],
            "counts": {action: counts.get(action, 0) for action in DECISION_ACTIONS},
            "canonicalization_pending": counts.get("approve", 0) + counts.get("group", 0),
            "remaining_queued": int(apply_result.get("remaining_queued", 0)),
            "promoted_internal_archive": int(promoted_internal_archive),
            "promotion": promotion,
            "approval_effect": "canonicalization_pending",
            "rights_clearance_effect": False,
            "public_release_effect": False,
        }

    def _run_promotion_hook(
        self,
        *,
        decision_batch_id: str,
        request_sha256: str,
        queue_revision: str,
        applied_path: Path,
        pending_path: Path,
    ) -> dict[str, Any]:
        if not self.promotion_command:
            return {"status": "not_configured", "public_release_effect": False}
        env = dict(os.environ)
        env.update({
            "OPENNANA_ARCHIVE_ROOT": str(self.paths.archive_root),
            "OPENNANA_DECISION_BATCH_ID": decision_batch_id,
            "OPENNANA_DECISION_REQUEST_SHA256": request_sha256,
            "OPENNANA_QUEUE_REVISION": queue_revision,
            "OPENNANA_APPLIED_PATH": str(applied_path),
            "OPENNANA_PENDING_PATH": str(pending_path),
            "OPENNANA_PUBLIC_RELEASE_ALLOWED": "0",
        })
        completed = subprocess.run(
            self.promotion_command,
            cwd=self.paths.archive_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "promotion hook failed")
        output: Any = None
        if completed.stdout.strip():
            try:
                output = json.loads(completed.stdout)
            except json.JSONDecodeError:
                output = {"message": completed.stdout.strip()[:1000]}
        if self._contains_release_true(output):
            raise RuntimeError("promotion hook reported a public-release boundary violation")
        return {"status": "succeeded", "output": output, "public_release_effect": False}

    @classmethod
    def _contains_release_true(cls, value: Any) -> bool:
        release_keys = {
            "public_release_effect",
            "public_release_eligible",
            "public_release_allowed",
            "release_eligible",
        }
        if isinstance(value, dict):
            return any(
                (key in release_keys and child is True) or cls._contains_release_true(child)
                for key, child in value.items()
            )
        if isinstance(value, list):
            return any(cls._contains_release_true(child) for child in value)
        return False

    def _rollback_files(self, before_mutable: dict[Path, bytes | None], immutable_existed: dict[Path, bool]) -> None:
        for path, content in before_mutable.items():
            if content is None:
                if path.exists():
                    path.unlink()
            else:
                atomic_write_text(path, content.decode("utf-8"))
        for path, existed in immutable_existed.items():
            if not existed and path.exists():
                path.unlink()


class ReviewHttpServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[SimpleHTTPRequestHandler],
        *,
        service: ReviewService,
        static_root: Path,
        allowed_origins: set[str],
        duplicate_store: DuplicateGroupStore | None = None,
        public_catalog_store: PublicCatalogStore | None = None,
        admin_access_policy: AdminAccessPolicy | None = None,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.review_service = service
        self.static_root = static_root
        self.allowed_origins = allowed_origins
        self.duplicate_store = duplicate_store
        self.public_catalog_store = public_catalog_store
        self.admin_access_policy = admin_access_policy or AdminAccessPolicy.from_env()
        self.sessions: dict[str, dict[str, Any]] = {}
        self.session_lock = threading.Lock()

    def issue_session(self) -> tuple[str, str]:
        session_id = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        with self.session_lock:
            self.sessions[session_id] = {"csrf": csrf, "expires_at": time.time() + 12 * 60 * 60}
        return session_id, csrf

    def validate_session(self, session_id: str, csrf: str) -> bool:
        with self.session_lock:
            record = self.sessions.get(session_id)
            return bool(
                record
                and record.get("expires_at", 0) >= time.time()
                and hmac.compare_digest(str(record.get("csrf") or ""), csrf)
            )


class ReviewRequestHandler(SimpleHTTPRequestHandler):
    server: ReviewHttpServer

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(kwargs.pop("directory", None) or ARCHIVE_ROOT), **kwargs)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == f"{PUBLIC_API_PREFIX}/summary" or path == f"{PUBLIC_API_PREFIX}/records" or path.startswith(f"{PUBLIC_API_PREFIX}/records/"):
            self._handle_public_catalog()
            return
        if path == f"{ADMIN_API_PREFIX}/status":
            self._handle_admin_status()
            return
        if path == f"{API_PREFIX}/state":
            self._handle_state()
            return
        if path == f"{DUPLICATES_API_PREFIX}/summary" or path == f"{DUPLICATES_API_PREFIX}/groups" or path.startswith(f"{DUPLICATES_API_PREFIX}/groups/"):
            self._handle_duplicates()
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path not in {f"{API_PREFIX}/draft", f"{API_PREFIX}/preview", f"{API_PREFIX}/commit"}:
            self._send_error_json(HTTPStatus.NOT_FOUND, "route_not_found", "API route not found")
            return
        try:
            session_id = self._require_same_origin_session()
            body = self._read_json_body()
            if path.endswith("/draft"):
                result = self.server.review_service.save_draft(body)
            elif path.endswith("/preview"):
                result = self.server.review_service.preview(body, session_id=session_id)
            else:
                if not isinstance(body, dict) or not isinstance(body.get("decisions"), dict):
                    raise ReviewApiError(HTTPStatus.BAD_REQUEST, "commit_body_malformed", "commit body must include the complete decision draft as decisions")
                result = self.server.review_service.commit(
                    body["decisions"],
                    decision_batch_id=str(body.get("decision_batch_id") or ""),
                    commit_token=str(body.get("commit_token") or ""),
                    session_id=session_id,
                )
            self._send_json(HTTPStatus.OK, result)
        except ReviewApiError as exc:
            self._send_error_json(exc.status, exc.code, exc.message)
        except Exception:
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "review API encountered an internal error")

    def end_headers(self) -> None:
        path = urlsplit(self.path).path
        is_api = (
            path.startswith(API_PREFIX)
            or path.startswith(DUPLICATES_API_PREFIX)
            or path.startswith(PUBLIC_API_PREFIX)
            or path.startswith(ADMIN_API_PREFIX)
        )
        self.send_header("Cache-Control", "no-store" if is_api else "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        super().end_headers()

    def _handle_public_catalog(self) -> None:
        try:
            store = self.server.public_catalog_store
            if store is None:
                raise PublicCatalogUnavailable("public catalog store is unavailable")
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            if parsed.path == f"{PUBLIC_API_PREFIX}/summary":
                self._reject_unknown_query(query, set())
                result = store.summary()
            elif parsed.path == f"{PUBLIC_API_PREFIX}/records":
                self._reject_unknown_query(query, {"q", "source", "lane", "rights_tier", "limit", "offset"})
                result = store.search(
                    q=self._optional_query(query, "q", max_length=200),
                    source_name=self._optional_query(query, "source", max_length=200),
                    lane=self._optional_query(query, "lane", max_length=64),
                    rights_tier=self._optional_query(query, "rights_tier", max_length=8),
                    limit=self._integer_query(query, "limit", PUBLIC_DEFAULT_LIMIT, minimum=1, maximum=PUBLIC_MAX_LIMIT),
                    offset=self._integer_query(query, "offset", 0, minimum=0),
                )
            else:
                self._reject_unknown_query(query, set())
                style_id = unquote(parsed.path.removeprefix(f"{PUBLIC_API_PREFIX}/records/"))
                result = store.record(style_id)
            self._send_json(HTTPStatus.OK, result)
        except PublicCatalogRecordNotFound:
            self._send_public_error(HTTPStatus.NOT_FOUND, "public_record_not_found", "Public catalog record was not found.")
        except PublicCatalogUnavailable as exc:
            self._send_public_error(HTTPStatus.SERVICE_UNAVAILABLE, "public_catalog_unavailable", str(exc))
        except ValueError as exc:
            self._send_public_error(HTTPStatus.BAD_REQUEST, "invalid_query", str(exc))
        except Exception:
            self._send_public_error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "Public catalog API encountered an internal error.")

    def _handle_admin_status(self) -> None:
        try:
            self.server.admin_access_policy.authorize(self)
            public_summary = {}
            if self.server.public_catalog_store is not None:
                try:
                    public_summary = self.server.public_catalog_store.summary()
                except PublicCatalogUnavailable:
                    public_summary = {"status": "unavailable"}
            queue = self.server.review_service.state()
            config_path = self.server.static_root / "platform.config.json"
            config = read_json(config_path) if config_path.is_file() else {}
            result = {
                "schema_version": "image-archive-admin-status-1.0",
                "status": "ready",
                "auth_mode": self.server.admin_access_policy.mode,
                "public_api_prefix": PUBLIC_API_PREFIX,
                "review_api_prefix": API_PREFIX,
                "duplicates_api_prefix": DUPLICATES_API_PREFIX,
                "rights_access_policy": config.get("rights_access_policy") or {},
                "public_catalog": public_summary,
                "review_queue": {
                    "queue_revision": queue.get("queue_revision"),
                    "run_id": queue.get("run_id"),
                    "item_count": queue.get("item_count"),
                    "queue_summary": queue.get("queue_summary"),
                },
            }
            self._send_json(HTTPStatus.OK, result)
        except ReviewApiError as exc:
            self._send_admin_error(exc.status, exc.code, exc.message)
        except Exception:
            self._send_admin_error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "Admin status API encountered an internal error.")

    def _handle_state(self) -> None:
        try:
            state = self.server.review_service.state()
            session_id, csrf = self.server.issue_session()
            state["csrf_token"] = csrf
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Set-Cookie", f"{SESSION_COOKIE}={session_id}; HttpOnly; SameSite=Strict; Path=/")
            payload = stable_json(state).encode("utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except ReviewApiError as exc:
            self._send_error_json(exc.status, exc.code, exc.message)

    def _handle_duplicates(self) -> None:
        try:
            store = self.server.duplicate_store
            if store is None:
                raise DuplicateIndexUnavailable("duplicate analysis index is unavailable")
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            if parsed.path == f"{DUPLICATES_API_PREFIX}/summary":
                self._reject_unknown_query(query, set())
                result = store.summary()
            elif parsed.path == f"{DUPLICATES_API_PREFIX}/groups":
                self._reject_unknown_query(query, {"kind", "limit", "offset", "q", "sort"})
                result = store.list_groups(
                    kind=self._optional_query(query, "kind", max_length=64),
                    limit=self._integer_query(query, "limit", DUPLICATES_DEFAULT_LIMIT, minimum=1, maximum=DUPLICATES_MAX_LIMIT),
                    offset=self._integer_query(query, "offset", 0, minimum=0),
                    q=self._optional_query(query, "q", max_length=200),
                    sort=self._optional_query(query, "sort", max_length=64) or "size_desc",
                )
            else:
                self._reject_unknown_query(query, {"limit", "offset"})
                encoded_group_id = parsed.path.removeprefix(f"{DUPLICATES_API_PREFIX}/groups/")
                group_id = unquote(encoded_group_id)
                if not group_id or "/" in group_id or len(group_id) > 200:
                    raise ValueError("group id is invalid")
                result = store.group_detail(
                    group_id,
                    limit=self._integer_query(query, "limit", DUPLICATES_DEFAULT_LIMIT, minimum=1, maximum=DUPLICATES_MAX_LIMIT),
                    offset=self._integer_query(query, "offset", 0, minimum=0),
                )
            self._send_json(HTTPStatus.OK, result)
        except DuplicateIndexUnavailable:
            self._send_duplicate_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "duplicate_index_unavailable",
                "Duplicate analysis index is unavailable. Build the private index before review.",
            )
        except DuplicateGroupNotFound:
            self._send_duplicate_error(HTTPStatus.NOT_FOUND, "duplicate_group_not_found", "Duplicate group was not found.")
        except ValueError as exc:
            self._send_duplicate_error(HTTPStatus.BAD_REQUEST, "invalid_query", str(exc))
        except Exception:
            self._send_duplicate_error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "Duplicate review API encountered an internal error.")

    @staticmethod
    def _reject_unknown_query(query: dict[str, list[str]], allowed: set[str]) -> None:
        unknown = sorted(set(query) - allowed)
        if unknown:
            raise ValueError(f"unknown query parameter: {unknown[0]}")

    @staticmethod
    def _single_query(query: dict[str, list[str]], name: str) -> str | None:
        values = query.get(name)
        if not values:
            return None
        if len(values) != 1:
            raise ValueError(f"{name} must be provided once")
        return values[0]

    @classmethod
    def _optional_query(cls, query: dict[str, list[str]], name: str, *, max_length: int) -> str | None:
        value = cls._single_query(query, name)
        if value is None or not value.strip():
            return None
        value = value.strip()
        if len(value) > max_length:
            raise ValueError(f"{name} is too long")
        return value

    @classmethod
    def _integer_query(
        cls,
        query: dict[str, list[str]],
        name: str,
        default: int,
        *,
        minimum: int,
        maximum: int | None = None,
    ) -> int:
        value = cls._single_query(query, name)
        if value is None:
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an integer") from exc
        if parsed < minimum or (maximum is not None and parsed > maximum):
            range_label = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
            raise ValueError(f"{name} must be {range_label}")
        return parsed

    def _require_same_origin_session(self) -> str:
        origin = self.headers.get("Origin", "")
        if origin not in self.server.allowed_origins:
            raise ReviewApiError(HTTPStatus.FORBIDDEN, "same_origin_required", "request Origin is not allowed")
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookies.get(SESSION_COOKIE)
        session_id = morsel.value if morsel else ""
        csrf = self.headers.get("X-Review-CSRF", "")
        if not session_id or not csrf or not self.server.validate_session(session_id, csrf):
            raise ReviewApiError(HTTPStatus.FORBIDDEN, "review_session_invalid", "review session or CSRF token is invalid")
        return session_id

    def _read_json_body(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
        if content_type != "application/json":
            raise ReviewApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "json_required", "Content-Type application/json is required")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ReviewApiError(HTTPStatus.BAD_REQUEST, "content_length_invalid", "Content-Length is invalid") from exc
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ReviewApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_size_invalid", "request body is empty or too large")
        try:
            value = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReviewApiError(HTTPStatus.BAD_REQUEST, "json_malformed", "request body is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ReviewApiError(HTTPStatus.BAD_REQUEST, "json_object_required", "request body must be a JSON object")
        return value

    def _send_json(self, status: int, value: dict[str, Any]) -> None:
        payload = stable_json(value, indent=None).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_error_json(self, status: int, code: str, message: str) -> None:
        self._send_json(status, {
            "schema_version": "opennana-review-api-error-1.0",
            "error": {"code": code, "message": message},
            "public_release_effect": False,
        })

    def _send_duplicate_error(self, status: int, code: str, message: str) -> None:
        self._send_json(status, {
            "schema_version": "duplicate-review-api-error-1.0",
            "error": {"code": code, "message": message},
            "read_only": True,
        })

    def _send_public_error(self, status: int, code: str, message: str) -> None:
        self._send_json(status, {
            "schema_version": "public-catalog-api-error-1.0",
            "error": {"code": code, "message": message},
            "read_only": True,
        })

    def _send_admin_error(self, status: int, code: str, message: str) -> None:
        self._send_json(status, {
            "schema_version": "image-archive-admin-api-error-1.0",
            "error": {"code": code, "message": message},
            "read_only": True,
        })


def promotion_command_from_args(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("--promotion-command-json must be a JSON array") from exc
    if not isinstance(parsed, list) or not parsed or not all(isinstance(item, str) and item for item in parsed):
        raise ValueError("--promotion-command-json must be a non-empty JSON string array")
    return parsed


def loopback_origins(port: int) -> set[str]:
    return {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        f"http://[::1]:{port}",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the image archive, public metadata API, and same-origin OpenNana review API on localhost.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--root", type=Path, default=ARCHIVE_ROOT)
    parser.add_argument("--token-ttl-seconds", type=int, default=TOKEN_TTL_SECONDS)
    parser.add_argument("--promotion-command-json", default=os.environ.get("OPENNANA_PROMOTION_COMMAND_JSON"))
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "::1"}:
        parser.error("local review server binds only to a loopback address")
    root = args.root.resolve()
    try:
        promotion_command = promotion_command_from_args(args.promotion_command_json)
    except ValueError as exc:
        parser.error(str(exc))
    if not promotion_command:
        # The production-shaped local path always closes the loop. A missing or
        # failing hook is detected during commit and rolls the decision apply
        # back instead of silently leaving approved rows unpromoted.
        promotion_command = [
            sys.executable,
            str(root / "src" / "opennana" / "build_archive_lane.py"),
            "--apply",
        ]
    service = ReviewService(
        ReviewPaths(root),
        token_ttl_seconds=args.token_ttl_seconds,
        promotion_command=promotion_command,
    )
    duplicate_store = DuplicateGroupStore(
        root / "data" / "private-research" / "duplicate-analysis" / "current" / "duplicate_index.sqlite3"
    )
    public_catalog_store = PublicCatalogStore(root)
    origins = loopback_origins(args.port)
    handler = lambda *handler_args, **handler_kwargs: ReviewRequestHandler(  # noqa: E731
        *handler_args,
        directory=root,
        **handler_kwargs,
    )
    server = ReviewHttpServer(
        (args.host, args.port),
        handler,
        service=service,
        static_root=root,
        allowed_origins=origins,
        duplicate_store=duplicate_store,
        public_catalog_store=public_catalog_store,
    )
    print(f"OpenNana review server: http://{args.host}:{args.port}/legacy/current_archive/approval-requests.html", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
