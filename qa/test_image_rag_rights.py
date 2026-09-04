"""Offline rights-notice contracts; all fixture writes remain in temporary roots."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ARCHIVE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ARCHIVE_ROOT / "src"))

from build_duplicate_index import BuildConfig
from image_rag_eval.dataset import _manifest_item
from image_rag_eval.experiment import digest, json_bytes, run_path
from image_rag_eval.expansion import _prepare_item
from image_rag_eval.rights import BAD_LICENSES, build_rights_catalog, normalize_image_rights, safe_source_url


IMAGE_SHA = "a" * 64


def image_proof(**changes):
    result = {"schema_version": "image-license-evidence-1", "scope": "image", "verification_status": "verified",
              "human_verified": True, "image_sha256": IMAGE_SHA, "evidence_sha256": "b" * 64,
              "reviewed_by": "fixture-human", "reviewed_at": "2026-09-03T00:00:00Z",
              "license_label": "CC-BY-4.0", "evidence_url": "https://example.com/individual-image-license",
              "creator_name": "Explicit image creator"}
    result.update(changes)
    return result


class RightsNormalizerTests(unittest.TestCase):
    def test_missing_data_always_warns_without_permission(self):
        for record in ({}, None, [], {"rights": None, "source": None}):
            with self.subTest(record=record):
                result = normalize_image_rights(record)
                self.assertEqual(result["status"], "unverified")
                self.assertEqual(result["source_name"], "출처 미확인")
                self.assertIsNone(result["source_url"])
                self.assertIsNone(result["creator_name"])
                self.assertEqual(result["license_label"], "라이선스 미확인")
                self.assertFalse(result["image_license_verified"])
                self.assertFalse(result["release_eligible"])
                self.assertIn("출처 표기는 이용 허락을 대신하지 않습니다", result["notice_text"])

    def test_source_repository_uploader_and_title_are_not_creator_evidence(self):
        record = {"source": {"name": "@repository-uploader", "url": "https://github.com/uploader/repo"},
                  "title": "Example (author @someone)", "author": "untyped author", "uploader": "uploader",
                  "provenance": {"raw_source": {"author_name": "not-explicit-image-creator"}}}
        before = copy.deepcopy(record)
        result = normalize_image_rights(record)
        self.assertEqual(result["source_name"], "@repository-uploader")
        self.assertIsNone(result["creator_name"])
        self.assertIn("개별 이미지 제작자 미확인", result["attribution_text"])
        self.assertEqual(record, before)

    def test_explicit_creator_is_attribution_not_permission(self):
        result = normalize_image_rights({"source": {"creator_name": "Explicit creator"}})
        self.assertEqual(result["creator_name"], "Explicit creator")
        self.assertEqual(result["status"], "unverified")

    def test_repository_mit_retains_both_copyright_and_permission_notice_warning(self):
        result = normalize_image_rights({"source": {"name": "CASE source", "repository": "https://github.com/owner/repo"},
            "license": {"reported_spdx": "MIT", "status": "repository_code_only",
                        "scope": "repository code; third-party case prompts and images are separate",
                        "evidence_url": "https://github.com/owner/repo/blob/main/LICENSE"}})
        self.assertEqual(result["license_label"], "MIT")
        self.assertEqual(result["license_scope"], "repository_only")
        self.assertEqual(result["status"], "unverified")
        self.assertIn("저장소 라이선스는 제3자 이미지·프롬프트의 이용 허락을 보장하지 않습니다", result["notice_text"])
        self.assertIn("원문의 저작권 고지와 허가 고지를 함께 유지", result["notice_text"])
        self.assertIn("단순 출처 표기로 대체할 수 없습니다", result["notice_text"])

    def test_generic_cleared_release_and_license_labels_never_verify(self):
        for label in ("MIT", "CC-BY-4.0", "CC0-1.0", "Public domain"):
            result = normalize_image_rights({"license": {"effective_spdx": label, "scope": "image"},
                "rights": {"status": "cleared", "explicitly_cleared": True, "release_eligible": True,
                           "commercial_reuse_claimed": True}, "review_status": "approved"})
            self.assertFalse(result["image_license_verified"])
            self.assertFalse(result["release_eligible"])
            self.assertEqual(result["status"], "unverified")

    def test_qualified_individual_proof_is_recorded_but_never_grants_release(self):
        result = normalize_image_rights({"sha256": IMAGE_SHA, "image_rights_evidence": image_proof()})
        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["image_license_verified"])
        self.assertEqual(result["license_scope"], "image")
        self.assertEqual(result["license_label"], "CC-BY-4.0")
        self.assertEqual(result["creator_name"], "Explicit image creator")
        self.assertFalse(result["release_eligible"])
        self.assertEqual(result["evidence_urls"], ["https://example.com/individual-image-license"])

    def test_unknown_or_restrictive_label_never_becomes_verified_evidence(self):
        for label in BAD_LICENSES | {"UNVERIFIED", "Unknown", "UNLICENSED", "all rights reserved", "all_rights_reserved"}:
            with self.subTest(label=label):
                result = normalize_image_rights({"sha256": IMAGE_SHA, "image_rights_evidence": image_proof(license_label=label)})
                self.assertFalse(result["image_license_verified"])
                self.assertEqual(result["status"], "unverified")

    def test_proof_requires_exact_image_and_complete_human_evidence(self):
        cases = [{key: None} for key in image_proof() if key != "creator_name"]
        cases += [{"human_verified": 1}, {"scope": "repository"}, {"image_sha256": "c" * 64},
                  {"evidence_sha256": "not-a-sha"}, {"reviewed_at": "2026-09-03T00:00:00"},
                  {"reviewed_at": "invalid"}, {"evidence_url": "https://example.com/evidence?signature=secret"},
                  {"evidence_url": "javascript:alert(1)"}]
        for changes in cases:
            with self.subTest(changes=changes):
                result = normalize_image_rights({"sha256": IMAGE_SHA, "image_rights_evidence": image_proof(**changes)})
                self.assertFalse(result["image_license_verified"])
                self.assertFalse(result["release_eligible"])

    def test_source_sha_and_selected_asset_binding_take_precedence(self):
        proof = image_proof()
        self.assertFalse(normalize_image_rights({"source_sha256": "c" * 64, "sha256": IMAGE_SHA,
                                                "image_rights_evidence": proof})["image_license_verified"])
        source = {"media": {"assets": [{"sha256": "c" * 64}, {"sha256": IMAGE_SHA}]}, "asset_index": 1,
                  "rights": {"individual_image_evidence": proof}}
        self.assertTrue(normalize_image_rights(source)["image_license_verified"])
        source["asset_index"] = 0
        self.assertFalse(normalize_image_rights(source)["image_license_verified"])

    def test_restriction_takes_precedence_over_qualified_proof(self):
        for fields in ({"rights_status": "takedown_requested"}, {"rights": {"rights_tier": "P4"}},
                       {"rights": {"status": "restricted"}}, {"license_label": "UNLICENSED"},
                       {"rights_display": {"status": "restricted"}}):
            result = normalize_image_rights({**fields, "sha256": IMAGE_SHA, "image_rights_evidence": image_proof()})
            self.assertEqual(result["status"], "restricted")
            self.assertFalse(result["image_license_verified"])
            self.assertFalse(result["release_eligible"])

    def test_previously_rendered_notice_cannot_self_certify(self):
        prior = normalize_image_rights({"sha256": IMAGE_SHA, "image_rights_evidence": image_proof()})
        result = normalize_image_rights({"sha256": IMAGE_SHA, "rights_display": prior})
        self.assertEqual(result["status"], "unverified")
        self.assertFalse(result["image_license_verified"])

    def test_text_controls_are_normalized_and_lengths_bounded(self):
        result = normalize_image_rights({"source_name": "A\x00\nB" + "x" * 600, "creator_name": "C" * 600})
        self.assertLessEqual(len(result["source_name"]), 300)
        self.assertLessEqual(len(result["creator_name"]), 300)
        self.assertNotIn("\x00", result["source_name"])
        self.assertNotIn("\n", result["source_name"])

    def test_schema_accepts_each_status_and_rejects_release_or_inconsistent_verification(self):
        import jsonschema
        schema = json.loads((ARCHIVE_ROOT.parent / "00_CORE/schemas/image_rights_notice.schema.json").read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        records = [{}, {"rights_status": "restricted"}, {"sha256": IMAGE_SHA, "image_rights_evidence": image_proof()}]
        for record in records:
            result = normalize_image_rights(record)
            jsonschema.validate(result, schema)
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate({**result, "release_eligible": True}, schema)
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate({**result, "image_license_verified": not result["image_license_verified"]}, schema)


class RightsUrlTests(unittest.TestCase):
    def test_safe_source_urls_remove_signed_query_and_only_keep_safe_fragment(self):
        self.assertEqual(safe_source_url("https://example.com/image?X-Amz-Signature=secret#case-12"), "https://example.com/image#case-12")
        self.assertEqual(safe_source_url("https://example.com/image#access_token=secret"), "https://example.com/image")
        self.assertEqual(safe_source_url("http://example.com:8080/source"), "http://example.com:8080/source")

    def test_unsafe_schemes_credentials_framing_and_private_destinations_are_rejected(self):
        urls = [None, "", "file:///C:/secret", "data:text/html,test", "javascript:alert(1)", "//example.com/image",
                "https://user:password@example.com/image", "https://user@example.com/image", "https://example.com\\@localhost",
                "https://example.com/\nimage", "https://example.com:bad/", "https://example.com:0/", "https://example.com:65536/",
                "http://localhost/x", "http://localhost./x", "http://test.local/x", "http://test.internal/x", "http://test.localhost/x",
                "http://127.0.0.1/x", "http://10.0.0.1/x", "http://192.168.1.2/x", "http://169.254.169.254/x", "http://[::1]/x",
                "http://127.1/x", "http://0177.0.0.1/x", "http://0x7f.0.0.1/x", "http://2130706433/x", "http://0x7f000001/x",
                "http://127%2e0%2e0%2e1/x", "http://-invalid.example.com/x"]
        for url in urls:
            with self.subTest(url=url):
                self.assertIsNone(safe_source_url(url))


class RightsCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.spec = {"run_id": "fixture-review", "items": [{"id": "asset-a", "style_id": "CASE-001", "source_sha256": IMAGE_SHA},
                                                             {"id": "asset-b", "style_id": "CASE-002", "source_sha256": "c" * 64}]}
        self.spec["spec_sha256"] = digest(json_bytes(self.spec))
        self.directory = run_path(self.root, "fixture-review") / "group-workflow-v1"
        self.directory.mkdir(parents=True)
        self.manifest = {"items": [{"id": "asset-a", "style_id": "CASE-001", "sha256": IMAGE_SHA,
                                    "catalog_key": "fixture:a", "source_name": "Manifest source", "rights_status": "needs_review"},
                                   {"id": "asset-b", "style_id": "CASE-002", "sha256": "c" * 64,
                                    "catalog_key": "fixture:b", "source_name": "Manifest B", "rights_status": "needs_review"}]}
        self.manifest_path = run_path(self.root, "fixture-source") / "manifest.json"
        self.manifest_path.parent.mkdir(parents=True)
        self.manifest_path.write_bytes(json_bytes(self.manifest))
        self.canonical_path = self.root / "data/canonical/archive_records.jsonl"
        self.canonical_path.parent.mkdir(parents=True)
        self.canonical = [{"catalog_key": "fixture:a", "style_id": "CASE-001", "source": {"name": "Canonical source", "url": "https://example.com/source"},
                           "license": {"reported_spdx": "MIT", "scope": "repository code"}},
                          {"catalog_key": "fixture:b", "style_id": "CASE-002", "source": {"name": "Canonical B", "url": "https://example.com/source-b"},
                           "creator_name": "Explicit creator B"}]
        self.canonical_path.write_text("".join(json.dumps(x) + "\n" for x in self.canonical), encoding="utf-8")
        self.binding = {"review_spec_sha256": self.spec["spec_sha256"], "files": [self.file_binding(self.manifest_path), self.file_binding(self.canonical_path)]}
        self.freeze_binding()

    def file_binding(self, path):
        return {"path": path.relative_to(self.root).as_posix(), "sha256": digest(path.read_bytes())}

    def freeze_binding(self):
        (self.directory / "source-bindings.json").write_bytes(json_bytes(self.binding))
        (self.directory / "build-receipt.json").write_bytes(json_bytes({"run_id": self.spec["run_id"], "status": "ready",
            "spec_sha256": self.spec["spec_sha256"], "binding_sha256": digest(json_bytes(self.binding))}))

    def test_hash_bound_catalog_enrichment_is_pure_and_exact(self):
        before = copy.deepcopy(self.spec)
        file_shas = {str(p): digest(p.read_bytes()) for p in self.root.rglob("*") if p.is_file()}
        result = build_rights_catalog(self.root, self.spec)
        self.assertEqual(set(result), {"asset-a", "asset-b"})
        self.assertEqual(result["asset-a"]["source_name"], "Canonical source")
        self.assertEqual(result["asset-a"]["source_url"], "https://example.com/source")
        self.assertEqual(result["asset-a"]["license_scope"], "repository_only")
        self.assertEqual(result["asset-b"]["creator_name"], "Explicit creator B")
        self.assertEqual(self.spec, before)
        self.assertEqual(file_shas, {str(p): digest(p.read_bytes()) for p in self.root.rglob("*") if p.is_file()})

    def test_no_bindings_retains_all_unknown_images(self):
        result = build_rights_catalog(self.root, {"items": [{"id": "unknown"}]})
        self.assertEqual(result["unknown"]["status"], "unverified")
        self.assertIsNone(result["unknown"]["source_url"])

    def test_canonical_source_drift_does_not_reuse_unbound_attribution_or_proof(self):
        self.canonical_path.write_text(json.dumps({"catalog_key": "fixture:a", "style_id": "CASE-001", "source": {"name": "Tampered"},
            "image_rights_evidence": image_proof()}) + "\n", encoding="utf-8")
        result = build_rights_catalog(self.root, self.spec)
        self.assertEqual(result["asset-a"]["source_name"], "Manifest source")
        self.assertIsNone(result["asset-a"]["source_url"])
        self.assertEqual(result["asset-a"]["status"], "unverified")

    def test_manifest_source_drift_falls_back_to_thin_unknown_items(self):
        self.manifest_path.write_bytes(b'{"items": []}')
        result = build_rights_catalog(self.root, self.spec)
        self.assertEqual(result["asset-a"]["source_name"], "출처 미확인")
        self.assertIsNone(result["asset-a"]["source_url"])

    def test_different_id_style_or_original_hash_never_maps_canonical_rights(self):
        for key, value in (("id", "wrong"), ("style_id", "CASE-999"), ("sha256", "d" * 64)):
            with self.subTest(key=key):
                changed = copy.deepcopy(self.manifest)
                changed["items"][0][key] = value
                self.manifest_path.write_bytes(json_bytes(changed))
                self.binding["files"][0] = self.file_binding(self.manifest_path)
                self.freeze_binding()
                result = build_rights_catalog(self.root, self.spec)
                self.assertEqual(result["asset-a"]["source_name"], "출처 미확인")

    def test_tampered_spec_binding_or_missing_receipt_fails_closed(self):
        changed = copy.deepcopy(self.spec)
        changed["items"][0]["style_id"] = "tampered"
        with self.assertRaisesRegex(ValueError, "identity"):
            build_rights_catalog(self.root, changed)
        changed_binding = copy.deepcopy(self.binding)
        changed_binding["files"][0]["sha256"] = "d" * 64
        (self.directory / "source-bindings.json").write_bytes(json_bytes(changed_binding))
        with self.assertRaisesRegex(ValueError, "identity"):
            build_rights_catalog(self.root, self.spec)
        self.freeze_binding()
        (self.directory / "build-receipt.json").unlink()
        with self.assertRaisesRegex(ValueError, "identity"):
            build_rights_catalog(self.root, self.spec)

    def test_duplicate_item_and_source_bindings_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "unique"):
            build_rights_catalog(self.root, {"items": [{"id": "duplicate"}, {"id": "duplicate"}]})
        self.binding["files"].append(self.binding["files"][0])
        self.freeze_binding()
        with self.assertRaisesRegex(ValueError, "file binding"):
            build_rights_catalog(self.root, self.spec)

    def test_ambiguous_catalog_mapping_fails_closed(self):
        other = run_path(self.root, "fixture-other") / "manifest.json"
        other.parent.mkdir(parents=True)
        changed = copy.deepcopy(self.manifest)
        changed["items"][0]["catalog_key"] = "other:a"
        other.write_bytes(json_bytes(changed))
        self.binding["files"].append(self.file_binding(other))
        self.freeze_binding()
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            build_rights_catalog(self.root, self.spec)

    def test_missing_original_hash_never_matches_an_unhashed_manifest_row(self):
        self.spec["items"][0].pop("source_sha256")
        self.manifest["items"][0].pop("sha256")
        self.spec["spec_sha256"] = digest(json_bytes({key: value for key, value in self.spec.items() if key != "spec_sha256"}))
        self.manifest_path.write_bytes(json_bytes(self.manifest))
        self.binding["review_spec_sha256"] = self.spec["spec_sha256"]
        self.binding["files"][0] = self.file_binding(self.manifest_path)
        self.freeze_binding()
        self.assertEqual(build_rights_catalog(self.root, self.spec)["asset-a"]["source_name"], "출처 미확인")


class RightsIntakeTests(unittest.TestCase):
    def test_new_manifest_and_prepared_image_carry_notice_without_mutating_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            legacy = root / "legacy/current_archive"
            original = legacy / "images/source.png"
            original.parent.mkdir(parents=True)
            Image.new("RGB", (24, 24), (11, 33, 55)).save(original)
            sha = digest(original.read_bytes())
            raw = {"source": {"name": "Fixture source", "url": "https://example.com/source?signature=do-not-copy"},
                   "license": {"reported_spdx": "MIT", "scope": "repository code"}, "prompt": {"text": "Fixture prompt"},
                   "media": {"assets": [{"uri_kind": "local", "uri": "images/source.png", "sha256": sha}]}}
            candidate = {"asset_index": 0, "asset_id": "asset-a", "style_id": "CASE-001", "sha256": sha,
                         "review_status": "needs_review", "rights_status": "needs_review", "catalog_key": "fixture:a",
                         "record_id": "fixture:a", "lane": "fixture", "source_name": "Fixture source",
                         "source_url": raw["source"]["url"], "title": "Fixture"}
            before = copy.deepcopy(raw)
            config = BuildConfig(platform_root=root, canonical_path=root / "data/canonical/archive_records.jsonl", legacy_root=legacy,
                                 output_dir=root / "data/private-research/duplicate-analysis/current", thumbnail_root=root / "media/derived")
            item = _manifest_item(root=root, config=config, candidate=candidate, raw_record=raw, overlay={})
            self.assertIsNotNone(item)
            self.assertEqual(item["rights_display"]["status"], "unverified")
            self.assertEqual(item["rights_display"]["source_url"], "https://example.com/source")
            self.assertNotIn("do-not-copy", json.dumps(item))
            prepared, content = _prepare_item(root, item)
            self.assertEqual(prepared["rights_display"], item["rights_display"])
            self.assertGreater(len(content), 0)
            self.assertFalse(prepared["rights_display"]["release_eligible"])
            self.assertEqual(raw, before)
            self.assertEqual(digest(original.read_bytes()), sha)


if __name__ == "__main__":
    unittest.main()
