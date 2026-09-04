from __future__ import annotations

import argparse
import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    from .common import DATA_ROOT, atomic_write_json, ensure_directories, load_config, read_json, sha256_text, source_id, stable_json
except ImportError:  # direct script execution
    from common import DATA_ROOT, atomic_write_json, ensure_directories, load_config, read_json, sha256_text, source_id, stable_json


PROMPT_BODY_KEYS = {"prompt", "prompts", "prompt_text", "negative_prompt", "system_prompt"}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def extract_payload_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "prompts", "results", "records"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        nested = extract_payload_items(data)
        if nested:
            return nested
    return []


def extract_detail(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("detail API response must be an object")
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def is_paid_or_locked(record: dict[str, Any]) -> bool:
    access_type = record.get("access_type")
    return bool(
        record.get("is_paid") is True
        or record.get("paid") is True
        or record.get("is_unlocked") is False
        or str(access_type) in {"1", "paid", "premium"}
    )


def strip_prompt_bodies(record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    clean: dict[str, Any] = {}
    removed = False
    for key, value in record.items():
        if key.casefold() in PROMPT_BODY_KEYS:
            removed = removed or value not in (None, "", [], {})
            continue
        clean[key] = value
    return clean, removed


def metadata_version(record: dict[str, Any]) -> str:
    for key in ("updated_at", "reviewed_at", "modified_at", "created_at"):
        value = record.get(key)
        if value:
            return str(value)
    filtered = {key: value for key, value in record.items() if key.casefold() not in PROMPT_BODY_KEYS}
    return sha256_text(stable_json(filtered, indent=None))


def parse_generic_robots(text: str) -> dict[str, Any]:
    """Return directives that apply only to the generic User-agent: * group.

    A later GPTBot/ClaudeBot block with ``Disallow: /`` must not be applied to
    this collector. This deliberately does not attempt to match our named UA;
    the collector's policy is to honor the public generic group.
    """
    blocks: list[tuple[list[str], list[tuple[str, str]]]] = []
    agents: list[str] = []
    directives: list[tuple[str, str]] = []

    def flush() -> None:
        nonlocal agents, directives
        if agents:
            blocks.append((agents, directives))
        agents = []
        directives = []

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        name, value = (part.strip() for part in line.split(":", 1))
        name = name.casefold()
        if name == "user-agent":
            if directives:
                flush()
            agents.append(value.casefold())
        elif agents:
            directives.append((name, value.strip()))
    flush()

    generic = [directive for block_agents, block in blocks if "*" in block_agents for directive in block]
    rules = [(name, value) for name, value in generic if name in {"allow", "disallow"}]
    signals: dict[str, str] = {}
    for name, value in generic:
        if name not in {"content-signal", "content-signal-policy"}:
            continue
        for part in value.split(","):
            if "=" in part:
                key, setting = (piece.strip().casefold() for piece in part.split("=", 1))
                signals[key] = setting
    return {"rules": rules, "content_signals": signals, "generic_group_found": bool(generic)}


def generic_robots_allows(text: str, path: str) -> tuple[bool, dict[str, Any]]:
    parsed = parse_generic_robots(text)
    matching: list[tuple[int, bool]] = []
    for name, rule_path in parsed["rules"]:
        if not rule_path:
            continue
        normalized_rule = rule_path.split("*", 1)[0]
        if path.startswith(normalized_rule):
            matching.append((len(normalized_rule), name == "allow"))
    if not matching:
        return True, parsed
    longest = max(length for length, _ in matching)
    winners = [allowed for length, allowed in matching if length == longest]
    return any(winners), parsed


def sample_bundle() -> dict[str, Any]:
    observed_at = "2026-08-31T00:00:00Z"
    free_details = [
        {
            "id": "sample-1001",
            "slug": "citrus-product-hero-grid",
            "title": "Citrus product hero grid",
            "access_type": 0,
            "is_unlocked": True,
            "model": "gpt-image-2",
            "tags": ["product", "beverage", "hero", "grid"],
            "reviewed_at": "2026-08-30T10:00:00Z",
            "author": {"name": "Sample Curator"},
            "cover_image_url": "https://example.invalid/opennana/sample-1001.jpg",
            "prompts": [
                {"text": "Create a bright citrus beverage hero with a clean product pedestal, generous copy space on the left, crisp studio daylight, and a 4:5 editorial crop."},
                {"text": "Keep all brand text editable outside the generated image. Do not invent labels, claims, awards, or logos."},
            ],
        },
        {
            "id": "sample-1002",
            "slug": "citrus-product-hero-grid-copy",
            "title": "Citrus product hero grid duplicate",
            "access_type": 0,
            "is_unlocked": True,
            "model": "gpt-image-2",
            "tags": ["product", "beverage"],
            "reviewed_at": "2026-08-30T11:00:00Z",
            "cover_image_url": "https://example.invalid/opennana/sample-1002.jpg",
            "prompts": [
                {"text": "Create a bright citrus beverage hero with a clean product pedestal, generous copy space on the left, crisp studio daylight, and a 4:5 editorial crop."},
                {"text": "Keep all brand text editable outside the generated image. Do not invent labels, claims, awards, or logos."},
            ],
        },
        {
            "id": "sample-1003",
            "slug": "citrus-product-hero-grid-variation",
            "title": "Citrus product hero grid variation",
            "access_type": 0,
            "is_unlocked": True,
            "model": "gpt-image-2",
            "tags": ["product", "beverage", "variation"],
            "reviewed_at": "2026-08-30T12:00:00Z",
            "cover_image_url": "https://example.invalid/opennana/sample-1003.jpg",
            "prompts": [
                {"text": "Create a bright citrus beverage hero with a clean product pedestal, generous copy space on the left, crisp studio daylight, and a 4:5 editorial crop. Add restrained condensation and one sliced lemon."},
                {"text": "Keep all brand text editable outside the generated image. Do not invent labels, claims, awards, or logos."},
            ],
        },
        {
            "id": "sample-1004",
            "slug": "universal-product-storyboard",
            "title": "Universal product storyboard",
            "access_type": 0,
            "is_unlocked": True,
            "model": "gpt-image-2",
            "tags": ["storyboard", "product", "how-to"],
            "reviewed_at": "2026-08-30T13:00:00Z",
            "cover_image_url": "https://example.invalid/opennana/sample-1004.jpg",
            "prompts": "Build a six-frame commercial storyboard for [PRODUCT]. Show [PRODUCT] alone, held by a hand, used in context, a material detail, a benefit metaphor, and an end card. Preserve [BRAND COLOR].",
        },
        {
            "id": "sample-1005",
            "slug": "universal-product-storyboard-remix",
            "title": "Universal product storyboard remix",
            "access_type": 0,
            "is_unlocked": True,
            "model": "gpt-image-2",
            "tags": ["storyboard", "cosmetics", "remix"],
            "reviewed_at": "2026-08-30T14:00:00Z",
            "cover_image_url": "https://example.invalid/opennana/sample-1005.jpg",
            "prompts": "Build a six-frame commercial storyboard for [SERUM BOTTLE]. Show [SERUM BOTTLE] alone, held by a hand, used in context, a material detail, a benefit metaphor, and an end card. Preserve [COBALT BLUE].",
        },
        {
            "id": "sample-1006",
            "slug": "technical-cutaway-product",
            "title": "Technical cutaway product reference",
            "access_type": 0,
            "is_unlocked": True,
            "model": "gpt-image-2",
            "tags": ["cutaway", "technical", "product"],
            "reviewed_at": "2026-08-30T15:00:00Z",
            "cover_image_url": "https://example.invalid/opennana/sample-1006.jpg",
            "prompts": "Create a clean three-quarter technical cutaway of a generic portable speaker. Keep the outer silhouette intact, reveal only plausible generic component zones, use numbered callout anchors without generated labels, and leave a wide text rail for later HTML annotations.",
        },
    ]
    locked_metadata = {
        "id": "sample-paid-2001",
        "slug": "locked-premium-sample",
        "title": "Locked premium metadata only",
        "access_type": 1,
        "is_unlocked": False,
        "reviewed_at": "2026-08-30T16:00:00Z",
    }
    return {
        "schema_version": "opennana-raw-bundle-1.0",
        "run_id": "sample-canary-v1",
        "mode": "local_fabricated_sample",
        "observed_at": observed_at,
        "source": "opennana",
        "request_summary": {"network_requests": 0, "list_items": 7, "free_details": 6, "locked_metadata_only": 1},
        "list_metadata": [
            {key: value for key, value in detail.items() if key not in PROMPT_BODY_KEYS}
            for detail in free_details
        ] + [locked_metadata],
        "selected_list_metadata": [
            {key: value for key, value in detail.items() if key not in PROMPT_BODY_KEYS}
            for detail in free_details
        ] + [locked_metadata],
        "free_details": free_details,
        "locked_metadata_only": [locked_metadata],
        "anomalies": [],
    }


class RateLimitedClient:
    def __init__(self, *, requests_per_second: float, timeout: int, user_agent: str) -> None:
        self.minimum_interval = 1.0 / max(requests_per_second, 0.01)
        self.timeout = timeout
        self.user_agent = user_agent
        self.last_request_at = 0.0
        self.request_count = 0

    def get_text(self, url: str) -> str:
        elapsed = time.monotonic() - self.last_request_at
        if elapsed < self.minimum_interval:
            time.sleep(self.minimum_interval - elapsed)
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept": "application/json,text/plain;q=0.9,*/*;q=0.1"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = getattr(response, "status", 200)
                if status in (403, 429):
                    raise RuntimeError(f"collection stopped: upstream returned {status}")
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                raise RuntimeError(f"collection stopped: upstream returned {exc.code}") from exc
            raise
        self.last_request_at = time.monotonic()
        self.request_count += 1
        return body

    def get_json(self, url: str) -> Any:
        return json.loads(self.get_text(url))


def fetch_bundle(config: dict[str, Any], state: dict[str, Any], max_details: int) -> dict[str, Any]:
    collection = config["collection"]
    source = config["source"]
    if int(collection.get("concurrency", 1)) != 1:
        raise ValueError("OpenNana canary requires concurrency=1")
    client = RateLimitedClient(
        requests_per_second=float(collection["requests_per_second"]),
        timeout=int(collection["timeout_seconds"]),
        user_agent=str(collection["user_agent"]),
    )
    robots = client.get_text(source["robots_url"])
    robots_allowed, robots_policy = generic_robots_allows(robots, "/api/prompts")
    if not robots_allowed:
        raise RuntimeError("collection stopped: generic robots policy disallows /api/prompts")
    signals = robots_policy["content_signals"]
    if signals.get("search") != "yes" or signals.get("ai-train") != "no":
        raise RuntimeError("collection stopped: generic robots Content-Signal is missing search=yes or ai-train=no")
    params = urllib.parse.urlencode(
        {
            "page": collection["page"],
            "limit": collection["page_size"],
            "sort": collection["sort"],
            "order": collection["order"],
            "access_type": 0,
        }
    )
    list_payload = client.get_json(f"{source['list_endpoint']}?{params}")
    list_items = extract_payload_items(list_payload)
    previous_versions = state.get("source_versions", {})
    changed = [item for item in list_items if previous_versions.get(source_id(item)) != metadata_version(item)]
    selected = changed[:max_details]
    free_details: list[dict[str, Any]] = []
    locked_metadata: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    for item in selected:
        slug = str(item.get("slug") or source_id(item))
        detail_url = source["detail_endpoint_template"].format(slug=urllib.parse.quote(slug, safe=""))
        detail = extract_detail(client.get_json(detail_url))
        if is_paid_or_locked(detail):
            clean, removed = strip_prompt_bodies(detail)
            locked_metadata.append(clean)
            if removed:
                anomalies.append({"code": "paid_body_removed", "upstream_id": source_id(detail)})
            continue
        if str(detail.get("access_type", 0)) not in {"0", "free", "None"}:
            raise RuntimeError(f"collection stopped: unexpected access_type for {source_id(detail)}")
        free_details.append(detail)
    observed_at = utc_now()
    run_id = observed_at.replace(":", "").replace("-", "").replace("Z", "Z").replace("T", "T")
    return {
        "schema_version": "opennana-raw-bundle-1.0",
        "run_id": f"fetch-{run_id}",
        "mode": "network_canary",
        "observed_at": observed_at,
        "source": "opennana",
        "request_summary": {
            "network_requests": client.request_count,
            "list_items": len(list_items),
            "changed_or_new": len(changed),
            "selected_details": len(selected),
            "overflow_requires_review": max(0, len(changed) - len(selected)),
            "free_details": len(free_details),
            "locked_metadata_only": len(locked_metadata),
        },
        "robots_observed_sha256": sha256_text(robots),
        "robots_policy": {
            "evaluated_user_agent": "*",
            "evaluated_path": "/api/prompts",
            "allowed": robots_allowed,
            "content_signals": signals,
        },
        "list_metadata": list_items,
        "selected_list_metadata": selected,
        "free_details": free_details,
        "locked_metadata_only": locked_metadata,
        "anomalies": anomalies,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Conservative OpenNana reference collector (dry-run by default).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--seed-sample", action="store_true", help="Use locally fabricated canary metadata; no network.")
    mode.add_argument("--fetch", action="store_true", help="Allow bounded upstream network reads; requires --apply.")
    parser.add_argument("--apply", action="store_true", help="Write private raw/run/state artifacts.")
    parser.add_argument("--max-details", type=int, default=None, help="Detail cap; never greater than 20 or configured cap.")
    parser.add_argument("--config", type=Path, default=DATA_ROOT / "config.json")
    parser.add_argument("--state", type=Path, default=DATA_ROOT / "state.json")
    args = parser.parse_args()

    config = load_config(args.config)
    configured_cap = int(config["collection"]["canary_max_details"])
    max_details = args.max_details if args.max_details is not None else configured_cap
    if configured_cap > 20 or not 1 <= max_details <= min(20, configured_cap):
        parser.error("max-details must be between 1 and the configured canary cap (maximum 20)")
    if args.fetch and not args.apply:
        parser.error("network fetch requires both --fetch and --apply")

    if not args.seed_sample and not args.fetch:
        print(stable_json({
            "mode": "dry_run_plan",
            "writes": False,
            "network": False,
            "next": "use --seed-sample --apply for the local canary, or --fetch --apply for an approved network canary",
            "max_details": max_details,
        }), end="")
        return 0

    state = read_json(args.state)
    bundle = sample_bundle() if args.seed_sample else fetch_bundle(config, state, max_details)
    bundle["free_details"] = bundle["free_details"][:max_details]
    if not args.apply:
        print(stable_json({"mode": bundle["mode"], "writes": False, "network": False, "summary": bundle["request_summary"]}), end="")
        return 0

    ensure_directories()
    raw_path = DATA_ROOT / "raw" / f"{bundle['run_id']}.json"
    run_path = DATA_ROOT / "runs" / f"{bundle['run_id']}.json"
    atomic_write_json(raw_path, bundle)
    atomic_write_json(run_path, {
        "schema_version": "opennana-run-record-1.0",
        "run_id": bundle["run_id"],
        "mode": bundle["mode"],
        "observed_at": bundle["observed_at"],
        "stage": "fetched_raw",
        "raw_path": raw_path.relative_to(DATA_ROOT).as_posix(),
        "request_summary": bundle["request_summary"],
    })
    if args.fetch:
        state["last_collection_run_id"] = bundle["run_id"]
        state["last_observed_at"] = bundle["observed_at"]
        versions = dict(state.get("source_versions", {}))
        # Do not advance the watermark for overflow rows that were not fetched;
        # they must remain eligible for the next bounded canary run.
        for item in bundle.get("selected_list_metadata", []):
            versions[source_id(item)] = metadata_version(item)
        state["source_versions"] = versions
        atomic_write_json(args.state, state)
    print(stable_json({"written": [str(raw_path), str(run_path)], "summary": bundle["request_summary"]}), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
