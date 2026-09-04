from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

try:
    from .collect import is_paid_or_locked
    from .common import DATA_ROOT, atomic_write_json, normalize_text, prompt_parts, read_json, safe_url, sha256_text, source_id, stable_json, template_text
except ImportError:
    from collect import is_paid_or_locked
    from common import DATA_ROOT, atomic_write_json, normalize_text, prompt_parts, read_json, safe_url, sha256_text, source_id, stable_json, template_text


def unwrap_author(value: Any) -> str | None:
    if isinstance(value, str):
        return normalize_text(value) or None
    if isinstance(value, dict):
        for key in ("name", "username", "display_name", "handle"):
            if value.get(key):
                return normalize_text(value[key])
    return None


def string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    result = {normalize_text(item) for item in value if isinstance(item, str) and normalize_text(item)}
    return sorted(result, key=str.casefold)


def image_urls(record: dict[str, Any]) -> list[str]:
    candidates: list[Any] = [record.get("cover_image_url"), record.get("image_url"), record.get("thumbnail_url")]
    for key in ("images", "media"):
        value = record.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    candidates.append(item)
                elif isinstance(item, dict):
                    candidates.extend([item.get("url"), item.get("src"), item.get("image_url")])
    return sorted({url for item in candidates if (url := safe_url(item))})


def normalize_free_detail(record: dict[str, Any], observed_at: str) -> dict[str, Any]:
    if is_paid_or_locked(record):
        raise ValueError(f"paid or locked detail reached normalizer: {source_id(record)}")
    parts = prompt_parts(record.get("prompts", record.get("prompt")))
    prompt_text = normalize_text("\n\n".join(parts))
    if not prompt_text:
        raise ValueError(f"free detail has no prompt body: {source_id(record)}")
    upstream_id = source_id(record)
    slug = normalize_text(record.get("slug") or upstream_id)
    title = normalize_text(record.get("title") or slug)
    source_url = f"https://opennana.com/awesome-prompt-gallery/{slug}"
    prompt_sha = sha256_text(prompt_text)
    template = template_text(prompt_text)
    normalized = {
        "schema_version": "opennana-normalized-record-1.0",
        "source": "opennana",
        "upstream_id": upstream_id,
        "slug": slug,
        "title": title,
        "source_url": source_url,
        "author": unwrap_author(record.get("author") or record.get("creator") or record.get("user")),
        "model": normalize_text(record.get("model") or record.get("model_name") or "") or None,
        "tags": string_list(record.get("tags") or record.get("categories")),
        "media_type": normalize_text(record.get("media_type") or record.get("type") or "image") or "image",
        "image_urls": image_urls(record),
        "prompt_text": prompt_text,
        "prompt_sha256": prompt_sha,
        "prompt_template_sha256": sha256_text(template) if template else None,
        "updated_at": record.get("updated_at") or record.get("reviewed_at") or record.get("created_at"),
        "observed_at": observed_at,
        "rights": {
            "purpose": "private_reference_review",
            "item_rights": "unverified",
            "commercial_reuse_claimed": False,
            "release_eligible": False,
            "source_image_downloaded": False,
        },
        "workflow_status": "normalized",
    }
    content_basis = {
        "source": normalized["source"],
        "upstream_id": upstream_id,
        "title": title,
        "prompt_sha256": prompt_sha,
        "image_urls": normalized["image_urls"],
        "updated_at": normalized["updated_at"],
    }
    normalized["content_sha256"] = sha256_text(stable_json(content_basis, indent=None))
    return normalized


def normalize_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    locked = bundle.get("locked_metadata_only", [])
    for record in locked:
        if prompt_parts(record.get("prompts", record.get("prompt"))):
            raise ValueError("locked metadata contains forbidden prompt body")
    records = [normalize_free_detail(record, bundle["observed_at"]) for record in bundle.get("free_details", [])]
    records.sort(key=lambda item: (item["upstream_id"], item["content_sha256"]))
    return {
        "schema_version": "opennana-normalized-bundle-1.0",
        "run_id": bundle["run_id"],
        "observed_at": bundle["observed_at"],
        "source_raw": f"raw/{bundle['run_id']}.json",
        "summary": {
            "normalized": len(records),
            "locked_metadata_only": len(locked),
            "source_image_downloads": 0,
        },
        "records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize one private OpenNana raw bundle (dry-run by default).")
    parser.add_argument("--input", type=Path, default=DATA_ROOT / "raw" / "sample-canary-v1.json")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    bundle = normalize_bundle(read_json(args.input))
    output = args.output or DATA_ROOT / "staging" / f"normalized-{bundle['run_id']}.json"
    if args.apply:
        atomic_write_json(output, bundle)
        print(stable_json({"written": str(output), "summary": bundle["summary"]}), end="")
    else:
        print(stable_json({"writes": False, "would_write": str(output), "summary": bundle["summary"]}), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
