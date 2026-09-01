from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "github-source-canary.yml"
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

    require(bool(re.search(r"(?m)^\s{2}workflow_dispatch:\s*$", text)), "manual trigger missing", errors)
    require(not re.search(r"(?m)^\s{2}schedule:\s*$", text), "scheduled trigger is not allowed in canary", errors)
    require(
        bool(re.search(r"(?ms)^permissions:\s*\n\s{2}contents:\s*read\s*(?:\n\S|\Z)", text)),
        "top-level permissions must be contents: read",
        errors,
    )

    forbidden_fragments = {
        "contents: write": "write permission is forbidden",
        "git push": "git push is forbidden",
        "NEON_": "Neon credentials are forbidden in this canary",
        "DATABASE_URL": "database credentials are forbidden in this canary",
        "OPENNANA_ENABLE_LIVE_SYNC": "OpenNana live sync is forbidden in this canary",
    }
    for fragment, message in forbidden_fragments.items():
        require(fragment not in text, message, errors)

    all_actions = ANY_ACTION.findall(text)
    pinned_actions = PINNED_ACTION.findall(text)
    require(bool(all_actions), "workflow has no actions", errors)
    require(
        len(all_actions) == len(pinned_actions),
        "every action must be pinned to a full 40-character commit SHA",
        errors,
    )
    require(
        "python qa/validate_repository_boundary.py" in text,
        "repository boundary validation step missing",
        errors,
    )

    if errors:
        for error in errors:
            print(error)
        return 1
    print({"ok": True, "workflow": WORKFLOW.name, "pinned_actions": len(pinned_actions)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
