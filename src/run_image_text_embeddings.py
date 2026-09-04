"""Dry-run by default. --apply --execute authorizes bounded Voyage text requests."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from image_rag_eval.text_embedding_run import TextRunError, execute_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True, help="Same private directory for every phase and manifest")
    parser.add_argument("--dotenv", type=Path)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        receipt = execute_manifest(args.manifest, args.tokenizer, args.run_dir,
            archive_root=Path(__file__).resolve().parents[1], apply=args.apply, execute=args.execute,
            batch_size=args.batch_size, dotenv_path=args.dotenv)
    except TextRunError as exc:
        parser.exit(2, "blocked: " + str(exc) + "\n")
    except Exception:
        parser.exit(2, "blocked: local_validation_or_io_failure\n")
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
