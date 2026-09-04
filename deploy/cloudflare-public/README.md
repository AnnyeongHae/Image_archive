# Public Cloudflare MVP

This directory contains the public MVP deployment bundle for the image prompt
archive.

## Scope

- Public read-only archive only
- Source: `awesome-gpt-image-2` local snapshot (`catalog-data.js`)
- Record count: `529`
- Local preview assets: `532`
- Admin, approval, duplicate-review, OpenNana, and manual private lanes excluded
- Live URL: `https://image-prompt-archive-public-staging.andrew4may.workers.dev/`
- Current version: `b426a4d9-3bde-4dc6-a47b-c58da630e883`

## Files

- `wrangler.jsonc`: public Worker config
- `worker/index.js`: lightweight static asset Worker with admin routes blocked
- `public/`: Cloudflare static asset bundle

## Deploy

From this directory:

```powershell
& 'C:\Users\user\AppData\Local\npm-cache\_npx\32026684e21afda6\node_modules\.bin\wrangler.cmd' deploy
```

Wrangler uses the existing local OAuth session. Never place a token in this
directory or its build artifacts.

## Notes

- The current bundle uses 543 static files and 154,790,594 bytes total.
- Largest single asset is about 5.18 MiB, which is below the Cloudflare
  25 MiB per-file static asset limit.
- This bundle is separate from the Access-protected staging admin shell at
  `deploy/cloudflare-staging/`.
