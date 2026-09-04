from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from PIL import Image, ImageOps, UnidentifiedImageError
import imagehash


ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "data" / "canonical" / "archive_records.jsonl"
OUTPUT_ROOT = ROOT / "data" / "private-research" / "remote-media-canary"
CURRENT_DIR = OUTPUT_ROOT / "current"
RUNS_DIR = OUTPUT_ROOT / "runs"
MAX_FETCH = 10
MAX_BYTES = 15 * 1024 * 1024
MAX_PIXELS = 80_000_000
DEFAULT_MIN_FREE_GIB = 5.0
HOST_MIN_INTERVAL_SECONDS = 1.0
MAX_RETRIES = 5
MAX_BACKOFF_SECONDS = 60.0
CHECKPOINT_EVERY = 25
DEFAULT_BULK_CONCURRENCY = 4
MAX_CONCURRENCY = 8
USER_AGENT = "CodexImageArchiveRemoteMediaCanary/1.0 (+internal research review)"
BLOCKED_HOSTS = {"localhost", "127.0.0.1", "::1"}
ALLOWED_IMAGE_MIME_PREFIX = "image/"
AMBIGUOUS_BINARY_MIME_TYPES = frozenset({"application/octet-stream"})
ALLOWED_IMAGE_HOSTS = frozenset(
    {
        "api.star-history.com",
        "cdn.jsdelivr.net",
        "cms-assets.youmind.com",
        "github.com",
        "github-production-user-asset-6210df.s3.amazonaws.com",
        "img.opennana.com",
        "pbs.twimg.com",
        "placehold.co",
        "raw.githubusercontent.com",
        "static.atlascloud.ai",
        "upload.maynor1024.live",
    }
)
RETRYABLE_HTTP_CODES = frozenset({429, 500, 502, 503, 504})


class CanaryError(RuntimeError):
    """Raised when the canary cannot preserve its safety boundary."""


class DiskFloorReached(CanaryError):
    """Raised when the private cache must pause to preserve free disk space."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def redact_url(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def validate_public_http_url(value: str, *, require_allowed_host: bool = True) -> None:
    parsed = urlsplit(value)
    if parsed.scheme.casefold() != "https":
        raise CanaryError("https_required")
    if parsed.username or parsed.password:
        raise CanaryError("credentials_in_url_blocked")
    host = parsed.hostname.casefold() if parsed.hostname else ""
    if not host or host in BLOCKED_HOSTS:
        raise CanaryError("blocked_host")
    if require_allowed_host and host not in ALLOWED_IMAGE_HOSTS:
        raise CanaryError("host_not_allowlisted")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = sorted(
                {
                    ipaddress.ip_address(item[4][0])
                    for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
                },
                key=str,
            )
        except OSError as exc:
            raise CanaryError("host_resolution_failed") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise CanaryError("non_public_address_blocked")


class ValidatingRedirectHandler(HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        validate_public_http_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(delete=False, dir=path.parent, prefix=f".{path.name}.", suffix=".tmp") as handle:
        handle.write(content)
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def public_image_candidate(url: str) -> bool:
    parsed = urlsplit(str(url or ""))
    host = parsed.hostname.casefold() if parsed.hostname else ""
    if parsed.scheme.casefold() != "https" or host not in ALLOWED_IMAGE_HOSTS:
        return False
    if host == "github.com":
        return parsed.path.startswith("/user-attachments/assets/")
    return True


def inspect_inventory() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records: list[dict[str, Any]] = []
    host_counter: Counter[str] = Counter()
    source_counter: Counter[str] = Counter()
    rights_counter: Counter[str] = Counter()
    blocked_public = 0
    with CANONICAL.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            raw = json.loads(line)
            media = raw.get("media") if isinstance(raw.get("media"), dict) else {}
            assets = media.get("assets") if isinstance(media.get("assets"), list) else []
            source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
            rights = raw.get("rights") if isinstance(raw.get("rights"), dict) else {}
            for asset_index, asset in enumerate(assets):
                if not isinstance(asset, dict):
                    continue
                if str(asset.get("uri_kind") or "").casefold() != "remote":
                    continue
                uri = str(asset.get("uri") or "").strip()
                if not uri.startswith(("http://", "https://")):
                    continue
                parsed = urlsplit(uri)
                host = parsed.hostname.casefold() if parsed.hostname else ""
                allowed = public_image_candidate(uri)
                if not allowed:
                    blocked_public += 1
                row = {
                    "catalog_key": raw.get("catalog_key"),
                    "record_id": raw.get("record_id"),
                    "style_id": raw.get("style_id"),
                    "lane": raw.get("lane"),
                    "asset_index": asset_index,
                    "asset_role": asset.get("role"),
                    "source_name": source.get("name"),
                    "source_url": source.get("url"),
                    "rights_status": rights.get("status"),
                    "release_eligible": bool((rights or {}).get("release_eligible")),
                    "url": uri,
                    "host": host,
                    "public_direct_candidate": allowed,
                }
                records.append(row)
                host_counter[host] += 1
                source_counter[str(source.get("name") or "unknown")] += 1
                rights_counter[str(rights.get("status") or "unknown")] += 1
    summary = {
        "schema_version": "remote-media-canary-inventory-1.0",
        "generated_at": utc_now(),
        "record_count": len(records),
        "unique_url_count": len({row["url"] for row in records}),
        "unique_allowed_url_count": len({row["url"] for row in records if row["public_direct_candidate"]}),
        "top_hosts": [{"host": host, "count": count} for host, count in host_counter.most_common(20)],
        "top_sources": [{"source_name": name, "count": count} for name, count in source_counter.most_common(20)],
        "rights_status_counts": dict(sorted(rights_counter.items())),
        "public_direct_candidates": sum(1 for row in records if row["public_direct_candidate"]),
        "blocked_non_direct_candidates": blocked_public,
    }
    return records, summary


def deduplicate_records_by_url(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse exact URLs for network work while preserving every catalog reference."""
    unique: dict[str, dict[str, Any]] = {}
    for row in records:
        url = str(row.get("url") or "")
        if not url:
            continue
        reference = {
            "catalog_key": row.get("catalog_key"),
            "record_id": row.get("record_id"),
            "style_id": row.get("style_id"),
            "asset_index": row.get("asset_index"),
            "asset_role": row.get("asset_role"),
        }
        if url not in unique:
            representative = dict(row)
            representative["references"] = [reference]
            representative["reference_count"] = 1
            unique[url] = representative
            continue
        unique[url]["references"].append(reference)
        unique[url]["reference_count"] = len(unique[url]["references"])
    return list(unique.values())


def select_canary(records: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    records = deduplicate_records_by_url(records)
    chosen: list[dict[str, Any]] = []
    seen_hosts: set[str] = set()
    for row in records:
        if not row["public_direct_candidate"]:
            continue
        host = row["host"]
        if host in seen_hosts:
            continue
        chosen.append(row)
        seen_hosts.add(host)
        if len(chosen) >= limit:
            return chosen
    for row in records:
        if not row["public_direct_candidate"] or row in chosen:
            continue
        chosen.append(row)
        if len(chosen) >= limit:
            break
    return chosen


def select_all(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in deduplicate_records_by_url(records) if row["public_direct_candidate"]]


def safe_blob_suffix(content_type: str, final_url: str, detected_format: str | None = None) -> str:
    detected = str(detected_format or "").casefold()
    detected_suffixes = {
        "avif": ".avif",
        "gif": ".gif",
        "jpeg": ".jpg",
        "png": ".png",
        "webp": ".webp",
    }
    if detected in detected_suffixes:
        return detected_suffixes[detected]
    lowered = content_type.casefold()
    if "png" in lowered:
        return ".png"
    if "webp" in lowered:
        return ".webp"
    if "gif" in lowered:
        return ".gif"
    if "jpeg" in lowered or "jpg" in lowered:
        return ".jpg"
    path = urlsplit(final_url).path.casefold()
    for suffix in (".png", ".webp", ".gif", ".jpg", ".jpeg"):
        if path.endswith(suffix):
            return ".jpg" if suffix == ".jpeg" else suffix
    return ".bin"


def ensure_free_disk(path: Path, *, minimum_free_bytes: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(path).free
    if free_bytes < minimum_free_bytes:
        raise DiskFloorReached(f"minimum_free_disk_not_met:{free_bytes}:{minimum_free_bytes}")


class HostPacer:
    def __init__(
        self,
        *,
        minimum_interval_seconds: float = HOST_MIN_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.minimum_interval_seconds = max(0.0, float(minimum_interval_seconds))
        self.clock = clock
        self.sleeper = sleeper
        self._last_started: dict[str, float] = {}
        self._state_lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}

    def _host_lock(self, host: str) -> threading.Lock:
        with self._state_lock:
            return self._host_locks.setdefault(host, threading.Lock())

    def wait(self, host: str) -> None:
        while True:
            now = self.clock()
            with self._state_lock:
                previous = self._last_started.get(host)
                remaining = (
                    self.minimum_interval_seconds - (now - previous)
                    if previous is not None
                    else 0.0
                )
                if remaining <= 0:
                    self._last_started[host] = now
                    return
            self.sleeper(remaining)

    @contextmanager
    def request_slot(self, host: str):
        """Serialize one host while allowing different hosts to run concurrently."""
        with self._host_lock(host):
            self.wait(host)
            yield


def retry_delay_seconds(exc: HTTPError, *, attempt: int) -> float:
    retry_after = exc.headers.get("Retry-After") if exc.headers is not None else None
    if retry_after:
        try:
            return min(MAX_BACKOFF_SECONDS, max(0.0, float(retry_after)))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
                return min(MAX_BACKOFF_SECONDS, max(0.0, delay))
            except (TypeError, ValueError, OverflowError):
                pass
    return min(MAX_BACKOFF_SECONDS, float(2 ** max(0, attempt - 1)))


def probe_and_fetch(
    row: dict[str, Any],
    *,
    max_bytes: int,
    max_pixels: int = MAX_PIXELS,
    opener: Any | None = None,
) -> dict[str, Any]:
    validate_public_http_url(row["url"])

    request = Request(
        row["url"],
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        },
    )
    active_opener = opener or build_opener(ValidatingRedirectHandler())
    temporary_dir = OUTPUT_ROOT / ".tmp"
    temporary_dir.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with active_opener.open(request, timeout=30) as response:
            final_url = response.geturl()
            validate_public_http_url(final_url)
            content_type = str(response.headers.get_content_type() or "")
            content_length = response.headers.get("Content-Length")
            normalized_content_type = content_type.casefold()
            if (
                not normalized_content_type.startswith(ALLOWED_IMAGE_MIME_PREFIX)
                and normalized_content_type not in AMBIGUOUS_BINARY_MIME_TYPES
            ):
                raise CanaryError(f"content_type_not_image:{content_type}")
            if content_length:
                try:
                    if int(content_length) > max_bytes:
                        raise CanaryError("content_length_exceeds_limit")
                except ValueError as exc:
                    raise CanaryError("invalid_content_length") from exc
            digest = hashlib.sha256()
            byte_size = 0
            with tempfile.NamedTemporaryFile(
                delete=False,
                dir=temporary_dir,
                prefix="remote-media-",
                suffix=".download",
            ) as temporary:
                temporary_path = Path(temporary.name)
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    byte_size += len(chunk)
                    if byte_size > max_bytes:
                        raise CanaryError("download_exceeds_limit")
                    temporary.write(chunk)
                    digest.update(chunk)
                temporary.flush()
                os.fsync(temporary.fileno())

        if temporary_path is None:
            raise CanaryError("temporary_download_missing")
        try:
            with Image.open(temporary_path) as image:
                width, height = image.size
                if width < 1 or height < 1 or width * height > max_pixels:
                    raise CanaryError(f"decoded_pixel_limit_exceeded:{width}x{height}")
                image.load()
                detected_format = str(image.format or "")
                oriented = ImageOps.exif_transpose(image)
                phash = str(imagehash.phash(oriented))
                dhash = str(imagehash.dhash(oriented))
        except (UnidentifiedImageError, OSError) as exc:
            raise CanaryError("pillow_decode_failed") from exc

        blob_sha = digest.hexdigest()
        suffix = safe_blob_suffix(content_type, final_url, detected_format)
        blob_path = OUTPUT_ROOT / "blobs" / blob_sha[:2] / f"{blob_sha}{suffix}"
        blob_path.parent.mkdir(parents=True, exist_ok=True)
        if blob_path.exists() and sha256_file(blob_path) == blob_sha:
            temporary_path.unlink()
            temporary_path = None
        else:
            os.replace(temporary_path, blob_path)
            temporary_path = None
        if sha256_file(blob_path) != blob_sha:
            raise CanaryError("committed_blob_sha256_mismatch")
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()

    return {
        "status": "fetched",
        "requested_url": redact_url(row["url"]),
        "requested_url_sha256": sha256_text(row["url"]),
        "final_url": redact_url(final_url),
        "final_url_sha256": sha256_text(final_url),
        "redirected": redact_url(row["url"]) != redact_url(final_url),
        "content_type": content_type,
        "detected_format": detected_format,
        "byte_size": byte_size,
        "sha256": blob_sha,
        "blob_path": blob_path.relative_to(ROOT).as_posix(),
        "blob_sha256_verified": True,
        "width": width,
        "height": height,
        "pixel_count": width * height,
        "phash": phash,
        "dhash": dhash,
    }


def fetch_with_retries(
    row: dict[str, Any],
    *,
    max_bytes: int,
    max_pixels: int,
    pacer: HostPacer,
    max_retries: int = MAX_RETRIES,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    host = str(row.get("host") or (urlsplit(row["url"]).hostname or "")).casefold()
    for attempt in range(1, max_retries + 1):
        try:
            with pacer.request_slot(host):
                result = probe_and_fetch(row, max_bytes=max_bytes, max_pixels=max_pixels)
            result["attempts"] = attempt
            return result
        except HTTPError as exc:
            if exc.code not in RETRYABLE_HTTP_CODES or attempt >= max_retries:
                raise
            sleeper(retry_delay_seconds(exc, attempt=attempt))
    raise CanaryError("retry_loop_exhausted")


def _row_references(row: dict[str, Any]) -> list[dict[str, Any]]:
    references = row.get("references")
    if isinstance(references, list) and references:
        return [item for item in references if isinstance(item, dict)]
    return [
        {
            "catalog_key": row.get("catalog_key"),
            "record_id": row.get("record_id"),
            "style_id": row.get("style_id"),
            "asset_index": row.get("asset_index"),
            "asset_role": row.get("asset_role"),
        }
    ]


def _blob_result_is_usable(result: dict[str, Any]) -> bool:
    relative = result.get("blob_path")
    expected_sha = str(result.get("sha256") or "")
    if not isinstance(relative, str) or not relative or not expected_sha:
        return False
    try:
        root_resolved = ROOT.resolve()
        candidate = (ROOT / relative).resolve()
        candidate.relative_to(root_resolved)
    except (OSError, ValueError):
        return False
    return candidate.is_file() and sha256_file(candidate) == expected_sha


def _cache_by_url_sha(existing_cache: dict[str, Any]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    items = existing_cache.get("items") if isinstance(existing_cache.get("items"), list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        url_sha = str(item.get("requested_url_sha256") or "")
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        if url_sha and url_sha not in indexed and _blob_result_is_usable(result):
            indexed[url_sha] = result
    return indexed


def _receipt_path(url_sha: str) -> Path:
    return OUTPUT_ROOT / "receipts" / url_sha[:2] / f"{url_sha}.json"


def _read_receipt(url_sha: str) -> dict[str, Any] | None:
    try:
        payload = read_json(_receipt_path(url_sha))
    except (json.JSONDecodeError, OSError):
        return None
    if payload.get("requested_url_sha256") != url_sha:
        return None
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    return result if _blob_result_is_usable(result) else None


def _write_receipt(row: dict[str, Any], fetched: dict[str, Any]) -> None:
    url_sha = sha256_text(row["url"])
    payload = {
        "schema_version": "remote-media-url-receipt-1.0",
        "updated_at": utc_now(),
        "requested_url": redact_url(row["url"]),
        "requested_url_sha256": url_sha,
        "reference_count": len(_row_references(row)),
        "references": _row_references(row),
        "result": fetched,
        "private_cache_only": True,
        "public_promotion": False,
    }
    write_text(_receipt_path(url_sha), stable_json(payload) + "\n")


def _merge_cache_entries(
    merged_cache: dict[tuple[str, int], dict[str, Any]],
    row: dict[str, Any],
    fetched: dict[str, Any],
) -> None:
    for reference in _row_references(row):
        catalog_key = str(reference.get("catalog_key") or "")
        asset_index = int(reference.get("asset_index") or 0)
        merged_cache[(catalog_key, asset_index)] = {
            "catalog_key": reference.get("catalog_key"),
            "record_id": reference.get("record_id"),
            "style_id": reference.get("style_id"),
            "asset_index": asset_index,
            "asset_role": reference.get("asset_role"),
            "host": row.get("host"),
            "requested_url": redact_url(row["url"]),
            "requested_url_sha256": sha256_text(row["url"]),
            "result": fetched,
        }


def _write_cache_index(path: Path, merged_cache: dict[tuple[str, int], dict[str, Any]]) -> None:
    items = sorted(
        merged_cache.values(),
        key=lambda row: (str(row.get("catalog_key") or ""), int(row.get("asset_index") or 0)),
    )
    write_text(
        path,
        stable_json({"schema_version": "remote-media-cache-index-1.0", "items": items}) + "\n",
    )


def _write_checkpoint(
    *,
    total: int,
    processed: int,
    counts: Counter[str],
    run_status: str,
    last_url_sha256: str | None,
    concurrency: int,
) -> None:
    write_text(
        CURRENT_DIR / "download_checkpoint.json",
        stable_json(
            {
                "schema_version": "remote-media-download-checkpoint-1.0",
                "updated_at": utc_now(),
                "run_status": run_status,
                "total_unique_urls": total,
                "processed_unique_urls": processed,
                "remaining_unique_urls": max(0, total - processed),
                "status_counts": dict(sorted(counts.items())),
                "last_url_sha256": last_url_sha256,
                "concurrency": concurrency,
                "private_cache_only": True,
                "public_promotion": False,
            }
        )
        + "\n",
    )


def _selection_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "catalog_key": row.get("catalog_key"),
        "record_id": row.get("record_id"),
        "style_id": row.get("style_id"),
        "lane": row.get("lane"),
        "asset_index": row.get("asset_index"),
        "asset_role": row.get("asset_role"),
        "source_name": row.get("source_name"),
        "source_url": redact_url(str(row.get("source_url") or "")),
        "rights_status": row.get("rights_status"),
        "release_eligible": bool(row.get("release_eligible")),
        "url": redact_url(row["url"]),
        "url_sha256": sha256_text(row["url"]),
        "host": row.get("host"),
        "reference_count": len(_row_references(row)),
    }


def _network_fetch_result(row: dict[str, Any], pacer: HostPacer) -> dict[str, Any]:
    """Worker-only network/decode work; it never writes receipts or indexes."""
    try:
        return fetch_with_retries(
            row,
            max_bytes=MAX_BYTES,
            max_pixels=MAX_PIXELS,
            pacer=pacer,
        )
    except HTTPError as exc:
        return {"status": "http_error", "error": f"http_{exc.code}"}
    except URLError as exc:
        return {"status": "network_error", "error": str(exc.reason)}
    except CanaryError as exc:
        return {"status": "blocked", "error": str(exc)}


def run(
    fetch: bool,
    apply: bool,
    limit: int,
    *,
    all_records: bool = False,
    min_free_gib: float = DEFAULT_MIN_FREE_GIB,
    progress: bool = False,
    concurrency: int | None = None,
) -> dict[str, Any]:
    if fetch and not apply:
        raise CanaryError("fetch_requires_apply")
    if all_records and not (fetch and apply):
        raise CanaryError("all_requires_fetch_and_apply")
    if not all_records and (limit < 1 or limit > MAX_FETCH):
        raise ValueError(f"limit must be between 1 and {MAX_FETCH}")
    if min_free_gib < 0:
        raise ValueError("min_free_gib_must_be_non_negative")
    effective_concurrency = (
        concurrency
        if concurrency is not None
        else (DEFAULT_BULK_CONCURRENCY if all_records else 1)
    )
    if effective_concurrency < 1 or effective_concurrency > MAX_CONCURRENCY:
        raise ValueError(f"concurrency must be between 1 and {MAX_CONCURRENCY}")
    if not all_records and effective_concurrency != 1:
        raise CanaryError("bounded_canary_concurrency_must_be_one")

    records, inventory = inspect_inventory()
    selected = select_all(records) if all_records else select_canary(records, limit=limit)
    result = {
        "schema_version": "remote-media-canary-run-1.0",
        "generated_at": utc_now(),
        "mode": "bulk_private_fetch" if all_records else ("fetch" if fetch else "inventory_only"),
        "run_status": "running" if fetch else "inventory_only",
        "inventory": inventory,
        "selection": {
            "limit": "all" if all_records else limit,
            "selected_count": len(selected),
            "selected_reference_count": sum(len(_row_references(row)) for row in selected),
            "url_level_deduplication": True,
            "concurrency": effective_concurrency,
            "same_host_parallelism": 1,
            "items": [_selection_item(row) for row in selected],
        },
        "fetch_results": [],
    }

    cache_index_path = CURRENT_DIR / "cache_index.json"
    existing_cache = read_json(cache_index_path)
    merged_cache: dict[tuple[str, int], dict[str, Any]] = {}
    for item in existing_cache.get("items", []) if isinstance(existing_cache.get("items"), list) else []:
        if not isinstance(item, dict):
            continue
        key = (str(item.get("catalog_key") or ""), int(item.get("asset_index") or 0))
        merged_cache[key] = item
    cached_by_url = _cache_by_url_sha(existing_cache)

    if apply:
        CURRENT_DIR.mkdir(parents=True, exist_ok=True)
        write_text(CURRENT_DIR / "inventory.json", stable_json(inventory) + "\n")

    run_status = "completed"
    minimum_free_bytes = int(min_free_gib * 1024**3)
    pacer = HostPacer()
    counts: Counter[str] = Counter()
    def commit_result(
        position: int,
        row: dict[str, Any],
        payload: dict[str, Any],
        *,
        was_cached: bool = False,
    ) -> None:
        nonlocal run_status
        url_sha = sha256_text(row["url"])
        outcome = {
            "_selection_position": position,
            "catalog_key": row.get("catalog_key"),
            "style_id": row.get("style_id"),
            "asset_index": row.get("asset_index"),
            "host": row.get("host"),
            "requested_url": redact_url(row["url"]),
            "requested_url_sha256": url_sha,
            "reference_count": len(_row_references(row)),
        }
        if was_cached:
            _merge_cache_entries(merged_cache, row, payload)
            _write_receipt(row, payload)
            outcome.update(payload)
            outcome["status"] = "already_cached"
        elif payload.get("status") == "fetched":
            _write_receipt(row, payload)
            _merge_cache_entries(merged_cache, row, payload)
            cached_by_url[url_sha] = payload
            outcome.update(payload)
        else:
            # Preserve a durable audit record for blocked/error outcomes while
            # keeping them retryable: _read_receipt only reuses verified blobs.
            _write_receipt(row, payload)
            outcome.update(payload)
        if outcome.get("status") == "paused_low_disk":
            run_status = "paused_low_disk"
        result["fetch_results"].append(outcome)
        counts[str(outcome.get("status") or "unknown")] += 1
        processed = len(result["fetch_results"])
        if apply and (processed % CHECKPOINT_EVERY == 0 or run_status == "paused_low_disk"):
            _write_checkpoint(
                total=len(selected),
                processed=processed,
                counts=counts,
                run_status=run_status,
                last_url_sha256=url_sha,
                concurrency=effective_concurrency,
            )
        if progress and (processed % CHECKPOINT_EVERY == 0 or run_status == "paused_low_disk"):
            print(
                f"remote-media {processed}/{len(selected)} "
                f"fetched={counts.get('fetched', 0)} cached={counts.get('already_cached', 0)} "
                f"errors={counts.get('blocked', 0) + counts.get('http_error', 0) + counts.get('network_error', 0) + counts.get('worker_error', 0)}",
                file=sys.stderr,
                flush=True,
            )

    if fetch and not all_records:
        # The bounded canary intentionally remains single-threaded.
        for position, row in enumerate(selected):
            url_sha = sha256_text(row["url"])
            cached = _read_receipt(url_sha) or cached_by_url.get(url_sha)
            if cached is not None:
                commit_result(position, row, cached, was_cached=True)
                continue
            try:
                ensure_free_disk(OUTPUT_ROOT, minimum_free_bytes=minimum_free_bytes)
            except DiskFloorReached as exc:
                commit_result(position, row, {"status": "paused_low_disk", "error": str(exc)})
                break
            commit_result(position, row, _network_fetch_result(row, pacer))

    if fetch and all_records:
        # Cache/index/checkpoint mutations happen only in this main thread. Workers
        # are restricted to network, decode, hashing, and atomic blob creation.
        host_queues: dict[str, deque[tuple[int, dict[str, Any]]]] = {}
        for position, row in enumerate(selected):
            url_sha = sha256_text(row["url"])
            cached = _read_receipt(url_sha) or cached_by_url.get(url_sha)
            if cached is not None:
                commit_result(position, row, cached, was_cached=True)
                continue
            host = str(row.get("host") or "")
            host_queues.setdefault(host, deque()).append((position, row))

        active_hosts: set[str] = set()
        futures: dict[Future[dict[str, Any]], tuple[int, dict[str, Any], str]] = {}
        pause_error: DiskFloorReached | None = None

        def schedule_available(executor: ThreadPoolExecutor) -> None:
            nonlocal pause_error
            if pause_error is not None:
                return
            for host, queue in host_queues.items():
                if len(futures) >= effective_concurrency:
                    return
                if host in active_hosts or not queue:
                    continue
                try:
                    ensure_free_disk(OUTPUT_ROOT, minimum_free_bytes=minimum_free_bytes)
                except DiskFloorReached as exc:
                    pause_error = exc
                    return
                position, row = queue.popleft()
                future = executor.submit(_network_fetch_result, row, pacer)
                futures[future] = (position, row, host)
                active_hosts.add(host)

        with ThreadPoolExecutor(
            max_workers=effective_concurrency,
            thread_name_prefix="remote-media",
        ) as executor:
            while futures or any(host_queues.values()):
                schedule_available(executor)
                if not futures:
                    break
                completed, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
                ordered = sorted(completed, key=lambda future: futures[future][0])
                for future in ordered:
                    position, row, host = futures.pop(future)
                    active_hosts.discard(host)
                    try:
                        payload = future.result()
                    except Exception as exc:  # fail one URL closed without losing the rest of the batch
                        payload = {
                            "status": "worker_error",
                            "error": f"{type(exc).__name__}:worker_failure",
                        }
                    commit_result(position, row, payload)

        if pause_error is not None:
            remaining = [item for queue in host_queues.values() for item in queue]
            if remaining:
                position, row = min(remaining, key=lambda item: item[0])
                commit_result(
                    position,
                    row,
                    {"status": "paused_low_disk", "error": str(pause_error)},
                )

    if result["fetch_results"]:
        result["fetch_results"].sort(key=lambda item: int(item.get("_selection_position") or 0))
        for item in result["fetch_results"]:
            item.pop("_selection_position", None)

    if not fetch:
        counts = Counter()
    if fetch and run_status == "completed" and any(
        counts.get(status, 0) for status in ("blocked", "http_error", "network_error", "worker_error")
    ):
        run_status = "completed_with_errors"
    result["run_status"] = run_status if fetch else "inventory_only"
    result["summary"] = {
        "requested": len(selected),
        "requested_unique_urls": len(selected),
        "requested_references": sum(len(_row_references(row)) for row in selected),
        "downloaded_this_run": counts.get("fetched", 0),
        "completed": counts.get("fetched", 0),
        "already_cached": counts.get("already_cached", 0),
        "complete_unique_urls": counts.get("fetched", 0) + counts.get("already_cached", 0),
        "blocked": counts.get("blocked", 0),
        "network_error": counts.get("network_error", 0),
        "http_error": counts.get("http_error", 0),
        "worker_error": counts.get("worker_error", 0),
        "paused_low_disk": counts.get("paused_low_disk", 0),
        "not_attempted": max(0, len(selected) - len(result["fetch_results"])),
        "skipped": 0 if fetch else len(selected),
        "minimum_free_disk_gib": min_free_gib,
        "concurrency": effective_concurrency,
        "same_host_parallelism": 1,
        "private_cache_only": True,
        "public_promotion": False,
        "redistribution_rights_cleared": False,
    }

    if apply:
        write_text(CURRENT_DIR / "latest_run.json", stable_json(result) + "\n")
        _write_cache_index(cache_index_path, merged_cache)
        if fetch:
            write_text(CURRENT_DIR / "latest_fetch_run.json", stable_json(result) + "\n")
            _write_checkpoint(
                total=len(selected),
                processed=len(result["fetch_results"]),
                counts=counts,
                run_status=result["run_status"],
                last_url_sha256=(
                    str(result["fetch_results"][-1].get("requested_url_sha256") or "")
                    if result["fetch_results"]
                    else None
                ),
                concurrency=effective_concurrency,
            )
        run_dir = RUNS_DIR / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        write_text(run_dir / "run.json", stable_json(result) + "\n")
    return result


def sanitize_existing_artifacts() -> dict[str, Any]:
    changed_files = 0
    changed_values = 0

    def walk(value: Any) -> Any:
        nonlocal changed_values
        if isinstance(value, list):
            return [walk(item) for item in value]
        if not isinstance(value, dict):
            return value
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"requested_url", "final_url", "url", "source_url"} and isinstance(item, str):
                redacted = redact_url(item)
                if redacted != item:
                    changed_values += 1
                cleaned[key] = redacted
                cleaned.setdefault(f"{key}_sha256", sha256_text(item))
            else:
                cleaned[key] = walk(item)
        return cleaned

    for path in sorted(OUTPUT_ROOT.rglob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        cleaned = walk(payload)
        rendered = stable_json(cleaned) + "\n"
        if rendered != path.read_text(encoding="utf-8"):
            write_text(path, rendered)
            changed_files += 1
    return {"changed_files": changed_files, "changed_url_values": changed_values}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch", action="store_true", help="perform a private-cache image fetch")
    parser.add_argument("--apply", action="store_true", help="write private-research outputs")
    parser.add_argument("--limit", type=int, default=5, help=f"maximum canary records to probe (1-{MAX_FETCH})")
    parser.add_argument(
        "--all",
        action="store_true",
        dest="all_records",
        help="fetch every unique allowlisted remote URL; requires --fetch --apply",
    )
    parser.add_argument(
        "--min-free-gib",
        type=float,
        default=DEFAULT_MIN_FREE_GIB,
        help=f"pause before a new download if free disk is below this reserve (default: {DEFAULT_MIN_FREE_GIB:g})",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help=(
            f"bulk workers across distinct hosts (1-{MAX_CONCURRENCY}); "
            f"default {DEFAULT_BULK_CONCURRENCY} with --all and 1 for bounded canary"
        ),
    )
    parser.add_argument("--sanitize-existing", action="store_true", help="remove query strings from existing private run artifacts")
    args = parser.parse_args()
    if args.sanitize_existing:
        if not args.apply:
            raise CanaryError("sanitize_existing_requires_apply")
        print(stable_json({"mode": "sanitize_existing", **sanitize_existing_artifacts()}))
        return 0
    payload = run(
        fetch=args.fetch,
        apply=args.apply,
        limit=args.limit,
        all_records=args.all_records,
        min_free_gib=args.min_free_gib,
        progress=args.all_records and args.fetch,
        concurrency=args.concurrency,
    )
    print(stable_json(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
