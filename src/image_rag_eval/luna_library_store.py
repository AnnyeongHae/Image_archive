"""Read verified sources into immutable full-library SQLite checkpoints.

Each checkpoint has a content-addressed directory. Missing model results and
missing telemetry are explicit states, never fabricated completion or zero use.
"""
from __future__ import annotations

import json
import copy
import os
import sqlite3
import tempfile
from pathlib import Path

from .approval_handoff import _archive, _committed, _key, _require_latest, _validate_commit, _verify_existing
from .incremental_workflow import load_frozen_workflow
from .luna_analysis_import import _path, digest, encode
from .luna_compact import contract
from .compact_projection import project_compact
from .metadata_candidate_store import _logical_dump, _strings, load_validated_sources, normalize_term
from .remaining_review import _verify_sources
from .prompt_arguments import extract_arguments

SCHEMA = "luna-library-store-3"
MIGRATION = "db/metadata/0002_luna_library.sqlite.sql"
SNAPSHOTS = "data/private-research/image-rag-admin/metadata-candidates/full-library-v3/snapshots"
REMAINING = "data/private-research/image-rag-canary/runs/2026-09-03-case-final-155-v1/remaining-review-v1"
OLD_DATABASE = "data/private-research/image-rag-admin/metadata-candidates/v1/candidates.sqlite3"


class LibraryStoreError(ValueError):
    pass


def validate_enum_repair(original: dict, effective: dict, replacements: list) -> None:
    """Accept only recorded enum spelling mappings, never observation rewrites."""
    allowed = {("medium", "digital_illustration", "illustration"),
               ("medium", "illustrated_diagram", "illustration"),
               ("setting", "illustrated_infographic", "information_layout"),
               ("setting", "architectural_educational_plate", "information_layout")}
    expected = copy.deepcopy(original)
    if not replacements or len(replacements) > 2:
        raise LibraryStoreError("Unexpected enum repair size")
    for replacement in replacements:
        if not isinstance(replacement, list) or len(replacement) != 3 or tuple(replacement) not in allowed:
            raise LibraryStoreError("Unapproved enum spelling mapping")
        field, before, after = replacement
        target = expected["visual"] if field == "medium" else expected["visual"]["background"]
        if target[field] != before:
            raise LibraryStoreError("Enum repair source value differs")
        target[field] = after
    if expected != effective:
        raise LibraryStoreError("Enum repair changed observation or other fields")


def text(value) -> str:
    return encode(value).decode("utf-8")


def _read(path: Path) -> tuple[dict, bytes]:
    # The all-library manifest is larger than the individual-result 2 MiB cap.
    if not path.is_file() or path.is_symlink() or not 0 < path.stat().st_size <= 25 * 1024 * 1024:
        raise LibraryStoreError("Missing/oversized/symlink source: " + path.name)
    raw = path.read_bytes()
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise LibraryStoreError("Duplicate JSON property")
            result[key] = value
        return result
    try:
        result = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(LibraryStoreError("Nonfinite JSON")))
    except (ValueError, UnicodeError) as exc:
        raise LibraryStoreError("Invalid JSON: " + path.name) from exc
    if not isinstance(result, dict):
        raise LibraryStoreError("Object source required")
    return result, raw


def load_library(root: Path) -> dict:
    from prepare_luna_full_library import BASE, RUN, SOURCE_RUN, prepare, read_manifest
    root = root.resolve()
    approval_db = root / "data/private-research/image-rag-admin/state.sqlite3"
    manifest, manifest_raw = read_manifest(root)
    expected = prepare(root, apply=False)
    if digest(manifest_raw) != expected["tasks_sha256"]:
        raise LibraryStoreError("Full-library manifest changed or approval is stale")
    pinned = contract(root)
    if (manifest["prefix_sha256"] != pinned["prefix_sha256"] or manifest["schema_sha256"] != pinned["schema_sha256"]
            or manifest["taxonomy_sha256"] != pinned["taxonomy_sha256"]):
        raise LibraryStoreError("Compact contract changed")
    committed = _committed(approval_db, SOURCE_RUN)
    spec = load_frozen_workflow(root, SOURCE_RUN)
    normalized = _validate_commit(spec, committed)
    handoff_base = f"data/private-research/image-rag-admin/handoffs/{_key(SOURCE_RUN, committed['commit']['id'])}"
    handoff = _verify_existing(root, handoff_base, committed, spec)
    handoff_items = {row["id"]: row for row in handoff["items"]}
    legacy = load_validated_sources(root, approval_db)
    evidence = {str(root / BASE / "tasks.json"): digest(manifest_raw)}
    for row in legacy["evidence"]:
        evidence[str((root.parent if row["scope"] == "workspace" else root) / row["path"])] = row["sha256"]
    handoff_receipt, handoff_raw = _read(root / handoff_base / "receipt.json")
    evidence[str(root / handoff_base / "receipt.json")] = digest(handoff_raw)
    for name, sha in handoff_receipt["files"].items():
        evidence[str(root / handoff_base / name)] = sha
    for row in handoff_receipt["source_files"]:
        evidence[str(root / row["path"])] = row["sha256"]
    for relative in ("00_CORE/schemas/image_luna_compact_result.schema.json", "00_CORE/templates/image_luna_compact.instructions.md"):
        path = root.parent / relative
        evidence[str(path)] = digest(path.read_bytes())
    projection_code = root / "src/image_rag_eval/compact_projection.py"
    evidence[str(projection_code)] = digest(projection_code.read_bytes())
    retained = set(normalized["stage2_overlay"]["active_ids"])
    items = []
    for row in manifest["source_records"]:
        source = handoff_items[row["id"]]
        prompt = row["original_prompt"]
        items.append({"item_id": row["id"], "style_id": row["style_id"], "source_run_id": SOURCE_RUN,
                      "state": "image_approved" if row["image_approved"] else "retained_unchecked" if row["id"] in retained else "archived_alias",
                      "original_sha256": row["source_sha256"], "prepared_sha256": row["prepared_sha256"],
                      "original_path": None, "prepared_path": row["prepared_image_path"],
                      "prompt_sha256": prompt["prompt_sha256"], "prompt_text": prompt["full_prompt"],
                      "rights": source["rights_display"], "memo": source["human_memo"], "raw": {"frozen_record": row, "handoff_record": source}})
    remaining_dir = root / REMAINING
    remaining_receipt, remaining_receipt_raw = _read(remaining_dir / "receipt.json")
    remaining, _ = _read(remaining_dir / "manifest.json")
    remaining_base, baseline_raw = _read(remaining_dir / "baseline.json")
    remaining_bindings, _ = _read(remaining_dir / "source-bindings.json")
    if (remaining_receipt.get("complete") is not True or remaining["source_commit"] != committed["commit"]
            or remaining_base["source_commit"] != committed["commit"]
            or remaining["baseline_sha256"] != digest(baseline_raw)):
        raise LibraryStoreError("Remaining-source approval baseline is stale")
    originals = {row["id"]: row for row in remaining_base["items"]}
    for row in items:
        original = originals.get(row["item_id"])
        if not original or original["sha256"] != row["original_sha256"] or original["prepared_sha256"] != row["prepared_sha256"]:
            raise LibraryStoreError("Original asset reference changed")
        row["original_path"] = original["path"]
        row["raw"]["baseline_source_record"] = original
    evidence[str(remaining_dir / "receipt.json")] = digest(remaining_receipt_raw)
    for name, sha in remaining_receipt["files"].items():
        path = _path(remaining_dir, name)
        if digest(path.read_bytes()) != sha:
            raise LibraryStoreError("Remaining-source package changed")
        evidence[str(path)] = sha
    _verify_sources(root, remaining_bindings)
    for row in remaining_bindings["files"]:
        evidence[str(root / row["path"])] = row["sha256"]
    canonical_path = root / "data/canonical/archive_records.jsonl"
    canonical_raw = canonical_path.read_bytes()
    if evidence.get(str(canonical_path)) != digest(canonical_raw):
        raise LibraryStoreError("Canonical source not pinned")
    wanted = {row["catalog_key"] for row in remaining["items"]}
    canonical = {}
    for line in canonical_raw.splitlines():
        record = json.loads(line)
        if record.get("catalog_key") in wanted:
            if record["catalog_key"] in canonical:
                raise LibraryStoreError("Ambiguous canonical source")
            canonical[record["catalog_key"]] = record
    for row in remaining["items"]:
        record = canonical.get(row["catalog_key"])
        if not record or record.get("prompt", {}).get("text") != row["prompt"] or row["human_review_status"] != "unreviewed":
            raise LibraryStoreError("Unreviewed original prompt binding mismatch")
        items.append({"item_id": row["id"], "style_id": row["style_id"], "source_run_id": remaining["run_id"], "state": "unreviewed",
                      "original_sha256": row["sha256"], "prepared_sha256": row["prepared_sha256"],
                      "original_path": row["path"], "prepared_path": f"{REMAINING}/{row['prepared_path']}",
                      "prompt_sha256": digest(row["prompt"].encode("utf-8")), "prompt_text": row["prompt"],
                      "rights": row["rights_display"], "memo": "", "raw": {"remaining_record": row, "canonical_record": record}})
    if len(items) != 655 or len({row["item_id"] for row in items}) != 655 or len({row["style_id"] for row in items}) != 655:
        raise LibraryStoreError("All-source identities overlap or expected scope changed")
    legacy_cards = {card["task"]["item_id"]: (run, card) for run in legacy["runs"] for card in run["cards"]}
    results, tasks = [], []
    batch_by_style = {style: batch["batch_id"] for batch in manifest["batches"] for style in batch["style_ids"]}
    for batch in manifest["batches"]:
        path = _path(root, batch["message_path"])
        if digest(path.read_bytes()) != batch["message_sha256"]:
            raise LibraryStoreError("Assigned batch message changed")
        evidence[str(path)] = batch["message_sha256"]
    watched = {}
    history = []
    draft_backups = []
    history_hashes_by_style = {}
    task_by_style = {task["style_id"]: task for task in manifest["tasks"]}
    for path in sorted((root / BASE / "draft-format-backups").glob("*.original.json")):
        draft, raw_draft = _read(path)
        task = task_by_style.get(draft.get("style_id"))
        if (task is None or task["analysis_mode"] != "new_compact" or path.name != task["style_id"] + ".original.json"
                or set(draft) != {"style_id", "visual"} or draft["visual"]["ocr"]["excerpt"] is not None):
            raise LibraryStoreError("Unexpected missing OCR draft backup")
        expected = copy.deepcopy(draft)
        expected["visual"]["ocr"]["excerpt"] = ""
        current, _ = _read(_path(root, task["visual_draft_path"]))
        if current != expected:
            raise LibraryStoreError("Missing OCR repair changed other observations")
        sha = digest(raw_draft)
        draft_backups.append({"sha256": sha, "item_id": task["item_id"], "path": path.relative_to(root).as_posix(),
                              "raw_json": raw_draft.decode(), "repair_kind": "missing_ocr_null_to_empty_string"})
        evidence[str(path)] = sha
    repair_artifacts = []
    task_by_output = {task[field]: task for task in manifest["tasks"] if task["analysis_mode"] == "new_compact"
                      for field in ("raw_result_path", "visual_draft_path")}
    for path in sorted((root / BASE / "literal-format-repairs").glob("*.json")):
        receipt, raw_receipt = _read(path)
        sha = digest(raw_receipt)
        if (path.stem != sha or receipt.get("schema_version") != "luna-literal-enum-repair-1"
                or receipt.get("analysis_run_id") != RUN or receipt.get("metadata_human_approved") is not False
                or receipt.get("release_eligible") is not False or receipt.get("observation_sentences_changed") is not False
                or type(receipt.get("model_calls")) is not int or receipt["model_calls"] != 0):
            raise LibraryStoreError("Invalid literal format repair evidence")
        paths, styles = set(), set()
        for row in receipt["records"]:
            task = task_by_output.get(row["path"])
            if task is None or row["path"] in paths or digest(row["original_raw_json"].encode()) != row["original_sha256"]:
                raise LibraryStoreError("Literal repair source path or bytes invalid")
            paths.add(row["path"]); styles.add(task["style_id"])
            effective, current_raw = _read(_path(root, row["path"]))
            if digest(current_raw) != row["effective_sha256"]:
                raise LibraryStoreError("Literal repair effective bytes changed")
            validate_enum_repair(json.loads(row["original_raw_json"]), effective, row["enum_replacements"])
        if sorted(styles) != sorted(receipt["style_ids"]) or len(styles) != len(receipt["style_ids"]):
            raise LibraryStoreError("Literal repair style binding differs")
        repair_artifacts.append({"sha256": sha, "raw_json": raw_receipt.decode(),
                                 "item_ids": [task_by_style[style]["item_id"] for style in sorted(styles)]})
        evidence[str(path)] = sha
    for path in sorted((root / BASE / "result-history").glob("*.json")):
        archive, archive_raw = _read(path)
        archive_sha = digest(archive_raw)
        if (path.stem != archive_sha or archive.get("schema_version") != "luna-result-history-1"
                or archive.get("analysis_run_id") != RUN or archive.get("task_manifest_sha256") != digest(manifest_raw)
                or archive.get("metadata_human_approved") is not False or archive.get("release_eligible") is not False
                or archive.get("reason") != "quality_flagged_luna_reanalysis"):
            raise LibraryStoreError("Invalid immutable result history")
        batch = next((row for row in manifest["batches"] if row["batch_id"] == archive.get("batch_id")), None)
        if batch is None or [row["style_id"] for row in archive["records"]] != batch["style_ids"]:
            raise LibraryStoreError("History must preserve the exact assigned batch")
        for row in archive["records"]:
            task = task_by_style[row["style_id"]]
            if (row["input_fingerprint"] != task["input_fingerprint"]
                    or digest(row["raw_json"].encode()) != row["raw_sha256"]
                    or digest(row["draft_json"].encode()) != row["draft_sha256"]):
                raise LibraryStoreError("History input or original bytes changed")
            context, _ = _read(_path(root, task["prompt_context_path"]))
            project_compact(json.loads(row["raw_json"]), json.loads(row["draft_json"]), pinned,
                            expected_style_id=row["style_id"], original_prompt=context["full_prompt"])
            history.append({**row, "item_id": task["item_id"], "history_sha256": archive_sha, "reason": archive["reason"]})
            history_hashes_by_style.setdefault(row["style_id"], set()).add(row["raw_sha256"])
        evidence[str(path)] = archive_sha
    quality_path = root / BASE / "quality-review.json"
    watched[str(quality_path)] = digest(quality_path.read_bytes()) if quality_path.is_file() else None
    quality = None
    quality_raw = None
    quality_by_style = {}
    if quality_path.exists():
        quality, quality_raw = _read(quality_path)
        if (quality.get("schema_version") != "luna-full-library-quality-review-1" or quality.get("analysis_run_id") != RUN
                or quality.get("metadata_human_approved") is not False or quality.get("release_eligible") is not False):
            raise LibraryStoreError("Invalid full-library quality review")
        evidence[str(quality_path)] = digest(quality_raw)
        for finding in quality["findings"]:
            if finding.get("style_id") not in {task["style_id"] for task in manifest["tasks"]}:
                raise LibraryStoreError("QA references an unknown task")
            for pointer in finding["affected_json_pointers"]:
                if not isinstance(pointer, str) or not pointer.startswith(("/visual", "/prompt", "/uses", "/extras_json")):
                    raise LibraryStoreError("QA must reference analysis fields")
                quality_by_style.setdefault(finding["style_id"], []).append({"field": pointer, "finding": finding})
    for task in manifest["tasks"]:
        state, error = "pending", None
        if task["analysis_mode"] == "legacy_reuse":
            run, card = legacy_cards[task["item_id"]]
            results.append({"item_id": task["item_id"], "source_run_id": run["manifest"]["analysis_run_id"],
                            "result": card["result"], "raw_json": card["raw_result_json"], "qa": card["qa_findings"], "mode": "legacy"})
            state = "legacy_reused"
        else:
            context, raw_context = _read(_path(root, task["prompt_context_path"]))
            if (context.get("style_id") != task["style_id"] or context.get("prompt_sha256") != task["prompt_sha256"]
                    or digest(context.get("full_prompt", "").encode("utf-8")) != task["prompt_sha256"]):
                raise LibraryStoreError("Assigned prompt context changed")
            evidence[str(root / task["prompt_context_path"])] = digest(raw_context)
            for field in ("raw_result_path", "visual_draft_path"):
                path = _path(root, task[field])
                watched[str(path)] = digest(path.read_bytes()) if path.is_file() else None
            result_path, draft_path = _path(root, task["raw_result_path"]), _path(root, task["visual_draft_path"])
            if result_path.exists():
                try:
                    result, raw_result = _read(result_path)
                    draft, raw_draft = _read(draft_path)
                    projection = project_compact(result, draft, pinned, expected_style_id=task["style_id"], original_prompt=context["full_prompt"])
                    qa = []
                    for finding in quality_by_style.get(task["style_id"], []):
                        applies = finding["finding"].get("applies_to_result_sha256")
                        if applies is not None and applies != digest(raw_result):
                            if applies not in history_hashes_by_style.get(task["style_id"], set()):
                                raise LibraryStoreError("QA references an unknown result revision")
                            continue
                        qa.append(finding)
                    results.append({"item_id": task["item_id"], "source_run_id": RUN, "result": result,
                                    "raw_json": raw_result.decode("utf-8"), "raw_draft_json": raw_draft.decode("utf-8"),
                                    "effective_result": projection["result"], "effective_draft": projection["draft"],
                                    "normalization": projection["normalization"],
                                    "qa": qa, "mode": "compact"})
                    state = "validated_candidate"
                except (ValueError, KeyError, OSError) as exc:
                    state, error = "invalid_result", {"error": str(exc)[:500]}
            elif draft_path.exists():
                state = "visual_draft_ready"
        tasks.append({**task, "state": state, "error": error, "batch_id": batch_by_style.get(task["style_id"])})
    # Observe only finalized schema-bearing receipt documents, not worker notes.
    tokens = []
    execution_dir = root / BASE / "execution"
    receipt_paths = sorted(execution_dir.glob("*.tokens.json")) if execution_dir.exists() else []
    for path in receipt_paths:
        receipt, raw = _read(path)
        if receipt.get("schema_version") not in {"image-luna-batch-token-usage-receipt-1", "image-luna-rollout-turn-usage-receipt-1"}:
            raise LibraryStoreError("Unknown token receipt schema")
        if ((receipt.get("analysis_run_id") is not None and receipt["analysis_run_id"] != RUN)
                or receipt.get("actual_billed_tokens") is not None or receipt.get("actual_billed_cost") is not None):
            raise LibraryStoreError("Invalid compact token receipt identity")
        tokens.append({"path": path.relative_to(root).as_posix(), "raw_json": raw.decode("utf-8"), "receipt": receipt})
        evidence[str(path)] = digest(raw)
    old_db = root / OLD_DATABASE
    if not old_db.is_file():
        raise LibraryStoreError("Preserved legacy candidate DB missing")
    evidence[str(old_db)] = digest(old_db.read_bytes())
    bundle = {"schema_version": SCHEMA, "analysis_run_id": RUN, "source_commit": committed["commit"],
              "manifest": manifest, "manifest_sha256": digest(manifest_raw), "items": items,
              "groups": normalized["approved_similarity_groups"], "aliases": _archive(normalized, spec),
              "tasks": tasks, "results": results, "tokens": tokens, "result_history": history,
              "literal_format_repairs": repair_artifacts, "draft_format_backups": draft_backups, "quality_review": quality,
              "quality_review_raw_json": quality_raw.decode("utf-8") if quality_raw is not None else None,
              "legacy_runs": [{key: run[key] for key in ("manifest", "task_manifest_sha256", "token", "token_raw_json")} for run in legacy["runs"]],
              "taxonomy": legacy["taxonomy"], "taxonomy_raw_json": legacy["taxonomy_raw_json"], "taxonomy_sha256": legacy["taxonomy_sha256"],
              "evidence": {Path(path).relative_to(root.parent).as_posix(): sha for path, sha in sorted(evidence.items())},
              "watched_outputs": {Path(path).relative_to(root).as_posix(): sha for path, sha in sorted(watched.items())}}
    _require_latest(approval_db, SOURCE_RUN, committed["commit"]["id"])
    return bundle


def _put(db, table, row):
    cols = tuple(row)
    existing = db.execute(f'SELECT 1 FROM "{table}" WHERE ' + " AND ".join(f'"{key}" IS ?' for key in cols), tuple(row.values())).fetchone()
    if not existing:
        db.execute(f'INSERT INTO "{table}" ({",".join(cols)}) VALUES ({",".join("?" for _ in cols)})', tuple(row.values()))


def _uses(result):
    if "uses" in result:
        return sorted(result["uses"], key=lambda row: row["priority"] != "primary")
    if "usage_selection" in result:
        selected = result["usage_selection"]
        return [selected["primary"], *selected["secondary"]] if selected["primary"] else []
    return []


def _check_tokens(usage):
    keys = ("input_tokens_including_cached", "cached_input_tokens", "output_tokens_including_reasoning", "reasoning_output_tokens", "total_tokens")
    if (any(type(usage.get(key)) is not int or usage[key] < 0 for key in keys)
            or usage[keys[1]] > usage[keys[0]] or usage[keys[3]] > usage[keys[2]]
            or usage[keys[4]] != usage[keys[0]] + usage[keys[2]]):
        raise LibraryStoreError("Token receipt has invalid subsets or totals")


def _turn_receipt(receipt: dict, style_items: dict) -> None:
    fields = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens")
    if (receipt.get("schema_version") != "image-luna-rollout-turn-usage-receipt-1"
            or receipt.get("scope") != "explicit_completed_turn_ids_only"
            or not isinstance(receipt.get("session_id_reported"), str)
            or not isinstance(receipt.get("agent_path_reported"), str)
            or not receipt["agent_path_reported"].startswith("/root/luna_")):
        raise LibraryStoreError("Invalid turn-scoped token identity")
    turns = receipt.get("turns", [])
    if (not turns or [turn.get("turn_id") for turn in turns] != receipt.get("turn_ids")
            or len({turn["turn_id"] for turn in turns}) != len(turns)
            or set(receipt["turn_ids"]) & set(receipt.get("excluded_historical_turn_ids", []))):
        raise LibraryStoreError("Turn receipt repeated or historically excluded a selected turn")
    for turn in turns:
        styles = turn.get("style_ids", [])
        if (not styles or len(styles) != len(set(styles)) or not set(styles) <= set(style_items)
                or turn.get("model_reported") != "gpt-5.6-luna" or turn.get("completion_event_reported") != "task_complete"
                or turn.get("work_success_inferred") is not False):
            raise LibraryStoreError("Turn binding does not match eligible Luna tasks")
        usage = turn["attributable_usage"]
        if set(usage) != set(fields) or any(value is not None and (type(value) is not int or value < 0) for value in usage.values()):
            raise LibraryStoreError("Turn usage must preserve nonnegative counters or null")
        input_count, output_count = usage["input_tokens"], usage["output_tokens"]
        expected = input_count + output_count if input_count is not None and output_count is not None else None
        if turn.get("total_tokens_calculated") != expected:
            raise LibraryStoreError("Turn total is not input plus output")
        if input_count is not None:
            for key in ("cached_input_tokens", "cache_write_input_tokens"):
                if usage[key] is not None and usage[key] > input_count:
                    raise LibraryStoreError("Cache counter exceeds input")
            if (usage["cached_input_tokens"] is not None and usage["cache_write_input_tokens"] is not None
                    and usage["cached_input_tokens"] + usage["cache_write_input_tokens"] > input_count):
                raise LibraryStoreError("Cache reads plus writes exceed input")
        if output_count is not None and usage["reasoning_output_tokens"] is not None and usage["reasoning_output_tokens"] > output_count:
            raise LibraryStoreError("Reasoning counter exceeds output")
    for key in (*fields, "total_tokens_calculated"):
        values = [turn["total_tokens_calculated"] if key == "total_tokens_calculated" else turn["attributable_usage"][key] for turn in turns]
        known, unknown = sum(value for value in values if value is not None), sum(value is None for value in values)
        if (receipt["usage"].get(key) != (known if not unknown else None)
                or receipt.get("known_usage_subtotals", {}).get(key) != known
                or receipt.get("unknown_turn_counts", {}).get(key) != unknown):
            raise LibraryStoreError("Aggregate turn telemetry masks missing counters")
    for remainder, parts in (("uncached_input_tokens_calculated", ("input_tokens", "cached_input_tokens")),
                             ("ordinary_input_tokens_calculated", ("input_tokens", "cached_input_tokens", "cache_write_input_tokens"))):
        values = [receipt["usage"][part] for part in parts]
        expected = values[0] - sum(values[1:]) if all(value is not None for value in values) else None
        if receipt["usage"].get(remainder) != expected:
            raise LibraryStoreError("Calculated token remainder changed")


def populate(db: sqlite3.Connection, bundle: dict, migration_sha: str) -> None:
    db.execute("BEGIN IMMEDIATE")
    try:
        commit = bundle["source_commit"]["id"]
        _put(db, "snapshot", {"id": 1, "source_sha256": digest(encode(bundle)), "migration_sha256": migration_sha,
                              "schema_version": SCHEMA, "source_commit_id": commit, "public_eligible": 0})
        for path, sha in bundle["evidence"].items():
            _put(db, "evidence", {"path": path, "sha256": sha})
        for row in bundle["items"]:
            if (row["rights"].get("release_eligible") is not False or
                    row["prompt_sha256"] != digest(row["prompt_text"].encode("utf-8"))):
                raise LibraryStoreError("Unsafe rights or prompt binding")
            for field in ("original_sha256", "prepared_sha256"):
                _put(db, "assets", {"sha256": row[field]})
            for role in ("original", "prepared"):
                if row[role + "_path"]:
                    _put(db, "asset_locations", {"sha256": row[role + "_sha256"], "path": row[role + "_path"], "role": role})
            _put(db, "prompts", {"sha256": row["prompt_sha256"], "original_text": row["prompt_text"]})
            parsed = extract_arguments(row["prompt_text"])
            _put(db, "source_prompt_argument_parses", {"prompt_sha256": row["prompt_sha256"],
                "parser_version": parsed["parser_version"], "argument_count": parsed["argument_count"],
                "unparsed_marker_offsets_json": text(parsed["unparsed_marker_offsets"])})
            for argument in parsed["arguments"]:
                _put(db, "source_prompt_arguments", {"prompt_sha256": row["prompt_sha256"], **argument,
                    "provenance": "literal_source_not_llm_or_human_approval"})
            _put(db, "source_items", {"item_id": row["item_id"], "style_id": row["style_id"],
                    "original_sha256": row["original_sha256"], "prepared_sha256": row["prepared_sha256"],
                    "prompt_sha256": row["prompt_sha256"], "approval_state": row["state"], "source_run_id": row["source_run_id"],
                    "raw_json": text(row["raw"]), "rights_json": text(row["rights"]), "public_eligible": 0})
            if row["memo"]:
                _put(db, "human_notes", {"item_id": row["item_id"], "memo": row["memo"],
                                        "provenance": "committed_human_memo_not_llm_tag", "source_commit_id": commit})
        for group in bundle["groups"]:
            _put(db, "approval_groups", {"group_id": group["candidate_id"], "representative_item_id": group["suggested_representative_id"],
                                         "source_commit_id": commit, "raw_json": text(group)})
            for ident in group["member_ids"]:
                _put(db, "group_memberships", {"group_id": group["candidate_id"], "item_id": ident,
                        "is_representative": int(ident == group["suggested_representative_id"])})
        for alias in bundle["aliases"]:
            _put(db, "archived_aliases", {"item_id": alias["id"], "representative_item_id": alias["final_representative_id"], "raw_json": text(alias)})
        taxonomy_sha = bundle["taxonomy_sha256"]
        if digest(bundle["taxonomy_raw_json"].encode("utf-8")) != taxonomy_sha:
            raise LibraryStoreError("Taxonomy SHA mismatch")
        _put(db, "taxonomy_versions", {"sha256": taxonomy_sha, "raw_json": bundle["taxonomy_raw_json"], "status": "proposal_needs_review"})
        terms = [("use_case", row) for family in bundle["taxonomy"]["families"] for row in family["use_cases"]]
        terms += [("asset_format", row) for row in bundle["taxonomy"].get("asset_formats", [])]
        aliases = []
        for facet, row in terms:
            _put(db, "taxonomy_terms", {"taxonomy_sha256": taxonomy_sha, "facet": facet, "term_id": row["id"], "label_ko": row["label_ko"], "raw_json": text(row)})
            aliases.extend((facet, row["id"], value) for value in (row["id"], row["label_ko"]))
        aliases.extend((row["facet"], row["target_id"], row["term"]) for row in bundle["taxonomy"].get("aliases", []))
        for facet, ident, term in aliases:
            _put(db, "taxonomy_aliases", {"taxonomy_sha256": taxonomy_sha, "facet": facet, "term_id": ident,
                                         "term": term, "normalized_term": normalize_term(term)})
        _put(db, "analysis_runs", {"run_id": bundle["analysis_run_id"], "mode": "compact_with_explicit_legacy_reuse",
                "manifest_sha256": bundle["manifest_sha256"], "manifest_json": text(bundle["manifest"])})
        for row in bundle.get("result_history", []):
            if digest(row["raw_json"].encode()) != row["raw_sha256"] or digest(row["draft_json"].encode()) != row["draft_sha256"]:
                raise LibraryStoreError("Result history bytes changed before insertion")
            _put(db, "analysis_result_history", {key: row[key] for key in
                ("item_id", "history_sha256", "input_fingerprint", "raw_sha256", "draft_sha256", "raw_json", "draft_json", "reason")} |
                {"source_run_id": bundle["analysis_run_id"], "metadata_human_approved": 0})
        for row in bundle.get("literal_format_repairs", []):
            if digest(row["raw_json"].encode()) != row["sha256"]:
                raise LibraryStoreError("Literal repair receipt changed")
            _put(db, "literal_format_repairs", {"sha256": row["sha256"], "source_run_id": bundle["analysis_run_id"],
                                               "raw_json": row["raw_json"], "model_calls": 0})
            for item_id in row["item_ids"]:
                _put(db, "literal_format_repair_items", {"repair_sha256": row["sha256"], "item_id": item_id})
        for row in bundle.get("draft_format_backups", []):
            if digest(row["raw_json"].encode()) != row["sha256"]:
                raise LibraryStoreError("Draft format backup bytes changed")
            _put(db, "draft_format_backups", {**row, "source_run_id": bundle["analysis_run_id"]})
        if bundle.get("quality_review") is not None:
            quality = bundle["quality_review"]
            raw_quality = bundle["quality_review_raw_json"]
            if json.loads(raw_quality) != quality or quality.get("metadata_human_approved") is not False or quality.get("release_eligible") is not False:
                raise LibraryStoreError("Quality sidecar changed or attempted approval")
            _put(db, "quality_reviews", {"sha256": digest(raw_quality.encode("utf-8")), "source_run_id": bundle["analysis_run_id"],
                    "finding_count": len(quality["findings"]), "raw_json": raw_quality})
        for run in bundle["legacy_runs"]:
            run_id = run["manifest"]["analysis_run_id"]
            _put(db, "analysis_runs", {"run_id": run_id, "mode": "legacy", "manifest_sha256": run["task_manifest_sha256"], "manifest_json": text(run["manifest"])})
            receipt = run["token"]
            _check_tokens(receipt["usage"])
            _put(db, "token_receipts", {"sha256": digest(run["token_raw_json"].encode("utf-8")), "source_run_id": run_id,
                    "path": f"data/private-research/image-rag-admin/luna-analysis/{run_id}/token-usage-receipt.json", "kind": "legacy",
                    "total_tokens": receipt["usage"]["total_tokens"], "scope": receipt.get("scope", "isolated_local_codex_sessions"),
                    "actual_billed_tokens": None, "actual_billed_cost": None, "raw_json": run["token_raw_json"]})
        for task in bundle["tasks"]:
            if db.execute("SELECT approval_state FROM source_items WHERE item_id=?", (task["item_id"],)).fetchone() != ("image_approved",):
                raise LibraryStoreError("Unapproved source cannot receive an analysis task")
            _put(db, "analysis_tasks", {"item_id": task["item_id"], "run_id": bundle["analysis_run_id"], "style_id": task["style_id"],
                    "mode": task["analysis_mode"], "input_fingerprint": task["input_fingerprint"], "batch_id": task["batch_id"], "state": task["state"],
                    "usage_state": "observed_legacy_scope" if task["analysis_mode"] == "legacy_reuse" else "usage_pending",
                    "error_json": text(task["error"]) if task["error"] else None, "raw_json": text(task)})
        lexical = {row["item_id"]: [row["style_id"], row["prompt_text"], row["memo"]] for row in bundle["items"]}
        for candidate in bundle["results"]:
            original, raw = candidate["result"], candidate["raw_json"]
            result = candidate.get("effective_result", original)
            normalization = candidate.get("normalization")
            if json.loads(raw) != original or result.get("release_eligible", False) is not False or result.get("metadata_human_approved", False) is not False:
                raise LibraryStoreError("Result raw JSON mismatch or attempted approval")
            if normalization is None and original != result:
                raise LibraryStoreError("Changed effective result requires explicit normalization evidence")
            if normalization is not None:
                raw_draft = candidate["raw_draft_json"]
                draft = json.loads(raw_draft)
                layout_adapter = (normalization.get("adapter_version") == "compact-layout-literal-join-1"
                                  and normalization.get("lossless_literal_join") is True
                                  and normalization.get("raw_status") == "nonconformant_layout_count_only")
                envelope_adapter = (normalization.get("adapter_version") == "compact-draft-envelope-1"
                                    and normalization.get("lossless_envelope_normalization") is True
                                    and normalization.get("raw_status") == "conformant_result_nonconformant_draft_envelope_only"
                                    and normalization.get("removed_envelope_fields") == {"schema_version": "luna-compact-3"}
                                    and normalization.get("visual_fields_unchanged") is True
                                    and original == result
                                    and draft == {"schema_version": "luna-compact-3", **candidate["effective_draft"]})
                if (not (layout_adapter or envelope_adapter)
                        or normalization.get("model_calls") != 0
                        or normalization.get("derived_status") != "validated_after_literal_normalization"
                        or normalization.get("raw_value_sha256") != digest(encode(original))
                        or normalization.get("draft_value_sha256") != digest(encode(draft))
                        or normalization.get("derived_value_sha256") != digest(encode(result))
                        or normalization.get("derived_draft_value_sha256") != digest(encode(candidate["effective_draft"]))
                        or normalization.get("metadata_human_approved") is not False or normalization.get("release_eligible") is not False):
                    raise LibraryStoreError("Normalization evidence does not bind original/draft/effective values")
            candidate_id = digest(encode([candidate["item_id"], candidate["source_run_id"], digest(raw.encode("utf-8"))]))
            prompt = result.get("prompt", result.get("prompt_analysis", result.get("prompt_intent")))
            freeform = {key: result[key] for key in ("reuse_ideas", "search_hints", "extras_json", "limitations") if key in result}
            _put(db, "analysis_results", {"candidate_id": candidate_id, "item_id": candidate["item_id"], "source_run_id": candidate["source_run_id"],
                    "result_schema": result["schema_version"], "raw_sha256": digest(raw.encode("utf-8")), "raw_json": raw,
                    "effective_json": text(result), "effective_sha256": digest(encode(result)),
                    "visual_json": text(result["visual"]), "prompt_json": text(prompt), "freeform_json": text(freeform),
                    "review_status": "needs_review", "metadata_human_approved": 0, "public_eligible": 0})
            if normalization is not None:
                _put(db, "candidate_normalizations", {"candidate_id": candidate_id, "adapter_version": normalization["adapter_version"],
                        "raw_draft_sha256": digest(candidate["raw_draft_json"].encode("utf-8")), "raw_draft_json": candidate["raw_draft_json"],
                        "effective_draft_json": text(candidate["effective_draft"]), "normalization_json": text(normalization)})
            for ordinal, use in enumerate(_uses(result)):
                _put(db, "usage_assignments", {"candidate_id": candidate_id, "ordinal": ordinal, "taxonomy_sha256": taxonomy_sha,
                        "facet": "use_case", "use_case_id": use["use_case_id"], "fit": use["fit"], "raw_json": text(use)})
            qa_roots = {qa["field"].split("/")[1] if qa["field"].startswith("/") else qa["field"].split(".")[0].split("[")[0] for qa in candidate["qa"]}
            for ordinal, qa in enumerate(candidate["qa"]):
                _put(db, "candidate_qa", {"candidate_id": candidate_id, "ordinal": ordinal, "field_path": qa["field"], "raw_json": text(qa)})
            lexical[candidate["item_id"]].extend(_strings({key: value for key, value in result.items() if key not in qa_roots}))
        style_items = {task["style_id"]: task["item_id"] for task in bundle["tasks"] if task["analysis_mode"] == "new_compact"}
        for entry in bundle["tokens"]:
            receipt, raw = entry["receipt"], entry["raw_json"]
            if (json.loads(raw) != receipt or (receipt.get("analysis_run_id") is not None and receipt["analysis_run_id"] != bundle["analysis_run_id"])
                    or receipt.get("actual_billed_tokens") is not None or receipt.get("actual_billed_cost") is not None):
                raise LibraryStoreError("Unbound token receipt")
            if receipt.get("schema_version") == "image-luna-rollout-turn-usage-receipt-1":
                _turn_receipt(receipt, style_items)
                sha = digest(raw.encode("utf-8"))
                _put(db, "token_receipts", {"sha256": sha, "source_run_id": bundle["analysis_run_id"], "path": entry["path"], "kind": "compact",
                        "total_tokens": receipt["usage"]["total_tokens_calculated"], "scope": receipt["scope"], "actual_billed_tokens": None,
                        "actual_billed_cost": None, "raw_json": raw})
                sid = receipt["session_id_reported"]
                for turn in receipt["turns"]:
                    usage = turn["attributable_usage"]
                    _put(db, "token_turns", {"session_id": sid, "turn_id": turn["turn_id"], "raw_sha256": digest(encode(turn)),
                            "total_tokens": turn["total_tokens_calculated"], "input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"],
                            "cached_input_tokens": usage["cached_input_tokens"], "reasoning_output_tokens": usage["reasoning_output_tokens"], "raw_json": text(turn)})
                    _put(db, "receipt_turns", {"receipt_sha256": sha, "session_id": sid, "turn_id": turn["turn_id"]})
                    assigned_batches = {task["batch_id"] for task in bundle["tasks"] if task["style_id"] in turn["style_ids"] and task["batch_id"]}
                    if turn.get("batch_ids") and set(turn["batch_ids"]) != assigned_batches:
                        raise LibraryStoreError("Turn telemetry batch IDs do not match assigned images")
                    for style in turn["style_ids"]:
                        _put(db, "turn_items", {"session_id": sid, "turn_id": turn["turn_id"], "item_id": style_items[style]})
                continue
            _check_tokens(receipt["usage"])
            expected = receipt.get("expected_style_ids", [])
            sessions = receipt.get("sessions", [])
            if (not expected or len(expected) != len(set(expected)) or not set(expected) <= set(style_items)
                    or sorted(style for session in sessions for style in session["style_ids"]) != sorted(expected)
                    or sum(session["usage"]["total_tokens"] for session in sessions) != receipt["usage"]["total_tokens"]):
                raise LibraryStoreError("Token session coverage or total mismatch")
            sha = digest(raw.encode("utf-8"))
            _put(db, "token_receipts", {"sha256": sha, "source_run_id": bundle["analysis_run_id"], "path": entry["path"], "kind": "compact",
                    "total_tokens": receipt["usage"]["total_tokens"], "scope": receipt["scope"], "actual_billed_tokens": None,
                    "actual_billed_cost": None, "raw_json": raw})
            for session in sessions:
                sid = session["session_id"]
                _put(db, "token_sessions", {"session_id": sid, "raw_sha256": digest(encode(session)),
                        "total_tokens": session["usage"]["total_tokens"], "raw_json": text(session)})
                _put(db, "receipt_sessions", {"receipt_sha256": sha, "session_id": sid})
                for style in session["style_ids"]:
                    _put(db, "session_items", {"session_id": sid, "item_id": style_items[style]})
                    db.execute("UPDATE analysis_tasks SET usage_state='observed_batch_scope' WHERE item_id=?", (style_items[style],))
        if db.execute("SELECT 1 FROM token_sessions s JOIN token_turns t USING(session_id) LIMIT 1").fetchone():
            raise LibraryStoreError("Cannot double-count session-wide and turn-scoped usage from the same session")
        # A retry turn may overlap styles. Known completed-turn telemetry wins;
        # unknown completed turns still remain in raw receipts and partial totals.
        db.execute("""UPDATE analysis_tasks SET usage_state='usage_unobserved_completed_turn'
            WHERE item_id IN(SELECT item_id FROM turn_items)""")
        db.execute("""UPDATE analysis_tasks SET usage_state='observed_turn_scope' WHERE item_id IN
            (SELECT t.item_id FROM turn_items t JOIN token_turns u USING(session_id,turn_id) WHERE u.total_tokens IS NOT NULL)""")
        for row in bundle["items"]:
            unique = {}
            for value in lexical[row["item_id"]]:
                if value and value.strip():
                    unique.setdefault(normalize_term(value), value.strip())
            content = "\n".join(unique.values())
            _put(db, "diagnostic_documents", {"item_id": row["item_id"], "text": content, "approval_state": row["state"], "public_eligible": 0})
            db.execute("INSERT INTO diagnostic_fts(item_id,text) VALUES (?,?)", (row["item_id"], content))
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise LibraryStoreError("Invalid relational source integrity")
        db.commit()
    except Exception:
        db.rollback()
        raise


def summary(db):
    count = lambda table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    states = dict(db.execute("SELECT approval_state,count(*) FROM source_items GROUP BY approval_state"))
    task_states = dict(db.execute("SELECT state,count(*) FROM analysis_tasks GROUP BY state"))
    usage = dict(db.execute("SELECT usage_state,count(*) FROM analysis_tasks GROUP BY usage_state"))
    known_session = db.execute("SELECT sum(total_tokens) FROM token_sessions").fetchone()[0]
    known_turn = db.execute("SELECT sum(total_tokens) FROM token_turns").fetchone()[0]
    unknown_turns = db.execute("SELECT count(*) FROM token_turns WHERE total_tokens IS NULL").fetchone()[0]
    known_total = sum(value for value in (known_session, known_turn) if value is not None) if known_session is not None or known_turn is not None else None
    return {"source_images": count("source_items"), "approval_states": states, "analysis_states": task_states, "usage_states": usage,
            "groups": count("approval_groups"), "memberships": count("group_memberships"), "memos": count("human_notes"),
            "prompts": count("prompts"), "results": count("analysis_results"), "qa_findings": count("candidate_qa"),
            "literal_source_arguments": count("source_prompt_arguments"),
            "prompts_with_unparsed_argument_markers": db.execute("SELECT count(*) FROM source_prompt_argument_parses WHERE json_array_length(unparsed_marker_offsets_json)>0").fetchone()[0],
            "literal_normalized_candidates": count("candidate_normalizations"),
            "preserved_result_revisions": count("analysis_result_history"),
            "literal_enum_repair_artifacts": count("literal_format_repairs"),
            "literal_enum_repaired_candidates": count("literal_format_repair_items"),
            "missing_ocr_draft_format_backups": count("draft_format_backups"),
            "new_raw_strict_valid_candidates": db.execute("SELECT count(*) FROM analysis_results WHERE source_run_id=(SELECT run_id FROM analysis_runs WHERE mode='compact_with_explicit_legacy_reuse')").fetchone()[0] - count("candidate_normalizations"),
            "quality_review_documents": count("quality_reviews"),
            "compact_quality_findings": db.execute("SELECT sum(finding_count) FROM quality_reviews").fetchone()[0],
            "token_receipts": count("token_receipts"), "compact_unique_sessions": count("token_sessions") + db.execute("SELECT count(DISTINCT session_id) FROM token_turns").fetchone()[0],
            "compact_observed_turns": count("token_turns"), "compact_unobserved_token_turns": unknown_turns,
            "observed_compact_tokens": None if unknown_turns else known_total, "known_compact_token_subtotal": known_total,
            "observed_legacy_scope_tokens": db.execute("SELECT sum(total_tokens) FROM token_receipts WHERE kind='legacy'").fetchone()[0],
            "metadata_human_approved": 0, "public_eligible": 0, "embedding_calls": 0, "provider_calls": 0,
            "database_bytes": len(db.serialize())}


def project_library(bundle: dict, output_base: Path, migration: Path, *, apply=False, recheck=None) -> dict:
    raw_sql = migration.read_bytes()
    projector_sha = digest(encode({name: digest(Path(__file__).with_name(name).read_bytes())
                                   for name in ("luna_library_store.py", "prompt_arguments.py")}))
    key = digest(encode({"bundle_sha256": digest(encode(bundle)), "migration_sha256": digest(raw_sql),
                         "projector_sha256": projector_sha}))
    if output_base.is_symlink() or any(parent.is_symlink() for parent in output_base.parents):
        raise LibraryStoreError("Checkpoint path must not contain symlinks")
    output_base = output_base.resolve()
    directory = output_base / key
    database = directory / "library.sqlite3"
    expected = sqlite3.connect(":memory:")
    try:
        expected.executescript(raw_sql.decode("utf-8"))
        populate(expected, bundle, digest(raw_sql))
        result = {"status": "dry_run", "schema_version": SCHEMA, "snapshot_key": key,
                  "projector_sha256": projector_sha,
                  "database_path": str(database), "source_commit_id": bundle["source_commit"]["id"], **summary(expected)}
        receipt = {key: value for key, value in result.items() if key not in {"status", "database_path"}}
        if directory.exists():
            if set(path.name for path in directory.iterdir()) != {"library.sqlite3", "receipt.json"}:
                raise LibraryStoreError("Existing checkpoint is incomplete")
            db = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)] or _logical_dump(db) != _logical_dump(expected):
                    raise LibraryStoreError("Immutable checkpoint content changed")
            finally:
                db.close()
            stored, _ = _read(directory / "receipt.json")
            if stored != {**receipt, "database_sha256": digest(database.read_bytes())}:
                raise LibraryStoreError("Immutable checkpoint receipt changed")
            if recheck:
                recheck()
            return {**result, "status": "unchanged", "database_sha256": digest(database.read_bytes())}
        if recheck:
            recheck()
        if not apply:
            return result
        output_base.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".library-", dir=output_base))
        try:
            target = sqlite3.connect(temporary / "library.sqlite3")
            try:
                expected.backup(target)
            finally:
                target.close()
            database_sha = digest((temporary / "library.sqlite3").read_bytes())
            with (temporary / "receipt.json").open("xb") as stream:
                stream.write(encode({**receipt, "database_sha256": database_sha}))
                stream.flush()
                os.fsync(stream.fileno())
            if recheck:
                recheck()
            temporary.rename(directory)
        finally:
            if temporary.exists():
                for name in ("library.sqlite3", "receipt.json"):
                    (temporary / name).unlink(missing_ok=True)
                temporary.rmdir()
        return {**result, "status": "prepared", "database_sha256": database_sha}
    finally:
        expected.close()


def diagnostic_search(db: sqlite3.Connection, query: str, limit=5) -> list[dict]:
    """Private group-aware discovery. No new clustering and no public approval."""
    if not isinstance(query, str) or not 0 < len(query.strip()) <= 200 or type(limit) is not int or not 1 <= limit <= 50:
        raise LibraryStoreError("Bounded plain query required")
    term = normalize_term(query)
    quoted = '"' + term.replace('"', '""') + '"'
    like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    hits = db.execute("""SELECT i.item_id,i.style_id,g.group_id,g.representative_item_id FROM source_items i
      JOIN diagnostic_documents d USING(item_id) LEFT JOIN group_memberships m USING(item_id)
      LEFT JOIN approval_groups g USING(group_id)
      WHERE i.approval_state='image_approved' AND (i.item_id IN
        (SELECT item_id FROM diagnostic_fts WHERE diagnostic_fts MATCH ?) OR lower(d.text) LIKE ? ESCAPE '\\'
        OR i.item_id IN(SELECT r.item_id FROM analysis_results r JOIN usage_assignments a USING(candidate_id)
          JOIN taxonomy_aliases t ON a.taxonomy_sha256=t.taxonomy_sha256 AND t.facet='use_case' AND a.use_case_id=t.term_id
          WHERE t.normalized_term=? AND NOT EXISTS(SELECT 1 FROM candidate_qa q WHERE q.candidate_id=r.candidate_id
            AND (q.field_path='/uses' OR q.field_path LIKE '/uses/%' OR q.field_path='usage_selection' OR q.field_path LIKE 'usage_selection.%'))))
      ORDER BY i.style_id""", (quoted, like, term)).fetchall()
    visible = dict(db.execute("SELECT item_id,style_id FROM source_items WHERE approval_state='image_approved'"))
    collapsed = {}
    for ident, style, group, representative in hits:
        key = group or ident
        if key in collapsed:
            collapsed[key]["matched_style_ids"].append(style)
            continue
        if representative not in visible:
            representative = ident
        collapsed[key] = {"item_id": representative if group else ident, "style_id": visible[representative] if group else style,
                          "group_id": group, "matched_style_ids": [style], "public_eligible": False, "purpose": "private_diagnostic_only"}
    return list(collapsed.values())[:limit]


def build_library_store(root: Path, *, apply=False) -> dict:
    root = root.resolve()
    bundle = load_library(root)
    def recheck():
        for path, sha in bundle["evidence"].items():
            if digest(_path(root.parent, path).read_bytes()) != sha:
                raise LibraryStoreError("Source changed while preparing checkpoint")
        for path, sha in bundle["watched_outputs"].items():
            artifact = _path(root, path)
            current = digest(artifact.read_bytes()) if artifact.is_file() else None
            if current != sha:
                raise LibraryStoreError("Luna output changed during checkpoint; retry to capture consistent progress")
        _require_latest(root / "data/private-research/image-rag-admin/state.sqlite3", bundle["manifest"]["source_run_id"], bundle["source_commit"]["id"])
    return project_library(bundle, root / SNAPSHOTS, root / MIGRATION, apply=apply, recheck=recheck)
