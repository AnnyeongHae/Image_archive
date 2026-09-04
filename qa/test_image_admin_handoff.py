from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from image_rag_eval.admin_store import AdminStore
from image_rag_eval.approval_handoff import (
    HandoffError, RELATIVE_ROOT, _archive, _committed, _file_hash, _hash, _safe, _vector_refs, handoff_status, prepare_admin_handoff,
)
from image_rag_eval.experiment import digest, json_bytes
from image_rag_eval.group_workflow import DEFAULT_IMAGE_APPROVAL_POLICY, blank_group_workflow_decisions


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_bytes(value))


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "archive"
        self.root.mkdir()
        self.run = "handoff-fixture"
        self.directory = self.root / f"data/private-research/image-rag-canary/runs/{self.run}/group-workflow-v1"
        self.directory.mkdir(parents=True)
        self.spec, self.seed = self.fixture()
        self.path = self.root / "data/private-research/image-rag-admin/state.sqlite3"
        self.store = AdminStore(self.path, self.spec, self.seed)
        self.counter = 0
        self.rights = {item["id"]: {"schema_version": "image-rights-notice-1", "status": "unverified",
            "source_name": "fixture", "source_url": "https://example.org/source", "creator_name": None,
            "license_label": "MIT", "license_scope": "repository_only", "notice_text": "권리 미확인",
            "release_eligible": False, "image_license_verified": False} for item in self.spec["items"]}
        self.rights_patch = patch("image_rag_eval.approval_handoff._rights", return_value=self.rights)
        self.rights_patch.start()
        self.addCleanup(self.rights_patch.stop)

    def fixture(self, count=7):
        ids = list("ABCDEFG") if count == 7 else [f"image-{index:03}" for index in range(count)]
        items, sources = [], []
        for index, ident in enumerate(ids):
            content = f"fixture-preview-{ident}".encode()
            sha = digest(content)
            preview = self.directory.parent / "inputs" / f"{sha}.png"
            preview.parent.mkdir(exist_ok=True)
            preview.write_bytes(content)
            items.append({"id": ident, "style_id": ident, "prepared_path": f"../inputs/{sha}.png",
                          "prepared_sha256": sha, "source_sha256": digest(f"original-{ident}".encode()),
                          "priority": {"tier": 1, "ordinal": index, "rank_index": index, "label": "tier1", "reason": "json"}})
            sources.append({"id": ident, "style_id": ident, "sha256": items[-1]["source_sha256"],
                "prepared_sha256": sha, "prompt": "원본 프롬프트 " + ident, "embedding_prompt": "short prompt",
                "source_name": "fixture", "catalog_key": "legacy:" + ident,
                "lane": "legacy", "signals": {"pixel_sha256": sha}})
        small = count == 7
        spec = {"schema_version": "image-group-workflow-spec-1", "run_id": self.run,
                "approval_policy": DEFAULT_IMAGE_APPROVAL_POLICY, "items": items,
                "stage1": {"active_ids": ids[:-1] if small else ids, "archived":
                           [{"id": "G", "representative_id": "A", "action": "logical_delete", "reasons": []}] if small else [],
                           "alias_lineage": []},
                "baseline": {"read_only_ids": list("AB") if small else [], "image_approvals":
                             [{"id": "A", "approved": True, "memo_text": "old memo"},
                              {"id": "B", "approved": False, "memo_text": "보류 유지"}] if small else [], "groups": []},
                "duplicate_candidates": [{"id": "dup-ac", "member_ids": list("AC"), "suggested_representative_id": "A"}] if small else [],
                "similarity_candidates": [{"id": "sim-de", "member_ids": list("DE"),
                                           "known_negative_pairs": [], "known_positive_pairs": []}] if small else []}
        spec["spec_sha256"] = digest(json_bytes(spec))
        manifest_path = self.root / "data/private-research/image-rag-canary/runs/source-fixture/manifest.json"
        write(manifest_path, {"schema_version": "1", "items": sources})
        raw = self.directory / "submitted-baseline.raw.json"
        raw.write_bytes(b"{}")
        binding = {"review_spec_sha256": spec["spec_sha256"], "source_decisions_sha256": digest(raw.read_bytes()),
                   "files": [{"path": manifest_path.relative_to(self.root).as_posix(), "sha256": _file_hash(manifest_path)}]}
        write(self.directory / "image-group-workflow.spec.json", spec)
        write(self.directory / "source-bindings.json", binding)
        write(self.directory / "build-receipt.json", {"schema_version": "image-incremental-human-workflow-1", "status": "ready",
              "run_id": self.run, "spec_sha256": spec["spec_sha256"], "binding_sha256": digest(json_bytes(binding))})
        for relative in ("00_CORE/schemas/image_archive_luna_metadata.schema.json", "00_CORE/templates/image_archive_luna_metadata.instructions.md"):
            write(self.root.parent / relative, {"fixture": True})
        seed = blank_group_workflow_decisions(spec)
        seed.update({"reviewer": "human-fixture", "reviewed_at": "2026-09-03T12:00:00Z"})
        if small:
            seed["duplicate_reviews"][0].update({"decision": "same_image_subset", "selected_ids": list("AC")})
            seed["similarity_reviews"][0].update({"decision": "approve_selected", "selected_ids": list("DE")})
            seed["image_approvals"] = [{"id": "F", "approved": True, "memo_text": "주관적인 메모 🧩"}]
        return spec, seed

    def body(self, **extra):
        self.counter += 1
        state = self.store.state()
        return {"run_id": self.run, "expected_revision": state["revision"], "request_id": f"event-{self.counter}",
                "stage": state["active_stage"], **extra}

    def advance_to_four(self):
        while self.store.state()["active_stage"] < 4:
            self.store.advance(self.body())

    def prepare(self, **kwargs):
        return prepare_admin_handoff(self.root, self.path, self.run, **kwargs)

    def document(self, result, name="snapshot.json"):
        return json.loads((self.root / result["handoff_path"] / name).read_text(encoding="utf-8"))

    def test_dry_run_writes_nothing_and_calls_no_provider(self):
        # A read-only connection to WAL SQLite may create/reuse its lock-index
        # sidecars. These are not SQL mutations or handoff artifacts.
        before_commit = _committed(self.path, self.run)
        before = {str(path): _file_hash(path) for path in self.root.rglob("*") if path.is_file() and not path.name.endswith(("-shm", "-wal"))}
        with patch("urllib.request.urlopen", side_effect=AssertionError("No network")):
            result = self.prepare()
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual((result["total_items"], result["retained_items"], result["approved_items"], result["archived_aliases"]), (7, 5, 4, 2))
        self.assertEqual(result["cached_retained_image_vectors"], 0)
        self.assertFalse((self.root / RELATIVE_ROOT).exists())
        self.assertEqual(before, {str(path): _file_hash(path) for path in self.root.rglob("*") if path.is_file() and not path.name.endswith(("-shm", "-wal"))})
        self.assertEqual(before_commit, _committed(self.path, self.run))

    def test_commit_only_snapshot_and_pending_outboxes_preserve_lineage_memos_false(self):
        result = self.prepare(apply=True)
        snapshot = self.document(result)
        self.assertEqual(snapshot["retained_ids"], list("ABDEF"))
        self.assertEqual(set(snapshot["approved_ids"]), set("ADEF"))
        self.assertEqual({row["id"] for row in snapshot["archived_aliases"]}, set("CG"))
        items = {row["id"]: row for row in snapshot["items"]}
        self.assertFalse(items["B"]["approved"])
        self.assertEqual(items["B"]["human_memo"], "보류 유지")
        self.assertEqual(items["F"]["human_memo"], "주관적인 메모 🧩")
        self.assertEqual(items["F"]["human_tags"], [])
        self.assertEqual(items["F"]["legacy_tags_texts"], ["주관적인 메모 🧩"])
        self.assertEqual(items["C"]["prompt"], "원본 프롬프트 C")
        self.assertEqual(items["G"]["archived_alias"]["final_representative_id"], "A")
        self.assertFalse(items["A"]["rights_display"]["image_license_verified"])
        self.assertFalse(snapshot["release_eligible"])
        self.assertEqual(snapshot["rights_catalog_sha256"], _hash(self.rights))
        luna = self.document(result, "luna-pending.json")
        text = self.document(result, "text-embedding-pending.json")
        self.assertEqual({row["id"] for row in luna["tasks"]}, set("ADEF"))
        self.assertTrue(all(row["result"] is None for row in luna["tasks"]))
        self.assertIsNone(text["model"])
        self.assertTrue(all(row["vector"] is None and row["embedding_request_key"] is None for row in text["tasks"]))
        self.assertNotIn("rights", " ".join(text["tasks"][0]["input_sections"]))
        self.assertEqual(luna["provider_calls"], 0)
        self.assertFalse(result["incremental_consumer_connected"])

    def test_newer_saved_draft_is_not_exported(self):
        self.advance_to_four()
        decisions = self.store.state()["decisions"]
        next(row for row in decisions["image_approvals"] if row["id"] == "F")["memo_text"] = "not committed"
        self.store.save_draft(self.body(decisions=decisions))
        result = self.prepare(apply=True)
        self.assertEqual(result["commit_revision"], 0)
        self.assertEqual(next(row for row in self.document(result)["items"] if row["id"] == "F")["human_memo"], "주관적인 메모 🧩")

    def test_restart_same_commit_reuses_without_cache_rebuild(self):
        first = self.prepare(apply=True)
        before = {str(path): path.read_bytes() for path in (self.root / first["handoff_path"]).iterdir()}
        with patch("image_rag_eval.approval_handoff._vector_refs", side_effect=AssertionError("Do not rebuild")):
            second = self.prepare(apply=True)
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(first["handoff_path"], second["handoff_path"])
        self.assertEqual(before, {str(path): path.read_bytes() for path in (self.root / first["handoff_path"]).iterdir()})

    def test_new_commit_gets_new_directory_and_memo_does_not_change_image_identity(self):
        first = self.prepare(apply=True)
        first_snapshot = self.document(first)
        self.advance_to_four()
        decisions = self.store.state()["decisions"]
        next(row for row in decisions["image_approvals"] if row["id"] == "F")["memo_text"] = "new committed memo"
        self.store.advance(self.body(decisions=decisions))
        second = self.prepare(apply=True)
        self.assertNotEqual(first["handoff_path"], second["handoff_path"])
        new = {row["id"]: row for row in self.document(second)["items"]}
        old = {row["id"]: row for row in first_snapshot["items"]}
        self.assertEqual(old["F"]["image_vector"], new["F"]["image_vector"])
        self.assertEqual(new["F"]["human_memo"], "new committed memo")
        self.assertTrue((self.root / first["handoff_path"] / "receipt.json").is_file())

    def test_expected_commit_and_commit_drift_fail_without_publish(self):
        with self.assertRaisesRegex(HandoffError, "stale"):
            self.prepare(apply=True, expected_commit_id="f" * 64)
        with patch("image_rag_eval.approval_handoff._require_latest", side_effect=HandoffError("commit changed")):
            with self.assertRaisesRegex(HandoffError, "changed"):
                self.prepare(apply=True)
        self.assertFalse((self.root / RELATIVE_ROOT).exists())

    def test_no_commit_does_not_infer_approval(self):
        path = self.root / "blank.sqlite3"
        AdminStore(path, self.spec)
        self.assertEqual(handoff_status(self.root, path, self.run)["status"], "pending_no_commit")
        with self.assertRaisesRegex(HandoffError, "No committed"):
            prepare_admin_handoff(self.root, path, self.run, apply=True)

    def test_tampered_frozen_preview_fails(self):
        path = self.directory / self.spec["items"][0]["prepared_path"]
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "preview"):
            self.prepare()

    def test_tampered_source_manifest_fails(self):
        binding = json.loads((self.directory / "source-bindings.json").read_text())
        (self.root / binding["files"][0]["path"]).write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            self.prepare()

    def test_tampered_existing_artifact_fails_closed(self):
        result = self.prepare(apply=True)
        (self.root / result["handoff_path"] / "luna-pending.json").write_bytes(b"{}")
        with self.assertRaisesRegex(HandoffError, "artifact changed"):
                self.prepare(apply=True)

    def test_metadata_contract_drift_blocks_reuse(self):
        self.prepare(apply=True)
        (self.root.parent / "00_CORE/templates/image_archive_luna_metadata.instructions.md").write_text("changed")
        with self.assertRaisesRegex(HandoffError, "metadata contract changed"):
            self.prepare(apply=True)

    def test_rights_normalization_drift_blocks_reuse(self):
        self.prepare(apply=True)
        self.rights["A"]["notice_text"] = "새로운 검토 필요"
        with self.assertRaisesRegex(HandoffError, "rights notices changed"):
            self.prepare(apply=True)

    def test_late_commit_change_cleans_only_own_staging_and_leaves_no_handoff(self):
        counter = 0
        def guard(*args):
            nonlocal counter
            counter += 1
            if counter == 3:
                raise HandoffError("late commit changed")
        with patch("image_rag_eval.approval_handoff._require_latest", side_effect=guard):
            with self.assertRaisesRegex(HandoffError, "late commit"):
                self.prepare(apply=True)
        self.assertEqual(list((self.root / RELATIVE_ROOT).iterdir()), [])
        self.assertIsNotNone(_committed(self.path, self.run)["commit"])

    def test_prepared_time_is_separate_from_committed_time(self):
        result = self.prepare(apply=True)
        receipt = self.document(result, "receipt.json")
        self.assertGreaterEqual(datetime.fromisoformat(receipt["prepared_at"]), datetime.fromisoformat(receipt["source_commit"]["committed_at"]))

    def test_cached_vector_wrong_model_is_rejected_without_provider(self):
        from image_rag_eval.comparison import request_key
        def wrong(request_root, requests):
            return {request_key(row): {"key": request_key(row), "provider": "voyage", "model": "wrong-model", "vector": [0.0] * 1024}
                    for row in requests}
        with patch("image_rag_eval.shared_vector_cache.lookup_shared_vectors", side_effect=wrong):
            with self.assertRaisesRegex(HandoffError, "vector identity mismatch"):
                _vector_refs(self.root, self.spec)

    def test_archived_alias_cycle_is_rejected(self):
        normalized = _committed(self.path, self.run)["normalized"]
        next(row for row in normalized["stage2_overlay"]["archived"] if row["id"] == "C")["representative_id"] = "G"
        next(row for row in normalized["stage2_overlay"]["archived"] if row["id"] == "G")["representative_id"] = "C"
        with self.assertRaisesRegex(HandoffError, "alias cycle"):
            _archive(normalized, self.spec)

    def test_outbox_cannot_claim_inference_even_with_rehashed_files(self):
        result = self.prepare(apply=True)
        directory = self.root / result["handoff_path"]
        luna = self.document(result, "luna-pending.json")
        luna["actual_inference_performed"] = True
        write(directory / "luna-pending.json", luna)
        receipt = self.document(result, "receipt.json")
        receipt["files"]["luna-pending.json"] = _file_hash(directory / "luna-pending.json")
        write(directory / "receipt.json", receipt)
        with self.assertRaisesRegex(HandoffError, "outbox is not bound"):
            self.prepare(apply=True)

    def test_status_is_readonly_current_commit_bound_and_relative(self):
        self.assertEqual(handoff_status(self.root, self.path, self.run)["status"], "pending_preparation")
        result = self.prepare(apply=True)
        status = handoff_status(self.root, self.path, self.run)
        self.assertEqual(status["commit_id"], result["commit_id"])
        self.assertNotIn(str(self.root), json.dumps(status))

    def test_unsafe_paths_are_rejected(self):
        for path in ("../secrets", "C:/secrets", "/secrets", "data/../../secrets", "data\\secrets"):
            with self.subTest(path=path), self.assertRaises(HandoffError):
                _safe(self.root, path)

    def test_full_committed_snapshot_is_not_capped_at_200(self):
        self.spec, self.seed = self.fixture(205)
        self.path = self.root / "large.sqlite3"
        self.store = AdminStore(self.path, self.spec, self.seed)
        self.rights_patch.stop()
        with patch("image_rag_eval.approval_handoff._rights", return_value={item["id"]: {"release_eligible": False} for item in self.spec["items"]}):
            result = self.prepare(apply=True)
        self.assertEqual(result["approved_items"], 205)
        self.assertEqual(len(self.document(result, "luna-pending.json")["tasks"]), 205)

    def test_corrupt_commit_front_is_not_accepted_even_with_valid_decision_hash(self):
        data = _committed(self.path, self.run)
        data["front"]["items"] = []
        with patch("image_rag_eval.approval_handoff._committed", return_value=data):
            with self.assertRaisesRegex(HandoffError, "projection mismatch"):
                self.prepare()

    def test_server_startup_after_store_reopen_reuses_last_commit_during_saved_draft(self):
        from image_rag_eval.admin_server import AdminHTTPServer
        first = self.prepare(apply=True)
        self.advance_to_four()
        decisions = self.store.state()["decisions"]
        next(row for row in decisions["image_approvals"] if row["id"] == "F")["memo_text"] = "unsaved downstream draft"
        self.store.save_draft(self.body(decisions=decisions))
        static = self.root / "test-ui"
        static.mkdir()
        for name in ("index.html", "admin.css", "admin.js"):
            (static / name).write_text("fixture")
        server = AdminHTTPServer(("127.0.0.1", 0), store=None, static_dir=static, media={},
            rights_catalog=self.rights, prepare_handoff=lambda commit_id: prepare_admin_handoff(
                self.root, self.path, self.run, apply=True, expected_commit_id=commit_id))
        self.addCleanup(server.server_close)
        server.store = AdminStore(self.path, self.spec, self.seed)
        state = server.store.state()
        self.assertEqual(state["status"], "saved")
        server.prepare_committed(state, allow_saved_draft=True)
        self.assertEqual(server.handoff_state(first["commit_id"])["status"], "prepared")
        self.assertEqual(server.store.state()["revision"], state["revision"])


if __name__ == "__main__":
    unittest.main()
