"""Offline synthetic checks for the read-only cloud-search verifier."""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("v2_cloud_search_under_test", ROOT / "qa/verify_v2_cloud_search.py")
search = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(search)

PRIVATE_PROMPT = "PRIVATE_SOURCE_PROMPT_DO_NOT_EXPORT 원문"
PRIVATE_QUERY = "PRIVATE_QUERY_TEXT_DO_NOT_EXPORT"
FAKE_SECRET = "SYNTHETIC_CREDENTIAL_DO_NOT_EXPORT"


def fixture():
    sid = "a" * 64
    items, points = [], []
    # The highest-scoring item is a group member, not its human representative.
    for index, (group, score) in enumerate(((0, .95), (0, .98), (1, .90), (2, .80), (3, .70), (4, .60), (5, .50))):
        ident = f"item-{index}"
        representative = "item-0" if group == 0 else ident
        payload = {"item_id": ident, "group_id": f"group-{group}", "representative_id": representative,
                   "snapshot_id": sid, "image_approved": True}
        items.append({**payload, "text_ready": True, "original_prompt": PRIVATE_PROMPT,
                      "retrieval_text": "PRIVATE_COMPACT_TEXT", "metadata_json": {"public_eligible": False}})
        points.append({"id": f"point-{index}", "payload": payload,
                       "vector": [score, math.sqrt(1-score*score)] + [0.] * 510})
    queries = [{"query_id": f"query-{index}", "snapshot_id": sid, "model": "voyage-4-lite",
                "dimension": 512, "vector_json": [1.] + [0.] * 511, "query_text": PRIVATE_QUERY}
               for index in range(11)]
    return {"manifest_sha256": search.PLAN_HASH,
            "manifest": {"snapshot_id": sid, "qdrant_collections": {"text": f"image_archive_v2_{sid}_text512"},
                         "files": {"items.jsonl": {"sha256": "b" * 64}}},
            "items": items, "text": points, "queries": queries}


def grouped_result(plan):
    # The query is the first unit axis, so each independently calculated cosine
    # is the first coordinate; do not call the verifier to fabricate its oracle.
    best = {}
    for point in plan["text"]:
        group = point["payload"]["group_id"]
        if group not in best or point["vector"][0] > best[group]["vector"][0]:
            best[group] = point
    groups = []
    for group, point in sorted(best.items(), key=lambda pair: (-pair[1]["vector"][0], pair[0]))[:5]:
        groups.append({"id": group, "hits": [{"id": point["id"], "score": point["vector"][0],
                                               "payload": copy.deepcopy(point["payload"])}]})
    return {"groups": groups}


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        connection = self.connection
        if not connection.readonly or connection.session != {
            "readonly": True, "isolation_level": "REPEATABLE READ", "autocommit": False
        }:
            raise AssertionError("SQL issued before read-only session setup")
        connection.statements.append((sql, params))
        if sql == "SHOW transaction_read_only":
            self.result = (connection.server_readonly,)
        elif sql == "SELECT manifest_sha256,manifest_json,state FROM image_archive_v2.snapshots WHERE snapshot_id=%s":
            if params != (connection.plan["manifest"]["snapshot_id"],):
                raise AssertionError("unexpected snapshot binding")
            self.result = connection.header
        else:
            raise AssertionError("unexpected or mutating SQL")

    def fetchone(self):
        return self.result


class FakeConnection:
    def __init__(self, plan):
        self.plan = plan
        self.readonly = False
        self.session = None
        self.server_readonly = "on"
        self.header = (plan["manifest_sha256"], plan["manifest"], "ready")
        self.statements = []
        self.closed = False

    def set_session(self, **kwargs):
        if self.statements:
            raise AssertionError("late read-only session setup")
        self.session = kwargs

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


class FakeNeon:
    def __init__(self, plan):
        self.connection = FakeConnection(plan)
        self.verify = MagicMock(side_effect=self._verify)

    def _verify(self, plan):
        if plan is not self.connection.plan or len(self.connection.statements) != 2:
            raise AssertionError("full readback must follow read-only header checks")


class FakeQdrant:
    def __init__(self, plan):
        self.plan = plan
        self.calls = []
        self.result = grouped_result(plan)

    def request(self, method, path, body):
        expected_path = "/collections/" + self.plan["manifest"]["qdrant_collections"]["text"] + "/points/query/groups"
        if method != "POST" or path != expected_path:
            raise AssertionError("unexpected or mutating Qdrant endpoint")
        self.calls.append((method, path, copy.deepcopy(body)))
        return copy.deepcopy(self.result)


class CloudSearchTests(unittest.TestCase):
    def setUp(self):
        self.plan = fixture()
        self.neon = FakeNeon(self.plan)
        self.qdrant = FakeQdrant(self.plan)

    def validate(self, result=None, plan=None):
        return search.validate_groups(result if result is not None else grouped_result(self.plan),
                                      self.plan["queries"][0], plan or self.plan)

    def test_eleven_cached_queries_readonly_and_one_full_neon_verification(self):
        result = search.verify_search(self.plan, self.neon, self.qdrant)
        self.assertTrue(result["all_queries_passed"])
        self.assertEqual(result["qdrant_read_queries"], 11)
        self.neon.verify.assert_called_once_with(self.plan)
        self.assertTrue(self.neon.connection.readonly)
        self.assertEqual(len(self.neon.connection.statements), 2)
        self.assertEqual(len(self.qdrant.calls), 11)
        for query in result["queries"]:
            self.assertEqual(query["distinct_groups"], 5)
            self.assertTrue(query["local_cosine_baseline_verified"])
            self.assertEqual(query["groups"][0]["matched_item_id"], "item-1")
            self.assertEqual(query["groups"][0]["representative_id"], "item-0")
        for _, _, body in self.qdrant.calls:
            self.assertEqual(len(body["query"]), 512)
            self.assertEqual(body["limit"], 5)
            self.assertEqual(body["group_size"], 1)
            self.assertEqual(body["group_by"], "group_id")
            self.assertFalse(body["with_vector"])
            self.assertEqual(set(body["with_payload"]), search.cloud.PAYLOAD_KEYS)
            self.assertEqual(body["filter"], {"must": [
                {"key": "snapshot_id", "match": {"value": "a"*64}},
                {"key": "image_approved", "match": {"value": True}}]})
            self.assertNotIn(PRIVATE_QUERY, json.dumps(body))

    def test_readonly_off_blocks_full_readback_and_qdrant(self):
        self.neon.connection.server_readonly = "off"
        with self.assertRaisesRegex(search.SearchVerificationError, "readonly_transaction"):
            search.verify_search(self.plan, self.neon, self.qdrant)
        self.neon.verify.assert_not_called()
        self.assertFalse(self.qdrant.calls)

    def test_snapshot_header_mismatch_and_full_readback_failure_block_queries(self):
        for header in (("wrong", self.plan["manifest"], "ready"),
                       (self.plan["manifest_sha256"], {}, "ready"),
                       (self.plan["manifest_sha256"], self.plan["manifest"], "staged")):
            neon = FakeNeon(self.plan)
            neon.connection.header = header
            with self.subTest(header=header[0]), self.assertRaisesRegex(search.SearchVerificationError, "manifest_mismatch"):
                search.verify_search(self.plan, neon, self.qdrant)
            neon.verify.assert_not_called()
        self.neon.verify.side_effect = search.cloud.SnapshotError("readback_failed")
        with self.assertRaises(search.cloud.SnapshotError):
            search.verify_search(self.plan, self.neon, self.qdrant)
        self.assertFalse(self.qdrant.calls)

    def test_cached_scope_count_identity_dimension_model_and_vectors(self):
        cases = [
            lambda p: p["queries"].pop(),
            lambda p: p["queries"][1].update(query_id=p["queries"][0]["query_id"]),
            lambda p: p["manifest"]["qdrant_collections"].update(text="legacy"),
            lambda p: p["queries"][0].update(snapshot_id="b"*64),
            lambda p: p["queries"][0].update(model="different-model"),
            lambda p: p["queries"][0].update(dimension=1024),
            lambda p: p["queries"][0].update(vector_json=[1.]*511),
            lambda p: p["queries"][0].update(vector_json=[math.nan]+[0.]*511),
            lambda p: p["queries"][0].update(vector_json=[0.]*512),
            lambda p: p.update(text=p["text"][:5]),
        ]
        for mutate in cases:
            plan = copy.deepcopy(self.plan)
            mutate(plan)
            with self.subTest(mutate=mutate), self.assertRaises((search.SearchVerificationError, search.cloud.SnapshotError)):
                search.verify_search(plan, self.neon, self.qdrant)
        self.assertFalse(self.neon.connection.statements)
        self.assertFalse(self.qdrant.calls)

    def test_payload_tampering_and_unknown_point_fail_closed(self):
        for key, value in (("snapshot_id", "b"*64), ("image_approved", False),
                           ("image_approved", 1), ("group_id", "other"),
                           ("representative_id", "missing"), ("original_prompt", PRIVATE_PROMPT)):
            result = grouped_result(self.plan)
            result["groups"][0]["hits"][0]["payload"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(search.SearchVerificationError):
                self.validate(result)
        result = grouped_result(self.plan)
        result["groups"][0]["hits"][0]["id"] = "unknown-point"
        with self.assertRaisesRegex(search.SearchVerificationError, "unknown_text_point"):
            self.validate(result)

    def test_nonfinite_boolean_out_of_range_and_non_descending_scores(self):
        for score in (math.nan, math.inf, -math.inf, True, "0.98", 1.1, -1.1):
            result = grouped_result(self.plan)
            result["groups"][0]["hits"][0]["score"] = score
            with self.subTest(score=score), self.assertRaisesRegex(search.SearchVerificationError, "finite_cosine"):
                self.validate(result)
        result = grouped_result(self.plan)
        result["groups"][0], result["groups"][1] = result["groups"][1], result["groups"][0]
        with self.assertRaisesRegex(search.SearchVerificationError, "descending"):
            self.validate(result)

    def test_missing_duplicate_wrong_groups_and_multiple_hits_rejected(self):
        cases = [None, {}, {"groups": []}]
        result = grouped_result(self.plan)
        cases.append({"groups": result["groups"][:-1]})
        duplicate = copy.deepcopy(result)
        duplicate["groups"][1] = copy.deepcopy(duplicate["groups"][0])
        cases.append(duplicate)
        wrong_group = copy.deepcopy(result)
        wrong_group["groups"][0]["id"] = "different-group"
        cases.append(wrong_group)
        multiple_hits = copy.deepcopy(result)
        multiple_hits["groups"][0]["hits"] *= 2
        cases.append(multiple_hits)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(search.SearchVerificationError):
                search.validate_groups(value, self.plan["queries"][0], self.plan)

    def test_returned_vectors_are_forbidden(self):
        for key in ("vector", "vectors"):
            result = grouped_result(self.plan)
            result["groups"][0]["hits"][0][key] = [1.]
            with self.subTest(key=key), self.assertRaisesRegex(search.SearchVerificationError, "returned_vectors"):
                self.validate(result)

    def test_neon_representative_group_mapping_must_match(self):
        cases = [
            lambda p: p["items"].pop(0),
            lambda p: p["items"][0].update(group_id="different-group"),
            lambda p: p["items"][0].update(snapshot_id="b"*64),
            lambda p: p["items"][0].update(representative_id="item-1"),
            lambda p: p["items"][1].update(representative_id="item-1"),
            lambda p: p["items"][1].update(text_ready=False),
        ]
        for mutate in cases:
            plan = copy.deepcopy(self.plan)
            mutate(plan)
            with self.subTest(mutate=mutate), self.assertRaisesRegex(search.SearchVerificationError, "representative_mapping"):
                self.validate(plan=plan)

    def test_local_cosine_rejects_wrong_score_member_and_low_ranked_group(self):
        result = grouped_result(self.plan)
        result["groups"][0]["hits"][0]["score"] = .97
        with self.assertRaisesRegex(search.SearchVerificationError, "cosine_group_baseline"):
            self.validate(result)
        result = grouped_result(self.plan)
        point = self.plan["text"][0]
        result["groups"][0]["hits"][0] = {"id": point["id"], "payload": point["payload"], "score": .95}
        with self.assertRaisesRegex(search.SearchVerificationError, "cosine_group_baseline"):
            self.validate(result)
        result = grouped_result(self.plan)
        point = self.plan["text"][-1]
        result["groups"][-1] = {"id": point["payload"]["group_id"], "hits": [
            {"id": point["id"], "payload": point["payload"], "score": .50}]}
        with self.assertRaisesRegex(search.SearchVerificationError, "cosine_group_baseline"):
            self.validate(result)

    def test_default_dry_run_never_reads_credentials_connects_or_writes(self):
        with patch.object(search.cloud, "read_plan", return_value=self.plan), \
             patch.object(search.cloud, "credentials") as credentials, \
             patch.object(search.cloud, "Neon") as neon, \
             patch.object(search.cloud, "Qdrant") as qdrant, \
             patch.object(search, "write_receipt") as write, \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(search.main([]), 0)
            result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "dry_run")
        for key in ("network_calls", "local_writes", "cloud_writes", "new_embedding_calls"):
            self.assertEqual(result[key], 0)
        for mocked in (credentials, neon, qdrant, write):
            mocked.assert_not_called()

    def test_nonfrozen_plan_blocks_credentials_even_with_verify(self):
        self.plan["manifest_sha256"] = "f"*64
        with patch.object(search.cloud, "read_plan", return_value=self.plan), \
             patch.object(search.cloud, "credentials") as credentials, \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(search.main(["--verify"]), 1)
        self.assertEqual(json.loads(output.getvalue())["error_code"], "frozen_plan_required")
        credentials.assert_not_called()

    def execute_offline(self, root):
        values = {"DATABASE_URL": FAKE_SECRET, "QDRANT_ENDPOINT": "https://synthetic.cloud.qdrant.io",
                  "QDRANT_API_KEY": FAKE_SECRET}
        with patch.object(search.cloud, "credentials", return_value=values) as credentials, \
             patch.object(search.cloud, "Neon", return_value=self.neon), \
             patch.object(search.cloud, "Qdrant", return_value=self.qdrant):
            result = search.execute_verification(self.plan, root=root)
        credentials.assert_called_once_with(root / ".env")
        return result

    def test_private_receipt_hashes_and_no_raw_prompts_queries_vectors_or_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            result = self.execute_offline(root)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["queries_passed"], 11)
            self.assertTrue(self.neon.connection.closed)
            path = root / result["receipt"]
            self.assertTrue(path.is_relative_to(root / "data/private-research"))
            raw = path.read_bytes()
            self.assertEqual(search.cloud.sha(raw), result["receipt_sha256"])
            receipt = json.loads(raw)
            self.assertEqual(receipt["privacy"], "owner_private")
            for key in ("cloud_writes", "new_embedding_calls", "new_credentials"):
                self.assertEqual(receipt[key], 0)
            self.assertFalse(receipt["rights_changed"])
            self.assertFalse(receipt["public_release"])
            for forbidden in (PRIVATE_PROMPT, PRIVATE_QUERY, FAKE_SECRET, "original_prompt", "vector_json", "PRIVATE_COMPACT_TEXT"):
                self.assertNotIn(forbidden, raw.decode())
            with self.assertRaises(FileExistsError):
                search.write_receipt(receipt, root)
            self.assertEqual(path.read_bytes(), raw)

    def test_transport_failure_closes_neon_and_receipt_sanitizes_error(self):
        self.qdrant.request = MagicMock(side_effect=RuntimeError(FAKE_SECRET + PRIVATE_PROMPT))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            result = self.execute_offline(root)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error_code"], "cloud_search_verification_failed")
            self.assertTrue(self.neon.connection.closed)
            self.qdrant.request.assert_called_once()
            raw = (root / result["receipt"]).read_text(encoding="utf-8")
            self.assertNotIn(FAKE_SECRET, raw)
            self.assertNotIn(PRIVATE_PROMPT, raw)
            self.assertEqual(json.loads(raw)["cloud_writes"], 0)

    def test_second_query_failure_keeps_completed_query_and_attempt_count_without_retry(self):
        self.qdrant.request = MagicMock(side_effect=[grouped_result(self.plan), RuntimeError(FAKE_SECRET)])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            result = self.execute_offline(root)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["queries_passed"], 1)
            self.assertEqual(self.qdrant.request.call_count, 2)
            self.neon.verify.assert_called_once_with(self.plan)
            receipt = json.loads((root / result["receipt"]).read_bytes())
            self.assertEqual(receipt["qdrant_read_queries"], 2)
            self.assertEqual(len(receipt["queries"]), 1)
            self.assertEqual(receipt["queries"][0]["query_id"], "query-0")
            self.assertFalse(receipt["all_queries_passed"])
            self.assertTrue(receipt["neon"]["readonly_transaction_confirmed"])
            self.assertTrue(self.neon.connection.closed)

    def test_receipt_path_cannot_escape_private_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with self.assertRaises(search.cloud.SnapshotError):
                search.write_receipt({"run_id": "../../../../outside"}, root)
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
