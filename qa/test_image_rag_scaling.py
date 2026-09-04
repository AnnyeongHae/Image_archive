from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


ARCHIVE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = ARCHIVE_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from build_duplicate_index import BuildConfig, build_duplicate_index  # noqa: E402
from image_rag_eval.carryover import import_parent_cache_and_ledger  # noqa: E402
from image_rag_eval.comparison import execute_comparison, load_inputs, requests_for  # noqa: E402
from image_rag_eval.experiment import digest, json_bytes, prepare, read_json, run_path, write_json  # noqa: E402
from image_rag_eval.expansion import prepare50  # noqa: E402
from image_rag_eval.scaling import build_scaled_manifest, prepare200  # noqa: E402


def sha256_file(path: Path) -> str:
    digestor = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digestor.update(chunk)
    return digestor.hexdigest()


def record(
    key: str,
    prompt_text: str,
    *,
    lane: str,
    source_name: str,
    local_uri: str,
    local_sha256: str,
) -> dict:
    return {
        "catalog_key": key,
        "record_id": key.replace("fixture:", "record-"),
        "style_id": key.replace("fixture:", "FIXTURE-"),
        "lane": lane,
        "title": key,
        "source": {"name": source_name, "url": f"https://example.invalid/{source_name}/{key}"},
        "rights": {"status": "unknown"},
        "review": {"status": "needs_review"},
        "prompt": {"text": prompt_text},
        "media": {
            "assets": [
                {
                    "uri": local_uri,
                    "uri_kind": "local",
                    "sha256": local_sha256,
                    "mime_type": "image/png",
                }
            ]
        },
    }


class FakeClient:
    def __init__(self, dimensions: int):
        self.dimensions = dimensions
        self.calls = 0

    def embed(self, **kwargs):
        self.calls += 1
        return {"vector": [1.0] + [0.0] * (self.dimensions - 1), "usage": {"total_tokens": 12}}


class ImageRagScalingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.legacy_root = self.root / "legacy" / "current_archive"
        self.image_root = self.legacy_root / "images"
        self.image_root.mkdir(parents=True)
        self.queries = [
            {"id": f"q{i:02d}", "text": f"query {i:02d}", "relevance": {}, "human_judged": False}
            for i in range(1, 6)
        ]
        self._build_fixture_archive()
        self.parent20_run_id = "source20"
        self.parent50_run_id = "expanded50"
        prepare(self.root, self.parent20_run_id, limit=20)
        prepare50(self.root, self.parent20_run_id, self.parent50_run_id, limit=50, apply=True)
        self.parent20_dir = run_path(self.root, self.parent20_run_id)
        self.parent50_dir = run_path(self.root, self.parent50_run_id)
        self.parent20_manifest = read_json(self.parent20_dir / "manifest.json")
        self.parent50_manifest = read_json(self.parent50_dir / "manifest.json")

    def _build_fixture_archive(self) -> None:
        def write_png(path: Path, color: tuple[int, int, int]) -> str:
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (48, 48), color).save(path, format="PNG")
            return sha256_file(path)

        rows = []
        for index in range(210):
            path = self.image_root / f"img-{index:03d}.png"
            sha = write_png(path, ((index * 13) % 255, (index * 29) % 255, (index * 47) % 255))
            if index < 4:
                group = index // 2
                prompt = f"Exact-media control {group} variant {'A' if index % 2 == 0 else 'B'}."
            elif index < 64:
                prompt = f"Exact prompt group {(index - 4) // 2:02d} with layout controls."
            elif index < 150:
                prompt = f"Prompt scaffold family {(index - 64) // 2:02d} with sections and sliders."
            else:
                prompt = f"Unique prompt {index:03d} with source notes and lane context."
            rows.append(
                record(
                    f"fixture:{index:03d}",
                    prompt,
                    lane=f"lane-{index % 6}",
                    source_name=f"source-{index % 11}",
                    local_uri=f"images/img-{index:03d}.png",
                    local_sha256=sha,
                )
            )

        for left in range(0, 4, 2):
            source = self.image_root / f"img-{left:03d}.png"
            target = self.image_root / f"img-{left + 1:03d}.png"
            shutil.copyfile(source, target)
            rows[left + 1]["media"]["assets"][0]["sha256"] = sha256_file(target)

        canonical = self.root / "data" / "canonical" / "archive_records.jsonl"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

        cache_index = self.root / "data" / "private-research" / "remote-media-canary" / "current" / "cache_index.json"
        cache_index.parent.mkdir(parents=True, exist_ok=True)
        cache_index.write_text(json.dumps({"schema_version": "remote-media-cache-index-1.0", "items": []}) + "\n", encoding="utf-8")

        config = BuildConfig(
            platform_root=self.root,
            canonical_path=canonical,
            legacy_root=self.legacy_root,
            output_dir=self.root / "data" / "private-research" / "duplicate-analysis" / "current",
            thumbnail_root=self.root / "media" / "derived" / "duplicate-review",
            remote_overlay_path=cache_index,
            perceptual_limit=16,
            phash_threshold=64,
            dhash_threshold=64,
            thumbnail_limit=0,
        )
        build_duplicate_index(config, apply=True)

    def _write_vector_receipt(self, cache_dir: Path, request: dict) -> None:
        vector = [1.0] + [0.0] * (request["dimensions"] - 1)
        write_json(
            cache_dir / f"{request['key']}.json",
            {
                "key": request["key"],
                "provider": request["provider"],
                "model": request["model"],
                "usage": {},
                "latency_seconds": 0.1,
                "vector": vector,
                "vector_sha256": digest(json_bytes(vector)),
            },
        )

    def _seed_parent50_comparison(
        self,
        *,
        completed_selected: int | None = None,
        failed_selected: int = 0,
        unrelated_failed: int = 0,
        reserved_usd: float = 0.001,
    ) -> list[dict]:
        comparison_dir = self.parent50_dir / "comparison-v1"
        if comparison_dir.exists():
            shutil.rmtree(comparison_dir)
        cache_dir = comparison_dir / "vector-cache"
        cache_dir.mkdir(parents=True)
        manifest, _, pixels = load_inputs(self.root, self.parent50_run_id, maximum_items=50)
        requests = list({request["key"]: request for request in requests_for(manifest, pixels, self.queries, arms_subset=["voyage_image"]) }.values())
        write_json(comparison_dir / "queries.json", self.queries)
        if completed_selected is None:
            completed_selected = len(requests)
        self.assertLessEqual(completed_selected + failed_selected, len(requests))
        completed = requests[:completed_selected]
        failed = requests[completed_selected:completed_selected + failed_selected]
        attempts = []
        for request in completed:
            attempts.append(
                {
                    "key": request["key"],
                    "reserved_usd": reserved_usd,
                    "status": "completed",
                    "at": "2026-09-03T00:00:00Z",
                    "usage": {},
                }
            )
            self._write_vector_receipt(cache_dir, request)
        for request in failed:
            attempts.append(
                {
                    "key": request["key"],
                    "reserved_usd": reserved_usd,
                    "status": "failed_or_uncertain",
                    "at": "2026-09-03T00:00:00Z",
                    "usage": {},
                }
            )
        for index in range(unrelated_failed):
            attempts.append(
                {
                    "key": f"unrelated-failed-{index:02d}",
                    "reserved_usd": reserved_usd,
                    "status": "failed_or_uncertain",
                    "at": "2026-09-03T00:00:00Z",
                    "usage": {},
                }
            )
        write_json(comparison_dir / "budget.json", {"attempts": attempts, "pricing_verified_date": "2026-09-03"})
        return requests

    def _prepare_200(self, run_id: str, *, with_carryover: bool = True) -> tuple[dict, dict]:
        result = prepare200(self.root, self.parent50_run_id, run_id, apply=True)
        if with_carryover:
            import_parent_cache_and_ledger(self.root, self.parent50_run_id, run_id, apply=True)
        manifest = read_json(run_path(self.root, run_id) / "manifest.json")
        return result, manifest

    @staticmethod
    def _consent(manifest: dict, providers: list[str]) -> dict:
        return {
            "source_manifest_sha256": digest(json_bytes(manifest)),
            "authorization_source": "user_message",
            "user_quote": "synthetic scaling fixture only",
            "recorded_at": "2026-09-03T00:00:00Z",
            "external_ai_approved": True,
            "max_cost_usd": 0.10,
            "providers": providers,
            "approved_asset_ids": [item["id"] for item in manifest["items"]],
        }

    def test_build_scaled_manifest_and_prepare200_preserve_parent50(self) -> None:
        self._seed_parent50_comparison()
        manifest, prepared_inputs, meta = build_scaled_manifest(self.root, self.parent50_run_id)
        result, applied_manifest = self._prepare_200("scaled200")
        loaded_manifest, images, pixels = load_inputs(self.root, "scaled200", maximum_items=200)

        self.assertEqual(len(manifest["items"]), 200)
        self.assertEqual(manifest["items"][:50], self.parent50_manifest["items"])
        self.assertEqual(applied_manifest["items"][:50], self.parent50_manifest["items"])
        self.assertEqual(meta["preserved_item_count"], 50)
        self.assertEqual(meta["additional_item_count"], 150)
        self.assertTrue(meta["preserved_subset_validated"])
        self.assertEqual(sum(meta["selection_counts"].values()), 150)
        self.assertEqual(result["status"], "prepared_local_only")
        self.assertTrue(result["budget_plan"]["within_existing_cap"])
        self.assertEqual(len(loaded_manifest["items"]), 200)
        self.assertEqual(len(images), 200)
        self.assertEqual(len(pixels), 200)
        self.assertEqual(len(prepared_inputs), len({item["prepared_path"] for item in manifest["items"]}))
        self.assertLessEqual(len(prepared_inputs), 200)
        self.assertEqual(loaded_manifest["evaluation_arms"], ["voyage_image"])
        self.assertEqual(loaded_manifest["selection_profile"]["provider"], "voyage")

    def test_prepare200_blocks_apply_when_historical_67_attempts_and_53_cache_exceed_cap(self) -> None:
        requests = self._seed_parent50_comparison(completed_selected=53, failed_selected=0, unrelated_failed=14, reserved_usd=0.0018)
        self.assertGreaterEqual(len(requests), 53)

        dry_run = prepare200(self.root, self.parent50_run_id, "overbudget200", apply=False)

        self.assertEqual(dry_run["budget_plan"]["prior_attempts"], 67)
        self.assertEqual(dry_run["budget_plan"]["reusable_selected_cache_keys"], 53)
        self.assertFalse(dry_run["budget_plan"]["within_existing_cap"])
        with self.assertRaisesRegex(ValueError, "existing US\\$0.10 cap"):
            prepare200(self.root, self.parent50_run_id, "overbudget200", apply=True)

    def test_missing_carryover_blocks_200_execute_before_calls(self) -> None:
        self._seed_parent50_comparison()
        _result, manifest = self._prepare_200("nocarry200", with_carryover=False)
        consent = self._consent(manifest, ["voyage"])
        clients = {"gemini": FakeClient(3072), "voyage": FakeClient(1024)}

        with self.assertRaisesRegex(ValueError, "carryover receipt is required"):
            execute_comparison(
                self.root,
                "nocarry200",
                consent,
                clients=clients,
                queries=copy.deepcopy(self.queries),
                providers_subset=["voyage"],
                arms_subset=["voyage_image"],
                sleep=lambda _: None,
                maximum_items=200,
                max_new_requests=1,
            )
        self.assertEqual(clients["gemini"].calls, 0)
        self.assertEqual(clients["voyage"].calls, 0)

    def test_child_prefix_drift_and_parent_receipt_drift_are_blocked(self) -> None:
        self._seed_parent50_comparison()
        _result, manifest = self._prepare_200("drift200")
        child_dir = run_path(self.root, "drift200")
        child_receipt = read_json(child_dir / "prepared.json")

        drifted_manifest = copy.deepcopy(manifest)
        drifted_manifest["items"][0]["prompt"] = "drifted prefix payload"
        write_json(child_dir / "manifest.json", drifted_manifest)
        child_receipt["manifest_sha256"] = digest(json_bytes(drifted_manifest))
        write_json(child_dir / "prepared.json", child_receipt)
        with self.assertRaisesRegex(ValueError, "expanded source subset does not match its parent"):
            load_inputs(self.root, "drift200", maximum_items=200)

        write_json(child_dir / "manifest.json", manifest)
        write_json(
            child_dir / "prepared.json",
            {
                "complete": True,
                "manifest_sha256": digest(json_bytes(manifest)),
                **{k: v for k, v in child_receipt.items() if k not in {"manifest_sha256"}},
            },
        )

        parent_receipt_path = self.parent50_dir / "prepared.json"
        parent_receipt = read_json(parent_receipt_path)
        drifted_parent_receipt = copy.deepcopy(parent_receipt)
        drifted_parent_receipt["at"] = "2026-09-04T00:00:00Z"
        write_json(parent_receipt_path, drifted_parent_receipt)
        consent = self._consent(manifest, ["voyage"])
        clients = {"gemini": FakeClient(3072), "voyage": FakeClient(1024)}
        with self.assertRaisesRegex(ValueError, "parent prepared receipt changed"):
            execute_comparison(
                self.root,
                "drift200",
                consent,
                clients=clients,
                queries=copy.deepcopy(self.queries),
                providers_subset=["voyage"],
                arms_subset=["voyage_image"],
                sleep=lambda _: None,
                maximum_items=200,
                max_new_requests=1,
            )
        self.assertEqual(clients["gemini"].calls, 0)
        self.assertEqual(clients["voyage"].calls, 0)

    def test_voyage_only_consent_permits_voyage_and_denies_gemini(self) -> None:
        self._seed_parent50_comparison()
        _result, manifest = self._prepare_200("voyage200")
        consent = self._consent(manifest, ["voyage"])

        denied_clients = {"gemini": FakeClient(3072), "voyage": FakeClient(1024)}
        with self.assertRaisesRegex(ValueError, "Voyage-image-only"):
            execute_comparison(
                self.root,
                "voyage200",
                consent,
                clients=denied_clients,
                queries=copy.deepcopy(self.queries),
                providers_subset=["gemini"],
                arms_subset=["gemini_image"],
                sleep=lambda _: None,
                maximum_items=200,
                max_new_requests=1,
            )
        self.assertEqual(denied_clients["gemini"].calls, 0)
        self.assertEqual(denied_clients["voyage"].calls, 0)

        allowed_clients = {"gemini": FakeClient(3072), "voyage": FakeClient(1024)}
        result = execute_comparison(
            self.root,
            "voyage200",
            consent,
            clients=allowed_clients,
            queries=copy.deepcopy(self.queries),
            providers_subset=["voyage"],
            arms_subset=["voyage_image"],
            sleep=lambda _: None,
            maximum_items=200,
            max_new_requests=1,
        )
        self.assertEqual(result["new_requests_this_invocation"], 1)
        self.assertEqual(allowed_clients["gemini"].calls, 0)
        self.assertEqual(allowed_clients["voyage"].calls, 1)

    def test_failed_parent_request_key_is_not_retried_in_child200(self) -> None:
        self._seed_parent50_comparison(completed_selected=52, failed_selected=1, unrelated_failed=0, reserved_usd=0.001)
        _result, manifest = self._prepare_200("failedkey200")
        consent = self._consent(manifest, ["voyage"])
        clients = {"gemini": FakeClient(3072), "voyage": FakeClient(1024)}

        with self.assertRaisesRegex(ValueError, "manual investigation required"):
            execute_comparison(
                self.root,
                "failedkey200",
                consent,
                clients=clients,
                queries=copy.deepcopy(self.queries),
                providers_subset=["voyage"],
                arms_subset=["voyage_image"],
                sleep=lambda _: None,
                maximum_items=200,
            )
        self.assertEqual(clients["gemini"].calls, 0)
        self.assertEqual(clients["voyage"].calls, 0)


if __name__ == "__main__":
    unittest.main()
