from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from prepare_image_text_embedding_run import prepare, save
from image_rag_eval.embedding_budget import encoded


class Counter:
    def no_truncation(self):
        pass

    def no_padding(self):
        pass

    def encode(self, text, add_special_tokens=False):
        return SimpleNamespace(ids=list(text))


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db, self.tok = self.root / "source.sqlite3", self.root / "tokenizer.json"
        self.db.write_bytes(b"unchanged fixture")
        self.tok.write_bytes(b"{}")
        rows = [{"item_id": "a", "style_id": "A", "compact_text": "retained meaning", "budget_blocked": False,
                 "excluded_qa_roots": [], "naive_baseline_sha256": hashlib.sha256(b"full original").hexdigest()},
                {"item_id": "b", "style_id": "B", "compact_text": "", "budget_blocked": True}]
        body = b"".join(encoded(row) for row in rows)
        summary = {"documents_sha256": hashlib.sha256(body).hexdigest(),
                   "database_sha256": hashlib.sha256(self.db.read_bytes()).hexdigest(),
                   "tokenizer_sha256": hashlib.sha256(self.tok.read_bytes()).hexdigest(),
                   "approved_document_count": 2, "budget_blocked_count": 1}
        raw = encoded(summary)
        self.plan = self.root / hashlib.sha256(raw).hexdigest()
        self.plan.mkdir()
        (self.plan / "summary.json").write_bytes(raw)
        (self.plan / "documents.jsonl").write_bytes(body)
        self.fixture = self.root / "evaluation.json"
        self.canary = {"schema_version": "image-text-embedding-inputs-1", "model": "voyage-4-lite",
                       "dimension": 512, "total_token_cap": 260000, "documents": [
                           {"input_id": "compact:a", "item_id": "a", "text": "retained meaning", "input_type": "document"},
                           {"input_id": "baseline:a", "item_id": "a", "text": "full original", "input_type": "document"},
                           {"input_id": "query:q1", "text": "purpose", "input_type": "query"}]}

    def build(self):
        self.fixture.write_bytes(encoded({"embedding_manifest": self.canary}))
        return prepare(self.plan, self.db, self.fixture, self.tok, tokenizer=Counter())

    def test_ready_only_deduplicated_budget_no_source_changes(self):
        before = self.db.read_bytes()
        artifacts = self.build()
        self.assertEqual(artifacts["preparation.json"]["unique_combined_inputs"], 3)
        self.assertEqual(artifacts["preparation.json"]["blocked_style_ids"], ["B"])
        self.assertEqual(len(artifacts["full-inputs.json"]["documents"]), 1)
        self.assertEqual(self.db.read_bytes(), before)

    def test_dry_run_and_immutable_apply(self):
        artifacts = self.build()
        target = self.root / "data/private-research/inputs"
        save(artifacts, target, self.root)
        self.assertFalse(target.exists())
        first = save(artifacts, target, self.root, apply=True)
        self.assertEqual(save(artifacts, target, self.root, apply=True), first)
        (target / "full-inputs.json").write_text("tampered")
        with self.assertRaises(ValueError):
            save(artifacts, target, self.root, apply=True)
        with self.assertRaises(ValueError):
            save(artifacts, self.root / "dist", self.root, apply=True)

    def test_plan_and_source_tamper_fail(self):
        self.db.write_bytes(b"drift")
        with self.assertRaisesRegex(ValueError, "database"):
            self.build()

    def test_canary_input_tamper_fail(self):
        for key, text in (("compact:a", "changed"), ("baseline:a", "changed"), ("compact:b", "unknown")):
            with self.subTest(key=key):
                self.canary["documents"][0] = {"input_id": key, "item_id": key.split(":")[1], "text": text, "input_type": "document"}
                with self.assertRaises(ValueError):
                    self.build()

    def test_duplicate_and_long_query_rejected(self):
        self.canary["documents"].append(dict(self.canary["documents"][0]))
        with self.assertRaises(ValueError):
            self.build()
        self.canary["documents"].pop()
        self.canary["documents"][-1]["text"] = "x" * 2001
        with self.assertRaises(ValueError):
            self.build()


if __name__ == "__main__":
    unittest.main()
