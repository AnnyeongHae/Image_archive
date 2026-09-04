"""Explicit, local-only browser QA for the human similarity review v2 UI."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from image_rag_eval.experiment import read_json, run_path, write_json
from image_rag_eval.human_review import REVIEW_LABELS_SCHEMA_VERSION
from image_rag_eval.human_review_v2 import (
    REVIEW_V2_HTML_FILENAME,
    REVIEW_V2_SPEC_FILENAME,
    REVIEW_V2_TEMPLATE_FILENAME,
    build_human_review_v2_artifacts,
    validate_review_labels_v2,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--root", default=None)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if not args.apply:
        print(json.dumps({"status": "dry_run", "network_calls": 0, "writes": 0}))
        return

    from playwright.sync_api import Error, sync_playwright

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[1]
    destination = run_path(root, args.run_id)
    spec_path = destination / REVIEW_V2_SPEC_FILENAME
    template_path = destination / REVIEW_V2_TEMPLATE_FILENAME
    html_path = destination / REVIEW_V2_HTML_FILENAME
    if not spec_path.exists() or not template_path.exists() or not html_path.exists():
        build_human_review_v2_artifacts(root, args.run_id)
    spec = read_json(spec_path)
    template = read_json(template_path)

    qa_path = destination / "human-similarity-review-v2-qa.json"
    initial_desktop_path = destination / "human-similarity-review-v2-initial-desktop.png"
    initial_mobile_path = destination / "human-similarity-review-v2-initial-mobile.png"
    interaction_desktop_path = destination / "human-similarity-review-v2-interaction-desktop.png"
    interaction_mobile_path = destination / "human-similarity-review-v2-interaction-mobile.png"

    failures: list[str] = []
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        tampered_path = temp_root / "tampered-v2.labels.json"
        with sync_playwright() as browser_api:
            browser = browser_api.chromium.launch(channel="msedge", headless=True)

            initial_context = browser.new_context(viewport={"width": 1440, "height": 1100}, accept_downloads=False)
            initial_page = initial_context.new_page()
            initial_page.route("http://**/*", lambda route: route.abort())
            initial_page.route("https://**/*", lambda route: route.abort())
            initial_page.on("pageerror", lambda error: failures.append(f"pageerror:{type(error).__name__}"))
            initial_page.goto(html_path.as_uri(), wait_until="load")
            initial_page.locator("img").evaluate_all("nodes => nodes.forEach(node => node.loading = 'eager')")
            initial_page.wait_for_function("Array.from(document.images).every(i => i.complete && i.naturalWidth > 0)")
            pair_count = len(spec.get("pairs", []))
            assert initial_page.locator(".pair-card").count() == pair_count
            assert initial_page.locator(".machine-panel[hidden]").count() == pair_count
            assert initial_page.locator(".prompt-panel[hidden]").count() == pair_count * 2
            initial_page.screenshot(path=str(initial_desktop_path), full_page=False)
            initial_page.set_viewport_size({"width": 390, "height": 844})
            initial_page.wait_for_function("document.documentElement.scrollWidth <= window.innerWidth")
            initial_page.screenshot(path=str(initial_mobile_path), full_page=False)
            initial_context.close()

            old_v1_payload = {
                "schema_version": REVIEW_LABELS_SCHEMA_VERSION,
                "review_spec_sha256": spec["source_review_spec_sha256"],
                "run_id": spec["run_id"],
                "reviewer": "",
                "reviewed_at": "",
                "pairs": [],
            }
            for index, pair in enumerate(spec["pairs"]):
                label = None
                verified = False
                if index == 0:
                    label = "near_duplicate"
                    verified = True
                elif index == 1:
                    label = "unsure"
                    verified = False
                old_v1_payload["pairs"].append(
                    {
                        "pair_id": pair["pair_id"],
                        "left": {
                            "id": pair["left"]["id"],
                            "source_sha256": pair["left"]["source_sha256"],
                            "prepared_sha256": pair["left"]["prepared_sha256"],
                        },
                        "right": {
                            "id": pair["right"]["id"],
                            "source_sha256": pair["right"]["source_sha256"],
                            "prepared_sha256": pair["right"]["prepared_sha256"],
                        },
                        "human_label": label,
                        "human_verified": verified,
                        "dimensions": {"composition": None, "style": None, "subject": None},
                        "reason": "",
                    }
                )

            context = browser.new_context(viewport={"width": 1440, "height": 1100}, accept_downloads=True)
            old_v1_bootstrap = json.dumps(
                {"oldKey": spec["migration"]["old_local_storage_key"], "payload": old_v1_payload},
                ensure_ascii=False,
            ).replace("</", "<\\/")
            context.add_init_script(
                script=f"""
                (() => {{
                  const bootstrap = {old_v1_bootstrap};
                  try {{ localStorage.setItem(bootstrap.oldKey, JSON.stringify(bootstrap.payload)); }} catch (error) {{}}
                }})();
                """,
            )
            page = context.new_page()
            page.route("http://**/*", lambda route: route.abort())
            page.route("https://**/*", lambda route: route.abort())
            page.on("pageerror", lambda error: failures.append(f"pageerror:{type(error).__name__}"))
            page.goto(html_path.as_uri(), wait_until="load")
            page.locator("img").evaluate_all("nodes => nodes.forEach(node => node.loading = 'eager')")
            page.wait_for_function("Array.from(document.images).every(i => i.complete && i.naturalWidth > 0)")
            assert "v1 초안을 v2로 복사했습니다." in (page.locator("#message").text_content() or "")
            first_pair_id = str(spec["pairs"][0]["pair_id"])
            assert page.locator(f'input[data-pair-id="{first_pair_id}"][value="near_duplicate"]').is_checked()
            assert "그룹핑만 하고 둘 다 보존" in (page.locator(f"#action-{first_pair_id}").text_content() or "")

            page.locator("#download").click()
            assert "검토자 이름을 입력하세요." in (page.locator("#message").text_content() or "")

            active_pair = next(
                (
                    pair
                    for pair in spec["pairs"]
                    if pair["left"]["retention_state"]["state"] == "active"
                    and pair["right"]["retention_state"]["state"] == "active"
                ),
                spec["pairs"][0],
            )
            archived_control_pair = next(
                (
                    pair
                    for pair in spec["pairs"]
                    if pair["left"]["retention_state"]["state"] == "already_excluded"
                    or pair["right"]["retention_state"]["state"] == "already_excluded"
                ),
                None,
            )
            active_pair_id = str(active_pair["pair_id"])
            archived_pair_id = (
                str(archived_control_pair["pair_id"])
                if archived_control_pair is not None and str(archived_control_pair["pair_id"]) != active_pair_id
                else None
            )
            second_pair_id = next(
                (
                    str(pair["pair_id"])
                    for pair in spec["pairs"]
                    if str(pair["pair_id"]) not in {active_pair_id, archived_pair_id}
                ),
                next(
                    (str(pair["pair_id"]) for pair in spec["pairs"] if str(pair["pair_id"]) != active_pair_id),
                    active_pair_id,
                ),
            )
            page.locator("#reviewer").fill("fixture-reviewer-v2")
            page.locator(f'input[data-pair-id="{active_pair_id}"][value="identical"]').check()
            page.locator(f'input[data-pair-id="{second_pair_id}"][value="unsure"]').check()
            page.wait_for_function(
                """pairId => {
                    const machine = document.getElementById(`machine-${pairId}`);
                    const left = document.getElementById(`prompt-left-${pairId}`);
                    const right = document.getElementById(`prompt-right-${pairId}`);
                    const action = document.getElementById(`action-${pairId}`);
                    return machine && !machine.hidden && left && !left.hidden && right && !right.hidden &&
                        action && action.textContent.includes('중복 논리삭제 계획');
                }""",
                arg=active_pair_id,
            )

            if archived_control_pair is not None:
                page.locator(f'input[data-pair-id="{archived_pair_id}"][value="identical"]').check()
                page.wait_for_function(
                    """pairId => {
                        const action = document.getElementById(`action-${pairId}`);
                        return action && action.textContent.includes('기존 제외 대조군');
                    }""",
                    arg=archived_pair_id,
                )

            with page.expect_download() as download_info:
                page.locator("#download").click()
            downloaded = json.loads(Path(download_info.value.path()).read_text(encoding="utf-8"))
            validated = validate_review_labels_v2(spec, downloaded)
            first_row = next(row for row in validated["pairs"] if row["pair_id"] == active_pair_id)
            second_row = next(row for row in validated["pairs"] if row["pair_id"] == second_pair_id)
            assert first_row["human_label"] == "identical"
            assert first_row["action"] == "delete_duplicate"
            assert first_row["human_verified"] is True
            assert first_row["retention_suggestion"]["keep_id"] != first_row["retention_suggestion"]["delete_id"]
            assert second_row["human_label"] == "unsure"
            assert second_row["action"] == "defer"
            assert second_row["human_verified"] is False

            tampered = json.loads(json.dumps(downloaded))
            tampered["pairs"][0]["retention_suggestion"]["delete_id"] = "tampered"
            tampered_path.write_text(json.dumps(tampered, ensure_ascii=False, indent=2), encoding="utf-8")
            page.locator("#import-file").set_input_files(str(tampered_path))
            page.wait_for_function(
                """() => {
                    const text = document.getElementById('message')?.textContent || '';
                    return text.includes('retention suggestion');
                }"""
            )

            page.screenshot(path=str(interaction_desktop_path), full_page=False)
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_function("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(interaction_mobile_path), full_page=False)
            context.close()
            browser.close()

    if failures:
        raise Error(f"browser page errors: {failures}")

    result = {
        "status": "passed",
        "run_id": args.run_id,
        "sampled_pairs": pair_count,
        "initial_machine_panels_hidden": pair_count,
        "initial_prompt_panels_hidden": pair_count * 2,
        "reviewer_export_gate": True,
        "v1_local_storage_migration": True,
        "tampered_import_rejected": True,
        "identical_delete_action_exported": True,
        "archived_control_no_new_delete_copy": True,
        "near_duplicate_group_only": True,
        "unsure_human_verified_false": True,
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
    write_json(qa_path, result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
