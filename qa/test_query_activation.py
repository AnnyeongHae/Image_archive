"""Offline query-activation regression tests, using only synthetic temp fixtures."""
from __future__ import annotations

import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'platform/v2/local'))
import query_activation as q


def config(snapshot):
    return {
        'compatibility_date': '2026-09-04', 'main': 'worker.bundle.mjs', 'name': q.WORKER,
        'no_bundle': True, 'observability': {'enabled': False}, 'preview_urls': False,
        'r2_buckets': [{'binding': 'PRIVATE_MEDIA', 'bucket_name': 'image-prompt-archive-private-staging'}],
        'ratelimits': [{'name': 'OWNER_RATE_LIMITER', 'namespace_id': '26090402', 'simple': {'limit': 20, 'period': 60}}],
        'workers_dev': True,
        'vars': {'ACCESS_JWT_REQUIRED': 'true', 'DAILY_QUERY_CALL_LIMIT': '20',
            'DAILY_QUERY_TOKEN_LIMIT': '40000', 'LIVE_QUERY_EMBEDDING_ENABLED': 'false',
            'OWNER_EMAIL_ALLOWLIST': '["andrew4may@gmail.com"]',
            'POLICY_AUD': 'ce2ce161bc489c0c16f510706bf62ac48bb042a032752f1059ad06d2a0349ee8',
            'PRIVATE_API_ENABLED': 'true', 'SNAPSHOT_ID': snapshot,
            'SNAPSHOT_MANIFEST_SHA256': q.PLAN_HASH,
            'TEAM_DOMAIN': 'https://travel-agency.cloudflareaccess.com',
            'TEXT_COLLECTION': 'image_archive_v2_' + snapshot + '_text512'},
    }


class ActivationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        identity = {'synthetic_fixture': True}
        self.snapshot = q.sha(q.encoded(identity))
        self.addCleanup(patch.stopall)
        patch.object(q, 'SNAPSHOT', self.snapshot).start()
        self.disabled = config(self.snapshot)
        files = {}
        for relative in q.WORKER_SOURCES:
            raw = ('// synthetic ' + relative + '\n').encode()
            self.write(relative, raw)
            files[relative] = {'sha256': q.sha(raw), 'bytes': len(raw)}
        self.artifacts = {'worker.bundle.mjs': b'// frozen synthetic bundle\n',
            'wrangler.candidate.json': q.encoded(self.disabled),
            'public-source-manifest.json': q.encoded({'files': files})}
        for name, raw in self.artifacts.items():
            self.write(q.BASE + '/' + name, raw)
        self.write(q.GENERATOR, b'# synthetic frozen generator\n')
        self.docs = {
            'candidate': {'targets': {'worker': q.WORKER, 'cloudflare_account_id': q.ACCOUNT},
                'new_embedding_enabled': False, 'secrets_included': False, 'snapshot_id': self.snapshot,
                'snapshot_manifest_sha256': q.PLAN_HASH,
                'files': {name: {'sha256': q.sha(raw), 'bytes': len(raw)} for name, raw in self.artifacts.items()}},
            'deployment': {'candidate_sha256': q.DISABLED_CANDIDATE,
                'worker': {'name': q.WORKER, 'version_id': q.VERSION,
                           'bundle_sha256': q.sha(self.artifacts['worker.bundle.mjs'])},
                'admin': {'new_query_embedding_enabled': False}, 'security': {'qdrant_key_access': 'r'}},
            'read_key_receipt': {'field_names': q.OLD_SECRET_NAMES, 'decoded_access': 'r',
                'preserved_fields': ['API_TOKEN_HASHES', 'DATABASE_URL', 'QDRANT_ENDPOINT'],
                'replaced_fields': ['QDRANT_API_KEY'], 'original_runtime_modified': False,
                'snapshot_id': self.snapshot, 'snapshot_manifest_sha256': q.PLAN_HASH},
            'snapshot': {'snapshot_id': self.snapshot, 'identity': identity,
                         'counts': {'items': 379, 'text_vectors': 377}},
            'domain_plan': {'hostname': 'api.photoposting.shop', 'service': q.WORKER,
                'deployments': {'deployments': [{'versions': [{'percentage': 100, 'version_id': q.VERSION}]}]}},
            'domain_bound': {'status': 'bound', 'worker_code_changed': False, 'plan_sha256': q.API_PLAN,
                'domains': [{'hostname': 'api.photoposting.shop', 'service': q.WORKER,
                             'zone_id': q.ZONE, 'enabled': True}]},
            'access': {'saved': True, 'added_target': 'api.photoposting.shop/api/admin/v2/*',
                       'owner_worker_check': 'unchanged'},
        }
        self.pins = {label: ('data/private-research/fixtures/' + label + '.json', '') for label in self.docs}
        patch.object(q, 'PINNED_INPUTS', self.pins).start()
        for label in self.docs:
            self.repin(label)

    def write(self, relative, raw):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)

    def repin(self, label):
        raw = q.encoded(self.docs[label])
        relative = self.pins[label][0]
        self.write(relative, raw)
        self.pins[label] = (relative, q.sha(raw))

    def reject_doc(self, label, change, code):
        change(self.docs[label])
        self.repin(label)
        with self.assertRaisesRegex(q.ActivationError, '^' + code + '$'):
            q.candidate(self.root)

    def test_dry_run_is_stable_and_does_not_write(self):
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        first = q.freeze(self.root)
        self.assertEqual(first, q.freeze(self.root))
        self.assertEqual(first['status'], 'dry_run')
        self.assertEqual(before, sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*')))

    def test_only_approved_variables_change(self):
        manifest, files = q.candidate(self.root)
        active = json.loads(files['wrangler.activation.json'])
        expected = copy.deepcopy(self.disabled)
        expected['vars']['LIVE_QUERY_EMBEDDING_ENABLED'] = 'true'
        expected['vars']['DAILY_QUERY_CALL_LIMIT'] = '200'
        expected['vars']['DAILY_QUERY_TOKEN_LIMIT'] = '200000'
        self.assertEqual(active, expected)
        self.assertEqual(manifest['targets'], {'worker': q.WORKER, 'cloudflare_account_id': q.ACCOUNT, 'api_origin': q.ORIGIN})

    def test_bundle_and_rollback_are_byte_exact(self):
        _, files = q.candidate(self.root)
        self.assertEqual(files['worker.bundle.mjs'], self.artifacts['worker.bundle.mjs'])
        self.assertEqual(files['wrangler.rollback.json'], self.artifacts['wrangler.candidate.json'])

    def test_usage_claims_are_reservations_not_billed_tokens(self):
        manifest, _ = q.candidate(self.root)
        budget = manifest['query_budget']
        self.assertEqual((budget['model'], budget['dimension'], budget['input_type']), ('voyage-4-lite', 512, 'query'))
        self.assertEqual((budget['daily_provider_call_limit'], budget['daily_reservation_unit_limit']), (200, 200000))
        self.assertEqual(budget['reservation_formula'], 'UTF8(query).byteLength + 256')
        self.assertFalse(budget['reservation_is_actual_billed_tokens'])
        self.assertTrue(budget['uncertain_calls_remain_reserved'])
        self.assertEqual(budget['provider_auto_retries'], 0)

    def test_secret_names_only_and_readonly_audit_preserved(self):
        _, files = q.candidate(self.root)
        secrets = json.loads(files['secret-requirements.json'])
        self.assertEqual(secrets['required_additional_secret_names'], ['VOYAGE_API_KEY'])
        self.assertEqual(secrets['preserve_existing_secret_names'], q.OLD_SECRET_NAMES)
        self.assertFalse(secrets['secret_values_included'])
        self.assertTrue(secrets['bulk_original_runtime_secret_upload_forbidden'])
        self.assertTrue(secrets['pre_deploy_remote_secret_names_and_key_scope_recheck_required'])

    def test_no_credentials_or_network_are_accessed(self):
        original = q.read
        seen = []
        def guarded(root, relative, maximum=2 * 1048576):
            seen.append(relative)
            self.assertNotIn('.env', relative)
            self.assertNotIn('worker-secrets', relative)
            return original(root, relative, maximum)
        with patch.object(q, 'read', side_effect=guarded), patch('socket.create_connection', side_effect=AssertionError('network forbidden')):
            result = q.freeze(self.root)
        self.assertTrue(seen)
        self.assertEqual([result[k] for k in ('network_calls', 'credential_reads', 'new_model_calls')], [0, 0, 0])

    def test_candidate_does_not_authorize_external_changes(self):
        manifest, _ = q.candidate(self.root)
        for key in ('eligible_for_release', 'deployment_performed', 'secret_upload_performed', 'record_reembedding',
                    'private_media_transfer_authorized_by_this_candidate', 'public_image_release_authorized_by_this_candidate'):
            self.assertFalse(manifest[key], key)
        self.assertIn('separate_approved_live_query_canary', manifest['pending'])

    def test_files_and_sources_are_hash_bound(self):
        manifest, files = q.candidate(self.root)
        self.assertEqual(manifest['files'], {name: {'sha256': q.sha(raw), 'bytes': len(raw)} for name, raw in files.items()})
        source = json.loads(files['worker-source-manifest.json'])
        self.assertEqual(set(source['files']), set(q.WORKER_SOURCES))
        self.assertEqual(source['bundle_matches_deployed_version'], q.VERSION)

    def test_worker_source_drift_blocks(self):
        self.write(q.WORKER_SOURCES[0], b'// changed auth\n')
        with self.assertRaisesRegex(q.ActivationError, 'worker_source_drift'):
            q.candidate(self.root)

    def test_pinned_evidence_tamper_blocks(self):
        self.write(self.pins['deployment'][0], b'{}\n')
        with self.assertRaisesRegex(q.ActivationError, 'pinned_evidence_changed'):
            q.candidate(self.root)

    def test_disabled_artifact_tamper_blocks(self):
        self.write(q.BASE + '/worker.bundle.mjs', b'changed\n')
        with self.assertRaisesRegex(q.ActivationError, 'disabled_artifact_changed'):
            q.candidate(self.root)

    def test_readwrite_qdrant_key_audit_rejected(self):
        self.reject_doc('read_key_receipt', lambda d: d.update(decoded_access='rw'), 'read_only_key_audit_mismatch')

    def test_missing_secret_preservation_rejected(self):
        self.reject_doc('read_key_receipt', lambda d: d.update(preserved_fields=[]), 'read_only_key_audit_mismatch')

    def test_changed_deployed_version_rejected(self):
        self.reject_doc('deployment', lambda d: d['worker'].update(version_id='another-version'), 'deployed_baseline_mismatch')

    def test_snapshot_identity_tamper_rejected(self):
        self.reject_doc('snapshot', lambda d: d['identity'].update(changed=True), 'snapshot_identity_mismatch')

    def test_snapshot_count_tamper_rejected(self):
        self.reject_doc('snapshot', lambda d: d['counts'].update(items=380), 'snapshot_identity_mismatch')

    def test_wrong_domain_rejected(self):
        self.reject_doc('domain_bound', lambda d: d['domains'][0].update(hostname='evil.example'), 'domain_binding_mismatch')

    def test_wrong_zone_rejected(self):
        self.reject_doc('domain_bound', lambda d: d['domains'][0].update(zone_id='another-zone'), 'domain_binding_mismatch')

    def test_unsaved_access_rejected(self):
        self.reject_doc('access', lambda d: d.update(saved=False), 'access_binding_mismatch')

    def test_access_path_expansion_rejected(self):
        self.reject_doc('access', lambda d: d.update(added_target='api.photoposting.shop/*'), 'access_binding_mismatch')

    def test_runtime_extra_fields_rejected(self):
        for extra in ('routes', 'assets', 'secrets', 'account_id'):
            with self.subTest(extra=extra):
                altered = copy.deepcopy(self.disabled)
                altered[extra] = {}
                with self.assertRaisesRegex(q.ActivationError, 'runtime_shape_mismatch'):
                    q.validate_config(altered)

    def test_runtime_boundary_modifications_rejected(self):
        for key, value in [('compatibility_date', '2027-01-01'), ('name', 'another-worker'),
                           ('workers_dev', False), ('preview_urls', True), ('r2_buckets', []), ('ratelimits', [])]:
            with self.subTest(key=key):
                altered = copy.deepcopy(self.disabled)
                altered[key] = value
                with self.assertRaisesRegex(q.ActivationError, 'runtime_boundary_mismatch'):
                    q.validate_config(altered)

    def test_runtime_auth_and_snapshot_changes_rejected(self):
        for key, value in [('ACCESS_JWT_REQUIRED', 'false'), ('PRIVATE_API_ENABLED', 'false'),
                           ('LIVE_QUERY_EMBEDDING_ENABLED', 'true'), ('OWNER_EMAIL_ALLOWLIST', '[]'),
                           ('TEAM_DOMAIN', 'https://evil.example'), ('SNAPSHOT_ID', 'another-snapshot'),
                           ('TEXT_COLLECTION', 'another-collection'), ('DAILY_QUERY_CALL_LIMIT', '200')]:
            with self.subTest(key=key):
                altered = copy.deepcopy(self.disabled)
                altered['vars'][key] = value
                with self.assertRaisesRegex(q.ActivationError, 'runtime_approval_scope_mismatch'):
                    q.validate_config(altered)

    def test_added_runtime_secret_variable_rejected(self):
        altered = copy.deepcopy(self.disabled)
        altered['vars']['VOYAGE_API_KEY'] = 'synthetic-never-a-real-secret'
        with self.assertRaisesRegex(q.ActivationError, 'runtime_variables_mismatch'):
            q.validate_config(altered)

    def test_budget_expansion_rejected(self):
        for key in ('CALL_LIMIT', 'RESERVATION_LIMIT'):
            with self.subTest(key=key), patch.object(q, key, 99999):
                with self.assertRaisesRegex(q.ActivationError, 'activation_scope_expanded'):
                    q.candidate(self.root)

    def test_apply_requires_exact_external_hash(self):
        digest = q.freeze(self.root)['candidate_sha256']
        for supplied in (None, '', '0' * 64, digest.upper(), digest + '\n'):
            with self.subTest(supplied=supplied):
                with self.assertRaisesRegex(q.ActivationError, 'exact_external_approval_hash_required'):
                    q.freeze(self.root, apply=True, approved_candidate_sha256=supplied)
        self.assertFalse((self.root / 'data/private-research/platform-v2/query-activation-candidates').exists())

    def test_hash_without_apply_is_rejected(self):
        with self.assertRaisesRegex(q.ActivationError, 'approval_hash_requires_apply'):
            q.freeze(self.root, approved_candidate_sha256='0' * 64)

    def test_prepare_writes_review_files_without_inventing_approval(self):
        dry = q.freeze(self.root)
        result = q.freeze(self.root, prepare=True)
        self.assertEqual(result['candidate_sha256'], dry['candidate_sha256'])
        self.assertEqual(result['status'], 'prepared_locally_pending_approval')
        manifest = json.loads((self.root / result['candidate_directory'] / 'candidate.json').read_bytes())
        self.assertEqual(manifest['state'], 'review_pending')
        self.assertFalse(manifest['eligible_for_release'])
        self.assertFalse(manifest['deployment_performed'])
        self.assertIn('exact_candidate_human_approval', manifest['pending'])

    def test_prepare_and_apply_are_exclusive(self):
        with self.assertRaisesRegex(q.ActivationError, 'prepare_and_apply_are_exclusive'):
            q.freeze(self.root, prepare=True, apply=True, approved_candidate_sha256='0' * 64)

    def test_approved_local_freeze_exact_and_idempotent(self):
        dry = q.freeze(self.root)
        applied = q.freeze(self.root, apply=True, approved_candidate_sha256=dry['candidate_sha256'])
        directory = self.root / applied['candidate_directory']
        manifest, files = q.candidate(self.root)
        self.assertEqual({p.name for p in directory.iterdir()}, {*files, 'candidate.json'})
        self.assertEqual((directory / 'candidate.json').read_bytes(), q.encoded(manifest))
        self.assertEqual(q.sha((directory / 'candidate.json').read_bytes()), dry['candidate_sha256'])
        for name, raw in files.items():
            self.assertEqual((directory / name).read_bytes(), raw)
        self.assertEqual(applied, q.freeze(self.root, apply=True, approved_candidate_sha256=dry['candidate_sha256']))
        self.assertFalse(applied['deployment_performed'])
        self.assertFalse(applied['eligible_for_release'])

    def test_candidate_manifest_is_written_last(self):
        dry = q.freeze(self.root)
        original = Path.open
        written = []
        def observe(path, mode='r', *args, **kwargs):
            if mode == 'xb':
                written.append(path.name)
            return original(path, mode, *args, **kwargs)
        with patch.object(Path, 'open', observe):
            q.freeze(self.root, apply=True, approved_candidate_sha256=dry['candidate_sha256'])
        self.assertEqual(written[-1], 'candidate.json')

    def test_existing_conflict_not_overwritten(self):
        dry = q.freeze(self.root)
        relative = dry['candidate_directory'] + '/worker.bundle.mjs'
        self.write(relative, b'conflict-preserve-me')
        with self.assertRaisesRegex(q.ActivationError, 'immutable_candidate_conflict'):
            q.freeze(self.root, apply=True, approved_candidate_sha256=dry['candidate_sha256'])
        self.assertEqual((self.root / relative).read_bytes(), b'conflict-preserve-me')
        self.assertFalse((self.root / dry['candidate_directory'] / 'candidate.json').exists())

    def test_unexpected_output_not_removed_or_replaced(self):
        dry = q.freeze(self.root)
        self.write(dry['candidate_directory'] + '/unexpected.txt', b'preserve')
        with self.assertRaisesRegex(q.ActivationError, 'unexpected_candidate_files'):
            q.freeze(self.root, apply=True, approved_candidate_sha256=dry['candidate_sha256'])
        self.assertEqual((self.root / dry['candidate_directory'] / 'unexpected.txt').read_bytes(), b'preserve')

    def test_generator_change_invalidates_approval(self):
        digest = q.freeze(self.root)['candidate_sha256']
        self.write(q.GENERATOR, b'# changed synthetic generator\n')
        with self.assertRaisesRegex(q.ActivationError, 'exact_external_approval_hash_required'):
            q.freeze(self.root, apply=True, approved_candidate_sha256=digest)

    def test_unsafe_paths_refused(self):
        for relative in ('../escape', '/absolute', 'C:/absolute', 'a\\b', 'a//b', 'a/./b', 'a/../b'):
            with self.subTest(relative=relative), self.assertRaises(q.ActivationError):
                q.local_path(self.root, relative)
        with self.assertRaisesRegex(q.ActivationError, 'private_output_required'):
            q.local_path(self.root, 'dist/publish.json', private=True)

    def test_linked_path_refused(self):
        linked = self.root / 'linked'
        with patch.object(Path, 'is_symlink', lambda p: p == linked):
            with self.assertRaisesRegex(q.ActivationError, 'linked_path_refused'):
                q.local_path(self.root, 'linked/secret.json')

    def test_bounded_local_reads(self):
        self.write('oversized.json', b'123456')
        with self.assertRaisesRegex(q.ActivationError, 'local_input_too_large'):
            q.read(self.root, 'oversized.json', maximum=5)

    def test_cli_generic_errors_do_not_leak_exception_details(self):
        output = io.StringIO()
        with patch.object(q, 'freeze', side_effect=RuntimeError('secret-never-print-this')), contextlib.redirect_stdout(output):
            self.assertEqual(q.main([]), 1)
        self.assertNotIn('secret-never-print-this', output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['error_code'], 'query_activation_preparation_failed')

    def test_cli_default_is_dryrun(self):
        output = io.StringIO()
        with patch.object(q, 'freeze', return_value={'status': 'dry_run'}) as mock, contextlib.redirect_stdout(output):
            self.assertEqual(q.main([]), 0)
        mock.assert_called_once_with(prepare=False, apply=False, approved_candidate_sha256=None)
        self.assertEqual(json.loads(output.getvalue()), {'status': 'dry_run'})


if __name__ == '__main__':
    unittest.main()
