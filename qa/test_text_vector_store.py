from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from image_rag_eval.text_vector_store import populate


class TextVectorStoreTests(unittest.TestCase):
    def fixture(self):
        docs = [{"item_id": ident, "style_id": ident.upper(), "group_id": "g" if ident != "c" else None,
                 "representative_item_id": "a" if ident != "c" else "c", "compact_text": "purpose" if ident != "c" else "",
                 "prompt_text": "original " + ident, "rights_json": "{\"release_eligible\":false}", "image_path": ident + ".png",
                 "prepared_sha256": "a" * 64, "budget_blocked": ident == "c"} for ident in "abc"]
        return docs, {"compact:a": [1.] + [0.] * 511, "compact:b": [1.] + [0.] * 511}

    def test_group_children_retained_vector_identity_dedup_and_deferred(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        docs, vectors = self.fixture()
        queries = [{"input_id": "query:1", "text": "find purpose", "input_type": "query", "vector": [0., 1.] + [0.] * 510}]
        populate(db, docs, vectors, queries, {"release_eligible": False})
        self.assertEqual(db.execute("SELECT count(*) FROM documents").fetchone()[0], 3)
        self.assertEqual(db.execute("SELECT count(*) FROM document_vectors").fetchone()[0], 2)
        self.assertEqual(db.execute("SELECT count(*) FROM text_vectors").fetchone()[0], 2)
        self.assertEqual(db.execute("SELECT length(vector_f32le) FROM text_vectors").fetchone()[0], 2048)
        self.assertEqual(db.execute("SELECT count(*) FROM documents WHERE public_eligible != 0").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT metadata_status FROM documents WHERE item_id='c'").fetchone()[0], "text_deferred")
        self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_blocked_vector_rejected(self):
        docs, vectors = self.fixture()
        vectors["compact:c"] = [1.] + [0.] * 511
        with sqlite3.connect(":memory:") as db, self.assertRaises(ValueError):
            populate(db, docs, vectors, [], {})

    def test_missing_representative_rejected(self):
        docs, vectors = self.fixture()
        docs[0]["representative_item_id"] = "unknown"
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        with self.assertRaises(sqlite3.IntegrityError):
            populate(db, docs, vectors, [], {})


if __name__ == "__main__":
    unittest.main()
