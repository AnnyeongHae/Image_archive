"""Explicit, local-only browser QA for the human similarity review UI."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from image_rag_eval.experiment import read_json, run_path, write_json
from image_rag_eval.human_review import build_human_review_artifacts, validate_review_labels


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if not args.apply:
        print(json.dumps({"status": "dry_run", "network_calls": 0, "writes": 0}))
        return

    from playwright.sync_api import Error, sync_playwright

    root = Path(__file__).resolve().parents[1]
    destination = run_path(root, args.run_id)
    spec_path = destination / "human-similarity-review.spec.json"
    template_path = destination / "human-similarity-review.template.json"
    html_path = destination / "human-similarity-review.html"
    summary_path = destination / "human-similarity-review-summary.json"

    if not spec_path.exists() or not template_path.exists() or not html_path.exists():
        build_human_review_artifacts(root, args.run_id)

    spec = read_json(spec_path)
    template = read_json(template_path)
    read_json(summary_path)

    qa_path = destination / "human-similarity-review-qa.json"
    initial_desktop_path = destination / "human-similarity-review-initial-desktop.png"
    initial_mobile_path = destination / "human-similarity-review-initial-mobile.png"
    interaction_desktop_path = destination / "human-similarity-review-interaction-desktop.png"
    interaction_mobile_path = destination / "human-similarity-review-interaction-mobile.png"

    failures: list[str] = []
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        tampered_path = temp_root / "tampered.labels.json"
        with sync_playwright() as browser_api:
            browser = browser_api.chromium.launch(channel="msedge", headless=True)
            initial_context = browser.new_context(
                viewport={"width": 1440, "height": 1100},
                accept_downloads=False,
            )
            initial_page = initial_context.new_page()
            initial_page.route("http://**/*", lambda route: route.abort())
            initial_page.route("https://**/*", lambda route: route.abort())
            initial_page.on("pageerror", lambda error: failures.append(f"pageerror:{type(error).__name__}"))
            initial_page.goto(html_path.as_uri(), wait_until="load")
            initial_page.locator("img").evaluate_all("nodes => nodes.forEach(node => node.loading = 'eager')")
            initial_page.wait_for_function("Array.from(document.images).every(i => i.complete && i.naturalWidth > 0)")

            pair_count = len(spec.get("pairs", []))
            cards = initial_page.locator(".pair-card")
            assert cards.count() == pair_count
            assert initial_page.locator(".machine-panel[hidden]").count() == pair_count
            assert initial_page.locator(".prompt-panel[hidden]").count() == pair_count * 2
            initial_page.screenshot(path=str(initial_desktop_path), full_page=False)
            initial_page.set_viewport_size({"width": 390, "height": 844})
            initial_page.wait_for_function("document.documentElement.scrollWidth <= window.innerWidth")
            initial_page.screenshot(path=str(initial_mobile_path), full_page=False)
            initial_context.close()

            context = browser.new_context(
                viewport={"width": 1440, "height": 1100},
                accept_downloads=True,
            )
            page = context.new_page()
            page.route("http://**/*", lambda route: route.abort())
            page.route("https://**/*", lambda route: route.abort())
            page.on("pageerror", lambda error: failures.append(f"pageerror:{type(error).__name__}"))
            page.goto(html_path.as_uri(), wait_until="load")
            page.locator("img").evaluate_all("nodes => nodes.forEach(node => node.loading = 'eager')")
            page.wait_for_function("Array.from(document.images).every(i => i.complete && i.naturalWidth > 0)")

            page.locator("#download").click()
            expect_message = page.locator("#message")
            expect_message.wait_for()
            assert "검토자 이름을 입력하세요." in expect_message.text_content()

            first_pair_id = str(spec["pairs"][0]["pair_id"])
            second_pair_id = str(spec["pairs"][1]["pair_id"]) if pair_count > 1 else first_pair_id
            page.locator(f'input[data-pair-id="{first_pair_id}"][value="unsure"]').check()
            page.locator(f'input[data-pair-id="{second_pair_id}"][value="same_visual_family"]').check()
            page.locator("#reviewer").fill("browser-qa-fixture-reviewer")
            page.locator(f'textarea[data-reason-for="{second_pair_id}"]').fill("fixture visual-family note")

            page.wait_for_function(
                """pairId => {
                    const machine = document.getElementById(`machine-${pairId}`);
                    const left = document.getElementById(`prompt-left-${pairId}`);
                    const right = document.getElementById(`prompt-right-${pairId}`);
                    return machine && !machine.hidden && left && !left.hidden && right && !right.hidden;
                }""",
                arg=first_pair_id,
            )
            page.reload(wait_until="load")
            page.locator("img").evaluate_all("nodes => nodes.forEach(node => node.loading = 'eager')")
            page.wait_for_function("Array.from(document.images).every(i => i.complete && i.naturalWidth > 0)")
            assert "로컬 초안을 복원했습니다." in (page.locator("#message").text_content() or "")
            assert page.locator(f'input[data-pair-id="{first_pair_id}"][value="unsure"]').is_checked()
            assert page.locator(f'input[data-pair-id="{second_pair_id}"][value="same_visual_family"]').is_checked()

            with page.expect_download() as download_info:
                page.locator("#download").click()
            download_path = Path(download_info.value.path())
            downloaded = json.loads(download_path.read_text(encoding="utf-8"))
            validated = validate_review_labels(spec, downloaded)
            first_row = next(row for row in validated["pairs"] if row["pair_id"] == first_pair_id)
            second_row = next(row for row in validated["pairs"] if row["pair_id"] == second_pair_id)
            assert first_row["human_label"] == "unsure"
            assert first_row["human_verified"] is False
            assert second_row["human_label"] == "same_visual_family"
            assert second_row["human_verified"] is True

            tampered = json.loads(json.dumps(downloaded))
            tampered["pairs"][0]["left"]["prepared_sha256"] = "0" * 64
            tampered_path.write_text(json.dumps(tampered, ensure_ascii=False, indent=2), encoding="utf-8")
            page.locator("#import-file").set_input_files(str(tampered_path))
            page.wait_for_function(
                """() => {
                    const text = document.getElementById('message')?.textContent || '';
                    return text.includes('pair binding이 현재 샘플과 다릅니다.');
                }"""
            )

            page.screenshot(path=str(interaction_desktop_path), full_page=False)
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_function("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(interaction_mobile_path), full_page=False)

            result = {
                "status": "passed",
                "run_id": args.run_id,
                "sampled_pairs": pair_count,
                "initial_machine_panels_hidden": pair_count,
                "initial_prompt_panels_hidden": pair_count * 2,
                "reviewer_export_gate": True,
                "local_draft_restore": True,
                "tampered_import_rejected": True,
                "unsure_human_verified_false": True,
                "validated_download_bindings": True,
                "download_saved_to_run": False,
                "external_network_allowed": False,
                "mobile_horizontal_overflow": False,
                "screenshots": [
                    initial_desktop_path.name,
                    initial_mobile_path.name,
                    interaction_desktop_path.name,
                    interaction_mobile_path.name,
                ],
                "js_errors": failures,
            }
            browser.close()

    if failures:
        raise Error(f"browser page errors: {failures}")

    write_json(qa_path, result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
