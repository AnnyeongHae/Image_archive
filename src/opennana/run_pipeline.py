from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from .common import DATA_ROOT, DEFAULT_CANONICAL, atomic_write_json, read_json, stable_json
except ImportError:
    from common import DATA_ROOT, DEFAULT_CANONICAL, atomic_write_json, read_json, stable_json


MODULE_DIR = Path(__file__).resolve().parent
ARCHIVE_ROOT = MODULE_DIR.parents[1]
QA_DIR = ARCHIVE_ROOT / "qa"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_stage(name: str, command: list[str]) -> dict[str, Any]:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    parsed: Any = None
    if result.stdout.strip():
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError:
            parsed = None
    if isinstance(parsed, dict) and name.startswith("validate_"):
        parsed = {
            key: parsed.get(key)
            for key in ("passed", "ok", "check_count", "failure_count", "failures")
            if key in parsed
        }
    return {
        "name": name,
        "exit_code": result.returncode,
        "ok": result.returncode == 0,
        "command": [Path(part).name if index == 1 and part.endswith(".py") else part for index, part in enumerate(command)],
        "result": parsed,
        "stdout_tail": result.stdout[-2000:] if parsed is None else None,
        "stderr_tail": result.stderr[-2000:] or None,
    }


def stage_or_stop(stages: list[dict[str, Any]], name: str, command: list[str]) -> dict[str, Any]:
    stage = run_stage(name, command)
    stages.append(stage)
    if not stage["ok"]:
        raise RuntimeError(f"stage failed: {name}")
    return stage


def run_pipeline(*, sample: bool, fetch: bool, apply: bool, max_details: int, canonical: Path) -> tuple[dict[str, Any], int]:
    if not apply:
        return {
            "schema_version": "opennana-pipeline-plan-1.0",
            "mode": "offline_dry_run",
            "writes": False,
            "network": False,
            "canonical_mutation": False,
            "planned_stages": ["collect", "normalize", "dedupe", "review_queue_and_projection", "validate_workflow", "validate_review_ui"],
            "next": "use --sample --apply for the offline canary or --fetch --apply for an approved network canary",
        }, 0

    mode = "network_canary" if fetch else "local_fabricated_sample"
    started_at = utc_now()
    stages: list[dict[str, Any]] = []
    run_id = f"failed-{started_at.replace(':', '').replace('-', '')}"
    raw_path: Path | None = None
    try:
        collect_command = [sys.executable, str(MODULE_DIR / "collect.py")]
        if fetch:
            collect_command.extend(["--fetch", "--apply", "--max-details", str(max_details)])
        else:
            collect_command.extend(["--seed-sample", "--apply", "--max-details", str(max_details)])
        collect_stage = stage_or_stop(stages, "collect", collect_command)
        written = collect_stage["result"].get("written", []) if isinstance(collect_stage["result"], dict) else []
        raw_candidates = [Path(path) for path in written if str(path).casefold().endswith(".json") and "raw" in Path(path).parts]
        if not raw_candidates:
            raise RuntimeError("collector did not report a raw JSON output")
        raw_path = raw_candidates[0]
        raw_bundle = read_json(raw_path)
        run_id = raw_bundle["run_id"]

        normalized_path = DATA_ROOT / "staging" / f"normalized-{run_id}.json"
        dedupe_path = DATA_ROOT / "staging" / f"dedupe-{run_id}.json"
        stage_or_stop(stages, "normalize", [sys.executable, str(MODULE_DIR / "normalize.py"), "--input", str(raw_path), "--output", str(normalized_path), "--apply"])
        stage_or_stop(stages, "dedupe", [sys.executable, str(MODULE_DIR / "dedupe.py"), "--input", str(normalized_path), "--canonical", str(canonical), "--output", str(dedupe_path), "--apply"])
        stage_or_stop(stages, "review_queue_and_projection", [sys.executable, str(MODULE_DIR / "build_review_queue.py"), "--input", str(dedupe_path), "--apply"])
        stage_or_stop(stages, "validate_workflow", [sys.executable, str(QA_DIR / "validate_opennana_workflow.py"), "--write-report"])
        stage_or_stop(stages, "validate_review_ui", [sys.executable, str(QA_DIR / "validate_opennana_review_queue.py")])
        exit_code = 0
        status = "passed"
        error = None
    except Exception as exc:
        exit_code = next((stage["exit_code"] for stage in reversed(stages) if not stage["ok"]), 1)
        status = "failed"
        error = str(exc)

    manifest = {
        "schema_version": "opennana-pipeline-run-1.0",
        "run_id": run_id,
        "mode": mode,
        "started_at": started_at,
        "finished_at": utc_now(),
        "status": status,
        "exit_code": exit_code,
        "network_enabled": fetch,
        "max_details": max_details,
        "canonical_compared": str(canonical),
        "canonical_mutated": False,
        "automatic_decision_apply": False,
        "raw_path": str(raw_path) if raw_path else None,
        "error": error,
        "stages": stages,
    }
    manifest_path = DATA_ROOT / "runs" / f"pipeline-{run_id}.json"
    atomic_write_json(manifest_path, manifest)
    return {"manifest": str(manifest_path), "status": status, "exit_code": exit_code, "run_id": run_id, "stage_count": len(stages)}, exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the bounded OpenNana private review pipeline.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--sample", action="store_true", help="Use the offline fabricated sample.")
    mode.add_argument("--fetch", action="store_true", help="Enable the bounded network collector; requires --apply.")
    parser.add_argument("--apply", action="store_true", help="Write private workflow artifacts and a run manifest.")
    parser.add_argument("--max-details", type=int, default=20)
    parser.add_argument("--canonical", type=Path, default=DEFAULT_CANONICAL)
    args = parser.parse_args()
    if args.fetch and not args.apply:
        parser.error("network pipeline requires both --fetch and --apply")
    if args.apply and not (args.sample or args.fetch):
        parser.error("--apply requires either --sample or --fetch")
    if not 1 <= args.max_details <= 20:
        parser.error("max-details must be between 1 and 20")
    result, exit_code = run_pipeline(
        sample=args.sample,
        fetch=args.fetch,
        apply=args.apply,
        max_details=args.max_details,
        canonical=args.canonical,
    )
    print(stable_json(result), end="")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
