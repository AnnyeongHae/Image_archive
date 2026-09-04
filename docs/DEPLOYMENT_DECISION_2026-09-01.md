# Deployment Decision 2026-09-01

Status: **proposed architecture / not deployed**. This document records the recommended
target and does not authorize Cloudflare, Neon, Vercel, GitHub, DNS, or public-release
changes. The detailed Korean report is
[`Reports/2026-09-01-02_이미지아카이브_배포아키텍처와_RAG_운영전략.md`](../../Reports/2026-09-01-02_이미지아카이브_배포아키텍처와_RAG_운영전략.md).

## Operational boundary analyzed

This decision covers the production delivery path for the image prompt archive platform:

- public read-only archive UI
- admin-only review, approval, source-master, and duplicate-review UI
- daily prompt collection and approval promotion
- image storage for original and derived assets
- future agent-facing REST API for retrieval and curation workflows

It does not authorize public release of rights-uncleared prompts or media.

## Confirmed local facts

- The canonical archive currently rebuilds from `data/canonical/archive_records.jsonl`.
- OpenNana approvals are applied into immutable private artifacts first, then folded into an internal archive lane.
- Public release eligibility remains fail-closed.
- The local review API contract already exists as `/api/review/v1/state`, `/preview`, and `/commit`.

## Assumptions

- This is a solo-operated system with low write concurrency and moderate read traffic.
- The public site is portfolio-first, not a high-frequency transactional product.
- Daily collection is the main automation path; bulk historical backfills are occasional maintenance jobs.
- Korea-facing latency matters more than multi-region write throughput.

## Decision

Choose a Cloudflare-first topology and avoid mixing Vercel into the serving path.

Recommended production shape:

1. Frontend: Cloudflare Workers Static Assets serving the built archive UI
2. Backend API: Cloudflare Workers for public read APIs and admin review APIs
3. Object storage: Cloudflare R2
4. Primary metadata database: Neon Postgres
5. Daily automation: GitHub Actions at first, with a clean upgrade path to Cloudflare scheduled Workers or Workflows later
6. Admin protection: Cloudflare Access in front of admin routes and admin asset paths

If you want the closest option to your original Option A, use:

- `R2 + Workers + Neon + GitHub Actions`

and treat Pages as optional legacy hosting rather than the strategic target for a new build.

## Why this is the smallest safe choice

### A. Cloudflare-first is operationally cleaner than mixing Vercel

Using `Cloudflare Pages + Vercel + R2 + Neon + GitHub Actions` creates two separate edge/runtime control planes:

- separate deployment surfaces
- separate logs and incident debugging paths
- separate secret and auth boundaries
- extra routing decisions between frontend and API

That added complexity does not buy a clear advantage for this workload. Your archive is storage-heavy, cache-heavy, and admin-gated. Those characteristics align better with Cloudflare as the primary serving layer.

### B. Workers Static Assets is the current Cloudflare-preferred static path

For new projects, Cloudflare now recommends Workers Static Assets over Pages for static and full-stack deployments. That means the cleanest target is one Worker project that serves:

- static built UI
- `/api/public/*`
- `/api/admin/*`

This removes the Pages-vs-Workers split brain while preserving the same CDN edge.

### C. Keep Neon for metadata, not D1, for this product phase

Neon is the better fit for your stated direction because you want:

- relational metadata
- future RAG-oriented retrieval support
- strong SQL ergonomics
- branching for safe schema or pipeline changes
- a later path to `pgvector` or similar extensions if you revisit embedding retrieval

Cloudflare explicitly documents Neon integration for Workers and recommends either Hyperdrive or the Neon serverless driver. For this platform, keep Neon as the system of record and do not split core metadata across Neon and D1.

### D. GitHub Actions is acceptable for daily collection, but not ideal forever

GitHub Actions is a good first scheduler because:

- the collection pipeline already lives in the repository
- daily cadence is low frequency
- failures are easy to inspect from run logs
- repo changes and collector changes stay in one change history

Its weaknesses are operational:

- schedules run on the default branch
- scheduled runs can be delayed under load
- public repos can have scheduled workflows disabled after inactivity

So Actions is fine for daily sync now, but the runtime should stay idempotent so you can later move the trigger to Cloudflare scheduled Workers or Workflows without changing the business logic.

## Exact service boundary

### Public plane

- Worker static assets serve archive HTML, CSS, JS
- Worker public API serves filtered metadata only
- R2 custom domain serves approved public derivatives only
- CDN cache fronts both public asset classes

### Admin plane

- Cloudflare Access gates `/admin/*` and `/api/admin/*`
- Worker admin API handles source refresh state, approval preview/commit, queue reads, and duplicate review data
- Neon stores durable metadata, review state, dedupe groups, and source-run receipts
- private originals remain outside public routes

### Batch plane

- GitHub Actions runs daily source sync
- batch writes to Neon and private R2 keys
- derivative conversion writes WebP plus fallback JPEG/PNG variants
- approval promotion is idempotent and append-only

## Data model recommendation

### R2 buckets or prefixes

- `originals/`: source or generated originals, private by default
- `derived/web/`: public-facing WebP derivatives
- `derived/fallback/`: JPEG or PNG fallback derivatives
- `receipts/`: optional import receipts, manifests, or transformation logs

Do not store base64 in Postgres. Store object keys, dimensions, MIME, byte size, checksum, and lineage only.

### Neon core tables

- `sources`
- `source_runs`
- `source_items`
- `prompts`
- `assets`
- `asset_variants`
- `review_queue`
- `review_decisions`
- `archive_records`
- `duplicate_groups`
- `duplicate_edges`
- `tag_candidates`
- `tag_overrides`

### Metadata tagging strategy

The immediate problem is not "perfect AI tagging". The immediate problem is durable structure. Use three layers:

1. Deterministic metadata
   - source
   - fetched_at
   - upstream_id
   - source_url
   - prompt text
   - model if reported
   - dimensions
   - MIME
   - checksums
   - exact duplicate keys
2. Machine-enriched metadata
   - category candidates
   - visual subject candidates
   - style candidates
   - prompt strategy candidates
   - safety flags
   - confidence score
   - model provenance
3. Human override metadata
   - approved tags
   - curated group membership
   - portfolio visibility
   - rights review outcome

This gives you searchable metadata now without pretending the first AI pass is ground truth.

## Search and RAG recommendation

Current best-fit recommendation for this product is not "full multimodal RAG first".

Phase 1:

- prompt text normalization
- Postgres full-text search
- structured filters
- curated groups and source filters
- exact and near-duplicate grouping

Phase 2:

- optional prompt embeddings for recall expansion
- optional image embeddings for visual-neighbor discovery
- reranking only on narrowed candidate sets

Reason: your stated goal is to retrieve the most useful reference from your viewpoint. That goal depends heavily on curation, grouping, and provenance, not just vector similarity.

## Duplicate handling

Use two separate paths:

1. Exact duplicate removal
   - normalized prompt hash
   - image checksum
   - if both match, keep one canonical record and preserve lineage references
2. Near/remix grouping
   - prompt similarity score
   - perceptual image hash distance
   - shared source family
   - shared template variables

Near duplicates should not be auto-deleted. They should be grouped as variants or remixes.

## Image delivery recommendation

Use:

- `WebP` as the default public preview format
- `JPEG` or `PNG` fallback only when needed

Rationale:

- `AVIF` can be smaller, but your stated concern about compatibility is valid
- `WebP` keeps the platform simpler
- public archive pages benefit more from predictable support and easier caching than from squeezing every last byte

Generate multiple derivative sizes and store them as separate R2 objects. Keep originals private.

## Failure and recovery

### Normal path validated locally

- approval promotion rebuilds the canonical archive and public inventory after durable decision artifacts exist

### Failure path validated locally

- the review API test suite includes rollback when promotion fails
- it also validates stale revision rejection and idempotent commit retry

### Recovery expectation in production

- promotion must remain idempotent by decision batch ID
- public asset keys should be content-addressed where possible
- replacing or deleting cached public objects requires explicit cache purge handling

## Risks and trade-offs accepted

- GitHub Actions is not the most reliable scheduler under load, but it is the smallest acceptable starting point
- Neon adds an external dependency outside Cloudflare, but keeps SQL capability and future retrieval flexibility
- Workers-only serving means you should design around Worker runtime constraints instead of long-running request handlers

## What still requires live environment verification

- real Korean user latency with Neon region selection
- whether Hyperdrive gives a meaningful latency reduction for your actual query patterns
- Cloudflare Access policy shape for admin-only UI and API routes
- R2 derivative cache behavior under your final custom domain and cache rules
- end-to-end ingest duration for the real daily source set

## Recommended rollout

1. Keep local JSON/JSONL as the migration source of truth
2. Define Neon schema and import the current canonical archive plus approval history
3. Move public frontend hosting target to Workers Static Assets
4. Reimplement the existing local review API contract on Workers
5. Keep daily sync on GitHub Actions first
6. Move daily trigger to Cloudflare scheduled Workers or Workflows only after the pipeline is idempotent and observable
7. Add machine-enriched metadata as an asynchronous post-ingest step, not a blocking ingest step

## Bottom line

Between your two original options, choose the Cloudflare-centered one.

But the stronger version is:

- `Cloudflare Workers Static Assets + Workers API + R2 + Neon + GitHub Actions`

not

- `Cloudflare Pages + Vercel + R2 + Neon + GitHub Actions`

The Vercel mix adds more operational surface than value for this archive platform.
