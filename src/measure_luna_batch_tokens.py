"""Measure explicit 1–5-image Luna sessions from local logs, without model calls.

The caller supplies exact session identities and complete expected Style IDs.
Every observed turn is counted, including failures/retries. Multi-image session
cost cannot be attributed to individual images and is deliberately left null.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from measure_luna_token_usage import MODEL, STYLE, USAGE_KEYS, UsageError, encode, inspect_session, sha

PRIVATE_ROOT = "data/private-research/image-rag-admin/luna-analysis"
BINDING_KEYS = {"style_ids", "log_path", "expected_session_id", "expected_agent_path"}
SCHEMA = "image-luna-batch-token-usage-receipt-1"


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UsageError("Duplicate JSON object property")
        result[key] = value
    return result


def _json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_object_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(UsageError("Non-finite JSON value")))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise UsageError("Malformed JSON evidence") from exc


def _styles(values, *, batch: bool = False) -> list[str]:
    if (not isinstance(values, list) or not values
            or (batch and len(values) > 5)
            or any(not isinstance(value, str) or not STYLE.fullmatch(value) for value in values)
            or len(set(values)) != len(values)):
        raise UsageError("Style IDs must be distinct; each session must bind 1–5 styles")
    return list(values)


def _partition(value: dict) -> dict:
    if not isinstance(value, dict) or any(type(value.get(key)) is not int or value[key] < 0 for key in USAGE_KEYS):
        raise UsageError("Missing or invalid observed token partition")
    ordinary = value["input_tokens"] - value["cached_input_tokens"] - value["cache_write_input_tokens"]
    if ordinary < 0:
        raise UsageError("Cached plus cache-write tokens exceed input; ordinary input is negative")
    if (value["total_tokens"] != value["input_tokens"] + value["output_tokens"]
            or value["reasoning_output_tokens"] > value["output_tokens"]):
        raise UsageError("Inconsistent token total or reasoning subset")
    return {
        "input_tokens_including_cached": value["input_tokens"],
        "cached_input_tokens": value["cached_input_tokens"],
        "cache_write_input_tokens": value["cache_write_input_tokens"],
        "ordinary_input_tokens_calculated": ordinary,
        "output_tokens_including_reasoning": value["output_tokens"],
        "reasoning_output_tokens": value["reasoning_output_tokens"],
        "total_tokens": value["total_tokens"],
    }


def _inspect(path: Path) -> dict:
    try:
        session = inspect_session(path)
        raw = path.read_bytes()
    except (OSError, AttributeError, TypeError, KeyError, UnicodeError) as exc:
        raise UsageError("Could not inspect complete local Luna session evidence") from exc
    if sha(raw) != session["source_log_sha256"]:
        raise UsageError("Session log changed while being measured")
    # Validate cache-write partition for every event, not just an aggregate that
    # could hide a malformed individual event. Missing fields are never zeroed.
    for line in raw.splitlines():
        row = _json(line)
        if not isinstance(row, dict):
            raise UsageError("Session log row must be an object")
        payload = row.get("payload")
        if (row.get("type") == "event_msg" and isinstance(payload, dict)
                and payload.get("type") == "token_count"):
            info = payload.get("info")
            if not isinstance(info, dict):
                raise UsageError("Token event lacks observed partitions")
            _partition(info.get("total_token_usage"))
            _partition(info.get("last_token_usage"))
    for turn in session["turns"]:
        _partition(turn["usage"])
    _partition(session["usage"])
    return session


def _target(root: Path, run_id: str, relative: str | None) -> Path:
    relative = relative or f"{PRIVATE_ROOT}/{run_id}/batch-token-usage-receipt.json"
    if (not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative
            or Path(relative).is_absolute() or ".." in Path(relative).parts):
        raise UsageError("Receipt path must be a safe archive-relative private path")
    allowed = (root / PRIVATE_ROOT / run_id).resolve()
    private = (root / PRIVATE_ROOT).resolve()
    target = (root / relative).resolve()
    if (not private.is_relative_to(root) or not allowed.is_relative_to(private)
            or not target.is_relative_to(allowed) or target.suffix != ".json"
            or target.name in {"tasks.json", "token-usage-receipt.json"}):
        raise UsageError("Receipt must be a new JSON artifact inside this private analysis run")
    return target


def measure_batch_tokens(root: Path, analysis_run_id: str, expected_style_ids: list[str],
                         session_bindings: list[dict], *, receipt_relative_path: str | None = None,
                         apply: bool = False) -> dict:
    """Return exact local batch telemetry; write only with explicit ``apply``.

    Bindings have style_ids, absolute log_path, expected_session_id and
    expected_agent_path. They are assertions supplied by the caller, checked
    against local evidence, not identity guessed from filenames.
    """
    root = Path(root).resolve()
    if not isinstance(analysis_run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", analysis_run_id):
        raise UsageError("Invalid analysis run ID")
    expected = _styles(expected_style_ids)
    if not isinstance(session_bindings, list) or not session_bindings:
        raise UsageError("Explicit session bindings are required")
    target = _target(root, analysis_run_id, receipt_relative_path)
    seen_styles, seen_logs, seen_sessions, seen_turns = set(), set(), set(), set()
    sessions, log_evidence = [], []
    for binding in session_bindings:
        if not isinstance(binding, dict) or set(binding) != BINDING_KEYS:
            raise UsageError("Unknown or missing explicit session binding fields")
        styles = _styles(binding["style_ids"], batch=True)
        session_id, agent_path = binding["expected_session_id"], binding["expected_agent_path"]
        if (not isinstance(session_id, str) or not session_id.strip()
                or not isinstance(agent_path, str) or not agent_path.startswith("/root/luna_")):
            raise UsageError("Expected Luna session identity is invalid")
        if not isinstance(binding["log_path"], (str, Path)):
            raise UsageError("Session log path must be explicit and absolute")
        path = Path(binding["log_path"])
        if not path.is_absolute():
            raise UsageError("Session log path must be explicit and absolute")
        path = path.resolve()
        if seen_styles.intersection(styles):
            raise UsageError("Repeated Style ID binding")
        if path in seen_logs or session_id in seen_sessions:
            raise UsageError("Repeated session or log binding")
        session = _inspect(path)
        if session["session_id"] != session_id or session["agent_path"] != agent_path:
            raise UsageError("Observed session identity does not match explicit binding")
        for turn in session["turns"]:
            turn_id = turn.get("turn_id")
            if not isinstance(turn_id, str) or not turn_id.strip() or turn_id in seen_turns:
                raise UsageError("Missing or repeated turn identity")
            seen_turns.add(turn_id)
        seen_styles.update(styles)
        seen_logs.add(path)
        seen_sessions.add(session_id)
        log_evidence.append((path, session["source_log_sha256"]))
        sessions.append({"style_ids": styles, "image_count": len(styles), **session})
    if seen_styles != set(expected):
        raise UsageError("Bindings must cover every expected Style ID exactly once")
    totals = {key: sum(session["usage"][key] for session in sessions) for key in USAGE_KEYS}
    usage = _partition(totals)
    per_image = []
    for session in sessions:
        attributable = session["image_count"] == 1
        for style in session["style_ids"]:
            per_image.append({
                "style_id": style, "session_id": session["session_id"],
                "attribution": "observed_single_image_session" if attributable else "not_observable_shared_session",
                "usage": _partition(session["usage"]) if attributable else None,
                "total_tokens": session["usage"]["total_tokens"] if attributable else None,
            })
    receipt = {
        "schema_version": SCHEMA, "analysis_run_id": analysis_run_id,
        "evidence_status": "observed_explicit_local_codex_sessions",
        "model_reported": MODEL, "expected_style_ids": expected,
        "expected_image_count": len(expected), "observed_session_count": len(sessions),
        "observed_turn_count": len(seen_turns),
        "scope": "all_completed_turns_in_explicit_bound_sessions_including_failures_and_retries",
        "usage": usage, "sessions": sessions,
        "per_image": sorted(per_image, key=lambda row: row["style_id"]),
        "actual_billed_tokens": None, "actual_billed_cost": None,
        "includes_parent_orchestrator_or_qa": False,
        "external_api_calls_by_meter": 0,
        "notes": [
            "Input includes cache reads and cache writes; neither is added to total again.",
            "Ordinary input is input minus cached input minus cache-write input; missing fields fail closed.",
            "Output includes reasoning output; reasoning is not added to total again.",
            "Multi-image session tokens are not divided or attributed to individual images.",
            "All observed bound turns count, including failed attempts and retries; image success is not inferred.",
            "Local execution telemetry is not a provider billing statement and does not prove prompt-specific cache hits.",
        ],
    }
    raw = encode(receipt)
    for path, expected_hash in log_evidence:
        if sha(path.read_bytes()) != expected_hash:
            raise UsageError("Session evidence changed before receipt creation")
    status = "dry_run"
    if target.exists():
        if not target.is_file() or target.read_bytes() != raw:
            raise UsageError("Existing receipt differs; immutable receipt cannot be overwritten")
        status = "unchanged"
    elif apply:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(raw)
        status = "prepared"
    return {"status": status, "analysis_run_id": analysis_run_id,
            "receipt_path": target.relative_to(root).as_posix(), "receipt_sha256": sha(raw),
            "usage": usage, "receipt": receipt, "external_api_calls": 0}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-run-id", required=True)
    parser.add_argument("--expected-styles", required=True, help="Exact comma-separated Style IDs")
    parser.add_argument("--bindings-json", type=Path, required=True,
                        help="JSON array of explicit local session bindings")
    parser.add_argument("--receipt-relative-path", help="New JSON receipt under this private analysis run")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.bindings_json.is_file() or not 0 < args.bindings_json.stat().st_size <= 1024 * 1024:
        raise UsageError("Bindings JSON is missing or oversized")
    result = measure_batch_tokens(Path(__file__).resolve().parents[1], args.analysis_run_id,
                                  args.expected_styles.split(","), _json(args.bindings_json.read_bytes()),
                                  receipt_relative_path=args.receipt_relative_path, apply=args.apply)
    print(json.dumps({key: value for key, value in result.items() if key != "receipt"}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
