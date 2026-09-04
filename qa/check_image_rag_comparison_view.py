"""Verify the private retention/group view without permitting remote requests."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from image_rag_eval.experiment import read_json, run_path, write_json


def _group_style_ids(group) -> list[str]:
    return [text.strip() for text in group.locator("article.card h3").all_inner_texts() if text.strip()]


def _safe_relative_basename(value: str) -> str:
    candidate = Path(value)
    normalized = value.strip()
    assert normalized
    assert candidate.name == normalized
    assert normalized not in {".", ".."}
    return normalized


def _group_identity(group: dict) -> tuple[str, str]:
    return (
        str(group.get("provider") or "local"),
        str(group.get("group_id") or json.dumps(group, sort_keys=True, ensure_ascii=False)),
    )


def _dedupe_groups(groups: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    ordered: list[dict] = []
    for group in groups:
        key = _group_identity(group)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(group)
    return ordered


def _is_prompt_variant_group(group: dict) -> bool:
    kind = str(group.get("kind") or "").strip().casefold()
    return kind in {"prompt_variant", "prompt_exact"}


def _expected_group_specs(groups: list[dict], style_by_item_id: dict[str, str]) -> list[dict]:
    specs: list[dict] = []
    for group in _dedupe_groups(groups):
        member_ids = [member for member in group.get("member_ids", []) if isinstance(member, str) and member in style_by_item_id]
        if len(member_ids) < 2:
            continue
        representative_id = str(group.get("representative_id") or "")
        representative_style_id = style_by_item_id.get(representative_id, style_by_item_id[member_ids[0]])
        specs.append(
            {
                "group_id": str(group.get("group_id") or ""),
                "member_style_ids": [style_by_item_id[member_id] for member_id in member_ids],
                "member_style_id_set": {style_by_item_id[member_id] for member_id in member_ids},
                "representative_style_id": representative_style_id,
            }
        )
    return specs


def _matching_group_indices(section_groups, expected_members: set[str]) -> list[int]:
    matches: list[int] = []
    for index in range(section_groups.count()):
        group = section_groups.nth(index)
        if set(_group_style_ids(group)) == expected_members:
            matches.append(index)
    return matches


def _containing_group_indices(section_groups, expected_members: set[str]) -> list[int]:
    matches: list[int] = []
    for index in range(section_groups.count()):
        group = section_groups.nth(index)
        if expected_members <= set(_group_style_ids(group)):
            matches.append(index)
    return matches


def _evaluation_style_ids(retrieval_section) -> set[str]:
    cells = retrieval_section.locator("article.eval-card tbody tr td:first-child")
    return {text.strip() for text in cells.all_inner_texts() if text.strip()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--source-run-id", default="2026-09-03-embedding-ab-v1")
    parser.add_argument("--comparison-dir", default="comparison-v1")
    parser.add_argument("--results-file", default="comparison-results-v1.html")
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({"status": "dry_run", "writes": 0, "network_calls": 0}))
        return
    from playwright.sync_api import sync_playwright

    root = Path(__file__).resolve().parents[1]
    source = run_path(root, args.source_run_id)
    comparison_dir = _safe_relative_basename(args.comparison_dir)
    results_file = _safe_relative_basename(args.results_file)
    destination = source / comparison_dir
    qa_path = destination / "browser-qa.json"
    desktop_path = destination / "results-desktop.png"
    mobile_path = destination / "results-mobile.png"
    for path in (qa_path, desktop_path, mobile_path):
        if path.exists():
            raise FileExistsError(f"QA output already exists: {path.name}; never overwrite prior run evidence")
    manifest = read_json(destination / "manifest.json")
    retention = read_json(destination / "retention.json")
    queries = read_json(destination / "queries.json") if (destination / "queries.json").exists() else []
    evaluation = read_json(destination / "evaluation.json") if (destination / "evaluation.json").exists() else {"evaluations": []}
    family_checked = False
    prompt_variant_checked = False
    errors = []
    style_by_item_id = {
        str(item.get("id")): str(item.get("style_id") or item.get("id"))
        for item in manifest.get("items", [])
        if item.get("id")
    }
    active_expected = len(retention["active_ids"])
    archived_expected = len(retention["archived"])
    manifest_expected = len(manifest.get("items", []))
    evaluation_count = len(evaluation.get("evaluations", []))
    vector_counts = evaluation.get("vector_counts", {}) if isinstance(evaluation.get("vector_counts"), dict) else {}
    voyage_image_count = int(vector_counts.get("voyage_image", 0) or 0)
    voyage_query_count = int(vector_counts.get("voyage_queries", 0) or 0)
    if evaluation_count == 0 or voyage_image_count < manifest_expected or voyage_query_count < len(queries):
        raise ValueError(
            f"comparison page is not ready for browser QA: evaluation_count={evaluation_count}, "
            f"voyage_image={voyage_image_count}/{manifest_expected}, voyage_queries={voyage_query_count}/{len(queries)}"
        )
    raw_exact_groups = [group for group in retention.get("exact_groups", []) if isinstance(group, dict)]
    raw_prompt_variant_groups = [group for group in (retention.get("prompt_variant_groups") or []) if isinstance(group, dict)]
    expected_logical_groups = _expected_group_specs(raw_exact_groups, style_by_item_id)
    expected_prompt_variant_groups = _expected_group_specs(raw_prompt_variant_groups, style_by_item_id)
    has_explicit_prompt_variant_groups = isinstance(retention.get("prompt_variant_groups"), list)
    if has_explicit_prompt_variant_groups:
        logical_deletion_expected = len(_dedupe_groups(raw_exact_groups))
        prompt_variant_expected = len(_dedupe_groups(raw_prompt_variant_groups))
    else:
        logical_deletion_expected = len(_dedupe_groups([group for group in raw_exact_groups if not _is_prompt_variant_group(group)]))
        prompt_variant_expected = len(_dedupe_groups(raw_prompt_variant_groups + [group for group in raw_exact_groups if _is_prompt_variant_group(group)]))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.route("http://**/*", lambda route: route.abort())
        page.route("https://**/*", lambda route: route.abort())
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        page.goto((source / results_file).as_uri())
        active = page.locator("section").filter(has=page.get_by_role("heading", name="활성 카드", exact=True))
        assert active.locator("article.card").count() == active_expected
        archive_summary = page.locator("summary").filter(has_text="보관 항목 보기")
        archive = archive_summary.locator("..")
        assert not archive.evaluate("el => el.open")
        archive_summary.click()
        assert archive.evaluate("el => el.open")
        assert archive.locator("article.card").count() == archived_expected
        assert active_expected + archived_expected == manifest_expected
        retrieval_summary = page.locator("summary").filter(has_text="질의별 retrieval 결과")
        assert retrieval_summary.count() == 1
        retrieval_section = retrieval_summary.locator("..")
        logical_summary = page.locator("summary").filter(has_text="삭제 대상(논리삭제)")
        prompt_summary = page.locator("summary").filter(has_text="동일 프롬프트의 다른 결과")
        similar_summary = page.locator("summary").filter(has_text="시각 유사 그룹")
        if logical_summary.count() == 0:
            logical_summary = page.locator("summary").filter(has_text="exact group")
        if similar_summary.count() == 0:
            similar_summary = page.locator("summary").filter(has_text="similar group")
        assert logical_summary.count() == 1
        assert similar_summary.count() == 1
        logical_section = logical_summary.locator("..")
        similar_section = similar_summary.locator("..")
        group_nodes = page.locator("details.group")
        logical_deletion_group_count = logical_section.locator(":scope > details.group").count()
        prompt_variant_group_count = prompt_summary.locator("..").locator(":scope > details.group").count() if prompt_summary.count() == 1 else 0
        similar_group_count = similar_section.locator(":scope > details.group").count()
        group_count = group_nodes.count()
        for index in range(group_count):
            group = group_nodes.nth(index)
            group.evaluate("el => { for (let p=el.parentElement; p; p=p.parentElement) { if (p.tagName==='DETAILS') p.open=true; } el.open = true; }")
        assert logical_deletion_group_count == logical_deletion_expected
        logical_groups = logical_section.locator(":scope > details.group")
        for expected in expected_logical_groups:
            matches = _matching_group_indices(logical_groups, expected["member_style_id_set"])
            assert len(matches) == 1
            group = logical_groups.nth(matches[0])
            assert group.evaluate("el => el.open")
            assert group.locator("article.card").count() == len(expected["member_style_ids"])
            assert group.locator("article.card h3").first.inner_text() == expected["representative_style_id"]
        if prompt_variant_expected > 0:
            assert prompt_summary.count() == 1
            prompt_section = prompt_summary.locator("..")
            assert prompt_variant_group_count == prompt_variant_expected
            assert "prompt match 기준이며 시각 유사도 판단이 아닙니다" in prompt_section.inner_text()
            prompt_groups = prompt_section.locator(":scope > details.group")
            for expected in expected_prompt_variant_groups:
                matches = _matching_group_indices(prompt_groups, expected["member_style_id_set"])
                assert len(matches) == 1
                variant = prompt_groups.nth(matches[0])
                assert variant.evaluate("el => el.open")
                assert variant.locator("article.card").count() == len(expected["member_style_ids"])
                assert variant.locator("article.card h3").first.inner_text() == expected["representative_style_id"]
            expected_prompt_sets = [expected["member_style_id_set"] for expected in expected_prompt_variant_groups]
            for expected_members in ({"CASE-088", "CASE-089"}, {"BST-001", "BST-002"}):
                if any(expected_members <= group_set for group_set in expected_prompt_sets):
                    matches = _containing_group_indices(prompt_groups, expected_members)
                    assert len(matches) == 1
                    prompt_variant_checked = True
        style_ids = set(style_by_item_id.values())
        if {"DAV490-019", "API-067"} <= style_ids:
            matching_indices = []
            similar_groups = similar_section.locator(":scope > details.group")
            for index in range(similar_group_count):
                group = similar_groups.nth(index)
                if set(_group_style_ids(group)) == {"DAV490-019", "API-067"} and "near_copy_candidate" in group.locator("div.group-meta").inner_text():
                    matching_indices.append(index)
            assert len(matching_indices) == 1
            family = similar_groups.nth(matching_indices[0])
            if not family.evaluate("el => el.open"):
                family.locator(":scope > summary").click()
            assert family.evaluate("el => el.open")
            assert family.locator("article.card").count() == 2
            assert family.locator("article.card h3").first.inner_text() == "DAV490-019"
            assert "near_copy_candidate" in family.locator("div.group-meta").inner_text()
            family_checked = True
        deleted_style_ids = {
            style_by_item_id[item_id]
            for item_id in [str(entry.get("id") or "").strip() for entry in retention.get("archived", []) if isinstance(entry, dict)]
            if item_id in style_by_item_id
        }
        retrieval_style_ids = _evaluation_style_ids(retrieval_section)
        for style_id in deleted_style_ids:
            assert style_id not in retrieval_style_ids
        assert page.locator("article.eval-card").count() == len(evaluation["evaluations"])
        for index, row in enumerate(evaluation["evaluations"]):
            assert page.locator("article.eval-card").nth(index).locator("tbody tr").count() == min(5, len(row["ranked"]))
        page.locator("img").evaluate_all("nodes => nodes.forEach(n => n.loading = 'eager')")
        page.wait_for_function("Array.from(document.images).every(i => i.complete && i.naturalWidth > 0)")
        assert not errors
        archive_summary.click()
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=str(destination / "results-desktop.png"), full_page=False)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=str(destination / "results-mobile.png"), full_page=False)
        browser.close()
    result = {"status": "passed", "active": len(retention["active_ids"]),
        "archived": len(retention["archived"]), "manifest_items": manifest_expected, "archive_collapsed_by_default": True,
        "archive_expansion_checked": True, "group_expansion_checked": True,
        "images_loaded": True, "js_errors": errors, "remote_requests_allowed": False,
        "preferred_similar_family_checked": family_checked,
        "prompt_variant_checked": prompt_variant_checked,
        "logical_deletion_group_count": logical_deletion_group_count, "prompt_variant_group_count": prompt_variant_group_count, "similar_group_count": similar_group_count,
        "retrieval_result_tables_checked": len(evaluation["evaluations"]),
        "viewports": ["1440x1000", "390x844"]}
    write_json(qa_path, result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
