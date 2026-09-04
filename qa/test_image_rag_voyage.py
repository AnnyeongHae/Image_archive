from __future__ import annotations

import importlib
import io
import json
import sys
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def load_module():
    importlib.invalidate_caches()
    return importlib.import_module("image_rag_eval.voyage_provider")


class FakeResponse:
    def __init__(self, payload: dict[str, object], url: str) -> None:
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._url = url
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._url


class VoyageProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_text_query_uses_query_input_type_and_fixed_endpoint(self) -> None:
        module = self.module
        captured: list[dict[str, object]] = []

        class FakeOpener:
            def open(self, request, timeout=0):
                captured.append(
                    {
                        "url": request.full_url,
                        "headers": dict(request.header_items()),
                        "body": json.loads(request.data.decode("utf-8")),
                    }
                )
                return FakeResponse(
                    {
                        "data": [{"embedding": [3.0, 4.0]}],
                        "model": "voyage-multimodal-3.5",
                        "usage": {"total_tokens": 77},
                    },
                    request.full_url,
                )

        embedder = module.VoyageEmbedder("voyage-secret", dimensions=2, timeout=5)
        with patch.object(module, "build_opener", return_value=FakeOpener()):
            result = embedder.embed(text="blue sports drink", task_type=module.TASK_RETRIEVAL_QUERY)

        self.assertEqual(captured[0]["url"], "https://api.voyageai.com/v1/multimodalembeddings")
        self.assertEqual(captured[0]["headers"]["Authorization"], "Bearer voyage-secret")
        self.assertEqual(captured[0]["body"]["input_type"], "query")
        self.assertEqual(captured[0]["body"]["truncation"], False)
        self.assertEqual(captured[0]["body"]["inputs"], [{"content": [{"type": "text", "text": "blue sports drink"}]}])
        self.assertAlmostEqual(result["vector"][0], 0.6, places=6)
        self.assertAlmostEqual(result["vector"][1], 0.8, places=6)
        self.assertEqual(result["usage"], {"total_tokens": 77})

    def test_joint_text_and_image_uses_content_parts_in_order(self) -> None:
        module = self.module
        captured: list[dict[str, object]] = []

        class FakeOpener:
            def open(self, request, timeout=0):
                captured.append(json.loads(request.data.decode("utf-8")))
                return FakeResponse(
                    {
                        "embeddings": [[0.0, 5.0]],
                        "text_tokens": 11,
                        "image_pixels": 50000,
                        "total_tokens": 101,
                    },
                    request.full_url,
                )

        embedder = module.VoyageEmbedder("voyage-secret", dimensions=2)
        with patch.object(module, "build_opener", return_value=FakeOpener()):
            result = embedder.embed(
                image_bytes=b"\x89PNG",
                mime_type="image/webp",
                text="approved bottle hero shot",
                task_type=module.TASK_RETRIEVAL_DOCUMENT,
            )

        body = captured[0]
        self.assertEqual(body["model"], "voyage-multimodal-3.5")
        self.assertEqual(body["input_type"], "document")
        self.assertEqual(body["output_dimension"], 2)
        self.assertEqual(body["content"] if "content" in body else None, None)
        parts = body["inputs"][0]["content"]
        self.assertEqual(parts[0], {"type": "text", "text": "approved bottle hero shot"})
        self.assertEqual(parts[1]["type"], "image_base64")
        self.assertTrue(parts[1]["image_base64"].startswith("data:image/webp;base64,"))
        self.assertEqual(result["usage"], {"image_pixels": 50000, "text_tokens": 11, "total_tokens": 101})
        self.assertEqual(result["model"], "voyage-multimodal-3.5")

    def test_image_only_document_request(self) -> None:
        module = self.module
        captured: list[dict[str, object]] = []

        class FakeOpener:
            def open(self, request, timeout=0):
                captured.append(json.loads(request.data.decode("utf-8")))
                return FakeResponse({"data": [{"embedding": [1.0, 0.0]}]}, request.full_url)

        embedder = module.VoyageEmbedder("voyage-secret", dimensions=2)
        with patch.object(module, "build_opener", return_value=FakeOpener()):
            result = embedder.embed(image_bytes=b"GIF89a", mime_type="image/gif")

        self.assertEqual(captured[0]["input_type"], "document")
        self.assertEqual(captured[0]["inputs"][0]["content"][0]["type"], "image_base64")
        self.assertEqual(result["vector"], [1.0, 0.0])

    def test_redirect_is_rejected_with_sanitized_provider_error(self) -> None:
        with self.assertRaises(self.module.ProviderError) as context:
            self.module._NoRedirectHandler().redirect_request(None, None, 302, "redirect", None, "https://evil.example")
        self.assertEqual(str(context.exception), "voyage:302")
        self.assertEqual(context.exception.provider_status, "unknown")
        self.assertIsNone(context.exception.retry_after_seconds)
        self.assertFalse(context.exception.quota_exhausted)
        self.assertEqual(context.exception.quota_period, "unknown")

    def test_invalid_host_is_rejected_before_network(self) -> None:
        module = self.module
        with patch.object(module, "build_opener", side_effect=AssertionError("network called")):
            with self.assertRaises(module.ProviderError) as context:
                module._request_json(
                    method="POST",
                    url="https://example.com/v1/multimodalembeddings",
                    headers={"Authorization": "Bearer secret"},
                    timeout=5,
                    payload={"inputs": [], "model": module.VOYAGE_MODEL},
                )
        self.assertEqual(str(context.exception), "voyage")

    def test_wrong_dimension_response_is_rejected(self) -> None:
        module = self.module

        class FakeOpener:
            def open(self, request, timeout=0):
                return FakeResponse({"data": [{"embedding": [1.0, 2.0, 3.0]}]}, request.full_url)

        embedder = module.VoyageEmbedder("voyage-secret", dimensions=2)
        with patch.object(module, "build_opener", return_value=FakeOpener()):
            with self.assertRaises(module.ProviderError) as context:
                embedder.embed(text="mismatch", task_type=module.TASK_RETRIEVAL_QUERY)
        self.assertEqual(str(context.exception), "voyage")

    def test_http_400_is_sanitized(self) -> None:
        module = self.module

        class FakeOpener:
            def open(self, request, timeout=0):
                headers = Message()
                headers["Content-Type"] = "application/json"
                raise HTTPError(
                    request.full_url,
                    400,
                    "bad request",
                    headers,
                    io.BytesIO(b'{"error":"secret body"}'),
                )

        embedder = module.VoyageEmbedder("voyage-secret", dimensions=2)
        with patch.object(module, "build_opener", return_value=FakeOpener()):
            with self.assertRaises(module.ProviderError) as context:
                embedder.embed(text="bad input", task_type=module.TASK_RETRIEVAL_QUERY)
        self.assertEqual(context.exception.provider, "voyage")
        self.assertEqual(context.exception.http_status, 400)
        self.assertEqual(str(context.exception), "voyage:400")
        self.assertEqual(context.exception.provider_status, "invalid_request")
        self.assertIsNone(context.exception.retry_after_seconds)
        self.assertFalse(context.exception.quota_exhausted)
        self.assertEqual(context.exception.quota_period, "unknown")

    def test_http_429_uses_retry_after_header_and_keeps_error_sanitized(self) -> None:
        module = self.module

        class FakeOpener:
            def open(self, request, timeout=0):
                headers = Message()
                headers["Content-Type"] = "application/json"
                headers["Retry-After"] = "61"
                raise HTTPError(
                    request.full_url,
                    429,
                    "rate limited",
                    headers,
                    io.BytesIO(b'{"error":"voyage-secret https://evil.example"}'),
                )

        embedder = module.VoyageEmbedder("voyage-secret", dimensions=2)
        with patch.object(module, "build_opener", return_value=FakeOpener()):
            with self.assertRaises(module.ProviderError) as context:
                embedder.embed(text="bad input", task_type=module.TASK_RETRIEVAL_QUERY)
        self.assertEqual(context.exception.provider, "voyage")
        self.assertEqual(context.exception.http_status, 429)
        self.assertEqual(context.exception.provider_status, "rate_limited")
        self.assertEqual(context.exception.retry_after_seconds, 61)
        self.assertFalse(context.exception.quota_exhausted)
        self.assertEqual(context.exception.quota_period, "unknown")
        self.assertNotIn("voyage-secret", str(context.exception))

    def test_invalid_mime_type_is_rejected(self) -> None:
        embedder = self.module.VoyageEmbedder("voyage-secret")
        with self.assertRaisesRegex(ValueError, "mime_type"):
            embedder.embed(image_bytes=b"123", mime_type="image/tiff")


if __name__ == "__main__":
    unittest.main()
