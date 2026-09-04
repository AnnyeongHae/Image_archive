# Cloudflare Access admin-shell canary

This deployment lane proves the private administrator boundary before any archive data is
connected. Cloudflare Access protects the whole Worker, and the Worker independently verifies
the `Cf-Access-Jwt-Assertion` signature, issuer, audience, and expiry.

Current safety properties:

- public record count is fixed at zero
- no file under `data/`, `legacy/`, `dist/`, or `media/` is bundled
- `/admin/` contains only a read-only, data-empty status shell
- `/api/admin/v1/status` reports zero private records and `mutation_enabled=false`
- every Worker and asset response is `no-store` and `noindex`
- R2 stays private; no `r2.dev` or custom-domain exposure is enabled
- Neon and Hyperdrive are intentionally absent
- mutating methods are rejected with `405`

Local contract test:

```powershell
node --experimental-default-type=module qa/test_cloudflare_staging_worker.mjs
```

The next gate is a separately reviewed, read-only Neon canary limited to 50 P3 records. It must
not change the public count or enable mutations.
