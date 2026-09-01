from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_TRACKED_BYTES = 50 * 1024 * 1024
ALLOWED_PRIVATE_PREFIX = "data/private-research/github-sources/fixtures/"
ALLOWED_PRIVATE_FILES = {
    "data/private-research/github-sources/source_registry.json",
}
DENIED_PREFIXES = (
    "legacy/",
    "experiments/",
    "media/",
    "dist/",
    "reports/",
    "data/public-export/",
    "data/canonical/",
    "app/data/",
    ".tmp/",
)
DENIED_SUFFIXES = (".sqlite", ".sqlite3", ".db", ".jsonl")
SECRET_PATTERNS = (
    re.compile(r"postgres(?:ql)?://[^:\s/]+:[^@\s/]+@", re.IGNORECASE),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
)
TEXT_SUFFIXES = {
    ".css", ".csv", ".html", ".ini", ".js", ".json", ".md", ".mjs",
    ".ps1", ".py", ".svg", ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml",
}


def tracked_paths() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return sorted(item.decode("utf-8") for item in result.stdout.split(b"\0") if item)


def main() -> int:
    errors: list[str] = []
    paths = tracked_paths()
    if not paths:
        raise SystemExit("no tracked files; repository boundary cannot be validated")

    for relative in paths:
        normalized = relative.replace("\\", "/")
        lower = normalized.casefold()
        path = ROOT / relative
        if lower == ".env" or (lower.startswith(".env.") and lower != ".env.example"):
            errors.append(f"secret environment file tracked: {normalized}")
            continue
        if lower.startswith("data/private-research/"):
            allowed = lower in ALLOWED_PRIVATE_FILES or lower.startswith(ALLOWED_PRIVATE_PREFIX)
            if not allowed:
                errors.append(f"private research tracked: {normalized}")
                continue
        if lower.startswith(DENIED_PREFIXES):
            errors.append(f"generated/private path tracked: {normalized}")
            continue
        if lower.endswith(DENIED_SUFFIXES):
            errors.append(f"bulk database/data file tracked: {normalized}")
            continue
        if not path.is_file():
            errors.append(f"tracked file missing: {normalized}")
            continue
        size = path.stat().st_size
        if size > MAX_TRACKED_BYTES:
            errors.append(f"tracked file exceeds 50 MiB: {normalized} ({size} bytes)")
            continue
        if path.suffix.casefold() not in TEXT_SUFFIXES or size > 2 * 1024 * 1024:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if normalized == ".env.example":
            text = "\n".join(line.split("=", 1)[0] + "=" for line in text.splitlines())
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                errors.append(f"credential-like value tracked: {normalized}")
                break

    if errors:
        for error in errors:
            print(error)
        return 1
    print({"ok": True, "tracked_files": len(paths), "max_tracked_bytes": MAX_TRACKED_BYTES})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
