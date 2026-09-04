"""Authenticated sealed Actions artifact -> immutable private local intake.

No network or writes by default. --fetch --apply is an explicit authenticated
download/import operation, not image approval, media download or a model call.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[3]
REPOSITORY = "AnnyeongHae/Image_archive"
WORKFLOW = ".github/workflows/github-source-daily-observation.yml"
BRANCH = "main"
ARTIFACT_NAME = "github-source-sealed-intake"
PRIVATE_KEY = "data/private-research/platform-v2/secrets/intake-recipient.private.jwk.json"
IMPORT_ROOT = "data/private-research/platform-v2/actions-imports"
MAX_ZIP_BYTES = 64 * 1024**2
MAX_SEALED_BYTES = 96 * 1024**2
MAX_BUNDLE_BYTES = 32 * 1024**2
MAX_METADATA_BYTES = 2 * 1024**2
SEALED_FIELDS = {"schema_version", "algorithm", "recipient_key_sha256", "plaintext_sha256", "plaintext_bytes",
                 "iv", "wrapped_key", "ciphertext", "ciphertext_sha256"}


class ActionsImportError(RuntimeError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ActionsImportError("duplicate_json_keys")
        result[key] = value
    return result


def read_json_bytes(raw, maximum):
    if len(raw) > maximum:
        raise ActionsImportError("json_size_limit_exceeded")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ActionsImportError("nonfinite_json")))
    except (ValueError, UnicodeError) as exc:
        raise ActionsImportError("invalid_json") from exc


def positive_id(value):
    value = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,19}", value):
        raise ActionsImportError("positive_numeric_id_required")
    return value


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ActionsImportError("invalid_lineage_timestamp") from exc
    if parsed.tzinfo is None:
        raise ActionsImportError("lineage_timezone_required")
    return parsed


def _bounded_command(command, maximum, *, timeout=120):
    """Drain bounded stdout without logging gh errors, credentials or prompts."""
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env={**os.environ, "GH_PROMPT_DISABLED": "1", "GIT_TERMINAL_PROMPT": "0"},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
    executor = ThreadPoolExecutor(max_workers=1)
    def read():
        chunks, total = [], 0
        while True:
            chunk = process.stdout.read(65536)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > maximum:
                raise ActionsImportError("command_output_size_limit_exceeded")
            chunks.append(chunk)
    future = executor.submit(read)
    try:
        output = future.result(timeout=timeout)
        if process.wait(timeout=10) != 0:
            raise ActionsImportError("authenticated_command_failed")
        return output
    except (FutureTimeout, subprocess.TimeoutExpired) as exc:
        raise ActionsImportError("authenticated_command_timeout") from exc
    finally:
        if process.poll() is None:
            process.kill()  # only this helper's newly started subprocess
            process.wait(timeout=10)
        process.stdout.close()
        executor.shutdown(wait=True, cancel_futures=True)


class GhClient:
    def __init__(self):
        self.executable = shutil.which("gh")
        if not self.executable:
            raise ActionsImportError("existing_gh_executable_required_no_install")

    def authenticate(self):
        _bounded_command([self.executable, "auth", "status", "--hostname", "github.com", "--active"], 65536)

    def api(self, endpoint):
        if not endpoint.startswith(f"repos/{REPOSITORY}/") or ".." in endpoint:
            raise ActionsImportError("fixed_github_repository_required")
        raw = _bounded_command([self.executable, "api", "--hostname", "github.com", "--method", "GET", endpoint], MAX_METADATA_BYTES)
        return read_json_bytes(raw, MAX_METADATA_BYTES)

    def download(self, artifact_id):
        endpoint = f"repos/{REPOSITORY}/actions/artifacts/{positive_id(artifact_id)}/zip"
        return _bounded_command([self.executable, "api", "--hostname", "github.com", "--method", "GET", endpoint], MAX_ZIP_BYTES)


def validate_run(run, run_id, *, attempt=None, expected_head_sha=None):
    if not isinstance(run, dict) or str(run.get("id")) != run_id:
        raise ActionsImportError("run_id_mismatch")
    if (not isinstance(run.get("repository"), dict) or not isinstance(run.get("head_repository"), dict)):
        raise ActionsImportError("run_repository_metadata_required")
    if (run.get("repository", {}).get("full_name") != REPOSITORY
            or run.get("head_repository", {}).get("full_name") != REPOSITORY
            or run.get("head_branch") != BRANCH or run.get("path") != WORKFLOW
            or run.get("event") not in {"schedule", "workflow_dispatch", "push"}
            or run.get("status") != "completed" or run.get("conclusion") != "success"):
        raise ActionsImportError("untrusted_or_unsuccessful_workflow_run")
    current_attempt = positive_id(run.get("run_attempt"))
    if attempt is not None and current_attempt != positive_id(attempt):
        raise ActionsImportError("run_attempt_mismatch")
    head = run.get("head_sha")
    if not re.fullmatch(r"[a-f0-9]{40}", str(head)) or (expected_head_sha and head != expected_head_sha):
        raise ActionsImportError("workflow_head_sha_mismatch")
    for key in ("repository", "head_repository"):
        positive_id(run[key].get("id"))
    positive_id(run.get("workflow_id"))
    if timestamp(run.get("run_started_at")) > timestamp(run.get("updated_at")):
        raise ActionsImportError("run_time_order_invalid")
    return current_attempt


def verify_lineage(client, run_id, *, attempt=None, expected_head_sha=None, now=None):
    run_id = positive_id(run_id)
    run = client.api(f"repos/{REPOSITORY}/actions/runs/{run_id}")
    actual_attempt = validate_run(run, run_id, attempt=attempt, expected_head_sha=expected_head_sha)
    # Attempt-specific metadata prevents an old artifact from a previous rerun
    # being accepted merely because the newest attempt is successful.
    attempted = client.api(f"repos/{REPOSITORY}/actions/runs/{run_id}/attempts/{actual_attempt}")
    validate_run(attempted, run_id, attempt=actual_attempt, expected_head_sha=run["head_sha"])
    if (attempted["workflow_id"] != run["workflow_id"] or attempted["repository"]["id"] != run["repository"]["id"]
            or attempted["head_repository"]["id"] != run["head_repository"]["id"]):
        raise ActionsImportError("attempt_metadata_conflicts_with_run")
    workflow = client.api(f"repos/{REPOSITORY}/actions/workflows/{positive_id(run['workflow_id'])}")
    if not isinstance(workflow, dict) or str(workflow.get("id")) != str(run["workflow_id"]) or workflow.get("path") != WORKFLOW:
        raise ActionsImportError("workflow_identity_mismatch")
    candidates, pages = [], []
    for page in range(1, 21):
        listing = client.api(f"repos/{REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=100&page={page}")
        rows = listing.get("artifacts") if isinstance(listing, dict) else None
        if not isinstance(rows, list) or len(rows) > 100 or any(not isinstance(row, dict) for row in rows):
            raise ActionsImportError("artifact_listing_invalid")
        pages.append(listing)
        candidates.extend(row for row in rows if row.get("name") == ARTIFACT_NAME)
        if len(rows) < 100:
            break
    else:
        raise ActionsImportError("artifact_pagination_limit_exceeded")
    if len(candidates) != 1:
        raise ActionsImportError("one_exact_named_artifact_required")
    artifact_id = positive_id(candidates[0].get("id"))
    artifact = client.api(f"repos/{REPOSITORY}/actions/artifacts/{artifact_id}")
    if not isinstance(artifact, dict) or not isinstance(artifact.get("workflow_run"), dict):
        raise ActionsImportError("artifact_workflow_metadata_required")
    origin = artifact.get("workflow_run") or {}
    current = now or datetime.now(timezone.utc)
    if (str(artifact.get("id")) != artifact_id or artifact.get("name") != ARTIFACT_NAME
            or artifact.get("expired") is not False or timestamp(artifact.get("expires_at")) <= current
            or str(origin.get("id")) != run_id or origin.get("head_sha") != run["head_sha"]
            or origin.get("head_branch") != BRANCH
            or origin.get("repository_id") != run["repository"]["id"]
            or origin.get("head_repository_id") != run["head_repository"]["id"]
            or type(artifact.get("size_in_bytes")) is not int or not 1 <= artifact["size_in_bytes"] <= MAX_ZIP_BYTES
            or not timestamp(attempted["run_started_at"]) <= timestamp(artifact.get("created_at")) <= timestamp(attempted["updated_at"])):
        raise ActionsImportError("artifact_origin_expiry_size_or_attempt_mismatch")
    return {"schema_version": "archive-actions-lineage-1", "repository": REPOSITORY, "workflow_path": WORKFLOW,
            "branch": BRANCH, "run_id": run_id, "run_attempt": actual_attempt, "artifact_id": artifact_id,
            "head_sha": run["head_sha"], "metadata_observed_at": current.isoformat(),
            "origin_verified": True, "origin_scope": "authenticated_github_repository_workflow_run_artifact",
            "workflow_commit_explicitly_pinned_by_operator": expected_head_sha is not None,
            "run": run, "attempt": attempted, "workflow": workflow, "artifact": artifact, "artifact_listing_pages": pages,
            "server_artifact_digest": {"reported": artifact.get("digest"), "verified": False,
                "status": "not_verified_zip_byte_scope_not_documentation_confirmed"},
            "source_rights_or_human_approval_inferred": False}


def extract_sealed(zip_bytes):
    if not isinstance(zip_bytes, bytes) or not 1 <= len(zip_bytes) <= MAX_ZIP_BYTES:
        raise ActionsImportError("artifact_zip_size_invalid")
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            entries = archive.infolist()
            if len(entries) != 1:
                raise ActionsImportError("artifact_must_contain_only_one_sealed_file")
            entry = entries[0]
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if (entry.filename != "intake.sealed.json" or entry.is_dir() or mode not in {0, stat.S_IFREG}
                    or entry.flag_bits & 1 or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    or not 1 <= entry.file_size <= MAX_SEALED_BYTES or entry.compress_size > MAX_ZIP_BYTES
                    or entry.file_size > max(1, entry.compress_size) * 10):
                raise ActionsImportError("unsafe_or_oversized_artifact_entry")
            with archive.open(entry) as stream:
                raw = stream.read(MAX_SEALED_BYTES + 1)
            if len(raw) != entry.file_size:
                raise ActionsImportError("sealed_entry_size_mismatch")
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise ActionsImportError("invalid_artifact_zip") from exc
    sealed = read_json_bytes(raw, MAX_SEALED_BYTES)
    if (not isinstance(sealed, dict) or set(sealed) != SEALED_FIELDS
            or sealed.get("schema_version") != "archive-sealed-intake-1"
            or sealed.get("algorithm") != "RSA-OAEP-256+A256GCM"
            or type(sealed.get("plaintext_bytes")) is not int or not 1 <= sealed["plaintext_bytes"] <= MAX_BUNDLE_BYTES
            or not re.fullmatch(r"[a-f0-9]{64}", str(sealed.get("plaintext_sha256")))):
        raise ActionsImportError("sealed_transport_contract_invalid")
    return raw, sealed


def private_path(root, relative, *, must_exist=False):
    root = root.resolve(strict=True)
    candidate = root / relative
    private = root / "data/private-research"
    if candidate.is_absolute() and not candidate.is_relative_to(private):
        raise ActionsImportError("private_descendant_required")
    current = root
    for part in candidate.relative_to(root).parts:
        if part in {"..", "."}:
            raise ActionsImportError("unsafe_private_path")
        current = current / part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ActionsImportError("private_symlink_or_junction_forbidden")
    resolved = candidate.resolve(strict=must_exist)
    if not resolved.is_relative_to(private.resolve()):
        raise ActionsImportError("private_descendant_required")
    return resolved


def immutable_write(root, relative, raw):
    target = private_path(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)  # normal Windows workspace ACL inheritance
    target = private_path(root, relative)
    if target.exists():
        if not target.is_file() or target.read_bytes() != raw:
            raise ActionsImportError("immutable_import_conflict")
        return target
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(target, flags, 0o600 if os.name != "nt" else 0o666)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    return target


def unseal_local(sealed_path, bundle_path, key_path):
    node = shutil.which("node")
    if not node:
        raise ActionsImportError("existing_node_required_no_install")
    output = _bounded_command([node, str(ROOT / "src/github_sources/seal_intake.mjs"), "unseal",
        "--private-key", str(key_path), "--input", str(sealed_path), "--output", str(bundle_path)], 16384)
    report = read_json_bytes(output, 16384)
    if report.get("ok") is not True:
        raise ActionsImportError("local_unseal_failed")
    return report


def planner_for_bundle(bundle):
    spec = importlib.util.spec_from_file_location("archive_v2_intake_planner", ROOT / "platform/v2/local/intake.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_plan(bundle)  # deliberately no media map and no downloads


def validate_bundle(bundle):
    sys.path.insert(0, str(ROOT / "src"))
    from github_sources.intake_envelope import validate_envelope
    if (not isinstance(bundle, dict) or bundle.get("schema_version") != "archive-sealed-intake-bundle-1"
            or bundle.get("canonical_promotion") is not False or bundle.get("public_release") is not False
            or bundle.get("image_binaries_downloaded") != 0 or not isinstance(bundle.get("records"), list)
            or len(bundle["records"]) > 4000 or not isinstance(bundle.get("containers"), list)
            or len(bundle["containers"]) > 20):
        raise ActionsImportError("private_bundle_contract_invalid")
    containers = {}
    for container in bundle["containers"]:
        if not isinstance(container, dict):
            raise ActionsImportError("invalid_source_container")
        raw = container.get("raw_utf8")
        if not isinstance(raw, str) or digest(raw.encode("utf-8")) != container.get("sha256"):
            raise ActionsImportError("source_container_hash_mismatch")
        key = (container.get("source_id"), container.get("path"))
        if key in containers:
            raise ActionsImportError("duplicate_source_container")
        containers[key] = container
    seen = set()
    for record in bundle["records"]:
        validate_envelope(record)
        key = (record["source_id"], record["source_item_id"])
        if key in seen:
            raise ActionsImportError("duplicate_source_item")
        seen.add(key)
        container = containers.get((record["source_id"], record["source_item_id"].split("#", 1)[0]))
        if not container or record.get("source_container_sha256") != container["sha256"]:
            raise ActionsImportError("source_item_container_binding_mismatch")
    return bundle


def import_run(run_id, *, root=ROOT, client=None, attempt=None, expected_head_sha=None, unsealer=None, planner=None):
    run_id = positive_id(run_id)
    root = Path(root).resolve(strict=True)
    key_path = private_path(root, PRIVATE_KEY, must_exist=True)
    if not key_path.is_file() or key_path.stat().st_size > 32768:
        raise ActionsImportError("local_private_key_required")
    client = client or GhClient()
    client.authenticate()
    lineage = verify_lineage(client, run_id, attempt=attempt, expected_head_sha=expected_head_sha)
    zip_bytes = client.download(lineage["artifact_id"])
    sealed_bytes, sealed = extract_sealed(zip_bytes)
    zip_sha, sealed_sha = digest(zip_bytes), digest(sealed_bytes)
    base = f"{IMPORT_ROOT}/{run_id}-{lineage['run_attempt']}-{lineage['artifact_id']}-{zip_sha[:16]}"
    # Raw server metadata and downloaded bytes are retained before decryption.
    # Wall-clock observation varies on retries; preserve the first receipt and
    # compare stable origin IDs/hashes rather than overwriting that observation.
    metadata_path = private_path(root, f"{base}/lineage.json")
    if metadata_path.exists():
        previous = read_json_bytes(metadata_path.read_bytes(), MAX_METADATA_BYTES * 24)
        for field in ("repository", "workflow_path", "branch", "run_id", "run_attempt", "artifact_id", "head_sha"):
            if previous.get(field) != lineage[field]:
                raise ActionsImportError("existing_lineage_conflict")
        lineage = previous
    else:
        immutable_write(root, f"{base}/lineage.json", encode(lineage))
    immutable_write(root, f"{base}/artifact.zip", zip_bytes)
    sealed_path = immutable_write(root, f"{base}/intake.sealed.json", sealed_bytes)
    bundle_path = private_path(root, f"{base}/bundle.json")
    if not bundle_path.exists():
        (unsealer or unseal_local)(sealed_path, bundle_path, key_path)
    bundle_path = private_path(root, f"{base}/bundle.json", must_exist=True)
    if not bundle_path.is_file() or bundle_path.stat().st_size != sealed["plaintext_bytes"]:
        raise ActionsImportError("unsealed_bundle_size_mismatch")
    bundle_bytes = bundle_path.read_bytes()
    if digest(bundle_bytes) != sealed["plaintext_sha256"]:
        raise ActionsImportError("unsealed_bundle_hash_mismatch")
    bundle = validate_bundle(read_json_bytes(bundle_bytes, MAX_BUNDLE_BYTES))
    plan = (planner or planner_for_bundle)(bundle)
    if (not isinstance(plan, dict) or plan.get("provider_calls") != 0
            or plan.get("human_approved") is not False or plan.get("public_eligible") is not False):
        raise ActionsImportError("intake_planner_crossed_approval_boundary")
    plan.update({"origin_verified": True, "origin_note": "Authenticated Actions artifact lineage only; no rights or image approval.",
                 "origin_receipt": f"{base}/lineage.json", "origin_receipt_sha256": digest(encode(lineage))})
    plan_path = immutable_write(root, f"{base}/intake-plan.json", encode(plan))
    receipt = {"schema_version": "archive-actions-import-receipt-1", "status": "imported_private_not_approved",
        "repository": REPOSITORY, "workflow_path": WORKFLOW, "run_id": run_id, "run_attempt": lineage["run_attempt"],
        "artifact_id": lineage["artifact_id"], "head_sha": lineage["head_sha"], "origin_verified": True,
        "server_artifact_digest": lineage["server_artifact_digest"], "zip_sha256_calculated": zip_sha,
        "artifact_size_bytes_reported": lineage["artifact"]["size_in_bytes"], "zip_bytes_calculated": len(zip_bytes),
        "sealed_sha256_calculated": sealed_sha, "bundle_sha256_verified": digest(bundle_bytes),
        "lineage_sha256": digest(encode(lineage)), "plan_sha256": digest(plan_path.read_bytes()),
        "records": len(bundle["records"]), "provider_calls": 0, "media_downloads": 0,
        "image_approved": False, "metadata_human_approved": False, "public_release": False,
        "files": {"zip": f"{base}/artifact.zip", "sealed": f"{base}/intake.sealed.json",
                  "lineage": f"{base}/lineage.json", "bundle": f"{base}/bundle.json", "plan": f"{base}/intake-plan.json"}}
    immutable_write(root, f"{base}/receipt.json", encode(receipt))
    return {"status": receipt["status"], "records": receipt["records"], "origin_verified": True,
            "provider_calls": 0, "media_downloads": 0, "receipt": f"{base}/receipt.json",
            "bundle": f"{base}/bundle.json", "plan": f"{base}/intake-plan.json",
            "server_digest_verified": False, "next_stage": "local_media_acquisition_then_human_image_review"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt")
    parser.add_argument("--expected-head-sha")
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    run_id = positive_id(args.run_id)
    attempt = positive_id(args.run_attempt) if args.run_attempt else None
    if args.expected_head_sha and not re.fullmatch(r"[a-f0-9]{40}", args.expected_head_sha):
        raise ActionsImportError("expected_head_sha_must_be_full_sha1")
    if args.fetch != args.apply:
        raise ActionsImportError("fetch_and_apply_required_together")
    if not args.fetch:
        result = {"status": "dry_run", "repository": REPOSITORY, "workflow": WORKFLOW, "branch": BRANCH,
                  "run_id": run_id, "run_attempt": attempt, "expected_head_sha": args.expected_head_sha,
                  "network_calls": 0, "writes": 0, "provider_calls": 0, "media_downloads": 0}
    else:
        result = import_run(run_id, attempt=attempt, expected_head_sha=args.expected_head_sha)
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ActionsImportError, OSError, ValueError, KeyError, TypeError):
        # gh stdout/stderr, server response bodies and decrypted contents never
        # appear in failure output. Partial private evidence remains preserved.
        print('{"status":"blocked","reason":"actions_lineage_or_import_gate_failed"}', file=sys.stderr)
        raise SystemExit(2)
