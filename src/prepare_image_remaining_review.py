"""Prepare remaining primary CASE images offline; no embeddings or approvals."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from image_rag_eval.remaining_review import prepare_remaining_case_review


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-run-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--expected-commit-id")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    private = (root / "data/private-research/image-rag-admin").resolve()
    db = (args.db or private / "state.sqlite3").resolve()
    if not db.is_relative_to(private) or db.suffix != ".sqlite3":
        parser.error("DB must be a .sqlite3 inside the private image administrator directory")
    result = prepare_remaining_case_review(root, db, args.reference_run_id, args.run_id, apply=args.apply,
                                           expected_commit_id=args.expected_commit_id)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
