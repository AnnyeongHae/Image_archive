# Image administrator deployment boundary — 2026-09-03

Status: local implementation verified; cloud deployment and new embedding calls not executed. This document supersedes neither existing human approvals nor the historical private/public canary receipts.

## Decision and scope

Keep the existing single-user four-stage UI and Python batch processing. Separate pure committed-library projection from its local HTTP/SQLite adapter. Do not expose the Python development HTTP server to the internet. The existing Workers/R2/Neon intention remains; this change does not create a database, change credentials or replace the approved storage plan.

| Responsibility | Current implementation | Cloud readiness |
|---|---|---|
| Original images, exact hashes, embedding cache, new candidate calculation | Local batch tools | Never run in a page request |
| Human decisions and optional inspiration memos | Local SQLite transactional commit | Durable cloud write adapter still required |
| Grouped private gallery | Pure `approved_library.py`, group-first browser view | Portable versioned data, human membership unchanged |
| Original prompt | Hash-bound source resolver, per-ID authenticated local endpoint | Full prompt in private bundle; not public static assets |
| Rights/source | Conservative per-image notice | Private access is not public/commercial permission |
| Private cloud read | Disabled Workers adapter for a pinned R2 object | Offline tests only, no R2 upload/activation |
| Automation | Offline GitHub Actions boundary checks | Workflow file prepared; no remote run/deploy claim |

## Private artifact

`src/prepare_image_private_bundle.py --run-id RUN` is dry-run. `--apply` prepares an immutable content-addressed folder under ignored `data/private-research/image-rag-admin/deployment-bundles/`. `--expected-commit-id SHA` rejects stale approval. It reads a consistent committed SQLite snapshot and revalidates the frozen decisions; mutable drafts are excluded.

- `library.json`: `image-private-library-bundle-1`, only approved images, exact human groups, original prompts, memos, source/rights, content-addressed preview keys. All release flags false. No vectors, SQLite, operational filesystem paths or credential configuration.
- `media-plan.local.json`: separate private relative source path + preview SHA/size plan. It is not a public artifact and `upload_authorized=false`.
- `receipt.json`: bundle and media-plan hashes, originating commit, proposed immutable R2 key. `deploy_enabled=false`.

Initial measured committed bundle: 379 approved images; 26 groups / 79 grouped / 300 ungrouped. JSON 1,409,393 bytes. Preview upload plan: 379 existing PNG derivatives, 230,542,971 bytes; no duplicate copies or actual uploads were made. This is not the final delivery encoding. Before a private media canary, generate separately hashed WebP derivatives and measure quality/size; never replace sources or reuse a PNG hash for converted bytes.

Shared machine contract: `../00_CORE/schemas/image_private_library_bundle.schema.json`, including the existing image rights notice contract. Actual 379-item output was validated offline against both. Runtime export/Worker adapters keep explicit invariants without installing a new dependency.

The 8 MiB / 2,000-item adapter cap is deliberately a bounded canary contract, not a scaling claim. Larger libraries need grouped pagination/shards and per-image lazy prompt fetching; do not deliver every full prompt on every page. Local UI already uses per-ID prompt requests and group-preserving pagination. An initial Worker cold read loads one bounded pinned object; repeated reads within the same isolate reuse that immutable object after authentication. Isolate cache is an optimization, not persistent storage or a usage guarantee.

## Disabled Workers adapter

The existing `deploy/cloudflare-staging/worker/index.js` retains Cloudflare Access JWT verification before all routes. New `/api/admin/v1/library` is GET/HEAD only and additionally requires `ADMIN_ENABLED=true`, `PRIVATE_LIBRARY_ENABLED=true`, an exact `PRIVATE_LIBRARY_SHA256` and the existing private R2 binding. The key is derived as `private-library/snapshots/{SHA}.json`; users cannot request arbitrary R2 keys.

The adapter bounds stream size, verifies SHA-256 and checks private schema/approved group membership before returning data. Errors expose no private object contents. Responses use no-store and no public CORS. No media-serving route, approval mutations, inference, arbitrary storage writes or public release are enabled. `wrangler.jsonc` was not changed to enable this feature. There is no complete remotely deployed four-stage administrator yet.

Cloudflare's documented [Worker-first asset routing](https://developers.cloudflare.com/workers/static-assets/routing/worker-script/) keeps authentication in front of static assets. R2 binding behavior follows the [Workers R2 API](https://developers.cloudflare.com/r2/api/workers/workers-api-reference/). Keep review data outside the [public static-asset directory](https://developers.cloudflare.com/workers/static-assets/binding/). These docs support the integration contract, not a claim of a successful remote deployment.

## Remaining CASE review

529 scoped CASE primary records minus 374 already reviewed = 155 remaining. All 155 local images are prepared in `2026-09-03-case-final-155-v1/remaining-review-v1/`. Exact file OR (full decoded pixels + nonempty exact prompt) found zero automatic alias proposals against all prior records and within the new set. Same prompt alone never deletes an image. Hash indexes cover 77,500 existing/new + 11,935 new/new pair combinations; these counts are not embedding API requests or pairwise network calls.

No cached image vectors exist for these 155 keys. Their semantic duplicate/group review is incomplete, not “no similar images.” Existing 381 retained vectors can be reused; only these new 155 need image embeddings. New provider calls require approval because free quota cannot be verified locally. The prepared HTML is a read-only status aid, not another JSON approval/upload workflow. After authorized vectors, build a new frozen review with a current committed-DB baseline adapter, then use the server's existing four-stage reviewer. Do not pass this package into the old 200-record baseline executor unchanged.

## Activation gates and rollback

Before cloud activation: explicit user approval of private canary scope, authenticated access tests, content-hash checked private R2 uploads, private image delivery, and a durable transactional approval adapter preserving revisions/idempotency/CSRF where applicable. No publicly shared raw image/prompt/media until separate per-item rights and release gates pass. Existing private access-shell or public canary is a different lane.

To roll back local code, restore only this change's code version after stopping its validated process; preserve the approval DB. A SQLite backup was taken before restart. Export preparation is additive and idempotent: old artifacts remain immutable, and no new bundle is “latest” merely because it exists. Disable a future cloud reader by clearing its explicit feature flag; keep old immutable object versions instead of overwriting shared keys.
