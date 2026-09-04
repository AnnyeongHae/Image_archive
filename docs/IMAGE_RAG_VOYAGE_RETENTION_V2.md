# Voyage selection and duplicate policy v2

Decision source: user request, 2026-09-03. Scope: private 50-record experiment; not a public deployment or archive-wide purge.

## Exact deletion versus shared-prompt variants

The deletion predicate is evaluated on the **same pair**:

`exact_file OR (exact_pixels AND prompt_exact)`

| File bytes match | Decoded pixels match | Non-empty raw prompt matches | Action |
|---|---|---|---|
| yes | any | any | Keep preferred representative; logically delete duplicate record |
| no | yes | yes | Keep preferred representative; logically delete duplicate record |
| no | yes | no | Keep both; different prompt must not be discarded by pixels alone |
| no | no | yes | Keep both searchable; group as prompt variants |
| no | no | no | Keep both; optional visual-similarity grouping is separate |

The user called the pixel signal `exact_pixel`; the existing code uses `exact_pixels`. They refer to the same decoded-pixel equality signal. pHash/dHash similarity is not exact-pixel identity and never authorizes deletion. Empty prompts do not produce prompt-exact relations.

Logical deletion removes a duplicate record from the active list and retrieval corpus. The source row, source file, prepared image, old vector receipt, and explicit relationship to its retained representative remain available for recovery/audit. This revision does not unlink physical files. In this sample, identical records can reference the exact same prepared file; removing that file would also break the retained record.

Representative priority remains useful JSON/JSON template → other structured prompts → descriptive prose → short/incomplete prompt; arrival evidence or canonical ordinal only breaks later ties. A lower-priority early record must not defeat a later stronger JSON prompt just because it arrived first. Representatives are chosen from direct eligible matches without merging arbitrary mixed-relation components.

Prompt families contain only retained records with the same non-empty raw prompt. Deleted exact copies do not create extra visible variants. The shared prompt is common provenance/intent; each image keeps its own identity and vector. A prompt family does **not** assert that its images look alike. Stored Voyage cosine evidence is supplementary and not a probability.

## Provider choice

`data/private-research/image-rag-canary/active-profile.json` selects `voyage_image` by default and pauses Gemini. Existing inference still requires its explicit apply/consent/budget gates. The profile does not trigger calls or authorize new spending. Historical Gemini caches remain intact. A future Gemini resumption requires a new user provider-selection decision; old retry authorizations alone do not override the pause.

The new view uses existing Voyage receipts only. Completing this selected-provider view is not completion of the old three-arm A/B, nor evidence that Voyage is objectively better. Relevance labels and threshold calibration are still missing.

## Offline revision workflow

From the workspace root:

```powershell
python -X utf8 08_AGENT_이미지_아카이브/src/revise_image_voyage_review.py
python -X utf8 08_AGENT_이미지_아카이브/src/revise_image_voyage_review.py --apply
python -X utf8 08_AGENT_이미지_아카이브/qa/check_image_rag_comparison_view.py --apply --source-run-id 2026-09-03-embedding-ab-50-v2 --comparison-dir comparison-v2 --results-file comparison-results-v2.html
```

The first command is dry-run. The second writes a new `comparison-v2/` and `comparison-results-v2.html` next to the immutable old results. It refuses to overwrite an existing revision. Preparation identity, the pinned parent ledger, completed attempts, cache model/dimension/hash and vector validity are checked before output. Missing or corrupt Voyage receipts block this path; it has no network fallback.

The UI separates logical-deletion evidence, same-prompt image variants, and visual-similarity groups. The active list and retrieval results exclude logically deleted records but include prompt variants. UI grouping and review do not grant rights or publish anything.

## Metadata boundary

No VLM metadata is generated in this revision. Later, prompt-intent/template analysis may be shared at the prompt-family level, but visible descriptions must stay image-specific. Same-prompt and visual-similarity relationships are not permission to copy factual image descriptions. See `IMAGE_RAG_METADATA_CONTRACT.md` for the draft analysis contract.
