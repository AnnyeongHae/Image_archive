"""Compact private Luna contract and stable-prefix compiler; no model calls."""
from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from .luna_analysis_import import _json, _path, digest, encode, LunaImportError

SCHEMA = "00_CORE/schemas/image_luna_compact_result.schema.json"
INSTRUCTIONS = "00_CORE/templates/image_luna_compact.instructions.md"
TAXONOMY = "data/private-research/image-rag-admin/luna-analysis/2026-09-04-luna-reuse-analysis-10-v2/taxonomy-context.json"
COMMON_POLICY = {
    "metadata_human_approved": False,
    "review_status": "needs_review",
    "release_eligible": False,
    "rights_source_resolution_require_separate_review": True,
    "flattened_preview_does_not_prove_editable_layers": True,
    "human_memo_is_separate": True,
}


def contract(root: Path) -> dict:
    schema, schema_raw = _json(_path(root.parent, SCHEMA))
    instructions_raw = _path(root.parent, INSTRUCTIONS).read_bytes()
    taxonomy, taxonomy_raw = _json(_path(root, TAXONOMY))
    Draft202012Validator.check_schema(schema)
    entries = [{key: row[key] for key in (
        "use_case_id", "definition_ko", "minimum_evidence_ko", "exclusion_ko"
    )} for row in taxonomy["use_cases"]]
    entries.sort(key=lambda row: row["use_case_id"])
    ids = [row["use_case_id"] for row in entries]
    if len(ids) != len(set(ids)) or not ids:
        raise LunaImportError("Taxonomy IDs must be nonempty and unique")
    prefix = (instructions_raw.decode("utf-8-sig").replace("\r\n", "\n").strip()
              + "\n\n## Fixed output schema\n" + encode(schema).decode("utf-8")
              + "\n\n## Fixed use-case dictionary\n" + encode(entries).decode("utf-8")
              + "\n\n## End of shared prefix\n")
    return {"schema": schema, "taxonomy_ids": set(ids), "prefix": prefix,
            "schema_sha256": digest(schema_raw), "instruction_sha256": digest(instructions_raw),
            "taxonomy_sha256": digest(taxonomy_raw), "prefix_sha256": digest(prefix.encode("utf-8"))}


def visual_value(result: dict, pointer: str) -> str:
    current = result
    try:
        for token in pointer.removeprefix("/").split("/"):
            current = current[int(token)] if isinstance(current, list) else current[token]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise LunaImportError("Evidence reference does not resolve") from exc
    if not isinstance(current, str) or not current.strip() or current.lower() == "unknown":
        raise LunaImportError("Evidence must reference a meaningful visible string")
    return current


def validate_compact(result: dict, pinned: dict, *, expected_style_id: str,
                     visual_draft: dict | None = None, original_prompt: str | None = None) -> None:
    errors = sorted(Draft202012Validator(pinned["schema"]).iter_errors(result), key=lambda e: str(e.path))
    if errors:
        raise LunaImportError("Compact schema rejected: " + errors[0].message)
    if result["style_id"] != expected_style_id or len(encode(result)) > 16000:
        raise LunaImportError("Wrong image identity or oversized compact result")
    if visual_draft is not None and (set(visual_draft) != {"style_id", "visual"}
            or visual_draft["style_id"] != expected_style_id or visual_draft["visual"] != result["visual"]):
        raise LunaImportError("Image-first draft differs from final visual fields")
    uses = result["uses"]
    ids = [row["use_case_id"] for row in uses]
    if len(ids) != len(set(ids)) or not set(ids) <= pinned["taxonomy_ids"]:
        raise LunaImportError("Use-case IDs must be unique and pinned")
    if uses:
        if sum(row["priority"] == "primary" for row in uses) != 1 or result["abstention_reason_ko"] is not None:
            raise LunaImportError("Selections require exactly one primary and no abstention")
    elif not result["abstention_reason_ko"]:
        raise LunaImportError("Empty selections require explicit abstention")
    for row in uses:
        if row["fit"] == "supported" and (row["basis"] == "prompt" or not row["evidence_refs"]):
            raise LunaImportError("Supported fit requires image evidence")
        for pointer in row["evidence_refs"]:
            visual_value(result, pointer)
    for row in result["prompt"]["conflicts"]:
        visual_value(result, row["visual_ref"])
        if original_prompt is not None and row["prompt_quote"] not in original_prompt:
            raise LunaImportError("Conflict quote not found in exact original prompt")
    reserved = {"keywords", "search_keywords_ko", "search_keywords_en", "approval", "rights", "release_eligible"}
    if reserved & set(result["extras_json"]) or len(encode(result["extras_json"])) > 1000:
        raise LunaImportError("Extras cannot hide core fields or be oversized")


def worker_message(pinned: dict, assignments: list[dict]) -> str:
    """Only task paths follow the stable prefix, never original prompt text/gold labels."""
    keys = {"style_id", "prepared_image_path", "prompt_context_path", "visual_draft_path", "raw_result_path"}
    if not 1 <= len(assignments) <= 5 or any(set(row) != keys for row in assignments):
        raise LunaImportError("Batch requires one to five minimal assignments")
    if len({row["style_id"] for row in assignments}) != len(assignments):
        raise LunaImportError("Duplicate image assignment")
    return pinned["prefix"] + "\n## Variable assignments\n" + encode(assignments).decode("utf-8")


def prepare_contract(root: Path, *, apply: bool = False) -> dict:
    pinned = contract(root)
    raw = pinned["prefix"].encode("utf-8")
    base = _path(root, "data/private-research/image-rag-admin/luna-contracts/" + pinned["prefix_sha256"])
    receipt = {"schema_version": "luna-static-prefix-3", "model_family": "gpt-5.6-luna",
               "schema_sha256": pinned["schema_sha256"], "instruction_sha256": pinned["instruction_sha256"],
               "taxonomy_sha256": pinned["taxonomy_sha256"], "prefix_sha256": pinned["prefix_sha256"],
               "prefix_utf8_bytes": len(raw), "prefix_characters": len(pinned["prefix"]),
               "token_count": None, "maximum_batch_size": 5, "common_policy": COMMON_POLICY,
               "model_calls": 0, "cache_controls_applied": False, "cache_hit_guaranteed": False,
               "cache_scope": "stable_worker_message_prefix_only_not_host_context",
               "source": "https://developers.openai.com/api/docs/guides/prompt-caching"}
    files = {"shared-prefix.txt": raw, "receipt.json": encode(receipt)}
    for name, value in files.items():
        target = base / name
        if target.exists() and target.read_bytes() != value:
            raise LunaImportError("Immutable prefix artifact changed")
    status = "unchanged" if all((base / name).exists() for name in files) else "dry_run"
    if apply:
        base.mkdir(parents=True, exist_ok=True)
        for name, value in files.items():
            target = base / name
            if not target.exists():
                with target.open("xb") as output:
                    output.write(value)
        status = "unchanged" if status == "unchanged" else "prepared"
    return {"status": status, "path": base.relative_to(root).as_posix(), **receipt}
