from __future__ import annotations

import argparse
import base64
import json
import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from image_rag_eval.group_review_ui import render_group_review  # noqa: E402
from image_rag_eval.group_workflow import validate_group_workflow_decisions  # noqa: E402


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9WnK8yQAAAAASUVORK5CYII="
)


def _fixture_spec(root: Path) -> dict:
    inputs_dir = root / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    items = []
    item_defs = [
        ("A", "CASE001"),
        ("B", "CASE002"),
        ("C", "CASE003"),
        ("D", "CASE004"),
        ("E", "BST001"),
        ("F", "BST002"),
    ]
    for index, (ident, style_id) in enumerate(item_defs, start=1):
        image_path = inputs_dir / f"{ident}.png"
        image_path.write_bytes(PNG_BYTES)
        items.append(
            {
                "id": ident,
                "style_id": style_id,
                "prepared_path": f"../inputs/{ident}.png",
                "source_sha256": f"source-{ident}",
                "prepared_sha256": f"prepared-{ident}",
                "prompt_sha256": f"prompt-{ident}",
                "priority": {"rank_index": index, "tier": 1, "label": f"p{index}"},
            }
        )
    return {
        "schema_version": "image-group-workflow-spec-1",
        "approval_policy": "default_retained_images_after_review_v1",
        "spec_sha256": "spec-fixture-qa-001",
        "run_id": "fixture-group-review-qa",
        "source_manifest_sha256": "manifest-fixture-qa-001",
        "vector_fingerprint": "voyage-fixture-qa-001",
        "source_labels_sha256": "labels-fixture-qa-001",
        "created_at": "2026-09-03T11:00:00Z",
        "items": items,
        "stage1": {
            "active_ids": ["A", "B", "C", "D", "E"],
            "archived": [{"id": "F", "representative_id": "E", "reason": "existing duplicate"}],
            "alias_lineage": [{"from": "F", "to": "E"}],
            "policy": "retention-v1",
        },
        "duplicate_candidates": [
            {
                "id": "dup-main",
                "member_ids": ["A", "B", "C", "D"],
                "suggested_representative_id": "A",
                "representative_priority_ids": ["B", "A", "C", "D"],
                "evidence": {"kind": "pixel_or_identity_candidate"},
            }
        ],
        "similarity_candidates": [
            {
                "id": "sim-main",
                "member_ids": ["A", "B", "D", "E"],
                "candidate_only": True,
                "evidence": {"kind": "voyage-high-similarity"},
                "known_positive_pairs": [{"left_id": "B", "right_id": "E", "human_label": "near_duplicate"}],
                "known_negative_pairs": [{"left_id": "A", "right_id": "E", "human_label": "unrelated"}],
            }
        ],
    }


def _load_actual_spec(spec_path: Path) -> dict:
    return json.loads(spec_path.read_text(encoding="utf-8"))


def _item_map(spec: dict) -> dict[str, dict]:
    return {
        str(item.get("id")).strip(): item
        for item in spec.get("items", [])
        if isinstance(item, dict) and str(item.get("id", "")).strip()
    }


def _pick_actual_duplicate_candidate(spec: dict) -> dict:
    item_map = _item_map(spec)
    preferred_style_ids = {"API-049", "ERK-1365"}
    for candidate in spec.get("duplicate_candidates", []):
        if not isinstance(candidate, dict):
            continue
        member_style_ids = {
            str(item_map.get(str(member_id).strip(), {}).get("style_id", "")).strip()
            for member_id in candidate.get("member_ids", [])
        }
        if preferred_style_ids.issubset(member_style_ids):
            return candidate
    for candidate in spec.get("duplicate_candidates", []):
        if isinstance(candidate, dict):
            return candidate
    raise ValueError("actual smoke requires at least one duplicate candidate")


def _pick_actual_similarity_candidate(spec: dict) -> dict:
    candidates = [
        candidate
        for candidate in spec.get("similarity_candidates", [])
        if isinstance(candidate, dict) and len(candidate.get("member_ids", [])) >= 3
    ]
    if not candidates:
        raise ValueError("actual smoke requires at least one 3+ member similarity candidate")
    candidates.sort(key=lambda candidate: (-len(candidate.get("member_ids", [])), str(candidate.get("id", "")).strip()))
    return candidates[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Isolated v3 default-approval UI QA; never imports synthetic decisions.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output-dir")
    parser.add_argument("--html-path")
    parser.add_argument("--spec-path")
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({"status": "dry_run", "network_calls": 0, "writes": 0}))
        return
    from playwright.sync_api import sync_playwright
    temp = tempfile.TemporaryDirectory()
    try:
        output_dir = Path(args.output_dir).resolve() if args.output_dir else Path(temp.name)
        output_dir.mkdir(parents=True, exist_ok=True)
        actual = bool(args.html_path or args.spec_path)
        if actual:
            if not args.html_path or not args.spec_path:
                raise ValueError("actual smoke requires both --html-path and --spec-path")
            spec = _load_actual_spec(Path(args.spec_path))
            html_path = Path(args.html_path).resolve()
        else:
            run = output_dir / "fixture-group-review-qa"
            spec = _fixture_spec(run)
            workflow = run / "group-workflow-v1"
            workflow.mkdir(parents=True, exist_ok=True)
            html_path = workflow / "group-review.html"
            html_path.write_text(render_group_review(spec), encoding="utf-8")
        result = {"mode": "actual" if actual else "fixture", "synthetic_decisions": True,
                  "human_approval": False, "production_decision_imports": 0, "external_network_allowed": False}
        errors = []
        with sync_playwright() as api:
            browser = api.chromium.launch(channel="msedge", headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1100}, accept_downloads=True)
            context.route("http://**/*", lambda route: route.abort())
            context.route("https://**/*", lambda route: route.abort())
            page = context.new_page()
            page.on("pageerror", lambda err: errors.append(str(err)))
            page.goto(html_path.as_uri(), wait_until="load")
            assert page.locator("#stage-4").is_hidden()
            assert page.locator("#stage-5").count() == 0
            with page.expect_download() as info:
                page.locator("#download").click()
            draft = json.loads(Path(info.value.path()).read_text(encoding="utf-8"))
            assert draft["schema_version"] == "image-group-workflow-draft-3"
            assert draft["decisions"]["reviewer"] == ""
            assert page.locator("#export-json").input_value()
            result["blank_reviewer_draft_and_json_fallback"] = True

            if not actual:
                legacy = page.evaluate("clone(template)")
                legacy.update(schema_version="image-group-workflow-decisions-1", reviewer="fixture-reviewer",
                              individual_approvals=[{"id": "A", "approved": False, "tags_text": ""}],
                              front_review_complete=True)
                legacy["duplicate_reviews"][0].update(decision="same_image_subset", selected_ids=["B"])
                legacy["similarity_reviews"][0].update(decision="defer", selected_ids=["D"])
                raw = json.dumps(legacy)
                page.evaluate("raw => localStorage.setItem(draftKey, raw)", raw)
                page.reload(wait_until="load")
                assert page.locator('input[data-duplicate-member="dup-main"][value="B"]').is_checked()
                assert page.locator('input[data-similarity-member="sim-main"][value="D"]').is_checked()
                assert page.evaluate("imageChoices.A.approved === false")
                backups = page.evaluate("Object.keys(localStorage).filter(k=>k.startsWith(draftKey+':recovery:')).map(k=>localStorage.getItem(k))")
                assert raw in backups
                assert page.locator("#stage-4").is_hidden()
                assert page.locator("#front-preview .preview-card").count() == 0
                result["v1_persisted_singleton_and_disabled_check_migration"] = True
                result["raw_recovery_backup"] = True
                page.locator('input[data-duplicate-member="dup-main"][value="D"]').check()
                page.locator('input[data-duplicate-remainder="dup-main"]').check()
                assert page.locator('input[data-similarity-member="sim-main"][value="D"]').is_disabled()
                assert page.locator('input[data-similarity-member="sim-main"][value="D"]').is_checked()
                page.locator('input[name="sim-decision-sim-main"][value="approve_selected"]').check()
                page.locator('button[data-select-all-similarity="sim-main"]').click()
                assert "known negative pair" in page.locator("#similarity-status-sim-main").inner_text()
                assert page.locator("#stage-4").is_hidden()
                page.locator('button[data-clear-similarity="sim-main"]').click()
                for member in ["B", "E"]:
                    page.locator(f'input[data-similarity-member="sim-main"][value="{member}"]').check()
                assert page.locator("#stage-4").is_visible()
                assert page.locator('input[data-image-approval="B"]').is_checked()
                assert page.locator('input[data-image-approval="C"]').is_checked()
                assert not page.locator('input[data-image-approval="A"]').is_checked()
                assert set(page.locator("#front-preview .mono").all_text_contents()) == {"B", "C", "E"}
                assert page.locator('[data-image-memo="E"]').input_value() == ""
                page.locator('input[data-image-approval="E"]').uncheck()
                page.locator('[data-image-memo="C"]').fill("이 구도를 상세페이지 첫 장에 활용")
                ids_expected = {"B", "C"}
                result.update({"gates_block_new_defaults": True, "default_retained_approval_after_gates": True,
                               "explicit_false_preserved": True, "group_member_can_be_unchecked": True,
                               "per_image_freeform_memo_optional": True})
            else:
                page.locator("#reviewer").fill("SYNTHETIC_QA_ONLY")
                page.evaluate("""() => {
                  for(const r of state.duplicate_reviews)r.decision='distinct_images';
                  for(const r of state.similarity_reviews)r.decision='keep_separate';
                  refreshUi();
                }""")
                assert page.locator("#stage-4").is_visible()
                selected = page.evaluate("""() => {
                  const chosen=spec.similarity_candidates.find(c=>c.member_ids.length>=2 &&
                    c.member_ids.some(id=>!readOnlyIds.has(id)) && !blockedNegativePairs(c,c.member_ids).length);
                  if(!chosen)return null;
                  const row=state.similarity_reviews.find(r=>r.candidate_id===chosen.id);
                  row.selected_ids=[...chosen.member_ids];row.decision='approve_selected';refreshUi();
                  return {id:chosen.id,member_ids:chosen.member_ids};
                }""")
                expected = page.evaluate("frontPreviewIds(snapshot(),collectState())")
                excluded = page.evaluate("[...snapshot().active].find(id=>!readOnlyIds.has(id)&&imageChoice(id).approved)")
                if excluded:
                    page.locator(f'input[data-image-approval="{excluded}"]').first.uncheck()
                    expected.remove(excluded)
                ids_expected = set(expected)
                result["actual_default_count_before_uncheck"] = len(expected) + bool(excluded)
                result["actual_candidate_checked"] = selected
            with page.expect_download() as info:
                page.locator("#download-decisions").click()
            decisions = json.loads(Path(info.value.path()).read_text(encoding="utf-8"))
            assert decisions["schema_version"] == "image-group-workflow-decisions-3"
            assert "front_review_complete" not in decisions
            assert "individual_approvals" not in decisions
            normalized = validate_group_workflow_decisions(spec, decisions)
            ids = {row["id"] for row in normalized["private_front_export_items"]}
            assert ids == ids_expected, (ids, ids_expected)
            assert set(page.locator("#front-preview .mono").all_text_contents()) == ids
            result["backend_validator_front_matches_preview"] = True
            result["front_count"] = len(ids)
            if not actual:
                memo = next(row for row in normalized["private_front_export_items"] if row["id"] == "C")
                assert memo["memo_text"] == "이 구도를 상세페이지 첫 장에 활용"
                long_memo = "한" * 2667
                page.locator('[data-image-memo="C"]').fill(long_memo)
                page.locator("#download-decisions").click()
                assert "8,000바이트" in page.locator("#message").inner_text()
                with page.expect_download() as info:
                    page.locator("#download").click()
                memo_draft = json.loads(Path(info.value.path()).read_text(encoding="utf-8"))
                assert memo_draft["image_choices"]["C"]["memo_text"] == long_memo
                page.locator('[data-image-memo="C"]').fill("이 구도를 상세페이지 첫 장에 활용")
                result["oversized_memo_blocks_review_export_but_survives_draft"] = True
                page.locator('input[data-similarity-member="sim-main"][value="B"]').uncheck()
                assert page.locator("#stage-4").is_hidden()
                assert page.locator("#front-preview .preview-card").count() == 0
                assert page.evaluate("imageChoices.E.approved===false")
                page.locator('input[data-similarity-member="sim-main"][value="B"]').check()
                assert page.locator("#stage-4").is_hidden()
                page.locator('input[name="sim-decision-sim-main"][value="approve_selected"]').check()
                assert set(page.locator("#front-preview .mono").all_text_contents()) == {"B", "C"}
                assert page.locator('[data-image-memo="C"]').input_value() == "이 구도를 상세페이지 첫 장에 활용"
                result["upstream_membership_change_requires_regate_preserves_exclusions"] = True
                with page.expect_download() as info:
                    page.locator("#download").click()
                current_draft = json.loads(Path(info.value.path()).read_text(encoding="utf-8"))
                page.reload(wait_until="load")
                assert page.evaluate("imageChoices") == current_draft["image_choices"]
                result["v3_draft_roundtrip"] = True

                v2 = json.loads(json.dumps(decisions))
                v2["schema_version"] = "image-group-workflow-decisions-2"
                v2.pop("image_approvals")
                v2["group_approvals"] = [{"candidate_id": "sim-main", "approved": False, "tags_text": ""}]
                v2["individual_approvals"] = [{"id": "A", "approved": False, "tags_text": ""},
                                             {"id": "C", "approved": True, "tags_text": ""}]
                page.evaluate("p=>applyState(validateImportedDecisions(p))", v2)
                assert set(page.locator("#front-preview .mono").all_text_contents()) == {"C"}
                result["v2_false_group_and_individual_migrate_without_default_override"] = True

                # Independent groups only collapse equal/full subsets, never partial overlaps.
                collapse = json.loads(json.dumps(spec))
                collapse["run_id"], collapse["spec_sha256"] = "collapse-fixture", "collapse-spec"
                collapse["duplicate_candidates"] = []
                collapse["similarity_candidates"] = [
                    {"id": name, "member_ids": members, "known_negative_pairs": [], "candidate_only": True}
                    for name, members in [("large", ["A", "B", "C", "E"]), ("small", ["A", "B"]),
                                          ("equal", ["A", "B", "C", "E"]), ("partial", ["A", "D"])]]
                collapse_path = html_path.parent / "collapse.html"
                collapse_path.write_text(render_group_review(collapse), encoding="utf-8")
                extra = context.new_page()
                extra.on("pageerror", lambda err: errors.append(str(err)))
                extra.goto(collapse_path.as_uri(), wait_until="load")
                extra.evaluate("""() => {
                  for(const r of state.similarity_reviews){r.decision='approve_selected';r.selected_ids=[...similarityCandidateMap.get(r.candidate_id).member_ids];}
                  refreshUi();
                }""")
                displayed = extra.evaluate("collapseApprovedGroups(snapshot())")
                assert len(displayed) == 2
                largest = max(displayed, key=lambda row: len(row["member_ids"]))
                assert set(largest["source_candidate_ids"]) == {"large", "small", "equal"}
                assert set(largest["member_ids"]) == {"A", "B", "C", "E"}
                assert extra.locator('[data-display-group]').count() == 2
                extra.locator('input[data-image-approval="A"]').first.uncheck()
                assert all(not extra.locator('input[data-image-approval="A"]').nth(i).is_checked()
                           for i in range(extra.locator('input[data-image-approval="A"]').count()))
                result["equal_subset_collapsed_with_provenance_no_partial_union"] = True

                combined = json.loads(json.dumps(spec))
                combined.update(run_id="baseline-fixture", spec_sha256="baseline-spec",
                    baseline={"read_only_ids": ["A", "B"], "image_approvals": [
                        {"id": "A", "approved": True, "memo_text": "기존 영감"},
                        {"id": "B", "approved": False, "memo_text": "기존 미승인"}],
                        "groups": [{"id": "old-group", "member_ids": ["A", "B"]}]})
                combined["duplicate_candidates"] = [{"id": "dup-main", "member_ids": ["A", "D"],
                    "baseline_anchor_ids": ["A"], "suggested_representative_id": "A", "representative_priority_ids": ["A", "D"]}]
                combined["similarity_candidates"] = [{"id": "sim-main", "member_ids": ["A", "B", "C", "E"],
                    "baseline_anchor_ids": ["A", "B"], "known_negative_pairs": [], "candidate_only": True}]
                baseline_path = html_path.parent / "baseline.html"
                baseline_path.write_text(render_group_review(combined), encoding="utf-8")
                extra.goto(baseline_path.as_uri(), wait_until="load")
                assert extra.locator("#baseline-context").is_visible()
                assert extra.locator('input[data-duplicate-member="dup-main"][value="A"]').is_disabled()
                assert extra.locator('input[data-duplicate-member="dup-main"][value="A"]').is_checked()
                extra.locator('input[data-duplicate-member="dup-main"][value="D"]').check()
                extra.locator('input[name="dup-decision-dup-main"][value="same_image_subset"]').check()
                extra.locator('button[data-clear-similarity="sim-main"]').click()
                assert extra.locator('input[data-similarity-member="sim-main"][value="A"]').is_checked()
                assert extra.locator('input[data-similarity-member="sim-main"][value="B"]').is_disabled()
                extra.locator('button[data-select-all-similarity="sim-main"]').click()
                extra.locator('input[name="sim-decision-sim-main"][value="approve_selected"]').check()
                assert extra.locator('#stage-4 input[data-image-approval="A"]').count() == 0
                assert set(extra.locator("#front-preview .mono").all_text_contents()) == {"A", "C", "E"}
                combined_decisions = extra.evaluate("({...collectState(),reviewer:'SYNTHETIC_QA',reviewed_at:new Date().toISOString()})")
                assert {r["id"] for r in combined_decisions["image_approvals"]} == {"C", "E"}
                actual_front = validate_group_workflow_decisions(combined, combined_decisions)["private_front_export_items"]
                assert {r["id"] for r in actual_front} == {"A", "C", "E"}
                result["baseline_readonly_false_preserved_anchors_locked_and_front_matches"] = True
                extra.close()

                malformed = "{bad json"
                page.evaluate("raw=>localStorage.setItem(draftKey,raw)", malformed)
                page.reload(wait_until="load")
                page.locator("#reviewer").fill("must-not-clobber")
                assert page.evaluate("localStorage.getItem(draftKey)") == malformed
                assert page.locator("#export-json").input_value() == malformed
                result["failed_restore_does_not_clobber_raw"] = True
            page.set_viewport_size({"width": 390, "height": 844})
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
            result["mobile_horizontal_overflow"] = False
            result["js_errors"] = errors
            assert not errors, errors
            context.close()
            browser.close()
        result["status"] = "passed"
        report = output_dir / ("image-group-workflow.actual-v3-qa.json" if actual else "group-review-ui-v3-qa.json")
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
    finally:
        temp.cleanup()


if __name__ == "__main__":
    main()
