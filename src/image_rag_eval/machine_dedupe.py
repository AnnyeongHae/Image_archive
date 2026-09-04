"""Exact image identity overlay for the new workflow; old runs stay immutable."""
from __future__ import annotations

import copy
import re
from collections import defaultdict
from typing import Any

from .experiment import digest, json_bytes
from .similarity import prompt_signals


POLICY = "machine_exact_file_or_decoded_pixels_independent_of_prompt_v1"


def _hash(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValueError("image identity requires a full SHA-256 or no evidence")
    return value.lower()


def build_machine_retention(source: dict[str, Any]) -> dict[str, Any]:
    """Use only validated original file/full-pixel hashes, never prompt or pHash.

    Exact hash equivalence is transitive; semantic-similarity relationships are
    not used in this union. Existing prompt-quality/arrival ranking is retained.
    """
    items = source["items"]
    by_id = {item["id"]: item for item in items}
    if len(by_id) != len(items) or any(not isinstance(key, str) or not key for key in by_id):
        raise ValueError("machine retention requires unique nonempty item ids")
    priorities = copy.deepcopy(source["retention"]["priority_by_id"])
    if any(item_id not in priorities for item_id in by_id):
        raise ValueError("machine retention is missing verified priority evidence")
    for item_id in by_id:
        rank = priorities[item_id].get("rank_index")
        if type(rank) is not int or rank < 1:
            raise ValueError("machine retention requires a positive priority rank")
    ordered = sorted(by_id, key=lambda item_id: (priorities[item_id]["rank_index"], item_id))
    parent = {item_id: item_id for item_id in ordered}

    def find(item_id: str) -> str:
        while parent[item_id] != item_id:
            parent[item_id] = parent[parent[item_id]]
            item_id = parent[item_id]
        return item_id

    hashes: dict[str, dict[str, str | None]] = {}
    first_by_hash: dict[tuple[str, str], str] = {}
    exact_edges: list[dict[str, Any]] = []
    for item_id in ordered:
        item = by_id[item_id]
        signals = item.get("signals") if isinstance(item.get("signals"), dict) else {}
        file_sha = _hash(item.get("sha256"))
        signal_sha = _hash(signals.get("sha256"))
        if file_sha and signal_sha and file_sha != signal_sha:
            raise ValueError("original file identity evidence conflicts")
        pixel_sha = _hash(item.get("pixel_sha256"))
        signal_pixel_sha = _hash(signals.get("pixel_sha256"))
        if pixel_sha and signal_pixel_sha and pixel_sha != signal_pixel_sha:
            raise ValueError("decoded pixel identity evidence conflicts")
        hashes[item_id] = {
            "file_sha256": file_sha or signal_sha,
            "pixel_sha256": signal_pixel_sha or pixel_sha,
        }
        for kind, sha in hashes[item_id].items():
            if sha is None:
                continue
            key = (kind, sha)
            previous = first_by_hash.get(key)
            if previous is None:
                first_by_hash[key] = item_id
                continue
            parent[find(item_id)] = find(previous)
            exact_edges.append({"left_id": previous, "right_id": item_id, "kind": kind, "sha256": sha})

    components: dict[str, list[str]] = defaultdict(list)
    for item_id in ordered:
        components[find(item_id)].append(item_id)
    active_ids: list[str] = []
    archived: list[dict[str, Any]] = []
    exact_groups: list[dict[str, Any]] = []
    lineage: list[dict[str, Any]] = []
    for members in components.values():
        keeper = members[0]  # Already ordered by prompt quality, then arrival.
        active_ids.append(keeper)
        member_set = set(members)
        edges = [edge for edge in exact_edges if edge["left_id"] in member_set and edge["right_id"] in member_set]
        group_id = "machine-exact-" + digest(json_bytes(sorted(members)))[:24]
        for item_id in members:
            priorities[item_id]["representative_id"] = keeper
            priorities[item_id]["selected_as_representative"] = item_id == keeper
            if item_id != keeper:
                archived.append({
                    "id": item_id, "representative_id": keeper, "action": "logical_delete",
                    "reasons": [{"kind": "computer_exact_image", "basis": POLICY,
                                 "exact_group_id": group_id, "identity_hashes": hashes[item_id]}],
                })
        if len(members) > 1:
            exact_groups.append({"group_id": group_id, "kind": "exact_logical_delete",
                                 "representative_id": keeper, "member_ids": members,
                                 "evidence": {"basis": POLICY, "pairs": edges}})
        aliases = []
        for item_id in members:
            item = by_id[item_id]
            aliases.append({**{key: copy.deepcopy(item.get(key)) for key in (
                "id", "style_id", "prompt", "path", "source_name", "lane", "record_id", "catalog_key", "source_aliases"
            )}, **hashes[item_id]})
        lineage.append({"representative_id": keeper, "archived_exact_ids": members[1:],
                        "member_ids": members, "aliases": aliases})

    prompt_members: dict[str, list[str]] = defaultdict(list)
    for item_id in active_ids:
        prompt = by_id[item_id].get("prompt", "")
        info = prompt_signals(prompt if isinstance(prompt, str) else "")
        if info["has_text"]:
            prompt_members[info["exact_sha256"]].append(item_id)
    variants = [{"group_id": "prompt-variant-" + digest(sha.encode())[:24], "kind": "prompt_variant",
                 "representative_id": members[0], "member_ids": members,
                 "evidence": {"prompt_exact_sha256": sha, "basis": "prompt_only_never_image_deletion"}}
                for sha, members in sorted(prompt_members.items()) if len(members) > 1]
    return {
        "schema_version": "machine-image-retention-1", "policy": POLICY,
        "selection_mode": "exact_hash_components_then_existing_prompt_quality_arrival_rank",
        "active_ids": active_ids, "archived": archived, "deleted_ids": [row["id"] for row in archived],
        "exact_groups": exact_groups, "prompt_variant_groups": variants, "priority_by_id": priorities,
        "order_evidence": copy.deepcopy(source["retention"].get("order_evidence", [])),
        "alias_lineage": lineage, "reversible": True, "source_mutations": False,
        "physical_deletions": 0,
    }


__all__ = ["POLICY", "build_machine_retention"]
