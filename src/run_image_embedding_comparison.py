"""Default is a local, write-free plan. Real inference needs exact human consent."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from image_rag_eval.comparison import ARMS, execute_comparison, plan_comparison, prepare_comparison_view, refresh_comparison
from image_rag_eval.experiment import read_json
from image_rag_eval.providers import ProviderError

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run-id", default="2026-09-03-embedding-ab-v1")
    parser.add_argument("--sample-limit", type=int, choices=(20, 50, 200), default=20)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--execute", action="store_true")
    actions.add_argument("--prepare", action="store_true")
    actions.add_argument("--refresh", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--consent", type=Path)
    parser.add_argument("--reviewed-http429-recovery", type=Path)
    parser.add_argument("--provider", choices=("gemini", "voyage"))
    parser.add_argument("--arm", dest="arms", action="append", choices=tuple(ARMS))
    parser.add_argument("--max-cost-usd", type=float, default=0)
    parser.add_argument("--request-interval-seconds", type=float, default=3.1)
    parser.add_argument("--max-new-requests", type=int)
    args = parser.parse_args()
    try:
        if args.refresh:
            if not args.apply:
                raise ValueError("--apply required to refresh the local view")
            result = refresh_comparison(ROOT, args.source_run_id, maximum_items=args.sample_limit)
        elif args.prepare:
            if not args.apply:
                raise ValueError("--apply required to write the local view")
            result = prepare_comparison_view(ROOT, args.source_run_id)
        elif not args.execute:
            result = plan_comparison(ROOT, args.source_run_id, maximum_items=args.sample_limit,
                providers_subset=[args.provider] if args.provider else None, arms_subset=args.arms)
        else:
            if not args.apply or not args.consent:
                raise ValueError("--apply and --consent required")
            result = execute_comparison(ROOT, args.source_run_id, read_json(args.consent),
                maximum_usd=args.max_cost_usd, progress=lambda p: print(json.dumps(p), flush=True),
                retry_evidence=read_json(args.reviewed_http429_recovery) if args.reviewed_http429_recovery else None,
                providers_subset=[args.provider] if args.provider else None,
                arms_subset=args.arms,
                request_interval_seconds=args.request_interval_seconds, max_new_requests=args.max_new_requests,
                maximum_items=args.sample_limit)
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
