"""Validate one complete ten-image reuse-oriented Luna batch, offline."""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from .approval_handoff import _committed, _require_latest, _validate_commit
from .approved_library import build_prompt_catalog
from .incremental_workflow import load_frozen_workflow
from .luna_analysis_import import (
    LunaImportError,
    _check_evidence,
    _json,
    _path,
    digest,
    encode,
    import_luna_results,
    validate_result_schema,
)

MODEL = "gpt-5.6-luna"
SCHEMA_PATH = "../00_CORE/schemas/image_luna_reuse_analysis_result.schema.json"
INSTRUCTION_PATH = "../00_CORE/templates/image_luna_reuse_analysis.instructions.md"
TAXONOMY_PATH = "../Reports/2026-09-04-02_활용목적형_이미지RAG_분류초안.json"
RELATIVE_ROOT = "data/private-research/image-rag-admin/luna-analysis"
TASK_COUNT = 10
RESULT_SCHEMA = "image-luna-reuse-validated-results-2"
SHA = re.compile(r"[a-f0-9]{64}\Z")
ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
MANIFEST_KEYS = {
    "schema_version", "source_run_id", "analysis_run_id", "batch_index", "source_commit", "model_family",
    "schema_path", "schema_sha256", "instruction_path", "instruction_sha256", "taxonomy_path", "taxonomy_sha256",
    "taxonomy_context_path", "taxonomy_context_sha256", "prior_batches", "tasks", "approved_library_count",
    "selected_count", "cumulative_unique_target", "group_representatives_only", "worker_partition",
    "token_metering_required", "embedding_calls_authorized", "model_execution_automatic", "human_memos_in_model_input",
    "group_context_in_model_input", "release_eligible",
}
TASK_KEYS = {
    "task_id", "input_fingerprint", "identity", "item_id", "style_id", "group_context", "prepared_image_path",
    "prepared_image_sha256", "source_image_sha256", "prompt_sha256", "prompt_context_path", "taxonomy_context_path",
    "visual_draft_path", "raw_result_path",
}
PRIOR_KEYS = {
    "analysis_run_id", "task_manifest_sha256", "validated_results_sha256", "import_receipt_sha256",
    "execution_receipt_sha256", "token_usage_receipt_sha256", "item_ids", "style_ids", "source_image_sha256s",
    "prepared_image_sha256s",
}


def _workspace(root: Path, relative: str) -> Path:
    if not relative.startswith("../"):
        raise LunaImportError("Workspace contract path must be archive-relative")
    return _path(root.parent, relative.removeprefix("../"))


def _group_map(normalized: dict) -> dict[str, dict]:
    result = {}
    for group in normalized["approved_similarity_groups"]:
        representative = group["suggested_representative_id"]
        for item_id in group["member_ids"]:
            if item_id in result:
                raise LunaImportError("Overlapping approved groups need human resolution")
            result[item_id] = {
                "group_id": group["candidate_id"],
                "representative_id": representative,
                "member_count": len(group["member_ids"]),
                "selected_is_representative": item_id == representative,
            }
    return result


def _prior_evidence(root: Path, db_path: Path, references: list[dict]) -> tuple[list[dict], dict[str, set[str]]]:
    evidence = []
    seen = {key: set() for key in ("item_id", "style_id", "source_image_sha256", "prepared_image_sha256")}
    if not isinstance(references, list) or len(references) != 1:
        raise LunaImportError("This second batch requires exactly one pinned prior batch")
    for reference in references:
        if not isinstance(reference, dict) or set(reference) != PRIOR_KEYS:
            raise LunaImportError("Invalid prior batch reference")
        run_id = reference["analysis_run_id"]
        checked = import_luna_results(root, db_path, run_id, apply=False)
        if checked.get("status") != "unchanged" or checked.get("candidate_count") != 10:
            raise LunaImportError("Prior batch must be a complete immutable import")
        directory = _path(root, f"{RELATIVE_ROOT}/{run_id}")
        manifest, manifest_raw = _json(directory / "tasks.json")
        manifest_sha = digest(manifest_raw)
        if manifest_sha != reference["task_manifest_sha256"]:
            raise LunaImportError("Prior task manifest changed")
        imported = directory / "imports" / manifest_sha
        import_receipt, import_raw = _json(imported / "receipt.json")
        execution, execution_raw = _json(directory / "execution-receipt.json")
        token, token_raw = _json(directory / "token-usage-receipt.json")
        if (import_receipt.get("validated_results_sha256") != reference["validated_results_sha256"]
                or digest(import_raw) != reference["import_receipt_sha256"]
                or digest(execution_raw) != reference["execution_receipt_sha256"]
                or digest(token_raw) != reference["token_usage_receipt_sha256"]
                or execution.get("analysis_run_id") != run_id or token.get("analysis_run_id") != run_id
                or token.get("task_manifest_sha256") != manifest_sha):
            raise LunaImportError("Prior import, execution, or token evidence changed")
        tasks = manifest.get("tasks", [])
        expected = {
            "item_ids": [task["item_id"] for task in tasks],
            "style_ids": [task["style_id"] for task in tasks],
            "source_image_sha256s": [task["source_image_sha256"] for task in tasks],
            "prepared_image_sha256s": [task["prepared_image_sha256"] for task in tasks],
        }
        if any(reference[key] != value for key, value in expected.items()):
            raise LunaImportError("Prior batch membership reference changed")
        for task in tasks:
            for key in seen:
                seen[key].add(task[key])
        for path, raw in ((directory / "tasks.json", manifest_raw), (imported / "receipt.json", import_raw),
                          (directory / "execution-receipt.json", execution_raw),
                          (directory / "token-usage-receipt.json", token_raw)):
            evidence.append({"scope": "archive", "path": path.relative_to(root).as_posix(), "sha256": digest(raw)})
    return evidence, seen


def _selection_rows(result: dict) -> list[dict]:
    selection = result["usage_selection"]
    primary, secondary = selection["primary"], selection["secondary"]
    if primary is None:
        if secondary or not isinstance(selection["abstention_reason_ko"], str) or not selection["abstention_reason_ko"].strip():
            raise LunaImportError("Abstention requires no selections and a reason")
        return []
    if selection["abstention_reason_ko"] is not None:
        raise LunaImportError("A selected primary cannot also claim abstention")
    return [primary, *secondary]


def _validate_selection(result: dict, taxonomy_ids: set[str]) -> None:
    rows = _selection_rows(result)
    ids = [row["use_case_id"] for row in rows]
    if len(ids) != len(set(ids)) or any(value not in taxonomy_ids for value in ids):
        raise LunaImportError("Usage selections must be distinct pinned taxonomy IDs")
    for row in rows:
        if row["evidence_basis"] == "prompt" and row["fit"] == "supported":
            raise LunaImportError("Prompt-only evidence cannot produce supported fit")
        if row["fit"] == "supported" and not row["visual_evidence_ko"]:
            raise LunaImportError("Supported fit requires explicit visible evidence")


def _task_identity(task: dict, manifest: dict) -> dict:
    return {
        "model_family": MODEL,
        "source_image_sha256": task["source_image_sha256"],
        "prepared_image_sha256": task["prepared_image_sha256"],
        "prompt_sha256": task["prompt_sha256"],
        "schema_sha256": manifest["schema_sha256"],
        "instruction_sha256": manifest["instruction_sha256"],
        "taxonomy_context_sha256": manifest["taxonomy_context_sha256"],
        "visual_first_protocol": "2",
    }


def _validated(root: Path, db_path: Path, analysis_run_id: str, expected_commit_id: str | None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", analysis_run_id):
        raise LunaImportError("Invalid analysis run ID")
    base = f"{RELATIVE_ROOT}/{analysis_run_id}"
    directory = _path(root, base)
    manifest, manifest_raw = _json(directory / "tasks.json")
    if (set(manifest) != MANIFEST_KEYS or manifest.get("schema_version") != "image-luna-reuse-analysis-tasks-2"
            or manifest.get("analysis_run_id") != analysis_run_id or manifest.get("model_family") != MODEL
            or manifest.get("schema_path") != SCHEMA_PATH or manifest.get("instruction_path") != INSTRUCTION_PATH
            or manifest.get("taxonomy_path") != TAXONOMY_PATH or manifest.get("batch_index") != 2
            or manifest.get("group_representatives_only") is not True
            or manifest.get("worker_partition") != "one_isolated_luna_session_per_image"
            or manifest.get("token_metering_required") is not True
            or any(manifest.get(key) is not False for key in ("embedding_calls_authorized", "model_execution_automatic",
                "human_memos_in_model_input", "group_context_in_model_input", "release_eligible"))):
        raise LunaImportError("Unsupported or unsafe reuse-analysis manifest")
    tasks = manifest["tasks"]
    if (not isinstance(tasks, list) or len(tasks) != TASK_COUNT or manifest.get("selected_count") != TASK_COUNT
            or manifest.get("cumulative_unique_target") != 20):
        raise LunaImportError("This v2 importer requires ten new tasks and a cumulative target of twenty")
    evidence = [{"scope": "archive", "path": f"{base}/tasks.json", "sha256": digest(manifest_raw)}]
    prior_evidence, prior_seen = _prior_evidence(root, db_path, manifest["prior_batches"])
    evidence.extend(prior_evidence)

    schema = None
    for name, relative in (("schema", SCHEMA_PATH), ("instruction", INSTRUCTION_PATH), ("taxonomy", TAXONOMY_PATH)):
        path = _workspace(root, relative)
        raw = path.read_bytes()
        if digest(raw) != manifest[name + "_sha256"]:
            raise LunaImportError("Pinned reuse-analysis contract changed")
        evidence.append({"scope": "workspace", "path": relative.removeprefix("../"), "sha256": digest(raw)})
        if name == "schema":
            schema = _json(path)[0]
    taxonomy_context_path = _path(root, manifest["taxonomy_context_path"])
    taxonomy_context, taxonomy_context_raw = _json(taxonomy_context_path)
    if (manifest["taxonomy_context_path"] != f"{base}/taxonomy-context.json"
            or digest(taxonomy_context_raw) != manifest["taxonomy_context_sha256"]
            or taxonomy_context.get("schema_version") != "image-reuse-taxonomy-model-context-1"
            or taxonomy_context.get("source_taxonomy_path") != TAXONOMY_PATH
            or taxonomy_context.get("source_taxonomy_sha256") != manifest["taxonomy_sha256"]):
        raise LunaImportError("Pinned model taxonomy context changed")
    use_cases = taxonomy_context.get("use_cases")
    taxonomy_ids = {row.get("use_case_id") for row in use_cases} if isinstance(use_cases, list) else set()
    if len(taxonomy_ids) != 40 or None in taxonomy_ids:
        raise LunaImportError("Taxonomy context must contain forty unique IDs")
    evidence.append({"scope": "archive", "path": manifest["taxonomy_context_path"], "sha256": digest(taxonomy_context_raw)})

    source_run_id = manifest["source_run_id"]
    data = _committed(db_path, source_run_id)
    if not data["commit"] or data["commit"] != manifest["source_commit"]:
        raise LunaImportError("Analysis manifest is not bound to the latest committed approval")
    if expected_commit_id is not None and expected_commit_id != data["commit"]["id"]:
        raise LunaImportError("Expected source commit is stale")
    spec = load_frozen_workflow(root, source_run_id)
    normalized = _validate_commit(spec, data)
    approved = {item["id"] for item in normalized["private_front_export_items"]}
    if manifest.get("approved_library_count") != len(approved):
        raise LunaImportError("Approved-library count changed")
    originals = {item["id"]: item for item in spec["items"]}
    prompts = build_prompt_catalog(root, spec)
    groups = _group_map(normalized)
    spec_directory = _path(root, f"data/private-research/image-rag-canary/runs/{source_run_id}/group-workflow-v1")
    for name in ("image-group-workflow.spec.json", "source-bindings.json", "build-receipt.json"):
        path = spec_directory / name
        evidence.append({"scope": "archive", "path": path.relative_to(root).as_posix(), "sha256": digest(path.read_bytes())})

    seen = {key: set() for key in ("task_id", "item_id", "style_id", "source_image_sha256", "prepared_image_sha256")}
    expected_paths = {"contexts": set(), "visual-drafts": set(), "raw-results": set()}
    results, bindings = [], []
    for task in tasks:
        if not isinstance(task, dict) or set(task) != TASK_KEYS:
            raise LunaImportError("Unknown or missing reuse-analysis task properties")
        for field in ("task_id", "input_fingerprint", "prepared_image_sha256", "source_image_sha256", "prompt_sha256"):
            if not isinstance(task[field], str) or not SHA.fullmatch(task[field]):
                raise LunaImportError("Invalid reuse-analysis task digest")
        for field in ("item_id", "style_id"):
            if not isinstance(task[field], str) or not ID.fullmatch(task[field]):
                raise LunaImportError("Invalid reuse-analysis task identity")
        for key in seen:
            value = task[key]
            if value in seen[key] or (key in prior_seen and value in prior_seen[key]):
                raise LunaImportError("New task overlaps this or a prior analysis batch")
            seen[key].add(value)
        item_id, style = task["item_id"], task["style_id"]
        if item_id not in approved:
            raise LunaImportError("Analysis task is not currently approved")
        source, prompt = originals[item_id], prompts[item_id]
        if (source["style_id"] != style or source["source_sha256"] != task["source_image_sha256"]
                or source["prepared_sha256"] != task["prepared_image_sha256"]
                or prompt["status"] == "unavailable" or prompt["prompt_sha256"] != task["prompt_sha256"]):
            raise LunaImportError("Task image or original prompt binding mismatch")
        expected_group = groups.get(item_id, {"group_id": None, "representative_id": None, "member_count": 1,
                                               "selected_is_representative": True})
        if task["group_context"] != expected_group or task["group_context"]["selected_is_representative"] is not True:
            raise LunaImportError("Task is not the current committed group representative")
        identity = _task_identity(task, manifest)
        fingerprint = digest(encode(identity))
        if (task["identity"] != identity or task["input_fingerprint"] != fingerprint
                or task["task_id"] != digest(encode({"input_fingerprint": fingerprint, "item_id": item_id}))):
            raise LunaImportError("Reuse-analysis task fingerprint mismatch")
        image = _path(root, task["prepared_image_path"])
        if image != (spec_directory / source["prepared_path"]).resolve() or digest(image.read_bytes()) != task["prepared_image_sha256"]:
            raise LunaImportError("Prepared image changed")
        evidence.append({"scope": "archive", "path": task["prepared_image_path"], "sha256": task["prepared_image_sha256"]})

        loaded, raw_hashes = {}, {}
        for folder, field in (("contexts", "prompt_context_path"), ("visual-drafts", "visual_draft_path"),
                              ("raw-results", "raw_result_path")):
            expected = f"{base}/{folder}/{style}.json"
            if task[field] != expected:
                raise LunaImportError("Task output path is not its assigned path")
            path = _path(root, expected)
            document, raw = _json(path)
            loaded[folder], raw_hashes[folder] = document, digest(raw)
            expected_paths[folder].add(path)
            evidence.append({"scope": "archive", "path": expected, "sha256": digest(raw)})
        expected_context = {"schema_version": "image-luna-prompt-context-1", "id": item_id, "style_id": style,
                            "full_prompt": prompt["full_prompt"], "prompt_sha256": task["prompt_sha256"]}
        if loaded["contexts"] != expected_context:
            raise LunaImportError("Separate prompt context changed")
        result, draft = loaded["raw-results"], loaded["visual-drafts"]
        validate_result_schema(result, schema)
        if any(result[field] != task[field] for field in ("task_id", "input_fingerprint", "item_id", "style_id")):
            raise LunaImportError("Result belongs to another task")
        if draft != {"task_id": task["task_id"], "input_fingerprint": fingerprint, "visual": result["visual"]}:
            raise LunaImportError("Image-first visual draft differs from the final visual block")
        visual = result["visual"]
        if (not visual["description_ko"].strip() or not visual["styles"]
                or not visual["background"]["description_ko"].strip()
                or not visual["background"]["evidence_ko"].strip()
                or not visual["editability"]["evidence_ko"].strip()):
            raise LunaImportError("Style, background, and editability evidence are mandatory")
        excerpt = visual["text_visible"]["excerpt"]
        if len(excerpt.split()) > 20 or (visual["text_visible"]["status"] in {"none", "unclear"} and excerpt):
            raise LunaImportError("Visible text excerpt violates the bounded OCR rule")
        _validate_selection(result, taxonomy_ids)
        results.append(result)
        bindings.append({
            "task_id": task["task_id"], "item_id": item_id, "style_id": style,
            "input_fingerprint": fingerprint, "group_context": task["group_context"],
            "image_sha256": task["prepared_image_sha256"], "prompt_sha256": task["prompt_sha256"],
            "context_sha256": raw_hashes["contexts"], "visual_draft_sha256": raw_hashes["visual-drafts"],
            "raw_result_sha256": raw_hashes["raw-results"],
        })
    for folder, expected in expected_paths.items():
        actual = {path.resolve() for path in (directory / folder).glob("*.json")}
        if actual != expected:
            raise LunaImportError("Unknown or incomplete reuse-analysis output files")
    if len(seen["item_id"] | prior_seen["item_id"]) != manifest["cumulative_unique_target"]:
        raise LunaImportError("Cumulative unique analysis target mismatch")
    payload = {
        "schema_version": RESULT_SCHEMA,
        "analysis_run_id": analysis_run_id,
        "source_run_id": source_run_id,
        "source_commit": data["commit"],
        "model_family": MODEL,
        "task_manifest_sha256": digest(manifest_raw),
        "schema_sha256": manifest["schema_sha256"],
        "instruction_sha256": manifest["instruction_sha256"],
        "taxonomy_sha256": manifest["taxonomy_sha256"],
        "taxonomy_context_sha256": manifest["taxonomy_context_sha256"],
        "prior_batches": manifest["prior_batches"],
        "cumulative_unique_count": manifest["cumulative_unique_target"],
        "candidate_status": "model_reported_candidate",
        "metadata_human_approved": False,
        "release_eligible": False,
        "visual_first_validation": "draft_final_content_equality_only",
        "execution_evidence_status": "separate_token_and_orchestrator_receipts_required",
        "model_calls_by_importer": 0,
        "embedding_calls": 0,
        "results": results,
        "task_bindings": bindings,
    }
    _require_latest(db_path, source_run_id, data["commit"]["id"])
    return payload, evidence


def import_luna_reuse_results(root: Path, db_path: Path, analysis_run_id: str, *, apply: bool = False,
                              expected_commit_id: str | None = None) -> dict:
    root = Path(root).resolve()
    payload, evidence = _validated(root, db_path, analysis_run_id, expected_commit_id)
    raw = encode(payload)
    relative = f"{RELATIVE_ROOT}/{analysis_run_id}/imports/{payload['task_manifest_sha256']}"
    destination = _path(root, relative)
    receipt = {
        "schema_version": "image-luna-reuse-import-receipt-2",
        "status": "validated_candidates",
        "analysis_run_id": analysis_run_id,
        "source_commit_id": payload["source_commit"]["id"],
        "task_manifest_sha256": payload["task_manifest_sha256"],
        "validated_results_sha256": digest(raw),
        "source_files": evidence,
        "candidate_count": len(payload["results"]),
        "cumulative_unique_count": payload["cumulative_unique_count"],
        "metadata_human_approved": False,
        "release_eligible": False,
        "model_calls_by_importer": 0,
        "embedding_calls": 0,
        "execution_evidence_status": "separate_token_and_orchestrator_receipts_required",
    }
    files = {"validated-results.json": raw, "receipt.json": encode(receipt)}
    summary = {
        "status": "dry_run", "analysis_run_id": analysis_run_id,
        "source_commit_id": payload["source_commit"]["id"], "candidate_count": len(payload["results"]),
        "cumulative_unique_count": payload["cumulative_unique_count"], "output_path": relative,
        "validated_results_sha256": digest(raw), "metadata_human_approved": False,
        "model_calls_by_importer": 0, "embedding_calls": 0, "release_eligible": False,
    }
    if destination.parent.exists():
        unexpected = [path for path in destination.parent.iterdir()
                      if path.is_dir() and not path.name.startswith(".luna-reuse-import-") and path != destination]
        if unexpected:
            raise LunaImportError("This batch already has a different immutable manifest import")
    if destination.exists():
        if {path.name for path in destination.iterdir()} != set(files):
            raise LunaImportError("Existing reuse-analysis import has unexpected files")
        if any((destination / name).read_bytes() != content for name, content in files.items()):
            raise LunaImportError("Completed reuse-analysis import changed")
        _check_evidence(root, evidence)
        _require_latest(db_path, payload["source_run_id"], payload["source_commit"]["id"])
        return {**summary, "status": "unchanged"}
    if not apply:
        _check_evidence(root, evidence)
        return summary
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".luna-reuse-import-", dir=destination.parent)).resolve()
    if temporary.parent != destination.parent.resolve():
        raise LunaImportError("Unsafe temporary import directory")
    try:
        for name, content in files.items():
            with (temporary / name).open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
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


__all__ = ["LunaImportError", "import_luna_reuse_results"]
