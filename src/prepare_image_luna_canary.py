"""Prepare a committed-image Luna canary; no model call or embedding operation."""
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
SCHEMA = "../00_CORE/schemas/image_luna_analysis_result.schema.json"
INSTRUCTIONS = "../00_CORE/templates/image_luna_analysis.instructions.md"
DEFAULT_STYLES = ("DAV490-019", "BST-001", "BST-002", "CASE-001", "CASE-003", "CASE-005", "CASE-006", "CASE-009", "CASE-158", "CASE-037")


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def prepare(root, source_run_id, analysis_run_id, *, style_ids=DEFAULT_STYLES, expected_commit_id=None, apply=False):
    root = Path(root).resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", analysis_run_id):
        raise ValueError("Invalid analysis run ID")
    if not 1 <= len(style_ids) <= 20 or len(set(style_ids)) != len(style_ids):
        raise ValueError("Select one to twenty distinct styles")
    db = root / "data/private-research/image-rag-admin/state.sqlite3"
    committed = _committed(db, source_run_id)
    if not committed["commit"] or (expected_commit_id and committed["commit"]["id"] != expected_commit_id):
        raise ValueError("Latest committed approval does not match")
    spec = load_frozen_workflow(root, source_run_id)
    normalized = _validate_commit(spec, committed)
    approved = {item["id"] for item in normalized["private_front_export_items"]}
    available = {item["style_id"]: item for item in spec["items"] if item["id"] in approved}
    if len(available) != len(approved):
        raise ValueError("Style IDs must resolve unambiguously")
    if not set(style_ids).issubset(available):
        raise ValueError("A selected style is not currently approved")
    prompts = build_prompt_catalog(root, spec)
    schema_sha, instruction_sha = sha((root / SCHEMA).read_bytes()), sha((root / INSTRUCTIONS).read_bytes())
    base = f"data/private-research/image-rag-admin/luna-analysis/{analysis_run_id}"
    folder = (root / base).resolve()
    if not folder.is_relative_to(root / "data/private-research/image-rag-admin/luna-analysis"):
        raise ValueError("Private output escaped its scope")
    spec_directory = root / f"data/private-research/image-rag-canary/runs/{source_run_id}/group-workflow-v1"
    tasks, payloads = [], {}
    for style in style_ids:
        image = available[style]
        path = (spec_directory / image["prepared_path"]).resolve()
        if not path.is_relative_to(root) or sha(path.read_bytes()) != image["prepared_sha256"]:
            raise ValueError("Prepared image changed or escaped archive")
        prompt = prompts[image["id"]]
        if prompt["status"] == "unavailable":
            raise ValueError("Original prompt source is unavailable")
        identity = {"model_family": MODEL, "source_image_sha256": image["source_sha256"],
                    "prepared_image_sha256": image["prepared_sha256"], "prompt_sha256": prompt["prompt_sha256"],
                    "schema_sha256": schema_sha, "instruction_sha256": instruction_sha,
                    "visual_first_protocol": "1"}
        fingerprint = sha(encode(identity))
        task = {"task_id": sha(encode({"input_fingerprint": fingerprint, "item_id": image["id"]})),
                "input_fingerprint": fingerprint, "identity": identity, "item_id": image["id"], "style_id": style,
                "prepared_image_path": path.relative_to(root).as_posix(),
                "prepared_image_sha256": image["prepared_sha256"], "source_image_sha256": image["source_sha256"],
                "prompt_sha256": prompt["prompt_sha256"],
                "prompt_context_path": f"{base}/contexts/{style}.json",
                "visual_draft_path": f"{base}/visual-drafts/{style}.json",
                "raw_result_path": f"{base}/raw-results/{style}.json"}
        tasks.append(task)
        payloads[f"contexts/{style}.json"] = encode({"schema_version": "image-luna-prompt-context-1",
            "id": image["id"], "style_id": style, "full_prompt": prompt["full_prompt"], "prompt_sha256": prompt["prompt_sha256"]})
    manifest = {"schema_version": "image-luna-analysis-tasks-1", "source_run_id": source_run_id,
                "analysis_run_id": analysis_run_id, "source_commit": committed["commit"], "model_family": MODEL,
                "schema_path": SCHEMA, "schema_sha256": schema_sha, "instruction_path": INSTRUCTIONS,
                "instruction_sha256": instruction_sha, "tasks": tasks,
                "approved_library_count": len(approved), "selected_count": len(tasks),
                "embedding_calls_authorized": False, "model_execution_automatic": False,
                "human_memos_in_model_input": False, "release_eligible": False}
    payloads["tasks.json"] = encode(manifest)
    _require_latest(db, source_run_id, committed["commit"]["id"])
    existing = folder.exists()
    if existing:
        for relative, raw in payloads.items():
            if not (folder / relative).is_file() or (folder / relative).read_bytes() != raw:
                raise ValueError("Existing canary input differs; do not overwrite")
    elif apply:
        (folder / "contexts").mkdir(parents=True)
        for name in ("visual-drafts", "raw-results"):
            (folder / name).mkdir()
        for relative, raw in payloads.items():
            with (folder / relative).open("xb") as output:
                output.write(raw)
        _require_latest(db, source_run_id, committed["commit"]["id"])
    return {"status": "unchanged" if existing else "prepared" if apply else "dry_run", "selected_images": len(tasks),
            "approved_library_images": len(approved), "tasks_path": f"{base}/tasks.json",
            "tasks_sha256": sha(payloads["tasks.json"]), "source_commit_id": committed["commit"]["id"],
            "model_family": MODEL, "model_inferences": 0, "embedding_calls": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--analysis-run-id", required=True)
    parser.add_argument("--expected-commit-id")
    parser.add_argument("--style-id", action="append")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = prepare(Path(__file__).resolve().parents[1], args.source_run_id, args.analysis_run_id,
                     style_ids=tuple(args.style_id or DEFAULT_STYLES), expected_commit_id=args.expected_commit_id, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
