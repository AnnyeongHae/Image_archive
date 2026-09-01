# Neon schema and API canary

상태: 2026-09-01 로컬 구현 및 50건 Neon canary 통과. 공개 배포는 아직 하지 않았다.

## 현재 정식 경로

- 버전 스키마: `schemas/image_archive_record.schema.json`
- 마이그레이션: `db/migrations/0001_archive_read_model.sql`, `0002_collection_and_review.sql`
- 체크섬 runner: `src/archive_platform/migrate.py`
- streaming importer: `src/archive_platform/import_canonical.py`
- 공개/관리자 REST canary: `src/archive_platform/api.py`
- Cloudflare Access 및 loopback 인증: `src/archive_platform/auth.py`

구형 default-schema 초안은 실행 경로에서 제거하고 `deploy/drafts/`에 보존했다.

## 저장 원칙

- `image_archive` 전용 schema만 사용한다.
- JSONL 원본은 migration source of truth로 유지한다.
- prompt text는 private table에 한 번 저장하고 검색용 `search_text` 사본은 만들지 않는다.
- 이미지는 Postgres에 넣지 않는다. URI/R2 key, SHA-256, MIME, 크기와 lineage만 저장한다.
- base64 data URI는 DB constraint와 importer에서 모두 거부한다.
- P1/P2 공개 DTO는 `archive_records_public`에만 존재한다.
- P3/P4는 private table에만 존재하며 P4는 별도 `quarantine:read` scope가 필요하다.

## 적용된 핵심 테이블

- `archive_records_private`, `archive_media_private`
- `archive_records_public`
- `import_batches`, `schema_migrations`
- `sources`, `source_runs`, `source_items`
- `review_drafts`, `review_decisions`
- `duplicate_groups`, `duplicate_group_members`

## 운영 명령

저장 없이 plan 확인:

```powershell
python -m src.archive_platform.migrate
```

실제 SQL 실행 후 전체 rollback 검사:

```powershell
python -m src.archive_platform.migrate --check
```

마이그레이션 적용:

```powershell
python -m src.archive_platform.migrate --apply
```

50건 이관 plan/적용:

```powershell
python -m src.archive_platform.import_canonical --limit 50
python -m src.archive_platform.import_canonical --limit 50 --apply
```

전체 이관은 `--all --apply`지만, 18,815건 전체에는 batch/COPY 최적화를 먼저 적용한 뒤 실행한다.

## 2026-09-01 관측 결과

- migration 2개 적용
- private record 50개
- private media metadata 50개
- public record 0개
- P3 50개, P4 0개
- inline base64 0개
- 공개/관리자 HTTP contract 통과
- synthetic transaction에서 P1/P2 공개 허용, P3 공개 차단, P2 prompt 원문 차단 후 전체 rollback 통과

public 0건은 장애가 아니다. 현재 canonical 18,815건이 모두 P3이므로 권리 정책상 정상적인 fail-closed 결과다.

## 인증 경계

- 기본값 `disabled`: 관리자 API 503
- 로컬: 명시적 development runtime + loopback + token 비교
- 운영: `Cf-Access-Jwt-Assertion`의 서명, issuer, audience, expiry, subject와 관리자 이메일 allowlist 검증
- P4: 일반 관리자 권한과 별도인 `quarantine:read`

Python API는 계약과 Neon canary용이다. 최종 Cloudflare Worker 구현은 같은 public/admin DTO와 권한 규칙을 따라야 한다.
