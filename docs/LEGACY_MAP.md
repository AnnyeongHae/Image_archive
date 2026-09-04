# Legacy Map

## Keep As-Is

- `legacy/current_archive/index.html`
- `legacy/current_archive/dashboard.js`
- `legacy/current_archive/dashboard.css`
- `legacy/current_archive/*.json`
- `legacy/current_archive/*.sqlite3`
- `legacy/current_archive/tools/`
- `legacy/current_archive/qa/`

## New Platform Owns

- `app/`
- `data/canonical/featured_five.json`
- `data/private-research/source_locations.json`
- `media/public/featured/`
- `src/build_static_canary.py`
- `src/refresh_platform_content_registry.py`
- `src/refresh_legacy_content_registry.py`
- `src/refresh_legacy_artifact_manifest.py`
- `qa/validate_platform.py`
- `experiments/detail_page_reference_study_v3/`
- `dist/`

## Registered, Not Moved

- `Reference/`: immutable source media and derived provenance evidence
- `runtime/detail_page_canary` and `runtime/detail_page_pipeline`: independent detailed-page products
- `runtime/detail_page_reference_study_v3`: compatibility junction to the platform-owned experiment
- `runtime/visual_*_rag_canary*`: historical RAG experiments
- `Reports/*.md`: workspace report source of truth
- `scripts/*reference*`: shared workspace ingest and catalog tools

The exact machine-readable pointers and observed sizes live in
`data/private-research/source_locations.json`.

## Reason

레거시 대시보드는 이미 큰 데이터 번들과 상대경로 체계를 갖고 있어서, 안전한 래핑이 우선이다. `Reference`, shared `runtime`, `Reports`, workspace `scripts`를 다시 복사하면 정본이 둘로 갈라진다. 따라서 플랫폼 소유 파일은 새 루트에 두고, 외부 정본은 검증 가능한 포인터로 등록한다.
