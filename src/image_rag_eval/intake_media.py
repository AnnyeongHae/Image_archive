"""Bounded, read-only expansion of declared intake media into review assets.

Bindings select existing archive-local files; they never authorize downloads,
model calls, canonicalization, rights clearance, or public release. Upstream
bundle origin authentication remains the caller's separate responsibility.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct

from PIL import Image, ImageOps

from github_sources.intake_envelope import validate_envelope
from .experiment import prepared_image
from .rights import normalize_image_rights

MAX_MEDIA = 300
MAX_BYTES = 15 * 1024**2
MAX_PIXELS = 80_000_000
PIXEL_POLICY = "rgba-exif-v2"
BINDING_FIELDS = {"source_id", "source_item_id", "media_index", "local_path", "sha256"}
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
SHA1 = re.compile(r"[a-f0-9]{40}\Z")
RASTER_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif", ".bmp", ".tif", ".tiff"}
SECRET_PART = re.compile(r"secret|credential|password|passwd|(?:^|[_.-])tokens?(?:$|[_.-])|^id_(?:rsa|ed25519)", re.I)
WINDOWS_DEVICE = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", re.I)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _identity(info) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _check_node(path: Path, *, directory: bool) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        raise ValueError("local_media_path_unavailable") from error
    # Windows junctions and other reparse points are not necessarily symlinks.
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        raise ValueError("local_media_symlink_or_junction_forbidden")
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError("local_media_must_be_regular_file_with_directory_ancestors")
    return info


def _safe_root(root: Path) -> Path:
    root = Path(root).absolute()
    # Inspect the lexical chain before resolve() can hide a symlink/junction.
    for parent in (*reversed(root.parents), root):
        _check_node(parent, directory=True)
    return root.resolve(strict=True)


def _safe_media_path(root: Path, relative: str) -> tuple[Path, os.stat_result]:
    if (not isinstance(relative, str) or not relative or len(relative) > 4096
            or re.search(r'[\\<>:"|?*\x00-\x1f\x7f]', relative)
            or relative.startswith("/")):
        raise ValueError("unsafe_local_media_path")
    parts = relative.split("/")
    if any(not part or part.startswith(".") or part.endswith((".", " "))
           or SECRET_PART.search(part) or WINDOWS_DEVICE.match(part) for part in parts):
        raise ValueError("unsafe_local_media_path")
    if PurePosixPath(relative).suffix.lower() not in RASTER_SUFFIXES:
        raise ValueError("local_media_must_be_raster_image")
    path = root.joinpath(*parts)
    # Recheck the root chain too: it may have been replaced since _safe_root.
    for parent in reversed(path.parents):
        _check_node(parent, directory=True)
    info = _check_node(path, directory=False)
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root) or resolved != path:
        raise ValueError("local_media_must_stay_inside_archive")
    return path, info


def _read_verified(root: Path, relative: str, expected_sha: str) -> tuple[bytes, tuple]:
    path, before = _safe_media_path(root, relative)
    if not 0 < before.st_size <= MAX_BYTES:
        raise ValueError("local_media_exceeds_15mib_or_is_empty")
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            # CPython/Windows can expose different ctime semantics through
            # path lstat and descriptor fstat. Compare dev/inode/size/mtime
            # across those APIs, and compare ctime only within the same API.
            if not stat.S_ISREG(opened.st_mode) or _identity(opened)[:4] != _identity(before)[:4]:
                raise ValueError("local_media_changed_during_read")
            raw = handle.read(MAX_BYTES + 1)
            if _identity(os.fstat(handle.fileno())) != _identity(opened):
                raise ValueError("local_media_changed_during_read")
    except OSError as error:
        raise ValueError("local_media_read_failed") from error
    _, after = _safe_media_path(root, relative)
    if len(raw) != before.st_size or len(raw) > MAX_BYTES or _identity(after) != _identity(before):
        raise ValueError("local_media_changed_during_read")
    if _digest(raw) != expected_sha:
        raise ValueError("local_media_sha256_mismatch")
    return raw, _identity(before)


def _pixels(raw: bytes) -> dict:
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if (image.width <= 0 or image.height <= 0 or image.width * image.height > MAX_PIXELS
                    or getattr(image, "n_frames", 1) != 1):
                raise ValueError("oversized_or_animated_media_requires_review")
            with ImageOps.exif_transpose(image).convert("RGBA") as pixels:
                # Full original-resolution RGBA, including hidden alpha pixels;
                # never use the resized/composited analysis preview as identity.
                hasher = hashlib.sha256(PIXEL_POLICY.encode("ascii") + b"\0" + struct.pack(">II", *pixels.size))
                hasher.update(pixels.tobytes())
                return {"pixel_sha256": hasher.hexdigest(), "pixel_policy": PIXEL_POLICY,
                        "width": pixels.width, "height": pixels.height}
    except (OSError, Image.DecompressionBombError) as error:
        raise ValueError("invalid_or_oversized_raster_media") from error


def _record_id(record: dict) -> str:
    # Match the existing v2 record-level plan identity; per-image ids below are
    # independent and bind the complete media reference, not just the prompt.
    key = record["source_id"] + ":" + record["source_item_id"]
    version = _digest(json.dumps(record["source_version"], sort_keys=True, ensure_ascii=False).encode())[:16]
    return "intake-" + _digest((key + "\0" + version).encode())[:32]


def prepare_assets(root: Path, bundle: dict, bindings: list[dict]) -> dict:
    """Return in-memory items/previews and explicit subset counts; write nothing.

    ``media_index`` and each output ``asset_index`` are zero based. A binding
    must select one uniquely identified declared reference, not an arbitrary
    file with a matching prompt. Current adapters require a pinned Git blob SHA.
    ``previews`` keys are their prepared-image SHA-256, without path prefixes.
    """
    if (not isinstance(bundle, dict) or bundle.get("schema_version") != "archive-sealed-intake-bundle-1"
            or not isinstance(bundle.get("records"), list) or not 1 <= len(bundle["records"]) <= 4000):
        raise ValueError("invalid_intake_bundle")
    if not isinstance(bindings, list) or not 1 <= len(bindings) <= MAX_MEDIA:
        raise ValueError("select_1_to_300_declared_media")
    records = {}
    total_media = 0
    for record in bundle["records"]:
        validate_envelope(record)
        key = (record["source_id"], record["source_item_id"])
        if key in records:
            raise ValueError("ambiguous_duplicate_intake_record")
        records[key] = record
        total_media += len(record["media_refs"])

    selected = set()
    work = []
    for binding in bindings:
        if not isinstance(binding, dict) or set(binding) != BINDING_FIELDS:
            raise ValueError("invalid_media_binding_fields")
        if (not isinstance(binding["source_id"], str) or not isinstance(binding["source_item_id"], str)
                or type(binding["media_index"]) is not int
                or not isinstance(binding["sha256"], str) or not SHA256.fullmatch(binding["sha256"])):
            raise ValueError("invalid_media_binding_identity")
        key = (binding["source_id"], binding["source_item_id"])
        record = records.get(key)
        index = binding["media_index"]
        if record is None or not 0 <= index < len(record["media_refs"]):
            raise ValueError("binding_must_select_declared_media")
        selection_key = (*key, index)
        if selection_key in selected:
            raise ValueError("duplicate_selected_media_reference")
        selected.add(selection_key)
        media_ref = record["media_refs"][index]
        if not isinstance(media_ref.get("git_blob_sha1"), str) or not SHA1.fullmatch(media_ref["git_blob_sha1"]):
            raise ValueError("declared_media_requires_pinned_git_blob_sha1")
        work.append((binding, record, media_ref))

    root = _safe_root(root)
    items, previews, asset_ids = [], {}, set()
    for binding, record, media_ref in work:
        raw, initial_identity = _read_verified(root, binding["local_path"], binding["sha256"])
        git_sha = hashlib.sha1(f"blob {len(raw)}\0".encode("ascii") + raw).hexdigest()
        if git_sha != media_ref["git_blob_sha1"]:
            raise ValueError("local_media_does_not_match_declared_git_blob")
        signals = _pixels(raw)
        preview = prepared_image(io.BytesIO(raw))
        # Both derivatives use exactly the checked raw bytes; check the source
        # again afterwards to reject same-size replacements and path swaps.
        _, final_identity = _read_verified(root, binding["local_path"], binding["sha256"])
        if final_identity != initial_identity:
            raise ValueError("local_media_changed_during_preparation")
        asset_sha = _digest(_canonical({"source_id": record["source_id"], "source_item_id": record["source_item_id"],
            "source_version": record["source_version"], "media_ref": media_ref}))
        asset_id = "intake-asset-" + asset_sha[:32]
        if asset_id in asset_ids:
            raise ValueError("ambiguous_duplicate_declared_media_identity")
        asset_ids.add(asset_id)
        index = binding["media_index"]
        case = re.search(r"#case-(\d+)\Z", record["source_item_id"], re.I)
        style = "CASE-" + case.group(1).zfill(3) if case else "V2-" + asset_sha[:12]
        if len(record["media_refs"]) > 1:
            style += f"-{index + 1:02d}"
        preview_sha = _digest(preview)
        item = {"id": asset_id, "style_id": style, "path": binding["local_path"], "sha256": binding["sha256"],
            "prepared_sha256": preview_sha, "prepared_path": f"inputs/{preview_sha}.png",
            "prompt": record["original_prompt"]["text"], "signals": signals,
            "source_name": record["source_id"], "source_url": record["source_url"],
            "source_url_sha256": _digest(record["source_url"].encode("utf-8")),
            "source_record": copy.deepcopy(record), "intake_media_ref": copy.deepcopy(media_ref),
            "asset_index": index, "catalog_key": "v2-intake:" + _digest(_canonical({
                "source_id": record["source_id"], "source_item_id": record["source_item_id"]})),
            "record_id": _record_id(record), "lane": "v2_intake", "title": record["title"],
            "review_status": "needs_review", "rights_status": "unknown", "external_ai_approved": False,
            "image_approved": False, "metadata_human_approved": False, "release_eligible": False}
        item["rights_display"] = normalize_image_rights(item)
        items.append(item)
        previews[preview_sha] = preview

    selected_records = {(source_id, item_id) for source_id, item_id, _ in selected}
    selection = {"bundle_records": len(records), "total_declared_media": total_media,
        "selected_media": len(selected), "deferred_media": total_media - len(selected),
        "selected_records": len(selected_records), "unselected_records": len(records) - len(selected_records),
        "records_without_media": sum(not record["media_refs"] for record in records.values()),
        "multi_image_records": sum(len(record["media_refs"]) > 1 for record in records.values()),
        "unselected_record_keys": [{"source_id": key[0], "source_item_id": key[1],
            "declared_media": len(record["media_refs"])} for key, record in records.items() if key not in selected_records]}
    return {"items": items, "previews": previews, "selection": selection}
