from __future__ import annotations

import argparse
import hashlib
import json
import warnings
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import PIL
from PIL import Image, ImageOps, features


ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "data" / "canonical" / "featured_five.json"
OUTPUT_DIR = ROOT / "data" / "private-research" / "media-benchmarks" / "current"
WEBP_QUALITY = 82
WEBP_METHOD = 6
JPEG_QUALITY = 82
PNG_COMPRESS_LEVEL = 9


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


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


def image_has_alpha(image: Image.Image) -> bool:
    if image.mode in {"RGBA", "LA"}:
        return True
    if image.mode == "P":
        return "transparency" in image.info
    return False


def encode_variant(image: Image.Image, variant: str) -> tuple[bytes, str]:
    buffer = BytesIO()
    if variant == "webp_q82":
        image.save(buffer, format="WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)
        return buffer.getvalue(), "image/webp"
    if variant == "webp_lossless":
        image.save(buffer, format="WEBP", lossless=True, method=WEBP_METHOD)
        return buffer.getvalue(), "image/webp"
    if variant == "jpeg_q82":
        image.convert("RGB").save(
            buffer,
            format="JPEG",
            quality=JPEG_QUALITY,
            optimize=True,
            progressive=True,
            subsampling="4:4:4",
        )
        return buffer.getvalue(), "image/jpeg"
    if variant == "png_optimized":
        payload = image
        if payload.mode not in {"RGBA", "RGB", "P", "LA"}:
            payload = payload.convert("RGBA" if image_has_alpha(payload) else "RGB")
        payload.save(buffer, format="PNG", optimize=True, compress_level=PNG_COMPRESS_LEVEL)
        return buffer.getvalue(), "image/png"
    raise ValueError(f"unsupported variant: {variant}")


def supported_variants(image: Image.Image) -> list[str]:
    variants = ["webp_q82", "png_optimized", "webp_lossless"]
    if not image_has_alpha(image):
        variants.insert(1, "jpeg_q82")
    return variants


def load_featured_items() -> list[dict[str, str]]:
    payload = json.loads(CANONICAL.read_text(encoding="utf-8"))
    items = payload.get("items")
    if not isinstance(items, list) or len(items) != 5:
        raise ValueError("featured_five.json must contain exactly 5 items")
    return items


def feature_supported(name: str) -> bool:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            return bool(features.check(name))
        except Exception:
            return False


def benchmark() -> dict[str, object]:
    rows = []
    totals: dict[str, int] = {}
    for item in load_featured_items():
        source_path = ROOT / item["image_path"]
        original_bytes = source_path.read_bytes()
        with Image.open(BytesIO(original_bytes)) as raw_image:
            image = ImageOps.exif_transpose(raw_image)
            alpha = image_has_alpha(image)
            variants = []
            for variant in supported_variants(image):
                encoded, mime_type = encode_variant(image, variant)
                totals[variant] = totals.get(variant, 0) + len(encoded)
                variants.append(
                    {
                        "variant": variant,
                        "mime_type": mime_type,
                        "bytes": len(encoded),
                        "sha256": sha256_bytes(encoded),
                        "savings_bytes": len(original_bytes) - len(encoded),
                        "savings_pct": round(((len(original_bytes) - len(encoded)) / len(original_bytes)) * 100, 2),
                    }
                )
        rows.append(
            {
                "reference_style_id": item["reference_style_id"],
                "source_path": item["image_path"],
                "source_bytes": len(original_bytes),
                "source_sha256": sha256_bytes(original_bytes),
                "source_has_alpha": alpha,
                "variants": variants,
            }
        )

    comparison = []
    source_total = sum(row["source_bytes"] for row in rows)
    for variant, byte_count in sorted(totals.items(), key=lambda pair: pair[1]):
        comparison.append(
            {
                "variant": variant,
                "bytes": byte_count,
                "savings_bytes": source_total - byte_count,
                "savings_pct": round(((source_total - byte_count) / source_total) * 100, 2),
            }
        )

    deployment_recommendation = {
        "preferred": "webp_q82",
        "fallback_for_opaque": "jpeg_q82",
        "fallback_for_alpha": "png_optimized",
        "avif_supported_in_runtime": feature_supported("avif"),
        "notes": [
            "Current Pillow runtime can encode WebP but not AVIF.",
            "Use compressed fallbacks only in dist; keep originals outside deploy artifacts.",
        ],
    }
    return {
        "schema_version": "media-format-benchmark-1.0",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "scope": "featured_five_only",
        "runtime_support": {
            "pillow_version": PIL.__version__,
            "webp": feature_supported("webp"),
            "avif": feature_supported("avif"),
            "jpg": feature_supported("jpg"),
        },
        "totals": {
            "source_total_bytes": source_total,
            "comparison": comparison,
        },
        "records": rows,
        "deployment_recommendation": deployment_recommendation,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write benchmark results to private-research")
    args = parser.parse_args()

    payload = benchmark()
    if not args.apply:
        print(json.dumps({"mode": "dry_run", "payload": payload}, ensure_ascii=False, indent=2))
        return 0

    write_text(OUTPUT_DIR / "featured_format_benchmark.json", json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    for row in payload["records"]:
        sample_dir = OUTPUT_DIR / "samples" / row["reference_style_id"]
        source_path = ROOT / row["source_path"]
        original = source_path.read_bytes()
        with Image.open(BytesIO(original)) as raw_image:
            image = ImageOps.exif_transpose(raw_image)
            for variant in supported_variants(image):
                encoded, _ = encode_variant(image, variant)
                suffix = ".webp" if variant.startswith("webp") else ".jpg" if variant.startswith("jpeg") else ".png"
                write_bytes(sample_dir / f"{variant}{suffix}", encoded)
    print(json.dumps({"mode": "apply", "status": "written", "output_dir": OUTPUT_DIR.relative_to(ROOT).as_posix()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
