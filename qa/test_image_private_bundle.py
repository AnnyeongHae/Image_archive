"""Offline bundle tests using an isolated real committed administrator fixture."""
from __future__ import annotations

import copy
import io
import json
import runpy
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from image_rag_eval import private_library_bundle as module
from image_rag_eval.approval_handoff import HandoffError, _committed
from image_rag_eval.admin_store import AdminStore
import test_image_admin_handoff as handoff_fixtures


class PrivateBundleTests(unittest.TestCase):
    def setUp(self):
        # Reuse fixture construction, not its operational test methods. Every
        # database and input is underneath a new TemporaryDirectory.
        self.fixture = handoff_fixtures.HandoffTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.path, self.run = self.fixture.root, self.fixture.path, self.fixture.run
        self.rights_patch = patch.object(module, "build_rights_catalog", return_value=copy.deepcopy(self.fixture.rights))
        self.rights_patch.start()
        self.addCleanup(self.rights_patch.stop)

    def prepare(self, **kwargs):
        return module.prepare_private_bundle(self.root, self.path, self.run, **kwargs)

    def document(self, result, name="library.json"):
        return json.loads((self.root / result["bundle_path"] / name).read_text(encoding="utf-8"))

    def source_bytes(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file() and not path.name.endswith(("-wal", "-shm"))
                and not path.is_relative_to(self.root / module.OUTPUT)}

    def test_dry_run_creates_no_bundle_and_does_not_call_providers(self):
        before = self.source_bytes()
        approval = _committed(self.path, self.run)
        with patch("urllib.request.urlopen", side_effect=AssertionError("No external network")):
            result = self.prepare()
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["provider_calls"], 0)
        self.assertEqual(result["external_writes"], 0)
        self.assertFalse(result["deploy_enabled"])
        self.assertFalse((self.root / module.OUTPUT).exists())
        self.assertEqual(before, self.source_bytes())
        self.assertEqual(approval, _committed(self.path, self.run))

    def test_portable_json_preserves_full_prompts_rights_memos_and_only_approved_images(self):
        result = self.prepare(apply=True)
        bundle = self.document(result)
        library = bundle["library"]
        items = {row["id"]: row for row in library["items"]}
        self.assertEqual(set(items), set("ADEF"))
        self.assertNotIn("B", items)  # Explicit prior human opt-out stays out.
        self.assertNotIn("C", items)  # Human exact duplicate stays excluded.
        self.assertNotIn("G", items)  # Machine exact alias stays excluded.
        self.assertEqual(items["F"]["memo_text"], "주관적인 메모 🧩")
        self.assertEqual(items["F"]["original_prompt"]["full_prompt"], "원본 프롬프트 F")
        self.assertNotEqual(items["F"]["original_prompt"]["full_prompt"], "short prompt")
        self.assertEqual(items["A"]["rights_display"]["status"], "unverified")
        self.assertFalse(items["A"]["rights_display"]["image_license_verified"])
        self.assertFalse(bundle["mutation_enabled"])
        self.assertFalse(bundle["release_eligible"])
        self.assertEqual(bundle["visibility"], "private_access_only")

    def test_human_group_projection_is_preserved_without_new_similarity_inference(self):
        result = self.prepare(apply=True)
        library = self.document(result)["library"]
        self.assertEqual(library["counts"]["approved_images"], 4)
        self.assertEqual(library["counts"]["display_groups"], 1)
        self.assertEqual(library["display_groups"][0]["member_ids"], list("DE"))
        self.assertEqual(set(library["ungrouped_ids"]), set("AF"))
        self.assertEqual(library["group_membership_policy"], "committed_only_no_automatic_merge")

    def test_portable_allowlist_has_no_filesystem_paths_credentials_or_local_preview_fields(self):
        result = self.prepare(apply=True)
        bundle = self.document(result)
        text = json.dumps(bundle, ensure_ascii=False)
        self.assertNotIn(str(self.root), text)
        for forbidden in ("local_source_path", "prepared_path", "manifest_path", "vector-cache", "state.sqlite3", "API_KEY"):
            self.assertNotIn(forbidden, text)
        for item in bundle["library"]["items"]:
            self.assertTrue(item["media_key"].startswith("private-library/media/"))
            self.assertNotIn("..", item["media_key"])
            self.assertTrue(item["media_key"].endswith(item["media_sha256"] + ".png"))

    def test_local_media_plan_is_separate_hash_bound_and_not_upload_authority(self):
        result = self.prepare(apply=True)
        plan = self.document(result, "media-plan.local.json")
        receipt = self.document(result, "receipt.json")
        self.assertFalse(plan["upload_authorized"])
        self.assertFalse(plan["public_release_authorized"])
        self.assertEqual(plan["library_sha256"], result["library_sha256"])
        self.assertEqual(receipt["media_plan_sha256"], module.digest(module.encode(plan)))
        for media in plan["items"]:
            path = self.root / media["local_source_path"]
            self.assertTrue(path.resolve().is_relative_to(self.root))
            self.assertEqual(module.digest(path.read_bytes()), media["sha256"])
            self.assertEqual(path.stat().st_size, media["bytes"])
        directory = self.root / result["bundle_path"]
        self.assertEqual({path.name for path in directory.iterdir()}, {"library.json", "media-plan.local.json", "receipt.json"})
        self.assertEqual(list(directory.rglob("*.png")), [])

    def test_new_saved_draft_does_not_leak_into_latest_committed_export(self):
        self.fixture.advance_to_four()
        decisions = self.fixture.store.state()["decisions"]
        next(row for row in decisions["image_approvals"] if row["id"] == "F")["memo_text"] = "NOT COMMITTED"
        self.fixture.store.save_draft(self.fixture.body(decisions=decisions))
        result = self.prepare(apply=True)
        bundle = self.document(result)
        self.assertEqual(bundle["source_commit"]["revision"], 0)
        self.assertEqual(next(row for row in bundle["library"]["items"] if row["id"] == "F")["memo_text"], "주관적인 메모 🧩")

    def test_same_commit_restart_is_byte_identical_and_unchanged(self):
        first = self.prepare(apply=True)
        directory = self.root / first["bundle_path"]
        before = {path.name: path.read_bytes() for path in directory.iterdir()}
        second = self.prepare(apply=True)
        dry_again = self.prepare()
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(dry_again["status"], "unchanged")
        self.assertEqual(second["bundle_path"], first["bundle_path"])
        self.assertEqual(before, {path.name: path.read_bytes() for path in directory.iterdir()})

    def test_new_commit_appends_new_bundle_without_changing_old_one(self):
        first = self.prepare(apply=True)
        first_file = self.root / first["bundle_path"] / "library.json"
        original = first_file.read_bytes()
        self.fixture.advance_to_four()
        decisions = self.fixture.store.state()["decisions"]
        next(row for row in decisions["image_approvals"] if row["id"] == "F")["memo_text"] = "다음 버전"
        self.fixture.store.advance(self.fixture.body(decisions=decisions))
        second = self.prepare(apply=True)
        self.assertNotEqual(first["bundle_path"], second["bundle_path"])
        self.assertEqual(first_file.read_bytes(), original)
        self.assertEqual(next(row for row in self.document(second)["library"]["items"] if row["id"] == "F")["memo_text"], "다음 버전")

    def test_tampered_existing_bundle_is_not_overwritten(self):
        result = self.prepare(apply=True)
        file = self.root / result["bundle_path"] / "library.json"
        file.write_bytes(b"tampered")
        with self.assertRaisesRegex(HandoffError, "never overwrite"):
            self.prepare(apply=True)
        self.assertEqual(file.read_bytes(), b"tampered")

    def test_unexpected_existing_file_is_not_deleted(self):
        result = self.prepare(apply=True)
        file = self.root / result["bundle_path"] / "unrelated.txt"
        file.write_text("preserve")
        with self.assertRaisesRegex(HandoffError, "unexpected files"):
            self.prepare(apply=True)
        self.assertEqual(file.read_text(), "preserve")

    def test_expected_stale_commit_fails_before_output_creation(self):
        with self.assertRaisesRegex(HandoffError, "not the latest"):
            self.prepare(apply=True, expected_commit_id="old")
        self.assertFalse((self.root / module.OUTPUT).exists())

    def test_no_committed_approval_fails_without_inference(self):
        path = self.root / "blank.sqlite3"
        AdminStore(path, self.fixture.spec)
        with self.assertRaisesRegex(HandoffError, "committed human approval"):
            module.prepare_private_bundle(self.root, path, self.run, apply=True)

    def test_late_commit_race_cleans_only_own_temporary_files(self):
        calls = 0
        def latest(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise HandoffError("changed before publish")
        before = self.source_bytes()
        with patch.object(module, "_require_latest", side_effect=latest):
            with self.assertRaisesRegex(HandoffError, "before publish"):
                self.prepare(apply=True)
        self.assertEqual(list((self.root / module.OUTPUT).iterdir()), [])
        self.assertEqual(self.source_bytes(), before)

    def test_changed_media_hash_is_rejected_even_after_initial_frozen_loader(self):
        path = self.fixture.directory / self.fixture.spec["items"][0]["prepared_path"]
        path.write_bytes(b"different image bytes")
        with patch.object(module, "load_frozen_workflow", return_value=self.fixture.spec):
            with self.assertRaisesRegex(HandoffError, "preview identity changed"):
                self.prepare(apply=True)
        self.assertFalse((self.root / module.OUTPUT).exists())

    def test_size_limit_fails_before_writes(self):
        with patch.object(module, "MAX_BUNDLE_BYTES", 1):
            with self.assertRaisesRegex(HandoffError, "exceeds"):
                self.prepare(apply=True)
        self.assertFalse((self.root / module.OUTPUT).exists())

    def test_source_manifest_drift_is_rejected(self):
        binding = json.loads((self.fixture.directory / "source-bindings.json").read_text())
        (self.root / binding["files"][0]["path"]).write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            self.prepare(apply=True)


class PrivateBundleCLITests(unittest.TestCase):
    def test_cli_is_dry_run_by_default_and_passes_explicit_apply_only(self):
        script = Path(__file__).resolve().parents[1] / "src/prepare_image_private_bundle.py"
        for extra, expected in (([], False), (["--apply"], True)):
            output = io.StringIO()
            with self.subTest(extra=extra), patch.object(sys, "argv", [str(script), "--run-id", "fixture", "--expected-commit-id", "bound", *extra]):
                with patch.object(module, "prepare_private_bundle", return_value={"status": "mock-only"}) as prepare:
                    with redirect_stdout(output):
                        runpy.run_path(str(script), run_name="__main__")
            self.assertEqual(prepare.call_args.kwargs, {"apply": expected, "expected_commit_id": "bound"})
            self.assertEqual(prepare.call_args.args[2], "fixture")
            self.assertEqual(json.loads(output.getvalue()), {"status": "mock-only"})


if __name__ == "__main__":
    unittest.main()
