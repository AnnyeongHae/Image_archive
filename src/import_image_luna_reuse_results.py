"""Validate one complete Luna reuse-analysis batch; dry-run unless --apply."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from image_rag_eval.luna_reuse_analysis_import import import_luna_reuse_results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-run-id", required=True)
    parser.add_argument("--expected-commit-id")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    result = import_luna_reuse_results(
        root,
        root / "data/private-research/image-rag-admin/state.sqlite3",
        args.analysis_run_id,
        apply=args.apply,
        expected_commit_id=args.expected_commit_id,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
