import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import prepare_luna_result_revision as revision
from image_rag_eval.luna_analysis_import import digest, encode
from prepare_luna_full_library import immutable
from qa.test_luna_compact import CompactContractTests


class ResultRevisionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        fixture = CompactContractTests(); fixture.setUp()
        self.result, self.pinned = fixture.result, fixture.pinned
        self.draft = {"style_id": "X-001", "visual": copy.deepcopy(self.result["visual"])}
        self.task = {"style_id": "X-001", "input_fingerprint": "f" * 64,
            "raw_result_path": "raw.json", "visual_draft_path": "draft.json", "prompt_context_path": "context.json",
            "prompt_sha256": digest(b"original"), "prepared_image_path": "image.png", "prepared_image_sha256": digest(b"image")}
        self.manifest = {"tasks": [self.task], "batches": [{"batch_id": "batch-001", "style_ids": ["X-001"]}]}
        immutable(self.root / "raw.json", encode(self.result))
        immutable(self.root / "draft.json", encode(self.draft))
        immutable(self.root / "context.json", encode({"style_id": "X-001", "prompt_sha256": digest(b"original"), "full_prompt": "original"}))
        immutable(self.root / "image.png", b"image")
        self.enterContext(patch.object(revision, "read_manifest", return_value=(self.manifest, encode(self.manifest))))
        self.enterContext(patch.object(revision, "contract", return_value=self.pinned))

    def test_dry_run_then_immutable_archive_keeps_source_bytes(self):
        originals = {name: (self.root / name).read_bytes() for name in ("raw.json", "draft.json", "context.json", "image.png")}
        proposed = revision.prepare_revision(self.root, "batch-001")
        self.assertFalse(Path(proposed["history_path"]).exists())
        result = revision.prepare_revision(self.root, "batch-001", apply=True)
        blob = Path(result["history_path"]).read_bytes()
        self.assertEqual(digest(blob), result["history_sha256"])
        stored = json.loads(blob)["records"][0]
        self.assertEqual(stored["raw_json"].encode(), originals["raw.json"])
        revision.prepare_revision(self.root, "batch-001", apply=True)
        self.assertEqual(originals, {name: (self.root / name).read_bytes() for name in originals})

    def test_unknown_batch_no_archive(self):
        with self.assertRaisesRegex(ValueError, "Unknown"):
            revision.prepare_revision(self.root, "batch-999", apply=True)

    def test_changed_source_rejected(self):
        (self.root / "image.png").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "image changed"):
            revision.prepare_revision(self.root, "batch-001", apply=True)

    def test_visual_drift_rejected_without_archive(self):
        self.draft["visual"]["caption_ko"] = "unrelated"
        (self.root / "draft.json").write_bytes(encode(self.draft))
        with self.assertRaises(ValueError):
            revision.prepare_revision(self.root, "batch-001", apply=True)
        self.assertFalse((self.root / revision.BASE / "result-history").exists())
