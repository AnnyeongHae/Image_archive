from __future__ import annotations

from dataclasses import dataclass
from typing import Any


MODEL_NONE = "none"
MODEL_LUNA = "gpt-5.6-luna"
MODEL_TERRA = "gpt-5.6-terra"
MODEL_SOL = "gpt-5.6-sol"

TASK_DEFAULTS = {
    "canonical_hashing": MODEL_NONE,
    "prompt_sha_indexing": MODEL_NONE,
    "media_sha_indexing": MODEL_NONE,
    "remote_media_probe": MODEL_NONE,
    "delivery_benchmark": MODEL_NONE,
    "exact_duplicate_resolution": MODEL_NONE,
    "prompt_family_naming": MODEL_LUNA,
    "metadata_repair": MODEL_LUNA,
    "taxonomy_backfill": MODEL_LUNA,
    "ambiguous_visual_family_review": MODEL_TERRA,
    "rerank_with_visual_context": MODEL_TERRA,
    "rights_sensitive_release_review": MODEL_SOL,
    "brand_or_person_risk_review": MODEL_TERRA,
}


@dataclass(frozen=True)
class RoutingDecision:
    task_class: str
    model: str
    reason: str
    escalation_required: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_class": self.task_class,
            "model": self.model,
            "reason": self.reason,
            "escalation_required": self.escalation_required,
        }


def route_task(
    task_class: str,
    *,
    confidence: float | None = None,
    contains_people: bool = False,
    contains_brands: bool = False,
    rights_sensitive: bool = False,
    final_release_decision: bool = False,
) -> RoutingDecision:
    normalized = str(task_class or "").strip() or "unknown"
    base = TASK_DEFAULTS.get(normalized, MODEL_LUNA)
    if base == MODEL_NONE:
        return RoutingDecision(
            task_class=normalized,
            model=MODEL_NONE,
            reason="deterministic path",
            escalation_required=False,
        )
    if final_release_decision and (rights_sensitive or contains_people or contains_brands):
        return RoutingDecision(
            task_class=normalized,
            model=MODEL_SOL,
            reason="final risk-sensitive release adjudication",
            escalation_required=True,
        )
    if normalized in {"prompt_family_naming", "metadata_repair", "taxonomy_backfill"} and confidence is not None:
        if confidence >= 0.92:
            return RoutingDecision(
                task_class=normalized,
                model=MODEL_NONE,
                reason="high-confidence deterministic shortcut",
                escalation_required=False,
            )
    if base == MODEL_SOL and not final_release_decision:
        return RoutingDecision(
            task_class=normalized,
            model=MODEL_TERRA,
            reason="draft review only; Sol reserved for final release adjudication",
            escalation_required=False,
        )
    if (rights_sensitive or contains_people or contains_brands) and base == MODEL_LUNA:
        return RoutingDecision(
            task_class=normalized,
            model=MODEL_LUNA,
            reason="semantic tagging is not a release decision",
            escalation_required=False,
        )
    return RoutingDecision(
        task_class=normalized,
        model=base,
        reason="default policy",
        escalation_required=base == MODEL_SOL,
    )


def public_policy_summary() -> dict[str, Any]:
    return {
        "schema_version": "image-archive-model-routing-1.0",
        "default": MODEL_NONE,
        "task_defaults": dict(TASK_DEFAULTS),
        "notes": [
            "Use no model for hashing, parsing, exact dedupe, delivery benchmarking, and remote probing.",
            "Use Luna for cheap metadata repair and prompt-family naming only when deterministic signals are insufficient.",
            "Use Terra for ambiguous multimodal family review and reranking.",
            "People or brand presence alone does not justify Sol; ordinary tagging remains Luna and ambiguous risk triage remains Terra.",
            "Use Sol only for a final risk-sensitive release adjudication that already passed lower-cost lanes.",
        ],
    }
