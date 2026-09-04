"""Project frozen Luna candidates into a private sidecar, with no provider calls."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from image_rag_eval.metadata_candidate_store import build_metadata_store


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approval-db", type=Path, help="Read-only source; never mutated")
    parser.add_argument("--apply", action="store_true", help="Create immutable private snapshot (default: in-memory dry-run)")
    args = parser.parse_args()
    result = build_metadata_store(Path(__file__).resolve().parents[1], approval_db=args.approval_db, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
