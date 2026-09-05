"""Public packet integrity and least-privilege deployment regression tests."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform/v2/local"))
import public_deployment as deploy
from frontend_projection import encoded, sha


class PublicDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.previous = deploy.ROOT
        deploy.ROOT = self.root
        source = self.root / "platform/v2/worker/public-gallery.js"
        source.parent.mkdir(parents=True)
        source.write_bytes((ROOT / "platform/v2/worker/public-gallery.js").read_bytes())
        self.files = {name: b"fixture" for name in deploy.STATIC_NAMES}
        self.counts = {"images": 1, "groups": 1, "variants": 0}
        self.files["data/catalog.json"] = encoded({"mode": "public", "counts": self.counts})
        self.manifest = {name: sha(raw) for name, raw in self.files.items()}
        self.identity = {"served_files": self.manifest, "grant_sha256": sha(b"pending grant")}
        self.candidate_id = sha(encoded(self.identity))
        self.candidate = self.root / deploy.CANDIDATE_BASE / self.candidate_id
        for name, raw in self.files.items():
            path = self.candidate / "assets" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        (self.candidate / "grant.json").write_bytes(b"pending grant")
        receipt = {"candidate_id": self.candidate_id, "identity": self.identity,
                   "schema_version": "image-gallery-public-reference-candidate-1", "mode": "public",
                   "assets_directory": "assets", "served_files": self.manifest,
                   "grant_sha256": sha(b"pending grant"), "counts": self.counts}
        (self.candidate / "candidate.json").write_bytes(encoded(receipt))

    def tearDown(self):
        deploy.ROOT = self.previous
        self.tmp.cleanup()

    def test_dry_run_creates_no_packet(self):
        result = deploy.stage(self.candidate_id)
        self.assertEqual(result["status"], "dry_run")
        self.assertFalse((self.root / result["path"]).exists())

    def test_packet_binds_exact_files_and_existing_domain(self):
        result = deploy.stage(self.candidate_id, apply=True)
        packet, manifest = deploy.verify_packet(result["deployment_id"])
        config = json.loads((packet / "wrangler.json").read_bytes())
        self.assertEqual(config["routes"], [{"pattern": "photoposting.shop", "custom_domain": True}])
        self.assertEqual(config["name"], deploy.WORKER_NAME)
        self.assertEqual(config["vars"]["PUBLIC_RELEASE_ID"], self.candidate_id)
        self.assertEqual((packet / config["assets"]["directory"]).resolve(), self.candidate / "assets")
        self.assertNotIn("r2_buckets", config)
        self.assertNotIn("services", config)
        self.assertEqual(manifest["status"], "pending_human_release")
        self.assertEqual(deploy.stage(self.candidate_id, apply=True), result)

    def test_asset_modified_after_staging_rejected(self):
        result = deploy.stage(self.candidate_id, apply=True)
        (self.candidate / "assets/gallery.js").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "asset_hash_mismatch"):
            deploy.verify_packet(result["deployment_id"])

    def test_extra_private_asset_rejected(self):
        (self.candidate / "assets/grant.json").write_bytes(b"secret")
        with self.assertRaisesRegex(ValueError, "unregistered_asset"):
            deploy.stage(self.candidate_id)

    def test_config_modified_after_staging_rejected(self):
        result = deploy.stage(self.candidate_id, apply=True)
        packet = self.root / result["path"]
        (packet / "wrangler.json").write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "packet_hash_mismatch"):
            deploy.verify_packet(result["deployment_id"])

    def test_invalid_candidate_id_cannot_escape_root(self):
        with self.assertRaisesRegex(ValueError, "invalid_candidate_id"):
            deploy.stage("../../.env")

    def test_changed_grant_rejected(self):
        (self.candidate / "grant.json").write_bytes(b"unrelated approval")
        with self.assertRaisesRegex(ValueError, "grant_hash_mismatch"):
            deploy.stage(self.candidate_id)


if __name__ == "__main__":
    unittest.main()
