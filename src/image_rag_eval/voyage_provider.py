from __future__ import annotations

import base64
import json
import math
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .providers import ProviderError, _provider_status, provider_error_from_http


VOYAGE_PROVIDER = "voyage"
VOYAGE_HOST = "api.voyageai.com"
VOYAGE_PATH = "/v1/multimodalembeddings"
VOYAGE_URL = f"https://{VOYAGE_HOST}{VOYAGE_PATH}"
VOYAGE_MODEL = "voyage-multimodal-3.5"
DEFAULT_DIMENSIONS = 1024
DEFAULT_TIMEOUT = 45
TASK_RETRIEVAL_DOCUMENT = "RETRIEVAL_DOCUMENT"
TASK_RETRIEVAL_QUERY = "RETRIEVAL_QUERY"
ALLOWED_IMAGE_MIME_TYPES = frozenset(
    {
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise ProviderError(VOYAGE_PROVIDER, code, provider_status=_provider_status(code))


def _validate_voyage_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme.casefold() != "https":
        raise ProviderError(VOYAGE_PROVIDER)
    if parsed.username or parsed.password:
        raise ProviderError(VOYAGE_PROVIDER)
    if parsed.query or parsed.fragment:
        raise ProviderError(VOYAGE_PROVIDER)
    if (parsed.hostname or "").casefold() != VOYAGE_HOST:
        raise ProviderError(VOYAGE_PROVIDER)
    if parsed.path != VOYAGE_PATH:
        raise ProviderError(VOYAGE_PROVIDER)


def _request_json(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    timeout: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    _validate_voyage_url(url)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Authorization": headers["Authorization"],
            "Content-Type": "application/json",
            "User-Agent": "image-rag-eval/1.0",
        },
        method=method,
    )
    opener = build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.geturl() != url:
                raise ProviderError(VOYAGE_PROVIDER)
            raw = response.read()
    except HTTPError as exc:
        raise provider_error_from_http(VOYAGE_PROVIDER, exc) from exc
    except URLError as exc:
        raise ProviderError(VOYAGE_PROVIDER) from exc

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError(VOYAGE_PROVIDER) from exc
    if not isinstance(parsed, dict):
        raise ProviderError(VOYAGE_PROVIDER)
    return parsed


def _normalize_image_mime_type(mime_type: str | None) -> str:
    normalized = str(mime_type or "").strip().casefold()
    if normalized == "image/jpg":
        normalized = "image/jpeg"
    if normalized not in ALLOWED_IMAGE_MIME_TYPES:
        raise ValueError("mime_type must be image/png, image/jpeg, image/webp, or image/gif")
    return normalized


def _input_type(task_type: str) -> str:
    if task_type == TASK_RETRIEVAL_DOCUMENT:
        return "document"
    if task_type == TASK_RETRIEVAL_QUERY:
        return "query"
    raise ValueError("unsupported task_type")


def _content_part_for_image(image_bytes: bytes, mime_type: str) -> dict[str, str]:
    data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    return {"type": "image_base64", "image_base64": data_url}


def _sanitize_usage(payload: dict[str, Any]) -> dict[str, int]:
    usage = payload.get("usage")
    sanitized: dict[str, int] = {}
    if isinstance(usage, dict):
        for key, value in usage.items():
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            sanitized[str(key)] = value
    for key in ("text_tokens", "image_pixels", "total_tokens"):
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        sanitized[key] = value
    return dict(sorted(sanitized.items()))


def _extract_vector(payload: dict[str, Any], *, dimensions: int) -> list[float]:
    values: Any = None
    data = payload.get("data")
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            values = first.get("embedding")
    if values is None:
        values = payload.get("embedding")
    if values is None:
        values = payload.get("embeddings", [None])[0] if isinstance(payload.get("embeddings"), list) else None
    if not isinstance(values, list) or len(values) != dimensions:
        raise ProviderError(VOYAGE_PROVIDER)
    vector: list[float] = []
    for item in values:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ProviderError(VOYAGE_PROVIDER)
        number = float(item)
        if not math.isfinite(number):
            raise ProviderError(VOYAGE_PROVIDER)
        vector.append(number)
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm <= 0.0:
        raise ProviderError(VOYAGE_PROVIDER)
    return [value / norm for value in vector]


class VoyageEmbedder:
    def __init__(
        self,
        api_key: str,
        dimensions: int = DEFAULT_DIMENSIONS,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        normalized_api_key = str(api_key or "").strip()
        if not normalized_api_key:
            raise ValueError("api_key is required")
        if int(dimensions) < 1:
            raise ValueError("dimensions must be positive")
        if timeout < 1:
            raise ValueError("timeout must be positive")
        self.api_key = normalized_api_key
        self.dimensions = int(dimensions)
        self.timeout = int(timeout)
        self.model = VOYAGE_MODEL

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

        content: list[dict[str, str]] = []
        if normalized_text:
            content.append({"type": "text", "text": normalized_text})
        if image_bytes is not None:
            content.append(_content_part_for_image(bytes(image_bytes), _normalize_image_mime_type(mime_type)))

        payload = {
            "inputs": [{"content": content}],
            "model": self.model,
            "input_type": _input_type(task_type),
            "output_dimension": self.dimensions,
            "output_encoding": None,
            "truncation": False,
        }
        response = _request_json(
            method="POST",
            url=VOYAGE_URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
            payload=payload,
        )
        return {
            "vector": _extract_vector(response, dimensions=self.dimensions),
            "model": str(response.get("model") or self.model),
            "usage": _sanitize_usage(response),
        }


__all__ = [
    "DEFAULT_DIMENSIONS",
    "DEFAULT_TIMEOUT",
    "TASK_RETRIEVAL_DOCUMENT",
    "TASK_RETRIEVAL_QUERY",
    "VOYAGE_MODEL",
    "VoyageEmbedder",
]
