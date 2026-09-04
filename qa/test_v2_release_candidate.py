import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "platform/v2/local"))
import release_candidate as release


class ReleaseCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.runtime = self.root / "data/private-research/runtime"
        self.runtime.mkdir(parents=True)
        self.base = {"name": "image-archive-owner-api-v2", "main": "worker/index.js", "vars": {}}
        base_path = self.root / "platform/v2/wrangler.jsonc"
        base_path.parent.mkdir(parents=True)
        base_path.write_bytes(release.encoded(self.base))
        bundle_path = self.root / "platform/v2/.tmp/v2-worker-dry-run/index.js"
        bundle_path.parent.mkdir(parents=True)
        bundle_path.write_bytes(b"export default {};")
        variables = {key: "fixture" for key in ("PRIVATE_API_ENABLED", "DAILY_QUERY_CALL_LIMIT", "DAILY_QUERY_TOKEN_LIMIT",
            "ACCESS_JWT_REQUIRED", "TEAM_DOMAIN", "POLICY_AUD", "OWNER_EMAIL_ALLOWLIST", "SNAPSHOT_ID",
            "SNAPSHOT_MANIFEST_SHA256", "TEXT_COLLECTION")}
        variables["LIVE_QUERY_EMBEDDING_ENABLED"] = "false"
        self.config = {**self.base, "vars": variables}
        self.save_config()
        for target, value in (("ROOT", self.root),):
            p = patch.object(release, target, value);p.start();self.addCleanup(p.stop)
        for target, value in (("candidate_paths", lambda: ["src/fixture.py"]), ("validate_paths", lambda *a, **kw: []),
                              ("_working_bytes", lambda p: b"# synthetic source\n")):
            p = patch.object(release.boundary, target, value);p.start();self.addCleanup(p.stop)

    def save_config(self):
        raw = release.encoded(self.config)
        (self.runtime / "wrangler.runtime.json").write_bytes(raw)
        (self.runtime / "runtime-manifest.json").write_bytes(release.encoded({"files": {"wrangler.runtime.json": release.sha(raw)}}))

    def test_dry_run_and_frozen_candidate_do_not_approve_or_include_secrets(self):
        first = release.freeze(self.runtime)
        target = self.root / first["candidate_directory"]
        self.assertFalse(target.exists())
        result = release.freeze(self.runtime, apply=True)
        self.assertEqual(result["artifact_sha256"], first["artifact_sha256"])
        self.assertFalse(result["eligible_for_release"])
        self.assertFalse(result["deployment_performed"])
        self.assertEqual({p.name for p in target.iterdir()}, {"candidate.json", "worker.bundle.mjs", "wrangler.candidate.json", "public-source-manifest.json"})
        self.assertFalse(json.loads((target / "candidate.json").read_bytes())["secrets_included"])

    def test_runtime_secret_variable_and_live_embedding_are_rejected(self):
        self.config["vars"]["QDRANT_API_KEY"] = "fixture"
        self.save_config()
        with self.assertRaises(release.SnapshotError):release.candidate(self.runtime)
        del self.config["vars"]["QDRANT_API_KEY"]
        self.config["vars"]["LIVE_QUERY_EMBEDDING_ENABLED"] = "true"
        self.save_config()
        with self.assertRaises(release.SnapshotError):release.candidate(self.runtime)

    def test_source_scan_failure_blocks_candidate(self):
        with patch.object(release.boundary, "validate_paths", return_value=["synthetic failure"]):
            with self.assertRaisesRegex(release.SnapshotError, "source_boundary_failed"):
                release.candidate(self.runtime)

    def test_existing_different_candidate_file_is_not_overwritten(self):
        first = release.freeze(self.runtime, apply=True)
        target = self.root / first["candidate_directory"] / "worker.bundle.mjs"
        target.write_bytes(b"different fixture")
        with self.assertRaisesRegex(release.SnapshotError, "immutable_release_candidate_conflict"):
            release.freeze(self.runtime, apply=True)
        self.assertEqual(target.read_bytes(), b"different fixture")


if __name__ == "__main__":
    unittest.main()
