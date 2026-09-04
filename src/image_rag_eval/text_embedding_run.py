"""Explicitly authorized Voyage text execution with immutable evidence and budget."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

MODEL, DIMENSION, TOKEN_CAP = "voyage-4-lite", 512, 260000
MANIFEST_SCHEMA, RUN_SCHEMA = "image-text-embedding-inputs-1", "image-text-embedding-run-1"
TOKENIZER_SHA256 = "c0382117ea329cdf097041132f6d735924b697924d6f6fc3945713e96ce87539"
URL = "https://api.voyageai.com/v1/embeddings"
PREFIXES = {"document": "Represent the document for retrieval: ", "query": "Represent the query for retrieving supporting documents: "}


class TextRunError(ValueError):
    """Messages are fixed categories, never provider responses or credentials."""

    def __init__(self, message, *, http_status=None, retry_after_seconds=None):
        super().__init__(message)
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        self.retry_after_seconds = retry_after_seconds if type(retry_after_seconds) is int and 0 <= retry_after_seconds <= 86400 else None


def _bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _int(value, *, positive=False):
    if type(value) is not int or value < int(positive):
        raise TextRunError("invalid_integer")
    return value


def _pairs(rows):
    value = {}
    for key, item in rows:
        if key in value:
            raise TextRunError("duplicate_json_key")
        value[key] = item
    return value


def _json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(TextRunError("nonfinite_json")))
    except (UnicodeError, ValueError) as exc:
        raise TextRunError("invalid_json") from None


def _read(path: Path) -> tuple[dict, bytes]:
    if path.is_symlink() or path.is_junction() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise TextRunError("unsafe_or_oversized_input")
    raw = path.read_bytes()
    return _json(raw), raw


def _private(path: Path, archive_root: Path) -> Path:
    path, root = Path(path).absolute(), Path(archive_root).resolve() / "data/private-research"
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise TextRunError("private_path_required")
    for current in (path, *path.parents):
        if current.is_symlink() or current.is_junction():
            raise TextRunError("symlink_or_junction_forbidden")
    return resolved


def _immutable(path: Path, raw: bytes, archive_root: Path):
    path = _private(path, archive_root)
    if path.exists():
        if path.read_bytes() != raw:
            raise TextRunError("immutable_artifact_mismatch")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".partial-")
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)  # Atomic, fails if destination exists; never replaces.
        except FileExistsError:
            if path.read_bytes() != raw:
                raise TextRunError("immutable_artifact_mismatch") from None
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _lock(run_dir: Path, archive_root: Path):
    lock = _private(run_dir / ".execute.lock", archive_root)
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        handle = lock.open("xb")
    except FileExistsError:
        raise TextRunError("run_locked_manual_audit_required") from None
    try:
        handle.write(_bytes({"pid": os.getpid(), "schema_version": RUN_SCHEMA}))
        handle.flush()
        os.fsync(handle.fileno())
        yield
    finally:
        handle.close()
        lock.unlink()


def _manifest(path: Path):
    manifest, raw = _read(path)
    required = {"schema_version", "model", "dimension", "documents", "total_token_cap"}
    if (not isinstance(manifest, dict) or set(manifest) != required or manifest["schema_version"] != MANIFEST_SCHEMA
            or manifest["model"] != MODEL or type(manifest["dimension"]) is not int or manifest["dimension"] != DIMENSION
            or type(manifest["total_token_cap"]) is not int or manifest["total_token_cap"] != TOKEN_CAP
            or not isinstance(manifest["documents"], list) or not 1 <= len(manifest["documents"]) <= 1000):
        raise TextRunError("unsupported_manifest_contract")
    seen = set()
    for document in manifest["documents"]:
        if (not isinstance(document, dict) or set(document) - {"input_id", "item_id", "text", "input_type"}
                or not {"input_id", "text", "input_type"} <= set(document)
                or not isinstance(document["input_id"], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", document["input_id"])
                or document["input_id"] in seen or not isinstance(document["text"], str) or not document["text"].strip()
                or len(document["text"].encode("utf-8")) > 200000
                or not isinstance(document["input_type"], str) or document["input_type"] not in PREFIXES
                or ("item_id" in document and not isinstance(document["item_id"], str))):
            raise TextRunError("invalid_manifest_document")
        seen.add(document["input_id"])
    return manifest, raw, _sha(raw)


def _identity(document: dict) -> dict:
    return {"model": MODEL, "dimension": DIMENSION, "input_type": document["input_type"], "text": document["text"]}


def cache_key(document: dict) -> str:
    return _sha(_bytes(_identity(document)))


def estimate_tokens(text: str, input_type: str, tokenizer) -> dict:
    body = len(tokenizer.encode(text, add_special_tokens=False).ids)
    prefixed = len(tokenizer.encode(PREFIXES[input_type] + text, add_special_tokens=False).ids)
    return {"body_tokens": body, "prefixed_tokens": prefixed, "reserved_tokens": math.ceil(prefixed * 1.02) + 8}


def _vector(vector):
    if (not isinstance(vector, list) or len(vector) != DIMENSION
            or any(type(value) not in (int, float) or not math.isfinite(value) for value in vector)
            or not math.isfinite(sum(value * value for value in vector)) or not any(vector)):
        raise TextRunError("invalid_vector")
    return [float(value) for value in vector]


def validate_response(response, expected_count: int) -> dict:
    if (not isinstance(response, dict) or ("model" in response and response["model"] != MODEL)
            or response.get("truncated") is True or response.get("truncation") is True
            or not isinstance(response.get("data"), list) or len(response["data"]) != expected_count
            or not isinstance(response.get("usage"), dict)):
        raise TextRunError("invalid_response_envelope")
    total = _int(response["usage"].get("total_tokens"), positive=True)
    rows = {}
    for row in response["data"]:
        if not isinstance(row, dict):
            raise TextRunError("invalid_response_row")
        index = _int(row.get("index"))
        if index in rows or index >= expected_count:
            raise TextRunError("invalid_response_index")
        rows[index] = {"index": index, "embedding": _vector(row.get("embedding"))}
    return {"model_reported": response.get("model"), "model_reported_status": "observed" if "model" in response else "absent_unknown",
            "data": [rows[index] for index in range(expected_count)], "usage": {"total_tokens": total}}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise TextRunError("redirect_refused")


def _post(payload: dict, api_key: str) -> dict:
    request = Request(URL, data=_bytes(payload), method="POST", headers={"Authorization": "Bearer " + api_key,
        "Content-Type": "application/json", "Accept": "application/json", "User-Agent": RUN_SCHEMA})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=60) as response:
            if response.geturl() != URL:
                raise TextRunError("unexpected_response_url")
            raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise TextRunError("response_too_large")
            return _json(raw)
    except HTTPError as exc:
        retry = exc.headers.get("Retry-After", "") if exc.headers else ""
        seconds = int(retry) if re.fullmatch(r"[0-9]{1,5}", retry) else None
        raise TextRunError("http_failure", http_status=exc.code, retry_after_seconds=seconds) from None
    except Exception:
        raise TextRunError("transport_or_response_failure") from None


def _credential(path: Path, archive_root: Path) -> str:
    path, root = Path(path).absolute(), Path(archive_root).resolve()
    if (path.name != ".env" or path.parent.resolve() not in (root, root.parent)
            or path.is_symlink() or path.is_junction()):
        raise TextRunError("credential_path_refused")
    values = []
    try:
        with path.open(encoding="utf-8-sig") as handle:
            for line in handle:
                match = re.fullmatch(r"\s*(?:export\s+)?VOYAGE_API_KEY\s*=\s*(.*?)\s*", line.rstrip("\r\n"))
                if match:
                    value = match[1]
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    values.append(value)
    except OSError:
        raise TextRunError("credential_unavailable") from None
    if len(values) != 1 or not values[0] or any(character.isspace() for character in values[0]):
        raise TextRunError("credential_missing_or_ambiguous")
    return values[0]


def _state(run_dir: Path, archive_root: Path) -> dict:
    state = {"events": [], "requests": {}, "completed": {}, "uncertain": set(), "actual_tokens": 0,
             "charged_tokens": 0, "reservation_exceeded": False}
    previous = None
    events_dir = _private(run_dir / "events", archive_root)
    for sequence, path in enumerate(sorted(events_dir.glob("*.json")), 1):
        event, raw = _read(_private(path, archive_root))
        digest = _sha(raw)
        if (path.name != f"{sequence:06d}-{digest}.json" or event.get("sequence") != sequence
                or event.get("previous_sha256") != previous or event.get("type") not in ("reserved", "completed", "uncertain")):
            raise TextRunError("ledger_integrity_failure")
        ident = event["request_id"]
        if event["type"] == "reserved":
            if ident in state["requests"]:
                raise TextRunError("duplicate_reservation")
            _int(event["reserved_tokens"], positive=True)
            request, request_raw = _read(_private(run_dir / "requests" / (ident + ".json"), archive_root))
            if _sha(request_raw) != event["request_sha256"] or request["request_id"] != ident:
                raise TextRunError("request_integrity_failure")
            state["requests"][ident] = event
        else:
            if ident not in state["requests"] or ident in state["completed"] or ident in state["uncertain"]:
                raise TextRunError("invalid_ledger_transition")
            if event["type"] == "completed":
                _int(event["actual_tokens"], positive=True)
                state["completed"][ident] = event
            else:
                state["uncertain"].add(ident)
        state["events"].append(event)
        previous = digest
    for ident, reservation in state["requests"].items():
        actual = state["completed"].get(ident, {}).get("actual_tokens", 0)
        state["actual_tokens"] += actual
        state["charged_tokens"] += max(reservation["reserved_tokens"], actual)
        state["reservation_exceeded"] |= actual > reservation["reserved_tokens"]
    state["pending"] = set(state["requests"]) - set(state["completed"])
    # Losing a tail reservation must not silently restore the consumed budget.
    # A crash after a request file but before reservation also needs manual audit.
    for folder in ("requests", "responses"):
        for path in _private(run_dir / folder, archive_root).glob("*.json"):
            if path.stem not in state["requests"]:
                raise TextRunError("orphan_request_or_response_manual_audit_required")
    for path in _private(run_dir / "cache", archive_root).glob("*.json"):
        value, _ = _read(_private(path, archive_root))
        if not isinstance(value, dict) or value.get("request_id") not in state["requests"]:
            raise TextRunError("orphan_cache_manual_audit_required")
    state["previous_sha256"] = previous
    return state


def _event(run_dir, state, event, archive_root):
    event = {**event, "sequence": len(state["events"]) + 1, "previous_sha256": state["previous_sha256"],
             "observed_at_utc": datetime.now(timezone.utc).isoformat()}
    raw = _bytes(event)
    _immutable(run_dir / "events" / f"{event['sequence']:06d}-{_sha(raw)}.json", raw, archive_root)


def _cache(document, run_dir, state, archive_root):
    key = cache_key(document)
    path = _private(run_dir / "cache" / (key + ".json"), archive_root)
    if not path.exists():
        if any(key in event["cache_sha256"] for event in state["completed"].values()):
            raise TextRunError("completed_cache_missing")
        return None
    value, raw = _read(path)
    completion = state["completed"].get(value.get("request_id"))
    if (not completion or completion["cache_sha256"].get(key) != _sha(raw) or value.get("identity") != _identity(document)
            or value.get("cache_key") != key or value.get("vector_sha256") != _sha(_bytes(value.get("vector")))):
        raise TextRunError("cache_integrity_failure")
    response, response_raw = _read(_private(run_dir / "responses" / (value["request_id"] + ".json"), archive_root))
    if (_sha(response_raw) != completion["response_sha256"] or response["usage"]["total_tokens"] != completion["actual_tokens"]
            or response["data"][value["response_index"]]["embedding"] != value["vector"]):
        raise TextRunError("response_integrity_failure")
    return _vector(value["vector"])


def _summary(manifest, manifest_sha, run_dir, state, unique, estimates, archive_root):
    missing = [key for key, document in unique.items() if _cache(document, run_dir, state, archive_root) is None]
    return {"schema_version": RUN_SCHEMA, "manifest_sha256": manifest_sha, "requested_inputs": len(manifest["documents"]),
            "unique_inputs": len(unique), "cache_hits": len(unique) - len(missing), "missing_inputs": len(missing),
            "additional_reserved_tokens": sum(estimates[key]["reserved_tokens"] for key in missing),
            "actual_reported_tokens": state["actual_tokens"], "conservative_charged_tokens": state["charged_tokens"],
            "remaining_conservative_tokens": TOKEN_CAP - state["charged_tokens"], "total_token_cap": TOKEN_CAP,
            "pending_or_uncertain_requests": len(state["pending"]), "completed_requests": len(state["completed"]),
            "model": MODEL, "dimension": DIMENSION, "rerank_calls": 0, "release_eligible": False,
            "metadata_human_approved": False, "actual_billed_cost": None}


def execute_manifest(manifest_path: Path, tokenizer_path: Path, run_dir: Path, *, archive_root: Path,
                     apply=False, execute=False, batch_size=20, dotenv_path=None,
                     tokenizer=None, transport=None, api_key=None) -> dict:
    """Fixture injection is Python-only; CLI always pins tokenizer and real transport."""
    if execute != apply:
        raise TextRunError("apply_and_execute_required_together")
    if not 1 <= _int(batch_size, positive=True) <= 20:
        raise TextRunError("batch_size_limit")
    root, run_dir = Path(archive_root).resolve(), _private(run_dir, archive_root)
    manifest, manifest_raw, manifest_sha = _manifest(Path(manifest_path))
    tokenizer_path = Path(tokenizer_path)
    _, tokenizer_raw = _read(tokenizer_path)
    tokenizer_sha = _sha(tokenizer_raw)
    if tokenizer is None:
        if tokenizer_sha != TOKENIZER_SHA256:
            raise TextRunError("unpinned_tokenizer")
        from tokenizers import Tokenizer
        tokenizer = Tokenizer.from_file(str(tokenizer_path))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    unique = {cache_key(document): document for document in manifest["documents"]}
    estimates = {key: estimate_tokens(document["text"], document["input_type"], tokenizer) for key, document in unique.items()}
    if any(value["prefixed_tokens"] > 32000 for value in estimates.values()):
        raise TextRunError("document_token_limit")
    config = {"schema_version": RUN_SCHEMA, "model": MODEL, "dimension": DIMENSION, "total_token_cap": TOKEN_CAP,
              "tokenizer_sha256": tokenizer_sha, "reservation_formula": "ceil(prefixed_local_tokens*1.02)+8_per_input",
              "truncation": False, "output_dtype": "float", "rerank_enabled": False}
    config_path = _private(run_dir / "run-config.json", root)
    if config_path.exists() and _read(config_path)[0] != config:
        raise TextRunError("frozen_run_config_mismatch")
    if not config_path.exists() and run_dir.exists() and any(run_dir.iterdir()):
        raise TextRunError("unconfigured_nonempty_run_directory")
    state = _state(run_dir, root)
    if state["pending"]:
        raise TextRunError("uncertain_request_manual_audit_required")
    if state["reservation_exceeded"]:
        raise TextRunError("actual_usage_exceeded_reservation_manual_audit_required")
    summary = _summary(manifest, manifest_sha, run_dir, state, unique, estimates, root)
    if summary["additional_reserved_tokens"] + state["charged_tokens"] > TOKEN_CAP:
        raise TextRunError("global_token_budget_exceeded")
    if not execute:
        return {"status": "dry_run", "provider_calls_this_invocation": 0, **summary}
    with _lock(run_dir, root):
        state = _state(run_dir, root)
        if state["pending"]:
            raise TextRunError("uncertain_request_manual_audit_required")
        if state["reservation_exceeded"]:
            raise TextRunError("actual_usage_exceeded_reservation_manual_audit_required")
        _immutable(config_path, _bytes(config), root)
        _immutable(run_dir / "manifests" / (manifest_sha + ".json"), manifest_raw, root)
        missing = [(key, document) for key, document in unique.items() if _cache(document, run_dir, state, root) is None]
        if state["charged_tokens"] + sum(estimates[key]["reserved_tokens"] for key, _ in missing) > TOKEN_CAP:
            raise TextRunError("global_token_budget_exceeded")
        credential = api_key
        if missing and credential is None:
            credential = _credential(dotenv_path or root / ".env", root)
        call = transport or _post
        calls = 0
        for input_type in ("document", "query"):
            inputs = [(key, document) for key, document in missing if document["input_type"] == input_type]
            for offset in range(0, len(inputs), batch_size):
                batch = inputs[offset:offset + batch_size]
                state = _state(run_dir, root)
                reserved = sum(estimates[key]["reserved_tokens"] for key, _ in batch)
                if state["charged_tokens"] + reserved > TOKEN_CAP:
                    raise TextRunError("global_token_budget_exceeded")
                payload = {"model": MODEL, "input": [document["text"] for _, document in batch], "input_type": input_type,
                           "output_dimension": DIMENSION, "output_dtype": "float", "truncation": False}
                request_id = _sha(_bytes({"manifest_sha256": manifest_sha, "cache_keys": [key for key, _ in batch]}))
                request = {"request_id": request_id, "manifest_sha256": manifest_sha, "payload": payload,
                           "cache_keys": [key for key, _ in batch], "estimates": [estimates[key] for key, _ in batch]}
                request_raw = _bytes(request)
                _immutable(run_dir / "requests" / (request_id + ".json"), request_raw, root)
                _event(run_dir, state, {"type": "reserved", "request_id": request_id,
                       "reserved_tokens": reserved, "request_sha256": _sha(request_raw)}, root)
                calls += 1
                try:
                    response = validate_response(call(payload, credential), len(batch))
                    response_raw = _bytes(response)
                    _immutable(run_dir / "responses" / (request_id + ".json"), response_raw, root)
                    objects = {}
                    for index, (key, document) in enumerate(batch):
                        vector = response["data"][index]["embedding"]
                        value = {"cache_key": key, "identity": _identity(document), "vector": vector,
                                 "vector_sha256": _sha(_bytes(vector)), "request_id": request_id, "response_index": index}
                        raw = _bytes(value)
                        _immutable(run_dir / "cache" / (key + ".json"), raw, root)
                        objects[key] = _sha(raw)
                    _event(run_dir, _state(run_dir, root), {"type": "completed", "request_id": request_id,
                           "actual_tokens": response["usage"]["total_tokens"], "response_sha256": _sha(response_raw),
                           "cache_sha256": objects}, root)
                except Exception as exc:
                    current = _state(run_dir, root)
                    if request_id not in current["completed"]:
                        _event(run_dir, current, {"type": "uncertain", "request_id": request_id,
                               "reason": "transport_response_or_persistence_failure_no_retry",
                               "http_status": exc.http_status if isinstance(exc, TextRunError) else None,
                               "retry_after_seconds": exc.retry_after_seconds if isinstance(exc, TextRunError) else None}, root)
                    raise TextRunError("uncertain_request_manual_audit_required") from None
                if response["usage"]["total_tokens"] > reserved:
                    raise TextRunError("actual_usage_exceeded_reservation_manual_audit_required")
                if _state(run_dir, root)["charged_tokens"] > TOKEN_CAP:
                    raise TextRunError("reported_usage_exceeded_reserved_budget_manual_audit_required")
        mapping = {document["input_id"]: cache_key(document) for document in manifest["documents"]}
        _immutable(run_dir / "manifest-vectors" / (manifest_sha + ".json"), _bytes({"manifest_sha256": manifest_sha, "cache_keys": mapping}), root)
        state = _state(run_dir, root)
        return {"status": "completed", "provider_calls_this_invocation": calls,
                **_summary(manifest, manifest_sha, run_dir, state, unique, estimates, root)}


def load_manifest_vectors(run_dir: Path, manifest_sha256: str, *, archive_root: Path) -> dict:
    if not re.fullmatch(r"[a-f0-9]{64}", manifest_sha256):
        raise TextRunError("invalid_manifest_digest")
    run_dir = _private(run_dir, archive_root)
    manifest, _, digest = _manifest(_private(run_dir / "manifests" / (manifest_sha256 + ".json"), archive_root))
    mapping, _ = _read(_private(run_dir / "manifest-vectors" / (manifest_sha256 + ".json"), archive_root))
    expected = {document["input_id"]: cache_key(document) for document in manifest["documents"]}
    if digest != manifest_sha256 or mapping != {"manifest_sha256": digest, "cache_keys": expected}:
        raise TextRunError("manifest_vector_binding_mismatch")
    state = _state(run_dir, archive_root)
    vectors = {document["input_id"]: _cache(document, run_dir, state, archive_root) for document in manifest["documents"]}
    if any(value is None for value in vectors.values()):
        raise TextRunError("manifest_vectors_incomplete")
    return {"manifest": manifest, "vectors": vectors,
            "receipt": {"actual_reported_tokens": state["actual_tokens"], "conservative_charged_tokens": state["charged_tokens"],
                        "pending_or_uncertain_requests": len(state["pending"]), "total_token_cap": TOKEN_CAP}}
