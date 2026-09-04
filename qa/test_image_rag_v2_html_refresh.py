from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import build_image_similarity_review_v2 as cli


class HtmlRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.html = self.root / cli.REVIEW_V2_HTML_FILENAME
        self.html.write_text("old html", encoding="utf-8")
        self.spec = self.root / "human-similarity-review-v2.spec.json"
        self.spec.write_text("immutable spec", encoding="utf-8")

    def invoke(self, *, apply: bool) -> dict:
        argv = ["review", "--root", str(self.root), "--source-run-id", "fixture", "--refresh-html"]
        if apply:
            argv.append("--apply")
        output = io.StringIO()
        with mock.patch.object(sys, "argv", argv), mock.patch.object(cli, "run_path", return_value=self.root), mock.patch.object(cli, "load_bound_review_spec_v2", return_value=({"review_spec_sha256": "bound-spec"}, {}, {})) as binding, mock.patch.object(cli, "review_html_v2", return_value="new html"), redirect_stdout(output):
            cli.main()
        binding.assert_called_once()
        return json.loads(output.getvalue())

    def test_dry_run_preserves_all_existing_files(self) -> None:
        result = self.invoke(apply=False)
        self.assertEqual(result["writes"], 0)
        self.assertEqual(self.html.read_text(encoding="utf-8"), "old html")
        self.assertEqual(self.spec.read_text(encoding="utf-8"), "immutable spec")

    def test_apply_changes_html_only_and_is_idempotent(self) -> None:
        result = self.invoke(apply=True)
        self.assertEqual(result["writes"], 1)
        self.assertEqual(result["spec_writes"], 0)
        self.assertEqual(result["label_writes"], 0)
        self.assertEqual(self.html.read_text(encoding="utf-8"), "new html")
        self.assertEqual(self.spec.read_text(encoding="utf-8"), "immutable spec")
        self.assertEqual(self.invoke(apply=True)["writes"], 0)


if __name__ == "__main__":
    unittest.main()
