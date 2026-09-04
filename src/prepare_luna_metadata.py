"""Prepare a private Luna metadata plan only. No model call, no network, dry-run by default."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from image_rag_eval.luna_metadata import PROMPT_MODES, prepare_luna_metadata


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--max-items", type=int, default=200)
    parser.add_argument("--prompt-mode", choices=PROMPT_MODES, default="image_plus_prompt")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = prepare_luna_metadata(
            ROOT,
            args.source_run_id,
            apply=args.apply,
            maximum_items=args.max_items,
            prompt_mode=args.prompt_mode,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
