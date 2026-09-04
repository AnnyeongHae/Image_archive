# Migration Record

## Completed Move

- 기존 아카이브 위치:
  `Reports/2026-08-25-01_상세페이지_프롬프트전수조사`
- 새 위치:
  `08_AGENT_이미지_아카이브/legacy/current_archive`

## Compatibility

- 기존 경로에는 Windows junction을 남겨 기존 링크와 로컬 화면을 깨지 않게 유지했다.
- 이전 파일 수와 이동 후 파일 수는 동일하게 검증했다.
- 핵심 파일 해시도 이동 전후 동일성을 확인했다.

## Experiment Move

- 기존 위치: `runtime/detail_page_reference_study_v3`
- 새 위치: `08_AGENT_이미지_아카이브/experiments/detail_page_reference_study_v3`
- 44개 파일과 75,407,411 bytes, 전 파일 SHA-256 일치
- 기존 runtime 위치에는 Windows junction 유지
- `scripts/verify_detail_page_image_candidates.py` 재검증: 24 candidates, 12 selected, PASS
- 증거: `data/canonical/experiment_migration_manifest.json`

## Current Refactor Stage

- 1단계: 레거시 안전 이동 완료
- 2단계: 새 루트 `app/data/media/docs/qa/deploy/dist` 계층 추가
- 3단계: 대표 예시 5개 전용 포트폴리오 canary 추가
- 4단계: 추후 선택 기반 상세 생성 플로우 연결 예정

## Deferred Work

- 레거시 쓰기 도구별 write-mode canary
- 전체 대시보드와 새 플랫폼의 데이터 어댑터 통합
- 실제 배포용 asset policy와 캐시 전략 확정

## Path coupling repair

기존 legacy 도구의 `Path(__file__).resolve().parents[3]` 결합은
`legacy/current_archive/tools/archive_paths.py`로 교체했다. 기본값은 현재 파일에서
`00_CORE`와 `Reports`를 가진 워크스페이스를 탐색하고, 자동 탐색을 원하지 않을 때는
`MARKETER_WORKSPACE_ROOT`와 `IMAGE_ARCHIVE_LEGACY_ROOT` 환경 변수로 명시할 수 있다.

이 수정 뒤 `validate_catalog.py`, `validate_external_catalog.py`,
`validate_social_prompt_archive.py`를 새 경로에서 통과시켰다. 다만 경로가 고쳐졌다는
사실은 쓰기 작업 승인과 같지 않다. 수집·등록·재빌드 같은 mutating collector는
도구별 dry-run 및 작은 write-mode canary를 검토하기 전까지 계속 비활성화한다.

이동이나 내부 도구 수정 뒤에는 아래 순서로 레거시 증거 해시만 갱신한다.

```powershell
python src/refresh_legacy_content_registry.py
python src/refresh_legacy_content_registry.py --apply
python src/refresh_legacy_artifact_manifest.py
python src/refresh_legacy_artifact_manifest.py --apply
```

자세한 이동 시점 수량과 해시는 `data/canonical/migration_manifest.json`에 있다.
