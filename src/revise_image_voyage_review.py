"""Rebuild a new Voyage-only review revision from existing caches. Default: dry-run."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from image_rag_eval.revisions import revise_voyage_view


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run-id", default="2026-09-03-embedding-ab-50-v2")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = revise_voyage_view(Path(__file__).resolve().parents[1], args.source_run_id, apply=args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
