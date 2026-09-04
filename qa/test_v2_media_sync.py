import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

LOCAL = Path(__file__).resolve().parents[1] / "platform/v2/local"
sys.path.insert(0, str(LOCAL))
import media_sync as m


class MediaSyncTests(unittest.TestCase):
    def setUp(self):
        self.data = b"\x89PNG\r\n\x1a\nsynthetic-not-a-real-image"
        self.row = {"prepared_sha256": m.sha(self.data)}

    def adapter(self, found=None):
        class Fake:
            writes = 0
            def get(inner, key):
                return inner.value
            def put(inner, key, data):
                inner.writes += 1
                inner.value = data
        fake = Fake()
        fake.value = found
        return fake

    def test_upload_then_full_byte_readback(self):
        fake = self.adapter()
        self.assertEqual(m.upload_one(fake, self.row, self.data)["status"], "uploaded_verified")
        self.assertEqual(fake.writes, 1)

    def test_existing_equal_is_not_written(self):
        fake = self.adapter(self.data)
        self.assertEqual(m.upload_one(fake, self.row, self.data)["status"], "reused_verified")
        self.assertEqual(fake.writes, 0)

    def test_existing_conflict_stops_before_write(self):
        fake = self.adapter(b"wrong")
        with self.assertRaisesRegex(m.SnapshotError, "conflict"):
            m.upload_one(fake, self.row, self.data)
        self.assertEqual(fake.writes, 0)

    def test_limits(self):
        for limit in (0, 380, True):
            with self.assertRaises(m.SnapshotError):
                m.media_rows({}, limit)

    def run_child(self, script, **kwargs):
        return m.run_bounded([sys.executable, "-B", "-c", script], cwd=LOCAL,
            env=os.environ.copy(), **kwargs)

    def test_bounded_child_transfers_exact_stdin_and_collects_both_pipes(self):
        result = self.run_child("import sys; d=sys.stdin.buffer.read(); sys.stdout.buffer.write(d); sys.stderr.buffer.write(b'diagnostic')",
            data=self.data, stdout_limit=len(self.data), stderr_limit=10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, self.data)
        self.assertEqual(result.stderr, b"diagnostic")

    def test_stdout_limit_fails_without_echoing_child_output(self):
        with self.assertRaisesRegex(m.SnapshotError, "^r2_stdout_oversized$"):
            self.run_child("import sys; sys.stdout.buffer.write(b'X'*200000); sys.stdout.flush()", stdout_limit=64)

    def test_stderr_limit_fails_without_echoing_child_output(self):
        with self.assertRaisesRegex(m.SnapshotError, "^r2_stderr_oversized$"):
            self.run_child("import sys; sys.stderr.buffer.write(b'X'*200000); sys.stderr.flush()", stderr_limit=64)

    def test_child_timeout_cannot_be_blocked_by_stdin_writer(self):
        started = time.monotonic()
        with self.assertRaisesRegex(m.SnapshotError, "^r2_cli_timeout$"):
            self.run_child("import time; time.sleep(5)", data=b"x"*1048576, timeout=0.15)
        self.assertLess(time.monotonic()-started, 3)

    def test_no_communicate_or_unbounded_subprocess_run(self):
        with patch.object(subprocess.Popen, "communicate", side_effect=AssertionError("unbounded communicate")), patch.object(subprocess, "run", side_effect=AssertionError("unbounded run")):
            result = self.run_child("import sys; sys.stdout.buffer.write(b'bounded')", stdout_limit=7)
        self.assertEqual(result.stdout, b"bounded")

    def test_fixed_bucket_prefix_lock_excludes_local_concurrent_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with m.bucket_lock(root):
                locks = list((root / "data/private-research/platform-v2/media-sync").glob(".bucket-*.lock"))
                self.assertEqual(len(locks), 1)
                with self.assertRaisesRegex(m.SnapshotError, "^r2_local_writer_locked$"):
                    with m.bucket_lock(root):
                        self.fail("second cooperating writer entered")
            self.assertFalse(locks[0].exists())
            with m.bucket_lock(root):
                pass

    def test_lock_released_after_normal_failure_but_retained_for_uncertain_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(m.SnapshotError, "normal_failure"):
                with m.bucket_lock(root):
                    raise m.SnapshotError("normal_failure")
            with self.assertRaisesRegex(m.SnapshotError, "r2_cli_cleanup_failed"):
                with m.bucket_lock(root):
                    raise m.SnapshotError("r2_cli_cleanup_failed")
            with self.assertRaisesRegex(m.SnapshotError, "r2_local_writer_locked"):
                with m.bucket_lock(root):
                    self.fail("uncertain child must retain lock")

    def test_upload_readback_mismatch_is_not_success(self):
        fake = self.adapter()
        def corrupt(key, data):
            fake.writes += 1
            fake.value = data + b"changed"
        fake.put = corrupt
        with self.assertRaisesRegex(m.SnapshotError, "readback_mismatch"):
            m.upload_one(fake, self.row, self.data)

    def test_verified_source_bytes_match_snapshot_before_adapter_receives_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            path=root / "data/private-research/test/image.png"
            path.parent.mkdir(parents=True)
            path.write_bytes(self.data)
            row={**self.row, "prepared_relative_path":path.relative_to(root).as_posix(),
                 "prepared_bytes":len(self.data), "prepared_mime_type":"image/png"}
            self.assertEqual(m.verified_bytes(row,root),self.data)
            path.write_bytes(self.data[:-1]+b"X")
            with self.assertRaisesRegex(m.SnapshotError, "media_source_hash_mismatch"):
                m.verified_bytes(row,root)

    def test_put_transport_failure_is_uncertain_and_cleanup_failure_is_preserved(self):
        adapter=object.__new__(m.Wrangler)
        for upstream, expected in (("r2_cli_timeout", "r2_put_uncertain_requires_readback"),
                                   ("r2_stdout_oversized", "r2_put_uncertain_requires_readback"),
                                   ("r2_cli_cleanup_failed", "r2_cli_cleanup_failed")):
            with patch.object(adapter,"run",side_effect=m.SnapshotError(upstream)):
                with self.assertRaisesRegex(m.SnapshotError,"^"+expected+"$"):
                    adapter.put("private/v2/sha256/synthetic.png",self.data)


if __name__ == "__main__":
    unittest.main()
