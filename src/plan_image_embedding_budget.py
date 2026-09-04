"""Offline text token-budget planning. No network, model calls, or DB writes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from image_rag_eval.embedding_budget import DOCUMENT_PREFIX, build_plan, write_plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--tokenizer", required=True, type=Path, help="Locally pinned voyage-4-lite tokenizer.json")
    parser.add_argument("--max-tokens", default=2048, type=int)
    parser.add_argument("--document-prefix", default=DOCUMENT_PREFIX, help="Optional local prefix sensitivity; not provider billing")
    parser.add_argument("--apply", action="store_true", help="Write a new content-addressed private plan only")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if args.apply and args.output_dir is None:
        parser.error("--apply requires --output-dir")
    root = Path(__file__).resolve().parents[1]
    try:
        plan = build_plan(args.database, args.tokenizer, max_tokens=args.max_tokens, document_prefix=args.document_prefix)
        output = write_plan(plan, args.output_dir or root / "data/private-research/image-rag-admin/embedding-budget/plans",
                            archive_root=root, apply=args.apply)
    except (ValueError, OSError, ImportError) as exc:
        parser.exit(2, "blocked: " + str(exc) + "\n")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
