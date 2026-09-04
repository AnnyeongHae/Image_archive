from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from image_rag_eval.human_review import (  # noqa: E402
    DEFAULT_COMPARISON_DIR,
    DEFAULT_SEED,
    MAX_REVIEW_PAIRS,
    build_human_review_artifacts,
    plan_human_review_build,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build offline human similarity review artifacts for a Voyage run.")
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--comparison-dir", default=DEFAULT_COMPARISON_DIR)
    parser.add_argument("--max-pairs", type=int, default=MAX_REVIEW_PAIRS)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    if args.apply:
        result = build_human_review_artifacts(
            root,
            args.source_run_id,
            comparison_dir=args.comparison_dir,
            max_pairs=args.max_pairs,
            seed=args.seed,
        )
    else:
        result = plan_human_review_build(
            root,
            args.source_run_id,
            comparison_dir=args.comparison_dir,
            max_pairs=args.max_pairs,
            seed=args.seed,
        )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
