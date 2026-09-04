"""Build a frozen existing-human-baseline plus new-image review; no API calls."""
import argparse
import json
from pathlib import Path
from image_rag_eval.incremental_workflow import build_incremental_workflow

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--incoming-run-id", required=True)
    parser.add_argument("--baseline-decisions", type=Path, required=True)
    parser.add_argument("--review-run-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build_incremental_workflow(Path(__file__).resolve().parents[1], args.incoming_run_id,
        args.baseline_decisions, args.review_run_id, apply=args.apply), ensure_ascii=False))
