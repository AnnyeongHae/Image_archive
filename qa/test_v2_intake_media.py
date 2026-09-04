from __future__ import annotations

import copy
import hashlib
import io
import os
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from github_sources.intake_envelope import POLICY, content_identity
from image_rag_eval import intake_media as media


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class V2IntakeMediaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.prompt = "  Exact 한글 prompt.\r\nSecond line.  \r\n"
        self.raw = self.save_image("media/red.png", "red")
        self.record = self.make_record([self.raw])
        self.bundle = {"schema_version": "archive-sealed-intake-bundle-1", "records": [self.record]}
        self.binding = self.make_binding(self.record, 0, "media/red.png", self.raw)

    def save_image(self, relative, color="red", *, size=(4, 4), mode="RGB", format="PNG"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        output = io.BytesIO()
        Image.new(mode, size, color).save(output, format=format)
        raw = output.getvalue()
        path.write_bytes(raw)
        return raw

    def make_record(self, images, *, item_id="docs/gallery-part-1.md#case-1", prompt=None):
        prompt = self.prompt if prompt is None else prompt
        record = {"schema_version": "archive-local-intake-1", "source_id": "github-fixture",
            "source_item_id": item_id, "source_url": "https://github.com/test/repo/blob/" + "1" * 40 + "/" + item_id,
            "title": "Synthetic example", "observed_at": "2026-09-04T00:00:00Z",
            "source_version": {"repository": "test/repo", "repository_commit_sha": "1" * 40,
                "repository_tree_sha": "2" * 40, "git_blob_sha1": "3" * 40, "adapter_version": "fixture-1"},
            "original_prompt": {"text": prompt, "sha256": sha(prompt.encode()), "status": "exact_source_fence"},
            "media_refs": [{"path": f"data/images/example-{index}.png",
                "git_blob_sha1": hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest(),
                "url": "https://raw.githubusercontent.com/test/repo/" + "1" * 40 + f"/data/images/example-{index}.png",
                "binary_downloaded": False, "rights_status": "unknown"} for index, raw in enumerate(images)],
            "deferred_media": [], "rights": dict(POLICY)}
        record["content_sha256"] = content_identity(record)
        return record

    def make_binding(self, record, index, path, raw):
        return {"source_id": record["source_id"], "source_item_id": record["source_item_id"],
                "media_index": index, "local_path": path, "sha256": sha(raw)}

    def prepare(self, bindings=None, bundle=None):
        return media.prepare_assets(self.root, self.bundle if bundle is None else bundle,
                                    [self.binding] if bindings is None else bindings)

    def test_exact_prompt_source_record_and_fail_closed_rights_are_preserved(self):
        original = copy.deepcopy(self.bundle)
        files_before = sorted(str(path) for path in self.root.rglob("*"))
        result = self.prepare()
        item = result["items"][0]
        self.assertEqual(item["prompt"], self.prompt)
        self.assertEqual(item["source_record"], self.record)
        self.assertIsNot(item["source_record"], self.record)
        self.assertEqual(item["intake_media_ref"], self.record["media_refs"][0])
        self.assertEqual(item["style_id"], "CASE-001")
        self.assertEqual(item["sha256"], sha(self.raw))
        self.assertEqual(item["asset_index"], 0)
        self.assertEqual(item["lane"], "v2_intake")
        self.assertEqual(item["rights_status"], "unknown")
        self.assertEqual(item["rights_display"]["status"], "unverified")
        for key in ("external_ai_approved", "image_approved", "metadata_human_approved", "release_eligible"):
            self.assertIs(item[key], False)
        preview = result["previews"][item["prepared_sha256"]]
        self.assertEqual(sha(preview), item["prepared_sha256"])
        self.assertEqual(item["prepared_path"], "inputs/" + sha(preview) + ".png")
        self.assertEqual(self.bundle, original)
        self.assertEqual(files_before, sorted(str(path) for path in self.root.rglob("*")))

    def test_every_decode_uses_same_verified_buffer_including_prepared_image(self):
        real_open = Image.open
        decoded = []
        def checked_open(value, *args, **kwargs):
            self.assertIsInstance(value, io.BytesIO)
            decoded.append(value.getvalue())
            return real_open(value, *args, **kwargs)
        with patch.object(Image, "open", side_effect=checked_open):
            item = self.prepare()["items"][0]
        self.assertEqual(decoded, [self.raw, self.raw])
        expected_pixels = Image.new("RGBA", (4, 4), "red").tobytes()
        self.assertEqual(item["signals"]["pixel_sha256"],
            sha(b"rgba-exif-v2\0" + struct.pack(">II", 4, 4) + expected_pixels))
        self.assertEqual(item["signals"]["pixel_policy"], "rgba-exif-v2")

    def test_selected_subset_counts_empty_and_multi_image_records(self):
        blue = self.save_image("media/blue.png", "blue")
        many = self.make_record([self.raw, blue])
        empty = self.make_record([], item_id="docs/gallery-part-1.md#case-2")
        unselected = self.make_record([blue], item_id="docs/gallery-part-1.md#case-3")
        bundle = {**self.bundle, "records": [many, empty, unselected]}
        result = self.prepare([self.make_binding(many, 1, "media/blue.png", blue)], bundle)
        expected = {"bundle_records": 3, "total_declared_media": 3, "selected_media": 1,
            "deferred_media": 2, "selected_records": 1, "unselected_records": 2,
            "records_without_media": 1, "multi_image_records": 1}
        for key, value in expected.items():
            self.assertEqual(result["selection"][key], value, key)
        self.assertEqual(len(result["selection"]["unselected_record_keys"]), 2)
        self.assertEqual(result["items"][0]["style_id"], "CASE-001-02")
        self.assertEqual(result["items"][0]["asset_index"], 1)

    def test_same_prompt_distinct_images_have_distinct_asset_identity(self):
        blue = self.save_image("media/blue.png", "blue")
        record = self.make_record([self.raw, blue])
        bindings = [self.make_binding(record, 0, "media/red.png", self.raw),
                    self.make_binding(record, 1, "media/blue.png", blue)]
        items = self.prepare(bindings, {**self.bundle, "records": [record]})["items"]
        self.assertNotEqual(items[0]["id"], items[1]["id"])
        self.assertNotEqual(items[0]["signals"]["pixel_sha256"], items[1]["signals"]["pixel_sha256"])
        self.assertEqual(items[0]["catalog_key"], items[1]["catalog_key"])
        self.assertEqual(items[0]["record_id"], items[1]["record_id"])
        self.assertEqual([item["prompt"] for item in items], [self.prompt, self.prompt])

    def test_asset_identity_binds_source_version_and_complete_declared_reference(self):
        first = self.prepare()["items"][0]
        record = copy.deepcopy(self.record)
        record["source_version"]["repository_commit_sha"] = "9" * 40
        record["content_sha256"] = content_identity(record)
        second = self.prepare(bundle={**self.bundle, "records": [record]})["items"][0]
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["catalog_key"], second["catalog_key"])
        record["media_refs"][0]["path"] = "data/images/other.png"
        record["content_sha256"] = content_identity(record)
        third = self.prepare(bundle={**self.bundle, "records": [record]})["items"][0]
        self.assertNotEqual(second["id"], third["id"])

    def test_another_declared_image_cannot_be_substituted_even_with_correct_local_sha(self):
        blue = self.save_image("media/blue.png", "blue")
        swapped = {**self.binding, "local_path": "media/blue.png", "sha256": sha(blue)}
        with self.assertRaisesRegex(ValueError, "declared_git_blob"):
            self.prepare([swapped])

    def test_full_pixels_are_not_preview_pixels_or_alpha_composite_pixels(self):
        first = self.save_image("media/alpha1.png", (255, 0, 0, 0), size=(1000, 2), mode="RGBA")
        second = self.save_image("media/alpha2.png", (0, 0, 255, 0), size=(1000, 2), mode="RGBA")
        record = self.make_record([first, second])
        result = self.prepare([self.make_binding(record, 0, "media/alpha1.png", first),
                               self.make_binding(record, 1, "media/alpha2.png", second)],
                              {**self.bundle, "records": [record]})
        a, b = result["items"]
        self.assertEqual(a["prepared_sha256"], b["prepared_sha256"])
        self.assertNotEqual(a["signals"]["pixel_sha256"], b["signals"]["pixel_sha256"])
        self.assertEqual(a["signals"]["width"], 1000)
        with Image.open(io.BytesIO(result["previews"][a["prepared_sha256"]])) as preview:
            self.assertEqual(preview.width, 768)
            self.assertEqual(preview.mode, "RGB")

    def test_full_pixel_hash_applies_exif_orientation(self):
        original = Image.new("RGB", (3, 2), "red")
        original.putpixel((0, 0), (0, 0, 255))
        exif = Image.Exif()
        exif[274] = 6
        output = io.BytesIO()
        original.save(output, format="PNG", exif=exif)
        raw = output.getvalue()
        (self.root / "media/oriented.png").write_bytes(raw)
        record = self.make_record([raw])
        item = self.prepare([self.make_binding(record, 0, "media/oriented.png", raw)],
                            {**self.bundle, "records": [record]})["items"][0]
        expected = original.transpose(Image.Transpose.ROTATE_270).convert("RGBA")
        self.assertEqual(item["signals"]["pixel_sha256"],
            sha(b"rgba-exif-v2\0" + struct.pack(">II", *expected.size) + expected.tobytes()))
        self.assertEqual((item["signals"]["width"], item["signals"]["height"]), (2, 3))

    def test_malicious_missing_extra_ambiguous_and_out_of_range_bindings_fail(self):
        variants = [{**self.binding, "approval": True}, {key: value for key, value in self.binding.items() if key != "sha256"},
            {**self.binding, "source_id": "unknown"}, {**self.binding, "source_id": []},
            {**self.binding, "media_index": True}, {**self.binding, "media_index": -1},
            {**self.binding, "media_index": 1}, {**self.binding, "media_index": "0"},
            {**self.binding, "sha256": "F" * 64}, {**self.binding, "sha256": "0" * 64}]
        for binding in variants:
            with self.subTest(binding=binding), self.assertRaises(ValueError):
                self.prepare([binding])
        for bindings in ([], [self.binding] * 2, [self.binding] * 301, {"items": [self.binding]}):
            with self.subTest(bindings_type=type(bindings)), self.assertRaises(ValueError):
                self.prepare(bindings)
        with self.assertRaisesRegex(ValueError, "duplicate_intake_record"):
            self.prepare(bundle={**self.bundle, "records": [self.record, self.record]})

    def test_unsafe_missing_and_nonregular_paths_fail_before_decode(self):
        paths = ["../media/red.png", "media/../red.png", "media\\red.png", "C:/media/red.png", "/media/red.png",
            ".env/red.png", "media/.private/red.png", "media/secrets/red.png", "media/credentials.png",
            "media/token.png", "media/id_rsa.png", "media/NUL.png", "media/red.png:secret", "media//red.png",
            "media/red.png ", "media/red.png.", "media/missing.png", "media/red.txt", "media"]
        with patch.object(media, "prepared_image", side_effect=AssertionError("must not decode")):
            for path in paths:
                with self.subTest(path=path), self.assertRaises(ValueError):
                    self.prepare([{**self.binding, "local_path": path}])

    def test_symlink_ancestor_and_direct_file_are_rejected(self):
        original = Path.lstat
        for target in (self.root / "media", self.root / "media/red.png", self.root):
            def marked_link(path, *args, **kwargs):
                info = original(path, *args, **kwargs)
                if path == target:
                    return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
                return info
            with self.subTest(target=target), patch.object(Path, "lstat", marked_link):
                with self.assertRaisesRegex(ValueError, "symlink_or_junction"):
                    self.prepare()

    def test_missing_media_git_identity_and_tampered_envelopes_fail(self):
        for mutate in (lambda record: record["media_refs"][0].pop("git_blob_sha1"),
                       lambda record: record["media_refs"][0].update(git_blob_sha1="not-git"),
                       lambda record: record["rights"].update(image_approved=True),
                       lambda record: record["original_prompt"].update(text="altered")):
            record = copy.deepcopy(self.record)
            mutate(record)
            record["content_sha256"] = content_identity(record)
            with self.assertRaises(ValueError):
                self.prepare(bundle={**self.bundle, "records": [record]})

    def test_animated_media_is_rejected_without_producing_preview(self):
        output = io.BytesIO()
        Image.new("RGB", (4, 4), "red").save(output, format="GIF", save_all=True,
            append_images=[Image.new("RGB", (4, 4), "blue")], duration=100, loop=0)
        raw = output.getvalue()
        (self.root / "media/animated.gif").write_bytes(raw)
        record = self.make_record([raw])
        with patch.object(media, "prepared_image", side_effect=AssertionError("must not prepare animated image")):
            with self.assertRaisesRegex(ValueError, "animated_media"):
                self.prepare([self.make_binding(record, 0, "media/animated.gif", raw)],
                             {**self.bundle, "records": [record]})

    def test_byte_and_pixel_caps_are_enforced(self):
        with patch.object(media, "MAX_BYTES", len(self.raw) - 1):
            with self.assertRaisesRegex(ValueError, "15mib"):
                self.prepare()
        with patch.object(media, "MAX_PIXELS", 15):
            with self.assertRaisesRegex(ValueError, "oversized"):
                self.prepare()
        self.assertEqual(media.MAX_BYTES, 15 * 1024**2)
        self.assertEqual(media.MAX_PIXELS, 80_000_000)

    def test_windows_fstat_ctime_difference_does_not_reject_unchanged_bytes(self):
        actual_fstat = os.fstat
        def descriptor_stat(handle):
            info = actual_fstat(handle)
            return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino,
                st_size=info.st_size, st_mtime_ns=info.st_mtime_ns, st_ctime_ns=info.st_ctime_ns + 100)
        with patch.object(os, "fstat", side_effect=descriptor_stat):
            self.assertEqual(self.prepare()["items"][0]["sha256"], sha(self.raw))

    def test_same_bytes_path_replacement_after_preparation_is_rejected(self):
        replacement = self.root / "media/same-bytes.png"
        replacement.write_bytes(self.raw)
        real_prepare = media.prepared_image
        def replace_after_decode(buffer):
            result = real_prepare(buffer)
            replacement.replace(self.root / "media/red.png")
            return result
        with patch.object(media, "prepared_image", side_effect=replace_after_decode):
            with self.assertRaisesRegex(ValueError, "changed_during_preparation"):
                self.prepare()

    def test_same_size_preserved_mtime_drift_after_preparation_is_rejected(self):
        raw = self.save_image("media/drift.bmp", "red", format="BMP")
        replacement = self.save_image("media/replacement.bmp", "blue", format="BMP")
        self.assertEqual(len(raw), len(replacement))
        record = self.make_record([raw])
        path = self.root / "media/drift.bmp"
        real_prepare = media.prepared_image
        def swap_after_decode(buffer):
            result = real_prepare(buffer)
            before = path.stat()
            path.write_bytes(replacement)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            return result
        with patch.object(media, "prepared_image", side_effect=swap_after_decode):
            with self.assertRaisesRegex(ValueError, "sha256_mismatch"):
                self.prepare([self.make_binding(record, 0, "media/drift.bmp", raw)],
                             {**self.bundle, "records": [record]})


if __name__ == "__main__":
    unittest.main()
