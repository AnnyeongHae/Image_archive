"""Authenticated local intake -> frozen, sequential human review.

No network, inference, source mutation, approval seed, or publication. Reuse
verified image-only cache entries; an absent vector is not an empty review.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import importlib.util
import json
import math
from pathlib import Path
import re

from .experiment import digest, json_bytes, run_path

SCHEMA = "image-v2-intake-review-1"
MANIFEST_SCHEMA = "image-v2-intake-manifest-1"
MODEL = "voyage-multimodal-3.5"
DIMENSIONS = 1024
HEX = re.compile(r"[a-f0-9]{64}\Z")
CODE_ROOT = Path(__file__).resolve().parents[2]


def _module(name):
    spec = importlib.util.spec_from_file_location("review_bridge_" + name,
        CODE_ROOT / "platform/v2/local" / (name + ".py"))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def _path(root, relative, *, exists=True):
    if (not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative
            or Path(relative).is_absolute() or any(p in {".", ".."} for p in relative.split("/"))):
        raise ValueError("unsafe_review_evidence_path")
    if not relative.startswith("data/private-research/"):
        raise ValueError("private_review_evidence_required")
    current = Path(root).resolve(strict=True)
    for part in relative.split("/"):
        if part.startswith(".") or "secret" in part.lower():
            raise ValueError("secret_or_hidden_review_path")
        current /= part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError("review_link_forbidden")
    result = current.resolve(strict=exists)
    if not result.is_relative_to(Path(root).resolve() / "data/private-research"):
        raise ValueError("review_path_escape")
    return result


def _raw(root, relative, maximum=32 * 1024**2):
    path = _path(root, relative)
    if not path.is_file() or path.stat().st_size > maximum:
        raise ValueError("review_evidence_size")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("review_evidence_size")
    return raw


def _evidence_hash(root, relative, *, declared_media=False):
    """Read-only original evidence; never an output or HTTP media allowlist."""
    exact = {"data/canonical/archive_records.jsonl", "deploy/cloudflare-public/public/catalog-data.js"}
    if not isinstance(relative, str):
        raise ValueError("invalid_readonly_evidence_path")
    if relative.startswith("data/private-research/"):
        return digest(_raw(root, relative, 96 * 1024**2))
    from .intake_media import _safe_media_path, RASTER_SUFFIXES
    if declared_media or Path(relative).suffix.lower() in RASTER_SUFFIXES:
        path, _ = _safe_media_path(Path(root).resolve(), relative)
        import hashlib
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    if relative not in exact:
        raise ValueError("readonly_evidence_not_allowlisted")
    if (Path(relative).is_absolute() or ":" in relative or "\\" in relative
            or any(not part or part.startswith(".") or "secret" in part.lower() for part in relative.split("/"))):
        raise ValueError("unsafe_readonly_evidence_path")
    current = Path(root).resolve(strict=True)
    for part in relative.split("/"):
        current /= part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError("readonly_evidence_link_forbidden")
    if not current.resolve(strict=True).is_relative_to(Path(root).resolve()) or not current.is_file():
        raise ValueError("readonly_evidence_escape")
    # Canonical JSONL can exceed the bounded private packet size. Stream its
    # digest instead of duplicating the complete 19k-record corpus in memory.
    import hashlib
    with current.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _json(raw):
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError("duplicate_review_json_key")
            result[key] = value
        return result
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_review_json")))


def _hash(value):
    return digest(json_bytes(value))


def load_import(root, relative, expected_sha):
    """Recheck the locally authenticated import, not an arbitrary origin flag.

    The caller pins the import receipt SHA. This verifies stored evidence,
    not current remote run state or an independent cryptographic sender claim.
    """
    if not HEX.fullmatch(str(expected_sha)):
        raise ValueError("exact_import_receipt_sha_required")
    raw = _raw(root, relative, 131072)
    if digest(raw) != expected_sha:
        raise ValueError("import_receipt_changed")
    receipt = _json(raw)
    actions = _module("actions_import")
    if (receipt.get("schema_version") != "archive-actions-import-receipt-1"
            or receipt.get("status") != "imported_private_not_approved"
            or receipt.get("repository") != actions.REPOSITORY or receipt.get("workflow_path") != actions.WORKFLOW
            or receipt.get("origin_verified") is not True
            or any(receipt.get(k) is not False for k in ("image_approved", "metadata_human_approved", "public_release"))
            or any(type(receipt.get(k)) is not int or receipt[k] != 0 for k in ("provider_calls", "media_downloads"))):
        raise ValueError("authenticated_private_intake_receipt_required")
    parent = Path(relative).parent.as_posix()
    expected = {key: parent + "/" + name for key, name in {
        "zip": "artifact.zip", "sealed": "intake.sealed.json", "lineage": "lineage.json",
        "bundle": "bundle.json", "plan": "intake-plan.json"}.items()}
    if receipt.get("files") != expected or not parent.startswith(actions.IMPORT_ROOT + "/"):
        raise ValueError("import_file_set_mismatch")
    blobs = {key: _raw(root, path, 96 * 1024**2) for key, path in expected.items()}
    checks = {"zip": "zip_sha256_calculated", "sealed": "sealed_sha256_calculated",
              "lineage": "lineage_sha256", "bundle": "bundle_sha256_verified", "plan": "plan_sha256"}
    if any(digest(blobs[key]) != receipt.get(field) for key, field in checks.items()):
        raise ValueError("import_evidence_changed")
    lineage, plan = _json(blobs["lineage"]), _json(blobs["plan"])
    for key in ("repository", "workflow_path", "run_id", "run_attempt", "artifact_id", "head_sha"):
        if lineage.get(key) != receipt.get(key):
            raise ValueError("import_lineage_conflict")
    actions.validate_run(lineage["run"], receipt["run_id"], attempt=receipt["run_attempt"],
                         expected_head_sha=receipt["head_sha"])
    if (lineage.get("origin_verified") is not True or plan.get("origin_verified") is not True
            or plan.get("origin_receipt") != expected["lineage"]
            or plan.get("origin_receipt_sha256") != digest(blobs["lineage"])):
        raise ValueError("import_origin_binding_mismatch")
    sealed_raw, sealed = actions.extract_sealed(blobs["zip"])
    if (sealed_raw != blobs["sealed"] or sealed["plaintext_sha256"] != digest(blobs["bundle"])
            or sealed["plaintext_bytes"] != len(blobs["bundle"])):
        raise ValueError("import_transport_binding_mismatch")
    bundle = actions.validate_bundle(_json(blobs["bundle"]))
    rebuilt = _module("intake").build_plan(bundle)
    for key in rebuilt.keys() - {"origin_verified", "origin_note"}:
        if plan.get(key) != rebuilt[key]:
            raise ValueError("import_plan_does_not_match_bundle")
    return bundle, [{"path": relative, "sha256": expected_sha}] + [
        {"path": expected[key], "sha256": digest(value)} for key, value in blobs.items()]


def request_for(item):
    return {"provider": "voyage", "model": MODEL, "dimensions": DIMENSIONS,
            "image_sha256": item["prepared_sha256"], "text": "", "task": "RETRIEVAL_DOCUMENT"}


def cached_vectors(root, items):
    from .comparison import request_key
    from .shared_vector_cache import lookup_shared_vectors, RELATIVE_ROOT
    requests = [request_for(item) for item in items]
    found = lookup_shared_vectors(root, requests)
    vectors, proof, missing, evidence = {}, [], [], {}
    for item, request in zip(items, requests):
        key = request_key(request)
        row = found.get(key)
        if row is None:
            missing.append({"id": item["id"], "request_key": key, "request_identity": request})
            continue
        values = row.get("vector")
        if (row.get("key") != key or row.get("provider") != "voyage" or row.get("model") != MODEL
                or not isinstance(values, list) or len(values) != DIMENSIONS
                or any(type(v) not in {int, float} or not math.isfinite(v) for v in values)
                or not math.isfinite(sum(v * v for v in values)) or sum(v * v for v in values) <= 0
                or row.get("vector_sha256") != _hash(values)
                or not HEX.fullmatch(str(row.get("shared_revision_id")))):
            raise ValueError("invalid_retained_image_vector")
        vector_path = f"{RELATIVE_ROOT}/objects/{key[:2]}/{key}.json"
        if row.get("shared_object_path") != vector_path:
            raise ValueError("invalid_cached_vector_path")
        paths = [vector_path, f"{RELATIVE_ROOT}/revisions/{row['shared_revision_id']}/manifest.json",
                 f"{RELATIVE_ROOT}/revisions/{row['shared_revision_id']}/receipt.json"]
        for relative in paths:
            if relative not in evidence:
                evidence[relative] = {"path": relative, "sha256": digest(_raw(root, relative))}
        vectors[item["id"]] = values
        proof.append({"id": item["id"], "request_key": key, "request_identity": request,
                      "vector_sha256": row["vector_sha256"], "object_path": vector_path,
                      "revision_id": row["shared_revision_id"]})
    return vectors, proof, missing, list(evidence.values())


def _exact_incoming(old_sources, old_active, incoming, archived=()):
    """Preserve human keepers. New JSON prompts stay in alias provenance.

    Legacy full-pixel hashes use another prefix/policy, not rgba-exif-v2.
    File hashes always work; pixel+prompt works only with an explicit identical
    full-pixel policy. The legacy coverage limit is recorded in the manifest.
    """
    intake = _module("intake")
    routes = {row["id"]: row["representative_id"] for row in archived}
    def final_keeper(ident):
        seen = set()
        while ident in routes:
            if ident in seen:
                raise ValueError("baseline_alias_cycle")
            seen.add(ident)
            ident = routes[ident]
        if ident not in old_active:
            raise ValueError("baseline_alias_does_not_resolve_to_retained")
        return ident
    old_rows = []
    for ident in list(old_active) + sorted(routes):
        source = old_sources[ident]
        prompt = source.get("prompt", "")
        signals = source.get("signals", {})
        old_rows.append({"item_id": ident, "keeper_id": final_keeper(ident), "file_sha256": source["sha256"],
            "pixel_sha256": signals.get("pixel_sha256") if signals.get("pixel_policy") == "rgba-exif-v2" else None,
            "pixel_policy": signals.get("pixel_policy"), "prompt_nonblank": bool(prompt.strip()),
            "prompt_sha256": digest(prompt.encode()), "original_prompt": prompt})
    rows = [{"item_id": item["id"], "original_prompt": item["prompt"],
             "file_sha256": item["sha256"], **item["signals"], "ingested_order": index,
             "prompt_nonblank": bool(item["prompt"].strip()), "prompt_sha256": digest(item["prompt"].encode())}
            for index, item in enumerate(incoming)]
    old_indexes = ({}, {})
    for old in old_rows:
        old_indexes[0].setdefault(old["file_sha256"], []).append(old)
        if old.get("pixel_sha256") and old["prompt_nonblank"]:
            old_indexes[1].setdefault((old["pixel_sha256"], old["prompt_sha256"]), []).append(old)
    # Probe every new member first, not merely its preferred new representative;
    # exact-file and pixel+prompt edges can form transitive identity components.
    plan = intake.dedupe_plan(rows)
    components = {group["representative_id"]: group["member_ids"] for group in plan["exact_groups"]}
    lookup = {row["item_id"]: row for row in rows}
    aliases, fresh = [], []
    for chosen in plan["active_ids"]:
        members = components.get(chosen, [chosen])
        matches = []
        for member in members:
            row = lookup[member]
            candidates = {old["item_id"]: old for old in old_indexes[0].get(row["file_sha256"], [])}
            if row.get("pixel_sha256") and row["prompt_nonblank"]:
                candidates.update({old["item_id"]: old for old in old_indexes[1].get(
                    (row["pixel_sha256"], row["prompt_sha256"]), [])})
            matches.extend((old["keeper_id"], member, reason, old["item_id"]) for old in candidates.values()
                           if (reason := intake.exact_reason(old, row)))
        anchors = sorted({row[0] for row in matches})
        if len(anchors) > 1:
            # Do not silently choose between conflicting previous human keepers.
            # Leave this component as new for explicit human identity review.
            anchors = []
        representative = anchors[0] if anchors else chosen
        if not anchors:
            fresh.append(chosen)
        evidence = [{"left_id": member, "right_id": matched, "representative_id": keeper, "reason": reason}
                    for keeper, member, reason, matched in matches]
        evidence += [row for row in plan["exact_evidence_edges"] if row["left"] in members and row["right"] in members]
        for ident in members:
            if ident != representative:
                aliases.append({"id": ident, "representative_id": representative,
                                "evidence": evidence, "physical_delete": False})
    return fresh, aliases


def build_intake_review(root, *, import_receipt, import_receipt_sha256, media_bindings,
                        baseline_run_id, db_path, review_run_id, apply=False):
    from .approval_handoff import _committed, _validate_commit, _sources
    from .incremental_workflow import load_frozen_workflow, assemble_spec
    from .intake_media import prepare_assets
    root = Path(root).resolve(strict=True)
    destination = run_path(root, review_run_id)
    if baseline_run_id == review_run_id:
        raise ValueError("new_review_run_required")
    bundle, evidence = load_import(root, import_receipt, import_receipt_sha256)
    binding_raw = _raw(root, media_bindings, 2 * 1024**2)
    prepared = prepare_assets(root, bundle, _json(binding_raw))
    incoming = prepared["items"]
    reference = load_frozen_workflow(root, baseline_run_id)
    if len(reference["items"]) > 4000:
        raise ValueError("baseline_review_limit")
    committed = _committed(Path(db_path).resolve(), baseline_run_id)
    baseline = _validate_commit(reference, committed)
    sources, baseline_evidence = _sources(root, reference)
    if set(sources) & {row["id"] for row in incoming}:
        raise ValueError("selected_media_already_in_baseline_use_new_items_only")
    old_active = baseline["stage2_overlay"]["active_ids"]
    fresh, aliases = _exact_incoming(sources, old_active, incoming, baseline["stage2_overlay"]["archived"])
    items_by_id = {row["id"]: row for row in reference["items"] + incoming}
    retained = [items_by_id[ident] for ident in old_active + fresh]
    vectors, proof, missing, vector_evidence = cached_vectors(root, retained)
    result = {"schema_version": SCHEMA, "status": "blocked_missing_image_vectors" if missing else "dry_run",
        "run_id": review_run_id, "baseline_run_id": baseline_run_id, "baseline_commit_id": committed["commit"]["id"],
        "incoming_images": len(incoming), "machine_aliases": len(aliases), "new_retained": len(fresh),
        "baseline_retained": len(old_active), "cached_retained_vectors": len(vectors),
        "missing_image_vectors": missing, "selection": prepared["selection"],
        "provider_calls": 0, "media_downloads": 0, "writes": 0, "image_approved": False,
        "release_eligible": False, "public_rights_approved": False}
    if missing:
        # No serveable spec or implicit empty candidate queue is produced.
        return result
    base = destination.relative_to(root).as_posix()
    workflow = base + "/group-workflow-v1"
    creation_path = workflow + "/creation.json"
    if _path(root, creation_path, exists=False).exists():
        creation = _json(_raw(root, creation_path, 4096))
        if creation.get("run_id") != review_run_id or creation.get("schema_version") != SCHEMA:
            raise ValueError("review_creation_record_conflict")
        stamp = datetime.fromisoformat(creation["created_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("review_creation_timezone_required")
    else:
        creation = {"schema_version": SCHEMA, "run_id": review_run_id,
                    "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    created_at = creation["created_at"]
    manifest = {"schema_version": MANIFEST_SCHEMA, "run_id": review_run_id,
        "items": incoming, "embedding_item_ids": fresh, "alias_routes": aliases,
        "intake_receipt": import_receipt, "intake_receipt_sha256": import_receipt_sha256,
        "selection": prepared["selection"], "provider_calls": 0, "physical_deletions": 0,
        "exact_policy": "exact_file_OR_full_rgba_pixels_AND_nonblank_prompt",
        "baseline_full_pixel_evidence_count": sum(sources[i].get("signals", {}).get("pixel_policy") == "rgba-exif-v2" for i in old_active),
        "baseline_pixel_note": "Legacy full-pixel hashes use another unversioned prefix; do not compare to rgba-exif-v2 directly.",
        "release_eligible": False, "image_approved": False}
    fingerprint = _hash({"manifest": manifest, "commit": committed["commit"], "reference": reference["spec_sha256"]})
    spec = assemble_spec(reference, baseline, manifest, vectors, review_run_id=review_run_id,
                         created_at=created_at, source_fingerprint=fingerprint)
    spec["intake_review_schema"] = SCHEMA
    spec["image_vector_gate"] = {"status": "complete_cached", "model": MODEL, "dimensions": DIMENSIONS,
                                  "retained_ids": old_active + fresh, "provider_calls": 0}
    spec["spec_sha256"] = _hash({key: value for key, value in spec.items() if key != "spec_sha256"})
    outputs = {creation_path: json_bytes(creation), base + "/manifest.json": json_bytes(manifest),
        workflow + "/submitted-baseline.raw.json": json_bytes(committed["normalized"]),
        workflow + "/baseline-commit.json": json_bytes(committed),
        workflow + "/image-vectors.json": json_bytes(vectors),
        workflow + "/vector-proof.json": json_bytes(proof)}
    # Copy only frozen preview bytes; no original file is moved or overwritten.
    previews = dict(prepared["previews"])
    for item in reference["items"]:
        raw = _raw(root, sources[item["id"]]["frozen_preview_path"], 15 * 1024**2)
        if digest(raw) != item["prepared_sha256"]:
            raise ValueError("baseline_preview_changed")
        previews[item["prepared_sha256"]] = raw
    for sha, raw in previews.items():
        outputs[base + "/inputs/" + sha + ".png"] = raw
    evidence += baseline_evidence + vector_evidence + [{"path": media_bindings, "sha256": digest(binding_raw)}]
    evidence += [{"path": item["path"], "sha256": item["sha256"]} for item in incoming]
    # Baseline input paths may include canonical JSON outside private research.
    # _sources already binds them; don't copy canonical data to the new run.
    files = {}
    declared_media = {item["path"] for item in incoming}
    for row in evidence:
        if _evidence_hash(root, row["path"], declared_media=row["path"] in declared_media) != row["sha256"]:
            raise ValueError("source_changed_during_review_preparation")
        if row["path"] in files and files[row["path"]] != row:
            raise ValueError("conflicting_frozen_evidence_hashes")
        files[row["path"]] = row
    for path, raw in outputs.items():
        files[path] = {"path": path, "sha256": digest(raw)}
    binding = {"schema_version": SCHEMA, "review_spec_sha256": spec["spec_sha256"],
        "source_decisions_sha256": digest(outputs[workflow + "/submitted-baseline.raw.json"]),
        "baseline_commit_id": committed["commit"]["id"], "files": sorted(files.values(), key=lambda r: r["path"]),
        "provider_calls": 0, "origin_scope": "hash_bound_previously_authenticated_local_Actions_import"}
    receipt = {**result, "status": "ready", "spec_sha256": spec["spec_sha256"],
        "binding_sha256": _hash(binding), "vector_fingerprint": spec["vector_fingerprint"],
        "duplicate_candidates": len(spec["duplicate_candidates"]), "similarity_candidates": len(spec["similarity_candidates"])}
    outputs[workflow + "/source-bindings.json"] = json_bytes(binding)
    outputs[workflow + "/image-group-workflow.spec.json"] = json_bytes(spec)
    outputs[workflow + "/build-receipt.json"] = json_bytes(receipt)  # completion marker last
    if (len(outputs[base + "/manifest.json"]) > 32 * 1024**2
            or any(len(raw) > 96 * 1024**2 for raw in outputs.values())):
        raise ValueError("frozen_review_too_large_select_smaller_batch")
    result.update({"spec_sha256": spec["spec_sha256"], "duplicate_candidates": receipt["duplicate_candidates"],
                   "similarity_candidates": receipt["similarity_candidates"]})
    if apply:
        # Confirm no newer human commit before freezing. A later baseline edit
        # remains separate; this run records exactly the consumed human commit.
        if _committed(Path(db_path).resolve(), baseline_run_id)["commit"] != committed["commit"]:
            raise ValueError("baseline_commit_changed_during_build")
        writer = _module("actions_import").immutable_write
        # Precheck all existing outputs so conflicting run IDs cannot partially
        # overwrite an earlier review. Interrupted exact writes remain resumable.
        for relative, raw in outputs.items():
            path = _path(root, relative, exists=False)
            if path.exists() and _raw(root, relative, 96 * 1024**2) != raw:
                raise ValueError("immutable_intake_review_conflict")
        created_count = 0
        for relative, raw in outputs.items():
            existed = _path(root, relative, exists=False).exists()
            writer(root, relative, raw)
            created_count += not existed
        load_intake_review(root, review_run_id)
        result.update(status="ready_for_human_review", writes=created_count, artifact_files=len(outputs))
    return result


def load_intake_review(root, review_run_id):
    """Strict new-contract loader used at startup, mutations and handoff."""
    from .comparison import request_key
    root = Path(root).resolve(strict=True)
    base = run_path(root, review_run_id).relative_to(root).as_posix()
    workflow = base + "/group-workflow-v1"
    spec = _json(_raw(root, workflow + "/image-group-workflow.spec.json"))
    binding = _json(_raw(root, workflow + "/source-bindings.json"))
    receipt = _json(_raw(root, workflow + "/build-receipt.json"))
    if (spec.get("schema_version") != "image-group-workflow-spec-1" or spec.get("intake_review_schema") != SCHEMA
            or spec.get("run_id") != review_run_id or receipt.get("schema_version") != SCHEMA
            or receipt.get("status") != "ready" or receipt.get("run_id") != review_run_id
            or spec.get("spec_sha256") != _hash({k: v for k, v in spec.items() if k != "spec_sha256"})
            or receipt.get("spec_sha256") != spec["spec_sha256"] or binding.get("review_spec_sha256") != spec["spec_sha256"]
            or receipt.get("binding_sha256") != _hash(binding)
            or spec.get("release_eligible") is not False or spec.get("public_rights_approved") is not False):
        raise ValueError("v2_frozen_review_identity_mismatch")
    manifest_path = base + "/manifest.json"
    manifest_raw = _raw(root, manifest_path)
    manifest = _json(manifest_raw)
    if manifest.get("schema_version") != MANIFEST_SCHEMA or manifest.get("run_id") != review_run_id:
        raise ValueError("invalid_v2_intake_manifest")
    declared_media = {item["path"] for item in manifest["items"]}
    files = {}
    for row in binding["files"]:
        relative = row.get("path")
        if relative in files or not HEX.fullmatch(str(row.get("sha256"))):
            raise ValueError("invalid_review_file_binding")
        actual = _evidence_hash(root, relative, declared_media=relative in declared_media)
        if actual != row["sha256"]:
            raise ValueError("v2_frozen_review_evidence_changed")
        files[relative] = row["sha256"]
    required = [base + "/manifest.json", workflow + "/submitted-baseline.raw.json",
                workflow + "/baseline-commit.json", workflow + "/image-vectors.json", workflow + "/vector-proof.json",
                workflow + "/creation.json"]
    if not set(required) <= set(files):
        raise ValueError("missing_v2_review_binding")
    if binding.get("source_decisions_sha256") != files[workflow + "/submitted-baseline.raw.json"]:
        raise ValueError("baseline_decisions_binding_mismatch")
    if (_json(_raw(root, workflow + "/creation.json", 4096)).get("created_at") != spec.get("created_at")
            or any(files.get(item["path"]) != item["sha256"] for item in manifest["items"])):
        raise ValueError("original_media_or_creation_not_bound")
    vectors = _json(_raw(root, workflow + "/image-vectors.json", 96 * 1024**2))
    proof = _json(_raw(root, workflow + "/vector-proof.json"))
    ids = spec["stage1"]["active_ids"]
    gate = spec.get("image_vector_gate", {})
    if (gate != {"status": "complete_cached", "model": MODEL, "dimensions": DIMENSIONS,
                 "retained_ids": ids, "provider_calls": 0}
            or set(vectors) != set(ids) or len(proof) != len(ids)
            or {row["id"] for row in proof} != set(ids) or _hash(vectors) != spec.get("vector_fingerprint")):
        raise ValueError("image_candidate_gate_incomplete")
    items = {item["id"]: item for item in spec["items"]}
    for item in spec["items"]:
        preview = base + "/inputs/" + item["prepared_sha256"] + ".png"
        if (item.get("prepared_path") != "../inputs/" + item["prepared_sha256"] + ".png"
                or files.get(preview) != item["prepared_sha256"]):
            raise ValueError("preview_not_bound_to_image")
    for row in proof:
        request = request_for(items[row["id"]])
        values = vectors[row["id"]]
        obj = _json(_raw(root, row["object_path"]))
        from .shared_vector_cache import PROTOCOL
        if (row["request_identity"] != request or row["request_key"] != request_key(request)
                or row["object_path"] not in files or row["vector_sha256"] != _hash(values)
                or obj.get("key") != row["request_key"] or obj.get("vector") != values
                or obj.get("request_identity") != {**request, "protocol": PROTOCOL}
                or len(values) != DIMENSIONS or any(type(v) not in {int, float} or not math.isfinite(v) for v in values)
                or not math.isfinite(sum(v * v for v in values)) or sum(v * v for v in values) <= 0):
            raise ValueError("cached_image_proof_mismatch")
    return spec
