# Compact metadata v3

Current accepted pipeline and embedding-input distinction: [ADR-IMAGE-RAG-001](IMAGE_RAG_PIPELINE_DECISION.md). 중복/사람 확인 이후 이미지와 원문을 분석하고 활용 텍스트를 임베딩한다. 아래 배치 A/B 대기 문구와 초기 준비 상태는 당시 이력이며 현재 완료 범위는 최신 실행 보고서를 따른다.

Current strategy and evidence: `../../Reports/2026-09-04-04_이미지메타데이터_정규화_캐시와_배치평가.md`.

This supersedes the historical 5–10-image recommendation: provisional default is 3 images, maximum 5. A/B execution awaits hypothesis confirmation. No accuracy winner is established.

- Shared schema: `../../00_CORE/schemas/image_luna_compact_result.schema.json`
- Shared instruction: `../../00_CORE/templates/image_luna_compact.instructions.md`
- Prepare a stable prefix: `python src/prepare_luna_compact_contract.py` (dry-run; explicit `--apply` writes private artifacts).
- Compact validation: `src/image_rag_eval/luna_compact.py`.
- Private v1/v2 candidate DB: `python src/build_luna_metadata_store.py` (dry-run; `--apply` creates an immutable snapshot).
- Batch usage receipt: `src/measure_luna_batch_tokens.py` accepts explicit session bindings of 1–5 images; per-image tokens are null for shared sessions.
- Existing v1/v2 contracts and immutable analysis outputs stay unchanged.

Normalized IDs and relationships belong in relational tables; freeform fields and immutable raw outputs remain JSON. Human approval, rights, public visibility, and embeddings are not granted by candidate storage.
