"""Dry-run by default. Real bounded Voyage image calls require --execute --apply --consent."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from image_rag_eval.experiment import read_json
from image_rag_eval.incremental_embedding import execute_incremental_embedding, plan_incremental_embedding
from image_rag_eval.providers import ProviderError

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--consent", type=Path)
    parser.add_argument("--retry-consent", type=Path, help="Explicit one-image investigated retry; requires --max-new-images 1")
    parser.add_argument("--max-cost-usd", type=float, default=.10)
    parser.add_argument("--max-new-images", type=int)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--interval", type=float, default=3.1)
    args = parser.parse_args()
    options = {"maximum_usd": args.max_cost_usd, "max_new_images": args.max_new_images,
               "batch_size": args.batch_size, "interval": args.interval}
    try:
        if args.retry_consent:
            options["retry_consent"] = read_json(args.retry_consent)
        if not args.execute:
            if args.apply or args.consent:
                raise ValueError("--apply and --consent are execution-only flags")
            result = plan_incremental_embedding(ROOT, args.run_id, **options)
        else:
            if not args.apply or not args.consent:
                raise ValueError("--execute requires --apply and exact --consent")
            result = execute_incremental_embedding(ROOT, args.run_id, read_json(args.consent), **options,
                progress=lambda value: print(json.dumps(value), flush=True))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ProviderError as exc:
        print(json.dumps({"status": "blocked", **exc.to_dict()}))
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
