"""Build an immutable private SQLite checkpoint of all 655 managed images."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from image_rag_eval.luna_library_store import build_library_store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Create a new content-addressed snapshot; existing snapshots are never replaced")
    args = parser.parse_args()
    print(json.dumps(build_library_store(Path(__file__).resolve().parents[1], apply=args.apply), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
