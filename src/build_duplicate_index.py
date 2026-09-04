from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageOps, UnidentifiedImageError
import imagehash


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CANONICAL_PATH = PLATFORM_ROOT / "data" / "canonical" / "archive_records.jsonl"
DEFAULT_LEGACY_ROOT = PLATFORM_ROOT / "legacy" / "current_archive"
DEFAULT_OUTPUT_DIR = PLATFORM_ROOT / "data" / "private-research" / "duplicate-analysis" / "current"
DEFAULT_THUMBNAIL_ROOT = PLATFORM_ROOT / "media" / "derived" / "duplicate-review"
DEFAULT_REMOTE_OVERLAY_PATH = (
    PLATFORM_ROOT / "data" / "private-research" / "remote-media-canary" / "current" / "cache_index.json"
)

INDEX_SCHEMA_VERSION = "archive-duplicate-index-1.1"
SUMMARY_SCHEMA_VERSION = "duplicate-analysis-summary-1.0"
ALLOWED_RASTER_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".avif"}
GROUP_KINDS = (
    "exact_prompt_media",
    "exact_media",
    "same_media_variant",
    "exact_prompt",
    "same_prompt_variant",
    "perceptual_candidate",
)
KIND_PRIORITY = {
    "exact_prompt_media": 0,
    "exact_media": 1,
    "same_media_variant": 2,
    "exact_prompt": 3,
    "same_prompt_variant": 4,
    "perceptual_candidate": 5,
}
RIGHTS_CLEAR_STATUSES = {"cleared", "explicitly_cleared", "approved_for_public_release"}
LANE_PRIORITY = {
    "manual": 40,
    "social": 35,
    "legacy": 28,
    "bul001": 24,
    "secret_codes": 20,
    "opennana": 12,
    "external": 8,
}
PROMPT_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
PROMPT_SLOT_RE = re.compile(r"(\[[^\]]+\]|\{[^}]+\}|<[^>]+>)")
PROMPT_NUMBER_RE = re.compile(r"\b\d+(?:[:./-]\d+)*(?:\s?(?:am|pm))?\b", re.IGNORECASE)
PROMPT_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u3400-\u9fff]+", re.IGNORECASE)


class DuplicateBuildError(RuntimeError):
    """Raised when the canary cannot preserve its fail-closed contract."""


@dataclass(frozen=True)
class BuildConfig:
    platform_root: Path = PLATFORM_ROOT
    canonical_path: Path = DEFAULT_CANONICAL_PATH
    legacy_root: Path = DEFAULT_LEGACY_ROOT
    output_dir: Path = DEFAULT_OUTPUT_DIR
    thumbnail_root: Path = DEFAULT_THUMBNAIL_ROOT
    remote_overlay_path: Path = DEFAULT_REMOTE_OVERLAY_PATH
    perceptual_limit: int = 128
    phash_threshold: int = 8
    dhash_threshold: int = 8
    thumbnail_limit: int = 64
    thumbnail_max_px: int = 640
    thumbnail_quality: int = 78

    @property
    def index_path(self) -> Path:
        return self.output_dir / "duplicate_index.sqlite3"

    @property
    def summary_path(self) -> Path:
        return self.output_dir / "summary.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stable_json(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=indent,
        separators=(",", ":") if indent is None else None,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def bounded_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.replace("\x00", " ").split()).strip()
    return text[:limit] if text else None


def public_http_url(value: Any) -> str | None:
    text = bounded_text(value, 2048)
    if text and text.casefold().startswith(("https://", "http://")):
        return text
    return None


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def sha256_file_stable(path: Path) -> tuple[str, int]:
    before = path.stat()
    digest, size = sha256_file(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise DuplicateBuildError("source_changed_during_hash")
    if size != before.st_size:
        raise DuplicateBuildError("source_size_changed_during_hash")
    return digest, size


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def validate_config(config: BuildConfig) -> None:
    if not 0 <= config.perceptual_limit <= 128:
        raise ValueError("perceptual_limit must be between 0 and 128")
    if not 0 <= config.phash_threshold <= 64:
        raise ValueError("phash_threshold must be between 0 and 64")
    if not 0 <= config.dhash_threshold <= 64:
        raise ValueError("dhash_threshold must be between 0 and 64")
    if not 0 <= config.thumbnail_limit <= 64:
        raise ValueError("thumbnail_limit must be between 0 and 64")
    if not 1 <= config.thumbnail_max_px <= 640:
        raise ValueError("thumbnail_max_px must be between 1 and 640")
    if not 1 <= config.thumbnail_quality <= 100:
        raise ValueError("thumbnail_quality must be between 1 and 100")


def load_remote_overlay(config: BuildConfig) -> dict[tuple[str, int], dict[str, Any]]:
    path = config.remote_overlay_path
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = None
    if isinstance(payload, dict):
        if isinstance(payload.get("entries"), list):
            entries = payload.get("entries")
        elif payload.get("schema_version") == "remote-media-cache-index-1.0" and isinstance(payload.get("items"), list):
            entries = []
            for item in payload.get("items", []):
                if not isinstance(item, dict):
                    continue
                result = item.get("result") if isinstance(item.get("result"), dict) else {}
                blob_path = bounded_text(result.get("blob_path"), 2048)
                if not blob_path:
                    continue
                entries.append(
                    {
                        "catalog_key": item.get("catalog_key"),
                        "asset_index": item.get("asset_index"),
                        "local_original_path": blob_path,
                        "requested_url": item.get("requested_url"),
                        "sha256": result.get("sha256"),
                    }
                )
    if not isinstance(entries, list):
        raise DuplicateBuildError("remote_overlay_entries_missing")
    overlay: dict[tuple[str, int], dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        catalog_key = bounded_text(entry.get("catalog_key"), 512)
        asset_index = entry.get("asset_index")
        local_original_path = bounded_text(entry.get("local_original_path"), 2048)
        if catalog_key is None or not isinstance(asset_index, int) or not local_original_path:
            continue
        overlay[(catalog_key, asset_index)] = dict(entry)
    return overlay


def resolve_local_asset(uri: Any, config: BuildConfig) -> Path:
    if not isinstance(uri, str) or not uri.strip():
        raise DuplicateBuildError("local_asset_uri_missing")
    normalized = uri.strip().replace("\\", "/")
    if normalized.casefold().startswith(("http://", "https://", "data:", "file:")):
        raise DuplicateBuildError("local_asset_uri_is_remote")
    if Path(normalized).is_absolute() or normalized.startswith("//"):
        raise DuplicateBuildError("absolute_local_asset_uri_rejected")
    platform_root = config.platform_root.resolve()
    base_roots = [config.legacy_root.resolve(), platform_root]
    for base_root in base_roots:
        candidate = (base_root / normalized).resolve()
        if not candidate.is_relative_to(platform_root):
            continue
        if candidate.suffix.casefold() not in ALLOWED_RASTER_SUFFIXES:
            continue
        if candidate.is_file():
            return candidate
    candidate = (config.legacy_root.resolve() / normalized).resolve()
    if not candidate.is_relative_to(platform_root):
        raise DuplicateBuildError("local_asset_escapes_platform_root")
    if candidate.suffix.casefold() not in ALLOWED_RASTER_SUFFIXES:
        raise DuplicateBuildError("local_asset_suffix_not_supported")
    raise DuplicateBuildError("local_asset_missing")


def record_projection(record: dict[str, Any]) -> dict[str, Any]:
    source = record.get("source") if isinstance(record.get("source"), dict) else {}
    rights = record.get("rights") if isinstance(record.get("rights"), dict) else {}
    review = record.get("review_release") if isinstance(record.get("review_release"), dict) else {}
    return {
        "catalog_key": bounded_text(record.get("catalog_key"), 512) or "",
        "record_id": bounded_text(record.get("record_id"), 512) or "",
        "style_id": bounded_text(record.get("style_id"), 512) or "",
        "lane": bounded_text(record.get("lane"), 64) or "unknown",
        "title": bounded_text(record.get("title"), 320) or "Untitled",
        "source_name": bounded_text(source.get("name"), 240),
        "source_url": public_http_url(source.get("url")),
        "source_type": bounded_text(source.get("type"), 120),
        "source_repository": bounded_text(source.get("repository"), 240),
        "source_commit": bounded_text(source.get("commit"), 80),
        "source_pinned_url": public_http_url(source.get("pinned_url")),
        "rights_status": bounded_text(rights.get("status"), 120),
        "rights_explicitly_cleared": bounded_text(rights.get("status"), 120) in RIGHTS_CLEAR_STATUSES,
        "review_status": bounded_text(review.get("review_status"), 120),
        "release_eligible": bool(rights.get("release_eligible") is True or review.get("release_eligible") is True),
        "prompt_sha256": None,
        "local_asset_count": 0,
        "remote_asset_count": 0,
        "assets": [],
    }


def group_member_entry(record: dict[str, Any], asset: dict[str, Any] | None) -> dict[str, Any]:
    return {"record": record, "asset": asset}


def prompt_template_signature(value: str) -> tuple[str | None, int, int]:
    text = " ".join(value.replace("\x00", " ").split()).strip().casefold()
    if not text:
        return None, 0, 0
    slot_count = len(PROMPT_SLOT_RE.findall(text))
    text = PROMPT_URL_RE.sub(" __url__ ", text)
    text = PROMPT_SLOT_RE.sub(" __slot__ ", text)
    text = PROMPT_NUMBER_RE.sub(" __num__ ", text)
    tokens = PROMPT_TOKEN_RE.findall(text)
    if len(tokens) < 8:
        return None, len(tokens), slot_count
    normalized = []
    previous = None
    for token in tokens:
        token = token.strip("_")
        if not token:
            continue
        if token == previous:
            continue
        previous = token
        normalized.append(token)
    if len(normalized) < 8 or slot_count == 0:
        return None, len(normalized), slot_count
    return " ".join(normalized[:160]), len(normalized), slot_count


def member_signal(record: dict[str, Any], asset: dict[str, Any] | None) -> dict[str, Any]:
    score = 0
    reasons: list[str] = []
    if record.get("rights_explicitly_cleared"):
        score += 400
        reasons.append("rights_cleared")
    if record.get("release_eligible"):
        score += 160
        reasons.append("release_gate_passed")
    review_status = str(record.get("review_status") or "")
    if "human_approved" in review_status or "approved" in review_status:
        score += 140
        reasons.append("human_reviewed")
    if isinstance(asset, dict) and asset.get("sha256"):
        score += 120
        reasons.append("local_asset_hashed")
    elif int(record.get("local_asset_count") or 0) > 0:
        score += 100
        reasons.append("local_asset_declared")
    if record.get("source_commit") or record.get("source_pinned_url"):
        score += 90
        reasons.append("pinned_source")
    if record.get("source_repository"):
        score += 40
        reasons.append("repository_reference")
    lane = str(record.get("lane") or "unknown")
    if lane in LANE_PRIORITY:
        score += LANE_PRIORITY[lane]
        reasons.append(f"lane:{lane}")
    if int(record.get("remote_asset_count") or 0) > 0 and int(record.get("local_asset_count") or 0) == 0:
        score -= 20
        reasons.append("remote_only_media")
    tier = "tier_1" if score >= 500 else "tier_2" if score >= 250 else "tier_3"
    return {
        "member_id": member_id(record, asset),
        "catalog_key": record["catalog_key"],
        "style_id": record["style_id"],
        "score": score,
        "tier": tier,
        "reasons": reasons[:6],
    }


def group_recommendation(members: list[dict[str, Any]]) -> dict[str, Any]:
    ranked = sorted(
        (member_signal(entry["record"], entry.get("asset")) for entry in members),
        key=lambda item: (-int(item["score"]), item["catalog_key"], item["member_id"]),
    )
    primary = ranked[0]
    return {
        "recommended_primary_member_id": primary["member_id"],
        "recommended_primary_catalog_key": primary["catalog_key"],
        "recommended_primary_style_id": primary["style_id"],
        "recommended_primary_tier": primary["tier"],
        "recommended_primary_score": primary["score"],
        "recommended_primary_reasons": primary["reasons"],
        "ranked_members": ranked,
    }


def scan_canonical(config: BuildConfig) -> dict[str, Any]:
    if not config.canonical_path.is_file():
        raise DuplicateBuildError(f"canonical archive missing: {config.canonical_path}")

    records: list[dict[str, Any]] = []
    prompt_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    prompt_template_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    local_assets: list[dict[str, Any]] = []
    prompt_requested = 0
    prompt_completed = 0
    prompt_skipped = 0
    prompt_errors: list[dict[str, str]] = []
    prompt_bytes = 0
    remote_assets = 0
    remote_assets_with_local_overlay = 0
    missing_assets = 0
    canonical_digest = hashlib.sha256()
    remote_overlay = load_remote_overlay(config)

    with config.canonical_path.open("rb") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            canonical_digest.update(raw_line)
            if not raw_line.strip():
                raise DuplicateBuildError(f"blank canonical line: {line_number}")
            try:
                raw = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise DuplicateBuildError(f"invalid canonical JSONL at line {line_number}: {exc}") from exc
            if not isinstance(raw, dict):
                raise DuplicateBuildError(f"canonical object required at line {line_number}")
            record = record_projection(raw)
            if not record["catalog_key"]:
                raise DuplicateBuildError(f"catalog_key missing at line {line_number}")

            prompt_requested += 1
            prompt = raw.get("prompt") if isinstance(raw.get("prompt"), dict) else {}
            prompt_text = prompt.get("text")
            if isinstance(prompt_text, str) and prompt_text:
                encoded = prompt_text.encode("utf-8")
                actual_prompt_sha = hashlib.sha256(encoded).hexdigest()
                prompt_bytes += len(encoded)
                declared_prompt_sha = prompt.get("sha256")
                if declared_prompt_sha and declared_prompt_sha != actual_prompt_sha:
                    prompt_errors.append(
                        {"catalog_key": record["catalog_key"], "code": "declared_prompt_sha256_mismatch"}
                    )
                record["prompt_sha256"] = actual_prompt_sha
                prompt_groups[actual_prompt_sha].append(record)
                signature, token_count, slot_count = prompt_template_signature(prompt_text)
                record["prompt_template_signature"] = signature
                record["prompt_token_count"] = token_count
                record["prompt_slot_count"] = slot_count
                if signature:
                    prompt_template_groups[signature].append(record)
                prompt_completed += 1
            else:
                prompt_skipped += 1

            media = raw.get("media") if isinstance(raw.get("media"), dict) else {}
            assets = media.get("assets") if isinstance(media.get("assets"), list) else []
            if not assets:
                missing_assets += 1
            for asset_index, asset in enumerate(assets):
                if not isinstance(asset, dict):
                    continue
                uri_kind = str(asset.get("uri_kind") or "").casefold()
                uri = asset.get("uri")
                if uri_kind == "remote" or (
                    isinstance(uri, str) and uri.casefold().startswith(("http://", "https://", "data:"))
                ):
                    remote_assets += 1
                    record["remote_asset_count"] += 1
                    overlay_entry = remote_overlay.get((record["catalog_key"], asset_index))
                    if not overlay_entry:
                        continue
                    overlay_path = bounded_text(overlay_entry.get("local_original_path"), 2048)
                    if not overlay_path:
                        continue
                    uri = overlay_path
                    uri_kind = "local"
                    remote_assets_with_local_overlay += 1
                if uri_kind != "local":
                    continue
                asset_id = "asset-" + sha256_text(f"{record['catalog_key']}\0{asset_index}\0{uri}")[:32]
                local = {
                    "asset_id": asset_id,
                    "catalog_key": record["catalog_key"],
                    "asset_index": asset_index,
                    "declared_sha256": bounded_text(asset.get("sha256"), 64),
                    "path": None,
                    "sha256": None,
                    "declared_sha256_match": None,
                    "byte_size": None,
                    "width": asset.get("width") if isinstance(asset.get("width"), int) else None,
                    "height": asset.get("height") if isinstance(asset.get("height"), int) else None,
                    "mime_type": bounded_text(asset.get("mime_type"), 120),
                    "phash": None,
                    "dhash": None,
                    "thumbnail_uri": None,
                    "record": record,
                    "uri": uri,
                    "downloaded_from_remote": bool(
                        isinstance(asset.get("uri"), str)
                        and asset.get("uri") != uri
                    ),
                    "remote_original_uri": public_http_url(asset.get("uri")),
                }
                record["assets"].append(local)
                record["local_asset_count"] += 1
                local_assets.append(local)
            records.append(record)

    canonical_sha = canonical_digest.hexdigest()
    return {
        "records": records,
        "prompt_groups": prompt_groups,
        "prompt_template_groups": prompt_template_groups,
        "local_assets": local_assets,
        "canonical_sha256": canonical_sha,
        "prompt_stats": {
            "requested": prompt_requested,
            "completed": prompt_completed,
            "skipped": prompt_skipped,
            "errors": len(prompt_errors),
            "bytes_hashed": prompt_bytes,
            "error_samples": prompt_errors[:20],
        },
        "remote_assets": remote_assets,
        "remote_assets_with_local_overlay": remote_assets_with_local_overlay,
        "records_without_assets": missing_assets,
    }


def hash_local_assets(scan: dict[str, Any], config: BuildConfig) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    errors: list[dict[str, str]] = []
    completed = 0
    bytes_hashed = 0
    declared_matches = 0
    declared_missing = 0
    declared_mismatches = 0

    for asset in scan["local_assets"]:
        try:
            path = resolve_local_asset(asset["uri"], config)
            digest, size = sha256_file_stable(path)
            asset["path"] = path
            asset["sha256"] = digest
            asset["byte_size"] = size
            declared = asset["declared_sha256"]
            if declared:
                matches = declared == digest
                asset["declared_sha256_match"] = matches
                if matches:
                    declared_matches += 1
                else:
                    declared_mismatches += 1
                    errors.append(
                        {"catalog_key": asset["catalog_key"], "code": "declared_media_sha256_mismatch"}
                    )
            else:
                declared_missing += 1
            groups[digest].append(asset)
            completed += 1
            bytes_hashed += size
        except (OSError, DuplicateBuildError) as exc:
            errors.append(
                {
                    "catalog_key": asset["catalog_key"],
                    "code": str(exc)[:160] or exc.__class__.__name__,
                }
            )

    return {
        "groups": groups,
        "stats": {
            "requested": len(scan["local_assets"]),
            "completed": completed,
            "skipped": len(scan["local_assets"]) - completed,
            "errors": len(errors),
            "bytes_hashed": bytes_hashed,
            "declared_sha256_matches": declared_matches,
            "declared_sha256_missing": declared_missing,
            "declared_sha256_mismatches": declared_mismatches,
            "remote_assets_indexed_from_local_overlay": scan["remote_assets_with_local_overlay"],
            "remote_assets_skipped": max(
                0,
                scan["remote_assets"] - scan["remote_assets_with_local_overlay"],
            ),
            "error_samples": errors[:20],
        },
    }


def normalized_image(path: Path) -> tuple[Image.Image, str | None]:
    with Image.open(path) as source:
        source_format = source.format
        image = ImageOps.exif_transpose(source)
        image.load()
        if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            image = Image.alpha_composite(background, rgba).convert("RGB")
        else:
            image = image.convert("RGB")
        return image, source_format


def fingerprint_asset(asset: dict[str, Any]) -> tuple[str, str, int, int]:
    path = asset.get("path")
    if not isinstance(path, Path):
        raise DuplicateBuildError("fingerprint_source_unavailable")
    image, _ = normalized_image(path)
    width, height = image.size
    phash = str(imagehash.phash(image, hash_size=8, highfreq_factor=4))
    dhash = str(imagehash.dhash(image, hash_size=8))
    return phash, dhash, width, height


def hamming_hex(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


def perceptual_canary(
    scan: dict[str, Any],
    exact_media_groups: dict[str, list[dict[str, Any]]],
    config: BuildConfig,
) -> dict[str, Any]:
    successful_assets = [asset for asset in scan["local_assets"] if asset.get("sha256") and asset.get("path")]
    exact_members = {
        asset["asset_id"]
        for members in exact_media_groups.values()
        if len(members) > 1
        for asset in members
    }
    exact_first = sorted(
        (asset for asset in successful_assets if asset["asset_id"] in exact_members),
        key=lambda item: (item["catalog_key"], item["asset_id"]),
    )
    remaining = sorted(
        (asset for asset in successful_assets if asset["asset_id"] not in exact_members),
        key=lambda item: sha256_text(
            f"{item['record']['lane']}\0{item['catalog_key']}\0{item['asset_id']}\0{item['sha256']}"
        ),
    )
    selected = (exact_first + remaining)[: config.perceptual_limit]
    errors: list[dict[str, str]] = []
    completed: list[dict[str, Any]] = []
    bytes_decoded = 0

    for asset in selected:
        try:
            phash, dhash, width, height = fingerprint_asset(asset)
            asset["phash"] = phash
            asset["dhash"] = dhash
            asset["width"] = width
            asset["height"] = height
            bytes_decoded += int(asset.get("byte_size") or 0)
            completed.append(asset)
        except (OSError, ValueError, UnidentifiedImageError, DuplicateBuildError) as exc:
            errors.append(
                {
                    "catalog_key": asset["catalog_key"],
                    "code": exc.__class__.__name__,
                }
            )

    pairs: list[dict[str, Any]] = []
    for left_index, left in enumerate(completed):
        for right in completed[left_index + 1 :]:
            if left["sha256"] == right["sha256"]:
                continue
            phash_distance = hamming_hex(left["phash"], right["phash"])
            dhash_distance = hamming_hex(left["dhash"], right["dhash"])
            if phash_distance > config.phash_threshold or dhash_distance > config.dhash_threshold:
                continue
            pair_key = "\0".join(sorted((left["asset_id"], right["asset_id"])))
            pair_digest = sha256_text(
                f"{pair_key}\0p{config.phash_threshold}\0d{config.dhash_threshold}\0imagehash-4.3"
            )
            pairs.append(
                {
                    "group_id": f"perceptual-{pair_digest}",
                    "left": left,
                    "right": right,
                    "phash_distance": phash_distance,
                    "dhash_distance": dhash_distance,
                    "similarity_score": round(1.0 - ((phash_distance + dhash_distance) / 128.0), 6),
                }
            )
    pairs.sort(key=lambda item: (item["phash_distance"] + item["dhash_distance"], item["group_id"]))
    return {
        "selected": selected,
        "completed_assets": completed,
        "pairs": pairs,
        "stats": {
            "requested": len(selected),
            "completed": len(completed),
            "skipped": len(selected) - len(completed),
            "errors": len(errors),
            "bytes_decoded": bytes_decoded,
            "candidate_pairs": len(pairs),
            "exact_group_members_selected_first": len(exact_first[: config.perceptual_limit]),
            "phash_threshold": config.phash_threshold,
            "dhash_threshold": config.dhash_threshold,
            "error_samples": errors[:20],
        },
    }


def member_id(record: dict[str, Any], asset: dict[str, Any] | None) -> str:
    if asset is not None:
        return asset["asset_id"]
    return "record-" + sha256_text(record["catalog_key"])[:32]


def grouped_prompt_media_pairs(scan: dict[str, Any]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in scan["records"]:
        prompt_sha = record.get("prompt_sha256")
        if not isinstance(prompt_sha, str) or not prompt_sha:
            continue
        for asset in record.get("assets", []):
            media_sha = asset.get("sha256")
            if not isinstance(media_sha, str) or not media_sha:
                continue
            grouped[(prompt_sha, media_sha)].append(group_member_entry(record, asset))
    return grouped


def build_group_specs(
    scan: dict[str, Any],
    media_hash: dict[str, Any],
    perceptual: dict[str, Any],
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    prompt_media_pairs = grouped_prompt_media_pairs(scan)
    for prompt_digest, media_digest in sorted(prompt_media_pairs):
        members = prompt_media_pairs[(prompt_digest, media_digest)]
        if len(members) < 2:
            continue
        specs.append(
            {
                "group_id": f"exact-prompt-media-{sha256_text(prompt_digest + media_digest)[:24]}",
                "kind": "exact_prompt_media",
                "exact_sha256": prompt_digest,
                "media_sha256": media_digest,
                "phash_distance": 0,
                "dhash_distance": 0,
                "similarity_score": 1.0,
                "display_title": f"Exact prompt + media | {len(members)} records",
                "members": members,
                "recommendation": group_recommendation(members),
            }
        )
    for digest, records in sorted(scan["prompt_groups"].items()):
        if len(records) < 2:
            continue
        members = [
            group_member_entry(record, record["assets"][0] if record["assets"] else None)
            for record in sorted(records, key=lambda item: item["catalog_key"])
        ]
        specs.append(
            {
                "group_id": f"exact-prompt-{digest}",
                "kind": "exact_prompt",
                "exact_sha256": digest,
                "media_sha256": None,
                "phash_distance": None,
                "dhash_distance": None,
                "similarity_score": 1.0,
                "display_title": f"Exact prompt | {len(members)} records",
                "members": members,
                "recommendation": group_recommendation(members),
            }
        )
    for signature, records in sorted(scan["prompt_template_groups"].items()):
        unique_prompts = {record.get("prompt_sha256") for record in records if record.get("prompt_sha256")}
        if len(records) < 2 or len(unique_prompts) < 2:
            continue
        members = [
            group_member_entry(record, record["assets"][0] if record["assets"] else None)
            for record in sorted(records, key=lambda item: item["catalog_key"])
        ]
        specs.append(
            {
                "group_id": f"same-prompt-variant-{sha256_text(signature)[:24]}",
                "kind": "same_prompt_variant",
                "exact_sha256": None,
                "media_sha256": None,
                "phash_distance": None,
                "dhash_distance": None,
                "similarity_score": round(min(0.999999, 0.72 + min(len(unique_prompts), 8) * 0.03), 6),
                "display_title": f"Prompt scaffold family | {len(members)} records",
                "members": members,
                "recommendation": group_recommendation(members),
                "prompt_template_signature": signature,
            }
        )
    for digest, assets in sorted(media_hash["groups"].items()):
        if len(assets) < 2:
            continue
        ordered = sorted(assets, key=lambda item: (item["catalog_key"], item["asset_id"]))
        members = [group_member_entry(asset["record"], asset) for asset in ordered]
        specs.append(
            {
                "group_id": f"exact-media-{digest}",
                "kind": "exact_media",
                "exact_sha256": None,
                "media_sha256": digest,
                "phash_distance": 0,
                "dhash_distance": 0,
                "similarity_score": 1.0,
                "display_title": f"Exact media | {len(ordered)} records",
                "members": members,
                "recommendation": group_recommendation(members),
            }
        )
        distinct_prompts = sorted(
            {str(asset["record"]["prompt_sha256"]) for asset in ordered if asset["record"].get("prompt_sha256")}
        )
        unresolved_prompts = sum(1 for asset in ordered if not asset["record"].get("prompt_sha256"))
        if len(distinct_prompts) >= 2 or unresolved_prompts > 0:
            specs.append(
                {
                    "group_id": f"same-media-variant-{digest}",
                    "kind": "same_media_variant",
                    "exact_sha256": None,
                    "media_sha256": digest,
                    "phash_distance": 0,
                    "dhash_distance": 0,
                    "similarity_score": round(
                        min(0.999999, 0.76 + min(len(distinct_prompts) + unresolved_prompts, 8) * 0.02),
                        6,
                    ),
                    "display_title": f"Same image, prompt variants | {len(ordered)} records",
                    "members": members,
                    "recommendation": group_recommendation(members),
                }
            )
    for pair in perceptual["pairs"]:
        left = pair["left"]
        right = pair["right"]
        title = bounded_text(
            f"Perceptual candidate · {left['record']['title']} ↔ {right['record']['title']}",
            320,
        )
        specs.append(
            {
                "group_id": pair["group_id"],
                "kind": "perceptual_candidate",
                "exact_sha256": None,
                "media_sha256": None,
                "phash_distance": pair["phash_distance"],
                "dhash_distance": pair["dhash_distance"],
                "similarity_score": pair["similarity_score"],
                "display_title": (title or "Perceptual candidate").replace(" · ", " | "),
                "members": [
                    group_member_entry(left["record"], left),
                    group_member_entry(right["record"], right),
                ],
                "recommendation": group_recommendation(
                    [
                        group_member_entry(left["record"], left),
                        group_member_entry(right["record"], right),
                    ]
                ),
            }
        )
    specs.sort(key=lambda item: (KIND_PRIORITY[item["kind"]], item["group_id"]))
    return specs


def encode_thumbnail(asset: dict[str, Any], config: BuildConfig) -> tuple[bytes, int, int]:
    path = asset.get("path")
    if not isinstance(path, Path):
        raise DuplicateBuildError("thumbnail_source_unavailable")
    image, _ = normalized_image(path)
    source_width, source_height = image.size
    image.thumbnail((config.thumbnail_max_px, config.thumbnail_max_px), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(
        output,
        format="WEBP",
        quality=config.thumbnail_quality,
        method=6,
        optimize=True,
    )
    return output.getvalue(), source_width, source_height


def generate_thumbnails(
    groups: list[dict[str, Any]],
    config: BuildConfig,
    *,
    apply: bool,
) -> dict[str, Any]:
    if config.thumbnail_limit == 0:
        return {
            "requested": 0,
            "completed": 0,
            "skipped": 0,
            "errors": 0,
            "source_bytes": 0,
            "output_bytes": 0,
            "logical_output_bytes": 0,
            "bytes_saved": 0,
            "unique_files": 0,
            "files_written": 0,
            "max_dimension_px": config.thumbnail_max_px,
            "quality": config.thumbnail_quality,
            "error_samples": [],
        }
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in sorted(groups, key=lambda item: (KIND_PRIORITY[item["kind"]], item["group_id"])):
        for entry in group["members"]:
            asset = entry.get("asset")
            if not isinstance(asset, dict) or not asset.get("path") or asset["asset_id"] in seen:
                continue
            seen.add(asset["asset_id"])
            candidates.append(asset)
            if len(candidates) == config.thumbnail_limit:
                break
        if len(candidates) == config.thumbnail_limit:
            break

    completed = 0
    skipped_not_smaller = 0
    errors: list[dict[str, str]] = []
    source_bytes = 0
    logical_output_bytes = 0
    unique_outputs: dict[str, int] = {}
    active_filenames: set[str] = set()
    written = 0
    cleaned = 0
    for asset in candidates:
        source_size = int(asset.get("byte_size") or 0)
        try:
            data, width, height = encode_thumbnail(asset, config)
            if len(data) >= source_size:
                skipped_not_smaller += 1
                continue
            digest = hashlib.sha256(data).hexdigest()
            filename = f"{digest}.webp"
            active_filenames.add(filename)
            asset["thumbnail_uri"] = f"/media/derived/duplicate-review/{filename}"
            source_bytes += source_size
            logical_output_bytes += len(data)
            unique_outputs.setdefault(digest, len(data))
            completed += 1
            if apply:
                target = config.thumbnail_root / filename
                if target.exists():
                    existing_digest, existing_size = sha256_file(target)
                    if existing_digest != digest or existing_size != len(data):
                        raise DuplicateBuildError("content_addressed_thumbnail_collision")
                else:
                    atomic_write_bytes(target, data)
                    written += 1
            if asset.get("width") is None:
                asset["width"] = width
            if asset.get("height") is None:
                asset["height"] = height
        except (OSError, ValueError, UnidentifiedImageError, DuplicateBuildError) as exc:
            errors.append(
                {"catalog_key": asset["catalog_key"], "code": exc.__class__.__name__}
            )
    if apply:
        for existing in config.thumbnail_root.glob("*.webp"):
            if existing.name in active_filenames:
                continue
            try:
                existing.relative_to(config.thumbnail_root)
            except ValueError as exc:
                raise DuplicateBuildError("thumbnail_cleanup_outside_root") from exc
            existing.unlink()
            cleaned += 1
    return {
        "requested": len(candidates),
        "completed": completed,
        "skipped": skipped_not_smaller,
        "errors": len(errors),
        "source_bytes": source_bytes,
        "output_bytes": sum(unique_outputs.values()),
        "logical_output_bytes": logical_output_bytes,
        "bytes_saved": max(0, source_bytes - logical_output_bytes),
        "unique_files": len(unique_outputs),
        "files_written": written,
        "files_cleaned": cleaned,
        "max_dimension_px": config.thumbnail_max_px,
        "quality": config.thumbnail_quality,
        "error_samples": errors[:20],
    }


def safe_group_rows(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for group in groups:
        lanes = sorted({entry["record"]["lane"] for entry in group["members"]})
        sources = sorted(
            {entry["record"]["source_name"] for entry in group["members"] if entry["record"]["source_name"]},
            key=str.casefold,
        )
        recommendation = group_recommendation(group["members"])
        thumbnails = sorted(
            {
                entry["asset"]["thumbnail_uri"]
                for entry in group["members"]
                if entry.get("asset") and entry["asset"].get("thumbnail_uri")
            }
        )[:4]
        search_values: list[str] = [group["group_id"], group["display_title"], *lanes, *sources]
        for entry in group["members"]:
            search_values.extend(
                [entry["record"]["catalog_key"], entry["record"]["title"], entry["record"]["style_id"]]
            )
        search_text = " ".join(value for value in search_values if value).casefold()
        rows.append(
            {
                "group_id": group["group_id"],
                "kind": group["kind"],
                "member_count": len(group["members"]),
                "display_title": group["display_title"],
                "exact_sha256": group["exact_sha256"],
                "phash_distance": group["phash_distance"],
                "dhash_distance": group["dhash_distance"],
                "similarity_score": group["similarity_score"],
                "lanes_json": stable_json(lanes),
                "sources_json": stable_json(sources),
                "thumbnail_uris_json": stable_json(thumbnails),
                "recommendation_json": stable_json(recommendation),
                "search_text": search_text,
                "members": group["members"],
            }
        )
    return rows


def package_versions() -> dict[str, str]:
    try:
        import importlib.metadata as metadata

        return {
            "Pillow": metadata.version("Pillow"),
            "ImageHash": metadata.version("ImageHash"),
            "numpy": metadata.version("numpy"),
        }
    except Exception:
        return {"Pillow": "unknown", "ImageHash": "unknown", "numpy": "unknown"}


def build_summary(
    scan: dict[str, Any],
    media_hash: dict[str, Any],
    perceptual: dict[str, Any],
    group_rows: list[dict[str, Any]],
    thumbnails: dict[str, Any],
    config: BuildConfig,
) -> dict[str, Any]:
    group_counts = Counter(row["kind"] for row in group_rows)
    analysis_basis = {
        "canonical_sha256": scan["canonical_sha256"],
        "perceptual_limit": config.perceptual_limit,
        "phash_threshold": config.phash_threshold,
        "dhash_threshold": config.dhash_threshold,
        "thumbnail_limit": config.thumbnail_limit,
        "thumbnail_max_px": config.thumbnail_max_px,
        "thumbnail_quality": config.thumbnail_quality,
        "versions": package_versions(),
    }
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "index_schema_version": INDEX_SCHEMA_VERSION,
        "analysis_id": "duplicate-analysis-" + sha256_text(stable_json(analysis_basis))[:24],
        "generated_at": utc_now(),
        "canonical": {
            "record_count": len(scan["records"]),
            "sha256": scan["canonical_sha256"],
            "records_without_assets": scan["records_without_assets"],
            "remote_assets_seen": scan["remote_assets"],
            "remote_assets_with_local_overlay": scan["remote_assets_with_local_overlay"],
        },
        "counts": {
            "records_indexed": len(scan["records"]),
            "local_assets_indexed": media_hash["stats"]["completed"],
            "cached_remote_assets_indexed": scan["remote_assets_with_local_overlay"],
            "groups_total": len(group_rows),
            "groups_by_kind": {kind: group_counts.get(kind, 0) for kind in GROUP_KINDS},
            "group_memberships": sum(row["member_count"] for row in group_rows),
        },
        "analysis": {
            "exact_prompt": scan["prompt_stats"],
            "exact_media": media_hash["stats"],
            "perceptual": perceptual["stats"],
            "thumbnails": thumbnails,
            "policy": {
                "source_records_immutable": True,
                "remote_media_fetched": scan["remote_assets_with_local_overlay"] > 0,
                "perceptual_matches_candidate_only": True,
                "automatic_merge_or_delete": False,
                "full_prompt_bodies_stored": False,
                "filesystem_paths_stored": False,
                "base64_stored": False,
                "default_model_lane": "none",
                "deterministic_first": True,
                "luna_lane": "metadata repair and prompt-family naming only",
                "terra_lane": "ambiguous visual-family adjudication",
                "sol_lane": "rights-sensitive or high-risk final review only",
            },
            "algorithms": {
                "prompt_exact": "sha256_utf8_prompt_text",
                "media_exact": "sha256_streamed_binary",
                "phash": "ImageHash.phash(hash_size=8,highfreq_factor=4)",
                "dhash": "ImageHash.dhash(hash_size=8)",
                "orientation": "Pillow.ImageOps.exif_transpose",
                "alpha_background": "opaque_white",
                "primary_recommendation": "deterministic_signal_scoring",
            },
            "package_versions": package_versions(),
        },
        "artifacts": {
            "sqlite": {"bytes": None, "sha256": None},
            "summary_json": {"bytes": None},
            "thumbnails": {
                "count": thumbnails["unique_files"],
                "bytes": thumbnails["output_bytes"],
                "bytes_saved": thumbnails["bytes_saved"],
            },
        },
    }


def create_sqlite(
    path: Path,
    scan: dict[str, Any],
    media_hash: dict[str, Any],
    group_rows: list[dict[str, Any]],
    summary: dict[str, Any],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as handle:
        temporary = Path(handle.name)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(temporary)
        connection.executescript(
            """
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL
            );
            CREATE TABLE records (
                catalog_key TEXT PRIMARY KEY,
                record_id TEXT NOT NULL,
                style_id TEXT NOT NULL,
                lane TEXT NOT NULL,
                title TEXT NOT NULL,
                source_name TEXT,
                source_url TEXT,
                rights_status TEXT,
                review_status TEXT,
                prompt_sha256 TEXT,
                local_asset_count INTEGER NOT NULL,
                remote_asset_count INTEGER NOT NULL
            );
            CREATE TABLE assets (
                asset_id TEXT PRIMARY KEY,
                catalog_key TEXT NOT NULL REFERENCES records(catalog_key),
                asset_index INTEGER NOT NULL,
                declared_sha256 TEXT,
                asset_sha256 TEXT NOT NULL,
                declared_sha256_match INTEGER,
                phash TEXT,
                dhash TEXT,
                width INTEGER,
                height INTEGER,
                byte_size INTEGER NOT NULL,
                mime_type TEXT,
                thumbnail_uri TEXT,
                UNIQUE(catalog_key, asset_index)
            );
            CREATE TABLE groups (
                group_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK(kind IN ('exact_prompt_media','exact_media','same_media_variant','exact_prompt','same_prompt_variant','perceptual_candidate')),
                member_count INTEGER NOT NULL,
                display_title TEXT NOT NULL,
                exact_sha256 TEXT,
                phash_distance INTEGER,
                dhash_distance INTEGER,
                similarity_score REAL,
                lanes_json TEXT NOT NULL,
                sources_json TEXT NOT NULL,
                thumbnail_uris_json TEXT NOT NULL,
                recommendation_json TEXT NOT NULL,
                search_text TEXT NOT NULL
            );
            CREATE TABLE group_members (
                group_id TEXT NOT NULL REFERENCES groups(group_id),
                ordinal INTEGER NOT NULL,
                member_id TEXT NOT NULL,
                catalog_key TEXT NOT NULL REFERENCES records(catalog_key),
                asset_id TEXT REFERENCES assets(asset_id),
                PRIMARY KEY(group_id, ordinal),
                UNIQUE(group_id, member_id)
            );
            CREATE INDEX idx_groups_kind_size ON groups(kind, member_count DESC, group_id);
            CREATE INDEX idx_groups_similarity ON groups(similarity_score DESC, group_id);
            CREATE INDEX idx_group_members_catalog ON group_members(catalog_key);
            CREATE INDEX idx_group_members_asset ON group_members(asset_id);
            CREATE INDEX idx_assets_sha256 ON assets(asset_sha256);
            CREATE INDEX idx_records_prompt_sha256 ON records(prompt_sha256);
            """
        )
        connection.execute(
            "INSERT INTO meta(key,value_json) VALUES(?,?)",
            ("index_schema_version", stable_json(INDEX_SCHEMA_VERSION)),
        )
        connection.execute(
            "INSERT INTO meta(key,value_json) VALUES(?,?)",
            ("public_summary", stable_json(summary)),
        )
        record_sql = (
            "INSERT INTO records(catalog_key,record_id,style_id,lane,title,source_name,source_url,"
            "rights_status,review_status,prompt_sha256,local_asset_count,remote_asset_count) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        for record in scan["records"]:
            connection.execute(
                record_sql,
                (
                    record["catalog_key"],
                    record["record_id"],
                    record["style_id"],
                    record["lane"],
                    record["title"],
                    record["source_name"],
                    record["source_url"],
                    record["rights_status"],
                    record["review_status"],
                    record["prompt_sha256"],
                    record["local_asset_count"],
                    record["remote_asset_count"],
                ),
            )
        asset_sql = (
            "INSERT INTO assets(asset_id,catalog_key,asset_index,declared_sha256,asset_sha256,"
            "declared_sha256_match,phash,dhash,width,height,byte_size,mime_type,thumbnail_uri) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        for asset in scan["local_assets"]:
            if not asset.get("sha256"):
                continue
            match = asset.get("declared_sha256_match")
            connection.execute(
                asset_sql,
                (
                    asset["asset_id"],
                    asset["catalog_key"],
                    asset["asset_index"],
                    asset["declared_sha256"],
                    asset["sha256"],
                    None if match is None else int(bool(match)),
                    asset["phash"],
                    asset["dhash"],
                    asset["width"],
                    asset["height"],
                    asset["byte_size"],
                    asset["mime_type"],
                    asset["thumbnail_uri"],
                ),
            )
        group_sql = (
            "INSERT INTO groups(group_id,kind,member_count,display_title,exact_sha256,phash_distance,"
            "dhash_distance,similarity_score,lanes_json,sources_json,thumbnail_uris_json,recommendation_json,search_text) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        for group in group_rows:
            connection.execute(
                group_sql,
                (
                    group["group_id"],
                    group["kind"],
                    group["member_count"],
                    group["display_title"],
                    group["exact_sha256"],
                    group["phash_distance"],
                    group["dhash_distance"],
                    group["similarity_score"],
                    group["lanes_json"],
                    group["sources_json"],
                    group["thumbnail_uris_json"],
                    group["recommendation_json"],
                    group["search_text"],
                ),
            )
            ordered = sorted(
                group["members"],
                key=lambda entry: (
                    entry["record"]["catalog_key"],
                    entry["asset"]["asset_id"] if entry.get("asset") else "",
                ),
            )
            for ordinal, entry in enumerate(ordered):
                asset = entry.get("asset")
                connection.execute(
                    "INSERT INTO group_members(group_id,ordinal,member_id,catalog_key,asset_id) VALUES(?,?,?,?,?)",
                    (
                        group["group_id"],
                        ordinal,
                        member_id(entry["record"], asset),
                        entry["record"]["catalog_key"],
                        asset["asset_id"] if asset else None,
                    ),
                )
        connection.commit()
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        if quick_check != "ok":
            raise DuplicateBuildError(f"duplicate SQLite quick_check failed: {quick_check}")
        connection.close()
        connection = None
        os.replace(temporary, path)
    finally:
        if connection is not None:
            connection.close()
        if temporary.exists():
            temporary.unlink()


def build_duplicate_index(config: BuildConfig, *, apply: bool = False) -> dict[str, Any]:
    validate_config(config)
    scan = scan_canonical(config)
    media_hash = hash_local_assets(scan, config)
    perceptual = perceptual_canary(scan, media_hash["groups"], config)
    group_specs = build_group_specs(scan, media_hash, perceptual)
    thumbnail_stats = generate_thumbnails(group_specs, config, apply=apply)
    group_rows = safe_group_rows(group_specs)
    summary = build_summary(scan, media_hash, perceptual, group_rows, thumbnail_stats, config)

    canonical_after, canonical_bytes = sha256_file(config.canonical_path)
    if canonical_after != scan["canonical_sha256"]:
        raise DuplicateBuildError("canonical archive changed during duplicate analysis")
    summary["canonical"]["bytes"] = canonical_bytes

    if apply:
        create_sqlite(config.index_path, scan, media_hash, group_rows, summary)
        sqlite_sha, sqlite_bytes = sha256_file(config.index_path)
        summary["artifacts"]["sqlite"] = {"bytes": sqlite_bytes, "sha256": sqlite_sha}
        summary_bytes = b""
        for _ in range(4):
            summary_bytes = (stable_json(summary, indent=2) + "\n").encode("utf-8")
            byte_count = len(summary_bytes)
            if summary["artifacts"]["summary_json"]["bytes"] == byte_count:
                break
            summary["artifacts"]["summary_json"]["bytes"] = byte_count
        summary_bytes = (stable_json(summary, indent=2) + "\n").encode("utf-8")
        atomic_write_bytes(config.summary_path, summary_bytes)

    return {"mode": "apply" if apply else "dry_run", "writes": apply, **summary}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build an immutable-source duplicate index and bounded image-fingerprint canary."
    )
    parser.add_argument("--canonical", type=Path, default=DEFAULT_CANONICAL_PATH)
    parser.add_argument("--legacy-root", type=Path, default=DEFAULT_LEGACY_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--thumbnail-root", type=Path, default=DEFAULT_THUMBNAIL_ROOT)
    parser.add_argument("--perceptual-limit", type=int, default=128)
    parser.add_argument("--phash-threshold", type=int, default=8)
    parser.add_argument("--dhash-threshold", type=int, default=8)
    parser.add_argument("--thumbnail-limit", type=int, default=64)
    parser.add_argument("--thumbnail-max-px", type=int, default=640)
    parser.add_argument("--thumbnail-quality", type=int, default=78)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    canonical = args.canonical.resolve()
    platform_root = canonical.parents[2] if len(canonical.parents) >= 3 else PLATFORM_ROOT
    config = BuildConfig(
        platform_root=platform_root,
        canonical_path=canonical,
        legacy_root=args.legacy_root.resolve(),
        output_dir=args.output_dir.resolve(),
        thumbnail_root=args.thumbnail_root.resolve(),
        perceptual_limit=args.perceptual_limit,
        phash_threshold=args.phash_threshold,
        dhash_threshold=args.dhash_threshold,
        thumbnail_limit=args.thumbnail_limit,
        thumbnail_max_px=args.thumbnail_max_px,
        thumbnail_quality=args.thumbnail_quality,
    )
    result = build_duplicate_index(config, apply=args.apply)
    print(stable_json(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
