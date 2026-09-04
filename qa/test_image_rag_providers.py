from __future__ import annotations

import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def load_module():
    importlib.invalidate_caches()
    return importlib.import_module("image_rag_eval.providers")


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


class ProvidersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_load_credentials_is_whitelisted_and_process_env_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "GEMINI_API_KEY=file-gemini",
                        "QDRANT_API_KEY=file-qdrant",
                        "QDRANT_ENDPOINT=https://demo.cloud.qdrant.io",
                        "VOYAGE_API_KEY=file-voyage",
                        "OPENAI_API_KEY=ignore-me",
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {
                    "GEMINI_API_KEY": "process-gemini",
                    "QDRANT_API_KEY": "process-qdrant",
                    "QDRANT_ENDPOINT": "",
                    "VOYAGE_API_KEY": "process-voyage",
                },
                clear=True,
            ):
                credentials = self.module.load_credentials([env_path])

        self.assertEqual(credentials["GEMINI_API_KEY"], "process-gemini")
        self.assertEqual(credentials["QDRANT_API_KEY"], "process-qdrant")
        self.assertEqual(credentials["QDRANT_ENDPOINT"], "https://demo.cloud.qdrant.io")
        self.assertEqual(credentials["VOYAGE_API_KEY"], "process-voyage")
        self.assertNotIn("OPENAI_API_KEY", credentials)
        self.assertEqual(
            self.module.credential_presence(credentials),
            {
                "GEMINI_API_KEY": True,
                "QDRANT_API_KEY": True,
                "QDRANT_ENDPOINT": True,
                "VOYAGE_API_KEY": True,
            },
        )

    def test_preflight_is_sanitized_and_does_not_leak_secrets(self) -> None:
        captured: list[tuple[str, str, dict[str, str], bytes | None]] = []
        module = self.module

        class FakeOpener:
            def open(self, request, timeout=0):
                captured.append((request.method, request.full_url, dict(request.header_items()), request.data))
                if request.full_url.endswith("/v1beta/models/gemini-embedding-2"):
                    return FakeResponse({"name": "models/gemini-embedding-2"}, request.full_url)
                if request.full_url.endswith("/collections"):
                    return FakeResponse(
                        {
                            "status": "ok",
                            "result": {"collections": [{"name": "a"}, {"name": "b"}]},
                        },
                        request.full_url,
                    )
                raise AssertionError(f"unexpected URL: {request.full_url}")

        credentials = {
            "GEMINI_API_KEY": "super-secret-gemini",
            "QDRANT_API_KEY": "super-secret-qdrant",
            "QDRANT_ENDPOINT": "https://demo.cloud.qdrant.io",
            "VOYAGE_API_KEY": "present-only",
        }
        with patch.object(module, "build_opener", return_value=FakeOpener()):
            payload = module.preflight(credentials)

        self.assertEqual(payload["gemini"]["model_id"], "models/gemini-embedding-2")
        self.assertEqual(payload["qdrant"]["collection_count"], 2)
        rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("super-secret-gemini", rendered)
        self.assertNotIn("super-secret-qdrant", rendered)
        self.assertNotIn("https://demo.cloud.qdrant.io", rendered)
        self.assertEqual(
            [item[1] for item in captured],
            [
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2",
                "https://demo.cloud.qdrant.io/collections",
            ],
        )

    def test_preflight_rejects_invalid_qdrant_host_without_network(self) -> None:
        module = self.module
        with patch.object(module, "build_opener", side_effect=AssertionError("network called")):
            payload = module.preflight(
                {
                    "QDRANT_API_KEY": "present",
                    "QDRANT_ENDPOINT": "https://example.com",
                }
            )
        self.assertEqual(payload["gemini"]["status"], "missing_credentials")
        self.assertEqual(payload["qdrant"]["status"], "error")
        self.assertEqual(payload["qdrant"]["provider"], "qdrant")
        self.assertIsNone(payload["qdrant"]["http_status"])

    def test_no_redirect_handler_raises_sanitized_provider_error(self) -> None:
        handler = self.module._NoRedirectHandler("gemini")
        with self.assertRaises(self.module.ProviderError) as context:
            handler.redirect_request(None, None, 302, "redirect", None, "https://evil.example")
        self.assertEqual(str(context.exception), "gemini:302")
        self.assertEqual(context.exception.provider_status, "unknown")
        self.assertIsNone(context.exception.retry_after_seconds)
        self.assertFalse(context.exception.quota_exhausted)
        self.assertEqual(context.exception.quota_period, "unknown")

    def test_embed_image_and_text_uses_joint_parts_and_normalizes_vector(self) -> None:
        captured: list[dict[str, object]] = []
        module = self.module

        class FakeOpener:
            def open(self, request, timeout=0):
                captured.append(
                    {
                        "url": request.full_url,
                        "headers": dict(request.header_items()),
                        "body": json.loads(request.data.decode("utf-8")) if request.data else None,
                    }
                )
                return FakeResponse(
                    {
                        "embedding": {"values": [3.0, 4.0, 0.0, 0.0]},
                        "usageMetadata": {"promptTokenCount": 12, "totalTokenCount": 12},
                    },
                    request.full_url,
                )

        embedder = module.GeminiEmbedder("gemini-secret", dimensions=4, timeout=5)
        with patch.object(module, "build_opener", return_value=FakeOpener()):
            result = embedder.embed(
                image_bytes=b"\x89PNG",
                mime_type="image/png",
                text="approved bottle hero shot",
                task_type=module.TASK_RETRIEVAL_DOCUMENT,
            )

        self.assertEqual(result["model"], "gemini-embedding-2")
        self.assertAlmostEqual(result["vector"][0], 0.6, places=6)
        self.assertAlmostEqual(result["vector"][1], 0.8, places=6)
        self.assertEqual(result["usage"], {"prompt_token_count": 12, "total_token_count": 12})
        body = captured[0]["body"]
        self.assertEqual(captured[0]["url"], "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:embedContent")
        self.assertEqual(body["outputDimensionality"], 4)
        self.assertNotIn("taskType", body)
        self.assertEqual(len(body["content"]["parts"]), 2)
        self.assertEqual(body["content"]["parts"][1]["text"], "approved bottle hero shot")

    def test_embed_query_formats_text_without_task_type_field(self) -> None:
        captured: list[dict[str, object]] = []
        module = self.module

        class FakeOpener:
            def open(self, request, timeout=0):
                captured.append(json.loads(request.data.decode("utf-8")))
                return FakeResponse({"embedding": {"values": [1.0, 0.0]}}, request.full_url)

        embedder = module.GeminiEmbedder("gemini-secret", dimensions=2)
        with patch.object(module, "build_opener", return_value=FakeOpener()):
            embedder.embed(text="blue sports drink", task_type=module.TASK_RETRIEVAL_QUERY)

        self.assertEqual(captured[0]["content"]["parts"], [{"text": "task: search result | query: blue sports drink"}])
        self.assertNotIn("taskType", captured[0])

    def test_preflight_accepts_qdrant_cloud_port_6333(self) -> None:
        captured: list[str] = []
        module = self.module

        class FakeOpener:
            def open(self, request, timeout=0):
                captured.append(request.full_url)
                return FakeResponse({"status": "ok", "result": {"collections": []}}, request.full_url)

        with patch.object(module, "build_opener", return_value=FakeOpener()):
            payload = module.preflight(
                {
                    "QDRANT_API_KEY": "present",
                    "QDRANT_ENDPOINT": "https://demo.cloud.qdrant.io:6333",
                }
            )
        self.assertEqual(payload["qdrant"]["status"], "ok")
        self.assertEqual(captured, ["https://demo.cloud.qdrant.io:6333/collections"])

    def test_embed_rejects_wrong_dimension_response(self) -> None:
        module = self.module

        class FakeOpener:
            def open(self, request, timeout=0):
                return FakeResponse({"embedding": {"values": [1.0, 2.0, 3.0]}}, request.full_url)

        embedder = module.GeminiEmbedder("gemini-secret", dimensions=4)
        with patch.object(module, "build_opener", return_value=FakeOpener()):
            with self.assertRaises(module.ProviderError) as context:
                embedder.embed(text="mismatch", task_type=module.TASK_RETRIEVAL_QUERY)
        self.assertEqual(str(context.exception), "gemini")

    def test_embed_http_400_is_sanitized(self) -> None:
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
                    io.BytesIO(b'{"error":{"message":"secret body"}}'),
                )

        embedder = module.GeminiEmbedder("gemini-secret", dimensions=2)
        with patch.object(module, "build_opener", return_value=FakeOpener()):
            with self.assertRaises(module.ProviderError) as context:
                embedder.embed(text="bad input", task_type=module.TASK_RETRIEVAL_QUERY)
        self.assertEqual(context.exception.provider, "gemini")
        self.assertEqual(context.exception.http_status, 400)
        self.assertEqual(str(context.exception), "gemini:400")
        self.assertNotIn("secret", str(context.exception))
        self.assertEqual(context.exception.provider_status, "invalid_request")
        self.assertIsNone(context.exception.retry_after_seconds)
        self.assertFalse(context.exception.quota_exhausted)
        self.assertEqual(context.exception.quota_period, "unknown")

    def test_embed_http_429_parses_sanitized_retry_and_quota_metadata(self) -> None:
        module = self.module

        class FakeOpener:
            def open(self, request, timeout=0):
                headers = Message()
                headers["Content-Type"] = "application/json"
                raise HTTPError(
                    request.full_url,
                    429,
                    "rate limited",
                    headers,
                    io.BytesIO(
                        json.dumps(
                            {
                                "error": {
                                    "code": 429,
                                    "message": "sk-proj-secret https://evil.example/private?id=1",
                                    "status": "RESOURCE_EXHAUSTED",
                                    "details": [
                                        {
                                            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                            "violations": [
                                                {
                                                    "quotaValue": "0",
                                                    "quotaMetric": "secretMetric",
                                                    "description": "limit per day",
                                                }
                                            ],
                                        },
                                        {
                                            "@type": "type.googleapis.com/google.rpc.RetryInfo",
                                            "retryDelay": "60s",
                                        },
                                    ],
                                }
                            }
                        ).encode("utf-8")
                    ),
                )

        embedder = module.GeminiEmbedder("gemini-secret", dimensions=2)
        with patch.object(module, "build_opener", return_value=FakeOpener()):
            with self.assertRaises(module.ProviderError) as context:
                embedder.embed(text="limited input", task_type=module.TASK_RETRIEVAL_QUERY)
        self.assertEqual(context.exception.provider, "gemini")
        self.assertEqual(context.exception.http_status, 429)
        self.assertEqual(context.exception.provider_status, "quota_exhausted")
        self.assertEqual(context.exception.retry_after_seconds, 60)
        self.assertTrue(context.exception.quota_exhausted)
        self.assertEqual(context.exception.quota_period, "day")
        self.assertNotIn("sk-proj-secret", str(context.exception))
        self.assertNotIn("evil.example", str(context.exception))

    def test_embed_http_429_uses_retry_after_header_without_body_leakage(self) -> None:
        module = self.module

        class FakeOpener:
            def open(self, request, timeout=0):
                headers = Message()
                headers["Content-Type"] = "text/plain"
                headers["Retry-After"] = "120"
                raise HTTPError(
                    request.full_url,
                    429,
                    "rate limited",
                    headers,
                    io.BytesIO(b"api_key=secret-token"),
                )

        embedder = module.GeminiEmbedder("gemini-secret", dimensions=2)
        with patch.object(module, "build_opener", return_value=FakeOpener()):
            with self.assertRaises(module.ProviderError) as context:
                embedder.embed(text="limited input", task_type=module.TASK_RETRIEVAL_QUERY)
        self.assertEqual(context.exception.provider_status, "rate_limited")
        self.assertEqual(context.exception.retry_after_seconds, 120)
        self.assertFalse(context.exception.quota_exhausted)
        self.assertEqual(context.exception.quota_period, "unknown")
        self.assertNotIn("secret-token", str(context.exception))


if __name__ == "__main__":
    unittest.main()
