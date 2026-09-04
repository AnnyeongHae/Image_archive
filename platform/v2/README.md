# Image Archive v2

상태: 2026-09-04 Neon/Qdrant 최초 비공개 적재·전량 readback 완료. 새 API 배포·원격 Git 반영·Actions 실행은 아직 완료되지 않았다.

| 경로 | 역할 |
|---|---|
| `worker/` | 소유자 인증, text512 그룹 RAG, Neon 원문/권리 조회 |
| `local/cloud_snapshot.py` | 검증된 379 이미지/377 텍스트/11 질의 export·명시적 cloud sync |
| `local/intake.py` | 소스 중립 인계 검증·offline exact 중복 계획; 사람 승인 생성 안 함 |
| `local/review_bridge.py` | 인증된 입고 영수증 + 로컬 미디어 + 기존 확정 결정 + 이미지 캐시 → 새 관리자 검토 run |
| `local/intake_keys.mjs` | 로컬 RSA 키 생성/재검증; 공개키만 Git 포함 |
| `local/actions_import.py` | 인증된 GitHub run/artifact 계보 검증 후 private 복호화·입고 계획 |
| `local/media_sync.py` | 고정 private R2 bucket으로 명시된 수만 업로드·동일 hash 재사용·readback |
| `local/runtime_setup.py` | 로컬 토큰/설정 준비와 별도 승인된 제한 Neon 역할 생성/검증 |
| `local/release_candidate.py` | 코드/설정 hash와 source allowlist를 묶는 승인 후보; 자체 배포/승인 없음 |
| `tests/` | WebCrypto 인증/HTTP/검색/예산 오프라인 테스트 |
| `../../db/v2/` | 새 비공개 schema; 기존 테이블 변경 안 함 |
| `../../src/github_sources/` | commit/tree 고정, gallery adapter, 암호화된 Actions 인계 |

## API 계약

- `GET /healthz`: 비공개 데이터 없는 상태.
- `POST /api/private/v2/search`: `Authorization: Bearer …`, `rag:search` scope. JSON `{ "query_id": "<stored-id>", "top": 5 }` 또는 `{ "query": "교육용 포스터", "top": 3 }`. top은 1/3/5. 둘을 함께 보내지 않는다.
- `GET /api/private/v2/queries`: `archive:read`, 모델 재호출 없는 저장 질의 목록.
- `GET /api/private/v2/groups/<group-id>?after=<item-id>`: `archive:read`, 최대 20개 멤버/페이지. 원문을 손실 없이 반환.
- `GET /api/private/v2/images/<item-id>`: `archive:read`, 해당 snapshot의 ID만 private R2 해시 키에 연결. 공개 URL이나 임의 source fetch는 제공하지 않는다.
- `GET /api/admin/v2/status`: Access JWT + 정확한 owner email allowlist 필수. API bearer로 관리자 권한을 얻을 수 없다.

검색 결과는 그룹별 최고 멤버의 점수 + 사람 지정 대표 + 접힌 멤버 주소다. Luna metadata는 `needs_review` 후보임을 표시하며 권리 허가로 해석하지 않는다. 이미지 벡터를 text512 query와 비교하지 않는다.

## 오프라인 검증

```powershell
node --test platform/v2/tests/*.test.mjs
python -B -m unittest qa.test_v2_intake qa.test_v2_cloud_snapshot -v
python -B platform/v2/local/cloud_snapshot.py
```

private 입력이 없는 새 clone에서는 cache export dry-run이 차단되는 것이 정상이다. Windows 운영 PC의 검증된 snapshot과 parent `00_CORE` 계약이 필요하다. 의존성은 기존 Python3/Pillow/psycopg2 및 Node22 이상을 사용하며, 자동 설치하지 않는다.

## DB 적재 순서

1. `cloud_snapshot.py` dry-run: 원문/그룹/벡터 캐시 SHA 재검증.
2. `cloud_snapshot.py --apply`: 내용 해시별 private plan 생성.
3. `cloud_snapshot.py --plan <private-plan-dir> --preflight`: 명시적 읽기 전용 Neon/Qdrant 확인.
4. 검토된 plan만 `--plan <private-plan-dir> --apply --execute`: 신규 namespace bounded upload/readback. 새 임베딩 없음.
5. Neon·Qdrant 모두 확인된 `ready` snapshot ID/manifest SHA와 text collection을 Worker에 고정한다. 캐시 키·source code가 바뀌면 새 snapshot이다.

최초 실제 `ready` snapshot: `db218336f4478bf138d9440de0ee605131ae5ef484f322c0e3d7bae2f6e28314`.
manifest SHA: `ae5910fb41af5c0e12d8c203bb203b90ebde3b249099da2ffb4edbe55724b183`.
379개 원문·메타데이터, 379 image1024 / 377 text512, 11 저장 질의, 326 그룹을 전량 재조회했다. Qdrant의 Cosine 정규화는 abs tolerance 2e-6으로 비교하고 로컬 원본 float32는 보존한다. 신규 임베딩 0회, 공개 승격 없음.

이전 개발 중 계획 디렉터리는 이력일 뿐 배포 대상으로 사용하지 않는다. Windows에서는 임시 디렉터리에서 생성된 파일을 이동할 때 접근 ACL이 따라오는 문제가 확인되어, 최종 ignored workspace 내 exclusive 생성·manifest-last 방식으로 수정했다. Windows ACL은 기존 workspace secret-store 상속이며 owner-only 강화를 주장하지 않는다.

## 배포 설정 — 기본 비활성

`wrangler.jsonc`는 기본 API·새 질의 embedding을 끈다. 실제 승인된 runtime 설정에는 `SNAPSHOT_ID`, `SNAPSHOT_MANIFEST_SHA256`, `TEXT_COLLECTION`이 필요하다. secrets는 `DATABASE_URL`, `QDRANT_ENDPOINT`, `QDRANT_API_KEY`, `API_TOKEN_HASHES`; 새 질의 호출을 승인한 경우에만 `VOYAGE_API_KEY`를 추가한다.

Access 설정: `ACCESS_JWT_REQUIRED=true`, `TEAM_DOMAIN`, `POLICY_AUD`, `OWNER_EMAIL_ALLOWLIST`(JSON 문자열 배열). missing/wildcard owner 설정은 거부한다. API 토큰 descriptor는 `{id,sha256,scopes,expires_at,revoked}`만 허용하며, 원문 토큰은 로컬 개인 설정에만 보관한다. 토큰은 만료/폐기할 수 있고 `admin:read`를 부여할 수 없다.

Cloudflare CLI가 기존 환경에 있으면 `deploy --dry-run --config platform/v2/wrangler.jsonc`로 bundle 검증한다. 실제 deploy는 정확한 artifact/target release gate 뒤에만 실행한다. 새 API 전용 Worker는 공개 정적 갤러리 bundle을 포함하지 않는다.

### 현재 활성화 대기 사항

로컬 scoped API token·90일 만료·제한 계정용 설정은 준비했지만, 실제 계정 생성이나 Worker secret 업로드/배포를 하지 않았다. `runtime_setup.py --plan <plan> --owner-email <confirmed-email> --apply`는 로컬 준비, 추가 `--execute`는 원격 권한 변경이며 별도 승인 대상이다. 성공 후 `--verify`로 권한·로그인을 읽기 전용 재확인한다. 기존 Access audience를 설정에 재사용하는 것만으로 새 Worker hostname의 Access 앱 연결이 완료되지 않는다.

2026-09-04 자동 안전 검토는 R2 3개 전송과 제한 Neon 역할 생성을 명시 승인 부족으로 차단했다. 두 명령 모두 실행 전에 거부되었으며 우회하지 않았다. 권한 계정의 제안 owner email 확인, exact private 이미지/버킷 승인, 새 Worker artifact/target 승인과 GitHub CLI 로그인이 필요하다. Qdrant 기존 API key의 collection-read-only scope도 별도 확인 전에는 최소 권한이라고 주장하지 않는다.

Worker 빌드 dry-run 통과: 36.87KiB / gzip 10.21KiB. private 이미지 응답은 크기·PNG signature·SHA-256을 검증하지만, 전체 15MiB 상한에서 Workers Free CPU 내 동작하는지는 배포 후 canary 실측이 필요하다. R2 uploader의 lock은 동일 로컬 도구 간 single-writer만 보장하며, 외부의 다른 writer까지 막는 원자적 create-only는 아니다.

GitHub 인계 명령은 `python -B platform/v2/local/actions_import.py --run-id <id>`가 dry-run이다. 로그인 및 실제 run 확인 후 `--run-attempt <n> --expected-head-sha <40hex> --fetch --apply`를 추가한다. ZIP/암호문/복호화 hash와 인증된 origin을 남기되, GitHub reported artifact digest의 byte scope는 미검증으로 구분한다. 이 명령은 이미지 다운로드·사람 승인·Luna 실행을 하지 않는다.

`qa/verify_v2_cloud_search.py --plan <plan>`은 오프라인 계획이며 `--verify`를 명시해야만 Neon read-only / Qdrant 그룹 검색을 한다. 11개 기존 질의 벡터만 사용하고 대표 연결·서로 다른 그룹·로컬 코사인 기준선을 확인한다. API 배포·인증이 완료되었다는 증거로 혼동하지 않는다.

배포 후보는 `release_candidate.py --runtime-directory <private-runtime-directory> --apply`로 고정한다. secret 파일은 포함하지 않으며 `candidate.json`의 `eligible_for_release`는 계속 false다. 코드/설정/대상 변경 시 새 후보 hash와 승인 필요. 공개 Git에는 `qa/validate_repository_boundary.py --candidate-files`의 source-only 제안만 검토해 올리고, staging 후 인덱스 자체를 다시 검사한다. `git add .`나 대용량 원장 포함·force push를 기본으로 삼지 않는다.

## 운영 경계

- `intake.py`는 후보 계획까지다. 신뢰한 GitHub repo/workflow/run의 인계인지 별도로 확인하고, 로컬 미디어·이미지 벡터 후보·사람 승인·Luna를 순서대로 거친다. 불특정 봉인 파일을 승인된 입력으로 해석하지 않는다.
- 완전 중복: `exact_file OR (same full pixels AND nonblank exact prompt)`. 프롬프트 일치만으로 삭제하지 않는다. JSON prompt tier1, 구조화 tier2, 일반 텍스트 tier3, 같은 tier는 먼저 입고된 기록을 우선한다.
- v2 입고→기존 4단계 관리자 연결은 [입고 검토 연결 계약](../../docs/IMAGE_ARCHIVE_V2_INTAKE_REVIEW.md)을 따른다. 기존 확정 결정을 동결 기준선으로 읽고 새 run은 1단계부터 시작한다. 이미지 캐시가 없으면 serve 가능한 spec을 만들지 않는다. 실제 Actions 인계 영수증이 아직 없으므로 전체 실운영 연결 성공으로 보고하지 않는다.
- 원본·private key·DB·원문 인계·Qdrant payload 전체를 GitHub에 올리지 않는다. GitHub에는 코드/계약/합성 테스트만.
- 무료 클라우드는 복구 원장이 아니다. 로컬 snapshot/벡터/cache와 외부 source pointer를 보존한다.

설계 근거: [ADR-002](../../docs/IMAGE_ARCHIVE_V2_DEPLOYMENT.md), 기존 운영 순서: [ADR-001](../../docs/IMAGE_RAG_PIPELINE_DECISION.md).
