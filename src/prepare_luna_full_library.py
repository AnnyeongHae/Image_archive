"""Freeze the approved library for resumable compact Luna analysis (offline)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from image_rag_eval.approval_handoff import _committed, _require_latest, _validate_commit
from image_rag_eval.approved_library import build_prompt_catalog
from image_rag_eval.incremental_workflow import load_frozen_workflow
from image_rag_eval.luna_analysis_import import _json, _path, digest, encode
from image_rag_eval.luna_compact import contract, worker_message, validate_compact
from image_rag_eval.compact_projection import project_compact
from image_rag_eval.metadata_candidate_store import load_validated_sources

RUN = "2026-09-04-luna-full-library-v3"
SOURCE_RUN = "2026-09-03-incremental-review-500-v1"
BASE = "data/private-research/image-rag-admin/luna-analysis/" + RUN
EXPECTED_MANIFEST_SHA256 = "e6314f928360b755e9f24a5871fc9fe143ef2304ba014928fe21e856624d7395"


def read_manifest(root: Path) -> tuple[dict, bytes]:
    path = root / BASE / "tasks.json"
    if not path.is_file() or not 0 < path.stat().st_size <= 16 * 1024 * 1024:
        raise ValueError("Missing or oversized full-library manifest")
    raw = path.read_bytes()
    if digest(raw) != EXPECTED_MANIFEST_SHA256:
        raise ValueError("Frozen full-library manifest hash changed")
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate manifest property")
            result[key] = value
        return result
    def reject_constant(value):
        raise ValueError("Non-finite manifest number")
    value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_pairs, parse_constant=reject_constant)
    if not isinstance(value, dict) or value.get("schema_version") != "luna-full-library-3":
        raise ValueError("Unexpected manifest contract")
    return value, raw


def immutable(path: Path, raw: bytes) -> None:
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("Refusing to change frozen artifact: " + path.name)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(raw)


def prepare(root: Path, *, apply: bool = False) -> dict:
    root = root.resolve()
    db = root / "data/private-research/image-rag-admin/state.sqlite3"
    committed = _committed(db, SOURCE_RUN)
    spec = load_frozen_workflow(root, SOURCE_RUN)
    normalized = _validate_commit(spec, committed)
    approved = {r["id"]: r for r in normalized["private_front_export_items"]}
    prompts = build_prompt_catalog(root, spec)
    pinned = contract(root)
    # Re-run the frozen import validators; legacy reuse is NOT a compact cache hit.
    load_validated_sources(root, db)
    legacy_tasks = {}
    for name in ("2026-09-03-luna-analysis-10-v1", "2026-09-04-luna-reuse-analysis-10-v2"):
        manifest, raw = _json(root / "data/private-research/image-rag-admin/luna-analysis" / name / "tasks.json")
        for task in manifest["tasks"]:
            legacy_tasks[task["item_id"]] = {"analysis_run_id": name, "task_manifest_sha256": digest(raw), "task": task}
    spec_dir = root / f"data/private-research/image-rag-canary/runs/{SOURCE_RUN}/group-workflow-v1"
    tasks, records, payloads = [], [], {}
    by_style = set()
    for item in spec["items"]:
        ident, style = item["id"], item["style_id"]
        if style in by_style:
            raise ValueError("Ambiguous Style ID")
        by_style.add(style)
        prompt = prompts[ident]
        prepared = (spec_dir / item["prepared_path"]).resolve()
        if not prepared.is_relative_to(root) or digest(prepared.read_bytes()) != item["prepared_sha256"]:
            raise ValueError("Prepared source changed")
        record = {**item, "prepared_image_path": prepared.relative_to(root).as_posix(),
                  "image_approved": ident in approved, "approval": approved.get(ident), "original_prompt": prompt}
        records.append(record)
        if ident not in approved:
            continue
        if prompt["status"] == "unavailable":
            raise ValueError("Missing exact source prompt: " + style)
        prior = legacy_tasks.get(ident)
        if prior:
            old = prior["task"]
            if (old["prepared_image_sha256"] != item["prepared_sha256"] or
                    old["source_image_sha256"] != item["source_sha256"] or old["prompt_sha256"] != prompt["prompt_sha256"]):
                raise ValueError("Legacy input changed")
        identity = {"model_family": "gpt-5.6-luna", "prepared_image_sha256": item["prepared_sha256"],
                    "source_image_sha256": item["source_sha256"], "prompt_sha256": prompt["prompt_sha256"],
                    "prefix_sha256": pinned["prefix_sha256"], "visual_first_protocol": "3"}
        fingerprint = digest(encode(identity))
        task = {"item_id": ident, "style_id": style, "input_fingerprint": fingerprint, "identity": identity,
                "prepared_image_path": record["prepared_image_path"], "prompt_sha256": prompt["prompt_sha256"],
                "source_image_sha256": item["source_sha256"], "prepared_image_sha256": item["prepared_sha256"],
                "prompt_context_path": f"{BASE}/contexts/{style}.json",
                "visual_draft_path": f"{BASE}/visual-drafts/{style}.json",
                "raw_result_path": f"{BASE}/raw-results/{style}.json",
                "analysis_mode": "legacy_reuse" if prior else "new_compact", "legacy": prior}
        tasks.append(task)
        payloads[f"contexts/{style}.json"] = encode({"style_id": style, "full_prompt": prompt["full_prompt"],
                                                     "prompt_sha256": prompt["prompt_sha256"]})
    pending = sorted((t for t in tasks if t["analysis_mode"] == "new_compact"), key=lambda t: t["style_id"])
    batches = []
    # Small independently completed batches; inspection can split hard cases to one.
    for start in range(0, len(pending), 3):
        chunk = pending[start:start + 3]
        batch_id = f"batch-{len(batches) + 1:03d}"
        assignments = [{key: t[key] if key == "style_id" else str(root / t[key])
                        for key in ("style_id", "prepared_image_path", "prompt_context_path", "visual_draft_path", "raw_result_path")}
                       for t in chunk]
        message = worker_message(pinned, assignments)
        payloads[f"batches/{batch_id}.txt"] = message.encode("utf-8")
        batches.append({"batch_id": batch_id, "style_ids": [t["style_id"] for t in chunk],
                        "message_path": f"{BASE}/batches/{batch_id}.txt", "message_sha256": digest(message.encode("utf-8"))})
    manifest = {"schema_version": "luna-full-library-3", "analysis_run_id": RUN, "source_run_id": SOURCE_RUN,
                "source_commit": committed["commit"], "spec_sha256": spec["spec_sha256"],
                "model_family": "gpt-5.6-luna", "prefix_sha256": pinned["prefix_sha256"],
                "schema_sha256": pinned["schema_sha256"], "taxonomy_sha256": pinned["taxonomy_sha256"],
                "source_records": records, "approved_groups": normalized["approved_similarity_groups"],
                "normalized_decisions": normalized, "tasks": tasks, "batches": batches,
                "counts": {"source_records": len(records), "approved_images": len(tasks),
                           "legacy_reused": len(tasks) - len(pending), "new_compact": len(pending), "batches": len(batches)},
                "scope": "current_committed_library_all_members_not_unreviewed_archive",
                "legacy_reuse_is_compact_cache_hit": False, "metadata_human_approved": False,
                "release_eligible": False, "embedding_calls": 0}
    payloads["tasks.json"] = encode(manifest)
    _require_latest(db, SOURCE_RUN, committed["commit"]["id"])
    if digest(payloads["tasks.json"]) != EXPECTED_MANIFEST_SHA256:
        raise ValueError("Approved source or contract changed; create a new explicitly reviewed run")
    # Check every existing target before writing even one new context.
    for relative, raw in payloads.items():
        target = root / BASE / relative
        if target.exists() and target.read_bytes() != raw:
            raise ValueError("Existing full-library artifact differs: " + relative)
    if apply:
        for relative, raw in payloads.items():
            immutable(root / BASE / relative, raw)
        for sub in ("raw-results", "visual-drafts", "execution"):
            (root / BASE / sub).mkdir(exist_ok=True)
        _require_latest(db, SOURCE_RUN, committed["commit"]["id"])
    return {"status": "prepared" if apply else "dry_run", **manifest["counts"], "tasks_path": f"{BASE}/tasks.json",
            "tasks_sha256": digest(payloads["tasks.json"]), "source_commit_id": committed["commit"]["id"]}


def validate_progress(root: Path) -> dict:
    manifest, manifest_raw = read_manifest(root)
    pinned = contract(root)
    if pinned["prefix_sha256"] != manifest["prefix_sha256"]:
        raise ValueError("Current contract drift")
    if "source_commit" in manifest:
        _require_latest(root / "data/private-research/image-rag-admin/state.sqlite3", SOURCE_RUN, manifest["source_commit"]["id"])
    complete, missing, invalid, input_invalid, normalized = [], [], [], [], []
    for task in manifest["tasks"]:
        if task["analysis_mode"] == "legacy_reuse":
            continue
        try:
            context, _ = _json(_path(root, task["prompt_context_path"]))
            if digest(_path(root, task["prepared_image_path"]).read_bytes()) != task["prepared_image_sha256"]:
                raise ValueError("Image hash mismatch")
            if digest(context["full_prompt"].encode("utf-8")) != task["prompt_sha256"]:
                raise ValueError("Prompt hash mismatch")
        except (ValueError, KeyError, OSError) as exc:
            input_invalid.append({"style_id": task["style_id"], "error": str(exc)[:300]})
            continue
        result_path = _path(root, task["raw_result_path"])
        if not result_path.exists():
            missing.append(task["style_id"])
            continue
        try:
            result, _ = _json(result_path)
            draft, _ = _json(_path(root, task["visual_draft_path"]))
            projection = project_compact(result, draft, pinned, expected_style_id=task["style_id"],
                                         original_prompt=context["full_prompt"])
            if projection["normalization"]:
                normalized.append(task["style_id"])
            complete.append(task["style_id"])
        except (ValueError, KeyError, OSError) as exc:
            invalid.append({"style_id": task["style_id"], "error": str(exc)[:300]})
    completed_set = set(complete)
    unfinished = [b for b in manifest.get("batches", []) if not set(b["style_ids"]) <= completed_set]
    input_invalid_set = {i["style_id"] for i in input_invalid}
    return {"analysis_run_id": RUN, "tasks_sha256": digest(manifest_raw), "legacy_reused": manifest["counts"]["legacy_reused"],
            "new_valid": len(complete), "missing": len(missing), "invalid": invalid, "input_invalid": input_invalid, "completed_styles": complete,
            "literal_normalized_styles": normalized, "raw_strict_valid_count": len(complete) - len(normalized),
            "next_styles": [s for b in unfinished for s in b["style_ids"] if s not in completed_set | input_invalid_set][:15],
            "next_batch_ids": [b["batch_id"] for b in unfinished if not set(b["style_ids"]) & input_invalid_set][:10],
            "remaining_batches": len(unfinished), "metadata_human_approved": False, "release_eligible": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    print(json.dumps(validate_progress(root) if args.progress else prepare(root, apply=args.apply), ensure_ascii=False))
