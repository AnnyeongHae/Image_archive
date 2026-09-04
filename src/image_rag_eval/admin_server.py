"""Loopback-only HTTP adapter for the private image administrator.

This is a personal local service, not an internet-facing production server.
Only explicit static resources and content-bound preview IDs are served.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

MAX_BODY_BYTES = 4 * 1024 * 1024
COOKIE = "image_archive_admin_session"
UI_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
            "/admin.css": ("admin.css", "text/css; charset=utf-8"),
            "/admin.js": ("admin.js", "text/javascript; charset=utf-8")}
SESSION_TTL = 12 * 60 * 60
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
       "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'self'")


class HttpError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class AdminHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], *, store, static_dir: Path,
                 media: dict[str, tuple[Path, str]], validate_source: Callable | None = None,
                 rights_catalog: dict | None = None, prepare_handoff: Callable | None = None,
                 prompt_catalog: dict | None = None):
        if address[0] != "127.0.0.1":
            raise ValueError("image administrator may bind only to 127.0.0.1")
        self.store = store
        self.static_dir = static_dir.resolve()
        self.media = media
        self.validate_source = validate_source
        self.rights_catalog = copy.deepcopy(rights_catalog or {})
        self.prompt_catalog = copy.deepcopy(prompt_catalog or {})
        if not set(self.prompt_catalog).issubset(media):
            raise ValueError("Prompt catalog contains images outside the frozen media allowlist")
        self.prepare_handoff = prepare_handoff
        self._handoff_lock = threading.Lock()
        self._handoffs: dict[str, dict] = {}
        self._sessions: dict[str, tuple[str, float]] = {}
        self._session_lock = threading.Lock()
        self.static = {route: ((self.static_dir / filename).read_bytes(), mime)
                       for route, (filename, mime) in UI_FILES.items()}
        super().__init__(address, AdminHandler)
        self.host_header = f"127.0.0.1:{self.server_port}"
        self.origin = f"http://{self.host_header}"

    def session(self, cookie_header: str, *, create: bool = False):
        try:
            cookies = SimpleCookie(cookie_header)
            token = cookies[COOKIE].value if COOKIE in cookies else ""
        except Exception:
            token = ""
        with self._session_lock:
            instant = time.monotonic()
            self._sessions = {key: value for key, value in self._sessions.items() if value[1] > instant}
            if token in self._sessions:
                return token, self._sessions[token][0]
            if not create:
                raise HttpError(403, "session_required", "페이지를 새로고침해 저장 연결을 다시 열어 주세요.")
            if len(self._sessions) >= 128:
                self._sessions.pop(next(iter(self._sessions)))
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self._sessions[token] = csrf, instant + SESSION_TTL
            return token, csrf

    def decorate_state(self, state: dict) -> dict:
        from .rights import normalize_image_rights
        value = copy.deepcopy(state)
        items = list(value.get("items", []))
        if isinstance(value.get("spec"), dict):
            items.extend(value["spec"].get("items", []))
        if isinstance(value.get("front_export"), dict):
            items.extend(value["front_export"].get("items", []))
        for item in items:
            item["media_url"] = "/media/" + item["id"]
            item["rights_display"] = copy.deepcopy(self.rights_catalog.get(item["id"]) or normalize_image_rights(item))
            prompt = self.prompt_catalog.get(item["id"])
            item["prompt_status"] = prompt["status"] if prompt else "unavailable"
            item["prompt_sha256"] = prompt["prompt_sha256"] if prompt else None
        commit_id = (value.get("last_commit") or {}).get("id") or value.get("commit_id")
        value["handoff"] = self.handoff_state(commit_id)
        return value

    def approved_gallery(self) -> dict:
        from .approved_library import project_approved_library
        value = self.decorate_state(self.store.gallery())
        value["library"] = project_approved_library(value, self.prompt_catalog)
        return value

    def handoff_state(self, commit_id: str | None) -> dict:
        with self._handoff_lock:
            return copy.deepcopy(self._handoffs.get(commit_id) or {
                "status": "pending" if self.prepare_handoff is not None else "not_configured",
                "provider_calls": 0, "commit_id": commit_id})

    def prepare_committed(self, state: dict, *, allow_saved_draft: bool = False):
        """Prepare local work only; a downstream failure never undoes approval.

        This is called on explicit server startup or successful stage4 POST,
        never from a GET. The callback verifies the immutable commit and is
        idempotent. Do not expose exceptions containing local paths or records.
        """
        commit = state.get("last_commit") or {}
        if (self.prepare_handoff is None or not commit.get("id")
                or (state.get("status") != "committed" and not allow_saved_draft)):
            return
        with self._handoff_lock:
            try:
                self.prepare_handoff(commit["id"])
                self._handoffs[commit["id"]] = {"status": "prepared", "provider_calls": 0, "commit_id": commit["id"]}
            except Exception as exc:
                # A class name is enough to route diagnostics without exposing
                # local paths, personal memos, request bodies, or credentials.
                self._handoffs[commit["id"]] = {"status": "preparation_failed", "provider_calls": 0,
                                                "commit_id": commit["id"], "error_type": type(exc).__name__}


class AdminHandler(BaseHTTPRequestHandler):
    server: AdminHTTPServer
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)
        self._body_consumed = False

    def _drain_rejected_body(self):
        """Drain a bounded, unambiguous body so Windows can deliver the error.

        Closing a socket with an unread request body can reset it before the
        client receives our JSON error. Never wait indefinitely or accept
        ambiguous/oversized framing just to make an error response friendlier.
        """
        if self._body_consumed or not hasattr(self, "headers"):
            return
        lengths = self.headers.get_all("Content-Length", [])
        if (self.headers.get_all("Transfer-Encoding") or len(lengths) != 1
                or not re.fullmatch(r"[0-9]{1,9}", lengths[0])):
            return
        remaining = int(lengths[0])
        if not 0 < remaining <= MAX_BODY_BYTES:
            return
        self._body_consumed = True
        previous_timeout = self.connection.gettimeout()
        deadline = time.monotonic() + 0.25
        try:
            while remaining:
                available_time = deadline - time.monotonic()
                if available_time <= 0:
                    break
                self.connection.settimeout(available_time)
                chunk = self.rfile.read1(min(remaining, 64 * 1024))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, ValueError):
            pass
        finally:
            self.connection.settimeout(previous_timeout)

    def log_message(self, *_):
        # Never log cookies, submitted memos, URLs containing tokens, or bodies.
        pass

    def _preflight(self):
        if self.client_address[0] != "127.0.0.1":
            raise HttpError(403, "loopback_only", "이 관리자는 로컬 PC에서만 접근할 수 있습니다.")
        hosts = self.headers.get_all("Host", [])
        if hosts != [self.server.host_header]:
            raise HttpError(403, "host_rejected", "서버가 안내한 127.0.0.1 주소를 사용해 주세요.")
        raw_target = self.requestline.split()[1]
        if (raw_target != self.path or not self.path.startswith("/") or self.path.startswith("//") or
                any(char in self.path for char in ("%", "\\", "?", "#")) or ".." in self.path):
            raise HttpError(400, "invalid_path", "허용되지 않는 경로입니다.")
        origins = self.headers.get_all("Origin", [])
        if origins and origins != [self.server.origin]:
            raise HttpError(403, "origin_rejected", "다른 사이트에서 관리자를 호출할 수 없습니다.")
        if self.headers.get("Sec-Fetch-Site", "") not in {"", "same-origin", "none"}:
            raise HttpError(403, "cross_site_rejected", "관리자 페이지 안에서 요청해 주세요.")
        if self.headers.get_all("Transfer-Encoding"):
            raise HttpError(400, "transfer_encoding_rejected", "청크 요청은 지원하지 않습니다.")
        if len(self.headers.get_all("Content-Length", [])) > 1:
            raise HttpError(400, "duplicate_length", "중복 Content-Length는 허용하지 않습니다.")
        if len(self.headers.get_all("Cookie", [])) > 1:
            raise HttpError(400, "duplicate_cookie", "중복 Cookie는 허용하지 않습니다.")

    def _response(self, status, content: bytes, mime="application/json; charset=utf-8", *, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def _json(self, status, value, *, headers=None):
        self._response(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"), headers=headers)

    def send_error(self, code, message=None, explain=None):
        self.close_connection = True
        self._drain_rejected_body()
        self._json(code, {"error": {"code": "unsupported_request", "message": "지원하지 않는 HTTP 요청입니다."}})

    def _run(self, fn):
        try:
            self._preflight()
            fn()
        except HttpError as exc:
            self.close_connection = True
            self._drain_rejected_body()
            self._json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception as exc:
            # Typed service validation errors are safe user-facing messages.
            self.close_connection = True
            self._drain_rejected_body()
            if hasattr(exc, "status") and hasattr(exc, "code"):
                self._json(exc.status, {"error": {"code": exc.code, "message": str(exc), "details": getattr(exc, "details", {})}})
            else:
                self._json(500, {"error": {"code": "internal_error", "message": "저장하지 못했습니다. 기존 승인 내용은 유지됩니다. 다시 시도해 주세요."}})

    def do_GET(self):
        self._run(self._get)

    def _get(self):
        if self.path in self.server.static:
            blob, mime = self.server.static[self.path]
            self._response(200, blob, mime)
            return
        if self.path == "/health":
            self._json(200, {"status": "ok", "mode": "loopback_private", "provider_calls": 0})
            return
        if self.path == "/api/admin/session":
            token, csrf = self.server.session(self.headers.get("Cookie", ""), create=True)
            self._json(200, {"csrf_token": csrf}, headers={"Set-Cookie": f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/"})
            return
        self.server.session(self.headers.get("Cookie", ""))
        if self.path == "/api/admin/state":
            self._json(200, self.server.decorate_state(self.server.store.state()))
        elif self.path == "/api/admin/gallery":
            self._json(200, self.server.approved_gallery())
        elif self.path == "/api/admin/handoff":
            commit = self.server.store.state().get("last_commit") or {}
            self._json(200, self.server.handoff_state(commit.get("id")))
        elif self.path.startswith("/api/admin/prompt/"):
            from .approved_library import missing_prompt
            ident = self.path[len("/api/admin/prompt/"):]
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", ident) or ident not in self.server.media:
                raise HttpError(404, "prompt_not_found", "등록된 원본 프롬프트가 아닙니다.")
            self._json(200, self.server.prompt_catalog.get(ident, missing_prompt(ident)))
        elif self.path.startswith("/media/"):
            ident = self.path[len("/media/"):]
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", ident) or ident not in self.server.media:
                raise HttpError(404, "image_not_found", "등록된 미리보기가 아닙니다.")
            path, expected = self.server.media[ident]
            blob = path.read_bytes()
            if hashlib.sha256(blob).hexdigest() != expected:
                raise HttpError(409, "image_changed", "미리보기 근거 파일이 바뀌었습니다. 검토를 중단해 주세요.")
            self._response(200, blob, "image/png", headers={"ETag": f'"{expected}"'})
        else:
            raise HttpError(404, "not_found", "없는 주소입니다.")

    def do_PUT(self):
        self._run(lambda: self._mutate("PUT"))

    def do_POST(self):
        self._run(lambda: self._mutate("POST"))

    def _method_rejected(self):
        raise HttpError(405, "method_not_allowed", "지원하지 않는 HTTP 메서드입니다.")

    def do_DELETE(self):
        self._run(self._method_rejected)

    do_PATCH = do_OPTIONS = do_HEAD = do_DELETE

    def _body(self):
        if self.headers.get_all("Origin", []) != [self.server.origin]:
            raise HttpError(403, "same_origin_required", "저장 요청은 같은 관리자 페이지에서만 가능합니다.")
        _, csrf = self.server.session(self.headers.get("Cookie", ""))
        supplied = self.headers.get_all("X-Admin-CSRF", [])
        if len(supplied) != 1 or not hmac.compare_digest(supplied[0].encode("utf-8"), csrf.encode("ascii")):
            raise HttpError(403, "csrf_rejected", "저장 연결이 만료되었습니다. 새로고침해 주세요.")
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            raise HttpError(415, "json_required", "JSON 요청만 허용합니다.")
        raw_length = self.headers.get("Content-Length", "")
        if not re.fullmatch(r"[0-9]{1,9}", raw_length):
            raise HttpError(400, "invalid_length", "요청 크기가 올바르지 않습니다.")
        length = int(raw_length)
        if not 0 < length <= MAX_BODY_BYTES:
            raise HttpError(413, "body_too_large", "저장 요청이 너무 큽니다.")
        raw = self.rfile.read(length)
        self._body_consumed = True
        if len(raw) != length:
            raise HttpError(400, "truncated_body", "저장 요청이 끝까지 도착하지 않았습니다.")
        def pairs(rows):
            out = {}
            for key, value in rows:
                if key in out:
                    raise ValueError("duplicate JSON key")
                out[key] = value
            return out
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise HttpError(400, "malformed_json", "올바른 JSON 객체가 아닙니다.")
        if not isinstance(value, dict):
            raise HttpError(400, "object_required", "JSON 객체가 필요합니다.")
        return value

    def _mutate(self, method):
        routes = {("PUT", "/api/admin/draft"): "save_draft", ("POST", "/api/admin/advance"): "advance",
                  ("POST", "/api/admin/rewind"): "rewind"}
        action = routes.get((method, self.path))
        if action is None:
            raise HttpError(404, "not_found", "없는 저장 주소입니다.")
        body = self._body()
        if action == "advance" and self.server.validate_source is not None:
            try:
                self.server.validate_source()
            except (ValueError, OSError):
                raise HttpError(409, "source_changed", "고정된 이미지·승인 근거가 달라졌습니다. 저장하지 않았습니다.")
        state = getattr(self.server.store, action)(body)
        if action == "advance":
            self.server.prepare_committed(state)
        self._json(200, self.server.decorate_state(state))


def media_map(root: Path, run_id: str, spec: dict):
    from .experiment import run_path
    directory = run_path(root, run_id)
    media = {}
    for row in spec["items"]:
        path = (directory / "group-workflow-v1" / row["prepared_path"]).resolve()
        if not path.is_relative_to((directory / "inputs").resolve()):
            raise ValueError("preview path escapes frozen input directory")
        media[row["id"]] = path, row["prepared_sha256"]
    return media
