# Image RAG A/B + similarity canary

Status: `local_preparation_complete / inference_blocked_pending_human_approval`.
Date: 2026-09-03. This is private experiment tooling, not the deployed retrieval API.

## What is being compared

| Arm | Model | Document input | Purpose |
|---|---|---|---|
| A | Gemini Embedding 2 | Image only | Visual retrieval / visual-family signal |
| B | Same model | Same image + original source prompt | Whether prompt context improves retrieval or overwhelms visible evidence |

This is an input ablation, NOT Gemini-vs-Voyage/SigLIP and NOT a measured SOTA claim. We deliberately avoid caption-generation calls in the first experiment. Prompts describe desired images and may not faithfully describe the actual result; human relevance labels must judge the image itself.

One request obtains a 3072-dimensional vector. Prefixes of 768 and 1536 are normalized and evaluated locally, with no second embedding call. All arms share the same input PNG, query, preprocessing, and dimensionality at each comparison. Never compare vectors across unrelated model spaces. Gemini Embedding 2 uses query text `task: search result | query: ...`, not the unsupported `taskType` field; image+prompt inputs have no task prefix. [Official embedding guide](https://ai.google.dev/gemini-api/docs/embeddings).

## Three separate relations, not one similarity percentage

| Relation | Evidence | Group behavior |
|---|---|---|
| Exact file / exact decoded pixels | SHA-256 of file; separate EXIF-normalized original RGBA pixel identity | Observed identity groups; retain every record and prompt |
| Near-copy candidate | 64-bit pHash and dHash distances, aspect ratio and color gates, low-information exclusion | Human-review groups; do not merge/delete assets |
| Visual family | Image-only embedding; mutual top-k neighbors; every group pair must pass an experimental cosine threshold | Overlapping soft reference collections, never duplicate identity |

Prompt SHA only detects exact text. Normalized text hashing and character-trigram Jaccard are independent evidence; same prompt does not prove same image. Empty prompts do not create a prompt family. "80% hash similarity" is not an 80% chance of semantic sameness. Current pHash/dHash thresholds of 8 bits and semantic-family cosine 0.85 are UNCALIBRATED starting hypotheses, not universal acceptance criteria. `no_match_by_current_signals` is not proof of unrelatedness.

Near-copy and visual families use complete-link candidate groups; A–B and B–C alone cannot force A–B–C. This intentionally under-groups ambiguous chains. Mutual-kNN also caps broad groups: manually curated category collections can be larger and can overlap, but need a separate human decision. All-pairs comparison / maximal-clique extraction here is bounded to 20 input items; do not run this implementation unchanged on the full corpus.

SSCD is a purpose-built copy-detection alternative when crop/watermark/edit recall is insufficient. DINOv3 is an alternative visual descriptor, not a universal duplicate detector. Neither is installed or evaluated here. Their inclusion requires a separately approved local experiment and labeled hard negatives. [SSCD paper](https://arxiv.org/abs/2202.10261), [DINOv3 paper](https://arxiv.org/abs/2508.10104).

## Cost and safety

The supplied screenshot says Tier 2, a paid usage tier. RPM/TPM/RPD are speed quotas, not free credits. The `.env` key's billing balance/project tier was NOT established by the model-metadata request. Default to no paid inference until budget approval. [Billing](https://ai.google.dev/gemini-api/docs/billing).

- Hard canary caps: 20 image records, 15 text queries, 55 inference attempts, configured reservation budget no greater than US$0.10.
- Pricing snapshot: image $0.00012 each; text $0.20 / million tokens. Worst-case reservation allows 8192 text tokens per text-bearing call, even though local text is capped at 6000 UTF-8 bytes. Twenty A + twenty B + fifteen query calls reserve up to $0.062144 before deduplication. This is a conservative estimate under the linked pricing, not an invoice guarantee, tax estimate, or account-wide spending cap. [Standard pricing](https://ai.google.dev/gemini-api/docs/pricing#gemini-embedding-2).
- Exact matching request keys reuse vectors across duplicate records, identical queries, and resumed runs within the SAME run directory. Cross-run reuse is not yet implemented; a new run can incur new calls.
- Entire remaining uncached experiment is checked before its first paid call. Per-call reservation is persisted before HTTP, including failed/uncertain attempts. No automatic retries/fallbacks.
- An archive-level execution lock serializes separate run IDs; an additional run lock prevents same-run races. Budgets remain per run, not a shared account-wide or lifetime quota. Crash locks are not automatically broken.
- No active inference by default. `--execute --apply --allow-paid --max-cost-usd ...` plus reviewed annotations are required. `--allow-paid` is a technical gate, never a substitute for an actual human instruction.
- `.env` is read only from the archive root; process environment takes precedence. Keys are headers, never CLI args/artifacts. Provider errors are redacted. No redirects or arbitrary external endpoints.
- Only local raster files inside the archive; checksum validation before analysis and before upload. Signed source URLs are not copied into the manifest. Original files are immutable.
- A source cached for private research is not automatically approved for third-party AI processing. Every manifest item starts unapproved. UI exports review decisions; it cannot spend or transmit.
- Qdrant is connectivity-check-only in this phase. No collection creation, point upload, deletion, R2 writes, Workers deploy, GitHub workflow execution, or public promotion.

## Current review artifact

`data/private-research/image-rag-canary/runs/2026-09-03-embedding-ab-v1/`

- `manifest.json`: 20 image records, 18 distinct originals, source/prompt hashes, preprocessing, model/dimension plan. Exact-media, exact-prompt, and perceptual seeds are mixed with source-diverse samples; selection is purposive, NOT statistically representative.
- `inputs/`: 18 content-addressed, 768px-max-side RGB PNG inputs shared by duplicate records; original alpha identity remains in signals. PNG is a private experiment input, not a change to the public WebP strategy.
- `offline.json`: 190 pair comparisons and six relation records: two exact-file, two exact-pixel, one exact-prompt, one near-copy candidate. These overlap; do not call them six independent duplicate sets. Near-copy is not human-confirmed.
- `review.html`: private image and group review, individual external-AI permission checkboxes, annotation JSON download.
- `annotations.template.json`: blank reviewer, unapproved inputs, empty query/gold-label placeholder. Never interpret unfilled labels as zero relevance.
- `prepared.json`: completion receipt bound to the manifest hash; missing/mismatched receipt blocks inference.
- `browser-qa.json`, `review-desktop.png`, `review-mobile.png`: local UI checks. The test download is synthetic and is never saved as live human annotations.

Earlier local smoke-run directories are preserved but superseded. Use ONLY the above run for review; no inference ran in any of them.

## Commands (archive root)

Offline write-free plan:

```powershell
python -X utf8 src/run_image_embedding_canary.py
```

Read-only provider connectivity, no embeddings:

```powershell
python -X utf8 src/run_image_embedding_canary.py --preflight
```

Prepare another isolated local sample; never overwrite an existing run ID:

```powershell
python -X utf8 src/run_image_embedding_canary.py --prepare --apply --run-id YOUR_NEW_RUN_ID --limit 20
```

Human steps: inspect images and prompts in review.html; check only authorized inputs; enter reviewer; download JSON. Save as that run's `annotations.json`. Fill 3–5 real search queries first (max 15), rather than maximizing quota. For quantitative retrieval scores, grade EVERY sample per query: 0 irrelevant, 1 partial, 2 good, 3 ideal, and set `human_judged: true`. Query evaluation must be independent of the source prompt. Add pair labels only when visually reviewed: `exact`, `near_copy`, `visual_family`, `semantic_only`, `unrelated`.

AFTER explicit budget and image-input approval only:

```powershell
python -X utf8 src/run_image_embedding_canary.py --execute --apply --allow-paid --max-cost-usd 0.10 --run-id 2026-09-03-embedding-ab-v1 --annotations data/private-research/image-rag-canary/runs/2026-09-03-embedding-ab-v1/annotations.json
```

Incomplete individual approvals block the whole run; changing the selected sample requires a fresh preparation/review. The runner does not silently skip unapproved items and alter the benchmark.

Outputs after successful inference: `budget.json`, `vector-cache/`, `vectors.json`, `executed-annotations.json`, `evaluation.json`. Retrieval includes top 1/3/5 metrics and MMR-diversified alternatives, three dimensions per arm, pair cosine evidence, and visual-family candidates. Unknown gold means metrics remain null. `winner` remains null: 20 images only establish a canary, not corpus-wide model superiority. No visual-family results exist yet because paid inference is pending.

## What follows a successful canary

1. Human-grade held-out queries and difficult pairs (same category/different composition, same prompt/different outcome, crop/watermark/recolor). Report pair precision/recall, false merge rate, Recall@1/3/5, nDCG@k and MRR@k. Pair labels are currently retained as evidence, but threshold sweeps/pair-F1 reporting need a subsequent evaluator.
2. Tune thresholds on one subset; validate once on a separate subset. Split by source/visual family to avoid duplicate leakage. Select the smallest dimension whose judged retrieval remains acceptable.
3. Test Gemini versus Voyage or local SigLIP/SSCD on the SAME held-out data; do not conflate input ablation with provider A/B. No extra providers were called here.
4. Add an isolated Qdrant canary collection after approval. Production can use named image/joint vectors and payload `asset_id`, `exact_group_id`, `visual_family_ids`, model/preprocessing versions, and review states. Keep full prompts/rights decisions in the canonical metadata store. Exact-family collapse + expand-members and optional MMR diversification give useful top3/top5 options without losing variants.
5. Scale candidate generation through Qdrant top-k; only locally compare retrieved neighbors. Run ingestion/analysis in a local job or bounded GitHub Actions, not heavy ML inside a request-serving Worker. Workers handle token authentication and search orchestration. None of that deployment is part of this canary.

Tests:

```powershell
python -m unittest discover -s qa -p 'test_image_rag_*.py'
python -m unittest discover -s qa -p 'test_duplicate*.py'
python -X utf8 qa/check_image_rag_review.py --run-id 2026-09-03-embedding-ab-v1 --apply
```
