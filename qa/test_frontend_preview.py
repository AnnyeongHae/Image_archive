"""Loopback HTTP tests; synthetic fixtures never use private archive content."""
from __future__ import annotations

import hashlib
import copy
import http.client
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("frontend_preview", ROOT / "platform/v2/local/frontend_preview.py")
preview = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preview)


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bundle = Path(self.tmp.name)
        counts = {"images": 0, "groups": 0, "variants": 0, "excluded": 0}
        self.files = {
            "index.html": b"<!doctype html><h1>Test gallery</h1>",
            "data/catalog.json": json.dumps({"schema_version": "image-gallery-2", "mode": "private_local_preview", "groups": [], "counts": counts}).encode(),
        }
        served = {name: hashlib.sha256(data).hexdigest() for name, data in self.files.items()}
        identity = {"schema_version": "image-gallery-build-2", "mode": "private_local_preview", "served_files": copy.deepcopy(served)}
        self.receipt = {"schema_version": "image-gallery-build-2", "mode": "private_local_preview", "status": "ready", "counts": counts,
                        "identity": identity, "build_id": preview.identity_sha(identity), "served_files": served}
        self.bundle = self.bundle / self.receipt["build_id"]
        (self.bundle / "data").mkdir(parents=True)
        for name, data in self.files.items():
            (self.bundle / name).write_bytes(data)
        self.write_receipt()
        self.server = None

    def write_receipt(self):
        (self.bundle / "build-receipt.json").write_text(json.dumps(self.receipt), encoding="utf-8")

    def tearDown(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=3)
        self.tmp.cleanup()

    def start(self):
        self.server = preview.create_server(self.bundle)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def request(self, path="/", *, method="GET", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_only_loopback_and_manifest_files_served(self):
        self.start()
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        code, headers, body = self.request()
        self.assertEqual((code, body), (200, self.files["index.html"]))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.request("/data/catalog.json")[0], 200)

    def test_private_and_traversal_routes_not_served(self):
        self.start()
        for target in ("/build-receipt.json", "/.env", "/data/", "/data/../build-receipt.json", "/%2e%2e/.env", "/%252e%252e/.env", "/api/admin/v2/status", "/gallery.js.map"):
            with self.subTest(target=target):
                self.assertEqual(self.request(target)[0], 404)

    def test_dns_rebinding_and_cross_origin_denied(self):
        self.start()
        for headers in ({"Host": "evil.example"}, {"Origin": "https://evil.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(headers=headers)[0], 403)
        port = self.server.server_port
        self.assertEqual(self.request(headers={"Origin": f"http://127.0.0.1:{port}", "Sec-Fetch-Site": "same-origin"})[0], 200)

    def test_mutations_are_not_supported(self):
        self.start()
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            self.assertEqual(self.request(method=method)[0], 405)

    def test_head_has_no_body(self):
        self.start()
        status, headers, body = self.request(method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(int(headers["Content-Length"]), len(self.files["index.html"]))

    def test_hash_drift_rejected_at_start(self):
        (self.bundle / "index.html").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(preview.PreviewError, "resource_hash_mismatch"):
            preview.make_handler(self.bundle)

    def test_hash_drift_rejected_after_start(self):
        self.start()
        (self.bundle / "index.html").write_text("changed", encoding="utf-8")
        self.assertEqual(self.request()[0], 409)

    def test_manifest_cannot_allow_private_or_arbitrary_paths(self):
        for name in (".env", "../index.html", "data/raw.json", "build-receipt.json", "C:/secret.txt", "media/../secret.png", "data/%2e%2e/secret.json"):
            with self.subTest(name=name):
                self.assertFalse(preview.safe_resource_name(name))
        self.receipt["served_files"][".env"] = "0" * 64
        self.write_receipt()
        with self.assertRaisesRegex(preview.PreviewError, "invalid_served_resource"):
            preview.make_handler(self.bundle)

    def test_unrecognized_catalog_mode_denied(self):
        data = json.dumps({"schema_version": "image-gallery-2", "mode": "all_private"}).encode()
        (self.bundle / "data/catalog.json").write_bytes(data)
        self.receipt["served_files"]["data/catalog.json"] = hashlib.sha256(data).hexdigest()
        self.write_receipt()
        with self.assertRaisesRegex(preview.PreviewError, "invalid_gallery_mode"):
            preview.make_handler(self.bundle)

    def test_edited_file_and_top_hash_do_not_bypass_build_identity(self):
        (self.bundle / "index.html").write_text("altered", encoding="utf-8")
        self.receipt["served_files"]["index.html"] = hashlib.sha256(b"altered").hexdigest()
        self.write_receipt()
        with self.assertRaisesRegex(preview.PreviewError, "invalid_build_identity"):
            preview.make_handler(self.bundle)

    def test_wrong_build_id_is_rejected(self):
        self.receipt["build_id"] = "0" * 64
        self.write_receipt()
        with self.assertRaisesRegex(preview.PreviewError, "invalid_build_identity"):
            preview.make_handler(self.bundle)

    def test_receipt_scope_cannot_override_catalog(self):
        self.receipt["mode"] = "public"
        self.write_receipt()
        with self.assertRaisesRegex(preview.PreviewError, "receipt_catalog_scope_mismatch"):
            preview.make_handler(self.bundle)

    def test_wrong_counts_are_rejected(self):
        self.receipt["counts"] = {"images": 9000}
        self.write_receipt()
        with self.assertRaisesRegex(preview.PreviewError, "receipt_catalog_scope_mismatch"):
            preview.make_handler(self.bundle)

    def test_public_empty_catalog_cannot_hide_detail_resources(self):
        catalog = json.loads((self.bundle / "data/catalog.json").read_bytes())
        catalog["mode"] = "public"
        data = json.dumps(catalog).encode()
        (self.bundle / "data/catalog.json").write_bytes(data)
        (self.bundle / "data/groups").mkdir()
        hidden_name = "data/groups/" + "1" * 64 + ".json"
        (self.bundle / hidden_name).write_bytes(b"{}")
        self.receipt["served_files"].update({"data/catalog.json": hashlib.sha256(data).hexdigest(), hidden_name: hashlib.sha256(b"{}").hexdigest()})
        self.receipt.update(mode="public", status="blocked")
        self.receipt["identity"].update(mode="public", served_files=copy.deepcopy(self.receipt["served_files"]))
        self.receipt["build_id"] = preview.identity_sha(self.receipt["identity"])
        target = self.bundle.parent / self.receipt["build_id"]
        self.bundle.rename(target)
        self.bundle = target
        self.write_receipt()
        with self.assertRaisesRegex(preview.PreviewError, "blocked_public_bundle_has_private_resources"):
            preview.make_handler(self.bundle)


if __name__ == "__main__":
    unittest.main()
