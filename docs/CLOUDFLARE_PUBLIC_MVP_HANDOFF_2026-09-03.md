# Cloudflare public MVP handoff — 2026-09-03

## Outcome

Deployed a separate public archive frontend:

- public URL:
  `https://image-prompt-archive-public-staging.andrew4may.workers.dev/`
- Cloudflare Worker version:
  `b426a4d9-3bde-4dc6-a47b-c58da630e883`

- bundle root:
  `08_AGENT_이미지_아카이브/deploy/cloudflare-public/`
- public assets:
  `08_AGENT_이미지_아카이브/deploy/cloudflare-public/public/`
- worker config:
  `08_AGENT_이미지_아카이브/deploy/cloudflare-public/wrangler.jsonc`
- worker entry:
  `08_AGENT_이미지_아카이브/deploy/cloudflare-public/worker/index.js`

This is the actual archive frontend MVP, not the previous Access-protected admin
shell.

## Included public scope

- `catalog-data.js` snapshot of `awesome-gpt-image-2`
- `529` case records
- `532` local preview images
- `dashboard.js`, `dashboard.css`, `favicon.svg`, and a public-only `index.html`

Excluded:

- `external-catalog-data.js` (`106 MiB`)
- admin pages
- approval queue
- duplicate review
- OpenNana internal lane
- manual/private collections

## Why the legacy folder was not deployable as-is

Confirmed local legacy archive size:

- total files: `1416`
- total bytes: `2,204,597,993` (~`2102.47 MiB`)
- largest JS data file:
  `legacy/current_archive/external-catalog-data.js` = `106,375,386` bytes

Cloudflare Workers static asset limits observed on 2026-09-03:

- static asset files per Worker version:
  `20,000` free / `100,000` paid
- individual static asset file size:
  `25 MiB`

So the full legacy archive cannot be published directly with the current asset
layout because one required data file exceeds the per-file limit.

## Public bundle verification

Local public bundle:

- files: `538`
- total size: `147.73 MiB`
- largest file: `5.18 MiB`

Local HTTP smoke:

- `GET /` returned `200 OK`
- `GET /catalog-data.js` returned `200 OK`

Wrangler dry-run:

- read `541` assets from the public directory
- Worker script upload size:
  `3.27 KiB / gzip: 1.22 KiB`

## Live verification

- worker name: `image-prompt-archive-public-staging`
- lane: `public-awesome-gpt-image-2-mvp`
- `/`: `200`
- `/healthz`: `200`, 529 public records, no private/admin data
- `/api/public/v1/summary`: `200`, 529 public records
- `/approval-requests.html`: `404`
- `/source-admin.html`: `404`
- `/duplicate-review.html`: `404`
- `/admin/`: `302` to the Access-protected administrator Worker
- browser smoke: 50 initial cards, headline `529개 스타일 일치`, zero broken loaded images, zero console errors
- keyboard focus recovery: empty-result reset returns focus to the search field;
  deployed `dashboard.js` hash matches the locally verified asset

## Operational boundary

- Existing private staging admin Worker remains unchanged.
- Public MVP Worker is a separate surface.
- `/admin*` redirects to the Access-protected administrator Worker.
- `/approval-requests*`, `/source-admin*`, and `/duplicate-review*` return
  `404` in the public Worker.
- The public bundle carries source, MIT notice, upstream disclaimer, and a
  non-commercial/reference-only boundary. Repository-level licensing is not
  presented as item-level commercial clearance.
