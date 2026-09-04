"""Synthetic offline intake -> cached candidates -> real local approval flow."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import http.client
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from github_sources.intake_envelope import parse_gallery
from image_rag_eval import intake_review
from image_rag_eval.admin_store import AdminStore
from image_rag_eval.admin_server import AdminHTTPServer, media_map
from image_rag_eval.approved_library import build_prompt_catalog
from image_rag_eval.approval_handoff import _committed, prepare_admin_handoff
from image_rag_eval.comparison import request_key
from image_rag_eval.experiment import digest, json_bytes, prepared_image
from image_rag_eval.group_workflow import blank_group_workflow_decisions
from image_rag_eval.incremental_workflow import load_frozen_workflow
from image_rag_eval.intake_media import prepare_assets
from image_rag_eval.rights import build_rights_catalog
from image_rag_eval.shared_vector_cache import RELATIVE_ROOT as CACHE_ROOT
from qa import test_v2_actions_import as actions_fixture


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_bytes(value))


def png(color: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (5, 7), color).save(output, format="PNG")
    return output.getvalue()


def git_blob(raw: bytes) -> str:
    return hashlib.sha1(f"blob {len(raw)}\0".encode("ascii") + raw).hexdigest()


class IntakeReviewIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "archive"
        self.root.mkdir()
        self.baseline_run = "baseline-human-fixture"
        self.review_run = "v2-incoming-human-fixture"
        self.db = self.root / "data/private-research/image-rag-admin/state.sqlite3"
        self.original_prompt = '  {\r\n  "목적": "정확한 원문 🧩",\r\n  "spaces": "  "\r\n}\n'
        self.originals = {"A": png("red"), "B": png("orange")}
        self._prepare_baseline()
        self._prepare_import()
        self.prepared = prepare_assets(self.root, self.bundle, self.media_rows)
        self.incoming_ids = [row["id"] for row in self.prepared["items"]]
        self.cache = {}
        for item in self.baseline_spec["items"] + self.prepared["items"]:
            self._cache_item(item)
        self.cache_patcher = patch("image_rag_eval.shared_vector_cache.lookup_shared_vectors", side_effect=self.lookup)
        self.cache_patcher.start()
        self.addCleanup(self.cache_patcher.stop)
        self.network_patcher = patch("urllib.request.urlopen", side_effect=AssertionError("No network in review integration"))
        self.network_patcher.start()
        self.addCleanup(self.network_patcher.stop)
        self.request_number = 0

    def _prepare_baseline(self):
        run = self.root / "data/private-research/image-rag-canary/runs" / self.baseline_run
        workflow = run / "group-workflow-v1"
        items, sources = [], []
        for index, (ident, raw) in enumerate(self.originals.items()):
            original = self.root / "data/private-research/fixture-media" / (ident + ".png")
            original.parent.mkdir(parents=True, exist_ok=True)
            original.write_bytes(raw)
            preview = prepared_image(original)
            sha = digest(preview)
            destination = run / "inputs" / (sha + ".png")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(preview)
            items.append({"id": ident, "style_id": "BASE-" + ident, "source_sha256": digest(raw),
                "prepared_sha256": sha, "prepared_path": "../inputs/" + sha + ".png",
                "priority": {"tier": 3, "ordinal": index, "rank_index": index, "label": "plain", "reason": "synthetic"}})
            sources.append({"id": ident, "style_id": "BASE-" + ident, "sha256": digest(raw),
                "prepared_sha256": sha, "prompt": "Synthetic baseline " + ident, "prompt_truncated": False,
                "source_name": "Baseline fixture", "source_url": "https://example.org/baseline/" + ident,
                "catalog_key": "fixture:" + ident, "lane": "legacy", "signals": {}})
        spec = {"schema_version": "image-group-workflow-spec-1", "run_id": self.baseline_run,
            "approval_policy": "default_retained_images_after_review_v1", "items": items,
            "stage1": {"active_ids": ["A", "B"], "archived": [], "alias_lineage": []},
            "baseline": {"read_only_ids": [], "image_approvals": [], "groups": []},
            "duplicate_candidates": [], "similarity_candidates": [],
            "release_eligible": False, "public_rights_approved": False}
        spec["spec_sha256"] = digest(json_bytes(spec))
        manifest_relative = "data/private-research/image-rag-canary/runs/baseline-source/manifest.json"
        write(self.root / manifest_relative, {"schema_version": "1", "items": sources})
        workflow.mkdir(parents=True)
        (workflow / "submitted-baseline.raw.json").write_bytes(b"{}")
        binding = {"review_spec_sha256": spec["spec_sha256"], "source_decisions_sha256": digest(b"{}"),
            "files": [{"path": manifest_relative, "sha256": digest((self.root / manifest_relative).read_bytes())}]}
        write(workflow / "image-group-workflow.spec.json", spec)
        write(workflow / "source-bindings.json", binding)
        write(workflow / "build-receipt.json", {"schema_version": "image-incremental-human-workflow-1",
            "status": "ready", "run_id": self.baseline_run, "spec_sha256": spec["spec_sha256"],
            "binding_sha256": digest(json_bytes(binding))})
        seed = blank_group_workflow_decisions(spec)
        seed.update({"reviewer": "SYNTHETIC_HUMAN_FIXTURE", "reviewed_at": "2026-09-04T01:00:00Z",
            "image_approvals": [{"id": "A", "approved": True, "memo_text": "기존 승인 메모"},
                                {"id": "B", "approved": False, "memo_text": "기존 제외 메모"}]})
        self.baseline_store = AdminStore(self.db, spec, seed_decisions=seed)
        self.baseline_spec = spec
        for relative in ("00_CORE/schemas/image_archive_luna_metadata.schema.json",
                         "00_CORE/templates/image_archive_luna_metadata.instructions.md"):
            write(self.root.parent / relative, {"synthetic_fixture": True})

    def _prepare_import(self):
        fixture = actions_fixture.ActionsImportTests()
        fixture.setUp()
        raw_images = [self.originals["A"], png("green"), png("blue")]
        prompts = ['{"different":"prompt but exact baseline file"}\n', self.original_prompt, self.original_prompt]
        media_tree = {f"images/case-{index}.png": {"mode": "100644", "sha": git_blob(raw)}
                      for index, raw in enumerate(raw_images, 1)}
        body = "".join(f'<a name="case-{index}"></a>\n### Example {index}\n![image](../images/case-{index}.png)\n```text\n{prompt}```\n'
                       for index, prompt in enumerate(prompts, 1)).encode("utf-8")
        parsed = parse_gallery(body, source={"source_id": "integration-fixture", "repository": "freestylefly/awesome-gpt-image-2"},
            path="docs/gallery-part-1.md", commit="b" * 40, tree_sha="c" * 40, blob_sha=git_blob(body),
            media_tree=media_tree, observed_at="2026-09-04T01:01:00Z")
        self.bundle = {"schema_version": "archive-sealed-intake-bundle-1", "run_id": "untrusted-payload-name",
            "records": parsed["records"], "containers": [{"source_id": "integration-fixture", "path": "docs/gallery-part-1.md",
                "raw_utf8": body.decode("utf-8"), "sha256": digest(body), "parse_complete": True, "deferred": []}],
            "canonical_promotion": False, "public_release": False, "image_binaries_downloaded": 0}
        fixture.bundle = self.bundle
        fixture.bundle_bytes = actions_fixture.module.encode(self.bundle)
        fixture.sealed.update(plaintext_sha256=digest(fixture.bundle_bytes), plaintext_bytes=len(fixture.bundle_bytes))
        fixture.zip = actions_fixture.zip_bytes([("intake.sealed.json", actions_fixture.module.encode(fixture.sealed))])
        fixture.prepare_root(self.root)
        result = actions_fixture.module.import_run("123", root=self.root,
            client=actions_fixture.FakeGh(fixture.zip), unsealer=fixture.unseal)
        self.import_receipt = result["receipt"]
        self.import_sha = digest((self.root / self.import_receipt).read_bytes())
        self.media_rows = []
        for index, (record, raw) in enumerate(zip(self.bundle["records"], raw_images), 1):
            relative = f"data/private-research/fixture-media/incoming-{index}.png"
            (self.root / relative).write_bytes(raw)
            self.media_rows.append({"source_id": record["source_id"], "source_item_id": record["source_item_id"],
                "media_index": 0, "local_path": relative, "sha256": digest(raw)})
        self.media_relative = "data/private-research/fixture-media/media-bindings.json"
        write(self.root / self.media_relative, self.media_rows)

    def _cache_item(self, item):
        request = intake_review.request_for(item)
        key = request_key(request)
        if key in self.cache:
            return
        vector = [0.0] * 1024
        vector[len(self.cache)] = 1.0
        revision = digest(("synthetic-revision:" + key).encode())
        object_path = f"{CACHE_ROOT}/objects/{key[:2]}/{key}.json"
        write(self.root / object_path, {"schema_version": "image-shared-vector-object-1", "key": key,
            "request_identity": {**request, "protocol": "three-arm-canary-v1"}, "vector": vector,
            "vector_sha256": digest(json_bytes(vector))})
        for name in ("manifest.json", "receipt.json"):
            write(self.root / CACHE_ROOT / "revisions" / revision / name, {"synthetic_fixture_only": True, "key": key})
        self.cache[key] = {"key": key, "provider": "voyage", "model": intake_review.MODEL,
            "vector": vector, "vector_sha256": digest(json_bytes(vector)), "shared_revision_id": revision,
            "shared_object_path": object_path, "shared_provenance": [{"basis": "synthetic_offline_fixture"}]}

    def lookup(self, root, requests):
        self.assertEqual(Path(root).resolve(), self.root.resolve())
        return {request_key(request): copy.deepcopy(self.cache.get(request_key(request))) for request in requests}

    def build(self, **options):
        return intake_review.build_intake_review(self.root, import_receipt=self.import_receipt,
            import_receipt_sha256=self.import_sha, media_bindings=self.media_relative,
            baseline_run_id=self.baseline_run, db_path=self.db, review_run_id=self.review_run, **options)

    def output_path(self, name):
        return self.root / "data/private-research/image-rag-canary/runs" / self.review_run / "group-workflow-v1" / name

    def state_body(self, store, **extra):
        self.request_number += 1
        state = store.state()
        return {"run_id": self.review_run, "expected_revision": state["revision"],
            "request_id": f"synthetic-review-{self.request_number}", "stage": state["active_stage"], **extra}

    def new_store(self):
        spec = load_frozen_workflow(self.root, self.review_run)
        return AdminStore(self.db, spec, validate_source=lambda: load_frozen_workflow(self.root, self.review_run))

    def file_snapshot(self):
        return {path.relative_to(self.root).as_posix(): digest(path.read_bytes())
                for path in self.root.rglob("*") if path.is_file() and not path.name.endswith(("-wal", "-shm"))}

    def test_dry_run_is_read_only_and_does_not_infer_human_approval(self):
        before = self.file_snapshot()
        committed = _committed(self.db, self.baseline_run)
        result = self.build()
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual((result["incoming_images"], result["machine_aliases"], result["new_retained"]), (3, 1, 2))
        self.assertEqual(result["provider_calls"], 0)
        self.assertFalse(result["image_approved"])
        self.assertEqual(result["writes"], 0)
        self.assertEqual(self.file_snapshot(), before)
        self.assertEqual(_committed(self.db, self.baseline_run), committed)
        self.assertFalse(self.output_path("build-receipt.json").exists())

    def test_ready_snapshot_loads_idempotently_without_mutating_baseline_or_seeding_new_approval(self):
        before = _committed(self.db, self.baseline_run)
        started_at = datetime.now(timezone.utc)
        first = self.build(apply=True)
        finished_at = datetime.now(timezone.utc)
        self.assertEqual(first["status"], "ready_for_human_review")
        self.assertEqual(first["writes"], first["artifact_files"])
        spec = load_frozen_workflow(self.root, self.review_run)
        creation = json.loads(self.output_path("creation.json").read_bytes())
        self.assertEqual(creation["created_at"], spec["created_at"])
        self.assertNotEqual(spec["created_at"], before["commit"]["committed_at"])
        created_at = datetime.fromisoformat(spec["created_at"].replace("Z", "+00:00"))
        self.assertEqual(created_at.utcoffset().total_seconds(), 0)
        self.assertLessEqual(started_at, created_at)
        self.assertLessEqual(created_at, finished_at)
        snapshot = self.file_snapshot()
        second = self.build(apply=True)
        self.assertEqual(second["writes"], 0)
        self.assertEqual({key: value for key, value in second.items() if key != "writes"},
                         {key: value for key, value in first.items() if key != "writes"})
        self.assertEqual(self.file_snapshot(), snapshot)
        store = self.new_store()
        state = store.state()
        self.assertEqual(state["active_stage"], 1)
        self.assertEqual(state["completed_stages"], [])
        self.assertIsNone(state["last_commit"])
        self.assertEqual(store.gallery()["items"], [])
        self.assertEqual(spec["baseline"]["read_only_ids"], ["A", "B"])
        self.assertEqual(_committed(self.db, self.baseline_run), before)

    def test_exact_file_alias_preserves_old_keeper_and_same_prompt_distinct_images_stay_retained(self):
        self.build(apply=True)
        spec = load_frozen_workflow(self.root, self.review_run)
        alias = next(row for row in spec["stage1"]["archived"] if row["id"] == self.incoming_ids[0])
        self.assertEqual(alias["representative_id"], "A")
        self.assertNotIn(self.incoming_ids[0], spec["stage1"]["active_ids"])
        self.assertTrue(set(self.incoming_ids[1:]) <= set(spec["stage1"]["active_ids"]))
        self.assertTrue(any(set(self.incoming_ids[1:]) <= set(row["member_ids"]) for row in spec["similarity_candidates"]))
        self.assertFalse(spec["release_eligible"])

    def test_full_prompt_and_rights_reach_existing_consumers(self):
        self.build(apply=True)
        spec = load_frozen_workflow(self.root, self.review_run)
        prompt = build_prompt_catalog(self.root, spec)[self.incoming_ids[1]]
        self.assertEqual(prompt["full_prompt"], self.original_prompt)
        self.assertEqual(prompt["prompt_sha256"], digest(self.original_prompt.encode("utf-8")))
        rights = build_rights_catalog(self.root, spec)[self.incoming_ids[1]]
        self.assertEqual(rights["source_name"], "integration-fixture")
        self.assertEqual(rights["source_url"], self.bundle["records"][1]["source_url"])
        self.assertEqual(rights["status"], "unverified")
        self.assertFalse(rights["release_eligible"])

    def test_loopback_http_serves_unapproved_stage_one_bound_preview_and_exact_prompt(self):
        self.build(apply=True)
        spec = load_frozen_workflow(self.root, self.review_run)
        store = self.new_store()
        server = AdminHTTPServer(("127.0.0.1", 0), store=store,
            static_dir=Path(__file__).resolve().parents[1] / "app/image-admin",
            media=media_map(self.root, self.review_run, spec),
            validate_source=lambda: load_frozen_workflow(self.root, self.review_run),
            rights_catalog=build_rights_catalog(self.root, spec),
            prompt_catalog=build_prompt_catalog(self.root, spec))
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request("GET", "/api/admin/session")
            response = connection.getresponse()
            cookie = response.getheader("Set-Cookie").split(";", 1)[0]
            self.assertEqual(response.status, 200)
            response.read()

            def get(path):
                connection.request("GET", path, headers={"Cookie": cookie})
                received = connection.getresponse()
                self.assertEqual(received.status, 200)
                return received.read(), received.getheader("Content-Type")

            raw_state, _ = get("/api/admin/state")
            state = json.loads(raw_state)
            self.assertEqual(state["active_stage"], 1)
            self.assertEqual(state["completed_stages"], [])
            self.assertIsNone(state["last_commit"])
            selected = self.prepared["items"][1]
            raw_preview, mime = get("/media/" + selected["id"])
            self.assertEqual(digest(raw_preview), selected["prepared_sha256"])
            self.assertEqual(mime, "image/png")
            raw_prompt, _ = get("/api/admin/prompt/" + selected["id"])
            prompt = json.loads(raw_prompt)
            self.assertEqual(prompt["status"], "available")
            self.assertEqual(prompt["full_prompt"], self.original_prompt)
            self.assertEqual(prompt["prompt_sha256"], digest(self.original_prompt.encode("utf-8")))
            self.assertIsNone(store.state()["last_commit"])
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

    def test_missing_cache_blocks_even_apply_and_writes_no_serveable_run(self):
        key = request_key(intake_review.request_for(self.prepared["items"][1]))
        self.cache.pop(key)
        before = self.file_snapshot()
        result = self.build(apply=True)
        self.assertEqual(result["status"], "blocked_missing_image_vectors")
        self.assertEqual(result["missing_image_vectors"][0]["id"], self.incoming_ids[1])
        self.assertEqual(self.file_snapshot(), before)
        self.assertFalse(self.output_path("build-receipt.json").exists())
        self.assertFalse(self.output_path("image-group-workflow.spec.json").exists())

    def test_advance_four_preserves_memo_optout_and_builds_pending_handoff_only(self):
        baseline_before = _committed(self.db, self.baseline_run)
        self.build(apply=True)
        store = self.new_store()
        decisions = store.state()["decisions"]
        decisions["reviewer"] = "SYNTHETIC_HUMAN_FIXTURE"
        store.advance(self.state_body(store, decisions=decisions))
        self.assertEqual(store.state()["active_stage"], 2)
        store.advance(self.state_body(store))
        decisions = store.state()["decisions"]
        for candidate in decisions["similarity_reviews"]:
            spec_candidate = next(row for row in store.spec["similarity_candidates"] if row["id"] == candidate["candidate_id"])
            candidate.update(decision="approve_selected", selected_ids=spec_candidate["member_ids"])
        store.advance(self.state_body(store, decisions=decisions))
        self.assertEqual(store.state()["active_stage"], 4)
        self.assertIsNone(store.state()["last_commit"])
        decisions = store.state()["decisions"]
        decisions["image_approvals"] = [{"id": self.incoming_ids[1], "approved": True, "memo_text": "개인 검토 메모 🧩"},
                                       {"id": self.incoming_ids[2], "approved": False, "memo_text": "이번에는 제외"}]
        state = store.advance(self.state_body(store, decisions=decisions))
        self.assertEqual(state["status"], "committed")
        self.assertEqual({row["id"] for row in store.gallery()["items"]}, {"A", self.incoming_ids[1]})
        result = prepare_admin_handoff(self.root, self.db, self.review_run, apply=True,
                                      expected_commit_id=state["last_commit"]["id"])
        directory = self.root / result["handoff_path"]
        snapshot = json.loads((directory / "snapshot.json").read_bytes())
        items = {row["id"]: row for row in snapshot["items"]}
        self.assertEqual(items[self.incoming_ids[1]]["human_memo"], "개인 검토 메모 🧩")
        self.assertEqual(items[self.incoming_ids[1]]["prompt"], self.original_prompt)
        self.assertFalse(items[self.incoming_ids[2]]["approved"])
        self.assertFalse(items["B"]["approved"])
        self.assertEqual(items["B"]["human_memo"], "기존 제외 메모")
        self.assertEqual(items[self.incoming_ids[0]]["archived_alias"]["final_representative_id"], "A")
        self.assertEqual(result["provider_calls"], 0)
        self.assertFalse(snapshot["release_eligible"])
        luna = json.loads((directory / "luna-pending.json").read_bytes())
        self.assertFalse(luna["actual_inference_performed"])
        self.assertEqual({row["id"] for row in luna["tasks"]}, {"A", self.incoming_ids[1]})
        self.assertEqual(_committed(self.db, self.baseline_run), baseline_before)

    def test_modified_media_bindings_prevent_progress_without_state_change(self):
        self.build(apply=True)
        store = self.new_store()
        before = store.state()
        (self.root / self.media_relative).write_bytes(b"[]")
        decisions = before["decisions"]
        decisions["reviewer"] = "SYNTHETIC_HUMAN_FIXTURE"
        with self.assertRaises(ValueError):
            store.advance(self.state_body(store, decisions=decisions))
        self.assertEqual(store.state()["revision"], before["revision"])
        self.assertIsNone(store.state()["last_commit"])

    def test_modified_frozen_preview_prevents_loading(self):
        self.build(apply=True)
        item = self.prepared["items"][1]
        target = self.output_path("build-receipt.json").parent.parent / "inputs" / (item["prepared_sha256"] + ".png")
        target.write_bytes(png("purple"))
        with self.assertRaises(ValueError):
            load_frozen_workflow(self.root, self.review_run)

    def test_modified_original_media_prevents_progress(self):
        self.build(apply=True)
        store = self.new_store()
        before = store.state()
        (self.root / self.media_rows[1]["local_path"]).write_bytes(png("purple"))
        decisions = before["decisions"]
        decisions["reviewer"] = "SYNTHETIC_HUMAN_FIXTURE"
        with self.assertRaises(ValueError):
            store.advance(self.state_body(store, decisions=decisions))
        self.assertEqual(store.state()["revision"], before["revision"])
        self.assertIsNone(store.state()["last_commit"])

    def test_legacy_original_and_catalog_evidence_paths_remain_compatible_and_bound(self):
        relative_images = "legacy/current_archive/assets/images/fixture-original.png"
        relative_catalog = "deploy/cloudflare-public/public/catalog-data.js"
        for relative, raw in ((relative_images, self.originals["A"]), (relative_catalog, b"const fixture = [];")):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        workflow = self.root / "data/private-research/image-rag-canary/runs" / self.baseline_run / "group-workflow-v1"
        binding = json.loads((workflow / "source-bindings.json").read_bytes())
        binding["files"].extend({"path": relative, "sha256": digest((self.root / relative).read_bytes())}
                                for relative in (relative_images, relative_catalog))
        write(workflow / "source-bindings.json", binding)
        receipt = json.loads((workflow / "build-receipt.json").read_bytes())
        receipt["binding_sha256"] = digest(json_bytes(binding))
        write(workflow / "build-receipt.json", receipt)
        self.build(apply=True)
        load_frozen_workflow(self.root, self.review_run)
        (self.root / relative_images).write_bytes(png("purple"))
        with self.assertRaises(ValueError):
            load_frozen_workflow(self.root, self.review_run)

    def test_replay_of_archived_original_resolves_to_its_human_keeper(self):
        incoming = copy.deepcopy(self.prepared["items"][1])
        old_sources = {"keeper": {"sha256": "f" * 64, "prompt": "human retained", "signals": {}},
                       "archived": {"sha256": incoming["sha256"], "prompt": "different archived prompt", "signals": {}}}
        fresh, aliases = intake_review._exact_incoming(old_sources, ["keeper"], [incoming],
            archived=[{"id": "archived", "representative_id": "keeper"}])
        self.assertEqual(fresh, [])
        self.assertEqual(aliases[0]["id"], incoming["id"])
        self.assertEqual(aliases[0]["representative_id"], "keeper")
        self.assertIn("archived", json.dumps(aliases[0]["evidence"]))
        self.assertFalse(aliases[0]["physical_delete"])

    def test_modified_cache_object_or_proof_prevents_loading(self):
        self.build(apply=True)
        proof = json.loads(self.output_path("vector-proof.json").read_bytes())
        target = self.root / proof[0]["object_path"]
        original = target.read_bytes()
        target.write_bytes(b"{}")
        with self.assertRaises(ValueError):
            load_frozen_workflow(self.root, self.review_run)
        target.write_bytes(original)
        self.output_path("vector-proof.json").write_bytes(b"[]")
        with self.assertRaises(ValueError):
            load_frozen_workflow(self.root, self.review_run)

    def test_rehashed_wrong_model_proof_cannot_bypass_embedding_gate(self):
        self.build(apply=True)
        proof_path = self.output_path("vector-proof.json")
        proof = json.loads(proof_path.read_bytes())
        proof[0]["request_identity"]["model"] = "wrong-vector-space"
        write(proof_path, proof)
        binding_path = self.output_path("source-bindings.json")
        binding = json.loads(binding_path.read_bytes())
        relative = proof_path.relative_to(self.root).as_posix()
        next(row for row in binding["files"] if row["path"] == relative)["sha256"] = digest(proof_path.read_bytes())
        write(binding_path, binding)
        receipt_path = self.output_path("build-receipt.json")
        receipt = json.loads(receipt_path.read_bytes())
        receipt["binding_sha256"] = digest(json_bytes(binding))
        write(receipt_path, receipt)
        with self.assertRaisesRegex(ValueError, "cached_image_proof_mismatch"):
            load_frozen_workflow(self.root, self.review_run)

    def test_wrong_pinned_import_receipt_fails_without_new_run(self):
        self.import_sha = "f" * 64
        with self.assertRaisesRegex(ValueError, "import_receipt_changed"):
            self.build(apply=True)
        self.assertFalse(self.output_path("build-receipt.json").exists())


if __name__ == "__main__":
    unittest.main()
