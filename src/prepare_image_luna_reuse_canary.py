"""Prepare ten new, group-aware Luna reuse-analysis tasks; no model/API call."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from image_rag_eval.approval_handoff import _committed, _require_latest, _validate_commit
from image_rag_eval.approved_library import build_prompt_catalog
from image_rag_eval.incremental_workflow import load_frozen_workflow

MODEL = "gpt-5.6-luna"
SCHEMA = "../00_CORE/schemas/image_luna_reuse_analysis_result.schema.json"
INSTRUCTIONS = "../00_CORE/templates/image_luna_reuse_analysis.instructions.md"
TAXONOMY = "../Reports/2026-09-04-02_활용목적형_이미지RAG_분류초안.json"
RELATIVE_ROOT = "data/private-research/image-rag-admin/luna-analysis"
TASK_COUNT = 10
DEFAULT_STYLES = (
    "CASE-074", "CASE-088", "CASE-093", "CASE-078", "G2AG-1002",
    "CASE-138", "CASE-132", "CASE-204", "CASE-223", "CASE-343",
)


def encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_object(path: Path) -> tuple[dict, bytes]:
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path.name}")
    return value, raw


def _workspace_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.parent):
        raise ValueError("Workspace contract escaped its scope")
    return path


def _taxonomy_context(taxonomy: dict, *, path: str, source_sha256: str) -> dict:
    contract = taxonomy.get("selection_contract")
    families = taxonomy.get("families")
    if (taxonomy.get("schema_version") != "image-reuse-taxonomy-draft-0.1"
            or taxonomy.get("status") != "proposal_needs_review"
            or taxonomy.get("effective_in_runtime") is not False
            or not isinstance(contract, dict) or not isinstance(families, list)):
        raise ValueError("Unsupported reuse taxonomy proposal")
    use_cases = []
    for family in families:
        for use_case in family.get("use_cases", []):
            use_cases.append({
                "family_id": family["id"],
                "family_label_ko": family["label"],
                "use_case_id": use_case["id"],
                "label_ko": use_case["label_ko"],
                "definition_ko": use_case["definition_ko"],
                "minimum_evidence_ko": use_case["minimum_evidence_ko"],
                "exclusion_ko": use_case["exclusion_ko"],
            })
    ids = [row["use_case_id"] for row in use_cases]
    if len(use_cases) != 40 or len(ids) != len(set(ids)):
        raise ValueError("Expected forty unique normalized use-case IDs")
    return {
        "schema_version": "image-reuse-taxonomy-model-context-1",
        "source_taxonomy_path": path,
        "source_taxonomy_sha256": source_sha256,
        "status": "analysis_context_from_human_accepted_proposal_not_runtime_taxonomy",
        "selection_contract": {
            "max_primary": contract["max_primary"],
            "max_secondary": contract["max_secondary"],
            "allow_abstain": contract["allow_abstain"],
            "fit_levels": contract["fit_levels"],
            "evidence_basis": ["image", "prompt", "image_and_prompt"],
            "reuse_modes": contract["reuse_modes"],
            "prompt_only_max_fit": contract["prompt_only_max_fit"],
            "direct_usage_rights_claim_allowed": contract["direct_usage_rights_claim_allowed"],
        },
        "use_cases": use_cases,
    }


def _prior_batch(root: Path, analysis_run_id: str) -> tuple[dict, list[dict]]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", analysis_run_id):
        raise ValueError("Invalid prior analysis run ID")
    directory = root / RELATIVE_ROOT / analysis_run_id
    manifest, manifest_raw = read_object(directory / "tasks.json")
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("Prior analysis run has no tasks")
    manifest_sha = sha(manifest_raw)
    imported = directory / "imports" / manifest_sha
    import_receipt, import_raw = read_object(imported / "receipt.json")
    execution_path = directory / "execution-receipt.json"
    token_path = directory / "token-usage-receipt.json"
    execution, execution_raw = read_object(execution_path)
    token, token_raw = read_object(token_path)
    if (import_receipt.get("task_manifest_sha256") != manifest_sha
            or import_receipt.get("candidate_count") != len(tasks)
            or import_receipt.get("metadata_human_approved") is not False
            or execution.get("analysis_run_id") != analysis_run_id
            or token.get("analysis_run_id") != analysis_run_id
            or token.get("completed_image_count") != len(tasks)
            or token.get("task_manifest_sha256") != manifest_sha):
        raise ValueError("Prior analysis evidence is incomplete or mismatched")
    reference = {
        "analysis_run_id": analysis_run_id,
        "task_manifest_sha256": manifest_sha,
        "validated_results_sha256": import_receipt["validated_results_sha256"],
        "import_receipt_sha256": sha(import_raw),
        "execution_receipt_sha256": sha(execution_raw),
        "token_usage_receipt_sha256": sha(token_raw),
        "item_ids": [task["item_id"] for task in tasks],
        "style_ids": [task["style_id"] for task in tasks],
        "source_image_sha256s": [task["source_image_sha256"] for task in tasks],
        "prepared_image_sha256s": [task["prepared_image_sha256"] for task in tasks],
    }
    return reference, tasks


def _group_map(normalized: dict) -> dict[str, dict]:
    mapping: dict[str, dict] = {}
    for group in normalized["approved_similarity_groups"]:
        representative = group["suggested_representative_id"]
        members = group["member_ids"]
        for item_id in members:
            if item_id in mapping:
                raise ValueError("Overlapping approved groups need human resolution before analysis selection")
            mapping[item_id] = {
                "group_id": group["candidate_id"],
                "representative_id": representative,
                "member_count": len(members),
                "selected_is_representative": item_id == representative,
            }
    return mapping


def prepare(
    root: Path,
    source_run_id: str,
    analysis_run_id: str,
    *,
    style_ids=DEFAULT_STYLES,
    prior_analysis_run_ids=("2026-09-03-luna-analysis-10-v1",),
    expected_commit_id: str | None = None,
    apply: bool = False,
) -> dict:
    root = Path(root).resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", analysis_run_id):
        raise ValueError("Invalid analysis run ID")
    if len(style_ids) != TASK_COUNT or len(set(style_ids)) != TASK_COUNT:
        raise ValueError("Select exactly ten distinct new styles")
    db = root / "data/private-research/image-rag-admin/state.sqlite3"
    committed = _committed(db, source_run_id)
    if not committed["commit"] or (expected_commit_id and committed["commit"]["id"] != expected_commit_id):
        raise ValueError("Latest committed approval does not match")
    spec = load_frozen_workflow(root, source_run_id)
    normalized = _validate_commit(spec, committed)
    approved = {item["id"] for item in normalized["private_front_export_items"]}
    available = {item["style_id"]: item for item in spec["items"] if item["id"] in approved}
    if len(available) != len(approved) or not set(style_ids).issubset(available):
        raise ValueError("Every selected style must resolve to one currently approved image")

    prior_references, prior_tasks = [], []
    for prior_id in prior_analysis_run_ids:
        reference, tasks = _prior_batch(root, prior_id)
        prior_references.append(reference)
        prior_tasks.extend(tasks)
    prior_values = {
        "item_id": {task["item_id"] for task in prior_tasks},
        "style_id": {task["style_id"] for task in prior_tasks},
        "source_image_sha256": {task["source_image_sha256"] for task in prior_tasks},
        "prepared_image_sha256": {task["prepared_image_sha256"] for task in prior_tasks},
    }
    for style in style_ids:
        image = available[style]
        checks = {"item_id": image["id"], "style_id": style,
                  "source_image_sha256": image["source_sha256"], "prepared_image_sha256": image["prepared_sha256"]}
        if any(value in prior_values[key] for key, value in checks.items()):
            raise ValueError("A selected image overlaps a prior analysis batch")

    prompts = build_prompt_catalog(root, spec)
    schema_path, instruction_path, taxonomy_path = (
        _workspace_path(root, SCHEMA), _workspace_path(root, INSTRUCTIONS), _workspace_path(root, TAXONOMY)
    )
    schema_sha, instruction_sha, taxonomy_sha = sha(schema_path.read_bytes()), sha(instruction_path.read_bytes()), sha(taxonomy_path.read_bytes())
    taxonomy, _ = read_object(taxonomy_path)
    taxonomy_context = _taxonomy_context(taxonomy, path=TAXONOMY, source_sha256=taxonomy_sha)
    taxonomy_context_raw = encode(taxonomy_context)
    taxonomy_context_sha = sha(taxonomy_context_raw)

    base = f"{RELATIVE_ROOT}/{analysis_run_id}"
    folder = (root / base).resolve()
    allowed_root = (root / RELATIVE_ROOT).resolve()
    if not folder.is_relative_to(allowed_root):
        raise ValueError("Private output escaped its scope")
    spec_directory = root / f"data/private-research/image-rag-canary/runs/{source_run_id}/group-workflow-v1"
    groups = _group_map(normalized)
    tasks, payloads = [], {"taxonomy-context.json": taxonomy_context_raw}
    for style in style_ids:
        image = available[style]
        path = (spec_directory / image["prepared_path"]).resolve()
        if not path.is_relative_to(root) or sha(path.read_bytes()) != image["prepared_sha256"]:
            raise ValueError("Prepared image changed or escaped archive")
        prompt = prompts[image["id"]]
        if prompt["status"] == "unavailable":
            raise ValueError("Original prompt source is unavailable")
        group = groups.get(image["id"], {"group_id": None, "representative_id": None, "member_count": 1,
                                         "selected_is_representative": True})
        if not group["selected_is_representative"]:
            raise ValueError("Grouped analysis candidates must be the committed representative")
        identity = {
            "model_family": MODEL,
            "source_image_sha256": image["source_sha256"],
            "prepared_image_sha256": image["prepared_sha256"],
            "prompt_sha256": prompt["prompt_sha256"],
            "schema_sha256": schema_sha,
            "instruction_sha256": instruction_sha,
            "taxonomy_context_sha256": taxonomy_context_sha,
            "visual_first_protocol": "2",
        }
        fingerprint = sha(encode(identity))
        task = {
            "task_id": sha(encode({"input_fingerprint": fingerprint, "item_id": image["id"]})),
            "input_fingerprint": fingerprint,
            "identity": identity,
            "item_id": image["id"],
            "style_id": style,
            "group_context": group,
            "prepared_image_path": path.relative_to(root).as_posix(),
            "prepared_image_sha256": image["prepared_sha256"],
            "source_image_sha256": image["source_sha256"],
            "prompt_sha256": prompt["prompt_sha256"],
            "prompt_context_path": f"{base}/contexts/{style}.json",
            "taxonomy_context_path": f"{base}/taxonomy-context.json",
            "visual_draft_path": f"{base}/visual-drafts/{style}.json",
            "raw_result_path": f"{base}/raw-results/{style}.json",
        }
        tasks.append(task)
        payloads[f"contexts/{style}.json"] = encode({
            "schema_version": "image-luna-prompt-context-1",
            "id": image["id"],
            "style_id": style,
            "full_prompt": prompt["full_prompt"],
            "prompt_sha256": prompt["prompt_sha256"],
        })
    cumulative = len(prior_tasks) + len(tasks)
    manifest = {
        "schema_version": "image-luna-reuse-analysis-tasks-2",
        "source_run_id": source_run_id,
        "analysis_run_id": analysis_run_id,
        "batch_index": len(prior_references) + 1,
        "source_commit": committed["commit"],
        "model_family": MODEL,
        "schema_path": SCHEMA,
        "schema_sha256": schema_sha,
        "instruction_path": INSTRUCTIONS,
        "instruction_sha256": instruction_sha,
        "taxonomy_path": TAXONOMY,
        "taxonomy_sha256": taxonomy_sha,
        "taxonomy_context_path": f"{base}/taxonomy-context.json",
        "taxonomy_context_sha256": taxonomy_context_sha,
        "prior_batches": prior_references,
        "tasks": tasks,
        "approved_library_count": len(approved),
        "selected_count": len(tasks),
        "cumulative_unique_target": cumulative,
        "group_representatives_only": True,
        "worker_partition": "one_isolated_luna_session_per_image",
        "token_metering_required": True,
        "embedding_calls_authorized": False,
        "model_execution_automatic": False,
        "human_memos_in_model_input": False,
        "group_context_in_model_input": False,
        "release_eligible": False,
    }
    payloads["tasks.json"] = encode(manifest)
    _require_latest(db, source_run_id, committed["commit"]["id"])
    existing = folder.exists()
    if existing:
        for relative, raw in payloads.items():
            target = folder / relative
            if not target.is_file() or target.read_bytes() != raw:
                raise ValueError("Existing reuse-analysis input differs; do not overwrite")
    elif apply:
        (folder / "contexts").mkdir(parents=True)
        (folder / "visual-drafts").mkdir()
        (folder / "raw-results").mkdir()
        for relative, raw in payloads.items():
            target = folder / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                output.write(raw)
        _require_latest(db, source_run_id, committed["commit"]["id"])
    return {
        "status": "unchanged" if existing else "prepared" if apply else "dry_run",
        "selected_images": len(tasks),
        "prior_analysed_images": len(prior_tasks),
        "cumulative_target": cumulative,
        "approved_library_images": len(approved),
        "tasks_path": f"{base}/tasks.json",
        "tasks_sha256": sha(payloads["tasks.json"]),
        "taxonomy_context_sha256": taxonomy_context_sha,
        "source_commit_id": committed["commit"]["id"],
        "model_family": MODEL,
        "model_inferences": 0,
        "embedding_calls": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--analysis-run-id", required=True)
    parser.add_argument("--expected-commit-id")
    parser.add_argument("--style-id", action="append")
    parser.add_argument("--prior-analysis-run-id", action="append")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = prepare(
        Path(__file__).resolve().parents[1],
        args.source_run_id,
        args.analysis_run_id,
        style_ids=tuple(args.style_id or DEFAULT_STYLES),
        prior_analysis_run_ids=tuple(args.prior_analysis_run_id or ("2026-09-03-luna-analysis-10-v1",)),
        expected_commit_id=args.expected_commit_id,
        apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
