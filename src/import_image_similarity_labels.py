from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from image_rag_eval.human_review import DEFAULT_COMPARISON_DIR, MIN_THRESHOLD_LABELS  # noqa: E402
from image_rag_eval.label_import import import_review_labels  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Import local human similarity review labels into append-only private artifacts.")
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--comparison-dir", default=DEFAULT_COMPARISON_DIR)
    parser.add_argument("--minimum-verified-pairs", type=int, default=MIN_THRESHOLD_LABELS)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    result = import_review_labels(
        root,
        args.source_run_id,
        args.labels,
        apply=args.apply,
        comparison_dir=args.comparison_dir,
        minimum_verified_pairs=args.minimum_verified_pairs,
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
