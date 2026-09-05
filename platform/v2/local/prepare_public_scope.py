"""Create an immutable review-PENDING public scope; never a release approval."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import frontend_projection as projection

ROOT = projection.ROOT
SCOPES = ["image_derivatives", "exact_original_prompts", "luna_metadata_candidates",
          "browse_categories", "human_reviewed_groups", "source_attribution", "rights_caution"]


def prepare(*, apply=False):
    plan_path = ROOT / projection.PINNED_SNAPSHOT
    plan = projection.read_snapshot(plan_path)
    media = {row["item_id"]: row for row in plan["manifest"]["media_manifest"]}
    grant = {
        "schema_version": "image-gallery-public-reference-grant-1",
        "decision": "review_pending", "purpose": "public_reference_display",
        "snapshot_id": plan["items"][0]["snapshot_id"],
        "snapshot_manifest_sha256": hashlib.sha256((plan_path / "manifest.json").read_bytes()).hexdigest(),
        "approved_by": None, "approved_at": None, "decision_evidence": None,
        "commercial_rights_approved": False, "license_verified": False,
        "scopes": SCOPES,
        "items": [{"item_id": row["item_id"], "group_id": row["group_id"],
                   "representative_id": row["representative_id"],
                   "prompt_sha256": hashlib.sha256(row["original_prompt"].encode("utf-8")).hexdigest(),
                   "prepared_sha256": media[row["item_id"]]["prepared_sha256"]}
                  for row in sorted(plan["items"], key=lambda item: item["item_id"])],
    }
    data = projection.encoded(grant)
    digest = projection.sha(data)
    target = ROOT / "data/private-research/platform-v2/public-scope-drafts" / digest / "grant.json"
    if apply:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() != data:
            raise ValueError("immutable_scope_collision")
        if not target.exists():
            with target.open("xb") as handle:
                handle.write(data)
    return {"status": "prepared_pending_human_review" if apply else "dry_run",
            "release_eligible": False, "grant": target.relative_to(ROOT).as_posix(),
            "grant_sha256": digest, "items": len(grant["items"]),
            "groups": len({row["group_id"] for row in grant["items"]})}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write ignored pending draft only; no publication")
    args = parser.parse_args()
    print(json.dumps(prepare(apply=args.apply)))
