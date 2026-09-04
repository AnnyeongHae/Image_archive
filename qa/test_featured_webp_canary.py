from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src" / "build_static_canary.py"


def load_module():
    spec = importlib.util.spec_from_file_location("build_static_canary", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FeaturedWebpCanaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_payload_contains_webp_metadata(self):
        payload, summary, derivatives = self.module.build_featured_payload()

        self.assertEqual(len(payload["items"]), 5)
        self.assertEqual(summary["derivative_count"], 10)
        self.assertEqual(len(derivatives), 10)

        for item in payload["items"]:
            self.assertTrue(item["webp_path"].endswith(".webp"))
            self.assertGreater(item["webp_bytes"], 0)
            self.assertEqual(len(item["webp_sha256"]), 64)
            self.assertGreater(item["image_width"], 0)
            self.assertGreater(item["image_height"], 0)
            self.assertEqual(item["image_width"], item["webp_width"])
            self.assertEqual(item["image_height"], item["webp_height"])
            self.assertGreater(item["webp_savings_bytes"], 0)
            self.assertIn(item["fallback_format"], {"jpeg", "png"})
            self.assertTrue(item["delivery_fallback_path"].endswith((".fallback.jpg", ".fallback.png")))
            self.assertGreater(item["delivery_fallback_bytes"], 0)
            self.assertEqual(len(item["delivery_fallback_sha256"]), 64)
        self.assertEqual(summary["preferred_format"], "webp")
        self.assertEqual(summary["fallback_format"], "mixed")
        self.assertEqual(summary["deployment_profile"], "compressed_only_dist")
        self.assertEqual(summary["supported_formats"], ["webp", "fallback"])
        self.assertEqual(summary["avif_policy"], "private_benchmark_only_not_emitted_to_public_bundle_v1")
        self.assertTrue(all("avif_path" not in item for item in payload["items"]))

    def test_encode_webp_applies_exif_orientation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "rotated.jpg"
            image = Image.new("RGB", (40, 20), "red")
            exif = Image.Exif()
            exif[274] = 6
            image.save(source, format="JPEG", exif=exif)

            content, width, height = self.module.encode_webp(source)

            self.assertEqual((width, height), (20, 40))
            with Image.open(io.BytesIO(content)) as encoded:
                self.assertEqual(encoded.size, (20, 40))

    def test_source_format_is_derived_from_path(self):
        self.assertEqual(self.module.source_format("asset.PNG"), "png")
        self.assertEqual(self.module.source_format("asset.jpg"), "jpeg")
        with self.assertRaises(ValueError):
            self.module.source_format("asset")

    def test_webp_relative_path_requires_suffix(self):
        with self.assertRaises(ValueError):
            self.module.webp_relative_path("media/public/featured/featured-01")

    def test_compressed_fallback_path_reflects_alpha(self):
        self.assertTrue(
            self.module.compressed_fallback_relative_path("media/public/featured/example.png", has_alpha=True).endswith(
                ".fallback.png"
            )
        )
        self.assertTrue(
            self.module.compressed_fallback_relative_path("media/public/featured/example.png", has_alpha=False).endswith(
                ".fallback.jpg"
            )
        )

    def test_dry_run_reports_webp_outputs_and_savings(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = self.module.build(False)

        self.assertEqual(exit_code, 0)
        report = json.loads(buffer.getvalue())
        self.assertEqual(report["mode"], "dry_run")
        self.assertIn("media/public/featured/*.webp", report["planned"])
        self.assertIn("media/public/featured/*.fallback.(jpg|png)", report["planned"])
        self.assertIn("dist/media/public/featured/*.webp", report["planned"])
        self.assertIn("dist/media/public/featured/*.fallback.(jpg|png)", report["planned"])
        self.assertGreater(report["featured_media_summary"]["savings_total_bytes"], 0)
        self.assertGreater(report["featured_media_summary"]["delivery_fallback_savings_total_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
