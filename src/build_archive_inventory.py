from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_ROOT = PLATFORM_ROOT / "legacy" / "current_archive"
OUTPUT_PATH = PLATFORM_ROOT / "data" / "canonical" / "archive_inventory.json"
CANONICAL_RECORDS_PATH = PLATFORM_ROOT / "data" / "canonical" / "archive_records.jsonl"
CANONICAL_MANIFEST_PATH = PLATFORM_ROOT / "data" / "canonical" / "archive_records_manifest.json"
PUBLIC_SHARDS_PATH = PLATFORM_ROOT / "data" / "public-export" / "shards"
OPENNANA_ARCHIVE_PATH = (
    PLATFORM_ROOT / "data" / "private-research" / "opennana" / "archive" / "opennana_records.json"
)
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".svg"}
REMOTE_PREFIXES = ("http://", "https://", "data:")


def load_json(name: str) -> Any:
    return json.loads((ARCHIVE_ROOT / name).read_text(encoding="utf-8-sig"))


def load_js_assignment(name: str) -> Any:
    text = (ARCHIVE_ROOT / name).read_text(encoding="utf-8-sig")
    start = text.index("=") + 1
    return json.JSONDecoder().raw_decode(text[start:].lstrip())[0]


def load_json_path(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def records(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def first_image(record: dict[str, Any]) -> str | None:
    for key in ("local_image_path", "image_url", "image", "thumbnail_url", "thumbnail"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    asset = record.get("asset")
    if isinstance(asset, dict):
        value = asset.get("local_path")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def resolve_local(value: str) -> Path:
    return (ARCHIVE_ROOT / value).resolve()


def classify_path(value: str | None) -> str:
    if not value:
        return "missing"
    if value.startswith(REMOTE_PREFIXES):
        return "remote"
    return "local" if resolve_local(value).is_file() else "broken"


def image_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES]


def all_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


def byte_total(paths: Iterable[Path]) -> int:
    return sum(path.stat().st_size for path in paths)


def generated_preview_path(record: dict[str, Any]) -> str | None:
    value = record.get("generated_preview_path")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return first_image(record)


def build_inventory() -> dict[str, Any]:
    legacy = records(load_json("gpt_image2_cases.json"), "cases")
    external = records(load_json("external_prompt_records.json"), "records")
    manual_raw = records(load_json("manual_prompt_records.json"), "records")
    social = records(load_json("social_prompt_records.json"), "records")
    secret = records(load_json("secret_code_records.json"), "records")
    bul = records(load_json("bul001_template_collection.json"), "templates")
    opennana = records(load_json_path(OPENNANA_ARCHIVE_PATH, {}), "records")
    generated = records(load_js_assignment("generated-preview-assets.js"))

    manual = [
        row
        for row in manual_raw
        if str(row.get("source_id") or "") != "secret-code-notion84"
        and str((row.get("raw_metadata") or {}).get("archive_group") or "") != "secret_codes"
    ]

    generated_lookup: dict[str, str] = {}
    for row in generated:
        path = row.get("local_image_path")
        if not isinstance(path, str) or not path.strip():
            continue
        for key in (row.get("record_id"), row.get("reference_style_id")):
            if key:
                generated_lookup[str(key)] = path.strip()

    lane_paths: dict[str, list[str | None]] = {
        "legacy": [first_image(row) for row in legacy],
        "external": [],
        "manual": [first_image(row) for row in manual],
        "social": [first_image(row) for row in social],
        "secret_codes": [first_image(row) for row in secret],
        "opennana": [first_image(row) for row in opennana],
        "bul001": [generated_preview_path(row) for row in bul],
    }
    for row in external:
        override = generated_lookup.get(str(row.get("record_id") or "")) or generated_lookup.get(
            str(row.get("reference_style_id") or "")
        )
        lane_paths["external"].append(override or first_image(row))

    lane_counts = {
        "legacy": len(legacy),
        "external": len(external),
        "manual": len(manual),
        "social": len(social),
        "secret_codes": len(secret),
        "opennana": len(opennana),
        "bul001": len(bul),
    }
    asset_states: dict[str, dict[str, int]] = {}
    totals = {"local": 0, "remote": 0, "broken": 0, "missing": 0}
    referenced_local_paths: set[Path] = set()
    for lane, paths in lane_paths.items():
        counts = {"local": 0, "remote": 0, "broken": 0, "missing": 0}
        for value in paths:
            state = classify_path(value)
            counts[state] += 1
            totals[state] += 1
            if state == "local" and value:
                referenced_local_paths.add(resolve_local(value))
        asset_states[lane] = counts

    # Hidden superseded manual rows remain physical evidence and still count as
    # referenced storage even though they are not rendered as separate cards.
    for row in manual_raw:
        value = first_image(row)
        if value and classify_path(value) == "local":
            referenced_local_paths.add(resolve_local(value))

    asset_image_files = image_files(ARCHIVE_ROOT / "assets" / "images")
    staging_image_files = image_files(ARCHIVE_ROOT / "assets" / "generated_staging")
    archive_image_files = image_files(ARCHIVE_ROOT)
    archive_files = all_files(ARCHIVE_ROOT)
    platform_files = all_files(PLATFORM_ROOT)
    staging_referenced = {path for path in referenced_local_paths if path.is_relative_to(ARCHIVE_ROOT / "assets" / "generated_staging")}

    external_kind_counts: dict[str, int] = {}
    for row in external:
        kind = str(row.get("record_kind") or "unclassified")
        external_kind_counts[kind] = external_kind_counts.get(kind, 0) + 1

    displayed_total = sum(lane_counts.values())
    if sum(totals.values()) != displayed_total:
        raise RuntimeError("Asset-state totals do not match displayed record count")

    canonical_manifest = (
        json.loads(CANONICAL_MANIFEST_PATH.read_text(encoding="utf-8"))
        if CANONICAL_MANIFEST_PATH.is_file()
        else {}
    )
    canonical_record_count = canonical_manifest.get("record_count")
    if isinstance(canonical_record_count, int) and canonical_record_count != displayed_total:
        raise RuntimeError(
            "Displayed record count does not match canonical manifest: "
            f"displayed={displayed_total}, canonical={canonical_record_count}"
        )
    canonical_export_complete = (
        CANONICAL_RECORDS_PATH.is_file()
        and canonical_record_count == displayed_total
        and canonical_manifest.get("catalog_key_count") == displayed_total
        and canonical_manifest.get("style_id_count") == displayed_total
    )
    public_shard_count = len(list(PUBLIC_SHARDS_PATH.glob("catalog-*.json"))) if PUBLIC_SHARDS_PATH.is_dir() else 0

    projections = []
    for name in (
        "external_prompt_records.json",
        "external_prompt_records.jsonl",
        "external_prompt_records.csv",
        "external-catalog-data.js",
        "full_prompt_library.sqlite3",
    ):
        path = ARCHIVE_ROOT / name
        projections.append(
            {
                "path": str(path.relative_to(PLATFORM_ROOT)).replace("\\", "/"),
                "exists": path.is_file(),
                "bytes": path.stat().st_size if path.is_file() else None,
            }
        )

    completed_steps = [
        "platform root established",
        "legacy archive moved under platform root",
        "compatibility junctions retained",
        f"full dashboard serves {displayed_total:,} logical records",
        "private research and public media boundaries established",
    ]
    remaining_steps = [
        "cut the browser over from legacy JavaScript projections to canonical public shards",
        "promote only rights-cleared or internally generated media to R2/public",
        "classify unreferenced staging files without deleting evidence",
    ]
    if canonical_export_complete:
        completed_steps.extend(
            [
                f"{displayed_total:,} records normalized into one versioned canonical JSONL",
                f"rights-filtered public metadata index and {public_shard_count} static shards generated",
            ]
        )
    else:
        remaining_steps[:0] = [
            f"promote all {displayed_total:,} records into one versioned canonical schema",
            "generate a rights-filtered public static index",
        ]

    return {
        "schema_version": "1.0.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "observed_local_inventory",
        "record_model": {
            "displayed_total": displayed_total,
            "components": lane_counts,
            "manual_raw_count": len(manual_raw),
            "manual_hidden_secret_code_duplicates": len(manual_raw) - len(manual),
            "external_record_kinds": dict(sorted(external_kind_counts.items())),
            "note": f"{displayed_total:,} is a logical record count, not a filesystem file count.",
        },
        "asset_state": {
            "totals": totals,
            "by_component": asset_states,
            "note": "Remote means a URL is declared; reachability and reuse rights are not implied.",
        },
        "physical_inventory": {
            "platform_root": {
                "file_count": len(platform_files),
                "bytes": byte_total(platform_files),
            },
            "legacy_archive": {
                "file_count": len(archive_files),
                "bytes": byte_total(archive_files),
                "image_file_count": len(archive_image_files),
                "image_bytes": byte_total(archive_image_files),
            },
            "legacy_assets": {
                "source_image_files": len(asset_image_files),
                "source_image_bytes": byte_total(asset_image_files),
                "generated_staging_files": len(staging_image_files),
                "generated_staging_bytes": byte_total(staging_image_files),
                "generated_staging_referenced_unique": len(staging_referenced),
                "generated_staging_unreferenced": len(set(staging_image_files) - staging_referenced),
            },
        },
        "storage": {
            "canonical_primary_record_file": "data/canonical/archive_records.jsonl",
            "canonical_record_count": canonical_record_count,
            "canonical_manifest_file": "data/canonical/archive_records_manifest.json",
            "public_shard_count": public_shard_count,
            "external_primary_record_file": "legacy/current_archive/external_prompt_records.json",
            "external_record_count": len(external),
            "generated_overlay_count": len(generated),
            "projections": projections,
            "policy": "Keep one authoritative machine store and treat JSONL, CSV, JS, SQLite, and static shards as generated projections.",
        },
        "refactor_status": {
            "phase": "phase_2a_canonical_export_complete" if canonical_export_complete else "phase_1_safe_move_complete",
            "canonical_record_export_complete": canonical_export_complete,
            "frontend_canonical_cutover_complete": False,
            "full_canonical_migration_complete": False,
            "completed": completed_steps,
            "remaining": remaining_steps,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a reproducible inventory for the image archive platform.")
    parser.add_argument("--apply", action="store_true", help="Write data/canonical/archive_inventory.json")
    args = parser.parse_args()
    inventory = build_inventory()
    if args.apply:
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"mode": "apply" if args.apply else "dry_run", **inventory}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
