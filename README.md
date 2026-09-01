# Image Archive Platform

이 디렉토리는 이미지 프롬프트 아카이브 플랫폼의 새 루트다. 기존 대시보드와 수집 산출물은 `legacy/current_archive/`에 그대로 보존하고, 그 위에 포트폴리오용 정적 플랫폼 레이어를 얹는다.

현재 기본 진입점은 두 개다.

- `app/index.html`: 작업용 포트폴리오 canary. 대표 5개 예시를 먼저 보고 방향을 고르는 얇은 화면이다.
- `legacy/current_archive/index.html`: 기존 통합 아카이브 대시보드다.
- `legacy/current_archive/source-admin.html`: 출처 단위 수집·갱신 관리자다.
- `legacy/current_archive/approval-requests.html`: OpenNana 등 외부 수집 후보의 사람 승인 대기 큐다.
- 승인 초안의 durable 사본은 `data/private-research/opennana/decisions/decision-draft.json`에 유지된다.
- `legacy/current_archive/duplicate-review.html`: 완전중복과 이미지 유사 후보를 원본 변경 없이 비교하는 읽기 전용 화면이다.

## 현재 상태 한줄 요약

- 플랫폼 루트 안의 물리 파일 수와 브라우저가 보여 주는 레코드 수는 다르다.
- `legacy/current_archive/`의 안전 이전과 현재 18,815개 전체 canonical JSONL 투영은 완료됐다.
- 기존 대용량 JSON·JSONL·SQLite는 삭제하지 않고 추적 가능한 입력/호환 projection으로 보존한다. 현재 브라우저는 아직 레거시 JS projection을 읽는다.
- OpenNana 전용 자동화는 `src/opennana/`와 `data/private-research/opennana/`에 격리했다. 수집 후 사람이 전체 큐를 결정하면 승인·그룹 항목만 비공개 OpenNana 아카이브 lane으로 자동 승계한다. 이 결정은 권리·상업 이용·외부 공개 승인이 아니다.
- 포트폴리오 공개와 OSS 출처 재사용 경계는 `docs/RIGHTS_AND_PORTFOLIO_POLICY.md`를 기준으로 한다.
- 공개 GitHub 소스는 `src/github_sources/`의 exact allowlist canary로만 조사한다. 현재 활성 workflow는 오프라인 계약 검증 전용이며, 원격 저장소 수동 실행이 통과하기 전에는 스케줄·바이너리 다운로드·정본 승격을 열지 않는다.
- canonical inventory의 직접 공개 원격 이미지 URL은 사용자 승인에 따라 전부 로컬 private cache 대상으로 전환했다. 완료 receipt를 건너뛰며 중단 후 재개할 수 있지만, 공개/R2 승격과 항목별 사용 권리는 계속 별도다.
- 구조 판단은 `docs/REFACTOR_STATUS.md`, 정본 계약은 `docs/CANONICAL_DATA.md`, 숫자 해석은 `docs/INVENTORY.md`, 최신 기계 판독 수치는 `data/canonical/archive_inventory.json`을 기준으로 본다.

운영 원칙:

- `legacy/current_archive/`는 레거시 원장이다. 내부 구조를 임의로 재배열하지 않는다.
- 새 플랫폼에서 직접 노출하는 이미지는 `media/public/featured/`로 승격 복사한다.
- 전체 내부 정본은 `data/canonical/archive_records.jsonl`, 대표 5개 큐레이션 원장은 `data/canonical/featured_five.json`이다.
- 사람이 고른 뒤 제작하는 흐름을 전제로 한다. 이 루트에서는 생성 기능을 제공하지 않는다.
- 배포는 아직 하지 않는다. `deploy/`는 Workers Static Assets + Worker API + R2 + Neon 연결을 위한 준비 문서만 담는다.
- 원본은 private evidence로 유지하고 `dist/`에는 압축 파생본만 둔다. 현재 builder는 WebP 우선이며, 번들 Sharp canary에서 미래 공개 bundle용 AVIF도 검증했다.
- 분석은 `no-model → Luna → Terra → Sol(final only)`이며 사람·브랜드 존재만으로 Sol을 호출하지 않는다.
- 공개 v1 전달 규격은 `WebP + JPEG/PNG fallback`으로 확정한다. AVIF 결과는 비공개 벤치마크 증거로만 보존하고 공개 bundle에는 넣지 않는다.
- 원격 미디어는 exact allowlist의 직접 공개 HTTPS URL만 로컬 private cache에 받을 수 있다. 인증·로그인·paywall·private network 우회는 하지 않으며, 다운로드 성공을 공개 또는 상업 이용 허가로 해석하지 않는다.

권장 순서:

1. `app/index.html`로 대표 예시 5개를 검토한다.
2. 마음에 드는 `Reference Style ID`를 고른다.
3. 필요하면 `legacy/current_archive/index.html`에서 전체 라이브러리로 확장 탐색한다.
4. 선택이 끝난 뒤에만 별도 생성 파이프라인으로 넘어간다.

## 디렉터리 역할

| 경로 | 역할 |
|---|---|
| `app/` | `file://`로도 열리는 경량 프런트 |
| `src/` | canonical/public projection과 정적 빌드 |
| `src/opennana/` | 최대 20건 수집 → 정규화 → 중복검사 → 승인 큐 → 사람 확정 → 내부 아카이브 승계를 잇는 파이프라인 |
| `src/github_sources/` | allowlist 기반 공개 GitHub 저장소 metadata/tree 후보 수집 canary; 바이너리·외부 링크 본문은 받지 않음 |
| `data/canonical/` | 기존 18,792개와 확정된 OpenNana lane을 합친 내부 정본, manifest, 사람의 큐레이션 결정 원장 |
| `data/private-research/` | 권리·출처 증거와 외부 정본 위치 포인터 |
| `data/private-research/opennana/` | OpenNana 전용 raw/staging/review_queue/decisions/runs 운영 레인 |
| `data/private-research/duplicate-analysis/` | 전역 exact hash와 제한적 pHash/dHash canary의 SQLite·요약 파생물 |
| `data/private-research/remote-media-canary/` | bounded canary와 사용자 승인 전수 로컬 캐시, content-addressed private blob, 재개 receipt, URL query 비노출 cache index |
| `data/private-research/media-benchmarks/` | WebP·AVIF·JPEG/PNG 비공개 포맷 실측 산출물 |
| `data/public-export/` | 권리·릴리스 검토 뒤의 공개 데이터 경계 |
| `media/public/featured/` | 첫 검토에 쓰는 정확히 5개 이미지 |
| `docs/` | 구조·이관·운영 결정 기록 |
| `experiments/` | 배포에서 제외한 self-contained 레퍼런스 실험 |
| `qa/` | 무결성 및 배포 경계 검사 |
| `deploy/` | Workers Static Assets + Worker API + R2 + Neon 준비 자료; 현재 배포 금지 |
| `dist/` | 재생성 가능한 정적 canary |

## 빌드와 검증

모든 mutating 빌드는 기본 dry-run이며 `--apply`를 명시해야 쓴다.

```powershell
python src/build_static_canary.py
python src/build_static_canary.py --apply
python src/build_canonical_archive.py
python src/build_canonical_archive.py --apply
python src/build_duplicate_index.py
python src/build_duplicate_index.py --perceptual-limit 128 --webp-limit 64 --apply
python src/remote_media_canary.py
# 명시적으로 승인된 private canary만; 공개 승격은 하지 않음
python src/remote_media_canary.py --fetch --apply --limit 10
# 사용자 승인 전수 로컬 캐시; 성공 항목은 건너뛰며 중단 후 같은 명령으로 재개
python src/remote_media_canary.py --all --fetch --apply
python src/benchmark_media_formats.py --apply
# IMAGE_ARCHIVE_SHARP_ROOT에 사용 가능한 Sharp package 경로 지정
node src/benchmark_modern_formats.mjs --apply
python src/opennana/build_review_queue_projection.py
python src/opennana/build_review_queue_projection.py --apply
python src/opennana/build_archive_lane.py
python src/opennana/build_archive_lane.py --apply
python src/opennana/run_pipeline.py
# 네트워크 canary는 명시적 승인 범위에서만 실행
python src/opennana/run_pipeline.py --fetch --apply --max-details 20
python qa/validate_canonical_archive.py
python qa/validate_platform.py --write-report
python qa/validate_opennana_workflow.py --write-report
python qa/validate_opennana_review_queue.py
python qa/verify_neon_env.py
python -m unittest qa.test_github_source_collector -v
python -m unittest qa.test_opennana_review_api -v
python -m unittest qa.test_archive_platform_contract -v
python qa/validate_github_workflow.py
python qa/validate_daily_source_workflow.py
# 공개 API read-only canary; --apply 없이는 artifact도 쓰지 않음
python src/github_sources/collect_public_repo.py --repo freestylefly/awesome-gpt-image-2 --fetch --limit 100
# Neon schema는 dry-run -> rollback check -> 명시적 apply 순서
python -m src.archive_platform.migrate
python -m src.archive_platform.migrate --check
```

전수 로컬 캐시는 scope가 무제한인 것이지, 보호 장치를 제거한 무제한 병렬 다운로드가 아니다. 기본 전체 동시성은 4(최대 8)지만 동일 host는 동시성 1과 최소 간격 1초를 유지한다. 파일당 최대 15 MiB, decode 최대 80 MP, checkpoint 간격 25 URL, D: 최소 여유 공간 5 GiB를 유지한다. 429/5xx는 최대 5회 재시도하고 backoff는 60초에서 cap한다. 디스크 하한이나 중단을 만나면 현재 cache index를 보존하고, 공간을 확보한 뒤 같은 명령으로 이어서 받는다.

다운로드된 바이너리는 내부 레퍼런스 가용성을 높이기 위한 사본이다. `dist/`, `media/public/`, R2, 공개 export에는 자동으로 들어가지 않는다. 출처와 항목별 권리 검토 없이 공개 또는 상업 자산으로 승격하지 않는다.

승인 페이지의 항목별 버튼은 브라우저 초안이다. 현재 큐 전체를 결정한 뒤 `확정 및 아카이브 승계`를 누르면 Review API가 큐 버전·콘텐츠 해시·완결성을 다시 검사한다. 요약 확인 후 한 번 더 명시적으로 확정해야 결정 원장·큐·내부 OpenNana lane이 함께 갱신된다. JSON 다운로드는 API 장애 때만 쓰는 복구 수단이다.

승인·그룹 결과는 내부 canonical archive에 반영되지만, `release_eligible=false`를 유지한다. 공개 export에는 프롬프트 원문과 원본 이미지 URL을 승계하지 않으며, 권리·상업 이용·외부 공개 게이트는 자동으로 열리지 않는다.

`app/data/featured-five.js`와 `dist/`는 canonical JSON에서 만든 파생물이다.
`data/public-export/`는 권리 필터링 projection이다. 현재 18,815건이 모두 P3이므로 공개 레코드와 공개 shard는 0개다. P1/P2 승인이 생길 때만 공개 DTO가 생성된다.
대용량 내부 JSONL·legacy JSON·SQLite·원본 수집 이미지는 `dist/`에 포함하지 않는다.

## 로컬 서버

전체 플랫폼과 쓰기 가능한 Review API를 하나의 loopback origin으로 실행한다. 일반 `python -m http.server`는 정적 열람만 가능하므로 승인 확정에 사용하지 않는다.

```powershell
python src/opennana/review_server.py --host 127.0.0.1 --port 8765
```

- 대표 5개: `http://127.0.0.1:8765/app/index.html`
- 전체 18,815개: `http://127.0.0.1:8765/legacy/current_archive/index.html`
- 출처 관리자: `http://127.0.0.1:8765/legacy/current_archive/source-admin.html`
- 체크박스 승인 큐: `http://127.0.0.1:8765/legacy/current_archive/approval-requests.html`
- 중복 그룹 검토: `http://127.0.0.1:8765/legacy/current_archive/duplicate-review.html`
- GitHub Actions 부트스트랩: `docs/GITHUB_ACTIONS_ENABLEMENT.md`
- Neon/API canary: `docs/NEON_SCHEMA.md`

HTTP 회귀 검증:

```powershell
$env:IMAGE_ARCHIVE_HTTP_BASE_URL='http://127.0.0.1:8765'
node qa/smoke_http_server.mjs
```

구조·수량 스냅샷 확인:

```powershell
python src/build_archive_inventory.py
python src/build_archive_inventory.py --apply
```

레거시 이동 뒤의 해시 증거 갱신은 항상 먼저 dry-run한다.

```powershell
python src/refresh_legacy_content_registry.py
python src/refresh_legacy_content_registry.py --apply
python src/refresh_legacy_artifact_manifest.py
python src/refresh_legacy_artifact_manifest.py --apply
```
