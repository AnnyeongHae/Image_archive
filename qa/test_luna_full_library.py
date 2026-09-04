import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import prepare_luna_full_library as full
from image_rag_eval.luna_analysis_import import encode, digest
import qa.test_luna_compact as compact_tests


class FullLibraryTests(unittest.TestCase):
    def test_immutable_same_is_idempotent_changed_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "nested/result.json"
            full.immutable(path, b"{}"); full.immutable(path, b"{}")
            with self.assertRaises(ValueError):
                full.immutable(path, b"[]")
            self.assertEqual(path.read_bytes(), b"{}")

    def test_manifest_strict_and_large(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / full.BASE / "tasks.json"
            value = {"schema_version": "luna-full-library-3", "extra": "x" * (2 * 1024 * 1024)}
            full.immutable(path, encode(value))
            with patch.object(full, "EXPECTED_MANIFEST_SHA256", digest(encode(value))):
                self.assertEqual(full.read_manifest(root)[0], value)
            with self.assertRaises(ValueError):
                full.read_manifest(root)

    def test_manifest_rejects_duplicate_and_nonfinite(self):
        for raw in (b'{"schema_version":"luna-full-library-3","x":1,"x":2}',
                    b'{"schema_version":"luna-full-library-3","x":NaN}', b'[]'):
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                full.immutable(root / full.BASE / "tasks.json", raw)
                with patch.object(full, "EXPECTED_MANIFEST_SHA256", digest(raw)), self.assertRaises(ValueError):
                    full.read_manifest(root)

    def test_progress_verifies_prompt_image_and_draft(self):
        fixture = compact_tests.CompactContractTests(); fixture.setUp()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            task = {"style_id": "X-001", "analysis_mode": "new_compact", "raw_result_path": "result.json",
                    "visual_draft_path": "draft.json", "prompt_context_path": "context.json",
                    "prepared_image_path": "image.png", "prepared_image_sha256": digest(b"image"),
                    "prompt_sha256": digest(b"original")}
            manifest = {"schema_version": "luna-full-library-3", "prefix_sha256": fixture.pinned["prefix_sha256"],
                        "counts": {"legacy_reused": 0}, "tasks": [task]}
            full.immutable(root / full.BASE / "tasks.json", encode(manifest))
            with patch.object(full, "contract", return_value=fixture.pinned), patch.object(full, "EXPECTED_MANIFEST_SHA256", digest(encode(manifest))):
                self.assertEqual(len(full.validate_progress(root)["input_invalid"]), 1)
                full.immutable(root / "context.json", encode({"full_prompt": "original"}))
                full.immutable(root / "image.png", b"image")
                self.assertEqual(full.validate_progress(root)["missing"], 1)
                full.immutable(root / "result.json", encode(fixture.result))
                full.immutable(root / "draft.json", encode({"style_id": "X-001", "visual": fixture.result["visual"]}))
                self.assertEqual(full.validate_progress(root)["new_valid"], 1)
                # Test-only corruption must fail closed.
                (root / "image.png").write_bytes(b"changed")
                self.assertEqual(len(full.validate_progress(root)["input_invalid"]), 1)


if __name__ == "__main__":
    unittest.main()
