from __future__ import annotations

import copy
import builtins
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, PngImagePlugin

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("frontend_projection", ROOT / "platform/v2/local/frontend_projection.py")
gallery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gallery)


class FrontendProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        contract_raw = (ROOT / gallery.BROWSE_CONTRACT).read_bytes()
        contract_path = self.root / gallery.BROWSE_CONTRACT
        contract_path.parent.mkdir(parents=True)
        contract_path.write_bytes(contract_raw)
        self.browse_contract = json.loads(contract_raw)
        self.taxonomy = {"narrative.key_art": "서사 핵심 이미지", "commerce.product_hero": "상품 첫인상"}
        path = self.root / gallery.TAXONOMY
        path.parent.mkdir(parents=True)
        path.write_bytes(gallery.encoded({"schema_version": "image-reuse-taxonomy-model-context-1",
                                         "use_cases": [{"use_case_id": k, "label_ko": v} for k, v in self.taxonomy.items()]}))
        shell = self.root / gallery.SHELL
        shell.mkdir(parents=True)
        for name in gallery.SHELL_FILES:
            (shell / name).write_bytes(("<!-- fixture -->" if name.endswith("html") else "/* fixture */").encode())

    def fixture(self, count=3):
        items = []
        for index in range(count):
            ident = f"asset-{index}"
            target = self.root / f"data/private-research/inputs/image-{index}.png"
            target.parent.mkdir(parents=True, exist_ok=True)
            image = Image.new("RGB", (800, 600), (index * 30, index * 20, 120))
            image.save(target, "PNG")
            raw = target.read_bytes()
            prompt = f"  Exact 원문 {index}\r\n--quality 1  "
            effective = {"schema_version": "luna-compact-3", "style_id": "CASE-" + str(index),
                         "visual": {"medium": "illustration", "styles": ["선묘", "선묘"],
                                    "subjects": ["소품"], "background": {"setting": "studio", "detail_ko": "스튜디오 책상"}},
                         "uses": [{"use_case_id": "commerce.product_hero" if index == 1 else "narrative.key_art",
                                   "why_ko": "보이는 대상", "fit": "conditional", "changes": ["문구 교체"], "constraints": ["권리 확인"]}],
                         "prompt": {"purpose_ko": "PRIVATE_PROMPT_ANALYSIS"}, "extras_json": {"secret": "EXTRAS_PRIVATE"}}
            rights = {"source_url": "https://example.com/source?token=SECRET#safe", "source_name": "테스트 출처",
                      "badge": "권리 미확인", "notice_text": "출처 표기는 이용 허가가 아닙니다.",
                      "license_label": "MIT", "attribution_text": "출처: 테스트 출처", "release_eligible": False}
            items.append({"item_id": ident, "group_id": "confirmed-group" if index < 2 else ident,
                          "representative_id": "asset-0" if index < 2 else ident, "original_prompt": prompt,
                          "rights_json": rights, "metadata_json": {"raw": {"secret": "RAW_PRIVATE"}, "effective": effective,
                          "qa": [{"secret": "QA_PRIVATE"}], "review_status": "needs_review", "metadata_human_approved": False, "public_eligible": False},
                          "human_note": "HUMAN_MEMO_PRIVATE", "text_ready": True, "retrieval_text": "RETRIEVAL_PRIVATE",
                          "private_data": {"title": "그림 " + str(index), "style_id": "CASE-" + str(index),
                            "original_sha256": gallery.sha(("original " + str(index)).encode()), "prepared_sha256": gallery.sha(raw),
                            "prompt_sha256": gallery.sha(prompt.encode()), "analysis_effective_sha256": gallery.sha(gallery.encoded(effective, newline=False)),
                            "prepared_relative_path": target.relative_to(self.root).as_posix(), "prepared_bytes": len(raw),
                            "prepared_mime_type": "image/png", "public_eligible": False, "source_record": {"private": "SOURCE_PRIVATE"}}})
        texts = {r["item_id"]: [1.] + [0.] * 511 for r in items}
        images = {r["item_id"]: [1.] + [0.] * 1023 for r in items}
        manifest, bodies = gallery.cloud.assemble(items, texts, images, [], {"source_sha256": "a" * 64})
        return items, manifest, bodies

    def save(self, items=None, manifest=None, bodies=None):
        if items is None:
            items, manifest, bodies = self.fixture()
        target = self.root / "data/private-research/snapshots" / gallery.sha(gallery.encoded(manifest))
        target.mkdir(parents=True, exist_ok=True)
        (target / "manifest.json").write_bytes(gallery.encoded(manifest))
        for name, raw in bodies.items():
            (target / name).write_bytes(raw)
        return target

    def projection(self):
        items, manifest, _ = self.fixture()
        media, _, _, _ = gallery.prepare_media(items, self.root)
        catalog, details = gallery.project_catalog(items, manifest, media, self.taxonomy, mode="private_local_preview")
        return items, manifest, catalog, details

    def test_group_representative_and_all_variants_preserved(self):
        items, _, catalog, details = self.projection()
        self.assertEqual(catalog["counts"], {"images": 3, "groups": 2, "variants": 1, "excluded": 0, "withheld": 0})
        card = next(c for c in catalog["groups"] if c["id"] == "confirmed-group")
        self.assertEqual(card["representative_id"], "asset-0")
        self.assertEqual(card["representative"]["id"], "asset-0")
        self.assertEqual(card["member_count"], 2)
        self.assertEqual([m["id"] for m in card["members"]], ["asset-0", "asset-1"])
        detail = details[card["detail_path"]]
        self.assertEqual(detail["members"][0]["original_prompt"], items[0]["original_prompt"])
        self.assertEqual(card["members"][1]["usage"], ["상품 첫인상"])
        self.assertEqual(card["representative"]["source"]["url"], "https://example.com/source#safe")
        self.assertEqual(card["representative"]["rights"]["badge"], "권리 미확인")
        self.assertNotIn("notice", card["representative"]["rights"])
        self.assertEqual(catalog["browse_taxonomy_version"], 1)
        self.assertEqual(len(catalog["browse_categories"]), 10)
        self.assertEqual(card["representative"]["category_ids"], ["story_scene"])
        self.assertEqual(card["members"][1]["category_ids"], ["commerce_brand"])
        self.assertEqual(card["members"][1]["categories"], ["상품·브랜드"])
        self.assertEqual(detail["members"][0]["category_source"], "legacy_use_case_mapping")

    def test_catalog_no_prompts_details_whitelist_no_private_fields(self):
        _, _, catalog, details = self.projection()
        compact = gallery.encoded(catalog).decode()
        all_output = compact + gallery.encoded(details).decode()
        for forbidden in ("original_prompt", "usage_notes", "human_note", "raw_json", "private_data", "vector"):
            self.assertNotIn('"' + forbidden + '"', compact)
        for secret in ("HUMAN_MEMO_PRIVATE", "RAW_PRIVATE", "QA_PRIVATE", "EXTRAS_PRIVATE", "SOURCE_PRIVATE",
                       "RETRIEVAL_PRIVATE", "PRIVATE_PROMPT_ANALYSIS", "SECRET", "data/private-research", str(self.root)):
            self.assertNotIn(secret, all_output)
        self.assertTrue(all(m["metadata_status"] == "candidate" for d in details.values() for m in d["members"]))

    def test_public_mode_empty_despite_image_and_repository_license(self):
        items, manifest, _ = self.fixture()
        catalog, details = gallery.project_catalog(items, manifest, {}, self.taxonomy)
        self.assertEqual(catalog["mode"], "public")
        self.assertEqual(catalog["status"], "blocked")
        self.assertEqual(catalog["groups"], [])
        self.assertEqual(details, {})
        self.assertEqual(catalog["counts"]["withheld"], 3)
        self.assertIn("no_item_public_release_evidence", catalog["blocked_reason"])

    def test_public_boolean_promotion_is_drift_not_clearance(self):
        for key in ("public_eligible", "release_eligible"):
            items, manifest, _ = self.fixture()
            (items[0]["private_data"] if key == "public_eligible" else items[0]["rights_json"])[key] = True
            with self.assertRaisesRegex(gallery.ProjectionError, "approval_drift"):
                gallery.project_catalog(items, manifest, {}, self.taxonomy)

    def test_existing_human_group_order_independent_and_not_metadata_merged(self):
        items, manifest, _ = self.fixture()
        media, _, _, _ = gallery.prepare_media(items, self.root)
        first = gallery.project_catalog(items, manifest, media, self.taxonomy, mode="private_local_preview")
        second = gallery.project_catalog(list(reversed(items)), manifest, media, self.taxonomy, mode="private_local_preview")
        self.assertEqual(first, second)
        # asset-2 has the same usage/style as asset-0, but remains a separate group.
        self.assertEqual(len(first[0]["groups"]), 2)

    def test_invalid_group_relations_fail_closed(self):
        mutations = (("missing", lambda rows: rows[0].update(representative_id="missing")),
                     ("cross", lambda rows: rows[2].update(representative_id="asset-0")),
                     ("conflicting", lambda rows: rows[1].update(representative_id="asset-1")),
                     ("identifier", lambda rows: rows[0].update(group_id="../../secret")),
                     ("duplicate", lambda rows: rows.append(copy.deepcopy(rows[0]))))
        for name, change in mutations:
            items, manifest, _ = self.fixture()
            change(items)
            with self.subTest(name=name), self.assertRaises(gallery.ProjectionError):
                gallery.validate_items(items, manifest)

    def test_source_hashes_and_media_manifest_are_required(self):
        for field in ("prompt_sha256", "analysis_effective_sha256", "prepared_sha256"):
            items, manifest, _ = self.fixture()
            items[0]["private_data"][field] = "b" * 64
            with self.subTest(field=field), self.assertRaises(gallery.ProjectionError):
                gallery.validate_items(items, manifest)

    def test_remaining_exact_media_duplicates_block_no_automatic_group_changes(self):
        items, manifest, _ = self.fixture()
        items[1]["private_data"]["original_sha256"] = items[0]["private_data"]["original_sha256"]
        with self.assertRaisesRegex(gallery.ProjectionError, "duplicate_media_requires_review"):
            gallery.validate_items(items, manifest)

    def test_metadata_cannot_be_attached_to_wrong_item_or_style(self):
        for key in ("item_id", "style_id"):
            items, manifest, _ = self.fixture()
            effective = items[0]["metadata_json"]["effective"]
            effective[key] = "other"
            items[0]["private_data"]["analysis_effective_sha256"] = gallery.sha(gallery.encoded(effective, newline=False))
            with self.assertRaisesRegex(gallery.ProjectionError, "metadata_item_or_style_identity_mismatch"):
                gallery.validate_items(items, manifest)

    def test_prompt_only_duplicate_is_kept(self):
        items, manifest, _ = self.fixture()
        items[1]["original_prompt"] = items[0]["original_prompt"]
        items[1]["private_data"]["prompt_sha256"] = items[0]["private_data"]["prompt_sha256"]
        self.assertEqual(len(gallery.validate_items(items, manifest)), 2)

    def test_all_supported_metadata_schemas_use_effective_only(self):
        v2 = {"schema_version": "image-luna-reuse-analysis-result-2", "visual": {"styles": ["2D"], "background": {"setting": "indoor"}},
              "usage_selection": {"primary": {"use_case_id": "narrative.key_art", "why_usable_ko": "대비", "adaptation_ko": "교체",
                                             "constraints_ko": ["검증"]}, "secondary": []}}
        v1 = {"schema_version": "image-luna-analysis-result-1", "visual": {"style": ["수묵"], "background": "종이"},
              "reuse_ideas": [{"use_case": "문학 포스터", "visual_reason": "실루엣", "adaptation": "주제 교체", "caution": "권리 확인"}],
              "search_hints": {"keywords_ko": ["수묵 실루엣"], "keywords_en": ["ink silhouette"]}}
        for schema, expected in ((v2, "서사 핵심 이미지"), (v1, "문학 포스터")):
            normalized = gallery.normalize_metadata({"effective": schema, "raw": {"usage": "wrong"}}, self.taxonomy)
            self.assertEqual(normalized["usage"], [expected])
            self.assertTrue(normalized["usage_notes"])
            self.assertEqual(normalized["metadata_status"], "candidate")
        self.assertEqual(gallery.normalize_metadata({"effective": None}, {})["metadata_status"], "none")

    def test_unknown_taxonomy_or_metadata_schema_cannot_be_inferred(self):
        for effective in ({"schema_version": "future"}, {"schema_version": "luna-compact-3", "uses": [{"use_case_id": "made.up"}]}):
            with self.assertRaises(gallery.ProjectionError):
                gallery.normalize_metadata({"effective": effective}, self.taxonomy)

    def test_browse_category_contract_has_eight_purposes_and_explicit_future_limits(self):
        contract = gallery.validate_browse_contract(self.browse_contract)
        self.assertEqual(len(contract["categories"]), 9)
        self.assertEqual(contract["unclassified"]["id"], "unclassified")
        self.assertEqual(contract["future_llm"]["primary"]["count"], 1)
        self.assertEqual(contract["future_llm"]["secondary"]["max_count"], 1)
        self.assertTrue(contract["future_llm"]["evidence"]["required_per_selected_category"])
        self.assertFalse(contract["future_llm"]["execution_authorized_by_this_contract"])

    def test_all_ten_legacy_families_map_to_exact_eight_categories(self):
        expected = {"commerce": "commerce_brand", "brand": "commerce_brand", "content": "content_editorial", "editorial": "content_editorial",
                    "education": "information_education", "character": "character", "narrative": "story_scene", "spatial": "space_place",
                    "service": "web_app", "decorative": "graphic_goods"}
        known = {family + ".fixture" for family in expected}
        for family, category in expected.items():
            result = gallery.browse_category_projection([family + ".fixture"], known, self.browse_contract)
            self.assertEqual(result["category_ids"], [category])
            self.assertEqual(result["category_source"], "legacy_use_case_mapping")

    def test_no_known_id_free_text_and_subjects_remain_unclassified(self):
        effective = {"schema_version": "image-luna-analysis-result-1", "visual": {"styles": ["캐릭터 상품 브랜드"], "subjects": ["상품 캐릭터"]},
                     "reuse_ideas": [{"use_case": "상품 판매 포스터", "visual_reason": "브랜드와 캐릭터", "adaptation": "문구 교체"}],
                     "search_hints": {"categories": ["commerce", "character"]}}
        before = copy.deepcopy(effective)
        result = gallery.normalize_metadata({"effective": effective}, self.taxonomy, self.browse_contract)
        self.assertEqual(result["category_ids"], ["unclassified"])
        self.assertEqual(result["categories"], ["미분류"])
        self.assertEqual(result["category_source"], "unclassified")
        self.assertEqual(result["usage"], ["상품 판매 포스터"])
        self.assertEqual(effective, before)
        none = gallery.normalize_metadata({"effective": None}, self.taxonomy, self.browse_contract)
        self.assertEqual(none["category_ids"], ["unclassified"])

    def test_known_family_prefix_without_known_exact_id_is_not_mapped(self):
        for ids in (["commerce.new_unknown"], ["brand"], ["character.design", "service.unknown"], []):
            mapped = gallery.browse_category_projection(ids, self.taxonomy, self.browse_contract)
            self.assertEqual(mapped["category_ids"], ["unclassified"])

    def test_legacy_mapping_deduplicates_categories_without_truncating_three_purposes(self):
        known = {"commerce.hero": "상품", "brand.identity": "브랜드", "content.cover": "표지", "character.design": "캐릭터"}
        result = gallery.browse_category_projection(list(known), known, self.browse_contract)
        self.assertEqual(result["category_ids"], ["commerce_brand", "content_editorial", "character"])
        self.assertEqual(result["categories"], ["상품·브랜드", "콘텐츠·출판", "캐릭터"])
        self.assertNotIn("unclassified", result["category_ids"])

    def test_malformed_or_semantically_changed_v1_browse_contract_fails_closed(self):
        for mutation in (lambda c: c.update(version=2), lambda c: c.update(version=True),
                         lambda c: c["categories"].pop(), lambda c: c["categories"][0].update(id="other"),
                         lambda c: c["categories"][0].update(legacy_use_case_families=["narrative"]),
                         lambda c: c["future_llm"]["secondary"].update(max_count=3),
                         lambda c: c["future_llm"].update(execution_authorized_by_this_contract=True)):
            document = copy.deepcopy(self.browse_contract)
            mutation(document)
            with self.assertRaises(gallery.ProjectionError):
                gallery.validate_browse_contract(document)

    def test_unsafe_source_urls_removed_without_mutating_original(self):
        for url in ("javascript:alert(1)", "file:///D:/secret", "http://127.0.0.1/a", "https://user:pass@example.com/a", "https://localhost/a"):
            items, manifest, _ = self.fixture()
            items[0]["rights_json"]["source_url"] = url
            media, _, _, _ = gallery.prepare_media(items, self.root)
            _, details = gallery.project_catalog(items, manifest, media, self.taxonomy, mode="private_local_preview")
            selected = next(m for d in details.values() for m in d["members"] if m["id"] == "asset-0")
            self.assertIsNone(selected["source"]["url"])
            self.assertEqual(items[0]["rights_json"]["source_url"], url)

    def test_path_traversal_alternate_stream_and_absolute_media_refused(self):
        unsafe = ("../other.png", "data/private-research/../secret.png", "D:/secret.png", "/secret.png",
                  "data\\private-research\\a.png", "data/private-research/a.png:secret", "data/private-research/%2e%2e/a.png",
                  "data/private-research//a.png", "data/private-research/a.png?token=1", "data/private-research/./a.png")
        for relative in unsafe:
            with self.subTest(relative=relative), self.assertRaises(gallery.ProjectionError):
                gallery.local_path(self.root, relative, private=True)

    def test_symlink_directory_refused(self):
        target = self.root / "data/private-research/link"
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda path: path == target or original(path)):
            with self.assertRaisesRegex(gallery.ProjectionError, "symlink"):
                gallery.local_path(self.root, "data/private-research/link/image.png", private=True)

    def test_thumbnail_dimensions_metadata_free_fallback_and_content_hashes(self):
        items, _, _ = self.fixture()
        original = {r["private_data"]["prepared_relative_path"]: (self.root / r["private_data"]["prepared_relative_path"]).read_bytes() for r in items}
        mapped, files, _, _ = gallery.prepare_media(items, self.root)
        self.assertEqual(len(files), 6)
        for output, raw in files.items():
            self.assertEqual(Path(output).stem, gallery.sha(raw))
            with Image.open(io.BytesIO(raw)) as image:
                if output.endswith("webp"):
                    self.assertLessEqual(max(image.size), 640)
                self.assertNotIn("exif", image.info)
        for entry in mapped.values():
            self.assertTrue(entry["image"]["src"].endswith(".jpg"))
            self.assertEqual(entry["thumbnail"]["src"], entry["image"]["src"])
        for relative, body in original.items():
            self.assertEqual((self.root / relative).read_bytes(), body)

    def test_media_bytes_drift_blocks_before_writes(self):
        path = self.save()
        source = self.root / "data/private-research/inputs/image-2.png"
        source.write_bytes(b"not an image")
        with self.assertRaisesRegex(gallery.ProjectionError, "media_hash_or_size_drift"):
            gallery.build_bundle(self.root, snapshot=path, mode="private_local_preview", apply=True)
        self.assertFalse((self.root / gallery.OUTPUT).exists())

    def test_alpha_fallback_preserves_alpha_but_strips_embedded_metadata(self):
        items, _, _ = self.fixture()
        target = self.root / items[0]["private_data"]["prepared_relative_path"]
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("secret", "PRIVATE_EMBEDDED_TEXT")
        image = Image.new("RGBA", (80, 60), (120, 40, 80, 110))
        exif = Image.Exif()
        exif[270] = "PRIVATE_EXIF_DESCRIPTION"
        image.save(target, "PNG", pnginfo=metadata, exif=exif)
        raw = target.read_bytes()
        items[0]["private_data"].update(prepared_sha256=gallery.sha(raw), prepared_bytes=len(raw))
        mapped, files, _, _ = gallery.prepare_media(items[:1], self.root)
        self.assertTrue(mapped["asset-0"]["image"]["src"].endswith(".png"))
        for body in files.values():
            self.assertNotIn(b"PRIVATE_EMBEDDED_TEXT", body)
            self.assertNotIn(b"PRIVATE_EXIF_DESCRIPTION", body)
            with Image.open(io.BytesIO(body)) as decoded:
                self.assertIn("A", decoded.mode)
                self.assertFalse(decoded.getexif())

    def test_missing_pillow_or_webp_fail_without_install_or_write(self):
        items, _, _ = self.fixture()
        original_import = builtins.__import__
        def missing(name, *args, **kwargs):
            if name == "PIL":
                raise ImportError("fixture unavailable")
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", missing), self.assertRaisesRegex(gallery.ProjectionError, "pillow_missing_no_install"):
            gallery.prepare_media(items, self.root)
        with patch("PIL.features.check", return_value=False), self.assertRaisesRegex(gallery.ProjectionError, "webp_unavailable"):
            gallery.prepare_media(items, self.root)
        self.assertFalse((self.root / gallery.OUTPUT).exists())

    def test_snapshot_tamper_blocks_before_writes(self):
        path = self.save()
        (path / "items.jsonl").write_bytes(b"{}\n")
        with self.assertRaisesRegex(gallery.ProjectionError, "file_drift"):
            gallery.build_bundle(self.root, snapshot=path, mode="private_local_preview", apply=True)
        self.assertFalse((self.root / gallery.OUTPUT).exists())

    def test_dry_run_no_files_no_credentials_or_network(self):
        path = self.save()
        before = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*"))
        with patch.object(gallery.cloud, "credentials", side_effect=AssertionError("no credentials")), patch("urllib.request.urlopen", side_effect=AssertionError("no network")):
            result = gallery.build_bundle(self.root, snapshot=path, mode="private_local_preview")
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["network_calls"], 0)
        self.assertEqual(result["new_embedding_calls"], 0)
        self.assertEqual(before, sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*")))

    def test_public_apply_contains_only_empty_catalog_and_shell(self):
        result = gallery.build_bundle(self.root, snapshot=self.save(), apply=True)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["media_files"], 0)
        target = Path(result["path"])
        receipt = json.loads((target / "build-receipt.json").read_bytes())
        self.assertEqual(set(receipt["served_files"]), set(gallery.SHELL_FILES) | {"data/catalog.json"})
        self.assertEqual(receipt["counts"]["withheld"], 3)

    def test_immutable_repeat_and_conflict_receipt_not_served(self):
        path = self.save()
        result = gallery.build_bundle(self.root, snapshot=path, mode="private_local_preview", apply=True)
        second = gallery.build_bundle(self.root, snapshot=path, mode="private_local_preview", apply=True)
        self.assertEqual(result, second)
        target = Path(result["path"])
        receipt = json.loads((target / "build-receipt.json").read_bytes())
        self.assertNotIn("build-receipt.json", receipt["served_files"])
        self.assertEqual(receipt["identity"]["browse_taxonomy_sources"],
                         {gallery.BROWSE_CONTRACT: gallery.sha((self.root / gallery.BROWSE_CONTRACT).read_bytes())})
        self.assertEqual(receipt["category_source_counts"], {"legacy_use_case_mapping": 3})
        self.assertEqual(receipt["browse_category_coverage"]["story_scene"], 2)
        self.assertEqual(receipt["browse_category_coverage"]["commerce_brand"], 1)
        self.assertEqual(receipt["browse_category_coverage"]["unclassified"], 0)
        for relative, digest in receipt["served_files"].items():
            self.assertEqual(gallery.sha((target / relative).read_bytes()), digest)
        self.assertEqual(gallery.sha(gallery.encoded(receipt["identity"])), target.name)
        (target / "index.html").write_text("other writer")
        with self.assertRaisesRegex(gallery.ProjectionError, "immutable_bundle_conflict"):
            gallery.build_bundle(self.root, snapshot=path, mode="private_local_preview", apply=True)
        self.assertEqual((target / "index.html").read_text(), "other writer")

    def test_partial_write_has_no_completion_receipt_and_retry_refuses(self):
        path = self.save()
        original_open = Path.open
        def fail_second(file, *args, **kwargs):
            if file.name == "gallery.css" and args and args[0] == "xb":
                raise OSError("interrupted")
            return original_open(file, *args, **kwargs)
        with patch.object(Path, "open", fail_second), self.assertRaises(OSError):
            gallery.build_bundle(self.root, snapshot=path, mode="private_local_preview", apply=True)
        bundles = list((self.root / gallery.OUTPUT).iterdir())
        self.assertEqual(len(bundles), 1)
        self.assertFalse((bundles[0] / "build-receipt.json").exists())
        with self.assertRaisesRegex(gallery.ProjectionError, "immutable_bundle_conflict"):
            gallery.build_bundle(self.root, snapshot=path, mode="private_local_preview", apply=True)

    def test_missing_shell_and_invalid_mode_no_write(self):
        path = self.save()
        for mode in ("publish", "override_rights"):
            with self.assertRaisesRegex(gallery.ProjectionError, "invalid_projection_mode"):
                gallery.build_bundle(self.root, snapshot=path, mode=mode, apply=True)
        (self.root / gallery.SHELL / "index.html").unlink()
        with self.assertRaisesRegex(gallery.ProjectionError, "shell_missing"):
            gallery.build_bundle(self.root, snapshot=path, mode="private_local_preview", apply=True)
        self.assertFalse((self.root / gallery.OUTPUT).exists())

    def test_private_paths_or_signed_urls_inside_exact_prompt_block_not_rewrite(self):
        for prompt in ("file:///D:/secret.png", "C:\\private\\image.png", "https://example.com/image?X-Amz-Signature=private",
                       "https://example.com/image?token=private", "https://user:password@example.com/image"):
            with self.subTest(prompt=prompt), self.assertRaisesRegex(gallery.ProjectionError, "private_text_or_signed_url"):
                gallery._assert_no_private_text({"data/detail.json": gallery.encoded({"original_prompt": prompt})})

    def test_normal_public_source_urls_are_not_drive_paths(self):
        gallery._assert_no_private_text({"data/detail.json": gallery.encoded({"source": {"url": "https://example.com/source#safe"},
                                                                             "original_prompt": "Reference http://example.com/image"})})

    def test_default_cli_is_public_dry_run(self):
        with patch.object(gallery, "build_bundle", return_value={"status": "dry_run"}) as build, patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(gallery.main([]), 0)
        self.assertEqual(build.call_args.kwargs["mode"], "public")
        self.assertFalse(build.call_args.kwargs["apply"])


if __name__ == "__main__":
    unittest.main()
