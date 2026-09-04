"""Prepare a latest-committed image approval handoff, offline and dry-run by default."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from image_rag_eval.approval_handoff import prepare_admin_handoff


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--expected-commit-id")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    private = (root / "data/private-research/image-rag-admin").resolve()
    db_path = (args.db or private / "state.sqlite3").resolve()
    if not db_path.is_relative_to(private) or db_path.suffix != ".sqlite3":
        parser.error("DB must be a .sqlite3 file inside data/private-research/image-rag-admin")
    result = prepare_admin_handoff(root, db_path, args.run_id, apply=args.apply,
                                   expected_commit_id=args.expected_commit_id)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
