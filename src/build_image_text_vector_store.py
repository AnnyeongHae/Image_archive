"""Build a separate private SQLite text-search snapshot; dry-run by default."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from image_rag_eval.text_vector_store import build_store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source-database", "plan-dir", "run-dir", "full-manifest", "query-manifest", "output-dir"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        result = build_store(
            root, args.source_database, args.plan_dir, args.run_dir,
            hashlib.sha256(args.full_manifest.read_bytes()).hexdigest(),
            hashlib.sha256(args.query_manifest.read_bytes()).hexdigest(),
            args.output_dir, apply=args.apply)
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, "blocked: " + str(exc) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
