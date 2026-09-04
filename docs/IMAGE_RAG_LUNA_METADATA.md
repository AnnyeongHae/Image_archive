# Image RAG Luna metadata plan

Date: 2026-09-03
Status: the legacy planner below is preparation-only; the separate executable canary is described at the end
Model label: `gpt-5.6-luna`

This lane prepares private metadata tasks. It does not call a model, read secrets, price an API, or change canonical archive rows.

## What exists now

- `src/image_rag_eval/luna_metadata.py`
  - validates a prepared image-RAG source run,
  - prefers `comparison-v1/retention.json` representatives when available,
  - falls back to exact-group representatives if retention is absent,
  - builds deterministic cache identities from image, prompt, preprocessing, schema, and instruction versions,
  - writes only private planning artifacts when `apply=True`.
- `src/prepare_luna_metadata.py`
  - CLI wrapper, dry-run by default.

## Output boundary

Private artifacts are written under:

- `data/private-research/image-rag-canary/runs/<source-run-id>/luna-metadata-v2/plan.json`
- `.../tasks.json`
- `.../drafts.json`
- `.../receipt.json`

No public export, no release change, no metadata injection into canonical rows.

## CLI

Dry run:

```powershell
python 08_AGENT_이미지_아카이브/src/prepare_luna_metadata.py --source-run-id <run-id>
```

Write private planning artifacts:

```powershell
python 08_AGENT_이미지_아카이브/src/prepare_luna_metadata.py --source-run-id <run-id> --apply
```

Optional prompt mode:

- `image_plus_prompt` (default)
- `image_only`

## Contract

Each planned task keeps these boundaries explicit:

- image-specific visible metadata is per distinct image,
- same-prompt families may share prompt-intent review context,
- same-prompt families must not inherit visible facts across distinct images,
- confidence fields are nullable and treated as uncalibrated model self-report later,
- every candidate output remains `needs_review`,
- rights, source authenticity, and release approval remain separate.

## Cache identity

The planner keys future reuse on:

- source image SHA-256,
- prepared image SHA-256,
- prepared image preprocessing string,
- prompt SHA-256 and normalized prompt SHA-256 when prompt mode includes text,
- model family,
- metadata schema version and schema SHA-256,
- analysis instruction version,
- generation parameter digest when present.

Changing any of those inputs creates a new cache identity.

## Planned metadata shape

`drafts.json` emits per-image candidate records matching `00_CORE/schemas/image_archive_luna_metadata.schema.json`.

The draft separates:

- `image_specific`
  - model-reported visual summary, subject, composition, style, colors, visible text
- `prompt_intent`
  - prompt-derived requested subjects, requested style, requested constraints
- `browse_metadata`
  - category, keywords, use cases, extension hypotheses, reuse potential
- `factuality`
  - explicit non-ground-truth and human-review flags

## Non-goals

This planner does not implement:

- actual Luna execution,
- batching against a live API,
- token billing or pricing claims,
- metadata acceptance into canonical search fields,
- UI rendering changes,
- rights inference.

## Recommended next execution step

For a future 200-image run:

1. prepare the source run first,
2. review retention representatives,
3. run this planner,
4. execute only the planned representative tasks in bounded private batches,
5. review metadata before any archive integration.

## Executable image-first canary (2026-09-04)

The preceding planner and its version 0.2 contracts remain unchanged because existing approval handoffs pin their hashes. Actual analysis now has a separate contract and private lane:

- `../00_CORE/schemas/image_luna_analysis_result.schema.json`
- `../00_CORE/templates/image_luna_analysis.instructions.md`
- `src/prepare_image_luna_canary.py`: latest committed, approved images only; image and exact original prompt are hash-bound and kept separate.
- `src/import_image_luna_results.py`: deterministic validation of the initial complete ten-image canary; it does not invoke a model.
- `src/build_image_luna_review.py`: read-only, image-linked HTML from an immutable validated import.
- `data/private-research/image-rag-admin/luna-analysis/2026-09-03-luna-analysis-10-v1/`: frozen inputs, visual drafts, raw model outputs, immutable imports and review artifacts.

Luna must open each actual prepared image, save its visual-only draft, and only then open the prompt context. The final visual fields must match that draft. The importer verifies content equality, not tool execution or chronology; those claims require the separate orchestrator execution receipt. No visual inference is delegated to an embedding provider. Codex account usage still applies.

The new result separates visible content, visual design attributes, bounded OCR, suggested search keywords, prompt intent, and evidence-linked reuse ideas. Human memos are neither model input nor an output overwrite target. An unavailable reference or unreadable detail belongs in `not_assessable`, not a confirmed mismatch. QA findings remain sidecar warnings, never silent edits to raw model output.

All three commands default to dry-run. Example for this frozen canary:

```powershell
python 08_AGENT_이미지_아카이브/src/prepare_image_luna_canary.py --source-run-id 2026-09-03-incremental-review-500-v1 --analysis-run-id 2026-09-03-luna-analysis-10-v1
python 08_AGENT_이미지_아카이브/src/import_image_luna_results.py --analysis-run-id 2026-09-03-luna-analysis-10-v1
python 08_AGENT_이미지_아카이브/src/build_image_luna_review.py --analysis-run-id 2026-09-03-luna-analysis-10-v1
```

Use `--apply` explicitly to create private artifacts. The model is a separately assigned `gpt-5.6-luna` Codex worker, not an automatic action of these commands. A model result passing import is still a `needs_review` candidate, not metadata acceptance. No canonical search write, human-approval change, embedding call, Qdrant write, image deletion or public deployment is part of this lane. Distinct images in a group continue to require separate visual analysis.

Scale only after reviewing this canary. Retain completed results for unchanged task fingerprints; use a new versioned task when image, prompt or contract changes. Do not blindly retry all images or silently replace an already imported result. The initial importer intentionally accepts exactly ten tasks; a later batch needs a separately tested bounded-batch contract.

## Reuse-oriented v2 expansion and token receipt (2026-09-04)

The next ten-image canary extends the reviewed population to twenty without re-running the unchanged first ten. Its separate v2 contract is:

- `../00_CORE/schemas/image_luna_reuse_analysis_result.schema.json`
- `../00_CORE/templates/image_luna_reuse_analysis.instructions.md`
- `src/prepare_image_luna_reuse_canary.py`
- `src/import_image_luna_reuse_results.py`
- `src/measure_luna_token_usage.py`
- `src/build_image_luna_reuse_review.py`

The contract keeps four roles distinct:

1. The image supplies evidence for visible style, background, layout, subjects, and editability.
2. The original prompt supplies intended purpose, fixed rules, and replaceable slots. Prompt-only facts cannot become supported visual facts.
3. The pinned reuse taxonomy supplies selectable task IDs, definitions, and exclusions.
4. The final interpretation selects bounded tasks and records fit, visual evidence, required adaptations, and constraints.

All v2 candidates remain private and `needs_review`. Group representatives are analyzed as distinct images; member expansion, rights approval, metadata approval, search indexing, and public release remain separate decisions.

Every future model-analysis run must write a hash-bound `token-usage-receipt.json`. It records input including cached input, the cached subset, calculated uncached input, output including reasoning, the reasoning subset, total tokens, model, run identity, and source-log hashes. Cached input and reasoning are subsets and must not be added a second time. Local Codex telemetry is execution evidence, not a provider invoice, so `actual_billed_tokens` and `actual_billed_cost` remain `null` unless an authoritative billing record is available.

The v2 measurement canary used one dedicated Luna session per image to obtain exact per-image attribution. This was deliberately diagnostic, not the new production default: it repeated fixed session and tool context and materially increased cached input. The next default is one bounded session for 5–10 images, with exact run-level token measurement and deterministic per-image result validation. If failure isolation is more important, use two five-image batches. Do not fabricate per-image token attribution inside a shared session.

## Reuse-analysis v2 and token receipts (2026-09-04)

The next private lane adds “how could this image be used later?” as a first-class output while preserving the same evidence boundary:

- `../00_CORE/schemas/image_luna_reuse_analysis_result.schema.json`
- `../00_CORE/templates/image_luna_reuse_analysis.instructions.md`
- `src/prepare_image_luna_reuse_canary.py`
- `src/import_image_luna_reuse_results.py`
- `src/build_image_luna_reuse_review.py`
- `src/measure_luna_token_usage.py`

This v2 contract fixes four roles in the output:

- image: visible style, background, layout, main subject, editable space, and other directly observed evidence
- original prompt: intended purpose, fixed rules, and replaceable slots
- usage taxonomy: one primary normalized task ID, up to two secondary IDs, each with exclusions
- final interpretation: why it fits, what must change, and what limits remain

Human memos are still out of model scope. Rights, release approval, search indexing, and embedding writes remain separate. Imported results stay `needs_review`.

Token measurement is now a required sidecar for each real analysis run. The receipt:

- reads local Codex execution telemetry only,
- records `input_tokens_including_cached`, `cached_input_tokens`, calculated uncached input, `output_tokens_including_reasoning`, and total tokens,
- never adds cached or reasoning subsets twice,
- does not claim provider-billed tokens or monetary cost unless those are directly observed.

The one-session-per-image partition was useful for exact per-image attribution in the 10-image v2 canary, but it materially increased repeated fixed context. Future default should be one bounded multi-image session per 5 to 10 images, with one run-level token receipt and deterministic result validation. Use isolated per-image sessions only when attribution evidence is more important than efficiency.
