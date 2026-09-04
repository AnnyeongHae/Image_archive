# Image Archive Agent Notes

## Scope
- 이미지 프롬프트 아카이브, 대표 예시 큐레이션, 포트폴리오용 정적 UI, 배포 준비 구조는 이 디렉토리를 우선 참조한다.

## Routing
- v2 운영 API/클라우드 snapshot/소스 중립 입고 시작점: `platform/v2/README.md`. 기존 데이터/레거시 위치를 이동하지 않는 additive v2다.
- v2 입고→4단계 관리자 연결: `platform/v2/local/review_bridge.py`, 계약 `docs/IMAGE_ARCHIVE_V2_INTAKE_REVIEW.md`. 인증된 import receipt SHA와 전체 선택 미디어 hash를 고정하고, 이미지 캐시 누락을 빈 유사 후보/승인 완료로 해석하지 않는다. 새 run에 자동 승인 seed를 넣지 않는다.
- 대표 예시 검토 시작점: `app/index.html`
- 기존 전체 검색/아카이브: `legacy/current_archive/index.html`
- 대표 예시 원장: `data/canonical/featured_five.json`
- 전체 내부 정본(기본 18,792건 + 승인된 OpenNana 동적 레인): `data/canonical/archive_records.jsonl`
- 권리 필터링 정적 인덱스: `data/public-export/catalog-index.json`
- 외부 정본/실험 위치 지도: `data/private-research/source_locations.json`
- 대표 예시 공개 미디어: `media/public/featured/`
- 정적 빌드 산출물: `dist/`
- 자체 레퍼런스 실험: `experiments/` (`dist/` 포함 금지)
- 출처 운영 관리자: `legacy/current_archive/source-admin.html`
- 사람 승인 큐: `legacy/current_archive/approval-requests.html`
- 중복 그룹 검토: `legacy/current_archive/duplicate-review.html`
- 중복 파생 DB/요약: `data/private-research/duplicate-analysis/current/`
- 중복 인덱스 빌더: `src/build_duplicate_index.py` (기본 dry-run, `--apply` 필요)
- 모델 라우팅: `src/model_routing_policy.py` (`none → Luna → Terra → Sol final-only`)
- 원격 미디어 로컬 캐시: `src/remote_media_canary.py` (10건 canary + 사용자 승인 전수 private cache)
- 현대 포맷 실측: `src/benchmark_modern_formats.mjs` (Sharp 경로를 명시, AVIF/WebP/JPEG)
- OpenNana 수집 정본/상태: `data/private-research/opennana/`
- OpenNana canary 실행기: `src/opennana/run_pipeline.py`
- OpenNana 전진 전용 기준선/일일 실행기: `src/opennana/run_daily_sync.py`
- OpenNana 수동 백로그 복구 실행기: `src/opennana/run_backlog_sync.py`
- GitHub 공개 소스 canary: `src/github_sources/collect_public_repo.py`와 `.github/workflows/github-source-canary.yml`
- 이미지 RAG 운영 순서와 임베딩 역할 결정: [ADR-IMAGE-RAG-001](docs/IMAGE_RAG_PIPELINE_DECISION.md). 신규 입고·중복·분석·검색 작업 전에 읽는다.

## Image RAG pipeline — human accepted 2026-09-04

이 규칙은 이미지 RAG 입고·분석·검색 레인에 적용한다. 아래 생성 레퍼런스 선택 Workflow나 다른 수집 레인의 권리·승인 게이트를 대체하지 않는다.

1. 입고 시 출처·권리 안내를 기록하고 파일/전체 디코딩 픽셀/원문 프롬프트 해시를 계산한다. 권리가 미확인이면 그대로 미확인 상태를 남긴다.
2. 확정 중복을 노출·후속 추론 대상에서 제외하되 원문·출처·대표 연결을 보존한다. 현재 보수적 기계 규칙은 `exact_file OR (exact_pixels AND nonblank_prompt_exact)`다. `exact_file`은 프롬프트가 달라도 확정 일치다. exact_file이 아닌 경우의 프롬프트 단독 일치·픽셀만 일치, 또는 높은 임베딩 유사도는 그 자체로 자동 삭제 승인이 아니다. 불가역 파일 삭제는 별도 명시 승인이 필요하다.
3. 유지 후보의 이미지-only 임베딩으로 유사 후보를 찾는다. 캐시 검증을 먼저 하고 미변경 이미지를 재호출하지 않는다. 대표만 저장하지 않고 서로 다른 그룹 멤버의 벡터도 유지한다.
4. 사람이 동일 제외 / 형식별 그룹 / 개별 유지를 판단한다. 같은 활용 목적만으로 시각 그룹을 합치지 않는다. 메타데이터 생성이나 높은 점수로 사람 판정을 대체하지 않는다.
5. 유지 승인된 각 이미지와 정확한 원문을 Luna로 분석한다. 시각 관찰·프롬프트 의도·활용 제안·사람 메모의 출처를 구분한다. 기본 한 turn 3개, 최대 5개이며 정확도 A/B 승자로 표현하지 않는다. 메모는 선택 사항이다.
6. 활용 중심 텍스트를 별도로 임베딩한다. 현행 compact 입력은 선별 메타데이터 + 분석된 프롬프트 의도/고정 조건/슬롯 + 메모이며, 원문 전체는 DB에 보존하지만 이 벡터 입력에는 그대로 넣지 않는다. 두 표현을 같은 입력이라고 보고하지 않는다. QA 제외로 입력이 비면 임베딩을 보류한다.

- 이미지 벡터는 시각 유사 후보용, 텍스트 벡터는 활용 검색용으로 분리한다. 현재 저장 질의 검색은 텍스트 벡터만 사용한다. 다른 모델의 벡터를 직접 비교/평균하거나 joint 임베딩·RRF가 이미 적용됐다고 주장하지 않는다.
- 검색은 그룹 멤버를 후보로 유지하되 top-k는 서로 다른 그룹으로 채우고, 그룹별 사람 지정 대표 한 장과 접힌 멤버를 표시한다.
- 데이터 인입, 이미지 승인, 분석 생성/기술 검증, 메타데이터 사람 검수, 임베딩 완료, 실제 서비스 연결, 공개 승인을 각각 구분해 보고한다.
- 모델 호출 전 대상·외부 전송 범위·예산을 확인하고, 호출 후 실제 사용량·캐시·미완료/불확실 상태를 남긴다. 캐시 입력은 전체 입력의 부분집합, reasoning은 출력의 부분집합이다. 서로 다른 제공자·실행 범위의 토큰을 과금 총액이나 이미지별 정확 비용으로 합산/균등분배하지 않는다.
- Luna의 긴 세션을 무제한 재사용하지 않는다. 고정 prefix 재사용과 누적 대화 이력 비용을 구분하고, 문맥·비캐시 입력·출력·재시도 비용이 증가하면 독립 작업 세션으로 전환한다. 높은 캐시 적중률만으로 최저 비용이라고 주장하지 않는다.
- 이 설계 승인은 새 API 호출·전량 재임베딩·권리 승격·배포 승인이 아니다. 원문 전체 포함, 통합 임베딩 전환, 후보/그룹 정책 변경은 영향·예산과 사람 평가를 제시하고 별도 결정한다.

## Safety
- v2 운영 코드는 `platform/v2/`, 신규 비공개 DB 계약은 `db/v2/`를 사용한다. `platform/__init__.py`를 만들지 않는다(Python 표준 모듈명 충돌).
- 2026-09-04 사용자는 백업 후 기존 `AnnyeongHae/Image_archive` 저장소 갱신·배포 구현을 요청했다. 이는 private 원문/이미지/DB를 공개 Git에 올리거나 새로운 항목 권리를 승인한 것이 아니다. `.gitignore`와 staged 파일 허용/secret 검사를 모두 통과해야 한다.
- 새 snapshot은 기존 379 이미지/377 텍스트 캐시를 검증해 재사용한다. Qdrant는 임베딩 API가 아니다. 컬렉션 업로드를 새 모델 호출로 보고하지 않는다.
- v2 API는 공개 갤러리와 분리한다. Access 소유자 인증과 scoped bearer 인증은 서로 대체하지 않는다. 새 질의 임베딩은 기본 비활성이고 전역 일별 예산 예약·실제 사용량 기록 후에만 호출한다. 실패/불확실 요청은 자동 재시도하지 않는다.
- 공개 GitHub Actions artifact/cache에 비공개 인계 원문을 평문으로 올리지 않는다. 본문은 소유자 public key로 봉인하고 private key는 로컬에만 둔다. 암호화는 송신자 진위 확인이 아니므로 수신 시 expected repo/workflow/run을 검증한다.
- 배포의 정확한 artifact hash·target·권리·사람 승인 게이트는 계속 적용한다. 코드 작성·오프라인 테스트·dry-run·원격 적재·실서버 배포 성공을 구분한다.
- `legacy/current_archive/`는 레거시 원장으로 취급한다.
- 외부 출처 이미지는 자동으로 공개 자산으로 승격하지 않는다.
- `media/public/featured/`에는 직접 큐레이션한 예시 5개만 둔다.
- 모든 대표 예시는 `release_eligible=false` 전제의 내부 포트폴리오/연구용이다.
- 일반 외부 수집 canary는 무료 상세 최대 20건, 1 req/s, 동시성 1을 넘기지 않는다.
- OpenNana 일일 동기화는 과거 상세 백필을 하지 않는다. 최초 1회 공개·무료 목록의 ID·목록 버전만 기준선으로 기록하고, 이후 24시간마다 기준선에 없던 신규 ID 또는 실제 목록 메타데이터 변경분의 상세만 1 req/s·동시성 1로 수집한다. 동일 출처의 미변경 버전과 exact prompt 중복은 승인 큐에서 제외하며 near/remix는 삭제하지 않고 관계 후보로 남긴다.
- 사용자가 2026-09-02 허용한 과거 미처리 OpenNana 상세 복구는 일일 동기화와 분리된 수동 실행에 한한다. `run_backlog_sync.py --fetch --apply --max-details N`처럼 총량 상한을 매번 명시하며, N은 100을 넘을 수 있지만 내부 완료 체크포인트 배치는 최대 100건이다. 이 레인은 private 승인 큐까지만 갱신하고 자동 스케줄, 정본 승격, 공개 반영을 하지 않는다.
- 유료·인증·paywall 뒤의 프롬프트 본문이나 미디어를 우회 수집하지 않는다.
- 사용자가 2026-08-31 승인한 범위에서는 exact allowlist의 직접 공개 HTTPS 이미지 URL을 중복 제거한 뒤 전부 로컬 private cache에 저장할 수 있다.
- 이 로컬 다운로드 승인은 공개 배포, R2 승격, 상업 이용 또는 항목별 권리 확인을 의미하지 않는다.
- 체크박스 `승인`은 `canonicalization_pending` 결정일 뿐, 권리·상업 사용·공개 릴리스 승인이 아니다.
- 중복·유사도 결과는 관계 후보일 뿐이며 삭제, 병합, 하드링크, 권리 승격을 자동 실행하지 않는다.
- AVIF/WebP/JPEG/PNG 전달본은 파생 미리보기이며 private 원본을 교체하거나 삭제하지 않는다.
- 사람·브랜드 존재만으로 Sol을 호출하지 않는다. Sol은 lower-cost lane 이후의 최종 release 예외 판정에만 쓴다.
- 원격 fetch 성공은 수집·재배포 권리 승인이 아니다. query/signed redirect URL은 artifact에 평문 보존하지 않는다.
- 전수 로컬 캐시는 URL hash receipt와 cache index로 재개하며, 완료된 URL을 다시 받지 않는다. 기본 전체 동시성 4(최대 8), 동일 host 동시성 1과 최소 1초 간격, 15 MiB/파일, 80 MP decode, D: 여유 공간 5 GiB 하한을 유지한다.
- 공개 v1 이미지 포맷은 WebP + JPEG/PNG fallback으로 고정한다. AVIF는 비공개 벤치마크로만 유지하고 공개 bundle에 생성하지 않는다.

## Workflow
1. 요청을 제품/카테고리, 페이지 역할, 구도, 매체, 조명, 카피 여백, 제품 충실도 제약으로 구조화한다.
2. `source_locations.json`과 아카이브에서 서로 실질적으로 다른 레퍼런스 5개를 찾는다.
3. 이미지 + Style ID + 짧은 선정 이유를 먼저 보여 준다.
4. 사람의 단일 또는 복수 선택을 기다린다. 선택된 속성을 명시한 뒤에만 생성 파이프라인으로 넘긴다.
5. 후보와 메타데이터는 `data/canonical/featured_five.json`에서 관리한다.
6. `src/build_static_canary.py --apply`로 `app/data/featured-five.js`와 `dist/`를 생성한다.
7. `qa/validate_platform.py --write-report`로 구조와 경로를 검증한다.

## Source automation workflow
1. `src/opennana/run_pipeline.py`는 인자 없이 실행하면 오프라인 dry-run 계획만 출력한다.
2. 네트워크 실행에는 `--fetch --apply`가 모두 필요하고 `--max-details`는 20 이하만 허용한다.
3. 수집 결과는 `raw/ → staging/ → review_queue/`로 이동하며 정본 JSONL을 직접 수정하지 않는다.
4. 승인 페이지는 결정 초안 JSON만 만든다. `apply_decisions.py`도 기본 dry-run이다.
5. 사람 검토 후 `--apply`해도 `canonicalization_pending`까지만 생성한다.
6. robots/Content-Signal 변경, 403/429, 필드 드리프트는 실패로 처리하고 자동 승인하지 않는다.
7. 전진 전용 운영을 시작할 때는 `src/opennana/run_daily_sync.py --fetch --apply --baseline-only`를 정확히 1회 실행한다. 이 명령은 목록 ID·버전만 기록하며 상세, raw/staging, 승인 큐, 정본을 변경하지 않는다.
8. 기준선이 확인된 뒤에만 `src/opennana/run_daily_sync.py --all-free --fetch --apply`를 사용한다. 공개·무료 목록은 신규 감지를 위해 확인하되, 상세 본문은 기준선 이후 신규·변경분만 가져오고 처리 완료 배치에 한해 checkpoint를 전진시킨다. 기준선이 없으면 fail closed한다.
9. 일일 동기화는 유료·잠금·인증 본문, 과거 상세 백필, 자동 승인, 정본 승격, 공개 반영을 금지한다. 한 실행의 실패 항목은 checkpoint를 전진시키지 않고 다음 24시간 실행에서 재시도한다.
10. 과거에 목록 버전만 관측하고 상세를 가져오지 않은 무료 항목은 자동 스케줄이 아닌 `src/opennana/run_backlog_sync.py --fetch --apply --max-details N`으로만 복구한다. 상한 N은 실행마다 명시하고, 내부 배치는 최대 100건씩 완료 체크포인트를 남긴다. 실패 시 마지막 완료 배치 이후부터 재개하며 private 승인 큐 외 정본·공개 파일은 변경하지 않는다.

## GitHub public-source canary workflow
1. `src/github_sources/collect_public_repo.py`는 인자 없이 오프라인 fixture만 검증한다.
2. 공개 API read-only canary에는 `--fetch --repo owner/name`가 모두 필요하다. 영구 private artifact는 추가로 `--apply`를 명시해야 한다.
3. exact allowlist 밖의 저장소, 인증·유료 콘텐츠, Markdown의 외부 링크, 바이너리 blob은 받지 않는다.
4. 저장소 라이선스는 관측 신호일 뿐 개별 프롬프트·이미지 공개 권리 승인이 아니다. 결과는 `data/private-research/github-sources/`에만 둔다.
5. `.github/workflows/github-source-canary.yml`은 오프라인 계약 검증 전용이다. 원격 `workflow_dispatch` 성공 전에는 schedule, contents write, canonical promotion을 추가하지 않는다.

## Remote media local cache workflow
1. 인자 없는 `src/remote_media_canary.py`는 inventory/dry-run이며 네트워크나 파일 쓰기를 하지 않는다.
2. 기존 bounded 확인은 `--fetch --apply --limit N`으로 실행하며 `N`은 1~10이다.
3. 사용자가 승인한 전수 로컬 캐시는 정확히 `--all --fetch --apply` 세 인자가 모두 있을 때만 실행한다.
4. 대상은 canonical inventory에서 중복 제거한 direct-public HTTPS URL과 exact allowlist host다. 인증, 로그인, paywall, private IP 또는 우회가 필요한 주소는 대상이 아니다.
5. 성공 receipt와 content-addressed cache index를 checkpoint로 사용한다. 중단되거나 디스크 여유 공간이 5 GiB 아래로 내려가면 안전하게 멈추고 같은 명령으로 재개한다.
6. 전체 동시성은 기본 4(최대 8)지만 동일 host는 동시성 1과 최소 1초 간격을 유지한다. 429/5xx는 Retry-After 또는 지수 backoff로 최대 5회 재시도하고 60초에서 cap한다.
7. 수집된 파일은 `data/private-research/remote-media-canary/` 아래에만 둔다. `dist/`, `media/public/`, R2 또는 공개 export로 자동 복사하지 않는다.
8. 항목별 출처 URL, fetch 시점, content hash, MIME, 크기와 실패 원인을 기록한다. 로컬 보존 여부와 공개·상업 이용 권리는 별도 필드로 관리한다.

## Generated files
- `app/data/featured-five.js`와 `dist/`는 직접 편집하지 않는다.
- canonical JSON 또는 `app/` 소스를 고친 뒤 다시 빌드한다.
- legacy 도구의 경로는 `legacy/current_archive/tools/archive_paths.py`를 사용한다.
- legacy mutating collector는 경로 복구 여부와 별개로, 도구별 write-mode canary를 검토하기 전까지 실행하지 않는다.
- legacy 해시 스냅샷은 `src/refresh_legacy_content_registry.py`와 `src/refresh_legacy_artifact_manifest.py`를 dry-run한 뒤 `--apply`한다.
- 전체 정본과 공개 shard는 `src/build_canonical_archive.py`를 dry-run한 뒤 `--apply`하고, `qa/validate_canonical_archive.py`로 검증한다.
- 저장소 라이선스를 개별 프롬프트·이미지의 상용 사용 허가로 승격하지 않는다. 공개 원문/미디어는 항목 권리와 release gate를 모두 통과해야 한다.
- 명시적인 사용자 승인과 release gate 통과 없이는 배포하지 않는다.
