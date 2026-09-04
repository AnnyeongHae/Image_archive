"""Explicit, local-only browser check using already installed Edge/Playwright."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from image_rag_eval.experiment import read_json, run_path, validate_annotations, write_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({"status": "dry_run", "network": 0, "writes": 0}))
        return
    from playwright.sync_api import sync_playwright

    root = Path(__file__).resolve().parents[1]
    destination = run_path(root, args.run_id)
    manifest = read_json(destination / "manifest.json")
    failures = []
    with sync_playwright() as browser_api:
        browser = browser_api.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
        page.route("http://**/*", lambda route: route.abort())
        page.route("https://**/*", lambda route: route.abort())
        page.on("pageerror", lambda error: failures.append(type(error).__name__))
        page.goto((destination / "review.html").as_uri())
        page.locator("img").evaluate_all("nodes => nodes.forEach(node => node.loading = 'eager')")
        page.wait_for_function("Array.from(document.images).every(i => i.complete && i.naturalWidth > 0)")
        assert page.locator("input[type=checkbox]").count() == len(manifest["items"])
        assert page.locator("input[type=checkbox]:checked").count() == 0
        page.locator("#reviewer").fill("automated-ui-fixture-not-human-approval")
        page.locator("input[type=checkbox]").first.check()
        with page.expect_download() as download:
            page.locator("#save").click()
        # Temporary browser download only; NEVER save it as live annotations.json.
        downloaded = read_json(Path(download.value.path()))
        validate_annotations(manifest, downloaded, require_approval=False)
        assert sum(i["approved_for_external_ai"] for i in downloaded["items"]) == 1
        page.locator("input[type=checkbox]").first.uncheck()
        page.locator("#reviewer").fill("")
        page.screenshot(path=str(destination / "review-desktop.png"), full_page=False)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "mobile horizontal overflow"
        page.screenshot(path=str(destination / "review-mobile.png"), full_page=False)
        browser.close()
    assert not failures, "browser script errors"
    result = {"status": "passed", "items": len(manifest["items"]), "js_errors": failures,
        "checked_by_default": 0, "download_contract_validated": True,
        "human_approval_created": False, "external_network_allowed": False,
        "viewports": ["1440x1000", "390x844"]}
    write_json(destination / "browser-qa.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
