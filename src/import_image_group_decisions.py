from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from image_rag_eval.group_workflow import import_group_workflow_decisions  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Import append-only private image group workflow decisions.")
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--comparison-dir", default=None)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    result = import_group_workflow_decisions(
        root,
        args.source_run_id,
        args.decisions,
        apply=args.apply,
        comparison_dir=args.comparison_dir,
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
