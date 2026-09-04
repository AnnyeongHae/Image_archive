"""Frozen human baseline plus a new-image review, without provider calls.

Build before importing the baseline into the old run: the completed embedding
validator deliberately rejects changed live baselines. This review pins the
validated files and the submitted human decision bytes in its own snapshot.
"""
from __future__ import annotations

import copy
import json
import re
from collections import defaultdict
from contextlib import ExitStack, nullcontext
from pathlib import Path
from typing import Any

from .approved_front import render_approved_front
from .experiment import digest, json_bytes, now, read_json, run_lock, run_path, write_json
from .group_workflow import (
    blank_group_workflow_decisions, canonicalize_approved_groups,
    render_group_review, validate_group_workflow_decisions,
)
from .incremental_embedding import SCHEMA as EMBEDDING_SCHEMA, _load, _state
from .prompt_priority import priority_sort_key, rank_prompt
from .similarity import cosine, prompt_signals
from .voyage_provider import VOYAGE_MODEL

POLICY = "default_retained_images_after_review_v1"
SCHEMA = "image-incremental-human-workflow-1"
REVIEW_DIR = "group-workflow-v1"


def _components(edges: list[dict]) -> list[list[str]]:
    adjacent: dict[str, set[str]] = defaultdict(set)
    for row in edges:
        adjacent[row["left_id"]].add(row["right_id"])
        adjacent[row["right_id"]].add(row["left_id"])
    result, seen = [], set()
    for start in sorted(adjacent):
        if start in seen:
            continue
        pending, members = [start], set()
        while pending:
            ident = pending.pop()
            if ident in members:
                continue
            members.add(ident)
            pending.extend(adjacent[ident] - members)
        seen.update(members)
        result.append(sorted(members))
    return result


def _canonical_groups(groups: list[dict]) -> list[dict]:
    return [{**group, "baseline_group": True} for group in canonicalize_approved_groups(groups)]


def assemble_spec(reference_spec: dict, baseline: dict, incoming: dict,
                  vectors: dict[str, list[float]], *, review_run_id: str,
                  created_at: str, source_fingerprint: str) -> dict:
    """Pure bounded candidate generation; connected sets are review queues only."""
    if baseline.get("stage2_duplicate_gate_status") != "complete" or baseline.get("stage3_similarity_gate_status") != "complete":
        raise ValueError("complete human baseline stages 2 and 3 required")
    old_items = {row["id"]: copy.deepcopy(row) for row in reference_spec["items"]}
    new_items = incoming["items"]
    if len(new_items) > 300 or set(old_items).intersection(row["id"] for row in new_items):
        raise ValueError("incoming review must contain at most 300 new raw ids")
    items = dict(old_items)
    for index, source in enumerate(new_items):
        priority = rank_prompt(source.get("prompt", ""))
        priority.update({"ordinal": len(old_items) + index, "rank_index": len(old_items) + index})
        prompt = prompt_signals(source.get("prompt", ""))
        items[source["id"]] = {
            "id": source["id"], "style_id": source["style_id"],
            "prepared_path": "../inputs/" + source["prepared_sha256"] + ".png",
            "prepared_sha256": source["prepared_sha256"], "source_sha256": source["sha256"],
            "prompt_exact_sha256": prompt["exact_sha256"] if prompt["has_text"] else None,
            "prompt_normalized_sha256": prompt["normalized_sha256"] if prompt["has_text"] else None,
            "priority": priority,
        }
    for row in items.values():
        row["prepared_path"] = "../inputs/" + row["prepared_sha256"] + ".png"
    old_active = list(baseline["stage2_overlay"]["active_ids"])
    fresh = list(incoming["embedding_item_ids"])
    if len(set(fresh)) != len(fresh) or not set(fresh) <= set(items):
        raise ValueError("invalid fresh embedding ids")
    old_set, new_set = set(old_active), set(fresh)
    if old_set & new_set or not (old_set | new_set) <= set(vectors):
        raise ValueError("retained vectors missing")
    archived = copy.deepcopy(baseline["stage2_overlay"]["archived"])
    routes = {row["id"]: row["representative_id"] for row in archived}
    routes.update({row["id"]: row["representative_id"] for row in incoming["alias_routes"]})

    def keeper(ident: str) -> str:
        seen = set()
        while ident in routes:
            if ident in seen:
                raise ValueError("alias cycle")
            seen.add(ident)
            ident = routes[ident]
        if ident not in old_set | new_set:
            raise ValueError("alias does not terminate at a retained image")
        return ident

    for row in archived:
        row["representative_id"] = keeper(row["id"])
    for row in incoming["alias_routes"]:
        archived.append({"id": row["id"], "representative_id": keeper(row["id"]),
                         "action": "logical_delete", "reasons": copy.deepcopy(row.get("evidence", [])),
                         "provenance_only": True})
    archived_ids = [row["id"] for row in archived]
    if (len(set(archived_ids)) != len(archived_ids) or set(archived_ids) & (old_set | new_set)
            or set(items) != old_set | new_set | set(archived_ids)):
        raise ValueError("retention must partition the combined corpus")
    groups = _canonical_groups(baseline["approved_similarity_groups"])
    old_front = {row["id"]: row for row in baseline["private_front_export_items"]}
    prior_notes = {row["id"]: row.get("tags_text", "") for row in baseline.get("individual_approvals", [])}
    prior_notes.update({row["id"]: row.get("memo_text", "") for row in baseline.get("image_approvals", [])})
    seeds = [{"id": ident, "approved": ident in old_front, "memo_text": prior_notes.get(ident, "")}
             for ident in old_active]

    # Normalize once. This is arithmetic on already generated vectors, never inference.
    all_ids = old_active + fresh
    try:
        import numpy as np
        matrix = np.asarray([vectors[i] for i in all_ids], dtype=np.float64)
        if matrix.ndim != 2 or not np.isfinite(matrix).all():
            raise ValueError("invalid cached vectors")
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        if (norms <= 0).any():
            raise ValueError("zero cached vector")
        matrix = matrix / norms
        positions = {ident: i for i, ident in enumerate(all_ids)}
        scores = matrix[len(old_active):] @ matrix.T
        def score(left: str, right: str) -> float:
            return round(float(scores[positions[left] - len(old_active), positions[right]]), 6)
    except ImportError:
        def score(left: str, right: str) -> float:
            return round(cosine(vectors[left], vectors[right]), 6)

    edges, identity_edges = [], []
    for index, left in enumerate(fresh):
        for right in old_active + fresh[:index]:
            value = score(left, right)
            same_prompt = bool(items[left].get("prompt_exact_sha256") and
                               items[left]["prompt_exact_sha256"] == items[right].get("prompt_exact_sha256"))
            if value >= .73 or same_prompt:
                row = {"left_id": left, "right_id": right, "image_cosine": value,
                       "prompt_exact": same_prompt}
                edges.append(row)
                if value >= .98:
                    identity_edges.append(row)

    def ordered(members: list[str]) -> list[str]:
        return sorted(set(members), key=lambda ident: (ident not in old_set, priority_sort_key(items[ident]["priority"]), ident))

    def candidate(prefix: str, members: list[str], evidence: list[dict], anchors: list[str]) -> dict:
        members = ordered(members)
        return {"id": prefix + digest(json_bytes(sorted(members)))[:24], "member_ids": members,
                "baseline_anchor_ids": ordered(anchors), "representative_priority_ids": members,
                "suggested_representative_id": members[0], "candidate_only": True,
                "known_positive_pairs": [], "known_negative_pairs": [],
                "evidence": {"basis": "cached_vectors_human_review_only", "method": "incremental_cached_vectors_review_component_not_automatic_membership",
                             "min_cosine_hypothesis": .73, "calibrated_min_cosine_for_review": .73,
                             "pair_count": len(evidence), "pair_cosines": evidence, "pairs": evidence}}

    duplicate_candidates = []
    for component in _components(identity_edges):
        anchors = sorted(set(component) & old_set)
        if len(anchors) > 1:
            # An ambiguous bridge must not merge previously independent keepers.
            for anchor in anchors:
                relevant = [row for row in identity_edges if anchor in (row["left_id"], row["right_id"])]
                members = sorted({ident for row in relevant for ident in (row["left_id"], row["right_id"])})
                duplicate_candidates.append(candidate("duplicate-candidate-", members, relevant, [anchor]))
        else:
            relevant = [row for row in identity_edges if {row["left_id"], row["right_id"]} <= set(component)]
            duplicate_candidates.append(candidate("duplicate-candidate-", component, relevant, anchors))

    # Connected NEW images are shown together, but only a person's selected subset
    # becomes a group. Old groups are anchors, not automatic bridge merge edges.
    new_edges = [row for row in edges if row["right_id"] in new_set]
    components = _components(new_edges)
    in_components = {ident for members in components for ident in members}
    components += [[ident] for ident in fresh if ident not in in_components]
    similarity_candidates, seen = [], set()
    anchored_queues: dict[tuple[str, ...], set[str]] = defaultdict(set)
    unanchored_queues: list[list[str]] = []
    old_groups_by_member: dict[str, list[dict]] = defaultdict(list)
    for group in groups:
        for ident in group["member_ids"]:
            old_groups_by_member[ident].append(group)
    for component in components:
        component_set = set(component)
        touching = [row for row in edges if row["left_id"] in component_set and row["right_id"] in old_set]
        anchor_sets: set[tuple[str, ...]] = set()
        for edge in touching:
            matched = edge["right_id"]
            found = old_groups_by_member.get(matched, [])
            anchor_sets.update(tuple(sorted(group["member_ids"])) for group in found)
            if not found:
                anchor_sets.add((matched,))
        if anchor_sets:
            for anchors in anchor_sets:
                anchored_queues[anchors].update(component)
        else:
            unanchored_queues.append(component)
    # One old group appears once per queue, with all its new candidate images.
    # This consolidates presentation, not human-confirmed membership.
    queues = [(list(members), list(anchors)) for anchors, members in sorted(anchored_queues.items())]
    queues += [(members, []) for members in unanchored_queues]
    for new_members, anchors in queues:
        members = new_members + anchors
        if len(members) < 2:
            continue
        member_set = set(members)
        relevant = [row for row in edges if {row["left_id"], row["right_id"]} <= member_set]
        built = candidate("similarity-candidate-", members, relevant, anchors)
        if built["id"] not in seen:
            seen.add(built["id"])
            similarity_candidates.append(built)
    spec = {
        "schema_version": "image-group-workflow-spec-1", "run_id": review_run_id,
        "created_at": created_at, "source_manifest_sha256": source_fingerprint,
        "vector_fingerprint": digest(json_bytes({i: vectors[i] for i in all_ids})),
        "source_labels_sha256": digest(json_bytes(baseline)), "approval_policy": POLICY,
        "items": list(items.values()), "initial_image_approvals": seeds,
        "baseline": {"read_only_ids": old_active, "image_approvals": seeds, "groups": groups,
                     "source_run_id": reference_spec["run_id"], "source_spec_sha256": reference_spec["spec_sha256"]},
        "baseline_front_url": "baseline-front/current/private-front.html",
        "new_item_ids": [row["id"] for row in new_items],
        "stage1": {"active_ids": old_active + fresh, "archived": archived,
                   "alias_lineage": copy.deepcopy(incoming["alias_routes"]),
                   "policy": "frozen_human_baseline_plus_machine_exact_new_aliases"},
        "duplicate_candidates": duplicate_candidates, "similarity_candidates": similarity_candidates,
        "metadata_optional": True, "front_review_requires_explicit_complete": False,
        "release_eligible": False, "public_rights_approved": False,
        "notes": ["Existing human choices remain read-only, including explicit unchecked images.",
                  "Stage 3 completion defaults retained NEW images to approved; optional personal memos are not labels inferred by AI.",
                  "Connected candidate components are display queues only; no automatic semantic merge or physical deletion."],
    }
    spec["spec_sha256"] = digest(json_bytes(spec))
    return spec


def build_incremental_workflow(root: Path, incoming_run_id: str, decisions_path: Path,
                               review_run_id: str, *, apply: bool = False) -> dict:
    root = root.resolve()
    with ExitStack() as locks:
        if apply:
            source = run_path(root, incoming_run_id)
            locks.enter_context(run_lock(source))
            reference_id = read_json(source / "manifest.json")["reference_run_id"]
            locks.enter_context(run_lock(run_path(root, reference_id) / REVIEW_DIR))
        return _build_incremental_workflow(root, incoming_run_id, decisions_path, review_run_id, apply=apply)


def _build_incremental_workflow(root: Path, incoming_run_id: str, decisions_path: Path,
                                review_run_id: str, *, apply: bool = False) -> dict:
    root = root.resolve()
    destination = run_path(root, review_run_id)
    if destination.exists():
        raise FileExistsError("review snapshot already exists; use its immutable files")
    data = _load(root, incoming_run_id)
    frozen_paths = set(data["bound"])
    frozen_paths.update(path.resolve() for path in data["destination"].rglob("*.json"))
    frozen_paths.update((data["source"] / name).resolve() for name in ("manifest.json", "prepared.json", "source-bindings.json"))
    frozen_paths.update((root / row["path"]).resolve() for row in data["manifest"]["items"])
    if any(not path.is_relative_to(root) for path in frozen_paths):
        raise ValueError("source binding escapes archive root")
    files = [{"path": path.relative_to(root).as_posix(), "sha256": digest(path.read_bytes())} for path in sorted(frozen_paths)]
    if any(digest(path.read_bytes()) != sha for path, sha in data["bound"].items()):
        raise ValueError("bound source changed after validation")
    _ledger, cache, _receipts = _state(data)
    receipt = read_json(data["destination"] / "execution-receipt.json")
    expected_receipt = {"schema_version": EMBEDDING_SCHEMA, "run_id": incoming_run_id, "provider": "voyage", "model": VOYAGE_MODEL,
                        "manifest_sha256": data["manifest_sha256"], "source_bindings_sha256": data["source_bindings_sha256"],
                        "status": "completed", "completed_image_ids": len(data["chosen"]), "target_image_ids": len(data["chosen"])}
    if (not isinstance(receipt, dict) or any(receipt.get(key) != value for key, value in expected_receipt.items())
            or type(receipt.get("completed_image_ids")) is not int or type(receipt.get("target_image_ids")) is not int
            or set(cache) != set(data["unique"])):
        raise ValueError("all incremental embeddings must be completed and validated")
    reference = run_path(root, data["manifest"]["reference_run_id"])
    old_spec = read_json(reference / REVIEW_DIR / "image-group-workflow.spec.json")
    raw_decisions = decisions_path.read_bytes()
    submitted = json.loads(raw_decisions.decode("utf-8-sig"))
    normalized = validate_group_workflow_decisions(old_spec, submitted)
    vectors = read_json(reference / "comparison-v1/vectors.json")["voyage_image"]
    vectors.update({ident: cache[data["requests"][ident]["key"]]["vector"] for ident in data["chosen"]})
    old_manifest = read_json(reference / "manifest.json")
    fingerprint = digest(json_bytes({"reference": old_manifest, "incoming": data["manifest"],
                                    "decisions_sha256": digest(raw_decisions)}))
    spec = assemble_spec(old_spec, normalized, data["manifest"], vectors, review_run_id=review_run_id,
                         created_at=now(), source_fingerprint=fingerprint)
    copied: dict[str, bytes] = {}
    for parent, rows in ((reference, old_manifest["items"]), (data["source"], data["manifest"]["items"])):
        for row in rows:
            path = (parent / row["prepared_path"]).resolve()
            if not path.is_relative_to((parent / "inputs").resolve()):
                raise ValueError("prepared input escapes source run")
            content = path.read_bytes()
            if digest(content) != row["prepared_sha256"]:
                raise ValueError("prepared image drift")
            copied[row["prepared_sha256"]] = content
    source_binding = {"schema_version": SCHEMA, "files": files, "source_decisions_sha256": digest(raw_decisions),
                      "review_spec_sha256": spec["spec_sha256"], "provider_calls": 0,
                      "basis": "completed_embedding_snapshot_validated_before_human_baseline_import"}
    template = blank_group_workflow_decisions(spec, schema_version="image-group-workflow-decisions-3")
    baseline_groups = {"run_id": review_run_id, "spec_sha256": spec["spec_sha256"], "groups": spec["baseline"]["groups"]}
    previews = {row["id"]: row["prepared_path"] for row in spec["items"]}
    baseline_items = [{**row, "prepared_path": previews[row["id"]]} for row in normalized["private_front_export_items"]]
    baseline_export = {"run_id": review_run_id, "spec_sha256": spec["spec_sha256"], "status": "ready",
                       "front_review_complete": True, "stage2_duplicate_gate_status": "complete",
                       "stage3_similarity_gate_status": "complete", "stage4_gate_status": "unlocked",
                       "items": baseline_items, "public_rights_approved": False,
                       "release_eligible": False}
    # Validate both renderers and the blank v3 decision contract even in dry-run.
    reviewed_html = render_group_review(spec)
    baseline_html = render_approved_front(baseline_export, baseline_groups)
    blank = validate_group_workflow_decisions(spec, {**template, "reviewer": "build-contract-check-not-human-approval", "reviewed_at": spec["created_at"].replace("+00:00", "Z")})
    if spec["duplicate_candidates"] or spec["similarity_candidates"]:
        if blank["private_front_export_items"]:
            raise ValueError("unreviewed incoming images cannot enter front export")
    summary = {"schema_version": SCHEMA, "status": "ready" if apply else "dry_run", "run_id": review_run_id,
               "spec_sha256": spec["spec_sha256"], "binding_sha256": digest(json_bytes(source_binding)),
               "source_decisions_sha256": digest(raw_decisions), "raw_items": len(spec["items"]),
               "existing_retained": len(spec["baseline"]["read_only_ids"]),
               "existing_approved": len(normalized["private_front_export_items"]),
               "existing_canonical_groups": len(spec["baseline"]["groups"]),
               "new_raw_items": len(data["manifest"]["items"]), "new_retained": len(data["chosen"]),
               "local_vector_pair_comparisons": len(data["chosen"]) * len(spec["baseline"]["read_only_ids"]) + len(data["chosen"]) * (len(data["chosen"]) - 1) // 2,
               "duplicate_candidates": len(spec["duplicate_candidates"]),
               "similarity_candidates": len(spec["similarity_candidates"]),
               "similarity_candidate_sizes": sorted([len(row["member_ids"]) for row in spec["similarity_candidates"]], reverse=True),
               "unique_preview_inputs": len(copied), "provider_calls": 0, "physical_deletions": 0,
               "canonical_writes": 0, "new_human_approvals": 0,
               "html_path": str(destination / REVIEW_DIR / "image-group-workflow.html")}
    if apply:
        if any(digest((root / row["path"]).read_bytes()) != row["sha256"] for row in files):
            raise ValueError("source changed during snapshot construction")
        destination.mkdir(parents=True, exist_ok=False)
        with run_lock(destination):
            workflow = destination / REVIEW_DIR
            workflow.mkdir(parents=True, exist_ok=True)
            (destination / "inputs").mkdir(parents=True, exist_ok=True)
            for sha, content in copied.items():
                (destination / "inputs" / (sha + ".png")).write_bytes(content)
            (workflow / "submitted-baseline.raw.json").write_bytes(raw_decisions)
            write_json(workflow / "baseline.normalized.json", normalized)
            write_json(workflow / "source-bindings.json", source_binding)
            write_json(workflow / "image-group-workflow.spec.json", spec)
            write_json(workflow / "image-group-workflow.template.json", template)
            write_json(workflow / "build-receipt.json", summary)
            (workflow / "image-group-workflow.html").write_text(reviewed_html, encoding="utf-8")
            baseline_dir = workflow / "baseline-front/current"
            baseline_dir.mkdir(parents=True, exist_ok=True)
            write_json(baseline_dir / "private-front-export.json", baseline_export)
            write_json(baseline_dir / "approved-groups.json", baseline_groups)
            (baseline_dir / "private-front.html").write_text(baseline_html, encoding="utf-8")
    return summary


def load_frozen_workflow(root: Path, review_run_id: str) -> dict:
    directory = run_path(root, review_run_id) / REVIEW_DIR
    receipt = read_json(directory / "build-receipt.json")
    if receipt.get("schema_version") == "image-v2-intake-review-1":
        # Source-neutral runs have their own origin/media/cache evidence
        # validator; they must never inherit the CASE-only legacy validator.
        from .intake_review import load_intake_review
        return load_intake_review(root, review_run_id)
    spec = read_json(directory / "image-group-workflow.spec.json")
    binding = read_json(directory / "source-bindings.json")
    unhashed = {key: value for key, value in spec.items() if key != "spec_sha256"}
    if (spec.get("schema_version") != "image-group-workflow-spec-1" or spec.get("approval_policy") != POLICY
            or receipt.get("schema_version") != SCHEMA or receipt.get("status") != "ready"
            or receipt.get("run_id") != review_run_id or receipt.get("spec_sha256") != spec.get("spec_sha256")
            or spec.get("run_id") != review_run_id or spec.get("spec_sha256") != digest(json_bytes(unhashed))
            or binding.get("review_spec_sha256") != spec["spec_sha256"]
            or receipt.get("binding_sha256") != digest(json_bytes(binding))
            or binding.get("source_decisions_sha256") != digest((directory / "submitted-baseline.raw.json").read_bytes())):
        raise ValueError("frozen workflow identity mismatch")
    for row in binding["files"]:
        if Path(row["path"]).is_absolute() or ".." in Path(row["path"]).parts:
            raise ValueError("unsafe frozen evidence path")
        path = (root / row["path"]).resolve()
        if not path.is_relative_to(root.resolve()) or digest(path.read_bytes()) != row["sha256"]:
            raise ValueError("frozen evidence changed")
    for item in spec["items"]:
        relative = item["prepared_path"]
        if not re.fullmatch(r"\.\./inputs/[a-f0-9]{64}\.png", relative):
            raise ValueError("unsafe frozen preview")
        if digest((directory / relative).read_bytes()) != item["prepared_sha256"]:
            raise ValueError("frozen preview changed")
    return spec


def import_incremental_decisions(root: Path, review_run_id: str, decisions_path: Path, *, apply: bool = False) -> dict:
    root = root.resolve()
    with run_lock(run_path(root, review_run_id) / REVIEW_DIR) if apply else nullcontext():
        return _import_incremental_decisions(root, review_run_id, decisions_path, apply=apply)


def _import_incremental_decisions(root: Path, review_run_id: str, decisions_path: Path, *, apply: bool = False) -> dict:
    root = root.resolve()
    spec = load_frozen_workflow(root, review_run_id)
    submitted_raw = decisions_path.read_bytes()
    submitted = json.loads(submitted_raw.decode("utf-8-sig"))
    if submitted.get("schema_version") != "image-group-workflow-decisions-3":
        raise ValueError("incremental review requires v3 decisions")
    normalized = validate_group_workflow_decisions(spec, submitted)
    identity = digest(json_bytes(normalized))
    directory = run_path(root, review_run_id) / REVIEW_DIR / "decision-imports" / identity[:24]
    groups = {"run_id": review_run_id, "spec_sha256": spec["spec_sha256"], "groups": normalized["approved_similarity_groups"]}
    export = {key: normalized[key] for key in ("run_id", "spec_sha256", "front_review_complete", "stage2_duplicate_gate_status",
                                               "stage3_similarity_gate_status", "stage4_gate_status")}
    export.update({"status": normalized["private_front_export_status"], "items": normalized["private_front_export_items"],
                   "decisions_schema_version": "image-group-workflow-decisions-3",
                   "front_approval_policy": POLICY,
                   "release_eligible": False, "public_rights_approved": False})
    result = {"schema_version": SCHEMA, "status": "imported" if apply else "dry_run", "decisions_sha256": identity,
              "spec_sha256": spec["spec_sha256"], "source_decisions_sha256": digest(submitted_raw), "front_items": len(export["items"]),
              "stage2": export["stage2_duplicate_gate_status"], "stage3": export["stage3_similarity_gate_status"],
              "stage4": export["stage4_gate_status"], "physical_deletions": 0, "provider_calls": 0,
              "canonical_writes": 0, "public_release_approval": False, "import_dir": str(directory)}
    if apply:
        payloads = {"decisions.json": normalized, "private-front-export.json": export, "approved-groups.json": groups,
                    "retention-overlay.json": normalized["stage2_overlay"], "receipt.json": result}
        rendered = render_approved_front(export, groups)
        for name, payload in payloads.items():
            path = directory / name
            if path.exists() and read_json(path) != payload:
                raise ValueError("existing immutable import differs")
        if (directory / "private-front.html").exists() and (directory / "private-front.html").read_text(encoding="utf-8") != rendered:
            raise ValueError("existing private front differs")
        if (directory / "submitted.raw.json").exists() and (directory / "submitted.raw.json").read_bytes() != submitted_raw:
            raise ValueError("existing immutable raw submission differs")
        directory.mkdir(parents=True, exist_ok=True)
        for name, payload in payloads.items():
            write_json(directory / name, payload)
        (directory / "submitted.raw.json").write_bytes(submitted_raw)
        (directory / "private-front.html").write_text(rendered, encoding="utf-8")
    return result
