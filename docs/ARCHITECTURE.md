# Architecture

이미지 RAG 입고·중복·분석·검색 레인의 현재 결정은 [ADR-IMAGE-RAG-001](IMAGE_RAG_PIPELINE_DECISION.md)을 따른다. 아래 레거시 이관/배포 구조와 별개로, 운영 순서·두 벡터의 역할·미구현 검색 결합 경계를 명시한다.

## Goal

기존에 분산된 이미지 프롬프트 아카이브를 하나의 플랫폼 루트로 재정리하되, 레거시 동작은 깨지 않게 유지한다.

## Layers

1. `legacy/current_archive/`
   레거시 수집물, 대시보드, SQLite, JSON, QA 산출물의 원장이다.
2. `data/canonical/`
   새 플랫폼이 직접 소비하는 작고 명시적인 정본 데이터다.
3. `media/public/featured/`
   포트폴리오 첫 화면에서 바로 보여줄 대표 예시의 공개 후보 복사본이다.
4. `app/`
   `file://` 환경에서도 동작하는 정적 프런트엔드 소스다.
5. `src/`
   데이터 변환과 정적 빌드를 담당하는 표준 라이브러리 기반 스크립트다.
6. `dist/`
   추후 배포 가능한 형태를 흉내 내는 canary 산출물이다. 아직 배포하지 않는다.
7. `data/private-research/duplicate-analysis/`
   canonical을 변경하지 않고 exact prompt/media hash와 제한적 perceptual candidate를 저장하는 비공개 SQLite 파생 인덱스다.
8. `media/derived/duplicate-review/`
   콘텐츠 주소형 WebP 비교 썸네일이다. 원본 대체물이 아니며 공개 빌드에 포함하지 않는다.

## Why This Shape

- 레거시 내부는 상대경로 결합이 강하다.
- 지금 단계에서 레거시를 추가로 쪼개면 회귀 위험이 크다.
- 따라서 우선은 새 플랫폼 레이어를 밖에 두고, 필요한 자산만 명시적으로 승격 복사한다.

## User Flow

1. 대표 예시 5개를 눈으로 본다.
2. `Reference Style ID`를 선택한다.
3. 마음에 들면 전체 아카이브로 확장 탐색한다.
4. 선택이 끝난 뒤 별도 생성 파이프라인으로 넘어간다.

## Non-Goals

- 이 루트에서 직접 이미지 생성 실행
- 전체 2GB+ 레거시를 지금 당장 세분 모듈로 분해
- 즉시 Cloudflare 배포

## Source-of-truth order

1. 역사적 프롬프트·이미지: `legacy/current_archive/`
2. 사람의 5개 큐레이션 결정: `data/canonical/featured_five.json`
3. 로컬 UI 투영: `app/data/featured-five.js`
4. 정적 빌드: `dist/` — 언제든 다시 만들 수 있는 파생물

## Future Cloudflare shape

- 정적 프런트: `dist/`를 Workers Static Assets로 전달하고 `/api/*`와 한 배포 단위로 둔다.
- 승인된 미디어: 공개 승인 뒤 R2와 custom domain으로 분리한다.
- 검색 메타데이터: Neon을 정본으로 두고 권리 필터를 통과한 cursor API/read projection만 공개한다.
- 관리자 결정: 로컬 `/api/review/v1/*` 계약을 Worker에 옮기고, queue·preview grant·decision
  ledger·private approved lane을 Neon transaction으로 처리한다. Workers 연결은 Hyperdrive
  canary로 검증하고, 접근은 Cloudflare Access·API 권한 검사·same-origin CSRF로 제한한다.
- R2는 항목 단위 권리 검토를 통과한 미디어만 받는다. 내부 참고 승인만으로
  외부 이미지를 public prefix로 승격하지 않는다. private original 보관도 source policy와
  보존 근거가 있는 항목만 허용한다.
- private research corpus, raw source capture, SQLite는 공개 빌드 밖에 둔다.
- 정적 asset bundle에는 작은 HTML/CSS/JS만 두고, 공개 승인된 WebP 파생본은 R2 object key로 연결한다.
- 원본은 보존하며 `<picture>` fallback, 명시적 크기, 첫 화면 1장 eager, 나머지 lazy/async 정책을 유지한다.

현재 Cloudflare 리소스는 만들거나 수정하지 않았다.
