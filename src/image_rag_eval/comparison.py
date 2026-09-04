"""Three-arm private canary: Gemini image/joint and Voyage image, one budget."""
from __future__ import annotations

import copy
import itertools
import json
import math
import time
from pathlib import Path

from PIL import Image

from .experiment import (BudgetLedger, IMAGE_RESERVE_USD, TEXT_RESERVE_USD, digest,
    json_bytes, now, read_json, run_lock, run_path, safe_source, unit_prefix, write_json)
from .providers import GeminiEmbedder, ProviderError
from .similarity import build_groups, build_visual_families, compare_pair, cosine, rank

MODELS = {"gemini": "gemini-embedding-2", "voyage": "voyage-multimodal-3.5"}
DIMENSIONS = {"gemini": 3072, "voyage": 1024}
ARMS = {"gemini_image": ("gemini", False), "gemini_joint": ("gemini", True), "voyage_image": ("voyage", False)}
MAX_REQUESTS = 70
QUERIES = [
    {"id": "q01", "text": "교실에 있는 애니메이션 캐릭터들의 장면", "relevance": {}, "human_judged": False},
    {"id": "q02", "text": "손으로 그린 도시 맛집 여행 지도", "relevance": {}, "human_judged": False},
    {"id": "q03", "text": "여러 가지 옷차림을 한 화면에 배치한 패션 콜라주", "relevance": {}, "human_judged": False},
    {"id": "q04", "text": "따뜻한 조명과 나무 인테리어의 일본 료칸 인물 사진", "relevance": {}, "human_judged": False},
    {"id": "q05", "text": "영화 스트리밍 서비스의 메인 화면 UI", "relevance": {}, "human_judged": False},
]


def request_key(request: dict) -> str:
    identity = {key: request[key] for key in ("provider", "model", "dimensions", "image_sha256", "text", "task")}
    identity["protocol"] = "three-arm-canary-v1"
    return digest(json_bytes(identity))


def selected_arms(arms_subset=None, providers_subset=None) -> tuple[list[str], list[str]]:
    arms = list(ARMS)
    explicit_arms = arms_subset is not None
    if arms_subset is not None:
        requested = []
        for arm in arms_subset:
            value = str(arm)
            if value not in ARMS:
                raise ValueError("invalid arm subset")
            if value not in requested:
                requested.append(value)
        arms = requested
    providers = None
    if providers_subset is not None:
        providers = []
        for provider in providers_subset:
            value = str(provider)
            if value not in MODELS:
                raise ValueError("invalid provider subset")
            if value not in providers:
                providers.append(value)
    if providers is not None:
        invalid = [arm for arm in arms if ARMS[arm][0] not in providers]
        if explicit_arms and invalid:
            raise ValueError("arm subset conflicts with provider subset")
        arms = [arm for arm in arms if ARMS[arm][0] in providers]
    if not arms:
        raise ValueError("no comparison arms selected")
    query_providers = []
    for arm in arms:
        provider = ARMS[arm][0]
        if provider not in query_providers:
            query_providers.append(provider)
    return arms, query_providers


def project_selected_arms(root: Path, arms_subset=None, providers_subset=None) -> tuple[list[str], list[str]]:
    """Honor an explicit project provider pause without discarding old caches."""
    path = root / "data/private-research/image-rag-canary/active-profile.json"
    if not path.exists():
        return selected_arms(arms_subset, providers_subset)
    profile = read_json(path)
    enabled = profile.get("enabled_providers")
    defaults = profile.get("default_arms")
    if (profile.get("schema_version") != "1" or profile.get("status") != "active"
            or not isinstance(enabled, list) or not enabled or not set(enabled) <= set(MODELS)
            or not isinstance(defaults, list) or not defaults):
        raise ValueError("invalid active image-RAG provider profile")
    default_arms, default_providers = selected_arms(defaults)
    if not set(default_providers) <= set(enabled):
        raise ValueError("profile defaults conflict with enabled providers")
    if arms_subset is None and providers_subset is None:
        return default_arms, default_providers
    arms, providers = selected_arms(arms_subset, providers_subset)
    if not set(providers) <= set(enabled):
        raise ValueError("provider paused by active project profile; explicit user selection change required")
    return arms, providers


def load_inputs(root: Path, source_run_id: str, maximum_items=20) -> tuple[dict, dict[str, bytes], dict[str, int]]:
    if maximum_items not in (20, 50, 200):
        raise ValueError("sample limit must be explicitly 20, 50 or 200")
    source = run_path(root, source_run_id)
    manifest = read_json(source / "manifest.json")
    receipt = read_json(source / "prepared.json")
    if receipt.get("complete") is not True or receipt.get("manifest_sha256") != digest(json_bytes(manifest)):
        raise ValueError("source preparation receipt mismatch")
    if not 1 <= len(manifest["items"]) <= maximum_items:
        raise ValueError("source sample exceeds the selected explicit item limit")
    if len(manifest["items"]) > 20:
        parent_id = receipt.get("source_run_id")
        parent_count = 50 if len(manifest["items"]) > 50 else 20
        if not parent_id or parent_id == source_run_id or receipt.get("preserved_item_count") != parent_count:
            raise ValueError("expanded sample needs its immutable bounded parent")
        parent, _, _ = load_inputs(root, parent_id, maximum_items=parent_count)
        if (len(parent["items"]) != parent_count or receipt.get("source_manifest_sha256") != digest(json_bytes(parent))
                or manifest["items"][:parent_count] != parent["items"]):
            raise ValueError("expanded source subset does not match its parent")
    ids = [item["id"] for item in manifest["items"]]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate asset id")
    images, pixels = {}, {}
    for item in manifest["items"]:
        original = safe_source(root, item["path"])
        if digest(original.read_bytes()) != item["sha256"]:
            raise ValueError("original source changed")
        relative = Path(item["prepared_path"])
        path = (source / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to((source / "inputs").resolve()):
            raise ValueError("prepared path escapes source run")
        data = path.read_bytes()
        if digest(data) != item["prepared_sha256"]:
            raise ValueError("prepared image changed")
        with Image.open(path) as image:
            if max(image.size) > 768 or image.format != "PNG":
                raise ValueError("unexpected preprocessing")
            pixels[item["id"]] = max(50_000, image.width * image.height)
        images[item["id"]] = data
        if len(item["embedding_prompt"].encode("utf-8")) > 6000:
            raise ValueError("source prompt exceeds byte cap")
    return manifest, images, pixels


def requests_for(manifest: dict, pixels: dict, queries: list[dict], *, arms_subset=None, providers_subset=None) -> list[dict]:
    if len(queries) > 5 or not queries:
        raise ValueError("comparison accepts 1..5 queries")
    if len({q["id"] for q in queries}) != len(queries):
        raise ValueError("duplicate query id")
    if any(not isinstance(q["text"], str) or not q["text"].strip()
           or len(q["text"].encode("utf-8")) > 6000 for q in queries):
        raise ValueError("invalid query text")
    arms, query_providers = selected_arms(arms_subset, providers_subset)
    requests = []
    # A small initial request to each provider detects auth/model failures early.
    for item in manifest["items"]:
        for arm in arms:
            provider, joint = ARMS[arm]
            text = item["embedding_prompt"] if joint else ""
            amount = (IMAGE_RESERVE_USD + (TEXT_RESERVE_USD if text else 0)) if provider == "gemini" else pixels[item["id"]] * .60 / 1_000_000_000 + 256 * .12 / 1_000_000
            request = {"provider": provider, "model": MODELS[provider], "dimensions": DIMENSIONS[provider],
                "image_sha256": item["prepared_sha256"], "text": text, "task": "RETRIEVAL_DOCUMENT",
                "id": item["id"], "arm": arm, "kind": "document", "reserved_usd": amount}
            request["key"] = request_key(request)
            requests.append(request)
    for query in queries:
        for provider in query_providers:
            request = {"provider": provider, "model": MODELS[provider], "dimensions": DIMENSIONS[provider],
                "image_sha256": None, "text": query["text"], "task": "RETRIEVAL_QUERY", "id": query["id"],
                "arm": provider, "kind": "query", "reserved_usd": TEXT_RESERVE_USD if provider == "gemini" else 32000 * .12 / 1_000_000}
            request["key"] = request_key(request)
            requests.append(request)
    return requests


def plan_comparison(root: Path, source_run_id: str, queries: list[dict] | None = None, *,
                    maximum_items=20, providers_subset=None, arms_subset=None) -> dict:
    manifest, _, pixels = load_inputs(root, source_run_id, maximum_items)
    arms, _ = project_selected_arms(root, arms_subset, providers_subset)
    requests = requests_for(manifest, pixels, queries or QUERIES, arms_subset=arms)
    unique = {r["key"]: r for r in requests}
    return {"status": "dry_run", "items": len(manifest["items"]), "queries": len(queries or QUERIES),
        "arms": arms, "logical_requests": len(requests), "unique_requests": len(unique),
        "reserved_upper_bound_usd": round(sum(r["reserved_usd"] for r in unique.values()), 8),
        "price_basis": "standard paid prices, ignoring any Voyage free balance", "network_calls": 0,
        "source_manifest_sha256": digest(json_bytes(manifest))}


def validate_consent(consent: dict, manifest: dict, maximum_usd: float, *, providers=None) -> None:
    if not math.isfinite(maximum_usd) or not 0 < maximum_usd <= .10:
        raise ValueError("combined budget must be positive and <= US$0.10")
    if (consent.get("source_manifest_sha256") != digest(json_bytes(manifest))
            or consent.get("authorization_source") != "user_message"
            or not consent.get("user_quote") or not consent.get("recorded_at")
            or consent.get("external_ai_approved") is not True
            or consent.get("max_cost_usd", 0) < maximum_usd
            or not set(consent.get("providers", [])) <= set(MODELS)
            or not set(providers if providers is not None else MODELS) <= set(consent.get("providers", []))
            or set(consent.get("approved_asset_ids", [])) != {i["id"] for i in manifest["items"]}):
        raise ValueError("explicit combined budget and exact sample authorization required")


def add_order_evidence(root: Path, manifest: dict) -> dict:
    """Canonical ordinal is only an explicit fallback, never an arrival timestamp."""
    result = copy.deepcopy(manifest)
    wanted = {i["catalog_key"] for i in result["items"]}
    rows = {}
    with (root / "data/canonical/archive_records.jsonl").open(encoding="utf-8") as source:
        for ordinal, line in enumerate(source):
            raw = json.loads(line)
            if raw.get("catalog_key") in wanted:
                rows[raw["catalog_key"]] = ordinal + 1
    for item in result["items"]:
        item.update({"ordinal": rows.get(item["catalog_key"]), "arrival_at": None,
            "arrival_basis": "canonical_ordinal_fallback_not_actual_arrival"})
    return result


def prepare_comparison_view(root: Path, source_run_id: str) -> dict:
    """Apply only the reversible local view. Never call an embedding provider."""
    from .retention import build_retention
    from .results_view import render_results

    manifest, _, _ = load_inputs(root, source_run_id)
    source = run_path(root, source_run_id)
    destination = source / "comparison-v1"
    destination.mkdir(exist_ok=True)
    if (destination / "evaluation.json").exists():
        raise ValueError("completed inference results exist; do not overwrite with offline view")
    manifest = add_order_evidence(root, manifest)
    retention = build_retention(manifest["items"])
    pairs = [compare_pair(a, b) for a, b in itertools.combinations(manifest["items"], 2)]
    groups = [g for g in build_groups(manifest["items"], pairs) if g["kind"] == "near_copy_candidate"]
    write_json(destination / "manifest.json", manifest)
    write_json(destination / "retention.json", retention)
    write_json(destination / "queries.json", QUERIES)
    results_path = source / "comparison-results-v1.html"
    results_path.write_text(render_results(manifest, retention, [], groups), encoding="utf-8")
    result = {"status": "local_view_ready_external_transfer_blocked", "active": len(retention["active_ids"]),
        "archived": len(retention["archived"]), "near_copy_candidates": len(groups),
        "arrival_timestamps_missing": sum(r["arrival_at"] == "unknown" for r in retention["order_evidence"]),
        "ordering_fallback": "canonical ordinal, not verified first arrival",
        "embedding_requests": 0, "physical_files_deleted_or_moved": 0, "results_path": str(results_path)}
    write_json(destination / "offline-view-summary.json", result)
    return result


def evaluate_comparison(manifest: dict, vectors: dict, queries: list[dict], retention: dict,
                        *, evaluation_arms=None) -> dict:
    active = set(retention["active_ids"])
    evaluations, groups, pairs = [], [], []
    completed_arms = []
    expected_ids = {i["id"] for i in manifest["items"]}
    expected_queries = {q["id"] for q in queries}
    requested_arms, _ = selected_arms(evaluation_arms)
    for arm in requested_arms:
        provider, _ = ARMS[arm]
        if (set(vectors.get(arm, {})) != expected_ids
                or set(vectors.get(provider + "_queries", {})) != expected_queries):
            continue
        completed_arms.append(arm)
        corpus = {ident: unit_prefix(vector, 1024) for ident, vector in vectors[arm].items()}
        for query in queries:
            query_vector = unit_prefix(vectors[provider + "_queries"][query["id"]], 1024)
            ranked = rank(query_vector, {i: v for i, v in corpus.items() if i in active}, 5)
            evaluations.append({"provider": MODELS[provider], "arm": arm, "dimensions": 1024,
                "query_id": query["id"], "query_text": query["text"], "ranked": ranked,
                "metrics": None, "human_judged": False, "scope": "active_representatives_only"})
        if arm != "gemini_joint":
            for group in build_visual_families({i: v for i, v in corpus.items() if i in active}, k=3, min_cosine=.85):
                groups.append({**group, "provider": MODELS[provider], "dimensions": 1024})
    for a, b in itertools.combinations(manifest["items"], 2):
        def pair_score(arm):
            corpus = vectors.get(arm, {})
            if a["id"] in corpus and b["id"] in corpus:
                return cosine(corpus[a["id"]], corpus[b["id"]])
            return None
        pair = compare_pair(a, b,
            image_cosine=pair_score("gemini_image"), joint_cosine=pair_score("gemini_joint"))
        pair["voyage_image_cosine"] = pair_score("voyage_image")
        pairs.append(pair)
    groups.extend(group for group in build_groups(manifest["items"], pairs) if group["kind"] == "near_copy_candidate")
    return {"status": "completed_unjudged_canary" if set(completed_arms) == set(requested_arms) else "partial_unjudged_canary",
        "requested_arms": requested_arms,
        "completed_arms": completed_arms, "vector_counts": {arm: len(rows) for arm, rows in vectors.items()},
        "evaluations": evaluations, "similarity_groups": groups,
        "pairs": pairs, "winner": None, "accuracy": None,
        "note": "No human relevance labels; query choices are corpus-aware smoke tests, not a held-out benchmark."}


def refresh_comparison(root: Path, source_run_id: str, *, maximum_items=20) -> dict:
    """Re-evaluate validated cached vectors with current retention. Zero API calls."""
    from .retention import build_retention
    from .results_view import render_results
    manifest, _, pixels = load_inputs(root, source_run_id, maximum_items)
    source = run_path(root, source_run_id)
    destination = source / "comparison-v1"
    with run_lock(source.parent), run_lock(source):
        if len(manifest["items"]) > 20:
            from .carryover import validate_parent_checkpoint
            validate_parent_checkpoint(root, source_run_id, read_json(destination / "budget.json"))
        queries = read_json(destination / "queries.json")
        requests = requests_for(manifest, pixels, queries)
        vectors = {key: {} for key in [*ARMS, "gemini_queries", "voyage_queries"]}
        for request in requests:
            path = destination / "vector-cache" / (request["key"] + ".json")
            if not path.exists():
                continue
            value = read_json(path)
            if (value.get("key") != request["key"] or value.get("model") != request["model"]
                    or len(value.get("vector", [])) != request["dimensions"]
                    or value.get("vector_sha256") != digest(json_bytes(value["vector"]))):
                raise ValueError("cached vector identity mismatch")
            target = request["arm"] if request["kind"] == "document" else request["provider"] + "_queries"
            vectors[target][request["id"]] = unit_prefix(value["vector"], request["dimensions"])
        manifest = add_order_evidence(root, manifest)
        retention = build_retention(manifest["items"])
        result = evaluate_comparison(manifest, vectors, queries, retention, evaluation_arms=manifest.get("evaluation_arms"))
        ledger = read_json(destination / "budget.json") if (destination / "budget.json").exists() else {"attempts": []}
        result["budget"] = {"attempts": len(ledger["attempts"]),
            "reserved_upper_bound_usd": sum(a["reserved_usd"] for a in ledger["attempts"]),
            "actual_invoice_usd": None, "voyage_free_balance_verified": False}
        result["refresh_network_calls"] = 0
        manifest["comparison_status"] = result["status"]
        for name, data in (("manifest", manifest), ("retention", retention), ("vectors", vectors), ("evaluation", result)):
            write_json(destination / f"{name}.json", data)
        results_path = source / "comparison-results-v1.html"
        results_path.write_text(render_results(manifest, retention, result["evaluations"], result["similarity_groups"]), encoding="utf-8")
        return {"status": result["status"], "network_calls": 0, "budget": result["budget"],
            "vector_counts": result["vector_counts"], "completed_arms": result["completed_arms"],
            "active": len(retention["active_ids"]), "archived": len(retention["archived"]),
            "results_path": str(results_path)}


def execute_comparison(root: Path, source_run_id: str, consent: dict, *, maximum_usd=.10,
                       clients=None, queries=None, sleep=time.sleep, progress=None,
                       retry_evidence=None, providers_subset=None, arms_subset=None,
                       request_interval_seconds=3.1, max_new_requests=None, maximum_items=20) -> dict:
    from .retention import build_retention
    from .results_view import render_results
    from .voyage_provider import VoyageEmbedder
    from .providers import load_credentials
    from .recovery import attempt_keys

    manifest, images, pixels = load_inputs(root, source_run_id, maximum_items)
    arms, selected = project_selected_arms(root, arms_subset, providers_subset)
    if maximum_items == 200 and arms != ["voyage_image"]:
        raise ValueError("200-record canary is explicitly Voyage-image-only")
    maximum_calls = {20: MAX_REQUESTS, 50: 165, 200: 230}[maximum_items]
    validate_consent(consent, manifest, maximum_usd, providers=selected)
    if not math.isfinite(request_interval_seconds) or not 3.1 <= request_interval_seconds <= 60:
        raise ValueError("request interval must be 3.1..60 seconds")
    if max_new_requests is not None and (isinstance(max_new_requests, bool) or not 1 <= max_new_requests <= maximum_calls):
        raise ValueError("invalid new request cap")
    queries = queries or QUERIES
    all_requests = requests_for(manifest, pixels, queries)
    requests = requests_for(manifest, pixels, queries, arms_subset=arms)
    source = run_path(root, source_run_id)
    destination = source / "comparison-v1"
    destination.mkdir(exist_ok=True)
    with run_lock(source.parent), run_lock(source):
        ledger = BudgetLedger(destination / "budget.json", maximum_usd, maximum_calls)
        if len(manifest["items"]) > 20:
            from .carryover import validate_parent_checkpoint
            validate_parent_checkpoint(root, source_run_id, ledger.data)
        cache_dir = destination / "vector-cache"
        cache_dir.mkdir(exist_ok=True)
        cached, unique = {}, {r["key"]: r for r in requests}
        for key, request in {r["key"]: r for r in all_requests}.items():
            path = cache_dir / f"{key}.json"
            if path.exists():
                value = read_json(path)
                if (value.get("key") != key or value.get("model") != request["model"]
                        or len(value.get("vector", [])) != request["dimensions"]
                        or value.get("vector_sha256") != digest(json_bytes(value["vector"]))):
                    raise ValueError("cached vector identity mismatch")
                cached[key] = unit_prefix(value["vector"], request["dimensions"])
        # A bounded probe budgets exactly its first N uncached unique requests.
        # All historical reservations still count; unselected work is neither
        # reserved nor sent. Full runs keep their all-remaining-work precheck.
        planned_unique = {}
        planned_new = 0
        for key, request in unique.items():
            if key in cached:
                planned_unique[key] = request
            elif max_new_requests is None or planned_new < max_new_requests:
                planned_unique[key] = request
                planned_new += 1
        aliases = attempt_keys(ledger, planned_unique, set(cached), retry_evidence, digest(json_bytes(manifest)),
            renewed_authorization=consent if retry_evidence is not None else None)
        ledger.precheck({aliases[k]: r["reserved_usd"] for k, r in planned_unique.items()}, set(cached))
        if retry_evidence is not None:
            write_json(destination / "reviewed-http429-recovery.json", retry_evidence)
        if clients is None:
            credentials = load_credentials([root / ".env"])
            if any(not credentials.get(provider.upper() + "_API_KEY") for provider in selected):
                raise ValueError("selected provider keys must be present before inference")
            clients = {}
            if "gemini" in selected:
                clients["gemini"] = GeminiEmbedder(credentials["GEMINI_API_KEY"], dimensions=3072)
            if "voyage" in selected:
                clients["voyage"] = VoyageEmbedder(credentials["VOYAGE_API_KEY"], dimensions=1024)
        ordered_manifest = add_order_evidence(root, manifest)
        retention = build_retention(ordered_manifest["items"])
        write_json(destination / "authorization.json", consent)
        write_json(destination / "manifest.json", ordered_manifest)
        write_json(destination / "retention.json", retention)
        write_json(destination / "queries.json", queries)
        vectors = {key: {} for key in [*ARMS, "gemini_queries", "voyage_queries"]}
        new_requests = 0
        for request in requests:
            key = request["key"]
            if key not in cached:
                if max_new_requests is not None and new_requests >= max_new_requests:
                    break
                attempt_id = aliases[key]
                ledger.reserve(attempt_id, request["reserved_usd"])
                started = time.monotonic()
                try:
                    response = clients[request["provider"]].embed(
                        image_bytes=images[request["id"]] if request["kind"] == "document" else None,
                        mime_type="image/png" if request["kind"] == "document" else None,
                        text=request["text"], task_type=request["task"])
                    if len(response["vector"]) != request["dimensions"]:
                        raise ValueError("provider returned wrong dimension")
                    vector = unit_prefix(response["vector"], request["dimensions"])
                    receipt = {"key": key, "provider": request["provider"], "model": request["model"],
                        "vector": vector, "vector_sha256": digest(json_bytes(vector)),
                        "usage": response.get("usage", {}), "latency_seconds": round(time.monotonic() - started, 3)}
                    write_json(cache_dir / f"{key}.json", receipt)
                    ledger.finish(attempt_id, "completed", response.get("usage", {}))
                    cached[key] = vector
                    new_requests += 1
                except Exception as exc:
                    ledger.finish(attempt_id, "failed_or_uncertain")
                    if isinstance(exc, ProviderError):
                        attempt = next(a for a in ledger.data["attempts"] if a["key"] == attempt_id)
                        attempt["http_status"] = exc.http_status
                        attempt["provider"] = exc.provider
                        for field in ("retry_after_seconds", "quota_exhausted", "quota_period", "provider_status"):
                            value = getattr(exc, field, None)
                            if value is not None:
                                attempt[field] = value
                        write_json(ledger.path, ledger.data)
                    raise
                if progress:
                    progress({"provider": request["provider"], "completed_unique_requests": len(set(cached) & set(unique)),
                        "planned_unique_requests": len(unique)})
                sleep(request_interval_seconds)
        for request in all_requests:
            key = request["key"]
            if key not in cached:
                continue
            target = request["arm"] if request["kind"] == "document" else request["provider"] + "_queries"
            vectors[target][request["id"]] = cached[key]
        write_json(destination / "vectors.json", vectors)
        result = evaluate_comparison(ordered_manifest, vectors, queries, retention, evaluation_arms=manifest.get("evaluation_arms"))
        result["budget"] = {"attempts": len(ledger.data["attempts"]),
            "reserved_upper_bound_usd": sum(a["reserved_usd"] for a in ledger.data["attempts"]),
            "actual_invoice_usd": None, "voyage_free_balance_verified": False}
        write_json(destination / "evaluation.json", result)
        results_path = source / "comparison-results-v1.html"
        results_path.write_text(render_results(ordered_manifest, retention,
            result["evaluations"], result["similarity_groups"]), encoding="utf-8")
        return {"status": result["status"], "budget": result["budget"],
            "new_requests_this_invocation": new_requests,
            "active": len(retention["active_ids"]), "archived": len(retention["archived"]),
            "groups": len(result["similarity_groups"]), "results_path": str(results_path)}
