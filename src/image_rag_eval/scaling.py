"""Offline, challenge-enriched 50 -> 200 preparation; immutable parent prefix."""
from __future__ import annotations

import copy
import itertools
from collections import Counter, defaultdict
from pathlib import Path

from . import dataset
from .comparison import MODELS, load_inputs, requests_for
from .experiment import annotations_template, digest, json_bytes, now, read_json, run_lock, run_path, write_json
from .expansion import _prepare_item, _resolve_additional_items, _select_additional_candidates
from .similarity import build_groups, compare_pair

TARGET_ITEMS = 200
PARENT_ITEMS = 50


def challenge_candidates(root: Path, parent_items: list[dict]) -> list[dict]:
    """Deterministic enrichment, not representative corpus prevalence sampling."""
    connection = dataset._connect_index(dataset._duplicate_index_path(root))
    ordered, seen = [], {str(item['id']) for item in parent_items}
    parent_ids = set(seen)
    try:
        for kind, quota in (("perceptual_candidate", 90), ("same_prompt_variant", 75),
                            ("exact_prompt", 30), ("exact_media", 10)):
            quota = min(quota, max(0, 120 - len(ordered)))
            grouped = defaultdict(list)
            for row in dataset._group_candidates(connection, kind):
                grouped[row['group_id']].append(row)
            group_ids = sorted(grouped, key=lambda gid: (
                not any(str(row.get('asset_id')) in parent_ids for row in grouped[gid]), gid))
            added = 0
            for gid in group_ids:
                if added >= quota:
                    break
                rows = sorted(grouped[gid], key=lambda row: (int(row.get('ordinal') or 0), str(row.get('asset_id'))))
                # Limit domination by giant clusters. In visual/prompt groups prefer different files.
                group_hashes = set()
                group_added = 0
                for row in rows:
                    sha = str(row.get('asset_sha256') or '')
                    if kind != 'exact_media' and sha in group_hashes:
                        continue
                    if str(row.get('asset_id')) in seen:
                        group_hashes.add(sha)
                        continue
                    if dataset._append_candidate(ordered, seen, row, group_seed_kind=kind):
                        ordered[-1]['challenge_seed_group_id'] = gid
                        group_hashes.add(sha)
                        group_added += 1
                        added += 1
                    if group_added >= 4 or added >= quota:
                        break
    finally:
        connection.close()
    # A generous local-only fallback pool handles missing/stale source images.
    ordered.extend(_select_additional_candidates(root, seen, TARGET_ITEMS - PARENT_ITEMS))
    return ordered


def build_scaled_manifest(root: Path, source_run_id: str, *, progress=None) -> tuple[dict, dict[str, bytes], dict]:
    root = dataset._normalized_root(Path(root))
    parent, image_bytes, _ = load_inputs(root, source_run_id, maximum_items=50)
    if len(parent['items']) != PARENT_ITEMS:
        raise ValueError('200-record expansion requires exactly 50 parent records')
    if progress:
        progress({'stage': 'parent_validated', 'items': len(parent['items'])})
    inputs = {item['prepared_path']: image_bytes[item['id']] for item in parent['items']}
    candidates = challenge_candidates(root, parent['items'])
    if progress:
        progress({'stage': 'candidate_pool_selected', 'candidates': len(candidates)})
    by_id = {row['asset_id']: row for row in candidates}
    additions = _resolve_additional_items(root, candidates, TARGET_ITEMS - PARENT_ITEMS)
    if progress:
        progress({'stage': 'local_sources_resolved', 'items': len(additions)})
    prepared_additions = []
    for item in additions:
        prepared, blob = _prepare_item(root, item)
        prepared['challenge_seed_group_id'] = by_id[item['id']].get('challenge_seed_group_id')
        if prepared['prepared_path'] in inputs and inputs[prepared['prepared_path']] != blob:
            raise ValueError('prepared image digest collision')
        inputs[prepared['prepared_path']] = blob
        prepared_additions.append(prepared)
        if progress and len(prepared_additions) % 25 == 0:
            progress({'stage': 'new_image_signals_prepared', 'items': len(prepared_additions)})
    manifest = {
        'schema_version': '1', 'created_at': now(),
        'selection_notes': [f'Immutable first 50 from {source_run_id}.',
            'Additional 150: perceptual and prompt-scaffold challenge seeds, exact controls, at least 30 source/lane diversity candidates.',
            'Enriched sample; not an unbiased estimate of corpus similarity prevalence.',
            'All similarity labels await actual human review.'],
        'items': copy.deepcopy(parent['items']) + prepared_additions,
        'preprocessing': parent.get('preprocessing'),
        'evaluation_arms': ['voyage_image'],
        'selection_profile': {'provider': 'voyage', 'model': MODELS['voyage'],
            'evaluation_arms': ['voyage_image'], 'gemini': 'paused_by_user'},
        'experiment': {'preparation_only': True, 'max_images': TARGET_ITEMS,
            'max_inference_calls': 0, 'human_verified': False, 'metadata_generation': 'not_executed'},
    }
    meta = {'source_run_id': source_run_id, 'source_manifest_sha256': digest(json_bytes(parent)),
        'preserved_item_count': PARENT_ITEMS, 'additional_item_count': len(prepared_additions),
        'preserved_subset_validated': manifest['items'][:PARENT_ITEMS] == parent['items'],
        'selection_counts': dict(Counter(item.get('group_seed_kind') or 'diversity' for item in prepared_additions))}
    return manifest, inputs, meta


def incremental_plan(root: Path, manifest: dict, source_run_id: str, inputs: dict[str, bytes]) -> dict:
    from io import BytesIO
    from PIL import Image
    source = run_path(root, source_run_id)
    queries = read_json(source / 'comparison-v1/queries.json')
    ledger = read_json(source / 'comparison-v1/budget.json')
    pixels = {}
    for item in manifest['items']:
        with Image.open(BytesIO(inputs[item['prepared_path']])) as image:
            pixels[item['id']] = max(50_000, image.width * image.height)
    requests = {r['key']: r for r in requests_for(manifest, pixels, queries, arms_subset=['voyage_image'])}
    # Identity/completed-state validation is repeated by carryover before inference.
    cached = {p.stem for p in (source / 'comparison-v1/vector-cache').glob('*.json')}
    additional = [r for key, r in requests.items() if key not in cached]
    prior = sum(a['reserved_usd'] for a in ledger['attempts'])
    increment = sum(r['reserved_usd'] for r in additional)
    return {'provider': 'voyage', 'model': MODELS['voyage'], 'unique_selected_requests': len(requests),
        'reusable_selected_cache_keys': len(cached & set(requests)), 'new_image_requests': len(additional),
        'prior_attempts': len(ledger['attempts']), 'prior_reserved_usd': prior,
        'incremental_reserved_usd': round(increment, 10), 'total_reserved_usd': round(prior + increment, 10),
        'maximum_usd': .10, 'within_existing_cap': prior + increment <= .10,
        'actual_invoice_usd': None, 'free_balance_verified': False,
        'price_source': 'https://docs.voyageai.com/docs/pricing',
        'price_basis': 'Paid-price reservation including safety text allowance; does not assume free credits'}


def prepare200(root: Path, source_run_id: str, run_id: str, *, apply: bool = False, progress=None) -> dict:
    root = Path(root).resolve()
    destination = run_path(root, run_id)
    if source_run_id == run_id:
        raise ValueError('new run id must differ from parent')
    if apply and destination.exists():
        raise FileExistsError('destination exists; never overwrite a prepared run')
    manifest, inputs, meta = build_scaled_manifest(root, source_run_id, progress=progress)
    plan = incremental_plan(root, manifest, source_run_id, inputs)
    result = {'status': 'dry_run', 'network_calls': 0, 'writes': 0, 'run_id': run_id,
        'items': len(manifest['items']), 'pairs': 19900, **meta, 'budget_plan': plan}
    if not apply:
        return result
    if not plan['within_existing_cap']:
        raise ValueError('incremental reservation exceeds existing US$0.10 cap')
    # A valid preparation receipt is written last, after all local evidence.
    pairs = [compare_pair(a, b) for a, b in itertools.combinations(manifest['items'], 2)]
    with run_lock(destination.parent):
        if destination.exists():
            raise FileExistsError('destination exists; never overwrite a prepared run')
        destination.mkdir()
        (destination / 'inputs').mkdir()
        for relative, blob in inputs.items():
            target = (destination / relative).resolve()
            if not target.is_relative_to((destination / 'inputs').resolve()):
                raise ValueError('prepared path escapes inputs')
            target.write_bytes(blob)
        for name, payload in [('manifest', manifest), ('annotations.template', annotations_template(manifest)),
            ('expansion-budget-plan', plan), ('offline', {'status': 'offline_only', 'pairs': pairs,
                'groups': build_groups(manifest['items'], pairs), 'embedding_calls': 0, 'human_verified': False})]:
            write_json(destination / f'{name}.json', payload)
        write_json(destination / 'prepared.json', {'complete': True, 'at': now(),
            'manifest_sha256': digest(json_bytes(manifest)), **meta, 'pair_count': len(pairs)})
    return {**result, 'status': 'prepared_local_only', 'writes': 1}
