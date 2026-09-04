"""Prepare at most 300 unsampled public CASE records; default offline dry-run."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from image_rag_eval.incremental import DEFAULT_REFERENCE_RUN, prepare_incremental_batch


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-run-id", "--source-run-id", default=DEFAULT_REFERENCE_RUN)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--max-records", type=int, default=300)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = prepare_incremental_batch(ROOT, args.reference_run_id, args.run_id,
            max_records=args.max_records, apply=args.apply,
            progress=lambda value: print(json.dumps(value, ensure_ascii=False), flush=True))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
