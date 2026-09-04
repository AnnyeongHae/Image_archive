"""Synthetic full-library turn receipts; never read actual sessions or a DB."""
from __future__ import annotations

import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import measure_luna_full_turn as wrapper
from image_rag_eval.luna_exec_usage import ExecUsageError, measure_rollout_turns as real_rollout_meter


TURN = "01a068a3-d712-7ab0-a94d-86800680ab76"
OTHER_SESSION = "11111111-2222-4333-8444-555555555555"
OTHER_AGENT = "/root/luna_full_batch018"


class FullTurnReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = self.root / "synthetic-not-read.jsonl"
        self.output = self.root / wrapper.BASE / "execution" / f"{TURN}.tokens.json"
        self.manifest = {"batches": [
            {"batch_id": "batch-001", "style_ids": ["CASE-001", "CASE-002", "CASE-003"]},
            {"batch_id": "batch-002", "style_ids": ["CASE-004", "CASE-005", "CASE-006"]},
        ]}
        self.manifest_raw = wrapper.encode(self.manifest)
        self.usage = {
            "input_tokens": 100, "cached_input_tokens": 60,
            "cache_write_input_tokens": None, "output_tokens": 20,
            "reasoning_output_tokens": None, "total_tokens_calculated": 120,
            "uncached_input_tokens_calculated": 40,
            "ordinary_input_tokens_calculated": None,
        }
        self.receipt = {
            "schema_version": "image-luna-rollout-turn-usage-receipt-1",
            "session_id_reported": wrapper.SESSION_ID,
            "agent_path_reported": "/root/luna_case_343",
            "source_log_name": self.log.name,
            "source_prefix_sha256": "a" * 64,
            "source_prefix_line_count": 10,
            "source_prefix_bytes": 1000,
            "scope": "explicit_completed_turn_ids_only",
            "turn_ids": [TURN],
            "excluded_historical_turn_ids": [wrapper.HISTORICAL_TURN],
            "usage": self.usage,
            "actual_billed_tokens": None,
            "actual_billed_cost": None,
        }
        manifest_patch = patch.object(wrapper, "read_manifest", return_value=(self.manifest, self.manifest_raw))
        meter_patch = patch.object(wrapper, "measure_rollout_turns", side_effect=lambda *a, **k: copy.deepcopy(self.receipt))
        progress_patch = patch.object(wrapper, "validate_progress", return_value={"completed_styles": ["CASE-001"]})
        self.manifest_mock = manifest_patch.start()
        self.meter_mock = meter_patch.start()
        self.progress_mock = progress_patch.start()
        self.addCleanup(manifest_patch.stop)
        self.addCleanup(meter_patch.stop)
        self.addCleanup(progress_patch.stop)

    def measure(self, batch_ids=None, **kwargs):
        return wrapper.measure(self.root, self.log, TURN, ["batch-001"] if batch_ids is None else batch_ids, **kwargs)

    def test_duplicate_empty_and_unknown_batches_rejected_before_meter(self):
        for batch_ids in ([], ["batch-001", "batch-001"], ["batch-999"]):
            with self.subTest(batch_ids=batch_ids), self.assertRaisesRegex(ValueError, "unique assigned batch"):
                self.measure(batch_ids, apply=True)
        self.meter_mock.assert_not_called()
        self.progress_mock.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_explicit_bindings_preserve_assignment_order_and_exclude_history(self):
        result = self.measure(["batch-002", "batch-001"])
        self.meter_mock.assert_called_once_with(
            self.log, expected_session_id=wrapper.SESSION_ID,
            expected_agent_path="/root/luna_case_343",
            turn_bindings=[{"turn_id": TURN,
                            "style_ids": ["CASE-004", "CASE-005", "CASE-006", "CASE-001", "CASE-002", "CASE-003"],
                            "batch_ids": ["batch-002", "batch-001"]}],
            excluded_turn_ids=[wrapper.HISTORICAL_TURN],
        )
        self.assertEqual(result["bound_images"], 6)
        self.assertNotIn(wrapper.HISTORICAL_TURN, self.meter_mock.call_args.kwargs["turn_bindings"][0]["turn_id"])

    def test_explicit_turn_and_absolute_log_required(self):
        for log, turn in ((Path("relative.jsonl"), TURN), (self.log, "not-a-turn-id"), (self.log, "-" * 36)):
            with self.subTest(log=log, turn=turn), self.assertRaisesRegex(ValueError, "Explicit turn UUID"):
                wrapper.measure(self.root, log, turn, ["batch-001"], apply=True)
        self.manifest_mock.assert_not_called()
        self.meter_mock.assert_not_called()

    def test_session_override_pair_forwarded_without_inferred_history(self):
        self.measure(expected_session_id=OTHER_SESSION, expected_agent_path=OTHER_AGENT)
        arguments = self.meter_mock.call_args.kwargs
        self.assertEqual(arguments["expected_session_id"], OTHER_SESSION)
        self.assertEqual(arguments["expected_agent_path"], OTHER_AGENT)
        self.assertEqual(arguments["excluded_turn_ids"], [])
        self.assertEqual(arguments["turn_bindings"][0]["turn_id"], TURN)

    def test_original_explicit_override_keeps_historical_exclusion(self):
        self.measure(expected_session_id=wrapper.SESSION_ID, expected_agent_path=wrapper.AGENT_PATH)
        self.assertEqual(self.meter_mock.call_args.kwargs["excluded_turn_ids"], [wrapper.HISTORICAL_TURN])

    def test_unpaired_or_invalid_overrides_rejected_before_inputs(self):
        invalid = [
            {"expected_session_id": OTHER_SESSION},
            {"expected_agent_path": OTHER_AGENT},
            {"expected_session_id": "-" * 36, "expected_agent_path": OTHER_AGENT},
            {"expected_session_id": "", "expected_agent_path": OTHER_AGENT},
            {"expected_session_id": 123, "expected_agent_path": OTHER_AGENT},
        ]
        for agent in ("/root/other", "/root/luna_", "/root/luna_a/../b", "/root/luna_a/child", "root/luna_a", "/root/luna_a\n", None, 123):
            invalid.append({"expected_session_id": OTHER_SESSION, "expected_agent_path": agent})
        for overrides in invalid:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.measure(apply=True, **overrides)
        self.manifest_mock.assert_not_called()
        self.meter_mock.assert_not_called()
        self.progress_mock.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_historical_turn_globally_rejected_for_default_or_new_session(self):
        for overrides in ({}, {"expected_session_id": OTHER_SESSION, "expected_agent_path": OTHER_AGENT}):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(ValueError, "historical turn"):
                wrapper.measure(self.root, self.log, wrapper.HISTORICAL_TURN, ["batch-001"], apply=True, **overrides)
        self.manifest_mock.assert_not_called()
        self.meter_mock.assert_not_called()

    def synthetic_rollout(self, *, session_id=OTHER_SESSION, agent_path=OTHER_AGENT,
                          turn_id=TURN, model="gpt-5.6-luna"):
        usage = {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 20, "total_tokens": 120}
        rows = [
            {"type": "session_meta", "payload": {"id": session_id, "agent_path": agent_path}},
            {"type": "turn_context", "payload": {"turn_id": turn_id, "model": model, "effort": "high"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": usage, "last_token_usage": usage}}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn_id}},
        ]
        self.log.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    def test_new_session_uses_real_meter_identity_model_and_turn_checks(self):
        self.meter_mock.side_effect = real_rollout_meter
        overrides = {"expected_session_id": OTHER_SESSION, "expected_agent_path": OTHER_AGENT}
        for wrong in ({"session_id": wrapper.SESSION_ID}, {"agent_path": wrapper.AGENT_PATH},
                      {"model": "gpt-5.6-sol"}, {"turn_id": "22222222-2222-4333-8444-555555555555"}):
            with self.subTest(wrong=wrong):
                self.synthetic_rollout(**wrong)
                with self.assertRaises(ExecUsageError):
                    self.measure(apply=True, **overrides)
                self.assertFalse(self.output.exists())
        self.progress_mock.assert_not_called()
        self.synthetic_rollout()
        result = self.measure(apply=True, **overrides)
        self.assertEqual(result["status"], "recorded")
        self.assertEqual(result["usage"]["total_tokens_calculated"], 120)
        saved = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(saved["session_id_reported"], OTHER_SESSION)
        self.assertEqual(saved["agent_path_reported"], OTHER_AGENT)
        self.assertEqual(saved["excluded_historical_turn_ids"], [])
        self.assertEqual(saved["turns"][0]["model_reported"], "gpt-5.6-luna")
        self.assertTrue(all(row["total_tokens_calculated"] is None for row in saved["per_image"]))
        initial = self.output.read_bytes()
        self.progress_mock.reset_mock()
        self.assertEqual(self.measure(apply=True, **overrides)["status"], "unchanged")
        self.assertEqual(self.output.read_bytes(), initial)
        self.progress_mock.assert_not_called()

    def test_cli_forwards_override_pair_and_preserves_dry_run_default(self):
        base = ["--log", str(self.log), "--turn-id", TURN, "--batch-id", "batch-001"]
        for arguments, expected in (
            ([], {"apply": False, "expected_session_id": None, "expected_agent_path": None}),
            (["--apply", "--expected-session-id", OTHER_SESSION, "--expected-agent-path", OTHER_AGENT],
             {"apply": True, "expected_session_id": OTHER_SESSION, "expected_agent_path": OTHER_AGENT}),
        ):
            with self.subTest(arguments=arguments), patch.object(wrapper, "measure", return_value={"status": "mocked"}) as measure:
                with redirect_stdout(io.StringIO()) as output:
                    wrapper.main(base + arguments)
                self.assertEqual(measure.call_args.args[1:], (self.log, TURN, ["batch-001"]))
                self.assertEqual(measure.call_args.kwargs, expected)
                self.assertEqual(json.loads(output.getvalue()), {"status": "mocked"})

    def test_cli_rejects_unpaired_override_before_reading_manifest(self):
        base = ["--log", str(self.log), "--turn-id", TURN, "--batch-id", "batch-001", "--apply"]
        for arguments in (["--expected-session-id", OTHER_SESSION], ["--expected-agent-path", OTHER_AGENT]):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, "supplied together"):
                wrapper.main(base + arguments)
        self.manifest_mock.assert_not_called()
        self.meter_mock.assert_not_called()
        self.progress_mock.assert_not_called()

    def test_dry_run_default_does_not_create_receipt(self):
        result = self.measure()
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["validated_images"], 1)
        self.assertEqual(result["usage"], self.usage)
        self.assertIsNone(result["actual_billed_tokens"])
        self.assertIsNone(result["actual_billed_cost"])
        self.assertFalse(self.output.exists())
        self.assertFalse(self.log.exists())

    def test_apply_records_original_progress_and_unknown_usage_without_allocation(self):
        self.progress_mock.return_value = {"completed_styles": ["CASE-001", "OTHER-RUN-001"]}
        result = self.measure(apply=True)
        saved = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "recorded")
        self.assertEqual(saved["analysis_run_id"], wrapper.RUN)
        self.assertEqual(saved["task_manifest_sha256"], wrapper.digest(self.manifest_raw))
        self.assertEqual(saved["assigned_styles"], ["CASE-001", "CASE-002", "CASE-003"])
        self.assertEqual(saved["schema_valid_output_styles"], ["CASE-001"])
        self.assertEqual(saved["output_incomplete_styles"], ["CASE-002", "CASE-003"])
        self.assertEqual(saved["usage"], self.usage)
        self.assertFalse(saved["metadata_human_approved"])
        self.assertFalse(saved["release_eligible"])
        self.assertIsNone(saved["actual_billed_tokens"])
        self.assertIsNone(saved["actual_billed_cost"])

    def test_existing_receipt_keeps_older_coverage_after_normalized_progress(self):
        self.measure(apply=True)
        initial = self.output.read_bytes()
        self.progress_mock.reset_mock()
        self.progress_mock.return_value = {"completed_styles": ["CASE-001", "CASE-002", "CASE-003"],
                                           "normalized_styles": ["CASE-002", "CASE-003"]}
        for apply in (False, True):
            with self.subTest(apply=apply):
                result = self.measure(apply=apply)
                self.assertEqual(result["status"], "unchanged")
                self.assertEqual(result["validated_images_at_receipt"], 1)
                self.assertNotIn("validated_images", result)
                self.assertEqual(self.output.read_bytes(), initial)
        self.progress_mock.assert_not_called()

    def test_invalid_saved_coverage_rejected_without_recalculation_or_write(self):
        self.measure(apply=True)
        original = json.loads(self.output.read_text(encoding="utf-8"))
        self.progress_mock.reset_mock()
        mutations = [
            {"schema_valid_output_styles": ["CASE-001", "CASE-001"]},
            {"output_incomplete_styles": ["CASE-002", "CASE-002", "CASE-003"]},
            {"schema_valid_output_styles": ["CASE-001", "CASE-002"]},
            {"output_incomplete_styles": ["CASE-002"]},
            {"schema_valid_output_styles": ["OTHER-001"]},
            {"output_incomplete_styles": ["CASE-002", "OTHER-001"]},
            {"schema_valid_output_styles": None},
            {"output_incomplete_styles": "CASE-002"},
            {"schema_valid_output_styles": [{"style_id": "CASE-001"}]},
            {"output_incomplete_styles": ["CASE-002", 3]},
        ]
        for fields in mutations:
            for apply in (False, True):
                with self.subTest(fields=fields, apply=apply):
                    invalid = {**original, **fields}
                    raw = wrapper.encode(invalid)
                    self.output.write_bytes(raw)
                    with self.assertRaisesRegex(ValueError, "coverage"):
                        self.measure(apply=apply)
                    self.assertEqual(self.output.read_bytes(), raw)
        for missing in ("schema_valid_output_styles", "output_incomplete_styles"):
            with self.subTest(missing=missing):
                invalid = {k: v for k, v in original.items() if k != missing}
                raw = wrapper.encode(invalid)
                self.output.write_bytes(raw)
                with self.assertRaisesRegex(ValueError, "coverage"):
                    self.measure(apply=True)
                self.assertEqual(self.output.read_bytes(), raw)
        self.progress_mock.assert_not_called()

    def test_saved_approval_flags_must_be_literal_false(self):
        self.measure(apply=True)
        original = json.loads(self.output.read_text(encoding="utf-8"))
        self.progress_mock.reset_mock()
        for key in ("metadata_human_approved", "release_eligible"):
            for value in (True, None, 0, "false", [], {}):
                with self.subTest(key=key, value=value):
                    invalid = {**original, key: value}
                    raw = wrapper.encode(invalid)
                    self.output.write_bytes(raw)
                    with self.assertRaisesRegex(ValueError, "approval flags"):
                        self.measure(apply=True)
                    self.assertEqual(self.output.read_bytes(), raw)
            with self.subTest(missing=key):
                invalid = {k: v for k, v in original.items() if k != key}
                raw = wrapper.encode(invalid)
                self.output.write_bytes(raw)
                with self.assertRaisesRegex(ValueError, "approval flags"):
                    self.measure(apply=True)
                self.assertEqual(self.output.read_bytes(), raw)
        self.progress_mock.assert_not_called()

    def test_saved_all_pending_or_all_validated_partition_remains_unchanged(self):
        self.measure(apply=True)
        original = json.loads(self.output.read_text(encoding="utf-8"))
        self.progress_mock.reset_mock()
        assigned = original["assigned_styles"]
        for validated, incomplete in (([], assigned), (assigned, [])):
            with self.subTest(validated=validated):
                saved = {**original, "schema_valid_output_styles": validated,
                         "output_incomplete_styles": incomplete}
                raw = wrapper.encode(saved)
                self.output.write_bytes(raw)
                result = self.measure(apply=True)
                self.assertEqual(result["status"], "unchanged")
                self.assertEqual(result["validated_images_at_receipt"], len(validated))
                self.assertEqual(self.output.read_bytes(), raw)
        self.progress_mock.assert_not_called()

    def test_changed_verified_usage_cannot_overwrite_existing_receipt(self):
        self.measure(apply=True)
        initial = self.output.read_bytes()
        self.receipt["usage"]["input_tokens"] = 101
        with self.assertRaisesRegex(ValueError, "differs from verified execution"):
            self.measure(apply=True)
        self.assertEqual(self.output.read_bytes(), initial)

    def test_changed_source_manifest_cannot_overwrite_existing_receipt(self):
        self.measure(apply=True)
        initial = self.output.read_bytes()
        self.manifest_mock.return_value = (self.manifest, b"changed frozen manifest")
        with self.assertRaisesRegex(ValueError, "differs from verified execution"):
            self.measure(apply=True)
        self.assertEqual(self.output.read_bytes(), initial)

    def test_rebinding_turn_to_other_batch_cannot_overwrite_existing_receipt(self):
        self.measure(apply=True)
        initial = self.output.read_bytes()
        with self.assertRaisesRegex(ValueError, "differs from verified execution"):
            self.measure(["batch-002"], apply=True)
        self.assertEqual(self.output.read_bytes(), initial)

    def test_unfinished_or_unobserved_turn_does_not_create_receipt(self):
        self.meter_mock.side_effect = ExecUsageError("Selected turn absent or unfinished")
        with self.assertRaisesRegex(ExecUsageError, "absent or unfinished"):
            self.measure(apply=True)
        self.progress_mock.assert_not_called()
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
