"""Bounded Voyage-only incremental image embedding; no automatic human decisions."""
from __future__ import annotations

import math
import time
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from .comparison import request_key
from .experiment import digest, json_bytes, now, read_json, run_lock, run_path, safe_source, write_json
from .providers import ProviderError, load_credentials
from .voyage_provider import VOYAGE_MODEL, VOYAGE_URL, _content_part_for_image, _request_json

SCHEMA = "image-incremental-embedding-1"
CONSENT_SCHEMA = "image-incremental-embedding-consent-1"
RETRY_SCHEMA = "image-incremental-manual-retry-consent-1"
MAX_IMAGES = 300
MAX_USD = 0.10
DIMENSIONS = 1024
DESTINATION = "embedding-v1"


def _vector(values: Any) -> list[float]:
    if (not isinstance(values, list) or len(values) != DIMENSIONS
            or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in values)):
        raise ValueError("invalid 1024-dimensional vector")
    norm = math.sqrt(sum(float(x) ** 2 for x in values))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("invalid vector norm")
    return [float(x) / norm for x in values]


def parse_batch_response(payload: Any, count: int) -> tuple[list[list[float]], dict[str, int]]:
    """Require a one-to-one indexed standard response, never positional guesses."""
    if not isinstance(payload, dict) or payload.get("model") != VOYAGE_MODEL:
        raise ProviderError("voyage")
    rows = payload.get("data")
    if not isinstance(rows, list) or len(rows) != count:
        raise ProviderError("voyage")
    indexed = {}
    try:
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("invalid row")
            index = row.get("index")
            if type(index) is not int or not 0 <= index < count or index in indexed:
                raise ValueError("invalid batch index")
            indexed[index] = _vector(row.get("embedding"))
    except (ValueError, TypeError, OverflowError) as exc:
        raise ProviderError("voyage") from exc
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    safe_usage = {k: v for k, v in usage.items()
                  if k in {"text_tokens", "image_pixels", "total_tokens", "total_pixels", "video_pixels"}
                  and type(v) is int and v >= 0}
    return [indexed[i] for i in range(count)], safe_usage


class VoyageImageBatchClient:
    """Up to eight independent images in a standard request, not async Batch API."""
    def __init__(self, api_key: str, timeout: int = 90):
        if not str(api_key or "").strip():
            raise ValueError("Voyage key is required")
        self._api_key, self.timeout = str(api_key).strip(), timeout

    def embed_images(self, images: list[bytes]) -> dict[str, Any]:
        if not 1 <= len(images) <= 8 or any(not isinstance(b, bytes) or len(b) > 20_000_000 for b in images):
            raise ValueError("invalid bounded image batch")
        return _request_json(method="POST", url=VOYAGE_URL,
            headers={"Authorization": f"Bearer {self._api_key}"}, timeout=self.timeout,
            payload={"inputs": [{"content": [_content_part_for_image(b, "image/png")]} for b in images],
                     "model": VOYAGE_MODEL, "input_type": "document", "output_dimension": DIMENSIONS,
                     "output_encoding": None, "truncation": False})


def _receipt_vector(receipt: dict, request: dict) -> list[float]:
    if (not isinstance(receipt, dict) or receipt.get("key") != request["key"]
            or receipt.get("provider") != "voyage" or receipt.get("model") != VOYAGE_MODEL
            or receipt.get("vector_sha256") != digest(json_bytes(receipt.get("vector")))):
        raise ValueError("vector cache identity mismatch")
    values = receipt["vector"]
    _vector(values)
    if abs(math.sqrt(sum(float(v) ** 2 for v in values)) - 1.0) > 1e-3:
        raise ValueError("vector cache is not normalized")
    return values


def _load(root: Path, run_id: str) -> dict:
    from .incremental import validate_incremental_prepared
    from .dataset import _safe_input_path

    root = Path(root).resolve()
    source = run_path(root, run_id)
    manifest, bindings = validate_incremental_prepared(root, run_id)
    prepared = read_json(source / "prepared.json")
    if (manifest.get("schema_version") != "image-incremental-manifest-1"
            or manifest.get("run_id") != run_id or prepared.get("complete") is not True
            or prepared.get("manifest_sha256") != digest(json_bytes(manifest))
            or manifest.get("source_bindings_sha256") != digest(json_bytes(bindings))
            or prepared.get("source_bindings_sha256") != digest(json_bytes(bindings))):
        raise ValueError("incremental preparation identity mismatch")
    bound = {}
    for row in bindings.get("files", []):
        relative = Path(row["path"])
        path = (root / relative).resolve()
        if relative.is_absolute() or not _safe_input_path(root, path) or not path.is_file():
            raise ValueError("unsafe bound source")
        if digest(path.read_bytes()) != row.get("sha256"):
            raise ValueError("bound source changed")
        bound[path.resolve()] = row["sha256"]
    items = manifest.get("items", [])
    ids = [r.get("id") for r in items]
    if (not 1 <= len(items) <= MAX_IMAGES or len(ids) != len(set(ids))
            or any(not isinstance(i, str) or not i for i in ids)
            or set(ids) & set(bindings.get("reference_ids", []))
            or any(r.get("lane") != "legacy" or not str(r.get("style_id", "")).startswith("CASE-") for r in items)):
        raise ValueError("incremental sample must be new bounded CASE legacy records")
    chosen = manifest.get("embedding_item_ids")
    if (not isinstance(chosen, list) or len(chosen) != len(set(chosen)) or not set(chosen) <= set(ids)):
        raise ValueError("invalid new-only embedding ids")
    requests, images = {}, {}
    for item in items:
        original = safe_source(root, item["path"])
        if digest(original.read_bytes()) != item.get("sha256"):
            raise ValueError("new original source changed")
        relative = Path(item["prepared_path"])
        path = (source / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to((source / "inputs").resolve()):
            raise ValueError("prepared image escapes inputs")
        blob = path.read_bytes()
        if len(blob) > 20_000_000 or digest(blob) != item.get("prepared_sha256"):
            raise ValueError("prepared image changed or exceeds limit")
        with Image.open(BytesIO(blob)) as image:
            if image.format != "PNG" or max(image.size) > 768 or min(image.size) <= 0 or getattr(image, "n_frames", 1) != 1:
                raise ValueError("unexpected incremental image preprocessing")
            pixels = max(50_000, image.width * image.height)
            image.verify()
        if item["id"] not in chosen:
            continue
        request = {"provider": "voyage", "model": VOYAGE_MODEL, "dimensions": DIMENSIONS,
                   "image_sha256": item["prepared_sha256"], "text": "", "task": "RETRIEVAL_DOCUMENT",
                   "id": item["id"], "arm": "voyage_image", "kind": "document", "pixels": pixels,
                   "reserved_usd": pixels * .60 / 1_000_000_000 + 256 * .12 / 1_000_000}
        request["key"] = request_key(request)
        requests[item["id"]] = request
        images[item["id"]] = blob
    unique = {requests[i]["key"]: requests[i] for i in chosen}
    return {"root": root, "source": source, "destination": source / DESTINATION,
            "manifest": manifest, "bindings": bindings, "bound": bound, "prepared": prepared,
            "manifest_sha256": digest(json_bytes(manifest)), "source_bindings_sha256": digest(json_bytes(bindings)),
            "requests": requests, "unique": unique, "images": images, "chosen": chosen}


def _retry_evidence(data: dict, retry: dict) -> None:
    from .dataset import _safe_input_path

    expected = {"schema_version", "approved", "approved_by", "run_id", "manifest_sha256", "source_bindings_sha256",
                "failed_ledger_sha256", "failed_request_key", "max_retry_count", "max_retry_images", "max_cost_usd",
                "investigation_evidence_path", "investigation_evidence_sha256", "reason_code", "charge_both_attempts"}
    if (not isinstance(retry, dict) or set(retry) != expected or retry.get("schema_version") != RETRY_SCHEMA
            or retry.get("approved") is not True or retry.get("charge_both_attempts") is not True
            or not isinstance(retry.get("approved_by"), str) or not 1 <= len(retry["approved_by"].strip()) <= 200
            or retry.get("run_id") != data["manifest"]["run_id"]
            or retry.get("manifest_sha256") != data["manifest_sha256"]
            or retry.get("source_bindings_sha256") != data["source_bindings_sha256"]
            or retry.get("failed_request_key") not in data["unique"]
            or type(retry.get("max_retry_count")) is not int or retry["max_retry_count"] != 1
            or type(retry.get("max_retry_images")) is not int or retry["max_retry_images"] != 1
            or type(retry.get("max_cost_usd")) not in (int, float)
            or not math.isfinite(retry["max_cost_usd"]) or not 0 < retry["max_cost_usd"] <= MAX_USD
            or retry.get("reason_code") != "manual_network_permission_investigated"):
        raise ValueError("invalid explicit manual retry consent")
    relative = Path(str(retry.get("investigation_evidence_path", "")))
    path = (data["root"] / relative).resolve()
    if (relative.is_absolute() or not _safe_input_path(data["root"], path) or path.suffix != ".json"
            or not path.is_file() or digest(path.read_bytes()) != retry.get("investigation_evidence_sha256")):
        raise ValueError("manual retry investigation evidence changed")


def _retry_history(data: dict, ledger: dict) -> dict | None:
    records = ledger.get("retry_authorizations", [])
    if not isinstance(records, list) or len(records) > 1:
        raise ValueError("only one manual retry authorization is permitted")
    if not records:
        return None
    record = records[0]
    retry = record.get("consent")
    _retry_evidence(data, retry)
    if record.get("consent_sha256") != digest(json_bytes(retry)):
        raise ValueError("manual retry authorization identity mismatch")
    path = (data["root"] / str(record.get("failed_ledger_archive_path", ""))).resolve()
    if not path.is_relative_to((data["destination"] / "manual-retry-evidence").resolve()) or not path.is_file():
        raise ValueError("failed ledger archive is missing")
    raw = path.read_bytes()
    if digest(raw) != retry["failed_ledger_sha256"]:
        raise ValueError("failed ledger archive changed")
    old = read_json(path)
    if (old.get("retry_authorizations") or not isinstance(old.get("attempts"), list) or not old["attempts"]
            or old["attempts"] != ledger["attempts"][:len(old["attempts"])]
            or old.get("manifest_sha256") != data["manifest_sha256"]
            or old.get("source_bindings_sha256") != data["source_bindings_sha256"]
            or not any(a.get("key") == retry["failed_request_key"] and a.get("status") == "failed_or_uncertain"
                       for a in old["attempts"])):
        raise ValueError("failed reservation history was changed")
    return retry


def _validate_retry(data: dict, ledger: dict, cache: dict, retry: dict, maximum_usd: float) -> str:
    _retry_evidence(data, retry)
    key = retry["failed_request_key"]
    if maximum_usd > retry["max_cost_usd"] or key in cache:
        raise ValueError("manual retry is outside budget or already completed")
    history = _retry_history(data, ledger)
    if history is not None:
        if history != retry:
            raise ValueError("manual retry authorization was already used")
    elif digest((data["destination"] / "budget.json").read_bytes()) != retry["failed_ledger_sha256"]:
        raise ValueError("manual retry consent binds a different failed ledger")
    previous = [a for a in ledger["attempts"] if str(a["key"]).split(":", 1)[0] == key]
    if len(previous) != 1 or previous[0]["key"] != key or previous[0]["status"] != "failed_or_uncertain":
        raise ValueError("only one explicit retry of a failed original request is permitted")
    unresolved = {str(a["key"]).split(":", 1)[0] for a in ledger["attempts"]
                  if str(a["key"]).split(":", 1)[0] not in cache}
    if unresolved != {key}:
        raise ValueError("manual single-image retry cannot bypass other uncertain requests")
    return key


def _state(data: dict, *, allow_uncertain: bool = False) -> tuple[dict, dict, dict]:
    dest, unique = data["destination"], data["unique"]
    header = {"schema_version": SCHEMA, "manifest_sha256": data["manifest_sha256"],
              "source_bindings_sha256": data["source_bindings_sha256"]}
    ledger = read_json(dest / "budget.json") if (dest / "budget.json").exists() else {**header, "attempts": []}
    if any(ledger.get(k) != v for k, v in header.items()) or not isinstance(ledger.get("attempts"), list):
        raise ValueError("incremental ledger identity mismatch")
    retry = _retry_history(data, ledger)
    attempted, content_attempts = {}, {}
    for attempt in ledger["attempts"]:
        attempt_key = attempt.get("key")
        key = str(attempt_key).split(":", 1)[0]
        allowed = {key}
        if retry is not None and retry["failed_request_key"] == key:
            allowed.add(key + ":manual-retry-1")
        if (key not in unique or attempt_key not in allowed or attempt_key in attempted
                or attempt.get("status") not in {"reserved", "completed", "failed_or_uncertain"}
                or type(attempt.get("reserved_usd")) not in (int, float)
                or not math.isfinite(attempt["reserved_usd"])
                or abs(attempt["reserved_usd"] - unique[key]["reserved_usd"]) > 1e-12):
            raise ValueError("invalid incremental reservation")
        attempted[attempt_key] = attempt
        content_attempts.setdefault(key, []).append(attempt_key)
    receipts = {}
    # A full validated batch receipt allows local completion after a crash without resending.
    for path in sorted((dest / "batch-receipts").glob("*.json")):
        batch = read_json(path)
        if (batch.get("status") != "completed" or any(batch.get(k) != v for k, v in header.items())
                or not isinstance(batch.get("receipts"), list) or not 1 <= len(batch["receipts"]) <= 8):
            raise ValueError("invalid batch checkpoint")
        batch_keys = [r.get("key") for r in batch["receipts"]]
        if len(batch_keys) != len(set(batch_keys)) or batch.get("keys") != batch_keys:
            raise ValueError("batch checkpoint index mismatch")
        for receipt in batch["receipts"]:
            key = receipt["key"]
            attempt_key = receipt.get("attempt_key", key)
            if (attempt_key not in attempted or str(attempt_key).split(":", 1)[0] != key or key in receipts
                    or (len(content_attempts.get(key, [])) > 1 and attempt_key == key)):
                raise ValueError("batch checkpoint has no unique reservation")
            _receipt_vector(receipt, unique[key])
            receipts[key] = receipt
    cache = {}
    if any(path.stem not in unique for path in (dest / "vector-cache").glob("*.json")):
        raise ValueError("incremental cache contains unrelated content keys")
    for key, request in unique.items():
        path = dest / "vector-cache" / (key + ".json")
        if path.exists():
            receipt = read_json(path)
            _receipt_vector(receipt, request)
            if key not in content_attempts or key not in receipts or receipt != receipts[key]:
                raise ValueError("incremental cache lacks full batch checkpoint")
            cache[key] = receipt
        elif key in receipts:
            cache[key] = receipts[key]
    # Reuse only pinned existing key caches with an observed completed reservation.
    reference = run_path(data["root"], data["manifest"]["reference_run_id"]) / "comparison-v1"
    old_budget_path = reference / "budget.json"
    old_attempts = read_json(old_budget_path).get("attempts", []) if old_budget_path.resolve() in data["bound"] else []
    completed = {str(a.get("key", "")).split(":", 1)[0] for a in old_attempts if a.get("status") == "completed"}
    for key, request in unique.items():
        if key in cache:
            continue
        path = reference / "vector-cache" / (key + ".json")
        if path.resolve() in data["bound"]:
            if key not in completed:
                raise ValueError("parent cache lacks completed reservation")
            receipt = read_json(path)
            _receipt_vector(receipt, request)
            cache[key] = receipt
    if not allow_uncertain and any(key not in receipts for key in content_attempts):
        raise ValueError("earlier attempt is uncertain; manual investigation required, no retry")
    return ledger, cache, receipts


def _limits(maximum_usd: float, max_new_images: int | None, batch_size: int, interval: float) -> None:
    if type(maximum_usd) not in (int, float) or not math.isfinite(maximum_usd) or not 0 < maximum_usd <= MAX_USD:
        raise ValueError("budget must be >0 and <=0.10 USD")
    if max_new_images is not None and (type(max_new_images) is not int or not 1 <= max_new_images <= MAX_IMAGES):
        raise ValueError("max-new-images must be 1..300")
    if type(batch_size) is not int or not 1 <= batch_size <= 8:
        raise ValueError("batch-size must be 1..8")
    if type(interval) not in (int, float) or not math.isfinite(interval) or not 3.1 <= interval <= 60:
        raise ValueError("interval must be 3.1..60 seconds")


def _consent_template(data: dict, maximum_usd: float) -> dict:
    return {"schema_version": CONSENT_SCHEMA, "approved": False, "external_ai_approved": False,
            "approved_by": "", "run_id": data["manifest"]["run_id"],
            "manifest_sha256": data["manifest_sha256"], "source_bindings_sha256": data["source_bindings_sha256"],
            "scope": "max300_unsampled_CASE_legacy_new_only", "provider": "voyage", "model": VOYAGE_MODEL,
            "dimensions": DIMENSIONS, "embedding_item_ids_sha256": digest(json_bytes(data["chosen"])),
            "request_keys_sha256": digest(json_bytes(sorted(data["unique"]))),
            "max_images": len(data["unique"]), "max_cost_usd": maximum_usd}


def _validate_consent(data: dict, consent: dict, maximum_usd: float) -> None:
    expected = _consent_template(data, maximum_usd)
    if not isinstance(consent, dict) or set(consent) != set(expected):
        raise ValueError("invalid consent fields")
    fixed = set(expected) - {"approved", "external_ai_approved", "approved_by", "max_cost_usd"}
    if (any(consent.get(k) != expected[k] for k in fixed) or consent.get("approved") is not True
            or consent.get("external_ai_approved") is not True or not isinstance(consent.get("approved_by"), str)
            or not consent["approved_by"].strip() or len(consent["approved_by"]) > 200
            or type(consent.get("max_cost_usd")) not in (int, float)
            or not math.isfinite(consent["max_cost_usd"]) or not maximum_usd <= consent["max_cost_usd"] <= MAX_USD):
        raise ValueError("consent does not approve this exact incremental experiment")


def plan_incremental_embedding(root: Path, run_id: str, *, maximum_usd: float = MAX_USD,
                               max_new_images: int | None = None, batch_size: int = 8,
                               interval: float = 3.1, retry_consent: dict | None = None) -> dict:
    _limits(maximum_usd, max_new_images, batch_size, interval)
    data = _load(root, run_id)
    ledger, cache, _ = _state(data, allow_uncertain=retry_consent is not None)
    pending = [r for k, r in data["unique"].items() if k not in cache]
    if retry_consent is not None:
        if max_new_images != 1:
            raise ValueError("explicit manual retry requires --max-new-images 1")
        key = _validate_retry(data, ledger, cache, retry_consent, maximum_usd)
        selected = [data["unique"][key]]
    else:
        selected = pending[:max_new_images] if max_new_images is not None else pending
    prior = sum(a["reserved_usd"] for a in ledger["attempts"])
    reserve = prior + sum(r["reserved_usd"] for r in selected)
    return {"status": "dry_run", "network_calls": 0, "writes": 0, "run_id": run_id,
            "new_image_ids": len(data["chosen"]), "unique_image_inputs": len(data["unique"]),
            "reusable_cached_inputs": len(cache), "uncached_images": len(pending), "selected_new_images": len(selected),
            "maximum_usd": maximum_usd, "reserved_upper_bound_usd": round(reserve, 10),
            "all_remaining_reserved_upper_bound_usd": round(prior + sum(r["reserved_usd"] for r in pending), 10),
            "within_cap": reserve <= maximum_usd + 1e-12, "actual_invoice_usd": None,
            "free_balance_verified": False, "automatic_retry": False,
            "manual_retry_requested": retry_consent is not None,
            "batch_size": batch_size, "first_request_images": 1, "query_calls": 0, "metadata_calls": 0,
            "consent_template": _consent_template(data, maximum_usd),
            "vectors_path": str(data["destination"] / "vectors.json")}


def execute_incremental_embedding(root: Path, run_id: str, consent: dict, *, maximum_usd: float,
                                  max_new_images: int | None = None, batch_size: int = 8, interval: float = 3.1,
                                  client=None, sleep=time.sleep, progress=None, retry_consent: dict | None = None) -> dict:
    _limits(maximum_usd, max_new_images, batch_size, interval)
    data = _load(root, run_id)
    _validate_consent(data, consent, maximum_usd)
    destination = data["destination"]
    with run_lock(data["source"]):
        ledger, cache, recovered = _state(data, allow_uncertain=retry_consent is not None)
        pending = [r for k, r in data["unique"].items() if k not in cache]
        retry_key = None
        if retry_consent is not None:
            if max_new_images != 1:
                raise ValueError("explicit manual retry requires --max-new-images 1")
            retry_key = _validate_retry(data, ledger, cache, retry_consent, maximum_usd)
            selected = [data["unique"][retry_key]]
        else:
            selected = pending[:max_new_images] if max_new_images is not None else pending
        reserve = sum(a["reserved_usd"] for a in ledger["attempts"]) + sum(r["reserved_usd"] for r in selected)
        if reserve > maximum_usd + 1e-12:
            raise ValueError("selected experiment exceeds approved reservation budget")
        if selected and client is None:
            credentials = load_credentials([data["root"] / ".env"])
            client = VoyageImageBatchClient(credentials.get("VOYAGE_API_KEY", ""))
        destination.mkdir(exist_ok=True)
        (destination / "vector-cache").mkdir(exist_ok=True)
        (destination / "batch-receipts").mkdir(exist_ok=True)
        if retry_consent is not None and not ledger.get("retry_authorizations"):
            raw = (destination / "budget.json").read_bytes()
            archive = destination / "manual-retry-evidence" / ("failed-ledger-" + digest(raw)[:16] + ".json")
            archive.parent.mkdir(exist_ok=True)
            if archive.exists() and archive.read_bytes() != raw:
                raise ValueError("failed ledger archive collision")
            if not archive.exists():
                archive.write_bytes(raw)
            ledger["retry_authorizations"] = [{"consent": retry_consent,
                "consent_sha256": digest(json_bytes(retry_consent)),
                "failed_ledger_archive_path": archive.relative_to(data["root"]).as_posix()}]
        write_json(destination / "authorization.json", consent)
        for key, receipt in recovered.items():
            write_json(destination / "vector-cache" / (key + ".json"), receipt)
            next(a for a in ledger["attempts"] if a["key"] == receipt.get("attempt_key", key))["status"] = "completed"
        write_json(destination / "budget.json", ledger)
        completed_now, calls = 0, 0

        def checkpoint(*, failed: bool = False) -> dict:
            vectors = {i: cache[r["key"]]["vector"] for i, r in data["requests"].items() if r["key"] in cache}
            write_json(destination / "vectors.json", {"voyage_image": vectors})
            result = {"schema_version": SCHEMA, "run_id": run_id, "provider": "voyage", "model": VOYAGE_MODEL,
                      "manifest_sha256": data["manifest_sha256"], "source_bindings_sha256": data["source_bindings_sha256"],
                      "status": "failed_or_uncertain" if failed else (
                          "completed" if len(vectors) == len(data["chosen"]) else "partial"),
                      "completed_image_ids": len(vectors), "target_image_ids": len(data["chosen"]),
                      "new_images_this_invocation": completed_now, "standard_requests_this_invocation": calls,
                      "reserved_upper_bound_usd": round(sum(a["reserved_usd"] for a in ledger["attempts"]), 10),
                      "actual_invoice_usd": None, "free_balance_verified": False, "automatic_retry": False,
                      "manual_retry_authorizations": len(ledger.get("retry_authorizations", [])),
                      "human_group_approval": False, "metadata_calls": 0, "query_calls": 0,
                      "vectors_path": (destination / "vectors.json").relative_to(data["root"]).as_posix(), "at": now()}
            write_json(destination / "execution-receipt.json", result)
            return {**result, "vectors_path": str(destination / "vectors.json")}

        checkpoint()
        while selected:
            count = 1 if not ledger["attempts"] else batch_size
            batch, selected = selected[:count], selected[count:]
            keys = [r["key"] for r in batch]
            attempt_keys = [key + ":manual-retry-1" if key == retry_key else key for key in keys]
            for r, attempt_key in zip(batch, attempt_keys):
                ledger["attempts"].append({"key": attempt_key, "reserved_usd": r["reserved_usd"],
                                           "status": "reserved", "at": now()})
            # Persist every content-key reservation before transmitting any image in the batch.
            write_json(destination / "budget.json", ledger)
            started = time.monotonic()
            calls += 1
            try:
                response = client.embed_images([data["images"][r["id"]] for r in batch])
                vectors, usage = parse_batch_response(response, len(batch))
                receipts = [{"key": r["key"], "attempt_key": attempt_key, "provider": "voyage", "model": VOYAGE_MODEL,
                             "vector": v, "vector_sha256": digest(json_bytes(v)),
                             "usage": {}, "usage_scope": "see_full_batch_receipt",
                             "latency_seconds": round(time.monotonic() - started, 3)} for r, v, attempt_key in zip(batch, vectors, attempt_keys)]
                batch_receipt = {"schema_version": SCHEMA, "manifest_sha256": data["manifest_sha256"],
                                 "source_bindings_sha256": data["source_bindings_sha256"], "status": "completed",
                                 "keys": keys, "receipts": receipts, "usage": usage, "at": now()}
                write_json(destination / "batch-receipts" / (digest(json_bytes(keys)) + ".json"), batch_receipt)
                for receipt in receipts:
                    key = receipt["key"]
                    write_json(destination / "vector-cache" / (key + ".json"), receipt)
                    cache[key] = receipt
                    next(a for a in ledger["attempts"] if a["key"] == receipt["attempt_key"])["status"] = "completed"
                write_json(destination / "budget.json", ledger)
                completed_now += len(batch)
                checkpoint()
            except Exception as exc:
                for attempt in ledger["attempts"]:
                    if attempt["key"] in attempt_keys:
                        attempt["status"] = "failed_or_uncertain"
                        if isinstance(exc, ProviderError):
                            attempt["error"] = exc.to_dict()
                write_json(destination / "budget.json", ledger)
                checkpoint(failed=True)
                if isinstance(exc, ProviderError):
                    raise
                raise ProviderError("voyage") from exc
            if progress:
                progress({"stage": "voyage_images", "new_images_completed": completed_now,
                          "standard_requests": calls, "remaining_selected_images": len(selected)})
            if selected:
                sleep(interval)
        return checkpoint()
