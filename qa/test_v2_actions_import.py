from __future__ import annotations

import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("archive_v2_actions_import", ROOT / "platform/v2/local/actions_import.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)
sys.path.insert(0, str(ROOT / "src"))
from github_sources.intake_envelope import parse_gallery


def zip_bytes(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return output.getvalue()


class FakeGh:
    def __init__(self, downloaded):
        self.downloaded, self.calls, self.auth_calls = downloaded, [], 0
        self.run = {"id": 123, "run_attempt": 2, "workflow_id": 9, "path": module.WORKFLOW,
            "repository": {"id": 7, "full_name": module.REPOSITORY},
            "head_repository": {"id": 7, "full_name": module.REPOSITORY}, "head_branch": "main",
            "head_sha": "a" * 40, "event": "workflow_dispatch", "status": "completed", "conclusion": "success",
            "run_started_at": "2026-09-04T01:00:00Z", "updated_at": "2026-09-04T01:02:00Z"}
        self.artifact = {"id": 99, "name": module.ARTIFACT_NAME, "expired": False,
            "expires_at": "2099-09-04T00:00:00Z", "created_at": "2026-09-04T01:01:00Z",
            "size_in_bytes": len(downloaded), "digest": "sha256:" + module.digest(downloaded),
            "workflow_run": {"id": 123, "head_sha": "a" * 40, "head_branch": "main", "repository_id": 7, "head_repository_id": 7}}
        self.listing = None

    def authenticate(self):
        self.auth_calls += 1

    def api(self, endpoint):
        self.calls.append(endpoint)
        prefix = f"repos/{module.REPOSITORY}/actions/"
        if endpoint in (prefix + "runs/123", prefix + "runs/123/attempts/2"):
            return copy.deepcopy(self.run)
        if endpoint == prefix + "workflows/9":
            return {"id": 9, "path": module.WORKFLOW}
        if endpoint == prefix + "runs/123/artifacts?per_page=100&page=1":
            return {"artifacts": copy.deepcopy(self.listing if self.listing is not None else [self.artifact])}
        if endpoint == prefix + "artifacts/99":
            return copy.deepcopy(self.artifact)
        raise AssertionError("unexpected endpoint: " + endpoint)

    def download(self, artifact_id):
        self.calls.append("download:" + artifact_id)
        return self.downloaded


class ActionsImportTests(unittest.TestCase):
    def setUp(self):
        raw = b'<a name="case-1"></a>\n### Literal example\n```text\nPrivate exact prompt.\n```\n'
        parsed = parse_gallery(raw, source={"source_id": "fixture", "repository": "freestylefly/awesome-gpt-image-2"},
            path="docs/gallery-part-1.md", commit="b" * 40, tree_sha="c" * 40,
            blob_sha=hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest(), media_tree={},
            observed_at="2026-09-04T01:01:00Z")
        self.bundle = {"schema_version": "archive-sealed-intake-bundle-1", "run_id": "untrusted-payload-timestamp",
            "records": parsed["records"], "containers": [{"source_id": "fixture", "path": "docs/gallery-part-1.md",
                "raw_utf8": raw.decode(), "sha256": module.digest(raw), "parse_complete": True, "deferred": []}],
            "canonical_promotion": False, "public_release": False, "image_binaries_downloaded": 0}
        self.bundle_bytes = module.encode(self.bundle)
        self.sealed = {"schema_version": "archive-sealed-intake-1", "algorithm": "RSA-OAEP-256+A256GCM",
            "recipient_key_sha256": "1" * 64, "plaintext_sha256": module.digest(self.bundle_bytes),
            "plaintext_bytes": len(self.bundle_bytes), "iv": "aXY", "wrapped_key": "a2V5",
            "ciphertext": "Y2lwaGVydGV4dA", "ciphertext_sha256": "2" * 64}
        self.zip = zip_bytes([("intake.sealed.json", module.encode(self.sealed))])

    def prepare_root(self, directory):
        root = Path(directory)
        key = root / module.PRIVATE_KEY
        key.parent.mkdir(parents=True)
        key.write_text("mock private key, never used by crypto", encoding="utf-8")
        return root

    def unseal(self, source, output, key):
        self.assertTrue(source.is_file())
        self.assertTrue(key.is_file())
        with output.open("xb") as stream:
            stream.write(self.bundle_bytes)
        return {"ok": True}

    def test_default_dry_run_never_authenticates_or_writes(self):
        with patch.object(module, "GhClient", side_effect=AssertionError("auth/network forbidden")):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(module.main(["--run-id", "123"]), 0)
            self.assertEqual(json.loads(output.getvalue())["writes"], 0)
            for flags in (["--fetch"], ["--apply"]):
                with self.assertRaises(module.ActionsImportError):
                    module.main(["--run-id", "123", *flags])

    def test_origin_is_server_metadata_not_payload_claimed_run_id(self):
        client = FakeGh(self.zip)
        receipt = module.verify_lineage(client, "123", attempt="2", expected_head_sha="a" * 40)
        self.assertTrue(receipt["origin_verified"])
        self.assertEqual(receipt["run_id"], "123")
        self.assertFalse(receipt["server_artifact_digest"]["verified"])

    def test_wrong_repo_branch_event_status_or_head_repo_is_rejected(self):
        mutations = [("head_branch", "untrusted"), ("event", "pull_request"), ("status", "in_progress"),
                     ("conclusion", "failure"), ("path", ".github/workflows/other.yml"),
                     ("repository", {"id": 7, "full_name": "attacker/repo"}),
                     ("head_repository", {"id": 8, "full_name": "attacker/fork"})]
        for field, value in mutations:
            client = FakeGh(self.zip)
            client.run[field] = value
            with self.subTest(field=field), self.assertRaises(module.ActionsImportError):
                module.verify_lineage(client, "123")

    def test_attempt_head_sha_and_old_attempt_artifact_fail_closed(self):
        with self.assertRaises(module.ActionsImportError):
            module.verify_lineage(FakeGh(self.zip), "123", attempt="1")
        with self.assertRaises(module.ActionsImportError):
            module.verify_lineage(FakeGh(self.zip), "123", expected_head_sha="f" * 40)
        client = FakeGh(self.zip)
        client.artifact["created_at"] = "2026-09-04T00:01:00Z"
        with self.assertRaises(module.ActionsImportError):
            module.verify_lineage(client, "123")

    def test_expired_ambiguous_or_foreign_artifact_rejected(self):
        client = FakeGh(self.zip)
        client.artifact["expired"] = True
        with self.assertRaises(module.ActionsImportError): module.verify_lineage(client, "123")
        client = FakeGh(self.zip)
        client.listing = [client.artifact, copy.deepcopy(client.artifact)]
        with self.assertRaises(module.ActionsImportError): module.verify_lineage(client, "123")
        client = FakeGh(self.zip)
        client.artifact["workflow_run"]["id"] = 124
        with self.assertRaises(module.ActionsImportError): module.verify_lineage(client, "123")

    def test_zip_traversal_extra_file_symlink_and_size_caps(self):
        for name in ("../intake.sealed.json", "/intake.sealed.json", "nested/intake.sealed.json", "intake.sealed.json\\evil"):
            with self.subTest(name=name), self.assertRaises(module.ActionsImportError):
                module.extract_sealed(zip_bytes([(name, module.encode(self.sealed))]))
        with self.assertRaises(module.ActionsImportError):
            module.extract_sealed(zip_bytes([("intake.sealed.json", module.encode(self.sealed)), ("extra.txt", b"no")]))
        symbolic = zipfile.ZipInfo("intake.sealed.json")
        symbolic.create_system = 3
        symbolic.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(module.ActionsImportError):
            module.extract_sealed(zip_bytes([(symbolic, b"target")]))
        with patch.object(module, "MAX_ZIP_BYTES", 1), self.assertRaises(module.ActionsImportError):
            module.extract_sealed(self.zip)
        sealed = {**self.sealed, "plaintext_bytes": module.MAX_BUNDLE_BYTES + 1}
        with self.assertRaises(module.ActionsImportError):
            module.extract_sealed(zip_bytes([("intake.sealed.json", module.encode(sealed))]))

    def test_duplicate_json_keys_are_rejected(self):
        with self.assertRaises(module.ActionsImportError):
            module.read_json_bytes(b'{"a":1,"a":2}', 100)

    def test_authenticated_import_writes_private_immutable_plan_without_media(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.prepare_root(directory)
            client = FakeGh(self.zip)
            result = module.import_run("123", root=root, client=client, unsealer=self.unseal)
            self.assertEqual(client.auth_calls, 1)
            self.assertEqual(result["records"], 1)
            self.assertEqual(result["media_downloads"], 0)
            self.assertEqual(result["provider_calls"], 0)
            receipt = json.loads((root / result["receipt"]).read_bytes())
            self.assertEqual(receipt["zip_sha256_calculated"], module.digest(self.zip))
            self.assertFalse(receipt["server_artifact_digest"]["verified"])
            self.assertFalse(receipt["image_approved"])
            plan = json.loads((root / result["plan"]).read_bytes())
            self.assertTrue(plan["origin_verified"])
            self.assertFalse(plan["human_approved"])
            self.assertEqual(plan["records"][0]["original_prompt"], "Private exact prompt.\n")
            with patch.object(module, "unseal_local", side_effect=AssertionError("must reuse exact existing plaintext")):
                again = module.import_run("123", root=root, client=client)
            self.assertEqual(result, again)

    def test_wrong_unsealed_hash_preserves_partial_evidence_without_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.prepare_root(directory)
            def wrong(source, output, key): output.write_bytes(b"wrong")
            with self.assertRaises(module.ActionsImportError):
                module.import_run("123", root=root, client=FakeGh(self.zip), unsealer=wrong)
            base = root / module.IMPORT_ROOT
            self.assertEqual(len(list(base.glob("*/artifact.zip"))), 1)
            self.assertFalse(list(base.glob("*/receipt.json")))

    def test_private_path_and_missing_key_fail_before_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeGh(self.zip)
            with self.assertRaises(OSError): module.import_run("123", root=root, client=client)
            self.assertEqual(client.auth_calls, 0)
            with self.assertRaises(module.ActionsImportError): module.private_path(root, "../outside")

    def test_bundle_rights_or_source_container_tamper_is_rejected(self):
        bundle = copy.deepcopy(self.bundle)
        bundle["public_release"] = True
        with self.assertRaises(module.ActionsImportError): module.validate_bundle(bundle)
        bundle = copy.deepcopy(self.bundle)
        bundle["containers"][0]["raw_utf8"] += "changed"
        with self.assertRaises(module.ActionsImportError): module.validate_bundle(bundle)

    def test_reported_artifact_size_is_not_claimed_as_zip_byte_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.prepare_root(directory)
            client = FakeGh(self.zip)
            client.artifact["size_in_bytes"] = len(self.zip) + 12
            result = module.import_run("123", root=root, client=client, unsealer=self.unseal)
            receipt = json.loads((root / result["receipt"]).read_bytes())
            self.assertNotEqual(receipt["artifact_size_bytes_reported"], receipt["zip_bytes_calculated"])
            self.assertFalse(receipt["server_artifact_digest"]["verified"])

    def test_failed_auth_stops_before_api_or_import_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.prepare_root(directory)
            client = FakeGh(self.zip)
            with patch.object(client, "authenticate", side_effect=module.ActionsImportError("auth failed")):
                with self.assertRaises(module.ActionsImportError):
                    module.import_run("123", root=root, client=client)
            self.assertFalse(client.calls)
            self.assertFalse((root / module.IMPORT_ROOT).exists())

    def test_subprocess_stdout_cap_and_failure_are_sanitized(self):
        self.assertEqual(module._bounded_command([sys.executable, "-c", "print('ok')"], 100).strip(), b"ok")
        with self.assertRaises(module.ActionsImportError):
            module._bounded_command([sys.executable, "-c", "print('x'*200)"], 100)
        with self.assertRaises(module.ActionsImportError) as failure:
            module._bounded_command([sys.executable, "-c", "import sys; print('do-not-log'); sys.exit(1)"], 100)
        self.assertNotIn("do-not-log", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
