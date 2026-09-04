"""Prepare only, without external calls. Default dry-run; immutable 50-record parent."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from image_rag_eval.scaling import prepare200

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run-id', default='2026-09-03-embedding-ab-50-v2')
    parser.add_argument('--run-id', default='2026-09-03-voyage-similarity-200-v1')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        result = prepare200(ROOT, args.source_run_id, args.run_id, apply=args.apply,
            progress=lambda value: print(json.dumps(value), flush=True))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        print(json.dumps({'status': 'blocked', 'reason': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
