# GitHub public-source canary and sealed local intake

## Sealed intake (collection only)

`collect_sealed_intake.py` is offline/dry-run by default. Live execution requires
`--fetch --collect`, explicit output/checkpoint paths and a valid owner public
RSA JWK at `config/intake-recipient.public.jwk.json`. It uses only the existing
enabled `freestylefly/awesome-gpt-image-2` source. The versioned parser supports
`docs/gallery-part-N.md`; README, JSON and the gallery index remain deferred,
not silently completed. A run fetches at most **20 containers, not 20 images**,
sequentially at at least one second between request starts. Commit and tree
identities are separate, and fetched prompt blobs are verified by Git blob SHA.

Prompt fences are retained exactly, including whitespace/newlines. Only pinned
in-repository image references are included; no image binary, external Markdown
link, login-only material, embedding or LLM request is fetched by this lane.
Rights are unknown/P3 and all image, metadata and release approvals are false.

Python streams the bundle directly into Node's built-in WebCrypto sealer. The
runner writes only RSA-OAEP/SHA-256 + AES-256-GCM ciphertext and hash-only metadata,
never a plaintext prompt file. The private key stays locally under the ignored
`data/private-research/platform-v2/secrets/` directory and is never sent to CI.
The sealer uses randomized keys/nonces and authenticates the envelope header.
Encryption protects confidentiality; it does **not** authenticate a sender.
Local import must verify the expected GitHub repository/workflow/run artifact
lineage before accepting a downloaded envelope.

The daily workflow uploads `intake.sealed.json` only. After the upload action
returns an artifact ID, the collector validates the exact ciphertext file hash
and acknowledges the metadata-only checkpoint. A separate cache-save step runs
after that acknowledgment. Failed or unparsed containers are never marked
complete. Cache state is advisory delivery state, not human approval. A cache
miss safely recollects; unchanged containers are re-offered after seven days
because artifact storage has a finite 30-day retention window. This is not a
permanent archive or proof of local import. Source records remain idempotent by
source/item/version and exact content hashes after decryption.

Local decryption (never run in Actions; output must not already exist):

```powershell
node src/github_sources/seal_intake.mjs unseal --private-key data/private-research/platform-v2/secrets/intake-recipient.private.jwk.json --input path/to/intake.sealed.json --output data/private-research/platform-v2/intake/downloaded-bundle.json
```

The decrypted bundle schema is `archive-sealed-intake-bundle-1`; its `records`
use `archive-local-intake-1`. `intake_envelope.validate_envelope()` verifies the
exact original prompt/content hashes and private approval boundary. `containers`
retain exact UTF-8 source text and parser-deferred items inside encryption for
local review. Neither decryption nor validation runs canonical promotion.

Offline checks:

```powershell
python -m unittest qa.test_github_source_collector qa.test_github_daily_observation qa.test_github_source_intake -v
node --test qa/test_github_source_seal.mjs
python qa/validate_daily_source_workflow.py
```

## Metadata canary

This lane discovers prompt-container and in-repository image candidates from an
explicit allowlist. It is not a general GitHub scraper and it never treats a
repository license as clearance for every prompt, image, logo, linked social
post, or user attachment.

Default offline validation:

```powershell
python src/github_sources/collect_public_repo.py
python -m unittest qa.test_github_source_collector -v
```

Bounded public API canary (read only):

```powershell
python src/github_sources/collect_public_repo.py `
  --repo freestylefly/awesome-gpt-image-2 `
  --fetch `
  --limit 100
```

Persisting the result additionally requires `--apply`; it writes only an
immutable artifact under `data/private-research/github-sources/runs/`. It does
not download blobs, edit canonical data, or publish media. Use the optional
`SOURCE_GITHUB_TOKEN` secret for a higher API allowance. Never place the token
in an artifact or command line.
