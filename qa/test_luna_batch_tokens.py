"""Synthetic local-log tests; no real sessions or external calls are used."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from measure_luna_batch_tokens import UsageError, measure_batch_tokens


class LunaBatchTokenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "archive"
        self.root.mkdir()

    @staticmethod
    def usage(input_tokens=100, cached=60, cache_write=10, output=20, reasoning=5):
        return {"input_tokens": input_tokens, "cached_input_tokens": cached,
                "cache_write_input_tokens": cache_write, "output_tokens": output,
                "reasoning_output_tokens": reasoning, "total_tokens": input_tokens + output}

    def binding(self, name, styles, *, deltas=None, reset=False):
        deltas = deltas or [self.usage()]
        cumulative = {key: 0 for key in deltas[0]}
        rows = [{"type": "session_meta", "payload": {
            "id": f"session-{name}", "agent_path": f"/root/luna_{name}", "model_provider": "openai"}}]
        for index, delta in enumerate(deltas):
            cumulative = dict(delta) if reset else {key: cumulative[key] + delta[key] for key in cumulative}
            rows.extend([
                {"type": "turn_context", "payload": {
                    "turn_id": f"turn-{name}-{index}", "model": "gpt-5.6-luna", "effort": "high"}},
                {"type": "event_msg", "payload": {"type": "token_count", "info": {
                    "total_token_usage": cumulative, "last_token_usage": delta}}},
                {"type": "event_msg", "payload": {"type": "task_complete",
                    "turn_id": f"turn-{name}-{index}", "started_at": index, "completed_at": index + 1}},
            ])
        path = self.base / f"{name}.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        return {"style_ids": styles, "log_path": str(path),
                "expected_session_id": f"session-{name}", "expected_agent_path": f"/root/luna_{name}"}

    def alter(self, binding, callback):
        path = Path(binding["log_path"])
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        callback(rows)
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    def measure(self, styles, bindings, **kwargs):
        return measure_batch_tokens(self.root, "synthetic-run", styles, bindings, **kwargs)

    def test_batch_three_and_five_exact_totals_no_per_image_division(self):
        three = ["A", "B", "C"]
        five = ["D", "E", "F", "G", "H"]
        result = self.measure(three + five, [self.binding("three", three), self.binding("five", five)])
        receipt = result["receipt"]
        self.assertEqual(receipt["usage"]["total_tokens"], 240)
        self.assertEqual(receipt["observed_session_count"], 2)
        self.assertEqual(receipt["expected_image_count"], 8)
        self.assertEqual([row["image_count"] for row in receipt["sessions"]], [3, 5])
        self.assertTrue(all(row["usage"] is None and row["total_tokens"] is None for row in receipt["per_image"]))
        self.assertIsNone(receipt["actual_billed_tokens"])
        self.assertIsNone(receipt["actual_billed_cost"])
        self.assertEqual(result["external_api_calls"], 0)
        self.assertFalse((self.root / result["receipt_path"]).exists())

    def test_cache_write_partition_is_separate_and_subsets_not_added(self):
        result = self.measure(["A"], [self.binding("single", ["A"])])
        usage = result["usage"]
        self.assertEqual(usage["input_tokens_including_cached"], 100)
        self.assertEqual(usage["cached_input_tokens"], 60)
        self.assertEqual(usage["cache_write_input_tokens"], 10)
        self.assertEqual(usage["ordinary_input_tokens_calculated"], 30)
        self.assertEqual(usage["output_tokens_including_reasoning"], 20)
        self.assertEqual(usage["reasoning_output_tokens"], 5)
        self.assertEqual(usage["total_tokens"], 120)
        self.assertEqual(result["receipt"]["per_image"][0]["usage"], usage)

    def test_all_failure_retry_turns_count_in_continued_or_reset_logs(self):
        for reset in (False, True):
            with self.subTest(reset=reset):
                binding = self.binding(f"retry_{reset}", ["A", "B", "C"], reset=reset,
                                       deltas=[self.usage(), self.usage(50, 10, 5, 7, 2)])
                result = self.measure(["A", "B", "C"], [binding])
                self.assertEqual(result["usage"]["total_tokens"], 177)
                self.assertEqual(result["usage"]["ordinary_input_tokens_calculated"], 65)
                self.assertEqual(result["receipt"]["observed_turn_count"], 2)

    def test_rejects_style_session_log_and_turn_duplicates(self):
        first = self.binding("one", ["A"])
        second = self.binding("two", ["A"])
        with self.assertRaisesRegex(UsageError, "Repeated Style"):
            self.measure(["A"], [first, second])
        second["style_ids"] = ["B"]
        repeated_session = {**second, "expected_session_id": first["expected_session_id"]}
        with self.assertRaisesRegex(UsageError, "Repeated session"):
            self.measure(["A", "B"], [first, repeated_session])
        repeated_log = {**second, "log_path": first["log_path"]}
        with self.assertRaisesRegex(UsageError, "Repeated session"):
            self.measure(["A", "B"], [first, repeated_log])
        self.alter(second, lambda rows: [row["payload"].update(turn_id="turn-one-0") for row in rows
                                        if row["type"] == "turn_context" or row["payload"].get("type") == "task_complete"])
        with self.assertRaisesRegex(UsageError, "repeated turn"):
            self.measure(["A", "B"], [first, second])

    def test_rejects_duplicate_turn_within_one_session(self):
        binding = self.binding("repeated_turn", ["A"], deltas=[self.usage(), self.usage()])
        self.alter(binding, lambda rows: [row["payload"].update(turn_id="same-turn") for row in rows
                                         if row["type"] == "turn_context" or row["payload"].get("type") == "task_complete"])
        with self.assertRaisesRegex(UsageError, "repeated turn"):
            self.measure(["A"], [binding])

    def test_rejects_incomplete_coverage_duplicate_expected_and_oversized_batch(self):
        binding = self.binding("coverage", ["A"])
        with self.assertRaisesRegex(UsageError, "cover every"):
            self.measure(["A", "B"], [binding])
        with self.assertRaisesRegex(UsageError, "distinct"):
            self.measure(["A", "A"], [binding])
        styles = list("ABCDEF")
        with self.assertRaisesRegex(UsageError, "1–5"):
            self.measure(styles, [{**binding, "style_ids": styles}])

    def test_rejects_explicit_identity_mismatch_and_nonabsolute_log(self):
        binding = self.binding("identity", ["A"])
        for key in ("expected_session_id", "expected_agent_path"):
            changed = {**binding, key: "wrong" if key == "expected_session_id" else "/root/luna_wrong"}
            with self.subTest(key=key), self.assertRaisesRegex(UsageError, "does not match"):
                self.measure(["A"], [changed])
        with self.assertRaisesRegex(UsageError, "absolute"):
            self.measure(["A"], [{**binding, "log_path": "relative.jsonl"}])

    def test_missing_usage_fields_and_unobserved_log_fail_closed(self):
        for field in self.usage():
            with self.subTest(field=field):
                binding = self.binding(f"missing_{field}", ["A"])
                self.alter(binding, lambda rows: rows[2]["payload"]["info"]["last_token_usage"].pop(field))
                with self.assertRaises(UsageError):
                    self.measure(["A"], [binding])
        binding = self.binding("unobserved", ["A"])
        self.alter(binding, lambda rows: rows.pop(2))
        with self.assertRaisesRegex(UsageError, "observed"):
            self.measure(["A"], [binding])

    def test_rejects_negative_ordinary_input(self):
        binding = self.binding("negative", ["A"], deltas=[self.usage(cached=80, cache_write=30)])
        with self.assertRaisesRegex(UsageError, "ordinary input is negative"):
            self.measure(["A"], [binding])

    def test_duplicate_cumulative_events_do_not_double_count(self):
        binding = self.binding("duplicate_event", ["A", "B", "C"])
        self.alter(binding, lambda rows: rows.insert(3, dict(rows[2])))
        result = self.measure(["A", "B", "C"], [binding])
        self.assertEqual(result["usage"]["total_tokens"], 120)
        self.assertEqual(result["receipt"]["sessions"][0]["turns"][0]["duplicate_cumulative_events"], 1)

    def test_non_luna_and_incomplete_turn_logs_fail_closed(self):
        binding = self.binding("non_luna", ["A"])
        self.alter(binding, lambda rows: rows[1]["payload"].update(model="other-model"))
        with self.assertRaisesRegex(UsageError, "non-Luna"):
            self.measure(["A"], [binding])
        binding = self.binding("incomplete", ["A"])
        self.alter(binding, lambda rows: rows.pop())
        with self.assertRaisesRegex(UsageError, "incomplete"):
            self.measure(["A"], [binding])

    def test_receipt_apply_is_immutable_and_dryrun_has_no_writes(self):
        binding = self.binding("immutable", ["A", "B", "C"])
        styles = ["A", "B", "C"]
        first = self.measure(styles, [binding])
        target = self.root / first["receipt_path"]
        self.assertFalse(target.parent.exists())
        applied = self.measure(styles, [binding], apply=True)
        original = target.read_bytes()
        self.assertEqual(applied["status"], "prepared")
        self.assertEqual(self.measure(styles, [binding], apply=True)["status"], "unchanged")
        changed = self.binding("immutable", styles, deltas=[self.usage(output=30)])
        with self.assertRaisesRegex(UsageError, "immutable receipt"):
            self.measure(styles, [changed], apply=True)
        self.assertEqual(target.read_bytes(), original)

    def test_receipt_cannot_escape_run_or_replace_frozen_receipt(self):
        binding = self.binding("safe_path", ["A"])
        for path in ("../outside.json", "data/canonical/new.json",
                     "data/private-research/image-rag-admin/luna-analysis/other/new.json",
                     "data/private-research/image-rag-admin/luna-analysis/synthetic-run/token-usage-receipt.json"):
            with self.subTest(path=path), self.assertRaises(UsageError):
                self.measure(["A"], [binding], receipt_relative_path=path, apply=True)


if __name__ == "__main__":
    unittest.main()
