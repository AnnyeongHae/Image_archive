"""Synthetic exec/rollout usage evidence only; no model or credential access."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from image_rag_eval.luna_exec_usage import (
    ExecUsageError, aggregate_exec_attempts, bind_exec_attempt, inspect_exec_events, measure_rollout_turns,
)


class ExecUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def save(self, name, rows):
        path = self.root / name
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        return path

    def events(self, usage=None, *, failed=False):
        return [{"type": "thread.started", "thread_id": "session-A"}, {"type": "turn.started"},
                {"type": "turn.failed", "error": {"message": "fixture failure"}} if failed else
                {"type": "turn.completed", "usage": usage or {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 20}}]

    def bind(self, path, **kwargs):
        values = {"attempt_id": "attempt-A", "style_ids": ["A", "B", "C"], "expected_thread_id": "session-A"}
        values.update(kwargs)
        return bind_exec_attempt(path, **values)

    def test_cli_reported_missing_fields_stay_null(self):
        path = self.save("exec.jsonl", self.events())
        result = self.bind(path, counter_scope="per_turn")
        self.assertEqual(result["reported_usage"]["input_tokens"], 100)
        self.assertIsNone(result["reported_usage"]["reasoning_output_tokens"])
        self.assertIsNone(result["reported_usage"]["cache_write_input_tokens"])
        self.assertIsNone(result["reported_usage"]["total_tokens"])
        self.assertEqual(result["total_tokens_calculated"], 120)
        self.assertEqual(result["uncached_input_tokens_calculated"], 40)
        self.assertIsNone(result["ordinary_input_tokens_calculated"])
        self.assertIsNone(result["turn_id_reported"])
        self.assertTrue(all(value is None for value in result["per_image_tokens"].values()))
        self.assertIsNone(result["actual_billed_tokens"])

    def test_cli_unknown_scope_does_not_blindly_sum_resumed_usage(self):
        result = self.bind(self.save("unknown.jsonl", self.events()))
        self.assertEqual(result["reported_usage"]["input_tokens"], 100)
        self.assertIsNone(result["total_tokens_calculated"])
        self.assertEqual(result["attribution_method"], "unavailable_counter_scope")

    def test_cli_cumulative_baseline_explicit_difference(self):
        path = self.save("cumulative.jsonl", self.events())
        result = self.bind(path, counter_scope="thread_cumulative", baseline_source_id="previous-attempt",
                           baseline_reported_usage={"input_tokens": 70, "cached_input_tokens": 40, "output_tokens": 12})
        self.assertEqual(result["attributable_usage"]["input_tokens"], 30)
        self.assertEqual(result["total_tokens_calculated"], 38)
        self.assertIsNone(result["attributable_usage"]["cache_write_input_tokens"])
        with self.assertRaisesRegex(ExecUsageError, "provenance"):
            self.bind(path, counter_scope="thread_cumulative", baseline_reported_usage={"input_tokens": 0})

    def test_cli_failures_and_launch_failure_preserved_unknown(self):
        failed = self.bind(self.save("failed.jsonl", self.events(failed=True)), counter_scope="per_turn", runner_exit_code=1)
        self.assertEqual(failed["turn_status_reported"], "failed")
        self.assertIsNone(failed["total_tokens_calculated"])
        self.assertEqual(failed["errors_reported"][0]["message_reported"], "fixture failure")
        launch = bind_exec_attempt(self.save("empty.jsonl", [{"type": "error", "message": "not authenticated"}]),
                                   attempt_id="launch", style_ids=["A"], runner_exit_code=1)
        self.assertEqual(launch["turn_status_reported"], "not_started")
        self.assertIsNone(launch["thread_id_reported"])
        completed = self.bind(self.save("completed.jsonl", self.events()), counter_scope="per_turn", attempt_id="completed")
        aggregate = aggregate_exec_attempts([completed, failed, launch])
        self.assertIsNone(aggregate["usage_complete"]["total_tokens"])
        self.assertEqual(aggregate["known_usage_subtotals"]["total_tokens"], 120)
        self.assertEqual(aggregate["unknown_attempt_counts"]["total_tokens"], 2)

    def test_cli_duplicate_capture_or_attempt_rejected(self):
        result = self.bind(self.save("dup.jsonl", self.events()), counter_scope="per_turn")
        with self.assertRaisesRegex(ExecUsageError, "Repeated"):
            aggregate_exec_attempts([result, result])
        copy = {**result, "attempt_id": "another-id"}
        with self.assertRaisesRegex(ExecUsageError, "Repeated"):
            aggregate_exec_attempts([result, copy])

    def test_cli_cannot_invent_real_turn_id_or_accept_invalid_counters(self):
        path = self.save("identity.jsonl", self.events())
        with self.assertRaisesRegex(ExecUsageError, "Real turn ID"):
            self.bind(path, expected_turn_id="guessed-turn")
        with self.assertRaisesRegex(ExecUsageError, "thread"):
            self.bind(path, expected_thread_id="other")
        invalid = self.events({"input_tokens": 100, "cached_input_tokens": 90, "cache_write_input_tokens": 20, "output_tokens": 1})
        with self.assertRaisesRegex(ExecUsageError, "subset"):
            inspect_exec_events(self.save("invalid.jsonl", invalid))

    def test_cli_cumulative_reset_is_unknown_not_negative_cost(self):
        result = self.bind(self.save("reset.jsonl", self.events()), counter_scope="thread_cumulative",
                           baseline_source_id="prior", baseline_reported_usage={"input_tokens": 500, "output_tokens": 80})
        self.assertEqual(result["attribution_method"], "unavailable_counter_reset")
        self.assertIsNone(result["total_tokens_calculated"])


class RolloutTurnUsageTests(unittest.TestCase):
    setUp = ExecUsageTests.setUp
    save = ExecUsageTests.save

    @staticmethod
    def usage(input_count=100, cached=60, write=10, output=20, reasoning=5):
        return {"input_tokens": input_count, "cached_input_tokens": cached, "cache_write_input_tokens": write,
                "output_tokens": output, "reasoning_output_tokens": reasoning, "total_tokens": input_count + output}

    def rollout(self, *, reset=False, absent_optional=False, duplicate=False, unfinished=False):
        rows = [{"type": "session_meta", "payload": {"id": "session-A", "agent_path": "/root/luna_case_343"}}]
        cumulative = {field: 0 for field in self.usage()}
        for ident, delta in (("historic", self.usage()), ("new-turn", self.usage(60, 30, 5, 8, 2))):
            if absent_optional:
                delta = {key: value for key, value in delta.items() if key not in {"cache_write_input_tokens", "reasoning_output_tokens"}}
            cumulative = dict(delta) if reset else {key: cumulative[key] + value for key, value in delta.items()}
            rows.extend([
                {"type": "turn_context", "payload": {"turn_id": ident, "model": "gpt-5.6-luna", "effort": "high"}},
                {"type": "event_msg", "payload": {"type": "token_count", "info": {
                    "total_token_usage": cumulative, "last_token_usage": delta}}},
            ])
            if duplicate:
                rows.append(dict(rows[-1]))
            if not (unfinished and ident == "new-turn"):
                rows.append({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": ident}})
        return self.save("rollout.jsonl", rows)

    def scoped(self, path, **kwargs):
        values = {"expected_session_id": "session-A", "expected_agent_path": "/root/luna_case_343",
                  "turn_bindings": [{"turn_id": "new-turn", "style_ids": [f"CASE-{i:03}" for i in range(30)],
                                      "batch_ids": [f"batch-{i:03}" for i in range(10)]}],
                  "excluded_turn_ids": ["historic"]}
        values.update(kwargs)
        return measure_rollout_turns(path, **values)

    def test_named_current_turn_excludes_historic_and_shared_allocations_null(self):
        result = self.scoped(self.rollout())
        self.assertEqual(result["usage"]["input_tokens"], 60)
        self.assertEqual(result["usage"]["output_tokens"], 8)
        self.assertEqual(result["usage"]["total_tokens_calculated"], 68)
        self.assertEqual(result["usage"]["ordinary_input_tokens_calculated"], 25)
        self.assertEqual(result["bound_image_count_unique"], 30)
        self.assertEqual(result["turns"][0]["counter_mode"], "continued_across_turns")
        self.assertTrue(all(row["total_tokens_calculated"] is None for row in result["per_batch"] + result["per_image"]))

    def test_reset_on_followup_and_duplicate_events_count_once(self):
        result = self.scoped(self.rollout(reset=True, duplicate=True))
        self.assertEqual(result["usage"]["total_tokens_calculated"], 68)
        self.assertEqual(result["turns"][0]["counter_mode"], "reset_at_turn_start")
        self.assertEqual(result["turns"][0]["duplicate_cumulative_event_count"], 1)

    def test_optional_absent_fields_remain_unknown_not_zero(self):
        result = self.scoped(self.rollout(absent_optional=True))
        self.assertEqual(result["usage"]["total_tokens_calculated"], 68)
        self.assertIsNone(result["usage"]["cache_write_input_tokens"])
        self.assertIsNone(result["usage"]["reasoning_output_tokens"])
        self.assertIsNone(result["usage"]["ordinary_input_tokens_calculated"])

    def test_selected_turn_must_be_completed_and_not_excluded(self):
        with self.assertRaisesRegex(ExecUsageError, "unfinished"):
            self.scoped(self.rollout(unfinished=True))
        with self.assertRaisesRegex(ExecUsageError, "historically excluded"):
            self.scoped(self.rollout(), excluded_turn_ids=["new-turn"])
        with self.assertRaisesRegex(ExecUsageError, "identity"):
            self.scoped(self.rollout(), expected_session_id="other-session")

    def test_completed_prefix_stable_when_later_turn_or_partial_line_appended(self):
        path = self.rollout()
        original = self.scoped(path)
        with path.open("ab") as stream:
            stream.write(b'{"type":"turn_context","payload":')
        self.assertEqual(self.scoped(path), original)

    def test_streams_only_selected_prefix_even_if_entire_file_exceeds_cap(self):
        path = self.rollout()
        original = self.scoped(path)
        with path.open("ab") as stream:
            stream.write(b"not read irrelevant trailing bytes" * 10000)
        with patch("image_rag_eval.luna_exec_usage.MAX_ROLLOUT_PREFIX_BYTES", original["source_prefix_bytes"]), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read forbidden")):
            self.assertEqual(self.scoped(path), original)
        with patch("image_rag_eval.luna_exec_usage.MAX_ROLLOUT_PREFIX_BYTES", original["source_prefix_bytes"] - 1):
            with self.assertRaisesRegex(ExecUsageError, "Consumed rollout prefix"):
                self.scoped(path)

    def test_multiple_current_turns_include_retry_work_and_reject_repeat_binding(self):
        path = self.rollout()
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        retry_usage = self.usage(10, 2, 1, 4, 1)
        rows.extend([
            {"type": "turn_context", "payload": {"turn_id": "retry", "model": "gpt-5.6-luna"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": retry_usage, "last_token_usage": retry_usage}}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "retry"}},
        ])
        path = self.save("retry.jsonl", rows)
        bindings = [{"turn_id": "new-turn", "style_ids": ["A", "B"], "batch_ids": ["batch-001"]},
                    {"turn_id": "retry", "style_ids": ["A", "B"], "batch_ids": ["batch-001"]}]
        result = self.scoped(path, turn_bindings=bindings)
        self.assertEqual(result["usage"]["total_tokens_calculated"], 82)
        self.assertEqual(result["bound_image_count_unique"], 2)
        self.assertEqual(len(result["per_batch"]), 2)
        with self.assertRaisesRegex(ExecUsageError, "repeated"):
            self.scoped(path, turn_bindings=[bindings[0], bindings[0]])

    def test_bad_delta_crosscheck_is_not_accepted(self):
        path = self.rollout()
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[-2]["payload"]["info"]["last_token_usage"]["input_tokens"] = 59
        rows[-2]["payload"]["info"]["last_token_usage"]["total_tokens"] = 67
        with self.assertRaisesRegex(ExecUsageError, "delta"):
            self.scoped(self.save("mismatch.jsonl", rows))

    def refreshed_rows(self):
        rows = [json.loads(line) for line in self.rollout().read_text().splitlines()]
        context = rows[-3]
        cumulative = dict(rows[-2]["payload"]["info"]["total_token_usage"])
        snapshot = {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": cumulative,
            "last_token_usage": {**self.usage(0, 0, 0, 0, 0), "total_tokens": 29193},
        }}}
        rows[-1:-1] = [{"type": "compacted", "payload": {"window_number": 1}}, context, snapshot]
        return rows

    def test_same_turn_context_refresh_preserves_usage_and_start_boundary(self):
        rows = [json.loads(line) for line in self.rollout().read_text().splitlines()]
        baseline = self.scoped(self.save("baseline.jsonl", rows))
        rows[-1:-1] = [rows[-3]]
        result = self.scoped(self.save("refresh.jsonl", rows))
        self.assertEqual(result["usage"], baseline["usage"])
        self.assertEqual(result["turns"][0]["start_line"], baseline["turns"][0]["start_line"])
        self.assertEqual(result["turns"][0]["usage_event_count"], baseline["turns"][0]["usage_event_count"])
        self.assertEqual(result["turns"][0]["context_refresh_lines_reported"], [7])
        self.assertNotIn("context_refresh_lines_reported", baseline["turns"][0])
        self.assertNotIn("compaction_usage_snapshots_reported", result["turns"][0])

    def test_post_compaction_snapshot_keeps_reported_context_total_without_adding_usage(self):
        rows = self.refreshed_rows()
        extra = self.usage(10, 2, 1, 4, 1)
        previous = rows[-2]["payload"]["info"]["total_token_usage"]
        cumulative = {field: previous[field] + extra[field] for field in extra}
        rows[-1:-1] = [{"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": cumulative, "last_token_usage": extra}}}]
        result = self.scoped(self.save("compacted.jsonl", rows))
        turn = result["turns"][0]
        self.assertEqual(result["usage"]["total_tokens_calculated"], 82)
        self.assertEqual(result["usage"]["input_tokens"], 70)
        self.assertEqual(result["usage"]["output_tokens"], 12)
        self.assertEqual(turn["duplicate_cumulative_event_count"], 1)
        self.assertEqual(turn["usage_event_count"], 3)
        self.assertEqual(turn["context_refresh_lines_reported"], [8])
        evidence = turn["compaction_usage_snapshots_reported"][0]
        self.assertEqual((evidence["compaction_line"], evidence["context_refresh_line"], evidence["line"]), (7, 8, 9))
        self.assertEqual(evidence["last_token_usage_reported"]["total_tokens"], 29193)
        self.assertFalse(evidence["contributes_to_usage_delta"])
        self.assertIn("does not establish zero compaction cost", evidence["note"])
        self.assertIsNone(result["actual_billed_tokens"])
        self.assertIsNone(result["actual_billed_cost"])

    def test_refresh_other_turn_model_or_effort_drift_rejected(self):
        for field, value in (("turn_id", "overlapping-turn"), ("model", "gpt-5.6-sol"), ("effort", "low")):
            with self.subTest(field=field):
                rows = self.refreshed_rows()
                rows[-3] = {"type": "turn_context", "payload": {**rows[-3]["payload"], field: value}}
                with self.assertRaisesRegex(ExecUsageError, "overlaps another turn or changes"):
                    self.scoped(self.save("drift.jsonl", rows))

    def test_identical_post_compaction_snapshot_repeat_is_evidence_not_usage(self):
        rows = self.refreshed_rows()
        duplicate = json.loads(json.dumps(rows[-2]))
        rows[-1:-1] = [{"type": "response_item", "payload": {"type": "message"}}, duplicate]
        result = self.scoped(self.save("duplicate-compaction.jsonl", rows))
        turn = result["turns"][0]
        self.assertEqual(result["usage"]["total_tokens_calculated"], 68)
        self.assertEqual(turn["duplicate_cumulative_event_count"], 2)
        self.assertEqual(turn["compaction_usage_snapshots_reported"][1]["identical_repeat_of_line"], 9)

    def test_changed_or_late_compaction_snapshot_repeat_still_rejected(self):
        for kind in ("changed_total", "coherent_event", "new_context"):
            with self.subTest(kind=kind):
                rows = self.refreshed_rows()
                duplicate = json.loads(json.dumps(rows[-2]))
                inserted = []
                if kind == "changed_total":
                    duplicate["payload"]["info"]["last_token_usage"]["total_tokens"] += 1
                elif kind == "coherent_event":
                    inserted.append({"type": "event_msg", "payload": {"type": "token_count", "info": {
                        "total_token_usage": duplicate["payload"]["info"]["total_token_usage"],
                        "last_token_usage": self.usage(0, 0, 0, 0, 0)}}})
                else:
                    inserted.append(rows[-3])
                rows[-1:-1] = inserted + [duplicate]
                with self.assertRaises(ExecUsageError):
                    self.scoped(self.save("bad-repeat.jsonl", rows))

    def test_completed_turn_context_reuse_is_still_rejected(self):
        rows = [json.loads(line) for line in self.rollout().read_text().splitlines()]
        rows.insert(4, rows[1])
        with self.assertRaisesRegex(ExecUsageError, "repeated actual rollout turn"):
            self.scoped(self.save("reused.jsonl", rows))

    def test_special_snapshot_requires_compaction_refresh_and_exact_observations(self):
        for invalidity in ("no_compaction", "no_refresh", "nonzero_last", "changed_cumulative", "unknown_counter", "later_snapshot", "intervening_usage"):
            with self.subTest(invalidity=invalidity):
                rows = self.refreshed_rows()
                snapshot = rows[-2]["payload"]["info"]
                if invalidity == "no_compaction":
                    del rows[-4]
                elif invalidity == "no_refresh":
                    del rows[-3]
                elif invalidity == "nonzero_last":
                    snapshot["last_token_usage"]["input_tokens"] = 1
                elif invalidity == "changed_cumulative":
                    snapshot["total_token_usage"]["input_tokens"] += 1
                    snapshot["total_token_usage"]["total_tokens"] += 1
                elif invalidity == "unknown_counter":
                    snapshot["total_token_usage"]["cache_write_input_tokens"] = None
                else:
                    # Only the first token event after the refresh is eligible.
                    coherent = {"type": "event_msg", "payload": {"type": "token_count", "info": {
                        "total_token_usage": snapshot["total_token_usage"],
                        "last_token_usage": self.usage(0, 0, 0, 0, 0)}}}
                    rows.insert(len(rows) - (3 if invalidity == "intervening_usage" else 2), coherent)
                with self.assertRaises(ExecUsageError):
                    self.scoped(self.save("invalid-snapshot.jsonl", rows))

    def test_cumulative_reset_inside_refreshed_turn_still_fails(self):
        rows = self.refreshed_rows()
        reset = self.usage(10, 2, 1, 4, 1)
        rows[-2]["payload"]["info"] = {"total_token_usage": reset, "last_token_usage": reset}
        with self.assertRaisesRegex(ExecUsageError, "decreased inside a turn"):
            self.scoped(self.save("reset-after-refresh.jsonl", rows))


if __name__ == "__main__":
    unittest.main()
