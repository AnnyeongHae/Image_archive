"""Measure one Luna batch from isolated local Codex session logs; no API call."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

MODEL = "gpt-5.6-luna"
RECEIPT = "token-usage-receipt.json"
USAGE_KEYS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens", "total_tokens",
)
STYLE = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")


class UsageError(ValueError):
    pass


def encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _usage(value) -> dict[str, int]:
    if not isinstance(value, dict) or any(type(value.get(key)) is not int or value[key] < 0 for key in USAGE_KEYS):
        raise UsageError("Malformed token usage event")
    result = {key: value[key] for key in USAGE_KEYS}
    if result["total_tokens"] != result["input_tokens"] + result["output_tokens"]:
        raise UsageError("Token total is not input plus output")
    if result["cached_input_tokens"] > result["input_tokens"] or result["reasoning_output_tokens"] > result["output_tokens"]:
        raise UsageError("Cached or reasoning token subset exceeds its parent count")
    return result


def _subtract(current: dict[str, int], previous: dict[str, int]) -> dict[str, int]:
    result = {key: current[key] - previous[key] for key in USAGE_KEYS}
    if any(value < 0 for value in result.values()):
        raise UsageError("Session cumulative usage moved backwards")
    return result


def inspect_session(path: Path) -> dict:
    path = path.resolve()
    if path.suffix != ".jsonl" or not path.is_file() or not 0 < path.stat().st_size <= 100 * 1024 * 1024:
        raise UsageError("Session log is missing, oversized, or not JSONL")
    raw = path.read_bytes()
    session_meta = None
    current = None
    turns = []
    previous_final = {key: 0 for key in USAGE_KEYS}
    for line_number, raw_line in enumerate(raw.splitlines(), 1):
        try:
            row = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UsageError(f"Malformed session JSONL at line {line_number}") from exc
        payload = row.get("payload") if isinstance(row, dict) else None
        if row.get("type") == "session_meta":
            if session_meta is not None or not isinstance(payload, dict):
                raise UsageError("Ambiguous session metadata")
            session_meta = {
                "session_id": payload.get("id") or payload.get("session_id"),
                "agent_path": payload.get("agent_path"),
                "model_provider": payload.get("model_provider"),
                "line": line_number,
            }
        elif row.get("type") == "turn_context":
            if current is not None:
                raise UsageError("Turn context changed before task completion")
            if not isinstance(payload, dict) or payload.get("model") != MODEL:
                raise UsageError("Session contains a non-Luna turn")
            current = {
                "turn_id": payload.get("turn_id"),
                "model": payload.get("model"),
                "reasoning_effort": payload.get("effort"),
                "start_line": line_number,
                "usage_events": 0,
                "duplicate_cumulative_events": 0,
                "last_total": None,
                "turn_baseline": None,
                "counter_mode": None,
                "delta_crosscheck": True,
            }
        elif row.get("type") == "event_msg" and isinstance(payload, dict) and payload.get("type") == "token_count":
            if current is None:
                raise UsageError("Usage event is not inside a turn")
            info = payload.get("info")
            if not isinstance(info, dict):
                raise UsageError("Usage event lacks info")
            total = _usage(info.get("total_token_usage"))
            last = _usage(info.get("last_token_usage"))
            current["usage_events"] += 1
            if current["last_total"] == total:
                current["duplicate_cumulative_events"] += 1
            else:
                if current["last_total"] is None:
                    zero = {key: 0 for key in USAGE_KEYS}
                    if total == last:
                        baseline = zero
                        current["counter_mode"] = "reset_at_turn_start"
                    else:
                        try:
                            continued = _subtract(total, previous_final)
                        except UsageError:
                            continued = None
                        baseline = previous_final
                        current["counter_mode"] = "continued_across_turns"
                        if continued != last:
                            current["delta_crosscheck"] = False
                    current["turn_baseline"] = baseline
                else:
                    baseline = current["last_total"]
                    if _subtract(total, baseline) != last:
                        current["delta_crosscheck"] = False
                current["last_total"] = total
        elif row.get("type") == "event_msg" and isinstance(payload, dict) and payload.get("type") == "task_complete":
            if current is None or payload.get("turn_id") != current["turn_id"]:
                raise UsageError("Task completion is not bound to the active turn")
            current["end_line"] = line_number
            current["started_at_unix"] = payload.get("started_at")
            current["completed_at_unix"] = payload.get("completed_at")
            if current["last_total"] is None:
                current["usage"] = None
            else:
                current["usage"] = _subtract(current["last_total"], current["turn_baseline"])
                previous_final = current["last_total"]
            current.pop("last_total")
            current.pop("turn_baseline")
            turns.append(current)
            current = None
    if session_meta is None or current is not None or not turns:
        raise UsageError("Session metadata or completed turn evidence is incomplete")
    if not isinstance(session_meta["session_id"], str) or not isinstance(session_meta["agent_path"], str):
        raise UsageError("Session identity is incomplete")
    if not session_meta["agent_path"].startswith("/root/luna_"):
        raise UsageError("Session is not an isolated Luna analysis worker")
    if any(turn["usage"] is None or not turn["delta_crosscheck"] for turn in turns):
        raise UsageError("Every worker turn needs observed, cross-checked usage")
    final = {key: sum(turn["usage"][key] for turn in turns) for key in USAGE_KEYS}
    return {
        "session_id": session_meta["session_id"],
        "agent_path": session_meta["agent_path"],
        "model_provider": session_meta["model_provider"],
        "source_log_name": path.name,
        "source_log_sha256": sha(raw),
        "source_log_line_count": len(raw.splitlines()),
        "turns": turns,
        "usage": final,
    }


def _parse_binding(value: str) -> tuple[list[str], Path]:
    if "=" not in value:
        raise UsageError("Binding must be STYLE[,STYLE...]=ABSOLUTE_JSONL_PATH")
    styles_raw, path_raw = value.split("=", 1)
    styles = styles_raw.split(",")
    if not styles or any(not STYLE.fullmatch(style) for style in styles) or len(styles) != len(set(styles)):
        raise UsageError("Binding contains invalid or duplicate Style IDs")
    path = Path(path_raw)
    if not path.is_absolute():
        raise UsageError("Session log path must be absolute")
    return styles, path


def measure(root: Path, analysis_run_id: str, bindings: list[str], *, apply: bool = False) -> dict:
    root = Path(root).resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", analysis_run_id):
        raise UsageError("Invalid analysis run ID")
    directory = (root / "data/private-research/image-rag-admin/luna-analysis" / analysis_run_id).resolve()
    allowed = (root / "data/private-research/image-rag-admin/luna-analysis").resolve()
    if not directory.is_relative_to(allowed):
        raise UsageError("Analysis run escaped its private scope")
    manifest_raw = (directory / "tasks.json").read_bytes()
    manifest = json.loads(manifest_raw)
    tasks = manifest.get("tasks")
    if (manifest.get("analysis_run_id") != analysis_run_id or manifest.get("model_family") != MODEL
            or manifest.get("worker_partition") != "one_isolated_luna_session_per_image"
            or manifest.get("token_metering_required") is not True or not isinstance(tasks, list)):
        raise UsageError("Run is not configured for isolated Luna token metering")
    expected_styles = {task["style_id"] for task in tasks}
    bound_styles, session_rows, seen_logs = set(), [], set()
    for raw_binding in bindings:
        styles, path = _parse_binding(raw_binding)
        if bound_styles.intersection(styles):
            raise UsageError("A Style ID is bound more than once")
        if path.resolve() in seen_logs:
            raise UsageError("One isolated session log cannot be bound twice")
        bound_styles.update(styles)
        seen_logs.add(path.resolve())
        session = inspect_session(path)
        session_rows.append({"style_ids": styles, **session})
    if bound_styles != expected_styles:
        raise UsageError("Session bindings must cover every task Style ID exactly once")
    if any(len(row["style_ids"]) != 1 for row in session_rows):
        raise UsageError("This run requires one isolated session per image")
    session_ids = [row["session_id"] for row in session_rows]
    if len(session_ids) != len(set(session_ids)):
        raise UsageError("Duplicate Luna session identity")
    totals = {key: sum(row["usage"][key] for row in session_rows) for key in USAGE_KEYS}
    usage = {
        "input_tokens_including_cached": totals["input_tokens"],
        "cached_input_tokens": totals["cached_input_tokens"],
        "cache_write_input_tokens": totals["cache_write_input_tokens"],
        "uncached_input_tokens_calculated": totals["input_tokens"] - totals["cached_input_tokens"],
        "output_tokens_including_reasoning": totals["output_tokens"],
        "reasoning_output_tokens": totals["reasoning_output_tokens"],
        "total_tokens": totals["total_tokens"],
    }
    per_image = []
    for row in sorted(session_rows, key=lambda item: item["style_ids"][0]):
        current = row["usage"]
        per_image.append({
            "style_id": row["style_ids"][0],
            "session_id": row["session_id"],
            "agent_path": row["agent_path"],
            "turn_count": len(row["turns"]),
            "input_tokens_including_cached": current["input_tokens"],
            "cached_input_tokens": current["cached_input_tokens"],
            "cache_write_input_tokens": current["cache_write_input_tokens"],
            "uncached_input_tokens_calculated": current["input_tokens"] - current["cached_input_tokens"],
            "output_tokens_including_reasoning": current["output_tokens"],
            "reasoning_output_tokens": current["reasoning_output_tokens"],
            "total_tokens": current["total_tokens"],
            "source_log_name": row["source_log_name"],
            "source_log_sha256": row["source_log_sha256"],
            "source_log_line_count": row["source_log_line_count"],
            "turns": row["turns"],
        })
    receipt = {
        "schema_version": "image-luna-token-usage-receipt-2",
        "analysis_run_id": analysis_run_id,
        "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "evidence_status": "observed_isolated_local_codex_logs",
        "scope": "all_turns_in_one_dedicated_worker_session_per_image",
        "model_reported": MODEL,
        "completed_image_count": len(tasks),
        "task_manifest_sha256": sha(manifest_raw),
        "usage": usage,
        "per_image": per_image,
        "aggregation_method": "sum_of_crosschecked_monotonic_session_deltas",
        "actual_billed_tokens": None,
        "actual_billed_cost": None,
        "includes_parent_orchestrator_or_qa": False,
        "notes": [
            "Input includes cached input; cached tokens are a subset and are not added again.",
            "Output includes reasoning output; reasoning tokens are a subset and are not added again.",
            "Counts are local Codex execution telemetry, not a provider billing statement.",
        ],
    }
    raw = encode(receipt)
    target = directory / RECEIPT
    status = "dry_run"
    if target.exists():
        existing = json.loads(target.read_text(encoding="utf-8-sig"))
        # recorded_at is observation metadata; compare the measurement body for idempotence.
        if {key: value for key, value in existing.items() if key != "recorded_at"} != {
                key: value for key, value in receipt.items() if key != "recorded_at"}:
            raise UsageError("Existing token receipt differs; do not overwrite")
        raw, status = target.read_bytes(), "unchanged"
    elif apply:
        with target.open("xb") as output:
            output.write(raw)
        status = "prepared"
    return {
        "status": status,
        "analysis_run_id": analysis_run_id,
        "completed_image_count": len(tasks),
        "receipt_path": str(target),
        "receipt_sha256": sha(raw),
        "usage": usage,
        "external_api_calls": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-run-id", required=True)
    parser.add_argument("--session", action="append", required=True,
                        help="STYLE[,STYLE...]=absolute Codex session JSONL path")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = measure(Path(__file__).resolve().parents[1], args.analysis_run_id, args.session, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
