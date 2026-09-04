from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from src.github_sources import collect_sealed_intake as intake
from src.github_sources.intake_envelope import content_identity, parse_gallery, sha256, validate_envelope


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.prompt = "  Exact 한글 prompt.\r\nSecond line.  \r\n"
        self.raw = ('<a name="case-1"></a>\r\n### Example one\r\n'
                    '![local](../data/images/case1.jpg)\r\n![external](https://example.com/private.png)\r\n'
                    '**提示词：**\r\n```text\r\n' + self.prompt + '```\r\n').encode()
        self.blob = hashlib.sha1(f"blob {len(self.raw)}\0".encode() + self.raw).hexdigest()
        self.source = {"source_id": "github-fixture", "repository": "freestylefly/awesome-gpt-image-2", "enabled": True,
                       "prompt_path_globs": ["docs/*.md", "README.md"], "media_path_globs": ["data/images/*.jpg"]}
        self.registry = {"policy": {"allowlist_only": True}, "sources": [self.source]}
        self.fixture = {"commit_sha": "1" * 40, "tree_sha": "2" * 40,
                        "tree": [{"path": "docs/gallery-part-1.md", "mode": "100644", "type": "blob", "sha": self.blob, "size": len(self.raw)},
                                 {"path": "data/images/case1.jpg", "mode": "100644", "type": "blob", "sha": "3" * 40, "size": 30}]}
        self.calls = []

    def api(self, path, *args, **kwargs):
        self.calls.append(path)
        return {"sha": self.blob, "size": len(self.raw), "encoding": "base64", "content": base64.b64encode(self.raw).decode()}, {}

    def collect(self, checkpoint=None, **kwargs):
        return intake.collect_bundle(self.registry, checkpoint or intake.empty_checkpoint(), api=self.api,
            snapshot=lambda _: (self.fixture, {}), **kwargs)

    def ack(self, state):
        for entry in state["entries"].values():
            if entry["artifact_id"] is None:
                entry["artifact_id"] = "123"
        state["content_sha256"] = intake._digest_checkpoint(state)
        return state

    def test_exact_prompt_and_pinned_media_unknown_rights(self):
        bundle, pending, summary = self.collect()
        self.assertEqual(summary["containers_completed"], 1)
        row = bundle["records"][0]
        self.assertEqual(row["original_prompt"]["text"], self.prompt)
        self.assertEqual(row["original_prompt"]["sha256"], sha256(self.prompt.encode()))
        self.assertEqual(row["rights"]["rights_status"], "unknown")
        self.assertFalse(row["rights"]["release_eligible"])
        self.assertEqual(row["media_refs"][0]["git_blob_sha1"], "3" * 40)
        self.assertIn("/" + "1" * 40 + "/data/images/case1.jpg", row["media_refs"][0]["url"])
        self.assertEqual(len(row["media_refs"]), 1)
        self.assertEqual(row["deferred_media"][0]["reason"], "external_or_query_image_not_fetched")
        self.assertEqual(len(self.calls), 1)
        serialized = json.dumps(pending)
        self.assertNotIn("Exact", serialized)
        self.assertNotIn("original_prompt", serialized)
        with self.assertRaises(intake.IntakeError):
            intake.validate_checkpoint(pending)  # no upload acknowledgment yet

    def test_same_blob_and_media_skip_but_media_change_recollects(self):
        _, state, _ = self.collect()
        state = self.ack(state)
        self.calls.clear()
        bundle, _, summary = self.collect(state)
        self.assertEqual(summary["unchanged"], 1)
        self.assertEqual(bundle["records"], [])
        self.assertFalse(self.calls)
        self.fixture["tree"][1]["sha"] = "4" * 40
        _, _, summary = self.collect(state)
        self.assertEqual(summary["containers_fetched"], 1)

    def test_ciphertext_retention_reoffers_old_state(self):
        _, state, _ = self.collect()
        state = self.ack(state)
        next(iter(state["entries"].values()))["last_sealed_at"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        state["content_sha256"] = intake._digest_checkpoint(state)
        _, _, summary = self.collect(state)
        self.assertEqual(summary["containers_fetched"], 1)

    def test_bad_blob_not_checkpointed(self):
        def corrupted(*args, **kwargs):
            body, headers = self.api(*args, **kwargs)
            body["content"] = base64.b64encode(b"different").decode()
            return body, headers
        bundle, state, summary = intake.collect_bundle(self.registry, intake.empty_checkpoint(), api=corrupted,
            snapshot=lambda _: (self.fixture, {}))
        self.assertEqual(summary["status"], "partial")
        self.assertEqual(state["entries"], {})
        self.assertEqual(bundle["records"], [])

    def test_unsupported_file_and_repository_remain_deferred(self):
        self.fixture["tree"][0]["path"] = "README.md"
        _, state, summary = self.collect()
        self.assertEqual(summary["unsupported_deferred"], 1)
        self.assertFalse(state["entries"])
        self.assertFalse(self.calls)
        self.registry["sources"][0]["repository"] = "other/repo"
        with patch.object(intake.collector, "live_fixture", side_effect=AssertionError("network forbidden")):
            _, state, summary = intake.collect_bundle(self.registry, intake.empty_checkpoint())
        self.assertEqual(summary["unsupported_deferred"], 1)

    def test_twenty_container_limit_is_not_twenty_records(self):
        template = self.fixture["tree"][0]
        self.fixture["tree"] = [{**template, "path": f"docs/gallery-part-{i}.md"} for i in range(1, 24)]
        _, _, summary = self.collect()
        self.assertEqual(summary["containers_fetched"], 20)
        self.assertEqual(summary["bounded_deferred"], 3)
        self.assertEqual(len(self.calls), 20)

    def test_partial_parser_preserves_raw_but_does_not_complete_container(self):
        self.raw += b'<a name="case-2"></a>\n### Missing prompt\n'
        self.blob = hashlib.sha1(f"blob {len(self.raw)}\0".encode() + self.raw).hexdigest()
        self.fixture["tree"][0].update(sha=self.blob, size=len(self.raw))
        bundle, state, _ = self.collect()
        self.assertEqual(len(bundle["records"]), 1)
        self.assertFalse(bundle["containers"][0]["parse_complete"])
        self.assertEqual(bundle["containers"][0]["raw_utf8"].encode(), self.raw)
        self.assertFalse(state["entries"])

    def test_markdown_inside_prompt_is_not_mistaken_for_source_media(self):
        raw = self.raw.replace(self.prompt.encode(), b'![not evidence](../data/images/case1.jpg)\r\n')
        # Remove the genuine source image; the only matching path is now inside
        # the literal prompt, which must not fabricate a media relationship.
        raw = raw.replace(b'![local](../data/images/case1.jpg)\r\n', b'')
        parsed = parse_gallery(raw, source=self.source, path='docs/gallery-part-1.md', commit='1'*40,
            tree_sha='2'*40, blob_sha=hashlib.sha1(f'blob {len(raw)}\0'.encode()+raw).hexdigest(),
            media_tree={'data/images/case1.jpg': {'mode':'100644','sha':'3'*40}},
            observed_at=datetime.now(timezone.utc).isoformat())
        self.assertFalse(parsed['records'][0]['media_refs'])

    def test_tampering_rights_prompt_or_checkpoint_is_rejected(self):
        bundle, state, _ = self.collect()
        row = copy.deepcopy(bundle["records"][0])
        row["original_prompt"]["text"] += "tamper"
        row["content_sha256"] = content_identity(row)
        with self.assertRaises(ValueError):
            validate_envelope(row)
        row = copy.deepcopy(bundle["records"][0])
        row["rights"]["release_eligible"] = True
        row["content_sha256"] = content_identity(row)
        with self.assertRaises(ValueError):
            validate_envelope(row)
        state["entries"][next(iter(state["entries"]))]["prompt"] = "leak"
        state["content_sha256"] = intake._digest_checkpoint(state)
        with self.assertRaises(intake.IntakeError):
            intake.validate_checkpoint(state, pending=True)

    def test_offline_default_and_missing_key_do_not_open_network(self):
        with patch.object(intake, "collect_bundle", side_effect=AssertionError("network forbidden")):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(intake.main([]), 0)
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                args = ["--fetch", "--collect", "--public-key", str(base / "missing.json"),
                        "--checkpoint", str(base / "state.json"), "--pending-checkpoint", str(base / "pending.json"),
                        "--sealed-output", str(base / "sealed.json"), "--report", str(base / "summary.json")]
                with self.assertRaises(intake.IntakeError):
                    intake.main(args)
                self.assertFalse(list(base.iterdir()))

    def test_acknowledgment_binds_exact_ciphertext(self):
        _, state, _ = self.collect()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            sealed, pending, checkpoint = (base / name for name in ("sealed.json", "pending.json", "checkpoint.json"))
            sealed.write_bytes(b"ciphertext fixture")
            intake.collector.write_json_atomic(pending, {"checkpoint": state, "sealed_file_sha256": sha256(sealed.read_bytes())})
            intake.acknowledge_upload(pending, sealed, "123", checkpoint)
            self.assertEqual(next(iter(intake.collector.read_json(checkpoint)["entries"].values()))["artifact_id"], "123")
            sealed.write_bytes(b"tampered")
            with self.assertRaises(intake.IntakeError):
                intake.acknowledge_upload(pending, sealed, "124", checkpoint)

    def test_pacing_waits_between_sequential_calls(self):
        values = iter([0.0, 0.2, 1.0])
        sleeps = []
        paced = intake.PacedAPI(get=lambda *args: ({}, {}), clock=lambda: next(values), sleep=sleeps.append)
        paced("one")
        paced("two")
        self.assertAlmostEqual(sleeps[0], 0.8)


if __name__ == "__main__":
    unittest.main()
