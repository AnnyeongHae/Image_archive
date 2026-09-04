"""Explicit one-time HTTP 429 recovery; never erase an uncertain reservation."""
from __future__ import annotations

from datetime import datetime, timezone

from .experiment import digest, json_bytes


def attempt_keys(ledger, requests: dict, cached: set, evidence: dict | None,
                 manifest_sha256: str, *, current_time=None, renewed_authorization=None) -> dict[str, str]:
    aliases = {key: key for key in requests}
    if evidence is None:
        return aliases
    if (evidence.get("source_manifest_sha256") != manifest_sha256
            or evidence.get("ledger_sha256") != digest(json_bytes(ledger.data))
            or evidence.get("http_status") != 429
            or evidence.get("maximum_retries") != 1
            or evidence.get("observation_source") != "reviewed_execution_output"
            or not evidence.get("evidence_note")):
        raise ValueError("invalid reviewed HTTP 429 recovery evidence")
    key = evidence.get("request_key")
    previous = [a for a in ledger.data["attempts"] if a["key"] == key]
    if key not in requests or key in cached or len(previous) != 1:
        raise ValueError("recovery must target exactly one uncached attempted request")
    if previous[0]["status"] != "failed_or_uncertain":
        raise ValueError("recovery target is not a failed request")
    latest = [a for a in ledger.data["attempts"] if a["key"] == key or a["key"].startswith(key + ":")][-1]
    if latest.get("http_status") != 429 or latest.get("provider_status") != "rate_limited":
        raise ValueError("latest attempt is not a confirmed rate-limit rejection")
    if (previous[0].get("http_status") != 429
            or previous[0].get("provider_status") != "rate_limited"
            or previous[0].get("quota_exhausted") is True
            or previous[0].get("quota_period") == "day"):
        raise ValueError("recovery requires ledger-recorded rate limiting, not ambiguous or exhausted quota")
    renewal = (renewed_authorization or {}).get("renewed_retry")
    renewed_alias = None
    if renewal is not None:
        if (renewed_authorization.get("source_manifest_sha256") != manifest_sha256
                or renewed_authorization.get("authorization_source") != "user_message"
                or renewed_authorization.get("external_ai_approved") is not True
                or not renewed_authorization.get("user_quote")
                or renewal.get("request_key") != key
                or renewal.get("max_additional_attempts") != 1
                or not renewal.get("phase_id")
                or evidence.get("renewed_authorization_sha256") != digest(json_bytes(renewed_authorization))):
            raise ValueError("new user retry authorization must bind this exact failure and evidence")
        renewed_alias = key + ":user-authorized-retry:" + digest(json_bytes(renewal))[:16]
        if any(a["key"] == renewed_alias for a in ledger.data["attempts"]):
            raise ValueError("this renewed user authorization has already been attempted")
    if renewed_alias is None and any(":http429-recovery-" in a["key"] or ":user-authorized-retry:" in a["key"] for a in ledger.data["attempts"]):
        raise ValueError("one reviewed recovery maximum per comparison")
    observed = datetime.fromisoformat(evidence["observed_at"].replace("Z", "+00:00"))
    started = datetime.fromisoformat(latest["at"].replace("Z", "+00:00"))
    current_time = current_time or datetime.now(timezone.utc)
    if observed.tzinfo is None or started.tzinfo is None:
        raise ValueError("recovery evidence needs timezone-aware timestamps")
    cooldown = max(60, latest.get("retry_after_seconds") or 0)
    if (current_time - max(observed, started)).total_seconds() < cooldown:
        raise ValueError("HTTP 429 recovery requires the server delay and at least 60 seconds cooldown")
    aliases[key] = renewed_alias or key + ":http429-recovery-1"
    return aliases
