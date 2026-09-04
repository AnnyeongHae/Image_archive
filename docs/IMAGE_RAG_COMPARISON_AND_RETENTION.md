# Exact retention and three-arm comparison

Historical pre-approval revision. Superseded by [JSON-first priority and live results](IMAGE_RAG_PRIORITY_AND_LIVE_RESULTS.md): the user explicitly approved transmission, Voyage completed, Gemini is partial after HTTP 429, and representative selection is now structure-first. The pending consent and zero-call statements below describe the earlier checkpoint only.

Date: 2026-09-03. Private canary only; no production or public deployment.

## Implemented local behavior

The 20-record sample now has 18 active records and two recoverably archived records. The underlying canonical rows and all original files remain unchanged. This is a derived display policy, not filesystem deletion/moving or a rewrite of the main archive dashboard.

| Representative retained | Archived from default sample list | Exact evidence |
|---|---|---|
| API-049 | DAV490-276 | File bytes, decoded pixels, and original prompt |
| YOM-045 | YOM-046 | File bytes and decoded pixels |

One perceptual near-copy pair remains expandable: API-067 and DAV490-019. It is a candidate, not a human-confirmed semantic family. Similarity groups can later receive Gemini and Voyage image-embedding scores; neither is used to delete records.

`retention.py` uses exact file/pixel OR exact original prompt evidence, excluding empty prompts. Later directly matching items reference their earlier counterpart; source aliases and the ancestor chain are retained. A later bridging record does not retroactively archive an earlier independent record. This is intentionally less aggressive than forcing an entire mixed image/prompt connected component into one item.

### First-arrival caveat

All 20 sampled records lack a verified first-arrival timestamp in this integration. The fallback is the existing canonical JSONL ordinal, explicitly labeled as such. This is not evidence of historical ingestion order. Do not deploy a corpus-wide "first arrived wins" claim from this sample.

Valid first-arrival fields are explicit `arrival_at`, `ingested_at`, `first_seen_at`, or `collected_at`, with a timezone-aware ISO timestamp. Approval, creation, discovery, observation, update, and review dates are retained as evidence but do not silently become ingestion dates. Future collector integration should persist an immutable `first_ingested_at` at initial acceptance, with an explicit mapping into `arrival_at`; do not rewrite it on updates. Production integration is still pending.

## Three-arm experiment ready, not executed

| Arm | Document input | Requested dimension | Comparison dimension |
|---|---|---:|---:|
| gemini_image | 768px-max-side prepared PNG | 3072 | normalized prefix 1024 |
| gemini_joint | Identical PNG + original prompt, max 6000 UTF-8 bytes | 3072 | normalized prefix 1024 |
| voyage_image | Identical PNG | 1024 | 1024 |

Five Korean queries cover anime classrooms, hand-drawn food maps, fashion collages, warm ryokan portraits, and streaming-service UI. These queries were chosen with knowledge of the corpus and have no human relevance labels: they are functional smoke tests, not an independent accuracy benchmark. No winner/Recall/nDCG is reported.

Gemini image vs joint isolates the prompt contribution. Gemini image vs Voyage image compares providers on the same samples and dimensions; never dot-product vectors from different providers together or compare raw cosine values as a cross-provider quality score. The Voyage adapter supports joint input too, but that fourth arm is deferred to save calls.

Voyage's official docs list 200M initial text tokens and 150B image pixels per account for the multimodal family. They do not establish a monthly refresh or this account's remaining balance. Pricing beyond the allowance is $0.12/M text tokens and $0.60/B image pixels. This implementation uses the standard endpoint, not Batch (where free credits do not apply). [Voyage pricing](https://docs.voyageai.com/docs/pricing), [multimodal API](https://docs.voyageai.com/reference/multimodal-embeddings-api).

The computed plan has 70 logical requests but 65 unique cache keys. Conservative paid-price reservation, ignoring free balances, is US$0.06807418 under the combined US$0.10 cap. Gemini text-bearing requests reserve the 8192-token allowance; Voyage text queries reserve its full 32000-token allowance; image-only Voyage requests reserve prepared-image pixels (50k minimum) plus 256 prefix tokens. This is an estimate at current prices, not an invoice or account-wide cap. [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing#gemini-embedding-2).

All providers share one run ledger and archive-wide execution lock. Every pending unique request must fit the remaining budget before the first call. Reservation is durable before HTTP; uncertain failures cannot auto-retry. No file-upload persistence API, Qdrant write, paid reranker, package installation, or deployment was used.

## Security gate: external transmission pending

The attempted execution was rejected by the environment's approval reviewer BEFORE process launch. Reason: the user's general request to test and provision keys did not explicitly authorize the exact private sample payload to both external destinations. No workaround or indirect execution followed.

Observed: inference requests 0; sample transmissions 0; `comparison-v1/budget.json` does not exist; all four configured key names are present. The consent record is `external_ai_approved=false` and `authorization_status=awaiting_explicit_transfer_approval_after_security_gate`.

Required next authorization: transmit these 20 sampled prepared images plus original prompts and five query texts to Gemini, and the same images plus five query texts to Voyage, for private embedding analysis within a total US$0.10 budget. This is not rights clearance, public release permission, or permission to delete original images. Ask for this explicitly rather than converting a general testing request into consent again.

## Files and commands

Run root: `data/private-research/image-rag-canary/runs/2026-09-03-embedding-ab-v1/`.

- `comparison-results-v1.html`: local active list, closed archive section, expandable exact/near-copy groups.
- `comparison-v1/manifest.json`: source sample plus explicit ordinal fallback.
- `comparison-v1/retention.json`: active IDs, archived aliases/reasons, exact groups, ordering evidence.
- `comparison-v1/queries.json`: five unjudged queries.
- `comparison-v1/offline-view-summary.json`: counts and unchanged-source boundary.
- `comparison-v1/browser-qa.json`, `results-desktop.png`, `results-mobile.png`: local browser checks.
- `consent-comparison-v1.json`: pending authorization, not executable approval.

From archive root:

```powershell
python -X utf8 src/run_image_embedding_comparison.py
python -X utf8 src/run_image_embedding_comparison.py --prepare --apply
python -m unittest discover -s qa -p 'test_image_rag_*.py'
python -m unittest discover -s qa -p 'test_duplicate*.py'
python -X utf8 qa/check_image_rag_comparison_view.py --apply
```

After explicit payload/budget approval is recorded (not before):

```powershell
python -X utf8 src/run_image_embedding_comparison.py --execute --apply --source-run-id 2026-09-03-embedding-ab-v1 --consent data/private-research/image-rag-canary/runs/2026-09-03-embedding-ab-v1/consent-comparison-v1.json --max-cost-usd 0.10
```

Validation: 75 image-RAG unit tests and 10 existing duplicate regression tests passed. Edge desktop/mobile checks verified active/archived counts, closed archive default, expanding archive/groups, loaded images, no horizontal mobile overflow, and no JS errors. Browser tests disallowed HTTP(S) requests.

Scope not completed: external embedding calls, human-rated model comparison, corpus-wide retention integration, verified first-ingestion history, Qdrant storage/search, Workers/R2/GitHub deployment. `ai-ml` evaluation and safety principles informed the isolated local test and explicit unmeasured-result state.
