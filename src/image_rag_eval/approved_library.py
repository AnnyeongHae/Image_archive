"""Portable, private approved-library projection and frozen full-prompt lookup.

The projection is pure Python data transformation. It never calculates image
similarity or changes human membership. The separate source loader reads only
hash-bound manifests/canonical records, never a handoff artifact or URL.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

SCHEMA = "image-approved-library-1"
PROMPT_SCHEMA = "image-original-prompt-1"
ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")


def _digest(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _json_bytes(value) -> bytes:
    # Same frozen-workflow identity protocol as experiment.json_bytes.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


def _path(root: Path, relative: str) -> Path:
    if (not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative
            or Path(relative).is_absolute() or ".." in Path(relative).parts):
        raise ValueError("Unsafe prompt evidence path")
    result = (root / relative).resolve()
    if not result.is_relative_to(root):
        raise ValueError("Prompt evidence escaped archive root")
    return result


def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Prompt evidence must be a JSON object")
    return value


def _ids(values, label: str) -> list[str]:
    if not isinstance(values, list) or any(not isinstance(value, str) or not ID.fullmatch(value) for value in values):
        raise ValueError(f"Invalid {label} IDs")
    if len(values) != len(set(values)):
        raise ValueError(f"Duplicate {label} ID")
    return list(values)


def missing_prompt(ident: str, *, status: str = "unavailable") -> dict:
    return {"schema_version": PROMPT_SCHEMA, "id": ident, "status": status,
            "full_prompt": "" if status == "missing" else None, "prompt_sha256": None,
            "source_binding": None, "release_eligible": False}


def build_prompt_catalog(root: Path, spec: dict) -> dict[str, dict]:
    """Resolve raw prompt text without `.strip`, reformatting or truncation.

    Historical `prompt_truncated` flags the embedding-input truncation. For
    such rows, require an independently pinned canonical full prompt anyway;
    absence is explicit unavailable, never a misleading 'full' label.
    """
    root = Path(root).resolve()
    run_id = spec.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", run_id):
        raise ValueError("Invalid prompt source run")
    items = {row["id"]: row for row in spec["items"]}
    _ids([row["id"] for row in spec["items"]], "frozen image")
    directory = _path(root, f"data/private-research/image-rag-canary/runs/{run_id}/group-workflow-v1")
    frozen = _read(directory / "image-group-workflow.spec.json")
    binding = _read(directory / "source-bindings.json")
    receipt = _read(directory / "build-receipt.json")
    unhashed = {key: value for key, value in spec.items() if key != "spec_sha256"}
    if (frozen != spec or spec.get("schema_version") != "image-group-workflow-spec-1"
            or spec.get("spec_sha256") != _digest(_json_bytes(unhashed))
            or binding.get("review_spec_sha256") != spec["spec_sha256"]
            or receipt.get("status") != "ready" or receipt.get("run_id") != run_id
            or receipt.get("spec_sha256") != spec["spec_sha256"]
            or receipt.get("binding_sha256") != _digest(_json_bytes(binding))):
        raise ValueError("Full prompt source binding mismatch")
    files = {}
    for row in binding["files"]:
        path, sha = row.get("path"), row.get("sha256")
        if not isinstance(sha, str) or not SHA.fullmatch(sha) or path in files:
            raise ValueError("Invalid or duplicate prompt source binding")
        _path(root, path)
        files[path] = sha
    sources = {}
    for relative, sha in files.items():
        if Path(relative).name != "manifest.json":
            continue
        path = _path(root, relative)
        raw = path.read_bytes()
        if _digest(raw) != sha:
            raise ValueError("Pinned prompt manifest changed")
        manifest = json.loads(raw.decode("utf-8-sig"))
        if manifest.get("schema_version") not in {"1", "image-incremental-manifest-1", "image-v2-intake-manifest-1"}:
            continue
        for row in manifest.get("items", []):
            ident = row.get("id")
            if ident not in items:
                continue
            item = items[ident]
            if (row.get("style_id") != item["style_id"] or row.get("sha256") != item["source_sha256"]
                    or row.get("prepared_sha256") != item["prepared_sha256"]):
                raise ValueError("Full prompt image identity mismatch")
            if ident in sources:
                raise ValueError("Ambiguous full prompt manifest identity")
            sources[ident] = {"item": row, "manifest_sha256": sha}
    canonical_relative = "data/canonical/archive_records.jsonl"
    canonical_sha = files.get(canonical_relative)
    canonical = {}
    if canonical_sha is not None:
        path = _path(root, canonical_relative)
        # Hash and parse the same immutable bytes to avoid read/parse races.
        raw = path.read_bytes()
        if _digest(raw) != canonical_sha:
            raise ValueError("Pinned original prompt catalog changed")
        wanted = {row["item"].get("catalog_key") for row in sources.values()}
        for line in raw.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            key = record.get("catalog_key")
            if key in wanted:
                if key in canonical:
                    raise ValueError("Ambiguous original prompt catalog key")
                canonical[key] = record
    result = {}
    for ident, item in items.items():
        source = sources.get(ident)
        if source is None:
            result[ident] = missing_prompt(ident)
            continue
        original = source["item"]
        raw_prompt = original.get("prompt")
        record = canonical.get(original.get("catalog_key"))
        origin = "pinned_manifest.prompt"
        if record is not None:
            prompt = record.get("prompt")
            canonical_text = prompt.get("text") if isinstance(prompt, dict) else None
            if record.get("style_id") != item["style_id"] or not isinstance(canonical_text, str):
                result[ident] = missing_prompt(ident)
                continue
            if raw_prompt != canonical_text:
                raise ValueError("Pinned manifest and full original prompt disagree")
            raw_prompt, origin = canonical_text, "pinned_canonical.prompt.text"
        elif original.get("prompt_truncated") is True:
            result[ident] = missing_prompt(ident)
            continue
        if not isinstance(raw_prompt, str):
            result[ident] = missing_prompt(ident)
            continue
        result[ident] = {"schema_version": PROMPT_SCHEMA, "id": ident,
            "status": "available" if raw_prompt.strip() else "missing", "full_prompt": raw_prompt,
            "prompt_sha256": _digest(raw_prompt.encode("utf-8")),
            "source_binding": {"run_id": run_id, "spec_sha256": spec["spec_sha256"],
                               "manifest_sha256": source["manifest_sha256"],
                               "canonical_sha256": canonical_sha if record is not None else None,
                               "original_image_sha256": item["source_sha256"], "prompt_field": "prompt",
                               "origin": origin}, "release_eligible": False}
    return result


def project_approved_library(gallery: dict, prompt_catalog: dict | None = None, *, include_prompt_text: bool = False) -> dict:
    """Project committed groups; no clustering, similarity thresholds or union.

    A partially overlapping human-approved group remains a separate group.
    Members explicitly unchecked for the front stay hidden, without altering
    the retained human membership. Representatives fall back deterministically
    if their own image was unchecked. A group with <2 visible members becomes
    ordinary ungrouped display; its committed source group remains untouched.
    """
    prompt_catalog = prompt_catalog or {}
    items = copy.deepcopy(gallery.get("items", []))
    approved = _ids([row["id"] for row in items], "approved image")
    retained = set(_ids(gallery.get("retained_ids", approved), "retained image"))
    if not set(approved).issubset(retained):
        raise ValueError("Approved library contains a non-retained image")
    lookup = {row["id"]: row for row in items}
    groups, seen_groups = [], set()
    for source in gallery.get("groups", []):
        group_id = source.get("candidate_id") or source.get("group_id")
        if not isinstance(group_id, str) or not ID.fullmatch(group_id) or group_id in seen_groups:
            raise ValueError("Invalid or duplicate human-approved group ID")
        seen_groups.add(group_id)
        members = _ids(source.get("member_ids"), "group member")
        if not set(members).issubset(retained):
            raise ValueError("Human-approved group contains an unknown retained image")
        visible = [ident for ident in members if ident in lookup]
        if len(visible) < 2:
            continue
        candidates = [source.get("suggested_representative_id"), *source.get("representative_priority_ids", []), *visible]
        representative = next(ident for ident in candidates if ident in visible)
        provenance = list(dict.fromkeys(source.get("source_candidate_ids", []) or [group_id]))
        groups.append({"group_id": group_id, "representative_id": representative,
                       "member_ids": visible, "source_candidate_ids": provenance,
                       "hidden_member_count": len(members) - len(visible),
                       "membership_basis": "committed_human_approval"})
    membership = Counter(ident for group in groups for ident in group["member_ids"])
    ungrouped = [ident for ident in approved if ident not in membership]
    for row in items:
        prompt = prompt_catalog.get(row["id"], missing_prompt(row["id"]))
        row["prompt_status"] = prompt["status"]
        row["prompt_sha256"] = prompt["prompt_sha256"]
        if include_prompt_text:
            row["original_prompt"] = copy.deepcopy(prompt)
    return {"schema_version": SCHEMA, "run_id": gallery.get("run_id"), "source_commit_id": gallery.get("commit_id"),
            "commit_revision": gallery.get("revision"), "decisions_sha256": gallery.get("decisions_sha256"),
            "items": items, "display_groups": groups, "ungrouped_ids": ungrouped,
            "counts": {"approved_images": len(items), "display_groups": len(groups), "grouped_images": len(membership),
                       "ungrouped_images": len(ungrouped), "overlapping_images": sum(count > 1 for count in membership.values()),
                       "source_human_groups": len(gallery.get("groups", []))},
            "group_membership_policy": "committed_only_no_automatic_merge", "release_eligible": False,
            "public_rights_approved": False}
