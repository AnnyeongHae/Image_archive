# Image Archive Agent Notes

## Scope
- 이미지 프롬프트 아카이브, 대표 예시 큐레이션, 포트폴리오용 정적 UI, 배포 준비 구조는 이 디렉토리를 우선 참조한다.

## Routing
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
- GitHub 공개 소스 canary: `src/github_sources/collect_public_repo.py`와 `.github/workflows/github-source-canary.yml`

## Safety
- `legacy/current_archive/`는 레거시 원장으로 취급한다.
- 외부 출처 이미지는 자동으로 공개 자산으로 승격하지 않는다.
- `media/public/featured/`에는 직접 큐레이션한 예시 5개만 둔다.
- 모든 대표 예시는 `release_eligible=false` 전제의 내부 포트폴리오/연구용이다.
- 일반 외부 수집 canary는 무료 상세 최대 20건, 1 req/s, 동시성 1을 넘기지 않는다.
- 사용자가 2026-09-01 최종 지정한 OpenNana 일일 동기화는 과거 상세 백필을 하지 않는다. 최초 1회 공개·무료 목록의 ID·목록 버전만 기준선으로 기록하고, 이후 24시간마다 기준선에 없던 신규 ID 또는 실제 목록 메타데이터 변경분의 상세만 1 req/s·동시성 1로 수집한다. 동일 출처의 미변경 버전과 exact prompt 중복은 승인 큐에서 제외하며 near/remix는 삭제하지 않고 관계 후보로 남긴다.
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
