"""Offline source-only packaging and non-disclosing credential scan regressions."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from unittest.mock import patch

try:
    from qa import validate_repository_boundary as boundary
except ModuleNotFoundError:
    import validate_repository_boundary as boundary


class RepositoryBoundaryTests(unittest.TestCase):
    def test_source_paths_include_jsonc_sql_and_data_empty_shell(self):
        for path in ("platform/v2/wrangler.jsonc", "db/v2/0002_api_budget.sql", "qa/test_repository_boundary.py", "deploy/cloudflare-public/source/_headers", "deploy/cloudflare-staging/public/admin/index.html"):
            with self.subTest(path=path):
                self.assertIsNone(boundary.path_error(path))

    def test_private_generated_and_secret_paths_are_denied(self):
        for path in ("assets/generated_staging/manual/example.png", "deploy/cloudflare-public/public/catalog.json", "deploy/cloudflare-public/release-record.json", "legacy/current_archive/index.html", "platform/v2/runtime/receipt.json", "qa/fixtures/.env.example", "src/.dev.vars", "docs/credentials.env", "qa/private.key", "qa/fixtures/new.sqlite", "data/private-research/github-sources/fixtures/unreviewed.json", "../README.md", "/README.md", "C:/README.md", "src/node_modules/example.js", "src/unknown.bin"):
            with self.subTest(path=path):
                self.assertIsNotNone(boundary.path_error(path))

    def test_private_exceptions_are_exact(self):
        for path in boundary.ALLOWED_PRIVATE_FILES:
            self.assertIsNone(boundary.path_error(path))
        self.assertEqual(len(boundary.ALLOWED_PRIVATE_FILES), 2)

    def test_synthetic_dsn_exception_is_exact_path_and_value(self):
        path = "platform/v2/tests/runtime.test.mjs"
        dsn = sorted(boundary.SYNTHETIC_DSNS[path])[0]
        self.assertEqual(boundary.content_errors(path, dsn.encode()), [])
        for other_path, other_dsn in (("qa/other_test.py", dsn), (path, dsn + "?password=unexpected"), (path, dsn.replace("u:p@", "u:changed@"))):
            with self.subTest(path=other_path):
                self.assertIn("credential-like value", boundary.content_errors(other_path, other_dsn.encode()))

    def test_jsonc_and_sql_secret_values_are_scanned_without_echo(self):
        secret = "iar_" + "v2_" + "A" * 43
        for path in ("platform/v2/wrangler.jsonc", "db/v2/example.sql"):
            errors = boundary.content_errors(path, json.dumps({"token": secret}).encode())
            self.assertEqual(errors, ["credential-like value"])
            self.assertNotIn(secret, repr(errors))

    def test_other_credentials_in_synthetic_fixture_are_not_exempt(self):
        token = "gh" + "p_" + "A" * 30
        self.assertEqual(boundary.content_errors("platform/v2/tests/runtime.test.mjs", token.encode()), ["credential-like value"])

    def test_synthetic_assignments_only_allow_exact_path_and_literal(self):
        for path, assignments in boundary.SYNTHETIC_ASSIGNMENTS.items():
            for assignment in assignments:
                self.assertEqual(boundary.content_errors(path, assignment.encode()), [])
                self.assertEqual(boundary.content_errors("qa/other_test.py", assignment.encode()), ["credential-like value"])
                # Mutate the value, not a JSON key's closing quote: both Python
                # keyword and quoted JSON assignments must remain parse-shaped.
                changed_value = assignment[:-1] + "A" + assignment[-1]
                self.assertEqual(boundary.content_errors(path, changed_value.encode()), ["credential-like value"])

    def test_every_reviewed_synthetic_dsn_is_scoped_to_exact_file_and_value(self):
        for path, dsns in boundary.SYNTHETIC_DSNS.items():
            for dsn in dsns:
                self.assertEqual(boundary.content_errors(path, dsn.encode()), [])
                self.assertEqual(boundary.content_errors("qa/unreviewed_test.py", dsn.encode()), ["credential-like value"])
                self.assertEqual(boundary.content_errors(path, (dsn + "unexpected").encode()), ["credential-like value"])

    def test_public_jwk_cannot_contain_private_parameters(self):
        path = "config/intake-recipient.public.jwk.json"
        public = {"kty": "RSA", "n": "synthetic", "e": "AQAB"}
        self.assertEqual(boundary.content_errors(path, json.dumps(public).encode()), [])
        for parameter in ("d", "p", "q", "dp", "dq", "qi", "oth"):
            errors = boundary.content_errors(path, json.dumps({**public, parameter: "synthetic"}).encode())
            self.assertIn("recipient key must contain public RSA material only", errors)

    def test_environment_example_does_not_hide_values(self):
        self.assertEqual(boundary.content_errors(".env.example", b"DATABASE_URL=\nARCHIVE_ADMIN_AUTH_MODE=disabled\n"), [])
        self.assertIn("secret field in environment example must be empty", boundary.content_errors(".env.example", b"NEON_API_KEY=synthetic\n"))
        self.assertIn("secret field in environment example must be empty", boundary.content_errors(".ENV.EXAMPLE", b"NEON_API_KEY=synthetic\n"))

    def test_oversized_binary_and_non_utf8_sources_fail(self):
        for raw in (b"x" * (boundary.MAX_TRACKED_BYTES + 1), b"x\0y", b"\xff"):
            self.assertTrue(boundary.content_errors("qa/example.py", raw))

    def test_default_scan_reads_index_blob_not_clean_worktree(self):
        secret = ("gh" + "p_" + "A" * 30).encode()
        with patch.object(boundary, "git_bytes", side_effect=[str(len(secret)).encode(), secret]) as git, patch.object(boundary, "_working_bytes", return_value=b"clean") as working:
            errors = boundary.validate_paths(["src/example.py"], entries={"src/example.py": ("100644", "a" * 40)})
        self.assertEqual(errors, ["credential-like value: src/example.py"])
        working.assert_not_called()
        self.assertEqual(git.call_count, 2)

    def test_index_symlinks_and_submodules_fail_without_loading(self):
        for mode in ("120000", "160000"):
            with patch.object(boundary, "git_bytes") as git:
                errors = boundary.validate_paths(["src/example.py"], entries={"src/example.py": (mode, "a" * 40)})
            self.assertTrue(errors)
            git.assert_not_called()

    def test_unmerged_index_is_rejected(self):
        with patch.object(boundary, "git_bytes", return_value=b"100644 abc 1\tsrc/example.py\0"):
            with self.assertRaisesRegex(ValueError, "unmerged"):
                boundary.index_entries()

    def test_candidate_proposal_filters_artifacts_and_checks_source_without_staging(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["validator", "--candidate-files"]), patch.object(boundary, "candidate_paths", return_value=["src/example.py", "assets/unapproved.png"]), patch.object(boundary, "validate_paths", return_value=[]) as validate, contextlib.redirect_stdout(output):
            status = boundary.main()
        result = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(result["paths"], ["src/example.py"])
        self.assertEqual(result["excluded_paths"], ["assets/unapproved.png"])
        validate.assert_called_once_with(["src/example.py"], worktree=True)

    def test_failed_candidate_scan_does_not_return_stageable_paths(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["validator", "--candidate-files"]), patch.object(boundary, "candidate_paths", return_value=["src/example.py"]), patch.object(boundary, "validate_paths", return_value=["credential-like value: src/example.py"]), contextlib.redirect_stdout(output):
            status = boundary.main()
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue())["paths"], [])


if __name__ == "__main__":
    unittest.main()
