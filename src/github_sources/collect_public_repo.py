#!/usr/bin/env python3
"""Discover prompt and image candidates from allowlisted public GitHub repos.

The collector is deliberately metadata-only. It does not download repository
blobs, follow links in Markdown, infer item rights from a repository license,
write to the canonical archive, or publish anything. Network access requires
``--fetch`` and durable private-research output additionally requires
``--apply``.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PLATFORM_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = PLATFORM_ROOT / "data" / "private-research" / "github-sources"
DEFAULT_REGISTRY = DATA_ROOT / "source_registry.json"
DEFAULT_FIXTURE = DATA_ROOT / "fixtures" / "public_repo_tree_canary.json"
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MAX_CANDIDATES = 5_000
API_VERSION = "2022-11-28"


class CollectorError(RuntimeError):
    """Raised for a fail-closed collector condition."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise CollectorError(f"expected JSON object: {path}")
    return payload


def stable_json(value: Any, *, indent: int | None = 2) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=indent)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(stable_json(value, indent=None).encode("utf-8")).hexdigest()


def normalize_repository(value: str) -> str:
    repository = value.strip().removesuffix(".git")
    if not REPO_PATTERN.fullmatch(repository):
        raise CollectorError(f"invalid GitHub repository identifier: {value!r}")
    return repository


def source_by_repository(registry: dict[str, Any], repository: str) -> dict[str, Any]:
    policy = registry.get("policy") or {}
    if policy.get("allowlist_only") is not True:
        raise CollectorError("registry must remain allowlist_only")
    for raw in registry.get("sources") or []:
        if not isinstance(raw, dict) or raw.get("enabled") is not True:
            continue
        if normalize_repository(str(raw.get("repository") or "")) == repository:
            return raw
    raise CollectorError(f"repository is not enabled in the allowlist: {repository}")


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    normalized = path.replace("\\", "/")
    return any(fnmatch.fnmatchcase(normalized, pattern) for pattern in patterns)


def classify_tree(source: dict[str, Any], tree: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    prompt_globs = [str(item) for item in source.get("prompt_path_globs") or []]
    media_globs = [str(item) for item in source.get("media_path_globs") or []]
    candidates: list[dict[str, Any]] = []
    ignored = 0
    for row in tree:
        if not isinstance(row, dict) or row.get("type") != "blob":
            continue
        path = str(row.get("path") or "")
        kind = "prompt_container" if matches_any(path, prompt_globs) else "media" if matches_any(path, media_globs) else None
        if kind is None:
            ignored += 1
            continue
        blob_sha = str(row.get("sha") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", blob_sha):
            raise CollectorError(f"invalid Git blob SHA for {path}")
        candidates.append(
            {
                "path": path,
                "kind": kind,
                "git_blob_sha1": blob_sha,
                "byte_size_reported": row.get("size") if isinstance(row.get("size"), int) else None,
            }
        )
    candidates.sort(key=lambda item: (item["kind"], item["path"]))
    limited = candidates[:limit]
    return limited, {
        "tree_blob_count": sum(1 for row in tree if isinstance(row, dict) and row.get("type") == "blob"),
        "matched_before_limit": len(candidates),
        "ignored_blob_count": ignored,
        "limited_out": max(0, len(candidates) - len(limited)),
    }


def deduplicate_candidates(candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    duplicate_aliases = 0
    for row in candidates:
        identity = (row["kind"], row["git_blob_sha1"])
        existing = by_identity.get(identity)
        if existing is None:
            canonical = dict(row)
            canonical["path_aliases"] = []
            by_identity[identity] = canonical
            continue
        existing["path_aliases"].append(row["path"])
        duplicate_aliases += 1
    rows = sorted(by_identity.values(), key=lambda item: (item["kind"], item["path"]))
    return rows, duplicate_aliases


def api_get(path: str, token: str | None = None, *, allow_not_found: bool = False) -> tuple[dict[str, Any], dict[str, str]]:
    url = f"https://api.github.com{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "image-archive-source-canary/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            response_headers = {key.lower(): value for key, value in response.headers.items()}
    except urllib.error.HTTPError as exc:
        if allow_not_found and exc.code == 404:
            return {}, {key.lower(): value for key, value in exc.headers.items()}
        retry_after = exc.headers.get("Retry-After")
        remaining = exc.headers.get("X-RateLimit-Remaining")
        suffix = f" retry_after={retry_after!r} remaining={remaining!r}"
        raise CollectorError(f"GitHub API {exc.code} for {path}.{suffix}") from exc
    except urllib.error.URLError as exc:
        raise CollectorError(f"GitHub API network failure for {path}: {exc.reason}") from exc
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise CollectorError(f"GitHub API returned a non-object for {path}")
    return payload, response_headers


def live_fixture(repository: str) -> tuple[dict[str, Any], dict[str, str]]:
    token = os.environ.get("SOURCE_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    repo_payload, repo_headers = api_get(f"/repos/{repository}", token)
    if repo_payload.get("private") is not False:
        raise CollectorError("only public repositories are allowed")
    default_branch = str(repo_payload.get("default_branch") or "")
    if not default_branch:
        raise CollectorError("repository has no default branch")
    tree_payload, tree_headers = api_get(f"/repos/{repository}/git/trees/{default_branch}?recursive=1", token)
    if tree_payload.get("truncated") is True:
        raise CollectorError("recursive tree response was truncated")
    tree = tree_payload.get("tree")
    if not isinstance(tree, list):
        raise CollectorError("repository tree is missing")
    license_payload, license_headers = api_get(f"/repos/{repository}/license", token, allow_not_found=True)
    license_info = license_payload.get("license") if isinstance(license_payload.get("license"), dict) else repo_payload.get("license")
    fixture = {
        "schema_version": "github-public-repo-live-snapshot-1.0",
        "repository": {
            "full_name": repo_payload.get("full_name"),
            "html_url": repo_payload.get("html_url"),
            "default_branch": default_branch,
            "stargazers_count": repo_payload.get("stargazers_count"),
            "pushed_at": repo_payload.get("pushed_at"),
            "license": license_info,
        },
        "commit_sha": tree_payload.get("sha"),
        "tree": tree,
    }
    headers = {
        "repo_etag": repo_headers.get("etag", ""),
        "tree_etag": tree_headers.get("etag", ""),
        "license_etag": license_headers.get("etag", ""),
        "rate_limit": repo_headers.get("x-ratelimit-limit", ""),
        "rate_remaining": repo_headers.get("x-ratelimit-remaining", ""),
        "rate_reset": repo_headers.get("x-ratelimit-reset", ""),
    }
    return fixture, headers


def build_result(source: dict[str, Any], fixture: dict[str, Any], *, mode: str, headers: dict[str, str], limit: int) -> dict[str, Any]:
    repository = normalize_repository(str(fixture.get("repository", {}).get("full_name") or source["repository"]))
    commit_sha = str(fixture.get("commit_sha") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
        raise CollectorError("snapshot has no valid commit SHA")
    tree = fixture.get("tree")
    if not isinstance(tree, list):
        raise CollectorError("snapshot tree must be a list")
    candidates, counts = classify_tree(source, tree, limit)
    unique, duplicate_aliases = deduplicate_candidates(candidates)
    repo_meta = fixture.get("repository") if isinstance(fixture.get("repository"), dict) else {}
    license_meta = repo_meta.get("license") if isinstance(repo_meta.get("license"), dict) else {}
    spdx = str(license_meta.get("spdx_id") or source.get("observed_repository_license") or "NOASSERTION")
    for row in unique:
        row.update(
            {
                "repository": repository,
                "repository_commit_sha": commit_sha,
                "source_url": f"https://github.com/{repository}/blob/{commit_sha}/{row['path']}",
                "raw_candidate_url": f"https://raw.githubusercontent.com/{repository}/{commit_sha}/{row['path']}",
                "repository_license_spdx": spdx,
                "license_scope": "repository_observed_not_item_clearance",
                "rights_status": "private_reference_only",
                "rights_tier": "P3",
                "portfolio_visibility": "admin_only",
                "public_release_status": "metadata_only",
                "binary_downloaded": False,
            }
        )
    result: dict[str, Any] = {
        "schema_version": "image-archive-github-source-canary-1.0",
        "observed_at": utc_now(),
        "mode": mode,
        "source_id": source.get("source_id"),
        "repository": repository,
        "repository_url": repo_meta.get("html_url") or f"https://github.com/{repository}",
        "repository_commit_sha": commit_sha,
        "default_branch": repo_meta.get("default_branch"),
        "stars_observed": repo_meta.get("stargazers_count"),
        "pushed_at_observed": repo_meta.get("pushed_at"),
        "repository_license_spdx": spdx,
        "rights_policy": {
            "repository_license_is_item_rights_clearance": False,
            "download_binaries": False,
            "follow_external_links": False,
            "canonical_promotion": False,
            "public_release": False,
        },
        "counts": {
            **counts,
            "candidate_count": len(candidates),
            "unique_candidate_count": len(unique),
            "exact_blob_alias_count": duplicate_aliases,
        },
        "rate_limit_observation": headers,
        "candidates": unique,
    }
    basis = {
        key: value
        for key, value in result.items()
        if key not in {"observed_at", "content_sha256", "rate_limit_observation"}
    }
    result["content_sha256"] = sha256_json(basis)
    return result


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(stable_json(value) + "\n", encoding="utf-8")
    temp.replace(path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--repo", help="Exact owner/repository allowlist entry")
    parser.add_argument("--fetch", action="store_true", help="Read public GitHub REST metadata")
    parser.add_argument("--apply", action="store_true", help="Persist a private-research run artifact")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--report", type=Path, help="Write an explicit canary report (for ephemeral CI artifacts)")
    parser.add_argument("--quiet", action="store_true", help="Suppress JSON stdout after validation")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not 1 <= args.limit <= MAX_CANDIDATES:
        raise CollectorError(f"limit must be between 1 and {MAX_CANDIDATES}")
    registry = read_json(args.registry)
    if args.fetch:
        if not args.repo:
            raise CollectorError("--fetch requires --repo")
        repository = normalize_repository(args.repo)
        source = source_by_repository(registry, repository)
        fixture, headers = live_fixture(repository)
        mode = "live_metadata_canary"
    else:
        fixture = read_json(args.fixture)
        repository = normalize_repository(str(fixture.get("repository", {}).get("full_name") or ""))
        if repository == "example/reference-gallery":
            source = {
                "source_id": "offline-fixture",
                "repository": repository,
                "prompt_path_globs": ["README*.md", "docs/*.md", "docs/**/*.md"],
                "media_path_globs": ["assets/*.png", "assets/**/*.png"],
                "observed_repository_license": "MIT",
            }
        else:
            source = source_by_repository(registry, repository)
        headers = {}
        mode = "offline_fixture"
    result = build_result(source, fixture, mode=mode, headers=headers, limit=args.limit)
    if args.apply:
        run_path = DATA_ROOT / "runs" / (
            f"github-source-{result['repository'].replace('/', '--')}-"
            f"{result['repository_commit_sha'][:12]}-{result['content_sha256'][:12]}.json"
        )
        if run_path.exists():
            existing = read_json(run_path)
            if existing.get("content_sha256") != result.get("content_sha256"):
                raise CollectorError(f"immutable run collision: {run_path}")
        else:
            write_json_atomic(run_path, result)
        result["private_run_artifact"] = str(run_path.relative_to(PLATFORM_ROOT)).replace("\\", "/")
    if args.report:
        write_json_atomic(args.report, result)
    if not args.quiet:
        print(stable_json(result))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CollectorError as exc:
        print(stable_json({"ok": False, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(2)
