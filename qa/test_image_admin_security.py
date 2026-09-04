"""Real loopback HTTP security tests with fake state; never an operational DB."""
from __future__ import annotations

import copy
import hashlib
import http.client
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from image_rag_eval import admin_server as module


class FakeStore:
    def __init__(self):
        self.calls = []
        self.error = None
        self.accepted = {"items": [{"id": "image-one"}], "release_eligible": False, "commit_id": "last-confirmed"}

    def state(self):
        if self.error:
            raise self.error
        return {"run_id": "security-fixture", "revision": len(self.calls), "active_stage": 1,
                "decisions": {}, "spec": {"items": [{"id": "image-one"}]}}

    def gallery(self):
        return copy.deepcopy(self.accepted)

    def save_draft(self, value):
        if self.error:
            raise self.error
        self.calls.append(("draft", copy.deepcopy(value)))
        return self.state()

    def advance(self, value):
        if self.error:
            raise self.error
        self.calls.append(("advance", copy.deepcopy(value)))
        return self.state()

    def rewind(self, value):
        if self.error:
            raise self.error
        self.calls.append(("rewind", copy.deepcopy(value)))
        return self.state()


class AdminSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        for filename in ("index.html", "admin.css", "admin.js"):
            (self.directory / filename).write_text("fixture " + filename, encoding="utf-8")
        (self.directory / ".env").write_text("FAKE_SECRET_DO_NOT_SERVE", encoding="utf-8")
        self.image = self.directory / "fixture.png"
        self.image.write_bytes(b"\x89PNG\r\n\x1a\nprivate fixture image")
        self.image_sha = hashlib.sha256(self.image.read_bytes()).hexdigest()
        self.store = FakeStore()
        self.server = module.AdminHTTPServer(("127.0.0.1", 0), store=self.store, static_dir=self.directory,
            media={"image-one": (self.image, self.image_sha)})
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.01), daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.cookie = self.csrf = None

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method="GET", path="/api/admin/state", body=None, headers=None):
        headers = dict(headers or {})
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def session(self):
        status, headers, raw = self.request(path="/api/admin/session")
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = json.loads(raw)["csrf_token"]
        return headers

    def authenticated(self, **overrides):
        if self.cookie is None:
            self.session()
        headers = {"Cookie": self.cookie, "Origin": self.server.origin, "X-Admin-CSRF": self.csrf,
                   "Content-Type": "application/json"}
        headers.update(overrides)
        return {key: value for key, value in headers.items() if value is not None}

    def raw_request(self, method, path, headers, body=b""):
        wire = f"{method} {path} HTTP/1.1\r\n".encode("ascii")
        wire += b"".join(f"{key}: {value}\r\n".encode("latin-1") for key, value in headers)
        wire += b"\r\n" + body
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3) as connection:
            connection.sendall(wire)
            connection.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(connection)
            response.begin()
            return response.status, dict(response.getheaders()), response.read()

    def test_only_explicit_loopback_bind_allowed(self):
        for address in ("0.0.0.0", "localhost", "::1", "192.168.1.1"):
            with self.subTest(address=address), self.assertRaisesRegex(ValueError, "127.0.0.1"):
                module.AdminHTTPServer((address, 0), store=self.store, static_dir=self.directory, media={})

    def test_session_security_headers_cookie_and_no_token_elsewhere(self):
        headers = self.session()
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertIn("Path=/", headers["Set-Cookie"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        status, _, body = self.request(headers=self.authenticated())
        self.assertEqual(status, 200)
        self.assertNotIn(self.csrf.encode(), body)
        self.assertNotIn(b"csrf_token", body)

    def test_session_required_for_state_gallery_and_media(self):
        for path in ("/api/admin/state", "/api/admin/gallery", "/api/admin/handoff", "/api/admin/prompt/image-one", "/media/image-one"):
            with self.subTest(path=path):
                status, _, body = self.request(path=path)
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(body)["error"]["code"], "session_required")
        for path in ("/", "/admin.css", "/admin.js", "/health"):
            self.assertEqual(self.request(path=path)[0], 200)
        self.assertEqual(self.store.calls, [])

    def test_original_prompt_route_preserves_raw_unicode_and_is_read_only(self):
        raw_prompt = '  {\n  "스타일": "스티커 🧩"\n}\n'
        record = {"schema_version": "image-original-prompt-1", "id": "image-one", "status": "available",
                  "full_prompt": raw_prompt, "prompt_sha256": hashlib.sha256(raw_prompt.encode()).hexdigest(),
                  "source_binding": {"run_id": "security-fixture", "spec_sha256": "bound-spec"}, "release_eligible": False}
        self.server.prompt_catalog["image-one"] = record
        status, headers, raw = self.request(path="/api/admin/prompt/image-one", headers=self.authenticated())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), record)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.store.calls, [])

    def test_prompt_route_does_not_read_arbitrary_files_or_fallback_to_embedding(self):
        headers = self.authenticated()
        for ident in ("unknown", "..", "%2e%2e", "image-one/.env", "image-one?path=.env"):
            status, _, raw = self.request(path="/api/admin/prompt/" + ident, headers=headers)
            self.assertIn(status, (400, 404))
            self.assertNotIn(b"FAKE_SECRET_DO_NOT_SERVE", raw)
        status, _, raw = self.request(path="/api/admin/prompt/image-one", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["status"], "unavailable")
        self.assertIsNone(json.loads(raw)["full_prompt"])

    def test_gallery_projects_confirmed_groups_without_mutating_membership(self):
        self.store.accepted.update({"run_id": "security-fixture", "revision": 8,
            "items": [{"id": "image-one"}, {"id": "image-two"}, {"id": "image-three"}],
            "retained_ids": ["image-one", "image-two", "image-three", "hidden-image"],
            "groups": [{"candidate_id": "approved-group", "member_ids": ["hidden-image", "image-one", "image-two"],
                        "suggested_representative_id": "hidden-image"}]})
        before = copy.deepcopy(self.store.accepted)
        status, _, raw = self.request(path="/api/admin/gallery", headers=self.authenticated())
        self.assertEqual(status, 200)
        response = json.loads(raw)
        self.assertEqual(response["groups"], before["groups"])
        group = response["library"]["display_groups"][0]
        self.assertEqual(group["member_ids"], ["image-one", "image-two"])
        self.assertEqual(group["representative_id"], "image-one")
        self.assertEqual(response["library"]["ungrouped_ids"], ["image-three"])
        self.assertEqual(response["library"]["source_commit_id"], "last-confirmed")
        self.assertEqual(self.store.accepted, before)
        self.assertEqual(self.store.calls, [])

    def test_rights_overlay_is_on_state_gallery_and_export_without_mutating_store(self):
        from image_rag_eval.rights import normalize_image_rights
        notice = normalize_image_rights({"id": "image-one"})
        self.server.rights_catalog = {"image-one": notice}
        self.store.accepted["front_export"] = {"items": [{"id": "image-one"}]}
        before = copy.deepcopy(self.store.accepted)
        state = json.loads(self.request(headers=self.authenticated())[2])
        self.assertEqual(state["spec"]["items"][0]["rights_display"], notice)
        result = json.loads(self.request(path="/api/admin/gallery", headers=self.authenticated())[2])
        self.assertEqual(result["items"][0]["rights_display"], notice)
        self.assertEqual(result["front_export"]["items"][0]["rights_display"], notice)
        self.assertFalse(notice["release_eligible"])
        self.assertFalse(notice["image_license_verified"])
        self.assertEqual(self.store.accepted, before)

    def test_handoff_get_never_prepares_or_writes(self):
        calls = []
        self.server.prepare_handoff = calls.append
        for path in ("/api/admin/handoff", "/api/admin/state", "/api/admin/gallery"):
            self.assertEqual(self.request(path=path, headers=self.authenticated())[0], 200)
        self.assertEqual(calls, [])
        self.assertEqual(self.store.calls, [])

    def test_committed_handoff_failure_does_not_turn_approval_into_http_failure(self):
        original_state = self.store.state
        self.store.state = lambda: {**original_state(), "status": "committed", "last_commit": {"id": "fixture-commit"}}
        def failed(commit):
            raise RuntimeError("sensitive local exception must not leak")
        self.server.prepare_handoff = failed
        status, _, raw = self.request("POST", "/api/admin/advance", {}, self.authenticated())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["status"], "committed")
        self.assertEqual(json.loads(raw)["handoff"]["status"], "preparation_failed")
        self.assertNotIn(b"sensitive", raw)
        self.assertEqual(len(self.store.calls), 1)
        prepared = []
        self.server.prepare_handoff = prepared.append
        self.server.prepare_committed(self.store.state())
        self.assertEqual(prepared, ["fixture-commit"])
        result = json.loads(self.request(path="/api/admin/handoff", headers=self.authenticated())[2])
        self.assertEqual(result, {"status": "prepared", "provider_calls": 0, "commit_id": "fixture-commit"})

    def test_handoff_status_is_commit_bound_and_startup_can_use_last_saved_commit(self):
        calls = []
        def prepare(commit):
            calls.append(commit)
            if commit == "older-A":
                raise ValueError("stale commit")
        self.server.prepare_handoff = prepare
        current = {"status": "committed", "last_commit": {"id": "newer-B"}}
        self.server.prepare_committed(current)
        self.server.prepare_committed({"status": "committed", "last_commit": {"id": "older-A"}})
        self.assertEqual(self.server.decorate_state(current)["handoff"]["status"], "prepared")
        self.assertEqual(self.server.handoff_state("older-A")["status"], "preparation_failed")
        self.assertEqual(self.server.handoff_state("not-yet-prepared")["status"], "pending")
        saved = {"status": "saved", "last_commit": {"id": "saved-last-commit"}}
        self.server.prepare_committed(saved)
        self.assertEqual(calls, ["newer-B", "older-A"])
        self.server.prepare_committed(saved, allow_saved_draft=True)
        self.assertEqual(calls[-1], "saved-last-commit")

    def test_host_origin_fetch_site_and_duplicate_headers_rejected(self):
        for header in ({"Host": "localhost:" + str(self.server.server_port)}, {"Host": "attacker.example"},
                       {"Origin": "null"}, {"Origin": "https://attacker.example"}, {"Sec-Fetch-Site": "cross-site"},
                       {"Sec-Fetch-Site": "same-site"}):
            with self.subTest(header=header):
                self.assertEqual(self.request(path="/api/admin/session", headers=header)[0], 403)
        cases = [([("Host", self.server.host_header), ("Host", self.server.host_header)], 403),
                 ([("Host", self.server.host_header), ("Origin", self.server.origin), ("Origin", self.server.origin)], 403),
                 ([("Host", self.server.host_header), ("Cookie", "a=1"), ("Cookie", "b=2")], 400)]
        for headers, expected in cases:
            with self.subTest(headers=headers):
                self.assertEqual(self.raw_request("GET", "/api/admin/session", headers)[0], expected)

    def test_mutation_requires_exact_origin_session_and_csrf(self):
        good = self.authenticated()
        for changes in ({"Origin": None}, {"Origin": "null"}, {"Cookie": None}, {"Cookie": "unrelated=1"},
                        {"X-Admin-CSRF": None}, {"X-Admin-CSRF": "wrong"}, {"X-Admin-CSRF": "\u00ff"}):
            with self.subTest(changes=changes):
                status, _, _ = self.request("PUT", "/api/admin/draft", {}, self.authenticated(**changes))
                self.assertEqual(status, 403)
        headers = [("Host", self.server.host_header), *good.items(), ("X-Admin-CSRF", self.csrf), ("Content-Length", "2")]
        self.assertEqual(self.raw_request("PUT", "/api/admin/draft", headers, b"{}")[0], 403)
        self.assertEqual(self.store.calls, [])

    def test_session_csrf_cannot_be_reused_with_another_session(self):
        self.session()
        cookie, csrf = self.cookie, self.csrf
        self.cookie = self.csrf = None
        self.session()
        self.assertNotEqual(cookie, self.cookie)
        headers = self.authenticated(**{"X-Admin-CSRF": csrf})
        self.assertEqual(self.request("PUT", "/api/admin/draft", {}, headers)[0], 403)

    def test_denied_small_body_reliably_returns_403_without_connection_resets(self):
        # This is a repeated independent assertion, not a retry. Before the
        # bounded denied-body drain, Windows observed 9 resets / 250 requests.
        headers = self.authenticated(Cookie=None)
        for index in range(150):
            with self.subTest(request=index):
                status, _, body = self.request("PUT", "/api/admin/draft", {}, headers)
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(body)["error"]["code"], "session_required")
        self.assertEqual(self.store.calls, [])

    def test_rejected_body_that_never_arrives_does_not_block_error_response(self):
        headers = self.authenticated(Cookie=None)
        headers["Content-Length"] = "1024"
        started = time.perf_counter()
        status, _, body = self.request("PUT", "/api/admin/draft", b"", headers)
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "session_required")
        # Generous scheduling margin around the 0.25-second total drain limit;
        # a missing body must not consume the ordinary 15-second socket timeout.
        self.assertLess(time.perf_counter() - started, 1.5)
        self.assertEqual(self.store.calls, [])

    def test_expired_session_and_oversized_session_pool_bounded(self):
        self.session()
        with patch.object(module.time, "monotonic", return_value=module.time.monotonic() + module.SESSION_TTL + 1):
            self.assertEqual(self.request(headers=self.authenticated())[0], 403)
        for _ in range(130):
            self.server.session("", create=True)
        self.assertLessEqual(len(self.server._sessions), 128)

    def test_malformed_and_nonfinite_json_never_reaches_store(self):
        for body in (b"{", b"[]", b"null", b'{"a":1,"a":2}', b'{"nested":{"a":1,"a":2}}',
                     b'{"a":NaN}', b'{"a":Infinity}', b'{"a":-Infinity}', b'\xff'):
            with self.subTest(body=body):
                status, _, _ = self.request("PUT", "/api/admin/draft", body, self.authenticated())
                self.assertEqual(status, 400)
        self.assertEqual(self.store.calls, [])

    def test_body_size_framing_and_type_rejected_before_dispatch(self):
        auth = self.authenticated()
        base = [("Host", self.server.host_header), *auth.items()]
        cases = [([("Content-Length", str(module.MAX_BODY_BYTES + 1))], b"", 413),
                 ([("Content-Length", "0")], b"", 413),
                 ([("Content-Length", "-1")], b"", 400),
                 ([("Content-Length", "+2")], b"{}", 400),
                 ([], b"{}", 400),
                 ([("Content-Length", "2"), ("Content-Length", "2")], b"{}", 400),
                 ([("Transfer-Encoding", "chunked")], b"2\r\n{}\r\n0\r\n\r\n", 400),
                 ([("Transfer-Encoding", "chunked"), ("Content-Length", "2")], b"{}", 400),
                 ([("Content-Length", "10")], b"{}", 400)]
        for extra, body, expected in cases:
            with self.subTest(extra=extra):
                self.assertEqual(self.raw_request("PUT", "/api/admin/draft", base + extra, body)[0], expected)
        status, _, _ = self.request("PUT", "/api/admin/draft", b"{}", self.authenticated(**{"Content-Type": "text/plain"}))
        self.assertEqual(status, 415)
        self.assertEqual(self.store.calls, [])

    def test_traversal_secret_arbitrary_file_and_unknown_media_not_served(self):
        for path in ("/.env", "/fixture.png", "/index.html", "/src/serve_image_admin.py", "/media/.env",
                     "/media/missing", "/../.env", "/%2eenv", "/media/%252e%252e/.env", "/media/C:\\Windows",
                     "/api/admin/state?file=.env", "http://attacker.example/api/admin/state"):
            with self.subTest(path=path):
                status, _, body = self.request(path=path, headers=self.authenticated())
                self.assertIn(status, (400, 403, 404))
                self.assertNotIn(b"FAKE_SECRET_DO_NOT_SERVE", body)
                self.assertNotIn(str(self.directory).encode(), body)

    def test_noncanonical_raw_targets_rejected_before_stdlib_normalization(self):
        headers = [("Host", self.server.host_header), *self.authenticated().items()]
        for target in ("//api/admin/state", "///api/admin/state", "http://attacker.example/api/admin/state"):
            with self.subTest(target=target):
                status, _, body = self.raw_request("GET", target, headers)
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["error"]["code"], "invalid_path")

    def test_unsupported_methods_are_sanitized_and_common_methods_preflight_host(self):
        for method in ("DELETE", "PATCH", "OPTIONS", "HEAD"):
            with self.subTest(method=method):
                status, headers, body = self.request(method, "/api/admin/draft", headers=self.authenticated())
                self.assertEqual(status, 405)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
                if method == "HEAD":
                    self.assertEqual(body, b"")
                else:
                    self.assertEqual(json.loads(body)["error"]["code"], "method_not_allowed")
                self.assertEqual(self.request(method, "/api/admin/draft", headers={"Host": "attacker.example"})[0], 403)
        status, headers, body = self.request("FAKE_SECRET_METHOD", "/api/admin/draft")
        self.assertEqual(status, 501)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn(b"FAKE_SECRET_METHOD", body)
        self.assertNotIn(b"<html", body)
        self.assertEqual(json.loads(body)["error"]["code"], "unsupported_request")
        self.assertEqual(self.store.calls, [])

    def test_media_allowlist_content_hash_and_urls(self):
        status, headers, body = self.request(path="/media/image-one", headers=self.authenticated())
        self.assertEqual(status, 200)
        self.assertEqual(body, self.image.read_bytes())
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(headers["ETag"], '"' + self.image_sha + '"')
        _, _, state = self.request(headers=self.authenticated())
        self.assertEqual(json.loads(state)["spec"]["items"][0]["media_url"], "/media/image-one")
        self.image.write_bytes(b"changed preview")
        status, _, body = self.request(path="/media/image-one", headers=self.authenticated())
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"]["code"], "image_changed")

    def test_only_exact_mutation_routes_dispatch_and_get_never_mutates(self):
        body = {"run_id": "security-fixture", "expected_revision": 0, "request_id": "fixture-id", "decisions": {"memo": "안녕"}}
        for method, path, action in (("PUT", "/api/admin/draft", "draft"), ("POST", "/api/admin/advance", "advance"),
                                     ("POST", "/api/admin/rewind", "rewind")):
            self.assertEqual(self.request(method, path, body, self.authenticated())[0], 200)
            self.assertEqual(self.store.calls[-1], (action, body))
        count = len(self.store.calls)
        for method, path in (("GET", "/api/admin/advance"), ("GET", "/api/admin/draft"),
                             ("POST", "/api/admin/draft"), ("PUT", "/api/admin/advance"), ("POST", "/unknown")):
            self.assertEqual(self.request(method, path, body if method != "GET" else None, self.authenticated())[0], 404)
        for method in ("DELETE", "PATCH", "OPTIONS", "TRACE", "HEAD"):
            self.assertIn(self.request(method, "/api/admin/draft", None, self.authenticated())[0], (405, 501))
        self.assertEqual(len(self.store.calls), count)

    def test_generic_exceptions_redacted_and_gallery_preserved(self):
        accepted = copy.deepcopy(self.store.accepted)
        self.store.error = RuntimeError("FAKE_SECRET_INTERNAL_SQL_PATH")
        for method, path, body in (("GET", "/api/admin/state", None), ("PUT", "/api/admin/draft", {}),
                                  ("POST", "/api/admin/advance", {}), ("POST", "/api/admin/rewind", {})):
            with self.subTest(method=method, path=path):
                status, headers, response = self.request(method, path, body, self.authenticated())
                self.assertEqual(status, 500)
                self.assertEqual(json.loads(response)["error"]["code"], "internal_error")
                self.assertNotIn(b"FAKE_SECRET_INTERNAL_SQL_PATH", response)
                self.assertNotIn(b"Traceback", response)
                self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.store.calls, [])
        self.assertEqual(self.store.accepted, accepted)
        self.assertEqual(self.request(path="/api/admin/gallery", headers=self.authenticated())[0], 200)

    def test_source_validation_failure_blocks_advance_not_last_gallery(self):
        def fail():
            raise ValueError("FAKE_SECRET_SOURCE_PATH")
        self.server.validate_source = fail
        status, _, response = self.request("POST", "/api/admin/advance", {}, self.authenticated())
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(response)["error"]["code"], "source_changed")
        self.assertNotIn(b"FAKE_SECRET_SOURCE_PATH", response)
        self.assertEqual(self.store.calls, [])
        self.assertEqual(self.request(path="/api/admin/gallery", headers=self.authenticated())[0], 200)


if __name__ == "__main__":
    unittest.main()
