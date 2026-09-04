"""Private R2 content-addressed delivery. Dry-run; explicit bounded --apply --execute.

Uses an already installed Wrangler JS entrypoint; installs nothing. Uploads the
verified in-memory bytes via stdin, never a source filename opened a second time.
Existing equal objects are reused; conflicts/failures stop with no automatic retry.
An exclusive fixed-bucket/prefix lock serializes cooperating local executions.
It cannot make get-then-put atomic against unrelated cloud writers; this remains
a single-writer canary, not a conditional-create or cloud-wide immutability claim.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from cloud_snapshot import ROOT, SnapshotError, encoded, private_path, read_plan

BUCKET = "image-prompt-archive-private-staging"
ACCOUNT = "b39fad7b5ebf74e820209ed506fd989b"
MAX_BYTES = 15 * 1048576
MAX_DIAGNOSTIC_BYTES = 64 * 1024
COMMAND_TIMEOUT_SECONDS = 60
PREFIX = "private/v2/sha256/"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def media_rows(plan, limit):
    if type(limit) is not int or not 1 <= limit <= 379:
        raise SnapshotError("media_limit_required_1_to_379")
    rows = plan["manifest"]["media_manifest"]
    if len(rows) != plan["manifest"]["counts"]["items"]:
        raise SnapshotError("media_manifest_count_mismatch")
    return rows[:limit]


def verified_bytes(row, root):
    path = private_path(root / row["prepared_relative_path"], root)
    if not 0 < row["prepared_bytes"] <= MAX_BYTES or row["prepared_mime_type"] != "image/png":
        raise SnapshotError("media_contract_mismatch")
    with path.open("rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if (len(data) != row["prepared_bytes"] or sha(data) != row["prepared_sha256"]
            or not data.startswith(b"\x89PNG\r\n\x1a\n")):
        raise SnapshotError("media_source_hash_mismatch")
    return data


def run_bounded(command, *, cwd, env, data=None, stdout_limit=MAX_BYTES,
                stderr_limit=MAX_DIAGNOSTIC_BYTES, timeout=COMMAND_TIMEOUT_SECONDS):
    """Drain both pipes concurrently under hard byte/time bounds; never echo them.

    stdin has a dedicated writer so a child that stops reading cannot prevent
    timeout/output-limit enforcement. Only the child this function created is
    terminated. No communicate() or unbounded pipe capture is used.
    """
    if data is not None and (not isinstance(data, bytes) or len(data) > MAX_BYTES):
        raise SnapshotError("r2_input_oversized")
    process = None
    threads = []
    failed = threading.Event()
    outputs = {}
    failures = []

    def fail(code):
        failures.append(code)
        failed.set()

    def collect(name, stream, limit):
        chunks = []
        size = 0
        try:
            while True:
                chunk = stream.read(min(65536, limit - size + 1))
                if not chunk:
                    outputs[name] = b"".join(chunks)
                    return
                size += len(chunk)
                if size > limit:
                    fail("r2_stdout_oversized" if name == "stdout" else "r2_stderr_oversized")
                    return
                chunks.append(chunk)
        except (OSError, ValueError):
            fail("r2_cli_transport_failed")
        finally:
            stream.close()

    def send(stream):
        try:
            remaining = memoryview(data)
            while remaining:
                written = stream.write(remaining[:65536])
                if not written:
                    raise OSError("closed stdin")
                remaining = remaining[written:]
        except (OSError, ValueError):
            fail("r2_cli_transport_failed")
        finally:
            stream.close()

    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env, bufsize=0)
        threads = [threading.Thread(target=collect, args=("stdout", process.stdout, stdout_limit), daemon=True),
                   threading.Thread(target=collect, args=("stderr", process.stderr, stderr_limit), daemon=True)]
        if data is not None:
            threads.append(threading.Thread(target=send, args=(process.stdin,), daemon=True))
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + timeout
        while process.poll() is None or any(thread.is_alive() for thread in threads):
            if failed.is_set():
                raise SnapshotError(failures[0])
            if time.monotonic() >= deadline:
                raise SnapshotError("r2_cli_timeout")
            failed.wait(min(0.025, max(0, deadline - time.monotonic())))
        if failures:
            raise SnapshotError(failures[0])
        return subprocess.CompletedProcess(command, process.returncode, outputs["stdout"], outputs["stderr"])
    except OSError:
        raise SnapshotError("r2_cli_transport_failed") from None
    finally:
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                cleanup_deadline = time.monotonic() + 5
                for thread in threads:
                    thread.join(max(0, cleanup_deadline - time.monotonic()))
                if any(thread.is_alive() for thread in threads):
                    raise SnapshotError("r2_cli_cleanup_failed")
            except (OSError, subprocess.TimeoutExpired):
                # Retain the bucket lock if child shutdown cannot be confirmed.
                raise SnapshotError("r2_cli_cleanup_failed") from None


@contextmanager
def bucket_lock(root=ROOT):
    """Cooperating-local-writer exclusion only; a crashed lock needs review."""
    directory = private_path(root / "data/private-research/platform-v2/media-sync", root)
    directory.mkdir(parents=True, exist_ok=True)
    scope = sha(f"{ACCOUNT}/{BUCKET}/{PREFIX}".encode())
    path = private_path(directory / f".bucket-{scope}.lock", root)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise SnapshotError("r2_local_writer_locked") from None
    identity = os.fstat(fd)
    retain = False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded({"pid": os.getpid(), "account": ACCOUNT, "bucket": BUCKET,
                                  "prefix": PREFIX, "scope": "cooperating_local_writers_only"}))
            handle.flush()
            os.fsync(handle.fileno())
        yield
    except SnapshotError as exc:
        retain = str(exc) == "r2_cli_cleanup_failed"
        raise
    finally:
        # Never remove somebody else's replacement lock. On hard process exit
        # or uncertain child cleanup, the remaining lock is an explicit blocker.
        if not retain:
            observed = path.lstat()
            if (observed.st_dev, observed.st_ino) != (identity.st_dev, identity.st_ino):
                raise SnapshotError("r2_local_lock_changed")
            path.unlink()


class Wrangler:
    def __init__(self, entry, workdir):
        self.entry = Path(entry).resolve()
        if not self.entry.is_file() or self.entry.name != "wrangler.js":
            raise SnapshotError("existing_wrangler_js_required")
        self.workdir = workdir
        self.environment = dict(os.environ, CLOUDFLARE_ACCOUNT_ID=ACCOUNT,
            WRANGLER_SEND_METRICS="false", CI="true", NO_COLOR="1",
            WRANGLER_LOG_PATH=str(workdir / "wrangler.log"))

    def run(self, args, data=None):
        return run_bounded(["node", str(self.entry), "r2", "object", *args, "--remote"],
            data=data, cwd=self.workdir, env=self.environment,
            stdout_limit=MAX_BYTES if args[0] == "get" else MAX_DIAGNOSTIC_BYTES)

    def get(self, key):
        result = self.run(["get", f"{BUCKET}/{key}", "--pipe"])
        if result.returncode:
            if b"The specified key does not exist." in result.stderr:
                return None
            raise SnapshotError("r2_get_failed")
        if len(result.stdout) > MAX_BYTES:
            raise SnapshotError("r2_object_oversized")
        return result.stdout

    def put(self, key, data):
        try:
            result = self.run(["put", f"{BUCKET}/{key}", "--pipe", "--content-type", "image/png",
                              "--cache-control", "private, no-store", "--storage-class", "Standard"], data)
        except SnapshotError as exc:
            if str(exc) == "r2_cli_cleanup_failed":
                raise  # Keep local writer exclusion until the child is reviewed.
            raise SnapshotError("r2_put_uncertain_requires_readback") from None
        if result.returncode:
            raise SnapshotError("r2_put_uncertain_requires_readback")


def upload_one(adapter, row, data):
    digest = row["prepared_sha256"]
    key = f"{PREFIX}{digest}.png"
    existing = adapter.get(key)
    if existing is not None:
        if sha(existing) != digest or len(existing) != len(data):
            raise SnapshotError("r2_existing_object_conflict")
        return {"key": key, "status": "reused_verified", "bytes": len(data), "sha256": digest}
    adapter.put(key, data)
    observed = adapter.get(key)
    if observed is None or sha(observed) != digest or len(observed) != len(data):
        raise SnapshotError("r2_upload_readback_mismatch")
    return {"key": key, "status": "uploaded_verified", "bytes": len(data), "sha256": digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--limit", required=True, type=int)
    parser.add_argument("--wrangler-js", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        plan = read_plan(args.plan, ROOT)
        rows = media_rows(plan, args.limit)
        if args.apply != args.execute:
            raise SnapshotError("both_apply_execute_required")
        result = {"status": "dry_run", "manifest_sha256": plan["manifest_sha256"],
            "bucket": BUCKET, "items": len(rows), "bytes": sum(r["prepared_bytes"] for r in rows),
            "public_release": False, "new_embedding_calls": 0,
            "writer_exclusion": "cooperating_local_only_not_cloud_atomic"}
        if args.execute:
            if not args.wrangler_js:
                raise SnapshotError("existing_wrangler_js_required")
            with bucket_lock(ROOT):
                destination = private_path(ROOT / "data/private-research/platform-v2/media-sync" / uuid.uuid4().hex, ROOT)
                destination.mkdir(parents=True, exist_ok=False)
                adapter = Wrangler(args.wrangler_js, destination)
                with (destination / "plan.json").open("xb") as handle:
                    handle.write(encoded(result))
                for index, row in enumerate(rows, 1):
                    data = verified_bytes(row, ROOT)
                    receipt = {"item_id": row["item_id"], **upload_one(adapter, row, data)}
                    with (destination / f"{index:04d}.json").open("xb") as handle:
                        handle.write(encoded(receipt))
                    print(json.dumps({"verified": index, "of": len(rows), "status": receipt["status"]}), flush=True)
                result.update(status="verified_private_media", receipt_dir=str(destination.relative_to(ROOT)),
                              completed_at=datetime.now(timezone.utc).isoformat())
                with (destination / "receipt.json").open("xb") as handle:
                    handle.write(encoded(result))
        print(json.dumps(result))
    except Exception as exc:
        print(json.dumps({"status": "failed", "code": str(exc) if isinstance(exc, SnapshotError) else "media_operation_failed"}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
