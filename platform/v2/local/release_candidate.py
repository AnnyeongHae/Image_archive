"""Freeze an approval candidate, not a deployment. Offline/dry-run by default."""
import argparse
import json
from pathlib import Path
import sys

from cloud_snapshot import ROOT, SnapshotError, encoded, private_path, sha

sys.path.insert(0, str(ROOT / "qa"))
import validate_repository_boundary as boundary


def candidate(runtime_directory):
    runtime = private_path(runtime_directory, ROOT)
    config_raw = private_path(runtime / "wrangler.runtime.json", ROOT).read_bytes()
    config = json.loads(config_raw)
    prepared = json.loads(private_path(runtime / "runtime-manifest.json", ROOT).read_bytes())
    variable_names = {"PRIVATE_API_ENABLED", "LIVE_QUERY_EMBEDDING_ENABLED", "DAILY_QUERY_CALL_LIMIT",
        "DAILY_QUERY_TOKEN_LIMIT", "ACCESS_JWT_REQUIRED", "TEAM_DOMAIN", "POLICY_AUD", "OWNER_EMAIL_ALLOWLIST",
        "SNAPSHOT_ID", "SNAPSHOT_MANIFEST_SHA256", "TEXT_COLLECTION"}
    base = json.loads((ROOT / "platform/v2/wrangler.jsonc").read_bytes())
    if (prepared.get("files", {}).get("wrangler.runtime.json") != sha(config_raw)
        or set(config.get("vars", {})) != variable_names or set(config) != set(base)
        or any(config[key] != base[key] for key in base if key not in {"main", "vars"})
        or boundary.content_errors("wrangler.candidate.json", config_raw)):
        raise SnapshotError("runtime_candidate_drift_or_secret")
    if (config.get("name") != "image-archive-owner-api-v2" or
        config.get("vars", {}).get("LIVE_QUERY_EMBEDDING_ENABLED") != "false"):
        raise SnapshotError("unreviewed_runtime_scope")
    config["main"] = "worker.bundle.mjs"
    config["no_bundle"] = True
    bundle_path = ROOT / "platform/v2/.tmp/v2-worker-dry-run/index.js"
    bundle = bundle_path.read_bytes()
    if not 0 < len(bundle) < 1048576:
        raise SnapshotError("worker_bundle_size_refused")
    paths = boundary.candidate_paths()
    allowed = [p for p in paths if not boundary.path_error(p)]
    rejected = [p for p in paths if boundary.path_error(p)]
    if not allowed or boundary.validate_paths(allowed, worktree=True):
        raise SnapshotError("source_boundary_failed")
    sources = {}
    for path in allowed:
        source_bytes = boundary._working_bytes(path)
        sources[path] = {"sha256": sha(source_bytes), "bytes": len(source_bytes)}
    source_record = {"schema_version": "public-source-candidate-1", "files": sources,
                     "excluded_local_paths": rejected, "private_data_included": False}
    files = {"worker.bundle.mjs": bundle, "wrangler.candidate.json": encoded(config),
             "public-source-manifest.json": encoded(source_record)}
    manifest = {"schema_version": "image-archive-v2-release-candidate-1",
      "state": "review_pending", "eligible_for_release": False,
      "files": {name: {"sha256": sha(raw), "bytes": len(raw)} for name, raw in files.items()},
      "targets": {"worker": "image-archive-owner-api-v2",
                  "cloudflare_account_id": "b39fad7b5ebf74e820209ed506fd989b",
                  "repository": "https://github.com/AnnyeongHae/Image_archive.git", "branch": "main"},
      "snapshot_id": config["vars"]["SNAPSHOT_ID"],
      "snapshot_manifest_sha256": config["vars"]["SNAPSHOT_MANIFEST_SHA256"],
      "source_files": len(allowed), "excluded_local_files": len(rejected),
      "new_embedding_enabled": False, "public_gallery_changed": False,
      "pending": ["confirm_owner_identity", "authorize_restricted_neon_role", "authorize_private_r2_canary",
                  "confirm_exact_worker_artifact_and_target", "github_cli_authentication",
                  "access_hostname_integration", "qdrant_key_scope_review", "deployed_smoke_and_cpu_test"],
      "secrets_included": False, "deployment_performed": False}
    return manifest, files


def freeze(runtime_directory, *, apply=False):
    manifest, files = candidate(runtime_directory)
    raw = encoded(manifest)
    digest = sha(raw)
    directory = private_path(ROOT / "data/private-research/platform-v2/release-candidates" / digest, ROOT)
    if apply:
        directory.mkdir(parents=True, exist_ok=True)
        for name, data in {**files, "candidate.json": raw}.items():
            target = private_path(directory / name, ROOT)
            if target.exists():
                if target.read_bytes() != data:
                    raise SnapshotError("immutable_release_candidate_conflict")
            else:
                with target.open("xb") as handle:
                    handle.write(data)
    return {"status": "prepared_for_review" if apply else "dry_run", "artifact_sha256": digest,
            "candidate_directory": str(directory.relative_to(ROOT)),
            "source_files": manifest["source_files"], "source_bytes": sum(r["bytes"] for r in json.loads(files["public-source-manifest.json"])["files"].values()),
            "eligible_for_release": False, "deployment_performed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-directory", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(freeze(args.runtime_directory, apply=args.apply)))
    except Exception as error:
        print(json.dumps({"status": "failed", "code": str(error) if isinstance(error, SnapshotError) else "release_candidate_failed"}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
