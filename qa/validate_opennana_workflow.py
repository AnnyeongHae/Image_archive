from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ARCHIVE_ROOT = Path(__file__).resolve().parents[1]
MODULE_DIR = ARCHIVE_ROOT / "src" / "opennana"
sys.path.insert(0, str(MODULE_DIR))

from apply_decisions import remaining_queue_after_decisions, validate_and_apply  # noqa: E402
from build_review_queue import build_queue, projection_javascript  # noqa: E402
from collect import PROMPT_BODY_KEYS, generic_robots_allows, parse_generic_robots  # noqa: E402
from common import DATA_ROOT, LEGACY_ROOT, read_json, sha256_text, stable_json, template_text  # noqa: E402
from dedupe import classify_bundle  # noqa: E402


class Validation:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def check(self, condition: bool, name: str, detail: Any = None) -> None:
        self.checks.append({"name": name, "ok": bool(condition), "detail": detail})

    def equal(self, actual: Any, expected: Any, name: str) -> None:
        self.check(actual == expected, name, {"actual": actual, "expected": expected})


def contains_prompt_body(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.casefold() in PROMPT_BODY_KEYS and child not in (None, "", [], {}):
                return True
            if contains_prompt_body(child):
                return True
    elif isinstance(value, list):
        return any(contains_prompt_body(item) for item in value)
    return False


def normalized_fixture(upstream_id: str, prompt: str, *, content_hash: str | None = None) -> dict[str, Any]:
    prompt_sha = sha256_text(prompt)
    return {
        "schema_version": "opennana-normalized-record-1.0",
        "source": "opennana",
        "upstream_id": upstream_id,
        "slug": upstream_id,
        "title": upstream_id,
        "source_url": f"https://opennana.com/awesome-prompt-gallery/{upstream_id}",
        "author": None,
        "model": "gpt-image-2",
        "tags": [],
        "media_type": "image",
        "image_urls": [],
        "prompt_text": prompt,
        "prompt_sha256": prompt_sha,
        "prompt_template_sha256": sha256_text(template_text(prompt)),
        "content_sha256": content_hash or sha256_text(f"content:{upstream_id}:{prompt_sha}"),
        "updated_at": "2026-08-31T00:00:00Z",
        "observed_at": "2026-08-31T00:00:00Z",
        "rights": {"release_eligible": False, "item_rights": "unverified"},
        "workflow_status": "normalized",
    }


def validate_robots(validation: Validation) -> None:
    generic_allowed_specific_denied = """
User-agent: *
Content-Signal: search=yes, ai-train=no, use=reference
Allow: /api/prompts

User-agent: GPTBot
Disallow: /
"""
    allowed, parsed = generic_robots_allows(generic_allowed_specific_denied, "/api/prompts")
    validation.check(allowed, "robots ignores GPTBot-only Disallow")
    validation.equal(parsed["content_signals"].get("search"), "yes", "robots generic search signal")
    validation.equal(parsed["content_signals"].get("ai-train"), "no", "robots generic ai-train signal")
    validation.equal(parsed["content_signals"].get("use"), "reference", "robots generic reference signal")

    generic_denied_specific_allowed = """
User-agent: *
Content-Signal: search=yes, ai-train=no, use=reference
Disallow: /api/prompts

User-agent: ImagePromptArchiveReferenceCollector
Allow: /
"""
    allowed, _ = generic_robots_allows(generic_denied_specific_allowed, "/api/prompts")
    validation.check(not allowed, "robots generic API denial is fail-closed")

    longest_rule = """
User-agent: *
Content-Signal: search=yes, ai-train=no, use=reference
Disallow: /
Allow: /api/prompts
"""
    allowed, _ = generic_robots_allows(longest_rule, "/api/prompts?page=1")
    validation.check(allowed, "robots longest matching allow wins")
    signals = parse_generic_robots("User-agent: *\nAllow: /api/prompts\n")["content_signals"]
    validation.check(not (signals.get("search") == "yes" and signals.get("ai-train") == "no"), "robots missing Content-Signal is detectable")


def validate_classifier(validation: Validation) -> None:
    exact_prompt = "A clean editorial product hero with soft daylight and a neutral pedestal."
    near_canonical = "Create a clean editorial product hero with soft daylight, a neutral pedestal, generous left copy space, and a restrained shadow for a premium catalog page."
    near_incoming = "Create a clean editorial product hero with soft daylight, a neutral pedestal, generous left copy space, and a restrained reflection for a premium catalog page."
    remix_canonical = "Build a four-panel campaign for [BOTTLE]. Preserve [BLUE] and keep text editable."
    remix_incoming = "Build a four-panel campaign for [JAR]. Preserve [RED] and keep text editable."
    source_prompt = "An unrelated structured reference board with front side and detail views."
    canonical_rows = [
        {
            "catalog_key": "fixture:exact",
            "record_id": "FIX-EXACT",
            "style_id": "FIX-001",
            "title": "exact",
            "prompt": {"text": exact_prompt, "sha256": sha256_text(exact_prompt)},
            "source": {"type": "fixture"},
        },
        {
            "catalog_key": "fixture:near",
            "record_id": "FIX-NEAR",
            "style_id": "FIX-002",
            "title": "near",
            "prompt": {"text": near_canonical, "sha256": sha256_text(near_canonical)},
            "source": {"type": "fixture"},
        },
        {
            "catalog_key": "fixture:remix",
            "record_id": "FIX-REMIX",
            "style_id": "FIX-003",
            "title": "remix",
            "prompt": {"text": remix_canonical, "sha256": sha256_text(remix_canonical)},
            "source": {"type": "fixture"},
        },
        {
            "catalog_key": "fixture:update",
            "record_id": "FIX-UPDATE",
            "style_id": "FIX-004",
            "title": "update",
            "content_sha256": "old-content-hash",
            "prompt": {"text": source_prompt, "sha256": sha256_text(source_prompt)},
            "source": {"type": "opennana", "upstream_id": "source-update"},
        },
    ]
    incoming = [
        normalized_fixture("incoming-exact", exact_prompt),
        normalized_fixture("incoming-near", near_incoming),
        normalized_fixture("incoming-remix", remix_incoming),
        normalized_fixture("source-update", source_prompt + " Updated composition.", content_hash="new-content-hash"),
    ]
    bundle = {"run_id": "fixture", "observed_at": "2026-08-31T00:00:00Z", "records": incoming}
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "canonical.jsonl"
        path.write_text("".join(stable_json(row, indent=None).strip() + "\n" for row in canonical_rows), encoding="utf-8")
        result = classify_bundle(bundle, path)
    classes = {record["upstream_id"]: record["dedupe"]["classification"] for record in result["records"]}
    validation.equal(classes["incoming-exact"], "exact_duplicate", "classifier exact duplicate")
    validation.equal(classes["incoming-near"], "near_duplicate", "classifier conservative near duplicate")
    validation.equal(classes["incoming-remix"], "remix_family", "classifier remix family")
    validation.equal(classes["source-update"], "same_source_update", "classifier same-source update")
    validation.equal(result["summary"]["auto_merged"], 0, "classifier never auto-merges near/remix")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the private OpenNana review workflow.")
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args()
    validation = Validation()

    config = read_json(DATA_ROOT / "config.json")
    state = read_json(DATA_ROOT / "state.json")
    raw = read_json(DATA_ROOT / "raw" / "sample-canary-v1.json")
    normalized = read_json(DATA_ROOT / "staging" / "normalized-sample-canary-v1.json")
    dedupe = read_json(DATA_ROOT / "staging" / "dedupe-sample-canary-v1.json")
    # The contract suite always exercises the fabricated fixture in memory.
    # The mutable current queue may contain a real approved network canary and
    # is validated independently by validate_opennana_review_queue.py.
    queue, draft, projection = build_queue(dedupe, state, config)
    js_path = LEGACY_ROOT / "opennana-review-data.js"

    validation.check(int(config["collection"]["canary_max_details"]) <= 20, "canary cap at most 20")
    validation.equal(config["collection"]["concurrency"], 1, "collector concurrency one")
    validation.check(float(config["collection"]["requests_per_second"]) <= 1.0, "collector rate at most one request per second")
    validation.equal(config["collection"]["access_type"], 0, "free access type only")
    validation.equal(config["policy"]["paid_prompt_body"], "forbidden", "paid body forbidden")
    validation.check(not config["policy"]["download_source_images"], "source image download disabled")
    validation.check(not config["policy"]["auto_publish"], "auto publish disabled")

    validation.equal(raw["mode"], "local_fabricated_sample", "sample is locally fabricated")
    validation.equal(raw["request_summary"]["network_requests"], 0, "sample uses no network")
    validation.check(len(raw["free_details"]) <= 20, "sample detail cap")
    validation.check(len(raw["selected_list_metadata"]) <= 20, "sample selected watermark cap")
    validation.check(not contains_prompt_body(raw["locked_metadata_only"]), "locked metadata has no prompt body")
    validation.equal(normalized["summary"]["source_image_downloads"], 0, "normalizer downloads no images")
    validation.check(all(not item["rights"]["release_eligible"] for item in normalized["records"]), "normalized rights fail closed")
    validation.check(all(url.startswith(("https://", "http://")) for item in normalized["records"] for url in item["image_urls"]), "media stays remote URL only")
    validation.check(all(item["source_url"].startswith("https://opennana.com/awesome-prompt-gallery/") for item in normalized["records"]), "OpenNana public detail URL shape")
    validation.equal(dedupe["summary"]["auto_merged"], 0, "sample dedupe does not auto-merge")
    validation.check(dedupe["summary"]["auto_collapsed"] >= 1, "sample exact duplicate collapsed")
    validation.equal(queue["summary"]["approval_is_public_release"], False, "queue approval is not release")
    validation.equal(len({item["queue_id"] for item in queue["items"]}), len(queue["items"]), "queue ids unique")
    validation.check(all(item["dedupe"]["classification"] not in {"exact_duplicate", "same_source_unchanged"} for item in queue["items"]), "collapsed duplicates omitted from queue")
    validation.equal(draft["queue_revision"], queue["queue_revision"], "decision draft revision matches queue")
    validation.equal([item["queue_id"] for item in draft["decisions"]], [item["queue_id"] for item in queue["items"]], "decision draft deterministically follows queue")
    validation.check(all(item["decision"] == "pending" for item in draft["decisions"]), "decision draft starts pending")
    validation.check(js_path.exists() and "window.OPENNANA_REVIEW_QUEUE" in js_path.read_text(encoding="utf-8"), "static admin projection exists")

    rebuilt_queue, rebuilt_draft, rebuilt_projection = build_queue(dedupe, state, config)
    validation.equal(stable_json(rebuilt_queue), stable_json(queue), "review queue idempotent")
    validation.equal(stable_json(rebuilt_draft), stable_json(draft), "decision draft idempotent")
    expected_js = projection_javascript(rebuilt_projection)
    validation.check(expected_js.startswith("// Generated from") and "window.OPENNANA_REVIEW_QUEUE" in expected_js, "fixture projection contract")
    validation.check(projection["review_boundary"] == "browser_draft_only_not_rights_or_release_approval", "fixture review boundary")

    extra_record = normalized_fixture("sample-backlog-extra", "A distinct monochrome architectural poster with generous type space.")
    extra_record["dedupe"] = {"classification": "new", "matches": []}
    later_bundle = {
        "run_id": "sample-later-run",
        "observed_at": "2026-08-31T01:00:00Z",
        "records": [extra_record],
    }
    merged_queue, _, _ = build_queue(later_bundle, state, config, queue)
    validation.equal(len(merged_queue["items"]), len(queue["items"]) + 1, "new queue build retains unresolved previous batch")
    validation.check(
        {item["queue_id"] for item in queue["items"]}.issubset({item["queue_id"] for item in merged_queue["items"]}),
        "all unresolved queue ids survive batch merge",
    )

    actions = ["approve", "reject", "defer", "group"]
    decision_rows = []
    for index, action in enumerate(actions):
        item = queue["items"][index]
        row = {"queue_id": item["queue_id"], "content_sha256": item["content_sha256"], "decision": action}
        if action == "group":
            row["group_with"] = queue["items"][0]["queue_id"]
        decision_rows.append(row)
    decision_fixture = {"queue_revision": queue["queue_revision"], "decided_at": "2026-08-31T02:00:00Z", "decisions": decision_rows}
    applied, pending, next_state = validate_and_apply(queue, decision_fixture, state)
    validation.equal(pending["record_count"], 2, "approve and group create canonicalization pending only")
    validation.check(not pending["public_release_eligible"], "pending records are not public release eligible")
    validation.equal(sorted(item["decision"] for item in applied["decisions"]), sorted(actions), "all explicit decision types validated")
    rejected_item = queue["items"][1]
    validation.equal(next_state["rejected_content_hashes"].get(rejected_item["upstream_id"]), rejected_item["content_sha256"], "legacy rejection suppression hash recorded")
    for index, action in enumerate(actions):
        item = queue["items"][index]
        ledger_entry = next_state["review_decision_ledger"][item["upstream_id"]][item["content_sha256"]]
        validation.equal(ledger_entry["decision"], action, f"durable {action} decision ledger")
    suppressed_queue, _, _ = build_queue(dedupe, next_state)
    suppressed_ids = {item["queue_id"] for item in suppressed_queue["items"]}
    validation.check(all(queue["items"][index]["queue_id"] not in suppressed_ids for index in range(4)), "all unchanged explicit decisions suppressed")
    validation.check(queue["items"][4]["queue_id"] in suppressed_ids, "unchecked pending item remains active")

    remaining = remaining_queue_after_decisions(queue, applied)
    validation.equal(len(remaining["items"]), len(queue["items"]) - len(actions), "remaining queue removes only explicit decisions")
    validation.equal(remaining["items"][0]["queue_id"], queue["items"][4]["queue_id"], "remaining queue preserves unchecked item")
    validation.check(remaining["queue_revision"] != queue["queue_revision"], "remaining queue receives deterministic new revision")
    validation.equal(
        remaining_queue_after_decisions(queue, applied)["queue_revision"],
        remaining["queue_revision"],
        "remaining queue rebuild is deterministic",
    )

    changed = dict(dedupe["records"][0])
    changed["content_sha256"] = sha256_text(changed["content_sha256"] + ":changed")
    changed["prompt_sha256"] = sha256_text(changed["prompt_text"] + " changed")
    changed_bundle = {"run_id": "changed-version", "observed_at": "2026-08-31T03:00:00Z", "records": [changed]}
    changed_queue, _, _ = build_queue(changed_bundle, next_state, config)
    validation.equal(len(changed_queue["items"]), 1, "changed content hash is reviewable after prior decision")

    stale_fixture = {**decision_fixture, "queue_revision": "stale-revision"}
    try:
        validate_and_apply(queue, stale_fixture, state)
    except ValueError as exc:
        stale_rejected = "queue_revision" in str(exc)
    else:
        stale_rejected = False
    validation.check(stale_rejected, "stale decision revision fails closed")

    empty_applied, empty_pending, empty_state = validate_and_apply(
        queue,
        {"queue_revision": queue["queue_revision"], "decisions": []},
        state,
    )
    validation.equal(empty_applied["decisions"], [], "empty decision draft has no implicit decisions")
    validation.equal(empty_pending["record_count"], 0, "empty decision draft promotes nothing")
    validation.equal(empty_state.get("review_decision_ledger", {}), state.get("review_decision_ledger", {}), "empty decision draft creates no ledger entries")

    validate_robots(validation)
    validate_classifier(validation)

    media_files = [path for path in DATA_ROOT.rglob("*") if path.is_file() and path.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}]
    validation.equal(media_files, [], "no source image binary downloaded")

    dry_run_targets = [DATA_ROOT / "state.json", DATA_ROOT / "raw" / "sample-canary-v1.json"]
    before = {str(path): sha256_text(path.read_text(encoding="utf-8")) for path in dry_run_targets}
    dry_run = subprocess.run([sys.executable, str(MODULE_DIR / "collect.py")], capture_output=True, text=True, check=False)
    after = {str(path): sha256_text(path.read_text(encoding="utf-8")) for path in dry_run_targets}
    validation.check(dry_run.returncode == 0 and '"network": false' in dry_run.stdout.casefold(), "collector default is offline dry-run")
    validation.equal(after, before, "collector default mutates no workflow records")
    fetch_without_apply = subprocess.run([sys.executable, str(MODULE_DIR / "collect.py"), "--fetch"], capture_output=True, text=True, check=False)
    validation.check(fetch_without_apply.returncode != 0 and "requires both --fetch and --apply" in fetch_without_apply.stderr, "network requires fetch plus apply")
    runner_dry_run = subprocess.run([sys.executable, str(MODULE_DIR / "run_pipeline.py")], capture_output=True, text=True, check=False)
    validation.check(runner_dry_run.returncode == 0 and '"canonical_mutation": false' in runner_dry_run.stdout.casefold(), "pipeline default is offline and non-canonical")
    runner_fetch_without_apply = subprocess.run([sys.executable, str(MODULE_DIR / "run_pipeline.py"), "--fetch"], capture_output=True, text=True, check=False)
    validation.check(runner_fetch_without_apply.returncode != 0 and "requires both --fetch and --apply" in runner_fetch_without_apply.stderr, "pipeline network mode requires fetch plus apply")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        queue_path = temp_root / "review_queue" / "current.json"
        draft_path = temp_root / "decisions" / "decision-draft.json"
        bundle_path = temp_root / "dedupe.json"
        state_path = temp_root / "state.json"
        config_path = temp_root / "config.json"
        for path, value in (
            (queue_path, queue),
            (bundle_path, later_bundle),
            (state_path, state),
            (config_path, config),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(stable_json(value), encoding="utf-8")
        build_apply = subprocess.run(
            [
                sys.executable,
                str(MODULE_DIR / "build_review_queue.py"),
                "--input", str(bundle_path),
                "--state", str(state_path),
                "--config", str(config_path),
                "--queue-output", str(queue_path),
                "--draft-output", str(draft_path),
                "--no-js",
                "--apply",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        history_files = list((queue_path.parent / "history").glob("*.json"))
        merged_on_disk = read_json(queue_path) if queue_path.exists() else {"items": []}
        validation.check(build_apply.returncode == 0, "queue builder apply succeeds in isolated root", build_apply.stderr)
        validation.equal(len(history_files), 1, "queue replacement persists immutable history snapshot")
        validation.equal(len(merged_on_disk["items"]), len(queue["items"]) + 1, "queue builder apply merges prior pending rows")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        queue_path = temp_root / "review_queue" / "current.json"
        decisions_path = temp_root / "decisions-input.json"
        state_path = temp_root / "state.json"
        config_path = temp_root / "config.json"
        draft_path = temp_root / "decision-draft.json"
        applied_path = temp_root / "applied.json"
        pending_path = temp_root / "canonicalization-pending.json"
        remaining_path = temp_root / "remaining.json"
        # This subprocess contract is an isolated one-decision-per-action
        # fixture. Do not inherit cumulative decisions from the operator's
        # real state.json, otherwise the expected counts drift after every
        # legitimate review session.
        isolated_state = {
            **state,
            "review_decision_counts": {},
            "review_decision_ledger": {},
            "rejected_content_hashes": {},
        }
        for path, value in (
            (queue_path, queue),
            (decisions_path, decision_fixture),
            (state_path, isolated_state),
            (config_path, config),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(stable_json(value), encoding="utf-8")
        apply_run = subprocess.run(
            [
                sys.executable,
                str(MODULE_DIR / "apply_decisions.py"),
                "--queue", str(queue_path),
                "--decisions", str(decisions_path),
                "--state", str(state_path),
                "--config", str(config_path),
                "--draft-output", str(draft_path),
                "--applied-output", str(applied_path),
                "--pending-output", str(pending_path),
                "--remaining-output", str(remaining_path),
                "--no-js",
                "--apply",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        current_after = read_json(queue_path) if queue_path.exists() else {"items": []}
        state_after = read_json(state_path) if state_path.exists() else {}
        history_files = list((queue_path.parent / "history").glob("*.json"))
        validation.check(apply_run.returncode == 0, "decision apply succeeds in isolated root", apply_run.stderr)
        validation.equal(len(current_after["items"]), len(queue["items"]) - len(actions), "decision apply rewrites current queue with pending only")
        validation.check(all(path.exists() for path in (applied_path, pending_path, remaining_path, draft_path)), "decision apply writes durable and remaining artifacts")
        validation.equal(len(history_files), 1, "decision apply snapshots reviewed queue before replacement")
        validation.equal(state_after.get("review_decision_counts"), {action: 1 for action in sorted(actions)}, "decision apply persists cumulative action counts")

    failures = [check for check in validation.checks if not check["ok"]]
    report = {
        "schema_version": "opennana-validation-1.0",
        "passed": not failures,
        "check_count": len(validation.checks),
        "failure_count": len(failures),
        "checks": validation.checks,
    }
    if args.write_report:
        (ARCHIVE_ROOT / "qa" / "opennana_validation.json").write_text(stable_json(report), encoding="utf-8", newline="\n")
    print(stable_json(report), end="")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
