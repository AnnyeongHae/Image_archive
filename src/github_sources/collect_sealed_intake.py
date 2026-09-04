"""Bounded GitHub gallery intake. Only sealed prompt artifacts may leave Actions.

Default is offline dry-run. Live collection requires --fetch --collect and a
valid owner public JWK. Checkpoints contain only hashes and source identities;
an upload acknowledgment is required before they become resumable state.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    from . import collect_public_repo as collector
    from .intake_envelope import ADAPTER, SUPPORTED_REPOSITORY, encode, parse_gallery, sha256, supported_container
    from .run_registry_observation import enabled_sources
except ImportError:
    import collect_public_repo as collector
    from intake_envelope import ADAPTER, SUPPORTED_REPOSITORY, encode, parse_gallery, sha256, supported_container
    from run_registry_observation import enabled_sources

ROOT = collector.PLATFORM_ROOT
SEALER = Path(__file__).with_name("seal_intake.mjs")
PUBLIC_KEY = ROOT / "config/intake-recipient.public.jwk.json"
MAX_CONTAINERS = 20
MAX_CONTAINER_BYTES = 4 * 1024 * 1024
CHECKPOINT_SCHEMA = "archive-intake-checkpoint-1"
CHECKPOINT_FIELDS = {"schema_version", "adapter_version", "entries", "content_sha256"}
ENTRY_FIELDS = {"source_id", "repository", "path", "git_blob_sha1", "repository_commit_sha",
                "repository_tree_sha", "source_container_sha256", "media_blob_sha1", "artifact_id", "last_sealed_at"}


class IntakeError(RuntimeError):
    pass


def _digest_checkpoint(value):
    return sha256(encode({key: item for key, item in value.items() if key != "content_sha256"}))


def empty_checkpoint():
    result = {"schema_version": CHECKPOINT_SCHEMA, "adapter_version": ADAPTER, "entries": {}}
    result["content_sha256"] = _digest_checkpoint(result)
    return result


def validate_checkpoint(value, *, pending=False):
    if (not isinstance(value, dict) or set(value) != CHECKPOINT_FIELDS
            or value.get("schema_version") != CHECKPOINT_SCHEMA or value.get("adapter_version") != ADAPTER
            or not isinstance(value.get("entries"), dict) or len(value["entries"]) > 10000
            or value.get("content_sha256") != _digest_checkpoint(value)):
        raise IntakeError("invalid_metadata_checkpoint")
    for key, entry in value["entries"].items():
        if (not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS
                or key != sha256((str(entry.get("source_id")) + "\n" + str(entry.get("path"))).encode())
                or entry.get("repository") != SUPPORTED_REPOSITORY
                or not supported_container(entry["repository"], str(entry.get("path")))
                or not isinstance(entry.get("source_id"), str) or not 1 <= len(entry["source_id"]) <= 200
                or not re.fullmatch(r"[a-f0-9]{64}", str(entry.get("source_container_sha256")))):
            raise IntakeError("invalid_metadata_checkpoint_entry")
        for field in ("git_blob_sha1", "repository_commit_sha", "repository_tree_sha"):
            if not re.fullmatch(r"[a-f0-9]{40}", str(entry.get(field))):
                raise IntakeError("invalid_checkpoint_git_identity")
        try:
            stamp = datetime.fromisoformat(entry["last_sealed_at"])
            if stamp.tzinfo is None:
                raise ValueError("timezone required")
        except (TypeError, ValueError):
            raise IntakeError("invalid_checkpoint_time") from None
        if not isinstance(entry["media_blob_sha1"], dict) or len(entry["media_blob_sha1"]) > 5000:
            raise IntakeError("invalid_checkpoint_media_metadata")
        for path, digest in entry["media_blob_sha1"].items():
            if not isinstance(path, str) or not 1 <= len(path) <= 1024 or not re.fullmatch(r"[a-f0-9]{40}", str(digest)):
                raise IntakeError("invalid_checkpoint_media_hash")
        if not (pending and entry["artifact_id"] is None) and not re.fullmatch(r"[1-9][0-9]*", str(entry["artifact_id"])):
            raise IntakeError("checkpoint_has_no_upload_acknowledgment")
    return value


PacedAPI = collector.PacedAPI


def fetch_blob(repository, candidate, api):
    expected = candidate["git_blob_sha1"]
    reported_size = candidate.get("byte_size_reported")
    if type(reported_size) is not int or not 1 <= reported_size <= MAX_CONTAINER_BYTES:
        raise IntakeError("container_size_out_of_bounds")
    token = os.environ.get("SOURCE_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    payload, _ = api(f"/repos/{repository}/git/blobs/{expected}", token)
    if payload.get("sha") != expected or payload.get("encoding") != "base64" or payload.get("size") != reported_size:
        raise IntakeError("pinned_blob_response_mismatch")
    content = payload.get("content")
    if not isinstance(content, str) or len(content) > MAX_CONTAINER_BYTES * 2:
        raise IntakeError("container_response_out_of_bounds")
    try:
        raw = base64.b64decode("".join(content.split()), validate=True)
    except ValueError as exc:
        raise IntakeError("invalid_blob_encoding") from exc
    git_sha = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
    if len(raw) != reported_size or git_sha != expected:
        raise IntakeError("pinned_blob_content_mismatch")
    return raw


def collect_bundle(registry, checkpoint, *, max_containers=MAX_CONTAINERS, api=None, snapshot=None):
    if type(max_containers) is not int or not 1 <= max_containers <= MAX_CONTAINERS:
        raise IntakeError("max_containers_must_be_1_to_20")
    validate_checkpoint(checkpoint)
    api = api or PacedAPI()
    snapshot = snapshot or (lambda repo: collector.live_fixture(repo, get=api))
    next_state = copy.deepcopy(checkpoint)
    bundle = {"schema_version": "archive-sealed-intake-bundle-1", "run_id": collector.utc_now(),
              "adapter_version": ADAPTER, "records": [], "containers": [],
              "canonical_promotion": False, "public_release": False, "image_binaries_downloaded": 0}
    summary = {"status": "collected", "containers_fetched": 0, "containers_completed": 0,
               "unchanged": 0, "unsupported_deferred": 0, "bounded_deferred": 0, "records": 0,
               "containers_incomplete": 0, "parser_deferred_items": 0, "media_refs": 0, "media_refs_deferred": 0,
               "failed_sources": [], "private_artifact_required": True, "new_embedding_calls": 0}
    for source in enabled_sources(registry):
        repository = collector.normalize_repository(source["repository"])
        if repository != SUPPORTED_REPOSITORY:
            summary["unsupported_deferred"] += 1
            continue  # Registry expansion is not automatic adapter approval.
        try:
            fixture, _ = snapshot(repository)
            commit, tree_sha = fixture.get("commit_sha"), fixture.get("tree_sha")
            if not all(re.fullmatch(r"[a-f0-9]{40}", str(value)) for value in (commit, tree_sha)):
                raise IntakeError("actual_commit_and_tree_required")
            candidates, counts = collector.classify_tree(source, fixture["tree"], collector.MAX_CANDIDATES)
            if counts["limited_out"]:
                raise IntakeError("tree_candidate_limit_requires_review")
            original_tree = {row["path"]: row for row in fixture["tree"] if row.get("type") == "blob"}
            media_paths = {row["path"] for row in candidates if row["kind"] == "media"}
            media_tree = {path: original_tree[path] for path in media_paths}
            for candidate in candidates:
                path = candidate["path"]
                if candidate["kind"] != "prompt_container":
                    continue
                if not supported_container(repository, path):
                    summary["unsupported_deferred"] += 1
                    continue
                if original_tree[path].get("mode") not in {"100644", "100755"}:
                    raise IntakeError("prompt_container_must_be_regular_blob")
                key = sha256((source["source_id"] + "\n" + path).encode())
                previous = checkpoint["entries"].get(key)
                if (previous and previous["git_blob_sha1"] == candidate["git_blob_sha1"]
                        and datetime.now(timezone.utc) - timedelta(days=7) < datetime.fromisoformat(previous["last_sealed_at"]) <= datetime.now(timezone.utc)
                        and all(media_tree.get(ref, {}).get("sha") == value for ref, value in previous["media_blob_sha1"].items())):
                    summary["unchanged"] += 1
                    continue
                if summary["containers_fetched"] >= max_containers:
                    summary["bounded_deferred"] += 1
                    continue
                raw = fetch_blob(repository, candidate, api)
                summary["containers_fetched"] += 1
                parsed = parse_gallery(raw, source=source, path=path, commit=commit, tree_sha=tree_sha,
                    blob_sha=candidate["git_blob_sha1"], media_tree=media_tree, observed_at=bundle["run_id"])
                bundle["records"].extend(parsed["records"])
                summary["containers_incomplete"] += int(not parsed["complete"])
                summary["parser_deferred_items"] += len(parsed["deferred"])
                summary["media_refs"] += sum(len(row["media_refs"]) for row in parsed["records"])
                summary["media_refs_deferred"] += sum(len(row["deferred_media"]) for row in parsed["records"])
                # Raw source text stays INSIDE encryption, allowing local parser
                # review without ever putting prompt contents in a checkpoint.
                bundle["containers"].append({"source_id": source["source_id"], "path": path,
                    "raw_utf8": raw.decode("utf-8", errors="strict"), "sha256": sha256(raw),
                    "parse_complete": parsed["complete"], "deferred": parsed["deferred"]})
                if parsed["complete"]:
                    media_refs = {ref["path"]: ref["git_blob_sha1"] for row in parsed["records"] for ref in row["media_refs"]}
                    next_state["entries"][key] = {"source_id": source["source_id"], "repository": repository,
                        "path": path, "git_blob_sha1": candidate["git_blob_sha1"], "repository_commit_sha": commit,
                        "repository_tree_sha": tree_sha, "source_container_sha256": sha256(raw),
                        "media_blob_sha1": media_refs, "artifact_id": None, "last_sealed_at": bundle["run_id"]}
                    summary["containers_completed"] += 1
        except (collector.CollectorError, IntakeError, ValueError, UnicodeError, KeyError):
            # Never include upstream exception text or prompt-bearing responses.
            summary["failed_sources"].append({"source_id": source["source_id"], "reason": "collection_or_parser_validation_failed"})
    summary["records"] = len(bundle["records"])
    summary["status"] = ("partial" if summary["failed_sources"] else
                         "collected_with_deferred_items" if summary["containers_incomplete"] else "collected")
    next_state["content_sha256"] = _digest_checkpoint(next_state)
    validate_checkpoint(next_state, pending=True)
    return bundle, next_state, summary


def _node(args, *, payload=None):
    result = subprocess.run(["node", str(SEALER), *args], input=payload, capture_output=True, timeout=90)
    if result.returncode:
        raise IntakeError("sealed_transport_failed")
    try:
        report = json.loads(result.stdout)
    except (ValueError, UnicodeError) as exc:
        raise IntakeError("sealed_transport_invalid_receipt") from exc
    if report.get("ok") is not True:
        raise IntakeError("sealed_transport_failed")
    return report


def acknowledge_upload(pending_path: Path, sealed_path: Path, artifact_id: str, checkpoint_path: Path):
    if not re.fullmatch(r"[1-9][0-9]*", artifact_id):
        raise IntakeError("successful_upload_artifact_id_required")
    pending = collector.read_json(pending_path)
    if set(pending) != {"checkpoint", "sealed_file_sha256"} or sha256(sealed_path.read_bytes()) != pending["sealed_file_sha256"]:
        raise IntakeError("uploaded_sealed_artifact_binding_mismatch")
    state = validate_checkpoint(pending["checkpoint"], pending=True)
    for entry in state["entries"].values():
        if entry["artifact_id"] is None:
            entry["artifact_id"] = artifact_id
    state["content_sha256"] = _digest_checkpoint(state)
    validate_checkpoint(state)
    collector.write_json_atomic(checkpoint_path, state)
    return {"ok": True, "checkpoint_entries": len(state["entries"]), "artifact_id": artifact_id}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--registry", type=Path, default=collector.DEFAULT_REGISTRY)
    parser.add_argument("--public-key", type=Path, default=PUBLIC_KEY)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--pending-checkpoint", type=Path)
    parser.add_argument("--sealed-output", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--max-containers", type=int, default=MAX_CONTAINERS)
    parser.add_argument("--acknowledge-artifact-id")
    args = parser.parse_args(argv)
    if args.acknowledge_artifact_id:
        if args.fetch or args.collect or not all((args.checkpoint, args.pending_checkpoint, args.sealed_output)):
            parser.error("acknowledgment requires checkpoint, pending checkpoint and sealed artifact only")
        print(json.dumps(acknowledge_upload(args.pending_checkpoint, args.sealed_output, args.acknowledge_artifact_id, args.checkpoint)))
        return 0
    if not args.fetch and not args.collect:
        print(json.dumps({"status": "dry_run", "network_calls": 0, "writes": 0, "max_containers": MAX_CONTAINERS,
                          "public_key_required": True, "plaintext_artifact_forbidden": True}))
        return 0
    if not args.fetch or not args.collect or not all((args.checkpoint, args.pending_checkpoint, args.sealed_output, args.report)):
        parser.error("live collection requires --fetch --collect and explicit checkpoint/sealed/report paths")
    if not 1 <= args.max_containers <= MAX_CONTAINERS:
        parser.error("--max-containers must be 1..20")
    # Key validation happens before even opening a collection API client.
    _node(["verify", "--public-key", str(args.public_key)])
    if args.sealed_output.exists() or args.pending_checkpoint.exists():
        raise IntakeError("immutable_output_exists")
    state = collector.read_json(args.checkpoint) if args.checkpoint.is_file() else empty_checkpoint()
    bundle, pending, summary = collect_bundle(collector.read_json(args.registry), state, max_containers=args.max_containers)
    seal_receipt = _node(["seal", "--public-key", str(args.public_key), "--output", str(args.sealed_output)], payload=encode(bundle))
    sealed_sha = sha256(args.sealed_output.read_bytes())
    collector.write_json_atomic(args.pending_checkpoint, {"checkpoint": pending, "sealed_file_sha256": sealed_sha})
    collector.write_json_atomic(args.report, {**summary, "sealed_file_sha256": sealed_sha, **seal_receipt})
    print(json.dumps({**summary, "sealed_file_sha256": sealed_sha}))
    # Partial successful containers are sealed, but CI must not acknowledge or
    # cache an overall failed run. The next run retries from prior state.
    return 2 if summary["failed_sources"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (IntakeError, OSError, ValueError, subprocess.SubprocessError):
        print('{"status":"blocked","error":"intake_gate_or_transport_failed"}', file=sys.stderr)
        raise SystemExit(2)
