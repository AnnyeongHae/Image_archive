"""Offline, immutable gallery-v2 projection of an approved private snapshot.

The default public mode is intentionally blocked for private-cloud-snapshot-2:
that input format contains image approvals, NOT item-level publication grants.
Only an explicit private_local_preview produces image/prompt cards. This module
does not load credentials, access a network, change approvals, or embed images.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import warnings
import unicodedata

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from image_rag_eval.rights import BASE_NOTICE, safe_source_url

_SPEC = importlib.util.spec_from_file_location("frontend_cloud_snapshot", Path(__file__).with_name("cloud_snapshot.py"))
cloud = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cloud)

SCHEMA = "image-gallery-2"
RECEIPT_SCHEMA = "image-gallery-build-2"
PINNED_SNAPSHOT = "data/private-research/v2/cloud-plans/ae5910fb41af5c0e12d8c203bb203b90ebde3b249099da2ffb4edbe55724b183"
OUTPUT = "data/private-research/platform-v2/frontend-v2"
TAXONOMY = "data/private-research/image-rag-admin/luna-analysis/2026-09-04-luna-reuse-analysis-10-v2/taxonomy-context.json"
BROWSE_CONTRACT = "platform/v2/contracts/browse-categories.v1.json"
BROWSE_CATEGORY_IDS = {"commerce_brand", "content_editorial", "information_education", "character",
                       "story_scene", "space_place", "web_app", "graphic_goods", "people"}
BROWSE_FAMILIES_V1 = {"commerce_brand": {"commerce", "brand"}, "content_editorial": {"content", "editorial"},
                      "information_education": {"education"}, "character": {"character"}, "story_scene": {"narrative"},
                      "space_place": {"spatial"}, "web_app": {"service"}, "graphic_goods": {"decorative"}, "people": {"people"}}
SHELL = "platform/v2/frontend"
SHELL_FILES = ("index.html", "gallery.css", "gallery.js", "gallery-core.mjs")
MODES = {"public", "private_local_preview"}
HEX = re.compile(r"[a-f0-9]{64}\Z")
IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")
MAX_MEDIA_BYTES = 15 * 1024 * 1024
MAX_PIXELS = 80_000_000
BACKGROUND = {"plain": "단색 배경", "studio": "스튜디오", "indoor": "실내", "outdoor": "야외",
              "natural": "자연", "urban": "도시", "abstract": "추상 배경", "transparent": "투명 배경 후보",
              "information_layout": "정보 레이아웃", "mixed": "혼합 배경", "unknown": "배경 미확인"}
MEDIUM = {"photography": "사진", "photo": "사진", "illustration": "일러스트", "3d": "3D",
          "3d_render": "3D 렌더", "graphic_design": "그래픽 디자인", "mixed": "혼합 매체"}


class ProjectionError(ValueError):
    """Fixed safe error codes only; never include private source bodies."""


def encoded(value, *, newline=True):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + ("\n" if newline else "")).encode("utf-8")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def clean(value, limit=240):
    return " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", value).split())[:limit] if isinstance(value, str) else ""


def record_datetime(data):
    """Return an observed source/ingestion date, never a build-time timestamp."""
    if not isinstance(data, dict):
        return None
    for key in ("created_at", "published_at", "ingested_at", "collected_at", "datetime"):
        value = data.get(key)
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+Z-]+)?", value.strip()):
            return value.strip()
    run_id = data.get("source_run_id")
    if isinstance(run_id, str):
        match = re.search(r"(20\d{2}-\d{2}-\d{2})", run_id)
        if match:
            return match.group(1)
    return None


def labels(values, limit=30):
    if values is None:
        return []
    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
        raise ProjectionError("invalid_metadata_labels")
    return list(dict.fromkeys(v for raw in values if (v := clean(raw))))[:limit]


def normalize_subjects(visual):
    """Return only explicit visual subject/caption evidence for the people facet."""
    values = labels(_dict(visual).get("subjects", []))
    caption = clean(_dict(visual).get("caption_ko") or _dict(visual).get("description_ko"))
    return " ".join(unicodedata.normalize("NFKC", " ".join(values + ([caption] if caption else []))).lower().split())


def _dict(value):
    return value if isinstance(value, dict) else {}


def _relative(value):
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or any(c in value for c in "?#%\x00") or re.search(r"[\x01-\x1f\x7f]", value)):
        raise ProjectionError("unsafe_relative_path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ProjectionError("unsafe_relative_path")
    return path


def local_path(root, relative, *, private=False):
    """Reject traversal, alternate streams, symlinks, and Windows junctions."""
    root = Path(root).resolve()
    relative = _relative(relative)
    if private and relative.parts[:2] != ("data", "private-research"):
        raise ProjectionError("private_source_path_required")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ProjectionError("symlink_or_junction_refused")
    if not current.resolve().is_relative_to(root):
        raise ProjectionError("path_outside_archive")
    return current


def _valid_hash(value):
    return isinstance(value, str) and HEX.fullmatch(value) is not None


def validate_browse_contract(document):
    """Version 1 fixes eight PURPOSE categories, not image identity groups."""
    if (not isinstance(document, dict) or document.get("schema_version") not in {"image-browse-categories-1", "image-browse-categories-2"}
            or type(document.get("version")) is not int or document["version"] != 1
            or document.get("axis") != "intended_reuse_purpose"):
        raise ProjectionError("invalid_browse_category_contract")
    rows = document.get("categories")
    if not isinstance(rows, list) or len(rows) != 9 or any(not isinstance(r, dict) for r in rows):
        raise ProjectionError("invalid_browse_category_contract")
    if {r.get("id") for r in rows} != BROWSE_CATEGORY_IDS:
        raise ProjectionError("invalid_browse_category_identifiers")
    families = []
    for row in rows:
        family = row.get("legacy_use_case_families")
        if (not clean(row.get("label")) or not clean(row.get("definition_ko"))
                or not isinstance(row.get("exclusions_ko"), list) or not row["exclusions_ko"]
                or not isinstance(family, list) or not family
                or any(not isinstance(v, str) or not re.fullmatch(r"[a-z]+", v) for v in family)):
            raise ProjectionError("invalid_browse_category_definition")
        families.extend(family)
        if set(family) != BROWSE_FAMILIES_V1[row["id"]]:
            raise ProjectionError("browse_v1_family_mapping_drift")
    if (len(families) != len(set(families))
            or set(families) != {"commerce", "brand", "content", "editorial", "education", "character", "narrative", "spatial", "service", "decorative", "people"}):
        raise ProjectionError("invalid_browse_family_mapping")
    unknown = _dict(document.get("unclassified"))
    future = _dict(document.get("future_llm"))
    allowed = future.get("allowed_category_ids", [])
    primary_count, secondary_max = _dict(future.get("primary")).get("count"), _dict(future.get("secondary")).get("max_count")
    if (unknown.get("id") != "unclassified" or not clean(unknown.get("label"))
            or not isinstance(allowed, list) or len(allowed) != 9 or set(allowed) != BROWSE_CATEGORY_IDS
            or type(primary_count) is not int or primary_count != 1
            or type(secondary_max) is not int or secondary_max != 1
            or _dict(future.get("abstention")).get("category_id") != "unclassified"
            or future.get("changes_visual_group_ids_or_representatives") is not False
            or future.get("grants_rights_or_public_release") is not False
            or future.get("execution_authorized_by_this_contract") is not False):
        raise ProjectionError("invalid_browse_selection_contract")
    return document


def load_browse_contract(root=ROOT):
    raw = local_path(root, BROWSE_CONTRACT).read_bytes()
    return validate_browse_contract(json.loads(raw)), {BROWSE_CONTRACT: sha(raw)}


def browse_category_projection(use_case_ids, known_use_case_ids, contract):
    """Map known exact IDs only. Never inspect prose, subjects, or style names."""
    contract = validate_browse_contract(contract)
    known_ids = set(known_use_case_ids)
    families = {ident.split(".", 1)[0] for ident in use_case_ids if isinstance(ident, str) and ident in known_ids}
    selected = [row for row in contract["categories"] if set(row["legacy_use_case_families"]) & families]
    if not selected:
        selected = [contract["unclassified"]]
    return {"category_ids": [row["id"] for row in selected], "categories": [row["label"] for row in selected],
            "category_source": "unclassified" if selected[0]["id"] == "unclassified" else "legacy_use_case_mapping"}


def normalize_metadata(metadata, taxonomy, browse_contract=None):
    """Only the QA-effective fields; raw analysis, prompt analysis and memos stay private."""
    if not isinstance(metadata, dict):
        raise ProjectionError("invalid_metadata_contract")
    browse_contract = browse_contract if browse_contract is not None else load_browse_contract()[0]
    effective = metadata.get("effective")
    if not effective:
        return {"usage": [], "style": [], "background": [], "keywords": [], "usage_notes": [], "metadata_status": "none",
                **browse_category_projection([], taxonomy, browse_contract)}
    if not isinstance(effective, dict):
        raise ProjectionError("invalid_effective_metadata")
    schema = effective.get("schema_version")
    visual = _dict(effective.get("visual"))
    hints = _dict(effective.get("search_hints"))
    bg = visual.get("background")
    background = ([BACKGROUND.get(bg.get("setting"), clean(bg.get("setting")))]
                  if isinstance(bg, dict) and bg.get("setting") else [clean(bg)] if isinstance(bg, str) else [])
    styles = labels(visual.get("styles", visual.get("style", [])))
    medium = clean(visual.get("medium"))
    if medium:
        styles = list(dict.fromkeys(styles + [MEDIUM.get(medium, medium)]))
    uses, notes, usage_ids = [], [], []
    if schema == "luna-compact-3":
        selections = effective.get("uses", [])
    elif schema == "image-luna-reuse-analysis-result-2":
        selection = _dict(effective.get("usage_selection"))
        selections = ([selection["primary"]] if selection.get("primary") else []) + selection.get("secondary", [])
    elif schema == "image-luna-analysis-result-1":
        selections = effective.get("reuse_ideas", [])
    else:
        raise ProjectionError("unsupported_effective_metadata_schema")
    if not isinstance(selections, list) or len(selections) > 5 or any(not isinstance(u, dict) for u in selections):
        raise ProjectionError("invalid_usage_selection")
    for selection in selections:
        use_id = selection.get("use_case_id")
        if use_id is not None:
            if use_id not in taxonomy:
                raise ProjectionError("unknown_usage_taxonomy_id")
            title = taxonomy[use_id]
            usage_ids.append(use_id)
        else:
            if schema != "image-luna-analysis-result-1":
                raise ProjectionError("missing_usage_taxonomy_id")
            title = clean(selection.get("use_case"))
        if not title:
            continue
        uses.append(title)
        why = clean(selection.get("why_ko") or selection.get("why_usable_ko") or selection.get("visual_reason"), 700)
        adaptation = clean(selection.get("adaptation_ko") or selection.get("adaptation"), 700)
        changes = labels(selection.get("changes", []), 8)
        constraints = labels(selection.get("constraints", selection.get("constraints_ko", [])), 8)
        caution = clean(selection.get("caution"), 700)
        parts = [title + (" · 조건부 제안" if selection.get("fit") == "conditional" else " · 활용 제안")]
        parts += (["근거: " + why] if why else [])
        parts += (["변경: " + adaptation] if adaptation else [])
        parts += (["변경: " + " / ".join(changes)] if changes else [])
        parts += (["제약: " + " / ".join(constraints)] if constraints else [])
        parts += (["유의: " + caution] if caution else [])
        notes.append("\n".join(parts))
    keywords = (labels(visual.get("subjects", [])) + labels(visual.get("layout", visual.get("composition", [])))
                + labels(visual.get("search_keywords_ko", [])) + labels(visual.get("search_keywords_en", []))
                + labels(hints.get("keywords_ko", [])) + labels(hints.get("keywords_en", []))
                + labels(hints.get("categories", [])) + usage_ids)
    description = clean(visual.get("caption_ko") or visual.get("description_ko"), 400)
    bg_detail = clean(_dict(bg).get("detail_ko") or _dict(bg).get("description_ko"), 400)
    keywords += [v for v in (description, bg_detail) if v]
    projected = browse_category_projection(usage_ids, taxonomy, browse_contract)
    subject_text = normalize_subjects(visual)
    if any(token in subject_text for token in ("인물", "사람", "여성", "남성", "소녀", "소년", "woman", "man", "person", "portrait")):
        projected["category_ids"] = list(dict.fromkeys(projected["category_ids"] + ["people"]))
        projected["categories"] = list(dict.fromkeys(projected["categories"] + ["인물"]))
        projected["category_source"] = "visual_subject_rule"
    return {"usage": list(dict.fromkeys(uses)), "style": styles,
            "background": [v for v in background if v], "keywords": list(dict.fromkeys(keywords)),
            "usage_notes": notes, "metadata_status": "human_verified" if metadata.get("metadata_human_approved") is True else "candidate",
            **projected}


def validate_items(items, manifest):
    """Trust pinned human relations, not metadata or new similarity guesses."""
    if not isinstance(items, list) or not items or len(items) > 379:
        raise ProjectionError("invalid_snapshot_item_scope")
    by_id, groups, seen_original, seen_prepared = {}, defaultdict(list), set(), set()
    media_rows = manifest.get("media_manifest")
    if not isinstance(media_rows, list) or len(media_rows) != len(items):
        raise ProjectionError("media_manifest_count_mismatch")
    media = {r.get("item_id"): r for r in media_rows if isinstance(r, dict)}
    if len(media) != len(items):
        raise ProjectionError("media_manifest_ambiguous")
    for row in items:
        if not isinstance(row, dict):
            raise ProjectionError("invalid_snapshot_item")
        for key in ("item_id", "group_id", "representative_id"):
            if not isinstance(row.get(key), str) or not IDENT.fullmatch(row[key]):
                raise ProjectionError("invalid_item_or_group_identifier")
        ident = row["item_id"]
        if ident in by_id or row.get("snapshot_id") != manifest.get("snapshot_id"):
            raise ProjectionError("duplicate_or_wrong_snapshot_item")
        data = _dict(row.get("private_data"))
        if (data.get("public_eligible") is not False or not isinstance(row.get("original_prompt"), str)
                or not isinstance(row.get("rights_json"), dict) or row["rights_json"].get("release_eligible") is not False):
            raise ProjectionError("private_snapshot_approval_drift")
        for key in ("prepared_sha256", "original_sha256", "prompt_sha256", "analysis_effective_sha256"):
            if not _valid_hash(data.get(key)):
                raise ProjectionError("missing_source_identity_hash")
        if sha(row["original_prompt"].encode("utf-8")) != data["prompt_sha256"]:
            raise ProjectionError("original_prompt_hash_mismatch")
        if sha(encoded(_dict(row.get("metadata_json")).get("effective"), newline=False)) != data["analysis_effective_sha256"]:
            raise ProjectionError("effective_metadata_hash_mismatch")
        effective = _dict(_dict(row.get("metadata_json")).get("effective"))
        if (not clean(data.get("style_id")) or effective.get("style_id") != data["style_id"]
                or (effective.get("item_id") is not None and effective["item_id"] != ident)):
            raise ProjectionError("metadata_item_or_style_identity_mismatch")
        if data["original_sha256"] in seen_original or data["prepared_sha256"] in seen_prepared:
            raise ProjectionError("snapshot_duplicate_media_requires_review")
        seen_original.add(data["original_sha256"])
        seen_prepared.add(data["prepared_sha256"])
        expected = {"item_id": ident, **{key: data.get(key) for key in
                    ("prepared_sha256", "prepared_relative_path", "prepared_mime_type", "prepared_bytes")}}
        if media.get(ident) != expected:
            raise ProjectionError("media_manifest_item_mismatch")
        by_id[ident] = row
        groups[row["group_id"]].append(row)
    for group_id, members in groups.items():
        rep_ids = {r["representative_id"] for r in members}
        if len(rep_ids) != 1:
            raise ProjectionError("conflicting_group_representatives")
        rep = by_id.get(next(iter(rep_ids)))
        if not rep or rep["group_id"] != group_id or rep["item_id"] != rep["representative_id"]:
            raise ProjectionError("missing_or_cross_group_representative")
    counts = manifest.get("counts", {})
    if counts.get("items") != len(items) or counts.get("groups") != len(groups):
        raise ProjectionError("snapshot_count_mismatch")
    return groups


def project_catalog(items, manifest, media, taxonomy, *, mode="public", browse_contract=None):
    if mode not in MODES:
        raise ProjectionError("invalid_projection_mode")
    grouped = validate_items(items, manifest)
    browse_contract = validate_browse_contract(browse_contract if browse_contract is not None else load_browse_contract()[0])
    browse_meta = {"browse_taxonomy_version": browse_contract["version"],
                   "browse_categories": [{"id": row["id"], "label": row["label"]}
                                         for row in [*browse_contract["categories"], browse_contract["unclassified"]]]}
    # This is not a general permission flag. private-cloud-snapshot-2 explicitly
    # forbids publication. A future public source adapter must consume the
    # canonical rights/release gate, never reinterpret review checkboxes.
    if mode == "public":
        return {"schema_version": SCHEMA, "mode": mode, "status": "blocked", **browse_meta,
                "blocked_reason": "private_snapshot_has_no_item_public_release_evidence",
                "counts": {"images": 0, "groups": 0, "variants": 0, "excluded": len(items), "withheld": len(items)},
                "groups": []}, {}
    cards, details = [], {}
    for group_id, members in sorted(grouped.items()):
        representative_id = members[0]["representative_id"]
        members = sorted(members, key=lambda r: (r["item_id"] != representative_id, r["item_id"]))
        expanded = []
        for row in members:
            data, rights = row["private_data"], row["rights_json"]
            meta = normalize_metadata(row["metadata_json"], taxonomy, browse_contract)
            source = {"name": clean(rights.get("source_name") or data.get("source_name")) or "출처 미확인",
                      "url": safe_source_url(rights.get("source_url") or data.get("source_url"))}
            public_rights = {"badge": clean(rights.get("badge")) or "권리 미확인",
                             "notice": clean(rights.get("notice_text"), 1800) or BASE_NOTICE,
                             "attribution": clean(rights.get("attribution_text"), 500),
                             "license": clean(rights.get("license_label"), 160) or "라이선스 미확인"}
            entry = {"id": row["item_id"], "style_id": clean(data.get("style_id")),
                     "title": clean(data.get("title"), 220) or clean(data.get("style_id")) or row["item_id"],
                     "datetime": record_datetime(data),
                     **media[row["item_id"]], "original_prompt": row["original_prompt"],
                     **meta, "source": source, "rights": public_rights}
            expanded.append(entry)
        detail = {"id": group_id, "representative_id": representative_id, "members": expanded}
        detail_path = "data/groups/" + sha(encoded(detail)) + ".json"
        details[detail_path] = detail
        rep = expanded[0]
        cards.append({"id": group_id, "representative_id": representative_id, "member_count": len(expanded),
                      "representative": {key: rep[key] for key in ("id", "style_id", "title", "datetime", "thumbnail", "usage", "style", "background", "source", "category_ids", "categories", "category_source")}
                      | {"rights": {"badge": rep["rights"]["badge"]}},
                      "datetime": rep.get("datetime"),
                      "members": [{key: entry[key] for key in ("id", "style_id", "title", "datetime", "usage", "style", "background", "keywords", "category_ids", "categories", "category_source")} for entry in expanded],
                      "detail_path": detail_path})
    cards.sort(key=lambda card: (card["representative"]["style_id"], card["id"]))
    return {"schema_version": SCHEMA, "mode": mode, "status": "private_preview", **browse_meta,
            "counts": {"images": len(items), "groups": len(cards), "variants": len(items) - len(cards), "excluded": 0, "withheld": 0},
            "groups": cards}, details


def read_snapshot(path, root=ROOT):
    try:
        plan = cloud.read_plan(path, root)
    except cloud.SnapshotError as exc:
        raise ProjectionError(str(exc)) from None
    validate_items(plan["items"], plan["manifest"])
    return plan


def load_taxonomy(root):
    path = local_path(root, TAXONOMY, private=True)
    raw = path.read_bytes()
    document = json.loads(raw)
    rows = document.get("use_cases", [])
    if document.get("schema_version") != "image-reuse-taxonomy-model-context-1" or not isinstance(rows, list):
        raise ProjectionError("invalid_usage_taxonomy")
    result = {}
    for row in rows:
        ident, label = row.get("use_case_id"), clean(row.get("label_ko"))
        if not isinstance(ident, str) or not IDENT.fullmatch(ident) or ident in result or not label:
            raise ProjectionError("invalid_usage_taxonomy")
        result[ident] = label
    if not result:
        raise ProjectionError("empty_usage_taxonomy")
    return result, {TAXONOMY: sha(raw)}


def prepare_media(items, root, *, render=True):
    try:
        from PIL import Image, ImageOps, features
        import PIL
    except ImportError:
        raise ProjectionError("pillow_missing_no_install_performed") from None
    if render and not features.check("webp"):
        raise ProjectionError("pillow_webp_unavailable")
    mapped, files, bindings = {}, {}, {}
    for row in items:
        data = row["private_data"]
        path = local_path(root, data["prepared_relative_path"], private=True)
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_MEDIA_BYTES:
            raise ProjectionError("prepared_media_missing_or_size_invalid")
        body = path.read_bytes()
        digest = sha(body)
        if digest != data["prepared_sha256"] or len(body) != data["prepared_bytes"]:
            raise ProjectionError("prepared_media_hash_or_size_drift")
        bindings[data["prepared_relative_path"]] = digest
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(body)) as source:
                    mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(source.format)
                    if (mime != data["prepared_mime_type"] or source.width * source.height > MAX_PIXELS
                            or getattr(source, "n_frames", 1) != 1):
                        raise ProjectionError("prepared_media_decode_contract_mismatch")
                    source.load()
                    if not render:
                        continue
                    # Metadata-free derivatives; the immutable prepared source
                    # stays untouched. One fallback serves both detail and the
                    # thumbnail fallback, with no member-ID duplicate copies.
                    normalized = ImageOps.exif_transpose(source)
                    alpha = normalized.mode in ("RGBA", "LA") or "transparency" in normalized.info
                    normalized = normalized.convert("RGBA" if alpha else "RGB")
                    normalized.info.clear()
                    fallback = io.BytesIO()
                    if alpha:
                        normalized.save(fallback, format="PNG", optimize=True)
                        suffix = ".png"
                    else:
                        normalized.save(fallback, format="JPEG", quality=92, optimize=True, subsampling=0)
                        suffix = ".jpg"
                    fallback_raw = fallback.getvalue()
                    src = "media/" + sha(fallback_raw) + suffix
                    files[src] = fallback_raw
                    thumb = normalized.copy()
                    thumb.thumbnail((640, 640), Image.Resampling.LANCZOS)
                    out = io.BytesIO()
                    thumb.save(out, format="WEBP", quality=82, method=4)
                    thumb_raw = out.getvalue()
                    webp = "media/" + sha(thumb_raw) + ".webp"
                    files[webp] = thumb_raw
                    mapped[row["item_id"]] = {"thumbnail": {"webp": webp, "src": src, "width": thumb.width, "height": thumb.height},
                                               "image": {"src": src, "width": normalized.width, "height": normalized.height}}
        except ProjectionError:
            raise
        except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise ProjectionError("prepared_media_decode_failed") from None
    return mapped, files, bindings, {"pillow_version": PIL.__version__, "thumbnail_max_side": 640,
                                     "thumbnail_webp_quality": 82, "fallback": "metadata-free JPEG quality92 or alpha PNG"}


def _assert_no_private_text(files):
    # Preserve exact prompts; if a prompt itself contains a local path or a
    # signed/credential-bearing URL, fail instead of silently rewriting it.
    private_path = re.compile(r"(?:file://|(?<![A-Za-z0-9])[A-Za-z]:[/\\]|data/private-research/)", re.I)
    signed_url = re.compile(r"https?://[^\s\"<>]*[?&](?:x-amz-[^=&\s]+|x-goog-[^=&\s]+|sig|signature|token|api[_-]?key|access[_-]?token)=", re.I)
    userinfo_url = re.compile(r"https?://[^/\s\"<>]+@", re.I)
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for child in value.values():
                yield from strings(child)
        elif isinstance(value, list):
            for child in value:
                yield from strings(child)
    for name, body in files.items():
        if name.endswith(".json"):
            for content in strings(json.loads(body)):
                if private_path.search(content) or signed_url.search(content) or userinfo_url.search(content):
                    raise ProjectionError("private_text_or_signed_url_in_projection")


def _write_immutable(target, files, receipt):
    complete = {**files, "build-receipt.json": encoded(receipt)}
    if target.exists():
        actual = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
        if actual != set(complete):
            raise ProjectionError("immutable_bundle_conflict")
        for name, raw in complete.items():
            path = local_path(target, name)
            if not path.is_file() or path.read_bytes() != raw:
                raise ProjectionError("immutable_bundle_conflict")
        return
    target.mkdir(parents=True, exist_ok=False)
    # Receipt is the last completion marker. Interruptions leave an incomplete
    # directory that can neither be served nor overwritten by a retry.
    for name, raw in complete.items():
        path = local_path(target, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())


def build_bundle(root=ROOT, *, snapshot=None, mode="public", apply=False):
    root = Path(root).resolve()
    if mode not in MODES:
        raise ProjectionError("invalid_projection_mode")
    snapshot = Path(snapshot) if snapshot else root / PINNED_SNAPSHOT
    if not snapshot.is_absolute():
        snapshot = root / snapshot
    plan = read_snapshot(snapshot, root)
    taxonomy, taxonomy_bindings = load_taxonomy(root)
    browse_contract, browse_bindings = load_browse_contract(root)
    # Validate every metadata row even when public mode withholds it.
    normalized_metadata = [normalize_metadata(row["metadata_json"], taxonomy, browse_contract) for row in plan["items"]]
    mapped, files, media_bindings, conversion = prepare_media(plan["items"], root, render=mode == "private_local_preview")
    catalog, details = project_catalog(plan["items"], plan["manifest"], mapped, taxonomy, mode=mode, browse_contract=browse_contract)
    files["data/catalog.json"] = encoded(catalog)
    files.update({name: encoded(value) for name, value in details.items()})
    shell_hashes = {}
    for name in SHELL_FILES:
        path = local_path(root, SHELL + "/" + name)
        if not path.is_file():
            raise ProjectionError("frontend_shell_missing")
        raw = path.read_bytes()
        files[name] = raw
        shell_hashes[SHELL + "/" + name] = sha(raw)
    _assert_no_private_text(files)
    code_sources = {"platform/v2/local/frontend_projection.py": sha(Path(__file__).read_bytes()),
                    "platform/v2/local/cloud_snapshot.py": sha(Path(__file__).with_name("cloud_snapshot.py").read_bytes()),
                    "src/image_rag_eval/rights.py": sha((ROOT / "src/image_rag_eval/rights.py").read_bytes())}
    served_files = {name: sha(body) for name, body in sorted(files.items())}
    identity = {"schema_version": RECEIPT_SCHEMA, "mode": mode, "snapshot_id": plan["manifest"]["snapshot_id"],
                "input_manifest_sha256": plan["manifest_sha256"], "input_files": plan["manifest"]["files"],
                "code_sources": code_sources, "frontend_sources": shell_hashes, "taxonomy_sources": taxonomy_bindings,
                "browse_taxonomy_sources": browse_bindings,
                "media_sources": media_bindings, "conversion": conversion, "served_files": served_files}
    build_id = sha(encoded(identity))
    target = local_path(root, OUTPUT + "/" + build_id, private=True)
    receipt = {"schema_version": RECEIPT_SCHEMA, "build_id": build_id, "identity": identity,
               "mode": mode, "status": "ready" if mode == "private_local_preview" else "blocked",
               "blocked_reason": catalog.get("blocked_reason"), "counts": catalog["counts"],
               "served_files": served_files, "source_snapshot_id": plan["manifest"]["snapshot_id"],
               "source_manifest_sha256": plan["manifest_sha256"], "input_images": len(plan["items"]),
               "upstream_duplicate_exclusions": "already_applied_in_snapshot_not_recounted",
               "metadata_status_counts": dict(Counter(r["metadata_status"] for r in normalized_metadata)),
               "browse_taxonomy_version": browse_contract["version"],
               "browse_category_coverage": {row["id"]: sum(row["id"] in r["category_ids"] for r in normalized_metadata)
                                            for row in [*browse_contract["categories"], browse_contract["unclassified"]]},
               "category_source_counts": dict(Counter(r["category_source"] for r in normalized_metadata)),
               "category_count_basis": "input_image_multilabel_counts_not_distinct_image_sum",
               "catalog_bytes": len(files["data/catalog.json"]),
               "catalog_gzip_bytes_calculated": len(gzip.compress(files["data/catalog.json"], mtime=0)),
               "detail_bytes": sum(len(files[p]) for p in details),
               "media_bytes": sum(len(raw) for name, raw in files.items() if name.startswith("media/")),
               "media_files": sum(name.startswith("media/") for name in files),
               "total_served_bytes": sum(map(len, files.values())), "new_embedding_calls": 0, "model_calls": 0,
               "network_calls": 0, "approval_writes": 0, "public_release": False, "originals_modified": False}
    # Recheck bindings after conversion and before the first write.
    for relative, expected in {**media_bindings, **taxonomy_bindings, **browse_bindings, **shell_hashes}.items():
        if sha(local_path(root, relative).read_bytes()) != expected:
            raise ProjectionError("source_changed_during_build")
    for name, expected in plan["manifest"]["files"].items():
        if sha((plan["path"] / name).read_bytes()) != expected["sha256"]:
            raise ProjectionError("snapshot_changed_during_build")
    if sha((plan["path"] / "manifest.json").read_bytes()) != plan["manifest_sha256"]:
        raise ProjectionError("snapshot_changed_during_build")
    if apply:
        _write_immutable(target, files, receipt)
    return {"status": receipt["status"] if apply else "dry_run", "mode": mode, "build_id": build_id,
            "path": str(target), "counts": catalog["counts"], "blocked_reason": receipt["blocked_reason"],
            "catalog_bytes": receipt["catalog_bytes"], "detail_bytes": receipt["detail_bytes"],
            "catalog_gzip_bytes_calculated": receipt["catalog_gzip_bytes_calculated"],
            "browse_category_coverage": receipt["browse_category_coverage"], "category_source_counts": receipt["category_source_counts"],
            "media_bytes": receipt["media_bytes"], "media_files": receipt["media_files"],
            "network_calls": 0, "model_calls": 0, "new_embedding_calls": 0, "public_release": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", type=Path, default=ROOT)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--mode", choices=sorted(MODES), default="public")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = build_bundle(args.archive_root, snapshot=args.snapshot, mode=args.mode, apply=args.apply)
        print(encoded(result).decode("utf-8"), end="")
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, ProjectionError) else "frontend_projection_failed"
        print(json.dumps({"status": "failed", "error_code": code, "new_embedding_calls": 0, "model_calls": 0, "network_calls": 0}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
