# JSON-first retention and approved live canary

2026-09-03. Private 20-record experiment; not a production rollout.

## Current result

The user explicitly approved the fixed Gemini image/prompt/query and Voyage image/query payloads with a combined US$0.10 limit. Approval is recorded in `consent-comparison-v2.json`; the earlier pending record is preserved.

Voyage completed: 18 distinct prepared images representing 20 records, plus five Korean query embeddings, in 23 successful API requests. Gemini completed five unique requests, then remained incomplete after two HTTP 429 responses. One deliberately reviewed recovery after more than five minutes succeeded; a later new request returned another 429, and Gemini execution stopped. The second response supplied no usable retry delay or quota period. Do not infer daily exhaustion, an invalid key, or a specific free-tier limit from that response.

The full three-arm A/B comparison is therefore **partial**, with no model winner. Cached Gemini vectors cover four sample records in each image/joint arm, but no Gemini query vectors exist. Shared cache aliases explain why covered record counts exceed unique requests. The completed Voyage arm supports all five top-1/3/5 smoke queries.

## Representative policy

| Tier | Preferred prompt structure |
|---|---|
| 1 | Useful, valid structured JSON; reusable JSON templates included |
| 2 | Explicit sections, key-value controls, or structured templates |
| 3 | Descriptive natural-language prompt |
| 4 | Minimal, empty, or materially underspecified text |

Within a tier, deterministic structural signals are used before arrival evidence. Length alone is not quality. This is a versionable local heuristic, not a validated semantic quality model and not an LLM call. Empty JSON, invalid JSON, and a single field wrapping prose must not gain Tier 1 merely by using braces. Original prompts are never rewritten.

`DAV490-019` is a valid JSON template with five top-level fields and four template controls. It is preferred for its family over the prose prompt `API-067`. Both remain active: their image file/pixel hashes are not exact matches.

Exact file, decoded pixel, or nonempty original-prompt equality can hide a lower-ranked record from the default sample list. A greedy direct-match policy records the direct counterpart and any ancestor path. It does not collapse every mixed image/prompt connected component into one item or retroactively hide an independent earlier-ranked representative through a later bridge. All archived records, prompts, images, and source aliases remain recoverable.

The present sample still has 18 active records and two archived records: `API-049` retains `DAV490-276`; `YOM-045` retains `YOM-046`. A global one-based `rank_index` from retention is also consumed by the family renderer, preventing a separate UI ranking rule. All 20 records lack verified ingestion timestamps; canonical ordinal is an explicitly labeled final tie-break fallback, not historical arrival proof.

## Similarity evidence and limits

`API-067` / `DAV490-019` has Voyage image cosine **0.999332**, plus pHash and dHash Hamming distance zero. This is strong candidate evidence, not “99.9332% identical” or permission to delete. Zero perceptual-hash distance also does not prove byte/pixel identity. The family can be expanded to see both prompts and both evidence types.

Visual-family candidates use image-only mutual k-nearest neighbors and complete-link grouping (`k=3`, cosine threshold hypothesis `0.85`). The threshold has not been calibrated with human labels; broad thematic/style similarity can be missed. No embedding similarity archives a record. Future collection-specific labels should distinguish exact copy, near-copy, visual family, semantic-only relation, and unrelated images.

Observed Voyage top-1 results:

| Query intent | Top-1 Style ID |
|---|---|
| Anime classroom | YOM-045 |
| Hand-drawn food/travel map | DAV490-019 |
| Multi-outfit fashion collage | CASE-398 |
| Warm wooden ryokan portrait | API-049 |
| Movie-streaming main UI | CASE-387 |

These are corpus-aware, unjudged smoke queries, not a held-out retrieval benchmark. Precision, Recall, nDCG, and winner remain unset. Similar results are retained in top-k; a future diverse-options API can expose one representative per family and expand its members, but this test does not claim that production API exists.

## Cost and recovery integrity

There are 30 recorded attempts: 28 successes and two 429 failures. The combined durable reservation, including both failures, is **US$0.031706176**. It is not the actual invoice. Voyage receipts report 7,599,360 image pixels and 83 text tokens; at standard paid prices this is US$0.004569576 before any free allowance. Gemini receipts report 2,988 prompt tokens without a retained modality breakdown. Free balance and actual billing are unverified. [Voyage pricing](https://docs.voyageai.com/docs/pricing), [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing#gemini-embedding-2).

One archive-wide lock and one budget ledger cover provider-specific continuation. Cached payloads are not resent. The exceptional reviewed 429 recovery adds an attempt ID and reservation rather than erasing the failed reservation; only one recovery is allowed in this comparison, after a minimum 60-second cooldown and exact ledger/manifest evidence checks. Other ambiguous failures remain fail-closed. Provider errors retain only bounded sanitized status, coarse quota period, and retry-delay metadata, never the raw response body.

The first historical recovery used the reviewed CLI 429 output because that original attempt lacked structured error metadata. The current hardened gate additionally requires a ledger-recorded `429/rate_limited`, honors the returned server delay, and rejects zero or daily-exhausted quota indications. Historical attempts have not been retroactively rewritten.

The 0.10 limit covers this experiment's modeled inference reservations, not unrelated account traffic, tax, storage, or a provider-side billing hard cap. No Qdrant writes, R2 uploads, deployment, package installation, or original deletion occurred.

## Offline refresh and remaining work

From the archive root, refresh current priority, cached scores, and the view without external API calls:

```powershell
python -X utf8 src/run_image_embedding_comparison.py --refresh --apply
python -m unittest discover -s qa -p 'test_image_rag_*.py'
python -X utf8 qa/check_image_rag_comparison_view.py --apply
```

Current artifacts are under `data/private-research/image-rag-canary/runs/2026-09-03-embedding-ab-v1/`: `comparison-results-v1.html`, and `comparison-v1/{retention,evaluation,budget,vectors}.json` plus content-addressed `vector-cache/` receipts. The original `offline-view-summary.json` is historical evidence from before external approval, not the current state. Read `evaluation.json` for current status.

Remaining: resolve the Gemini project's actual Embedding 2 quota/rate condition before authorizing another bounded recovery plan; complete the missing Gemini arm/query vectors; collect human relevance/family labels; integrate the chosen policy into the full archive. API-token search, Qdrant indexing, Workers/R2/GitHub automation and public release are outside this completed private canary. The `ai-ml` workflow informed explicit measurement, cache reuse, and the distinction between a working partial run and validated retrieval quality.
