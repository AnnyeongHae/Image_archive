from __future__ import annotations

import copy
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image
from PIL.PngImagePlugin import PngInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from image_rag_eval import incremental
from image_rag_eval.comparison import request_key
from image_rag_eval.experiment import digest, json_bytes, read_json, run_path, write_json
from image_rag_eval.expansion import _prepare_item


def identity(item_id: str, file_sha: str, pixel_sha: str, *, prompt: str = "plain") -> dict:
    return {"id": item_id, "style_id": item_id, "sha256": file_sha * 64,
            "signals": {"sha256": file_sha * 64, "pixel_sha256": pixel_sha * 64}, "prompt": prompt}


class IncrementalSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = [{"asset_id": f"asset-{n}", "catalog_key": f"legacy:CASE-{n:03d}",
                      "record_id": f"record-{n}", "asset_index": 0, "asset_sha256": "a" * 64,
                      "style_id": f"CASE-{n:03d}", "lane": "legacy"} for n in (5, 1, 3, 2, 4)]
        self.public = {row["style_id"] for row in self.rows}

    def test_numeric_selection_is_unsampled_case_only_and_raw_capped(self) -> None:
        pool = self.rows + [{**self.rows[0], "asset_id": "wrong-lane", "lane": "secret_code"},
                            {**self.rows[0], "asset_id": "unlisted", "style_id": "CASE-999"}]
        ref = [{"id": "asset-2", "catalog_key": "legacy:CASE-002", "style_id": "CASE-002", "lane": "legacy"}]
        selected, report = incremental.select_case_candidates(pool, self.public, ref, 3)
        self.assertEqual([row["style_id"] for row in selected], ["CASE-001", "CASE-003", "CASE-004"])
        self.assertEqual(report["unsampled_case_records"], 4)
        self.assertEqual(report["remaining_unsampled_case_records"], 1)
        self.assertTrue(report["selection_cap_applied_before_dedup"])

    def test_invalid_bounds_rejected(self) -> None:
        for count in (0, 301, -1, True, "300", 1.5):
            with self.subTest(count=count), self.assertRaises(ValueError):
                incremental.select_case_candidates(self.rows, self.public, [], count)

    def test_missing_public_record_and_missing_original_hash_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "public CASE allowlist"):
            incremental.select_case_candidates(self.rows[:-1], self.public, [], 3)
        rows = copy.deepcopy(self.rows)
        next(row for row in rows if row["style_id"] == "CASE-001")["asset_sha256"] = ""
        with self.assertRaisesRegex(ValueError, "no original file identity"):
            incremental.select_case_candidates(rows, self.public, [], 3)

    def test_exact_pixel_alias_uses_frozen_old_keeper_despite_better_new_prompt(self) -> None:
        old = [identity("old", "a", "b"), identity("old-alias", "c", "b")]
        incoming = [identity("new-json", "d", "b", prompt='{"well":"structured"}')]
        stage1 = {"active_ids": ["old"], "archived": [{"id": "old-alias", "representative_id": "old"}]}
        ids, routes = incremental.route_exact_aliases(incoming, old, stage1)
        self.assertEqual(ids, [])
        self.assertEqual(routes[0]["representative_id"], "old")
        self.assertEqual(routes[0]["matched_alias_ids"], ["old", "old-alias"])
        self.assertEqual(routes[0]["match_kinds"], ["exact_pixels"])
        self.assertFalse(routes[0]["replacement_of_existing_keeper"])
        self.assertEqual(incoming[0]["prompt"], '{"well":"structured"}')

    def test_incoming_duplicates_collapse_before_embedding_and_prompt_alone_does_not(self) -> None:
        old = [identity("old", "a", "b")]
        incoming = [identity("one", "c", "d"), identity("alias", "e", "d"), identity("different", "f", "1")]
        ids, routes = incremental.route_exact_aliases(incoming, old, {"active_ids": ["old"], "archived": []})
        self.assertEqual(ids, ["one", "different"])
        self.assertEqual(routes[0]["reference_scope"], "incoming")
        self.assertEqual(routes[0]["representative_id"], "one")

    def test_ambiguous_old_keepers_and_sampled_ids_are_rejected(self) -> None:
        old = [identity("old", "a", "b"), identity("other-old", "c", "b")]
        stage1 = {"active_ids": ["old", "other-old"], "archived": []}
        with self.assertRaisesRegex(ValueError, "multiple frozen keepers"):
            incremental.route_exact_aliases([identity("new", "d", "b")], old, stage1)
        with self.assertRaisesRegex(ValueError, "unique and unsampled"):
            incremental.route_exact_aliases([identity("old", "a", "b")], old, stage1)

    def test_later_useful_json_wins_only_inside_new_exact_component(self) -> None:
        old = [identity("old", "a", "b")]
        incoming = [identity("first-plain", "c", "d", prompt="a landscape"),
                    identity("later-json", "e", "d", prompt='{"subject":"a landscape","lighting":"soft","style":"watercolor"}')]
        before = copy.deepcopy(incoming)
        ids, routes = incremental.route_exact_aliases(incoming, old, {"active_ids": ["old"], "archived": []})
        self.assertEqual(ids, ["later-json"])
        self.assertEqual(routes[0]["id"], "first-plain")
        self.assertEqual(routes[0]["representative_id"], "later-json")
        self.assertEqual(routes[0]["representative_prompt_priority"]["tier"], 1)
        self.assertEqual(incoming, before)

    def test_new_hash_bridge_unions_components_before_keeper_selection(self) -> None:
        old = [identity("old", "a", "b")]
        # Synthetic hashes exercise transitive equivalence independent of the
        # image decoder: the bridge connects the two previously separate roots.
        incoming = [identity("left", "c", "d"), identity("right", "e", "f"),
                    identity("bridge", "c", "f", prompt='{"subject":"landscape","lighting":"soft","style":"watercolor"}')]
        ids, routes = incremental.route_exact_aliases(incoming, old, {"active_ids": ["old"], "archived": []})
        self.assertEqual(ids, ["bridge"])
        self.assertEqual({row["representative_id"] for row in routes}, {"bridge"})
        self.assertEqual(routes[0]["exact_component_ids"], ["left", "right", "bridge"])
        self.assertEqual(len(routes[0]["component_identity_edges"]), 2)

    def test_incoming_bridge_to_one_old_keeper_does_not_replace_it(self) -> None:
        old = [identity("old", "a", "b")]
        incoming = [identity("initially-new", "c", "d"), identity("bridge", "a", "d",
                     prompt='{"subject":"landscape","lighting":"soft","style":"watercolor"}')]
        ids, routes = incremental.route_exact_aliases(incoming, old, {"active_ids": ["old"], "archived": []})
        self.assertEqual(ids, [])
        self.assertEqual({row["representative_id"] for row in routes}, {"old"})
        self.assertTrue(all(row["reference_scope"] == "existing" for row in routes))


class IncrementalPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.reference_id = "reference200-fixture"
        self.new_id = "incoming300-fixture"
        for name, value in (("REFERENCE_COUNT", 2), ("PUBLIC_CASE_COUNT", 4)):
            patcher = mock.patch.object(incremental, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        fixtures = self.root / "fixtures"
        fixtures.mkdir()
        red = Image.new("RGB", (40, 60), "red")
        red.save(fixtures / "old.png")
        info = PngInfo()
        info.add_text("source", "same pixels another file")
        red.save(fixtures / "case2.png", pnginfo=info)
        Image.new("RGB", (48, 64), "blue").save(fixtures / "case3.png")
        (fixtures / "case4.png").write_bytes((fixtures / "case3.png").read_bytes())
        self.reference = run_path(self.root, self.reference_id)
        (self.reference / "inputs").mkdir(parents=True)
        parent_items = []
        for item_id, style, lane, catalog in (("old", "CASE-001", "legacy", "legacy:CASE-001"),
                                              ("old-alias", "EXT-001", "external", "external:EXT-001")):
            item, data = _prepare_item(self.root, {"id": item_id, "style_id": style, "lane": lane, "catalog_key": catalog,
                "path": "fixtures/old.png", "prompt": "old prompt", "sha256": digest((fixtures / "old.png").read_bytes())})
            (self.reference / item["prepared_path"]).write_bytes(data)
            parent_items.append(item)
        self.parent = {"items": parent_items, "preprocessing": "fixture"}
        write_json(self.reference / "manifest.json", self.parent)
        write_json(self.reference / "prepared.json", {"complete": True, "manifest_sha256": digest(json_bytes(self.parent))})
        spec = {"schema_version": "image-group-workflow-spec-1", "run_id": self.reference_id,
                "source_manifest_sha256": digest(json_bytes(self.parent)),
                "stage1": {"active_ids": ["old"], "archived": [{"id": "old-alias", "representative_id": "old"}]}}
        spec["spec_sha256"] = digest(json_bytes(spec))
        (self.reference / "group-workflow-v1").mkdir()
        write_json(self.reference / incremental.REFERENCE_SPEC_PATH, spec)
        comparison = self.reference / "comparison-v1"
        comparison.mkdir()
        write_json(comparison / "vectors.json", {"voyage_image": {"old": [1.0], "old-alias": [1.0]}})
        write_json(comparison / "budget.json", {"attempts": []})
        catalog = self.root / incremental.PUBLIC_CATALOG_PATH
        catalog.parent.mkdir(parents=True)
        catalog.write_text("window.DETAILPAGE_CATALOG_META = {};\nwindow.DETAILPAGE_CATALOG_CASES = "
                           + json.dumps([{"id": n} for n in range(1, 5)]) + ";\n", encoding="utf-8")
        canonical = incremental.dataset._canonical_path(self.root)
        canonical.parent.mkdir(parents=True)
        db_path = incremental.dataset._duplicate_index_path(self.root)
        db_path.parent.mkdir(parents=True)
        connection = sqlite3.connect(db_path)
        connection.executescript("CREATE TABLE meta(key TEXT, value_json TEXT);"
            "CREATE TABLE records(catalog_key TEXT,style_id TEXT,record_id TEXT,lane TEXT,title TEXT,source_name TEXT,source_url TEXT,rights_status TEXT,review_status TEXT);"
            "CREATE TABLE assets(asset_id TEXT,catalog_key TEXT,asset_index INTEGER,asset_sha256 TEXT);")
        connection.execute("INSERT INTO meta VALUES (?,?)", ("index_schema_version", json.dumps(incremental.dataset.INDEX_SCHEMA_VERSION)))
        raws = []
        for n in range(1, 5):
            style = f"CASE-{n:03d}"
            catalog_key = "legacy:" + style
            path = f"fixtures/{'old' if n == 1 else 'case' + str(n)}.png"
            asset_id = "old" if n == 1 else "incoming-" + str(n)
            connection.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?)",
                               (catalog_key, style, "record-" + str(n), "legacy", "fixture", "fixture", "", "not_cleared", "needs_review"))
            connection.execute("INSERT INTO assets VALUES (?,?,?,?)", (asset_id, catalog_key, 0, digest((self.root / path).read_bytes())))
            raws.append({"catalog_key": catalog_key, "style_id": style, "lane": "legacy",
                         "prompt": {"text": '{"subject":"fixture"}'}, "media": {"assets": [{"uri_kind": "local", "uri": path}]}})
        connection.commit()
        connection.close()
        canonical.write_text("\n".join(json.dumps(row) for row in raws), encoding="utf-8")
        overlay = incremental.dataset._remote_overlay_path(self.root)
        overlay.parent.mkdir(parents=True)
        write_json(overlay, {"entries": []})

    def prepare(self, *, apply: bool = False, max_records: int = 3) -> dict:
        return incremental.prepare_incremental_batch(self.root, self.reference_id, self.new_id,
                                                     max_records=max_records, apply=apply)

    def test_dry_run_has_no_writes_and_reports_dedup_before_embeddings(self) -> None:
        before = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*"))
        result = self.prepare()
        after = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob("*"))
        self.assertEqual(before, after)
        self.assertEqual(result["writes"], 0)
        self.assertEqual(result["selected_raw_records"], 3)
        self.assertEqual(result["existing_alias_records"], 1)
        self.assertEqual(result["incoming_duplicate_records"], 1)
        self.assertEqual(result["novel_representative_records"], 1)
        self.assertEqual(result["new_image_request_count"], 1)
        self.assertEqual(result["network_calls"], 0)

    def test_apply_creates_separate_prepared_run_and_validator_accepts_it(self) -> None:
        old_digest = digest((self.reference / "manifest.json").read_bytes())
        result = self.prepare(apply=True)
        self.assertEqual(result["status"], "prepared_local_only")
        manifest, bindings = incremental.validate_incremental_prepared(self.root, self.new_id)
        self.assertEqual(manifest["embedding_item_ids"], ["incoming-3"])
        self.assertEqual(len(manifest["items"]), 3)
        self.assertEqual(bindings["reference_ids"], ["old", "old-alias"])
        self.assertEqual(digest((self.reference / "manifest.json").read_bytes()), old_digest)
        self.assertEqual(manifest["semantic_group_matching_status"], "pending_new_vectors_and_imported_human_decisions")
        self.assertTrue(all(item["external_ai_approved"] is False for item in manifest["items"]))

    def test_raw_cap_does_not_refill_after_dedup(self) -> None:
        result = self.prepare(max_records=1)
        self.assertEqual(result["selected_raw_records"], 1)
        self.assertEqual(result["novel_representative_records"], 0)
        self.assertEqual(result["new_image_request_count"], 0)
        self.assertEqual(result["remaining_unsampled_case_records"], 2)

    def test_original_source_drift_is_rejected_before_apply(self) -> None:
        Image.new("RGB", (40, 60), "green").save(self.root / "fixtures/old.png")
        with self.assertRaisesRegex(ValueError, "source item digest changed"):
            self.prepare(apply=True)
        self.assertFalse(run_path(self.root, self.new_id).exists())

    def test_source_binding_drift_blocks_a_prepared_run(self) -> None:
        self.prepare(apply=True)
        canonical = incremental.dataset._canonical_path(self.root)
        canonical.write_text(canonical.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "source drift"):
            incremental.validate_incremental_prepared(self.root, self.new_id)

    def test_prepared_image_mutation_is_rejected(self) -> None:
        self.prepare(apply=True)
        destination = run_path(self.root, self.new_id)
        manifest = read_json(destination / "manifest.json")
        (destination / manifest["items"][0]["prepared_path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "prepared input digest changed"):
            incremental.validate_incremental_prepared(self.root, self.new_id)

    def test_existing_run_and_new_human_decisions_are_never_overwritten(self) -> None:
        self.prepare(apply=True)
        with self.assertRaises(FileExistsError):
            self.prepare(apply=True)
        receipt = self.reference / "group-workflow-v1/decision-imports/new/receipt.json"
        receipt.parent.mkdir(parents=True)
        write_json(receipt, {"human": True})
        with self.assertRaisesRegex(ValueError, "human decisions changed"):
            incremental.validate_incremental_prepared(self.root, self.new_id)

    def test_bound_completed_cache_can_be_reused_without_copy_or_calls(self) -> None:
        manifest, _bindings, plan, _inputs = incremental.build_incremental_payloads(self.root, self.reference_id, self.new_id, max_records=3)
        request = plan["embedding_requests"][0]
        self.assertEqual(request["key"], request_key(request))
        cache = self.reference / "comparison-v1/vector-cache"
        cache.mkdir()
        vector = [1.0] + [0.0] * 1023
        write_json(cache / (request["key"] + ".json"), {"key": request["key"], "model": request["model"],
            "vector": vector, "vector_sha256": digest(json_bytes(vector))})
        write_json(self.reference / "comparison-v1/budget.json", {"attempts": [{"key": request["key"], "status": "completed"}]})
        result = self.prepare()
        self.assertEqual(result["new_image_request_count"], 0)
        self.assertEqual(result["reusable_image_request_count"], 1)
        self.assertEqual(result["incremental_reserved_usd"], 0)


if __name__ == "__main__":
    unittest.main()
