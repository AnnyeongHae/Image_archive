from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from image_rag_eval.experiment import digest, json_bytes, run_path, write_json
from image_rag_eval.group_workflow import (
    GROUP_WORKFLOW_DIR, GROUP_WORKFLOW_HTML_FILENAME, GROUP_WORKFLOW_SPEC_FILENAME,
    GROUP_WORKFLOW_SPEC_SCHEMA_VERSION, refresh_group_workflow_html,
)


class GroupHtmlRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run_id = "refresh-fixture"
        self.directory = run_path(self.root, self.run_id) / GROUP_WORKFLOW_DIR
        self.directory.mkdir(parents=True)
        self.spec = {"schema_version": GROUP_WORKFLOW_SPEC_SCHEMA_VERSION, "run_id": self.run_id,
                     "items": [], "stage1": {"active_ids": [], "archived": []},
                     "duplicate_candidates": [], "similarity_candidates": []}
        self.spec["spec_sha256"] = digest(json_bytes(self.spec))
        self.spec_path = self.directory / GROUP_WORKFLOW_SPEC_FILENAME
        self.html_path = self.directory / GROUP_WORKFLOW_HTML_FILENAME
        write_json(self.spec_path, self.spec)
        self.old_spec = self.spec_path.read_bytes()
        self.html_path.write_text("old display only", encoding="utf-8")

    def test_dry_run_no_writes(self):
        result = refresh_group_workflow_html(self.root, self.run_id)
        self.assertEqual(result["writes"], 0)
        self.assertEqual(self.html_path.read_text(), "old display only")
        self.assertEqual(self.spec_path.read_bytes(), self.old_spec)

    def test_refresh_preserves_spec_and_previous_html_and_is_idempotent(self):
        result = refresh_group_workflow_html(self.root, self.run_id, apply=True)
        self.assertEqual(result["status"], "refreshed")
        self.assertEqual(self.spec_path.read_bytes(), self.old_spec)
        self.assertEqual(Path(result["previous_html_path"]).read_text(), "old display only")
        rendered = self.html_path.read_text(encoding="utf-8")
        self.assertIn('id="download-decisions"', rendered)
        self.assertIn('id="stage-4"', rendered)
        self.assertEqual(refresh_group_workflow_html(self.root, self.run_id, apply=True)["status"], "unchanged")

    def test_tampered_spec_blocks_refresh(self):
        self.spec["run_id"] = "wrong"
        write_json(self.spec_path, self.spec)
        with self.assertRaises(ValueError):
            refresh_group_workflow_html(self.root, self.run_id, apply=True)
        self.assertEqual(self.html_path.read_text(), "old display only")


if __name__ == "__main__":
    unittest.main()
