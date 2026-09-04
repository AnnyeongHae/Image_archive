# Cloudflare private staging handoff — 2026-09-03

## Outcome

The private administrator shell is deployed at:

- `https://image-prompt-archive-staging.andrew4may.workers.dev`
- Worker version: `dbd0856f-78c7-4bce-a9dd-f6846467d7f0`
- deployed at: `2026-09-02T15:11:42Z`

This is not the public archive MVP. The public count remains zero.

## Verified boundaries

- Cloudflare Access protects `/`, `/healthz`, `/admin/`, and `/api/admin/v1/status`.
- An unauthenticated request to every route above returns `302` to the Access team domain.
- The Worker independently validates the Access JWT with the team JWKS, issuer, audience,
  expiry, and RS256 signature.
- The administrator shell is read-only and reports zero private records.
- Mutating HTTP methods are rejected.
- Private R2 is bound but contains zero promoted archive objects.
- Neon and Hyperdrive are not bound.
- `data/`, `legacy/`, `dist/`, and `media/` are not in the deployment bundle.

The dependency-free local JWT and routing test passed all six cases. An authenticated visual
check in the administrator's browser remains a human verification item because this handoff
does not export or reuse the administrator's Access session.

## Current decision boundary

Allowed now:

- private staging shell maintenance
- outer Access and inner JWT validation
- read-only health/status endpoints

Still blocked:

- public archive records or media
- public R2 access
- administrator mutations
- bulk Neon or R2 migration
- any P3/P4 record outside the authenticated administrator plane

## Next canary

Bind a least-privilege, read-only Neon connection and return at most 50 P3 records from one
cursor endpoint. Keep the public record count at zero, do not upload R2 media, and retain a
single-command rollback to the current Worker version.

## Rollback reference

The prior empty infrastructure version is `77db0e97-2217-4629-8e8a-b02330c8bb86`. The current
R2 bucket is still empty, so rolling back the Worker does not risk archive object loss.
