"""Read-only accounting for explicitly bound ``codex exec --json`` turns.

Source audit (CLI 0.147.0, no model calls):
https://github.com/openai/codex/blob/rust-v0.147.0/codex-rs/exec/src/exec_events.rs
https://github.com/openai/codex/blob/rust-v0.147.0/codex-rs/exec/src/event_processor_with_jsonl_output.rs

The JSONL stream omits real turn IDs. The processor's usage_from_last_total
copies thread cumulative counters; therefore resumed captures cannot safely be
summed as per-turn usage without an explicit baseline. CLI-reported zeros can
also be defaults upstream. This module labels reported/calculated data, leaves
missing fields null, and never equates telemetry with billed usage.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
          "output_tokens", "reasoning_output_tokens")
MAX_CAPTURE_BYTES = 100 * 1024 * 1024
MAX_ROLLOUT_PREFIX_BYTES = 1024 * 1024 * 1024
MAX_ROLLOUT_LINE_BYTES = 16 * 1024 * 1024
ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")


class ExecUsageError(ValueError):
    """Ambiguous identity or malformed evidence, never a fabricated zero."""


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ExecUsageError("Duplicate JSON field")
        value[key] = item
    return value


def _row(line: bytes) -> dict:
    try:
        value = json.loads(line.decode("utf-8-sig"), object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ExecUsageError("Non-finite JSON")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ExecUsageError("Malformed exec JSONL evidence") from exc
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise ExecUsageError("Exec JSONL event must have a string type")
    return value


def _reported_usage(value) -> dict:
    if value is not None and not isinstance(value, dict):
        raise ExecUsageError("Reported usage must be an object or null")
    value = value or {}
    result = {}
    for field in (*FIELDS, "total_tokens"):
        amount = value.get(field)
        if amount is not None and (type(amount) is not int or amount < 0):
            raise ExecUsageError("Reported token fields must be nonnegative integers or null")
        result[field] = amount
    _check_subsets(result)
    return result


def _check_subsets(value: dict) -> None:
    input_count, cached = value["input_tokens"], value["cached_input_tokens"]
    write, output, reasoning = value["cache_write_input_tokens"], value["output_tokens"], value["reasoning_output_tokens"]
    if (input_count is not None and cached is not None and cached > input_count
            or output is not None and reasoning is not None and reasoning > output
            or all(part is not None for part in (input_count, cached, write)) and cached + write > input_count):
        raise ExecUsageError("Reported/calculated token subset exceeds its parent")
    total = value.get("total_tokens")
    if all(part is not None for part in (input_count, output, total)) and total != input_count + output:
        raise ExecUsageError("Reported total conflicts with input plus output")


def _error(row: dict, line_number: int) -> dict:
    value = row.get("error")
    message = value.get("message") if isinstance(value, dict) else row.get("message")
    return {"line": line_number, "type": row["type"], "message_reported": message if isinstance(message, str) else None}


def inspect_exec_events(path: Path) -> dict:
    """Parse an explicit capture; no guessing session IDs from its filename."""
    path = Path(path).resolve()
    if not path.is_file() or path.stat().st_size > MAX_CAPTURE_BYTES:
        raise ExecUsageError("Exec JSONL capture is missing or oversized")
    raw = path.read_bytes()
    thread_id = None
    turns, before_turn_errors = [], []
    active = None
    for number, line in enumerate(raw.splitlines(), 1):
        row = _row(line)
        event = row["type"]
        if event == "thread.started":
            if thread_id is not None or active is not None or turns:
                raise ExecUsageError("Repeated or late thread identity")
            thread_id = row.get("thread_id")
            if not isinstance(thread_id, str) or not thread_id.strip():
                raise ExecUsageError("Missing reported thread identity")
        elif event == "turn.started":
            if active is not None:
                raise ExecUsageError("A second turn started before the prior terminal event")
            active = {"turn_ordinal": len(turns) + 1, "start_line": number, "end_line": None,
                      "turn_id_reported": row.get("turn_id"), "status_reported": "incomplete",
                      "reported_usage": _reported_usage(None), "errors_reported": []}
            if active["turn_id_reported"] is not None and not isinstance(active["turn_id_reported"], str):
                raise ExecUsageError("Invalid optional reported turn identity")
        elif event in {"turn.completed", "turn.failed"}:
            if active is None:
                raise ExecUsageError("Terminal event has no open explicitly bounded turn")
            terminal_id = row.get("turn_id")
            if terminal_id is not None:
                if not isinstance(terminal_id, str) or active["turn_id_reported"] not in (None, terminal_id):
                    raise ExecUsageError("Conflicting reported turn identity")
                active["turn_id_reported"] = terminal_id
            active.update(end_line=number, status_reported=event.removeprefix("turn."),
                          reported_usage=_reported_usage(row.get("usage")))
            if event == "turn.failed":
                active["errors_reported"].append(_error(row, number))
            turns.append(active)
            active = None
        elif event == "error":
            (active["errors_reported"] if active is not None else before_turn_errors).append(_error(row, number))
        elif event in {"item.started", "item.updated", "item.completed"}:
            if active is None:
                raise ExecUsageError("Item event is outside any turn")
        else:
            raise ExecUsageError(f"Unsupported exec event type: {event}")
    if active is not None:
        # Preserve timeout/interruption evidence instead of claiming zero usage.
        active["end_line"] = len(raw.splitlines())
        turns.append(active)
    return {"schema_version": "image-luna-exec-stream-1", "thread_id_reported": thread_id,
            "capture_name": path.name, "capture_sha256": hashlib.sha256(raw).hexdigest(),
            "capture_bytes": len(raw), "capture_line_count": len(raw.splitlines()),
            "turns": turns, "pre_turn_errors_reported": before_turn_errors}


def bind_exec_attempt(path: Path, *, attempt_id: str, style_ids: list[str],
                      expected_thread_id: str | None = None, turn_ordinal: int = 1,
                      counter_scope: str = "unknown", baseline_reported_usage: dict | None = None,
                      baseline_source_id: str | None = None, runner_exit_code: int | None = None,
                      expected_turn_id: str | None = None) -> dict:
    """Bind one attempt to one capture ordinal; retain failed attempts.

    ``counter_scope`` is explicitly asserted by the caller. For cumulative
    scope, provide a baseline and its provenance ID; otherwise attributable
    usage remains null. No implicit zero baseline or negative/reset repair.
    An absent real turn ID cannot satisfy ``expected_turn_id``.
    """
    if not isinstance(attempt_id, str) or not ID.fullmatch(attempt_id):
        raise ExecUsageError("Invalid explicit attempt ID")
    if (not isinstance(style_ids, list) or not 1 <= len(style_ids) <= 5
            or any(not isinstance(value, str) or not ID.fullmatch(value) for value in style_ids)
            or len(style_ids) != len(set(style_ids))):
        raise ExecUsageError("Attempt must bind 1–5 distinct Style IDs")
    if type(turn_ordinal) is not int or turn_ordinal < 1:
        raise ExecUsageError("Turn ordinal must be an explicit positive integer")
    if counter_scope not in {"unknown", "per_turn", "thread_cumulative"}:
        raise ExecUsageError("Unknown counter scope")
    if runner_exit_code is not None and type(runner_exit_code) is not int:
        raise ExecUsageError("Runner exit code must be reported as an integer or null")
    stream = inspect_exec_events(path)
    if expected_thread_id is not None and stream["thread_id_reported"] != expected_thread_id:
        raise ExecUsageError("Capture thread does not match explicit expected identity")
    if not stream["turns"] and turn_ordinal == 1:
        selected = {"turn_ordinal": 1, "start_line": None, "end_line": None, "turn_id_reported": None,
                    "status_reported": "not_started", "reported_usage": _reported_usage(None), "errors_reported": []}
    elif turn_ordinal > len(stream["turns"]):
        raise ExecUsageError("Bound turn ordinal does not exist in the capture")
    else:
        selected = stream["turns"][turn_ordinal - 1]
    if expected_turn_id is not None and selected["turn_id_reported"] != expected_turn_id:
        raise ExecUsageError("Real turn ID is absent or does not match; ordinal binding is not a verified ID")
    reported = selected["reported_usage"]
    attributed = {field: None for field in FIELDS}
    issues = []
    method = "unavailable_counter_scope"
    baseline = None
    if counter_scope == "per_turn":
        if baseline_reported_usage is not None or baseline_source_id is not None:
            raise ExecUsageError("Per-turn scope cannot carry a cumulative baseline")
        attributed = {field: reported[field] for field in FIELDS}
        method = "reported_caller_asserted_per_turn"
    elif counter_scope == "thread_cumulative":
        if baseline_reported_usage is not None:
            if not isinstance(baseline_source_id, str) or not baseline_source_id.strip():
                raise ExecUsageError("Cumulative baseline needs explicit provenance")
            baseline = _reported_usage(baseline_reported_usage)
            for field in FIELDS:
                if reported[field] is not None and baseline[field] is not None:
                    delta = reported[field] - baseline[field]
                    if delta < 0:
                        issues.append("cumulative_counter_decreased_or_reset")
                        break
                    attributed[field] = delta
            if issues:
                attributed = {field: None for field in FIELDS}
                method = "unavailable_counter_reset"
            else:
                method = "calculated_cumulative_difference"
        else:
            if baseline_source_id is not None:
                raise ExecUsageError("Baseline provenance cannot exist without a baseline")
            method = "unavailable_cumulative_baseline"
    elif baseline_reported_usage is not None or baseline_source_id is not None:
        raise ExecUsageError("Unknown scope cannot carry an asserted baseline")
    _check_subsets(attributed)
    def difference(*fields):
        values = [attributed[field] for field in fields]
        return values[0] - sum(values[1:]) if all(value is not None for value in values) else None
    total = (attributed["input_tokens"] + attributed["output_tokens"]
             if all(attributed[field] is not None for field in ("input_tokens", "output_tokens")) else None)
    return {"schema_version": "image-luna-exec-attempt-usage-1", "attempt_id": attempt_id,
            "style_ids": list(style_ids), "capture_sha256": stream["capture_sha256"],
            "capture_name": stream["capture_name"], "capture_bytes": stream["capture_bytes"],
            "thread_id_reported": stream["thread_id_reported"], "expected_thread_id": expected_thread_id,
            "turn_ordinal": turn_ordinal, "turn_id_reported": selected["turn_id_reported"],
            "binding_basis": "explicit_capture_and_turn_ordinal", "runner_exit_code_reported": runner_exit_code,
            "turn_status_reported": selected["status_reported"],
            "line_range": [selected["start_line"], selected["end_line"]],
            "reported_usage": reported, "counter_scope_asserted": counter_scope,
            "baseline_reported_usage": baseline, "baseline_source_id": baseline_source_id,
            "attributable_usage": attributed, "attribution_method": method,
            "total_tokens_calculated": total,
            "uncached_input_tokens_calculated": difference("input_tokens", "cached_input_tokens"),
            "ordinary_input_tokens_calculated": difference("input_tokens", "cached_input_tokens", "cache_write_input_tokens"),
            "errors_reported": [*stream["pre_turn_errors_reported"], *selected["errors_reported"]],
            "issues": issues, "actual_billed_tokens": None, "actual_billed_cost": None,
            "per_image_tokens": {style: total if len(style_ids) == 1 else None for style in style_ids},
            "model_identity_verified_by_usage_stream": False,
            "notes": ["Missing counters are unknown, not zero; CLI-reported zeros may be upstream defaults.",
                      "Total is calculated input plus output; cache/reasoning subsets are not added again.",
                      "Failure or timeout does not imply zero token usage.",
                      "Usage stream has no model identity and normally has no real turn ID."]}


def aggregate_exec_attempts(attempts: list[dict]) -> dict:
    """Aggregate explicitly attributed attempts without dropping failures.

    Known subtotals are separate from complete totals: one unknown attempt
    makes that complete counter null. Repeated Style IDs across retries are
    expected; repeated attempt/event-turn identities are not.
    """
    if not isinstance(attempts, list) or not attempts:
        raise ExecUsageError("At least one attempt receipt is required")
    seen_attempts, seen_events, seen_turns = set(), set(), set()
    for attempt in attempts:
        if not isinstance(attempt, dict) or attempt.get("schema_version") != "image-luna-exec-attempt-usage-1":
            raise ExecUsageError("Unknown attempt receipt")
        ident = attempt["attempt_id"]
        event_key = (attempt["capture_sha256"], attempt["turn_ordinal"])
        turn_key = (attempt["thread_id_reported"], attempt["turn_id_reported"])
        if ident in seen_attempts or event_key in seen_events or attempt["turn_id_reported"] is not None and turn_key in seen_turns:
            raise ExecUsageError("Repeated attempt or bound event/turn would double count")
        seen_attempts.add(ident)
        seen_events.add(event_key)
        seen_turns.add(turn_key)
        _check_subsets(attempt["attributable_usage"])
    names = (*FIELDS, "total_tokens")
    known, complete, missing = {}, {}, {}
    for name in names:
        values = [attempt["total_tokens_calculated"] if name == "total_tokens" else attempt["attributable_usage"][name]
                  for attempt in attempts]
        known[name] = sum(value for value in values if value is not None)
        missing[name] = sum(value is None for value in values)
        complete[name] = known[name] if not missing[name] else None
    return {"schema_version": "image-luna-exec-aggregate-usage-1", "attempt_count": len(attempts),
            "attempt_ids": [attempt["attempt_id"] for attempt in attempts],
            "noncompleted_attempt_count": sum(attempt["turn_status_reported"] != "completed" for attempt in attempts),
            "usage_complete": complete, "known_usage_subtotals": known, "unknown_attempt_counts": missing,
            "total_tokens_basis": "calculated_input_plus_output_per_explicitly_attributed_attempt",
            "actual_billed_tokens": None, "actual_billed_cost": None,
            "failures_included": True, "external_calls_by_meter": 0}


def _counter_difference(current: dict, baseline: dict) -> dict:
    result = {}
    for field in FIELDS:
        if current[field] is None or baseline[field] is None:
            result[field] = None
        else:
            result[field] = current[field] - baseline[field]
            if result[field] < 0:
                raise ExecUsageError("Rollout cumulative counter decreased inside a turn")
    return result


def _matches_known(delta: dict, last: dict) -> bool:
    return all(delta[field] == last[field] for field in FIELDS
               if delta[field] is not None and last[field] is not None)


def _rollout_turn_usage(events: list[tuple[dict, dict]], previous: dict) -> tuple[dict, dict, str, int]:
    nulls = {field: None for field in FIELDS}
    if not events:
        return nulls, previous, "unobserved", 0
    first, first_last = events[0]
    core = ("input_tokens", "output_tokens")
    reset = (all(first[field] is not None and first_last[field] is not None and first[field] == first_last[field]
                 for field in core) and _matches_known(first, first_last))
    baseline = {field: 0 for field in FIELDS} if reset else previous
    initial_delta = _counter_difference(first, baseline)
    if not _matches_known(initial_delta, first_last):
        raise ExecUsageError("Initial rollout cumulative delta does not match its last-token usage")
    final = first
    duplicate_events = 0
    for cumulative, last in events[1:]:
        if cumulative == final:
            duplicate_events += 1
            continue
        delta = _counter_difference(cumulative, final)
        if not _matches_known(delta, last):
            raise ExecUsageError("Rollout cumulative delta does not match last-token usage")
        final = cumulative
    usage = _counter_difference(final, baseline)
    # If a component was absent in any contributing event, it cannot be made
    # exact merely because a later cumulative snapshot contains that component.
    for field in FIELDS:
        if any(cumulative[field] is None or last[field] is None for cumulative, last in events):
            usage[field] = None
    _check_subsets(usage)
    return usage, final, "reset_at_turn_start" if reset else "continued_across_turns", duplicate_events


def _rollout_lines(path: Path):
    """Bound per-line memory; never read irrelevant trailing session history."""
    with path.open("rb") as stream:
        number = 0
        while True:
            line = stream.readline(MAX_ROLLOUT_LINE_BYTES + 1)
            if not line:
                return
            number += 1
            if len(line) > MAX_ROLLOUT_LINE_BYTES:
                raise ExecUsageError("A rollout JSONL line exceeds the bounded parser limit")
            yield number, line


def measure_rollout_turns(path: Path, *, expected_session_id: str, expected_agent_path: str,
                          turn_bindings: list[dict], excluded_turn_ids: list[str] | None = None) -> dict:
    """Meter only explicitly named completed turns in a persistent Luna log.

    Bindings are ``{turn_id, style_ids, batch_ids}``, with 1..N distinct styles
    and zero or more batch IDs. Multiple batches in one turn have no observable
    individual allocation. This function is read-only; the runner owns saving
    its receipt. Later appended session turns do not change the consumed-prefix
    evidence hash. Reads stop at the final selected terminal event; only the
    consumed prefix has a 1 GiB bound, with 16 MiB maximum individual JSONL lines.
    Task completion is not treated as image-analysis success.
    """
    if (not isinstance(expected_session_id, str) or not expected_session_id
            or not isinstance(expected_agent_path, str) or not expected_agent_path.startswith("/root/luna_")):
        raise ExecUsageError("Explicit persistent Luna session identity is required")
    if not isinstance(turn_bindings, list) or not turn_bindings:
        raise ExecUsageError("Explicit completed-turn bindings are required")
    exclusions = excluded_turn_ids or []
    if (not isinstance(exclusions, list) or any(not isinstance(value, str) or not value for value in exclusions)
            or len(exclusions) != len(set(exclusions))):
        raise ExecUsageError("Invalid excluded historical turn identities")
    binding_map = {}
    for binding in turn_bindings:
        if not isinstance(binding, dict) or set(binding) != {"turn_id", "style_ids", "batch_ids"}:
            raise ExecUsageError("Turn bindings need exactly turn_id, style_ids and batch_ids")
        turn_id, styles, batches = binding["turn_id"], binding["style_ids"], binding["batch_ids"]
        if not isinstance(turn_id, str) or not turn_id or turn_id in binding_map or turn_id in exclusions:
            raise ExecUsageError("Turn identity is repeated or historically excluded")
        if (not isinstance(styles, list) or not styles or any(not isinstance(style, str) or not ID.fullmatch(style) for style in styles)
                or len(styles) != len(set(styles))):
            raise ExecUsageError("A turn needs 1..N distinct Style IDs")
        if (not isinstance(batches, list) or any(not isinstance(batch, str) or not ID.fullmatch(batch) for batch in batches)
                or len(batches) != len(set(batches))):
            raise ExecUsageError("Batch IDs must be distinct valid IDs")
        binding_map[turn_id] = binding
    path = Path(path).resolve()
    if not path.is_file():
        raise ExecUsageError("Persistent rollout is missing")
    meta, active = None, None
    previous = {field: 0 for field in FIELDS}
    completed, seen_turns = {}, set()
    last_selected_line = 0
    consumed_bytes, selected_prefix_bytes = 0, 0
    prefix_hash = hashlib.sha256()
    selected_prefix_sha256 = None
    for number, line in _rollout_lines(path):
        consumed_bytes += len(line)
        if consumed_bytes > MAX_ROLLOUT_PREFIX_BYTES:
            raise ExecUsageError("Consumed rollout prefix exceeds the 1 GiB parser limit")
        prefix_hash.update(line)
        row = _row(line)
        payload = row.get("payload")
        if row["type"] == "session_meta":
            if meta is not None or not isinstance(payload, dict):
                raise ExecUsageError("Ambiguous persistent session metadata")
            meta = payload
            if ((meta.get("id") or meta.get("session_id")) != expected_session_id
                    or meta.get("agent_path") != expected_agent_path):
                raise ExecUsageError("Persistent session identity does not match explicit binding")
        elif row["type"] == "turn_context":
            if not isinstance(payload, dict):
                raise ExecUsageError("Rollout turn context overlaps an unfinished turn")
            turn_id = payload.get("turn_id")
            if active is not None:
                if (turn_id != active["turn_id"] or payload.get("model") != active["model_reported"]
                        or payload.get("effort") != active["reasoning_effort_reported"]):
                    raise ExecUsageError("Rollout context overlaps another turn or changes its model/effort")
                active.setdefault("context_refresh_lines", []).append(number)
                active.pop("repeat_compaction_snapshot", None)
                compaction_line = active.pop("pending_compaction_line", None)
                active["compaction_snapshot_window"] = (
                    {"compaction_line": compaction_line, "context_refresh_line": number}
                    if compaction_line is not None else None)
                continue
            if not isinstance(turn_id, str) or not turn_id or turn_id in seen_turns:
                raise ExecUsageError("Missing or repeated actual rollout turn ID")
            seen_turns.add(turn_id)
            active = {"turn_id": turn_id, "model_reported": payload.get("model"),
                      "reasoning_effort_reported": payload.get("effort"), "start_line": number, "events": []}
        elif row["type"] == "compacted" and isinstance(payload, dict) and active is not None:
            active["pending_compaction_line"] = number
            active.pop("compaction_snapshot_window", None)
            active.pop("repeat_compaction_snapshot", None)
        elif row["type"] == "event_msg" and isinstance(payload, dict):
            event = payload.get("type")
            if event == "token_count":
                if active is None or not isinstance(payload.get("info"), dict):
                    raise ExecUsageError("Rollout usage event has no active turn or info")
                info = payload["info"]
                # A usage event before the matching context refresh closes the
                # special window too; an old compaction cannot authorize it.
                active.pop("pending_compaction_line", None)
                cumulative = _reported_usage(info.get("total_token_usage"))
                last_raw = info.get("last_token_usage")
                snapshot_window = active.pop("compaction_snapshot_window", None)
                previous_snapshot = active.pop("repeat_compaction_snapshot", None)
                repeated_line = None
                if (snapshot_window is None and previous_snapshot is not None
                        and last_raw == previous_snapshot["last_raw"]
                        and cumulative == previous_snapshot["cumulative"]):
                    snapshot_window = previous_snapshot["window"]
                    repeated_line = previous_snapshot["first_line"]
                if (snapshot_window is not None and active["events"]
                        and cumulative == active["events"][-1][0]
                        and all(type(cumulative[field]) is int for field in FIELDS)
                        and isinstance(last_raw, dict)
                        and all(type(last_raw.get(field)) is int and last_raw[field] == 0 for field in FIELDS)
                        and type(last_raw.get("total_tokens")) is int and last_raw["total_tokens"] > 0):
                    # A narrowly observed post-compaction snapshot: its parent
                    # cumulative counters did not change. Keep its incompatible
                    # last total as evidence, never treat it as billable usage.
                    last = _reported_usage({key: value for key, value in last_raw.items() if key != "total_tokens"})
                    active.setdefault("compaction_usage_snapshots", []).append({
                        **snapshot_window, "line": number, "last_token_usage_reported": last_raw,
                        "cumulative_usage_reported": cumulative, "contributes_to_usage_delta": False,
                        "note": "Unchanged cumulative counters add no observed usage delta; this does not establish zero compaction cost or billed tokens.",
                    })
                    if repeated_line is not None:
                        active["compaction_usage_snapshots"][-1]["identical_repeat_of_line"] = repeated_line
                    active["repeat_compaction_snapshot"] = {"last_raw": last_raw, "cumulative": cumulative,
                        "window": snapshot_window, "first_line": repeated_line or number}
                else:
                    last = _reported_usage(last_raw)
                active["events"].append((cumulative, last))
            elif event == "task_complete":
                if active is None or payload.get("turn_id") != active["turn_id"]:
                    raise ExecUsageError("Rollout completion does not match active turn")
                usage, previous, counter_mode, duplicates = _rollout_turn_usage(active["events"], previous)
                turn_id = active["turn_id"]
                if turn_id in binding_map:
                    if active["model_reported"] != "gpt-5.6-luna":
                        raise ExecUsageError("Bound rollout turn is not Luna")
                    binding = binding_map[turn_id]
                    total = (usage["input_tokens"] + usage["output_tokens"]
                             if usage["input_tokens"] is not None and usage["output_tokens"] is not None else None)
                    completed[turn_id] = {
                        "turn_id": turn_id, "style_ids": list(binding["style_ids"]), "batch_ids": list(binding["batch_ids"]),
                        "model_reported": active["model_reported"], "reasoning_effort_reported": active["reasoning_effort_reported"],
                        "start_line": active["start_line"], "end_line": number,
                        "completion_event_reported": "task_complete", "work_success_inferred": False,
                        "counter_mode": counter_mode, "usage_event_count": len(active["events"]),
                        "duplicate_cumulative_event_count": duplicates, "attributable_usage": usage,
                        "total_tokens_calculated": total,
                        "first_cumulative_usage_reported": active["events"][0][0] if active["events"] else _reported_usage(None),
                        "last_cumulative_usage_reported": active["events"][-1][0] if active["events"] else _reported_usage(None),
                    }
                    if active.get("context_refresh_lines"):
                        completed[turn_id]["context_refresh_lines_reported"] = active["context_refresh_lines"]
                    if active.get("compaction_usage_snapshots"):
                        completed[turn_id]["compaction_usage_snapshots_reported"] = active["compaction_usage_snapshots"]
                    last_selected_line = number
                    selected_prefix_bytes = consumed_bytes
                    selected_prefix_sha256 = prefix_hash.hexdigest()
                active = None
                if set(completed) == set(binding_map) and set(exclusions).issubset(seen_turns):
                    # Consume only the immutable completed prefix. A concurrent
                    # subsequent turn may be appending a partial JSONL line.
                    break
    if meta is None or set(completed) != set(binding_map):
        raise ExecUsageError("Selected turns are absent, unfinished, or have no session identity")
    if not set(exclusions).issubset(seen_turns):
        raise ExecUsageError("An explicitly excluded historical turn is absent from this session")
    turns = [completed[turn_id] for turn_id in binding_map]
    usage, known, unknown = {}, {}, {}
    for field in (*FIELDS, "total_tokens_calculated"):
        values = [turn[field] if field == "total_tokens_calculated" else turn["attributable_usage"][field] for turn in turns]
        unknown[field] = sum(value is None for value in values)
        known[field] = sum(value for value in values if value is not None)
        usage[field] = known[field] if not unknown[field] else None
    def remainder(*fields):
        values = [usage[field] for field in fields]
        return values[0] - sum(values[1:]) if all(value is not None for value in values) else None
    usage["uncached_input_tokens_calculated"] = remainder("input_tokens", "cached_input_tokens")
    usage["ordinary_input_tokens_calculated"] = remainder("input_tokens", "cached_input_tokens", "cache_write_input_tokens")
    per_batch, per_image = [], []
    for turn in turns:
        for batch in turn["batch_ids"]:
            per_batch.append({"batch_id": batch, "turn_id": turn["turn_id"],
                              "total_tokens_calculated": turn["total_tokens_calculated"] if len(turn["batch_ids"]) == 1 else None,
                              "allocation": "whole_turn" if len(turn["batch_ids"]) == 1 else "not_observable_shared_turn"})
        for style in turn["style_ids"]:
            per_image.append({"style_id": style, "turn_id": turn["turn_id"],
                              "total_tokens_calculated": turn["total_tokens_calculated"] if len(turn["style_ids"]) == 1 else None,
                              "allocation": "whole_turn" if len(turn["style_ids"]) == 1 else "not_observable_shared_turn"})
    return {"schema_version": "image-luna-rollout-turn-usage-receipt-1",
            "session_id_reported": expected_session_id, "agent_path_reported": expected_agent_path,
            "source_log_name": path.name, "source_prefix_sha256": selected_prefix_sha256,
            "source_prefix_line_count": last_selected_line, "source_prefix_bytes": selected_prefix_bytes,
            "scope": "explicit_completed_turn_ids_only", "turn_ids": list(binding_map),
            "excluded_historical_turn_ids": list(exclusions), "other_unbound_turns_excluded": len(seen_turns - set(binding_map)),
            "turns": turns, "usage": usage, "known_usage_subtotals": known, "unknown_turn_counts": unknown,
            "bound_image_count_unique": len({style for turn in turns for style in turn["style_ids"]}),
            "per_batch": per_batch, "per_image": per_image,
            "total_tokens_basis": "calculated_input_plus_output_from_crosschecked_rollout_deltas",
            "all_bound_retry_and_failure_work_included": True, "work_success_inferred": False,
            "actual_billed_tokens": None, "actual_billed_cost": None, "external_calls_by_meter": 0,
            "notes": ["Only explicitly selected completed turns are counted; historic and other turns are excluded.",
                      "Cache reads/writes and reasoning are subsets, never added to input/output totals.",
                      "Missing counters remain null. Shared-turn per-batch/per-image usage is not divided.",
                      "Completion is a telemetry boundary, not proof that every image analysis succeeded."]}
