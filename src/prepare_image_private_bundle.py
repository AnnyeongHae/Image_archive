"""Offline deployment preparation only; --apply writes an ignored private bundle."""
import argparse
import json
from pathlib import Path

from image_rag_eval.private_library_bundle import prepare_private_bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-commit-id")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    result = prepare_private_bundle(root, root / "data/private-research/image-rag-admin/state.sqlite3", args.run_id,
                                    apply=args.apply, expected_commit_id=args.expected_commit_id)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
