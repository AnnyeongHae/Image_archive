"""A narrowly bounded, lossless structural adapter; never mutates model output."""
from __future__ import annotations

import copy

from jsonschema import Draft202012Validator

from .luna_analysis_import import digest, encode, LunaImportError
from .luna_compact import validate_compact

ADAPTER_VERSION = "compact-layout-literal-join-1"


def project_compact(result: dict, draft: dict, pinned: dict, *, expected_style_id: str,
                    original_prompt: str) -> dict:
    kwargs = {"expected_style_id": expected_style_id, "original_prompt": original_prompt}
    errors = list(Draft202012Validator(pinned["schema"]).iter_errors(result))
    if not errors:
        if (set(draft) == {"schema_version", "style_id", "visual"}
                and draft["schema_version"] == "luna-compact-3"):
            effective_draft = {"style_id": draft["style_id"], "visual": copy.deepcopy(draft["visual"])}
            # Only a redundant envelope label is removed in a derived copy.
            # Strict semantic validation still requires identical visual facts.
            validate_compact(result, pinned, visual_draft=effective_draft, **kwargs)
            receipt = {"adapter_version": "compact-draft-envelope-1", "style_id": expected_style_id,
                       "raw_status": "conformant_result_nonconformant_draft_envelope_only",
                       "derived_status": "validated_after_literal_normalization",
                       "lossless_envelope_normalization": True, "model_calls": 0,
                       "raw_value_sha256": digest(encode(result)), "draft_value_sha256": digest(encode(draft)),
                       "derived_value_sha256": digest(encode(result)),
                       "derived_draft_value_sha256": digest(encode(effective_draft)),
                       "removed_envelope_fields": {"schema_version": "luna-compact-3"},
                       "visual_fields_unchanged": True, "metadata_human_approved": False, "release_eligible": False}
            return {"result": result, "draft": effective_draft, "normalization": receipt}
        validate_compact(result, pinned, visual_draft=draft, **kwargs)
        return {"result": result, "draft": draft, "normalization": None}
    if (len(errors) != 1 or list(errors[0].absolute_path) != ["visual", "layout"]
            or errors[0].validator != "maxItems" or len(result["visual"]["layout"]) != 4):
        raise LunaImportError("Not an eligible single layout-count overflow")
    relaxed = {**pinned, "schema": copy.deepcopy(pinned["schema"])}
    relaxed["schema"]["properties"]["visual"]["properties"]["layout"]["maxItems"] = 4
    # All semantic checks, evidence whitelist and raw/draft equality still apply.
    validate_compact(result, relaxed, visual_draft=draft, **kwargs)
    old_layout = result["visual"]["layout"]
    separator = " · "
    joined = old_layout[0] + separator + old_layout[1]
    if len(joined) > 100:
        raise LunaImportError("Literal layout join exceeds compact length")
    effective = copy.deepcopy(result)
    effective["visual"]["layout"] = [joined, old_layout[2], old_layout[3]]
    mapping = {"/visual/layout/0": "/visual/layout/0", "/visual/layout/1": "/visual/layout/0",
               "/visual/layout/2": "/visual/layout/1", "/visual/layout/3": "/visual/layout/2"}
    for use in effective["uses"]:
        use["evidence_refs"] = list(dict.fromkeys(mapping.get(ref, ref) for ref in use["evidence_refs"]))
    for conflict in effective["prompt"]["conflicts"]:
        conflict["visual_ref"] = mapping.get(conflict["visual_ref"], conflict["visual_ref"])
    effective_draft = {"style_id": expected_style_id, "visual": copy.deepcopy(effective["visual"])}
    validate_compact(effective, pinned, visual_draft=effective_draft, **kwargs)
    receipt = {"adapter_version": ADAPTER_VERSION, "style_id": expected_style_id,
               "raw_status": "nonconformant_layout_count_only",
               "derived_status": "validated_after_literal_normalization", "lossless_literal_join": True,
               "model_calls": 0, "raw_value_sha256": digest(encode(result)), "draft_value_sha256": digest(encode(draft)),
               "derived_value_sha256": digest(encode(effective)), "derived_draft_value_sha256": digest(encode(effective_draft)),
               "source_layout": old_layout, "derived_layout": effective["visual"]["layout"],
               "separator": separator, "evidence_pointer_mapping": mapping,
               "metadata_human_approved": False, "release_eligible": False}
    return {"result": effective, "draft": effective_draft, "normalization": receipt}
