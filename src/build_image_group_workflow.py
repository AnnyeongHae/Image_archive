from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from image_rag_eval.group_workflow import build_group_workflow_artifacts, plan_group_workflow_build, refresh_group_workflow_html  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Build private image group workflow artifacts for one prepared run.")
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--comparison-dir", default=None)
    parser.add_argument("--root", default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--refresh-html", action="store_true", help="Refresh display code only; preserve spec, decisions and prior HTML.")
    args = parser.parse_args()

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[1]
    if args.refresh_html:
        result = refresh_group_workflow_html(root, args.source_run_id, apply=args.apply)
    elif args.apply:
        result = build_group_workflow_artifacts(root, args.source_run_id, comparison_dir=args.comparison_dir)
    else:
        result = plan_group_workflow_build(root, args.source_run_id, comparison_dir=args.comparison_dir)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
