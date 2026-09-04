#!/usr/bin/env python3
"""Build the local five-reference canary without external dependencies.

Dry-run is the default. Use --apply to write generated projections and dist/.
The script deliberately copies only the five explicit media records; it never
walks or exports the full legacy research archive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps


ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "data" / "canonical" / "featured_five.json"
APP = ROOT / "app"
DIST = ROOT / "dist"
WEBP_QUALITY = 82
WEBP_METHOD = 6
JPEG_QUALITY = 82
JPEG_SUBSAMPLING = "4:4:4"
PNG_COMPRESS_LEVEL = 9


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def load_collection() -> dict:
    payload = json.loads(CANONICAL.read_text(encoding="utf-8"))
    items = payload.get("items", [])
    if len(items) != 5:
        raise ValueError(f"Expected exactly 5 featured items, found {len(items)}")
    style_ids = [item.get("reference_style_id") for item in items]
    if len(style_ids) != len(set(style_ids)):
        raise ValueError("reference_style_id values must be unique")
    for item in items:
        image = ROOT / item["image_path"]
        if not is_within(image, ROOT) or not image.is_file():
            raise ValueError(f"Missing or unsafe image path: {item['image_path']}")
        if sha256(image) != item["image_sha256"]:
            raise ValueError(f"SHA-256 mismatch: {item['reference_style_id']}")
        if item.get("release_eligible") is not False:
            raise ValueError("Canary accepts review-only records; release flag must be false")
    return payload


def js_projection(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    return (
        "// Generated from data/canonical/featured_five.json. Do not edit.\n"
        f"window.IMAGE_ARCHIVE_FEATURED_FIVE = {encoded};\n"
    )


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def copy_verified(source: Path, destination: Path) -> dict:
    if not is_within(source, ROOT) or not is_within(destination, ROOT):
        raise ValueError(f"Copy outside platform root refused: {source} -> {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if sha256(source) != sha256(destination):
        raise ValueError(f"Copy verification failed: {destination}")
    return {
        "path": destination.relative_to(ROOT).as_posix(),
        "bytes": destination.stat().st_size,
        "sha256": sha256(destination),
    }


def write_binary_verified(destination: Path, content: bytes) -> dict:
    if not is_within(destination, ROOT):
        raise ValueError(f"Write outside platform root refused: {destination}")
    write_bytes(destination, content)
    expected_hash = sha256_bytes(content)
    if sha256(destination) != expected_hash:
        raise ValueError(f"Binary write verification failed: {destination}")
    return {
        "path": destination.relative_to(ROOT).as_posix(),
        "bytes": destination.stat().st_size,
        "sha256": expected_hash,
    }


def clear_directory_files(path: Path) -> None:
    if not is_within(path, ROOT):
        raise ValueError(f"Refusing to clear outside platform root: {path}")
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        return
    if not path.is_dir():
        raise ValueError(f"Expected directory for cleanup: {path}")
    for child in path.iterdir():
        if child.is_file():
            child.unlink()


def webp_relative_path(relative_path: str) -> str:
    path = Path(relative_path)
    if not path.suffix:
        raise ValueError(f"Cannot derive WebP path without suffix: {relative_path}")
    return path.with_suffix(".webp").as_posix()


def source_format(relative_path: str) -> str:
    suffix = Path(relative_path).suffix.lower().lstrip(".")
    if not suffix:
        raise ValueError(f"Cannot derive source format without suffix: {relative_path}")
    return "jpeg" if suffix == "jpg" else suffix


def encode_webp(source: Path) -> tuple[bytes, int, int]:
    buffer = BytesIO()
    with Image.open(source) as image:
        oriented = ImageOps.exif_transpose(image)
        width, height = oriented.size
        oriented.save(buffer, format="WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)
    return buffer.getvalue(), width, height


def image_has_alpha(image: Image.Image) -> bool:
    if image.mode in {"RGBA", "LA"}:
        return True
    if image.mode == "P":
        return "transparency" in image.info
    return False


def compressed_fallback_relative_path(relative_path: str, *, has_alpha: bool) -> str:
    suffix = ".fallback.png" if has_alpha else ".fallback.jpg"
    return Path(relative_path).with_suffix(suffix).as_posix()


def encode_compressed_fallback(source: Path) -> tuple[bytes, str, int, int, bool]:
    buffer = BytesIO()
    with Image.open(source) as image:
        oriented = ImageOps.exif_transpose(image)
        width, height = oriented.size
        has_alpha = image_has_alpha(oriented)
        if has_alpha:
            payload = oriented
            if payload.mode not in {"RGBA", "LA", "P"}:
                payload = payload.convert("RGBA")
            payload.save(buffer, format="PNG", optimize=True, compress_level=PNG_COMPRESS_LEVEL)
            return buffer.getvalue(), "png", width, height, has_alpha
        payload = oriented.convert("RGB") if oriented.mode != "RGB" else oriented
        payload.save(
            buffer,
            format="JPEG",
            quality=JPEG_QUALITY,
            optimize=True,
            progressive=True,
            subsampling=JPEG_SUBSAMPLING,
        )
        return buffer.getvalue(), "jpeg", width, height, has_alpha


def build_featured_payload() -> tuple[dict, dict, list[dict]]:
    payload = load_collection()
    featured_items = []
    derivatives = []
    source_total_bytes = 0
    webp_total_bytes = 0
    fallback_total_bytes = 0
    fallback_formats: set[str] = set()
    fallback_alpha_count = 0
    for item in payload["items"]:
        source = ROOT / item["image_path"]
        source_bytes = source.stat().st_size
        webp_content, width, height = encode_webp(source)
        fallback_content, fallback_format, fallback_width, fallback_height, has_alpha = encode_compressed_fallback(source)
        webp_bytes = len(webp_content)
        fallback_bytes = len(fallback_content)
        webp_path = webp_relative_path(item["image_path"])
        fallback_path = compressed_fallback_relative_path(item["image_path"], has_alpha=has_alpha)
        savings_bytes = source_bytes - webp_bytes
        fallback_savings_bytes = source_bytes - fallback_bytes
        enriched_item = dict(item)
        enriched_item.update(
            {
                "image_bytes": source_bytes,
                "image_width": width,
                "image_height": height,
                "source_format": source_format(item["image_path"]),
                "source_has_alpha": has_alpha,
                "webp_path": webp_path,
                "webp_bytes": webp_bytes,
                "webp_sha256": sha256_bytes(webp_content),
                "webp_width": width,
                "webp_height": height,
                "webp_quality": WEBP_QUALITY,
                "webp_method": WEBP_METHOD,
                "webp_savings_bytes": savings_bytes,
                "webp_savings_pct": round((savings_bytes / source_bytes) * 100, 2),
                "delivery_fallback_path": fallback_path,
                "delivery_fallback_format": fallback_format,
                "delivery_fallback_bytes": fallback_bytes,
                "delivery_fallback_sha256": sha256_bytes(fallback_content),
                "delivery_fallback_width": fallback_width,
                "delivery_fallback_height": fallback_height,
                "delivery_fallback_savings_bytes": fallback_savings_bytes,
                "delivery_fallback_savings_pct": round((fallback_savings_bytes / source_bytes) * 100, 2),
                "fallback_format": fallback_format,
            }
        )
        featured_items.append(enriched_item)
        derivatives.append(
            {
                "item_id": item["reference_style_id"],
                "kind": "webp",
                "relative_path": webp_path,
                "content": webp_content,
            }
        )
        derivatives.append(
            {
                "item_id": item["reference_style_id"],
                "kind": "fallback",
                "relative_path": fallback_path,
                "content": fallback_content,
            }
        )
        source_total_bytes += source_bytes
        webp_total_bytes += webp_bytes
        fallback_total_bytes += fallback_bytes
        fallback_formats.add(fallback_format)
        if has_alpha:
            fallback_alpha_count += 1

    savings_total_bytes = source_total_bytes - webp_total_bytes
    fallback_savings_total_bytes = source_total_bytes - fallback_total_bytes
    supported_formats = ["webp", "fallback"]
    preferred_format = "webp"
    summary = {
        "source_total_bytes": source_total_bytes,
        "webp_total_bytes": webp_total_bytes,
        "savings_total_bytes": savings_total_bytes,
        "savings_total_pct": round((savings_total_bytes / source_total_bytes) * 100, 2),
        "delivery_fallback_total_bytes": fallback_total_bytes,
        "delivery_fallback_savings_total_bytes": fallback_savings_total_bytes,
        "delivery_fallback_savings_total_pct": round((fallback_savings_total_bytes / source_total_bytes) * 100, 2),
        "derivative_count": len(derivatives),
        "preferred_format": preferred_format,
        "fallback_format": next(iter(fallback_formats)) if len(fallback_formats) == 1 else "mixed",
        "fallback_alpha_count": fallback_alpha_count,
        "derivative_scope": "featured_five_only",
        "deployment_profile": "compressed_only_dist",
        "avif_policy": "private_benchmark_only_not_emitted_to_public_bundle_v1",
        "supported_formats": supported_formats,
    }
    enriched_payload = dict(payload)
    enriched_payload["items"] = featured_items
    enriched_payload["featured_media_summary"] = summary
    enriched_payload["image_delivery"] = {
        "preferred_format": preferred_format,
        "fallback_format": summary["fallback_format"],
        "fallback_path_field": "delivery_fallback_path",
        "dist_profile": "compressed_only",
        "first_card_loading": "eager",
        "remaining_cards_loading": "lazy_async",
        "supported_formats": supported_formats,
    }
    return enriched_payload, summary, derivatives


def build(apply: bool) -> int:
    payload, summary, derivatives = build_featured_payload()
    planned = [
        "app/data/featured-five.js",
        "media/public/featured/*.webp",
        "media/public/featured/*.fallback.(jpg|png)",
        "dist/index.html",
        "dist/archive.html",
        "dist/styles/app.css",
        "dist/scripts/app.js",
        "dist/data/featured-five.js",
        "dist/media/public/featured/*.fallback.(jpg|png)",
        "dist/media/public/featured/*.webp",
        "dist/build-manifest.json",
    ]
    if not apply:
        print(
            json.dumps(
                {
                    "mode": "dry_run",
                    "planned": planned,
                    "featured_media_summary": summary,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    required_app = [APP / "index.html", APP / "styles" / "app.css", APP / "scripts" / "app.js"]
    missing = [str(path) for path in required_app if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Application source is incomplete: {missing}")

    projection = js_projection(payload)
    write_text(APP / "data" / "featured-five.js", projection)

    index_html = (APP / "index.html").read_text(encoding="utf-8")
    expected_marker = 'data-platform-root=".."'
    if expected_marker not in index_html:
        raise ValueError(f"app/index.html must contain {expected_marker}")
    dist_index = index_html.replace(expected_marker, 'data-platform-root="."', 1)
    dist_index = dist_index.replace("../legacy/current_archive/index.html", "./archive.html")
    write_text(DIST / "index.html", dist_index)
    write_text(
        DIST / "archive.html",
        """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>전체 아카이브 준비 중</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f2ebdc;color:#1f2b22;font:16px/1.7 system-ui,sans-serif}.box{max-width:42rem;margin:2rem;padding:2rem;border:1px solid #d8cdbb;border-radius:1.5rem;background:#fffaf1}a{color:#7f2f1e}</style></head>
<body><main class="box"><p>Private research boundary</p><h1>전체 아카이브는 아직 공개 빌드에 포함하지 않았습니다.</h1><p>현재 배포 후보에는 사람이 검토할 대표 이미지 5개만 들어 있습니다. 권리 검토와 공개용 데이터 투영이 끝난 뒤 전체 검색 화면을 연결합니다.</p><p><a href="./index.html">대표 5개로 돌아가기</a></p></main></body></html>
""",
    )

    outputs = []
    outputs.append(copy_verified(APP / "styles" / "app.css", DIST / "styles" / "app.css"))
    outputs.append(copy_verified(APP / "scripts" / "app.js", DIST / "scripts" / "app.js"))
    write_text(DIST / "data" / "featured-five.js", projection)
    outputs.append(
        {
            "path": "dist/data/featured-five.js",
            "bytes": (DIST / "data" / "featured-five.js").stat().st_size,
            "sha256": sha256(DIST / "data" / "featured-five.js"),
        }
    )

    clear_directory_files(DIST / "media" / "public" / "featured")
    for derivative in derivatives:
        source_destination = ROOT / derivative["relative_path"]
        outputs.append(write_binary_verified(source_destination, derivative["content"]))
        dist_destination = DIST / derivative["relative_path"]
        outputs.append(copy_verified(source_destination, dist_destination))

    outputs.append(
        {
            "path": "dist/index.html",
            "bytes": (DIST / "index.html").stat().st_size,
            "sha256": sha256(DIST / "index.html"),
        }
    )
    outputs.append(
        {
            "path": "dist/archive.html",
            "bytes": (DIST / "archive.html").stat().st_size,
            "sha256": sha256(DIST / "archive.html"),
        }
    )
    manifest = {
        "schema_version": "1.0.0",
        "build_kind": "local_review_canary",
        "built_at": datetime.now(timezone.utc).isoformat(),
        "source_collection": CANONICAL.relative_to(ROOT).as_posix(),
        "source_collection_sha256": sha256(CANONICAL),
        "release_eligible": False,
        "dist_original_source_media_included": False,
        "featured_media_summary": summary,
        "outputs": sorted(outputs, key=lambda row: row["path"]),
    }
    write_text(DIST / "build-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "mode": "apply",
                "status": "built",
                "output_count": len(outputs),
                "featured_media_summary": summary,
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write app projection and dist output")
    args = parser.parse_args()
    return build(args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
