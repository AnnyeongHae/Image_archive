"""Render the cumulative Luna reuse-analysis review; dry-run by default."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from image_rag_eval.luna_reuse_analysis_view import build_luna_reuse_review


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-run-id", required=True)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = build_luna_reuse_review(Path(__file__).resolve().parents[1], args.analysis_run_id, db_path=args.db, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
