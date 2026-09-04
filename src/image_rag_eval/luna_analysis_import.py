"""Deterministic import of one complete image-first Luna candidate canary.

This module does not run a model, infer human approval, or establish that a
draft was chronologically created before prompt viewing. Those execution
claims require the separate orchestrator record. It verifies content bindings
and draft/final equality, leaving every result a model-reported candidate.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from .approval_handoff import _committed, _require_latest, _validate_commit
from .approved_library import build_prompt_catalog
from .incremental_workflow import load_frozen_workflow

MODEL = "gpt-5.6-luna"
SCHEMA_PATH = "../00_CORE/schemas/image_luna_analysis_result.schema.json"
INSTRUCTION_PATH = "../00_CORE/templates/image_luna_analysis.instructions.md"
RELATIVE_ROOT = "data/private-research/image-rag-admin/luna-analysis"
SCHEMA = "image-luna-validated-results-1"
MAX_JSON_BYTES = 2 * 1024 * 1024
TASK_COUNT = 10
SHA = re.compile(r"[a-f0-9]{64}\Z")
ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
MANIFEST_KEYS = {"schema_version", "source_run_id", "analysis_run_id", "source_commit", "model_family",
    "schema_path", "schema_sha256", "instruction_path", "instruction_sha256", "tasks", "approved_library_count",
    "selected_count", "embedding_calls_authorized", "model_execution_automatic", "human_memos_in_model_input", "release_eligible"}
TASK_KEYS = {"task_id", "input_fingerprint", "identity", "item_id", "style_id", "prepared_image_path",
    "prepared_image_sha256", "source_image_sha256", "prompt_sha256", "prompt_context_path", "visual_draft_path", "raw_result_path"}


class LunaImportError(ValueError):
    pass


def encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _path(root: Path, relative: str) -> Path:
    if (not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative
            or Path(relative).is_absolute() or ".." in Path(relative).parts):
        raise LunaImportError("Unsafe analysis artifact path")
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise LunaImportError("Analysis artifact escaped archive root")
    return path


def _json(path: Path) -> tuple[dict, bytes]:
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_JSON_BYTES:
        raise LunaImportError(f"Missing or oversized JSON artifact: {path.name}")
    raw = path.read_bytes()
    def pairs(rows):
        value = {}
        for key, item in rows:
            if key in value:
                raise LunaImportError("Duplicate JSON object property")
            value[key] = item
        return value
    try:
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(LunaImportError("Non-finite JSON value")))
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise LunaImportError(f"Malformed JSON artifact: {path.name}") from exc
    if not isinstance(value, dict):
        raise LunaImportError(f"Expected a JSON object: {path.name}")
    return value, raw


def validate_result_schema(value, schema: dict, location: str = "result") -> None:
    """Validate the deliberately small executable schema using only stdlib.

    Unknown validation vocabulary is rejected, never silently ignored. This
    is not advertised as a general-purpose JSON Schema implementation.
    """
    supported = {"$schema", "$id", "title", "description", "type", "const", "enum", "required", "properties",
                 "additionalProperties", "maxLength", "pattern", "maxItems", "uniqueItems", "items"}
    if not isinstance(schema, dict) or set(schema) - supported:
        raise LunaImportError("Unsupported executable schema vocabulary")
    if "const" in schema and encode(value) != encode(schema["const"]):
        raise LunaImportError(f"{location}: constant mismatch")
    if "enum" in schema and not any(encode(value) == encode(option) for option in schema["enum"]):
        raise LunaImportError(f"{location}: invalid enum value")
    types = schema.get("type")
    types = [types] if isinstance(types, str) else types
    if types is not None:
        checks = {"object": isinstance(value, dict), "array": isinstance(value, list), "string": isinstance(value, str),
                  "boolean": isinstance(value, bool), "null": value is None}
        if any(kind not in checks for kind in types) or not any(checks[kind] for kind in types):
            raise LunaImportError(f"{location}: invalid value type")
    if isinstance(value, dict):
        if not set(schema.get("required", [])).issubset(value):
            raise LunaImportError(f"{location}: required field missing")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise LunaImportError(f"{location}: unknown properties")
        for key, item in value.items():
            if key in properties:
                validate_result_schema(item, properties[key], location + "." + key)
    elif isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise LunaImportError(f"{location}: too many items")
        if schema.get("uniqueItems") and len({encode(item) for item in value}) != len(value):
            raise LunaImportError(f"{location}: duplicate array item")
        if "items" in schema:
            for index, item in enumerate(value):
                validate_result_schema(item, schema["items"], f"{location}[{index}]")
    elif isinstance(value, str):
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise LunaImportError(f"{location}: text too long")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise LunaImportError(f"{location}: pattern mismatch")


def _task_identity(task: dict, manifest: dict) -> dict:
    return {"model_family": MODEL, "source_image_sha256": task["source_image_sha256"],
            "prepared_image_sha256": task["prepared_image_sha256"], "prompt_sha256": task["prompt_sha256"],
            "schema_sha256": manifest["schema_sha256"], "instruction_sha256": manifest["instruction_sha256"],
            "visual_first_protocol": "1"}


def _check_evidence(root: Path, evidence: list[dict]) -> None:
    for row in evidence:
        base = root.parent if row["scope"] == "workspace" else root
        if digest(_path(base, row["path"]).read_bytes()) != row["sha256"]:
            raise LunaImportError("Analysis input or result changed during validation")


def _validated(root: Path, db_path: Path, analysis_run_id: str, expected_commit_id: str | None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", analysis_run_id):
        raise LunaImportError("Invalid analysis run ID")
    base = f"{RELATIVE_ROOT}/{analysis_run_id}"
    directory = _path(root, base)
    manifest, manifest_raw = _json(directory / "tasks.json")
    if (set(manifest) != MANIFEST_KEYS or manifest.get("schema_version") != "image-luna-analysis-tasks-1"
            or manifest.get("analysis_run_id") != analysis_run_id or manifest.get("model_family") != MODEL
            or manifest.get("schema_path") != SCHEMA_PATH or manifest.get("instruction_path") != INSTRUCTION_PATH
            or any(manifest.get(key) is not False for key in ("embedding_calls_authorized", "model_execution_automatic",
                                                              "human_memos_in_model_input", "release_eligible"))):
        raise LunaImportError("Unsupported or unsafe analysis manifest")
    tasks = manifest["tasks"]
    if (not isinstance(tasks, list) or len(tasks) != TASK_COUNT or type(manifest["selected_count"]) is not int
            or manifest["selected_count"] != TASK_COUNT):
        raise LunaImportError("This initial importer requires the complete ten-task canary")
    evidence = [{"scope": "archive", "path": base + "/tasks.json", "sha256": digest(manifest_raw)}]
    contracts = {}
    for name, relative in (("schema", SCHEMA_PATH), ("instruction", INSTRUCTION_PATH)):
        workspace_relative = relative.removeprefix("../")
        path = _path(root.parent, workspace_relative)
        raw = path.read_bytes()
        if digest(raw) != manifest[name + "_sha256"]:
            raise LunaImportError("Executable analysis schema or instruction changed")
        evidence.append({"scope": "workspace", "path": workspace_relative, "sha256": digest(raw)})
        contracts[name] = _json(path)[0] if name == "schema" else raw
    source_run_id = manifest["source_run_id"]
    data = _committed(db_path, source_run_id)
    if not data["commit"] or data["commit"] != manifest["source_commit"]:
        raise LunaImportError("Analysis manifest is not bound to the latest committed approval")
    if expected_commit_id is not None and expected_commit_id != data["commit"]["id"]:
        raise LunaImportError("Expected source commit is stale")
    spec = load_frozen_workflow(root, source_run_id)
    normalized = _validate_commit(spec, data)
    approved = {item["id"] for item in normalized["private_front_export_items"]}
    if type(manifest["approved_library_count"]) is not int or manifest["approved_library_count"] != len(approved):
        raise LunaImportError("Approved-library count changed")
    originals = {item["id"]: item for item in spec["items"]}
    prompts = build_prompt_catalog(root, spec)
    spec_directory = _path(root, f"data/private-research/image-rag-canary/runs/{source_run_id}/group-workflow-v1")
    for name in ("image-group-workflow.spec.json", "source-bindings.json", "build-receipt.json"):
        path = spec_directory / name
        evidence.append({"scope": "archive", "path": path.relative_to(root).as_posix(), "sha256": digest(path.read_bytes())})
    seen_tasks, seen_items, seen_styles = set(), set(), set()
    expected_paths = {"contexts": set(), "visual-drafts": set(), "raw-results": set()}
    results, bindings = [], []
    for task in tasks:
        if not isinstance(task, dict) or set(task) != TASK_KEYS:
            raise LunaImportError("Unknown or missing analysis task properties")
        for field in ("task_id", "input_fingerprint", "prepared_image_sha256", "source_image_sha256", "prompt_sha256"):
            if not isinstance(task[field], str) or not SHA.fullmatch(task[field]):
                raise LunaImportError("Invalid analysis task digest")
        for field in ("item_id", "style_id"):
            if not isinstance(task[field], str) or not ID.fullmatch(task[field]):
                raise LunaImportError("Invalid analysis task identity")
        ident, style = task["item_id"], task["style_id"]
        if task["task_id"] in seen_tasks or ident in seen_items or style in seen_styles:
            raise LunaImportError("Duplicate task, image or style in canary")
        seen_tasks.add(task["task_id"]); seen_items.add(ident); seen_styles.add(style)
        if ident not in approved:
            raise LunaImportError("Analysis task is not an approved retained image")
        source = originals[ident]
        prompt = prompts[ident]
        if (source["style_id"] != style or source["source_sha256"] != task["source_image_sha256"]
                or source["prepared_sha256"] != task["prepared_image_sha256"]
                or prompt["status"] == "unavailable" or prompt["prompt_sha256"] != task["prompt_sha256"]):
            raise LunaImportError("Analysis task image or full prompt binding mismatch")
        identity = _task_identity(task, manifest)
        fingerprint = digest(encode(identity))
        if (task["identity"] != identity or task["input_fingerprint"] != fingerprint
                or task["task_id"] != digest(encode({"input_fingerprint": fingerprint, "item_id": ident}))):
            raise LunaImportError("Analysis task fingerprint mismatch")
        image = _path(root, task["prepared_image_path"])
        if image != (spec_directory / source["prepared_path"]).resolve() or digest(image.read_bytes()) != task["prepared_image_sha256"]:
            raise LunaImportError("Prepared image path or content changed")
        evidence.append({"scope": "archive", "path": task["prepared_image_path"], "sha256": task["prepared_image_sha256"]})
        loaded = {}
        raw_hashes = {}
        for folder, field in (("contexts", "prompt_context_path"), ("visual-drafts", "visual_draft_path"), ("raw-results", "raw_result_path")):
            expected = f"{base}/{folder}/{style}.json"
            if task[field] != expected:
                raise LunaImportError("Task output path is not its assigned canary path")
            path = _path(root, task[field])
            document, raw = _json(path)
            loaded[folder], raw_hashes[folder] = document, digest(raw)
            expected_paths[folder].add(path)
            evidence.append({"scope": "archive", "path": task[field], "sha256": digest(raw)})
        context = {"schema_version": "image-luna-prompt-context-1", "id": ident, "style_id": style,
                   "full_prompt": prompt["full_prompt"], "prompt_sha256": task["prompt_sha256"]}
        if loaded["contexts"] != context:
            raise LunaImportError("Separate prompt context differs from full original prompt")
        result, draft = loaded["raw-results"], loaded["visual-drafts"]
        validate_result_schema(result, contracts["schema"])
        if not result["visual"]["description_ko"].strip():
            raise LunaImportError("Empty visual description is not a completed image analysis")
        if any(result[field] != task[field] for field in ("task_id", "input_fingerprint", "item_id", "style_id")):
            raise LunaImportError("Result belongs to another task or image")
        expected_draft = {"task_id": task["task_id"], "input_fingerprint": fingerprint,
                          "visual": result["visual"], "search_hints": result["search_hints"]}
        if draft != expected_draft:
            raise LunaImportError("Image-first visual draft differs from final visual/search fields")
        excerpt = result["visual"]["text_visible"]["excerpt"]
        if len(excerpt.split()) > 20:
            raise LunaImportError("Visible text excerpt exceeds the twenty-word bound")
        if result["visual"]["text_visible"]["status"] in {"none", "unclear"} and excerpt:
            raise LunaImportError("None or unclear visible text must not claim a transcription")
        results.append(result)
        bindings.append({"task_id": task["task_id"], "item_id": ident, "style_id": style, "input_fingerprint": fingerprint,
                         "image_sha256": task["prepared_image_sha256"], "prompt_sha256": task["prompt_sha256"],
                         "context_sha256": raw_hashes["contexts"], "visual_draft_sha256": raw_hashes["visual-drafts"],
                         "raw_result_sha256": raw_hashes["raw-results"]})
    for folder, expected in expected_paths.items():
        actual = {path.resolve() for path in (directory / folder).glob("*.json")}
        if actual != expected:
            raise LunaImportError("Unknown or incomplete canary output files")
    payload = {"schema_version": SCHEMA, "analysis_run_id": analysis_run_id, "source_run_id": source_run_id,
               "source_commit": data["commit"], "model_family": MODEL, "task_manifest_sha256": digest(manifest_raw),
               "schema_sha256": manifest["schema_sha256"], "instruction_sha256": manifest["instruction_sha256"],
               "candidate_status": "model_reported_candidate", "metadata_human_approved": False, "release_eligible": False,
               "visual_first_validation": "draft_final_content_equality_only",
               "execution_evidence_status": "separate_orchestrator_record_required",
               "model_calls_by_importer": 0, "embedding_calls": 0, "results": results, "task_bindings": bindings}
    _require_latest(db_path, source_run_id, data["commit"]["id"])
    return payload, evidence


def import_luna_results(root: Path, db_path: Path, analysis_run_id: str, *, apply: bool = False,
                        expected_commit_id: str | None = None) -> dict:
    root = Path(root).resolve()
    payload, evidence = _validated(root, db_path, analysis_run_id, expected_commit_id)
    raw = encode(payload)
    relative = f"{RELATIVE_ROOT}/{analysis_run_id}/imports/{payload['task_manifest_sha256']}"
    destination = _path(root, relative)
    receipt = {"schema_version": "image-luna-import-receipt-1", "status": "validated_candidates",
               "analysis_run_id": analysis_run_id, "source_commit_id": payload["source_commit"]["id"],
               "task_manifest_sha256": payload["task_manifest_sha256"], "validated_results_sha256": digest(raw),
               "source_files": evidence, "candidate_count": len(payload["results"]), "metadata_human_approved": False,
               "release_eligible": False, "model_calls_by_importer": 0, "embedding_calls": 0,
               "execution_evidence_status": "separate_orchestrator_record_required"}
    files = {"validated-results.json": raw, "receipt.json": encode(receipt)}
    summary = {"status": "dry_run", "analysis_run_id": analysis_run_id, "source_commit_id": payload["source_commit"]["id"],
               "candidate_count": len(payload["results"]), "output_path": relative,
               "validated_results_sha256": digest(raw), "metadata_human_approved": False,
               "model_calls_by_importer": 0, "embedding_calls": 0, "release_eligible": False}
    if destination.parent.exists():
        unexpected = [path for path in destination.parent.iterdir() if path.is_dir() and not path.name.startswith(".luna-import-") and path != destination]
        if unexpected:
            raise LunaImportError("This canary already has a different immutable manifest import")
    if destination.exists():
        if {path.name for path in destination.iterdir()} != set(files):
            raise LunaImportError("Existing analysis import has unexpected files")
        if any((destination / name).read_bytes() != content for name, content in files.items()):
            raise LunaImportError("Completed analysis import changed; do not overwrite or disguise a retry")
        _check_evidence(root, evidence)
        _require_latest(db_path, payload["source_run_id"], payload["source_commit"]["id"])
        return {**summary, "status": "unchanged"}
    if not apply:
        _check_evidence(root, evidence)
        return summary
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".luna-import-", dir=destination.parent)).resolve()
    if temporary.parent != destination.parent.resolve():
        raise LunaImportError("Unsafe temporary import directory")
    try:
        for name, content in files.items():
            with (temporary / name).open("xb") as stream:
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
        _check_evidence(root, evidence)
        _require_latest(db_path, payload["source_run_id"], payload["source_commit"]["id"])
        temporary.rename(destination)
    finally:
        if temporary.exists():
            for name in files:
                (temporary / name).unlink(missing_ok=True)
            temporary.rmdir()
    _require_latest(db_path, payload["source_run_id"], payload["source_commit"]["id"])
    return {**summary, "status": "prepared"}
