"""Build an append-only private shared vector cache, with no API requests."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from image_rag_eval.shared_vector_cache import build_shared_vector_cache

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run-id", action="append", required=True, help="Explicit completed source run; repeat for multiple runs")
    parser.add_argument("--apply", action="store_true", help="Append immutable local cache revision; default is read-only")
    args = parser.parse_args()
    print(json.dumps(build_shared_vector_cache(ROOT, args.source_run_id, apply=args.apply), ensure_ascii=False))


if __name__ == "__main__":
    main()
