"""Offline CASE remainder inventory and exact-match proposals, never approval.

Compare against every committed retained image AND every archived fingerprint.
Original files and existing representative choices stay immutable. New semantic
embeddings, human decisions, canonical promotion and deployment are not executed.
"""
from __future__ import annotations

import copy
import html
import json
import os
import re
import tempfile
from collections import defaultdict
from pathlib import Path

from . import dataset
from .approval_handoff import _archive, _committed, _load_spec, _require_latest, _sources, _validate_commit
from .comparison import request_key
from .experiment import digest, json_bytes, run_path, safe_source
from .expansion import _prepare_item, _resolve_additional_items
from .incremental import PUBLIC_CATALOG_PATH, _file_binding, _public_case_ids, select_case_candidates
from .prompt_priority import priority_sort_key, rank_prompt
from .shared_vector_cache import RELATIVE_ROOT as CACHE_ROOT, lookup_shared_vectors
from .similarity import _decoded_rgba_image, _pixel_sha256, prompt_signals

SCHEMA = "image-remaining-case-review-1"
DIRECTORY = "remaining-review-v1"
MAX_IMAGES = 300
MODEL = "voyage-multimodal-3.5"
DIMENSIONS = 1024


def _fingerprint(item):
    sha, pixel = item.get("sha256"), item.get("signals", {}).get("pixel_sha256")
    if any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in (sha, pixel)):
        raise ValueError("original file and full decoded pixel fingerprints are required")
    if item.get("signals", {}).get("sha256") not in (None, sha):
        raise ValueError("file fingerprint evidence conflicts")
    prompt = prompt_signals(item.get("prompt", ""))
    return {"file": sha, "pixels": pixel, "prompt": prompt["exact_sha256"] if prompt["has_text"] else None}


def propose_exact_relations(incoming: list[dict], existing: list[dict], retained_ids: list[str],
                            archived_aliases: list[dict]) -> dict:
    """Only file OR (pixels AND nonblank exact prompt) forms identity edges.

    Exact equality is transitive; perceptual/prompt-only similarity never unions
    components. Bridges across existing anchors remain an explicit human queue.
    """
    all_items = existing + incoming
    by_id = {row["id"]: row for row in all_items}
    old_ids, new_ids = {row["id"] for row in existing}, {row["id"] for row in incoming}
    retained = set(retained_ids)
    if (len(by_id) != len(all_items) or len(retained) != len(retained_ids) or not retained <= old_ids
            or len(incoming) > MAX_IMAGES):
        raise ValueError("unique bounded new IDs and valid retained IDs are required")
    aliases = {row["id"]: row["representative_id"] for row in archived_aliases}
    if (len(aliases) != len(archived_aliases) or retained & set(aliases)
            or retained | set(aliases) != old_ids):
        raise ValueError("retained and archived fingerprints must partition the committed baseline")
    anchors = {ident: ident for ident in retained}
    for ident in aliases:
        anchor, visited = aliases[ident], {ident}
        while anchor in aliases:
            if anchor in visited:
                raise ValueError("archived alias cycle")
            visited.add(anchor)
            anchor = aliases[anchor]
        if anchor not in retained:
            raise ValueError("archived fingerprint has no retained anchor")
        anchors[ident] = anchor
    fingerprints = {ident: _fingerprint(row) for ident, row in by_id.items()}
    parent = {ident: ident for ident in by_id}

    def find(ident):
        while parent[ident] != ident:
            parent[ident] = parent[parent[ident]]
            ident = parent[ident]
        return ident

    indexed, edges = {}, []
    for ident in by_id:
        value = fingerprints[ident]
        keys = [("exact_file", value["file"])]
        if value["prompt"]:
            keys.append(("exact_pixels_and_prompt", value["pixels"], value["prompt"]))
        for key in keys:
            previous = indexed.setdefault(key, ident)
            if previous != ident:
                parent[find(ident)] = find(previous)
                edges.append({"left_id": previous, "right_id": ident, "kind": key[0]})
    components = defaultdict(list)
    for ident in by_id:
        components[find(ident)].append(ident)
    arrival = {row["id"]: index for index, row in enumerate(incoming)}
    proposals, conflicts, retained_new = [], [], set()
    for members in components.values():
        new_members = [ident for ident in members if ident in new_ids]
        if not new_members:
            continue
        old_members = [ident for ident in members if ident in old_ids]
        old_anchors = sorted({anchors[ident] for ident in old_members})
        component_edges = [edge for edge in edges if edge["left_id"] in members and edge["right_id"] in members]
        if len(old_anchors) > 1:
            conflicts.append({"kind": "exact_bridge_between_committed_anchors", "member_ids": members,
                              "incoming_ids": new_members, "existing_anchor_ids": old_anchors,
                              "evidence_edges": component_edges, "action": "human_resolution_required"})
            continue
        if old_anchors:
            representative = old_anchors[0]
            policy = "preserve_committed_anchor_attach_better_prompt_as_alias_only"
        else:
            representative = min(new_members, key=lambda ident: (
                *priority_sort_key(rank_prompt(by_id[ident].get("prompt", ""))), arrival[ident], ident))
            policy = "new_exact_component_useful_JSON_then_structure_then_arrival"
            retained_new.add(representative)
        proposed_alias_ids = [ident for ident in new_members if ident != representative]
        if proposed_alias_ids:
            proposals.append({"kind": "deterministic_exact_alias_proposal", "member_ids": members,
                "incoming_ids": new_members, "proposed_alias_ids": proposed_alias_ids,
                "representative_id": representative, "representative_selection_policy": policy,
                "matched_archived_alias_ids": [ident for ident in old_members if ident in aliases],
                "evidence_edges": component_edges, "action": "proposed_logical_alias_not_applied",
                "existing_anchor_replaced": False, "physical_deletions": 0})
    # Independent equality buckets show all siblings together, not isolated pairs.
    review_groups = []
    for signal, kind in (("pixels", "same_pixels_different_prompt"), ("prompt", "prompt_exact_group_only")):
        buckets = defaultdict(list)
        for ident, values in fingerprints.items():
            if values[signal]:
                buckets[values[signal]].append(ident)
        for sha, members in sorted(buckets.items()):
            if not new_ids.intersection(members) or len(members) < 2:
                continue
            # Already-proven identities are covered by exact proposals. Do not
            # present them again as a prompt/near-similarity deletion decision.
            if len({find(ident) for ident in members}) < 2:
                continue
            review_groups.append({"kind": kind, "member_ids": members, "evidence_sha256": sha,
                "existing_anchor_ids": sorted({anchors[ident] for ident in members if ident in old_ids}),
                "action": "human_review_only_no_deletion_or_group_approval", "automatic_union": False})
    return {"policy": "exact_file_OR_full_pixels_AND_nonempty_exact_prompt_v1",
        "retained_new_candidate_ids": [row["id"] for row in incoming if row["id"] in retained_new],
        "exact_alias_proposals": proposals, "anchor_conflicts": conflicts, "human_groups": review_groups,
        "old_new_exact_comparisons": len(existing) * len(incoming),
        "new_new_exact_comparisons": len(incoming) * (len(incoming) - 1) // 2,
        "comparison_method": "hash_index_complete_pair_coverage_not_pairwise_loop",
        "existing_alias_anchor_map": anchors, "human_approval_inferred": False,
        "automatic_group_merges": 0, "physical_deletions": 0}


def _request(item):
    return {"provider": "voyage", "model": MODEL, "dimensions": DIMENSIONS,
            "image_sha256": item["prepared_sha256"], "text": "", "task": "RETRIEVAL_DOCUMENT"}


def _vector_inventory(root, items, candidate_ids):
    requests = [_request(item) for item in items]
    cached = lookup_shared_vectors(root, requests)
    references, evidence, required = {}, {}, {}
    for item, request in zip(items, requests):
        key = request_key(request)
        found = cached[key]
        reference = {"request_key": key, "request_identity": request, "status": "missing"}
        if found:
            reference.update({"status": "cached", "vector_sha256": found["vector_sha256"],
                              "object_path": found["shared_object_path"], "shared_revision_id": found["shared_revision_id"]})
            for relative in (found["shared_object_path"], f"{CACHE_ROOT}/revisions/{found['shared_revision_id']}/manifest.json",
                             f"{CACHE_ROOT}/revisions/{found['shared_revision_id']}/receipt.json"):
                evidence[relative] = _file_binding(root, root / relative)
        elif item["id"] in candidate_ids:
            row = required.setdefault(key, {"request_key": key, "request_identity": request, "item_ids": [],
                "status": "requires_new_external_embedding_authorization", "execution_authorized": False})
            row["item_ids"].append(item["id"])
        references[item["id"]] = reference
    return references, [required[key] for key in sorted(required)], list(evidence.values())


def _render(manifest, baseline, review):
    esc = lambda value: html.escape(str(value), quote=True)
    all_items = {row["id"]: row for row in baseline["items"] + manifest["items"]}
    def card(ident):
        row = all_items[ident]
        path = row["review_image_path"]
        if not re.fullmatch(r"(?:inputs|\.\./\.\./[A-Za-z0-9_-]+/inputs)/[a-f0-9]{64}\.png", path):
            raise ValueError("review preview must be an explicitly prepared local image")
        archived = {row["id"] for row in baseline["archived_aliases"]}
        kind = ("미검토 신규" if ident in set(manifest["incoming_ids"]) else
                "기존 보관 별칭 · 복원 안 함" if ident in archived else "기존 확정 유지 · 변경 불가")
        rights = row.get("rights_display", {})
        source_name = rights.get("source_name") or row.get("source_name") or "출처 미확인"
        badge = rights.get("badge") or "권리 미확인"
        notice = rights.get("notice_text") or "개별 이미지 이용 허락 미확인. 출처 표기는 이용 허락을 대신하지 않습니다."
        return (f'<figure><img loading="lazy" src="{esc(path)}" alt="{esc(row["style_id"])}"><figcaption>{esc(row["style_id"])} · {esc(kind)}'
                f'<br>{esc(badge)} · 출처: {esc(source_name)}<details><summary>권리 안내</summary>{esc(notice)}</details></figcaption></figure>')
    sections = []
    for group in review["exact_alias_proposals"] + review["anchor_conflicts"] + review["human_groups"]:
        title = {"deterministic_exact_alias_proposal": "컴퓨터 완전 중복 · 별칭 제안", "exact_bridge_between_committed_anchors": "기존 대표 충돌 · 사람 판단 필요",
                 "same_pixels_different_prompt": "같은 픽셀·다른 프롬프트 · 자동 삭제 안 함", "prompt_exact_group_only": "같은 프롬프트 · 이미지 그룹 검토"}[group["kind"]]
        members = list(group["member_ids"])
        if group.get("representative_id") and group["representative_id"] not in members:
            members.insert(0, group["representative_id"])
        sections.append(f'<section><h2>{title}</h2><p>읽기 전용 제안입니다. 기존 승인·그룹·원본은 바뀌지 않습니다.</p><div class="grid">'+"".join(card(i) for i in members)+"</div></section>")
    novel = "".join(card(ident) for ident in review["retained_new_candidate_ids"])
    return f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' file:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>남은 CASE · 오프라인 최종 검토 준비</title><style>body{{font:16px/1.6 system-ui;margin:0;background:#f3f3ee;color:#192d2b}}main{{max-width:1400px;margin:auto;padding:24px}}section{{background:white;padding:18px;margin:24px 0;border:1px solid #bccac4}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}}figure{{margin:0}}img{{width:100%;height:250px;object-fit:contain}}figcaption{{overflow-wrap:anywhere}}</style>
<main><h1>남은 CASE {len(manifest['items'])}개 · 오프라인 검토 준비</h1><p>현재 확정본: 승인 {len(baseline['approved_ids'])}개 / 유지 {len(baseline['retained_ids'])}개 / 보관 별칭 {len(baseline['archived_aliases'])}개. 아래 신규 항목은 승인되지 않았습니다.</p>
<p>파일 완전 일치 또는 전체 픽셀+비어 있지 않은 원문 프롬프트가 함께 일치할 때만 중복 별칭을 제안합니다. 같은 프롬프트만으로 삭제하지 않습니다. 출처 표기는 이용 허락을 대신하지 않습니다. 원본 삭제·API 호출·신규 그룹 승인은 0건입니다.</p>
{''.join(sections)}<section><h2>중복 제외 후 남은 신규 후보 · 별도 사람 승인 필요</h2><p>아래 목록은 유사도가 낮다는 판단이 아닙니다. 캐시가 없는 이미지의 의미 기반 유사도는 아직 계산하지 않았습니다.</p><div class="grid">{novel}</div></section></main></html>'''


def _build_payloads(root, db_path, reference_run_id, run_id, progress=None):
    root = dataset._normalized_root(Path(root))
    if run_id == reference_run_id:
        raise ValueError("remaining review must use a new run ID")
    run_path(root, run_id)
    committed = _committed(db_path, reference_run_id)
    if not committed.get("commit"):
        raise ValueError("a completed committed baseline is required")
    spec = _load_spec(root, reference_run_id)
    normalized = _validate_commit(spec, committed)
    sources, files = _sources(root, spec)
    existing = [copy.deepcopy(sources[row["id"]]) for row in spec["items"]]
    archived = _archive(normalized, spec)
    connection = dataset._connect_index(dataset._duplicate_index_path(root))
    try:
        pool = dataset._all_asset_candidates(connection)
        public_ids = _public_case_ids(root)
        selected, inventory = select_case_candidates(pool, public_ids, existing, MAX_IMAGES)
    finally:
        connection.close()
    if inventory["remaining_unsampled_case_records"]:
        raise ValueError("remainder exceeds the explicit 300-image offline bound")
    if not selected:
        raise ValueError("no unreviewed primary CASE records remain")
    raw_items = _resolve_additional_items(root, selected, len(selected))
    files.extend(_file_binding(root, path) for path in (root / PUBLIC_CATALOG_PATH, dataset._canonical_path(root),
        dataset._duplicate_index_path(root), dataset._remote_overlay_path(root)))
    if progress:
        progress({"stage": "inventory_resolved", "remaining": len(raw_items), "baseline": len(existing)})
    # Old pixel fingerprints are recomputed, not inferred from prepared images.
    for row in existing:
        original = safe_source(root, row["path"])
        if (digest(original.read_bytes()) != row["sha256"]
                or _pixel_sha256(_decoded_rgba_image(original)) != row["signals"]["pixel_sha256"]):
            raise ValueError("committed original fingerprint changed")
        row["review_image_path"] = f"../../{reference_run_id}/inputs/{row['prepared_sha256']}.png"
    incoming, blobs = [], {}
    for index, raw in enumerate(raw_items, 1):
        item, blob = _prepare_item(root, raw)
        item["review_image_path"] = item["prepared_path"]
        incoming.append(item)
        if item["prepared_path"] in blobs and blobs[item["prepared_path"]] != blob:
            raise ValueError("conflicting prepared image content")
        blobs[item["prepared_path"]] = blob
        files.append(_file_binding(root, safe_source(root, item["path"])))
        if progress and index % 25 == 0:
            progress({"stage": "remaining_prepared_offline", "images": index})
    review = propose_exact_relations(incoming, existing, normalized["stage2_overlay"]["active_ids"], archived)
    refs, missing, vector_files = _vector_inventory(root, incoming, set(review["retained_new_candidate_ids"]))
    files.extend(vector_files)
    for row in incoming:
        row["image_vector"] = refs[row["id"]]
        row["human_review_status"] = "unreviewed"
    baseline = {"schema_version": SCHEMA, "reference_run_id": reference_run_id, "source_commit": committed["commit"],
        "spec_sha256": spec["spec_sha256"], "items": existing, "retained_ids": normalized["stage2_overlay"]["active_ids"],
        "approved_ids": [row["id"] for row in normalized["private_front_export_items"]],
        "archived_aliases": archived, "groups": normalized["approved_similarity_groups"],
        "normalized_decisions": normalized, "read_only": True, "release_eligible": False}
    inventory["scope"] = "one_primary_local_asset_per_public_CASE_record"
    inventory["indexed_primary_CASE_assets"] = sum(row.get("lane") == "legacy" and row.get("style_id") in public_ids for row in pool)
    manifest = {"schema_version": SCHEMA, "run_id": run_id, "reference_run_id": reference_run_id,
        "source_commit": committed["commit"], "selection": inventory, "items": incoming,
        "incoming_ids": [row["id"] for row in incoming], "baseline_sha256": digest(json_bytes(baseline)),
        "human_review_status": "pending", "external_ai_execution_authorized": False, "release_eligible": False}
    unique_files = {}
    for row in files:
        if row["path"] in unique_files and unique_files[row["path"]] != row:
            raise ValueError("source evidence changed during remaining preparation")
        unique_files[row["path"]] = row
    bindings = {"schema_version": SCHEMA, "reference_run_id": reference_run_id, "source_commit": committed["commit"],
                "files": [unique_files[key] for key in sorted(unique_files)]}
    vector_plan = {"schema_version": SCHEMA, "requests": missing, "provider_calls": 0,
                   "execution_authorized": False, "status": "blocked_pending_explicit_external_execution_approval",
                   "cached_remaining_ids": [ident for ident, value in refs.items() if value["status"] == "cached"],
                   "semantic_comparison_status": "not_executed_no_new_vectors"}
    html_bytes = _render(manifest, baseline, review).encode("utf-8")
    payloads = {"manifest.json": json_bytes(manifest), "baseline.json": json_bytes(baseline),
                "exact-review.json": json_bytes(review), "embedding-pending.json": json_bytes(vector_plan),
                "source-bindings.json": json_bytes(bindings), "review.html": html_bytes, **blobs}
    summary = {"schema_version": SCHEMA, "run_id": run_id, "reference_run_id": reference_run_id,
        "source_commit_id": committed["commit"]["id"], "source_commit_revision": committed["commit"]["revision"],
        "existing_reviewed": len(existing), "existing_retained": len(baseline["retained_ids"]),
        "existing_archived": len(archived), "existing_approved": len(baseline["approved_ids"]), "existing_groups": len(baseline["groups"]),
        "remaining_primary_images": len(incoming), "local_images_available": len(incoming),
        "exact_alias_proposed_images": sum(len(row["proposed_alias_ids"]) for row in review["exact_alias_proposals"]),
        "exact_alias_proposal_groups": len(review["exact_alias_proposals"]), "anchor_conflict_groups": len(review["anchor_conflicts"]),
        "human_comparison_groups": len(review["human_groups"]), "new_retained_candidates": len(review["retained_new_candidate_ids"]),
        "cached_remaining_images": len(vector_plan["cached_remaining_ids"]), "new_embedding_content_keys": len(missing),
        "new_embedding_image_ids": sum(len(row["item_ids"]) for row in missing),
        "old_new_exact_comparisons": review["old_new_exact_comparisons"], "new_new_exact_comparisons": review["new_new_exact_comparisons"],
        "comparison_method": review["comparison_method"],
        "provider_calls": 0, "physical_deletions": 0, "human_approvals_created": 0, "canonical_writes": 0, "release_eligible": False}
    _require_latest(db_path, reference_run_id, committed["commit"]["id"])
    return payloads, bindings, summary


def _verify_sources(root, bindings):
    for row in bindings["files"]:
        if _file_binding(root, root / row["path"]) != row:
            raise ValueError("remaining review source evidence changed")


def prepare_remaining_case_review(root: Path, db_path: Path, reference_run_id: str, run_id: str, *,
                                 apply: bool = False, expected_commit_id: str | None = None, progress=None) -> dict:
    """Dry-run computes only; apply appends one immutable private review package."""
    root = Path(root).resolve()
    payloads, bindings, summary = _build_payloads(root, db_path, reference_run_id, run_id, progress)
    if expected_commit_id is not None and summary["source_commit_id"] != expected_commit_id:
        raise ValueError("latest approval differs from expected commit")
    destination = (run_path(root, run_id) / DIRECTORY).resolve()
    if not destination.is_relative_to(root):
        raise ValueError("remaining review destination escapes archive")
    receipt = {"schema_version": SCHEMA, "complete": True, "summary": summary,
               "files": {name: digest(content) for name, content in sorted(payloads.items())}}
    payloads["receipt.json"] = json_bytes(receipt)
    if not apply:
        return {**summary, "status": "dry_run", "package_path": str(destination), "writes": 0}
    _verify_sources(root, bindings)
    _require_latest(db_path, reference_run_id, summary["source_commit_id"])
    if destination.exists():
        for relative, content in payloads.items():
            path = (destination / relative).resolve()
            if not path.is_relative_to(destination) or not path.is_file() or path.read_bytes() != content:
                raise ValueError("refusing to overwrite changed immutable remaining review")
        return {**summary, "status": "unchanged", "package_path": str(destination), "writes": 0}
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".remaining-review-", dir=destination.parent))
    # A failed publication leaves its bounded staging directory for diagnosis;
    # no source or user-owned paths are removed during error cleanup.
    for relative, content in payloads.items():
        path = temporary / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    _verify_sources(root, bindings)
    _require_latest(db_path, reference_run_id, summary["source_commit_id"])
    temporary.rename(destination)
    return {**summary, "status": "prepared_pending_human_review", "package_path": str(destination), "writes": len(payloads)}


__all__ = ["prepare_remaining_case_review", "propose_exact_relations"]
