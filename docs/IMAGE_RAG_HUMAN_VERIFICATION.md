# 200-record human similarity review

Current decision UI: v2 separates **동일 — 중복 삭제** from **거의 동일 — 그룹핑**. See [IMAGE_RAG_DUPLICATE_DECISIONS.md](IMAGE_RAG_DUPLICATE_DECISIONS.md). The v1 contract below remains supported; old near-duplicate labels never become deletion approval.

## Purpose and scope

Voyage image vectors propose review candidates. Human decisions define which pairs are useful to group; embedding scores do not create human verification. This is a private, challenge-enriched 200-record experiment, not a full archive migration, deletion job or public release.

The first 50 records and their prior run remain unchanged. The extra 150 use 3 perceptual candidates, 75 prompt-scaffold variants, 30 exact-prompt candidates, 10 exact-media controls and 32 source/lane-diverse records. These are sampling reasons, not ground-truth labels. The persisted duplicate index only had perceptual hashes for 128 of 9,001 assets, so its three perceptual groups alone could not supply a broad visual sample. The new 200 images receive local identity/perceptual signals and Voyage vectors separately.

The existing logical-deletion rule remains `exact_file OR (exact_pixels AND prompt_exact)` with JSON-priority representative selection. Prompt-only variants remain active. A human near-duplicate label never authorizes physical deletion or silently broadens that exact rule.

## Human label meanings

| Label | Decision |
|---|---|
| `near_duplicate` | Essentially the same rendered result, allowing resizing, re-encoding, a crop or a very small edit. Not exact file/pixel identity. |
| `same_visual_family` | Visually related composition/style/subject; useful to open as one family while keeping each output. |
| `same_theme_only` | Shared topic or intent but substantially different visual outputs. |
| `unrelated` | No useful common visual family. |
| `unsure` | Unresolved; never counted as a verified positive or negative. |

Composition, style and subject can also be judged separately. This helps explain why a shared prompt can produce unrelated visuals and why an embedding score can be high despite an important layout difference.

Only a directly reviewed pair can become human-verified. A→B and B→C labels do not verify A→C or an entire group. Review verification records a human's stated decision, not a cryptographic identity check or an independent rights approval. Luna must not fill these human decisions.

## Review sequence

1. Open the run's `human-similarity-review.html` and enter the actual reviewer name/identifier.
2. Judge the images first. Machine scores and prompt context start hidden and become available after a label is chosen. Later revisions remain possible; this is not a blinded research trial.
3. Choose one label, optional composition/style/subject dimensions, and a short reason when useful. Use `unsure` instead of forcing an answer.
4. Download the review JSON as the durable handoff. Browser localStorage is a convenience draft, not the sole record; it may be cleared or differ between browsers.
5. Import the exported JSON with `src/import_image_similarity_labels.py --source-run-id <run-id> --labels <export.json>`; inspect the dry-run result, then add `--apply` to append the validated private review.

Every export is bound to the run, manifest SHA, model/dimension, vector fingerprint, sampled pair IDs and both image hashes. A changed run or different pair list must not accept stale labels. Existing reviews are retained instead of overwritten.

## Threshold interpretation

The compact review set targets at most 80 pairs rather than requiring all 19,900 record pairs. Exact controls are limited; high-score, boundary, prompt-challenge and negative pairs provide contrasting examples. Ordinary non-exact review candidates come from the retained active records so archived copies do not dominate judgments.

The current 0.85 visual-family grouping cutoff and the review's 0.90 boundary are hypotheses, not verified universal cutoffs. A cosine of 0.90 is not a 90% probability of similarity.

Before enough resolved human labels exist, threshold summaries remain unavailable. After the minimum is met, summaries at several cutoffs show only match rates in the reviewed sample. They do not estimate unbiased archive precision/recall, do not choose an automatic production cutoff and do not certify model superiority. The enriched selection and human disagreements must remain visible. A separate held-out set is required before claiming a general improvement.

## Operational boundaries

- Voyage only; Gemini remains paused.
- Existing cache/query vectors and the complete prior cost ledger are carried over. New calls stay within the existing cumulative US$0.10 reservation cap; free-credit balance and actual billing are not assumed.
- No source deletion, canonical DB mutation, Qdrant/R2 writes, publication or release approval.
- Luna metadata remains a separate preparation-only lane. Image-specific facts, source-prompt intent and speculative reuse ideas stay separate. See `IMAGE_RAG_LUNA_METADATA.md`.
