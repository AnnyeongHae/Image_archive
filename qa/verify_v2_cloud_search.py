"""Read-only Neon/Qdrant search smoke; default is offline, with no credentials.

Only --verify opens the existing cloud connections. Never invokes embeddings,
creates credentials, changes roles, uploads media, or calls a Qdrant write API.
The immutable private receipt contains IDs/hashes/scores, never source prompts.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import uuid

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "platform/v2/local/cloud_snapshot.py"
SPEC = importlib.util.spec_from_file_location("v2_cloud_search_snapshot", SOURCE)
cloud = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cloud)
PLAN_HASH = "ae5910fb41af5c0e12d8c203bb203b90ebde3b249099da2ffb4edbe55724b183"
DEFAULT_PLAN = ROOT / "data/private-research/v2/cloud-plans" / PLAN_HASH
RECEIPTS = "data/private-research/v2/cloud-search-verification"
TOP = 5
QUERY_COUNT = 11
SCORE_TOLERANCE = 2e-5


class SearchVerificationError(ValueError):
    """Only static safe codes may be placed in this exception."""


def safe_code(exc):
    # Do not trust upstream exception strings, even from a familiar library.
    if isinstance(exc, SearchVerificationError):
        return str(exc)
    if isinstance(exc, cloud.SnapshotError):
        return "snapshot_validation_or_transport_failed"
    return "cloud_search_verification_failed"


def cosine(left, right):
    left, right = cloud.vector(left, 512), cloud.vector(right, 512)
    return sum(a*b for a, b in zip(left, right)) / math.sqrt(sum(a*a for a in left) * sum(b*b for b in right))


def validate_scope(plan):
    if len(plan["queries"]) != QUERY_COUNT:
        raise SearchVerificationError("exactly_eleven_cached_queries_required")
    if len({query["query_id"] for query in plan["queries"]}) != QUERY_COUNT:
        raise SearchVerificationError("duplicate_query_id")
    sid = plan["manifest"]["snapshot_id"]
    name = plan["manifest"]["qdrant_collections"]["text"]
    if name != f"image_archive_v2_{sid}_text512" or not re.fullmatch(r"[a-f0-9]{64}", sid):
        raise SearchVerificationError("fixed_text_collection_required")
    if len({point["payload"]["group_id"] for point in plan["text"]}) < TOP:
        raise SearchVerificationError("fewer_than_five_searchable_groups")
    for query in plan["queries"]:
        if query["snapshot_id"] != sid or query["model"] != "voyage-4-lite" or query["dimension"] != 512:
            raise SearchVerificationError("cached_query_contract_mismatch")
        cloud.vector(query["vector_json"], 512)


def query_body(query, snapshot_id):
    return {"query": cloud.vector(query["vector_json"], 512), "group_by": "group_id", "group_size": 1,
            "limit": TOP, "with_payload": sorted(cloud.PAYLOAD_KEYS), "with_vector": False,
            "filter": {"must": [{"key": "snapshot_id", "match": {"value": snapshot_id}},
                                 {"key": "image_approved", "match": {"value": True}}]},
            "params": {"exact": False, "hnsw_ef": 64}}


def validate_groups(result, query, plan):
    """Validate grouping, row hydration, and exhaustive local cosine agreement.

    A near-equal cutoff permits tied groups/members, not arbitrary ranking drift.
    This is a consistency smoke, not an independent relevance benchmark.
    """
    groups = result.get("groups") if isinstance(result, dict) else None
    if not isinstance(groups, list) or len(groups) != TOP:
        raise SearchVerificationError("expected_five_distinct_groups")
    items = {row["item_id"]: row for row in plan["items"]}
    points = {point["id"]: point for point in plan["text"]}
    group_scores, point_scores = {}, {}
    for ident, point in points.items():
        score = cosine(query["vector_json"], point["vector"])
        group_id = point["payload"]["group_id"]
        point_scores[ident] = score
        group_scores[group_id] = max(group_scores.get(group_id, -math.inf), score)
    expected = sorted(group_scores, key=lambda ident: (-group_scores[ident], ident))[:TOP]
    cutoff = group_scores[expected[-1]]
    required = {ident for ident, score in group_scores.items() if score > cutoff + SCORE_TOLERANCE}
    seen, rows, previous = set(), [], math.inf
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("id"), str) or group["id"] in seen:
            raise SearchVerificationError("invalid_or_duplicate_group_id")
        group_id = group["id"]
        hits = group.get("hits")
        if not isinstance(hits, list) or len(hits) != 1 or not isinstance(hits[0], dict):
            raise SearchVerificationError("exactly_one_group_hit_required")
        hit = hits[0]
        score, point_id, payload = hit.get("score"), hit.get("id"), hit.get("payload")
        if type(score) not in (float, int) or not math.isfinite(score) or not -1-SCORE_TOLERANCE <= score <= 1+SCORE_TOLERANCE:
            raise SearchVerificationError("invalid_finite_cosine_score")
        if score > previous + SCORE_TOLERANCE:
            raise SearchVerificationError("scores_not_descending")
        if not isinstance(point_id, str) or point_id not in points:
            raise SearchVerificationError("unknown_text_point_id")
        if not isinstance(payload, dict) or set(payload) != cloud.PAYLOAD_KEYS or payload != points[point_id]["payload"]:
            raise SearchVerificationError("point_payload_snapshot_or_approval_mismatch")
        if hit.get("vector") is not None or hit.get("vectors") is not None:
            raise SearchVerificationError("unexpected_returned_vectors")
        if payload["group_id"] != group_id or payload["snapshot_id"] != plan["manifest"]["snapshot_id"] or payload["image_approved"] is not True:
            raise SearchVerificationError("point_payload_snapshot_or_approval_mismatch")
        matched, representative = items.get(payload["item_id"]), items.get(payload["representative_id"])
        if (not matched or not representative or matched["snapshot_id"] != payload["snapshot_id"] or
            representative["snapshot_id"] != payload["snapshot_id"] or matched["text_ready"] is not True or
            matched["group_id"] != group_id or representative["group_id"] != group_id or
            matched["representative_id"] != representative["item_id"] or
            representative["representative_id"] != representative["item_id"]):
            raise SearchVerificationError("verified_neon_representative_mapping_mismatch")
        if (group_id not in group_scores or group_scores[group_id] < cutoff-SCORE_TOLERANCE or
            abs(score-point_scores[point_id]) > SCORE_TOLERANCE or
            group_scores[group_id]-point_scores[point_id] > SCORE_TOLERANCE):
            raise SearchVerificationError("local_cosine_group_baseline_mismatch")
        seen.add(group_id)
        previous = score
        rows.append({"group_id": group_id, "point_id": point_id, "matched_item_id": matched["item_id"],
                     "representative_id": representative["item_id"], "score": score,
                     "representative_mapping_verified": True})
    if not required.issubset(seen):
        raise SearchVerificationError("local_cosine_required_group_missing")
    return {"query_id": query["query_id"], "cached_vector_sha256": cloud.sha(cloud.encoded(query["vector_json"])),
            "status": "passed", "distinct_groups": len(seen), "expected_distinct_groups": TOP,
            "local_cosine_baseline_verified": True, "groups": rows}


def verify_search(plan, neon, qdrant, *, progress=None):
    validate_scope(plan)
    progress = progress if progress is not None else {}
    progress.update(qdrant_read_queries=0, queries=[], all_queries_passed=False)
    # libpq read-only is enabled before the first statement. Repeatable read
    # binds the snapshot/header checks and Neon.verify's complete row readback.
    neon.connection.readonly = True
    neon.connection.set_session(readonly=True, isolation_level="REPEATABLE READ", autocommit=False)
    with neon.connection.cursor() as cur:
        cur.execute("SHOW transaction_read_only")
        if cur.fetchone() != ("on",):
            raise SearchVerificationError("neon_readonly_transaction_not_confirmed")
        cur.execute("SELECT manifest_sha256,manifest_json,state FROM image_archive_v2.snapshots WHERE snapshot_id=%s",
                    (plan["manifest"]["snapshot_id"],))
        if cur.fetchone() != (plan["manifest_sha256"], plan["manifest"], "ready"):
            raise SearchVerificationError("neon_ready_snapshot_manifest_mismatch")
    # Exactly one full readback; equality proves the local rows/queries used
    # below are the verified remote rows, without issuing 55 hydration queries.
    neon.verify(plan)
    progress["neon"] = {"readonly_transaction_confirmed": True, "snapshot_state": "ready",
                        "manifest_verified": True, "full_row_verifications": 1,
                        "items_verified": len(plan["items"]), "cached_queries_verified": len(plan["queries"])}
    path = "/collections/" + plan["manifest"]["qdrant_collections"]["text"] + "/points/query/groups"
    for query in plan["queries"]:
        progress["qdrant_read_queries"] += 1
        result = qdrant.request("POST", path, query_body(query, plan["manifest"]["snapshot_id"]))
        progress["queries"].append(validate_groups(result, query, plan))
    progress["all_queries_passed"] = len(progress["queries"]) == QUERY_COUNT
    return progress


def write_receipt(receipt, root=ROOT):
    raw = cloud.encoded(receipt)
    directory = cloud.private_path(root / RECEIPTS / receipt["run_id"], root)
    directory.mkdir(parents=True, exist_ok=False, mode=0o777 if os.name == "nt" else 0o700)
    target = cloud.private_path(directory / "receipt.json", root)
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666 if os.name == "nt" else 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return target.relative_to(root).as_posix(), cloud.sha(raw)


def execute_verification(plan, *, root=ROOT):
    code_hashes = {"qa/verify_v2_cloud_search.py": cloud.sha(Path(__file__).read_bytes()),
                   "platform/v2/local/cloud_snapshot.py": cloud.sha(SOURCE.read_bytes())}
    receipt = {"schema_version": "archive-v2-cloud-search-verification-1",
               "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12],
               "started_at": datetime.now(timezone.utc).isoformat(), "plan_sha256": plan["manifest_sha256"],
               "snapshot_id": plan["manifest"]["snapshot_id"], "code_sha256": code_hashes,
               "plan_files": plan["manifest"]["files"], "status": "failed", "cloud_writes": 0,
               "new_embedding_calls": 0, "new_credentials": 0, "rights_changed": False,
               "public_release": False, "privacy": "owner_private",
               "permission_boundary": "inherited_workspace_not_hardened" if os.name == "nt" else "posix_0600",
               "quality_scope": "cached_source_queries_consistency_not_independent_relevance_evaluation"}
    neon = None
    try:
        validate_scope(plan)
        values = cloud.credentials(root / ".env")
        neon = cloud.Neon(values.get("DATABASE_URL") or values["NEON_DATABASE_KEY"], root / "db/v2/0001_private_library.sql")
        qdrant = cloud.Qdrant(values["QDRANT_ENDPOINT"], values["QDRANT_API_KEY"])
        # Populate the receipt incrementally so a failed fifth query does not
        # erase evidence of four completed read-only checks. Never retry here.
        verify_search(plan, neon, qdrant, progress=receipt)
        if code_hashes != {name: cloud.sha((ROOT / name).read_bytes()) for name in code_hashes}:
            raise SearchVerificationError("verification_code_changed_during_run")
        receipt["status"] = "passed"
    except Exception as exc:
        receipt["error_code"] = safe_code(exc)
    finally:
        if neon is not None:
            neon.connection.close()
    receipt["completed_at"] = datetime.now(timezone.utc).isoformat()
    path, digest = write_receipt(receipt, root)
    return {"status": receipt["status"], "receipt": path, "receipt_sha256": digest,
            "snapshot_id": receipt["snapshot_id"], "plan_sha256": receipt["plan_sha256"],
            "queries_passed": len(receipt.get("queries", [])), "qdrant_read_queries": receipt.get("qdrant_read_queries", 0),
            "items_verified": receipt.get("neon", {}).get("items_verified", 0),
            "new_embedding_calls": 0, "cloud_writes": 0,
            **({"error_code": receipt["error_code"]} if "error_code" in receipt else {})}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    try:
        path = args.plan if args.plan.is_absolute() else ROOT / args.plan
        plan = cloud.read_plan(path)
        if plan["manifest_sha256"] != PLAN_HASH:
            raise SearchVerificationError("frozen_plan_required")
        validate_scope(plan)
        if args.verify:
            result = execute_verification(plan)
        else:
            result = {"status": "dry_run", "snapshot_id": plan["manifest"]["snapshot_id"],
                      "plan_sha256": plan["manifest_sha256"], "cached_queries": QUERY_COUNT,
                      "query_dimension": 512, "groups_per_query": TOP, "network_calls": 0,
                      "local_writes": 0, "cloud_writes": 0, "new_embedding_calls": 0}
        print(cloud.encoded(result).decode(), end="")
        return 0 if result["status"] != "failed" else 1
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_code": safe_code(exc), "new_embedding_calls": 0, "cloud_writes": 0}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
