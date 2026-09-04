from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from image_rag_eval.experiment import digest, run_lock, run_path
from image_rag_eval.human_review_v2 import (
    REVIEW_V2_HTML_FILENAME,
    build_human_review_v2_artifacts,
    load_bound_review_spec_v2,
    plan_human_review_v2_build,
    review_html_v2,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build offline human similarity review v2 artifacts from a stored v1 review spec.")
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--comparison-dir", default=None)
    parser.add_argument("--root", default=None)
    parser.add_argument("--refresh-html", action="store_true", help="Refresh only the v2 HTML after validating the unchanged stored review contract; requires --apply to write.")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[1]
    if args.refresh_html:
        destination = run_path(root, args.source_run_id)
        html_path = destination / REVIEW_V2_HTML_FILENAME
        with run_lock(destination):
            spec, _v1_spec, _source = load_bound_review_spec_v2(root, args.source_run_id, comparison_dir=args.comparison_dir)
            before = html_path.read_bytes()
            rendered = review_html_v2(spec).encode("utf-8")
            changed = before != rendered
            if args.apply and changed:
                html_path.write_bytes(rendered)
        result = {
            "status": "html_refreshed" if args.apply else "dry_run",
            "run_id": args.source_run_id,
            "html_path": str(html_path),
            "review_spec_sha256": spec["review_spec_sha256"],
            "previous_html_sha256": digest(before),
            "rendered_html_sha256": digest(rendered),
            "changed": changed,
            "writes": int(args.apply and changed),
            "network_calls": 0,
            "spec_writes": 0,
            "label_writes": 0,
        }
    elif args.apply:
        result = build_human_review_v2_artifacts(root, args.source_run_id, comparison_dir=args.comparison_dir)
    else:
        result = plan_human_review_v2_build(root, args.source_run_id, comparison_dir=args.comparison_dir)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
