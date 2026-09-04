# Refactor Status

Observed on `2026-08-31`.

## What is already refactored

- The image archive now has a single platform root at `08_AGENT_이미지_아카이브/`.
- The previous archive workspace was moved into `legacy/current_archive/`.
- Compatibility junctions keep older `Reports/...` and `runtime/...` file paths alive where needed.
- New platform-owned layers already exist: `app/`, `docs/`, `deploy/`, `media/`, `qa/`, `src/`, and `dist/`.
- All `18,792` display records are now normalized into `data/canonical/archive_records.jsonl` with lane-scoped keys, lineage, prompt, source, license, rights, media, taxonomy, generation, review, and search fields.
- `data/public-export/` now contains a fail-closed metadata index and 38 static shards. Rights-uncleared prompt text and media are omitted.

This means path consolidation and the canonical record export are done.

## What is not fully refactored yet

The legacy browser has not cut over to the canonical projection yet.

- The browser renders `18,792` logical cards assembled from several record lanes, but those are not `18,792` physical files.
- Most external records still live inside large container artifacts such as:
  - `legacy/current_archive/full_prompt_library.sqlite3`
  - `legacy/current_archive/external_prompt_records.json`
  - `legacy/current_archive/external_prompt_records.jsonl`
  - `legacy/current_archive/external-catalog-data.js`
- Legacy case images and generated previews live in image trees under `legacy/current_archive/assets/`.
- Immutable source evidence remains outside the platform root by design and is registered by pointer in `data/private-research/source_locations.json`.

The legacy containers are now immutable lineage inputs rather than the intended long-term source of truth. The remaining refactor is front-end cutover, item-level rights review, and an approved R2/public release path.

## Correct interpretation of counts

Use two different counts:

- Physical file count: how many files exist under the platform root.
- Logical record count: how many archive cards the browser can render.

The confusion came from comparing these two unlike numbers.

Expected current split:

- Platform root physical files: calculated on every inventory run rather than treated as a fixed record count
- Legacy detail-page cases: `529`
- External records: `18,092`
- Secret codes: `131`
- Manual records raw: `17`
- Manual records displayed in UI: `12`
- Social records: `3`
- BUL-001 template cards displayed in UI: `25`
- Generated preview asset manifests: `329`

Displayed catalog cards: `18,792`

The generated preview asset manifests are not extra cards by themselves. They attach preview files and generation metadata onto existing records.

The archive UI can show slightly different collection totals after presentation-time collapsing, deduplication, or browser cache. That does not change the storage fact: the missing `17k+` are mostly inside large JSON, JSONL, and SQLite containers, not lost.

## Should everything be refactored?

Yes. The full record set is now refactored into one canonical JSONL, not one file per record.

Splitting `18k+` records into `18k+` tiny files would hurt Git operations, Cloudflare deployment shape, and browser hydration. The better boundary is:

- Raw immutable sources stay where provenance requires them:
  - `Reference/`
  - `Reference/_derived/`
  - selected historical `runtime/` experiments
- Platform-owned canonical data now uses:
  - one internal normalized JSONL with 18,792 rows
  - one rights-filtered static export with 38 JSON shards
  - media metadata prepared for a later approved R2 key manifest

## Recommended target shape

- `data/raw/`: imported snapshots and untouched source projections
- `data/canonical/archive_records.jsonl`: one normalized row per logical record
- `data/canonical/archive.db`: optional operational SQLite for QA, dedupe, and admin queries
- `data/public-export/shards/*.json`: static client payloads for Cloudflare Pages
- `data/public-export/media_manifest.json`: preview path, hash, width, height, and future R2 key
- `media/public/`: only release-cleared previews
- `media/private-research/`: internal source mirrors and research-only media that are excluded from public export

## Remaining safe migration path

1. Keep `legacy/current_archive/` immutable except for bounded compatibility fixes.
2. Move the front-end to read the canonical export, not the monolithic legacy JS blobs.
3. Record item-level prompt/media clearance separately from repository license metadata.
4. Only after human release approval, copy cleared media into public deploy assets or R2.

## Operational tool

Run this to regenerate the evidence snapshot:

```powershell
python src/build_archive_inventory.py
python src/build_archive_inventory.py --apply
python src/build_canonical_archive.py
python src/build_canonical_archive.py --apply
python qa/validate_canonical_archive.py
```

The generated source of truth is `data/canonical/archive_inventory.json`. The current observed asset-state split is `1,029 local`, `7,937 remote URL`, `0 broken`, and `9,826 without preview`. A remote URL is a declaration only; it is not evidence of availability or reuse permission.
