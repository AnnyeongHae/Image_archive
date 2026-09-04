"""Validate or append a completed/partial human incremental review; dry-run default."""
import argparse
import json
from pathlib import Path
from image_rag_eval.incremental_workflow import import_incremental_decisions

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-run-id", required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(import_incremental_decisions(Path(__file__).resolve().parents[1], args.review_run_id,
        args.decisions, apply=args.apply), ensure_ascii=False))
