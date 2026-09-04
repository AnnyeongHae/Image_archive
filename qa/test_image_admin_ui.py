"""Isolated administrator frontend tests: mocked loopback only; no real DB/API."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "app" / "image-admin"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9WnK8yQAAAAASUVORK5CYII=")
ORIGIN = "http://127.0.0.1:58993"


def fixture(stage=1):
    ids = ["O1", "O2", "A", "B", "C", "D"]
    spec = {
        "run_id": "SYNTHETIC_ADMIN_UI_ONLY",
        "spec_sha256": "synthetic",
        "approval_policy": "default_retained_images_after_review_v1",
        "items": [{"id": ident, "style_id": f"CASE-{ident}", "media_url": f"/media/{ident}",
                   "priority": {"rank_index": index + 1}} for index, ident in enumerate(ids)],
        "stage1": {"active_ids": ids, "archived": []},
        "baseline": {
            "read_only_ids": ["O1", "O2"],
            "image_approvals": [{"id": "O1", "approved": True, "memo_text": "기존 메모"},
                                {"id": "O2", "approved": False, "memo_text": ""}],
            "groups": [{"group_id": "old", "member_ids": ["O1", "O2"]}],
        },
        "duplicate_candidates": [{"id": "dup-1", "member_ids": ["A", "B", "C"],
                                   "representative_priority_ids": ["A", "B", "C"]}],
        "similarity_candidates": [{"id": "sim-1", "member_ids": ["O1", "O2", "A", "C", "D"],
                                    "baseline_anchor_ids": ["O1", "O2"]}],
    }
    decisions = {
        "schema_version": "image-group-workflow-decisions-3",
        "run_id": spec["run_id"], "spec_sha256": "synthetic",
        "approval_policy": spec["approval_policy"], "reviewer": "SYNTHETIC_QA_ONLY",
        "reviewed_at": "", "metadata_optional": True, "notes": "",
        "duplicate_reviews": [{"candidate_id": "dup-1", "decision": "distinct_images",
                               "selected_ids": ["A", "B"], "remainder_distinct": False}],
        "similarity_reviews": [{"candidate_id": "sim-1", "decision": "approve_selected",
                                "selected_ids": ["O1", "O2", "A", "C", "D"], "tags_text": ""}],
        "image_approvals": [{"id": ident, "approved": True, "memo_text": ""}
                            for ident in ["A", "B", "C", "D"]],
    }
    return {"run_id": spec["run_id"], "revision": 0, "active_stage": stage,
            "completed_stages": list(range(1, stage)), "spec": spec, "decisions": decisions,
            "summary": {"retained_image_ids": ids, "confirmed_front_count": 1,
                        "draft_front_count": 5}, "saved_at": "2026-09-03T00:00:00Z",
            "last_commit": None}


def grouped_gallery(member_lists, singles=()):
    ids = list(dict.fromkeys([ident for members in member_lists for ident in members] + list(singles)))
    items = [{"id": ident, "style_id": f"CASE-{ident}", "media_url": f"/media/{ident}",
              "memo_text": f"영감 메모 {ident}", "prompt_status": "available"} for ident in ids]
    groups = [{"group_id": f"group-{index}", "representative_id": members[0], "member_ids": members,
               "source_candidate_ids": [f"human-{index}"], "hidden_member_count": 0}
              for index, members in enumerate(member_lists)]
    return {"items": items, "groups": copy.deepcopy(groups), "committed_at": "2026-09-03T00:00:00Z",
            "library": {"schema_version": "image-approved-library-1", "display_groups": groups,
                        "ungrouped_ids": list(singles), "counts": {"approved_images": len(ids)}}}


class AssetTests(unittest.TestCase):
    def test_assets_are_same_origin_external_and_no_download_workflow(self):
        html = (ASSETS / "index.html").read_text(encoding="utf-8")
        js = (ASSETS / "admin.js").read_text(encoding="utf-8")
        self.assertIn('<script src="/admin.js" defer></script>', html)
        self.assertIn('href="/admin.css"', html)
        self.assertNotRegex(html, r"\son\w+\s*=|\sstyle\s*=|\sdownload(?:\s|=|>)")
        self.assertNotRegex(js, r"createObjectURL|data:|https://")
        self.assertIn("X-Admin-CSRF", js)
        self.assertIn("expected_revision:server.revision", js)
        self.assertIn("pendingTransition", js)
        self.assertIn("pendingSave", js)
        self.assertIn('data-anchor-toggle', js)
        self.assertIn('data-image-memo', js)
        self.assertIn('id="restore-draft"', html)
        self.assertIn('id="footer-gallery"', html)
        self.assertIn('id="zoom-rights"', html)

    def test_accessible_dialogs_and_native_inputs(self):
        html = (ASSETS / "index.html").read_text(encoding="utf-8")
        css = (ASSETS / "admin.css").read_text(encoding="utf-8")
        self.assertEqual(html.count("<dialog "), 3)
        self.assertIn('id="prompt-text"', html)
        self.assertIn("readonly", html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn('href="#stage-content"', html)
        self.assertIn(":focus-visible", css)
        self.assertIn("prefers-reduced-motion", css)
        self.assertNotIn("@import", css)


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
            cls.driver = sync_playwright().start()
            cls.browser = cls.driver.chromium.launch(channel="msedge", headless=True)
        except (ImportError, Exception) as exc:
            if hasattr(cls, "driver"):
                cls.driver.stop()
            raise unittest.SkipTest(f"Existing Playwright/Edge unavailable: {exc}") from exc

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.driver.stop()

    def setUp(self):
        self.state = fixture()
        self.mutations = []
        self.responses = {}
        self.fail_next = None
        self.errors = []
        self.gallery_override = None
        self.prompt_overrides = {}
        self.prompt_requests = []
        self.raw_prompt = '\n{\r\n  "주제": "스티커 🧩",\r\n  "문구": "<script>원문 유지</script>"\r\n}\t '
        self.context = self.browser.new_context(viewport={"width": 1440, "height": 960})
        self.page = self.context.new_page()
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.context.route(f"{ORIGIN}/**", self.route)

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [])

    def route(self, route):
        request = route.request
        path = urlsplit(request.url).path
        if path in {"/", "/admin.js", "/admin.css"}:
            file = ASSETS / ("index.html" if path == "/" else path[1:])
            content_type = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}[file.suffix]
            route.fulfill(body=file.read_bytes(), content_type=content_type,
                          headers={"Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'"})
            return
        if path.startswith("/media/"):
            route.fulfill(body=PNG, content_type="image/png")
            return
        if path == "/api/admin/session":
            route.fulfill(json={"csrf_token": "synthetic-csrf"})
            return
        if path == "/api/admin/state":
            route.fulfill(json=self.state)
            return
        if path == "/api/admin/gallery":
            item = self.state["spec"]["items"][0]
            route.fulfill(json=self.gallery_override or {"items": [item], "groups": [],
                                "committed_at": "2026-09-03T00:00:00Z",
                                "library": {"schema_version": "image-approved-library-1", "display_groups": [],
                                            "ungrouped_ids": [item["id"]], "counts": {"approved_images": 1}}})
            return
        if path.startswith("/api/admin/prompt/"):
            ident = path.rsplit("/", 1)[1]
            self.prompt_requests.append(ident)
            route.fulfill(json=self.prompt_overrides.get(ident) or {
                "schema_version": "image-original-prompt-1", "id": ident, "status": "available",
                "full_prompt": self.raw_prompt,
                "prompt_sha256": hashlib.sha256(self.raw_prompt.encode("utf-8")).hexdigest(),
                "source_binding": {"run_id": self.state["run_id"], "spec_sha256": self.state["spec"]["spec_sha256"],
                                   "prompt_field": "prompt"}, "release_eligible": False})
            return
        body = request.post_data_json
        self.mutations.append({"path": path, "body": copy.deepcopy(body), "headers": request.headers})
        if self.fail_next == "conflict":
            self.fail_next = None
            self.state["revision"] += 1
            route.fulfill(status=409, json={"error": {"code": "revision_conflict", "message": "synthetic conflict"}})
            return
        key = body["request_id"]
        if key in self.responses:
            route.fulfill(json=self.responses[key])
            return
        if body["expected_revision"] != self.state["revision"]:
            route.fulfill(status=409, json={"error": {"code": "revision_conflict", "message": "stale"}})
            return
        self.assertEqual(request.headers["x-admin-csrf"], "synthetic-csrf")
        self.assertEqual(body["stage"], self.state["active_stage"])
        stage = body["stage"]
        old = copy.deepcopy(self.state["decisions"])
        if "decisions" in body:
            self.state["decisions"] = copy.deepcopy(body["decisions"])
        if stage == 2 and old["duplicate_reviews"] != self.state["decisions"]["duplicate_reviews"]:
            for row in self.state["decisions"]["similarity_reviews"]:
                row["decision"] = "defer"
        if path.endswith("/advance"):
            if stage < 4:
                self.state["active_stage"] += 1
            self.state["completed_stages"] = list(range(1, stage + 1))
        elif path.endswith("/rewind"):
            self.state["active_stage"] = body["target_stage"]
            self.state["completed_stages"] = list(range(1, body["target_stage"]))
        self.state["revision"] += 1
        self.responses[key] = copy.deepcopy(self.state)
        if self.fail_next == "uncertain":
            self.fail_next = None
            route.abort("failed")
            return
        route.fulfill(json=self.state)

    def open(self, stage=1):
        self.state = fixture(stage)
        self.page.goto(ORIGIN)
        self.page.wait_for_selector(f'#stage-content[data-stage="{stage}"]')

    def saved(self):
        self.page.wait_for_function("() => document.querySelector('#save-label').textContent === '서버에 저장됨'")

    def advance(self, target):
        self.page.locator("#advance-stage").click()
        self.page.wait_for_selector(f'#stage-content[data-stage="{target}"]')

    def completed(self):
        self.page.wait_for_selector('#completion-summary:not([hidden])', state="attached")
        self.assertTrue(self.page.locator("#advance-stage").is_hidden())
        self.assertTrue(self.page.locator("#footer-gallery").is_enabled())

    def test_four_sequential_stages_and_optional_memo(self):
        self.open()
        self.assertEqual(self.page.locator("#stage-content h1").count(), 1)
        self.assertEqual(self.page.locator("[data-image-memo]").count(), 0)
        self.advance(2)
        self.assertEqual(self.page.locator("#stage-content .image-card").count(), 3)
        self.page.locator('[data-candidate-decision][value="same_image_subset"]').check()
        self.advance(3)
        self.assertTrue(self.page.locator('[data-candidate-decision][value="defer"]').is_checked())
        self.assertEqual(self.page.locator("#stage-content .image-card").count(), 5)
        self.page.locator("[data-anchor-toggle]").uncheck()
        self.assertFalse(self.page.locator('[data-candidate-member][value="O1"]').is_checked())
        self.assertFalse(self.page.locator('[data-candidate-member][value="O2"]').is_checked())
        self.page.locator('[data-candidate-decision][value="approve_selected"]').check()
        self.advance(4)
        self.assertEqual(self.page.locator("[data-image-approval]").count(), 3)
        self.assertEqual(self.page.locator("[data-image-memo][required]").count(), 0)
        self.page.locator('[data-image-approval="A"]').uncheck()
        self.page.locator('[data-image-memo="C"]').fill("상세페이지에 활용 <script>안전</script>")
        self.saved()
        self.page.locator("#advance-stage").click()
        self.completed()
        self.assertFalse(next(row for row in self.state["decisions"]["image_approvals"] if row["id"] == "A")["approved"])
        self.assertEqual(next(row for row in self.state["decisions"]["image_approvals"] if row["id"] == "C")["memo_text"], "상세페이지에 활용 <script>안전</script>")
        self.assertTrue(self.page.locator("#advance-stage").is_disabled())
        self.page.locator('[data-close-dialog="gallery-dialog"]').click()
        self.assertTrue(self.page.locator("#footer-gallery").evaluate("node => document.activeElement === node"))

    def test_conflict_preserves_edit_and_explicit_recovery_is_accessible(self):
        self.open(4)
        self.fail_next = "conflict"
        self.page.locator('[data-image-memo="A"]').fill("잃으면 안 되는 메모")
        self.page.wait_for_selector("#connection-banner:not([hidden])")
        self.assertEqual(self.page.locator('[data-image-memo="A"]').input_value(), "잃으면 안 되는 메모")
        self.assertTrue(self.page.locator("#advance-stage").is_disabled())
        self.page.once("dialog", lambda dialog: dialog.accept())
        self.page.locator("#reload-state").click()
        self.page.wait_for_selector("#recovery-banner:not([hidden])")
        self.assertEqual(self.page.locator('[data-image-memo="A"]').input_value(), "")
        self.page.once("dialog", lambda dialog: dialog.accept())
        self.page.locator("#restore-draft").click()
        self.saved()
        self.assertEqual(self.page.locator('[data-image-memo="A"]').input_value(), "잃으면 안 되는 메모")
        self.assertEqual(next(row for row in self.state["decisions"]["image_approvals"] if row["id"] == "A")["memo_text"], "잃으면 안 되는 메모")

    def test_uncertain_final_commit_retries_exact_id_and_payload(self):
        self.open(4)
        self.fail_next = "uncertain"
        self.page.locator("#advance-stage").click()
        self.page.wait_for_selector("#retry-write:not([hidden])")
        first = copy.deepcopy(self.mutations[-1]["body"])
        self.assertNotEqual(self.page.locator("#save-label").inner_text(), "서버에 저장됨")
        self.page.locator("#retry-write").click()
        self.completed()
        self.assertEqual(self.mutations[-1]["body"], first)
        self.assertEqual(len(self.responses), 1)

    def test_uncertain_autosave_keeps_newer_edit_and_serializes_followup(self):
        self.open(4)
        self.fail_next = "uncertain"
        self.page.locator('[data-image-memo="A"]').fill("첫 번째 선택")
        self.page.wait_for_selector("#retry-write:not([hidden])")
        first = copy.deepcopy(self.mutations[-1]["body"])
        self.page.locator('[data-image-memo="A"]').fill("더 최신의 메모")
        self.page.locator("#retry-write").click()
        self.saved()
        self.assertEqual(self.mutations[1]["body"], first)
        self.assertNotEqual(self.mutations[2]["body"]["request_id"], first["request_id"])
        self.assertEqual(self.mutations[2]["body"]["expected_revision"], 1)
        self.assertEqual(next(row for row in self.state["decisions"]["image_approvals"] if row["id"] == "A")["memo_text"], "더 최신의 메모")
        self.assertEqual(self.page.locator('[data-image-memo="A"]').input_value(), "더 최신의 메모")

    def test_zoom_focus_and_mobile_layout(self):
        self.open(3)
        zoom = self.page.locator('[data-zoom-id="A"]')
        zoom.click()
        self.assertTrue(self.page.locator("#zoom-dialog").evaluate("node => node.open"))
        self.page.keyboard.press("Escape")
        self.page.wait_for_function("() => document.activeElement.dataset.zoomId === 'A'")
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 390)
        self.assertEqual(self.page.locator("#stage-content h1").count(), 1)

    def test_completed_reload_at_700px_has_read_only_gallery_entry(self):
        self.state = fixture(4)
        self.state["completed_stages"] = [1, 2, 3, 4]
        self.state["last_commit"] = {"id": "synthetic-commit", "created_at": "2026-09-03T00:00:00Z"}
        self.page.set_viewport_size({"width": 700, "height": 900})
        self.page.goto(ORIGIN)
        self.completed()
        self.assertFalse(self.page.locator("#gallery-dialog").evaluate("node => node.open"))
        self.assertTrue(self.page.locator("#footer-gallery").is_visible())
        self.assertIn("검토 완료", self.page.locator("#header-stage").inner_text())
        self.assertIn("승인 완료", self.page.locator('[data-go-stage="4"]').get_attribute("aria-label"))
        self.assertIn("✓", self.page.locator('[data-go-stage="4"]').inner_text())
        bounds = self.page.locator("#footer-gallery").bounding_box()
        self.assertGreaterEqual(bounds["y"], 0)
        self.assertLessEqual(bounds["y"] + bounds["height"], 900)
        self.page.locator("#footer-gallery").click()
        self.page.wait_for_selector("#gallery-content .image-card")
        self.assertEqual(self.mutations, [])
        self.page.locator('[data-close-dialog="gallery-dialog"]').click()
        self.page.reload()
        self.completed()
        self.assertTrue(self.page.locator("#footer-gallery").is_visible())
        self.assertEqual(self.mutations, [])
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 700)

    def test_rights_unknown_fallback_visible_on_cards_gallery_and_zoom(self):
        self.open(3)
        self.assertEqual(self.page.locator("#stage-content .rights-badge").count(), 5)
        self.page.locator('[data-card-id="A"] .rights-details summary').click()
        self.assertIn("권리 미확인", self.page.locator('[data-card-id="A"]').inner_text())
        self.assertIn("제작자 미확인", self.page.locator('[data-card-id="A"]').inner_text())
        self.assertIn("공개·상업 이용 허가가 아닙니다", self.page.locator('[data-card-id="A"]').inner_text())
        self.page.locator('[data-zoom-id="A"]').click()
        self.assertEqual(self.page.locator("#zoom-rights .rights-badge").inner_text(), "권리 미확인")
        self.page.locator('[data-close-dialog="zoom-dialog"]').click()
        self.page.locator("#footer-gallery").click()
        self.page.wait_for_selector("#gallery-content .rights-note")
        self.assertEqual(self.page.locator("#gallery-content .rights-badge").inner_text(), "권리 미확인")
        self.assertEqual(self.mutations, [])

    def test_rights_contract_escapes_text_rejects_unsafe_links_and_keeps_scope(self):
        self.state = fixture(2)
        self.state["spec"]["items"][2]["rights_display"] = {
            "schema_version": "image-rights-notice-1", "status": "unverified",
            "badge": "권리 미확인", "source_name": "<script>출처</script>",
            "source_url": "javascript:alert(1)", "creator_name": None,
            "license_label": "MIT (저장소)", "license_scope": "repository_only",
            "attribution_text": "Copyright 원문 유지", "notice_text": "MIT는 이 이미지의 사용 허가를 보장하지 않습니다.",
            "image_license_verified": False, "release_eligible": False,
        }
        self.state["spec"]["items"][3]["rights_display"] = {
            **self.state["spec"]["items"][2]["rights_display"],
            "source_url": "https://example.test/source?a=1&b=2", "source_name": "확인된 원문",
        }
        self.page.goto(ORIGIN)
        self.page.wait_for_selector('#stage-content[data-stage="2"]')
        card = self.page.locator('[data-card-id="A"]')
        card.locator(".rights-details summary").click()
        self.assertEqual(card.locator(".rights-note a").count(), 0)
        self.assertEqual(card.locator("script").count(), 0)
        self.assertIn("<script>출처</script>", card.inner_text())
        self.assertIn("저장소에만 적용", card.inner_text())
        self.assertIn("사용 허가를 보장하지 않습니다", card.inner_text())
        link = self.page.locator('[data-card-id="B"] .rights-note a')
        self.assertEqual(link.get_attribute("href"), "https://example.test/source?a=1&b=2")
        self.assertEqual(link.get_attribute("rel"), "noopener noreferrer")
        self.assertEqual(self.mutations, [])

    def test_handoff_preparation_failure_does_not_claim_approval_failure(self):
        self.state = fixture(4)
        self.state["completed_stages"] = [1, 2, 3, 4]
        self.state["handoff"] = {"status": "preparation_failed", "provider_calls": 0}
        self.page.goto(ORIGIN)
        self.completed()
        self.assertIn("승인은 정상 저장", self.page.locator("#handoff-notice").inner_text())
        self.assertIn("LLM 분석·텍스트 임베딩은 자동 실행되지 않습니다", self.page.locator("#completion-summary").inner_text())
        self.assertEqual(self.mutations, [])

    def test_group_pagination_and_search_keep_whole_memberships(self):
        self.gallery_override = grouped_gallery([[f"G{index}{suffix}" for suffix in "ABC"] for index in range(7)],
                                                [f"S{index}" for index in range(14)])
        self.open(4)
        self.page.locator("#footer-gallery").click()
        self.page.wait_for_selector("[data-library-group]")
        self.assertEqual(self.page.locator("[data-library-group]").count(), 6)
        first = self.page.locator('[data-library-group="group-0"]')
        self.assertEqual(first.locator(".image-card").count(), 3)
        first.locator(".group-members > summary").click()
        self.assertEqual(first.locator(".image-card:visible").count(), 3)
        self.assertEqual(first.locator(".rights-note").count(), 3)
        self.assertEqual(first.locator("[data-prompt-view]").count(), 3)
        self.assertIn("개인 메모", first.inner_text())
        self.page.locator('[data-gallery-kind="groups"][data-gallery-page="1"]').click()
        self.assertEqual(self.page.locator("[data-library-group]").count(), 1)
        self.assertEqual(self.page.locator('[data-library-group="group-6"] .image-card').count(), 3)
        self.page.locator("#gallery-search").fill("CASE-G0B")
        self.assertEqual(self.page.locator("[data-library-group]").count(), 1)
        first = self.page.locator('[data-library-group="group-0"]')
        self.assertEqual(first.locator(".image-card").count(), 3)
        self.assertIn("CASE-G0A", first.inner_text())
        self.assertIn("CASE-G0B", first.inner_text())
        self.assertIn("CASE-G0C", first.inner_text())
        self.assertIn("승인 이미지 35개", self.page.locator(".library-overview").inner_text())
        self.assertEqual(self.mutations, [])

    def test_partial_overlap_groups_remain_distinct_and_unique_total_is_correct(self):
        self.gallery_override = grouped_gallery([list("ABC"), list("BCD")], ["E"])
        self.open(4)
        self.page.locator("#footer-gallery").click()
        self.page.wait_for_selector("[data-library-group]")
        self.assertEqual(self.page.locator("[data-library-group]").count(), 2)
        self.assertIn("승인 이미지 5개", self.page.locator(".library-overview").inner_text())
        self.assertIn("여러 그룹에 속한 이미지 2개", self.page.locator(".library-overview").inner_text())
        self.page.locator("#gallery-filter").select_option("singles")
        self.assertEqual(self.page.locator("[data-library-group]").count(), 0)
        self.assertEqual(self.page.locator("#gallery-content .image-card").count(), 1)
        self.assertEqual(self.page.locator("#gallery-content .image-card").get_attribute("data-card-id"), "E")
        self.assertEqual(self.mutations, [])

    def test_prompt_clipboard_preserves_full_raw_text_and_waits_for_success(self):
        self.raw_prompt = '\n{\r\n  "주제": "스티커 🧩",\r\n  "긴 원문": "' + ("가" * 6500) + '",\r\n  "문구": "</textarea><script>원문</script>"\r\n}\t '
        self.page.add_init_script("Object.defineProperty(navigator, 'clipboard', {configurable:true,value:{writeText:text=>new Promise(resolve=>{window.copiedRaw=text;window.finishCopy=resolve;})}})")
        self.open(4)
        self.page.locator('[data-prompt-copy="A"]').click()
        self.page.wait_for_function("() => document.querySelector('#prompt-status').textContent.includes('복사하고 있습니다')")
        self.assertEqual(self.page.evaluate("window.copiedRaw"), self.raw_prompt)
        self.assertNotIn("복사했습니다", self.page.locator("#prompt-status").inner_text())
        self.page.evaluate("window.finishCopy()")
        self.page.wait_for_function("() => document.querySelector('#prompt-status').textContent.includes('복사했습니다')")
        self.assertEqual(self.page.locator("#prompt-text").input_value(), self.raw_prompt.replace("\r\n", "\n"))
        self.assertEqual(self.page.locator("#prompt-dialog script").count(), 0)
        self.page.locator('[data-close-dialog="prompt-dialog"]').click()
        self.assertTrue(self.page.locator('[data-prompt-copy="A"]').evaluate("node => document.activeElement === node"))
        self.page.locator('[data-prompt-view="A"]').click()
        self.page.wait_for_function("() => !document.querySelector('#prompt-copy').disabled")
        self.assertEqual(self.prompt_requests, ["A"])
        self.assertEqual(self.mutations, [])

    def test_prompt_clipboard_failure_offers_selected_manual_fallback(self):
        self.page.add_init_script("Object.defineProperty(navigator, 'clipboard', {configurable:true,value:{writeText:async()=>{throw new Error('denied')}}})")
        self.open(3)
        self.page.locator('[data-prompt-copy="A"]').click()
        self.page.wait_for_function("() => document.querySelector('#prompt-status').textContent.includes('자동 복사가 허용되지 않았습니다')")
        self.assertNotIn("복사했습니다", self.page.locator("#prompt-status").inner_text())
        selection = self.page.locator("#prompt-text").evaluate("node => [node.selectionStart,node.selectionEnd,node.value.length]")
        self.assertEqual(selection, [0, selection[2], selection[2]])
        self.assertTrue(self.page.locator("#prompt-select").is_enabled())
        self.assertEqual(self.mutations, [])

    def test_missing_and_hash_mismatched_prompt_never_copy_truncated_text(self):
        self.prompt_overrides["A"] = {"schema_version": "image-original-prompt-1", "id": "A", "status": "missing",
                                      "full_prompt": "", "prompt_sha256": None, "source_binding": None}
        self.open(4)
        self.page.locator('[data-prompt-view="A"]').click()
        self.page.wait_for_function("() => document.querySelector('#prompt-status').textContent.includes('원본 프롬프트가 없습니다')")
        self.assertTrue(self.page.locator("#prompt-copy").is_disabled())
        self.page.locator('[data-close-dialog="prompt-dialog"]').click()
        self.prompt_overrides["B"] = {"schema_version": "image-original-prompt-1", "id": "B", "status": "available",
                                      "full_prompt": "wrong source", "prompt_sha256": "0" * 64,
                                      "source_binding": {"run_id": self.state["run_id"], "spec_sha256": "synthetic", "prompt_field": "prompt"}}
        self.page.locator('[data-prompt-copy="B"]').click()
        self.page.wait_for_function("() => document.querySelector('#prompt-status').textContent.includes('해시가 일치하지 않습니다')")
        self.assertTrue(self.page.locator("#prompt-copy").is_disabled())
        self.assertEqual(self.page.locator("#prompt-text").input_value(), "")
        self.assertEqual(self.mutations, [])

    def test_empty_candidates_can_advance_and_search_preserves_focus(self):
        self.open(4)
        self.page.locator("#image-search").fill("CASE-C")
        self.assertEqual(self.page.locator("[data-image-memo]").count(), 1)
        self.assertTrue(self.page.locator("#image-search").evaluate("node => document.activeElement === node"))
        self.page.locator("#image-filter").select_option("all")
        self.page.locator("#image-search").fill("CASE-O2")
        self.assertEqual(self.page.locator("#stage-content .image-card").count(), 1)
        self.assertEqual(self.page.locator("#stage-content [data-image-approval]").count(), 0)
        self.assertIn("기존 미승인", self.page.locator("#stage-content").inner_text())


if __name__ == "__main__":
    unittest.main()
