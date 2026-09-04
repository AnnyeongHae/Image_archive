# OpenNana private review workflow

This directory owns the source-specific collector and review contracts. It never writes directly to `data/canonical/archive_records.jsonl`.

## Safe local canary

All commands are dry-run unless `--apply` is present. The sample is locally fabricated and performs no network requests.

```powershell
python src/opennana/collect.py --seed-sample --apply
python src/opennana/normalize.py --apply
python src/opennana/dedupe.py --apply
python src/opennana/build_review_queue.py --apply
python qa/validate_opennana_workflow.py --write-report
```

The same offline sequence is available through one operational entry point:

```powershell
python src/opennana/run_pipeline.py --sample --apply --max-details 20
```

The queue builder writes the private review contract to `data/private-research/opennana/review_queue/current.json`, a decision draft to `decisions/decision-draft.json`, and an admin-only static projection to `legacy/current_archive/opennana-review-data.js`.

## Network boundary

Network access requires both `--fetch` and `--apply`. The configured cap and the hard cap are both 20 free details. The collector evaluates only the `User-agent: *` robots group for `/api/prompts`, requires the expected reference-use Content-Signal, stops on 403/429, strips any unexpected locked body before writing raw data, and never downloads source images.

```powershell
python src/opennana/run_pipeline.py --fetch --apply --max-details 20
```

## Forward-only daily sync

The bounded command above remains the network canary. Historical detail backfill is disabled. Establish the current public/free ID and visible-metadata inventory once without reading any detail endpoint or changing the review queue:

```powershell
python src/opennana/run_daily_sync.py --fetch --apply --baseline-only
```

After that baseline exists, the daily lane compares the public/free list and fetches details only for IDs absent from the baseline or source versions whose visible list metadata changed:

```powershell
python src/opennana/run_daily_sync.py --all-free --fetch --apply
```

The baseline command records source ID/version watermarks only and preserves raw, staging, queue, decision draft, review projection, and canonical files byte-for-byte. The daily command fails closed before opening a client when the configured forward-only mode has no valid baseline. It keeps the same 1 request/second and concurrency 1 boundary, processes every changed/new detail candidate in 100-item batches rather than a hard total cap, commits source-version and exact-prompt checkpoints only after the batch has passed normalization, deduplication, and review-queue merge, and resumes failed work on the next run. Same-source unchanged versions and durable exact prompt hashes are omitted from the active approval queue; near-duplicate and remix-family candidates remain reviewable. Paid, locked, authenticated, or paywalled prompt bodies remain forbidden. Neither command approves, promotes to canonical, publishes, or downloads source image binaries.

## Explicit bounded backlog recovery

Previously observed free rows whose detail body was never processed are kept
out of the daily lane. Recover them only through the separate manual command:

```powershell
python src/opennana/run_backlog_sync.py --fetch --apply --max-details 300
```

`--max-details` is a required run-level safety bound and may be greater than
100. The runner still splits work into completed batches of at most 100
(`300` becomes `100 + 100 + 100`), records the remaining estimate, and resumes
from the durable detail watermark after interruption. An invocation without
the complete `--fetch --apply --max-details N` activation is an offline,
write-free plan. This lane is not scheduled and never applies decisions,
mutates the canonical archive, or changes the public release.

Each applied pipeline run writes a manifest to `data/private-research/opennana/runs/pipeline-<run-id>.json`. An approval produces only `canonicalization_pending`. The runner never applies decisions and never mutates the canonical archive. Approval does not grant item rights, public release, or commercial reuse permission.

## Local review API

Use the deployment-shaped server instead of `python -m http.server` when an
administrator will finalize browser decisions:

```powershell
python src/opennana/review_server.py --host 127.0.0.1 --port 8765
```

It serves the platform root and exposes only three same-origin endpoints:

- `GET /api/review/v1/state` creates an HttpOnly local review session and
  returns its CSRF token plus the exact active queue revision.
- `POST /api/review/v1/preview` accepts the existing complete
  `opennana-decision-draft-1.0` object. Every current row must be explicitly
  decided and every content hash must still match. It returns action counts,
  a deterministic decision batch ID, and a five-minute commit token.
- `POST /api/review/v1/commit` accepts
  `{decision_batch_id, commit_token, decisions: <complete draft>}`. It
  revalidates the token, session, revision, hashes and full coverage before
  calling `apply_decisions.py --apply`.

POST requests require the exact local `Origin`, the session cookie and the
`X-Review-CSRF` header. The server emits no CORS allow header. A deterministic
batch receipt makes a network retry idempotent; malformed, incomplete, stale,
expired and cross-origin requests fail closed. JSON download in the browser
remains the recovery path.

The default post-apply promotion command is:

```powershell
python src/opennana/build_archive_lane.py --apply
```

It receives these environment variables:

- `OPENNANA_ARCHIVE_ROOT`
- `OPENNANA_DECISION_BATCH_ID`
- `OPENNANA_DECISION_REQUEST_SHA256`
- `OPENNANA_QUEUE_REVISION`
- `OPENNANA_APPLIED_PATH`
- `OPENNANA_PENDING_PATH`
- `OPENNANA_PUBLIC_RELEASE_ALLOWED=0`

The hook must be deterministic, idempotent for the batch ID, atomic for its
own outputs, and must return a non-zero exit code before leaving partial output.
JSON stdout is recorded in the commit result. Override it without invoking a
shell by passing a JSON argv array through `--promotion-command-json` or
`OPENNANA_PROMOTION_COMMAND_JSON`. If decision apply or promotion fails, the
server restores the pre-commit queue/state/projection and removes only the new
decision artifacts from that attempt. No review API path may make rights,
commercial-use, or public-release eligibility true.

Run the isolated stdlib contract suite with:

```powershell
python -m unittest qa.test_opennana_review_api -v
```
