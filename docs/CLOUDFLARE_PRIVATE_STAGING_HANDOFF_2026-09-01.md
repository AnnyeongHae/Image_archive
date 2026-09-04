# Cloudflare staging handoff — 2026-09-01

## Current outcome

An intentionally empty infrastructure canary is deployed at:

- `https://image-prompt-archive-staging.andrew4may.workers.dev`

This deployment is **not** the archive release. Production and portfolio publication remain
blocked. The canary contains only four static files and a small Worker entrypoint.

Verified properties:

- public archive record count: `0`
- private records, prompts, approval queue, and images included: `false`
- admin routes: disabled and return `404`
- Neon/Hyperdrive: not bound
- R2 binding: present
- R2 object count: `0`
- response cache policy: `no-store`
- crawler policy: `noindex`, `nofollow`, `noarchive`, and `robots.txt` disallows `/`

Cloudflare resources created:

- Worker: `image-prompt-archive-staging`
- private R2 bucket: `image-prompt-archive-private-staging`
- deployed Worker version: `77db0e97-2217-4629-8e8a-b02330c8bb86`

## Why the real archive was excluded

The existing `dist/` build declares `release_eligible=false`. The 18,815 canonical records are
currently rights-tiered for private use, and the public export contains zero records. Therefore
the staging canary does not bundle anything under `data/`, `legacy/`, `dist/`, or `media/`.

## Manual Cloudflare step still required

The Cloudflare plugin is installed and advertises the official Cloudflare API MCP server, but
its `search` and `execute` tools were not callable in this task. Wrangler OAuth was available,
so Workers and R2 setup completed through Wrangler. Cloudflare Access still needs dashboard
configuration:

1. Open **Workers & Pages**.
2. Select **image-prompt-archive-staging**.
3. Open the **Access** tab.
4. Choose **Protect this Worker behind Access**.
5. Protect **all traffic** for this Worker.
6. Add an allow policy restricted to the intended administrator account.
7. Apply the policy and confirm that an unauthenticated request is redirected to Access.

Do not add admin data, Neon, prompts, or images until this test passes.

## Next canaries

1. Access-protected admin shell with no database.
2. Hyperdrive connected to a rotated, least-privilege Neon role.
3. Read-only 50-record P3 admin query while the public query remains zero.
4. Private R2 original upload and authenticated media proxy.
5. Only after per-record rights approval, a separate public derivative lane.

## Rollback

If this canary is no longer needed, remove the Worker and the empty R2 bucket separately. No
archive data would be lost because neither resource currently contains archive content.
