from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "github-source-daily-observation.yml"
PINNED_ACTION = re.compile(
    r"^\s*uses:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@([0-9a-f]{40})\s*$",
    re.MULTILINE,
)
ANY_ACTION = re.compile(r"^\s*uses:\s*([^\s#]+)", re.MULTILINE)


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def main() -> int:
    text = WORKFLOW.read_text(encoding="utf-8")
    errors: list[str] = []
    require("schedule:" in text, "daily schedule missing", errors)
    require("workflow_dispatch:" in text, "manual trigger missing", errors)
    require("contents: read" in text, "permissions must remain contents: read", errors)
    require("run_registry_observation.py" in text, "registry observer missing", errors)
    require("--fetch" in text and "--report" in text, "explicit fetch/report gates missing", errors)
    forbidden = {
        "contents: write": "contents write is forbidden",
        "git push": "git push is forbidden",
        "DATABASE_URL": "database secret is forbidden in metadata observation",
        "NEON_": "Neon credentials are forbidden in metadata observation",
        "pip install": "runtime package installation is forbidden",
        "--apply": "canonical or database apply is forbidden",
        "download_images": "image download is forbidden",
    }
    for fragment, message in forbidden.items():
        require(fragment not in text, message, errors)
    all_actions = ANY_ACTION.findall(text)
    pinned = PINNED_ACTION.findall(text)
    require(bool(all_actions), "workflow has no actions", errors)
    require(len(all_actions) == len(pinned), "every action must be pinned to a full SHA", errors)
    if errors:
        for error in errors:
            print(error)
        return 1
    print({"ok": True, "workflow": WORKFLOW.name, "pinned_actions": len(pinned)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
