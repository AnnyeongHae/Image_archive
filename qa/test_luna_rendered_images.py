import hashlib
import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from audit_luna_rendered_images import audit


class RenderedImageAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sessions = self.root / "sessions"
        self.base = self.root / "data/private-research/image-rag-admin/luna-analysis/2026-09-04-luna-full-library-v3"
        (self.base / "execution").mkdir(parents=True)
        self.sid = "11111111-1111-4111-8111-111111111111"
        self.asset = "a" * 64
        self.manifest = {"analysis_run_id": "test", "tasks": [{"style_id": "X", "analysis_mode": "new_compact", "prepared_image_sha256": self.asset}]}
        (self.base / "tasks.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        self.receipt_path = self.base / "execution/test.tokens.json"

    def fixture(self, *, returned=True, turn="allowed", spaced=False):
        literal = '"' + self.asset + '"'
        if spaced:
            literal = '"' + self.asset[:20] + " " + self.asset[20:] + '".replace(/ /g,\'\')'
        records = [
            {"type": "session_meta", "payload": {"id": self.sid}},
            {"type": "turn_context", "payload": {"turn_id": turn}},
            {"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "c1", "input": "const h=" + literal + "; tools.view_image({path:h});"}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c1", "output": [{"type": "input_image"}] if returned else [{"type": "input_text", "text": "file not found"}]}},
        ]
        raw = ("\n".join(json.dumps(r) for r in records) + "\n").encode()
        name = "rollout-2026-09-04T01-02-03-" + self.sid + ".jsonl"
        path = self.sessions / "2026/09/04" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        receipt = {"session_id_reported": self.sid, "source_log_name": name, "source_prefix_line_count": len(records),
                   "source_prefix_bytes": len(raw), "source_prefix_sha256": hashlib.sha256(raw).hexdigest(),
                   "turn_ids": ["allowed"], "assigned_styles": ["X"]}
        self.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        return path, receipt

    def test_literal_rendered_evidence_is_read_only(self):
        self.fixture()
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = audit(self.root, self.sessions)
        self.assertEqual(result["literal_rendered_style_ids"], ["X"])
        self.assertFalse(result["metadata_human_approved"])
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_exact_constant_space_removal(self):
        self.fixture(spaced=True)
        self.assertEqual(audit(self.root, self.sessions)["literal_rendered_style_ids"], ["X"])

    def test_no_returned_image_is_not_evidence(self):
        self.fixture(returned=False)
        self.assertEqual(audit(self.root, self.sessions)["not_literal_matched_style_ids"], ["X"])

    def test_unassigned_turn_not_counted(self):
        self.fixture(turn="unassigned")
        self.assertEqual(audit(self.root, self.sessions)["literal_rendered_style_count"], 0)

    def test_changed_prefix_rejected(self):
        path, _ = self.fixture()
        path.write_bytes(path.read_bytes().replace(b"c1", b"c2"))
        with self.assertRaisesRegex(ValueError, "prefix changed"):
            audit(self.root, self.sessions)

    def test_appended_future_events_ignored(self):
        path, _ = self.fixture()
        with path.open("ab") as stream:
            stream.write(b"not yet a complete future JSON record")
        self.assertEqual(audit(self.root, self.sessions)["literal_rendered_style_count"], 1)

    def test_unbounded_prefix_rejected(self):
        _, receipt = self.fixture()
        receipt["source_prefix_bytes"] = 1024**3 + 1
        self.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Bounded"):
            audit(self.root, self.sessions)

    def test_partial_failed_call_can_return_exact_image(self):
        path, receipt = self.fixture()
        binary = b"fixture image bytes"
        self.manifest["tasks"][0]["prepared_image_sha256"] = hashlib.sha256(binary).hexdigest()
        (self.base / "tasks.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        records = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
        records[-1]["payload"]["output"] = [
            {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(binary).decode()},
            {"type": "input_text", "text": "Script failed after the first image"},
        ]
        raw = ("\n".join(json.dumps(r) for r in records) + "\n").encode()
        path.write_bytes(raw)
        receipt.update(source_prefix_bytes=len(raw), source_prefix_sha256=hashlib.sha256(raw).hexdigest())
        self.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        result = audit(self.root, self.sessions)
        self.assertEqual(result["exact_returned_image_style_ids"], ["X"])
        self.assertEqual(result["literal_rendered_style_ids"], [])

    def test_literal_call_is_not_exact_byte_proof(self):
        self.fixture()
        result = audit(self.root, self.sessions)
        self.assertEqual(result["exact_returned_image_style_count"], 0)
        self.assertEqual(result["literal_only_style_ids"], ["X"])


if __name__ == "__main__":
    unittest.main()
