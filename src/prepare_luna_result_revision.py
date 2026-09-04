"""Preserve validated generated outputs before an explicitly scoped Luna revision.

No existing file is removed or changed; this only creates immutable evidence.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from image_rag_eval.compact_projection import project_compact
from image_rag_eval.luna_analysis_import import _json, _path, digest, encode
from image_rag_eval.luna_compact import contract
from prepare_luna_full_library import BASE, RUN, immutable, read_manifest


def prepare_revision(root: Path, batch_id: str, *, apply=False) -> dict:
    manifest, raw_manifest = read_manifest(root)
    batch = next((row for row in manifest["batches"] if row["batch_id"] == batch_id), None)
    if batch is None:
        raise ValueError("Unknown explicitly assigned batch")
    tasks = {task["style_id"]: task for task in manifest["tasks"]}
    pinned, rows = contract(root), []
    for style in batch["style_ids"]:
        task = tasks[style]
        raw, raw_bytes = _json(_path(root, task["raw_result_path"]))
        draft, draft_bytes = _json(_path(root, task["visual_draft_path"]))
        context, _ = _json(_path(root, task["prompt_context_path"]))
        if digest(_path(root, task["prepared_image_path"]).read_bytes()) != task["prepared_image_sha256"]:
            raise ValueError("Source image changed")
        project_compact(raw, draft, pinned, expected_style_id=style, original_prompt=context["full_prompt"])
        if (context.get("style_id") != style or context.get("prompt_sha256") != task["prompt_sha256"]
                or digest(context["full_prompt"].encode()) != task["prompt_sha256"]):
            raise ValueError("Source prompt changed")
        rows.append({"style_id": style, "input_fingerprint": task["input_fingerprint"],
                     "raw_json": raw_bytes.decode("utf-8"), "raw_sha256": digest(raw_bytes),
                     "draft_json": draft_bytes.decode("utf-8"), "draft_sha256": digest(draft_bytes)})
    payload = {"schema_version": "luna-result-history-1", "analysis_run_id": RUN,
               "task_manifest_sha256": digest(raw_manifest), "batch_id": batch_id,
               "reason": "quality_flagged_luna_reanalysis", "records": rows,
               "metadata_human_approved": False, "release_eligible": False}
    raw = encode(payload)
    path = root / BASE / "result-history" / (digest(raw) + ".json")
    if apply:
        immutable(path, raw)
    return {"status": "preserved" if apply else "dry_run", "history_path": str(path),
            "history_sha256": digest(raw), "batch_id": batch_id,
            "styles": batch["style_ids"], "existing_files_changed": 0, "model_calls": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(prepare_revision(Path(__file__).resolve().parents[1], args.batch_id, apply=args.apply), ensure_ascii=False))
