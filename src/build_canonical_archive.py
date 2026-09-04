from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
LEGACY_ROOT = PLATFORM_ROOT / "legacy" / "current_archive"
CANONICAL_PATH = PLATFORM_ROOT / "data" / "canonical" / "archive_records.jsonl"
MANIFEST_PATH = PLATFORM_ROOT / "data" / "canonical" / "archive_records_manifest.json"
PUBLIC_ROOT = PLATFORM_ROOT / "data" / "public-export"
PUBLIC_INDEX_PATH = PUBLIC_ROOT / "catalog-index.json"
PUBLIC_SHARDS_DIR = PUBLIC_ROOT / "shards"
OPENNANA_ARCHIVE_PATH = PLATFORM_ROOT / "data" / "private-research" / "opennana" / "archive" / "opennana_records.json"

SCHEMA_VERSION = "canonical-image-prompt-record-1.1"
MANIFEST_SCHEMA_VERSION = "canonical-image-prompt-manifest-1.1"
PUBLIC_SCHEMA_VERSION = "public-image-prompt-record-1.1"
PUBLIC_INDEX_SCHEMA_VERSION = "public-image-prompt-index-1.1"
PUBLIC_SHARD_SCHEMA_VERSION = "public-image-prompt-shard-1.1"
SHARD_SIZE = 500
BASE_TOTAL = 18_793
BASE_LANE_COUNTS = {
    "legacy": 529,
    "external": 18_092,
    "social": 3,
    "manual": 13,
    "secret_codes": 131,
    "bul001": 25,
}

INPUT_FILES = (
    "detailpage_candidates.json",
    "gpt_image2_cases.json",
    "external_prompt_records.json",
    "social_prompt_records.json",
    "manual_prompt_records.json",
    "secret_code_records.json",
    "bul001_template_collection.json",
    "generated_preview_assets.json",
)

PRIVATE_PATH_MARKERS = (
    "private-research",
    "private_research",
    "reference/_derived",
    "reference\\_derived",
)
GENERATED_STAGING_MARKERS = (
    "assets/generated_staging",
    "assets\\generated_staging",
    "generated_staging",
)

# Rights status is deliberately fail-closed. A repository license alone is not
# proof that an individual prompt, image, social post, or third-party example is
# covered. Records must be both release-eligible and explicitly rights-cleared.
RIGHTS_CLEAR_STATUSES = {
    "cleared",
    "commercial_reuse_cleared",
    "public_domain",
    "permissive_license_verified",
}
RIGHTS_METADATA_LINK_STATUSES = {
    "metadata_link_cleared",
    "public_metadata_cleared",
    "public_metadata_link_only",
}
RIGHTS_BLOCKED_STATUSES = {
    "blocked",
    "prohibited",
    "redistribution_prohibited",
    "rights_conflict",
    "takedown_requested",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(name: str) -> Any:
    return json.loads((LEGACY_ROOT / name).read_text(encoding="utf-8-sig"))


def load_opennana_archive() -> dict[str, Any]:
    if not OPENNANA_ARCHIVE_PATH.is_file():
        return {
            "schema_version": "opennana-internal-archive-1.0",
            "record_count": 0,
            "public_release_eligible": False,
            "records": [],
        }
    payload = json.loads(OPENNANA_ARCHIVE_PATH.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("schema_version") != "opennana-internal-archive-1.0":
        raise ValueError("Unsupported OpenNana internal archive schema")
    archive_rows = payload.get("records")
    if not isinstance(archive_rows, list) or payload.get("record_count") != len(archive_rows):
        raise ValueError("OpenNana internal archive record_count does not match records")
    if payload.get("public_release_eligible") is not False:
        raise ValueError("OpenNana internal archive must remain public-release ineligible")
    return payload


def expected_archive_shape(opennana_payload: dict[str, Any]) -> tuple[int, dict[str, int]]:
    count = len(opennana_payload.get("records") or [])
    lane_counts = dict(BASE_LANE_COUNTS)
    if count:
        lane_counts["opennana"] = count
    return BASE_TOTAL + count, lane_counts


def rows(payload: Any, key: str) -> list[dict[str, Any]]:
    value = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(value, list):
        raise ValueError(f"Expected list at {key!r}")
    return value


def stable_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def pretty_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def object_sha256(value: Any, excluded_key: str = "content_sha256") -> str:
    if not isinstance(value, dict):
        return hashlib.sha256(stable_json_bytes(value)).hexdigest()
    payload = {key: item for key, item in value.items() if key != excluded_key}
    return hashlib.sha256(stable_json_bytes(payload)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = clean_text(item)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def unique_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, list):
            candidates = value
        else:
            candidates = [value]
        for candidate in candidates:
            text = clean_text(candidate)
            if text and text not in seen:
                seen.add(text)
                result.append(text)
    return result


def compact_search_text(values: Iterable[Any]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, list):
            candidates = value
        else:
            candidates = [value]
        for candidate in candidates:
            text = clean_text(candidate)
            if not text:
                continue
            text = " ".join(text.split())
            folded = text.casefold()
            if folded not in seen:
                seen.add(folded)
                parts.append(text)
    return " ".join(parts)


def classify_uri(uri: str | None) -> str:
    if not uri:
        return "missing"
    lowered = uri.casefold()
    if lowered.startswith(("http://", "https://", "data:")):
        return "remote"
    return "local"


def is_private_path(value: str | None) -> bool:
    lowered = (value or "").replace("\\", "/").casefold()
    return any(marker.replace("\\", "/") in lowered for marker in PRIVATE_PATH_MARKERS)


def is_generated_staging_path(value: str | None) -> bool:
    lowered = (value or "").replace("\\", "/").casefold()
    return any(marker.replace("\\", "/") in lowered for marker in GENERATED_STAGING_MARKERS)


def first_nonempty(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "" and value != [] and value != {}:
            return value
    return None


def normalized_license(raw_license: Any, fallback_spdx: str | None = None) -> dict[str, Any]:
    data = raw_license if isinstance(raw_license, dict) else {}
    if isinstance(raw_license, str):
        fallback_spdx = raw_license
    reported = clean_text(data.get("reported_spdx"))
    detected = clean_text(data.get("detected_spdx"))
    effective = detected or reported or clean_text(fallback_spdx)
    return {
        "reported_spdx": reported,
        "detected_spdx": detected,
        "effective_spdx": effective,
        "status": clean_text(data.get("status")) or ("reported" if effective else "not_verified"),
        "scope": clean_text(data.get("scope")),
        "evidence_url": clean_text(data.get("evidence_url")),
        "note": clean_text(data.get("note")),
    }


def normalized_rights(
    *,
    status: Any,
    release_eligible: bool,
    license_data: dict[str, Any],
    risk_flags: Iterable[Any],
    prompt_present: bool,
    media_present: bool,
) -> dict[str, Any]:
    rights_status = clean_text(status) or "not_verified"
    normalized_status = rights_status.casefold()
    explicitly_cleared = normalized_status in RIGHTS_CLEAR_STATUSES
    if normalized_status in RIGHTS_BLOCKED_STATUSES:
        rights_tier = "P4"
        portfolio_visibility = "admin_only"
        admin_usage_status = "quarantine_only"
        public_metadata_eligible = False
    elif release_eligible and explicitly_cleared:
        rights_tier = "P1"
        portfolio_visibility = "public"
        admin_usage_status = "public_or_metadata"
        public_metadata_eligible = True
    elif release_eligible and normalized_status in RIGHTS_METADATA_LINK_STATUSES:
        rights_tier = "P2"
        portfolio_visibility = "metadata_link_only"
        admin_usage_status = "public_or_metadata"
        public_metadata_eligible = True
    else:
        rights_tier = "P3"
        portfolio_visibility = "admin_only"
        admin_usage_status = "reference_allowed"
        public_metadata_eligible = False
    prompt_publication_eligible = bool(rights_tier == "P1" and prompt_present)
    media_publication_eligible = bool(rights_tier == "P1" and media_present)
    return {
        "status": rights_status,
        "rights_tier": rights_tier,
        "portfolio_visibility": portfolio_visibility,
        "admin_usage_status": admin_usage_status,
        "public_metadata_eligible": public_metadata_eligible,
        "release_eligible": bool(release_eligible),
        "explicitly_cleared": explicitly_cleared,
        "prompt_publication_eligible": prompt_publication_eligible,
        "media_publication_eligible": media_publication_eligible,
        "commercial_reuse_claimed": False,
        "requires_human_review": not (release_eligible and explicitly_cleared),
        "license_is_not_item_rights_clearance": not explicitly_cleared,
        "risk_flags": unique_strings(risk_flags),
        "effective_spdx": license_data.get("effective_spdx"),
    }


def normalized_source(
    *,
    name: Any = None,
    url: Any = None,
    source_type: Any = None,
    repository: Any = None,
    commit: Any = None,
    pinned_url: Any = None,
) -> dict[str, Any]:
    return {
        "name": clean_text(name),
        "url": clean_text(url),
        "type": clean_text(source_type),
        "repository": clean_text(repository),
        "commit": clean_text(commit),
        "pinned_url": clean_text(pinned_url),
    }


def prompt_object(text: str | None, prompt_format: Any = None, language: Any = None) -> dict[str, Any]:
    return {
        "present": bool(text),
        "text": text,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None,
        "format": clean_text(prompt_format),
        "language": clean_text(language),
    }


def media_asset(
    uri: Any,
    *,
    origin: str,
    role: str,
    sha256: Any = None,
    mime_type: Any = None,
    width: Any = None,
    height: Any = None,
    release_eligible: bool = False,
) -> dict[str, Any] | None:
    normalized_uri = clean_text(uri)
    if not normalized_uri:
        return None
    return {
        "uri": normalized_uri,
        "uri_kind": classify_uri(normalized_uri),
        "origin": origin,
        "role": role,
        "sha256": clean_text(sha256),
        "mime_type": clean_text(mime_type),
        "width": width if isinstance(width, int) else None,
        "height": height if isinstance(height, int) else None,
        "private_path": is_private_path(normalized_uri),
        "generated_staging": origin == "generated_staging" or is_generated_staging_path(normalized_uri),
        "release_eligible": bool(release_eligible),
    }


def dedupe_assets(assets: Iterable[dict[str, Any] | None]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for asset in assets:
        if not asset:
            continue
        key = (str(asset.get("uri")), str(asset.get("role")))
        if key not in seen:
            seen.add(key)
            result.append(asset)
    return result


def taxonomy_object(
    *,
    product_categories: Any = None,
    section_roles: Any = None,
    visual_techniques: Any = None,
    style_tags: Any = None,
    use_case_tags: Any = None,
    languages: Any = None,
    search_facets: Any = None,
) -> dict[str, Any]:
    return {
        "product_categories": string_list(product_categories),
        "section_roles": string_list(section_roles),
        "visual_techniques": string_list(visual_techniques),
        "style_tags": string_list(style_tags),
        "use_case_tags": string_list(use_case_tags),
        "languages": string_list(languages),
        "search_facets": search_facets if isinstance(search_facets, dict) else {},
    }


def overlay_lookup() -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    assets = rows(load_json("generated_preview_assets.json"), "assets")
    lookup: dict[str, dict[str, Any]] = {}
    for asset in assets:
        for value in (asset.get("record_id"), asset.get("reference_style_id")):
            key = clean_text(value)
            if not key:
                continue
            previous = lookup.get(key)
            if previous is not None and previous.get("asset_id") != asset.get("asset_id"):
                raise ValueError(f"Generated preview overlay collision for {key}")
            lookup[key] = asset
    return lookup, assets


def find_overlay(
    lookup: dict[str, dict[str, Any]], record_id: str, style_id: str
) -> dict[str, Any] | None:
    by_record = lookup.get(record_id)
    by_style = lookup.get(style_id)
    if by_record and by_style and by_record.get("asset_id") != by_style.get("asset_id"):
        raise ValueError(f"Generated preview record/style mismatch for {record_id} / {style_id}")
    return by_record or by_style


def overlay_asset(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not raw:
        return None
    return media_asset(
        raw.get("local_image_path"),
        origin="generated_staging",
        role="generated_preview",
        sha256=raw.get("image_sha256"),
        release_eligible=bool(raw.get("release_eligible")),
    )


def finalize_record(record: dict[str, Any]) -> dict[str, Any]:
    taxonomy = record["taxonomy"]
    source = record["source"]
    prompt = record["prompt"]
    search_facets = taxonomy.get("search_facets") or {}
    facet_values: list[str] = []
    for value in search_facets.values():
        if isinstance(value, list):
            facet_values.extend(string_list(value))
        else:
            text = clean_text(value)
            if text:
                facet_values.append(text)
    record["search_text"] = compact_search_text(
        [
            record.get("style_id"),
            record.get("parent_style_id"),
            record.get("title"),
            prompt.get("text"),
            source.get("name"),
            source.get("type"),
            source.get("repository"),
            taxonomy.get("product_categories"),
            taxonomy.get("section_roles"),
            taxonomy.get("visual_techniques"),
            taxonomy.get("style_tags"),
            taxonomy.get("use_case_tags"),
            facet_values,
        ]
    )
    record["content_sha256"] = object_sha256(record)
    return record


def normalize_legacy(
    raw: dict[str, Any],
    *,
    index: int,
    candidate_overlay: dict[str, Any] | None,
    generated_overlay: dict[str, Any] | None,
) -> dict[str, Any]:
    case_id = int(raw["case_id"])
    style_id = f"CASE-{case_id:03d}"
    record_id = f"LEGACY-CASE-{case_id:03d}"
    prompt_raw = raw.get("prompt") if isinstance(raw.get("prompt"), dict) else {}
    text = clean_text(prompt_raw.get("raw"))
    source_raw = raw.get("source") if isinstance(raw.get("source"), dict) else {}
    fit = raw.get("detailpage_fit") if isinstance(raw.get("detailpage_fit"), dict) else {}
    asset_raw = raw.get("asset") if isinstance(raw.get("asset"), dict) else {}
    risk_raw = raw.get("risk_and_rights") if isinstance(raw.get("risk_and_rights"), dict) else {}
    source = normalized_source(
        name=source_raw.get("source_label"),
        url=source_raw.get("source_url"),
        source_type="upstream_gallery_case",
        repository=source_raw.get("repository"),
        commit=source_raw.get("commit"),
        pinned_url=source_raw.get("gallery_url_pinned"),
    )
    license_data = normalized_license(
        {
            "reported_spdx": risk_raw.get("repository_code_license"),
            "status": "repository_code_only",
            "scope": "repository code; third-party case prompts and images are separate",
            "evidence_url": source_raw.get("repository"),
            "note": risk_raw.get("third_party_case_and_image_rights"),
        }
    )
    generated_asset = overlay_asset(generated_overlay)
    source_asset = media_asset(
        asset_raw.get("local_path") or asset_raw.get("pinned_raw_url"),
        origin="source_reference",
        role="source_preview",
        sha256=asset_raw.get("sha256"),
        mime_type=asset_raw.get("mime_type"),
        width=asset_raw.get("width"),
        height=asset_raw.get("height"),
        release_eligible=False,
    )
    assets = dedupe_assets((generated_asset, source_asset))
    release_eligible = False
    rights_status = risk_raw.get("commercial_reuse_status") or "not_cleared"
    rights = normalized_rights(
        status=rights_status,
        release_eligible=release_eligible,
        license_data=license_data,
        risk_flags=risk_raw.get("risk_flags") or [],
        prompt_present=bool(text),
        media_present=bool(assets),
    )
    taxonomy = taxonomy_object(
        product_categories=fit.get("product_categories"),
        section_roles=fit.get("section_roles"),
        visual_techniques=fit.get("prompting_techniques"),
        style_tags=source_raw.get("upstream_styles_inferred"),
        use_case_tags=source_raw.get("upstream_scenes_inferred"),
        languages=[prompt_raw.get("language")] if prompt_raw.get("language") else [],
        search_facets={
            "candidate_scope": fit.get("candidate_scope"),
            "tier": fit.get("tier"),
            "recommended_blueprint_ids": fit.get("recommended_blueprint_ids") or [],
            "control_blocks": prompt_raw.get("control_blocks_detected") or [],
        },
    )
    return finalize_record(
        {
            "schema_version": SCHEMA_VERSION,
            "catalog_key": f"legacy:{record_id}",
            "lane": "legacy",
            "record_id": record_id,
            "style_id": style_id,
            "parent_style_id": None,
            "title": clean_text(raw.get("title_original")) or style_id,
            "prompt": prompt_object(text, prompt_raw.get("format"), prompt_raw.get("language")),
            "source": source,
            "license": license_data,
            "rights": rights,
            "media": {"assets": assets, "primary_role": assets[0]["role"] if assets else None},
            "taxonomy": taxonomy,
            "generation": {
                "generated_preview_overlay": generated_overlay,
                "model_relation": None,
                "reported_model": None,
                "canary_status": None,
            },
            "review_release": {
                "review_status": "rule_based_unverified",
                "release_eligible": release_eligible,
                "selected_for_detailpage_pool": bool(fit.get("selected_for_detailpage_pool")),
                "candidate_scope": fit.get("candidate_scope"),
                "tier": fit.get("tier"),
                "risk_flags": unique_strings(risk_raw.get("risk_flags") or []),
            },
            "provenance": {
                "source_file": "legacy/current_archive/gpt_image2_cases.json",
                "source_record_locator": f"cases[{index}]",
                "detailpage_candidate_overlay_file": (
                    "legacy/current_archive/detailpage_candidates.json" if candidate_overlay else None
                ),
                "raw_source": raw,
                "editorial_overlay": candidate_overlay,
                "generated_overlay_source": generated_overlay,
            },
        }
    )


def normalized_unified(
    raw: dict[str, Any],
    *,
    lane: str,
    index: int,
    source_file: str,
    generated_overlay: dict[str, Any] | None,
) -> dict[str, Any]:
    record_id = clean_text(raw.get("record_id"))
    style_id = clean_text(raw.get("reference_style_id"))
    if not record_id or not style_id:
        raise ValueError(f"Missing record_id/reference_style_id in {source_file}[{index}]")
    text = clean_text(raw.get("prompt"))
    source = normalized_source(
        name=raw.get("source_name"),
        url=raw.get("source_url"),
        source_type=raw.get("source_type"),
        repository=(raw.get("raw_metadata") or {}).get("repository")
        if isinstance(raw.get("raw_metadata"), dict)
        else None,
        commit=raw.get("source_commit"),
        pinned_url=raw.get("source_file_url_pinned"),
    )
    license_data = normalized_license(raw.get("source_license"))
    generated_asset = overlay_asset(generated_overlay)
    raw_generated = raw.get("generated_preview_metadata")
    raw_generated = raw_generated if isinstance(raw_generated, dict) else {}
    raw_image = clean_text(raw.get("image")) or clean_text(raw.get("thumbnail"))
    raw_image_is_staging = bool(raw_generated.get("is_generated_staging")) or is_generated_staging_path(raw_image)
    original_asset = media_asset(
        raw_image,
        origin="generated_staging" if raw_image_is_staging else "source_reference",
        role="generated_preview" if raw_image_is_staging else "source_preview",
        sha256=raw_generated.get("preview_file_sha256"),
        release_eligible=bool(raw.get("release_eligible")) and not raw_image_is_staging,
    )
    assets = dedupe_assets((generated_asset, original_asset))
    release_eligible = bool(raw.get("release_eligible"))
    rights = normalized_rights(
        status=raw.get("rights_status"),
        release_eligible=release_eligible,
        license_data=license_data,
        risk_flags=raw.get("risk_flags") or [],
        prompt_present=bool(text),
        media_present=bool(assets),
    )
    taxonomy = taxonomy_object(
        product_categories=raw.get("product_categories"),
        section_roles=raw.get("section_roles"),
        visual_techniques=raw.get("visual_techniques"),
        style_tags=raw.get("style_tags"),
        use_case_tags=raw.get("use_case_tags"),
        languages=raw.get("languages"),
        search_facets=raw.get("search_facets"),
    )
    return finalize_record(
        {
            "schema_version": SCHEMA_VERSION,
            "catalog_key": f"{lane}:{record_id}",
            "lane": lane,
            "record_id": record_id,
            "style_id": style_id,
            "parent_style_id": clean_text(raw.get("parent_reference_style_id")),
            "title": clean_text(raw.get("title")) or style_id,
            "prompt": prompt_object(text, "freeform_prose", ",".join(string_list(raw.get("languages"))) or None),
            "source": source,
            "license": license_data,
            "rights": rights,
            "media": {"assets": assets, "primary_role": assets[0]["role"] if assets else None},
            "taxonomy": taxonomy,
            "generation": {
                "model_relation": clean_text(raw.get("model_relation")),
                "reported_model": clean_text(raw.get("reported_model")),
                "gpt_image_2_evidence": raw.get("gpt_image_2_evidence")
                if isinstance(raw.get("gpt_image_2_evidence"), dict)
                else {},
                "canary_status": clean_text(raw.get("canary_status")),
                "record_generated_preview_metadata": raw_generated,
                "generated_preview_overlay": generated_overlay,
            },
            "review_release": {
                "review_status": clean_text(raw.get("review_status")) or "needs_human_review",
                "release_eligible": release_eligible,
                "provenance_status": clean_text(raw.get("provenance_status")),
                "prompt_image_pair_status": clean_text(raw.get("prompt_image_pair_status")),
                "risk_flags": unique_strings(raw.get("risk_flags") or []),
            },
            "provenance": {
                "source_file": f"legacy/current_archive/{source_file}",
                "source_record_locator": f"records[{index}]",
                "raw_source": raw,
                "generated_overlay_source": generated_overlay,
            },
        }
    )


def normalize_opennana(raw: dict[str, Any], *, index: int) -> dict[str, Any]:
    upstream_id = clean_text(raw.get("upstream_id"))
    if not upstream_id:
        raise ValueError(f"Missing upstream_id in OpenNana archive records[{index}]")
    record_id = f"OPENNANA-{upstream_id}"
    style_id = f"ONN-{upstream_id}"
    if clean_text(raw.get("record_id")) != record_id or clean_text(raw.get("reference_style_id")) != style_id:
        raise ValueError(f"OpenNana stable identity mismatch for upstream_id={upstream_id}")
    if raw.get("release_eligible") is not False or (raw.get("rights") or {}).get("release_eligible") is not False:
        raise ValueError(f"OpenNana internal archive row attempts release: {style_id}")
    text = clean_text(raw.get("prompt"))
    if not text:
        raise ValueError(f"OpenNana internal archive row lacks prompt text: {style_id}")
    source = normalized_source(
        name="OpenNana",
        url=raw.get("source_url"),
        source_type="curated_prompt_gallery_internal",
    )
    license_data = normalized_license(
        {
            "status": "not_verified",
            "scope": "individual prompt and media rights are unverified",
            "evidence_url": raw.get("source_url"),
            "note": "Human curation is not copyright or commercial-use clearance.",
        }
    )
    assets = dedupe_assets(
        media_asset(
            uri,
            origin="source_reference",
            role="source_preview",
            release_eligible=False,
        )
        for uri in raw.get("image_urls") or []
    )
    rights = normalized_rights(
        status="not_cleared",
        release_eligible=False,
        license_data=license_data,
        risk_flags=["item_rights_unverified", "public_release_not_approved"],
        prompt_present=True,
        media_present=bool(assets),
    )
    if any(
        (
            rights.get("release_eligible"),
            rights.get("prompt_publication_eligible"),
            rights.get("media_publication_eligible"),
            rights.get("commercial_reuse_claimed"),
        )
    ):
        raise ValueError(f"OpenNana rights boundary failed closed: {style_id}")
    tags = string_list(raw.get("tags"))
    taxonomy = taxonomy_object(
        style_tags=unique_strings([tags, raw.get("style_tags")]),
        use_case_tags=unique_strings([raw.get("use_case_tags"), ["prompt_reference", "internal_archive"]]),
        languages=raw.get("languages"),
        search_facets={
            "source_platform": "opennana",
            "upstream_id": upstream_id,
            "reported_model": clean_text(raw.get("reported_model")),
            "media_type": clean_text(raw.get("media_type")),
            "group_with": clean_text((raw.get("human_decision") or {}).get("group_with")),
        },
    )
    return finalize_record(
        {
            "schema_version": SCHEMA_VERSION,
            "catalog_key": f"opennana:{record_id}",
            "lane": "opennana",
            "record_id": record_id,
            "style_id": style_id,
            "parent_style_id": clean_text((raw.get("human_decision") or {}).get("group_with")),
            "title": clean_text(raw.get("title")) or style_id,
            "prompt": prompt_object(text, "freeform_prose", None),
            "source": source,
            "license": license_data,
            "rights": rights,
            "media": {"assets": assets, "primary_role": "source_preview" if assets else None},
            "taxonomy": taxonomy,
            "generation": {
                "model_relation": "reported_generation_model",
                "reported_model": clean_text(raw.get("reported_model")),
                "canary_status": None,
            },
            "review_release": {
                "review_status": "human_approved_internal_reference",
                "release_eligible": False,
                "canonicalization_decision": clean_text((raw.get("human_decision") or {}).get("decision")),
                "rights_clearance_effect": False,
                "public_release_effect": False,
                "risk_flags": ["item_rights_unverified", "public_release_not_approved"],
            },
            "provenance": {
                "source_file": "data/private-research/opennana/archive/opennana_records.json",
                "source_record_locator": f"records[{index}]",
                "source_content_sha256": clean_text(raw.get("source_content_sha256")),
                "raw_source": raw,
            },
        }
    )


def bul_prompt_text(raw: dict[str, Any]) -> str:
    sections = [
        "Prompt template:\n" + json.dumps(raw.get("prompt_template") or {}, ensure_ascii=False, indent=2)
    ]
    examples = string_list(raw.get("examples"))
    if examples:
        sections.append("Examples:\n" + "\n".join(f"- {item}" for item in examples))
    generation_prompt = clean_text(raw.get("generation_prompt"))
    if generation_prompt:
        sections.append("Canary generation prompt:\n" + generation_prompt)
    return "\n\n".join(sections)


def normalize_bul(
    raw: dict[str, Any], *, index: int, collection: dict[str, Any]
) -> dict[str, Any]:
    style_id = clean_text(raw.get("reference_style_id"))
    if not style_id:
        raise ValueError(f"Missing BUL style id at templates[{index}]")
    record_id = f"BUL-TEMPLATE-{style_id.removeprefix('BUL-')}"
    source_raw = raw.get("source") if isinstance(raw.get("source"), dict) else {}
    source = normalized_source(
        name=collection.get("source_repository"),
        url=source_raw.get("blob_url"),
        source_type="open_source_prompt_template",
        repository=collection.get("source_repository"),
        commit=collection.get("source_commit"),
        pinned_url=source_raw.get("blob_url"),
    )
    license_data = normalized_license(
        {
            "reported_spdx": collection.get("source_license"),
            "status": "reported",
            "scope": "repository template; item rights still require release review",
            "evidence_url": source_raw.get("blob_url"),
            "note": "Generated previews are internal staging outputs, not upstream source outputs.",
        }
    )
    text = bul_prompt_text(raw)
    asset = media_asset(
        raw.get("generated_preview_path"),
        origin="generated_staging",
        role="generated_preview",
        sha256=raw.get("generated_preview_sha256"),
        width=raw.get("generated_preview_width"),
        height=raw.get("generated_preview_height"),
        release_eligible=bool(raw.get("release_eligible")),
    )
    assets = dedupe_assets((asset,))
    release_eligible = bool(raw.get("release_eligible"))
    rights = normalized_rights(
        status="permissive_repo_research_only",
        release_eligible=release_eligible,
        license_data=license_data,
        risk_flags=["generated_preview_needs_human_release_review"],
        prompt_present=True,
        media_present=bool(assets),
    )
    category_tips = raw.get("category_tips") if isinstance(raw.get("category_tips"), dict) else {}
    taxonomy = taxonomy_object(
        product_categories=list(category_tips),
        section_roles=["ecommerce_template", clean_text(raw.get("source_template_id"))],
        visual_techniques=raw.get("keywords"),
        style_tags=raw.get("trigger_phrases"),
        use_case_tags=["detail_page", "ecommerce", "prompt_template"],
        languages=["zh", "en"],
        search_facets={
            "supports_image_reference": bool(raw.get("supports_image_reference")),
            "sequence": raw.get("sequence"),
            "variants": list((raw.get("variants") or {}).keys())
            if isinstance(raw.get("variants"), dict)
            else [],
        },
    )
    return finalize_record(
        {
            "schema_version": SCHEMA_VERSION,
            "catalog_key": f"bul001:{record_id}",
            "lane": "bul001",
            "record_id": record_id,
            "style_id": style_id,
            "parent_style_id": clean_text(raw.get("parent_reference_style_id")) or "BUL-001",
            "title": clean_text(raw.get("title_ko")) or clean_text(raw.get("title")) or style_id,
            "prompt": prompt_object(text, "structured_template", "multilingual_zh_en"),
            "source": source,
            "license": license_data,
            "rights": rights,
            "media": {"assets": assets, "primary_role": "generated_preview" if assets else None},
            "taxonomy": taxonomy,
            "generation": {
                "requested_model_policy": collection.get("requested_model_policy"),
                "execution_path": collection.get("execution_path"),
                "execution_model_attestation": collection.get("execution_model_attestation"),
                "generation_prompt": clean_text(raw.get("generation_prompt")),
                "edit_prompt": clean_text(raw.get("edit_prompt")),
                "revision": raw.get("generation_revision"),
            },
            "review_release": {
                "review_status": clean_text(raw.get("review_status")) or "pending_human_review",
                "release_eligible": release_eligible,
                "visual_review": raw.get("visual_review") if isinstance(raw.get("visual_review"), dict) else {},
                "is_source_output": bool(raw.get("is_source_output")),
                "is_product_evidence": bool(raw.get("is_product_evidence")),
                "risk_flags": ["generated_preview_needs_human_release_review"],
            },
            "provenance": {
                "source_file": "legacy/current_archive/bul001_template_collection.json",
                "source_record_locator": f"templates[{index}]",
                "raw_source": raw,
                "collection_metadata": collection,
            },
        }
    )


def iter_canonical_records(
    opennana_payload: dict[str, Any] | None = None,
) -> tuple[Iterator[dict[str, Any]], dict[str, Any]]:
    lookup, overlay_assets = overlay_lookup()
    opennana_payload = opennana_payload or load_opennana_archive()

    def generator() -> Iterator[dict[str, Any]]:
        candidate_payload = load_json("detailpage_candidates.json")
        candidate_rows = rows(candidate_payload, "cases")
        candidates = {int(item["case_id"]): item for item in candidate_rows}
        advertised_count = int((candidate_payload.get("metadata") or {}).get("case_record_count") or 0)
        legacy_payload = load_json("gpt_image2_cases.json")
        legacy_rows = rows(legacy_payload, "cases")
        if advertised_count != len(legacy_rows):
            raise ValueError(
                "detailpage_candidates.json advertises "
                f"{advertised_count} cases but gpt_image2_cases.json contains {len(legacy_rows)}"
            )
        for index, raw in enumerate(legacy_rows):
            case_id = int(raw["case_id"])
            record_id = f"LEGACY-CASE-{case_id:03d}"
            style_id = f"CASE-{case_id:03d}"
            yield normalize_legacy(
                raw,
                index=index,
                candidate_overlay=candidates.get(case_id),
                generated_overlay=find_overlay(lookup, record_id, style_id),
            )

        for lane, filename in (
            ("external", "external_prompt_records.json"),
            ("social", "social_prompt_records.json"),
            ("manual", "manual_prompt_records.json"),
            ("secret_codes", "secret_code_records.json"),
        ):
            source_rows = rows(load_json(filename), "records")
            if lane == "manual":
                source_rows = [
                    item
                    for item in source_rows
                    if clean_text(item.get("source_id")) != "secret-code-notion84"
                    and clean_text((item.get("raw_metadata") or {}).get("archive_group")) != "secret_codes"
                ]
            for index, raw in enumerate(source_rows):
                record_id = clean_text(raw.get("record_id")) or ""
                style_id = clean_text(raw.get("reference_style_id")) or ""
                yield normalized_unified(
                    raw,
                    lane=lane,
                    index=index,
                    source_file=filename,
                    generated_overlay=find_overlay(lookup, record_id, style_id),
                )

        bul_payload = load_json("bul001_template_collection.json")
        collection = bul_payload.get("collection") if isinstance(bul_payload.get("collection"), dict) else {}
        for index, raw in enumerate(rows(bul_payload, "templates")):
            yield normalize_bul(raw, index=index, collection=collection)

        for index, raw in enumerate(rows(opennana_payload, "records")):
            yield normalize_opennana(raw, index=index)

    return generator(), {
        "generated_overlay_count": len(overlay_assets),
        "overlay_lookup": lookup,
        "opennana_record_count": len(opennana_payload.get("records") or []),
    }


def safe_public_title(record: dict[str, Any], include_prompt: bool) -> str:
    title = str(record.get("title") or record.get("style_id") or "Untitled reference")
    prompt_text = clean_text(record.get("prompt", {}).get("text"))
    if not include_prompt and prompt_text and " ".join(title.split()) == " ".join(prompt_text.split()):
        return f"Reference {record.get('style_id')}"
    return title


def public_search_text(record: dict[str, Any], include_prompt: bool) -> str:
    taxonomy = record["taxonomy"]
    source = record["source"]
    values: list[Any] = [
        record.get("style_id"),
        record.get("parent_style_id"),
        safe_public_title(record, include_prompt),
        source.get("name"),
        source.get("type"),
        source.get("repository"),
        taxonomy.get("product_categories"),
        taxonomy.get("section_roles"),
        taxonomy.get("visual_techniques"),
        taxonomy.get("style_tags"),
        taxonomy.get("use_case_tags"),
    ]
    if include_prompt:
        values.append(record["prompt"].get("text"))
    return compact_search_text(values)


def public_record(record: dict[str, Any]) -> dict[str, Any]:
    rights = record["rights"]
    rights_tier = rights.get("rights_tier")
    if rights_tier not in {"P1", "P2"} or not rights.get("public_metadata_eligible"):
        raise ValueError(f"Admin-only record cannot enter public export: {record['catalog_key']}")
    if rights_tier == "P1" and not (rights.get("release_eligible") and rights.get("explicitly_cleared")):
        raise ValueError(f"P1 record lacks release and rights clearance: {record['catalog_key']}")
    if rights_tier == "P2" and (
        rights.get("portfolio_visibility") != "metadata_link_only"
        or rights.get("prompt_publication_eligible")
        or rights.get("media_publication_eligible")
    ):
        raise ValueError(f"P2 record exceeded metadata-link-only scope: {record['catalog_key']}")
    prompt = record["prompt"]
    prompt_allowed = bool(rights.get("prompt_publication_eligible"))
    media_allowed = bool(rights.get("media_publication_eligible"))
    included_assets: list[dict[str, Any]] = []
    if media_allowed:
        for asset in record["media"].get("assets") or []:
            if asset.get("private_path"):
                continue
            if asset.get("generated_staging") and not asset.get("release_eligible"):
                continue
            included_assets.append(
                {
                    key: asset.get(key)
                    for key in ("uri", "uri_kind", "origin", "role", "sha256", "mime_type", "width", "height")
                }
            )
    public_prompt = {
        "present_in_private_catalog": bool(prompt.get("present")),
        "available_in_public_export": prompt_allowed,
        "text_included": bool(prompt_allowed and prompt.get("text")),
        "sha256": prompt.get("sha256"),
        "omission_reason": None if prompt_allowed else "rights_or_release_gate_not_passed",
    }
    if prompt_allowed:
        public_prompt["text"] = prompt.get("text")
    public_media = {
        "present_in_private_catalog": bool(record["media"].get("assets")),
        "available_in_public_export": bool(included_assets),
        "assets": included_assets,
        "omission_reason": None if included_assets else "rights_or_release_gate_not_passed",
    }
    public = {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "catalog_key": record["catalog_key"],
        "canonical_content_sha256": record["content_sha256"],
        "lane": record["lane"],
        "record_id": record["record_id"],
        "style_id": record["style_id"],
        "parent_style_id": record["parent_style_id"],
        "title": safe_public_title(record, prompt_allowed),
        "source": record["source"],
        "license": {
            key: record["license"].get(key)
            for key in ("reported_spdx", "detected_spdx", "effective_spdx", "status", "scope", "evidence_url")
        },
        "rights": {
            key: rights.get(key)
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
            )
        },
        "prompt": public_prompt,
        "media": public_media,
        "taxonomy": record["taxonomy"],
        "review_release": {
            "review_status": record["review_release"].get("review_status"),
            "release_eligible": record["review_release"].get("release_eligible"),
        },
        "search_text": public_search_text(record, include_prompt=prompt_allowed),
    }
    public["content_sha256"] = object_sha256(public)
    return public


def index_row(record: dict[str, Any], shard_id: str) -> dict[str, Any]:
    return {
        "catalog_key": record["catalog_key"],
        "lane": record["lane"],
        "record_id": record["record_id"],
        "style_id": record["style_id"],
        "parent_style_id": record["parent_style_id"],
        "title": record["title"],
        "source_name": record["source"].get("name"),
        "source_url": record["source"].get("url"),
        "license_spdx": record["license"].get("effective_spdx"),
        "rights_status": record["rights"].get("status"),
        "rights_tier": record["rights"].get("rights_tier"),
        "portfolio_visibility": record["rights"].get("portfolio_visibility"),
        "release_eligible": record["rights"].get("release_eligible"),
        "prompt_availability": (
            "included" if record["prompt"].get("available_in_public_export") else "metadata_only"
        ),
        "media_availability": (
            "included" if record["media"].get("available_in_public_export") else "metadata_only"
        ),
        "search_text": record["search_text"],
        "shard_id": shard_id,
        "content_sha256": record["content_sha256"],
    }


def input_evidence(opennana_payload: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = []
    for name in INPUT_FILES:
        path = LEGACY_ROOT / name
        evidence.append(
            {
                "path": f"legacy/current_archive/{name}",
                "bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    if OPENNANA_ARCHIVE_PATH.is_file():
        evidence.append(
            {
                "path": "data/private-research/opennana/archive/opennana_records.json",
                "bytes": OPENNANA_ARCHIVE_PATH.stat().st_size,
                "sha256": file_sha256(OPENNANA_ARCHIVE_PATH),
                "record_count": len(opennana_payload.get("records") or []),
                "release_eligible": False,
            }
        )
    return evidence


def stage_bytes(path: Path, data: bytes, pending: list[tuple[Path, Path]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    pending.append((temporary, path))


def commit_pending(pending: list[tuple[Path, Path]]) -> None:
    for temporary, destination in pending:
        os.replace(temporary, destination)


def build(*, apply: bool) -> dict[str, Any]:
    generated_at = utc_now()
    opennana_payload = load_opennana_archive()
    expected_total, expected_lane_counts = expected_archive_shape(opennana_payload)
    canonical_digest = hashlib.sha256()
    canonical_bytes = 0
    record_count = 0
    lane_counts: Counter[str] = Counter()
    style_ids: set[str] = set()
    catalog_keys: set[str] = set()
    record_ids: set[str] = set()
    overlay_asset_ids: set[str] = set()
    public_prompt_count = 0
    public_media_count = 0
    public_record_count = 0
    public_style_ids: set[str] = set()
    public_index_rows: list[dict[str, Any]] = []
    shard_descriptors: list[dict[str, Any]] = []
    current_shard: list[dict[str, Any]] = []
    pending: list[tuple[Path, Path]] = []

    canonical_temp: Path | None = None
    canonical_handle = None
    if apply:
        CANONICAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        canonical_temp = CANONICAL_PATH.with_name(CANONICAL_PATH.name + ".tmp")
        canonical_handle = canonical_temp.open("wb")

    def flush_shard() -> None:
        nonlocal current_shard
        if not current_shard:
            return
        shard_number = len(shard_descriptors) + 1
        shard_id = f"catalog-{shard_number:04d}"
        payload: dict[str, Any] = {
            "schema_version": PUBLIC_SHARD_SCHEMA_VERSION,
            "shard_id": shard_id,
            "record_count": len(current_shard),
            "records": current_shard,
        }
        payload["content_sha256"] = object_sha256(payload)
        data = pretty_json_bytes(payload)
        relative_path = f"shards/{shard_id}.json"
        descriptor = {
            "shard_id": shard_id,
            "path": relative_path,
            "record_count": len(current_shard),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "content_sha256": payload["content_sha256"],
            "first_catalog_key": current_shard[0]["catalog_key"],
            "last_catalog_key": current_shard[-1]["catalog_key"],
        }
        shard_descriptors.append(descriptor)
        for row in current_shard:
            public_index_rows.append(index_row(row, shard_id))
        if apply:
            stage_bytes(PUBLIC_ROOT / relative_path, data, pending)
        current_shard = []

    records_iterator, overlay_info = iter_canonical_records(opennana_payload)
    try:
        for record in records_iterator:
            catalog_key = record["catalog_key"]
            style_id = record["style_id"]
            record_id = record["record_id"]
            if catalog_key in catalog_keys:
                raise ValueError(f"Duplicate catalog_key: {catalog_key}")
            if style_id in style_ids:
                raise ValueError(f"Duplicate style_id: {style_id}")
            if record_id in record_ids:
                raise ValueError(f"Duplicate record_id: {record_id}")
            catalog_keys.add(catalog_key)
            style_ids.add(style_id)
            record_ids.add(record_id)
            lane_counts[record["lane"]] += 1
            generated_overlay = record["generation"].get("generated_preview_overlay")
            if isinstance(generated_overlay, dict) and generated_overlay.get("asset_id"):
                overlay_asset_ids.add(str(generated_overlay["asset_id"]))

            line = stable_json_bytes(record) + b"\n"
            canonical_digest.update(line)
            canonical_bytes += len(line)
            record_count += 1
            if canonical_handle is not None:
                canonical_handle.write(line)

            if record["rights"].get("public_metadata_eligible"):
                public = public_record(record)
                public_record_count += 1
                public_style_ids.add(style_id)
                public_prompt_count += int(bool(public["prompt"].get("text_included")))
                public_media_count += len(public["media"].get("assets") or [])
                current_shard.append(public)
                if len(current_shard) == SHARD_SIZE:
                    flush_shard()
        flush_shard()
    finally:
        if canonical_handle is not None:
            canonical_handle.close()

    if record_count != expected_total:
        raise ValueError(f"Canonical record count drifted: {record_count} != {expected_total}")
    if dict(lane_counts) != expected_lane_counts:
        raise ValueError(f"Canonical lane counts drifted: {dict(lane_counts)}")
    if len(style_ids) != expected_total or len(catalog_keys) != expected_total:
        raise ValueError("Canonical key/style uniqueness invariant failed")
    if len(overlay_asset_ids) != overlay_info["generated_overlay_count"]:
        raise ValueError(
            "Not every generated preview overlay matched a canonical record: "
            f"matched={len(overlay_asset_ids)} source={overlay_info['generated_overlay_count']}"
        )

    public_index: dict[str, Any] = {
        "schema_version": PUBLIC_INDEX_SCHEMA_VERSION,
        "generated_at": generated_at,
        "record_count": public_record_count,
        "canonical_record_count": record_count,
        "shard_size": SHARD_SIZE,
        "shard_count": len(shard_descriptors),
        "style_id_count": len(public_style_ids),
        "prompt_text_included_count": public_prompt_count,
        "media_asset_included_count": public_media_count,
        "rights_policy": {
            "mode": "fail_closed",
            "repository_license_is_not_item_rights_clearance": True,
            "private_research_paths_excluded": True,
            "rights_uncleared_full_prompts_excluded": True,
            "p3_admin_only_records_excluded": True,
            "p4_quarantine_records_excluded": True,
            "release_ineligible_generated_staging_excluded": True,
        },
        "shards": shard_descriptors,
        "records": public_index_rows,
    }
    public_index["content_sha256"] = object_sha256(public_index)
    public_index_bytes = pretty_json_bytes(public_index)

    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": generated_at,
        "record_count": record_count,
        "catalog_key_count": len(catalog_keys),
        "record_id_count": len(record_ids),
        "style_id_count": len(style_ids),
        "lane_counts": dict(sorted(lane_counts.items())),
        "canonical_jsonl": {
            "path": "data/canonical/archive_records.jsonl",
            "bytes": canonical_bytes,
            "sha256": canonical_digest.hexdigest(),
            "line_count": record_count,
        },
        "public_export": {
            "index_path": "data/public-export/catalog-index.json",
            "index_bytes": len(public_index_bytes),
            "index_sha256": hashlib.sha256(public_index_bytes).hexdigest(),
            "index_content_sha256": public_index["content_sha256"],
            "canonical_record_count": record_count,
            "record_count": public_record_count,
            "shard_size": SHARD_SIZE,
            "shard_count": len(shard_descriptors),
            "prompt_text_included_count": public_prompt_count,
            "media_asset_included_count": public_media_count,
            "shards": shard_descriptors,
        },
        "generated_preview_overlay": {
            "source_asset_count": overlay_info["generated_overlay_count"],
            "matched_asset_count": len(overlay_asset_ids),
        },
        "input_sources": input_evidence(opennana_payload),
        "lineage_note": (
            "detailpage_candidates.json contains the 93 selected cases and advertises the complete 529-case "
            "catalog; gpt_image2_cases.json supplies the 529 physical case rows. Both are immutable lineage inputs. "
            "The optional OpenNana lane is a deterministic private projection of durable human approval artifacts."
        ),
        "public_release_boundary": (
            "Public shards contain P1 records and P2 metadata-link-only records. P3 private references and P4 "
            "quarantine records are admin-only and are excluded from every public index and shard."
        ),
    }
    manifest["content_sha256"] = object_sha256(manifest)
    manifest_bytes = pretty_json_bytes(manifest)

    if apply:
        if canonical_temp is None:
            raise AssertionError("Canonical temporary path was not initialized")
        pending.insert(0, (canonical_temp, CANONICAL_PATH))
        stage_bytes(PUBLIC_INDEX_PATH, public_index_bytes, pending)
        stage_bytes(MANIFEST_PATH, manifest_bytes, pending)
        commit_pending(pending)

        expected_shards = {PUBLIC_ROOT / item["path"] for item in shard_descriptors}
        if PUBLIC_SHARDS_DIR.exists():
            root = PUBLIC_SHARDS_DIR.resolve()
            for path in PUBLIC_SHARDS_DIR.glob("catalog-*.json"):
                if path not in expected_shards and path.resolve().parent == root:
                    path.unlink()

    return {
        "mode": "apply" if apply else "dry_run",
        "record_count": record_count,
        "lane_counts": dict(sorted(lane_counts.items())),
        "catalog_key_count": len(catalog_keys),
        "style_id_count": len(style_ids),
        "canonical_jsonl_bytes": canonical_bytes,
        "canonical_jsonl_sha256": canonical_digest.hexdigest(),
        "public_shard_count": len(shard_descriptors),
        "public_record_count": public_record_count,
        "public_index_bytes": len(public_index_bytes),
        "public_prompt_text_included_count": public_prompt_count,
        "public_media_asset_included_count": public_media_count,
        "generated_overlay_count": overlay_info["generated_overlay_count"],
        "generated_overlay_matched_count": len(overlay_asset_ids),
        "outputs_written": apply,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the 18,792-record base archive plus the dynamic private OpenNana lane and rights-safe public shards."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write canonical JSONL, manifest, public catalog index, and 500-record public shards.",
    )
    args = parser.parse_args()
    summary = build(apply=args.apply)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
