# Image Gallery v2 · local implementation contract

Status: ready. Execution gate: allowed for offline implementation and loopback preview, following the user's 2026-09-04 “프론트 v2 진행하자”. Deployment, DNS changes and public promotion are not included.

## Problem / goal

The old public 529-record gallery does not reflect the reviewed v2 groups. A visitor needs different design options, not several cards for the same format. Keep one human-chosen representative per group, expose variants on demand, and make the exact original prompt easy to copy.

## Scope and acceptance criteria

- One top-level card per confirmed group; a member-only search match still returns that group's human-chosen representative.
- Combined search/facets must be satisfied by at least one member, not assembled from incompatible attributes across different members. The returned card remains the group's representative.
- Variants are initially collapsed and can be opened individually. No inferred grouping by usage tags or identical prompt alone.
- Prompt-copy is visible on every card, copies the exact selected member's original prompt, and has an honest error/manual fallback.
- The exact original string must be passed to the clipboard API. OS-level LF/CRLF normalization is reported separately, not misrepresented as byte-identical clipboard storage.
- Eight controlled reuse-purpose categories plus an explicit unclassified state; keyword search (not a claim of semantic/RAG search); reset, loading, empty and retry states.
- Granular usage/style/background dropdowns include only values shared by at least **two distinct confirmed groups**. Repeated members in one group count once. Single-group values remain in details and keyword search; no record or metadata is deleted to shorten dropdowns.
- Category/facet option counts are distinct group counts, not raw image counts. Combined category, search and granular facets must match the same member; the group's representative remains unchanged.
- Native dialog/details/buttons provide keyboard interaction; mobile width has no page-level overflow; dialog closes with Escape and restores focus.
- Initial catalog excludes full prompts. Group detail and images load on demand; local WebP derivatives never replace originals.
- Public projection fails closed on missing rights/release gates. A private local preview is separately labeled and never a deployable public approval.
- No private memos, raw source records, local paths, vectors, API credentials or provider calls in the browser projection.
- Focused unit and actual browser checks must pass before local acceptance. No external issue/PR/push/deploy/DNS or new package installation is required by this task.

## Dependencies / boundaries

Existing pinned private snapshot, verified prepared image cache, existing Pillow and installed Chromium/Playwright. Keep legacy/public v1 and all canonical/analysis sources unchanged. Private preview runs on loopback only with an explicit served-file allowlist; it is not a public or LAN server.

## Catalog: `data/catalog.json`

```text
schema_version: image-gallery-2
mode: private_local_preview | public
counts: {images, groups, variants, excluded}
browse_taxonomy_version: 1
browse_categories[]: {id, label}  # eight purpose IDs and unclassified; no new model-made IDs
groups[]:
  id, representative_id, member_count
  representative: {id, style_id, title, thumbnail, usage[], style[], background[], category_ids[], categories[], category_source, source?, rights?}
  members[]: {id, style_id, title, usage[], style[], background[], keywords[], category_ids[], categories[], category_source}
  detail_path: data/groups/<safe hash>.json
```

## Detail shard

```text
id, representative_id
members[]:
  id, style_id, title
  thumbnail: {webp, src, width, height}
  image: {src, width, height}
  original_prompt
  usage[], style[], background[], keywords[], usage_notes[]
  category_ids[], categories[]
  category_source: legacy_use_case_mapping | unclassified
  metadata_status: candidate | human_verified | none
  source: {name, url}
  rights: {badge, notice, attribution, license}
```

Paths are generated relative site paths, not filesystem paths. URLs are sanitized public source links. All displayed input is treated as data, never executable HTML. Candidate Luna labels are not human-confirmed facts or rights permission.

The versioned [browse category contract](../contracts/browse-categories.v1.json) maps only known exact legacy task IDs to broad purpose categories. Free-text-only/no-task legacy results are unclassified without heuristic inference; strict unknown task-ID errors remain failures. Existing multi-category mappings are preserved. Future Luna selection is primary one/secondary at most one with evidence or an explicit abstention, not a retroactive rewrite of current results. See [category rationale and future instructions](../../../docs/IMAGE_ARCHIVE_BROWSE_CATEGORIES.md).

## Build / preview

Builder: `platform/v2/local/frontend_projection.py`, dry-run by default. Generated bundles and their receipts stay under ignored `data/private-research/platform-v2/frontend-v2/` and are immutable. A receipt's `served_files` maps relative resource path to SHA-256. The receipt is not itself served. A public build must not copy private memos or bypass the existing rights policy. A private preview never changes a record's approval.

The receipt's `identity.browse_taxonomy_sources` binds the browse contract hash. `browse_category_coverage` counts input images per category (multi-label totals may exceed image count), unlike the UI's distinct-group option counts. Category projection does not call models, rewrite the database or embedding inputs, or change human visual groups and representatives.

Server: `platform/v2/local/frontend_preview.py`; loopback-only, read-only, no directory listing, no upstream proxy, no arbitrary repository file serving. No credential/model/DB API is required by gallery interaction.

## Verification status

Implementation and fresh verification evidence are recorded in `docs/IMAGE_ARCHIVE_FRONTEND_V2_IMPLEMENTATION.md`. This contract records requirements, not a claim that they already passed.

## Separate public release adapter (2026-09-04 follow-up)

`public_frontend_release.py` now constructs a fresh reference-display projection from an item/hash-bound grant. It does not relax this private-preview builder. Ignored pending candidates live under `data/private-research/v2/p/<candidate>/assets/`; grant and candidate receipts remain outside the served directory. The shorter output path avoids Windows MAX_PATH failures.

`public_deployment.py` freezes the public-only Worker, exact assets and existing `photoposting.shop` target. Source rights remain unverified; reference-display approval is not commercial-use clearance. A separate human release decision must bind the candidate/deployment hashes before actual deployment. `verify_public_live.py` and `qa/smoke_frontend_v2.mjs --public` verify the real deployed version, not merely localhost. See `docs/IMAGE_ARCHIVE_V2_SERVICE_COMPLETION.md` for the current evidence/status.
