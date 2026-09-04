from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "github-source-daily-observation.yml"
PINNED_ACTION = re.compile(
    r"^\s*uses:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)@([0-9a-f]{40})\s*$",
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
    require("push:" in text, "path-scoped push trigger missing", errors)
    require("contents: read" in text, "permissions must remain contents: read", errors)
    require("run_registry_observation.py" in text, "registry observer missing", errors)
    require("--fetch" in text and "--report" in text, "explicit fetch/report gates missing", errors)
    require("collect_sealed_intake.py" in text and "--max-containers 20" in text, "bounded sealed intake missing", errors)
    require("seal_intake.mjs verify --public-key" in text, "public recipient preflight missing", errors)
    require(text.find("seal_intake.mjs verify") < text.find("--fetch --collect"), "body collection precedes recipient check", errors)
    require("--acknowledge-artifact-id" in text and "steps.sealed_upload.outputs.artifact-id" in text, "upload acknowledgment missing", errors)
    require("actions/cache/restore@" in text and "actions/cache/save@" in text, "hash checkpoint transport missing", errors)
    require(text.find("--acknowledge-artifact-id") < text.find("actions/cache/save@"), "checkpoint saved before upload acknowledgment", errors)
    forbidden = {
        "contents: write": "contents write is forbidden",
        "git push": "git push is forbidden",
        "DATABASE_URL": "database secret is forbidden in collection",
        "NEON_": "Neon credentials are forbidden in collection",
        "pip install": "runtime package installation is forbidden",
        "--apply": "canonical or database apply is forbidden",
        "download_images": "image download is forbidden",
        "private.jwk": "owner private key is forbidden in Actions",
        "VOYAGE_API_KEY": "embedding credentials are forbidden in collection",
        "QDRANT_API_KEY": "vector-service credentials are forbidden in collection",
    }
    for fragment, message in forbidden.items():
        require(fragment not in text, message, errors)
    all_actions = ANY_ACTION.findall(text)
    pinned = PINNED_ACTION.findall(text)
    require(bool(all_actions), "workflow has no actions", errors)
    require(len(all_actions) == len(pinned), "every action must be pinned to a full SHA", errors)
    # Exact artifact/cache paths only: no directory uploads, globs, plaintext
    # intermediates or private-key parent directories.
    allowed_paths = {"${{ runner.temp }}/archive-intake-checkpoint/checkpoint.json",
                     "${{ runner.temp }}/archive-intake-output/intake.sealed.json",
                     "qa/github-source-daily-observation.json", "qa/github-source-sealed-summary.json"}
    for block in re.split(r"(?m)^      - name:", text)[1:]:
        if "actions/upload-artifact@" not in block and "actions/cache/" not in block:
            continue
        match = re.search(r"(?m)^          path: (.+)$", block)
        require(match is not None, "artifact/cache exact path missing", errors)
        if not match:
            continue
        paths = ([line.strip() for line in block[match.end():].splitlines() if line.startswith("            ")]
                 if match.group(1) == "|" else [match.group(1)])
        require(bool(paths) and all(path in allowed_paths for path in paths), "unsafe artifact/cache path", errors)
    if errors:
        for error in errors:
            print(error)
        return 1
    print({"ok": True, "workflow": WORKFLOW.name, "pinned_actions": len(pinned)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
