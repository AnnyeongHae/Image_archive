from __future__ import annotations

import argparse
import json
import re
import stat
import subprocess
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
MAX_TRACKED_BYTES = 2 * 1024 * 1024
ALLOWED_PRIVATE_FILES = {
    "data/private-research/github-sources/source_registry.json",
    "data/private-research/github-sources/fixtures/public_repo_tree_canary.json",
}
ALLOWED_FILES = ALLOWED_PRIVATE_FILES | {
    ".env.example", ".gitignore", "agents.md", "readme.md", "favicon.svg",
    "platform.config.json", "app/index.html", "config/intake-recipient.public.jwk.json",
    "platform/v2/readme.md", "platform/v2/package.json", "platform/v2/wrangler.jsonc",
    "deploy/cloudflare-public/readme.md", "deploy/cloudflare-public/wrangler.jsonc",
    "deploy/cloudflare-staging/readme.md", "deploy/cloudflare-staging/wrangler.jsonc",
    "deploy/cloudflare-staging/public/index.html", "deploy/cloudflare-staging/public/404.html",
    "deploy/cloudflare-staging/public/_headers", "deploy/cloudflare-staging/public/robots.txt",
    "deploy/cloudflare-staging/public/app.css",
    "deploy/cloudflare-staging/public/admin/index.html",
    "deploy/cloudflare-staging/public/admin/admin.css",
    "deploy/cloudflare-staging/public/admin/admin.js",
}
ALLOWED_SOURCE_PREFIXES = (
    "src/", "qa/", "docs/", "schemas/", "scripts/", ".github/workflows/",
    "db/migrations/", "db/metadata/", "db/v2/", "app/image-admin/", "app/scripts/", "app/styles/",
    "platform/v2/local/", "platform/v2/worker/", "platform/v2/tests/",
    "deploy/cloudflare-public/worker/", "deploy/cloudflare-public/source/",
    "deploy/cloudflare-staging/worker/",
)
DENIED_PREFIXES = (
    "legacy/", "experiments/", "media/", "assets/", "dist/", "reports/",
    "data/public-export/", "data/canonical/", "app/data/", "platform/v2/runtime/",
    "deploy/cloudflare-public/public/", "deploy/cloudflare-public/public-backup",
)
DENIED_PARTS = {".git", ".tmp", ".wrangler", ".wrangler-dry-run", "node_modules", "__pycache__", ".venv"}
DENIED_SUFFIXES = (".sqlite", ".sqlite3", ".db", ".jsonl", ".zip", ".bundle", ".pem", ".key", ".pfx", ".p12", ".private.jwk.json")
DSN_PATTERN = re.compile(r"postgres(?:ql)?://[^:\s/'\"]+:[^@\s/'\"]+@[^\s'\"<>`]+", re.IGNORECASE)
# Exact synthetic fixtures only: do not exempt a directory or arbitrary test DSNs.
# Split protocol literals keep this policy's own source from masquerading as a credential.
SYNTHETIC_DSNS = {
    "platform/v2/tests/runtime.test.mjs": {
        "postgres" + "ql://user:synthetic-password@ep-unit-test.us-east-2.aws.neon.tech/db?sslmode=require",
        "postgres" + "://u:p@ep-test.us-east-2.aws.neon.tech/db",
        "postgres" + "://u:p@evil.test/db",
        "postgres" + "://u:p@neon.tech.evil.test/db",
    },
    "qa/test_v2_cloud_snapshot.py": {
        "postgres" + "ql://owner:fake@ep-test.neon.tech/neondb",
        "postgres" + "ql://owner:fake@ep-test.neon.tech/neondb?",
        "postgres" + "ql://owner:fake@ep-test.neon.tech:5432/neondb?sslmode=verify-full&channel_binding=require",
    },
    "platform/v2/tests/media.test.mjs": {
        "postgres" + "ql://fixture:synthetic-password@ep-unit.us-east-2.aws.neon.tech/db?sslmode=require",
    },
    "qa/test_v2_runtime_setup.py": {
        "postgres" + "ql://owner:SYNTHETIC_ADMIN_PASSWORD@ep-test.us.aws.neon.tech/neondb?sslmode=require",
    },
}
SYNTHETIC_ASSIGNMENTS = {
    "qa/test_text_embedding_run.py": {'api_' + 'key="CANARY_SECRET_DO_NOT_PERSIST"'},
    "src/evaluate_image_text_embeddings.py": {'api_' + 'key="unused-offline-cache-replay"'},
    "qa/test_v2_runtime_setup.py": {'API_' + 'KEY": "SYNTHETIC_QDRANT_KEY"'},
}
LITERAL_SECRET_PATTERN = re.compile(r'''(?:API_KEY|API_TOKEN|ACCESS_TOKEN|ADMIN_TOKEN|CLIENT_SECRET)["']?\s*[:=]\s*["'][A-Za-z0-9_-]{20,}["']''', re.IGNORECASE)
SECRET_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\biar_v2_[A-Za-z0-9_-]{43,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    re.compile(r'"d"\s*:\s*"[A-Za-z0-9_-]{128,}"'),
)
TEXT_SUFFIXES = {
    ".cjs", ".css", ".csv", ".html", ".ini", ".js", ".json", ".jsonc", ".md", ".mjs",
    ".ps1", ".py", ".sql", ".svg", ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml",
}


def git_bytes(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True).stdout


def index_entries() -> dict[str, tuple[str, str]]:
    entries: dict[str, tuple[str, str]] = {}
    for item in git_bytes("ls-files", "--stage", "-z").split(b"\0"):
        if not item:
            continue
        metadata, path = item.split(b"\t", 1)
        mode, oid, stage = metadata.decode("ascii").split()
        if stage != "0":
            raise ValueError("unmerged index cannot be validated")
        entries[path.decode("utf-8")] = (mode, oid)
    return entries


def tracked_paths() -> list[str]:
    return sorted(index_entries())


def candidate_paths() -> list[str]:
    untracked = [item.decode("utf-8") for item in git_bytes("ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if item]
    return sorted(set(tracked_paths()) | set(untracked))


def path_error(relative: str) -> str | None:
    normalized = relative.replace("\\", "/")
    parts = normalized.casefold().split("/")
    if not normalized or normalized.startswith("/") or any(part in {"", ".", ".."} for part in parts) or ":" in normalized or "\0" in normalized:
        return "unsafe repository path"
    lower = normalized.casefold()
    if any(part in DENIED_PARTS for part in parts):
        return "private/generated directory"
    if any(part == ".env" or part.startswith(".env.") or part == ".dev.vars" or part.startswith(".dev.vars.") or part.endswith(".env") for part in parts) and lower != ".env.example":
        return "secret environment file"
    if lower.startswith(DENIED_PREFIXES) or lower.endswith(DENIED_SUFFIXES):
        return "private/generated artifact"
    if lower.startswith("data/private-research/") and lower not in ALLOWED_PRIVATE_FILES:
        return "unapproved private research"
    if lower in ALLOWED_FILES:
        return None
    if not lower.startswith(ALLOWED_SOURCE_PREFIXES):
        return "outside source-only allowlist"
    if PurePosixPath(lower).suffix not in TEXT_SUFFIXES and parts[-1] != "_headers":
        return "non-source file type"
    return None


def content_errors(relative: str, raw: bytes) -> list[str]:
    if len(raw) > MAX_TRACKED_BYTES:
        return ["source exceeds 2 MiB scan limit"]
    if b"\0" in raw:
        return ["binary content is not public source"]
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ["source is not valid UTF-8"]
    errors: list[str] = []
    normalized = relative.replace("\\", "/")
    allowed_dsns = SYNTHETIC_DSNS.get(normalized, set())
    allowed_assignments = SYNTHETIC_ASSIGNMENTS.get(normalized, set())
    if any(match.group() not in allowed_dsns for match in DSN_PATTERN.finditer(text)) or any(pattern.search(text) for pattern in SECRET_PATTERNS) or any(match.group() not in allowed_assignments for match in LITERAL_SECRET_PATTERN.finditer(text)):
        errors.append("credential-like value")
    if normalized.casefold() == ".env.example":
        for line in text.splitlines():
            key, separator, value = line.partition("=")
            if separator and re.search(r"(?:DATABASE_URL|DATABASE_KEY|API_KEY|TOKEN|SECRET|PASSWORD)$", key.strip(), re.IGNORECASE) and value.strip():
                errors.append("secret field in environment example must be empty")
                break
    if normalized.casefold() == "config/intake-recipient.public.jwk.json":
        try:
            key = json.loads(text)
            if not isinstance(key, dict) or key.get("kty") != "RSA" or not key.get("n") or not key.get("e") or set(key) & {"d", "p", "q", "dp", "dq", "qi", "oth"}:
                errors.append("recipient key must contain public RSA material only")
        except (TypeError, ValueError):
            errors.append("recipient public key is not valid JSON")
    return errors


def _working_bytes(relative: str) -> bytes:
    path = ROOT / relative
    for current in (path, *path.parents):
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("symlink/reparse path is not source")
        if current == ROOT:
            break
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_TRACKED_BYTES:
        raise ValueError("not a regular source file or exceeds 2 MiB")
    return path.read_bytes()


def validate_paths(paths: list[str], *, worktree: bool = False, entries: dict[str, tuple[str, str]] | None = None) -> list[str]:
    errors: list[str] = []
    entries = entries if entries is not None else ({} if worktree else index_entries())
    for relative in paths:
        reason = path_error(relative)
        if reason:
            errors.append(f"{reason}: {relative}")
            continue
        try:
            if worktree:
                raw = _working_bytes(relative)
            else:
                mode, oid = entries[relative]
                if mode not in {"100644", "100755"}:
                    raise ValueError("non-regular Git index entry")
                if int(git_bytes("cat-file", "-s", oid)) > MAX_TRACKED_BYTES:
                    raise ValueError("source exceeds 2 MiB scan limit")
                raw = git_bytes("cat-file", "blob", oid)
            errors.extend(f"{error}: {relative}" for error in content_errors(relative, raw))
        except (KeyError, OSError, ValueError, subprocess.CalledProcessError):
            errors.append(f"source unavailable, oversized, or unsafe: {relative}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail-closed public source boundary. Default scans Git index blobs, not working copies; never stages files.")
    parser.add_argument("--worktree", action="store_true", help="preflight working copies instead of the index")
    parser.add_argument("--include-untracked", action="store_true", help="with --worktree, audit every unignored candidate")
    parser.add_argument("--paths", nargs="+", help="with --worktree, check an exact proposed source list")
    parser.add_argument("--candidate-files", action="store_true", help="print checked source-only candidate/exclusion lists as JSON; does not stage files")
    args = parser.parse_args()
    if args.candidate_files:
        if args.worktree or args.include_untracked or args.paths:
            parser.error("--candidate-files is a standalone read-only proposal mode")
        candidates = candidate_paths()
        allowed = [path for path in candidates if path_error(path) is None]
        excluded = [path for path in candidates if path_error(path) is not None]
        errors = validate_paths(allowed, worktree=True)
        if not allowed:
            errors.append("no source candidates")
        result = {"ok": not errors, "mode": "proposal-only", "paths": allowed if not errors else [], "excluded_paths": excluded, "errors": errors}
        print(json.dumps(result, ensure_ascii=False))
        return int(bool(errors))
    if (args.include_untracked or args.paths) and not args.worktree:
        parser.error("--include-untracked and --paths require --worktree")
    if args.include_untracked and args.paths:
        parser.error("choose either --include-untracked or --paths")
    paths = args.paths or (candidate_paths() if args.include_untracked else tracked_paths())
    if not paths:
        raise SystemExit("no tracked files; repository boundary cannot be validated")
    errors = validate_paths(paths, worktree=args.worktree)
    if errors:
        for error in errors:
            print(error)
        return 1
    print({"ok": True, "files": len(paths), "source": "worktree" if args.worktree else "git-index", "max_tracked_bytes": MAX_TRACKED_BYTES})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
