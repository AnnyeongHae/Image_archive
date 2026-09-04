from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_PATH = PLATFORM_ROOT / "data" / "canonical" / "archive_records.jsonl"
MANIFEST_PATH = PLATFORM_ROOT / "data" / "canonical" / "archive_records_manifest.json"
PUBLIC_ROOT = PLATFORM_ROOT / "data" / "public-export"
PUBLIC_INDEX_PATH = PUBLIC_ROOT / "catalog-index.json"
OPENNANA_ARCHIVE_PATH = PLATFORM_ROOT / "data" / "private-research" / "opennana" / "archive" / "opennana_records.json"
GENERATED_PREVIEW_PATH = PLATFORM_ROOT / "legacy" / "current_archive" / "generated_preview_assets.json"

BASE_TOTAL = 18_793
BASE_LANE_COUNTS = {
    "legacy": 529,
    "external": 18_092,
    "social": 3,
    "manual": 13,
    "secret_codes": 131,
    "bul001": 25,
}
REQUIRED_CANONICAL_FIELDS = {
    "schema_version",
    "catalog_key",
    "lane",
    "record_id",
    "style_id",
    "parent_style_id",
    "title",
    "prompt",
    "source",
    "license",
    "rights",
    "media",
    "taxonomy",
    "generation",
    "review_release",
    "search_text",
    "content_sha256",
    "provenance",
}
PRIVATE_PATH_MARKERS = (
    "private-research",
    "private_research",
    "reference/_derived",
)
PUBLIC_RIGHTS_TIERS = {"P1", "P2"}


class ValidationFailure(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationFailure(message)


def stable_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def object_sha256(value: dict[str, Any], excluded_key: str = "content_sha256") -> str:
    payload = {key: item for key, item in value.items() if key != excluded_key}
    return hashlib.sha256(stable_json_bytes(payload)).hexdigest()


def file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            byte_count += len(chunk)
            digest.update(chunk)
    return byte_count, digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    require(isinstance(value, dict), f"Expected JSON object: {path}")
    return value


def expected_archive_shape() -> tuple[int, dict[str, int], int]:
    opennana_count = 0
    if OPENNANA_ARCHIVE_PATH.is_file():
        payload = load_json(OPENNANA_ARCHIVE_PATH)
        require(
            payload.get("schema_version") == "opennana-internal-archive-1.0",
            "Unsupported OpenNana internal archive schema",
        )
        records = payload.get("records")
        require(isinstance(records, list), "OpenNana internal archive records must be a list")
        require(payload.get("record_count") == len(records), "OpenNana internal archive count drifted")
        require(payload.get("public_release_eligible") is False, "OpenNana archive release boundary failed")
        opennana_count = len(records)
    lane_counts = dict(BASE_LANE_COUNTS)
    if opennana_count:
        lane_counts["opennana"] = opennana_count
    return BASE_TOTAL + opennana_count, lane_counts, opennana_count


def generated_preview_asset_ids() -> set[str]:
    require(GENERATED_PREVIEW_PATH.is_file(), f"Missing generated preview inventory: {GENERATED_PREVIEW_PATH}")
    payload = load_json(GENERATED_PREVIEW_PATH)
    require(
        payload.get("schema_version") == "generated-preview-assets-1.0",
        "Unsupported generated preview inventory schema",
    )
    assets = payload.get("assets")
    require(isinstance(assets, list), "Generated preview assets must be a list")
    require(payload.get("asset_count") == len(assets), "Generated preview inventory count drifted")
    asset_ids: set[str] = set()
    for index, asset in enumerate(assets):
        require(isinstance(asset, dict), f"Generated preview asset {index} is not an object")
        asset_id = str(asset.get("asset_id") or "").strip()
        require(bool(asset_id), f"Generated preview asset {index} has no asset_id")
        require(asset_id not in asset_ids, f"Duplicate generated preview asset_id: {asset_id}")
        asset_ids.add(asset_id)
    return asset_ids


def recursive_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from recursive_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from recursive_strings(item)


def contains_private_path(value: Any) -> str | None:
    for text in recursive_strings(value):
        normalized = text.replace("\\", "/").casefold()
        for marker in PRIVATE_PATH_MARKERS:
            if marker in normalized:
                return text
    return None


def require_rights_policy(rights: dict[str, Any], catalog_key: str) -> None:
    tier = rights.get("rights_tier")
    visibility = rights.get("portfolio_visibility")
    admin_usage = rights.get("admin_usage_status")
    public_metadata = rights.get("public_metadata_eligible")
    require(tier in {"P1", "P2", "P3", "P4"}, f"Invalid rights tier: {catalog_key}")
    if tier == "P1":
        require(visibility == "public", f"P1 visibility drift: {catalog_key}")
        require(admin_usage == "public_or_metadata", f"P1 usage drift: {catalog_key}")
        require(public_metadata is True, f"P1 public metadata gate failed: {catalog_key}")
        require(rights.get("release_eligible") is True, f"P1 release gate failed: {catalog_key}")
        require(rights.get("explicitly_cleared") is True, f"P1 rights gate failed: {catalog_key}")
    elif tier == "P2":
        require(visibility == "metadata_link_only", f"P2 visibility drift: {catalog_key}")
        require(admin_usage == "public_or_metadata", f"P2 usage drift: {catalog_key}")
        require(public_metadata is True, f"P2 public metadata gate failed: {catalog_key}")
        require(rights.get("release_eligible") is True, f"P2 release gate failed: {catalog_key}")
        require(rights.get("prompt_publication_eligible") is False, f"P2 prompt leaked: {catalog_key}")
        require(rights.get("media_publication_eligible") is False, f"P2 media leaked: {catalog_key}")
    elif tier == "P3":
        require(visibility == "admin_only", f"P3 visibility drift: {catalog_key}")
        require(admin_usage == "reference_allowed", f"P3 usage drift: {catalog_key}")
        require(public_metadata is False, f"P3 public metadata leaked: {catalog_key}")
        require(rights.get("prompt_publication_eligible") is False, f"P3 prompt leaked: {catalog_key}")
        require(rights.get("media_publication_eligible") is False, f"P3 media leaked: {catalog_key}")
    else:
        require(visibility == "admin_only", f"P4 visibility drift: {catalog_key}")
        require(admin_usage == "quarantine_only", f"P4 usage drift: {catalog_key}")
        require(public_metadata is False, f"P4 public metadata leaked: {catalog_key}")
        require(rights.get("prompt_publication_eligible") is False, f"P4 prompt leaked: {catalog_key}")
        require(rights.get("media_publication_eligible") is False, f"P4 media leaked: {catalog_key}")


def canonical_summary() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    expected_total, expected_lane_counts, opennana_count = expected_archive_shape()
    require(CANONICAL_PATH.is_file(), f"Missing canonical JSONL: {CANONICAL_PATH}")
    catalog: dict[str, dict[str, Any]] = {}
    style_ids: set[str] = set()
    record_ids: set[str] = set()
    lane_counts: Counter[str] = Counter()
    generated_overlay_asset_ids: set[str] = set()
    line_count = 0

    with CANONICAL_PATH.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            require(bool(line.strip()), f"Blank JSONL line at {line_number}")
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValidationFailure(f"Invalid JSONL at line {line_number}: {exc}") from exc
            require(isinstance(record, dict), f"Canonical line {line_number} is not an object")
            missing = REQUIRED_CANONICAL_FIELDS - set(record)
            require(not missing, f"Canonical line {line_number} missing fields: {sorted(missing)}")
            expected_hash = object_sha256(record)
            require(
                record.get("content_sha256") == expected_hash,
                f"Canonical content hash mismatch at line {line_number} ({record.get('catalog_key')})",
            )
            catalog_key = str(record.get("catalog_key") or "")
            lane = str(record.get("lane") or "")
            style_id = str(record.get("style_id") or "")
            record_id = str(record.get("record_id") or "")
            require(catalog_key.startswith(f"{lane}:"), f"Catalog key is not lane-scoped: {catalog_key}")
            require(catalog_key not in catalog, f"Duplicate catalog key: {catalog_key}")
            require(style_id not in style_ids, f"Duplicate Style ID: {style_id}")
            require(record_id not in record_ids, f"Duplicate record ID: {record_id}")
            require(
                bool((record.get("provenance") or {}).get("raw_source")),
                f"Missing raw source lineage: {catalog_key}",
            )
            require(
                record.get("review_release", {}).get("release_eligible")
                == record.get("rights", {}).get("release_eligible"),
                f"Release eligibility disagrees between canonical sections: {catalog_key}",
            )
            require_rights_policy(record.get("rights") or {}, catalog_key)
            if lane == "opennana":
                require(style_id == f"ONN-{record_id.removeprefix('OPENNANA-')}", f"OpenNana Style ID drift: {style_id}")
                require(record_id.startswith("OPENNANA-"), f"OpenNana record ID drift: {record_id}")
                rights = record.get("rights") or {}
                require(rights.get("release_eligible") is False, f"OpenNana row became release eligible: {catalog_key}")
                require(rights.get("prompt_publication_eligible") is False, f"OpenNana prompt became public: {catalog_key}")
                require(rights.get("media_publication_eligible") is False, f"OpenNana media became public: {catalog_key}")
                require(rights.get("commercial_reuse_claimed") is False, f"OpenNana commercial reuse was claimed: {catalog_key}")
                require(bool((record.get("prompt") or {}).get("text")), f"Private OpenNana prompt missing: {catalog_key}")
            generated_overlay = (record.get("generation") or {}).get("generated_preview_overlay")
            if isinstance(generated_overlay, dict) and generated_overlay.get("asset_id"):
                asset_id = str(generated_overlay["asset_id"])
                require(
                    asset_id not in generated_overlay_asset_ids,
                    f"Generated preview asset mapped more than once: {asset_id}",
                )
                generated_overlay_asset_ids.add(asset_id)
            catalog[catalog_key] = {
                "content_sha256": record["content_sha256"],
                "style_id": style_id,
                "record_id": record_id,
                "source": record.get("source") or {},
                "license": record.get("license") or {},
                "rights": record.get("rights") or {},
                "review_release": record.get("review_release") or {},
                "prompt": record.get("prompt") or {},
                "media_assets": record.get("media", {}).get("assets") or [],
            }
            style_ids.add(style_id)
            record_ids.add(record_id)
            lane_counts[lane] += 1
            line_count += 1

    require(line_count == expected_total, f"Canonical count {line_count} != {expected_total}")
    require(dict(lane_counts) == expected_lane_counts, f"Canonical lane counts drifted: {dict(lane_counts)}")
    require(len(catalog) == expected_total, "Canonical catalog-key count drifted")
    require(len(style_ids) == expected_total, "Canonical Style ID count drifted")
    require(len(record_ids) == expected_total, "Canonical record ID count drifted")
    size, digest = file_digest(CANONICAL_PATH)
    return catalog, {
        "line_count": line_count,
        "catalog_key_count": len(catalog),
        "style_id_count": len(style_ids),
        "record_id_count": len(record_ids),
        "lane_counts": dict(sorted(lane_counts.items())),
        "bytes": size,
        "sha256": digest,
        "expected_total": expected_total,
        "expected_lane_counts": expected_lane_counts,
        "opennana_record_count": opennana_count,
        "generated_overlay_asset_ids": generated_overlay_asset_ids,
    }


def validate_manifest(canonical_stats: dict[str, Any]) -> dict[str, Any]:
    require(MANIFEST_PATH.is_file(), f"Missing canonical manifest: {MANIFEST_PATH}")
    manifest = load_json(MANIFEST_PATH)
    require(
        manifest.get("content_sha256") == object_sha256(manifest),
        "Canonical manifest content hash mismatch",
    )
    expected_total = canonical_stats["expected_total"]
    require(manifest.get("record_count") == expected_total, "Manifest record count drifted")
    require(manifest.get("catalog_key_count") == expected_total, "Manifest catalog key count drifted")
    require(manifest.get("record_id_count") == expected_total, "Manifest record ID count drifted")
    require(manifest.get("style_id_count") == expected_total, "Manifest Style ID count drifted")
    require(manifest.get("lane_counts") == canonical_stats["lane_counts"], "Manifest lane counts drifted")
    jsonl = manifest.get("canonical_jsonl") or {}
    require(jsonl.get("line_count") == canonical_stats["line_count"], "Manifest JSONL line count mismatch")
    require(jsonl.get("bytes") == canonical_stats["bytes"], "Manifest JSONL byte count mismatch")
    require(jsonl.get("sha256") == canonical_stats["sha256"], "Manifest JSONL file hash mismatch")

    input_sources = manifest.get("input_sources")
    expected_input_count = 8 + int(OPENNANA_ARCHIVE_PATH.is_file())
    require(
        isinstance(input_sources, list) and len(input_sources) == expected_input_count,
        "Manifest input source evidence missing",
    )
    for evidence in input_sources:
        relative = str(evidence.get("path") or "")
        path = PLATFORM_ROOT / relative
        require(path.is_file(), f"Manifest input source missing: {relative}")
        size, digest = file_digest(path)
        require(size == evidence.get("bytes"), f"Input byte count mismatch: {relative}")
        require(digest == evidence.get("sha256"), f"Input SHA-256 mismatch: {relative}")
    overlay = manifest.get("generated_preview_overlay") or {}
    source_asset_count = int(overlay.get("source_asset_count") or 0)
    matched_asset_count = int(overlay.get("matched_asset_count") or 0)
    source_asset_ids = generated_preview_asset_ids()
    matched_asset_ids = canonical_stats["generated_overlay_asset_ids"]
    require(
        source_asset_count == len(source_asset_ids),
        "Generated overlay source count does not match current preview inventory",
    )
    require(
        matched_asset_count == len(matched_asset_ids),
        "Generated overlay matched count does not match canonical records",
    )
    require(
        matched_asset_ids == source_asset_ids,
        "Generated preview inventory is not exactly represented in canonical overlays",
    )
    return manifest


def validate_public_record(
    record: dict[str, Any], canonical: dict[str, dict[str, Any]], shard_id: str
) -> tuple[str, int, int]:
    require(isinstance(record, dict), f"Non-object record in {shard_id}")
    catalog_key = str(record.get("catalog_key") or "")
    require(catalog_key in canonical, f"Public record absent from canonical catalog: {catalog_key}")
    source_record = canonical[catalog_key]
    require(
        record.get("canonical_content_sha256") == source_record["content_sha256"],
        f"Canonical hash pointer mismatch: {catalog_key}",
    )
    require(
        record.get("content_sha256") == object_sha256(record),
        f"Public record content hash mismatch: {catalog_key}",
    )
    require(record.get("source") == source_record["source"], f"Public source metadata drift: {catalog_key}")
    public_rights = record.get("rights") or {}
    canonical_rights = source_record["rights"]
    require(
        canonical_rights.get("rights_tier") in PUBLIC_RIGHTS_TIERS
        and canonical_rights.get("public_metadata_eligible") is True,
        f"Admin-only record entered public export: {catalog_key}",
    )
    for key in (
        "status",
        "rights_tier",
        "portfolio_visibility",
        "admin_usage_status",
        "public_metadata_eligible",
        "release_eligible",
        "explicitly_cleared",
        "prompt_publication_eligible",
        "media_publication_eligible",
        "commercial_reuse_claimed",
        "requires_human_review",
        "license_is_not_item_rights_clearance",
    ):
        require(
            public_rights.get(key) == canonical_rights.get(key),
            f"Public rights metadata drift ({key}): {catalog_key}",
        )

    private_value = contains_private_path(record)
    require(private_value is None, f"Private path leaked into public record {catalog_key}: {private_value}")
    serialized_keys = set(record)
    require("provenance" not in serialized_keys and "raw_source" not in serialized_keys, f"Raw lineage leaked: {catalog_key}")

    prompt = record.get("prompt") or {}
    prompt_allowed = bool(canonical_rights.get("prompt_publication_eligible"))
    prompt_text = prompt.get("text")
    require(
        bool(prompt.get("available_in_public_export")) == prompt_allowed,
        f"Public prompt availability metadata disagrees with rights gate: {catalog_key}",
    )
    if prompt_allowed:
        require(bool(prompt_text), f"Rights-cleared public prompt was not included: {catalog_key}")
        require(prompt.get("text_included") is True, f"Prompt inclusion flag missing: {catalog_key}")
        require(
            prompt_text == source_record["prompt"].get("text"),
            f"Public prompt differs from canonical prompt: {catalog_key}",
        )
    else:
        require(not prompt_text, f"Rights-uncleared full prompt leaked: {catalog_key}")
        require(prompt.get("text_included") is False, f"Prompt exclusion flag missing: {catalog_key}")
        canonical_prompt_text = str(source_record["prompt"].get("text") or "")
        if canonical_prompt_text:
            normalized_prompt = " ".join(canonical_prompt_text.split())
            normalized_title = " ".join(str(record.get("title") or "").split())
            require(
                normalized_title != normalized_prompt,
                f"Rights-uncleared full prompt leaked through public title: {catalog_key}",
            )

    media = record.get("media") or {}
    media_assets = media.get("assets") or []
    media_allowed = bool(canonical_rights.get("media_publication_eligible"))
    require(not media_assets or media_allowed, f"Rights-uncleared media leaked: {catalog_key}")
    canonical_assets = {
        str(asset.get("uri")): asset
        for asset in source_record["media_assets"]
        if isinstance(asset, dict) and asset.get("uri")
    }
    for asset in media_assets:
        uri = str(asset.get("uri") or "")
        require(uri in canonical_assets, f"Public media is not canonical: {catalog_key} / {uri}")
        canonical_asset = canonical_assets[uri]
        require(not canonical_asset.get("private_path"), f"Private media path leaked: {catalog_key}")
        require(
            not canonical_asset.get("generated_staging") or canonical_asset.get("release_eligible"),
            f"Release-ineligible generated staging media leaked: {catalog_key}",
        )
    require(
        bool(media.get("available_in_public_export")) == bool(media_assets),
        f"Public media availability metadata drift: {catalog_key}",
    )
    if canonical_rights.get("rights_tier") == "P2":
        require(not prompt_text and not media_assets, f"P2 exceeded metadata-link-only scope: {catalog_key}")
    if catalog_key.startswith("opennana:"):
        require(prompt.get("text_included") is False and not prompt_text, f"OpenNana prompt leaked publicly: {catalog_key}")
        require(not media_assets, f"OpenNana media leaked publicly: {catalog_key}")
    return catalog_key, int(bool(prompt_text)), len(media_assets)


def validate_public(
    manifest: dict[str, Any], canonical: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    require(PUBLIC_INDEX_PATH.is_file(), f"Missing public catalog index: {PUBLIC_INDEX_PATH}")
    index = load_json(PUBLIC_INDEX_PATH)
    public_manifest = manifest.get("public_export") or {}
    index_size, index_digest = file_digest(PUBLIC_INDEX_PATH)
    require(index_size == public_manifest.get("index_bytes"), "Public index byte count mismatch")
    require(index_digest == public_manifest.get("index_sha256"), "Public index file hash mismatch")
    require(index.get("content_sha256") == object_sha256(index), "Public index content hash mismatch")
    require(
        index.get("content_sha256") == public_manifest.get("index_content_sha256"),
        "Public index content hash differs from manifest",
    )
    expected_public_keys = {
        key
        for key, row in canonical.items()
        if row["rights"].get("rights_tier") in PUBLIC_RIGHTS_TIERS
        and row["rights"].get("public_metadata_eligible") is True
    }
    expected_total = len(expected_public_keys)
    expected_shard_count = (expected_total + 499) // 500
    require(
        public_manifest.get("canonical_record_count") == len(canonical),
        "Manifest public export canonical count drifted",
    )
    require(
        index.get("canonical_record_count") == len(canonical),
        "Public index canonical count drifted",
    )
    require(public_manifest.get("record_count") == expected_total, "Manifest public record count drifted")
    require(index.get("record_count") == expected_total, "Public index record count drifted")
    require(index.get("style_id_count") == expected_total, "Public index Style ID count drifted")
    require(index.get("shard_size") == 500, "Public shard size must be 500")
    require(index.get("shard_count") == expected_shard_count, "Public shard count drifted")
    require(contains_private_path(index) is None, "Private path leaked into public catalog index")

    index_shards = index.get("shards") or []
    manifest_shards = public_manifest.get("shards") or []
    require(index_shards == manifest_shards, "Public shard descriptors differ between index and manifest")
    require(len(index_shards) == expected_shard_count, "Public index shard descriptors drifted")

    index_rows = index.get("records") or []
    require(len(index_rows) == expected_total, "Public index locator row count drifted")
    locator_by_key: dict[str, dict[str, Any]] = {}
    for row in index_rows:
        key = str(row.get("catalog_key") or "")
        require(key not in locator_by_key, f"Duplicate public index catalog key: {key}")
        require(key in expected_public_keys, f"Admin-only or absent key entered public index: {key}")
        require(
            row.get("style_id") == canonical[key]["style_id"],
            f"Public index Style ID drift: {key}",
        )
        require(
            row.get("rights_status") == canonical[key]["rights"].get("status"),
            f"Public index rights status drift: {key}",
        )
        locator_by_key[key] = row

    public_keys: set[str] = set()
    prompt_count = 0
    media_count = 0
    for descriptor in index_shards:
        shard_id = str(descriptor.get("shard_id") or "")
        relative_path = str(descriptor.get("path") or "")
        shard_path = PUBLIC_ROOT / relative_path
        require(shard_path.is_file(), f"Missing public shard: {relative_path}")
        size, digest = file_digest(shard_path)
        require(size == descriptor.get("bytes"), f"Public shard byte count mismatch: {shard_id}")
        require(digest == descriptor.get("sha256"), f"Public shard file hash mismatch: {shard_id}")
        shard = load_json(shard_path)
        require(shard.get("shard_id") == shard_id, f"Public shard ID mismatch: {shard_id}")
        require(shard.get("content_sha256") == object_sha256(shard), f"Shard content hash mismatch: {shard_id}")
        require(
            shard.get("content_sha256") == descriptor.get("content_sha256"),
            f"Shard content hash differs from manifest: {shard_id}",
        )
        records = shard.get("records") or []
        require(len(records) == descriptor.get("record_count"), f"Shard record count mismatch: {shard_id}")
        require(shard.get("record_count") == len(records), f"Shard internal count mismatch: {shard_id}")
        if records:
            require(
                records[0].get("catalog_key") == descriptor.get("first_catalog_key"),
                f"Shard first key mismatch: {shard_id}",
            )
            require(
                records[-1].get("catalog_key") == descriptor.get("last_catalog_key"),
                f"Shard last key mismatch: {shard_id}",
            )
        for record in records:
            key, prompt_increment, media_increment = validate_public_record(record, canonical, shard_id)
            require(key not in public_keys, f"Duplicate public shard record: {key}")
            require(
                locator_by_key[key].get("shard_id") == shard_id,
                f"Public index points to wrong shard: {key}",
            )
            require(
                locator_by_key[key].get("content_sha256") == record.get("content_sha256"),
                f"Public index content hash pointer drift: {key}",
            )
            public_keys.add(key)
            prompt_count += prompt_increment
            media_count += media_increment

    require(public_keys == expected_public_keys, "Public shards do not exactly cover the P1/P2 catalog")
    require(prompt_count == index.get("prompt_text_included_count"), "Public prompt count drifted")
    require(prompt_count == public_manifest.get("prompt_text_included_count"), "Manifest prompt count drifted")
    require(media_count == index.get("media_asset_included_count"), "Public media count drifted")
    require(media_count == public_manifest.get("media_asset_included_count"), "Manifest media count drifted")
    return {
        "record_count": len(public_keys),
        "shard_count": len(index_shards),
        "prompt_text_included_count": prompt_count,
        "media_asset_included_count": media_count,
        "index_bytes": index_size,
        "index_sha256": index_digest,
    }


def validate() -> dict[str, Any]:
    canonical, canonical_stats = canonical_summary()
    manifest = validate_manifest(canonical_stats)
    public_stats = validate_public(manifest, canonical)
    return {
        "status": "pass",
        "record_count": canonical_stats["expected_total"],
        "base_record_count": BASE_TOTAL,
        "opennana_record_count": canonical_stats["opennana_record_count"],
        "lane_counts": canonical_stats["lane_counts"],
        "catalog_key_count": canonical_stats["catalog_key_count"],
        "record_id_count": canonical_stats["record_id_count"],
        "style_id_count": canonical_stats["style_id_count"],
        "canonical_jsonl_bytes": canonical_stats["bytes"],
        "canonical_jsonl_sha256": canonical_stats["sha256"],
        "generated_overlay_matched_count": int((manifest.get("generated_preview_overlay") or {}).get("matched_asset_count") or 0),
        "public": public_stats,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stream-validate the canonical image-prompt JSONL and rights-safe public export."
    )
    parser.parse_args()
    try:
        result = validate()
    except (ValidationFailure, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "fail", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
