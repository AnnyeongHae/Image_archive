#!/usr/bin/env python3
"""Build a local review-queue projection for the approval dashboard.

Dry-run is the default. Apply mode writes only the browser projection JS and
the private state file. Canonical archive records remain untouched.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from .build_review_queue import build_projection, projection_javascript
except ImportError:
    from build_review_queue import build_projection, projection_javascript


ROOT = Path(__file__).resolve().parents[2]
QUEUE_DIR = ROOT / "data" / "private-research" / "opennana" / "review_queue"
CONFIG_PATH = ROOT / "data" / "private-research" / "opennana" / "config.json"
STATE_PATH = ROOT / "data" / "private-research" / "opennana" / "state.json"
OUTPUT_JS = ROOT / "legacy" / "current_archive" / "opennana-review-data.js"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def projection_payload() -> dict:
    config = read_json(CONFIG_PATH)
    queue = read_json(QUEUE_DIR / "current.json")
    return build_projection(queue, config)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8", newline="\n")
    temp.replace(path)


def build(apply: bool) -> int:
    payload = projection_payload()
    if not apply:
        print(json.dumps({"mode": "dry_run", "summary": payload["summary"], "source_files": payload["source_files"]}, ensure_ascii=False, indent=2))
        return 0

    write_text(OUTPUT_JS, projection_javascript(payload))

    state = read_json(STATE_PATH)
    state["last_queue_build_at"] = payload["generated_at"]
    state["last_queue_item_count"] = payload["summary"]["total_items"]
    write_text(STATE_PATH, json.dumps(state, ensure_ascii=False, indent=2) + "\n")

    print(json.dumps({"mode": "apply", "output": OUTPUT_JS.relative_to(ROOT).as_posix(), "summary": payload["summary"]}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write opennana-review-data.js and state.json")
    args = parser.parse_args()
    return build(args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
