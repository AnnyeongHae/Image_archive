#!/usr/bin/env python3
"""Validate the local OpenNana review queue contract."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "data" / "private-research" / "opennana"
OUTPUT_JS = ROOT / "legacy" / "current_archive" / "opennana-review-data.js"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, passed: bool, detail: str) -> None:
      checks.append((name, passed, detail))

    for rel in [
        "config.json",
        "state.json",
        "raw/README.md",
        "staging/README.md",
        "review_queue/README.md",
        "decisions/README.md",
        "runs/README.md",
    ]:
        path = BASE / rel
        check(rel, path.is_file(), str(path))

    config = read_json(BASE / "config.json")
    check("config_source_id", bool(config.get("source_id")), str(config.get("source_id")))
    check("config_public_release_disabled", config.get("public_release_allowed") is False, str(config.get("public_release_allowed")))

    state = read_json(BASE / "state.json")
    check("state_schema", state.get("schema_version") == "opennana-state-1.0", str(state.get("schema_version")))

    js_ok = OUTPUT_JS.is_file()
    js_text = OUTPUT_JS.read_text(encoding="utf-8") if js_ok else ""
    check("projection_exists", js_ok, OUTPUT_JS.as_posix())
    check("projection_global", "window.OPENNANA_REVIEW_QUEUE =" in js_text, "global assignment missing")

    failures = [name for name, passed, _ in checks if not passed]
    print(json.dumps({
        "ok": not failures,
        "check_count": len(checks),
        "failures": failures,
        "checks": [{"name": name, "passed": passed, "detail": detail} for name, passed, detail in checks],
    }, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
