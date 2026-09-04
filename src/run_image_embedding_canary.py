"""Run without arguments for a write-free, offline experiment plan."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from image_rag_eval.experiment import execute, plan, prepare, read_json
from image_rag_eval.providers import credential_presence, load_credentials, preflight, ProviderError

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--prepare", action="store_true", help="private local sample/HTML/hash analysis")
    action.add_argument("--preflight", action="store_true", help="read-only model and Qdrant connectivity, no inference")
    action.add_argument("--execute", action="store_true", help="approved bounded paid Gemini embedding calls")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--run-id", default="2026-09-03-first20")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--annotations", type=Path)
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--max-cost-usd", type=float, default=0)
    args = parser.parse_args()
    try:
        credentials = load_credentials([ROOT / ".env"])
        if (args.prepare or args.execute) and not args.apply:
            raise ValueError("--apply is required for local artifact writes")
        if args.prepare:
            result = prepare(ROOT, args.run_id, args.limit)
        elif args.preflight:
            result = preflight(credentials)
        elif args.execute:
            if args.annotations is None:
                raise ValueError("--annotations with human-reviewed inputs is required")
            result = execute(ROOT, args.run_id, read_json(args.annotations), credentials,
                allow_paid=args.allow_paid, maximum_usd=args.max_cost_usd)
            result = {key: value for key, value in result.items() if key not in {"retrieval", "pairs", "human_pair_labels"}}
        else:
            result = plan(args.limit)
            result["credentials_present"] = credential_presence(credentials)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ProviderError as exc:
        print(json.dumps({"status": "blocked", "provider": exc.provider, "http_status": exc.http_status}))
        return 2
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        # These exception types contain local guard messages only, never provider bodies.
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False))
        return 2
    except Exception as exc:
        # No tracebacks: chained transport exceptions can contain sensitive request context.
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
