"""Source-neutral offline intake/dedupe planner. Never infers human approval.

CLI input is a decrypted archive-sealed-intake-bundle-1 under private research.
Its origin must be authenticated separately (expected repository/workflow/run).
--media-map is optional item_id -> existing local image path evidence. No network.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import struct
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from github_sources.intake_envelope import validate_envelope

POLICY = "exact-file-or-pixel-and-nonblank-prompt-v2"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def image_hashes(path: Path) -> dict:
    from PIL import Image, ImageOps
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 15 * 1024**2:
        raise ValueError("invalid_local_media")
    before = path.stat()
    with path.open("rb") as handle:
        raw = handle.read(15 * 1024**2 + 1)
    if len(raw) > 15 * 1024**2:
        raise ValueError("invalid_local_media")
    file_sha = digest(raw)
    with Image.open(io.BytesIO(raw)) as original:
        if original.width * original.height > 80_000_000 or getattr(original, "n_frames", 1) != 1:
            raise ValueError("oversized_or_animated_media_requires_review")
        pixels = ImageOps.exif_transpose(original).convert("RGBA")
        # No resize/lossy conversion: dimensions and all decoded pixels count.
        pixel_sha = digest(b"rgba-exif-v2\0" + struct.pack(">II", *pixels.size) + pixels.tobytes())
    after = path.stat()
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise ValueError("media_changed_during_hash")
    with path.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != file_sha:
            raise ValueError("media_changed_during_hash")
    return {"file_sha256": file_sha, "pixel_sha256": pixel_sha,
            "pixel_policy": "rgba-exif-v2", "width": pixels.width, "height": pixels.height}


def prompt_tier(prompt: str) -> int:
    value = prompt.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*\n|\n```$", "", value, flags=re.I).strip()
    try:
        parsed = json.loads(value)
        if isinstance(parsed, (dict, list)) and parsed:
            return 1
    except (ValueError, TypeError):
        pass
    # Structured headings/key-value instructions before plain prose; no claim
    # that longer or JSON instructions produce better images.
    headings = sum(bool(re.match(r"\s*(?:#{1,6}\s+|[-*]\s+|[\w가-힣 ]{2,30}:)", line)) for line in prompt.splitlines())
    return 2 if headings >= 2 else 3 if prompt.strip() else 4


def exact_reason(left: dict, right: dict) -> str | None:
    if left.get("file_sha256") and left["file_sha256"] == right.get("file_sha256"):
        return "exact_file"
    if (left.get("pixel_sha256") and left["pixel_sha256"] == right.get("pixel_sha256")
            and left.get("pixel_policy") == right.get("pixel_policy") and left.get("pixel_policy")
            and left.get("prompt_nonblank") and right.get("prompt_nonblank")
            and left.get("prompt_sha256") == right.get("prompt_sha256")):
        return "exact_pixels_and_prompt_exact"
    return None


def dedupe_plan(rows: list[dict]) -> dict:
    """Indexed exact evidence, not quadratic image embedding comparisons."""
    if len({row["item_id"] for row in rows}) != len(rows):
        raise ValueError("duplicate_item_ids")
    parent = list(range(len(rows)))
    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index
    indexes = ({}, {})
    edges = []
    for index, row in enumerate(rows):
        keys = [row.get("file_sha256"),
                (row.get("pixel_policy"), row["pixel_sha256"], row.get("prompt_sha256"))
                if row.get("pixel_policy") and row.get("pixel_sha256") and row.get("prompt_nonblank") else None]
        for table, key in zip(indexes, keys):
            if key is None:
                continue
            if key in table:
                other = table[key]
                reason = exact_reason(row, rows[other])
                if reason:
                    parent[find(index)] = find(other)
                    edges.append({"left": row["item_id"], "right": rows[other]["item_id"], "reason": reason})
            else:
                table[key] = index
    components = {}
    for index, row in enumerate(rows):
        components.setdefault(find(index), []).append(row)
    groups, active, aliases = [], [], []
    for members in components.values():
        keeper = min(members, key=lambda row: (prompt_tier(row.get("original_prompt", "")), row.get("ingested_order", 0), row["item_id"]))
        active.append(keeper["item_id"])
        if len(members) > 1:
            groups.append({"representative_id": keeper["item_id"], "member_ids": sorted(row["item_id"] for row in members)})
            aliases.extend({"item_id": row["item_id"], "representative_id": keeper["item_id"]} for row in members if row is not keeper)
    # Same prompt without identity proof remains a candidate group, never alias.
    by_prompt = {}
    for row in rows:
        if row.get("prompt_nonblank"):
            by_prompt.setdefault(row["prompt_sha256"], []).append(row["item_id"])
    active_set = set(active)
    candidates = [sorted(identifier for identifier in ids if identifier in active_set) for ids in by_prompt.values()]
    return {"policy": POLICY, "active_ids": sorted(active), "aliases": aliases, "exact_groups": groups,
            "exact_evidence_edges": edges, "prompt_group_candidates": [ids for ids in candidates if len(ids) > 1],
            "physical_deletions": 0, "human_approved": False, "public_eligible": False}


def build_plan(bundle: dict, media_map: dict | None = None) -> dict:
    if (bundle.get("schema_version") != "archive-sealed-intake-bundle-1" or not isinstance(bundle.get("records"), list)
            or len(bundle["records"]) > 4000):
        raise ValueError("invalid_intake_bundle")
    rows = []
    for order, record in enumerate(bundle["records"]):
        validate_envelope(record)
        prompt = record["original_prompt"]["text"]
        key = record["source_id"] + ":" + record["source_item_id"]
        version = digest(json.dumps(record["source_version"], sort_keys=True, ensure_ascii=False).encode())[:16]
        item_id = "intake-" + digest((key + "\0" + version).encode())[:32]
        row = {"item_id": item_id, "source_key": key, "source_record": record, "original_prompt": prompt,
               "prompt_sha256": digest(prompt.encode()), "prompt_nonblank": bool(prompt.strip()), "ingested_order": order}
        path_value = (media_map or {}).get(item_id)
        if path_value:
            path = Path(path_value).resolve(strict=True)
            if not path.is_relative_to(ROOT):
                raise ValueError("media_must_be_inside_archive")
            row.update(image_hashes(path))
        rows.append(row)
    result = dedupe_plan(rows)
    result.update({"schema_version": "archive-local-pipeline-plan-v2", "records": rows,
                   "provider_calls": 0, "next_stage": "local_media_then_image_candidate_review",
                   "origin_verified": False, "origin_note": "Bind to expected GitHub repository, workflow and run before applying.",
                   "human_review_required_before_luna": True})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--media-map", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    private = (ROOT / "data/private-research").resolve()
    source = args.input.resolve(strict=True)
    if not source.is_relative_to(private) or source.stat().st_size > 32 * 1024**2:
        raise ValueError("input_must_be_private")
    result = build_plan(json.loads(source.read_text(encoding="utf-8")),
                        json.loads(args.media_map.read_text(encoding="utf-8")) if args.media_map else None)
    plan_hash = digest(json.dumps(result, ensure_ascii=False, sort_keys=True).encode())
    output = private / "platform-v2/intake-plans" / (plan_hash + ".json")
    if args.apply:
        output.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if output.exists():
            if output.read_text(encoding="utf-8") != content: raise ValueError("immutable_plan_conflict")
        else:
            with output.open("x", encoding="utf-8") as handle: handle.write(content)
    print(json.dumps({"status": "prepared_not_human_approved" if args.apply else "dry_run", "records": len(result["records"]),
                      "active": len(result["active_ids"]), "aliases": len(result["aliases"]), "provider_calls": 0,
                      "output": str(output.relative_to(ROOT)) if args.apply else None}, ensure_ascii=True))


if __name__ == "__main__":
    main()
