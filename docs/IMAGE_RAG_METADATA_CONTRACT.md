# Image RAG metadata contract draft

Historical draft notice (2026-09-04): 이 문서는 초기 대표 canary 설계 이력이다. 현재 이미지 RAG 운영 순서와 유지된 서로 다른 그룹 멤버의 분석/벡터 보존 정책은 [ADR-IMAGE-RAG-001](IMAGE_RAG_PIPELINE_DECISION.md)을 따른다. 아래 과거 실행 상태와 권고를 현재 완료 범위로 해석하지 않는다.

Date: 2026-09-03
Status: design only, not executed
Scope: private image-RAG experiment metadata for deduped representatives first

## 1. Decision

Do not attach free-form VLM output directly to every archive row.

Use a staged, cacheable derived-metadata lane that:

1. keeps the canonical archive record unchanged,
2. distinguishes deterministic image evidence from model-reported interpretation,
3. runs first on dedupe representatives only,
4. reuses cached results only for identical analysis identity, including instruction/preprocessing revisions,
5. never upgrades rights, release eligibility, or provenance certainty.

This is a recommendation only. No metadata generation, model call, schema migration, or UI change was executed in this step.

## 2. Existing implementation vs recommendation

| Area | Existing local implementation | Recommendation |
|---|---|---|
| Canonical item | `image_archive_record.schema.json` stores prompt, rights, media, taxonomy, provenance | Keep as system of record; do not inject draft VLM fields into canonical rows |
| Similarity/dedup | exact hash, prompt hash, near-copy candidate, visual-family candidate | Use these groups to choose metadata representatives before any model call |
| Retention | exact duplicates can be visually collapsed in the review/result layer | Generate metadata on retained representative first; link archived duplicates by lineage |
| Evaluation cache | vector cache already keyed per request identity in image-RAG experiment | Apply the same discipline to metadata generation cache |
| Results UI | current results view works for the canary and browser QA passed | For 50 items, keep the same view philosophy; if density later becomes a problem, reduce initially expanded content rather than rewriting the renderer |

## 3. Why metadata is separate from embeddings

Embeddings answer retrieval similarity. Metadata answers browse/filter/explain.

They should not be conflated:

- embedding vectors are numeric retrieval features,
- metadata is human-readable structured interpretation,
- metadata may be inferred and fallible even when the image is stable,
- prompt text may describe intent rather than the actual visible result.

Therefore the metadata contract must separate:

- deterministic image evidence from model-reported visual interpretation,
- image evidence from prompt-intent evidence,
- model suggestion from human-approved fields.

## 4. Minimal staged pipeline

### Stage 0. Deterministic pre-pass

Use existing local signals first:

- exact file / exact pixels
- near-copy candidate
- visual-family candidate
- prompt exact / normalized prompt similarity

Outcome:

- choose one representative per `exact_file` / `exact_pixels` group,
- keep non-exact similarity groups as review-only collections,
- do not ask a model to analyze every exact duplicate separately.

### Stage 1. Representative-only metadata draft

Run metadata extraction only for:

- active retained items,
- preferred representative of each exact group,
- optional preferred representative of each near-copy or visual-family candidate when the user wants browsing aids.

Do not propagate inferred fields as ground truth to sibling items. At most, attach them as inherited suggestions with explicit lineage.
Same prompt, different image is not reusable visible metadata. Exact-prompt grouping can reduce review effort, but it cannot justify inheriting one image's visible-description draft onto another distinct image.

### Stage 2. Human review / selective acceptance

Human review may approve:

- factual visible descriptors,
- useful browse tags,
- reuse potential notes,
- prompt-intent summary when clearly marked inferred.

Human review may not implicitly approve:

- rights clearance,
- public release,
- commercial reuse,
- source authenticity beyond recorded provenance.

### Stage 3. Retrieval use

Use metadata only as auxiliary browse/filter/rerank signals:

- lexical query expansion,
- filter chips,
- explanation snippets,
- representative captions in review UI.

Do not make metadata a required blocking step for ingest.

## 5. Proposed derived JSON shape

One metadata document per analyzed image+prompt input:

```json
{
  "schema_version": "image-rag-metadata-draft-0.1",
  "record_id": "external:example",
  "style_id": "DAV490-019",
  "analysis_status": "draft",
  "analysis_scope": "representative_only",
  "identity": {
    "content_sha256": "canonical row content hash",
    "image_identity_sha256": "exact decoded image identity hash",
    "prompt_sha256": "original prompt sha or null",
    "prepared_image_sha256": "prepared raster used for model input",
    "prepared_image_preprocessing": "exif transpose; alpha on white; RGB; max side 768; PNG",
    "generation_parameters_sha256": "nullable source generation-parameter digest when available"
  },
  "cache_key": {
    "analysis_schema_version": "image-rag-metadata-draft-0.1",
    "model_family": "TBD",
    "model_revision": "TBD",
    "analysis_instruction_version": "metadata prompt or instruction template version",
    "image_identity_sha256": "required",
    "prepared_image_sha256": "required for model-visible identity",
    "prepared_image_preprocessing": "required",
    "prompt_sha256": "nullable",
    "prompt_mode": "image_only | image_plus_prompt",
    "generation_parameters_sha256": "nullable",
    "request_options_sha256": "nullable normalized request/body options digest"
  },
  "model_reported_visual": {
    "summary": {
      "value": "short visible description",
      "basis": "model_reported_visual",
      "confidence": null,
      "approved_by_human": false
    },
    "objects": [],
    "scene": [],
    "style_cues": [],
    "color_mood": [],
    "text_visible": {
      "present": false,
      "value": null,
      "basis": "model_reported_visual",
      "confidence": null,
      "approved_by_human": false
    }
  },
  "prompt_intent": {
    "summary": {
      "value": "what the source prompt appears to request",
      "basis": "inferred_from_prompt",
      "confidence": null,
      "approved_by_human": false
    },
    "requested_subjects": [],
    "requested_style": [],
    "requested_constraints": []
  },
  "category": {
    "primary": {
      "value": "interior | fashion | character | food | UI | other",
      "basis": "inferred",
      "confidence": null,
      "approved_by_human": false
    },
    "secondary": []
  },
  "core_keywords": [
    {
      "value": "red sofa",
      "basis": "inferred",
      "confidence": null,
      "approved_by_human": false
    }
  ],
  "reuse_potential": {
    "value": "high | medium | low | unknown",
    "basis": "inferred",
    "confidence": null,
    "approved_by_human": false,
    "note": "why this may be a strong reusable reference"
  },
  "provenance": {
    "source_kind": "model_draft",
    "source_model": "TBD",
    "source_model_revision": "TBD",
    "analysis_instruction_version": "metadata prompt or instruction template version",
    "generated_at": null,
    "used_prompt_text": true,
    "notes": [
      "does not change canonical rights",
      "does not assert item-level rights clearance",
      "model-reported visual text is not ground-truth observation until human review"
    ]
  },
  "review": {
    "status": "needs_review",
    "reviewer": null,
    "reviewed_at": null
  },
  "lineage": {
    "derived_from_record_id": "external:example",
    "derived_from_exact_group_id": null,
    "derived_from_visual_family_group_id": null,
    "representative_for_item_ids": []
  }
}
```

## 6. Required field rules

### 6.1 Model-reported visual block

Purpose: model-produced visual description of what appears present in the image.

Rules:

- `basis` must be `model_reported_visual` for model-produced visual claims.
- if the model is uncertain, keep the field empty instead of fabricating detail.
- OCR-like text belongs under `text_visible`; do not mix it into scene summary silently.
- no rights, authorship, or source-truth claims here.
- do not call this block `observed` unless a deterministic extractor or a human review step has separately confirmed it.

### 6.2 Prompt intent block

Purpose: what the original prompt appears to ask for.

Rules:

- derived from prompt text, not from the image alone.
- can disagree with the visible image.
- if prompt is absent, keep this block sparse rather than inventing intent.

### 6.3 Category and keywords

Purpose: cheap browse and rerank helpers.

Rules:

- keep category coarse and stable,
- keep keywords short and deduplicated,
- use `approved_by_human=false` until reviewed,
- treat these as inferred browse aids unless they come from a deterministic source,
- do not claim these are canonical taxonomy replacements.

### 6.4 Reuse potential

Purpose: personal-reference usefulness, not commercial permission.

Rules:

- allowed examples: "clear composition", "good lighting reference", "useful UI layout reference"
- forbidden examples: "safe to reuse commercially", "copyright clear", "public-ready"

## 7. Provenance, confidence, and truth labeling

Every non-empty drafted field should carry:

- `basis`: `model_reported_visual`, `inferred`, or `inferred_from_prompt`
- `confidence`: nullable model self-report only
- `approved_by_human`: boolean

Interpretation:

- `model_reported_visual` = model-generated description of visible content, not ground truth
- `inferred` = model interpretation from image content
- `inferred_from_prompt` = interpretation from prompt text

Confidence handling:

- `null` means unavailable or intentionally omitted.
- do not use placeholder `0` / `0.0`; it will be misread as a real calibrated score.
- if a provider returns confidence-like numbers, treat them as uncalibrated self-report unless separately validated.

This prevents one common corruption path: inferred browse text getting mistaken for factual archive truth.

## 8. Cache contract

Metadata generation should be cached per exact analysis identity, not per style ID alone.

Recommended cache identity:

`image_identity_sha256 + prepared_image_sha256 + prepared_image_preprocessing + prompt_sha256 + analysis_schema_version + analysis_instruction_version + model_family + model_revision + prompt_mode + generation_parameters_sha256 + request_options_sha256`

Implications:

- exact duplicate rows reuse one metadata draft,
- same image with changed prompt gets a new cache entry,
- same image/prompt with changed metadata instruction template gets a new cache entry,
- same original image with different prepared raster or preprocessing gets a new cache entry,
- same prompt with a different image must not reuse visible metadata,
- same source image with changed generation parameters may need a new cache entry when those parameters are part of the reasoning context,
- same schema with a different model revision gets a new cache entry,
- same model result should not be recomputed just because a row was re-ranked in retention.

Do not cache only by filename, path, or style ID.

## 9. Dedup and representative strategy

Apply the cheapest useful path first.

### Exact duplicates

- analyze one representative,
- record sibling IDs in `lineage.representative_for_item_ids`,
- do not run identical VLM requests for every exact duplicate.

### Exact-prompt but different image

- do not inherit visible metadata across the group,
- analyze each distinct image+prompt input when visible metadata is actually needed,
- prompt-exact evidence can help queue review work, but it is not a substitute for image-specific visual metadata.

### Near-copy candidates

- optional representative-only metadata is acceptable,
- never silently copy near-copy metadata onto every sibling,
- if copied as hints later, store as inherited suggestion, not as canonical fact.

### Visual-family candidates

- use metadata for browse explanation only,
- keep family membership and metadata acceptance separate decisions.

This preserves user intent: similar images are grouped for review, not deleted or hard-merged.

## 10. Interaction with embedding A/B

The metadata question is separate from the current embedding A/B.

Recommended experimental order:

1. finish the 50-item embedding comparison and grouping evidence,
2. choose retained representatives,
3. run metadata only on representatives if the user explicitly approves an external model path or a local model path later,
4. compare image-only metadata vs image+prompt metadata on a small judged subset,
5. only then decide whether prompt-aware metadata helps or pollutes retrieval.

Do not assume prompt-aware metadata is always better. In this corpus, prompts can reflect intent more strongly than visible outcome.

## 11. Read-only UI guidance for 50-item scaling

No UI edit is proposed here. These are only notes for a later change request.

Current result view is acceptable for the canary. For 50 items, density-related issues are anticipated, not measured in this document. The current renderer already uses lazy image loading.

Recommended future adjustments:

1. keep archived/exact/similar sections collapsed by default unless user asks otherwise,
2. keep prompts inside `<details>` and avoid pre-expanding long text,
3. keep image lazy-loading on every non-primary card,
4. render representative card first within each group, then the rest,
5. cap initially rendered cards per large group and show an explicit “more” expansion,
6. keep evidence JSON collapsed by default,
7. avoid repeating the same member set across multiple similar-group cards,
8. keep wording explicit that cosine is not probability and similarity groups are review-only.

The browser QA result for the current canary suggests the layout principle is sound. If 50-item review feels heavy later, the likely pressure points are payload density, repeated cards, and how much content starts expanded by default.

## 12. Storage boundary

Keep this metadata in a private derived lane, not in public export.

Safe placement options later:

- run-scoped experiment artifacts under `data/private-research/image-rag-canary/runs/...`
- a future archive-wide private metadata index under `data/private-research/...`
- durable DB storage only after the contract stabilizes

Do not place draft inferred metadata into:

- `data/public-export/`
- released cards
- rights-cleared public DTOs

## 13. Non-goals

This draft does not authorize or implement:

- any external API call,
- any model/vendor choice,
- any cost approval,
- any canonical schema migration,
- any rights inference,
- any release automation,
- any public deployment,
- any automatic metadata propagation across non-exact similar items.

## 14. Smallest safe next step

If the user later wants implementation, the smallest safe step is:

1. add one private run-scoped metadata draft JSON format,
2. generate it only for exact-group representatives in a bounded canary,
3. store basis/confidence/human-approval on every drafted field,
4. surface it in review UI as draft metadata, not canonical truth,
5. measure whether it actually helps top1/top3/top5 retrieval review or browse speed.

Anything broader than that should wait for judged evidence.
