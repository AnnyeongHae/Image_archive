"""Freeze a narrowly scoped owner-query activation candidate; never deploy it.

Default is offline/dry-run and reads no credential values. --prepare writes the
immutable local review files with approval pending. --apply additionally requires
an externally supplied exact --approved-candidate-sha256. Neither local mode
uploads secrets, deploys, or invokes Voyage. The legacy default-disabled
runtime/release builders remain unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[3]
GENERATOR = 'platform/v2/local/query_activation.py'
DISABLED_CANDIDATE = '66159b2862c9d329206b76575c2e498734d4adf3f80b58e34efb166ab1f6c2ae'
SNAPSHOT = 'db218336f4478bf138d9440de0ee605131ae5ef484f322c0e3d7bae2f6e28314'
PLAN_HASH = 'ae5910fb41af5c0e12d8c203bb203b90ebde3b249099da2ffb4edbe55724b183'
VERSION = '107d8e97-541b-449b-aec7-230df865b3d5'
WORKER = 'image-archive-owner-api-v2'
ORIGIN = 'https://api.photoposting.shop'
ACCOUNT = 'b39fad7b5ebf74e820209ed506fd989b'
ZONE = 'd500f888e2e47e3d20a13e3485b7eaed'
CALL_LIMIT = 200
RESERVATION_LIMIT = 200000
BASE = 'data/private-research/platform-v2/release-candidates/' + DISABLED_CANDIDATE
DOMAIN = 'data/private-research/platform-v2/domain-link-20260904/'
API_PLAN = 'c107c8a9f27fbbd6fa333861360a3e2daa2ea4e300f297331120fad689cf34fc'
PACKET_HASH = 'b2cc56cf537e2bb723e416791bceba5229132deba94f083bac2712e40abf9bca'
PINNED_INPUTS = {
    'candidate': (BASE + '/candidate.json', DISABLED_CANDIDATE),
    'deployment': ('data/private-research/platform-v2/activation-review/deployment-receipt-v1.json',
                   'f673906976699ff656a983739d3014b82585c5a7c1f121fb232abc03ee50bf30'),
    'read_key_receipt': ('data/private-research/platform-v2/activation-review/worker-execution-v1/' + PACKET_HASH + '/receipt.json', PACKET_HASH),
    'snapshot': ('data/private-research/v2/cloud-plans/' + PLAN_HASH + '/manifest.json', PLAN_HASH),
    'domain_plan': (DOMAIN + 'api-plan-' + API_PLAN + '.json', API_PLAN),
    'domain_bound': (DOMAIN + 'api-bound-' + API_PLAN + '.json', '0dd47cd9e384f65a97a1480fbddabc16e99dafa70c94b140aa653e8c284bcf1a'),
    'access': (DOMAIN + 'access-saved.json', '3057ce7ef2272b07d6eae95aaec369740240afcbe1a70b8be2a23bd1597daa7e'),
}
WORKER_SOURCES = tuple('platform/v2/worker/' + name + '.js' for name in ('auth', 'http', 'index', 'media', 'neon', 'retrieval'))
OLD_SECRET_NAMES = ['API_TOKEN_HASHES', 'DATABASE_URL', 'QDRANT_API_KEY', 'QDRANT_ENDPOINT']
HEX = re.compile(r'^[a-f0-9]{64}$')


class ActivationError(ValueError):
    """Only fixed redacted validation codes are exposed by the CLI."""


def require(condition, code):
    if not condition:
        raise ActivationError(code)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def local_path(root, relative, *, private=False):
    root = Path(root).resolve()
    require(isinstance(relative, str) and relative and not relative.startswith(('/', '\\'))
            and '\\' not in relative and ':' not in relative
            and all(part not in ('', '.', '..') for part in relative.split('/')), 'invalid_relative_path')
    path = root
    for part in relative.split('/'):
        path = path / part
        require(not path.is_symlink() and not (hasattr(path, 'is_junction') and path.is_junction()), 'linked_path_refused')
    require(path.resolve().is_relative_to(root), 'outside_archive_refused')
    if private:
        require(path.resolve().is_relative_to(root / 'data/private-research'), 'private_output_required')
    return path


def read(root, relative, maximum=2 * 1048576):
    with local_path(root, relative).open('rb') as stream:
        raw = stream.read(maximum + 1)
    require(len(raw) <= maximum, 'local_input_too_large')
    return raw


def load_baseline(root):
    docs = {}
    for label, (relative, digest) in PINNED_INPUTS.items():
        raw = read(root, relative)
        require(sha(raw) == digest, 'pinned_evidence_changed')
        docs[label] = json.loads(raw)
    candidate, deployed, packet, snapshot = (docs[key] for key in ('candidate', 'deployment', 'read_key_receipt', 'snapshot'))
    require(candidate['targets']['worker'] == WORKER and candidate['targets']['cloudflare_account_id'] == ACCOUNT
            and candidate['new_embedding_enabled'] is False and candidate['secrets_included'] is False
            and candidate['snapshot_id'] == SNAPSHOT and candidate['snapshot_manifest_sha256'] == PLAN_HASH,
            'disabled_candidate_scope_mismatch')
    require(deployed['candidate_sha256'] == DISABLED_CANDIDATE and deployed['worker']['name'] == WORKER
            and deployed['worker']['version_id'] == VERSION and deployed['admin']['new_query_embedding_enabled'] is False
            and deployed['security']['qdrant_key_access'] == 'r', 'deployed_baseline_mismatch')
    require(packet['field_names'] == OLD_SECRET_NAMES and packet['decoded_access'] == 'r'
            and packet['preserved_fields'] == ['API_TOKEN_HASHES', 'DATABASE_URL', 'QDRANT_ENDPOINT']
            and packet['replaced_fields'] == ['QDRANT_API_KEY'] and packet['original_runtime_modified'] is False
            and packet['snapshot_id'] == SNAPSHOT and packet['snapshot_manifest_sha256'] == PLAN_HASH,
            'read_only_key_audit_mismatch')
    require(snapshot['snapshot_id'] == SNAPSHOT and sha(encoded(snapshot['identity'])) == SNAPSHOT
            and snapshot['counts']['items'] == 379 and snapshot['counts']['text_vectors'] == 377,
            'snapshot_identity_mismatch')
    plan, bound, access = (docs[key] for key in ('domain_plan', 'domain_bound', 'access'))
    require(plan['hostname'] == 'api.photoposting.shop' and plan['service'] == WORKER
            and plan['deployments']['deployments'][0]['versions'] == [{'percentage': 100, 'version_id': VERSION}],
            'domain_deployment_changed')
    domains = bound['domains']
    require(bound['status'] == 'bound' and bound['worker_code_changed'] is False and bound['plan_sha256'] == API_PLAN
            and len(domains) == 1 and domains[0]['hostname'] == 'api.photoposting.shop'
            and domains[0]['service'] == WORKER and domains[0]['zone_id'] == ZONE and domains[0]['enabled'] is True,
            'domain_binding_mismatch')
    require(access['saved'] is True and access['added_target'] == 'api.photoposting.shop/api/admin/v2/*'
            and access['owner_worker_check'] == 'unchanged', 'access_binding_mismatch')
    require(set(candidate['files']) == {'worker.bundle.mjs', 'wrangler.candidate.json', 'public-source-manifest.json'},
            'unexpected_disabled_candidate_files')
    artifacts = {}
    for name, expected in candidate['files'].items():
        raw = read(root, BASE + '/' + name)
        require(expected == {'sha256': sha(raw), 'bytes': len(raw)}, 'disabled_artifact_changed')
        artifacts[name] = raw
    require(sha(artifacts['worker.bundle.mjs']) == deployed['worker']['bundle_sha256'], 'deployed_bundle_mismatch')
    return docs, artifacts


def validate_config(config):
    require(set(config) == {'compatibility_date', 'main', 'name', 'no_bundle', 'observability', 'preview_urls',
                           'r2_buckets', 'ratelimits', 'vars', 'workers_dev'}, 'runtime_shape_mismatch')
    require(config['compatibility_date'] == '2026-09-04'
            and config['name'] == WORKER and config['main'] == 'worker.bundle.mjs' and config['no_bundle'] is True
            and config['workers_dev'] is True and config['preview_urls'] is False
            and config['observability'] == {'enabled': False}
            and config['r2_buckets'] == [{'binding': 'PRIVATE_MEDIA', 'bucket_name': 'image-prompt-archive-private-staging'}]
            and config['ratelimits'] == [{'name': 'OWNER_RATE_LIMITER', 'namespace_id': '26090402',
                                         'simple': {'limit': 20, 'period': 60}}], 'runtime_boundary_mismatch')
    values = config['vars']
    require(set(values) == {'ACCESS_JWT_REQUIRED', 'DAILY_QUERY_CALL_LIMIT', 'DAILY_QUERY_TOKEN_LIMIT',
                           'LIVE_QUERY_EMBEDDING_ENABLED', 'OWNER_EMAIL_ALLOWLIST', 'POLICY_AUD', 'PRIVATE_API_ENABLED',
                           'SNAPSHOT_ID', 'SNAPSHOT_MANIFEST_SHA256', 'TEAM_DOMAIN', 'TEXT_COLLECTION'}, 'runtime_variables_mismatch')
    require(values['ACCESS_JWT_REQUIRED'] == 'true' and values['PRIVATE_API_ENABLED'] == 'true'
            and values['LIVE_QUERY_EMBEDDING_ENABLED'] == 'false' and values['DAILY_QUERY_CALL_LIMIT'] == '20'
            and values['DAILY_QUERY_TOKEN_LIMIT'] == '40000'
            and json.loads(values['OWNER_EMAIL_ALLOWLIST']) == ['andrew4may@gmail.com']
            and values['TEAM_DOMAIN'] == 'https://travel-agency.cloudflareaccess.com'
            and values['POLICY_AUD'] == 'ce2ce161bc489c0c16f510706bf62ac48bb042a032752f1059ad06d2a0349ee8'
            and values['SNAPSHOT_ID'] == SNAPSHOT and values['SNAPSHOT_MANIFEST_SHA256'] == PLAN_HASH
            and values['TEXT_COLLECTION'] == 'image_archive_v2_' + SNAPSHOT + '_text512', 'runtime_approval_scope_mismatch')


def candidate(root=ROOT):
    docs, old = load_baseline(root)
    disabled = json.loads(old['wrangler.candidate.json'])
    validate_config(disabled)
    source_record = json.loads(old['public-source-manifest.json'])
    sources = {}
    for relative in WORKER_SOURCES:
        raw = read(root, relative)
        expected = source_record['files'].get(relative)
        require(expected == {'sha256': sha(raw), 'bytes': len(raw)}, 'worker_source_drift')
        sources[relative] = expected
    activated = json.loads(json.dumps(disabled))
    changes = {'LIVE_QUERY_EMBEDDING_ENABLED': 'true', 'DAILY_QUERY_CALL_LIMIT': str(CALL_LIMIT),
               'DAILY_QUERY_TOKEN_LIMIT': str(RESERVATION_LIMIT)}
    activated['vars'].update(changes)
    # Compare every other field, not merely the security fields we know today.
    reconstructed = json.loads(json.dumps(activated))
    for key in changes:
        reconstructed['vars'][key] = disabled['vars'][key]
    require(reconstructed == disabled and CALL_LIMIT == 200 and RESERVATION_LIMIT == 200000, 'activation_scope_expanded')
    files = {
        'worker.bundle.mjs': old['worker.bundle.mjs'],
        'wrangler.activation.json': encoded(activated),
        'wrangler.rollback.json': old['wrangler.candidate.json'],
        'worker-source-manifest.json': encoded({'schema_version': 'owner-query-source-1', 'files': sources,
                                              'bundle_matches_deployed_version': VERSION}),
        'secret-requirements.json': encoded({'required_additional_secret_names': ['VOYAGE_API_KEY'],
            'preserve_existing_secret_names': OLD_SECRET_NAMES, 'secret_values_included': False,
            'bulk_original_runtime_secret_upload_forbidden': True,
            'qdrant_read_only_evidence': PINNED_INPUTS['read_key_receipt'][1],
            'pre_deploy_remote_secret_names_and_key_scope_recheck_required': True}),
    }
    manifest = {'schema_version': 'owner-query-activation-candidate-1', 'state': 'review_pending',
        'eligible_for_release': False, 'deployment_performed': False, 'secret_upload_performed': False,
        'targets': {'worker': WORKER, 'cloudflare_account_id': ACCOUNT, 'api_origin': ORIGIN},
        'baseline_candidate_sha256': DISABLED_CANDIDATE, 'baseline_worker_version': VERSION,
        'snapshot_id': SNAPSHOT, 'snapshot_manifest_sha256': PLAN_HASH,
        'generator_sha256': sha(read(root, GENERATOR)),
        'pinned_evidence': {key: {'path': value[0], 'sha256': value[1]} for key, value in PINNED_INPUTS.items()},
        'files': {name: {'sha256': sha(raw), 'bytes': len(raw)} for name, raw in files.items()},
        'variable_changes': {key: {'before': disabled['vars'][key], 'after': value} for key, value in changes.items()},
        'query_budget': {'model': 'voyage-4-lite', 'dimension': 512, 'input_type': 'query',
            'daily_provider_call_limit': CALL_LIMIT, 'daily_reservation_unit_limit': RESERVATION_LIMIT,
            'reservation_formula': 'UTF8(query).byteLength + 256', 'window': 'UTC calendar day',
            'scope': 'global model across isolates and tokens', 'reservation_is_actual_billed_tokens': False,
            'actual_usage_source': 'Voyage response usage.total_tokens', 'provider_auto_retries': 0,
            'uncertain_calls_remain_reserved': True, 'cache': '10 minutes / 128 entries / isolate only'},
        'operational_db_writes_after_activation': ['api_daily_budget', 'api_query_receipts', 'api_model_guard'],
        'preserved': ['worker_bundle', 'authentication', 'owner_allowlist', 'read_only_qdrant_secret',
                      'scoped_api_tokens', 'neon_role', 'snapshot', 'image_and_text_vectors', 'custom_domains',
                      'access_paths', 'private_r2_binding', 'public_gallery'],
        'model_calls_performed': 0, 'record_reembedding': False, 'secret_values_included': False,
        'private_media_transfer_authorized_by_this_candidate': False,
        'public_image_release_authorized_by_this_candidate': False,
        'pending': ['exact_candidate_human_approval', 'current_remote_version_and_secret_scope_check',
                    'separate_voyage_secret_upload_and_deployment', 'separate_approved_live_query_canary'],
    }
    return manifest, files


def freeze(root=ROOT, *, prepare=False, apply=False, approved_candidate_sha256=None):
    require(not (prepare and apply), 'prepare_and_apply_are_exclusive')
    require(apply or approved_candidate_sha256 is None, 'approval_hash_requires_apply')
    manifest, files = candidate(root)
    raw = encoded(manifest)
    digest = sha(raw)
    relative = 'data/private-research/platform-v2/query-activation-candidates/' + digest
    directory = local_path(root, relative, private=True)
    if apply:
        require(isinstance(approved_candidate_sha256, str) and HEX.fullmatch(approved_candidate_sha256)
                and approved_candidate_sha256 == digest, 'exact_external_approval_hash_required')
    if prepare or apply:
        outputs = {**files, 'candidate.json': raw}
        # Validate all existing files first; never replace a conflicting artifact.
        if directory.exists():
            require(directory.is_dir() and {p.name for p in directory.iterdir()}.issubset(outputs), 'unexpected_candidate_files')
        for name, body in outputs.items():
            target = local_path(root, relative + '/' + name, private=True)
            if target.exists():
                require(target.is_file() and target.read_bytes() == body, 'immutable_candidate_conflict')
        directory.mkdir(parents=True, exist_ok=True)
        for name, body in outputs.items():
            target = local_path(root, relative + '/' + name, private=True)
            if not target.exists():
                with target.open('xb') as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
    status = 'frozen_locally_not_deployed' if apply else 'prepared_locally_pending_approval' if prepare else 'dry_run'
    return {'status': status, 'candidate_sha256': digest,
            'candidate_directory': relative, 'target': manifest['targets'], 'query_budget': manifest['query_budget'],
            'variable_changes': manifest['variable_changes'], 'required_additional_secret_names': ['VOYAGE_API_KEY'],
            'network_calls': 0, 'credential_reads': 0, 'new_model_calls': 0, 'secret_upload_performed': False,
            'deployment_performed': False, 'eligible_for_release': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--approved-candidate-sha256')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(freeze(prepare=args.prepare, apply=args.apply, approved_candidate_sha256=args.approved_candidate_sha256)))
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, ActivationError) else 'query_activation_preparation_failed'
        print(json.dumps({'status': 'failed', 'error_code': code, 'network_calls': 0, 'credential_reads': 0,
                          'new_model_calls': 0, 'deployment_performed': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
