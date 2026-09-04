from __future__ import annotations

import base64
import json
import math
import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


ALLOWED_ENV_KEYS = frozenset(
    {
        "GEMINI_API_KEY",
        "QDRANT_API_KEY",
        "QDRANT_ENDPOINT",
        "VOYAGE_API_KEY",
    }
)
GOOGLE_HOST = "generativelanguage.googleapis.com"
GOOGLE_BASE_URL = f"https://{GOOGLE_HOST}"
QDRANT_HOST_SUFFIX = ".cloud.qdrant.io"
DEFAULT_TIMEOUT = 45
DEFAULT_GEMINI_MODEL = "gemini-embedding-2"
DEFAULT_DIMENSIONS = 768
ALLOWED_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png"})
TASK_RETRIEVAL_DOCUMENT = "RETRIEVAL_DOCUMENT"
TASK_RETRIEVAL_QUERY = "RETRIEVAL_QUERY"
MAX_ERROR_BODY_BYTES = 64 * 1024
ALLOWED_QUOTA_PERIODS = frozenset({"day", "minute", "unknown"})
ALLOWED_PROVIDER_STATUSES = frozenset(
    {
        "unknown",
        "invalid_request",
        "unauthorized",
        "forbidden",
        "not_found",
        "rate_limited",
        "quota_exhausted",
        "server_error",
    }
)


class ProviderError(RuntimeError):
    """Sanitized provider exception that never carries secrets or raw bodies."""

    def __init__(
        self,
        provider: str,
        http_status: int | None = None,
        *,
        retry_after_seconds: int | None = None,
        quota_exhausted: bool = False,
        quota_period: str = "unknown",
        provider_status: str = "unknown",
    ) -> None:
        normalized_quota_period = str(quota_period or "unknown").casefold()
        normalized_provider_status = str(provider_status or "unknown").casefold()
        if normalized_quota_period not in ALLOWED_QUOTA_PERIODS:
            raise ValueError("unsupported quota_period")
        if normalized_provider_status not in ALLOWED_PROVIDER_STATUSES:
            raise ValueError("unsupported provider_status")
        self.provider = provider
        self.http_status = http_status
        self.retry_after_seconds = None if retry_after_seconds is None else max(0, int(retry_after_seconds))
        self.quota_exhausted = bool(quota_exhausted)
        self.quota_period = normalized_quota_period
        self.provider_status = normalized_provider_status
        super().__init__(str(self))

    def __str__(self) -> str:
        if self.http_status is None:
            return self.provider
        return f"{self.provider}:{self.http_status}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "http_status": self.http_status,
            "retry_after_seconds": self.retry_after_seconds,
            "quota_exhausted": self.quota_exhausted,
            "quota_period": self.quota_period,
            "provider_status": self.provider_status,
        }


class _NoRedirectHandler(HTTPRedirectHandler):
    def __init__(self, provider: str) -> None:
        super().__init__()
        self.provider = provider

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise ProviderError(self.provider, code, provider_status=_provider_status(code))


def _normalize_env_value(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[:1] == value[-1:] and value[:1] in {'"', "'"}:
        return value[1:-1]
    if " #" in value:
        return value.split(" #", 1)[0].rstrip()
    return value


def _read_env_file(path: Path) -> dict[str, str]:
    loaded: dict[str, str] = {}
    if not path.is_file():
        return loaded
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key not in ALLOWED_ENV_KEYS:
            continue
        value = _normalize_env_value(raw_value)
        if value:
            loaded[key] = value
    return loaded


def load_credentials(env_paths: list[Path]) -> dict[str, str]:
    credentials: dict[str, str] = {}
    for path in env_paths:
        credentials.update(_read_env_file(path))
    for key in ALLOWED_ENV_KEYS:
        value = str(os.environ.get(key) or "").strip()
        if value:
            credentials[key] = value
    return credentials


def credential_presence(credentials: dict[str, str]) -> dict[str, bool]:
    return {key: bool(str(credentials.get(key) or "").strip()) for key in sorted(ALLOWED_ENV_KEYS)}


def _provider_status(http_status: int | None, *, quota_exhausted: bool = False) -> str:
    if quota_exhausted:
        return "quota_exhausted"
    if http_status == 400:
        return "invalid_request"
    if http_status == 401:
        return "unauthorized"
    if http_status == 403:
        return "forbidden"
    if http_status == 404:
        return "not_found"
    if http_status == 429:
        return "rate_limited"
    if http_status is not None and 500 <= http_status <= 599:
        return "server_error"
    return "unknown"


def _read_bounded_error_body(exc: HTTPError) -> bytes:
    handle = getattr(exc, "fp", None)
    if handle is None:
        return b""
    try:
        return handle.read(MAX_ERROR_BODY_BYTES + 1)[:MAX_ERROR_BODY_BYTES]
    except Exception:
        return b""


def _parse_json_body(raw: bytes) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _retry_after_from_header(value: str | None) -> int | None:
    if not value:
        return None
    stripped = str(value).strip()
    if not stripped:
        return None
    try:
        return max(0, int(float(stripped)))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(stripped)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        seconds = int((parsed - datetime.now(timezone.utc)).total_seconds())
        return max(0, seconds)


def _retry_after_from_retry_delay(value: Any) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return max(0, int(value))
    text = str(value or "").strip().casefold()
    if not text:
        return None
    if text.endswith("s"):
        text = text[:-1]
    try:
        return max(0, int(float(text)))
    except ValueError:
        return None


def _iter_json_nodes(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_json_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_json_nodes(child)


def _quota_period_from_payload(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return "unknown"
    for node in _iter_json_nodes(payload):
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            combined = f"{key} {value}".casefold()
            if any(token in combined for token in ("per day", "1d", "day")):
                return "day"
            if any(token in combined for token in ("per minute", "1m", "minute", "rpm", "tpm")):
                return "minute"
    return "unknown"


def _quota_exhausted_from_payload(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    for node in _iter_json_nodes(payload):
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            if str(key).casefold() != "quotavalue":
                continue
            if value == 0 or str(value).strip() == "0":
                return True
    return False


def _retry_after_from_payload(payload: dict[str, Any] | None) -> int | None:
    if not isinstance(payload, dict):
        return None
    for node in _iter_json_nodes(payload):
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            if str(key) == "retryDelay":
                seconds = _retry_after_from_retry_delay(value)
                if seconds is not None:
                    return seconds
    return None


def provider_error_from_http(provider: str, exc: HTTPError) -> ProviderError:
    payload = _parse_json_body(_read_bounded_error_body(exc))
    retry_after_seconds = _retry_after_from_payload(payload)
    if retry_after_seconds is None:
        retry_after_seconds = _retry_after_from_header(exc.headers.get("Retry-After") if exc.headers else None)
    quota_exhausted = _quota_exhausted_from_payload(payload)
    quota_period = _quota_period_from_payload(payload)
    return ProviderError(
        provider,
        exc.code,
        retry_after_seconds=retry_after_seconds,
        quota_exhausted=quota_exhausted,
        quota_period=quota_period,
        provider_status=_provider_status(exc.code, quota_exhausted=quota_exhausted),
    )


def _validate_base_url(parsed: SplitResult, *, provider: str) -> None:
    if parsed.scheme.casefold() != "https":
        raise ProviderError(provider)
    if parsed.username or parsed.password:
        raise ProviderError(provider)
    if parsed.query or parsed.fragment:
        raise ProviderError(provider)


def _gemini_model_url(model: str) -> str:
    return f"{GOOGLE_BASE_URL}/v1beta/models/{model}"


def _gemini_embed_url(model: str) -> str:
    return f"{_gemini_model_url(model)}:embedContent"


def _validate_gemini_url(url: str) -> None:
    parsed = urlsplit(url)
    _validate_base_url(parsed, provider="gemini")
    if (parsed.hostname or "").casefold() != GOOGLE_HOST:
        raise ProviderError("gemini")
    if not parsed.path.startswith("/v1beta/models/"):
        raise ProviderError("gemini")


def _validated_qdrant_base(endpoint: str) -> str:
    parsed = urlsplit(str(endpoint or "").strip())
    _validate_base_url(parsed, provider="qdrant")
    host = (parsed.hostname or "").casefold()
    if not host or not host.endswith(QDRANT_HOST_SUFFIX):
        raise ProviderError("qdrant")
    if host == QDRANT_HOST_SUFFIX.removeprefix("."):
        raise ProviderError("qdrant")
    if parsed.port not in (None, 443, 6333):
        raise ProviderError("qdrant")
    if parsed.path not in ("", "/"):
        raise ProviderError("qdrant")
    if parsed.port in (None, 443):
        return f"https://{host}"
    return f"https://{host}:{parsed.port}"


def _sanitize_usage(payload: dict[str, Any]) -> dict[str, int]:
    usage = payload.get("usageMetadata")
    if not isinstance(usage, dict):
        return {}
    sanitized: dict[str, int] = {}
    for raw_key, value in usage.items():
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        key = str(raw_key)
        parts: list[str] = []
        current = []
        for char in key:
            if char.isupper() and current:
                parts.append("".join(current))
                current = [char.casefold()]
            else:
                current.append(char.casefold())
        if current:
            parts.append("".join(current))
        sanitized["_".join(parts)] = value
    return dict(sorted(sanitized.items()))


def _request_json(
    provider: str,
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    timeout: int,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if provider == "gemini":
        _validate_gemini_url(url)
    elif provider == "qdrant":
        _validated_qdrant_base(url.rsplit("/collections", 1)[0] if url.endswith("/collections") else url)
    else:
        raise ValueError(f"unsupported provider: {provider}")

    body = None
    request_headers = dict(headers)
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request_headers.setdefault("Accept", "application/json")
    request_headers.setdefault("User-Agent", "image-rag-eval/1.0")
    request = Request(url, data=body, headers=request_headers, method=method)
    opener = build_opener(_NoRedirectHandler(provider))
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.geturl() != url:
                raise ProviderError(provider)
            raw = response.read()
    except HTTPError as exc:
        raise provider_error_from_http(provider, exc) from exc
    except URLError as exc:
        raise ProviderError(provider) from exc

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError(provider) from exc
    if not isinstance(parsed, dict):
        raise ProviderError(provider)
    return parsed


def _preflight_gemini(credentials: dict[str, str]) -> dict[str, Any]:
    api_key = str(credentials.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return {"ok": False, "status": "missing_credentials"}
    try:
        payload = _request_json(
            "gemini",
            method="GET",
            url=_gemini_model_url(DEFAULT_GEMINI_MODEL),
            headers={"x-goog-api-key": api_key},
            timeout=DEFAULT_TIMEOUT,
        )
    except ProviderError as exc:
        return {"ok": False, "status": "error", **exc.to_dict()}
    model_id = str(payload.get("name") or f"models/{DEFAULT_GEMINI_MODEL}")
    return {"ok": True, "status": "ok", "model_id": model_id}


def _preflight_qdrant(credentials: dict[str, str]) -> dict[str, Any]:
    api_key = str(credentials.get("QDRANT_API_KEY") or "").strip()
    endpoint = str(credentials.get("QDRANT_ENDPOINT") or "").strip()
    if not api_key or not endpoint:
        return {"ok": False, "status": "missing_credentials"}
    try:
        base_url = _validated_qdrant_base(endpoint)
        payload = _request_json(
            "qdrant",
            method="GET",
            url=f"{base_url}/collections",
            headers={"api-key": api_key},
            timeout=DEFAULT_TIMEOUT,
        )
    except ProviderError as exc:
        return {"ok": False, "status": "error", **exc.to_dict()}
    result = payload.get("result")
    collections = result.get("collections") if isinstance(result, dict) else []
    collection_count = len(collections) if isinstance(collections, list) else 0
    return {
        "ok": True,
        "status": str(payload.get("status") or "unknown"),
        "collection_count": collection_count,
    }


def preflight(credentials: dict[str, str]) -> dict[str, Any]:
    return {
        "credentials": credential_presence(credentials),
        "gemini": _preflight_gemini(credentials),
        "qdrant": _preflight_qdrant(credentials),
    }


def _normalize_image_mime_type(mime_type: str | None) -> str:
    normalized = str(mime_type or "").strip().casefold()
    if normalized == "image/jpg":
        normalized = "image/jpeg"
    if normalized not in ALLOWED_IMAGE_MIME_TYPES:
        raise ValueError("mime_type must be image/png or image/jpeg")
    return normalized


def _embedding_text(text: str, *, task_type: str, image_present: bool) -> str:
    normalized = text.strip()
    if not normalized:
        return ""
    if image_present:
        return normalized
    if task_type == TASK_RETRIEVAL_QUERY:
        return f"task: search result | query: {normalized}"
    if task_type == TASK_RETRIEVAL_DOCUMENT:
        return f"title: none | text: {normalized}"
    raise ValueError("unsupported task_type")


def _normalized_vector(values: Any, *, dimensions: int) -> list[float]:
    if not isinstance(values, list) or len(values) != dimensions:
        raise ProviderError("gemini")
    vector: list[float] = []
    for item in values:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ProviderError("gemini")
        number = float(item)
        if not math.isfinite(number):
            raise ProviderError("gemini")
        vector.append(number)
    magnitude = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(magnitude) or magnitude <= 0.0:
        raise ProviderError("gemini")
    return [value / magnitude for value in vector]


class GeminiEmbedder:
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_GEMINI_MODEL,
        dimensions: int = DEFAULT_DIMENSIONS,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        normalized_api_key = str(api_key or "").strip()
        normalized_model = str(model or "").strip()
        if not normalized_api_key:
            raise ValueError("api_key is required")
        if not normalized_model:
            raise ValueError("model is required")
        if dimensions < 1:
            raise ValueError("dimensions must be positive")
        if timeout < 1:
            raise ValueError("timeout must be positive")
        self.api_key = normalized_api_key
        self.model = normalized_model
        self.dimensions = int(dimensions)
        self.timeout = int(timeout)

    def embed(
        self,
        image_bytes: bytes | None = None,
        mime_type: str | None = None,
        text: str = "",
        task_type: str = TASK_RETRIEVAL_DOCUMENT,
    ) -> dict[str, Any]:
        normalized_text = str(text or "").strip()
        if image_bytes is None and not normalized_text:
            raise ValueError("image_bytes or text is required")
        if image_bytes is not None and not isinstance(image_bytes, (bytes, bytearray)):
            raise ValueError("image_bytes must be bytes-like")

        parts: list[dict[str, Any]] = []
        if image_bytes is not None:
            parts.append(
                {
                    "inline_data": {
                        "mime_type": _normalize_image_mime_type(mime_type),
                        "data": base64.b64encode(bytes(image_bytes)).decode("ascii"),
                    }
                }
            )
        if normalized_text:
            parts.append(
                {
                    "text": _embedding_text(
                        normalized_text,
                        task_type=task_type,
                        image_present=image_bytes is not None,
                    )
                }
            )

        payload = {
            "content": {"parts": parts},
            "outputDimensionality": self.dimensions,
        }
        response = _request_json(
            "gemini",
            method="POST",
            url=_gemini_embed_url(self.model),
            headers={"x-goog-api-key": self.api_key},
            timeout=self.timeout,
            payload=payload,
        )
        embedding = response.get("embedding")
        values = embedding.get("values") if isinstance(embedding, dict) else None
        return {
            "vector": _normalized_vector(values, dimensions=self.dimensions),
            "usage": _sanitize_usage(response),
            "model": self.model,
        }


__all__ = [
    "DEFAULT_DIMENSIONS",
    "DEFAULT_GEMINI_MODEL",
    "GeminiEmbedder",
    "ProviderError",
    "credential_presence",
    "load_credentials",
    "preflight",
]
