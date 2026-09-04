#!/usr/bin/env python3
"""Fail-closed integrity checks for the local image archive canary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
MAX_STATIC_FILE_BYTES = 25 * 1024 * 1024


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def validate() -> dict:
    errors: list[str] = []
    checks: list[dict] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": passed, "detail": detail})
        if not passed:
            errors.append(f"{name}: {detail}")

    required = [
        "README.md",
        "AGENTS.md",
        "platform.config.json",
        "app/index.html",
        "app/styles/app.css",
        "app/scripts/app.js",
        "app/data/featured-five.js",
        "data/canonical/featured_five.json",
        "data/canonical/archive_records.jsonl",
        "data/canonical/archive_records_manifest.json",
        "data/canonical/archive_inventory.json",
        "data/canonical/migration_manifest.json",
        "data/canonical/experiment_migration_manifest.json",
        "data/canonical/content_registry.json",
        "data/private-research/source_locations.json",
        "data/public-export/catalog-index.json",
        "data/public-export/README.md",
        "docs/ARCHITECTURE.md",
        "docs/INVENTORY.md",
        "docs/MIGRATION.md",
        "docs/REFACTOR_STATUS.md",
        "docs/SOURCE_AUTOMATION.md",
        "experiments/README.md",
        "experiments/detail_page_reference_study_v3/prompt_registry.json",
        "deploy/README.md",
        "dist/index.html",
        "dist/archive.html",
        "dist/build-manifest.json",
        "legacy/current_archive/index.html",
        "legacy/current_archive/source-admin.html",
        "legacy/current_archive/approval-requests.html",
        "legacy/current_archive/approval-requests.css",
        "legacy/current_archive/approval-requests.js",
        "legacy/current_archive/duplicate-review.html",
        "legacy/current_archive/duplicate-review.css",
        "legacy/current_archive/duplicate-review.js",
        "legacy/current_archive/opennana-review-data.js",
        "legacy/current_archive/opennana-catalog-data.js",
        "legacy/current_archive/tools/smoke_approval_requests.mjs",
        "data/private-research/opennana/config.json",
        "data/private-research/opennana/state.json",
        "data/private-research/opennana/review_queue/current.json",
        "data/private-research/opennana/archive/opennana_records.json",
        "src/opennana/run_pipeline.py",
        "src/opennana/apply_decisions.py",
        "src/opennana/build_archive_lane.py",
        "src/opennana/build_review_queue_projection.py",
        "qa/validate_opennana_workflow.py",
        "qa/validate_opennana_review_queue.py",
        "src/build_canonical_archive.py",
        "src/build_duplicate_index.py",
        "src/model_routing_policy.py",
        "src/benchmark_media_formats.py",
        "src/benchmark_modern_formats.mjs",
        "src/remote_media_canary.py",
        "src/duplicate_review_store.py",
        "data/private-research/duplicate-analysis/current/duplicate_index.sqlite3",
        "data/private-research/duplicate-analysis/current/summary.json",
        "data/private-research/media-benchmarks/current/featured_format_benchmark.json",
        "data/private-research/media-benchmarks/modern-current/modern_format_benchmark.json",
        "data/private-research/remote-media-canary/current/inventory.json",
        "data/private-research/remote-media-canary/current/latest_run.json",
        "data/private-research/remote-media-canary/current/cache_index.json",
        "qa/test_duplicate_index.py",
        "qa/test_duplicate_review_api.py",
        "qa/test_featured_webp_canary.py",
        "qa/test_remote_media_canary.py",
        "qa/test_model_routing_policy.py",
        "qa/smoke_modern_formats.mjs",
        "qa/modern_format_browser_smoke.json",
        "qa/validate_canonical_archive.py",
        "src/build_cloudflare_public_frontend.py",
        "deploy/cloudflare-public/deployment-manifest.json",
        "deploy/cloudflare-public/release-record.json",
        "deploy/cloudflare-public/wrangler.jsonc",
        "deploy/cloudflare-public/worker/index.js",
        "qa/test_cloudflare_public_worker.mjs",
    ]
    missing = [rel for rel in required if not (ROOT / rel).is_file()]
    check("required_files", not missing, "missing=" + ", ".join(missing) if missing else "all present")

    config_path = ROOT / "platform.config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    deployment = config.get("deployment", {})
    public_mvp = deployment.get("public_mvp", {})
    public_manifest_path = ROOT / str(public_mvp.get("deployment_manifest_path", ""))
    public_release_path = ROOT / str(public_mvp.get("release_record_path", ""))
    public_manifest = (
        json.loads(public_manifest_path.read_text(encoding="utf-8"))
        if public_manifest_path.is_file()
        else {}
    )
    public_release = (
        json.loads(public_release_path.read_text(encoding="utf-8"))
        if public_release_path.is_file()
        else {}
    )
    blocked_contract = (
        deployment.get("status") == "blocked"
        and deployment.get("publish_authorized") is False
    )
    public_mvp_contract = (
        deployment.get("status") == "public_mvp_deployed"
        and deployment.get("publish_authorized") is True
        and deployment.get("authorization_scope") == "public-awesome-gpt-image-2-mvp-529-only"
        and deployment.get("full_archive_status") == "blocked"
        and public_mvp.get("status") == "deployed"
        and public_mvp.get("public_record_count") == 529
        and public_mvp.get("private_data_included") is False
        and public_mvp.get("admin_data_included") is False
        and public_manifest.get("version_id") == public_mvp.get("worker_version_id")
        and public_manifest.get("bundle", {}).get("public_record_count") == 529
        and public_manifest.get("public_boundary", {}).get("private_data_included") is False
        and public_release.get("state") == "released"
        and public_release.get("decision") == "approved"
        and public_release.get("eligible_for_release") is True
        and public_release.get("artifact_sha256")
        == public_manifest.get("bundle", {}).get("bundle_sha256")
    )
    check(
        "deployment_authorization_contract",
        blocked_contract or public_mvp_contract,
        (
            f"status={deployment.get('status')}, "
            f"publish_authorized={deployment.get('publish_authorized')}, "
            f"scope={deployment.get('authorization_scope')}, "
            f"full_archive_status={deployment.get('full_archive_status')}"
        ),
    )
    media_delivery = config.get("media_delivery", {})
    check(
        "compressed_dist_policy",
        media_delivery.get("preferred_format") == "webp"
        and media_delivery.get("preferred_format_for_future_public_bundle") == "webp"
        and media_delivery.get("avif_policy") == "private_benchmark_only_not_emitted_to_public_bundle_v1"
        and media_delivery.get("dist_includes_original_source_media") is False,
        (
            f"preferred={media_delivery.get('preferred_format')}, "
            f"future={media_delivery.get('preferred_format_for_future_public_bundle')}, "
            f"dist_originals={media_delivery.get('dist_includes_original_source_media')}"
        ),
    )
    analysis_routing = config.get("analysis_routing", {})
    tier_names = [tier.get("name") for tier in analysis_routing.get("tiers", []) if isinstance(tier, dict)]
    check(
        "analysis_routing_policy",
        analysis_routing.get("goal") == "deterministic_first_minimum_model_cost"
        and tier_names[:2] == ["deterministic_pre_filter", "heuristic_family_grouping"]
        and analysis_routing.get("analyze_unique_hashes_only") is True
        and analysis_routing.get("sol_batch_allowed") is False,
        f"tiers={tier_names}",
    )
    rights_access = config.get("rights_access_policy", {})
    rights_tiers = rights_access.get("tiers") or {}
    check(
        "rights_access_policy",
        rights_access.get("status") == "active_fail_closed"
        and rights_access.get("public_tiers") == ["P1", "P2"]
        and rights_access.get("admin_only_tiers") == ["P3", "P4"]
        and (rights_tiers.get("P3") or {}).get("portfolio_visibility") == "admin_only"
        and (rights_tiers.get("P3") or {}).get("admin_usage_status") == "reference_allowed"
        and (rights_tiers.get("P4") or {}).get("portfolio_visibility") == "admin_only"
        and (rights_tiers.get("P4") or {}).get("admin_usage_status") == "quarantine_only"
        and rights_access.get("unknown_is_permission") is False,
        f"policy={rights_access}",
    )

    canonical_manifest_path = ROOT / "data" / "canonical" / "archive_records_manifest.json"
    canonical_manifest = (
        json.loads(canonical_manifest_path.read_text(encoding="utf-8"))
        if canonical_manifest_path.is_file()
        else {}
    )
    canonical_public = canonical_manifest.get("public_export", {})
    opennana_archive_path = ROOT / "data" / "private-research" / "opennana" / "archive" / "opennana_records.json"
    opennana_archive = (
        json.loads(opennana_archive_path.read_text(encoding="utf-8"))
        if opennana_archive_path.is_file()
        else {}
    )
    opennana_records = opennana_archive.get("records") if isinstance(opennana_archive.get("records"), list) else []
    expected_canonical_count = canonical_manifest.get("record_count")
    expected_shard_count = canonical_public.get("shard_count")
    canonical_manifest_counts_consistent = (
        isinstance(expected_canonical_count, int)
        and canonical_manifest.get("catalog_key_count") == expected_canonical_count
        and canonical_manifest.get("record_id_count") == expected_canonical_count
        and canonical_manifest.get("style_id_count") == expected_canonical_count
        and canonical_public.get("canonical_record_count") == expected_canonical_count
        and isinstance(canonical_public.get("record_count"), int)
        and 0 <= canonical_public.get("record_count") <= expected_canonical_count
        and isinstance(expected_shard_count, int)
        and expected_shard_count == (canonical_public.get("record_count") + 499) // 500
    )
    check(
        "canonical_export_contract",
        canonical_manifest_counts_consistent,
        (
            f"records={canonical_manifest.get('record_count')}, "
            f"shards={canonical_public.get('shard_count')}"
        ),
    )
    inventory_path = ROOT / "data" / "canonical" / "archive_inventory.json"
    inventory = (
        json.loads(inventory_path.read_text(encoding="utf-8"))
        if inventory_path.is_file()
        else {}
    )
    inventory_record_model = inventory.get("record_model") or {}
    inventory_storage = inventory.get("storage") or {}
    check(
        "archive_inventory_matches_canonical",
        canonical_manifest_counts_consistent
        and inventory_record_model.get("displayed_total") == expected_canonical_count
        and inventory_storage.get("canonical_record_count") == expected_canonical_count
        and inventory_storage.get("public_shard_count") == expected_shard_count
        and (inventory_record_model.get("components") or {}).get("opennana") == len(opennana_records),
        (
            f"inventory={inventory_record_model.get('displayed_total')}, "
            f"canonical={expected_canonical_count}, opennana={len(opennana_records)}"
        ),
    )
    opennana_projection_path = ROOT / "legacy" / "current_archive" / "opennana-catalog-data.js"
    opennana_projection_text = (
        opennana_projection_path.read_text(encoding="utf-8")
        if opennana_projection_path.is_file()
        else ""
    )
    opennana_ids_valid = all(
        item.get("record_id") == f"OPENNANA-{item.get('upstream_id')}"
        and item.get("reference_style_id") == f"ONN-{item.get('upstream_id')}"
        and item.get("release_eligible") is False
        and (item.get("rights") or {}).get("release_eligible") is False
        and (item.get("rights") or {}).get("prompt_publication_eligible") is False
        and (item.get("rights") or {}).get("media_publication_eligible") is False
        for item in opennana_records
    )
    check(
        "opennana_internal_archive_contract",
        opennana_archive.get("schema_version") == "opennana-internal-archive-1.0"
        and opennana_archive.get("record_count") == len(opennana_records)
        and opennana_archive.get("public_release_eligible") is False
        and opennana_ids_valid
        and "window.DETAILPAGE_OPENNANA_RECORDS" in opennana_projection_text,
        f"records={len(opennana_records)}, ids_valid={opennana_ids_valid}",
    )
    check(
        "canonical_public_policy_boundary",
        isinstance(canonical_public.get("prompt_text_included_count"), int)
        and canonical_public.get("prompt_text_included_count") >= 0
        and canonical_public.get("prompt_text_included_count") <= canonical_public.get("record_count", -1)
        and isinstance(canonical_public.get("media_asset_included_count"), int)
        and canonical_public.get("media_asset_included_count") >= 0,
        (
            f"prompts={canonical_public.get('prompt_text_included_count')}, "
            f"media={canonical_public.get('media_asset_included_count')}"
        ),
    )

    duplicate_summary_path = ROOT / "data" / "private-research" / "duplicate-analysis" / "current" / "summary.json"
    duplicate_db_path = duplicate_summary_path.with_name("duplicate_index.sqlite3")
    duplicate_summary = (
        json.loads(duplicate_summary_path.read_text(encoding="utf-8"))
        if duplicate_summary_path.is_file()
        else {}
    )
    duplicate_counts = duplicate_summary.get("counts") or {}
    duplicate_analysis = duplicate_summary.get("analysis") or {}
    duplicate_policy = duplicate_analysis.get("policy") or {}
    duplicate_canonical = duplicate_summary.get("canonical") or {}
    duplicate_artifacts = duplicate_summary.get("artifacts") or {}
    duplicate_db_meta = duplicate_artifacts.get("sqlite") or {}
    duplicate_db_hash_ok = (
        duplicate_db_path.is_file()
        and duplicate_db_meta.get("bytes") == duplicate_db_path.stat().st_size
        and duplicate_db_meta.get("sha256") == sha256(duplicate_db_path)
    )
    check(
        "duplicate_analysis_contract",
        duplicate_summary.get("schema_version") == "duplicate-analysis-summary-1.0"
        and duplicate_counts.get("records_indexed") == expected_canonical_count
        and duplicate_counts.get("groups_total") == sum((duplicate_counts.get("groups_by_kind") or {}).values())
        and duplicate_canonical.get("sha256") == canonical_manifest.get("canonical_jsonl", {}).get("sha256")
        and duplicate_policy.get("automatic_merge_or_delete") is False
        and duplicate_policy.get("base64_stored") is False
        and duplicate_policy.get("filesystem_paths_stored") is False
        and duplicate_policy.get("source_records_immutable") is True
        and duplicate_db_hash_ok,
        (
            f"records={duplicate_counts.get('records_indexed')}, "
            f"groups={duplicate_counts.get('groups_total')}, db_hash_ok={duplicate_db_hash_ok}"
        ),
    )
    duplicate_thumbnails = list((ROOT / "media" / "derived" / "duplicate-review").glob("*.webp"))
    expected_duplicate_thumbnails = int(((duplicate_artifacts.get("thumbnails") or {}).get("count") or 0))
    check(
        "duplicate_webp_derivatives",
        len(duplicate_thumbnails) == expected_duplicate_thumbnails
        and sum(path.stat().st_size for path in duplicate_thumbnails)
        == int(((duplicate_artifacts.get("thumbnails") or {}).get("bytes") or -1)),
        f"count={len(duplicate_thumbnails)}, expected={expected_duplicate_thumbnails}",
    )
    canonical_schema_path = REPO / "00_CORE" / "schemas" / "image_archive_record.schema.json"
    check(
        "canonical_schema_registered",
        canonical_schema_path.is_file(),
        f"path={canonical_schema_path}",
    )

    benchmark_path = ROOT / "data" / "private-research" / "media-benchmarks" / "current" / "featured_format_benchmark.json"
    benchmark_payload = json.loads(benchmark_path.read_text(encoding="utf-8")) if benchmark_path.is_file() else {}
    benchmark_runtime = benchmark_payload.get("runtime_support") or {}
    benchmark_totals = benchmark_payload.get("totals") or {}
    benchmark_comparison = benchmark_totals.get("comparison") if isinstance(benchmark_totals.get("comparison"), list) else []
    check(
        "media_benchmark_contract",
        benchmark_payload.get("schema_version") == "media-format-benchmark-1.0"
        and benchmark_runtime.get("webp") is True
        and any(item.get("variant") == "webp_q82" for item in benchmark_comparison),
        f"comparison_count={len(benchmark_comparison)}",
    )
    modern_benchmark_path = (
        ROOT
        / "data"
        / "private-research"
        / "media-benchmarks"
        / "modern-current"
        / "modern_format_benchmark.json"
    )
    modern_benchmark = (
        json.loads(modern_benchmark_path.read_text(encoding="utf-8"))
        if modern_benchmark_path.is_file()
        else {}
    )
    modern_support = modern_benchmark.get("format_support") or {}
    modern_aggregate = modern_benchmark.get("aggregate") or {}
    check(
        "modern_media_benchmark_contract",
        modern_benchmark.get("schema_version") == "modern-image-format-benchmark-1.0"
        and modern_support.get("avif") is True
        and modern_support.get("webp") is True
        and modern_support.get("jxl") is False
        and int((modern_aggregate.get("avif_q55") or {}).get("total_bytes") or 0) > 0,
        f"support={modern_support}",
    )
    modern_browser_path = ROOT / "qa" / "modern_format_browser_smoke.json"
    modern_browser = (
        json.loads(modern_browser_path.read_text(encoding="utf-8")) if modern_browser_path.is_file() else {}
    )
    check(
        "modern_format_browser_decode",
        modern_browser.get("requested") == 15
        and modern_browser.get("decoded") == 15
        and modern_browser.get("failures") == [],
        f"requested={modern_browser.get('requested')}, decoded={modern_browser.get('decoded')}",
    )
    remote_inventory_path = ROOT / "data" / "private-research" / "remote-media-canary" / "current" / "inventory.json"
    remote_latest_path = ROOT / "data" / "private-research" / "remote-media-canary" / "current" / "latest_run.json"
    remote_cache_path = ROOT / "data" / "private-research" / "remote-media-canary" / "current" / "cache_index.json"
    remote_inventory = json.loads(remote_inventory_path.read_text(encoding="utf-8")) if remote_inventory_path.is_file() else {}
    remote_latest = json.loads(remote_latest_path.read_text(encoding="utf-8")) if remote_latest_path.is_file() else {}
    remote_cache = json.loads(remote_cache_path.read_text(encoding="utf-8")) if remote_cache_path.is_file() else {}
    check(
        "remote_media_canary_contract",
        remote_inventory.get("schema_version") == "remote-media-canary-inventory-1.0"
        and remote_latest.get("schema_version") == "remote-media-canary-run-1.0"
        and remote_cache.get("schema_version") == "remote-media-cache-index-1.0",
        (
            f"inventory={remote_inventory.get('schema_version')}, "
            f"latest={remote_latest.get('schema_version')}, cache={remote_cache.get('schema_version')}"
        ),
    )
    serialized_remote_artifacts = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (remote_latest_path, remote_cache_path)
        if path.is_file()
    )
    check(
        "remote_signed_query_not_persisted",
        "X-Amz-Signature=" not in serialized_remote_artifacts
        and "X-Amz-Credential=" not in serialized_remote_artifacts,
        "signed query parameters must not be persisted",
    )

    collection_path = ROOT / "data" / "canonical" / "featured_five.json"
    collection = json.loads(collection_path.read_text(encoding="utf-8")) if collection_path.is_file() else {}
    items = collection.get("items", [])
    check("featured_count", len(items) == 5, f"count={len(items)}")
    ids = [item.get("reference_style_id") for item in items]
    check("featured_unique_ids", len(ids) == len(set(ids)), f"ids={ids}")
    check("featured_rank_sequence", [item.get("rank") for item in items] == [1, 2, 3, 4, 5], "ranks must be 1..5")
    policy = collection.get("selection_policy", {})
    check(
        "human_selection_gate",
        policy.get("human_selection_required") is True
        and policy.get("generation_after_selection_only") is True
        and collection.get("selection_status") == "awaiting_human_decision",
        f"selection_status={collection.get('selection_status')}",
    )

    for item in items:
        style_id = item.get("reference_style_id", "unknown")
        image_path = ROOT / str(item.get("image_path", ""))
        legacy_path = ROOT / str(item.get("legacy_image_path", ""))
        expected_hash = item.get("image_sha256")
        safe = is_within(image_path, ROOT) and is_within(legacy_path, ROOT)
        present = image_path.is_file() and legacy_path.is_file()
        hashes_match = present and sha256(image_path) == expected_hash and sha256(legacy_path) == expected_hash
        check(f"featured_asset_{style_id}", safe and present and hashes_match, f"safe={safe}, present={present}, hash_match={hashes_match}")
        review_status = str(item.get("review_status", ""))
        check(
            f"release_flag_{style_id}",
            item.get("release_eligible") is False and "review" in review_status,
            f"candidate must remain review-only; status={review_status}",
        )

    public_root = ROOT / "media" / "public" / "featured"
    public_originals = [public_root / str(item.get("image_path", "")).split("media/public/featured/")[-1] for item in items]
    public_webp = list(public_root.glob("*.webp"))
    public_fallbacks = list(public_root.glob("*.fallback.*"))
    check(
        "public_featured_has_originals_and_compressed_derivatives",
        all(path.is_file() for path in public_originals) and len(public_webp) == 5 and len(public_fallbacks) == 5,
        f"originals={sum(1 for path in public_originals if path.is_file())}, webp={len(public_webp)}, fallback={len(public_fallbacks)}",
    )

    dist = ROOT / "dist"
    dist_files = [path for path in dist.rglob("*") if path.is_file()] if dist.is_dir() else []
    oversized = [path.relative_to(ROOT).as_posix() for path in dist_files if path.stat().st_size > MAX_STATIC_FILE_BYTES]
    forbidden = [
        path.relative_to(ROOT).as_posix()
        for path in dist_files
        if path.suffix.lower() in {".sqlite", ".sqlite3", ".db", ".env"}
    ]
    dist_featured = dist / "media" / "public" / "featured"
    dist_originals = [dist_featured / Path(str(item.get("image_path", ""))).name for item in items]
    dist_webp = list(dist_featured.glob("*.webp"))
    dist_fallbacks = list(dist_featured.glob("*.fallback.*"))
    check("dist_file_size_boundary", not oversized, f"oversized={oversized}")
    check("dist_private_file_boundary", not forbidden, f"forbidden={forbidden}")
    check(
        "dist_has_compressed_featured_derivatives_only",
        not any(path.exists() for path in dist_originals) and len(dist_webp) == 5 and len(dist_fallbacks) == 5,
        f"originals={sum(1 for path in dist_originals if path.exists())}, webp={len(dist_webp)}, fallback={len(dist_fallbacks)}",
    )

    dist_index = dist / "index.html"
    dist_index_text = dist_index.read_text(encoding="utf-8") if dist_index.is_file() else ""
    check(
        "dist_is_self_contained",
        'data-platform-root="."' in dist_index_text
        and "../legacy/current_archive" not in dist_index_text
        and 'href="./archive.html"' in dist_index_text,
        "dist must not link outside its static root",
    )

    manifest_path = dist / "build-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    check(
        "dist_manifest_review_only",
        manifest.get("release_eligible") is False and manifest.get("build_kind") == "local_review_canary",
        f"build_kind={manifest.get('build_kind')}",
    )
    featured_media_summary = manifest.get("featured_media_summary") or {}
    check(
        "featured_webp_savings_recorded",
        featured_media_summary.get("derivative_count") == 10
        and float(featured_media_summary.get("savings_total_pct") or 0) >= 70
        and featured_media_summary.get("fallback_format") == "mixed"
        and featured_media_summary.get("preferred_format") == "webp",
        (
            f"derivatives={featured_media_summary.get('derivative_count')}, "
            f"savings={featured_media_summary.get('savings_total_pct')}"
        ),
    )
    projection = ROOT / "app" / "data" / "featured-five.js"
    projection_text = projection.read_text(encoding="utf-8") if projection.is_file() else ""
    for item in items:
        source_name = Path(str(item.get("image_path", ""))).stem
        matches = sorted(dist_featured.glob(f"{source_name}.fallback.*"))
        dist_copy = matches[0] if len(matches) == 1 else dist_featured
        expected = None
        if len(matches) == 1:
            projection_match = re.search(
                rf'"reference_style_id":\s*"{re.escape(str(item.get("reference_style_id") or ""))}".*?"delivery_fallback_sha256":\s*"([0-9a-f]{{64}})"',
                projection_text,
                re.DOTALL,
            )
            if projection_match:
                expected = projection_match.group(1)
        check(
            f"dist_asset_{item.get('reference_style_id')}",
            len(matches) == 1 and dist_copy.is_file() and expected is not None and sha256(dist_copy) == expected,
            f"path={dist_copy.relative_to(ROOT) if is_within(dist_copy, ROOT) else dist_copy}",
        )

    missing_ids = [style_id for style_id in ids if style_id and style_id not in projection_text]
    check("app_projection_ids", not missing_ids, f"missing_ids={missing_ids}")

    old_path = REPO / "Reports" / "2026-08-25-01_상세페이지_프롬프트전수조사"
    legacy = ROOT / "legacy" / "current_archive"
    same_location = False
    try:
        same_location = old_path.exists() and legacy.exists() and os.path.samefile(old_path, legacy)
    except OSError:
        same_location = False
    check("legacy_compatibility_junction", same_location, f"old_path={old_path}")

    old_experiment = REPO / "runtime" / "detail_page_reference_study_v3"
    new_experiment = ROOT / "experiments" / "detail_page_reference_study_v3"
    same_experiment_location = False
    try:
        same_experiment_location = (
            old_experiment.exists()
            and new_experiment.exists()
            and os.path.samefile(old_experiment, new_experiment)
        )
    except OSError:
        same_experiment_location = False
    check(
        "experiment_compatibility_junction",
        same_experiment_location,
        f"old_path={old_experiment}",
    )

    experiment_manifest_path = ROOT / "data" / "canonical" / "experiment_migration_manifest.json"
    experiment_manifest = (
        json.loads(experiment_manifest_path.read_text(encoding="utf-8"))
        if experiment_manifest_path.is_file()
        else {}
    )
    experiment_observation = experiment_manifest.get("move_time_observation", {})
    check(
        "experiment_migration_verified",
        experiment_manifest.get("status") == "verified"
        and experiment_observation.get("file_count_before") == 44
        and experiment_observation.get("file_count_after") == 44
        and experiment_observation.get("counts_bytes_and_hashes_match") is True
        and experiment_manifest.get("deployment_included") is False,
        f"status={experiment_manifest.get('status')}",
    )

    root_agents = REPO / "AGENTS.md"
    root_agents_text = root_agents.read_text(encoding="utf-8") if root_agents.is_file() else ""
    check("future_agent_routing", "08_AGENT_이미지_아카이브/" in root_agents_text, "root AGENTS.md routes image help here")

    registry_path = ROOT / "data" / "canonical" / "content_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.is_file() else {}
    registry_items = registry.get("items", [])
    registry_errors: list[str] = []
    registry_ids: list[str] = []
    public_release_link_ids = {
        "IMG-ARCHIVE-CLOUDFLARE-PUBLIC-MANIFEST-001",
        "IMG-ARCHIVE-CLOUDFLARE-PUBLIC-RELEASE-001",
        "IMG-ARCHIVE-CLOUDFLARE-PUBLIC-HANDOFF-001",
    }
    public_release_record_id = "REL-IMAGE-ARCHIVE-CF-PUBLIC-20260903-01"
    for item in registry_items if isinstance(registry_items, list) else []:
        content_id = str(item.get("content_id") or "")
        registry_ids.append(content_id)
        artifact_value = str(item.get("artifact_path") or "")
        artifact_path = (ROOT / artifact_value).resolve() if artifact_value else ROOT
        if not content_id or not artifact_value:
            registry_errors.append(f"{content_id or '<empty>'}: required field")
            continue
        if not is_within(artifact_path, REPO) or not artifact_path.is_file():
            registry_errors.append(f"{content_id}: artifact path")
            continue
        expected_hash = item.get("artifact_sha256")
        if expected_hash is None and content_id != "IMG-ARCHIVE-STRUCTURE-QA-001":
            registry_errors.append(f"{content_id}: missing static artifact hash")
        elif expected_hash is not None and sha256(artifact_path) != expected_hash:
            registry_errors.append(f"{content_id}: artifact hash")
        release_record_id = item.get("release_record_id")
        release_link_allowed = (
            public_mvp_contract
            and content_id in public_release_link_ids
            and release_record_id == public_release_record_id
        )
        if item.get("status") != "needs_review" or (
            release_record_id is not None and not release_link_allowed
        ):
            registry_errors.append(f"{content_id}: release boundary")
    check(
        "content_registry_integrity",
        isinstance(registry_items, list)
        and bool(registry_items)
        and len(registry_ids) == len(set(registry_ids))
        and not registry_errors,
        f"items={len(registry_ids)}, errors={registry_errors}",
    )
    dynamic_report = next(
        (item for item in registry_items if item.get("content_id") == "IMG-ARCHIVE-STRUCTURE-QA-001"),
        {},
    ) if isinstance(registry_items, list) else {}
    check(
        "dynamic_validation_report_not_self_hashed",
        dynamic_report.get("artifact_path") == "qa/latest_validation.json"
        and dynamic_report.get("artifact_sha256") is None,
        "self-updating validation report must keep a null hash",
    )

    location_map_path = ROOT / "data" / "private-research" / "source_locations.json"
    location_map = json.loads(location_map_path.read_text(encoding="utf-8")) if location_map_path.is_file() else {}
    registered_paths = [
        str(item.get("path") or "")
        for item in location_map.get("locations", [])
        if isinstance(item, dict)
    ]
    registered_paths.extend(str(path) for path in location_map.get("shared_tools", []))
    registered_paths.extend(str(path) for path in location_map.get("historical_reports", []))
    missing_registered_paths = [
        path
        for path in registered_paths
        if not path or not is_within(REPO / path, REPO) or not (REPO / path).exists()
    ]
    check(
        "registered_external_locations_exist",
        bool(registered_paths) and not missing_registered_paths,
        f"registered={len(registered_paths)}, missing={missing_registered_paths}",
    )

    return {
        "schema_version": "1.0.0",
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "status": "passed" if not errors else "failed",
        "check_count": len(checks),
        "error_count": len(errors),
        "errors": errors,
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args()
    report = validate()
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.write_report:
        destination = ROOT / "qa" / "latest_validation.json"
        destination.write_text(rendered + "\n", encoding="utf-8", newline="\n")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
