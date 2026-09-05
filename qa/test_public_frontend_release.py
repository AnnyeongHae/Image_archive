from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("public_frontend_release", ROOT / "platform/v2/local/public_frontend_release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
FIXTURE_SPEC = importlib.util.spec_from_file_location("projection_test_fixture", ROOT / "qa/test_frontend_projection.py")
fixtures = importlib.util.module_from_spec(FIXTURE_SPEC)
FIXTURE_SPEC.loader.exec_module(fixtures)
gallery = release.projection


class PublicFrontendReleaseTests(unittest.TestCase):
    # Reuse only fixture factories, not another test class's test methods.
    fixture = fixtures.FrontendProjectionTests.fixture
    save = fixtures.FrontendProjectionTests.save

    def setUp(self):
        fixtures.FrontendProjectionTests.setUp(self)
        (self.root / gallery.SHELL / "index.html").write_text("<!doctype html><footer>Fixture</footer>", encoding="utf-8")
        notice = self.root / release.NOTICES_PATH
        notice.parent.mkdir(parents=True)
        notice.write_bytes((ROOT / "deploy/cloudflare-public/source/THIRD_PARTY_NOTICES.txt").read_bytes())

    def draft(self, plan, ids=None):
        rows = plan["items"] if ids is None else [r for r in plan["items"] if r["item_id"] in ids]
        return {"schema_version": release.GRANT_SCHEMA, "decision": "review_pending", "purpose": "public_reference_display",
                "snapshot_id": plan["manifest"]["snapshot_id"], "snapshot_manifest_sha256": plan["manifest_sha256"],
                "approved_by": None, "approved_at": None, "decision_evidence": None,
                "commercial_rights_approved": False, "license_verified": False, "scopes": list(release.SCOPES),
                "items": [{"item_id": r["item_id"], "group_id": r["group_id"], "representative_id": r["representative_id"],
                           "prompt_sha256": r["private_data"]["prompt_sha256"], "prepared_sha256": r["private_data"]["prepared_sha256"]} for r in rows]}

    def write_grant(self, document):
        raw = gallery.encoded(document)
        digest = gallery.sha(raw)
        path = self.root / "data/private-research/grants" / digest / "grant.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path, digest

    def inputs(self):
        path = self.save()
        plan = gallery.read_snapshot(path, self.root)
        grant = self.draft(plan)
        grant_path, digest = self.write_grant(grant)
        return path, plan, grant, grant_path, digest

    def build(self, *, apply=False):
        snapshot, _, _, grant, digest = self.inputs()
        return release.build_candidate(self.root, snapshot=snapshot, grant=grant, grant_sha256=digest, apply=apply)

    def test_missing_grant_is_blocked_without_reads_or_writes(self):
        with patch.object(gallery, "read_snapshot", side_effect=AssertionError("should not read")):
            result = release.build_candidate(self.root, apply=True)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["release_eligible"])
        self.assertFalse((self.root / release.OUTPUT).exists())

    def test_dry_run_keeps_candidate_unwritten_and_not_release_eligible(self):
        result = self.build()
        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(Path(result["path"]).exists())
        self.assertFalse(result["release_eligible"])
        self.assertTrue(result["permission_pending"])
        self.assertEqual(result["counts"], {"images": 3, "groups": 2, "variants": 1, "excluded": 0, "withheld": 0})
        self.assertEqual(result["network_calls"] + result["model_calls"] + result["new_embedding_calls"], 0)

    def test_pending_candidate_has_fresh_public_assets_and_sibling_receipts(self):
        result = self.build(apply=True)
        target = Path(result["path"])
        receipt = json.loads((target / "candidate.json").read_bytes())
        self.assertEqual(receipt["schema_version"], release.CANDIDATE_SCHEMA)
        self.assertEqual(gallery.sha(gallery.encoded(receipt["identity"])), target.name)
        self.assertEqual(receipt["candidate_id"], target.name)
        self.assertEqual(receipt["status"], "prepare_only")
        self.assertFalse(receipt["release_eligible"])
        self.assertTrue(receipt["permission_pending"])
        self.assertEqual(receipt["grant_decision"], "review_pending")
        self.assertEqual(gallery.sha((target / "grant.json").read_bytes()), receipt["grant_sha256"])
        assets = target / "assets"
        self.assertFalse((assets / "grant.json").exists())
        self.assertFalse((assets / "candidate.json").exists())
        self.assertEqual({p.relative_to(assets).as_posix() for p in assets.rglob("*") if p.is_file()}, set(receipt["served_files"]))
        for path, digest in receipt["served_files"].items():
            self.assertEqual(gallery.sha((assets / path).read_bytes()), digest)
        catalog = json.loads((assets / "data/catalog.json").read_bytes())
        self.assertEqual(catalog["mode"], "public")
        self.assertEqual(catalog["status"], "public_reference_display")
        self.assertEqual(len(catalog["groups"]), 2)
        self.assertIn(b'notice.html', (assets / "index.html").read_bytes())
        self.assertIn(b'privacy.html', (assets / "index.html").read_bytes())

    def test_exact_prompts_metadata_groups_and_member_categories_preserved(self):
        snapshot, plan, _, grant, digest = self.inputs()
        result = release.build_candidate(self.root, snapshot=snapshot, grant=grant, grant_sha256=digest, apply=True)
        assets = Path(result["assets_path"])
        catalog = json.loads((assets / "data/catalog.json").read_bytes())
        by_id = {r["item_id"]: r for r in plan["items"]}
        card = next(g for g in catalog["groups"] if g["id"] == "confirmed-group")
        self.assertEqual(card["representative_id"], "asset-0")
        self.assertEqual(card["member_count"], 2)
        self.assertEqual(card["members"][1]["category_ids"], ["commerce_brand"])
        detail = json.loads((assets / card["detail_path"]).read_bytes())
        self.assertEqual([r["id"] for r in detail["members"]], ["asset-0", "asset-1"])
        for row in detail["members"]:
            self.assertEqual(row["original_prompt"], by_id[row["id"]]["original_prompt"])
            self.assertEqual(row["metadata_status"], "candidate")
            self.assertEqual(row["rights"]["badge"], "참고용 · 권리 미확인")
            self.assertIn("허가를 뜻하지 않습니다", row["rights"]["notice"])

    def test_private_fields_excluded_and_safe_source_queries_removed(self):
        result = self.build(apply=True)
        assets = Path(result["assets_path"])
        body = "\n".join(p.read_text(encoding="utf-8") for p in assets.rglob("*.json"))
        for secret in ("HUMAN_MEMO_PRIVATE", "RAW_PRIVATE", "QA_PRIVATE", "EXTRAS_PRIVATE", "SOURCE_PRIVATE", "RETRIEVAL_PRIVATE",
                       "PRIVATE_PROMPT_ANALYSIS", "SECRET", "data/private-research", str(self.root), '"private_data"', '"human_note"'):
            self.assertNotIn(secret, body)
        self.assertIn("https://example.com/source#safe", body)
        compact = (assets / "data/catalog.json").read_text(encoding="utf-8")
        self.assertNotIn("original_prompt", compact)
        self.assertNotIn("usage_notes", compact)

    def test_same_input_idempotent_but_existing_file_drift_refused(self):
        snapshot, _, _, grant, digest = self.inputs()
        first = release.build_candidate(self.root, snapshot=snapshot, grant=grant, grant_sha256=digest, apply=True)
        second = release.build_candidate(self.root, snapshot=snapshot, grant=grant, grant_sha256=digest, apply=True)
        self.assertEqual(first, second)
        catalog = Path(first["assets_path"]) / "data/catalog.json"
        catalog.write_bytes(b"{}")
        with self.assertRaisesRegex(release.ReleaseError, "immutable_candidate_conflict"):
            release.build_candidate(self.root, snapshot=snapshot, grant=grant, grant_sha256=digest, apply=True)
        self.assertEqual(catalog.read_bytes(), b"{}")

    def test_wrong_or_missing_expected_grant_hash_fails_before_write(self):
        snapshot, _, _, grant, _ = self.inputs()
        for value, expected in ((None, "expected_grant_sha256_required"), ("f" * 64, "grant_hash_mismatch")):
            with self.subTest(value=value), self.assertRaisesRegex(release.ReleaseError, expected):
                release.build_candidate(self.root, snapshot=snapshot, grant=grant, grant_sha256=value, apply=True)
        self.assertFalse((self.root / release.OUTPUT).exists())

    def test_grant_path_must_stay_ignored_private_without_traversal(self):
        for value in ("../secret.json", "data/private-research/../secret.json", "public/grant.json"):
            with self.subTest(path=value), self.assertRaises((release.ReleaseError, gallery.ProjectionError)):
                release.build_candidate(self.root, grant=value, grant_sha256="a" * 64, apply=True)

    def test_snapshot_prompt_media_group_and_representative_bindings_are_required(self):
        _, plan, grant, _, _ = self.inputs()
        mutations = [lambda g: g.update(snapshot_id="a" * 64), lambda g: g.update(snapshot_manifest_sha256="a" * 64),
                     lambda g: g["items"][0].update(prompt_sha256="a" * 64), lambda g: g["items"][0].update(prepared_sha256="a" * 64),
                     lambda g: g["items"][0].update(group_id="other"), lambda g: g["items"][0].update(representative_id="asset-1")]
        for mutate in mutations:
            value = copy.deepcopy(grant)
            mutate(value)
            with self.assertRaises(release.ReleaseError):
                release.validate_grant(value, plan, self.root)

    def test_partial_reviewed_group_is_refused_including_representative_only(self):
        _, plan, _, _, _ = self.inputs()
        for ids in ({"asset-0"}, {"asset-1"}, {"asset-0", "asset-2"}):
            with self.subTest(ids=ids), self.assertRaisesRegex(release.ReleaseError, "whole_reviewed_groups"):
                release.validate_grant(self.draft(plan, ids), plan, self.root)

    def test_whole_group_subset_reports_withheld_and_does_not_regroup(self):
        snapshot, plan, _, _, _ = self.inputs()
        grant, digest = self.write_grant(self.draft(plan, {"asset-0", "asset-1"}))
        result = release.build_candidate(self.root, snapshot=snapshot, grant=grant, grant_sha256=digest)
        self.assertEqual(result["counts"], {"images": 2, "groups": 1, "variants": 1, "excluded": 1, "withheld": 1})

    def test_grant_cannot_claim_commercial_or_license_clearance(self):
        _, plan, grant, _, _ = self.inputs()
        for key in ("commercial_rights_approved", "license_verified"):
            value = copy.deepcopy(grant)
            value[key] = True
            with self.assertRaisesRegex(release.ReleaseError, "invalid_reference_grant_contract"):
                release.validate_grant(value, plan, self.root)

    def test_pending_cannot_claim_approval_and_approved_requires_bound_evidence(self):
        _, plan, grant, _, _ = self.inputs()
        value = copy.deepcopy(grant)
        value["approved_by"] = "test reviewer"
        with self.assertRaisesRegex(release.ReleaseError, "must_not_claim_approval"):
            release.validate_grant(value, plan, self.root)
        value.update(decision="approved", approved_at="2026-09-04T15:00:00+09:00")
        with self.assertRaisesRegex(release.ReleaseError, "missing_evidence"):
            release.validate_grant(value, plan, self.root)
        evidence = self.root / "data/private-research/test-human-decision.txt"
        evidence.write_text("SYNTHETIC UNIT TEST ONLY", encoding="utf-8")
        value["decision_evidence"] = {"path": evidence.relative_to(self.root).as_posix(), "sha256": gallery.sha(evidence.read_bytes())}
        selected, bindings = release.validate_grant(value, plan, self.root)
        self.assertEqual(len(selected), 3)
        self.assertEqual(len(bindings), 1)
        evidence.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(release.ReleaseError, "evidence_hash_mismatch"):
            release.validate_grant(value, plan, self.root)

    def test_scope_drift_duplicate_unknown_extra_or_empty_items_fail(self):
        _, plan, grant, _, _ = self.inputs()
        mutations = [lambda g: g["scopes"].append("personal_memos"), lambda g: g.update(items=[]),
                     lambda g: g["items"].append(g["items"][0]), lambda g: g["items"][0].update(item_id="missing"),
                     lambda g: g["items"][0].update(secret="private"), lambda g: g.update(public_eligible=True)]
        for mutate in mutations:
            value = copy.deepcopy(grant)
            mutate(value)
            with self.assertRaises(release.ReleaseError):
                release.validate_grant(value, plan, self.root)

    def test_duplicate_json_keys_and_nonfinite_values_refused(self):
        for raw in (b'{"decision":"approved","decision":"review_pending"}', b'{"items":NaN}'):
            with self.assertRaises(release.ReleaseError):
                release._json(raw)

    def test_explicit_rights_restrictions_are_not_overridden_by_reference_grant(self):
        _, plan, grant, _, _ = self.inputs()
        plan["items"][0]["rights_json"]["status"] = "takedown_requested"
        with self.assertRaisesRegex(release.ReleaseError, "explicit_rights_restriction"):
            release.validate_grant(grant, plan, self.root)

    def test_private_path_in_exact_prompt_fails_instead_of_rewriting(self):
        rows, _, _ = self.fixture()
        rows[0]["original_prompt"] = "C:\\private\\password.txt"
        rows[0]["private_data"]["prompt_sha256"] = gallery.sha(rows[0]["original_prompt"].encode())
        vectors = {r["item_id"]: [1.] + [0.] * 511 for r in rows}
        images = {r["item_id"]: [1.] + [0.] * 1023 for r in rows}
        manifest, bodies = gallery.cloud.assemble(rows, vectors, images, [], {"source_sha256": "a" * 64})
        snapshot = self.save(rows, manifest, bodies)
        plan = gallery.read_snapshot(snapshot, self.root)
        grant, digest = self.write_grant(self.draft(plan))
        with self.assertRaisesRegex(gallery.ProjectionError, "private_text_or_signed_url"):
            release.build_candidate(self.root, snapshot=snapshot, grant=grant, grant_sha256=digest, apply=True)
        self.assertFalse((self.root / release.OUTPUT).exists())

    def test_media_drift_is_caught_before_any_candidate_write(self):
        snapshot, plan, _, grant, digest = self.inputs()
        path = self.root / plan["items"][0]["private_data"]["prepared_relative_path"]
        path.write_bytes(b"different")
        with self.assertRaisesRegex(gallery.ProjectionError, "prepared_media_hash_or_size_drift"):
            release.build_candidate(self.root, snapshot=snapshot, grant=grant, grant_sha256=digest, apply=True)
        self.assertFalse((self.root / release.OUTPUT).exists())

    def test_support_assets_are_strict_and_noindex(self):
        files = release.support_files()
        self.assertEqual(set(files), {"notice.html", "privacy.html", "404.html", "robots.txt", "_headers"})
        self.assertEqual(files["robots.txt"], b"User-agent: *\nDisallow: /\n")
        self.assertIn(b"frame-ancestors 'none'", files["_headers"])
        self.assertNotIn(b"unsafe-inline", files["_headers"])
        self.assertIn(b"immutable", files["_headers"])

    def test_exact_upstream_notice_is_preserved_and_hash_pinned(self):
        result = self.build(apply=True)
        notice = Path(result["assets_path"]) / "THIRD_PARTY_NOTICES.txt"
        self.assertEqual(gallery.sha(notice.read_bytes()), release.NOTICES_SHA256)
        self.assertIn(b"Copyright (c) 2026 freestylefly", notice.read_bytes())
        self.assertIn(b"THIRD_PARTY_NOTICES.txt", (Path(result["assets_path"]) / "notice.html").read_bytes())
        snapshot, _, _, grant, digest = self.inputs()
        (self.root / release.NOTICES_PATH).write_bytes(b"changed copyright")
        with self.assertRaisesRegex(release.ReleaseError, "notice_source_hash_mismatch"):
            release.build_candidate(self.root, snapshot=snapshot, grant=grant, grant_sha256=digest, apply=True)

    def test_windows_long_path_is_blocked_before_creating_directory(self):
        target = self.root / "data/private-research" / ("x" * 150)
        with patch.object(release.os, "name", "nt"):
            with self.assertRaisesRegex(release.ReleaseError, "candidate_path_exceeds_windows_limit"):
                release._write_candidate(target, {"data/groups/" + "a" * 64 + ".json": b"{}"}, b"{}", {})
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
