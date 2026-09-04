"""Record one explicitly assigned, completed Luna turn without API calls."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from image_rag_eval.luna_exec_usage import measure_rollout_turns
from image_rag_eval.luna_analysis_import import _json, digest, encode
from prepare_luna_full_library import BASE, RUN, immutable, read_manifest, validate_progress

SESSION_ID = "01a06869-73e9-7931-bb64-9a0b5a9dd557"
AGENT_PATH = "/root/luna_case_343"
HISTORICAL_TURN = "01a06869-745e-7a41-8f16-4753cd785262"
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
LUNA_AGENT_PATTERN = re.compile(r"/root/luna_[a-z0-9_]+")


def _session_identity(session_id: str | None, agent_path: str | None) -> tuple[str, str]:
    if (session_id is None) != (agent_path is None):
        raise ValueError("Session ID and agent path overrides must be supplied together")
    if session_id is None:
        return SESSION_ID, AGENT_PATH
    if (not isinstance(session_id, str) or not UUID_PATTERN.fullmatch(session_id)
            or not isinstance(agent_path, str) or not LUNA_AGENT_PATTERN.fullmatch(agent_path)):
        raise ValueError("Explicit session UUID and canonical /root/luna_ agent path required")
    return session_id, agent_path


def _validate_saved_coverage(existing: dict, assigned_styles: list[str]) -> None:
    """Check the recorded snapshot itself, never substitute current progress."""
    if (existing.get("metadata_human_approved") is not False
            or existing.get("release_eligible") is not False):
        raise ValueError("Existing token receipt approval flags must be literal false")
    validated = existing.get("schema_valid_output_styles")
    incomplete = existing.get("output_incomplete_styles")
    for values in (validated, incomplete):
        if (not isinstance(values, list) or any(not isinstance(value, str) for value in values)
                or len(values) != len(set(values))):
            raise ValueError("Existing token receipt coverage must contain unique style ID lists")
    validated_set, incomplete_set = set(validated), set(incomplete)
    if (validated_set & incomplete_set
            or validated_set | incomplete_set != set(assigned_styles)):
        raise ValueError("Existing token receipt coverage must exactly partition assigned styles")


def measure(root: Path, log: Path, turn_id: str, batch_ids: list[str], *, apply=False,
            expected_session_id: str | None = None, expected_agent_path: str | None = None) -> dict:
    session_id, agent_path = _session_identity(expected_session_id, expected_agent_path)
    if not isinstance(turn_id, str) or not UUID_PATTERN.fullmatch(turn_id) or not log.is_absolute():
        raise ValueError("Explicit turn UUID and absolute session log required")
    if turn_id == HISTORICAL_TURN:
        raise ValueError("The historical turn is excluded from every full-library run")
    manifest, raw = read_manifest(root)
    batches = {b["batch_id"]: b for b in manifest["batches"]}
    if not batch_ids or len(batch_ids) != len(set(batch_ids)) or not set(batch_ids) <= set(batches):
        raise ValueError("Explicit unique assigned batch IDs required")
    styles = [s for b in batch_ids for s in batches[b]["style_ids"]]
    # The original log must contain its historical exclusion. Independent logs
    # cannot contain that turn; selection is nevertheless forbidden above.
    exclusions = [HISTORICAL_TURN] if session_id == SESSION_ID else []
    receipt = measure_rollout_turns(log, expected_session_id=session_id, expected_agent_path=agent_path,
        turn_bindings=[{"turn_id": turn_id, "style_ids": styles, "batch_ids": batch_ids}],
        excluded_turn_ids=exclusions)
    output = root / BASE / "execution" / f"{turn_id}.tokens.json"
    if output.exists():
        existing, _ = _json(output)
        if (any(existing.get(key) != value for key, value in receipt.items())
                or existing.get("analysis_run_id") != RUN or existing.get("task_manifest_sha256") != digest(raw)
                or existing.get("assigned_styles") != styles):
            raise ValueError("Existing token receipt differs from verified execution")
        _validate_saved_coverage(existing, styles)
        return {"status": "unchanged", "path": str(output), "bound_images": len(styles),
                "validated_images_at_receipt": len(existing["schema_valid_output_styles"]),
                "batches": batch_ids, "usage": receipt["usage"], "actual_billed_cost": None, "actual_billed_tokens": None}
    progress = validate_progress(root)
    completed = set(progress["completed_styles"])
    receipt.update(analysis_run_id=RUN, task_manifest_sha256=digest(raw),
                   assigned_styles=styles, schema_valid_output_styles=[s for s in styles if s in completed],
                   output_incomplete_styles=[s for s in styles if s not in completed],
                   metadata_human_approved=False, release_eligible=False)
    if apply:
        immutable(output, encode(receipt))
    return {"status": "recorded" if apply else "dry_run", "path": str(output), "bound_images": len(styles),
            "validated_images": len(receipt["schema_valid_output_styles"]), "batches": batch_ids, "usage": receipt["usage"],
            "actual_billed_cost": None, "actual_billed_tokens": None}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--turn-id", required=True)
    parser.add_argument("--batch-id", action="append", required=True)
    parser.add_argument("--expected-session-id", help="Explicit session UUID; requires --expected-agent-path")
    parser.add_argument("--expected-agent-path", help="Explicit /root/luna_ agent path; requires --expected-session-id")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(measure(Path(__file__).resolve().parents[1], args.log, args.turn_id, args.batch_id,
                             apply=args.apply, expected_session_id=args.expected_session_id,
                             expected_agent_path=args.expected_agent_path), ensure_ascii=False))


if __name__ == "__main__":
    main()
